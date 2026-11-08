"""T-23a/T-71: service status, ensure, scan, exclusion, history, quota-check.

`ensure` is idempotent and never disarms (D-004: rescue is always on while the
plugin is enabled). ``status`` is banked-only (D-010) and carries the compact
quota summary (D-017). ``quota_check`` is the one-method passthrough to L2's
flow that L3's CLI calls. ``build_service`` is the factory L4's route imports.
"""

from __future__ import annotations

import unittest
from unittest import mock

from reset_rescue import host as host_module
from reset_rescue.accounts import AccountRef, Response
from reset_rescue.dto import QuotaCheckDTO, StatusDTO
from reset_rescue.service import PROBLEM_CODES, Service, build_service, resolve_language
from reset_rescue.settings import parse_settings
from tests.fakes import (
    FakeConfig,
    FakeCron,
    FakeSource,
    FakeStore,
    FakeWrappers,
    make_deps,
    make_settings,
    usage_payload,
)

KEY_A = "a" * 16
KEY_B = "b" * 16
CREDIT = "c" * 16


def service_with(**kwargs) -> tuple:
    store = kwargs.pop("store", None) or FakeStore()
    cron = kwargs.pop("cron", None) or FakeCron()
    wrappers = kwargs.pop("wrappers", None) or FakeWrappers()
    config = kwargs.pop("config", None) or FakeConfig()
    deps = make_deps(store=store, cron=cron, wrappers=wrappers, config=config, **kwargs)
    return Service(deps), deps, store, cron, wrappers, config


def seed_account(store: FakeStore, key: str = KEY_A, label: str = "b***@e***.com") -> None:
    store.account_upsert(
        {
            "account_key": key,
            "label": label,
            "source_kind": "pool",
            "last_list_ok_at": 1_800_000_000.0,
            "last_error_code": None,
        }
    )


def seed_credit(store: FakeStore, key: str = CREDIT, account_key: str = KEY_A, **over) -> None:
    row = {
        "credit_key": key,
        "account_key": account_key,
        "title": "Full reset",
        "reset_type": "codex_rate_limits",
        "granted_at": 1_799_000_000.0,
        "expires_at": 1_800_003_600.0,
        "backend_state": "available",
        "status": "scheduled",
        "updated_at": 1_800_000_000.0,
    }
    row.update(over)
    store.credit_upsert(row)


def seeded() -> tuple:
    """A service with one banked account and one credit, plus a live source."""
    source = FakeSource(
        [AccountRef(account_key=KEY_A, label="b***@e***.com", source_kind="pool", entry_id="e1")]
    )
    source.on("GET", "/usage", Response(200, usage_payload(weekly=60.0, session=12.0)))
    service, deps, store, cron, wrappers, config = service_with(source=source)
    seed_account(store)
    seed_credit(store)
    return service, deps, store, cron, wrappers, config


