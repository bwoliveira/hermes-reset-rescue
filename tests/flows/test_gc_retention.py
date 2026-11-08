"""T-35 — GC, retention and startup healing (A§7.5, A§6, I7, R7, R8).

The three properties this file pins:

* **No run removes its own job** and a removal needs the scheduler to report
  the job finished *and* the slot to be older than the script timeout plus ten
  minutes, so the scheduler has already killed or finished any run of it.
* **A stale run is closed** as ``interrupted`` when its pid is gone, and a live
  one is left alone.
* **A credit left ``redeeming`` with ``posted_at`` is finished by the next run
  that sees it**, with the same ``redeem_request_id`` (I2) — never a new UUID.

Retention: settled credits and sent notices after 14 days, rehearsals after one
day, history capped at 5 000 rows.
"""

from __future__ import annotations

import os
import unittest

from tests.flows.support import DAY, HOUR, MINUTE, Harness

SCRIPT_TIMEOUT_S = 3_600.0
GC_MARGIN_S = 600.0


class GcCase(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness(self)

    def scan(self, **kwargs):
        return self.h.scan(**kwargs)

    def run_fire(self, role: str = "primary", **kwargs):
        return self.h.run_fire(role=role, **kwargs)

    def armed(self, *, expires_in: float = 30 * HOUR, credit_id: str = "credit-1"):
        account = self.h.add_account()
        self.h.add_credit(account, credit_id, expires_in=expires_in)
        self.assertEqual(self.scan().exit_code, 0)
        return account

    def job_names_for(self, account) -> list[str]:
        """The credit's *redemption* one-shots (reminders are a separate role)."""
        key8 = self.h.key(account)[:8]
        return [name for name in self.h.job_names() if key8 in name and "remind-" not in name]

    def at_slot(self, account, role: str, *, early: float = 0.0) -> float:
        slot = self.h.slot(account, role)
        self.assertIsNotNone(slot)
        self.h.advance_to(float(slot["fire_at"]) + early)
        return float(slot["fire_at"])


class NoRunRemovesItsOwnJob(GcCase):
    def test_a_primary_run_leaves_its_own_job_in_place(self):
        account = self.armed()
        names = self.job_names_for(account)
        self.assertEqual(len(names), 2, names)
        self.at_slot(account, "primary")
        self.run_fire("primary")
        for name in names:
            self.assertIsNotNone(self.h.job_for(name), f"{name} vanished during its own run")

    def test_a_backup_run_leaves_its_own_job_in_place(self):
        account = self.armed()
        names = self.job_names_for(account)
        self.at_slot(account, "backup")
        self.run_fire("backup")
        for name in names:
            self.assertIsNotNone(self.h.job_for(name))

    def test_a_scan_leaves_every_live_job_in_place(self):
        self.armed()
        before = self.h.job_names()
        self.scan()
        self.assertEqual(self.h.job_names(), before)


class GcTiming(GcCase):
    def test_removal_needs_completed_plus_timeout_plus_ten_minutes(self):
        account = self.armed()
        fire_at = self.at_slot(account, "primary")
        self.run_fire("primary")
        name = self.job_names_for(account)[0]
        # Not finished yet: never removed, however old.
        self.h.advance_to(fire_at + SCRIPT_TIMEOUT_S + GC_MARGIN_S + 5 * MINUTE)
        self.scan()
        self.assertIsNotNone(self.h.job_for(name), "a live job was removed")
        # Finished, but inside the script timeout + margin, measured from the slot.
        self.h.complete_job(name)
        self.h.advance_to(fire_at + SCRIPT_TIMEOUT_S + GC_MARGIN_S - MINUTE)
        self.scan()
        self.assertIsNotNone(self.h.job_for(name), "GC ignored the script timeout")
        # Past the timeout plus the margin.
        self.h.advance_to(fire_at + SCRIPT_TIMEOUT_S + GC_MARGIN_S + MINUTE)
        self.scan()
        self.assertIsNone(self.h.job_for(name), "GC never removed the finished job")

    def test_gc_measures_from_the_slot_time_not_from_completion(self):
        """A job completed late is still removed once its slot is old enough."""
        account = self.armed()
        fire_at = self.at_slot(account, "primary")
        self.run_fire("primary")
        name = self.job_names_for(account)[0]
        self.h.advance_to(fire_at + SCRIPT_TIMEOUT_S + GC_MARGIN_S + MINUTE)
        self.h.complete_job(name)
        self.scan()
        self.assertIsNone(self.h.job_for(name))

    def test_a_disabled_job_counts_as_finished(self):
        account = self.armed()
        fire_at = self.at_slot(account, "primary")
        self.run_fire("primary")
        name = self.job_names_for(account)[0]
        job = self.h.job_for(name)
        job["enabled"] = False
        self.h.advance_to(fire_at + SCRIPT_TIMEOUT_S + GC_MARGIN_S + MINUTE)
        self.scan()
        self.assertIsNone(self.h.job_for(name))

    def test_a_pending_slot_is_protected_even_when_finished(self):
        """A slot that never ran keeps its job: the backup may still need it."""
        account = self.armed()
        self.at_slot(account, "primary")
        self.run_fire("primary")
        backup = [name for name in self.job_names_for(account) if name.endswith("-b")][0]
        self.h.advance(SCRIPT_TIMEOUT_S + GC_MARGIN_S + 5 * MINUTE)
        self.h.complete_job(backup)
        self.scan()
        self.assertIsNotNone(self.h.job_for(backup), "GC removed a slot that never ran")

    def test_gc_never_touches_a_job_it_does_not_own(self):
        self.armed()
        self.h.cron.jobs["foreign-1"] = {
            "id": "foreign-1",
            "name": "reset-rescue-mine",
            "enabled": False,
            "state": "completed",
            "schedule": "every 1h",
            "argv": [],
        }
        self.h.advance(10 * HOUR)
        self.scan()
        self.assertIsNotNone(self.h.job_for("reset-rescue-mine"))


class StartupHealing(GcCase):
    def test_a_stale_run_is_closed_as_interrupted(self):
        dead = self.h.store.start_run("fire", "primary", 999_999, self.h.clock.now())
        self.scan()
        rows = {row["id"]: row for row in self.h.store.list_runs()}
        self.assertEqual(rows[dead]["outcome"], "interrupted")
        self.assertIsNotNone(rows[dead]["ended_at"])

    def test_a_live_run_is_left_alone(self):
        live = self.h.store.start_run("fire", "primary", os.getpid(), self.h.clock.now())
        self.scan()
        rows = {row["id"]: row for row in self.h.store.list_runs()}
        self.assertIsNone(rows[live]["ended_at"])

    def test_a_fire_heals_before_it_works(self):
        dead = self.h.store.start_run("scan", None, 999_999, self.h.clock.now())
        account = self.armed()
        self.at_slot(account, "primary")
        self.run_fire("primary")
        rows = {row["id"]: row for row in self.h.store.list_runs()}
        self.assertEqual(rows[dead]["outcome"], "interrupted")

    def test_a_redeeming_row_is_finished_with_the_same_uuid(self):
        """A crash after the POST leaves ``redeeming``; the next run finishes it (I2)."""
        account = self.armed()
        key = self.h.key(account)
        # Simulate the crash: the pre-POST write landed, the process died.
        request_id = self.h.store.begin_redeem(key, self.h.clock.now())
        self.h.store.update_credit(key, status="redeeming", posted_at=self.h.clock.now())
        # The backend did consume it, but nobody read back.
        self.h.backend.only(account.account_key).status = "redeemed"
        self.h.backend.only(account.account_key).redeemed_at = self.h.clock.now()
        self.at_slot(account, "primary")
        result = self.run_fire("primary")
        row = self.h.row(account)
        self.assertEqual(row["status"], "redeemed")
        self.assertEqual(row["redeem_request_id"], request_id)
        self.assertEqual(self.h.backend.posts, [], "a settled credit was posted again")
        self.assertEqual(result.exit_code, 0)

    def test_a_redeeming_row_still_listed_is_posted_again_with_the_same_uuid(self):
        account = self.armed()
        key = self.h.key(account)
        request_id = self.h.store.begin_redeem(key, self.h.clock.now())
        self.h.store.update_credit(key, status="redeeming", posted_at=self.h.clock.now())
        self.at_slot(account, "primary")
        self.run_fire("primary")
        self.assertEqual(len(self.h.backend.posts), 1)
        self.assertEqual(self.h.backend.posts[0]["redeem_request_id"], request_id)
        self.assertEqual(self.h.row(account)["status"], "redeemed")

    def test_the_scan_finishes_a_redeeming_row_too(self):
        account = self.armed()
        key = self.h.key(account)
        request_id = self.h.store.begin_redeem(key, self.h.clock.now())
        self.h.store.update_credit(key, status="redeeming", posted_at=self.h.clock.now())
        self.h.backend.only(account.account_key).status = "redeemed"
        self.h.backend.only(account.account_key).redeemed_at = self.h.clock.now()
        self.scan()
        row = self.h.row(account)
        self.assertEqual(row["status"], "redeemed")
        self.assertEqual(row["redeem_request_id"], request_id)


class Retention(GcCase):
    def test_settled_credits_are_pruned_after_fourteen_days(self):
        account = self.armed()
        self.at_slot(account, "primary")
        self.run_fire("primary")
        key = self.h.key(account)
        self.assertEqual(self.h.row(account)["status"], "redeemed")
        self.h.advance(13 * DAY)
        self.scan()
        self.assertIsNotNone(self.h.store.get_credit(key), "pruned too early")
        self.h.advance(2 * DAY)
        self.scan()
        self.assertIsNone(self.h.store.get_credit(key))

    def test_an_unsettled_credit_is_kept(self):
        account = self.armed()
        key = self.h.key(account)
        self.h.advance(30 * DAY)
        self.scan()
        self.assertIsNotNone(self.h.store.get_credit(key))

    def test_sent_notices_are_pruned_but_queued_ones_survive(self):
        account = self.armed()
        self.at_slot(account, "primary")
        self.run_fire("primary")
        sent = self.h.store.list_notices(state="sent")
        self.assertTrue(sent, "no notice was recorded")
        self.h.store.enqueue_notice(
            "queued:1", kind="success", message_key="success", params={}, now=self.h.clock.now()
        )
        self.h.advance(15 * DAY)
        self.scan()
        self.assertEqual(self.h.store.list_notices(state="sent"), [])
        self.assertEqual(len(self.h.store.list_notices(state="queued")), 1)

    def test_history_is_capped(self):
        for _ in range(5_050):
            self.h.store.add_history(
                [{"at": self.h.clock.now(), "action": "scan", "outcome": "ok"}]
            )
        self.assertEqual(self.h.store.count_history(), 5_000)


class DeadJobReconciliation(GcCase):
    def test_a_job_that_vanished_from_cron_is_marked_removed_not_left_active(self):
        """A lost job is recreated, but its old row must not stay 'active'."""
        account = self.armed()
        old = [self.h.job_for(name)["id"] for name in self.job_names_for(account)]
        for job_id in old:
            self.h.cron.jobs.pop(job_id)
        self.h.advance(SCRIPT_TIMEOUT_S + GC_MARGIN_S + MINUTE)
        self.scan()
        active = {row["job_id"] for row in self.h.store.list_owned_jobs(active_only=True)}
        self.assertEqual(active & set(old), set(), "a vanished job stayed active")
        # And the credit is still scheduled: the lost one-shot came back.
        self.assertEqual(len(self.job_names_for(account)), 2)

    def test_a_slot_whose_job_vanished_is_recreated_by_the_next_scan(self):
        account = self.armed()
        name = self.job_names_for(account)[0]
        job = self.h.job_for(name)
        self.h.cron.jobs.pop(job["id"])
        self.scan()
        self.assertIsNotNone(self.h.job_for(name), "the lost one-shot was not recreated")


if __name__ == "__main__":
    unittest.main(verbosity=2)
