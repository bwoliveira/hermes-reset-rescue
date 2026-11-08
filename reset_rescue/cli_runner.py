"""The one place Reset Rescue starts a subprocess (architecture A§4.8, T-12).

Every Hermes interaction the plugin performs outside its own process goes
through this module: ``hermes cron create/list/remove``, ``hermes send`` and
``hermes auth refresh``. Keeping it in one module is what makes the safety
properties checkable:

* **argv only, never a shell.** ``argv`` must be a sequence of strings; a
  single string raises :class:`TypeError` before anything runs, so
  ``shell=True`` is structurally impossible.
* **no approval bypass.** ``--yolo``, ``--accept-hooks`` and the
  ``HERMES_ACCEPT_HOOKS`` / ``HERMES_YOLO_MODE`` environment variables are
  refused (rule 12). A run that would need a prompt fails instead of
  prompting: ``stdin`` is ``DEVNULL``.
* **bounded.** Every call has a timeout; the child is killed when it expires
  and the caller gets ``timed_out=True`` instead of a hung cron run.
* **pinned home.** ``HERMES_HOME`` is set to the active profile for the child,
  so a cron subprocess can never act on a different profile than the one that
  scheduled it.
* **no secrets added.** The child inherits the process environment minus the
  approval-bypass variables; the plugin never injects a token, a chat id or a
  config value into a child's environment.

``CliResult`` is defined in :mod:`reset_rescue.clock` (the bottom layer) so a
fake and the real runner return the same value object.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from .clock import CliResult

__all__ = [
    "CliRunner",
    "CliRunnerError",
    "ForbiddenArgv",
    "DEFAULT_TIMEOUT_S",
    "CRON_TIMEOUT_S",
    "SEND_TIMEOUT_S",
    "AUTH_TIMEOUT_S",
    "FORBIDDEN_FLAGS",
    "FORBIDDEN_ENV",
    "STDOUT_LIMIT",
    "CRON_LIST_LIMIT",
    "TRUNCATION_MARKER",
    "resolve_launcher",
    "scrub_env",
    "truncate",
]

#: Per-call timeouts (A§4.8): cron 60 s, send 30 s, auth refresh 60 s.
DEFAULT_TIMEOUT_S = 60.0
CRON_TIMEOUT_S = 60.0
SEND_TIMEOUT_S = 30.0
AUTH_TIMEOUT_S = 60.0

#: Flags that would bypass an approval gate. Never passed, never accepted.
FORBIDDEN_FLAGS = ("--yolo", "--accept-hooks", "--no-approval", "--dangerously-skip-permissions")

#: Environment variables that carry an approval bypass into a child process.
FORBIDDEN_ENV = ("HERMES_ACCEPT_HOOKS", "HERMES_YOLO_MODE", "HERMES_YOLO")

#: Captured output kept per stream; the rest is dropped with a marker so a
#: provider body can never be persisted through a log line (I8).
STDOUT_LIMIT = 4000

#: The ``cron list`` document is parsed whole by :mod:`reset_rescue.cron_gateway`
#: (one block per job), so it gets its own, much larger bound: truncating it
#: would hide the plugin's own jobs behind unrelated ones (R8). It is still
#: bounded, and the marker below is what tells the gateway the view is partial.
CRON_LIST_LIMIT = 8 * 1024 * 1024

#: Appended by :func:`truncate`; the gateway refuses a listing that carries it.
TRUNCATION_MARKER = "…[truncated "


class CliRunnerError(RuntimeError):
    """A fixed-code failure of the runner itself (never carries child text)."""


class ForbiddenArgv(ValueError):
    """An argv that would bypass an approval gate was refused before running."""


def truncate(text: str, limit: int = STDOUT_LIMIT) -> str:
    """Cap captured output, keeping the head (where the parsed line lives)."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated {len(text) - limit} chars]"


def scrub_env(
    base: Optional[Mapping[str, str]] = None,
    *,
    hermes_home: Optional[os.PathLike[str] | str] = None,
) -> dict[str, str]:
    """Child environment: inherited, minus approval bypasses, home pinned.

    ``HERMES_HOME`` is always set when a home is known: a cron child that
    inherited the gateway's environment could otherwise resolve a different
    profile (plugin-dev core rule 4).
    """
    env = dict(os.environ if base is None else base)
    for name in FORBIDDEN_ENV:
        env.pop(name, None)
    if hermes_home is not None:
        env["HERMES_HOME"] = str(hermes_home)
    return env


def resolve_launcher(
    *, explicit: Optional[str] = None, recorded: Optional[str] = None
) -> Optional[str]:
    """Resolve the ``hermes`` launcher for one operation (A§4.8).

    Order: an explicit path (tests, or a caller that already resolved it), then
    ``shutil.which("hermes")``, then the path recorded in ``meta.hermes_launcher``
    at the last successful ``ensure``. The recorded path is what makes a cron
    child work when the gateway's PATH does not contain the launcher.

    Only **absolute** paths are accepted. ``~`` is deliberately not expanded
    here: a plugin that resolves a home directory itself is one step away from
    reading the wrong profile (I8), and every launcher ``shutil.which`` returns
    is already absolute.
    """
    for candidate in (explicit, recorded):
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_absolute() and path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return shutil.which("hermes")


