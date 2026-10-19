# -*- coding: utf-8 -*-
"""Tool wrappers. They always return a JSON string and never raise.

`request_secret` is the only tool in this plugin that blocks on a human, so it
owns four exit paths that the plan's status table does not spell out and the
tests do: the deadline it applies by itself (nobody else swept the record), the
cancellation that must reach a terminal state before we return, the idempotent
replay of a request whose ref may already be spent, and the cleanup that has to
happen when something *else* raises mid-poll (a tool call that ended with a card
still live is a card a human can still answer).

Two boundaries this module must keep (spec section 7.1, rulings R15/R41):

* Identity comes from ``identity.py`` **only** — injected here as callables, so
  unit tests need no booted host and ``tools.py`` never imports ``qwenpaw``. That
  "never" is a gated constraint now, not a comment: ``tests/test_secret_store_gate.py``
  (R32, widened to this module in Task 7 review I-1) reads this file's import surface.
  The real ``session_id``/``agent_id`` go into ``registry.create``: ``client_request_id``
  is model-chosen text with no namespace, and a bare-cid key let one session
  consume another session's ref.
* The result string carries no URL, no peer, no nonce and no value. The answer-page
  URL exists in exactly one place, the INFO log (spec section 8.3 layer 2); a
  model-visible URL would be a clickable entry point (spec section 10.2 item 4).
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from typing import Any, Callable

from .records import RequestRegistry
from .scrub import Scrubber
from .vault import Vault

logger = logging.getLogger(__name__)

# 轮询步长。spec 15 A6 在真机上核对的就是"点 Stop 之后最迟一个 tick 工具就返回"，
# 所以这个数同时是取消延迟上限；改成 asyncio.wait 三方竞速会把 A6 变成"看不见的竞态"
# （理由见 _await 的 docstring）。
_POLL_SECONDS = 0.2
# 监听口没有进程内可读的 API（宿主在 `qwenpaw app --port` 那里定口，plugins/api.py 不往
# 外露），所以默认取 spec 5 实测的 19999；换口的部署用 config.public_base_url 覆盖
# （spec 11 写明这个键**只**影响日志里这条 URL，答页响应里的 answer_url 按 Host 头推导）。
_DEFAULT_BASE_URL = "http://127.0.0.1:19999"
_CARD_PATH = "/api/secret-input/card/"
# 我们自己的校验消息回给模型时的长度上界（见下面 ValueError 分支的注释）。
_MAX_MESSAGE_CHARS = 200

# 闭集文案（spec 7.1 的返回表 + 参照实现的两句）。模型会照字面行事，
# 所以每条都得说清"下一步做什么"，而且不许出现 URL、端口、对端地址。
_HINT_SAVED = "Pass ref to secret_exec or secret_export. You never see the value."
_HINT_DECLINED = "The user chose not to provide it. Do not ask again for the same thing."
_HINT_TIMED_OUT = ("The card is closed. Ask again with request_secret using a "
                   "NEW client_request_id if it is still needed.")
# 重放撞上"已经消费过/已过期"。必须点名 NEW client_request_id：幂等表里那条 saved
# 记录不会被这条重放清掉（Task 3 的 _finish 是这么设计的），同一个 cid 只会再拿到
# 一次 ref_unavailable —— 不写 NEW，模型就永远在同一个死键上重试。
_HINT_REF_UNAVAILABLE = ("That secretRef was already used or no longer exists. Ask the user "
                         "again with request_secret using a NEW client_request_id.")


def _text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _failure_detail(exc: BaseException) -> str:
    """定位失败而不渲染任何可能带值的东西（裁定 R43，Task 4/5 的 R34 先例）。

    只取 ``traceback.extract_tb`` 的 filename/lineno/name：never ``str(exc)``、
    never 帧的源码行、never 局部变量。宿主或底层那句原话可能正带着它咬到的字符，
    而 `logger.exception` 会把整段 exc_text 打进日志 —— 那是 spec 10.1 明确排除的通道。
    """
    try:
        frames = traceback.extract_tb(exc.__traceback__)
        deepest = frames[-1] if frames else None
        site = "unknown" if deepest is None else (
            f"{deepest.filename}:{deepest.lineno} in {deepest.name}")
        return f"exc={type(exc).__name__} site={site} tool=request_secret"
    except Exception:  # noqa: BLE001 - 记不住现场也不能把异常顶给宿主
        return f"exc={type(exc).__name__} site=unknown tool=request_secret"


class SecretTools:
    def __init__(self, *, registry: RequestRegistry, vault: Vault, scrubber: Scrubber,
                 config, identity: Callable[[], dict[str, str]],
                 cancel_event: Callable[[], asyncio.Event | None],
                 env_writer: Callable[[str, str], Any] | None = None,
                 project_dir: Callable[[], str] | None = None) -> None:
        self._registry = registry
        self._vault = vault
        self._scrubber = scrubber
        self._config = config
        self._identity = identity
        self._cancel_event = cancel_event
        self._env_writer = env_writer
        self._project_dir = project_dir

    async def request_secret(self, label: str, reason: str, placeholder: str = "",
                             timeout_seconds: int = 600,
                             client_request_id: str = "") -> str:
        try:
            if not str(label).strip() or not str(reason).strip():
                return _text({"status": "error",
                              "message": "label and reason are both required (non-empty)."})
            who = self._identity() or {}
            try:
                request = await self._registry.create(
                    label=label, reason=reason, placeholder=placeholder,
                    session_id=who.get("session_id", ""), agent_id=who.get("agent_id", ""),
                    user_id=who.get("user_id", ""), channel=who.get("channel", ""),
                    client_request_id=(client_request_id or None),
                    timeout_seconds=timeout_seconds,
                )
            except ValueError as exc:
                # 这一条是唯一允许把 str(exc) 交回模型的路径，理由要说清：这个 ValueError
                # 是 records 自己校验出来的固定串（"timeout_seconds must be in 30..3600,
                # got <int|类型名>"、取消原因闭集），里面没有外来文本；R43 禁的是宿主/子系统
                # 抛上来的消息，那两类只能走最外层的兜底分支（只报类名）。截一下防长串。
                return _text({"status": "error", "message": str(exc)[:_MAX_MESSAGE_CHARS]})

            # 只有 request_id 进这一行，nonce 不进（spec 7.1 末句）。答页 URL 只出现在日志里。
            logger.info(
                "secret-input: answer page for request %s -> %s",
                request.request_id,
                (self._config.public_base_url or _DEFAULT_BASE_URL).rstrip("/")
                + _CARD_PATH + request.request_id,
            )
            if not str(who.get("session_id", "")).strip():
                # 裁定 R41：空 session_id 就是"没有会话"（工具在会话之外被调用）。卡照样建 ——
                # 拒绝服务不是这一层的权力 —— 但必须告诉操作者：会话内那张卡片按会话过滤，
                # 列不出这条，剩下的发现路只有 loopback 上的裸 `/pending`（spec 8.1 的 curl 兜底）。
                # 这一行不含 label/reason（模型写的文本，R19），更不含值。
                logger.warning(
                    "secret-input: request %s has no session context, so the in-session card "
                    "cannot list it; use the loopback bare /pending list or the answer-page URL",
                    request.request_id)
            # create() 交回一条已终结的记录，只可能是幂等复用（新建的一律 pending）。
            replayed = request.status != "pending"
            outcome = await self._await(request)
            if outcome.get("status") == "saved":
                # 非消费读：登记给 scrub 是 I4 的钩子，消费是 Task 8 那两个工具的权力（I3）。
                value = self._registry.peek_value(request.ref) if request.ref else None
                if value is not None:
                    self._scrubber.register(value)
                elif replayed:
                    # 重放一个已 saved 的请求时，ref 可能已经被消费掉（I3）或过了 TTL；
                    # 这时如实回 ref_unavailable，而不是把模型早已拿过一次的 ref 再报一遍
                    # "saved"。fresh 路径不降级：peek 拿不到值只可能是"这一层的 vault 没有
                    # peek"（桩），把人类刚给的答案说成用掉过是假话（用例钉住这处不对称）。
                    outcome = {"status": "ref_unavailable", "hint": _HINT_REF_UNAVAILABLE}
            return _text(outcome)
        except Exception as exc:  # noqa: BLE001 - 刻意：绝不把异常抛给宿主
            # CancelledError 是 BaseException，不会被这一支吞掉：宿主取消我们时它照原样
            # 往上走，而记录的终态由 `_await` 的 CancelledError 分支负责（那里有理由）。
            logger.error("secret-input: request_secret failed %s", _failure_detail(exc))
            return _text({"status": "error",
                          "message": f"secret request failed: {type(exc).__name__}. "
                                     f"Retry once, then tell the user to open the console."})

    async def _cancel_quietly(self, request_id: str) -> None:
        """尽力把卡推到终态，且**绝不**因为自己又抛一个异常而顶掉原始失败（评审 I-3）。

        调用点在 `_await` 的兜底 except 里：那里已经有一个异常正要上抛、`request_secret`
        会把它翻成 `{"status":"error"}`。若这一步再抛，模型看到的错误就换成了清理路径的
        错误，而日志里丢的是真正坏掉的那一层。消息只取类名（裁定 R43）。
        """
        try:
            await self._registry.cancel(request_id, "run_ended")
        except Exception as exc:  # noqa: BLE001 - 清账失败不能变成第二次失败
            logger.error("secret-input: request %s could not be settled after a failure "
                         "(exc=%s)", request_id, type(exc).__name__)

    async def _await(self, request) -> dict[str, Any]:
        """Race the human's answer against tool cancellation and the deadline.

        轮询而不是 `asyncio.wait`（本任务刻意不换，spec 15 A6 依赖这个形状）：取消信号与
        记录截止都是**可直接观测的状态**（`cancel_event.is_set()`、`expires_at`），读它们
        不必为 `Event.wait()` 造一个任务再取消它 —— 那会要求一条可重入的取消路径，而
        `_supervise` 的取消语义正是 A6 要在真机上确认"Stop 真的到了我们手里"的那件事。
        代价是最多 `_POLL_SECONDS` 的返回延迟，换来的是三个正常出口都有确定终态、异常出口
        也先把卡结掉再上抛（I-3）、且没有"future 已结算但没人收"的悬挂任务。
        """
        try:
            # 缝的读数放在 try 里面（评审 I-3）：`cancel_event` 由装配方注入，它回什么形状
            # 不是这一层能保证的（今天回一个没有 is_set() 的对象、明天自己抛），而任何
            # 非取消的异常都绕过下面那两支清理 —— 这次工具调用已经结束了，卡不能还活着。
            cancel = self._cancel_event()
            while True:
                if request.future.done():
                    break
                if cancel is not None and cancel.is_set():
                    # 本轮结束/被 Stop 掐掉：先把记录推到终态再返回，否则答页会挂着一张
                    # 永远答不了的卡（spec 15 A6 的"若不成立"分支担心的就是这件事）。
                    await self._registry.cancel(request.request_id, "run_ended")
                    break
                if request.status == "pending" and request.seconds_left() <= 0:
                    # 没有别的人来 sweep（GC 是惰性的，spec 9），这张卡自己负责让它终态。
                    swept = self._registry.sweep()
                    # 裁定 R42 / Task 4 的 D1：观察者是读时回收的，`0` 不代表"没有东西过期"，
                    # 所以这里只说"扫了 N 条"，绝不说"N 条已过期"。
                    logger.info("secret-input: swept %d pending request(s)", swept)
                    break
                await asyncio.sleep(_POLL_SECONDS)
        except asyncio.CancelledError:
            # 宿主取消的两段形状（qwenpaw 2.2.2 `tool_calls/_coordinator.py`）：
            # `cancel_grace = 5.0` 在 :69/:73 定死；`cancel()` 的非强制支只 **set** Event
            # (:456)，`_handle_parent_cancel` (:221) 与 `shutdown()` (:630) 同样是先 set；
            # 之后 `_await_grace_or_force_cancel` (:946-960) 用 `asyncio.shield` 给任务一整段
            # 协作停机的宽限期，**宽限期跑完仍未返回**才轮到 `_apply_force_cancel` (:962-972)
            # 硬 `task.cancel()`（强制路径 :451 的那句注释说的就是这件事的顺序）。
            # ⇒ 上面的 Event 轮询是**主出口**（0.2 s 一个 tick，远早于 5 s 宽限期用完），
            # 这一支是**兜底**：硬取消（force=True）、宽限期耗尽、shutdown 收尾才会把
            # CancelledError 打进我们正 await 的 `sleep()`。兜底也必须清账：否则这张卡留在
            # `/pending` 里最长到 `expires_at`，人在一个已经结束的回合后面答题（A6 要排除的
            # 正是"挂着一个永远不结束的请求"）。清完原样上抛，绝不吞取消。
            # `registry.cancel()` 内部无 await（records.py 的迁移点全同步），所以这一步不会被
            # 第二次取消打断；调用它也不关心返回值：记录可能刚好已答完，那就不动。
            await self._registry.cancel(request.request_id, "run_ended")
            raise
        except Exception:
            # 兜底清账后原样上抛，交给 request_secret 的 error 支（异常没被吞 ⇒ ruff BLE001
            # 不适用，所以这里不需要 noqa；被吞的那两处才需要）。
            await self._cancel_quietly(request.request_id)
            raise
        if request.future.done():
            outcome = dict(request.future.result())
        else:
            # 评审 M-1：这一支按不变式是不可达的（`_finish` 是唯一的迁移点，它总会结算
            # future），但过去它回的是 `{"status": request.status}` —— 不变式一旦破了就吐出一个
            # 闭集外的 "pending"，模型拿到一个"这张卡还活着"的假信号。加固而不是许诺：这个出口
            # 语义上唯一站得住的解释就是"到点了"，于是吐 timed_out（下一行照样补上那句文案），
            # 结果字母表永远留在 spec 7.1 那六个状态里。用例用 `_NoSweepRegistry` 强行走到这里。
            outcome = {"status": "timed_out"}
        if outcome.get("status") == "timed_out":
            outcome["hint"] = _HINT_TIMED_OUT
        elif outcome.get("status") == "declined":
            outcome["hint"] = _HINT_DECLINED
        elif outcome.get("status") == "cancelled":
            outcome.setdefault("reason", request.cancel_reason or "run_ended")
        elif outcome.get("status") == "saved":
            outcome["hint"] = _HINT_SAVED
        return outcome
