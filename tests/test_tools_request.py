import asyncio
import io
import json
import unittest

from backend.config import PluginConfig
from backend.records import RequestRegistry
from backend.scrub import Scrubber
from backend.tools import SecretTools
from backend.vault import Vault
from tests.support import DUMMY

ID = {"session_id": "s1", "agent_id": "a1", "user_id": "u1", "channel": "console"}


def make(cancel=None):
    # 本模块的规矩（评审 I-4）：每一次工具调用都显式传 timeout_seconds=30，唯一例外是那条
    # 专门测越界值的 5。`_await` 有自己的截止，分支回归时用例必须**快速变红**，而不是挂到
    # 配置默认的 600 秒上。注册表构造处的 600 只是"调用方不传时才生效"的默认；request_secret
    # 省略参数时取 config.answer_timeout_seconds（R44），这两条语义由 AnswerTimeoutPrecedenceTest
    # 钉住。
    vault = Vault(ttl_seconds=300)
    registry = RequestRegistry(vault, timeout_seconds=600, max_pending=8)
    scrubber = Scrubber()
    tools = SecretTools(registry=registry, vault=vault, scrubber=scrubber,
                        config=PluginConfig(), identity=lambda: dict(ID),
                        cancel_event=cancel or (lambda: None))
    return tools, registry, scrubber, vault


def build(identity_map, cancel=None, registry=None, vault=None, scrubber=None):
    """`make()` 的另一形状：会话身份可换，其余默认。边界用例要用它跑两个会话/两个 agent。"""
    v = vault or Vault(ttl_seconds=300)
    reg = registry or RequestRegistry(v, timeout_seconds=600, max_pending=8)
    scr = scrubber or Scrubber()
    tools = SecretTools(registry=reg, vault=v, scrubber=scr, config=PluginConfig(),
                        identity=lambda: dict(identity_map),
                        cancel_event=cancel or (lambda: None))
    return tools, reg, scr, v


async def answer_pending(registry, session_id, value=DUMMY):
    """把那张待答卡答掉，返回记录本身（测试侧的"人类"，不是产品代码路径）。"""
    rec = registry.list_pending(session_id)[0]
    await registry.answer(rec.request_id, nonce=rec.nonce, value=value)
    return rec


# 六个状态面各自的键集（结果形状门用它；放在模块级而不是类属性，见 ruff RUF012）。
CLOSED_KEYSETS = {
    "saved": {"status", "ref", "ref_expires_in", "hint"},
    "declined": {"status", "hint"},
    "timed_out": {"status", "hint"},
    "cancelled": {"status", "reason"},
    "ref_unavailable": {"status", "hint"},
    "error": {"status", "message"},
}

# spec 7.1 的返回表里，"下一步做什么"必须点名工具（评审 M-2）：模型会照字面去调用，
# 文案里的工具名就是接口。只断言 `client_request_id` 出现在 hint 里，把
# `request_secret` 从 `_HINT_TIMED_OUT` 删掉照样绿 —— 那是一条会静默漂走的合同。
HINT_TOOL_NAMES = {
    "saved": ("secret_exec", "secret_export"),
    "timed_out": ("request_secret", "client_request_id"),
    "ref_unavailable": ("request_secret", "client_request_id"),
}
# 反面：declined 的文案刻意**不许**点名任何工具 —— spec 要的是"别再问同一样东西"，
# 给它一条重问路径就是把这句禁令抵消掉。（cancelled 与 error 没有 hint 键，由形状门管。）
HINT_TOOL_FREE = {"declined": ("request_secret", "secret_exec", "secret_export")}


