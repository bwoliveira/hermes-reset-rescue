#!/usr/bin/env python3
"""Prepare real core-only dependency state for native offline plugin-disable.

Run only through scripts/ticker_prepare.sh. This is a prerequisite, not a ticker
scenario: PM may download official Python packages, never Codex/account data.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

LIVE_HERMES_HOME = Path(os.environ.get("RR_LIVE_HERMES_HOME") or Path.home() / ".hermes")
HERMES_INSTALL = Path(os.environ.get("RR_HERMES_INSTALL") or "/usr/local/lib/hermes-agent")

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "tests/e2e")]
from fake_backend import copy_plugin  # noqa: E402
from seed import _selection, install_key, seed_profile  # noqa: E402


def main() -> None:
    root = Path(os.environ["RR_TICKER_PREP_ROOT"]).resolve()
    home = root / "home"
    if not root.is_relative_to(Path("/var/tmp")):
        raise SystemExit("prepared dependency state must be below /var/tmp")
    for path in (LIVE_HERMES_HOME, HERMES_INSTALL):
        if not os.statvfs(path).f_flag & os.ST_RDONLY:
            raise SystemExit(f"preparation requires read-only mount: {path}")
    if sys.argv[1:] == ["--check"]:
        _selection(home / "installs" / install_key())
        print(home / "installs" / install_key())
        return
    root.mkdir()  # Refuse existing input; never recursively delete a user's seed.
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_PROFILE"] = ""
    os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    store = root / "tools"
    store.mkdir()
    for item in LIVE_HERMES_HOME / "tools".iterdir():
        if item.name == "facts.json":
            shutil.copy2(item, store / item.name)
        elif item.is_dir() and not item.name.startswith((".", "fetch-")):
            (store / item.name).symlink_to(item, target_is_directory=True)
    os.environ["HERMES_RUNTIME_DIR"] = str(store)
    if seed_profile(home) is None:
        raise SystemExit("installed dependency state is incomplete")
    copy_plugin(REPO, home)
    cache = LIVE_HERMES_HOME / "cache" / "uv"
    if cache.is_dir():
        (home / "cache").mkdir()
        shutil.copytree(cache, home / "cache/uv", symlinks=True)
    sys.path.insert(0, str(HERMES_INSTALL))
    from hermes_constants import get_default_hermes_root, get_hermes_home

    for args in (
        ("config", "set", "plugins.enabled", '["reset-rescue"]'),
        ("plugins", "disable", "reset-rescue"),
    ):
        if get_hermes_home().resolve() != home or get_default_hermes_root().resolve() != home:
            raise SystemExit("resolved preparation home is unsafe")
        print(f"verified preparation home before Hermes call: {home}", flush=True)
        subprocess.run(["hermes", *args], stdin=subprocess.DEVNULL, timeout=600, check=True)
    config = (home / "config.yaml").read_text()
    if "reset-rescue" not in config:
        raise SystemExit("native disable did not persist selection")
    _selection(home / "installs" / install_key())
    print("prepared native dependency selection:", home / "installs" / install_key())


if __name__ == "__main__":
    main()
