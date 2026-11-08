"""Credit classification (architecture A§4.4, invariant I1, T-05).

The single place that decides whether a credit is done. Every flow classifies
through here, so R5 (a ``redeeming`` row announced as redeemed) and R4 (an
unreadable readback reported as success) cannot come back.

``available``
    Clean: status ``available`` and no ``redeem_started_at``. The planner may
    schedule it.
``pending``
    Work in progress: ``redeeming``/``pending``, or ``available`` with a
    ``redeem_started_at``. **Must still be posted** — it is not done.
``redeemed``
    The backend says so: ``redeemed_at`` set or status ``redeemed``.
``unknown``
    Anything else (an unrecognized status, an expired row, a missing field).
    Never success, never scheduled.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

__all__ = [
    "AVAILABLE",
    "PENDING",
    "REDEEMED",
    "UNKNOWN",
    "CLASSIFICATIONS",
    "Credit",
    "parse_credit",
    "parse_credit_list",
    "classify",
    "is_actionable",
    "is_settled",
    "coerce_epoch",
    "CREDIT_FIELDS",
]

AVAILABLE = "available"
PENDING = "pending"
REDEEMED = "redeemed"
UNKNOWN = "unknown"

CLASSIFICATIONS = frozenset({AVAILABLE, PENDING, REDEEMED, UNKNOWN})

#: The only credit fields the plugin keeps (A§4.4). Everything else is dropped
#: with the raw body.
CREDIT_FIELDS = (
    "id",
    "reset_type",
    "status",
    "granted_at",
    "expires_at",
    "redeem_started_at",
    "redeemed_at",
    "title",
)

_PENDING_STATUSES = frozenset({"redeeming", "pending", "in_progress"})
_REDEEMED_STATUSES = frozenset({"redeemed", "used", "consumed"})
_AVAILABLE_STATUSES = frozenset({"available", "active", "ready"})


def coerce_epoch(value: Any) -> Optional[float]:
    """Backend timestamps: epoch seconds, epoch milliseconds, or ISO-8601.

    Returns ``None`` for anything unparseable rather than raising: an
    unparseable expiry makes a credit unschedulable, never a crash.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number <= 0:
            return None
        # Anything past year 5138 in seconds is really milliseconds.
        if number > 1e11:
            number /= 1000.0
        return number
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return coerce_epoch(float(text))
        except ValueError:
            pass
        try:
            import datetime

            normalized = text.replace("Z", "+00:00")
            moment = datetime.datetime.fromisoformat(normalized)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=datetime.timezone.utc)
            return moment.timestamp()
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class Credit:
    """One reset credit, parsed down to the fields the plugin needs.

    ``id`` is the raw backend id and is **never** persisted, logged or
    serialized (I8); flows key on ``keys.credit_key`` instead.
    """

    id: str
    status: str
    expires_at: Optional[float] = None
    granted_at: Optional[float] = None
    redeem_started_at: Optional[float] = None
    redeemed_at: Optional[float] = None
    reset_type: str = ""
    title: str = ""

    @property
    def classification(self) -> str:
        """``available`` / ``pending`` / ``redeemed`` / ``unknown`` (I1)."""
        status = (self.status or "").strip().lower()
        if self.redeemed_at is not None or status in _REDEEMED_STATUSES:
            return REDEEMED
        if status in _PENDING_STATUSES:
            return PENDING
        if status in _AVAILABLE_STATUSES:
            # An ``available`` row with a set ``redeem_started_at`` already
            # started its consume: work in progress, not a clean slot (R5).
            return PENDING if self.redeem_started_at is not None else AVAILABLE
        return UNKNOWN

    @property
    def available(self) -> bool:
        """Only a clean ``available`` row may be scheduled by the planner."""
        return self.classification == AVAILABLE

    @property
    def actionable(self) -> bool:
        """A credit a fire may still post for (I1/I2: available or pending)."""
        return self.classification in (AVAILABLE, PENDING)

    @property
    def settled(self) -> bool:
        """Backend evidence that the credit is spent."""
        return self.classification == REDEEMED

    def expires_in(self, now: float) -> Optional[float]:
        if self.expires_at is None:
            return None
        return self.expires_at - float(now)

    def expired(self, now: float, *, margin_s: float = 0.0) -> bool:
        """True when the credit cannot be redeemed any more."""
        if self.expires_at is None:
            return False
        return self.expires_at <= float(now) + float(margin_s)

    def __repr__(self) -> str:
        # The raw id never appears in a repr (I8).
        return (
            f"Credit(status={self.status!r}, expires_at={self.expires_at!r}, "
            f"classification={self.classification!r})"
        )


