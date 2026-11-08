"""T-20 / T-51 / T-74 — the notice catalog: en + pt-BR, short, no leaks, local times.

The catalog is the only thing a user reads, so every assertion below is about
the text a human actually sees. The §5 examples are pinned verbatim: if a
template changes, this file fails and the change has to be deliberate.
"""

from __future__ import annotations

import re
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from reset_rescue import messages

SAO_PAULO = ZoneInfo("America/Sao_Paulo")
UTC = timezone.utc


def _epoch(text: str, tz=SAO_PAULO) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=tz).timestamp()


def _params_for(kind: str) -> dict:
    """Every declared parameter of one kind, with a realistic value.

    Deliberately built from ``messages.PARAMS``: a kind that grows a parameter
    without a test value fails here instead of rendering an empty phrase.
    """
    values = {
        "label": "b***@gmail.com",
        "expires_at": _epoch("2026-11-09T21:40:00"),
        "fire_at": _epoch("2026-11-09T21:30:00"),
        "retry_at": _epoch("2026-11-09T21:36:00"),
        "redeemed_at": _epoch("2026-11-09T21:30:00"),
        "in_hours": "24h",
        "reason": "sign-in expired",
    }
    return {name: values[name] for name in messages.PARAMS[kind]}


class CatalogTests(unittest.TestCase):
    def test_catalog_is_consistent(self):
        self.assertEqual(messages.validate_catalog(), [])

    def test_the_seven_kinds_of_the_simplification_pass(self):
        self.assertEqual(
            set(messages.KINDS),
            {
                "reminder",
                "success",
                "early_redeem",
                "info_nothing_to_reset",
                "retry_pending",
                "final_failure",
                "account_error",
            },
        )

    def test_every_kind_has_both_languages(self):
        for kind in messages.KINDS:
            for language in messages.LANGUAGES:
                self.assertTrue(messages.template_for(kind, language).strip(), f"{kind}/{language}")

    def test_every_kind_renders_in_both_languages(self):
        for kind in messages.KINDS:
            params = _params_for(kind)
            for language in messages.LANGUAGES:
                text = messages.render(kind, params, language=language, tz=SAO_PAULO)
                self.assertTrue(text.strip(), f"{kind}/{language}")
                self.assertNotIn("{", text, f"{kind}/{language} left a placeholder")
                self.assertNotIn("[", text, f"{kind}/{language} left a group")
                self.assertNotIn("  ", text, f"{kind}/{language} has a double space")

    def test_notices_are_one_line_and_short(self):
        """D-012/§5: a notice is one friendly line, not a report."""
        for kind in messages.KINDS:
            params = _params_for(kind)
            for language in messages.LANGUAGES:
                text = messages.render(kind, params, language=language, tz=SAO_PAULO)
                self.assertLessEqual(len(text), 220, f"{kind}/{language}: {text}")
                self.assertNotIn("\n", text, f"{kind}/{language}")

    def test_every_kind_has_an_emoji(self):
        for kind in messages.KINDS:
            text = messages.template_for(kind, "en")
            self.assertTrue(any(ord(ch) > 0x2100 for ch in text), kind)

    def test_english_and_portuguese_differ(self):
        for kind in messages.KINDS:
            self.assertNotEqual(
                messages.template_for(kind, "en"), messages.template_for(kind, "pt-BR"), kind
            )

    def test_no_internal_vocabulary_in_the_templates(self):
        """Placeholders are stripped first: `{expires_at}` is a field name, not
        vocabulary the user ever reads."""
        banned = (
            "credit_key",
            "account_key",
            "plugin_data_dir",
            "HERMES_HOME",
            "auth.json",
            "http",
            "sqlite",
            "job_id",
            "uuid",
            "POST",
            "expires_at",
            "fire_at",
            "retry_at",
            "redeemed_at",
            "None",
        )
        for kind in messages.KINDS:
            for language in messages.LANGUAGES:
                text = re.sub(r"\{[a-z_]+\}", "", messages.template_for(kind, language))
                for needle in banned:
                    self.assertNotIn(needle, text, f"{kind}/{language}: {needle}")

    def test_no_notice_shows_a_quota_percentage(self):
        """D-010: quota percentages never appear in a notice."""
        for kind in messages.KINDS:
            for language in messages.LANGUAGES:
                text = messages.render(kind, _params_for(kind), language=language, tz=SAO_PAULO)
                self.assertNotIn("%", text, f"{kind}/{language}: {text}")

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(messages.NoticeError):
            messages.render("nope")

    def test_extra_parameter_is_refused(self):
        """A caller cannot smuggle a value (an id, a path) into a notice."""
        with self.assertRaises(messages.NoticeError):
            messages.render("success", {"label": "x", "credit_id": "abc"})

    def test_aliases_map_to_a_real_kind(self):
        for alias, target in messages.KIND_ALIASES.items():
            self.assertIn(target, messages.KINDS, alias)
            self.assertTrue(messages.render(alias, _params_for(target)).strip(), alias)

    def test_the_scheduled_notice_is_cut_and_maps_to_the_reminder(self):
        """§5 cut standalone `scheduled`; the alias keeps old callers working."""
        self.assertEqual(messages.KIND_ALIASES["scheduled"], "reminder")
        text = messages.render("scheduled", _params_for("reminder"))
        self.assertIn("rescue time:", text)


