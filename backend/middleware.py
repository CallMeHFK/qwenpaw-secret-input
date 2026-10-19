# -*- coding: utf-8 -*-
"""on_acting scrubber. Plugins are always inner of the host's pruning and
coordinator middlewares (qwenpaw/runtime/builder.py:1336-1343), so what we
yield is what gets truncated, offloaded to disk, and written into memory.

Fail-open is the ruling (R28): if scrubbing itself raises, the event is yielded
**unchanged** and exactly one ERROR line is logged, because silently dropping or
blanking a tool result would break the agent's tool chain, and a broken tool
chain costs more than one accidental occurrence of a plaintext the user already
put there by hand. The behaviour is asserted in tests/test_middleware.py — event
passes through, one ERROR record, no plaintext in it — and the ERROR line names
only the exception *class*, never its text: a traceback renders the message, and
an upstream message can quote the characters it choked on, which is the same
channel scrub.py's own backstops refuse to log (spec section 10.1).

Thread-free contract, for whoever assembles this (Task 9): ``Scrubber.replace``
is called on the event loop here and must stay there — never in a thread, a
thread pool, or ``asyncio.to_thread``. The registry it walks is single-loop only
(backend/scrub.py states it in the ``_live_values`` comment; backend/vault.py has
the same assumption but does not document it yet), and unlike the host's pruning
middleware this path does its work inline because str.replace over at most 64
entries against a <= 32KB result is cheap enough to keep it there.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncGenerator, Callable

from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolChunk, ToolResponse

from .config import PluginConfig
from .scrub import Scrubber

logger = logging.getLogger(__name__)

_MUTABLE_RESULT = (ToolResponse, ToolChunk)


def _block_text(block: Any) -> str | None:
    if isinstance(block, dict):
        value = block.get("text")
        return value if isinstance(value, str) else None
    value = getattr(block, "text", None)
    return value if isinstance(value, str) else None


def _replace_block(block: Any, text: str) -> Any:
    if isinstance(block, dict):
        return {**block, "text": text}
    return block.model_copy(update={"text": text})


class SecretScrubMiddleware(MiddlewareBase):
    """Replace known plaintexts in tool results before they reach the context.

    Only ``on_acting`` is implemented, following the host's pruning middleware
    (``ToolResultPruningMiddleware.on_acting``): consume the inner chain event by
    event, rewrite the text blocks of result events, pass everything else
    through by identity. Rewriting never mutates the event the host holds
    elsewhere — it copies the block and the event.
    """

    def __init__(self, scrubber: Scrubber) -> None:
        self._scrubber = scrubber

    async def on_acting(
        self,
        agent: Any,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        async for event in next_handler():
            if isinstance(event, _MUTABLE_RESULT):
                event = self._scrub_event(event)
            yield event

    def _scrub_event(self, event: Any) -> Any:
        try:
            content = list(event.content or [])
            changed = False
            for index, block in enumerate(content):
                text = _block_text(block)
                if text is None:
                    continue
                replacement = self._scrubber.replace(text)
                if replacement != text:
                    content[index] = _replace_block(block, replacement)
                    changed = True
            if not changed:
                return event
            return event.model_copy(update={"content": content})
        except Exception as exc:  # noqa: BLE001 - 擦洗失败绝不能改坏工具执行结果
            # R28 fail-open, and deliberately not logger.exception: the rendered
            # traceback would carry the exception message, which is data we do
            # not control and can contain part of the very text being scrubbed.
            logger.error("secret-input: scrub skipped for one tool result (%s)",
                         type(exc).__name__)
            return event


def make_scrub_middleware_factory(
    scrubber: Scrubber,
    config: PluginConfig,
) -> Callable[[Any, Any], SecretScrubMiddleware | None]:
    """Build the per-request factory the host calls during agent assembly.

    Returning ``None`` is how ``scrub_enabled=false`` switches the feature off:
    the middleware is never mounted, instead of mounted and doing nothing.
    Neither argument is read, so a plain ``object()`` is a legal caller.
    """

    # ctx / agent_config 都不读，所以宿主传什么都不影响（brief 里那条 ANN001
    # 抑制注释是惰性的：两个参数本来就带注解，规则不会命中）。
    def factory(ctx: Any, agent_config: Any) -> SecretScrubMiddleware | None:
        if not config.scrub_enabled:
            return None
        return SecretScrubMiddleware(scrubber)

    return factory
