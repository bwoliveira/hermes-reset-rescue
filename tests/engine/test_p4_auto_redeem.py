"""D-023 (P4): ``auto_redeem_enabled`` — the master switch for automatic redemption.

Symmetric with ``early_redeem_enabled``: an explicit setting, default **true**,
that turns the *automatic* redemption off at expiry without turning the plugin
off. What must still happen while it is off:

* the scan still runs and still records what it found (the popup shows it);
* reminders still go out (they are notices, not spends);
* **no** primary/backup fire job is armed;
* ``authorize_consume`` refuses every automatic role with ``auto_redeem_disabled``;
* a manual, confirmed ``Redeem now`` still works;
* turning it back on re-arms on the next scan.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from reset_rescue.authorize import authorize_consume
from reset_rescue.settings import (
    DEFAULT_AUTO_REDEEM_ENABLED,
    Settings,
    config_schema,
    parse_settings,
)
from tests.flows.support import HOUR, Harness


def settings_with(**overrides) -> Settings:
    raw = {"auto_redeem_enabled": True}
    raw.update(overrides)
    return parse_settings(raw)


class TheSetting(unittest.TestCase):
    def test_default_is_on(self):
        self.assertTrue(DEFAULT_AUTO_REDEEM_ENABLED)
        self.assertTrue(parse_settings({}).auto_redeem_enabled)
        self.assertTrue(Settings().auto_redeem_enabled)

    def test_a_wrong_type_falls_back_to_the_default_with_a_warning(self):
        """``coerce_bool`` accepts the YAML spellings and refuses the rest."""
        for raw, expected in (
            (False, False),
            (True, True),
            ("false", False),
            ("no", False),
            (0, False),
        ):
            self.assertEqual(
                parse_settings({"auto_redeem_enabled": raw}).auto_redeem_enabled, expected, raw
            )
        for raw in ("maybe", [], {}):
            settings = parse_settings({"auto_redeem_enabled": raw})
            self.assertTrue(settings.auto_redeem_enabled, raw)
            self.assertIn("settings_auto_redeem_invalid", settings.warnings, raw)

    def test_the_manifest_declares_it(self):
        entry = config_schema()["auto_redeem_enabled"]
        self.assertEqual(entry["type"], "bool")
        self.assertEqual(entry["default"], True)
        self.assertEqual(entry["label"], "Redeem automatically before expiry")


class Authorize(unittest.TestCase):
    """The money boundary refuses every automatic role while the switch is off."""

    def setUp(self) -> None:
        self.credit = SimpleNamespace(expires_at=1_800_000_000.0)
        self.off = settings_with(auto_redeem_enabled=False)
        self.on = settings_with()

    def test_automatic_roles_are_refused_with_a_reason_code(self):
        for role in ("primary", "backup", "scan"):
            decision = authorize_consume(
                role=role, credit=self.credit, settings=self.off, now=1_800_000_000.0 - 60
            )
            self.assertFalse(decision, role)
            self.assertEqual(decision.reason, "auto_redeem_disabled", role)

    def test_the_quota_role_is_refused_too(self):
        decision = authorize_consume(
            role="early",
            credit=self.credit,
            settings=settings_with(
                auto_redeem_enabled=False, early_redeem_enabled=True, early_redeem_remaining_pct=50
            ),
            now=1_800_000_000.0 - 60,
            quota_reading=10.0,
        )
        self.assertFalse(decision)
        self.assertEqual(decision.reason, "auto_redeem_disabled")

    def test_manual_confirmed_still_works(self):
        decision = authorize_consume(
            role="manual", credit=self.credit, settings=self.off, now=1_800_000_000.0, confirm=True
        )
        self.assertTrue(decision, decision.reason)
        self.assertEqual(decision.reason, "manual_confirmed")

    def test_manual_still_needs_the_confirmation(self):
        decision = authorize_consume(
            role="manual", credit=self.credit, settings=self.off, now=1_800_000_000.0, confirm=False
        )
        self.assertFalse(decision)
        self.assertEqual(decision.reason, "confirmation_required")

    def test_dry_run_still_wins_over_the_switch(self):
        decision = authorize_consume(
            role="primary",
            credit=self.credit,
            settings=settings_with(auto_redeem_enabled=False, dry_run=True),
            now=1_800_000_000.0 - 60,
        )
        self.assertFalse(decision)
        self.assertEqual(decision.reason, "dry_run")

    def test_on_still_allows_the_primary_inside_its_window(self):
        decision = authorize_consume(
            role="primary", credit=self.credit, settings=self.on, now=1_800_000_000.0 - 60
        )
        self.assertTrue(decision, decision.reason)


class Planner(unittest.TestCase):
    """No fire/backup job while off; reminders and the scan still happen."""

    def setUp(self) -> None:
        self.h = Harness(self)
        self.h.deps.settings = settings_with(auto_redeem_enabled=False)
        self.account = self.h.add_account()
        self.credit = self.h.add_credit(self.account, expires_in=30 * HOUR)
        self.assertEqual(self.h.scan().exit_code, 0)
        self.key = self.h.key(self.account)

    def fire_jobs(self) -> list[dict]:
        return [
            job
            for job in self.h.cron.jobs.values()
            if any(marker in job["name"] for marker in ("fire", "primary", "backup"))
            and "remind" not in job["name"]
        ]

    def reminder_jobs(self) -> list[dict]:
        return [job for job in self.h.cron.jobs.values() if "remind-" in job["name"]]

    def test_no_redemption_job_is_armed(self):
        self.assertEqual(self.fire_jobs(), [], [job["name"] for job in self.h.cron.jobs.values()])
        self.assertIsNone(self.h.slot(self.account, "primary"))
        self.assertIsNone(self.h.slot(self.account, "backup"))

    def test_the_credit_is_still_recorded_and_shown(self):
        row = self.h.row(self.account)
        self.assertIsNotNone(row, "the popup still lists what the scan found")
        self.assertEqual(row["status"], "seen", "recorded, never scheduled")

    def test_reminders_still_go_out(self):
        self.assertEqual(len(self.reminder_jobs()), 2, "both reminder one-shots stay armed")
        self.h.advance(30 * HOUR - 24 * HOUR + 1)
        self.h.run_fire(role="remind")
        self.assertTrue(self.h.notices("reminder"), "a reminder is a notice, never a spend")

    def test_a_late_credit_is_not_redeemed_by_the_scan(self):
        """The late branch of the scan is an automatic consume too."""
        self.h.advance(30 * HOUR - 5 * 60)
        before = len(self.h.source.posts())
        self.h.scan()
        self.assertEqual(len(self.h.source.posts()), before, "no POST while auto-redeem is off")

    def test_turning_it_back_on_re_arms_on_the_next_scan(self):
        self.h.deps.settings = settings_with(auto_redeem_enabled=True)
        self.assertEqual(self.h.scan().exit_code, 0)
        self.assertIsNotNone(self.h.slot(self.account, "primary"))
        self.assertIsNotNone(self.h.slot(self.account, "backup"))

    def test_a_manual_redeem_still_fires_while_off(self):
        """A confirmed manual redemption is not an automatic consume."""
        from reset_rescue.fire import ROLE_MANUAL

        result = self.h.fire().redeem_one(self.key, role=ROLE_MANUAL, confirm=True)
        self.assertFalse(result.failure, result.reason)
        self.assertEqual(len(self.h.source.posts()), 1)


class ServiceView(unittest.TestCase):
    """The popup's own payload says auto-redeem is off."""

    def test_status_settings_carry_the_switch(self):
        from reset_rescue.service import Service
        from tests.fakes import make_deps

        deps = make_deps(settings=settings_with(auto_redeem_enabled=False))
        status = Service(deps).status()
        self.assertFalse(status.settings.auto_redeem_enabled)
        self.assertFalse(status.auto_redeem_enabled)

    def test_status_reports_it_on_by_default(self):
        from reset_rescue.service import Service
        from tests.fakes import make_deps

        status = Service(make_deps()).status()
        self.assertTrue(status.settings.auto_redeem_enabled)
        self.assertTrue(status.auto_redeem_enabled)
