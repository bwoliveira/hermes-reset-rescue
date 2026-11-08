"""T-36 — load the plugin through Hermes' real discovery in a temp home.

Every other suite builds ``Deps`` by hand, so it proves the engine and never the
*contract with Hermes*: that a manifest in ``<HERMES_HOME>/plugins/`` is found,
that ``register(ctx)`` is accepted, that ``hermes plugins doctor`` passes, and
that the CLI command the cron wrapper calls actually exists.

This harness does that, and only that, against a temporary ``HERMES_HOME``. It
never reads or writes the real profile: the guard in ``scripts/offline_guard.py``
refuses a write there, and ``temp_home`` puts the scratch home under
``<HERMES_HOME>/cache/scratch/rr-l5/``.

Two modes:

* **in-process** — ``discover()`` and ``doctor()`` call Hermes' own
  ``scan_directory`` / ``doctor_plugin`` with ``HERMES_HOME`` pointed at the
  scratch home. Fast, and the failure message names the manifest problem.
* **subprocess** — ``hermes plugins list`` / ``hermes plugins doctor`` in a real
  ``hermes`` process with ``HERMES_HOME`` in its environment. Slower, but it
  proves the launcher sees the same home the plugin will run under. Used only
  where the in-process check cannot prove the contract (the CLI entry point).

The plugin tree is *copied* into the temp home rather than symlinked: discovery
resolves paths, and a symlink would make a passing test depend on the checkout
location.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any, Iterable, Optional

from tests.support.isolation import scratch_root

REPO = Path(__file__).resolve().parents[2]
#: Retained for the docstring's sake only; ``temp_home`` builds outside ``$HOME``
#: (``tests.support.isolation``). It used to be the scratch root, which is inside
#: the real profile and therefore resolved to it (R17).
SCRATCH = scratch_root()

#: Everything the plugin needs at runtime, copied into the temp home. Docs and
#: tests are excluded: they are not loaded, and copying them makes every test
#: walk the whole repository.
PLUGIN_ENTRIES = (
    "plugin.yaml",
    "__init__.py",
    "reset_rescue",
    "dashboard",
    "desktop",
)

#: Files that must never appear in a temp home copy (a leak would mean the
#: plugin shipped state or a secret inside the package).
FORBIDDEN_IN_COPY = ("auth.json", "config.yaml", "state.db", ".env")


def plugin_manifest() -> Optional[Path]:
    """The manifest, if L3 has landed it."""
    for name in ("plugin.yaml", "plugin.yml"):
        candidate = REPO / name
        if candidate.is_file():
            return candidate
    return None


def has_manifest() -> bool:
    return plugin_manifest() is not None


def stage_plugin(home: Path, *, name: str = "reset-rescue") -> Path:
    """Copy the plugin into ``<home>/plugins/<name>/`` and return that path.

    A missing entry is skipped rather than failing: L3 lands the manifest and the
    dashboard half on its own schedule, and a harness that refuses to run until
    every file exists cannot report *which* one is missing.
    """
    plugins_root = home / "plugins"
    plugins_root.mkdir(parents=True, exist_ok=True)
    target = plugins_root / name
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for entry in PLUGIN_ENTRIES:
        source = REPO / entry
        if not source.exists():
            continue
        if source.is_dir():
            shutil.copytree(
                source, target / entry, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
            )
        else:
            shutil.copy2(source, target / entry)
    for forbidden in FORBIDDEN_IN_COPY:
        if (target / forbidden).exists():  # pragma: no cover - a leak, not a path
            raise AssertionError(f"{forbidden} was copied into the plugin package")
    return target


def temp_home(prefix: str = "t36-") -> Path:
    """A fresh temp ``HERMES_HOME`` outside the real profile, verified (R17).

    It used to live under ``<HERMES_HOME>/cache/scratch/rr-l5`` — inside the real
    profile, which ``hermes_constants._hermes_root_of`` resolves back to
    ``~/.hermes``. Every "temp profile" discovery/doctor run was therefore
    reading and writing the real profile. ``tests.support.isolation`` creates it
    outside ``$HOME`` and refuses one that does not resolve to itself.

    The profile also gets the install's committed dependency state, linked from
    local files by :mod:`seed`. A real profile has that state; without it the
    first ``hermes`` launch refuses with "no dependency environment is committed
    for this install".
    """
    from tests.support.isolation import temp_home as isolated_home

    home = isolated_home(prefix)
    (home / "plugin-data" / "reset-rescue").mkdir(parents=True, exist_ok=True)
    (home / "cron").mkdir(parents=True, exist_ok=True)
    _seed(home)
    return home


def _seed(home: Path) -> None:
    """Give *home* the install's dependency state, or skip when none exists."""
    import sys

    e2e = str(REPO / "tests" / "e2e")
    if e2e not in sys.path:
        sys.path.insert(0, e2e)
    import seed

    seed.seed_profile(home)


