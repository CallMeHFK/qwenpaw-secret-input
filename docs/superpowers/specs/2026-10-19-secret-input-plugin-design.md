# secret-input：QwenPaw 私密输入插件 设计规格

- 日期：2026-10-19
- 插件 id：`secret-input`（显示名「Secret Input（私密输入）」）
- 宿主基线：qwenpaw **2.2.2**（`/home/leo/miniforge3/lib/python3.12/site-packages/qwenpaw/`，进程 `qwenpaw app --host 0.0.0.0 --port 19999`）
- 参照实现：T3 Code `request_secret`，取证自 `~/.t3/runtime/versions/0.1.1-preview.20261019.3339/t3`（ELF，带 debug_info，未 strip）
- 状态：设计已获 leo 批准（消费路径=受控执行器 + 落 env 两条都要；出站擦洗默认开）

---

## 1. 问题

Agent 执行到一半需要人类才持有的机密（token / API key / webhook 签名密钥 / 密码）。今天只有两条路，都坏：

1. 用户把密钥打进聊天框 → 值进入模型上下文、进转录、进落盘会话历史，还会随请求发给模型供应商。
2. 模型把密钥写进某个工具的参数 → 值进了上下文（它是作者），还进了工具调用记录。

本插件给第三条路：**值只走浏览器 → 插件 HTTP 端点这一条独立传输，模型的入参、结果、转录、上下文里永远只有不透明引用**。这跟 T3 的做法同构，并额外补上 T3 没有的出站擦洗。

## 2. 参照实现的事实（逐字）

来自 T3 二进制内嵌源（`src/mcp/toolkits/orchestrator/tools.ts`、`packages/contracts/src/secretRequest.ts`、`src/auth/ServerSecretStore.ts`）：

| 事实 | 逐字证据 |
| --- | --- |
| 值绝不返回给模型 | `description: "…The value is kept by the app and NEVER returned to you or shown in the transcript."` |
| 入参四项 + 幂等键 | `label` / `reason` / `placeholder?` / `timeoutMs`（`Default 10 minutes`）/ `clientRequestId`（`Reuse when retrying a call that lost its result, so the user sees one card and its answer is returned again`） |
| 结果四态 | `OrchestratorMcpRequestSecretResult = Union([Struct({status: Literal("saved"), secretRef…}), Struct({status: Literals(["declined","cancelled","timed_out"])})])` |
| ref 一次性、不可见 | `secretRef: SecretRef.annotate({ description: "Pass it to a tool that accepts a secretRef; it works once, and you never see the value." })` |
| 失效报文教模型重新问 | `ref_unavailable: "That secretRef was already used or does not exist. Ask the user again with request_secret."`；`ref_expired: "That secretRef expired. Ask the user again with request_secret."` |
| 答案走独立 RPC，不过模型 | `SecretRequestAnswerInput = Struct({threadId, turnItemId, answer: Union([Struct({type: Literal("save"), secret: TrimmedNonEmptyString}), Struct({type: Literal("decline")})])})`，配套 `WsSecretsAnswerRequestRpc` |
| 消费方声明 ref 字段而非明文 | `secretRef: optional$9(SecretRef).annotate({ description: "A secret the user entered through request_secret, used instead of secret. It is consumed by this save." })` |
| 落盘权限 | `fileSystem.chmod(serverConfig.secretsDir, 448)`（0o700），单文件 0o600 |
| 系统提示硬规矩 | `Never ask for a secret in chat, never invent one, and never repeat one.` |

采契约与不变式，不复制实现（T3 用 Effect/RPC，宿主是 FastAPI + agentscope）。

## 3. 不变式（所有设计与测试都为这四条服务）

- **I1 入站不可见**：值的合法传输路径只有 `POST /api/secret-input/card/{request_id}/answer` 的 body。它不出现在任何工具入参、工具结果、聊天转录、会话上下文（memory）、宿主日志、异常报文，以及**宿主为截断而落的磁盘工件**（`<workspace>/tool_results/*.txt`、`{dialog_path}/*.jsonl`）里。逐通道判定见 §10.1。
- **I2 引用不可读**：`ref` 是不可逆句柄。除 `secret_exec` / `secret_export` 两个消费点外，插件不暴露任何能读出已存值的端点或工具。
- **I3 一次性**：ref 成功消费后立即失效；重放同一 ref 得到 `ref_unavailable`，报文直指"用 request_secret 重新问"。ref 另有 TTL，过期得 `ref_expired`。
- **I4 出站擦洗**：插件已知的明文（及其 base64/base64url/hex/percent 变体）在写进 agent 上下文前被替换成 `***`。这是尽力而为的兜底，不是沙箱（见 §10）。

## 4. 非目标

- 不做通用密钥管理系统（不接管宿主 provider/env 的读写面）。
- 不做多机/云端中转、不做端到端加密信道（值仍走本机 HTTP）。
- 不保证同机进程隔离：本机 agent 握着 `execute_shell_command`，能读 `/proc`、能 curl loopback，插件不在 OS 层面与它对峙（§10）。
- 不持久化待消费的值（内存态、TTL 到期即弃；只有 `secret_export` 走宿主自己的加密 env 存储落地）。
- 不改宿主代码。只用公开扩展点。

## 5. 宿主事实基线（本轮 file:line 取证）

