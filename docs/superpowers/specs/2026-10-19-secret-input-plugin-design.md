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

- **I1 入站不可见**：值的合法传输路径只有 `POST /api/secret-input/card/{request_id}/answer` 的 body。它不出现在任何工具入参、工具结果、聊天转录、会话上下文、`qwenpaw.log`、异常报文里。
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

## 7. 工具契约

三个工具的 wrapper **一律返回纯字符串（JSON 文本），绝不向宿主抛异常**；外部失败在字符串里给可操作指引。工具默认 `enabled=False`，README 必须写明要在设置里显式开启。

### 7.1 `request_secret`

```python
request_secret(
    label: str,                 # 卡片标题，如 "GitHub webhook secret"
    reason: str,                # 一两句：这是什么用在哪、用户去哪拿
    placeholder: str | None,    # 输入框内的提示
    timeout_seconds: int = 600, # 等人回答的上限，范围 30..3600
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
- 幂等：同一 `client_request_id` 重复调用 → 复用同一条记录，不重发卡；若该记录已 `saved` 且 ref **未被消费**，重放返回同一个 `ref`（丢了结果可恢复）；若 ref 已消费 → `{"status":"ref_unavailable", …}`。
- 会话归属：`session_id/agent_id/user_id/channel` 从 `agent_context` 与 `get_call_context()` 取，用于 `/pending` 过滤与审计。
- 同时把答案页 URL 打到 INFO 日志：`http://127.0.0.1:19999/api/secret-input/card/<request_id>`（只有 request_id，**nonce 不进日志**）。

### 7.2 `secret_exec`

```python
secret_exec(
    ref: str,
    command: str,               # 含 {{secret}} 占位符；或用 env_name 给子进程读
    env_name: str = "SECRET",   # 注入到子进程 environ 的变量名
    timeout_seconds: int = 120, # 1..600
    cwd: str | None = None,     # 必须在当前 workspace/project 目录内，否则 error
) -> str
```

- 消费 ref（一次性）。取到明文后由插件自己 fork：`{{secret}}` 出现的所有位置替换成明文，同时 `env[env_name] = 明文`。**模型写的 command 模板里不含明文。**
- 返回 `{"status":"ok","exit_code":N,"stdout":"…","stderr":"…"}`，其中 stdout/stderr 一律先过 `scrub`（§8.3）：出现的明文及其变体换成 `***`。
- 无 `{{secret}}` 且不需要 env 的调用不报错（照跑），但 `note` 字段提示"占位符没用上，值只进了 env"。

### 7.3 `secret_export`

```python
secret_export(ref: str, env_key: str) -> str
```

- 校验 `env_key` 匹配 `^[A-Z_][A-Z0-9_]{1,63}$`，消费 ref，调 `envs.store.set_env_var(env_key, value)`。
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

无 session 上下文时的终端入口写进 README：`curl -s --noproxy '*' http://127.0.0.1:19999/api/secret-input/pending | jq -r '.[].answer_url'`，再开该 URL 作答（这条同时是 A1/A3 失败时的兜底路径与 §14 的取证手段）。

### 8.2 访问控制

- `request_id` 与 `nonce` 都是不可猜句柄；`request_id` 单独可得（日志/URL），**答题必须带 nonce**，因此只有真正打开过答页或轮询到 `/pending` 的一侧能提交。
- 每个端点先过插件自检：TCP peer 属 loopback（`request.client.host`）**或** `request.state.user` 已由 `AuthMiddleware` 认证。二者皆无 → 403 `{"detail":"secret-input: loopback or authenticated caller required"}`。配置 `allow_remote=true` 可放宽，默认 `false`，README 里写明风险。
- nonce 比较用 `hmac.compare_digest`。
- 值只做一次性传递：`/answer` 成功后不返回任何值相关字段；无 GET 值的端点（I2）。
- 任何端点、日志、异常都不写值。实现约束：handler 里值只赋给局部变量后立刻进 vault，且 `logger` 调用一律不接 value（用一条 lint 断言覆盖）。

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

**防的**：密钥经模型之手（入参/结果/转录/上下文/发往供应商的请求体）；意外泄露（`printenv`、`cat /proc/self/environ`、`cat ~/.aws/credentials` 之类的输出）经 I4 被抹掉；LAN 侧提交伪造答案（默认 loopback-only + nonce）。

**不防的（README 的 Threat model 一节要逐条写）**：
1. 同机进程隔离。宿主 agent 有 `execute_shell_command`，能以同一 uid 读 `/home/leo/.qwenpaw.secret/envs.json`（同进程写的 env 它也能 `printenv`）。擦洗是**尽力而为**，不是沙箱：值若被编码/分片/改写进输出（如 `echo ${TOKEN:0:4}`）就抓不住。
2. `secret_export` 的持久面是**设计上的暴露**：值进宿主加密 env 存储 + 进程 environ 长期驻留，供 `$TOKEN` 用法。只想一次性用就别调它，用 `secret_exec`。
3. Python `str` 不可清零；内存取证不在防御范围。TTL 只缩小窗口。
4. 卡片文案由模型写，它可以在 label/reason 里做提示注入诱导用户；UI 上把 label/reason 当不可信文本渲染（不解析 HTML、不插链接）。
5. auth 关闭 + `0.0.0.0` 监听时，LAN 能读到 `/api/*`（宿主既有行为）。本插件的 loopback 门只覆盖自己的 4 个端点，不改宿主全局策略。
6. 握着 shell 的本机 agent 可以 `curl /api/secret-input/pending` 读到 label/reason/answer_url（读不到 nonce、读不到值）。它因此能知道"正在等什么密钥"，但不能替人类填出一个能用的答案，也取不出任何已存值 —— 不违反 I1/I2。这一条只是透明度声明：要在同机上做出"agent 完全看不见有请求在等"，需要 OS 级隔离，本插件不提供。