class PinnedExampleTests(unittest.TestCase):
    """The §5 examples, verbatim (with a fixed zone and a fixed "now")."""

    def _render(self, kind: str, language: str, **overrides) -> str:
        params = _params_for(kind)
        params.update(overrides)
        return messages.render(kind, params, language=language, tz=SAO_PAULO)

    def test_reminder_english(self):
        self.assertEqual(
            self._render("reminder", "en"),
            "⏰ Reminder: your Codex reset for account b***@gmail.com expires Nov 9, 21:40 "
            "(in 24h) — rescue time: 21:30.",
        )

    def test_reminder_portuguese(self):
        self.assertEqual(
            self._render("reminder", "pt-BR"),
            "⏰ Lembrete: seu reset do Codex da conta b***@gmail.com vence 09/11 às "
            "21:40 (em 24h) — horário do resgate: 21:30.",
        )

    def test_success_english(self):
        self.assertEqual(
            self._render("success", "en"),
            "✅ Rescued! Codex reset for account b***@gmail.com redeemed at 21:30 "
            "(was expiring 21:40).",
        )

    def test_success_portuguese(self):
        self.assertEqual(
            self._render("success", "pt-BR"),
            "✅ Salvo! Reset do Codex da conta b***@gmail.com resgatado às 21:30 "
            "(vencia às 21:40).",
        )

    def test_early_redeem_english(self):
        text = self._render("early_redeem", "en")
        self.assertIn("⚡ Early redemption", text)
        self.assertIn("weekly quota threshold reached", text)

    def test_info_english(self):
        self.assertEqual(
            self._render("info_nothing_to_reset", "en"),
            "ℹ️ Codex reset for account b***@gmail.com (expiring 21:40) wasn't needed — "
            "usage windows were already clear.",
        )

    def test_retry_pending_english(self):
        self.assertEqual(
            self._render("retry_pending", "en"),
            "⚠️ Rescue retry pending for account b***@gmail.com (expires 21:40) — "
            "temporary error, next try at 21:36.",
        )

    def test_final_failure_english(self):
        self.assertEqual(
            self._render("final_failure", "en"),
            "❌ Couldn't rescue Codex reset for account b***@gmail.com (expires 21:40): "
            "sign-in expired — sign in again if needed, or redeem it manually.",
        )

    def test_dry_run_prefix_english(self):
        text = messages.render(
            "success", _params_for("success"), language="en", tz=SAO_PAULO, dry_run=True
        )
        self.assertTrue(text.startswith("🧪 [Dry run] ✅ Rescued!"))

    def test_dry_run_prefix_portuguese(self):
        text = messages.render(
            "success", _params_for("success"), language="pt-BR", tz=SAO_PAULO, dry_run=True
        )
        self.assertTrue(text.startswith("🧪 [Simulação] ✅ Salvo!"))


