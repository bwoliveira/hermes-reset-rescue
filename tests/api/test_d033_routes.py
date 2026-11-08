"""D-033: mounted route updates, immediate resets and safe failures."""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi import FastAPI

from tests.api.asgi_client import AsgiClient
from tests.api.test_r4_settings_race import KEY_A, KEY_B, LABEL_A, LABEL_B, PREFIX, _Base
from tests.fakes import temp_home

REPO = Path(__file__).resolve().parents[2]


class Resets(_Base):
    def test_global_reset_matches_manifest_defaults_and_preserves_private_and_account_keys(self):
        schema = yaml.safe_load((REPO / "plugin.yaml").read_text())["config_schema"]
        untouched = {
            "notify_target": "telegram:123456",
            "account_overrides": {KEY_A: {"label": LABEL_A, "monitored": False}},
            "excluded_accounts": [KEY_B],
        }
        self.write_config(
            {**untouched, "lead_minutes": 90, "language": "pt-BR", "mask_labels": False}
        )
        response = self.http.post(f"{PREFIX}/settings/reset", json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), self.http.get(f"{PREFIX}/settings").json())
        raw = yaml.safe_load((self.home / "config.yaml").read_text())["plugins"]["entries"][
            "reset-rescue"
        ]["settings"]
        for key, spec in schema.items():
            self.assertEqual(raw[key], untouched[key] if key in untouched else spec["default"], key)

    def test_account_reset_all_nulls_every_field_including_orphans_and_reenables_excluded(self):
        self.write_config(
            {
                "notify_target": "local",
                "lead_minutes": 90,
                "excluded_accounts": [KEY_A, KEY_B],
                "account_overrides": {
                    KEY_A: {"label": LABEL_A, "monitored": False, "auto_redeem_enabled": False},
                    KEY_B: {"label": LABEL_B, "reminders_enabled": False},
                    "c" * 16: {"label": "c***@e***.com", "early_redeem_remaining_pct": 30},
                },
            }
        )
        response = self.http.post(f"{PREFIX}/settings/account/reset-all", json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), self.http.get(f"{PREFIX}/settings").json())
        settings = self.read_settings()
        self.assertEqual(settings.excluded_accounts, ())
        raw = yaml.safe_load((self.home / "config.yaml").read_text())["plugins"]["entries"][
            "reset-rescue"
        ]["settings"]
        self.assertEqual(raw["excluded_accounts"], [])
        from reset_rescue.settings import OVERRIDE_FIELDS

        for entry in raw["account_overrides"].values():
            for field in OVERRIDE_FIELDS:
                self.assertIsNone(entry[field])
        self.assertEqual(settings.lead_minutes, 90)
        self.assertEqual(settings.notify_target, "local")
        for key in (KEY_A, KEY_B, "c" * 16):
            self.assertEqual(settings.account_override_fields(key), {})
        self.assertNotIn("alice@", (self.home / "config.yaml").read_text())

    def test_all_post_failures_are_fixed_json_with_detail_and_never_echo_inputs(self):
        cases = [
            ("/settings", {"key": "alice@example.com", "value": "private-token"}),
            ("/settings/account", {"account_key": KEY_A, "field": "bad", "value": "private-token"}),
            ("/settings/reset", {"unknown": "alice@example.com"}),
            ("/settings/account/reset-all", {"unknown": "private-token"}),
            ("/scan", {"unknown": "private-token"}),
            ("/redeem", {}),
            ("/missing", {}),
        ]
        for path, body in cases:
            with self.subTest(path=path):
                response = self.http.post(f"{PREFIX}{path}", json=body)
                self.assertGreaterEqual(response.status_code, 400)
                self.assertIn("error", response.json())
                if path != "/missing":
                    self.assertIsInstance(response.json().get("detail"), str)
                self.assertNotIn("alice@example.com", response.text)
                self.assertNotIn("private-token", response.text)
        unknown = self.http.get(f"{PREFIX}/missing")
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unknown.json(), {"error": "not_found"})

    def test_reset_write_failure_is_safe_json(self):
        with patch(
            "hermes_cli.plugins_settings.save_plugin_setting",
            side_effect=OSError("alice@example.com private-token"),
        ):
            response = self.http.post(f"{PREFIX}/settings/account/reset-all", json={})
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(response.json()["error"], "internal_error")
        self.assertIsInstance(response.json()["detail"], str)
        self.assertNotIn("alice@", response.text)
        self.assertNotIn("private-token", response.text)

    def test_reset_all_serializes_with_per_account_save_and_does_not_restore_old_values(self):
        from hermes_cli import plugins_settings

        self.write_config(
            {
                "excluded_accounts": [KEY_A],
                "account_overrides": {
                    KEY_A: {"label": LABEL_A, "auto_redeem_enabled": True},
                    KEY_B: {"label": LABEL_B, "monitored": False},
                },
            }
        )
        entered = threading.Event()
        release = threading.Event()
        overlap = threading.Event()
        results = {}
        real = plugins_settings.save_plugin_setting

        def save(plugin_id, segments, value):
            if segments == ("excluded_accounts",):
                entered.set()
                release.wait(3)
                try:
                    return real(plugin_id, segments, value)
                finally:
                    entered.clear()
            if entered.is_set():
                overlap.set()
            return real(plugin_id, segments, value)

        def request(name, path, body):
            client = AsgiClient(self.http._app)
            try:
                results[name] = client.post(f"{PREFIX}{path}", json=body).status_code
            finally:
                client.close()

        with patch.object(plugins_settings, "save_plugin_setting", side_effect=save):
            reset = threading.Thread(
                target=request, args=("reset", "/settings/account/reset-all", {})
            )
            edit = threading.Thread(
                target=request,
                args=(
                    "edit",
                    "/settings/account",
                    {"account_key": KEY_A, "field": "auto_redeem_enabled", "value": False},
                ),
            )
            reset.start()
            try:
                self.assertTrue(entered.wait(2), "reset must reach the writer")
                edit.start()
                overlap.wait(0.15)
            finally:
                release.set()
                reset.join(10)
                if edit.ident is not None:
                    edit.join(10)
        self.assertFalse(overlap.is_set(), "all writes share the profile lock")
        self.assertEqual(results, {"reset": 200, "edit": 200})
        settings = self.read_settings()
        self.assertEqual(settings.excluded_accounts, ())
        self.assertEqual(settings.account_override_fields(KEY_B), {})
        self.assertFalse(settings.for_account(KEY_A).auto_redeem_enabled)