class RequestSecretTest(unittest.TestCase):
    def test_saved_result_has_ref_and_no_value(self):
        tools, registry, _scr, _v = make()

        async def scenario():
            task = asyncio.create_task(
                tools.request_secret("GitHub token", "signing", timeout_seconds=30))
            await asyncio.sleep(0)
            rec = registry.list_pending("s1")[0]
            await registry.answer(rec.request_id, nonce=rec.nonce, value=DUMMY)
            return json.loads(await task)

        out = asyncio.run(scenario())
        self.assertEqual(out["status"], "saved")
        self.assertTrue(out["ref"].startswith("sr_"))
        self.assertNotIn(DUMMY, json.dumps(out))

    def test_declined_and_timed_out_shapes(self):
        tools, registry, _scr, _v = make()

        async def scenario():
            task = asyncio.create_task(tools.request_secret("l", "r", timeout_seconds=30))
            await asyncio.sleep(0)
            rec = registry.list_pending("s1")[0]
            await registry.answer(rec.request_id, nonce=rec.nonce, decline=True)
            return json.loads(await task)

        self.assertEqual(asyncio.run(scenario())["status"], "declined")

        tools, registry, _scr, _v = make()

        async def expire():
            task = asyncio.create_task(tools.request_secret("l", "r", timeout_seconds=30))
            await asyncio.sleep(0)
            rec = registry.list_pending("s1")[0]
            rec.expires_at = 0.0
            registry.sweep()
            return json.loads(await task)

        out = asyncio.run(expire())
        self.assertEqual(out["status"], "timed_out")
        # M-2：这句文案的两条约束都要钉 —— 用什么工具重问（request_secret）、用什么键重问
        # （NEW client_request_id）。原来只钉了后半句，删掉工具名也不会红。
        self.assertIn("request_secret", out["hint"])
        self.assertIn("client_request_id", out["hint"])

    def test_cancel_event_short_circuits_to_cancelled(self):
        tools, registry, _scr, _v = make(cancel=lambda: self.evt)
        self.evt = asyncio.Event()

        async def scenario():
            task = asyncio.create_task(tools.request_secret("l", "r", timeout_seconds=30))
            await asyncio.sleep(0)
            self.evt.set()
            return json.loads(await task)

        out = asyncio.run(scenario())
        self.assertEqual(out["status"], "cancelled")
        self.assertEqual(out["reason"], "run_ended")

    def test_capacity_supersession_surfaces_cancelled_superseded(self):
        # 评审 M-7：spec 7.1 那一行写的是"本轮结束/被取代"，但 tools 层此前只演过前一半。
        # records 的容量逐出（`_enforce_capacity` → `_cancel_now(oldest, "superseded")`）
        # 在注册表级别有门，这里补上上层那半条路：被取代的那次调用必须把
        # {"status":"cancelled","reason":"superseded"} 交回模型，而不是含糊成 run_ended
        # 或 error —— 这两个词在模型侧的下一步动作完全不同（重问 vs 等本轮结束）。
        vault = Vault(ttl_seconds=300)
        registry = RequestRegistry(vault, timeout_seconds=600, max_pending=1)
        tools, _r, _s, _v = build(ID, registry=registry, vault=vault)

        async def scenario():
            first = asyncio.create_task(tools.request_secret("l1", "r1", timeout_seconds=30))
            await asyncio.sleep(0)
            second = asyncio.create_task(tools.request_secret("l2", "r2", timeout_seconds=30))
            await asyncio.sleep(0)
            a = json.loads(await first)          # 第二张卡把第一张逐出，第一个调用就结了
            rec = await answer_pending(registry, "s1")
            b = json.loads(await second)
            return a, b, rec

        a, b, rec = asyncio.run(scenario())
        self.assertEqual(a, {"status": "cancelled", "reason": "superseded"})
        self.assertEqual(set(a), CLOSED_KEYSETS["cancelled"])
        self.assertEqual(b["status"], "saved")
        self.assertEqual(b["ref"], rec.ref)                   # 活下来并被答掉的正是第二张
        self.assertEqual(registry.list_pending("s1"), [])

    def test_bad_label_returns_error_string_not_raise(self):
        tools, _r, _s, _v = make()
        out = json.loads(asyncio.run(tools.request_secret("", "  ")))
        self.assertEqual(out["status"], "error")

    def test_out_of_range_timeout_is_reported_not_raised(self):
        tools, _r, _s, _v = make()
        out = json.loads(asyncio.run(tools.request_secret("l", "r", timeout_seconds=5)))
        self.assertEqual(out["status"], "error")
        self.assertIn("30..3600", out["message"])

    def test_non_numeric_timeout_is_a_local_validation_error(self):
        # 不是越界，而是 int() 本身就失败：这一支必须在 create 之前本地拦下，
        # 只报参数名与类型名（不回显模型写的值），并且不留下一张没人答得掉的卡。
        tools, registry, _s, _v = make()
        out = json.loads(asyncio.run(tools.request_secret("l", "r", timeout_seconds="soon")))
        self.assertEqual(out, {"status": "error",
                               "message": "timeout_seconds must be an integer; got str."})
        self.assertNotIn("soon", out["message"])
        self.assertEqual(registry.list_pending("s1"), [])

    def test_client_request_id_replay_gives_the_same_ref(self):
        tools, registry, _scr, vault = make()

        async def scenario():
            first = asyncio.create_task(
                tools.request_secret("l", "r", client_request_id="c1", timeout_seconds=30))
            await asyncio.sleep(0)
            rec = registry.list_pending("s1")[0]
            await registry.answer(rec.request_id, nonce=rec.nonce, value=DUMMY)
            a = json.loads(await first)
            # 重放这一支也要夹 30 秒：幂等查表一旦断掉（对照 C4a），第二次调用就不是
            # "取回同一条记录"而是"再发一张新卡并等人"，没这个参数它就挂到 600 秒。
            b = json.loads(await tools.request_secret("l", "r", client_request_id="c1",
                                                      timeout_seconds=30))
            return a, b, rec

        a, b, rec = asyncio.run(scenario())
        self.assertEqual(a["ref"], rec.ref)
        self.assertEqual(b["ref"], rec.ref)          # 重放可恢复丢失的结果

    def test_replay_after_consumption_reports_unavailable(self):
        tools, registry, _scr, vault = make()

        async def scenario():
            first = asyncio.create_task(
                tools.request_secret("l", "r", client_request_id="c9", timeout_seconds=30))
            await asyncio.sleep(0)
            rec = registry.list_pending("s1")[0]
            await registry.answer(rec.request_id, nonce=rec.nonce, value=DUMMY)
            await first
            vault.consume(rec.ref)
            return json.loads(await tools.request_secret("l", "r", client_request_id="c9",
                                                         timeout_seconds=30))

        out = asyncio.run(scenario())
        self.assertEqual(out["status"], "ref_unavailable")
        self.assertIn("request_secret", out["hint"])

    def test_answer_page_url_is_logged_for_the_headless_path(self):
        # spec 8.3 第 2 层：TUI/ACP/API 驱动时，日志里那条 URL 是唯一入口
        import logging

        tools, registry, _scr, _v = make()
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        root = logging.getLogger()
        # 单测进程的 root 是 WARNING，插件那条是 INFO：不降档就一个字也捕不到（假红）。
        # 宿主进程里 root 由宿主自己提到 INFO，这一句只是把单测摆成宿主的形状。
        previous = root.level
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        try:
            async def scenario():
                task = asyncio.create_task(tools.request_secret("l", "r", timeout_seconds=30))
                await asyncio.sleep(0)
                rec = registry.list_pending("s1")[0]
                await registry.answer(rec.request_id, nonce=rec.nonce, value=DUMMY)
                await task
                return rec

            rec = asyncio.run(scenario())
        finally:
            root.removeHandler(handler)
            root.setLevel(previous)
        logged = buf.getvalue()
        self.assertIn(rec.request_id, logged)
        self.assertIn("/api/secret-input/card/", logged)
        self.assertNotIn(DUMMY, logged)

    def test_no_url_or_peer_in_the_tool_result(self):
        tools, registry, _scr, _v = make()

        async def scenario():
            task = asyncio.create_task(tools.request_secret("l", "r", timeout_seconds=30))
            await asyncio.sleep(0)
            rec = registry.list_pending("s1")[0]
            await registry.answer(rec.request_id, nonce=rec.nonce, value=DUMMY)
            return await task

        text = asyncio.run(scenario())
        for needle in ("http://", "https://", "127.0.0.1", "/api/"):
            self.assertNotIn(needle, text)


