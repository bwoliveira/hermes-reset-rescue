"""D-030: per-account overrides in the settings layer.

Pins the parsing rules for ``account_overrides`` (types, bounds, unknown
fields, unknown account keys, the label fallback that keeps a rebuilt database
matching), the effective value of each overridable field, and the D-028
exclusion path, which now reads as ``monitored: false``.
"""

from __future__ import annotations

import unittest

from reset_rescue.settings import config_schema, parse_settings, settings_fields

KEY_A = "a" * 16
KEY_B = "b" * 16
KEY_OTHER = "e" * 16
LABEL_A = "b***@e***.com"
LABEL_B = "c***@f***.com"
WARN = "settings_account_overrides_invalid"


class OverrideParsing(unittest.TestCase):
    def test_default_is_an_empty_mapping(self):
        self.assertEqual(dict(parse_settings({}).account_overrides), {})

    def test_a_valid_entry_is_kept_without_warning(self):
        raw = {KEY_A: {"label": LABEL_A, "auto_redeem_enabled": False}}
        settings = parse_settings({"account_overrides": raw})
        self.assertEqual(settings.account_overrides[KEY_A]["auto_redeem_enabled"], False)
        self.assertNotIn(WARN, settings.warnings)

    def test_an_unknown_field_is_dropped_with_a_warning(self):
        raw = {KEY_A: {"label": LABEL_A, "lead_minutes": 5, "auto_redeem_enabled": False}}
        settings = parse_settings({"account_overrides": raw})
        self.assertNotIn("lead_minutes", settings.account_overrides[KEY_A])
        self.assertFalse(settings.account_overrides[KEY_A]["auto_redeem_enabled"])
        self.assertIn(WARN, settings.warnings)

    def test_a_wrong_type_is_ignored_with_a_warning(self):
        raw = {KEY_A: {"label": LABEL_A, "auto_redeem_enabled": "false"}}
        settings = parse_settings({"account_overrides": raw})
        self.assertNotIn("auto_redeem_enabled", settings.account_overrides[KEY_A])
        self.assertIn(WARN, settings.warnings)

    def test_an_out_of_bounds_percentage_is_ignored_with_a_warning(self):
        raw = {KEY_A: {"label": LABEL_A, "early_redeem_remaining_pct": 101}}
        settings = parse_settings({"account_overrides": raw})
        self.assertNotIn("early_redeem_remaining_pct", settings.account_overrides[KEY_A])
        self.assertIn(WARN, settings.warnings)

    def test_a_malformed_account_key_is_dropped_with_a_warning(self):
        raw = {"not-a-key": {"label": LABEL_A, "monitored": False}}
        settings = parse_settings({"account_overrides": raw})
        self.assertNotIn("not-a-key", settings.account_overrides)
        self.assertIn(WARN, settings.warnings)

    def test_a_non_mapping_entry_is_dropped_with_a_warning(self):
        settings = parse_settings({"account_overrides": {KEY_A: "off"}})
        self.assertNotIn(KEY_A, settings.account_overrides)
        self.assertIn(WARN, settings.warnings)

    def test_an_unknown_account_key_is_kept_and_does_not_touch_other_accounts(self):
        raw = {KEY_OTHER: {"label": "z***@z***.com", "auto_redeem_enabled": False}}
        settings = parse_settings({"account_overrides": raw})
        self.assertIn(KEY_OTHER, settings.account_overrides)
        self.assertTrue(settings.for_account(KEY_A, LABEL_A).auto_redeem_enabled)

    def test_the_schema_declares_a_dict_with_an_empty_default(self):
        entry = config_schema()["account_overrides"]
        self.assertEqual(entry["type"], "dict")
        self.assertEqual(entry["default"], {})
        self.assertTrue(entry["description"])

    def test_the_overrides_are_not_a_form_control(self):
        self.assertNotIn("account_overrides", [field["key"] for field in settings_fields({})])


