"""T-48 — the offline guard must actually fail the run.

These tests are the reason a green suite means something: they prove the audit
hook blocks a socket, an unexpected process and a write into the real Hermes
profile, and that the explicit escape hatches still work. If the guard is ever
removed, this file fails first.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import offline_guard  # noqa: E402  (path set above)


class GuardInstalled(unittest.TestCase):
    def test_guard_is_installed_by_the_runner(self):
        self.assertTrue(
            offline_guard.installed(),
            "scripts/run_tests.py must install the offline guard before importing tests",
        )

    def test_guard_knows_the_real_home(self):
        """The guard protects whatever profile the runner installed it around.

        It used to assert the name looked like ``.hermes``, which held while the
        suites ran against the real profile. Now the runner protects the temp
        profile it built (R17), so the assertion is the contract: a directory
        that exists and is not inside another profile.
        """
        home = offline_guard._state.real_home
        self.assertIsNotNone(home)
        self.assertTrue(Path(home).is_dir(), f"guard home does not exist: {home}")
        self.assertFalse(
            str(home).startswith(str(Path.home() / ".hermes" / "cache")),
            f"the guard is protecting a scratch path inside the real profile: {home}",
        )


class NetworkBlocked(unittest.TestCase):
    def test_socket_creation_raises(self):
        with self.assertRaises(offline_guard.OfflineViolation) as ctx:
            socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.assertIn("network access", str(ctx.exception))

    def test_connection_attempt_raises(self):
        with self.assertRaises(offline_guard.OfflineViolation):
            socket.create_connection(("127.0.0.1", 9), timeout=0.1)

    def test_name_resolution_raises(self):
        with self.assertRaises(offline_guard.OfflineViolation):
            socket.getaddrinfo("chatgpt.com", 443)


class AsyncioSelfPipeAllowed(unittest.TestCase):
    """An AF_UNIX socketpair is local IPC: the event loop must be able to start.

    Regression: blocking ``socket.__new__`` outright made every ``asyncio``
    loop impossible, which pushed ``tests/api`` into a private ``os.pipe``
    loop subclass. The guard allows AF_UNIX and nothing more.
    """

    def test_socketpair_is_allowed(self):
        left, right = socket.socketpair()
        try:
            self.assertEqual(left.family, socket.AF_UNIX)
            left.send(b"x")
            self.assertEqual(right.recv(1), b"x")
        finally:
            left.close()
            right.close()

    def test_an_event_loop_can_run(self):
        import asyncio

        async def go() -> int:
            await asyncio.sleep(0)
            return 7

        loop = asyncio.new_event_loop()
        try:
            self.assertEqual(loop.run_until_complete(go()), 7)
        finally:
            loop.close()

    def test_af_unix_does_not_open_the_internet(self):
        # The AF_UNIX exemption must not leak into AF_INET.
        with self.assertRaises(offline_guard.OfflineViolation):
            socket.socket(socket.AF_INET, socket.SOCK_STREAM)


class LoopbackOptIn(unittest.TestCase):
    """``allow_loopback`` permits a local listener and nothing beyond it."""

    def test_loopback_listener_and_client(self):
        with offline_guard.allow_loopback():
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.connect(("127.0.0.1", port))
            peer, _ = server.accept()
            client.send(b"ping")
            self.assertEqual(peer.recv(4), b"ping")
            for sock in (client, peer, server):
                sock.close()

    def test_non_loopback_bind_is_refused_inside_the_opt_in(self):
        with offline_guard.allow_loopback():
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                with self.assertRaises(offline_guard.OfflineViolation) as ctx:
                    probe.bind(("0.0.0.0", 0))
                self.assertIn("non-loopback", str(ctx.exception))
            finally:
                probe.close()

    def test_a_hostname_is_never_resolved_inside_the_opt_in(self):
        with offline_guard.allow_loopback():
            with self.assertRaises(offline_guard.OfflineViolation) as ctx:
                socket.getaddrinfo("chatgpt.com", 443)
            self.assertIn("refused to resolve", str(ctx.exception))

    def test_the_opt_in_is_scoped(self):
        with offline_guard.allow_loopback():
            self.assertTrue(offline_guard.loopback_allowed())
        self.assertFalse(offline_guard.loopback_allowed())
        with self.assertRaises(offline_guard.OfflineViolation):
            socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    def test_loopback_literal_detection(self):
        for good in ("127.0.0.1", "127.9.9.9", "::1", "localhost", "[::1]"):
            self.assertTrue(offline_guard._is_loopback_host(good), good)
        for bad in ("chatgpt.com", "0.0.0.0", "10.0.0.1", "", None, 7):
            self.assertFalse(offline_guard._is_loopback_host(bad), bad)


class ProcessBlocked(unittest.TestCase):
    def test_unexpected_process_raises(self):
        with self.assertRaises(offline_guard.OfflineViolation) as ctx:
            subprocess.run(["/bin/echo", "should not run"], check=False)
        self.assertIn("unexpected process", str(ctx.exception))

    def test_allowlisted_process_runs(self):
        with offline_guard.allow_subprocess("echo"):
            done = subprocess.run(
                ["/bin/echo", "allowed"], capture_output=True, text=True, check=False
            )
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout.strip(), "allowed")

    def test_allowlist_is_restored_after_the_block(self):
        with offline_guard.allow_subprocess("echo"):
            pass
        with self.assertRaises(offline_guard.OfflineViolation):
            subprocess.run(["/bin/echo", "blocked again"], check=False)


class RealProfileWriteBlocked(unittest.TestCase):
    """The guard refuses writes into whatever profile the runner protected.

    These tests used to assume the ambient ``HERMES_HOME`` was the real
    ``~/.hermes``. It is not: the runner protects ``HERMES_HOME``, which a
    developer's shell may point at a scratch home (and a leaked value from
    another lane's run made this file fail for reasons that had nothing to do
    with the guard). They now build a profile of their own under the scratch
    root and install a guard around it, so the assertion is about the rule, not
    about the machine.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="rr-guard-home-")
        self.home = Path(self._tmp.name) / ".hermes"
        for name in ("cron", "plugin-data/reset-rescue"):
            (self.home / name).mkdir(parents=True, exist_ok=True)
        self._saved = (offline_guard._state.real_home, offline_guard._state.allowed_roots)
        offline_guard._state.real_home = self.home
        # Deliberately *not* the scratch root: the point of these tests is that a
        # path inside the protected profile is refused, so the allowed roots must
        # not contain it. A path that cannot exist keeps that unambiguous.
        offline_guard._state.allowed_roots = (Path("/nonexistent-allowed-root"),)

    def tearDown(self):
        offline_guard._state.real_home, offline_guard._state.allowed_roots = self._saved
        self._tmp.cleanup()

    def test_auth_json_write_raises(self):
        with self.assertRaises(offline_guard.OfflineViolation):
            (self.home / "auth.json").open("w", encoding="utf-8")

    def test_cron_dir_write_raises(self):
        with self.assertRaises(offline_guard.OfflineViolation):
            (self.home / "cron" / "jobs.json").open("a", encoding="utf-8")

    def test_plugin_data_write_raises(self):
        with self.assertRaises(offline_guard.OfflineViolation):
            (self.home / "plugin-data" / "reset-rescue" / "data.db").open("wb")

    def test_scratch_write_is_allowed(self):
        scratch = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
        scratch.mkdir(parents=True, exist_ok=True)
        target = scratch / "rr-guard-probe.txt"
        target.write_text("ok", encoding="utf-8")
        self.assertEqual(target.read_text(encoding="utf-8"), "ok")
        target.unlink()

    def test_reading_the_real_profile_is_allowed(self):
        # Reads are legitimate (the runtime lives on disk); only writes are refused.
        self.assertTrue(self.home.exists())
        self.assertTrue((self.home / "auth.json").parent.is_dir())

    def test_the_runner_protected_a_profile(self):
        """The runner must always name a profile; a None here means no guard."""
        self.assertIsNotNone(self._saved[0])


if __name__ == "__main__":
    unittest.main()
