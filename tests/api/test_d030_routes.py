"""D-030 API: per-account settings routes.

* ``GET /settings`` also lists the accounts with their overrides and effective values;
* ``POST /settings/account`` sets or clears one field and writes ``config.yaml``
  through Hermes' own writer, in a temporary HERMES_HOME;
* ``POST /settings/account/reset`` removes the whole entry;
* invalid input is a 400, an account that is not in the pool is a 404.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path

import yaml
from fastapi import FastAPI

from reset_rescue.service import Service
from reset_rescue.settings import parse_settings
from tests.api.asgi_client import AsgiClient
from tests.fakes import FakeStore, make_deps, temp_home
from tests.support.isolation import env_for

REPO = Path(__file__).resolve().parents[2]
API_FILE = REPO / "dashboard" / "plugin_api.py"
MODULE_NAME = "hermes_dashboard_plugin_reset-rescue"
PREFIX = "/api/plugins/reset-rescue"
KEY_A = "a" * 16
KEY_B = "c" * 16
KEY_UNKNOWN = "d" * 16
LABEL_A = "b***@e***.com"
LABEL_B = "c***@f***.com"


def load_api():
    spec = importlib.util.spec_from_file_location(MODULE_NAME, API_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


API = load_api()


class PerAccountRoutes(unittest.TestCase):
    def setUp(self) -> None:
        self.home = temp_home("d030-routes-")
        (self.home / "config.yaml").write_text(
            "plugins:\n  entries:\n    reset-rescue:\n      settings:\n        lead_minutes: 10\n",
            encoding="utf-8",
        )
        self._saved = {k: os.environ.get(k) for k in ("HERMES_HOME", "TMPDIR")}
        os.environ.update(env_for(self.home))
        self.store = FakeStore()
        self.store.account_upsert(
            {
                "account_key": KEY_A,
                "label": LABEL_A,
                "source_kind": "pool",
                "last_list_ok_at": 1_800_000_000.0,
                "last_error_code": None,
            }
        )
        API.set_service_factory(self._factory)
        app = FastAPI()
        app.include_router(API.router, prefix=PREFIX)
        self.http = AsgiClient(app)

    def tearDown(self) -> None:
        self.http.close()
        API.set_service_factory(None)
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _factory(self):
        from reset_rescue.host import read_plugin_settings

        return Service(make_deps(store=self.store, settings=parse_settings(read_plugin_settings())))

    def write_config(self, settings: dict) -> None:
        data = {
            "plugins": {"entries": {"reset-rescue": {"settings": {"lead_minutes": 10, **settings}}}}
        }
        (self.home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")

    def written_overrides(self) -> dict:
        data = yaml.safe_load((self.home / "config.yaml").read_text(encoding="utf-8")) or {}
        entry = ((data.get("plugins") or {}).get("entries") or {}).get("reset-rescue", {})
        return (entry.get("settings") or {}).get("account_overrides", {})

    def post_account(self, field, value):
        return self.http.post(
            f"{PREFIX}/settings/account",
            json={"account_key": KEY_A, "field": field, "value": value},
        )


class ReadRoute(PerAccountRoutes):
    def test_get_settings_lists_accounts_with_overrides_and_effective(self):
        self.write_config({"account_overrides": {KEY_A: {"label": LABEL_A, "monitored": True}}})
        response = self.http.get(f"{PREFIX}/settings")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["account_overrides"][KEY_A]["monitored"], True)
        [account] = body["accounts"]
        self.assertEqual(account["account_key"], KEY_A)
        self.assertEqual(account["label"], LABEL_A)
        self.assertEqual(account["overrides"], {"monitored": True})
        self.assertIn("auto_redeem_enabled", account["effective"])


class WriteRoute(PerAccountRoutes):
    def test_post_account_sets_one_field_and_writes_config_yaml(self):
        response = self.post_account("auto_redeem_enabled", False)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("accounts", response.json()["status"])
        entry = self.written_overrides()[KEY_A]
        self.assertIs(entry["auto_redeem_enabled"], False)
        self.assertEqual(entry["label"], LABEL_A)

    def test_null_clears_one_field_and_keeps_the_others(self):
        self.write_config(
            {
                "account_overrides": {
                    KEY_A: {"label": LABEL_A, "monitored": False, "reminders_enabled": False}
                }
            }
        )
        response = self.post_account("reminders_enabled", None)
        self.assertEqual(response.status_code, 200, response.text)
        entry = self.written_overrides()[KEY_A]
        # The writer deep-merges, so a cleared leaf is kept on disk as null (= follow global).
        self.assertIsNone(entry["reminders_enabled"])
        self.assertIs(entry["monitored"], False)

    def test_reset_removes_the_whole_entry(self):
        self.write_config({"account_overrides": {KEY_A: {"label": LABEL_A, "monitored": False}}})
        response = self.http.post(f"{PREFIX}/settings/account/reset", json={"account_key": KEY_A})
        self.assertEqual(response.status_code, 200, response.text)
        # Every field is nulled (= follows global); the account has no active override.
        entry = self.written_overrides()[KEY_A]
        self.assertTrue(all(entry[name] is None for name in ("monitored",)))


class InvalidInput(PerAccountRoutes):
    def test_an_unknown_field_is_400(self):
        self.assertEqual(self.post_account("lead_minutes", 5).status_code, 400)

    def test_a_wrong_value_type_is_400(self):
        self.assertEqual(self.post_account("monitored", "yes").status_code, 400)

    def test_an_out_of_bounds_value_is_400(self):
        self.assertEqual(self.post_account("early_redeem_remaining_pct", 101).status_code, 400)

    def test_a_malformed_account_key_is_400(self):
        response = self.http.post(
            f"{PREFIX}/settings/account",
            json={"account_key": "NOT-A-KEY", "field": "monitored", "value": False},
        )
        self.assertEqual(response.status_code, 400)

    def test_an_account_not_in_the_pool_is_404(self):
        response = self.http.post(
            f"{PREFIX}/settings/account",
            json={"account_key": KEY_UNKNOWN, "field": "monitored", "value": False},
        )
        self.assertEqual(response.status_code, 404)

    def test_a_reset_for_a_malformed_key_is_400(self):
        response = self.http.post(f"{PREFIX}/settings/account/reset", json={"account_key": "x"})
        self.assertEqual(response.status_code, 400)


class SingleAccountRule(PerAccountRoutes):
    """Contract: ``account_count`` counts pool accounts (paused included);
    ``per_account_available`` is ``account_count >= 2`` or any listed account
    holds an override. Overrides are honored with one account too."""

    def add_second_account(self):
        self.store.account_upsert(
            {
                "account_key": KEY_B,
                "label": LABEL_B,
                "source_kind": "pool",
                "last_list_ok_at": 1_800_000_000.0,
                "last_error_code": None,
            }
        )

    def settings_body(self):
        response = self.http.get(f"{PREFIX}/settings")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def status_body(self):
        response = self.http.get(f"{PREFIX}/status")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        return {
            "pool_account_count": body["pool_account_count"],
            "per_account_available": body["per_account_available"],
        }

    def test_one_account_without_override(self):
        for body in (self.settings_body(), self.status_body()):
            self.assertEqual(body["pool_account_count"], 1)
            self.assertFalse(body["per_account_available"])

    def test_one_account_with_override_still_honors_it(self):
        self.write_config(
            {"account_overrides": {KEY_A: {"label": LABEL_A, "auto_redeem_enabled": False}}}
        )
        for body in (self.settings_body(), self.status_body()):
            self.assertEqual(body["pool_account_count"], 1)
            self.assertTrue(body["per_account_available"])
        self.assertFalse(self.settings_body()["accounts"][0]["effective"]["auto_redeem_enabled"])

    def test_two_accounts_without_override(self):
        self.add_second_account()
        for body in (self.settings_body(), self.status_body()):
            self.assertEqual(body["pool_account_count"], 2)
            self.assertTrue(body["per_account_available"])
