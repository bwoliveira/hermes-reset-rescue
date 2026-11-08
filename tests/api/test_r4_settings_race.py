"""R4 review fixes for the D-030 per-account save path (P1-01, P1-02, P2 response).

* Two concurrent per-account POSTs must not undo each other's values (P1-01).
* A reset racing an edit of another account must not undo that edit (P1-01).
* With ``mask_labels`` off, edit / clear / reset must never write a full e-mail
  to ``config.yaml``, with the masking off or on again (P1-02).
* The POST response must describe the value that was just saved (P2).

The real Hermes writer, the real router and a real temp ``HERMES_HOME`` are used;
accounts and the Codex backend are fakes.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi import FastAPI

from reset_rescue.authorize import authorize_consume
from reset_rescue.service import Service
from reset_rescue.settings import parse_settings
from tests.api.asgi_client import AsgiClient
from tests.fakes import FakeStore, make_deps, temp_home
from tests.support.isolation import env_for

REPO = Path(__file__).resolve().parents[2]
API_FILE = REPO / "dashboard" / "plugin_api.py"
MODULE_NAME = "hermes_dashboard_plugin_reset-rescue_r4"
PREFIX = "/api/plugins/reset-rescue"
KEY_A = "a" * 16
KEY_B = "b" * 16
LABEL_A = "a***@e***.com"
LABEL_B = "b***@e***.com"
FULL_A = "alice@example.com"
BARRIER_TIMEOUT_S = 2.0


def load_api():
    spec = importlib.util.spec_from_file_location(MODULE_NAME, API_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


API = load_api()


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.home = temp_home("r4-settings-")
        (self.home / "config.yaml").write_text(
            "plugins:\n  entries:\n    reset-rescue:\n      settings:\n        lead_minutes: 10\n",
            encoding="utf-8",
        )
        self._saved = {k: os.environ.get(k) for k in ("HERMES_HOME", "TMPDIR")}
        os.environ.update(env_for(self.home))
        self.store = FakeStore()
        self.add_account(KEY_A, LABEL_A)
        self.add_account(KEY_B, LABEL_B)
        self.mask_labels = True
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

    def add_account(self, key: str, label: str) -> None:
        self.store.account_upsert(
            {
                "account_key": key,
                "label": label,
                "source_kind": "pool",
                "last_list_ok_at": 1_800_000_000.0,
                "last_error_code": None,
            }
        )

    def _factory(self):
        from reset_rescue.host import read_plugin_settings

        deps = make_deps(store=self.store, settings=parse_settings(read_plugin_settings()))
        full = {KEY_A: FULL_A}
        source = getattr(deps, "source", None)
        if source is not None:
            source.full_label = lambda key: full.get(key, "")
        return Service(deps)

    def write_config(self, settings: dict) -> None:
        data = {
            "plugins": {"entries": {"reset-rescue": {"settings": {"lead_minutes": 10, **settings}}}}
        }
        (self.home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")

    def read_settings(self):
        from reset_rescue.host import read_plugin_settings

        return parse_settings(read_plugin_settings())

    def post_account(self, key: str, field: str, value):
        return self.http.post(
            f"{PREFIX}/settings/account", json={"account_key": key, "field": field, "value": value}
        )

    def barrier_save(self, count: int = 2):
        """Patch the Hermes writer so ``count`` requests reach the save together.

        Every request has already computed what it will write before it reaches
        the barrier. Without a lock the two saves interleave and the second one
        restores the value the first one changed. The barrier times out instead
        of hanging, because a correct implementation serialises the saves.
        """
        from hermes_cli import plugins_settings

        real = plugins_settings.save_plugin_settings
        barrier = threading.Barrier(count)

        def save(plugin_id, plugin_dir, values):
            try:
                barrier.wait(timeout=BARRIER_TIMEOUT_S)
            except threading.BrokenBarrierError:
                pass
            return real(plugin_id, plugin_dir, values)

        return patch.object(plugins_settings, "save_plugin_settings", side_effect=save)


class ConcurrentSaves(_Base):
    def test_two_auto_off_saves_for_two_accounts_both_stay_off(self):
        self.write_config(
            {
                "account_overrides": {
                    KEY_A: {"label": LABEL_A, "auto_redeem_enabled": True},
                    KEY_B: {"label": LABEL_B, "auto_redeem_enabled": True},
                }
            }
        )
        results: dict[str, int] = {}

        def request(key: str) -> None:
            client = AsgiClient(self._app())
            try:
                results[key] = client.post(
                    f"{PREFIX}/settings/account",
                    json={"account_key": key, "field": "auto_redeem_enabled", "value": False},
                ).status_code
            finally:
                client.close()

        with self.barrier_save():
            threads = [threading.Thread(target=request, args=(k,)) for k in (KEY_A, KEY_B)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

        self.assertEqual(results, {KEY_A: 200, KEY_B: 200})
        settings = self.read_settings()
        self.assertFalse(settings.for_account(KEY_A, LABEL_A).auto_redeem_enabled)
        self.assertFalse(settings.for_account(KEY_B, LABEL_B).auto_redeem_enabled)
        for key, label in ((KEY_A, LABEL_A), (KEY_B, LABEL_B)):
            decision = authorize_consume(
                role="primary",
                settings=settings,
                credit=_credit(),
                now=1_800_000_000.0,
                account_key=key,
                label=label,
            )
            self.assertFalse(decision.allowed, key)

    def _app(self):
        app = FastAPI()
        app.include_router(API.router, prefix=PREFIX)
        return app

    def test_reset_of_one_account_does_not_undo_an_edit_of_another(self):
        # B's reminders are explicitly On, so a reset of A that read the old map
        # would write B's On back over the edit that turns them Off.
        self.write_config(
            {
                "account_overrides": {
                    KEY_A: {"label": LABEL_A, "monitored": False},
                    KEY_B: {"label": LABEL_B, "reminders_enabled": True},
                }
            }
        )
        results: dict[str, int] = {}

        def reset_a() -> None:
            client = AsgiClient(self._app())
            try:
                results["reset"] = client.post(
                    f"{PREFIX}/settings/account/reset", json={"account_key": KEY_A}
                ).status_code
            finally:
                client.close()

        def edit_b() -> None:
            client = AsgiClient(self._app())
            try:
                results["edit"] = client.post(
                    f"{PREFIX}/settings/account",
                    json={"account_key": KEY_B, "field": "reminders_enabled", "value": False},
                ).status_code
            finally:
                client.close()

        with self.barrier_save():
            threads = [threading.Thread(target=reset_a), threading.Thread(target=edit_b)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

        self.assertEqual(results, {"reset": 200, "edit": 200})
        settings = self.read_settings()
        self.assertFalse(settings.for_account(KEY_B, LABEL_B).reminders_enabled)
        self.assertEqual(settings.account_override_fields(KEY_A, LABEL_A), {})


class MaskLabelsOff(_Base):
    def _assert_no_full_email(self) -> None:
        raw = (self.home / "config.yaml").read_bytes()
        self.assertNotIn(FULL_A.encode(), raw)
        self.assertNotIn(b"alice@", raw)

    def test_edit_clear_and_reset_persist_no_full_email_with_masking_off_then_on(self):
        self.write_config({"mask_labels": False})
        self.assertEqual(self.post_account(KEY_A, "auto_redeem_enabled", False).status_code, 200)
        self._assert_no_full_email()

        self.assertEqual(self.post_account(KEY_A, "auto_redeem_enabled", None).status_code, 200)
        self._assert_no_full_email()

        response = self.http.post(f"{PREFIX}/settings/account/reset", json={"account_key": KEY_A})
        self.assertEqual(response.status_code, 200, response.text)
        self._assert_no_full_email()

        self.write_config({"mask_labels": True})
        self.assertEqual(self.post_account(KEY_A, "auto_redeem_enabled", False).status_code, 200)
        self._assert_no_full_email()
        self.assertEqual(self.post_account(KEY_A, "auto_redeem_enabled", None).status_code, 200)
        self._assert_no_full_email()
        response = self.http.post(f"{PREFIX}/settings/account/reset", json={"account_key": KEY_A})
        self.assertEqual(response.status_code, 200, response.text)
        self._assert_no_full_email()


class FreshResponse(_Base):
    def test_post_response_shows_the_value_it_just_saved(self):
        response = self.post_account(KEY_A, "auto_redeem_enabled", False)
        self.assertEqual(response.status_code, 200, response.text)
        accounts = {item["account_key"]: item for item in response.json()["status"]["accounts"]}
        self.assertFalse(accounts[KEY_A]["effective"]["auto_redeem_enabled"])


def _credit():
    from types import SimpleNamespace

    return SimpleNamespace(expires_at=1_800_000_300.0)


if __name__ == "__main__":
    unittest.main()
