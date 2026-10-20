# -*- coding: utf-8 -*-
"""Source-level gates for the in-session overlay bundle (no browser, no build).

The bundle is served as-is to the host Console, so its safety properties are
checked by reading it: host SDK only, no HTML injection, no external origin,
no value ever read back from a response.
"""

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

    def test_error_text_is_fixed_and_never_echoes_the_server(self):
        # 失败串必须是本地闭集，绝不把响应里的任意文本拼进卡片。
        self.assertNotIn(".detail", SRC)
        self.assertIn("setError(", SRC)

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
        self.assertIn("answered_at", SRC)

    def test_done_callback_uses_its_own_request_id(self):
        # 终态回读 meta 时必须用卡片自己的 id；回读 pending[0] 会串到下一张卡。
        self.assertNotIn("pending[0].request_id", SRC)
        self.assertIn("var requestId = current.request_id;", SRC)

    def test_requires_the_plugin_id_guard_before_registering(self):
        self.assertIn('if (!window.QwenPaw', SRC)


if __name__ == "__main__":
    unittest.main()