| 需要的能力 | 宿主提供 | 出处 |
| --- | --- | --- |
| 注册工具 | `register_tool(tool_name, tool_func, description='', icon='🔧', enabled=False, tool_type='network', target_param='')` | `plugins/api.py:820` |
| 注册 HTTP 路由 | `register_http_router(router, *, prefix, tags=None)` → 挂在 `/api` + prefix | `plugins/api.py:600` |
| 注册中间件 | `register_middleware(factory, *, priority=100)`，factory 收 `(ctx, agent_config)` 返 `MiddlewareBase\|None`，lower=outermost | `plugins/api.py:654` |
| 注入系统提示段 | `register_prompt_section(name, after, provider, *, priority=100, condition=None, agent_id=None)`，anchor `workspace/multimodal/env_context` | `plugins/api.py:1161` |
| 会话身份 | `app/agent_context.py` `get_current_session_id()/get_current_user_id()/get_current_channel()/get_current_agent_id()`；工具内另有 `tool_calls/_ctxvars.py:23 get_call_context()` → `ToolCallContext(session_id, agent_id, root_session_id, cancel_event, is_cancelled, …)`（`tool_calls/_context.py:47`） | 已验 |
| 阻塞等人 | 插件工具**无 per-tool 超时**：`ToolCoordinator` 构造时未传 `default_timeout_secs` ⇒ `_resolve_timeout` 返 `None`、`offload_deadline` 为 `None` | `app/app_services/app_service_manager.py:58`、`tool_calls/_coordinator.py:1161-1178` |
| 改写写进上下文的结果 | 宿主先例：`async for event in next_handler(): if isinstance(event, ToolResponse): event = await self.prune_tool_response_async(event)`，注释 "Cap the current ToolResponse **before it enters agent context**" | `agents/middlewares.py:762` |
| 事件类型 | `from agentscope.tool import ToolChunk, ToolResponse`；`from agentscope.message import Msg, TextBlock, ToolResultState`；`from agentscope.middleware import MiddlewareBase` | `agents/middlewares.py:24-26` |
| **中间件洋葱序（擦洗能否拦住磁盘副本的判据）** | `runtime/builder.py:1336-1343` 的 docstring 明写 "Order (onion model, outermost first): 1. ToolResultPruningMiddleware 2. ToolCoordinatorMiddleware 3. **Plugin-registered middlewares (sorted by priority)** 4. VisualCompressionMiddleware"。剪枝中间件在 `:1372` 最先 append，插件 mw 在 `:1418-1422` 之后 append ⇒ **插件永远在剪枝的内侧**，`priority` 只能在插件之间排序、**不可能排到剪枝外面**（也不需要）。工具的 `final_response` 由 coordinator 的 `_drain` 从内层链收集（`tool_calls/_coordinator.py:741-748`、`:840`），落盘/截断只发生在这之后（后台路 `:877` 才调 `background_result_processor`；常规路是最外层的剪枝中间件）⇒ **擦洗先于截断，磁盘副本天然已是 `***` 版** | 已逐行验 |
| 工具结果的磁盘落盘面（测试要 grep 的对象） | 超阈值时先整体落盘再截断，且**截断stub 里带文件路径**（模型可 read_file 读回）：`agents/tools/utils.py:111 save_text_output` → `{output_dir}/{safe_id}-{uuid}.txt`；`prune_text` 仅当 `len(bytes) > max_bytes + slack` 才存（`:263-269`），存到才生成带 `file_path` 的 stub（`:272-283`）。目录 = `<workspace_dir>/<tool_result_pruning_config.tool_results_cache>`，默认名 **`tool_results`**（`config/config.py:1191`），剪枝 **默认 enabled=True**（`:1143`，recent 50000B / old 3000B） | `agents/tools/utils.py:134/241/263-283`、`runtime/builder.py:1290/1307-1310`（同型计算另见 `:1283`） |
| 会话压缩的另一条落盘面 | `agents/offloader.py` `QwenPawOffloader.offload_context` 把被逐出的消息追加到 `{dialog_path}/{YYYY-MM-DD}.jsonl`、`offload_tool_result` 把工具结果写 `{tool_results_dir}/{uuid}.txt` ⇒ 写的是**内存里的那份**（已被我们擦洗） | 已读 |
| 落 env（持久） | `envs/store.py:295 set_env_var(key, value)` → `_save_envs_unlocked` 末尾 `_sync_environ(old, envs)`（**确实写进宿主进程 environ**），文件 `~/.qwenpaw.secret/envs.json` Fernet 加密 + `chmod 0o600` | `envs/store.py:146/258-262/295` |
| 加密底座（可选） | `security/secret_store.py` `encrypt/decrypt/is_encrypted`（372/381/403），master key 走 keyring 或 `~/.qwenpaw.secret/.master_key`（0600） | 已验 |
| 前端扩展 | `window.QwenPaw` Host SDK：`host.React/antd/getApiToken/fetch`、`host.getCurrentSessionId()`、`slot.fill(pluginId,'overlay.global',render)`、`chat.toolRender(pluginId, toolName, ({result,sessionId,messageId})=>…)` | `docs/plugins.en.md:482-780` |
| 鉴权 | `AuthMiddleware` 全局挂在 `app/_app.py:831`；`_should_skip_auth`：auth 未启用或无注册用户 ⇒ 全放行；启用时 `/api/*` 要 Bearer，除 `security.allow_no_auth_hosts` 默认 `["127.0.0.1","::1"]` | `app/auth.py:724-790`、`config/config.py:3055` |
| 监听面 | 宿主进程 `--host 0.0.0.0 --port 19999` ⇒ 默认 auth 关闭时**局域网可达且无鉴权** | `ps` 实测 pid 8999 |

先前一份调研报告称 `app/tool_calls/_middleware.py` 有 `on_result_end(entry, result)` 钩子 —— **不成立**（该路径不存在，`on_result_end` 全仓无匹配）。真实擦洗点按上表 `agents/middlewares.py:762` 的先例定为 `on_acting`。

## 6. 包结构

```
secret_input/                      # 仓库根 = 插件根（plugin.json 在根，装/发两条路径都认）
├── plugin.json
├── README.md / README.zh-CN.md
├── CONTRIBUTING.md / CODE_OF_CONDUCT.md / SECURITY.md / .github/   # 按 repo-standards-alignment
├── backend/
│   ├── __init__.py
│   ├── main.py        # plugin 对象 + register(api)，每个注册单独 try/except
│   ├── records.py     # SecretRequest 状态机 + 进程内登记表 + GC + clientRequestId 幂等
│   ├── vault.py       # ref → 明文（内存态）、一次性 consume、TTL
│   ├── scrub.py       # 已知明文集合 + 变体匹配 + 替换
│   ├── tools.py       # request_secret / secret_exec / secret_export（只返字符串）
│   ├── router.py      # APIRouter：/pending /card/{id} /card/{id}/meta /card/{id}/answer
│   ├── card_page.py   # /card/{id} 的自包含 HTML（无 CDN、无构建）
│   ├── middleware.py  # SecretScrubMiddleware(MiddlewareBase).on_acting
│   └── config.py      # 读 api.config / 环境变量，容忍空 config
├── ui/index.js        # 免构建前端 bundle：overlay 卡片（React.createElement）
├── tests/             # unittest，见 §12
├── packaging/
│   ├── build_zip.sh   # 只打包运行文件，symlink 非空即拒绝构建
│   └── verify_qp_plugin.py   # 技能里的离线验证脚本（含 8 条负对照门）
└── docs/superpowers/specs/
```

manifest（`plugin.json`）：

```json
{
  "id": "secret-input",
  "name": "Secret Input（私密输入）",
  "version": "0.1.0",
  "type": "general",
  "description": "Ask the user for a secret through a private card; the value never enters the model's context — only a one-time ref does.",
  "description_i18n": {"zh-CN": "用私密卡片向用户索取密钥；明文永不进入模型上下文，模型只拿到一次性引用。"},
  "author": "leo",
  "entry": {"backend": "backend/main.py", "frontend": "ui/index.js"},
  "dependencies": [],
  "qwenpaw_version": {"min": "2.1.0", "max": "2.99.0"},
  "meta": {"category": "security"}
}
```

`max` 主版本 2、不等于宿主 2.2.2（右开语义下填 `2.2.2` 是自我排除且宿主今天完全不报，只能靠自查脚本抓）。`dependencies: []` 保离线验证不污染宿主。不注册 skill（`register_skill_provider` 不用）。

### 6.1 出站擦洗中间件（I4 的实现点）

