"""The redemption flow: one credit, decided from the backend (A§8.3, T-18a).

``redeem_one`` is the single place that can spend a credit. It is shared by the
primary/backup one-shots, the late scan and ``redeem now``, so every path has
exactly the same semantics:

1. fresh ``GET`` of the account's credit list (I2 — local status never decides);
2. classify: absent/pending/available/redeemed/unknown (I1);
3. for available or pending: persist ``redeeming`` + the idempotency UUID
   **before** the POST, then ``POST consume`` with that UUID (I2);
4. readback: only a successful list that no longer shows the credit as
   available/pending confirms success (I1). An unreadable readback is
   ``unconfirmed``, never success (R4).

Roles come from the job (I3): ``Fire.run(role)`` handles only the slots of its
own role, never an early window, and a lock wait never turns a primary into the
backup (R1). ``role='remind'`` is the reminder one-shot (T-72): it only ever
reads, over a ``ReadOnlyTransport``, and cannot spend a credit. A slot that
already ran is never handled again (R9), and a slot older than ``MAX_LATE_S``
is left to the planner's ``missed`` rule.

Nothing here removes a job: GC is a separate, time-bound step (I7/R7/R8).
"""

from __future__ import annotations

import datetime
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from . import messages
from .accounts import canonical_label
from .authorize import authorize_consume
from .classify import REDEEMED, UNKNOWN, Credit, CreditList
from .codex_api import (
    REASON_ALREADY_REDEEMED,
    REASON_HTTP,
    REASON_INELIGIBLE,
    REASON_NO_CREDIT,
    REASON_NOTHING_TO_RESET,
    REASON_RESET,
    REASON_UNKNOWN,
    ConsumeResult,
)
from .cron_gateway import SCRIPT_FOR_ROLE, SLOT_SCRIPT_ROLE, build_slot_spec, slot_job_name
from .keys import credit_key as derive_credit_key
from .store import LockTimeout, StateError, Store

__all__ = [
    "Fire",
    "RedeemResult",
    "account_label",
    "credit_key_for",
    "ensure_slot",
    "gc_jobs",
    "notice_params",
    "HORIZON_S",
    "MIN_LEAD_S",
    "DUE_SLACK_S",
    "EXPIRY_MARGIN_S",
    "MISS_AFTER_S",
    "MAX_LATE_S",
    "SCRIPT_TIMEOUT_S",
    "GC_MARGIN_S",
    "REAL_ROLES",
    "ROLE_REMIND",
    "ROLE_MANUAL",
    "REMINDER_ROLES",
]

#: Only schedule inside this horizon (A§8).
HORIZON_S = 48 * 3_600.0
#: A slot closer than this is never created; the scan redeems late instead.
MIN_LEAD_S = 3 * 60.0
#: A slot is due from its own time, with this much slack for a late ticker.
DUE_SLACK_S = 30.0
#: Never POST past ``expires_at - this``.
EXPIRY_MARGIN_S = 60.0
#: A scheduled fire this late never ran: the planner reports it once.
MISS_AFTER_S = 15 * 60.0
#: A fire handles its slot only this long after its own time (R9D).
MAX_LATE_S = 15 * 60.0
#: POST retries: 3 tries, backoff 5/15/30 s, never past ``expires_at - 30 s``.
MAX_POST_ATTEMPTS = 3
RETRY_DELAYS_S = (5.0, 15.0, 30.0)
POST_DEADLINE_MARGIN_S = 30.0
#: The scheduler's script timeout and the GC margin on top of it (A§7.5).
SCRIPT_TIMEOUT_S = 3_600.0
GC_MARGIN_S = 600.0
REAL_ROLES = ("primary", "backup")
#: The reminder one-shot role (T-72). Never a redemption role.
ROLE_REMIND = "remind"
#: The explicit, user-confirmed redemption role (D-015).
ROLE_MANUAL = "manual"
REMINDER_ROLES = ("reminder1", "reminder2")

#: Transport codes that a retry may fix.
TRANSIENT_CODES = frozenset({"timeout", "network", "transient"})

#: Notice kinds (A§8.6).
NOTICE_SCHEDULED = "scheduled"
NOTICE_REMINDER = "reminder"
NOTICE_SUCCESS = "success"
NOTICE_INFO = "info"
NOTICE_RETRY = "retry_pending"
NOTICE_FINAL = "final_failure"
NOTICE_ACCOUNT = "account_error"
NOTICE_MISSED = "missed"
NOTICE_DRY_RUN = "dry_run"

#: Outcomes a credit row may carry after a run.
OUT_REDEEMED = "redeemed"
OUT_ALREADY = "already_redeemed"
OUT_NOTHING = "nothing_to_reset"
OUT_VANISHED = "vanished"
OUT_FAILED = "failed"
OUT_UNCONFIRMED = "unconfirmed"

#: Outcomes that close a credit for good: a later slot must not repeat them.
FINAL_OUTCOMES = frozenset({"success", "info", "final_failure", "missed"})


def credit_key_for(salt: str, account_key: str, credit_id: str) -> str:
    """The stored key of one backend credit (A§4.5)."""
    return derive_credit_key(salt, account_key, credit_id)


@dataclass(frozen=True)
class RedeemResult:
    """What one ``redeem_one`` did, in fixed codes only (never a raw body)."""

    credit_key: str
    outcome: str
    reason: str = ""
    confirmed: bool = False
    windows_reset: int = 0
    usage: Optional[Mapping[str, Any]] = None
    notice_kind: str = ""
    failure: bool = False
    attempts: int = 0
    posted: bool = False

    @property
    def success(self) -> bool:
        return self.outcome in (OUT_REDEEMED, OUT_ALREADY)


# --------------------------------------------------------------------- helpers


def _now(deps: Any) -> float:
    return float(deps.clock.now())


def _account_for(deps: Any, account_key: str):
    """The live ``AccountRef`` for a stored key, or ``None`` when it is gone."""
    try:
        accounts = deps.source.accounts()
    except Exception:  # noqa: BLE001 - a source failure is "account unavailable"
        return None
    for ref in accounts:
        if ref.account_key == account_key:
            return ref
    return None


