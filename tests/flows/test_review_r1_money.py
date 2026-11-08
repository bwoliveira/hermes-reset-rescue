"""Review round 1, lane FA — the money boundary (R1, R2, R3, R4, R5, R10, R15, R16).

Every test here is the reviewer's own counterexample, ported to the repo's
harness (fakes only, no network), plus one variant of the same class designed
here. The class is the same in all of them: a rule about spending a credit that
one caller enforces and another silently skips.

The tests are written against the **public** surface a user reaches — the CLI,
the host service, the planner, the notifier and the real ``Store`` — not against
the gate's internals, so a fix that special-cases a caller still fails here.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import unittest

from reset_rescue.accounts import (
    CREDITS_PATH,
    OFFICIAL_ORIGIN,
    AccountRef,
    PoolAccountSource,
    Response,
    mask_label,
)
from reset_rescue.cli import build_parser, handle_cli
from reset_rescue.fire import account_label, notice_params
from reset_rescue.host import HostService
from reset_rescue.notify import Notifier
from reset_rescue.service import Service
from tests.flows.support import HOUR, MINUTE, Harness

#: The token the reviewer's R3 counterexample builds: a workspace claim plus a
#: user subject, which is what every real pool row carries (docs/evidence/S2.md).
WORKSPACE = "synthetic-workspace"
USER = "synthetic-user"


def _jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{payload}.signature"


def _token(*, user: str = USER, workspace: str = WORKSPACE) -> str:
    claims: dict = {"https://api.openai.com/auth": {"chatgpt_account_id": workspace}}
    if user:
        claims["sub"] = user
    return _jwt(claims)


class R1DryRun(unittest.TestCase):
    """R1 — dry run still permits real consume POSTs."""

    def test_dry_run_setting_must_stop_an_already_armed_fire(self):
        """The reviewer's counterexample: a slot armed before ``dry_run`` was set."""
        h = Harness(self)
        account, key = h.armed(expires_in=HOUR)
        h.deps.settings = dataclasses.replace(h.settings, dry_run=True)
        h.clock.set(h.store.get_slot(key, "primary")["fire_at"])
        h.run_fire()
        self.assertEqual(h.backend.posts, [], "the pause switch did not pause")

    def test_dry_run_setting_must_stop_the_late_scan_branch(self):
        """Variant: the scan's own late branch, not a slot, with the setting on."""
        h = Harness(self, dry_run=True)
        account = h.add_account()
        h.add_credit(account, expires_in=8 * MINUTE)
        h.scan()
        self.assertEqual(h.backend.posts, [], "the late branch ignored dry_run")

    def test_the_cli_fire_dry_run_flag_must_be_honoured(self):
        """The reviewer's counterexample: ``fire --dry-run`` parsed, then dropped."""
        h = Harness(self)
        account, key = h.armed(expires_in=HOUR)
        h.deps.fire = h.fire()
        h.clock.set(h.store.get_slot(key, "primary")["fire_at"])
        args = build_parser().parse_args(["fire", "--role", "primary", "--dry-run"])
        handle_cli(args, service=HostService(h.deps), out=lambda _: None, err=lambda _: None)
        self.assertEqual(h.backend.posts, [], "the explicit flag was discarded")

    def test_the_cli_scan_must_preserve_the_dry_run_setting_without_a_flag(self):
        """The reviewer's counterexample: an absent flag overriding the setting."""
        h = Harness(self, dry_run=True)
        account = h.add_account()
        h.add_credit(account, expires_in=8 * MINUTE)
        h.deps.planner = h.planner()
        args = build_parser().parse_args(["scan"])
        handle_cli(args, service=Service(h.deps), out=lambda _: None, err=lambda _: None)
        self.assertEqual(h.backend.posts, [], "an absent flag overrode the setting")

    def test_the_cli_scan_dry_run_flag_must_stop_a_late_credit(self):
        """Variant: the flag *on* with the setting off, through the same CLI path."""
        h = Harness(self)
        account = h.add_account()
        h.add_credit(account, expires_in=8 * MINUTE)
        h.deps.planner = h.planner()
        args = build_parser().parse_args(["scan", "--dry-run"])
        handle_cli(args, service=Service(h.deps), out=lambda _: None, err=lambda _: None)
        self.assertEqual(h.backend.posts, [], "the scan flag was discarded")


