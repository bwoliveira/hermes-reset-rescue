"""T-23b: redeem-now, the rehearsa hook point, and uninstall.

`redeem_now` refuses without `confirm` and in a dry run; it uses the persisted
`redeem_request_id` (I2). `uninstall` removes only owned jobs and only our
wrappers (I7/I10).
"""

from __future__ import annotations

import unittest

from reset_rescue.dto import RedeemResultDTO
from reset_rescue.service import (
    OWNED_JOB_KINDS,
    REFUSE_CONFIRM,
    REFUSE_DRY_RUN,
    REFUSE_INVALID_KEY,
    REFUSE_UNKNOWN_CREDIT,
    Service,
    refuse_reason,
)
from tests.fakes import FakeCron, FakeStore, FakeWrappers, make_deps, make_settings

KEY_A = "a" * 16
CREDIT = "c" * 16


def service_with(**kwargs) -> tuple:
    store = kwargs.pop("store", None) or FakeStore()
    cron = kwargs.pop("cron", None) or FakeCron()
    wrappers = kwargs.pop("wrappers", None) or FakeWrappers()
    deps = make_deps(store=store, cron=cron, wrappers=wrappers, **kwargs)
    return Service(deps), deps, store, cron, wrappers


def seed(
    store: FakeStore, *, status: str = "scheduled", backend: str = "available", **over
) -> None:
    store.account_upsert({"account_key": KEY_A, "label": "b***@e***.com", "source_kind": "pool"})
    row = {
        "credit_key": CREDIT,
        "account_key": KEY_A,
        "title": "Full reset",
        "reset_type": "codex_rate_limits",
        "expires_at": 1_800_003_600.0,
        "backend_state": backend,
        "status": status,
        "updated_at": 1_800_000_000.0,
        "redeem_request_id": "uuid-persisted",
    }
    row.update(over)
    store.credit_upsert(row)


class FakeFire:
    """Records what the service asked for; returns a scripted outcome."""

    def __init__(self, outcome=None) -> None:
        self.calls: list[dict] = []
        self.outcome = outcome or {
            "outcome": "redeemed",
            "reason_code": "reset",
            "windows_reset": 2,
        }

    def redeem_one(self, credit_key: str, *, role: str = "manual", confirm: bool = False):
        self.calls.append({"credit_key": credit_key, "role": role, "confirm": confirm})
        return self.outcome


class RefusalTable(unittest.TestCase):
    """The pure guard, so the HTTP layer can answer 422 without a flow."""

    def test_confirm_must_be_literally_true(self):
        settings = make_settings()
        for value in (None, False, "true", 1, "yes", [], {}):
            self.assertEqual(
                refuse_reason(settings, confirm=value, credit_key=CREDIT), REFUSE_CONFIRM, value
            )

    def test_true_passes(self):
        self.assertIsNone(refuse_reason(make_settings(), confirm=True, credit_key=CREDIT))

    def test_a_dry_run_refuses_even_with_confirm(self):
        settings = make_settings(dry_run=True)
        self.assertEqual(refuse_reason(settings, confirm=True, credit_key=CREDIT), REFUSE_DRY_RUN)

    def test_an_invalid_key_is_refused_first(self):
        settings = make_settings(dry_run=True)
        for key in ("", "short", "Z" * 16, "rehearsal-" + "a" * 16, None):
            self.assertEqual(
                refuse_reason(settings, confirm=True, credit_key=key), REFUSE_INVALID_KEY, key
            )


