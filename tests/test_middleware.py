# -*- coding: utf-8 -*-
"""on_acting scrubbing (spec §3 I4) plus the §10.1 path-⑥ disk gate.

The disk pair (`test_disk_artifact_is_scrubbed` +
`test_disk_artifact_control_proves_the_gate_is_live`) is the load-bearing one:
it re-verifies against the *real* host `ToolResultPruner` that scrubbing
happens before the truncate-and-offload step, instead of trusting the onion-order
reading in `qwenpaw/runtime/builder.py:1336-1343`. Both call it the way the host
does — with the **block list**, `prune_output(response.content or [], ...)`, as
`qwenpaw/agents/middlewares.py:800-803` does — so this file is a faithful pattern
for Task 9's `test_invariants` path⑥.

Every gate here is proven able to go red (spec §12's red-control column, R23):
the log-channel and repr assertions each carry a positive control, and the
rewriting assertions are what the brief's Step 5 mutation reddens. R32's
"middleware must not import the host package" is *not* asserted here — it is an
AST gate in tests/test_secret_store_gate.py, next to the R23 gates.
"""

import asyncio
import inspect
import logging
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from tests.support import DUMMY

from backend.config import PluginConfig
from backend.middleware import SecretScrubMiddleware, make_scrub_middleware_factory
from backend.scrub import REDACT, Scrubber

from agentscope.message import DataBlock, TextBlock, ToolCallBlock
from agentscope.tool import ToolChunk, ToolResponse


@dataclass
class RegistrySnapshot:
    """A value-carrying repr: the shape the no-repr tripwire guards against.

    Slapping `@dataclass` on `Scrubber` (or on any wrapper holding the registry)
    generates a repr that prints every field, i.e. every known plaintext. The
    helper in `test_middleware_repr_names_no_plaintext` must catch this object,
    otherwise that gate is empty.
    """

    registry: list


def collect(mw, events, input_kwargs=None):
    """Drive `on_acting` the way the host drives it and hand back the events.

    `input_kwargs` defaults to a bare `object()` tool call (R31: the middleware
    reads nothing out of it); the R28 case passes a shaped one to prove the ERROR
    line picks up the tool *name* and never the tool input.
    """

    async def drive():
        async def next_handler():
            for event in events:
                yield event

        out = []
        async for item in mw.on_acting(agent=None,
                                       input_kwargs=input_kwargs or {"tool_call": object()},
                                       next_handler=next_handler):
            out.append(item)
        return out

    return asyncio.run(drive())


def blocks_of(event):
    return [getattr(b, "text", None) for b in (event.content or [])]


def joined_text(blocks):
    """The text the host keeps for the context, straight off the block list."""
    parts = []
    for block in blocks:
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        parts.append(text if isinstance(text, str) else "")
    return " ".join(parts)


def reveals_plaintext(obj):
    """Would this object's repr leak the dummy plaintext? (spec §10.1, log channel)"""
    return DUMMY in repr(obj)


