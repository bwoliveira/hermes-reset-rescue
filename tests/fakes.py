"""Offline fakes shared by every lane's tests (T-03).

Nothing here touches the network, ``~/.hermes``, ``auth.json`` or the real
cron. Each fake records what it was asked so a test can assert on behaviour
*and* on the absence of forbidden behaviour (no POST in a dry run, no second
refresh, no write to the pool).

Import surface other lanes rely on (keep stable):

``FakeClock``, ``FakeCli``, ``FakeCron``, ``FakeWrappers``, ``FakeNotifier``,
``FakeStore``, ``FakeTransport``, ``FakePool``, ``FakeSource``,
``make_deps``, ``make_settings``, ``credits_payload``, ``credit_row``,
``usage_payload``, ``jwt_for``, ``temp_home``.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from reset_rescue.accounts import (
    AccountRef,
    AccountSource,
    Response,
    SourceUnavailable,
    account_key_for,
)
from reset_rescue.clock import CliResult, Deps, Paths

__all__ = [
    "FakeClock",
    "FakeCli",
    "FakeCron",
    "FakeWrappers",
    "FakeNotifier",
    "FakeStore",
    "FakeTransport",
    "FakePool",
    "FakeSource",
    "FakeConfig",
    "make_deps",
    "make_settings",
    "credits_payload",
    "credit_row",
    "usage_payload",
    "jwt_for",
    "temp_home",
    "SALT",
]

SALT = "a" * 64
DAY = 86400.0
HOUR = 3600.0
MINUTE = 60.0


# ---------------------------------------------------------------------- clock


class FakeClock:
    """A clock the test advances explicitly; ``sleep`` advances it too."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self._now = float(start)
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        if seconds > 0:
            self._now += float(seconds)

    def advance(self, seconds: float) -> float:
        self._now += float(seconds)
        return self._now

    def set(self, epoch: float) -> float:
        self._now = float(epoch)
        return self._now


# ------------------------------------------------------------------------ cli


class FakeCli:
    """``hermes`` invocations, recorded, with scripted outcomes.

    ``responses`` maps a tuple of the first N argv words to a ``CliResult`` or a
    callable returning one. ``default`` answers anything unmapped.
    """

    def __init__(self, *, default: Optional[CliResult] = None) -> None:
        self.calls: list[list[str]] = []
        self.responses: list[tuple[tuple[str, ...], Any]] = []
        self.default = default or CliResult(argv=(), returncode=0, stdout="", stderr="")

    def on(
        self,
        *argv_prefix: str,
        result: Any = None,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        timed_out: bool = False,
    ) -> "FakeCli":
        if result is None:
            result = CliResult(
                argv=tuple(argv_prefix),
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
                timed_out=timed_out,
            )
        self.responses.append((tuple(argv_prefix), result))
        return self

    def run(self, argv: Sequence[str], *, timeout: float = 60.0) -> CliResult:
        call = [str(part) for part in argv]
        self.calls.append(call)
        for prefix, result in self.responses:
            if tuple(call[: len(prefix)]) == prefix:
                return result(call) if callable(result) else result
        return self.default

    # -- assertions --------------------------------------------------------

    def argv_calls(self) -> list[list[str]]:
        return [list(call) for call in self.calls]

    def count(self, *argv_prefix: str) -> int:
        return sum(1 for call in self.calls if tuple(call[: len(argv_prefix)]) == argv_prefix)

    def refreshes(self) -> list[str]:
        """Entry ids passed to ``auth refresh openai-codex <id>``."""
        out = []
        for call in self.calls:
            if call[:3] == ["auth", "refresh", "openai-codex"] and len(call) >= 4:
                out.append(call[3])
        return out


# ------------------------------------------------------------------- cron CLI


