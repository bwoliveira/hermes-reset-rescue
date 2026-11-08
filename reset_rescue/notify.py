"""Notices through Hermes' own delivery (simplification §3, D-008, D-013, T-19/T-74).

The plugin never creates a bot, a token or a service: it calls
``hermes send -t <target> --json <text>``, which reuses whatever messaging
channel the profile already has. The resolution rules, in order:

1. ``notify_target`` from settings, when it is a valid ``hermes send`` target.
2. Otherwise the **bare platform target** of the first connected platform that
   has a home channel — priority ``telegram``, ``discord``, ``slack``,
   ``whatsapp``, ``signal``, then registry order (§3). The target is the bare
   platform name (``telegram``), never ``telegram:<chat_id>``: Hermes routes a
   bare platform to its configured home chat **and thread** itself, and
   rebuilding that here would be a second, worse copy of the resolution. This
   is also the case that matters on the real profile, where
   ``hermes send --list --json`` returns ``{"platforms": {}}`` (the channel
   directory is populated by a running gateway) while ``hermes send -t
   telegram`` works.
3. Nothing usable → ``local``: the notice stays in the popup and the history
   and no subprocess runs.

**Labels** follow D-013: ``mask_labels`` (on by default) renders
``b***@gmail.com``; turning it off shows the label exactly as Hermes stores it,
so three accounts sharing an initial stay distinguishable. Masking is applied
here, at the render boundary, and never in the catalog — the templates stay
label-agnostic.

Delivery is best effort and never fatal: a failed send returns
``delivered=False`` with a fixed ``error_code``, and the caller keeps the
notice in the outbox for the next flush (I6). Nothing here logs or returns the
target's chat id — ``target_display`` is the platform name plus a masked tail.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from . import messages

__all__ = [
    "Notifier",
    "NoticeResult",
    "LOCAL_TARGET",
    "PRIORITY_PLATFORMS",
    "parse_target",
    "mask_target",
    "resolve_target",
    "home_platform",
    "validate_target",
]

#: The popup/history sink: no messaging platform, no subprocess.
LOCAL_TARGET = "local"

#: Home-channel priority (§3). A platform not listed keeps registry order.
PRIORITY_PLATFORMS = ("telegram", "discord", "slack", "whatsapp", "signal")

_PLATFORM_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


@dataclass(frozen=True)
class NoticeResult:
    """Outcome of one delivery attempt (never carries the raw target)."""

    target_display: str
    delivered: bool
    skipped: bool = False
    error_code: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.delivered or self.skipped


def parse_target(value: Any) -> Optional[tuple[str, Optional[str], Optional[str]]]:
    """``(platform, chat_id, thread_id)`` or ``None`` when the value is unusable.

    Grammar (``hermes send --to``): ``platform``, ``platform:chat``,
    ``platform:chat:thread``. A chat id may contain ``:``-free text such as
    ``#ops`` or a negative number. ``local`` is accepted and means "no send".
    """
    text = str(value or "").strip()
    if not text:
        return None
    parts = text.split(":")
    platform = parts[0].strip().lower()
    if not _PLATFORM_RE.match(platform):
        return None
    if platform == LOCAL_TARGET:
        return (LOCAL_TARGET, None, None)
    if len(parts) == 1:
        return (platform, None, None)
    chat = parts[1].strip()
    if not chat:
        return None
    thread = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
    if len(parts) > 3:
        return None
    return (platform, chat, thread)


def validate_target(value: Any) -> bool:
    """True when ``value`` is empty (automatic) or a valid send target."""
    text = str(value or "").strip()
    if not text:
        return True
    return parse_target(text) is not None


def mask_target(value: Any) -> str:
    """A display string safe for the popup and the logs: no full chat id."""
    parsed = parse_target(value)
    if parsed is None:
        return LOCAL_TARGET
    platform, chat, thread = parsed
    if platform == LOCAL_TARGET or chat is None:
        return platform
    tail = chat[-4:] if len(chat) > 4 else "…"
    display = f"{platform}:…{tail}"
    if thread:
        display += f":…{thread[-3:]}" if len(thread) > 3 else ":…"
    return display


def home_platform() -> Optional[str]:
    """The bare platform name of the profile's home channel, or ``None``.

    Reads the same object ``hermes send`` reads (``load_gateway_config``), so
    the plugin and the CLI can never disagree about which platform is home.
    Priority follows §3; a platform with no ``chat_id`` is skipped rather than
    guessed at. Imported lazily and defensively: outside the Hermes runtime (a
    bare unit test) this returns ``None`` and the notifier falls back to
    ``local``.
    """
    try:  # pragma: no cover - exercised through the runtime in integration
        from gateway.config import load_gateway_config

        config = load_gateway_config()
        candidates: list[str] = []
        for platform in config.get_connected_platforms():
            name = str(getattr(platform, "value", platform))
            home = config.get_home_channel(platform)
            if home is None or not str(getattr(home, "chat_id", "") or "").strip():
                continue
            candidates.append(name)
    except Exception:
        return None
    if not candidates:
        return None
    for preferred in PRIORITY_PLATFORMS:
        if preferred in candidates:
            return preferred
    return candidates[0]


def resolve_target(
    configured: Any = "", *, home: Optional[Callable[[], Optional[str]]] = None
) -> tuple[str, bool]:
    """``(target, explicit)`` for this profile.

    ``explicit`` is True when the user set a target; False when the plugin
    resolved the home channel itself. ``target`` is ``"local"`` when nothing
    can deliver, which is a supported state, not an error.
    """
    text = str(configured or "").strip()
    if text:
        parsed = parse_target(text)
        if parsed is not None:
            return (text, True)
        # An invalid setting behaves as local with a settings warning; it never
        # silently becomes a different destination.
        return (LOCAL_TARGET, True)
    platform = (home or home_platform)()
    if platform:
        return (platform, False)
    return (LOCAL_TARGET, False)


class _Sender(Protocol):  # pragma: no cover - structural typing only
    def run(self, argv: Sequence[str], *, timeout: float = ...) -> Any: ...


class Notifier:
    """Send rendered notices through ``hermes send``.

    ``outbox`` is the store's durable queue: :meth:`flush` drains it, and a
    notice that fails stays queued with its attempt count. The notifier itself
    keeps no state, so a cron run, the dashboard and the CLI all behave the
    same way.

    ``resolve_label`` is the D-013 unmask, and it is the **only** place the full
    label exists in the notice path (R16): the outbox stores the masked label
    the plugin keeps, and this callable turns it into the full one in memory,
    at send time, only while ``mask_labels`` is off. Turning the setting back on
    therefore leaves nothing full on disk to clean up.
    """

    def __init__(
        self,
        cli: "_Sender",
        *,
        target: str = LOCAL_TARGET,
        language: str = messages.DEFAULT_LANGUAGE,
        mask_labels: bool = True,
        timeout: float = 30.0,
        clock: Optional[Callable[[], float]] = None,
        store: Any = None,
        resolve_label: Optional[Callable[[str], str]] = None,
    ) -> None:
        self.cli = cli
        self.target = target or LOCAL_TARGET
        self.language = language
        self.mask_labels = bool(mask_labels)
        self.timeout = float(timeout)
        self._clock = clock
        self._store = store
        self.resolve_label = resolve_label

    # -- rendering ---------------------------------------------------------

    def text(
        self, kind: str, params: Optional[Mapping[str, Any]] = None, *, dry_run: bool = False
    ) -> str:
        return messages.render(
            kind,
            self._visible(self._with_derived(kind, params)),
            language=self.language,
            dry_run=dry_run,
        )

    def _with_derived(
        self, kind: str, params: Optional[Mapping[str, Any]]
    ) -> Optional[Mapping[str, Any]]:
        """Fill a derived field's source from the store when the flow omitted it.

        ``account_error``'s sentence depends on **why** the account failed, and
        the flows queue only the label and the soonest expiry. The account row
        already holds the answer (``accounts.last_error_code``, written by the
        scan), so the notifier reads it here instead of guessing a category.
        A missing store or an unknown label is the unknown category, never an
        empty sentence.
        """
        canonical = messages.KIND_ALIASES.get(kind, kind)
        derived = messages.DERIVED.get(canonical)
        if not derived or not params:
            return params
        if any(source in params for source, _ in derived.values()):
            return params
        visible = dict(params)
        for source, table in derived.values():
            if table == "account_error":
                visible[source] = self._account_error_code(params.get("label"))
        return visible

    def _account_error_code(self, label: Any) -> str:
        """The stored error code of the account with this label, else ``""``.

        The comparison is **canonical** (R10/R16): the notice carries the masked
        label the plugin stores, while an account row written by an older build
        (or seeded by hand) may still hold a full address. Comparing the raw
        strings would silently lose the category and turn a network failure into
        "unexpected error", so both sides are reduced to the canonical label
        first.
        """
        from .accounts import canonical_label

        wanted = canonical_label(str(label or ""))
        if not wanted or self._store is None:
            return ""
        lister = getattr(self._store, "list_accounts", None)
        if not callable(lister):
            return ""
        try:
            rows = _as_list(lister())
        except Exception:  # noqa: BLE001 - a phrase is never worth failing a send
            return ""
        fallback = ""
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            if canonical_label(str(row.get("label") or "")) != wanted:
                continue
            code = str(row.get("last_error_code") or "").strip()
            if code:
                return code
            fallback = fallback or code
        return fallback

    def _visible(self, params: Optional[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
        """Apply the D-013 label rule to a notice's parameters.

        Only ``label`` is touched, and only in one of two directions:

        * masking **on** → the label is masked (idempotent, so a stored masked
          label stays as it is);
        * masking **off** → a label that is still masked is resolved through
          ``resolve_label`` in memory (R16). This is what lets the outbox keep
          only the masked string and still unmask at send time. A label that is
          already full (a direct ``notify`` call with the user's own text) is
          left exactly as it is.

        Every other parameter is a time or a fixed reason phrase, and the
        catalog refuses anything it did not declare.
        """
        if not params:
            return params
        label = params.get("label")
        if label is None:
            return params
        from .accounts import canonical_label, mask_label

        visible = dict(params)
        text = str(label)
        if self.mask_labels:
            visible["label"] = mask_label(text)
            return visible
        if self.resolve_label is None:
            return visible
        # Only a masked label is resolved: a full one is already what the user
        # asked to see, and re-resolving it could swap in another account.
        if canonical_label(text) != text:
            return visible
        try:
            full = self.resolve_label(text)
        except Exception:  # noqa: BLE001 - a failed unmask is a masked notice
            return visible
        if isinstance(full, str) and full.strip():
            visible["label"] = full.strip()
        return visible

    # -- delivery ----------------------------------------------------------

    def send_text(self, text: str, *, target: Optional[str] = None) -> NoticeResult:
        """Deliver one already-rendered text; never raises."""
        destination = target or self.target
        if not destination or destination == LOCAL_TARGET:
            return NoticeResult(target_display=LOCAL_TARGET, delivered=False, skipped=True)
        body = str(text or "").strip()
        if not body:
            return NoticeResult(
                target_display=mask_target(destination),
                delivered=False,
                skipped=True,
                error_code="empty_notice",
            )
        result = self.cli.run(["send", "-t", destination, "--json", body], timeout=self.timeout)
        display = mask_target(destination)
        if result.timed_out:
            return NoticeResult(target_display=display, delivered=False, error_code="send_timeout")
        if result.exec_error:
            return NoticeResult(
                target_display=display, delivered=False, error_code=result.exec_error
            )
        if result.returncode != 0:
            return NoticeResult(
                target_display=display,
                delivered=False,
                error_code=_send_error_code(result.stdout, result.stderr),
            )
        payload = _json_payload(result.stdout)
        if payload is None:
            # `--json` always prints a JSON object; a non-JSON success line means
            # the CLI changed. Treat it as delivered only on exit 0 with output.
            return NoticeResult(
                target_display=display,
                delivered=bool(result.stdout.strip()),
                error_code=None if result.stdout.strip() else "send_unconfirmed",
            )
        if payload.get("error"):
            return NoticeResult(target_display=display, delivered=False, error_code="send_failed")
        if payload.get("skipped"):
            return NoticeResult(
                target_display=display, delivered=False, skipped=True, error_code="send_skipped"
            )
        return NoticeResult(
            target_display=display,
            delivered=bool(payload.get("success")) or "note" in payload,
            error_code=None,
        )

    def notify(
        self,
        kind: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        target: Optional[str] = None,
        dry_run: bool = False,
    ) -> NoticeResult:
        """Render and deliver one notice."""
        try:
            text = self.text(kind, params, dry_run=dry_run)
        except messages.NoticeError:
            return NoticeResult(
                target_display=mask_target(target or self.target),
                delivered=False,
                skipped=True,
                error_code="bad_notice",
            )
        return self.send_text(text, target=target)

    # -- outbox ------------------------------------------------------------

    def enqueue(
        self,
        kind: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        credit_key: Optional[str] = None,
        dedupe_key: Optional[str] = None,
        target: Optional[str] = None,
    ) -> Optional[int]:
        """Queue one notice in the store's outbox (I6); ``None`` when unavailable.

        The row is written in the caller's own flow, so a crash between the
        state change and the send leaves the notice queued rather than lost.
        """
        store = self._store
        if store is None:
            return None
        key = dedupe_key or f"{kind}:{credit_key or ''}:{self._next_attempt_key()}"
        try:
            return store.enqueue_notice(
                key,
                kind=kind,
                message_key=kind,
                params=dict(params or {}),
                target=target or self.target,
                credit_key=credit_key,
            )
        except Exception:
            return None

    def _next_attempt_key(self) -> str:
        if self._clock is None:
            return "0"
        try:
            return str(int(self._clock()))
        except Exception:
            return "0"

    def flush(self, outbox: Any = None, *, limit: int = 20) -> list[NoticeResult]:
        """Drain up to ``limit`` *due* notices; never raises.

        ``outbox`` defaults to the store this notifier was built with. Two
        shapes are accepted, because the store and the tests differ:

        * the store's own API — ``due_notices(limit, max_attempts)`` plus
          ``mark_notice_sent`` / ``mark_notice_failed`` / ``mark_notice_blocked``
          / ``mark_notice_skipped`` (a store without ``due_notices`` falls back
          to ``list_notices(state="queued")``, i.e. no retry);
        * a small facade — ``pending(limit)`` plus ``mark_sent`` / ``mark_failed``.

        **Retry policy (R11).** A delivery failure is not the end of the row:

        * a *retryable* failure (timeout, spawn error, non-zero exit, an
          unparsable success) leaves the row ``failed`` with a **persisted
          deadline**: the store records ``next_attempt_at`` as an exponential
          wait (``RETRY_BASE_DELAY_S``, doubling, capped at
          ``RETRY_MAX_DELAY_S``). A flush before that instant does not attempt
          the row and does **not** spend an attempt, so a burst of cron ticks
          during one short outage cannot burn the budget; a flush after it
          retries. Once :data:`MAX_DELIVERY_ATTEMPTS` attempts have been made
          the row becomes ``abandoned`` and is never retried.
        * a *permanent configuration* failure (no home channel, platform not
          configured, a usage error, a notice the catalog refuses) becomes
          ``blocked``: retrying cannot succeed, so it is not attempted again and
          is never filed as a transient failure.
        * a ``local`` target or an already-sent row is untouched.

        **There is no exactly-once delivery guarantee.** If the process dies
        after ``hermes send`` accepted the message but before the row is marked
        ``sent``, the next flush delivers that notice a second time. Closing
        that window needs an idempotency key honoured by the delivery side,
        which ``hermes send`` does not offer; the honest contract is
        at-least-once with bounded retries, and the row keeps its attempt count
        so the duplicate is visible instead of silent.

        A missing or incompatible outbox is a no-op: notices are a side channel,
        never a reason for a run to fail.
        """
        source = outbox if outbox is not None else self._store
        if source is None:
            return []
        rows = _due_rows(source, limit, MAX_DELIVERY_ATTEMPTS)
        results: list[NoticeResult] = []
        for row in rows:
            row_id = row.get("id")
            body = row.get("text") or row.get("message")
            target = row.get("target") or self.target
            if not body:
                body = _render_row(
                    row, self.language, visible=self._visible, fill=self._with_derived
                )
            if not str(body or "").strip():
                # The catalog refused the row's kind or params: permanent, and
                # nothing is sent for it (R11).
                result = NoticeResult(
                    target_display=mask_target(target),
                    delivered=False,
                    error_code="bad_notice",
                )
            else:
                result = self.send_text(str(body), target=target)
            results.append(result)
            if row_id is None:
                continue
            _mark(source, row_id, result, MAX_DELIVERY_ATTEMPTS)
        return results


#: How many times one notice may be attempted before it is abandoned (R11).
#: Small on purpose: the outbox is drained by every scan/fire, so five attempts
#: span several hours of a transient messaging outage without keeping a stale
#: row alive for the whole 14-day retention window. The attempts are spaced by
#: the store's persisted ``next_attempt_at`` (``store.RETRY_BASE_DELAY_S``,
#: doubling, capped), so this cap bounds the *number* of tries while the
#: deadline bounds their *rate*.
MAX_DELIVERY_ATTEMPTS = 5

#: Error codes that mean "the configuration is wrong", not "the network was
#: unhappy": retrying one of these cannot succeed (R11).
PERMANENT_ERROR_CODES = frozenset(
    {
        "no_home_channel",
        "platform_not_configured",
        "send_usage",
        "bad_notice",
        "empty_notice",
    }
)


def _due_rows(source: Any, limit: int, max_attempts: int) -> list[dict]:
    """Rows a flush may attempt now, from either supported store shape.

    Never raises: an unreadable outbox means "nothing to do this run".
    """
    due = getattr(source, "due_notices", None)
    if callable(due):
        try:
            return [
                dict(row)
                for row in _as_list(due(limit=limit, max_attempts=max_attempts))
                if isinstance(row, Mapping)
            ]
        except Exception:
            return []
    return _pending_rows(source, limit)


def _pending_rows(source: Any, limit: int) -> list[dict]:
    """Queued outbox rows from either supported store shape (never raises)."""
    lister = getattr(source, "list_notices", None)
    if callable(lister):
        try:
            rows = lister(state="queued", limit=limit)
        except TypeError:
            try:
                rows = [row for row in _as_list(lister()) if str(row.get("state")) == "queued"][
                    :limit
                ]
            except Exception:
                return []
        except Exception:
            return []
        return [dict(row) for row in _as_list(rows) if isinstance(row, Mapping)]
    pending = getattr(source, "pending", None)
    if callable(pending):
        try:
            return [dict(row) for row in _as_list(pending(limit)) if isinstance(row, Mapping)]
        except Exception:
            return []
    return []


def _as_list(value: Any) -> list:
    """``list(value)`` for anything iterable, ``[]`` for a non-iterable store answer."""
    try:
        return list(value)
    except TypeError:
        return []


def _render_row(
    row: Mapping[str, Any],
    language: str,
    *,
    visible: Optional[Callable[[Optional[Mapping[str, Any]]], Optional[Mapping[str, Any]]]] = None,
    fill: Optional[Callable[[str, Mapping[str, Any]], Any]] = None,
) -> str:
    """Render a queued row that stored its kind + params instead of the text.

    ``visible`` is the notifier's own D-013 rule (``Notifier._visible``), so a
    row drained from the outbox is unmasked exactly the way a live notice is:
    the row itself only ever holds the masked label (R16).
    """
    kind = str(row.get("message_key") or row.get("kind") or "")
    params: Mapping[str, Any] = {}
    raw = row.get("params_json")
    if isinstance(raw, str) and raw:
        try:
            decoded = json.loads(raw)
        except ValueError:
            decoded = {}
        if isinstance(decoded, dict):
            params = decoded
    if fill is not None:
        params = fill(kind, params) or params
    if visible is not None:
        params = visible(params) or params
    try:
        return messages.render(kind, params, language=language)
    except messages.NoticeError:
        return ""


def _mark(source: Any, row_id: Any, result: NoticeResult, max_attempts: int = 0) -> None:
    """Record the outcome on the outbox row, whichever API the store exposes.

    A retryable failure goes through ``mark_notice_failed`` with the attempt
    budget so the row becomes ``abandoned`` at the cap; a permanent
    configuration error goes through ``mark_notice_blocked`` (R11). A store that
    only knows the older vocabulary still records *something* rather than
    losing the outcome.
    """
    if result.delivered:
        marker = getattr(source, "mark_notice_sent", None) or getattr(source, "mark_sent", None)
        if callable(marker):
            try:
                marker(row_id, None)
            except TypeError:
                marker(row_id)
        return
    if result.skipped:
        marker = getattr(source, "mark_notice_skipped", None)
        if callable(marker):
            marker(row_id, result.error_code)
        return
    code = result.error_code or "send_failed"
    if code in PERMANENT_ERROR_CODES:
        blocked = getattr(source, "mark_notice_blocked", None)
        if callable(blocked):
            blocked(row_id, code)
            return
    marker = getattr(source, "mark_notice_failed", None) or getattr(source, "mark_failed", None)
    if not callable(marker):
        return
    try:
        marker(row_id, code, max_attempts=max_attempts)
    except TypeError:
        try:
            marker(row_id, code)
        except TypeError:
            marker(row_id)


def _json_payload(text: str) -> Optional[dict]:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        # `hermes send --json` prints one object; a trailing notice line would
        # break a strict parse, so retry on the last object-shaped line.
        for line in reversed(raw.splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                break
        else:
            return None
    return payload if isinstance(payload, dict) else None


def _send_error_code(stdout: str, stderr: str) -> str:
    """A fixed code from the CLI's error text (never the text itself)."""
    text = f"{stdout}\n{stderr}".lower()
    if "no home channel" in text:
        return "no_home_channel"
    if "not configured" in text or "no targets" in text:
        return "platform_not_configured"
    if "usage error" in text or "required" in text:
        return "send_usage"
    return "send_failed"
