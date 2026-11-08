"""T-21 / T-13 wiring — the service layer driving the real host collaborators.

The other host tests exercise each module alone with a fake. This one wires
L1's ``Service`` to the **real** ``CronGateway``, ``wrappers`` and ``Notifier``
over a fake CLI, which is the pairing that ships: it is where a naming,
argv-shape or ownership mismatch between lanes would show up.

Nothing here touches the network, the real profile or a real subprocess.
"""

from __future__ import annotations

import contextlib
import json
import tempfile
import unittest
from pathlib import Path

from reset_rescue import wrappers
from reset_rescue.clock import Deps
from reset_rescue.cron_gateway import SCAN_JOB_NAME, CronGateway
from reset_rescue.notify import Notifier
from reset_rescue.service import Service
from reset_rescue.settings import Settings
from tests.fakes import parse_create_argv

LIST_EMPTY = "No scheduled jobs in profile 'custom'.\n"


class _CronCli:
    """A ``CliRunner`` that behaves like the real cron store (create/list/remove)."""

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
                    "repeat": "0/1" if spec.repeat else "",
                }
            )
            return CliResult(argv=tuple(call), returncode=0, stdout=f"Created job: {job_id}\n")
        if call[:2] == ["cron", "remove"]:
            job_id = call[2]
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
        if call[:1] == ["send"]:
            # `hermes send --json` prints the send_message_tool result object
            # (hermes_cli/send_cmd.py:_emit_result): `success`/`skipped`/`error`
            # plus an optional `note`. Empty stdout is not a shape the real CLI
            # can produce, so the fake must not produce it either.
            return CliResult(
                argv=tuple(call),
                returncode=0,
                stdout=json.dumps({"success": True, "message_id": 1, "note": "sent"}),
            )
        return CliResult(argv=tuple(call), returncode=0, stdout="")

    def _listing(self) -> str:
        if not self.jobs:
            return LIST_EMPTY
        blocks = []
        for job in self.jobs:
            lines = [
                f"  {job['id']} [{job.get('state', 'active')}]",
                f"    Name:      {job['name']}",
                f"    Schedule:  {job['schedule']}",
                f"    Deliver:   {job['deliver']}",
                f"    Script:    {job['script']}",
            ]
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks) + "\n"

    def count(self, *prefix: str) -> int:
        return sum(1 for call in self.calls if tuple(call[: len(prefix)]) == prefix)


