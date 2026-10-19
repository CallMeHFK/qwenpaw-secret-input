import asyncio
import time
import unittest

from tests.support import DUMMY

from backend.config import PluginConfig
from backend.records import RequestRegistry
from backend.vault import Vault


def run(coro):
    return asyncio.run(coro)


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.vault = Vault(ttl_seconds=300)
        self.reg = RequestRegistry(self.vault, timeout_seconds=600, max_pending=3)

    def _create(self, cid=None, label="GitHub token", reason="for the webhook"):
        return run(self.reg.create(
            label=label, reason=reason, placeholder="paste it",
            session_id="s1", agent_id="a1", user_id="u1", channel="console",
            client_request_id=cid,
        ))

    def test_save_answer_resolves_future_with_ref_and_no_value(self):
        req = self._create()
        ok, why = run(self.reg.answer(req.request_id, nonce=req.nonce, value=DUMMY,
                                      peer="127.0.0.1", user_agent="Mozilla/5.0"))
        self.assertTrue(ok, why)
        outcome = req.future.result()
        self.assertEqual(outcome["status"], "saved")
        self.assertTrue(outcome["ref"].startswith("sr_"))
        self.assertNotIn(DUMMY, str(outcome))          # 结果里只有 ref
        self.assertEqual(req.answered_peer, "127.0.0.1")  # 答者审计
        self.assertIsNotNone(req.answered_at)

    def test_decline_resolves_with_declined(self):
        req = self._create()
        ok, _ = run(self.reg.answer(req.request_id, nonce=req.nonce, decline=True))
        self.assertTrue(ok)
        self.assertEqual(req.future.result()["status"], "declined")

    def test_wrong_nonce_is_rejected_and_stays_pending(self):
        req = self._create()
        ok, why = run(self.reg.answer(req.request_id, nonce="nope", value=DUMMY))
        self.assertFalse(ok)
        self.assertEqual(why, "invalid_nonce")
        self.assertEqual(len(self.reg.list_pending("s1")), 1)

    def test_double_answer_reports_already_answered(self):
        req = self._create()
        run(self.reg.answer(req.request_id, nonce=req.nonce, value=DUMMY))
        ok, why = run(self.reg.answer(req.request_id, nonce=req.nonce, value=DUMMY))
        self.assertFalse(ok)
        self.assertEqual(why, "already_answered")

    def test_unknown_request_id_is_not_found(self):
        ok, why = run(self.reg.answer("deadbeef", nonce="x", value=DUMMY))
        self.assertFalse(ok)
        self.assertEqual(why, "not_found")

    def test_client_request_id_reuses_the_same_card(self):
        first = self._create(cid="c-1")
        second = self._create(cid="c-1")
        self.assertIs(first, second)                     # 不重发卡
        self.assertEqual(len(self.reg.list_pending()), 1)

    def test_replay_returns_the_same_ref_until_consumed(self):
        req = self._create(cid="c-2")
        run(self.reg.answer(req.request_id, nonce=req.nonce, value=DUMMY))
        again = self._create(cid="c-2")
        self.assertIs(again, req)
        self.assertEqual(again.future.result()["ref"], req.ref)

    def test_timeout_marks_record_and_resolves_future(self):
        req = self._create()
        req.expires_at = time.monotonic() - 1
        self.assertEqual(self.reg.sweep(), 1)
        self.assertEqual(req.future.result()["status"], "timed_out")
        self.assertEqual(req.status, "timed_out")

    def test_cancel_resolves_with_reason(self):
        req = self._create()
        self.assertTrue(run(self.reg.cancel(req.request_id, "run_ended")))
        outcome = req.future.result()
        self.assertEqual(outcome["status"], "cancelled")
        self.assertEqual(outcome["reason"], "run_ended")

    def test_max_pending_cancels_oldest_and_reports_superseded(self):
        old = self._create(label="oldest")
        for i in range(3):
            self._create(label=f"n{i}")
        self.assertEqual(old.future.result()["status"], "cancelled")
        self.assertEqual(old.future.result()["reason"], "superseded")
        self.assertLessEqual(len(self.reg.list_pending()), 3)

    def test_timeout_floor_and_ceiling(self):
        ok_timeout = run(self.reg.create(label="l", reason="r", placeholder="", session_id="s2",
                                         agent_id="a", user_id="u", channel="console",
                                         timeout_seconds=PluginConfig().answer_timeout_seconds))
        self.assertEqual(ok_timeout.status, "pending")
        with self.assertRaises(ValueError):
            run(self.reg.create(label="l", reason="r", placeholder="", session_id="s",
                                agent_id="a", user_id="u", channel="console",
                                timeout_seconds=5))

    # --- 以下四条钉住本次对计划正文的四处必要偏离（见 task-3-report.md §6）---

    def test_answer_racing_cancel_settles_the_future_exactly_once(self):
        # 计划正文用 asyncio.ensure_future 延后 supersede，且 answer/_finish 之间没有 await；
        # 这条把"future 只被结算一次"钉成门：并发 answer+cancel 之后，
        # 第二次结算必须炸 InvalidStateError。
        req = self._create()

        async def hammer():
            return await asyncio.gather(
                self.reg.answer(req.request_id, nonce=req.nonce, value=DUMMY),
                self.reg.cancel(req.request_id, "run_ended"),
                self.reg.answer(req.request_id, nonce=req.nonce, decline=True),
                self.reg.cancel(req.request_id, "shutdown"),
            )

        results = run(hammer())
        settled = sum(1 for r in results if r is True or (isinstance(r, tuple) and r[0]))
        self.assertEqual(settled, 1)                    # 四路并发只可能有一路真结算
        self.assertTrue(req.future.done())
        with self.assertRaises(asyncio.InvalidStateError):
            req.future.set_result({"status": "poison"})
        outcome = req.future.result()
        self.assertEqual(req.status, outcome["status"])  # 记录状态与 future 结果同源
        self.assertNotIn(DUMMY, str(outcome))
        self.assertIsNone(self.reg.get(req.request_id))
        self.assertEqual(self.reg.list_pending("s1"), [])

    def test_sweep_leaves_status_future_and_later_answers_in_agreement(self):
        req = self._create(cid="c-3")
        req.expires_at = time.monotonic() - 1
        self.assertEqual(self.reg.sweep(), 1)
        self.assertEqual(self.reg.sweep(), 0)             # 扫走的卡不再重复计数
        self.assertEqual(req.status, "timed_out")
        self.assertEqual(req.future.result()["status"], "timed_out")
        self.assertEqual(req.seconds_left(), 0.0)
        ok, why = run(self.reg.answer(req.request_id, nonce=req.nonce))
        self.assertFalse(ok)
        self.assertEqual(why, "expired")                  # 不是 not_found，也不是 already_answered
        self.assertIsNone(self.reg.find_by_client_request_id("c-3"))  # 过期卡不许重放
        fresh = self._create(cid="c-3")
        self.assertIsNot(fresh, req)
        self.assertEqual(fresh.status, "pending")

    def test_capacity_is_enforced_before_create_returns(self):
        # 钉住偏离：计划正文用 asyncio.ensure_future 把 supersede 推到下一轮 loop，于是同
        # 一个 loop 里 create 刚返回时上限还没生效（/pending 多报一张卡、被逐出者的 future
        # 还没结算）。所以这里刻意用一条协程连发四次，中途不 yield 回 loop。
        async def scenario():
            first = await self.reg.create(label="first", reason="r", placeholder="",
                                          session_id="s1", agent_id="a", user_id="u",
                                          channel="console")
            for i in range(2):
                await self.reg.create(label=f"f{i}", reason="r", placeholder="",
                                      session_id="s1", agent_id="a", user_id="u",
                                      channel="console")
            self.assertEqual(len(self.reg.list_pending()), 3)   # 还没越界
            newest = await self.reg.create(label="overflow", reason="r", placeholder="",
                                           session_id="s1", agent_id="a", user_id="u",
                                           channel="console")
            self.assertEqual(len(self.reg.list_pending()), 3)   # 越界当场就补上了
            self.assertTrue(first.future.done())
            self.assertEqual((first.status, first.cancel_reason), ("cancelled", "superseded"))
            self.assertEqual(newest.status, "pending")

        run(scenario())

    def test_malformed_timeout_and_identity_stay_inside_the_layer(self):
        # 坏 timeout 一律 ValueError（T7 只把 ValueError 翻成 error 串）；怪身份不许抛 TypeError
        run(self.reg.create(label=None, reason=None, placeholder=None, session_id=None,
                            agent_id=None, user_id=None, channel=None))
        for bad in ("abc", True, 3601, "60.5"):
            with self.assertRaises(ValueError):
                run(self.reg.create(label="l", reason="r", placeholder="", session_id="s",
                                    agent_id="a", user_id="u", channel="console",
                                    timeout_seconds=bad))
        self.assertEqual(run(self.reg.create(label="l", reason="r", placeholder="",
                                             session_id="s", agent_id="a", user_id="u",
                                             channel="console", timeout_seconds="60")).status,
                         "pending")
        # 来自网络输入的形状怪：只许得到 not_found / invalid_nonce，不许 TypeError 抛穿到端点
        for weird_id in (None, "", 123, ["a"], {"k": "v"}):
            self.assertEqual(run(self.reg.answer(weird_id, nonce="x", value=DUMMY)),
                             (False, "not_found"))
        req = self._create()
        for weird_nonce in (None, "", 123, "é☃", "frésh"):
            ok, why = run(self.reg.answer(req.request_id, nonce=weird_nonce, value=DUMMY))
            self.assertFalse(ok)
            self.assertEqual(why, "invalid_nonce")
        self.assertEqual(len(self.reg.list_pending("s1")), 1)  # 抢答失败后卡还在


if __name__ == "__main__":
    unittest.main()