def parse_create_argv(argv: Sequence[str]):
    """Read a ``cron create`` argv into a ``JobSpec`` (test-side helper).

    The plugin keeps no argv parser of its own any more (one spec path, R18),
    so a CLI fake that has to understand ``cron create`` reads it here.
    """
    from reset_rescue.cron_gateway import JobSpec

    parts = [str(part) for part in argv]
    if parts[:2] == ["cron", "create"]:
        parts = parts[2:]
    if not parts or parts[0].startswith("-"):
        return None
    schedule = parts[0]
    flags: dict[str, str] = {}
    index = 1
    while index < len(parts):
        token = parts[index]
        if token.startswith("--") and index + 1 < len(parts):
            flags[token] = parts[index + 1]
            index += 2
            continue
        index += 1
    name = flags.get("--name", "")
    script = flags.get("--script", "")
    if not name or not script:
        return None
    repeat = flags.get("--repeat")
    return JobSpec(
        name=name,
        schedule=schedule,
        script=script,
        deliver=flags.get("--deliver", "local"),
        failure_deliver=flags.get("--failure-deliver"),
        repeat=int(repeat) if repeat else None,
    )


class FakeCron:
    """In-memory cron with the shapes ``cron_gateway`` parses and produces.

    Speaks the gateway's real API (``JobSpec`` in, ``CronJob``-shaped dicts
    out) so a test exercises the same call the production code makes.
    """

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.created: list[list[str]] = []
        self.removed: list[str] = []
        self.fail_create = False
        self.fail_remove = False
        self._seq = 0

    def list_jobs(self, *, include_all: bool = True) -> list[dict]:
        return [dict(job) for job in self.jobs.values()]

    def find(self, name: str):
        from reset_rescue.cron_gateway import CronJob

        for job in self.jobs.values():
            if job["name"] == name:
                return CronJob(
                    job_id=job["id"],
                    name=job["name"],
                    state=str(job.get("state") or "scheduled"),
                    schedule=str(job.get("schedule") or ""),
                    script=str(job.get("script") or ""),
                    deliver=str(job.get("deliver") or ""),
                    enabled=bool(job.get("enabled", True)),
                )
        return None

    def owns(self, job, owned) -> bool:
        return job.job_id in set(owned)

    def create(self, spec):
        from reset_rescue.cron_gateway import CronJob

        self.created.append(list(spec.argv()))
        if self.fail_create:
            return None
        self._seq += 1
        job_id = f"job-{self._seq:04d}"
        self.jobs[job_id] = {
            "id": job_id,
            "name": spec.name,
            "state": "scheduled",
            "enabled": True,
            "schedule": spec.schedule,
            "script": spec.script,
            "deliver": spec.deliver,
            "repeat": "0/1" if spec.repeat else "",
        }
        return CronJob(
            job_id=job_id,
            name=spec.name,
            state="scheduled",
            schedule=spec.schedule,
            script=spec.script,
            deliver=spec.deliver,
        )

    def ensure(self, spec, *, owned, now=None):
        """Create or keep ``spec``; mirrors the gateway's decisions."""
        existing = self.find(spec.name)
        owned_ids = set(owned)
        if existing is not None:
            if existing.job_id not in owned_ids:
                return "foreign", existing
            if not self.drift(existing, spec):
                return "unchanged", existing
            if not self.remove(existing.job_id, owned=owned_ids):
                return "failed", existing
            created = self.create(spec)
            return ("recreated", created) if created is not None else ("failed", None)
        created = self.create(spec)
        return ("created", created) if created is not None else ("failed", None)

    def drift(self, job, spec) -> list[str]:
        from reset_rescue.cron_gateway import _schedule_matches

        reasons = []
        if job.script and job.script != spec.script:
            reasons.append("script")
        if job.deliver and job.deliver != spec.deliver:
            reasons.append("deliver")
        if job.schedule and not _schedule_matches(job.schedule, spec.schedule):
            reasons.append("schedule")
        if job.terminal:
            reasons.append("terminal")
        return reasons

    def remove(self, job_id, *, owned) -> bool:
        job_id = job_id.job_id if hasattr(job_id, "job_id") else str(job_id)
        self.removed.append(job_id)
        if job_id not in set(owned) or self.fail_remove:
            return False
        return self.jobs.pop(job_id, None) is not None

    def names(self) -> list[str]:
        return sorted(job["name"] for job in self.jobs.values())


