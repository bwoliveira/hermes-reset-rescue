"""R17 — every temporary profile must be outside the real one, and verified.

The reviewer's isolated run failed at the *first* check, ``plugins enable``:
``hermes_cli/plugins_admission.py:66-77`` calls ``sync_venv(explicit=True)``,
which ``pm/install.py:935-938`` exempts from the lazy-install policy, so
admission re-resolved the whole recorded extras union and needed
``firecrawl-py``/``huggingface-hub`` from the network.

The reason that was invisible here is the more important defect:

**Hermes collapses a ``HERMES_HOME`` nested under the native home back to the
native home.** ``hermes_constants._hermes_root_of`` returns ``~/.hermes`` for any
path under it (that is how ``<root>/profiles/<name>`` works). Every temp profile
this repo built lived under ``<HERMES_HOME>/cache/scratch/...``, so:

* the e2e enabled the plugin in the **real** profile and read its already-current
  ``installs/`` state — it passed here and failed in a clean isolation;
* the T-36 integration harness ran discovery and ``plugins doctor`` against the
  real profile's plugin set;
* the unit fakes' ``temp_home`` handed out the real profile as a "temp" one.

These tests pin the mechanism and then enforce it everywhere:

* the resolver itself (a nested home collapses; a home outside ``$HOME`` does
  not), so a future scratch path under ``~/.hermes`` fails loudly;
* the shared guard (``tests/support/isolation.py``) refuses a bad home and
  builds a good one;
* a repo-wide scan that fails when a helper sets ``HERMES_HOME`` without going
  through the guard — the rule the user asked for, enforced rather than
  remembered.
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for entry in (str(REPO), str(REPO / "scripts"), str(REPO / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from tests.support import isolation  # noqa: E402  (path set above)

E2E_RUNNER = REPO / "tests" / "e2e" / "run_e2e.py"


class TheResolverCollapsesANestedHome(unittest.TestCase):
    """The mechanism, exercised through Hermes' own code."""

    def test_a_home_nested_under_the_native_home_collapses_to_it(self):
        native = Path.home() / ".hermes"
        nested = native / "cache" / "scratch" / "rr-l5" / "e2e-home"
        self.assertEqual(
            isolation.resolved_root(nested),
            native,
            "Hermes no longer collapses a nested HERMES_HOME; the guard may be "
            "unnecessary — but this test must be updated deliberately, not silently",
        )
        self.assertTrue(isolation.is_inside_native_home(nested))

    def test_a_home_outside_the_native_home_resolves_to_itself(self):
        """My variant: the fix's premise — a profile elsewhere is a real profile."""
        with tempfile.TemporaryDirectory(prefix="rr-r17-", dir=str(isolation.TEMP_ROOT)) as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            self.assertEqual(isolation.resolved_root(home), home)
            self.assertFalse(isolation.is_inside_native_home(home))


class TheGuardRefusesTheRealProfile(unittest.TestCase):
    def test_assert_isolated_rejects_a_nested_home(self):
        nested = Path.home() / ".hermes" / "cache" / "scratch" / "rr-gc" / "home"
        with self.assertRaises(isolation.IsolationError) as caught:
            isolation.assert_isolated(nested)
        self.assertIn("real profile", str(caught.exception))

    def test_assert_isolated_rejects_the_native_home_itself(self):
        with self.assertRaises(isolation.IsolationError):
            isolation.assert_isolated(Path.home() / ".hermes")

    def test_temp_home_is_outside_the_real_profile_and_verified(self):
        home = isolation.temp_home("rr-guard-")
        try:
            self.assertFalse(isolation.is_inside_native_home(home))
            self.assertEqual(isolation.resolved_root(home), home)
            self.assertTrue(home.is_dir())
        finally:
            import shutil

            shutil.rmtree(home, ignore_errors=True)

    def test_env_for_refuses_a_bad_home_before_building_an_environment(self):
        with self.assertRaises(isolation.IsolationError):
            isolation.env_for(Path.home() / ".hermes" / "cache" / "scratch" / "x")

    def test_env_for_drops_pythonpath(self):
        home = isolation.temp_home("rr-guard-env-")
        try:
            env = isolation.env_for(home, PYTHONDONTWRITEBYTECODE="1")
            self.assertEqual(env["HERMES_HOME"], str(home))
            self.assertNotIn("PYTHONPATH", env)
            self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        finally:
            import shutil

            shutil.rmtree(home, ignore_errors=True)


