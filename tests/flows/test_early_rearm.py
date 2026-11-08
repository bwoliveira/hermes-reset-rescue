"""Early-redeem re-arm guard (D-009): one early spend per exhausted weekly window.

After an early redeem for an account, that account is not early-redeemed again
until a fresh, readable usage reading shows its weekly remaining above its
threshold. A stale, unchanged or unreadable reading never spends a second
credit. Lead-time auto-redeem and Redeem now are not affected.
"""

from __future__ import annotations

import unittest

from reset_rescue.planner import QUOTA_COOLDOWN_S
from reset_rescue.settings import parse_settings
from tests.flows.support import HOUR, MINUTE, Harness

THRESHOLD = 20
REARM_KEY = "early_rearm:{}"


def weekly_usage(remaining_pct: float) -> dict:
    return {
        "rate_limit": {
            "primary_window": {
                "limit_window_seconds": 604800,
                "used_percent": 100.0 - remaining_pct,
            },
            "secondary_window": {"limit_window_seconds": 18000, "used_percent": 0.0},
        }
    }


class RearmCase(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness(self)
        self.h.deps.settings = parse_settings(
            {
                "early_redeem_enabled": True,
                "early_redeem_weekly_remaining_pct": THRESHOLD,
            }
        )
        self.a = self.h.add_account("b***@e***.com")
        self.b = self.h.add_account("c***@f***.com")
        self.h.add_credit(self.a, "credit-a1", expires_in=30 * HOUR)
        self.h.add_credit(self.a, "credit-a2", expires_in=40 * HOUR)
        self.h.add_credit(self.b, "credit-b1", expires_in=31 * HOUR)
        result = self.h.scan()
        self.assertEqual(result.exit_code, 0, result)
        # account_key -> remaining percent (float), a usage body (dict), or an exception.
        self.readings: dict = {self.a.account_key: 15.0, self.b.account_key: 15.0}
        self.h.backend.on_usage = self._usage

    def _usage(self, account_key):
        value = self.readings.get(account_key)
        if value is None:
            return None  # unlisted: the harness default body
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, dict):
            return value
        return weekly_usage(float(value))

    def set_reading(self, account, value) -> None:
        self.readings[account.account_key] = value

    def check(self):
        return self.h.planner().quota_check()

    def posts_for(self, account) -> list[dict]:
        return [p for p in self.h.backend.posts if p["account_key"] == account.account_key]

    def rearm_meta(self, account):
        return self.h.store.get_meta(REARM_KEY.format(account.account_key))

    def status(self, credit_id: str) -> str:
        row = self.h.row(self.a, credit_id)
        assert row is not None, credit_id
        return str(row["status"])

    def spent(self, account) -> bool:
        return self.rearm_meta(account) == "spent"

    def advance_past_cooldown(self) -> None:
        self.h.advance(QUOTA_COOLDOWN_S + MINUTE)


class FirstSpend(RearmCase):
    def test_fifteen_percent_spends_one_credit(self):
        result = self.check()
        self.assertEqual(result.exit_code, 0, result)
        self.assertEqual(len(self.posts_for(self.a)), 1)
        self.assertEqual(self.posts_for(self.a)[0]["credit_id"], "credit-a1")
        self.assertTrue(self.spent(self.a), "a successful early redeem did not arm the guard")


class StillExhausted(RearmCase):
    def test_still_fifteen_after_cooldown_spends_no_second_credit(self):
        self.check()
        self.advance_past_cooldown()
        self.check()  # reading still 15%: the window did not refill
        self.assertEqual(
            len(self.posts_for(self.a)),
            1,
            "a second credit was spent on an unchanged exhausted reading",
        )
        self.assertEqual(self.status("credit-a2"), "scheduled")

    def test_the_guard_survives_many_unchanged_checks(self):
        self.check()
        for _ in range(3):
            self.advance_past_cooldown()
            self.check()
        self.assertEqual(len(self.posts_for(self.a)), 1)


