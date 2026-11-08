"""D-015 — manual **Redeem now**: same idempotent path, plus a per-credit lock.

The manual role is back (the simplification pass had cut it). What must hold:

* it refuses without a literal confirmation and in a dry run, before touching
  anything;
* it uses the **same** ``redeem_one``: the persisted ``redeem_request_id``, the
  readback rule, the same notices — never a second POST site (I9);
* a **per-credit lock** serialises it against a primary/backup one-shot that is
  already redeeming the same credit, so the loser waits and re-decides instead
  of spending twice;
* it works outside the lead window (that is the point of a manual click) and
  still refuses a credit the backend has already settled.
"""

from __future__ import annotations

import os
import unittest

from reset_rescue.fire import ROLE_MANUAL
from reset_rescue.store import LockTimeout
from tests.flows.support import HOUR, Harness


class ManualCase(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness(self)
        self.account = self.h.add_account()
        self.credit = self.h.add_credit(self.account, expires_in=30 * HOUR)
        self.assertEqual(self.h.scan().exit_code, 0)
        self.key = self.h.key(self.account)
        self.fire = self.h.fire()

    def redeem_now(self, **kwargs):
        return self.fire.redeem_now(self.key, **kwargs)

    def posts(self) -> list[dict]:
        return self.h.backend.posts


class Confirmation(ManualCase):
    def test_it_refuses_without_confirmation(self):
        result = self.redeem_now()
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.problem_code, "confirmation_required")
        self.assertEqual(self.posts(), [])

    def test_a_truthy_string_is_not_a_confirmation(self):
        for value in ("true", 1, "yes", [], {}):
            result = self.fire.redeem_now(self.key, confirm=value)  # type: ignore[arg-type]
            self.assertEqual(result.exit_code, 2, value)
        self.assertEqual(self.posts(), [])

    def test_a_dry_run_refuses_even_with_confirmation(self):
        result = self.redeem_now(confirm=True, dry_run=True)
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.problem_code, "dry_run")
        self.assertEqual(self.posts(), [])

    def test_an_unknown_credit_is_refused(self):
        result = self.fire.redeem_now("0" * 16, confirm=True)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.problem_code, "unknown_credit")
        self.assertEqual(self.posts(), [])


class SameIdempotentPath(ManualCase):
    def test_a_confirmed_click_redeems(self):
        result = self.redeem_now(confirm=True)
        self.assertEqual(result.exit_code, 0, result)
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.h.row(self.account)["status"], "redeemed")

    def test_it_uses_the_persisted_uuid(self):
        request_id = self.h.store.ensure_request_id(self.key)
        self.redeem_now(confirm=True)
        self.assertEqual(self.posts()[0]["redeem_request_id"], request_id)
        self.assertEqual(self.h.row(self.account)["redeem_request_id"], request_id)

    def test_it_requires_a_readback(self):
        self.h.backend.on_post = lambda *_: {"code": "reset", "windows_reset": 1}
        result = self.redeem_now(confirm=True)
        self.assertEqual(result.exit_code, 1)
        self.assertNotEqual(self.h.row(self.account)["status"], "redeemed")

    def test_it_works_outside_the_lead_window(self):
        self.h.advance_to(self.credit.expires_at - 20 * HOUR)
        result = self.redeem_now(confirm=True)
        self.assertEqual(result.exit_code, 0, result)
        self.assertEqual(len(self.posts()), 1)

    def test_a_second_click_does_not_post_again(self):
        self.redeem_now(confirm=True)
        posts = len(self.posts())
        second = self.redeem_now(confirm=True)
        self.assertEqual(len(self.posts()), posts)
        self.assertEqual(second.exit_code, 0)

    def test_a_settled_credit_is_not_posted(self):
        self.credit.status = "redeemed"
        self.credit.redeemed_at = self.h.clock.now()
        result = self.redeem_now(confirm=True)
        self.assertEqual(self.posts(), [])
        self.assertEqual(result.exit_code, 0)

    def test_it_announces_one_success(self):
        self.redeem_now(confirm=True)
        successes = [notice for notice in self.h.notices() if notice["kind"] == "success"]
        self.assertEqual(len(successes), 1)

    def test_it_records_the_redemption_in_the_activity_log(self):
        self.redeem_now(confirm=True)
        rows = self.h.store.list_history(action="redemption")
        self.assertTrue(rows)
        self.assertEqual(rows[0]["credit_key"], self.key)
        self.assertEqual(rows[0]["role"], ROLE_MANUAL)

    def test_the_service_shape_returns_a_redeem_result(self):
        # The service validates ``confirm`` before it calls this facade and
        # passes it through, so the gate sees the same evidence the user gave.
        result = self.fire.redeem_one(self.key, role=ROLE_MANUAL, confirm=True)
        self.assertEqual(result.credit_key, self.key)
        self.assertEqual(result.outcome, "redeemed")
        self.assertEqual(len(self.posts()), 1)


class PerCreditLock(ManualCase):
    def test_the_credit_lock_is_exclusive_across_processes(self):
        path = self.h.data_dir / f"credit-{self.key}.lock"
        other = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, other)
        import fcntl

        fcntl.flock(other, fcntl.LOCK_EX)
        try:
            with self.assertRaises(LockTimeout):
                with self.h.store.credit_lock(self.key, wait=0.2, poll=0.02):
                    self.fail("the per-credit lock was not exclusive")
        finally:
            fcntl.flock(other, fcntl.LOCK_UN)

    def test_a_locked_credit_makes_redeem_now_report_lock_timeout(self):
        path = self.h.data_dir / f"credit-{self.key}.lock"
        other = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, other)
        import fcntl

        fcntl.flock(other, fcntl.LOCK_EX)
        try:
            self.h.store._lock_wait = 0.2
            self.h.store._lock_poll = 0.02
            result = self.redeem_now(confirm=True)
        finally:
            fcntl.flock(other, fcntl.LOCK_UN)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.problem_code, "lock_timeout")
        self.assertEqual(self.posts(), [])

    def test_the_lock_is_released_after_a_click(self):
        self.redeem_now(confirm=True)
        with self.h.store.credit_lock(self.key, wait=0.5):
            pass

    def test_the_lock_is_released_when_the_body_raises(self):
        with self.assertRaises(RuntimeError):
            with self.h.store.credit_lock(self.key, wait=0.5):
                raise RuntimeError("boom")
        with self.h.store.credit_lock(self.key, wait=0.5):
            pass

    def test_the_lock_file_is_0600(self):
        import stat

        with self.h.store.credit_lock(self.key, wait=0.5):
            pass
        path = self.h.data_dir / f"credit-{self.key}.lock"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_two_credits_do_not_block_each_other(self):
        self.h.add_credit(self.account, "credit-2", expires_in=31 * HOUR)
        other_key = self.h.key(self.account, "credit-2")
        with self.h.store.credit_lock(self.key, wait=0.5):
            with self.h.store.credit_lock(other_key, wait=0.5):
                pass


class ManualNeverBecomesASlot(ManualCase):
    def test_a_manual_click_does_not_mark_the_primary_slot(self):
        self.redeem_now(confirm=True)
        self.assertIsNone(self.h.slot(self.account, "primary")["ran_at"])

    def test_a_manual_click_does_not_remove_a_live_job(self):
        names = sorted(self.h.job_names())
        self.redeem_now(confirm=True)
        self.assertEqual(sorted(self.h.job_names()), names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
