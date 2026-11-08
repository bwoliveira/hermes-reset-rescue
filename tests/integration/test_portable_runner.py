"""Portable entry points refuse a missing runtime before importing any suite."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import offline_guard  # noqa: E402


class PortableRunner(unittest.TestCase):
    def run_command(self, command, *, wrapper=None):
        env = dict(os.environ)
        env.pop("RUNTIME_WRAPPER", None)
        if wrapper is not None:
            env["RUNTIME_WRAPPER"] = str(wrapper)
        with offline_guard.allow_subprocess("bash", Path(sys.executable).name):
            return subprocess.run(
                command,
                cwd=REPO,
                env=env,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )

    def assert_instruction(self, done):
        output = done.stdout + done.stderr
        self.assertEqual(done.returncode, 2, output)
        self.assertIn("RUNTIME_WRAPPER=", output)
        self.assertIn("run_runtime_script.py", output)
        self.assertNotIn("Traceback", output)
        self.assertNotIn("ImportError", output)
        self.assertNotIn("== tests/", output)
        self.assertNotIn("=== unit", output)
        self.assertEqual(output.count("RUNTIME_WRAPPER="), 1, output)

    def test_unset_wrapper_stops_every_entry_point_with_one_instruction(self):
        commands = (
            ["bash", str(SCRIPTS / "test.sh"), "--python-only"],
            ["bash", str(SCRIPTS / "check.sh")],
            [sys.executable, "-I", str(SCRIPTS / "run_tests.py"), "engine"],
        )
        for command in commands:
            with self.subTest(command=command):
                self.assert_instruction(self.run_command(command))

    def test_unimportable_hermes_stops_the_python_runner_without_a_traceback(self):
        done = self.run_command(
            [sys.executable, "-I", "-S", str(SCRIPTS / "run_tests.py"), "engine"],
            wrapper=SCRIPTS / "run_tests.py",
        )
        self.assert_instruction(done)

    def test_unimportable_hermes_stops_both_shell_entry_points_before_suites(self):
        with tempfile.TemporaryDirectory(prefix="rr-runtime-probe-") as tmp:
            wrapper = Path(tmp) / "without-hermes.py"
            wrapper.write_text(
                "import os, sys\n"
                "os.execv(sys.executable, [sys.executable, '-I', '-S', *sys.argv[1:]])\n",
                encoding="utf-8",
            )
            for name in ("test.sh", "check.sh"):
                with self.subTest(script=name):
                    self.assert_instruction(
                        self.run_command(["bash", str(SCRIPTS / name)], wrapper=wrapper)
                    )

    def test_listing_suites_needs_no_runtime(self):
        for name in ("test.sh", "check.sh"):
            with self.subTest(script=name):
                done = self.run_command(["bash", str(SCRIPTS / name), "--list"])
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn("integration", done.stdout)