class R2LeadWindow(unittest.TestCase):
    """R2 — automatic redemption can run before the user's lead window."""

    def test_auto_scan_must_not_consume_before_the_lead_window(self):
        """The reviewer's counterexample: 12 minutes left, lead is 10."""
        h = Harness(self)
        account = h.add_account()
        h.add_credit(account, expires_in=12 * MINUTE)
        h.scan()
        self.assertEqual(h.backend.posts, [], "12 minutes remain, lead is 10")

    def test_an_armed_slot_must_revalidate_a_fresh_expiry_extension(self):
        """The reviewer's counterexample: the backend extended the expiry by a day."""
        h = Harness(self)
        account, key = h.armed(expires_in=HOUR)
        h.clock.set(h.store.get_slot(key, "primary")["fire_at"])
        h.backend.only(account.account_key).expires_at += 24 * HOUR
        h.run_fire()
        self.assertEqual(h.backend.posts, [], "the saved expiry authorized the spend")

    def test_a_manual_primary_fire_before_the_lead_boundary_must_not_spend(self):
        """Variant: the 30 s due-slack let a manual ``fire`` run early."""
        h = Harness(self)
        account, key = h.armed(expires_in=HOUR)
        fire_at = float(h.store.get_slot(key, "primary")["fire_at"])
        h.clock.set(fire_at - 20.0)
        h.run_fire()
        self.assertEqual(h.backend.posts, [], "20 s of slack spent the credit early")

    def test_a_shortened_lead_window_must_still_authorize_the_slot(self):
        """Variant: the user widened the lead, so the armed slot is now in-window."""
        h = Harness(self)
        account, key = h.armed(expires_in=HOUR)
        h.deps.settings = dataclasses.replace(h.settings, lead_minutes=90)
        h.clock.set(h.store.get_slot(key, "primary")["fire_at"])
        h.run_fire()
        self.assertEqual(len(h.backend.posts), 1, "a wider lead window refused its own slot")


class R3Header(unittest.TestCase):
    """R3 — pool identity is incorrectly sent as ``ChatGPT-Account-ID``."""

    def _source(self, *, rows, seen: list):
        def transport(method, url, headers, body, timeout):
            seen.append(dict(headers))
            return Response(200, {"credits": []})

        return PoolAccountSource(salt="a" * 64, read_pool=lambda: rows, transport=transport)

    def test_the_header_must_be_the_workspace_not_the_identity_key(self):
        """The reviewer's counterexample: ``workspace:user`` was sent."""
        seen: list[dict] = []
        source = self._source(
            rows=[{"id": "entry-review", "account_id": WORKSPACE, "access_token": _token()}],
            seen=seen,
        )
        source.request(source.accounts()[0], "GET", OFFICIAL_ORIGIN + CREDITS_PATH)
        self.assertEqual(seen[0]["ChatGPT-Account-ID"], WORKSPACE)

    def test_two_users_in_one_workspace_must_share_the_header_but_not_the_key(self):
        """Variant: the case the composite identity exists for."""
        seen: list[dict] = []
        source = self._source(
            rows=[
                {"id": "entry-a", "account_id": WORKSPACE, "access_token": _token(user="user-a")},
                {"id": "entry-b", "account_id": WORKSPACE, "access_token": _token(user="user-b")},
            ],
            seen=seen,
        )
        refs = source.accounts()
        self.assertEqual(len(refs), 2, "two users of one workspace collapsed into one account")
        self.assertNotEqual(refs[0].account_key, refs[1].account_key)
        for ref in refs:
            source.request(ref, "GET", OFFICIAL_ORIGIN + CREDITS_PATH)
        self.assertEqual(
            [headers["ChatGPT-Account-ID"] for headers in seen], [WORKSPACE, WORKSPACE]
        )
        self.assertEqual(
            sorted(headers["Authorization"] for headers in seen),
            sorted([f"Bearer {_token(user='user-a')}", f"Bearer {_token(user='user-b')}"]),
        )