def _find(credits: CreditList, salt: str, account_key: str, key: str) -> Optional[Credit]:
    for credit in credits:
        if credit_key_for(salt, account_key, credit.id) == key:
            return credit
    return None


def _is_transient(code: str) -> bool:
    return code in TRANSIENT_CODES or code == "auth_expired"


def account_label(deps: Any, store: Store, account_key: str, account: Any = None) -> str:
    """The account label to put in a notice, masked or full per D-013.

    A notice is about an account, so ``label`` is the account's label and never
    the credit's title: masking only knows how to hide a label, and "Full reset"
    tells the user nothing about which account was rescued.

    Two rules meet here:

    * the label Hermes stores is **masked** (``b***@example.com``), because a
      pool row's label is already masked by the source;
    * when the user turned masking off, the full label is resolved from the
      source's ``full_label`` **in memory, at send time** (R16) — turning the
      setting off must actually unmask the notice, and the store keeps only the
      masked string.

    The live ``AccountRef`` wins (freshest), then the row the scan stored, then
    the source. An empty answer is honest: the renderer shows the rest of the
    sentence rather than a wrong account.

    **This is the presentation label and nothing else (R10).** It may be the
    full address when masking is off, so it must never be handed to a decision:
    use :func:`canonical_account_identity` for anything that decides whether an
    account may be spent.
    """
    if _masking_off(deps):
        full = _full_label(deps, account_key, account)
        if full:
            return full
    return stored_label(store, account_key, account)


def stored_label(store: Any, account_key: str, account: Any = None) -> str:
    """The label as Hermes stores it: masked, never the D-013 unmask (R10/R16).

    One function builds it, so the notice text, the outbox row and the
    authorization identity cannot drift apart. The live ``AccountRef`` is
    freshest, then the row the scan stored, then the source.
    """
    label = str(getattr(account, "label", "") or "").strip()
    if label:
        return label
    getter = getattr(store, "get_account", None)
    if callable(getter):
        try:
            found = getter(str(account_key))
        except Exception:  # noqa: BLE001 - a label is never worth failing a flow
            found = None
        if isinstance(found, Mapping):
            label = str(found.get("label") or "").strip()
            if label:
                return label
    return ""


def canonical_account_identity(store: Any, account_key: str, account: Any = None) -> Any:
    """The one canonical identity a **decision** may use (R10).

    Delegates to :func:`reset_rescue.accounts.canonical_identity`, which reads
    the stored label and re-masks it. Kept here as the flow-facing name so a
    caller cannot accidentally reach for ``account_label`` (presentation)
    instead.
    """
    from .accounts import canonical_identity

    return canonical_identity(store, account_key, account)


def _masking_off(deps: Any) -> bool:
    """True only when ``mask_labels`` is explicitly off (D-013)."""
    settings = getattr(deps, "settings", None)
    return getattr(settings, "mask_labels", True) is False


def _full_label(deps: Any, account_key: str, account: Any = None) -> str:
    """The unmasked label from the source, in memory only (``""`` if unknown)."""
    resolver = getattr(deps.source, "full_label", None)
    if callable(resolver):
        try:
            value = resolver(str(account_key))
        except Exception:  # noqa: BLE001 - a missing full label is not a failure
            value = ""
        if isinstance(value, str) and value.strip():
            return value.strip()
    full = getattr(account, "full_label", "")
    return full.strip() if isinstance(full, str) else ""


def notice_params(
    kind: str,
    *,
    label: str,
    expires_at: Any = None,
    reason_code: Any = "",
    redeemed_at: Any = None,
    retry_at: Any = None,
    fire_at: Any = None,
    in_hours: Any = None,
    language: str = messages.DEFAULT_LANGUAGE,
) -> dict:
    """The catalog's parameters for ``kind``, and only those (T-20, A§8.6).

    ``messages.PARAMS`` is the contract: a key it does not declare makes the
    renderer refuse the whole notice, which is how a notice silently becomes an
    empty message. Building every notice's parameters here is what keeps the
    flows and the catalog in step.

    ``label`` is reduced to the **canonical** label here (R10/R16): masked, as
    the plugin stores it. These parameters are what the outbox serializes, so
    the full address must never enter them — the notifier resolves it in memory
    at send time from this masked label. A caller therefore cannot leak the
    unmask into SQLite by handing a full label to this function.
    """
    canonical = messages.KIND_ALIASES.get(kind, kind)
    declared = messages.PARAMS.get(canonical, ())
    values: dict[str, Any] = {
        "label": canonical_label(label),
        "expires_at": expires_at,
        "redeemed_at": redeemed_at,
        "retry_at": retry_at,
        "fire_at": fire_at,
        "in_hours": in_hours,
        "reason": messages.reason_text(reason_code, language) if reason_code else "",
    }
    return {name: values[name] for name in declared}


def in_hours_text(expires_at: Any, reference: Any) -> str:
    """``24h`` / ``90min`` — the time left, as §5's reminder sentence says it."""
    try:
        seconds = float(expires_at) - float(reference)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return "0min"
    if seconds >= 3_600.0:
        return f"{int(round(seconds / 3_600.0))}h"
    return f"{int(round(seconds / 60.0))}min"


def notice_language(deps: Any) -> str:
    """The language the notifier will render in (D-016), for fixed phrases."""
    language = str(getattr(getattr(deps, "notifier", None), "language", "") or "")
    return language if language in messages.LANGUAGES else messages.DEFAULT_LANGUAGE


def _notice(
    deps: Any,
    store: Store,
    *,
    credit_key: str,
    kind: str,
    params: Mapping[str, Any],
    dedupe_key: str,
) -> None:
    """Queue one notice in the outbox (I6) and hand it to the notifier.

    Called inside the caller's transaction so the outbox row and the credit's
    state change commit together (I6): a crash between them would either lose
    the notice or repeat it.
    """
    store.enqueue_notice(
        dedupe_key, kind=kind, message_key=kind, params=params, credit_key=credit_key
    )
    enqueue = getattr(deps.notifier, "enqueue", None)
    if callable(enqueue):
        enqueue(kind, dict(params), credit_key=credit_key, dedupe_key=dedupe_key)


