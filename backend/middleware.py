# -*- coding: utf-8 -*-
"""on_acting scrubber. Plugins are always inner of the host's pruning and
coordinator middlewares (qwenpaw/runtime/builder.py:1336-1343), so what we
yield is what gets truncated, offloaded to disk, and written into memory.

Fail-open is the ruling (R28): if scrubbing itself raises, the event is yielded
**unchanged** and exactly one ERROR line is logged, because silently dropping or
blanking a tool result would break the agent's tool chain, and a broken tool
chain costs more than one accidental occurrence of a plaintext the user already
put there by hand. The behaviour is asserted in tests/test_middleware.py — event
passes through, one ERROR record, no plaintext in it. The ERROR line carries the
exception *class*, the deepest traceback frame as `file:line in function`, and
the tool *name* (ruling R34): a silent I4 degradation that cannot be located in
the host log is worth less than nothing, while the exception text itself must
never be logged — a rendered traceback shows the message, and an upstream message
can quote the characters it choked on, which is the same channel scrub.py's own
backstops refuse to log (spec section 10.1). Only
``traceback.extract_tb()``'s filename/lineno/name are read (a frame's source line
and locals are never rendered), and the tool name is model-supplied, so it is
accepted only when it is a plain identifier; ``tool_call.input`` is never read.

Thread-free contract, for whoever assembles this (Task 9): ``Scrubber.replace``
is called on the event loop here and must stay there — never in a thread, a
thread pool, or ``asyncio.to_thread``. The registry it walks is single-loop only
(backend/scrub.py states it in the ``_live_values`` comment; backend/vault.py
states it in its class docstring, ruling R40), and unlike the host's pruning
middleware this path does its work inline because str.replace over at most 64
entries against a <= 32KB result is cheap enough to keep it there.

Import discipline (rulings R30/R32/R33): this layer must not depend on the host
package, so it stays testable and immune to host refactors. Nothing here imports
``qwenpaw`` — not even for a type — and the only absolute third-party import is
``agentscope``; relative siblings (``.scrub``) are legal because they are our own
package. ``PluginConfig`` is needed for an annotation only, so it is imported
under ``typing.TYPE_CHECKING`` and never at runtime (proved by
``'backend.config' in sys.modules`` after importing this module); annotations are
strings here because of ``from __future__ import annotations``, so the one thing
that would need the real name is ``typing.get_type_hints`` on the factory — the
host and agentscope never call it on middleware (checked: no ``get_type_hints``
in ``qwenpaw/plugins``, ``qwenpaw/agent``, ``qwenpaw/tool_calls``,
``qwenpaw/runtime``, ``agentscope/middleware``). The R32 rule is enforced by an
AST walk over this file in tests/test_secret_store_gate.py.
"""

from __future__ import annotations

import logging
import traceback
from typing import TYPE_CHECKING, Any, AsyncGenerator, Callable

from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolChunk, ToolResponse

from .scrub import Scrubber

if TYPE_CHECKING:  # R33: annotation-only, so the runtime import stays out
    from .config import PluginConfig

logger = logging.getLogger(__name__)

_MUTABLE_RESULT = (ToolResponse, ToolChunk)

# A real tool name is a registry identifier; anything longer or oddly shaped is
# not worth a log line, and this field comes from the model (see _tool_name).
_MAX_TOOL_NAME_CHARS = 64


def _tool_name(input_kwargs: dict[str, Any]) -> str:
    """The name of the tool being scrubbed, or ``"unknown"``.

    Deliberately narrow (R34): only ``tool_call.name`` is looked at, only when it
    is a plain identifier within ``_MAX_TOOL_NAME_CHARS``, and ``tool_call.input``
    — the payload that may carry the very plaintext being scrubbed — is never
    touched. The name is model-supplied, so a value that is not shaped like a
    registry identifier must not reach the log channel. The ``isinstance`` checks
    are not decoration: the host contract is a dict, but this runs inside the
    fail-open path, where a mistyped caller must cost a log field, not a tool
    result.
    """
    call = input_kwargs.get("tool_call") if isinstance(input_kwargs, dict) else None
    name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
    if (isinstance(name, str) and name.isidentifier()
            and len(name) <= _MAX_TOOL_NAME_CHARS):
        return name
    return "unknown"


def _failure_detail(exc: BaseException, input_kwargs: dict[str, Any]) -> str:
    """Locate a scrub failure without rendering anything that carries a value.

    Frames are taken from ``traceback.extract_tb`` and only their
    filename/lineno/name are used: never ``str(exc)``, never a frame's source
    line, never its locals. Built so that the fail-open path cannot raise — if
    anything here goes wrong it degrades to ``site=unknown`` rather than
    interrupting the tool chain R28 protects.
    """
    try:
        frames = traceback.extract_tb(exc.__traceback__)
        deepest = frames[-1] if frames else None
        site = "unknown" if deepest is None else (
            f"{deepest.filename}:{deepest.lineno} in {deepest.name}")
        return f"exc={type(exc).__name__} site={site} tool={_tool_name(input_kwargs)}"
    except Exception:  # noqa: BLE001 - 记不住现场也不能打断工具链
        return f"exc={type(exc).__name__} site=unknown tool=unknown"


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
                event = self._scrub_event(event, input_kwargs)
            yield event

    def _scrub_event(self, event: Any, input_kwargs: dict[str, Any]) -> Any:
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
            # The line still has to say *where* it happened (R34) — hence the
            # frame identity from _failure_detail, which reads no values.
            logger.error("secret-input: scrub skipped for one tool result (%s)",
                         _failure_detail(exc, input_kwargs))
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
