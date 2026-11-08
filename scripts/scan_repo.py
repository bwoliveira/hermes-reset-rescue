#!/usr/bin/env python3
"""Run Hermes' install-time plugin scanner over this repository (S11, T-52).

Run it under the Hermes runtime, never a bare interpreter:

    python3 .../run_runtime_script.py <repo>/scripts/scan_repo.py [path]

The scanner is the same one `hermes plugins install` and
`hermes plugins validate` use (`tools.plugin_guard.scan_plugin`), so a verdict
here is the verdict a reviewer's install will get. `dangerous` is a hard stop;
`caution` is a list of findings a human must explain in the PR.

Exit codes: 0 safe, 1 caution, 2 dangerous, 3 the scanner could not run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: The reviewable allow-list lives next to the evidence it belongs to:
#: ``<pattern_id>|<file>|<why>``. Keeping it in a data file means a new finding
#: is a visible diff in review, not a silent code change.
ALLOWLIST_PATH = REPO / "docs" / "release" / "scanner-allowlist.txt"

#: Whole path prefixes whose findings are documentation-only: prose that
#: describes a threat while explaining why the plugin does not do it.
DOC_PREFIXES = ("docs/",)


def load_allowlist(path: Path = ALLOWLIST_PATH) -> dict[tuple[str, str], str]:
    """``{(pattern_id, file): reason}``; a malformed row is a hard error.

    A ``file`` ending in ``/`` is a category: it covers every file under that
    prefix. Use one only where the whole category is expected — ``tests/`` is
    the case here, because the offline guard already forces every test that
    starts a process to declare ``allow_subprocess`` in its own code.
    """
    rows: dict[tuple[str, str], str] = {}
    if not path.is_file():
        return rows
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        fields = raw.split("|")
        if len(fields) < 3:
            raise ValueError(f"{path}:{number}: expected pattern_id|file|reason")
        pattern_id, file_name = fields[0].strip(), fields[1].strip()
        rows[(pattern_id, file_name)] = "|".join(fields[2:]).strip()
    return rows


def _explained_by(pattern_id: str, file_name: str, rows: dict[tuple[str, str], str]) -> bool:
    """Exact row, or a category row whose prefix covers ``file_name``."""
    if (pattern_id, file_name) in rows:
        return True
    for row_pattern, row_file in rows:
        if row_pattern == pattern_id and row_file.endswith("/") and file_name.startswith(row_file):
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("path", nargs="?", default=str(REPO))
    parser.add_argument(
        "--strict",
        action="store_true",
        help="fail on any finding not listed in EXPLAINED, even a low one",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    options = parser.parse_args(argv)

    target = Path(options.path).resolve()
    if not target.is_dir():
        print(f"scan_repo: not a directory: {target}", file=sys.stderr)
        return 3

    try:
        from tools.plugin_guard import scan_plugin  # noqa: PLC0415 - runtime import
    except Exception as exc:  # pragma: no cover - only when run outside the runtime
        print(
            f"scan_repo: cannot import the Hermes scanner ({type(exc).__name__}). "
            f"Run this through the runtime wrapper, not a bare python3.",
            file=sys.stderr,
        )
        return 3

    result = scan_plugin(target)
    try:
        explained = load_allowlist()
    except ValueError as exc:
        print(f"scan_repo: {exc}", file=sys.stderr)
        return 3

    if options.json:
        import json  # noqa: PLC0415

        print(
            json.dumps(
                {
                    "verdict": result.verdict,
                    "summary": result.summary,
                    "findings": [
                        {
                            "severity": f.severity,
                            "pattern_id": f.pattern_id,
                            "file": f.file,
                            "line": f.line,
                            "description": f.description,
                            "explained": _explained_by(f.pattern_id, f.file, explained),
                        }
                        for f in result.findings
                    ],
                },
                indent=2,
            )
        )
        # JSON mode is for a machine: nothing else may reach stdout.
        return {"safe": 0, "caution": 1, "dangerous": 2}.get(result.verdict, 3)
    print(f"scanner: {result.summary}")
    print(f"verdict: {result.verdict}")
    print()
    unexplained = 0
    for finding in sorted(result.findings, key=lambda f: (f.severity, f.file, f.line)):
        if _explained_by(finding.pattern_id, finding.file, explained):
            state = "explained"
        elif finding.file.startswith(DOC_PREFIXES):
            state = "documentation"
        else:
            state = "UNEXPLAINED"
            unexplained += 1
        print(
            f"  [{finding.severity:8}] {state:13} {finding.pattern_id} "
            f"{finding.file}:{finding.line}"
        )
        print(f"             {finding.description}")
    print()
    if result.findings:
        print(f"{len(result.findings)} finding(s), {unexplained} unexplained")
    else:
        print("no findings")

    if result.verdict == "dangerous":
        print(
            "RESULT: DANGEROUS — a catalog install would be blocked. Stop and fix the cause; "
            "never disguise it.",
            file=sys.stderr,
        )
        return 2
    if options.strict:
        unexplained = [
            f
            for f in result.findings
            if not _explained_by(f.pattern_id, f.file, explained)
            and not f.file.startswith(DOC_PREFIXES)
        ]
        if unexplained:
            print(f"RESULT: FAIL ({len(unexplained)} unexplained finding(s))", file=sys.stderr)
            return 1
        # A row that no longer matches anything is stale evidence: it would let a
        # real finding hide behind an old excuse. Delete it instead.
        seen = {(f.pattern_id, f.file) for f in result.findings}
        stale = sorted(
            key
            for key in explained
            if key not in seen
            and not (
                key[1].endswith("/")
                and any(
                    f.file.startswith(key[1]) and f.pattern_id == key[0] for f in result.findings
                )
            )
        )
        if stale:
            for pattern_id, file_name in stale:
                print(f"stale allow-list row: {pattern_id} {file_name}", file=sys.stderr)
            print(f"RESULT: FAIL ({len(stale)} stale allow-list row(s))", file=sys.stderr)
            return 1
    if result.verdict == "caution":
        print("RESULT: CAUTION — every finding above must be explained in docs/release/scanner.md")
        return 1
    print("RESULT: SAFE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
