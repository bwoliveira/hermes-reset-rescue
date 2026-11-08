"""Shared harness for the L2 flow tests (T-08/T-17/T-18/T-22/T-35).

Everything is offline: a fake backend that models the three states the real
service exposes (``available`` → ``pending``/``redeeming`` → ``redeemed``), a
fake ``AccountSource`` behind the **real** ``CodexResetApi`` (so classification,
parsing and the read-only transport under test are the shipped ones), a fake
cron, a fake notifier and the real ``Store`` on a temp data dir under the lane
scratch root.

The backend is deliberately stateful: the invariants are about what a run does
with a credit the backend still lists, so a test must be able to change the
backend between two runs and watch the second run re-decide.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from reset_rescue.accounts import (
    AccountRef,
    AccountSource,
    AuthExpired,
    Response,
    SourceUnavailable,
    account_key_for,
)
from reset_rescue.clock import Deps, Paths
from reset_rescue.codex_api import CodexResetApi
from reset_rescue.store import Store
from tests.fakes import SALT, FakeClock, FakeCron, FakeNotifier, make_settings
from tests.support.isolation import scratch_root

#: A scratch root **outside the real profile**. It used to be
#: ``<HERMES_HOME>/cache/scratch/rr-l2`` — inside ``~/.hermes``, which Hermes
#: resolves back to the real root, so every temp DB landed in real state
#: (R17). ``tests.support.isolation`` refuses a root under ``$HOME``.
SCRATCH = scratch_root()

DAY = 86_400.0
HOUR = 3_600.0
MINUTE = 60.0
SECOND = 1.0

CREDITS_FRAGMENT = "/rate-limit-reset-credits"
CONSUME_FRAGMENT = "/rate-limit-reset-credits/consume"
USAGE_FRAGMENT = "/wham/usage"


# -------------------------------------------------------------------- backend


class Credit:
    """One backend row, mutable so a test can move it between states."""

    def __init__(
        self,
        credit_id: str,
        *,
        expires_at: float,
        status: str = "available",
        title: str = "Full reset",
        reset_type: str = "codex_rate_limits",
        granted_at: Optional[float] = None,
        redeem_started_at: Optional[float] = None,
        redeemed_at: Optional[float] = None,
    ) -> None:
        self.id = credit_id
        self.expires_at = float(expires_at)
        self.status = status
        self.title = title
        self.reset_type = reset_type
        self.granted_at = granted_at
        self.redeem_started_at = redeem_started_at
        self.redeemed_at = redeemed_at

    def payload(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "reset_type": self.reset_type,
            "status": self.status,
            "granted_at": self.granted_at,
            "expires_at": self.expires_at,
            "redeem_started_at": self.redeem_started_at,
            "redeemed_at": self.redeemed_at,
        }

    def __repr__(self) -> str:  # never the raw id
        return f"Credit(status={self.status!r}, expires_at={self.expires_at!r})"


class Backend:
    """The Codex credit service, in memory.

    ``on_post`` / ``on_list`` / ``on_usage`` let a test script a failure without
    touching the flow. ``posts`` records every consume attempt so a test can
    prove the absence of one.
    """

    def __init__(self) -> None:
        self.rows: dict[str, list[Credit]] = {}
        self.posts: list[dict] = []
        self.lists: list[str] = []
        self.usages: list[str] = []
        self.on_post: Optional[Any] = None
        self.on_list: Optional[Any] = None
        self.on_usage: Optional[Any] = None
        self.fail_list: dict[str, Any] = {}
        self.windows_reset = 1
        self.usage_body: Mapping[str, Any] = {
            "rate_limit": {
                "primary_window": {"limit_window_seconds": 604800, "used_percent": 4.0},
                "secondary_window": {"limit_window_seconds": 18000, "used_percent": 0.0},
            }
        }

    # -- setup -------------------------------------------------------------
    def add(self, account_key: str, credit: Credit) -> Credit:
        self.rows.setdefault(account_key, []).append(credit)
        return credit

    def credit(self, account_key: str, credit_id: str = "credit-1", **kwargs: Any) -> Credit:
        return self.add(account_key, Credit(credit_id, **kwargs))

    def find(self, account_key: str, credit_id: str) -> Optional[Credit]:
        for row in self.rows.get(account_key, []):
            if row.id == credit_id:
                return row
        return None

    def remove(self, account_key: str, credit_id: str) -> None:
        rows = self.rows.get(account_key, [])
        self.rows[account_key] = [row for row in rows if row.id != credit_id]

    def only(self, account_key: str) -> Credit:
        rows = self.rows.get(account_key) or []
        assert len(rows) == 1, rows
        return rows[0]

    # -- the three endpoints ----------------------------------------------
    def list_payload(self, account_key: str) -> dict:
        self.lists.append(account_key)
        failure = self.fail_list.get(account_key)
        if failure is not None:
            raise failure
        if self.on_list is not None:
            scripted = self.on_list(account_key)
            if scripted is not None:
                return scripted
        rows = [row.payload() for row in self.rows.get(account_key, [])]
        return {
            "credits": rows,
            "available_count": sum(
                1 for row in rows if row["status"] in ("available", "redeeming")
            ),
        }

    def consume(self, account_key: str, credit_id: str, request_id: str) -> dict:
        self.posts.append(
            {"account_key": account_key, "credit_id": credit_id, "redeem_request_id": request_id}
        )
        if self.on_post is not None:
            scripted = self.on_post(account_key, credit_id, request_id)
            if scripted is not None:
                return scripted
        row = self.find(account_key, credit_id)
        if row is None:
            return {"code": "no_credit"}
        if row.status == "redeemed" or row.redeemed_at is not None:
            return {"code": "already_redeemed"}
        row.status = "redeemed"
        row.redeemed_at = time.time()
        return {"code": "reset", "windows_reset": self.windows_reset}

    def usage_payload(self, account_key: str) -> dict:
        self.usages.append(account_key)
        if self.on_usage is not None:
            scripted = self.on_usage(account_key)
            if scripted is not None:
                return scripted
        return dict(self.usage_body)


class BackendSource(AccountSource):
    """``AccountSource`` over :class:`Backend`; the real API sits on top."""

    kind = "pool"

    def __init__(self, backend: Backend, *, salt: str = SALT) -> None:
        self.backend = backend
        self.salt = salt
        self._accounts: list[AccountRef] = []
        self.requests: list[dict] = []
        self.fail_accounts: Optional[str] = None

    def accounts(self) -> list[AccountRef]:
        if self.fail_accounts:
            raise SourceUnavailable(self.fail_accounts)
        return list(self._accounts)

    def add_account(
        self, label: str = "b***@e***.com", account_key: Optional[str] = None
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
            {"account_key": account.account_key, "method": method.upper(), "url": url, "json": json}
        )
        if method.upper() == "POST":
            if CONSUME_FRAGMENT not in url:
                raise AssertionError(f"unexpected POST to {url}")
            body = json if isinstance(json, Mapping) else {}
            payload = self.backend.consume(
                account.account_key,
                str(body.get("credit_id") or ""),
                str(body.get("redeem_request_id") or ""),
            )
            return Response(status=200, body=payload)
        if CONSUME_FRAGMENT in url:
            raise AssertionError("consume must be a POST")
        if USAGE_FRAGMENT in url:
            return Response(status=200, body=self.backend.usage_payload(account.account_key))
        if CREDITS_FRAGMENT in url:
            try:
                return Response(status=200, body=self.backend.list_payload(account.account_key))
            except AuthExpired:
                return Response(status=401, body=None)
            except SourceUnavailable as exc:
                if exc.code in ("timeout", "network", "transient"):
                    return Response(status=503, body=None)
                raise
        raise AssertionError(f"unexpected URL {url}")

    def posts(self) -> list[dict]:
        return [req for req in self.requests if req["method"] == "POST"]

    def close(self) -> None:
        return None


# --------------------------------------------------------------------- harness


def credit_key_for(salt: str, account_key: str, credit_id: str) -> str:
    """The stored key of a backend credit (``fire.credit_key_for``)."""
    from reset_rescue.fire import credit_key_for as derive

    return derive(salt, account_key, credit_id)


class Harness:
    """One fully wired, fully faked run environment."""

    def __init__(
        self,
        case: Any,
        *,
        lead_minutes: int = 10,
        backup_lead_minutes: int = 4,
        dry_run: bool = False,
        automatic_rescue: bool = True,
        scan_interval_hours: int = 12,
        notify_target: str = "",
    ) -> None:
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(SCRATCH), prefix="flows-")
        case.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.data_dir = self.home / "plugin-data" / "reset-rescue"
        self.clock = FakeClock()
        self.backend = Backend()
        self.source = BackendSource(self.backend)
        self.api = CodexResetApi(self.source, sleep=self.clock.sleep)
        self.cron = FakeCron()
        self.notifier = FakeNotifier()
        self.settings = make_settings(
            lead_minutes=lead_minutes,
            backup_lead_minutes=backup_lead_minutes,
            dry_run=dry_run,
            automatic_rescue=automatic_rescue,
            scan_interval_hours=scan_interval_hours,
            notify_target=notify_target,
        )
        self.store = Store(self.data_dir, clock=self.clock.now)
        case.addCleanup(self.store.close)
        self.deps = Deps(
            clock=self.clock,
            sleep=self.clock.sleep,
            source=self.source,
            api=self.api,
            store=self.store,
            cron=self.cron,
            notifier=self.notifier,
            settings=self.settings,
            salt=SALT,
            hermes_home=self.home,
            paths=Paths(
                data_dir=self.data_dir,
                db_path=self.data_dir / "data.db",
                lock_path=self.data_dir / "run.lock",
            ),
        )

    # -- setup helpers ------------------------------------------------------
    def add_account(self, label: str = "b***@e***.com") -> AccountRef:
        return self.source.add_account(label)

    def add_credit(
        self,
        account: AccountRef,
        credit_id: str = "credit-1",
        *,
        expires_in: float = 30 * HOUR,
        **kwargs: Any,
    ) -> Credit:
        kwargs.setdefault("expires_at", self.clock.now() + expires_in)
        return self.backend.add(account.account_key, Credit(credit_id, **kwargs))

    def account(self, label: str = "b***@e***.com") -> AccountRef:
        for ref in self.source.accounts():
            if ref.label == label:
                return ref
        raise AssertionError(f"no account {label!r}")

    def key(self, account: AccountRef, credit_id: str = "credit-1") -> str:
        return credit_key_for(SALT, account.account_key, credit_id)

    def row(self, account: AccountRef, credit_id: str = "credit-1") -> Optional[dict]:
        return self.store.get_credit(self.key(account, credit_id))

    # -- running ------------------------------------------------------------
    def planner(self, **kwargs: Any):
        from reset_rescue.planner import Planner

        return Planner(self.deps, self.store, **kwargs)

    def fire(self):
        from reset_rescue.fire import Fire

        return Fire(self.deps, self.store)

    def scan(self, **kwargs: Any):
        return self.planner().scan(**kwargs)

    def run_fire(self, role: str = "primary", **kwargs: Any):
        return self.fire().run(role=role, **kwargs)

    def armed(
        self,
        *,
        expires_in: float = 30 * HOUR,
        credit_id: str = "credit-1",
        label: str = "b***@e***.com",
    ):
        """Add one account + credit and let a scan schedule it."""
        account = self.add_account(label)
        self.add_credit(account, credit_id, expires_in=expires_in)
        result = self.scan()
        assert result.exit_code == 0, result
        return account, self.key(account, credit_id)

    def armed_flow(
        self,
        *,
        expires_in: float = 30 * HOUR,
        credit_id: str = "credit-1",
        label: str = "b***@e***.com",
    ):
        """Same as :meth:`armed`, through the flows the harness wires directly."""
        account = self.add_account(label)
        self.add_credit(account, credit_id, expires_in=expires_in)
        result = self.planner().scan()
        assert result.exit_code == 0, result
        return account, self.key(account, credit_id)

    # -- assertions ---------------------------------------------------------
    def notices(self, kind: Optional[str] = None) -> list[dict]:
        rows = list(self.notifier.flushed)
        if kind is not None:
            rows = [row for row in rows if row["kind"] == kind]
        return rows

    def job_names(self) -> list[str]:
        return sorted(job["name"] for job in self.cron.jobs.values())

    def slot(self, account: AccountRef, role: str, credit_id: str = "credit-1") -> Optional[dict]:
        return self.store.get_slot(self.key(account, credit_id), role)

    def job_for(self, name: str) -> Optional[dict]:
        for job in self.cron.jobs.values():
            if job["name"] == name:
                return job
        return None

    def complete_job(self, name: str) -> None:
        """Make the scheduler report a job as finished (GC precondition)."""
        job = self.job_for(name)
        assert job is not None, name
        job["state"] = "completed"
        job["enabled"] = False

    def advance_to(self, epoch: float) -> None:
        self.clock.set(epoch)

    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds)
