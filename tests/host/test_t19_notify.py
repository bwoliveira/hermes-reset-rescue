"""T-19 / D-008 — notices through Hermes' own delivery, home channel by default.

The important regression this file pins: on the real profile
``hermes send --list --json`` returns ``{"platforms": {}}`` while
``hermes send -t telegram`` works, because a **bare platform target** is
resolved by ``send_message_tool`` through the configured home channel. The
notifier must therefore send the bare platform name, never a chat id it rebuilt
from the directory.
"""

from __future__ import annotations

import json
import unittest

from reset_rescue import notify
from reset_rescue.cli_runner import CliResult
from reset_rescue.notify import (
    LOCAL_TARGET,
    NoticeResult,
    Notifier,
    mask_target,
    parse_target,
    resolve_target,
    validate_target,
)


class _Cli:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        timed_out: bool = False,
        exec_error: str | None = None,
    ) -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.exec_error = exec_error

    def run(self, argv, *, timeout: float = 30.0) -> CliResult:
        self.calls.append([str(part) for part in argv])
        return CliResult(
            argv=tuple(str(part) for part in argv),
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
            timed_out=self.timed_out,
            exec_error=self.exec_error,
        )


def _ok(note: str = "sent") -> str:
    return json.dumps({"success": True, "note": note})


def _success_params() -> dict:
    """The minimum a success notice needs (the catalog refuses anything else)."""
    return {
        "label": "b***@gmail.com",
        "redeemed_at": 1_800_000_000.0,
        "expires_at": 1_800_000_600.0,
    }


class TargetGrammarTests(unittest.TestCase):
    def test_bare_platform(self):
        self.assertEqual(parse_target("telegram"), ("telegram", None, None))

    def test_platform_and_chat(self):
        self.assertEqual(
            parse_target("telegram:-1001234567890"), ("telegram", "-1001234567890", None)
        )

    def test_platform_chat_and_thread(self):
        self.assertEqual(
            parse_target("telegram:-1001234567890:17585"), ("telegram", "-1001234567890", "17585")
        )

    def test_channel_name(self):
        self.assertEqual(parse_target("discord:#ops"), ("discord", "#ops", None))

    def test_local_is_a_target(self):
        self.assertEqual(parse_target("local"), ("local", None, None))

    def test_empty_is_automatic(self):
        self.assertIsNone(parse_target(""))
        self.assertIsNone(parse_target("   "))

    def test_garbage_is_rejected(self):
        for value in ("not a target", "telegram:", "telegram:a:b:c", "://x", "-telegram"):
            self.assertIsNone(parse_target(value), value)

    def test_validate_accepts_empty_and_local(self):
        self.assertTrue(validate_target(""))
        self.assertTrue(validate_target("local"))
        self.assertTrue(validate_target("telegram"))
        self.assertFalse(validate_target("nonsense target"))

    def test_mask_never_shows_a_full_chat_id(self):
        masked = mask_target("telegram:-1001234567890:17585")
        self.assertNotIn("-1001234567890", masked)
        self.assertTrue(masked.startswith("telegram:"))
        self.assertEqual(mask_target("telegram"), "telegram")
        self.assertEqual(mask_target("local"), "local")
        self.assertEqual(mask_target(""), "local")


class ResolveTests(unittest.TestCase):
    def test_an_explicit_target_wins(self):
        target, explicit = resolve_target("telegram:-100:17", home=lambda: "discord")
        self.assertEqual(target, "telegram:-100:17")
        self.assertTrue(explicit)

    def test_an_invalid_target_becomes_local_not_another_channel(self):
        target, explicit = resolve_target("nonsense target", home=lambda: "telegram")
        self.assertEqual(target, LOCAL_TARGET)
        self.assertTrue(explicit)

    def test_empty_uses_the_bare_home_platform(self):
        """The D-008 rule: bare platform, never a rebuilt chat id."""
        target, explicit = resolve_target("", home=lambda: "telegram")
        self.assertEqual(target, "telegram")
        self.assertFalse(explicit)

    def test_no_home_channel_means_local(self):
        target, explicit = resolve_target("", home=lambda: None)
        self.assertEqual(target, LOCAL_TARGET)
        self.assertFalse(explicit)

    def test_home_platform_reads_the_gateway_config(self):
        """``home_platform`` is the same resolver ``hermes send`` uses."""
        import unittest.mock as mock

        class _Platform:
            value = "telegram"

        class _Home:
            chat_id = "-1001234567890"

        class _Config:
            def get_connected_platforms(self):
                return [_Platform()]

            def get_home_channel(self, _platform):
                return _Home()

        with mock.patch("gateway.config.load_gateway_config", return_value=_Config()):
            self.assertEqual(notify.home_platform(), "telegram")

    def test_home_platform_is_none_when_nothing_is_configured(self):
        import unittest.mock as mock

        class _Config:
            def get_connected_platforms(self):
                return []

            def get_home_channel(self, _platform):
                return None

        with mock.patch("gateway.config.load_gateway_config", return_value=_Config()):
            self.assertIsNone(notify.home_platform())

    def test_home_platform_survives_a_broken_config(self):
        import unittest.mock as mock

        with mock.patch("gateway.config.load_gateway_config", side_effect=RuntimeError("boom")):
            self.assertIsNone(notify.home_platform())


