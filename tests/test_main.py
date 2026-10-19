# -*- coding: utf-8 -*-
"""Startup-hook registration: the deliberate swallow must stay observable."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.main import SecretInputPlugin  # noqa: E402

# 本任务的唯一交付物就是这一行登记（名字按宿主原样存 hook_name，无前缀）
EXPECTED_CALL = ("secret_input_boot", 100)


class RecordingApi:
    """Stub PluginApi：按宿主 register_startup_hook(plugin_id 除外) 的形状记录调用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def register_startup_hook(self, hook_name, callback, priority=100):  # noqa: ANN001
        self.calls.append((hook_name, priority))


class ExplodingApi(RecordingApi):
    def register_startup_hook(self, hook_name, callback, priority=100):  # noqa: ANN001
        raise RuntimeError("host signature drifted")


class RegistrationTest(unittest.TestCase):
    def test_register_schedules_the_startup_hook_without_swallowing_anything(self):
        api = RecordingApi()
        plugin = SecretInputPlugin()
        # assertNoLogs 是这条门的正半：登记成功时不许有 ERROR 被吞掉
        with self.assertNoLogs("backend.main", level="ERROR"):
            plugin.register(api)
        self.assertEqual(api.calls, [EXPECTED_CALL])
        self.assertIs(plugin.api, api)

    def test_registration_failure_is_swallowed_but_logged_as_error(self):
        # 刻意保留 except：一处登记失败不能带崩整包加载；但它必须留下 ERROR + traceback
        api = ExplodingApi()
        with self.assertLogs("backend.main", level="ERROR") as logged:
            SecretInputPlugin().register(api)  # 不抛
        self.assertEqual(api.calls, [])
        output = "\n".join(logged.output)
        self.assertIn("startup hook registration failed", output)
        self.assertIn("RuntimeError: host signature drifted", output)

    def test_missing_host_attribute_is_swallowed_but_logged_as_error(self):
        # 最可能的真机漂移：宿主根本不再有 register_startup_hook
        with self.assertLogs("backend.main", level="ERROR") as logged:
            SecretInputPlugin().register(object())  # 不抛
        self.assertIn("AttributeError", "\n".join(logged.output))


if __name__ == "__main__":
    unittest.main()
