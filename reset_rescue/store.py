"""SQLite state for Reset Rescue (A§6).

One ``Store`` per operation, opened against ``<HERMES_HOME>/plugin-data/
reset-rescue/`` (A§6). Nothing here caches a home, a connection or settings
across calls: ``Store.for_plugin()`` resolves the plugin data dir on every call
so one process can serve several profiles (plugin-dev core rule 4).

Design notes that differ from, or extend, A§6:

* The DB is opened here rather than through ``plugins.plugin_storage.plugin_db``
  because A§6 requires ``busy_timeout=10000`` and every mutation in an explicit
  ``BEGIN IMMEDIATE``; ``plugin_db`` opens with a 5 s busy timeout and leaves
  transactions to the caller. The file is the same one ``plugin_db`` names
  (``data.db`` under ``plugin_data_dir("reset-rescue")``).
* ``credits.notified_outcome`` is added to A§6's column list: the production
  redeemer's R9 fix ("announce each outcome once") needs it, and the planner's
  missed alert is encoded as ``notified_outcome='missed'``.
* Slot roles are ``reminder1``, ``reminder2``, ``primary`` and ``backup``
  (simplification §3). There is no rehearsal role: the rehearsal world was cut,
  and ``dry_run`` covers that need with real GETs and zero spend.

Guarantees enforced here:

* ``data.db``, ``data.db-wal``, ``data.db-shm`` and ``run.lock`` are 0600 and the
  data dir is 0700, re-applied on every open and after every commit.
* Every mutation runs inside ``BEGIN IMMEDIATE`` (``isolation_level=None`` plus
  an explicit ``BEGIN``), so two processes cannot interleave a read-modify-write.
* A newer ``schema_version`` opens read-only (``problem_code='schema_newer'``);
  an unreadable DB is never recreated (``problem_code='state_unreadable'``) and
  every mutation refuses.
* ``run_lock`` is an ``fcntl.flock`` on ``run.lock``, re-entrant in-process so
  the planner can call ``redeem_one`` while holding it.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

__all__ = [
    "Store",
    "LockTimeout",
    "StateReadOnly",
    "StateUnreadable",
    "StateError",
    "SCHEMA_VERSION",
    "HISTORY_CAP",
    "RETENTION_S",
    "LOCK_WAIT_S",
    "SETTLED_STATUSES",
    "CONSUMED_STATUSES",
    "UNCONSUMED_STATUSES",
    "REAL_ROLES",
    "CREDIT_STATUSES",
    "pid_alive",
]

SCHEMA_VERSION = 3
HISTORY_CAP = 5_000
RETENTION_S = 14 * 86_400.0
LOCK_WAIT_S = 120.0
LOCK_POLL_S = 0.05
BUSY_TIMEOUT_MS = 10_000

#: R11 backoff: the wait before retry *n* is ``RETRY_BASE_DELAY_S * 2**(n-1)``,
#: capped at ``RETRY_MAX_DELAY_S``. Five attempts therefore span roughly
#: 30s + 1m + 2m + 4m of a transient messaging outage instead of one instant,
#: which is what the attempt cap alone failed to do. The cap keeps a row from
#: parking for the whole 14-day retention window.
RETRY_BASE_DELAY_S = 30.0
RETRY_MAX_DELAY_S = 3_600.0


def retry_delay_s(attempts: int) -> float:
    """Seconds to wait before the retry that follows *attempts* failures.

    ``attempts`` is the number that just failed (1 for the first), so the first
    retry waits ``RETRY_BASE_DELAY_S`` and each later one doubles. A non-positive
    or unparsable count is treated as the first failure rather than crashing a
    flush: the outbox is a side channel and must never break a run.
    """
    try:
        count = max(1, int(attempts))
    except (TypeError, ValueError):
        count = 1
    return min(RETRY_BASE_DELAY_S * (2.0 ** (count - 1)), RETRY_MAX_DELAY_S)


REAL_ROLES = ("primary", "backup")
SLOT_ROLES = ("reminder1", "reminder2", "primary", "backup")

#: A credit in one of these states has a terminal verdict from backend evidence
#: (or from the planner's miss rule) and is retention-eligible. ``redeeming`` and
#: ``scheduled`` are deliberately absent: they are still in flight.
#:
#: The set is split in two because the two halves age differently (R5): a
#: **consumed** credit is gone from the backend, so its row may go 14 days after
#: the verdict; an **unconsumed** one may still be sitting in the user's account
#: (`nothing_to_reset` means exactly that, and a failed POST spent nothing), so
#: its row — and with it the one ``redeem_request_id`` this credit must ever
#: have — has to outlive the credit itself.
CONSUMED_STATUSES = (
    "redeemed",
    "already_redeemed",
    "vanished",
    "missed",
    "expired",
)
UNCONSUMED_STATUSES = (
    "nothing_to_reset",
    "failed",
)
SETTLED_STATUSES = CONSUMED_STATUSES + UNCONSUMED_STATUSES

CREDIT_STATUSES = (
    "seen",
    "scheduled",
    "redeeming",
    "redeemed",
    "already_redeemed",
    "nothing_to_reset",
    "vanished",
    "failed",
    "missed",
    "expired",
)

_CREDIT_COLUMNS = (
    "credit_key",
    "account_key",
    "title",
    "reset_type",
    "granted_at",
    "expires_at",
    "backend_state",
    "status",
    "redeem_request_id",
    "posted_at",
    "confirmed_at",
    "attempts",
    "last_code",
    "last_reason",
    "last_failure_category",
    "notified_success",
    "notified_outcome",
    "first_seen_at",
    "updated_at",
)

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS accounts(
    account_key TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    entry_id TEXT,
    first_seen_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    last_list_ok_at REAL,
    last_error_code TEXT
);
CREATE TABLE IF NOT EXISTS credits(
    credit_key TEXT PRIMARY KEY,
    account_key TEXT NOT NULL,
    title TEXT,
    reset_type TEXT,
    granted_at REAL,
    expires_at REAL,
    backend_state TEXT,
    status TEXT NOT NULL DEFAULT 'seen',
    redeem_request_id TEXT,
    posted_at REAL,
    confirmed_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_code TEXT,
    last_reason TEXT,
    last_failure_category TEXT,
    notified_success INTEGER NOT NULL DEFAULT 0,
    notified_outcome TEXT,
    first_seen_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS credits_account ON credits(account_key);
CREATE INDEX IF NOT EXISTS credits_status ON credits(status);
CREATE TABLE IF NOT EXISTS slots(
    credit_key TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('reminder1','reminder2','primary','backup')),
    fire_at REAL NOT NULL,
    job_id TEXT,
    job_name TEXT,
    state TEXT NOT NULL DEFAULT 'planned',
    deliver TEXT,
    ran_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(credit_key, role)
);
CREATE TABLE IF NOT EXISTS owned_jobs(
    job_id TEXT PRIMARY KEY,
    job_name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    script TEXT,
    created_at REAL NOT NULL,
    removed_at REAL
);
CREATE TABLE IF NOT EXISTS outbox(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT NOT NULL UNIQUE,
    credit_key TEXT,
    kind TEXT NOT NULL,
    message_key TEXT NOT NULL,
    params_json TEXT NOT NULL,
    target TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'queued',
    message_id TEXT,
    last_error_code TEXT,
    created_at REAL NOT NULL,
    sent_at REAL
);
CREATE INDEX IF NOT EXISTS outbox_state ON outbox(state);
CREATE TABLE IF NOT EXISTS history(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    action TEXT NOT NULL,
    credit_key TEXT,
    account_key TEXT,
    role TEXT,
    outcome TEXT NOT NULL,
    reason_code TEXT
);
CREATE TABLE IF NOT EXISTS runs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    role TEXT,
    pid INTEGER NOT NULL,
    started_at REAL NOT NULL,
    ended_at REAL,
    outcome TEXT
);
"""

