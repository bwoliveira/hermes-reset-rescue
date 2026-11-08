"""T-13 — owned cron jobs: parse the real CLI, create/verify, never touch a foreign job.

The fixtures below are the **real** output shapes of
``hermes cron list --all`` (installed v0.23.0+209, spike S4): a two-space
indented ``<id> [state]`` header, four-space ``Label:  value`` rows, a
``[paused]``/``[completed]`` badge, and a footer warning that is not a job.
"""

from __future__ import annotations

import unittest

from reset_rescue import cron_gateway, keys
from reset_rescue.cli_runner import CliResult
from reset_rescue.cron_gateway import (
    CronError,
    CronGateway,
    JobSpec,
    build_scan_spec,
    build_slot_spec,
    parse_job_list,
    quota_spec,
    slot_job_name,
)
from tests.fakes import parse_create_argv

# The footer `hermes cron list` prints when no gateway serves the profile. It is
# verbatim CLI output, so it is one long line by nature; split here only so the
# module stays under the 100-column lint bar.
SCHEDULER_WARNING = (
    "  ⚠  Scheduler is not ready for profile 'custom': no gateway serves it and no fresh "
    "ticker heartbeat.\n"
)

LIST_TWO_JOBS = (
    """\
┌─────────────────────────────────────────────────────────────────────────┐
│                         Scheduled Jobs (profile: custom)                │
└─────────────────────────────────────────────────────────────────────────┘

  fb4915604b5e [paused]
    Name:      reset-rescue-scan
    Schedule:  every 720m
    Repeat:    ∞
    Next run:  None
    Deliver:   local
    Script:    reset-rescue-scan.py
    Mode:      no-agent (script stdout delivered directly)

  32093bd0cd15 [active]
    Name:      reset-rescue-primary-01234567
    Schedule:  once at 2026-11-07 05:49
    Repeat:    0/1
    Next run:  2026-11-07T05:49:19-03:00
    Deliver:   telegram
    Script:    reset-rescue-fire.py
    Mode:      no-agent (script stdout delivered directly)

"""
    + SCHEDULER_WARNING
    + """\
     If no gateway is running: hermes gateway install
     Check status:  hermes cron status
"""
)

LIST_SCAN_ACTIVE = """\
  fb4915604b5e [active]
    Name:      reset-rescue-scan
    Schedule:  every 720m
    Repeat:    ∞
    Next run:  2026-11-07T17:49:19-03:00
    Deliver:   local
    Script:    reset-rescue-scan.py
"""

LIST_COMPLETED = """\
  32093bd0cd15 [completed]
    Name:      reset-rescue-primary-01234567
    Schedule:  once at 2026-11-07 05:49
    Repeat:    1/1
    Next run:  None
    Deliver:   local
    Script:    reset-rescue-fire.py
    Last run:  2026-11-07T05:49:19-03:00  ok
"""

KEY = "0123456789abcdef"


class _FakeCli:
    """A ``CliRunner`` stand-in that behaves like the real cron store.

    ``jobs`` is the live registry: ``create`` adds the job and ``remove`` drops
    it, so a test can verify that the gateway's "list is the fact" check works
    against a store that actually changes.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.jobs: list[dict] = []
        self.create_output = "Created job: abcd1234ef56\n  Name: reset-rescue-scan\n"
        self.remove_output = "Removed job: reset-rescue-scan (abcd1234ef56)\n"
        self.fail_create = False
        self.fail_list = False
        self.fail_remove = False
        self.sticky_remove = False  # CLI claims success, the job stays listed
        self._seq = 0

    def seed(self, text: str) -> "_FakeCli":
        """Load a ``cron list`` fixture into the registry."""
        self.jobs = [job.as_dict() for job in parse_job_list(text)]
        return self

    def listing(self) -> str:
        """Render the registry in the real CLI's format."""
        if not self.jobs:
            return "No scheduled jobs in profile 'custom'.\n"
        blocks = []
        for job in self.jobs:
            state = job.get("state") or ("active" if job.get("enabled", True) else "paused")
            lines = [f"  {job['id']} [{state}]", f"    Name:      {job.get('name', '')}"]
            if job.get("schedule"):
                lines.append(f"    Schedule:  {job['schedule']}")
            if job.get("repeat"):
                lines.append(f"    Repeat:    {job['repeat']}")
            lines.append(f"    Deliver:   {job.get('deliver') or 'local'}")
            if job.get("script"):
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
            if self.fail_create:
                return CliResult(
                    argv=tuple(call), returncode=1, stdout="Failed to create job: nope\n"
                )
            spec = parse_create_argv(call)
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
            return CliResult(
                argv=tuple(call),
                returncode=0,
                stdout=f"Created job: {job_id}\n  Name: {spec.name}\n",
            )
        if call[:2] == ["cron", "remove"]:
            if self.fail_remove:
                return CliResult(
                    argv=tuple(call), returncode=1, stdout="Failed to remove job: not found\n"
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
                    stdout=f"Failed to remove job: Job with ID or name '{job_id}' not found.\n",
                )
            return CliResult(
                argv=tuple(call), returncode=0, stdout=f"Removed job: (unnamed) ({job_id})\n"
            )
        return CliResult(argv=tuple(call), returncode=0, stdout="")

    def count(self, *prefix: str) -> int:
        return sum(1 for call in self.calls if tuple(call[: len(prefix)]) == prefix)