def blob_block():
    """A non-text block. `media_type` is not optional on this host: agentscope
    2.0.10's `DataBlock.source` is `Union[Base64Source, URLSource]` and both
    require it (the brief left it out and could not build the block at all)."""
    return DataBlock(source={"type": "url", "url": "file:///tmp/x.png",
                             "media_type": "image/png"})


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
        """路径⑥：截断落盘读到的必须是擦洗后的文本。

        调用形态对齐宿主：`qwenpaw/agents/middlewares.py:800-803` 喂给
        `prune_output` 的是 **块列表**（`response.content or []`）与
        `response.metadata`，不是字符串；list 分支（`agents/tools/utils.py:219-239`）
        还会经 `_set_block_text` 就地改写块。之前这里喂的是 str，走的是另一条
        分支（评审 Important 2）——两条分支最后确实都进 `prune_text` →
        `save_text_output`，但 §15 A2 的话是"用真宿主 pruner 复证"，所以按宿主
        的形状调，Task 9 的 test_invariants 路径⑥ 才抄得到同一个形状。
        """
        from qwenpaw.agents.tools.utils import ToolResultPruner

        (scrubbed,) = collect(self.mw, [ToolResponse(
            content=[TextBlock(text=("leak " + DUMMY + " ") * 60)])])
        before = scrubbed.content[0].text
        self.assertIn(REDACT, before)
        self.assertNotIn(DUMMY, before)          # 擦洗先发生，否则下面看的都不是上下文里那份
        with tempfile.TemporaryDirectory() as tmp:
            pruner = ToolResultPruner(tmp)
            blocks, changed = pruner.prune_output(scrubbed.content or [], max_bytes=64,
                                                  metadata=scrubbed.metadata)
            files = list(Path(tmp).glob("*.txt"))
            self.assertTrue(changed and files, "这条门没走到落盘分支，等于没测")
            self.assertIs(blocks, scrubbed.content)   # list 分支就地改块，不换新列表
            # 落盘的整份与留在块上的截断桩都必须干净。
            for path in files:
                self.assertNotIn(DUMMY, path.read_text(encoding="utf-8"))
            self.assertNotIn(DUMMY, joined_text(blocks))
            self.assertLess(len(joined_text(blocks)), len(before),
                            "块没被就地截断，说明这条门看的不是宿主那条 list 分支")

    def test_disk_artifact_control_proves_the_gate_is_live(self):
        """阳性对照：不经中间件的同一断言必须泄露，否则上面那条是空门。

        同样按宿主的形状喂块列表（评审 Important 2 的"控制也要 host-shaped"）：
        摘掉中间件时，落盘文件与块上文本两条轴都得看得见明文。
        """
        from qwenpaw.agents.tools.utils import ToolResultPruner

        raw = ToolResponse(content=[TextBlock(text=("leak " + DUMMY + " ") * 60)])
        with tempfile.TemporaryDirectory() as tmp:
            pruner = ToolResultPruner(tmp)
            blocks, changed = pruner.prune_output(raw.content or [], max_bytes=64,
                                                  metadata=raw.metadata)
            files = list(Path(tmp).glob("*.txt"))
            self.assertTrue(changed and files)
            self.assertIn(DUMMY, files[0].read_text(encoding="utf-8"))
            self.assertIn(DUMMY, joined_text(blocks))

    # --- R28: 擦洗本身抛了 => 放行 + 可观测，且不泄露 -------------------------

    def test_scrubber_failure_fails_open_with_one_clean_error_log(self):
        # 断言：(a) 事件原样放行（同一个对象，一个字都不能变）—— 擦洗坏了
        # 也不能把工具链打断，打断工具链比泄露一次意外出现的明文更糟；
        # (b) 恰好一条 ERROR；(c) 那条日志（含 traceback 渲染出来的每一帧与异常
        # 报文）里没有明文；(d) R34：ERROR 行要能定位现场——异常类名 + 最深帧的
        # 文件:行号/函数名 + 工具**名**，但绝不含异常报文、也绝不读 tool_call.input
        # （那里正是明文可能在的地方）。异常报文里故意带 DUMMY：中间件要是把异常
        # 文本写进日志（`logger.exception` 就会），(c) 立刻红。
        def boom(text):
            raise RuntimeError(f"simulated scrubber explosion on {text}")

        self.scrubber.replace = boom
        original = ToolResponse(content=[TextBlock(text=f"got {DUMMY} here")])
        # 宿主形状的 tool_call 是 pydantic 的 ToolCallBlock（Task 5 carry-in：原来
        # 用裸 dict，`_tool_name` 的生产分支其实一条用例都没钉住）。`input` 在宿主
        # 模型里是 str，所以明文就放在那里 —— 中间件只准读 `name`。
        tool_call = ToolCallBlock(id="tc-1", name="secret_exec", input=DUMMY)
        with self.assertLogs("backend.middleware", level="DEBUG") as logged:
            (out,) = collect(self.mw, [original], input_kwargs={"tool_call": tool_call})

        self.assertIs(out, original)
        self.assertEqual(blocks_of(out), [f"got {DUMMY} here"])
        errors = [record for record in logged.records if record.levelno == logging.ERROR]
        self.assertEqual(len(errors), 1, render(logged.records))
        line = render(logged.records)
        self.assertNotIn(DUMMY, line)
        # (d) 现场身份：静默降级必须在宿主日志里找得到位置，否则 I4 掉了一次
        # 没人知道掉在哪（R34）。摘掉帧信息这条立刻红。断言整段 `file:line in func`
        # 而不是 `site=\S*`：检出路径里带空格时（Task 5 carry-in）后者会假红。
        self.assertIn("RuntimeError", line)
        self.assertIn(f"site={__file__}:{inspect.getsourcelines(boom)[1] + 1} in boom", line)
        self.assertIn(f":{inspect.getsourcelines(boom)[1] + 1} in boom", line)
        self.assertIn("tool=secret_exec", line)
        # 报文与控制台里的值都不许出现：只准有类名与帧坐标。
        self.assertNotIn("simulated scrubber explosion", line)
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
        # 如实写清这条的性质：今天这三者都是默认对象 repr（scrub.py 与
        # middleware.py 里没有 __repr__/__str__），所以三条 assertFalse 当场不可能
        # 红 —— 它是一根**面向未来的绊线**：谁给 Scrubber 加 @dataclass（生成的
        # repr 会把登记表整串打出来）就会红。绊线必须先自证能红（评审 Important 1），
        # 判据同 R23 与 Task 4 抓到的那条空门：同一个 helper 要先能抓到 repr 里
        # 真有明文的对象。
        factory = make_scrub_middleware_factory(self.scrubber, PluginConfig())
        self.assertFalse(reveals_plaintext(self.mw))
        self.assertFalse(reveals_plaintext(factory))
        self.assertFalse(reveals_plaintext(self.scrubber))
        # 绊线自证：一个把值装进 repr 的对象必须被同一个 helper 抓到——两种最可能
        # 的未来写法：SimpleNamespace 与 @dataclass（给登记表加装饰器时生成的
        # repr 会把整串值打出来）。
        self.assertTrue(reveals_plaintext(SimpleNamespace(registry=[DUMMY])),
                        "helper 抓不到会泄的 repr = 这条门是空的")
        self.assertTrue(reveals_plaintext(RegistrySnapshot(registry=[DUMMY])))

    def test_a_mixed_event_rewrites_only_the_text_block(self):
        # R35：真实的一条事件里就是会混着 text 块与 data 块（工具结果 + 截图）。
        # 要同时成立：text 块被改写，**改写后的事件里** DataBlock 按身份活着
        # （不是"另建了一个长得一样的"），宿主靠它取图。
        blob = blob_block()
        text_block = TextBlock(text=f"shot of {DUMMY}")
        original = ToolResponse(content=[text_block, blob])
        (out,) = collect(self.mw, [original])
        self.assertIsNot(out, original)
        self.assertEqual(blocks_of(out), [f"shot of {REDACT}", None])
        self.assertIs(out.content[1], blob)
        self.assertIsNot(out.content[0], text_block)
        self.assertEqual(text_block.text, f"shot of {DUMMY}")   # 原块没被就地改

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
        # spec §6.1 明写改写要覆盖 dict 形态的块（`block["text"]`），宿主剪枝中间件
        # 同样按 dict 读（`block.get`）。这条分支在本宿主上是**防御性**的：正常构造
        # 走不到 —— `ToolResponse(content=[{"type": "text", ...}])` 会被 pydantic
        # 强制转成 TextBlock（本机实测），只有绕过校验的构造（`model_construct`，
        # 例如反序列化路径或 `model_construct` 类工厂）才把裸 dict 留在 content 里。
        # 所以这里用 `model_construct` 造，别改回普通构造：那样测的是 TextBlock 分支。
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
