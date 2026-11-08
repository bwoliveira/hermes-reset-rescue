"""T-14 / T-37 — wrapper files: install, verify, never overwrite or delete a foreign file.

T-14 covers the file itself: it must run under the cron contract (stdlib only,
no argv, role as a constant, ``HERMES_HOME`` derived from its own path, no
gateway-lifecycle command). T-37 covers ownership: a file the plugin cannot
prove it wrote is left untouched by ``ensure`` and by ``uninstall``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from reset_rescue import wrappers
from reset_rescue.wrappers import (
    WRAPPER_FILES,
    WrapperError,
    body_hash,
    install_wrappers,
    is_ours,
    parse_header,
    remove_owned_wrappers,
    render_wrapper,
    verify_wrappers,
)

LAUNCHER = "/usr/local/bin/hermes"


class _Home:
    """A throwaway ``<HERMES_HOME>`` with a ``scripts/`` directory."""

    def __enter__(self) -> Path:
        self._dir = tempfile.TemporaryDirectory(prefix="rr-l3-wrappers-")
        home = Path(self._dir.name)
        (home / "scripts").mkdir()
        return home

    def __exit__(self, *_exc) -> None:
        self._dir.cleanup()


class RenderTests(unittest.TestCase):
    def test_every_role_renders_with_a_verifiable_header(self):
        for role in WRAPPER_FILES:
            text = render_wrapper(role, LAUNCHER)
            header = parse_header(text)
            self.assertIsNotNone(header, role)
            self.assertEqual(header["role"], role)
            self.assertEqual(header["template"], wrappers.WRAPPER_TEMPLATE_VERSION)
            self.assertTrue(is_ours(text, role), role)

    def test_the_body_hash_changes_when_the_body_changes(self):
        text = render_wrapper("scan", LAUNCHER)
        tampered = text.replace("return 1", "return 0")
        self.assertNotEqual(body_hash(text), body_hash(tampered))
        self.assertFalse(is_ours(tampered, "scan"))

    def test_a_file_without_a_header_is_not_ours(self):
        self.assertFalse(is_ours("#!/usr/bin/env python3\nprint('hi')\n", "scan"))

    def test_a_file_claiming_another_role_is_not_ours(self):
        text = render_wrapper("scan", LAUNCHER)
        self.assertFalse(is_ours(text, "primary"))

    def test_the_role_is_a_constant_in_the_file(self):
        for role, expected in (
            ("primary", '"--role", "primary"'),
            ("backup", '"--role", "backup"'),
            ("remind", '"--role", "remind"'),
        ):
            text = render_wrapper(role, LAUNCHER)
            self.assertIn(expected, text, role)

    def test_the_quota_wrapper_calls_quota_check(self):
        text = render_wrapper("quota", LAUNCHER)
        self.assertIn('"reset-rescue", "quota-check"', text)

    def test_the_five_wrappers_are_the_simplified_set(self):
        """§6 cut the rehearsal world: five roles, not six."""
        self.assertEqual(set(WRAPPER_FILES), {"scan", "primary", "backup", "remind", "quota"})

    def test_the_wrapper_passes_no_arguments_from_cron(self):
        """Cron runs the file with no argv; the file must not read sys.argv."""
        for role in WRAPPER_FILES:
            text = render_wrapper(role, LAUNCHER)
            self.assertNotIn("sys.argv", text, role)

    def test_home_comes_from_the_files_own_location(self):
        text = render_wrapper("scan", LAUNCHER)
        self.assertIn("Path(__file__).resolve().parent.parent", text)
        self.assertIn("HERMES_HOME=str(HOME)", text)

    def test_stdin_is_closed_so_a_prompt_fails_instead_of_hanging(self):
        for role in WRAPPER_FILES:
            self.assertIn("stdin=subprocess.DEVNULL", render_wrapper(role, LAUNCHER), role)

    def test_no_approval_bypass_in_any_wrapper(self):
        for role in WRAPPER_FILES:
            text = render_wrapper(role, LAUNCHER)
            for flag in ("--yolo", "--accept-hooks", "HERMES_ACCEPT_HOOKS", "HERMES_YOLO"):
                self.assertNotIn(flag, text, f"{role}: {flag}")

    def test_no_gateway_lifecycle_command_in_any_wrapper(self):
        """The cron create-time guard scans the script; our text must pass it."""
        from cron.lifecycle_guard import check_gateway_lifecycle

        for role in WRAPPER_FILES:
            text = render_wrapper(role, LAUNCHER)
            with _Home() as home:
                path = home / "scripts" / WRAPPER_FILES[role]
                path.write_text(text, encoding="utf-8")
                check_gateway_lifecycle("", str(path))  # raises if blocked

    def test_the_wrapper_is_stdlib_only(self):
        text = render_wrapper("scan", LAUNCHER)
        imports = [
            line.strip()
            for line in text.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        ]
        self.assertEqual(
            sorted(imports),
            sorted(
                [
                    "import os",
                    "import shutil",
                    "import subprocess",
                    "import sys",
                    "from pathlib import Path",
                ]
            ),
        )

    def test_unknown_role_is_refused(self):
        with self.assertRaises(WrapperError):
            render_wrapper("nope", LAUNCHER)

    def test_launcher_path_with_a_quote_is_still_valid_python(self):
        """A path with a space and a quote must still produce valid source.

        ``ast.parse`` proves syntax without compiling to a code object — the
        generated file is never executed here, only read by the scheduler.
        """
        import ast

        text = render_wrapper("scan", "/opt/my hermes/bin/hermes")
        ast.parse(text, filename="<wrapper>")


class InstallTests(unittest.TestCase):
    def test_install_writes_every_file_with_0700(self):
        with _Home() as home:
            state = install_wrappers(home, LAUNCHER)
            self.assertTrue(state.all_usable)
            for name in WRAPPER_FILES.values():
                path = home / "scripts" / name
                self.assertTrue(path.is_file(), name)
                self.assertEqual(path.stat().st_mode & 0o777, 0o700, name)

    def test_install_is_idempotent(self):
        with _Home() as home:
            install_wrappers(home, LAUNCHER)
            before = {
                name: (home / "scripts" / name).stat().st_mtime_ns
                for name in WRAPPER_FILES.values()
            }
            state = install_wrappers(home, LAUNCHER)
            after = {
                name: (home / "scripts" / name).stat().st_mtime_ns
                for name in WRAPPER_FILES.values()
            }
            self.assertTrue(state.all_usable)
            self.assertEqual(before, after, "a second ensure must not rewrite the files")

    def test_install_replaces_an_older_template(self):
        with _Home() as home:
            path = home / "scripts" / WRAPPER_FILES["scan"]
            path.write_text(render_wrapper("scan", LAUNCHER, template_version=0), encoding="utf-8")
            state = install_wrappers(home, LAUNCHER)
            self.assertEqual(state["reset-rescue-scan.py"], "ok")
            self.assertEqual(
                parse_header(path.read_text())["template"], wrappers.WRAPPER_TEMPLATE_VERSION
            )

    def test_install_never_overwrites_a_foreign_file(self):
        with _Home() as home:
            path = home / "scripts" / WRAPPER_FILES["scan"]
            path.write_text("#!/usr/bin/env python3\nprint('mine')\n", encoding="utf-8")
            state = install_wrappers(home, LAUNCHER)
            self.assertEqual(state["reset-rescue-scan.py"], "foreign")
            self.assertIn("reset-rescue-scan.py", state.conflicts)
            self.assertEqual(path.read_text(), "#!/usr/bin/env python3\nprint('mine')\n")

    def test_install_never_overwrites_a_user_edited_file(self):
        with _Home() as home:
            path = home / "scripts" / WRAPPER_FILES["primary"]
            text = render_wrapper("primary", LAUNCHER).replace("return 1", "return 0")
            path.write_text(text, encoding="utf-8")
            state = install_wrappers(home, LAUNCHER)
            self.assertEqual(state["reset-rescue-fire.py"], "conflict")
            self.assertEqual(path.read_text(), text)

    def test_install_only_touches_its_own_five_names(self):
        with _Home() as home:
            (home / "scripts" / "my-script.py").write_text("print(1)\n", encoding="utf-8")
            install_wrappers(home, LAUNCHER)
            self.assertTrue((home / "scripts" / "my-script.py").is_file())
            self.assertEqual(
                sorted(p.name for p in (home / "scripts").iterdir()),
                sorted([*WRAPPER_FILES.values(), "my-script.py"]),
            )

    def test_install_leaves_no_temp_file_behind(self):
        with _Home() as home:
            install_wrappers(home, LAUNCHER)
            leftovers = [p.name for p in (home / "scripts").iterdir() if p.name.startswith(".")]
            self.assertEqual(leftovers, [])

    def test_a_partial_role_subset_installs_only_those(self):
        with _Home() as home:
            state = install_wrappers(home, LAUNCHER, roles=["scan"])
            self.assertEqual(list(state), ["reset-rescue-scan.py"])
            self.assertFalse((home / "scripts" / WRAPPER_FILES["primary"]).exists())


class VerifyTests(unittest.TestCase):
    def test_verify_reports_missing_without_writing(self):
        with _Home() as home:
            statuses = verify_wrappers(home)
            self.assertEqual({status.state for status in statuses}, {"missing"})
            self.assertEqual(list((home / "scripts").iterdir()), [])

    def test_verify_reports_ok_after_install(self):
        with _Home() as home:
            install_wrappers(home, LAUNCHER)
            self.assertEqual({status.state for status in verify_wrappers(home)}, {"ok"})

    def test_verify_reports_a_conflict_for_an_edited_file(self):
        with _Home() as home:
            install_wrappers(home, LAUNCHER)
            path = home / "scripts" / WRAPPER_FILES["backup"]
            path.write_text(path.read_text().replace("return 1", "return 2"), encoding="utf-8")
            states = {status.name: status.state for status in verify_wrappers(home)}
            self.assertEqual(states["reset-rescue-fire-backup.py"], "conflict")

    def test_verify_reports_foreign_for_a_file_without_a_header(self):
        with _Home() as home:
            (home / "scripts" / WRAPPER_FILES["quota"]).write_text("x = 1\n", encoding="utf-8")
            states = {status.name: status.state for status in verify_wrappers(home)}
            self.assertEqual(states["reset-rescue-quota.py"], "foreign")


class RemoveTests(unittest.TestCase):
    def test_remove_deletes_only_our_files(self):
        with _Home() as home:
            install_wrappers(home, LAUNCHER)
            (home / "scripts" / "keep-me.py").write_text("print(1)\n", encoding="utf-8")
            removed = remove_owned_wrappers(home)
            self.assertEqual(sorted(removed), sorted(WRAPPER_FILES.values()))
            self.assertEqual([p.name for p in (home / "scripts").iterdir()], ["keep-me.py"])

    def test_remove_keeps_a_foreign_file(self):
        with _Home() as home:
            path = home / "scripts" / WRAPPER_FILES["scan"]
            path.write_text("#!/usr/bin/env python3\nprint('mine')\n", encoding="utf-8")
            removed = remove_owned_wrappers(home)
            self.assertEqual(removed, [])
            self.assertTrue(path.is_file())

    def test_remove_keeps_a_user_edited_file(self):
        with _Home() as home:
            install_wrappers(home, LAUNCHER)
            path = home / "scripts" / WRAPPER_FILES["primary"]
            edited = path.read_text() + "\n# my note\n"
            path.write_text(edited, encoding="utf-8")
            removed = remove_owned_wrappers(home)
            self.assertNotIn("reset-rescue-fire.py", removed)
            self.assertTrue(path.is_file())

    def test_remove_is_idempotent(self):
        with _Home() as home:
            install_wrappers(home, LAUNCHER)
            remove_owned_wrappers(home)
            self.assertEqual(remove_owned_wrappers(home), [])


class ExecutionTests(unittest.TestCase):
    """Run a rendered wrapper against a stub launcher: the exit code must pass through.

    These are the only host tests that start a real process, and they must:
    the wrapper's whole contract is what the *interpreter* does with the file
    (its ``__file__``-derived home, its argv, its exit code). The offline guard
    is told so explicitly, per test, with ``allow_subprocess`` — never by
    widening the default allow-list.
    """

    def _allowed(self):
        """Widen the offline guard for this one process, when it is installed.

        The lane runner (`tests/run_lane.py`) does not install the guard; the
        full suite (`scripts/run_tests.py`) does. Returning a null context keeps
        the same test honest under both.
        """
        import contextlib

        try:
            from offline_guard import allow_subprocess
        except ImportError:  # pragma: no cover - lane runner
            return contextlib.nullcontext()
        return allow_subprocess(os.path.basename(sys.executable), "hermes")

    def _run(
        self, home: Path, role: str, *, exit_code: int, env_home: str = ""
    ) -> subprocess.CompletedProcess:
        stub = home / "bin" / "hermes"
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "print(json.dumps({'argv': sys.argv[1:], 'home': os.environ.get('HERMES_HOME')}))\n"
            f"sys.exit({exit_code})\n",
            encoding="utf-8",
        )
        os.chmod(stub, 0o755)
        text = render_wrapper(role, str(stub))
        path = home / "scripts" / WRAPPER_FILES[role]
        path.write_text(text, encoding="utf-8")
        os.chmod(path, 0o700)
        env = dict(os.environ)
        env.pop("HERMES_HOME", None)
        env["PATH"] = f"{stub.parent}:{env.get('PATH', '')}"
        if env_home:
            env["HERMES_HOME"] = env_home
        return subprocess.run(
            [sys.executable, str(path)], capture_output=True, text=True, env=env, timeout=30
        )

    def test_the_wrapper_calls_the_right_subcommand_and_pins_the_home(self):
        with self._allowed(), _Home() as home:
            result = self._run(home, "primary", exit_code=0)
            self.assertEqual(result.returncode, 0, result.stderr)
            import json

            payload = json.loads(result.stdout)
            self.assertEqual(payload["argv"], ["reset-rescue", "fire", "--role", "primary"])
            self.assertEqual(payload["home"], str(home))

    def test_the_exit_code_passes_through(self):
        with self._allowed(), _Home() as home:
            result = self._run(home, "scan", exit_code=3)
            self.assertEqual(result.returncode, 3)

    def test_the_wrapper_overrides_an_inherited_home(self):
        with self._allowed(), _Home() as home:
            result = self._run(home, "backup", exit_code=0, env_home="/tmp/wrong-profile")
            import json

            self.assertEqual(json.loads(result.stdout)["home"], str(home))

    def test_a_missing_launcher_fails_cleanly(self):
        with self._allowed(), _Home() as home:
            text = render_wrapper("scan", "/nonexistent/hermes")
            path = home / "scripts" / WRAPPER_FILES["scan"]
            path.write_text(text, encoding="utf-8")
            env = dict(os.environ)
            env["PATH"] = "/usr/bin:/bin"  # no `hermes` on PATH
            env.pop("HERMES_HOME", None)
            result = subprocess.run(
                [sys.executable, str(path)], capture_output=True, text=True, env=env, timeout=30
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("launcher not found", result.stderr)


if __name__ == "__main__":
    unittest.main()
