"""T-21 / T-74 — the host verbs: fire (three roles) and quota-check.

These are the paths a cron job actually runs, so the assertions are about the
contract with cron rather than about internals:

* ``fire --role`` passes the role through untouched and returns an exit code
  plus a short stderr summary (I6), never a traceback.
* ``fire --role remind`` never reaches a rescue path — a reminder must not be
  able to spend a credit.
* ``quota-check`` calls the real planner flow (R15) and reports a failure when
  the flow fails or is absent.
* ``ensure`` arms the quota job only when it can do something (§3).
"""

from __future__ import annotations

import unittest

from reset_rescue.clock import Deps
from reset_rescue.cron_gateway import CronGateway, quota_spec
from reset_rescue.host import QUOTA_JOB_NAME, HostService
from reset_rescue.notify import Notifier
from reset_rescue.planner import Planner
from reset_rescue.settings import Settings
from tests.fakes import parse_create_argv


class _Cli:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.jobs: list[dict] = []
        self._seq = 0

    def run(self, argv, *, timeout: float = 60.0):
        from reset_rescue.cli_runner import CliResult

        call = [str(part) for part in argv]
        self.calls.append(call)
        if call[:2] == ["cron", "list"]:
            return CliResult(argv=tuple(call), returncode=0, stdout=self._listing())
        if call[:2] == ["cron", "create"]:
            spec = parse_create_argv(call)
            if spec is None:
                return CliResult(
                    argv=tuple(call), returncode=1, stdout="Failed to create job: bad argv\n"
                )
            self._seq += 1
            job_id = f"{self._seq:012x}"
            self.jobs.append(
                {
                    "id": job_id,
                    "name": spec.name,
                    "state": "active",
                    "enabled": True,
                    "schedule": spec.schedule,
                    "script": spec.script,
                    "deliver": spec.deliver,
                }
            )
            return CliResult(argv=tuple(call), returncode=0, stdout=f"Created job: {job_id}\n")
        if call[:2] == ["cron", "remove"]:
            job_id = call[2]
            self.jobs = [job for job in self.jobs if job["id"] != job_id]
            return CliResult(
                argv=tuple(call), returncode=0, stdout=f"Removed job: (unnamed) ({job_id})\n"
            )
        return CliResult(argv=tuple(call), returncode=0, stdout="")

    def _listing(self) -> str:
        if not self.jobs:
            return "No scheduled jobs in profile 'custom'.\n"
        return (
            "\n\n".join(
                "\n".join(
                    [
                        f"  {job['id']} [{job['state']}]",
                        f"    Name:      {job['name']}",
                        f"    Schedule:  {job['schedule']}",
                        f"    Deliver:   {job['deliver']}",
                        f"    Script:    {job['script']}",
                    ]
                )
                for job in self.jobs
            )
            + "\n"
        )

    def count(self, *prefix: str) -> int:
        return sum(1 for call in self.calls if tuple(call[: len(prefix)]) == prefix)


