"""R6 P1-03: a reset never exposes a spend-authorizing state that neither endpoint allows.

Hermes writes plugin settings one key at a time. Between two writes a quota check can
read the config. The routes below record the settings after every real write and
assert that the early (quota) decision stays refused in each of them, whenever it is
refused both before and after the reset. Fake accounts, real writer, temp HERMES_HOME.
"""

from __future__ import annotations

import random
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hermes_cli import plugins_settings

from reset_rescue import settings as settings_module
from reset_rescue.authorize import authorize_consume
from reset_rescue.host import read_plugin_settings
from reset_rescue.settings import parse_settings
from tests.api.test_r4_settings_race import KEY_A, KEY_B, LABEL_A, LABEL_B, PREFIX, _Base

#: Weekly quota left, below the 30 % threshold of the scenarios: a refused quota
#: decision here is refused only because of the settings under test.
QUOTA_LEFT_PCT = 20.0
ACCOUNT_LABELS = {KEY_A: LABEL_A, KEY_B: LABEL_B}


def early_allowed(raw, account_key=KEY_A, quota=QUOTA_LEFT_PCT):
    decision = authorize_consume(
        role="early",
        credit=SimpleNamespace(expires_at=None),
        settings=parse_settings(raw),
        now=0.0,
        account_key=account_key,
        label=ACCOUNT_LABELS[account_key],
        quota_reading=quota,
    )
    return decision.allowed


class ResetWriteOrder(_Base):
    def raw(self):
        return read_plugin_settings()

    def record_writes(self, call):
        """Return ``(states, response)``; ``states[i]`` is the settings after real write ``i``."""
        real = plugins_settings.save_plugin_setting
        states = []

        def spy(plugin_id, segments, value):
            real(plugin_id, segments, value)
            states.append(self.raw())

        with patch.object(plugins_settings, "save_plugin_setting", side_effect=spy):
            response = call()
        return states, response

    def assert_refused_in_every_state(self, before, states, after, account_key=KEY_A):
        self.assertFalse(early_allowed(before, account_key), "start state must refuse")
        self.assertFalse(early_allowed(after, account_key), "end state must refuse")
        for index, state in enumerate(states):
            self.assertFalse(
                early_allowed(state, account_key),
                f"write {index + 1} of {len(states)} authorizes an early spend: {state}",
            )

    def test_global_reset_never_exposes_early_spend_between_writes(self):
        self.write_config(
            {
                "auto_redeem_enabled": False,
                "early_redeem_enabled": True,
                "early_redeem_remaining_pct": 30,
            }
        )
        before = self.raw()
        states, response = self.record_writes(
            lambda: self.http.post(f"{PREFIX}/settings/reset", json={})
        )
        self.assertEqual(response.status_code, 200, response.text)
        after = self.raw()
        self.assertGreater(len(states), 1)
        self.assert_refused_in_every_state(before, states, after)

    def test_reset_all_never_exposes_early_spend_between_writes(self):
        self.write_config(
            {
                "excluded_accounts": [KEY_B],
                "account_overrides": {
                    KEY_A: {
                        "label": LABEL_A,
                        "auto_redeem_enabled": False,
                        "early_redeem_enabled": True,
                        "early_redeem_remaining_pct": 30,
                    },
                    KEY_B: {"label": LABEL_B, "monitored": False, "early_redeem_enabled": True},
                },
            }
        )
        before = self.raw()
        states, response = self.record_writes(
            lambda: self.http.post(f"{PREFIX}/settings/account/reset-all", json={})
        )
        self.assertEqual(response.status_code, 200, response.text)
        after = self.raw()
        self.assertGreater(len(states), 1)
        self.assert_refused_in_every_state(before, states, after, KEY_A)
        self.assert_refused_in_every_state(before, states, after, KEY_B)

    def test_account_reset_never_exposes_early_spend_between_writes(self):
        self.write_config(
            {
                "account_overrides": {
                    KEY_A: {
                        "label": LABEL_A,
                        "auto_redeem_enabled": False,
                        "early_redeem_enabled": True,
                        "early_redeem_remaining_pct": 30,
                    },
                },
            }
        )
        before = self.raw()
        states, response = self.record_writes(
            lambda: self.http.post(f"{PREFIX}/settings/account/reset", json={"account_key": KEY_A})
        )
        self.assertEqual(response.status_code, 200, response.text)
        after = self.raw()
        self.assert_refused_in_every_state(before, states, after, KEY_A)


def _random_state(rng: random.Random) -> dict:
    state = {
        "auto_redeem_enabled": rng.choice([True, False]),
        "early_redeem_enabled": rng.choice([True, False]),
        "early_redeem_remaining_pct": rng.choice([0, 10, 30, 60, 100]),
        "lead_minutes": rng.choice([10, 60]),
        "excluded_accounts": rng.choice([[], [KEY_A], [KEY_B]]),
    }
    overrides = {}
    for key in (KEY_A, KEY_B):
        entry: dict = {"label": ACCOUNT_LABELS[key]}
        if rng.random() < 0.5:
            entry["monitored"] = rng.choice([True, False])
        if rng.random() < 0.5:
            entry["auto_redeem_enabled"] = rng.choice([True, False])
        if rng.random() < 0.5:
            entry["early_redeem_enabled"] = rng.choice([True, False])
        if rng.random() < 0.5:
            entry["early_redeem_remaining_pct"] = rng.choice([0, 10, 30, 60, 100])
        overrides[key] = entry
    state["account_overrides"] = overrides
    return state


def _apply(state: dict, path: tuple, value) -> dict:
    """Apply one write from ``spend_safe_order`` to a copy of ``state``."""
    out = {
        **state,
        "account_overrides": {k: dict(v) for k, v in state["account_overrides"].items()},
    }
    if path[0] == "account_overrides":
        _, key, field = path
        out["account_overrides"].setdefault(key, {})[field] = value
    else:
        out[path[0]] = value
    return out


class SpendSafeOrderProperty(unittest.TestCase):
    def test_every_prefix_refuses_whenever_both_endpoints_refuse(self):
        rng = random.Random(0x6E6)
        checked = 0
        for _case in range(600):
            before = _random_state(rng)
            after = _random_state(rng)
            quota = rng.choice([10.0, 20.0, 50.0, 90.0])
            for account_key in (KEY_A, KEY_B):
                if early_allowed(before, account_key, quota) or early_allowed(
                    after, account_key, quota
                ):
                    continue
                writes = settings_module.spend_safe_order(before, after)
                state = before
                self.assertFalse(early_allowed(state, account_key, quota))
                for path, value in writes:
                    state = _apply(state, path, value)
                    self.assertFalse(
                        early_allowed(state, account_key, quota),
                        f"prefix ending at {path}={value!r} authorizes; "
                        f"before={before} after={after}",
                    )
                    checked += 1
        self.assertGreater(checked, 0)


if __name__ == "__main__":
    unittest.main()