class EffectiveValues(unittest.TestCase):
    def test_a_missing_override_follows_the_global_value(self):
        settings = parse_settings(
            {
                "auto_redeem_enabled": False,
                "early_redeem_enabled": True,
                "early_redeem_weekly_remaining_pct": 20,
            }
        )
        effective = settings.for_account(KEY_A, LABEL_A)
        self.assertFalse(effective.auto_redeem_enabled)
        self.assertTrue(effective.early_redeem_enabled)
        self.assertEqual(effective.early_redeem_remaining_pct, 20)
        self.assertTrue(effective.reminders_enabled)

    def test_an_override_wins_and_the_global_value_is_unchanged(self):
        settings = parse_settings(
            {
                "account_overrides": {
                    KEY_A: {
                        "label": LABEL_A,
                        "auto_redeem_enabled": False,
                        "early_redeem_enabled": True,
                        "early_redeem_remaining_pct": 35,
                        "reminders_enabled": False,
                    }
                }
            }
        )
        effective = settings.for_account(KEY_A, LABEL_A)
        self.assertFalse(effective.auto_redeem_enabled)
        self.assertTrue(effective.early_redeem_enabled)
        self.assertEqual(effective.early_redeem_remaining_pct, 35)
        self.assertFalse(effective.reminders_enabled)
        self.assertTrue(settings.auto_redeem_enabled)

    def test_another_account_keeps_the_global_value(self):
        settings = parse_settings(
            {"account_overrides": {KEY_A: {"label": LABEL_A, "auto_redeem_enabled": False}}}
        )
        self.assertTrue(settings.for_account(KEY_B, LABEL_B).auto_redeem_enabled)

    def test_an_old_masked_label_does_not_match_when_the_key_changed(self):
        """R4: overrides apply by key only. A rebuilt DB (new salt) misses the key.

        The account then follows the global setting.
        """
        settings = parse_settings(
            {"account_overrides": {KEY_OTHER: {"label": "b***@gmail.com", "monitored": False}}}
        )
        self.assertEqual(settings.account_overrides[KEY_OTHER]["label"], "b***@g***.com")
        self.assertTrue(settings.account_included(KEY_A, "b***@g***.com"))

    def test_a_new_masked_label_does_not_match_another_key(self):
        """R4: a masked label never selects an override; the key alone does."""
        settings = parse_settings(
            {"account_overrides": {KEY_OTHER: {"label": "b***@g***.com", "monitored": False}}}
        )
        self.assertTrue(settings.account_included(KEY_A, "b***@g***.com"))


class MonitoredAndExclusion(unittest.TestCase):
    def test_monitored_false_excludes_the_account_with_global_on(self):
        settings = parse_settings(
            {"account_overrides": {KEY_A: {"label": LABEL_A, "monitored": False}}}
        )
        self.assertFalse(settings.account_included(KEY_A, LABEL_A))
        self.assertTrue(settings.account_included(KEY_B, LABEL_B))

    def test_an_explicit_monitored_true_beats_the_excluded_accounts_list(self):
        settings = parse_settings(
            {
                "excluded_accounts": LABEL_A,
                "account_overrides": {KEY_A: {"label": LABEL_A, "monitored": True}},
            }
        )
        self.assertTrue(settings.account_included(KEY_A, LABEL_A))

    def test_excluded_accounts_still_excludes_without_an_override(self):
        settings = parse_settings({"excluded_accounts": LABEL_A})
        self.assertFalse(settings.account_included(KEY_A, LABEL_A))
        self.assertTrue(settings.account_included(KEY_B, LABEL_B))


class ValidateOverride(unittest.TestCase):
    def test_a_valid_field_and_value_pass(self):
        from reset_rescue.settings import validate_override

        self.assertEqual(
            validate_override(KEY_A, "early_redeem_remaining_pct", 40),
            ("early_redeem_remaining_pct", 40),
        )
        self.assertEqual(validate_override(KEY_A, "monitored", False), ("monitored", False))

    def test_null_is_the_clear_value(self):
        from reset_rescue.settings import validate_override

        self.assertEqual(validate_override(KEY_A, "monitored", None), ("monitored", None))

    def test_a_global_only_field_is_refused(self):
        from reset_rescue.settings import validate_override

        for field in ("lead_minutes", "notify_target", "language"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_override(KEY_A, field, 5)

    def test_a_wrong_type_is_refused(self):
        from reset_rescue.settings import validate_override

        with self.assertRaises(ValueError):
            validate_override(KEY_A, "monitored", "yes")
        with self.assertRaises(ValueError):
            validate_override(KEY_A, "early_redeem_remaining_pct", True)

    def test_bounds_are_enforced(self):
        from reset_rescue.settings import validate_override

        with self.assertRaises(ValueError):
            validate_override(KEY_A, "early_redeem_remaining_pct", 101)
        with self.assertRaises(ValueError):
            validate_override(KEY_A, "early_redeem_remaining_pct", -1)

    def test_a_malformed_account_key_is_refused(self):
        from reset_rescue.settings import validate_override

        with self.assertRaises(ValueError):
            validate_override("NOT-A-KEY", "monitored", False)