class Rearm(RearmCase):
    def test_fresh_high_reading_rearms_then_low_reading_spends_once_more(self):
        self.check()
        self.advance_past_cooldown()
        self.set_reading(self.a, 98.0)
        self.check()
        self.assertFalse(self.spent(self.a), "a reading of 98% did not re-arm the account")
        self.assertEqual(len(self.posts_for(self.a)), 1, "a healthy reading spent a credit")

        self.advance_past_cooldown()
        self.set_reading(self.a, 15.0)
        self.check()
        posts = self.posts_for(self.a)
        self.assertEqual(len(posts), 2, posts)
        self.assertEqual(posts[1]["credit_id"], "credit-a2")
        self.assertTrue(self.spent(self.a))

    def test_a_reading_at_the_threshold_does_not_rearm(self):
        self.check()
        self.advance_past_cooldown()
        self.set_reading(self.a, float(THRESHOLD))
        self.check()
        self.assertTrue(self.spent(self.a), "re-armed at a reading equal to the threshold")
        self.assertEqual(len(self.posts_for(self.a)), 1)


class UnknownReading(RearmCase):
    def test_unknown_body_after_spend_keeps_it_spent(self):
        self.check()
        self.advance_past_cooldown()
        self.set_reading(self.a, {"rate_limit": {}})
        self.check()
        self.assertTrue(self.spent(self.a), "an unknown reading re-armed the account")
        self.assertEqual(len(self.posts_for(self.a)), 1)

    def test_failed_usage_read_after_spend_keeps_it_spent(self):
        self.check()
        self.advance_past_cooldown()
        self.set_reading(self.a, ConnectionError("usage down"))
        result = self.check()
        self.assertEqual(result.exit_code, 0, result)
        self.assertTrue(self.spent(self.a))
        self.assertEqual(len(self.posts_for(self.a)), 1)

    def test_unknown_then_low_still_does_not_spend(self):
        self.check()
        self.advance_past_cooldown()
        self.set_reading(self.a, {"rate_limit": {}})
        self.check()
        self.advance_past_cooldown()
        self.set_reading(self.a, 15.0)
        self.check()
        self.assertEqual(len(self.posts_for(self.a)), 1, "an unknown reading re-armed the account")


class Unconfirmed(RearmCase):
    def test_interrupted_consume_persists_spent_before_the_post(self):
        def interrupted_post(account_key, credit_id, _request_id):
            # The backend accepted the credit, but the caller dies before its
            # response/readback can return. Recovery must not spend credit-a2.
            credit = self.h.backend.find(account_key, credit_id)
            assert credit is not None
            credit.status = "redeemed"
            raise SystemExit("interrupted after send")

        self.h.backend.on_post = interrupted_post
        with self.assertRaises(SystemExit):
            self.check()
        self.assertTrue(self.spent(self.a), "the in-flight spend was not persisted")
        self.h.backend.on_post = None
        self.h.scan()  # reconcile the accepted credit after the interrupted run
        self.advance_past_cooldown()
        self.check()
        self.assertEqual(len(self.posts_for(self.a)), 1)

    def test_an_unconfirmed_spend_stays_spent(self):
        # The POST returns without a readback: the outcome is unknown, not "not spent".
        self.h.backend.on_post = lambda *_: {"code": "reset", "windows_reset": 1}
        self.check()
        self.assertTrue(self.spent(self.a), "an unknown spend did not keep the guard armed")
        self.h.backend.on_post = None
        self.advance_past_cooldown()
        self.check()
        self.assertEqual(
            len(self.posts_for(self.a)),
            1,
            "a second credit was spent after an unconfirmed spend",
        )


