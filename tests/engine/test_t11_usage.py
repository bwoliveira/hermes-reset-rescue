"""T-11/T-71: the usage read — early trigger (D-009) and summary (D-017).

Two consumers, one read:

* the opt-in early-redemption trigger compares ``weekly_remaining_pct`` against
  the configured threshold and never lets the number escape;
* the popup's compact summary (D-017) shows the plan name and each present
  window's **remaining** percent, rounded, and nothing else.

A failed read must leave both untouched: no decision changes, no exception, no
redemption affected.
"""

from __future__ import annotations

import unittest

from reset_rescue.accounts import AccountRef, Response, SourceUnavailable, account_key_for
from reset_rescue.codex_api import (
    CodexResetApi,
    ReadOnlyTransport,
    UsageSummary,
    UsageWindow,
    parse_usage,
    plan_type_from,
)
from reset_rescue.dto import RedeemResultDTO
from reset_rescue.service import Service
from reset_rescue.settings import parse_settings
from tests.fakes import FakeSource, make_deps, make_settings, usage_payload

SALT = "a" * 64
WEEKLY = 604800
FIVE_HOURS = 18000


def account() -> AccountRef:
    return AccountRef(
        account_key=account_key_for(SALT, "acct-1"),
        label="b***@e***.com",
        source_kind="pool",
        entry_id="e1",
    )


class PercentagesAreParsed(unittest.TestCase):
    def test_weekly_and_five_hour_percentages(self):
        summary = parse_usage(usage_payload(weekly=42.0, session=7.5))
        self.assertEqual(summary.weekly_pct, 42.0)
        self.assertEqual(summary.five_hour_pct, 7.5)
        self.assertTrue(summary.known)

    def test_a_window_is_matched_by_its_length_not_its_position(self):
        payload = {
            "rate_limit": {
                "primary_window": {"limit_window_seconds": FIVE_HOURS, "used_percent": 11.0},
                "secondary_window": {"limit_window_seconds": WEEKLY, "used_percent": 22.0},
            }
        }
        summary = parse_usage(payload)
        self.assertEqual(summary.five_hour_pct, 11.0)
        self.assertEqual(summary.weekly_pct, 22.0)

    def test_only_one_window_present(self):
        summary = parse_usage(usage_payload(weekly=99.0))
        self.assertEqual(summary.weekly_pct, 99.0)
        self.assertIsNone(summary.five_hour_pct)
        self.assertTrue(summary.known)

    def test_an_unknown_window_length_is_ignored(self):
        summary = parse_usage(
            {"rate_limit": {"primary_window": {"limit_window_seconds": 3600, "used_percent": 5.0}}}
        )
        self.assertFalse(summary.known)

    def test_malformed_payloads_are_empty_not_errors(self):
        for payload in (
            None,
            {},
            [],
            "x",
            {"rate_limit": None},
            {"rate_limit": []},
            {"rate_limit": {"primary_window": "x"}},
            {"rate_limit": {"primary_window": {"limit_window_seconds": WEEKLY}}},
        ):
            self.assertFalse(parse_usage(payload).known, payload)

    def test_a_bool_is_not_a_percentage(self):
        self.assertFalse(
            parse_usage(
                {
                    "rate_limit": {
                        "primary_window": {"limit_window_seconds": WEEKLY, "used_percent": True}
                    }
                }
            ).known
        )

    def test_a_string_percentage_is_not_accepted(self):
        self.assertFalse(
            parse_usage(
                {
                    "rate_limit": {
                        "primary_window": {"limit_window_seconds": WEEKLY, "used_percent": "42"}
                    }
                }
            ).known
        )


class PlanType(unittest.TestCase):
    """D-017: the plan name as the backend reports it, title-cased like Hermes."""

    def test_snake_case_is_title_cased(self):
        self.assertEqual(plan_type_from({"plan_type": "pro_200"}), "Pro 200")
        self.assertEqual(plan_type_from({"plan_type": "plus"}), "Plus")
        self.assertEqual(plan_type_from({"plan_type": "STUDENT"}), "Student")

    def test_hyphens_are_treated_like_underscores(self):
        self.assertEqual(plan_type_from({"plan_type": "pro-500"}), "Pro 500")

    def test_missing_or_odd_values_are_none(self):
        for payload in ({}, {"plan_type": ""}, {"plan_type": None}, {"plan_type": 42}, None):
            self.assertIsNone(plan_type_from(payload), payload)

    def test_parse_usage_carries_it(self):
        payload = usage_payload(weekly=10.0)
        payload["plan_type"] = "pro_200"
        self.assertEqual(parse_usage(payload).plan_type, "Pro 200")


