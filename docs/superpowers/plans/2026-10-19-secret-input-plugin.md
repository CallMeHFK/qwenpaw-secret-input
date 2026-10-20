# secret-input（QwenPaw 私密输入插件）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 做一个 QwenPaw 后端插件，让 agent 在执行中途通过私密卡片向人类索取密钥，明文永不进入模型上下文，模型只拿到一次性引用。

**Architecture:** 三个工具（`request_secret` 阻塞等人、`secret_exec` 受控执行、`secret_export` 落宿主加密 env 存储）+ 一个自带 FastAPI 路由的答题面（会话内 overlay 卡片 / 自包含 HTML 答页）+ 一个 `on_acting` 出站擦洗中间件。值只经 `POST /answer` 进入插件进程，存内存 vault，按一次性 ref 消费。

**Tech Stack:** Python 3.12（宿主 miniforge 解释器，进程内插件）、FastAPI/pydantic（宿主自带）、agentscope `MiddlewareBase`/`ToolResponse`、免构建 React bundle（宿主 `window.QwenPaw` Host SDK）、`unittest`。

**Spec:** [docs/superpowers/specs/2026-10-19-secret-input-plugin-design.md](file:///home/leo/develop/secret_input/docs/superpowers/specs/2026-10-19-secret-input-plugin-design.md) —— 本计划逐节对应它，执行时两份一起读。

## Global Constraints

每个任务的隐含要求，逐字来自 spec：

- 宿主版本 **qwenpaw 2.2.2**，解释器 **`/home/leo/miniforge3/bin/python3.12`**；测试与验证脚本一律用它，不用系统 `python3`。
- `dependencies: []`（离线验证阶段不得给宿主装包）。
- `plugin.json` 的 `qwenpaw_version` = `{min:"2.1.0", max:"2.99.0"}`；`max` 主版本 ∈ {1,2} 且 ≠ 宿主版本（右开语义下填 `2.2.2` = 自我排除，且宿主今天完全不报）。
- 工具 wrapper **一律返回字符串、绝不向宿主抛异常**；`register(api)` 里每个注册调用单独 try/except 并 log。
- `register_tool(..., enabled=False)` ⇒ 首装默认关闭，README 必须写。
- 擦洗替换标记恒为 `***`。**禁止**使用 `qwenpaw.security.secret_store.mask_secret_value`（保留首尾字符，等于把泄露面从 0 改成若干个字符）。
- 任何端点、日志、异常文本、返回串都**不得**包含用户输入的明文；`logger.*` 不接 value。
- 端点默认只接受 loopback TCP peer 或 `request.state.user` 已认证者；`allow_remote` 默认 false。
- `secret_exec` 输出上限 `max_output_bytes=32768`（让宿主的"整体落盘再截断"根本不触发，spec §10.1 路径⑥）。
- 热装路径 `api.config == {}` ⇒ 所有配置项必须有默认值。
- 测试命令（全项目统一）：`cd /home/leo/develop/secret_input && /home/leo/miniforge3/bin/python3.12 -m unittest discover -s tests -t . -v`。
- 提交粒度：每个任务一次 commit，消息写"为什么"。

## 文件结构（先锁定边界，再拆任务）

| 文件 | 责任 | 任务 |
| --- | --- | --- |
| `plugin.json` | manifest | 1 |
| `backend/config.py` | 配置优先级链与默认值，容忍空 config | 1 |
| `backend/vault.py` | ref → 明文（内存态）、一次性 consume、TTL | 2 |
| `backend/records.py` | `SecretRequest` 状态机、future、`client_request_id` 幂等、上限、GC、答者审计字段 | 3 |
| `backend/scrub.py` | 已知明文集合 + 5 变体 + `***` 替换 | 4 |
| `backend/middleware.py` | `on_acting` 改写 `ToolResponse`/`ToolChunk` | 5 |
| `backend/card_page.py` | 自包含 HTML 答页（内联 JS，无 CDN） | 6 |
| `backend/router.py` | 4 个端点 + loopback/nonce 门 + 审计 | 6 |
| `backend/tools.py` | 三个工具的 wrapper | 7、8 |
| `backend/prompt.py` | 注入系统提示的那段规矩（逐字文本） | 9 |
| `backend/main.py` | `plugin` 对象 + `register(api)` 装配全部扩展点 | 9 |
| `ui/index.js` | overlay 卡片（免构建，`window.QwenPaw`） | 10 |
| `packaging/verify_qp_plugin.py`、`packaging/build_zip.sh` | 离线验证与发布包 | 1、11 |
| `tests/*.py` | 与上面一一对应 + `test_invariants.py` 七路 | 各任务 |
| `README.md`、`README.zh-CN.md`、`SECURITY.md`、`CONTRIBUTING.md`、`.github/workflows/ci.yml`、`pyproject.toml` | 仓库标准与双语文档 | 13 |

## 接口契约（跨任务照抄，不得改名）

```python
# backend/config.py
@dataclass(frozen=True)
class PluginConfig:
    answer_timeout_seconds: int = 600
    ref_ttl_seconds: int = 300
    scrub_enabled: bool = True
    scrub_ttl_seconds: int = 3600
    scrub_max_entries: int = 64
    allow_remote: bool = False
    public_base_url: str = ""
    max_pending: int = 32
    @classmethod
    def load(cls, api_config: dict | None = None, meta: dict | None = None) -> "PluginConfig": ...
    # 优先级：meta["config"] > api_config > 环境变量 QWENPAW_SECRET_INPUT_<UPPER_KEY> > 默认

# backend/vault.py
class Vault:
    def __init__(self, ttl_seconds: int = 300, max_entries: int = 64) -> None: ...
    def issue(self, value: str) -> str: ...                       # "sr_" + secrets.token_urlsafe(16)
    def consume(self, ref: str) -> tuple[str | None, str]: ...    # (value|None, "ok"|"ref_unavailable"|"ref_expired")
    def status(self, ref: str) -> str: ...                        # "live"|"gone"|"expired"|"unknown"
    def sweep(self) -> int: ...

# backend/records.py
@dataclass
class SecretRequest:
    request_id: str; nonce: str; label: str; reason: str; placeholder: str
    session_id: str; agent_id: str; user_id: str; channel: str
    client_request_id: str | None
    created_at: float; expires_at: float
    status: str                    # pending|saved|declined|cancelled|timed_out
    future: asyncio.Future         # 结果为 dict：{"status": ..., "ref": ...}
    ref: str | None = None
    answered_peer: str | None = None
    answered_at: float | None = None
    answered_ua: str | None = None

class RequestRegistry:
    def __init__(self, vault: Vault, *, timeout_seconds: int = 600, max_pending: int = 32) -> None: ...
    async def create(self, *, label: str, reason: str, placeholder: str, session_id: str,
                     agent_id: str, user_id: str, channel: str,
                     client_request_id: str | None = None,
                     timeout_seconds: int | None = None) -> SecretRequest: ...
    def get(self, request_id: str) -> SecretRequest | None: ...
    def find_by_client_request_id(self, client_request_id: str) -> SecretRequest | None: ...
    def list_pending(self, session_id: str | None = None) -> list[SecretRequest]: ...
    async def answer(self, request_id: str, *, nonce: str, value: str | None = None,
                     decline: bool = False, peer: str = "", user_agent: str = "") -> tuple[bool, str]: ...
    async def cancel(self, request_id: str, reason: str) -> bool: ...   # reason: run_ended|superseded|shutdown
    def sweep(self) -> int: ...
    # answer 的第二个返回值："" | "not_found" | "invalid_nonce" | "already_answered" | "expired"

# backend/scrub.py
REDACT = "***"
class Scrubber:
    def __init__(self, *, ttl_seconds: int = 3600, max_entries: int = 64) -> None: ...
    def register(self, value: str) -> None: ...
    def variants(self, value: str) -> list[str]: ...          # raw/base64/base64url/hex/percent
    def replace(self, text: str) -> str: ...                   # 全量替换为 REDACT
    def sweep(self) -> int: ...

# backend/middleware.py
class SecretScrubMiddleware(MiddlewareBase):
    def __init__(self, scrubber: Scrubber) -> None: ...
    async def on_acting(self, agent, input_kwargs, next_handler): ...   # async generator
def make_scrub_middleware_factory(scrubber: Scrubber, config: PluginConfig):
    """factory(ctx, agent_config) -> SecretScrubMiddleware | None（config.scrub_enabled=False 时 None）"""

# backend/router.py
def build_router(*, registry: RequestRegistry, config: PluginConfig, vault: Vault) -> "APIRouter"
    # prefix="/secret-input"；GET /pending, GET /card/{request_id},
    # GET /card/{request_id}/meta, POST /card/{request_id}/answer

# backend/card_page.py
def render_page(request_id: str) -> str     # 自包含 HTML；不内嵌任何值，只内嵌 request_id

# backend/tools.py
class SecretTools:
    def __init__(self, *, registry: RequestRegistry, vault: Vault, scrubber: Scrubber,
                 config: PluginConfig) -> None: ...
    async def request_secret(self, label: str, reason: str, placeholder: str = "",
                             timeout_seconds: int = 0, client_request_id: str = "") -> str: ...
    # R44/Task 9：0 或省略 = 用 config.answer_timeout_seconds；显式值（30..3600）仍然获胜。
    async def secret_exec(self, ref: str, command: str, env_name: str = "SECRET",
                          timeout_seconds: int = 120, cwd: str = "",
                          max_output_bytes: int = 32768) -> str: ...
    async def secret_export(self, ref: str, env_key: str) -> str: ...

# backend/main.py
class SecretInputPlugin:
    def register(self, api) -> None: ...
plugin = SecretInputPlugin()
```

测试明文常量（全项目唯一，任何测试都不得换成真密钥）：
`DUMMY = "DUMMY-SK-abcdef123456"`（`tests/support.py` 里定义并导出）。

---

### Task 1: 骨架 —— manifest、config、可被真 PluginLoader 加载的空插件

**Files:**
- Create: `plugin.json`、`backend/__init__.py`、`backend/config.py`、`backend/main.py`、`tests/__init__.py`、`tests/support.py`、`tests/test_config.py`、`packaging/verify_qp_plugin.py`
- Modify: `.gitignore`（已存在，追加 `~/.qwenpaw` 无关项不需要）

**Interfaces:**
- Consumes: 无
- Produces: `PluginConfig.load()`、`backend/main.py` 的 `plugin` 对象（本任务只 `register(api)` 里注册 startup hook 的空壳）、`tests/support.py` 的 `DUMMY`

- [ ] **Step 0: 确认 `tests` 命名空间不被 site-packages 遮蔽**（本机有此历史坑）

Run:
```bash
cd /home/leo/develop/secret_input && mkdir -p backend tests && touch backend/__init__.py tests/__init__.py
/home/leo/miniforge3/bin/python3.12 -c "import tests, pathlib; print(tests.__file__)"
```
Expected: 打印 `/home/leo/develop/secret_input/tests/__init__.py`。若打印 site-packages 里的路径，后续所有 `unittest` 命令加 `-t .` 仍不够，须在 `tests/support.py` 顶部不依赖 `tests.` 前缀、改用 `sys.path.insert(0, str(Path(__file__).resolve().parents[1]))` 直接 import `backend.*`（本计划已按此写法，见 Step 4）。

- [ ] **Step 1: 写失败测试 `tests/test_config.py`**

```python
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import PluginConfig


class ConfigTest(unittest.TestCase):
    def test_empty_api_config_is_accepted(self):
        # 热装路径 api.config == {}，全部键必须有默认值
        cfg = PluginConfig.load({}, None)
        self.assertEqual(cfg.answer_timeout_seconds, 600)
        self.assertEqual(cfg.ref_ttl_seconds, 300)
        self.assertTrue(cfg.scrub_enabled)
        self.assertFalse(cfg.allow_remote)
        self.assertEqual(cfg.scrub_max_entries, 64)

    def test_precedence_meta_beats_api_beats_env(self):
        os.environ["QWENPAW_SECRET_INPUT_REF_TTL_SECONDS"] = "111"
        try:
            self.assertEqual(PluginConfig.load({}, None).ref_ttl_seconds, 111)      # env 胜默认
            self.assertEqual(PluginConfig.load({"ref_ttl_seconds": 222}, None).ref_ttl_seconds, 222)
            got = PluginConfig.load({"ref_ttl_seconds": 222}, {"config": {"ref_ttl_seconds": 333}})
            self.assertEqual(got.ref_ttl_seconds, 333)                              # meta 胜 api
        finally:
            del os.environ["QWENPAW_SECRET_INPUT_REF_TTL_SECONDS"]

    def test_bad_values_fall_back_to_defaults_instead_of_raising(self):
        cfg = PluginConfig.load({"ref_ttl_seconds": "not-a-number", "allow_remote": "maybe"}, None)
        self.assertEqual(cfg.ref_ttl_seconds, 300)
        self.assertFalse(cfg.allow_remote)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/leo/develop/secret_input && /home/leo/miniforge3/bin/python3.12 -m unittest discover -s tests -t . -v 2>&1 | tail -5`
Expected: `ModuleNotFoundError: No module named 'backend.config'`

- [ ] **Step 3: 写 `backend/config.py`**

```python
# -*- coding: utf-8 -*-
"""Plugin configuration with a four-tier precedence chain."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from typing import Any

_ENV_PREFIX = "QWENPAW_SECRET_INPUT_"
_BOOL_TRUE = {"true", "1", "yes", "on"}


def _coerce(raw: Any, default: Any) -> Any:
    if isinstance(default, bool):
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in _BOOL_TRUE
        return default
    if isinstance(default, int):
        if isinstance(raw, bool):   # bool is an int subclass: True must not become TTL 1
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default
    if isinstance(default, str):
        return raw if isinstance(raw, str) else default
    return default


@dataclass(frozen=True)
class PluginConfig:
    answer_timeout_seconds: int = 600
    ref_ttl_seconds: int = 300
    scrub_enabled: bool = True
    scrub_ttl_seconds: int = 3600
    scrub_max_entries: int = 64
    allow_remote: bool = False
    public_base_url: str = ""
    max_pending: int = 32

    @classmethod
    def load(cls, api_config: dict | None = None, meta: dict | None = None) -> "PluginConfig":
        """meta["config"] > api_config > env > default. Never raises."""
        defaults = cls()
        values: dict[str, Any] = {}
        for field in fields(cls):
            key = field.name
            default = getattr(defaults, key)
            env_raw = os.environ.get(_ENV_PREFIX + key.upper())
            picked = env_raw if env_raw is not None else default
            # Non-dict layers must not raise: api.config is {} on hot install, and a
            # malformed meta.config must degrade to defaults, never break plugin load.
            meta_config = meta.get("config") if isinstance(meta, dict) else None
            layers = [layer for layer in (api_config, meta_config) if isinstance(layer, dict)]
            for layer in layers:
                if key in layer:
                    picked = layer[key]
            values[key] = _coerce(picked, default)
        return cls(**values)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /home/leo/develop/secret_input && /home/leo/miniforge3/bin/python3.12 -m unittest discover -s tests -t . -v 2>&1 | tail -5`
Expected: `OK` / `3 passed`

- [ ] **Step 5: 写 `plugin.json`（manifest 逐字）**

```json
{
  "id": "secret-input",
  "name": "Secret Input（私密输入）",
  "version": "0.1.0",
  "type": "general",
  "description": "Ask the user for a secret through a private card; the value never enters the model's context — only a one-time ref does.",
  "description_i18n": {
    "zh-CN": "用私密卡片向用户索取密钥；明文永不进入模型上下文，模型只拿到一次性引用。"
  },
  "author": "leo",
  "entry": {
    "backend": "backend/main.py",
    "frontend": "ui/index.js"
  },
  "dependencies": [],
  "qwenpaw_version": {
    "min": "2.1.0",
    "max": "2.99.0"
  },
  "meta": {
    "category": "security"
  }
}
```

`entry.frontend` 指向 `ui/index.js`，本任务先放一个占位空文件（Task 10 才有内容）——注意 loader 只校验 backend 模块可导入，前端由 Console 自己按 `frontend_entry` 拉；空文件会让浏览器 404 之外的行为变成"什么都不注册"，安全。

```bash
mkdir -p ui && : > ui/index.js
```

- [ ] **Step 6: 写 `tests/support.py` 与最小 `backend/main.py`**

`tests/support.py`：
```python
# -*- coding: utf-8 -*-
"""Shared test helpers. Never import real credentials anywhere."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DUMMY = "DUMMY-SK-abcdef123456"
```

`backend/main.py`（本任务只到"能被 Loader 加载"，Task 9 再装配）：
```python
# -*- coding: utf-8 -*-
"""secret-input plugin entry point."""

from __future__ import annotations

import logging

from .config import PluginConfig

logger = logging.getLogger(__name__)


class SecretInputPlugin:
    def __init__(self) -> None:
        self.config = PluginConfig.load({}, None)

    def register(self, api) -> None:  # noqa: ANN001 - PluginApi 在宿主侧
        self.api = api
        try:
            api.register_startup_hook(
                "secret_input_boot",
                lambda: logger.info("secret-input loaded (config=%s)", self.config),
                priority=100,
            )
        except Exception:  # noqa: BLE001 - 刻意：一处失败不带崩整包
            logger.exception("secret-input: startup hook registration failed")


plugin = SecretInputPlugin()
```

- [ ] **Step 6b: 注册可观测门 `tests/test_main.py`**（评审轮补，后续任务照此办理）

骨架唯一的运行期行为是"startup hook 登记上了"，而 `register()` 里吞异常的 `except` 是设计（单点失败不带崩整包）。
它必须可观测：离线脚本只**打印**钩子计数且 `logging.disable(logging.ERROR)`，所以 `scheduled startup hooks (0)` 也会 `PASS`。

```python
import unittest
from unittest.mock import assertNoLogs

EXPECTED_CALL = ("secret_input_boot", 100)   # Task 9 把钩子改名为 secret_input_sweep 时同步更新


class RecordingApi:
    def __init__(self):
        self.calls = []

    def register_startup_hook(self, hook_name, callback, priority=100):
        self.calls.append((hook_name, priority))


class RegisterTest(unittest.TestCase):
    def test_register_schedules_the_startup_hook_without_swallowing_anything(self):
        from backend.main import SecretInputPlugin

        api = RecordingApi()
        plugin = SecretInputPlugin()
        with assertNoLogs("backend.main", level="ERROR"):
            plugin.register(api)
        self.assertEqual(api.calls, [EXPECTED_CALL])
        self.assertIs(plugin.api, api)

    def test_a_registration_failure_is_logged_not_swallowed_silently(self):
        from backend.main import SecretInputPlugin

        class Boom(RecordingApi):
            def register_startup_hook(self, *args, **kwargs):
                raise RuntimeError("host signature drifted")

        with self.assertLogs("backend.main", level="ERROR") as got:
            SecretInputPlugin().register(Boom())
        self.assertTrue(any("startup hook registration failed" in line for line in got.output))


if __name__ == "__main__":
    unittest.main()
```

Run: `/home/leo/miniforge3/bin/python3.12 -m unittest tests.test_main -v` → `OK`。
变异对照：把 `backend/main.py` 里的钩子名改掉，第一条必须红，然后还原。
`RecordingApi.register_startup_hook` 的签名对齐宿主 `api.py:451-456`（`hook_name, callback, priority=100`），
所以登记调用被改名/漏调/换名都会红——这不是"桩自己记了自己"的自证。

- [ ] **Step 7: 取离线验证脚本（技能的定稿版，逐字抄入，不要自己重写门）**

把 `qwenpaw-plugin-release` 技能第 4 节那段 `verify_qp_plugin.py` 全文存为 `packaging/verify_qp_plugin.py`。它是本包唯一的静态 manifest 门（宿主不报 `max==宿主` 的自我排除，也不报缺 SKILL.md）。

- [ ] **Step 8: 跑离线验证，确认真 PluginLoader 吃得下这个骨架**

Run: `cd /home/leo/develop/secret_input && /home/leo/miniforge3/bin/python3.12 packaging/verify_qp_plugin.py .`
Expected:
```
manifest: id=secret-input version=0.1.0 window >=2.1.0 <2.99.0
discovered: secret-input 0.1.0 at .../plugins/secret-input
loaded: id=secret-input version=0.1.0 enabled=True instance=SecretInputPlugin
scheduled startup hooks (1): secret_input_boot@100
PASS: plugin loads under the real PluginLoader (register(api) ran, startup hooks did not)
```
若 `FAIL: qwenpaw_version.max 2.99.0 equals host ...` 说明宿主已升到 2.99 —— 那是真信号（自我排除），按 spec §5 改 max 而不是改判据。

- [ ] **Step 9: 验负对照仍然会红（证明门不是空门）**

Run（三条，逐条 Expected 是 exit 1）：
```bash
cd /home/leo/develop/secret_input
cp -r . /tmp/qp-neg && rm /tmp/qp-neg/plugin.json && /home/leo/miniforge3/bin/python3.12 packaging/verify_qp_plugin.py /tmp/qp-neg; echo "rc=$?"
# Expected: FAIL: no plugin.json in /tmp/qp-neg  / rc=1
cp -r . /tmp/qp-neg2 && /home/leo/miniforge3/bin/python3.12 - <<'PY'
import json, pathlib
p = pathlib.Path("/tmp/qp-neg2/plugin.json"); m = json.loads(p.read_text())
m["qwenpaw_version"]["max"] = "2.2.2"; p.write_text(json.dumps(m))
PY
/home/leo/miniforge3/bin/python3.12 packaging/verify_qp_plugin.py /tmp/qp-neg2; echo "rc=$?"
# Expected: FAIL: qwenpaw_version.max 2.2.2 equals host 2.2.2 -> self-exclusion / rc=1
```
两条都红才继续；红了但被"补回文件"转绿的步骤要真做一遍（把 `/tmp/qp-neg` 的 `plugin.json` 复制回去再跑，应转 PASS）。

- [ ] **Step 10: Commit**

```bash
cd /home/leo/develop/secret_input
git add plugin.json backend tests packaging ui .gitignore
git commit -m "feat: 插件骨架与配置优先级链，过真 PluginLoader 离线验证（含负对照复证）"
```

---

### Task 2: vault —— 一次性 ref 与 TTL

**Files:**
- Create: `backend/vault.py`、`tests/test_vault.py`

**Interfaces:**
- Consumes: 无
- Produces: `Vault()`、`issue/consume/status/sweep/__len__`（签名见顶部契约）

- [ ] **Step 1: 写失败测试 `tests/test_vault.py`**

```python
import time
import unittest

from tests.support import DUMMY  # noqa: F401  (路径已在 support 里插好)

from backend.vault import Vault


class VaultTest(unittest.TestCase):
    def test_ref_is_opaque_and_not_derivable_from_value(self):
        v = Vault()
        ref = v.issue(DUMMY)
        self.assertTrue(ref.startswith("sr_"))
        self.assertNotIn(DUMMY, ref)
        self.assertGreaterEqual(len(ref), 20)

    def test_consume_works_exactly_once(self):
        v = Vault()
        ref = v.issue(DUMMY)
        value, status = v.consume(ref)
        self.assertEqual((value, status), (DUMMY, "ok"))
        value2, status2 = v.consume(ref)
        self.assertIsNone(value2)
        self.assertEqual(status2, "ref_unavailable")

    def test_unknown_ref_is_unavailable_not_expired(self):
        v = Vault()
        self.assertEqual(v.consume("sr_nope")[1], "ref_unavailable")

    def test_ttl_expires_unconsumed_ref(self):
        v = Vault(ttl_seconds=1)
        ref = v.issue(DUMMY)
        v._issued[ref] = (DUMMY, time.monotonic() - 5)   # 直接把时间拨过去，别 sleep
        value, status = v.consume(ref)
        self.assertIsNone(value)
        self.assertEqual(status, "ref_expired")

    def test_max_entries_evicts_oldest(self):
        v = Vault(max_entries=3)
        refs = [v.issue(f"x{i}") for i in range(5)]
        self.assertEqual(len(v), 3)
        self.assertEqual(v.status(refs[0]), "gone")
        self.assertEqual(v.status(refs[-1]), "live")

    def test_sweep_removes_expired_and_returns_count(self):
        v = Vault(ttl_seconds=1)
        ref = v.issue(DUMMY)
        v._issued[ref] = (DUMMY, time.monotonic() - 5)
        self.assertEqual(v.sweep(), 1)
        self.assertEqual(v.consume(ref)[1], "ref_expired")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/leo/develop/secret_input && /home/leo/miniforge3/bin/python3.12 -m unittest tests.test_vault -v 2>&1 | tail -3`
Expected: `ModuleNotFoundError: No module named 'backend.vault'`

- [ ] **Step 3: 写 `backend/vault.py`**

```python
# -*- coding: utf-8 -*-
"""In-memory one-time handle store. Values never leave this process."""

from __future__ import annotations

import secrets
import time
from collections import OrderedDict

_PREFIX = "sr_"
_MAX_SCAN = 256


class Vault:
    """Maps an opaque ref to the plaintext, consumable exactly once.

    Nothing is persisted: a restart drops every ref, which is the point
    (spec section 9) — the model then gets ref_unavailable and must ask again.
    """

    def __init__(self, ttl_seconds: int = 300, max_entries: int = 64) -> None:
        self._ttl = max(int(ttl_seconds), 1)
        self._max = max(int(max_entries), 1)
        # ref -> (value, monotonic issued at). OrderedDict keeps eviction
        # deterministic (oldest first) instead of relying on dict order luck.
        self._issued: "OrderedDict[str, tuple[str, float]]" = OrderedDict()
        # Consumed/expired refs we still recognise, so a late consumer can be
        # told "expired" instead of a vague "unavailable" (T3 distinguishes them).
        self._expired: set[str] = set()

    def issue(self, value: str) -> str:
        ref = _PREFIX + secrets.token_urlsafe(16)
        self._issued[ref] = (value, time.monotonic())
        while len(self._issued) > self._max:
            self._issued.popitem(last=False)
        return ref

    def _live(self, ref: str) -> tuple[str | None, str]:
        entry = self._issued.get(ref)
        if entry is None:
            return None, ("ref_expired" if self._was_known_and_expired(ref) else "ref_unavailable")
        value, at = entry
        if time.monotonic() - at > self._ttl:
            del self._issued[ref]
            self._expired.add(ref)
            return None, "ref_expired"
        return value, "ok"

    def _was_known_and_expired(self, ref: str) -> bool:
        return ref in self._expired

    def consume(self, ref: str) -> tuple[str | None, str]:
        value, status = self._live(ref)
        if status == "ok" and value is not None:
            del self._issued[ref]
            return value, "ok"
        return None, status

    def status(self, ref: str) -> str:
        if ref in self._expired:
            return "expired"
        if ref not in self._issued:
            return "gone"
        value, at = self._issued[ref]
        return "live" if time.monotonic() - at <= self._ttl else "expired"

    def sweep(self) -> int:
        now = time.monotonic()
        stale = [ref for ref, (_v, at) in self._issued.items() if now - at > self._ttl][:_MAX_SCAN]
        for ref in stale:
            self._issued.pop(ref, None)
            self._expired.add(ref)
        self._trim_expired()
        return len(stale)

    def _trim_expired(self) -> None:
        while len(self._expired) > self._max:
            self._expired.pop()

    def __len__(self) -> int:
        return len(self._issued)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /home/leo/develop/secret_input && /home/leo/miniforge3/bin/python3.12 -m unittest tests.test_vault -v 2>&1 | tail -4`
Expected: `OK` / `6 tests`

- [ ] **Step 5: 红对照 —— 把 `consume` 改成不删（读而非消费），断言必红**

Run:
```bash
cd /home/leo/develop/secret_input && cp backend/vault.py /tmp/vault.bak
/home/leo/miniforge3/bin/python3.12 - <<'PY'
from pathlib import Path
p = Path("backend/vault.py"); t = p.read_text()
old = '        if status == "ok" and value is not None:\n            del self._issued[ref]\n            return value, "ok"'
assert old in t, "consume body not found"
p.write_text(t.replace(old, '        if status == "ok" and value is not None:\n            return value, "ok"', 1))
PY
/home/leo/miniforge3/bin/python3.12 -m unittest tests.test_vault -v 2>&1 | tail -4
cp /tmp/vault.bak backend/vault.py
/home/leo/miniforge3/bin/python3.12 -m unittest tests.test_vault 2>&1 | tail -2
```
Expected: 改坏后 `FAILED (failures=1)`（`test_consume_works_exactly_once`），还原后 `OK`。**没红就说明这条门是空的，停下修门。**

- [ ] **Step 6: Commit**

```bash
git add backend/vault.py tests/test_vault.py
git commit -m "feat: 一次性 ref vault（内存态、TTL、区分 expired/unavailable）"
```

---

### Task 3: records —— 请求状态机、future、幂等、上限与 GC

**Files:**
- Create: `backend/records.py`、`tests/test_records.py`

**Interfaces:**
- Consumes: `Vault.issue`（Task 2）
- Produces: `SecretRequest`、`RequestRegistry`（签名见顶部契约）

- [ ] **Step 1: 写失败测试 `tests/test_records.py`**

```python
import asyncio
import time
import unittest

from tests.support import DUMMY

from backend.config import PluginConfig
from backend.records import RequestRegistry
from backend.vault import Vault


def run(coro):
    return asyncio.run(coro)


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.vault = Vault(ttl_seconds=300)
        self.reg = RequestRegistry(self.vault, timeout_seconds=600, max_pending=3)

    def _create(self, cid=None, label="GitHub token", reason="for the webhook"):
        return run(self.reg.create(
            label=label, reason=reason, placeholder="paste it",
            session_id="s1", agent_id="a1", user_id="u1", channel="console",
            client_request_id=cid,
        ))

    def test_save_answer_resolves_future_with_ref_and_no_value(self):
        req = self._create()
        ok, why = run(self.reg.answer(req.request_id, nonce=req.nonce, value=DUMMY,
                                      peer="127.0.0.1", user_agent="Mozilla/5.0"))
        self.assertTrue(ok, why)
        outcome = req.future.result()
        self.assertEqual(outcome["status"], "saved")
        self.assertTrue(outcome["ref"].startswith("sr_"))
        self.assertNotIn(DUMMY, str(outcome))          # 结果里只有 ref
        self.assertEqual(req.answered_peer, "127.0.0.1")  # 答者审计
        self.assertIsNotNone(req.answered_at)

    def test_decline_resolves_with_declined(self):
        req = self._create()
        ok, _ = run(self.reg.answer(req.request_id, nonce=req.nonce, decline=True))
        self.assertTrue(ok)
        self.assertEqual(req.future.result()["status"], "declined")

    def test_wrong_nonce_is_rejected_and_stays_pending(self):
        req = self._create()
        ok, why = run(self.reg.answer(req.request_id, nonce="nope", value=DUMMY))
        self.assertFalse(ok)
        self.assertEqual(why, "invalid_nonce")
        self.assertEqual(len(self.reg.list_pending("s1")), 1)

    def test_double_answer_reports_already_answered(self):
        req = self._create()
        run(self.reg.answer(req.request_id, nonce=req.nonce, value=DUMMY))
        ok, why = run(self.reg.answer(req.request_id, nonce=req.nonce, value=DUMMY))
        self.assertFalse(ok)
        self.assertEqual(why, "already_answered")

    def test_unknown_request_id_is_not_found(self):
        ok, why = run(self.reg.answer("deadbeef", nonce="x", value=DUMMY))
        self.assertFalse(ok)
        self.assertEqual(why, "not_found")

    def test_client_request_id_reuses_the_same_card(self):
        first = self._create(cid="c-1")
        second = self._create(cid="c-1")
        self.assertIs(first, second)                     # 不重发卡
        self.assertEqual(len(self.reg.list_pending()), 1)

    def test_replay_returns_the_same_ref_until_consumed(self):
        req = self._create(cid="c-2")
        run(self.reg.answer(req.request_id, nonce=req.nonce, value=DUMMY))
        again = self._create(cid="c-2")
        self.assertIs(again, req)
        self.assertEqual(again.future.result()["ref"], req.ref)

    def test_timeout_marks_record_and_resolves_future(self):
        req = self._create()
        req.expires_at = time.monotonic() - 1
        self.assertEqual(self.reg.sweep(), 1)
        self.assertEqual(req.future.result()["status"], "timed_out")
        self.assertEqual(req.status, "timed_out")

    def test_cancel_resolves_with_reason(self):
        req = self._create()
        self.assertTrue(run(self.reg.cancel(req.request_id, "run_ended")))
        outcome = req.future.result()
        self.assertEqual(outcome["status"], "cancelled")
        self.assertEqual(outcome["reason"], "run_ended")

    def test_max_pending_cancels_oldest_and_reports_superseded(self):
        old = self._create(label="oldest")
        for i in range(3):
            self._create(label=f"n{i}")
        self.assertEqual(old.future.result()["status"], "cancelled")
        self.assertEqual(old.future.result()["reason"], "superseded")
        self.assertLessEqual(len(self.reg.list_pending()), 3)

    def test_timeout_floor_and_ceiling(self):
        with self.assertRaises(ValueError):
            self._create.__self__  # 仅为可读性；真正的断言在下面两次调用
        run(self.reg.create(label="l", reason="r", placeholder="", session_id="s",
                            agent_id="a", user_id="u", channel="console",
                            timeout_seconds=PluginConfig().answer_timeout_seconds))
        with self.assertRaises(ValueError):
            run(self.reg.create(label="l", reason="r", placeholder="", session_id="s",
                                agent_id="a", user_id="u", channel="console",
                                timeout_seconds=5))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/leo/develop/secret_input && /home/leo/miniforge3/bin/python3.12 -m unittest tests.test_records -v 2>&1 | tail -3`
Expected: `ModuleNotFoundError: No module named 'backend.records'`

- [ ] **Step 3: 写 `backend/records.py`**

> **本步代码块是修复轮 1 之前的文本，与已提交实现有 6 处分歧，权威文本是 `backend/records.py`@f202b7c。**
> 分歧点（复评点名）：`_by_client_id` 类型注解、查表处的元组键+agent 守卫、存键仍是裸 cid、
> `find_by_client_request_id` 的单参签名（顶部契约同样陈旧）、`_finish` 里按裸 cid 弹键，以及 `declined` 分支缺答者三元组、
> Step 5 的 `Expected: failures=2`（提交版为 4，且其中两条是 Step 1 未创建的用例名）。
> 只有重做 Task 3（例如上下文丢失后恢复）时需要以提交为准；Task 6/7 抄的是各自步骤的代码，不受影响。

```python
# -*- coding: utf-8 -*-
"""Pending secret requests: state machine, futures, idempotency, GC."""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_MIN_TIMEOUT = 30
_MAX_TIMEOUT = 3600
_EXPIRED_MARKS: set[str] = set()


@dataclass
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
    by_ref: dict[str, Any] = field(default_factory=dict)  # unused; keeps repr cheap

    def seconds_left(self) -> float:
        return max(self.expires_at - time.monotonic(), 0.0)


class RequestRegistry:
    def __init__(self, vault, *, timeout_seconds: int = 600, max_pending: int = 32) -> None:
        self._vault = vault
        self._default_timeout = min(max(int(timeout_seconds), _MIN_TIMEOUT), _MAX_TIMEOUT)
        self._max_pending = max(int(max_pending), 1)
        self._pending: dict[str, SecretRequest] = {}
        self._by_client_id: dict[str, SecretRequest] = {}

    async def create(self, *, label: str, reason: str, placeholder: str, session_id: str,
                     agent_id: str, user_id: str, channel: str,
                     client_request_id: str | None = None,
                     timeout_seconds: int | None = None) -> SecretRequest:
        if client_request_id:
            # 幂等键带会话（修复轮 1 / 裁定 R15）：client_request_id 是模型自选文本
            # （request_secret 没有命名空间），"retry-1" 这类默认值跨会话相撞在预料之内；
            # 裸 cid 当键会让 B 会话的一次重试拿到 A 的记录对象 —— A 的卡还 pending 就
            # 让 B 挂在 A 的卡上、A 的人类答案交付给 B；A 的卡已 saved 就当场把 A 的 ref
            # 交给 B 去消费 A 的用户为另一个会话提供的值（spec 7.1 会话归属、10.2 第 6 条）。
            # 再加一道 agent 守卫：一张卡绝不负给两个 agent。
            key = (str(session_id or ""), str(client_request_id))
            reuse = self._by_client_id.get(key)
            if reuse is not None and str(reuse.agent_id or "") == str(agent_id or ""):
                return reuse

        timeout = self._default_timeout
        if timeout_seconds is not None:
            timeout = int(timeout_seconds)
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
            request.request_id[:8], session_id[:8], request.label, timeout,
        )
        return request

    def _enforce_capacity(self, newest: SecretRequest) -> None:
        while len(self._pending) > self._max_pending:
            oldest = next(iter(self._pending))
            if oldest == newest.request_id:
                break
            request = self._pending[oldest]
            asyncio.ensure_future(self.cancel(oldest, "superseded"))
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
        request = self._pending.get(request_id)
        if request is None:
            return False, ("expired" if request_id in _EXPIRED_MARKS else "not_found")
        if not hmac.compare_digest(str(nonce or ""), request.nonce):
            return False, "invalid_nonce"
        if request.status != "pending":
            return False, "already_answered"
        if request.seconds_left() <= 0:
            await self._finish(request, "timed_out")
            return False, "expired"

        if decline:
            # 拒答同样要落答者三元组（修复轮 1 / 裁定 R16）：spec 8.2 要这三项落在
            # **每一条**已答记录上，"脚本化拒答是不是别人替按的"正是 10.2 第 6 条要让
            # 人类看见的那件事；不记的话 T6 的"已由 <peer> 于 <时刻> 提交"对拒答永远空白。
            request.answered_peer = peer or None
            request.answered_at = time.time()
            request.answered_ua = (user_agent or "")[:200] or None
            await self._finish(request, "declined")
            return True, ""

        secret = str(value or "")
        if not secret:
            return False, "invalid_nonce"  # 空值按未授权处理，不给"存了什么"的信号
        request.ref = self._vault.issue(secret)
        request.answered_peer = peer or None
        request.answered_at = time.time()
        request.answered_ua = (user_agent or "")[:200] or None
        await self._finish(request, "saved", ref=request.ref)
        return True, ""

    async def cancel(self, request_id: str, reason: str) -> bool:
        request = self._pending.get(request_id)
        if request is None or request.status != "pending":
            return False
        request.cancel_reason = reason
        await self._finish(request, "cancelled", reason=reason)
        return True

    async def _finish(self, request: SecretRequest, status: str, **extra: Any) -> None:
        request.status = status
        self._pending.pop(request.request_id, None)
        if request.client_request_id:
            self._by_client_id.pop(request.client_request_id, None)
        outcome: dict[str, Any] = {"status": status, **extra}
        if status == "saved" and request.ref:
            outcome["ref_expires_in"] = getattr(self._vault, "_ttl", 300)
        if not request.future.done():
            request.future.set_result(outcome)
        logger.info("secret-input request %s -> %s", request.request_id[:8], status)

    def sweep(self) -> int:
        now = time.monotonic()
        stale = [r for r in self._pending.values() if r.expires_at <= now][:256]
        for request in stale:
            _EXPIRED_MARKS.add(request.request_id)
            request.status = "timed_out"
            self._pending.pop(request.request_id, None)
            if request.client_request_id:
                self._by_client_id.pop(request.client_request_id, None)
            if not request.future.done():
                request.future.set_result({"status": "timed_out"})
        while len(_EXPIRED_MARKS) > 1024:
            _EXPIRED_MARKS.pop()
        return len(stale)
```

删掉 `SecretRequest` 里那行 `by_ref` —— 它不属于接口，写出来只为说明"别加用不到的字段"：最终版本**不含** `by_ref` 字段，也删掉 `# 仅为可读性` 那行测试夹具里的怪语句（见 Step 4 修正）。

**Task 3 补记（修复轮 1，裁定 R15/R16）—— 幂等与重放是按会话分键的，外加一道 agent 守卫。**
`client_request_id` 由模型自选、没有命名空间，所以 `_by_client_id` 的键是 `(session_id, client_request_id)`
（实现里由 `_replay_key(session_id, client_request_id)` 统一产出，`create` 命中查表、`create` 落表、
`_finish` 的 saved 保留/其余终态删除三处**必须用同一个键**；`dict` 的注解为 `dict[tuple[str, str], SecretRequest]`）。
命中之后还要比 `agent_id`：同会话换 agent 也重发一张卡，一张卡绝不负给两个 agent。
`find_by_client_request_id(client_request_id, session_id=None)` 因此多了第二个参数 —— **T6/T7 要重放就必须把
发起侧的 `session_id` 一起传进来**，不传只会命中"无会话"那一格，跨会话命中在结构上不可能。
后续任务照抄时以 `backend/records.py` 为准；已核过 Task 6/Task 7 正文与本条不冲突
（T7 的 `request_secret` 本来就把 `who["session_id"]` 传进 `create`，它的两条重放用例共用同一个 identity，
所以同会话复用照旧成立；T6 的 `/card/{id}/meta` 在记录已终结时回 `answered_peer`/`answered_at`，
本任务补上拒答三元组之后它才显示得出来）。spec §7.1 那句"同一 client_request_id 重复调用 → 复用同一条记录"
读时要补上"同一会话内"这个限定，spec 本身按任务边界不动。

- [ ] **Step 4: 跑测试确认通过（含两处清理）**

先清掉 Step 1 里那两行无用怪句（`self._create.__self__` 那三行）与 Step 3 的 `by_ref` 字段：
```bash
cd /home/leo/develop/secret_input
/home/leo/miniforge3/bin/python3.12 - <<'PY'
from pathlib import Path
r = Path("tests/test_records.py"); t = r.read_text()
old = '        with self.assertRaises(ValueError):\n            self._create.__self__  # 仅为可读性；真正的断言在下面两次调用\n'
assert old in t
t = t.replace(old, '', 1)
t = t.replace('''        run(self.reg.create(label="l", reason="r", placeholder="", session_id="s",
                            agent_id="a", user_id="u", channel="console",
                            timeout_seconds=PluginConfig().answer_timeout_seconds))
''', '''        ok_timeout = run(self.reg.create(label="l", reason="r", placeholder="", session_id="s2",
                                agent_id="a", user_id="u", channel="console",
                                timeout_seconds=PluginConfig().answer_timeout_seconds))
        self.assertEqual(ok_timeout.status, "pending")
''', 1)
r.write_text(t)

v = Path("backend/records.py"); s = v.read_text()
junk = '    cancel_reason: str | None = None\n    by_ref: dict[str, Any] = field(default_factory=dict)  # unused; keeps repr cheap\n'
assert junk in s
s = s.replace(junk, '    cancel_reason: str | None = None\n', 1)
s = s.replace('from dataclasses import dataclass, field\n', 'from dataclasses import dataclass\n', 1)
v.write_text(s)
PY
/home/leo/miniforge3/bin/python3.12 -m unittest discover -s tests -t . -v 2>&1 | tail -6
```
Expected: 全部 `OK`（config 3 + vault 6 + records 11）

- [ ] **Step 5: 红对照 —— 幂等门**

Run:
```bash
cd /home/leo/develop/secret_input && cp backend/records.py /tmp/records.bak
/home/leo/miniforge3/bin/python3.12 - <<'PY'
from pathlib import Path
p = Path("backend/records.py"); t = p.read_text()
old = '            if reuse is not None'
assert old in t, "idempotency block not found"
p.write_text(t.replace(old, '            if False and reuse is not None', 1))
PY
/home/leo/miniforge3/bin/python3.12 -m unittest tests.test_records 2>&1 | tail -3
cp /tmp/records.bak backend/records.py && /home/leo/miniforge3/bin/python3.12 -m unittest tests.test_records 2>&1 | tail -2
```
Expected: 摘掉幂等后 `FAILED (failures=4)`（`test_client_request_id_reuses_the_same_card`、`test_replay_returns_the_same_ref_until_consumed`、`test_client_request_id_is_scoped_to_the_session`、`test_reuse_refuses_to_serve_a_different_agent`），还原后 `OK`。锚点用 `if reuse is not None` 这一行，正文与会话分键后的实现里都只有 1 处命中，所以摘掉整条复用门时不必再跟着键形状改。

- [ ] **Step 6: Commit**

```bash
git add backend/records.py tests/test_records.py
git commit -m "feat: 请求状态机（future/幂等/上限/superseded/答者审计）"
```

---

### Task 4: scrub —— 已知明文集合与五个变体替换

**Files:**
- Create: `backend/scrub.py`、`tests/test_scrub.py`

**Interfaces:**
- Consumes: 无
- Produces: `REDACT`、`Scrubber`（签名见顶部契约）

- [ ] **Step 1: 写失败测试 `tests/test_scrub.py`**

```python
import base64
import binascii
import time
import unittest
from urllib.parse import quote

from tests.support import DUMMY

from backend.scrub import REDACT, Scrubber


class ScrubTest(unittest.TestCase):
    def setUp(self):
        self.s = Scrubber(ttl_seconds=3600, max_entries=64)
        self.s.register(DUMMY)

    def test_raw_replaced(self):
        self.assertEqual(self.s.replace(f"Bearer {DUMMY} done"), f"Bearer {REDACT} done")

    def test_all_five_variants_replaced(self):
        raw = DUMMY.encode()
        forms = [
            base64.b64encode(raw).decode(),
            base64.urlsafe_b64encode(raw).decode(),
            binascii.hexlify(raw).decode(),
            quote(DUMMY, safe=""),
        ]
        self.assertEqual(len(forms), 4)
        for form in forms:
            self.assertEqual(self.s.replace(f"x{form}y"), f"x{REDACT}y", msg=form)

    def test_unknown_text_untouched(self):
        self.assertEqual(self.s.replace("nothing to see"), "nothing to see")

    def test_empty_and_short_values_never_registered(self):
        s = Scrubber()
        s.register("")
        s.register("a")        # 1 字符值会把正常文本吃掉，必须拒绝登记
        self.assertEqual(s.replace("banana"), "banana")

    def test_ttl_expires_entry(self):
        s = Scrubber(ttl_seconds=1)
        s.register(DUMMY)
        s._entries[DUMMY] = time.monotonic() - 5
        self.assertEqual(s.sweep(), 1)
        self.assertEqual(s.replace(DUMMY), DUMMY)

    def test_max_entries_evicts_oldest(self):
        s = Scrubber(max_entries=2)
        s.register("DUMMY-SK-older0000")
        s.register(DUMMY)
        self.assertEqual(len(s), 2)
        s.register("DUMMY-SK-newest99")
        self.assertEqual(len(s), 2)
        self.assertNotIn("DUMMY-SK-older0000", s)

    def test_mask_function_is_not_used(self):
        # 守卫：宿主 mask_secret_value 保留首尾字符，绝不能进擦洗实现
        import inspect

        from backend import scrub as mod

        src = inspect.getsource(mod)
        self.assertNotIn("mask_secret_value", src)
        self.assertNotIn("qwenpaw.security.secret_store", src)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/leo/develop/secret_input && /home/leo/miniforge3/bin/python3.12 -m unittest tests.test_scrub -v 2>&1 | tail -3`
Expected: `ModuleNotFoundError: No module named 'backend.scrub'`

- [ ] **Step 3: 写 `backend/scrub.py`**

```python
# -*- coding: utf-8 -*-
"""Egress scrubber: replace known plaintext (and encodings) in tool output.

Best-effort by design (spec section 10.2). Never import the host's
mask_secret_value: it keeps leading/trailing characters, which would turn a
zero-char leak into a several-char leak.
"""

from __future__ import annotations

import base64
import binascii
import logging
import time
from collections import OrderedDict
from urllib.parse import quote

logger = logging.getLogger(__name__)

REDACT = "***"
_MIN_VALUE_CHARS = 8


class Scrubber:
    def __init__(self, *, ttl_seconds: int = 3600, max_entries: int = 64) -> None:
        self._ttl = max(int(ttl_seconds), 1)
        self._max = max(int(max_entries), 1)
        self._entries: "OrderedDict[str, float]" = OrderedDict()

    def variants(self, value: str) -> list[str]:
        raw = value.encode("utf-8")
        out = [value]
        for form in (
            base64.b64encode(raw).decode("ascii"),
            base64.urlsafe_b64encode(raw).decode("ascii"),
            binascii.hexlify(raw).decode("ascii"),
            binascii.hexlify(raw).decode("ascii").upper(),
            quote(value, safe=""),
        ):
            if form not in out:
                out.append(form)
        return out

    def register(self, value: str) -> None:
        if not value or len(value) < _MIN_VALUE_CHARS:
            logger.info("secret-input: scrub skipped for a value under %d chars",
                        _MIN_VALUE_CHARS)
            return
        self._entries[value] = time.monotonic()
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def _live_values(self) -> list[str]:
        now = time.monotonic()
        # Longest first so an encoded form cannot be partially eaten by a
        # shorter neighbour's replacement.
        return sorted(
            (v for v, at in self._entries.items() if now - at <= self._ttl),
            key=len,
            reverse=True,
        )

    def replace(self, text: str) -> str:
        if not text:
            return text
        out = text
        for value in self._live_values():
            for form in self.variants(value):
                if form in out:
                    out = out.replace(form, REDACT)
        return out

    def sweep(self) -> int:
        now = time.monotonic()
        stale = [v for v, at in self._entries.items() if now - at > self._ttl]
        for value in stale:
            self._entries.pop(value, None)
        return len(stale)

    def __contains__(self, value: str) -> bool:
        return value in self._entries

    def __len__(self) -> int:
        return len(self._entries)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /home/leo/develop/secret_input && /home/leo/miniforge3/bin/python3.12 -m unittest tests.test_scrub -v 2>&1 | tail -4`
Expected: `OK` / `7 tests`

- [ ] **Step 5: 红对照 —— 变体门不是空门**

把 `variants()` 改成只返回 `[value]`，`test_all_five_variants_replaced` 必须红：
```bash
cd /home/leo/develop/secret_input && cp backend/scrub.py /tmp/scrub.bak
/home/leo/miniforge3/bin/python3.12 - <<'PY'
from pathlib import Path
p = Path("backend/scrub.py"); t = p.read_text()
old = "        return out\n\n    def register"
assert old in t, "variants tail not found"
p.write_text(t.replace(old, "        return [value]\n\n    def register", 1))
PY
/home/leo/miniforge3/bin/python3.12 -m unittest tests.test_scrub 2>&1 | tail -3
cp /tmp/scrub.bak backend/scrub.py && /home/leo/miniforge3/bin/python3.12 -m unittest tests.test_scrub 2>&1 | tail -2
```
Expected: 红 1 条（变体用例），还原后 `OK`。

- [ ] **Step 6: Commit**

```bash
git add backend/scrub.py tests/test_scrub.py
git commit -m "feat: 出站擦洗集合（5 变体、TTL、拒收短值、显式禁用宿主掩码函数）"
```

---

### Task 5: middleware —— 在 `on_acting` 拦住将要进上下文的那一份

**Files:**
- Create: `backend/middleware.py`、`tests/test_middleware.py`

**Interfaces:**
- Consumes: `Scrubber.replace`（Task 4）、`REDACT`
- Produces: `SecretScrubMiddleware`、`make_scrub_middleware_factory(scrubber, config) -> factory`

本任务还承担 spec §10.1 **路径⑥**（磁盘工件）的门：用**真宿主 `ToolResultPruner`** 复证"擦洗先于截断落盘"这个从源码读出来的次序，并配一条**阳性对照**证明这条门真的能看见泄露。

- [ ] **Step 1: 写失败测试 `tests/test_middleware.py`**

```python
import asyncio
import tempfile
import unittest
from pathlib import Path

from tests.support import DUMMY

from backend.config import PluginConfig
from backend.middleware import SecretScrubMiddleware, make_scrub_middleware_factory
from backend.scrub import REDACT, Scrubber

from agentscope.message import DataBlock, TextBlock
from agentscope.tool import ToolChunk, ToolResponse


def collect(mw, events):
    async def drive():
        async def next_handler():
            for event in events:
                yield event

        out = []
        async for item in mw.on_acting(agent=None, input_kwargs={"tool_call": object()},
                                       next_handler=next_handler):
            out.append(item)
        return out

    return asyncio.run(drive())


def blocks_of(event):
    return [getattr(b, "text", None) for b in (event.content or [])]


class MiddlewareTest(unittest.TestCase):
    def setUp(self):
        self.scrubber = Scrubber()
        self.scrubber.register(DUMMY)
        self.mw = SecretScrubMiddleware(self.scrubber)

    def test_tool_response_text_block_is_scrubbed(self):
        (out,) = collect(self.mw, [ToolResponse(content=[TextBlock(text=f"got {DUMMY} here")])])
        self.assertEqual(blocks_of(out), [f"got {REDACT} here"])

    def test_tool_chunk_is_scrubbed_too(self):
        (out,) = collect(self.mw, [ToolChunk(content=[TextBlock(text=DUMMY)])])
        self.assertEqual(blocks_of(out), [REDACT])

    def test_non_text_blocks_pass_through_untouched(self):
        blob = DataBlock(source={"type": "url", "url": "file:///tmp/x.png"})
        (out,) = collect(self.mw, [ToolResponse(content=[blob])])
        self.assertIs(out.content[0], blob)

    def test_events_that_are_not_results_pass_through(self):
        sentinel = object()

        async def drive():
            async def next_handler():
                yield sentinel

            got = []
            async for item in self.mw.on_acting(agent=None,
                                                input_kwargs={"tool_call": object()},
                                                next_handler=next_handler):
                got.append(item)
            return got

        self.assertIs(asyncio.run(drive())[0], sentinel)

    def test_original_object_is_not_mutated(self):
        # 改写必须产出新对象，宿主别处的引用不能被动过
        original = ToolResponse(content=[TextBlock(text=DUMMY)])
        (out,) = collect(self.mw, [original])
        self.assertIsNot(out, original)
        self.assertEqual(original.content[0].text, DUMMY)

    def test_factory_returns_none_when_scrub_disabled(self):
        factory = make_scrub_middleware_factory(self.scrubber,
                                                PluginConfig(scrub_enabled=False))
        self.assertIsNone(factory(object(), object()))

    def test_factory_returns_middleware_when_enabled(self):
        factory = make_scrub_middleware_factory(self.scrubber, PluginConfig())
        self.assertIsInstance(factory(object(), object()), SecretScrubMiddleware)

    def test_disk_artifact_is_scrubbed(self):
        """路径⑥：截断落盘读到的必须是擦洗后的文本。"""
        from qwenpaw.agents.tools.utils import ToolResultPruner

        (scrubbed,) = collect(self.mw, [ToolResponse(
            content=[TextBlock(text=("leak " + DUMMY + " ") * 60)])])
        text = blocks_of(scrubbed)[0]
        with tempfile.TemporaryDirectory() as tmp:
            pruner = ToolResultPruner(tmp)
            pruned, changed = pruner.prune_output(text, max_bytes=64, metadata={})
            files = list(Path(tmp).glob("*.txt"))
            self.assertTrue(changed and files, "这条门没走到落盘分支，等于没测")
            for path in files:
                self.assertNotIn(DUMMY, path.read_text(encoding="utf-8"))
            self.assertNotIn(DUMMY, pruned)

    def test_disk_artifact_control_proves_the_gate_is_live(self):
        """阳性对照：不经中间件的同一断言必须泄露，否则上面那条是空门。"""
        from qwenpaw.agents.tools.utils import ToolResultPruner

        raw = ("leak " + DUMMY + " ") * 60
        with tempfile.TemporaryDirectory() as tmp:
            pruner = ToolResultPruner(tmp)
            _pruned, changed = pruner.prune_output(raw, max_bytes=64, metadata={})
            files = list(Path(tmp).glob("*.txt"))
            self.assertTrue(changed and files)
            self.assertIn(DUMMY, files[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/leo/develop/secret_input && /home/leo/miniforge3/bin/python3.12 -m unittest tests.test_middleware -v 2>&1 | tail -3`
Expected: `ModuleNotFoundError: No module named 'backend.middleware'`

- [ ] **Step 3: 写 `backend/middleware.py`**

```python
# -*- coding: utf-8 -*-
"""on_acting scrubber. Plugins are always inner of the host's pruning and
coordinator middlewares (qwenpaw/runtime/builder.py:1336-1343), so what we
yield is what gets truncated, offloaded to disk, and written into memory.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncGenerator, Callable

from agentscope.middleware import MiddlewareBase
from agentscope.tool import ToolChunk, ToolResponse

from .scrub import Scrubber

logger = logging.getLogger(__name__)

_MUTABLE_RESULT = (ToolResponse, ToolChunk)


def _block_text(block: Any) -> str | None:
    if isinstance(block, dict):
        value = block.get("text")
        return value if isinstance(value, str) else None
    value = getattr(block, "text", None)
    return value if isinstance(value, str) else None


def _replace_block(block: Any, text: str) -> Any:
    if isinstance(block, dict):
        return {**block, "text": text}
    return block.model_copy(update={"text": text})


class SecretScrubMiddleware(MiddlewareBase):
    def __init__(self, scrubber: Scrubber) -> None:
        self._scrubber = scrubber

    async def on_acting(
        self,
        agent: Any,
        input_kwargs: dict[str, Any],
        next_handler: Callable[..., AsyncGenerator[Any, None]],
    ) -> AsyncGenerator[Any, None]:
        async for event in next_handler():
            if isinstance(event, _MUTABLE_RESULT):
                event = self._scrub_event(event)
            yield event

    def _scrub_event(self, event: Any) -> Any:
        try:
            content = list(event.content or [])
            changed = False
            for index, block in enumerate(content):
                text = _block_text(block)
                if text is None:
                    continue
                replacement = self._scrubber.replace(text)
                if replacement != text:
                    content[index] = _replace_block(block, replacement)
                    changed = True
            if not changed:
                return event
            return event.model_copy(update={"content": content})
        except Exception:  # noqa: BLE001 - 擦洗失败绝不能改坏工具执行结果
            logger.exception("secret-input: scrub skipped for one tool result")
            return event


def make_scrub_middleware_factory(scrubber: Scrubber, config):
    def factory(ctx: Any, agent_config: Any):  # noqa: ANN001 - 宿主签名如此
        if not config.scrub_enabled:
            return None
        return SecretScrubMiddleware(scrubber)

    return factory
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /home/leo/develop/secret_input && /home/leo/miniforge3/bin/python3.12 -m unittest tests.test_middleware -v 2>&1 | tail -6`
Expected: `OK` / `16 tests`（含两条磁盘用例）。若 `test_disk_artifact_*` 报 "这条门没走到落盘分支"，是 `max_bytes` 与文本长度不匹配，把 `max_bytes=64` 调小而不是删断言。

> 计数按交付版更正（Task 5 评审修复轮 1）：本行原写 `9 tests`，那是 Step 1 那份代码的数；交付时已是 15 条，修复轮 1 后是 **16** 条（`tests/test_middleware.py`：新增 R35 混排块用例，repr 绊线补自证正对照，磁盘两条改为宿主形状调用），全套 **91** 条。`tests/test_secret_store_gate.py` 另加 4 条（R32 的"中间件不得 import 宿主包" AST 门）。

- [ ] **Step 5: 红对照 —— 磁盘门不是空的**

把 `_scrub_event` 直接返回 `event`（等效于没擦洗），磁盘用例必须翻红而对照用例仍绿：
```bash
cd /home/leo/develop/secret_input && cp backend/middleware.py /tmp/mw.bak
/home/leo/miniforge3/bin/python3.12 - <<'PY'
from pathlib import Path
p = Path("backend/middleware.py"); t = p.read_text()
old = "            if not changed:\n                return event\n            return event.model_copy(update={\"content\": content})"
assert old in t, "scrub tail not found"
p.write_text(t.replace(old, "            return event  # NEGATIVE CONTROL", 1))
PY
/home/leo/miniforge3/bin/python3.12 -m unittest tests.test_middleware 2>&1 | tail -4
cp /tmp/mw.bak backend/middleware.py && /home/leo/miniforge3/bin/python3.12 -m unittest tests.test_middleware 2>&1 | tail -2
```
Expected: `FAILED (failures=9)`（按交付版更正；`failures=4` 是 Step 1 那份 9 条测试的数），且必须**含**R2 点名的这四条：`test_tool_response_text_block_is_scrubbed`、`test_tool_chunk_is_scrubbed_too`、`test_original_object_is_not_mutated`、`test_disk_artifact_is_scrubbed`；另外五条是同机制的改写断言：`test_dict_shaped_text_block_is_scrubbed_without_mutating_it`、`test_every_event_is_yielded_in_order`、`test_only_the_text_blocks_change`、`test_a_mixed_event_rewrites_only_the_text_block`、`test_two_agent_configs_get_their_own_middleware_over_one_registry`。红的是别的应用说明对照没造对，不许只按数字放行。磁盘用例在摘掉擦洗时**必须**红，否则它就是空门；`test_disk_artifact_control_proves_the_gate_is_live`（阳性对照）、两条透传用例、R28/R34 用例、repr 用例必须仍绿。

- [ ] **Step 6: Commit**

```bash
git add backend/middleware.py tests/test_middleware.py
git commit -m "feat: on_acting 擦洗中间件（不改原对象；磁盘工件门配阳性对照）"
```

---

### Task 6: 答题面 —— 自包含 HTML 答页 + 四个端点与鉴权门

**Files:**
- Create: `backend/card_page.py`、`backend/router.py`、`tests/test_router.py`、`tests/test_card_page.py`
- Modify: `backend/records.py`（Step 6 末尾补 `find_record_any_state` 与 `_graveyard`）

**Interfaces:**
- Consumes: `RequestRegistry`（Task 3）、`PluginConfig`（Task 1）
- Produces: `render_page(request_id: str) -> str`、`build_router(*, registry, config, vault) -> APIRouter`

- [ ] **Step 1: 写失败测试 `tests/test_card_page.py`**

```python
import re
import unittest
import uuid

from tests.support import DUMMY

from backend.card_page import render_page


class CardPageTest(unittest.TestCase):
    def test_page_is_self_contained_and_has_no_cdn(self):
        html = render_page(uuid.uuid4().hex)
        self.assertIn('type="password"', html)
        self.assertNotIn("http://", html.replace("http://www.w3.org/2000/svg", ""))
        self.assertNotIn("https://", html)
        self.assertIn("<script>", html)               # 内联，无 src

    def test_page_embeds_only_the_request_id_never_a_value(self):
        html = render_page("a" * 32)
        self.assertIn("a" * 32, html)
        self.assertNotIn(DUMMY, html)

    def test_request_id_is_html_escaped_before_embedding(self):
        evil = '"><script>alert(1)</script>'
        self.assertNotIn(evil, render_page(evil))     # 非 hex 的 id 必须被拒绝或转义
        self.assertNotIn("<script>alert(1)</script>", render_page(evil))

    def test_label_and_reason_are_set_by_textcontent_not_innerhtml(self):
        # 卡片文案由模型写，属不可信文本：只允许 textContent
        html = render_page("b" * 32)
        self.assertNotIn("innerhtml", html.lower())
        self.assertGreaterEqual(html.count("textContent"), 2)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 写 `backend/card_page.py`**

```python
# -*- coding: utf-8 -*-
"""Self-contained answer page. No CDN, no build step, no secret in the HTML.

The URL carries only the unguessable request id; the nonce is fetched by the
page itself so it never lands in a log line or shell history.
"""

from __future__ import annotations

import re

from fastapi import Response

_HEX = re.compile(r"^[0-9a-f]{32}$")

_TEMPLATE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>私密输入 · Secret Input</title>
<style>
body{font:14px/1.6 system-ui,sans-serif;margin:0;padding:24px;background:#0d1117;color:#e6edf3}
main{max-width:420px;margin:0 auto;padding:20px;border:1px solid #30363d;border-radius:10px}
h1{font-size:16px;margin:0 0 4px}p.reason{color:#9198a1;margin:0 0 16px;font-size:13px}
label{display:block;margin-bottom:6px;font-size:12px;color:#9198a1}
input{width:100%%;padding:8px;box-sizing:border-box;border:1px solid #30363d;border-radius:6px;
background:#161b22;color:#e6edf3;font-family:monospace}
button{margin-top:12px;padding:8px 14px;border-radius:6px;border:1px solid #30363d;
background:#238636;color:#fff;cursor:pointer}
button.ghost{background:transparent;color:#9198a1;margin-left:8px}
#status{margin-top:12px;font-size:13px;min-height:20px}
.done{color:#3fb950}.err{color:#f85149}
</style></head><body><main>
<h1 id="label">载入中…</h1><p class="reason" id="reason"></p>
<form id="f" autocomplete="off">
<label for="v">密钥（不会进入对话，也不会给模型看到）</label>
<input id="v" type="password" autocomplete="new-password" placeholder="…">
<div><button id="save" type="submit">保存</button>
<button id="no" type="button" class="ghost">拒绝</button></div>
</form><div id="status"></div>
<script>
const ID = "%s";
const BASE = "/api/secret-input/card/";
let nonce = "";
const el = (id) => document.getElementById(id);
async function load() {
  const r = await fetch(BASE + ID + "/meta", {headers: {Accept: "application/json"}});
  const d = await r.json();
  if (d.status && d.status !== "pending") {
    el("label").textContent = "该请求已结束";
    el("reason").textContent = d.status_text || "";
    el("f").style.display = "none";
    if (d.answered_peer) {
      el("status").textContent = "已由 " + d.answered_peer + " 于 " + d.answered_at + " 提交";
    }
    return;
  }
  el("label").textContent = d.label; el("reason").textContent = d.reason;
  if (d.placeholder) { el("v").placeholder = d.placeholder; }
  nonce = d.nonce;
}
async function send(body) {
  const r = await fetch(BASE + ID + "/answer", {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
  });
  const d = await r.json().catch(() => ({}));
  if (r.ok) {
    el("status").textContent = d.status === "declined" ? "已拒绝" : "已保存，值不会出现在对话里";
    el("status").className = "done"; el("f").style.display = "none";
  } else {
    el("status").textContent = "失败：" + (d.detail || r.status);
    el("status").className = "err";
  }
}
el("f").addEventListener("submit", (e) => {
  e.preventDefault();
  const value = el("v").value;
  if (!value || !nonce) { el("status").textContent = "值为空或未载入"; return; }
  send({nonce: nonce, value: value}); el("v").value = "";
});
el("no").addEventListener("click", () => nonce && send({nonce: nonce, decline: true}));
load();
</script></body></html>
"""


def render_page(request_id: str) -> str:
    """Return the HTML, or an error page if the id is not a 32-hex token."""
    if not _HEX.match(str(request_id or "")):
        return (
            "<!doctype html><meta charset=utf-8><title>无效请求</title>"
            "<p>这个私密输入链接不可用（请求 id 不合法）。</p>"
        )
    return _TEMPLATE.replace("%s", str(request_id), 1) % ()


def html_response(request_id: str) -> Response:
    return Response(content=render_page(request_id), media_type="text/html; charset=utf-8")
```

注意 `_TEMPLATE` 里的 `%%`：模板中 CSS 需要一个字面 `%`，而 `render_page` 用 `.replace()` 而非 `%` 格式化，所以 `%%` 要写成单个 `%`。**实现时把 CSS 那行改为 `width:100%;`，并保留 `.replace()` 写法**（不要用 `%` 格式化，避免和 JS 里的 `%` 打架）。上面代码里 `render_page` 末尾的 `% ()` 是多余的，删掉它。

- [ ] **Step 3: 跑 `test_card_page` 确认通过**

Run: `cd /home/leo/develop/secret_input && /home/leo/miniforge3/bin/python3.12 -m unittest tests.test_card_page -v 2>&1 | tail -4`
Expected: `OK` / `4 tests`。`test_page_is_self_contained_and_has_no_cdn` 会因 `%%`/`% ()` 残留而红 —— 按 Step 2 的说明修正模板与函数，修到绿。

- [ ] **Step 4: 写失败测试 `tests/test_router.py`**

```python
import asyncio
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.support import DUMMY

from backend.config import PluginConfig
from backend.records import RequestRegistry
from backend.router import build_router
from backend.vault import Vault

API = "/api/secret-input"


def make_client(config=PluginConfig()):
    vault = Vault(ttl_seconds=300)
    registry = RequestRegistry(vault, timeout_seconds=600, max_pending=8)
    app = FastAPI()
    app.include_router(build_router(registry=registry, config=config, vault=vault),
                       prefix="/api/secret-input")
    # TestClient 默认 peer 是 "testclient"（非 loopback），必须显式伪装成 loopback
    return TestClient(app, client=("127.0.0.1", 51000)), registry


class RouterTest(unittest.TestCase):
    def setUp(self):
        self.client, self.registry = make_client()

    def new_request(self, session_id="s1"):
        return asyncio.run(self.registry.create(
            label="GitHub token", reason="webhook signing", placeholder="ghp_...",
            session_id=session_id, agent_id="a1", user_id="u1", channel="console",
        ))

    def test_pending_lists_session_request_without_nonce(self):
        req = self.new_request()
        body = self.client.get(f"{API}/pending", params={"session_id": "s1"}).json()
        self.assertEqual(len(body["requests"]), 1)
        item = body["requests"][0]
        self.assertEqual(item["label"], "GitHub token")
        self.assertNotIn("nonce", item)
        self.assertIn(f"/card/{req.request_id}", item["answer_url"])

    def test_pending_without_session_requires_loopback(self):
        app = FastAPI()
        app.include_router(build_router(registry=self.registry, config=PluginConfig(),
                                        vault=Vault()), prefix="/api/secret-input")
        self.assertEqual(TestClient(app, client=("10.0.0.9", 1)).get(f"{API}/pending").status_code,
                         403)

    def test_meta_exposes_nonce_only_to_allowed_caller(self):
        req = self.new_request()
        meta = self.client.get(f"{API}/card/{req.request_id}/meta").json()
        self.assertEqual(meta["nonce"], req.nonce)
        self.assertEqual(meta["status"], "pending")

    def test_answer_requires_correct_nonce(self):
        req = self.new_request()
        bad = self.client.post(f"{API}/card/{req.request_id}/answer",
                               json={"nonce": "wrong", "value": DUMMY})
        self.assertEqual(bad.status_code, 403)
        good = self.client.post(f"{API}/card/{req.request_id}/answer",
                                json={"nonce": req.nonce, "value": DUMMY})
        self.assertEqual(good.status_code, 200)
        self.assertEqual(good.json()["status"], "saved")

    def test_answer_response_body_contains_no_value(self):
        req = self.new_request()
        res = self.client.post(f"{API}/card/{req.request_id}/answer",
                               json={"nonce": req.nonce, "value": DUMMY})
        self.assertNotIn(DUMMY, res.text)

    def test_decline_path(self):
        req = self.new_request()
        res = self.client.post(f"{API}/card/{req.request_id}/answer",
                               json={"nonce": req.nonce, "decline": True})
        self.assertEqual(res.json()["status"], "declined")

    def test_second_answer_is_gone_with_410(self):
        req = self.new_request()
        self.client.post(f"{API}/card/{req.request_id}/answer",
                         json={"nonce": req.nonce, "value": DUMMY})
        again = self.client.post(f"{API}/card/{req.request_id}/answer",
                                 json={"nonce": req.nonce, "value": DUMMY})
        self.assertEqual(again.status_code, 410)

    def test_unknown_request_id_is_404_on_answer_404_on_meta(self):
        self.assertEqual(self.client.post(f"{API}/card/{'f' * 32}/answer",
                                          json={"nonce": "x", "value": DUMMY}).status_code, 404)
        self.assertEqual(self.client.get(f"{API}/card/{'f' * 32}/meta").status_code, 404)

    def test_answer_page_is_html_and_shows_audit_after_end(self):
        req = self.new_request()
        page = self.client.get(f"{API}/card/{req.request_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("text/html", page.headers["content-type"])
        self.client.post(f"{API}/card/{req.request_id}/answer",
                         json={"nonce": req.nonce, "value": DUMMY})
        meta = self.client.get(f"{API}/card/{req.request_id}/meta").json()
        self.assertEqual(meta["status"], "saved")
        self.assertEqual(meta["answered_peer"], "127.0.0.1")   # 抢答可发现的审计

    def test_allow_remote_true_lets_a_non_loopback_peer_in(self):
        app = FastAPI()
        app.include_router(build_router(registry=self.registry,
                                        config=PluginConfig(allow_remote=True),
                                        vault=Vault()), prefix="/api/secret-input")
        remote = TestClient(app, client=("10.0.0.9", 1))
        self.assertEqual(remote.get(f"{API}/pending", params={"session_id": "s1"}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 5: 跑测试确认失败**

Run: `cd /home/leo/develop/secret_input && /home/leo/miniforge3/bin/python3.12 -m unittest tests.test_router -v 2>&1 | tail -3`
Expected: `ModuleNotFoundError: No module named 'backend.router'`

- [ ] **Step 6: 写 `backend/router.py`**

```python
# -*- coding: utf-8 -*-
"""Answer transport. The plaintext only ever moves in a POST body.

Access control: loopback TCP peer, or a caller AuthMiddleware already
authenticated (request.state.user). allow_remote overrides. The host binds
0.0.0.0 and auth is off by default, so this plugin-level gate is the only
thing standing between the LAN and these four endpoints.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .card_page import html_response

logger = logging.getLogger(__name__)

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_ANSWER_MAP = {
    "": ("saved_or_declined", 200),
    "not_found": ("not_found", 404),
    "invalid_nonce": ("invalid_nonce", 403),
    "already_answered": ("already_answered", 410),
    "expired": ("expired", 410),
}


class AnswerBody(BaseModel):
    """Never log or echo `value`."""

    nonce: str = Field(min_length=1, max_length=128)
    value: str | None = Field(default=None, max_length=4096)
    decline: bool = False


def _peer(request: Request) -> str:
    return request.client.host if request.client else ""


def _allowed(request: Any, *, require_loopback: bool = False) -> bool:
    if require_loopback:
        return _peer(request) in _LOOPBACK
    if _peer(request) in _LOOPBACK:
        return True
    if getattr(request.state, "user", None) is not None:
        return True
    return bool(getattr(request.app.state, "secret_input_allow_remote", False))


def _answer_url(request: Request, request_id: str) -> str:
    return f"{request.url.scheme}://{request.url.netloc}/api/secret-input/card/{request_id}"


def build_router(*, registry, config, vault) -> APIRouter:
    router = APIRouter()

    def guard(request: Request, *, loopback_only: bool = False) -> Response | None:
        if config.allow_remote or _allowed(request, require_loopback=loopback_only):
            return None
        return JSONResponse({"detail": "secret-input: loopback or authenticated caller required"},
                            status_code=403)

    @router.get("/pending")
    async def pending(request: Request, session_id: str | None = None):
        # 省略 session_id 的清单是终端入口，只允许 loopback
        blocked = guard(request, loopback_only=session_id is None)
        if blocked is not None:
            return blocked
        registry.sweep()
        items = []
        for rec in registry.list_pending(session_id):
            items.append({
                "request_id": rec.request_id,
                "label": rec.label,
                "reason": rec.reason,
                "placeholder": rec.placeholder,
                "expires_in": round(rec.seconds_left(), 1),
                "answer_url": _answer_url(request, rec.request_id),
            })
        return {"requests": items}

    @router.get("/card/{request_id}")
    async def card(request: Request, request_id: str):
        blocked = guard(request)
        if blocked is not None:
            return blocked
        return html_response(request_id)

    @router.get("/card/{request_id}/meta")
    async def meta(request: Request, request_id: str):
        blocked = guard(request)
        if blocked is not None:
            return blocked
        rec = registry.get(request_id)
        if rec is None:
            return JSONResponse({"detail": "This secret request no longer exists."},
                                status_code=404)
        payload = {
            "label": rec.label,
            "reason": rec.reason,
            "placeholder": rec.placeholder,
            "status": rec.status,
            "expires_in": round(rec.seconds_left(), 1),
            "status_text": _status_text(rec.status),
        }
        if rec.status == "pending":
            payload["nonce"] = rec.nonce
        else:
            payload["answered_peer"] = rec.answered_peer
            payload["answered_at"] = _fmt_time(rec.answered_at)
        return payload

    @router.post("/card/{request_id}/answer")
    async def answer(request: Request, request_id: str, body: AnswerBody):
        blocked = guard(request)
        if blocked is not None:
            return blocked
        ok, why = await registry.answer(
            request_id, nonce=body.nonce, value=body.value, decline=body.decline,
            peer=_peer(request), user_agent=request.headers.get("user-agent", ""),
        )
        if not ok:
            code = _ANSWER_MAP.get(why, ("invalid_nonce", 403))[1]
            return JSONResponse({"detail": why}, status_code=code)
        rec = registry.find_record_any_state(request_id)
        status = rec.status if rec is not None else "saved"
        if status == "saved":
            vault.sweep()
        # 只回终结状态，绝不回值、绝不回 ref
        return {"status": status}

    return router


def _status_text(status: str) -> str:
    return {
        "pending": "等待输入", "saved": "已保存", "declined": "已拒绝",
        "cancelled": "已取消", "timed_out": "已超时",
    }.get(status, status)


def _fmt_time(epoch: float | None) -> str:
    if not epoch:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))
```

**先做这条（R20 交接，控制器裁定）：** `AnswerBody` 必须前置校验空值——`value` 为空且 `decline` 为假时直接回 `422 {"detail":"empty_value"}`，**不得**进入 `registry.answer()`。原因：`records.py` 对"nonce 正确但值为空"只能返回闭集里的 `invalid_nonce`（枚举由 spec 定死），照直渲染会让用户在空输入框上按保存后看到"nonce 不对"。`_ANSWER_MAP` 需加 `empty_value: 422`。
另外 decline 现在也带 `answered_peer`/`answered_at`（Task 3 修复轮 1），meta 的 `status_text` 文案要区分"提交"与"拒绝"，不许对 decline 显示"已提交"。

`registry.find_record_any_state(request_id)` 是本任务给 `RequestRegistry` 补的一个小方法（已终结的记录已从 `_pending` 移除，但答后需要它的终态）。在 `backend/records.py` 里加：

```python
    def find_record_any_state(self, request_id: str) -> SecretRequest | None:
        """Finished requests are unregistered but the answer handler still
        needs their terminal status to report back."""
        return self._graveyard.get(request_id)
```

并在 `_finish` 里落一份引用：`__init__` 增 `self._graveyard: dict[str, SecretRequest] = {}`，`_finish` 末尾增
```python
        self._graveyard[request.request_id] = request
        while len(self._graveyard) > 256:
            self._graveyard.pop(next(iter(self._graveyard)))
```

- [ ] **Step 7: 跑全套测试确认通过**

Run: `cd /home/leo/develop/secret_input && /home/leo/miniforge3/bin/python3.12 -m unittest discover -s tests -t . -v 2>&1 | tail -8`
Expected: `OK`（config 3 + vault 6 + records 11 + scrub 7 + middleware 9 + card_page 4 + router 10）

- [ ] **Step 8: 红对照 —— loopback 门**

Run:
```bash
cd /home/leo/develop/secret_input && cp backend/router.py /tmp/router.bak
/home/leo/miniforge3/bin/python3.12 - <<'PY'
from pathlib import Path
p = Path("backend/router.py"); t = p.read_text()
old = "    blocked = guard(request, loopback_only=session_id is None)\n    if blocked is not None:\n        return blocked"
assert old in t, "pending guard not found verbatim"
p.write_text(t.replace(old, "    # NEGATIVE CONTROL: gate removed", 1))
PY
/home/leo/miniforge3/bin/python3.12 -m unittest tests.test_router 2>&1 | tail -4
cp /tmp/router.bak backend/router.py && /home/leo/miniforge3/bin/python3.12 -m unittest tests.test_router 2>&1 | tail -2
```
Expected: `FAILED (failures=1)`（`test_pending_without_session_requires_loopback`），还原后 `OK`。

- [ ] **Step 9: Commit**

```bash
git add backend/router.py backend/card_page.py backend/records.py tests/test_router.py tests/test_card_page.py
git commit -m "feat: 答题面（HTML 答页 + 四端点 + loopback/nonce 门 + 答者审计）"
```

---

### Task 7: `request_secret` —— 阻塞、取消检测与幂等重放

**Files:**
- Create: `backend/identity.py`、`backend/tools.py`、`tests/test_tools_request.py`
- Modify: `backend/records.py`（补 `peek_value`）、`backend/vault.py`（补 `peek`）

**Interfaces:**
- Consumes: `RequestRegistry`（Task 3）、`PluginConfig`（Task 1）
- Produces: `identity()` → `dict[str,str]`、`cancel_event()` → `asyncio.Event | None`、`SecretTools.request_secret`

- [ ] **Step 1: 写 `backend/identity.py`**（宿主 ContextVar 的唯一出口，测试好替换）

```python
# -*- coding: utf-8 -*-
"""The only place we read host per-call context. Imports stay inside functions
so unit tests never need a booted host."""

from __future__ import annotations

import asyncio


def identity() -> dict[str, str]:
    out = {"session_id": "", "agent_id": "", "user_id": "", "channel": ""}
    try:
        from qwenpaw.app import agent_context as ac
    except Exception:  # noqa: BLE001
        return out
    for key, name in (("session_id", "get_current_session_id"),
                      ("agent_id", "get_current_agent_id"),
                      ("user_id", "get_current_user_id"),
                      ("channel", "get_current_channel")):
        getter = getattr(ac, name, None)
        if callable(getter):
            out[key] = str(getter() or "")
    return out


def cancel_event() -> asyncio.Event | None:
    try:
        from qwenpaw.tool_calls._ctxvars import get_call_context
    except Exception:  # noqa: BLE001
        return None
    ctx = get_call_context()
    return getattr(ctx, "cancel_event", None)


def project_dir() -> str:
    try:
        from qwenpaw.config.context import get_current_project_dir
    except Exception:  # noqa: BLE001
        return ""
    return str(get_current_project_dir() or "")
```

- [ ] **Step 2: 写失败测试 `tests/test_tools_request.py`**

```python
import asyncio
import io
import json
import unittest

from tests.support import DUMMY

from backend.config import PluginConfig
from backend.records import RequestRegistry
from backend.scrub import Scrubber
from backend.tools import SecretTools
from backend.vault import Vault

ID = {"session_id": "s1", "agent_id": "a1", "user_id": "u1", "channel": "console"}


def make(cancel=None):
    vault = Vault(ttl_seconds=300)
    registry = RequestRegistry(vault, timeout_seconds=600, max_pending=8)
    scrubber = Scrubber()
    tools = SecretTools(registry=registry, vault=vault, scrubber=scrubber,
                        config=PluginConfig(), identity=lambda: dict(ID),
                        cancel_event=cancel or (lambda: None))
    return tools, registry, scrubber, vault


class RequestSecretTest(unittest.TestCase):
    def test_saved_result_has_ref_and_no_value(self):
        tools, registry, _scr, _v = make()

        async def scenario():
            task = asyncio.create_task(tools.request_secret("GitHub token", "signing"))
            await asyncio.sleep(0)
            rec = registry.list_pending("s1")[0]
            await registry.answer(rec.request_id, nonce=rec.nonce, value=DUMMY)
            return json.loads(await task)

        out = asyncio.run(scenario())
        self.assertEqual(out["status"], "saved")
        self.assertTrue(out["ref"].startswith("sr_"))
        self.assertNotIn(DUMMY, json.dumps(out))

    def test_declined_and_timed_out_shapes(self):
        tools, registry, _scr, _v = make()

        async def scenario():
            task = asyncio.create_task(tools.request_secret("l", "r"))
            await asyncio.sleep(0)
            rec = registry.list_pending("s1")[0]
            await registry.answer(rec.request_id, nonce=rec.nonce, decline=True)
            return json.loads(await task)

        self.assertEqual(asyncio.run(scenario())["status"], "declined")

        tools, registry, _scr, _v = make()

        async def expire():
            task = asyncio.create_task(tools.request_secret("l", "r", timeout_seconds=30))
            await asyncio.sleep(0)
            rec = registry.list_pending("s1")[0]
            rec.expires_at = 0.0
            registry.sweep()
            return json.loads(await task)

        out = asyncio.run(expire())
        self.assertEqual(out["status"], "timed_out")
        self.assertIn("client_request_id", out["hint"])

    def test_cancel_event_short_circuits_to_cancelled(self):
        tools, registry, _scr, _v = make(cancel=lambda: self.evt)
        self.evt = asyncio.Event()

        async def scenario():
            task = asyncio.create_task(tools.request_secret("l", "r"))
            await asyncio.sleep(0)
            self.evt.set()
            return json.loads(await task)

        out = asyncio.run(scenario())
        self.assertEqual(out["status"], "cancelled")
        self.assertEqual(out["reason"], "run_ended")

    def test_bad_label_returns_error_string_not_raise(self):
        tools, _r, _s, _v = make()
        out = json.loads(asyncio.run(tools.request_secret("", "  ")))
        self.assertEqual(out["status"], "error")

    def test_out_of_range_timeout_is_reported_not_raised(self):
        tools, _r, _s, _v = make()
        out = json.loads(asyncio.run(tools.request_secret("l", "r", timeout_seconds=5)))
        self.assertEqual(out["status"], "error")
        self.assertIn("30..3600", out["message"])

    def test_client_request_id_replay_gives_the_same_ref(self):
        tools, registry, _scr, vault = make()

        async def scenario():
            first = asyncio.create_task(tools.request_secret("l", "r", client_request_id="c1"))
            await asyncio.sleep(0)
            rec = registry.list_pending("s1")[0]
            await registry.answer(rec.request_id, nonce=rec.nonce, value=DUMMY)
            a = json.loads(await first)
            b = json.loads(await tools.request_secret("l", "r", client_request_id="c1"))
            return a, b, rec

        a, b, rec = asyncio.run(scenario())
        self.assertEqual(a["ref"], rec.ref)
        self.assertEqual(b["ref"], rec.ref)          # 重放可恢复丢失的结果

    def test_replay_after_consumption_reports_unavailable(self):
        tools, registry, _scr, vault = make()

        async def scenario():
            first = asyncio.create_task(tools.request_secret("l", "r", client_request_id="c9"))
            await asyncio.sleep(0)
            rec = registry.list_pending("s1")[0]
            await registry.answer(rec.request_id, nonce=rec.nonce, value=DUMMY)
            await first
            vault.consume(rec.ref)
            return json.loads(await tools.request_secret("l", "r", client_request_id="c9"))

        out = asyncio.run(scenario())
        self.assertEqual(out["status"], "ref_unavailable")
        self.assertIn("request_secret", out["hint"])

    def test_answer_page_url_is_logged_for_the_headless_path(self):
        # spec 8.3 第 2 层：TUI/ACP/API 驱动时，日志里那条 URL 是唯一入口
        import logging

        tools, registry, _scr, _v = make()
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            async def scenario():
                task = asyncio.create_task(tools.request_secret("l", "r"))
                await asyncio.sleep(0)
                rec = registry.list_pending("s1")[0]
                await registry.answer(rec.request_id, nonce=rec.nonce, value=DUMMY)
                await task
                return rec

            rec = asyncio.run(scenario())
        finally:
            root.removeHandler(handler)
        logged = buf.getvalue()
        self.assertIn(rec.request_id, logged)
        self.assertIn("/api/secret-input/card/", logged)
        self.assertNotIn(DUMMY, logged)

    def test_no_url_or_peer_in_the_tool_result(self):
        tools, registry, _scr, _v = make()

        async def scenario():
            task = asyncio.create_task(tools.request_secret("l", "r"))
            await asyncio.sleep(0)
            rec = registry.list_pending("s1")[0]
            await registry.answer(rec.request_id, nonce=rec.nonce, value=DUMMY)
            return await task

        text = asyncio.run(scenario())
        for needle in ("http://", "https://", "127.0.0.1", "/api/"):
            self.assertNotIn(needle, text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd /home/leo/develop/secret_input && /home/leo/miniforge3/bin/python3.12 -m unittest tests.test_tools_request -v 2>&1 | tail -3`
Expected: `ModuleNotFoundError: No module named 'backend.tools'`

- [ ] **Step 4: 写 `backend/tools.py`（本任务只到 `request_secret`；Task 8 续写两个消费点）**

```python
# -*- coding: utf-8 -*-
"""Tool wrappers. They always return a JSON string and never raise."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

from .records import RequestRegistry
from .scrub import Scrubber
from .vault import Vault

logger = logging.getLogger(__name__)

_POLL_SECONDS = 0.2


def _text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


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
                             timeout_seconds: int = 0,   # R44：0/省略 = config 默认
                             client_request_id: str = "") -> str:
        try:
            if not str(label).strip() or not str(reason).strip():
                return _text({"status": "error",
                              "message": "label and reason are both required (non-empty)."})
            who = self._identity() or {}
            request = await self._registry.create(
                label=label, reason=reason, placeholder=placeholder,
                session_id=who.get("session_id", ""), agent_id=who.get("agent_id", ""),
                user_id=who.get("user_id", ""), channel=who.get("channel", ""),
                client_request_id=(client_request_id or None),
                timeout_seconds=timeout_seconds,
            )
            logger.info(
                "secret-input: answer page for request %s -> %s",
                request.request_id,
                (self._config.public_base_url or "http://127.0.0.1:19999").rstrip("/")
                + "/api/secret-input/card/"
                + request.request_id,
            )
            outcome = await self._await(request)
            if outcome["status"] == "saved":
                value = self._registry.peek_value(request.ref) if request.ref else None
                if value is not None:
                    self._scrubber.register(value)
            return _text(outcome)
        except ValueError as exc:
            return _text({"status": "error", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 刻意：绝不把异常抛给宿主
            logger.exception("secret-input: request_secret failed")
            return _text({"status": "error",
                          "message": f"secret request failed: {type(exc).__name__}. "
                                     f"Retry once, then tell the user to open the console."})

    async def _await(self, request) -> dict[str, Any]:
        """Race the human's answer against tool cancellation and the deadline."""
        cancel = self._cancel_event()
        while True:
            if request.future.done():
                break
            if cancel is not None and cancel.is_set():
                await self._registry.cancel(request.request_id, "run_ended")
                break
            if request.status == "pending" and request.seconds_left() <= 0:
                self._registry.sweep()
                break
            await asyncio.sleep(_POLL_SECONDS)
        outcome = dict(request.future.result()) if request.future.done() else {
            "status": request.status,
        }
        if outcome.get("status") == "timed_out":
            outcome["hint"] = ("The card is closed. Ask again with request_secret using a "
                               "NEW client_request_id if it is still needed.")
        elif outcome.get("status") == "declined":
            outcome["hint"] = "The user chose not to provide it. Do not ask again for the same thing."
        elif outcome.get("status") == "cancelled":
            outcome.setdefault("reason", request.cancel_reason or "run_ended")
        elif outcome.get("status") == "saved":
            outcome["hint"] = ("Pass ref to secret_exec or secret_export. "
                               "You never see the value.")
        return outcome
```

`_await` 用轮询而不是 `asyncio.wait`，理由写进注释：`cancel_event` 与 `future` 的三方向竞速在 `_supervise` 的取消语义下轮询更稳（不必为 `Event.wait()` 造任务再取消）。`_POLL_SECONDS = 0.2` 与 spec §15 A6 的验证对应。

`RequestRegistry` 需要再补一个方法（答后到消费前的取值只走内部，不给模型）：

```python
    def peek_value(self, ref: str | None) -> str | None:
        """Non-consuming read for the scrubber only."""
        if not ref:
            return None
        return self._vault_peek(ref)
```

在 `RequestRegistry.__init__` 增 `self._vault_peek = getattr(vault, "peek", None) or (lambda _ref: None)`，并在 `Vault` 增：

```python
    def peek(self, ref: str) -> str | None:
        value, status = self._live(ref)
        return value if status == "ok" else None
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd /home/leo/develop/secret_input && /home/leo/miniforge3/bin/python3.12 -m unittest tests.test_tools_request -v 2>&1 | tail -5`
Expected: `OK` / `9 tests`

- [ ] **Step 6: 红对照 —— 结果里出现 URL 就必红**

Run:
```bash
cd /home/leo/develop/secret_input && cp backend/tools.py /tmp/tools.bak
/home/leo/miniforge3/bin/python3.12 - <<'PY'
from pathlib import Path
p = Path("backend/tools.py"); t = p.read_text()
old = '        if outcome.get("status") == "timed_out":'
assert old in t
t = t.replace(old, '        outcome["card_url"] = "http://127.0.0.1:19999/api/secret-input/card/x"\n' + old, 1)
p.write_text(t)
PY
/home/leo/miniforge3/bin/python3.12 -m unittest tests.test_tools_request 2>&1 | tail -3
cp /tmp/tools.bak backend/tools.py && /home/leo/miniforge3/bin/python3.12 -m unittest tests.test_tools_request 2>&1 | tail -2
```
Expected: `FAILED (failures=…)` 至少含 `test_no_url_or_peer_in_the_tool_result` 与 `test_timed_out_shape`，还原后 `OK`。

- [ ] **Step 7: Commit**

```bash
git add backend/identity.py backend/tools.py backend/records.py backend/vault.py tests/test_tools_request.py
git commit -m "feat: request_secret（阻塞/取消/超时/幂等重放；结果不含 URL 与值，日志留答页 URL）"
```

---

### Task 8: `secret_exec` 与 `secret_export` —— 值在插件进程里被换掉

**Files:**
- Modify: `backend/tools.py`（追加两个方法与 `SECRET_MARKER`/`_clip`/`_ENV_KEY_RE`）
- Modify: `backend/vault.py`（补 `reopen`）
- Create: `tests/test_tools_consume.py`

**Interfaces:**
- Consumes: `Vault.consume`（Task 2）、`Scrubber.replace`（Task 4）、`identity.project_dir`（Task 7）
- Produces: `SecretTools.secret_exec`、`SecretTools.secret_export`、模块常量 `SECRET_MARKER = "{{secret}}"`

- [ ] **Step 1: 写失败测试 `tests/test_tools_consume.py`**

```python
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from tests.support import DUMMY

from backend.config import PluginConfig
from backend.records import RequestRegistry
from backend.scrub import REDACT, Scrubber
from backend.tools import SECRET_MARKER, SecretTools
from backend.vault import Vault


def make(env_writer=None):
    vault = Vault(ttl_seconds=300)
    registry = RequestRegistry(vault, timeout_seconds=600, max_pending=8)
    scrubber = Scrubber()
    ref = vault.issue(DUMMY)
    scrubber.register(DUMMY)
    tools = SecretTools(registry=registry, vault=vault, scrubber=scrubber,
                        config=PluginConfig(), identity=lambda: {},
                        cancel_event=lambda: None, env_writer=env_writer,
                        project_dir=lambda: self_dir())
    return tools, vault, scrubber, ref


def self_dir():
    return str(Path(tempfile.gettempdir()).resolve())


def run(coro):
    return asyncio.run(coro)


class ExecTest(unittest.TestCase):
    def test_placeholder_is_substituted_outside_the_model_and_output_is_scrubbed(self):
        tools, _v, _s, ref = make()
        out = json.loads(run(tools.secret_exec(
            ref, f'printf "%s" {SECRET_MARKER}')))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["stdout"], REDACT)          # 模型侧看到的是 ***
        self.assertEqual(out["ref"], "consumed")

    def test_child_really_sees_the_value_via_env(self):
        # 阳性对照：子进程写进文件的必须是真值，否则"替身成功"是假的
        tools, _v, _s, ref = make()
        probe = Path(tempfile.mkdtemp()) / "seen.txt"
        out = json.loads(run(tools.secret_exec(
            ref, f'printf "%s" "${{SECRET}}" > {probe} && echo done')))
        self.assertEqual(out["status"], "ok", out)
        self.assertEqual(probe.read_text(encoding="utf-8"), DUMMY)
        self.assertNotIn(DUMMY, json.dumps(out))

    def test_second_use_of_same_ref_is_ref_unavailable(self):
        tools, _v, _s, ref = make()
        run(tools.secret_exec(ref, "true"))
        out = json.loads(run(tools.secret_exec(ref, "true")))
        self.assertEqual(out["status"], "ref_unavailable")
        self.assertIn("request_secret", out["hint"])

    def test_expired_ref_reports_expired(self):
        tools, vault, _s, ref = make()
        vault._issued[ref] = (DUMMY, 0.0)
        vault.sweep()
        out = json.loads(run(tools.secret_exec(ref, "true")))
        self.assertEqual(out["status"], "ref_expired")

    def test_output_is_capped_below_the_host_pruning_threshold(self):
        tools, _v, _s, ref = make()
        out = json.loads(run(tools.secret_exec(
            ref, f"seq 1 20000 | tr '\\n' ' '; echo", max_output_bytes=1024)))
        self.assertLessEqual(len(out["stdout"].encode()), 1024 + len(REDACT) + 64)
        self.assertIn("[truncated by secret-input]", out["stdout"])

    def test_cwd_outside_project_dir_is_refused_and_ref_not_consumed(self):
        tools, vault, _s, ref = make()
        out = json.loads(run(tools.secret_exec(ref, "true", cwd="/"]))
        self.assertEqual(out["status"], "error")
        self.assertEqual(vault.status(ref), "live")      # 失败绝不消费 ref

    def test_failing_command_still_returns_string_without_value(self):
        tools, _v, _s, ref = make()
        out = json.loads(run(tools.secret_exec(
            ref, f'printf "%s" {SECRET_MARKER} >&2; exit 7')))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["exit_code"], 7)
        self.assertEqual(out["stderr"], REDACT)

    def test_internal_error_string_carries_no_value(self):
        tools, _v, _s, ref = make()
        tools._vault.consume = lambda _r: (_ for _ in ()).throw(RuntimeError(DUMMY))
        out = json.loads(run(tools.secret_exec(ref, "true")))
        self.assertEqual(out["status"], "error")
        self.assertNotIn(DUMMY, json.dumps(out))


class ExportTest(unittest.TestCase):
    def test_export_writes_env_key_and_consumes_ref(self):
        seen = {}

        def writer(key, value):
            seen[key] = value
            return {key: value}

        tools, vault, _s, ref = make(env_writer=writer)
        out = json.loads(run(tools.secret_export(ref, "GITHUB_TOKEN")))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(seen["GITHUB_TOKEN"], DUMMY)     # 插件确实把值交出去了
        self.assertNotIn(DUMMY, json.dumps(out))
        self.assertEqual(vault.status(ref), "gone")

    def test_bad_env_key_is_rejected_and_ref_survives(self):
        tools, vault, _s, ref = make(env_writer=lambda k, v: {})
        for bad in ("lower_case", "1TOKEN", "A" * 80, "GITHUB TOKEN", ""):
            out = json.loads(run(tools.secret_export(ref, bad)))
            self.assertEqual(out["status"], "error", bad)
            self.assertEqual(vault.status(ref), "live")

    def test_writer_failure_does_not_consume_ref(self):
        def boom(_key, _value):
            raise OSError("env store is read-only")

        tools, vault, _s, ref = make(env_writer=boom)
        out = json.loads(run(tools.secret_export(ref, "GITHUB_TOKEN")))
        self.assertEqual(out["status"], "error")
        self.assertIn("not consumed", out["message"])
        self.assertEqual(vault.status(ref), "live")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/leo/develop/secret_input && /home/leo/miniforge3/bin/python3.12 -m unittest tests.test_tools_consume -v 2>&1 | tail -4`
Expected: `AttributeError: 'SecretTools' object has no attribute 'secret_exec'`

- [ ] **Step 3: 在 `backend/tools.py` 追加两个方法与常量**

```python
SECRET_MARKER = "{{secret}}"
_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_MIN_OUTPUT_BYTES = 1024
_MAX_OUTPUT_BYTES = 32768


def _clip(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    clipped = raw[:limit].decode("utf-8", "ignore")
    return clipped + f"\n[truncated by secret-input: {len(raw) - limit} bytes dropped]"
```

方法体（放进 `class SecretTools`）：

```python
    async def secret_exec(self, ref: str, command: str, env_name: str = "SECRET",
                          timeout_seconds: int = 120, cwd: str = "",
                          max_output_bytes: int = 32768) -> str:
        value = None
        try:
            template = str(command or "")
            if not template.strip():
                return _text({"status": "error", "message": "command is required."})
            limit = min(max(int(max_output_bytes), _MIN_OUTPUT_BYTES), _MAX_OUTPUT_BYTES)
            timeout = min(max(int(timeout_seconds), 1), 600)
            workdir, bad = self._resolve_cwd(cwd)
            if bad:
                return _text({"status": "error", "message": bad})

            value, status = self._vault.consume(ref)
            if status != "ok" or value is None:
                return _text({"status": status,
                              "hint": "Ask the user again with request_secret."})
            self._scrubber.register(value)

            real = template.replace(SECRET_MARKER, value)
            env = dict(os.environ)
            env[str(env_name or "SECRET")] = value
            argv = ["/bin/sh", "-c", real]
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=workdir, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                # 超时也必须让 ref 保持已消费（值可能已被子进程用掉）
                return _text({"status": "error",
                              "message": f"command timed out after {timeout}s; the ref was "
                                         f"consumed, ask again if you need to retry."})

            stdout = self._scrubber.replace(out.decode("utf-8", "replace"))
            stderr = self._scrubber.replace(err.decode("utf-8", "replace"))
            note = None
            if SECRET_MARKER not in template:
                note = (f"{{secret}} placeholder unused; the value reached the command only "
                        f"through ${env_name}.")
            payload = {"status": "ok", "exit_code": proc.returncode,
                       "stdout": _clip(stdout, limit), "stderr": _clip(stderr, limit),
                       "ref": "consumed"}
            if note:
                payload["note"] = note
            return _text(payload)
        except Exception as exc:  # noqa: BLE001 - 异常文本里绝不带 command 实串
            logger.exception("secret-input: secret_exec failed")
            return _text({"status": "error",
                          "message": f"secret_exec failed: {type(exc).__name__} "
                                     f"(the command template and the secret are not shown)"})

    async def secret_export(self, ref: str, env_key: str) -> str:
        try:
            key = str(env_key or "")
            if not _ENV_KEY_RE.match(key):
                return _text({"status": "error",
                              "message": "env_key must match ^[A-Z_][A-Z0-9_]{0,63}$, got: "
                                         f"{key!r}. The ref was not consumed."})
            value, status = self._vault.consume(ref)
            if status != "ok" or value is None:
                return _text({"status": status,
                              "hint": "Ask the user again with request_secret."})
            self._scrubber.register(value)
            try:
                self._write_env(key, value)
            except Exception as exc:  # noqa: BLE001
                self._vault.reopen(ref, value)      # 没写成功就把 ref 还回去
                return _text({"status": "error",
                              "message": f"env store write failed: {type(exc).__name__}. "
                                         f"The ref was not consumed; retry or re-ask."})
            return _text({"status": "ok", "env_key": key, "ref": "consumed",
                          "hint": f"Later shell tools can use ${key}. The value is not in "
                                  f"context; it is in the host env store and process "
                                  f"environment."})
        except Exception as exc:  # noqa: BLE001
            logger.exception("secret-input: secret_export failed")
            return _text({"status": "error",
                          "message": f"secret_export failed: {type(exc).__name__}"})

    def _write_env(self, key: str, value: str) -> None:
        # R46：`backend/tools.py` 由导入门禁止 import 宿主包（`tests/test_secret_store_gate.py`
        # 的 HOST_GATED_MODULES 覆盖 tools.py，函数体内的 import 也算）。宿主调用只能走
        # Task 9 在 main.py 里注入的 env_writer；没注入就是配置错误，直说而不是偷偷 import。
        if self._env_writer is None:
            raise RuntimeError("env writer not wired (plugin assembly defect)")
        self._env_writer(key, value)

    def _resolve_cwd(self, cwd: str) -> tuple[str | None, str]:
        raw = str(cwd or "").strip()
        base = self._project_dir() if callable(self._project_dir) else ""
        if not raw:
            return (str(Path(base).resolve()) if base else None), ""
        target = Path(raw) if os.path.isabs(raw) else (Path(base) / raw if base else Path(raw))
        try:
            resolved = target.resolve()
        except OSError as exc:
            return None, f"cannot resolve cwd: {exc}"
        if not resolved.is_dir():
            return None, f"cwd is not a directory: {resolved.name}"
        if base:
            root = Path(base).resolve()
            if root not in resolved.parents and resolved != root:
                return None, ("cwd must stay inside the current project directory "
                              f"({root.name}); got {resolved.name}")
        return str(resolved), ""
```

`Vault` 补 `reopen`（导出失败时把 ref 还回去，spec §7.3 的"失败不消费"）：
```python
    def reopen(self, ref: str, value: str) -> None:
        """Give a ref back to a caller whose side effect did not land."""
        if isinstance(ref, str) and ref.startswith(_PREFIX):
            self._issued[ref] = (value, time.monotonic())
            self._expired.discard(ref)
```

`tools.py` 顶部需要 `import os, re` 与 `from pathlib import Path`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /home/leo/develop/secret_input && /home/leo/miniforge3/bin/python3.12 -m unittest tests.test_tools_consume -v 2>&1 | tail -6`
Expected: `OK` / `11 tests`

- [ ] **Step 5: 红对照 —— 擦洗不是装饰**

```bash
cd /home/leo/develop/secret_input && cp backend/tools.py /tmp/tools.bak
/home/leo/miniforge3/bin/python3.12 - <<'PY'
from pathlib import Path
p = Path("backend/tools.py"); t = p.read_text()
old = '            stdout = self._scrubber.replace(out.decode("utf-8", "replace"))'
assert old in t
t = t.replace(old, '            stdout = out.decode("utf-8", "replace")', 1)
p.write_text(t)
PY
/home/leo/miniforge3/bin/python3.12 -m unittest tests.test_tools_consume 2>&1 | tail -4
cp /tmp/tools.bak backend/tools.py && /home/leo/miniforge3/bin/python3.12 -m unittest tests.test_tools_consume 2>&1 | tail -2
```
Expected: `FAILED (failures=…)`（含占位符替换用例、`printf` 到 stderr 用例），还原后 `OK`。

- [ ] **Step 6: Commit**

```bash
git add backend/tools.py backend/vault.py tests/test_tools_consume.py
git commit -m "feat: 消费点 secret_exec/secret_export（占位符在插件侧替换、输出封顶且擦洗、失败不消费 ref）"
```

---

### Task 9: 装配 —— 提示词段、全部扩展点接线、I1 七路不变式测试

**Files:**
- Create: `backend/prompt.py`、`tests/test_invariants.py`
- Modify: `backend/main.py`（替换 Task 1 的空壳 `register`）

**Interfaces:**
- Consumes: 前 8 个任务的全部公开对象
- Produces: `PROMPT_SECTION`（逐字文本）、完整的 `SecretInputPlugin.register(api)`

- [ ] **Step 1: 写 `backend/prompt.py`（逐字，不要改措辞）**

```python
# -*- coding: utf-8 -*-
"""System-prompt section. Mirrors T3 Code's rule set for request_secret."""

PROMPT_SECTION = """\
Secrets (tokens, API keys, signing secrets, passwords):
- Ask with the `request_secret` tool only. It shows a private card to the user.
- Never ask for a secret in chat, never invent one, and never repeat one back.
- The value is never returned to you. You get a one-time `ref` instead.
- Spend a `ref` with `secret_exec` ({{secret}} placeholder, plugin-side substitution)
  or `secret_export` (persist into the host env store as $ENV_KEY).
- `ref_unavailable` / `ref_expired` means: call `request_secret` again.
- Never put a secret into a tool argument, a file you write, or a command echo.\
"""
```

- [ ] **Step 2: 写失败测试 `tests/test_invariants.py`（I1 七路）**

```python
"""I1: the plaintext never reaches the model's side of the wire.

Seven capture points. Each is paired with the control in tests that proves the
capture is reading something real.

test_3 必须多走一路：其中一次请求的 `label` 由模型写成含 `DUMMY` 的串。`records.py`
对日志里的 label 只做 120 字符截断（R19），短密钥仍会原样落宿主日志——这一路把 spec §10.1
"宿主日志：不会" 从推断变成实测，也是 §10.2 item 4（模型自撰文案不可信）唯一的落地证据。
"""

import asyncio
import io
import json
import logging
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from tests.support import DUMMY

from backend.config import PluginConfig
from backend.middleware import SecretScrubMiddleware
from backend.records import RequestRegistry
from backend.router import build_router
from backend.scrub import REDACT, Scrubber
from backend.tools import SECRET_MARKER, SecretTools
from backend.vault import Vault

ID = {"session_id": "s1", "agent_id": "a1", "user_id": "u1", "channel": "console"}


class InvariantsTest(unittest.TestCase):
    def setUp(self):
        self.vault = Vault(ttl_seconds=300)
        self.registry = RequestRegistry(self.vault, timeout_seconds=600, max_pending=8)
        self.scrubber = Scrubber()
        self.config = PluginConfig()
        self.tools = SecretTools(registry=self.registry, vault=self.vault,
                                 scrubber=self.scrubber, config=self.config,
                                 identity=lambda: dict(ID), cancel_event=lambda: None,
                                 env_writer=lambda k, v: None,
                                 project_dir=lambda: str(Path(tempfile.gettempdir()).resolve()))
        app = FastAPI()
        app.include_router(build_router(registry=self.registry, config=self.config,
                                        vault=self.vault), prefix="/api/secret-input")
        self.client = TestClient(app, client=("127.0.0.1", 52000))
        self.log_capture = io.StringIO()
        self.handler = logging.StreamHandler(self.log_capture)
        logging.getLogger().addHandler(self.handler)

    def tearDown(self):
        logging.getLogger().removeHandler(self.handler)

    def _full_flow(self):
        async def scenario():
            task = asyncio.create_task(self.tools.request_secret("GitHub token", "signing"))
            await asyncio.sleep(0)
            rec = self.registry.list_pending("s1")[0]
            res = self.client.post(f"/api/secret-input/card/{rec.request_id}/answer",
                                   json={"nonce": rec.nonce, "value": DUMMY})
            result_text = await task
            exec_text = await self.tools.secret_exec(rec.ref, f'printf "%s" {SECRET_MARKER}')
            export_text = await self.tools.secret_export(self.vault.issue(DUMMY), "GITHUB_TOKEN")
            return rec, res, result_text, exec_text, export_text

        return asyncio.run(scenario())

    def test_1_input_schema_has_no_value_bearing_field(self):
        import inspect

        allow = {"label", "reason", "placeholder", "timeout_seconds", "client_request_id",
                 "ref", "command", "env_name", "cwd", "max_output_bytes", "env_key"}
        for fn in (self.tools.request_secret, self.tools.secret_exec, self.tools.secret_export):
            params = set(inspect.signature(fn).parameters) - {"self"}
            self.assertTrue(params <= allow, f"{fn.__name__}: {params - allow}")

    def test_2_tool_results_carry_no_value(self):
        _rec, _res, result_text, exec_text, export_text = self._full_flow()
        for text in (result_text, exec_text, export_text):
            self.assertNotIn(DUMMY, text)
            self.assertIn("sr_", result_text)

    def test_3_nothing_reaches_the_log(self):
        self._full_flow()
        self.handler.flush()
        self.assertNotIn(DUMMY, self.log_capture.getvalue())

    def test_4_no_endpoint_response_carries_the_value(self):
        rec, res, result_text, _e, _x = self._full_flow()
        bodies = [res.text]
        for path in (f"/api/secret-input/card/{rec.request_id}",
                     f"/api/secret-input/card/{rec.request_id}/meta"):
            bodies.append(self.client.get(path).text)
        bodies.append(self.client.get("/api/secret-input/pending", params={"session_id": "s1"}).text)
        bodies.append(self.client.get("/api/secret-input/pending").text)
        for body in bodies:
            self.assertNotIn(DUMMY, body)

    def test_5_memory_bound_block_is_scrubbed(self):
        self._full_flow()
        mw = SecretScrubMiddleware(self.scrubber)

        async def drive():
            async def next_handler():
                yield ToolResponse(content=[TextBlock(text=f"token is {DUMMY}")])

            async for event in mw.on_acting(None, {"tool_call": object()}, next_handler):
                return event

        block_text = asyncio.run(drive()).content[0].text
        self.assertEqual(block_text, f"token is {REDACT}")

    def test_6_disk_artifact_is_scrubbed(self):
        from qwenpaw.agents.tools.utils import ToolResultPruner

        self._full_flow()
        mw = SecretScrubMiddleware(self.scrubber)

        async def drive():
            async def next_handler():
                yield ToolResponse(content=[TextBlock(text=("tok " + DUMMY + " ") * 80)])

            async for event in mw.on_acting(None, {"tool_call": object()}, next_handler):
                return event

        text = asyncio.run(drive()).content[0].text
        with tempfile.TemporaryDirectory() as tmp:
            _pruned, changed = ToolResultPruner(tmp).prune_output(text, max_bytes=64,
                                                                  metadata={})
            files = list(Path(tmp).glob("*.txt"))
            self.assertTrue(changed and files, "没走到落盘分支")
            for path in files:
                self.assertNotIn(DUMMY, path.read_text(encoding="utf-8"))

    def test_7_error_strings_carry_no_value(self):
        self._full_flow()
        broken = SecretTools(registry=self.registry, vault=self.vault, scrubber=self.scrubber,
                             config=self.config, identity=lambda: dict(ID),
                             cancel_event=lambda: None, env_writer=None,
                             project_dir=lambda: "/")
        out = json.loads(asyncio.run(broken.secret_exec("sr_whatever", "true", cwd="/")))
        self.assertNotIn(DUMMY, json.dumps(out))
        boom = SecretTools(registry=self.registry, vault=self.vault, scrubber=self.scrubber,
                           config=self.config, identity=lambda: dict(ID),
                           cancel_event=lambda: None,
                           env_writer=lambda k, v: (_ for _ in ()).throw(ValueError(DUMMY)),
                           project_dir=lambda: "")
        out2 = json.loads(asyncio.run(boom.secret_export(self.vault.issue(DUMMY), "GITHUB_TOKEN")))
        self.assertEqual(out2["status"], "error")
        self.assertNotIn(DUMMY, json.dumps(out2))

    def test_control_the_grep_can_actually_see_the_value(self):
        """阳性对照：捕获通道不是空读。"""
        self._full_flow()
        self.handler.flush()
        self.assertIn("GitHub token", self.log_capture.getvalue())   # 日志确实在被捕获
        rec, res, _a, _b, _c = self._full_flow()
        self.assertEqual(res.status_code, 200)
        leaked = json.dumps({"oops": DUMMY})
        self.assertIn(DUMMY, leaked)                                 # grep 本身有效


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd /home/leo/develop/secret_input && /home/leo/miniforge3/bin/python3.12 -m unittest tests.test_invariants -v 2>&1 | tail -6`
Expected: **七路里任何一条红，都是 Task 1–8 的真缺陷**，修对应模块、不许改测试。本任务是把已实现的门合成为一条 I1 总闸，不是 red-first。

若七路当场全绿（预期如此），必须做一次变异对照证明门是活的：摘掉 Task 7 里 `request_secret` 的 `self._scrubber.register(value)` 再跑 `tests.test_invariants`，`test_2` 与 `test_5`/`test_6` 应转红；恢复后转绿。

> 路径⑥的调用形状以 Task 5 交付版为准，抄它别另起炉灶：`tests/test_middleware.py::test_disk_artifact_is_scrubbed` 现在喂的是**块列表**——`pruner.prune_output(scrubbed.content or [], max_bytes=64, metadata=scrubbed.metadata)`，与宿主调用点逐字同形（`qwenpaw/agents/middlewares.py:800-803`；list 分支在 `agents/tools/utils.py:219-239`，会经 `_set_block_text` 就地改写块），并断言返回块的文本 `joined_text(blocks)` 不含明文、块确实被就地截短。上面 Step 2 代码块里的 `prune_output(text, ...)`（str 形态）走的是同一终点（`prune_text` → `save_text_output`）但分支不同，写 `test_6` 时按块列表那一形落地。

```bash
cd /home/leo/develop/secret_input && cp backend/tools.py /tmp/t9.bak
python3 - <<'PY'
from pathlib import Path
q = Path("backend/tools.py")
s = q.read_text()
old = (
    "                value = self._registry.peek_value(request.ref) if request.ref else None\n"
    "                if value is not None:\n"
    "                    self._scrubber.register(value)"
)
assert old in s, "scrub registration not found verbatim"
q.write_text(s.replace(old, "                pass  # NEGATIVE CONTROL", 1))
PY
/home/leo/miniforge3/bin/python3.12 -m unittest tests.test_invariants 2>&1 | tail -4
cp /tmp/t9.bak backend/tools.py && /home/leo/miniforge3/bin/python3.12 -m unittest tests.test_invariants 2>&1 | tail -2
```

- [ ] **Step 4: 重写 `backend/main.py` 的 `register`（装配全部扩展点）**

```python
# -*- coding: utf-8 -*-
"""secret-input plugin entry point."""

from __future__ import annotations

import logging

from .config import PluginConfig
from .identity import cancel_event, identity, project_dir
from .middleware import make_scrub_middleware_factory
from .prompt import PROMPT_SECTION
from .records import RequestRegistry
from .router import build_router
from .scrub import Scrubber
from .tools import SecretTools
from .vault import Vault

logger = logging.getLogger(__name__)

TOOL_DESCRIPTIONS = {
    "request_secret": (
        "Ask the user for a secret (token, API key, signing secret, password) through a "
        "private card, and wait. The value is kept by the plugin and NEVER returned to you "
        "or shown in the transcript. On save you get a one-time `ref`. Never ask for a "
        "secret in chat, never invent one, never repeat one. Reuse client_request_id when "
        "retrying a call that lost its result; use a new one after timed_out."
    ),
    "secret_exec": (
        "Run a shell command with the secret the user just provided. Write {{secret}} where "
        "the value belongs; the plugin substitutes it in-process, the value never passes "
        "through you. It is also exported to $<env_name> (default SECRET). stdout/stderr are "
        "scrubbed: any occurrence of the value renders as ***. The ref works once."
    ),
    "secret_export": (
        "Persist the secret behind a ref into QwenPaw's encrypted env store under env_key "
        "(^[A-Z_][A-Z0-9_]{0,63}$). Later shell tools use $ENV_KEY without you ever seeing "
        "the value. The ref is consumed only if the write succeeds."
    ),
}


class SecretInputPlugin:
    def __init__(self) -> None:
        self.config = PluginConfig.load({}, None)

    def register(self, api) -> None:  # noqa: ANN001
        self.api = api
        self.config = PluginConfig.load(getattr(api, "config", None) or {},
                                        getattr(getattr(api, "manifest", None), "meta", None))
        self.vault = Vault(ttl_seconds=self.config.ref_ttl_seconds,
                           max_entries=self.config.scrub_max_entries * 4)
        self.registry = RequestRegistry(self.vault,
                                        timeout_seconds=self.config.answer_timeout_seconds,

> **R44（Task 7 评审 I-2 的裁定，Task 9 必须做）**：现在 `request_secret` 的签名默认是字面量 600，所以 `answer_timeout_seconds` 对本工具是**死的**——把配置传给 registry 也修不好（显式实参永远覆盖 `_default_timeout`，`records.py:119-133`）。正确改法在 `backend/tools.py`：签名默认改成哨兵 `timeout_seconds: int = 0`，函数体首行解析 `timeout = int(timeout_seconds) or self._config.answer_timeout_seconds` 再交给 `create`，注册描述里写明「omit（或 0）= 用运维配置的默认值」。同时删掉 `# TODO(Task 9, R44)`，并把 Task 7 那条「显式实参永远赢过配置」的 precedence 测试改成断言新语义（显式值仍赢；省略时取配置）。**验收**：把 `answer_timeout_seconds` 设成 30，省略实参跑一次，断言 30 秒左右返回 `timed_out`（不是 600）。代价若判错：模型看到的 schema 默认值从 600 变成 0，需在 README 与描述里解释「0 是省略」而不是「立刻超时」。

                                        max_pending=self.config.max_pending)
        self.scrubber = Scrubber(ttl_seconds=self.config.scrub_ttl_seconds,
                                 max_entries=self.config.scrub_max_entries)
        self.tools = SecretTools(registry=self.registry, vault=self.vault,
                                 scrubber=self.scrubber, config=self.config,
                                 identity=identity, cancel_event=cancel_event,
                                 project_dir=project_dir,
                                 env_writer=_write_env_via_host_store)

> **R46 的另一半（Task 9 必做）**：`env_writer` 由 `main.py` 提供，那里才允许 `import qwenpaw`：
> ```python
> def _write_env_via_host_store(key: str, value: str) -> None:
>     from qwenpaw.envs.store import set_env_var  # 唯一合法落点：main.py（导入门的管辖范围不含 main.py）
>     set_env_var(key, value)
> ```
> 同时把 `tests/test_secret_store_gate.py` 的共享 `MIDDLEWARE_ALLOWLIST` 拆成 per-module（`middleware.py` 允许 `agentscope`，`tools.py` 什么都不许），并改掉那个已名不副实的常量名（复评 Residual B）。
        for hook in (
            lambda: api.register_tool("request_secret", self.tools.request_secret,
                                      description=TOOL_DESCRIPTIONS["request_secret"],
                                      icon="🔑", enabled=False, tool_type="internal"),
            lambda: api.register_tool("secret_exec", self.tools.secret_exec,
                                      description=TOOL_DESCRIPTIONS["secret_exec"],
                                      icon="🛡️", enabled=False, tool_type="shell",
                                      target_param="command"),
            lambda: api.register_tool("secret_export", self.tools.secret_export,
                                      description=TOOL_DESCRIPTIONS["secret_export"],
                                      icon="📦", enabled=False, tool_type="internal"),
            lambda: api.register_http_router(
                build_router(registry=self.registry, config=self.config, vault=self.vault),
                prefix="/secret-input", tags=["secret-input"]),
            lambda: api.register_middleware(
                make_scrub_middleware_factory(self.scrubber, self.config), priority=50),
            lambda: api.register_prompt_section(
                "secret_input", after="env_context", provider=lambda _agent: PROMPT_SECTION),
            lambda: api.register_startup_hook("secret_input_sweep", self._sweep, priority=100),
            lambda: api.register_shutdown_hook("secret_input_shutdown", self._shutdown,
                                               priority=100),
            lambda: api.register_uninstall_hook("secret_input_uninstall", self._uninstall,
                                                priority=100),
        ):
            try:
                hook()
            except Exception:  # noqa: BLE001 - 一处失败不能带崩整包加载
                logger.exception("secret-input: a registration failed; the plugin is degraded")

    def _sweep(self) -> None:
        self.registry.sweep()
        self.vault.sweep()
        self.scrubber.sweep()
        logger.info("secret-input: swept (pending=%d vault=%d scrub=%d)",
                    len(self.registry.list_pending()), len(self.vault), len(self.scrubber))

    async def _shutdown(self) -> None:
        for rec in self.registry.list_pending():
            await self.registry.cancel(rec.request_id, "shutdown")

    def _uninstall(self, **_kwargs) -> None:
        # Refs die with the process; nothing of the user's secret is persisted.
        self._sweep()

    def status(self) -> dict:
        return {"pending": [r.request_id for r in self.registry.list_pending()],
                "vault_size": len(self.vault), "scrub_entries": len(self.scrubber),
                "scrub_enabled": self.config.scrub_enabled}


plugin = SecretInputPlugin()
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd /home/leo/develop/secret_input && /home/leo/miniforge3/bin/python3.12 -m unittest discover -s tests -t . -v 2>&1 | tail -8`
Expected: 全 `OK`（含 invariants 10 条）

- [ ] **Step 6: 离线验证再跑一次（装配后必须仍 PASS）**

Run: `cd /home/leo/develop/secret_input && /home/leo/miniforge3/bin/python3.12 packaging/verify_qp_plugin.py .`
Expected: `PASS` 且 `scheduled startup hooks` 里出现 `register_tool_secret-input_request_secret@50`、`…_secret_exec@50`、`…_secret_export@50`（**这是工具已登记的唯一可观测证据**；裸 Loader 不执行 hooks，所以看不到工具表属正常）。

- [ ] **Step 7: Commit**

```bash
git add backend/prompt.py backend/main.py tests/test_invariants.py
git commit -m "feat: 装配九个扩展点 + I1 七路不变式门（含阳性对照）"
```

---

### Task 10: 会话内卡片 —— 免构建前端 bundle（overlay 轮询）

**Files:**
- Create: `ui/index.js`、`tests/test_ui_bundle.py`

**Interfaces:**
- Consumes: 后端 `GET /api/secret-input/pending`、`GET /card/{id}/meta`、`POST /card/{id}/answer`（Task 6）
- Produces: `window.QwenPaw.slot.fill("secret-input","overlay.global",Card)` 注册的卡片

**为什么不用 `chat.toolRender`**：文档只保证它拿到 `result`（`docs/plugins.en.md:764-780`），而 `request_secret` 在等人回答时**还没有 result**。卡片必须在阻塞期就能填，所以走常驻 overlay + 轮询（spec §8.3、§15 A1）。

- [ ] **Step 1: 写失败测试 `tests/test_ui_bundle.py`（源码级断言，不起浏览器）**

```python
import subprocess
import unittest
from pathlib import Path

BUNDLE = Path(__file__).resolve().parents[1] / "ui" / "index.js"
SRC = BUNDLE.read_text(encoding="utf-8")


class BundleTest(unittest.TestCase):
    def test_syntax_parses_under_node(self):
        proc = subprocess.run(["node", "--check", str(BUNDLE)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_uses_the_host_sdk_only(self):
        self.assertIn("window.QwenPaw", SRC)
        self.assertIn("overlay.global", SRC)
        self.assertIn("getCurrentSessionId", SRC)
        self.assertIn("host.fetch", SRC)          # 自动带 Bearer + X-Agent-Id

    def test_polls_pending_and_never_reads_a_value_field(self):
        self.assertIn("/pending", SRC)
        for forbidden in ("item.value", "rec.value", "d.value", "requests[0].value"):
            self.assertNotIn(forbidden, SRC)     # 响应对象上不能读出任何值字段（输入框的 e.target.value 不在此列）

    def test_label_and_reason_are_not_injected_as_html(self):
        low = SRC.lower()
        self.assertNotIn("innerhtml", low)
        self.assertGreaterEqual(SRC.count("textContent"), 3)

    def test_no_external_origin(self):
        self.assertNotIn("https://", SRC)
        self.assertNotIn("cdn", SRC.lower())

    def test_uses_password_input_and_clears_it(self):
        # 依据：宿主 SDK 只给 antd（docs/plugins.en.md:487），密码框即 antd.Input.Password
        self.assertIn("Input.Password", SRC)
        self.assertIn('setValue("")', SRC)        # 提交后立刻清掉内存里的输入

    def test_shows_the_who_answered_audit(self):
        self.assertIn("answered_peer", SRC)      # 抢答可发现（spec §10.2 第 6 条）

    def test_requires_the_plugin_id_guard_before_registering(self):
        self.assertIn('if (!window.QwenPaw', SRC)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/leo/develop/secret_input && /home/leo/miniforge3/bin/python3.12 -m unittest tests.test_ui_bundle -v 2>&1 | tail -4`
Expected: `test_syntax_parses_under_node` 之外的用例全红（`ui/index.js` 目前为空文件）。

- [ ] **Step 3: 写 `ui/index.js`**

```js
/**
 * secret-input —— 会话内私密输入卡片（免构建，React.createElement）
 * 常驻 overlay.global，2s 轮询本会话的待答请求；值只 POST 回插件，
 * 绝不写进聊天、绝不进模型上下文。与 qwenpaw-file-browser 同一套宿主范式。
 */
(function () {
  "use strict";

  if (!window.QwenPaw || !window.QwenPaw.host) {
    console.error("[secret-input] QwenPaw host SDK not ready");
    return;
  }

  var QP = window.QwenPaw;
  var React = QP.host.React;
  var antd = QP.host.antd;
  var h = React.createElement;

  var PLUGIN_ID = "secret-input";
  var API = "/api/secret-input";
  var POLL_MS = 2000;

  function sessionId() {
    try {
      if (typeof QP.host.getCurrentSessionId === "function") {
        return QP.host.getCurrentSessionId() || "";
      }
    } catch (e) { /* 宿主未就绪时静默，卡片下一轮再试 */ }
    return "";
  }

  async function getPending(sid) {
    var url = API + "/pending?session_id=" + encodeURIComponent(sid);
    var resp = await QP.host.fetch(url, { headers: { Accept: "application/json" } });
    if (!resp.ok) return [];
    var data = await resp.json();
    return (data && data.requests) || [];
  }

  async function postAnswer(item, body) {
    var resp = await QP.host.fetch(API + "/card/" + item.request_id + "/answer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    return { ok: resp.ok, status: resp.status, data: await resp.json().catch(function () { return {}; }) };
  }

  function Card() {
    var items = [];
    var version = QP.host.useCurrentSession ? 0 : 0;   // 见 Step 4：改用 state + effect
    return h(
      "div",
      { style: { display: "none" } },
      "placeholder",
    );
  }

  function SecretCard(props) {
    var item = props.item;
    var done = props.done;
    var input = React.useState("");
    var value = input[0];
    var setValue = input[1];
    var busy = React.useState(false);
    var isBusy = busy[0];
    var setBusy = busy[1];
    var err = React.useState("");
    var error = err[0];
    var setError = err[1];

    async function save() {
      if (!value) { setError("值为空"); return; }
      var meta = await QP.host.fetch(API + "/card/" + item.request_id + "/meta");
      if (!meta.ok) { setError("请求已结束"); return; }
      var d = await meta.json();
      setBusy(true);
      var res = await postAnswer(item, { nonce: d.nonce, value: value });
      setBusy(false);
      setValue("");
      if (res.ok) { done(res.data.status || "saved"); }
      else { setError("失败：" + (res.data.detail || res.status)); }
    }

    async function decline() {
      var meta = await QP.host.fetch(API + "/card/" + item.request_id + "/meta");
      if (!meta.ok) { setError("请求已结束"); return; }
      var d = await meta.json();
      var res = await postAnswer(item, { nonce: d.nonce, decline: true });
      if (res.ok) { done("declined"); }
    }

    return h(
      "div",
      {
        style: {
          position: "fixed", right: 24, bottom: 96, width: 360, zIndex: 1080,
          background: "#161b22", color: "#e6edf3", border: "1px solid #30363d",
          borderRadius: 10, padding: 16, boxShadow: "0 8px 24px rgba(0,0,0,.4)",
        },
      },
      h("div", { style: { fontSize: 15, fontWeight: 600 }, textContent: item.label }),
      h("div", { style: { fontSize: 12, color: "#9198a1", marginTop: 4 },
                 textContent: item.reason }),
      h("div", {
        style: { fontSize: 11, color: "#d29922", marginTop: 8 },
        textContent: "值不会进入对话，模型看不到它。",
      }),
      h(antd.Input.Password, {
        style: { marginTop: 10 },
        placeholder: item.placeholder || "粘贴密钥",
        value: value,
        onChange: function (e) { setValue(e.target.value); },
        onPressEnter: save,
        autoComplete: "new-password",
      }),
      error
        ? h("div", { style: { color: "#f85149", fontSize: 12, marginTop: 6 },
                     textContent: error })
        : null,
      h(
        "div",
        { style: { marginTop: 10 } },
        h(antd.Button, { type: "primary", size: "small", loading: isBusy, onClick: save,
                         children: "保存" }),
        h(antd.Button, { size: "small", style: { marginLeft: 8 }, onClick: decline,
                         children: "拒绝" }),
      ),
    );
  }

  function EndedCard(props) {
    var info = props.info || {};
    return h(
      "div",
      {
        style: {
          position: "fixed", right: 24, bottom: 96, width: 360, zIndex: 1080,
          background: "#161b22", border: "1px solid #238636", borderRadius: 10, padding: 12,
          color: "#3fb950", fontSize: 13,
        },
      },
      h("div", { textContent: props.status === "declined" ? "已拒绝" : "已保存" }),
      info.answered_peer
        ? h("div", {
            style: { color: "#9198a1", fontSize: 11, marginTop: 4 },
            textContent: "已由 " + info.answered_peer + " 于 " + (info.answered_at || "") + " 提交",
          })
        : null,
    );
  }

  function Overlay() {
    var state = React.useState([]);
    var pending = state[0];
    var setPending = state[1];
    var ended = React.useState(null);
    var endedState = ended[0];
    var setEnded = ended[1];

    React.useEffect(function () {
      var stopped = false;
      async function tick() {
        var sid = sessionId();
        if (!sid) return;
        try {
          var list = await getPending(sid);
          if (!stopped) setPending(list);
        } catch (e) { /* 轮询失败保持上一帧，下一轮再试 */ }
      }
      tick();
      var timer = setInterval(tick, POLL_MS);
      return function () { stopped = true; clearInterval(timer); };
    }, []);

    if (endedState) { return h(EndedCard, endedState); }
    if (!pending.length) { return null; }
    return h(SecretCard, {
      item: pending[0],
      done: function (status) {
        QP.host.fetch(API + "/card/" + pending[0].request_id + "/meta")
          .then(function (r) { return r.ok ? r.json() : {}; })
          .then(function (info) { setEnded({ status: status, info: info }); })
          .catch(function () { setEnded({ status: status, info: {} }); });
      },
    });
  }

  QP.slot.fill(PLUGIN_ID, "overlay.global", function () { return h(Overlay); });
  console.info("[secret-input] overlay card registered");
})();
```

Step 3 里那个没用的 `function Card()` 是草稿残留 —— **实现时删掉它**（连同 `version` 那行）。

- [ ] **Step 4: 删残留并跑测试**

```bash
cd /home/leo/develop/secret_input && /home/leo/miniforge3/bin/python3.12 - <<'PY'
from pathlib import Path
p = Path("ui/index.js"); t = p.read_text(encoding="utf-8")
start = t.index("  function Card() {")
end = t.index("  function SecretCard(props) {")
t = t[:start] + t[end:]
p.write_text(t)
PY
node --check ui/index.js && /home/leo/miniforge3/bin/python3.12 -m unittest tests.test_ui_bundle -v 2>&1 | tail -4
```
Expected: `node --check` 无输出（rc=0）+ `OK` / `8 tests`。

- [ ] **Step 5: Commit**

```bash
git add ui/index.js tests/test_ui_bundle.py
git commit -m "feat: overlay 私密卡片（阻塞期常驻轮询、值只回插件、显示答者审计）"
```

---

### Task 11: 发布包 —— zip 布局与 symlink 门

**Files:**
- Create: `packaging/build_zip.sh`、`tests/test_packaging.py`

**Interfaces:**
- Consumes: `plugin.json`、`backend/`、`ui/`
- Produces: `dist/secret-input-<version>.zip`，唯一顶层目录 `secret-input/`

- [ ] **Step 1: 写失败测试 `tests/test_packaging.py`**

```python
import json
import subprocess
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PackagingTest(unittest.TestCase):
    def setUp(self):
        proc = subprocess.run(["bash", "packaging/build_zip.sh"], cwd=ROOT,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        version = json.loads((ROOT / "plugin.json").read_text())["version"]
        self.zip_path = ROOT / "dist" / f"secret-input-{version}.zip"
        self.assertTrue(self.zip_path.is_file(), str(self.zip_path))

    def test_single_top_level_dir_named_after_the_plugin_id(self):
        names = zipfile.ZipFile(self.zip_path).namelist()
        tops = {n.split("/")[0] for n in names if n}
        self.assertEqual(tops, {"secret-input"})

    def test_plugin_json_is_found_by_both_install_paths(self):
        with zipfile.ZipFile(self.zip_path) as zf:
            self.assertIn("secret-input/plugin.json", zf.namelist())

    def test_ships_only_runtime_files(self):
        names = zipfile.ZipFile(self.zip_path).namelist()
        for forbidden in ("tests/", "packaging/", ".github/", "docs/", "__pycache__"):
            self.assertFalse(any(f in n for n in names), forbidden)
        self.assertTrue(any(n.endswith("backend/main.py") for n in names))
        self.assertTrue(any(n.endswith("ui/index.js") for n in names))

    def test_no_symlink_members(self):
        # zipfile 不重建 symlink；成员落成"内容为目标路径的普通文件"是静默坏
        with zipfile.ZipFile(self.zip_path) as zf:
            for info in zf.infolist():
                mode = (info.external_attr >> 16) & 0o170000
                self.assertNotEqual(mode, 0o120000, info.filename)

    def test_no_pyc_or_stale_build(self):
        for n in zipfile.ZipFile(self.zip_path).namelist():
            self.assertFalse(n.endswith((".pyc", ".pyo")), n)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /home/leo/develop/secret_input && /home/leo/miniforge3/bin/python3.12 -m unittest tests.test_packaging -v 2>&1 | tail -3`
Expected: `bash: packaging/build_zip.sh: No such file`

- [ ] **Step 3: 写 `packaging/build_zip.sh`**

```bash
#!/usr/bin/env bash
# 发布包：唯一顶层目录 secret-input/，只带运行文件（spec section 6 / 13.5）。
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stage="${root}/.build/secret-input"
version="$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "${root}/plugin.json" | head -1)"
[ -n "${version}" ] || { echo "FATAL: cannot read version from plugin.json" >&2; exit 1; }

# symlink 非空即拒绝构建：zip 解压不重建链接，成员会静默变成"内容为目标路径的普通文件"
if [ -n "$(find "${root}/backend" "${root}/ui" -type l -print -quit)" ]; then
  echo "FATAL: symlink found in runtime tree; refuse to build" >&2
  find "${root}/backend" "${root}/ui" -type l >&2
  exit 1
fi

rm -rf "${root}/.build"
mkdir -p "${stage}/backend" "${stage}/ui"
cp "${root}/plugin.json" "${stage}/plugin.json"
cp "${root}/backend/"*.py "${stage}/backend/"
cp "${root}/ui/index.js" "${stage}/ui/index.js"
find "${stage}" -name '__pycache__' -type d -prune -exec rm -rf {} +

mkdir -p "${root}/dist"
out="${root}/dist/secret-input-${version}.zip"
rm -f "${out}"
( cd "${root}/.build" && zip -qr "${out}" secret-input -x '*/__pycache__/*' )
rm -rf "${root}/.build"

echo "built ${out}"
unzip -l "${out}" | tail -3
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /home/leo/develop/secret_input && chmod +x packaging/build_zip.sh && /home/leo/miniforge3/bin/python3.12 -m unittest tests.test_packaging -v 2>&1 | tail -5 && git status --short | head -5`
Expected: `OK` / `5 tests`；`dist/` 已被 `.gitignore` 忽略（若没有，追加 `dist/` 与 `.build/` 后重跑）。

- [ ] **Step 5: 红对照 —— symlink 门会拦**

```bash
cd /home/leo/develop/secret_input && mkdir -p backend && ln -s /etc/passwd backend/evil.py
bash packaging/build_zip.sh; echo "rc=$?"; rm backend/evil.py
bash packaging/build_zip.sh >/dev/null && echo "removed -> builds"
```
Expected: 带 symlink 时 `FATAL: symlink found in runtime tree; refuse to build` + `rc=1`；删掉后正常。

- [ ] **Step 6: Commit**

```bash
git add packaging/build_zip.sh tests/test_packaging.py .gitignore
git commit -m "build: 发布包单顶层目录 + symlink 拒建门"
```

---

### Task 12: 真机热装与验收（spec §14 的 8 条 + A4/A6/A7）

**Files:**
- Create: `docs/verification/2026-10-19-acceptance.md`（每条命令与逐字读数都贴进去，红的也贴）
- Modify: `plugin.json` 仅 Step 8 临时改 `answer_timeout_seconds`，做完必须还原并 `git diff` 确认干净

**前置事实**：宿主在跑（`pgrep -af "qwenpaw app"` → pid，端口 19999）。**Step 5 起需要一次人工配合**（打开三个工具、点 Stop），不要试图用 curl 伪造。

- [ ] **Step 1: 离线门全绿（热装前）**

Run: `cd /home/leo/develop/secret_input && /home/leo/miniforge3/bin/python3.12 -m unittest discover -s tests -t . 2>&1 | tail -3 && /home/leo/miniforge3/bin/python3.12 -m compileall -q backend && /home/leo/miniforge3/bin/python3.12 packaging/verify_qp_plugin.py .`
Expected: `OK`、compileall 无输出、`PASS`。

- [ ] **Step 2: 热装（app 在跑 ⇒ 目录走 API，无需重启）**

Run:
```bash
curl -s --noproxy '*' -X POST http://127.0.0.1:19999/api/plugins/install \
  -H 'Content-Type: application/json' \
  -d '{"source":"/home/leo/develop/secret_input","force":true}' | python3 -m json.tool | head -30
```
Expected: HTTP 层 `loaded`/`reload` 字段为真，日志出现 `✓ Loaded plugin 'secret-input' successfully`。若报 `plugin.json not found`：说明它走了离线分支（app 没在跑），先起宿主。

- [ ] **Step 3: 回读装配状态（不接受"没报错"当结论）**

Run:
```bash
curl -s --noproxy '*' http://127.0.0.1:19999/api/plugins | python3 -c "
import json,sys
d=json.load(sys.stdin)
items = d if isinstance(d,list) else (d.get('plugins') or d.get('items') or [])
p=[x for x in items if (x.get('id') or x.get('plugin_id'))=='secret-input']
assert p, 'NOT LISTED'
print(json.dumps(p[0], ensure_ascii=False))
"
curl -s --noproxy '*' http://127.0.0.1:19999/api/plugins/secret-input/status | head -c 300; echo
curl -s --noproxy '*' http://127.0.0.1:19999/frontend_plugin | python3 -c "
import json,sys
rows=[x for x in json.load(sys.stdin) if x.get('id')=='secret-input']
assert rows and rows[0].get('frontend_entry'), rows
print('frontend_entry:', rows[0]['frontend_entry'], 'enabled:', rows[0]['enabled'])
"
curl -s --noproxy '*' http://127.0.0.1:19999/api/tools | python3 -c "
import json,sys
d=json.load(sys.stdin); items=d if isinstance(d,list) else (d.get('tools') or d.get('items') or [])
want={'request_secret','secret_exec','secret_export'}
got={(t.get('name') or t.get('tool_name') or '') for t in items}
print('missing:', want-got); print('present:', sorted(want & got))
"
ls ~/.qwenpaw/plugins/ | grep -c '^secret-input$'
diff -rq -x __pycache__ /home/leo/develop/secret_input/backend ~/.qwenpaw/plugins/secret-input/backend
```
Expected: `enabled` 与 `loaded` 都为 true；`frontend_entry: ui/index.js`；`missing: []`；plugins 目录下**恰好一份**；`diff` 无输出。

- [ ] **Step 4: 服务端四端点（含鉴权门的真实读数）**

Run:
```bash
A=http://127.0.0.1:19999/api/secret-input
curl -s --noproxy '*' -o /dev/null -w "pending(loopback)=%{http_code}\n" "$A/pending"
curl -s --noproxy '*' "$A/pending" | head -c 200; echo
curl -s --noproxy '*' -o /dev/null -w "card(bad-id)=%{http_code}\n" "$A/card/notahexid"
curl -s --noproxy '*' -o /dev/null -w "meta(unknown)=%{http_code}\n" "$A/card/$(printf 'f%.0s' {1..32})/meta"
curl -s --noproxy '*' -X POST "$A/card/$(printf 'f%.0s' {1..32})/answer" \
  -H 'Content-Type: application/json' -d '{"nonce":"x","value":"probe"}' \
  -o /dev/null -w "answer(unknown)=%{http_code}\n"
LAN=$(ip -4 -o addr show | awk '$2!="lo" && $2!="docker0"{split($4,a,"/");print a[1];exit}')
echo "non-loopback probe from $LAN:"
curl -s --noproxy '*' -o /dev/null -w "remote=%{http_code}\n" --connect-timeout 3 \
  "http://$LAN:19999/api/secret-input/pending" || echo "remote unreachable (记录为不可达)"
```
Expected: `pending(loopback)=200`、`meta(unknown)=404`、`answer(unknown)=404`、`card(bad-id)` 返回 200 且页面是"链接不可用"文案；非 loopback 若可达必须 **403**（`allow_remote` 默认 false）。把这五行读数原样写进验收记录。

- [ ] **Step 5: 端到端 —— 真 agent 阻塞等人回答（需人工一次）**

先决条件（人工）：在 Console 的目标 agent 设置里把 `request_secret`、`secret_exec`、`secret_export` 三个工具打开（首装默认 disabled，这是宿主的既定行为）。

1. 在 Console 该会话里发：`用 request_secret 向我要一个测试 token（label=probe token, reason=验收），拿到 ref 后用 secret_exec 跑 printf "%s" {{secret}}，把结果告诉我。`
2. 卡片应在**阻塞期**出现（overlay 右下）。**不要**在卡片里填，先做 Step 6 的抢答实验，再回来填 `DUMMY-SK-abcdef123456`。
3. 期望：`secret_exec` 返回 `stdout` 为 `***`、`status: ok`；模型复述不出明文。

- [ ] **Step 6: 抢答可见性（spec §10.2 第 6 条）**

在第 5 步的卡片出现后、人类填之前：
```bash
ID=$(curl -s --noproxy '*' http://127.0.0.1:19999/api/secret-input/pending \
    | python3 -c "import json,sys;print(json.load(sys.stdin)['requests'][0]['request_id'])")
N=$(curl -s --noproxy '*' "http://127.0.0.1:19999/api/secret-input/card/$ID/meta" \
    | python3 -c "import json,sys;print(json.load(sys.stdin)['nonce'])")
curl -s --noproxy '*' -X POST "http://127.0.0.1:19999/api/secret-input/card/$ID/answer" \
  -H 'Content-Type: application/json' -d "{\"nonce\":\"$N\",\"value\":\"BOGUS-NOT-A-SECRET\"}"
curl -s --noproxy '*' "http://127.0.0.1:19999/api/secret-input/card/$ID/meta" | head -c 300; echo
```
Expected: 第一条 200 `saved`；meta 回 `status: saved` + `answered_peer: 127.0.0.1` + `answered_at`；**卡片侧显示"已由 127.0.0.1 于 <时刻> 提交"**（人因此能发现被抢答）。第二次 answer 必须 410。

- [ ] **Step 7: 明文零命中（三条 grep + 一条阳性对照）**

```bash
cd /home/leo/develop/secret_input
D=DUMMY-SK-abcdef123456
echo "--- memory (API 回读) ---"
curl -s --noproxy '*' "http://127.0.0.1:19999/api/console/history?session_id=<SESSION>" \
  | grep -c "$D" || true      # 期望 0；route 名以 GET /openapi.json 里 console 下的消息查询路径为准
echo "--- logs ---"; grep -rc "$D" ~/.qwenpaw/logs/qwenpaw.log* | grep -v ':0$' || echo "0 命中"
echo "--- tool_results ---"
find ~/.qwenpaw/workspaces -path '*/tool_results/*' -name '*.txt' -print0 \
  | xargs -0 -r grep -l "$D" || echo "0 命中"
echo "--- 阳性对照（grep 通道本身有效）---"
grep -c "secret-input" ~/.qwenpaw/logs/qwenpaw.log || true   # 插件自己的日志行（swept/注册失败等），必须能命中
```
Expected: 前三处 0 命中，第四条命中 >0。**若第四条也是 0，说明捕获通道根本没读到东西**（找错目录/日志级别），本次判据作废、修 grep 再重跑，不许写"零命中所以安全"。**标签/理由自 Task 9 起不再进任何插件日志**（短密钥放在 label 里时 120 字符截断挡不住，R51a），因此阳性对照不能再拿 label 当哨兵。
注：`<SESSION>` 用第 5 步那个会话的真实 id；history 路由名先从 `curl -s http://127.0.0.1:19999/openapi.json` 里查，**不要照抄本行**。

- [ ] **Step 8: A4（长阻塞不被宿主掐）**

前提：Task 9 的 R44 哨兵已落地（否则本工具永远等 600 秒，这一步测的是宿主而不是配置）。把 `answer_timeout_seconds` 设为 300（`plugin.json` 的 `meta.config`，或起宿主前 `QWENPAW_SECRET_INPUT_ANSWER_TIMEOUT_SECONDS=300`），发起请求时**省略** `timeout_seconds`，不回答，等 5 分钟。
Expected: 工具返回 `timed_out` 且 `hint` 提到 new client_request_id；**不是**被宿主 kill、也没有 `Tool timeout` 字样。

- [ ] **Step 9: A6（阻塞期点 Stop，取消必须传到）**

在卡片挂着时于 Console 点 Stop。
Expected: 工具返回 `{"status":"cancelled","reason":"run_ended"}`；`/pending` 不再列该条；日志一行 `-> cancelled`。若卡片此后仍显示该请求 → 记为**红**，修法是在 `cancel()` 的 `_finish` 里已删除 `_pending`（应已生效），排查方向是前端轮询缓存而非后端。

- [ ] **Step 10: A7 + 答页独立可用**

```bash
python3 - <<'PY'
from pathlib import Path
print("ok" if Path("/home/leo/miniforge3/lib/python3.12/site-packages/qwenpaw/console/index.html").is_file() else "check")
PY
```
用浏览器（或 `cbc` 之外的人工）打开 Step 4 记录的 answer_url，填一次并断言页面转为"已保存"。同时 `curl -sI` 该 URL 记录 `content-type` 与安全头，确认响应未被 `RuntimeBoundaryMiddleware`/压缩改写。

- [ ] **Step 11: 卸载钩子与清理**

```bash
printf 'y\n' | /home/leo/miniforge3/bin/python3.12 -m qwenpaw plugin uninstall secret-input 2>&1 | tail -3
ls ~/.qwenpaw/plugins/ | grep -c '^secret-input$' || echo "已移除"
```
Expected: 卸载成功且目录消失；`plugins/` 下不留 `.prev-` 备份（留了就搬去 `~/.qwenpaw/plugin-archive/`，同 id 多份会重复注册）。**这一步做完，Task 12 才算完；验收记录里要含 Step 3 的重跑（重装回去）**。

- [ ] **Step 12: 重装回运行时（交付=装上，不是停在 commit）**

Run: 重做 Step 2 + Step 3，并把两轮读数一起写进 `docs/verification/2026-10-19-acceptance.md`。
```bash
git add docs/verification && git commit -m "test: 真机热装与 §14 验收记录（含抢答可见性与零命中 grep 的阳性对照）"
```

---

### Task 13: 文档与仓库标准

**Files:**
- Create: `README.md`、`README.zh-CN.md`、`SECURITY.md`、`CONTRIBUTING.md`、`.github/workflows/ci.yml`、`pyproject.toml`、`.github/ISSUE_TEMPLATE.md`、`.github/PULL_REQUEST_TEMPLATE.md`、`CODE_OF_CONDUCT.md`

- [ ] **Step 1: `pyproject.toml`（ruff 显式 select，BLE 必须豁免）**

```toml
[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
# 显式 select。BLE001 属于 BLE 规则集，这里没选 BLE ⇒ ignore 那项是防御性冗余；
# 代码里的 `# noqa: BLE001` 只在有人把 BLE 加进 select 时才生效。不选 BLE 是刻意的：
# wrapper 绝不向宿主抛异常、单点注册失败不带崩整包，都是设计而非疏忽。
select = ["E", "F", "W", "I", "UP", "B", "ANN"]
ignore = ["BLE001", "ANN401"]

[tool.ruff.lint.per-file-ignores]
"tests/*" = ["ANN", "B011"]
```

- [ ] **Step 2: `.github/workflows/ci.yml`（三步门禁）**

```yaml
name: ci
on: {push: {branches: [main]}, pull_request: {branches: [main]}}
jobs:
  gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.12"}
      - run: pip install ruff
      - run: ruff check backend tests packaging
      - run: python -m compileall -q backend
      - run: node --check ui/index.js
        # 离线验证需要真 qwenpaw，CI 里不做：它由本机宿主解释器跑（见 README 的开发章节）
```
CI 只跑纯静态三步（ruff/compileall/node --check）。`unittest` 里 `test_middleware`、`test_invariants`、`test_packaging` 依赖宿主包，**CI 必然红** ⇒ 要么在 CI 里 `pip install qwenpaw` 装真宿主（会拖进一堆依赖），要么按上面注释声明"完整测试以本机宿主解释器为准"并在 README 写清这条边界。**不许为了让 CI 绿而删断言。**

- [ ] **Step 3: `README.md` / `README.zh-CN.md`**

两份是完整对等文档、顶部互挂语言链接（`[中文版](README.zh-CN.md)` / `English version`）。必需章节与**必须逐字写进去**的句子：

1. What it does — `The value never enters the model's context; the model only ever receives a one-time ref.`
2. Install —— 三种情形分开写：
   - app 在跑：`qwenpaw plugin install <目录|zip|URL>` 或 `POST /api/plugins/install {"source":…,"force":true}`（热装即跑 startup hook，无需重启）
   - app 停着：**只接受目录或 zip 的 URL**，本地 zip 会报 `plugin.json not found`
   - 从 release：`…/releases/download/v0.1.0/secret-input-0.1.0.zip`
3. Enable the tools — `All three tools register disabled (enabled=False). Turn them on in the agent's tool settings before use.`
4. Usage — 三个工具的调用顺序与返回样例（含 `timed_out` / `ref_unavailable` / `ref_expired` 的重问文案）；`secret_exec` vs `secret_export` 的取舍一句话。
5. Where the answer can be given — overlay 卡片 / `/api/secret-input/card/<id>` 答页 / `curl -s http://127.0.0.1:19999/api/secret-input/pending | jq -r '.[].answer_url'` 三条路径。
6. Threat model —— 把 spec §10.2 六条**原样搬过来**，其中第 6 条标题就叫 `Your local agent can answer the card before you do`，并说明卡片显示答者 peer 与时刻；擦洗一句 `best-effort, not a sandbox`；`secret_export` 一句 `the value stays in the host env store and process environment`。
7. Development — 测试命令（宿主解释器 + `unittest discover -s tests -t .`）、`packaging/verify_qp_plugin.py`、负对照约定（每道门都要能红）。
8. Config —— §11 的 7 个键与默认值表，标注 `api.config is {} on hot install; every key has a default`。

- [ ] **Step 4: `SECURITY.md`（含"如何报告"与"本插件不防的"摘要）** 与 `CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`、两个模板：按 repo-standards-alignment 的治理六件套补齐，内容与双语 README 顶部链接一致。

- [ ] **Step 5: 文档自查**

Run:
```bash
cd /home/leo/develop/secret_input
grep -n "README.zh-CN.md" README.md && grep -n "README.md" README.zh-CN.md
grep -rn "enabled=False\|默认关闭\|disabled" README.md README.zh-CN.md | head -4
grep -c "not a sandbox\|不是沙箱" README.md README.zh-CN.md
grep -rn "TODO\|TBD\|待补" README.md README.zh-CN.md SECURITY.md || echo "no placeholders"
/home/leo/miniforge3/bin/python3.12 -m ruff check backend tests packaging 2>&1 | tail -3
```
Expected: 双向链接各命中；disabled 说明在两文都有；沙箱声明两文各 ≥1；无占位；ruff `All checks passed!`（若报 ANN/B 类，按实际语义修代码或明确豁免，不许盲加 `# noqa`）。

- [ ] **Step 6: Commit**

```bash
git add README.md README.zh-CN.md SECURITY.md CONTRIBUTING.md CODE_OF_CONDUCT.md .github pyproject.toml
git commit -m "docs: 双语 README + 治理六件套 + ruff/CI 门禁（威胁模型逐条对齐 spec §10.2）"
```

---

## 覆盖对照（spec → 任务）

| spec | 落在哪个任务 |
| --- | --- |
| §3 I1 | 9（`test_invariants` 七路）+ 12 Step 7 |
| §3 I2 | 2（ref 不可逆）+ 6（无读值端点）+ 12 Step 4 |
| §3 I3 | 2（consume/TTL）+ 8（`ref_unavailable`/`ref_expired` 文案）+ 7 Step 6 重放 |
| §3 I4 | 4 + 5（含磁盘工件门与阳性对照） |
| §6 结构 / manifest | 1 |
| §6.1 擦洗实现点 | 5 |
| §7.1 `request_secret` | 7 |
| §7.2 `secret_exec`（32KB 上限） | 8 |
| §7.3 `secret_export`（失败不消费） | 8 |
| §8.1/8.2 端点与鉴权 | 6 |
| §8.2 答者审计 | 6（字段）+ 10（显示）+ 12 Step 6（真机可见） |
| §8.3 前端两层 | 6（HTML 答页）+ 10（overlay） |
| §8.4 提示词段 | 9 |
| §9 生命周期/GC/上限 | 3 |
| §10 威胁模型 | 6 + 9 + 12 + 13（README 原样搬） |
| §11 配置 | 1 |
| §12 测试 | 1–11 各任务的测试文件 |
| §13 发布 | 11 + 12 + 13 |
| §14 验收 | 12 |
| §15 A1–A7 | A1→12 Step 3/5、A2→5 Step 5、A3→12 Step 10、A4→12 Step 8、A5→9 Step 6+12 Step 3、A6→12 Step 9、A7→12 Step 10 |
| §17 评审新增项 | 磁盘工件→5/9；抢答可见性→6/10/12；SKILL.md N/A→1 Step 7；ref_expired→8 |

