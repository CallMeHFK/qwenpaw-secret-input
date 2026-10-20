# secret-input 真机验收记录（2026-10-19 spec §14 / A4 A6 A7）

- 宿主：`qwenpaw app --host 0.0.0.0 --port 19999`（pid 74210，2026-10-20 10:08 起在跑）
- 插件版本：0.1.0（分支 `feat/secret-input-plugin`，验收开始时的 HEAD `2fc84a0`）
- 离线解释器：`/home/leo/miniforge3/bin/python3.12`
- 本文件只记**真机读数**。凡是尚未执行的步骤显式标 `PENDING`，不预填结论。
- **公开发布前的脱敏**：本机标识符已替换为占位符 —— 会话 id → `<SESSION_ID>`／日志里的 8 位前缀 → `<SESSION8>`、request_id → `<REQUEST_ID>`／`<REQ8>`、一次性 ref → `sr_<REDACTED>`、非 loopback 探针地址 → `192.0.2.7`（RFC 5737 文档保留段，原来是真的内网地址）、workspace/agent 名清单 → 只留计数。**读数形状、时间戳与判定结论一字未改**；`DUMMY-SK-abcdef123456` 是 `CONTRIBUTING.md` 里约定的公开哑值，不是脱敏遗漏。

## Step 1 离线门（热装前）

```console
$ cd /home/leo/develop/secret_input && /home/leo/miniforge3/bin/python3.12 -m unittest discover -s tests -t . 2>&1 | tail -3 && /home/leo/miniforge3/bin/python3.12 -m compileall -q backend && /home/leo/miniforge3/bin/python3.12 packaging/verify_qp_plugin.py . 2>&1 | tail -3
Ran 273 tests in 11.944s

OK
compileall: rc=0
loaded: id=secret-input version=0.1.0 enabled=True instance=SecretInputPlugin
scheduled startup hooks (4): register_tool_secret-input_request_secret@50, register_tool_secret-input_secret_exec@50, register_tool_secret-input_secret_export@50, secret_input_sweep@100
PASS: plugin loads under the real PluginLoader (register(api) ran, startup hooks did not)
```

`OK` / compileall 无输出（rc=0） / `PASS`。273 条比计划写的 271 条多 2 条：R55 的 manifest 漂移门 + R56 的日志接管门。

## Step 2 热装

```console
$ curl -s --noproxy '*' -X POST http://127.0.0.1:19999/api/plugins/install \
    -H 'Content-Type: application/json' \
    -d '{"source":"/home/leo/develop/secret_input","force":true}'
{... "id": "secret-input", "loaded": true ...}
```

宿主日志（`~/.qwenpaw/qwenpaw.log`）：

```text
2026-10-20 10:13:53 | INFO | miniforge3/lib/python3.12/site-packages/qwenpaw/plugins/loader.py:806 | ✓ Loaded plugin 'secret-input' successfully
2026-10-20 10:13:53 | INFO | .qwenpaw/plugins/secret-input/backend/main.py:172 | secret-input: swept (pending=0 vault=0 scrub=0)
```

第二条是 **R56 修复后的新证据**：修复前同一轮热装只有 loader 行，插件的 INFO 一条也进不了宿主日志（见下）。

## Step 3 装配回读

```console
$ curl -s --noproxy '*' http://127.0.0.1:19999/api/plugins | python3 -c "...过滤 id=='secret-input'..."
{'id': 'secret-input', 'enabled': True, 'loaded': True, 'version': '0.1.0', 'frontend_entry': 'ui/index.js'}

$ curl -s --noproxy '*' http://127.0.0.1:19999/api/plugins/secret-input/status
{"id":"secret-input","loaded":true,"enabled":true,"version":"0.1.0"}

$ curl -s --noproxy '*' http://127.0.0.1:19999/api/tools | python3 -c "...三个工具名..."
missing: set()
present: ['request_secret', 'secret_exec', 'secret_export']

$ ls ~/.qwenpaw/plugins/ | grep -c '^secret-input$'
1
$ diff -rq -x __pycache__ /home/leo/develop/secret_input/backend ~/.qwenpaw/plugins/secret-input/backend
diff: clean
```

**计划书 Step 3 的两处修正（实测）**：