class IdentityBoundaryTest(unittest.TestCase):
    """R15（幂等按会话+agent 分键）、R41（空会话）、R42（sweep 措辞）、R43（异常文本）、
    硬取消的终态、以及 `peek` 的非消费性 —— 这几条都在 brief 的返回表之外，但都是
    Task 8/9 与真机 A6 要踩的边界。"""

    def test_same_client_request_id_in_two_sessions_gets_two_cards(self):
        # 裁定 R15 的会话那一半：cid 是模型自选文本，没有命名空间。
        other = {"session_id": "s2", "agent_id": "a1", "user_id": "u2", "channel": "console"}
        vault = Vault(ttl_seconds=300)
        registry = RequestRegistry(vault, timeout_seconds=600, max_pending=8)
        tools_a = SecretTools(registry=registry, vault=vault, scrubber=Scrubber(),
                              config=PluginConfig(), identity=lambda: dict(ID),
                              cancel_event=lambda: None)
        tools_b = SecretTools(registry=registry, vault=vault, scrubber=Scrubber(),
                              config=PluginConfig(), identity=lambda: dict(other),
                              cancel_event=lambda: None)

        async def scenario():
            ta = asyncio.create_task(
                tools_a.request_secret("l", "r", client_request_id="retry-1", timeout_seconds=30))
            await asyncio.sleep(0)
            tb = asyncio.create_task(
                tools_b.request_secret("l", "r", client_request_id="retry-1", timeout_seconds=30))
            await asyncio.sleep(0)
            ids = sorted(r.request_id for r in registry.list_pending())
            rec_a = await answer_pending(registry, "s1")
            rec_b = await answer_pending(registry, "s2")
            return json.loads(await ta), json.loads(await tb), rec_a, rec_b, ids

        a, b, rec_a, rec_b, ids = asyncio.run(scenario())
        self.assertEqual(len(ids), 2, "第二个会话必须另发一张卡，不能挂到别人的卡上")
        self.assertNotEqual(rec_a.request_id, rec_b.request_id)
        self.assertNotEqual(a["ref"], b["ref"])
        # B 的 cid 只落在 B 的会话格里：拿 A 的会话去查表必须还是 A 那条。
        self.assertEqual(registry.find_by_client_request_id("retry-1", "s1").request_id,
                         rec_a.request_id)
        self.assertEqual(registry.find_by_client_request_id("retry-1", "s2").request_id,
                         rec_b.request_id)

    def test_same_client_request_id_for_another_agent_gets_its_own_card(self):
        # 裁定 R15 的 agent 那一半：一张卡绝不服务两个 agent。
        other = {"session_id": "s1", "agent_id": "a2", "user_id": "u1", "channel": "console"}
        vault = Vault(ttl_seconds=300)
        registry = RequestRegistry(vault, timeout_seconds=600, max_pending=8)
        tools_a, _s, _scr, _v = build(ID, vault=vault, registry=registry)
        tools_b, _s2, _scr2, _v2 = build(other, vault=vault, registry=registry)

        async def scenario():
            ta = asyncio.create_task(
                tools_a.request_secret("l", "r", client_request_id="retry-1", timeout_seconds=30))
            await asyncio.sleep(0)
            tb = asyncio.create_task(
                tools_b.request_secret("l", "r", client_request_id="retry-1", timeout_seconds=30))
            await asyncio.sleep(0)
            rec_a = await answer_pending(registry, "s1")
            pending = [r for r in registry.list_pending("s1") if r.request_id != rec_a.request_id]
            self.assertEqual(len(pending), 1, "换 agent 必须重发一张卡")
            rec_b = await answer_pending(registry, "s1")
            return json.loads(await ta), json.loads(await tb), rec_a, rec_b

        a, b, rec_a, rec_b = asyncio.run(scenario())
        self.assertNotEqual(rec_a.request_id, rec_b.request_id)
        self.assertNotEqual(a["ref"], b["ref"])

    def test_no_session_context_still_creates_the_card_but_warns(self):
        # 裁定 R41：session_id 为空 = "没有会话"，卡片找不到它，但请求照建、WARNING 照报，
        # 且那一行既没有 label 也没有值。
        no_session = {"session_id": "", "agent_id": "a1", "user_id": "", "channel": ""}
        tools, registry, _scr, _v = build(no_session)
        secret_label = "LABEL-MUST-NOT-BE-LOGGED"

        with self.assertLogs("backend.tools", level="WARNING") as logged:
            async def scenario():
                task = asyncio.create_task(
                    tools.request_secret(secret_label, "r", timeout_seconds=30))
                await asyncio.sleep(0)
                rec = await answer_pending(registry, "")
                return json.loads(await task), rec

            out, rec = asyncio.run(scenario())
        text = "\n".join(logged.output)
        self.assertEqual(out["status"], "saved")
        self.assertEqual(rec.session_id, "")
        self.assertIn("/pending", text)                 # 只剩 loopback 裸 /pending 这一条发现路
        self.assertIn(rec.request_id, text)
        self.assertNotIn(secret_label, text)
        self.assertNotIn(DUMMY, text)

    def test_deadline_is_applied_by_the_poll_and_logged_as_swept(self):
        # `_await` 的截止分支：没人替它 sweep 时，自己 sweep 并按 R42/D1 记 "swept N"
        # —— 记 "N expired" 是假话，观察者是读时回收的，0 也不代表没有东西过期。
        tools, registry, _scr, _v = make()
        with self.assertLogs("backend.tools", level="INFO") as logged:
            async def expire():
                task = asyncio.create_task(tools.request_secret("l", "r", timeout_seconds=30))
                await asyncio.sleep(0)
                rec = registry.list_pending("s1")[0]
                rec.expires_at = 0.0                    # 拨时间，不 sleep（全局约束）
                return json.loads(await task), rec

            out, rec = asyncio.run(expire())
        text = "\n".join(logged.output)
        self.assertEqual(out["status"], "timed_out")
        self.assertEqual(rec.status, "timed_out")
        self.assertIn("swept 1", text)
        self.assertNotIn("expired", text)

    def test_task_cancellation_still_puts_the_record_in_a_terminal_state(self):
        # 宿主先 set `cancel_event`（`_coordinator.py:456/:221/:630`），再给 5 秒 shielded 宽限期
        # 让任务协作停（`:69/:73` 定 `cancel_grace=5.0`、`:946-960` 是那段等待），**宽限期用完**
        # 才 `task.cancel()`（`:962-972`；强制支 :451 那句注释讲的就是这个顺序）。
        # ⇒ 常规 Stop 走的是 Event 轮询那条主出口（上面 `test_cancel_event_short_circuits_to_cancelled`
        # 演的就是它）；这一条钉的是兜底那一支：真把 CancelledError 打进 sleep 时，记录必须终态、
        # 异常必须原样上抛（吞掉取消会把协调器的语义搞坏）。
        tools, registry, _scr, _v = make()

        async def scenario():
            task = asyncio.create_task(tools.request_secret("l", "r", timeout_seconds=30))
            await asyncio.sleep(0)
            rec = registry.list_pending("s1")[0]
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)
            return rec

        rec = asyncio.run(scenario())
        self.assertEqual(rec.status, "cancelled")
        self.assertEqual(rec.cancel_reason, "run_ended")
        self.assertEqual(registry.list_pending("s1"), [])

    def test_saved_registers_the_value_but_does_not_consume_the_ref(self):
        # I4 的登记钩子走非消费读；消费是 Task 8 那两个工具的权力（I3）。
        tools, registry, scrubber, vault = make()

        async def scenario():
            task = asyncio.create_task(tools.request_secret("l", "r", timeout_seconds=30))
            await asyncio.sleep(0)
            rec = await answer_pending(registry, "s1")
            return json.loads(await task), rec

        out, _rec = asyncio.run(scenario())
        self.assertIn(DUMMY, scrubber)
        self.assertEqual(vault.status(out["ref"]), "live")
        value, status = vault.consume(out["ref"])
        self.assertEqual((value, status), (DUMMY, "ok"))
        self.assertEqual(len(vault), 0)

    def test_registry_without_vault_peek_still_reports_saved(self):
        # records 的 `_vault_peek` 在 vault 没有 peek 时回 None。这一条钉住那处刻意的不对称：
        # 只有"重放"才有资格把 saved 降级成 ref_unavailable；否则一个没有 peek 的 vault
        # 会把人类刚提交的答案说成"这个 ref 已经用掉了"——那是假话，而且是模型可见的假话。
        class _NoPeekVault:
            _ttl = 300

            def __init__(self):
                self._values = {}

            def issue(self, value):
                ref = f"sr_stub_{len(self._values)}"
                self._values[ref] = value
                return ref

            def consume(self, ref):
                if ref in self._values:
                    return self._values.pop(ref), "ok"
                return None, "ref_unavailable"

            def sweep(self):
                return 0

        vault = _NoPeekVault()
        tools, registry, scrubber, _v = build(ID, vault=vault,
                                              registry=RequestRegistry(vault, timeout_seconds=600,
                                                                       max_pending=8))

        async def scenario():
            task = asyncio.create_task(
                tools.request_secret("l", "r", client_request_id="c7", timeout_seconds=30))
            await asyncio.sleep(0)
            rec = await answer_pending(registry, "s1")
            return json.loads(await task), rec

        out, _rec = asyncio.run(scenario())
        self.assertEqual(out["status"], "saved")
        self.assertTrue(out["ref"].startswith("sr_"))
        self.assertEqual(len(scrubber), 0)              # 没有 peek 就没有登记，仅此而已


