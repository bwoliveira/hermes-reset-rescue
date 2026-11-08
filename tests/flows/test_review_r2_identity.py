"""Review round 2, lane GA — identity and proof of consumption (R10, R4, R16).

Every test here is the reviewer's own counterexample, ported to the repo's
harness (fakes only, no network), plus one more variant of the same class
designed here. The classes:

* **R10** — a rule about *who* an account is must not be decided by a label
  that exists only to be displayed. The gate gets the canonical identity
  (account key plus the masked label the plugin stores), whatever
  ``mask_labels`` says.
* **R4** — a list is readable only when *every* row parsed with an identity.
  One dropped row makes absence unproven; a valid empty list still proves it.
* **R16** — the outbox keeps the masked label and a reference; the full label
  is resolved in memory at send time, so turning unmasking off later leaves
  nothing full on disk.

The tests drive the public surface a user reaches — ``Planner``, ``Fire``, the
real ``Store`` and the real ``Notifier`` — so a fix that special-cases one
caller still fails here.
"""

from __future__ import annotations

import dataclasses
import json
import unittest

from reset_rescue.accounts import PoolAccountSource, Response, mask_label
from reset_rescue.cli_runner import CliResult
from reset_rescue.notify import Notifier
from tests.fakes import jwt_for
from tests.flows.support import HOUR, Harness

FULL = "synthetic.full@example.invalid"
MASKED = "s***@e***.invalid"


class _Sender:
    """A ``hermes send --json`` stand-in that always succeeds."""

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

    def bodies(self) -> list[str]:
        return [call[-1] for call in self.calls]


class _NoFlush:
    """A notifier double that queues nothing and never drains the outbox."""

    language = "en"

    def enqueue(self, *args, **kwargs) -> None:
        return None


def _resolver(masked: str, full: str = FULL):
    """The production resolver's contract: masked label → full label, in memory."""

    def resolve(label: str) -> str:
        return full if str(label) == masked else ""

    return resolve


class R10CanonicalIdentity(unittest.TestCase):
    """R10 — presentation must not decide authorization to spend."""

    def test_r10_unmasked_notice_setting_must_not_bypass_masked_label_exclusion(self):
        """The reviewer's counterexample: exclusion by the masked label, unmask on."""
        h = Harness(self)
        account, key = h.armed(expires_in=HOUR)
        h.source.full_label = lambda account_key: FULL
        h.deps.settings = dataclasses.replace(
            h.settings, mask_labels=False, excluded_accounts=(account.label,)
        )
        h.clock.set(h.store.get_slot(key, "primary")["fire_at"])
        h.run_fire()
        self.assertEqual(h.backend.posts, [], "notice display mode changed account authorization")

    def test_r10_a_full_source_label_must_not_defeat_the_stored_masked_exclusion(self):
        """Variant: the source hands a full label, the exclusion is the masked one.

        A legacy or foreign source row may carry a full label while the
        exclusion the user wrote is the masked label the plugin itself stores.
        The gate must ask the canonical identity, not whichever label the
        source happens to hand this run.
        """
        h = Harness(self)
        account = h.add_account(FULL)
        h.add_credit(account, expires_in=HOUR)
        self.assertEqual(h.scan().exit_code, 0)
        key = h.key(account)
        h.deps.settings = dataclasses.replace(
            h.settings, mask_labels=False, excluded_accounts=(mask_label(FULL),)
        )
        h.clock.set(h.store.get_slot(key, "primary")["fire_at"])
        h.run_fire()
        self.assertEqual(h.backend.posts, [], "the stored masked label did not exclude the account")

    def test_r10_real_pool_source_must_not_bypass_masked_label_exclusion(self):
        """The reviewer's second counterexample: the real ``PoolAccountSource``."""
        h = Harness(self)
        bridge_key = {"value": ""}

        def bridge(method, url, headers, payload, timeout):
            if method == "POST":
                body = json.loads(payload)
                return Response(
                    200,
                    h.backend.consume(
                        bridge_key["value"], body["credit_id"], body["redeem_request_id"]
                    ),
                )
            if url.endswith("/usage"):
                return Response(200, h.backend.usage_payload(bridge_key["value"]))
            return Response(200, h.backend.list_payload(bridge_key["value"]))

        pool = PoolAccountSource(
            salt=h.deps.salt,
            read_pool=lambda: [
                {
                    "id": "synthetic-entry",
                    "account_id": "synthetic-workspace",
                    "access_token": jwt_for("synthetic-workspace", FULL, user_id="synthetic-user"),
                }
            ],
            transport=bridge,
        )
        account = pool.accounts()[0]
        bridge_key["value"] = account.account_key
        h.deps.source = pool
        h.api.source = pool
        h.add_credit(account, expires_in=HOUR)
        h.scan()
        key = h.key(account)
        h.deps.settings = dataclasses.replace(
            h.settings, mask_labels=False, excluded_accounts=(account.label,)
        )
        h.clock.set(h.store.get_slot(key, "primary")["fire_at"])
        h.run_fire()
        self.assertEqual(h.backend.posts, [], "the production source bypassed the exclusion")

    def test_r10_the_same_setup_without_the_exclusion_still_spends(self):
        """Control: the three tests above must not pass because nothing works."""
        h = Harness(self)
        account, key = h.armed(expires_in=HOUR)
        h.source.full_label = lambda account_key: FULL
        h.deps.settings = dataclasses.replace(h.settings, mask_labels=False)
        h.clock.set(h.store.get_slot(key, "primary")["fire_at"])
        h.run_fire()
        self.assertEqual(len(h.backend.posts), 1, "an included account must still be rescued")


