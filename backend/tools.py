# -*- coding: utf-8 -*-
"""Tool wrappers. They always return a JSON string and never raise.

`request_secret` is the only tool in this plugin that blocks on a human, so it
owns four exit paths that the plan's status table does not spell out and the
tests do: the deadline it applies by itself (nobody else swept the record), the
cancellation that must reach a terminal state before we return, the idempotent
replay of a request whose ref may already be spent, and the cleanup that has to
happen when something *else* raises mid-poll (a tool call that ended with a card
still live is a card a human can still answer).

`secret_exec`/`secret_export` (Task 8) are the two places a ref is *spent* (I3),
and the two places the plaintext exists outside the vault. Three things follow
from that and none of them is style:

* `{{secret}}` is replaced **here**, in-process, one statement before the child is
  spawned. The model only ever sees its own template, `env[env_name]` carries the
  value to the child, and only the template may appear in a log line or an error
  string (R43: type names plus frame coordinates, never `str(exc)`).
* stdout/stderr are scrubbed *before* they are clipped, and the clip ceiling is
  32 KiB on purpose: it sits under the host's recent-result pruning threshold, so
  the result never takes the "write the whole thing to `<workspace>/tool_results/
  *.txt`, then stub it (stub embeds that path)" route — spec 10.1 path (6). That
  number is a security control, not output tidiness; there is a test on it.
  Three things make the sentence above actually true (fix round 1, finding I-1),
  and the first two rounds of this file got all three wrong at once: the ceiling
  is a *combined* budget for both streams (`_budget_pair`, order-stable and
  stdout-first, unused stdout allowance flowing to stderr), it is accounted in
  *serialized* bytes (`_wire_len` — JSON escaping doubles every `\n`/`"`/control
  byte, and 32 KiB of newlines is 64 KiB of result), and the truncation notice is
  reserved *inside* the budget instead of appended after the cut. Two 40 000-byte
  streams used to produce a 65 803-byte result, over the 50 000-byte threshold;
  the test now measures the serialized result, not the constant.
* `vault.consume`/`reopen` stay on the event loop (R14). No `to_thread`, no thread
  pool: the read-then-delete in `consume` is atomic only because nothing is
  awaited inside it.

Two boundaries this module must keep (spec section 7.1, rulings R15/R41/R46):

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
import os
import re
import traceback
from pathlib import Path
from typing import Any, Callable

from .records import RequestRegistry
from .scrub import Scrubber
from .vault import Vault

logger = logging.getLogger(__name__)

# 轮询步长。spec 15 A6 在真机上核对的就是"点 Stop 之后最迟一个 tick 工具就返回"，
# 所以这个数同时是取消延迟上限；改成 asyncio.wait **三方**竞速会把 A6 变成"看不见的竞态"
# （理由见 _await 的 docstring）。`_communicate` 用的是同一个 tick 轮询，但只等**一个**
# 已经存在的 future（子进程的 communicate），没有竞速，所以不在这条禁令里。
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

# ---- Task 8：两个消费点 -------------------------------------------------------
# 占位符由模型写、由插件替换。这个字面串既不进 vault 也不进结果串，模型侧看到的永远
# 是自己的模板（spec 7.2 第一句）。
SECRET_MARKER = "{{secret}}"
# `secret_export` 的 env_key 形状（spec 7.3）。下面那句错误消息把这条正则**原样**写给
# 模型看，所以 pattern 漂一个字符就得有一条用例红（tests/test_tools_consume.py）。
_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
# `secret_exec` 的 env_name 形状（上面 secret_exec 里写了为什么比 `_ENV_KEY_RE` 宽）。
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# Fix round 1 / I-5（R49 + R50）：正则只管形状，管不了"这个名字是不是宿主自己赖以运行的
# 那一个"。`PATH`/`HOME`/`IFS`/`LD_PRELOAD`/`NODE_OPTIONS` 全都匹配得通过，而 spec §5 写明
# `set_env_var` **既**落盘 `envs.json`（重启还在）**又**同步宿主进程 environ ——
# 模型自选一个就是可持久化的宿主自我 DoS。拒绝表只加在 `secret_export`：`secret_exec` 的
# `env_name` 覆写只活在这一个孩子进程的 environ 里，孩子死了就没了（裁定 R50 的后半句）。
# 表要短、要点名宿主真正读的那几个；正当的凭据名（`GITHUB_TOKEN` 这类）一律不在表里。
# Fix round 2 / I-5（R50）：轮 1 抄这张表时把 spec §7.3 点名的 **`BASH_ENV` 与 `ENV` 漏了**
# （当时用 `DYLD_INSERT_LIBRARIES`/`SHELL` 换了位置，却没写"换了两个"）。漏掉不是纸面问题：
# `_ENV_KEY_RE` 认这两个名字，宿主 `set_env_var`（`qwenpaw/envs/store.py:295-307`）只按
# `is_managed_control_key` 过滤，`_apply_to_environ`（同文件 105-133）直写 `os.environ`，
# `envs/registry.py:162-168` 保护的只有 `QWENPAW_WORKING_DIR`/`SECRET_DIR`/`_INTERNAL_KEYS`
# —— 宿主侧**没有兜底**。本机实测 `BASH_ENV=<文件> bash -c 'true'` 会执行那个文件，正是
# R50 说的"shell 启动期执行 + 落盘重启还在"这一类。`ENV` 在 POSIX/ksh 系 shell 里同族，本机
# dash 与 `bash --posix -c` 实测不执行 ⇒ 那条按 spec 点名收（多挡一个宿主常识名，代价是
# 模型不能把 secret 导出成 `ENV`，正当凭据名不受影响）。现在 12 项 = spec §7.3 的 10 项 +
# `DYLD_INSERT_LIBRARIES`/`SHELL`（macOS dylib 注入与宿主 shell 路径，宿主确实读）。
# 这张表**不能**只靠 `sorted(_HOST_ENV_DENYLIST)` 自证：用例迭代的是测试文件里另写的
# spec 字面清单（`tests/test_tools_consume.py`），删掉任意一项都会红。
_HOST_ENV_DENYLIST = frozenset({
    "PATH", "HOME", "IFS", "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES",
    "NODE_OPTIONS", "PYTHONPATH", "PROMPT_COMMAND", "SHELL", "BASH_ENV", "ENV",
})
# 输出封顶，`1024..32768`。**这是安全控制，不是排版**：宿主的 ToolResultPruningMiddleware
# 会先把整条结果写进 `<workspace>/tool_results/*.txt` 再截断，而截断桩里嵌的就是那个文件
# 路径（spec 10.1 路径⑥）。
# Fix round 1 / I-1（实测）：这个数是**两条流合起来的预算**，不是逐流上限，而且记账单位是
# **序列化之后的字节数**。理由：宿主剪枝读的是整条结果字符串。原来"逐流 32768 + 标记追加在
# 预算之外"的形状下，两条各 40 000 B 的输出实测产生 65 803 B 的结果 > recent 阈值 50 000 B，
# 于是路径⑥ 的那个磁盘工件天天产生；换行这种会被 JSON 翻倍的内容，单条流就能到 65 755 B。
# 现在：每流 ≤ 预算、两流合计 ≤ 预算、标记在预算**里面**预留，`_MAX_OUTPUT_BYTES` 加上
# 约 250 B 的 JSON 外壳仍然严格低于 50 000 B。
# 别把这个数"改进"成不封顶或更大 —— 那一改就把明文写盘的路打开了。
_MIN_OUTPUT_BYTES = 1024
_MAX_OUTPUT_BYTES = 32768
# 合池预算给 stderr 留的下限（评审给的形状）：stdout 把额度用光时 stderr 不能被饿到一句
# 错误都读不到，那种情况下两流各分 `limit // 2`。
_MIN_STREAM_SHARE = 4096
# 两个消费点共用的"重问"一句（spec 7.2/7.3 那两行文案在这一点上是同一句）。
_HINT_ASK_AGAIN = "Ask the user again with request_secret."
# 取消的两条出口都落在闭集里的 error（spec 7.2 没有 cancelled 这一档），但两句必须说清
# ref 在谁手里：入口那条还没消费、还能用；轮询那条已经花掉、必须重问。说反一句的后果是
# 一次无谓的重问，或者一次注定 ref_unavailable 的重试 —— 模型照字面行事。
_CANCELLED_NOT_STARTED = ("The tool call was cancelled before the secret was used. The ref was "
                          "not consumed, so it can still be used once a run is live again; "
                          "otherwise ask again with request_secret.")
_CANCELLED_CHILD_KILLED = ("The tool call was cancelled; the child process was killed and the "
                          "ref was consumed, so a retry needs a new request_secret.")
# Fix round 1 / I-6：兜底 error 支同样要把"花没花"说死。E2BIG/`OSError`/transport 故障发生在
# 消费**之后**（值已经进过 argv 或宿主存储），原来的句子两句都不写，于是模型要么去重试一个
# 注定 ref_unavailable 的键，要么白重问一次人类。消费前的路早就写了"not consumed"
# （env_key/env_name/cwd 三条），这两句补齐剩下那一半。
_SPENT_NEEDS_NEW_REF = ("The ref was already consumed, so a retry needs a new request_secret.")
_NOT_SPENT_STILL_GOOD = ("The ref was not consumed, so the same ref can be retried after the "
                         "input is fixed.")


def _text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


# 标记串的三段里只有那个十进制数会变形，所以预留按"整串最坏长度"算，一次算完不用迭代。
# `[truncated by secret-input]` 这个字面串是 spec §7.2 与用例断言的那个（带闭合方括号）；
# 计划示例代码写的是 `[truncated by secret-input: N bytes dropped]`，那样计划自己 Step 1 的
# `assertIn` 永远红 —— 冲突按"spec 字面串权威"解，字节数照报，只是不再冒充标记串的一部分。
_TRUNCATE_HEAD = "\n[truncated by secret-input] "
_TRUNCATE_TAIL = " bytes dropped"


def _wire_len(text: str) -> int:
    """`text` 进 JSON 字符串之后占的**字节数**（不含两侧引号）。

    Fix round 1 / I-1(2)：宿主剪枝中间件量的就是序列化后的整条结果，所以封顶必须按这把尺子
    记账。按原始 UTF-8 字节记的话，`\\n`、`\\"`、控制字符每个原始字节翻成两个结果字节，
    32 KiB 换行 = 64 KiB 结果，工件照样落盘（实测单条流 65 755 B）。非 ASCII 不膨胀
    （`ensure_ascii=False` 原样写出），所以这个数总是 ≥ 原始字节数 —— 用它记账两头都安全。
    """
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8")) - 2


def _budget_pair(stdout_len: int, stderr_len: int, limit: int) -> tuple[int, int]:
    """合池预算：stdout 优先、用剩的给 stderr（order-stable + give-when-unused）。

    不变式（`tests/test_tools_consume.py::CombinedBudgetTest` 逐个形状钉）：
    ``a <= limit``、``b <= limit``、``a + b <= limit``，同输入同输出（确定性）。

    两条对裁定字面式的修正（都写在报告的偏离清单里）：
    * 触发"对半分"的条件带上了 **stderr 真有话说** (`stderr_len > share`)。字面式
      `b = min(len(se), limit - a); b < floor ⇒ 对半分` 在 stderr 为空时也成立（0 < floor），
      于是"32 KiB stdout + 没有 stderr"这一最常见的形状会白丢一半 stdout。加了守卫之后
      空/够用的 stderr 不动 stdout 的额度，只有被饿到的才触发重分。
    * 反过来，stderr 只有 3 字节而 stdout 写满时，这一式仍然把 stdout 砍到 `limit // 2` ——
      那是裁定给的补救式本身的价格，保留它、不单方面换成"只让出 `min(se, floor)`"。
    """
    floor = min(limit // 4, _MIN_STREAM_SHARE)
    first = min(stdout_len, limit)
    share = limit - first
    if stderr_len > share and share < floor:
        first = limit // 2
        share = limit - first
    return first, min(stderr_len, share)


def _clip(text: str, limit: int) -> str:
    """把 `text` 压进 `limit` 个**序列化字节**，标记串在预算**里面**预留。

    旧实现先切 `limit` 再**追加** ~72 B 的标记，所以 `limit` 从来不是这一流的上界，
    "emitted ≤ max_output_bytes" 那句是假的（fix round 1 / I-1(1)）。
    二分找"能装下正文 + 标记"的最大前缀：切在字符边界上（不是字节边界），所以不再需要
    `decode(..., "ignore")` 去擦半个码点；`_wire_len` 对前缀单调不减、标记长度单调不增，
    谓词单调 ⇒ 二分成立。
    """
    whole = _wire_len(text)
    if whole <= limit:
        return text

    def notice(kept_wire: int) -> str:
        return f"{_TRUNCATE_HEAD}{max(whole - kept_wire, 0)}{_TRUNCATE_TAIL}"

    def fits(kept: int) -> bool:
        body = text[:kept]
        return _wire_len(body) + _wire_len(notice(_wire_len(body))) <= limit

    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid - 1
    if fits(lo):
        body = text[:lo]
        return body + notice(_wire_len(body))
    # 预算小到连标记都放不下（今天不可达：合池规则保证要截断的流至少拿到 floor=256 B，
    # 而标记 ~48 B）。真到了那里，宁可少给正文也不能说谎——回一条能装下的最长前缀，
    # 不带标记，长度上界照样守住。
    while lo > 0 and _wire_len(text[:lo]) > limit:
        lo -= 1
    return text[:lo]


def _short(text: str, cap: int = _MAX_MESSAGE_CHARS) -> str:
    """回显给模型的**模型自选文本**一律截断。

    R43 说结果串里只准有类名与帧坐标；这里的内容不是异常而是模型自己写的名字/路径片段，
    它合法上要回显（不然模型不知道改哪儿），但长度不由我们兜着 —— 一句 5 KiB 的
    `env_key` 报错本身就是一条把结果撑过剪枝阈值的路（fix round 1 / I-1 的同一条纪律）。
    """
    return text if len(text) <= cap else text[:cap] + "…[truncated]"


def _failure_detail(exc: BaseException, *, tool: str = "request_secret") -> str:
    """定位失败而不渲染任何可能带值的东西（裁定 R43，Task 4/5 的 R34 先例）。

    只取 ``traceback.extract_tb`` 的 filename/lineno/name：never ``str(exc)``、
    never 帧的源码行、never 局部变量。宿主或底层那句原话可能正带着它咬到的字符，
    而 `logger.exception` 会把整段 exc_text 打进日志 —— 那是 spec 10.1 明确排除的通道。
    Task 8 的两个消费点离明文最近（`env_writer` 与 vault 抛的那句话里就可能是值），
    所以它们也走这同一个函数，只是把 `tool=` 换成自己的名字，观察面才能分到层。
    """
    try:
        frames = traceback.extract_tb(exc.__traceback__)
        deepest = frames[-1] if frames else None
        site = "unknown" if deepest is None else (
            f"{deepest.filename}:{deepest.lineno} in {deepest.name}")
        return f"exc={type(exc).__name__} site={site} tool={tool}"
    except Exception:  # noqa: BLE001 - 记不住现场也不能把异常顶给宿主
        return f"exc={type(exc).__name__} site=unknown tool={tool}"


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
        # Task 7 复评 T7-b（Task 8 收掉）：`request` 必须在 `try` **外面**初始化。
        # `create()` 返回之后、`_await` 之前还有三行会抛（日志行里那个 config 读数是即时
        # 求值的、`replayed` 读记录、`peek_value` 读 vault），原来只有 `_await` 内部的失败
        # 有清账（I-3），这一段窗口抛了就留下一张活卡：模型已经拿到 error，人还能答它，
        # 答进去的值没有任何人认领。提到外面之后，外层 handler 就能在它已存在时把卡结掉。
        request = None
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
            if request is not None:
                # T7-b 的外层清账。`_cancel_quietly` 自己已经把 registry.cancel() 的异常
                # 记成一条类名日志、绝不外抛，这里再包一层只为了"`request.request_id` 这一句
                # 取值本身也不许改变返回形状"（记录对象由装配方给的注册表造，形状不由我们
                # 保证）。只夹 Exception：清理途中真被硬取消时，CancelledError 必须继续上抛，
                # 那正是 T7-a 守的那条纪律，不能在这里反过来破一次。
                # 残余项（诚实记）：`create()` **内部**在插进 `_pending` 之后才抛的那一档仍然
                # 清不到 —— 那一刻 `request` 还是 None，id 只活在 records 的帧里。要闭合它得
                # 在 `RequestRegistry.create` 里包一层 try/except（那是 Task 3 的文件；Task 9
                # 接线时复核），不在本文件的所有权范围内，所以留话不留假动作。
                try:
                    await self._cancel_quietly(request.request_id)
                except Exception as cleanup_exc:  # noqa: BLE001 - 清账不许改写结果形状
                    logger.error("secret-input: request_secret post-failure cleanup raised "
                                 "(exc=%s)", type(cleanup_exc).__name__)
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
            # Task 7 复评 T7-a（Task 8 收掉）：这一句原来是裸的 `await self._registry.cancel(...)`。
            # `registry.cancel()` 内部无 await（records.py 的迁移点全同步），所以它**不会**被第二次
            # 取消打断 —— 但它自己可以抛（reason 闭集校验是 ValueError，宿主注册表坏掉是任意异常），
            # 而那时顶掉本次取消的就是清理路径：替换上来的异常是 Exception，被 `request_secret`
            # 的外层 `except Exception` 接住、翻成 {"status":"error"} 结果串 = **取消被咽下去**，
            # 协调器收到"工具正常返回"。走 `_cancel_quietly`（它就是为这件事写的：只记类名、
            # 绝不外抛），然后 `raise` 原样上抛。
            await self._cancel_quietly(request.request_id)
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

    # ---- Task 8：两个消费点 ---------------------------------------------------

    async def secret_exec(self, ref: str, command: str, env_name: str = "SECRET",
                          timeout_seconds: int = 120, cwd: str | None = None,
                          max_output_bytes: int = 32768) -> str:
        # fix round 1 / I-6：`spent` 必须在 `try` **外面**初始化 —— 消费之后的任何一次抛
        # （spawn 的 E2BIG、transport 故障、擦洗内部错误）都得能说出"ref 已经花掉了"。
        spent = False
        try:
            template = str(command or "")
            if not template.strip():
                return _text({"status": "error", "message": "command is required."})
            limit = min(max(int(max_output_bytes), _MIN_OUTPUT_BYTES), _MAX_OUTPUT_BYTES)
            timeout = min(max(int(timeout_seconds), 1), 600)
            if self._cancel_requested():
                # 本轮已经结束：既不能再花掉一个一次性 ref，也不该再 fork。
                return _text({"status": "error", "message": _CANCELLED_NOT_STARTED})
            workdir, bad = self._resolve_cwd(cwd)
            if bad:
                return _text({"status": "error", "message": bad})
            # env_name 与 cwd/env_key 同一档：校验必须在消费**之前**（计划给这两个输入都
            # 配了"失败绝不消费 ref"的用例，唯独漏了这一头）。不校验的代价不是报错那么简单：
            # `execve` 对带空格/`=`/NUL 的键名要么当场抛，要么把一个子进程读不到的键交出去
            # —— 前者让异常支把已经消费的 ref 花掉，后者让人类白答一次。
            # 这里比 `secret_export` 的 `_ENV_KEY_RE` 宽（收小写）是刻意的：export 的键名要
            # 落进宿主 env 存储、按宿主的命名规矩来；这里的名字只活在这一个子进程的
            # environ 里，小写照样读得到，拒绝它只是把人挡在能用的一天之外。
            # **不需要** `_HOST_ENV_DENYLIST`（裁定 R50）：这次覆写随孩子一起消失。
            effective_env_name = str(env_name or "SECRET")
            if not _ENV_NAME_RE.match(effective_env_name):
                return _text({"status": "error",
                              "message": "env_name must match ^[A-Za-z_][A-Za-z0-9_]{0,63}$, "
                                         f"got: {_short(repr(effective_env_name))}. The ref was "
                                         f"not consumed."})

            # R14：这一行没有 await，读+删在同一个事件循环里是原子的 —— 别把它挪进线程。
            value, status = self._vault.consume(ref)
            if status != "ok" or value is None:
                return _text({"status": status, "hint": _HINT_ASK_AGAIN})
            spent = True
            self._scrubber.register(value)

            # 替换发生在**这一行**，在插件进程里：模型写的模板到此为止，`real` 不出进程
            # 也不进任何日志/异常文本（下面 except 的注释）。env 那条是同一个值的第二个
            # 通道，`{{secret}}` 没用上时它是唯一通道，所以 note 那句话必须说清。
            real = template.replace(SECRET_MARKER, value)
            env = dict(os.environ)
            env[effective_env_name] = value
            argv = ["/bin/sh", "-c", real]
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=workdir, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            kind, out, err = await self._communicate(proc, timeout)
            if kind == "cancelled":
                return _text({"status": "error", "message": _CANCELLED_CHILD_KILLED})
            if kind == "timeout":
                # 超时也必须让 ref 保持已消费（值可能已被子进程用掉），而且这句要说给模型：
                # 不说它就会拿同一个 ref 再试一次，然后收到一个"骗人"的 ref_unavailable。
                return _text({"status": "error",
                              "message": f"command timed out after {timeout}s; the ref was "
                                         f"consumed, ask again if you need to retry."})

            # 先擦洗后截断，顺序不能倒：截断点切在值中间会留下一个谁都匹配不上的半截明文。
            stdout = self._scrubber.replace(out.decode("utf-8", "replace"))
            stderr = self._scrubber.replace(err.decode("utf-8", "replace"))
            # 合池预算（fix round 1 / I-1）：`limit` 是**两条流加起来**的上界，按序列化字节
            # 记账，stdout 优先、用剩的给 stderr、stderr 真被饿到时才对半分。逐流各给 `limit`
            # 的话，两条各 40 000 B 实测产出 65 803 B 的结果 —— 越过宿主 50 000 B 阈值，
            # §10.1 路径⑥ 的磁盘工件就是这么回来的。
            budget_out, budget_err = _budget_pair(_wire_len(stdout), _wire_len(stderr), limit)
            note = None
            if SECRET_MARKER not in template:
                note = (f"{{secret}} placeholder unused; the value reached the command only "
                        f"through ${effective_env_name}.")
            payload = {"status": "ok", "exit_code": proc.returncode,
                       "stdout": _clip(stdout, budget_out),
                       "stderr": _clip(stderr, budget_err), "ref": "consumed"}
            if note:
                payload["note"] = note
            return _text(payload)
        except Exception as exc:  # noqa: BLE001 - 异常文本里绝不带 command 实串
            # 三件事在这一支里是刻意的：
            # 1. 只报类名（R43）。`real` 那个变量就在这一帧的 locals 里，所以既不打 `str(exc)`
            #    也不打帧内容 —— `logger.exception` 会把异常原文写进宿主日志，Task 5 的评审
            #    就是为这件事把它换掉的（同一理由在这里成立两次：`os.environ` 的拷贝、子进程
            #    抛回来的任何一句话都可能带着值）。
            # 2. CancelledError 是 BaseException，不进这一支：取消必须原样上抛（`_communicate`
            #    里那一支负责杀子进程后重抛），把它翻成结果串就是把协调器的语义破一次。
            # 3. 结果里不回显模板：模板是模型写的，它可能刚把上一个值抄进去过。
            logger.error("secret-input: secret_exec failed %s",
                         _failure_detail(exc, tool="secret_exec"))
            tail = _SPENT_NEEDS_NEW_REF if spent else _NOT_SPENT_STILL_GOOD
            return _text({"status": "error",
                          "message": _short(f"secret_exec failed: {type(exc).__name__}. "
                                            f"{tail}")})

    async def secret_export(self, ref: str, env_key: str) -> str:
        spent = False                       # 同 secret_exec：兜底支也要能说清 ref 花没花（I-6）
        try:
            key = str(env_key or "")
            if not _ENV_KEY_RE.match(key):
                return _text({"status": "error",
                              "message": "env_key must match ^[A-Z_][A-Z0-9_]{0,63}$, got: "
                                         f"{_short(repr(key))}. The ref was not consumed."})
            # fix round 1 / I-5（R49/R50）：形状对了还不够，这个名字**是不是宿主自己赖以运行
            # 的那一个**也要问。`set_env_var` 落盘 + 同步宿主进程 environ（spec §5），所以
            # `PATH`/`HOME`/`IFS`/`LD_PRELOAD`/`NODE_OPTIONS` 这类名字交出去就是一次可持久化的
            # 宿主自我 DoS —— 而且下一次启动还在。检查放在消费之前：挡住了就不该花掉 ref。
            if key in _HOST_ENV_DENYLIST:
                return _text({"status": "error",
                              "message": f"env_key is host-critical and cannot be overwritten "
                                         f"by this plugin: {key}. The ref was not consumed; "
                                         f"pick a name that only your own tool reads."})
            # 值进宿主 env 存储是**长期驻留**（spec 10.2 第 2 条：那是设计上的暴露面），
            # 所以一轮已经结束的调用更不该留下它 —— 取消检查放在消费之前。
            if self._cancel_requested():
                return _text({"status": "error", "message": _CANCELLED_NOT_STARTED})
            value, status = self._vault.consume(ref)
            if status != "ok" or value is None:
                return _text({"status": status,
                              "hint": _HINT_ASK_AGAIN})
            spent = True
            self._scrubber.register(value)
            try:
                self._write_env(key, value)
            except Exception as exc:  # noqa: BLE001
                # 注入进来的 writer 抛的那句话里完全可能带着它刚拿到的明文 ⇒ 只记类名与
                # 帧坐标（`site=... in _write_env` 与 `site=... in <宿主 setter>` 分得开，
                # "没接线"这种装配缺陷因此查得到）。然后把 ref 还回去：没写成功却把 ref
                # 花掉，等于让人类白答一次（spec 7.3 的"失败不消费"）。
                logger.error("secret-input: secret_export env write failed %s",
                             _failure_detail(exc, tool="secret_export"))
                self._vault.reopen(ref, value)      # 没写成功就把 ref 还回去
                spent = False                      # 还回去了，下面那句才敢说"没消费"
                return _text({"status": "error",
                              "message": f"env store write failed: {type(exc).__name__}. "
                                         f"The ref was not consumed; retry or re-ask."})
            # spec §7.3 把"值现在到底在哪儿"写进了返回契约，其中 `(Fernet, 0600)` 是操作者
            # 判断这条暴露面是加密落盘还是明文落盘的唯一线索（fix round 1 minor：原来漏了）。
            return _text({"status": "ok", "env_key": key, "ref": "consumed",
                          "hint": f"Later shell tools can use ${key}. The value is not in "
                                  f"context; it is now in the host env store (Fernet, 0600) "
                                  f"and in the host process environment."})
        except Exception as exc:  # noqa: BLE001
            logger.error("secret-input: secret_export failed %s",
                         _failure_detail(exc, tool="secret_export"))
            tail = _SPENT_NEEDS_NEW_REF if spent else _NOT_SPENT_STILL_GOOD
            return _text({"status": "error",
                          "message": _short(f"secret_export failed: {type(exc).__name__}. "
                                            f"{tail}")})

    def _cancel_requested(self) -> bool:
        """本轮是不是已经点了 Stop。读法只有 `cancel_event()` 这一个缝（R14 的同一条纪律：
        不起任务、不进线程、不碰宿主）。

        回 None 表示"不在工具调用上下文里"（`identity.py` 的注释），那时什么都不能算取消。
        读数本身不在这里包 try：两个调用方的外层 except 已经负责把它变成 error 结果串，
        而在消费**之前**抛也就意味着 ref 还在（与 Task 7 评审 I-3 的读数进 try 同构）。
        """
        cancel = self._cancel_event()
        return cancel is not None and bool(cancel.is_set())

    async def _communicate(self, proc, timeout: int) -> tuple[str, Any, Any]:
        """等子进程，同时盯着 deadline 与 cancel_event，回 (腿, stdout, stderr)。

        为什么不用 `asyncio.wait_for(proc.communicate(), timeout)`：`wait_for` 一超时就
        **取消**它等的对象，而 `communicate()` 不可重入 —— 想用轮询去看 cancel_event 就得
        每 tick 重新包一次，第一个 tick 就把那个唯一的读者取消了，之后没人读管道，子进程
        会把 stdout 撑满然后卡在写管道上（这才是真死锁）。这里建**一次**任务，用
        `asyncio.wait` 每 tick 看一眼：它超时**不取消**在等的任务，所以读者始终只有一个。
        代价是最多多等一个 `_POLL_SECONDS`，换来 Stop 之后立刻 kill 子进程，而不是等宿主
        5 s 宽限期用完再硬取消。与 `_await` 同一个形状，也不违反 `_POLL_SECONDS` 那条
        "不要三方竞速"的禁令（这里只等一个 future）。

        四条出口（fix round 1 / I-3、I-4 之后）：cancelled / timeout / 硬取消 / 非取消异常。
        **四条都不许把子进程留在原地**：前两条与后两支都经过 `_stop_child`，后两支收尸之后
        原样上抛（这一层的职责是"不留活进程"，不是"把失败改成成功"）。
        """
        comm = asyncio.ensure_future(proc.communicate())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            while True:
                if self._cancel_requested():
                    await self._stop_child(proc, comm)
                    return "cancelled", None, None
                if loop.time() >= deadline:
                    await self._stop_child(proc, comm)
                    return "timeout", None, None
                done, _pending = await asyncio.wait({comm}, timeout=_POLL_SECONDS)
                if done:
                    out, err = comm.result()
                    return "ok", out, err
        except asyncio.CancelledError:
            # 硬取消（force=True / 宽限期用完 / shutdown）：收掉子进程再**原样上抛**。
            # 取消不是失败，变成结果串就是把协调器的语义破一次（T7-a 同一条纪律）。
            # fix round 1 / I-3：`_stop_child` 现在**第一步就是同步的 `proc.kill()`**，所以
            # 第二次取消最多打断后面的收尾（settle 读者 / `proc.wait()`），不可能让这一支
            # "返回一个还活着的子进程"。顺序反过来时（旧实现 kill 排在那次 await 之后），
            # 从这条腿抛出去的 CancelledError 上面**没有** handler 会再接一次 ——
            # 实测 pre-fix `stops == []`：明文就留在那个孤儿的 argv/environ 里。
            await self._stop_child(proc, comm)
            raise
        except Exception:
            # fix round 1 / I-4：`comm.result()` 或轮询里任何一层抛**非取消**异常时，原来
            # 只有 `finally` 取消读者，没有任何一支去杀进程 —— `secret_exec` 的兜底 except
            # 把它翻成 error 结果串交回宿主，而子进程还在跑，argv/environ 里就是明文。
            # 收尸之后再原样上抛：这一层的职责是"不留活进程"，不是"把失败改成成功"。
            await self._stop_child(proc, comm)
            raise
        finally:
            if not comm.done():
                comm.cancel()

    async def _stop_child(self, proc, comm) -> None:
        """杀掉子进程并回收。**顺序是刻意的，而且和上一版相反**（fix round 1 / I-3）。

        1. `proc.kill()` —— 同步，排在**任何 await 之前**。这条腿存在的唯一理由就是"别让
           带着明文的进程继续活"；把它排在一次可被打断的 await 之后，等于把这件事交给
           "清理期间不再来第二次取消"这个假设，而协调器的 force 路径与 shutdown 收尾恰恰会
           连着取消（用例 `test_second_cancel_during_cleanup_still_kills_the_child` 演的
           就是那次取消，pre-fix 实测 `kill()` 一次都没执行）。
        2. `comm.cancel()` —— 取消那个唯一的管道读者。
        3. `await asyncio.wait({comm})` —— 让上一次取消落地，读者 settle 掉。asyncio 文档警告
           的是"PIPE 还挂着读者时单独 await wait() 会死锁"；这里读者已被取消，而且**子进程
           已经死了**，管道不会再被写，所以这一步和下一步都不可能因为管道撑满而挂住。
        4. `await proc.wait()` —— 收尸。若第二次取消正好打在这一步，进程已退出，剩下的只是
           一个表项，由 loop 的子进程监视器回收，不再是"带着明文的活进程"。
        """
        proc.kill()
        comm.cancel()
        await asyncio.wait({comm})
        await proc.wait()

    def _write_env(self, key: str, value: str) -> None:
        # R46：`backend/tools.py` 由导入门禁止 import 宿主包（`tests/test_secret_store_gate.py`
        # 的 HOST_GATED_MODULES 覆盖 tools.py，函数体内的 import 也算）。宿主调用只能走
        # Task 9 在 main.py 里注入的 env_writer；没注入就是配置错误，直说而不是偷偷 import。
        if self._env_writer is None:
            raise RuntimeError("env writer not wired (plugin assembly defect)")
        self._env_writer(key, value)

    def _resolve_cwd(self, cwd: str | None) -> tuple[str | None, str]:
        """把模型自选的工作目录夹在 project 目录内；夹不住就**拒绝**，不是"算了随你"。

        fix round 1 / I-2（R48，实测）：`project_dir()` 在没有会话上下文时回 `""`
        （`identity.py` 明写），旧代码的 `if base:` 于是让 containment **变成可选的** ——
        `cwd="/tmp"`、`cwd="/etc"` 直接跑通。spec §7.2 那句"必须在当前 workspace/project
        目录内，否则 error"没有例外从句，所以缺上下文时唯一站得住的是 fail closed：
        证明不了"在里面"，就不接受显式目录。正当的出路不是给模型加一个"项目参数"
        （那是 fail-open 多走一步），而是 Task 9 装配时注入的 `project_dir`。
        没有显式 cwd 时不挡：那一条腿上模型没有任何目录自由度（子进程落在插件进程自己的
        cwd），要拒它就是把工具关掉，不是把面收窄。
        """
        raw = str(cwd or "").strip()
        base = self._project_dir() if callable(self._project_dir) else ""
        if not raw:
            return (str(Path(base).resolve()) if base else None), ""
        if not base:
            return None, ("cwd cannot be checked: this tool call has no project/workspace "
                          "context. The ref was not consumed. Omit cwd to use the default "
                          "working directory, or run the call from inside a project.")
        target = Path(raw) if os.path.isabs(raw) else (Path(base) / raw)
        try:
            resolved = target.resolve()
        except OSError as exc:
            # fix round 1 minor（R43）：这里原来是 `f"cannot resolve cwd: {exc}"`，把模型
            # 自选的**无限长**路径文本连宿主 strerror 一起写进结果串。兄弟分支只回
            # `resolved.name`，这一句同理：报错只准说"哪一类失败"。
            return None, f"cannot resolve cwd: {type(exc).__name__}"
        if not resolved.is_dir():
            return None, f"cwd is not a directory: {_short(resolved.name)}"
        root = Path(base).resolve()
        if root not in resolved.parents and resolved != root:
            return None, ("cwd must stay inside the current project directory "
                          f"({_short(root.name)}); got {_short(resolved.name)}")
        return str(resolved), ""
