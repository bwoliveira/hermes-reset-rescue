"""Cross-layer notice pipeline — every kind a flow can produce must be sent.

This file guards a bug class: the flows queued parameters the
catalog does not declare, ``messages.render`` refused them, and the notice was
dropped as ``bad_notice`` / ``empty_notice`` **after** the flow tests had
already counted it in the fake notifier. Nothing here is faked except the
subprocess: the real ``Planner`` / ``Fire`` build the parameters with
``fire.notice_params``, the real ``Store`` persists them in the outbox, the
real ``Notifier`` renders them through the real catalog, and the real
``CliRunner`` builds the argv ``hermes send`` would receive.

Two layers:

* :class:`FlowNoticePipelineTests` drives the real flows (scan, fire, remind,
  quota check) and asserts exactly one ``hermes send`` per queued notice, with
  the account label (masked or not) and the times in the configured zone.
* :class:`CatalogKindPipelineTests` walks every kind of the catalog with the
  real parameter builder, so a kind no flow happens to produce today is still
  proven to render and to reach the argv.

The zone is Hermes' configured one (``HERMES_TIMEZONE``, D-011), not the host's:
each notice is checked against the Tokyo rendering and against the absence of
the host-zone rendering.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from reset_rescue import fire, messages
from reset_rescue.cli_runner import CliRunner
from reset_rescue.fire import ROLE_REMIND
from reset_rescue.notify import Notifier
from reset_rescue.settings import parse_settings
from tests.flows.support import HOUR, MINUTE, Harness

TARGET = "telegram"
LAUNCHER = "/usr/local/bin/hermes"
TOKYO = ZoneInfo("Asia/Tokyo")
FULL_LABEL = "bea.doe@example.com"
MASKED_LABEL = "b***@e***.com"
SENT_OK = json.dumps({"success": True, "message_id": 1, "note": "sent"})

#: A fixed instant for the catalog-level layer (the flows use their own clock).
EPOCH = 1_800_000_000.0


def _unmask(masked: str) -> str:
    """The D-013 unmask as the notifier sees it: masked label → full label (R16).

    The outbox stores only ``MASKED_LABEL``, so this is the in-memory step that
    turns it back into ``FULL_LABEL`` at send time. It answers ``""`` for any
    other label, which is what the production resolver does when it cannot tell
    which account a row belongs to.
    """
    return FULL_LABEL if str(masked) == MASKED_LABEL else ""


class _Recorder:
    """The subprocess the plugin would start: records the argv, never runs it."""

    def __init__(self, *, stdout: str = SENT_OK, returncode: int = 0) -> None:
        self.calls: list[dict] = []
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, argv, env, timeout):
        self.calls.append(
            {"argv": [str(part) for part in argv], "env": dict(env), "timeout": float(timeout)}
        )
        return subprocess.CompletedProcess(
            list(argv), self.returncode, stdout=self.stdout, stderr=""
        )

    def bodies(self) -> list[str]:
        return [call["argv"][-1] for call in self.calls]


@contextlib.contextmanager
def _configured_zone(name: str = "Asia/Tokyo"):
    """Hermes' configured zone, as the plugin reads it (D-011)."""
    with mock.patch.dict("os.environ", {"HERMES_TIMEZONE": name}, clear=False):
        zone = messages.local_zone()
        assert str(zone) == name, f"local_zone() is {zone!r}, not {name}"
        yield zone


def _time_text(
    renderer: messages.TimeRenderer, key: str, stamps, epoch: float, language: str
) -> str:
    if key in stamps:
        return renderer.stamp(epoch, language)
    return renderer.time(epoch, language)


