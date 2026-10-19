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
        self.assertLessEqual(len(out["stdout"].encode()), 1024 + len(REDACT) + 64)
        self.assertIn("[truncated by secret-input]", out["stdout"])

    def test_cwd_outside_project_dir_is_refused_and_ref_not_consumed(self):
        tools, vault, _s, ref = make()
        out = json.loads(run(tools.secret_exec(ref, "true", cwd="/")))
        self.assertEqual(out["status"], "error")
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

    `communicate` 的三种命运由 mode 决定，全部由**用例**控制，不碰墙上时钟：
      hang       —— 挂在永不 set 的 Event 上（取消腿/超时腿用）
      hard_cancel —— 自己抛 CancelledError（宿主硬取消腿用）
    `cancel_after_spawn` 让子进程一起就把 cancel_event 按下去（轮询取消腿用）。
    """

    def __init__(self, stops, mode="hang", cancel_event=None, cancel_after_spawn=False):
        self.stops = stops
        self.returncode = None
        self._mode = mode
        self._released = asyncio.Event()
        self._cancel_event = cancel_event
        self._cancel_after_spawn = cancel_after_spawn

    async def communicate(self):
        if self._cancel_after_spawn and self._cancel_event is not None:
            self._cancel_event.set()
        if self._mode == "hard_cancel":
            raise asyncio.CancelledError
        await self._released.wait()
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
        # 封顶是安全控制不是排版：必须严格低于宿主剪枝阈值（spec §5 recent=50000B），
        # 否则 §10.1 路径⑥（结果整体落盘 `tool_results/*.txt`、截断桩里还写着那个路径）
        # 就会被踩上。把这两个数改掉的那次提交必须在这里红，而不是在 Task 12 的现场。
        self.assertLess(tools_mod._MAX_OUTPUT_BYTES, 50000)
        sig = inspect.signature(SecretTools.secret_exec)
        self.assertEqual(sig.parameters["env_name"].default, "SECRET")
        self.assertEqual(sig.parameters["max_output_bytes"].default, 32768)
        self.assertEqual(sig.parameters["timeout_seconds"].default, 120)

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
        self.assertLessEqual(len(out["stdout"].encode()), 32768 + len(REDACT) + 64)
        self.assertIn("[truncated by secret-input]", out["stdout"])

    def test_request_below_the_floor_is_clamped_up(self):
        tools, _v, _s, ref = make()
        out = json.loads(run(tools.secret_exec(
            ref, "seq 1 20000 | tr '\\n' ' '; echo", max_output_bytes=1)))
        self.assertLessEqual(len(out["stdout"].encode()), 1024 + len(REDACT) + 64)
        self.assertGreaterEqual(len(out["stdout"].encode()), 1024)

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
        tools, _v, _s, ref = make()
        huge = json.loads(run(tools.secret_exec(
            ref, "seq 1 20000 | tr '\\n' ' '; echo", max_output_bytes=10 ** 9)))
        self.assertLessEqual(len(huge["stdout"].encode()), tools_mod._MAX_OUTPUT_BYTES + 67)


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
