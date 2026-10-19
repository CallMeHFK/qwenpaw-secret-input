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

    def test_bool_is_not_silently_used_as_an_int_value(self):
        # 配置里把某个 TTL 误写成 true ⇒ 回落默认，而不是当成 1 秒（int(True)）
        cfg = PluginConfig.load({"ref_ttl_seconds": True, "answer_timeout_seconds": True}, None)
        self.assertEqual(cfg.ref_ttl_seconds, 300)
        self.assertEqual(cfg.answer_timeout_seconds, 600)
        # False 同理：0 秒 TTL 比"关掉"更危险，必须回落
        cfg = PluginConfig.load({"scrub_ttl_seconds": False, "max_pending": True}, None)
        self.assertEqual(cfg.scrub_ttl_seconds, 3600)
        self.assertEqual(cfg.max_pending, 32)
        # 但 bool 字段本身照旧接受 true/false
        self.assertFalse(PluginConfig.load({"allow_remote": False}, None).allow_remote)

    def test_non_dict_config_layers_fall_back_instead_of_raising(self):
        # 契约是"never raises"：宿主/手写 manifest 给出非 dict 的某一层，该层视作不存在
        for bad in ([], (), "ref_ttl_seconds", 42, True, {"config": 5}.keys()):
            self.assertEqual(PluginConfig.load(bad, None).ref_ttl_seconds, 300)          # api_config 坏
            self.assertEqual(PluginConfig.load(None, bad).ref_ttl_seconds, 300)          # meta 坏
            self.assertEqual(PluginConfig.load({"ref_ttl_seconds": 222}, bad).ref_ttl_seconds, 222)
            self.assertEqual(PluginConfig.load(None, {"config": bad}).ref_ttl_seconds, 300)  # meta["config"] 坏


if __name__ == "__main__":
    unittest.main()