class _BoomRegistry:
    """只用来制造一次内部失败：create 抛的是宿主形状的错误，消息里带着明文。"""

    def __init__(self, exc):
        self._exc = exc

    async def create(self, **_kwargs):
        raise self._exc


class FailureShapeTest(unittest.TestCase):
    def test_internal_failure_keeps_only_the_type_name(self):
        # 裁定 R43（Task 4/5 先例、spec 10.1）：结果串与日志都不许带 str(exc)。
        # `logger.exception` 会把 exc_text（这里就是那句带值的消息）打进日志 ⇒ 必须是 error+类名。
        boom = RuntimeError("host layer said: " + DUMMY)
        tools = SecretTools(registry=_BoomRegistry(boom), vault=Vault(ttl_seconds=300),
                            scrubber=Scrubber(), config=PluginConfig(),
                            identity=lambda: dict(ID), cancel_event=lambda: None)
        with self.assertLogs("backend.tools", level="ERROR") as logged:
            text = asyncio.run(tools.request_secret("l", "r"))
        out = json.loads(text)
        self.assertEqual(out["status"], "error")
        self.assertIn("RuntimeError", out["message"])
        self.assertNotIn(DUMMY, text)
        self.assertNotIn("host layer said", text)
        joined = "\n".join(logged.output)
        self.assertNotIn(DUMMY, joined)
        self.assertNotIn("host layer said", joined)


