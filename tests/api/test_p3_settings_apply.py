"""P3: a saved setting reaches the next status read, with nothing restarted.

The maintainer turned masking off and switched the language in Desktop →
Capabilities → Plugins → Reset Rescue, and neither the popup nor the plugin
changed. The save path itself is Hermes core (`plugins.manage settings` →
`hermes_cli.plugins_settings.save_plugin_settings` → `config.yaml`), and it was
verified working against a copy of the real profile. What this module pins is
our half of the contract:

* the plugin reads `plugins.entries.reset-rescue.settings` fresh on every
  call — no import-time snapshot, no cache — so the very next `/status` sees a
  value written after the process started;
* the dashboard route builds a new service per request, so a save is visible on
  the next poll without a restart;
* every key the form can write is a key the engine actually reads (schema and
  engine cannot drift), which is what makes "the setting does nothing"
  impossible for a key that is in the form.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

from fastapi import FastAPI

from reset_rescue import host as host_module
from reset_rescue.settings import SETTING_NAMES, config_schema, parse_settings
from tests.api.asgi_client import AsgiClient
from tests.fakes import FakeStore, make_deps

REPO = Path(__file__).resolve().parents[2]
API_FILE = REPO / "dashboard" / "plugin_api.py"
MODULE_NAME = "hermes_dashboard_plugin_reset-rescue"
KEY_A = "a" * 16
CREDIT = "c" * 16


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


def seeded_store() -> FakeStore:
    store = FakeStore()
    store.account_upsert(
        {
            "account_key": KEY_A,
            "label": "b***@e***.com",
            "source_kind": "pool",
            "last_list_ok_at": 1_800_000_000.0,
            "last_error_code": None,
        }
    )
    store.credit_upsert(
        {
            "credit_key": CREDIT,
            "account_key": KEY_A,
            "title": "Full reset",
            "reset_type": "codex",
            "granted_at": 1_799_000_000.0,
            "expires_at": 1_800_100_000.0,
            "backend_state": "available",
            "status": "available",
            "plan": "beyond_horizon",
            "last_result_code": None,
            "last_result_at": None,
            "last_result_reason": None,
            "notified_outcome": None,
            "notified_success": None,
        }
    )
    return store


class FreshSettingsPerRequest(unittest.TestCase):
    """The engine never memoises a settings read (A§8.7)."""

    def setUp(self) -> None:
        self.store = seeded_store()
        self.raw: dict = {}

    def tearDown(self) -> None:
        API.set_service_factory(None)

    def _factory(self):
        from reset_rescue.service import Service

        deps = make_deps(store=self.store, settings=parse_settings(self.raw))
        return Service(deps)

    def test_a_setting_written_after_start_is_seen_by_the_next_status(self):
        API.set_service_factory(self._factory)
        c = client()
        before = c.get("/api/plugins/reset-rescue/status").json()
        self.assertTrue(before["settings"]["mask_labels"], "default is masked")
        self.assertEqual(before["language"], "auto")

        # Exactly what the Desktop form's save lands in config.yaml.
        self.raw.update({"mask_labels": False, "language": "pt-BR"})

        after = c.get("/api/plugins/reset-rescue/status").json()
        self.assertFalse(after["settings"]["mask_labels"], "the save must reach the popup")
        self.assertEqual(after["language"], "pt-BR")

    def test_the_engine_gets_the_same_values_the_form_wrote(self):
        self.raw.update({"mask_labels": False, "language": "pt-BR", "scan_interval_hours": 6})
        service = self._factory()
        settings = service.settings()
        self.assertFalse(settings.mask_labels)
        self.assertEqual(settings.language, "pt-BR")
        self.assertEqual(settings.scan_interval_hours, 6)

    def test_no_module_level_cache_holds_the_old_value(self):
        """A second build in the same process must not answer from the first."""
        self.raw.update({"mask_labels": True})
        first = self._factory().settings()
        self.raw.update({"mask_labels": False})
        second = self._factory().settings()
        self.assertTrue(first.mask_labels)
        self.assertFalse(second.mask_labels)


class ReadPath(unittest.TestCase):
    """``host.read_plugin_settings`` reads the live config, fail-soft."""

    def test_it_reads_the_plugin_entry(self):
        from unittest import mock

        config = {"plugins": {"entries": {"reset-rescue": {"settings": {"mask_labels": False}}}}}
        with mock.patch("hermes_cli.config.load_config_readonly", return_value=config):
            self.assertEqual(host_module.read_plugin_settings(), {"mask_labels": False})

    def test_a_broken_config_is_an_empty_mapping_not_a_crash(self):
        from unittest import mock

        with mock.patch("hermes_cli.config.load_config_readonly", side_effect=RuntimeError("boom")):
            self.assertEqual(host_module.read_plugin_settings(), {})

    def test_a_missing_entry_is_empty(self):
        from unittest import mock

        with mock.patch("hermes_cli.config.load_config_readonly", return_value={"plugins": {}}):
            self.assertEqual(host_module.read_plugin_settings(), {})


class FormAndEngineAgree(unittest.TestCase):
    """Every form key is a key the engine coerces, with the same default."""

    def test_the_generated_schema_matches_the_table(self):
        generated = config_schema()
        self.assertEqual(set(generated), set(SETTING_NAMES))

    def test_the_manifest_matches_the_generated_schema(self):
        import yaml

        manifest = yaml.safe_load((REPO / "plugin.yaml").read_text(encoding="utf-8"))
        declared = manifest["config_schema"]
        self.assertEqual(set(declared), set(SETTING_NAMES))
        for key, entry in config_schema().items():
            self.assertEqual(declared[key]["type"], entry["type"], key)
            self.assertEqual(declared[key]["default"], entry["default"], key)
            self.assertEqual(declared[key]["label"], entry["label"], key)

    def test_every_key_changes_the_engine(self):
        """A form key that parses to the same value is a setting that does nothing."""
        defaults = parse_settings({})
        probes = {
            "scan_interval_hours": 6,
            "lead_minutes": 20,
            "reminder_1_hours": 12,
            "reminder_2_hours": 2,
            "notify_target": "local",
            "auto_redeem_enabled": False,
            "early_redeem_enabled": True,
            "early_redeem_remaining_pct": 40,
            "excluded_accounts": KEY_A,
            "mask_labels": False,
            "language": "pt-BR",
            "dry_run": True,
        }
        # D-030: account_overrides is not a form key; it is probed by test_d030_settings.
        self.assertEqual(set(probes), set(SETTING_NAMES) - {"account_overrides"})
        for key, value in probes.items():
            changed = parse_settings({key: value})
            self.assertNotEqual(
                getattr(changed, key), getattr(defaults, key), f"{key} did not change the engine"
            )
