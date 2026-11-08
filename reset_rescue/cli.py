"""``hermes reset-rescue …`` — the CLI tree (simplification §6, T-21a/T-21/T-74).

The CLI is a thin shell over the service: it parses argv, resolves the service,
prints the DTO and returns an exit code. It holds no business logic, so every
behaviour the CLI shows is also what the dashboard and the cron wrappers get.

**Eight subcommands** (§6 cut `include` and `test-notice`; D-014/D-015 put
`history` and `redeem-now` back):

* ``status [--json]`` — read-only view.
* ``ensure`` — install/repair wrappers + jobs (idempotent).
* ``scan [--dry-run] [--json]`` — discover and schedule.
* ``fire --role primary|backup|remind`` — what a cron wrapper calls.
* ``quota-check`` — the D-009 early-redemption trigger (T-74).
* ``history [--limit N] [--json]`` — the local activity log (D-014).
* ``redeem-now <credit> --yes`` — one explicit redemption (D-015).
* ``uninstall [--keep-data]`` — remove owned jobs and wrappers.

Exit codes (I6 and the CLI contract):

* ``0`` — the operation succeeded (or was a no-op that needed no action).
* ``1`` — the operation ran and something failed (a credit, a job, a notice).
* ``2`` — usage error: bad arguments. **No service call happens** on a usage
  error, which is what makes a mistyped command harmless.

``--json`` prints the DTO through :func:`reset_rescue.dto.to_json` (the
allow-list serializer), never ``repr`` of an internal object.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Protocol, Sequence

from . import clock, messages
from .dto import to_json
from .version import VERSION

__all__ = [
    "EXIT_OK",
    "EXIT_FAILURE",
    "EXIT_USAGE",
    "SUBCOMMANDS",
    "FIRE_ROLES",
    "ServiceLike",
    "build_parser",
    "setup_cli",
    "handle_cli",
    "main",
]

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

#: The eight subcommands, in ``--help`` order (§6 + D-014/D-015).
SUBCOMMANDS = (
    "status",
    "ensure",
    "scan",
    "fire",
    "quota-check",
    "history",
    "redeem-now",
    "uninstall",
)

#: Roles a wrapper may pin. ``remind`` is GET-only (I5-lite); the D-009 trigger
#: has its own subcommand and never runs from a rescue wrapper.
FIRE_ROLES = ("primary", "backup", "remind")


class ServiceLike(Protocol):  # pragma: no cover - structural typing only
    """The facade the CLI needs (implemented by ``reset_rescue.host.HostService``)."""

    def status(self) -> Any: ...
    def ensure(self) -> Any: ...
    def scan(self, *, dry_run: Optional[bool] = None) -> Any: ...
    def fire(self, *, role: str, dry_run: Optional[bool] = None) -> Any: ...
    def quota_check(self) -> Any: ...
    def history(self, limit: int = 50) -> Any: ...
    def redeem_now(self, credit_key: str, *, confirm: Any) -> Any: ...
    def uninstall(self, *, keep_data: bool = False) -> Any: ...


# --------------------------------------------------------------------- parser


def build_parser(prog: str = "hermes reset-rescue") -> argparse.ArgumentParser:
    """The full argparse tree (also used by ``hermes <cmd> --help``)."""
    parser = argparse.ArgumentParser(
        prog=prog,
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"reset-rescue {VERSION}")
    _attach_subcommands(parser)
    return parser


def setup_cli(parser: argparse.ArgumentParser) -> None:
    """``ctx.register_cli_command`` setup hook: attach the tree to Hermes' parser.

    Hermes hands us a fresh subparser for ``hermes reset-rescue``; the tree is
    built on it directly (the command name is already fixed by the host).
    """
    parser.description = _DESCRIPTION
    parser.epilog = _EPILOG
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    parser.add_argument("--version", action="version", version=f"reset-rescue {VERSION}")
    _attach_subcommands(parser)


_DESCRIPTION = (
    "Rescue expiring OpenAI Codex rate-limit reset credits. Scans every Codex "
    "account in this profile's Hermes credential pool, redeems each banked "
    "credit minutes before it expires, confirms by readback and sends one "
    "notice per result."
)
_EPILOG = (
    "Exit codes: 0 ok, 1 a failure, 2 usage error.\n"
    "Remove the background jobs with `uninstall` before "
    "`hermes plugins remove`."
)


def _attach_subcommands(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="reset_rescue_command", metavar="<command>")

    status = sub.add_parser("status", help="Show accounts, credits and the schedule")
    status.add_argument("--json", action="store_true", help="Print the raw status JSON")

    sub.add_parser("ensure", help="Install or repair the wrappers and the jobs")

    scan = sub.add_parser("scan", help="Look for credits now and schedule rescues")
    scan.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Plan only: never redeem, never create a redemption job",
    )
    scan.add_argument("--json", action="store_true", help="Print the raw status JSON")

    fire = sub.add_parser("fire", help="Run one scheduled attempt (used by cron)")
    fire.add_argument(
        "--role",
        required=True,
        choices=list(FIRE_ROLES),
        help="Which slot this run handles; the wrapper pins it",
    )
    fire.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Read-only: GETs only, no consume POST",
    )

    sub.add_parser("quota-check", help="Early redemption when the weekly quota is low")

    history = sub.add_parser("history", help="Show the local activity log")
    history.add_argument(
        "--limit", type=int, default=50, help="How many entries to show (1-200, default 50)"
    )
    history.add_argument("--json", action="store_true", help="Print the raw history JSON")

    redeem = sub.add_parser("redeem-now", help="Redeem one banked reset right now")
    redeem.add_argument("credit", help="The credit key shown by `status --json`")
    redeem.add_argument(
        "--yes",
        dest="confirm",
        action="store_true",
        help="Confirm the redemption; without it nothing happens",
    )

    uninstall = sub.add_parser("uninstall", help="Remove the plugin's jobs and wrapper files")
    uninstall.add_argument(
        "--keep-data",
        dest="keep_data",
        action="store_true",
        help="Keep the local state and the redemption ids",
    )

    parser.set_defaults(reset_rescue_command=None)


# -------------------------------------------------------------------- service


def _load_service() -> ServiceLike:
    """Build the real service (lazy: the CLI must import fast and never at load)."""
    from .host import build_service  # noqa: PLC0415 - deliberately lazy

    return build_service()


# -------------------------------------------------------------------- handler


def handle_cli(
    args: argparse.Namespace,
    *,
    service: Optional[ServiceLike] = None,
    out: Optional[Callable[[str], None]] = None,
    err: Optional[Callable[[str], None]] = None,
) -> int:
    """Run one subcommand; returns the process exit code.

    ``service`` is injectable so the CLI tests never build a real one. A
    missing subcommand prints help and exits 2 (a usage error, like any other).
    """
    write = out or _stdout_writer
    write_err = err or _stderr_writer
    command = getattr(args, "reset_rescue_command", None)
    if not command:
        build_parser().print_help()
        return EXIT_USAGE
    if command not in SUBCOMMANDS:
        write_err(f"reset-rescue: unknown command {command!r}.")
        return EXIT_USAGE

    try:
        svc = service if service is not None else _load_service()
    except Exception:
        write_err(
            "reset-rescue: the plugin is not available in this process "
            "(see README: the CLI runs in-process)."
        )
        return EXIT_FAILURE

    try:
        return _dispatch(command, args, svc, write, write_err)
    except KeyboardInterrupt:
        write_err("reset-rescue: interrupted.")
        return EXIT_FAILURE
    except Exception as exc:  # noqa: BLE001 - a CLI must not print a traceback
        # Fixed text only: an exception message can carry a URL or an id.
        write_err(f"reset-rescue: {command} failed ({type(exc).__name__}).")
        return EXIT_FAILURE


def _stdout_writer(text: str) -> None:
    """Write one already-formatted block to stdout, without adding a newline.

    The service's ``fire`` output is the notice channel (I6): it must reach the
    cron runner byte for byte, so the CLI never re-formats or re-terminates it.
    """
    sys.stdout.write(text if text.endswith("\n") else text + "\n")


def _stderr_writer(text: str) -> None:
    sys.stderr.write(text if text.endswith("\n") else text + "\n")


def _dispatch(
    command: str,
    args: argparse.Namespace,
    svc: ServiceLike,
    write: Callable[[str], None],
    write_err: Callable[[str], None],
) -> int:
    if command == "status":
        dto = svc.status()
        if getattr(args, "json", False):
            write(to_json(dto))
        else:
            write(_render_status(dto))
        return EXIT_OK

    if command == "ensure":
        dto = svc.ensure()
        write(_render_ensure(dto))
        return EXIT_OK if not getattr(dto, "problems", None) else EXIT_FAILURE

    if command == "scan":
        dto = svc.scan(dry_run=_flag_or_none(args, "dry_run"))
        if getattr(args, "json", False):
            write(to_json(dto))
        else:
            write(_render_status(dto))
        if _status_failed(dto):
            # A non-zero exit with no reason is a dead end for a script: the
            # specific code goes to stderr, the same phrase the status block
            # shows (R6).
            write_err(f"reset-rescue: {_status_problem_text(dto)}")
            return EXIT_FAILURE
        return EXIT_OK

    if command == "fire":
        return _emit_run(
            svc.fire(role=args.role, dry_run=_flag_or_none(args, "dry_run")), write, write_err
        )

    if command == "quota-check":
        return _emit_run(svc.quota_check(), write, write_err)

    if command == "history":
        dto = svc.history(limit=int(getattr(args, "limit", 50) or 50))
        if getattr(args, "json", False):
            write(to_json(dto))
        else:
            write(_render_history(dto))
        return EXIT_OK

    if command == "redeem-now":
        if not getattr(args, "confirm", False):
            # A redemption spends a credit: no confirmation, no service call.
            write_err("reset-rescue: redeem-now needs --yes to spend a credit.")
            return EXIT_USAGE
        return _emit_redeem(svc.redeem_now(args.credit, confirm=True), write, write_err)

    if command == "uninstall":
        dto = svc.uninstall(keep_data=bool(getattr(args, "keep_data", False)))
        write(_render_uninstall(dto))
        return EXIT_OK if not getattr(dto, "problems", None) else EXIT_FAILURE

    write_err(f"reset-rescue: unknown command {command!r}.")
    return EXIT_USAGE


def _flag_or_none(args: argparse.Namespace, name: str) -> Optional[bool]:
    """``True`` when the flag was given, ``None`` when it was absent.

    An absent flag must stay absent: ``False`` here would override a persistent
    ``dry_run=true`` and turn the pause switch into a spend (R1).
    """
    return True if getattr(args, name, False) else None


def _emit_run(result: Any, write: Callable[[str], None], write_err: Callable[[str], None]) -> int:
    """Print a run's stdout/stderr and return its exit code (I6 pass-through)."""
    text = getattr(result, "stdout", "") or ""
    if text:
        write(text)
    failure = getattr(result, "stderr", "") or ""
    if failure:
        write_err(failure)
    code = getattr(result, "exit_code", None)
    if isinstance(code, int):
        return code
    return EXIT_OK