# ------------------------------------------------------------------- wrappers


class FakeWrappers:
    """Wrapper install state, no filesystem unless the test asks for it."""

    def __init__(self, states: Optional[Mapping[str, str]] = None) -> None:
        self.states = dict(states or {})
        self.installed = 0
        self.removed: list[str] = []

    def install(self) -> Mapping[str, str]:
        self.installed += 1
        for name in ("reset_rescue_scan.py", "reset_rescue_fire.py", "reset_rescue_fire_backup.py"):
            self.states.setdefault(name, "ok")
        return dict(self.states)

    def states_view(self) -> Mapping[str, str]:
        """Read-only view for `/status` (the real class exposes `states()`)."""
        return dict(self.states)

    def remove_owned(self) -> Sequence[str]:
        removed = [name for name in self.states if name.startswith("reset_rescue_")]
        for name in removed:
            self.states.pop(name, None)
        self.removed.extend(removed)
        return removed


# ------------------------------------------------------------------- notifier


class FakeNotifier:
    """Records enqueued notices; ``flush`` marks them delivered."""

    def __init__(self, *, deliver: bool = True) -> None:
        self.enqueued: list[dict] = []
        self.flushed: list[dict] = []
        self.deliver = deliver

    def enqueue(
        self,
        kind: str,
        params: Mapping[str, Any],
        *,
        credit_key: Optional[str] = None,
        dedupe_key: Optional[str] = None,
    ) -> None:
        self.enqueued.append(
            {
                "kind": kind,
                "params": dict(params),
                "credit_key": credit_key,
                "dedupe_key": dedupe_key,
            }
        )

    def flush(self) -> Sequence[Any]:
        delivered = []
        for item in self.enqueued:
            record = dict(item)
            record["delivered"] = self.deliver
            delivered.append(record)
        self.flushed.extend(delivered)
        self.enqueued.clear()
        return delivered

    def kinds(self) -> list[str]:
        return [item["kind"] for item in self.enqueued]


# ---------------------------------------------------------------------- store


