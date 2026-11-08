"""The money boundary: exactly one function decides whether a consume POST may happen.

Every path that can spend a banked reset — the primary one-shot, the backup
one-shot, the late branch of the scan, the opt-in early (quota) redemption and
``redeem now`` — reaches the network through ``fire._post_with_retries``, and
``_post_with_retries`` is called from exactly one place: ``fire.redeem_one``.
:func:`authorize_consume` is called immediately before that call, with the
**fresh** backend credit, the current settings and the clock. No other code may
decide to spend, and no path may POST without a ``Decision.allowed`` answer.

Why one function instead of a check per caller
----------------------------------------------
The review of round 1 found the same class of bug five times: a rule that lived
in one caller (the planner's lead window, the CLI's ``--dry-run`` flag, the
service's exclusion list) and was silently absent from another (a slot that was
already armed, a manual ``fire``, the quota path). A rule that is enforced at
the boundary cannot be forgotten by a new caller, and a rule that is not at the
boundary is not enforced at all.

The rules, in the order they are applied
----------------------------------------
1. **Dry run is monotonic.** ``settings.dry_run`` **or** an explicit
   ``dry_run`` flag means never POST. A flag can only add a pause, never remove
   one: ``dry_run=False`` from a CLI default must not override a persistent
   ``dry_run=true``. That is why callers pass ``None`` for "the flag was
   absent" and why this function ORs the two sources instead of choosing one.
2. **An excluded account is never spent.** ``settings.account_included`` is
   asked with both the stored account key and the account's label, so an
   exclusion written by key and one written by label behave the same, and a
   slot armed *before* the exclusion is refused when it fires.
3. **Automatic roles only inside their window.** ``primary``, ``backup`` and
   ``scan`` (the late branch) may POST only while
   ``expires_at - lead <= now < expires_at``, using the **fresh** expiry read
   from the backend in this run. A saved expiry is never used: a credit whose
   expiry was extended by a day must not be spent by the slot that was armed
   for the old one, and a credit with 12 minutes left is not inside a
   10-minute lead window.
4. **Manual needs a real confirmation.** ``manual`` bypasses the window (the
   user asked for it now) but only with ``confirm is True`` — a truthy string
   is not a click.
5. **Quota needs the opt-in and a fresh reading.** ``early`` bypasses the
   window (that is its purpose) but only when ``early_redeem_enabled`` is on
   *and* this run's weekly reading is at or below the configured remaining
   percentage. A missing reading is "unknown", never "exhausted".
6. **Automatic redemption needs the master switch (D-023).** With
   ``auto_redeem_enabled`` off, every automatic role — ``primary``, ``backup``,
   ``scan`` and ``early`` — is refused with ``auto_redeem_disabled``. The
   manual role is not automatic: a confirmed ``Redeem now`` still spends.
7. **Anything else is refused.** An unknown role is a bug, and a bug never
   spends a credit.

What this function deliberately does not do
-------------------------------------------
It does not read the store, the clock or the network, and it does not write
anything. It is pure so that every caller and every test can ask the same
question with the same answer, and so the decision can be logged without a side
effect. The caller persists the intent (``begin_redeem``) only after an
``allowed`` answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

__all__ = [
    "AUTOMATIC_ROLES",
    "scoped_settings",
    "QUOTA_ROLE",
    "MANUAL_ROLE",
    "Decision",
    "authorize_consume",
]

#: Roles whose POST is bounded by the credit's own lead window.
AUTOMATIC_ROLES = ("primary", "backup", "scan")
#: The opt-in early-redemption role (D-009).
QUOTA_ROLE = "early"
#: The explicit, user-confirmed role (D-015).
MANUAL_ROLE = "manual"


@dataclass(frozen=True)
class Decision:
    """``allowed`` plus a fixed reason code (never a sentence, never a secret)."""

    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


def authorize_consume(
    *,
    role: str,
    credit: Any,
    settings: Any,
    now: float,
    account_key: str = "",
    label: str = "",
    confirm: Optional[bool] = None,
    dry_run: bool = False,
    quota_reading: Optional[float] = None,
) -> Decision:
    """Whether this role may POST ``consume`` for this credit right now.

    ``credit`` is the **fresh** backend credit of this run (its ``expires_at``
    is the only expiry that counts). ``dry_run`` is the explicit flag of this
    operation; the persistent setting is read here, so the answer is
    ``settings.dry_run or dry_run``. ``quota_reading`` is this run's weekly
    *remaining* percentage, or ``None`` when it could not be read.
    """
    if bool(getattr(settings, "dry_run", False)) or bool(dry_run):
        return Decision(False, "dry_run")

    if _paused(settings, account_key, label):
        return Decision(False, "account_paused")

    if not _included(settings, account_key, label):
        return Decision(False, "account_excluded")

    # D-030: every decision below reads this account's effective values.
    settings = scoped_settings(settings, account_key, label)

    if role == MANUAL_ROLE:
        if confirm is not True:
            return Decision(False, "confirmation_required")
        return Decision(True, "manual_confirmed")

    if role == QUOTA_ROLE:
        if not _auto_redeem_enabled(settings):
            return Decision(False, "auto_redeem_disabled")
        threshold = getattr(settings, "early_redeem_threshold_pct", None)
        if threshold is None:
            return Decision(False, "early_redeem_disabled")
        if quota_reading is None:
            return Decision(False, "quota_unknown")
        if float(quota_reading) > float(threshold):
            return Decision(False, "quota_above_threshold")
        return Decision(True, "quota_exhausted")

    if role in AUTOMATIC_ROLES:
        # D-023: the master switch. Checked before the window so the reason
        # says why the credit was left alone, not that it was too early.
        if not _auto_redeem_enabled(settings):
            return Decision(False, "auto_redeem_disabled")
        return _automatic(credit, settings, now)

    return Decision(False, "unknown_role")


def scoped_settings(settings: Any, account_key: str = "", label: str = "") -> Any:
    """The account's effective settings (D-030); a settings double is returned as is."""
    scope = getattr(settings, "for_account", None)
    return scope(account_key, label) if callable(scope) else settings


def _paused(settings: Any, account_key: str, label: str) -> bool:
    fields = getattr(settings, "account_override_fields", None)
    if not callable(fields):
        return False
    return fields(account_key, label).get("monitored") is False


def _auto_redeem_enabled(settings: Any) -> bool:
    """``settings.auto_redeem_enabled``, defaulting to on (D-023).

    A settings double without the field is an older build, and the behaviour it
    had was automatic redemption: absent means on, never off.
    """
    value = getattr(settings, "auto_redeem_enabled", True)
    return bool(value)


def _included(settings: Any, account_key: str, label: str) -> bool:
    """``settings.account_included`` with the label, and never a crash."""
    checker = getattr(settings, "account_included", None)
    if not callable(checker):
        return True
    try:
        return bool(checker(account_key, label))
    except TypeError:  # a settings double without the label parameter
        return bool(checker(account_key))
    except Exception:  # noqa: BLE001 - an unreadable setting is not a licence to spend
        return False


def _automatic(credit: Any, settings: Any, now: float) -> Decision:
    """The lead window, on the fresh expiry."""
    expires_at = getattr(credit, "expires_at", None)
    if expires_at is None:
        return Decision(False, "expiry_unknown")
    expires_at = float(expires_at)
    lead = float(getattr(settings, "lead_seconds", 0.0) or 0.0)
    if now >= expires_at:
        return Decision(False, "expired")
    if now < expires_at - lead:
        return Decision(False, "before_lead_window")
    return Decision(True, "inside_lead_window")
