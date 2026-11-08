"""T-71: the pieces the simplification pass added to the engine.

Four independent contracts live here, one class each:

* **D-011 time zone** — Hermes' configured zone when it has one, else the
  host's, resolved through ``hermes_time`` (the installed Hermes helper) and
  never guessed; the Desktop formats its own local zone from the ISO UTC value.
* **S2 identity** — an account key is user **plus** workspace, never the
  workspace alone (four users shared one workspace on one profile).
* **D-013 masking** — full labels only when the setting is off, and never in a
  file.
* **D-017 summary** — plan type and remaining percent, the only quota figures
  any DTO may carry.
"""

from __future__ import annotations

import datetime
import unittest
from unittest import mock

from reset_rescue.accounts import (
    PoolAccountSource,
    account_key_for,
    chatgpt_account_id,
    chatgpt_user_id,
    identity_material,
)
from reset_rescue.clock import (
    display_timezone,
    format_when,
    hermes_timezone,
    hermes_timezone_name,
    host_timezone,
    to_display,
)
from reset_rescue.codex_api import UsageSummary, parse_usage
from reset_rescue.dto import QuotaDTO
from reset_rescue.service import Service, build_service, resolve_language
from reset_rescue.settings import parse_settings
from tests.fakes import FakePool, FakeTransport, jwt_for, make_deps

SALT = "a" * 64


# ------------------------------------------------------------------ D-011


class HermesTimeZone(unittest.TestCase):
    """The helper reads Hermes' own resolution, read-only."""

    def test_it_returns_the_configured_zone_under_the_runtime(self):
        tz = hermes_timezone()
        if tz is None:
            self.skipTest("not running under the Hermes runtime")
        self.assertIsNotNone(tz)
        self.assertEqual(str(tz), hermes_timezone_name())

    def test_it_never_raises_when_hermes_time_is_absent(self):
        with mock.patch.dict("sys.modules", {"hermes_time": None}):
            self.assertIsNone(hermes_timezone())
            self.assertEqual(hermes_timezone_name(), "")

    def test_a_broken_helper_falls_back_instead_of_raising(self):
        class Broken:
            @staticmethod
            def get_timezone():
                raise RuntimeError("config is a directory")

            @staticmethod
            def get_timezone_name():
                raise RuntimeError("config is a directory")

        with mock.patch.dict("sys.modules", {"hermes_time": Broken}):
            self.assertIsNone(hermes_timezone())
            self.assertEqual(hermes_timezone_name(), "")

    def test_display_timezone_prefers_hermes(self):
        fixed = datetime.timezone(datetime.timedelta(hours=-3), "America/Sao_Paulo")
        with mock.patch("reset_rescue.clock.hermes_timezone", return_value=fixed):
            self.assertIs(display_timezone(), fixed)

    def test_display_timezone_falls_back_to_the_host(self):
        with mock.patch("reset_rescue.clock.hermes_timezone", return_value=None):
            self.assertIsNotNone(display_timezone())
            self.assertIsNotNone(host_timezone())

    def test_to_display_converts_an_epoch_into_the_display_zone(self):
        fixed = datetime.timezone(datetime.timedelta(hours=-3))
        with mock.patch("reset_rescue.clock.hermes_timezone", return_value=fixed):
            moment = to_display(1_800_000_000.0)
        self.assertEqual(moment.utcoffset(), datetime.timedelta(hours=-3))
        self.assertEqual(moment.tzinfo, fixed)
        self.assertEqual(to_display(None), None)

    def test_format_when_renders_both_languages(self):
        fixed = datetime.timezone(datetime.timedelta(hours=-3))
        # 2027-01-15T08:00:00Z → 05:00 in UTC-3.
        with mock.patch("reset_rescue.clock.hermes_timezone", return_value=fixed):
            self.assertEqual(format_when(1_800_000_000.0, "en"), "Jan 15, 05:00")
            self.assertEqual(format_when(1_800_000_000.0, "pt-BR"), "15/01 05:00")
            self.assertEqual(format_when(None, "en"), "")

    def test_the_dto_keeps_utc_iso_so_the_desktop_formats_its_own_zone(self):
        """D-011: the popup formats in the Desktop's zone from ISO UTC."""
        from reset_rescue.dto import iso_z

        self.assertEqual(iso_z(1_800_000_000.0), "2027-01-15T08:00:00Z")
        self.assertTrue(iso_z(1_800_000_000.0).endswith("Z"))


