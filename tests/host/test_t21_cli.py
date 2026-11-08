"""T-21a / T-21 — the CLI tree: six subcommands, usage errors before any work.

The contract this file pins:

* ``--help`` lists exactly the six subcommands of the simplification pass.
* A usage error (a missing ``--role``, an unknown command) exits **2** and
  never calls the service.
* Every subcommand reaches exactly one service method, and the exit code
  reflects the result (0 ok, 1 failure, 2 usage).
* ``--json`` goes through the DTO allow-list serializer, never ``repr``.
"""

from __future__ import annotations

import argparse
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

from reset_rescue import cli
from reset_rescue.dto import (
    AccountDTO,
    AttentionDTO,
    CreditDTO,
    EnsureDTO,
    HistoryDTO,
    HistoryItemDTO,
    RedeemResultDTO,
    ScheduleDTO,
    SettingsViewDTO,
    SlotDTO,
    StatusDTO,
)


class _Service:
    """Records which method ran and answers with a canned DTO."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.status_dto = StatusDTO(
            version="0.1.0",
            schema_version=1,
            source_kind="pool",
            coverage_note_key=None,
            dry_run=False,
            language="en",
            settings=SettingsViewDTO(
                scan_interval_hours=12,
                lead_minutes=10,
                backup_lead_minutes=4,
                notify_target_display="telegram",
                included_accounts_count=0,
            ),
            settings_warnings=[],
            schedule=ScheduleDTO(
                armed=True,
                scan_job=None,
                next_scan_at="2026-11-07T12:00:00Z",
                last_scan_at=None,
                last_scan_outcome=None,
                wrappers=[],
                problem_code=None,
            ),
            accounts=[],
            attention=AttentionDTO(failed=0, pending=0, next_fire_at=None),
        )
        self.ensure_dto = EnsureDTO(
            armed=True, created=["reset-rescue-scan"], updated=[], removed=[], problems=[]
        )

    def status(self):
        self.calls.append(("status", {}))
        return self.status_dto

    def ensure(self):
        self.calls.append(("ensure", {}))
        return self.ensure_dto

    def scan(self, *, dry_run=None):
        # ``None`` means "the flag was absent", which is what keeps a
        # persistent ``dry_run=true`` in force (R1).
        self.calls.append(("scan", {"dry_run": dry_run}))
        return self.status_dto

    def fire(self, *, role: str, dry_run=None):
        self.calls.append(("fire", {"role": role, "dry_run": dry_run}))
        return type("FireResult", (), {"exit_code": 0, "stdout": "", "stderr": ""})()

    def quota_check(self):
        self.calls.append(("quota_check", {}))
        return type("QuotaResult", (), {"exit_code": 0, "stdout": "", "stderr": ""})()

    def history(self, limit: int = 50):
        self.calls.append(("history", {"limit": limit}))
        return getattr(self, "history_dto", HistoryDTO(items=[]))

    def redeem_now(self, credit_key: str, *, confirm: bool = False):
        self.calls.append(("redeem_now", {"credit_key": credit_key}))
        return getattr(
            self,
            "redeem_dto",
            RedeemResultDTO(
                credit_key=credit_key, outcome="redeemed", reason_code=None, windows_reset=None
            ),
        )

    def uninstall(self, *, keep_data: bool = False):
        self.calls.append(("uninstall", {"keep_data": keep_data}))
        return EnsureDTO(
            armed=False, created=[], updated=[], removed=["reset-rescue-scan"], problems=[]
        )


def _run(argv: list[str], service=None) -> tuple[int, str, str]:
    parser = cli.build_parser()
    args = parser.parse_args(argv)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.handle_cli(args, service=service or _Service())
    return code, out.getvalue(), err.getvalue()


class HelpTests(unittest.TestCase):
    def test_help_lists_the_eight_subcommands(self):
        text = cli.build_parser().format_help()
        for name in cli.SUBCOMMANDS:
            self.assertIn(name, text, name)
        self.assertEqual(len(cli.SUBCOMMANDS), 8)

    def test_the_cut_subcommands_are_gone(self):
        """§6 removed test-notice, include and the rehearsal/manual-role verbs."""
        text = cli.build_parser().format_help()
        for name in ("test-notice", "include", "rehearse", "remind"):
            self.assertNotIn(f"    {name}", text, name)

    def test_history_and_redeem_now_are_back(self):
        """D-014/D-015: both were cut in §6 and restored by the maintainer."""
        text = cli.build_parser().format_help()
        self.assertIn("    history", text)
        self.assertIn("    redeem-now", text)

    def test_every_subcommand_has_its_own_help(self):
        for name in cli.SUBCOMMANDS:
            with self.assertRaises(SystemExit) as caught:
                cli.build_parser().parse_args([name, "--help"])
            self.assertEqual(caught.exception.code, 0, name)

    def test_no_subcommand_is_a_usage_error(self):
        code, out, err = _run([])
        self.assertEqual(code, cli.EXIT_USAGE)

    def test_version_is_printed(self):
        with self.assertRaises(SystemExit) as caught:
            cli.build_parser().parse_args(["--version"])
        self.assertEqual(caught.exception.code, 0)

    def test_setup_cli_builds_the_same_tree_on_a_host_parser(self):
        """Hermes hands us its own subparser; the tree must attach to it."""
        parser = argparse.ArgumentParser(prog="hermes reset-rescue")
        cli.setup_cli(parser)
        self.assertEqual(parser.parse_args(["status"]).reset_rescue_command, "status")
        self.assertEqual(parser.parse_args(["fire", "--role", "backup"]).role, "backup")


class UsageBeforeServiceTests(unittest.TestCase):
    def test_a_missing_role_is_a_usage_error_and_never_calls_the_service(self):
        with self.assertRaises(SystemExit) as caught:
            cli.build_parser().parse_args(["fire"])
        self.assertEqual(caught.exception.code, 2)

    def test_an_unknown_role_is_refused_by_argparse(self):
        with self.assertRaises(SystemExit) as caught:
            cli.build_parser().parse_args(["fire", "--role", "rehearsal"])
        self.assertEqual(caught.exception.code, 2)

    def test_an_unknown_command_exits_2_without_a_service_call(self):
        service = _Service()
        args = argparse.Namespace(reset_rescue_command="include")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.handle_cli(args, service=service)
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertEqual(service.calls, [])
        self.assertIn("unknown command", err.getvalue())


class DispatchTests(unittest.TestCase):
    def test_status_renders_a_human_view(self):
        code, out, _ = _run(["status"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("Reset Rescue 0.1.0", out)
        self.assertIn("Schedule: armed", out)

    def test_status_json_is_the_allow_list_serializer(self):
        code, out, _ = _run(["status", "--json"])
        self.assertEqual(code, cli.EXIT_OK)
        payload = json.loads(out)
        self.assertEqual(payload["version"], "0.1.0")
        self.assertEqual(payload["schedule"]["armed"], True)
        self.assertNotIn("token", json.dumps(payload).lower())

    @mock.patch("reset_rescue.clock.display_timezone", return_value=ZoneInfo("America/Sao_Paulo"))
    def test_status_lists_accounts_and_credits(self, _display_timezone):
        service = _Service()
        service.status_dto.accounts = [
            AccountDTO(
                account_key="a" * 16,
                label="me@example.com",
                included=True,
                reachable=True,
                last_error_code=None,
                last_checked_at=None,
                credits=[
                    CreditDTO(
                        credit_key="b" * 16,
                        title="Rate limit reset",
                        reset_type="codex",
                        granted_at=None,
                        expires_at="2026-11-30T19:53:00Z",
                        state="available",
                        plan=None,
                        primary=SlotDTO(
                            role="primary", fire_at="2026-11-30T19:43:00Z", job_state="scheduled"
                        ),
                        backup=None,
                        last_result=None,
                        can_redeem_now=True,
                    )
                ],
            )
        ]
        code, out, _ = _run(["status"], service)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("me@example.com", out)
        self.assertIn("1 banked reset(s)", out)
        self.assertIn(
            "  - expires 2026-11-30 16:53 -03 (available, rescue 2026-11-30 16:43 -03)",
            out,
        )
        code, out, _ = _run(["status", "--json"], service)
        self.assertEqual(code, cli.EXIT_OK)
        credit = json.loads(out)["accounts"][0]["credits"][0]
        self.assertEqual(credit["expires_at"], "2026-11-30T19:53:00Z")
        self.assertEqual(credit["primary"]["fire_at"], "2026-11-30T19:43:00Z")

    def test_no_account_message_is_actionable(self):
        code, out, _ = _run(["status"])
        self.assertIn("hermes auth add openai-codex", out)

    def test_ensure_reports_what_it_did(self):
        code, out, _ = _run(["ensure"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("reset-rescue-scan", out)

    def test_ensure_with_problems_exits_1(self):
        service = _Service()
        service.ensure_dto.problems = ["cron_unavailable"]
        code, out, _ = _run(["ensure"], service)
        self.assertEqual(code, cli.EXIT_FAILURE)
        self.assertIn("Problems", out)

    def test_scan_passes_dry_run(self):
        service = _Service()
        _run(["scan", "--dry-run"], service)
        self.assertEqual(service.calls[0], ("scan", {"dry_run": True}))
        service = _Service()
        _run(["scan"], service)
        self.assertEqual(service.calls[0], ("scan", {"dry_run": None}))

    def test_scan_with_a_failed_credit_exits_1(self):
        service = _Service()
        service.status_dto.attention = AttentionDTO(failed=2, pending=0, next_fire_at=None)
        code, _, _ = _run(["scan"], service)
        self.assertEqual(code, cli.EXIT_FAILURE)

    def test_fire_passes_each_role(self):
        for role in cli.FIRE_ROLES:
            service = _Service()
            code, _, _ = _run(["fire", "--role", role], service)
            self.assertEqual(code, cli.EXIT_OK, role)
            self.assertEqual(service.calls[0], ("fire", {"role": role, "dry_run": None}))

    def test_fire_prints_the_stdout_and_stderr_of_the_run(self):
        service = _Service()

        def fire(*, role, dry_run=None):
            service.calls.append(("fire", {"role": role, "dry_run": dry_run}))
            return type(
                "R", (), {"exit_code": 1, "stdout": "notice text\n", "stderr": "summary\n"}
            )()

        service.fire = fire  # type: ignore[method-assign]
        code, out, err = _run(["fire", "--role", "primary"], service)
        self.assertEqual(code, 1)
        self.assertEqual(out, "notice text\n")
        self.assertEqual(err, "summary\n")

    def test_quota_check_reaches_the_service(self):
        service = _Service()
        code, _, _ = _run(["quota-check"], service)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(service.calls, [("quota_check", {})])

    def test_quota_check_failure_exits_1(self):
        service = _Service()
        service.quota_check = lambda: type(  # type: ignore[method-assign]
            "R", (), {"exit_code": 1, "stdout": "", "stderr": "boom\n"}
        )()
        code, _, err = _run(["quota-check"], service)
        self.assertEqual(code, 1)
        self.assertIn("boom", err)

    @mock.patch("reset_rescue.clock.display_timezone", return_value=ZoneInfo("America/Sao_Paulo"))
    def test_history_renders_the_local_log(self, _display_timezone):
        service = _Service()
        service.history_dto = HistoryDTO(
            items=[
                HistoryItemDTO(
                    at="2026-11-09T21:30:00Z",
                    action="redeem",
                    credit_key="b" * 16,
                    account_key="a" * 16,
                    outcome="redeemed",
                    reason_code=None,
                ),
                HistoryItemDTO(
                    at="2026-11-09T20:00:00Z",
                    action="reminder",
                    credit_key="b" * 16,
                    account_key="a" * 16,
                    outcome="sent",
                    reason_code=None,
                ),
            ]
        )
        code, out, _ = _run(["history"], service)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("2026-11-09 18:30 -03  redeem  redeemed", out)
        self.assertIn("2026-11-09 17:00 -03  reminder  sent", out)
        self.assertEqual(service.calls[0][0], "history")
        code, out, _ = _run(["history", "--json"], service)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(json.loads(out)["items"][0]["at"], "2026-11-09T21:30:00Z")

    def test_history_limit_is_passed_and_bounded_by_the_service(self):
        service = _Service()
        _run(["history", "--limit", "7"], service)
        self.assertEqual(service.calls[0], ("history", {"limit": 7}))

    def test_history_json_is_the_allow_list_serializer(self):
        service = _Service()
        service.history_dto = HistoryDTO(
            items=[
                HistoryItemDTO(
                    at="2026-11-09T21:30:00Z",
                    action="redeem",
                    credit_key="b" * 16,
                    account_key="a" * 16,
                    outcome="redeemed",
                    reason_code=None,
                )
            ]
        )
        code, out, _ = _run(["history", "--json"], service)
        self.assertEqual(code, cli.EXIT_OK)
        payload = json.loads(out)
        self.assertEqual(payload["items"][0]["action"], "redeem")

    def test_history_empty_is_a_friendly_line(self):
        service = _Service()
        service.history_dto = HistoryDTO(items=[])
        code, out, _ = _run(["history"], service)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("No activity yet", out)

    def test_redeem_now_without_yes_exits_2_and_never_calls_the_service(self):
        service = _Service()
        code, _, err = _run(["redeem-now", "b" * 16], service)
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertEqual(service.calls, [])
        self.assertIn("--yes", err)

    def test_redeem_now_with_yes_reaches_the_service_once(self):
        service = _Service()
        code, out, _ = _run(["redeem-now", "b" * 16, "--yes"], service)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(service.calls, [("redeem_now", {"credit_key": "b" * 16})])
        self.assertIn("Redeemed", out)

    def test_redeem_now_failure_exits_1_with_the_reason(self):
        service = _Service()
        service.redeem_dto = RedeemResultDTO(
            credit_key="b" * 16, outcome="failed", reason_code="auth_expired", windows_reset=None
        )
        code, _, err = _run(["redeem-now", "b" * 16, "--yes"], service)
        self.assertEqual(code, cli.EXIT_FAILURE)
        self.assertIn("auth_expired", err)

    def test_uninstall_passes_keep_data(self):
        service = _Service()
        code, out, _ = _run(["uninstall", "--keep-data"], service)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(service.calls, [("uninstall", {"keep_data": True})])
        self.assertIn("reset-rescue-scan", out)


class ShortTimeTests(unittest.TestCase):
    @mock.patch("reset_rescue.clock.display_timezone", return_value=ZoneInfo("America/Sao_Paulo"))
    def test_offset_input_converts_and_rolls_back_the_date(self, _display_timezone):
        self.assertEqual(
            cli._short_time("2026-12-01T02:15:59.123456+02:00"), "2026-11-30 21:15 -03"
        )

    @mock.patch("reset_rescue.clock.display_timezone", return_value=timezone.utc)
    def test_utc_is_explicit_too(self, _display_timezone):
        self.assertEqual(cli._short_time("2026-11-30T19:53:00Z"), "2026-11-30 19:53 UTC")

    @mock.patch(
        "reset_rescue.clock.display_timezone", return_value=timezone(timedelta(hours=5, minutes=30))
    )
    def test_fixed_offset_is_visible(self, _display_timezone):
        self.assertEqual(cli._short_time("2026-11-30T19:53:00Z"), "2026-12-01 01:23 UTC+05:30")

    def test_missing_and_invalid_values_stay_readable(self):
        self.assertEqual(cli._short_time(None), "")
        self.assertEqual(cli._short_time(""), "")
        self.assertEqual(cli._short_time("unknown"), "unknown")


class FailureTests(unittest.TestCase):
    def test_a_service_that_raises_prints_a_fixed_line_not_a_traceback(self):
        service = _Service()

        def boom():
            raise RuntimeError("token sk-live-123 leaked in a message")

        service.status = boom  # type: ignore[method-assign]
        code, out, err = _run(["status"], service)
        self.assertEqual(code, cli.EXIT_FAILURE)
        self.assertIn("RuntimeError", err)
        self.assertNotIn("sk-live-123", err)
        self.assertNotIn("Traceback", err)

    def test_an_unavailable_service_is_a_clean_failure(self):
        args = cli.build_parser().parse_args(["status"])
        out, err = io.StringIO(), io.StringIO()

        import unittest.mock as mock

        with mock.patch.object(cli, "_load_service", side_effect=ImportError("no runtime")):
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.handle_cli(args)
        self.assertEqual(code, cli.EXIT_FAILURE)
        self.assertIn("not available", err.getvalue())

    def test_keyboard_interrupt_exits_1(self):
        service = _Service()

        def interrupted(**_kwargs):
            raise KeyboardInterrupt

        service.scan = interrupted  # type: ignore[method-assign]
        code, _, err = _run(["scan"], service)
        self.assertEqual(code, cli.EXIT_FAILURE)
        self.assertIn("interrupted", err)


if __name__ == "__main__":
    unittest.main()