class StatusShape(unittest.TestCase):
    def test_status_is_a_status_dto_that_serializes(self):
        service, _, _, _, _, _ = service_with()
        status = service.status()
        self.assertIsInstance(status, StatusDTO)
        payload = status.to_dict()
        self.assertEqual(payload["version"], "0.1.0")
        self.assertEqual(payload["source_kind"], "pool")
        self.assertIn("attention", payload)

    def test_status_never_raises_with_no_collaborators(self):
        from reset_rescue.clock import Deps

        status = Service(Deps()).status()
        self.assertIsInstance(status, StatusDTO)
        self.assertEqual(status.accounts, [])

    def test_status_reports_an_empty_state_as_unarmed(self):
        service, _, _, _, _, _ = service_with()
        status = service.status()
        self.assertFalse(status.schedule.armed)
        self.assertIsNone(status.schedule.scan_job)

    def test_status_lists_accounts_and_credits(self):
        service, _, store, _, _, _ = service_with()
        seed_account(store)
        seed_credit(store)
        status = service.status()
        self.assertEqual(len(status.accounts), 1)
        account = status.accounts[0]
        self.assertEqual(account.account_key, KEY_A)
        self.assertTrue(account.included)
        self.assertEqual(len(account.credits), 1)
        self.assertEqual(account.credits[0].state, "available")
        self.assertTrue(account.credits[0].can_redeem_now)

    def test_an_account_with_no_banked_reset_is_listed_with_no_credits(self):
        """D-027: every pool account is listed; one with no banked reset has none."""
        service, _, store, _, _, _ = service_with()
        seed_account(store, KEY_A)
        seed_account(store, KEY_B)
        seed_credit(store, CREDIT, KEY_A)
        by_key = {account.account_key: account for account in service.status().accounts}
        self.assertEqual(sorted(by_key), sorted([KEY_A, KEY_B]))
        self.assertEqual(by_key[KEY_B].credits, [])

    def test_an_account_whose_only_credit_settled_is_listed_without_banked_credits(self):
        """D-027: a settled credit does not hide the account; it has no banked credit."""
        service, _, store, _, _, _ = service_with()
        seed_account(store)
        seed_credit(store, status="redeemed", backend_state="redeemed")
        accounts = service.status().accounts
        self.assertEqual(len(accounts), 1)
        self.assertNotIn("available", [credit.state for credit in accounts[0].credits])

    def test_credits_are_sorted_soonest_expiry_first(self):
        service, _, store, _, _, _ = service_with()
        seed_account(store)
        seed_credit(store, "1" * 16, expires_at=1_800_009_000.0)
        seed_credit(store, "2" * 16, expires_at=1_800_001_000.0)
        credits = service.status().accounts[0].credits
        self.assertEqual([credit.credit_key for credit in credits], ["2" * 16, "1" * 16])

    def test_a_settled_credit_cannot_be_redeemed_now(self):
        service, _, store, _, _, _ = service_with()
        seed_account(store)
        seed_credit(store, status="redeemed", backend_state="redeemed")
        seed_credit(store, "d" * 16)
        credits = {credit.credit_key: credit for credit in service.status().accounts[0].credits}
        self.assertEqual(credits[CREDIT].state, "redeemed")
        self.assertFalse(credits[CREDIT].can_redeem_now)

    def test_a_pending_credit_can_still_be_redeemed_now(self):
        service, _, store, _, _, _ = service_with()
        seed_account(store)
        seed_credit(store, status="redeeming", backend_state="pending")
        credit = service.status().accounts[0].credits[0]
        self.assertEqual(credit.state, "pending")
        self.assertTrue(credit.can_redeem_now)

    def test_slots_are_reported_when_the_store_has_them(self):
        service, _, store, _, _, _ = service_with()
        seed_account(store)
        seed_credit(store)
        store.slot_upsert(CREDIT, "primary", {"fire_at": 1_800_003_000.0, "state": "armed"})
        store.slot_upsert(CREDIT, "backup", {"fire_at": 1_800_003_360.0, "state": "planned"})
        credit = service.status().accounts[0].credits[0]
        self.assertEqual(credit.primary.job_state, "armed")
        self.assertEqual(credit.primary.role, "primary")
        self.assertEqual(credit.backup.job_state, "planned")
        self.assertEqual(credit.plan, "scheduled")

    def test_attention_counts_failed_and_pending(self):
        service, _, store, _, _, _ = service_with()
        seed_account(store, KEY_A)
        seed_account(store, KEY_B)
        store.accounts[KEY_B]["last_error_code"] = "auth_expired"
        seed_credit(store, "1" * 16, KEY_A, backend_state="available")
        seed_credit(store, "2" * 16, KEY_B, backend_state="pending", status="redeeming")
        attention = service.status().attention
        self.assertEqual(attention.failed, 1)
        self.assertEqual(attention.pending, 2)

    def test_the_notify_target_is_masked_in_the_dto(self):
        service, _, _, _, _, _ = service_with(
            settings=make_settings(notify_target="telegram:123456789:42")
        )
        payload = service.status().to_dict()
        self.assertNotIn("123456789", str(payload))
        self.assertIn("…6789", payload["settings"]["notify_target_display"])

    def test_settings_warnings_are_surfaced(self):
        service, _, _, _, _, _ = service_with(settings=parse_settings({"scan_interval_hours": 0}))
        self.assertIn("settings_scan_interval_invalid", service.status().settings_warnings)

    def test_a_broken_store_is_a_problem_code_not_an_exception(self):
        class BrokenStore(FakeStore):
            def account_list(self):
                raise OSError("db gone")

        service, _, _, _, _, _ = service_with(store=BrokenStore())
        status = service.status()
        self.assertEqual(status.schedule.problem_code, "state_unreadable")
        self.assertIn(status.schedule.problem_code, PROBLEM_CODES)

    def test_status_does_not_take_a_lock_or_mutate(self):
        service, deps, store, cron, _, _ = service_with()
        seed_account(store)
        before = (dict(store.meta), list(cron.created))
        service.status()
        self.assertEqual((dict(store.meta), list(cron.created)), before)

    def test_status_reports_the_derived_backup_lead(self):
        service, _, _, _, _, _ = service_with(settings=parse_settings({"lead_minutes": 20}))
        payload = service.status().to_dict()
        self.assertEqual(payload["settings"]["backup_lead_minutes"], 4)

    def test_status_reports_the_exclusion_count_not_the_list(self):
        service, _, _, _, _, _ = service_with(
            settings=parse_settings({"excluded_accounts": f"{KEY_A},{KEY_B}"})
        )
        payload = service.status().to_dict()
        self.assertEqual(payload["settings"]["excluded_accounts_count"], 2)
        self.assertNotIn(KEY_A, str(payload["settings"]))


