"""T-04/T-71: the settings table, coercion, bounds and internal derivations.

The surface is the simplification pass' nine keys plus ``mask_labels`` (D-013)
and ``language`` (D-016). ``backup_lead_minutes``, ``account_source``,
``automatic_rescue`` and ``included_accounts`` are **not** settings any more:
the first is an internal formula, the others are automatic behavior.
"""

from __future__ import annotations

import unittest

from reset_rescue.settings import (
    DEFAULTS,
    MAX_BACKUP_LEAD_MINUTES,
    MAX_EXCLUDED_ACCOUNTS,
    MIN_BACKUP_GAP_MINUTES,
    SETTING_NAMES,
    SETTINGS_TABLE,
    WARN_CODES,
    Settings,
    SettingsError,
    backup_lead_for,
    config_schema,
    mask_notify_target,
    parse_settings,
    validate_notify_target,
)

#: The binding nine keys of simplification.md §2.
NINE_KEYS = (
    "scan_interval_hours",
    "lead_minutes",
    "reminder_1_hours",
    "reminder_2_hours",
    "notify_target",
    "early_redeem_enabled",
    "early_redeem_remaining_pct",
    "excluded_accounts",
    "dry_run",
)

#: The two keys D-013/D-016 restored.
RESTORED_KEYS = ("mask_labels", "language")

#: D-023: the automatic-redemption master switch, symmetric with the early one.
AUTO_REDEEM_KEYS = ("auto_redeem_enabled",)


class CoercionTable(unittest.TestCase):
    """Out-of-range and wrong-typed values fall back with a warning code."""

    def test_out_of_range_scan_interval_falls_back_with_a_warning(self):
        for value in (0, 25, -3, 1000):
            settings = parse_settings({"scan_interval_hours": value})
            self.assertEqual(settings.scan_interval_hours, 12, value)
            self.assertIn("settings_scan_interval_invalid", settings.warnings, value)

    def test_out_of_range_lead_falls_back_with_a_warning(self):
        for value in (4, 241, 0, -10):
            settings = parse_settings({"lead_minutes": value})
            self.assertEqual(settings.lead_minutes, 10, value)
            self.assertIn("settings_lead_invalid", settings.warnings, value)

    def test_wrong_types_fall_back_instead_of_raising(self):
        for value in ("twelve", None, [], {}, 12.5, True):
            settings = parse_settings({"scan_interval_hours": value})
            self.assertEqual(settings.scan_interval_hours, 12, value)
            self.assertIn("settings_scan_interval_invalid", settings.warnings, value)

    def test_a_whole_float_and_a_numeric_string_are_accepted(self):
        self.assertEqual(parse_settings({"scan_interval_hours": 6.0}).scan_interval_hours, 6)
        self.assertEqual(parse_settings({"scan_interval_hours": " 6 "}).scan_interval_hours, 6)
        self.assertEqual(parse_settings({"scan_interval_hours": "6"}).warnings, ())

    def test_booleans_are_not_integers(self):
        settings = parse_settings({"lead_minutes": True})
        self.assertEqual(settings.lead_minutes, 10)
        self.assertIn("settings_lead_invalid", settings.warnings)


class InternalBackupLead(unittest.TestCase):
    """``backup_lead_minutes`` is a formula, not a setting (simplification §2)."""

    def test_the_formula_is_min_of_four_and_lead_minus_three(self):
        self.assertEqual(backup_lead_for(10), 4)
        self.assertEqual(backup_lead_for(240), 4)
        self.assertEqual(backup_lead_for(5), 2)
        self.assertEqual(backup_lead_for(7), 4)
        self.assertEqual(backup_lead_for(6), 3)

    def test_the_gap_is_at_least_three_minutes(self):
        for lead in range(5, 241):
            self.assertGreaterEqual(lead - backup_lead_for(lead), MIN_BACKUP_GAP_MINUTES, lead)

    def test_the_backup_never_fires_later_than_four_minutes(self):
        for lead in range(5, 241):
            self.assertLessEqual(backup_lead_for(lead), MAX_BACKUP_LEAD_MINUTES, lead)

    def test_parse_settings_derives_it(self):
        self.assertEqual(parse_settings({"lead_minutes": 20}).backup_lead_minutes, 4)
        self.assertEqual(parse_settings({"lead_minutes": 5}).backup_lead_minutes, 2)

    def test_the_default_settings_expose_the_derived_value(self):
        settings = parse_settings({})
        self.assertEqual(settings.effective_backup_lead_minutes, 4)
        self.assertEqual(settings.backup_lead_seconds, 240.0)
        self.assertEqual(settings.backup_fire_offset_s, 240.0)

    def test_a_pinned_value_wins(self):
        settings = Settings(lead_minutes=30, backup_lead_minutes=6)
        self.assertEqual(settings.effective_backup_lead_minutes, 6)
        self.assertEqual(settings.backup_fire_offset_s, 360.0)

    def test_it_is_not_in_the_settings_table(self):
        self.assertNotIn("backup_lead_minutes", SETTING_NAMES)
        self.assertNotIn("backup_lead_minutes", config_schema())


