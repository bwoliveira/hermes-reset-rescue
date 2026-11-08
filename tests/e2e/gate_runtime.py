#!/usr/bin/env python3
"""Launch the supplied runtime wrapper with a seeded, isolated bootstrap home.

check.sh deliberately drops an inherited HERMES_HOME. A read-only release gate
must not let the runtime resolver take install locks in the real home instead.
This adapter preserves the supplied public-launcher wrapper without caching or
bypassing its dependency/bootstrap contract.
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


def main() -> None:
    root = Path(os.environ["RR_GATE_TMP_ROOT"]).resolve()
    if not root.is_relative_to(Path("/var/tmp")):
        raise SystemExit("gate runtime: temporary root must be under /var/tmp")
    home = Path(os.environ.get("HERMES_HOME") or root / "bootstrap-home").resolve()
    if not home.is_relative_to(root):
        raise SystemExit("gate runtime: home escaped the isolated gate root")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from seed import seed_profile

    if not (home / "installs").is_dir():
        seed_profile(home)
    from tests.support.isolation import assert_isolated

    assert_isolated(home)
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_PROFILE"] = ""
    wrapper = Path(os.environ["RR_ORIGINAL_RUNTIME_WRAPPER"]).resolve()
    if wrapper == Path(__file__).resolve():
        raise SystemExit("gate runtime: recursive runtime wrapper")
    sys.argv[0] = str(wrapper)
    runpy.run_path(str(wrapper), run_name="__main__")


if __name__ == "__main__":
    main()
