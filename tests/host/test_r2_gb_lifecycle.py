"""R2 fix lane GB — lifecycle honesty (R13, R6, R7, R18, quota arming).

Every case here drives the shipped ``HostService``/``Service`` over the **real**
``CronGateway`` and a CLI fake that only speaks the text the installed
``hermes cron`` prints. The fakes keep no ``argv`` key and nothing pokes the
gateway's internals, so a passing test is evidence about the production path.

The properties pinned here:

* R13 — a removal the scheduler does not confirm (its listing failed) is
  *uncertain*: the ``Removed job`` line proves nothing, ownership, credit,
  UUID and wrappers stay, and the result carries a problem for the retry.
* R6 — the public ``scan`` CLI exits non-zero, with a specific message, when
  the scan could not arm the rescue.
* R7 — ``status`` proves liveness against the live cron listing: a missing or
  terminal scan job is not armed and reports a problem code; an unreadable
  listing is unknown, never armed.
* R18 — the dead ``owned_job_*`` aliases, the stale ``create_job``/``remove_job``
  protocol and the docstring that denies the manual path are gone.
* Quota — a scan that discovers the first credit with early redemption on arms
  the quota job without a second ``ensure``.

Each class carries the reviewer's counterexample plus one more instance of the
same class, designed here.
"""

from __future__ import annotations

import io
import unittest

from reset_rescue import cli, messages
from reset_rescue.clock import CronGatewayProtocol
from reset_rescue.cron_gateway import QUOTA_JOB_NAME
from reset_rescue.fire import Fire
from reset_rescue.service import PROBLEM_CODES
from reset_rescue.store import Store
from tests.host.test_r1_fb_lifecycle import HOUR, SCAN, Harness


def _run_cli(argv: list[str], service) -> tuple[int, str, str]:
    """Run one CLI subcommand in-process and capture both streams."""
    out, err = io.StringIO(), io.StringIO()

    def write(text: str) -> None:
        out.write(text + "\n")

    def write_err(text: str) -> None:
        err.write(text + "\n")

    code = cli.handle_cli(
        cli.build_parser().parse_args(argv), service=service, out=write, err=write_err
    )
    return code, out.getvalue(), err.getvalue()


# ------------------------------------------------------------------------ R13


class UnverifiedRemovalIsUncertain(unittest.TestCase):
    """R13: the listing is the fact; the CLI's ``Removed job`` line is a claim."""

    def _armed(self) -> tuple[Harness, object]:
        h = Harness(self)
        account = h.account()
        h.credit(account, expires_in=30 * HOUR)
        h.service().ensure()
        h.scan()
        self.assertTrue(h.store.list_credits())
        self.assertTrue(h.store.list_owned_jobs(active_only=True))
        return h, account

    def test_a_remove_the_cli_claims_while_the_listing_fails_is_not_cleanup(self):
        """The reviewer's counterexample: sticky remove + unreadable listing."""
        h, account = self._armed()
        key = h.key(account)
        h.store.begin_redeem(key, h.clock.now())
        request_id = h.store.get_credit(key)["redeem_request_id"]
        self.assertTrue(request_id)
        h.cron_cli.sticky_remove = True
        h.cron_cli.fail_list = True

        dto = h.service().uninstall()

        self.assertTrue(dto.problems, "an unverified absence was accepted as cleanup")
        self.assertTrue(h.store.list_owned_jobs(active_only=True), "ownership was forgotten")
        self.assertEqual(h.store.get_credit(key)["redeem_request_id"], request_id)
        self.assertTrue(h.cron_cli.jobs, "live jobs were reported as removed")
        self.assertTrue(h.wrappers.states, "wrappers were deleted under live jobs")

    def test_a_truthful_remove_the_listing_cannot_confirm_is_still_uncertain(self):
        """Variant: the job really went away, but nothing proved it.

        Keeping the row costs one idempotent retry; forgetting it would erase
        the credit and its UUID on the CLI's word alone.
        """
        h, account = self._armed()
        key = h.key(account)
        h.store.begin_redeem(key, h.clock.now())
        request_id = h.store.get_credit(key)["redeem_request_id"]
        h.cron_cli.fail_list = True

        dto = h.service().uninstall()

        self.assertTrue(dto.problems, "an unconfirmed removal was reported as done")
        self.assertTrue(h.store.list_owned_jobs(active_only=True))
        self.assertEqual(h.store.get_credit(key)["redeem_request_id"], request_id)
        self.assertTrue(h.wrappers.states)

        # The retry, once the scheduler can be read again, finishes the job.
        h.cron_cli.fail_list = False
        again = h.service().uninstall()

        self.assertEqual(again.problems, [])
        self.assertEqual(h.store.list_owned_jobs(active_only=True), [])
        self.assertEqual(h.cron_cli.jobs, [])
        self.assertEqual(h.store.list_credits(), [])


# ------------------------------------------------------------------------- R6


