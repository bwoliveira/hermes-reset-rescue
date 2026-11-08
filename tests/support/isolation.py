"""One guard for every temporary Hermes profile this repo builds.

Why this exists (R17)
---------------------
Hermes does **not** treat every ``HERMES_HOME`` as a profile of its own.
``hermes_constants._hermes_root_of`` checks whether the value resolves *under*
the native home (``~/.hermes``) and, when it does, returns the native home: a
path nested inside the real profile **is** the real profile. That is deliberate
— profiles live at ``<root>/profiles/<name>`` — but it means a test that does

    HERMES_HOME=~/.hermes/cache/scratch/<lane>/home

is not testing a temp profile at all. It enables plugins in the real profile,
reads the real ``installs/<key>/`` dependency state, writes the real
``plugin-data/``, and passes or fails depending on what that machine happens to
have. The Reset Rescue e2e did exactly this until R17: it passed here (the real
install was current) and failed in the reviewer's fresh isolation at the first
``plugins enable``.

The rule
--------
Every temporary profile is created **outside** ``~/.hermes`` and is checked
against Hermes' own resolver before anything is written into it. A home that
resolves to something other than itself aborts the run — never a warning, never
a fallback — because continuing means touching the real profile.

``temp_home()`` is the only supported way to build one. ``assert_isolated()``
is the guard; ``resolved_root()`` is the resolver, run in a child process so it
reflects a *clean* import of ``hermes_constants`` rather than whatever this
process has already imported.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterator, Optional

#: Hermes' own install. Read-only; the resolver is imported from here.
HERMES_INSTALL = Path("/usr/local/lib/hermes-agent")

#: The native profile. A temp home must never resolve to this, or into it.
NATIVE_HOME = Path.home() / ".hermes"

#: Where temp profiles go: a real filesystem outside ``$HOME``. ``/var/tmp``
#: survives reboots and is not a tmpfs, so a seeded dependency environment
#: (~900 MB) is not competing with RAM.
#:
#: ``RR_TEMP_ROOT`` overrides it, and the override is checked: a value inside the
#: real profile is refused rather than honoured. ``scripts/release_gate.sh``
#: sets it to its own writable scratch, because it bind-mounts the real profile
#: (and therefore ``/var/tmp``) read-only.
#:
#: **Never trust ``TMPDIR``.** In this environment ``TMPDIR`` is
#: ``<HERMES_HOME>/cache/scratch`` — inside the real profile — so
#: ``tempfile.mkdtemp()``, ``mktemp`` and Node's ``os.tmpdir()`` all land there
#: and collapse to ``~/.hermes``. That is the same defect as a nested
#: ``HERMES_HOME``, one layer down, and it is why a read-only real profile turned
#: every scratch write into ``EROFS`` instead of a silent write into the real one.
TEMP_ROOT = Path(os.environ.get("RR_TEMP_ROOT") or "/var/tmp")


class IsolationError(RuntimeError):
    """A temporary profile would have been the real one. The run must stop."""


@contextlib.contextmanager
def _interpreter_allowed() -> Iterator[None]:
    """Let the resolver's own ``python`` child start under the offline guard.

    The guard refuses unexpected processes, and the resolver *must* be a child:
    this process may already have imported ``hermes_constants`` with a different
    ``HERMES_HOME``, and the resolver memoises. The allowance is scoped to this
    one call and to the exact interpreter, so it cannot become a hole a test
    reuses.
    """
    try:
        import offline_guard  # noqa: PLC0415 - optional: absent outside the suites
    except ImportError:
        yield
        return
    name = Path(sys.executable).name
    with offline_guard.allow_subprocess(name, "python3", "python"):
        yield


def resolved_root(home: Path | str) -> Path:
    """What Hermes resolves ``HERMES_HOME=home`` to, imported in a clean child.

    A child process is the only honest way to ask: this process may have
    imported ``hermes_constants`` before ``HERMES_HOME`` changed, and the
    resolver memoises. ``-I`` drops ``PYTHONPATH`` so the child sees the
    installed Hermes, not a lane's tree.
    """
    code = (
        f"import sys; sys.path.insert(0, {str(HERMES_INSTALL)!r});"
        "from hermes_constants import get_default_hermes_root;"
        "print(get_default_hermes_root())"
    )
    env = {k: v for k, v in os.environ.items() if k not in {"HERMES_HOME", "PYTHONPATH"}}
    env["HERMES_HOME"] = str(home)
    with _interpreter_allowed():
        done = subprocess.run(
            [sys.executable, "-I", "-c", code],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
            check=False,
        )
    if done.returncode != 0:
        raise IsolationError(f"cannot resolve HERMES_HOME={home}: {done.stderr.strip()}")
    return Path(done.stdout.strip())


def is_inside_native_home(home: Path | str) -> bool:
    """True when *home* is the native profile or a path under it."""
    try:
        return Path(home).resolve().is_relative_to(NATIVE_HOME.resolve())
    except (OSError, ValueError):
        return False


def assert_tempdir_outside(home: Path | str | None = None) -> Path:
    """Refuse a ``TMPDIR`` under the real profile; return the effective temp root.

    ``tempfile.mkdtemp()``, ``mktemp`` and Node's ``os.tmpdir()`` all read
    ``TMPDIR``. When it points inside ``~/.hermes`` — as it does in this
    environment — every scratch directory they create is inside the real profile,
    and a test that only checks its ``HERMES_HOME`` still writes real state (or,
    against a read-only profile, fails with ``EROFS`` for the wrong reason).
    """
    raw = os.environ.get("TMPDIR") or tempfile.gettempdir()
    path = Path(raw)
    if is_inside_native_home(path):
        raise IsolationError(
            f"TMPDIR={path} is inside the real profile {NATIVE_HOME}; every "
            f"tempfile.mkdtemp()/mktemp/os.tmpdir() would land in real state. "
            f"Export TMPDIR to a directory outside $HOME (scripts/check.sh does "
            f"this for every child step)."
        )
    return path


def assert_isolated(home: Path | str) -> Path:
    """Refuse a temp home that Hermes would resolve to the real profile.

    Returns the resolved root so a caller can assert against it. Raises
    :class:`IsolationError` — the run must abort, because every later write
    would land in the real profile.
    """
    path = Path(home)
    if is_inside_native_home(path):
        raise IsolationError(
            f"{path} is inside the real profile {NATIVE_HOME}; Hermes resolves it to "
            f"{NATIVE_HOME} and the run would touch real state. Build temp profiles "
            f"outside $HOME (see tests/support/isolation.py)."
        )
    root = resolved_root(path)
    if root.resolve() != path.resolve():
        raise IsolationError(
            f"HERMES_HOME={path} resolves to {root}, not to itself; the run would "
            "touch a profile it did not create."
        )
    return root


def scratch_root() -> Path:
    """The scratch directory for temp state: outside the real profile, verified.

    ``RR_SCRATCH`` names it outright; otherwise it is ``<TEMP_ROOT>/rr-scratch``.
    Both are checked, so an override cannot quietly point back into ``~/.hermes``.
    """
    raw = os.environ.get("RR_SCRATCH")
    path = Path(raw) if raw else TEMP_ROOT / "rr-scratch"
    if is_inside_native_home(path):
        raise IsolationError(
            f"scratch root {path} is inside the real profile {NATIVE_HOME}; "
            f"set RR_SCRATCH to a directory outside $HOME"
        )
    path.mkdir(parents=True, exist_ok=True)
    return path


def temp_home(prefix: str = "rr-home-", *, root: Optional[Path] = None) -> Path:
    """Create a fresh, verified temp profile and return it.

    The directory is created under :data:`TEMP_ROOT` (outside ``$HOME``), then
    checked with :func:`assert_isolated` before the caller can write to it. A
    failure removes the directory and raises: a half-built profile that resolves
    to the real one must not be handed out.

    ``TMPDIR`` is deliberately **not** used as the base: it points inside the
    real profile here, so ``tempfile.mkdtemp()`` alone would be the bug this
    guard exists to prevent.
    """
    base = Path(root) if root is not None else TEMP_ROOT
    if is_inside_native_home(base):
        raise IsolationError(
            f"temp root {base} is inside the real profile {NATIVE_HOME}; pass a "
            f"directory outside $HOME"
        )
    base.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix=prefix, dir=str(base)))
    try:
        assert_isolated(home)
    except IsolationError:
        shutil.rmtree(home, ignore_errors=True)
        raise
    return home


def env_for(home: Path, **extra: str) -> dict[str, str]:
    """A child environment for *home*, verified before it is built.

    ``PYTHONPATH`` is dropped: the launcher must resolve the installed Hermes,
    not whatever a lane's shell exported.
    """
    assert_isolated(home)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["HERMES_HOME"] = str(home)
    env.update(extra)
    return env
