"""One scan, one attempt per credit (I9): the paths must not double-spend.

A scan has three ways to reach a redemption: the late path in ``_absorb``, the
reconcile step's ``redeeming`` completion, and the D-009 early trigger. They
must never POST the same credit twice inside one run — a transient failure in
the first path would otherwise be retried by the second, spending a second
attempt the user did not ask for.
"""

from __future__ import annotations

import unittest

from reset_rescue.settings import parse_settings
from tests.flows.support import HOUR, MINUTE, Harness


class SingleAttemptPerScan(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness(self)
        self.h.deps.settings = parse_settings(
            {
                "early_redeem_enabled": True,
                "early_redeem_weekly_remaining_pct": 0,
                "lead_minutes": 10,
                "backup_lead_minutes": 4,
            }
        )
        self.account = self.h.add_account()
        self.credit = self.h.add_credit(self.account, expires_in=30 * HOUR)
        self.h.backend.usage_body = {
            "rate_limit": {
                "primary_window": {"limit_window_seconds": 604800, "used_percent": 100.0}
            }
        }

    def test_a_transient_failure_in_the_late_path_is_not_retried_by_the_trigger(self):
        # First scan schedules; then the slots are gone and the credit is late.
        self.h.scan()
        for name in list(self.h.job_names()):
            job = self.h.job_for(name)
            self.h.cron.jobs.pop(job["id"])
        self.h.advance_to(self.credit.expires_at - 5 * MINUTE)
        self.h.backend.on_post = lambda *_: {"code": "rate_limit_reset_ineligible"}
        self.h.scan()
        self.assertEqual(
            len(self.h.backend.posts), 1, "the same credit was posted twice in one scan"
        )

    def test_a_successful_late_path_leaves_nothing_for_the_trigger(self):
        self.h.scan()
        for name in list(self.h.job_names()):
            job = self.h.job_for(name)
            self.h.cron.jobs.pop(job["id"])
        self.h.advance_to(self.credit.expires_at - 5 * MINUTE)
        self.h.scan()
        self.assertEqual(len(self.h.backend.posts), 1)
        self.assertEqual(self.h.row(self.account)["status"], "redeemed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
