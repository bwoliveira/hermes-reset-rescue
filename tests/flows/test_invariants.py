"""T-08 — the invariants, written before the flows exist.

One test per R1-R8 counterexample of ``codex-credit-redeemer/review-r1.md``
plus the I1-I7 properties of architecture A§9. Every scenario is ported from
the production regressions (``r1_regressions.py`` / ``r2_regressions.py``) to
this plugin's interfaces; the assertions are about behaviour, never about the
old implementation.

Run red first: each test imports the flow it constrains lazily, so a missing
``planner.py``/``fire.py`` fails that test alone with an ImportError instead of
taking the module down.
"""

from __future__ import annotations

import unittest

from tests.flows.support import HOUR, MINUTE, Harness

#: The script timeout the scheduler enforces (A§7.5: 3 600 s).
SCRIPT_TIMEOUT_S = 3_600.0
#: GC waits the script timeout plus this before removing a finished job.
GC_MARGIN_S = 600.0
#: A slot is due from its own time, never earlier (A§8.3).
DUE_SLACK_S = 30.0


class FlowCase(unittest.TestCase):
    """Base: one harness per test, plus the lazy flow imports."""

    harness_kwargs: dict = {}

    def setUp(self) -> None:
        self.h = Harness(self, **self.harness_kwargs)

    @property
    def planner(self):
        from reset_rescue.planner import Planner

        return Planner(self.h.deps, self.h.store)

    @property
    def fire(self):
        from reset_rescue.fire import Fire

        return Fire(self.h.deps, self.h.store)

    def scan(self, **kwargs):
        return self.planner.scan(**kwargs)

    def run_fire(self, role: str = "primary", **kwargs):
        return self.fire.run(role=role, **kwargs)

    # -- shared scenario ---------------------------------------------------
    def armed(self, *, expires_in: float = 30 * HOUR, credit_id: str = "credit-1"):
        """One account + one credit, scheduled by a real scan."""
        account = self.h.add_account()
        credit = self.h.add_credit(account, credit_id, expires_in=expires_in)
        result = self.scan()
        self.assertEqual(result.exit_code, 0, result)
        return account, credit

    def at_slot(
        self, account, role: str, credit_id: str = "credit-1", *, early: float = 0.0
    ) -> float:
        """Move the clock to the slot's own time (+ ``early`` seconds)."""
        slot = self.h.slot(account, role, credit_id)
        self.assertIsNotNone(slot, f"no {role} slot")
        self.h.advance_to(float(slot["fire_at"]) + early)
        return float(slot["fire_at"])


# ============================================================ I1 / R4 / R5