class _Store:
    """The store surface ``HostService`` touches."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.credits: list[dict] = []
        self.notices: list[dict] = []
        self._notice_id = 0

    def record_job(self, job_id: str, job_name: str, kind: str, script: str = "") -> None:
        self.jobs[job_id] = {
            "job_id": job_id,
            "job_name": job_name,
            "kind": kind,
            "removed_at": None,
        }

    def mark_job_removed(self, job_id: str, now=None) -> None:
        row = self.jobs.get(job_id)
        if row is not None:
            row["removed_at"] = 1.0 if now is None else float(now)

    def list_owned_jobs(self, *, active_only: bool = False, kind=None) -> list[dict]:
        rows = [dict(row) for row in self.jobs.values()]
        if active_only:
            rows = [row for row in rows if row.get("removed_at") is None]
        if kind is not None:
            rows = [row for row in rows if row.get("kind") == kind]
        return rows

    def get_job_by_name(self, job_name: str):
        for row in self.jobs.values():
            if row["job_name"] == job_name:
                return dict(row)
        return None

    def list_credits(self, *, account_key=None, statuses=None) -> list[dict]:
        rows = [dict(row) for row in self.credits]
        if statuses:
            rows = [row for row in rows if row.get("status") in statuses]
        return rows

    def credit_list(self, *, account_key=None, statuses=None) -> list[dict]:
        return self.list_credits(account_key=account_key, statuses=statuses)

    def account_list(self) -> list[dict]:
        return []

    def enqueue_notice(
        self,
        dedupe_key: str,
        *,
        kind: str,
        message_key: str,
        params=None,
        target=None,
        credit_key=None,
        now=None,
    ) -> int:
        self._notice_id += 1
        self.notices.append(
            {
                "id": self._notice_id,
                "dedupe_key": dedupe_key,
                "kind": kind,
                "message_key": message_key,
                "params_json": __import__("json").dumps(params or {}),
                "target": target,
                "state": "queued",
            }
        )
        return self._notice_id

    def list_notices(self, *, state=None, limit=None) -> list[dict]:
        rows = [dict(row) for row in self.notices]
        if state is not None:
            rows = [row for row in rows if row.get("state") == state]
        return rows[:limit] if limit else rows

    def mark_notice_sent(self, notice_id, message_id=None, now=None) -> None:
        for row in self.notices:
            if row["id"] == notice_id:
                row["state"] = "sent"

    def mark_notice_failed(self, notice_id, code=None, now=None) -> None:
        for row in self.notices:
            if row["id"] == notice_id:
                row["state"] = "failed"

    def mark_notice_skipped(self, notice_id, code=None, now=None) -> None:
        for row in self.notices:
            if row["id"] == notice_id:
                row["state"] = "skipped"

    def meta_get(self, key: str):
        return None

    def meta_set(self, key: str, value: str) -> None:
        return None


class _Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self._now = now

    def now(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += seconds


class _Fire:
    def __init__(self, *, exit_code: int = 0, problem: str = "") -> None:
        self.roles: list[str] = []
        self.exit_code = exit_code
        self.problem = problem
        self.reminded = 0
        self.quota_checks = 0
        self.quota_exit = 0

    def run(self, role: str, *, dry_run: bool = False):
        self.roles.append(role)
        self.last_dry_run = dry_run
        return type(
            "R", (), {"exit_code": self.exit_code, "problem_code": self.problem, "results": []}
        )()

    def run_remind(self):
        self.reminded += 1
        return type("R", (), {"exit_code": 0, "problem_code": "", "results": []})()

    def run_quota_check(self):
        # Not a Fire method any more: R15 moved the flow to the planner, and
        # this stub stays only so a regression that calls it is visible.
        self.quota_checks += 1
        return type("R", (), {"exit_code": self.quota_exit, "stderr": ""})()


def _service(
    *, settings: Settings | None = None, fire: _Fire | None = None
) -> tuple[HostService, _Cli, _Store, _Fire]:
    cli = _Cli()
    store = _Store()
    engine = fire or _Fire()
    deps = Deps(
        clock=_Clock(),
        cli=cli,
        cron=CronGateway(cli),
        store=store,
        notifier=Notifier(cli, target="telegram", store=store),
        settings=settings or Settings(),
        fire=engine,
    )
    deps.planner = Planner(deps, store)
    return HostService(deps), cli, store, engine


class FireTests(unittest.TestCase):
    def test_the_role_reaches_the_engine_unchanged(self):
        service, cli, store, engine = _service()
        for role in ("primary", "backup"):
            out = service.fire(role=role)
            self.assertEqual(out.exit_code, 0, role)
        self.assertEqual(engine.roles, ["primary", "backup"])

    def test_a_failure_returns_a_short_reason_and_no_traceback(self):
        service, cli, store, engine = _service(fire=_Fire(exit_code=1, problem="network"))
        out = service.fire(role="primary")
        self.assertEqual(out.exit_code, 1)
        self.assertIn("network error", out.stderr)
        self.assertNotIn("Traceback", out.stderr)
        self.assertEqual(out.stdout, "")

    def test_an_unknown_role_is_a_usage_error(self):
        service, cli, store, engine = _service()
        out = service.fire(role="nonsense")
        self.assertEqual(out.exit_code, 2)
        self.assertEqual(engine.roles, [])

    def test_the_remind_role_never_reaches_a_rescue(self):
        """A reminder must not be able to spend a credit (I5-lite)."""
        service, cli, store, engine = _service()
        out = service.fire(role="remind")
        self.assertEqual(out.exit_code, 0)
        self.assertEqual(engine.roles, [])
        self.assertEqual(engine.reminded, 1)

    def test_a_missing_engine_fails_cleanly(self):
        service, cli, store, engine = _service()
        service.deps.fire = None
        out = service.fire(role="primary")
        self.assertEqual(out.exit_code, 1)
        self.assertIn("not available", out.stderr)


class _Planner:
    """The real flow's shape: ``quota_check()`` returns a result object."""

    def __init__(self, *, exit_code: int = 0, problem: str = "") -> None:
        self.calls = 0
        self.exit_code = exit_code
        self.problem = problem

    def quota_check(self):
        self.calls += 1
        return type(
            "Q",
            (),
            {
                "exit_code": self.exit_code,
                "problem_code": self.problem,
                "accounts": 0,
                "cooled": 0,
                "redeemed": 0,
                "failed": 0,
                "results": [],
            },
        )()


