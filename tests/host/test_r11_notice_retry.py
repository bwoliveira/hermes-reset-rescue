"""R11 — a failed delivery is retried on a later flush, with bounded attempts.

The outbox is the only thing that survives between two runs, so the retry policy
has to live there. These tests drive the **real** ``Store`` (temp dir under the
lane scratch root) with a fake ``hermes send`` sender, so what is asserted is the
row state the next cron tick will read — not a notifier-internal counter.

Three separate outcomes, deliberately distinguishable in the database:

* ``queued``    — never attempted;
* ``failed``    — a retryable failure (network, timeout, non-zero exit); the row
                  is picked up again by the next flush while ``attempts`` is
                  below the cap;
* ``abandoned`` — the cap was reached; no further attempt is made, and the row
                  stays visible with its last error code;
* ``blocked``   — a permanent configuration error (no home channel, platform not
                  configured, usage error, unrenderable notice). Retrying cannot
                  help, so it is not attempted again and is never confused with
                  a transient failure.

There is **no exactly-once delivery guarantee**: a process that dies after
``hermes send`` accepted the message but before the row is marked ``sent`` will
deliver that notice again on the next flush. Closing that window needs an
idempotency key honoured by the delivery side, which ``hermes send`` does not
offer; the honest contract is at-least-once with bounded retries.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from reset_rescue.cli_runner import CliResult
from reset_rescue.notify import Notifier
from reset_rescue.store import Store

#: A scratch root **outside the real profile**. It used to be
#: ``<HERMES_HOME>/cache/scratch/rr-fc`` — inside ``~/.hermes``, which Hermes
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


class Sender:
    """``hermes send`` stand-in: fails the first ``failures`` calls, then succeeds."""

    def __init__(self, *, failures: int = 1, stderr: str = "", returncode: int = 1) -> None:
        self.failures = failures
        self.stderr = stderr
        self.returncode = returncode
        self.calls = 0

    def run(self, argv, *, timeout: float = 30.0) -> CliResult:
        self.calls += 1
        if self.calls <= self.failures:
            return CliResult(
                argv=tuple(str(part) for part in argv),
                returncode=self.returncode,
                stdout="",
                stderr=self.stderr,
            )
        return CliResult(
            argv=tuple(str(part) for part in argv),
            returncode=0,
            stdout='{"success": true, "note": "sent"}',
            stderr="",
        )


class Clock:
    """A settable clock. R11's backoff is a *deadline*, so time must move."""

    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now

    def past_deadline(self, row: dict) -> None:
        """Move to just after *row*'s persisted retry deadline."""
        deadline = row.get("next_attempt_at")
        if deadline is not None:
            self.now = float(deadline) + 0.001


class RetryCase(unittest.TestCase):
    def setUp(self) -> None:
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(SCRATCH), prefix="r11-")
        self.addCleanup(self._tmp.cleanup)
        self.clock = Clock()
        self.store = Store(Path(self._tmp.name), clock=self.clock)
        self.addCleanup(self.store.close)

    def notifier(self, sender: Sender) -> Notifier:
        return Notifier(sender, target="telegram", store=self.store)

    def enqueue(self, dedupe: str = "r11:1") -> int:
        return self.store.enqueue_notice(
            dedupe,
            kind="success",
            message_key="success",
            params=PARAMS,
            target="telegram",
            now=1_800_000_000.0,
        )

    def row(self, notice_id: int) -> dict:
        for row in self.store.list_notices():
            if int(row["id"]) == int(notice_id):
                return row
        raise AssertionError(f"no outbox row {notice_id}")


class FailedDeliveryIsRetried(RetryCase):
    def test_failed_delivery_must_be_retried_on_next_flush(self):
        """The reviewer's counterexample: fail once, succeed on the second call.

        R11's backoff means "next flush" is the first one *after* the persisted
        deadline; the clock is advanced to it rather than the row being retried
        at the same instant (that would be the defect the backoff fixes).
        """
        sender = Sender(failures=1)
        notice = self.enqueue()
        self.notifier(sender).flush()
        self.clock.past_deadline(self.row(notice))
        self.notifier(sender).flush()
        self.assertEqual(sender.calls, 2, "the second flush never retried the failed row")
        self.assertEqual(self.row(notice)["state"], "sent")

    def test_a_backlog_of_queued_rows_is_drained_after_a_failure(self):
        """My variant: a failed row must not block the rows queued behind it."""
        sender = Sender(failures=1)
        first = self.enqueue("r11:a")
        second = self.enqueue("r11:b")
        third = self.enqueue("r11:c")
        self.notifier(sender).flush(limit=1)
        self.assertEqual(self.row(first)["state"], "failed")
        self.assertEqual(self.row(second)["state"], "queued")
        self.clock.past_deadline(self.row(first))
        self.notifier(sender).flush(limit=10)
        states = {self.row(n)["state"] for n in (first, second, third)}
        self.assertEqual(states, {"sent"}, f"backlog not drained: {states}")


class AttemptsAreBounded(RetryCase):
    def test_retries_stop_at_the_cap_and_the_row_stays_visible(self):
        """My variant: an always-failing send must not retry forever."""
        from reset_rescue.notify import MAX_DELIVERY_ATTEMPTS

        sender = Sender(failures=10_000)
        notice = self.enqueue()
        for _ in range(MAX_DELIVERY_ATTEMPTS + 3):
            self.notifier(sender).flush()
            self.clock.past_deadline(self.row(notice))
        self.assertEqual(
            sender.calls,
            MAX_DELIVERY_ATTEMPTS,
            f"expected {MAX_DELIVERY_ATTEMPTS} attempts, saw {sender.calls}",
        )
        row = self.row(notice)
        self.assertEqual(row["state"], "abandoned")
        self.assertEqual(int(row["attempts"]), MAX_DELIVERY_ATTEMPTS)
        self.assertEqual(row["last_error_code"], "send_failed")


class PermanentErrorsAreSeparate(RetryCase):
    def test_a_missing_home_channel_is_blocked_not_retried(self):
        """My variant: a configuration error is not a transient failure."""
        sender = Sender(failures=10_000, stderr="Error: no home channel for telegram")
        notice = self.enqueue()
        self.notifier(sender).flush()
        self.notifier(sender).flush()
        self.assertEqual(sender.calls, 1, "a permanent configuration error was retried")
        row = self.row(notice)
        self.assertEqual(row["state"], "blocked")
        self.assertEqual(row["last_error_code"], "no_home_channel")
        self.assertEqual(
            [r["id"] for r in self.store.list_notices(state="failed")],
            [],
            "a permanent error must not be filed as a retryable failure",
        )

    def test_an_unrenderable_notice_is_blocked_without_a_send(self):
        """My variant: a catalog refusal is permanent and never reaches the CLI.

        ``messages.render`` refuses a parameter the kind does not declare, so a
        row written by an older/newer build (or a corrupted ``params_json``)
        cannot be sent. Retrying it can never succeed.
        """
        sender = Sender()
        notice = self.store.enqueue_notice(
            "r11:bad",
            kind="success",
            message_key="success",
            params={"label": "x", "chat_id": "-1001234567890"},  # undeclared key
            target="telegram",
            now=1_800_000_000.0,
        )
        self.notifier(sender).flush()
        self.notifier(sender).flush()
        self.assertEqual(sender.calls, 0, "an unrenderable notice reached the CLI")
        self.assertEqual(self.row(notice)["state"], "blocked")


if __name__ == "__main__":
    unittest.main(verbosity=2)
