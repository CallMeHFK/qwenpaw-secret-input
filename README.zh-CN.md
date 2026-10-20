# secret-input

一个 QwenPaw 后端插件：在**一次运行的过程中**向人索取密钥，同时不把密钥放进模型上下文。

[English version](README.md) | 中文版

## 它做什么

模型调用 `request_secret`；会话里出现一张私密卡片（另有独立答页）；人输入值；模型只拿到一个
一次性 `ref`，永远拿不到值。

**The value never enters the model's context; the model only ever receives a
one-time ref.**（明文不进模型上下文，模型只拿到一次性 ref。）

插件只在内存里保存明文（不落盘）：`secret_exec` 在插件进程内完成替换，工具输出里出现的
任何明文都会被擦成 `***`。`secret_export` 是唯一、且刻意的例外：它把值写进宿主的加密
env 存储，供之后的 shell 工具用 `$ENV_KEY`。

- 三个工具：`request_secret`、`secret_exec`、`secret_export`
- `/api/secret-input` 下四个端点（loopback 或已认证调用方）
- 阻塞期的 overlay 卡片 + 独立答页
- 一个中间件：在结果进入转录、memory 或磁盘卸载路径**之前**完成擦洗

## 安装

### QwenPaw 正在运行（热装，无需重启）

```bash
qwenpaw plugin install /path/to/secret_input --force
# 或者桌面端走的同一条 API：
curl -s -X POST http://127.0.0.1:19999/api/plugins/install \
  -H 'Content-Type: application/json' \
  -d '{"source":"/path/to/secret_input","force":true}'
```

startup hook 当场执行（宿主日志出现 `secret-input: swept ...`），不需要重启。

### QwenPaw 没在运行

`qwenpaw plugin install <目录>` 或 `qwenpaw plugin install <zip 的 https URL>` 都可。
这一模式下**本地 zip 文件**会被拒绝并报 `plugin.json not found` —— 用目录，或把 zip
放到可访问的 URL 上再装。

### 从 release 安装

从本仓库 Releases 页面下载 `secret-input-0.1.0.zip`（由 `packaging/build_zip.sh` 构建，
压缩包只有一个顶层目录 `secret-input/`），然后按上面的方式安装。

## 打开工具

三个工具都默认关闭（`enabled=False`）。使用前必须在目标 agent 的工具设置里手动打开。

卸载插件会**清掉每个 agent 里的这些 `builtin_tools` 条目**（宿主日志会打
`Removed tools [...] from agents.tools`），你手动打开的开关随之丢失；重装时写回的是
`enabled=False`，需要再点一次。

热装还会把 `plugin.json` → `meta.tools[]` 里列出的工具同步进每个 agent 的
`builtin_tools`；新增第四个工具时要同步加进去，否则它在 `/api/tools` 里不可见，直到宿主重启。

`/api/tools` 读的是这份 agent 配置，不是运行时注册结果，所以工具可能"列在那里却叫不动"
（同步出来的条目描述为空、图标是默认值）。热装是否真的注册成功，判据在
`~/.qwenpaw/qwenpaw.log`：`Attached ToolDescriptor to '<name>'` 与
`Injected '<name>' into workspace '<agent>' ToolRegistry`，且没有
`Failed to register tool ... (rolled back)`。

## 用法

一次典型交互：

```text
模型 → request_secret(label="GitHub token", reason="push the fix")
        （阻塞；人填卡片）
工具 ← {"status": "saved", "ref": "sr_…", "ref_expires_in": 300,
        "hint": "Pass ref to secret_exec or secret_export. You never see the value."}

模型 → secret_exec(ref="sr_…", command='curl -H "Authorization: Bearer $SECRET" …')
工具 ← {"status": "ok", "exit_code": 0, "stdout": "…", "stderr": "", "ref": "consumed"}
```