1. `GET /frontend_plugin` **不是 JSON**，它回的是 Console 的 SPA HTML。前端 bundle 的真实证据在 `GET /api/plugins` 的 `frontend_entry` 字段：`ui/index.js`。
2. `/api/tools` 的可见性取决于 **agent 配置的 `builtin_tools`**。热装时宿主只从 `plugin.json` 的 `meta.tools[]`（或旧形 `meta.tool_name`）同步到各 agent（`_sync_plugin_tools_to_agents`），`register_tool` 的延迟 hook 本身不会让它们出现在 `/api/tools`。补 `meta.tools` 后三个工具立刻可见（`enabled: false`，需人工打开）。
3. **（R58 后追加）`/api/tools` 不能当作"注册成功"的证据。** 该路由只读 `load_agent_config(...).tools.builtin_tools`，与运行时 `ToolDescriptor` 无关；所以它既看不到回滚（R55 的复验当时就是这么误判的），也看不到我们传的 `description`/`icon`（`_write_tool_config` 对**已存在**的条目只记一行 `already in agent '<id>' config, skipping`，不覆盖字段 ⇒ 页面上的 `description: ""`、`icon: "🔧"` 是配置条目的默认值）。注册成功的判据是宿主日志三类行：`Registered tool function '<name>' to tools module`、`Attached ToolDescriptor to '<name>'`、`Injected '<name>' into workspace '<agent>' ToolRegistry`，或一次真实工具调用。

## Step 4 服务端四端点与鉴权门

```console
pending(loopback)=200
{"requests":[]}
card(bad-id)=200
链接不可用
meta(unknown)=404
answer(unknown)=404
non-loopback probe from 192.0.2.7:
remote=403
```

与期望一致：`/pending` 200；坏 request_id 的答页是 200 + “链接不可用”文案；未知 id 的 `meta`/`answer` 均 404；非 loopback 且 `allow_remote=false` 时 403，响应体为
`{"detail":"secret-input: loopback or authenticated caller required"}`。

403 那一行同时暴露了 R56：请求体是我们 router 的固定文案，但 `logger.warning("secret-input access denied: ...")` 在宿主日志里**查不到**。修复后同一探针的日志：

```text
2026-10-20 10:13:57 | WARNING | .qwenpaw/plugins/secret-input/backend/router.py:131 | secret-input access denied: peer=192.0.2.7 path=/api/secret-input/pending
```

## 真机验收发现

### R55 — 热装后 `/api/tools` 看不到插件工具（已修）

- 症状：热装成功、`/api/plugins` 里 `enabled/loaded` 均 true、router 已挂载（`/pending` 200），但 `/api/tools` 里 `missing: {request_secret, secret_exec, secret_export}`。
- 根因：`/api/tools` 读的是当前 agent 的 `builtin_tools` 配置；热装只从 manifest 的 `meta.tools[]` 同步（`_sync_plugin_tools_to_agents`），装载期的 `_write_tool_config` 在没有 agent 上下文时被跳过。`plugin.json` 原先没有 `meta.tools`。
- 修复：`plugin.json` 增加三个工具条目；`tests/test_main.py::ManifestToolListTest` 钉住 manifest 名字与 `TOOL_DESCRIPTIONS` 一致，防止再次漂移。
- 复验：重装后 `missing: set()`，三个工具均出现在 `/api/tools`（`enabled: false`，首装默认关闭是宿主既定行为）。**这条复验当时是假阳性**：可见性只证明 `meta.tools` 同步成功，同一轮里三个工具的运行时注册其实正被整体回滚（见 R58）。

### R56 — 插件日志进不了宿主日志（已修）

- 症状：插件 `plugin_secret_input.*` 的 INFO/WARNING 在 `~/.qwenpaw/qwenpaw.log` 中零命中；用非 loopback 探针触发 `router.py` 的 403 分支（确定执行了 `logger.warning`）仍查不到。
- 根因：宿主把文件 handler 挂在 `qwenpaw` logger 上并设 `propagate = False`（`qwenpaw/utils/logging.py:256-258`），而插件由 loader 以 `plugin_<id>` 命名空间导入，记录到不了 `qwenpaw`，root 上也没有文件 handler。
- 影响：spec §8.1 承诺的“答案页 URL 打到 INFO 日志”、§8.3 的答者审计行、以及计划 Step 9 期望的 `-> cancelled` 日志证据全部落空。
- 修复：`backend/main.py` 新增 `_adopt_host_log_handlers()`（在 `register()` 最前调用，线程/重复装载安全）：仅当模块名为 loader 的 `plugin_*` 形时，把宿主 logger 的 handler 挂到插件包 logger 上，INFO 级别与去重都做；直接 import（测试）不触碰进程 logging。失败只降级可观测性，不阻断装配。
- 复验：见 Step 2 的 `secret-input: swept` 与 Step 4 的 `access denied` 两行。

