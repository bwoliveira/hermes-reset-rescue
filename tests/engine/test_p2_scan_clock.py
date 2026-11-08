"""D-023 follow-up (P2): the "Checked HH:MM" clock must reach the popup.

The real profile had finished scan runs and a fresh ``accounts.last_list_ok_at``
but the popup still said "Not checked yet": ``service._status_schedule`` reads
``meta['last_scan_at']`` and **nothing ever wrote that key**. These tests pin the
two halves of the repair:

* a finished scan records ``last_scan_at`` / ``last_scan_outcome`` in ``meta``;
* a profile whose meta keys are missing (every install so far) still reports the
  newest finished scan from the ``runs`` table, so the popup heals without a
  fresh scan.
"""

from __future__ import annotations

import unittest

from reset_rescue.service import Service
from tests.fakes import FakeStore, make_deps


def service_with(store=None, **kwargs) -> tuple:
    store = store or FakeStore()
    deps = make_deps(store=store, **kwargs)
    return Service(deps), deps, store


def run(
    run_id: int,
    *,
    kind: str = "scan",
    role=None,
    started: float,
    ended,
    outcome: str = "ok",
) -> dict:
    """One ``runs`` row, shaped like the real table."""
    return {
        "id": run_id,
        "kind": kind,
        "role": role,
        "pid": 1,
        "started_at": started,
        "ended_at": ended,
        "outcome": outcome,
    }


def seed_account(store: FakeStore, key: str = "a" * 16, label: str = "b***@e***.com") -> None:
    store.account_upsert(
        {
            "account_key": key,
            "label": label,
            "source_kind": "pool",
            "last_list_ok_at": 1_800_000_000.0,
            "last_error_code": None,
        }
    )


def seed_credit(store: FakeStore, key: str = "c" * 16, account_key: str = "a" * 16) -> None:
    store.credit_upsert(
        {
            "credit_key": key,
            "account_key": account_key,
            "title": "Full reset",
            "reset_type": "codex",
            "granted_at": 1_799_000_000.0,
            "expires_at": 1_800_100_000.0,
            "backend_state": "available",
            "state": "available",
            "plan": "beyond_horizon",
            "last_result_code": None,
            "last_result_at": None,
            "last_result_reason": None,
            "notified_outcome": None,
            "notified_success": None,
        }
    )


class ScheduleClock(unittest.TestCase):
    """``schedule.last_scan_at`` is the last finished scan, however it was recorded."""

    def test_meta_keys_are_reported(self):
        store = FakeStore()
        store.meta_set("last_scan_at", "1800000000.0")
        store.meta_set("last_scan_outcome", "ok")
        service, _deps, _store = service_with(store)
        schedule = service.status().schedule
        self.assertEqual(schedule.last_scan_at, "2027-01-15T08:00:00Z")
        self.assertEqual(schedule.last_scan_outcome, "ok")

    def test_a_finished_run_is_the_fallback_when_meta_is_missing(self):
        """Every install so far: runs exist, meta['last_scan_at'] never did."""
        store = FakeStore()
        seed_account(store)
        seed_credit(store)
        store.runs = [
            {
                "id": 1,
                "kind": "scan",
                "role": None,
                "pid": 1,
                "started_at": 1_799_999_000.0,
                "ended_at": 1_799_999_010.0,
                "outcome": "ok",
            },
            {
                "id": 2,
                "kind": "scan",
                "role": None,
                "pid": 1,
                "started_at": 1_800_000_000.0,
                "ended_at": 1_800_000_005.0,
                "outcome": "ok",
            },
        ]
        service, _deps, _store = service_with(store)
        schedule = service.status().schedule
        self.assertEqual(schedule.last_scan_at, "2027-01-15T08:00:05Z", "newest finished scan")
        self.assertEqual(schedule.last_scan_outcome, "ok")

    def test_an_unfinished_run_is_not_a_scan_yet(self):
        store = FakeStore()
        store.runs = [
            {
                "id": 3,
                "kind": "scan",
                "role": None,
                "pid": 1,
                "started_at": 1_800_000_000.0,
                "ended_at": None,
                "outcome": None,
            },
            {
                "id": 2,
                "kind": "scan",
                "role": None,
                "pid": 1,
                "started_at": 1_799_999_000.0,
                "ended_at": 1_799_999_005.0,
                "outcome": "failed",
            },
        ]
        service, _deps, _store = service_with(store)
        schedule = service.status().schedule
        self.assertEqual(schedule.last_scan_at, "2027-01-15T07:43:25Z")
        self.assertEqual(schedule.last_scan_outcome, "failed")

    def test_a_non_scan_run_is_ignored(self):
        store = FakeStore()
        store.runs = [
            {
                "id": 4,
                "kind": "fire",
                "role": "primary",
                "pid": 1,
                "started_at": 1_800_000_500.0,
                "ended_at": 1_800_000_600.0,
                "outcome": "ok",
            },
        ]
        service, _deps, _store = service_with(store)
        self.assertIsNone(service.status().schedule.last_scan_at)

    def test_no_store_still_answers_none(self):
        deps = make_deps(store=None)
        deps.store = None
        self.assertIsNone(Service(deps).status().schedule.last_scan_at)

    def test_meta_wins_over_an_older_run(self):
        store = FakeStore()
        store.meta_set("last_scan_at", "1800000900.0")
        store.meta_set("last_scan_outcome", "ok")
        store.runs = [
            {
                "id": 2,
                "kind": "scan",
                "role": None,
                "pid": 1,
                "started_at": 1_800_000_000.0,
                "ended_at": 1_800_000_005.0,
                "outcome": "ok",
            },
        ]
        service, _deps, _store = service_with(store)
        self.assertEqual(service.status().schedule.last_scan_at, "2027-01-15T08:15:00Z")