- 挂载：`api.register_middleware(factory, priority=50)`；`factory(ctx, agent_config)` 在 `scrub_enabled=false` 时返回 `None`（装配期即关闭，不留常驻开销）。
- 类：`SecretScrubMiddleware(MiddlewareBase)`，只实现 `on_acting`。照宿主剪枝中间件的写法（`agents/middlewares.py:762`）逐件消费内层链：`async for event in next_handler(): if isinstance(event, (ToolResponse, ToolChunk)): event = self._scrub_event(event)` 再 `yield`，其余事件原样透传。改写只动 `text` 块（`TextBlock.text` / dict 形态的 `block["text"]`），不改 `state`/`id`。
- 为什么这个点位够用：§5 的洋葱序把插件 mw 固定在剪枝与 coordinator 的**内侧**，所以擦洗先于"截断 + 整体落盘"，也先于写进 memory 的那一份。
- 匹配集：登记在 `scrub.py` 的已知明文（`ref` 保存时登记，消费后再保留 `scrub_ttl_seconds`），每条同时生成 **raw / base64 / base64url / hex / percent-encoded** 五个变体；替换恒为 `***`。**禁止**用 `security/secret_store.mask_secret_value`（保留首尾字符，等于把泄露面从 0 改成若干个字符，`secret_store.py:504`）。
- 匹配用朴素 `str.replace` 全量替换即可（值条数被 `scrub_max_entries=64` 限制，单条结果 ≤32KB，成本可控）。
- 兜底：若 §12 路径⑥ 的复证变红（说明读码推断错了次序），擦洗点改挂 `on_model_call`，在请求发出前对消息列表做同一套替换，并重跑路径⑤⑥。

## 7. 工具契约

三个工具的 wrapper **一律返回纯字符串（JSON 文本），绝不向宿主抛异常**；外部失败在字符串里给可操作指引。工具默认 `enabled=False`，README 必须写明要在设置里显式开启。

### 7.1 `request_secret`

```python
request_secret(
    label: str,                 # 卡片标题，如 "GitHub webhook secret"
    reason: str,                # 一两句：这是什么用在哪、用户去哪拿
    placeholder: str | None,    # 输入框内的提示
    timeout_seconds: int = 0,   # 0/省略 = 用运维配置 answer_timeout_seconds；显式值范围 30..3600 且仍然获胜
    client_request_id: str | None = None,  # 重试复用同一张卡
) -> str
```

`register_tool(..., description=…, tool_type="internal", target_param="")`。描述里写死 T3 那三条不变式（值绝不返回给你 / ref 一次性 / 别在聊天里索要）。

返回（JSON 文本）：

| 情况 | 返回 |
| --- | --- |
| 用户保存 | `{"status":"saved","ref":"sr_<22字符>","ref_expires_in":300,"hint":"Pass ref to secret_exec or secret_export. You never see the value."}` |
| 用户拒绝 | `{"status":"declined","hint":"The user chose not to provide it. Do not ask again for the same thing."}` |
| 本轮结束/被取代 | `{"status":"cancelled","reason":"run_ended"\|"superseded"}` |
| 超时 | `{"status":"timed_out","hint":"Ask again with request_secret using a NEW client_request_id if still needed."}` |
| 内部失败 | `{"status":"error","message":"<可操作排障文本>"}` |

行为：
- 先建 `SecretRequest`（`request_id = uuid4().hex`、`nonce = token_urlsafe(24)`、`expires_at`），再 `await` 该 future，直到 answered / 超时 / 取消。
- 取消检测：轮询 `get_call_context().cancel_event`（`asyncio.wait` 同时等 future、cancel_event、deadline），命中 → `cancelled/run_ended`。
  - **2026-10-20 真机修订（R60）**：这个 JSON **模型看不到**。用户在 Console 点 Stop 时，宿主协调器抢先把工具结果替换成 `<system-reminder>The tool call was cancelled by the user.</system-reminder>` 并把该块置为 `state:"interrupted"`（18:50:58 实测，Console 显示"工具调用在返回结果前已中断"）。插件侧的取消语义照常发生且可独立取证：记录进 `cancelled` 终态、`/pending` 不再列它、日志一行 `-> cancelled`、此后再答一律 410、vault 里没有值。所以 A6 的判据是"宿主中断 **且** 插件侧这几件事都发生"，**不是**"模型读到 cancelled JSON"；`cancelled/run_ended` 仍然是本工具的正确返回（其它取消路径，如 shutdown hook，会真的把它交出去）。
- 幂等：同一 `(session_id, client_request_id)` 重复调用 → **在同一会话内**复用同一条记录，不重发卡；`agent_id` 不同一律不发给复用者（一张卡绝不服务两个 agent）。若该记录已 `saved` 且 ref **未被消费**，重放返回同一个 `ref`（丢了结果可恢复）；若 ref 已消费 → `{"status":"ref_unavailable", …}`。**cid 由模型自选且无命名空间，因此绝不能做全局键**：否则另一个会话用一个常见重试名（`retry-1`）即可拿到他人那张会话里那张卡的可兑付 ref（2026-10-19 Task 3 评审抓到，spec §7.1 的会话归属是权威）。
- 会话归属：`session_id/agent_id/user_id/channel` 从 `agent_context` 与 `get_call_context()` 取，用于 `/pending` 过滤与审计。
- 同时把答案页 URL 打到 INFO 日志：`http://127.0.0.1:19999/api/secret-input/card/<request_id>`（只有 request_id，**nonce 不进日志**）。

### 7.2 `secret_exec`

```python
secret_exec(
    ref: str,
    command: str,               # 含 {{secret}} 占位符；或用 env_name 给子进程读
    env_name: str = "SECRET",   # 注入到子进程 environ 的变量名
    timeout_seconds: int = 120, # 1..600
    cwd: str | None = None,     # 必须在当前 project 目录内；**无 project 上下文时一律 error（fail-closed）**，让调用方省略 cwd
    max_output_bytes: int = 32768,  # 1024..32768，**stdout+stderr 合计**上限，见下条
) -> str
```

- 消费 ref（一次性）。取到明文后由插件自己 fork：`{{secret}}` 出现的所有位置替换成明文，同时 `env[env_name] = 明文`。**模型写的 command 模板里不含明文。**
- **输出预算是「合计」而不是「每流」**（R47，Task 8 评审实测：每流各 32 KB 时结果可达 65803B，越过宿主 50000B 剪枝阈值，"根本不落盘工件"的论证当场失效）。规则：`a = min(len(stdout), limit)`，`b = min(len(stderr), limit - a)`；仅当 `stderr` 本身会挤掉 stdout 到下限（`min(limit//4, 4096)`）以下时，才各分 `limit//2`。stdout 优先、顺序稳定、单流不超上限、**总和必不超**。截断提示 `[truncated by secret-input]` 必须**预留**在预算之内（先算再切，不是切完再贴），使"发出 ≤ `max_output_bytes`"逐字成立。
- 这条**不是**为了省 token，而是为了让结果始终小于宿主剪枝阈值（recent 50000B，§5），从根上不走"整体落盘 `tool_results/*.txt` 再截断"那条路——那是唯一可能把工具结果原样写进磁盘的通道（§10.1）。**验收断言加在序列化后的 JSON 结果上**（宿主量的就是这个），不是加在两流各自长度上；§10.1 路径⑥ 的用例必须喂"双流都灌满"的样本，否则它只会采到一个小编辑结果而对该形状全盲。
- 返回 `{"status":"ok","exit_code":N,"stdout":"…","stderr":"…"}`，其中 stdout/stderr 一律先过 `scrub`（§6.1）：出现的明文及其变体换成 `***`。
- ref 取不到时区分两种终态（与 T3 的 `ref_unavailable`/`ref_expired` 对齐）：`{"status":"ref_unavailable","hint":"Ask the user again with request_secret."}`（已消费或从来不存在）与 `{"status":"ref_expired","hint":"Ask the user again with request_secret."}`（超过 `ref_ttl_seconds` 未被消费）。`ref_expired` 只出现在这两个消费点；`request_secret` 自己的返回表是 `saved|declined|cancelled|timed_out|error`，外加一条 §7.1 要求的重放态 `ref_unavailable`（重放一个已消费**或**已过期未消费的 ref 都回 `ref_unavailable`，不回 `ref_expired`——Task 7 评审 M-4 记为已知不对称，理由是 §7.1 的幂等语义优先）。
- 无 `{{secret}}` 且不需要 env 的调用不报错（照跑），但 `note` 字段提示"占位符没用上，值只进了 env"。

