"""T-12 — the subprocess runner: argv only, no bypass, bounded, pinned home.

Every assertion here is a property the plugin depends on at fire time, so the
tests use a fake runner (no process is ever started) and check the exact argv
and environment the real runner would hand to ``subprocess.run``.
"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path

from reset_rescue import cli_runner
from reset_rescue.cli_runner import (
    CliRunner,
    ForbiddenArgv,
    resolve_launcher,
    scrub_env,
    truncate,
)


class _Completed:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class RecordingRunner:
    """Stands in for ``subprocess.run``: records argv/env/timeout, returns scripted output."""

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        raises: BaseException | None = None,
    ) -> None:
        self.calls: list[dict] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.raises = raises

    def __call__(self, argv, env, timeout):
        self.calls.append({"argv": list(argv), "env": dict(env), "timeout": timeout})
        if self.raises is not None:
            raise self.raises
        return _Completed(self.returncode, self.stdout, self.stderr)


def _runner(**kwargs) -> tuple[CliRunner, RecordingRunner]:
    fake = RecordingRunner(**kwargs)
    runner = CliRunner(
        launcher="/usr/local/bin/hermes",
        hermes_home="/tmp/rr-home",
        env={
            "PATH": "/usr/bin",
            "HERMES_ACCEPT_HOOKS": "1",
            "HERMES_YOLO_MODE": "1",
            "KEEP": "yes",
        },
        runner=fake,
    )
    return runner, fake


class ArgvTests(unittest.TestCase):
    def test_argv_is_passed_verbatim_after_the_launcher(self):
        runner, fake = _runner()
        runner.run(["cron", "list", "--all"])
        self.assertEqual(fake.calls[0]["argv"], ["/usr/local/bin/hermes", "cron", "list", "--all"])

    def test_a_command_string_is_refused(self):
        runner, fake = _runner()
        with self.assertRaises(TypeError):
            runner.run("cron list")
        self.assertEqual(fake.calls, [])

    def test_empty_argv_is_refused(self):
        runner, _ = _runner()
        with self.assertRaises(ValueError):
            runner.run([])

    def test_approval_bypass_flags_are_refused_before_running(self):
        for flag in cli_runner.FORBIDDEN_FLAGS:
            runner, fake = _runner()
            with self.assertRaises(ForbiddenArgv, msg=flag):
                runner.run(["cron", "create", flag])
            self.assertEqual(fake.calls, [], flag)

    def test_no_helper_ever_adds_a_bypass_flag(self):
        runner, fake = _runner()
        runner.cron("list")
        runner.send("-t", "telegram", "--json", "hi")
        runner.auth_refresh("openai-codex", "abc123")
        for call in fake.calls:
            self.assertEqual(cli_runner.iter_forbidden(call["argv"]), [])


class EnvTests(unittest.TestCase):
    def test_home_is_pinned_and_bypasses_are_dropped(self):
        runner, fake = _runner()
        runner.run(["cron", "list"])
        env = fake.calls[0]["env"]
        self.assertEqual(env["HERMES_HOME"], "/tmp/rr-home")
        self.assertNotIn("HERMES_ACCEPT_HOOKS", env)
        self.assertNotIn("HERMES_YOLO_MODE", env)
        self.assertEqual(env["KEEP"], "yes")
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_scrub_env_never_invents_a_variable(self):
        env = scrub_env({"A": "1"}, hermes_home="/h")
        self.assertEqual(env, {"A": "1", "HERMES_HOME": "/h"})

    def test_no_secret_is_added_by_the_runner(self):
        runner, fake = _runner()
        runner.send("-t", "telegram", "--json", "hello")
        env = fake.calls[0]["env"]
        for name in env:
            self.assertNotIn("TOKEN", name.upper())
            self.assertNotIn("SECRET", name.upper())


class TimeoutTests(unittest.TestCase):
    def test_default_timeouts_per_surface(self):
        runner, fake = _runner()
        runner.cron("list")
        runner.send("-t", "telegram", "--json", "x")
        runner.auth_refresh("openai-codex", "id")
        timeouts = [call["timeout"] for call in fake.calls]
        self.assertEqual(
            timeouts,
            [cli_runner.CRON_TIMEOUT_S, cli_runner.SEND_TIMEOUT_S, cli_runner.AUTH_TIMEOUT_S],
        )

    def test_a_timeout_is_reported_not_raised(self):
        runner, _ = _runner(raises=subprocess.TimeoutExpired(cmd=["hermes"], timeout=1))
        result = runner.run(["cron", "list"])
        self.assertTrue(result.timed_out)
        self.assertFalse(result.ok)

    def test_an_oserror_becomes_a_fixed_code(self):
        runner, _ = _runner(raises=OSError("permission denied: /secret/path"))
        result = runner.run(["cron", "list"])
        self.assertEqual(result.exec_error, "spawn_failed")
        self.assertNotIn("secret", repr(result))

    def test_a_nonzero_exit_keeps_its_output(self):
        runner, _ = _runner(returncode=1, stdout="Failed to create job: nope", stderr="")
        result = runner.run(["cron", "create", "x"])
        self.assertEqual(result.returncode, 1)
        self.assertFalse(result.ok)
        self.assertIn("Failed to create job", result.stdout)


class LauncherTests(unittest.TestCase):
    def test_missing_launcher_is_a_clean_failure(self):
        fake = RecordingRunner()
        runner = CliRunner(launcher="/nonexistent/hermes", recorded_launcher=None, runner=fake)
        # `shutil.which` may find the real launcher on this host; force the miss.
        import unittest.mock as mock

        with mock.patch.object(cli_runner.shutil, "which", return_value=None):
            result = runner.run(["cron", "list"])
        self.assertEqual(result.exec_error, "launcher_not_found")
        self.assertEqual(fake.calls, [])

    def test_explicit_launcher_wins(self):
        fake = RecordingRunner()
        runner = CliRunner(launcher="/usr/local/bin/hermes", runner=fake)
        runner.run(["cron", "list"])
        self.assertEqual(fake.calls[0]["argv"][0], "/usr/local/bin/hermes")

    def test_recorded_launcher_is_the_last_resort(self, tmp_path: Path | None = None):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "hermes"
            script.write_text("#!/bin/sh\n", encoding="utf-8")
            os.chmod(script, 0o755)
            import unittest.mock as mock

            with mock.patch.object(cli_runner.shutil, "which", return_value=None):
                self.assertEqual(resolve_launcher(recorded=str(script)), str(script))

    def test_resolution_is_cached_until_refreshed(self):
        import unittest.mock as mock

        runner = CliRunner(launcher=None, recorded_launcher=None)
        with mock.patch.object(cli_runner.shutil, "which", return_value="/bin/hermes") as which:
            self.assertEqual(runner.launcher(), "/bin/hermes")
            self.assertEqual(runner.launcher(), "/bin/hermes")
            self.assertEqual(which.call_count, 1)
            with mock.patch.object(cli_runner.shutil, "which", return_value="/bin/hermes2"):
                self.assertEqual(runner.refresh_launcher(), "/bin/hermes2")


class TruncateTests(unittest.TestCase):
    def test_short_text_is_unchanged(self):
        self.assertEqual(truncate("abc"), "abc")

    def test_long_text_keeps_the_head_and_says_so(self):
        text = "x" * (cli_runner.STDOUT_LIMIT + 50)
        capped = truncate(text)
        self.assertTrue(capped.startswith("x" * 100))
        self.assertIn("truncated", capped)
        self.assertLess(len(capped), len(text) + 40)


if __name__ == "__main__":
    unittest.main()
