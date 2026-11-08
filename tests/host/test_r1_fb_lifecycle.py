"""R1 fix lane FB — scheduling lifecycle regressions (R6, R7, R8, R9, R12, R13, R14).

Every test drives the shipped ``HostService``/``Service`` over the **real**
``CronGateway`` and a CLI fake that only speaks the text the installed
``hermes cron`` prints. The fake keeps no ``argv`` key and nothing here pokes
the gateway's internals, so a passing test is evidence about the production
path rather than about a fake that was shaped to agree with it.

The properties pinned here:

* R6 — a scan that could not arm a redemption one-shot never reports success.
* R7 — a recorded job is not a live job: absent or terminal jobs are repaired.
* R8 — the cron listing is parsed whole, and an overflow is refused, never
  read as "no jobs".
* R9 — a crash between the primary and the backup create does not lose the
  backup that is still safely in the future.
* R12 — uninstall serialises with a live run and never erases its state.
* R13 — every owned kind is removed, and a failed removal keeps ownership.
* R14 — a same-name foreign job is never adopted (and never deleted).
"""

from __future__ import annotations

import dataclasses
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from reset_rescue.accounts import Response
from reset_rescue.cli_runner import CliResult, CliRunner
from reset_rescue.clock import Deps
from reset_rescue.codex_api import CodexResetApi
from reset_rescue.cron_gateway import CronError, CronGateway, JobSpec
from reset_rescue.fire import Fire, credit_key_for
from reset_rescue.host import HostService
from reset_rescue.planner import Planner
from reset_rescue.service import Service
from reset_rescue.store import Store
from tests.fakes import (
    SALT,
    FakeClock,
    FakeConfig,
    FakeNotifier,
    FakeSource,
    FakeWrappers,
    credit_row,
    credits_payload,
    make_settings,
    usage_payload,
)

#: A scratch root **outside the real profile**. It used to be
#: ``<HERMES_HOME>/cache/scratch/rr-fb`` — inside ``~/.hermes``, which Hermes
#: resolves back to the real root, so every temp DB landed in real state
#: (R17). ``tests.support.isolation`` refuses a root under ``$HOME``.
SCRATCH = Path(
    os.environ.get("RR_SCRATCH")
    or os.path.join(os.environ.get("RR_TEMP_ROOT") or "/var/tmp", "rr-scratch")
)
MINUTE = 60.0
HOUR = 3_600.0

SCAN = "reset-rescue-scan"
FIRE = "reset-rescue-fire.py"
BACKUP = "reset-rescue-fire-backup.py"
REMIND = "reset-rescue-remind.py"


# ------------------------------------------------------------------- cron CLI


def _parse_create(argv: list[str]) -> JobSpec | None:
    """Read the ``cron create`` argv the gateway emits (test-side only).

    The CLI fake has to understand the real grammar; the plugin no longer keeps
    an argv parser of its own (one spec path, R18), so this small reader lives
    with the fake instead of in production code.
    """
    parts = list(argv[2:])
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


