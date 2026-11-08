"""GET /settings carries the Global defaults that POST /settings/reset writes.

The popup's "Restore defaults" applies these at once on its confirming click
(like every other control) and then reconciles with the reply, so the payload
must name exactly the fields the reset changes, with exactly the values it
writes, and nothing private (no notify_target).
"""

from __future__ import annotations

import copy
import unittest

import yaml

from tests.api.test_r4_settings_race import KEY_A, KEY_B, LABEL_A, PREFIX, _Base

# Every Global key away from its default, plus the keys the reset preserves.
NON_DEFAULT = {
    "scan_interval_hours": 3,
    "lead_minutes": 90,
    "reminder_1_hours": 48,
    "reminder_2_hours": 5,
    "notify_target": "telegram:123456",
    "auto_redeem_enabled": False,
    "early_redeem_enabled": True,
    "early_redeem_remaining_pct": 25,
    "excluded_accounts": [KEY_B],
    "dry_run": True,
    "mask_labels": False,
    "language": "pt-BR",
    "account_overrides": {KEY_A: {"label": LABEL_A, "monitored": False}},
}


class SettingsDefaults(_Base):
    def raw(self) -> dict:
        data = yaml.safe_load((self.home / "config.yaml").read_text())
        return data["plugins"]["entries"]["reset-rescue"]["settings"]

    def test_defaults_name_exactly_what_the_reset_writes(self):
        self.write_config(copy.deepcopy(NON_DEFAULT))
        before = self.raw()
        got = self.http.get(f"{PREFIX}/settings")
        self.assertEqual(got.status_code, 200, got.text)
        defaults = got.json().get("defaults")
        self.assertIsInstance(defaults, dict, "GET /settings has no defaults")

        reset = self.http.post(f"{PREFIX}/settings/reset", json={})
        self.assertEqual(reset.status_code, 200, reset.text)
        after = self.raw()

        changed = {key for key in after if before.get(key) != after.get(key)}
        self.assertTrue(changed, "the fixture must make the reset change something")
        self.assertEqual(set(defaults), changed, "defaults name exactly the changed fields")
        for key, value in defaults.items():
            self.assertEqual(after[key], value, key)
            self.assertEqual(type(after[key]), type(value), key)

    def test_defaults_carry_nothing_private_or_per_account(self):
        self.write_config(copy.deepcopy(NON_DEFAULT))
        defaults = self.http.get(f"{PREFIX}/settings").json()["defaults"]
        for key in ("notify_target", "account_overrides", "excluded_accounts"):
            self.assertNotIn(key, defaults)
        text = yaml.safe_dump(defaults)
        self.assertNotIn("telegram", text)
        self.assertNotIn(KEY_A, text)

    def test_defaults_are_static_and_cover_every_global_field_shown(self):
        self.write_config({})
        first = self.http.get(f"{PREFIX}/settings").json()
        self.write_config(copy.deepcopy(NON_DEFAULT))
        second = self.http.get(f"{PREFIX}/settings").json()
        self.assertEqual(first["defaults"], second["defaults"], "defaults do not follow config")
        shown = {field["key"] for field in second["fields"]}
        self.assertLessEqual(set(second["defaults"]), shown, "every default is a Global row")
        # The rows the reset leaves alone are exactly the preserved ones.
        self.assertEqual(shown - set(second["defaults"]), {"notify_target", "excluded_accounts"})

    def test_reset_reply_carries_the_same_defaults(self):
        self.write_config(copy.deepcopy(NON_DEFAULT))
        defaults = self.http.get(f"{PREFIX}/settings").json()["defaults"]
        reply = self.http.post(f"{PREFIX}/settings/reset", json={}).json()
        self.assertEqual(reply["defaults"], defaults)
        values = {field["key"]: field["value"] for field in reply["fields"]}
        for key, value in defaults.items():
            self.assertEqual(values[key], value, key)


if __name__ == "__main__":
    unittest.main()
