#!/usr/bin/env python3
"""Lane L2 test runner (temporary until L5's scripts/test.sh lands).

Runs the stdlib ``unittest`` discovery over this directory with the repository
root on ``sys.path`` so ``reset_rescue`` imports as the plugin package.

Usage::

    python3 <RUNTIME_WRAPPER> <wt>/tests/flows/run.py [suite dir]
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def main(argv: list[str]) -> int:
    suite_dir = Path(argv[1]).resolve() if len(argv) > 1 else HERE
    for entry in (str(ROOT), str(suite_dir)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    loader = unittest.TestLoader()
    suite = loader.discover(str(suite_dir), top_level_dir=str(suite_dir))
    runner = unittest.TextTestRunner(verbosity=2, stream=sys.stderr)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