class PlannerRecordsTheClock(unittest.TestCase):
    """A scan writes the two meta keys the popup reads (the missing writer)."""

    def test_a_finished_scan_records_last_scan_at_and_outcome(self):
        from tests.flows.support import HOUR, Harness

        class Case(unittest.TestCase):
            pass

        case = Case()
        harness = Harness(case)
        try:
            account = harness.add_account()
            harness.add_credit(account, expires_in=30 * HOUR)
            started = harness.clock.now()
            result = harness.scan()
            self.assertEqual(result.exit_code, 0)
            raw = harness.store.get_meta("last_scan_at")
            self.assertIsNotNone(raw, "the scan must record when it ran")
            self.assertGreaterEqual(float(raw), started)
            self.assertEqual(harness.store.get_meta("last_scan_outcome"), "ok")
        finally:
            case.doCleanups()

    def test_a_failed_scan_records_the_failure_outcome(self):
        from tests.flows.support import Harness

        class Case(unittest.TestCase):
            pass

        case = Case()
        harness = Harness(case)
        try:
            harness.source.fail_accounts = "network"
            harness.add_account()
            result = harness.scan()
            self.assertNotEqual(result.exit_code, 0)
            self.assertEqual(harness.store.get_meta("last_scan_outcome"), "failed")
            self.assertIsNotNone(harness.store.get_meta("last_scan_at"))
        finally:
            case.doCleanups()

    def test_a_dry_run_scan_still_records_the_clock(self):
        from tests.flows.support import HOUR, Harness

        class Case(unittest.TestCase):
            pass

        case = Case()
        harness = Harness(case)
        try:
            account = harness.add_account()
            harness.add_credit(account, expires_in=30 * HOUR)
            harness.scan(dry_run=True)
            self.assertIsNotNone(harness.store.get_meta("last_scan_at"))
        finally:
            case.doCleanups()


class StatusAfterAScan(unittest.TestCase):
    """The end-to-end shape the popup sees: scan once, then status reports it."""

    def test_status_reports_the_scan_that_just_ran(self):
        from tests.flows.support import HOUR, Harness

        class Case(unittest.TestCase):
            pass

        case = Case()
        harness = Harness(case)
        try:
            account = harness.add_account()
            harness.add_credit(account, expires_in=30 * HOUR)
            harness.scan()
            service = Service(harness.deps)
            schedule = service.status().schedule
            self.assertIsNotNone(schedule.last_scan_at, "the popup would say 'Not checked yet'")
            self.assertEqual(schedule.last_scan_outcome, "ok")
            # Rendered exactly like the Desktop's formatRecent: same day → HH:MM.
            self.assertRegex(schedule.last_scan_at, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        finally:
            case.doCleanups()
