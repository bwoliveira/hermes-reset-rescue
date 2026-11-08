"""D-028 / D-034: every account row says whether Excluded accounts names it.

The popup reads Watch's inherited value from this ``excluded`` flag instead of
re-deriving it, so the flag must agree with the engine's own matcher
(``Settings.account_included``) and must never carry a full e-mail address.
"""

from __future__ import annotations

import json
import unittest

from reset_rescue.service import Service
from reset_rescue.settings import parse_settings
from tests.api.test_r4_settings_race import FULL_A
from tests.api.test_r4_settings_race import KEY_A as POST_KEY_A
from tests.api.test_r4_settings_race import KEY_B as POST_KEY_B
from tests.api.test_r4_settings_race import _Base as AccountRouteBase
from tests.fakes import FakeStore, make_deps

KEY_A = "a" * 16
KEY_B = "c" * 16
LABEL_A = "d***@e***.com"  # canonical label of dave@example.com
LABEL_B = "c***@f***.com"
FULL_EMAIL = "dave@example.com"
LEGACY_MASK = "d***@example.com"
CURRENT_MASK = "d***@e***.com"


def _store(label_a: str) -> FakeStore:
    store = FakeStore()
    for key, label in ((KEY_A, label_a), (KEY_B, LABEL_B)):
        store.account_upsert(
            {
                "account_key": key,
                "label": label,
                "source_kind": "pool",
                "last_list_ok_at": 1_800_000_000.0,
                "last_error_code": None,
            }
        )
    return store


def _views(excluded: str, label_a: str = LABEL_A) -> tuple[dict, dict, str]:
    """(status accounts by key, per-account rows by key, both responses as text)."""
    settings = parse_settings({"excluded_accounts": excluded})
    service = Service(make_deps(store=_store(label_a), settings=settings))
    status = service.status().to_dict()
    per_account = service.per_account_view()
    status_rows = {row["account_key"]: row for row in status["accounts"]}
    view_rows = {row["account_key"]: row for row in per_account["accounts"]}
    text = json.dumps([status, per_account], default=str)
    return status_rows, view_rows, text


class ExcludedFlag(unittest.TestCase):
    def assert_flags(self, excluded: str, want_a: bool, why: str, label_a: str = LABEL_A) -> None:
        status_rows, view_rows, _text = _views(excluded, label_a)
        for name, rows in (("status", status_rows), ("per-account", view_rows)):
            with self.subTest(view=name, why=why):
                self.assertIs(rows[KEY_A]["excluded"], want_a)
                self.assertIs(rows[KEY_B]["excluded"], False, "the other account is never excluded")

    def test_a_legacy_masked_entry_excludes_the_account(self) -> None:
        self.assert_flags(LEGACY_MASK, True, "legacy mask entry")

    def test_a_current_masked_entry_excludes_the_account(self) -> None:
        self.assert_flags(CURRENT_MASK, True, "current mask entry")

    def test_a_full_email_entry_excludes_the_account(self) -> None:
        self.assert_flags(FULL_EMAIL, True, "full e-mail entry")

    def test_a_full_email_stored_label_still_matches_a_masked_entry(self) -> None:
        self.assert_flags(
            CURRENT_MASK, True, "stored label is the full address", label_a=FULL_EMAIL
        )

    def test_an_account_key_entry_excludes_the_account(self) -> None:
        self.assert_flags(KEY_A, True, "account_key entry")

    def test_an_account_not_named_is_not_excluded(self) -> None:
        self.assert_flags("x***@z***.com", False, "an entry naming neither account")

    def test_another_accounts_label_excludes_only_that_account(self) -> None:
        status_rows, view_rows, _text = _views(LABEL_B)
        for rows in (status_rows, view_rows):
            self.assertIs(rows[KEY_A]["excluded"], False)
            self.assertIs(rows[KEY_B]["excluded"], True)

    def test_no_exclusions_means_nothing_is_excluded(self) -> None:
        self.assert_flags("", False, "empty Excluded accounts")

    def test_the_flag_ignores_the_accounts_own_watch_override(self) -> None:
        # ``excluded`` is the list match alone; ``monitored`` is the decision.
        settings = parse_settings({"excluded_accounts": CURRENT_MASK})
        self.assertIs(settings.excluded_by_list(KEY_A, LABEL_A), True)
        self.assertIs(settings.account_included(KEY_A, LABEL_A), False)

    def test_no_full_email_appears_in_either_response(self) -> None:
        for excluded in (LEGACY_MASK, CURRENT_MASK, FULL_EMAIL, KEY_A):
            with self.subTest(excluded=excluded):
                _status, _view, text = _views(excluded, label_a=FULL_EMAIL)
                self.assertNotIn(FULL_EMAIL, text)
                self.assertNotIn("dave@", text)


class PostAccountExcludedFlag(AccountRouteBase):
    def assert_post_flag(self, excluded, account_key, expected):
        self.write_config({"excluded_accounts": excluded})
        response = self.post_account(account_key, "reminders_enabled", False)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIs(body["excluded"], expected)
        row = next(row for row in body["status"]["accounts"] if row["account_key"] == account_key)
        self.assertIs(row["excluded"], expected)
        self.assertNotIn(FULL_A, response.text)
        self.assertNotIn("alice@", response.text)

    def test_post_reports_true_for_an_excluded_account(self):
        for excluded in ([POST_KEY_A], POST_KEY_A, "a***@example.com", "a***@e***.com", FULL_A):
            with self.subTest(excluded=excluded):
                self.assert_post_flag(excluded, POST_KEY_A, True)

    def test_popup_exclusions_are_saved_as_a_list_but_read_back_as_csv(self):
        self.write_config({})
        response = self.http.post(
            "/api/plugins/reset-rescue/settings",
            json={"key": "excluded_accounts", "value": POST_KEY_A},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["value"], [POST_KEY_A])
        self.assertEqual(self.read_settings().excluded_accounts, (POST_KEY_A,))
        fields = self.http.get("/api/plugins/reset-rescue/settings").json()["fields"]
        field = next(row for row in fields if row["key"] == "excluded_accounts")
        self.assertEqual(field["type"], "str")
        self.assertEqual(field["value"], POST_KEY_A)
        self.assertNotIn(FULL_A, response.text)

    def test_post_reports_false_for_an_account_not_excluded(self):
        self.assert_post_flag([POST_KEY_A], POST_KEY_B, False)


if __name__ == "__main__":
    unittest.main()