class MountedRouteReload(unittest.TestCase):
    def test_new_and_changed_handlers_are_live_without_rebuilding_the_app(self):
        home = temp_home("d033-router-")
        root = home / "plugin"
        (root / "dashboard").mkdir(parents=True)
        path = root / "dashboard/plugin_api.py"
        shutil.copy2(REPO / "dashboard/plugin_api.py", path)
        name = "hermes_dashboard_plugin_d033_hot"
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        app = FastAPI()
        app.include_router(module.router, prefix=PREFIX)
        http = AsgiClient(app)
        try:
            before = http.get(f"{PREFIX}/brand-new")
            self.assertEqual(before.status_code, 404)
            original_routes = tuple(app.router.routes)
            with path.open("a") as stream:
                stream.write(
                    "\nasync def d033_marker(request):\n"
                    '    return JSONResponse({"marker": "updated"})\n'
                    '_HANDLERS[("GET", "/brand-new")] = d033_marker\n'
                    '_HANDLERS[("POST", "/brand-new")] = d033_marker\n'
                    '_HANDLERS[("GET", "/settings")] = d033_marker\n'
                )
            stamp = path.stat().st_mtime_ns + 2_000_000_000
            os.utime(path, ns=(stamp, stamp))
            for route in ("/brand-new", "/settings"):
                response = http.get(f"{PREFIX}{route}")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json(), {"marker": "updated"})
            self.assertEqual(http.post(f"{PREFIX}/brand-new", json={}).status_code, 200)
            self.assertIs(module._handler_module._SAVE_LOCKS, module._SAVE_LOCKS)
            self.assertIs(module._handler_module._SAVE_LOCKS_GUARD, module._SAVE_LOCKS_GUARD)
            # A second, same-sized update uses mtime, not Python's timestamp pyc.
            source = path.read_text().replace('"updated"', '"changed"')
            path.write_text(source)
            os.utime(path, ns=(stamp + 2_000_000_000, stamp + 2_000_000_000))
            self.assertEqual(http.get(f"{PREFIX}/brand-new").json(), {"marker": "changed"})
            # A partial/bad install fails as sanitized JSON, then recovers.
            path.write_text(source + '\nraise RuntimeError("alice@example.com private-token")\n')
            failure = http.post(f"{PREFIX}/brand-new", json={})
            self.assertEqual(failure.status_code, 500)
            self.assertEqual(failure.json()["error"], "internal_error")
            self.assertIn("detail", failure.json())
            self.assertNotIn("alice@", failure.text)
            self.assertNotIn("private-token", failure.text)
            path.write_text(source)
            self.assertEqual(http.post(f"{PREFIX}/brand-new", json={}).status_code, 200)
            self.assertEqual(tuple(app.router.routes), original_routes)
        finally:
            http.close()
            for key in [key for key in sys.modules if key.startswith(name)]:
                sys.modules.pop(key, None)
