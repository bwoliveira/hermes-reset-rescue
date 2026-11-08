"""T-72 — reminder one-shots: ``fire --role remind`` over a read-only transport.

Two reminder one-shots per credit (``remind_1`` at 24 h, ``remind_2`` at 1 h by
default, each configurable or off), planned by the scan and fired by
``Fire.run(role='remind')``. The role is a job, not a time (I3), and the run is
physically unable to POST: it reads through ``ReadOnlyTransport`` (I5-lite).

What these tests pin:

* the scan arms one one-shot per enabled reminder, at ``expires_at - N hours``,
  and never arms one whose time has already passed;
* ``0`` disables a reminder and no job is created for it;
* the remind run sends exactly one notice per ``(credit, role)``, never twice,
  and never for a settled credit;
* a remind run never POSTs, never changes a credit row and never removes a job
  that could still run.
"""

from __future__ import annotations

import unittest

from reset_rescue.codex_api import MutationRefused
from reset_rescue.fire import REMINDER_ROLES, ROLE_REMIND, Fire
from reset_rescue.settings import parse_settings
from tests.flows.support import HOUR, MINUTE, Harness


def settings_with(reminder_1: int = 24, reminder_2: int = 1, **overrides):
    raw = {"reminder_1_hours": reminder_1, "reminder_2_hours": reminder_2}
    raw.update(overrides)
    return parse_settings(raw)