def _next_slot_at(store: Store, credit_key: str, role: str, now: float) -> Optional[float]:
    """When the *other* one-shot of this credit will still try, if it can."""
    other = "backup" if role == "primary" else "primary"
    slot = store.get_slot(credit_key, other)
    if slot is None or slot.get("ran_at") is not None:
        return None
    fire_at = float(slot["fire_at"])
    return fire_at if fire_at > now else None


def _finish(
    deps: Any,
    store: Store,
    row: Mapping[str, Any],
    *,
    role: str,
    kind: str,
    outcome: str,
    reason: str,
    now: float,
    settle: bool = False,
    success: bool = False,
    confirmed: bool = False,
    windows_reset: int = 0,
    usage: Optional[Mapping[str, Any]] = None,
    failure: bool = False,
    attempts: int = 0,
    posted: bool = False,
    account: Any = None,
) -> RedeemResult:
    """Record the verdict and its notice in one transaction (I1/I6/R9)."""
    credit_key = str(row["credit_key"])
    previous = (store.get_credit(credit_key) or {}).get("notified_outcome")
    already_announced = previous in FINAL_OUTCOMES
    label = account_label(deps, store, str(row["account_key"]), account)
    expires_at = float(row.get("expires_at") or 0.0)
    ahead: Optional[float] = None
    if kind == NOTICE_RETRY:
        ahead = _next_slot_at(store, credit_key, role, now)
        if ahead is None:
            kind = NOTICE_FINAL
    if kind == NOTICE_SUCCESS and int(row.get("notified_success") or 0):
        kind = ""  # announced exactly once (I6)
    elif kind != NOTICE_SUCCESS and already_announced:
        kind = ""  # this outcome was already reported; never repeat it (R9)
    params: dict[str, Any] = {}
    if kind:
        params = notice_params(
            kind,
            label=label,
            expires_at=expires_at,
            reason_code=reason,
            redeemed_at=now if kind == NOTICE_SUCCESS else None,
            retry_at=ahead if kind == NOTICE_RETRY else None,
            language=notice_language(deps),
        )
    with store.transaction():
        if settle:
            store.settle_credit(credit_key, outcome, reason, now)
        else:
            store.fail_credit(
                credit_key, reason, "transient" if kind == NOTICE_RETRY else "definitive", now
            )
        if kind:
            _notice(
                deps,
                store,
                credit_key=credit_key,
                kind=kind,
                params=params,
                dedupe_key=f"{kind}:{credit_key}:{int(row.get('attempts') or 0)}",
            )
            if kind == NOTICE_SUCCESS:
                store.mark_notified_success(credit_key, "success", now)
            else:
                store.mark_notified_outcome(credit_key, kind, now)
    return RedeemResult(
        credit_key,
        outcome,
        reason=reason,
        confirmed=confirmed,
        windows_reset=int(windows_reset or 0),
        usage=usage,
        notice_kind=kind,
        failure=failure,
        attempts=attempts,
        posted=posted,
    )


def flush_notices(deps: Any, store: Store) -> list[Any]:
    """Deliver queued notices; a delivery failure never fails the run (I6)."""
    flush = getattr(deps.notifier, "flush", None)
    if not callable(flush):
        return []
    try:
        delivered = list(flush() or [])
    except Exception:  # noqa: BLE001 - the outbox keeps the row for the next run
        return []
    for item in delivered:
        if not isinstance(item, Mapping):
            continue
        dedupe = item.get("dedupe_key")
        if not dedupe:
            continue
        for row in store.list_notices(state="queued"):
            if row["dedupe_key"] == dedupe:
                store.mark_notice_sent(int(row["id"]), None, _now(deps))
                break
    return delivered


def _reasons_for(result: ConsumeResult) -> tuple[str, bool]:
    """``(reason, transient)`` for a consume result that is not a success."""
    if result.reason == REASON_NO_CREDIT:
        return "no_credit", False
    if result.reason == REASON_INELIGIBLE:
        return "rate_limit_reset_ineligible", False
    if result.reason == REASON_NOTHING_TO_RESET:
        return "nothing_to_reset", False
    if result.reason == REASON_UNKNOWN:
        return "unknown_response", False
    if result.reason == REASON_HTTP:
        transient = bool(result.status and 500 <= int(result.status) <= 599)
        return "http_error", transient
    return result.reason or "backend_error", True


# ----------------------------------------------------------------- redeem_one