class FakeStore:
    """Dict-backed stand-in for ``store.Store`` (L2 owns the real one)."""

    def __init__(self) -> None:
        self.meta: dict[str, str] = {}
        self.history: list[dict] = []
        self.credits: dict[str, dict] = {}
        self.accounts: dict[str, dict] = {}
        self.outbox: list[dict] = []
        self.slots: dict[tuple[str, str], dict] = {}
        self.owned_jobs: dict[str, dict] = {}
        self.runs: list[dict] = []
        self.cleared = 0

    # runs (the scan clock the popup reads, P2)
    def start_run(
        self, kind: str, role: Optional[str], pid: int, now: Optional[float] = None
    ) -> int:
        run_id = len(self.runs) + 1
        self.runs.append(
            {
                "id": run_id,
                "kind": kind,
                "role": role,
                "pid": int(pid),
                "started_at": 0.0 if now is None else float(now),
                "ended_at": None,
                "outcome": None,
            }
        )
        return run_id

    def finish_run(self, run_id: int, outcome: str, now: Optional[float] = None) -> None:
        for row in self.runs:
            if row["id"] == int(run_id):
                row["ended_at"] = 0.0 if now is None else float(now)
                row["outcome"] = outcome

    def list_runs(self, limit: int = 50) -> list[dict]:
        return [
            dict(row)
            for row in sorted(self.runs, key=lambda r: r["id"], reverse=True)[: int(limit)]
        ]

    def heal_runs(self, now: Optional[float] = None, alive: Any = None) -> int:
        return 0

    # meta
    def meta_get(self, key: str) -> Optional[str]:
        return self.meta.get(key)

    def meta_set(self, key: str, value: str) -> None:
        self.meta[key] = value

    # history
    def history_add(
        self,
        action: str,
        *,
        outcome: str,
        credit_key: Optional[str] = None,
        account_key: Optional[str] = None,
        role: Optional[str] = None,
        reason_code: Optional[str] = None,
    ) -> None:
        self.history.append(
            {
                "action": action,
                "outcome": outcome,
                "credit_key": credit_key,
                "account_key": account_key,
                "role": role,
                "reason_code": reason_code,
            }
        )

    def history_tail(self, limit: int = 50) -> list[dict]:
        return list(reversed(self.history))[:limit]

    # credits / accounts (shapes the flows use)
    def credit_get(self, credit_key: str) -> Optional[dict]:
        row = self.credits.get(credit_key)
        return dict(row) if row else None

    def credit_upsert(self, row: Mapping[str, Any]) -> None:
        key = str(row["credit_key"])
        current = self.credits.get(key, {})
        current.update(dict(row))
        self.credits[key] = current

    def credit_list(self, *, account_key: Optional[str] = None) -> list[dict]:
        rows = [dict(row) for row in self.credits.values()]
        if account_key is not None:
            rows = [row for row in rows if row.get("account_key") == account_key]
        return rows

    def account_upsert(self, row: Mapping[str, Any]) -> None:
        key = str(row["account_key"])
        current = self.accounts.get(key, {})
        current.update(dict(row))
        self.accounts[key] = current

    def account_list(self) -> list[dict]:
        return [dict(row) for row in self.accounts.values()]

    # slots
    def slot_get(self, credit_key: str, role: str) -> Optional[dict]:
        row = self.slots.get((credit_key, role))
        return dict(row) if row else None

    def slot_upsert(self, credit_key: str, role: str, row: Mapping[str, Any]) -> None:
        current = self.slots.get((credit_key, role), {})
        current.update(dict(row))
        self.slots[(credit_key, role)] = current

    def slot_list(self) -> list[dict]:
        return [dict(row) for row in self.slots.values()]

    # owned jobs
    def record_job(self, job_id: str, job_name: str, kind: str, script: str = "") -> None:
        for other, row in list(self.owned_jobs.items()):
            if row["job_name"] == job_name and other != job_id:
                del self.owned_jobs[other]
        self.owned_jobs[job_id] = {
            "job_id": job_id,
            "job_name": job_name,
            "kind": kind,
            "script": script,
            "removed_at": None,
        }

    def mark_job_removed(self, job_id: str, now: Optional[float] = None) -> None:
        row = self.owned_jobs.get(job_id)
        if row is not None:
            row["removed_at"] = 1.0 if now is None else float(now)

    def list_owned_jobs(
        self, *, active_only: bool = False, kind: Optional[str] = None
    ) -> list[dict]:
        rows = [dict(row) for row in self.owned_jobs.values()]
        if active_only:
            rows = [row for row in rows if row.get("removed_at") is None]
        if kind is not None:
            rows = [row for row in rows if row.get("kind") == kind]
        return rows

    def get_job_by_name(self, job_name: str) -> Optional[dict]:
        for row in self.owned_jobs.values():
            if row["job_name"] == job_name:
                return dict(row)
        return None

    def clear_all(self) -> None:
        self.cleared += 1
        self.meta.clear()
        self.history.clear()
        self.credits.clear()
        self.accounts.clear()
        self.outbox.clear()
        self.slots.clear()
        self.owned_jobs.clear()


# ------------------------------------------------------------------ transport


class FakeTransport:
    """Scripted HTTP: ``(method, path)`` → ``Response`` or an exception.

    A handler may be a callable taking ``(method, url, headers, body, timeout)``
    so a test can assert on headers without ever seeing them in a log.
    """

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.routes: list[tuple[str, str, Any]] = []
        self.default = Response(status=200, body={})

    def on(self, method: str, path_contains: str, response: Any) -> "FakeTransport":
        self.routes.append((method.upper(), path_contains, response))
        return self

    def __call__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Optional[bytes],
        timeout: float,
    ) -> Response:
        record = {
            "method": method.upper(),
            "url": url,
            "headers": dict(headers),
            "body": body,
            "timeout": timeout,
        }
        self.requests.append(record)
        for route_method, fragment, response in self.routes:
            if route_method == record["method"] and fragment in url:
                if callable(response):
                    return response(method, url, headers, body, timeout)
                if isinstance(response, Exception):
                    raise response
                return response
        return self.default

    # -- assertions --------------------------------------------------------

    def posts(self) -> list[dict]:
        return [req for req in self.requests if req["method"] == "POST"]

    def gets(self) -> list[dict]:
        return [req for req in self.requests if req["method"] == "GET"]

    def count(self, method: str, fragment: str = "") -> int:
        return sum(
            1 for req in self.requests if req["method"] == method.upper() and fragment in req["url"]
        )


