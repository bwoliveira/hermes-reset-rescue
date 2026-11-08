"""T-44 / T-47 / T-63 — the README must describe the plugin the spec defines.

`docs/plan/decisions.md` D-013..D-019 **override** `simplification.md` where the
two differ: masking is a setting that defaults on, the activity log and
History view are back, manual **Redeem now** is back, `language` is a setting,
the compact quota summary is back, sort and small QoL are welcome, and the
engineering bar is unit + integration + e2e + lint + types.

`SPEC` below is written out on purpose rather than read from `plugin.yaml`: the
README must match the *decisions*, not whatever the manifest happens to contain
while L3 is still merging, and a test that derives its expectation from the code
under test proves nothing.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
README = REPO / "README.md"
DECISIONS = REPO / "docs" / "plan" / "decisions.md"
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

#: The eleven keys of D-013..D-019 (plus the six that survived the simplification).
#: name -> (label in the README table, default cell, bounds/choices cell)
SPEC: dict[str, tuple[str, str, str]] = {
    "scan_interval_hours": ("Scan interval (hours)", "`12`", "1–24"),
    "lead_minutes": ("Redeem lead (minutes before expiry)", "`10`", "5–240"),
    "reminder_1_hours": ("First reminder (hours before expiry)", "`24`", "0–168"),
    "reminder_2_hours": ("Second reminder (hours before expiry)", "`1`", "0–72"),
    "notify_target": ("Notice destination", '`""`', "Hermes delivery target"),
    "early_redeem_enabled": ("Redeem an account's soonest-expiring credit", "`false`", "—"),
    "early_redeem_remaining_pct": ("The threshold for the setting above", "`0`", "0–100"),
    "excluded_accounts": ("Account keys or labels", "`[]`", "—"),
    "mask_labels": ("Show `b***@gmail.com`", "`true`", "—"),
    "language": ("Language of the popup", "`auto`", "`auto`, `en`, `pt-BR`"),
    "dry_run": ("Keep scans, jobs and notices active", "`false`", "—"),
}

#: Keys the simplification pass removed and D-013..D-019 did NOT restore.
REMOVED = ("backup_lead_minutes", "account_source", "automatic_rescue", "included_accounts")


def readme_text() -> str:
    return README.read_text(encoding="utf-8")


def readme_prose() -> str:
    """The README with line wrapping collapsed, for phrase assertions."""
    return re.sub(r"\s+", " ", readme_text()).lower()


def setting_rows(text: str) -> dict[str, list[str]]:
    """``{first cell: [cells]}`` for every row of every table."""
    rows: dict[str, list[str]] = {}
    for line in text.splitlines():
        if not line.startswith("| ") or line.startswith("|---"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 3:
            rows[cells[0]] = cells
    return rows


class SettingsDocumented(unittest.TestCase):
    def setUp(self):
        self.text = readme_text()
        self.rows = setting_rows(self.text)

    def test_the_table_has_exactly_the_eleven_keys(self):
        found = {name for name in SPEC if f"`{name}`" in self.text}
        self.assertEqual(found, set(SPEC), "every key must be named in backticks")

    def test_every_key_has_its_default(self):
        for name, (_label, default, _bounds) in SPEC.items():
            row = next((cells for cells in self.rows.values() if f"`{name}`" in cells), None)
            self.assertIsNotNone(row, f"{name}: no README table row")
            self.assertIn(
                default, row[2], f"{name}: default cell {row[2]!r} should contain {default!r}"
            )

    def test_bounds_are_stated(self):
        for name, (_label, _default, bounds) in SPEC.items():
            if bounds == "—":
                continue
            row = next((cells for cells in self.rows.values() if f"`{name}`" in cells), None)
            self.assertIsNotNone(row, f"{name}: no README table row")
            self.assertIn(
                bounds, row[3], f"{name}: range cell {row[3]!r} should contain {bounds!r}"
            )

    def test_removed_settings_are_not_offered(self):
        for removed in REMOVED:
            self.assertNotIn(removed, self.text, f"{removed} was cut; the README must not offer it")

    def test_backup_attempt_is_described_as_automatic(self):
        """`backup_lead_minutes` became an internal formula, not a setting."""
        prose = readme_prose()
        self.assertIn("backup", prose)
        self.assertIn("(6 at the default)", prose)


class Disclosure(unittest.TestCase):
    def setUp(self):
        self.text = readme_text()
        self.prose = readme_prose()

    def test_disclosure_section_exists(self):
        self.assertIn("## What this plugin does on your machine", self.text)

    def test_disclosure_names_the_read_path(self):
        self.assertIn("read_credential_pool", self.text)

    def test_disclosure_names_all_three_endpoints(self):
        self.assertIn("GET /backend-api/wham/rate-limit-reset-credits", self.text)
        self.assertIn("POST /backend-api/wham/rate-limit-reset-credits/consume", self.text)
        self.assertIn("GET /backend-api/wham/usage", self.text)

    def test_disclosure_states_no_token_leaves_for_the_desktop(self):
        self.assertIn("never reach the Desktop", self.text)

    def test_disclosure_states_no_telemetry(self):
        self.assertIn("No telemetry", self.text)

    def test_disclosure_names_the_refresh_path(self):
        self.assertIn("hermes auth refresh openai-codex", self.text)

    def test_disclosure_names_the_files_written(self):
        self.assertIn("<HERMES_HOME>/scripts/", self.text)
        self.assertIn("<HERMES_HOME>/plugin-data/reset-rescue/", self.text)

    def test_disclosure_names_the_only_mutation(self):
        self.assertIn("only mutation", self.prose)

    def test_terms_note_exists(self):
        self.assertIn("## Terms", self.text)
        self.assertIn("not a documented public API", self.text)


class RequiredSections(unittest.TestCase):
    def setUp(self):
        self.text = readme_text()
        self.prose = readme_prose()

    def test_has_the_tagline_and_story(self):
        self.assertIn("Never lose a Codex reset again", self.text)
        self.assertIn("free resets expired unused", self.prose)
        # The story never states how many resets were lost.
        self.assertNotIn("four free resets", self.prose)

    def test_documents_background_jobs(self):
        self.assertIn("cron", self.prose)
        self.assertIn("primary", self.prose)
        self.assertIn("backup", self.prose)

    def test_documents_uninstall_order(self):
        position_uninstall = self.text.find("hermes reset-rescue uninstall")
        position_remove = self.text.find("hermes plugins remove reset-rescue")
        self.assertGreater(position_uninstall, 0)
        self.assertGreater(position_remove, 0)
        self.assertLess(
            position_uninstall,
            position_remove,
            "the README must say to uninstall before removing the plugin",
        )

    def test_documents_prior_art(self):
        for name in (
            "quota",
            "hermes-resetwatch",
            "account-pool",
            "hermes-codex-resets",
            "AURES",
            "Codex Radar Sentinel",
            "pi-better-openai",
        ):
            self.assertIn(name, self.text, f"prior art {name} is not named")

    def test_documents_limits(self):
        self.assertIn("## Limits", self.text)
        self.assertIn("Linux and macOS", self.text)

    def test_documents_screenshots_with_demo_data(self):
        self.assertIn("## Screenshots", self.text)
        self.assertIn("Demo data", self.text)
        self.assertIn("masked", self.prose)

    def test_documents_the_finish_gate(self):
        self.assertIn("scripts/check.sh", self.text)
        for step in ("unit", "desktop", "lint", "types", "integration", "e2e", "tree"):
            self.assertIn(step, self.text, f"the gate step {step} is not documented")


class DecisionsD013ToD019(unittest.TestCase):
    """The decisions that override the simplification pass must be visible."""

    def setUp(self):
        self.text = readme_text()
        self.prose = readme_prose()

    def test_d007_two_reminders(self):
        self.assertIn("24 h", self.prose)
        self.assertIn("1 h", self.prose)
        self.assertIn("turns it off", self.prose)

    def test_d008_notices_use_the_existing_channel(self):
        self.assertIn("telegram", self.prose)
        self.assertIn("no new bot", self.prose)

    def test_d009_early_redemption_is_opt_in(self):
        self.assertIn("off by default", self.prose)
        self.assertIn("weekly quota remaining", self.prose)

    def test_d010_popup_lists_banked_resets(self):
        self.assertIn("banked reset", self.prose)

    def test_d011_local_time_zone(self):
        self.assertIn("time zone", self.prose)
        self.assertIn("desktop's time zone", self.prose)

    def test_d013_masking_is_a_setting_on_by_default(self):
        self.assertIn("`mask_labels`", self.text)
        self.assertIn("masking is on by default", self.prose)
        # D-025: the example is neutral now (example@example.com -> e***@...).
        self.assertIn("e***@e***.com", self.text)

    def test_d014_activity_log_and_history(self):
        self.assertIn("activity log", self.prose)
        self.assertIn("hermes reset-rescue history", self.text)
        self.assertIn("History", self.text)

    def test_d015_manual_redeem_now_with_confirmation(self):
        self.assertIn("Redeem now", self.text)
        self.assertIn("confirmation", self.prose)
        self.assertIn("--yes", self.text)

    def test_d016_language_setting(self):
        self.assertIn("`language`", self.text)
        self.assertIn("pt-BR", self.text)
        self.assertIn("auto", self.prose)

    def test_d017_compact_quota_summary(self):
        self.assertIn("quota line", self.prose)
        self.assertIn("plan", self.prose)
        self.assertIn("5 h", self.prose)
        self.assertIn("weekly", self.prose)

    def test_d018_sort_control(self):
        self.assertIn("sort", self.prose)
        self.assertIn("soonest", self.prose)

    def test_d019_quality_bar(self):
        self.assertIn("ruff", self.text)
        self.assertIn("mypy", self.text)
        self.assertIn("end-to-end", self.prose)

    def test_rehearsal_uses_dry_run(self):
        self.assertIn("set `dry_run` before a rehearsal", self.prose)

    def test_d012_simple_tone_no_jargon_dump(self):
        for jargon in (
            "StatusDTO",
            "CreditDTO",
            "AccountDTO",
            "plugin_data_dir",
            "ReadOnlyTransport",
            "HORIZON",
            "DUE_SLACK",
        ):
            self.assertNotIn(jargon, self.text, f"{jargon} is internal jargon")


class BindingDecisionsArePresent(unittest.TestCase):
    def test_decisions_doc_exists(self):
        self.assertTrue(DECISIONS.is_file(), "docs/plan/decisions.md is binding")

    def test_the_decisions_cover_d013_to_d019(self):
        text = DECISIONS.read_text(encoding="utf-8")
        for number in range(13, 20):
            self.assertIn(f"| D-0{number} |", text, f"D-0{number} is missing")

    def test_the_spec_has_eleven_keys(self):
        self.assertEqual(len(SPEC), 11)


if __name__ == "__main__":
    unittest.main()