class TheE2eHomeMustBeARealProfile(unittest.TestCase):
    def test_the_runner_exposes_its_home_and_it_is_not_the_real_one(self):
        """The runner's home must be outside ``~/.hermes`` and resolve to itself.

        Read from the module's source rather than by importing it: importing
        ``run_e2e`` starts nothing, but the constant is what matters, and the
        resolver call below is the same one the runner makes.
        """
        import ast

        source = E2E_RUNNER.read_text(encoding="utf-8")
        tree = ast.parse(source)
        default = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "E2E_HOME":
                        default = ast.unparse(node.value)
        self.assertIsNotNone(default, "run_e2e.py no longer defines E2E_HOME")
        assert default is not None  # narrows for the type checker; the assert above is the test
        self.assertIn("RR_E2E_HOME", default, "the e2e home is not overridable")
        # The default path literal is the part that must be outside $HOME.
        literals = [part for part in default.split("or") if "/" in part]
        self.assertTrue(literals, f"no path literal in E2E_HOME = {default}")
        path = Path(literals[-1].strip().strip("'\""))
        self.assertFalse(
            isolation.is_inside_native_home(path),
            f"the e2e home default {path} is inside the real profile; Hermes "
            "collapses it to the real root and the run proves nothing",
        )
        self.assertEqual(isolation.resolved_root(path), path)