class Limits(unittest.TestCase):
    """D-017: the compact summary rides on the accounts that are listed."""

    def test_the_summary_is_read_for_a_banked_account(self):
        service, deps, store, _, _, _ = seeded()
        quota = service.status().accounts[0].quota
        self.assertIsNone(quota.plan_type)
        self.assertEqual(quota.five_hour_remaining_pct, 88)
        self.assertEqual(quota.weekly_remaining_pct, 40)

    def test_the_plan_name_is_carried(self):
        source = FakeSource(
            [
                AccountRef(
                    account_key=KEY_A, label="b***@e***.com", source_kind="pool", entry_id="e1"
                )
            ]
        )
        payload = usage_payload(weekly=10.0)
        payload["plan_type"] = "pro_200"
        source.on("GET", "/usage", Response(200, payload))
        service, _, store, _, _, _ = service_with(source=source)
        seed_account(store)
        seed_credit(store)
        self.assertEqual(service.status().accounts[0].quota.plan_type, "Pro 200")

    def test_no_usage_read_for_an_account_that_is_not_listed(self):
        source = FakeSource([])
        service, _, store, _, _, _ = service_with(source=source)
        seed_account(store)  # no credit: not in the payload
        service.status()
        self.assertEqual(source.count("GET", "/usage"), 0)

    def test_an_unreadable_summary_leaves_the_account_listed(self):
        source = FakeSource(
            [
                AccountRef(
                    account_key=KEY_A, label="b***@e***.com", source_kind="pool", entry_id="e1"
                )
            ]
        )
        source.on("GET", "/usage", Response(500))
        service, _, store, _, _, _ = service_with(source=source)
        seed_account(store)
        seed_credit(store)
        account = service.status().accounts[0]
        self.assertIsNone(account.quota)
        self.assertEqual(len(account.credits), 1)

    def test_a_missing_api_leaves_the_summary_absent(self):
        service, deps, store, _, _, _ = service_with()
        deps.api = None
        seed_account(store)
        seed_credit(store)
        self.assertIsNone(service.status().accounts[0].quota)


class Labels(unittest.TestCase):
    """D-013: masking is a setting, on by default."""

    def test_the_stored_label_is_masked_by_default(self):
        service, _, store, _, _, _ = service_with()
        seed_account(store, KEY_A, "b***@e***.com")
        seed_credit(store)
        self.assertEqual(service.status().accounts[0].label, "b***@e***.com")

    def test_masking_off_uses_the_source_full_label(self):
        source = FakeSource(
            [
                AccountRef(
                    account_key=KEY_A, label="bob@example.com", source_kind="pool", entry_id="e1"
                )
            ]
        )
        source.full_label = lambda key: "bob@example.com" if key == KEY_A else ""
        service, _, store, _, _, _ = service_with(
            source=source, settings=parse_settings({"mask_labels": False})
        )
        seed_account(store, KEY_A, "b***@e***.com")
        seed_credit(store)
        self.assertEqual(service.status().accounts[0].label, "bob@example.com")

    def test_masking_off_without_a_source_label_keeps_the_stored_one(self):
        service, _, store, _, _, _ = service_with(settings=parse_settings({"mask_labels": False}))
        seed_account(store, KEY_A, "b***@e***.com")
        seed_credit(store)
        self.assertEqual(service.status().accounts[0].label, "b***@e***.com")

    def test_a_full_label_in_the_store_is_masked_again_by_default(self):
        """D-013's default must hold even for a row an older build wrote.

        The store keeps masked labels, but nothing in SQLite enforces that: a
        row written before masking existed (or by hand) would otherwise reach
        the popup and the HTTP body verbatim. ``mask_labels`` on re-masks what
        is stored, and masking is idempotent, so the normal path is unchanged.
        """
        service, _, store, _, _, _ = service_with()
        seed_account(store, KEY_A, "bea.doe@example.com")
        seed_credit(store)
        payload = service.status().to_json()
        self.assertNotIn("bea.doe@", payload)
        self.assertIn("b***@e***.com", payload)

    def test_a_full_label_in_the_store_survives_when_masking_is_off(self):
        service, _, store, _, _, _ = service_with(settings=parse_settings({"mask_labels": False}))
        seed_account(store, KEY_A, "bea.doe@example.com")
        seed_credit(store)
        self.assertEqual(service.status().accounts[0].label, "bea.doe@example.com")


