# -*- coding: utf-8 -*-
"""Task 8 —— 两个消费点：值在插件进程里被换掉，模型只看得到模板与 `***`。

文件分两段。前半是计划 Task 8 Step 1 的逐字用例（`ExecTest` / `ExportTest` 与那两个
helper），只对两处**纯 lint**的东西动了刀：计划里那句没人用的 `import os`（F401）与
截断用例里那个不带占位符的 `f` 前缀（F541，去掉后字符串一字不变）；断言、命令实串、
期望值全部原样。后半是本任务自审追加的腿（封顶常量与上界、note、env_name、cwd 放行、
取消三条腿、超时、装配缺陷、日志卫生、状态闭集、R14 单循环门），每条在注释里写清
为什么值得钉。
"""

import ast
import asyncio
import inspect
import json
import logging
import tempfile
import time
import unittest
from pathlib import Path

from tests.support import DUMMY

import backend.tools as tools_mod
from backend.config import PluginConfig
from backend.records import RequestRegistry
from backend.scrub import REDACT, Scrubber
from backend.tools import SECRET_MARKER, SecretTools
from backend.vault import Vault


def make(env_writer=None):
    vault = Vault(ttl_seconds=300)
    registry = RequestRegistry(vault, timeout_seconds=600, max_pending=8)
    scrubber = Scrubber()
    ref = vault.issue(DUMMY)
    scrubber.register(DUMMY)
    tools = SecretTools(registry=registry, vault=vault, scrubber=scrubber,
                        config=PluginConfig(), identity=lambda: {},
                        cancel_event=lambda: None, env_writer=env_writer,
                        project_dir=lambda: self_dir())
    return tools, vault, scrubber, ref


def self_dir():
    return str(Path(tempfile.gettempdir()).resolve())


def run(coro):
    return asyncio.run(coro)


_TOOLS_LOGGER = logging.getLogger("backend.tools")
_NULL_HANDLER = logging.NullHandler()


def setUpModule():
    """让全量跑的输出保持干净：三条**故意**走到失败支的用例（计划逐字那条也在内）不
    捕获日志，record 会经 propagate 落到 root 的 lastResort 而打到 stderr。

    这里只挂一个 NullHandler —— 它挡的是 lastResort，不是捕获：`assertLogs` 自己往这个
    logger 插 handler 并把 propagate 关掉，那些用例照样读得到记录。生产代码一行没改。
    """
    _TOOLS_LOGGER.addHandler(_NULL_HANDLER)


def tearDownModule():
    _TOOLS_LOGGER.removeHandler(_NULL_HANDLER)


class ExecTest(unittest.TestCase):
    def test_placeholder_is_substituted_outside_the_model_and_output_is_scrubbed(self):
        tools, _v, _s, ref = make()
        out = json.loads(run(tools.secret_exec(
            ref, f'printf "%s" {SECRET_MARKER}')))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["stdout"], REDACT)          # 模型侧看到的是 ***
        self.assertEqual(out["ref"], "consumed")

    def test_child_really_sees_the_value_via_env(self):
        # 阳性对照：子进程写进文件的必须是真值，否则"替身成功"是假的
        tools, _v, _s, ref = make()
        probe = Path(tempfile.mkdtemp()) / "seen.txt"
        out = json.loads(run(tools.secret_exec(
            ref, f'printf "%s" "${{SECRET}}" > {probe} && echo done')))
        self.assertEqual(out["status"], "ok", out)
        self.assertEqual(probe.read_text(encoding="utf-8"), DUMMY)
        self.assertNotIn(DUMMY, json.dumps(out))

    def test_second_use_of_same_ref_is_ref_unavailable(self):
        tools, _v, _s, ref = make()
        run(tools.secret_exec(ref, "true"))
        out = json.loads(run(tools.secret_exec(ref, "true")))
        self.assertEqual(out["status"], "ref_unavailable")
        self.assertIn("request_secret", out["hint"])

    def test_expired_ref_reports_expired(self):
        tools, vault, _s, ref = make()
        vault._issued[ref] = (DUMMY, 0.0)
        vault.sweep()
        out = json.loads(run(tools.secret_exec(ref, "true")))
        self.assertEqual(out["status"], "ref_expired")

    def test_output_is_capped_below_the_host_pruning_threshold(self):
        tools, _v, _s, ref = make()
        out = json.loads(run(tools.secret_exec(
            ref, "seq 1 20000 | tr '\\n' ' '; echo", max_output_bytes=1024)))
        # Fix round 1 / I-1(1)：截断标记在预算**里面**预留。原来的 `_clip` 是"切 limit 字节
        # 再把 ~72 B 的标记**追加**上去"，所以 `limit` 从来不是这一流的上界，
        # "emitted ≤ max_output_bytes" 那句是假的。现在它是字面真的，断言因此能收紧到 limit。
        self.assertLessEqual(len(out["stdout"].encode()), 1024)
        self.assertIn("[truncated by secret-input]", out["stdout"])

    def test_cwd_outside_project_dir_is_refused_and_ref_not_consumed(self):
        tools, vault, _s, ref = make()
        out = json.loads(run(tools.secret_exec(ref, "true", cwd="/")))
        self.assertEqual(out["status"], "error")
        # 有项目上下文时说的是 containment（不是"没有上下文"那句），文案漂了要红。
        self.assertIn("project directory", out["message"])
        self.assertEqual(vault.status(ref), "live")      # 失败绝不消费 ref

    def test_failing_command_still_returns_string_without_value(self):
        tools, _v, _s, ref = make()
        out = json.loads(run(tools.secret_exec(
            ref, f'printf "%s" {SECRET_MARKER} >&2; exit 7')))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["exit_code"], 7)
        self.assertEqual(out["stderr"], REDACT)

    def test_internal_error_string_carries_no_value(self):
        tools, _v, _s, ref = make()
        tools._vault.consume = lambda _r: (_ for _ in ()).throw(RuntimeError(DUMMY))
        out = json.loads(run(tools.secret_exec(ref, "true")))
        self.assertEqual(out["status"], "error")
        self.assertNotIn(DUMMY, json.dumps(out))


class ExportTest(unittest.TestCase):
    def test_export_writes_env_key_and_consumes_ref(self):
        seen = {}

        def writer(key, value):
            seen[key] = value
            return {key: value}

        tools, vault, _s, ref = make(env_writer=writer)
        out = json.loads(run(tools.secret_export(ref, "GITHUB_TOKEN")))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(seen["GITHUB_TOKEN"], DUMMY)     # 插件确实把值交出去了
        self.assertNotIn(DUMMY, json.dumps(out))
        self.assertEqual(vault.status(ref), "gone")
        # spec §7.3 的返回契约把"值落在哪儿"写进了 hint 的字面串里，其中
        # `(Fernet, 0600)` 是操作者判断"这个暴露面是加密落盘还是明文落盘"的唯一线索。
        # Fix round 1 minor：文案原来只说 "host env store and process environment"。
        self.assertIn("(Fernet, 0600)", out["hint"])
        self.assertIn("host process environment", out["hint"])

    def test_bad_env_key_is_rejected_and_ref_survives(self):
        tools, vault, _s, ref = make(env_writer=lambda k, v: {})
        for bad in ("lower_case", "1TOKEN", "A" * 80, "GITHUB TOKEN", ""):
            out = json.loads(run(tools.secret_export(ref, bad)))
            self.assertEqual(out["status"], "error", bad)
            self.assertEqual(vault.status(ref), "live")

    def test_writer_failure_does_not_consume_ref(self):
        def boom(_key, _value):
            raise OSError("env store is read-only")

        tools, vault, _s, ref = make(env_writer=boom)
        out = json.loads(run(tools.secret_export(ref, "GITHUB_TOKEN")))
        self.assertEqual(out["status"], "error")
        self.assertIn("not consumed", out["message"])
        self.assertEqual(vault.status(ref), "live")


