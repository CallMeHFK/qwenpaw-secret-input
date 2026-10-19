import asyncio
import time
import unittest

from tests.support import DUMMY

from backend.config import PluginConfig
from backend.records import _GRAVEYARD_CAP, RequestRegistry
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
        # 名字里的 until_consumed 到底测什么：重放回来的仍然是同一张已答卡（T7 要靠它
        # 判 ref 存活），但 ref 只有一次寿命 —— 消费点（T8 走 vault.consume）第二次
        # 拿到的是 ref_unavailable，重放不会把值再交出去一次。
        value, status = self.vault.consume(req.ref)
        self.assertEqual((value, status), (DUMMY, "ok"))
        replayed = self._create(cid="c-2")
        self.assertIs(replayed, req)
        self.assertEqual(replayed.future.result()["ref"], req.ref)
        again_value, again_status = self.vault.consume(replayed.future.result()["ref"])
        self.assertIsNone(again_value)
        self.assertEqual(again_status, "ref_unavailable")
        self.assertNotIn(DUMMY, str(replayed.future.result()))

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
        with self.assertRaises(ValueError) as cm:
            run(self.reg.create(label="l", reason="r", placeholder="", session_id="s",
                                agent_id="a", user_id="u", channel="console",
                                timeout_seconds=5))
        self.assertIn("30..3600", str(cm.exception))   # 越界消息要能把范围说给模型

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
        self.assertIsNone(self.reg.find_by_client_request_id("c-3", "s1"))  # 过期卡不许重放
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
        # 偏离 8（计划正文写的是 session_id[:8]）：日志参数必须先 str 再切。
        # session_id 同样来自宿主，None 与 int 都得活着过 logger.info —— 计划那行会
        # TypeError（logger.info 的实参是 eager 求值的），所以这一句就是那条偏离的门。
        with self.assertLogs("backend.records", level="INFO") as logged:
            for weird_session in (None, 20261019, 7):
                odd = run(self.reg.create(label="l", reason="r", placeholder="",
                                          session_id=weird_session, agent_id=None,
                                          user_id=None, channel=None))
                self.assertEqual(odd.status, "pending")
        self.assertIn("20261019", "\n".join(logged.output))

    # --- 修复轮 1：评审 Critical + Important-1/2/3 与裁定 R15~R21 的门 ---

    def _create_as(self, session_id, agent_id="a1", cid=None):
        return run(self.reg.create(
            label="GitHub token", reason="for the webhook", placeholder="paste it",
            session_id=session_id, agent_id=agent_id, user_id="u1", channel="console",
            client_request_id=cid,
        ))

    def test_client_request_id_is_scoped_to_the_session(self):
        # Critical：cid 是模型自选文本（request_secret 没有命名空间），"retry-1" 这种
        # 默认值极易跨会话相撞。不分会话复用 = B 挂到 A 的卡上、A 的人答交付给 B，
        # 或 B 立刻拿到 A 的 ref 去消费 A 的用户为另一个会话提供的值（spec 7.1/10.2#6）。
        a = self._create_as("sA", cid="retry-1")
        b = self._create_as("sB", cid="retry-1")
        self.assertIsNot(a, b)
        self.assertNotEqual(a.request_id, b.request_id)
        self.assertNotEqual(a.nonce, b.nonce)
        self.assertFalse(b.future.done())                 # B 绝不许挂在 A 的 future 上
        self.assertEqual([r.session_id for r in self.reg.list_pending()], ["sA", "sB"])
        # A 答完之后 A 的记录会被 retained（saved 的重放窗口），B 依旧不许沾。
        run(self.reg.answer(a.request_id, nonce=a.nonce, value=DUMMY, peer="127.0.0.1"))
        self.assertEqual(a.future.result()["status"], "saved")
        self.assertFalse(b.future.done())
        again = self._create_as("sB", cid="retry-1")
        self.assertIs(again, b)                           # 同会话仍然复用
        self.assertEqual(again.status, "pending")
        self.assertIs(self.reg.find_by_client_request_id("retry-1", "sA"), a)
        self.assertIs(self.reg.find_by_client_request_id("retry-1", "sB"), b)

    def test_reuse_refuses_to_serve_a_different_agent(self):
        # 裁定 R15 的 agent 守卫：同一会话里换 agent 用同一个 cid，也不能共用一张卡。
        first = self._create_as("s1", "a1", cid="c-agent")
        self.assertIs(self._create_as("s1", "a1", cid="c-agent"), first)   # 原行为不回归
        other = self._create_as("s1", "a2", cid="c-agent")
        self.assertIsNot(other, first)
        self.assertNotEqual(other.request_id, first.request_id)
        self.assertFalse(other.future.done())
        self.assertEqual(len(self.reg.list_pending("s1")), 2)

    def test_decline_records_the_answerer_audit_triple(self):
        # Important-1 / R16：spec 8.2 要三元组落在**每一条**已答记录上；拒答同样要能
        # 查出"是谁替人类按了拒绝"，否则 T6 的"已由 <peer> 于 <时刻> 提交"对拒答空白。
        req = self._create()
        ok, why = run(self.reg.answer(req.request_id, nonce=req.nonce, decline=True,
                                      peer="10.0.0.9", user_agent="curl/8.9.1"))
        self.assertTrue(ok, why)
        self.assertEqual(req.future.result()["status"], "declined")
        self.assertEqual(req.answered_peer, "10.0.0.9")
        self.assertIsNotNone(req.answered_at)
        self.assertEqual(req.answered_ua, "curl/8.9.1")

    def test_finish_logs_the_answerer_and_never_the_ua_or_the_value(self):
        # R18：spec 8.2 末句"日志里同样只记这三项" ⇒ saved/declined 都要把 peer 与时刻
        # 打进日志；UA 是指定不记的（10.2#6 只承诺 peer + 时间戳），值永远不许出现在日志。
        req = self._create()
        with self.assertLogs("backend.records", level="INFO") as saved:
            run(self.reg.answer(req.request_id, nonce=req.nonce, value=DUMMY,
                                peer="127.0.0.1", user_agent="Mozilla/5.0 (X11; Linux)"))
        saved_text = "\n".join(saved.output)
        self.assertIn("saved", saved_text)
        self.assertIn("127.0.0.1", saved_text)
        self.assertIn("answered_at", saved_text)
        self.assertNotIn("Mozilla", saved_text)
        self.assertNotIn(DUMMY, saved_text)
        decl = self._create()
        with self.assertLogs("backend.records", level="INFO") as declined:
            run(self.reg.answer(decl.request_id, nonce=decl.nonce, decline=True,
                                peer="10.1.2.3", user_agent="curl/8"))
        declined_text = "\n".join(declined.output)
        self.assertIn("declined", declined_text)
        self.assertIn("10.1.2.3", declined_text)
        self.assertNotIn("curl", declined_text)

    def test_cancel_rejects_unknown_reason_and_keeps_the_card(self):
        # R17：reason 是闭集且这一层是唯一产出者；T7/T9 拼错一个字符就会变成模型可见
        # 的状态字段，所以宁可在产出处当场炸。
        req = self._create()
        with self.assertRaises(ValueError) as cm:
            run(self.reg.cancel(req.request_id, "runEnd"))
        message = str(cm.exception)
        for allowed in ("run_ended", "superseded", "shutdown"):
            self.assertIn(allowed, message)
        self.assertIn("runEnd", message)
        self.assertEqual(req.status, "pending")
        self.assertFalse(req.future.done())
        self.assertEqual(len(self.reg.list_pending("s1")), 1)   # 没被半路改掉状态
        for bad in (None, "", "RUN_ENDED"):
            with self.assertRaises(ValueError):
                run(self.reg.cancel(req.request_id, bad))
        self.assertTrue(run(self.reg.cancel(req.request_id, "shutdown")))
        self.assertEqual(req.future.result()["reason"], "shutdown")

    def test_logged_label_is_truncated_to_120_chars(self):
        # R19：label 是模型写的不可信文本（spec 10.2#4），把机密塞进 label 再让它进宿主
        # 日志是真实路径 ⇒ 日志只截 120 字符；记录本身不截（卡片要显示全文案）。
        canary = "S" * 120 + "CANARY-IN-THE-TAIL"
        with self.assertLogs("backend.records", level="INFO") as captured:
            req = self._create(label=canary)
        output = "\n".join(captured.output)
        self.assertIn("S" * 120, output)
        self.assertNotIn("CANARY-IN-THE-TAIL", output)
        self.assertEqual(req.label, canary)


