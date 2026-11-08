"""T-73 — the conditional 15-minute quota check (D-009).

A separate flow (``Planner.quota_check``) rather than a branch of the scan:

* **armed** only when ``early_redeem_enabled`` is on *and* at least one credit
  is still unsettled — otherwise it does nothing and reads nothing;
* reads ``/wham/usage`` only for accounts that actually hold a banked credit;
* redeems at most the soonest-expiring available credit of an account, through
  ``redeem_one(role='early')``: same persisted UUID, same readback, same notices;
* a per-account 15-minute cooldown, so a 15-minute job cannot hammer the API or
  spend twice for the same exhausted quota;
* never two credits of one account at once, never inside the normal lead window.
"""

from __future__ import annotations

import unittest

from reset_rescue.planner import QUOTA_COOLDOWN_S, QUOTA_INTERVAL_S
from reset_rescue.settings import parse_settings
from tests.flows.support import HOUR, MINUTE, Harness


def early_settings(
    *, enabled: bool = True, pct: int = 0, lead_minutes: int = 10, backup_lead_minutes: int = 4
):
    return parse_settings(
        {
            "early_redeem_enabled": enabled,
            "early_redeem_weekly_remaining_pct": pct,
            "lead_minutes": lead_minutes,
            "backup_lead_minutes": backup_lead_minutes,
        }
    )


class QuotaCase(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness(self)
        self.h.deps.settings = early_settings()
        self.account = self.h.add_account()
        self.credit = self.h.add_credit(self.account, expires_in=30 * HOUR)
        self.assertEqual(self.h.scan().exit_code, 0)
        self.planner = self.h.planner()

    def usage(self, used_pct: float) -> None:
        self.h.backend.usage_body = {
            "rate_limit": {
                "primary_window": {"limit_window_seconds": 604800, "used_percent": used_pct},
                "secondary_window": {"limit_window_seconds": 18000, "used_percent": 0.0},
            }
        }

    def check(self):
        return self.planner.quota_check()

    def posts(self) -> list[dict]:
        return self.h.backend.posts


class Arming(QuotaCase):
    def test_the_interval_is_fifteen_minutes(self):
        self.assertEqual(QUOTA_INTERVAL_S, 15 * MINUTE)
        self.assertEqual(QUOTA_COOLDOWN_S, 15 * MINUTE)

    def test_disabled_is_not_armed(self):
        self.h.deps.settings = early_settings(enabled=False)
        self.assertFalse(self.planner.quota_armed())
        result = self.check()
        self.assertEqual(result.problem_code, "not_armed")
        self.assertEqual(self.h.backend.usages, [], "a disabled check read usage")

    def test_no_unsettled_credit_is_not_armed(self):
        self.h.store.settle_credit(
            self.h.key(self.account), "redeemed", "reset", self.h.clock.now()
        )
        self.assertFalse(self.planner.quota_armed())
        self.assertEqual(self.check().problem_code, "not_armed")
        self.assertEqual(self.h.backend.usages, [])

    def test_an_enabled_check_with_a_banked_credit_is_armed(self):
        self.assertTrue(self.planner.quota_armed())

    def test_it_never_reads_usage_for_an_account_without_credits(self):
        other = self.h.add_account("o***@e***.com")
        self.usage(100.0)
        self.check()
        self.assertNotIn(other.account_key, self.h.backend.usages)


class Trigger(QuotaCase):
    def test_an_exhausted_quota_redeems_the_soonest_credit(self):
        self.usage(100.0)
        result = self.check()
        self.assertEqual(result.exit_code, 0, result)
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.posts()[0]["credit_id"], self.credit.id)
        self.assertEqual(self.h.row(self.account)["status"], "redeemed")

    def test_a_healthy_quota_does_nothing(self):
        self.usage(40.0)
        self.check()
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.h.row(self.account)["status"], "scheduled")

    def test_the_threshold_is_inclusive(self):
        self.h.deps.settings = early_settings(pct=10)
        self.usage(90.0)
        self.check()
        self.assertEqual(len(self.posts()), 1)

    def test_just_above_the_threshold_does_nothing(self):
        self.h.deps.settings = early_settings(pct=10)
        self.usage(89.0)
        self.check()
        self.assertEqual(self.posts(), [])

    def test_an_unknown_usage_never_triggers(self):
        self.h.backend.usage_body = {"rate_limit": {}}
        self.check()
        self.assertEqual(self.posts(), [])

    def test_a_failing_usage_read_never_breaks_the_check(self):
        def boom(account_key):
            raise ConnectionError("usage down")

        self.h.backend.on_usage = boom
        result = self.check()
        self.assertEqual(result.exit_code, 0, result)
        self.assertEqual(self.posts(), [])

    def test_only_the_soonest_expiring_credit_is_redeemed(self):
        self.h.add_credit(self.account, "credit-later", expires_in=40 * HOUR)
        self.h.scan()
        self.usage(100.0)
        self.check()
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.posts()[0]["credit_id"], self.credit.id)

    def test_never_two_at_once(self):
        self.h.add_credit(self.account, "credit-second", expires_in=31 * HOUR)
        self.h.scan()
        self.usage(100.0)
        self.check()
        self.assertEqual(len(self.posts()), 1, "two credits were spent in one check")

    def test_a_credit_already_in_flight_blocks_the_check(self):
        self.h.add_credit(self.account, "credit-second", expires_in=31 * HOUR)
        self.h.scan()
        key = self.h.key(self.account)
        self.h.store.begin_redeem(key, self.h.clock.now())
        self.h.store.update_credit(key, status="redeeming", posted_at=self.h.clock.now())
        self.usage(100.0)
        self.check()
        self.assertEqual(self.posts(), [], "a credit was spent while one was in flight")

    def test_it_does_not_fire_inside_the_normal_lead_window(self):
        self.usage(100.0)
        self.h.advance_to(self.credit.expires_at - 5 * MINUTE)
        self.check()
        self.assertEqual(self.posts(), [], "the check raced the slot path")

    def test_a_settled_credit_is_not_picked(self):
        self.h.store.settle_credit(
            self.h.key(self.account), "redeemed", "reset", self.h.clock.now()
        )
        self.h.backend.only(self.account.account_key).status = "redeemed"
        self.h.backend.only(self.account.account_key).redeemed_at = self.h.clock.now()
        self.usage(100.0)
        self.check()
        self.assertEqual(self.posts(), [])