class RedeemNowGuards(unittest.TestCase):
    def test_without_confirm_nothing_is_called(self):
        fire = FakeFire()
        service, deps, store, _, _ = service_with()
        deps.fire = fire
        seed(store)
        result = service.redeem_now(CREDIT, confirm=None)
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(result.reason_code, REFUSE_CONFIRM)
        self.assertEqual(fire.calls, [])

    def test_a_falsy_string_confirm_is_refused(self):
        fire = FakeFire()
        service, deps, store, _, _ = service_with()
        deps.fire = fire
        seed(store)
        result = service.redeem_now(CREDIT, confirm="yes")
        self.assertEqual(result.reason_code, REFUSE_CONFIRM)
        self.assertEqual(fire.calls, [])

    def test_a_dry_run_refuses_and_reports_dry_run(self):
        fire = FakeFire()
        service, deps, store, _, _ = service_with(settings=make_settings(dry_run=True))
        deps.fire = fire
        seed(store)
        result = service.redeem_now(CREDIT, confirm=True)
        self.assertEqual(result.outcome, "dry_run")
        self.assertEqual(result.reason_code, REFUSE_DRY_RUN)
        self.assertEqual(fire.calls, [])

    def test_an_unknown_credit_is_refused(self):
        fire = FakeFire()
        service, deps, store, _, _ = service_with()
        deps.fire = fire
        result = service.redeem_now("f" * 16, confirm=True)
        self.assertEqual(result.reason_code, REFUSE_UNKNOWN_CREDIT)
        self.assertEqual(fire.calls, [])

    def test_an_invalid_key_is_refused_before_the_store(self):
        service, deps, store, _, _ = service_with()
        deps.fire = FakeFire()
        result = service.redeem_now("nope", confirm=True)
        self.assertEqual(result.reason_code, REFUSE_INVALID_KEY)

    def test_a_missing_fire_is_not_implemented(self):
        service, _, store, _, _ = service_with()
        seed(store)
        result = service.redeem_now(CREDIT, confirm=True)
        self.assertEqual(result.reason_code, "not_implemented")


class RedeemNowFlow(unittest.TestCase):
    def test_a_confirmed_redeem_calls_the_fire_with_role_manual(self):
        fire = FakeFire()
        service, deps, store, _, _ = service_with()
        deps.fire = fire
        seed(store)
        result = service.redeem_now(CREDIT, confirm=True)
        self.assertEqual(fire.calls, [{"credit_key": CREDIT, "role": "manual", "confirm": True}])
        self.assertEqual(result.outcome, "redeemed")
        self.assertEqual(result.windows_reset, 2)

    def test_the_result_is_a_dto_that_serializes(self):
        service, deps, store, _, _ = service_with()
        deps.fire = FakeFire()
        seed(store)
        result = service.redeem_now(CREDIT, confirm=True)
        self.assertIsInstance(result, RedeemResultDTO)
        self.assertEqual(
            set(result.to_dict()), {"credit_key", "outcome", "reason_code", "windows_reset"}
        )

    def test_the_persisted_uuid_is_what_the_fire_sees(self):
        """I2: the row's `redeem_request_id` is reused, never regenerated."""
        seen = {}

        class Fire:
            def redeem_one(self, credit_key, *, role="manual", confirm=False):
                seen["uuid"] = store.credit_get(credit_key)["redeem_request_id"]
                return {"outcome": "redeemed", "reason_code": "reset", "windows_reset": 1}

        service, deps, store, _, _ = service_with()
        deps.fire = Fire()
        seed(store, redeem_request_id="uuid-from-an-earlier-run")
        service.redeem_now(CREDIT, confirm=True)
        self.assertEqual(seen["uuid"], "uuid-from-an-earlier-run")

    def test_a_fire_exception_becomes_a_fixed_code(self):
        class Fire:
            def redeem_one(self, credit_key, *, role="manual", confirm=False):
                raise RuntimeError("provider said something secret")

        service, deps, store, _, _ = service_with()
        deps.fire = Fire()
        seed(store)
        result = service.redeem_now(CREDIT, confirm=True)
        self.assertEqual(result.outcome, "failed")
        self.assertEqual(result.reason_code, "internal_error")
        self.assertNotIn("secret", result.to_dict().__repr__())

    def test_a_mapping_outcome_is_accepted(self):
        service, deps, store, _, _ = service_with()
        deps.fire = FakeFire(
            {"outcome": "unconfirmed", "reason_code": "transient", "windows_reset": None}
        )
        seed(store)
        result = service.redeem_now(CREDIT, confirm=True)
        self.assertEqual(result.outcome, "unconfirmed")

    def test_an_object_outcome_is_accepted(self):
        class Outcome:
            outcome = "already_redeemed"
            reason_code = "already_redeemed"
            windows_reset = 0

        service, deps, store, _, _ = service_with()
        deps.fire = FakeFire(Outcome())
        seed(store)
        self.assertEqual(service.redeem_now(CREDIT, confirm=True).outcome, "already_redeemed")

    def test_redeem_now_on_a_settled_credit_still_reaches_the_fire(self):
        """The fire re-reads the backend; the local row is not the authority (I1)."""
        fire = FakeFire(
            {"outcome": "already_redeemed", "reason_code": "already_redeemed", "windows_reset": 0}
        )
        service, deps, store, _, _ = service_with()
        deps.fire = fire
        seed(store, status="redeemed", backend="redeemed")
        result = service.redeem_now(CREDIT, confirm=True)
        self.assertEqual(result.outcome, "already_redeemed")