# ----------------------------------------------------------------------- pool


class FakePool:
    """The credential pool as ``read_credential_pool`` returns it.

    ``rows`` are the raw dicts Hermes stores; ``write_calls`` proves the plugin
    never writes (the fake raises if anything tries).
    """

    def __init__(self, rows: Optional[Iterable[Mapping[str, Any]]] = None) -> None:
        # Rows are kept verbatim (a real pool can contain junk, and the source
        # must tolerate it); only mappings are copied.
        self.rows: list[Any] = [
            dict(row) if isinstance(row, Mapping) else row for row in (rows or [])
        ]
        self.reads = 0
        self.write_calls: list[str] = []

    def read(self) -> list:
        self.reads += 1
        return [dict(row) if isinstance(row, Mapping) else row for row in self.rows]

    def write(self, *_args: Any, **_kwargs: Any) -> None:
        self.write_calls.append("write")
        raise AssertionError("the plugin must never write the credential pool")

    def set_token(self, entry_id: str, token: str) -> None:
        for row in self.rows:
            if row.get("id") == entry_id:
                row["access_token"] = token

    def add(self, row: Mapping[str, Any]) -> dict:
        new_row = dict(row)
        self.rows.append(new_row)
        return new_row


class FakeSource(AccountSource):
    """A source over explicit accounts, with per-account scripted responses."""

    kind = "pool"

    def __init__(
        self, accounts: Optional[Sequence[AccountRef]] = None, *, salt: str = SALT
    ) -> None:
        self._accounts = list(accounts or [])
        self.salt = salt
        self.requests: list[dict] = []
        self.responses: list[tuple[str, str, AccountRef | None, Any]] = []
        self.default = Response(status=200, body={})
        self.refresh_calls: list[str] = []
        self.fail_accounts: Optional[str] = None

    def accounts(self) -> list[AccountRef]:
        if self.fail_accounts:
            raise SourceUnavailable(self.fail_accounts)
        return list(self._accounts)

    def add_account(
        self, label: str = "a***@e***.com", account_key: Optional[str] = None
    ) -> AccountRef:
        key = account_key or account_key_for(self.salt, f"acct-{len(self._accounts)}")
        ref = AccountRef(
            account_key=key,
            label=label,
            source_kind=self.kind,
            entry_id=f"entry-{len(self._accounts)}",
        )
        self._accounts.append(ref)
        return ref

    def on(
        self, method: str, path_contains: str, response: Any, account: Optional[AccountRef] = None
    ) -> "FakeSource":
        self.responses.append((method.upper(), path_contains, account, response))
        return self

    def request(
        self,
        account: AccountRef,
        method: str,
        url: str,
        *,
        json: object | None = None,
        timeout: float = 30.0,
    ) -> Response:
        self.requests.append(
            {
                "account_key": account.account_key,
                "method": method.upper(),
                "url": url,
                "json": json,
                "timeout": timeout,
            }
        )
        for route_method, fragment, route_account, response in self.responses:
            if route_method != method.upper() or fragment not in url:
                continue
            if route_account is not None and route_account.account_key != account.account_key:
                continue
            if callable(response):
                return response(account, method, url, json)
            if isinstance(response, Exception):
                raise response
            return response
        return self.default

    def posts(self) -> list[dict]:
        return [req for req in self.requests if req["method"] == "POST"]

    def count(self, method: str, fragment: str = "") -> int:
        return sum(
            1 for req in self.requests if req["method"] == method.upper() and fragment in req["url"]
        )


