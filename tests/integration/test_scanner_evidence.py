"""S11 — the scanner evidence must stay true.

`docs/release/scanner.md` claims the tree scans `safe` and that every finding
outside `docs/` is explained. Those are assertions about the code, so they get
a test: if someone adds a `subprocess` call to runtime code, or lets an
allow-list row go stale, this fails before a reviewer finds it.

The scanner itself is Hermes' (`tools.plugin_guard.scan_plugin`), imported
through the runtime — the same function `hermes plugins install` calls.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import offline_guard  # noqa: E402  (path set above)
import scan_repo  # noqa: E402  (path set above)

# The Hermes scanner shells out to `git ls-files` to find tracked bytecode
# (catalog rule 17), so proving the scan needs that one process. Scoped to this
# module: nothing else here may start a process.
_ALLOWED = None


def setUpModule():
    global _ALLOWED
    _ALLOWED = offline_guard.allow_subprocess("git")
    _ALLOWED.__enter__()


def tearDownModule():
    if _ALLOWED is not None:
        _ALLOWED.__exit__(None, None, None)


class ScannerEvidence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from tools.plugin_guard import scan_plugin
        except Exception as exc:  # pragma: no cover - only outside the runtime
            raise unittest.SkipTest(f"Hermes scanner unavailable: {type(exc).__name__}") from exc
        cls.result = scan_plugin(REPO)
        cls.explained = scan_repo.load_allowlist()

    def test_verdict_is_safe(self):
        self.assertEqual(self.result.verdict, "safe", self.result.summary)

    def test_no_critical_or_high_findings(self):
        flagged = [f for f in self.result.findings if f.severity in ("critical", "high")]
        self.assertEqual(
            flagged,
            [],
            "a critical/high finding makes a catalog install dangerous or reviewable: "
            + ", ".join(f"{f.pattern_id} {f.file}:{f.line}" for f in flagged),
        )

    def test_every_finding_outside_docs_is_explained(self):
        unexplained = [
            f"{f.pattern_id} {f.file}:{f.line}"
            for f in self.result.findings
            if not scan_repo._explained_by(f.pattern_id, f.file, self.explained)
            and not f.file.startswith(scan_repo.DOC_PREFIXES)
        ]
        self.assertEqual(
            unexplained,
            [],
            "add a row to docs/release/scanner-allowlist.txt with a true reason, "
            "or remove the cause",
        )

    def test_allowlist_has_no_stale_rows(self):
        seen = {(f.pattern_id, f.file) for f in self.result.findings}
        stale = []
        for pattern_id, file_name in self.explained:
            if (pattern_id, file_name) in seen:
                continue
            if file_name.endswith("/") and any(
                f.pattern_id == pattern_id and f.file.startswith(file_name)
                for f in self.result.findings
            ):
                continue
            stale.append((pattern_id, file_name))
        self.assertEqual(sorted(stale), [], "stale allow-list rows would hide a future finding")

    def test_allowlist_rows_carry_a_reason(self):
        for key, reason in self.explained.items():
            self.assertGreater(len(reason), 20, f"{key} needs a real reason, not a placeholder")

    def test_no_binary_or_bytecode_findings(self):
        """Catalog rule 17: no unscannable payload."""
        bad = [
            f
            for f in self.result.findings
            if f.pattern_id
            in (
                "binary_file",
                "bytecode_file",
                "too_many_files",
                "oversized_bundle",
                "symlink_escape",
            )
        ]
        self.assertEqual(bad, [], ", ".join(f"{f.pattern_id} {f.file}" for f in bad))

    def test_evidence_doc_exists_and_matches_the_verdict(self):
        doc = REPO / "docs" / "release" / "scanner.md"
        self.assertTrue(doc.is_file(), "docs/release/scanner.md is the S11 evidence")
        text = doc.read_text(encoding="utf-8")
        self.assertIn("Verdict: PASS", text)
        self.assertIn(self.result.verdict, text)


class AllowlistParser(unittest.TestCase):
    def test_malformed_row_is_an_error(self):
        import tempfile

        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as handle:
            handle.write("only_one_field\n")
            path = Path(handle.name)
        try:
            with self.assertRaises(ValueError):
                scan_repo.load_allowlist(path)
        finally:
            path.unlink()

    def test_comments_and_blanks_are_ignored(self):
        import tempfile

        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as handle:
            handle.write("# a comment\n\npattern|file.py|because the reason is real\n")
            path = Path(handle.name)
        try:
            rows = scan_repo.load_allowlist(path)
        finally:
            path.unlink()
        self.assertEqual(list(rows), [("pattern", "file.py")])

    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(scan_repo.load_allowlist(Path("/nonexistent/allowlist.txt")), {})


if __name__ == "__main__":
    unittest.main()