class R4Readback(unittest.TestCase):
    """R4 — a malformed HTTP-200 list is accepted as proof of consumption."""

    def test_a_malformed_http_200_readback_must_not_claim_consumed(self):
        """The reviewer's counterexample: ``{'unexpected': ...}`` settled a credit."""
        h = Harness(self)
        account, key = h.armed(expires_in=HOUR)
        h.clock.set(h.store.get_slot(key, "primary")["fire_at"])

        def on_post(*_args):
            h.backend.on_list = lambda _account_key: {"unexpected": "not a credit list"}
            return {"code": "reset", "windows_reset": 1}

        h.backend.on_post = on_post
        result = h.run_fire()
        self.assertFalse(result.results[0].confirmed, "a malformed body confirmed the spend")
        row = h.store.get_credit(key)
        self.assertNotEqual(row["status"], "redeemed")
        self.assertTrue(row["redeem_request_id"], "the idempotency UUID was dropped")
        self.assertIsNotNone(h.store.get_slot(key, "backup"), "the backup was suppressed")

    def test_a_list_without_the_credits_container_must_not_confirm_absence(self):
        """Variant: a valid-looking body whose ``credits`` key is missing entirely."""
        h = Harness(self)
        account, key = h.armed(expires_in=HOUR)
        h.clock.set(h.store.get_slot(key, "primary")["fire_at"])

        def on_post(*_args):
            h.backend.on_list = lambda _account_key: {"available_count": 1}
            return {"code": "reset", "windows_reset": 1}

        h.backend.on_post = on_post
        result = h.run_fire()
        self.assertFalse(result.results[0].confirmed, "a missing container confirmed the spend")
        row = h.store.get_credit(key)
        self.assertNotEqual(row["status"], "redeemed")
        self.assertTrue(row["redeem_request_id"], "the idempotency UUID was dropped")
        self.assertIsNotNone(h.store.get_slot(key, "backup"), "the backup was suppressed")

    def test_a_valid_empty_list_still_confirms_absence(self):
        """The other half of the rule: a real empty list is a real confirmation."""
        h = Harness(self)
        account, key = h.armed(expires_in=HOUR)
        h.clock.set(h.store.get_slot(key, "primary")["fire_at"])

        def on_post(*_args):
            h.backend.on_list = lambda _account_key: {"credits": []}
            return {"code": "reset", "windows_reset": 1}

        h.backend.on_post = on_post
        result = h.run_fire()
        self.assertTrue(result.results[0].confirmed, "a valid empty list is a confirmation")
        self.assertEqual(h.store.get_credit(key)["status"], "redeemed")