def redeem_one(
    deps: Any,
    store: Store,
    row: Mapping[str, Any],
    *,
    role: str,
    account: Any = None,
    dry_run: bool = False,
    confirm: Optional[bool] = None,
    quota_reading: Optional[float] = None,
    timeout: Optional[float] = None,
    attempted: Optional[set] = None,
    before_post: Optional[Callable[[], None]] = None,
) -> RedeemResult:
    """Decide from the backend and, when needed, spend *this* credit (I1/I2).

    ``dry_run`` is this operation's explicit flag; the persistent setting is
    read by the gate, so either one pauses the spend (R1). ``confirm`` and
    ``quota_reading`` are the manual and quota roles' own evidence, checked by
    the same gate (R2/R15). ``attempted`` is the calling run's set of credit
    keys it already POSTed for: a credit in that set is never posted a second
    time by the same run, however many decision paths the run has. ``before_post``
    runs only once the gate has allowed the POST, right before the intent is
    persisted, so a caller can record a spend that is about to happen (and never
    a refused or dry-run attempt that sends nothing).
    """
    credit_key = str(row["credit_key"])
    account_key = str(row["account_key"])
    expires_at = float(row.get("expires_at") or 0.0)
    now = _now(deps)

    if account is None:
        account = _account_for(deps, account_key)
    if account is None:
        return _finish(
            deps,
            store,
            row,
            role=role,
            account=account,
            kind=NOTICE_FINAL,
            outcome=OUT_FAILED,
            reason="account_missing",
            now=now,
            failure=True,
        )

    try:
        credits = deps.api.list_credits(account)
    except Exception as exc:  # noqa: BLE001 - classified, never re-raised
        code = str(getattr(exc, "code", "") or "network")
        return _finish(
            deps,
            store,
            row,
            role=role,
            account=account,
            kind=NOTICE_RETRY if _is_transient(code) else NOTICE_FINAL,
            outcome=OUT_FAILED,
            reason=code,
            now=now,
            failure=True,
        )

    credit = _find(credits, deps.salt, account_key, credit_key)
    if credit is None:
        readable = bool(getattr(credits, "readable", True))
        if row.get("posted_at") and readable:
            # Absent after a consume was sent, from a list that really was a
            # list: that IS the confirmation (I1).
            usage = _usage(deps, account)
            return _finish(
                deps,
                store,
                row,
                role=role,
                account=account,
                kind=NOTICE_SUCCESS,
                outcome=OUT_REDEEMED,
                reason="confirmed_absent",
                now=now,
                settle=True,
                confirmed=True,
                usage=usage,
            )
        if not readable:
            # The body was not a credit list at all: absence proves nothing, so
            # the credit keeps its UUID and the backup keeps its chance (R4).
            return _finish(
                deps,
                store,
                row,
                role=role,
                account=account,
                kind=NOTICE_RETRY,
                outcome=OUT_UNCONFIRMED,
                reason="unconfirmed",
                now=now,
                failure=True,
            )
        return _finish(
            deps,
            store,
            row,
            role=role,
            account=account,
            kind=NOTICE_INFO,
            outcome=OUT_VANISHED,
            reason="vanished",
            now=now,
            settle=True,
        )

    if credit.classification == REDEEMED:
        usage = _usage(deps, account)
        if row.get("posted_at"):
            # We posted and the backend now says redeemed: that is OUR success,
            # not somebody else's. Never downgrade it to ``already_redeemed``
            # (which would announce an info instead of the success, R5).
            return _finish(
                deps,
                store,
                row,
                role=role,
                account=account,
                kind=NOTICE_SUCCESS,
                outcome=OUT_REDEEMED,
                reason="confirmed_redeemed",
                now=now,
                settle=True,
                confirmed=True,
                usage=usage,
            )
        return _finish(
            deps,
            store,
            row,
            role=role,
            account=account,
            kind=NOTICE_INFO,
            outcome=OUT_ALREADY,
            reason="already_redeemed",
            now=now,
            settle=True,
            confirmed=True,
            usage=usage,
        )

    if credit.classification == UNKNOWN:
        # Listed, but neither available, pending nor redeemed: never success.
        return _finish(
            deps,
            store,
            row,
            role=role,
            account=account,
            kind=NOTICE_FINAL,
            outcome=OUT_FAILED,
            reason="unavailable_unknown",
            now=now,
            failure=True,
        )

    if attempted is not None and credit_key in attempted:
        # This run already spent an attempt on this credit through another
        # decision path; a second POST in the same run is never right.
        return RedeemResult(
            credit_key, OUT_UNCONFIRMED, reason="already_attempted", notice_kind="", failure=False
        )

    # THE money boundary (see ``authorize.py``): one function, called right
    # before the one consume call site, with the FRESH credit of this run. No
    # caller may decide to spend on its own, and no rule lives outside it.
    #
    # The gate gets the **canonical** identity, never the presentation label
    # (R10): with ``mask_labels`` off the presentation label is the full
    # address, and an exclusion written with the masked label the plugin stores
    # would silently stop matching.
    identity = canonical_account_identity(store, account_key, account)
    decision = authorize_consume(
        role=role,
        credit=credit,
        settings=getattr(deps, "settings", None),
        now=now,
        account_key=identity.account_key,
        label=identity.label,
        confirm=confirm,
        dry_run=bool(dry_run),
        quota_reading=quota_reading,
    )
    if not decision.allowed:
        if decision.reason == "dry_run":
            return RedeemResult(credit_key, "dry_run", reason="dry_run", notice_kind=NOTICE_DRY_RUN)
        # A refusal is not a failure of the credit: nothing was sent, the row
        # keeps its state and the UUID, and the backup is untouched.
        return RedeemResult(
            credit_key, OUT_UNCONFIRMED, reason=decision.reason, notice_kind="", failure=False
        )

    # Persist the intent BEFORE the POST: a crash leaves a retryable
    # ``redeeming`` row and the same UUID is reused by the next attempt (I2).
    if before_post is not None:
        before_post()
    request_id = store.begin_redeem(credit_key, now)
    if attempted is not None:
        attempted.add(credit_key)
    deadline = (
        min(expires_at - POST_DEADLINE_MARGIN_S, expires_at - EXPIRY_MARGIN_S)
        if expires_at
        else now + 3_600.0
    )

    result, transport_code = _post_with_retries(
        deps, account, credit.id, request_id, deadline=deadline
    )
    attempts = int((store.get_credit(credit_key) or {}).get("attempts") or 1)
    now = _now(deps)

    if result is None:
        transient = transport_code in TRANSIENT_CODES
        return _finish(
            deps,
            store,
            row,
            role=role,
            account=account,
            kind=NOTICE_RETRY if transient else NOTICE_FINAL,
            outcome=OUT_FAILED,
            reason=transport_code,
            now=now,
            failure=True,
            attempts=attempts,
            posted=True,
        )

    if result.reason == REASON_NOTHING_TO_RESET:
        return _finish(
            deps,
            store,
            row,
            role=role,
            account=account,
            kind=NOTICE_INFO,
            outcome=OUT_NOTHING,
            reason="nothing_to_reset",
            now=now,
            settle=True,
            attempts=attempts,
            posted=True,
        )

    if result.reason in (REASON_RESET, REASON_ALREADY_REDEEMED):
        verdict = _confirm(deps, account, credit_key)
        if verdict != "confirmed":
            # Unreadable or still listed: pending, never success (R4/R5).
            return _finish(
                deps,
                store,
                row,
                role=role,
                account=account,
                kind=NOTICE_RETRY,
                outcome=OUT_UNCONFIRMED,
                reason="unconfirmed",
                now=now,
                failure=True,
                attempts=attempts,
                posted=True,
            )
        usage = _usage(deps, account)
        return _finish(
            deps,
            store,
            row,
            role=role,
            account=account,
            kind=NOTICE_SUCCESS,
            outcome=OUT_REDEEMED,
            reason=result.reason,
            now=now,
            settle=True,
            confirmed=True,
            windows_reset=result.windows_reset,
            usage=usage,
            attempts=attempts,
            posted=True,
        )

    reason, transient = _reasons_for(result)
    return _finish(
        deps,
        store,
        row,
        role=role,
        account=account,
        kind=NOTICE_RETRY if transient else NOTICE_FINAL,
        outcome=OUT_FAILED,
        reason=reason,
        now=now,
        failure=True,
        attempts=attempts,
        posted=True,
    )


