"""T-01 — the manifest is valid and ``register(ctx)`` is inert (I11).

Three layers of proof:

* **Static**: ``plugin.yaml`` parses, carries the fields architecture A§3
  requires, declares ``requires_auth: [openai-codex]`` and a
  ``requires_hermes`` floor, and its ``config_schema`` is well formed.
* **Runtime, real loader**: :func:`hermes_cli.plugin_dev.doctor_plugin` stages
  the plugin in a temporary ``HERMES_HOME`` and runs Hermes' own registration
  path (never the real profile, sockets blocked by the doctor itself).
* **Runtime, direct call**: ``register`` is called with a stub context while
  ``builtins.open``, ``socket.socket`` and ``subprocess.run`` are patched to
  fail — so a hidden file write, socket or child process fails the test
  instead of passing silently on a machine that happens to allow it.
"""

from __future__ import annotations

import builtins
import importlib.util
import socket
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]

MANIFEST_REQUIRED = {
    "manifest_version": 2,
    "name": "reset-rescue",
    "kind": "standalone",
}
#: The twelve keys: §2's nine plus `mask_labels` (D-013), `language` (D-016)
#: and `auto_redeem_enabled` (D-023). This set is the contract: a thirteenth
#: key, or a missing one, fails.
CONFIG_KEYS = {
    "scan_interval_hours",
    "lead_minutes",
    "reminder_1_hours",
    "reminder_2_hours",
    "notify_target",
    "auto_redeem_enabled",
    "early_redeem_enabled",
    "early_redeem_remaining_pct",
    "excluded_accounts",
    "mask_labels",
    "language",
    "dry_run",
    "account_overrides",  # D-030
}
#: The two §2 keys that are renames of L1 keys (`T-71` lands them in settings.py).
RENAMED_IN_T71 = {"early_redeem_remaining_pct", "excluded_accounts"}

#: The two keys D-013/D-016 added; L1's `Settings` gains them with `T-71`.
NEW_IN_T74 = {"mask_labels"}
ALLOWED_TYPES = {
    "str",
    "string",
    "int",
    "integer",
    "float",
    "number",
    "bool",
    "boolean",
    "list",
    "array",
    "dict",
    "object",
    "secret",
}


def _load_manifest() -> dict:
    import yaml  # the runtime ships PyYAML; the manifest is the only YAML we read

    return yaml.safe_load((REPO / "plugin.yaml").read_text(encoding="utf-8"))


class ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = _load_manifest()

    def test_required_fields(self):
        for key, expected in MANIFEST_REQUIRED.items():
            self.assertEqual(self.manifest.get(key), expected, key)
        self.assertTrue(str(self.manifest.get("version") or "").strip())
        self.assertTrue(str(self.manifest.get("description") or "").strip())

    def test_requires_auth_is_the_codex_provider(self):
        self.assertEqual(self.manifest.get("requires_auth"), ["openai-codex"])

    def test_requires_hermes_floor_parses(self):
        spec = str(self.manifest.get("requires_hermes") or "")
        self.assertTrue(spec.startswith(">="), spec)
        from hermes_cli.plugin_validate import _requires_hermes_spec_valid

        self.assertTrue(_requires_hermes_spec_valid(spec), spec)

    def test_no_privileged_capabilities_and_no_tools(self):
        self.assertEqual(self.manifest.get("capabilities"), [])
        self.assertEqual(self.manifest.get("provides_tools"), [])
        self.assertEqual(self.manifest.get("provides_hooks"), [])
        self.assertEqual(self.manifest.get("python_dependencies"), [])

    def test_platforms_exclude_windows(self):
        platforms = self.manifest.get("platforms") or []
        self.assertIn("linux", platforms)
        self.assertNotIn("win32", platforms)
        self.assertNotIn("windows", platforms)

    def test_config_schema_is_well_formed(self):
        schema = self.manifest.get("config_schema") or {}
        self.assertTrue(schema, "config_schema must not be empty")
        self.assertEqual(set(schema), CONFIG_KEYS)
        for key, spec in schema.items():
            self.assertIsInstance(spec, dict, key)
            self.assertIn(str(spec.get("type", "")).lower(), ALLOWED_TYPES, key)
            self.assertTrue(str(spec.get("label") or "").strip(), key)
            self.assertTrue(str(spec.get("description") or "").strip(), key)
            self.assertIn("default", spec, key)
            if "choices" in spec:
                self.assertIsInstance(spec["choices"], list, key)
                self.assertIn(spec["default"], spec["choices"], key)

    def test_config_defaults_match_the_documented_behaviour(self):
        """The §2 table, value by value."""
        schema = self.manifest["config_schema"]
        self.assertEqual(schema["scan_interval_hours"]["default"], 12)
        self.assertEqual(schema["lead_minutes"]["default"], 10)
        self.assertEqual(schema["reminder_1_hours"]["default"], 24)
        self.assertEqual(schema["reminder_2_hours"]["default"], 1)
        self.assertEqual(schema["notify_target"]["default"], "")
        self.assertEqual(schema["auto_redeem_enabled"]["default"], True)
        self.assertEqual(schema["early_redeem_enabled"]["default"], False)
        self.assertEqual(schema["early_redeem_remaining_pct"]["default"], 0)
        self.assertEqual(schema["excluded_accounts"]["default"], [])
        self.assertEqual(schema["mask_labels"]["default"], True)
        self.assertEqual(schema["language"]["default"], "auto")
        self.assertEqual(schema["dry_run"]["default"], False)

    def test_the_language_setting_offers_exactly_the_three_choices(self):
        """D-016: auto, en, pt-BR — and English is the fallback, not a choice."""
        schema = self.manifest["config_schema"]["language"]
        self.assertEqual(schema["choices"], ["auto", "en", "pt-BR"])
        self.assertEqual(schema["type"], "str")

    def test_the_cut_settings_are_gone(self):
        """§2 removed these four keys; the manifest must not still offer them."""
        schema = self.manifest["config_schema"]
        for key in (
            "backup_lead_minutes",
            "account_source",
            "automatic_rescue",
            "included_accounts",
            "early_redeem_weekly_remaining_pct",
        ):
            self.assertNotIn(key, schema, key)

    def test_the_manifest_and_the_settings_table_agree_on_the_shared_keys(self):
        """`settings.py` (L1) owns coercion; the manifest owns the form.

        The two must agree on every key they share. L1 still carries the four
        cut keys until its own simplification ticket lands, so this compares the
        intersection rather than requiring equality — the *manifest* is what the
        user sees, and `test_the_cut_settings_are_gone` pins that side.
        """
        from reset_rescue.settings import config_schema

        generated = config_schema()
        declared = self.manifest["config_schema"]
        # `settings.py` still carries the four cut keys and the two old names
        # until L1's T-71 lands; the manifest is already the §2 nine. Until then
        # compare the keys the two share, and require the manifest to be a
        # subset so a key can never exist only in the form.
        self.assertLessEqual(set(declared), set(generated) | RENAMED_IN_T71 | NEW_IN_T74)
        for key in set(declared) & set(generated):
            # `type` and `default` are behaviour and must match. `label` is the
            # form's wording, which this lane owns: it may say "Notice language"
            # where the settings table says "Language", and a test that froze
            # the wording would make every copy edit a two-file change.
            for field in ("type", "default"):
                self.assertEqual(
                    declared[key].get(field), generated[key].get(field), f"{key}.{field}"
                )
            self.assertTrue(str(declared[key].get("label") or "").strip(), key)
            self.assertTrue(str(declared[key].get("description") or "").strip(), key)

    def test_manifest_has_no_unknown_fields(self):
        from hermes_cli.plugins_manifest import _KNOWN_MANIFEST_FIELDS

        unknown = set(self.manifest) - set(_KNOWN_MANIFEST_FIELDS)
        self.assertEqual(unknown, set(), f"unknown manifest fields: {sorted(unknown)}")

    def test_no_secret_in_the_manifest(self):
        text = (REPO / "plugin.yaml").read_text(encoding="utf-8").lower()
        for needle in ("token:", "api_key:", "password:", "secret:"):
            self.assertNotIn(needle, text)