class QuotaCheckTests(unittest.TestCase):
    def test_quota_check_reaches_the_real_planner_flow(self):
        """R15: the host verb must run the flow that exists, not a missing one."""
        service, cli, store, engine = _service()
        planner = _Planner()
        service.deps.planner = planner
        out = service.quota_check()
        self.assertEqual(out.exit_code, 0)
        self.assertEqual(planner.calls, 1)
        self.assertEqual(engine.quota_checks, 0, "the missing Fire method was called")

    def test_a_failing_quota_check_reports_it(self):
        service, cli, store, _ = _service()
        service.deps.planner = _Planner(exit_code=1, problem="network")
        out = service.quota_check()
        self.assertEqual(out.exit_code, 1)
        self.assertIn("network error", out.stderr)

    def test_a_process_with_no_planner_has_nothing_to_run(self):
        """R15 (FA's contract): no planner at all is the one honest no-op."""
        service, cli, store, engine = _service()
        service.deps.planner = None
        out = service.quota_check()
        self.assertEqual(out.exit_code, 0)
        self.assertEqual(engine.quota_checks, 0)


class EnsureQuotaJobTests(unittest.TestCase):
    def test_the_quota_job_is_armed_when_early_redeem_is_on_and_a_credit_exists(self):
        service, cli, store, engine = _service(settings=Settings(early_redeem_enabled=True))
        service.deps.wrappers = _NullWrappers()
        store.credits = [{"credit_key": "a" * 16, "status": "seen", "expires_at": 1_800_100_000.0}]
        dto = service.ensure()
        names = {job["name"] for job in cli.jobs}
        self.assertIn(QUOTA_JOB_NAME, names)
        self.assertIn(QUOTA_JOB_NAME, dto.created)

    def test_the_quota_job_is_not_armed_when_early_redeem_is_off(self):
        service, cli, store, engine = _service(settings=Settings(early_redeem_enabled=False))
        service.deps.wrappers = _NullWrappers()
        store.credits = [{"credit_key": "a" * 16, "status": "seen", "expires_at": 1_800_100_000.0}]
        service.ensure()
        self.assertNotIn(QUOTA_JOB_NAME, {job["name"] for job in cli.jobs})

    def test_the_quota_job_is_not_armed_without_a_credit(self):
        """No unsettled credit = nothing for the job to do (§3)."""
        service, cli, store, engine = _service(settings=Settings(early_redeem_enabled=True))
        service.deps.wrappers = _NullWrappers()
        store.credits = []
        service.ensure()
        self.assertNotIn(QUOTA_JOB_NAME, {job["name"] for job in cli.jobs})

    def test_turning_early_redeem_off_removes_the_job(self):
        service, cli, store, engine = _service(settings=Settings(early_redeem_enabled=True))
        service.deps.wrappers = _NullWrappers()
        store.credits = [{"credit_key": "a" * 16, "status": "seen", "expires_at": 1_800_100_000.0}]
        service.ensure()
        self.assertIn(QUOTA_JOB_NAME, {job["name"] for job in cli.jobs})
        service.deps.settings = Settings(early_redeem_enabled=False)
        dto = service.ensure()
        self.assertNotIn(QUOTA_JOB_NAME, {job["name"] for job in cli.jobs})
        self.assertIn(QUOTA_JOB_NAME, dto.removed)

    def test_the_quota_spec_is_a_recurring_fifteen_minute_job(self):
        spec = quota_spec(deliver="telegram")
        self.assertEqual(spec.name, QUOTA_JOB_NAME)
        self.assertEqual(spec.schedule, "every 15m")
        self.assertEqual(spec.script, "reset-rescue-quota.py")
        self.assertIsNone(spec.repeat)


class _NullWrappers:
    def install(self):
        return {}

    def remove_owned(self):
        return []

    def states_view(self):
        return {}


if __name__ == "__main__":
    unittest.main()