### R58 — 热装把三个工具的运行时注册整体回滚（已修）

- 症状：`/api/tools` 里三个工具都在，但会话中模型拿不到它们；每一轮热装宿主日志都留下三条 ERROR（10:13:53、10:21:09 两轮完全相同）。
- 证据（逐字）：

```text
2026-10-20 10:21:09 | ERROR | miniforge3/lib/python3.12/site-packages/qwenpaw/plugins/api.py:948 | Failed to register tool 'request_secret' after governance sync (rolled back): 'method' object has no attribute '_tool_descriptor'
    _bridge_to_runtime(
  File "/home/leo/miniforge3/lib/python3.12/site-packages/qwenpaw/plugins/api.py", line 168, in _bridge_to_runtime
    tool_func._tool_descriptor = desc  # type: ignore[attr-defined]
AttributeError: 'method' object has no attribute '_tool_descriptor'
```

- 根因：宿主 `_bridge_to_runtime` 的最后一步是给 callable 挂 `_tool_descriptor`，而**绑定方法没有 `__dict__`**，赋值即抛。`register()` 传下去的正是 `self.tools.request_secret` 这类绑定方法。异常落进宿主的 except 分支：删掉 `tools_module` 上的属性、`_unbridge_from_runtime`、`_release_tool_registration`（连治理白名单一起释放）⇒ 三个工具"看得见、叫不动"，`meta.tools` 同步出来的配置条目还在，所以 `/api/tools` 一切正常。
- `enabled` 不是触发条件：它只在那一行**之后**才进 `ToolDescriptor(enabled_by_default=…)` 和 `_write_tool_config`。把 `enabled=False` 改成 `True` 修不了回滚（10:21:09 那轮即反证），还会破坏 spec §10.2 与 README 承诺的"默认关闭"⇒ 源码保持 `enabled=False`。
- 连带查出的第二处契约：`local_workspace.list_tools()` 只把 `desc.func` 交给 agentscope 的 `FunctionTool`，工具名取 `__name__`、模型可见描述取 docstring，`ToolDescriptor.description` **从不参与** schema。所以包装体必须自带 `__name__`/`__doc__`：裸 `functools.partial` 在 `FunctionTool` 里直接 `AttributeError: 'functools.partial' object has no attribute '__name__'`；只补 `__name__` 则描述回落成 `partial(func, *args, **keywords) - new function with partial…`，且每个参数的说明被抹掉（实测三者 schema 逐字对比）。
- 修复：`backend/main.py` 新增 `_host_callable(name, method)` —— `functools.partial` 包装（有 `__dict__`，且保留 `inspect.signature` 与协程识别），并挂 `__name__ = name`、`__doc__ = TOOL_DESCRIPTIONS[name]`；三处 `register_tool` 改传包装体。`tests/test_main.py::HostToolBridgeContractTest` 三条门：负对照（裸绑定方法必须炸，复现宿主 `api.py:168` 同一行）、桥接存活（descriptor 的 `name`/`async_execution`/`description`，且 `enabled_by_default is False`）、模型可见 schema（`FunctionTool` 的 name、描述、required 参数）。
- 复验（2026-10-20 15:27:01 热装进同一个宿主进程，**未重启**）：

```text
INFO | .../qwenpaw/plugins/api.py:910 | Registered tool function 'request_secret' to tools module
INFO | .../qwenpaw/plugins/api.py:169 | Attached ToolDescriptor to 'request_secret'
INFO | .../qwenpaw/plugins/api.py:192 | Injected 'request_secret' into workspace 'default' ToolRegistry
```

  计数：`Registered tool function` 3 条、`Attached ToolDescriptor` 3 条、`Injected … ToolRegistry` 24 条（8 个 workspace × 3 个工具；workspace 名略）。同一轮里 `rolled back`、`_tool_descriptor`、`Failed to register tool`、`is denied by policy` 全部 0 命中。离线门：276 tests OK、`ruff` All checks passed、`compileall` 无输出、`node --check` 通过、`verify_qp_plugin.py` PASS。
