"""T-28 / T-32: dashboard/plugin_api.py through real HTTP (FastAPI TestClient).

The module is loaded exactly the way Hermes loads it: by file location under
``hermes_dashboard_plugin_reset-rescue``, with no package context. The service
is the real ``reset_rescue.service.Service`` over ``tests.fakes`` collaborators.

Run: python3 <runtime wrapper> scripts/run_tests.py api
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
import threading
import time
import unittest
from pathlib import Path

from fastapi import FastAPI

from reset_rescue import dto as dto_module
from reset_rescue.service import Service
from reset_rescue.store import LockTimeout
from tests.api.asgi_client import AsgiClient
from tests.fakes import FakeStore, make_deps, make_settings

REPO = Path(__file__).resolve().parents[2]
API_FILE = REPO / "dashboard" / "plugin_api.py"
MODULE_NAME = "hermes_dashboard_plugin_reset-rescue"

KEY_A = "a" * 16
KEY_B = "b" * 16
CREDIT_1 = "c" * 16
CREDIT_2 = "d" * 16
T0 = 1_800_000_000.0


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


def seeded_service(**settings) -> Service:
    store = FakeStore()
    for key, label in ((KEY_A, "a***@e***.com"), (KEY_B, "b***@e***.com")):
        store.account_upsert(
            {
                "account_key": key,
                "label": label,
                "source_kind": "pool",
                "last_list_ok_at": T0,
                "last_error_code": None,
                # Stray row fields must never reach the HTTP body.
                "account_id": "acct-raw-123",
                "access_token": "tok-SECRET",
            }
        )
    for key, account, expires in ((CREDIT_1, KEY_A, T0 + 3600), (CREDIT_2, KEY_B, T0 + 7200)):
        store.credit_upsert(
            {
                "credit_key": key,
                "account_key": account,
                "title": "Full reset",
                "reset_type": "codex_rate_limits",
                "granted_at": T0 - 86400,
                "expires_at": expires,
                "backend_state": "available",
                "status": "scheduled",
                "updated_at": T0,
                "credit_id": "raw-credit-id-XYZ",
                "raw_body": '{"secret":1}',
            }
        )
    store.slot_upsert(CREDIT_1, "primary", {"fire_at": T0 + 3000, "state": "armed"})
    deps = make_deps(store=store, settings=make_settings(**settings) if settings else None)
    return Service(deps)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.http = client()

    def tearDown(self) -> None:
        API.set_service_factory(None)
        self.http.close()

    def get(self, path: str):
        return self.http.get(f"/api/plugins/reset-rescue{path}")


# ----------------------------------------------------------------- T-28


class RouteTable(Base):
    def test_route_table_is_pinned(self):
        served = sorted((m, r.path) for r in API.router.routes for m in r.methods)
        self.assertEqual(served, sorted(API.ROUTES))

    def test_no_route_or_parameter_named_profile(self):
        for route in API.router.routes:
            self.assertNotIn("profile", route.path.lower())
            params = inspect.signature(route.endpoint).parameters
            self.assertFalse([p for p in params if "profile" in p.lower()], route.path)
            dependant = getattr(route, "dependant", None)
            if dependant is not None:
                names = [
                    p.name
                    for p in (
                        dependant.query_params
                        + dependant.path_params
                        + dependant.body_params
                        + dependant.header_params
                    )
                ]
                self.assertFalse([n for n in names if "profile" in n.lower()], route.path)

    def test_hermes_profile_query_is_ignored_not_consumed(self):
        API.set_service_factory(seeded_service)
        response = self.get("/status?profile=other")
        self.assertEqual(response.status_code, 200)

    def test_manifest_hides_the_tab_and_points_at_this_file(self):
        import json

        manifest = json.loads((REPO / "dashboard" / "manifest.json").read_text())
        self.assertEqual(manifest["name"], "reset-rescue")
        self.assertEqual(manifest["api"], "plugin_api.py")
        self.assertIs(manifest["tab"]["hidden"], True)


class StatusRoute(Base):
    def test_status_returns_the_status_dto(self):
        API.set_service_factory(seeded_service)
        response = self.get("/status")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(tuple(body), dto_module.StatusDTO.FIELDS)
        self.assertEqual([a["account_key"] for a in body["accounts"]], [KEY_A, KEY_B])
        credit = body["accounts"][0]["credits"][0]
        self.assertEqual(credit["expires_at"], "2027-01-15T09:00:00Z")
        self.assertEqual(credit["primary"]["fire_at"], "2027-01-15T08:50:00Z")
        self.assertEqual(body["attention"]["pending"], 2)

    def test_service_runs_in_an_executor_thread_through_plugin_code(self):
        seen = {}

        def factory():
            seen["thread"] = threading.current_thread() is not threading.main_thread()
            frames = inspect.stack()
            seen["plugin_frame"] = any(Path(f.filename).resolve() == API_FILE for f in frames)
            return seeded_service()

        API.set_service_factory(factory)
        self.assertEqual(self.get("/status").status_code, 200)
        self.assertTrue(seen["thread"], "blocking call must leave the event loop thread")
        self.assertTrue(seen["plugin_frame"], "plugin_api.py must be on the executor stack")

    def test_a_fresh_service_per_request(self):
        built = []

        def factory():
            built.append(1)
            return seeded_service()

        API.set_service_factory(factory)
        self.get("/status")
        self.get("/status")
        self.assertEqual(len(built), 2)

    def test_not_ready_when_no_service_can_be_built(self):
        API.set_service_factory(None)
        if hasattr(sys.modules.get("reset_rescue.service"), "build_service"):
            self.skipTest("host wiring landed; covered by the default-factory test")
        response = self.get("/status")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(),
            {
                "error": "not_ready",
                "message_key": "errors.not_ready",
                "detail": "The plugin is not ready.",
            },
        )

    def test_slow_service_answers_504_before_the_ui_timeout(self):
        original = API.STATUS_TIMEOUT_S
        API.STATUS_TIMEOUT_S = 0.2

        class Slow:
            def status(self):
                time.sleep(1.0)
                return seeded_service().status()

        try:
            API.set_service_factory(Slow)
            started = time.monotonic()
            response = self.get("/status")
            self.assertLess(time.monotonic() - started, 0.9)
            self.assertEqual(response.status_code, 504)
            self.assertEqual(response.json()["error"], "timeout")
        finally:
            API.STATUS_TIMEOUT_S = original

    def test_held_run_lock_maps_to_409_busy(self):
        class Locked:
            def status(self):
                raise LockTimeout("run.lock held by pid 4242 at <HERMES_HOME>/x")

        API.set_service_factory(Locked)
        response = self.get("/status")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json(),
            {
                "error": "busy",
                "message_key": "errors.busy",
                "detail": "Another operation is running.",
            },
        )


# ----------------------------------------------------------------- T-32


def walk_keys(value, path=""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield f"{path}.{key}" if path else key, key
            yield from walk_keys(item, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for item in value:
            yield from walk_keys(item, f"{path}[]")


ALLOWED = {
    "": dto_module.StatusDTO.FIELDS,
    "settings": dto_module.SettingsViewDTO.FIELDS,
    "schedule": dto_module.ScheduleDTO.FIELDS,
    "schedule.scan_job": dto_module.JobDTO.FIELDS,
    "schedule.wrappers[]": dto_module.WrapperStateDTO.FIELDS,
    "accounts[]": dto_module.AccountDTO.FIELDS,
    # D-030: the per-account overrides and the effective values are plain maps
    # of the overridable fields only; their keys are pinned here.
    "accounts[].overrides": (
        "monitored",
        "auto_redeem_enabled",
        "early_redeem_enabled",
        "early_redeem_remaining_pct",
        "reminders_enabled",
    ),
    "accounts[].effective": (
        "auto_redeem_enabled",
        "early_redeem_enabled",
        "early_redeem_remaining_pct",
        "reminders_enabled",
    ),
    "accounts[].credits[]": dto_module.CreditDTO.FIELDS,
    "accounts[].credits[].primary": dto_module.SlotDTO.FIELDS,
    "accounts[].credits[].backup": dto_module.SlotDTO.FIELDS,
    "accounts[].credits[].last_result": dto_module.LastResultDTO.FIELDS,
    "attention": dto_module.AttentionDTO.FIELDS,
}


class AllowListThroughHttp(Base):
    def test_every_key_in_the_status_body_is_allow_listed(self):
        API.set_service_factory(seeded_service)
        body = self.get("/status").json()
        for full, key in walk_keys(body):
            parent = full.rsplit(".", 1)[0] if "." in full else ""
            self.assertIn(parent, ALLOWED, f"unexpected object at {full}")
            self.assertIn(key, ALLOWED[parent], f"{full} is not allow-listed")

    def test_no_secret_raw_id_or_quota_in_the_body(self):
        """D-010 + D-017: no secret, raw id or *used* quota figure.

        The compact summary D-017 restored is the one exception: the plan name
        and each present window's *remaining* percent (plus the user's own
        configured threshold). A used percentage, a per-credit figure or any
        other quota field is still a failure.
        """
        API.set_service_factory(seeded_service)
        text = self.get("/status").text
        for needle in (
            "tok-SECRET",
            "acct-raw-123",
            "raw-credit-id-XYZ",
            "secret",
            "access_token",
            "profile",
            "used_pct",
            "quota_percent",
            "weekly_pct",
            "five_hour_pct",
        ):
            self.assertNotIn(needle, text)
        body = self.get("/status").json()
        self.assertIn("quota", body["accounts"][0])

    def test_a_non_dto_result_is_refused_not_serialized(self):
        class Leaky:
            def status(self):
                return {"access_token": "tok-SECRET", "accounts": []}

        API.set_service_factory(Leaky)
        response = self.get("/status")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json(),
            {
                "error": "internal_error",
                "message_key": "errors.internal_error",
                "detail": "The operation failed.",
            },
        )
        self.assertNotIn("tok-SECRET", response.text)

    def test_a_dto_carrying_an_extra_attribute_is_refused(self):
        def factory():
            service = seeded_service()
            status = service.status()
            status.access_token = "tok-SECRET"  # DTOLeak on to_dict()

            class Fixed:
                def status(self_inner):
                    return status

            return Fixed()

        API.set_service_factory(factory)
        response = self.get("/status")
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("tok-SECRET", response.text)

    def test_errors_carry_codes_not_provider_text(self):
        class Boom:
            def status(self):
                raise RuntimeError(
                    '401 from https://chatgpt.com/backend-api body={"detail":"Bearer abc"}'
                )

        API.set_service_factory(Boom)
        with self.assertLogs("reset_rescue.api", level="WARNING") as logs:
            response = self.get("/status")
        self.assertEqual(response.status_code, 500)
        self.assertEqual(set(response.json()), {"error", "message_key", "detail"})
        for text in (response.text, "\n".join(logs.output)):
            self.assertNotIn("chatgpt.com", text)
            self.assertNotIn("Bearer", text)


class StandaloneImport(unittest.TestCase):
    def test_engine_loads_by_file_location_without_sys_path(self):
        """Hermes imports plugin_api.py with no package context: the engine
        loader must find reset_rescue next to it."""
        saved = {
            name: mod
            for name, mod in sys.modules.items()
            if name == "reset_rescue" or name.startswith("reset_rescue.")
        }
        saved_path = list(sys.path)
        try:
            for name in saved:
                del sys.modules[name]
            sys.path[:] = [
                p for p in sys.path if Path(p or ".").resolve() not in (REPO, REPO / "tests")
            ]
            module = API._engine("reset_rescue.dto")
            self.assertEqual(Path(module.__file__).resolve(), REPO / "reset_rescue" / "dto.py")
        finally:
            for name in [
                n for n in sys.modules if n == "reset_rescue" or n.startswith("reset_rescue.")
            ]:
                del sys.modules[name]
            sys.modules.update(saved)
            sys.path[:] = saved_path


if __name__ == "__main__":
    unittest.main()