class CronCli:
    """``hermes cron`` as a text interface over an in-memory job store.

    Only the three verbs the plugin uses, with the real output shapes: the
    ``Created job: <id>`` line, the ``Removed job: … (<id>)`` line and the
    two-space-indented ``<id> [state]`` listing blocks.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.jobs: list[dict] = []
        self._seq = 0
        self.fail_list = False
        self.fail_create = False
        self.fail_remove = False
        self.sticky_remove = False
        self.fail_create_scripts: set[str] = set()
        self.crash_on_create: int | None = None
        self.create_calls = 0

    # -- store -------------------------------------------------------------
    def add_foreign(self, name: str, *, state: str = "active", script: str = "foreign.py") -> str:
        self._seq += 1
        job_id = f"{self._seq:012x}"
        self.jobs.append(
            {
                "id": job_id,
                "name": name,
                "state": state,
                "enabled": state not in ("completed", "paused"),
                "schedule": "every 1h",
                "script": script,
                "deliver": "local",
                "repeat": "",
            }
        )
        return job_id

    def names(self) -> list[str]:
        return sorted(job["name"] for job in self.jobs)

    def live_ids(self) -> set[str]:
        return {
            job["id"]
            for job in self.jobs
            if job.get("enabled") is not False and job.get("state") not in ("completed", "paused")
        }

    def job(self, job_id: str) -> dict | None:
        for job in self.jobs:
            if job["id"] == job_id:
                return job
        return None

    def finish(self, name: str) -> None:
        for job in self.jobs:
            if job["name"] == name:
                job["state"] = "completed"
                job["enabled"] = False

    # -- the CLI -----------------------------------------------------------
    def listing(self) -> str:
        if not self.jobs:
            return "No scheduled jobs in profile 'custom'.\n"
        blocks = []
        for job in self.jobs:
            lines = [
                f"  {job['id']} [{job['state']}]",
                f"    Name:      {job['name']}",
                f"    Schedule:  {job['schedule']}",
            ]
            if job.get("repeat"):
                lines.append(f"    Repeat:    {job['repeat']}")
            lines.append(f"    Deliver:   {job['deliver']}")
            lines.append(f"    Script:    {job['script']}")
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks) + "\n"

    def run(self, argv, *, timeout: float = 60.0) -> CliResult:
        call = [str(part) for part in argv]
        self.calls.append(call)
        if call[:2] == ["cron", "list"]:
            if self.fail_list:
                return CliResult(argv=tuple(call), returncode=1, stdout="", stderr="boom")
            return CliResult(argv=tuple(call), returncode=0, stdout=self.listing())
        if call[:2] == ["cron", "create"]:
            self.create_calls += 1
            if self.crash_on_create is not None and self.create_calls == self.crash_on_create:
                raise SystemExit("synthetic crash during cron create")
            spec = _parse_create(call)
            if spec is None:
                return CliResult(
                    argv=tuple(call), returncode=1, stdout="Failed to create job: bad argv\n"
                )
            if self.fail_create or spec.script in self.fail_create_scripts:
                return CliResult(
                    argv=tuple(call), returncode=1, stdout="Failed to create job: nope\n"
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
                    "repeat": "0/1" if spec.repeat else "",
                }
            )
            return CliResult(argv=tuple(call), returncode=0, stdout=f"Created job: {job_id}\n")
        if call[:2] == ["cron", "remove"]:
            if self.fail_remove:
                return CliResult(
                    argv=tuple(call), returncode=1, stdout="Failed to remove job: nope\n"
                )
            job_id = call[2]
            if self.sticky_remove:
                return CliResult(
                    argv=tuple(call), returncode=0, stdout=f"Removed job: (unnamed) ({job_id})\n"
                )
            before = len(self.jobs)
            self.jobs = [job for job in self.jobs if job["id"] != job_id]
            if len(self.jobs) == before:
                return CliResult(
                    argv=tuple(call),
                    returncode=0,
                    stdout=f"Failed to remove job: '{job_id}' not found.\n",
                )
            return CliResult(
                argv=tuple(call), returncode=0, stdout=f"Removed job: (unnamed) ({job_id})\n"
            )
        return CliResult(argv=tuple(call), returncode=0, stdout="")


# -------------------------------------------------------------------- harness


class Harness:
    """Real store, real gateway, real flows; only the CLI and HTTP are faked."""

    def __init__(self, case: unittest.TestCase, **overrides) -> None:
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self._tmp = tempfile.TemporaryDirectory(dir=str(SCRATCH), prefix="fb-")
        case.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.data_dir = self.home / "plugin-data" / "reset-rescue"
        self.clock = FakeClock()
        self.source = FakeSource([], salt=SALT)
        self.api = CodexResetApi(self.source, sleep=self.clock.sleep)
        self.cron_cli = CronCli()
        self.cron = CronGateway(self.cron_cli)
        self.store = Store(self.data_dir, clock=self.clock.now)
        case.addCleanup(self.store.close)
        self.notifier = FakeNotifier()
        self.wrappers = FakeWrappers()
        self.settings = make_settings(lead_minutes=10, backup_lead_minutes=4, **overrides)
        self.deps = Deps(
            clock=self.clock,
            sleep=self.clock.sleep,
            source=self.source,
            api=self.api,
            store=self.store,
            cron=self.cron,
            notifier=self.notifier,
            wrappers=self.wrappers,
            config=FakeConfig(),
            settings=self.settings,
            salt=SALT,
            hermes_home=self.home,
        )
        self.deps.planner = Planner(self.deps, self.store)
        self.deps.fire = Fire(self.deps, self.store)

    # -- setup -------------------------------------------------------------
    def service(self) -> HostService:
        return HostService(self.deps)

    def account(self, label: str = "b***@e***.com"):
        return self.source.add_account(label)

    def credit(
        self,
        account,
        *,
        expires_in: float,
        credit_id: str = "credit-1",
        status: str = "available",
    ):
        self.source.on(
            "GET",
            "/rate-limit-reset-credits",
            Response(
                200,
                credits_payload(
                    credit_row(
                        credit_id,
                        status=status,
                        expires_at=self.clock.now() + expires_in,
                    )
                ),
            ),
        )
        self.source.on(
            "POST",
            "/rate-limit-reset-credits/consume",
            Response(200, {"code": "reset", "windows_reset": 1}),
        )
        self.source.on("GET", "/wham/usage", Response(200, usage_payload(weekly=4.0)))

    # -- running -----------------------------------------------------------
    def scan(self, **kwargs):
        return self.deps.planner.scan(**kwargs)

    def key(self, account, credit_id: str = "credit-1") -> str:
        return credit_key_for(SALT, account.account_key, credit_id)

    def slot(self, account, role: str, credit_id: str = "credit-1"):
        return self.store.get_slot(self.key(account, credit_id), role)

    def notice_kinds(self) -> list[str]:
        return [str(row["kind"]) for row in self.store.list_notices()]


# ------------------------------------------------------------------------ R6


class SchedulingFailureIsNeverSuccess(unittest.TestCase):
    """R6: a failed cron create must not be reported as protection."""

    def test_all_creation_failures_must_not_report_scheduled_success(self):
        h = Harness(self)
        account = h.account()
        h.credit(account, expires_in=HOUR)
        h.cron_cli.fail_create = True

        result = h.scan()

        self.assertNotEqual(result.exit_code, 0, "a scan with no job at all reported success")
        self.assertEqual(h.store.list_slots(credit_key=h.key(account)), [])
        self.assertTrue(
            [kind for kind in h.notice_kinds() if kind in ("retry_pending", "final_failure")],
            "no durable notice told the user the rescue is not armed",
        )

    def test_a_reminder_only_success_is_not_redemption_protection(self):
        h = Harness(self)
        account = h.account()
        h.credit(account, expires_in=30 * HOUR)
        h.cron_cli.fail_create_scripts = {FIRE, BACKUP}

        result = h.scan()

        self.assertNotEqual(result.exit_code, 0, "reminder one-shots were counted as protection")
        self.assertTrue(any("remind" in name for name in h.cron_cli.names()))
        redemption = [
            name
            for name in h.cron_cli.names()
            if name.startswith("reset-rescue-") and "remind" not in name
        ]
        self.assertEqual(redemption, [], "a redemption job exists although both creates failed")

    def test_a_backup_only_failure_is_reported_and_repaired(self):
        h = Harness(self)
        account = h.account()
        h.credit(account, expires_in=30 * HOUR)
        h.cron_cli.fail_create_scripts = {BACKUP}

        first = h.scan()

        self.assertNotEqual(first.exit_code, 0, "a credit without a backup reported success")
        self.assertIsNotNone(h.slot(account, "primary"))
        self.assertIsNone(h.slot(account, "backup"))

        h.cron_cli.fail_create_scripts = set()
        second = h.scan()

        self.assertEqual(second.exit_code, 0)
        backup = h.slot(account, "backup")
        self.assertIsNotNone(backup, "the next scan did not repair the missing backup")
        self.assertIn(str(backup["job_id"]), h.cron_cli.live_ids())


# ------------------------------------------------------------------------ R7


class RecordedIsNotLive(unittest.TestCase):
    """R7: repair joins the recorded ownership with the live cron view."""

    def test_existing_scan_record_without_live_job_must_be_recreated(self):
        h = Harness(self)
        service = h.service()
        service.ensure()
        self.assertTrue(h.cron_cli.jobs)
        h.cron_cli.jobs.clear()

        dto = service.ensure()

        self.assertTrue(h.cron_cli.jobs, "owned_jobs row is not a live scheduler job")
        self.assertIn(SCAN, h.cron_cli.names())
        self.assertTrue(dto.armed)

    def test_a_terminal_scan_job_is_replaced(self):
        h = Harness(self)
        service = h.service()
        service.ensure()
        h.cron_cli.finish(SCAN)

        dto = service.ensure()

        live = [
            job
            for job in h.cron_cli.jobs
            if job["name"] == SCAN and job["id"] in h.cron_cli.live_ids()
        ]
        self.assertEqual(len(live), 1, "a completed scan job was kept as armed")
        self.assertTrue(dto.armed)

    def test_a_terminal_credit_slot_is_rearmed(self):
        h = Harness(self)
        account = h.account()
        h.credit(account, expires_in=30 * HOUR)
        h.scan()
        primary = h.slot(account, "primary")
        h.cron_cli.finish(str(primary["job_name"]))

        h.scan()

        repaired = h.slot(account, "primary")
        self.assertIsNotNone(repaired)
        self.assertIn(
            str(repaired["job_id"]), h.cron_cli.live_ids(), "a completed one-shot stayed armed"
        )


# ------------------------------------------------------------------------ R8


class CronListingIsNotTruncated(unittest.TestCase):
    """R8: the listing is parsed whole; an overflow is refused, never absence."""

    @staticmethod
    def _listing(foreign: int) -> str:
        blocks = [
            f"  {index:08x} [scheduled]\n    Name:      foreign-{index}\n"
            f"    Schedule:  every 1h\n    Deliver:   local\n    Script:    foreign.py"
            for index in range(foreign)
        ]
        blocks.append(
            f"  abcddcba [scheduled]\n    Name:      {SCAN}\n"
            "    Schedule:  every 720m\n    Deliver:   local\n    Script:    reset-rescue-scan.py"
        )
        return "\n\n".join(blocks) + "\n"

    def _gateway(self, text: str) -> CronGateway:
        runner = CliRunner(
            launcher="/bin/true",
            runner=lambda *args: SimpleNamespace(returncode=0, stdout=text, stderr=""),
        )
        return CronGateway(runner)

    def test_real_cron_gateway_list_must_not_truncate_owned_jobs(self):
        names = [job["name"] for job in self._gateway(self._listing(60)).list_jobs()]
        self.assertIn(SCAN, names, "the plugin's job vanished behind 60 foreign jobs")

    def test_an_oversized_listing_is_refused_never_read_as_empty(self):
        text = self._listing(2_000)
        self.assertGreater(len(text), 64 * 1024)
        gateway = self._gateway(text)
        try:
            names = [job["name"] for job in gateway.list_jobs()]
        except CronError:
            return  # refused: honest, and the caller keeps its state
        self.assertIn(SCAN, names, "a truncated listing was parsed as if it were complete")


# ------------------------------------------------------------------------ R9


class PartialSchedulingCrash(unittest.TestCase):
    """R9: each slot is reconciled on its own, in every planning branch."""

    def test_crash_after_primary_creation_must_not_lose_backup(self):
        h = Harness(self)
        account = h.account()
        h.credit(account, expires_in=20 * MINUTE)
        h.cron_cli.crash_on_create = 2  # primary created, crash before the backup

        with self.assertRaises(SystemExit):
            h.scan()
        self.assertIsNotNone(h.slot(account, "primary"))
        self.assertIsNone(h.slot(account, "backup"))

        h.cron_cli.crash_on_create = None
        h.clock.advance(8 * MINUTE)  # primary is inside the 3-minute guard now
        h.scan()

        backup = h.slot(account, "backup")
        self.assertIsNotNone(backup, "the backup that was still in the future was not restored")
        self.assertIn(str(backup["job_id"]), h.cron_cli.live_ids())

    def test_a_crash_before_the_first_create_is_retried_by_the_next_scan(self):
        h = Harness(self)
        account = h.account()
        h.credit(account, expires_in=30 * HOUR)
        h.cron_cli.crash_on_create = 1

        with self.assertRaises(SystemExit):
            h.scan()
        self.assertEqual(h.store.list_slots(credit_key=h.key(account)), [])

        h.cron_cli.crash_on_create = None
        h.scan()

        self.assertIsNotNone(h.slot(account, "primary"))
        self.assertIsNotNone(h.slot(account, "backup"))


# ----------------------------------------------------------------------- R12


class UninstallIsSerialised(unittest.TestCase):
    """R12: uninstall participates in the cross-process run lock."""

    def test_uninstall_must_respect_another_store_run_lock(self):
        h = Harness(self)
        h.service().ensure()
        second = Store(h.data_dir, clock=h.clock.now, lock_wait=0)
        self.addCleanup(second.close)
        deps = dataclasses.replace(h.deps, store=second)

        with h.store.run_lock():
            dto = Service(deps).uninstall()

        self.assertTrue(dto.problems, "uninstall cleared state while another run held run.lock")
        self.assertTrue(h.store.list_credits() or h.store.list_owned_jobs(active_only=True))
        self.assertTrue(h.cron_cli.jobs, "jobs were removed while another run held the lock")

    def test_uninstall_waits_for_the_lock_and_then_succeeds(self):
        h = Harness(self)
        h.service().ensure()
        second = Store(h.data_dir, clock=h.clock.now, lock_wait=3.0)
        self.addCleanup(second.close)
        deps = dataclasses.replace(h.deps, store=second)
        release = threading.Event()

        def hold() -> None:
            with h.store.run_lock():
                release.wait(2.0)

        holder = threading.Thread(target=hold)
        holder.start()
        time.sleep(0.05)
        releaser = threading.Thread(target=lambda: (time.sleep(0.2), release.set()))
        releaser.start()

        dto = Service(deps).uninstall()

        holder.join(3.0)
        releaser.join(3.0)
        self.assertEqual(dto.problems, [])
        self.assertEqual(h.cron_cli.jobs, [])


# ----------------------------------------------------------------------- R13


class UninstallRemovesEveryOwnedKind(unittest.TestCase):
    """R13: one kind vocabulary, verified removals, retry-safe ownership."""

    def _armed(self) -> Harness:
        h = Harness(self)
        h.service().ensure()
        account = h.account()
        h.credit(account, expires_in=30 * HOUR)
        h.scan()
        return h

    def test_uninstall_must_remove_reminder_jobs(self):
        h = self._armed()
        self.assertTrue(any("remind" in name for name in h.cron_cli.names()))

        dto = h.service().uninstall()

        self.assertEqual(
            h.cron_cli.jobs, [], f"remaining jobs: {[job['name'] for job in h.cron_cli.jobs]}"
        )
        self.assertEqual(dto.problems, [])

    def test_uninstall_failure_must_preserve_ownership_for_retry(self):
        h = self._armed()
        h.cron_cli.fail_remove = True

        dto = h.service().uninstall()

        self.assertTrue(dto.problems, "failed removals are reported as success")
        self.assertTrue(h.store.list_owned_jobs(active_only=True), "ownership was forgotten")
        self.assertTrue(h.cron_cli.jobs)
        self.assertTrue(h.store.list_credits(), "state was cleared although jobs remain")
        self.assertTrue(h.wrappers.states, "wrappers were deleted under live jobs")

        h.cron_cli.fail_remove = False
        again = h.service().uninstall()

        self.assertEqual(again.problems, [])
        self.assertEqual(h.cron_cli.jobs, [])
        self.assertEqual(h.store.list_owned_jobs(active_only=True), [])

    def test_a_remove_the_cli_claims_but_did_not_do_is_a_failure(self):
        h = self._armed()
        h.cron_cli.sticky_remove = True

        dto = h.service().uninstall()

        self.assertTrue(dto.problems, "a job that is still listed was reported as removed")
        self.assertTrue(h.store.list_owned_jobs(active_only=True))


# ----------------------------------------------------------------------- R14


class ForeignSameNameJobIsNeverAdopted(unittest.TestCase):
    """R14: the announced id is verified; a same-name foreign job is untouched."""

    def test_cron_creation_ambiguity_must_not_adopt_foreign_same_name_id(self):
        cli = CronCli()
        foreign = cli.add_foreign(SCAN)
        gateway = CronGateway(cli)

        job = gateway.create(
            JobSpec(name=SCAN, schedule="every 12h", script="reset-rescue-scan.py", deliver="local")
        )

        self.assertIsNotNone(job)
        self.assertNotEqual(job.job_id, foreign, "the foreign job id was adopted as ours")
        self.assertEqual(job.job_id, cli.jobs[-1]["id"])
        self.assertIn(foreign, [row["id"] for row in cli.jobs])

    def test_an_unlisted_announced_id_is_never_replaced_by_a_foreign_job(self):
        """The racing-list fallback keeps the announced id, never a namesake."""

        class Liar(CronCli):
            def run(self, argv, *, timeout: float = 60.0) -> CliResult:
                call = [str(part) for part in argv]
                if call[:2] == ["cron", "create"]:
                    self.calls.append(call)
                    return CliResult(
                        argv=tuple(call), returncode=0, stdout="Created job: bbbbbbbb\n"
                    )
                if call[:2] == ["cron", "list"]:
                    self.calls.append(call)
                    # The store write raced the list: only the foreign job shows.
                    return CliResult(argv=tuple(call), returncode=0, stdout=self.listing())
                return super().run(argv, timeout=timeout)

        cli = Liar()
        foreign = cli.add_foreign(SCAN)
        gateway = CronGateway(cli)

        job = gateway.create(
            JobSpec(name=SCAN, schedule="every 12h", script="reset-rescue-scan.py", deliver="local")
        )

        self.assertIsNotNone(job)
        self.assertEqual(job.job_id, "bbbbbbbb")
        self.assertNotEqual(job.job_id, foreign, "a foreign namesake was adopted")

    def test_ensure_reports_a_foreign_same_name_job_instead_of_reusing_it(self):
        h = Harness(self)
        foreign = h.cron_cli.add_foreign(SCAN)

        dto = h.service().ensure()

        self.assertTrue(dto.problems, "a foreign same-name job was adopted as ours")
        self.assertIn(foreign, [row["id"] for row in h.cron_cli.jobs])
        self.assertEqual(h.store.list_owned_jobs(active_only=True), [])


if __name__ == "__main__":
    unittest.main()
