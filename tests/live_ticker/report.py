#!/usr/bin/env python3
"""Turn actual scenario markers into an evidence table, with log line links."""

from __future__ import annotations

import re
import sys
from pathlib import Path

SCENARIOS = (
    "Native scan arming",
    "Successful primary",
    "503 primary to backup",
    "Global quota",
    "Account-only quota",
    "Plugin disable",
    "Uninstall ownership",
    "Privacy and isolation",
)


def main() -> None:
    log, output = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    if not output.is_relative_to(Path("/var/tmp")):
        raise SystemExit("ticker report must be outside the real home, under /var/tmp")
    lines = log.read_text().splitlines()
    markers = {}
    for number, line in enumerate(lines, 1):
        if line.startswith("SCENARIO PASS | "):
            _, name, detail = line.split(" | ", 2)
            markers[name] = (number, detail)
    runtime = next(
        (re.search(r"([\d.]+)s\)$", line) for line in reversed(lines) if line.startswith("E2E OK")),
        None,
    )
    table = [
        "# Reset Rescue real-ticker validation",
        "",
        f"Log: `{log}`; exit code: `{sys.argv[3]}`.",
        f"Runtime: {runtime[1] + ' seconds' if runtime else 'incomplete (see log)'}.",
        "",
        "| Scenario | Result | Evidence |",
        "|---|---|---|",
    ]
    for name in SCENARIOS:
        if name in markers:
            number, detail = markers[name]
            table.append(f"| {name} | PASS | `{log.name}:L{number}` — {detail} |")
        else:
            table.append(f"| {name} | FAIL | Not completed; see `{log.name}` |")
    table.extend(
        [
            "",
            "Timing fixtures only rewind the throwaway job/credit deadlines and quota "
            "cooldowns. The native scheduler dispatches every scan/fire/quota invocation.",
            "Successful-primary cleanup uses a native backup no-op and native scan after "
            "fixture-only GC deadline aging; disable leaves cron definitions inert (no dispatch).",
            "The wrapper uses private mount + network namespaces, read-only real home/install, "
            "and verifies resolved temporary homes before every CLI invocation.",
            "",
        ]
    )
    output.write_text("\n".join(table))
    print(f"scenario matrix: {output}")


if __name__ == "__main__":
    main()
