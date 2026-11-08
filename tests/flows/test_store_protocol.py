"""The store dialect ``service.py`` and ``tests/fakes.FakeStore`` speak.

L1's service facade and the shared fakes use a narrower, dict-shaped store
protocol (``meta_get``/``credit_upsert``/``owned_jobs_list``/``clear_all``…).
The flows use the richer one. Both must work on the *real* ``Store``, or a
production wiring would silently degrade to ``problem_code`` paths.
"""

from __future__ import annotations

import unittest

from reset_rescue.service import Service
from tests.fakes import FakeCron, FakeWrappers
from tests.flows.support import Harness

KEY_A = "a" * 16


class StoreProtocolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness(self)
        self.store = self.h.store

    def test_meta_roundtrip(self):
        self.store.meta_set("salt", "b" * 64)
        self.assertEqual(self.store.meta_get("salt"), "b" * 64)
        self.assertIsNone(self.store.meta_get("absent"))

    def test_history_add_and_tail(self):
        self.store.history_add("scan", outcome="ok", credit_key="c" * 16)
        tail = self.store.history_tail()
        self.assertEqual(tail[0]["action"], "scan")
        self.assertEqual(tail[0]["outcome"], "ok")

    def test_credit_upsert_keeps_flow_fields(self):
        self.store.credit_upsert(
            {
                "credit_key": "c" * 16,
                "account_key": KEY_A,
                "title": "Full reset",
                "expires_at": 1_800_003_600.0,
                "backend_state": "available",
                "status": "scheduled",
                "redeem_request_id": "uuid-persisted",
                "updated_at": 1_800_000_000.0,
            }
        )
        row = self.store.credit_get("c" * 16)
        self.assertEqual(row["redeem_request_id"], "uuid-persisted")
        self.assertEqual(row["status"], "scheduled")
        self.assertEqual(self.store.credit_list(account_key=KEY_A)[0]["title"], "Full reset")

    def test_credit_upsert_refuses_a_row_without_account(self):
        with self.assertRaises(ValueError):
            self.store.credit_upsert({"credit_key": "c" * 16})

    def test_account_and_slot_dialect(self):
        self.store.account_upsert(
            {"account_key": KEY_A, "label": "b***@e***.com", "source_kind": "pool"}
        )
        self.assertEqual(self.store.account_list()[0]["label"], "b***@e***.com")
        self.store.credit_upsert({"credit_key": "c" * 16, "account_key": KEY_A})
        self.store.slot_upsert(
            "c" * 16,
            "primary",
            {
                "fire_at": 1_800_000_600.0,
                "job_id": "job1",
                "job_name": "reset-rescue-cccccccc",
                "state": "armed",
            },
        )
        self.assertEqual(self.store.slot_get("c" * 16, "primary")["job_id"], "job1")
        self.assertEqual(len(self.store.slot_list()), 1)

    def test_owned_job_dialect(self):
        self.store.record_job("job1", "reset-rescue-scan", "scan")
        self.assertEqual(self.store.list_owned_jobs()[0]["kind"], "scan")
        self.store.mark_job_removed("job1")
        # A removed job is kept as history but no longer listed as owned.
        self.assertEqual(self.store.list_owned_jobs(active_only=True), [])
        self.assertIsNotNone(self.store.get_job_by_name("reset-rescue-scan")["removed_at"])

    def test_clear_all_keeps_the_schema_and_drops_rows(self):
        self.store.meta_set("salt", "b" * 64)
        self.store.credit_upsert({"credit_key": "c" * 16, "account_key": KEY_A})
        self.store.clear_all()
        self.assertIsNone(self.store.meta_get("salt"))
        self.assertEqual(self.store.credit_list(), [])
        # The store is still usable: the schema survived.
        self.store.credit_upsert({"credit_key": "d" * 16, "account_key": KEY_A})
        self.assertEqual(len(self.store.credit_list()), 1)


class ServiceOverRealStoreTest(unittest.TestCase):
    """The facade must see the flows' state through the real store."""

    def setUp(self) -> None:
        self.h = Harness(self)
        self.h.deps.cron = FakeCron()
        self.h.deps.wrappers = FakeWrappers()
        self.h.deps.planner = self.h.planner()
        self.h.deps.fire = self.h.fire()
        self.service = Service(self.h.deps)

    def test_status_lists_a_scheduled_credit_with_its_slots(self):
        account = self.h.add_account()
        self.h.add_credit(account, expires_in=30 * 60 * 60.0)
        self.h.deps.planner.scan()
        status = self.service.status()
        self.assertEqual(len(status.accounts), 1)
        listed = status.accounts[0].credits[0]
        self.assertEqual(listed.credit_key, self.h.key(account))
        self.assertEqual(listed.state, "available")
        self.assertEqual(listed.plan, "scheduled")
        self.assertIsNotNone(listed.primary)
        self.assertIsNotNone(listed.backup)
        self.assertLess(listed.primary.fire_at, listed.backup.fire_at)

    def test_status_reports_a_redeemed_credit(self):
        """D-027 (supersedes D-010): a redeemed credit leaves the account listed.

        D-027: every pool account is listed. Once the only credit is spent, the
        account stays in the payload with no banked credit (its row says No resets).
        The credit's own settled state is still readable from the store.
        """
        account, credit = self.h.armed()
        self.h.advance_to(float(self.h.slot(account, "primary")["fire_at"]))
        self.h.deps.fire.run(role="primary")
        status = self.service.status()
        self.assertEqual([c.state for a in status.accounts for c in a.credits], ["redeemed"])
        self.assertEqual(self.h.store.get_credit(credit)["status"], "redeemed")

    def test_history_survives_the_protocol_dialect(self):
        account, credit = self.h.armed()
        self.h.advance_to(float(self.h.slot(account, "primary")["fire_at"]))
        self.h.deps.fire.run(role="primary")
        history = self.service.history()
        self.assertTrue(history.items)
        self.assertEqual(history.items[0].action, "redemption")


if __name__ == "__main__":
    unittest.main(verbosity=2)
