"""I1: the plaintext never reaches the model's side of the wire.

Seven capture points. Each is paired with the control in tests that proves the
capture is reading something real.

test_3 必须多走一路：其中一次请求的 `label` 由模型写成含 `DUMMY` 的串。这条腿在 Task 9
实测出过真缺陷：`records.py` 原先按 R19 只把 label 截到 120 字符，短密钥照样整段落进宿主
日志；修法是这一行不再记 label/reason。现在它把 spec §10.1 "宿主日志：不会" 从推断变成
实测，也是 §10.2 item 4（模型自撰文案不可信）唯一的落地证据。
"""

import asyncio
import io
import json
import logging
import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agentscope.message import TextBlock
from agentscope.tool import ToolResponse
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.config import PluginConfig
from backend.middleware import SecretScrubMiddleware
from backend.records import RequestRegistry
from backend.router import build_router
from backend.scrub import REDACT, Scrubber
from backend.tools import SECRET_MARKER, SecretTools
from backend.vault import Vault
from tests.support import DUMMY

ID = {"session_id": "s1", "agent_id": "a1", "user_id": "u1", "channel": "console"}

# 门必须按秒失败，不能把测试套件挂住。工具自己的截止下限是 30 s（records._MIN_TIMEOUT），
# 所以 5 s 这个上限只能放在 asyncio 层：整个场景被 wait_for 包住，答页链路一旦断掉就是
# TimeoutError 而不是等到 600 s。显式 30 s 则保证工具内部的 deadline 不会退回配置默认。
_FLOW_WATCHDOG_SECONDS = 5
_TOOL_TIMEOUT_SECONDS = 30

# spec §7 的返回表：request_secret 六态 + 两个消费点的 ok/ref_expired。
DOCUMENTED_STATUSES = {
    "saved", "declined", "timed_out", "cancelled", "ref_unavailable",
    "ref_expired", "error", "ok",
}
# spec §7.1 的 request_secret 闭集（重放态含在内）。
REQUEST_STATUSES = {"saved", "declined", "timed_out", "cancelled",
                    "ref_unavailable", "error"}


def _wire_bytes(text: str) -> int:
    """What the host measures: the string's JSON serialized size, minus quotes."""
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8")) - 2


def _boom_writer(_key: str, _value: str) -> None:
    """Raises with the plaintext in the message; the log must carry class+site only."""
    raise ValueError(DUMMY)