- 残留状态说明：`default` 的三条 `enabled: true` 不是本次注册写的 —— `_sync_plugin_tools_to_agents` 只会写 `enabled=False`，`_write_tool_config` 对已存在条目只 `skipping`，所以只能是 `PATCH /api/tools/{name}/toggle`（UI 开关）改的；其余 9 个 agent 的条目仍是 `enabled: false`。治理白名单这一轮注册成功（无 governance 失败行），但"模型真的能叫动"要到 Step 5 的真实调用才算闭环。

## Step 5 端到端（人工配合，`default` agent，会话 `<SESSION_ID>`）

前置：人工在 Console 的 `default` agent 工具设置里打开三个工具（首装默认 disabled）。全程共 6 轮，其中第 4 轮（16:58）走**答页**、第 6 轮（18:43）走 **overlay 卡片**，两条投递路径都有真人提交的读数。

第 6 轮（人填哨兵值，逐字取自会话文件）：

```text
18:43:11 tool_call    request_secret {"client_request_id": "probe-run-5-20261020", "label": "probe token", "reason": "验收"}
18:43:57 tool_result  {"status": "saved", "ref": "sr_<REDACTED>", "ref_expires_in": 300,
                       "hint": "Pass ref to secret_exec or secret_export. You never see the value."}
18:44:03 tool_call    secret_exec {"command": "printf \"%s\" {{secret}}", "ref": "sr_<REDACTED>"}
18:44:33 tool_result  {"status": "ok", "exit_code": 0, "stdout": "***", "stderr": "", "ref": "consumed"}
```

人工判据（leo 现场确认）：overlay 卡片上 label `probe token`、reason `验收`、倒计时、密码输入框四样**都看得见**；填入的是逐字 `DUMMY-SK-abcdef123456`；模型复述里只有 `***`。
工具入参在 transcript 里只有 `label`/`reason`/`client_request_id`，**没有值字段**。

## Step 6 抢答可见性（spec §10.2 第 6 条）

两轮抢答（15:55:04 与 17:59:54）读数一致：

```text
answer#1  http=200 {"status":"saved"}
meta      {"status":"saved","status_text":"已保存","answered_peer":"127.0.0.1","answered_at":"2026-10-20 17:59:54"}
answer#2  http=410 {"detail":"already_answered"}          ← 重放
/pending  {"requests":[]}                                  ← 答后不再列出
日志      records.py:153 pending (session=<SESSION8> timeout=600s)
          tools.py:333   answer page for request <id> -> http://127.0.0.1:19999/api/secret-input/card/<id>
          records.py:286 -> saved (answered by=127.0.0.1 answered_at=1792490394.5850296)
零命中    BOGUS-NOT-A-SECRET / BOGUS-SECOND-TRY / nonce 在 qwenpaw.log 各 0 命中
```

人工判据（leo 现场确认）：抢答后卡片翻成终态，文案**符合期望**（「已由 127.0.0.1 于 2026-10-20 17:59:54 提交」）⇒ 人因此能发现被抢答。

## Step 7 明文零命中（哨兵值 = `DUMMY-SK-abcdef123456`，第 6 轮真人提交）

**方法论修正**：第一次跑（答后 35s）的十个 0 **作废** —— 模型侧 `secret_exec` 结果 18:44:33 才落盘，grep 在 18:43:58 就跑了，差一秒。改为答后等 120s 再 grep，下表是重跑结果（18:46:03）。

| 落盘面 | 命中文件数 |
| --- | --- |
| `~/.qwenpaw/qwenpaw.log*` | 0 |
| `workspaces/*/tool_results/*.txt` | 0 |
| `workspaces/default/dialog/*.jsonl` | 0 |
| `workspaces/default/mem_session/dialog/*.jsonl` | 0 |
| `workspaces/default/sessions/` + `chats.json` | 0 |
| `workspaces/default/history.db*` | 0 |
| `MEMORY.md` + `mem_metadata/`（含 `.zst` 解压后） | 0 |
| `~/.qwenpaw/token_usage.json` | 0 |
| `~/.qwenpaw/governance/` | 0 |
| `workspaces/default/checkpoints/`（shadow.git） | 0 |