class Cooldown(QuotaCase):
    def test_a_second_check_inside_the_cooldown_reads_nothing(self):
        self.usage(40.0)
        self.check()
        reads = len(self.h.backend.usages)
        self.h.advance(5 * MINUTE)
        self.check()
        self.assertEqual(len(self.h.backend.usages), reads, "the cooldown was ignored")

    def test_after_the_cooldown_it_reads_again(self):
        self.usage(40.0)
        self.check()
        reads = len(self.h.backend.usages)
        self.h.advance(QUOTA_COOLDOWN_S + MINUTE)
        self.check()
        self.assertEqual(len(self.h.backend.usages), reads + 1)

    def test_the_cooldown_is_per_account(self):
        other = self.h.add_account("o***@e***.com")
        self.h.add_credit(other, "credit-other", expires_in=32 * HOUR)
        self.h.scan()
        self.usage(40.0)
        self.check()
        self.h.advance(5 * MINUTE)
        self.check()
        # Both accounts were read exactly once: the cooldown is per account.
        self.assertEqual(len(self.h.backend.usages), 2)

    def test_a_failed_attempt_still_sets_the_cooldown(self):
        self.h.backend.on_post = lambda *_: {"code": "http_error"}
        self.usage(100.0)
        self.check()
        posts = len(self.posts())
        self.h.advance(5 * MINUTE)
        self.check()
        self.assertEqual(len(self.posts()), posts, "the cooldown did not cover a failure")


class SamePath(QuotaCase):
    def test_it_uses_the_persisted_uuid(self):
        request_id = self.h.store.ensure_request_id(self.h.key(self.account))
        self.usage(100.0)
        self.check()
        self.assertEqual(self.posts()[0]["redeem_request_id"], request_id)
        self.assertEqual(self.h.row(self.account)["redeem_request_id"], request_id)

    def test_it_requires_a_readback(self):
        self.h.backend.on_post = lambda *_: {"code": "reset", "windows_reset": 1}
        self.usage(100.0)
        self.check()
        self.assertNotEqual(self.h.row(self.account)["status"], "redeemed")

    def test_it_announces_one_success(self):
        self.usage(100.0)
        self.check()
        successes = [notice for notice in self.h.notices() if notice["kind"] == "success"]
        self.assertEqual(len(successes), 1)

    def test_a_second_check_after_the_redeem_is_silent(self):
        self.usage(100.0)
        self.check()
        posts = len(self.posts())
        self.h.advance(QUOTA_COOLDOWN_S + MINUTE)
        self.check()
        self.assertEqual(len(self.posts()), posts)

    def test_an_excluded_account_is_never_checked(self):
        self.h.deps.settings = early_settings().with_included(["0" * 16])
        self.usage(100.0)
        self.check()
        self.assertEqual(self.posts(), [])

    def test_it_never_writes_a_quota_percentage(self):
        """D-010: no quota figure is persisted or announced."""
        self.usage(100.0)
        self.check()
        for row in self.h.store.list_notices():
            self.assertNotIn("pct", row["params_json"].lower())
            self.assertNotIn("percent", row["params_json"].lower())
        for row in self.h.store.list_history():
            self.assertNotIn("pct", str(row).lower())


class PerAccount(QuotaCase):
    def test_each_account_gets_its_own_decision(self):
        other = self.h.add_account("o***@e***.com")
        self.h.add_credit(other, "credit-other", expires_in=32 * HOUR)
        self.h.scan()
        self.usage(100.0)
        self.check()
        self.assertEqual(len(self.posts()), 2)
        self.assertEqual(
            {post["account_key"] for post in self.posts()},
            {self.account.account_key, other.account_key},
        )

    def test_an_account_with_a_healthy_quota_is_left_alone(self):
        other = self.h.add_account("o***@e***.com")
        self.h.add_credit(other, "credit-other", expires_in=32 * HOUR)
        self.h.scan()
        self.h.backend.on_usage = lambda account_key: (
            {
                "rate_limit": {
                    "primary_window": {"limit_window_seconds": 604800, "used_percent": 10.0}
                }
            }
            if account_key == other.account_key
            else None
        )
        self.usage(100.0)
        self.check()
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.posts()[0]["account_key"], self.account.account_key)


if __name__ == "__main__":
    unittest.main(verbosity=2)