class NotifyTargetTable(unittest.TestCase):
    def test_valid_targets_are_kept(self):
        for value in (
            "",
            "local",
            "telegram",
            "telegram:123456789",
            "telegram:-1001234567890:42",
            "discord:99",
        ):
            target, ok = validate_notify_target(value)
            self.assertTrue(ok, value)
            self.assertEqual(target, value.strip(), value)

    def test_invalid_targets_become_local_with_a_warning(self):
        for value in (
            "nonsense",
            "telegram:abc",
            "telegram:1:2:3",
            "local:1",
            "telegram:",
            ":123",
            "email:notanumber",
            42,
            None,
        ):
            settings = parse_settings({"notify_target": value})
            self.assertEqual(settings.notify_target, "", value)
            self.assertIn("settings_notify_target_invalid", settings.warnings, value)

    def test_a_target_with_a_space_is_invalid(self):
        _, ok = validate_notify_target("telegram: 123")
        self.assertFalse(ok)

    def test_display_masks_the_chat_and_thread_ids(self):
        self.assertEqual(mask_notify_target("telegram:123456789:42"), "telegram:…6789:…")
        self.assertEqual(mask_notify_target("telegram:123456789"), "telegram:…6789")
        self.assertEqual(mask_notify_target(""), "local")
        self.assertEqual(mask_notify_target("local"), "local")

    def test_display_never_contains_the_full_chat_id(self):
        display = mask_notify_target("telegram:987654321012")
        self.assertNotIn("987654321012", display)
        self.assertIn("1012", display)


class EnumSettings(unittest.TestCase):
    def test_language_choices(self):
        for value in ("auto", "en", "pt-BR"):
            self.assertEqual(parse_settings({"language": value}).language, value)
        settings = parse_settings({"language": "pt_BR"})
        self.assertEqual(settings.language, "auto")
        self.assertIn("settings_language_invalid", settings.warnings)

    def test_mask_labels_defaults_on_and_warns_on_nonsense(self):
        self.assertTrue(parse_settings({}).mask_labels)
        settings = parse_settings({"mask_labels": "perhaps"})
        self.assertTrue(settings.mask_labels)
        self.assertIn("settings_mask_labels_invalid", settings.warnings)

    def test_mask_labels_accepts_only_a_real_bool(self):
        """D-013: a hand-edited ``"false"`` must not read as truthy."""
        for value in ("false", "0", "", 0, 1, None, [], {}):
            settings = parse_settings({"mask_labels": value})
            self.assertTrue(settings.mask_labels, repr(value))
            self.assertIn("settings_mask_labels_invalid", settings.warnings, repr(value))
        self.assertFalse(parse_settings({"mask_labels": False}).mask_labels)
        self.assertFalse(parse_settings({"mask_labels": False}).warnings)

    def test_booleans_accept_strings_and_numbers(self):
        self.assertTrue(parse_settings({"dry_run": "true"}).dry_run)
        self.assertTrue(parse_settings({"dry_run": 1}).dry_run)
        self.assertFalse(parse_settings({"dry_run": "off"}).dry_run)
        settings = parse_settings({"dry_run": "maybe"})
        self.assertFalse(settings.dry_run)
        self.assertIn("settings_dry_run_invalid", settings.warnings)

    def test_removed_switches_are_not_settings(self):
        for name in (
            "account_source",
            "automatic_rescue",
            "included_accounts",
            "backup_lead_minutes",
        ):
            self.assertNotIn(name, SETTING_NAMES)
            self.assertNotIn(name, config_schema())