阳性对照（三条，缺一不可）：

1. `grep -c 'secret-input' ~/.qwenpaw/qwenpaw.log` = 38（日志通道活着）
2. **同一批面里本轮的 ref `sr_<REDACTED>` 命中 `sessions/` 1 个文件、`mem_session/dialog/` 1 个文件** ⇒ grep 确实读到了本轮数据，哨兵值的缺席才有意义（比计划原定的 `grep -c secret-input` 强：那条只证明通道活着，不证明"这一轮"被读到）
3. 哨兵文件自检 1 行

取证纪律：哨兵值只从 `/tmp/r2_needle.txt` 经 `grep -Ff` 读入，**绝不出现在命令行**（本机曾发生过 `ps` 把 `--mcp-config` 里的 token 泄进会话历史的事故）；watcher 输出里对值就地打码，命中只报文件与计数。

注：日志在 16:47 轮转过（`qwenpaw.log.1`），grep 一律用 `qwenpaw.log*`。计划里写的 history 回读 API 在本宿主不存在（`/openapi.json` 404、`/api/messages` 只有 `POST /send`），所以 memory 面改为直接 grep 磁盘 transcript。

## Step 8 A4（长阻塞不被宿主掐）

`plugin.json` 临时加 `meta.config.answer_timeout_seconds=300` 热装（**不重启**），调用省略 `timeout_seconds`，人不回答：

| 判据 | 读数 |
| --- | --- |
| 配置生效 | `/pending` `expires_in=299.8`；日志 `pending (session=<SESSION8> timeout=300s)`。install 路径不传 `api.config` ⇒ 该值只能来自 manifest 的 `meta.config` 层（Task 1 carry-in 与 R44 哨兵在真机成立） |
| 精确 300s | 卡 19:00:08 → `records.py:290 -> timed_out` 19:05:08，间隔 **300s** |
| 入参确实省略 | tool_call 只有 `{"client_request_id": "probe-run-7-20261020", "label": "probe token", "reason": "验收"}` |
| 模型侧 | `{"status": "timed_out", "hint": "The card is closed. Ask again with request_secret using a NEW client_request_id if it is still needed."}` |
| 不是被宿主掐 | `Tool timeout` 0 命中。日志里 4 条 `TimeoutError` 经核为宿主 **MCP 驱动连接超时**（`mcp_stateful_client.py:410`，17:10:45 与 18:57:33，即两次热装后 workspace 重建的时刻），与本工具无关 |
| 人工侧 | leo 确认卡片倒计时从 **5:00** 起（不是 10:00） |

做完已 `git checkout -- plugin.json` 还原，`git diff` 干净；Step 12 重装后安装位 `meta.config` 回读为 `None`。

顺带白送的一条：第 2、3 轮（16:03、16:14）在 600s 默认下超时后，模型**自己换了新的 `client_request_id`** 重问（`probe-run-2-…-a7f3` → `probe-run-2b-…-c91d`）⇒ `timed_out` 的 hint 文案在真机上被模型正确执行。

## Step 9 A6（阻塞期点 Stop）

```text
18:50:09 tool_call    request_secret {"client_request_id": "probe-run-6-20261020", …}
18:50:40 records.py:153  pending (session=<SESSION8> timeout=600s)   ← 卡片出现
18:50:58 人工点 Stop
         meta      {"status":"cancelled","status_text":"已取消","answered_peer":null,"answered_at":""}
         /pending  {"requests":[]}
         日志      records.py:290 | secret-input request <REQ8> -> cancelled
```

附加加固：取消后的卡**不能再被塞值** —— `POST /answer` 回 `410 {"detail":"already_answered"}`，投入的 `ORPHAN-ATTEMPT` 在日志 0 命中。

**R60（计划期望值错，非产品缺陷）**：计划 Step 9 写「期望模型收到 `{"status":"cancelled","reason":"run_ended"}`」——真机上**收不到**。宿主在 Stop 时抢先替换了工具结果：

