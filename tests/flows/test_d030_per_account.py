"""D-030 planner flows: per-account jobs follow the effective settings.

* an account with automatic redemption off gets no fire or backup one-shot;
* its existing one-shots are removed on the next scan and come back when it is
  turned on again;
* ``reminders_enabled: false`` removes only that account's reminders;
* a paused account (``monitored: false``) loses its jobs and is not scanned;
* an account with early redemption on arms the quota check while the global
  switch is off;
* every other account keeps the global behaviour throughout.
"""

from __future__ import annotations

import unittest

from reset_rescue.settings import parse_settings
from tests.flows.support import HOUR, Harness

LABEL_A = "b***@e***.com"
LABEL_B = "c***@f***.com"


class TwoAccounts(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness(self)
        self.a = self.h.add_account(LABEL_A)
        self.b = self.h.add_account(LABEL_B)
        self.h.add_credit(self.a, expires_in=30 * HOUR)
        self.h.add_credit(self.b, expires_in=30 * HOUR)
        self.key_a = self.h.key(self.a)
        self.key_b = self.h.key(self.b)

    def use(self, raw: dict) -> None:
        self.h.deps.settings = parse_settings(raw)

    def override(self, account, **fields) -> dict:
        return {"account_overrides": {account.account_key: {"label": account.label, **fields}}}

    def scan(self):
        result = self.h.scan()
        self.assertEqual(result.exit_code, 0, result)
        return result

    def roles(self, key: str) -> set[str]:
        return {str(slot["role"]) for slot in self.h.store.list_slots(credit_key=key)}

    def job_id(self, key: str, role: str):
        slot = self.h.store.get_slot(key, role)
        return None if slot is None else slot.get("job_id")


class AutomaticRedemption(TwoAccounts):
    def test_an_account_with_auto_off_gets_no_fire_or_backup_slot(self):
        self.use(self.override(self.a, auto_redeem_enabled=False))
        self.scan()
        self.assertFalse({"primary", "backup"} & self.roles(self.key_a))
        self.assertTrue({"primary", "backup"} <= self.roles(self.key_b))

    def test_turning_auto_off_removes_the_existing_jobs_on_the_next_scan(self):
        self.scan()
        job = self.job_id(self.key_a, "primary")
        self.assertIsNotNone(job)
        self.assertIn(job, self.h.cron.jobs)
        b_before = self.roles(self.key_b)

        self.use(self.override(self.a, auto_redeem_enabled=False))
        self.scan()

        self.assertNotIn("primary", self.roles(self.key_a))
        self.assertNotIn(job, self.h.cron.jobs)
        self.assertEqual(self.roles(self.key_b), b_before)

    def test_turning_auto_back_on_rearms_on_the_next_scan(self):
        self.use(self.override(self.a, auto_redeem_enabled=False))
        self.scan()
        self.assertNotIn("primary", self.roles(self.key_a))

        self.use({})
        self.scan()

        job = self.job_id(self.key_a, "primary")
        self.assertIsNotNone(job)
        self.assertIn(job, self.h.cron.jobs)


class Reminders(TwoAccounts):
    def test_reminders_disabled_for_one_account_only(self):
        self.use(self.override(self.a, reminders_enabled=False))
        self.scan()
        self.assertFalse({"reminder1", "reminder2"} & self.roles(self.key_a))
        self.assertIn("reminder1", self.roles(self.key_b))


class Paused(TwoAccounts):
    def test_a_paused_account_loses_its_jobs_and_is_not_counted(self):
        self.scan()
        job = self.job_id(self.key_a, "primary")
        self.assertIn(job, self.h.cron.jobs)

        self.use(self.override(self.a, monitored=False))
        result = self.scan()

        self.assertEqual(self.h.store.list_slots(credit_key=self.key_a), [])
        self.assertNotIn(job, self.h.cron.jobs)
        self.assertEqual(result.accounts, 1)
        self.assertIn("primary", self.roles(self.key_b))


class EarlyRedemption(TwoAccounts):
    def usage(self, used_pct: float) -> None:
        self.h.backend.usage_body = {
            "rate_limit": {
                "primary_window": {"limit_window_seconds": 604800, "used_percent": used_pct},
                "secondary_window": {"limit_window_seconds": 18000, "used_percent": 0.0},
            }
        }

    def test_early_on_for_one_account_arms_the_quota_check_while_global_is_off(self):
        self.use(self.override(self.a, early_redeem_enabled=True, early_redeem_remaining_pct=0))
        self.scan()
        self.usage(100.0)  # weekly remaining 0 %: at the threshold of 0

        planner = self.h.planner()
        self.assertTrue(planner.quota_armed())
        result = planner.quota_check()

        self.assertEqual(result.exit_code, 0, result)
        self.assertEqual(len(self.h.backend.posts), 1, self.h.backend.posts)
