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

    @classmethod
    def load(cls, api_config: dict | None = None, meta: dict | None = None) -> "PluginConfig":
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
