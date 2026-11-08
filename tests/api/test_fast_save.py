"""Fast single-field saves: one Hermes round-trip, authoritative responses."""

from __future__ import annotations

from unittest.mock import patch

from hermes_cli import plugins_settings

from tests.api.test_r4_settings_race import API, KEY_A, LABEL_A, PREFIX, _Base


class FastSave(_Base):
    def test_single_field_and_unchanged_label_use_one_writer_call(self):
        self.write_config({"account_overrides": {KEY_A: {"label": LABEL_A}}})
        real = plugins_settings.save_plugin_settings
        with (
            patch.object(plugins_settings, "save_plugin_settings", wraps=real) as save,
            patch.object(
                plugins_settings, "save_plugin_setting", wraps=plugins_settings.save_plugin_setting
            ) as leaf,
        ):
            response = self.post_account(KEY_A, "auto_redeem_enabled", False)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(save.call_count, 1)
        self.assertEqual(leaf.call_count, 1)
        delta = save.call_args.args[2]["account_overrides"][KEY_A]
        self.assertEqual(delta, {"auto_redeem_enabled": False})
        self.assert_account_matches_get(response.json())

    def test_new_masked_label_and_field_share_one_writer_call(self):
        real = plugins_settings.save_plugin_settings
        with patch.object(plugins_settings, "save_plugin_settings", wraps=real) as save:
            response = self.post_account(KEY_A, "reminders_enabled", False)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(save.call_count, 1)
        self.assertEqual(
            save.call_args.args[2]["account_overrides"][KEY_A],
            {"label": LABEL_A, "reminders_enabled": False},
        )
        self.assert_account_matches_get(response.json())

    def test_single_field_save_builds_only_one_service(self):
        real = API._build_service
        with patch.object(API, "_build_service", wraps=real) as build:
            response = self.post_account(KEY_A, "early_redeem_enabled", True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(build.call_count, 1)
        self.assert_account_matches_get(response.json())

    def test_null_leaf_is_one_delta_and_response_inherits_global(self):
        self.write_config(
            {
                "auto_redeem_enabled": False,
                "account_overrides": {
                    KEY_A: {"label": LABEL_A, "auto_redeem_enabled": True, "monitored": False}
                },
            }
        )
        response = self.post_account(KEY_A, "auto_redeem_enabled", None)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertNotIn("auto_redeem_enabled", body["overrides"])
        self.assertFalse(body["effective"]["auto_redeem_enabled"])
        self.assertIs(body["overrides"]["monitored"], False)
        self.assert_account_matches_get(body)

    def test_batched_delta_preserves_leaf_managed_key_refusal(self):
        path = (
            f"plugins.entries.reset-rescue.settings.account_overrides.{KEY_A}.auto_redeem_enabled"
        )
        with (
            patch("hermes_cli.managed_scope.is_key_managed", side_effect=lambda key: key == path),
            patch.object(plugins_settings, "save_plugin_settings") as save,
        ):
            response = self.post_account(KEY_A, "auto_redeem_enabled", False)
        self.assertEqual(response.status_code, 403, response.text)
        save.assert_not_called()

    def test_reset_response_matches_the_authoritative_read(self):
        self.write_config(
            {"account_overrides": {KEY_A: {"label": LABEL_A, "auto_redeem_enabled": False}}}
        )
        response = self.http.post(f"{PREFIX}/settings/account/reset", json={"account_key": KEY_A})
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_account_matches_get(response.json())
        self.assertEqual(response.json()["overrides"], {})

    def test_reset_does_not_rewrite_already_materialized_null_fields(self):
        from reset_rescue.settings import OVERRIDE_FIELDS

        entry = dict.fromkeys(OVERRIDE_FIELDS)
        entry.update(label=LABEL_A, auto_redeem_enabled=False)
        self.write_config({"account_overrides": {KEY_A: entry}})
        with patch.object(
            plugins_settings, "save_plugin_setting", wraps=plugins_settings.save_plugin_setting
        ) as save:
            response = self.http.post(
                f"{PREFIX}/settings/account/reset", json={"account_key": KEY_A}
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(save.call_count, 1)
        self.assert_account_matches_get(response.json())

    def test_global_one_field_uses_one_writer_and_matches_get(self):
        real = plugins_settings.save_plugin_settings
        with patch.object(plugins_settings, "save_plugin_settings", wraps=real) as save:
            response = self.http.post(
                f"{PREFIX}/settings", json={"key": "auto_redeem_enabled", "value": False}
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(save.call_count, 1)
        self.assertFalse(response.json()["status"]["auto_redeem_enabled"])
        body = self.http.get(f"{PREFIX}/settings").json()
        field = next(f for f in body["fields"] if f["key"] == "auto_redeem_enabled")
        self.assertEqual(response.json()["value"], field["value"])

    def assert_account_matches_get(self, body):
        current = self.http.get(f"{PREFIX}/settings").json()
        row = next(a for a in current["accounts"] if a["account_key"] == KEY_A)
        self.assertEqual(body["account_key"], KEY_A)
        self.assertEqual(body["overrides"], row["overrides"])
        self.assertEqual(body["effective"], row["effective"])
        status_row = next(a for a in body["status"]["accounts"] if a["account_key"] == KEY_A)
        self.assertEqual(status_row["effective"], row["effective"])