class FakeConfig:
    """The plugin settings writer (`hermes config set` equivalent)."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, Any]] = []
        self.fail = False

    def set_setting(self, name: str, value: Any) -> None:
        if self.fail:
            raise RuntimeError("config write failed")
        self.writes.append((name, value))

    def last(self) -> Optional[tuple[str, Any]]:
        return self.writes[-1] if self.writes else None


# ------------------------------------------------------------------- payloads


def jwt_for(
    account_id: str = "acct-1",
    email: str = "bob@example.com",
    *,
    residency: Optional[str] = None,
    expires_in: float = 3600.0,
    user_id: Optional[str] = None,
) -> str:
    """A structurally valid (unsigned) JWT carrying the Codex auth claims.

    ``user_id`` fills the per-user claim S2 needs (``sub``, the claim Hermes'
    own ``_codex_principal_identity`` reads); ``None`` leaves it out, which is
    what an older token looks like.
    """
    auth: dict[str, Any] = {"chatgpt_account_id": account_id}
    if residency:
        auth["chatgpt_data_residency"] = residency
    header = {"alg": "none", "typ": "JWT"}
    payload: dict[str, Any] = {
        "https://api.openai.com/auth": auth,
        "email": email,
        "exp": int(time.time() + expires_in),
    }
    if user_id is not None:
        payload["sub"] = user_id

    def enc(value: Mapping[str, Any]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{enc(header)}.{enc(payload)}.signature"


def credit_row(
    credit_id: str = "credit-1",
    *,
    expires_in: float = 3600.0,
    title: str = "Full reset",
    reset_type: str = "codex_rate_limits",
    status: str = "available",
    granted_at: Optional[float] = None,
    redeem_started_at: Optional[float] = None,
    expires_at: Optional[float] = None,
) -> dict:
    """One row of the backend's reset-credit list."""
    now = time.time()
    row: dict[str, Any] = {
        "id": credit_id,
        "title": title,
        "reset_type": reset_type,
        "status": status,
        "granted_at": granted_at if granted_at is not None else now - DAY,
        "expires_at": expires_at if expires_at is not None else now + expires_in,
    }
    if redeem_started_at is not None:
        row["redeem_started_at"] = redeem_started_at
    return row


def credits_payload(
    *rows: Mapping[str, Any], available_count: Optional[int] = None, **kwargs: Any
) -> dict:
    """The ``GET /rate-limit-reset-credits`` body."""
    items = [dict(row) for row in rows]
    if not items and kwargs:
        items = [credit_row(**kwargs)]
    count = (
        available_count
        if available_count is not None
        else sum(
            1 for row in items if str(row.get("status") or "").lower() in ("available", "redeeming")
        )
    )
    return {"credits": items, "available_count": count}


def usage_payload(*, weekly: Optional[float] = None, session: Optional[float] = None) -> dict:
    """The ``GET /usage`` body with ``limit_window_seconds`` windows."""
    rate: dict[str, Any] = {}
    if weekly is not None:
        rate["primary_window"] = {"limit_window_seconds": 604800, "used_percent": weekly}
    if session is not None:
        rate["secondary_window"] = {"limit_window_seconds": 18000, "used_percent": session}
    return {"rate_limit": rate}


# ------------------------------------------------------------------- settings


def make_settings(**overrides: Any) -> Any:
    """A ``Settings`` with test-friendly values (L1 owns the real class)."""
    from reset_rescue.settings import Settings

    return Settings(**overrides)


# ----------------------------------------------------------------------- deps