#: Schema v2 (D-014): the activity log gains a bounded ``detail`` column and
#: two indexes so the History view can filter by action and page by time.
#: Guarded with a table rebuild rather than a bare ``ALTER`` so a DB that a
#: v1 build already touched cannot fail the migration with a duplicate column.
_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS history_v2(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    action TEXT NOT NULL,
    credit_key TEXT,
    account_key TEXT,
    role TEXT,
    outcome TEXT NOT NULL,
    reason_code TEXT,
    detail TEXT
);
INSERT OR IGNORE INTO history_v2(id, at, action, credit_key, account_key, role,
                                 outcome, reason_code)
    SELECT id, at, action, credit_key, account_key, role, outcome, reason_code
    FROM history;
DROP TABLE history;
ALTER TABLE history_v2 RENAME TO history;
CREATE INDEX IF NOT EXISTS history_at ON history(at);
CREATE INDEX IF NOT EXISTS history_action ON history(action);
"""

_MIGRATIONS: dict[int, str] = {1: _SCHEMA_V1, 2: _SCHEMA_V2}

#: Schema v3 (R11): the outbox gains a persisted retry deadline. An attempt cap
#: with no spacing is not backoff: per-credit cron jobs run back to back, so a
#: short messaging outage spent the whole budget in one second and the next
#: healthy scan never selected the row again. ``next_attempt_at`` is the
#: earliest instant a flush may attempt the row; NULL means "due now" (a queued
#: row, or one that is terminal).
_SCHEMA_V3 = """
ALTER TABLE outbox ADD COLUMN next_attempt_at REAL;
CREATE INDEX IF NOT EXISTS outbox_next_attempt ON outbox(state, next_attempt_at);
"""
#: A migration statement whose effect is already present is a no-op, not a
#: failure: ``ALTER TABLE ... ADD COLUMN`` is the one DDL sqlite cannot repeat,
#: and a database a v3 build opened before the version row landed already has it.

_MIGRATIONS[3] = _SCHEMA_V3


class StateError(RuntimeError):
    """Base class for a state problem the caller must surface, never swallow."""

    code = "state_error"


class LockTimeout(StateError):
    """The run lock was held by another process for longer than ``wait``."""

    code = "lock_timeout"


class StateReadOnly(StateError):
    """A mutation was attempted on a read-only store (newer schema)."""

    code = "schema_newer"


class StateUnreadable(StateError):
    """The DB file exists but cannot be read; it is never recreated."""

    code = "state_unreadable"


def pid_alive(pid: int) -> bool:
    """True when ``pid`` names a live process we may signal (A§6 healing)."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:  # pragma: no cover - defensive
        return exc.errno != errno.ESRCH
    return True


