# secret-input 真机验收记录（2026-10-19 spec §14 / A4 A6 A7）

- 宿主：`qwenpaw app --host 0.0.0.0 --port 19999`（pid 74210，2026-10-20 10:08 起在跑）
- 插件版本：0.1.0（分支 `feat/secret-input-plugin`，验收开始时的 HEAD `2fc84a0`）
- 离线解释器：`/home/leo/miniforge3/bin/python3.12`
- 本文件只记**真机读数**。凡是尚未执行的步骤显式标 `PENDING`，不预填结论。

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

## Step 5–12 `PENDING`（需一次人工配合）

以下步骤按计划要求**不接受 curl 伪造**，必须在 Console 中由人操作；读数会在完成后逐条补进本文件。

- Step 5：Console 目标 agent 设置里打开 `request_secret`、`secret_exec`、`secret_export`；发探针提示词；卡片填 `DUMMY-SK-abcdef123456`；期望 `secret_exec` 返回 `status: ok`、`stdout: ***`。
- Step 6：卡片出现后先抢答（`/pending` → `/meta` 取 nonce → `answer`），期望 200 `saved`、meta 回 `answered_peer: 127.0.0.1`，卡片显示“已由 … 提交”，第二次 answer 410。
- Step 7：对 memory 回读、`~/.qwenpaw/qwenpaw.log*`、`tool_results/*.txt` 三处 grep 明文，0 命中；阳性对照用 `secret-input`（**不是** label，R51a 起 label/reason 不进日志）。日志路径以实测的 `~/.qwenpaw/qwenpaw.log` 为准，计划里的 `~/.qwenpaw/logs/` 不存在。
- Step 8：A4，`answer_timeout_seconds=300` 且调用省略 `timeout_seconds`，等 5 分钟，期望 `timed_out` + `hint` 提到 new client_request_id。
- Step 9：A6，阻塞期点 Stop，期望 `{"status":"cancelled","reason":"run_ended"}`、`/pending` 不再列出、日志一行 `-> cancelled`（R56 修复后这条日志现在可达）。
- Step 10：A7 + 答页独立可用，浏览器提交并核对 `content-type`/安全头。
- Step 11：卸载钩子与目录清理。
- Step 12：重装回运行时，并把两轮读数并入本文件。
