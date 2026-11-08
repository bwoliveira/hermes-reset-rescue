"""Private install metadata/locks/leases, even with a writable source home.

The source fixture deliberately lives on writable scratch storage, not behind
our release namespace's read-only mount. Mutating the resulting seed must not
change a source inode, timestamp, file or directory entry.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
E2E = REPO / "tests" / "e2e"
if str(E2E) not in sys.path:
    sys.path.insert(0, str(E2E))

import seed  # noqa: E402  (path set above)


class SeedNeverWritesTheRealProfile(unittest.TestCase):
    def test_a_profile_inside_the_real_home_is_refused_before_any_write(self):
        probe = seed.native_home() / "cache" / "rr-seed-refuse-probe"
        with self.assertRaises(seed.IsolationRefused):
            seed.seed_profile(probe)
        self.assertFalse(probe.exists(), "a refused seed still created directories")

    def test_the_real_home_itself_is_refused(self):
        with self.assertRaises(seed.IsolationRefused):
            seed.assert_profile_isolated(seed.native_home())

    def test_a_symlinked_installs_into_the_real_home_is_refused(self):
        real_cache = seed.native_home() / "cache"
        key = seed.install_key()
        with tempfile.TemporaryDirectory(prefix="rr-seed-") as tmp:
            profile = Path(tmp) / "profile"
            profile.mkdir()
            os.symlink(real_cache, profile / "installs")
            before = real_cache.exists()
            with self.assertRaises(seed.IsolationRefused):
                seed.seed_profile(profile)
            self.assertEqual(before, real_cache.exists())
            self.assertFalse((real_cache / key).exists())

    def test_a_profile_outside_the_real_home_is_accepted(self):
        with tempfile.TemporaryDirectory(prefix="rr-seed-ok-") as tmp:
            profile = Path(tmp) / "profile"
            profile.mkdir()
            resolved = seed.assert_profile_isolated(profile)
            self.assertEqual(resolved, profile.resolve())

    def test_an_old_live_install_symlink_is_refused_without_reusing_it(self):
        with tempfile.TemporaryDirectory(prefix="rr-seed-old-") as tmp:
            profile = Path(tmp) / "profile"
            target = profile / "installs" / seed.install_key()
            target.parent.mkdir(parents=True)
            target.symlink_to(seed.installed_state(), target_is_directory=True)
            with self.assertRaises(seed.IsolationRefused):
                seed.seed_profile(profile)
            self.assertTrue(target.is_symlink())


class SeedCopiesMutableRuntimeState(unittest.TestCase):
    def setUp(self):
        # Never exercise unsafe regression writes against the actual live home.
        temporary = tempfile.TemporaryDirectory(prefix="rr-seed-copy-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.native = self.root / ".hermes"
        self.install = self.root / "hermes-agent"
        self.install.mkdir()
        self.source = self.native / "installs" / seed.install_key(self.install)
        self.generation = self.source / "environments" / "app"
        self.venv = self.generation / "venv"
        self.package = self.venv / "lib" / "python3.14" / "site-packages" / "example.py"
        self.package.parent.mkdir(parents=True)
        self.package.write_text("VALUE = 1\n")
        (self.venv / "pyvenv.cfg").write_text("version_info = 3.14.9\n")
        (self.venv / "lib64").symlink_to("lib", target_is_directory=True)
        (self.generation / "workspace").mkdir()
        self.lock = self.generation / "workspace" / "uv.lock"
        self.lock.write_text("version = 1\n")
        # Editable hooks in a copied payload must not import the live workspace.
        (self.package.parent / "hermes.pth").write_text(str(self.lock.parent) + "\n")
        (self.generation / ".lease-managed").touch()
        (self.generation / ".leases").mkdir()
        (self.generation / ".leases" / "live-lease").write_text("do not copy\n")
        runtime = self.source / "pm-runtime"
        self.pm_generation = runtime / "generations" / "pm"
        self.pm_generation.mkdir(parents=True)
        (self.pm_generation / "pyvenv.cfg").write_text("version_info = 3.14.9\n")
        (self.pm_generation / "pm-runtime.json").write_text('{"inputs": "ready"}')
        (self.pm_generation / ".lease-managed").touch()
        (self.pm_generation / ".leases").mkdir()
        (self.pm_generation / ".leases" / "live-lease").write_text("do not copy\n")
        (runtime / "selected.json").write_text(
            json.dumps({"inputs": "ready", "generation": "generations/pm"})
        )
        (self.source / ".install.lock").write_text("owner install lock\n")
        (runtime / ".prepare.lock").write_text("owner PM lock\n")
        self.facts = {
            "schema": 1,
            "packages": {
                "venv": {
                    "environment": str(self.venv),
                    "resolved_lock": str(self.lock),
                    "stamp": "committed",
                    "extras": ["all"],
                }
            },
        }
        (self.source / "facts.json").write_text(json.dumps(self.facts))
        home = mock.patch.object(Path, "home", return_value=self.root)
        home.start()
        self.addCleanup(home.stop)
        self.profile = self.root / "profile"
        self.target = self.profile / "installs" / seed.install_key(self.install)

    def snapshot(self):
        return {
            str(p.relative_to(self.native)): (
                p.lstat().st_ino,
                p.lstat().st_mtime_ns,
                p.lstat().st_ctime_ns,
                str(p.readlink())
                if p.is_symlink()
                else (hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None),
            )
            for p in [self.native, *self.native.rglob("*")]
        }

    def test_an_explicit_prepared_source_is_used_without_editing_its_fingerprint(self):
        with mock.patch.object(seed, "installed_state", side_effect=AssertionError("wrong source")):
            target = seed.seed_profile(self.profile, install=self.install, source_state=self.source)
        self.assertEqual(target, self.target)
        fact = json.loads((target / "facts.json").read_text())["packages"]["venv"]
        self.assertEqual(fact["stamp"], "committed")
        self.assertEqual(Path(fact["resolved_lock"]).read_text(), self.lock.read_text())

    def test_writable_source_never_receives_seed_or_runtime_writes(self):
        before = self.snapshot()
        target = seed.seed_profile(self.profile, install=self.install)
        self.assertEqual(target, self.target)
        self.assertFalse(target.is_symlink(), "the install state still aliases live state")
        seeded = json.loads((target / "facts.json").read_text())["packages"]["venv"]
        environment = Path(seeded["environment"])
        self.assertTrue(environment.resolve().is_relative_to(target.resolve()))
        self.assertEqual(seeded["stamp"], "committed")
        self.assertEqual(seeded["extras"], ["all"])
        self.assertEqual(Path(seeded["resolved_lock"]).read_text(), "version = 1\n")
        self.assertFalse(list(target.rglob("live-lease")))
        self.assertFalse(any(p.resolve().is_relative_to(self.native) for p in target.rglob("*")))
        site = environment / self.package.parent.relative_to(self.venv)
        self.assertEqual((site / "hermes.pth").read_text(), str(self.install) + "\n")
        self.assertNotEqual((site / "example.py").stat().st_ino, self.package.stat().st_ino)
        # These are the exact paths boot/PM/lease management mutate.
        for path in [
            target / ".install.lock",
            target / "facts.json",
            target / "pm-runtime" / ".prepare.lock",
            target / "pm-runtime" / "selected.json",
            environment.parent / ".leases" / "test-lease",
            target / "pm-runtime" / "generations" / "pm" / ".leases" / "test-lease",
            environment / ".lock",
            site / "example.py",
            Path(seeded["resolved_lock"]),
        ]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("isolated mutation\n")
        self.assertEqual(before, self.snapshot(), "seed/runtime changed the source install")

    def test_existing_state_is_not_replaced_or_trusted(self):
        self.target.mkdir(parents=True)
        marker = self.target / "sentinel"
        marker.write_text("keep\n")
        before = self.snapshot()
        with self.assertRaises(seed.SeedError):
            seed.seed_profile(self.profile, install=self.install)
        self.assertEqual(marker.read_text(), "keep\n")
        self.assertEqual(before, self.snapshot())

    def test_invalid_selections_are_refused_before_any_destination_write(self):
        original = (self.source / "facts.json").read_text()
        for field, value in [
            ("environment", str(self.native)),
            ("resolved_lock", str(self.native / "facts.json")),
        ]:
            with self.subTest(field=field):
                facts = json.loads(original)
                facts["packages"]["venv"][field] = value
                (self.source / "facts.json").write_text(json.dumps(facts))
                self.assertFalse(seed.available(self.install))
                self.assertIsNone(seed.seed_profile(self.profile, install=self.install))
                self.assertFalse(self.profile.exists())
        (self.source / "facts.json").write_text(original)
        for generation in [
            str(Path("..") / "outside"),
            "/absolute",
            str(Path("generations") / ".." / ".." / "outside"),
        ]:
            with self.subTest(generation=generation):
                (self.source / "pm-runtime" / "selected.json").write_text(
                    json.dumps({"generation": generation, "inputs": "ready"})
                )
                self.assertFalse(seed.available(self.install))
                self.assertIsNone(seed.seed_profile(self.profile, install=self.install))
                self.assertFalse(self.profile.exists())

    def test_copy_failure_removes_partial_state_without_changing_the_source(self):
        before = self.snapshot()
        with mock.patch.object(seed.shutil, "copyfile", side_effect=OSError("copy failed")):
            with self.assertRaises(seed.SeedError):
                seed.seed_profile(self.profile, install=self.install)
        self.assertFalse(self.target.exists())
        self.assertEqual(before, self.snapshot())

    def test_readonly_packages_with_a_writable_editable_workspace_are_copied(self):
        # A read-only lib/ alone is insufficient: its .pth/finder can import
        # code from the original generation's otherwise writable workspace.
        with mock.patch.object(
            seed, "_read_only_tree", side_effect=lambda p: p == self.venv / "lib"
        ):
            target = seed.seed_profile(self.profile, install=self.install)
        environment = Path(
            json.loads((target / "facts.json").read_text())["packages"]["venv"]["environment"]
        )
        self.assertFalse((environment / "lib").is_symlink())

    def test_chmod_readonly_is_not_a_readonly_filesystem(self):
        self.package.chmod(0o444)
        self.assertFalse(seed._read_only_tree(self.venv / "lib"))

    def test_a_writable_symlink_below_readonly_lib_forces_copying(self):
        escape = self.root / "writable"
        escape.mkdir()
        (self.venv / "lib" / "escape").symlink_to(escape, target_is_directory=True)
        readonly = getattr(os, "ST_RDONLY", 1)

        def flags(path):
            return SimpleNamespace(f_flag=0 if Path(path) == escape else readonly)

        with mock.patch.object(seed.os, "statvfs", side_effect=flags):
            self.assertFalse(seed._read_only_tree(self.venv / "lib"))


class ReleaseGateHasNoLiveWritableBind(unittest.TestCase):
    def test_only_scratch_is_bound_writable(self):
        script = (REPO / "scripts" / "release_gate.sh").read_text()
        writable_binds = [
            line.strip() for line in script.splitlines() if line.strip().startswith("--bind ")
        ]
        self.assertEqual(writable_binds, ['--bind "$WORK" "$WORK" \\'])
        self.assertIn('--ro-bind "$REAL_HOME" "$REAL_HOME"', script)
        self.assertIn("--setenv HERMES_DISABLE_LAZY_INSTALLS 1", script)


if __name__ == "__main__":
    unittest.main()
