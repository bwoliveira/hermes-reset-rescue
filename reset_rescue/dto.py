"""Allow-list serializers for everything that leaves the engine.

Every object Hermes, the CLI or the Desktop can see is a :class:`DTO` whose
``FIELDS`` tuple is the complete list of keys it may emit. ``to_dict()`` walks
that tuple, so a value added to an instance but not to the allow-list raises
:class:`DTOLeak` instead of leaking (architecture A§5, invariant I8).

Rules enforced here, not by convention:

* no field is named ``profile`` (Hermes owns ``?profile=``; A§5),
* no field name matches a credential-ish pattern (``token``, ``secret``,
  ``authorization``, ``cookie``, ``password``, ``api_key``, ``base_url``),
* no field carries a quota *percentage* except the compact summary D-017
  restored (:data:`LIMIT_SUMMARY_FIELDS`); the summary is plan type plus each
  present window's **remaining** percent, never a used percent and never a
  per-credit figure,
* times are emitted as ISO-8601 UTC with a ``Z`` suffix, or ``None``,
* nested DTOs, lists of DTOs and plain mappings are serialized recursively.

The module imports nothing from the rest of the package, so every layer can
depend on it without a cycle.
"""

from __future__ import annotations

import datetime
import json
from typing import Any, ClassVar, Iterable, Mapping, Optional, Tuple

__all__ = [
    "DTO",
    "DTOLeak",
    "AccountDTO",
    "AttentionDTO",
    "CreditDTO",
    "EnsureDTO",
    "HistoryDTO",
    "HistoryItemDTO",
    "JobDTO",
    "LastResultDTO",
    "NoticeResultDTO",
    "QuotaDTO",
    "QuotaCheckDTO",
    "RedeemResultDTO",
    "ScheduleDTO",
    "SettingsViewDTO",
    "SlotDTO",
    "StatusDTO",
    "WrapperStateDTO",
    "iso_z",
    "to_json",
    "CREDIT_STATES",
    "OUTCOMES",
    "LIMIT_SUMMARY_FIELDS",
]

#: Credit states a DTO may report (A§5 ``CreditDTO.state``).
CREDIT_STATES = frozenset({"available", "pending", "redeemed", "vanished", "expired", "unknown"})

#: Redemption outcomes a DTO may report (A§5 ``RedeemResultDTO.outcome``).
OUTCOMES = frozenset(
    {
        "redeemed",
        "already_redeemed",
        "nothing_to_reset",
        "vanished",
        "failed",
        "unconfirmed",
        "dry_run",
    }
)

#: Substrings that must never appear in a DTO field name.
_FORBIDDEN_NAME_PARTS = (
    "token",
    "secret",
    "authorization",
    "cookie",
    "password",
    "api_key",
    "access_key",
    "refresh",
    "base_url",
    "profile",
    # D-010/D-017: quota is shown only as the compact summary. A percentage
    # name is refused everywhere except the one window DTO below, so no other
    # DTO can grow a per-account quota field by accident.
    "pct",
    "percent",
    "quota",
    "usage",
    "weekly",
    "five_hour",
)

#: The only quota-shaped field names D-017 reintroduces, and the only classes
#: that may declare them. Each is a *remaining* figure; there is deliberately no
#: ``used_pct`` entry, so a used percentage cannot be added by accident.
#: ``AccountDTO.quota`` is the one place the nested summary hangs off.
LIMIT_SUMMARY_FIELDS: Mapping[str, Tuple[str, ...]] = {
    "AccountDTO": ("quota",),
    "QuotaDTO": ("five_hour_remaining_pct", "weekly_remaining_pct"),
    # The user's own configured early-redemption threshold, shown in the
    # footer. It is a setting, not a reading, and it names no account.
    "SettingsViewDTO": ("early_redeem_remaining_pct",),
}

_FORBIDDEN_EXACT = frozenset({"profile"})


class DTOLeak(Exception):
    """An instance carried a value its allow-list does not permit."""

    def __init__(self, dto: str, field: str):
        super().__init__(f"{dto}.{field} is not in the DTO allow-list")
        self.dto = dto
        self.field = field


