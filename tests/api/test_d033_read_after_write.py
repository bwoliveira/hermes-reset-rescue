"""D-033 R6b: GET /settings reads under the profile save lock.

A settings POST answers 504 while its shielded write keeps running in the
executor. The popup reconciles with GET /settings right after that 504. The
read must return the value the write produced, or 504 ``busy`` while the write
still holds the lock. It must never return the value from before the write.

The real router, the real Hermes writer and a temp HERMES_HOME are used. The
write is held open with a gate around ``save_plugin_settings``. Each test lets
the POST reach its 504 first, then reads, so the read always starts after a
write that already answered 504.
"""

from __future__ import annotations

import random
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from fastapi import FastAPI
from hermes_cli import plugins_settings

from tests.api.asgi_client import AsgiClient
from tests.api.test_r4_settings_race import API, PREFIX, _Base

#: Short enough for a fast test. The POST times out after this, the GET's lock
#: wait is bounded below it.
SHORT_TIMEOUT_S = 1.0
SETTING_KEY = "early_redeem_enabled"
OLD_VALUE = False
NEW_VALUE = True


def _field(body: dict, key: str):
    for field in body["fields"]:
        if field["key"] == key:
            return field["value"]
    raise AssertionError(f"no field {key!r} in GET /settings")


class ReadAfterWrite(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.timeout_patch = patch.object(API, "STATUS_TIMEOUT_S", SHORT_TIMEOUT_S)
        self.timeout_patch.start()
        self.release = threading.Event()
        self.entered = threading.Event()
        self.threads: list[threading.Thread] = []

    def tearDown(self) -> None:
        self.release.set()
        for thread in self.threads:
            thread.join(timeout=10)
        self.timeout_patch.stop()
        super().tearDown()

    @contextmanager
    def gated_writes(self):
        """Hold every ``save_plugin_settings`` call until ``self.release`` is set."""
        real = plugins_settings.save_plugin_settings

        def save(plugin_id, plugin_dir, values):
            self.entered.set()
            self.release.wait(timeout=20)
            return real(plugin_id, plugin_dir, values)

        with patch.object(plugins_settings, "save_plugin_settings", side_effect=save):
            yield

    def _app(self):
        app = FastAPI()
        app.include_router(API.router, prefix=PREFIX)
        return app

    def start_post(self, value: bool):
        """POST one setting from its own thread; return ``(answered, status)``.

        ``answered`` is set as soon as the response arrives. The thread then
        closes its client, which waits for the held executor, so the caller
        must not join the thread until the gate is open.
        """
        answered = threading.Event()
        status: dict[str, int] = {}

        def run() -> None:
            client = AsgiClient(self._app())
            try:
                response = client.post(
                    f"{PREFIX}/settings", json={"key": SETTING_KEY, "value": value}
                )
                status["status"] = response.status_code
                answered.set()
            finally:
                client.close()

        thread = threading.Thread(target=run)
        self.threads.append(thread)
        thread.start()
        return answered, status, thread

    def test_read_while_the_write_is_held_is_busy_never_the_old_value(self):
        self.write_config({SETTING_KEY: OLD_VALUE})
        with self.gated_writes():
            answered, status, _thread = self.start_post(NEW_VALUE)
            self.assertTrue(self.entered.wait(timeout=5), "the write never started")
            self.assertTrue(answered.wait(timeout=5), "the POST never answered")
            self.assertEqual(status["status"], 504, "the POST answers 504 while the write runs")
            response = self.http.get(f"{PREFIX}/settings")

        self.assertEqual(response.status_code, 504, response.text)
        self.assertEqual(response.json().get("error"), "busy")

    def test_read_waits_for_the_write_and_returns_the_new_value(self):
        self.write_config({SETTING_KEY: OLD_VALUE})
        with self.gated_writes():
            answered, status, _thread = self.start_post(NEW_VALUE)
            self.assertTrue(self.entered.wait(timeout=5), "the write never started")
            self.assertTrue(answered.wait(timeout=5), "the POST never answered")
            self.assertEqual(status["status"], 504)
            # The write lands while the read is waiting for the lock.
            threading.Timer(0.1, self.release.set).start()
            response = self.http.get(f"{PREFIX}/settings")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(_field(response.json(), SETTING_KEY), NEW_VALUE)

    def test_no_read_returns_a_value_older_than_the_write(self):
        """Property: a 200 read after a 504 write is never the old value.

        Each round holds the write, then releases it after a random delay. The
        read starts at a random point during that window, so it lands before,
        during and after the commit. Allowed answers: 200 with the new value, or
        504 ``busy``.
        """
        rng = random.Random(33)
        for round_no in range(8):
            with self.subTest(round=round_no):
                self.write_config({SETTING_KEY: OLD_VALUE})
                # A fresh gate per round: a timer left from an earlier round
                # sets its own old event, never this round's.
                self.release = threading.Event()
                self.entered = threading.Event()
                with self.gated_writes():
                    answered, status, thread = self.start_post(NEW_VALUE)
                    self.assertTrue(self.entered.wait(timeout=5))
                    self.assertTrue(answered.wait(timeout=5))
                    self.assertEqual(status["status"], 504)
                    threading.Timer(rng.uniform(0.0, 0.3), self.release.set).start()
                    time.sleep(rng.uniform(0.0, 0.3))
                    response = self.http.get(f"{PREFIX}/settings")
                if response.status_code == 200:
                    self.assertEqual(
                        _field(response.json(), SETTING_KEY),
                        NEW_VALUE,
                        "a 200 read returned the value from before the write",
                    )
                else:
                    self.assertEqual(response.status_code, 504, response.text)
                    self.assertEqual(response.json().get("error"), "busy")
                self.release.set()
                thread.join(timeout=10)
                # The write is done: the next read must show the new value.
                self.assertEqual(
                    _field(self.http.get(f"{PREFIX}/settings").json(), SETTING_KEY),
                    NEW_VALUE,
                )
                self.write_config({SETTING_KEY: OLD_VALUE})


if __name__ == "__main__":
    unittest.main()