def drive_six_surfaces():
    """把六个状态面各跑一遍，返回 `{status: 结果 dict}`。

    形状门与文案门共用这一份驱动（M-2 要在同一批真结果上检查 hint，另写一遍场景就会和
    形状门漂开）。每个场景都显式夹 `timeout_seconds=30`（I-4），过期一律靠拨 `expires_at`。
    """
    seen = {}

    tools, registry, _scr, vault = make()

    async def saved_then_replay():
        first = asyncio.create_task(
            tools.request_secret("l", "r", client_request_id="k1", timeout_seconds=30))
        await asyncio.sleep(0)
        rec = await answer_pending(registry, "s1")
        saved = json.loads(await first)
        vault.consume(rec.ref)
        replay = json.loads(await tools.request_secret("l", "r", client_request_id="k1",
                                                       timeout_seconds=30))
        return saved, replay

    seen["saved"], seen["ref_unavailable"] = asyncio.run(saved_then_replay())

    tools, registry, _scr, _v = make()

    async def declined():
        task = asyncio.create_task(tools.request_secret("l", "r", timeout_seconds=30))
        await asyncio.sleep(0)
        rec = registry.list_pending("s1")[0]
        await registry.answer(rec.request_id, nonce=rec.nonce, decline=True)
        return json.loads(await task)

    seen["declined"] = asyncio.run(declined())

    evt = asyncio.Event()
    tools, registry, _scr, _v = make(cancel=lambda: evt)

    async def cancelled():
        task = asyncio.create_task(tools.request_secret("l", "r", timeout_seconds=30))
        await asyncio.sleep(0)
        evt.set()
        return json.loads(await task)

    seen["cancelled"] = asyncio.run(cancelled())

    tools, registry, _scr, _v = make()

    async def expired():
        task = asyncio.create_task(tools.request_secret("l", "r", timeout_seconds=30))
        await asyncio.sleep(0)
        rec = registry.list_pending("s1")[0]
        rec.expires_at = 0.0
        return json.loads(await task)

    seen["timed_out"] = asyncio.run(expired())

    tools, _r, _s, _v = make()
    seen["error"] = json.loads(asyncio.run(tools.request_secret("", "")))
    return seen