class _PipelineCase(unittest.TestCase):
    """Shared wiring and assertions for both layers."""

    def setUp(self) -> None:
        # The host zone, captured before any patch: the assertion below is that
        # a notice uses the *configured* zone instead of this one.
        self.host_zone = messages.local_zone()

    # -- wiring ------------------------------------------------------------
    def notifier(self, *, mask_labels: bool = True, language: str = "en", store=None, clock=None):
        recorder = _Recorder()
        runner = CliRunner(launcher=LAUNCHER, runner=recorder)
        notifier = Notifier(
            runner,
            target=TARGET,
            language=language,
            mask_labels=mask_labels,
            store=store,
            clock=clock,
            # R16: the outbox keeps the masked label, so the unmask is resolved
            # here, in memory, at send time. The production composition root
            # wires the same contract from the live source (``host.label_resolver``).
            resolve_label=_unmask,
        )
        return notifier, recorder

    def pipeline(self, *, mask_labels: bool = True, language: str = "en"):
        """A flows harness whose notifier is the real one over a fake process."""
        h = Harness(self)
        notifier, recorder = self.notifier(
            mask_labels=mask_labels, language=language, store=h.store, clock=h.clock.now
        )
        h.deps.notifier = notifier
        return h, recorder

    def armed(self, h, *, label: str = FULL_LABEL):
        account = h.add_account(label)
        h.add_credit(account, expires_in=30 * HOUR)
        self.assertEqual(h.scan().exit_code, 0)
        return account

    # -- assertions --------------------------------------------------------
    def assert_times_in_the_configured_zone(
        self, kind: str, params, text: str, language: str
    ) -> int:
        canonical = messages.KIND_ALIASES.get(kind, kind)
        stamps = messages.STAMP_KEYS.get(canonical, frozenset())
        configured = messages.TimeRenderer(tz=TOKYO)
        host = messages.TimeRenderer(tz=self.host_zone) if self.host_zone else None
        checked = 0
        for key in messages.PARAMS[canonical]:
            if key not in messages.TIME_KEYS:
                continue
            raw = params.get(key)
            if raw in (None, ""):
                continue
            expected = _time_text(configured, key, stamps, float(raw), language)
            self.assertIn(expected, text, f"{kind}.{key} is not in the configured zone")
            checked += 1
            if host is not None and str(self.host_zone) != "Asia/Tokyo":
                other = _time_text(host, key, stamps, float(raw), language)
                if other != expected:
                    self.assertNotIn(other, text, f"{kind}.{key} used the host zone")
        return checked

    def assert_send_argv(
        self, recorder: _Recorder, *, mask_labels: bool, language: str = "en"
    ) -> None:
        self.assertEqual(len(recorder.calls), 1, "one notice, one send")
        argv = recorder.calls[0]["argv"]
        self.assertEqual(len(argv), 6, argv)
        self.assertEqual(argv[:5], [LAUNCHER, "send", "-t", TARGET, "--json"], argv)
        text = argv[5]
        self.assertTrue(text.strip(), "the notice body is empty")
        self.assertNotIn("{", text, "a placeholder was left in the notice")
        self.assertNotIn("  ", text, "a field left a hole")
        if mask_labels:
            self.assertIn(MASKED_LABEL, text)
            self.assertNotIn(FULL_LABEL, text)
        else:
            self.assertIn(FULL_LABEL, text)

    def assert_every_notice_sent(
        self,
        h,
        recorder: _Recorder,
        *,
        language: str = "en",
        mask_labels: bool = True,
        expect: tuple[str, ...] = (),
    ) -> list[dict]:
        rows = h.store.list_notices()
        self.assertTrue(rows, "no notice was queued")
        kinds = {str(row["message_key"] or row["kind"]) for row in rows}
        for kind in expect:
            self.assertIn(kind, kinds, f"{kind} was never queued")
        self.assertEqual(
            [row["id"] for row in rows if row["state"] != "sent"],
            [],
            "a notice was skipped or failed instead of sent",
        )
        self.assertEqual(len(recorder.calls), len(rows), "exactly one send per notice")
        for row, call in zip(rows, recorder.calls, strict=True):
            kind = str(row["message_key"] or row["kind"])
            params = json.loads(row["params_json"])
            argv = call["argv"]
            self.assertEqual(len(argv), 6, argv)
            self.assertEqual(argv[:5], [LAUNCHER, "send", "-t", TARGET, "--json"], argv)
            text = argv[5]
            self.assertTrue(text.strip(), f"{kind}: empty body")
            self.assertNotIn("{", text, f"{kind}: a placeholder was left")
            self.assertNotIn("  ", text, f"{kind}: a field left a hole")
            self.assertEqual(params.get("label"), MASKED_LABEL, kind)
            if mask_labels:
                self.assertIn(MASKED_LABEL, text, kind)
                self.assertNotIn(FULL_LABEL, text, kind)
            else:
                self.assertIn(FULL_LABEL, text, kind)
            checked = self.assert_times_in_the_configured_zone(kind, params, text, language)
            self.assertGreater(checked, 0, f"{kind} carries no time to check")
        return rows