class TimeTests(unittest.TestCase):
    def test_english_stamp_uses_month_day_and_time(self):
        stamp = messages.TimeRenderer(tz=SAO_PAULO).stamp(_epoch("2026-11-09T21:40:00"), "en")
        self.assertEqual(stamp, "Nov 9, 21:40")

    def test_portuguese_stamp_uses_day_month(self):
        stamp = messages.TimeRenderer(tz=SAO_PAULO).stamp(_epoch("2026-11-09T21:40:00"), "pt-BR")
        self.assertEqual(stamp, "09/11 às 21:40")

    def test_a_stamp_always_carries_the_date(self):
        """§5's reminder reads "expires Nov 9, 21:40" even on the 9th: a bare
        time would be ambiguous for a 24 h heads-up."""
        renderer = messages.TimeRenderer(tz=SAO_PAULO)
        self.assertEqual(renderer.stamp(_epoch("2026-11-09T21:40:00"), "en"), "Nov 9, 21:40")
        self.assertEqual(renderer.stamp(_epoch("2026-11-09T21:40:00"), "pt-BR"), "09/11 às 21:40")

    def test_a_bare_time_is_available_for_a_sentence_that_already_dates(self):
        renderer = messages.TimeRenderer(tz=SAO_PAULO)
        self.assertEqual(renderer.time(_epoch("2026-11-09T21:40:00")), "21:40")

    def test_the_zone_is_the_one_passed_in(self):
        """D-011: the same instant reads differently in two zones, as it must."""
        instant = _epoch("2026-11-09T21:40:00")
        self.assertEqual(messages.TimeRenderer(tz=SAO_PAULO).time(instant), "21:40")
        self.assertEqual(messages.TimeRenderer(tz=UTC).time(instant), "00:40")

    def test_local_zone_falls_back_to_the_host(self):
        self.assertIsNotNone(messages.local_zone())

    def test_local_zone_reads_the_hermes_config(self):
        """The profile's `timezone` is honoured (D-011), through the public API."""
        import unittest.mock as mock

        with mock.patch.dict("os.environ", {"HERMES_TIMEZONE": "America/Sao_Paulo"}, clear=False):
            zone = messages.local_zone()
        self.assertEqual(str(zone), "America/Sao_Paulo")

    def test_an_invalid_zone_falls_back_instead_of_raising(self):
        import unittest.mock as mock

        with mock.patch.dict("os.environ", {"HERMES_TIMEZONE": "Mars/Olympus"}, clear=False):
            self.assertIsNotNone(messages.local_zone())


class LanguageTests(unittest.TestCase):
    def test_explicit_value_wins(self):
        self.assertEqual(messages.resolve_language("pt-BR", "en"), "pt-BR")
        self.assertEqual(messages.resolve_language("en", "pt-BR"), "en")

    def test_auto_follows_the_host(self):
        self.assertEqual(messages.resolve_language("auto", "pt"), "pt-BR")
        self.assertEqual(messages.resolve_language("auto", "pt-BR"), "pt-BR")
        self.assertEqual(messages.resolve_language("auto", "pt_BR"), "pt-BR")
        self.assertEqual(messages.resolve_language("auto", "en"), "en")
        self.assertEqual(messages.resolve_language("auto", None), "en")

    def test_an_unknown_language_argument_falls_back_to_english(self):
        text = messages.render("success", _params_for("success"), language="de")
        self.assertEqual(text, messages.render("success", _params_for("success"), language="en"))


class FragmentTests(unittest.TestCase):
    def test_reason_codes_become_friendly_phrases(self):
        for code in (
            "auth_expired",
            "network",
            "timeout",
            "http_5xx",
            "unconfirmed",
            "lock_timeout",
            "launcher_not_found",
        ):
            en = messages.reason_text(code, "en")
            pt = messages.reason_text(code, "pt-BR")
            self.assertTrue(en and pt, code)
            self.assertNotEqual(en, pt, code)
            self.assertNotIn("_", en, code)

    def test_an_unknown_reason_is_generic_not_raw(self):
        self.assertEqual(
            messages.reason_text("weird_code", "en"), messages.reason_text("unknown", "en")
        )
        self.assertNotIn("weird", messages.reason_text("weird_code", "en"))


class NoLeakTests(unittest.TestCase):
    def test_a_rendered_notice_never_contains_a_token_shaped_string(self):
        text = messages.render("success", _params_for("success"), language="en", tz=SAO_PAULO)
        self.assertIsNone(re.search(r"[A-Za-z0-9_-]{32,}", text))

    def test_the_label_is_printed_verbatim(self):
        """R-006: notices show the account label exactly as Hermes stores it."""
        label = "bea.doe@example.com"
        text = messages.render(
            "success",
            {"label": label, "redeemed_at": 1.0, "expires_at": 2.0},
            language="en",
            tz=SAO_PAULO,
        )
        self.assertIn(label, text)


if __name__ == "__main__":
    unittest.main()
