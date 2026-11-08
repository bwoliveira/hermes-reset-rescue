"""The three Codex endpoints, over an ``AccountSource`` (A§4.4, T-06).

``CodexResetApi`` is the only module that knows a URL. It exposes

``list_credits(account)``
    ``GET /backend-api/wham/rate-limit-reset-credits``
``consume(account, credit_id, redeem_request_id)``
    ``POST /backend-api/wham/rate-limit-reset-credits/consume``
``usage(account)``
    ``GET /backend-api/wham/usage``

and nothing else may be called (I9). Bodies are parsed into frozen dataclasses
carrying only the fields the plugin needs; the raw body is dropped immediately
(I8).

``ReadOnlyTransport`` wraps a source and raises :class:`MutationRefused` on any
non-GET, so a dry run or a rehearsal physically cannot POST (I5).

Transient failures (timeout, connection, 5xx) on a GET get exactly one retry
after 2 s. A 401 is the source's business (§4.3): it is never retried here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .accounts import (
    CONSUME_PATH,
    CREDITS_PATH,
    OFFICIAL_ORIGIN,
    USAGE_PATH,
    AccountRef,
    AccountSource,
    Response,
    SourceUnavailable,
)
from .classify import CreditList, parse_credit_list

__all__ = [
    "CodexResetApi",
    "ConsumeResult",
    "MutationRefused",
    "ReadOnlyTransport",
    "UsageSummary",
    "CREDITS_URL",
    "CONSUME_URL",
    "USAGE_URL",
    "TRANSIENT_STATUSES",
    "RETRY_DELAY_S",
    "HTTP_TIMEOUT_S",
    "parse_consume",
    "parse_usage",
]

CREDITS_URL = OFFICIAL_ORIGIN + CREDITS_PATH
CONSUME_URL = OFFICIAL_ORIGIN + CONSUME_PATH
USAGE_URL = OFFICIAL_ORIGIN + USAGE_PATH

#: Every endpoint the plugin is allowed to call (I9). Tests assert on this.
ALLOWED_URLS = frozenset({CREDITS_URL, CONSUME_URL, USAGE_URL})

#: Statuses worth one retry on a GET: the request never reached the backend or
#: the backend failed transiently. 4xx (other than 429) is not retried.
TRANSIENT_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

RETRY_DELAY_S = 2.0
HTTP_TIMEOUT_S = 30.0

#: Fixed reason codes a consume can produce (never a raw body).
REASON_NO_CREDIT = "no_credit"
REASON_INELIGIBLE = "rate_limit_reset_ineligible"
REASON_ALREADY_REDEEMED = "already_redeemed"
REASON_NOTHING_TO_RESET = "nothing_to_reset"
REASON_RESET = "reset"
REASON_UNKNOWN = "unknown_response"
REASON_HTTP = "http_error"


class MutationRefused(Exception):
    """A non-GET was attempted through a read-only transport (I5)."""

    def __init__(self, method: str = "", url: str = ""):
        super().__init__("mutation refused")
        self.method = method.upper()
        # The URL is a constant, never a token-bearing value, so it is safe.
        self.url = url


@dataclass(frozen=True)
class ConsumeResult:
    """Outcome of one ``POST consume``, reduced to fixed codes.

    ``code`` is the backend's own string when it is a short known token, else
    a fixed reason. ``raw_present`` records only *whether* an unrecognized body
    arrived, never its content.
    """

    ok: bool
    reason: str
    windows_reset: int = 0
    status: int = 0

    @property
    def reset(self) -> bool:
        return self.reason in (REASON_RESET, REASON_ALREADY_REDEEMED)

    def __repr__(self) -> str:
        return (
            f"ConsumeResult(ok={self.ok!r}, reason={self.reason!r}, "
            f"windows_reset={self.windows_reset!r}, status={self.status!r})"
        )


@dataclass(frozen=True)
class UsageWindow:
    """One quota window as the backend reports it (D-017).

    ``label`` is ``session`` (5 h) or ``weekly`` (7 days), decided by
    ``limit_window_seconds`` and never by response position — Hermes does the
    same in ``agent/account_usage.py:632-661`` (a lone weekly window arrives in
    ``primary_window``).
    """

    label: str
    used_pct: float
    window_seconds: int

    @property
    def remaining_pct(self) -> float:
        """``100 - used``, clamped: D-017 shows *remaining* percent."""
        return max(0.0, min(100.0, 100.0 - float(self.used_pct)))

    def as_dict(self) -> dict:
        return {"label": self.label, "remaining_pct": round(self.remaining_pct)}

    def __repr__(self) -> str:
        return f"UsageWindow(label={self.label!r})"


@dataclass(frozen=True)
class UsageSummary:
    """The usage read: the early-redemption trigger (D-009) and the summary (D-017).

    ``weekly_pct`` / ``five_hour_pct`` are the *used* percentages the backend
    reports; ``weekly_remaining_pct`` is the complement D-009 compares. The
    summary DTO carries only :meth:`windows` (remaining %, rounded) and
    :attr:`plan_type`; nothing here is ever logged or persisted.
    """

    weekly_pct: Optional[float] = None
    five_hour_pct: Optional[float] = None
    plan_type: Optional[str] = None

    @property
    def known(self) -> bool:
        return self.weekly_pct is not None or self.five_hour_pct is not None

    @property
    def weekly_remaining_pct(self) -> Optional[float]:
        """Weekly quota *remaining*, the figure D-009 compares.

        ``used_percent`` is what the backend reports, so remaining is its
        complement. ``None`` when the backend did not report a weekly window,
        which means the trigger cannot fire (never "assume exhausted").
        """
        if self.weekly_pct is None:
            return None
        remaining = 100.0 - float(self.weekly_pct)
        return max(0.0, min(100.0, remaining))

    @property
    def windows(self) -> tuple[UsageWindow, ...]:
        """The present windows, 5 h first then weekly (D-017)."""
        out: list[UsageWindow] = []
        if self.five_hour_pct is not None:
            out.append(UsageWindow("session", float(self.five_hour_pct), 18000))
        if self.weekly_pct is not None:
            out.append(UsageWindow("weekly", float(self.weekly_pct), 604800))
        return tuple(out)

    def as_dict(self) -> dict:
        """Internal shape for the trigger; never serialize this into a DTO."""
        return {"weekly_pct": self.weekly_pct, "five_hour_pct": self.five_hour_pct}

    def __repr__(self) -> str:
        # A repr can end up in a log, so it names no percentage (D-010).
        return f"UsageSummary(known={self.known!r})"


# ------------------------------------------------------------------ read-only


class ReadOnlyTransport:
    """Wraps a source and refuses every non-GET (I5).

    Dry runs and rehearsals get this instead of the source, so a code path that
    tries to POST fails loudly in a test rather than spending a credit.
    """

    def __init__(self, source: AccountSource) -> None:
        self._source = source
        self.attempts: list[tuple] = []

    def request(
        self,
        account: Any,
        method: str,
        url: str,
        *,
        json: object | None = None,
        timeout: float = HTTP_TIMEOUT_S,
    ) -> Response:
        self.attempts.append((method.upper(), url))
        if method.upper() != "GET":
            raise MutationRefused(method, url)
        return self._source.request(account, "GET", url, timeout=timeout)

    def accounts(self):
        return self._source.accounts()

    def coverage_note(self) -> str:
        return self._source.coverage_note()

    def close(self) -> None:
        self._source.close()


# ------------------------------------------------------------------------ api


class CodexResetApi:
    """The only caller of the three endpoints (A§4.4, I9)."""

    def __init__(
        self,
        source: Any,
        *,
        retry_delay_s: float = RETRY_DELAY_S,
        sleep: Optional[Any] = None,
        timeout: float = HTTP_TIMEOUT_S,
    ) -> None:
        self.source = source
        self.retry_delay_s = retry_delay_s
        self._sleep = sleep
        self.timeout = timeout

    # -- list --------------------------------------------------------------

    def list_credits(self, account: AccountRef) -> CreditList:
        """``GET`` the credit list. One retry on a transient failure."""
        response = self._get(account, CREDITS_URL)
        if response.status != 200:
            raise SourceUnavailable(_status_code(response.status))
        return parse_credit_list(response.body)

    # -- consume -----------------------------------------------------------

    def consume(self, account: AccountRef, credit_id: str, redeem_request_id: str) -> ConsumeResult:
        """``POST`` the consume. Never retried here: the caller owns retries.

        A retry of a POST needs the *same* ``redeem_request_id`` and is a
        decision the fire flow makes (I2), so this method sends exactly one
        request and reports what came back.
        """
        if not credit_id or not redeem_request_id:
            raise ValueError("credit_id and redeem_request_id are required")
        response = self._request(
            account,
            "POST",
            CONSUME_URL,
            json={"redeem_request_id": str(redeem_request_id), "credit_id": str(credit_id)},
        )
        return parse_consume(response)

    # -- usage -------------------------------------------------------------

    def usage(self, account: AccountRef, *, timeout: Optional[float] = None) -> UsageSummary:
        """``GET`` the usage summary. Best effort: never raises for display.

        ``timeout`` bounds the whole call (both attempts): the popup's
        ``/status`` has a 9 s budget and must never wait on a slow usage read.
        """
        try:
            response = self._get(account, USAGE_URL, timeout=timeout)
        except SourceUnavailable:
            return UsageSummary()
        if response.status != 200:
            return UsageSummary()
        return parse_usage(response.body)

    # -- transport ---------------------------------------------------------

    def _get(self, account: AccountRef, url: str, *, timeout: Optional[float] = None) -> Response:
        """A GET with exactly one retry on a transient failure."""
        response = self._request(account, "GET", url, timeout=timeout)
        if response.status in TRANSIENT_STATUSES:
            self._wait()
            response = self._request(account, "GET", url, timeout=timeout)
        return response

    def _request(
        self,
        account: AccountRef,
        method: str,
        url: str,
        *,
        json: object | None = None,
        timeout: Optional[float] = None,
    ) -> Response:
        if url not in ALLOWED_URLS:  # pragma: no cover - defence in depth
            raise SourceUnavailable("unsupported_endpoint")
        try:
            return self.source.request(
                account,
                method,
                url,
                json=json,
                timeout=self.timeout if timeout is None else timeout,
            )
        except (SourceUnavailable, MutationRefused):
            # A refusal must stay a refusal: mapping it to a transport code
            # would let a dry run report "network" instead of failing loudly.
            raise
        except Exception as exc:  # noqa: BLE001 - mapped to a fixed code
            raise SourceUnavailable(_exception_code(exc)) from None

    def _wait(self) -> None:
        if self._sleep is not None:
            self._sleep(self.retry_delay_s)


def _status_code(status: int) -> str:
    if status == 401:
        return "auth_expired"
    if status == 403:
        return "forbidden"
    if status == 404:
        return "not_found"
    if status in TRANSIENT_STATUSES:
        return "transient"
    return "http_error"


def _exception_code(exc: Exception) -> str:
    name = type(exc).__name__
    if isinstance(exc, TimeoutError) or name in ("ReadTimeout", "ConnectTimeout", "Timeout"):
        return "timeout"
    if name in ("ConnectionError", "ConnectError", "RemoteProtocolError"):
        return "network"
    return "network"


# -------------------------------------------------------------------- parsing


def parse_consume(response: Response) -> ConsumeResult:
    """Reduce a consume response to fixed codes, dropping the body.

    Recognized shapes (``code`` is the backend's short token when present):

    * 200/201 with ``code`` in {``reset``, ``already_redeemed``,
      ``nothing_to_reset``, ``no_credit``, ``rate_limit_reset_ineligible``};
    * 200 with ``windows_reset`` and no ``code`` → ``reset``;
    * 409 → ``already_redeemed``;
    * 403 → ``rate_limit_reset_ineligible``;
    * 404 → ``no_credit``;
    * anything else → ``http_error`` / ``unknown_response``.
    """
    status = int(response.status or 0)
    body = response.body if isinstance(response.body, Mapping) else {}

    if status in (200, 201, 202):
        code = body.get("code")
        reason = code.strip().lower() if isinstance(code, str) else ""
        windows = body.get("windows_reset")
        windows_reset = (
            int(windows) if isinstance(windows, int) and not isinstance(windows, bool) else 0
        )
        if reason in (
            REASON_RESET,
            REASON_ALREADY_REDEEMED,
            REASON_NOTHING_TO_RESET,
            REASON_NO_CREDIT,
            REASON_INELIGIBLE,
        ):
            return ConsumeResult(
                ok=reason in (REASON_RESET, REASON_ALREADY_REDEEMED),
                reason=reason,
                windows_reset=windows_reset,
                status=status,
            )
        if windows_reset > 0:
            return ConsumeResult(
                ok=True, reason=REASON_RESET, windows_reset=windows_reset, status=status
            )
        if not body:
            # A 200 with no body is not evidence of anything: the readback
            # decides (I1). Reported as unknown, never as success.
            return ConsumeResult(ok=False, reason=REASON_UNKNOWN, status=status)
        return ConsumeResult(ok=False, reason=REASON_UNKNOWN, status=status)

    if status == 409:
        return ConsumeResult(ok=True, reason=REASON_ALREADY_REDEEMED, status=status)
    if status == 403:
        return ConsumeResult(ok=False, reason=REASON_INELIGIBLE, status=status)
    if status == 404:
        return ConsumeResult(ok=False, reason=REASON_NO_CREDIT, status=status)
    return ConsumeResult(ok=False, reason=REASON_HTTP, status=status)


def parse_usage(payload: Any) -> UsageSummary:
    """``limit_window_seconds`` 604800 → weekly, 18000 → 5 h (A§4.4, D-017).

    Mirrors Hermes' own ``agent/account_usage.py``: a window with a non-numeric
    percentage or an unexpected shape is simply absent, and the window is
    identified by its published duration, never by its position. ``plan_type``
    is title-cased the way Hermes does it (``_title_case_slug``,
    ``agent/account_usage.py:79-81``).
    """
    if not isinstance(payload, Mapping):
        return UsageSummary()
    rate = payload.get("rate_limit")
    if not isinstance(rate, Mapping):
        rate = {}
    weekly: Optional[float] = None
    five_hour: Optional[float] = None
    for name in ("primary_window", "secondary_window"):
        window = rate.get(name)
        if not isinstance(window, Mapping):
            continue
        seconds = window.get("limit_window_seconds")
        used = window.get("used_percent")
        if isinstance(seconds, bool) or not isinstance(seconds, int):
            continue
        if isinstance(used, bool) or not isinstance(used, (int, float)):
            continue
        if seconds == 604800:
            weekly = float(used)
        elif seconds == 18000:
            five_hour = float(used)
    return UsageSummary(
        weekly_pct=weekly, five_hour_pct=five_hour, plan_type=plan_type_from(payload)
    )


def plan_type_from(payload: Any) -> Optional[str]:
    """``plan_type`` title-cased (``pro_200`` → ``Pro 200``), or ``None``.

    Same transformation as Hermes' ``_title_case_slug`` so the popup and
    ``/usage`` never disagree on the plan name (D-017).
    """
    if not isinstance(payload, Mapping):
        return None
    raw = payload.get("plan_type")
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    return cleaned.replace("_", " ").replace("-", " ").title()