class RemainingPercent(unittest.TestCase):
    """D-009 compares weekly *remaining*, so the complement must be right."""

    def test_remaining_is_the_complement_of_used(self):
        self.assertEqual(UsageSummary(weekly_pct=42.0).weekly_remaining_pct, 58.0)
        self.assertEqual(UsageSummary(weekly_pct=0.0).weekly_remaining_pct, 100.0)
        self.assertEqual(UsageSummary(weekly_pct=100.0).weekly_remaining_pct, 0.0)

    def test_an_unknown_weekly_window_yields_none_not_zero(self):
        # None means "cannot decide"; 0 would mean "exhausted" and would fire
        # an early redemption on missing data.
        self.assertIsNone(UsageSummary().weekly_remaining_pct)
        self.assertIsNone(UsageSummary(five_hour_pct=50.0).weekly_remaining_pct)

    def test_remaining_is_clamped_for_out_of_range_input(self):
        self.assertEqual(UsageSummary(weekly_pct=120.0).weekly_remaining_pct, 0.0)
        self.assertEqual(UsageSummary(weekly_pct=-20.0).weekly_remaining_pct, 100.0)


class SummaryWindows(unittest.TestCase):
    """D-017: the present windows, 5 h first, each with remaining percent."""

    def test_both_windows_present(self):
        windows = UsageSummary(weekly_pct=60.0, five_hour_pct=12.0).windows
        self.assertEqual([window.label for window in windows], ["session", "weekly"])
        self.assertEqual([window.remaining_pct for window in windows], [88.0, 40.0])
        self.assertEqual([window.window_seconds for window in windows], [FIVE_HOURS, WEEKLY])

    def test_only_the_weekly_window(self):
        windows = UsageSummary(weekly_pct=100.0).windows
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].label, "weekly")
        self.assertEqual(windows[0].remaining_pct, 0.0)

    def test_no_window_at_all(self):
        self.assertEqual(UsageSummary().windows, ())

    def test_the_window_dict_rounds_and_keeps_only_remaining(self):
        window = UsageWindow("weekly", used_pct=41.4, window_seconds=WEEKLY)
        self.assertEqual(window.as_dict(), {"label": "weekly", "remaining_pct": 59})

    def test_a_window_repr_names_no_percentage(self):
        text = repr(UsageWindow("weekly", used_pct=41.0, window_seconds=WEEKLY))
        self.assertNotIn("41", text)
        self.assertNotIn("59", text)


class UsageNeverFailsARedemption(unittest.TestCase):
    def test_a_transport_failure_returns_an_empty_summary(self):
        source = FakeSource([account()])
        source.on("GET", "/usage", SourceUnavailable("network"))
        api = CodexResetApi(source, sleep=lambda _s: None)
        self.assertFalse(api.usage(source.accounts()[0]).known)

    def test_a_timeout_returns_an_empty_summary(self):
        source = FakeSource([account()])
        source.on("GET", "/usage", TimeoutError())
        api = CodexResetApi(source, sleep=lambda _s: None)
        self.assertFalse(api.usage(source.accounts()[0]).known)

    def test_a_500_returns_an_empty_summary(self):
        source = FakeSource([account()])
        source.on("GET", "/usage", Response(500))
        api = CodexResetApi(source, sleep=lambda _s: None)
        self.assertFalse(api.usage(source.accounts()[0]).known)

    def test_a_401_returns_an_empty_summary_without_raising(self):
        source = FakeSource([account()])
        source.on("GET", "/usage", Response(401))
        api = CodexResetApi(source, sleep=lambda _s: None)
        self.assertFalse(api.usage(source.accounts()[0]).known)

    def test_a_usage_read_does_not_prevent_a_consume(self):
        source = FakeSource([account()])
        source.on("GET", "/usage", SourceUnavailable("network"))
        source.on("POST", "/consume", Response(200, {"code": "reset", "windows_reset": 1}))
        api = CodexResetApi(source, sleep=lambda _s: None)
        acct = source.accounts()[0]
        self.assertFalse(api.usage(acct).known)
        self.assertTrue(api.consume(acct, "credit-1", "uuid-1").ok)

    def test_usage_is_a_get_only(self):
        source = FakeSource([account()])
        source.on("GET", "/usage", Response(200, usage_payload(weekly=1.0)))
        api = CodexResetApi(source, sleep=lambda _s: None)
        api.usage(source.accounts()[0])
        self.assertEqual(source.count("POST"), 0)
        self.assertEqual(source.count("GET", "/usage"), 1)

    def test_a_read_only_transport_can_still_read_usage(self):
        source = FakeSource([account()])
        source.on("GET", "/usage", Response(200, usage_payload(weekly=5.0)))
        api = CodexResetApi(ReadOnlyTransport(source), sleep=lambda _s: None)
        self.assertEqual(api.usage(source.accounts()[0]).weekly_pct, 5.0)

    def test_the_call_accepts_a_timeout_budget(self):
        """``/status`` has a 9 s budget: the read must honour a shorter one."""
        source = FakeSource([account()])
        source.on("GET", "/usage", Response(200, usage_payload(weekly=5.0)))
        api = CodexResetApi(source, sleep=lambda _s: None)
        api.usage(source.accounts()[0], timeout=1.5)
        self.assertEqual(source.requests[-1]["timeout"], 1.5)