class I1NeverDoneWhileListed(FlowCase):
    """I1: a credit the backend still lists as available/pending is never done."""

    def test_i1_pending_credit_is_posted_never_announced_as_redeemed(self):
        """R5: backend ``redeeming`` + ``redeem_started_at`` is work in progress."""
        account, credit = self.armed()
        credit.status = "redeeming"
        credit.redeem_started_at = self.h.clock.now() - 30
        self.at_slot(account, "primary")
        result = self.run_fire("primary")
        self.assertEqual(len(self.h.backend.posts), 1, "a pending credit must be posted")
        self.assertEqual(result.exit_code, 0, result)
        self.assertEqual(self.h.row(account)["status"], "redeemed")
        kinds = [notice["kind"] for notice in self.h.notices()]
        self.assertIn("success", kinds)

    def test_i1_available_with_redeem_started_at_is_pending(self):
        """R5 second form: ``available`` + ``redeem_started_at`` is not a clean slot."""
        account, credit = self.armed()
        credit.redeem_started_at = self.h.clock.now() - 5
        self.at_slot(account, "primary")
        self.run_fire("primary")
        self.assertEqual(len(self.h.backend.posts), 1)
        self.assertEqual(self.h.row(account)["status"], "redeemed")

    def test_i1_unreadable_readback_is_unconfirmed_not_success(self):
        """R4: a consume that answers ``reset`` but cannot be confirmed is not success."""
        account, credit = self.armed()

        def post_then_break(account_key, credit_id, request_id):
            self.h.backend.fail_list[account_key] = ConnectionError("readback down")
            return {"code": "reset", "windows_reset": 1}

        self.h.backend.on_post = post_then_break
        self.at_slot(account, "primary")
        result = self.run_fire("primary")
        row = self.h.row(account)
        self.assertNotEqual(row["status"], "redeemed", "unreadable readback became success")
        self.assertIsNone(row["confirmed_at"])
        self.assertEqual(result.exit_code, 1)
        kinds = [notice["kind"] for notice in self.h.notices()]
        self.assertNotIn("success", kinds)
        self.assertIn("retry_pending", kinds)

    def test_i1_readback_still_listing_the_credit_is_unconfirmed(self):
        """R4: a 200 that still lists the credit is not evidence of a redemption."""
        account, credit = self.armed()

        def post_without_effect(account_key, credit_id, request_id):
            return {"code": "reset", "windows_reset": 1}

        self.h.backend.on_post = post_without_effect
        self.at_slot(account, "primary")
        result = self.run_fire("primary")
        row = self.h.row(account)
        self.assertNotEqual(row["status"], "redeemed")
        self.assertEqual(result.exit_code, 1)
        self.assertNotIn("success", [notice["kind"] for notice in self.h.notices()])

    def test_i1_absent_after_post_is_confirmed(self):
        """I1: absent from a successful list after ``posted_at`` IS the confirmation."""
        account, credit = self.armed()

        def post_and_vanish(account_key, credit_id, request_id):
            self.h.backend.remove(account_key, credit_id)
            return {"code": "reset", "windows_reset": 1}

        self.h.backend.on_post = post_and_vanish
        self.at_slot(account, "primary")
        result = self.run_fire("primary")
        row = self.h.row(account)
        self.assertEqual(row["status"], "redeemed")
        self.assertIsNotNone(row["confirmed_at"])
        self.assertEqual(result.exit_code, 0)

    def test_i1_redeemed_in_backend_is_already_redeemed_without_a_post(self):
        account, credit = self.armed()
        credit.status = "redeemed"
        credit.redeemed_at = self.h.clock.now() - 10
        self.at_slot(account, "primary")
        result = self.run_fire("primary")
        self.assertEqual(self.h.backend.posts, [])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.h.row(account)["status"], "already_redeemed")
        self.assertIn("info", [notice["kind"] for notice in self.h.notices()])

    def test_i1_unknown_backend_state_is_never_success(self):
        account, credit = self.armed()
        credit.status = "weird_future_state"
        self.at_slot(account, "primary")
        result = self.run_fire("primary")
        self.assertEqual(self.h.backend.posts, [])
        self.assertNotEqual(self.h.row(account)["status"], "redeemed")
        self.assertEqual(result.exit_code, 1)


# ================================================================== I2 / R5


class I2ReDecideFromBackend(FlowCase):
    """I2: every run re-decides from a fresh list with the persisted UUID."""

    def test_i2_uuid_is_persisted_before_the_post_and_reused_on_retry(self):
        account, credit = self.armed()
        self.h.backend.on_post = lambda *_: {"code": "http_error"}
        self.at_slot(account, "primary")
        self.run_fire("primary")
        first = self.h.backend.posts[0]["redeem_request_id"]
        self.assertTrue(first)
        self.assertEqual(self.h.row(account)["redeem_request_id"], first)

        # The backup retries the same credit: same UUID, never a new one.
        self.h.backend.on_post = None
        self.at_slot(account, "backup")
        self.run_fire("backup")
        self.assertEqual(len(self.h.backend.posts), 2)
        self.assertEqual(self.h.backend.posts[1]["redeem_request_id"], first)
        self.assertEqual(self.h.row(account)["status"], "redeemed")

    def test_i2_local_status_never_blocks_a_credit_the_backend_lists(self):
        account, credit = self.armed()
        self.h.store.update_credit(
            self.h.key(account), status="failed", last_failure_category="definitive"
        )
        self.at_slot(account, "primary")
        self.run_fire("primary")
        self.assertEqual(len(self.h.backend.posts), 1)
        self.assertEqual(self.h.row(account)["status"], "redeemed")

    def test_i2_absent_before_any_post_is_vanished_not_redeemed(self):
        account, credit = self.armed()
        self.h.backend.remove(account.account_key, credit.id)
        self.at_slot(account, "primary")
        result = self.run_fire("primary")
        self.assertEqual(self.h.backend.posts, [])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.h.row(account)["status"], "vanished")
        self.assertIn("info", [notice["kind"] for notice in self.h.notices()])

    def test_i2_a_credit_redeemed_elsewhere_leaves_the_run_silent(self):
        """Two systems may race (D-002): the loser must not POST or cry failure."""
        account, credit = self.armed()
        self.at_slot(account, "primary")
        credit.status = "redeemed"
        credit.redeemed_at = self.h.clock.now()
        result = self.run_fire("primary")
        self.assertEqual(self.h.backend.posts, [])
        self.assertEqual(result.exit_code, 0)


