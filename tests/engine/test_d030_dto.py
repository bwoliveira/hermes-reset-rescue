"""D-030: ``AccountDTO`` carries ``monitored``, ``overrides`` and ``effective``."""

from __future__ import annotations

import unittest

from reset_rescue.dto import AccountDTO
from reset_rescue.service import Service
from reset_rescue.settings import parse_settings
from tests.fakes import FakeStore, make_deps

KEY_A = "a" * 16
LABEL_A = "b***@e***.com"
EFFECTIVE_FIELDS = {
    "auto_redeem_enabled",
    "early_redeem_enabled",
    "early_redeem_remaining_pct",
    "reminders_enabled",
}


def store_with_account() -> FakeStore:
    store = FakeStore()
    store.account_upsert(
        {
            "account_key": KEY_A,
            "label": LABEL_A,
            "source_kind": "pool",
            "last_list_ok_at": 1_800_000_000.0,
            "last_error_code": None,
        }
    )
    return store


def account_view(raw: dict):
    service = Service(make_deps(store=store_with_account(), settings=parse_settings(raw)))
    accounts = service.status().accounts
    assert len(accounts) == 1, accounts
    return accounts[0]


class AccountFields(unittest.TestCase):
    def test_the_dto_declares_the_new_fields(self):
        for name in ("monitored", "overrides", "effective"):
            self.assertIn(name, AccountDTO.FIELDS)

    def test_no_override_means_monitored_and_the_global_values(self):
        view = account_view({"auto_redeem_enabled": False})
        self.assertTrue(view.monitored)
        self.assertEqual(view.overrides, {})
        self.assertEqual(set(view.effective), EFFECTIVE_FIELDS)
        self.assertFalse(view.effective["auto_redeem_enabled"])

    def test_overrides_list_only_the_fields_that_are_set(self):
        view = account_view(
            {
                "account_overrides": {
                    KEY_A: {
                        "label": LABEL_A,
                        "auto_redeem_enabled": False,
                        "reminders_enabled": False,
                    }
                }
            }
        )
        self.assertEqual(view.overrides, {"auto_redeem_enabled": False, "reminders_enabled": False})
        self.assertFalse(view.effective["auto_redeem_enabled"])
        self.assertFalse(view.effective["reminders_enabled"])

    def test_a_paused_account_reads_as_not_monitored(self):
        view = account_view({"account_overrides": {KEY_A: {"label": LABEL_A, "monitored": False}}})
        self.assertFalse(view.monitored)
        self.assertEqual(view.overrides, {"monitored": False})

    def test_an_excluded_account_reads_as_not_monitored(self):
        view = account_view({"excluded_accounts": LABEL_A})
        self.assertFalse(view.monitored)
        self.assertFalse(view.included)


class StatusAccountCount(unittest.TestCase):
    """The status payload carries ``account_count`` and ``per_account_available``."""

    def status_for(self, raw: dict, extra_keys: tuple = ()):
        store = store_with_account()
        for key in extra_keys:
            store.account_upsert(
                {
                    "account_key": key,
                    "label": "c***@f***.com",
                    "source_kind": "pool",
                    "last_list_ok_at": 1_800_000_000.0,
                    "last_error_code": None,
                }
            )
        return Service(make_deps(store=store, settings=parse_settings(raw))).status()

    def test_one_account_no_override(self):
        status = self.status_for({})
        self.assertEqual(status.pool_account_count, 1)
        self.assertFalse(status.per_account_available)

    def test_one_account_with_override(self):
        status = self.status_for(
            {"account_overrides": {KEY_A: {"label": LABEL_A, "monitored": False}}}
        )
        self.assertEqual(status.pool_account_count, 1)
        self.assertTrue(status.per_account_available)

    def test_two_accounts_no_override(self):
        status = self.status_for({}, extra_keys=("c" * 16,))
        self.assertEqual(status.pool_account_count, 2)
        self.assertTrue(status.per_account_available)

    def test_a_paused_account_is_still_counted(self):
        status = self.status_for(
            {"account_overrides": {KEY_A: {"label": LABEL_A, "monitored": False}}},
            extra_keys=("c" * 16,),
        )
        self.assertEqual(status.pool_account_count, 2)