class Language(unittest.TestCase):
    """D-016: ``auto`` follows Hermes, ``en`` is the fallback."""

    def test_an_explicit_setting_wins(self):
        self.assertEqual(resolve_language(parse_settings({"language": "pt-BR"}), "en"), "pt-BR")
        self.assertEqual(resolve_language(parse_settings({"language": "en"}), "pt-BR"), "en")

    def test_auto_follows_the_host(self):
        self.assertEqual(resolve_language(parse_settings({"language": "auto"}), "pt-BR"), "pt-BR")
        self.assertEqual(resolve_language(parse_settings({"language": "auto"}), "pt"), "pt-BR")

    def test_auto_falls_back_to_english(self):
        for host in ("", "fr", "de-DE", None):
            self.assertEqual(resolve_language(parse_settings({"language": "auto"}), host), "en")

    def test_status_reports_the_resolved_language(self):
        service, _, _, _, _, _ = service_with(settings=parse_settings({"language": "pt-BR"}))
        self.assertEqual(service.status().language, "pt-BR")


class StatusLanguageIsTheSetting(unittest.TestCase):
    """D-016: ``auto`` must reach the Desktop as ``auto``.

    The Desktop is the only place its own locale is known. Emitting the
    *resolved* language made a pt-BR Desktop with Hermes' ``display.language``
    unset receive ``"en"`` and render English. The DTO therefore carries the
    setting (``auto`` / ``en`` / ``pt-BR``) and the Desktop resolves ``auto``
    itself; notices keep resolving ``auto`` against Hermes, because a cron run
    has no Desktop.
    """

    def _status_language(self, setting: str, display_language: str) -> str:
        """Build a real status with Hermes' ``display.language`` pinned."""
        service, _, _, _, _, _ = service_with(settings=parse_settings({"language": setting}))
        config = {"display": {"language": display_language}} if display_language else {}
        with mock.patch("hermes_cli.config.load_config_readonly", return_value=config):
            return service.status().language

    def test_auto_stays_auto_whatever_hermes_says(self):
        for host in ("pt-BR", "en", "", "de"):
            self.assertEqual(self._status_language("auto", host), "auto", host)

    def test_an_explicit_setting_is_sent_as_is(self):
        self.assertEqual(self._status_language("pt-BR", "en"), "pt-BR")
        self.assertEqual(self._status_language("en", "pt-BR"), "en")

    def test_the_notice_language_still_follows_hermes(self):
        with mock.patch("reset_rescue.host._host_language", return_value="pt-BR"):
            with mock.patch(
                "reset_rescue.host.read_plugin_settings", return_value={"language": "auto"}
            ):
                self.assertEqual(host_module.plugin_language(), "pt-BR")