class InvariantsTest(unittest.TestCase):
    def setUp(self):
        self.vault = Vault(ttl_seconds=300)
        self.registry = RequestRegistry(self.vault, timeout_seconds=600, max_pending=8)
        self.scrubber = Scrubber()
        self.config = PluginConfig()
        self.tools = SecretTools(registry=self.registry, vault=self.vault,
                                 scrubber=self.scrubber, config=self.config,
                                 identity=lambda: dict(ID), cancel_event=lambda: None,
                                 env_writer=lambda k, v: None,
                                 project_dir=lambda: str(Path(tempfile.gettempdir()).resolve()))
        app = FastAPI()
        app.include_router(build_router(registry=self.registry, config=self.config,
                                        vault=self.vault), prefix="/api/secret-input")
        self.client = TestClient(app, client=("127.0.0.1", 52000))
        self.log_capture = io.StringIO()
        self.handler = logging.StreamHandler(self.log_capture)
        # 带 logger 名：test_3 才能把"插件自己发的行"和别的库的 DEBUG 噪音分开。
        self.handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        self.root = logging.getLogger()
        self._old_level = self.root.level
        # DEBUG 抓全：records 的 INFO 审计行与 tools 的失败 ERROR 行都在这一层下面。
        self.root.setLevel(logging.DEBUG)
        self.root.addHandler(self.handler)

    def tearDown(self):
        self.root.removeHandler(self.handler)
        self.root.setLevel(self._old_level)

    def _run_bounded(self, coro):
        """Run one scenario under a 5 s ceiling; a stuck answer path fails, not hangs."""
        result = asyncio.run(asyncio.wait_for(coro, timeout=_FLOW_WATCHDOG_SECONDS))
        self.assertIsNotNone(result, "scenario did not complete")
        return result

    async def _spend_with_approval(self, coro):
        """Run a consuming tool and approve its confirmation card from the test client.

        The invariant suite drives the production path end to end, and since the consent
        gate defaults on that path now includes a card. Approving it here keeps these
        tests measuring the real flow instead of a gate-disabled variant.
        """
        task = asyncio.create_task(coro)
        for _ in range(500):
            pending = self.registry.list_pending()
            if pending:
                rec = pending[0]
                res = self.client.post(
                    f"/api/secret-input/card/{rec.request_id}/answer",
                    json={"nonce": rec.nonce, "approve": True})
                self.assertEqual(res.status_code, 200, res.text)
                self.assertEqual(res.json()["status"], "approved")
                return await task
            await asyncio.sleep(0.01)
        raise AssertionError("no confirmation card appeared")

    def _full_flow(self):
        async def scenario():
            task = asyncio.create_task(
                self.tools.request_secret("GitHub token", "signing",
                                          timeout_seconds=_TOOL_TIMEOUT_SECONDS))
            await asyncio.sleep(0)
            rec = self.registry.list_pending("s1")[0]
            res = self.client.post(f"/api/secret-input/card/{rec.request_id}/answer",
                                   json={"nonce": rec.nonce, "value": DUMMY})
            result_text = await task
            exec_text = await self._spend_with_approval(
                self.tools.secret_exec(rec.ref, f'printf "%s" {SECRET_MARKER}'))
            export_text = await self._spend_with_approval(
                self.tools.secret_export(self.vault.issue(DUMMY), "GITHUB_TOKEN"))
            return rec, res, result_text, exec_text, export_text

        return self._run_bounded(scenario())

    def _ask_and_answer(self, label="GitHub token", *, reason="signing",
                        toolset=None, registry=None):
        toolset = toolset or self.tools
        registry = registry or self.registry

        async def scenario():
            task = asyncio.create_task(toolset.request_secret(
                label, reason, timeout_seconds=_TOOL_TIMEOUT_SECONDS))
            await asyncio.sleep(0)
            rec = registry.list_pending("s1")[0]
            res = self.client.post(f"/api/secret-input/card/{rec.request_id}/answer",
                                   json={"nonce": rec.nonce, "value": DUMMY})
            return rec, res, await task

        return self._run_bounded(scenario())

    def test_1_input_schema_has_no_value_bearing_field(self):
        import inspect

        allow = {"label", "reason", "placeholder", "timeout_seconds", "client_request_id",
                 "ref", "command", "env_name", "cwd", "max_output_bytes", "env_key"}
        for fn in (self.tools.request_secret, self.tools.secret_exec, self.tools.secret_export):
            params = set(inspect.signature(fn).parameters) - {"self"}
            self.assertTrue(params <= allow, f"{fn.__name__}: {params - allow}")

    def test_2_tool_results_carry_no_value_and_the_saved_value_is_scrubbable(self):
        _rec, _res, result_text, exec_text, export_text = self._full_flow()
        for text in (result_text, exec_text, export_text):
            self.assertNotIn(DUMMY, text)
        self.assertIn("sr_", result_text)
        # 隔离腿（R44 的变异对照就是打这一条）：只经过 request_secret 的一次保存，
        # 还没有任何消费点碰过这份登记表。saved 分支必须把值登记给 scrub —— 否则
        # 下一条流经中间件的工具结果就带着明文进上下文。
        vault = Vault(ttl_seconds=300)
        registry = RequestRegistry(vault, timeout_seconds=600, max_pending=8)
        scrubber = Scrubber()
        toolset = SecretTools(registry=registry, vault=vault, scrubber=scrubber,
                              config=self.config, identity=lambda: dict(ID),
                              cancel_event=lambda: None, env_writer=lambda k, v: None,
                              project_dir=lambda: str(Path(tempfile.gettempdir()).resolve()))

        async def only_request():
            task = asyncio.create_task(
                toolset.request_secret("l", "r", timeout_seconds=_TOOL_TIMEOUT_SECONDS))
            await asyncio.sleep(0)
            rec = registry.list_pending("s1")[0]
            await registry.answer(rec.request_id, nonce=rec.nonce, value=DUMMY)
            return await task

        text = self._run_bounded(only_request())
        self.assertNotIn(DUMMY, text)
        self.assertEqual(scrubber.replace(f"x{DUMMY}y"), f"x{REDACT}y")

    def test_3_nothing_reaches_the_log(self):
        # §10.2 item 4：label 是模型写的文本，可以正好是一个短密钥。旧的 120 字符
        # 截断挡不住它；records.py 现在不记 label/reason，这一路直接量宿主日志通道。
        label = f"GitHub token {DUMMY}"
        reason = "signing-canary-8f31"
        rec, res, _text = self._ask_and_answer(label, reason=reason)
        self.assertEqual(res.status_code, 200)
        self.handler.flush()
        captured = self.log_capture.getvalue()
        plugin_lines = [line for line in captured.splitlines() if line.startswith("backend.")]
        # 阳性对照不是测试自己写的行：必须看到 records._finish 在 pending+answer 周期里
        # 真实发出的审计行，否则下面的 assertNotIn 只是对空通道说话。
        audit = (f"secret-input request {rec.request_id[:8]} -> saved "
                 f"(answered by=127.0.0.1 answered_at=")
        self.assertTrue(any(audit in line for line in plugin_lines),
                        f"records audit line not captured: {plugin_lines!r}")
        for line in plugin_lines:
            self.assertNotIn("GitHub token", line)
            self.assertNotIn(reason, line)
            self.assertNotIn(DUMMY, line)
        self.assertNotIn(DUMMY, captured)

    def test_4_no_endpoint_response_carries_the_value(self):
        label = "GitHub token"
        reason = "signing"

        async def scenario():
            task = asyncio.create_task(
                self.tools.request_secret(label, reason,
                                          timeout_seconds=_TOOL_TIMEOUT_SECONDS))
            await asyncio.sleep(0)
            rec = self.registry.list_pending("s1")[0]

            # 先读"还活着"的三面：_full_flow 会把 ref 全部消费掉，之后再查 /card 与
            # /meta 只能读到静态页/终态，断言再干净也证明不了响应面本身。
            pending_session = self.client.get("/api/secret-input/pending",
                                              params={"session_id": "s1"})
            pending_all = self.client.get("/api/secret-input/pending")
            card = self.client.get(f"/api/secret-input/card/{rec.request_id}")
            meta = self.client.get(f"/api/secret-input/card/{rec.request_id}/meta")
            self.assertEqual(pending_session.status_code, 200)
            self.assertEqual(pending_all.status_code, 200)
            self.assertEqual(card.status_code, 200)
            self.assertEqual(meta.status_code, 200)
            listed = pending_session.json()["requests"]
            self.assertEqual([item["request_id"] for item in listed], [rec.request_id])
            self.assertEqual(listed[0]["label"], label)
            self.assertEqual(listed[0]["reason"], reason)
            self.assertIn(rec.request_id, pending_all.text)
            self.assertIn(rec.request_id, card.text)          # 卡片带的正面内容
            meta_body = meta.json()
            self.assertEqual(meta_body["status"], "pending")
            self.assertEqual(meta_body["label"], label)
            self.assertEqual(meta_body["reason"], reason)
            self.assertTrue(meta_body["nonce"])
            for body in (pending_session.text, pending_all.text, card.text, meta.text):
                self.assertNotIn(DUMMY, body)

            res = self.client.post(f"/api/secret-input/card/{rec.request_id}/answer",
                                   json={"nonce": rec.nonce, "value": DUMMY})
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json(), {"status": "saved"})
            await task

            # 终态：答案已经进了 vault，卡与 meta 还读得到（墓地），但都不许回显值。
            done_card = self.client.get(f"/api/secret-input/card/{rec.request_id}")
            done_meta = self.client.get(f"/api/secret-input/card/{rec.request_id}/meta")
            self.assertEqual(done_card.status_code, 200)
            self.assertEqual(done_meta.status_code, 200)
            self.assertEqual(done_meta.json()["status"], "saved")
            self.assertEqual(done_meta.json()["label"], label)
            for body in (res.text, done_card.text, done_meta.text):
                self.assertNotIn(DUMMY, body)
            return rec, res

        rec, _res = self._run_bounded(scenario())
        self.assertEqual(rec.status, "saved")

    def test_5_memory_bound_block_is_scrubbed(self):
        self._full_flow()
        mw = SecretScrubMiddleware(self.scrubber)

        async def drive():
            async def next_handler():
                yield ToolResponse(content=[TextBlock(text=f"token is {DUMMY}")])

            async for event in mw.on_acting(None, {"tool_call": object()}, next_handler):
                return event

        block_text = asyncio.run(drive()).content[0].text
        self.assertEqual(block_text, f"token is {REDACT}")

    def test_6_disk_artifact_is_scrubbed(self):
        from qwenpaw.agents.tools.utils import ToolResultPruner

        self._full_flow()
        mw = SecretScrubMiddleware(self.scrubber)

        async def drive():
            async def next_handler():
                yield ToolResponse(content=[TextBlock(text=("tok " + DUMMY + " ") * 80)])

            async for event in mw.on_acting(None, {"tool_call": object()}, next_handler):
                return event

        scrubbed = asyncio.run(drive())
        blocks = scrubbed.content or []
        before = " ".join(getattr(b, "text", "") for b in blocks)
        with tempfile.TemporaryDirectory() as tmp:
            pruner = ToolResultPruner(tmp)
            # 宿主形状（qwenpaw/agents/middlewares.py:800-803）：块列表 + metadata，
            # 不是字符串。list 分支会就地改写块。
            pruned, changed = pruner.prune_output(blocks, max_bytes=64,
                                                  metadata=scrubbed.metadata)
            files = list(Path(tmp).glob("*.txt"))
            self.assertTrue(changed and files, "没走到落盘分支")
            after = " ".join(getattr(b, "text", "") for b in pruned)
            # 真正要钉的是"就地剪短"：assertIs 只证明返回了同一个 list，剪没剪都绿。
            self.assertLess(len(after), len(before), "块没有被剪短")
            self.assertNotIn(DUMMY, after)
            for path in files:
                self.assertNotIn(DUMMY, path.read_text(encoding="utf-8"))

        # 第二腿：双流都灌满的 secret_exec 结果。R47 的预算是**合计**的序列化字节，
        # 结果必须低于宿主 50000 B 的 recent 剪枝阈值，宿主的 pruner 因此不落盘。
        ref = self.vault.issue(DUMMY)
        script = (
            "import sys;"
            f"sys.stdout.write('A' * 100000 + {SECRET_MARKER!r});"
            f"sys.stderr.write('B' * 100000 + {SECRET_MARKER!r})"
        )
        result = self._run_bounded(self._spend_with_approval(
            self.tools.secret_exec(
                ref, f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}")))
        payload = json.loads(result)
        self.assertEqual(payload["status"], "ok")
        combined = _wire_bytes(payload["stdout"]) + _wire_bytes(payload["stderr"])
        self.assertLessEqual(combined, 32768)          # max_output_bytes 是合池预算
        self.assertLess(len(result.encode("utf-8")), 50000)   # 宿主 recent 剪枝阈值
        self.assertIn("[truncated by secret-input]", payload["stdout"])
        self.assertIn("[truncated by secret-input]", payload["stderr"])
        self.assertNotIn(DUMMY, result)
        with tempfile.TemporaryDirectory() as tmp:
            pruner = ToolResultPruner(tmp)
            changed = pruner.prune_output([TextBlock(text=result)],
                                          max_bytes=50000, metadata={})[1]
            self.assertFalse(changed, "结果过了 50000 B，宿主会把它整份写盘")
            self.assertEqual(list(Path(tmp).glob("*.txt")), [])

    def test_7_error_strings_carry_no_value_and_every_status_stays_in_the_closed_set(self):
        statuses = set()
        _rec, _res, result_text, exec_text, export_text = self._full_flow()
        statuses.update(json.loads(text)["status"]
                        for text in (result_text, exec_text, export_text))

        # declined
        async def decline():
            task = asyncio.create_task(
                self.tools.request_secret("l", "r", timeout_seconds=_TOOL_TIMEOUT_SECONDS))
            await asyncio.sleep(0)
            rec = self.registry.list_pending("s1")[0]
            res = self.client.post(f"/api/secret-input/card/{rec.request_id}/answer",
                                   json={"nonce": rec.nonce, "decline": True})
            return res, await task

        res, text = self._run_bounded(decline())
        self.assertEqual(res.status_code, 200)
        statuses.add(json.loads(text)["status"])

        # timed_out：把记录上的截止拨到过去，由工具自己的轮询出口扫掉（不睡觉）。
        async def timed_out():
            task = asyncio.create_task(
                self.tools.request_secret("l", "r", timeout_seconds=_TOOL_TIMEOUT_SECONDS))
            await asyncio.sleep(0)
            rec = self.registry.list_pending("s1")[0]
            rec.expires_at = time.monotonic() - 1
            return await task

        statuses.add(json.loads(self._run_bounded(timed_out()))["status"])

        # cancelled：本轮已被 Stop（cancel_event 注入缝）。
        event = asyncio.Event()
        event.set()
        cancelling = SecretTools(registry=self.registry, vault=self.vault,
                                 scrubber=self.scrubber, config=self.config,
                                 identity=lambda: dict(ID), cancel_event=lambda: event,
                                 env_writer=lambda k, v: None,
                                 project_dir=lambda: str(Path(tempfile.gettempdir()).resolve()))

        async def cancelled():
            task = asyncio.create_task(
                cancelling.request_secret("l", "r", timeout_seconds=_TOOL_TIMEOUT_SECONDS))
            await asyncio.sleep(0)
            return await task

        statuses.add(json.loads(self._run_bounded(cancelled()))["status"])

        # ref_unavailable + error（注入的 writer 抛出的文本里故意带 DUMMY）。
        broken = SecretTools(registry=self.registry, vault=self.vault, scrubber=self.scrubber,
                             config=self.config, identity=lambda: dict(ID),
                             cancel_event=lambda: None, env_writer=None, project_dir=lambda: "/")
        out = json.loads(asyncio.run(broken.secret_exec("sr_whatever", "true", cwd="/")))
        statuses.add(out["status"])
        self.assertNotIn(DUMMY, json.dumps(out))

        # 这一腿只验 env 写入失败的错误面；确认卡本身由 tests/test_consume_approval.py
        # 覆盖，这里把门关掉，免得等一张这个测试并不打算答的卡。
        boom = SecretTools(registry=self.registry, vault=self.vault, scrubber=self.scrubber,
                           config=PluginConfig(require_consume_approval=False),
                           identity=lambda: dict(ID),
                           cancel_event=lambda: None, env_writer=_boom_writer,
                           project_dir=lambda: "")
        out2 = json.loads(asyncio.run(boom.secret_export(self.vault.issue(DUMMY),
                                                         "GITHUB_TOKEN")))
        statuses.add(out2["status"])
        self.assertEqual(out2["status"], "error")
        self.assertNotIn(DUMMY, json.dumps(out2))

        # 异常路径的**日志**必须是固定形状（类名 + 帧坐标 + 工具名），异常报文里的
        # 明文不许跟着进去。ref_expired 不是异常（见下），class+site 只属于这一支。
        self.handler.flush()
        failure_lines = [line for line in self.log_capture.getvalue().splitlines()
                         if line.startswith("backend.tools:")
                         and "secret_export env write failed" in line]
        self.assertEqual(len(failure_lines), 1, failure_lines)
        self.assertRegex(failure_lines[0],
                         r"exc=ValueError site=.*test_invariants\.py:\d+ in _boom_writer "
                         r"tool=secret_export")
        self.assertNotIn(DUMMY, failure_lines[0])

        # ref_expired：真的等过一次 TTL（Vault 的下限是 1 s），不是伪造的过期标记。
        # 它是结果里的闭集状态（status+hint），不是异常 ⇒ 这一条断言固定结果形状。
        expiring = Vault(ttl_seconds=1)
        expiring_tools = SecretTools(
            registry=RequestRegistry(expiring, timeout_seconds=600, max_pending=8),
            vault=expiring, scrubber=self.scrubber, config=self.config,
            identity=lambda: dict(ID), cancel_event=lambda: None,
            env_writer=lambda k, v: None,
            project_dir=lambda: str(Path(tempfile.gettempdir()).resolve()))
        stale = expiring.issue(DUMMY)
        time.sleep(1.1)
        expired = json.loads(asyncio.run(expiring_tools.secret_exec(stale, "true")))
        statuses.add(expired["status"])
        self.assertEqual(expired["status"], "ref_expired")
        self.assertEqual(set(expired), {"status", "hint"})
        self.assertIn("request_secret", expired["hint"])
        self.assertNotIn(DUMMY, json.dumps(expired))

        self.assertEqual(statuses, DOCUMENTED_STATUSES)

    def test_control_the_grep_can_actually_see_the_value(self):
        """阳性对照：捕获通道不是空读，`assertNotIn(DUMMY, ...)` 也不是空话。"""
        self._full_flow()
        # 宿主自己的日志行确实落进了捕获（这条 INFO 每张卡都有）。
        self.assertIn("answer page for request", self.log_capture.getvalue())
        # 再把 DUMMY 本身写进同一个 logger 管道：test_3 用的断言能在真实捕获上命中。
        logging.getLogger("tests.invariants").warning("capture control: %s", DUMMY)
        self.handler.flush()
        self.assertIn(DUMMY, self.log_capture.getvalue())
        rec, res, _a, _b, _c = self._full_flow()
        self.assertEqual(res.status_code, 200)


if __name__ == "__main__":
    unittest.main()
