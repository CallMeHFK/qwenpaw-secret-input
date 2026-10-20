import time
import unittest

from backend.vault import Vault
from tests.support import DUMMY  # noqa: F401  (路径已在 support 里插好)


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

    def test_len_is_live_only_not_raw_dict_size(self):
        # 裁定 R40（对齐 R24 在 scrub 那侧的修法）：Task 9 把 len() 当 `vault_size` 报给
        # 观察面，它必须等于"现在还兑得出值的条数"。TTL 到点又没人来消费的条目在清扫之前
        # 一直躺在 `_issued` 里，读原始 dict 就是虚报"多一条可用句柄"。
        v = Vault(ttl_seconds=1)
        live = v.issue(DUMMY)
        stale = v.issue("stale-value")
        v._issued[stale] = ("stale-value", time.monotonic() - 5)   # 直接把时间拨过去，别 sleep
        self.assertEqual(len(v), 1)
        # 回收不许把 ref 降级成"从来不存在"：晚来的消费者要听到 ref_expired。
        self.assertEqual(v.consume(stale)[1], "ref_expired")
        self.assertEqual(v.consume(live), (DUMMY, "ok"))
        self.assertEqual(len(v), 0)

    def test_peek_reads_without_consuming(self):
        # 裁定 R40：Task 7 的 scrub 登记要"值还在不在"这个信息，但读一次不许把一次性
        # 句柄花掉（I3 只把消费权给 secret_exec / secret_export）。
        v = Vault(ttl_seconds=300)
        ref = v.issue(DUMMY)
        self.assertEqual(v.peek(ref), DUMMY)
        self.assertEqual(v.peek(ref), DUMMY)
        self.assertEqual(v.consume(ref), (DUMMY, "ok"))
        self.assertIsNone(v.peek(ref))

    def test_peek_of_unknown_or_expired_ref_is_none(self):
        v = Vault(ttl_seconds=1)
        ref = v.issue(DUMMY)
        v._issued[ref] = (DUMMY, time.monotonic() - 5)
        self.assertIsNone(v.peek(ref))
        self.assertIsNone(v.peek("sr_nope"))


if __name__ == "__main__":
    unittest.main()
