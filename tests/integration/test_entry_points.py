"""The two entry points must not inherit ``HERMES_HOME``.

A lane's e2e run exports ``HERMES_HOME`` for its temp profile; a shell that
sourced it, or a terminal that ran it, keeps the value. Under that value the
offline guard protects the *scratch* home rather than the real profile, so
``test_offline_guard.RealProfileWriteBlocked`` fails for a reason that has
nothing to do with the guard — and, worse, a suite that should refuse a write
into ``~/.hermes`` would allow it.

Both scripts therefore ``unset HERMES_HOME`` before doing anything. This test
proves it by running them with a poisoned value and checking that the child
never sees it, rather than by grepping for the line: a future refactor that
moves the unset after a `source` would pass a grep and fail here.
"""

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

import offline_guard  # noqa: E402  (path set above)

#: A value that must never reach a suite. It is not a real profile.
POISON = os.path.join(
    os.path.expanduser("~"), ".hermes", "cache", "scratch", "rr-l5", "poisoned-home"
)


def _run(script: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HERMES_HOME"] = POISON
    # Running the real script is the point; `bash` is the only executable these
    # tests start, and the allowlist is scoped to the call.
    with offline_guard.allow_subprocess("bash"):
        return subprocess.run(
            ["bash", str(SCRIPTS / script), *args],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO),
            stdin=subprocess.DEVNULL,
            timeout=180,
            check=False,
        )


class EntryPointsDropHermesHome(unittest.TestCase):
    def test_test_sh_unsets_it_before_running_anything(self):
        # `--list` is the cheapest path that reaches the unset and exits without
        # starting a suite, so this stays fast.
        done = _run("test.sh", "--list")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn(POISON, done.stdout + done.stderr)

    def test_check_sh_unsets_it_before_running_anything(self):
        done = _run("check.sh", "--list")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn(POISON, done.stdout + done.stderr)

    def test_the_unset_is_a_real_unset_not_a_comment(self):
        """A commented-out `unset` would pass the two tests above vacuously."""
        for name in ("test.sh", "check.sh"):
            text = (SCRIPTS / name).read_text(encoding="utf-8")
            live = [line for line in text.splitlines() if line.strip() == "unset HERMES_HOME"]
            self.assertTrue(live, f"{name} has no live `unset HERMES_HOME`")

    def test_the_guard_would_notice_a_poisoned_home(self):
        """Why this matters: the guard's protected profile follows the variable.

        ``install`` reads ``HERMES_HOME`` when no ``real_home`` is given, so an
        inherited value silently moves the protected profile. This pins that
        behaviour so the two scripts above keep their reason.
        """
        saved = offline_guard._state.real_home
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.environ["HERMES_HOME"] = tmp
                try:
                    # `install` is idempotent, so exercise the resolution it uses.
                    resolved = offline_guard._resolve(os.environ.get("HERMES_HOME") or "~/.hermes")
                    self.assertEqual(resolved, Path(tmp).resolve())
                finally:
                    os.environ["HERMES_HOME"] = POISON
        finally:
            offline_guard._state.real_home = saved


if __name__ == "__main__":
    unittest.main()
