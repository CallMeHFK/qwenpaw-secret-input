import asyncio
import io
import json
import unittest

from tests.support import DUMMY

from backend.config import PluginConfig
from backend.records import RequestRegistry
from backend.scrub import Scrubber
from backend.tools import SecretTools
from backend.vault import Vault

ID = {"session_id": "s1", "agent_id": "a1", "user_id": "u1", "channel": "console"}


def make(cancel=None):
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


class RequestSecretTest(unittest.TestCase):
    def test_saved_result_has_ref_and_no_value(self):
        tools, registry, _scr, _v = make()

        async def scenario():
            task = asyncio.create_task(tools.request_secret("GitHub token", "signing"))
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
            task = asyncio.create_task(tools.request_secret("l", "r"))
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
        self.assertIn("client_request_id", out["hint"])

    def test_cancel_event_short_circuits_to_cancelled(self):
        tools, registry, _scr, _v = make(cancel=lambda: self.evt)
        self.evt = asyncio.Event()

        async def scenario():
            task = asyncio.create_task(tools.request_secret("l", "r"))
            await asyncio.sleep(0)
            self.evt.set()
            return json.loads(await task)

        out = asyncio.run(scenario())
        self.assertEqual(out["status"], "cancelled")
        self.assertEqual(out["reason"], "run_ended")

    def test_bad_label_returns_error_string_not_raise(self):
        tools, _r, _s, _v = make()
        out = json.loads(asyncio.run(tools.request_secret("", "  ")))
        self.assertEqual(out["status"], "error")

    def test_out_of_range_timeout_is_reported_not_raised(self):
        tools, _r, _s, _v = make()
        out = json.loads(asyncio.run(tools.request_secret("l", "r", timeout_seconds=5)))
        self.assertEqual(out["status"], "error")
        self.assertIn("30..3600", out["message"])

    def test_client_request_id_replay_gives_the_same_ref(self):
        tools, registry, _scr, vault = make()

        async def scenario():
            first = asyncio.create_task(tools.request_secret("l", "r", client_request_id="c1"))
            await asyncio.sleep(0)
            rec = registry.list_pending("s1")[0]
            await registry.answer(rec.request_id, nonce=rec.nonce, value=DUMMY)
            a = json.loads(await first)
            b = json.loads(await tools.request_secret("l", "r", client_request_id="c1"))
            return a, b, rec

        a, b, rec = asyncio.run(scenario())
        self.assertEqual(a["ref"], rec.ref)
        self.assertEqual(b["ref"], rec.ref)          # 重放可恢复丢失的结果

    def test_replay_after_consumption_reports_unavailable(self):
        tools, registry, _scr, vault = make()

        async def scenario():
            first = asyncio.create_task(tools.request_secret("l", "r", client_request_id="c9"))
            await asyncio.sleep(0)
            rec = registry.list_pending("s1")[0]
            await registry.answer(rec.request_id, nonce=rec.nonce, value=DUMMY)
            await first
            vault.consume(rec.ref)
            return json.loads(await tools.request_secret("l", "r", client_request_id="c9"))

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
                task = asyncio.create_task(tools.request_secret("l", "r"))
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
            task = asyncio.create_task(tools.request_secret("l", "r"))
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
            ta = asyncio.create_task(tools_a.request_secret("l", "r", client_request_id="retry-1"))
            await asyncio.sleep(0)
            tb = asyncio.create_task(tools_b.request_secret("l", "r", client_request_id="retry-1"))
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
            ta = asyncio.create_task(tools_a.request_secret("l", "r", client_request_id="retry-1"))
            await asyncio.sleep(0)
            tb = asyncio.create_task(tools_b.request_secret("l", "r", client_request_id="retry-1"))
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
                task = asyncio.create_task(tools.request_secret(secret_label, "r"))
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
        # 宿主 Stop 的真实形状是"先 set cancel_event，再 task.cancel()"
        # （qwenpaw 2.2.2 tool_calls/_coordinator.py:451/968-972）：CancelledError 落在我们
        # 正 await 的 sleep 上 ⇒ 轮询一次也跑不到。这一条钉住 `_await` 的 CancelledError 分支：
        # 记录必须终态、异常必须原样上抛（吞掉取消会把协调器的语义搞坏）。
        tools, registry, _scr, _v = make()

        async def scenario():
            task = asyncio.create_task(tools.request_secret("l", "r"))
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
            task = asyncio.create_task(tools.request_secret("l", "r"))
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
            task = asyncio.create_task(tools.request_secret("l", "r", client_request_id="c7"))
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


class ResultSurfaceTest(unittest.TestCase):
    """六个状态面的**键集是闭集**。`test_no_url_or_peer_in_the_tool_result` 按字面量黑名单
    拦（http://、127.0.0.1、/api/），这一条按形状拦：多一个键就是多一样模型可以依赖、
    我们却要负责的东西，而它不必长得像 URL（红对照 C1 塞的 `card_url` 两种都该抓到 ——
    brief 那条 shape 用例只断言 status 与 hint，看不见多出来的键，所以这道门是补它的）。"""

    def test_every_status_result_carries_exactly_its_own_keys(self):
        seen = {}

        tools, registry, _scr, vault = make()

        async def saved_then_replay():
            first = asyncio.create_task(tools.request_secret("l", "r", client_request_id="k1"))
            await asyncio.sleep(0)
            rec = await answer_pending(registry, "s1")
            saved = json.loads(await first)
            vault.consume(rec.ref)
            replay = json.loads(await tools.request_secret("l", "r", client_request_id="k1"))
            return saved, replay

        seen["saved"], seen["ref_unavailable"] = asyncio.run(saved_then_replay())

        tools, registry, _scr, _v = make()

        async def declined():
            task = asyncio.create_task(tools.request_secret("l", "r"))
            await asyncio.sleep(0)
            rec = registry.list_pending("s1")[0]
            await registry.answer(rec.request_id, nonce=rec.nonce, decline=True)
            return json.loads(await task)

        seen["declined"] = asyncio.run(declined())

        evt = asyncio.Event()
        tools, registry, _scr, _v = make(cancel=lambda: evt)

        async def cancelled():
            task = asyncio.create_task(tools.request_secret("l", "r"))
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

        self.assertEqual(set(seen), set(CLOSED_KEYSETS), "六个状态面必须都被跑到")
        for status, out in seen.items():
            self.assertEqual(out["status"], status)
            self.assertEqual(set(out), CLOSED_KEYSETS[status], f"{status} 面的键集漂了: {out}")


if __name__ == "__main__":
    unittest.main()