class R5Retention(unittest.TestCase):
    """R5 — retention can delete the UUID of a credit that was not consumed."""

    @staticmethod
    def redeem_now(h, key):
        """The user's own path: ``redeem now`` through the service, confirmed.

        Deliberately not ``fire.redeem_one``: the service is what a user (and
        the dashboard) actually calls, and it exists unchanged on both trees, so
        a RED here is about retention and not about a signature.
        """
        h.deps.fire = h.fire()
        return Service(h.deps).redeem_now(key, confirm=True)

    def test_nothing_to_reset_must_keep_one_uuid_for_the_credit_lifetime(self):
        """The reviewer's counterexample: two POSTs, two UUIDs after pruning."""
        h = Harness(self)
        account, key = h.armed(expires_in=30 * 24 * HOUR)
        h.backend.on_post = lambda *_args: {"code": "nothing_to_reset"}
        self.redeem_now(h, key)
        first = h.backend.posts[-1]["redeem_request_id"]
        h.advance(15 * 24 * HOUR)
        h.store.prune(h.clock.now())
        h.scan()
        row = h.store.get_credit(key)
        self.assertIsNotNone(row, "retention deleted a credit that was never consumed")
        self.redeem_now(h, key)
        self.assertEqual(h.backend.posts[-1]["redeem_request_id"], first)

    def test_a_failed_post_must_keep_its_row_and_uuid_for_the_credit_lifetime(self):
        """Variant: the same rule for ``failed``, the other non-consumed status."""
        h = Harness(self)
        account, key = h.armed(expires_in=30 * 24 * HOUR)
        h.backend.on_post = lambda *_args: {"code": "http_error"}
        self.redeem_now(h, key)
        first = h.backend.posts[-1]["redeem_request_id"]
        h.advance(15 * 24 * HOUR)
        h.store.prune(h.clock.now())
        row = h.store.get_credit(key)
        self.assertIsNotNone(row, "retention deleted a credit whose POST failed")
        self.redeem_now(h, key)
        self.assertEqual(h.backend.posts[-1]["redeem_request_id"], first)

    def test_a_redeemed_credit_is_still_pruned_after_retention(self):
        """The rule must not become "keep everything forever"."""
        h = Harness(self)
        account, key = h.armed(expires_in=HOUR)
        h.clock.set(h.store.get_slot(key, "primary")["fire_at"])
        h.run_fire()
        self.assertEqual(h.store.get_credit(key)["status"], "redeemed")
        h.advance(15 * 24 * HOUR)
        h.store.prune(h.clock.now())
        self.assertIsNone(h.store.get_credit(key), "a settled credit was never pruned")


class R10Exclusion(unittest.TestCase):
    """R10 — excluding an account does not revoke an existing fire."""

    def test_an_excluded_account_must_not_fire_a_preexisting_slot(self):
        """The reviewer's counterexample: exclusion by key, slot already armed."""
        h = Harness(self)
        account, key = h.armed(expires_in=HOUR)
        h.deps.settings = dataclasses.replace(h.settings, excluded_accounts=(account.account_key,))
        h.clock.set(h.store.get_slot(key, "primary")["fire_at"])
        h.run_fire()
        self.assertEqual(h.backend.posts, [], "an excluded account was spent")

    def test_a_label_exclusion_must_remove_already_scheduled_jobs(self):
        """The reviewer's counterexample: exclusion by label left the slots armed."""
        h = Harness(self)
        account, key = h.armed(expires_in=HOUR)
        h.deps.settings = dataclasses.replace(h.settings, excluded_accounts=(account.label,))
        h.scan()
        self.assertEqual(h.store.list_slots(credit_key=key), [], "the label exclusion was ignored")

    def test_a_label_exclusion_must_also_refuse_a_slot_that_still_fires(self):
        """Variant: the label exclusion, evaluated at the boundary instead of the scan."""
        h = Harness(self)
        account, key = h.armed(expires_in=HOUR)
        h.deps.settings = dataclasses.replace(h.settings, excluded_accounts=(account.label,))
        h.clock.set(h.store.get_slot(key, "primary")["fire_at"])
        h.run_fire()
        self.assertEqual(h.backend.posts, [], "a label exclusion did not reach the boundary")


