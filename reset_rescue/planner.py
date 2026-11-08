"""The recurring scan: discover, plan slots, reconcile, late redeem (A§8.2).

One ``Planner.scan()`` under the run lock:

1. read settings and the account list (one account failing never stops others);
2. ``GET`` each included account's credits and store the descriptive fields;
3. plan slots for every available/pending credit inside the horizon, at
   ``expires_at - lead`` and ``expires_at - backup_lead``, never inside
   ``MIN_LEAD``; a late credit (its slots are already too close) is redeemed
   right here unless a live owned one-shot still owns it;
4. reconcile known rows against the fresh list (confirm posted, report
   vanished, mark missed, ``already_redeemed``);
5. remove the jobs of accounts the user excluded, GC, retention, flush.

Every decision is taken from the backend list, never from the local status
(I1/I2). The planner never removes a job that could still run (I7).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from . import messages
from .authorize import QUOTA_ROLE
from .classify import AVAILABLE, PENDING, REDEEMED, Credit, CreditList
from .fire import (
    EXPIRY_MARGIN_S,
    HORIZON_S,
    MIN_LEAD_S,
    MISS_AFTER_S,
    NOTICE_ACCOUNT,
    NOTICE_FINAL,
    NOTICE_INFO,
    NOTICE_MISSED,
    NOTICE_SCHEDULED,
    OUT_ALREADY,
    OUT_FAILED,
    OUT_REDEEMED,
    OUT_VANISHED,
    REAL_ROLES,
    REMINDER_ROLES,
    RedeemResult,
    account_label,
    credit_key_for,
    ensure_slot,
    flush_notices,
    gc_jobs,
    in_hours_text,
    notice_language,
    notice_params,
    pending_job_ids,
    redeem_one,
    remove_credit_jobs,
    remove_slot,
)
from .settings import Settings
from .store import LockTimeout, Store

__all__ = [
    "Planner",
    "ScanResult",
    "QuotaCheckResult",
    "PLANNER_INTERVAL_S",
    "QUOTA_INTERVAL_S",
    "QUOTA_COOLDOWN_S",
]

#: The scan interval a reminder must leave room for (T-72).
PLANNER_INTERVAL_S = 12 * 3_600.0
#: Extra margin for "the next scan will still schedule it".
PLANNER_SLACK_S = 3_600.0
#: The quota check cadence and its per-account cooldown (T-73, D-009).
QUOTA_INTERVAL_S = 15 * 60.0
QUOTA_COOLDOWN_S = 15 * 60.0


@dataclass
class ScanResult:
    """What one scan did (fixed codes only)."""

    exit_code: int = 0
    problem_code: str = ""
    accounts: int = 0
    scheduled: int = 0
    redeemed: int = 0
    missed: int = 0
    failed: int = 0
    reminders: int = 0
    results: list = field(default_factory=list)
    #: Credits this scan already POSTed for: no second path may spend them again.
    attempted: set = field(default_factory=set)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass
class QuotaCheckResult:
    """What one quota check did (fixed codes only, never a percentage)."""

    exit_code: int = 0
    problem_code: str = ""
    accounts: int = 0
    cooled: int = 0
    redeemed: int = 0
    failed: int = 0
    results: list = field(default_factory=list)
    attempted: set = field(default_factory=set)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


#: Credit statuses that make a credit settled for the quota check's arming rule.
_SETTLED = frozenset(
    {
        "redeemed",
        "already_redeemed",
        "nothing_to_reset",
        "vanished",
        "failed",
        "missed",
        "expired",
    }
)
#: The complement: a credit the plugin still expects to act on.
_UNSETTLED = frozenset({"seen", "scheduled", "redeeming"})


def _cooldown_key(account_key: str) -> str:
    """The ``meta`` key of one account's quota-check cooldown (never a secret)."""
    return f"quota_cooldown:{account_key}"


def _rearm_key(account_key: str) -> str:
    """The ``meta`` key of one account's early re-arm guard (D-009, never a secret)."""
    return f"early_rearm:{account_key}"


#: Stored value while an account's last early redeem is spent (or unknown).
_REARM_SPENT = "spent"


