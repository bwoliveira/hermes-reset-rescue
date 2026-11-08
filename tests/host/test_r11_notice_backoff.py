"""R11 — bounded backoff: a persisted ``next_attempt_at`` gates every retry.

The reviewer's counterexample (``additional_regressions.py``,
``test_r11_clustered_flushes_must_not_exhaust_retry_budget_without_backoff``)
drives five flushes at the same instant and watches the row reach
``abandoned``: an attempt *cap* with no spacing is not backoff. Cron jobs per
credit run back to back, so a short messaging outage would spend the whole
budget in one second and the next healthy scan would never select the row.

The contract these tests pin:

* every retryable failure persists ``next_attempt_at`` — the earliest instant
  the next flush may attempt the row — and that instant grows with the attempt
  number (exponential, capped);
* a flush **before** ``next_attempt_at`` does not attempt the row and does not
  spend an attempt: ``attempts`` and ``state`` are unchanged;
* a flush **after** it attempts the row again, and the row is delivered when
  the sender recovers;
* the deadline survives the process: it is a column, read back by a second
  ``Store`` over the same file, not an in-memory timer;
* a ``queued`` row is due immediately (``next_attempt_at`` NULL) — backoff
  never delays a first delivery.

The clock is a fake (``Store(clock=...)``), so "before" and "after" are exact.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from reset_rescue.cli_runner import CliResult
from reset_rescue.notify import MAX_DELIVERY_ATTEMPTS, Notifier
from reset_rescue.store import Store

#: A scratch root **outside the real profile**. It used to be
#: ``<HERMES_HOME>/cache/scratch/rr-gc`` — inside ``~/.hermes``, which Hermes
#: resolves back to the real root, so every temp DB landed in real state
#: (R17). ``tests.support.isolation`` refuses a root under ``$HOME``.
SCRATCH = Path(
    os.environ.get("RR_SCRATCH")
    or os.path.join(os.environ.get("RR_TEMP_ROOT") or "/var/tmp", "rr-scratch")
)

PARAMS = {
    "label": "b***@e***.com",
    "redeemed_at": 1_800_000_000.0,
    "expires_at": 1_800_000_600.0,
}

T0 = 1_800_000_000.0


class Clock:
    """A settable clock: the only way time moves in these tests."""

    def __init__(self, now: float = T0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


class Sender:
    """``hermes send`` stand-in: fails while ``failing``, then succeeds."""

    def __init__(self, *, failing: bool = True) -> None:
        self.failing = failing
        self.calls = 0

    def run(self, argv, *, timeout: float = 30.0) -> CliResult:
        self.calls += 1
        if self.failing:
            return CliResult(
                argv=tuple(str(part) for part in argv),
                returncode=1,
                stdout="",
                stderr="connection reset",
            )
        return CliResult(
            argv=tuple(str(part) for part in argv),
            returncode=0,
            stdout='{"success": true, "note": "sent"}',
            stderr="",
        )


class BackoffCase(unittest.TestCase):
    def setUp(self) -> None:
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(SCRATCH), prefix="r11-backoff-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.clock = Clock()
        self.store = Store(self.dir, clock=self.clock)
        self.addCleanup(self.store.close)

    def notifier(self, sender: Sender) -> Notifier:
        return Notifier(sender, target="telegram", store=self.store)

    def enqueue(self, dedupe: str = "r11:backoff") -> int:
        return self.store.enqueue_notice(
            dedupe,
            kind="success",
            message_key="success",
            params=PARAMS,
            target="telegram",
            now=T0,
        )

    def row(self, notice_id: int) -> dict:
        for row in self.store.list_notices():
            if int(row["id"]) == int(notice_id):
                return row
        raise AssertionError(f"no outbox row {notice_id}")


class ClusteredFlushesMustNotSpendTheBudget(BackoffCase):
    def test_the_reviewers_counterexample_five_flushes_at_one_instant(self):
        """R11 counterexample: five flushes at the same clock must not abandon."""
        sender = Sender(failing=True)
        notice = self.enqueue()
        for _ in range(5):
            self.notifier(sender).flush()
        row = self.row(notice)
        self.assertEqual(
            sender.calls,
            1,
            f"the same instant was attempted {sender.calls} times; a retry needs a deadline",
        )
        self.assertEqual(int(row["attempts"]), 1, "a flush before the deadline spent an attempt")
        self.assertEqual(row["state"], "failed")
        self.assertIsNotNone(row["next_attempt_at"], "no persisted next_attempt_at")
        self.assertGreater(float(row["next_attempt_at"]), T0)

    def test_a_flush_before_the_deadline_does_not_touch_the_row(self):
        """My variant: the row is byte-identical before and after an early flush."""
        sender = Sender(failing=True)
        notice = self.enqueue()
        self.notifier(sender).flush()
        before = self.row(notice)
        self.clock.advance(float(before["next_attempt_at"]) - self.clock.now - 0.001)
        self.notifier(sender).flush()
        after = self.row(notice)
        self.assertEqual(sender.calls, 1, "an early flush attempted the row")
        self.assertEqual(int(after["attempts"]), int(before["attempts"]))
        self.assertEqual(after["state"], before["state"])
        self.assertEqual(float(after["next_attempt_at"]), float(before["next_attempt_at"]))

    def test_a_flush_after_the_deadline_retries_and_can_deliver(self):
        """My variant: crossing the deadline is what makes the retry due."""
        sender = Sender(failing=True)
        notice = self.enqueue()
        self.notifier(sender).flush()
        first = self.row(notice)
        self.clock.advance(float(first["next_attempt_at"]) - self.clock.now + 0.001)
        sender.failing = False
        self.notifier(sender).flush()
        row = self.row(notice)
        self.assertEqual(sender.calls, 2, "the row was not retried after its deadline")
        self.assertEqual(row["state"], "sent")
        self.assertIsNone(row["next_attempt_at"], "a sent row kept a retry deadline")

    def test_the_deadline_grows_with_the_attempt_number(self):
        """My variant: the spacing widens, so a long outage is not hammered."""
        sender = Sender(failing=True)
        notice = self.enqueue()
        waits: list[float] = []
        for _ in range(3):
            self.notifier(sender).flush()
            row = self.row(notice)
            waits.append(float(row["next_attempt_at"]) - self.clock.now)
            self.clock.advance(waits[-1] + 0.001)
        self.assertEqual(sender.calls, 3, f"attempts: {sender.calls}")
        self.assertLess(waits[0], waits[1], f"no widening: {waits}")
        self.assertLess(waits[1], waits[2], f"no widening: {waits}")

    def test_the_spacing_is_capped(self):
        """My variant: doubling forever would park a row for days."""
        sender = Sender(failing=True)
        notice = self.enqueue()
        waits: list[float] = []
        for _ in range(MAX_DELIVERY_ATTEMPTS):
            self.notifier(sender).flush()
            row = self.row(notice)
            if row["state"] == "abandoned":
                break
            waits.append(float(row["next_attempt_at"]) - self.clock.now)
            self.clock.advance(waits[-1] + 0.001)
        self.assertGreaterEqual(len(waits), 3, f"only {len(waits)} spaced attempts: {waits}")
        self.assertLessEqual(max(waits), 3600.0, f"the spacing is unbounded: {waits}")

    def test_the_deadline_survives_a_new_store_over_the_same_file(self):
        """My variant: the deadline is a column, not a timer in this process."""
        sender = Sender(failing=True)
        notice = self.enqueue()
        self.notifier(sender).flush()
        deadline = float(self.row(notice)["next_attempt_at"])
        self.store.close()
        reopened = Store(self.dir, clock=self.clock)
        self.addCleanup(reopened.close)
        row = next(r for r in reopened.list_notices() if int(r["id"]) == int(notice))
        self.assertEqual(float(row["next_attempt_at"]), deadline)
        self.clock.advance(deadline - self.clock.now - 0.001)
        sender.failing = False
        Notifier(sender, target="telegram", store=reopened).flush()
        self.assertEqual(sender.calls, 1, "a reopened store retried before the deadline")
        self.clock.advance(0.002)
        Notifier(sender, target="telegram", store=reopened).flush()
        self.assertEqual(sender.calls, 2, "a reopened store never retried after the deadline")

    def test_a_queued_row_is_due_immediately(self):
        """My variant: backoff must never delay a first delivery."""
        sender = Sender(failing=False)
        notice = self.enqueue()
        self.assertIsNone(self.row(notice)["next_attempt_at"])
        self.notifier(sender).flush()
        self.assertEqual(sender.calls, 1)
        self.assertEqual(self.row(notice)["state"], "sent")


class AbandonedRowsAreStillBounded(BackoffCase):
    def test_the_row_is_abandoned_at_the_cap_after_the_spacing_is_honoured(self):
        """The cap still exists; only the *spacing* was missing."""
        sender = Sender(failing=True)
        notice = self.enqueue()
        for _ in range(MAX_DELIVERY_ATTEMPTS + 3):
            self.notifier(sender).flush()
            row = self.row(notice)
            if row["state"] == "abandoned":
                break
            self.clock.advance(float(row["next_attempt_at"]) - self.clock.now + 0.001)
        row = self.row(notice)
        self.assertEqual(row["state"], "abandoned")
        self.assertEqual(int(row["attempts"]), MAX_DELIVERY_ATTEMPTS)
        self.assertEqual(sender.calls, MAX_DELIVERY_ATTEMPTS, f"attempts: {sender.calls}")
        self.assertIsNone(row["next_attempt_at"], "an abandoned row kept a retry deadline")


if __name__ == "__main__":
    unittest.main(verbosity=2)