命令行里优先用 `$SECRET`（或你自己的 `env_name`），少用 `{{secret}}` 标记 —— 原因见
[威胁模型](#威胁模型)第 7 条。

`request_secret` 参数：`label`、`reason`、`placeholder`、`timeout_seconds`、
`client_request_id`。

- `timeout_seconds` 省略或传 `0` 表示“用运维配置的 `answer_timeout_seconds`（默认
  600）”，**不是**“立刻超时”。显式值必须在 `30..3600` 且优先于配置。
- 结果丢失后的重试应复用同一个 `client_request_id`（幂等重放）。遇到 `timed_out` /
  `ref_unavailable` / `ref_expired` 之后必须换**新的** `client_request_id`。

必须处理的返回：

| status | 含义 | 下一步 |
| --- | --- | --- |
| `saved` | 人已作答；`ref` 可用一次 | 交给 `secret_exec`/`secret_export` |
| `declined` | 人拒绝 | 不要再为同一件事索要 |
| `timed_out` | 截止前无人作答 | 用 `request_secret` 重问，并换**新的** `client_request_id` |
| `cancelled` | 卡片还开着时运行被 Stop | 停止；不要盲目重试 |
| `ref_unavailable` | ref 已花掉或不存在 | 换**新的** `client_request_id` 重问 |
| `ref_expired` | ref 超过 `ref_ttl_seconds` | 换**新的** `client_request_id` 重问 |

`secret_exec`：值会导出到子进程的 `$<env_name>`（默认 `SECRET`），命令文本里的
`{{secret}}` 标记由插件在进程内替换。**优先用 `$<env_name>`**：替换后的命令会成为子进程的
`argv`，而 `/proc/<pid>/cmdline` 在子进程整个生命周期内是**任何本地用户可读**的（模式
`0444`），同一段文本还会进 `ps` 输出、auditd 的 `EXECVE` 记录与进程记账 —— 这比威胁模型
第 1 条的"同 uid"宽得多。改从环境变量读，这些面一个都不落。`stdout`/`stderr` 先过擦洗
（`***`），两流共用同一个 `max_output_bytes` 预算（默认 32768，范围 1024..32768）；截断
标记在预算**里面**预留，所以序列化后的结果严格低于宿主 50 000 B 的磁盘卸载阈值。

`secret_export`：`env_key` 必须匹配 `^[A-Z_][A-Z0-9_]{0,63}$`，且不能是宿主关键名
（`PATH`、`HOME`、`LD_PRELOAD`、`NODE_OPTIONS`、`GIT_SSH_COMMAND`、`PYTHONSTARTUP`
等，共 20 个）。值会写进宿主加密 env 存储与进程 environ，供之后的 `$ENV_KEY` 使用；只有
写入成功才消费 ref。**一次性使用优先选 `secret_exec`** —— export 是设计上的长期暴露面。
这张黑名单是"别意外把宿主搞瘫或被跨重启劫持"的护栏，**不是安全边界**：模型本来就通过
`secret_exec` 有任意执行能力，所以"不在表里"从来不等于"安全"。

## 在哪里作答

1. **Console 会话里的 overlay 卡片**（工具阻塞期间出现；每 2 秒轮询本会话的待答请求）。
   一次只渲染**一张**卡 —— 本会话的第一条待答请求；若同时挂着多张，答完一张，下一张会在
   2 秒内出现。
2. **独立答页** `/api/secret-input/card/<request_id>` —— 无 CDN、无构建，普通浏览器
   新标签页即可。
3. **终端兜底**：

```bash
curl -s http://127.0.0.1:19999/api/secret-input/pending \
  | jq -r '.requests[].answer_url'
```

`nonce` 只由 `GET /card/<id>/meta` 在卡片仍 pending 时给出；`/pending` 里没有它，工具结果
里也没有它。

## 威胁模型

1. **同机进程隔离并不存在。** 宿主 agent 能以同一 uid 跑 `execute_shell_command`、读宿主
   env 存储、`printenv`。擦洗是**尽力而为，不是沙箱**：被编码、分片或改写的值
   （`echo ${TOKEN:0:4}`）抓不住。
2. **`secret_export` 是设计上的暴露。** 值会长期留在宿主 env 存储与进程 environ 里。只需
   一次性使用时，请用 `secret_exec`。
3. Python `str` 无法清零；内存取证不在防御范围。TTL 只缩小窗口。
4. **卡片文案由模型撰写。** `label`/`reason` 按不可信文本渲染（不解析 HTML、不插链接），
   但仍可能夹带针对人的提示注入诱饵。
5. **宿主关闭 auth 且监听 `0.0.0.0`** 时，LAN 能读 `/api/*`；本插件的 loopback 门只覆盖
   自己的四个端点，不改变宿主全局策略。`allow_remote=true` 会进一步放开（并短路裸
   `/pending` 的 loopback-only 规则）。
6. **本机 agent 可以抢在你之前作答。** agent 能以 loopback 身份调用
   `GET /card/{id}/meta` 拿到 nonce，再提交**它自己编的值**。它仍然读不到自己本来不知道的
   机密，但能让插件存进一个假值。插件会记录并显示答者的 TCP peer 与时间戳
   （“已由 `<peer>` 于 `<时刻>` 提交”），让人能发现抢答；**索取高价值凭据时，人必须在场，
   并在提交后立刻核对卡片。**
7. **命令行里的 `{{secret}}` 会把值放进 `argv`。** `/proc/<pid>/cmdline` 在子进程整个
   生命周期内是任何本地用户可读的（模式 `0444`），同一段文本还会进 `ps` 输出、auditd 的
   `EXECVE` 记录与进程记账 —— 比第 1 条的"同 uid"宽得多。优先用 `$<env_name>`：值改走
   子进程的环境变量，别的本地用户读不到。
8. **短于 8 个字符的值完全不做出口擦洗。** 登记它们会把普通输出打得满地 `***`，所以擦洗器
   直接拒收（`scrub.py` 的 `_MIN_VALUE_CHARS = 8`）：一个 6 位密码会**原样**出现在
   `stdout` 里，中间件也不会拦。请索取 8 字符以上的值，否则按"不擦洗"对待。

擦洗会把已登记明文的每种变体（raw/base64/base64url/hex/percent）替换成恰好 `***` —— 但只对
**8 个字符以上**的值生效（第 8 条）。**不**使用宿主的 `mask_secret_value`，因为它会保留首尾
字符。

## 配置

热装路径上 `api.config` 是 `{}`；每个键都有默认值。优先级：`plugin.json meta.config` >
`api.config` > 环境变量 `QWENPAW_SECRET_INPUT_*` > 默认值。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `answer_timeout_seconds` | 600 | `request_secret` 省略/传 0 时的默认等待 |
| `ref_ttl_seconds` | 300 | ref 存活时长 |
| `scrub_enabled` | true | 输出擦洗开关（关掉 = 只保证入站） |
| `scrub_ttl_seconds` | 3600 | 明文留在擦洗集合里的时长 |
| `scrub_max_entries` | 64 | 擦洗集合上限 |
| `allow_remote` | false | 是否接受非 loopback 未认证调用 |
| `public_base_url` | 空 | INFO 日志里答案 URL 的前缀；空则 `http://127.0.0.1:19999`。插件在进程内读不到宿主的监听口，所以**宿主监听在别的口时必须设这个键**，否则日志里那条链接是死的。 |
| `max_pending` | 32 | 同时挂起的卡片上限 |

loopback 判定只匹配字面 TCP 对端 `127.0.0.1`、`::1`、`localhost`。从别的 loopback 地址
（`127.0.0.2` 等）连入会被拒绝（fail-closed）—— 请用 `127.0.0.1`。

## 开发

```bash
# 测试与打包验证用宿主解释器跑
python3.12 -m unittest discover -s tests -t . -v
python3.12 -m compileall -q backend
python3.12 packaging/verify_qp_plugin.py .
python3.12 -m ruff check backend tests packaging
node --check ui/index.js          # Debian 系主机上可能是 nodejs
bash packaging/build_zip.sh
```

- `packaging/verify_qp_plugin.py` 用**真** `PluginLoader` 离线验证。它是冻结的**证据**、
  不是产品代码：`tests/test_verifier_gate.py` 钉住它的 sha256（`2182bc4d…f18a`），并真跑
  它的 8 条负对照 + 1 条正向对照 —— 改它、或者交出一个它本该拒收的包，套件就会红。
- 每道门都要有**负对照**（拆掉防线，看测试变红）。不会红的门不算门。
- 测试一律用显眼假值（`DUMMY-SK-abcdef123456`），禁用真实密钥。
- 完整 `unittest` 依赖宿主包（会 import `qwenpaw`），所以 CI 只跑静态三步；完整套件在本机
  用宿主解释器跑。
- 构建发布 zip 需要 `python3`、`zip`、`unzip`。

## 许可

MIT —— 见 [LICENSE](LICENSE)。
