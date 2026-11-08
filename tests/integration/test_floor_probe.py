"""T-02 — the `requires_hermes` floor probe.

The probe runs `git log -S` / `git tag --contains` against the Hermes install.
These tests build a throw-away repository with the same shape (a symbol that
appears in one commit, tags before and after) so the probe's arithmetic is
proven without touching the install, the network or the clock.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROBE = REPO / "scripts" / "floor_probe.sh"

_SCRIPTS = REPO / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
import offline_guard  # noqa: E402  (path set above)

# The probe is a shell script that shells out to git; proving it works means
# starting those two processes. Everything else stays blocked. Scoped to this
# module so a later suite cannot inherit the exemption.
_ALLOWED = None


def setUpModule():
    global _ALLOWED
    _ALLOWED = offline_guard.allow_subprocess("bash", "git")
    _ALLOWED.__enter__()


def tearDownModule():
    if _ALLOWED is not None:
        _ALLOWED.__exit__(None, None, None)


GIT_ENV = {
    "GIT_AUTHOR_NAME": "Reset Rescue",
    "GIT_AUTHOR_EMAIL": "dev@example.invalid",
    "GIT_COMMITTER_NAME": "Reset Rescue",
    "GIT_COMMITTER_EMAIL": "dev@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000",
}


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ, **GIT_ENV)
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=env
    )
    return done.stdout


def _commit(repo: Path, filename: str, text: str, message: str) -> str:
    (repo / filename).write_text(text, encoding="utf-8")
    _git(repo, "add", filename)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").strip()


class FloorProbeTest(unittest.TestCase):
    """A synthetic Hermes-shaped history: old surface, new surface, tags."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="rr-floor-")
        cls.repo = Path(cls.tmp.name) / "hermes"
        cls.repo.mkdir()
        _git(cls.repo, "init", "-q", "-b", "main")
        _commit(cls.repo, "old.py", "def old_surface():\n    return 1\n", "feat: old surface")
        _git(cls.repo, "tag", "-a", "v0.20.0", "-m", "v0.20.0")
        _commit(cls.repo, "new.py", "def new_surface():\n    return 2\n", "feat: new surface")
        _git(cls.repo, "tag", "-a", "v0.21.0", "-m", "v0.21.0")
        _git(cls.repo, "tag", "-a", "v0.21.0+canary.20260101T000000Z", "-m", "canary")
        # A later commit that must not move the introducing commit of either symbol.
        _commit(cls.repo, "doc.md", "docs\n", "docs: unrelated")
        _git(cls.repo, "tag", "-a", "v0.22.0", "-m", "v0.22.0")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _symbols(self, body: str) -> Path:
        path = Path(self.tmp.name) / "symbols.txt"
        path.write_text(textwrap.dedent(body), encoding="utf-8")
        return path

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(PROBE), *args], capture_output=True, text=True, check=False
        )

    def test_reports_earliest_stable_tag_per_symbol(self):
        symbols = self._symbols("""
            old|old_surface|old.py|an old surface
            new|new_surface|new.py|a newer surface
        """)
        done = self._run("--repo", str(self.repo), "--symbols", str(symbols))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("old ", done.stdout)
        self.assertIn("v0.20.0", done.stdout)
        self.assertIn("v0.21.0", done.stdout)
        self.assertIn("candidate floor: >=0.21.0", done.stdout)
        self.assertIn("RESULT: OK", done.stdout)

    def test_canary_tags_never_become_the_floor(self):
        symbols = self._symbols("new|new_surface|new.py|a newer surface\n")
        done = self._run("--repo", str(self.repo), "--symbols", str(symbols))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertNotIn("canary", done.stdout)
        self.assertIn("candidate floor: >=0.21.0", done.stdout)

    def test_unresolved_symbol_fails_the_probe(self):
        symbols = self._symbols("ghost|never_existed|ghost.py|not in the tree\n")
        done = self._run("--repo", str(self.repo), "--symbols", str(symbols))
        self.assertEqual(done.returncode, 2)
        self.assertIn("UNRESOLVED", done.stdout)
        self.assertIn("RESULT: FAIL", done.stdout)

    def test_informational_rows_cannot_raise_the_floor(self):
        symbols = self._symbols("""
            new|new_surface|new.py|a newer surface
            doc|never_existed|doc.md|documentation only|no
        """)
        done = self._run("--repo", str(self.repo), "--symbols", str(symbols))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("candidate floor: >=0.21.0", done.stdout)
        self.assertIn("(n/a)", done.stdout)

    def test_check_mode_matches_and_mismatches(self):
        symbols = self._symbols("new|new_surface|new.py|a newer surface\n")
        ok = self._run("--repo", str(self.repo), "--symbols", str(symbols), "--check", "0.21.0")
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        bad = self._run("--repo", str(self.repo), "--symbols", str(symbols), "--check", "0.20.0")
        self.assertEqual(bad.returncode, 1)
        self.assertIn("does NOT match", bad.stdout)

    def test_out_file_is_written_with_the_header_and_rows(self):
        symbols = self._symbols("new|new_surface|new.py|a newer surface\n")
        out = Path(self.tmp.name) / "floor.txt"
        done = self._run(
            "--repo", str(self.repo), "--symbols", str(symbols), "--out", str(out), "--quiet"
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        text = out.read_text(encoding="utf-8")
        self.assertIn("requires_hermes floor probe", text)
        self.assertIn("candidate floor: >=0.21.0", text)

    def test_missing_repo_is_a_usage_error(self):
        symbols = self._symbols("new|new_surface|new.py|a newer surface\n")
        done = self._run("--repo", str(Path(self.tmp.name) / "nope"), "--symbols", str(symbols))
        self.assertEqual(done.returncode, 2)
        self.assertIn("not a git checkout", done.stderr)

    def test_committed_symbol_list_is_well_formed(self):
        """The real list must stay parseable: 4 or 5 fields, paths that exist."""
        committed = REPO / "docs" / "release" / "symbols.txt"
        self.assertTrue(committed.is_file(), "docs/release/symbols.txt is missing")
        install = Path("/usr/local/lib/hermes-agent")
        rows = 0
        for number, raw in enumerate(committed.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            fields = raw.split("|")
            self.assertIn(len(fields), (4, 5), f"line {number}: {raw!r}")
            ident, symbol, path = fields[0], fields[1], fields[2]
            self.assertTrue(ident and symbol and path, f"line {number}: empty field")
            if install.is_dir():
                self.assertTrue(
                    (install / path).exists(),
                    f"line {number}: {path} does not exist in the Hermes install",
                )
            rows += 1
        self.assertGreaterEqual(rows, 10, "the symbol list lost rows")


if __name__ == "__main__":
    unittest.main()
