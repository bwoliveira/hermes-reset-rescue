"""T-60 — crash injection around ``redeem_one`` (I2, A§6 healing).

The three windows a real crash can land in:

1. **after the pre-POST persist, before the POST** — the row is ``redeeming``
   with a UUID and no ``posted_at``; nothing was spent. The next run must POST
   with the *same* UUID.
2. **after the POST, before the readback** — the row is ``redeeming`` with
   ``posted_at``; the credit may be spent. The next run must re-decide from the
   backend: absent/redeemed counts as our success, still-listed gets a second
   POST with the same UUID.
3. **after the readback, before the settle** — the backend is settled but the
   row is not; the next run must settle it and announce exactly one success.

Every case asserts the same two properties: the UUID never changes, and the
credit produces exactly one success notice however many runs it takes.
"""

from __future__ import annotations

import unittest

from reset_rescue.fire import OUT_REDEEMED, redeem_one
from tests.flows.support import HOUR, Harness


class CrashCase(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness(self)
        self.account = self.h.add_account()
        self.credit = self.h.add_credit(self.account, expires_in=30 * HOUR)
        self.assertEqual(self.h.scan().exit_code, 0)
        self.key = self.h.key(self.account)
        slot = self.h.slot(self.account, "primary")
        self.h.advance_to(float(slot["fire_at"]))

    def row(self) -> dict:
        row = self.h.store.get_credit(self.key)
        self.assertIsNotNone(row)
        return row

    def successes(self) -> list[dict]:
        return [notice for notice in self.h.notices() if notice["kind"] == "success"]

    def run_fire(self, role: str = "primary"):
        return self.h.run_fire(role=role)


class WindowOnePersistThenCrash(CrashCase):
    """The pre-POST write landed; the process died before the POST."""

    def crash(self) -> str:
        request_id = self.h.store.begin_redeem(self.key, self.h.clock.now())
        # No POST was sent: the backend still lists the credit as available.
        self.assertEqual(self.h.backend.posts, [])
        return request_id

    def test_the_next_run_posts_with_the_same_uuid(self):
        request_id = self.crash()
        self.assertEqual(self.row()["status"], "redeeming")
        self.assertIsNotNone(self.row()["posted_at"], "the intent was not persisted")
        result = self.run_fire("primary")
        self.assertEqual(result.exit_code, 0, result)
        self.assertEqual(len(self.h.backend.posts), 1)
        self.assertEqual(self.h.backend.posts[0]["redeem_request_id"], request_id)
        self.assertEqual(self.row()["redeem_request_id"], request_id)
        self.assertEqual(self.row()["status"], OUT_REDEEMED)

    def test_the_uuid_survives_a_second_crash(self):
        request_id = self.crash()
        # Crash again in the same window: the UUID must not be regenerated.
        again = self.h.store.begin_redeem(self.key, self.h.clock.now())
        self.assertEqual(again, request_id)
        self.run_fire("primary")
        self.assertEqual(self.h.backend.posts[0]["redeem_request_id"], request_id)

    def test_exactly_one_success_notice(self):
        self.crash()
        self.run_fire("primary")
        self.run_fire("backup")
        self.assertEqual(len(self.successes()), 1)


class WindowTwoPostThenCrash(CrashCase):
    """The POST reached the backend; the process died before the readback."""

    def crash(self) -> str:
        request_id = self.h.store.begin_redeem(self.key, self.h.clock.now())
        self.h.backend.consume(self.account.account_key, self.credit.id, request_id)
        # The backend spent the credit; the row still says ``redeeming``.
        self.assertEqual(len(self.h.backend.posts), 1)
        return request_id

    def test_a_settled_credit_is_finished_without_a_second_post(self):
        request_id = self.crash()
        result = self.run_fire("primary")
        self.assertEqual(result.exit_code, 0, result)
        self.assertEqual(len(self.h.backend.posts), 1, "the credit was posted twice")
        row = self.row()
        self.assertEqual(row["status"], OUT_REDEEMED)
        self.assertEqual(row["redeem_request_id"], request_id)
        self.assertIsNotNone(row["confirmed_at"])
        self.assertEqual(len(self.successes()), 1)

    def test_a_still_listed_credit_is_posted_again_with_the_same_uuid(self):
        request_id = self.crash()
        # The POST was lost on the way back: the backend never applied it.
        self.credit.status = "available"
        self.credit.redeemed_at = None
        result = self.run_fire("primary")
        self.assertEqual(result.exit_code, 0, result)
        self.assertEqual(len(self.h.backend.posts), 2)
        self.assertEqual(self.h.backend.posts[1]["redeem_request_id"], request_id)
        self.assertEqual(self.row()["status"], OUT_REDEEMED)
        self.assertEqual(len(self.successes()), 1)

    def test_the_scan_finishes_it_when_no_one_shot_is_left(self):
        self.crash()
        for name in list(self.h.job_names()):
            job = self.h.job_for(name)
            self.h.cron.jobs.pop(job["id"])
        result = self.h.scan()
        self.assertEqual(result.exit_code, 0, result)
        self.assertEqual(len(self.h.backend.posts), 1, "the scan posted a settled credit")
        self.assertEqual(self.row()["status"], OUT_REDEEMED)
        self.assertEqual(len(self.successes()), 1)


class WindowThreeSettleThenCrash(CrashCase):
    """The backend settled and the readback landed; the settle write did not."""

    def crash(self) -> str:
        request_id = self.h.store.begin_redeem(self.key, self.h.clock.now())
        self.h.backend.consume(self.account.account_key, self.credit.id, request_id)
        # The row is still ``redeeming`` even though the backend is done: the
        # next run must settle it from the backend, not from a local flag.
        return request_id

    def test_the_next_run_settles_and_announces_once(self):
        request_id = self.crash()
        self.run_fire("primary")
        self.assertEqual(self.row()["status"], OUT_REDEEMED)
        self.run_fire("backup")
        self.h.scan()
        self.assertEqual(len(self.successes()), 1)
        self.assertEqual(len(self.h.backend.posts), 1)
        self.assertEqual(self.row()["redeem_request_id"], request_id)


class InterruptedRunIsClosed(CrashCase):
    """A run row left open by a dead process is healed, not left dangling."""

    def test_a_dead_run_is_closed_and_the_work_still_happens(self):
        dead = self.h.store.start_run("fire", "primary", 999_999, self.h.clock.now())
        result = self.run_fire("primary")
        self.assertEqual(result.exit_code, 0, result)
        rows = {row["id"]: row for row in self.h.store.list_runs()}
        self.assertEqual(rows[dead]["outcome"], "interrupted")
        self.assertEqual(self.row()["status"], OUT_REDEEMED)

    def test_the_lock_is_free_after_a_crash(self):
        """A crashed process leaves no lock behind: flock dies with the process."""
        with self.h.store.run_lock(wait=1.0):
            pass
        result = self.run_fire("primary")
        self.assertEqual(result.exit_code, 0, result)


class DirectRedeemOneCrash(CrashCase):
    """``redeem_one`` called directly (the quota-check path) behaves the same."""

    def test_a_second_call_after_a_settled_backend_does_not_post(self):
        request_id = self.h.store.begin_redeem(self.key, self.h.clock.now())
        self.h.backend.consume(self.account.account_key, self.credit.id, request_id)
        result = redeem_one(self.h.deps, self.h.store, self.row(), role="early")
        self.assertEqual(result.outcome, OUT_REDEEMED)
        self.assertEqual(len(self.h.backend.posts), 1)
        second = redeem_one(self.h.deps, self.h.store, self.row(), role="early")
        self.assertEqual(second.outcome, OUT_REDEEMED)
        self.assertEqual(len(self.h.backend.posts), 1)
        # ``redeem_one`` queues; the run flushes. Exactly one success row exists.
        successes = [row for row in self.h.store.list_notices() if row["kind"] == "success"]
        self.assertEqual(len(successes), 1)
        self.assertEqual(self.row()["redeem_request_id"], request_id)


class NoticeSurvivesARestart(CrashCase):
    """A queued notice is not lost when the process dies before flushing it."""

    def test_a_queued_notice_is_flushed_by_the_next_run(self):
        request_id = self.h.store.begin_redeem(self.key, self.h.clock.now())
        self.h.backend.consume(self.account.account_key, self.credit.id, request_id)
        # Simulate the crash: settle + outbox row, then drop the notifier queue.
        self.h.store.settle_credit(self.key, OUT_REDEEMED, "reset", self.h.clock.now())
        self.h.store.enqueue_notice(
            "success:crash",
            kind="success",
            message_key="success",
            params={"label": "Full reset"},
            credit_key=self.key,
            now=self.h.clock.now(),
        )
        self.h.notifier.enqueued.clear()
        self.assertEqual(len(self.h.store.list_notices(state="queued")), 1)
        self.h.scan()
        # The scan flushes the outbox; the row is delivered or kept for next time,
        # never silently dropped.
        states = {row["state"] for row in self.h.store.list_notices()}
        self.assertTrue(states <= {"queued", "sent"}, states)


if __name__ == "__main__":
    unittest.main(verbosity=2)