class UsageReachesOnlyTheSummary(unittest.TestCase):
    """D-010 + D-017: percentages live only in ``AccountDTO.quota``."""

    def test_the_summary_repr_names_no_percentage(self):
        text = repr(UsageSummary(weekly_pct=42.0, five_hour_pct=7.5))
        self.assertNotIn("42", text)
        self.assertNotIn("7.5", text)
        self.assertIn("known=True", text)

    def test_a_redeem_result_has_no_usage_field(self):
        self.assertNotIn("usage", RedeemResultDTO.FIELDS)

    def test_a_status_dto_has_no_usage_field_at_any_depth(self):
        deps = make_deps(salt=SALT)
        payload = Service(deps).status().to_dict()

        def walk(value, path=""):
            if isinstance(value, dict):
                for key, item in value.items():
                    self.assertNotIn("usage", key.lower(), f"{path}.{key}")
                    walk(item, f"{path}.{key}")
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    walk(item, f"{path}[{index}]")

        walk(payload)

    def test_the_only_percentage_keys_are_the_summary_and_the_threshold(self):
        """D-017 + D-012: the summary's remaining figures and the user's own
        configured threshold. Nothing else may carry a percentage."""
        deps = make_deps(salt=SALT)
        payload = Service(deps).status().to_dict()
        allowed = {"five_hour_remaining_pct", "weekly_remaining_pct", "early_redeem_remaining_pct"}

        def walk(value, path=""):
            if isinstance(value, dict):
                for key, item in value.items():
                    if "pct" in key.lower() or "percent" in key.lower():
                        self.assertIn(key, allowed, f"{path}.{key}")
                    walk(item, f"{path}.{key}")
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    walk(item, f"{path}[{index}]")

        walk(payload)

    def test_the_early_redeem_threshold_is_the_users_own_setting(self):
        """The footer shows the configured threshold (a setting, not a reading)."""
        settings = parse_settings({"early_redeem_enabled": True, "early_redeem_remaining_pct": 15})
        self.assertEqual(settings.early_redeem_threshold_pct, 15.0)
        deps = make_deps(salt=SALT, settings=settings)
        payload = Service(deps).status().to_dict()
        self.assertEqual(payload["settings"]["early_redeem_remaining_pct"], 15)
        self.assertNotIn("early_redeem_weekly_remaining_pct", str(payload))


class TriggerDecision(unittest.TestCase):
    """The comparison D-009 defines, exercised through the settings object."""

    def _fires(self, *, enabled: bool, threshold: int, used_pct):
        settings = make_settings(early_redeem_enabled=enabled, early_redeem_remaining_pct=threshold)
        limit = settings.early_redeem_threshold_pct
        if limit is None:
            return False
        remaining = UsageSummary(weekly_pct=used_pct).weekly_remaining_pct
        if remaining is None:
            return False
        return remaining <= limit

    def test_off_never_fires(self):
        self.assertFalse(self._fires(enabled=False, threshold=100, used_pct=99.0))

    def test_at_or_below_the_threshold_fires(self):
        self.assertTrue(self._fires(enabled=True, threshold=10, used_pct=90.0))
        self.assertTrue(self._fires(enabled=True, threshold=10, used_pct=95.0))

    def test_above_the_threshold_does_not_fire(self):
        self.assertFalse(self._fires(enabled=True, threshold=10, used_pct=89.0))

    def test_the_default_threshold_of_zero_only_fires_when_fully_used(self):
        self.assertTrue(self._fires(enabled=True, threshold=0, used_pct=100.0))
        self.assertFalse(self._fires(enabled=True, threshold=0, used_pct=99.9))

    def test_an_unknown_weekly_window_never_fires(self):
        self.assertFalse(self._fires(enabled=True, threshold=100, used_pct=None))


if __name__ == "__main__":
    unittest.main()