# ================================================================ I3 / R1


class I3RoleComesFromTheJob(FlowCase):
    """I3: role is an argv constant; no early window; lock wait never flips it."""

    def test_i3_no_early_window(self):
        account, credit = self.armed()
        self.at_slot(account, "primary", early=-120)
        result = self.run_fire("primary")
        self.assertEqual(self.h.backend.posts, [])
        self.assertEqual(result.exit_code, 0)
        self.assertIsNone(self.h.slot(account, "primary")["ran_at"])

    def test_i3_backup_does_nothing_before_its_own_time(self):
        account, credit = self.armed()
        self.at_slot(account, "primary")
        result = self.run_fire("backup")
        self.assertEqual(self.h.backend.posts, [])
        self.assertEqual(result.exit_code, 0)

    def test_i3_delayed_primary_never_handles_the_backup_slot(self):
        """R1: a primary that runs late keeps its role and never spends the backup."""
        account, credit = self.armed()
        self.h.backend.on_post = lambda *_: {"code": "http_error"}
        # The primary runs 110 s late, while the backup is still ahead of it.
        self.at_slot(account, "primary", early=110)
        self.run_fire("primary")
        primary = self.h.slot(account, "primary")
        backup = self.h.slot(account, "backup")
        self.assertIsNotNone(primary["ran_at"])
        self.assertIsNone(backup["ran_at"], "the primary marked the backup slot")

        # The backup's own time arrives: it still gets its full attempt.
        self.h.backend.on_post = None
        self.at_slot(account, "backup")
        result = self.run_fire("backup")
        self.assertEqual(len(self.h.backend.posts), 2, "the backup lost its attempt")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.h.row(account)["status"], "redeemed")

    def test_i3_a_primary_that_runs_after_the_backup_time_still_uses_its_own_slot(self):
        """R1: lateness is bounded, and the role never turns into the other one."""
        account, credit = self.armed()
        self.at_slot(account, "backup", early=10)  # both slots are now in the past
        self.run_fire("primary")
        self.assertEqual(len(self.h.backend.posts), 1)
        self.assertIsNotNone(self.h.slot(account, "primary")["ran_at"])
        self.assertIsNone(self.h.slot(account, "backup")["ran_at"])

    def test_i3_each_slot_has_its_own_ran_at(self):
        account, credit = self.armed()
        self.h.backend.on_post = lambda *_: {"code": "http_error"}
        self.at_slot(account, "primary")
        self.run_fire("primary")
        self.assertIsNotNone(self.h.slot(account, "primary")["ran_at"])
        self.assertIsNone(self.h.slot(account, "backup")["ran_at"])

    def test_i3_a_second_run_of_the_same_role_is_silent(self):
        account, credit = self.armed()
        self.at_slot(account, "primary")
        self.run_fire("primary")
        posts = len(self.h.backend.posts)
        second = self.run_fire("primary")
        self.assertEqual(len(self.h.backend.posts), posts)
        self.assertEqual(second.exit_code, 0)

    def test_i3_a_slot_older_than_the_late_window_is_left_to_the_planner(self):
        """R9D: past ``MAX_LATE`` the fire does nothing; the planner owns ``missed``."""
        account, credit = self.armed()
        self.at_slot(account, "backup", early=20 * MINUTE)
        result = self.run_fire("primary")
        self.assertEqual(self.h.backend.posts, [])
        self.assertEqual(result.exit_code, 0)


# ================================================================ I4 / R2


