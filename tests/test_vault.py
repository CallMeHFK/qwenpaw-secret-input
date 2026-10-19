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
