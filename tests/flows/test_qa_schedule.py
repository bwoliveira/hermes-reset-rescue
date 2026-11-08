"""QA scenarios S1-S5 and S15: install scan, horizon, fire, network retry, redemption race.

Run through the flows harness (fake backend, fake cron, fake notifier, fake clock).
Every assertion is on what the plugin sent or persisted, not on its printed output.

    python3 <RUNTIME_WRAPPER> <wt>/tests/flows/run.py
"""

from __future__ import annotations

import unittest

from reset_rescue.cron_gateway import slot_job_name
from tests.flows.support import HOUR, Harness


class ScheduleCase(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness(self)
        self.account = self.h.add_account()

    def row(self, credit_id: str = "credit-1") -> dict:
        row = self.h.row(self.account, credit_id)
        self.assertIsNotNone(row, f"no credit row for {credit_id}")
        return row

    def successes(self) -> list[dict]:
        return self.h.notices("success")


class S1InstallScanStatus(ScheduleCase):
    """S1 engine subset: scanning shows a credit; no-credit scan arms no slots."""

    def test_scan_on_a_fresh_install_lists_the_credit_and_exits_zero(self):
        self.h.add_credit(self.account, expires_in=30 * HOUR)
        result = self.h.scan()
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.row()["backend_state"], "available")

    def test_scan_with_no_credits_creates_no_jobs(self):
        result = self.h.scan()
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.h.job_names(), [])


class S2OutsideHorizon(ScheduleCase):
    """S2: a credit beyond the 48 h horizon is shown but not armed; inside it gets fire+backup."""

    def test_credit_beyond_horizon_has_no_fire_or_backup_job(self):
        self.h.add_credit(self.account, expires_in=60 * HOUR)
        result = self.h.scan()
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.h.job_names(), [])
        self.assertIsNone(self.h.slot(self.account, "primary"))
        self.assertIsNone(self.h.slot(self.account, "backup"))

    def test_credit_enters_horizon_after_time_advances(self):
        credit = self.h.add_credit(self.account, expires_in=60 * HOUR)
        self.assertEqual(self.h.scan().exit_code, 0)
        self.assertIsNone(self.h.slot(self.account, "primary"))
        self.h.advance(13 * HOUR)
        self.assertEqual(self.h.scan().exit_code, 0)
        primary = self.h.slot(self.account, "primary")
        backup = self.h.slot(self.account, "backup")
        self.assertIsNotNone(primary)
        self.assertIsNotNone(backup)
        assert primary is not None and backup is not None
        settings = self.h.deps.settings
        assert settings is not None
        self.assertAlmostEqual(
            float(primary["fire_at"]), credit.expires_at - settings.primary_fire_offset_s
        )
        self.assertAlmostEqual(
            float(backup["fire_at"]), credit.expires_at - settings.backup_fire_offset_s
        )

    def test_credit_inside_horizon_gets_primary_and_backup_at_expiry_minus_offsets(self):
        credit = self.h.add_credit(self.account, expires_in=30 * HOUR)
        self.assertEqual(self.h.scan().exit_code, 0)
        settings = self.h.deps.settings
        primary = self.h.slot(self.account, "primary")
        backup = self.h.slot(self.account, "backup")
        self.assertIsNotNone(primary)
        self.assertIsNotNone(backup)
        self.assertAlmostEqual(
            float(primary["fire_at"]), credit.expires_at - settings.primary_fire_offset_s, places=3
        )
        self.assertAlmostEqual(
            float(backup["fire_at"]), credit.expires_at - settings.backup_fire_offset_s, places=3
        )
        self.assertLess(float(primary["fire_at"]), float(backup["fire_at"]))
        key = self.h.key(self.account)
        self.assertIn(slot_job_name("primary", key), self.h.job_names())
        self.assertIn(slot_job_name("backup", key), self.h.job_names())