def _status_problem_text(dto: Any) -> str:
    """The one phrase explaining why a ``scan`` exited non-zero (R6).

    A failed credit names its own code; an unarmed schedule names the schedule
    problem. Never a raw code and never provider text (I8).
    """
    language = getattr(dto, "language", messages.DEFAULT_LANGUAGE)
    schedule = getattr(dto, "schedule", None)
    code = getattr(schedule, "problem_code", None) if schedule is not None else None
    if code:
        return messages.reason_text(code, language)
    accounts = list(getattr(dto, "accounts", None) or ())
    for account in accounts:
        failure = getattr(account, "last_error_code", None)
        if failure:
            return messages.reason_text(failure, language)
    return messages.reason_text("unknown", language)


def _status_failed(dto: Any) -> bool:
    """True when the scan did not leave the rescue armed (R6).

    Two independent failures: a credit that could not be rescued, and a
    schedule that is not armed. The second one is what the CLI used to miss —
    a scan whose every ``cron create`` was refused printed "not armed" and
    still exited 0, so a script or a cron wrapper saw success.
    """
    attention = getattr(dto, "attention", None)
    if getattr(attention, "failed", 0):
        return True
    schedule = getattr(dto, "schedule", None)
    return bool(getattr(schedule, "problem_code", None))


