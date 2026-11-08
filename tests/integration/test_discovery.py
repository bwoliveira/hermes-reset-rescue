"""T-36 — discovery, doctor and CLI smoke against a temp ``HERMES_HOME``.

The discovery and doctor tests are marked ``waiting for L3's plugin.yaml`` until
the manifest is on ``integration``; they assert the real contract the moment it
lands, without a code change here. The CLI smoke test needs the same manifest
plus a registered command, so it waits on the same condition.
"""

from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INTEGRATION = Path(__file__).resolve().parent
for entry in (str(REPO), str(INTEGRATION), str(REPO / "scripts")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import harness  # noqa: E402  (path set above)
import offline_guard  # noqa: E402  (path set above)

NEEDS_MANIFEST = unittest.skipUnless(
    harness.has_manifest(),
    "waiting for L3's plugin.yaml on integration: discovery and doctor cannot run "
    "without a manifest, and the harness must not fake one",
)
NEEDS_ENTRY = unittest.skipUnless(
    (REPO / "__init__.py").is_file(),
    "waiting for L3's __init__.py on integration: register(ctx) cannot be imported yet",
)

# `hermes plugins doctor` stages a copy and runs the real loader, which shells
# out to git for the tracked-bytecode check, and the CLI smoke tests run the
# real `hermes` launcher against a temp profile — that *is* the contract they
# prove. Scoped to this module.
_ALLOWED = None


def setUpModule():
    global _ALLOWED
    _ALLOWED = offline_guard.allow_subprocess("git", "hermes")
    _ALLOWED.__enter__()


def tearDownModule():
    if _ALLOWED is not None:
        _ALLOWED.__exit__(None, None, None)


class ManifestSchema(unittest.TestCase):
    """The manifest contract Hermes actually enforces (drift found while building T-36).

    A§3 drafts `kind: native` and `provides_middleware: []`. The installed
    Hermes accepts neither: `native` is not a valid kind (it is silently
    downgraded to `standalone`), and `provides_middleware` is not a known
    manifest field (it warns and is ignored). These tests pin the real contract
    so L3's manifest cannot ship the draft values.
    """

    def test_valid_kinds_are_the_installed_set(self):
        from hermes_cli.plugins_manifest import _VALID_PLUGIN_KINDS

        self.assertEqual(
            _VALID_PLUGIN_KINDS,
            {"standalone", "backend", "exclusive", "platform", "model-provider"},
        )
        self.assertNotIn("native", _VALID_PLUGIN_KINDS)

    def test_provides_middleware_is_not_a_manifest_field(self):
        from hermes_cli.plugins_manifest import _KNOWN_MANIFEST_FIELDS

        self.assertNotIn("provides_middleware", _KNOWN_MANIFEST_FIELDS)
        # The fields the plugin actually needs must all be known.
        for field in (
            "requires_hermes",
            "requires_auth",
            "config_schema",
            "capabilities",
            "provides_tools",
            "provides_hooks",
            "python_dependencies",
            "platforms",
        ):
            self.assertIn(field, _KNOWN_MANIFEST_FIELDS)

    @NEEDS_MANIFEST
    def test_committed_manifest_uses_a_valid_kind(self):
        from hermes_cli.plugins_manifest import _VALID_PLUGIN_KINDS

        manifest = harness.plugin_manifest()
        assert manifest is not None  # NEEDS_MANIFEST guarantees it
        text = manifest.read_text(encoding="utf-8")
        kind_line = next(
            (line for line in text.splitlines() if line.strip().startswith("kind:")), ""
        )
        self.assertTrue(kind_line, "plugin.yaml must declare kind")
        kind = kind_line.split(":", 1)[1].strip().strip("'\"")
        self.assertIn(
            kind,
            _VALID_PLUGIN_KINDS,
            f"kind {kind!r} is silently downgraded to standalone by Hermes",
        )

    @NEEDS_MANIFEST
    def test_committed_manifest_has_no_unknown_fields(self):
        # Parse with the module Hermes itself uses. PyYAML is not a dependency of
        # the runtime (it is ruamel), so importing `yaml` here proved nothing
        # about the real parse and broke whenever PyYAML was absent.
        import hermes_yaml as yaml
        from hermes_cli.plugins_manifest import _KNOWN_MANIFEST_FIELDS

        manifest = harness.plugin_manifest()
        assert manifest is not None  # NEEDS_MANIFEST guarantees it
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        unknown = sorted(set(data) - _KNOWN_MANIFEST_FIELDS)
        self.assertEqual(unknown, [], f"unknown manifest field(s): {unknown}")


class DiscoveryInTempHome(unittest.TestCase):
    @NEEDS_MANIFEST
    def test_real_discovery_finds_the_plugin(self):
        home = harness.temp_home("t36-disc-")
        try:
            harness.stage_plugin(home)
            with harness.HomeEnv(home):
                manifests = harness.discover(home)
            names = [m.name for m in manifests]
            self.assertIn(
                "reset-rescue", names, f"discovery found {names!r} under {home / 'plugins'}"
            )
        finally:
            shutil.rmtree(home, ignore_errors=True)

    @NEEDS_MANIFEST
    def test_discovered_manifest_carries_the_contract_fields(self):
        home = harness.temp_home("t36-fields-")
        try:
            harness.stage_plugin(home)
            with harness.HomeEnv(home):
                manifests = harness.discover(home)
            manifest = next(m for m in manifests if m.name == "reset-rescue")
            self.assertEqual(manifest.version, "0.1.0")
            self.assertEqual(
                manifest.kind,
                "standalone",
                "kind must be one of backend/exclusive/model-provider/platform/standalone",
            )
            self.assertTrue(manifest.requires_hermes, "requires_hermes must be a real floor")
            self.assertNotIn("native", str(manifest.kind))
        finally:
            shutil.rmtree(home, ignore_errors=True)

    @NEEDS_MANIFEST
    def test_discovery_is_isolated_from_the_real_profile(self):
        """The temp home must not see the real profile's plugins."""
        home = harness.temp_home("t36-iso-")
        try:
            harness.stage_plugin(home)
            with harness.HomeEnv(home):
                manifests = harness.discover(home)
            self.assertTrue(all(m.source == "user" for m in manifests))
            self.assertEqual({m.name for m in manifests}, {"reset-rescue"})
        finally:
            shutil.rmtree(home, ignore_errors=True)

    @NEEDS_MANIFEST
    def test_plugin_data_dir_resolves_inside_the_temp_home(self):
        home = harness.temp_home("t36-data-")
        try:
            with harness.HomeEnv(home):
                from plugins.plugin_storage import plugin_data_dir

                resolved = plugin_data_dir("reset-rescue")
            self.assertTrue(
                resolved.is_relative_to(home), f"plugin_data_dir escaped the temp home: {resolved}"
            )
        finally:
            shutil.rmtree(home, ignore_errors=True)


class DoctorInTempHome(unittest.TestCase):
    @NEEDS_MANIFEST
    def test_doctor_passes(self):
        home = harness.temp_home("t36-doctor-")
        try:
            staged = harness.stage_plugin(home)
            with harness.HomeEnv(home):
                report = harness.doctor(staged)
            findings = "\n".join(harness.doctor_messages(report))
            self.assertTrue(report.ok, "doctor findings:\n" + findings)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    @NEEDS_MANIFEST
    def test_doctor_reports_no_errors(self):
        home = harness.temp_home("t36-doctor-err-")
        try:
            staged = harness.stage_plugin(home)
            with harness.HomeEnv(home):
                report = harness.doctor(staged)
            errors = [m for m in harness.doctor_messages(report) if m.startswith("error")]
            self.assertEqual(errors, [], "\n".join(errors))
        finally:
            shutil.rmtree(home, ignore_errors=True)

    @NEEDS_MANIFEST
    def test_doctor_does_not_touch_the_real_profile(self):
        """Doctor stages its own temp home; ours must be unchanged but for the copy."""
        home = harness.temp_home("t36-doctor-iso-")
        try:
            staged = harness.stage_plugin(home)
            before = sorted(p.name for p in home.iterdir())
            with harness.HomeEnv(home):
                harness.doctor(staged)
            after = sorted(p.name for p in home.iterdir())
            self.assertEqual(before, after)
        finally:
            shutil.rmtree(home, ignore_errors=True)


class RegisterContract(unittest.TestCase):
    """`register(ctx)` must be importable and must not do I/O at import time."""

    @NEEDS_ENTRY
    def test_register_is_callable(self):
        home = harness.temp_home("t36-register-")
        try:
            staged = harness.stage_plugin(home)
            with harness.HomeEnv(home):
                module = harness.load_plugin_module(staged)
                self.assertTrue(
                    callable(getattr(module, "register", None)),
                    "the plugin must expose register(ctx)",
                )
        finally:
            shutil.rmtree(home, ignore_errors=True)

    @NEEDS_ENTRY
    def test_register_accepts_the_documented_context(self):
        home = harness.temp_home("t36-register-ctx-")
        try:
            staged = harness.stage_plugin(home)
            with harness.HomeEnv(home):
                module = harness.load_plugin_module(staged)
                ctx = harness.fake_context()
                module.register(ctx)
            self.assertIsInstance(ctx.cli_commands, list)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    @NEEDS_ENTRY
    def test_register_does_not_create_state(self):
        """Registration is wiring only: no store, no lock, no cron job."""
        home = harness.temp_home("t36-register-io-")
        try:
            staged = harness.stage_plugin(home)
            data_dir = home / "plugin-data" / "reset-rescue"
            before = sorted(p.name for p in data_dir.iterdir())
            with harness.HomeEnv(home):
                module = harness.load_plugin_module(staged)
                module.register(harness.fake_context())
            after = sorted(p.name for p in data_dir.iterdir())
            self.assertEqual(
                before, after, "register() must not create state; that is ensure()'s job"
            )
        finally:
            shutil.rmtree(home, ignore_errors=True)


class CliSmoke(unittest.TestCase):
    """`hermes reset-rescue status` through the real launcher, fakes underneath."""

    @NEEDS_MANIFEST
    def test_plugins_list_sees_the_plugin(self):
        if not harness.cli_runner_available():
            self.skipTest("hermes launcher not on PATH")
        home = harness.temp_home("t36-cli-list-")
        try:
            harness.stage_plugin(home)
            done = harness.run_hermes(["plugins", "list"], home)
            self.assertEqual(done.returncode, 0, done.stderr[-2000:])
            self.assertIn("reset-rescue", done.stdout)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    @NEEDS_MANIFEST
    def test_plugins_doctor_cli_passes(self):
        if not harness.cli_runner_available():
            self.skipTest("hermes launcher not on PATH")
        home = harness.temp_home("t36-cli-doctor-")
        try:
            staged = harness.stage_plugin(home)
            done = harness.run_hermes(["plugins", "doctor", str(staged)], home)
            self.assertEqual(done.returncode, 0, (done.stdout + done.stderr)[-2000:])
        finally:
            shutil.rmtree(home, ignore_errors=True)


class StatusWithFakes(unittest.TestCase):
    """`status` must answer from a half-wired process, with no network and no cron."""

    def test_status_answers_without_collaborators(self):
        from reset_rescue.clock import Deps
        from reset_rescue.service import Service

        home = harness.temp_home("t36-status-")
        try:
            with harness.HomeEnv(home):
                service = Service(Deps(hermes_home=home))
                dto = service.status()
            payload = dto.to_dict() if hasattr(dto, "to_dict") else dto
            self.assertIsInstance(payload, dict)
            self.assertIn("accounts", payload)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_status_is_json_serializable_and_token_free(self):
        from reset_rescue.clock import Deps
        from reset_rescue.service import Service

        home = harness.temp_home("t36-status-json-")
        try:
            with harness.HomeEnv(home):
                service = Service(Deps(hermes_home=home))
                dto = service.status()
            text = json.dumps(dto.to_dict() if hasattr(dto, "to_dict") else dto)
            self.assertNotIn("access_token", text)
            self.assertNotIn("refresh_token", text)
            self.assertNotIn("Bearer ", text)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_status_with_fakes_reports_the_pool(self):
        from reset_rescue.service import Service
        from tests.fakes import make_deps

        home = harness.temp_home("t36-status-fakes-")
        try:
            with harness.HomeEnv(home):
                deps = make_deps()
                deps.hermes_home = home
                service = Service(deps)
                dto = service.status()
            payload = dto.to_dict() if hasattr(dto, "to_dict") else dto
            self.assertIsInstance(payload.get("accounts"), list)
        finally:
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