class R4RowLevelReadability(unittest.TestCase):
    """R4 — readability must include the rows, not only the container."""

    def test_r4_invalid_credit_row_readback_must_not_confirm_consumption(self):
        """The reviewer's counterexample: a 200 whose only row has no id."""
        h = Harness(self)
        account, key = h.armed(expires_in=HOUR)
        h.clock.set(h.store.get_slot(key, "primary")["fire_at"])

        def on_post(*args):
            h.backend.on_list = lambda account_key: {
                "credits": [{"status": "available", "expires_at": h.clock.now() + 300}],
                "available_count": 1,
            }
            return {"code": "reset", "windows_reset": 1}

        h.backend.on_post = on_post
        result = h.run_fire()
        self.assertFalse(result.results[0].confirmed, "a dropped row was used as absence proof")
        self.assertEqual(h.backend.only(account.account_key).status, "available")

    def test_r4_invalid_row_reconciliation_must_not_settle_posted_credit(self):
        """The reviewer's counterexample: ``{'credits': [None]}`` in a scan."""
        h = Harness(self)
        account, key = h.armed(expires_in=HOUR)
        h.store.begin_redeem(key, h.clock.now())
        h.backend.on_list = lambda account_key: {"credits": [None], "available_count": 1}
        h.scan()
        row = h.store.get_credit(key)
        self.assertNotEqual(row["status"], "redeemed", "an unreadable row settled the credit")
        self.assertEqual(h.backend.only(account.account_key).status, "available")

    def test_r4_one_bad_row_among_good_ones_still_proves_nothing(self):
        """Variant: a partly parsed list is not a readable list.

        The good row must still be parsed (the scan can schedule it), but the
        absence of *another* credit is unproven, so a posted row stays pending.
        """
        h = Harness(self)
        account, key = h.armed(expires_in=HOUR)
        other = h.add_credit(account, "credit-2", expires_in=HOUR)
        h.store.begin_redeem(key, h.clock.now())
        h.backend.on_list = lambda account_key: {
            "credits": [other.payload(), None],
            "available_count": 2,
        }
        h.scan()
        row = h.store.get_credit(key)
        self.assertNotEqual(row["status"], "redeemed", "a partly parsed list confirmed absence")

    def test_r4_a_valid_empty_list_still_proves_absence(self):
        """Control: the fix must not turn every empty list into uncertainty."""
        h = Harness(self)
        account, key = h.armed(expires_in=HOUR)
        h.clock.set(h.store.get_slot(key, "primary")["fire_at"])

        def on_post(*args):
            h.backend.on_list = lambda account_key: {"credits": [], "available_count": 0}
            return {"code": "reset", "windows_reset": 1}

        h.backend.on_post = on_post
        result = h.run_fire()
        self.assertTrue(result.results[0].confirmed, "a valid empty list must confirm absence")
        self.assertEqual(h.store.get_credit(key)["status"], "redeemed")