#: Credit statuses that make a reminder pointless: the credit is already done.
_SETTLED_FOR_REMINDER = frozenset(
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


def reminder_meta_key(credit_key: str, role: str) -> str:
    """The ``meta`` key that records one reminder as sent (never a secret)."""
    return f"reminder_sent:{credit_key}:{role}"


def _log_action(action: str, result: RedeemResult) -> str:
    """Map a flow action onto the activity-log vocabulary (D-014)."""
    if action == "redeem_now":
        return "redemption"
    if action == "fire":
        return "redemption" if result.posted else "fire"
    if action in ("scan", "remind", "quota_check"):
        return action
    return "redemption"


def _post_with_retries(
    deps: Any, account: Any, credit_id: str, request_id: str, *, deadline: float
) -> tuple[Optional[ConsumeResult], str]:
    """POST consume, retrying only what a retry may fix, always with the same UUID."""
    attempts = 0
    while True:
        attempts += 1
        if _now(deps) >= deadline:
            return None, "expired_before"
        try:
            result = deps.api.consume(account, credit_id, request_id)
        except Exception as exc:  # noqa: BLE001 - classified below
            code = str(getattr(exc, "code", "") or "network")
            if code in TRANSIENT_CODES and attempts < MAX_POST_ATTEMPTS:
                delay = RETRY_DELAYS_S[min(attempts - 1, len(RETRY_DELAYS_S) - 1)]
                if _now(deps) + delay >= deadline:
                    return None, "expired_before"
                deps.wait(delay)
                continue
            return None, code
        if (
            result.reason == REASON_HTTP
            and result.status
            and 500 <= int(result.status) <= 599
            and attempts < MAX_POST_ATTEMPTS
        ):
            delay = RETRY_DELAYS_S[min(attempts - 1, len(RETRY_DELAYS_S) - 1)]
            if _now(deps) + delay >= deadline:
                return None, "expired_before"
            deps.wait(delay)
            continue
        return result, ""


def _confirm(deps: Any, account: Any, credit_key: str) -> str:
    """``confirmed`` / ``unconfirmed`` after a consume (I1, R4).

    Only a **readable** list may confirm by absence: a malformed 200 is an
    unreadable readback, so the credit stays ``redeeming`` with its UUID and the
    backup keeps its chance.
    """
    try:
        credits = deps.api.list_credits(account)
    except Exception:  # noqa: BLE001 - an unreadable readback cannot confirm
        return "unconfirmed"
    if not getattr(credits, "readable", True):
        return "unconfirmed"
    credit = _find(credits, deps.salt, account.account_key, credit_key)
    if credit is None:
        return "confirmed"
    return "confirmed" if credit.classification == REDEEMED else "unconfirmed"


def _usage(deps: Any, account: Any) -> Optional[Mapping[str, Any]]:
    try:
        usage = deps.api.usage(account)
    except Exception:  # noqa: BLE001 - usage is display only
        return None
    as_dict = getattr(usage, "as_dict", None)
    if callable(as_dict):
        return dict(as_dict())
    return dict(usage) if isinstance(usage, Mapping) else None


# ---------------------------------------------------------------- slots / jobs


def _iso(epoch: float) -> str:
    return datetime.datetime.fromtimestamp(float(epoch)).astimezone().isoformat(timespec="seconds")


def _target(deps: Any) -> str:
    settings = getattr(deps, "settings", None)
    return str(getattr(settings, "notify_target", "") or "local")


def _script_for(role: str) -> str:
    """The wrapper file one slot role runs (A§7.2, T-72)."""
    script_role = SLOT_SCRIPT_ROLE.get(role)
    if script_role is None:
        raise ValueError(f"unknown slot role {role!r}")
    return SCRIPT_FOR_ROLE[script_role]


def ensure_slot(
    deps: Any,
    store: Store,
    row: Mapping[str, Any],
    role: str,
    *,
    fire_at: float,
    now: float,
) -> Optional[dict]:
    """Create, repair or re-time one slot's one-shot (A§7.4, R7/R9).

    Idempotent within 60 s. The slot is reconciled **on its own**: the live job
    is joined by the id the store recorded, and it must still be armed and
    still match the spec. A missing, terminal, renamed or retimed job is
    replaced here, whatever the other slot of the same credit is doing — a
    live primary never hides a missing backup (R9).
    """
    credit_key = str(row["credit_key"])
    name = slot_job_name(role, credit_key)
    spec = build_slot_spec(
        role=role,
        key=credit_key,
        fire_at_iso=_iso(fire_at),
        deliver=_target(deps),
        failure_deliver=_target(deps),
    )
    slot = store.get_slot(credit_key, role)
    try:
        jobs = {str(job.get("id")): job for job in deps.cron.list_jobs()}
    except Exception:  # noqa: BLE001 - an unreadable list is never "no job"
        return None
    job = jobs.get(str(slot["job_id"])) if slot and slot.get("job_id") else None
    if job is not None and _job_matches(job, spec, fire_at=fire_at):
        if store.get_job_by_name(name) is None:
            store.record_job(str(job["id"]), name, role, spec.script, now)
        store.upsert_slot(
            credit_key,
            role,
            fire_at=float(fire_at),
            job_id=str(job["id"]),
            job_name=name,
            state="armed",
            deliver=_target(deps),
            now=now,
        )
        return store.get_slot(credit_key, role)

    if job is not None:
        # A job that exists but is not what this slot needs: remove it first.
        if not _remove_job(deps, store, str(job["id"]), now):
            return None
    elif slot is not None and slot.get("job_id"):
        # Recorded but not listed: the scheduler lost it. Forget the id, keep
        # the slot (its fire time is still the plan) and recreate below.
        store.mark_job_removed(str(slot["job_id"]), now)

    try:
        created = deps.cron.create(spec)
    except Exception:  # noqa: BLE001 - a failed create is a reported failure
        return None
    if created is None:
        return None
    store.record_job(str(created.job_id), name, role, spec.script, now)
    row_now = store.upsert_slot(
        credit_key,
        role,
        fire_at=float(fire_at),
        job_id=str(created.job_id),
        job_name=name,
        state="armed",
        deliver=_target(deps),
        now=now,
    )
    # A brand-new one-shot is a new chance: the old ``ran_at`` belonged to the
    # job we just replaced, and keeping it would make the new job a no-op.
    if slot is not None and slot.get("ran_at") is not None:
        store.clear_slot_ran(credit_key, role, now)
    return row_now


def _job_matches(job: Mapping[str, Any], spec: Any, *, fire_at: float) -> bool:
    """True when the live job is still the armed one-shot this slot wants."""
    if str(job.get("name") or "") != spec.name:
        return False
    if job.get("enabled") is False:
        return False
    if str(job.get("state") or "") in ("completed", "error", "paused", "disabled"):
        return False
    script = str(job.get("script") or "")
    if script and script != spec.script:
        return False
    deliver = str(job.get("deliver") or "")
    if deliver and deliver != spec.deliver:
        return False
    scheduled = _parse_schedule(str(job.get("schedule") or ""))
    if scheduled is not None and abs(scheduled - float(fire_at)) > 60.0:
        return False
    return True


def _parse_schedule(text: str) -> Optional[float]:
    """The epoch of a rendered one-shot schedule (``once at …``), else ``None``.

    ``hermes cron list`` renders a one-shot as ``once at 2026-11-07 05:49`` in
    the profile's zone. A schedule that cannot be read is not evidence of
    drift: the caller treats ``None`` as "cannot tell" and keeps the job.
    """
    value = " ".join(str(text or "").split())
    if not value.lower().startswith("once at "):
        return None
    stamp = value[len("once at ") :]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.datetime.strptime(stamp, fmt).astimezone().timestamp()
        except ValueError:
            continue
    try:
        return datetime.datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return None


def _remove_job(deps: Any, store: Store, job_id: str, now: Optional[float] = None) -> bool:
    """Remove one owned job and forget it **only** when it is really gone.

    ``False`` means the scheduler still lists it (or the removal could not be
    verified): the caller keeps the slot, the ownership row and the credit, so
    the next run retries instead of reporting a cleanup that did not happen
    (R13).
    """
    now = _now(deps) if now is None else float(now)
    owned = {str(row["job_id"]) for row in store.list_owned_jobs(active_only=True)}
    try:
        removed = deps.cron.remove(job_id, owned=owned)
    except Exception:  # noqa: BLE001 - an unverifiable removal is a failure
        return False
    if not removed:
        return False
    store.mark_job_removed(job_id, now)
    return True


def remove_credit_jobs(
    deps: Any, store: Store, credit_key: str, now: Optional[float] = None
) -> list[str]:
    """Remove every owned one-shot of one credit; returns the ids still live."""
    now = _now(deps) if now is None else float(now)
    stuck: list[str] = []
    for slot in store.list_slots(credit_key=credit_key):
        job_id = str(slot.get("job_id") or "")
        if job_id and not _remove_job(deps, store, job_id, now):
            stuck.append(job_id)
            continue
        store.delete_slot(credit_key, str(slot["role"]))
    return stuck


def remove_slot(
    deps: Any, store: Store, credit_key: str, role: str, now: Optional[float] = None
) -> bool:
    """Remove one owned one-shot of one credit and its slot row."""
    now = _now(deps) if now is None else float(now)
    slot = store.get_slot(credit_key, role)
    if slot is None:
        return True
    job_id = str(slot.get("job_id") or "")
    if job_id and not _remove_job(deps, store, job_id, now):
        return False
    store.delete_slot(credit_key, role)
    return True


def _slot_fire_at(store: Store, job_id: str) -> Optional[float]:
    for slot in store.list_slots():
        if str(slot.get("job_id") or "") == job_id:
            return float(slot["fire_at"])
    return None


def gc_jobs(
    deps: Any, store: Store, *, protect: Sequence[str] = (), now: Optional[float] = None
) -> list[str]:
    """Remove finished owned one-shots, bound to process exit (I7/R7/R8).

    A job goes only when the scheduler reports it completed/disabled **and** its
    fire time is older than the script timeout plus ten minutes, so the
    scheduler has already killed or finished any run of it. ``protect`` holds
    the ids of this run's own slots and of every still-pending slot.
    """
    now = _now(deps) if now is None else float(now)
    protected = {str(job_id) for job_id in protect if job_id}
    try:
        jobs = {str(job.get("id")): job for job in deps.cron.list_jobs()}
    except Exception:  # noqa: BLE001 - cleanup is never fatal
        return []
    removed: list[str] = []
    for owned in store.list_owned_jobs(active_only=True):
        job_id = str(owned["job_id"])
        if job_id in protected:
            continue
        job = jobs.get(job_id)
        if job is None:
            store.mark_job_removed(job_id, now)
            continue
        finished = str(job.get("state") or "") == "completed" or job.get("enabled") is False
        if not finished:
            continue
        fire_at = _slot_fire_at(store, job_id)
        if fire_at is None or now < fire_at + SCRIPT_TIMEOUT_S + GC_MARGIN_S:
            continue
        if not _remove_job(deps, store, job_id, now):
            continue
        removed.append(job_id)
    return removed


def pending_job_ids(store: Store) -> list[str]:
    """Job ids of slots that may still act: GC must never touch them."""
    ids: list[str] = []
    for slot in store.list_slots():
        if slot.get("job_id") and slot.get("ran_at") is None:
            ids.append(str(slot["job_id"]))
    return ids


# ------------------------------------------------------------------------ Fire


class Fire:
    """The primary/backup one-shot, the reminder one-shot and ``redeem_now``.

    Two ways to spend a credit, both on the same idempotent path: the scheduled
    one-shots (``run`` for ``primary``/``backup``, A§8.3) and the explicit,
    user-confirmed ``redeem_now`` (D-015). The rehearsal world was cut (§6);
    ``dry_run`` covers "show me without spending", and reminders are
    notice-only.
    """

    def __init__(self, deps: Any, store: Store) -> None:
        self.deps = deps
        self.store = store

    # -- public ------------------------------------------------------------
    def run(self, role: str = "primary", *, dry_run: bool = False) -> "RunResult":
        """Handle the due slots of ``role`` only (I3).

        ``dry_run`` is the operation's explicit flag; the persistent setting is
        read by the gate, so either one pauses the spend (R1).
        """
        if role == ROLE_REMIND:
            return self.run_remind()
        if role not in REAL_ROLES:
            raise ValueError(f"unknown fire role {role!r}")
        now = _now(self.deps)
        try:
            with self.store.run_lock():
                return self._run_locked(role, now, dry_run=bool(dry_run))
        except LockTimeout as exc:
            return RunResult(exit_code=1, problem_code=exc.code, role=role)

    def redeem_now(
        self, credit_key: str, *, confirm: bool = False, dry_run: bool = False
    ) -> "RunResult":
        """Explicit, user-confirmed redemption of one credit (D-015, A§8.4).

        Same idempotent path as every other redemption: the persisted
        ``redeem_request_id``, the same readback, the same notices. The extra
        guard is a **per-credit lock**, so a Redeem now that races a primary or
        backup one-shot waits and then re-decides from the backend instead of
        posting a second time.
        """
        if confirm is not True:
            # ``confirm`` must be literally true: a truthy string is not a click.
            return RunResult(exit_code=2, problem_code="confirmation_required", role=ROLE_MANUAL)
        if dry_run:
            return RunResult(exit_code=2, problem_code="dry_run", role=ROLE_MANUAL)
        now = _now(self.deps)
        try:
            with self.store.run_lock():
                with self.store.credit_lock(credit_key):
                    return self._redeem_now_locked(credit_key, now, confirm=True)
        except LockTimeout as exc:
            return RunResult(exit_code=1, problem_code=exc.code, role=ROLE_MANUAL)

    def redeem_one(
        self, credit_key: str, *, role: str = ROLE_MANUAL, confirm: bool = False
    ) -> RedeemResult:
        """``redeem_one`` for one stored key, under both locks (service A§8.4).

        The service facade calls this shape; the flow above is the same code, so
        a manual redemption shares the persisted UUID and the readback rule.
        ``confirm`` is the caller's own evidence that the user asked for this:
        the gate refuses a manual spend without it, and the service passes the
        value it already validated.
        """
        now = _now(self.deps)
        with self.store.run_lock():
            with self.store.credit_lock(credit_key):
                row = self.store.get_credit(credit_key)
                if row is None:
                    return RedeemResult(
                        credit_key,
                        OUT_FAILED,
                        reason="unknown_credit",
                        notice_kind=NOTICE_FINAL,
                        failure=True,
                    )
                run_id = self.store.start_run("redeem_now", role, os.getpid(), now)
                result = redeem_one(self.deps, self.store, row, role=role, confirm=confirm)
                self._record_history("redeem_now", role, [result], now)
                flush_notices(self.deps, self.store)
                self.store.finish_run(run_id, "failed" if result.failure else "ok", _now(self.deps))
                return result

    def run_remind(self) -> "RunResult":
        """Send the reminders whose one-shot fired (T-72, D-007).

        Read-only by construction: the API is wrapped in ``ReadOnlyTransport``,
        so even a bug in this path cannot POST. A reminder never changes a
        credit row beyond the ``reminder_sent`` marker in ``meta``.
        """
        now = _now(self.deps)
        try:
            with self.store.run_lock():
                return self._run_remind_locked(now)
        except LockTimeout as exc:
            return RunResult(exit_code=1, problem_code=exc.code, role=ROLE_REMIND)

    def _run_remind_locked(self, now: float) -> "RunResult":
        """Queue the reminders of every due reminder slot; never a POST."""
        run_id = self.store.start_run("remind", ROLE_REMIND, os.getpid(), now)
        self.store.heal_runs(now)
        sent: list[str] = []
        own_ids: list[str] = []
        read_only = self._read_only_api()
        for role in REMINDER_ROLES:
            for slot in self.store.list_slots(role=role):
                if slot.get("ran_at") is not None:
                    continue
                fire_at = float(slot["fire_at"])
                if fire_at > now + DUE_SLACK_S:
                    continue
                if fire_at <= now - MAX_LATE_S:
                    continue  # too old: the scan's own reminder rule owns it
                row = self.store.get_credit(str(slot["credit_key"]))
                if row is None:
                    continue
                if str(row.get("status") or "") in _SETTLED_FOR_REMINDER:
                    self.store.mark_slot_ran(str(slot["credit_key"]), role, now)
                    continue
                if slot.get("job_id"):
                    own_ids.append(str(slot["job_id"]))
                self.store.mark_slot_ran(str(slot["credit_key"]), role, now)
                if self._remind(row, role, read_only, now):
                    sent.append(str(slot["credit_key"]))

        self._record_history(
            "remind",
            ROLE_REMIND,
            [RedeemResult(key, "reminded", reason="reminder") for key in sent],
            now,
        )
        self.store.add_history(
            [
                {
                    "at": now,
                    "action": "reminder",
                    "role": ROLE_REMIND,
                    "outcome": "sent",
                    "detail": {"count": len(sent)},
                }
            ]
            if sent
            else []
        )
        gc_jobs(self.deps, self.store, protect=list(own_ids) + pending_job_ids(self.store), now=now)
        flush_notices(self.deps, self.store)
        self.store.finish_run(run_id, "ok", _now(self.deps))
        return RunResult(exit_code=0, role=ROLE_REMIND)

    def _read_only_api(self) -> Any:
        """The API over a ``ReadOnlyTransport``: a reminder physically cannot POST."""
        source = getattr(self.deps, "source", None)
        api = getattr(self.deps, "api", None)
        if source is None or api is None:
            return api
        try:
            from .codex_api import CodexResetApi, ReadOnlyTransport

            return CodexResetApi(ReadOnlyTransport(source), sleep=getattr(self.deps, "sleep", None))
        except Exception:  # noqa: BLE001 - fall back to the shared api, still GET-only
            return api

    def _remind(self, row: Mapping[str, Any], role: str, api: Any, now: float) -> bool:
        """Queue one reminder notice for ``row``; ``False`` when already sent."""
        credit_key = str(row["credit_key"])
        if self.store.get_meta(reminder_meta_key(credit_key, role)) is not None:
            return False
        # A read-only freshness check: only remind about a credit the backend
        # still lists as available or pending (I1). An unreadable list still
        # reminds — the credit row itself is evidence enough, and a reminder
        # costs nothing.
        account = _account_for(self.deps, str(row["account_key"]))
        if account is not None and api is not None:
            try:
                credits = api.list_credits(account)
            except Exception:  # noqa: BLE001 - a reminder never fails a run
                credits = None
            if credits is not None:
                credit = _find(credits, self.deps.salt, str(row["account_key"]), credit_key)
                if credit is not None and credit.classification == REDEEMED:
                    return False
        params = notice_params(
            NOTICE_REMINDER,
            label=account_label(self.deps, self.store, str(row["account_key"]), account),
            expires_at=float(row.get("expires_at") or 0.0),
            fire_at=float(row.get("expires_at") or 0.0)
            - float(getattr(self.deps.settings, "primary_fire_offset_s", 0.0)),
            in_hours=in_hours_text(row.get("expires_at"), now),
            language=notice_language(self.deps),
        )
        dedupe = f"{NOTICE_REMINDER}:{credit_key}:{role}"
        self.store.enqueue_notice(
            dedupe,
            kind=NOTICE_REMINDER,
            message_key=NOTICE_REMINDER,
            params=params,
            credit_key=credit_key,
            now=now,
        )
        enqueue = getattr(self.deps.notifier, "enqueue", None)
        if callable(enqueue):
            enqueue(NOTICE_REMINDER, dict(params), credit_key=credit_key, dedupe_key=dedupe)
        self.store.set_meta(reminder_meta_key(credit_key, role), str(float(now)))
        return True

    # -- internals ---------------------------------------------------------
    def _run_locked(self, role: str, now: float, *, dry_run: bool = False) -> "RunResult":
        run_id = self.store.start_run("fire", role, os.getpid(), now)
        self.store.heal_runs(now)
        due = self._due(role, now)
        results: list[RedeemResult] = []
        own_ids: list[str] = []
        for slot, row in due:
            account = _account_for(self.deps, str(row["account_key"]))
            # Mark the slot BEFORE the work: the marker is persisted by the same
            # write that persists ``redeeming`` (pre-POST), so a crash mid-POST
            # or a manual re-run of the same one-shot cannot repeat it (R9).
            self.store.mark_slot_ran(str(row["credit_key"]), role, now)
            if slot.get("job_id"):
                own_ids.append(str(slot["job_id"]))
            try:
                result = redeem_one(
                    self.deps, self.store, row, role=role, account=account, dry_run=dry_run
                )
            except StateError:
                # The pre-POST state write failed: nothing was sent and this
                # slot did not run after all.
                self.store.clear_slot_ran(str(row["credit_key"]), role, now)
                continue
            results.append(result)

        self._record_history("fire", role, results, now)
        gc_jobs(self.deps, self.store, protect=list(own_ids) + pending_job_ids(self.store), now=now)
        flush_notices(self.deps, self.store)
        failed = any(result.failure for result in results)
        self.store.finish_run(run_id, "failed" if failed else "ok", _now(self.deps))
        return RunResult(exit_code=1 if failed else 0, role=role, results=results)

    def _redeem_now_locked(
        self, credit_key: str, now: float, *, confirm: bool = False
    ) -> "RunResult":
        row = self.store.get_credit(credit_key)
        if row is None:
            return RunResult(exit_code=1, problem_code="unknown_credit", role=ROLE_MANUAL)
        run_id = self.store.start_run("redeem_now", ROLE_MANUAL, os.getpid(), now)
        result = redeem_one(self.deps, self.store, row, role=ROLE_MANUAL, confirm=confirm)
        self._record_history("redeem_now", ROLE_MANUAL, [result], now)
        flush_notices(self.deps, self.store)
        self.store.finish_run(run_id, "failed" if result.failure else "ok", _now(self.deps))
        return RunResult(exit_code=1 if result.failure else 0, role=ROLE_MANUAL, results=[result])

    def _due(self, role: str, now: float) -> list[tuple[dict, dict]]:
        """Slots of ``role`` that are due, never ran and are still actionable.

        A slot is due only from its own ``fire_at``: the old 30 s slack let a
        manual ``fire`` spend up to half a minute before the lead boundary the
        user chose (R2). Lateness is still bounded by ``MAX_LATE_S``.
        """
        out: list[tuple[dict, dict]] = []
        for slot in self.store.list_slots(role=role):
            if slot.get("ran_at") is not None:
                continue
            fire_at = float(slot["fire_at"])
            if fire_at > now:
                continue
            if fire_at <= now - MAX_LATE_S:
                # Too old: the planner's ``missed`` rule owns it (R9D).
                continue
            row = self.store.get_credit(str(slot["credit_key"]))
            if row is None:
                continue
            expires_at = float(row.get("expires_at") or 0.0)
            if expires_at and expires_at <= now + EXPIRY_MARGIN_S:
                continue
            out.append((slot, row))
        return out

    def _record_history(
        self, action: str, role: str, results: Sequence[RedeemResult], now: float
    ) -> None:
        """Write one activity-log row per result (D-014).

        The action vocabulary is the store's: a redemption is ``redemption``,
        a failed attempt is also ``failure`` so the History view can filter the
        bad news without reading every row.
        """
        if not results:
            return
        records: list[dict] = []
        for result in results:
            records.append(
                {
                    "at": now,
                    "action": _log_action(action, result),
                    "credit_key": result.credit_key,
                    "role": role,
                    "outcome": result.outcome,
                    "reason_code": result.reason or None,
                    "detail": {"role": role, "attempts": result.attempts},
                }
            )
            if result.failure:
                records.append(
                    {
                        "at": now,
                        "action": "failure",
                        "credit_key": result.credit_key,
                        "role": role,
                        "outcome": result.outcome,
                        "reason_code": result.reason or None,
                    }
                )
        self.store.add_history(records)


@dataclass
class RunResult:
    """Exit code plus what the run did (never a raw body)."""

    exit_code: int
    role: str = ""
    problem_code: str = ""
    results: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0
