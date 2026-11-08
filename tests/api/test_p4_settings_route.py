"""D-025: the popup's settings routes (GET + POST /settings).

The Desktop form (Capabilities > Plugins > Reset Rescue) cannot translate its
labels and always has a Save button, so the popup grows its own Settings tab.
It needs two routes:

* ``GET /settings`` — every ``config_schema`` key with its type, bounds,
  choices and current value, so the tab renders the right control without a
  second copy of the schema in JavaScript;
* ``POST /settings`` — ``{"key", "value"}``, validated by
  ``reset_rescue.settings`` and written with Hermes' own
  ``hermes_cli.plugins_settings.save_plugin_settings`` (never by hand), then
  the fresh ``StatusDTO`` so the popup refreshes in the same round trip.

These tests use a real temporary ``HERMES_HOME``: the point is that the value
lands in ``config.yaml`` through the same writer the Hermes form uses.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path

import yaml
from fastapi import FastAPI

from reset_rescue.settings import SETTING_NAMES
from tests.api.asgi_client import AsgiClient
from tests.fakes import temp_home
from tests.support.isolation import env_for

REPO = Path(__file__).resolve().parents[2]
API_FILE = REPO / "dashboard" / "plugin_api.py"
MODULE_NAME = "hermes_dashboard_plugin_reset-rescue"


def load_api():
    spec = importlib.util.spec_from_file_location(MODULE_NAME, API_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


API = load_api()


def client() -> AsgiClient:
    app = FastAPI()
    app.include_router(API.router, prefix="/api/plugins/reset-rescue")
    return AsgiClient(app)


class TempProfile(unittest.TestCase):
    """A throwaway profile whose ``config.yaml`` the real writer edits."""

    def setUp(self) -> None:
        self.home = temp_home("p4-settings-")
        (self.home / "config.yaml").write_text(
            "plugins:\n  entries:\n    reset-rescue:\n      settings:\n        lead_minutes: 10\n",
            encoding="utf-8",
        )
        self._saved = {k: os.environ.get(k) for k in ("HERMES_HOME", "TMPDIR")}
        os.environ.update(env_for(self.home))
        self.http = client()

    def tearDown(self) -> None:
        self.http.close()
        API.set_service_factory(None)
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def written(self) -> dict:
        data = yaml.safe_load((self.home / "config.yaml").read_text(encoding="utf-8")) or {}
        return (
            ((data.get("plugins") or {}).get("entries") or {})
            .get("reset-rescue", {})
            .get("settings", {})
        )


class SettingsRead(TempProfile):
    def test_get_settings_lists_every_schema_key_with_its_control(self):
        response = self.http.get("/api/plugins/reset-rescue/settings")
        self.assertEqual(response.status_code, 200, response.text)
        fields = response.json()["fields"]
        # D-030: account_overrides is edited per account, never as a form field.
        self.assertEqual(
            [field["key"] for field in fields],
            [name for name in SETTING_NAMES if name != "account_overrides"],
        )
        by_key = {field["key"]: field for field in fields}
        self.assertEqual(by_key["auto_redeem_enabled"]["type"], "bool")
        self.assertEqual(by_key["lead_minutes"]["type"], "int")
        self.assertEqual(by_key["lead_minutes"]["min"], 5)
        self.assertEqual(by_key["lead_minutes"]["max"], 240)
        self.assertEqual(by_key["language"]["type"], "enum")
        self.assertEqual(by_key["language"]["choices"], ["auto", "en", "pt-BR"])
        self.assertEqual(by_key["notify_target"]["type"], "str")

    def test_get_settings_reports_the_current_value(self):
        response = self.http.get("/api/plugins/reset-rescue/settings")
        by_key = {field["key"]: field for field in response.json()["fields"]}
        self.assertEqual(by_key["lead_minutes"]["value"], 10)
        self.assertEqual(by_key["auto_redeem_enabled"]["value"], True)

    def test_get_settings_never_names_a_profile(self):
        response = self.http.get("/api/plugins/reset-rescue/settings?profile=other")
        self.assertEqual(response.status_code, 200)


class SettingsWrite(TempProfile):
    def test_a_value_is_written_through_hermes_own_writer(self):
        response = self.http.post(
            "/api/plugins/reset-rescue/settings", json={"key": "lead_minutes", "value": 20}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.written()["lead_minutes"], 20)

    def test_the_response_carries_the_fresh_status(self):
        response = self.http.post(
            "/api/plugins/reset-rescue/settings",
            json={"key": "auto_redeem_enabled", "value": False},
        )
        body = response.json()
        self.assertEqual(body["key"], "auto_redeem_enabled")
        self.assertEqual(body["value"], False)
        self.assertIn("status", body)
        self.assertEqual(body["status"]["auto_redeem_enabled"], False)

    def test_a_boolean_is_written_as_a_boolean(self):
        self.http.post(
            "/api/plugins/reset-rescue/settings", json={"key": "mask_labels", "value": False}
        )
        self.assertIs(self.written()["mask_labels"], False)

    def test_an_unknown_key_is_422_and_writes_nothing(self):
        response = self.http.post(
            "/api/plugins/reset-rescue/settings", json={"key": "not_a_key", "value": 1}
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json(),
            {
                "error": "invalid_setting",
                "message_key": "errors.invalid_setting",
                "detail": "Invalid setting.",
            },
        )
        self.assertNotIn("not_a_key", self.written())

    def test_a_wrong_type_is_422_and_writes_nothing(self):
        response = self.http.post(
            "/api/plugins/reset-rescue/settings", json={"key": "lead_minutes", "value": "twenty"}
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.written()["lead_minutes"], 10)

    def test_an_out_of_range_number_is_422(self):
        for value in (0, 241, -5):
            response = self.http.post(
                "/api/plugins/reset-rescue/settings", json={"key": "lead_minutes", "value": value}
            )
            self.assertEqual(response.status_code, 422, value)
        self.assertEqual(self.written()["lead_minutes"], 10)

    def test_an_enum_outside_the_choices_is_422(self):
        response = self.http.post(
            "/api/plugins/reset-rescue/settings", json={"key": "language", "value": "de"}
        )
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("language", self.written())

    def test_a_bad_notify_target_is_422(self):
        response = self.http.post(
            "/api/plugins/reset-rescue/settings",
            json={"key": "notify_target", "value": "not a target"},
        )
        self.assertEqual(response.status_code, 422)

    def test_an_unknown_body_field_is_422(self):
        response = self.http.post(
            "/api/plugins/reset-rescue/settings",
            json={"key": "lead_minutes", "value": 20, "extra": 1},
        )
        self.assertEqual(response.status_code, 422)

    def test_the_body_never_carries_a_profile(self):
        response = self.http.post(
            "/api/plugins/reset-rescue/settings",
            json={"key": "lead_minutes", "value": 20, "profile": "other"},
        )
        self.assertEqual(response.status_code, 422)

    def test_a_managed_key_refusal_is_403_not_a_crash(self):
        from unittest import mock

        with mock.patch(
            "hermes_cli.plugins_settings.save_plugin_settings",
            side_effect=PermissionError("administrator-managed"),
        ):
            response = self.http.post(
                "/api/plugins/reset-rescue/settings", json={"key": "lead_minutes", "value": 20}
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json(),
            {
                "error": "settings_read_only",
                "message_key": "errors.settings_read_only",
                "detail": "Settings are read-only.",
            },
        )

    def test_a_writer_failure_is_a_fixed_500_without_the_exception_text(self):
        from unittest import mock

        with mock.patch(
            "hermes_cli.plugins_settings.save_plugin_settings",
            side_effect=RuntimeError("boom: could not write the file"),
        ):
            response = self.http.post(
                "/api/plugins/reset-rescue/settings", json={"key": "lead_minutes", "value": 20}
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json(),
            {
                "error": "internal_error",
                "message_key": "errors.internal_error",
                "detail": "The operation failed.",
            },
        )
        self.assertNotIn("boom", response.text)


class RouteTable(unittest.TestCase):
    def test_the_settings_routes_are_declared(self):
        served = sorted(
            (method, route.path) for route in API.router.routes for method in route.methods
        )
        self.assertIn(("GET", "/settings"), served)
        self.assertIn(("POST", "/settings"), served)
        self.assertEqual(served, sorted(API.ROUTES))


if __name__ == "__main__":
    unittest.main()
