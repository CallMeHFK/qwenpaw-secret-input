"""Answer transport. The plaintext only ever moves in a POST body.

Access control: a loopback TCP peer, or a caller the host's AuthMiddleware
already authenticated (``request.state.user``). ``allow_remote`` overrides both.
The host binds 0.0.0.0 and auth is off by default, so this plugin-level gate is
the only thing standing between the LAN and these four endpoints (spec 8.2).

Read surfaces are deliberately anaemic: nothing here can hand back a stored
value or a ref (spec 3 I2), and the nonce goes out from exactly one route
(``/card/{id}/meta``) and only while the card is still ``pending`` — that is
spec 8.1's two-step, not an accident to be "simplified" away.
"""

from __future__ import annotations

import json
import logging
import time
import unicodedata
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from .card_page import html_response

if TYPE_CHECKING:
    from .config import PluginConfig
    from .records import RequestRegistry, SecretRequest
    from .vault import Vault

logger = logging.getLogger(__name__)

# `request.client.host` 是**套接字地址**，永远不会等于字符串 "localhost"（终审 Minor 9：
# 原来那一项是死的，而且一旦宿主哪天挂上 ProxyHeadersMiddleware，它就成了可用 Host/XFF
# 头伪造的放行口）。IPv4-mapped 的 `::ffff:127.0.0.1`（`::` 绑定时 loopback 的形状）刻意
# 不在表里 —— fail closed：宁可让本机 IPv6 回环走 `request.state.user` 那条腿，也不放宽匹配。
_LOOPBACK = {"127.0.0.1", "::1"}
# 一次性 nonce 是持有型凭据：拿到它 + request_id（就在 INFO 日志与答页 URL 里）就能替人提交。
# 答页设计上要在新标签页打开，`answer_url` 又是按 Host 头推出来的（远程控制台要能点）⇒ 中间
# 可能有代理。no-store + nosniff 挡住浏览器与中间缓存留下比 pending 窗口更长寿的副本
# （终审 Important 3；验收 Step 10 当时把这条显式留给终审裁定）。
_NO_STORE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
# 一条 answer 请求该有的形状，全部在这里定义一次；超限不是"截断"而是拒绝。
_MAX_NONCE_CHARS = 128            # nonce 是 token_urlsafe(24)，32 字符；128 是宽裕的上界
_MAX_VALUE_CHARS = 4096           # 计划正文的 Field(max_length=4096)
_MAX_ANSWER_BYTES = 64 * 1024     # 读 body 之前先按 Content-Length 拦一刀（无该头时靠上面的字符数）
# reason → 状态码的唯一一张表：registry 的闭集（spec 7.1）与端点自己产出的 422 都走这里，
# 免得"同一个 reason 在两个地方各有各的码"。未知 reason 一律 403 收口（fail closed）。
# 表里只放"会被 _rejected() 发出去的 reason"：成功路径的 200 不在这里（它由 FastAPI 的
# 默认值给出，没有 reason 可查），所以也没有一个只为了把 200 留在断言集合里的空键。
_ANSWER_MAP = {
    "not_found": ("not_found", 404),
    "invalid_nonce": ("invalid_nonce", 403),
    "already_answered": ("already_answered", 410),
    "expired": ("expired", 410),
    # 裁定 R20：value 为空且不是拒答 ⇒ 端点前置 422，绝不进 registry.answer()。
    # registry 的 reason 是闭集，"nonce 正确但值为空"只能落 invalid_nonce，在空输入框上
    # 按保存的人会看到"nonce 不对"这种误导性文案。
    "empty_value": ("empty_value", 422),
    # 端点自己产出的形状错误。detail 只能是这里列出的固定串：body 里带着明文，
    # 回显输入或把异常文本当 detail 就是把 spec 3 的 I2 交出去（实测见 task-6-report）。
    "invalid_nonce_field": ("invalid_nonce_field", 422),
    "invalid_value_field": ("invalid_value_field", 422),
    "invalid_decline_field": ("invalid_decline_field", 422),
    "invalid_approve_field": ("invalid_approve_field", 422),
    "invalid_decision_field": ("invalid_decision_field", 422),
    "value_too_long": ("value_too_long", 422),
    "invalid_json": ("invalid_json", 422),
    "invalid_body": ("invalid_body", 422),
    # 413/400 不在 brief 钉死的码集（200/403/404/410/422）里，所以这两条也并进 422：
    # 区分靠 detail 的固定串，客户端本来就渲染那个串。
    "body_too_large": ("body_too_large", 422),
    "unreadable_body": ("unreadable_body", 422),
}
# 被拒的访问要留痕，但路径里的 request id 不可猜、也不是秘密；截一下防超长 URL。
_LOG_PATH = 120
# 展示用文本的上界按"最坏转义"算：tools 侧上限 4000 个原始字符，一个字符最多展开成
# `\U000e0001` 这 10 个字符。够大就不会把任何被 tools 放行的命令截掉 —— 截掉尾部
# 等于让人批准一段没看见的内容。真超了（别的调用方绕过了 tools 的上限）也必须留痕。
_DISPLAY_ESCAPE_OVERHEAD = 10
_MAX_DISPLAY_CHARS = 4000 * _DISPLAY_ESCAPE_OVERHEAD