**明确禁令**：不得用 `security/secret_store.mask_secret_value` 做出站擦洗——它保留前 2-3 字符与后 2-4 字符（`secret_store.py:504`），会把泄露面从 0 变成若干个字符。替换标记恒为 `***`。

## 11. 配置

`api.config` 在热装路径是 `{}`（只有 boot 注入），全部键都要有默认值；优先级 `meta.config` > `api.config` > 环境变量 `QWENPAW_SECRET_INPUT_*` > 默认。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `answer_timeout_seconds` | 600 | 工具侧默认等待 |
| `ref_ttl_seconds` | 300 | ref 存活 |
| `scrub_enabled` | true | I4 开关（关掉即"只保证入站"） |
| `scrub_ttl_seconds` | 3600 | 明文留在擦洗集合的时长 |
| `scrub_max_entries` | 64 | 集合上限 |
| `allow_remote` | false | 端点是否接受非 loopback 未认证调用 |
| `public_base_url` | 空 | 只影响 **INFO 日志里那条答案页 URL** 的前缀。空则用 `http://127.0.0.1:<监听口>`（本机实测 `--port 19999`）。`/pending`、`/card/*` 响应里的 `answer_url` 一律按**当前请求的 Host 头**推导，好让远程控制台拿到能用的链接。工具结果里永远不出现任何 URL（不给模型可点的入口） |

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
| `test_invariants.py` | **I1 专项**：走一遍真实请求流，捕获①所有工具结果串②logging 全部记录③`/pending`+`/card/*` 所有响应体④模拟写入 memory 的 `ToolResponse` 文本，断言四者均不含 dummy 明文 | 把某个端点响应改成回显值 → 必红 |
| `test_packaging.py` | 调 `packaging/verify_qp_plugin.py`，并跑技能里那 8 条负对照（缺 manifest / entry 文件不存在 / 不导出 plugin / register 抛错 / skill 缺 SKILL.md / max==宿主 / max 主版本 3 / min 过高） | 逐条已在 2.2.1 验过会红；本轮在 2.2.2 重跑一遍记录输出 |

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
5. 回读该会话的消息与 `~/.qwenpaw/logs/qwenpaw.log`，对 dummy 明文 grep **0 命中**（且阳性对照证明 grep 有效）。
6. 二次消费同一 ref 得到 `ref_unavailable`，报文含"Ask the user again with request_secret"。
7. `secret_export` 后在后续 shell 工具里 `$ENV_KEY` 可用、模型侧无明文。
8. 非 loopback 且未认证的 `/answer` 请求被 403 拒。

## 15. 实现期待验证的假设（不做成"看起来已经成立"）

| # | 假设 | 怎么验 | 若不成立 |
| --- | --- | --- | --- |
| A1 | `slot.fill(...,'overlay.global')` 在阻塞期仍在轮询（overlay 常驻挂载） | 起一个真请求 + 浏览器 Playwright 看卡片出现且 `/pending` 有请求记录 | 退化为只用答页；卡片改挂 `chat.rightHeader` 徽标 + 用户点击开页 |
| A2 | 优先级的洋葱序里，我们的 `on_acting`(priority=50) 能看到最终写进 memory 的那份 `ToolResponse` | 用两个中间件（50/150）各自打标记，比对进 `agent.state.context` 的那份含哪个 | 改挂 `on_model_call` 在请求发出前擦 |
| A3 | `GET /card/{id}` 的 HTML 页在宿主静态/CSP 策略下能正常加载并 POST | Playwright 打开该 URL 提交一次，断言 200 与卡片终态 | 去掉 HTML 页，只留 `/pending`+`/answer` 的 JSON 契约 + 文档化 curl |
| A4 | 插件工具无超时 ⇒ 阻塞 600 秒不被宿主掐 | 真跑一次 300 秒不答的请求，看是否收到 `timed_out` 而非宿主 kill | 把上限调低并在描述里写清 |
| A5 | `register_prompt_section(after="env_context")` 在 2.2.2 上仍是合法 anchor | 加载后回读拼装出的系统提示含该段 | 换 anchor 为 `workspace` 或用 `on_system_prompt` |

## 16. 命名与边界备忘

- 模块 id `secret-input`（kebab）；Python 包目录 `backend/` 不带连字符，避免 import 名问题。
- 工具名定为 `request_secret` / `secret_exec` / `secret_export`（不带插件前缀，与 T3 同名以便迁移直觉）。2026-10-19 对运行中的宿主回读 `GET /api/tools`（63 个工具）实测三名**均未被占用**，最接近的是 `get_token_usage`。loader 的治理注册对已存在条目按"已存在不动"处理，所以日后撞名要在 README 记明取舍。
- HTTP prefix `/secret-input` 由 `register_http_router` 独占校验（重复 prefix 抛 `ValueError`）。