# ------------------------------------------------------------------- renderers


def _render_status(dto: Any) -> str:
    """A short human view: accounts, credits, the next rescue."""
    language = getattr(dto, "language", messages.DEFAULT_LANGUAGE)
    lines: list[str] = [f"Reset Rescue {getattr(dto, 'version', VERSION)}"]
    schedule = getattr(dto, "schedule", None)
    if schedule is not None:
        armed = "armed" if getattr(schedule, "armed", False) else "not armed"
        next_scan = getattr(schedule, "next_scan_at", None)
        lines.append(f"Schedule: {armed}" + (f", next scan {next_scan}" if next_scan else ""))
        problem = getattr(schedule, "problem_code", None)
        if problem:
            lines.append(f"Problem: {messages.reason_text(problem, language)}")
    accounts = list(getattr(dto, "accounts", None) or ())
    if not accounts:
        lines.append(
            "No Codex account found yet — add one in Hermes (Providers, or "
            "`hermes auth add openai-codex`)."
        )
    for account in accounts:
        credits = list(getattr(account, "credits", None) or ())
        label = getattr(account, "label", "Codex account")
        if not credits:
            lines.append(f"{label}: no banked resets")
            continue
        lines.append(f"{label}: {len(credits)} banked reset(s)")
        for credit in credits:
            expires = _short_time(getattr(credit, "expires_at", None))
            state = getattr(credit, "state", "unknown")
            primary = getattr(credit, "primary", None)
            fire_at = _short_time(getattr(primary, "fire_at", None)) if primary else ""
            tail = f", rescue {fire_at}" if fire_at else ""
            lines.append(f"  - expires {expires} ({state}{tail})")
    return "\n".join(lines)