class ScanCliReportsSchedulingFailure(unittest.TestCase):
    """R6: a scan that armed nothing must not exit 0."""

    def test_public_scan_exits_nonzero_when_every_create_fails(self):
        """The reviewer's counterexample: all ``cron create`` calls fail."""
        h = Harness(self)
        account = h.account()
        h.credit(account, expires_in=HOUR)
        h.cron_cli.fail_create = True

        code, out, err = _run_cli(["scan"], h.service())

        self.assertNotEqual(code, 0, "the public scan reported success with no job")
        self.assertIn(messages.reason_text("cron_unavailable", "en"), err)
        self.assertNotIn("Traceback", err)

    def test_public_scan_exits_nonzero_when_only_the_primary_fails(self):
        """Variant: one of the two rescue one-shots was refused.

        A backup alone is a second chance, not a schedule: the scan still did
        not do what it says it did.
        """
        h = Harness(self)
        account = h.account()
        h.credit(account, expires_in=30 * HOUR)
        h.cron_cli.fail_create_scripts = {"reset-rescue-fire.py"}

        code, out, err = _run_cli(["scan"], h.service())

        self.assertNotEqual(code, 0, "a refused primary one-shot was reported as scheduled")
        self.assertIn(messages.reason_text("cron_unavailable", "en"), err)

    def test_a_healthy_scan_still_exits_zero(self):
        """Control: the new failure rule must not make every scan fail."""
        h = Harness(self)
        account = h.account()
        h.credit(account, expires_in=30 * HOUR)

        code, out, err = _run_cli(["scan"], h.service())

        self.assertEqual(code, 0, err)
        self.assertEqual(err.strip(), "")


# ------------------------------------------------------------------------- R7


class StatusProvesLiveness(unittest.TestCase):
    """R7: a recorded row is not a live job; status asks the scheduler."""

    def test_status_is_not_armed_when_the_scan_job_vanished(self):
        """The reviewer's counterexample: the row survives, the job does not."""
        h = Harness(self)
        service = h.service()
        service.ensure()
        h.cron_cli.jobs.clear()

        dto = service.status()

        self.assertFalse(dto.schedule.armed, "a missing scan job was reported as armed")
        self.assertIn(dto.schedule.problem_code, PROBLEM_CODES)
        self.assertIsNotNone(dto.schedule.problem_code)

    def test_status_is_not_armed_when_the_scan_job_is_terminal(self):
        """The reviewer's counterexample: a completed job will not fire again."""
        h = Harness(self)
        service = h.service()
        service.ensure()
        h.cron_cli.finish(SCAN)

        dto = service.status()

        self.assertFalse(dto.schedule.armed, "a terminal scan job was reported as armed")
        self.assertIn(dto.schedule.problem_code, PROBLEM_CODES)

    def test_status_is_unknown_never_armed_when_the_listing_fails(self):
        """Variant: no listing, no proof. Unknown is not armed."""
        h = Harness(self)
        service = h.service()
        service.ensure()
        h.cron_cli.fail_list = True

        dto = service.status()

        self.assertFalse(dto.schedule.armed, "an unreadable listing was read as armed")
        self.assertIn(dto.schedule.problem_code, PROBLEM_CODES)

    def test_status_is_armed_only_with_a_live_listed_job(self):
        """Control: the honest case still says armed."""
        h = Harness(self)
        service = h.service()
        service.ensure()

        dto = service.status()

        self.assertTrue(dto.schedule.armed)
        self.assertIsNone(dto.schedule.problem_code)
        self.assertEqual(dto.schedule.scan_job.job_id, h.cron_cli.jobs[0]["id"])


# ------------------------------------------------------------------------ R18


class DeadContractIsGone(unittest.TestCase):
    """R18: one store dialect, one cron protocol, one honest docstring."""

    def test_the_owned_job_aliases_are_gone(self):
        for name in ("owned_job_add", "owned_jobs_list", "owned_job_remove"):
            self.assertFalse(hasattr(Store, name), f"{name} is a second dialect for one store")

    def test_the_cron_protocol_has_no_argv_level_create_or_remove(self):
        stale = [
            name for name in ("create_job", "remove_job") if hasattr(CronGatewayProtocol, name)
        ]
        self.assertEqual(stale, [], "the protocol still describes the abandoned argv API")

    def test_the_fire_docstring_does_not_deny_the_manual_path(self):
        doc = (Fire.__doc__ or "").lower()
        self.assertNotIn("no manual", doc)
        self.assertIn("redeem_now", doc)


# ---------------------------------------------------------------------- quota


class ScanArmsTheQuotaJob(unittest.TestCase):
    """The quota job follows the credits, not the next manual ``ensure``."""

    def test_the_scan_that_discovers_the_first_credit_arms_the_quota_job(self):
        """The reviewer's counterexample: ensure ran before any credit existed."""
        h = Harness(self, early_redeem_enabled=True)
        service = h.service()
        service.ensure()
        self.assertNotIn(QUOTA_JOB_NAME, h.cron_cli.names())

        account = h.account()
        h.credit(account, expires_in=30 * HOUR)
        service.scan()

        self.assertIn(QUOTA_JOB_NAME, h.cron_cli.names())

    def test_a_later_scan_disarms_the_quota_job_when_the_setting_turns_off(self):
        """Variant: the same scan seam removes it, with no second ``ensure``."""
        h = Harness(self, early_redeem_enabled=True)
        service = h.service()
        service.ensure()
        account = h.account()
        h.credit(account, expires_in=30 * HOUR)
        service.scan()
        self.assertIn(QUOTA_JOB_NAME, h.cron_cli.names())

        import dataclasses

        h.deps.settings = dataclasses.replace(h.settings, early_redeem_enabled=False)
        service.scan()

        self.assertNotIn(QUOTA_JOB_NAME, h.cron_cli.names())

    def test_a_dry_run_scan_never_arms_a_job(self):
        """Control: a dry run mutates nothing, so it arms nothing."""
        h = Harness(self, early_redeem_enabled=True)
        service = h.service()
        account = h.account()
        h.credit(account, expires_in=30 * HOUR)

        service.scan(dry_run=True)

        self.assertNotIn(QUOTA_JOB_NAME, h.cron_cli.names())


if __name__ == "__main__":
    unittest.main()
