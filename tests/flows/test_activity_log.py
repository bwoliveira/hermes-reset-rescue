"""D-014 — the sanitized activity log.

Every action the plugin takes leaves one row: scan, schedule, reminder,
redemption, refresh, failure. The log is a file the user may share, so the
store — not the caller — is the last line of defence:

* an action outside the fixed vocabulary is refused (no unfilterable rows);
* ``detail`` is a bounded JSON object; a key that names a credential or a raw
  identifier is dropped, and every value is truncated;
* one query function serves the CLI and the API, with the filters both need;
* retention: 14 days, and the 5 000-row cap.
"""

from __future__ import annotations

import json
import unittest

from reset_rescue.store import HISTORY_CAP, Store
from tests.flows.support import DAY, HOUR, Harness


class LogCase(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness(self)
        self.store = self.h.store

    def add(self, **record):
        record.setdefault("at", self.h.clock.now())
        record.setdefault("action", "scan")
        record.setdefault("outcome", "ok")
        self.store.add_history([record])


class Vocabulary(LogCase):
    def test_every_documented_action_is_accepted(self):
        for action in ("scan", "schedule", "reminder", "redemption", "refresh", "failure"):
            self.add(action=action)
        self.assertEqual(self.store.count_history(), 6)

    def test_an_unknown_action_is_refused(self):
        with self.assertRaises(ValueError):
            self.add(action="something_else")
        self.assertEqual(self.store.count_history(), 0)

    def test_the_vocabulary_is_exposed(self):
        self.assertIn("redemption", Store.HISTORY_ACTIONS)
        self.assertIn("reminder", Store.HISTORY_ACTIONS)


class Sanitization(LogCase):
    def test_a_credential_key_is_dropped(self):
        self.add(
            detail={
                "label": "b***@e***.com",
                "token": "sk-secret",
                "refresh_token": "x",
                "authorization": "Bearer y",
            }
        )
        row = self.store.list_history()[0]
        detail = json.loads(row["detail"])
        self.assertEqual(detail, {"label": "b***@e***.com"})

    def test_a_raw_identifier_key_is_dropped(self):
        self.add(
            detail={
                "credit_id": "raw-credit-123",
                "account_id": "acct-9",
                "chat_id": "12345",
                "body": "{...}",
                "role": "primary",
            }
        )
        detail = json.loads(self.store.list_history()[0]["detail"])
        self.assertEqual(detail, {"role": "primary"})

    def test_values_are_truncated(self):
        self.add(detail={"label": "x" * 500})
        detail = json.loads(self.store.list_history()[0]["detail"])
        self.assertEqual(len(detail["label"]), 120)

    def test_the_key_count_is_bounded(self):
        self.add(detail={f"k{index}": "v" for index in range(20)})
        detail = json.loads(self.store.list_history()[0]["detail"])
        self.assertLessEqual(len(detail), 8)

    def test_an_empty_detail_is_null(self):
        self.add(detail={})
        self.assertIsNone(self.store.list_history()[0]["detail"])
        self.add(detail={"token": "x"})
        self.assertIsNone(self.store.list_history()[0]["detail"])

    def test_a_non_mapping_detail_is_ignored(self):
        self.add(detail="just a string")
        self.assertIsNone(self.store.list_history()[0]["detail"])

    def test_the_row_never_carries_a_token_even_when_the_caller_passes_one(self):
        self.add(detail={"token": "sk-live-123", "label": "Full reset"})
        row = self.store.list_history()[0]
        self.assertNotIn("sk-live-123", json.dumps(row))


class Query(LogCase):
    def setUp(self) -> None:
        super().setUp()
        base = self.h.clock.now()
        self.add(at=base, action="scan", outcome="ok")
        self.add(
            at=base + 10,
            action="schedule",
            outcome="scheduled",
            credit_key="c" * 16,
            account_key="a" * 16,
        )
        self.add(
            at=base + 20,
            action="redemption",
            outcome="redeemed",
            credit_key="c" * 16,
            account_key="a" * 16,
        )
        self.add(
            at=base + 30,
            action="failure",
            outcome="failed",
            reason_code="auth_expired",
            account_key="b" * 16,
        )

    def test_newest_first(self):
        rows = self.store.list_history()
        self.assertEqual(
            [row["action"] for row in rows], ["failure", "redemption", "schedule", "scan"]
        )

    def test_filter_by_action(self):
        rows = self.store.list_history(action="redemption")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "redeemed")

    def test_filter_by_account(self):
        rows = self.store.list_history(account_key="a" * 16)
        self.assertEqual(len(rows), 2)

    def test_filter_by_credit(self):
        rows = self.store.list_history(credit_key="c" * 16)
        self.assertEqual(len(rows), 2)

    def test_filter_by_time_window(self):
        base = self.h.clock.now()
        rows = self.store.list_history(since=base + 15)
        self.assertEqual([row["action"] for row in rows], ["failure", "redemption"])
        rows = self.store.list_history(before=base + 15)
        self.assertEqual([row["action"] for row in rows], ["schedule", "scan"])

    def test_filters_combine(self):
        rows = self.store.list_history(action="redemption", account_key="a" * 16)
        self.assertEqual(len(rows), 1)
        rows = self.store.list_history(action="redemption", account_key="b" * 16)
        self.assertEqual(rows, [])

    def test_limit_is_respected_and_never_zero(self):
        self.assertEqual(len(self.store.list_history(limit=2)), 2)
        self.assertEqual(len(self.store.list_history(limit=0)), 1)

    def test_history_tail_is_the_same_query(self):
        self.assertEqual(
            [row["action"] for row in self.store.history_tail(2)], ["failure", "redemption"]
        )