### 7.3 `secret_export`

```python
secret_export(ref: str, env_key: str) -> str
```

- 校验 `env_key` 匹配 `^[A-Z_][A-Z0-9_]{0,63}$`（R49：`{1,63}` 无根据，两字符名同样合法），再过一个 **export 专属黑名单**（R50）：`PATH`/`HOME`/`IFS`/`LD_PRELOAD`/`LD_LIBRARY_PATH`/`NODE_OPTIONS`/`PYTHONPATH`/`PROMPT_COMMAND`/`BASH_ENV`/`ENV` 一律拒绝。理由是 §5 写明 `set_env_var` 既落 `envs.json` 又同步进宿主进程 environ —— 模型自选 `PATH` 就是一次**持久**的 host 自我拒绝服务。`secret_exec` 的 `env_name` 不设黑名单（子进程作用域，随进程死），此不对称是刻意的。消费 ref，调 `envs.store.set_env_var(env_key, value)`（经 R46 的注入槽，`tools.py` 不 import 宿主）。
  - **2026-10-20 终审 Important 5 修订**：实现里的表比上面这 10 项宽，共 **20 项** —— 另含 `DYLD_INSERT_LIBRARIES`/`SHELL`（macOS dylib 注入与宿主 shell 路径），以及 `GIT_SSH_COMMAND`/`GIT_SSH`/`LD_AUDIT`/`PYTHONSTARTUP`/`PYTHONHOME`/`LESSOPEN`/`PERL5OPT`/`RUBYOPT`。补 VCS 那两个的理由最具体：宿主自己 spawn `["git", *args]`（`qwenpaw/modes/mission/state.py:53`），而 `set_env_var` 既落盘又同步 environ ⇒ 导出成功就是**跨重启**劫持宿主自己的 git 子进程；其余几个同属"解释器/分页器在启动期执行指定文件或命令"这一类。
  - **并且必须写清这张表的性质**：它是"别意外把宿主搞瘫或被跨重启驻留"的护栏，**不是安全边界**。"会导致代码执行的 env 名"这个类不可能被穷举，而模型本来就通过 `secret_exec` 有任意执行能力，所以"不在表里"从来不等于"安全"。这句话同时落在 README 与 SECURITY.md，不许只在代码注释里成立。
- 返回 `{"status":"ok","env_key":"GITHUB_TOKEN","hint":"Later shell tools can use $GITHUB_TOKEN. The value is not in context; it is now in the host env store (Fernet, 0600) and in the host process environment."}`
- 若插件已知该值（刚消费），把它登记给 scrub，供 I4 兜底；**擦洗不覆盖 `$ENV_KEY` 这种引用形式**。
- env 存储失败 → `{"status":"error","message":"env store write failed: <原因>. The ref was not consumed; retry or re-ask."}`（失败不消费 ref）。

## 8. HTTP 端点与卡片

`register_http_router(router, prefix="/secret-input")` ⇒ 全在 `/api/secret-input/*`，因而落在 `AuthMiddleware` 覆盖范围内。

### 8.1 端点

| 方法/路径 | 用途 | 返回 |
| --- | --- | --- |
| `GET /pending?session_id=` | 前端卡片轮询本会话待答请求 | `[{"request_id","label","reason","placeholder","expires_in","answer_url"}]`，**不含 nonce** |
| `GET /pending`（省略 `session_id`） | 只允许 loopback peer：列出全部待答请求，给终端用户/self-check 用 | 同上 |
| `GET /card/{request_id}` | 自包含 HTML 答页（无 CDN、无构建；`type=password` 输入 + 保存/拒绝） | `text/html` |
| `GET /card/{request_id}/meta` | 答页取文案与 nonce（URL 里只有 request_id） | `{label, reason, placeholder, nonce, expires_in, status}` |
| `POST /card/{request_id}/answer` | 提交值或拒绝 | 保存：`{"status":"saved"}`；拒绝：body `{"nonce":…,"decline":true}` → `{"status":"declined"}` |

答案 body 形状对齐 T3 的 `save|decline` 二选一：`{"nonce": "<token_urlsafe(24)>", "value": "<明文>"}`。

**两步取 nonce**：`/pending` 有意不给 nonce（它按会话过滤、调用频率高），所以前端卡片与答页都必须再用 `request_id` 打一次 `/card/{id}/meta` 拿 nonce 才能提交。实现与测试都要按这条串起来，别让卡片直接读 `/pending` 里的 nonce 字段（不存在）。

无 session 上下文时的终端入口写进 README：`curl -s --noproxy '*' http://127.0.0.1:19999/api/secret-input/pending | jq -r '.requests[].answer_url'`（响应是 `{"requests":[…]}` 包裹，不是裸数组——2026-10-19 Task 6 实现时按 §8.1 的响应形状校正本条），再开该 URL 作答（这条同时是 A1/A3 失败时的兜底路径与 §14 的取证手段）。

**答题体不许交给框架自动校验**（Task 6 实测，2026-10-19 本机 FastAPI 0.141.1 / pydantic v2 复现）：校验失败时框架回 `422` 并把**非法输入原样**放进 `detail[].input`。实测六种提交形状，五种泄露 `value`：body 是 JSON 数组、裸 JSON 字符串、`Content-Type: text/plain` 包 JSON（三者 `loc:["body"]` 整块回显）、**缺 `nonce` 字段时把其余字段连同 `value` 一起回显**、`value` 超长时逐字回显；只有"对象合法、某字段违规"这一种不外泄。⇒ `/answer` 必须自己读原始 body、自己校验，错误信息只用闭集固定串；另注：畸形二进制体在框架解析层可抛 `UnicodeDecodeError` 落到宿主异常面。README 的 curl 示例因此**不要**用 `--data-urlencode` 之类会改写 body 的写法，并在威胁模型里写明"答错的代价是响应里出现固定错误码，不出现你填的值"。