class ExcludedAccounts(unittest.TestCase):
    """``excluded_accounts`` is one CSV, not a list textarea (D-012 §2)."""

    def test_a_csv_of_keys_is_kept_and_deduplicated(self):
        settings = parse_settings({"excluded_accounts": f"{'a' * 16},{'b' * 16},{'a' * 16}"})
        self.assertEqual(settings.excluded_accounts, ("a" * 16, "b" * 16))
        self.assertEqual(settings.warnings, ())

    def test_masked_labels_are_accepted_too(self):
        settings = parse_settings({"excluded_accounts": "b***@example.com"})
        self.assertEqual(settings.excluded_accounts, ("b***@example.com",))

    def test_an_empty_value_excludes_nothing(self):
        for value in ("", None, "   "):
            settings = parse_settings({"excluded_accounts": value})
            self.assertEqual(settings.excluded_accounts, (), value)
            self.assertNotIn("settings_excluded_accounts_invalid", settings.warnings, value)

    def test_a_list_is_still_accepted(self):
        settings = parse_settings({"excluded_accounts": ["a" * 16, "b***@example.com"]})
        self.assertEqual(settings.excluded_accounts, ("a" * 16, "b***@example.com"))

    def test_an_upper_case_key_is_normalized(self):
        settings = parse_settings({"excluded_accounts": "A" * 16})
        self.assertEqual(settings.excluded_accounts, ("a" * 16,))

    def test_too_many_entries_drop_the_whole_list(self):
        value = ",".join(f"{index:016x}" for index in range(MAX_EXCLUDED_ACCOUNTS + 1))
        settings = parse_settings({"excluded_accounts": value})
        self.assertEqual(settings.excluded_accounts, ())
        self.assertIn("settings_excluded_accounts_invalid", settings.warnings)

    def test_account_included_honours_keys_and_labels(self):
        settings = parse_settings({"excluded_accounts": f"{'a' * 16},b***@example.com"})
        self.assertFalse(settings.account_included("a" * 16))
        self.assertFalse(settings.account_included("c" * 16, "b***@example.com"))
        self.assertTrue(settings.account_included("c" * 16, "c***@example.com"))

    def test_an_empty_list_includes_everything(self):
        settings = parse_settings({})
        self.assertTrue(settings.account_included("a" * 16, "a***@example.com"))


class SettingsTable(unittest.TestCase):
    """The table is the contract: the form and the engine cannot drift."""

    def test_every_row_has_a_unique_name_and_its_own_warning_code(self):
        names = [spec.name for spec in SETTINGS_TABLE]
        self.assertEqual(len(names), len(set(names)))
        warns = [spec.warn for spec in SETTINGS_TABLE]
        self.assertEqual(len(warns), len(set(warns)))

    def test_setting_names_and_defaults_are_derived_from_the_table(self):
        self.assertEqual(SETTING_NAMES, tuple(spec.name for spec in SETTINGS_TABLE))
        for spec in SETTINGS_TABLE:
            self.assertEqual(DEFAULTS[spec.name], spec.default)

    def test_every_default_coerces_to_itself_without_a_warning(self):
        for spec in SETTINGS_TABLE:
            value, ok = spec.coerce(spec.default)
            self.assertTrue(ok, spec.name)
            self.assertEqual(value, spec.default, spec.name)

    def test_the_generated_config_schema_covers_every_setting(self):
        schema = config_schema()
        self.assertEqual(set(schema), set(SETTING_NAMES))
        for name, entry in schema.items():
            self.assertIn("type", entry, name)
            self.assertIn("label", entry, name)
            self.assertIn("description", entry, name)
            self.assertIn("default", entry, name)

    def test_the_schema_uses_only_types_the_form_understands(self):
        # D-030: account_overrides is a dict, edited per account, not a form control.
        for name, entry in config_schema().items():
            if name == "account_overrides":
                self.assertEqual(entry["type"], "dict")
                continue
            self.assertIn(entry["type"], ("int", "bool", "str", "list"))

    def test_enum_rows_publish_their_choices(self):
        schema = config_schema()
        self.assertEqual(schema["language"]["choices"], ["auto", "en", "pt-BR"])

    def test_the_table_is_the_nine_keys_plus_the_two_restored(self):
        # D-030 appends account_overrides after the restored keys.
        self.assertEqual(
            SETTING_NAMES,
            NINE_KEYS[:5]
            + AUTO_REDEEM_KEYS
            + NINE_KEYS[5:]
            + RESTORED_KEYS
            + ("account_overrides",),
        )

    def test_the_settings_keys_are_exactly_the_manifest_config_schema_keys(self):
        """T-71: ``settings.py`` and ``plugin.yaml`` cannot drift.

        The manifest is what the user's form shows; the table is what the
        engine coerces. A key in one and not the other is either a setting that
        silently does nothing or a value with no form field.
        """
        from pathlib import Path

        import yaml

        manifest = yaml.safe_load(
            (Path(__file__).resolve().parents[2] / "plugin.yaml").read_text(encoding="utf-8")
        )
        declared = set(manifest["config_schema"])
        self.assertEqual(set(SETTING_NAMES), declared)
        # D-030 adds account_overrides (a dict, per account) as the thirteenth row.
        self.assertEqual(len(SETTING_NAMES), 13)

    def test_the_generated_schema_matches_the_manifest_types_and_defaults(self):
        from pathlib import Path

        import yaml

        manifest = yaml.safe_load(
            (Path(__file__).resolve().parents[2] / "plugin.yaml").read_text(encoding="utf-8")
        )
        generated = config_schema()
        for name, declared in manifest["config_schema"].items():
            self.assertEqual(generated[name]["type"], declared["type"], f"{name}.type")
            self.assertEqual(generated[name]["default"], declared["default"], f"{name}.default")

    def test_parse_settings_ignores_unknown_keys(self):
        settings = parse_settings({"not_a_setting": 1, "lead_minutes": 20})
        self.assertEqual(settings.lead_minutes, 20)
        self.assertEqual(settings.warnings, ())

    def test_every_setting_is_reachable_as_an_attribute(self):
        settings = parse_settings({})
        for name in SETTING_NAMES:
            self.assertTrue(hasattr(settings, name), name)

    def test_the_old_threshold_name_still_parses(self):
        settings = parse_settings(
            {"early_redeem_enabled": True, "early_redeem_weekly_remaining_pct": 15}
        )
        self.assertEqual(settings.early_redeem_remaining_pct, 15)


