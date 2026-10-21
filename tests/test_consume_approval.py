"""Consent card before a ref is spent.

The host does **not** gate plugin tools behind its own approval; measured on the
live 2.2.2 host on 2026-10-21: `/api/approval/list` stayed empty while
`secret_exec` ran. The plugin therefore owns a confirmation card of its own.

This file also pins the honest boundary: the card is a consent/visibility
device, not a sandbox. The same loopback caller the plugin accepts can answer it
(spec §10.2 item 6), so a deliberately malicious agent can approve its own
command. What the card does buy is that an accidental or prompt-injected call
becomes visible to the human *before* the value leaves, and that a denial costs
the ref nothing.
"""

import asyncio
import json
import time
import unittest

from backend.config import PluginConfig
from backend.records import RequestRegistry
from backend.scrub import REDACT, Scrubber
from backend.tools import SECRET_MARKER, SecretTools
from backend.vault import Vault
from tests.support import DUMMY


def run(coro):
    return asyncio.run(coro)


def make_registry(timeout=30):
    vault = Vault(ttl_seconds=300)
    return vault, RequestRegistry(vault, timeout_seconds=timeout, max_pending=8)


def create_confirm(registry, kind="exec", command="printf hi", cwd="/tmp",
                   env_name="SECRET", session_id="s1", agent_id="a1",
                   timeout_seconds=30):
    return run(registry.create_confirmation(
        kind=kind, command=command, cwd=cwd, env_name=env_name,
        session_id=session_id, agent_id=agent_id, user_id="u1", channel="console",
        timeout_seconds=timeout_seconds,
    ))


class ConfirmationRegistryTest(unittest.TestCase):
    def setUp(self):
        self.vault, self.registry = make_registry()

    def test_approve_finishes_approved_and_issues_no_ref(self):
        rec = create_confirm(self.registry)
        ok, why = run(self.registry.answer(rec.request_id, nonce=rec.nonce, approve=True))
        self.assertTrue(ok, why)
        self.assertEqual(rec.status, "approved")
        self.assertIsNone(rec.ref)
        self.assertEqual(len(self.vault), 0)
        self.assertEqual(self.registry.list_pending(), [])

    def test_decline_finishes_declined(self):
        rec = create_confirm(self.registry)
        ok, why = run(self.registry.answer(rec.request_id, nonce=rec.nonce, decline=True))
        self.assertTrue(ok, why)
        self.assertEqual(rec.status, "declined")
        self.assertIsNone(rec.ref)

    def test_neither_decision_is_rejected_and_the_card_stays_pending(self):
        rec = create_confirm(self.registry)
        ok, why = run(self.registry.answer(rec.request_id, nonce=rec.nonce))
        self.assertFalse(ok)
        self.assertEqual(why, "invalid_decision_field")
        self.assertEqual(rec.status, "pending")
        self.assertEqual(len(self.registry.list_pending()), 1)

    def test_both_decisions_are_rejected(self):
        rec = create_confirm(self.registry)
        ok, why = run(self.registry.answer(
            rec.request_id, nonce=rec.nonce, approve=True, decline=True))
        self.assertFalse(ok)
        self.assertEqual(why, "invalid_decision_field")
        self.assertEqual(rec.status, "pending")

    def test_confirmation_can_never_carry_a_value(self):
        rec = create_confirm(self.registry)
        ok, why = run(self.registry.answer(
            rec.request_id, nonce=rec.nonce, value=DUMMY, approve=True))
        self.assertFalse(ok)
        self.assertEqual(why, "invalid_value_field")
        self.assertEqual(rec.status, "pending")
        self.assertEqual(len(self.vault), 0)

    def test_approve_on_a_secret_request_is_rejected(self):
        rec = run(self.registry.create(
            label="GitHub token", reason="webhook", placeholder="",
            session_id="s1", agent_id="a1", user_id="u1", channel="console",
        ))
        ok, why = run(self.registry.answer(rec.request_id, nonce=rec.nonce, approve=True))
        self.assertFalse(ok)
        self.assertEqual(why, "invalid_approve_field")
        self.assertEqual(rec.status, "pending")

    def test_kind_is_a_closed_set(self):
        with self.assertRaises(ValueError):
            create_confirm(self.registry, kind="bogus")

    def test_repr_never_carries_the_command(self):
        rec = create_confirm(self.registry, command="echo SUPERSECRET-COMMAND-TEXT")
        self.assertNotIn("SUPERSECRET", repr(rec))
        self.assertNotIn("nonce", repr(rec))

    def test_confirmation_is_scoped_to_its_session(self):
        create_confirm(self.registry, session_id="s1")
        self.assertEqual(len(self.registry.list_pending("s2")), 0)
        self.assertEqual(len(self.registry.list_pending("s1")), 1)