class ReminderPlanCase(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness(self)
        self.h.deps.settings = settings_with()
        self.account = self.h.add_account()
        self.credit = self.h.add_credit(self.account, expires_in=30 * HOUR)
        self.assertEqual(self.h.scan().exit_code, 0)
        self.key = self.h.key(self.account)

    def jobs(self, role: str) -> list[dict]:
        return [job for job in self.h.cron.jobs.values() if f"remind-{role[-1]}" in job["name"]]

    def reminder_jobs(self) -> list[dict]:
        return [job for job in self.h.cron.jobs.values() if "remind-" in job["name"]]

    def run_remind(self):
        return self.h.fire().run(role=ROLE_REMIND)

    def reminders(self) -> list[dict]:
        return [notice for notice in self.h.notices() if notice["kind"] == "reminder"]


class Planning(ReminderPlanCase):
    def test_one_one_shot_per_enabled_reminder(self):
        jobs = self.reminder_jobs()
        self.assertEqual(len(jobs), 2, [job["name"] for job in jobs])
        names = sorted(job["name"] for job in jobs)
        self.assertEqual(
            names,
            [f"reset-rescue-remind-1-{self.key[:8]}", f"reset-rescue-remind-2-{self.key[:8]}"],
        )

    def test_each_one_shot_sits_at_expiry_minus_its_offset(self):
        for role, hours in (("reminder1", 24), ("reminder2", 1)):
            slot = self.h.slot(self.account, role)
            self.assertIsNotNone(slot, role)
            self.assertEqual(slot["fire_at"], self.credit.expires_at - hours * HOUR)

    def test_reminder_jobs_use_the_remind_wrapper(self):
        for job in self.reminder_jobs():
            self.assertEqual(job["script"], "reset-rescue-remind.py")

    def test_zero_disables_a_reminder_and_creates_no_job(self):
        self.h.deps.settings = settings_with(reminder_1=24, reminder_2=0)
        self.h.scan()
        self.assertEqual(len(self.reminder_jobs()), 1)
        self.assertIsNone(self.h.slot(self.account, "reminder2"))

    def test_both_off_creates_no_reminder_job(self):
        self.h.deps.settings = settings_with(reminder_1=0, reminder_2=0)
        self.h.scan()
        self.assertEqual(self.reminder_jobs(), [])

    def test_a_reminder_already_in_the_past_is_not_armed(self):
        """A late reminder is the scan's business; a one-shot would burst."""
        self.h.advance(10 * HOUR)  # 24 h reminder is now 4 h in the past
        self.h.deps.settings = settings_with(reminder_1=24, reminder_2=0)
        self.h.scan()
        self.assertIsNone(self.h.slot(self.account, "reminder1"))

    def test_the_scan_is_idempotent_for_reminders(self):
        before = sorted(job["name"] for job in self.reminder_jobs())
        self.h.scan()
        self.assertEqual(sorted(job["name"] for job in self.reminder_jobs()), before)

    def test_a_setting_change_retimes_the_reminder(self):
        self.h.deps.settings = settings_with(reminder_1=20, reminder_2=0, lead_minutes=10)
        self.h.scan()
        slot = self.h.slot(self.account, "reminder1")
        self.assertEqual(slot["fire_at"], self.credit.expires_at - 20 * HOUR)


class Firing(ReminderPlanCase):
    def at_reminder(self, role: str = "reminder1") -> None:
        slot = self.h.slot(self.account, role)
        self.assertIsNotNone(slot, role)
        self.h.advance_to(float(slot["fire_at"]) + 5)

    def test_the_run_sends_the_reminder(self):
        self.at_reminder("reminder1")
        result = self.run_remind()
        self.assertEqual(result.exit_code, 0, result)
        # The role lives in the dedupe key, not in the params: the catalog
        # declares only what the sentence shows (T-20).
        self.assertEqual(
            [notice["dedupe_key"].rsplit(":", 1)[-1] for notice in self.reminders()], ["reminder1"]
        )

    def test_the_run_never_posts(self):
        self.at_reminder("reminder1")
        self.run_remind()
        self.assertEqual(self.h.backend.posts, [])
        self.assertEqual(self.h.source.posts(), [])

    def test_the_run_never_changes_the_credit_row(self):
        self.at_reminder("reminder1")
        before = dict(self.h.row(self.account))
        self.run_remind()
        after = self.h.row(self.account)
        for field in (
            "status",
            "redeem_request_id",
            "posted_at",
            "confirmed_at",
            "attempts",
            "notified_outcome",
        ):
            self.assertEqual(after[field], before[field], field)

    def test_a_second_run_is_silent(self):
        self.at_reminder("reminder1")
        self.run_remind()
        self.run_remind()
        self.assertEqual(len(self.reminders()), 1)

    def test_the_second_reminder_is_a_separate_message(self):
        self.at_reminder("reminder1")
        self.run_remind()
        self.at_reminder("reminder2")
        self.run_remind()
        self.assertEqual(
            [notice["dedupe_key"].rsplit(":", 1)[-1] for notice in self.reminders()],
            ["reminder1", "reminder2"],
        )

    def test_nothing_is_sent_before_the_slot_time(self):
        slot = self.h.slot(self.account, "reminder1")
        self.h.advance_to(float(slot["fire_at"]) - 2 * MINUTE)
        self.run_remind()
        self.assertEqual(self.reminders(), [])

    def test_a_settled_credit_gets_no_reminder(self):
        self.at_reminder("reminder1")
        self.h.store.settle_credit(self.key, "redeemed", "reset", self.h.clock.now())
        self.run_remind()
        self.assertEqual(self.reminders(), [])

    def test_a_credit_redeemed_in_the_backend_gets_no_reminder(self):
        self.at_reminder("reminder1")
        self.credit.status = "redeemed"
        self.credit.redeemed_at = self.h.clock.now()
        self.run_remind()
        self.assertEqual(self.reminders(), [])

    def test_the_run_never_removes_its_own_job(self):
        self.at_reminder("reminder1")
        names = sorted(job["name"] for job in self.reminder_jobs())
        self.run_remind()
        self.assertEqual(sorted(job["name"] for job in self.reminder_jobs()), names)

    def test_a_too_old_reminder_slot_is_left_to_the_scan(self):
        slot = self.h.slot(self.account, "reminder1")
        self.h.advance_to(float(slot["fire_at"]) + 20 * MINUTE)
        self.run_remind()
        self.assertEqual(self.reminders(), [])

    def test_the_reminder_params_carry_no_raw_identifiers(self):
        self.at_reminder("reminder1")
        self.run_remind()
        params = self.reminders()[0]["params"]
        for forbidden in ("credit_id", "token", "account_key", "chat_id"):
            self.assertNotIn(forbidden, params)


class ReadOnlyByConstruction(ReminderPlanCase):
    """The remind run wraps the source, so a POST raises instead of spending."""

    def test_the_transport_refuses_a_post(self):
        from reset_rescue.codex_api import ReadOnlyTransport

        transport = ReadOnlyTransport(self.h.source)
        with self.assertRaises(MutationRefused):
            transport.request(self.account, "POST", "https://chatgpt.com/x")
        self.assertEqual(self.h.backend.posts, [])

    def test_the_remind_run_uses_the_read_only_api(self):
        fire = Fire(self.h.deps, self.h.store)
        api = fire._read_only_api()
        self.assertTrue(hasattr(api, "list_credits"))
        self.assertIsNot(api, self.h.deps.api)
        self.assertEqual(type(api.source).__name__, "ReadOnlyTransport")

    def test_a_reminder_role_is_never_a_redemption_role(self):
        self.assertNotIn(ROLE_REMIND, ("primary", "backup"))
        for role in REMINDER_ROLES:
            self.assertNotIn(role, ("primary", "backup"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