class Reminders(unittest.TestCase):
    """D-007: two expiry reminders, each in hours before expiry or 0 for off."""

    def test_defaults_are_24h_and_1h(self):
        settings = parse_settings({})
        self.assertEqual(settings.reminder_1_hours, 24)
        self.assertEqual(settings.reminder_2_hours, 1)

    def test_offsets_are_in_seconds_and_ordered(self):
        settings = parse_settings({"reminder_1_hours": 24, "reminder_2_hours": 1})
        self.assertEqual(
            settings.reminder_offsets_s, (("reminder1", 86400.0), ("reminder2", 3600.0))
        )

    def test_zero_disables_a_reminder(self):
        settings = parse_settings({"reminder_1_hours": 0})
        self.assertEqual(settings.reminder_offsets_s, (("reminder2", 3600.0),))
        both_off = parse_settings({"reminder_1_hours": 0, "reminder_2_hours": 0})
        self.assertEqual(both_off.reminder_offsets_s, ())

    def test_only_the_second_reminder_can_be_kept(self):
        settings = parse_settings({"reminder_1_hours": 0, "reminder_2_hours": 2})
        self.assertEqual(settings.reminder_offsets_s, (("reminder2", 7200.0),))

    def test_out_of_range_reminders_fall_back_with_a_warning(self):
        settings = parse_settings({"reminder_1_hours": 500})
        self.assertEqual(settings.reminder_1_hours, 24)
        self.assertIn("settings_reminder_1_invalid", settings.warnings)
        settings = parse_settings({"reminder_2_hours": -1})
        self.assertEqual(settings.reminder_2_hours, 1)
        self.assertIn("settings_reminder_2_invalid", settings.warnings)

    def test_a_reminder_equal_to_the_lead_is_accepted(self):
        # lead 60 min = 1 h, so a 1-hour reminder lands exactly at the lead.
        settings = parse_settings({"lead_minutes": 60, "reminder_2_hours": 1})
        self.assertEqual(settings.reminder_2_hours, 1)
        self.assertNotIn("settings_reminder_before_lead", settings.warnings)

    def test_a_reminder_shorter_than_the_lead_is_reset(self):
        # lead 240 min = 4 h; a 1-hour reminder would fire after the redemption.
        settings = parse_settings({"lead_minutes": 240, "reminder_2_hours": 1})
        self.assertEqual(settings.reminder_2_hours, 1)
        self.assertIn("settings_reminder_before_lead", settings.warnings)


