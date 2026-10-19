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