class _Store:
    """Just enough store for ``ensure``/``uninstall``."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.cleared = False
        self.lock_depth = 0

    def record_job(self, job_id: str, job_name: str, kind: str, script: str = "") -> None:
        self.jobs[job_id] = {
            "job_id": job_id,
            "job_name": job_name,
            "kind": kind,
            "script": script,
            "removed_at": None,
        }

    def mark_job_removed(self, job_id: str, now: float | None = None) -> None:
        row = self.jobs.get(job_id)
        if row is not None:
            row["removed_at"] = 1.0 if now is None else float(now)

    def list_owned_jobs(self, *, active_only: bool = False, kind: str | None = None) -> list[dict]:
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

    @contextlib.contextmanager
    def run_lock(self, wait: float | None = None, poll: float | None = None):
        self.lock_depth += 1
        try:
            yield
        finally:
            self.lock_depth -= 1

    def meta_get(self, key: str):
        return None

    def meta_set(self, key: str, value: str) -> None:
        return None

    def history_add(self, *_args, **_kwargs) -> None:
        return None

    def history_tail(self, limit: int = 50):
        return []

    def account_list(self) -> list[dict]:
        return []

    def credit_list(self, account_key: str | None = None) -> list[dict]:
        return []

    def clear_all(self) -> None:
        self.cleared = True
        self.jobs.clear()


class _Wrappers:
    """The real wrapper module, bound to one temp home."""

    def __init__(self, home: Path, launcher: str) -> None:
        self.home = home
        self.launcher = launcher

    def install(self):
        return wrappers.install_wrappers(self.home, self.launcher)

    def remove_owned(self):
        return wrappers.remove_owned_wrappers(self.home)

    def states_view(self):
        return {status.name: status.state for status in wrappers.verify_wrappers(self.home)}


class _Config:
    def __init__(self) -> None:
        self.values: dict = {}

    def set_setting(self, key, value):
        self.values[key] = value


def _service(home: Path, *, settings: Settings | None = None) -> tuple[Service, _CronCli, _Store]:
    cli = _CronCli()
    store = _Store()
    deps = Deps(
        cli=cli,
        cron=CronGateway(cli),
        store=store,
        wrappers=_Wrappers(home, "/usr/local/bin/hermes"),
        notifier=Notifier(cli, target="local"),
        config=_Config(),
        settings=settings or Settings(),
    )
    return Service(deps), cli, store


class EnsureWiringTests(unittest.TestCase):
    def test_ensure_creates_the_scan_job_with_an_installed_script(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            service, cli, store = _service(home)
            dto = service.ensure()
            self.assertEqual(dto.problems, [])
            self.assertTrue(dto.armed)
            self.assertIn(SCAN_JOB_NAME, dto.created)
            # The job must point at a file the plugin actually installed.
            self.assertTrue((home / "scripts" / "reset-rescue-scan.py").is_file())
            created = [call for call in cli.calls if call[:2] == ["cron", "create"]]
            self.assertEqual(len(created), 1)
            self.assertIn("reset-rescue-scan.py", created[0])
            self.assertEqual(len(store.jobs), 1)

    def test_a_second_ensure_creates_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            service, cli, store = _service(Path(directory))
            service.ensure()
            dto = service.ensure()
            self.assertEqual(dto.created, [])
            self.assertTrue(dto.armed)
            self.assertEqual(cli.count("cron", "create"), 1)
            self.assertEqual(len(store.jobs), 1)

    def test_automatic_rescue_off_removes_the_owned_job(self):
        with tempfile.TemporaryDirectory() as directory:
            service, cli, store = _service(Path(directory))
            service.ensure()
            self.assertEqual(len(store.jobs), 1)
            service.deps.settings = Settings(automatic_rescue=False)
            dto = service.ensure()
            self.assertFalse(dto.armed)
            self.assertEqual(store.list_owned_jobs(active_only=True), [])
            self.assertEqual(cli.count("cron", "remove"), 1)

    def test_uninstall_removes_jobs_and_our_wrappers_only(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            service, cli, store = _service(home)
            service.ensure()
            foreign = home / "scripts" / "my-own.py"
            foreign.write_text("print('mine')\n", encoding="utf-8")
            dto = service.uninstall()
            self.assertEqual(dto.problems, [])
            self.assertEqual(store.jobs, {})
            self.assertTrue(foreign.is_file(), "a foreign file must survive uninstall")
            self.assertFalse((home / "scripts" / "reset-rescue-scan.py").exists())

    def test_uninstall_keeps_data_when_asked(self):
        with tempfile.TemporaryDirectory() as directory:
            service, cli, store = _service(Path(directory))
            service.ensure()
            service.uninstall(keep_data=True)
            self.assertFalse(store.cleared)

    def test_uninstall_clears_data_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            service, cli, store = _service(Path(directory))
            service.ensure()
            service.uninstall()
            self.assertTrue(store.cleared)

    def test_a_foreign_job_with_our_name_is_never_removed(self):
        """I10: only recorded ids are removable, even with the plugin's name."""
        with tempfile.TemporaryDirectory() as directory:
            service, cli, store = _service(Path(directory))
            cli.jobs.append(
                {
                    "id": "ffffffffffff",
                    "name": SCAN_JOB_NAME,
                    "state": "active",
                    "enabled": True,
                    "schedule": "every 720m",
                    "script": "reset-rescue-scan.py",
                    "deliver": "local",
                }
            )
            dto = service.uninstall()
            self.assertEqual(dto.removed, [])
            self.assertEqual(cli.count("cron", "remove"), 0)
            self.assertEqual(len(cli.jobs), 1)

    def test_status_reports_the_wrappers_it_installed(self):
        with tempfile.TemporaryDirectory() as directory:
            service, cli, store = _service(Path(directory))
            service.ensure()
            status = service.status()
            states = {wrapper.name: wrapper.state for wrapper in status.schedule.wrappers}
            self.assertEqual(states.get("reset-rescue-scan.py"), "ok")
            self.assertTrue(status.schedule.armed)


class NoticeWiringTests(unittest.TestCase):
    def test_a_local_target_is_skipped_not_failed(self):
        with tempfile.TemporaryDirectory():
            cli = _CronCli()
            notifier = Notifier(cli, target="local")
            result = notifier.notify(
                "success",
                {
                    "label": "b***@gmail.com",
                    "redeemed_at": 1_800_000_000.0,
                    "expires_at": 1_800_000_600.0,
                },
            )
            self.assertTrue(result.skipped)
            self.assertFalse(result.delivered)
            self.assertEqual(cli.count("send"), 0)

    def test_a_platform_target_sends_through_hermes(self):
        """The one call the plugin makes to deliver: `hermes send -t <target>`."""
        with tempfile.TemporaryDirectory():
            cli = _CronCli()
            notifier = Notifier(cli, target="telegram", language="pt-BR")
            result = notifier.notify(
                "success",
                {
                    "label": "b***@gmail.com",
                    "redeemed_at": 1_800_000_000.0,
                    "expires_at": 1_800_000_600.0,
                },
            )
            self.assertTrue(result.delivered)
            self.assertEqual(cli.calls[0][:4], ["send", "-t", "telegram", "--json"])
            self.assertIn("Salvo!", cli.calls[0][4])


if __name__ == "__main__":
    unittest.main()