class AnswerBody(BaseModel):
    """Never log or echo `value`.

    字段一律 `Any`，刻意不用 pydantic 的强校验：FastAPI 的默认 422 响应会把 `input`
    原样回显（实测：`Content-Type: text/plain` 装 JSON、JSON 数组、JSON 字符串三种 body
    都把提交的值打进了响应体；二进制 body 更直接把 `RequestValidationError` 顶成
    `UnicodeDecodeError` 抛给宿主异常面），而 `value` 正是明文唯一的载体。所以这一层只做
    取字段与忽略多余键，形状判定全部在 `_check_answer_body`，回出去的 detail 是闭集里的
    固定串。
    """

    model_config = ConfigDict(extra="ignore")

    nonce: Any = None
    value: Any = None
    decline: Any = False
    approve: Any = False


def _peer(request: Request) -> str:
    return request.client.host if request.client else ""


def _code_for(reason: str) -> int:
    return _ANSWER_MAP.get(reason, ("invalid_nonce", 403))[1]


def _expires_in(rec: SecretRequest) -> float:
    """响应面上的剩余秒数，下界 0（修复轮 1 · R38）。

    `rec.seconds_left()` 现在自己夹了 0，但那是 records 层的内部口径，不是这条响应的契约：
    这个数是要画到答页/卡片倒计时上的，负数就是"已超时 -12 秒"这种假话，而且过期后从墓地
    读出来的终态卡走的正是这条路。响应面自己夹住，不依赖下层记得夹（测试用回负数的桩钉这条）。
    """
    return max(0.0, round(rec.seconds_left(), 1))


def _rejected(reason: str) -> JSONResponse:
    """固定 detail 的拒绝响应。绝不带 body 的任何一部分回去。"""
    return JSONResponse({"detail": reason}, status_code=_code_for(reason))


def _display_text(text: str) -> str:
    """把模型写的命令变成"看得到就等于执行得到"的展示串。

    命令要交给人来判断，所以**内容是原样给的**，只有控制类字符被显式转义：ESC/CSI 能
    清屏、改标题、伪造提示符（`\\x1b[2J` + 一段假的 shell 提示），bidi 覆盖（U+202E 等）
    能让 `exe.bat` 在屏幕上显示成 `tab.exe` —— 批准一段"看起来是 A、执行是 B"的命令正是
    这张卡要防的事。换行与制表符保留：它们是排版，不是伪装。
    """
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if ch in "\n\t":
            out.append(ch)
        elif unicodedata.category(ch) in ("Cc", "Cf", "Cs") or code in (0x2028, 0x2029):
            out.append(f"\\u{code:04x}" if code <= 0xFFFF else f"\\U{code:08x}")
        else:
            out.append(ch)
    shown = "".join(out)
    if len(shown) > _MAX_DISPLAY_CHARS:
        return shown[:_MAX_DISPLAY_CHARS] + "… (truncated)"
    return shown


def _confirmation_fields(rec: SecretRequest) -> dict[str, Any]:
    """确认卡的展示字段。原始 `command` 一个字节都不出这条面。"""
    return {
        "kind": rec.kind,
        "command_display": _display_text(rec.command),
        "cwd": _display_text(rec.cwd),
        "env_name": rec.env_name,
    }


