"""QA lifecycle checks for S14: a ticker that is down across a fire.

The one-shot cron grace is a fixed 120 s (reset_rescue/settings.py:67). A slot
that fires late but inside the grace must redeem exactly once. A slot whose
credit expired while the ticker was down must never post and must never raise a
consume-failure notice (retry_pending / final_failure). Both run the real Planner
and Fire against the in-memory Harness backend, which never reaches a live
service.
"""

from __future__ import annotations

import unittest

from tests.flows.support import HOUR, MINUTE, Harness

#: One-shot cron grace, in seconds (reset_rescue/settings.py:67).
CRON_GRACE_S = 120
#: Notice kinds that would mean the user was told a consume failed.
FAILURE_KINDS = frozenset({"retry_pending", "final_failure", "account_error"})


class _Lifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness(self)

    def _arm(self, *, expires_in: float):
        account = self.h.add_account()
        credit = self.h.add_credit(account, expires_in=expires_in)
        self.assertEqual(self.h.scan().exit_code, 0)
        return account, credit

    def _planner(self):
        from reset_rescue.planner import Planner

        return Planner(self.h.deps, self.h.store)

    def _fire(self):
        from reset_rescue.fire import Fire

        return Fire(self.h.deps, self.h.store)

    def _status(self, account) -> str:
        row = self.h.row(account)
        if row is None:
            self.fail("the credit row is missing")
        return str(row["status"])

    def _at_slot(self, account, role: str, *, early: float = 0.0) -> None:
        slot = self.h.slot(account, role)
        self.assertIsNotNone(slot, f"no {role} slot was scheduled")
        self.h.advance_to(float(slot["fire_at"]) + early)


class S14TickerDownWithinGrace(_Lifecycle):
    """The ticker is late by less than the cron grace: one redemption, no more."""

    def test_a_primary_late_inside_the_grace_posts_exactly_once(self):
        account, _ = self._arm(expires_in=30 * HOUR)
        self._at_slot(account, "primary", early=CRON_GRACE_S - 10)
        result = self._fire().run("primary")
        self.assertEqual(result.exit_code, 0, result)
        self.assertEqual(len(self.h.backend.posts), 1)
        self.assertEqual(self._status(account), "redeemed")

    def test_the_backup_and_a_rerun_after_a_late_primary_do_not_post_again(self):
        account, _ = self._arm(expires_in=30 * HOUR)
        self._at_slot(account, "primary", early=CRON_GRACE_S - 10)
        self._fire().run("primary")
        self._at_slot(account, "backup")
        self._fire().run("backup")
        self._fire().run("primary")
        self.assertEqual(len(self.h.backend.posts), 1, "a late ticker caused a second consume")

    def test_a_late_primary_announces_one_success(self):
        account, _ = self._arm(expires_in=30 * HOUR)
        self._at_slot(account, "primary", early=CRON_GRACE_S - 10)
        self._fire().run("primary")
        self._at_slot(account, "backup")
        self._fire().run("backup")
        self.assertEqual(len(self.h.notices("success")), 1)
        self.assertEqual(len(self.h.notices("final_failure")), 0)


class S14TickerDownAcrossExpiry(_Lifecycle):
    """The ticker is down past expiry: no POST, no failure notice, one missed."""

    def test_an_expired_credit_is_never_posted(self):
        account, credit = self._arm(expires_in=30 * MINUTE)
        self.h.advance_to(credit.expires_at + MINUTE)
        self._fire().run("primary")
        self._fire().run("backup")
        self._planner().scan()
        self.assertEqual(self.h.backend.posts, [], "an expired credit was posted")

    def test_an_expired_credit_raises_no_consume_failure_notice(self):
        account, credit = self._arm(expires_in=30 * MINUTE)
        self.h.advance_to(credit.expires_at + MINUTE)
        self._fire().run("primary")
        self._fire().run("backup")
        self._planner().scan()
        kinds = {str(notice["kind"]) for notice in self.h.notices()}
        self.assertFalse(
            kinds & FAILURE_KINDS,
            f"a consume failure was announced for an expired credit: {sorted(kinds)}",
        )

    def test_an_expired_credit_is_reported_missed_once(self):
        account, credit = self._arm(expires_in=30 * MINUTE)
        self.h.advance_to(credit.expires_at + 16 * MINUTE)
        self._planner().scan()
        self._planner().scan()
        self.assertEqual(self._status(account), "missed")
        self.assertEqual(len(self.h.notices("missed")), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
