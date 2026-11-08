#!/usr/bin/env python3
"""Run the offline Python suites of Reset Rescue under the Hermes runtime.

Usage (through the runtime wrapper, never with a bare interpreter):

    python3 <hermes-agent-local>/scripts/run_runtime_script.py \
        <repo>/scripts/run_tests.py [suite ...]

Suites: ``engine``, ``flows``, ``host``, ``api``, ``integration`` (directories
under ``tests/``). With no argument every suite that exists runs.

The offline guard (``scripts/offline_guard.py``) is installed **before** the
first test module is imported, so a test that opens a socket, starts an
unexpected process or writes into the real Hermes profile fails with
``OfflineViolation`` instead of passing on a machine that happens to have
network access.

Exit codes: 0 every suite passed, 1 a test failed, 2 the runtime is unavailable
or no suite was found, 3 the guard was violated outside a test (import-time side effect).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SUITES = ("engine", "flows", "host", "api", "integration")


def _bootstrap_path() -> None:
    """Make ``reset_rescue``, ``tests.fakes`` and the suite modules importable."""
    for entry in (REPO, REPO / "tests"):
        text = str(entry)
        if text not in sys.path:
            sys.path.insert(0, text)


def _install_guard() -> None:
    scripts_dir = str(REPO / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import offline_guard  # noqa: PLC0415 - must import after sys.path is ready

    offline_guard.install(
        real_home=os.environ.get("HERMES_HOME") or "~/.hermes",
        allowed_roots=(str(REPO),),
    )


def _suite_dirs(names: list[str]) -> list[Path]:
    found: list[Path] = []
    for name in names:
        directory = REPO / "tests" / name
        if directory.is_dir():
            found.append(directory)
    return found


def _run_suite(directory: Path) -> tuple[unittest.result.TestResult, float, str]:
    text = str(directory)
    if text not in sys.path:
        sys.path.insert(0, text)
    loader = unittest.TestLoader()
    # Keep file order for deterministic output; unittest's default sorts by name.
    loader.sortTestMethodsUsing = None  # type: ignore[assignment]
    started = time.monotonic()
    suite = loader.discover(start_dir=text, top_level_dir=text, pattern="test_*.py")
    runner = unittest.TextTestRunner(stream=sys.stdout, verbosity=2, buffer=False)
    result = runner.run(suite)
    return result, time.monotonic() - started, directory.name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("suites", nargs="*", help=f"subset of {', '.join(SUITES)}")
    parser.add_argument("--list", action="store_true", help="list the suites that exist and exit")
    parser.add_argument("--check-runtime", action="store_true", help=argparse.SUPPRESS)
    options = parser.parse_args(argv)

    requested = options.suites or list(SUITES)
    unknown = [name for name in requested if name not in SUITES]
    if unknown:
        print(
            f"unknown suite(s): {', '.join(unknown)}; known: {', '.join(SUITES)}", file=sys.stderr
        )
        return 2

    directories = _suite_dirs(requested)
    if options.list:
        for directory in directories:
            print(directory.name)
        return 0
    if not directories:
        print(
            "no test suite found under tests/ (engine, flows, host, api, integration)",
            file=sys.stderr,
        )
        return 2

    instruction = (
        "Reset Rescue tests require the Hermes runtime; run: "
        "RUNTIME_WRAPPER=/path/to/run_runtime_script.py bash scripts/check.sh"
    )
    wrapper = os.environ.get("RUNTIME_WRAPPER")
    if not wrapper or not Path(wrapper).is_file():
        print(instruction, file=sys.stderr)
        return 2
    try:
        import hermes_cli  # noqa: F401, PLC0415 - provided by the Hermes runtime only
    except ImportError:
        print(instruction, file=sys.stderr)
        return 2
    if options.check_runtime:
        return 0

    _bootstrap_path()
    _install_guard()

    print(f"reset-rescue offline tests: {', '.join(d.name for d in directories)}")
    print(f"runtime: {sys.executable}")
    print(
        f"guard: sockets blocked, subprocesses allow-listed, real profile write-protected "
        f"({os.environ.get('HERMES_HOME') or '~/.hermes'})"
    )
    print()

    failures = 0
    errors = 0
    skipped = 0
    total = 0
    for directory in directories:
        print(f"== tests/{directory.name} " + "=" * max(0, 60 - len(directory.name)))
        try:
            result, elapsed, _ = _run_suite(directory)
        except Exception:  # import-time crash: report and keep the other suites running
            traceback.print_exc()
            errors += 1
            print(f"-- tests/{directory.name}: IMPORT ERROR\n")
            continue
        total += result.testsRun
        failures += len(result.failures)
        errors += len(result.errors)
        skipped += len(result.skipped)
        status = "ok" if result.wasSuccessful() else "FAIL"
        print(f"-- tests/{directory.name}: {status} ({result.testsRun} tests, {elapsed:.1f}s)\n")

    print("=" * 62)
    print(f"total: {total} tests, {failures} failure(s), {errors} error(s), {skipped} skipped")
    if failures or errors:
        print("RESULT: FAIL")
        return 1
    print("RESULT: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