### 8.2 访问控制

- `request_id` 与 `nonce` 都是不可猜句柄；`request_id` 单独可得（日志/URL），**答题必须带 nonce**，因此只有真正打开过答页或轮询到 `/pending` 的一侧能提交。
- 每个端点先过插件自检：TCP peer 属 loopback（`request.client.host`）**或** `request.state.user` 已由 `AuthMiddleware` 认证。二者皆无 → 403 `{"detail":"secret-input: loopback or authenticated caller required"}`。配置 `allow_remote=true` 可放宽，默认 `false`，README 里写明风险。
- nonce 比较用 `hmac.compare_digest`。
- 值只做一次性传递：`/answer` 成功后不返回任何值相关字段；无 GET 值的端点（I2）。
- 任何端点、日志、异常都不写值。实现约束：handler 里值只赋给局部变量后立刻进 vault，且 `logger` 调用一律不接 value（用一条 lint 断言覆盖）。
- **答者审计（依 §10.2 第 6 条加）**：每条记录落 `answered_peer_ip`、`answered_at`、`user_agent` 三个字段（**不含值**）。`GET /pending` 与答页在请求已终结时把它们回给前端，卡片显示"已由 `<peer>` 于 `<时刻>` 提交"，让人类能发现被抢答。日志里同样只记这三项。

### 8.3 前端（两层，互不依赖）

1. **会话内卡片**（`ui/index.js`）：`window.QwenPaw.slot.fill("secret-input", "overlay.global", Card)`。Card 用 `host.getCurrentSessionId()` 每 2 秒轮 `/api/secret-input/pending`（走 `host.fetch`，自动带 Bearer + X-Agent-Id），有待答就渲染 antd `Input.Password` 卡片（label/reason/placeholder/倒计时/保存/拒绝），提交 `POST /card/{id}/answer`。**刻意不依赖 `chat.toolRender`** —— 文档只保证它拿到 `result`，工具阻塞期是否挂载未证实，而卡片必须在阻塞期就能填。
2. **答页兜底**：`/card/{request_id}` 的自包含 HTML（无 CDN、无构建）。服务 TUI/ACP/API 驱动、前端 bundle 未加载、或用户要在新标签页填的场景。日志里的 URL 即入口。

可选第三项（v0 不做，留给后续）：`chat.toolRender("secret-input","request_secret", …)` 渲染已完成的终态徽标（`已保存/已拒绝/已超时`）——结果里只有 status+ref，渲染安全。

### 8.4 提示词段

`register_prompt_section("secret_input", after="env_context", provider=…)`，正文对齐 T3 那句：需要 token/API key/签名密钥/密码时只能调 `request_secret`；禁止在聊天里索要；禁止编造；禁止复述任何疑似密钥；ref 只能交给 `secret_exec` / `secret_export`。

## 9. 生命周期

```
SecretRequest:  pending ──answer(save)──> saved ──(ref 交给消费点)──> consumed
                    ├──answer(decline)──> declined
                    ├──deadline 到──────> timed_out
                    └──取消/被取代/shutdown──> cancelled
ref (vault 条目): live ──consume()──> gone（内存对象立即丢弃）
                    └──ref_ttl 到────> expired
```

- GC 时机：**惰性**——每次 `create`、每次 `/pending` 轮询、每次 `consume` 时清扫过期项。不起后台 asyncio 任务（少一个失败面）。
- 上限：同时 pending ≤ 32（超了最老的先 `cancelled/superseded`）；vault 条目 ≤ 64。
- scrub 集合的保留窗：值被消费后仍留在 scrub 集合里 `scrub_ttl`（默认 3600 秒），用于抓后续工具输出里的意外泄露。
- 与宿主 approval 的关系：**不复用 `ApprovalService`**（`app/approvals/` 的决策枚举是 `APPROVED/DENIED/TIMEOUT`，没有自由文本通道，`/answer` 只能落在我们自己的端点上）。插件自持登记表。

## 10. 安全与威胁模型

### 10.1 逐通道判定（不再用一句"尽力而为"笼统带过）

| 通道 | 明文会不会进 | 依据 | 谁来测 |
| --- | --- | --- | --- |
| 工具**入参** | 不会 | 三个工具的参数里没有任何可放值的字段（`request_secret` 只有 label/reason/placeholder/timeout/client_request_id；两个消费点收的是 ref） | `test_invariants` 路径①：schema 断言无自由文本值字段 |
| 工具**结果** | 不会 | `request_secret` 只回 status+ref；`secret_exec` 的 stdout/stderr 先过 scrub | 路径②：结果串 grep |
| 写进 agent **上下文/memory** | 不会 | 插件 mw 在剪枝与 coordinator 的**内侧**，`final_response` 从内层链收集后才截断/落盘（§5 洋葱序行） | 路径⑤ + §14 端到端回读 |
| **磁盘**：`<workspace>/tool_results/*.txt` | 不会（是 `***` 版） | 双重保证：①次序上截断与落盘读的都是擦洗后的 `final_response`；②体量上 `request_secret` 结果 ~150B、`secret_exec` 序列化结果受 §7.2 **合计**预算 ≤32KB ⇒ 都不过 50000B 阈值，这条路本就不该触发（②是 R47 之后才成立的，早先按每流预算可写出 65803B） | **路径⑥**：跑完一轮后 grep 该目录，必须 0 命中；并断言 stub 里出现的 `file_path` 指去的文件不含明文 |
| **磁盘**：`{dialog_path}/*.jsonl`（上下文压缩逐出） | 不会 | `offload_context` 写的是内存里那份 | 同路径⑥ 一起 grep |
| 宿主 **INFO/DEBUG 日志** | 不会（设计禁令） | handler 里值只赋局部变量即入 vault；`logger.*` 一律不接 value | 路径③：logging capture；§14 #5 的日志 grep 扩到 `~/.qwenpaw/logs/` 全量 |
| **SSE / console 前端流** | 值本来就不经这条路 | 工具结果里没有明文可推；`push-messages` 序列化的是 `tool_call.input`（§8.2 已声明值绝不做入参）。**人在自己的浏览器里看见自己刚敲的东西不是泄露**，I1 只管模型侧 | 不需额外门；README 说明这条为什么不算 I1 违反 |
| 异常报文 | 不会 | wrapper 恒返字符串；`secret_exec` 内部 fork 的异常在插件内 catch 成字符串，且**构造异常文本时只带 command 模板不带替换后的实串** | 路径⑦：`test_tools.py` 故意让 fork 失败，断言返回串里无明文 |
| `secret_export` 之后被 `printenv` 读出 | **会**（见 10.2 第 2 条） | env 真进宿主进程 environ；I4 只擦**输出**，擦不掉"模型主动去读 env"这件事本身，除非它把值原样打印出来（那时 scrub 会命中） | §14 #7 只测"引用可用"，不承诺值不可被读回 |
| 多 agent / subagent | 部分 | 每个 agent 装配各自跑 `on_acting` mw ⇒ 子 agent 的**输出**同样被擦；但 `ref` 是全局进程内的，子 agent 也能消费同一个 ref | `test_middleware.py` 用两个不同 `agent_config` 各跑一次 |