```text
tool_result | 2026-10-20T18:50:58 | output: "<system-reminder>The tool call was cancelled by the user.</system-reminder>"
                                     state: "interrupted"
```

Console 侧显示"工具调用在返回结果前已中断"（leo 现场确认）。插件侧的取消语义完整无缺：卡片进终态、`/pending` 清空、审计行落盘、vault 里没有值、取消后 410 不可再答。所以判据应改为「**宿主中断 + 插件侧四件事都发生**」，而不是「模型读到我们的 cancelled JSON」。

## Step 10 答页独立可用 + 响应头

- 真人于 16:59 通过答页 `…/api/secret-input/card/<id>` 提交成功（当时 overlay 因 R59 坏着，这条路径是唯一的投递口）⇒ 答页独立可用。
- 响应头（逐字）：

```text
GET /api/secret-input/card/deadbeef → HTTP/1.1 200 OK
  content-type: text/html; charset=utf-8      content-length: 130      server: uvicorn
GET /api/secret-input/card/deadbeef/meta → 404  content-type: application/json
GET /api/secret-input/pending → 200            content-type: application/json
```

  **没有任何安全头**（无 CSP、无 X-Frame-Options、无 Cache-Control）—— 这正是 spec §15 A3 的期望：「顺查一次响应头确认**没多出**安全头」，否则自包含 HTML 答页会被全局 CSP 挡掉。响应也未被压缩/`RuntimeBoundaryMiddleware` 改写。
- 答页自身安全性复核（`backend/card_page.py`）：request_id 先过 `^[0-9a-f]{32}$` 才插值，不合法直接回静态错误页；label/reason/status_text/answered_peer/answered_at **全部走 `textContent`**，整份文件无 `innerHTML`；nonce 由页面自己 `GET /meta` 取，从不进 HTML；提交后 `el("v").value = ""` 清输入框。
- 可选加固（未实施，留给终审裁定）：给 `/meta` 与答页加 `Cache-Control: no-store`、`X-Content-Type-Options: nosniff`。当前无泄漏（值从不出现在任何响应里，nonce 一次性且只在 pending 期发放），属于纵深防御而非缺陷修复。

## Step 11 卸载（`DELETE /api/plugins/secret-input`）

```text
19:09:09 main.py:199    | secret-input: swept (pending=0 vault=0 scrub=0)        ← 我们的 uninstall hook 跑了
19:09:09 loader.py:1614 | Removed tools ['request_secret','secret_exec','secret_export'] from agents.tools
19:09:09 api.py:89      | Unregistered governance tools for plugin 'secret-input': request_secret, secret_exec, secret_export
19:09:09 registry.py:1038| Unregistered all entries for plugin 'secret-input'
19:09:09 loader.py:1493 | Deleted plugin files at /home/leo/.qwenpaw/plugins/secret-input
19:09:09 loader.py:1497 | Unloaded plugin 'secret-input'
```

- 目录已删除；`/api/plugins` 不再列出；`/api/secret-input/pending`、`/card/<id>`、frontend bundle 三项均 **404**；`/api/tools` 里三个工具消失；无任何插件相关 ERROR/Traceback。
- 第 3 行同时是**治理白名单确实注册过**的直接证据（R58 里"治理默认拒绝未注册工具"的疑虑到此闭环：注册成功过，所以卸载时才有的可注销）。
- **副作用（要写进 README 的运维提示）**：卸载会清掉各 agent `builtin_tools` 里的三个条目，人工打开的 `enabled=true` 开关随之丢失；重装后 `_sync_plugin_tools_to_agents` 重新写回 `enabled=False`，需要再点一次。

## Step 12 重装（`POST /api/plugins/install`，force）

| 判据 | 读数 |
| --- | --- |
| 运行时注册 | `Registered tool function` 3、`Attached ToolDescriptor` 3、`Injected … ToolRegistry` 24（8 workspace × 3 工具）、`rolled back`/`Failed to register tool` **0** |
| 插件自身日志（R56） | `secret-input: swept` 1 条 |
| 端点 | `/api/secret-input/pending` 200 |
| 前端产物 | bundle 200，内容为 `var API = "/secret-input"`（R59 修复版），与仓库 `ui/index.js` **逐字节相同** |
| 后端产物 | `diff -rq -x __pycache__ backend ~/.qwenpaw/plugins/secret-input/backend` 无输出 |
| 配置 | 安装位 `meta.config` = `None`（Step 8 的临时值已还原） |
| 工具开关 | 10 个 agent 全部 `enabled=False` —— 与 spec「三个工具默认关闭，由人显式打开」一致 |