class StatusAccountCount(unittest.TestCase):
    """The empty state's "scanning M accounts" needs every scanned account.

    ``accounts`` is banked-only (D-010), so the popup cannot count the pool
    itself. ``account_count`` is the number of accounts the next scan covers:
    stored accounts that are not excluded, whether or not they hold a reset.
    """

    def test_it_counts_banked_and_empty_accounts_alike(self):
        service, _, store, _, _, _ = service_with()
        seed_account(store, KEY_A)
        seed_account(store, KEY_B)
        seed_account(store, "c" * 16)
        seed_credit(store, CREDIT, KEY_A)  # only KEY_A holds a reset
        status = service.status()
        self.assertEqual(
            sorted(account.account_key for account in status.accounts),
            sorted([KEY_A, KEY_B, "c" * 16]),
        )
        self.assertEqual(status.account_count, 3)

    def test_excluded_accounts_are_not_counted(self):
        service, _, store, _, _, _ = service_with(
            settings=parse_settings({"excluded_accounts": KEY_B})
        )
        seed_account(store, KEY_A)
        seed_account(store, KEY_B)
        self.assertEqual(service.status().account_count, 1)

    def test_before_the_first_scan_the_live_pool_is_counted(self):
        """The empty state must be right on the very first open, before scan one."""
        source = FakeSource(
            [
                AccountRef(
                    account_key=KEY_A, label="a***@e***.com", source_kind="pool", entry_id="e1"
                ),
                AccountRef(
                    account_key=KEY_B, label="b***@e***.com", source_kind="pool", entry_id="e2"
                ),
            ]
        )
        service, _, _, _, _, _ = service_with(source=source)
        self.assertEqual(service.status().account_count, 2)

    def test_an_empty_pool_is_zero(self):
        """A genuinely empty pool is the one case that means "no account"."""
        service, _, _, _, _, _ = service_with(source=FakeSource([]))
        self.assertEqual(service.status().account_count, 0)

    def test_an_unreadable_store_reports_no_count(self):
        service, _, store, _, _, _ = service_with()

        def boom():
            raise RuntimeError("db is gone")

        store.account_list = boom
        self.assertIsNone(service.status().account_count)

    def test_an_unreadable_pool_reports_no_count(self):
        """A dead pool is "cannot tell", never "you have no account"."""
        source = FakeSource([])
        source.fail_accounts = "pool_unreadable"
        service, _, _, _, _, _ = service_with(source=source)
        self.assertIsNone(service.status().account_count)


class EnsureIdempotent(unittest.TestCase):
    def test_first_ensure_installs_wrappers_and_the_scan_job(self):
        service, _, store, cron, wrappers, _ = service_with()
        result = service.ensure()
        self.assertTrue(result.armed)
        self.assertEqual(len(cron.created), 1)
        self.assertIn("reset-rescue-scan", result.created)
        self.assertEqual(wrappers.installed, 1)
        self.assertEqual(result.problems, [])
        self.assertEqual(len(store.owned_jobs), 1)

    def test_a_second_ensure_creates_nothing(self):
        service, _, _, cron, _, _ = service_with()
        service.ensure()
        second = service.ensure()
        self.assertEqual(len(cron.created), 1)
        self.assertEqual(second.created, [])
        self.assertTrue(second.armed)

    def test_the_scan_job_argv_is_the_documented_one(self):
        service, _, _, cron, _, _ = service_with()
        service.ensure()
        argv = cron.created[0]
        self.assertEqual(argv[:2], ["cron", "create"])
        self.assertIn("--name", argv)
        self.assertIn("reset-rescue-scan", argv)
        self.assertIn("--script", argv)
        # The job must point at the wrapper the plugin actually installs.
        self.assertIn("reset-rescue-scan.py", argv)
        self.assertIn("--no-agent", argv)

    def test_a_changed_interval_recreates_the_job(self):
        service, deps, _, cron, _, _ = service_with()
        service.ensure()
        deps.settings = make_settings(scan_interval_hours=6)
        result = service.ensure()
        self.assertEqual(len(cron.created), 2)
        self.assertIn("reset-rescue-scan", result.updated)
        self.assertIn("every 6h", cron.created[-1])

    def test_a_missing_cron_is_a_problem_code(self):
        from reset_rescue.clock import Deps

        deps = Deps(settings=make_settings(), wrappers=FakeWrappers(), store=FakeStore())
        result = Service(deps).ensure()
        self.assertFalse(result.armed)
        self.assertIn("cron_unavailable", result.problems)

    def test_a_failed_create_is_reported_and_not_armed(self):
        service, _, _, cron, _, _ = service_with()
        cron.fail_create = True
        result = service.ensure()
        self.assertFalse(result.armed)
        self.assertIn("cron_unavailable", result.problems)
        self.assertEqual(result.created, [])

    def test_ensure_never_disarms(self):
        """D-004: there is no ``automatic_rescue`` switch to turn rescue off."""
        service, _, _, cron, _, _ = service_with()
        service.ensure()
        again = service.ensure()
        self.assertTrue(again.armed)
        self.assertEqual(again.removed, [])
        self.assertEqual(len(cron.jobs), 1)


