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

from fastapi import FastAPI
from fastapi.testclient import TestClient
from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from tests.support import DUMMY

from backend.config import PluginConfig
from backend.middleware import SecretScrubMiddleware
from backend.records import RequestRegistry
from backend.router import build_router
from backend.scrub import REDACT, Scrubber
from backend.tools import SECRET_MARKER, SecretTools
from backend.vault import Vault

ID = {"session_id": "s1", "agent_id": "a1", "user_id": "u1", "channel": "console"}

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
        self.root = logging.getLogger()
        self._old_level = self.root.level
        if self.root.level == logging.NOTSET or self.root.level > logging.INFO:
            self.root.setLevel(logging.INFO)
        self.root.addHandler(self.handler)

    def tearDown(self):
        self.root.removeHandler(self.handler)
        self.root.setLevel(self._old_level)

    def _full_flow(self):
        async def scenario():
            task = asyncio.create_task(self.tools.request_secret("GitHub token", "signing"))
            await asyncio.sleep(0)
            rec = self.registry.list_pending("s1")[0]
            res = self.client.post(f"/api/secret-input/card/{rec.request_id}/answer",
                                   json={"nonce": rec.nonce, "value": DUMMY})
            result_text = await task
            exec_text = await self.tools.secret_exec(rec.ref, f'printf "%s" {SECRET_MARKER}')
            export_text = await self.tools.secret_export(self.vault.issue(DUMMY), "GITHUB_TOKEN")
            return rec, res, result_text, exec_text, export_text

        return asyncio.run(scenario())

    def _ask_and_answer(self, label="GitHub token", *, toolset=None, registry=None):
        toolset = toolset or self.tools
        registry = registry or self.registry

        async def scenario():
            task = asyncio.create_task(toolset.request_secret(label, "signing"))
            await asyncio.sleep(0)
            rec = registry.list_pending("s1")[0]
            res = self.client.post(f"/api/secret-input/card/{rec.request_id}/answer",
                                   json={"nonce": rec.nonce, "value": DUMMY})
            return rec, res, await task

        return asyncio.run(scenario())

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
            task = asyncio.create_task(toolset.request_secret("l", "r"))
            await asyncio.sleep(0)
            rec = registry.list_pending("s1")[0]
            await registry.answer(rec.request_id, nonce=rec.nonce, value=DUMMY)
            return await task

        text = asyncio.run(only_request())
        self.assertNotIn(DUMMY, text)
        self.assertEqual(scrubber.replace(f"x{DUMMY}y"), f"x{REDACT}y")

    def test_3_nothing_reaches_the_log(self):
        self._full_flow()
        # §10.2 item 4：label 是模型写的文本，可以正好是一个短密钥。旧的 120 字符
        # 截断挡不住它；records.py 现在不记 label/reason，这一路直接量宿主日志通道。
        self._ask_and_answer(f"GitHub token {DUMMY}")
        self.handler.flush()
        captured = self.log_capture.getvalue()
        self.assertIn("answer page for request", captured)     # 先证明捕获是活的
        self.assertNotIn(DUMMY, captured)

    def test_4_no_endpoint_response_carries_the_value(self):
        rec, res, _result, _e, _x = self._full_flow()
        bodies = [res.text]
        for path in (f"/api/secret-input/card/{rec.request_id}",
                     f"/api/secret-input/card/{rec.request_id}/meta"):
            bodies.append(self.client.get(path).text)
        bodies.append(self.client.get("/api/secret-input/pending",
                                      params={"session_id": "s1"}).text)
        bodies.append(self.client.get("/api/secret-input/pending").text)
        for body in bodies:
            self.assertNotIn(DUMMY, body)

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
        text = scrubbed.content[0].text
        with tempfile.TemporaryDirectory() as tmp:
            pruner = ToolResultPruner(tmp)
            # 宿主形状（qwenpaw/agents/middlewares.py:800-803）：块列表 + metadata，
            # 不是字符串。list 分支会就地改写块。
            blocks, changed = pruner.prune_output(scrubbed.content or [], max_bytes=64,
                                                  metadata=scrubbed.metadata)
            files = list(Path(tmp).glob("*.txt"))
            self.assertTrue(changed and files, "没走到落盘分支")
            self.assertIs(blocks, scrubbed.content)
            for path in files:
                self.assertNotIn(DUMMY, path.read_text(encoding="utf-8"))
            joined = " ".join(getattr(b, "text", "") for b in blocks)
            self.assertNotIn(DUMMY, joined)

        # 第二腿：双流都灌满的 secret_exec 结果。R47 的预算是**合计**的序列化字节，
        # 结果必须低于宿主 50000 B 的 recent 剪枝阈值，宿主的 pruner 因此不落盘。
        ref = self.vault.issue(DUMMY)
        script = (
            "import sys;"
            f"sys.stdout.write('A' * 100000 + {SECRET_MARKER!r});"
            f"sys.stderr.write('B' * 100000 + {SECRET_MARKER!r})"
        )
        result = asyncio.run(self.tools.secret_exec(
            ref, f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"))
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
            blocks, changed = pruner.prune_output([TextBlock(text=result)],
                                                  max_bytes=50000, metadata={})
            self.assertFalse(changed, "结果过了 50000 B，宿主会把它整份写盘")
            self.assertEqual(list(Path(tmp).glob("*.txt")), [])

    def test_7_error_strings_carry_no_value_and_every_status_stays_in_the_closed_set(self):
        statuses = set()
        _rec, _res, result_text, exec_text, export_text = self._full_flow()
        statuses.update(json.loads(text)["status"]
                        for text in (result_text, exec_text, export_text))

        # declined
        async def decline():
            task = asyncio.create_task(self.tools.request_secret("l", "r"))
            await asyncio.sleep(0)
            rec = self.registry.list_pending("s1")[0]
            res = self.client.post(f"/api/secret-input/card/{rec.request_id}/answer",
                                   json={"nonce": rec.nonce, "decline": True})
            return res, await task

        res, text = asyncio.run(decline())
        self.assertEqual(res.status_code, 200)
        statuses.add(json.loads(text)["status"])

        # timed_out：把记录上的截止拨到过去，由工具自己的轮询出口扫掉（不睡觉）。
        async def timed_out():
            task = asyncio.create_task(self.tools.request_secret("l", "r"))
            await asyncio.sleep(0)
            rec = self.registry.list_pending("s1")[0]
            rec.expires_at = time.monotonic() - 1
            return await task

        statuses.add(json.loads(asyncio.run(timed_out()))["status"])

        # cancelled：本轮已被 Stop（cancel_event 注入缝）。
        event = asyncio.Event()
        event.set()
        cancelling = SecretTools(registry=self.registry, vault=self.vault,
                                 scrubber=self.scrubber, config=self.config,
                                 identity=lambda: dict(ID), cancel_event=lambda: event,
                                 env_writer=lambda k, v: None,
                                 project_dir=lambda: str(Path(tempfile.gettempdir()).resolve()))

        async def cancelled():
            task = asyncio.create_task(cancelling.request_secret("l", "r"))
            await asyncio.sleep(0)
            return await task

        statuses.add(json.loads(asyncio.run(cancelled()))["status"])

        # ref_unavailable + error（注入的 writer 抛出的文本里故意带 DUMMY）。
        broken = SecretTools(registry=self.registry, vault=self.vault, scrubber=self.scrubber,
                             config=self.config, identity=lambda: dict(ID),
                             cancel_event=lambda: None, env_writer=None, project_dir=lambda: "/")
        out = json.loads(asyncio.run(broken.secret_exec("sr_whatever", "true", cwd="/")))
        statuses.add(out["status"])
        self.assertNotIn(DUMMY, json.dumps(out))

        boom = SecretTools(registry=self.registry, vault=self.vault, scrubber=self.scrubber,
                           config=self.config, identity=lambda: dict(ID),
                           cancel_event=lambda: None,
                           env_writer=lambda k, v: (_ for _ in ()).throw(ValueError(DUMMY)),
                           project_dir=lambda: "")
        out2 = json.loads(asyncio.run(boom.secret_export(self.vault.issue(DUMMY),
                                                         "GITHUB_TOKEN")))
        statuses.add(out2["status"])
        self.assertEqual(out2["status"], "error")
        self.assertNotIn(DUMMY, json.dumps(out2))

        self.assertEqual(statuses, REQUEST_STATUSES | {"ok"})
        self.assertTrue(statuses <= DOCUMENTED_STATUSES)

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