def _allowed(request: Any, *, require_loopback: bool = False) -> bool:
    if require_loopback:
        # 省略 session_id 的全量清单是终端入口（spec 8.1 的 curl 兜底路径），
        # 它不按会话过滤，所以连 state.user 都不认 —— 只有本机 TCP 对端能看。
        return _peer(request) in _LOOPBACK
    if _peer(request) in _LOOPBACK:
        return True
    if getattr(request.state, "user", None) is not None:
        return True
    return bool(getattr(request.app.state, "secret_input_allow_remote", False))


def _answer_url(request: Request, request_id: str) -> str:
    # 按当前请求的 Host 头推导（spec 11）：远程控制台要拿到能点的链接。
    # public_base_url 只管 INFO 日志里那条 URL，属 T7/T9，不参与这里。
    return f"{request.url.scheme}://{request.url.netloc}/api/secret-input/card/{request_id}"


def build_router(*, registry: RequestRegistry, config: PluginConfig,
                 vault: Vault) -> APIRouter:
    router = APIRouter()

    def guard(request: Request, *, loopback_only: bool = False) -> Response | None:
        if config.allow_remote or _allowed(request, require_loopback=loopback_only):
            return None
        # 只记 peer 与截断过的路径：body 永不入日志，label/reason 也不入这一行。
        logger.warning("secret-input access denied: peer=%s path=%s",
                       _peer(request), request.url.path[:_LOG_PATH])
        return JSONResponse({"detail": "secret-input: loopback or authenticated caller required"},
                            status_code=403)

    @router.get("/pending")
    async def pending(request: Request, session_id: str | None = None) -> Response:
        # 省略 session_id 的清单是终端入口，只允许 loopback
        blocked = guard(request, loopback_only=session_id is None)
        if blocked is not None:
            return blocked
        registry.sweep()
        items = []
        for rec in registry.list_pending(session_id):
            item = {
                "request_id": rec.request_id,
                "kind": rec.kind,
                "label": rec.label,
                "reason": rec.reason,
                "placeholder": rec.placeholder,
                "expires_in": _expires_in(rec),
                "answer_url": _answer_url(request, rec.request_id),
            }
            if rec.kind != "secret":
                item.update(_confirmation_fields(rec))
            items.append(item)
        return {"requests": items}

    @router.get("/card/{request_id}")
    async def card(request: Request, request_id: str) -> Response:
        blocked = guard(request)
        if blocked is not None:
            return blocked
        page = html_response(request_id)
        page.headers.update(_NO_STORE)
        return page

    @router.get("/card/{request_id}/meta")
    async def meta(request: Request, request_id: str) -> Response:
        blocked = guard(request)
        if blocked is not None:
            return blocked
        # 答后的记录已经离开 _pending（_finish 是唯一迁移点），终态去墓地拿：卡片要显示
        # "已由 <peer> 于 <时刻>"，410 门要把 expired 与 already_answered 与 not_found 分开。
        rec = registry.get(request_id) or registry.find_record_any_state(request_id)
        if rec is None:
            return JSONResponse({"detail": "This secret request no longer exists."},
                                status_code=404)
        payload = {
            "kind": rec.kind,
            "label": rec.label,
            "reason": rec.reason,
            "placeholder": rec.placeholder,
            "status": rec.status,
            "expires_in": _expires_in(rec),
            "status_text": _status_text(rec.status),
        }
        if rec.kind != "secret":
            payload.update(_confirmation_fields(rec))
        if rec.status == "pending":
            payload["nonce"] = rec.nonce          # 全仓库唯一发 nonce 的响应面
        else:
            payload["answered_peer"] = rec.answered_peer
            payload["answered_at"] = _fmt_time(rec.answered_at)
        return JSONResponse(payload, headers=_NO_STORE)

    @router.post("/card/{request_id}/answer")
    async def answer(request: Request, request_id: str) -> Response:
        blocked = guard(request)
        if blocked is not None:
            return blocked
        payload, rejected = await _read_answer_body(request)
        if rejected is not None:
            return rejected
        try:
            body = AnswerBody.model_validate(payload)
        except Exception:                     # 容错模型理论上不抛；真抛了也只回固定串
            logger.warning("secret-input answer body unreadable: peer=%s", _peer(request))
            return _rejected("invalid_body")
        checked = _check_answer_body(body)
        if isinstance(checked, str):
            return _rejected(checked)
        nonce, value, decline, approve = checked
        if not decline and not approve and not str(value or ""):
            # 裁定 R20：value 为空且不是拒答/批准 ⇒ 端点前置 422，绝不进 registry.answer()。
            # 这一句也先于查表，所以它不透露这个 id 到底存不存在。批准不携带值，它走的是
            # registry 里确认卡那一支，由那里拒掉 value 与"两个决定都给"的形状。
            return _rejected("empty_value")
        ok, why = await registry.answer(
            request_id, nonce=nonce, value=value, decline=decline, approve=approve,
            peer=_peer(request), user_agent=request.headers.get("user-agent", ""),
        )
        if not ok:
            return _rejected(why)
        rec = registry.find_record_any_state(request_id)
        status = rec.status if rec is not None else "saved"
        if status == "saved":
            vault.sweep()
        # 只回终结状态，绝不回值、绝不回 ref
        return {"status": status}

    return router