def make_deps(
    *,
    accounts: Optional[Sequence[AccountRef]] = None,
    pool_rows: Optional[Iterable[Mapping[str, Any]]] = None,
    clock: Optional[FakeClock] = None,
    settings: Any = None,
    salt: str = SALT,
    store: Any = None,
    transport: Optional[FakeTransport] = None,
    source: Optional[AccountSource] = None,
    api: Any = None,
    cli: Optional[FakeCli] = None,
    cron: Optional[FakeCron] = None,
    wrappers: Optional[FakeWrappers] = None,
    notifier: Optional[FakeNotifier] = None,
    config: Optional[FakeConfig] = None,
    hermes_home: Optional[Path] = None,
) -> Deps:
    """A fully faked ``Deps``: no network, no real clock, no real files."""
    fake_clock = clock or FakeClock()
    fake_store = store or FakeStore()
    fake_cli = cli or FakeCli()
    chosen_source = source
    if chosen_source is None:
        if pool_rows is not None:
            pool = FakePool(pool_rows)
            chosen_source = _PoolBackedSource(pool, salt=salt, cli=fake_cli, transport=transport)
        else:
            chosen_source = FakeSource(accounts, salt=salt)
    if transport is not None and isinstance(chosen_source, FakeSource):
        # A FakeSource with scripted routes already answers; a transport is
        # only attached when the test asked for one explicitly.
        pass
    if api is None:
        # The real API over the fake source: the usage read the summary needs
        # (D-017) resolves through the same scripted routes as every other call.
        from reset_rescue.codex_api import CodexResetApi

        api = CodexResetApi(chosen_source, sleep=fake_clock.sleep)
    scratch = os.environ.get("RR_SCRATCH") or os.path.join(
        os.environ.get("RR_TEMP_ROOT") or "/var/tmp", "rr-scratch"
    )
    home = hermes_home or Path(scratch) / "fake-home"
    deps = Deps(
        clock=fake_clock,
        sleep=fake_clock.sleep,
        source=chosen_source,
        api=api,
        store=fake_store,
        cli=fake_cli,
        cron=cron or FakeCron(),
        wrappers=wrappers or FakeWrappers(),
        notifier=notifier or FakeNotifier(),
        config=config or FakeConfig(),
        settings=settings or make_settings(),
        salt=salt,
        hermes_home=home,
        paths=Paths(
            data_dir=home / "plugin-data" / "reset-rescue",
            db_path=home / "plugin-data" / "reset-rescue" / "data.db",
            lock_path=home / "plugin-data" / "reset-rescue" / "run.lock",
        ),
    )
    return deps


class _PoolBackedSource(FakeSource):
    """A ``FakeSource`` whose accounts come from a :class:`FakePool`."""

    def __init__(
        self, pool: FakePool, *, salt: str, cli: FakeCli, transport: Optional[FakeTransport]
    ) -> None:
        super().__init__([], salt=salt)
        self.pool = pool
        self.cli = cli
        self.transport = transport or FakeTransport()

    def accounts(self) -> list[AccountRef]:
        from reset_rescue.accounts import PoolAccountSource

        source = PoolAccountSource(
            salt=self.salt,
            read_pool=self.pool.read,
            refresh=lambda entry_id: bool(
                self.cli.run(["auth", "refresh", "openai-codex", entry_id]).ok
            ),
            transport=self.transport,
        )
        refs = source.accounts()
        self._accounts = refs
        self._pool_source = source
        return refs


# --------------------------------------------------------------------- temp


def temp_home(name: str = "home") -> Path:
    """A fresh temp ``HERMES_HOME`` outside the real profile, verified (R17).

    It used to live under ``<HERMES_HOME>/cache/scratch/rr-l1`` — inside the
    real profile, which Hermes resolves back to ``~/.hermes``
    (``hermes_constants._hermes_root_of``). Tests that set ``HERMES_HOME`` to it
    were therefore writing the real profile. ``tests.support.isolation`` builds
    the home outside ``$HOME`` and refuses one that does not resolve to itself.
    """
    from tests.support.isolation import temp_home as isolated_home

    path = isolated_home(f"{name}-")
    (path / "plugin-data" / "reset-rescue").mkdir(parents=True, exist_ok=True)
    return path


def cleanup_home(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def env_for_home(path: Path) -> dict:
    """Environment for a subprocess that must see ``path`` as ``HERMES_HOME``."""
    from tests.support.isolation import env_for

    return env_for(path)