class SendTests(unittest.TestCase):
    def test_a_local_target_never_starts_a_process(self):
        cli = _Cli()
        notifier = Notifier(cli, target=LOCAL_TARGET)
        result = notifier.notify("success", _success_params())
        self.assertTrue(result.skipped)
        self.assertFalse(result.delivered)
        self.assertEqual(cli.calls, [])

    def test_the_send_argv_is_exactly_the_documented_shape(self):
        cli = _Cli(stdout=_ok())
        notifier = Notifier(cli, target="telegram")
        notifier.notify("success", _success_params())
        self.assertEqual(cli.calls[0][:4], ["send", "-t", "telegram", "--json"])
        self.assertEqual(len(cli.calls[0]), 5)
        self.assertIn("Rescued!", cli.calls[0][4])

    def test_a_successful_send_reports_delivered(self):
        cli = _Cli(stdout=_ok())
        result = Notifier(cli, target="telegram").notify("success", _success_params())
        self.assertTrue(result.delivered)
        self.assertIsNone(result.error_code)

    def test_a_skipped_send_is_not_an_error(self):
        cli = _Cli(stdout=json.dumps({"skipped": True, "reason": "quiet hours"}))
        result = Notifier(cli, target="telegram").notify("success", _success_params())
        self.assertTrue(result.skipped)
        self.assertFalse(result.delivered)
        self.assertEqual(result.error_code, "send_skipped")

    def test_a_cli_error_is_a_fixed_code(self):
        cli = _Cli(returncode=1, stdout=json.dumps({"error": "chat not found"}))
        result = Notifier(cli, target="telegram").notify("success", _success_params())
        self.assertFalse(result.delivered)
        self.assertEqual(result.error_code, "send_failed")

    def test_no_home_channel_is_its_own_code(self):
        cli = _Cli(
            returncode=1,
            stdout="",
            stderr="No home channel set for telegram to determine where to send",
        )
        result = Notifier(cli, target="telegram").notify("success", _success_params())
        self.assertEqual(result.error_code, "no_home_channel")

    def test_a_timeout_is_reported(self):
        cli = _Cli(timed_out=True)
        result = Notifier(cli, target="telegram").notify("success", _success_params())
        self.assertEqual(result.error_code, "send_timeout")

    def test_a_missing_launcher_is_reported(self):
        cli = _Cli(exec_error="launcher_not_found")
        result = Notifier(cli, target="telegram").notify("success", _success_params())
        self.assertEqual(result.error_code, "launcher_not_found")

    def test_non_json_success_output_still_counts(self):
        cli = _Cli(stdout="sent\n")
        result = Notifier(cli, target="telegram").notify("success", _success_params())
        self.assertTrue(result.delivered)

    def test_an_empty_body_is_not_sent(self):
        cli = _Cli()
        result = Notifier(cli, target="telegram").send_text("   ")
        self.assertTrue(result.skipped)
        self.assertEqual(result.error_code, "empty_notice")
        self.assertEqual(cli.calls, [])

    def test_a_bad_notice_kind_never_reaches_the_cli(self):
        cli = _Cli()
        result = Notifier(cli, target="telegram").notify("not_a_kind")
        self.assertTrue(result.skipped)
        self.assertEqual(result.error_code, "bad_notice")
        self.assertEqual(cli.calls, [])

    def test_the_target_is_masked_in_every_result(self):
        cli = _Cli(returncode=1, stdout="")
        result = Notifier(cli, target="telegram:-1001234567890:17585").notify(
            "success", _success_params()
        )
        self.assertNotIn("-1001234567890", result.target_display)

    def test_the_language_flows_into_the_text(self):
        cli = _Cli(stdout=_ok())
        notifier = Notifier(cli, target="telegram", language="pt-BR")
        notifier.notify(
            "success", {"label": "b***@gmail.com", "redeemed_at": 1.0, "expires_at": 2.0}
        )
        self.assertIn("Salvo!", cli.calls[0][4])

    def test_labels_are_masked_by_default(self):
        """D-013: masking is on unless the user turns it off."""
        cli = _Cli(stdout=_ok())
        notifier = Notifier(cli, target="telegram", language="en")
        notifier.notify(
            "success",
            {
                "label": "bea.doe@example.com",
                "redeemed_at": 1_800_000_000.0,
                "expires_at": 1_800_000_600.0,
            },
        )
        body = cli.calls[0][4]
        self.assertIn("b***@e***.com", body)
        self.assertNotIn("bea.doe@", body)

    def test_masking_can_be_turned_off(self):
        """D-013: with the setting off the label is exactly what Hermes stores."""
        cli = _Cli(stdout=_ok())
        notifier = Notifier(cli, target="telegram", language="en", mask_labels=False)
        notifier.notify(
            "success",
            {
                "label": "bea.doe@example.com",
                "redeemed_at": 1_800_000_000.0,
                "expires_at": 1_800_000_600.0,
            },
        )
        self.assertIn("bea.doe@example.com", cli.calls[0][4])

    def test_an_unmasked_notice_still_refuses_an_undeclared_parameter(self):
        """Turning masking off must not widen what a notice may carry."""
        cli = _Cli(stdout=_ok())
        notifier = Notifier(cli, target="telegram", mask_labels=False)
        result = notifier.notify("success", {"label": "x", "credit_id": "abc"})
        self.assertTrue(result.skipped)
        self.assertEqual(result.error_code, "bad_notice")
        self.assertEqual(cli.calls, [])

    def test_a_dry_run_notice_carries_the_marker(self):
        cli = _Cli(stdout=_ok())
        notifier = Notifier(cli, target="telegram", language="en")
        notifier.notify(
            "success", {"label": "x", "redeemed_at": 1.0, "expires_at": 2.0}, dry_run=True
        )
        self.assertTrue(cli.calls[0][4].startswith("🧪 [Dry run]"))


