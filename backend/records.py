# -*- coding: utf-8 -*-
"""Pending secret requests: state machine, futures, idempotency, GC.

单循环前提（裁定 R14）：这里每一步状态迁移都不 await，`_finish` 是唯一让记录离开
`pending` 并结算 future 的地方 ⇒ 一个 future 只可能被结算一次，`answer` 与
`cancel`/`supersede` 之间没有插入窗口。别把这些调用挪进 to_thread/线程池。
记录上、outcome 里永远没有明文：只有 vault 的 ref 与答者审计三元组。
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 只为注解存在，运行时不给 records 加一条对 vault 的 import 边
    from .vault import Vault

logger = logging.getLogger(__name__)

_MIN_TIMEOUT = 30
_MAX_TIMEOUT = 3600
# 只装不可猜的 id（uuid4 hex），不装值、label、nonce。模块级：与计划的 _EXPIRED_MARKS 同构。
_EXPIRED_MARKS: set[str] = set()
_MARKS_CAP = 1024
# 幂等表里"已答待重放"条目的上限（与 T6 墓地同量级）；只逐出已终结的最老条目。
_MAX_REPLAY = 256


@dataclass(repr=False)
class SecretRequest:
    request_id: str
    nonce: str
    label: str
    reason: str
    placeholder: str
    session_id: str
    agent_id: str
    user_id: str
    channel: str
    client_request_id: str | None
    created_at: float
    expires_at: float
    status: str
    future: asyncio.Future
    ref: str | None = None
    answered_peer: str | None = None
    answered_at: float | None = None
    answered_ua: str | None = None
    cancel_reason: str | None = None

    def seconds_left(self) -> float:
        return max(self.expires_at - time.monotonic(), 0.0)

    def __repr__(self) -> str:
        # 默认的 dataclass repr 会把 nonce（答这张卡的凭据）打进日志、断言文本和任何
        # `"%s" % record` 里。这里只留答不了卡的信息：截断 id、状态、会话、有无 ref。
        return (
            f"SecretRequest(request_id={self.request_id[:8]!r} status={self.status!r} "
            f"session={self.session_id!r} has_ref={self.ref is not None} "
            f"left={self.seconds_left():.0f}s)"
        )


class RequestRegistry:
    def __init__(self, vault: Vault, *, timeout_seconds: int = 600, max_pending: int = 32) -> None:
        self._vault = vault
        self._default_timeout = min(max(int(timeout_seconds), _MIN_TIMEOUT), _MAX_TIMEOUT)
        self._max_pending = max(int(max_pending), 1)
        self._pending: dict[str, SecretRequest] = {}
        self._by_client_id: dict[str, SecretRequest] = {}
        # 答过/拒答/被取消的 request_id：用来把"这张卡答过了"与"这个 id 从没存在过"分开
        # （契约里的 already_answered，T6 把它映射成 410 而不是 404）。只存 id，不存记录本身。
        self._settled_ids: set[str] = set()

    async def create(self, *, label: str, reason: str, placeholder: str, session_id: str,
                     agent_id: str, user_id: str, channel: str,
                     client_request_id: str | None = None,
                     timeout_seconds: int | None = None) -> SecretRequest:
        if client_request_id:
            reuse = self._by_client_id.get(client_request_id)
            if reuse is not None:
                return reuse

        timeout = self._default_timeout
        if timeout_seconds is not None:
            # 坏值与越界同一个口径：都必须是 ValueError，因为 T7 只把 ValueError 翻成
            # error 结果串；TypeError 会绕到它的兜底 except，模型拿到的是没用的话术。
            try:
                timeout = int(timeout_seconds)
            except (TypeError, ValueError):
                raise ValueError(
                    f"timeout_seconds must be an int in {_MIN_TIMEOUT}..{_MAX_TIMEOUT}, "
                    f"got {type(timeout_seconds).__name__}"
                ) from None
            if timeout < _MIN_TIMEOUT or timeout > _MAX_TIMEOUT:
                raise ValueError(
                    f"timeout_seconds must be in {_MIN_TIMEOUT}..{_MAX_TIMEOUT}, got {timeout}"
                )

        loop = asyncio.get_running_loop()
        now = time.monotonic()
        request = SecretRequest(
            request_id=uuid.uuid4().hex,
            nonce=secrets.token_urlsafe(24),
            label=str(label).strip(),
            reason=str(reason).strip(),
            placeholder=str(placeholder or "").strip(),
            session_id=session_id, agent_id=agent_id, user_id=user_id, channel=channel,
            client_request_id=client_request_id or None,
            created_at=now, expires_at=now + timeout,
            status="pending", future=loop.create_future(),
        )
        self._pending[request.request_id] = request
        if request.client_request_id:
            self._by_client_id[request.client_request_id] = request
        self._enforce_capacity(newest=request)
        self._vault.sweep()
        # 只记不可猜的 id 与会话，绝不记 label/reason 之外更敏感的东西（它们由模型写）
        logger.info(
            "secret-input request %s pending (session=%s label=%r timeout=%.0fs)",
            request.request_id[:8], str(session_id)[:8], request.label, timeout,
        )
        return request

    def _enforce_capacity(self, newest: SecretRequest) -> None:
        while len(self._pending) > self._max_pending:
            oldest = next(iter(self._pending))
            if oldest == newest.request_id:
                break
            # 同步逐出，不用 ensure_future(cancel(...))：挂起的任务要等下一轮 loop 才跑，
            # create 返回后立刻读 list_pending()/future 的调用方（T6 的 /pending、T7 的
            # _await 首轮轮询）会看到"还没被 supersede"的瞬时态。
            self._cancel_now(oldest, "superseded")
            break  # one supersession per creation keeps the newest always alive

    def get(self, request_id: str) -> SecretRequest | None:
        return self._pending.get(request_id)

    def find_by_client_request_id(self, client_request_id: str) -> SecretRequest | None:
        return self._by_client_id.get(client_request_id)

    def list_pending(self, session_id: str | None = None) -> list[SecretRequest]:
        items = [r for r in self._pending.values() if r.status == "pending"]
        if session_id is not None:
            items = [r for r in items if r.session_id == session_id]
        return items

    async def answer(self, request_id: str, *, nonce: str, value: str | None = None,
                     decline: bool = False, peer: str = "",
                     user_agent: str = "") -> tuple[bool, str]:
        # request_id / nonce 都来自网络输入，怪形状只许得到 not_found / invalid_nonce，
        # 不许把 TypeError 抛给端点（全局约束：这一层绝不因畸形输入而抛）。
        request_id = str(request_id or "")
        request = self._pending.get(request_id)
        if request is None:
            if request_id in self._settled_ids:
                return False, "already_answered"
            return False, ("expired" if request_id in _EXPIRED_MARKS else "not_found")
        # compare_digest 收到含非 ASCII 的 str 会 TypeError；nonce 是 HTTP body 里的字段，
        # 所以两边都先 utf-8 编码成 bytes（errors=replace 连孤立代理项也不会抛）。
        guess = str(nonce or "").encode("utf-8", "replace")
        if not hmac.compare_digest(guess, request.nonce.encode("utf-8")):
            return False, "invalid_nonce"
        if request.status != "pending":
            # 纵深防御：_pending 里本只该有 pending 态；万一以后有任务把已结算记录留在
            # 表里，这条保证同一张卡不会被二次结算。
            return False, "already_answered"
        if request.seconds_left() <= 0:
            self._finish(request, "timed_out")
            return False, "expired"

        if decline:
            self._finish(request, "declined")
            return True, ""

        secret = str(value or "")
        if not secret:
            return False, "invalid_nonce"  # 空值按未授权处理，不给"存了什么"的信号
        request.ref = self._vault.issue(secret)
        request.answered_peer = peer or None
        request.answered_at = time.time()
        request.answered_ua = (user_agent or "")[:200] or None
        self._finish(request, "saved", ref=request.ref)
        return True, ""

    async def cancel(self, request_id: str, reason: str) -> bool:
        return self._cancel_now(request_id, reason)

    def _cancel_now(self, request_id: str, reason: str) -> bool:
        request = self._pending.get(str(request_id or ""))
        if request is None or request.status != "pending":
            return False
        request.cancel_reason = reason
        self._finish(request, "cancelled", reason=reason)
        return True

    def _finish(self, request: SecretRequest, status: str, **extra: Any) -> None:
        """状态迁移与 future 结算的唯一出口：status 与 outcome 同源，不会各写一半。"""
        request.status = status
        self._pending.pop(request.request_id, None)
        if request.client_request_id:
            if status == "saved":
                # 已答未消费：幂等表要留着这条，重放同一 client_request_id 才拿得到
                # 同一个 ref（T7 的"结果丢了就重放"依赖它）。其余终态必须清，否则
                # 一张 decline/cancel/timed_out 的死卡会被永远复用、再也问不出新的。
                self._by_client_id[request.client_request_id] = request
                self._trim_replay_cache()
            else:
                self._by_client_id.pop(request.client_request_id, None)
        if status == "timed_out":
            _EXPIRED_MARKS.add(request.request_id)
            while len(_EXPIRED_MARKS) > _MARKS_CAP:
                _EXPIRED_MARKS.pop()
        else:
            self._settled_ids.add(request.request_id)
            while len(self._settled_ids) > _MARKS_CAP:
                self._settled_ids.pop()
        outcome: dict[str, Any] = {"status": status, **extra}
        if status == "saved" and request.ref:
            outcome["ref_expires_in"] = getattr(self._vault, "_ttl", 300)
        if not request.future.done():
            request.future.set_result(outcome)
        logger.info("secret-input request %s -> %s", request.request_id[:8], status)

    def _trim_replay_cache(self) -> None:
        while len(self._by_client_id) > _MAX_REPLAY:
            for client_id, record in self._by_client_id.items():
                if record.status != "pending":
                    self._by_client_id.pop(client_id, None)
                    break
            else:
                return

    def sweep(self) -> int:
        now = time.monotonic()
        stale = [r for r in self._pending.values() if r.expires_at <= now][:256]
        for request in stale:
            self._finish(request, "timed_out")
        return len(stale)