## spec §14 八条判据的对账（终审 Important 4/6 后补，诚实标注缺口）

本文件此前只按 Step 编号记录，没有逐条对 spec §14 对账，导致三条**没有真机读数**的判据在文档里是沉默的（而本文第 6 行自己写着"凡是尚未执行的步骤显式标 `PENDING`"）。逐条补上：

| §14 | 判据 | 状态 | 证据 / 缺口 |
| --- | --- | --- | --- |
| 1 | verifier PASS + **8 条负对照仍会红（输出留档）** | **部分** | PASS 有读数（Step 1、Step 12）；**8 条负对照的输出从未留档**。终审另查出 spec §12 要求 `test_packaging.py` 调 verifier 并跑其中 7 条负对照，而该文件实际只测了 `build_zip.sh`（计划 Task 11 悄悄缩了范围）。已由 `tests/test_verifier_gate.py` 补上并在本文留档 |
| 2 | `unittest` 全绿，含 I1 四条路径 | **通过** | 284 tests OK（终审后新增 6 条）；`test_invariants.py` 的 7 个捕获点覆盖 I1 |
| 3 | 热装成功、`enabled/loaded` true、`diff -rq` 一致、单份 | **通过** | Step 3 / Step 12 |
| 4 | 真会话端到端：卡片阻塞期出现 → 人填 → `saved`+ref → `secret_exec` 返回 `***` | **通过** | Step 5（第 6 轮，overlay 投递）+ Step 10（第 4 轮，答页投递） |
| 5 | 三处 grep 零命中 + 阳性对照 | **通过（口径已修订）** | Step 7 的十处面 + "本轮 ref 必须命中同一批面"。**与 spec 原文的差异**：spec 要求的阳性对照是"把同一串当普通文本发进聊天、回读必须命中"，本宿主没有 history 回读 API（`/openapi.json` 404、`/api/messages` 只有 POST），改为 grep 磁盘 transcript 并以本轮 ref 作对照 —— 同样能证明"这一轮的数据确实被读到"，但不是 spec 写的那一条 |
| 6 | 二次消费同一 ref → `ref_unavailable`，报文含 "Ask the user again with request_secret" | **PENDING（真机）** | 离线有门（`tests/test_tools_consume.py` 的重放/已消费用例），**真机零读数**。Step 6 那个 410 是 HTTP `/answer` 的重放，与"工具级二次消费"不是同一件事，不能拿来充数。补法：让模型用同一个 ref 连调两次 `secret_exec`，第二次的返回逐字留档 |
| 7 | `secret_export` 后 `$ENV_KEY` 在后续 shell 工具里可用、模型侧无明文 | **PENDING（真机）** | `secret_export` 在本文只出现在注册/列举/卸载的日志行里，**从未被真正调用过**。而它正是唯一那条设计上持久的路径，且所有离线用例都注入 `env_writer` 桩 ⇒ 真的 `qwenpaw.envs.store.set_env_var` 集成（含宿主的 `is_managed_control_key` 会不会静默丢掉我们放行的键）端到端未验。补法：人答一张卡 → `secret_export(env_key=…)` → 另起一次 `execute_shell_command` 跑 `printenv <key>` 并对模型侧输出做零命中 grep |
| 8 | 非 loopback 且未认证的 `/answer` → 403 | **通过** | Step 4（peer 为 RFC 5737 文档段的探针） |

两条 PENDING 都需要一次人工配合（各约 3 分钟），不接受 curl 伪造（计划 R8）。

### 判据 1 的负对照输出留档（`tests/test_verifier_gate.py`，2026-10-20）

冻结 verifier 的 sha256 = `2182bc4d2150b91ff6d623338cce4fafadaf4c65d122352589cfed212509f18a`（与 `tests/test_verifier_gate.py:VERIFIER_SHA256` 逐字一致；改它一个字节，第一条用例就红）。8 条负对照各在**临时副本**上真跑一次 verifier 子进程，断言 `rc=1` 且 FAIL 文案对上；第 9 条是正向对照（未突变副本必须 `rc=0` + `PASS`），否则"verifier 恒红"也能让 8 条负对照全绿。