class CliRunner:
    """Run ``hermes`` with argv lists, a timeout and a pinned ``HERMES_HOME``.

    ``runner`` is the injection point for tests: it receives
    ``(argv, env, timeout)`` and returns an object with ``returncode``,
    ``stdout`` and ``stderr`` (``subprocess.CompletedProcess`` shape), or
    raises ``subprocess.TimeoutExpired``. Production never overrides it.
    """

    def __init__(
        self,
        *,
        launcher: Optional[str] = None,
        recorded_launcher: Optional[str] = None,
        hermes_home: Optional[os.PathLike[str] | str] = None,
        env: Optional[Mapping[str, str]] = None,
        runner=None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._explicit = launcher
        self._recorded = recorded_launcher
        self.hermes_home = Path(hermes_home) if hermes_home is not None else None
        self._base_env = dict(env) if env is not None else None
        self._runner = runner
        self.timeout = float(timeout)
        self._resolved: Optional[str] = None
        self._resolved_once = False

    # -- launcher ----------------------------------------------------------

    def launcher(self) -> Optional[str]:
        """The resolved launcher, resolved at most once per instance."""
        if not self._resolved_once:
            self._resolved = resolve_launcher(explicit=self._explicit, recorded=self._recorded)
            self._resolved_once = True
        return self._resolved

    def refresh_launcher(self) -> Optional[str]:
        """Forget the cached resolution (after ``ensure`` records a new path)."""
        self._resolved = None
        self._resolved_once = False
        return self.launcher()

    # -- execution ---------------------------------------------------------

    def run(self, argv: Sequence[str], *, timeout: Optional[float] = None) -> CliResult:
        """Run ``hermes <argv>``; never raises for a child failure."""
        if isinstance(argv, (str, bytes)):
            raise TypeError("CliRunner.run takes an argv sequence, never a command string")
        parts = [str(part) for part in argv]
        if not parts:
            raise ValueError("CliRunner.run needs at least one argument")
        for part in parts:
            if part in FORBIDDEN_FLAGS:
                raise ForbiddenArgv(f"refusing to pass {part}: approval bypasses are never used")

        launcher = self.launcher()
        if not launcher:
            # No launcher is a clean, fixed failure: the caller turns it into a
            # problem code, never a traceback in a cron run.
            return CliResult(argv=tuple(parts), returncode=None, exec_error="launcher_not_found")

        full = [launcher, *parts]
        env = scrub_env(self._base_env, hermes_home=self.hermes_home)
        limit = float(self.timeout if timeout is None else timeout)
        # `cron list` is a whole document the gateway parses block by block, so
        # it gets the document bound; everything else keeps the small log bound.
        chars = CRON_LIST_LIMIT if parts[:2] == ["cron", "list"] else STDOUT_LIMIT
        try:
            completed = (self._runner or self._default_runner)(full, env, limit)
        except subprocess.TimeoutExpired as exc:
            return CliResult(
                argv=tuple(full),
                timed_out=True,
                stdout=truncate(_text(getattr(exc, "stdout", None)), chars),
                stderr=truncate(_text(getattr(exc, "stderr", None)), chars),
            )
        except OSError:
            # A fixed category only: the OSError message can carry a path.
            return CliResult(argv=tuple(full), exec_error="spawn_failed")
        return CliResult(
            argv=tuple(full),
            returncode=getattr(completed, "returncode", None),
            stdout=truncate(_text(getattr(completed, "stdout", None)), chars),
            stderr=truncate(_text(getattr(completed, "stderr", None)), chars),
        )

    def _default_runner(self, argv: list[str], env: dict[str, str], timeout: float):
        """The only real subprocess in the plugin.

        ``shell=False`` is the default and is stated explicitly; ``stdin`` is
        ``DEVNULL`` so a command that would prompt fails instead of hanging a
        cron run (rule 12).
        """
        return subprocess.run(
            argv,
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
            check=False,
        )

    # -- convenience -------------------------------------------------------

    def cron(self, *args: str, timeout: float = CRON_TIMEOUT_S) -> CliResult:
        return self.run(["cron", *args], timeout=timeout)

    def send(self, *args: str, timeout: float = SEND_TIMEOUT_S) -> CliResult:
        return self.run(["send", *args], timeout=timeout)

    def auth_refresh(
        self, provider: str, target: str, *, timeout: float = AUTH_TIMEOUT_S
    ) -> CliResult:
        return self.run(["auth", "refresh", provider, target], timeout=timeout)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"CliRunner(launcher={self.launcher()!r}, home={self.hermes_home!r})"


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def iter_forbidden(argv: Iterable[str]) -> list[str]:
    """Return the approval-bypass flags present in ``argv`` (tests and guards)."""
    return [part for part in argv if part in FORBIDDEN_FLAGS]
