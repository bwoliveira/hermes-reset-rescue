"""T-74 — the host side of D-013 (masking) and D-016 (language), plus S3/S4 parity.

Three things are pinned here, each of which only the host lane can prove:

* **D-013** — ``mask_labels`` reaches the notifier from the plugin's settings
  entry, defaults to on, and a non-bool value falls back to the default instead
  of being truthy-coerced from a string.
* **D-016** — ``language = auto`` follows Hermes' ``display.language`` and falls
  back to English when Hermes' language is neither English nor Portuguese;
  ``en``/``pt-BR`` win over the host.
* **S3/S4 parity** — the ``hermes cron create`` argv this plugin builds is the
  argv the spikes proved the installed CLI accepts: same flags, same order.
  The evidence docs are the source of truth, so the test reads them.

No network, no real profile, no subprocess: ``load_config_readonly`` is mocked.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from unittest import mock

from reset_rescue import host, messages
from reset_rescue.cron_gateway import JobSpec, build_scan_spec, build_slot_spec

REPO = Path(__file__).resolve().parents[2]
EVIDENCE = REPO / "docs" / "evidence"

#: Flags the spikes added only to keep their probe jobs inert. The plugin's real
#: jobs are never created paused, so these are expected to be absent.
SPIKE_ONLY_FLAGS = ("--paused", "--paused-reason")


def _config(settings: dict, *, display_language: str = "") -> dict:
    """A ``config.yaml`` shape with this plugin's entry and a display language."""
    config: dict = {"plugins": {"entries": {host.PLUGIN_ID: {"settings": dict(settings)}}}}
    if display_language:
        config["display"] = {"language": display_language}
    return config


def _patched(settings: dict, *, display_language: str = ""):
    """Patch the one read the host does, so no real profile is touched."""
    return mock.patch(
        "hermes_cli.config.load_config_readonly",
        return_value=_config(settings, display_language=display_language),
    )


class MaskLabelsTests(unittest.TestCase):
    """D-013: masking is a setting, on by default, read from the plugin entry."""

    def test_the_flag_defaults_to_on_when_the_setting_is_absent(self):
        with _patched({}):
            self.assertTrue(host.plugin_mask_labels())

    def test_the_flag_is_read_from_the_plugin_entry(self):
        with _patched({"mask_labels": False}):
            self.assertFalse(host.plugin_mask_labels())
        with _patched({"mask_labels": True}):
            self.assertTrue(host.plugin_mask_labels())

    def test_a_non_bool_value_falls_back_to_the_default(self):
        """A string from a hand-edited config must not read as truthy."""
        for value in ("false", "0", "", 0, None):
            with _patched({"mask_labels": value}):
                self.assertTrue(host.plugin_mask_labels(), repr(value))

    def test_a_broken_config_still_yields_the_default(self):
        with mock.patch("hermes_cli.config.load_config_readonly", side_effect=RuntimeError("boom")):
            self.assertTrue(host.plugin_mask_labels())

    def test_the_notifier_built_by_the_composition_root_carries_the_setting(self):
        """The switch has to reach the object that renders the notice."""
        with _patched({"mask_labels": False, "notify_target": "telegram"}):
            deps = host.build_deps()
        self.assertFalse(deps.notifier.mask_labels)

    def test_the_notifier_masks_by_default(self):
        with _patched({"notify_target": "telegram"}):
            deps = host.build_deps()
        self.assertTrue(deps.notifier.mask_labels)

    def test_masking_off_shows_the_label_and_on_hides_it(self):
        """End to end through the notifier's own renderer, no send."""
        params = {
            "label": "bea.doe@example.com",
            "redeemed_at": 1_800_000_000.0,
            "expires_at": 1_800_000_600.0,
        }
        with _patched({"mask_labels": False, "notify_target": "telegram"}):
            unmasked = host.build_deps().notifier.text("success", params)
        with _patched({"mask_labels": True, "notify_target": "telegram"}):
            masked = host.build_deps().notifier.text("success", params)
        self.assertIn("bea.doe@example.com", unmasked)
        self.assertIn("b***@e***.com", masked)
        self.assertNotIn("bea.doe@", masked)


class LanguageTests(unittest.TestCase):
    """D-016: auto follows Hermes, en and pt-BR force one bundle."""

    def test_an_explicit_setting_wins_over_the_host(self):
        with _patched({"language": "en"}, display_language="pt-BR"):
            self.assertEqual(host.plugin_language(), "en")
        with _patched({"language": "pt-BR"}, display_language="en"):
            self.assertEqual(host.plugin_language(), "pt-BR")

    def test_auto_follows_the_hermes_display_language(self):
        with _patched({"language": "auto"}, display_language="pt-BR"):
            self.assertEqual(host.plugin_language(), "pt-BR")
        with _patched({"language": "auto"}, display_language="en"):
            self.assertEqual(host.plugin_language(), "en")

    def test_auto_falls_back_to_english_for_a_language_we_do_not_ship(self):
        for host_language in ("de", "fr", "ja", "pl"):
            with _patched({"language": "auto"}, display_language=host_language):
                self.assertEqual(host.plugin_language(), messages.DEFAULT_LANGUAGE, host_language)

    def test_auto_without_a_host_language_is_english(self):
        with _patched({"language": "auto"}):
            self.assertEqual(host.plugin_language(), "en")

    def test_an_unset_setting_behaves_as_auto(self):
        with _patched({}, display_language="pt-BR"):
            self.assertEqual(host.plugin_language(), "pt-BR")

    def test_a_broken_config_falls_back_to_english(self):
        with mock.patch("hermes_cli.config.load_config_readonly", side_effect=RuntimeError("boom")):
            self.assertEqual(host.plugin_language(), "en")

    def test_the_notifier_built_by_the_composition_root_speaks_the_setting(self):
        with _patched({"language": "pt-BR"}):
            deps = host.build_deps()
        self.assertEqual(deps.notifier.language, "pt-BR")
        text = deps.notifier.text(
            "success", {"label": "x", "redeemed_at": 1_800_000_000.0, "expires_at": 1_800_000_600.0}
        )
        self.assertIn("Salvo!", text)

    def test_auto_reaches_the_notifier_through_the_host_language(self):
        with _patched({"language": "auto"}, display_language="pt-BR"):
            deps = host.build_deps()
        self.assertEqual(deps.notifier.language, "pt-BR")