class GraveyardTest(unittest.TestCase):
    """裁定 R36：`_graveyard` 是"已终结"的唯一来源，替换掉 T3 的临时 `_settled_ids`。

    T3 只存 id（够用：那时没人要终态记录对象），T6 要的是记录本身 —— 答后的 meta 要显示
    终态与答者三元组，410 门要把 `expired` 与 `not_found` 分开。两套来源并存的话，审计显示
    与 410 门迟早各说各话，所以这里同时钉住"旧来源已经不存在"。
    """

    def setUp(self):
        self.vault = Vault(ttl_seconds=300)
        self.reg = RequestRegistry(self.vault, timeout_seconds=600, max_pending=8)

    def _create(self, session_id="s1"):
        return run(self.reg.create(
            label="GitHub token", reason="for the webhook", placeholder="paste it",
            session_id=session_id, agent_id="a1", user_id="u1", channel="console",
        ))

    def test_find_record_any_state_returns_the_terminal_record_itself(self):
        for kind in ("saved", "declined", "cancelled"):
            with self.subTest(kind=kind):
                reg = RequestRegistry(Vault(ttl_seconds=300), timeout_seconds=600, max_pending=8)
                req = run(reg.create(label="GitHub token", reason="r", placeholder="p",
                                     session_id="s1", agent_id="a1", user_id="u1",
                                     channel="console"))
                if kind == "saved":
                    run(reg.answer(req.request_id, nonce=req.nonce, value=DUMMY,
                                   peer="127.0.0.1"))
                elif kind == "declined":
                    run(reg.answer(req.request_id, nonce=req.nonce, decline=True,
                                   peer="10.0.0.9"))
                else:
                    run(reg.cancel(req.request_id, "run_ended"))
                rec = reg.find_record_any_state(req.request_id)
                self.assertIs(rec, req)                    # 存引用，不存第二份真相
                self.assertEqual(rec.status, kind)
                self.assertIsNone(reg.get(req.request_id))  # _pending 里确实没有了

        timed = self._create()
        timed.expires_at = time.monotonic() - 1
        self.assertEqual(self.reg.sweep(), 1)
        rec = self.reg.find_record_any_state(timed.request_id)
        self.assertEqual(rec.status, "timed_out")

    def test_terminal_reasons_stay_split_between_expired_answered_and_unknown(self):
        # 墓地单源之后三支仍互不塌陷：这是 T6 把 410/410/404 分开的前提。
        saved = self._create()
        run(self.reg.answer(saved.request_id, nonce=saved.nonce, value=DUMMY))
        declined = self._create()
        run(self.reg.answer(declined.request_id, nonce=declined.nonce, decline=True))
        expired = self._create()
        expired.expires_at = time.monotonic() - 1
        self.reg.sweep()
        self.assertEqual(run(self.reg.answer(saved.request_id, nonce=saved.nonce,
                                             value=DUMMY)), (False, "already_answered"))
        self.assertEqual(run(self.reg.answer(declined.request_id, nonce=declined.nonce,
                                             value=DUMMY)), (False, "already_answered"))
        self.assertEqual(run(self.reg.answer(expired.request_id, nonce=expired.nonce,
                                             value=DUMMY)), (False, "expired"))
        self.assertEqual(run(self.reg.answer("e" * 32, nonce="x", value=DUMMY)),
                         (False, "not_found"))

    def test_the_stopgap_sources_are_gone_and_the_graveyard_holds_no_plaintext(self):
        import backend.records as records_module

        self.assertFalse(hasattr(self.reg, "_settled_ids"),
                         "R36: _settled_ids must be replaced by the graveyard, not coexist")
        self.assertFalse(hasattr(records_module, "_EXPIRED_MARKS"),
                         "expired must come from the graveyard record's status")
        req = self._create()
        run(self.reg.answer(req.request_id, nonce=req.nonce, value=DUMMY, peer="127.0.0.1"))
        rec = self.reg.find_record_any_state(req.request_id)
        self.assertNotIn(DUMMY, str(rec))                  # repr 是 __repr__ 那道闸
        self.assertNotIn(DUMMY, repr(self.reg._graveyard))
        self.assertNotIn(req.nonce, str(rec))

    def test_graveyard_is_bounded_and_eviction_is_oldest_first(self):
        # 有界：墓地只留最近的 _GRAVEYARD_CAP 条终态。逐出后的后果必须如实：
        # 那条 id 退化成 not_found（404），而不是永远占内存。
        records = [self._create() for _ in range(_GRAVEYARD_CAP + 40)]
        for req in records:
            run(self.reg.answer(req.request_id, nonce=req.nonce, value=DUMMY,
                                peer="127.0.0.1"))
        self.assertLessEqual(len(self.reg._graveyard), _GRAVEYARD_CAP)
        self.assertIsNone(self.reg.find_record_any_state(records[0].request_id))
        self.assertEqual(run(self.reg.answer(records[0].request_id, nonce=records[0].nonce,
                                             value=DUMMY)), (False, "not_found"))
        survivor = self.reg.find_record_any_state(records[-1].request_id)
        self.assertEqual(survivor.status, "saved")
        self.assertEqual(survivor.answered_peer, "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
