"""Native scheduler e2e: scan/arm, failed primary + backup, quota and cleanup.

Only the backend is fake. Every scheduled invocation is dispatched by Hermes'
InProcessCronScheduler, never by calling tick/fire or a wrapper ourselves.
Timing fixtures rewind persisted due times (and quota cooldowns), not the clock.
Requires an isolated mount/network namespace, read-only live home/install, and
loopback only. No connectivity probe ever attempts the real backend.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

OWN_REPO = Path(__file__).resolve().parents[2]
E2E_DIR = OWN_REPO / "tests" / "e2e"
LIVE_HERMES_HOME = Path(os.environ.get("RR_LIVE_HERMES_HOME") or Path.home() / ".hermes")
HERMES_INSTALL = Path(os.environ.get("RR_HERMES_INSTALL") or "/usr/local/lib/hermes-agent")
for _entry in (str(OWN_REPO / "scripts"), str(HERMES_INSTALL), str(E2E_DIR)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

import offline_guard  # noqa: E402
import run_e2e as base  # noqa: E402
from fake_backend import (  # noqa: E402
    CONSUME_PATH,
    CREDITS_PATH,
    USAGE_PATH,
    FakeBackend,
    credit_row,
    credits_payload,
    jwt_for,
    pool_row,
    write_auth,
)

TICKER_ROOT = Path(os.environ.get("RR_TICKER_HOME") or "/var/tmp/rr-rt/ticker-home")
FIRE_TIMEOUT_S = float(os.environ.get("RR_TICKER_TIMEOUT_S") or 180)
WORKSPACE = base.WORKSPACE


def _preflight() -> None:
    """Refuse unsafe execution before seed, CLI, listeners or scheduler imports."""
    base.check(
        os.environ.get("RR_TICKER_ISOLATED") == "1",
        "run through scripts/ticker_check.sh (unshare -mn is mandatory)",
    )
    for path in (LIVE_HERMES_HOME, HERMES_INSTALL):
        base.check(
            bool(os.statvfs(path).f_flag & os.ST_RDONLY), f"{path} must be bind-mounted read-only"
        )
    for path in (
        TICKER_ROOT,
        Path(os.environ.get("TMPDIR", "")),
        Path(os.environ.get("HERMES_HOME", "")),
    ):
        base.check(
            path.resolve().is_relative_to(Path("/var/tmp")), f"unsafe temporary path: {path}"
        )
    base.check(
        os.environ.get("HERMES_DISABLE_LAZY_INSTALLS") == "1",
        "HERMES_DISABLE_LAZY_INSTALLS=1 is required",
    )
    interfaces = {name for _, name in socket.if_nameindex()}
    base.check(interfaces == {"lo"}, f"network namespace has non-loopback links: {interfaces}")
    base.check(int(Path("/sys/class/net/lo/flags").read_text().strip(), 16) & 1, "loopback is down")
    base.assert_isolated(Path(os.environ["HERMES_HOME"]))


def _jobs(home: Path) -> list[dict[str, Any]]:
    data = json.loads((home / "cron/jobs.json").read_text())
    return data["jobs"] if isinstance(data, dict) else data


def _job(runner: base.Runner, name: str) -> dict[str, Any]:
    hits = [j for j in _jobs(runner.home) if j.get("name") == name]
    base.check(len(hits) == 1, f"expected one {name} job, found {len(hits)}")
    base.check(hits[0].get("no_agent") is True, f"{name} is not script-only")
    return hits[0]


def _rows(runner: base.Runner, sql: str, params: tuple = ()) -> list[tuple]:
    with runner.state() as conn:
        return conn.execute(sql, params).fetchall()


def _poll(predicate: Callable[[], Any], description: str) -> Any:
    deadline = time.monotonic() + FIRE_TIMEOUT_S
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        threading.Event().wait(0.1)
    raise base.CheckFailed(f"timed out waiting for {description}")


def _attempts(home: Path, job_id: str) -> list[tuple]:
    path = home / "cron/executions.db"
    if not path.exists():
        return []
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        return conn.execute(
            "SELECT id,status,source FROM executions WHERE job_id=?", (job_id,)
        ).fetchall()


def _fire(runner: base.Runner, job_id: str, *, success: bool = True) -> None:
    """One real native tick, awaiting its terminal ledger row, not a fixed sleep."""
    from cron.scheduler_provider import InProcessCronScheduler

    before = {r[0] for r in _attempts(runner.home, job_id)}
    path = runner.home / "cron/jobs.json"
    data = json.loads(path.read_text())
    jobs = data["jobs"] if isinstance(data, dict) else data
    matches = [j for j in jobs if j["id"] == job_id]
    base.check(len(matches) == 1, f"cannot rewind missing job {job_id}")
    from datetime import datetime, timezone

    matches[0]["next_run_at"] = datetime.fromtimestamp(time.time() - 2, timezone.utc).isoformat()
    temporary = path.with_suffix(".e2e.tmp")
    temporary.write_text(json.dumps(data))
    temporary.replace(path)
    stop = threading.Event()
    errors: list[BaseException] = []

    def run() -> None:
        try:
            InProcessCronScheduler().start(stop_event=stop, interval=1)
        except BaseException as exc:
            errors.append(exc)

    ticker = threading.Thread(target=run, name="native-e2e-ticker", daemon=True)
    ticker.start()
    try:

        def terminal() -> Any:
            base.check(not errors, f"native scheduler crashed: {errors}")
            return [
                r
                for r in _attempts(runner.home, job_id)
                if r[0] not in before
                and r[1] in {"completed", "failed", "unknown", "cancelled", "missed"}
            ]

        records = _poll(terminal, f"native execution {job_id}")
        base.check(len(records) == 1, f"duplicate native attempts: {records}")
        if records[0][1] != ("completed" if success else "failed"):
            with sqlite3.connect(runner.home / "cron/executions.db") as conn:
                print(
                    "  failed execution diagnostic:",
                    conn.execute(
                        "SELECT * FROM executions WHERE id=?", (records[0][0],)
                    ).fetchone(),
                )
        base.check(
            records[0][1] == ("completed" if success else "failed"),
            f"unexpected native execution outcome: {records}",
        )
        base.check(records[0][2] == "builtin", f"not native ticker provenance: {records}")
        print(f"  native job {job_id}: {records[0][1]} (ledger {records[0][0]})")
    finally:
        stop.set()
        ticker.join(timeout=30)
        base.check(not ticker.is_alive(), "native ticker did not stop")


def _config(runner: base.Runner, settings: dict[str, Any]) -> None:
    done = runner.hermes(
        "config", "set", "plugins.entries.reset-rescue.settings", json.dumps(settings)
    )
    base.check(done.returncode == 0, f"settings save failed: {done.stdout}\n{done.stderr}")


def _scan_assert(runner: base.Runner, backend: FakeBackend) -> None:
    _fire(runner, _job(runner, "reset-rescue-scan")["id"])
    status = runner.status()
    base.check(
        status["schedule"]["armed"] is True and not status["schedule"].get("problem_code"),
        f"scan did not arm: {status}",
    )
    slots = _rows(
        runner,
        "SELECT s.role,s.job_id FROM slots s JOIN credits c USING(credit_key) "
        "WHERE s.role IN ('primary','backup') AND c.status='scheduled'",
    )
    base.check(slots and all(j for _, j in slots), f"unarmed rescue slots: {slots}")
    ids = {j["id"] for j in _jobs(runner.home)}
    base.check(all(j in ids for _, j in slots), "slot references missing native job")
    base.check(not backend.posts(), "scan spent a credit outside the lead window")


def _primary_success(runner: base.Runner, backend: FakeBackend) -> None:
    base.section("native primary: one consume -> redeemed readback -> local success notice")
    key = base._make_primary_due(runner, backend)
    slots = dict(_rows(runner, "SELECT role,job_id FROM slots WHERE credit_key=?", (key,)))
    before = len(backend.requests)
    _fire(runner, slots["primary"])
    base._assert_state(runner, key)
    posts = backend.posts()
    base.check(len(posts) == 1, f"successful primary consumed {len(posts)} times")
    fresh = backend.requests[before:]
    post_index = next(i for i, request in enumerate(fresh) if request.method == "POST")
    base.check(
        any(r.method == "GET" and r.path == CREDITS_PATH for r in fresh[post_index + 1 :]),
        "successful POST was not followed by a credits readback",
    )
    base.check(
        _rows(runner, "SELECT confirmed_at FROM credits WHERE credit_key=?", (key,))[0][0],
        "successful primary lacks confirmed readback timestamp",
    )
    notice = _rows(
        runner, "SELECT state,target FROM outbox WHERE credit_key=? AND kind='success'", (key,)
    )
    base.check(
        len(notice) == 1 and notice[0][0] in ("queued", "skipped"),
        f"success notice not retained locally: {notice}",
    )
    base.check(notice[0][1] in (None, "local"), f"notice escaped local target: {notice}")
    base.check(
        runner.status()["settings"]["notify_target_display"] == "local", "not a local target"
    )
    # I7/I8: finish the inert backup, then age only the persisted GC deadlines
    # past the script timeout + ten-minute margin. Never bypass live-job guards.
    with runner.state_rw() as conn:
        conn.execute(
            "UPDATE slots SET fire_at=? WHERE credit_key=? AND role='backup'",
            (time.time() - 1, key),
        )
    _fire(runner, slots["backup"])
    with runner.state_rw() as conn:
        conn.execute("UPDATE slots SET fire_at=? WHERE credit_key=?", (time.time() - 7200, key))
    _fire(runner, _job(runner, "reset-rescue-scan")["id"])
    live_ids = {j["id"] for j in _jobs(runner.home)}
    base.check(not live_ids.intersection(slots.values()), "settled credit plugin jobs remain")
    base.check(len(backend.posts()) == 1, "primary cleanup consumed again")
    print(
        "SCENARIO PASS | Successful primary | one POST; redeemed readback; "
        f"local outbox={notice[0][0]} (unsent); native scan removed credit jobs"
    )


def _retry_backup(runner: base.Runner, backend: FakeBackend) -> None:
    base.section("native primary: real 503 retries; native backup reuses persisted UUID")
    key = base._make_primary_due(runner, backend)
    slots = dict(_rows(runner, "SELECT role,job_id FROM slots WHERE credit_key=?", (key,)))
    backend.consume_status = 503
    backend.consume = {"error": "temporary_unavailable"}
    _fire(runner, slots["primary"], success=False)
    posts = backend.posts()
    base.check(len(posts) >= 2, "503 primary did not perform a real retry")
    request_id = _rows(runner, "SELECT redeem_request_id FROM credits WHERE credit_key=?", (key,))[
        0
    ][0]
    base.check(str(uuid.UUID(request_id)) == request_id, "persisted request id is not a UUID")
    base.check(
        {p.body.get("redeem_request_id") for p in posts} == {request_id},
        "primary retries changed UUID",
    )
    base.check(
        _rows(runner, "SELECT status FROM credits WHERE credit_key=?", (key,))[0][0] != "redeemed",
        "503 was falsely settled",
    )
    with runner.state_rw() as conn:
        conn.execute(
            "UPDATE slots SET fire_at=? WHERE credit_key=? AND role='backup'",
            (time.time() - 1, key),
        )
    backend.consume_status = 200
    backend.consume = {"code": "reset", "windows_reset": 1}
    before = len(posts)
    _fire(runner, slots["backup"])
    base.check(len(backend.posts()) == before + 1, "backup did not consume exactly once")
    base.check(
        {p.body.get("redeem_request_id") for p in backend.posts()} == {request_id},
        "backup lost the primary UUID across worker processes",
    )
    _poll(
        lambda: _rows(
            runner,
            "SELECT confirmed_at FROM credits WHERE credit_key=? AND status='redeemed'",
            (key,),
        ),
        "backup settlement",
    )
    base.check(
        _rows(runner, "SELECT ran_at FROM slots WHERE credit_key=? AND role='backup'", (key,))[0][
            0
        ],
        "backup slot not marked run",
    )
    base._assert_history(runner)
    base._assert_notice(runner)
    print(
        f"SCENARIO PASS | 503 primary to backup | primary POSTs={before}; "
        "backup POSTs=1; persisted UUID reused"
    )


def _quota(
    runner: base.Runner, backend: FakeBackend, token: str, other: str, *, account_only: bool
) -> None:
    label = "account-only" if account_only else "global"
    base.section(f"native quota: {label} 15% threshold, repeated unchanged readings")
    # Retire earlier two-minute expiry fixtures before their backend entries
    # disappear, avoiding wall-time-dependent missed-credit errors.
    with runner.state_rw() as conn:
        conn.execute(
            "UPDATE credits SET expires_at=? WHERE status='scheduled'",
            (time.time() + 7200,),
        )
    backend.reset()
    backend.credits_by_token = {
        token: credits_payload(
            credit_row(f"{label}-first", expires_in=86_400),
            credit_row(f"{label}-spare", expires_in=90_000),
        ),
        other: credits_payload(credit_row(f"{label}-other", expires_in=95_000)),
    }
    account_key = _rows(
        runner, "SELECT account_key FROM accounts WHERE label=?", ("b***@e***.com",)
    )[0][0]
    settings: dict[str, Any] = {
        "early_redeem_enabled": not account_only,
        "early_redeem_remaining_pct": 15,
    }
    if account_only:
        settings["account_overrides"] = {
            account_key: {"early_redeem_enabled": True, "early_redeem_remaining_pct": 15}
        }
    _config(runner, settings)
    _scan_assert(runner, backend)
    job_id = _job(runner, "reset-rescue-quota")["id"]
    backend.usage = {
        "rate_limit": {
            "primary_window": {"limit_window_seconds": 604800, "used_percent": 84},
            "secondary_window": {"limit_window_seconds": 18000, "used_percent": 0},
        }
    }

    def age_cooldowns() -> None:
        with runner.state_rw() as conn:
            conn.execute("UPDATE meta SET value='0' WHERE key LIKE 'quota_cooldown:%'")

    usage_before = len([r for r in backend.requests if r.path == USAGE_PATH])
    age_cooldowns()
    _fire(runner, job_id)
    expected = 1 if account_only else 2
    usage_delta = len([r for r in backend.requests if r.path == USAGE_PATH]) - usage_before
    base.check(
        usage_delta == expected,
        f"healthy quota check read {usage_delta} accounts, expected {expected}",
    )
    base.check(not backend.posts(), "16% remaining incorrectly crossed 15% threshold")
    # Advance only the persisted cooldown fixture, retaining the spent/re-arm guard.
    age_cooldowns()
    settled_before = _rows(
        runner, "SELECT count(*) FROM credits WHERE status='redeemed' AND confirmed_at IS NOT NULL"
    )[0][0]
    backend.usage["rate_limit"]["primary_window"]["used_percent"] = 85
    _fire(runner, job_id)
    base.check(
        _rows(
            runner,
            "SELECT count(*) FROM credits WHERE status='redeemed' AND confirmed_at IS NOT NULL",
        )[0][0]
        == settled_before + expected,
        "quota consume was not confirmed by readback",
    )
    base.check(len(backend.posts()) == expected, f"{label} quota POSTs: {backend.posts()}")
    base.check(
        {p.body.get("credit_id") for p in backend.posts()}
        == ({f"{label}-first"} if account_only else {f"{label}-first", f"{label}-other"}),
        "wrong account or wrong soonest credit redeemed",
    )
    base.check(
        all(
            str(uuid.UUID(p.body["redeem_request_id"])) == p.body["redeem_request_id"]
            for p in backend.posts()
        ),
        "quota POST lacks UUID",
    )
    for _ in range(2):
        age_cooldowns()
        reads = len([r for r in backend.requests if r.path == USAGE_PATH])
        _fire(runner, job_id)
        base.check(
            len([r for r in backend.requests if r.path == USAGE_PATH]) == reads + 1,
            "repeat did not read the spare credit account's fresh unchanged quota",
        )
        base.check(len(backend.posts()) == expected, "unchanged 15% drained another credit")
    base.check(
        any(
            r[0] == "spent"
            for r in _rows(runner, "SELECT value FROM meta WHERE key LIKE 'early_rearm:%'")
        ),
        "missing persistent spent guard",
    )
    name = "Account-only quota" if account_only else "Global quota"
    print(
        f"SCENARIO PASS | {name} | 16% no spend; 15% inclusive; "
        "two fresh unchanged checks; no extra POST"
    )


def _disable_uninstall(runner: base.Runner, backend: FakeBackend) -> None:
    base.section("disabled automation and uninstall preserve a foreign prefix canary")
    done = runner.hermes(
        "cron",
        "create",
        "every 24h",
        "--name",
        "reset-rescue-foreign-canary",
        "--script",
        "reset-rescue-scan.py",
        "--no-agent",
        "--deliver",
        "local",
    )
    base.check(done.returncode == 0, f"cannot create canary: {done.stderr}")
    canary = _job(runner, "reset-rescue-foreign-canary")
    base.check(
        not _rows(runner, "SELECT job_id FROM owned_jobs WHERE job_id=?", (canary["id"],)),
        "foreign canary accidentally registered as owned",
    )
    done = runner.hermes("plugins", "disable", "reset-rescue")
    base.check(done.returncode == 0, f"plugin disable failed: {done.stdout} {done.stderr}")
    import yaml

    config = yaml.safe_load((runner.home / "config.yaml").read_text())
    base.check("reset-rescue" in config["plugins"]["disabled"], "disable not persisted")
    base.check("reset-rescue" not in config["plugins"]["enabled"], "plugin still enabled")
    live_ids = {j["id"] for j in _jobs(runner.home) if j.get("enabled")}
    candidates = [
        r
        for r in _rows(
            runner,
            "SELECT s.credit_key,s.job_id FROM slots s JOIN credits c USING(credit_key) "
            "WHERE s.role='primary' AND c.status='scheduled'",
        )
        if r[1] in live_ids
    ]
    base.check(candidates, "disable test has no live unsettled primary canary")
    key, job_id = candidates[0]
    with runner.state_rw() as conn:
        conn.execute(
            "UPDATE slots SET fire_at=? WHERE credit_key=? AND role='primary'",
            (time.time() - 1, key),
        )
        conn.execute("UPDATE credits SET expires_at=? WHERE credit_key=?", (time.time() + 120, key))
    for payload in backend.credits_by_token.values():
        for row in payload.get("credits", []):
            row["expires_at"] = time.time() + 120
    before = len(backend.posts())
    reads_before = len(backend.requests)
    _fire(runner, job_id, success=False)
    _fire(runner, _job(runner, "reset-rescue-scan")["id"], success=False)
    base.check(len(backend.requests) == reads_before, "disabled plugin reached the backend")
    base.check(len(backend.posts()) == before, "disabled native job spent a credit")
    base.check(
        _job(runner, "reset-rescue-foreign-canary") == canary, "disable mutated foreign canary"
    )
    persisted = _rows(
        runner, "SELECT credit_key,redeem_request_id FROM credits ORDER BY credit_key"
    )
    print(
        "SCENARIO PASS | Plugin disable | CLI disable persisted; primary + scan inert; "
        "zero HTTP; foreign job unchanged"
    )
    done = runner.hermes("config", "set", "plugins.disabled", "[]")
    base.check(done.returncode == 0, "cannot clear disabled selection for uninstall")
    base._enable(runner)
    done = runner.hermes("reset-rescue", "uninstall", "--keep-data")
    base.check(done.returncode == 0, f"uninstall failed: {done.stdout}\n{done.stderr}")
    base.check(_jobs(runner.home) == [canary], "uninstall did not remove only owned jobs")
    base.check(
        _rows(runner, "SELECT credit_key,redeem_request_id FROM credits ORDER BY credit_key")
        == persisted,
        "keep-data discarded persistent UUIDs",
    )
    base.check(
        not list((runner.home / "scripts").glob("reset-rescue-*.py")),
        "uninstall left owned wrappers",
    )
    base.check(len(backend.posts()) == before, "uninstall caused a POST")
    print(
        "SCENARIO PASS | Uninstall ownership | only foreign canary remains; "
        "UUIDs retained; owned wrappers removed"
    )


def main(argv: list[str]) -> int:
    started = time.monotonic()
    keep = "--keep" in argv
    saved_env = dict(os.environ)
    home: Path | None = None
    try:
        _preflight()
        base.E2E_HOME = TICKER_ROOT
        home = base._prepare_home()
        base.assert_isolated(home)
        offline_guard.install(real_home=str(home), allowed_roots=(str(base.REPO), str(TICKER_ROOT)))
        with (
            offline_guard.allow_subprocess("hermes", "openssl", "python3", "python"),
            offline_guard.allow_loopback("chatgpt.com"),
            FakeBackend(home.parent) as backend,
        ):
            runner = base.Runner(home, base._env_for(home, backend))
            token = jwt_for(WORKSPACE, "bob@example.com", user_id="user-jane")
            other = jwt_for(WORKSPACE, "ana@example.com", user_id="user-ana")
            write_auth(
                home,
                [
                    pool_row("e2e0000000000001", token, WORKSPACE, "bob@example.com"),
                    pool_row("e2e0000000000002", other, WORKSPACE, "ana@example.com"),
                ],
            )
            backend.credits_by_token = {
                token: credits_payload(credit_row("credit-e2e-1", expires_in=3600)),
                other: credits_payload(credit_row("credit-e2e-2", expires_in=3600)),
            }
            base._enable(runner)
            _config(runner, {"notify_target": "local"})
            base._ensure(runner)
            os.environ.update(runner.env)
            base.section(
                "native recurring scan: empty state -> discovered credit -> primary + backup"
            )
            base.check(not _rows(runner, "SELECT credit_key FROM credits"), "not a fresh credit DB")
            base.check(
                not _rows(runner, "SELECT job_id FROM slots"), "slots existed before native scan"
            )
            _scan_assert(runner, backend)
            print(
                "SCENARIO PASS | Native scan arming | empty DB; primary + backup inside 48h; "
                "zero CLI scan; zero POST"
            )
            _primary_success(runner, backend)
            backend.reset()
            backend.credits_by_token = {
                token: credits_payload(credit_row("backup-credit-1", expires_in=3600)),
                other: credits_payload(credit_row("backup-credit-2", expires_in=3600)),
            }
            _scan_assert(runner, backend)
            _retry_backup(runner, backend)
            _quota(runner, backend, token, other, account_only=False)
            _quota(runner, backend, token, other, account_only=True)
            _disable_uninstall(runner, backend)
            base._assert_no_secret_leak(runner, token)
            requests = backend.requests
            base.check(
                all(
                    r.path in {CONSUME_PATH, CREDITS_PATH, USAGE_PATH}
                    and r.has_bearer
                    and r.headers.get("chatgpt-account-id") == WORKSPACE
                    for r in requests
                ),
                "backend saw unexpected route, auth or workspace",
            )
            print(
                "SCENARIO PASS | Privacy and isolation | no token leak; loopback-only backend; "
                "protected read-only mounts; checked CLI homes"
            )
        print(
            "\nE2E OK (native scan, successful primary, 503/backup UUID, "
            "global/account quota, disable/uninstall; "
            f"{time.monotonic() - started:.1f}s)"
        )
        return 0
    except Exception as exc:
        print(f"\nE2E FAIL: {type(exc).__name__}: {exc}")
        return 1
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        if not keep and home is not None:
            base._cleanup_home(home)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