class ParseTests(unittest.TestCase):
    def test_parses_the_real_two_job_listing(self):
        jobs = parse_job_list(LIST_TWO_JOBS)
        self.assertEqual(len(jobs), 2)
        scan, one_shot = jobs
        self.assertEqual(scan.job_id, "fb4915604b5e")
        self.assertEqual(scan.name, "reset-rescue-scan")
        self.assertEqual(scan.state, "paused")
        self.assertFalse(scan.enabled)
        self.assertEqual(scan.schedule, "every 720m")
        self.assertEqual(scan.repeat, "∞")
        self.assertEqual(scan.script, "reset-rescue-scan.py")
        self.assertEqual(scan.deliver, "local")
        self.assertTrue(one_shot.enabled)
        self.assertEqual(one_shot.state, "active")
        self.assertEqual(one_shot.repeat, "0/1")
        self.assertEqual(one_shot.repeat_completed(), 0)

    def test_footer_warning_is_not_a_job(self):
        jobs = parse_job_list(LIST_TWO_JOBS)
        self.assertFalse(any("Scheduler" in job.name for job in jobs))

    def test_completed_one_shot_is_terminal(self):
        (job,) = parse_job_list(LIST_COMPLETED)
        self.assertTrue(job.terminal)
        self.assertFalse(job.live)
        self.assertEqual(job.repeat_completed(), 1)

    def test_ansi_is_stripped(self):
        # Built from chr(27) rather than a literal escape: the same bytes, but
        # the tree scanner does not read a colour code as an encoded blob.
        esc = chr(27)
        colored = (
            f"{esc}[33m  fb4915604b5e{esc}[0m {esc}[33m[paused]{esc}[0m\n"
            f"    {esc}[33mName:{esc}[0m      reset-rescue-scan\n"
        )
        (job,) = parse_job_list(colored)
        self.assertEqual(job.job_id, "fb4915604b5e")
        self.assertEqual(job.name, "reset-rescue-scan")

    def test_empty_output_is_no_jobs(self):
        self.assertEqual(parse_job_list("No scheduled jobs in profile 'custom'.\n"), [])

    def test_unknown_rows_are_kept_in_extra(self):
        text = (
            "  32093bd0cd15 [active]\n"
            "    Name:      reset-rescue-scan\n"
            "    Future:    something new\n"
        )
        (job,) = parse_job_list(text)
        self.assertEqual(job.extra.get("Future"), "something new")