### 10.2 不防的（README 的 Threat model 一节要逐条写）
1. 同机进程隔离。宿主 agent 有 `execute_shell_command`，能以同一 uid 读 `~/.qwenpaw.secret/envs.json`（同进程写的 env 它也能 `printenv`）。擦洗是**尽力而为**，不是沙箱：值若被编码/分片/改写进输出（如 `echo ${TOKEN:0:4}`）就抓不住。
2. `secret_export` 的持久面是**设计上的暴露**：值进宿主加密 env 存储 + 进程 environ 长期驻留，供 `$TOKEN` 用法。只想一次性用就别调它，用 `secret_exec`。
3. Python `str` 不可清零；内存取证不在防御范围。TTL 只缩小窗口。
4. 卡片文案由模型写，它可以在 label/reason 里做提示注入诱导用户；UI 上把 label/reason 当不可信文本渲染（不解析 HTML、不插链接）。
5. auth 关闭 + `0.0.0.0` 监听时，LAN 能读到 `/api/*`（宿主既有行为）。本插件的 loopback 门只覆盖自己的 4 个端点，不改宿主全局策略。
6. **本机 agent 可以冒充人类作答**（措辞于 2026-10-19 依第二方评审更正，原句"不能替人类填出一个能用的答案"是过度承诺）。事实是：agent 以 loopback 身份 `GET /card/{id}/meta` 就能拿到 nonce，再 `POST /answer` 提交**它自己编的值**。它仍然拿不到它本来不知道的机密（没有读值的端点），但它能让插件存进一个假值。这不是 HTTP 层能修的——同机同 uid 下浏览器能做的它都能做，任何"human-presence"探测（UA、Sec-Fetch-*、最小间隔）对 curl 都不成立。**本插件的做法是如实记账而不是假装有防**：记录并展示答者的 TCP peer 与时间戳，`/answer` 成功后在卡片侧显示"已由 <peer> 于 <时刻> 提交"，让人类能发现被抢答；README 把这条列为使用前置条件（**索取高价值凭据时，人必须在场并在提交后立刻核对**）。

**明确禁令**：不得用 `security/secret_store.mask_secret_value` 做出站擦洗——它保留前 2-3 字符与后 2-4 字符（`secret_store.py:504`），会把泄露面从 0 变成若干个字符。替换标记恒为 `***`。

### 10.2 追加两条（终审 Important 1/2，2026-10-20 补；README 的 Threat model 同步为 8 条）
7. **`{{secret}}` 写进命令行 = 值进 `argv` = 任何本地用户可读。** `tools.py` 把替换后的整条命令交给 `["/bin/sh","-c",real]`，于是它成为子进程的 `argv`；`/proc/<pid>/cmdline` 模式是 `0444`（本机实测），在子进程整个生命周期内对**所有**本地用户可读，同一段文本还会进 `ps` 输出、auditd 的 `EXECVE` 记录与进程记账。这比第 1 条的"同 uid"宽一档。插件自身无法消除它（内联值进 shell 命令本来就是这个形状），能做的是**把推荐反过来**：文档与 README 示例一律优先 `$<env_name>`（值走子进程环境变量，别的本地用户读不到），`{{secret}}` 只留给"命令确实必须内联"的场合；`tools.py` 的 `note` 字段已经在检测到 `$<env_name>` 未被引用时反向提示。
8. **短于 8 字符的值完全不做出站擦洗。** `scrub.py:_MIN_VALUE_CHARS = 8`，`register()` 对更短的值直接跳过（只记一行不含值的 INFO）。理由是把 3-4 字符的串登记进擦洗集合会让普通输出满地 `***`（假阳性代价高于收益）；代价是**一个 6 位密码会原样出现在 `stdout`**，中间件也不拦。这是设计取舍而非缺陷，但必须与"编码/分片抓不住"并列写进威胁模型，不能只藏在代码里。

## 11. 配置

`api.config` 在热装路径是 `{}`（只有 boot 注入），全部键都要有默认值；优先级 `meta.config` > `api.config` > 环境变量 `QWENPAW_SECRET_INPUT_*` > 默认。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `answer_timeout_seconds` | 600 | 工具侧默认等待（`request_secret` 省略 `timeout_seconds` 或传 0 时生效） |
| `ref_ttl_seconds` | 300 | ref 存活 |
| `scrub_enabled` | true | I4 开关（关掉即"只保证入站"） |
| `scrub_ttl_seconds` | 3600 | 明文留在擦洗集合的时长 |
| `scrub_max_entries` | 64 | 集合上限 |
| `allow_remote` | false | 端点是否接受非 loopback 未认证调用 |
| `public_base_url` | 空 | 只影响 **INFO 日志里那条答案页 URL** 的前缀。空则用 `http://127.0.0.1:19999`（`tools.py:_DEFAULT_BASE_URL` 的硬编码值 —— 终审 Minor 11：插件在进程内读不到宿主真正的监听口，所以宿主监听在别处时**必须**设这个键，否则日志里那条链接是死的；旧文档写的"空则用 `<监听口>`"是过度承诺）。`/pending`、`/card/*` 响应里的 `answer_url` 一律按**当前请求的 Host 头**推导，好让远程控制台拿到能用的链接。工具结果里永远不出现任何 URL（不给模型可点的入口） |

## 12. 测试

离线、dummy 值一律用显眼串（`DUMMY-SK-abcdef123456`），禁真实密钥。`python3 -m unittest discover -s tests -v` + `compileall` 两道 CI 门禁。

| 层 | 断言 | 红对照（证明门不是空门） |
| --- | --- | --- |
| `test_records.py` | 五态迁移、TTL、pending 上限 32、`client_request_id` 幂等复用同一记录 | 把幂等查表打掉 → 两次调用发两张卡 |
| `test_vault.py` | ref 一次性、`consume` 后第二次取不到、TTL 过期报 `ref_expired` | 让 consume 变成读而非删 → 断言必红 |
| `test_scrub.py` | raw/base64/base64url/hex/percent 五种变体全部替换成 `***` | 只测 raw → 变体用例红；确认没在用 `mask_secret_value` |
| `test_middleware.py` | `on_acting` 改写 `ToolResponse`/`ToolChunk` 后 `***`，其他事件原样透传 | 不注册中间件时明文必须原样通过（阳性对照） |
| `test_router.py` | 无 nonce → 403；nonce 错 → 403；保存后 `/pending` 不再列该条；`allow_remote=false` 且非 loopback → 403 | 去掉 loopback 检查 → 403 用例红 |
| `test_tools.py` | 三个 wrapper 恒返字符串；`secret_exec` 返回体经 scrub；`secret_export` 失败不消费 ref | 让 wrapper 抛异常 → 断言"不抛"红 |
| `test_invariants.py` | **I1 专项**：走一遍真实请求流，逐路捕获 ①三个工具的入参 schema 里无任何可承载值的字段 ②所有工具结果串 ③logging 全部记录（挂 capture handler）④`/pending`+`/card/*` 全部响应体 ⑤模拟写入 memory 的 `ToolResponse` 文本 ⑥磁盘：把⑤的输出喂给**真宿主** `ToolResultPruner(tmp_dir)`，断言要么根本没落盘、要么落盘文件不含明文 ⑦故意让 fork 失败时的异常返回串，七者均不含 dummy 明文 | 把某个端点响应改成回显值 → 必红；把 mw 摘掉再跑⑥ → 必红（证明落盘面测的是真顺序，不是空断言） |
| `test_packaging.py` | 调 `packaging/verify_qp_plugin.py`，并跑其中 **7 条**适用的负对照（缺 manifest / entry 文件不存在 / 不导出 plugin / register 抛错 / max==宿主 / max 主版本 3 / min 过高）。**「skill 缺 SKILL.md」那条对本包 N/A**——本包不注册 skill，且该门本身是条件式的（只有存在 `skills/<x>/` 才判），不是本包的红 | 逐条已在 2.2.1 验过会红；本轮在 2.2.2 重跑一遍记录输出，并把 N/A 那条的理由写在脚本注释里（防下一个人以为门坏了） |