class Uninstall(unittest.TestCase):
    def _seeded(self):
        service, deps, store, cron, wrappers = service_with()
        deps.settings = make_settings(automatic_rescue=True)
        service.ensure()
        return service, deps, store, cron, wrappers

    def test_uninstall_removes_owned_jobs_and_wrappers(self):
        service, deps, store, cron, wrappers = self._seeded()
        self.assertEqual(len(cron.jobs), 1)
        result = service.uninstall()
        self.assertEqual(cron.jobs, {})
        self.assertEqual(wrappers.states, {})
        self.assertIn("reset-rescue-scan", result.removed)
        self.assertFalse(result.armed)

    def test_uninstall_never_removes_a_job_it_does_not_own(self):
        service, deps, store, cron, _ = self._seeded()
        cron.jobs["foreign-1"] = {
            "id": "foreign-1",
            "name": "someone-elses-job",
            "state": "scheduled",
            "enabled": True,
            "schedule": "every 1h",
            "script": "theirs.py",
            "deliver": "local",
        }
        service.uninstall()
        self.assertIn("foreign-1", cron.jobs)
        self.assertEqual(cron.names(), ["someone-elses-job"])

    def test_uninstall_never_removes_a_foreign_wrapper(self):
        service, deps, store, cron, wrappers = self._seeded()
        wrappers.states["someone_elses_script.py"] = "ok"
        service.uninstall()
        self.assertEqual(wrappers.states, {"someone_elses_script.py": "ok"})

    def test_keep_data_preserves_the_store(self):
        service, deps, store, _, _ = self._seeded()
        store.meta_set("salt", "a" * 64)
        service.uninstall(keep_data=True)
        self.assertEqual(store.cleared, 0)
        self.assertEqual(store.meta_get("salt"), "a" * 64)

    def test_without_keep_data_the_store_is_cleared(self):
        service, deps, store, _, _ = self._seeded()
        service.uninstall(keep_data=False)
        self.assertEqual(store.cleared, 1)

    def test_uninstall_is_idempotent(self):
        service, deps, store, cron, wrappers = self._seeded()
        service.uninstall()
        second = service.uninstall()
        self.assertEqual(second.removed, [])
        self.assertEqual(second.problems, [])

    def test_only_known_kinds_are_ever_removed(self):
        service, deps, store, cron, _ = self._seeded()
        # A recorded row with an unknown kind, backed by a real cron job, must
        # be ignored: an unrecognized kind is not ours to remove (I10).
        cron.jobs["job-9999"] = {
            "id": "job-9999",
            "name": "reset-rescue-weird",
            "enabled": True,
            "schedule": "every 1h",
            "argv": [],
        }
        store.record_job("job-9999", "reset-rescue-weird", "other")
        service.uninstall()
        self.assertEqual(
            OWNED_JOB_KINDS, {"scan", "primary", "backup", "reminder1", "reminder2", "quota"}
        )
        self.assertIn("job-9999", [job["id"] for job in cron.list_jobs()])

    def test_a_failing_wrapper_removal_is_a_problem_code(self):
        class BrokenWrappers(FakeWrappers):
            def remove_owned(self):
                raise OSError("permission denied")

        service, deps, store, cron, _ = service_with(wrappers=BrokenWrappers())
        result = service.uninstall()
        self.assertIn("wrappers_unavailable", result.problems)

    def test_ensure_after_uninstall_restores_everything(self):
        service, deps, store, cron, wrappers = self._seeded()
        service.uninstall(keep_data=True)
        result = service.ensure()
        self.assertTrue(result.armed)
        self.assertEqual(len(cron.jobs), 1)
        self.assertTrue(wrappers.states)


class RehearseHookPoint(unittest.TestCase):
    """A§8.5: the service exposes the hook; L2 owns the flow."""

    def test_rehearse_is_absent_until_the_flow_lands(self):
        service, deps, store, _, _ = service_with()
        seed(store)
        # No `rehearse` attribute yet: the HTTP layer must answer 501, not 500.
        self.assertFalse(hasattr(service, "rehearse"))


if __name__ == "__main__":
    unittest.main()