class SpecTests(unittest.TestCase):
    def test_scan_spec_argv_matches_the_documented_contract(self):
        spec = build_scan_spec(interval_hours=12, deliver="telegram")
        self.assertEqual(
            spec.argv(),
            [
                "cron",
                "create",
                "every 12h",
                "--name",
                "reset-rescue-scan",
                "--script",
                "reset-rescue-scan.py",
                "--no-agent",
                "--deliver",
                "telegram",
                "--failure-deliver",
                "telegram",
            ],
        )

    def test_slot_spec_is_a_one_shot_with_the_role_script(self):
        spec = build_slot_spec(
            role="backup", key=KEY, fire_at_iso="2026-11-09T21:30:00-03:00", deliver="telegram"
        )
        self.assertEqual(spec.name, "reset-rescue-01234567-b")
        self.assertEqual(spec.script, "reset-rescue-fire-backup.py")
        self.assertEqual(spec.repeat, 1)
        argv = spec.argv()
        self.assertIn("--repeat", argv)
        self.assertIn("1", argv)
        self.assertNotIn("--grace-minutes", argv)  # rejected for one-shots

    def test_the_primary_slot_keeps_the_short_name(self):
        spec = build_slot_spec(
            role="primary", key=KEY, fire_at_iso="2026-11-09T21:30:00-03:00", deliver="local"
        )
        self.assertEqual(spec.name, "reset-rescue-01234567")
        self.assertEqual(spec.script, "reset-rescue-fire.py")

    def test_the_two_reminders_have_their_own_names_and_one_wrapper(self):
        """R13: the slot roles are the job kinds, so uninstall can match them."""
        first = build_slot_spec(
            role="reminder1", key=KEY, fire_at_iso="2026-11-09T21:30:00-03:00", deliver="local"
        )
        second = build_slot_spec(
            role="reminder2", key=KEY, fire_at_iso="2026-11-09T21:30:00-03:00", deliver="local"
        )
        self.assertNotEqual(first.name, second.name)
        self.assertEqual(first.name, "reset-rescue-remind-1-01234567")
        self.assertEqual(second.name, "reset-rescue-remind-2-01234567")
        self.assertEqual(first.script, "reset-rescue-remind.py")
        self.assertEqual(second.script, "reset-rescue-remind.py")

    def test_slot_job_name_is_the_one_naming_rule(self):
        roles = ("primary", "backup", "reminder1", "reminder2")
        names = {slot_job_name(role, KEY) for role in roles}
        self.assertEqual(len(names), 4)
        self.assertIn("reset-rescue-01234567", names)

    def test_the_quota_spec_is_a_recurring_fifteen_minute_job(self):
        spec = quota_spec(deliver="telegram")
        self.assertEqual(spec.name, "reset-rescue-quota")
        self.assertEqual(spec.schedule, "every 15m")
        self.assertEqual(spec.script, "reset-rescue-quota.py")
        self.assertIsNone(spec.repeat)

    def test_unknown_role_is_refused(self):
        for role in ("scan", "quota", "nonsense"):
            with self.assertRaises(CronError):
                build_slot_spec(
                    role=role, key=KEY, fire_at_iso="2026-11-09T21:30:00-03:00", deliver="local"
                )


class CreateTests(unittest.TestCase):
    def test_create_returns_the_id_the_cli_announced(self):
        """R14: the announced id is the identity; a namesake is never adopted."""
        cli = _FakeCli()
        gateway = CronGateway(cli)
        spec = build_scan_spec(interval_hours=12, deliver="local")
        cli.seed(LIST_TWO_JOBS)
        job = gateway.create(spec)
        self.assertIsNotNone(job)
        self.assertEqual(job.job_id, "000000000001")
        self.assertEqual(job.name, spec.name)
        self.assertEqual(cli.count("cron", "create"), 1)
        self.assertEqual(cli.count("cron", "list"), 1)

    def test_create_falls_back_to_the_announced_id_when_the_list_races(self):
        cli = _FakeCli()
        gateway = CronGateway(cli)
        # The store write races the list: `create` announces an id but the
        # listing is still empty on the same call.
        cli.listing = lambda: "No scheduled jobs in profile 'custom'.\n"  # type: ignore[method-assign]
        cli.jobs = []
        job = gateway.create(build_scan_spec(interval_hours=12, deliver="local"))
        self.assertIsNotNone(job)
        self.assertEqual(len(job.job_id), 12)

    def test_create_failure_raises_a_fixed_code(self):
        cli = _FakeCli()
        cli.fail_create = True
        gateway = CronGateway(cli)
        with self.assertRaises(CronError) as caught:
            gateway.create(build_scan_spec(interval_hours=12, deliver="local"))
        self.assertEqual(caught.exception.code, "cron_create_failed")

    def test_a_foreign_name_is_refused_before_any_call(self):
        cli = _FakeCli()
        gateway = CronGateway(cli)
        spec = JobSpec(name="my-own-job", schedule="every 1h", script="x.py", deliver="local")
        with self.assertRaises(CronError):
            gateway.create(spec)
        self.assertEqual(cli.calls, [])

    def test_the_listing_is_never_parsed_when_it_was_truncated(self):
        """R8: a cut listing is a failure, not "no jobs"."""
        cli = _FakeCli()
        gateway = CronGateway(cli)
        cli.listing = lambda: (  # type: ignore[method-assign]
            "  32093bd0cd15 [active]\n    Name:      reset-rescue-scan\n"
            "…[truncated 4000 of 90000 chars]"
        )
        with self.assertRaises(CronError) as caught:
            gateway.jobs()
        self.assertEqual(caught.exception.code, "cron_list_truncated")