def parse_credit(row: Mapping[str, Any]) -> Optional[Credit]:
    """One backend row → :class:`Credit`, or ``None`` when it has no id.

    Unknown keys are dropped here, with the raw body (A§4.4).
    """
    if not isinstance(row, Mapping):
        return None  # type: ignore[unreachable]
    raw_id = row.get("id")
    credit_id = raw_id.strip() if isinstance(raw_id, str) else ""
    if not credit_id:
        return None
    status = row.get("status")
    return Credit(
        id=credit_id,
        status=status.strip().lower() if isinstance(status, str) else "",
        expires_at=coerce_epoch(row.get("expires_at")),
        granted_at=coerce_epoch(row.get("granted_at")),
        redeem_started_at=coerce_epoch(row.get("redeem_started_at")),
        redeemed_at=coerce_epoch(row.get("redeemed_at")),
        reset_type=_text(row.get("reset_type")),
        title=_text(row.get("title")),
    )


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


@dataclass(frozen=True)
class CreditList:
    """A parsed ``GET /rate-limit-reset-credits`` body.

    ``available_count`` is the backend's own count when present; it is kept for
    display only and never used to decide whether a specific credit is done.

    ``readable`` is the difference between "the backend said there are no
    credits" and "the body was not a credit list at all" (R4). Only a
    ``readable`` list may turn a missing credit into proof of consumption: a
    malformed 200 must stay unconfirmed, keeping the UUID and the backup.
    """

    credits: tuple
    available_count: Optional[int] = None
    readable: bool = True

    def by_id(self, credit_id: str) -> Optional[Credit]:
        for credit in self.credits:
            if credit.id == credit_id:
                return credit
        return None

    def actionable(self) -> tuple:
        return tuple(credit for credit in self.credits if credit.actionable)

    def __len__(self) -> int:
        return len(self.credits)

    def __iter__(self):
        return iter(self.credits)


def parse_credit_list(payload: Any) -> CreditList:
    """Parse a list body; a malformed payload is an **unreadable** empty list.

    The distinction matters exactly once, and it is the safety-critical case: a
    credit that is absent from a valid list was consumed, but a credit that is
    absent from a body that was never a list proves nothing. ``readable``
    carries that difference to ``_confirm`` and the reconciliation (R4).

    **Readability is row-level too (R4, round 2).** A body that *is* a list can
    still be incomplete: a row without an id, a ``null`` entry or a non-object
    is dropped by :func:`parse_credit` and its identity is lost. The parsed
    rows are kept — a scan may still schedule the credits it did understand —
    but the list is marked unreadable, because the absence of *another* credit
    is no longer evidence of anything. Only a list where **every** row parsed
    may prove absence, and a valid empty list still proves it.
    """
    if not isinstance(payload, Mapping):
        return CreditList(credits=(), readable=False)
    rows = payload.get("credits")
    if not isinstance(rows, (list, tuple)):
        return CreditList(credits=(), readable=False)
    credits: list[Credit] = []
    dropped = 0
    for row in rows:
        credit = parse_credit(row)
        if credit is not None:
            credits.append(credit)
        else:
            dropped += 1
    count = payload.get("available_count")
    available_count = int(count) if isinstance(count, int) and not isinstance(count, bool) else None
    return CreditList(
        credits=tuple(credits),
        available_count=available_count,
        readable=dropped == 0,
    )


def classify(row: Any) -> str:
    """Classify a raw row, a :class:`Credit` or a status string.

    Convenience for tests and for callers that already have a parsed credit.
    """
    if isinstance(row, Credit):
        return row.classification
    if isinstance(row, Mapping):
        credit = parse_credit(row)
        return credit.classification if credit is not None else UNKNOWN
    if isinstance(row, str):
        return Credit(id="", status=row).classification
    return UNKNOWN


def is_actionable(credit: Any) -> bool:
    return classify(credit) in (AVAILABLE, PENDING)


def is_settled(credit: Any) -> bool:
    return classify(credit) == REDEEMED