class Store:
    """One SQLite handle plus the run lock for a single plugin data dir."""

    def __init__(
        self,
        data_dir: Path | str,
        *,
        clock: Callable[[], float] | None = None,
        busy_timeout_ms: int = BUSY_TIMEOUT_MS,
        lock_wait: float = LOCK_WAIT_S,
        lock_poll: float = LOCK_POLL_S,
    ) -> None:
        self.data_dir = Path(data_dir)
        self._clock = clock or time.time
        self._busy_timeout_ms = int(busy_timeout_ms)
        self._lock_wait = float(lock_wait)
        self._lock_poll = float(lock_poll)
        self.db_path = self.data_dir / "data.db"
        self.lock_path = self.data_dir / "run.lock"
        self._conn: sqlite3.Connection | None = None
        self._depth = 0
        self._lock_depth = 0
        self._lock_fd: int | None = None
        self._read_only = False
        self._problem_code: str | None = None
        self._schema_version = SCHEMA_VERSION
        self._open()

    # ------------------------------------------------------------------ open
    @classmethod
    def for_plugin(cls, **kwargs: Any) -> "Store":
        """Open the store of the active profile (resolved per call, never cached)."""
        from plugins.plugin_storage import plugin_data_dir  # local import: host only

        return cls(plugin_data_dir("reset-rescue"), **kwargs)

    def _open(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        _chmod(self.data_dir, 0o700)
        try:
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=self._busy_timeout_ms / 1000.0,
                isolation_level=None,
                check_same_thread=False,
            )
        except sqlite3.Error as exc:  # pragma: no cover - open rarely fails
            self._problem_code = "state_unreadable"
            raise StateUnreadable(f"cannot open state: {exc}") from exc
        self._conn = conn
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
            # A corrupt file fails here, before any PRAGMA that could write.
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.DatabaseError:
            conn.close()
            self._conn = None
            self._problem_code = "state_unreadable"
            return
        self._apply_journal_mode()
        self._enforce_permissions()
        self._init_schema()

    def _apply_journal_mode(self) -> None:
        assert self._conn is not None
        try:
            row = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()
            mode = str(row[0]).lower() if row else ""
        except sqlite3.DatabaseError:
            mode = ""
        if mode != "wal":
            # Network filesystems silently refuse WAL; DELETE is the safe fallback.
            with contextlib.suppress(sqlite3.DatabaseError):
                self._conn.execute("PRAGMA journal_mode=DELETE")

    def _init_schema(self) -> None:
        assert self._conn is not None
        current = self._read_schema_version()
        if current is None:
            self._migrate_from(0)
            return
        if current > SCHEMA_VERSION:
            # Never overwrite a newer DB after a downgrade (A§6).
            self._read_only = True
            self._problem_code = "schema_newer"
            self._schema_version = current
            return
        if current < SCHEMA_VERSION:
            self._migrate_from(current)
        else:
            self._schema_version = current

    def _read_schema_version(self) -> int | None:
        assert self._conn is not None
        try:
            row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        except sqlite3.DatabaseError:
            return None
        if row is None:
            return None
        try:
            return int(str(row[0]))
        except (TypeError, ValueError):
            return None

    def _migrate_from(self, current: int) -> None:
        assert self._conn is not None
        try:
            for version in range(current + 1, SCHEMA_VERSION + 1):
                statement = _MIGRATIONS.get(version)
                if statement is None:  # pragma: no cover - programming error
                    raise StateError(f"missing migration {version}")
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    for chunk in _statements(statement):
                        self._execute_migration(chunk)
                    self._conn.execute(
                        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(version),),
                    )
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
                self._conn.execute("COMMIT")
        except sqlite3.DatabaseError as exc:
            self._problem_code = "state_unreadable"
            raise StateUnreadable(f"cannot migrate state: {exc}") from exc
        self._schema_version = SCHEMA_VERSION
        self._ensure_salt()
        self._enforce_permissions()

    def _execute_migration(self, statement: str) -> None:
        """Run one migration statement, tolerating one a previous build already applied.

        ``ALTER TABLE ... ADD COLUMN`` is not idempotent, and a database that a
        v3 build opened before the version row was written (an interrupted
        migration, or a hand-edited ``schema_version``) already has the column.
        Re-running the *same* statement must then be a no-op rather than a
        "duplicate column name" failure that makes the state unreadable.
        """
        assert self._conn is not None
        try:
            self._conn.execute(statement)
        except sqlite3.OperationalError as exc:
            text = str(exc).lower()
            if "duplicate column name" not in text and "already exists" not in text:
                raise
            # The column/index this statement adds is already there: the
            # migration's *effect* is present, which is what the version row
            # records. Nothing else in a migration statement is tolerated.

    def _ensure_salt(self) -> None:
        if self._read_only or self._conn is None:
            return
        if self.get_meta("salt") is None:
            self.set_meta("salt", uuid.uuid4().hex + uuid.uuid4().hex)

    # ------------------------------------------------------------- accessors
    def connection(self) -> sqlite3.Connection:
        """The live connection; raises when the DB is unreadable."""
        return self.connection_or_raise()

    def connection_or_raise(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StateUnreadable("state is unreadable")
        return self._conn

    @property
    def problem_code(self) -> str | None:
        return self._problem_code

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def schema_version(self) -> int:
        return self._schema_version

    def close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(sqlite3.Error):
                self._conn.close()
            self._conn = None
        if self._lock_fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(self._lock_fd)
            self._lock_fd = None
            self._lock_depth = 0

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------ permissions
    def _enforce_permissions(self) -> None:
        _chmod(self.data_dir, 0o700)
        for path in (
            self.db_path,
            Path(str(self.db_path) + "-wal"),
            Path(str(self.db_path) + "-shm"),
            self.lock_path,
        ):
            if path.exists():
                _chmod(path, 0o600)

    # ----------------------------------------------------------- transactions
    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One ``BEGIN IMMEDIATE`` transaction; nests without a second BEGIN."""
        conn = self.connection_or_raise()
        if self._read_only:
            raise StateReadOnly("state is read-only (newer schema)")
        if self._depth:
            self._depth += 1
            try:
                yield conn
            finally:
                self._depth -= 1
            return
        conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield conn
        except BaseException:
            self._depth = 0
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
        else:
            self._depth = 0
            conn.execute("COMMIT")
            self._enforce_permissions()

    def _write(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        """Run one statement in its own transaction (or the caller's)."""
        with self.transaction() as conn:
            return conn.execute(sql, tuple(params))

    # ------------------------------------------------------------------- meta
    def get_meta(self, key: str) -> str | None:
        conn = self.connection_or_raise()
        try:
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        except sqlite3.DatabaseError:
            return None
        return None if row is None else str(row[0])

    def set_meta(self, key: str, value: str) -> None:
        self._write(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def salt(self) -> str:
        value = self.get_meta("salt")
        if value:
            return value
        if self._read_only:
            raise StateReadOnly("state is read-only (newer schema)")
        self._ensure_salt()
        value = self.get_meta("salt")
        if not value:  # pragma: no cover - only on a broken DB
            raise StateUnreadable("state has no salt")
        return value

    # --------------------------------------------------------------- accounts
    def upsert_account(
        self,
        account_key: str,
        label: str,
        source_kind: str,
        entry_id: str | None = None,
        *,
        now: float | None = None,
    ) -> None:
        now = self._clock() if now is None else float(now)
        self._write(
            "INSERT INTO accounts(account_key, label, source_kind, entry_id, first_seen_at,"
            " last_seen_at) VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(account_key) DO UPDATE SET label=excluded.label,"
            " source_kind=excluded.source_kind, entry_id=excluded.entry_id,"
            " last_seen_at=excluded.last_seen_at",
            (account_key, label, source_kind, entry_id, now, now),
        )

    def mark_account_listed(self, account_key: str, now: float | None = None) -> None:
        now = self._clock() if now is None else float(now)
        self._write(
            "UPDATE accounts SET last_list_ok_at=?, last_error_code=NULL,"
            " last_seen_at=? WHERE account_key=?",
            (now, now, account_key),
        )

    def set_account_error(
        self, account_key: str, code: str | None, now: float | None = None
    ) -> None:
        now = self._clock() if now is None else float(now)
        self._write(
            "UPDATE accounts SET last_error_code=?, last_seen_at=? WHERE account_key=?",
            (code, now, account_key),
        )

    def get_account(self, account_key: str) -> dict | None:
        return self._one("SELECT * FROM accounts WHERE account_key=?", (account_key,))

    def list_accounts(self) -> list[dict]:
        return self._all("SELECT * FROM accounts ORDER BY account_key")

    # ---------------------------------------------------------------- credits
    def upsert_credit(
        self,
        credit_key: str,
        account_key: str,
        *,
        title: str | None = None,
        reset_type: str | None = None,
        granted_at: float | None = None,
        expires_at: float | None = None,
        backend_state: str | None = None,
        status: str | None = None,
        now: float | None = None,
    ) -> dict:
        """Insert or refresh a credit without disturbing its redemption state.

        The descriptive columns follow the backend; ``status``,
        ``redeem_request_id``, ``posted_at``, ``confirmed_at``, ``attempts`` and
        the notice flags belong to the flows and are only written when the
        caller passes ``status`` explicitly.
        """
        now = self._clock() if now is None else float(now)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO credits(credit_key, account_key, title, reset_type, granted_at,"
                " expires_at, backend_state, status, first_seen_at, updated_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(credit_key) DO UPDATE SET account_key=excluded.account_key,"
                " title=excluded.title, reset_type=excluded.reset_type,"
                " granted_at=excluded.granted_at, expires_at=excluded.expires_at,"
                " backend_state=excluded.backend_state, updated_at=excluded.updated_at",
                (
                    credit_key,
                    account_key,
                    title,
                    reset_type,
                    granted_at,
                    expires_at,
                    backend_state,
                    status or "seen",
                    now,
                    now,
                ),
            )
        row = self.get_credit(credit_key)
        assert row is not None
        return row

    def get_credit(self, credit_key: str) -> dict | None:
        return self._one("SELECT * FROM credits WHERE credit_key=?", (credit_key,))

    def list_credits(
        self, *, account_key: str | None = None, statuses: Sequence[str] | None = None
    ) -> list[dict]:
        sql = "SELECT * FROM credits"
        clauses: list[str] = []
        params: list[Any] = []
        if account_key is not None:
            clauses.append("account_key=?")
            params.append(account_key)
        if statuses:
            clauses.append("status IN (%s)" % ",".join("?" * len(statuses)))
            params.extend(statuses)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY expires_at IS NULL, expires_at, credit_key"
        return self._all(sql, tuple(params))

    def update_credit(self, credit_key: str, *, now: float | None = None, **fields: Any) -> None:
        """Update an allow-listed subset of credit columns."""
        unknown = set(fields) - set(_CREDIT_COLUMNS)
        if unknown:
            raise ValueError(f"unknown credit columns: {sorted(unknown)}")
        if not fields:
            return
        now = self._clock() if now is None else float(now)
        fields.setdefault("updated_at", now)
        assignments = ", ".join(f"{name}=?" for name in fields)
        params = list(fields.values()) + [credit_key]
        self._write(f"UPDATE credits SET {assignments} WHERE credit_key=?", params)

    def ensure_request_id(self, credit_key: str, now: float | None = None) -> str:
        """Return the persisted idempotency key, creating it exactly once (I2)."""
        row = self.get_credit(credit_key)
        if row is None:
            raise StateError(f"no credit row for {credit_key}")
        existing = row.get("redeem_request_id")
        if existing:
            return str(existing)
        request_id = str(uuid.uuid4())
        # Conditional write: if another process won the race, keep its UUID.
        with self.transaction() as conn:
            conn.execute(
                "UPDATE credits SET redeem_request_id=?, updated_at=?"
                " WHERE credit_key=? AND redeem_request_id IS NULL",
                (request_id, self._clock() if now is None else float(now), credit_key),
            )
        row = self.get_credit(credit_key)
        assert row is not None
        return str(row["redeem_request_id"])

    def begin_redeem(self, credit_key: str, now: float) -> str:
        """Persist ``redeeming`` + the UUID before the POST (I2, crash safety)."""
        request_id = self.ensure_request_id(credit_key, now)
        self.update_credit(
            credit_key,
            now=now,
            status="redeeming",
            posted_at=self._coalesce_posted(credit_key, now),
            attempts=self._attempts(credit_key) + 1,
        )
        return request_id

    def _coalesce_posted(self, credit_key: str, now: float) -> float:
        row = self.get_credit(credit_key) or {}
        posted = row.get("posted_at")
        return float(posted) if posted is not None else float(now)

    def _attempts(self, credit_key: str) -> int:
        row = self.get_credit(credit_key) or {}
        return int(row.get("attempts") or 0)

    def settle_credit(self, credit_key: str, status: str, code: str | None, now: float) -> None:
        """Record backend-confirmed completion; clears the failure category."""
        self.update_credit(
            credit_key,
            now=now,
            status=status,
            last_code=code,
            last_failure_category=None,
            confirmed_at=float(now),
        )

    def fail_credit(self, credit_key: str, reason: str, category: str, now: float) -> None:
        """Record a failure without ever marking the credit settled."""
        self.update_credit(
            credit_key, now=now, status="failed", last_reason=reason, last_failure_category=category
        )

    def set_credit_status(
        self, credit_key: str, status: str, now: float, *, reason: str | None = None
    ) -> None:
        fields: dict[str, Any] = {"status": status}
        if reason is not None:
            fields["last_reason"] = reason
        self.update_credit(credit_key, now=now, **fields)

    def mark_notified_success(
        self, credit_key: str, outcome: str = "success", now: float | None = None
    ) -> None:
        self.update_credit(credit_key, now=now, notified_success=1, notified_outcome=outcome)

    def mark_notified_outcome(
        self, credit_key: str, outcome: str, now: float | None = None
    ) -> None:
        self.update_credit(credit_key, now=now, notified_outcome=outcome)

    # ------------------------------------------------------------------ slots
    def upsert_slot(
        self,
        credit_key: str,
        role: str,
        *,
        fire_at: float,
        job_id: str | None = None,
        job_name: str | None = None,
        state: str = "planned",
        deliver: str | None = None,
        now: float | None = None,
    ) -> dict:
        if role not in SLOT_ROLES:
            raise ValueError(f"unknown slot role {role!r}")
        now = self._clock() if now is None else float(now)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO slots(credit_key, role, fire_at, job_id, job_name, state,"
                " deliver, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(credit_key, role) DO UPDATE SET fire_at=excluded.fire_at,"
                " job_id=excluded.job_id, job_name=excluded.job_name,"
                " state=excluded.state, deliver=excluded.deliver,"
                " updated_at=excluded.updated_at",
                (credit_key, role, float(fire_at), job_id, job_name, state, deliver, now, now),
            )
        row = self.get_slot(credit_key, role)
        assert row is not None
        return row

    def get_slot(self, credit_key: str, role: str) -> dict | None:
        return self._one("SELECT * FROM slots WHERE credit_key=? AND role=?", (credit_key, role))

    def list_slots(self, *, credit_key: str | None = None, role: str | None = None) -> list[dict]:
        sql = "SELECT * FROM slots"
        clauses: list[str] = []
        params: list[Any] = []
        if credit_key is not None:
            clauses.append("credit_key=?")
            params.append(credit_key)
        if role is not None:
            clauses.append("role=?")
            params.append(role)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY fire_at, credit_key, role"
        return self._all(sql, tuple(params))

    def set_slot_state(
        self, credit_key: str, role: str, state: str, now: float | None = None
    ) -> None:
        now = self._clock() if now is None else float(now)
        self._write(
            "UPDATE slots SET state=?, updated_at=? WHERE credit_key=? AND role=?",
            (state, now, credit_key, role),
        )

    def mark_slot_ran(self, credit_key: str, role: str, now: float) -> None:
        """Record that *this* one-shot processed the credit (R9, per role)."""
        self._write(
            "UPDATE slots SET ran_at=?, state='fired', updated_at=? WHERE credit_key=? AND role=?",
            (float(now), float(now), credit_key, role),
        )

    def clear_slot_ran(self, credit_key: str, role: str, now: float | None = None) -> None:
        """Undo a marker whose pre-POST write did not land (R9)."""
        now = self._clock() if now is None else float(now)
        self._write(
            "UPDATE slots SET ran_at=NULL, updated_at=? WHERE credit_key=? AND role=?",
            (now, credit_key, role),
        )

    def delete_slot(self, credit_key: str, role: str) -> None:
        self._write("DELETE FROM slots WHERE credit_key=? AND role=?", (credit_key, role))

    # ------------------------------------------------------------- owned jobs
    def record_job(
        self,
        job_id: str,
        job_name: str,
        kind: str,
        script: str | None = None,
        now: float | None = None,
    ) -> None:
        """Record an owned job; a re-created job replaces the old row of that name."""
        now = self._clock() if now is None else float(now)
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM owned_jobs WHERE job_name=? AND job_id<>?", (job_name, job_id)
            )
            conn.execute(
                "INSERT INTO owned_jobs(job_id, job_name, kind, script, created_at)"
                " VALUES(?, ?, ?, ?, ?) ON CONFLICT(job_id) DO UPDATE SET"
                " job_name=excluded.job_name, kind=excluded.kind,"
                " script=excluded.script, removed_at=NULL",
                (job_id, job_name, kind, script, now),
            )

    def mark_job_removed(self, job_id: str, now: float | None = None) -> None:
        now = self._clock() if now is None else float(now)
        self._write("UPDATE owned_jobs SET removed_at=? WHERE job_id=?", (now, job_id))

    def list_owned_jobs(self, *, active_only: bool = False, kind: str | None = None) -> list[dict]:
        sql = "SELECT * FROM owned_jobs"
        clauses: list[str] = []
        params: list[Any] = []
        if active_only:
            clauses.append("removed_at IS NULL")
        if kind is not None:
            clauses.append("kind=?")
            params.append(kind)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at, job_id"
        return self._all(sql, tuple(params))

    def get_job_by_name(self, job_name: str) -> dict | None:
        return self._one("SELECT * FROM owned_jobs WHERE job_name=?", (job_name,))

    # ----------------------------------------------------------------- outbox
    def enqueue_notice(
        self,
        dedupe_key: str,
        *,
        kind: str,
        message_key: str,
        params: Mapping[str, Any] | None = None,
        target: str | None = None,
        credit_key: str | None = None,
        now: float | None = None,
    ) -> int:
        """Queue one notice in the caller's transaction (I6); idempotent."""
        now = self._clock() if now is None else float(now)
        payload = json.dumps(dict(params or {}), sort_keys=True, ensure_ascii=False)
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO outbox(dedupe_key, credit_key, kind, message_key, params_json,"
                " target, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(dedupe_key) DO NOTHING",
                (dedupe_key, credit_key, kind, message_key, payload, target, now),
            )
            row = conn.execute("SELECT id FROM outbox WHERE dedupe_key=?", (dedupe_key,)).fetchone()
        assert row is not None
        return int(row[0])

    def list_notices(self, *, state: str | None = None, limit: int | None = None) -> list[dict]:
        sql = "SELECT * FROM outbox"
        params: list[Any] = []
        if state is not None:
            sql += " WHERE state=?"
            params.append(state)
        sql += " ORDER BY id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return self._all(sql, tuple(params))

    def mark_notice_sent(
        self, notice_id: int, message_id: str | None, now: float | None = None
    ) -> None:
        now = self._clock() if now is None else float(now)
        self._write(
            "UPDATE outbox SET state='sent', message_id=?, sent_at=?,"
            " last_error_code=NULL, next_attempt_at=NULL WHERE id=?",
            (message_id, now, int(notice_id)),
        )

    def due_notices(self, *, limit: int, max_attempts: int, now: float | None = None) -> list[dict]:
        """Rows a flush may attempt **now**, oldest first (R11).

        A row is due when it is not terminal and its persisted deadline has
        passed:

        * ``queued`` with ``next_attempt_at`` NULL — never attempted, due now;
        * ``failed`` with ``attempts < max_attempts`` and
          ``next_attempt_at`` NULL or ``<= now`` — a retry whose backoff has
          elapsed.

        ``blocked`` (permanent configuration error), ``abandoned`` (budget
        spent), ``sent`` and ``skipped`` are never due: retrying them cannot
        change the outcome.

        ``now`` defaults to the store's clock. The deadline is a column, not a
        timer in the caller, so a flush in a different process (the cron tick
        after a scan) honours exactly the same spacing.
        """
        moment = self._clock() if now is None else float(now)
        return self._all(
            "SELECT * FROM outbox WHERE (state='queued' AND next_attempt_at IS NULL)"
            " OR (state='failed' AND attempts < ?"
            "     AND (next_attempt_at IS NULL OR next_attempt_at <= ?))"
            " ORDER BY id LIMIT ?",
            (int(max_attempts), moment, int(limit)),
        )

    def mark_notice_failed(
        self,
        notice_id: int,
        code: str | None,
        now: float | None = None,
        *,
        max_attempts: int | None = None,
    ) -> None:
        """Record one retryable failure and persist the next retry deadline.

        ``max_attempts`` is the caller's policy (``notify.MAX_DELIVERY_ATTEMPTS``);
        ``None`` keeps the row retryable forever, which is only right for a
        caller that has its own bound.

        The deadline is exponential in the attempt number that just failed —
        ``RETRY_BASE_DELAY_S * 2**(attempts-1)``, capped at
        ``RETRY_MAX_DELAY_S`` — and is stored on the row, so a flush before it
        spends nothing and a flush after it retries. A row that reaches the cap
        becomes ``abandoned`` and its deadline is cleared: a terminal row has no
        next attempt.
        """
        moment = self._clock() if now is None else float(now)
        if max_attempts is None:
            attempts = self._notice_attempts(notice_id) + 1
            self._write(
                "UPDATE outbox SET state='failed', last_error_code=?, attempts=?,"
                " next_attempt_at=? WHERE id=?",
                (code, attempts, moment + retry_delay_s(attempts), int(notice_id)),
            )
            return
        attempts = self._notice_attempts(notice_id) + 1
        if attempts >= int(max_attempts):
            self._write(
                "UPDATE outbox SET state='abandoned', last_error_code=?, attempts=?,"
                " next_attempt_at=NULL WHERE id=?",
                (code, attempts, int(notice_id)),
            )
            return
        self._write(
            "UPDATE outbox SET state='failed', last_error_code=?, attempts=?,"
            " next_attempt_at=? WHERE id=?",
            (code, attempts, moment + retry_delay_s(attempts), int(notice_id)),
        )

    def _notice_attempts(self, notice_id: int) -> int:
        """The row's current attempt count (0 when the row is gone)."""
        row = self._one("SELECT attempts FROM outbox WHERE id=?", (int(notice_id),))
        if row is None:
            return 0
        try:
            return int(row["attempts"])
        except (TypeError, ValueError):
            return 0

    def mark_notice_blocked(
        self, notice_id: int, code: str | None, now: float | None = None
    ) -> None:
        """Record a permanent failure: retrying it cannot succeed (R11)."""
        now = self._clock() if now is None else float(now)
        self._write(
            "UPDATE outbox SET state='blocked', last_error_code=?, attempts=attempts+1,"
            " next_attempt_at=NULL WHERE id=?",
            (code, int(notice_id)),
        )

    def mark_notice_skipped(
        self, notice_id: int, code: str | None, now: float | None = None
    ) -> None:
        now = self._clock() if now is None else float(now)
        self._write(
            "UPDATE outbox SET state='skipped', last_error_code=?, attempts=attempts+1,"
            " next_attempt_at=NULL WHERE id=?",
            (code, int(notice_id)),
        )

    # ---------------------------------------------------------------- history
    #: The complete set of actions the activity log records (D-014). A value
    #: outside this set is refused, so a typo cannot create an unfilterable row.
    HISTORY_ACTIONS = (
        "scan",
        "schedule",
        "reminder",
        "redemption",
        "refresh",
        "failure",
        "fire",
        "remind",
        "quota_check",
        "notice",
        "uninstall",
        "heal",
        "gc",
    )

    def add_history(self, records: Iterable[Mapping[str, Any]]) -> int:
        """Append activity-log rows, sanitized and capped (D-014).

        Every row is validated here rather than at the call sites: an action
        outside :data:`HISTORY_ACTIONS` is refused, and ``detail`` is a bounded
        JSON object of display values only. A caller cannot smuggle a token, a
        raw credit id or a full email into the log through this method.
        """
        rows: list[tuple] = []
        for record in records:
            action = str(record.get("action") or "")
            if action not in self.HISTORY_ACTIONS:
                raise ValueError(f"unknown history action {action!r}")
            rows.append(
                (
                    float(record.get("at") or self._clock()),
                    action,
                    record.get("credit_key"),
                    record.get("account_key"),
                    record.get("role"),
                    str(record.get("outcome") or ""),
                    record.get("reason_code"),
                    _sanitize_detail(record.get("detail")),
                )
            )
        if not rows:
            return 0
        with self.transaction() as conn:
            conn.executemany(
                "INSERT INTO history(at, action, credit_key, account_key, role,"
                " outcome, reason_code, detail)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.execute(
                "DELETE FROM history WHERE id <= (SELECT id FROM history"
                " ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (HISTORY_CAP,),
            )
        return len(rows)

    def list_history(
        self,
        limit: int = 50,
        *,
        action: str | None = None,
        account_key: str | None = None,
        credit_key: str | None = None,
        since: float | None = None,
        before: float | None = None,
    ) -> list[dict]:
        """The activity log, newest first, filtered (D-014).

        One query function for the CLI and the API: every filter is optional and
        combines with AND, so ``history --action redemption --limit 20`` and the
        popup's History view use the same code path.
        """
        sql = "SELECT * FROM history"
        clauses: list[str] = []
        params: list[Any] = []
        if action is not None:
            clauses.append("action=?")
            params.append(str(action))
        if account_key is not None:
            clauses.append("account_key=?")
            params.append(str(account_key))
        if credit_key is not None:
            clauses.append("credit_key=?")
            params.append(str(credit_key))
        if since is not None:
            clauses.append("at >= ?")
            params.append(float(since))
        if before is not None:
            clauses.append("at < ?")
            params.append(float(before))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY at DESC, id DESC LIMIT ?"
        params.append(max(1, int(limit)))
        return self._all(sql, tuple(params))

    def count_history(self) -> int:
        row = self.connection_or_raise().execute("SELECT count(*) FROM history").fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------- runs
    def start_run(self, kind: str, role: str | None, pid: int, now: float | None = None) -> int:
        now = self._clock() if now is None else float(now)
        cursor = self._write(
            "INSERT INTO runs(kind, role, pid, started_at) VALUES(?, ?, ?, ?)",
            (kind, role, int(pid), now),
        )
        return int(cursor.lastrowid or 0)

    def finish_run(self, run_id: int, outcome: str, now: float | None = None) -> None:
        now = self._clock() if now is None else float(now)
        self._write("UPDATE runs SET ended_at=?, outcome=? WHERE id=?", (now, outcome, int(run_id)))

    def list_runs(self, limit: int = 50) -> list[dict]:
        return self._all("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (int(limit),))

    def heal_runs(
        self, now: float | None = None, alive: Callable[[int], bool] | None = None
    ) -> int:
        """Close runs whose process died without finishing (A§6 healing)."""
        now = self._clock() if now is None else float(now)
        check = alive or pid_alive
        stale = [
            row
            for row in self._all("SELECT id, pid FROM runs WHERE ended_at IS NULL")
            if not check(int(row["pid"]))
        ]
        if not stale:
            return 0
        with self.transaction() as conn:
            conn.executemany(
                "UPDATE runs SET ended_at=?, outcome='interrupted' WHERE id=?",
                [(now, int(row["id"])) for row in stale],
            )
        return len(stale)

    # -------------------------------------------------------------- retention
    def prune(self, now: float | None = None) -> dict[str, int]:
        """Retention (A§6): settled credits 14 d, sent notices 14 d.

        A **consumed** credit is aged from the moment it settled
        (``confirmed_at``, else ``updated_at``): aging from ``updated_at`` alone
        would keep a credit alive forever while a scan keeps refreshing its
        descriptive fields.

        An **unconsumed** credit (``nothing_to_reset``, ``failed``) is not aged
        at all while the backend could still list it (R5): deleting that row
        would throw away the one ``redeem_request_id`` the credit must keep for
        its whole life, and the next attempt would mint a different UUID for the
        same credit. It is removed only once the credit's own expiry is older
        than the retention window, so the row outlives the credit it protects.
        """
        now = self._clock() if now is None else float(now)
        credit_cutoff = now - RETENTION_S
        removed: dict[str, int] = {}
        with self.transaction() as conn:
            placeholders = ",".join("?" * len(CONSUMED_STATUSES))
            cursor = conn.execute(
                f"DELETE FROM credits WHERE status IN ({placeholders})"
                f" AND COALESCE(confirmed_at, updated_at) < ?",
                (*CONSUMED_STATUSES, credit_cutoff),
            )
            removed["credits"] = cursor.rowcount or 0
            placeholders = ",".join("?" * len(UNCONSUMED_STATUSES))
            cursor = conn.execute(
                f"DELETE FROM credits WHERE status IN ({placeholders})"
                f" AND expires_at IS NOT NULL AND expires_at < ?",
                (*UNCONSUMED_STATUSES, credit_cutoff),
            )
            removed["credits"] += cursor.rowcount or 0
            cursor = conn.execute(
                "DELETE FROM slots WHERE credit_key NOT IN (SELECT credit_key FROM credits)"
            )
            removed["slots"] = cursor.rowcount or 0
            cursor = conn.execute(
                "DELETE FROM outbox WHERE state IN ('sent','skipped','abandoned','blocked')"
                " AND created_at < ?",
                (credit_cutoff,),
            )
            removed["notices"] = cursor.rowcount or 0
            cursor = conn.execute(
                "DELETE FROM runs WHERE ended_at IS NOT NULL AND ended_at < ?", (credit_cutoff,)
            )
            removed["runs"] = cursor.rowcount or 0
            # The activity log keeps 14 days even when under its row cap, so a
            # quiet profile does not accumulate a year of noise (D-014).
            cursor = conn.execute("DELETE FROM history WHERE at < ?", (credit_cutoff,))
            removed["history"] = cursor.rowcount or 0
        return removed

    # --------------------------------------------------------------- run lock
    @contextlib.contextmanager
    def credit_lock(
        self, credit_key: str, wait: float | None = None, poll: float | None = None
    ) -> Iterator[None]:
        """Serialise the redemption of one credit across processes (D-015).

        The run lock already serialises whole runs; this narrower lock is what
        makes an explicit **Redeem now** safe against a primary/backup one-shot
        that is already inside ``redeem_one`` for the same credit: the second
        caller waits, then re-decides from the backend and finds the credit
        settled instead of posting a second time.
        """
        wait = self._lock_wait if wait is None else float(wait)
        poll = self._lock_poll if poll is None else float(poll)
        path = self.data_dir / f"credit-{credit_key}.lock"
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
        _chmod(path, 0o600)
        deadline = time.monotonic() + max(0.0, wait)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    if time.monotonic() >= deadline:
                        raise LockTimeout(
                            f"another run is redeeming this credit (waited {wait:.0f}s)"
                        ) from exc
                    time.sleep(poll)
            try:
                yield
            finally:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)

    @contextlib.contextmanager
    def run_lock(self, wait: float | None = None, poll: float | None = None) -> Iterator[None]:
        """Serialise scan/fire/redeem-now/uninstall across processes (A§6).

        Re-entrant in-process: the planner holds it and calls ``redeem_one``
        without deadlocking on itself. A second *process* waits up to ``wait``
        seconds and then raises :class:`LockTimeout` with ``code='lock_timeout'``.
        """
        if self._lock_depth:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        wait = self._lock_wait if wait is None else float(wait)
        poll = self._lock_poll if poll is None else float(poll)
        fd = os.open(str(self.lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        _chmod(self.lock_path, 0o600)
        deadline = time.monotonic() + max(0.0, wait)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    if time.monotonic() >= deadline:
                        raise LockTimeout(
                            f"another Reset Rescue run holds the lock (waited {wait:.0f}s)"
                        ) from exc
                    time.sleep(poll)
            self._lock_fd = fd
            self._lock_depth = 1
            try:
                yield
            finally:
                self._lock_depth = 0
                self._lock_fd = None
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
                with contextlib.suppress(OSError):
                    os.close(fd)
        except BaseException:
            if self._lock_fd is None:
                with contextlib.suppress(OSError):
                    os.close(fd)
            raise

    # ---------------------------------------------------------------- helpers
    def _one(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        row = self.connection_or_raise().execute(sql, tuple(params)).fetchone()
        return None if row is None else dict(row)

    def _all(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        return [
            dict(row) for row in self.connection_or_raise().execute(sql, tuple(params)).fetchall()
        ]

    # ------------------------------------------- protocol used by service/CLI
    # ``service.py`` (L1) and ``tests/fakes.FakeStore`` (shared) speak this
    # narrower, dict-shaped dialect. The rich methods above are the flows' own
    # API; these aliases keep one store object usable by both without either
    # lane importing the other's shapes.

    def meta_get(self, key: str) -> str | None:
        return self.get_meta(key)

    def meta_set(self, key: str, value: str) -> None:
        self.set_meta(key, value)

    def history_add(
        self,
        action: str,
        *,
        outcome: str,
        credit_key: str | None = None,
        account_key: str | None = None,
        role: str | None = None,
        reason_code: str | None = None,
    ) -> None:
        """One history row, in the protocol shape ``service``/``fakes`` use."""
        self.add_history(
            [
                {
                    "at": self._clock(),
                    "action": action,
                    "outcome": outcome,
                    "credit_key": credit_key,
                    "account_key": account_key,
                    "role": role,
                    "reason_code": reason_code,
                }
            ]
        )

    def history_tail(self, limit: int = 50) -> list[dict]:
        return self.list_history(limit)

    def credit_get(self, credit_key: str) -> dict | None:
        return self.get_credit(credit_key)

    def credit_upsert(self, row: Mapping[str, Any]) -> dict:
        """Upsert from a mapping; unknown keys are ignored, never stored."""
        key = str(row["credit_key"])
        account_key = str(row.get("account_key") or "")
        if not account_key:
            raise ValueError("credit_upsert needs account_key")
        stored = self.upsert_credit(
            key,
            account_key,
            title=row.get("title"),
            reset_type=row.get("reset_type"),
            granted_at=row.get("granted_at"),
            expires_at=row.get("expires_at"),
            backend_state=row.get("backend_state"),
            status=row.get("status"),
            now=row.get("updated_at"),
        )
        # The protocol row may carry flow-owned fields a caller seeded directly.
        extra = {
            name: row[name]
            for name in (
                "redeem_request_id",
                "posted_at",
                "confirmed_at",
                "attempts",
                "last_code",
                "last_reason",
                "last_failure_category",
                "notified_success",
                "notified_outcome",
            )
            if name in row
        }
        if extra:
            self.update_credit(key, **extra)
            stored = self.get_credit(key) or stored
        return stored

    def credit_list(
        self, *, account_key: str | None = None, statuses: Sequence[str] | None = None
    ) -> list[dict]:
        return self.list_credits(account_key=account_key, statuses=statuses)

    def account_upsert(self, row: Mapping[str, Any]) -> None:
        self.upsert_account(
            str(row["account_key"]),
            str(row.get("label") or ""),
            str(row.get("source_kind") or "pool"),
            row.get("entry_id"),
            now=row.get("last_seen_at"),
        )

    def account_list(self) -> list[dict]:
        return self.list_accounts()

    def slot_get(self, credit_key: str, role: str) -> dict | None:
        return self.get_slot(credit_key, role)

    def slot_upsert(self, credit_key: str, role: str, row: Mapping[str, Any]) -> None:
        self.upsert_slot(
            credit_key,
            role,
            fire_at=float(row.get("fire_at") or 0.0),
            job_id=row.get("job_id"),
            job_name=row.get("job_name"),
            state=str(row.get("state") or "planned"),
            deliver=row.get("deliver"),
            now=row.get("updated_at"),
        )

    def slot_list(self) -> list[dict]:
        return self.list_slots()

    def clear_all(self) -> None:
        """``uninstall`` without ``--keep-data``: drop every row, keep the schema."""
        with self.transaction() as conn:
            for table in (
                "credits",
                "slots",
                "accounts",
                "outbox",
                "history",
                "runs",
                "owned_jobs",
            ):
                conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM meta WHERE key <> 'schema_version'")


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:  # pragma: no cover - a vanished file is not a failure here
        pass


#: Substrings that must never appear in a history ``detail`` value (D-014/I8).
_FORBIDDEN_DETAIL_PARTS = (
    "token",
    "secret",
    "authorization",
    "cookie",
    "password",
    "api_key",
    "refresh",
    "chat_id",
    "credit_id",
    "account_id",
    "body",
)
#: A detail value is a short display string; anything longer is truncated.
_DETAIL_MAX_CHARS = 120
_DETAIL_MAX_KEYS = 8


def _sanitize_detail(detail: Any) -> str | None:
    """A bounded JSON object of display values, or ``None`` (D-014, I8).

    The activity log is a file the user may share, so the store — not the
    caller — is the last line of defence: a key that names a credential or a raw
    identifier is dropped, and every value is truncated to a display string.
    """
    if not detail:
        return None
    if not isinstance(detail, Mapping):
        return None
    cleaned: dict[str, str] = {}
    for key, value in list(detail.items())[:_DETAIL_MAX_KEYS]:
        name = str(key)
        lowered = name.lower()
        if any(part in lowered for part in _FORBIDDEN_DETAIL_PARTS):
            continue
        if value is None:
            continue
        text = str(value)
        cleaned[name] = text[:_DETAIL_MAX_CHARS]
    if not cleaned:
        return None
    return json.dumps(cleaned, sort_keys=True, ensure_ascii=False)


def _statements(script: str) -> list[str]:
    """Split a DDL script into single statements (sqlite3 refuses multi-statement)."""
    return [chunk.strip() for chunk in script.split(";") if chunk.strip()]