class RemoveTests(unittest.TestCase):
    def test_remove_refuses_an_id_that_is_not_owned(self):
        cli = _FakeCli()
        gateway = CronGateway(cli)
        self.assertFalse(gateway.remove("fb4915604b5e", owned={"other-id"}))
        self.assertEqual(cli.calls, [])

    def test_remove_verifies_by_listing(self):
        cli = _FakeCli()
        gateway = CronGateway(cli)
        cli.seed(LIST_TWO_JOBS.replace("fb4915604b5e", "ffffffffffff"))
        self.assertTrue(gateway.remove("fb4915604b5e", owned={"fb4915604b5e"}))
        self.assertEqual(cli.count("cron", "remove"), 1)

    def test_remove_reports_false_when_the_job_survives(self):
        cli = _FakeCli()
        gateway = CronGateway(cli)
        cli.seed(LIST_TWO_JOBS)
        cli.sticky_remove = True  # the CLI says "Removed", the list still shows it
        self.assertFalse(gateway.remove("fb4915604b5e", owned={"fb4915604b5e"}))

    def test_remove_refuses_an_unrecorded_id_even_when_the_name_is_ours(self):
        """I10/R14: a same-name job that is not in ``owned`` is never deleted."""
        cli = _FakeCli()
        gateway = CronGateway(cli)
        cli.seed(LIST_TWO_JOBS)
        self.assertFalse(gateway.remove("fb4915604b5e", owned=set()))
        self.assertEqual(cli.count("cron", "remove"), 0)
        self.assertEqual(len(cli.jobs), 2)

    def test_remove_of_an_absent_id_succeeds_because_the_goal_is_met(self):
        cli = _FakeCli()
        gateway = CronGateway(cli)
        self.assertTrue(gateway.remove("32093bd0cd15", owned={"32093bd0cd15"}))


