"""Plugin configuration with a four-tier precedence chain."""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from typing import Any

_ENV_PREFIX = "QWENPAW_SECRET_INPUT_"
_BOOL_TRUE = {"true", "1", "yes", "on"}
_BOOL_FALSE = {"false", "0", "no", "off"}


def _coerce(raw: Any, default: Any) -> Any:
    if isinstance(default, bool):
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            token = raw.strip().lower()
            if token in _BOOL_TRUE:
                return True
            if token in _BOOL_FALSE:
                return False
            # 认不出的写法（"ture"/"maybe"/空串）回落默认，和 int 分支同一条契约。
            # 对默认开的开关尤其重要：旧实现把未知串一律当 False，一个拼写错误就能
            # 悄悄关掉确认卡（fail open），而这里必须 fail closed。
            return default
        return default
    if isinstance(default, int):
        if isinstance(raw, bool):
            # bool 不是"能转成 int 的坏值"：True 掉进 int() 会变成 1，把误写成 true 的
            # TTL 静默变成 1 秒。契约是坏值回落默认，所以这里也回落。
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
    # 消费 ref 之前是否把确切动作交给人类确认（确认卡）。宿主自己的 approval 不覆盖
    # 插件工具（2026-10-21 实测 `/api/approval/list` 全程为空），所以这道门只能由插件
    # 自己提供。默认开；关掉它等于回到"模型写什么命令就跑什么"的直接路径。
    # 它**不是**沙箱：同一个 loopback 调用者也能答这张卡（spec §10.2 第 6/9 条），
    # 默认开的价值是让外发动作在发生前可见、可被人拒掉。
    require_consume_approval: bool = True

    @classmethod
    def load(cls, api_config: dict | None = None, meta: dict | None = None) -> PluginConfig:
        """meta["config"] > api_config > env > default. Never raises."""
        defaults = cls()
        values: dict[str, Any] = {}
        # "Never raises" 覆盖入参本身：任一层不是 dict（宿主给了 list/str/数字），
        # 就把那一层当作不存在，而不是让 `key in layer` / `layer[key]` 抛出去。
        meta_config = meta.get("config") if isinstance(meta, dict) else None
        layers = [layer for layer in (api_config, meta_config) if isinstance(layer, dict)]
        for field in fields(cls):
            key = field.name
            default = getattr(defaults, key)
            env_raw = os.environ.get(_ENV_PREFIX + key.upper())
            picked = env_raw if env_raw is not None else default
            for layer in layers:
                if key in layer:
                    picked = layer[key]
            values[key] = _coerce(picked, default)
        return cls(**values)