class R15Quota(unittest.TestCase):
    """R15 — weekly-quota early redemption is wired to a nonexistent method."""

    def test_the_host_quota_check_must_call_the_real_planner(self):
        """The reviewer's counterexample: ``HostService`` looked for ``fire.run_quota_check``."""
        h = Harness(self)
        account, key = h.armed(expires_in=30 * HOUR)
        h.deps.settings = dataclasses.replace(
            h.settings, early_redeem_enabled=True, early_redeem_remaining_pct=100
        )
        h.backend.usage_body = {
            "rate_limit": {
                "secondary_window": {"limit_window_seconds": 604800, "used_percent": 100}
            }
        }
        h.deps.planner = h.planner()
        h.deps.fire = h.fire()
        HostService(h.deps).quota_check()
        self.assertEqual(len(h.backend.posts), 1, "the host quota path did nothing")

    def test_the_host_quota_check_must_not_spend_when_the_opt_in_is_off(self):
        """Variant: the same host path with early redemption disabled."""
        h = Harness(self)
        account, key = h.armed(expires_in=30 * HOUR)
        h.deps.settings = dataclasses.replace(
            h.settings, early_redeem_enabled=False, early_redeem_remaining_pct=100
        )
        h.backend.usage_body = {
            "rate_limit": {
                "secondary_window": {"limit_window_seconds": 604800, "used_percent": 100}
            }
        }
        h.deps.planner = h.planner()
        h.deps.fire = h.fire()
        HostService(h.deps).quota_check()
        self.assertEqual(h.backend.posts, [], "the quota path spent without the opt-in")


class R16Unmask(unittest.TestCase):
    """R16 — ``mask_labels=false`` unmasks the notice, in memory only.

    Round 2 moved the unmask from "resolve early, then serialize the result"
    to "store the masked label, resolve at send time". The tests below were
    rewritten with it: the parameter builder now *reduces* a label to the
    canonical masked one, and the notifier's ``resolve_label`` is what turns it
    back into the full address while rendering.
    """

    def test_the_unmask_setting_must_resolve_the_full_live_label(self):
        """The reviewer's counterexample: a masked pool label stayed masked."""
        full = "review.account@example.invalid"
        h = Harness(self)
        account, key = h.armed()
        h.deps.settings = dataclasses.replace(h.settings, mask_labels=False)

        class Source:
            def accounts(self):
                return [
                    AccountRef(account.account_key, mask_label(full), "pool", "synthetic-entry")
                ]

            def full_label(self, account_key):
                return full

        h.deps.source = Source()
        params = notice_params(
            "success",
            label=account_label(h.deps, h.store, account.account_key, h.deps.source.accounts()[0]),
            redeemed_at=h.clock.now(),
            expires_at=h.clock.now() + HOUR,
        )
        sender = Notifier(
            None,
            mask_labels=False,
            resolve_label=lambda masked: full if masked == mask_label(full) else "",
        )
        self.assertIn(full, sender.text("success", params))

    def test_the_masked_label_is_what_the_store_keeps(self):
        """Variant: unmasking is in memory at send time, never on disk."""
        full = "review.account@example.invalid"
        h = Harness(self)
        account, key = h.armed()
        h.deps.settings = dataclasses.replace(h.settings, mask_labels=False)

        class Source:
            def accounts(self):
                return [
                    AccountRef(account.account_key, mask_label(full), "pool", "synthetic-entry")
                ]

            def full_label(self, account_key):
                return full

        h.deps.source = Source()
        h.scan()
        stored = h.store.get_account(account.account_key) or {}
        self.assertNotIn(full, json.dumps(stored), "the full address reached SQLite")
        params = notice_params(
            "success",
            label=account_label(h.deps, h.store, account.account_key, h.deps.source.accounts()[0]),
            redeemed_at=h.clock.now(),
            expires_at=h.clock.now() + HOUR,
        )
        self.assertEqual(
            params["label"], mask_label(full), "the parameters kept a presentation label"
        )
        self.assertNotIn(full, json.dumps(params), "the full address entered the outbox payload")
        sender = Notifier(
            None,
            mask_labels=False,
            resolve_label=lambda masked: full if masked == mask_label(full) else "",
        )
        self.assertIn(full, sender.text("success", params))


if __name__ == "__main__":
    unittest.main(verbosity=2)
