"""The only place we read host per-call context. Imports stay inside functions
so unit tests never need a booted host."""

from __future__ import annotations

import asyncio

# 宿主实约定（qwenpaw 2.2.2 逐行读码）：`app/agent_context.py:351/378/388` 的三个 getter
# 在未设上下文时回 **None**，`get_current_agent_id()`(:320) 则回落 `get_active_agent_id()`
# ⇒ "default"。`tool_calls/_ctxvars.py:23` 的 ContextVar 默认 **None**，所以 `get_call_context()`
# 在工具调用之外拿到的是 None，`getattr(None, "cancel_event", None)` 安全地回 None。
# 每一个 import 都留在函数体内并带 try/except：单测永远不需要一个启动过的宿主（裁定 R30/R32），
# 而这四个名字是 tools 层唯一能拿到会话身份与取消信号的地方 —— 幂等键要按会话分（R15）、
# 取消要能真的看见（spec 15 A6），两边都只能从这里取。


def identity() -> dict[str, str]:
    out = {"session_id": "", "agent_id": "", "user_id": "", "channel": ""}
    try:
        from qwenpaw.app import agent_context as ac
    except Exception:  # noqa: BLE001
        return out
    for key, name in (("session_id", "get_current_session_id"),
                      ("agent_id", "get_current_agent_id"),
                      ("user_id", "get_current_user_id"),
                      ("channel", "get_current_channel")):
        getter = getattr(ac, name, None)
        if callable(getter):
            # 调用也要兜住（终审 Minor 17）：只 guard import 的话，某个 getter 一旦漂移
            # （改名/改签名/自己抛）就变成一次工具级 `error` 结果，而这一层的设计意图是
            # **静默降级**成空串 —— 会话身份只用于 `/pending` 过滤与幂等键分格，缺一项
            # 不该让整次索要失败。逐项兜，一个坏了不连累另外三个。
            try:
                out[key] = str(getter() or "")
            except Exception:  # noqa: BLE001
                out[key] = ""
    return out


def cancel_event() -> asyncio.Event | None:
    try:
        from qwenpaw.tool_calls._ctxvars import get_call_context
    except Exception:  # noqa: BLE001
        return None
    ctx = get_call_context()
    return getattr(ctx, "cancel_event", None)


def project_dir() -> str:
    try:
        from qwenpaw.config.context import get_current_project_dir
    except Exception:  # noqa: BLE001
        return ""
    return str(get_current_project_dir() or "")
