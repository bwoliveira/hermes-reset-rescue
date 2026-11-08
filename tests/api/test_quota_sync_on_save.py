"""Settings writes re-arm or disarm the quota job right away (D-009, D-030).

The save routes schedule ``HostService.sync_quota_job`` after the write. The
sync runs in a worker thread and never sits on the response path, so a slow or
failing cron call cannot delay or fail a save.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi import FastAPI

from reset_rescue.host import QUOTA_JOB_NAME, HostService
from reset_rescue.planner import Planner
from reset_rescue.settings import parse_settings
from tests.api.asgi_client import AsgiClient
from tests.fakes import FakeCron, FakeStore, make_deps, temp_home
from tests.support.isolation import env_for

REPO = Path(__file__).resolve().parents[2]
API_FILE = REPO / "dashboard" / "plugin_api.py"
MODULE_NAME = "hermes_dashboard_plugin_reset-rescue_quota_sync"
PREFIX = "/api/plugins/reset-rescue"
KEY_A = "a" * 16
LABEL_A = "b***@e***.com"


def load_api():
    spec = importlib.util.spec_from_file_location(MODULE_NAME, API_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


API = load_api()


class _Store(FakeStore):
    """FakeStore with the list call the planner uses."""

    def list_credits(self, *, account_key=None, statuses=None):
        return self.credit_list(account_key=account_key)


class _BlockingHost(HostService):
    """A real HostService whose quota sync waits on a gate first."""

    gate = threading.Event()

    def sync_quota_job(self) -> None:
        self.gate.wait(5)
        super().sync_quota_job()


class _FailingHost(HostService):
    def sync_quota_job(self) -> None:
        raise RuntimeError("cron exploded")


class SaveSyncBase(unittest.TestCase):
    host_class = HostService

    def setUp(self) -> None:
        self.home = temp_home("quota-sync-")
        (self.home / "config.yaml").write_text(
            "plugins:\n  entries:\n    reset-rescue:\n      settings:\n        lead_minutes: 10\n",
            encoding="utf-8",
        )
        self._saved = {k: os.environ.get(k) for k in ("HERMES_HOME", "TMPDIR")}
        os.environ.update(env_for(self.home))
        self.store = _Store()
        self.store.account_upsert(
            {
                "account_key": KEY_A,
                "label": LABEL_A,
                "source_kind": "pool",
                "last_list_ok_at": 1_800_000_000.0,
                "last_error_code": None,
            }
        )
        self.cron = FakeCron()
        self.add_credit("seen")
        API.set_service_factory(self._factory)
        app = FastAPI()
        app.include_router(API.router, prefix=PREFIX)
        self.http = AsgiClient(app)

    def tearDown(self) -> None:
        self.drain()
        self.http.close()
        API.set_service_factory(None)
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def add_credit(self, status: str) -> None:
        self.store.credit_upsert(
            {
                "credit_key": "c" * 16,
                "account_key": KEY_A,
                "status": status,
                "expires_at": 1_900_000_000.0,
            }
        )

    def _factory(self):
        from reset_rescue.host import read_plugin_settings

        deps = make_deps(
            store=self.store,
            settings=parse_settings(read_plugin_settings()),
            cron=self.cron,
        )
        deps.planner = Planner(deps, self.store)
        return self.host_class(deps)

    def drain(self) -> None:
        """Wait for scheduled quota syncs; they run on the client's loop."""
        tasks = list(getattr(API, "_quota_sync_tasks", ()))
        if tasks:
            self.http._loop.run_until_complete(asyncio.wait(tasks))

    def quota_job_present(self) -> bool:
        self.drain()
        return any(job["name"] == QUOTA_JOB_NAME for job in self.cron.jobs.values())

    def post_account(self, field, value):
        return self.http.post(
            f"{PREFIX}/settings/account",
            json={"account_key": KEY_A, "field": field, "value": value},
        )

    def post_setting(self, key, value):
        return self.http.post(f"{PREFIX}/settings", json={"key": key, "value": value})

    def write_config(self, settings: dict) -> None:
        data = {
            "plugins": {"entries": {"reset-rescue": {"settings": {"lead_minutes": 10, **settings}}}}
        }
        (self.home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


class AccountOverrideSync(SaveSyncBase):
    def test_account_early_on_with_global_off_arms_the_job_right_after_the_save(self):
        self.write_config({})
        response = self.post_account("early_redeem_enabled", True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(self.quota_job_present())

    def test_account_early_back_to_null_removes_the_job(self):
        self.write_config({})
        self.post_account("early_redeem_enabled", True)
        self.assertTrue(self.quota_job_present())
        response = self.post_account("early_redeem_enabled", None)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(self.quota_job_present())

    def test_monitored_false_disarms_and_null_rearms(self):
        self.write_config({"early_redeem_enabled": True})
        self.post_setting("early_redeem_enabled", True)
        self.assertTrue(self.quota_job_present())
        response = self.post_account("monitored", False)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(self.quota_job_present())
        response = self.post_account("monitored", None)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(self.quota_job_present())

    def test_account_threshold_save_reconciles_an_existing_early_setting(self):
        self.write_config({"account_overrides": {KEY_A: {"early_redeem_enabled": True}}})
        response = self.post_account("early_redeem_remaining_pct", 25)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(self.quota_job_present())

    def test_account_reset_removes_the_job(self):
        self.write_config({})
        self.post_account("early_redeem_enabled", True)
        self.assertTrue(self.quota_job_present())
        response = self.http.post(f"{PREFIX}/settings/account/reset", json={"account_key": KEY_A})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(self.quota_job_present())

    def test_account_reset_all_removes_the_job(self):
        self.write_config({})
        self.post_account("early_redeem_enabled", True)
        self.assertTrue(self.quota_job_present())
        response = self.http.post(f"{PREFIX}/settings/account/reset-all", json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(self.quota_job_present())


class GlobalSettingSync(SaveSyncBase):
    def test_global_early_on_arms_and_off_removes_the_job(self):
        self.write_config({})
        response = self.post_setting("early_redeem_enabled", True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(self.quota_job_present())
        response = self.post_setting("early_redeem_enabled", False)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(self.quota_job_present())

    def test_exclusions_disarm_and_clear_rearms(self):
        self.write_config({"early_redeem_enabled": True})
        self.post_setting("early_redeem_enabled", True)
        self.assertTrue(self.quota_job_present())
        response = self.post_setting("excluded_accounts", KEY_A)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(self.quota_job_present())
        response = self.post_setting("excluded_accounts", "")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(self.quota_job_present())

    def test_global_threshold_save_reconciles_an_existing_early_setting(self):
        self.write_config({"early_redeem_enabled": True})
        response = self.post_setting("early_redeem_remaining_pct", 25)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(self.quota_job_present())

    def test_account_reset_all_clears_exclusions_and_rearms(self):
        self.write_config({"early_redeem_enabled": True, "excluded_accounts": [KEY_A]})
        self.post_setting("early_redeem_enabled", True)
        self.assertFalse(self.quota_job_present())
        response = self.http.post(f"{PREFIX}/settings/account/reset-all", json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(self.quota_job_present())

    def test_global_reset_disarms_an_armed_job(self):
        self.write_config({"early_redeem_enabled": True})
        self.drain()
        self.assertFalse(self.quota_job_present())  # not armed until a save
        self.post_setting("early_redeem_enabled", True)
        self.assertTrue(self.quota_job_present())
        response = self.http.post(f"{PREFIX}/settings/reset", json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(self.quota_job_present())


class SaveNeverWaitsForTheSync(SaveSyncBase):
    host_class = _BlockingHost

    def setUp(self) -> None:
        _BlockingHost.gate = threading.Event()
        super().setUp()

    def tearDown(self) -> None:
        _BlockingHost.gate.set()
        super().tearDown()

    def test_the_save_returns_while_the_quota_sync_is_still_blocked(self):
        self.write_config({})
        started = time.monotonic()
        response = self.post_account("early_redeem_enabled", True)
        elapsed = time.monotonic() - started
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(_BlockingHost.gate.is_set())
        self.assertLess(elapsed, 4.0, "the save waited for the quota sync")
        _BlockingHost.gate.set()
        self.assertTrue(self.quota_job_present())


class SaveOrderingAndProfile(SaveSyncBase):
    def test_sync_keeps_the_request_profile_context(self):
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        scoped = temp_home("quota-sync-profile-")
        (scoped / "config.yaml").write_text(
            "plugins:\n  entries:\n    reset-rescue:\n      settings: {}\n", encoding="utf-8"
        )
        app = self.http._app

        @app.middleware("http")
        async def profile_scope(request, call_next):
            token = set_hermes_home_override(str(scoped))
            try:
                return await call_next(request)
            finally:
                reset_hermes_home_override(token)

        response = self.post_account("early_redeem_enabled", True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(self.quota_job_present(), "the sync read the launch profile instead")
        launch = yaml.safe_load((self.home / "config.yaml").read_text(encoding="utf-8"))
        self.assertNotIn(
            "account_overrides", launch["plugins"]["entries"]["reset-rescue"]["settings"]
        )

    def test_timeout_syncs_only_after_the_write_finishes(self):
        gate = threading.Event()
        entered = threading.Event()
        original = API._write_setting

        def delayed_write(key, value):
            entered.set()
            if not gate.wait(5):
                raise RuntimeError("write was not released")
            return original(key, value)

        with (
            patch.object(API, "_write_setting", delayed_write),
            patch.object(API, "STATUS_TIMEOUT_S", 0.05),
        ):
            try:
                response = self.post_setting("early_redeem_enabled", True)
                self.assertEqual(response.status_code, 504, response.text)
                self.assertTrue(entered.is_set())
                self.assertFalse(self.quota_job_present())
            finally:
                gate.set()
                pending = asyncio.all_tasks(self.http._loop)
                if pending:
                    self.http._loop.run_until_complete(asyncio.wait(pending))
            self.assertTrue(self.quota_job_present())

    def test_unrelated_settings_do_not_schedule_a_quota_sync(self):
        self.write_config({"early_redeem_enabled": True})
        response = self.post_setting("lead_minutes", 20)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(self.quota_job_present())
        response = self.post_account("auto_redeem_enabled", False)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(self.quota_job_present())

    def test_executor_shutdown_is_logged_without_an_unhandled_task_error(self):
        with (
            self.assertLogs(API._log.name, level="WARNING") as logs,
            patch.object(
                API.asyncio, "to_thread", side_effect=RuntimeError("private executor detail")
            ),
        ):
            self.http._loop.run_until_complete(API._sync_quota_in_thread())
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.assertNotIn("private executor detail", "\n".join(logs.output))

    def test_failed_write_does_not_schedule_a_quota_sync(self):
        with patch.object(API, "_write_setting", side_effect=ValueError("invalid")):
            response = self.post_setting("early_redeem_enabled", True)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertFalse(self.quota_job_present())


class SyncFailureNeverFailsTheSave(SaveSyncBase):
    host_class = _FailingHost

    def test_a_broken_sync_still_saves_and_answers_200(self):
        self.write_config({})
        with self.assertLogs(API._log.name, level="WARNING") as logs:
            response = self.post_account("early_redeem_enabled", True)
            self.assertEqual(response.status_code, 200, response.text)
            self.drain()
        self.assertIn("RuntimeError", "\n".join(logs.output))
        self.assertNotIn("cron exploded", "\n".join(logs.output))
        self.assertFalse(self.quota_job_present())
        saved = yaml.safe_load((self.home / "config.yaml").read_text(encoding="utf-8"))
        overrides = saved["plugins"]["entries"]["reset-rescue"]["settings"]["account_overrides"]
        self.assertTrue(overrides[KEY_A]["early_redeem_enabled"])


if __name__ == "__main__":
    unittest.main()