# -------------------------------------------------------------------- S2


def pool_row(
    entry_id: str = "e1",
    *,
    workspace: str = "ws-1",
    user: str = "user-1",
    email: str = "bob@example.com",
    label: str = "",
) -> dict:
    return {
        "id": entry_id,
        "provider": "openai-codex",
        "access_token": jwt_for(workspace, email, user_id=user),
        "refresh_token": "refresh-secret",
        "label": label,
        "base_url": "",
        "last_status": "ok",
    }


def source_for(rows) -> PoolAccountSource:
    pool = FakePool(rows)
    return PoolAccountSource(salt=SALT, read_pool=pool.read, transport=FakeTransport())


class IdentityIsUserPlusWorkspace(unittest.TestCase):
    """S2: four users shared one workspace; the key must not collapse them."""

    def test_the_user_claim_is_read_from_the_token(self):
        token = jwt_for("ws-1", "bob@example.com", user_id="user-1")
        self.assertEqual(chatgpt_account_id(token), "ws-1")
        self.assertEqual(chatgpt_user_id(token), "user-1")

    def test_a_token_without_a_user_claim_yields_an_empty_string(self):
        token = jwt_for("ws-1", "bob@example.com")
        self.assertEqual(chatgpt_user_id(token), "")
        self.assertEqual(identity_material("ws-1", ""), "ws-1")

    def test_identity_material_is_user_plus_workspace(self):
        self.assertEqual(identity_material("ws-1", "user-1"), "ws-1:user-1")
        self.assertNotEqual(
            identity_material("ws-1", "user-1"), identity_material("ws-1", "user-2")
        )

    def test_four_users_of_one_workspace_are_four_accounts(self):
        rows = [
            pool_row(f"e{index}", workspace="ws-shared", user=f"user-{index}") for index in range(4)
        ]
        refs = source_for(rows).accounts()
        self.assertEqual(len(refs), 4)
        self.assertEqual(len({ref.account_key for ref in refs}), 4)

    def test_the_same_user_and_workspace_is_one_account(self):
        rows = [
            pool_row("e1", workspace="ws-1", user="user-1"),
            pool_row("e2", workspace="ws-1", user="user-1"),
        ]
        source = source_for(rows)
        refs = source.accounts()
        self.assertEqual(len(refs), 1)
        self.assertEqual(source.duplicates()[refs[0].account_key], ["e2"])

    def test_a_token_without_a_user_claim_keeps_the_workspace_key(self):
        rows = [pool_row("e1", workspace="ws-1", user="")]
        refs = source_for(rows).accounts()
        self.assertEqual(refs[0].account_key, account_key_for(SALT, "ws-1"))

    def test_the_key_never_contains_the_raw_identity(self):
        rows = [pool_row("e1", workspace="ws-secret", user="user-secret")]
        key = source_for(rows).accounts()[0].account_key
        self.assertEqual(len(key), 16)
        self.assertNotIn("ws-secret", key)
        self.assertNotIn("user-secret", key)

    def test_the_pool_key_and_the_identity_agree(self):
        rows = [pool_row("e1", workspace="ws-1", user="user-1")]
        ref = source_for(rows).accounts()[0]
        self.assertEqual(ref.account_key, account_key_for(SALT, "ws-1:user-1"))


# ------------------------------------------------------------------ D-013


