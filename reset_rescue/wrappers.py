"""Wrapper files in ``<HERMES_HOME>/scripts/`` (architecture A§7.2, T-14).

A native ``--no-agent --script`` cron job runs ``python <script>`` with **no
arguments**, and the script must resolve inside ``<HERMES_HOME>/scripts/``
(``cron/scheduler_script.py``). The plugin's real work lives in the plugin, so
each role gets a ~25-line stdlib-only wrapper that calls ``hermes reset-rescue
<role>`` and passes the exit code through (the I6 contract: stdout is the
notice channel, the exit code is the failure signal).

Why the file is written this way:

* **The role is a constant in the file**, never derived from the clock or from
  argv (I3). A late backup run can never consume the primary's attempt.
* **``HERMES_HOME`` is derived from the file's own location**
  (``<home>/scripts/<file>`` → ``<home>``), so a wrapper copied to another
  profile works there, and a cron child never inherits the wrong profile.
* **``stdin=DEVNULL``**: a command that would prompt fails instead of hanging
  a cron run (rule 12).
* **No gateway-lifecycle command anywhere in the text**: the cron create-time
  guard (``cron/lifecycle_guard.py``) scans the script and would refuse the
  job (spike S4 proves the accepted template).
* **Ownership is provable**: the header carries a marker, the template
  version and the sha256 of the body. A file with no header, or with a body
  that does not hash to the header's value, is **not ours** and is never
  overwritten or deleted (T-37).
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .version import WRAPPER_TEMPLATE_VERSION

__all__ = [
    "WRAPPER_FILES",
    "ROLE_FOR_FILE",
    "WrapperError",
    "WrapperState",
    "WrapperStatus",
    "render_wrapper",
    "parse_header",
    "body_hash",
    "is_ours",
    "install_wrappers",
    "verify_wrappers",
    "remove_owned_wrappers",
    "wrapper_path",
    "SCRIPTS_DIRNAME",
]

#: ``<HERMES_HOME>/scripts`` — the only directory cron will run a script from.
SCRIPTS_DIRNAME = "scripts"

#: The files this plugin owns, by role (simplification §3).
#:
#: Five roles: one discovery scan, two rescue slots, the reminder fire and the
#: D-009 quota trigger. The rehearsal world was cut (§6), so its wrapper is
#: gone — and because ``remove_owned_wrappers`` only deletes files listed
#: here, an old rehearsal wrapper from a previous version is simply left for
#: the user to delete rather than silently removed by a file we no longer
#: describe.
WRAPPER_FILES: Mapping[str, str] = {
    "scan": "reset-rescue-scan.py",
    "primary": "reset-rescue-fire.py",
    "backup": "reset-rescue-fire-backup.py",
    "remind": "reset-rescue-remind.py",
    "quota": "reset-rescue-quota.py",
}

ROLE_FOR_FILE: Mapping[str, str] = {name: role for role, name in WRAPPER_FILES.items()}

#: CLI argv per role. The wrapper never decides anything else.
_CLI_ARGS: Mapping[str, tuple[str, ...]] = {
    "scan": ("scan",),
    "primary": ("fire", "--role", "primary"),
    "backup": ("fire", "--role", "backup"),
    "remind": ("fire", "--role", "remind"),
    "quota": ("quota-check",),
}

_HEADER = re.compile(
    r"^# managed-by: reset-rescue; template: (?P<template>\d+); role: (?P<role>[a-z]+); "
    r"sha256: (?P<sha>[0-9a-f]{64})$",
    re.MULTILINE,
)
_HEADER_LINE = re.compile(r"^# managed-by: reset-rescue;.*$", re.MULTILINE)

#: File mode: owner rwx only. A cron script needs no group/world access.
FILE_MODE = 0o700


class WrapperError(RuntimeError):
    """A fixed-code wrapper failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class WrapperStatus:
    """One file's state, as the popup and ``/status`` report it."""

    name: str
    role: str
    state: str  # ok | missing | stale | conflict | foreign
    template_version: Optional[int] = None

    @property
    def usable(self) -> bool:
        return self.state in {"ok", "stale"}


class WrapperState(dict):
    """``{file name: state}`` plus the ordered statuses (a dict for the DTO)."""

    def __init__(self, statuses: Iterable[WrapperStatus] = ()) -> None:
        super().__init__({status.name: status.state for status in statuses})
        self.statuses = list(statuses)

    @property
    def conflicts(self) -> list[str]:
        return [status.name for status in self.statuses if status.state in {"conflict", "foreign"}]

    @property
    def all_usable(self) -> bool:
        return all(status.usable for status in self.statuses)


def _body(role: str, launcher: str) -> str:
    """The executable part of a wrapper, without the header line.

    Rendered as text (not a template format string) so a brace in the launcher
    path can never break the file.
    """
    args = _CLI_ARGS[role]
    argv = ", ".join(f'"{part}"' for part in ("reset-rescue", *args))
    return (
        '"""Reset Rescue wrapper — runs one role of the plugin engine.\n'
        "\n"
        "Safe to delete: `hermes reset-rescue uninstall` removes it. Do not edit:\n"
        "the plugin verifies this file's hash and will never overwrite a file it\n"
        "cannot prove it wrote.\n"
        '"""\n'
        "import os\n"
        "import shutil\n"
        "import subprocess\n"
        "import sys\n"
        "from pathlib import Path\n"
        "\n"
        "# <HERMES_HOME>/scripts/<this file> -> <HERMES_HOME>\n"
        "HOME = Path(__file__).resolve().parent.parent\n"
        f"LAUNCHER = {launcher!r}\n"
        "\n"
        "\n"
        "def main():\n"
        '    exe = shutil.which("hermes") or LAUNCHER\n'
        "    if not exe or not os.path.exists(exe):\n"
        '        sys.stderr.write("Reset Rescue: hermes launcher not found\\n")\n'
        "        return 1\n"
        "    env = dict(os.environ, HERMES_HOME=str(HOME))\n"
        f"    argv = [exe, {argv}]\n"
        "    try:\n"
        "        proc = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL)\n"
        "    except OSError:\n"
        '        sys.stderr.write("Reset Rescue: could not start hermes\\n")\n'
        "        return 1\n"
        "    return proc.returncode\n"
        "\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    sys.exit(main())\n"
    )