class R16OutboxKeepsNoFullLabel(unittest.TestCase):
    """R16 — the outbox keeps the masked label or a reference, never the full one."""

    def _db_bytes(self, h: Harness) -> bytes:
        """The whole SQLite file, WAL and shared memory included."""
        data = b""
        for name in ("data.db", "data.db-wal", "data.db-shm"):
            path = h.data_dir / name
            if path.exists():
                data += path.read_bytes()
        return data

    def test_r16_unmasked_notice_must_not_persist_full_email(self):
        """The reviewer's counterexample: the full address in ``params_json``."""
        h = Harness(self)
        account, key = h.armed(expires_in=HOUR)
        h.source.full_label = lambda account_key: FULL
        h.deps.settings = dataclasses.replace(h.settings, mask_labels=False)
        h.clock.set(h.store.get_slot(key, "primary")["fire_at"])
        h.run_fire()
        stored = "\n".join(str(row["params_json"]) for row in h.store.list_notices())
        self.assertTrue(stored, "no notice was queued")
        self.assertNotIn(FULL, stored, "the full label was serialized into the outbox")

    def test_r16_the_whole_database_file_holds_no_full_label(self):
        """Variant: read the SQLite bytes, then prove the send still unmasks.

        Storing only the masked label is easy if the unmask feature is dropped;
        this variant proves both halves: nothing full on disk, and the notice
        still shows the full label when it is rendered at send time.
        """
        h = Harness(self)
        account, key = h.armed(expires_in=HOUR, label=MASKED)
        h.source.full_label = lambda account_key: FULL
        h.deps.settings = dataclasses.replace(h.settings, mask_labels=False)
        sender = _Sender()
        notifier = Notifier(
            sender, target="telegram", language="en", mask_labels=False, store=h.store
        )
        notifier.resolve_label = _resolver(MASKED)
        h.deps.notifier = notifier
        h.clock.set(h.store.get_slot(key, "primary")["fire_at"])
        h.run_fire()
        blob = self._db_bytes(h)
        self.assertFalse(FULL.encode() in blob, "the full label reached the database")
        rows = h.store.list_notices()
        self.assertTrue(rows, "no notice was queued")
        payload = json.loads(rows[0]["params_json"])
        self.assertEqual(payload.get("label"), MASKED, "the outbox did not keep the masked label")
        self.assertTrue(sender.bodies(), "the queued notice was not sent")
        self.assertIn(FULL, sender.bodies()[-1], "the send-time render lost the unmask")

    def test_r16_turning_unmasking_off_later_leaves_nothing_full_to_flush(self):
        """Variant: mask off at queue time, mask on at flush time."""
        h = Harness(self)
        account, key = h.armed(expires_in=HOUR, label=MASKED)
        h.source.full_label = lambda account_key: FULL
        h.deps.settings = dataclasses.replace(h.settings, mask_labels=False)
        h.deps.notifier = _NoFlush()
        h.clock.set(h.store.get_slot(key, "primary")["fire_at"])
        h.run_fire()
        rows = h.store.list_notices()
        self.assertTrue(rows, "no notice was queued")
        queued = "\n".join(str(row["params_json"]) for row in rows)
        self.assertNotIn(
            FULL, queued, "the queued row kept the full label after the setting was off"
        )
        sender = _Sender()
        notifier = Notifier(
            sender, target="telegram", language="en", mask_labels=True, store=h.store
        )
        notifier.resolve_label = _resolver(MASKED)
        notifier.flush()
        self.assertTrue(sender.bodies(), "the queued notice was not sent")
        self.assertNotIn(
            FULL, sender.bodies()[-1], "a queued row unmasked after the setting was off"
        )
        self.assertIn(MASKED, sender.bodies()[-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