class S3FireOnePost(ScheduleCase):
    """S3 engine subset: one fire POSTs, persists its UUID and notifies once."""

    def setUp(self) -> None:
        super().setUp()
        self.credit = self.h.add_credit(self.account, expires_in=30 * HOUR)
        self.assertEqual(self.h.scan().exit_code, 0)
        self.h.advance_to(float(self.h.slot(self.account, "primary")["fire_at"]))

    def test_one_post_with_a_persisted_uuid_that_matches_the_readback(self):
        seen_before_post = []

        def capture_intent(account_key, credit_id, request_id):
            seen_before_post.append(self.row()["redeem_request_id"])
            return None

        self.h.backend.on_post = capture_intent
        result = self.h.run_fire("primary")
        self.assertEqual(result.exit_code, 0)
        posts = self.h.backend.posts
        self.assertEqual(seen_before_post, [posts[0]["redeem_request_id"]])
        self.assertEqual(len(posts), 1, "exactly one POST for one fire")
        row = self.row()
        self.assertEqual(row["status"], "redeemed")
        self.assertEqual(row["redeem_request_id"], posts[0]["redeem_request_id"])
        self.assertTrue(posts[0]["redeem_request_id"])

    def test_exactly_one_success_notice(self):
        self.h.run_fire("primary")
        self.assertEqual(len(self.successes()), 1)

    def test_the_fire_slot_is_marked_run_and_the_backend_credit_is_redeemed(self):
        self.h.run_fire("primary")
        self.assertIsNotNone(self.h.slot(self.account, "primary")["ran_at"])
        backend_row = self.h.backend.find(self.account.account_key, self.credit.id)
        self.assertIsNotNone(backend_row)
        assert backend_row is not None
        self.assertEqual(backend_row.status, "redeemed")


class S4NetworkRetrySameId(ScheduleCase):
    """S4: a transient network failure is retried with the SAME UUID and no duplicate."""

    def setUp(self) -> None:
        super().setUp()
        self.credit = self.h.add_credit(self.account, expires_in=30 * HOUR)
        self.assertEqual(self.h.scan().exit_code, 0)
        self.h.advance_to(float(self.h.slot(self.account, "primary")["fire_at"]))

    def test_transient_failure_then_success_reuses_the_uuid_and_posts_once_redeemed(self):
        calls = {"n": 0}

        def flaky(account_key, credit_id, request_id):
            calls["n"] += 1
            if calls["n"] == 1:
                # A real transport exception, not a 200 body: this is the path
                # _post_with_retries retries (TRANSIENT_CODES, fire.py:808).
                raise ConnectionError("simulated reset before response")
            return None  # fall through to the real backend answer

        self.h.backend.on_post = flaky
        self.h.run_fire("primary")
        posts = self.h.backend.posts
        self.assertEqual(len(posts), 2, "expected exactly one retry after the network error")
        ids = {p["redeem_request_id"] for p in posts}
        self.assertEqual(len(ids), 1, "retry changed the UUID")
        self.assertEqual(self.row()["status"], "redeemed")
        self.assertEqual(len(self.successes()), 1)


class S15RedemptionRaceNoDuplicate(ScheduleCase):
    """S5/S15: a credit redeemed elsewhere (external or coexisting redeemer) is not POSTed again."""

    def setUp(self) -> None:
        super().setUp()
        self.credit = self.h.add_credit(self.account, expires_in=30 * HOUR)
        self.assertEqual(self.h.scan().exit_code, 0)
        self.h.advance_to(float(self.h.slot(self.account, "primary")["fire_at"]))

    def test_external_redemption_before_fire_causes_no_post_and_no_failure_notice(self):
        self.credit.status = "redeemed"
        self.credit.redeemed_at = self.h.clock.now()
        before = len(self.h.notices())
        result = self.h.run_fire("primary")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.h.backend.posts, [])
        self.assertEqual(self.successes(), [])
        # fire.py:613-622 reports an externally redeemed credit as an info
        # ("already_redeemed"), never as a failure or a retry.
        fired = self.h.notices()[before:]
        self.assertEqual([n["kind"] for n in fired], ["info"], fired)

    def test_redemption_after_fire_sent_is_not_posted_twice_by_the_backup(self):
        self.h.run_fire("primary")
        self.assertEqual(len(self.h.backend.posts), 1)
        self.h.advance_to(float(self.h.slot(self.account, "backup")["fire_at"]))
        self.h.run_fire("backup")
        self.assertEqual(len(self.h.backend.posts), 1, "backup POSTed an already-redeemed credit")
        self.assertEqual(len(self.successes()), 1)


if __name__ == "__main__":
    unittest.main()