def body_hash(text: str) -> str:
    """sha256 of the file's body (everything after the header line)."""
    match = _HEADER_LINE.search(text)
    if match is None:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    body = text[match.end() :]
    body = body.lstrip("\n")
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def render_wrapper(
    role: str, launcher: str, *, template_version: int = WRAPPER_TEMPLATE_VERSION
) -> str:
    """Full file text for one role: header line + body."""
    if role not in WRAPPER_FILES:
        raise WrapperError("unknown_role")
    body = _body(role, launcher)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    header = (
        f"# managed-by: reset-rescue; template: {int(template_version)}; "
        f"role: {role}; sha256: {digest}"
    )
    return f"{header}\n{body}"


def parse_header(text: str) -> Optional[dict[str, Any]]:
    """``{template, role, sha256}`` from the first header line, else ``None``."""
    match = _HEADER.search(text or "")
    if match is None:
        return None
    return {
        "template": int(match.group("template")),
        "role": match.group("role"),
        "sha256": match.group("sha"),
    }


def is_ours(text: str, role: str) -> bool:
    """True only when the header is present, the role matches and the body hashes.

    A user-edited file fails the hash check and is therefore *not* ours: the
    plugin leaves it alone (T-37). A file whose header claims another role is
    also not ours — that is a copy-paste, not our file.
    """
    header = parse_header(text)
    if header is None:
        return False
    if header["role"] != role:
        return False
    return header["sha256"] == body_hash(text)


def wrapper_path(home: os.PathLike[str] | str, role: str) -> Path:
    return Path(home) / SCRIPTS_DIRNAME / WRAPPER_FILES[role]


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return None


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, fsync, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, FILE_MODE)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def verify_wrappers(
    home: os.PathLike[str] | str, launcher: Optional[str] = None
) -> list[WrapperStatus]:
    """Read-only state of every wrapper file (no writes, safe from ``/status``)."""
    statuses: list[WrapperStatus] = []
    for role, name in WRAPPER_FILES.items():
        path = Path(home) / SCRIPTS_DIRNAME / name
        text = _read(path)
        if text is None:
            statuses.append(WrapperStatus(name=name, role=role, state="missing"))
            continue
        header = parse_header(text)
        if header is None:
            statuses.append(WrapperStatus(name=name, role=role, state="foreign"))
            continue
        if header["role"] != role or not is_ours(text, role):
            statuses.append(
                WrapperStatus(
                    name=name, role=role, state="conflict", template_version=int(header["template"])
                )
            )
            continue
        version = int(header["template"])
        state = "ok" if version >= WRAPPER_TEMPLATE_VERSION else "stale"
        statuses.append(WrapperStatus(name=name, role=role, state=state, template_version=version))
    return statuses


def install_wrappers(
    home: os.PathLike[str] | str, launcher: str, *, roles: Optional[Iterable[str]] = None
) -> WrapperState:
    """Install or refresh the wrappers we own; never touch a file we do not.

    * absent → write
    * ours, same template → nothing
    * ours, older template → atomic replace
    * not ours (no header, wrong role, hash mismatch) → ``conflict``/``foreign``
      and left untouched, so a user's edit survives an ``ensure`` (T-37)
    """
    wanted = tuple(roles) if roles is not None else tuple(WRAPPER_FILES)
    statuses: list[WrapperStatus] = []
    for role in wanted:
        if role not in WRAPPER_FILES:
            raise WrapperError("unknown_role")
        name = WRAPPER_FILES[role]
        path = Path(home) / SCRIPTS_DIRNAME / name
        text = _read(path)
        if text is None:
            _atomic_write(path, render_wrapper(role, launcher))
            statuses.append(
                WrapperStatus(
                    name=name, role=role, state="ok", template_version=WRAPPER_TEMPLATE_VERSION
                )
            )
            continue
        header = parse_header(text)
        if header is None:
            statuses.append(WrapperStatus(name=name, role=role, state="foreign"))
            continue
        if header["role"] != role or not is_ours(text, role):
            statuses.append(
                WrapperStatus(
                    name=name, role=role, state="conflict", template_version=int(header["template"])
                )
            )
            continue
        version = int(header["template"])
        if version < WRAPPER_TEMPLATE_VERSION:
            _atomic_write(path, render_wrapper(role, launcher))
            statuses.append(
                WrapperStatus(
                    name=name, role=role, state="ok", template_version=WRAPPER_TEMPLATE_VERSION
                )
            )
        else:
            statuses.append(
                WrapperStatus(name=name, role=role, state="ok", template_version=version)
            )
    return WrapperState(statuses)


def remove_owned_wrappers(home: os.PathLike[str] | str) -> list[str]:
    """Delete every wrapper file that is provably ours; return the names removed.

    A file whose header or hash does not match is kept and reported by the
    caller: uninstall removes what the plugin wrote, nothing else (T-37).
    """
    removed: list[str] = []
    for role, name in WRAPPER_FILES.items():
        path = Path(home) / SCRIPTS_DIRNAME / name
        text = _read(path)
        if text is None or not is_ours(text, role):
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed.append(name)
    return removed