class ResultSurfaceTest(unittest.TestCase):
    """六个状态面的**键集是闭集**，且**文案点名 spec 要求的那个工具**。
    `test_no_url_or_peer_in_the_tool_result` 按字面量黑名单拦（http://、127.0.0.1、/api/），
    形状门按键集拦：多一个键就是多一样模型可以依赖、我们却要负责的东西，而它不必长得像 URL
    （红对照 C1 塞的 `card_url` 两种都该抓到 —— brief 那条 shape 用例只断言 status 与 hint，
    看不见多出来的键，所以这道门是补它的）。文案门补的是同一个方向的漏洞：**少**一个词
    （工具名）同样改变合同，而键集看不见它。"""

    def test_every_status_result_carries_exactly_its_own_keys(self):
        seen = drive_six_surfaces()
        self.assertEqual(set(seen), set(CLOSED_KEYSETS), "六个状态面必须都被跑到")
        for status, out in seen.items():
            self.assertEqual(out["status"], status)
            self.assertEqual(set(out), CLOSED_KEYSETS[status], f"{status} 面的键集漂了: {out}")

    def test_hints_name_exactly_the_tools_the_spec_names(self):
        # 评审 M-2：把 `request_secret` 从 `_HINT_TIMED_OUT` 里删掉，或把 `_HINT_SAVED` 里
        # 那两个消费点工具名删掉，都必须在这里变红（对照 H-1/H-2）。
        seen = drive_six_surfaces()
        for status, needles in HINT_TOOL_NAMES.items():
            for needle in needles:
                with self.subTest(status=status, needle=needle):
                    self.assertIn(needle, seen[status]["hint"])
        for status, banned in HINT_TOOL_FREE.items():
            for needle in banned:
                with self.subTest(status=status, banned=needle):
                    self.assertNotIn(needle, seen[status]["hint"])

    def test_unsettled_fallback_stays_inside_the_status_alphabet(self):
        # 评审 M-1：兜底出口过去写的是 `{"status": request.status}`，不变式一破就吐出闭集外
        # 的 "pending"（模型在文档里永远查不到这个值，而它已经拿着一个"还活着"的假信号）。
        # 现在加固成语义正确的 timed_out。这一条用 `_NoSweepRegistry` 把那条"不可达"的路
        # 强行走到 —— 加固必须可测，不能只写在注释里保证。
        vault = Vault(ttl_seconds=300)
        registry = _NoSweepRegistry(vault, timeout_seconds=600, max_pending=8)
        tools, _r, _s, _v = build(ID, registry=registry, vault=vault)

        async def scenario():
            task = asyncio.create_task(tools.request_secret("l", "r", timeout_seconds=30))
            await asyncio.sleep(0)
            rec = registry.list_pending("s1")[0]
            rec.expires_at = 0.0                    # 到点，但没人结算它
            return json.loads(await task), rec

        out, rec = asyncio.run(scenario())
        self.assertEqual(rec.status, "pending")     # 前提成立：这条世界线里 future 没被结算
        self.assertIn(out["status"], CLOSED_KEYSETS, f"闭集外的状态: {out}")
        self.assertEqual(out["status"], "timed_out")
        self.assertEqual(set(out), CLOSED_KEYSETS["timed_out"])
        self.assertIn("request_secret", out["hint"])


class _NoSweepRegistry(RequestRegistry):
    """只用于 M-1：把"到点自己 sweep"这一支的结算动作拿走（真 records 会结算，所以兜底
    出口在真代码里不可达）。轮询到点后 break，future 仍未结算 ⇒ 走 `_await` 的最后一支。"""

    def sweep(self) -> int:
        return 0