class HomeEnv:
    """Point ``HERMES_HOME`` at a temp home for the length of a block.

    ``hermes_constants.get_hermes_home()`` reads the environment on every call,
    so setting it here is enough for discovery, ``plugin_data_dir()`` and the
    doctor's own staging. The previous value is restored even on failure.
    """

    def __init__(self, home: Path):
        self.home = home
        self._previous: Optional[str] = None

    def __enter__(self) -> Path:
        from tests.support.isolation import assert_isolated

        assert_isolated(self.home)
        self._previous = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.home)
        return self.home

    def __exit__(self, *exc_info: object) -> None:
        if self._previous is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._previous


def discover(home: Path) -> list[Any]:
    """Run Hermes' real directory scan over ``<home>/plugins``."""
    from hermes_cli.plugins_discovery import scan_directory

    return scan_directory(home / "plugins", source="user")


def doctor(plugin_dir: Path) -> Any:
    """Run ``hermes plugins doctor``'s own implementation on one plugin dir."""
    from hermes_cli.plugin_dev import doctor_plugin

    return doctor_plugin(plugin_dir)


def doctor_messages(report: Any) -> list[str]:
    """Flatten a ``DoctorReport`` into ``level: message`` strings."""
    out: list[str] = []
    for finding in getattr(report, "findings", ()):
        out.append(f"{getattr(finding, 'level', '?')}: {getattr(finding, 'message', finding)}")
    return out