def _render_ensure(dto: Any) -> str:
    created = list(getattr(dto, "created", None) or ())
    updated = list(getattr(dto, "updated", None) or ())
    removed = list(getattr(dto, "removed", None) or ())
    problems = list(getattr(dto, "problems", None) or ())
    armed = "armed" if getattr(dto, "armed", False) else "not armed"
    parts = [f"Schedule {armed}."]
    if created:
        parts.append(f"Created: {', '.join(map(str, created))}.")
    if updated:
        parts.append(f"Updated: {', '.join(map(str, updated))}.")
    if removed:
        parts.append(f"Removed: {', '.join(map(str, removed))}.")
    if problems:
        parts.append(f"Problems: {', '.join(map(str, problems))}.")
    return " ".join(parts)


def _render_history(dto: Any) -> str:
    """One line per entry: when, what, which credit, the outcome."""
    items = list(getattr(dto, "items", None) or ())
    if not items:
        return "No activity yet."
    lines: list[str] = []
    for item in items:
        at = _short_time(getattr(item, "at", None))
        action = str(getattr(item, "action", "") or "")
        outcome = str(getattr(item, "outcome", "") or "")
        reason = str(getattr(item, "reason_code", "") or "")
        tail = f" ({reason})" if reason else ""
        lines.append(f"{at}  {action}  {outcome}{tail}".strip())
    return "\n".join(lines)


def _emit_redeem(
    result: Any, write: Callable[[str], None], write_err: Callable[[str], None]
) -> int:
    """Print one redemption outcome; exit 0 only when the credit settled."""
    outcome = str(getattr(result, "outcome", "") or "")
    reason = str(getattr(result, "reason_code", "") or "")
    if outcome in ("redeemed", "already_redeemed"):
        write(f"Redeemed: {outcome}.")
        return EXIT_OK
    if outcome == "dry_run":
        write("Dry run: no credit was spent.")
        return EXIT_OK
    detail = f" ({reason})" if reason else ""
    write_err(f"reset-rescue: redemption failed: {outcome}{detail}.")
    return EXIT_FAILURE


def _render_uninstall(dto: Any) -> str:
    removed = list(getattr(dto, "removed", None) or ())
    problems = list(getattr(dto, "problems", None) or ())
    parts = ["Reset Rescue removed." if not problems else "Reset Rescue partially removed."]
    if removed:
        parts.append(f"Removed: {', '.join(map(str, removed))}.")
    if problems:
        parts.append(f"Kept (not ours): {', '.join(map(str, problems))}.")
    return " ".join(parts)


def _short_time(value: Any) -> str:
    """ISO timestamp → ``YYYY-MM-DD HH:MM ZONE`` in the notice display zone (D-011)."""
    text = str(value or "")
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(clock.display_timezone())
    zone = moment.tzname() or moment.strftime("%z")
    return f"{moment:%Y-%m-%d %H:%M} {zone}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Standalone entry point (``python -m reset_rescue.cli``); Hermes uses the hook."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return handle_cli(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
