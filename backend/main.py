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
