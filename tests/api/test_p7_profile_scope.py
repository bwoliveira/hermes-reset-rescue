"""D-025: a settings request stays inside the profile Hermes scoped it to.

Hermes scopes every ``/api/plugins/<name>/`` request with
``_plugin_route_secret_scope`` → ``_config_profile_scope``, which enters the
requested profile's home through a **ContextVar**
(``hermes_constants.set_hermes_home_override``). A worker thread only inherits
that context through ``asyncio.to_thread``; ``loop.run_in_executor`` starts
with an empty context, so the plugin read and wrote the LAUNCH profile's
``config.yaml`` even for a ``?profile=B`` request — a save that "succeeds" and
lands in a file the popup never reads.

This suite pins the property directly: the ContextVar is set on the request
task, and the code that reads/writes the settings must still see it.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

from fastapi import FastAPI

from tests.api.asgi_client import AsgiClient

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


class ProfileScopeReachesTheWorker(unittest.TestCase):
    """A ContextVar set on the request must be visible where the work runs."""

    def setUp(self) -> None:
        self.app = FastAPI()
        self.app.include_router(API.router, prefix="/api/plugins/reset-rescue")
        self.http = AsgiClient(self.app)
        self.seen: list = []

    def tearDown(self) -> None:
        self.http.close()
        API.set_service_factory(None)

    def _probe(self, call: str, method: str, path: str, body: dict | None = None) -> None:
        """Set a ContextVar on the request, then check the worker saw it."""
        from hermes_constants import (
            get_hermes_home_override,
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        @self.app.middleware("http")
        async def scope(request, call_next):
            token = set_hermes_home_override("/var/tmp/rr-scoped-profile")
            try:
                return await call_next(request)
            finally:
                reset_hermes_home_override(token)

        original = getattr(API, call)
        try:

            def probe(*args, **kwargs):
                self.seen.append(get_hermes_home_override())
                return original(*args, **kwargs)

            setattr(API, call, probe)
            # The status is not the point: under the release gate the real
            # profile is read-only, so a write legitimately fails. What must
            # hold is that the worker ran and saw the request's profile.
            self.http.request(method, path, json=body)
            self.assertEqual(len(self.seen), 1, "the worker never ran")
        finally:
            setattr(API, call, original)

    def test_the_settings_read_runs_inside_the_request_profile(self):
        self._probe("_read_settings_fields", "GET", "/api/plugins/reset-rescue/settings")
        self.assertEqual(self.seen, ["/var/tmp/rr-scoped-profile"])

    def test_the_settings_write_runs_inside_the_request_profile(self):
        self._probe(
            "_write_setting",
            "POST",
            "/api/plugins/reset-rescue/settings",
            {"key": "lead_minutes", "value": 20},
        )
        self.assertEqual(self.seen, ["/var/tmp/rr-scoped-profile"])

    def test_the_service_call_runs_inside_the_request_profile(self):
        from reset_rescue.service import Service
        from tests.fakes import FakeStore, make_deps, make_settings

        API.set_service_factory(
            lambda: Service(make_deps(store=FakeStore(), settings=make_settings()))
        )
        self._probe("_invoke", "GET", "/api/plugins/reset-rescue/status")
        self.assertEqual(self.seen, ["/var/tmp/rr-scoped-profile"])


class NoRunInExecutor(unittest.TestCase):
    def test_the_plugin_never_uses_run_in_executor(self):
        """A regression here silently moves every read and write to the launch profile."""
        source = API_FILE.read_text(encoding="utf-8")
        # The docstring explains why; only a real call site is a regression.
        self.assertNotIn("run_in_executor(", source)
        self.assertIn("asyncio.to_thread(", source)


if __name__ == "__main__":
    unittest.main()