class Planner:
    """The recurring scan (A§8.2)."""

    def __init__(self, deps: Any, store: Store) -> None:
        self.deps = deps
        self.store = store

    # -- public ------------------------------------------------------------
    def scan(self, *, dry_run: bool = False) -> ScanResult:
        now = float(self.deps.clock.now())
        try:
            with self.store.run_lock():
                return self._scan_locked(now, dry_run=bool(dry_run))
        except LockTimeout as exc:
            return ScanResult(exit_code=1, problem_code=exc.code)

    # -- internals ---------------------------------------------------------
    def _scan_locked(self, now: float, *, dry_run: bool) -> ScanResult:
        result = ScanResult()
        run_id = self.store.start_run("scan", None, os.getpid(), now)
        self.store.heal_runs(now)
        settings = self.deps.settings
        try:
            accounts = list(self.deps.source.accounts())
        except Exception:  # noqa: BLE001 - no source is a reported problem
            result.exit_code = 1
            result.problem_code = "source_unavailable"
            finished = float(self.deps.clock.now())
            self.store.finish_run(run_id, "failed", finished)
            # P2: a scan that could not read the pool is still a check; the
            # popup must show when it ran and that it failed.
            self.store.set_meta("last_scan_at", str(finished))
            self.store.set_meta("last_scan_outcome", "failed")
            return result

        lists: dict[str, CreditList] = {}
        failed: dict[str, str] = {}
        for account in accounts:
            if not settings.account_included(account.account_key, account.label):
                continue
            result.accounts += 1
            self.store.upsert_account(
                account.account_key, account.label, account.source_kind, account.entry_id, now=now
            )
            try:
                lists[account.account_key] = self.deps.api.list_credits(account)
            except Exception as exc:  # noqa: BLE001 - one account never stops others
                # Only record the code here. Writing it to the account row is
                # ``_account_error``'s job, and it must happen *after* that
                # method has read the previous code: writing first made the
                # transition check compare a value with itself, so no scan ever
                # queued the account-error notice.
                failed[account.account_key] = str(getattr(exc, "code", "") or "network")
                continue
            self.store.mark_account_listed(account.account_key, now)

        for account in accounts:
            if account.account_key in failed:
                self._account_error(account, failed[account.account_key], now)
                continue
            credits = lists.get(account.account_key)
            if credits is None:
                continue
            self._absorb(account, credits, now=now, dry_run=dry_run, result=result)

        self._reconcile(accounts, lists, failed, now=now, dry_run=dry_run, result=result)
        self._drop_excluded(accounts, settings, now)

        if not dry_run:
            self.store.prune(now)
            gc_jobs(self.deps, self.store, protect=pending_job_ids(self.store), now=now)
            flush_notices(self.deps, self.store)

        result.exit_code = 1 if (failed or result.failed or result.missed) else 0
        self._log_scan(result, failed, accounts, now)
        outcome = "failed" if result.exit_code else "ok"
        finished = float(self.deps.clock.now())
        self.store.finish_run(run_id, outcome, finished)
        # P2: the popup's "Checked HH:MM" reads these two keys. Nothing wrote
        # them before, so a profile with finished scans still said "Not checked
        # yet"; the service also falls back to the `runs` table for installs
        # that scanned before this line existed.
        self.store.set_meta("last_scan_at", str(finished))
        self.store.set_meta("last_scan_outcome", outcome)
        return result

    def _log_scan(
        self, result: ScanResult, failed: Mapping[str, str], accounts: Sequence[Any], now: float
    ) -> None:
        """One activity-log row for the scan and one per failure (D-014)."""
        records: list[dict] = [
            {
                "at": now,
                "action": "scan",
                "outcome": "ok" if not result.exit_code else "failed",
                "detail": {
                    "accounts": result.accounts,
                    "scheduled": result.scheduled,
                    "redeemed": result.redeemed,
                    "missed": result.missed,
                    "failed": result.failed,
                    "reminders": result.reminders,
                },
            }
        ]
        for account in accounts:
            code = failed.get(account.account_key)
            if code is None:
                continue
            records.append(
                {
                    "at": now,
                    "action": "failure",
                    "account_key": account.account_key,
                    "outcome": "failed",
                    "reason_code": code,
                    "detail": {"phase": "list"},
                }
            )
        self.store.add_history(records)

    # -- step 3: absorb one account's fresh list ---------------------------
    def _absorb(
        self, account: Any, credits: CreditList, *, now: float, dry_run: bool, result: ScanResult
    ) -> None:
        # D-030: this account's effective settings (its overrides over the globals).
        settings = self.deps.settings.for_account(account.account_key, account.label)
        for credit in credits:
            key = credit_key_for(self.deps.salt, account.account_key, credit.id)
            previous = self.store.get_credit(key)
            row = self.store.upsert_credit(
                key,
                account.account_key,
                title=credit.title or None,
                reset_type=credit.reset_type or None,
                granted_at=credit.granted_at,
                expires_at=credit.expires_at,
                backend_state=credit.classification,
                now=now,
            )
            if credit.classification not in (AVAILABLE, PENDING):
                continue
            if credit.expires_at is None:
                continue
            primary_at = credit.expires_at - settings.primary_fire_offset_s
            backup_at = credit.expires_at - settings.backup_fire_offset_s
            if credit.expires_at - now > HORIZON_S:
                continue  # beyond the horizon: shown, never scheduled
            # D-023: with automatic redemption off the credit is still listed
            # and still gets its reminders, but no fire/backup one-shot is
            # armed and the late branch below never redeems it. The scan is
            # the only thing that re-arms after the switch goes back on.
            if not settings.auto_redeem_enabled:
                if not dry_run:
                    # D-030: turning auto off removes this account's owned fire and
                    # backup one-shots through the verified removal path.
                    for role in ("primary", "backup"):
                        remove_slot(self.deps, self.store, key, role, now)
                    self._arm_reminders(row, key, now)
                continue
            if primary_at > now + MIN_LEAD_S:
                if dry_run:
                    result.scheduled += 1
                    continue
                # Every slot is reconciled on its own, in this branch and in the
                # late one: a live primary must never hide a missing backup
                # (R9). A slot whose time has passed is not created; the late
                # branch below redeems instead.
                armed, failed = self._arm_slots(row, key, primary_at, backup_at, now)
                result.failed += failed
                if failed:
                    # A credit with no verified one-shot is not protected; say
                    # so once, durably, instead of reporting a scheduled rescue
                    # the scheduler never accepted (R6).
                    self._final_notice(row, "cron_failed", now)
                if armed and failed == 0:
                    result.scheduled += 1
                    if previous is None or not previous.get("notified_outcome"):
                        self._scheduled_notice(row, primary_at, backup_at, now)
                    self._log_schedule(key, account.account_key, primary_at, backup_at, now)
                if armed:
                    self.store.set_credit_status(key, "scheduled", now)
                continue
            # Late: the slots cannot be created any more. Redeem here unless a
            # live owned one-shot of this credit still owns the redemption.
            # A backup that is still safely in the future is (re)armed first:
            # a crash after the primary create must not cost the backup (R9).
            if not dry_run:
                self._arm_late_backup(row, key, backup_at, now, result)
            if self._missed(row, now):
                # Its last slot is long past: report it once (A§8.2 step 5).
                self._mark_missed(row, now, result)
            if credit.expires_at <= now + EXPIRY_MARGIN_S:
                continue
            if self._live_job(key):
                continue
            if dry_run:
                result.redeemed += 1
                continue
            outcome = redeem_one(
                self.deps, self.store, row, role="scan", account=account, attempted=result.attempted
            )
            result.results.append(outcome)
            result.attempted.add(key)
            if outcome.failure:
                result.failed += 1
            elif outcome.success:
                result.redeemed += 1

    def _arm_slots(
        self, row: Mapping[str, Any], key: str, primary_at: float, backup_at: float, now: float
    ) -> tuple[int, int]:
        """Arm the rescue pair and the reminders; ``(redemption_slots, failures)``.

        Only the primary and the backup are protection: a reminder one-shot
        reads and never redeems, so a reminder that was created while both
        rescue slots failed must not be counted as a scheduled rescue (R6).
        """
        armed = 0
        failed = 0
        for role, fire_at in (("primary", primary_at), ("backup", backup_at)):
            if fire_at <= now + MIN_LEAD_S:
                continue
            slot = ensure_slot(self.deps, self.store, row, role, fire_at=fire_at, now=now)
            if slot is None:
                failed += 1
            else:
                armed += 1
        for role, fire_at in self._reminder_slots(row, key, now):
            if ensure_slot(self.deps, self.store, row, role, fire_at=fire_at, now=now) is None:
                failed += 1
        return armed, failed

    def _arm_reminders(self, row: Mapping[str, Any], key: str, now: float) -> None:
        """Arm only the reminder one-shots for a credit (D-023).

        With automatic redemption off the credit still deserves its warnings:
        a reminder reads and never spends, so it is safe to keep while the
        fire/backup pair is deliberately absent.
        """
        for role, fire_at in self._reminder_slots(row, key, now):
            ensure_slot(self.deps, self.store, row, role, fire_at=fire_at, now=now)

    def _arm_late_backup(
        self,
        row: Mapping[str, Any],
        key: str,
        backup_at: float,
        now: float,
        result: ScanResult,
    ) -> None:
        """Arm the backup when it is still safely in the future (R9).

        The primary is already inside the guard (that is why this branch ran),
        but the backup is later: a scan that crashed between the two creates
        left a credit with no second chance, and the next scan is hours away.
        """
        if backup_at <= now + MIN_LEAD_S:
            return
        if ensure_slot(self.deps, self.store, row, "backup", fire_at=backup_at, now=now) is None:
            result.failed += 1

    def _live_job(self, credit_key: str) -> bool:
        """True when a *redemption* one-shot of this credit will still fire.

        A reminder one-shot never owns a redemption: it only reads, so it must
        not stop the scan from redeeming a late credit (T-72).
        """
        jobs = {str(job.get("id")): job for job in self.deps.cron.list_jobs()}
        for slot in self.store.list_slots(credit_key=credit_key):
            if str(slot.get("role")) not in REAL_ROLES:
                continue
            if slot.get("ran_at") is not None:
                continue
            job = jobs.get(str(slot.get("job_id") or ""))
            if job is None:
                continue
            if str(job.get("state") or "") == "completed" or job.get("enabled") is False:
                continue
            return True
        return False

    def _log_schedule(
        self, credit_key: str, account_key: str, primary_at: float, backup_at: float, now: float
    ) -> None:
        """One ``schedule`` row per newly armed credit (D-014)."""
        self.store.add_history(
            [
                {
                    "at": now,
                    "action": "schedule",
                    "credit_key": credit_key,
                    "account_key": account_key,
                    "outcome": "scheduled",
                    "detail": {"primary_at": int(primary_at), "backup_at": int(backup_at)},
                }
            ]
        )

    # -- step 5: reconcile known rows --------------------------------------
    def _reconcile(
        self,
        accounts: Sequence[Any],
        lists: Mapping[str, CreditList],
        failed: Mapping[str, str],
        *,
        now: float,
        dry_run: bool,
        result: ScanResult,
    ) -> None:
        by_key = {account.account_key: account for account in accounts}
        for row in self.store.list_credits():
            key = str(row["credit_key"])
            account_key = str(row["account_key"])
            if account_key in failed:
                continue
            account = by_key.get(account_key)
            credits = lists.get(account_key)
            if account is None or credits is None:
                continue
            credit = self._find(credits, account_key, key, self.deps.salt)
            status = str(row.get("status") or "")
            if credit is not None and credit.classification == REDEEMED:
                if row.get("posted_at"):
                    # Our own POST, now confirmed by the backend: a success,
                    # never relabelled as somebody else's (R5).
                    if status != OUT_REDEEMED:
                        self.store.settle_credit(key, OUT_REDEEMED, "confirmed_redeemed", now)
                        self._success_notice(row, now)
                        if not dry_run:
                            remove_credit_jobs(self.deps, self.store, key, now)
                    continue
                if status not in (OUT_ALREADY, "redeemed"):
                    self.store.settle_credit(key, OUT_ALREADY, "already_redeemed", now)
                    self._info_notice(row, "already_redeemed", now)
                    if not dry_run:
                        remove_credit_jobs(self.deps, self.store, key, now)
                continue
            if credit is not None:
                # Still listed as available/pending: a posted row is finished
                # with the SAME UUID (A§8.2 step 5), a scheduled row may be
                # missed once its last slot is long past.
                if status == "redeeming" and row.get("posted_at") and not dry_run:
                    outcome = redeem_one(self.deps, self.store, row, role="scan", account=account)
                    result.results.append(outcome)
                    self._announce_scan(outcome, row, now)
                elif status == "scheduled" and self._missed(row, now):
                    self.store.set_credit_status(key, "missed", now, reason="missed")
                    result.missed += 1
                    result.exit_code = 1
                    self._missed_notice(row, now)
                    if not dry_run:
                        remove_credit_jobs(self.deps, self.store, key, now)
                continue
            # Absent from a successful list.
            if not getattr(credits, "readable", True):
                # A body that was never a credit list proves nothing: keep the
                # row, its UUID and its backup (R4).
                continue
            if row.get("posted_at"):
                if status != OUT_REDEEMED:
                    self.store.settle_credit(key, OUT_REDEEMED, "confirmed_absent", now)
                    result.redeemed += 1
                    self._success_notice(row, now)
                continue
            expires_at = float(row.get("expires_at") or 0.0)
            if expires_at and expires_at <= now:
                if status not in (OUT_FAILED, "expired"):
                    self.store.set_credit_status(
                        key, OUT_FAILED, now, reason="expired_before_redemption"
                    )
                    result.failed += 1
                    self._final_notice(row, "expired_before_redemption", now)
                continue
            if status not in (OUT_VANISHED,):
                self.store.set_credit_status(key, OUT_VANISHED, now, reason="vanished")
                self._info_notice(row, "vanished", now)
                if not dry_run:
                    remove_credit_jobs(self.deps, self.store, key, now)

    def _missed(self, row: Mapping[str, Any], now: float) -> bool:
        last = None
        for role in REAL_ROLES:
            slot = self.store.get_slot(str(row["credit_key"]), role)
            if slot is None:
                continue
            fire_at = float(slot["fire_at"])
            last = fire_at if last is None else max(last, fire_at)
        if last is None:
            last = float(row.get("expires_at") or 0.0) - self.deps.settings.backup_lead_seconds
        return now > last + MISS_AFTER_S

    def _mark_missed(self, row: Mapping[str, Any], now: float, result: ScanResult) -> None:
        """Report a scheduled fire that never ran, exactly once (A§8.2 step 5)."""
        key = str(row["credit_key"])
        current = self.store.get_credit(key) or row
        if str(current.get("status") or "") == "missed":
            return
        self.store.set_credit_status(key, "missed", now, reason="missed")
        result.missed += 1
        result.exit_code = 1
        self._missed_notice(current, now)
        remove_credit_jobs(self.deps, self.store, key, now)

    # -- reminder slots (T-72) ---------------------------------------------
    def _reminder_slots(
        self, row: Mapping[str, Any], key: str, now: float
    ) -> list[tuple[str, float]]:
        """``(role, fire_at)`` for each enabled reminder that can still be armed.

        A reminder is a one-shot at ``expires_at - N hours`` (D-007). One whose
        time has already passed is not armed: the scan's own reminder rule
        covers a late reminder, so a missed one-shot never becomes a burst.
        A disabled reminder's old slot is removed, so turning it off takes
        effect on the next scan.
        """
        account_key = str(row.get("account_key") or "")
        scoped = self._account_settings(account_key)
        offsets = dict(tuple(getattr(scoped, "reminder_offsets_s", ())))
        for role in REMINDER_ROLES:
            if role not in offsets:
                remove_slot(self.deps, self.store, key, role, now)
        out: list[tuple[str, float]] = []
        expires_at = row.get("expires_at")
        if expires_at is None:
            return out
        for role, offset in offsets.items():
            fire_at = float(expires_at) - float(offset)
            if fire_at <= now + MIN_LEAD_S:
                # Too late to arm: drop any stale slot so a re-enabled reminder
                # cannot fire at a time that has already passed.
                remove_slot(self.deps, self.store, key, role, now)
                continue
            out.append((role, fire_at))
        return out

    # -- T-73: the conditional 15-minute quota check (D-009) ---------------
    def quota_check(self) -> "QuotaCheckResult":
        """One conditional quota check, under the run lock (T-73).

        Armed only when ``early_redeem_enabled`` is on and at least one credit
        is still unsettled. Reads ``/wham/usage`` for accounts that actually
        hold a banked credit, honours a per-account 15-minute cooldown, and
        redeems at most the soonest-expiring available credit of an account
        through ``redeem_one(role='early')`` — same UUID, same readback.
        """
        now = float(self.deps.clock.now())
        try:
            with self.store.run_lock():
                return self._quota_check_locked(now)
        except LockTimeout as exc:
            return QuotaCheckResult(exit_code=1, problem_code=exc.code)

    def quota_armed(self) -> bool:
        """True when the quota check has anything to do (T-73 arming rule)."""
        settings = self.deps.settings
        if settings.early_redeem_threshold_pct is None and not settings.account_overrides:
            return False
        for row in self.store.list_credits():
            if str(row.get("status") or "") not in _UNSETTLED:
                continue
            account_key = str(row.get("account_key") or "")
            if not settings.account_included(account_key, self._label_of(account_key)):
                continue
            if self._account_settings(account_key).early_redeem_threshold_pct is not None:
                return True
        return False

    def _label_of(self, account_key: str) -> str:
        getter = getattr(self.store, "get_account", None)
        row = (getter(account_key) if callable(getter) else None) or {}
        return str(row.get("label") or "")

    def _account_settings(self, account_key: str) -> Settings:
        """The effective settings of one stored account (D-030)."""
        getter = getattr(self.store, "get_account", None)
        row = (getter(account_key) if callable(getter) else None) or {}
        return self.deps.settings.for_account(account_key, str(row.get("label") or ""))

    def _quota_check_locked(self, now: float) -> "QuotaCheckResult":
        result = QuotaCheckResult()
        if not self.quota_armed():
            result.problem_code = "not_armed"
            return result
        run_id = self.store.start_run("quota_check", "early", os.getpid(), now)
        self.store.heal_runs(now)
        settings = self.deps.settings
        try:
            accounts = list(self.deps.source.accounts())
        except Exception:  # noqa: BLE001 - no source is a reported problem
            result.exit_code = 1
            result.problem_code = "source_unavailable"
            self.store.finish_run(run_id, "failed", now)
            return result

        for account in accounts:
            if not settings.account_included(account.account_key, account.label):
                continue
            # D-030: each account's own early settings decide whether it is checked.
            account_settings = settings.for_account(account.account_key, account.label)
            if account_settings.early_redeem_threshold_pct is None:
                continue
            threshold = account_settings.early_redeem_threshold_pct
            rows = [
                row
                for row in self.store.list_credits(account_key=account.account_key)
                if str(row.get("status") or "") not in _SETTLED
            ]
            if not rows:
                continue  # banked accounts only: no credit, no usage read
            result.accounts += 1
            if self._cooling_down(account.account_key, now):
                result.cooled += 1
                continue
            remaining = self._weekly_remaining(account)
            if self._spent(account.account_key):
                # Re-arm only on a fresh, readable reading above the threshold.
                if remaining is None or float(remaining) <= float(threshold):
                    self.store.set_meta(_cooldown_key(account.account_key), str(now))
                    continue
                self._clear_spent(account.account_key)
            if remaining is None or float(remaining) > float(threshold):
                self.store.set_meta(_cooldown_key(account.account_key), str(now))
                continue
            self.store.set_meta(_cooldown_key(account.account_key), str(now))
            outcome = self._early_redeem_one(account, now, result, remaining=remaining)
            if outcome is None:
                continue
            result.results.append(outcome)
            if outcome.failure:
                result.failed += 1
            elif outcome.success:
                result.redeemed += 1

        self.store.finish_run(
            run_id, "failed" if result.failed else "ok", float(self.deps.clock.now())
        )
        self.store.add_history(
            [
                {
                    "at": now,
                    "action": "quota_check",
                    "role": "early",
                    "outcome": "redeemed" if result.redeemed else "checked",
                    "detail": {
                        "accounts": result.accounts,
                        "cooled": result.cooled,
                        "redeemed": result.redeemed,
                        "failed": result.failed,
                    },
                }
            ]
        )
        flush_notices(self.deps, self.store)
        return result

    def _cooling_down(self, account_key: str, now: float) -> bool:
        raw = self.store.get_meta(_cooldown_key(account_key))
        if raw is None:
            return False
        try:
            last = float(raw)
        except (TypeError, ValueError):
            return False
        return now - last < QUOTA_COOLDOWN_S

    def _spent(self, account_key: str) -> bool:
        return self.store.get_meta(_rearm_key(account_key)) == _REARM_SPENT

    def _mark_spent(self, account_key: str) -> None:
        self.store.set_meta(_rearm_key(account_key), _REARM_SPENT)

    def _clear_spent(self, account_key: str) -> None:
        self.store.set_meta(_rearm_key(account_key), "")

    def _weekly_remaining(self, account: Any) -> Optional[float]:
        """The weekly quota *remaining* percent, or ``None`` when unreadable.

        ``None`` is "unknown", never "exhausted": a failed or partial usage read
        must not authorize a spend (R15's rule, enforced again by the gate).
        """
        try:
            usage = self.deps.api.usage(account)
        except Exception:  # noqa: BLE001 - an unreadable usage never triggers
            return None
        remaining = getattr(usage, "weekly_remaining_pct", None)
        return None if remaining is None else float(remaining)

    def _early_redeem_one(
        self, account: Any, now: float, result: "QuotaCheckResult", *, remaining: Optional[float]
    ) -> Optional[RedeemResult]:
        """Redeem the account's soonest-expiring available credit, if any."""
        try:
            credits = self.deps.api.list_credits(account)
        except Exception:  # noqa: BLE001 - one account never stops the others
            return None
        rows = self.store.list_credits(account_key=account.account_key)
        if any(str(row.get("status") or "") == "redeeming" for row in rows):
            return None  # never two at once
        candidate = self._soonest_available(account, credits, now)
        if candidate is None:
            return None
        key = credit_key_for(self.deps.salt, account.account_key, candidate.id)
        row = self.store.get_credit(key)
        if row is None:
            return None
        # Persist the spend only once the gate has allowed the POST, and before
        # that POST: a crash after sending consume must not leave this account
        # armed for another early credit. A refused or dry-run attempt sends
        # nothing, so it leaves the guard unchanged (D-009a).
        account_key = account.account_key
        outcome = redeem_one(
            self.deps,
            self.store,
            row,
            role=QUOTA_ROLE,
            account=account,
            quota_reading=remaining,
            attempted=result.attempted,
            before_post=lambda: self._mark_spent(account_key),
        )
        result.attempted.add(key)
        return outcome

    def _soonest_available(self, account: Any, credits: CreditList, now: float) -> Optional[Credit]:
        settings = self.deps.settings
        best: Optional[Credit] = None
        for credit in credits:
            if credit.classification != AVAILABLE:
                continue
            if credit.expires_at is None:
                continue
            if credit.expires_at <= now + EXPIRY_MARGIN_S:
                continue
            if credit.expires_at - settings.primary_fire_offset_s <= now + MIN_LEAD_S:
                continue  # inside the lead window: the slot path owns it
            if best is None or credit.expires_at < best.expires_at:
                best = credit
        return best

    # -- notices -----------------------------------------------------------
    def _label(self, row: Mapping[str, Any], account: Any = None) -> str:
        """The account label as Hermes stores it (D-013 masks it at render)."""
        return account_label(self.deps, self.store, str(row.get("account_key") or ""), account)

    def _scheduled_notice(
        self, row: Mapping[str, Any], primary_at: float, backup_at: float, now: float
    ) -> None:
        """The §5 reminder sentence, sent once when a credit is first armed.

        §5 cut the standalone ``scheduled`` notice: what it said is exactly what
        the reminder says (the rescue time), so it renders as that kind instead
        of a second, differently-worded message about the same credit.
        """
        key = str(row["credit_key"])
        expires_at = float(row.get("expires_at") or 0.0)
        params = notice_params(
            NOTICE_SCHEDULED,
            label=self._label(row),
            expires_at=expires_at,
            fire_at=float(primary_at),
            in_hours=in_hours_text(expires_at, now),
            language=notice_language(self.deps),
        )
        dedupe = f"scheduled:{key}:{int(primary_at)}"
        self.store.enqueue_notice(
            dedupe,
            kind=NOTICE_SCHEDULED,
            message_key=NOTICE_SCHEDULED,
            params=params,
            credit_key=key,
            now=now,
        )
        enqueue = getattr(self.deps.notifier, "enqueue", None)
        if callable(enqueue):
            enqueue(NOTICE_SCHEDULED, dict(params), credit_key=key, dedupe_key=dedupe)

    def _announce_scan(self, outcome: RedeemResult, row: Mapping[str, Any], now: float) -> None:
        key = str(row["credit_key"])
        if outcome.notice_kind == "success":
            return
        previous = (self.store.get_credit(key) or {}).get("notified_outcome")
        if previous in ("success", "info", "final_failure", "missed"):
            return
        kind = outcome.notice_kind or NOTICE_INFO
        self._queue(
            key,
            kind,
            notice_params(
                kind,
                label=self._label(row),
                expires_at=float(row.get("expires_at") or 0.0),
                reason_code=outcome.reason,
                redeemed_at=now,
                language=notice_language(self.deps),
            ),
            now,
        )
        self.store.mark_notified_outcome(key, kind, now)

    def _success_notice(self, row: Mapping[str, Any], now: float) -> None:
        key = str(row["credit_key"])
        if int(row.get("notified_success") or 0):
            return
        self._queue(
            key,
            "success",
            notice_params(
                "success",
                label=self._label(row),
                expires_at=float(row.get("expires_at") or 0.0),
                redeemed_at=now,
                language=notice_language(self.deps),
            ),
            now,
        )
        self.store.mark_notified_success(key, "success", now)

    def _info_notice(self, row: Mapping[str, Any], reason: str, now: float) -> None:
        key = str(row["credit_key"])
        previous = (self.store.get_credit(key) or {}).get("notified_outcome")
        if previous in ("success", "info", "final_failure", "missed"):
            return
        self._queue(
            key,
            NOTICE_INFO,
            notice_params(
                NOTICE_INFO,
                label=self._label(row),
                expires_at=float(row.get("expires_at") or 0.0),
                reason_code=reason,
                language=notice_language(self.deps),
            ),
            now,
        )
        self.store.mark_notified_outcome(key, NOTICE_INFO, now)

    def _final_notice(self, row: Mapping[str, Any], reason: str, now: float) -> None:
        key = str(row["credit_key"])
        previous = (self.store.get_credit(key) or {}).get("notified_outcome")
        if previous in ("success", "info", "final_failure", "missed"):
            return
        self._queue(
            key,
            NOTICE_FINAL,
            notice_params(
                NOTICE_FINAL,
                label=self._label(row),
                expires_at=float(row.get("expires_at") or 0.0),
                reason_code=reason,
                language=notice_language(self.deps),
            ),
            now,
        )
        self.store.mark_notified_outcome(key, NOTICE_FINAL, now)

    def _missed_notice(self, row: Mapping[str, Any], now: float) -> None:
        key = str(row["credit_key"])
        previous = (self.store.get_credit(key) or {}).get("notified_outcome")
        if previous in ("success", "info", "final_failure", "missed"):
            return
        self._queue(
            key,
            NOTICE_MISSED,
            notice_params(
                NOTICE_MISSED,
                label=self._label(row),
                expires_at=float(row.get("expires_at") or 0.0),
                reason_code="credit_expired",
                language=notice_language(self.deps),
            ),
            now,
        )
        self.store.mark_notified_outcome(key, NOTICE_MISSED, now)

    def _account_error(self, account: Any, code: str, now: float) -> None:
        """One notice per account error transition, not every scan (A§8.2).

        The caller only reports *that* the account failed; the row is written
        here, after the previous code has been read, so the comparison means
        something. A transition is "no error → error" or "a different
        category of error" (auth, network, rate-limited, unknown): the same
        category failing twice in a row is the same thing the user was already
        told, and a scan every 12 h must not repeat it. Recovery is
        ``mark_account_listed`` clearing the code — silent, because the
        catalog has no recovery kind and a "it works again" message would be
        noise.
        """
        previous = self.store.get_account(account.account_key) or {}
        previous_code = str(previous.get("last_error_code") or "").strip()
        self.store.set_account_error(account.account_key, code, now)
        if previous_code and self._same_error_category(previous_code, code):
            return
        # The sentence names one expiry, so it names the soonest one at risk:
        # the first credit this account would have lost.
        soonest = None
        for row in self.store.list_credits(account_key=account.account_key):
            if str(row.get("status") or "") in _SETTLED:
                continue
            expires_at = row.get("expires_at")
            if expires_at is None:
                continue
            soonest = float(expires_at) if soonest is None else min(soonest, float(expires_at))
        self._queue(
            f"account:{account.account_key}",
            NOTICE_ACCOUNT,
            notice_params(
                NOTICE_ACCOUNT,
                label=account.label,
                expires_at=soonest,
                language=notice_language(self.deps),
            ),
            now,
        )

    @staticmethod
    def _same_error_category(first: str, second: str) -> bool:
        """True when two codes render the same account-error sentence.

        The category is what the notice says, so it is also what decides
        whether a new notice is warranted: ``network`` → ``timeout`` is a
        different code but the same sentence, and ``auth_expired`` →
        ``network`` is a real change the user must hear about. Asking the
        catalog keeps the rule in one place instead of a second copy of the
        code→category table.
        """
        return messages.account_error_text(first) == messages.account_error_text(second)

    def _queue(self, key: str, kind: str, params: Mapping[str, Any], now: float) -> None:
        dedupe = f"{kind}:{key}:{int(now)}"
        self.store.enqueue_notice(
            dedupe,
            kind=kind,
            message_key=kind,
            params=params,
            credit_key=None if key.startswith("account:") else key,
            now=now,
        )
        enqueue = getattr(self.deps.notifier, "enqueue", None)
        if callable(enqueue):
            enqueue(
                kind,
                dict(params),
                credit_key=None if key.startswith("account:") else key,
                dedupe_key=dedupe,
            )

    # -- step: excluded accounts -------------------------------------------
    def _drop_excluded(self, accounts: Sequence[Any], settings: Any, now: float) -> None:
        """Remove the slots of every account the user excluded (R10).

        ``account_included`` is asked with the label too: an exclusion written
        by label is the same instruction as one written by key, and leaving the
        old jobs armed after a scope reduction is exactly the bug R10 found.
        """
        for account in accounts:
            if settings.account_included(account.account_key, account.label):
                continue
            for row in self.store.list_credits(account_key=account.account_key):
                if self.store.list_slots(credit_key=str(row["credit_key"])):
                    remove_credit_jobs(self.deps, self.store, str(row["credit_key"]), now)

    @staticmethod
    def _find(credits: CreditList, account_key: str, key: str, salt: str) -> Optional[Credit]:
        for credit in credits:
            if credit_key_for(salt, account_key, credit.id) == key:
                return credit
        return None
