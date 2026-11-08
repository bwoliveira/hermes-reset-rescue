"""D-030: ``authorize_consume`` decides with the account's effective settings.

Each case builds one global setting and one override for account A, then asks
the same question for A and for B. B must always get the global answer.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from reset_rescue.authorize import authorize_consume
from reset_rescue.settings import parse_settings

KEY_A = "a" * 16
KEY_B = "b" * 16
LABEL_A = "b***@e***.com"
LABEL_B = "c***@f***.com"
NOW = 1_800_000_000.0


def credit_inside_window():
    """Expires 5 minutes from now: inside the default 10-minute lead window."""
    return SimpleNamespace(expires_at=NOW + 300.0)


def ask(settings, role, account_key, label, **kwargs):
    return authorize_consume(
        role=role,
        credit=credit_inside_window(),
        settings=settings,
        now=NOW,
        account_key=account_key,
        label=label,
        **kwargs,
    )


def override(key, label, **fields):
    return {"account_overrides": {key: {"label": label, **fields}}}


class PausedAccount(unittest.TestCase):
    def setUp(self):
        self.settings = parse_settings(override(KEY_A, LABEL_A, monitored=False))

    def test_an_automatic_role_is_refused_as_paused(self):
        for role in ("primary", "backup", "scan"):
            with self.subTest(role=role):
                decision = ask(self.settings, role, KEY_A, LABEL_A)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, "account_paused")

    def test_a_confirmed_manual_redeem_is_refused_as_paused(self):
        decision = ask(self.settings, "manual", KEY_A, LABEL_A, confirm=True)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "account_paused")

    def test_the_other_account_is_not_paused(self):
        self.assertTrue(ask(self.settings, "primary", KEY_B, LABEL_B).allowed)


class AutoRedeemPerAccount(unittest.TestCase):
    def test_auto_off_for_one_account_while_global_is_on(self):
        settings = parse_settings(override(KEY_A, LABEL_A, auto_redeem_enabled=False))
        for role in ("primary", "backup", "scan"):
            with self.subTest(role=role):
                decision = ask(settings, role, KEY_A, LABEL_A)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, "auto_redeem_disabled")
        self.assertTrue(ask(settings, "primary", KEY_B, LABEL_B).allowed)

    def test_auto_on_for_one_account_while_global_is_off(self):
        settings = parse_settings(
            {
                "auto_redeem_enabled": False,
                "account_overrides": {KEY_A: {"label": LABEL_A, "auto_redeem_enabled": True}},
            }
        )
        self.assertTrue(ask(settings, "primary", KEY_A, LABEL_A).allowed)
        decision = ask(settings, "primary", KEY_B, LABEL_B)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "auto_redeem_disabled")

    def test_a_confirmed_manual_redeem_still_works_when_auto_is_off(self):
        settings = parse_settings(override(KEY_A, LABEL_A, auto_redeem_enabled=False))
        decision = ask(settings, "manual", KEY_A, LABEL_A, confirm=True)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "manual_confirmed")


class EarlyPerAccount(unittest.TestCase):
    def test_early_on_for_one_account_while_global_is_off(self):
        settings = parse_settings(
            override(KEY_A, LABEL_A, early_redeem_enabled=True, early_redeem_remaining_pct=10)
        )
        self.assertTrue(
            authorize_consume(
                role="early",
                credit=credit_inside_window(),
                settings=settings,
                now=NOW,
                account_key=KEY_A,
                label=LABEL_A,
                quota_reading=5.0,
            ).allowed
        )
        decision = authorize_consume(
            role="early",
            credit=credit_inside_window(),
            settings=settings,
            now=NOW,
            account_key=KEY_B,
            label=LABEL_B,
            quota_reading=5.0,
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "early_redeem_disabled")

    def test_the_account_threshold_is_used(self):
        settings = parse_settings(
            override(KEY_A, LABEL_A, early_redeem_enabled=True, early_redeem_remaining_pct=10)
        )
        decision = authorize_consume(
            role="early",
            credit=credit_inside_window(),
            settings=settings,
            now=NOW,
            account_key=KEY_A,
            label=LABEL_A,
            quota_reading=50.0,
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "quota_above_threshold")

    def test_early_needs_auto_on_for_that_account(self):
        settings = parse_settings(
            override(
                KEY_A,
                LABEL_A,
                auto_redeem_enabled=False,
                early_redeem_enabled=True,
                early_redeem_remaining_pct=10,
            )
        )
        decision = authorize_consume(
            role="early",
            credit=credit_inside_window(),
            settings=settings,
            now=NOW,
            account_key=KEY_A,
            label=LABEL_A,
            quota_reading=0.0,
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "auto_redeem_disabled")