class EarlyRedemption(unittest.TestCase):
    """D-009: opt-in, off by default, triggered on weekly remaining percent."""

    def test_off_by_default(self):
        settings = parse_settings({})
        self.assertFalse(settings.early_redeem_enabled)
        self.assertIsNone(settings.early_redeem_threshold_pct)

    def test_enabling_exposes_the_threshold(self):
        settings = parse_settings({"early_redeem_enabled": True, "early_redeem_remaining_pct": 15})
        self.assertTrue(settings.early_redeem_enabled)
        self.assertEqual(settings.early_redeem_threshold_pct, 15.0)

    def test_zero_remaining_is_a_real_threshold_not_falsy(self):
        settings = parse_settings({"early_redeem_enabled": True, "early_redeem_remaining_pct": 0})
        self.assertEqual(settings.early_redeem_threshold_pct, 0.0)

    def test_a_disabled_threshold_is_none_even_with_a_value(self):
        settings = parse_settings({"early_redeem_enabled": False, "early_redeem_remaining_pct": 50})
        self.assertIsNone(settings.early_redeem_threshold_pct)

    def test_out_of_range_threshold_falls_back(self):
        for value in (101, -1, "x"):
            settings = parse_settings(
                {"early_redeem_enabled": True, "early_redeem_remaining_pct": value}
            )
            self.assertEqual(settings.early_redeem_remaining_pct, 0, value)
            self.assertIn("settings_early_redeem_pct_invalid", settings.warnings, value)

    def test_a_bad_enabled_flag_falls_back_to_off(self):
        settings = parse_settings({"early_redeem_enabled": "perhaps"})
        self.assertFalse(settings.early_redeem_enabled)
        self.assertIn("settings_early_redeem_invalid", settings.warnings)


class DerivedValues(unittest.TestCase):
    def test_offsets_are_in_seconds(self):
        settings = parse_settings({"lead_minutes": 10})
        self.assertEqual(settings.lead_seconds, 600.0)
        self.assertEqual(settings.backup_lead_seconds, 240.0)
        self.assertEqual(settings.primary_fire_offset_s, 600.0)
        self.assertEqual(settings.backup_fire_offset_s, 240.0)

    def test_scan_interval_seconds(self):
        self.assertEqual(parse_settings({"scan_interval_hours": 12}).scan_interval_seconds, 43200.0)

    def test_notify_target_display_masks(self):
        settings = parse_settings({"notify_target": "telegram:123456789"})
        self.assertEqual(settings.notify_target_display(), "telegram:…6789")


class DefaultsAndWarnings(unittest.TestCase):
    def test_an_empty_mapping_yields_every_default_with_no_warning(self):
        settings = parse_settings({})
        self.assertEqual(settings.scan_interval_hours, DEFAULTS["scan_interval_hours"])
        self.assertEqual(settings.lead_minutes, DEFAULTS["lead_minutes"])
        self.assertEqual(settings.effective_backup_lead_minutes, 4)
        self.assertEqual(settings.notify_target, DEFAULTS["notify_target"])
        self.assertEqual(settings.language, DEFAULTS["language"])
        self.assertTrue(settings.mask_labels)
        self.assertEqual(settings.warnings, ())

    def test_none_and_non_mappings_are_treated_as_empty(self):
        for value in (None, "nope", 42, []):
            self.assertEqual(parse_settings(value).warnings, (), value)

    def test_every_emitted_warning_is_a_known_code(self):
        settings = parse_settings(
            {
                "scan_interval_hours": 0,
                "lead_minutes": 0,
                "reminder_1_hours": -1,
                "reminder_2_hours": 999,
                "excluded_accounts": 42,
                "notify_target": "x",
                "early_redeem_enabled": "x",
                "early_redeem_remaining_pct": 500,
                "dry_run": "x",
                "mask_labels": "x",
                "language": "x",
            }
        )
        self.assertTrue(settings.warnings)
        for code in settings.warnings:
            self.assertIn(code, WARN_CODES)

    def test_require_returns_the_settings(self):
        settings = Settings()
        self.assertIs(settings.require(), settings)
        parse_settings({"scan_interval_hours": 0}).require()

    def test_with_included_keeps_every_other_value(self):
        base = parse_settings({"lead_minutes": 20})
        changed = base.with_included(["a" * 16])
        self.assertEqual(changed.lead_minutes, 20)
        self.assertEqual(changed.effective_backup_lead_minutes, 4)
        self.assertEqual(changed.included_accounts, ("a" * 16,))
        self.assertEqual(base.included_accounts, ())

    def test_with_excluded_keeps_every_other_value(self):
        base = parse_settings({"lead_minutes": 20, "dry_run": True})
        changed = base.with_excluded(["a" * 16])
        self.assertEqual(changed.lead_minutes, 20)
        self.assertTrue(changed.dry_run)
        self.assertEqual(changed.excluded_accounts, ("a" * 16,))

    def test_min_backup_gap_is_the_documented_three_minutes(self):
        self.assertEqual(MIN_BACKUP_GAP_MINUTES, 3)

    def test_settings_error_exists_for_require_style_callers(self):
        self.assertTrue(issubclass(SettingsError, Exception))


if __name__ == "__main__":
    unittest.main()