```text
test_the_verifier_is_byte_identical_to_the_pinned_hash            ok   （sha256 钉死）
test_nc1_missing_manifest_fails                                   ok   → "no plugin.json in"
test_nc2_unreadable_manifest_fails                                ok   → "plugin.json unreadable"
test_nc3_missing_version_fails                                    ok   → "plugin.json has no version"
test_nc4_missing_entry_backend_fails                              ok   → "declares no entry.backend"
test_nc5_entry_backend_points_at_a_missing_file                   ok   → "entry.backend file missing"
test_nc6_version_window_major_out_of_range_fails                  ok   → "max major must be 1 or 2"
test_nc7_self_excluding_max_fails                                 ok   → "self-exclusion"
test_nc8_register_raising_is_reported_not_swallowed               ok   → rc=1 + FAIL
test_positive_control_unmutated_tree_passes                       ok   → rc=0 + PASS
Ran 10 tests in 3.948s — OK
```

`nc7` 是这套门里最值钱的一条：`qwenpaw_version.max` 是**右开**区间且宿主自 2.2.1 起停用上限校验，所以"max 填宿主自己的版本"今天能装、enforcement 回来那天自我排除，**宿主不会警告你**，这个 verifier 是唯一一道门。


## 真机验收发现（Step 5–12 期间）

### R59 — overlay 卡片永不显示（已修，commit 88bf7e2）

- 症状：模型连要三次卡，人一次也没看见；宿主日志零 403、浏览器零 pageerror、`[secret-input] overlay card registered` 正常打印。唯一线索在浏览器 Network：

```text
GET http://127.0.0.1:19999/api/api/secret-input/pending?session_id=<SESSION_ID> 404 (Not Found)
    getPending @ 6a9dbb48-…:44    tick @ 6a9dbb48-…:296
```

- 根因：宿主 SDK 的 `host.fetch` 是 `ei(e,t)=fetch(ce(e),…)`，而 `ce(e)="/api"+(e.startsWith("/")?e:"/"+e)` —— **无条件加 `/api` 前缀**（`console/assets/index-BpFaGbCq.js` 逐字读出）。bundle 里的 `API` 常量又写了一遍 `/api`，于是每个请求都 404；而 `getPending` 对非 2xx 一律回 `null`「保留上一帧、不闪没卡片」⇒ 失败完全静默。
- 排除掉的三个错误归因：①会话解析没问题（`QwenPaw.host.getCurrentSessionId()` 返回的正是后端认的 `<SESSION_ID>`，用该值直接 curl `/pending?session_id=` 命中 1 条）②不是 403 鉴权（loopback 放行，日志零 denied）③不是 bundle 没加载（无头浏览器实测：bundle 被拉取、`slot.fill overlay.global` 审计行打印、零 pageerror）。
- 门的缺口：`tests/test_ui_bundle.py` 的渲染 harness 把 `host.fetch` 桩成"一调用就抛"，从没跑过轮询路径 ⇒ 没有任何断言看过真实 URL。修复同时新增 `POLL_HARNESS`：按宿主语义（`prefix(u)="/api"+u`）桩 `host.fetch`，真跑一轮 `tick()`，断言发出的 URL 恰为 `/api/secret-input/pending?session_id=s1`；负对照把常量改回 `/api/secret-input`，harness 复现出与真机**逐字相同**的 `/api/api/…` 并使两条门变红。
- 复验：热装后 leo 硬刷新 Console，卡片正常出现并可用（第 6 轮即由 overlay 提交）。

### R60 — 计划 Step 9 的期望值错（文档已改，代码无需动）

见 Step 9 一节：宿主在 Stop 时用自己的 `<system-reminder>The tool call was cancelled by the user.</system-reminder>`（`state: "interrupted"`）替换工具结果，我们的 `{"status":"cancelled","reason":"run_ended"}` 到不了 transcript。插件侧取消语义完整，判据改为「宿主中断 + 卡片终态 + `/pending` 清空 + `-> cancelled` 审计行 + 取消后 410」。

