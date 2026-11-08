"""The quota job is armed by the planner's per-account rule (D-030, D-009).

Armed when at least one credit is still unsettled (seen, scheduled or redeeming),
belongs to a monitored, non-excluded account, and that account resolves to early
redemption on (its own override, else the global switch). Removed as soon as no
account meets that rule. Tests go through the production ``HostService.ensure``
path; the settings writers reconcile the same job without a scan.
"""

from __future__ import annotations

import unittest

from reset_rescue.host import QUOTA_JOB_NAME
from reset_rescue.planner import Planner
from reset_rescue.settings import Settings
from tests.host.test_t21_host_service import _NullWrappers, _service

KEY = "a" * 16
OTHER = "b" * 16
FAR_FUTURE = 1_900_000_000.0


def _credit(store, account_key: str = KEY, *, status: str = "seen", key: str = "c" * 16):
    store.credits.append(
        {
            "credit_key": key,
            "account_key": account_key,
            "status": status,
            "expires_at": FAR_FUTURE,
        }
    )


def _armed(cli) -> bool:
    return QUOTA_JOB_NAME in {job["name"] for job in cli.jobs}


class QuotaJobArmingRule(unittest.TestCase):
    def _service(self, settings: Settings):
        service, cli, store, _engine = _service(settings=settings)
        service.deps.wrappers = _NullWrappers()
        service.deps.planner = Planner(service.deps, store)
        return service, cli, store

    def test_account_override_arms_the_job_when_the_global_switch_is_off(self):
        service, cli, store = self._service(
            Settings(
                early_redeem_enabled=False,
                account_overrides={KEY: {"early_redeem_enabled": True}},
            )
        )
        _credit(store)
        service.ensure()
        self.assertTrue(_armed(cli))

    def test_global_on_with_only_far_future_seen_credits_arms_the_job(self):
        service, cli, store = self._service(Settings(early_redeem_enabled=True))
        _credit(store)
        service.ensure()
        self.assertTrue(_armed(cli))

    def test_excluded_account_does_not_arm_even_with_the_global_switch_on(self):
        service, cli, store = self._service(
            Settings(early_redeem_enabled=True, excluded_accounts=(KEY,))
        )
        _credit(store)
        service.ensure()
        self.assertFalse(_armed(cli))

    def test_excluded_scheduled_account_does_not_arm_the_job(self):
        service, cli, store = self._service(
            Settings(early_redeem_enabled=True, excluded_accounts=(KEY,))
        )
        _credit(store, status="scheduled")
        service.ensure()
        self.assertFalse(_armed(cli))

    def test_only_early_account_excluded_does_not_arm_the_job(self):
        service, cli, store = self._service(
            Settings(
                early_redeem_enabled=False,
                account_overrides={KEY: {"early_redeem_enabled": True}},
                excluded_accounts=(KEY,),
            )
        )
        _credit(store)
        _credit(store, OTHER, key="d" * 16)
        service.ensure()
        self.assertFalse(_armed(cli))

    def test_scheduled_and_redeeming_credits_also_arm_the_job(self):
        for status in ("scheduled", "redeeming"):
            with self.subTest(status=status):
                service, cli, store = self._service(Settings(early_redeem_enabled=True))
                _credit(store, status=status)
                service.ensure()
                self.assertTrue(_armed(cli))

    def test_account_with_early_off_override_does_not_arm_under_global_on(self):
        service, cli, store = self._service(
            Settings(
                early_redeem_enabled=True,
                account_overrides={KEY: {"early_redeem_enabled": False}},
            )
        )
        _credit(store)
        service.ensure()
        self.assertFalse(_armed(cli))

    def test_an_unmonitored_account_does_not_arm_the_job(self):
        service, cli, store = self._service(
            Settings(
                early_redeem_enabled=False,
                account_overrides={KEY: {"early_redeem_enabled": True, "monitored": False}},
            )
        )
        _credit(store)
        service.ensure()
        self.assertFalse(_armed(cli))

    def test_one_early_account_among_others_arms_the_job(self):
        service, cli, store = self._service(
            Settings(
                early_redeem_enabled=False,
                account_overrides={OTHER: {"early_redeem_enabled": True}},
            )
        )
        _credit(store, KEY, key="c" * 16)
        _credit(store, OTHER, key="d" * 16)
        service.ensure()
        self.assertTrue(_armed(cli))

    def test_all_early_off_removes_an_armed_job(self):
        service, cli, store = self._service(Settings(early_redeem_enabled=True))
        _credit(store)
        service.ensure()
        self.assertTrue(_armed(cli))
        service.deps.settings = Settings(
            early_redeem_enabled=False,
            account_overrides={KEY: {"early_redeem_enabled": False}},
        )
        service.ensure()
        self.assertFalse(_armed(cli))

    def test_clearing_an_account_override_removes_its_job(self):
        service, cli, store = self._service(
            Settings(
                early_redeem_enabled=False,
                account_overrides={KEY: {"early_redeem_enabled": True}},
            )
        )
        _credit(store)
        service.ensure()
        self.assertTrue(_armed(cli))
        service.deps.settings = Settings(
            early_redeem_enabled=False,
            account_overrides={KEY: {"early_redeem_enabled": None}},
        )
        service.ensure()
        self.assertFalse(_armed(cli))

    def test_no_unsettled_credit_means_not_armed(self):
        service, cli, store = self._service(Settings(early_redeem_enabled=True))
        _credit(store, status="redeemed")
        service.ensure()
        self.assertFalse(_armed(cli))

    def test_no_credit_at_all_means_not_armed(self):
        service, cli, store = self._service(Settings(early_redeem_enabled=True))
        store.credits = []
        service.ensure()
        self.assertFalse(_armed(cli))


if __name__ == "__main__":
    unittest.main()