class I4RefreshOn401(FlowCase):
    """I4/R2: a 401 on the list is never mistaken for an empty list."""

    def test_i4_a_401_on_the_list_is_a_failure_not_an_empty_list(self):
        account, credit = self.armed()
        self.h.backend.fail_list[account.account_key] = ConnectionError("401")
        # The source maps a 401 to a 401 Response; the API turns it into auth_expired.
        self.h.backend.fail_list.clear()

        def unauthorized(account_key):
            from reset_rescue.accounts import Response

            return Response(status=401, body=None)

        # Script the raw 401 through the source's list path.
        original = self.h.source.request

        def request(account_ref, method, url, *, json=None, timeout=30.0):
            if "/rate-limit-reset-credits" in url and method.upper() == "GET":
                from reset_rescue.accounts import Response

                return Response(status=401, body=None)
            return original(account_ref, method, url, json=json, timeout=timeout)

        self.h.source.request = request  # type: ignore[method-assign]
        self.at_slot(account, "primary")
        result = self.run_fire("primary")
        self.assertEqual(self.h.backend.posts, [])
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.h.row(account)["status"], "failed")
        self.assertNotEqual(self.h.row(account)["status"], "redeemed")
        self.assertEqual(self.h.row(account)["last_reason"], "auth_expired")

    def test_i4_auth_expired_never_settles_the_credit(self):
        account, credit = self.armed()
        self.h.backend.fail_list[account.account_key] = ConnectionError("down")
        self.at_slot(account, "primary")
        result = self.run_fire("primary")
        self.assertEqual(result.exit_code, 1)
        self.assertIsNone(self.h.row(account)["confirmed_at"])
        # The next slot still tries, and succeeds once the backend is healthy.
        self.h.backend.fail_list.clear()
        self.at_slot(account, "backup")
        self.run_fire("backup")
        self.assertEqual(self.h.row(account)["status"], "redeemed")

    def test_i4_planner_records_the_account_error_and_keeps_other_accounts(self):
        good = self.h.add_account("g***@e***.com")
        bad = self.h.add_account("x***@e***.com")
        self.h.add_credit(good, "credit-good", expires_in=2 * HOUR)
        self.h.add_credit(bad, "credit-bad", expires_in=2 * HOUR)
        self.h.backend.fail_list[bad.account_key] = ConnectionError("down")
        result = self.scan()
        self.assertEqual(result.exit_code, 1)
        self.assertIsNotNone(self.h.store.get_account(bad.account_key)["last_error_code"])
        # The healthy account was still scheduled.
        self.assertIsNotNone(self.h.slot(good, "primary", "credit-good"))
        self.assertIsNone(self.h.slot(bad, "primary", "credit-bad"))


# ================================================================ I6 / R6


class I6OneNoticePerResult(FlowCase):
    """I6/R6: notices are per credit and survive a mixed batch."""

    def test_i6_mixed_batch_keeps_the_success_notice(self):
        """R6: one failing credit must not swallow the other's success notice."""
        ok = self.h.add_account("ok***@example.com")
        bad = self.h.add_account("bad***@example.com")
        self.h.add_credit(ok, "credit-ok", expires_in=30 * HOUR)
        self.h.add_credit(bad, "credit-bad", expires_in=30 * HOUR)
        self.assertEqual(self.scan().exit_code, 0)

        def post(account_key, credit_id, request_id):
            if account_key == bad.account_key:
                return {"code": "rate_limit_reset_ineligible"}
            return None

        self.h.backend.on_post = post
        for role in ("primary",):
            for account in (ok, bad):
                self.at_slot(account, role, "credit-ok" if account is ok else "credit-bad")
                self.run_fire(role)
        kinds = [notice["kind"] for notice in self.h.notices()]
        self.assertIn("success", kinds, "the successful credit lost its notice")
        self.assertIn("final_failure", kinds)

    def test_i6_exit_code_is_one_when_any_credit_failed(self):
        account, credit = self.armed()
        self.h.backend.on_post = lambda *_: {"code": "rate_limit_reset_ineligible"}
        self.at_slot(account, "primary")
        result = self.run_fire("primary")
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.h.row(account)["last_failure_category"], "definitive")

    def test_i6_success_is_announced_exactly_once(self):
        account, credit = self.armed()
        self.at_slot(account, "primary")
        self.run_fire("primary")
        self.at_slot(account, "backup")
        self.run_fire("backup")
        successes = [notice for notice in self.h.notices() if notice["kind"] == "success"]
        self.assertEqual(len(successes), 1)

    def test_i6_a_failed_final_is_not_repeated_by_the_other_slot(self):
        account, credit = self.armed()
        self.h.backend.on_post = lambda *_: {"code": "rate_limit_reset_ineligible"}
        self.at_slot(account, "primary")
        self.run_fire("primary")
        self.at_slot(account, "backup")
        self.run_fire("backup")
        failures = [notice for notice in self.h.notices() if notice["kind"] == "final_failure"]
        self.assertEqual(len(failures), 1)

    def test_i6_retry_pending_names_the_backup(self):
        account, credit = self.armed()

        def drop(account_key, credit_id, request_id):
            raise ConnectionError("connection reset")

        self.h.backend.on_post = drop
        self.at_slot(account, "primary")
        result = self.run_fire("primary")
        retries = [notice for notice in self.h.notices() if notice["kind"] == "retry_pending"]
        self.assertEqual(len(retries), 1)
        self.assertIn("retry_at", retries[0]["params"])
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.h.row(account)["last_failure_category"], "transient")