class VaultRemainingTest(unittest.TestCase):
    def test_live_ref_reports_its_remaining_ttl(self):
        vault = Vault(ttl_seconds=300)
        ref = vault.issue(DUMMY)
        left = vault.remaining(ref)
        self.assertIsNotNone(left)
        self.assertGreater(left, 290)
        self.assertLessEqual(left, 300)

    def test_expired_ref_reports_zero(self):
        vault = Vault(ttl_seconds=300)
        ref = vault.issue(DUMMY)
        vault._issued[ref] = (DUMMY, 0.0)
        self.assertEqual(vault.remaining(ref), 0.0)

    def test_unknown_ref_reports_none(self):
        self.assertIsNone(Vault(ttl_seconds=300).remaining("sr_never-issued"))


def make_tools(*, approval=True, answer_timeout=30):
    vault = Vault(ttl_seconds=300)
    registry = RequestRegistry(vault, timeout_seconds=answer_timeout, max_pending=8)
    scrubber = Scrubber()
    ref = vault.issue(DUMMY)
    scrubber.register(DUMMY)
    tools = SecretTools(
        registry=registry, vault=vault, scrubber=scrubber,
        config=PluginConfig(require_consume_approval=approval,
                            answer_timeout_seconds=answer_timeout),
        identity=lambda: {"session_id": "s1", "agent_id": "a1", "user_id": "u1",
                          "channel": "console"},
        cancel_event=lambda: None, env_writer=lambda key, value: None,
    )
    return tools, vault, registry, ref


async def answer_when_pending(registry, *, approve=False, decline=False,
                              mutate=None):
    for _ in range(500):
        pending = registry.list_pending()
        if pending:
            rec = pending[0]
            if mutate is not None:
                mutate(rec)
            return await registry.answer(
                rec.request_id, nonce=rec.nonce, approve=approve, decline=decline)
        await asyncio.sleep(0.01)
    raise AssertionError("no confirmation card appeared")


async def exec_and_answer(tools, registry, ref, command, *, approve=False,
                          decline=False, mutate=None):
    task = asyncio.create_task(answer_when_pending(
        registry, approve=approve, decline=decline, mutate=mutate))
    try:
        out = await asyncio.wait_for(tools.secret_exec(ref, command), timeout=5)
    finally:
        if not task.done():
            task.cancel()
    await task
    return json.loads(out)