class _RecordingRegistry(RequestRegistry):
    """记下 create 交回的记录对象，好在失败之后仍然读得到它的终态（I-3 要看的正是它）。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.created = []

    async def create(self, **kwargs):
        request = await super().create(**kwargs)
        self.created.append(request)
        return request


class _NotAHostEvent:
    """宿主那个 ContextVar 的形状不由我们保证：`cancel_event()` 回一个没有 `is_set()` 的
    对象 ⇒ 轮询里抛 AttributeError（评审 I-3 的现场形状：异常绕过取消清理分支，卡还活着）。"""


class _RaisingSeam:
    """缝本身坏掉：读数这一句就抛。I-3 要求把读数挪进 `try`，靠的就是这一形 ——
    读数在 `try` 之外时，异常从 `_await` 第一行就逃出去，连循环都进不去。"""

    def __call__(self):
        raise RuntimeError("seam died: " + DUMMY)


class _BadCancelRegistry(_RecordingRegistry):
    """清账这一步自己再抛（宿主的注册表坏在第二次迁移上）。守卫必须让它闭嘴，
    否则顶掉原始失败的是这条清理路径，模型看到的错误就换了一个人。"""

    async def cancel(self, request_id: str, reason: str) -> bool:
        raise RuntimeError("cleanup exploded: " + DUMMY)


class SeamFailureTest(unittest.TestCase):
    """评审 I-3：`_await` 里任何非 CancelledError 的抛出都不许留下一张活卡。"""

    def test_seam_failure_does_not_leave_a_live_card(self):
        # 两形都要：读数就抛（RuntimeError）与读数形状不对（AttributeError，抛在循环里）。
        for label, expected, seam in (
                ("读数就抛", "RuntimeError", _RaisingSeam()),
                ("读数形状不对", "AttributeError", lambda: _NotAHostEvent())):
            with self.subTest(seam=label):
                vault = Vault(ttl_seconds=300)
                registry = _RecordingRegistry(vault, timeout_seconds=600, max_pending=8)
                tools, _r, _s, _v = build(ID, cancel=seam, registry=registry, vault=vault)

                with self.assertLogs("backend.tools", level="ERROR") as logged:
                    out = json.loads(asyncio.run(
                        tools.request_secret("l", "r", timeout_seconds=30)))
                rec = registry.created[0]
                # 返回形状不许因为多做了清账而改变（还是裁定 R43 那一句 error 话术）。
                self.assertEqual(out["status"], "error")
                self.assertEqual(set(out), CLOSED_KEYSETS["error"])
                self.assertIn(expected, out["message"])
                self.assertNotIn(DUMMY, json.dumps(out))
                # 兜底那条日志同样只许有类名：这里两条缝抛的消息都刻意带着 DUMMY。
                self.assertNotIn(DUMMY, "\n".join(logged.output))
                # 关键那条：这一次工具调用已经结束了，卡必须跟着进终态，否则人还能答它，
                # 答进去的值没有任何人认领（最长答到 expires_at）。
                self.assertEqual(registry.list_pending("s1"), [])
                self.assertEqual(rec.status, "cancelled")
                self.assertEqual(rec.cancel_reason, "run_ended")

    def test_cleanup_failure_keeps_the_original_error_and_stays_silent(self):
        # 清账自己再抛时：守卫必须保住原始失败（那是模型该看到的那一个），而且这条守卫
        # 自己的日志也只能有类名（R43）—— 清账消息里带着的正是宿主咬到的字符。
        vault = Vault(ttl_seconds=300)
        registry = _BadCancelRegistry(vault, timeout_seconds=600, max_pending=8)
        tools, _r, _s, _v = build(ID, cancel=lambda: _NotAHostEvent(),
                                  registry=registry, vault=vault)

        with self.assertLogs("backend.tools", level="ERROR") as logged:
            out = json.loads(asyncio.run(
                tools.request_secret("l", "r", timeout_seconds=30)))
        self.assertEqual(set(out), CLOSED_KEYSETS["error"])
        self.assertIn("AttributeError", out["message"])
        self.assertNotIn("RuntimeError", out["message"])
        joined = "\n".join(logged.output)
        self.assertNotIn(DUMMY, joined)
        self.assertNotIn("cleanup exploded", joined)


class _NoBaseUrlConfig:
    """装配给的 config 少了这个键（Task 9 之前没人保证 `PluginConfig` 的形状不变）。

    `request_secret` 在 `create()` **返回之后**、`_await` **之前**的那句 INFO 日志里读它，
    参数是即时求值的 ⇒ 这一句抛出去，卡已经插进 `_pending` 了。T7-b 说的窗口②就是这个。
    """

    def __getattr__(self, name):
        raise AttributeError("config has no " + name)


class CancelCleanupGuardTest(unittest.TestCase):
    """T7-a（Task 7 复评留下、由本文件现所有者 Task 8 同批收掉）。

    `_await` 的 CancelledError 支里那句裸 `await self._registry.cancel(...)`：宿主取消
    途中这一句自己抛的话，替换上来的 RuntimeError 会被 `request_secret` 的外层
    `except Exception` 接住、翻成 {"status":"error"} —— **取消被咽下去**，协调器收到的
    是"工具正常返回了一个结果"，5 s 宽限期与 force-cancel 的语义全废。
    """

    def test_registry_cancel_raising_during_a_hard_cancel_is_still_a_cancellation(self):
        vault = Vault(ttl_seconds=300)
        registry = _BadCancelRegistry(vault, timeout_seconds=600, max_pending=8)
        tools, _r, _s, _v = build(ID, registry=registry, vault=vault)

        async def scenario():
            task = asyncio.create_task(tools.request_secret("l", "r", timeout_seconds=30))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            return registry.created[0]

        with self.assertLogs("backend.tools", level="ERROR") as logged:
            rec = asyncio.run(scenario())

        # 取消原样上抛（上面 assertRaises 已经钉了），清账失败只留一条类名日志。
        # 这条用例红的时候长这样：`AssertionError: CancelledError not raised`，
        # 因为被咽下去的那一次交出的是 {"status":"error"} 结果串。
        joined = "\n".join(logged.output)
        self.assertNotIn(DUMMY, joined)
        self.assertNotIn("cleanup exploded", joined)
        self.assertIn("RuntimeError", joined)
        # 那个注册表是坏的，所以卡确实没能进终态 —— 这是它的错，不是清理顺序的错；
        # 记在这里防止下一个人把"卡还活着"误读成 T7-b 的回归。
        self.assertEqual(rec.status, "pending")


class OrphanWindowTest(unittest.TestCase):
    """T7-b（同上）：`create()` 与 `_await` 之间的抛出不能留下一张活卡。"""

    def test_failure_between_create_and_await_does_not_leave_a_live_card(self):
        vault = Vault(ttl_seconds=300)
        registry = _RecordingRegistry(vault, timeout_seconds=600, max_pending=8)
        tools = SecretTools(registry=registry, vault=vault, scrubber=Scrubber(),
                            config=_NoBaseUrlConfig(), identity=lambda: dict(ID),
                            cancel_event=lambda: None)

        with self.assertLogs("backend.tools", level="ERROR") as logged:
            out = json.loads(asyncio.run(
                tools.request_secret("l", "r", timeout_seconds=30)))

        self.assertEqual(set(out), CLOSED_KEYSETS["error"])
        self.assertIn("AttributeError", out["message"])
        self.assertNotIn(DUMMY, json.dumps(out))
        # 这一条红的时候长这样：`AssertionError: [<SecretRequest>] != []` —— 工具调用已经
        # 结束了，答页上还挂着一张能答、答完没人在乎的卡（spec 15 A6 要排除的形状）。
        self.assertEqual(registry.list_pending("s1"), [])
        rec = registry.created[0]
        self.assertEqual(rec.status, "cancelled")
        self.assertEqual(rec.cancel_reason, "run_ended")
        self.assertNotIn(DUMMY, "\n".join(logged.output))


class AnswerTimeoutPrecedenceTest(unittest.TestCase):
    """裁定 R44：`answer_timeout_seconds` 现在真的生效。

    签名默认是哨兵 0（含省略），函数体首行解析成 `int(timeout_seconds) or
    config.answer_timeout_seconds`。显式实参仍赢；省略（或 0）取配置。两条都在创建记录
    的那一刻读数（`expires_at - created_at`），不睡觉。
    """

    def _tools(self, cfg, registry_timeout=900):
        vault = Vault(ttl_seconds=300)
        registry = RequestRegistry(vault, timeout_seconds=registry_timeout, max_pending=8)
        tools = SecretTools(registry=registry, vault=vault, scrubber=Scrubber(), config=cfg,
                            identity=lambda: dict(ID), cancel_event=lambda: None)
        return tools, registry

    def test_explicit_timeout_seconds_still_wins_over_config_and_registry_default(self):
        cfg = PluginConfig(answer_timeout_seconds=1234)
        tools, registry = self._tools(cfg)

        async def scenario():
            task = asyncio.create_task(tools.request_secret("l", "r", timeout_seconds=30))
            await asyncio.sleep(0)
            rec = await answer_pending(registry, "s1")
            return json.loads(await task), rec

        out, rec = asyncio.run(scenario())
        self.assertEqual(out["status"], "saved")
        # 三个数各管一层，只有显式 30 到了记录上：config 1234（省略时才读）、
        # 注册表默认 900（只有调用方不传、config 也没有时才生效）、显式 30。
        self.assertEqual(round(rec.expires_at - rec.created_at), 30)

    def test_omitted_or_zero_timeout_uses_config_and_beats_registry_default(self):
        # 配置设成 records 的下限 30：省略实参必须拿到 30，而不是落回 600/900。
        # 只读记录上的截止，不等待超时本身（那条腿要 30 秒，慢得多）。
        cfg = PluginConfig(answer_timeout_seconds=30)
        tools, registry = self._tools(cfg)

        async def scenario():
            task = asyncio.create_task(tools.request_secret("l", "r"))
            await asyncio.sleep(0)
            rec = await answer_pending(registry, "s1")
            return json.loads(await task), rec

        out, rec = asyncio.run(scenario())
        self.assertEqual(out["status"], "saved")
        self.assertEqual(round(rec.expires_at - rec.created_at), 30)

        async def zero():
            task = asyncio.create_task(tools.request_secret("l", "r", timeout_seconds=0))
            await asyncio.sleep(0)
            rec = await answer_pending(registry, "s1")
            return json.loads(await task), rec

        out, rec = asyncio.run(zero())
        self.assertEqual(out["status"], "saved")
        self.assertEqual(round(rec.expires_at - rec.created_at), 30)

        async def without_arg():
            rec = await registry.create(label="l", reason="r", placeholder="",
                                        session_id="s1", agent_id="a1", user_id="u1",
                                        channel="console", timeout_seconds=None)
            return round(rec.expires_at - rec.created_at)

        self.assertEqual(asyncio.run(without_arg()), 900)


if __name__ == "__main__":
    unittest.main()