class CombinedBudgetTest(unittest.TestCase):
    """Fix round 1 / I-1（R47，实测过）：封顶必须是**两条流合起来**的预算。

    原来 `limit` 是逐流参数，两条流各自 32768+ε，合起来能到 **65803 B** —— 正好越过宿主
    `ToolResultPruningMiddleware` 的 recent 阈值 50000 B，于是 §10.1 路径⑥ 的那个磁盘工件
    从"永远不会产生"变成"极端输出下天天产生"。这一类用例量的是**宿主真正读的那个数**
    （序列化后的整条结果），不是常量比较。
    """

    BOTH_FULL = ("{ head -c 40000 /dev/zero | tr '\\0' 'x'; "
                 "head -c 40000 /dev/zero | tr '\\0' 'y' >&2; }")
    BOTH_FULL_NEWLINES = ("{ head -c 40000 /dev/zero | tr '\\0' '\\n'; "
                          "head -c 40000 /dev/zero | tr '\\0' '\\n' >&2; }")

    def test_both_streams_full_stays_below_the_host_pruning_threshold(self):
        # 这条取代原来那句 `assertLess(_MAX_OUTPUT_BYTES, 50000)`：同样的名字，
        # 量的是产物而不是常数。
        tools, _v, _s, ref = make()
        result = run(tools.secret_exec(ref, self.BOTH_FULL))
        size = len(result.encode("utf-8"))
        self.assertLess(size, 50000, f"serialized result is {size} B")
        out = json.loads(result)
        self.assertLessEqual(len(out["stdout"].encode("utf-8")), 32768)
        self.assertLessEqual(len(out["stderr"].encode("utf-8")), 32768)
        self.assertIn("[truncated by secret-input]", out["stdout"])
        self.assertIn("[truncated by secret-input]", out["stderr"])
        self.assertNotIn(DUMMY, result)

    def test_escape_heavy_output_is_budgeted_in_serialized_bytes(self):
        # JSON 转义把 `\n`/`"`/控制字符**每个原始字节翻成两个**：按原始字节记账的话，
        # 32 KiB 换行 = 64 KiB 序列化，工件照样落盘（实测：单条流就 65755 B）。
        # 预算必须按"进 JSON 之后占多少字节"给，否则上面那条断言只是运气好。
        tools, _v, _s, ref = make()
        result = run(tools.secret_exec(ref, self.BOTH_FULL_NEWLINES))
        size = len(result.encode("utf-8"))
        self.assertLess(size, 50000, f"serialized result is {size} B")
        self.assertIn("[truncated by secret-input]", json.loads(result)["stdout"])

    def test_two_full_streams_split_the_budget_in_half_each(self):
        tools, _v, _s, ref = make()
        out = json.loads(run(tools.secret_exec(ref, self.BOTH_FULL)))
        for stream in ("stdout", "stderr"):
            size = len(out[stream].encode("utf-8"))
            self.assertGreaterEqual(size, 32768 // 2 - 256, stream)
            self.assertLessEqual(size, 32768 // 2, stream)

    def test_unused_allowance_flows_to_stderr(self):
        # give-when-unused：stdout 短，stderr 可以拿掉剩下的几乎整个预算。
        tools, _v, _s, ref = make()
        out = json.loads(run(tools.secret_exec(
            ref, "printf tiny; head -c 40000 /dev/zero | tr '\\0' 'e' >&2")))
        self.assertEqual(out["stdout"], "tiny")
        self.assertGreater(len(out["stderr"].encode("utf-8")), 32768 - 256)
        self.assertLessEqual(len(out["stderr"].encode("utf-8")), 32768)

    def test_a_nonempty_stderr_reclaims_half_the_pool_even_when_it_is_short(self):
        # 评审给的形状：stderr 有话说而额度不够 ⇒ 两流各分 `limit // 2`。
        # 这个代价要如实记：stdout 已经写满时，哪怕 stderr 只有 3 字节，stdout 也会被砍到
        # 一半。替代形状（只从 stdout 手里要 `min(se, floor)` 那么多）记在报告"顾虑"里，
        # 等 Task 9/12 判 —— 本轮不单方面改掉裁定给的补救式。
        # 但**空 stderr 不许触发这条**：靠 `stderr_len > share` 那个守卫。少了它，最常见的一类
        # 命令（>32 KiB stdout、没有 stderr）也会白丢一半 stdout —— 那是裁定字面式里的 bug，
        # 不是我的选择，所以守卫保留（下面 `silent` 那一腿就是它的用例）。
        tools, _v, _s, ref = make()
        out = json.loads(run(tools.secret_exec(
            ref, "head -c 40000 /dev/zero | tr '\\0' 'o'; printf OOO >&2")))
        self.assertEqual(out["stderr"], "OOO")
        self.assertLessEqual(len(out["stdout"].encode("utf-8")), 32768 // 2)
        self.assertGreater(len(out["stdout"].encode("utf-8")), 32768 // 2 - 256)

        tools, _v, _s, ref = make()
        silent = json.loads(run(tools.secret_exec(
            ref, "head -c 40000 /dev/zero | tr '\\0' 'o'")))
        self.assertEqual(silent["stderr"], "")
        self.assertGreater(len(silent["stdout"].encode("utf-8")), 32768 - 256)
        self.assertLessEqual(len(silent["stdout"].encode("utf-8")), 32768)
        self.assertLessEqual(len(out["stdout"].encode("utf-8")), 32768)

    def test_budget_arithmetic_holds_for_every_shape(self):
        # 纯函数层的合池不变式：deterministic、每流 ≤ 上限、总和 ≤ 上限，而且
        # "stderr 被额度卡住"只发生在它真的还有话要说的时候（floor 那条的准确形式）。
        limit = 4096
        shapes = [(0, 0), (10, 10), (99999, 10), (10, 99999), (99999, 99999),
                  (2000, 2000), (4000, 4000), (4080, 4096), (4090, 99999)]
        for so_len, se_len in shapes:
            with self.subTest(stdout=so_len, stderr=se_len):
                a, b = tools_mod._budget_pair(so_len, se_len, limit)
                self.assertLessEqual(a, limit)
                self.assertLessEqual(b, limit)
                self.assertLessEqual(a + b, limit)
                self.assertEqual((a, b), tools_mod._budget_pair(so_len, se_len, limit))
                self.assertLessEqual(a, so_len)
                self.assertLessEqual(b, se_len)
                if b < min(limit // 4, 4096):
                    # 拿到少于 floor 的额度 ⇒ 它本来就没那么多话要说（全给了）。
                    self.assertEqual(b, se_len)
                if a < min(limit // 4, 4096):
                    self.assertEqual(a, so_len)

    def test_clipped_notice_is_inside_the_budget(self):
        # `_clip` 的两条硬不变式（旧实现两条都违反：先切 limit 再**追加**标记）：
        # 1. 输出（正文 + 标记）的序列化字节数 ≤ limit；
        # 2. 不截断时原样返回，一个字节都不少。
        for limit in (1024, 4096, 32768):
            text = "z" * (limit * 2)
            clipped = tools_mod._clip(text, limit)
            with self.subTest(limit=limit):
                self.assertLessEqual(tools_mod._wire_len(clipped), limit)
                self.assertIn("[truncated by secret-input]", clipped)
        whole = tools_mod._clip("abcd", 1024)
        self.assertEqual(whole, "abcd")


# ---------------------------------------------------------------- 自审追加的腿
#
# 计划那 11 条钉住了正常形状。下面这些钉的是只在这两个工具里存在的承诺：
# 输出封顶是安全控制（§10.1 路径⑥）、占位符没用上时必须说一句、取消与超时都不许
# 让一次性 ref 悄悄花掉、装配缺陷不许伪装成用户错误、日志通道必须干净、状态是闭集。


def render(records):
    """按宿主 handler 的口径渲染整条记录，含 traceback 段。

    只读 `record.getMessage()` 的断言会把 `logger.exception` 判成干净 —— 异常原文活在
    traceback 段里。Task 5 的 `tests/test_middleware.py::render` 是同一条理由（R34）。
    """
    formatter = logging.Formatter()
    return "\n".join(formatter.format(record) for record in records)


def build(env_writer=None, cancel=None, project_dir=None):
    """`make()` 的另一形状：只换注入缝，默认与 `make()` 逐字相同。

    计划那 11 条因此可以原样保留（它们要的是"没有取消、env_writer 可注入"的默认形状），
    追加用例又能量到 cancel / project_dir 这两条缝。
    """
    vault = Vault(ttl_seconds=300)
    registry = RequestRegistry(vault, timeout_seconds=600, max_pending=8)
    scrubber = Scrubber()
    ref = vault.issue(DUMMY)
    scrubber.register(DUMMY)
    tools = SecretTools(registry=registry, vault=vault, scrubber=scrubber,
                        config=PluginConfig(), identity=lambda: {},
                        cancel_event=cancel or (lambda: None), env_writer=env_writer,
                        project_dir=project_dir or (lambda: self_dir()))
    return tools, vault, scrubber, ref


class _FakeChild:
    """假子进程：不 fork、不 sleep，`kill()` 只记一笔。

    `communicate` 的几种命运由 mode 决定，全部由**用例**控制，不碰墙上时钟：
      hang       —— 挂在永不 set 的 Event 上（取消腿/超时腿/第二次取消腿用）
      hard_cancel —— 自己抛 CancelledError（宿主硬取消腿用）
      boom       —— 抛一个非取消异常（I-4：`comm.result()` 炸掉时也得收尸）
    `cancel_after_spawn` 让子进程一起就把 cancel_event 按下去（轮询取消腿用）。
    `on_comm_cancel` 在 communicate 被取消的那一刻回调 —— fix round 1 / I-3 用它把
    "**第二次**取消打进清理路径的那一次 await"这个形状演出来（不靠 sleep 猜时序）。
    """

    def __init__(self, stops, mode="hang", cancel_event=None, cancel_after_spawn=False,
                 on_comm_cancel=None, cancel_on_start=None):
        self.stops = stops
        self.returncode = None
        self._mode = mode
        self._released = asyncio.Event()
        self._cancel_event = cancel_event
        self._cancel_after_spawn = cancel_after_spawn
        self._on_comm_cancel = on_comm_cancel
        self._cancel_on_start = cancel_on_start

    async def communicate(self):
        if self._cancel_on_start is not None:
            # 复现协调器的 force 路径：取消打在工具任务**正 await 轮询**的那一刻。
            self._cancel_on_start()
        if self._cancel_after_spawn and self._cancel_event is not None:
            self._cancel_event.set()
        if self._mode == "hard_cancel":
            raise asyncio.CancelledError
        if self._mode == "boom":
            raise OSError("read error on fd 3: transport endpoint is gone")
        try:
            await self._released.wait()
        except asyncio.CancelledError:
            if self._on_comm_cancel is not None:
                self._on_comm_cancel()
            raise
        return b"", b""

    def kill(self):
        self.stops.append("kill")

    async def wait(self):
        self.stops.append("wait")
        self.returncode = -9
        return self.returncode


def patch_spawn(factory):
    """换掉 `asyncio.create_subprocess_exec`，返回 (original)；用例自己 addCleanup 还原。

    这三个腿（取消/超时/硬取消）在真机上要等真进程，单测里既不许 sleep 也不许起线程，
    所以把 spawn 这一缝换成假子进程：腿的顺序、杀进程的动作、ref 的去向全都可观察。
    """
    original = asyncio.create_subprocess_exec

    async def fake_spawn(*args, **kwargs):
        return factory(args, kwargs)

    asyncio.create_subprocess_exec = fake_spawn
    return original


class MarkerAndNoteTest(unittest.TestCase):
    def test_marker_constant_and_env_key_pattern_are_the_documented_literals(self):
        # 文案里那句正则就是接口（模型照字面改 env_key），漂一个字符都得红。
        self.assertEqual(SECRET_MARKER, "{{secret}}")
        self.assertEqual(tools_mod._ENV_KEY_RE.pattern, r"^[A-Z_][A-Z0-9_]{0,63}$")
        self.assertEqual(tools_mod._MIN_OUTPUT_BYTES, 1024)
        self.assertEqual(tools_mod._MAX_OUTPUT_BYTES, 32768)
        # Fix round 1 / I-1(2)：这里原来钉的是 `assertLess(_MAX_OUTPUT_BYTES, 50000)`——
        # 一条"常量比阈值小所以工件不会产生"的安慰断言，而它量错了对象：宿主剪枝读的是
        # **序列化后的整条结果**，两条流各自 32768 合起来能到 65803 B（实测，见
        # `CombinedBudgetTest`）。那个比较永远绿，缺陷永远在。权威改成量产物本身的那条。
        sig = inspect.signature(SecretTools.secret_exec)
        self.assertEqual(sig.parameters["env_name"].default, "SECRET")
        self.assertEqual(sig.parameters["max_output_bytes"].default, 32768)
        self.assertEqual(sig.parameters["timeout_seconds"].default, 120)
        # spec §7.2 写的是 `cwd: str | None = None`；工具签名就是模型在 schema 里看到的
        # 那一行，`cwd: str = ""` 是 shipped 与 spec 的漂移（fix round 1 minor）。
        self.assertIsNone(sig.parameters["cwd"].default)
        self.assertIn("None", str(sig.parameters["cwd"].annotation))

    def test_note_names_the_placeholder_gap_and_only_when_it_is_unused(self):
        tools, _v, _s, ref = make()
        out = json.loads(run(tools.secret_exec(ref, "true")))
        self.assertEqual(out["status"], "ok")
        self.assertIn("placeholder unused", out["note"])
        self.assertIn("$SECRET", out["note"])
        self.assertNotIn(DUMMY, json.dumps(out))

        tools, _v, _s, ref = make()
        used = json.loads(run(tools.secret_exec(ref, f'printf "%s" {SECRET_MARKER}')))
        self.assertNotIn("note", used)

    def test_custom_env_name_reaches_the_child(self):
        # 默认名 SECRET 已由计划那条 probe 用例钉住；这一条钉"换一个名字也一样真"。
        tools, _v, _s, ref = make()
        probe = Path(tempfile.mkdtemp()) / "custom.txt"
        out = json.loads(run(tools.secret_exec(
            ref, f'printf "%s" "${{MY_TOKEN}}" > {probe}', env_name="MY_TOKEN")))
        self.assertEqual(out["status"], "ok", out)
        self.assertEqual(probe.read_text(encoding="utf-8"), DUMMY)
        self.assertIn("$MY_TOKEN", out["note"])

    def test_empty_env_name_falls_back_to_the_default_key(self):
        # 计划写的是 `str(env_name or "SECRET")`：空串要回落到 SECRET，而且 note 里说的
        # 必须就是实际注入的那个键（原来两处各写各的，空串时 note 会说成 `$.`）。
        tools, _v, _s, ref = make()
        probe = Path(tempfile.mkdtemp()) / "fallback.txt"
        out = json.loads(run(tools.secret_exec(
            ref, f'printf "%s" "${{SECRET}}" > {probe}', env_name="")))
        self.assertEqual(out["status"], "ok", out)
        self.assertEqual(probe.read_text(encoding="utf-8"), DUMMY)
        self.assertIn("$SECRET", out["note"])

    def test_lowercase_env_name_works(self):
        # 收小写是刻意的（`_ENV_NAME_RE` 比 `_ENV_KEY_RE` 宽）：小写名子进程一样读得到，
        # 拒绝它没有安全收益，只有摩擦。这条红 = 有人把两条规则合并成了一条。
        tools, _v, _s, ref = make()
        probe = Path(tempfile.mkdtemp()) / "lower.txt"
        out = json.loads(run(tools.secret_exec(
            ref, f'printf "%s" "${{myToken}}" > {probe}', env_name="myToken")))
        self.assertEqual(out["status"], "ok", out)
        self.assertEqual(probe.read_text(encoding="utf-8"), DUMMY)

    def test_unusable_env_name_is_refused_before_the_ref_is_spent(self):
        # env_name 与 cwd/env_key 同一档：坏名字必须在消费之前挡住，否则 execve 抛在
        # 消费之后 = 人类白答一次（`test_cwd_outside_project_dir_…` 钉的是同一条纪律）。
        tools, vault, _s, ref = make()
        for bad in ("has space", "1LEAD", "with=equals", "nul\x00x", "A" * 65, "dash-name"):
            out = json.loads(run(tools.secret_exec(ref, "true", env_name=bad)))
            with self.subTest(env_name=bad):
                self.assertEqual(out["status"], "error")
                self.assertIn("env_name must match", out["message"])
                self.assertIn("not consumed", out["message"])
                self.assertEqual(vault.status(ref), "live")


class OutputCapTest(unittest.TestCase):
    """封顶两侧都要夹住：`1024..32768`。只夹下界等于把默认值让给模型开。"""

    def test_request_above_the_ceiling_is_clamped_down(self):
        tools, _v, _s, ref = make()
        out = json.loads(run(tools.secret_exec(
            ref, "seq 1 20000 | tr '\\n' ' '; echo", max_output_bytes=999999)))
        self.assertLessEqual(len(out["stdout"].encode()), 32768)
        self.assertIn("[truncated by secret-input]", out["stdout"])

    def test_request_below_the_floor_is_clamped_up(self):
        tools, _v, _s, ref = make()
        out = json.loads(run(tools.secret_exec(
            ref, "seq 1 20000 | tr '\\n' ' '; echo", max_output_bytes=1)))
        self.assertLessEqual(len(out["stdout"].encode()), 1024)
        # 下界那条仍然量得到东西：正文 = `limit` 减掉预留的标记（~50 B），不是随便小一点。
        self.assertGreaterEqual(len(out["stdout"].encode()), 1024 - 128)

    def test_short_output_is_returned_whole_without_a_truncation_notice(self):
        # 反面对照：封顶不许把正常输出也啃一口。
        tools, _v, _s, ref = make()
        out = json.loads(run(tools.secret_exec(ref, "printf abcd")))
        self.assertEqual(out["stdout"], "abcd")
        self.assertNotIn("truncated", out["stdout"])

    def test_output_ceiling_clamp_is_a_live_gate(self):
        # 封顶的**上界**（32768）单独钉一条：把 `min(..., _MAX_OUTPUT_BYTES)` 摘掉的话，
        # 模型传 `max_output_bytes=10**9` 就能让结果长成"超过宿主剪枝阈值"的形状，
        # 于是 §10.1 路径⑥ 的那个磁盘工件回来了。这条红 = 那条路被封着。
        # fix round 1 / I-1：上界现在是**两条流合起来的预算**，所以 `+67` 的余量没了。
        tools, _v, _s, ref = make()
        huge = json.loads(run(tools.secret_exec(
            ref, "seq 1 20000 | tr '\\n' ' '; echo", max_output_bytes=10 ** 9)))
        self.assertLessEqual(len(huge["stdout"].encode()), tools_mod._MAX_OUTPUT_BYTES)


class CwdGuardTest(unittest.TestCase):
    def test_relative_cwd_inside_the_project_dir_is_allowed(self):
        # containment 不是"只准绝对路径"：相对路径拼到 project_dir 里必须放行，读得到
        # 那个目录里的文件才算真放行了（`pwd` 在 dash 下会读继承来的 $PWD，不可信）。
        base = Path(tempfile.mkdtemp())
        (base / "sub").mkdir()
        (base / "sub" / "hello.txt").write_text("inside", encoding="utf-8")
        tools, vault, _s, ref = build(project_dir=lambda: str(base))
        out = json.loads(run(tools.secret_exec(ref, "cat hello.txt", cwd="sub")))
        self.assertEqual(out["status"], "ok", out)
        self.assertEqual(out["stdout"], "inside")
        self.assertEqual(vault.status(ref), "gone")

    def test_dotdot_escape_is_refused_and_the_ref_stays_live(self):
        base = Path(tempfile.mkdtemp())
        (base / "sub").mkdir()
        tools, vault, _s, ref = build(project_dir=lambda: str(base))
        out = json.loads(run(tools.secret_exec(ref, "true", cwd="sub/../../")))
        self.assertEqual(out["status"], "error")
        self.assertIn("project directory", out["message"])
        self.assertEqual(vault.status(ref), "live")

    def test_missing_cwd_is_refused_before_the_ref_is_spent(self):
        base = Path(tempfile.mkdtemp())
        tools, vault, _s, ref = build(project_dir=lambda: str(base))
        out = json.loads(run(tools.secret_exec(ref, "true", cwd="nope")))
        self.assertEqual(out["status"], "error")
        self.assertIn("not a directory", out["message"])
        self.assertEqual(vault.status(ref), "live")

    def test_no_project_context_refuses_any_explicit_cwd_and_keeps_the_ref(self):
        # Fix round 1 / I-2（R48，实测）：`project_dir()` 回 "" 时（不在工具调用上下文里，
        # `identity.py` 明写），原来 `if base:` 让 containment **变成可选的**，
        # `cwd="/tmp"` 与 `cwd="/etc"` 直接跑通 —— spec §7.2 的"必须在当前 workspace/project
        # 目录内，否则 error"没有例外从句，所以缺上下文时唯一站得住的是 **fail closed**：
        # 证明不了"在里面"，就不接受显式目录。出路写在消息里（省略 cwd）。
        for cwd in ("/tmp", "/etc", "/"):
            tools, vault, _s, ref = build(project_dir=lambda: "")
            out = json.loads(run(tools.secret_exec(ref, "true", cwd=cwd)))
            with self.subTest(cwd=cwd):
                self.assertEqual(out["status"], "error")
                self.assertIn("Omit cwd", out["message"])      # 出路要写在消息里，不是靠猜
                self.assertIn("not consumed", out["message"])
                self.assertEqual(vault.status(ref), "live")   # 挡住发生在消费之前

    def test_no_project_context_still_runs_when_cwd_is_omitted(self):
        # 反面对照：fail closed 关的是"模型自选目录"这一扇门，不是整个工具。没有显式
        # cwd 时模型没有任何目录自由度，子进程落在插件进程自己的 cwd（原来那条腿）。
        tools, vault, _s, ref = build(project_dir=lambda: "")
        out = json.loads(run(tools.secret_exec(ref, "printf ok")))
        self.assertEqual(out["status"], "ok", out)
        self.assertEqual(out["stdout"], "ok")
        self.assertEqual(vault.status(ref), "gone")

    def test_cwd_none_and_empty_string_are_the_same_thing(self):
        # spec §7.2 的形状是 `cwd: str | None = None`，实现要保住 None 与 "" 等价
        # （否则 Task 9 的 schema 生成器把 None 交进来就变成一个"非法值"）。
        for cwd in (None, "", "   "):
            tools, vault, _s, ref = build(project_dir=lambda: "")
            out = json.loads(run(tools.secret_exec(ref, "printf ok", cwd=cwd)))
            with self.subTest(cwd=repr(cwd)):
                self.assertEqual(out["status"], "ok", out)
                self.assertEqual(vault.status(ref), "gone")

    def test_resolve_failure_names_the_exception_type_instead_of_echoing_the_path(self):
        # Fix round 1 minor（R43）：`f"cannot resolve cwd: {exc}"` 把模型自选的**无限长**
        # 路径文本连同宿主 strerror 一起写进结果串；兄弟分支只回 `resolved.name`。
        # 这里代理 `Path.resolve` 抛一句带 300 个 A 的 OSError，断言结果里只有类名。
        original = tools_mod.Path

        class _StubPath:
            def __init__(self, *parts):
                self._inner = original(*parts)

            def resolve(self, *args, **kwargs):
                raise OSError("kernel cannot resolve " + ("A" * 300))

            def is_dir(self):
                return self._inner.is_dir()

            @property
            def name(self):
                return self._inner.name

            def __truediv__(self, other):
                return _StubPath(str(self._inner / other))

        tools_mod.Path = _StubPath
        self.addCleanup(setattr, tools_mod, "Path", original)
        base = Path(tempfile.mkdtemp())
        tools, vault, _s, ref = build(project_dir=lambda: str(base))
        out = json.loads(run(tools.secret_exec(ref, "true", cwd="sub")))
        self.assertEqual(out["status"], "error")
        self.assertIn("OSError", out["message"])
        self.assertNotIn("AAAA", out["message"])              # 路径实串不回显
        self.assertEqual(len(out["message"]), len("cannot resolve cwd: OSError"))
        self.assertEqual(vault.status(ref), "live")


class CancellationTest(unittest.TestCase):
    def test_cancel_event_set_before_the_call_leaves_the_ref_untouched(self):
        # 入口腿：本轮已经点了 Stop，就不该再花掉一次性 ref，更不该 fork。
        evt = asyncio.Event()
        evt.set()
        writes = []
        tools, vault, _s, ref = build(cancel=lambda: evt,
                                      env_writer=lambda k, v: writes.append(k))
        out = json.loads(run(tools.secret_exec(ref, "true")))
        self.assertEqual(out["status"], "error")
        self.assertIn("cancel", out["message"])
        self.assertIn("request_secret", out["message"])
        self.assertEqual(vault.status(ref), "live")

    def test_cancel_after_spawn_kills_the_child_and_says_the_ref_is_spent(self):
        stops = []
        evt = asyncio.Event()
        original = patch_spawn(lambda args, kwargs: _FakeChild(
            stops, mode="hang", cancel_event=evt, cancel_after_spawn=True))
        self.addCleanup(setattr, asyncio, "create_subprocess_exec", original)

        tools, vault, _s, ref = build(cancel=lambda: evt)
        out = json.loads(run(tools.secret_exec(ref, "true")))
        self.assertEqual(out["status"], "error")
        self.assertIn("killed", out["message"])
        self.assertIn("ref was consumed", out["message"])
        self.assertEqual(stops, ["kill", "wait"])         # 子进程不能留着继续跑
        self.assertEqual(vault.status(ref), "gone")       # 值可能已经进了子进程
        self.assertNotIn(DUMMY, json.dumps(out))

    def test_export_stops_on_a_cancelled_run_without_spending_the_ref(self):
        # 值进宿主 env 存储是**长期**驻留（§10.2 第 2 条）：一轮已经结束的调用不该留下它。
        writes = []
        evt = asyncio.Event()
        evt.set()
        tools, vault, _s, ref = build(env_writer=lambda k, v: writes.append(k),
                                      cancel=lambda: evt)
        out = json.loads(run(tools.secret_export(ref, "GITHUB_TOKEN")))
        self.assertEqual(out["status"], "error")
        self.assertIn("cancel", out["message"])
        self.assertEqual(writes, [])
        self.assertEqual(vault.status(ref), "live")

    def test_hard_cancel_is_raised_not_converted_and_reaps_the_child(self):
        # CancelledError 是 BaseException：最外层那个 `except Exception` 碰不到它。
        # 这一条钉的就是"取消不许变成结果串"（协调器 5 s 宽限期用完后走这里）。
        stops = []
        original = patch_spawn(lambda args, kwargs: _FakeChild(stops, mode="hard_cancel"))
        self.addCleanup(setattr, asyncio, "create_subprocess_exec", original)

        tools, vault, _s, ref = build()
        with self.assertRaises(asyncio.CancelledError):
            run(tools.secret_exec(ref, "true"))
        self.assertEqual(stops, ["kill", "wait"])
        self.assertEqual(vault.status(ref), "gone")

    def test_second_cancel_during_cleanup_still_kills_the_child(self):
        # Fix round 1 / I-3：原来的 `_stop_child` 是 `comm.cancel()` → `await wait({comm})`
        # → `proc.kill()`，也就是**杀进程排在一次 await 之后**。落到这条腿时上面已经没有别的
        # handler 会再接一遍：硬取消打在轮询那次 await 上 → `_communicate` 的 CancelledError 支
        # → `_stop_child` → 第二次取消打在 `_stop_child` 自己的 await 上 → 异常直接穿出
        # `_communicate`，`kill()` 永远到不了。而这一支的全部意义就是杀子进程 —— 留下的孤儿
        # 手里正拿着明文，argv 和 environ 都在它的地址空间里。
        # 这里让假子进程在两个时刻各取消一次工具任务，把那个窗口精确演出来（不靠 sleep 猜时序）。
        stops = []
        fired = []
        holder = {}

        def first_cancel():
            fired.append("first")            # 协调器的 force 路径
            holder["task"].cancel()

        def second_cancel():
            fired.append("second")          # 清理路径上的第二次取消
            holder["task"].cancel()

        child = _FakeChild(stops, mode="hang", cancel_on_start=first_cancel,
                           on_comm_cancel=second_cancel)
        original = patch_spawn(lambda args, kwargs: child)
        self.addCleanup(setattr, asyncio, "create_subprocess_exec", original)

        tools, vault, _s, ref = build()

        async def scenario():
            task = asyncio.create_task(tools.secret_exec(ref, "true"))
            holder["task"] = task
            with self.assertRaises(asyncio.CancelledError):
                await task
            return list(fired)

        seen = run(scenario())
        self.assertEqual(seen, ["first", "second"], seen)   # 两次取消确实都落了地
        # 杀进程发生在任何 await **之前**，所以第二次取消顶多打断收尾（`wait()` 收尸那一步），
        # 不可能留下一个还活着的子进程。
        self.assertIn("kill", stops, stops)
        self.assertEqual(vault.status(ref), "gone")
        self.assertNotIn(DUMMY, str(stops))


class ChildReapingTest(unittest.TestCase):
    """I-3 / I-4 的收尸面：`_communicate` 的每条出口都不许把子进程留在原地。"""

    def test_non_cancel_failure_in_the_reader_still_reaps_the_child(self):
        # Fix round 1 / I-4：`comm.result()`（或轮询里任何一层）抛**非取消**异常时，
        # 原来只有 `finally` 取消读者，没有任何一支去 kill：`secret_exec` 把它翻成 error
        # 结果串交回宿主，而子进程还活着、argv/environ 里还带着明文。
        stops = []
        original = patch_spawn(lambda args, kwargs: _FakeChild(stops, mode="boom"))
        self.addCleanup(setattr, asyncio, "create_subprocess_exec", original)

        tools, vault, _s, ref = build()
        with self.assertLogs("backend.tools", level="DEBUG") as logged:
            out = json.loads(run(tools.secret_exec(ref, "true")))
        self.assertEqual(out["status"], "error")
        self.assertEqual(stops, ["kill", "wait"], stops)     # kill 了，也回收了
        self.assertEqual(vault.status(ref), "gone")          # 值确实进过那个进程
        self.assertIn("consumed", out["message"])            # I-6：error 里要说清花没花
        line = render(logged.records)
        self.assertNotIn(DUMMY, line)
        self.assertIn("OSError", line)


class TimeoutTest(unittest.TestCase):
    def test_timeout_kills_child_and_reports_the_ref_as_consumed(self):
        # 超时腿唯一能确定性走到的形状：假子进程挂在 Event 上，真 deadline 算术判定。
        # `timeout_seconds` 被夹到最小 1 秒，所以这一条花一个 tick×5 ≈ 1 秒墙上时间，
        # 不花 `sleep 120` 那一整个进程的生命。
        stops = []
        original = patch_spawn(lambda args, kwargs: _FakeChild(stops, mode="hang"))
        self.addCleanup(setattr, asyncio, "create_subprocess_exec", original)

        tools, vault, _s, ref = make()
        out = json.loads(run(tools.secret_exec(ref, "printf slow", timeout_seconds=1)))
        self.assertEqual(out["status"], "error")
        self.assertIn("timed out after 1s", out["message"])
        self.assertIn("the ref was consumed", out["message"])
        self.assertEqual(stops, ["kill", "wait"])
        self.assertEqual(vault.status(ref), "gone")
        self.assertNotIn(DUMMY, json.dumps(out))


class EnvWriterWiringTest(unittest.TestCase):
    """裁定 R46：`tools.py` 永不 import 宿主，env 写入只能走装配注入的 `env_writer`。"""

    def test_missing_env_writer_is_reported_and_the_ref_is_given_back(self):
        tools, vault, _s, ref = make()               # env_writer=None：装配缺陷
        with self.assertLogs("backend.tools", level="DEBUG") as logged:
            out = json.loads(run(tools.secret_export(ref, "GITHUB_TOKEN")))
        self.assertEqual(out["status"], "error")
        self.assertIn("not consumed", out["message"])
        self.assertEqual(vault.status(ref), "live")   # reopen 走的就是这一支
        line = render(logged.records)
        self.assertNotIn(DUMMY, line)
        self.assertIn("RuntimeError", line)
        # 现场身份要能区分"没接线"与"接了但写失败"：帧落在 _write_env 才是装配缺陷。
        self.assertRegex(line, r"site=\S*backend/tools\.py:\d+ in _write_env")
        self.assertIn("tool=secret_export", line)

    def test_reopen_hands_back_a_spendable_ref_not_just_a_live_status(self):
        # 计划那条 `status()=="live"` 只证明了状态位；这一条证明 ref **还能再兑一次**，
        # 也就是 reopen 之后消费路径真的通（没有 reopen，第二次就是 ref_unavailable）。
        calls = []

        def flaky(key, value):
            calls.append(key)
            if len(calls) == 1:
                raise OSError("env store is read-only")
            return {key: value}

        tools, vault, _s, ref = make(env_writer=flaky)
        first = json.loads(run(tools.secret_export(ref, "GITHUB_TOKEN")))
        self.assertEqual(first["status"], "error")
        self.assertIn("not consumed", first["message"])
        second = json.loads(run(tools.secret_export(ref, "GITHUB_TOKEN")))
        self.assertEqual(second["status"], "ok", second)
        self.assertEqual(vault.status(ref), "gone")
        self.assertEqual(calls, ["GITHUB_TOKEN", "GITHUB_TOKEN"])


class EnvKeyDenylistTest(unittest.TestCase):
    """Fix round 1 / I-5（R49 + 新的 R50）：`env_key` 的正则放过宿主自己赖以运行的名字。

    `PATH`/`HOME`/`IFS`/`LD_PRELOAD`/`NODE_OPTIONS` 全都匹配 `^[A-Z_][A-Z0-9_]{0,63}$`，
    而按 spec §5，`set_env_var` **既**落盘 `envs.json`（持久）**又**同步宿主进程 environ。
    模型自选一个这样的名字 = 一次可持久化的宿主自我 DoS。裁定：拒绝表只加在
    `secret_export`；`secret_exec` 的 `env_name` 不需要表 —— 那个覆写只活在这一个孩子进程
    的 environ 里，孩子死了就没了，而且 loader 打值那条日志早被擦洗过。
    """

    def test_host_critical_env_keys_are_refused_and_the_ref_survives(self):
        writes = []
        tools, vault, _s, ref = make(env_writer=lambda k, v: writes.append(k))
        for name in sorted(tools_mod._HOST_ENV_DENYLIST):
            with self.subTest(env_key=name):
                out = json.loads(run(tools.secret_export(ref, name)))
                self.assertEqual(out["status"], "error", name)
                self.assertIn("host-critical", out["message"])
                self.assertIn("not consumed", out["message"])
                self.assertEqual(vault.status(ref), "live")   # 拒绝发生在消费之前
        self.assertEqual(writes, [])                          # 一次都没落到宿主存储

        # 反向对照：表不能宽到把正当用法一起挡了（否则 I-5 的修法就是新功能缺陷）。
        for name in ("GITHUB_TOKEN", "MY_TOKEN", "AWS_SECRET_ACCESS_KEY", "QODER_CI_KEY",
                     "_LEADING_UNDERSCORE", "TOKEN2"):
            tools, vault, _s, ref = make(env_writer=lambda k, v: {k: v})
            with self.subTest(allowed=name):
                out = json.loads(run(tools.secret_export(ref, name)))
                self.assertEqual(out["status"], "ok", out)
                self.assertEqual(vault.status(ref), "gone")

    def test_the_denylist_is_export_only_and_is_a_frozenset(self):
        # `secret_exec` 的 `env_name` 走的是另一条纪律：孩子级作用域，死了就没了。
        self.assertIsInstance(tools_mod._HOST_ENV_DENYLIST, frozenset)
        self.assertTrue({"PATH", "HOME", "IFS", "LD_PRELOAD", "NODE_OPTIONS"}
                        <= set(tools_mod._HOST_ENV_DENYLIST))
        tools, vault, _s, ref = make()
        # 阳性对照：`env_name="PATH"` 真的把孩子的 PATH 覆写成了那个值 —— 子进程打印它拿到的
        # 是擦洗后的 `***`。两件事同时成立："exec 不挡这个名字"（覆写确实发生了）而且
        # "exec 不需要挡"（覆写只活在这一个孩子里，值又 Never 出结果串）。
        out = json.loads(run(tools.secret_exec(ref, 'printf "%s" "$PATH"', env_name="PATH")))
        self.assertEqual(out["status"], "ok", out)
        self.assertEqual(out["stdout"], REDACT)
        self.assertIn("$PATH", out["note"])
        self.assertEqual(vault.status(ref), "gone")


class SpentReportingTest(unittest.TestCase):
    """Fix round 1 / I-6：兜底 error 支必须说清 ref 花没花。

    消费前的路都写了"The ref was not consumed"，超时/取消两支写了"已消费"，唯独
    `secret_exec`/`secret_export` 的兜底支两句都不写 —— E2BIG、`OSError`、transport 故障
    就把模型推进一次注定 `ref_unavailable` 的重试（或者反过来，让它以为还能重试一个已经
    花掉的 ref）。
    """

    def test_exec_error_after_the_consume_says_the_ref_is_spent(self):
        # 演 E2BIG/ARG_MAX 那一档：spawn 自己抛，此时 ref 已经消费、值已经进过 argv。
        def too_long(*args, **kwargs):
            raise OSError(7, "Argument list too long")

        original = asyncio.create_subprocess_exec
        self.addCleanup(setattr, asyncio, "create_subprocess_exec", original)
        asyncio.create_subprocess_exec = too_long

        tools, vault, _s, ref = make()
        with self.assertLogs("backend.tools", level="DEBUG"):
            out = json.loads(run(tools.secret_exec(ref, "true")))
        self.assertEqual(out["status"], "error")
        self.assertIn("already consumed", out["message"])
        self.assertIn("request_secret", out["message"])       # 告诉他下一步怎么重来
        self.assertNotIn("not consumed", out["message"])
        self.assertEqual(vault.status(ref), "gone")
        self.assertNotIn(DUMMY, json.dumps(out))

    def test_exec_error_before_the_consume_says_the_ref_survives(self):
        # 消费之前的失败（这里是 `int(max_output_bytes)` 被模型传的垃圾值炸掉）：ref 还在，
        # 消息就必须说"没消费、可以重试"，否则模型白重问一次人类。
        tools, vault, _s, ref = make()
        with self.assertLogs("backend.tools", level="DEBUG"):
            out = json.loads(run(tools.secret_exec(ref, "true", max_output_bytes="not-int")))
        self.assertEqual(out["status"], "error")
        self.assertIn("not consumed", out["message"])
        self.assertNotIn("already consumed", out["message"])
        self.assertEqual(vault.status(ref), "live")
        # 同一个 ref 随后确实还能用（消息说的和状态机做的是同一件事）。
        again = json.loads(run(tools.secret_exec(ref, "printf ok")))
        self.assertEqual(again["status"], "ok", again)

    def test_export_error_after_the_consume_says_the_ref_is_spent(self):
        tools, vault, _s, ref = make(env_writer=lambda k, v: {k: v})

        def eat(_value):
            raise RuntimeError("scrubber exploded")

        tools._scrubber.register = eat
        with self.assertLogs("backend.tools", level="DEBUG"):
            out = json.loads(run(tools.secret_export(ref, "GITHUB_TOKEN")))
        self.assertEqual(out["status"], "error")
        self.assertIn("already consumed", out["message"])
        self.assertEqual(vault.status(ref), "gone")
        self.assertNotIn(DUMMY, json.dumps(out))

    def test_export_error_before_the_consume_says_the_ref_survives(self):
        def bad_cancel():
            raise RuntimeError("cancel seam is not wired")

        tools, vault, _s, ref = make(env_writer=lambda k, v: {k: v})
        tools._cancel_event = bad_cancel
        with self.assertLogs("backend.tools", level="DEBUG"):
            out = json.loads(run(tools.secret_export(ref, "GITHUB_TOKEN")))
        self.assertEqual(out["status"], "error")
        self.assertIn("not consumed", out["message"])
        self.assertEqual(vault.status(ref), "live")


class VaultReopenTest(unittest.TestCase):
    """Fix round 1 的 vault 侧：`reopen` 的两句过度承诺。

    1. docstring 说 `_PREFIX` 检查能挡住"伪造键在一次性存储里铸出一个可兑付条目"——它挡不住
       （任何 `sr_*` 都能过），而且和 `issue` 不同，它绕过了 `max_entries` 淘汰。
    2. 它把时间戳重铸成 now，于是**每一次失败的导出都给这份明文重新续一整段 TTL**；
       反复失败 = 明文寿命无上限。修法是保住原始签发时刻（消费时留一个有界的 tombstone），
       重试不再把到期时刻往后推。
    """

    def test_two_consecutive_failed_exports_do_not_extend_the_ttl_window(self):
        def fail(_key, _value):
            raise OSError("env store is read-only")

        tools, vault, _s, ref = make(env_writer=fail)
        issued_at = time.monotonic() - (vault._ttl - 5)      # 只剩 5 秒窗口
        vault._issued[ref] = (DUMMY, issued_at)
        for attempt in (1, 2):
            with self.subTest(attempt=attempt):
                out = json.loads(run(tools.secret_export(ref, "GITHUB_TOKEN")))
                self.assertEqual(out["status"], "error", out)
                self.assertEqual(vault.status(ref), "live")
                # 时间戳一个字节都没动 ⇒ 到期时刻也没动。
                self.assertEqual(vault._issued[ref][1], issued_at)
        self.assertLess(time.monotonic() - vault._issued[ref][1], vault._ttl)

    def test_reopen_cannot_grow_the_store_past_max_entries(self):
        # 两腿各量一种生长方式：同一键反复还回去（不会生长，量的是"不许越界"这条不变式），
        # 和每轮换一个新 `sr_*` 键（正是 `_PREFIX` 检查挡不住的那种调用 ⇒ 旧实现里绕过 `issue`
        # 的淘汰，可以无界生长）。第二腿才是这条门的动力来源。
        for forged in (False, True):
            with self.subTest(round_robin="每轮换一个新键" if forged else "同一个键反复还回"):
                vault = Vault(ttl_seconds=300, max_entries=4)
                refs = [vault.issue(f"value-{i}") for i in range(4)]
                vault.consume(refs[-1])
                for round_no in range(8):
                    key = f"sr_forged_{round_no}" if forged else refs[-1]
                    if not forged:
                        vault.consume(refs[-1])
                    vault.reopen(key, f"value-{round_no}")
                    self.assertLessEqual(len(vault._issued), 4)
                self.assertLessEqual(len(vault._spent_at), 4)   # tombstone 同样有界
                self.assertEqual(vault.status(refs[-1]), "gone" if forged else "live")

    def test_reopen_of_a_ref_this_vault_never_issued_is_a_no_op_for_the_clock(self):
        # 没有 tombstone（不是我们消费过的那一个）时只能按 now 记账 —— 这条把"什么时候
        # 用 now、什么时候保住原时刻"的分界钉住，免得下一个人把 tombstone 当成可选优化。
        vault = Vault(ttl_seconds=300)
        before = time.monotonic()
        vault.reopen("sr_never_issued_here", "whatever")
        self.assertEqual(vault.status("sr_never_issued_here"), "live")
        self.assertGreaterEqual(vault._issued["sr_never_issued_here"][1], before)


class EnvMessageHygieneTest(unittest.TestCase):
    """兜底 error 之外，回显给模型的**模型自选文本**也要有界（同 R43 的量纲）。

    `env_key`/`env_name` 的错误消息把收到的名字 `repr` 回去。名字超过 64 字符时正则先挡，
    但那条消息本身仍然会把整串抄回去，于是"结果串永远小于剪枝阈值"这句又多一个例外。
    """

    def test_oversized_echoed_names_are_truncated_in_the_message(self):
        # 回显仍然在（模型要知道自己写错了什么），但**长度由我们兜**：整条消息 ≤ 400，
        # 抄回去的名字 ≤ `_MAX_MESSAGE_CHARS`。
        long_name = "A" * 5000
        cap = tools_mod._MAX_MESSAGE_CHARS
        tools, vault, _s, ref = make(env_writer=lambda k, v: {k: v})
        out = json.loads(run(tools.secret_export(ref, long_name)))
        self.assertEqual(out["status"], "error")
        self.assertLess(len(out["message"]), 400, len(out["message"]))
        self.assertLess(out["message"].count("A"), cap + 10)
        self.assertIn("env_key must match", out["message"])
        self.assertIn("not consumed", out["message"])
        self.assertEqual(vault.status(ref), "live")

        tools, vault, _s, ref = make()
        out = json.loads(run(tools.secret_exec(ref, "true", env_name=long_name)))
        self.assertEqual(out["status"], "error")
        self.assertLess(len(out["message"]), 400, len(out["message"]))
        self.assertLess(out["message"].count("A"), cap + 10)
        self.assertIn("not consumed", out["message"])
        self.assertEqual(vault.status(ref), "live")


class LogHygieneTest(unittest.TestCase):
    def test_exec_failure_log_carries_type_and_site_only(self):
        # spec §10.1「宿主 INFO/DEBUG 日志」行：失败日志只准有类名 + 帧坐标。
        # 这里把明文放进异常报文，正是 `logger.exception` 会打进 traceback 段的那种消息
        # （Task 5 的评审就是这么把 logger.exception 换掉的，R28/R34）。
        tools, _v, _s, ref = make()

        def boom(_r):
            raise RuntimeError(f"vault ate {DUMMY}")

        tools._vault.consume = boom
        with self.assertLogs("backend.tools", level="DEBUG") as logged:
            out = json.loads(run(tools.secret_exec(ref, "true")))
        self.assertEqual(out["status"], "error")
        line = render(logged.records)
        self.assertNotIn(DUMMY, line)
        self.assertNotIn("vault ate", line)
        self.assertIn("RuntimeError", line)
        self.assertIn("tool=secret_exec", line)
        self.assertRegex(line, r"site=\S*test_tools_consume\.py:\d+ in boom")
        # 阳性对照：上面那句 notIn 看的通道确实会带上异常原文，不是空门。
        try:
            raise RuntimeError(DUMMY)
        except RuntimeError as control:
            probe = logging.LogRecord("backend.tools", logging.ERROR,
                                      __file__, 1, "probe", None, None)
            probe.exc_info = (type(control), control, control.__traceback__)
            self.assertIn(DUMMY, render([probe]))

    def test_export_env_write_failure_log_carries_type_and_site_only(self):
        tools, vault, _s, ref = make(env_writer=lambda k, v: (_ for _ in ()).throw(
            ValueError(f"env store rejected {DUMMY}")))
        with self.assertLogs("backend.tools", level="DEBUG") as logged:
            out = json.loads(run(tools.secret_export(ref, "GITHUB_TOKEN")))
        self.assertEqual(out["status"], "error")
        line = render(logged.records)
        self.assertNotIn(DUMMY, line)
        self.assertIn("ValueError", line)
        self.assertIn("tool=secret_export", line)
        self.assertEqual(vault.status(ref), "live")


class StatusAlphabetTest(unittest.TestCase):
    """闭集：`ok|ref_unavailable|ref_expired|error`（spec §7.2/§7.3）。"""

    def test_every_reachable_status_is_in_the_closed_set(self):
        allowed = {"ok", "ref_unavailable", "ref_expired", "error"}
        seen = []

        tools, _v, _s, ref = make()
        seen.append(json.loads(run(tools.secret_exec(ref, "true")))["status"])
        seen.append(json.loads(run(tools.secret_exec(ref, "true")))["status"])

        tools, vault, _s, ref = make()
        vault._issued[ref] = (DUMMY, 0.0)
        vault.sweep()
        seen.append(json.loads(run(tools.secret_exec(ref, "true")))["status"])

        tools, _v, _s, ref = make()
        seen.append(json.loads(run(tools.secret_exec(ref, "  ")))["status"])
        seen.append(json.loads(run(tools.secret_exec(ref, "true", cwd="/")))["status"])

        tools, _v, _s, ref = make(env_writer=lambda k, v: {})
        seen.append(json.loads(run(tools.secret_export(ref, "GITHUB_TOKEN")))["status"])
        seen.append(json.loads(run(tools.secret_export(ref, "bad key")))["status"])

        self.assertEqual(set(seen) - allowed, set(), seen)
        self.assertEqual(set(seen), allowed, seen)

    def test_expired_and_unavailable_are_distinct_live_gates(self):
        # 验证项 (b)：两条腿各红各的。把 `_live()` 的过期支折进 unavailable，这条就红；
        # 反过来把 unavailable 说成 expired 也一样。hint 两句都得点名 request_secret。
        #
        # "过期"有**两条**到达路径，两条都要在消费点演一遍：`sweep()` 之后走 `_expired` 查表
        # 那一支（vault.py:51），没人来 sweep 时走 `_live()` 里现场判定并回收那一支（:56）。
        # 只演第一条的话，把第二支折进 unavailable 照样全绿 —— Task 2 的评审在 vault 层
        # 钉过现场那一支，消费点这一层原来是空的（本轮自审补上）。
        for lazy in (False, True):
            with self.subTest(expired_via="现场回收" if lazy else "sweep 查表"):
                tools, vault, _s, ref = make()
                vault._issued[ref] = (DUMMY, 0.0)
                if not lazy:
                    vault.sweep()
                self.assertEqual(vault.status(ref), "expired")
                expired = json.loads(run(tools.secret_exec(ref, "true")))
                self.assertEqual(expired["status"], "ref_expired")
                self.assertIn("request_secret", expired["hint"])
                self.assertNotIn(DUMMY, json.dumps(expired))

        tools, _v, _s, ref = make()
        run(tools.secret_exec(ref, "true"))
        self.assertEqual(vault.status(ref), "gone")
        gone = json.loads(run(tools.secret_exec(ref, "true")))
        self.assertEqual(gone["status"], "ref_unavailable")
        self.assertNotEqual(gone["status"], "ref_expired")


def reachable_names(source: str) -> set[str]:
    """一段源码里"够得着"的名字：被调用的属性名/函数名，加上 import 进来的根与别名。

    与 `test_secret_store_gate.py` 的 AST 走法同一族，但这里要抓的是**调用名**
    （`asyncio.to_thread(...)` 的 `to_thread`），不是导入路径，所以不必解析别名链。
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            callee = node.func
            if isinstance(callee, ast.Attribute):
                names.add(callee.attr)
            elif isinstance(callee, ast.Name):
                names.add(callee.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
                names.add(alias.asname or alias.name.split(".")[0])
    return names


class SingleLoopGateTest(unittest.TestCase):
    """裁定 R14 的门：`vault.consume`/`reopen` 留在事件循环上。

    `consume` 的"读+删"之所以是一次性的，唯一理由是它里面没有 await；把它挪进
    `to_thread`/线程池/第二个循环，就是让两个调用同时进同一个 dict，然后同一个明文能兑
    两次（I3 直接失效）。这条承诺原来只有 `vault.py` 的注释在守，消费点这边一个读数都没有
    —— Task 8 正是"最想 await 的一层"（它在等子进程），所以门加在这里。
    """

    OFF_LOOP = ("to_thread", "run_in_executor", "Thread", "run_coroutine_threadsafe",
                "new_event_loop", "ThreadPoolExecutor")

    def test_tools_module_never_moves_vault_work_off_the_loop(self):
        source = (Path(__file__).resolve().parents[1] / "backend" / "tools.py").read_text(
            encoding="utf-8")
        offenders = sorted(reachable_names(source) & set(self.OFF_LOOP))
        self.assertEqual(offenders, [], f"R14 破了：off-loop 调用 {offenders}")

    def test_the_gate_actually_fires_on_the_shapes_it_names(self):
        # 阳性对照：门不是永远绿的字符串比较。
        for src in ("import asyncio\nasync def f():\n    await asyncio.to_thread(v.consume, r)\n",
                    "from concurrent.futures import ThreadPoolExecutor\nx = 1\n",
                    "import threading\nthreading.Thread(target=t).start()\n"):
            with self.subTest(src=src.splitlines()[0]):
                self.assertTrue(reachable_names(src) & set(self.OFF_LOOP), src)


if __name__ == "__main__":
    unittest.main()