端到端验收（真实宿主，§14）额外断言：**用 API 回读会话消息，grep 明文必须为 0 命中**，并跑一次阴性对照（把 dummy 串当普通文本发进聊天，确认回读能命中它，证明 grep 有效而非读错了空数据）。

## 13. 交付与发布

1. 本地：`unittest` 全绿 + `compileall` + `verify_qp_plugin.py` PASS（宿主 python `/home/leo/miniforge3/bin/python3.12`）。
2. 热装（app 在跑，pid 8999/端口 19999）：`POST /api/plugins/install {"source":"<repo 目录>","force":true}`；回读 `GET /api/plugins` 看 `enabled && loaded`，`GET /api/plugins/secret-input/status` 看版本。
3. 安装位核对：`diff -rq -x __pycache__ <源> ~/.qwenpaw/plugins/secret-input/`；`ls ~/.qwenpaw/plugins/ | grep 'secret-input'` 只一份（同 id 多份会重复注册、胜出者不可预期）。
4. 工具默认 disabled：按需在 agent 设置里显式开，或用 `GET /api/tools` 回读确认条目存在。
5. 发布 zip：`packaging/build_zip.sh` → `secret-input-0.1.0.zip`，单顶层目录 `secret-input/`（同时满足 in-app `_find_plugin_dir` 与离线 CLI 的"唯一顶层目录"）；打包前 `find secret-input -type l` 必须为空（zip 不解 symlink，成员会静默变成"内容为目标路径的普通文件"）。
6. 通用仓库标准走 repo-standards-alignment：README 双语互挂链接（中文是 `README.zh-CN.md` 完整对等文档）、release 标题 = tag 名（`v0.1.0`）、发布前密钥审计、ruff 显式 select（wrapper 返字符串不抛是刻意设计，需豁免 BLE 族）。
7. README 必须写：工具首装默认 disabled；安装命令按 app 在跑（本地 zip 可 / 目录 / URL）与不在跑（只目录或 zip URL）分别给；三个工具的用法示例 + 何时用 `secret_exec` 而非 `secret_export`；§10 残余风险逐条。

## 14. 验收标准（全绿才算交付）

1. `verify_qp_plugin.py` 对本包 PASS / exit 0，8 条负对照仍会红（输出留档）。
2. `unittest` 全绿，其中 `test_invariants.py` 覆盖 I1 的四条路径。
3. 真机热装成功、`enabled/loaded` 为 true、`diff -rq` 与源码一致、`plugins/` 下单份。
4. 真会话里让 agent 跑一次需要密钥的任务：卡片在**阻塞期**出现，用户填完，工具返回 `saved`+ref；随后 `secret_exec` 跑通一条真实带鉴权的命令（dummy 服务即可）并返回 `***` 化输出。
5. 回读该会话的消息，并对 `~/.qwenpaw/logs/`（全量，含轮转 `.1/.2/.3`）与 `<workspace>/tool_results/*.txt`、`<workspace>/**/dialog*/*.jsonl` 三处做 grep，对 dummy 明文一律 **0 命中**（且阳性对照证明 grep 有效：把同一串当普通文本发进聊天，回读必须命中它）。
6. 二次消费同一 ref 得到 `ref_unavailable`，报文含"Ask the user again with request_secret"。
7. `secret_export` 后在后续 shell 工具里 `$ENV_KEY` 可用、模型侧无明文。
8. 非 loopback 且未认证的 `/answer` 请求被 403 拒。

## 15. 实现期待验证的假设（不做成"看起来已经成立"）

| # | 假设 | 怎么验 | 若不成立 |
| --- | --- | --- | --- |
| A1 | `slot.fill(…,'overlay.global')` 在阻塞期仍在轮询（overlay 常驻挂载）。**前半已验**：`type:"general"` + `entry.frontend` 会被 Console 加载（bundle 里 `t.map(s=>Pb(Tb(s.id,s.frontend_entry)))` 按 `frontend_entry` 逐个加载，不按 `plugin_type` 过滤；`plugin_type!==expectedType` 那道判据只属于 PawApp 的 `expectedType==="app"` 通路；服务端 `app/routers/frontend_plugin.py:36-58` 也只列 `entry.frontend`） | 起一个真请求 + Playwright 看卡片出现且 `/pending` 有请求记录；并回读 `GET /frontend_plugin` 含本插件 | 退化为只用答页；卡片改挂 `chat.rightHeader` 徽标 + 用户点击开页 |
| A2 | ~~我们的 `on_acting` 看到的是最终写进 memory 的那份~~ → **本轮已由读码定为成立**：洋葱序 built-ins 在外、plugins 在内（`runtime/builder.py:1336-1343`），`final_response` 由 coordinator `_drain` 从内层链收集（`tool_calls/_coordinator.py:741-748/840`），截断+落盘在其后（`:877`） | 不靠推理下结论：`test_invariants` 路径⑥用**真宿主 `ToolResultPruner`** 复证（摘掉 mw 必须变红） | 若复证红，把擦洗点改到 `on_model_call`（请求发出前）并重测 |
| A3 | `GET /card/{id}` 的 HTML 页能加载并 POST。**CSP 风险已排**：`app/_app.py` 只挂 ResponseCompression/AgentContext/Auth/RuntimeBoundary/CORS 五个中间件，**无 CSP、无 X-Frame-Options**（`:826-838`） | Playwright 打开该 URL 提交一次，断言 200 与卡片终态；顺查一次响应头确认没多出安全头 | 去掉 HTML 页，只留 `/pending`+`/answer` 的 JSON 契约 + 文档化 curl |
| A4 | 插件工具无超时 ⇒ 阻塞 600 秒不被宿主掐 | 真跑一次 300 秒不答的请求，看是否收到 `timed_out` 而非宿主 kill | 把上限调低并在描述里写清 |
| A5 | `register_prompt_section(after="env_context")` 在 2.2.2 上仍是合法 anchor | 加载后回读拼装出的系统提示含该段 | 换 anchor 为 `workspace` 或用 `on_system_prompt` |
| A6 | 阻塞期间 SSE 连接保持、且用户点 Stop 时取消真的传到 `get_call_context().cancel_event`（否则卡片会挂着一个永远不结束的请求） | 真跑：发起 → 阻塞 → 前端点 Stop → 断言**记录被置 `cancelled` 终态、`/pending` 不再列它、日志一行 `-> cancelled`、此后再答 410**。（R60 修订：原判据"断言工具返回 `cancelled/reason=run_ended`"在真机上不成立 —— 宿主在 Stop 时抢先把工具结果替换成自己的 `<system-reminder>The tool call was cancelled by the user.</system-reminder>` 并置 `state:"interrupted"`，2026-10-20 18:50:58 实测；插件侧语义完整，判据改看插件侧那四件事。） | 改为轮询 `cancel_event` + `asyncio.wait` 三方竞速的兜底实现；并在 §8.2 补"答页对已结束请求回 `already_answered`" |
| A7 | `RuntimeBoundaryMiddleware` 与 `ResponseCompressionMiddleware` 不把我们 4 个端点的响应改写得无法解析（尤其 HTML 页与 JSON 的 `Content-Type`） | 对四个端点各发一次，断言状态码与可解析性 | 关掉该路径的压缩（响应体极小，本也不会压）或改返回纯文本 |