async def _read_answer_body(request: Request) -> tuple[dict | None, Response | None]:
    """把 body 读成一个 dict，读不出来就回一个固定 detail 的拒绝响应。

    刻意不借 FastAPI 的 body 校验（理由见 `AnswerBody`）：`/answer` 是明文唯一经过的通道，
    框架的 422 会把提交的内容回显出去。`Content-Type` 不参与判定、一律按 JSON 读 ——
    这道面的凭据是 nonce 与 loopback，不是请求头（spec 10.2 第 6 条已经把"同机 agent
    能抢答"列为不防）。
    """
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > _MAX_ANSWER_BYTES:
        return None, _rejected("body_too_large")     # 先拒再读，不为一个 4 KB 的框收 64 KB
    try:
        # 没有 Content-Length（chunked 传输）时上面那刀砍不到，而 `await request.body()`
        # 会**先把整个 body 缓冲完**才轮到后面的字符数检查 —— 一个几 MB 的提交就能白占内存，
        # 且这条面对"已认证的远程调用者"也开着（`_allowed` 的 `state.user` 那一支）。
        # 所以改成边读边封顶：超限立刻停读，回同一个固定 detail（终审 Minor 8）。
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > _MAX_ANSWER_BYTES:
                return None, _rejected("body_too_large")
            chunks.append(chunk)
        raw = b"".join(chunks)
    except Exception:
        # 传输层怎么失败不由这一层决定（断连/超时/解码…）。任何一种都只回固定串，
        # 并且既不把 body 也不把异常文本写进响应或日志。
        return None, _rejected("unreadable_body")
    try:
        # replace 而不是 strict：非 UTF-8 字节先变成 U+FFFD，再让 json.loads 判它不成话，
        # 于是得到 invalid_json —— 而不是一个把原始字节顶到宿主异常面的 UnicodeDecodeError。
        payload = json.loads(raw.decode("utf-8", "replace") or "null")
    except ValueError:
        return None, _rejected("invalid_json")
    if not isinstance(payload, dict):
        return None, _rejected("invalid_body")
    return payload, None


def _check_answer_body(body: AnswerBody) -> tuple[str, str | None, bool, bool] | str:
    """返回 `(nonce, value, decline, approve)`，或闭集里的一个 422 reason。

    比 registry 更严是故意的：类型不对就 422，绝不把 int/list 丢给 registry 的
    `str(value or "")` 去"洗"成一段字符串 secret（那等于把 JSON 文本当密钥存进 vault）。
    `decline`/`approve` 同理不许用 int 1 蒙混 —— 它们决定这张卡是 saved、declined 还是
    approved，而 approved 会让一个 ref 被花掉。
    """
    nonce = body.nonce
    if not isinstance(nonce, str) or not nonce or len(nonce) > _MAX_NONCE_CHARS:
        return "invalid_nonce_field"
    decline = body.decline
    if decline is None:
        decline = False
    if not isinstance(decline, bool):
        return "invalid_decline_field"
    approve = body.approve
    if approve is None:
        approve = False
    if not isinstance(approve, bool):
        return "invalid_approve_field"
    value = body.value
    if value is not None:
        if not isinstance(value, str):
            return "invalid_value_field"
        if len(value) > _MAX_VALUE_CHARS:
            return "value_too_long"
    return (nonce, value, decline, approve)


def _status_text(status: str) -> str:
    return {
        "pending": "等待输入", "saved": "已保存", "declined": "已拒绝",
        "approved": "已批准", "cancelled": "已取消", "timed_out": "已超时",
    }.get(status, status)


def _fmt_time(epoch: float | None) -> str:
    if not epoch:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))
