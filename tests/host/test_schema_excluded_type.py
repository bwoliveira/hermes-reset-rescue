"""The real Hermes schema validator accepts the stored exclusion-list shape."""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml
from hermes_cli.plugins_manifest import validate_config_schema

from reset_rescue.settings import config_schema, parse_settings, settings_fields, validate_setting

REPO = Path(__file__).resolve().parents[2]
KEY = "a" * 16
LABEL = "a***@e***.com"


class ExcludedSchemaType(unittest.TestCase):
    def setUp(self):
        self.schema = yaml.safe_load((REPO / "plugin.yaml").read_text())["config_schema"]

    def test_stored_lists_validate_without_a_hermes_warning(self):
        for value in ([], [KEY], [KEY, LABEL]):
            with self.subTest(value=value):
                self.assertEqual(
                    validate_config_schema(
                        "reset-rescue", self.schema, {"excluded_accounts": value}
                    ),
                    [],
                )

    def test_manifest_default_and_generated_schema_are_list_shaped(self):
        self.assertEqual(self.schema["excluded_accounts"]["type"], "list")
        self.assertEqual(self.schema["excluded_accounts"]["default"], [])
        self.assertEqual(config_schema()["excluded_accounts"], self.schema["excluded_accounts"])

    def test_legacy_csv_and_lists_keep_the_same_exclusions(self):
        for value in (f"{KEY},{LABEL}", [KEY, LABEL]):
            with self.subTest(value=value):
                settings = parse_settings({"excluded_accounts": value})
                self.assertEqual(settings.excluded_accounts, (KEY, LABEL))
                self.assertEqual(settings.warnings, ())

    def test_popup_csv_is_stored_as_a_list_and_shown_as_text(self):
        key, stored = validate_setting("excluded_accounts", f"{KEY},{LABEL}")
        self.assertEqual(stored, [KEY, LABEL])
        self.assertEqual(validate_config_schema("reset-rescue", self.schema, {key: stored}), [])
        field = next(row for row in settings_fields({key: stored}) if row["key"] == key)
        self.assertEqual(field["type"], "str")
        self.assertEqual(field["value"], f"{KEY},{LABEL}")
