"""T-46 — the demo script must read aloud in under 85 seconds.

"Read aloud in under 85 s" is a measurable claim, so it is measured. The script
table in ``docs/release/demo-script.md`` is parsed, the spoken column extracted,
and the duration computed at three reading paces. The slowest pace is the one
that matters: a demo read too fast sounds rushed, and the ceiling exists so the
recording never has to be.

Pace model: 150 words per minute is a natural technical-demo pace; 130 wpm is
deliberate and clear; 170 wpm is brisk. The script must fit at 130 wpm — if it
only fits when hurried, it is too long.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "docs" / "release" / "demo-script.md"

CEILING_SECONDS = 85.0
PACES = {"deliberate": 130, "natural": 150, "brisk": 170}
MUST_FIT_AT = "deliberate"


def spoken_lines() -> list[str]:
    """The "Say" column of every shot row, in order."""
    text = SCRIPT.read_text(encoding="utf-8")
    lines: list[str] = []
    for raw in text.splitlines():
        if not raw.startswith("| ") or raw.startswith("|---"):
            continue
        cells = [cell.strip() for cell in raw.strip().strip("|").split("|")]
        # Shot rows: # | Time | On screen | Say   (4 cells, first is a digit)
        if len(cells) == 4 and cells[0].isdigit():
            lines.append(cells[3])
    return lines


def word_count(text: str) -> int:
    """Words as a reader speaks them: quoted text counts, markdown does not."""
    cleaned = re.sub(r"[`*_>\[\]()#]", " ", text)
    return len([word for word in cleaned.split() if any(ch.isalnum() for ch in word)])


class DemoScript(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lines = spoken_lines()
        cls.words = sum(word_count(line) for line in cls.lines)

    def test_script_file_exists(self):
        self.assertTrue(SCRIPT.is_file())

    def test_every_shot_has_narration(self):
        self.assertGreaterEqual(len(self.lines), 8, "the script lost shots")
        for index, line in enumerate(self.lines, 1):
            self.assertTrue(line.strip(), f"shot {index} has no narration")

    def test_reads_aloud_under_the_ceiling_at_a_deliberate_pace(self):
        wpm = PACES[MUST_FIT_AT]
        seconds = self.words / wpm * 60
        self.assertLess(
            seconds,
            CEILING_SECONDS,
            f"{self.words} words is {seconds:.1f}s at {wpm} wpm, over the "
            f"{CEILING_SECONDS:.0f}s ceiling. Cut words; do not read faster.",
        )

    def test_reports_the_duration_at_every_pace(self):
        for name, wpm in PACES.items():
            seconds = self.words / wpm * 60
            self.assertLess(
                seconds, CEILING_SECONDS, f"{name} ({wpm} wpm): {seconds:.1f}s exceeds the ceiling"
            )

    def test_shot_times_are_monotonic_and_end_under_ninety(self):
        text = SCRIPT.read_text(encoding="utf-8")
        times = re.findall(r"\|\s*(\d+):(\d\d)\s*[–-]\s*(\d+):(\d\d)\s*\|", text)
        self.assertGreaterEqual(len(times), 8)
        previous_end = -1.0
        for start_min, start_sec, end_min, end_sec in times:
            start = int(start_min) * 60 + int(start_sec)
            end = int(end_min) * 60 + int(end_sec)
            self.assertGreater(end, start, f"shot ends before it starts: {start}-{end}")
            self.assertGreaterEqual(
                start, previous_end - 0.01, "shot times must not overlap or go backwards"
            )
            previous_end = end
        self.assertLessEqual(previous_end, 90.0, f"the cut runs {previous_end:.0f}s")

    def test_shot_list_matches_the_planned_features(self):
        """Every shot maps to something the build has (T-46 acceptance).

        Manual Redeem now (behind a confirmation step) and the dry-run rehearsal
        are both shipped features (D-015, D-020), so the shot list may show them.
        """
        text = SCRIPT.read_text(encoding="utf-8").lower()
        for feature in (
            "status bar",
            "popup",
            "cron list",
            "settings",
            "reminder",
            "readme",
            "install",
            "early",
        ):
            self.assertIn(feature, text, f"no shot covers {feature}")

    def test_dry_run_is_labelled_as_such(self):
        """D-005: a dry-run rescue spends nothing and must say so on screen."""
        text = SCRIPT.read_text(encoding="utf-8").lower()
        self.assertIn("dry run", text)
        self.assertIn("zero credit spent", text)

    def test_no_cut_features_are_advertised(self):
        """Cut features may be named only inside the "What not to show" list.

        "Redeem now" (with confirmation) and the rehearsal are current features,
        so they are not in this list. The confirmation dialog's internal name is
        an implementation detail that must never appear on screen or in narration.
        """
        text = SCRIPT.read_text(encoding="utf-8").lower()
        head, _, tail = text.partition("## what not to show")
        self.assertTrue(tail, "the script needs a 'What not to show' section")
        body = tail.split("\n## ", 1)[0]
        for cut in ("confirmdialog",):
            self.assertNotIn(cut, head, f"{cut} is advertised outside the 'do not show' list")
            self.assertNotIn(cut, tail[len(body) :], f"{cut} leaks past the 'do not show' section")

    def test_what_not_to_show_forbids_real_spend_and_private_data(self):
        """The do-not-show list must rule out a real credit, full e-mails and tokens."""
        text = SCRIPT.read_text(encoding="utf-8").lower()
        _, _, tail = text.partition("## what not to show")
        body = tail.split("\n## ", 1)[0]
        for phrase in ("real credit", "full e-mail", "token", "chat id"):
            self.assertIn(phrase, body, f"'what not to show' omits: {phrase}")

    def test_demo_uses_a_demo_profile(self):
        """R-006: labels are shown in full, so the demo must not use real ones."""
        text = SCRIPT.read_text(encoding="utf-8").lower()
        self.assertIn("demo profile", text)

    def test_no_untrue_claims_about_quota(self):
        text = SCRIPT.read_text(encoding="utf-8").lower()
        self.assertIn("quota percentages", text)  # the "if you see this, it's wrong" note
        self.assertNotIn("shows your quota", text)


if __name__ == "__main__":
    unittest.main()