def _evidence_argv(document: str) -> list[list[str]]:
    """Every ``hermes cron create …`` argv recorded in an evidence document.

    The two spikes record the same command in different places — S4 under a
    top-level ``commands`` list, S3 under ``schedule.argv`` — so every JSON
    block is scanned for both shapes. A document that yields nothing is a
    failure, not a skip: the parity test is worthless if the source of truth
    silently disappeared.
    """
    text = (EVIDENCE / document).read_text(encoding="utf-8")
    blocks = re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)
    if not blocks:
        raise AssertionError(f"{document}: no sanitized JSON block")
    found: list[list[str]] = []
    for block in blocks:
        try:
            payload = json.loads(block)
        except ValueError:
            continue
        candidates = list(payload.get("commands") or [])
        schedule = payload.get("schedule")
        if isinstance(schedule, dict) and schedule.get("argv"):
            candidates.append(schedule["argv"])
        for command in candidates:
            argv = [str(part) for part in command]
            if argv[:1] and argv[0].endswith("hermes") and argv[1:3] == ["cron", "create"]:
                found.append(argv)
    return found


def _without_spike_flags(argv: list[str]) -> list[str]:
    """Drop the probe-only pause flags, keeping every other token in order."""
    cleaned: list[str] = []
    skip = False
    for token in argv:
        if skip:
            skip = False
            continue
        if token in SPIKE_ONLY_FLAGS:
            skip = token == "--paused-reason"
            continue
        cleaned.append(token)
    return cleaned


def _flag_sequence(argv: list[str]) -> list[str]:
    """The flags of an argv, in order, with their values dropped."""
    return [token for token in argv if token.startswith("--")]


@unittest.skipUnless(EVIDENCE.is_dir(), "docs/evidence is maintainer-only, not in the public tree")
class CronArgvParityTests(unittest.TestCase):
    """The argv the plugin builds is the argv the spikes proved (S3, S4)."""

    def test_the_evidence_documents_are_present_and_parse(self):
        for document in ("S3.md", "S4.md"):
            self.assertTrue(_evidence_argv(document), document)

    def test_a_one_shot_argv_matches_the_s4_contract(self):
        recorded = None
        for argv in _evidence_argv("S4.md"):
            if "--repeat" in argv:
                recorded = _without_spike_flags(argv)
                break
        self.assertIsNotNone(recorded, "S4 records a one-shot create argv")
        spec = JobSpec(
            name="rr-spike-once",
            schedule=recorded[3],
            script="rr_spike_noop.py",
            deliver="local",
            failure_deliver="local",
            repeat=1,
        )
        self.assertEqual(["hermes"] + spec.argv(), recorded)

    def test_a_recurring_argv_matches_the_s4_contract(self):
        recorded = None
        for argv in _evidence_argv("S4.md"):
            if argv[3] == "every 12h" and "--repeat" not in argv:
                recorded = _without_spike_flags(argv)
                break
        self.assertIsNotNone(recorded, "S4 records a recurring create argv")
        spec = JobSpec(
            name="rr-spike-periodic",
            schedule="every 12h",
            script="rr_spike_noop.py",
            deliver="local",
            failure_deliver="local",
        )
        self.assertEqual(["hermes"] + spec.argv(), recorded)

    def test_the_s3_one_shot_agrees_with_the_s4_one_shot(self):
        """Two independent spikes, one grammar: a drift between them is a bug.

        The values differ by design (a different instant, job name and script);
        what must agree is the *grammar* — the same flags, in the same order.
        """
        s3 = _without_spike_flags(_evidence_argv("S3.md")[0])
        s4 = _without_spike_flags(
            next(argv for argv in _evidence_argv("S4.md") if "--repeat" in argv)
        )
        self.assertEqual(_flag_sequence(s3), _flag_sequence(s4))

    def test_the_plugin_never_creates_a_paused_job(self):
        """The spike paused its probes; a real rescue job must run."""
        for spec in (
            build_scan_spec(interval_hours=12, deliver="local"),
            build_slot_spec(
                role="primary",
                key="0123456789abcdef",
                fire_at_iso="2026-11-09T21:30:00-03:00",
                deliver="local",
            ),
        ):
            argv = spec.argv()
            self.assertNotIn("--paused", argv)
            self.assertNotIn("--paused-reason", argv)
            self.assertIn("--no-agent", argv)
            self.assertIn("--failure-deliver", argv)

    def test_the_scan_spec_is_the_periodic_shape_of_the_contract(self):
        argv = build_scan_spec(interval_hours=12, deliver="telegram").argv()
        self.assertEqual(argv[:3], ["cron", "create", "every 12h"])
        self.assertLess(argv.index("--name"), argv.index("--script"))
        self.assertLess(argv.index("--script"), argv.index("--no-agent"))
        self.assertLess(argv.index("--deliver"), argv.index("--failure-deliver"))


if __name__ == "__main__":
    unittest.main()