class FlowNoticePipelineTests(_PipelineCase):
    """Every notice a real flow queues renders and reaches the send argv."""

    # -- one scenario per notice kind the flows produce ---------------------
    def scheduled(self):
        """The arm-time notice (§5's reminder sentence, cut kind)."""
        h, recorder = self.pipeline()
        account = self.armed(h)
        return h, recorder, account, "scheduled"

    def reminder(self):
        h, recorder = self.pipeline()
        account = self.armed(h)
        slot = h.slot(account, "reminder1")
        self.assertIsNotNone(slot, "no reminder slot was armed")
        h.advance_to(float(slot["fire_at"]) + 5)
        h.fire().run(role=ROLE_REMIND)
        return h, recorder, account, "reminder"

    def success(self):
        h, recorder = self.pipeline()
        account = self.armed(h)
        h.advance_to(float(h.slot(account, "primary")["fire_at"]))
        h.run_fire("primary")
        return h, recorder, account, "success"

    def info_already_redeemed(self):
        h, recorder = self.pipeline()
        account = self.armed(h)
        credit = h.backend.only(account.account_key)
        credit.status = "redeemed"
        credit.redeemed_at = h.clock.now()
        h.advance_to(float(h.slot(account, "primary")["fire_at"]))
        h.run_fire("primary")
        return h, recorder, account, "info"

    def info_nothing_to_reset(self):
        h, recorder = self.pipeline()
        account = self.armed(h)
        h.backend.on_post = lambda *_: {"code": "nothing_to_reset"}
        h.advance_to(float(h.slot(account, "primary")["fire_at"]))
        h.run_fire("primary")
        return h, recorder, account, "info"

    def final_failure(self):
        h, recorder = self.pipeline()
        account = self.armed(h)
        h.backend.on_post = lambda *_: {"code": "rate_limit_reset_ineligible"}
        h.advance_to(float(h.slot(account, "primary")["fire_at"]))
        h.run_fire("primary")
        return h, recorder, account, "final_failure"

    def retry_pending(self):
        h, recorder = self.pipeline()
        account = self.armed(h)
        h.backend.fail_list[account.account_key] = ConnectionError("down")
        h.advance_to(float(h.slot(account, "primary")["fire_at"]))
        h.run_fire("primary")
        return h, recorder, account, "retry_pending"

    def account_error(self):
        """An account error *transition* — the only thing that queues this kind.

        ``Planner._account_error`` compares the code it is given with the one
        already stored, so the scenario has to start from a different stored
        code. The builder itself is the real one: the same call the scan makes
        when an account's list GET fails.
        """
        h, recorder = self.pipeline()
        account = self.armed(h)
        h.store.set_account_error(account.account_key, "auth_expired", h.clock.now())
        h.backend.fail_list[account.account_key] = ConnectionError("down")
        h.planner()._account_error(account, "network", h.clock.now())
        fire.flush_notices(h.deps, h.store)
        return h, recorder, account, "account_error"

    def missed(self):
        h, recorder = self.pipeline()
        account = self.armed(h)
        h.advance_to(float(h.slot(account, "backup")["fire_at"]) + 16 * MINUTE)
        h.scan()
        return h, recorder, account, "missed"

    def early_rescue(self):
        h, recorder = self.pipeline()
        h.deps.settings = parse_settings(
            {
                "early_redeem_enabled": True,
                "early_redeem_remaining_pct": 0,
                "lead_minutes": 10,
                "backup_lead_minutes": 4,
            }
        )
        account = self.armed(h)
        h.backend.usage_body = {
            "rate_limit": {
                "primary_window": {"limit_window_seconds": 604800, "used_percent": 100.0},
                "secondary_window": {"limit_window_seconds": 18000, "used_percent": 0.0},
            }
        }
        h.planner().quota_check()
        return h, recorder, account, "success"

    SCENARIOS = (
        "scheduled",
        "reminder",
        "success",
        "info_already_redeemed",
        "info_nothing_to_reset",
        "final_failure",
        "retry_pending",
        "account_error",
        "missed",
        "early_rescue",
    )

    def test_every_flow_notice_reaches_the_send_argv(self):
        for name in self.SCENARIOS:
            with self.subTest(scenario=name):
                with _configured_zone():
                    h, recorder, account, kind = getattr(self, name)()
                    self.assert_every_notice_sent(h, recorder, expect=(kind,))

    def test_the_full_label_is_printed_when_masking_is_off(self):
        with _configured_zone():
            h, recorder = self.pipeline(mask_labels=False)
            account = self.armed(h)
            h.advance_to(float(h.slot(account, "primary")["fire_at"]))
            h.run_fire("primary")
            rows = self.assert_every_notice_sent(
                h, recorder, mask_labels=False, expect=("success",)
            )
            self.assertTrue(any("success" == str(row["message_key"]) for row in rows))

    def test_the_portuguese_bundle_reaches_the_argv(self):
        with _configured_zone():
            h, recorder = self.pipeline(language="pt-BR")
            account = self.armed(h)
            h.advance_to(float(h.slot(account, "primary")["fire_at"]))
            h.run_fire("primary")
            self.assert_every_notice_sent(h, recorder, language="pt-BR", expect=("success",))
            self.assertIn("Salvo!", recorder.bodies()[-1])

    def test_a_network_account_error_never_reads_as_an_expired_sign_in(self):
        """The gap left by one fixed phrase for every account error."""
        with _configured_zone():
            h, recorder, account, _ = self.account_error()
            body = recorder.bodies()[-1]
            self.assertNotIn("sign-in expired", body)
            self.assertNotIn("login expirado", body)
            self.assertIn("couldn't reach the account", body)

    def test_a_dry_run_notice_keeps_the_prefix_and_still_sends_once(self):
        notifier, recorder = self.notifier()
        with _configured_zone():
            result = notifier.notify(
                "success",
                fire.notice_params(
                    "success", label=FULL_LABEL, redeemed_at=EPOCH, expires_at=EPOCH + 600.0
                ),
                dry_run=True,
            )
            self.assertTrue(result.delivered, result)
            self.assertFalse(result.skipped, result)
            self.assert_send_argv(recorder, mask_labels=True)
            self.assertTrue(recorder.bodies()[0].startswith("🧪 [Dry run] "))

    def test_a_scan_queues_the_account_error_notice(self):
        """The scan path queues ``account_error`` on a none → error transition.

        ``Planner._scan_locked`` records only *that* the account failed and
        ``Planner._account_error`` writes the code after reading the previous
        one, so the comparison sees the old value and the transition notice is
        reachable from a scan. Before that ordering fix the write happened
        first and the comparison always saw the value it was about to compare,
        so this test used to be pinned as a known failure.
        """
        with _configured_zone():
            h, recorder = self.pipeline()
            account = self.armed(h)
            h.store.set_account_error(account.account_key, None, h.clock.now())
            h.backend.fail_list[account.account_key] = ConnectionError("down")
            h.scan()
            kinds = {str(row["message_key"]) for row in h.store.list_notices()}
            self.assertIn("account_error", kinds)