# ================================================================ I7 / R7 / R8


class I7RemovalBoundToExit(FlowCase):
    """I7/R7/R8: no run removes its own job; GC is completed + timeout bound."""

    def test_i7_a_fire_never_removes_its_own_job(self):
        account, credit = self.armed()
        names = self.h.job_names()
        self.assertTrue(
            any("primary" in name or name.endswith(self.h.key(account)[:8]) for name in names),
            names,
        )
        own = [name for name in names if self.h.key(account)[:8] in name]
        self.at_slot(account, "primary")
        self.run_fire("primary")
        for name in own:
            self.assertIsNotNone(self.h.job_for(name), f"{name} was removed by its own run")

    def test_i7_gc_keeps_a_finished_job_until_the_timeout_and_margin_pass(self):
        account, credit = self.armed()
        self.at_slot(account, "primary")
        self.run_fire("primary")
        name = next(name for name in self.h.job_names() if self.h.key(account)[:8] in name)
        self.h.complete_job(name)
        # Just after completion: still inside the script timeout + margin.
        self.h.advance(SCRIPT_TIMEOUT_S)
        self.scan()
        self.assertIsNotNone(self.h.job_for(name), "GC removed a job too early")
        # Past the timeout plus the margin, from the slot's fire time.
        self.h.advance(GC_MARGIN_S + MINUTE)
        self.scan()
        self.assertIsNone(self.h.job_for(name), "GC never removed the finished job")

    def test_i7_gc_never_removes_a_live_job(self):
        account, credit = self.armed(expires_in=30 * HOUR)
        name = next(name for name in self.h.job_names() if self.h.key(account)[:8] in name)
        self.h.advance(2 * HOUR)
        self.scan()
        self.assertIsNotNone(self.h.job_for(name), "GC removed a job that never ran")

    def test_i7_primary_never_removes_the_backup_job(self):
        """R8: the primary's cleanup must not cancel the backup before it starts."""
        account, credit = self.armed()
        self.at_slot(account, "primary")
        self.run_fire("primary")
        backup_names = [
            name
            for name in self.h.job_names()
            if self.h.key(account)[:8] in name and name.endswith("-b")
        ]
        self.assertTrue(backup_names, self.h.job_names())
        for name in backup_names:
            self.assertIsNotNone(self.h.job_for(name))

    def test_i7_a_scan_never_removes_a_job_that_is_still_scheduled(self):
        account, credit = self.armed()
        before = self.h.job_names()
        self.scan()
        self.assertEqual(self.h.job_names(), before)


# ================================================================== I9 / R3


class I9OnlyConsumePosts(FlowCase):
    """I9: the only POST is consume, only for a credit inside its window."""

    def test_i9_a_scan_never_posts_for_a_credit_beyond_the_horizon(self):
        account = self.h.add_account()
        self.h.add_credit(account, expires_in=60 * HOUR)
        self.scan()
        self.assertEqual(self.h.backend.posts, [])
        self.assertEqual(self.h.source.posts(), [])

    def test_i9_a_scan_never_posts_for_a_credit_it_just_scheduled(self):
        account = self.h.add_account()
        self.h.add_credit(account, expires_in=30 * HOUR)
        self.scan()
        self.assertEqual(self.h.source.posts(), [])

    def test_i9_the_late_scan_redeems_only_without_a_live_job(self):
        """A§8.2 step 4: a live owned one-shot owns the redemption."""
        account = self.h.add_account()
        self.h.add_credit(account, expires_in=30 * HOUR)
        self.scan()
        # Jump to the primary slot time with the jobs still live: the scan must not post.
        self.at_slot(account, "primary")
        self.scan()
        self.assertEqual(self.h.backend.posts, [])
        # With only the primary gone the backup still owns it: still no POST.
        key8 = self.h.key(account)[:8]
        primary = self.h.job_for(f"reset-rescue-{key8}")
        self.h.cron.remove(primary["id"], owned={primary["id"]})
        self.scan()
        self.assertEqual(self.h.backend.posts, [], "the scan raced the live backup")
        # Both jobs lost while the backup is still safely in the future: the
        # scan re-arms the backup (R9) and still does not post — the re-armed
        # one-shot owns the redemption now.
        backup = self.h.job_for(f"reset-rescue-{key8}-b")
        self.h.cron.remove(backup["id"], owned={backup["id"]})
        self.scan()
        self.assertEqual(self.h.backend.posts, [], "the scan posted over a re-armed backup")
        self.assertIsNotNone(
            self.h.job_for(f"reset-rescue-{key8}-b"), "the backup was not restored"
        )
        # Past the point where a backup can be armed, nothing owns it: the scan
        # redeems the credit itself.
        self.at_slot(account, "backup")
        for name in list(self.h.job_names()):
            job = self.h.job_for(name)
            self.h.cron.remove(job["id"], owned={job["id"]})
        result = self.scan()
        self.assertEqual(len(self.h.backend.posts), 1)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.h.row(account)["status"], "redeemed")

    def test_i9_dry_run_never_posts(self):
        account = self.h.add_account()
        self.h.add_credit(account, expires_in=30 * HOUR)
        self.scan(dry_run=True)
        self.assertEqual(self.h.source.posts(), [])
        self.assertEqual(self.h.job_names(), [], "a dry run created a job")