def run_hermes(
    args: Iterable[str], home: Path, *, timeout: float = 120.0
) -> subprocess.CompletedProcess:
    """Run the real ``hermes`` launcher against ``home`` (never the real profile).

    ``HERMES_DISABLE_LAZY_INSTALLS=1`` is the documented bridge var the Docker
    image and the hermetic harness set. A temp profile has no ``installs/<key>/``
    of its own, so without it the first launch tries to build one
    (``hermes_cli/venv_sync._prepare_borrowed_launch``) and needs the network —
    the same defect R17 found in the e2e. A real profile already has that state;
    these tests must not install anything.
    """
    from tests.support.isolation import env_for

    env = env_for(home, PYTHONDONTWRITEBYTECODE="1", HERMES_DISABLE_LAZY_INSTALLS="1")
    return subprocess.run(
        ["hermes", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        stdin=subprocess.DEVNULL,
        check=False,
    )


def cli_runner_available() -> bool:
    return shutil.which("hermes") is not None


def load_plugin_module(plugin_dir: Path) -> Any:
    """Import the staged ``__init__.py`` the way the host does: by path.

    The host republishes plugin packages under its own module names, so the
    plugin must not rely on being importable as ``reset_rescue`` from ``cwd``.
    Raises ``FileNotFoundError`` when the entry point has not landed yet, so a
    caller can skip instead of failing on a missing file.
    """
    import importlib.util

    entry = plugin_dir / "__init__.py"
    if not entry.is_file():
        raise FileNotFoundError(entry)
    spec = importlib.util.spec_from_file_location(
        "reset_rescue_plugin_under_test", entry, submodule_search_locations=[str(plugin_dir)]
    )
    if spec is None or spec.loader is None:  # pragma: no cover - only on a broken file
        raise AssertionError(f"cannot import {entry}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def fake_context() -> Any:
    """A ``ctx`` that records registrations instead of touching the host.

    Only the surface ``register(ctx)`` is allowed to use is present; a call to
    anything else raises ``AttributeError``, which is the point — a plugin that
    reaches outside the documented context fails here rather than in production.
    """

    class RecordingContext:
        def __init__(self) -> None:
            self.cli_commands: list[dict] = []
            self.tools: list[str] = []
            self.hooks: list[str] = []
            self.locales: list[str] = []
            self.settings: dict = {}
            self.plugin_name = "reset-rescue"
            self.plugin_dir = REPO

        # documented registration surface
        def register_cli_command(self, *args: Any, **kwargs: Any) -> None:
            self.cli_commands.append({"args": args, "kwargs": kwargs})

        def register_tool(self, *args: Any, **kwargs: Any) -> None:
            self.tools.append(str(args[0] if args else kwargs.get("name")))

        def register_hook(self, *args: Any, **kwargs: Any) -> None:
            self.hooks.append(str(args[0] if args else kwargs.get("name")))

        def register_locale(self, *args: Any, **kwargs: Any) -> None:
            self.locales.append(str(args[0] if args else kwargs.get("lang")))

        def get_setting(self, name: str, default: Any = None) -> Any:
            return self.settings.get(name, default)

        def log(self, *args: Any, **kwargs: Any) -> None:
            return None

    return RecordingContext()


class HarnessSelfTest(unittest.TestCase):
    """The harness must be trustworthy before anything relies on it."""

    def test_repo_layout_is_what_the_harness_copies(self):
        self.assertTrue(
            (REPO / "__init__.py").is_file(),
            "the repository root is the plugin: __init__.py must exist",
        )
        self.assertTrue((REPO / "reset_rescue").is_dir())

    def test_stage_plugin_copies_only_the_runtime_entries(self):
        home = temp_home("t36-stage-")
        try:
            staged = stage_plugin(home)
            self.assertTrue((staged / "__init__.py").is_file())
            self.assertTrue((staged / "reset_rescue").is_dir())
            self.assertFalse((staged / "docs").exists(), "docs must not be copied")
            self.assertFalse((staged / "tests").exists(), "tests must not be copied")
            self.assertFalse((staged / "__pycache__").exists())
            for forbidden in FORBIDDEN_IN_COPY:
                self.assertFalse((staged / forbidden).exists())
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_temp_home_lives_outside_the_real_profile(self):
        """The home must be outside ``~/.hermes`` and resolve to itself (R17)."""
        from tests.support import isolation

        home = temp_home("t36-scratch-")
        try:
            self.assertFalse(isolation.is_inside_native_home(home))
            self.assertEqual(isolation.resolved_root(home), home)
            self.assertTrue((home / "plugin-data" / "reset-rescue").is_dir())
            self.assertTrue((home / "cron").is_dir())
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_home_env_restores_the_previous_value(self):
        home = temp_home("t36-env-")
        before = os.environ.get("HERMES_HOME")
        try:
            with HomeEnv(home) as active:
                self.assertEqual(active, home)
                self.assertEqual(os.environ["HERMES_HOME"], str(home))
            self.assertEqual(os.environ.get("HERMES_HOME"), before)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_home_env_restores_even_when_the_block_raises(self):
        home = temp_home("t36-env-raise-")
        before = os.environ.get("HERMES_HOME")
        try:
            with self.assertRaises(RuntimeError):
                with HomeEnv(home):
                    raise RuntimeError("boom")
            self.assertEqual(os.environ.get("HERMES_HOME"), before)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_fake_context_records_and_refuses_anything_else(self):
        ctx = fake_context()
        ctx.register_cli_command("reset-rescue", help="x")
        self.assertEqual(len(ctx.cli_commands), 1)
        with self.assertRaises(AttributeError):
            ctx.register_dashboard_route("/x")  # not a documented ctx method

    def test_manifest_presence_is_reported_honestly(self):
        # Not an assertion about L3's progress: whichever way it goes, the
        # harness must answer a boolean, not raise.
        self.assertIsInstance(has_manifest(), bool)


if __name__ == "__main__":
    unittest.main()