class Retention(LogCase):
    def test_rows_older_than_fourteen_days_are_pruned(self):
        self.add(at=self.h.clock.now() - 15 * DAY, action="scan")
        self.add(at=self.h.clock.now(), action="scan")
        pruned = self.store.prune(self.h.clock.now())
        self.assertEqual(pruned["history"], 1)
        self.assertEqual(self.store.count_history(), 1)

    def test_the_row_cap_still_applies(self):
        base = self.h.clock.now()
        for index in range(HISTORY_CAP + 50):
            self.add(at=base + index, action="scan")
        self.assertEqual(self.store.count_history(), HISTORY_CAP)

    def test_the_cap_keeps_the_newest_rows(self):
        base = self.h.clock.now()
        for index in range(HISTORY_CAP + 5):
            self.add(at=base + index, action="scan", outcome=str(index))
        newest = self.store.list_history(limit=1)[0]
        self.assertEqual(newest["outcome"], str(HISTORY_CAP + 4))


class FlowsWriteIt(LogCase):
    """The flows log their own actions without the caller asking."""

    def test_a_scan_logs_a_scan_row(self):
        self.h.add_account()
        self.h.scan()
        rows = self.store.list_history(action="scan")
        self.assertEqual(len(rows), 1)
        detail = json.loads(rows[0]["detail"])
        self.assertIn("accounts", detail)

    def test_a_schedule_logs_a_schedule_row(self):
        account = self.h.add_account()
        self.h.add_credit(account, expires_in=30 * HOUR)
        self.h.scan()
        rows = self.store.list_history(action="schedule")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["credit_key"], self.h.key(account))

    def test_a_redemption_logs_a_redemption_row(self):
        account = self.h.add_account()
        self.h.add_credit(account, expires_in=30 * HOUR)
        self.h.scan()
        self.h.advance_to(float(self.h.slot(account, "primary")["fire_at"]))
        self.h.run_fire("primary")
        rows = self.store.list_history(action="redemption")
        self.assertTrue(rows)
        self.assertEqual(rows[0]["outcome"], "redeemed")

    def test_a_failed_redemption_logs_a_failure_row(self):
        account = self.h.add_account()
        self.h.add_credit(account, expires_in=30 * HOUR)
        self.h.scan()
        self.h.backend.on_post = lambda *_: {"code": "rate_limit_reset_ineligible"}
        self.h.advance_to(float(self.h.slot(account, "primary")["fire_at"]))
        self.h.run_fire("primary")
        rows = self.store.list_history(action="failure")
        self.assertTrue(rows)
        self.assertEqual(rows[0]["reason_code"], "rate_limit_reset_ineligible")

    def test_a_reminder_logs_a_reminder_row(self):
        account = self.h.add_account()
        self.h.add_credit(account, expires_in=30 * HOUR)
        self.h.scan()
        slot = self.h.slot(account, "reminder1")
        self.assertIsNotNone(slot)
        self.h.advance_to(float(slot["fire_at"]) + 5)
        self.h.fire().run_remind()
        rows = self.store.list_history(action="reminder")
        self.assertEqual(len(rows), 1)

    def test_a_quota_check_logs_a_row(self):
        from reset_rescue.settings import parse_settings

        self.h.deps.settings = parse_settings(
            {"early_redeem_enabled": True, "early_redeem_weekly_remaining_pct": 0}
        )
        account = self.h.add_account()
        self.h.add_credit(account, expires_in=30 * HOUR)
        self.h.scan()
        self.h.planner().quota_check()
        rows = self.store.list_history(action="quota_check")
        self.assertEqual(len(rows), 1)

    def test_no_logged_row_contains_a_token_or_a_raw_credit_id(self):
        account = self.h.add_account()
        credit = self.h.add_credit(account, expires_in=30 * HOUR)
        self.h.scan()
        self.h.advance_to(float(self.h.slot(account, "primary")["fire_at"]))
        self.h.run_fire("primary")
        blob = json.dumps(self.store.list_history(limit=200))
        self.assertNotIn(credit.id, blob)
        self.assertNotIn("token", blob.lower())
        self.assertNotIn(account.account_key, blob.replace(account.account_key, "", 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