class Scan(unittest.TestCase):
    def test_scan_calls_the_planner_and_returns_status(self):
        class Planner:
            def __init__(self):
                self.calls = []

            def scan(self, *, dry_run=False):
                self.calls.append(dry_run)

        service, deps, _, _, _, _ = service_with()
        planner = Planner()
        deps.planner = planner
        status = service.scan()
        self.assertIsInstance(status, StatusDTO)
        self.assertEqual(planner.calls, [False])

    def test_a_dry_run_request_is_forwarded(self):
        class Planner:
            def __init__(self):
                self.calls = []

            def scan(self, *, dry_run=False):
                self.calls.append(dry_run)

        service, deps, _, _, _, _ = service_with()
        planner = Planner()
        deps.planner = planner
        service.scan(dry_run=True)
        self.assertEqual(planner.calls, [True])

    def test_the_setting_dry_run_is_the_default(self):
        class Planner:
            def __init__(self):
                self.calls = []

            def scan(self, *, dry_run=False):
                self.calls.append(dry_run)

        service, deps, _, _, _, _ = service_with(settings=make_settings(dry_run=True))
        planner = Planner()
        deps.planner = planner
        service.scan()
        self.assertEqual(planner.calls, [True])

    def test_a_missing_planner_reports_not_implemented(self):
        service, _, _, _, _, _ = service_with()
        status = service.scan()
        self.assertEqual(status.schedule.problem_code, "not_implemented")
        self.assertFalse(status.schedule.armed)

    def test_a_planner_exception_does_not_escape(self):
        class Planner:
            def scan(self, *, dry_run=False):
                raise RuntimeError("boom")

        service, deps, _, _, _, _ = service_with()
        deps.planner = Planner()
        status = service.scan()
        self.assertEqual(status.schedule.problem_code, "state_unreadable")


class QuotaCheck(unittest.TestCase):
    """T-71: the one-method passthrough L3's ``cli.py`` calls."""

    def test_a_missing_planner_is_not_implemented(self):
        service, _, _, _, _, _ = service_with()
        result = service.quota_check()
        self.assertIsInstance(result, QuotaCheckDTO)
        self.assertEqual(result.problem_code, "not_implemented")
        self.assertEqual(result.exit_code, 1)

    def test_the_flow_result_is_mapped_onto_the_dto(self):
        class Planner:
            def quota_check(self):
                return {
                    "exit_code": 0,
                    "problem_code": "",
                    "accounts": 2,
                    "cooled": 1,
                    "redeemed": 1,
                    "failed": 0,
                    "results": [
                        {
                            "credit_key": CREDIT,
                            "outcome": "redeemed",
                            "reason_code": "reset",
                            "windows_reset": 1,
                        }
                    ],
                }

        service, deps, _, _, _, _ = service_with()
        deps.planner = Planner()
        result = service.quota_check()
        self.assertEqual(result.accounts, 2)
        self.assertEqual(result.cooled, 1)
        self.assertEqual(result.redeemed, 1)
        self.assertEqual(result.results[0].credit_key, CREDIT)
        self.assertEqual(result.results[0].outcome, "redeemed")

    def test_an_object_result_is_mapped_too(self):
        class Outcome:
            credit_key = CREDIT
            outcome = "already_redeemed"
            reason = "already_redeemed"
            windows_reset = 0

        class Result:
            exit_code = 0
            problem_code = ""
            accounts = 1
            cooled = 0
            redeemed = 1
            failed = 0
            results = [Outcome()]

        class Planner:
            def quota_check(self):
                return Result()

        service, deps, _, _, _, _ = service_with()
        deps.planner = Planner()
        result = service.quota_check()
        self.assertEqual(result.results[0].outcome, "already_redeemed")
        self.assertEqual(result.results[0].reason_code, "already_redeemed")

    def test_a_flow_exception_is_a_fixed_code(self):
        class Planner:
            def quota_check(self):
                raise RuntimeError("boom")

        service, deps, _, _, _, _ = service_with()
        deps.planner = Planner()
        result = service.quota_check()
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.problem_code, "internal_error")

    def test_the_dto_carries_no_percentage(self):
        service, _, _, _, _, _ = service_with()
        self.assertNotIn("pct", service.quota_check().to_json())