## 16. 命名与边界备忘

- 模块 id `secret-input`（kebab）；Python 包目录 `backend/` 不带连字符，避免 import 名问题。
- 工具名定为 `request_secret` / `secret_exec` / `secret_export`（不带插件前缀，与 T3 同名以便迁移直觉）。2026-10-19 对运行中的宿主回读 `GET /api/tools`（63 个工具）实测三名**均未被占用**，最接近的是 `get_token_usage`。loader 的治理注册对已存在条目按"已存在不动"处理，所以日后撞名要在 README 记明取舍。
- HTTP prefix `/secret-input` 由 `register_http_router` 独占校验（重复 prefix 抛 `ValueError`）。

## 17. 第二方评审处置（CodeBuddy，2026-10-19）

派单方式：`cbc -p -y`，工作目录 `/tmp/cb-review`，喂给它的是 spec + 16 个宿主源文件的**逐字节副本**（保留原相对路径与行号），因此它碰不到真实宿主树与仓库；事后 `git status` 确认仓库干净。它的总判定是「需修订后实现 / 1 blocker」。**逐条自己复验后才采纳**，其中一条它的机制是错的：

| 它的条目 | 我的复验结论 | 处置 |
| --- | --- | --- |
| [blocker] `tool_results_dir` 磁盘副本未覆盖 + scrub 与剪枝中间件优先级未钉死，"若剪枝更靠内则明文落盘" | **机制否证，问题升格为约束**。`runtime/builder.py:1336-1343` 的 docstring 与 append 次序（`:1372` 剪枝最外、`:1418-1422` 插件在内）决定**插件 mw 不可能排到剪枝外侧，也无需**；`final_response` 由 `_coordinator.py:741-748/840` 从内层链收集，截断+落盘发生在 `:877` 之后 ⇒ 磁盘副本天然已是擦洗后。但它指出的**这条盘路我原先没测**成立，而且我原先没意识到截断 stub 里带 `file_path`（`agents/tools/utils.py:272-283`）⇒ 模型可 read_file 读回，所以这条路必须被钉死而不是靠推理 | §5 新增两行事实；§7.2 新增 32KB 输出上限（让截断根本不触发）；§10.1 路径⑥；§12 `test_invariants` 扩到七路且用**真宿主 `ToolResultPruner`** 复证、摘掉 mw 必须变红；§14 #5 grep 面扩到 `tool_results/` 与 dialog JSONL；§15 A2 从"待验"改为"已验+测试复证" |
| [major] 泄露面清单缺项、只用一句"尽力而为"带过 | 成立 | §10 重写为 10.1 逐通道表 + 10.2 残余风险 |
| [major] §10 item 6「agent 不能替人类填出一个能用的答案」过度承诺、与 §8 矛盾 | **成立，我错了**。`/card/{id}/meta` 回 nonce，loopback 侧的 agent 一次 curl 即可抢答。任何 UA/Sec-Fetch/最小间隔的"human-presence"探测对 curl 都不成立，不装样子 | §10.2 第 6 条改写为事实陈述 + §8.2 新增答者审计三字段（peer/时刻/UA，卡片显示，可发现抢答） |
| [major] `test_invariants` 恒绿空门（未覆盖磁盘/SSE） | 前半成立（已并入路径⑥）；**SSE 那半否证**：SSE/console 流不是模型上下文路径，且我们的工具结果里本就没有明文可推，人在自己浏览器看见自己敲的值不算违反 I1 | 磁盘已补；SSE 在 §10.1 里明确标为"不算 I1 通道"并给出理由，避免下一个人误加门 |
| [minor] `type:"general"` + `entry.frontend` 是否被加载未证实 | **已验成立**：加载按 `frontend_entry` 逐个 `import`，不按 `plugin_type` 过滤（`plugin_type!==expectedType` 那道判据属 PawApp 的 `expectedType==="app"` 通路）；服务端 `app/routers/frontend_plugin.py:36-58` 同样只看 `entry.frontend` | §15 A1 写入该事实，仍保留 `GET /frontend_plugin` 回读作验收 |
| [minor] 自包含 HTML 答页可能被全局 CSP 挡 | **已验无此风险**：`app/_app.py:826-838` 只有 ResponseCompression / AgentContext / Auth / RuntimeBoundary / CORS 五个中间件，无 CSP、无 X-Frame-Options | §15 A3 更新；保留顺查响应头一次 |
| [minor] `verify_qp_plugin` 的「skill 缺 SKILL.md」负对照与本包"不注册 skill"矛盾 | 成立（该门本身是条件式的，我 §12 写成像是要跑的 8 条） | §12 改为 7 条适用 + 1 条标 N/A 并写明理由 |
| [minor] `ref_expired` 未出现在消费点返回表 | 成立 | §7.2 补两态并写明"只在消费点返回，不在 `request_secret` 返回表里" |
| [minor] A 系列缺"阻塞期 SSE 保活 + Stop 取消是否传到 cancel_event" | 成立，且这是最可能在真机炸的一条 | 新增 §15 A6，并连带加 A7（宿主两个 HTTP 中间件不改坏我们 4 个端点） |
| [minor] §5 `app/_app.py:831` 引用它无法核验 | **我方独立已验**（`app.add_middleware(AuthMiddleware)` 确在 `:831`）—— 它只是没拿到那个文件 | 不改 |

它「我不确定的」五条里，四条我已在宿主侧查到（前端加载判据、剪枝优先级与落盘先后、`ToolResultPruner` 位置在 `agents/tools/utils.py:134` 而非 agentscope、CSP），第五条（SSE 是否复用同一 `ToolResponse` 对象）**没有查**，理由见上：那条不是上下文路径，且工具结果里不含明文，所以即便复用同一个对象也不构成 I1 风险。