class CatalogKindPipelineTests(_PipelineCase):
    """Every kind of the catalog, built by the real parameter builder."""

    def params(self, kind: str, *, language: str = "en", reason_code: str = "auth_expired") -> dict:
        params = fire.notice_params(
            kind,
            label=FULL_LABEL,
            expires_at=EPOCH + 3600.0,
            redeemed_at=EPOCH,
            retry_at=EPOCH + 600.0,
            fire_at=EPOCH + 3000.0,
            in_hours="24h",
            reason_code=reason_code,
            language=language,
        )
        # ``notice_params`` returns only the parameters a template *shows*;
        # ``account_error``'s category comes from ``reason_code``, which the
        # renderer accepts as the source of the derived ``{reason}``.
        if messages.DERIVED.get(messages.KIND_ALIASES.get(kind, kind)):
            params["reason_code"] = reason_code
        return params

    def test_every_catalog_kind_renders_and_reaches_the_argv(self):
        for kind in messages.KINDS:
            for language in messages.LANGUAGES:
                for mask in (True, False):
                    with self.subTest(kind=kind, language=language, mask=mask):
                        with _configured_zone():
                            notifier, recorder = self.notifier(mask_labels=mask, language=language)
                            params = self.params(kind, language=language)
                            result = notifier.notify(kind, params)
                            self.assertTrue(result.delivered, f"{kind}: {result}")
                            self.assertFalse(result.skipped, f"{kind}: {result}")
                            self.assertIsNone(result.error_code, f"{kind}: {result}")
                            self.assert_send_argv(recorder, mask_labels=mask, language=language)
                            checked = self.assert_times_in_the_configured_zone(
                                kind, params, recorder.bodies()[0], language
                            )
                            self.assertGreater(checked, 0, f"{kind} carries no time")

    def test_the_account_error_names_the_right_category(self):
        """auth, network, rate_limited and unknown each get their own phrase."""
        cases = {
            "auth_expired": ("sign-in expired", "login expirado"),
            "network": ("couldn't reach the account", "não deu para falar com a conta"),
            "transient": ("rate-limiting", "limitando"),
            "weird_code": ("unexpected error", "erro inesperado"),
        }
        for code, (en, pt) in cases.items():
            with self.subTest(code=code):
                with _configured_zone():
                    notifier, recorder = self.notifier()
                    result = notifier.notify(
                        "account_error", self.params("account_error", reason_code=code)
                    )
                    self.assertTrue(result.delivered, result)
                    body = recorder.bodies()[0]
                    self.assertIn(en, body, code)
                    self.assertIn(en, messages.account_error_text(code, "en"))
                with _configured_zone():
                    notifier, recorder = self.notifier(language="pt-BR")
                    notifier.notify(
                        "account_error",
                        self.params("account_error", language="pt-BR", reason_code=code),
                    )
                    self.assertIn(pt, recorder.bodies()[0], code)
                    self.assertIn(pt, messages.account_error_text(code, "pt-BR"))

    def test_the_four_account_error_categories_are_distinct(self):
        """One phrase for every category is exactly the bug this test guards against."""
        codes = ("auth_expired", "network", "transient", "weird_code")
        for language in messages.LANGUAGES:
            phrases = [messages.account_error_text(code, language) for code in codes]
            self.assertEqual(len(set(phrases)), len(codes), phrases)
            for phrase in phrases:
                self.assertTrue(phrase.strip(), language)
                self.assertNotIn("{", phrase)
        self.assertNotIn("sign-in", messages.account_error_text("network", "en"))
        self.assertNotIn("login", messages.account_error_text("network", "pt-BR"))

    def test_a_missing_code_never_leaves_a_hole(self):
        """The flows queue no code; the sentence still reads as a sentence."""
        for language in messages.LANGUAGES:
            text = messages.render(
                "account_error",
                {"label": FULL_LABEL, "expires_at": EPOCH + 60.0},
                language=language,
                tz=TOKYO,
            )
            self.assertNotIn("{", text)
            self.assertNotIn("  ", text)
            self.assertIn(messages.account_error_text("", language), text)

    def test_every_flow_reason_code_has_a_phrase(self):
        """The six codes that once rendered as "unexpected error"."""
        codes = (
            "expired_before_redemption",
            "unavailable_unknown",
            "account_missing",
            "already_redeemed",
            "vanished",
            "nothing_to_reset",
        )
        unknown = messages.reason_text("unknown", "en")
        for code in codes:
            with self.subTest(code=code):
                en = messages.reason_text(code, "en")
                pt = messages.reason_text(code, "pt-BR")
                self.assertTrue(en and pt, code)
                self.assertNotEqual(en, unknown, f"{code} still renders as unknown")
                self.assertNotEqual(en, pt, code)
                self.assertNotIn("_", en, code)
                with _configured_zone():
                    notifier, recorder = self.notifier()
                    result = notifier.notify(
                        "final_failure", self.params("final_failure", reason_code=code)
                    )
                    self.assertTrue(result.delivered, result)
                    self.assertIn(en, recorder.bodies()[0], code)

    def test_no_catalog_kind_is_refused_by_the_renderer(self):
        """A kind whose params the catalog refuses is a notice that never sends."""
        self.assertEqual(messages.validate_catalog(), [])
        for kind in messages.KINDS:
            for language in messages.LANGUAGES:
                text = messages.render(
                    kind, self.params(kind, language=language), language=language, tz=TOKYO
                )
                self.assertTrue(text.strip(), f"{kind}/{language}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