def _check_field_name(dto_name: str, field: str) -> None:
    if field in _FORBIDDEN_EXACT:
        raise DTOLeak(dto_name, field)
    if field in LIMIT_SUMMARY_FIELDS.get(dto_name, ()):
        return
    lowered = field.lower()
    for part in _FORBIDDEN_NAME_PARTS:
        if part in lowered:
            raise DTOLeak(dto_name, field)


def iso_z(epoch: Optional[float]) -> Optional[str]:
    """Epoch seconds → ``2026-11-09T00:30:00Z`` (UTC, second precision)."""
    if epoch is None:
        return None
    moment = datetime.datetime.fromtimestamp(float(epoch), datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _serialize(value: Any) -> Any:
    if isinstance(value, DTO):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_serialize(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # Anything else (a dataclass from another module, an exception, a socket)
    # is not serializable on purpose: it would bypass the allow-list.
    raise DTOLeak(type(value).__name__, "value")


class DTO:
    """Base class for an allow-listed serializable object.

    Subclasses set ``FIELDS`` to the complete tuple of public keys they emit.
    Instances are plain objects (``__dict__`` based) so ``to_dict`` can prove
    that nothing outside the allow-list is attached.
    """

    FIELDS: ClassVar[Tuple[str, ...]] = ()

    # -- allow-list machinery -------------------------------------------------

    @classmethod
    def fields(cls) -> Tuple[str, ...]:
        return tuple(cls.FIELDS)

    def _extra_attributes(self) -> Iterable[str]:
        allowed = set(self.FIELDS)
        for name in vars(self):
            if name.startswith("_"):
                continue
            if name not in allowed:
                yield name

    def check(self) -> "DTO":
        """Raise :class:`DTOLeak` if the instance carries an extra attribute.

        Also validates every declared field name, so a subclass that extends
        ``FIELDS`` with a credential- or quota-shaped name fails here rather
        than emitting it.
        """
        for name in self.FIELDS:
            _check_field_name(type(self).__name__, name)
        for name in self._extra_attributes():
            raise DTOLeak(type(self).__name__, name)
        for name in self.FIELDS:
            if name not in vars(self):
                raise DTOLeak(type(self).__name__, name)
        return self

    def to_dict(self) -> dict:
        self.check()
        return {name: _serialize(getattr(self, name)) for name in self.FIELDS}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=False)

    def __repr__(self) -> str:
        # Only allow-listed values are ever printed; ``check`` first so a
        # stray attribute is a hard error even in a log line.
        self.check()
        parts = ", ".join(f"{name}={getattr(self, name)!r}" for name in self.FIELDS)
        return f"{type(self).__name__}({parts})"


def to_json(value: Any) -> str:
    """Serialize a DTO (or a plain structure of DTOs) to JSON text."""
    return json.dumps(_serialize(value), ensure_ascii=False, sort_keys=False)


class _Record(DTO):
    """Small helper: a DTO built from keyword arguments with a fixed field list.

    Every subclass annotates its fields (``account_key: str``) as well as
    listing them in ``FIELDS``. The annotations are not decoration: without
    them a type checker cannot see an attribute that ``setattr`` creates, so a
    consumer reading ``credit.expires_at`` was reported as drift. A test pins
    the two sets equal.
    """

    def __init__(self, **kwargs: Any) -> None:
        allowed = set(self.FIELDS)
        for name, value in kwargs.items():
            if name not in allowed:
                raise DTOLeak(type(self).__name__, name)
            setattr(self, name, value)
        for name in self.FIELDS:
            if name not in kwargs:
                setattr(self, name, None)


# ------------------------------------------------------------------- settings


class SettingsViewDTO(_Record):
    """The subset of settings the popup shows (never the raw target).

    Field names follow ``plugin.yaml`` exactly, so a reader of the form and a
    reader of the DTO see one vocabulary. ``included_accounts_count`` is
    accepted as a constructor alias for ``excluded_accounts_count`` because
    L3's CLI fixtures still build this DTO with the pre-T-71 name; it is not
    emitted.
    """

    FIELDS = (
        "scan_interval_hours",
        "lead_minutes",
        "backup_lead_minutes",
        "notify_target_display",
        "excluded_accounts_count",
        "mask_labels",
        "auto_redeem_enabled",
        "early_redeem_enabled",
        "early_redeem_remaining_pct",
    )

    scan_interval_hours: int
    lead_minutes: int
    backup_lead_minutes: int
    notify_target_display: Optional[str]
    excluded_accounts_count: int
    mask_labels: bool
    auto_redeem_enabled: bool
    early_redeem_enabled: bool
    early_redeem_remaining_pct: int

    def __init__(self, **kwargs: Any) -> None:
        if "included_accounts_count" in kwargs:
            kwargs.setdefault("excluded_accounts_count", kwargs.pop("included_accounts_count"))
        super().__init__(**kwargs)


# ------------------------------------------------------------------ scheduling


class JobDTO(_Record):
    FIELDS = ("name", "job_id", "schedule_display", "enabled")

    name: str
    job_id: str
    schedule_display: str
    enabled: bool


class WrapperStateDTO(_Record):
    FIELDS = ("name", "state")

    name: str
    state: str


class ScheduleDTO(_Record):
    FIELDS = (
        "armed",
        "scan_job",
        "early_job",
        "next_scan_at",
        "last_scan_at",
        "last_scan_outcome",
        "wrappers",
        "problem_code",
    )

    armed: bool
    scan_job: Optional["JobDTO"]
    early_job: Optional["JobDTO"]
    next_scan_at: Optional[str]
    last_scan_at: Optional[str]
    last_scan_outcome: Optional[str]
    wrappers: list["WrapperStateDTO"]
    problem_code: Optional[str]


# -------------------------------------------------------------------- credits


class SlotDTO(_Record):
    FIELDS = ("role", "fire_at", "job_state")

    role: str
    fire_at: Optional[str]
    job_state: str


class LastResultDTO(_Record):
    FIELDS = ("code", "at", "reason_code")

    code: str
    at: Optional[str]
    reason_code: Optional[str]


class CreditDTO(_Record):
    FIELDS = (
        "credit_key",
        "title",
        "reset_type",
        "granted_at",
        "expires_at",
        "state",
        "plan",
        "primary",
        "backup",
        "last_result",
        "can_redeem_now",
    )

    credit_key: str
    title: str
    reset_type: str
    granted_at: Optional[str]
    expires_at: Optional[str]
    state: str
    plan: str
    primary: Optional["SlotDTO"]
    backup: Optional["SlotDTO"]
    last_result: Optional["LastResultDTO"]
    can_redeem_now: bool


class QuotaDTO(_Record):
    """The compact per-account quota summary (D-017).

    ``plan_type`` is the backend's own plan name, title-cased the way Hermes
    renders it (``Pro 200``). Each window field is the **remaining** percent,
    rounded, or ``None`` when the backend did not report that window — the
    Desktop renders only the ones present, under the banked resets.

    This is the only DTO allowed to carry a percentage (see
    :data:`LIMIT_SUMMARY_FIELDS`), and it carries no used figure at all.
    """

    FIELDS = ("plan_type", "five_hour_remaining_pct", "weekly_remaining_pct")

    plan_type: Optional[str]
    five_hour_remaining_pct: Optional[int]
    weekly_remaining_pct: Optional[int]


class AccountDTO(_Record):
    FIELDS = (
        "account_key",
        "label",
        "included",
        "excluded",
        "monitored",
        "overrides",
        "effective",
        "reachable",
        "last_error_code",
        "last_checked_at",
        "quota",
        "credits",
    )

    account_key: str
    label: str
    included: bool
    #: D-028/D-034: the Excluded accounts list names this account (key or masked label).
    excluded: bool
    #: D-030: False when the account is paused (override) or excluded (D-028).
    monitored: bool
    #: D-030: only the overridable fields this account sets; empty follows the globals.
    overrides: dict
    #: D-030: the effective values this account is decided with.
    effective: dict
    reachable: bool
    last_error_code: Optional[str]
    last_checked_at: Optional[str]
    quota: Optional["QuotaDTO"]
    credits: list["CreditDTO"]


class AttentionDTO(_Record):
    FIELDS = ("failed", "pending", "next_fire_at")

    failed: int
    pending: int
    next_fire_at: Optional[str]


class StatusDTO(_Record):
    """The whole popup payload (D-010 banked-only, D-017 summary).

    ``accounts`` lists every pool account (D-027); an account with no banked
    reset has an empty ``credits`` list and the popup says ``No resets``. ``account_count`` is
    the number of accounts the next scan covers (every stored account that is
    not excluded), so the empty state can say "scanning M accounts" without
    the popup having to see the pool; ``None`` means "not known yet" (a fresh
    install before its first scan), never "no account".

    ``language`` is the **setting** (``auto`` / ``en`` / ``pt-BR``), not the
    resolved bundle: ``auto`` must reach the Desktop as ``auto`` so it can
    follow its own locale (D-016). Notices resolve ``auto`` against Hermes'
    ``display.language`` on the host side, where no Desktop exists.
    """

    FIELDS = (
        "version",
        "schema_version",
        "source_kind",
        "coverage_note_key",
        "dry_run",
        "auto_redeem_enabled",
        "language",
        "settings",
        "settings_warnings",
        "schedule",
        "accounts",
        "attention",
        "account_count",
        "pool_account_count",
        "per_account_available",
    )

    version: str
    schema_version: int
    source_kind: str
    coverage_note_key: str
    dry_run: bool
    #: D-023: the popup says "auto-redeem is off" without digging into settings.
    auto_redeem_enabled: bool
    language: str
    settings: "SettingsViewDTO"
    settings_warnings: list[str]
    schedule: "ScheduleDTO"
    accounts: list["AccountDTO"]
    attention: "AttentionDTO"
    account_count: Optional[int]
    #: D-030: every pool account, paused included (``account_count`` keeps its
    #: meaning: the accounts the next scan covers).
    pool_account_count: Optional[int]
    #: D-030: 2+ pool accounts, or any account holding an override.
    per_account_available: bool


# --------------------------------------------------------------------- results


class RedeemResultDTO(_Record):
    """Outcome of one redemption (``redeem now`` included, D-015).

    There is deliberately no ``usage`` field: the quota summary lives on the
    account (D-017) and a redemption result never carries a percentage.
    """

    FIELDS = (
        "credit_key",
        "outcome",
        "reason_code",
        "windows_reset",
    )

    credit_key: str
    outcome: str
    reason_code: Optional[str]
    windows_reset: Optional[int]


class QuotaCheckDTO(_Record):
    """What one ``quota-check`` run did (D-009), in fixed codes and counts.

    No percentage: the threshold comparison happens inside the flow and the
    outcome is reported as counts plus per-credit results.
    """

    FIELDS = ("exit_code", "problem_code", "accounts", "cooled", "redeemed", "failed", "results")

    exit_code: int
    problem_code: str
    accounts: int
    cooled: int
    redeemed: int
    failed: int
    results: list["RedeemResultDTO"]


class EnsureDTO(_Record):
    FIELDS = ("armed", "created", "updated", "removed", "problems")

    armed: bool
    created: list[str]
    updated: list[str]
    removed: list[str]
    problems: list[str]


class HistoryItemDTO(_Record):
    FIELDS = ("at", "action", "credit_key", "account_key", "outcome", "reason_code")

    at: Optional[str]
    action: str
    credit_key: Optional[str]
    account_key: Optional[str]
    outcome: str
    reason_code: Optional[str]


class HistoryDTO(_Record):
    FIELDS = ("items",)

    items: list["HistoryItemDTO"]


class NoticeResultDTO(_Record):
    FIELDS = ("target_display", "delivered", "skipped", "error_code")

    target_display: str
    delivered: bool
    skipped: bool
    error_code: Optional[str]