# ============================================================== T-17 basics


class PlannerBasics(FlowCase):
    """The slot arithmetic the invariants sit on (T-17 acceptance)."""

    def test_slots_sit_at_expiry_minus_each_lead(self):
        account = self.h.add_account()
        credit = self.h.add_credit(account, expires_in=30 * HOUR)
        self.scan()
        primary = self.h.slot(account, "primary")
        backup = self.h.slot(account, "backup")
        self.assertEqual(primary["fire_at"], credit.expires_at - 10 * MINUTE)
        self.assertEqual(backup["fire_at"], credit.expires_at - 4 * MINUTE)
        self.assertEqual(primary["job_name"], f"reset-rescue-{self.h.key(account)[:8]}")
        self.assertEqual(backup["job_name"], f"reset-rescue-{self.h.key(account)[:8]}-b")

    def test_no_slot_is_created_inside_the_minimum_lead(self):
        account = self.h.add_account()
        self.h.add_credit(account, expires_in=2 * MINUTE)
        self.scan()
        self.assertIsNone(self.h.slot(account, "primary"))
        self.assertEqual(self.h.job_names(), [])

    def test_a_credit_beyond_the_horizon_is_not_scheduled(self):
        account = self.h.add_account()
        self.h.add_credit(account, expires_in=60 * HOUR)
        result = self.scan()
        self.assertIsNone(self.h.slot(account, "primary"))
        self.assertEqual(result.exit_code, 0)

    def test_scan_is_idempotent(self):
        account, credit = self.armed()
        before = self.h.job_names()
        self.scan()
        self.assertEqual(self.h.job_names(), before)

    def test_a_lead_change_retimes_the_slots(self):
        account, credit = self.armed()
        self.h.deps.settings = self.h.settings.__class__(
            lead_minutes=20, backup_lead_minutes=6, scan_interval_hours=12
        )
        self.scan()
        primary = self.h.slot(account, "primary")
        self.assertEqual(primary["fire_at"], credit.expires_at - 20 * MINUTE)

    def test_an_excluded_account_gets_no_slots_and_loses_its_jobs(self):
        account, credit = self.armed()
        self.h.deps.settings = self.h.settings.with_included(["0" * 16])
        self.scan()
        self.assertEqual(self.h.job_names(), [])
        self.assertIsNone(self.h.slot(account, "primary"))


class MissedRule(FlowCase):
    """A§8.2 step 5: a scheduled fire that never ran is reported once."""

    def test_a_fire_that_never_ran_is_missed_once(self):
        account, credit = self.armed()
        self.h.advance_to(float(self.h.slot(account, "backup")["fire_at"]) + 16 * MINUTE)
        result = self.scan()
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(self.h.row(account)["status"], "missed")
        missed = [notice for notice in self.h.notices() if notice["kind"] == "missed"]
        self.assertEqual(len(missed), 1)
        self.scan()
        missed = [notice for notice in self.h.notices() if notice["kind"] == "missed"]
        self.assertEqual(len(missed), 1, "the missed notice was repeated")

    def test_a_missed_credit_is_reported_once_and_never_posted(self):
        """MISS_AFTER is past expiry with the shipped constants: no POST is possible."""
        account, credit = self.armed()
        self.h.advance_to(float(self.h.slot(account, "backup")["fire_at"]) + 16 * MINUTE)
        self.scan()
        self.assertEqual(self.h.row(account)["status"], "missed")
        self.assertEqual(self.h.backend.posts, [], "an expired credit was posted")
        self.scan()
        self.assertEqual(len([n for n in self.h.notices() if n["kind"] == "missed"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
