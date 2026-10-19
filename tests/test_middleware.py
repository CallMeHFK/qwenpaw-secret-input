# -*- coding: utf-8 -*-
"""on_acting scrubbing (spec §3 I4) plus the §10.1 path-⑥ disk gate.

The disk pair (`test_disk_artifact_is_scrubbed` +
`test_disk_artifact_control_proves_the_gate_is_live`) is the load-bearing one:
it re-verifies against the *real* host `ToolResultPruner` that scrubbing
happens before the truncate-and-offload step, instead of trusting the onion-order
reading in `qwenpaw/runtime/builder.py:1336-1343`.
"""

import asyncio
import logging
import tempfile
import unittest
from pathlib import Path

from tests.support import DUMMY

from backend.config import PluginConfig
from backend.middleware import SecretScrubMiddleware, make_scrub_middleware_factory
from backend.scrub import REDACT, Scrubber

from agentscope.message import DataBlock, TextBlock
from agentscope.tool import ToolChunk, ToolResponse


def collect(mw, events):
    async def drive():
        async def next_handler():
            for event in events:
                yield event

        out = []
        async for item in mw.on_acting(agent=None, input_kwargs={"tool_call": object()},
                                       next_handler=next_handler):
            out.append(item)
        return out

    return asyncio.run(drive())


def blocks_of(event):
    return [getattr(b, "text", None) for b in (event.content or [])]


def render(records):
    """As a host handler would: the message *and* any rendered traceback.

    A captured-log assertion that only read `record.getMessage()` would call a
    `logger.exception` call clean, because the exception text lives in the
    traceback section. Everything the middleware can hand to a handler has to
    pass the no-plaintext assertion, so this renders the whole record.
    """
    formatter = logging.Formatter()
    return "\n".join(formatter.format(record) for record in records)


