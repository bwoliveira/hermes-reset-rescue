"""D-013 — every notice names the account, never the credit title.

``params["label"]`` is the account label as Hermes stores it; masking is the
render boundary's job (``notify.Notifier``, ``mask_labels``). A credit title
("Full reset") in that slot is invisible to masking and tells the user nothing
about *which* account the notice is about.

The second half of this file is the guard that hid the bug: a notice whose
parameters do not match ``messages.PARAMS`` is refused by the catalog and
delivered as an empty message, so a flow test that only counts notices in the
fake notifier never notices. Every kind below is therefore also rendered
through the real catalog and delivered through the real ``Notifier``.
"""

from __future__ import annotations

import json
import unittest

from reset_rescue import messages
from reset_rescue.cli_runner import CliResult
from reset_rescue.fire import ROLE_REMIND
from reset_rescue.notify import Notifier
from reset_rescue.settings import parse_settings
from tests.flows.support import HOUR, MINUTE, Harness


class _Cli:
    """A ``hermes send --json`` that always succeeds (the real payload shape)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, argv, *, timeout: float = 30.0) -> CliResult:
        call = [str(part) for part in argv]
        self.calls.append(call)
        return CliResult(
            argv=tuple(call),
            returncode=0,
            stdout=json.dumps({"success": True, "message_id": 1, "note": "sent"}),
        )


class NoticeLabelCase(unittest.TestCase):
    """One scenario per notice kind; each returns ``(harness, account)``."""

    #: Every kind a user can receive (D-007, D-009, D-015, A§8.6).
    SCENARIOS = (
        "reminder",
        "success",
        "final_failure",
        "info",
        "early",
        "manual",
        "account_error",
        "missed",
    )

    def harness(self) -> Harness:
        h = Harness(self)
        h.deps.settings = parse_settings(
            {
                "early_redeem_enabled": True,
                "early_redeem_weekly_remaining_pct": 0,
                "lead_minutes": 10,
                "backup_lead_minutes": 4,
            }
        )
        return h

    def armed(self, h: Harness):
        account = h.add_account()
        credit = h.add_credit(account, expires_in=30 * HOUR)
        self.assertEqual(h.scan().exit_code, 0)
        return account, credit

    # -- one scenario per kind ---------------------------------------------
    def reminder(self):
        h = self.harness()
        account, _ = self.armed(h)
        slot = h.slot(account, "reminder1")
        self.assertIsNotNone(slot, "no reminder slot")
        h.advance_to(float(slot["fire_at"]) + 5)
        h.fire().run(role=ROLE_REMIND)
        return h, account

    def success(self):
        h = self.harness()
        account, _ = self.armed(h)
        h.advance_to(float(h.slot(account, "primary")["fire_at"]))
        h.run_fire("primary")
        return h, account

    def final_failure(self):
        h = self.harness()
        account, _ = self.armed(h)
        h.backend.on_post = lambda *_: {"code": "rate_limit_reset_ineligible"}
        h.advance_to(float(h.slot(account, "primary")["fire_at"]))
        h.run_fire("primary")
        return h, account

    def info(self):
        h = self.harness()
        account, credit = self.armed(h)
        credit.status = "redeemed"
        credit.redeemed_at = h.clock.now()
        h.advance_to(float(h.slot(account, "primary")["fire_at"]))
        h.run_fire("primary")
        return h, account

    def early(self):
        h = self.harness()
        account, _ = self.armed(h)
        h.backend.usage_body = {
            "rate_limit": {
                "primary_window": {"limit_window_seconds": 604800, "used_percent": 100.0},
                "secondary_window": {"limit_window_seconds": 18000, "used_percent": 0.0},
            }
        }
        h.planner().quota_check()
        return h, account

    def manual(self):
        h = self.harness()
        account, _ = self.armed(h)
        h.fire().redeem_now(h.key(account), confirm=True)
        return h, account

    def account_error(self):
        h = self.harness()
        account, _ = self.armed(h)  # the account and its credit are known
        h.backend.fail_list[account.account_key] = ConnectionError("down")
        h.scan()  # the sign-in expired: one transition notice
        return h, account

    def missed(self):
        h = self.harness()
        account, _ = self.armed(h)
        h.advance_to(float(h.slot(account, "backup")["fire_at"]) + 16 * MINUTE)
        h.scan()
        return h, account

    # -- assertions ---------------------------------------------------------
    def test_every_notice_carries_the_account_label(self):
        for name in self.SCENARIOS:
            with self.subTest(kind=name):
                h, account = getattr(self, name)()
                notices = h.notices()
                self.assertTrue(notices, f"{name}: no notice was queued")
                for notice in notices:
                    params = notice["params"]
                    self.assertEqual(
                        params.get("label"),
                        account.label,
                        f"{name}: label is not the account label",
                    )
                    self.assertNotIn(
                        "Full reset",
                        json.dumps(params),
                        f"{name}: the credit title leaked into the params",
                    )

    def test_every_notice_renders_under_the_catalog(self):
        """A parameter the catalog does not declare drops the whole notice."""
        for name in self.SCENARIOS:
            with self.subTest(kind=name):
                h, account = getattr(self, name)()
                for notice in h.notices():
                    kind = notice["kind"]
                    canonical = messages.KIND_ALIASES.get(kind, kind)
                    self.assertIn(canonical, messages.KINDS, f"{name}: {kind}")
                    for key in messages.PARAMS[canonical]:
                        self.assertNotIn(
                            notice["params"].get(key), (None, ""), f"{name}: {kind}.{key} is empty"
                        )
                    text = messages.render(kind, notice["params"], language="en")
                    self.assertTrue(text.strip(), f"{name}: rendered empty")
                    self.assertIn(account.label, text, name)
                    self.assertNotIn("  ", text, f"{name}: a field left a hole")

    def test_every_notice_reaches_the_cli_with_the_label(self):
        """The real notifier, over the real catalog, with masking on (D-013)."""
        for name in self.SCENARIOS:
            with self.subTest(kind=name):
                h, account = getattr(self, name)()
                cli = _Cli()
                notifier = Notifier(cli, target="telegram", language="en", mask_labels=True)
                for notice in h.notices():
                    result = notifier.notify(notice["kind"], notice["params"])
                    self.assertTrue(result.delivered, f"{name}: {result}")
                bodies = [call[-1] for call in cli.calls]
                self.assertTrue(bodies, f"{name}: nothing reached the CLI")
                for body in bodies:
                    self.assertIn(account.label, body, name)

    def test_the_outbox_row_stores_the_account_label(self):
        """The durable record carries the label, so a later flush still masks it."""
        h, account = self.success()
        rows = h.store.list_notices()
        self.assertTrue(rows, "no outbox row was written")
        for row in rows:
            payload = json.loads(row["params_json"])
            self.assertEqual(payload.get("label"), account.label, row["kind"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