class EnsureTests(unittest.TestCase):
    def test_ensure_creates_when_absent(self):
        cli = _FakeCli()
        gateway = CronGateway(cli)
        action, job = gateway.ensure(
            build_scan_spec(interval_hours=12, deliver="local"), owned=set()
        )
        self.assertEqual(action, "created")

    def test_ensure_keeps_a_matching_job(self):
        cli = _FakeCli()
        cli.seed(LIST_SCAN_ACTIVE)
        gateway = CronGateway(cli)
        action, job = gateway.ensure(
            build_scan_spec(interval_hours=12, deliver="local"), owned={"fb4915604b5e"}
        )
        self.assertEqual(action, "unchanged")
        self.assertEqual(cli.count("cron", "create"), 0)

    def test_ensure_never_touches_a_foreign_job_with_our_name(self):
        cli = _FakeCli()
        cli.seed(LIST_TWO_JOBS)
        gateway = CronGateway(cli)
        action, job = gateway.ensure(
            build_scan_spec(interval_hours=12, deliver="local"), owned=set()
        )
        self.assertEqual(action, "foreign")
        self.assertEqual(cli.count("cron", "create"), 0)
        self.assertEqual(cli.count("cron", "remove"), 0)

    def test_ensure_recreates_on_deliver_drift(self):
        cli = _FakeCli()
        cli.seed(LIST_TWO_JOBS)
        gateway = CronGateway(cli)
        spec = build_scan_spec(interval_hours=12, deliver="telegram")
        action, job = gateway.ensure(spec, owned={"fb4915604b5e"})
        self.assertEqual(action, "recreated")
        self.assertEqual(cli.count("cron", "remove"), 1)
        self.assertEqual(cli.count("cron", "create"), 1)

    def test_ensure_recreates_on_interval_drift(self):
        cli = _FakeCli()
        cli.seed(LIST_TWO_JOBS)
        gateway = CronGateway(cli)
        spec = build_scan_spec(interval_hours=6, deliver="local")
        action, _ = gateway.ensure(spec, owned={"fb4915604b5e"})
        self.assertEqual(action, "recreated")

    def test_ensure_does_not_recreate_a_one_shot_that_is_too_close(self):
        cli = _FakeCli()
        gateway = CronGateway(cli)
        spec = build_slot_spec(
            role="primary", key=KEY, fire_at_iso="2026-11-09T21:30:00-03:00", deliver="local"
        )
        now = 1794000000.0  # ~2026-11-06; the slot is 3 days out -> fine
        action, _ = gateway.ensure(spec, owned=set(), now=now)
        self.assertEqual(action, "created")
        # Now pretend the slot is 30 s away: never create it.
        cli2 = _FakeCli()
        gateway2 = CronGateway(cli2)
        import datetime

        fire_at = datetime.datetime.fromisoformat("2026-11-09T21:30:00-03:00").timestamp()
        action2, job2 = gateway2.ensure(spec, owned=set(), now=fire_at - 30)
        self.assertEqual(action2, "too_late")
        self.assertIsNone(job2)
        self.assertEqual(cli2.count("cron", "create"), 0)

    def test_ensure_removes_an_owned_one_shot_that_is_too_late(self):
        cli = _FakeCli()
        cli.seed(
            "  32093bd0cd15 [active]\n    Name:      reset-rescue-01234567\n"
            "    Schedule:  once at 2026-11-07 05:49\n    Deliver:   telegram\n"
            "    Script:    reset-rescue-fire.py\n"
        )
        gateway = CronGateway(cli)
        spec = build_slot_spec(
            role="primary", key=KEY, fire_at_iso="2026-11-07T05:49:19-03:00", deliver="telegram"
        )
        import datetime

        fire_at = datetime.datetime.fromisoformat("2026-11-07T05:49:19-03:00").timestamp()
        action, _ = gateway.ensure(spec, owned={"32093bd0cd15"}, now=fire_at - 10)
        self.assertEqual(action, "too_late")
        self.assertEqual(cli.count("cron", "remove"), 1)


class ScheduleMatchTests(unittest.TestCase):
    def test_interval_hours_match_the_rendered_minutes(self):
        self.assertTrue(cron_gateway._schedule_matches("every 720m", "every 12h"))
        self.assertTrue(cron_gateway._schedule_matches("every 12h", "every 12h"))
        self.assertFalse(cron_gateway._schedule_matches("every 360m", "every 12h"))

    def test_one_shot_iso_matches_the_rendered_once_at(self):
        self.assertTrue(
            cron_gateway._schedule_matches("once at 2026-11-09 21:30", "2026-11-09T21:30:00-03:00")
        )

    def test_a_different_kind_does_not_match(self):
        self.assertFalse(cron_gateway._schedule_matches("every 720m", "2026-11-09T21:30:00-03:00"))


class ListFailureTests(unittest.TestCase):
    def test_a_failed_list_raises_a_fixed_code(self):
        cli = _FakeCli()
        cli.fail_list = True
        gateway = CronGateway(cli)
        with self.assertRaises(CronError) as caught:
            gateway.jobs()
        self.assertEqual(caught.exception.code, "cron_list_failed")

    def test_completed_owned_returns_nothing_when_the_list_fails(self):
        cli = _FakeCli()
        cli.fail_list = True
        gateway = CronGateway(cli)
        self.assertEqual(gateway.completed_owned(owned={"x"}), [])


class OwnershipNameTests(unittest.TestCase):
    def test_prefix_is_the_plugin_namespace(self):
        gateway = CronGateway(_FakeCli())
        self.assertTrue(gateway.owns_name("reset-rescue-scan"))
        self.assertTrue(gateway.owns_name("reset-rescue-primary-01234567"))
        self.assertFalse(gateway.owns_name("reset-rescue"))  # bare prefix is not a job
        self.assertFalse(gateway.owns_name("my-reset-rescue-scan"))
        self.assertFalse(gateway.owns_name(""))

    def test_job_names_are_unique_per_role_and_key(self):
        names = {
            keys.job_name("primary", KEY),
            keys.job_name("backup", KEY),
            keys.job_name("remind", KEY),
        }
        self.assertEqual(len(names), 3)


if __name__ == "__main__":
    unittest.main()