class OutboxTests(unittest.TestCase):
    class _Outbox:
        def __init__(self, rows):
            self.rows = rows
            self.sent: list[object] = []
            self.failed: list[tuple[object, str]] = []

        def pending(self, limit):
            return self.rows[:limit]

        def mark_sent(self, row_id):
            self.sent.append(row_id)

        def mark_failed(self, row_id, code):
            self.failed.append((row_id, code))

    def test_flush_drains_and_marks_each_row(self):
        cli = _Cli(stdout=_ok())
        outbox = self._Outbox(
            [
                {"id": 1, "text": "one", "target": "telegram"},
                {"id": 2, "text": "two", "target": "telegram"},
            ]
        )
        results = Notifier(cli, target="telegram").flush(outbox)
        self.assertEqual(len(results), 2)
        self.assertEqual(outbox.sent, [1, 2])
        self.assertEqual(outbox.failed, [])

    def test_a_failed_row_stays_queued_with_its_code(self):
        cli = _Cli(returncode=1, stdout="")
        outbox = self._Outbox([{"id": 7, "text": "x", "target": "telegram"}])
        Notifier(cli, target="telegram").flush(outbox)
        self.assertEqual(outbox.sent, [])
        self.assertEqual(outbox.failed, [(7, "send_failed")])

    def test_a_skipped_row_is_neither_sent_nor_failed(self):
        outbox = self._Outbox([{"id": 3, "text": "x", "target": "local"}])
        Notifier(_Cli(), target="local").flush(outbox)
        self.assertEqual(outbox.sent, [])
        self.assertEqual(outbox.failed, [])

    def test_a_broken_outbox_is_a_no_op(self):
        self.assertEqual(Notifier(_Cli()).flush(object()), [])

    def test_flush_respects_the_limit(self):
        cli = _Cli(stdout=_ok())
        outbox = self._Outbox([{"id": i, "text": "x", "target": "telegram"} for i in range(5)])
        Notifier(cli, target="telegram").flush(outbox, limit=2)
        self.assertEqual(len(cli.calls), 2)


class NoLeakTests(unittest.TestCase):
    def test_no_result_repr_carries_the_target(self):
        result = NoticeResult(
            target_display="telegram:…7890", delivered=False, error_code="send_failed"
        )
        self.assertNotIn("-1001234567890", repr(result))

    def test_the_notifier_never_prints(self):
        import contextlib
        import io

        buffer_out, buffer_err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buffer_out), contextlib.redirect_stderr(buffer_err):
            Notifier(_Cli(stdout=_ok()), target="telegram").notify("success", _success_params())
        self.assertEqual(buffer_out.getvalue(), "")
        self.assertEqual(buffer_err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