class PerAccount(RearmCase):
    def test_accounts_are_independent(self):
        self.set_reading(self.b, 98.0)
        self.check()
        self.assertEqual(len(self.posts_for(self.a)), 1)
        self.assertEqual(self.posts_for(self.b), [], "a's spend armed or blocked b")
        self.assertTrue(self.spent(self.a))
        self.assertFalse(self.spent(self.b))

        self.advance_past_cooldown()
        self.check()  # a still 15%, b still 98%
        self.assertEqual(len(self.posts_for(self.a)), 1)
        self.assertEqual(self.posts_for(self.b), [])

        self.advance_past_cooldown()
        self.set_reading(self.b, 15.0)
        self.check()
        self.assertEqual(len(self.posts_for(self.b)), 1, "b was blocked by a's guard")
        self.assertEqual(len(self.posts_for(self.a)), 1)
        self.assertTrue(self.spent(self.b))

    def test_a_rearm_on_one_account_does_not_rearm_the_other(self):
        self.check()  # a and b both at 15%: both spend once
        self.assertEqual(len(self.posts_for(self.a)), 1)
        self.assertEqual(len(self.posts_for(self.b)), 1)
        self.advance_past_cooldown()
        self.set_reading(self.a, 98.0)
        self.check()
        self.assertFalse(self.spent(self.a))
        self.assertTrue(self.spent(self.b))


class ManualUnaffected(RearmCase):
    def test_confirmed_redeem_now_still_spends_while_early_is_spent(self):
        self.check()
        self.assertTrue(self.spent(self.a))
        outcome = self.h.fire().redeem_now(self.h.key(self.a, "credit-a2"), confirm=True)
        self.assertEqual(outcome.exit_code, 0, outcome)
        self.assertTrue(outcome.results[0].success, outcome)
        self.assertEqual(len(self.posts_for(self.a)), 2)
        self.assertTrue(self.spent(self.a), "manual redemption re-armed early redemption")


class LeadTimeUnaffected(RearmCase):
    def test_the_scheduled_lead_time_fire_still_redeems_after_an_early_spend(self):
        self.check()  # early spend of credit-a1
        self.assertTrue(self.spent(self.a))
        slot = self.h.slot(self.a, "primary", "credit-a2")
        self.assertIsNotNone(slot, "credit-a2 has no scheduled primary slot")
        self.h.advance_to(float(slot["fire_at"]))
        self.h.run_fire("primary")
        ids = [p["credit_id"] for p in self.posts_for(self.a)]
        self.assertIn("credit-a2", ids, "the early guard blocked the lead-time redeem")


class NoPostPaths(RearmCase):
    """A refused or dry-run attempt makes no POST, so it must not latch the guard."""

    def _set_settings(self, **changes) -> None:
        from dataclasses import replace

        current = self.h.deps.settings
        assert current is not None
        self.h.deps.settings = replace(current, **changes)

    def test_auto_redeem_off_check_does_not_latch_the_guard(self):
        self._set_settings(auto_redeem_enabled=False)
        self.check()
        self.assertEqual(self.h.backend.posts, [], "auto redeem Off still POSTed")
        self.assertIsNone(self.rearm_meta(self.a), "a refused check latched the guard")
        self.assertFalse(self.spent(self.a))

        self._set_settings(auto_redeem_enabled=True)
        self.advance_past_cooldown()
        self.check()
        self.assertEqual(
            [p["credit_id"] for p in self.posts_for(self.a)],
            ["credit-a1"],
            "re-enabling auto redeem with the same reading did not spend the soonest credit",
        )

    def test_dry_run_check_does_not_latch_the_guard(self):
        self._set_settings(dry_run=True)
        self.check()
        self.assertEqual(self.h.backend.posts, [], "dry run On still POSTed")
        self.assertFalse(self.spent(self.a), "a dry-run check latched the guard")

        self._set_settings(dry_run=False)
        self.advance_past_cooldown()
        self.check()
        self.assertEqual(
            [p["credit_id"] for p in self.posts_for(self.a)],
            ["credit-a1"],
            "leaving dry run did not spend the soonest credit",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
