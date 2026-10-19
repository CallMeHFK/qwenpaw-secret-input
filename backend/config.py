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
            for layer in (api_config or {}, (meta or {}).get("config") or {}):
                if key in layer:
                    picked = layer[key]
            values[key] = _coerce(picked, default)
        return cls(**values)