class DoctorLoadTests(unittest.TestCase):
    """The real loader, in a temp home, with the doctor's own socket guard.

    ``doctor_plugin`` shells out to ``git`` (it checks the plugin's provenance),
    so this one test tells the offline guard that a single ``git`` process is
    expected. Nothing else is widened: the guard still blocks sockets and every
    other process, which is what the doctor's own temp home needs.
    """

    def test_doctor_loads_the_plugin_without_errors(self):
        from hermes_cli.plugin_dev import doctor_plugin

        # The lane runner (`tests/run_lane.py`) does not install the offline
        # guard; the full suite (`scripts/run_tests.py`) does. Import softly so
        # the same test works under both, and only widen the guard when present.
        try:
            from offline_guard import allow_subprocess
        except ImportError:  # pragma: no cover - lane runner
            allow_subprocess = None

        if allow_subprocess is None:
            report = doctor_plugin(REPO)
        else:
            with allow_subprocess("git"):
                report = doctor_plugin(REPO)
        errors = [finding.message for finding in report.findings if finding.level == "error"]
        self.assertEqual(errors, [], f"doctor errors: {errors}")
        self.assertEqual(report.manifest.name, "reset-rescue")


class DirectRegisterTests(unittest.TestCase):
    """Call ``register`` with a stub context and prove it only registers."""

    def _load_package(self):
        name = "hermes_plugins.reset_rescue"
        parent = sys.modules.setdefault("hermes_plugins", types.ModuleType("hermes_plugins"))
        parent.__path__ = []  # type: ignore[attr-defined]
        spec = importlib.util.spec_from_file_location(
            name, REPO / "__init__.py", submodule_search_locations=[str(REPO)]
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        module.__package__ = name
        module.__path__ = [str(REPO)]  # type: ignore[attr-defined]
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    def test_register_only_calls_register_cli_command(self):
        module = self._load_package()
        calls: list[dict] = []

        class StubCtx:
            def register_cli_command(self, **kwargs):
                calls.append(kwargs)
                return object()

            def __getattr__(self, item):  # any other registration is a failure
                raise AssertionError(f"register(ctx) must not call ctx.{item}")

        module.register(StubCtx())
        self.assertEqual(len(calls), 1)
        entry = calls[0]
        self.assertEqual(entry["name"], "reset-rescue")
        self.assertTrue(callable(entry["setup_fn"]))
        self.assertTrue(callable(entry["handler_fn"]))
        self.assertTrue(entry["help"])
        self.assertTrue(entry["description"])

    def test_register_does_no_io(self):
        module = self._load_package()

        class StubCtx:
            def register_cli_command(self, **_kwargs):
                return object()

        def refuse(*_args, **_kwargs):
            raise AssertionError("register(ctx) performed I/O")

        with (
            mock.patch.object(builtins, "open", refuse),
            mock.patch.object(socket, "socket", refuse),
            mock.patch.object(socket, "create_connection", refuse),
            mock.patch.object(subprocess, "run", refuse),
            mock.patch.object(subprocess, "Popen", refuse),
        ):
            module.register(StubCtx())

    def test_register_survives_a_context_without_extra_methods(self):
        """A stub context with exactly one method is enough: nothing else is used."""
        module = self._load_package()
        seen = {}

        class MinimalCtx:
            def register_cli_command(self, name, help, setup_fn, handler_fn, description=""):
                seen.update(name=name, setup=setup_fn, handler=handler_fn)

        module.register(MinimalCtx())
        self.assertEqual(seen["name"], "reset-rescue")


class StaticInertnessTests(unittest.TestCase):
    """A static scan of the files that run at import time."""

    def test_init_has_no_io_calls(self):
        text = (REPO / "__init__.py").read_text(encoding="utf-8")
        for needle in (
            "open(",
            "subprocess",
            "socket",
            "requests",
            "urlopen",
            "Path(",
            "os.environ",
        ):
            self.assertNotIn(needle, text, f"__init__.py must stay inert: {needle}")

    def test_package_init_has_no_io_calls(self):
        text = (REPO / "reset_rescue" / "__init__.py").read_text(encoding="utf-8")
        for needle in ("open(", "subprocess", "socket", "requests", "urlopen"):
            self.assertNotIn(needle, text, f"reset_rescue/__init__.py must stay inert: {needle}")

    def test_cli_module_does_not_import_the_service_at_module_level(self):
        text = (REPO / "reset_rescue" / "cli.py").read_text(encoding="utf-8")
        top = [
            line
            for line in text.splitlines()
            if line.startswith("from .") or line.startswith("import ")
        ]
        self.assertFalse(
            any("service" in line for line in top),
            "service must be imported lazily inside the handler",
        )


if __name__ == "__main__":
    unittest.main()
