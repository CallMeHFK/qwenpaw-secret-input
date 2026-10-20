# -*- coding: utf-8 -*-
"""Pending secret requests: state machine, futures, idempotency, GC.

单循环前提（裁定 R14）：这里每一步状态迁移都不 await，`_finish` 是唯一让记录离开
`pending` 并结算 future 的地方 ⇒ 一个 future 只可能被结算一次，`answer` 与
`cancel`/`supersede` 之间没有插入窗口。别把这些调用挪进 to_thread/线程池。
记录上、outcome 里永远没有明文：只有 vault 的 ref 与答者审计三元组。
幂等键是 `(session_id, client_request_id)` 并且再加一道 agent 守卫：`client_request_id`
是模型自选文本，一张卡绝不负给第二个会话或第二个 agent（spec 7.1 会话归属、10.2 第 6 条）。
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
# 闭集（spec 7.1 的取消原因）：这一层是唯一产出者，调用方写错一个字符就当场 ValueError。
_CANCEL_REASONS: tuple[str, ...] = ("run_ended", "superseded", "shutdown")
# 墓地容量（裁定 R36）：只装已终结的记录对象本身，不装值。逐出最老的后果是措辞精度
# （那条 id 从 already_answered 退化成 not_found），不是安全漏洞 —— 记录上根本没有值。
_GRAVEYARD_CAP = 256
# 幂等表里"已答待重放"条目的上限（与墓地同量级）；只逐出已终结的最老条目。
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
        # 键是 (session_id, client_request_id)：见 _replay_key，一条 cid 只在它自己的会话里有效。
        self._by_client_id: dict[tuple[str, str], SecretRequest] = {}
        # 已终结的记录（答过/拒答/被取消/超时）留一份引用：`_pending.pop` 之后，答后的
        # meta 还要读终态与答者三元组，`answer` 还要把"这张卡答过了"与"这个 id 从没存在过"
        # 分开（T6 映射成 410 与 404）。这一本字典是"已终结"的唯一来源（裁定 R36）—— T3 的
        # 临时 `_settled_ids` 与模块级 `_EXPIRED_MARKS` 已由它替换，不要并存，否则审计显示
        # 和 410 门迟早各说各话。存的是记录对象引用，记录上没有值，只有 ref。
        self._graveyard: dict[str, SecretRequest] = {}
        # 非消费读的入口（Task 7 的 scrub 登记要它）。取的是 `getattr` 而不是直接
        # `vault.peek`：装配方给的可能是一个没有 peek 的桩，那时这条通道安静地读不到值，
        # 擦洗少登记一条，而请求流程照跑 —— 这一层不为上层的需求去假设下层的形状。
        self._vault_peek = getattr(vault, "peek", None) or (lambda _ref: None)

    @staticmethod
    def _replay_key(session_id: Any, client_request_id: Any) -> tuple[str, str]:
        """幂等/重放查表的键。

        `client_request_id` 由模型自己挑（`request_secret` 没有命名空间），`retry-1`、
        某个 label 的 slug 这类默认值跨会话相撞完全在预料之内。裸 cid 当键的话，B 会话
        一次重试就会拿到 A 的记录对象：A 的卡还 pending ⇒ B 挂在 A 的卡上等 A 的人类作答，
        A 的卡已经 saved ⇒ B 当场拿到 A 的 ref 并把 A 的用户为另一个会话提供的值消费掉
        （spec 7.1「会话归属」、10.2 第 6 条划的边界）。键里带上会话是结构性的修法。
        """
        return (str(session_id or ""), str(client_request_id or ""))

    async def create(self, *, label: str, reason: str, placeholder: str, session_id: str,
                     agent_id: str, user_id: str, channel: str,
                     client_request_id: str | None = None,
                     timeout_seconds: int | None = None) -> SecretRequest:
        if client_request_id:
            reuse = self._by_client_id.get(self._replay_key(session_id, client_request_id))
            # 一张卡绝不服务两个 agent：同会话里换了 agent_id 也算新请求，重发一张，
            # 否则前一个 agent 的人类答案会被原样交给后一个 agent。
            if reuse is not None and str(reuse.agent_id or "") == str(agent_id or ""):
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
            self._by_client_id[self._replay_key(session_id, request.client_request_id)] = request
        self._enforce_capacity(newest=request)
        self._vault.sweep()
        # 只记不可猜的 id、会话与超时。**label/reason 不进日志**：它们是模型写的文本
        # （spec 10.2 第 4 条），一个短密钥可以整段塞进 label，120 字符截断挡不住它。
        # T9/I1 的 test_3 用"label 里放 DUMMY"实测过这条通道；日志里留下的是
        # `pending` + id + 超时，卡片文案仍走 request.label 这条合法出口。
        logger.info(
            "secret-input request %s pending (session=%s timeout=%.0fs)",
            request.request_id[:8], str(session_id)[:8], timeout,
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

    def find_by_client_request_id(self, client_request_id: str,
                                  session_id: str | None = None) -> SecretRequest | None:
        """重放查表：按 (会话, cid) 分键（裁定 R15）。

        不传 `session_id` 只会命中"无会话"那一格，跨会话命中在结构上不可能 —— T6/T7 想
        重放就得把发起侧的 session_id 一起传进来，别指望这张表替它们猜会话。
        """
        return self._by_client_id.get(self._replay_key(session_id, client_request_id))

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
            # 终态查表只有一个来源（裁定 R36）：墓地记录自己的 status 决定这一支回 expired
            # 还是 already_answered —— T6 靠它把 410 分成两种、把从没见过的 id 回 404。
            finished = self._graveyard.get(request_id)
            if finished is None:
                return False, "not_found"
            if finished.status == "timed_out":
                return False, "expired"
            return False, "already_answered"
        # compare_digest 收到含非 ASCII 的 str 会 TypeError；nonce 是 HTTP body 里的字段，
        # 所以两边都先 utf-8 编码成 bytes（errors=replace 连孤立代理项也不会抛）。
        guess = str(nonce or "").encode("utf-8", "replace")
        if not hmac.compare_digest(guess, request.nonce.encode("utf-8")):
            return False, "invalid_nonce"
        if request.status != "pending":
            # 纵深防御。不变式：`_pending ⇒ status == pending`（_finish 是唯一迁移点，
            # 迁移时先把记录摘出 _pending），所以这条当前不可达；留着是因为它守的是
            # "同一张卡不被二次结算"这条安全不变式，不是为了让返回值多一种可能。
            return False, "already_answered"
        if request.seconds_left() <= 0:
            self._finish(request, "timed_out")
            return False, "expired"

        if decline:
            # 拒答同样要落答者三元组（Important-1 / R16）：spec 8.2 要求三元组落在
            # **每一条**已答记录上，而"脚本化拒答是不是别人替按的"正是 10.2 第 6 条
            # 要让人类看见的那件事；不记的话 T6 的"已由 <peer> 于 <时刻> 提交"对拒答
            # 卡片永远显示空白。
            self._stamp_answer(request, peer, user_agent)
            self._finish(request, "declined")
            return True, ""

        secret = str(value or "")
        if not secret:
            # 空值按未授权处理，不给"存了什么"的信号（reason 是 spec 的闭集，不改串）。
            # 给 T6 的路由：请在端点先校验 value 非空并回 422，别让用户点"保存"空框时
            # 看到"invalid_nonce"这种误导性文案（裁定 R20）。
            return False, "invalid_nonce"
        request.ref = self._vault.issue(secret)
        self._stamp_answer(request, peer, user_agent)
        self._finish(request, "saved", ref=request.ref)
        return True, ""

    @staticmethod
    def _stamp_answer(request: SecretRequest, peer: str, user_agent: str) -> None:
        """答者审计三元组（spec 8.2）：saved 与 declined 共用同一段，谁答的都留痕。不含值。"""
        request.answered_peer = peer or None
        request.answered_at = time.time()
        request.answered_ua = (str(user_agent or ""))[:200] or None

    async def cancel(self, request_id: str, reason: str) -> bool:
        return self._cancel_now(request_id, reason)

    def _cancel_now(self, request_id: str, reason: str) -> bool:
        # reason 是闭集（run_ended/superseded/shutdown），而这一层是唯一产出它的地方：
        # T7 传 "runEnd" 这种拼错的字串会原样变成模型可见的状态字段，越往上传越查不动。
        # 调用方都是内部代码，写错就是编程错误 ⇒ 在产出处当场 ValueError，不静默降级。
        if reason not in _CANCEL_REASONS:
            raise ValueError(
                f"cancel reason must be one of {', '.join(_CANCEL_REASONS)}, got {reason!r}"
            )
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
            key = self._replay_key(request.session_id, request.client_request_id)
            if status == "saved":
                # 已答未消费：幂等表要留着这条，重放同一 client_request_id 才拿得到
                # 同一个 ref（T7 的"结果丢了就重放"依赖它）。其余终态必须清，否则
                # 一张 decline/cancel/timed_out 的死卡会被永远复用、再也问不出新的。
                self._by_client_id[key] = request
                self._trim_replay_cache()
            else:
                self._by_client_id.pop(key, None)
        outcome: dict[str, Any] = {"status": status, **extra}
        if status == "saved" and request.ref:
            outcome["ref_expires_in"] = getattr(self._vault, "_ttl", 300)
        if not request.future.done():
            request.future.set_result(outcome)
        # 答者审计也要进日志（spec 8.2 末句"日志里同样只记这三项"，裁定 R18）：只记
        # peer 与时刻，UA 刻意不记（10.2 第 6 条只承诺 peer + 时间戳，UA 是浏览器字符串，
        # 属于模型/调用方可控文本），值永远不进日志。
        if request.answered_peer is not None or request.answered_at is not None:
            logger.info("secret-input request %s -> %s (answered by=%s answered_at=%s)",
                        request.request_id[:8], status,
                        request.answered_peer, request.answered_at)
        else:
            logger.info("secret-input request %s -> %s", request.request_id[:8], status)
        # 墓地落一份引用（T6 补，裁定 R36）：`_finish` 是唯一的迁移点，所以这里也是唯一的
        # 入库点 ⇒ "已终结"与 `_pending` 的移除严格同刻，不存在两个来源。存的是对象引用，
        # 不是副本（副本会让答者三元组出现两份真相）。有界：只留最近 _GRAVEYARD_CAP 条，
        # 越界逐最老的 —— 代价是那条 id 之后回 not_found(404) 而不是 already_answered(410)，
        # 拿到的是措辞精度，不是值：记录上从来没有明文。
        self._graveyard[request.request_id] = request
        while len(self._graveyard) > _GRAVEYARD_CAP:
            self._graveyard.pop(next(iter(self._graveyard)))

    def find_record_any_state(self, request_id: str) -> SecretRequest | None:
        """Finished requests are unregistered but the answer handler still
        needs their terminal status to report back."""
        return self._graveyard.get(str(request_id or ""))

    def peek_value(self, ref: str | None) -> str | None:
        """Non-consuming read for the scrubber only."""
        # 这条通道**不是** spec 3 I2 说的那个面：它不给模型、不给端点、不进任何返回值，
        # 只给 Task 7 内部那一句 `scrubber.register(value)`（I4 要知道"哪些明文已经出站过"）。
        # 读它不消费（I3 的消耗只属于 secret_exec/secret_export），也绝不返回给调用方之外。
        if not ref:
            return None
        return self._vault_peek(ref)

    def _trim_replay_cache(self) -> None:
        while len(self._by_client_id) > _MAX_REPLAY:
            for key, record in self._by_client_id.items():
                if record.status != "pending":
                    self._by_client_id.pop(key, None)
                    break
            else:
                return

    def sweep(self) -> int:
        now = time.monotonic()
        stale = [r for r in self._pending.values() if r.expires_at <= now][:256]
        for request in stale:
            self._finish(request, "timed_out")
        return len(stale)