class BuildService(unittest.TestCase):
    """T-71: the factory L4's ``/status`` route imports."""

    def test_it_returns_a_service_with_injected_deps(self):
        deps = make_deps()
        service = build_service(deps=deps)
        self.assertIsInstance(service, Service)
        self.assertIs(service.deps, deps)

    def test_without_deps_it_returns_the_host_service(self):
        """The real factory is L3's ``HostService``, which delegates to us."""
        service = build_service()
        self.assertTrue(callable(getattr(service, "status", None)))
        status = service.status()
        self.assertIsInstance(status, StatusDTO)
        self.assertEqual(status.version, "0.1.0")

    def test_the_dashboard_route_default_factory_resolves_it(self):
        import importlib.util
        import sys
        from pathlib import Path

        repo = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location(
            "rr_plugin_api_probe", repo / "dashboard" / "plugin_api.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["rr_plugin_api_probe"] = module
        try:
            spec.loader.exec_module(module)
            factory = module._default_factory()
            service = factory()
            self.assertTrue(callable(getattr(service, "status", None)))
            self.assertIsInstance(service.status(), StatusDTO)
        finally:
            sys.modules.pop("rr_plugin_api_probe", None)


class Exclusion(unittest.TestCase):
    def test_excluding_an_account_writes_the_setting(self):
        service, deps, store, _, _, config = service_with()
        seed_account(store)
        seed_credit(store)
        status = service.set_excluded(KEY_A, True)
        self.assertEqual(config.last(), ("excluded_accounts", KEY_A))
        self.assertFalse(status.accounts[0].included)

    def test_including_an_account_removes_it_from_the_list(self):
        service, deps, store, _, _, config = service_with(
            settings=parse_settings({"excluded_accounts": f"{KEY_A},{KEY_B}"})
        )
        seed_account(store)
        seed_credit(store)
        service.set_excluded(KEY_A, False)
        self.assertEqual(config.last(), ("excluded_accounts", KEY_B))

    def test_excluding_twice_does_not_duplicate(self):
        service, deps, _, _, _, config = service_with(
            settings=parse_settings({"excluded_accounts": KEY_A})
        )
        service.set_excluded(KEY_A, True)
        self.assertEqual(config.last(), ("excluded_accounts", KEY_A))

    def test_a_config_failure_is_a_problem_code(self):
        service, _, _, _, _, config = service_with()
        config.fail = True
        status = service.set_excluded(KEY_A, True)
        self.assertEqual(status.schedule.problem_code, "settings_invalid")

    def test_no_config_writer_is_a_problem_code(self):
        from reset_rescue.clock import Deps

        deps = Deps(settings=make_settings(), store=FakeStore())
        status = Service(deps).set_excluded(KEY_A, True)
        self.assertEqual(status.schedule.problem_code, "settings_invalid")


class History(unittest.TestCase):
    def test_history_is_newest_first_and_bounded(self):
        service, _, store, _, _, _ = service_with()
        for index in range(5):
            store.history_add("scan", outcome="ok", credit_key=f"{index:016x}")
        items = service.history(limit=2).items
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].credit_key, f"{4:016x}")

    def test_the_limit_is_clamped(self):
        service, _, store, _, _, _ = service_with()
        for _ in range(3):
            store.history_add("scan", outcome="ok")
        self.assertEqual(len(service.history(limit=0).items), 1)
        self.assertEqual(len(service.history(limit=10_000).items), 3)

    def test_an_empty_history_is_an_empty_list(self):
        service, _, _, _, _, _ = service_with()
        self.assertEqual(service.history().items, [])

    def test_history_items_serialize(self):
        service, _, store, _, _, _ = service_with()
        store.history_add("fire", outcome="ok", credit_key=CREDIT, role="primary")
        payload = service.history().to_dict()
        self.assertEqual(payload["items"][0]["action"], "fire")


class ServiceNeverLeaks(unittest.TestCase):
    def test_every_dto_a_service_returns_serializes(self):
        service, deps, store, _, _, _ = service_with()
        seed_account(store)
        seed_credit(store)
        service.ensure()
        dtos = [
            service.status(),
            service.ensure(),
            service.history(),
            service.quota_check(),
            service.scan(),
        ]
        for dto in dtos:
            self.assertIsInstance(dto.to_dict(), dict)

    def test_no_dto_carries_a_token_or_raw_credit_id(self):
        service, _, store, _, _, _ = service_with()
        seed_account(store)
        seed_credit(store)
        text = service.status().to_json()
        self.assertNotIn("token", text.lower())
        self.assertNotIn("Bearer", text)


if __name__ == "__main__":
    unittest.main()
