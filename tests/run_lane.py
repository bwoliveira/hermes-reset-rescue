#!/usr/bin/env python3
"""Lane L1 test runner.

Run under the Hermes runtime so ``agent``/``hermes_cli`` imports resolve:

    python3 <hermes-agent-local>/scripts/run_runtime_script.py \
        <worktree>/tests/run_lane.py tests/engine

With no arguments it discovers ``tests/engine``. Extra args are passed to
``unittest`` (``-v``, ``-k pattern``).
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

LANE_ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str]) -> int:
    if str(LANE_ROOT) not in sys.path:
        sys.path.insert(0, str(LANE_ROOT))
    os.chdir(LANE_ROOT)

    args = list(argv[1:])
    start = "tests/engine"
    if args and not args[0].startswith("-"):
        start = args.pop(0)
    verbosity = 2 if "-v" in args else 1
    args = [arg for arg in args if arg != "-v"]

    loader = unittest.TestLoader()
    target = LANE_ROOT / start
    if target.is_file():
        # A single file: import it as a module of its package so relative
        # imports and ``tests.*`` names keep working.
        module = ".".join(target.relative_to(LANE_ROOT).with_suffix("").parts)
        suite = loader.loadTestsFromName(module)
    else:
        suite = loader.discover(start_dir=start, top_level_dir=str(LANE_ROOT), pattern="test_*.py")
    runner = unittest.TextTestRunner(verbosity=verbosity, buffer=False)
    result = runner.run(suite)
    print(
        f"\nran={result.testsRun} failures={len(result.failures)} "
        f"errors={len(result.errors)} skipped={len(result.skipped)}"
    )
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