class E2ePreparationOwnsOnlyNewDirectories(unittest.TestCase):
    def setUp(self):
        from unittest import mock

        self.mock = mock
        e2e_dir = str(E2E_RUNNER.parent)
        if e2e_dir not in sys.path:
            sys.path.insert(0, e2e_dir)
        import run_e2e

        self.e2e = run_e2e
        temporary = tempfile.TemporaryDirectory(prefix="rr-e2e-root-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "existing"
        self.root.mkdir()
        self.sentinel = self.root / "sentinel"
        self.sentinel.write_text("keep me\n")
        patcher = mock.patch.object(run_e2e, "E2E_HOME", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_rejected_isolation_never_deletes_the_configured_directory(self):
        """Exercise the destructive regression only against a synthetic profile."""
        with (
            self.mock.patch.object(
                self.e2e, "assert_isolated", side_effect=isolation.IsolationError("refused")
            ),
            self.assertRaises(isolation.IsolationError),
        ):
            self.e2e._prepare_home()
        self.assertTrue(self.sentinel.is_file(), "isolation failure deleted pre-existing data")
        self.assertEqual(self.sentinel.read_text(), "keep me\n")

    def test_unsafe_roots_are_refused_before_any_write_or_delete(self):
        unsafe = [
            Path("/"),
            Path("/var/tmp"),
            Path("relative"),
            Path.home(),
            isolation.NATIVE_HOME,
            isolation.NATIVE_HOME / "cache" / "probe",
            isolation.HERMES_INSTALL,
            REPO,
        ]
        for root in unsafe:
            with (
                self.subTest(root=root),
                self.mock.patch.object(self.e2e, "E2E_HOME", root),
                self.mock.patch.object(Path, "mkdir") as mkdir,
                self.mock.patch.object(self.e2e.shutil, "rmtree") as remove,
                self.assertRaises(isolation.IsolationError),
            ):
                self.e2e._prepare_home()
            mkdir.assert_not_called()
            remove.assert_not_called()

    def test_symlinked_root_and_ancestor_are_refused_without_touching_target(self):
        link = self.root.parent / "alias"
        link.symlink_to(self.root, target_is_directory=True)
        for root in [link, link / "child"]:
            with (
                self.subTest(root=root),
                self.mock.patch.object(self.e2e, "E2E_HOME", root),
                self.assertRaises(isolation.IsolationError),
            ):
                self.e2e._prepare_home()
        self.assertEqual(self.sentinel.read_text(), "keep me\n")
        self.assertFalse((self.root / "child").exists())

    def test_each_preparation_owns_a_unique_directory_and_cleanup_preserves_existing_data(self):
        with (
            self.mock.patch.object(self.e2e, "copy_plugin"),
            self.mock.patch.object(self.e2e.seed, "seed_profile", return_value=self.root),
        ):
            first = self.e2e._prepare_home()
            second = self.e2e._prepare_home()
        self.assertNotEqual(first, second)
        self.assertEqual(first.parent.parent, self.root)
        self.assertTrue(first.is_dir())
        self.assertTrue(second.is_dir())
        self.e2e._cleanup_home(first)
        self.assertFalse(first.parent.exists())
        self.assertTrue(second.is_dir())
        self.e2e._cleanup_home(second)
        self.assertEqual(list(self.root.iterdir()), [self.sentinel])

    def test_failed_seeding_removes_only_its_new_directory(self):
        with (
            self.mock.patch.object(self.e2e, "copy_plugin"),
            self.mock.patch.object(self.e2e.seed, "seed_profile", return_value=None),
            self.assertRaises(self.e2e.CheckFailed),
        ):
            self.e2e._prepare_home()
        self.assertEqual(list(self.root.iterdir()), [self.sentinel])

    def test_child_isolation_failure_cleans_only_its_new_directory(self):
        with (
            self.mock.patch.object(
                self.e2e,
                "assert_isolated",
                side_effect=[self.root, isolation.IsolationError("child refused")],
            ),
            self.assertRaises(isolation.IsolationError),
        ):
            self.e2e._prepare_home()
        self.assertEqual(list(self.root.iterdir()), [self.sentinel])

    def test_cleanup_refuses_unowned_and_replaced_directories(self):
        with self.assertRaises(isolation.IsolationError):
            self.e2e._cleanup_home(self.root / "home")
        with (
            self.mock.patch.object(self.e2e, "copy_plugin"),
            self.mock.patch.object(self.e2e.seed, "seed_profile", return_value=self.root),
        ):
            home = self.e2e._prepare_home()
        old = home.parent.with_name(home.parent.name + "-moved")
        home.parent.rename(old)
        home.parent.mkdir()
        replacement = home.parent / "sentinel"
        replacement.write_text("not owned\n")
        with self.assertRaises(isolation.IsolationError):
            self.e2e._cleanup_home(home)
        self.assertEqual(replacement.read_text(), "not owned\n")
        # Return the actual owned directory so the normal verified cleanup works.
        replacement.unlink()
        home.parent.rmdir()
        old.rename(home.parent)
        self.e2e._cleanup_home(home)

    def test_cli_refuses_a_changed_home_before_starting_a_process(self):
        home = self.root / "home"
        home.mkdir()
        runner = self.e2e.Runner(home, {"HERMES_HOME": str(isolation.NATIVE_HOME)})
        with self.mock.patch.object(self.e2e.subprocess, "run") as launch:
            with self.assertRaises(isolation.IsolationError):
                runner.hermes("status", network=True)
            with self.assertRaises(isolation.IsolationError):
                runner.python(home / "script.py")
            runner.admin_env["HERMES_HOME"] = str(isolation.NATIVE_HOME)
            with self.assertRaises(isolation.IsolationError):
                runner.hermes("status")
        launch.assert_not_called()


class NoHelperMaySetHermesHomeUnguarded(unittest.TestCase):
    """The rule, enforced: a temp ``HERMES_HOME`` goes through the guard.

    A file may set ``HERMES_HOME`` when it is one of:

    * the guard itself (``tests/support/isolation.py``);
    * the e2e runner, which calls ``assert_isolated`` on its home;
    * the integration harness, which routes through ``isolation.temp_home`` /
      ``isolation.env_for``;
    * the unit fakes, which do the same;
    * a script that ``unset``s it (``scripts/test.sh``, ``scripts/check.sh``);
    * a test that deliberately passes a *poisoned* value to prove a script
      drops it (``tests/integration/test_entry_points.py``).

    Anything else is a new temp-home builder that skipped the guard, which is
    exactly how R17 happened. The allowlist is by file, and each entry carries
    its reason, so adding one is a deliberate act.
    """

    #: file (relative to the repo) -> why it may touch HERMES_HOME directly.
    ALLOWED = {
        "tests/support/isolation.py": "the guard itself",
        "tests/e2e/gate_runtime.py": "seeds and verifies a gate-only bootstrap home",
        "tests/e2e/run_e2e.py": "validates roots, creates owned profiles, checks CLI homes",
        "tests/live_ticker/prepare_seed.py": "checks temp home before native PM preparation",
        "tests/live_ticker/run_ticker_e2e.py": "uses guarded preparation for its scheduler profile",
        "scripts/ticker_check.sh": "checks the resolved temp home before every Hermes launch",
        "tests/integration/harness.py": "routes through isolation.temp_home/env_for",
        "tests/integration/test_entry_points.py": "passes a poisoned value on purpose",
        "tests/fakes.py": "routes through isolation.temp_home/env_for",
        "scripts/test.sh": "unsets it",
        "scripts/check.sh": "unsets it",
        "scripts/offline_guard.py": "reads it to decide what to protect",
        "scripts/run_tests.py": "reads it to decide what to protect",
        "reset_rescue/cli_runner.py": "the plugin's own child environment (host code)",
        "reset_rescue/wrappers.py": "the generated wrapper's environment (host code)",
        "reset_rescue/host.py": "the plugin's own profile resolution (host code)",
        "reset_rescue/clock.py": "documents <HERMES_HOME>/scripts (host code)",
        "reset_rescue/store.py": "documents <HERMES_HOME>/plugin-data (host code)",
        "reset_rescue/version.py": "documents the profile layout (host code)",
        "__init__.py": "documents the stub context (host code)",
        "tests/integration/test_offline_guard.py": "installs a guard around a synthetic profile",
        "tests/integration/test_r17_fresh_isolation.py": "this file",
        "tests/host/test_t12_cli_runner.py": "asserts the child env the runner builds",
        "tests/host/test_t14_t37_wrappers.py": "asserts the wrapper text",
        "tests/host/test_t01_manifest_register.py": "asserts the manifest contract",
        "tests/host/test_t20_messages.py": "asserts notice text",
        "tests/flows/test_store.py": "documents that it never touches one",
        "tests/integration/test_readme.py": "asserts README text",
    }

    #: Files that only *mention* the name in prose/docs are not builders; the
    #: scan looks for an assignment or an export, not for the word.
    ASSIGNMENT = re.compile(
        r"""(?x)
        (?:^|\n)\s*
        (?:
            (?:export\s+)?HERMES_HOME\s*=                 # shell / env assignment
          | (?:os\.)?environ\[[\"']HERMES_HOME[\"']\]\s*=  # python dict assignment
          | (?:os\.)?environ\.setdefault\([\"']HERMES_HOME[\"']
          | env\[[\"']HERMES_HOME[\"']\]\s*=               # a plain dict named env
          | env\.setdefault\([\"']HERMES_HOME[\"']
          | env\.update\([^)]*HERMES_HOME                # env.update(HERMES_HOME=...)
        )""",
    )

    def _sources(self) -> list[Path]:
        out: list[Path] = []
        skipped = {".git", "docs", "__pycache__", "node_modules"}
        for pattern in ("*.py", "*/*.py", "*/*/*.py", "*.sh", "*/*.sh"):
            for path in REPO.glob(pattern):
                if any(part in skipped for part in path.parts):
                    continue
                out.append(path)
        return sorted(set(out))

    def test_every_file_that_sets_hermes_home_is_on_the_allowlist(self):
        offenders: list[str] = []
        for path in self._sources():
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if not self.ASSIGNMENT.search(text):
                continue
            relative = path.relative_to(REPO).as_posix()
            if relative not in self.ALLOWED:
                offenders.append(relative)
        self.assertEqual(
            offenders,
            [],
            "these files set HERMES_HOME without the isolation guard; build temp "
            "profiles with tests/support/isolation.py (temp_home/assert_isolated/"
            "env_for) or add the file here with its reason",
        )

    def test_every_allowlisted_file_still_exists_and_still_sets_it(self):
        """A stale allowlist entry hides a rename; a live one must be live."""
        for relative in self.ALLOWED:
            path = REPO / relative
            self.assertTrue(path.is_file(), f"allowlist names a missing file: {relative}")
            if relative in {"tests/support/isolation.py", "scripts/test.sh", "scripts/check.sh"}:
                continue
            text = path.read_text(encoding="utf-8")
            self.assertTrue(
                self.ASSIGNMENT.search(text) or "HERMES_HOME" in text,
                f"allowlist names a file that no longer mentions HERMES_HOME: {relative}",
            )


class TheGateMustRefuseAnUnprovenE2e(unittest.TestCase):
    def test_check_sh_runs_the_e2e_in_a_network_namespace(self):
        script = (REPO / "scripts" / "check.sh").read_text(encoding="utf-8")
        self.assertIn("unshare -rn", script, "the e2e step lost its network isolation")
        self.assertIn("record e2e na", script, "an unisolated e2e must be n/a, not a pass")

    def test_check_sh_fails_when_the_seed_cannot_be_built(self):
        script = (REPO / "scripts" / "check.sh").read_text(encoding="utf-8")
        self.assertIn("tests/e2e/seed.py", script, "check.sh does not check the offline seed")
        self.assertIn(
            "record e2e fail",
            script,
            "a missing seed must fail the gate, never fall back to a network sync",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
