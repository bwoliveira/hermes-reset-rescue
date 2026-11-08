#!/usr/bin/env python3
"""T-07 — store.py: schema, migrations, BEGIN IMMEDIATE, run lock, 0600.

Every test here is offline: a temp data dir under the scratch root, a fake
clock, no network, no real ``HERMES_HOME``.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for entry in (str(ROOT), str(HERE)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from reset_rescue import store as store_mod  # noqa: E402
from reset_rescue.store import LockTimeout, StateReadOnly, StateUnreadable, Store  # noqa: E402
from tests.support.isolation import scratch_root  # noqa: E402

#: A scratch root **outside the real profile**. It used to be
#: ``<HERMES_HOME>/cache/scratch/rr-l2`` — inside ``~/.hermes``, which Hermes
#: resolves back to the real root, so every temp DB landed in real state
#: (R17). ``tests.support.isolation`` refuses a root under ``$HOME``.
SCRATCH = scratch_root()
BASE = 1_800_000_000.0
DAY = 86_400.0


class FakeClock:
    def __init__(self, start: float = BASE) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(SCRATCH), prefix="store-")
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name) / "plugin-data" / "reset-rescue"
        self.clock = FakeClock()
        self.store = self.open()

    def open(self, **kwargs) -> Store:
        store = Store(self.data_dir, clock=self.clock, **kwargs)
        self.addCleanup(store.close)
        return store

    # -- helpers ---------------------------------------------------------
    def credit(self, key: str = "c" * 16, **overrides) -> dict:
        row = {
            "credit_key": key,
            "account_key": "a" * 16,
            "title": "Reset",
            "reset_type": "codex_rate_limits",
            "granted_at": self.clock() - 60,
            "expires_at": self.clock() + 3600,
            "backend_state": "available",
        }
        row.update(overrides)
        return row


class PermissionsTest(StoreCase):
    def test_data_dir_is_0700_and_db_files_are_0600(self):
        self.store.upsert_credit(**self.credit())
        self.assertEqual(stat.S_IMODE(self.data_dir.stat().st_mode), 0o700)
        for name in ("data.db", "data.db-wal", "data.db-shm"):
            path = self.data_dir / name
            if not path.exists():
                continue
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, name)

    def test_lock_file_is_0600(self):
        with self.store.run_lock(wait=1.0):
            pass
        lock = self.data_dir / "run.lock"
        self.assertTrue(lock.exists())
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)

    def test_permissions_are_repaired_on_reopen(self):
        self.store.upsert_credit(**self.credit())
        self.store.close()
        os.chmod(self.data_dir / "data.db", 0o644)
        reopened = self.open()
        reopened.upsert_credit(**self.credit(key="d" * 16))
        self.assertEqual(stat.S_IMODE((self.data_dir / "data.db").stat().st_mode), 0o600)


class SchemaTest(StoreCase):
    def test_schema_version_and_salt_are_recorded_once(self):
        self.assertEqual(self.store.schema_version, store_mod.SCHEMA_VERSION)
        salt = self.store.salt()
        self.assertEqual(len(salt), 64)
        self.assertEqual(self.open().salt(), salt, "the salt must be created once")

    def test_tables_exist(self):
        names = {
            row[0]
            for row in self.store.connection().execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for table in (
            "meta",
            "accounts",
            "credits",
            "slots",
            "owned_jobs",
            "outbox",
            "history",
            "runs",
        ):
            self.assertIn(table, names)

    def test_newer_schema_opens_read_only(self):
        self.store.set_meta("schema_version", str(store_mod.SCHEMA_VERSION + 5))
        self.store.close()
        newer = self.open()
        self.assertEqual(newer.problem_code, "schema_newer")
        self.assertTrue(newer.read_only)
        with self.assertRaises(StateReadOnly):
            newer.upsert_credit(**self.credit())
        # A read still works, and the newer version is never overwritten.
        self.assertEqual(newer.get_meta("schema_version"), str(store_mod.SCHEMA_VERSION + 5))

    def test_unreadable_db_is_never_recreated(self):
        self.store.close()
        path = self.data_dir / "data.db"
        for suffix in ("-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)
        path.write_bytes(b"this is not a sqlite database at all" * 10)
        before = path.read_bytes()
        broken = self.open()
        self.assertEqual(broken.problem_code, "state_unreadable")
        with self.assertRaises(StateUnreadable):
            broken.upsert_credit(**self.credit())
        self.assertEqual(path.read_bytes(), before, "the unreadable DB was rewritten")

    def test_migration_is_forward_only_and_in_one_transaction(self):
        self.store.set_meta("schema_version", "0")
        self.store.close()
        migrated = self.open()
        self.assertEqual(migrated.schema_version, store_mod.SCHEMA_VERSION)
        self.assertIsNone(migrated.problem_code)


class TransactionTest(StoreCase):
    def test_mutations_use_begin_immediate(self):
        seen: list[str] = []
        self.store.connection().set_trace_callback(seen.append)
        self.store.upsert_credit(**self.credit())
        self.store.connection().set_trace_callback(None)
        upper = [sql.upper() for sql in seen]
        self.assertTrue(any(sql.startswith("BEGIN IMMEDIATE") for sql in upper), seen)
        begin = next(i for i, sql in enumerate(upper) if sql.startswith("BEGIN IMMEDIATE"))
        insert = next(i for i, sql in enumerate(upper) if sql.startswith("INSERT INTO CREDITS"))
        self.assertLess(begin, insert)
        self.assertTrue(any(sql.startswith("COMMIT") for sql in upper), seen)

    def test_a_failed_mutation_rolls_back(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.transaction() as conn:
                conn.execute("INSERT INTO meta(key, value) VALUES('dup', '1')")
                conn.execute("INSERT INTO meta(key, value) VALUES('dup', '2')")
        self.assertIsNone(self.store.get_meta("dup"))

    def test_nested_transaction_does_not_begin_twice(self):
        seen: list[str] = []
        with self.store.transaction():
            self.store.connection().set_trace_callback(seen.append)
            self.store.set_meta("nested", "yes")
            self.store.connection().set_trace_callback(None)
        begins = [sql for sql in seen if sql.upper().startswith("BEGIN")]
        self.assertEqual(begins, [], seen)
        self.assertEqual(self.store.get_meta("nested"), "yes")


class RunLockTest(StoreCase):
    def test_lock_is_exclusive_across_open_file_descriptions(self):
        other = os.open(self.data_dir / "run.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, other)
        fcntl.flock(other, fcntl.LOCK_EX)
        try:
            with self.assertRaises(LockTimeout) as caught:
                with self.store.run_lock(wait=0.25, poll=0.02):
                    self.fail("the lock was not exclusive")
            self.assertEqual(caught.exception.code, "lock_timeout")
        finally:
            fcntl.flock(other, fcntl.LOCK_UN)

    def test_lock_is_released_after_the_block(self):
        with self.store.run_lock(wait=0.5):
            pass
        with self.store.run_lock(wait=0.5):
            pass

    def test_lock_is_released_when_the_body_raises(self):
        with self.assertRaises(RuntimeError):
            with self.store.run_lock(wait=0.5):
                raise RuntimeError("boom")
        with self.store.run_lock(wait=0.5):
            pass

    def test_first_waiter_wins_and_the_second_still_times_out(self):
        first = self.open()
        with first.run_lock(wait=0.5):
            with self.assertRaises(LockTimeout):
                with self.store.run_lock(wait=0.2, poll=0.02):
                    self.fail("two holders at once")

    def test_a_second_process_waits_then_times_out_with_lock_timeout(self):
        script = (
            "import sys, time\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from reset_rescue.store import Store, LockTimeout\n"
            f"store = Store({str(self.data_dir)!r})\n"
            "try:\n"
            "    with store.run_lock(wait=0.3, poll=0.02):\n"
            "        print('acquired')\n"
            "except LockTimeout as exc:\n"
            "    print('lock_timeout:' + exc.code)\n"
        )
        import subprocess

        python = os.path.basename(sys.executable)
        with self._allow_subprocess(python):
            with self.store.run_lock(wait=1.0):
                proc = subprocess.run(
                    [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
                )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "lock_timeout:lock_timeout")
            # Once released, a new process gets it immediately.
            proc = subprocess.run(
                [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
            )
        self.assertEqual(proc.stdout.strip(), "acquired", proc.stderr)

    @staticmethod
    def _allow_subprocess(*names: str):
        """Let the offline guard through for one real second-process probe."""
        try:
            from offline_guard import allow_subprocess  # noqa: PLC0415
        except ImportError:  # the guard is optional when run directly
            import contextlib

            return contextlib.nullcontext()
        return allow_subprocess(*names)


class CreditStateTest(StoreCase):
    def test_request_id_is_created_once_and_reused(self):
        self.store.upsert_credit(**self.credit())
        first = self.store.ensure_request_id("c" * 16)
        second = self.store.ensure_request_id("c" * 16)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 36)
        self.assertEqual(self.store.get_credit("c" * 16)["redeem_request_id"], first)

    def test_begin_redeem_persists_intent_and_uuid(self):
        self.store.upsert_credit(**self.credit())
        request_id = self.store.begin_redeem("c" * 16, self.clock())
        row = self.store.get_credit("c" * 16)
        self.assertEqual(row["status"], "redeeming")
        self.assertEqual(row["posted_at"], self.clock())
        self.assertEqual(row["redeem_request_id"], request_id)
        self.assertEqual(row["attempts"], 1)
        # A second attempt keeps the same UUID (I2).
        again = self.store.begin_redeem("c" * 16, self.clock() + 10)
        self.assertEqual(again, request_id)
        self.assertEqual(self.store.get_credit("c" * 16)["attempts"], 2)

    def test_upsert_keeps_the_request_id_and_posted_at(self):
        self.store.upsert_credit(**self.credit())
        request_id = self.store.begin_redeem("c" * 16, self.clock())
        self.store.upsert_credit(**self.credit(expires_at=self.clock() + 7200))
        row = self.store.get_credit("c" * 16)
        self.assertEqual(row["redeem_request_id"], request_id)
        self.assertIsNotNone(row["posted_at"])
        self.assertEqual(row["expires_at"], self.clock() + 7200)

    def test_settle_records_confirmation(self):
        self.store.upsert_credit(**self.credit())
        self.store.begin_redeem("c" * 16, self.clock())
        self.store.settle_credit("c" * 16, "redeemed", "reset", now=self.clock() + 5)
        row = self.store.get_credit("c" * 16)
        self.assertEqual(row["status"], "redeemed")
        self.assertEqual(row["confirmed_at"], self.clock() + 5)
        self.assertIsNone(row["last_failure_category"])

    def test_fail_keeps_the_credit_unsettled(self):
        self.store.upsert_credit(**self.credit())
        self.store.fail_credit("c" * 16, "backend_error", "transient", now=self.clock())
        row = self.store.get_credit("c" * 16)
        self.assertEqual(row["status"], "failed")
        self.assertIsNone(row["confirmed_at"])
        self.assertEqual(row["last_failure_category"], "transient")

    def test_list_credits_filters_by_account_and_status(self):
        self.store.upsert_credit(**self.credit(key="1" * 16))
        self.store.upsert_credit(**self.credit(key="2" * 16, account_key="b" * 16))
        self.assertEqual(len(self.store.list_credits()), 2)
        self.assertEqual(len(self.store.list_credits(account_key="b" * 16)), 1)
        self.assertEqual(len(self.store.list_credits(statuses=("seen",))), 2)
        self.assertEqual(len(self.store.list_credits(statuses=("redeemed",))), 0)


class SlotTest(StoreCase):
    def test_slot_upsert_and_mark_ran(self):
        self.store.upsert_credit(**self.credit())
        self.store.upsert_slot(
            "c" * 16,
            "primary",
            fire_at=self.clock() + 600,
            job_id="job1",
            job_name="reset-rescue-cccccccc",
            state="armed",
            deliver="local",
        )
        slot = self.store.get_slot("c" * 16, "primary")
        self.assertEqual(slot["job_id"], "job1")
        self.assertIsNone(slot["ran_at"])
        self.store.mark_slot_ran("c" * 16, "primary", self.clock())
        self.assertEqual(self.store.get_slot("c" * 16, "primary")["ran_at"], self.clock())
        self.store.clear_slot_ran("c" * 16, "primary")
        self.assertIsNone(self.store.get_slot("c" * 16, "primary")["ran_at"])

    def test_slots_are_unique_per_credit_and_role(self):
        self.store.upsert_credit(**self.credit())
        self.store.upsert_slot("c" * 16, "primary", fire_at=self.clock() + 600)
        self.store.upsert_slot("c" * 16, "primary", fire_at=self.clock() + 900)
        self.store.upsert_slot("c" * 16, "backup", fire_at=self.clock() + 1200)
        self.assertEqual(len(self.store.list_slots()), 2)
        self.assertEqual(self.store.get_slot("c" * 16, "primary")["fire_at"], self.clock() + 900)

    def test_role_check_rejects_an_unknown_role(self):
        self.store.upsert_credit(**self.credit())
        with self.assertRaises(ValueError):
            self.store.upsert_slot("c" * 16, "manual", fire_at=self.clock())
        # The table's own CHECK is the second line of defence.
        with self.assertRaises(sqlite3.IntegrityError):
            with self.store.transaction() as conn:
                conn.execute(
                    "INSERT INTO slots(credit_key, role, fire_at, created_at,"
                    " updated_at) VALUES(?, 'manual', ?, ?, ?)",
                    ("c" * 16, self.clock(), self.clock(), self.clock()),
                )


class OutboxTest(StoreCase):
    def test_dedupe_key_is_unique(self):
        first = self.store.enqueue_notice(
            "success:c:1",
            kind="success",
            message_key="success",
            params={"label": "b***@x"},
            target="local",
            now=self.clock(),
        )
        second = self.store.enqueue_notice(
            "success:c:1",
            kind="success",
            message_key="success",
            params={"label": "b***@x"},
            target="local",
            now=self.clock(),
        )
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.list_notices()), 1)

    def test_params_are_stored_as_json_and_never_as_a_body(self):
        self.store.enqueue_notice(
            "k",
            kind="success",
            message_key="success",
            params={"label": "b***@x", "pct": 4},
            target="local",
            now=self.clock(),
        )
        row = self.store.list_notices()[0]
        self.assertEqual(json.loads(row["params_json"])["label"], "b***@x")

    def test_mark_sent_and_failed(self):
        notice_id = self.store.enqueue_notice(
            "k", kind="success", message_key="success", params={}, target="local", now=self.clock()
        )
        self.store.mark_notice_sent(notice_id, "m1", self.clock())
        row = self.store.list_notices()[0]
        self.assertEqual(row["state"], "sent")
        self.assertEqual(row["message_id"], "m1")
        other = self.store.enqueue_notice(
            "k2", kind="failure", message_key="failure", params={}, target="local", now=self.clock()
        )
        self.store.mark_notice_failed(other, "send_failed", self.clock())
        self.assertEqual(
            self.store.list_notices(state="failed")[0]["last_error_code"], "send_failed"
        )


class HistoryAndRunsTest(StoreCase):
    def test_history_is_capped(self):
        self.store.add_history(
            [
                {"at": self.clock(), "action": "scan", "outcome": "ok"}
                for _ in range(store_mod.HISTORY_CAP + 120)
            ]
        )
        self.assertEqual(self.store.count_history(), store_mod.HISTORY_CAP)

    def test_history_list_is_newest_first(self):
        self.store.add_history(
            [{"at": self.clock() + i, "action": "scan", "outcome": "ok"} for i in range(3)]
        )
        rows = self.store.list_history(limit=2)
        self.assertEqual([row["at"] for row in rows], [self.clock() + 2, self.clock() + 1])

    def test_stale_runs_are_closed_as_interrupted(self):
        dead = self.store.start_run("scan", None, 999_999, self.clock())
        live = self.store.start_run("fire", "primary", os.getpid(), self.clock())
        healed = self.store.heal_runs(self.clock() + 60)
        self.assertEqual(healed, 1)
        rows = {row["id"]: row for row in self.store.list_runs()}
        self.assertEqual(rows[dead]["outcome"], "interrupted")
        self.assertIsNotNone(rows[dead]["ended_at"])
        self.assertIsNone(rows[live]["ended_at"])

    def test_finish_run_records_the_outcome(self):
        run_id = self.store.start_run("fire", "backup", os.getpid(), self.clock())
        self.store.finish_run(run_id, "ok", self.clock() + 3)
        row = self.store.list_runs()[0]
        self.assertEqual(row["outcome"], "ok")
        self.assertEqual(row["ended_at"], self.clock() + 3)


class RetentionTest(StoreCase):
    def test_settled_credits_are_dropped_after_fourteen_days(self):
        self.store.upsert_credit(**self.credit(key="old" * 5 + "x"))
        self.store.upsert_credit(**self.credit(key="new" * 5 + "y"))
        self.store.settle_credit("old" * 5 + "x", "redeemed", "reset", now=self.clock())
        self.clock.advance(15 * DAY)
        # The second credit settles now, so only the first is old enough.
        self.store.settle_credit("new" * 5 + "y", "redeemed", "reset", now=self.clock())
        self.store.upsert_credit(**self.credit(key="new" * 5 + "y"))
        pruned = self.store.prune(self.clock())
        self.assertEqual(pruned["credits"], 1)
        self.assertIsNone(self.store.get_credit("old" * 5 + "x"))
        self.assertIsNotNone(self.store.get_credit("new" * 5 + "y"))

    def test_unsettled_credits_are_kept(self):
        self.store.upsert_credit(**self.credit())
        self.clock.advance(30 * DAY)
        self.store.prune(self.clock())
        self.assertIsNotNone(self.store.get_credit("c" * 16))

    def test_sent_notices_are_dropped_after_fourteen_days(self):
        notice_id = self.store.enqueue_notice(
            "k", kind="success", message_key="success", params={}, target="local", now=self.clock()
        )
        self.store.mark_notice_sent(notice_id, "m1", self.clock())
        self.clock.advance(15 * DAY)
        pruned = self.store.prune(self.clock())
        self.assertEqual(pruned["notices"], 1)

    def test_queued_notices_survive(self):
        self.store.enqueue_notice(
            "k", kind="success", message_key="success", params={}, target="local", now=self.clock()
        )
        self.clock.advance(30 * DAY)
        self.store.prune(self.clock())
        self.assertEqual(len(self.store.list_notices(state="queued")), 1)


class OwnedJobsTest(StoreCase):
    def test_jobs_are_recorded_and_marked_removed(self):
        self.store.record_job(
            "job1", "reset-rescue-scan", "scan", "reset-rescue-scan.py", self.clock()
        )
        self.assertEqual(self.store.list_owned_jobs()[0]["job_name"], "reset-rescue-scan")
        self.store.mark_job_removed("job1", self.clock())
        self.assertIsNotNone(self.store.list_owned_jobs()[0]["removed_at"])
        self.assertEqual(self.store.list_owned_jobs(active_only=True), [])

    def test_job_name_is_unique(self):
        self.store.record_job("job1", "reset-rescue-scan", "scan", "s.py", self.clock())
        # Re-recording the same name replaces the old row (a re-created job).
        self.store.record_job("job2", "reset-rescue-scan", "scan", "s.py", self.clock())
        self.assertEqual([row["job_id"] for row in self.store.list_owned_jobs()], ["job2"])
        # A different name with the same id is a plain update, never a duplicate.
        self.store.record_job("job2", "reset-rescue-scan", "scan", "s.py", self.clock())
        self.assertEqual(len(self.store.list_owned_jobs()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
