#!/usr/bin/env python3
"""Fail closed before every real CLI launch, including recorded absolute paths."""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

LIVE_HERMES_HOME = Path(os.environ.get("RR_LIVE_HERMES_HOME") or Path.home() / ".hermes")
HERMES_INSTALL = Path(os.environ.get("RR_HERMES_INSTALL") or "/usr/local/lib/hermes-agent")


def main() -> None:
    sys.path.insert(0, str(HERMES_INSTALL))
    from hermes_constants import get_default_hermes_root, get_hermes_home

    root = Path(os.environ["RR_TICKER_ROOT"]).resolve()
    if not root.is_relative_to(Path("/var/tmp")):
        raise SystemExit("ticker CLI: scratch escaped /var/tmp")
    home = get_hermes_home().resolve()
    resolved_root = get_default_hermes_root().resolve()
    for path in (home, resolved_root, Path(os.environ["TMPDIR"]).resolve()):
        if not path.is_relative_to(root):
            raise SystemExit(f"ticker CLI: unsafe resolved home/root/tmp: {path}")
    if home != resolved_root:
        raise SystemExit("ticker CLI: nested profile resolution is forbidden")
    if os.environ.get("HERMES_DISABLE_LAZY_INSTALLS") != "1":
        raise SystemExit("ticker CLI: lazy installs must be disabled")
    for path in (LIVE_HERMES_HOME, HERMES_INSTALL):
        if not os.statvfs(path).f_flag & os.ST_RDONLY:
            raise SystemExit(f"ticker CLI: protected mount is writable: {path}")
    if {name for _, name in socket.if_nameindex()} != {"lo"}:
        raise SystemExit("ticker CLI: non-loopback network interface present")
    if sys.argv[1:] == ["--check-only"]:
        print(f"verified home before any Hermes call: {home}", flush=True)
        return
    with Path(os.environ["RR_TICKER_CLI_LOG"]).open("a") as log:
        log.write(f"verified resolved home: {home}\n")
    launcher = Path(os.environ["RR_TICKER_REAL_LAUNCHER"]).resolve()
    if not launcher.is_relative_to(root):
        raise SystemExit("ticker CLI: copied launcher escaped scratch")
    os.execv(str(launcher), [str(launcher), *sys.argv[1:]])


if __name__ == "__main__":
    main()