class MiddlewareTest(unittest.TestCase):
    def setUp(self):
        self.scrubber = Scrubber()
        self.scrubber.register(DUMMY)
        self.mw = SecretScrubMiddleware(self.scrubber)

    def test_tool_response_text_block_is_scrubbed(self):
        (out,) = collect(self.mw, [ToolResponse(content=[TextBlock(text=f"got {DUMMY} here")])])
        self.assertEqual(blocks_of(out), [f"got {REDACT} here"])

    def test_tool_chunk_is_scrubbed_too(self):
        (out,) = collect(self.mw, [ToolChunk(content=[TextBlock(text=DUMMY)])])
        self.assertEqual(blocks_of(out), [REDACT])

    def test_non_text_blocks_pass_through_untouched(self):
        # media_type 不是可选项：agentscope 2.0.10 的 DataBlock.source 是
        # Union[Base64Source, URLSource]，URLSource 里 url 与 media_type 都必填
        # （brief 少写了这一个，直接 ValidationError，构造不出非文本块）。
        blob = DataBlock(source={"type": "url", "url": "file:///tmp/x.png",
                                 "media_type": "image/png"})
        (out,) = collect(self.mw, [ToolResponse(content=[blob])])
        self.assertIs(out.content[0], blob)

    def test_events_that_are_not_results_pass_through(self):
        sentinel = object()

        async def drive():
            async def next_handler():
                yield sentinel

            got = []
            async for item in self.mw.on_acting(agent=None,
                                                input_kwargs={"tool_call": object()},
                                                next_handler=next_handler):
                got.append(item)
            return got

        self.assertIs(asyncio.run(drive())[0], sentinel)

    def test_original_object_is_not_mutated(self):
        # 改写必须产出新对象，宿主别处的引用不能被动过
        original = ToolResponse(content=[TextBlock(text=DUMMY)])
        (out,) = collect(self.mw, [original])
        self.assertIsNot(out, original)
        self.assertEqual(original.content[0].text, DUMMY)

    def test_factory_returns_none_when_scrub_disabled(self):
        factory = make_scrub_middleware_factory(self.scrubber,
                                                PluginConfig(scrub_enabled=False))
        self.assertIsNone(factory(object(), object()))

    def test_factory_returns_middleware_when_enabled(self):
        factory = make_scrub_middleware_factory(self.scrubber, PluginConfig())
        self.assertIsInstance(factory(object(), object()), SecretScrubMiddleware)

    def test_disk_artifact_is_scrubbed(self):
        """路径⑥：截断落盘读到的必须是擦洗后的文本。"""
        from qwenpaw.agents.tools.utils import ToolResultPruner

        (scrubbed,) = collect(self.mw, [ToolResponse(
            content=[TextBlock(text=("leak " + DUMMY + " ") * 60)])])
        text = blocks_of(scrubbed)[0]
        with tempfile.TemporaryDirectory() as tmp:
            pruner = ToolResultPruner(tmp)
            pruned, changed = pruner.prune_output(text, max_bytes=64, metadata={})
            files = list(Path(tmp).glob("*.txt"))
            self.assertTrue(changed and files, "这条门没走到落盘分支，等于没测")
            for path in files:
                self.assertNotIn(DUMMY, path.read_text(encoding="utf-8"))
            self.assertNotIn(DUMMY, pruned)

    def test_disk_artifact_control_proves_the_gate_is_live(self):
        """阳性对照：不经中间件的同一断言必须泄露，否则上面那条是空门。"""
        from qwenpaw.agents.tools.utils import ToolResultPruner

        raw = ("leak " + DUMMY + " ") * 60
        with tempfile.TemporaryDirectory() as tmp:
            pruner = ToolResultPruner(tmp)
            _pruned, changed = pruner.prune_output(raw, max_bytes=64, metadata={})
            files = list(Path(tmp).glob("*.txt"))
            self.assertTrue(changed and files)
            self.assertIn(DUMMY, files[0].read_text(encoding="utf-8"))

    # --- R28: 擦洗本身抛了 => 放行 + 可观测，且不泄露 -------------------------

    def test_scrubber_failure_fails_open_with_one_clean_error_log(self):
        # 三条断言：(a) 事件原样放行（同一个对象，一个字都不能变）—— 擦洗坏了
        # 也不能把工具链打断，打断工具链比泄露一次意外出现的明文更糟；
        # (b) 恰好一条 ERROR；(c) 那条日志（含 traceback 渲染出来的每一帧与异常
        # 报文）里没有明文。异常报文里故意带 DUMMY：中间件要是把异常文本写进日
        # 志（`logger.exception` 就会），(c) 立刻红。
        def boom(text):
            raise RuntimeError(f"simulated scrubber explosion on {text}")

        self.scrubber.replace = boom
        original = ToolResponse(content=[TextBlock(text=f"got {DUMMY} here")])
        with self.assertLogs("backend.middleware", level="DEBUG") as logged:
            (out,) = collect(self.mw, [original])

        self.assertIs(out, original)
        self.assertEqual(blocks_of(out), [f"got {DUMMY} here"])
        errors = [record for record in logged.records if record.levelno == logging.ERROR]
        self.assertEqual(len(errors), 1, render(logged.records))
        self.assertNotIn(DUMMY, render(logged.records))
        # 阳性对照：证明上面那条断言看的通道真会带上异常文本，不是空门。
        try:
            raise RuntimeError(DUMMY)
        except RuntimeError as control:
            probe = logging.LogRecord("backend.middleware", logging.ERROR,
                                       __file__, 1, "probe", None, None)
            probe.exc_info = (type(control), control, control.__traceback__)
            self.assertIn(DUMMY, render([probe]))

    # --- 其余承诺：通到日志的 repr、事件流顺序、块形态、只动 text --------------

    def test_middleware_repr_names_no_plaintext(self):
        # §10.1 的日志通道判定要落到"对象被写进日志时打出来的那串"上：中间件、
        # 它的工厂、以及它握着的登记表，repr 里都不能出现明文。
        factory = make_scrub_middleware_factory(self.scrubber, PluginConfig())
        self.assertNotIn(DUMMY, repr(self.mw))
        self.assertNotIn(DUMMY, repr(factory))
        self.assertNotIn(DUMMY, repr(self.scrubber))

    def test_every_event_is_yielded_in_order(self):
        # 「always yield every event」是硬约束：一条都不能被吞掉，顺序不能变。
        events = [
            ToolChunk(content=[TextBlock(text=DUMMY)]),
            ToolChunk(content=[DataBlock(source={"type": "url", "url": "file:///tmp/x.png",
                                                  "media_type": "image/png"})]),
            object(),
            ToolResponse(content=[TextBlock(text="nothing to scrub here")]),
        ]
        out = collect(self.mw, events)
        self.assertEqual(len(out), 4)
        self.assertEqual(blocks_of(out[0]), [REDACT])
        self.assertIs(out[1], events[1])
        self.assertIs(out[2], events[2])
        self.assertIs(out[3], events[3])

    def test_dict_shaped_text_block_is_scrubbed_without_mutating_it(self):
        # spec §6.1 明写改写要覆盖 dict 形态的块（`block["text"]`）——反序列化
        # 过来的事件里块就是裸 dict，宿主剪枝中间件同样按 dict 读（block.get）。
        # 裸 dict 也不能就地改：那是宿主别处持有的同一个对象。
        block = {"type": "text", "text": f"got {DUMMY} here"}
        event = ToolResponse.model_construct(content=[block])
        (out,) = collect(self.mw, [event])
        self.assertEqual([item["text"] for item in out.content], [f"got {REDACT} here"])
        self.assertEqual(block["text"], f"got {DUMMY} here")

    def test_only_the_text_blocks_change(self):
        # spec §6.1：「改写只动 text 块，不改 state/id」。宿主靠 id 配对工具结果、
        # 靠 state 判终态，动这两个字段就是把擦洗变成故障。
        original = ToolResponse(content=[TextBlock(text=DUMMY)], id="tool-id-1",
                                metadata={"truncation": {"index": 0}})
        (out,) = collect(self.mw, [original])
        self.assertEqual(out.content[0].text, REDACT)
        self.assertEqual(out.id, original.id)
        self.assertEqual(out.state, original.state)
        self.assertEqual(out.metadata, original.metadata)

    def test_two_agent_configs_get_their_own_middleware_over_one_registry(self):
        # spec §10.1「多 agent / subagent」行点名由本文件来测：装配两次、各拿一个
        # 实例，登记表是进程内共享的那一份 ⇒ 子 agent 的出站结果同样被擦。
        factory = make_scrub_middleware_factory(self.scrubber, PluginConfig())
        mw_a = factory(object(), object())
        mw_b = factory(object(), object())
        self.assertIsInstance(mw_a, SecretScrubMiddleware)
        self.assertIsInstance(mw_b, SecretScrubMiddleware)
        self.assertIsNot(mw_a, mw_b)
        for mw in (mw_a, mw_b):
            (out,) = collect(mw, [ToolResponse(content=[TextBlock(text=DUMMY)])])
            self.assertEqual(blocks_of(out), [REDACT])

if __name__ == "__main__":
    unittest.main()