class MaskingIsASetting(unittest.TestCase):
    def test_the_pool_keeps_the_masked_label_and_the_full_one_separately(self):
        rows = [pool_row("e1", email="bob@example.com")]
        source = source_for(rows)
        ref = source.accounts()[0]
        self.assertEqual(ref.label, "b***@e***.com")
        self.assertEqual(source.full_label(ref.account_key), "bob@example.com")

    def test_the_full_label_is_empty_for_an_unknown_key(self):
        source = source_for([pool_row("e1")])
        source.accounts()
        self.assertEqual(source.full_label("f" * 16), "")

    def test_the_stored_row_label_wins_over_the_email_claim(self):
        rows = [pool_row("e1", email="bob@example.com", label="Jane (work)")]
        source = source_for(rows)
        ref = source.accounts()[0]
        self.assertEqual(source.full_label(ref.account_key), "Jane (work)")
        self.assertNotIn("bob@example.com", ref.label)

    def test_the_full_label_never_reaches_a_dto_by_default(self):
        rows = [pool_row("e1", email="bob@example.com")]
        deps = make_deps(pool_rows=rows, salt=SALT)
        deps.store.account_upsert(
            {
                "account_key": account_key_for(SALT, "ws-1:user-1"),
                "label": "b***@e***.com",
                "source_kind": "pool",
            }
        )
        deps.store.credit_upsert(
            {
                "credit_key": "c" * 16,
                "account_key": account_key_for(SALT, "ws-1:user-1"),
                "title": "Full reset",
                "reset_type": "codex_rate_limits",
                "expires_at": 1_800_003_600.0,
                "backend_state": "available",
                "status": "scheduled",
                "updated_at": 1_800_000_000.0,
            }
        )
        text = Service(deps).status().to_json()
        self.assertNotIn("bob@example.com", text)
        self.assertIn("b***@e***.com", text)


# ------------------------------------------------------------------ D-017


class SummaryShape(unittest.TestCase):
    def test_the_summary_is_plan_plus_present_windows(self):
        summary = parse_usage(
            {
                "plan_type": "pro_200",
                "rate_limit": {
                    "primary_window": {"limit_window_seconds": 604800, "used_percent": 60.0}
                },
            }
        )
        self.assertEqual(summary.plan_type, "Pro 200")
        windows = {window.label: window.remaining_pct for window in summary.windows}
        dto = QuotaDTO(
            plan_type=summary.plan_type,
            five_hour_remaining_pct=None,
            weekly_remaining_pct=round(windows["weekly"]),
        )
        self.assertEqual(
            dto.to_dict(),
            {"plan_type": "Pro 200", "five_hour_remaining_pct": None, "weekly_remaining_pct": 40},
        )

    def test_a_missing_plan_is_none_not_an_empty_string(self):
        self.assertIsNone(UsageSummary().plan_type)

    def test_remaining_is_rounded_for_display(self):
        dto = QuotaDTO(
            plan_type=None, five_hour_remaining_pct=None, weekly_remaining_pct=round(40.4)
        )
        self.assertEqual(dto.weekly_remaining_pct, 40)


# ------------------------------------------------------- service factory


class BuildServiceFactory(unittest.TestCase):
    def test_it_accepts_injected_deps(self):
        deps = make_deps()
        self.assertIs(build_service(deps=deps).deps, deps)

    def test_it_answers_status_without_the_host_lane(self):
        status = build_service().status()
        self.assertEqual(status.version, "0.1.0")

    def test_resolve_language_is_shared_by_the_route_and_the_notices(self):
        self.assertEqual(resolve_language(parse_settings({"language": "pt-BR"}), "en"), "pt-BR")
        self.assertEqual(resolve_language(parse_settings({"language": "auto"}), "pt-BR"), "pt-BR")
        self.assertEqual(resolve_language(parse_settings({"language": "auto"}), ""), "en")


if __name__ == "__main__":
    unittest.main()
