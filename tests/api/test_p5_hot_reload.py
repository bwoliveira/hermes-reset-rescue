"""D-026: the engine hot-reloads behind a stable route shim.

The dashboard mounts a plugin's API routes once, at startup, and never
remounts them (``hermes_cli/web_server_dashboard.py`` ``_mount_plugin_api_routes``
/ ``_plugin_api_mounted``). An update that lands while the dashboard runs
therefore kept serving the old engine: the popup said "Not checked yet" while
the database already held finished scans.

The fix is not to patch Hermes: ``dashboard/plugin_api.py`` stays a thin,
stable shim and every request loads the engine through a loader that re-imports
``reset_rescue`` when any of its ``.py`` files changed (mtime or size) since the
last import — dropping the ``reset_rescue.*`` entries from ``sys.modules`` and
importing again, under a lock.

This suite proves it the way the brief asks: in a temp dir, import the shim,
call ``/status``, rewrite an engine file to return a marker, call again, and
the marker appears without re-importing the shim.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI

from tests.api.asgi_client import AsgiClient

REPO = Path(__file__).resolve().parents[2]
MODULE_NAME = "hermes_dashboard_plugin_reset-rescue"


def copy_tree(destination: Path) -> Path:
    """A throwaway copy of the plugin, so a test can rewrite the engine."""
    shutil.copytree(
        REPO,
        destination,
        ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__", "*.pyc"),
    )
    return destination


def load_shim(root: Path):
    api_file = root / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location(MODULE_NAME, api_file)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def client(api) -> AsgiClient:
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/reset-rescue")
    return AsgiClient(app)


class HotReload(unittest.TestCase):
    def setUp(self) -> None:
        # Honour TMPDIR: the release gate runs inside a read-only mount
        # namespace whose only writable scratch root is its own TMPDIR (under
        # /var/tmp), so a hardcoded path would fail there for the wrong reason.
        self.tmp = Path(tempfile.mkdtemp(prefix="rr-hot-reload-"))
        self.root = copy_tree(self.tmp / "plugin")
        # Snapshot the engine modules other suites imported: this test swaps
        # ``reset_rescue.*`` for a throwaway copy, and must put the originals
        # back or every later suite would import from a deleted directory.
        self._saved = {
            name: module
            for name, module in sys.modules.items()
            if name == "reset_rescue" or name.startswith("reset_rescue.")
        }
        self._drop_engine()
        self.api = load_shim(self.root)
        self.clock_patch = patch.object(self.api, "monotonic", return_value=1000.0)
        self.clock = self.clock_patch.start()
        self.http = client(self.api)

    def tearDown(self) -> None:
        self.http.close()
        self.clock_patch.stop()
        self.api.set_service_factory(None)
        self._drop_engine()
        sys.modules.update(self._saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _drop_engine() -> None:
        for name in [
            n for n in sys.modules if n == "reset_rescue" or n.startswith("reset_rescue.")
        ]:
            del sys.modules[name]

    def _rewrite_version(self, marker: str) -> None:
        """Change one engine file; the mtime must move even on a coarse clock."""
        path = self.root / "reset_rescue" / "version.py"
        text = path.read_text(encoding="utf-8")
        lines = [
            f'VERSION = "{marker}"' if line.startswith("VERSION = ") else line
            for line in text.splitlines()
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        stamp = time.time() + 2
        import os

        os.utime(path, (stamp, stamp))
        # Engine updates become visible at the next one-second signature poll.
        self.clock.return_value += 1.0

    def test_a_rewritten_engine_file_is_live_after_the_poll_interval(self):
        first = self.http.get("/api/plugins/reset-rescue/status")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertNotEqual(first.json()["version"], "9.9.9-hot")

        self._rewrite_version("9.9.9-hot")

        second = self.http.get("/api/plugins/reset-rescue/status")
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["version"], "9.9.9-hot", "the engine was re-imported")

    def test_the_shim_itself_is_not_reimported(self):
        """The route module keeps its identity: only the engine is reloaded."""
        before = id(self.api)
        self.http.get("/api/plugins/reset-rescue/status")
        self._rewrite_version("9.9.9-hot")
        self.http.get("/api/plugins/reset-rescue/status")
        self.assertIs(sys.modules[MODULE_NAME], self.api)
        self.assertEqual(id(self.api), before)

    def test_an_unchanged_tree_is_not_reimported(self):
        """No rewrite, no reload: the module object in sys.modules is the same."""
        self.http.get("/api/plugins/reset-rescue/status")
        module = sys.modules["reset_rescue.version"]
        self.http.get("/api/plugins/reset-rescue/status")
        self.assertIs(sys.modules["reset_rescue.version"], module)

    def test_a_new_engine_module_appears_without_a_restart(self):
        """A file added by an update is importable on the next request."""
        (self.root / "reset_rescue" / "freshly_added.py").write_text(
            'MARKER = "fresh-module"\n', encoding="utf-8"
        )
        self.http.get("/api/plugins/reset-rescue/status")
        module = self.api._engine("reset_rescue.freshly_added")
        self.assertEqual(module.MARKER, "fresh-module")

    def test_signature_stat_is_cached_for_one_second(self):
        with patch.object(self.api, "_signature", wraps=self.api._signature) as signature:
            self.api._ensure_engine()
            self.api._ensure_engine()
            self.clock.return_value += 0.99
            self.api._ensure_engine()
            self.assertEqual(signature.call_count, 1)
            self.clock.return_value += 0.01
            self.api._ensure_engine()
            self.assertEqual(signature.call_count, 2)

    def test_the_loader_is_a_single_shared_entry_point(self):
        """Every route goes through one loader, so a reload cannot half-apply."""
        self.assertTrue(callable(getattr(self.api, "_engine", None)))
        self.assertTrue(callable(getattr(self.api, "engine_version", None)))
        self.assertEqual(
            self.api.engine_version(),
            self.http.get("/api/plugins/reset-rescue/status").json()["version"],
        )


if __name__ == "__main__":
    unittest.main()