class SecretExecApprovalTest(unittest.TestCase):
    def test_approval_shows_the_exact_template_then_runs_it(self):
        tools, vault, registry, ref = make_tools()
        seen = {}

        async def read_card():
            for _ in range(500):
                pending = registry.list_pending()
                if pending:
                    seen["rec"] = pending[0]
                    return await registry.answer(
                        pending[0].request_id, nonce=pending[0].nonce, approve=True)
                await asyncio.sleep(0.01)
            raise AssertionError("no confirmation card appeared")

        async def go():
            task = asyncio.create_task(read_card())
            out = await asyncio.wait_for(
                tools.secret_exec(ref, f'printf "%s" {SECRET_MARKER}'), timeout=5)
            await task
            return json.loads(out)

        out = run(go())
        self.assertEqual(out["status"], "ok", out)
        self.assertEqual(out["stdout"], REDACT)
        self.assertEqual(out["ref"], "consumed")
        rec = seen["rec"]
        self.assertEqual(rec.kind, "exec")
        self.assertEqual(rec.command, f'printf "%s" {SECRET_MARKER}')
        self.assertEqual(rec.env_name, "SECRET")
        self.assertNotIn(DUMMY, repr(rec))
        self.assertEqual(vault.status(ref), "gone")

    def test_denial_keeps_the_ref_and_never_spawns(self):
        tools, vault, registry, ref = make_tools()
        original = asyncio.create_subprocess_exec

        async def forbidden(*_a, **_k):
            raise AssertionError("a denied command must never be spawned")

        asyncio.create_subprocess_exec = forbidden
        try:
            out = run(exec_and_answer(tools, registry, ref, "true", decline=True))
        finally:
            asyncio.create_subprocess_exec = original
        self.assertEqual(out["status"], "declined")
        self.assertEqual(out["ref"], "not_consumed")
        self.assertEqual(vault.status(ref), "live")

    def test_timeout_keeps_the_ref_and_never_spawns(self):
        tools, vault, registry, ref = make_tools()

        def expire(rec):
            rec.expires_at = time.monotonic() - 1

        original = asyncio.create_subprocess_exec

        async def forbidden(*_a, **_k):
            raise AssertionError("a timed-out confirmation must never spawn")

        asyncio.create_subprocess_exec = forbidden
        try:
            async def go():
                task = asyncio.create_task(answer_when_pending(registry, mutate=expire))
                out = await asyncio.wait_for(tools.secret_exec(ref, "true"), timeout=5)
                await task
                return json.loads(out)

            out = run(go())
        finally:
            asyncio.create_subprocess_exec = original
        self.assertEqual(out["status"], "timed_out")
        self.assertEqual(out["ref"], "not_consumed")
        self.assertEqual(vault.status(ref), "live")

    def test_cancel_while_waiting_keeps_the_ref(self):
        tools, vault, registry, ref = make_tools()
        cancel = asyncio.Event()

        async def go():
            tools._cancel_event = lambda: cancel
            task = asyncio.create_task(_cancel_when_pending(registry, cancel))
            out = await asyncio.wait_for(tools.secret_exec(ref, "true"), timeout=5)
            await task
            return json.loads(out)

        out = run(go())
        self.assertEqual(out["status"], "cancelled")
        self.assertEqual(out["ref"], "not_consumed")
        self.assertEqual(vault.status(ref), "live")

    def test_approval_disabled_restores_the_direct_path(self):
        tools, _vault, registry, ref = make_tools(approval=False)
        out = json.loads(run(tools.secret_exec(ref, "true")))
        self.assertEqual(out["status"], "ok")
        self.assertEqual(registry.list_pending(), [])

    def test_overlong_command_is_refused_before_any_card_is_shown(self):
        tools, vault, registry, ref = make_tools()
        out = json.loads(run(tools.secret_exec(ref, "x" * 5000)))
        self.assertEqual(out["status"], "error")
        self.assertIn("too long", out["message"])
        self.assertEqual(registry.list_pending(), [])
        self.assertEqual(vault.status(ref), "live")

    def test_confirmation_timeout_never_outlives_the_ref(self):
        tools, vault, registry, ref = make_tools(answer_timeout=30)
        vault._issued[ref] = (DUMMY, time.monotonic() - 290)   # ~10 s left
        seen = {}

        def deny_and_capture(rec):
            seen["left"] = rec.expires_at - time.monotonic()

        out = run(exec_and_answer(tools, registry, ref, "true", decline=True,
                                  mutate=deny_and_capture))
        self.assertEqual(out["status"], "declined")
        self.assertLessEqual(seen["left"], 11)
        self.assertGreater(seen["left"], 0)


async def _cancel_when_pending(registry, event):
    for _ in range(500):
        if registry.list_pending():
            event.set()
            return
        await asyncio.sleep(0.01)
    raise AssertionError("no confirmation card appeared")


async def export_and_answer(tools, registry, ref, key, *, approve=False, decline=False):
    task = asyncio.create_task(answer_when_pending(
        registry, approve=approve, decline=decline))
    try:
        out = await asyncio.wait_for(tools.secret_export(ref, key), timeout=5)
    finally:
        if not task.done():
            task.cancel()
    await task
    return json.loads(out)


class SecretExportApprovalTest(unittest.TestCase):
    def test_export_runs_only_after_approval(self):
        written = {}
        tools, vault, registry, ref = make_tools()
        tools._env_writer = lambda key, value: written.update(key=key, value=value)
        out = run(export_and_answer(tools, registry, ref, "GITHUB_TOKEN", approve=True))
        self.assertEqual(out["status"], "ok", out)
        self.assertEqual(written, {"key": "GITHUB_TOKEN", "value": DUMMY})
        self.assertEqual(out["env_key"], "GITHUB_TOKEN")
        self.assertEqual(vault.status(ref), "gone")

    def test_export_denial_keeps_the_ref_and_writes_nothing(self):
        written = {}
        tools, vault, registry, ref = make_tools()
        tools._env_writer = lambda key, value: written.update(key=key, value=value)
        out = run(export_and_answer(tools, registry, ref, "GITHUB_TOKEN", decline=True))
        self.assertEqual(out["status"], "declined")
        self.assertEqual(out["ref"], "not_consumed")
        self.assertEqual(written, {})
        self.assertEqual(vault.status(ref), "live")


if __name__ == "__main__":
    unittest.main()
