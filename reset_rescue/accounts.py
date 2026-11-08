"""Account discovery and authenticated requests (architecture A§4.3).

Two sources implement :class:`AccountSource`:

``PoolAccountSource`` (default, D-001)
    Reads every ``openai-codex`` row of the profile's credential pool through
    Hermes' own ``read_credential_pool`` — **read-only**, never ``load_pool``,
    never a write. Each row's own access token is sent only to
    ``https://chatgpt.com``. A 401 triggers at most one
    ``hermes auth refresh openai-codex <entry id>`` per account per run, then a
    pool re-read and one retry (I4/R2/R4).

``ActiveSignInSource`` (fallback)
    One account, through Hermes' ``credentialed_provider_request``, used when
    the pool slice is empty or ``account_source = active_sign_in``.

Secrets discipline (I8/I12): a pool row is never copied, cached, logged or
serialized. ``request()`` keeps the token in local variables only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from .clock import Deps

__all__ = [
    "AccountRef",
    "AccountSource",
    "AccountMismatch",
    "AuthExpired",
    "Response",
    "SourceUnavailable",
    "PoolAccountSource",
    "ActiveSignInSource",
    "select_source",
    "mask_label",
    "mask_email",
    "canonical_label",
    "AccountIdentity",
    "canonical_identity",
    "account_key_for",
    "jwt_claims",
    "OFFICIAL_ORIGIN",
    "CREDITS_PATH",
    "CONSUME_PATH",
    "USAGE_PATH",
]

#: The only origin a token may be sent to (I12).
OFFICIAL_ORIGIN = "https://chatgpt.com"

CREDITS_PATH = "/backend-api/wham/rate-limit-reset-credits"
CONSUME_PATH = "/backend-api/wham/rate-limit-reset-credits/consume"
USAGE_PATH = "/backend-api/wham/usage"

#: Fixed, non-secret failure codes.
ERR_AUTH_EXPIRED = "auth_expired"
ERR_UNSUPPORTED_ENDPOINT = "unsupported_endpoint"
ERR_ACCOUNT_MISSING = "account_missing"
ERR_POOL_UNREADABLE = "pool_unreadable"
ERR_PROVIDER_NOT_SIGNED_IN = "provider_not_signed_in"
ERR_PROVIDER_PERMISSION = "provider_permission"
ERR_NETWORK = "network"
ERR_TIMEOUT = "timeout"
ERR_HTTP = "http_error"
ERR_REDIRECT = "redirect_refused"

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+$")
#: Second-level labels that sit under a two-letter country code (``com.br``, ``co.uk``).
_SECOND_LEVEL = re.compile(r"^(com|co|net|org|gov|edu|ac|or|ne|go|gob|mil)$")
_HEX16 = re.compile(r"^[0-9a-f]{16}$")


# --------------------------------------------------------------------- values


@dataclass(frozen=True)
class Response:
    """A bounded HTTP response: status plus a parsed JSON object.

    ``body`` is the parsed payload only; the raw bytes are dropped by the
    transport (A§4.4). ``None`` means the body was not JSON.
    """

    status: int
    body: Optional[Mapping[str, Any]] = None
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def __repr__(self) -> str:  # never print a body
        return f"Response(status={self.status})"


class SourceUnavailable(Exception):
    """The source cannot answer at all (no pool row, not signed in, packaging)."""

    def __init__(self, code: str, *, detail: Optional[str] = None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class AuthExpired(SourceUnavailable):
    """A 401 that a Hermes refresh could not fix (I4)."""

    def __init__(self, label: str = "", *, detail: Optional[str] = None):
        super().__init__(ERR_AUTH_EXPIRED, detail=detail)
        self.label = label


class AccountMismatch(SourceUnavailable):
    """Active-sign-in mode signed in to a different account than the row needs."""

    def __init__(self, detail: Optional[str] = None):
        super().__init__(ERR_ACCOUNT_MISSING, detail=detail)


@dataclass(frozen=True)
class AccountRef:
    """One Codex account, safe to hold in memory and to serialize.

    ``account_key`` is ``sha256(salt ‖ ChatGPT account id)[:16]``: stable across
    sources and re-logins, and never the raw account id (A§4.3, I8).
    """

    account_key: str
    label: str
    source_kind: str
    entry_id: Optional[str] = None

    def __repr__(self) -> str:
        return (
            f"AccountRef(account_key={self.account_key!r}, label={self.label!r}, "
            f"source_kind={self.source_kind!r})"
        )


# ------------------------------------------------------------------- helpers


def mask_email(value: str) -> str:
    """Mask an address so neither the local part nor the domain name shows (D-028).

    ``jane.doe@example.com.br`` → ``j***@e***.com.br``; ``example@example.com``
    → ``e***@e***.com``. The first character of the local part, then ``***``;
    the first character of the first domain label, then ``***``; then the public
    suffix: the last label, or the last two when the last is a two-letter country
    code and the label before it is a short second-level label (``com.br``,
    ``co.uk``). Lower-cased, idempotent, and ``""`` for anything that is not an
    address.
    """
    text = (value or "").strip().lower()
    if not _EMAIL.match(text):
        return ""
    local, _, domain = text.partition("@")
    labels = domain.split(".")
    if not local or any(not label for label in labels):
        return ""
    if len(labels) == 1:
        return f"{local[:1]}***@{labels[0][:1]}***"
    keep = 2 if len(labels) >= 3 and len(labels[-1]) == 2 and _SECOND_LEVEL.match(labels[-2]) else 1
    suffix = ".".join(labels[-keep:])
    return f"{local[:1]}***@{labels[0][:1]}***.{suffix}"


def mask_label(value: str, *, fallback_key: str = "") -> str:
    """Mask a pool label: an email is masked, anything else is shortened.

    A label that already looks masked is returned as is, so masking twice is
    harmless.
    """
    text = (value or "").strip()
    masked = mask_email(text)
    if masked:
        return masked
    if "***" in text and len(text) <= 64:
        return text
    if not text:
        return f"Codex account {fallback_key[:4]}" if fallback_key else "Codex account"
    # A non-email label: keep a short prefix, drop anything after whitespace so
    # a stray token or path cannot ride along.
    head = text.split()[0]
    if len(head) <= 8:
        return head
    return f"{head[:8]}…"


def canonical_label(value: str) -> str:
    """The **canonical** label of an account: masked, stable, never presentation.

    This is the string the plugin stores in ``accounts.label`` and the string an
    ``excluded_accounts`` entry may name. It is derived from a label only by
    masking it when it is a full e-mail address; anything else (an already
    masked address, ``Codex account abcd``) is returned exactly as stored, so
    two different accounts never collapse onto one canonical label.

    Masking is idempotent here: ``mask_email("b***@example.com")`` is
    ``"b***@example.com"``, so a row written by an older build and a row written
    today produce the same identity (R10).
    """
    text = (value or "").strip()
    if not text:
        return ""
    return mask_email(text) or text


@dataclass(frozen=True)
class AccountIdentity:
    """Who an account **is**, for decisions — never for display (R10).

    ``account_key`` is the stable salted key; ``label`` is the canonical masked
    label the plugin stores. The presentation label (which may be the full
    address when the user turned masking off, D-013) is a different thing and
    must never reach a decision: an exclusion written by the masked label would
    stop matching the moment unmasking was turned on.
    """

    account_key: str
    label: str

    def __repr__(self) -> str:
        return f"AccountIdentity(account_key={self.account_key!r}, label={self.label!r})"


def canonical_identity(store: Any, account_key: str, account: Any = None) -> AccountIdentity:
    """Build the one canonical identity every decision uses (R10).

    The stored ``accounts.label`` wins, because that is the value the user sees
    in the popup and can copy into ``excluded_accounts``; the live source label
    is the fallback for an account the store has not seen yet. Either way the
    result is passed through :func:`canonical_label`, so a source that hands a
    full address cannot change who the account is.

    ``store`` is duck-typed (``get_account``), so this module still never
    imports the store and a test can pass a fake.
    """
    key = str(account_key or "")
    label = ""
    getter = getattr(store, "get_account", None)
    if callable(getter):
        try:
            found = getter(key)
        except Exception:  # noqa: BLE001 - an unreadable row is not a new identity
            found = None
        if isinstance(found, Mapping):
            label = str(found.get("label") or "")
    if not label:
        label = str(getattr(account, "label", "") or "")
    return AccountIdentity(account_key=key, label=canonical_label(label))


def jwt_claims(token: str) -> Mapping[str, Any]:
    """Decode (never verify) the payload claims of a JWT access token.

    Same rule as Hermes' own ``agent/codex_headers.py``: a malformed token
    yields ``{}`` rather than raising. Nothing is stored.
    """
    if not isinstance(token, str) or not token.strip():
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload)
        claims = json.loads(raw)
    except Exception:  # noqa: BLE001 - a malformed token is simply anonymous
        return {}
    return claims if isinstance(claims, dict) else {}


def _auth_claims(token: str) -> Mapping[str, Any]:
    claims = jwt_claims(token)
    auth = claims.get("https://api.openai.com/auth")
    return auth if isinstance(auth, dict) else {}


def chatgpt_account_id(token: str) -> str:
    """The ChatGPT workspace-account id from the token's claims (``""``)."""
    value = _auth_claims(token).get("chatgpt_account_id")
    return value.strip() if isinstance(value, str) else ""


def chatgpt_user_id(token: str) -> str:
    """The stable per-user claim of a Codex token (``""`` when absent).

    S2 (``docs/evidence/S2.md``) found four users sharing one workspace
    account, so a workspace id alone collapses real accounts. Hermes' own
    ``agent/credential_pool._codex_principal_identity`` uses ``sub`` for this;
    the Codex auth block also carries ``chatgpt_user_id`` / ``user_id`` on some
    tokens, so those are the fallbacks.
    """
    claims = jwt_claims(token)
    for name in ("sub",):
        value = claims.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    auth = _auth_claims(token)
    for name in ("chatgpt_user_id", "user_id", "chatgpt_account_user_id"):
        value = auth.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def identity_material(account_id: str, user_id: str = "") -> str:
    """The string an account key is derived from: **user plus workspace** (S2).

    A workspace alone is not an identity: members of one ChatGPT workspace
    share ``chatgpt_account_id`` and hold their own credits. When the token
    carries no user claim the bare account id is used, which keeps the key
    stable for older tokens instead of collapsing rows that cannot be told
    apart.
    """
    workspace = str(account_id or "").strip()
    user = str(user_id or "").strip()
    if not user:
        return workspace
    return f"{workspace}:{user}"


def account_key_for(salt: str, backend_account_id: str) -> str:
    """``sha256(salt ‖ identity)[:16]`` (A§4.3, A§4.5).

    ``backend_account_id`` is the output of :func:`identity_material` for a
    real account (user plus workspace); a bare workspace id still works and is
    what an older token yields.
    """
    material = f"{salt}{backend_account_id}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:16]


def is_official_base_url(base_url: str) -> bool:
    """True when a row's ``base_url`` points at ``https://chatgpt.com``.

    A custom gateway is skipped with ``unsupported_endpoint`` and never
    receives a request (A§4.3, T-33).
    """
    text = (base_url or "").strip()
    if not text:
        return True  # absent base_url means the default official endpoint
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return host == "chatgpt.com"


# --------------------------------------------------------------------- source


class AccountSource(ABC):
    """Discovery plus authenticated requests for one profile (A§4.3)."""

    kind: str = ""

    @abstractmethod
    def accounts(self) -> list[AccountRef]:
        """Every usable account, deduplicated and masked."""

    @abstractmethod
    def request(
        self,
        account: AccountRef,
        method: str,
        url: str,
        *,
        json: object | None = None,
        timeout: float = 30.0,
    ) -> Response:
        """Perform one request for ``account``.

        Raises :class:`AuthExpired` when a 401 survives the one allowed
        refresh, :class:`AccountMismatch` in active-sign-in mode when the
        signed-in account is not the requested one, and
        :class:`SourceUnavailable` for any other transport-level failure.
        """

    def coverage_note(self) -> str:
        """i18n key describing what this source covers (shown in the popup)."""
        return f"coverage.{self.kind or 'unknown'}"

    def close(self) -> None:  # noqa: B027 - an optional hook, not a required override
        """Drop any cached token material.

        Deliberately concrete and empty: most sources hold nothing to release,
        and a subclass that does (a pooled transport, say) overrides it. Marking
        it abstract would force every source to write a no-op.
        """


# ---------------------------------------------------------------- urllib HTTP


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: a 3xx is reported, never followed (A§4.4)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401,ANN001
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _urllib_request(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: Optional[bytes],
    timeout: float,
) -> Response:
    """One request with stdlib urllib, TLS verification on, no redirects."""
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 0) or 0)
            raw = response.read()
            parsed = _parse_json(raw)
            return Response(
                status=status,
                body=parsed,
                headers={k.lower(): v for k, v in response.headers.items()},
            )
    except urllib.error.HTTPError as exc:
        # A 4xx/5xx is a response, not a transport failure: keep the status and
        # the parsed body (the caller decides what the code means).
        try:
            raw = exc.read()
        except Exception:  # noqa: BLE001 - a body we cannot read is simply absent
            raw = b""
        return Response(
            status=int(exc.code or 0),
            body=_parse_json(raw),
            headers={k.lower(): v for k, v in (exc.headers or {}).items()},
        )
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            raise SourceUnavailable(ERR_TIMEOUT) from None
        raise SourceUnavailable(ERR_NETWORK) from None
    except TimeoutError:
        raise SourceUnavailable(ERR_TIMEOUT) from None
    except OSError:
        raise SourceUnavailable(ERR_NETWORK) from None


def _parse_json(raw: bytes) -> Optional[Mapping[str, Any]]:
    if not raw:
        return None
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def build_headers(token: str, account_id: str = "") -> dict:
    """The Codex CLI headers Hermes' own usage fetcher sends (A§4.3).

    Built locally: ``agent.codex_headers`` is not on the plugin allow-list.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "codex-cli",
    }
    auth = _auth_claims(token)
    claimed = auth.get("chatgpt_account_id")
    effective = account_id or (claimed.strip() if isinstance(claimed, str) else "")
    if effective:
        headers["ChatGPT-Account-ID"] = effective
    residency = auth.get("chatgpt_data_residency") or auth.get("chatgpt_compute_residency")
    if isinstance(residency, str) and residency.strip():
        headers["x-openai-internal-codex-residency"] = residency.strip()
    return headers


# ------------------------------------------------------------- pool reading


class PoolAccountSource(AccountSource):
    """Every ``openai-codex`` row of the profile's credential pool (D-001).

    ``read_pool`` is injected so this module never imports Hermes at load time
    and so a test can hand it a fake. The default resolves
    ``agent.credential_pool.read_credential_pool`` lazily, on first use.
    """

    kind = "pool"

    def __init__(
        self,
        *,
        salt: str,
        deps: Optional[Deps] = None,
        read_pool: Optional[Callable[[], Sequence[Mapping[str, Any]]]] = None,
        refresh: Optional[Callable[[str], bool]] = None,
        transport: Optional[Callable[..., Response]] = None,
    ) -> None:
        self.salt = salt
        self.deps = deps or Deps()
        self._read_pool = read_pool or _default_read_pool
        self._refresh = refresh or self._default_refresh
        self._transport = transport or _urllib_request
        #: ``entry_id -> {"access_token": ..., "account_id": ..., "base_url": ...}``
        #: Rebuilt on every ``accounts()`` and after a refresh; never persisted.
        self._rows: dict[str, dict] = {}
        #: Accounts discovered by the last ``accounts()`` call.
        self._refs: dict[str, AccountRef] = {}
        #: ``account_key -> entry_id`` used by the last ``accounts()`` call.
        self._by_key: dict[str, str] = {}
        #: Accounts refreshed in this run (I4: at most once per account).
        self._refreshed: set[str] = set()
        self._refresh_failed: dict[str, str] = {}
        self._duplicates: dict[str, list[str]] = {}
        self._skipped: dict[str, str] = {}
        #: ``account_key -> unmasked label`` for the D-013 ``mask_labels`` off
        #: path. Never persisted, never in a DTO unless the user turned masking
        #: off; the DB keeps the masked label.
        self._full_labels: dict[str, str] = {}

    # -- discovery ---------------------------------------------------------

    def accounts(self) -> list[AccountRef]:
        rows = self._load_rows()
        grouped: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
        skipped: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                continue  # type: ignore[unreachable]
            entry_id = str(row.get("id") or "").strip()
            token = str(row.get("access_token") or "").strip()
            if not entry_id or not token:
                continue
            base_url = str(row.get("base_url") or "")
            if not is_official_base_url(base_url):
                skipped[entry_id] = ERR_UNSUPPORTED_ENDPOINT
                continue
            account_id = str(row.get("account_id") or "").strip() or chatgpt_account_id(token)
            if not account_id:
                # No identity: cannot build a stable key, so the row is not
                # usable. Reported as a skipped entry, never guessed.
                skipped[entry_id] = ERR_ACCOUNT_MISSING
                continue
            # S2: a workspace id alone is not an identity — four users shared
            # one workspace on the maintainer's profile. Group by user plus
            # workspace, and fall back to the bare workspace only when the
            # token carries no user claim (then the rows cannot be told apart).
            identity = identity_material(account_id, chatgpt_user_id(token))
            grouped.setdefault(identity, []).append((entry_id, row))

        refs: list[AccountRef] = []
        by_key: dict[str, str] = {}
        rows_by_entry: dict[str, dict] = {}
        duplicates: dict[str, list[str]] = {}
        full_labels: dict[str, str] = {}
        for identity, entries in grouped.items():
            chosen_id, chosen_row = _choose_row(entries)
            key = account_key_for(self.salt, identity)
            label = _label_for(chosen_row, key)
            refs.append(
                AccountRef(account_key=key, label=label, source_kind=self.kind, entry_id=chosen_id)
            )
            by_key[key] = chosen_id
            token = str(chosen_row.get("access_token") or "").strip()
            rows_by_entry[chosen_id] = {
                "access_token": token,
                # The HTTP header is the **workspace** id, never the local
                # identity: ``identity`` is ``workspace:user`` so two members of
                # one workspace keep separate keys, but sending that composite
                # as ``ChatGPT-Account-ID`` names a different account to the
                # backend (R3).
                "account_id": _workspace_id(chosen_row, token),
                "base_url": str(chosen_row.get("base_url") or ""),
            }
            full = _full_label_for(chosen_row)
            if full:
                full_labels[key] = full
            others = [entry_id for entry_id, _ in entries if entry_id != chosen_id]
            if others:
                duplicates[key] = others
        refs.sort(key=lambda ref: ref.account_key)
        self._refs = {ref.account_key: ref for ref in refs}
        self._by_key = by_key
        self._rows = rows_by_entry
        self._duplicates = duplicates
        self._skipped = skipped
        self._full_labels = full_labels
        return refs

    def _load_rows(self) -> Sequence[Mapping[str, Any]]:
        try:
            rows = self._read_pool()
        except Exception as exc:  # noqa: BLE001 - reported as a fixed code
            raise SourceUnavailable(ERR_POOL_UNREADABLE, detail=type(exc).__name__) from None
        return rows if isinstance(rows, (list, tuple)) else []

    # -- introspection used by the DTO layer -------------------------------

    def duplicates(self) -> Mapping[str, list[str]]:
        """``account_key -> other entry ids`` for duplicate sign-ins (T-33)."""
        return dict(self._duplicates)

    def skipped(self) -> Mapping[str, str]:
        """``entry_id -> reason`` for rows that never receive a request."""
        return dict(self._skipped)

    def entry_for(self, account_key: str) -> Optional[str]:
        return self._by_key.get(account_key)

    def full_label(self, account_key: str) -> str:
        """The unmasked label for D-013's ``mask_labels: false`` (``""`` if none).

        Only the popup/notice path asks for this, and only when the user turned
        masking off. Nothing here is logged or persisted: the store keeps the
        masked label, so turning the setting back on needs no cleanup.
        """
        return self._full_labels.get(account_key, "")

    # -- requests ----------------------------------------------------------

    def request(
        self,
        account: AccountRef,
        method: str,
        url: str,
        *,
        json: object | None = None,
        timeout: float = 30.0,
    ) -> Response:
        if not _url_allowed(url):
            raise SourceUnavailable(ERR_UNSUPPORTED_ENDPOINT)
        entry_id = self._by_key.get(account.account_key) or account.entry_id
        if not entry_id:
            raise SourceUnavailable(ERR_ACCOUNT_MISSING)
        row = self._rows.get(entry_id)
        if row is None:
            # Not discovered in this run: re-read once so a fresh source can
            # serve a key persisted by an earlier run.
            self.accounts()
            row = self._rows.get(entry_id)
        if row is None:
            raise SourceUnavailable(ERR_ACCOUNT_MISSING)

        response = self._send(row, method, url, json, timeout)
        if response.status == 401:
            if not self._refresh_account(account, entry_id):
                raise AuthExpired(account.label)
            row = self._rows.get(entry_id)
            if row is None:
                raise AuthExpired(account.label)
            response = self._send(row, method, url, json, timeout)
            if response.status == 401:
                raise AuthExpired(account.label)
        return response

    def _send(
        self, row: Mapping[str, Any], method: str, url: str, body: object | None, timeout: float
    ) -> Response:
        headers = build_headers(
            str(row.get("access_token") or ""), str(row.get("account_id") or "")
        )
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json_dumps(body)
        return self._transport(method, url, headers, payload, timeout)

    # -- refresh -----------------------------------------------------------

    def _refresh_account(self, account: AccountRef, entry_id: str) -> bool:
        """One Hermes-owned refresh per account per run (I4, R-004)."""
        if entry_id in self._refreshed:
            return False
        self._refreshed.add(entry_id)
        try:
            ok = bool(self._refresh(entry_id))
        except Exception:  # noqa: BLE001 - a failed refresh is a fixed code
            ok = False
        if not ok:
            self._refresh_failed[entry_id] = ERR_AUTH_EXPIRED
            return False
        # Re-read the pool so every later request in this run uses the new
        # token (R4: the old token must never be reused after a refresh).
        try:
            rows = self._load_rows()
        except SourceUnavailable:
            self._refresh_failed[entry_id] = ERR_POOL_UNREADABLE
            return False
        for row in rows:
            if not isinstance(row, Mapping):
                continue  # type: ignore[unreachable]
            if str(row.get("id") or "").strip() != entry_id:
                continue
            token = str(row.get("access_token") or "").strip()
            if not token:
                return False
            self._rows[entry_id] = {
                "access_token": token,
                "account_id": _workspace_id(row, token),
                "base_url": str(row.get("base_url") or ""),
            }
            return True
        return False

    def _default_refresh(self, entry_id: str) -> bool:
        """``hermes auth refresh openai-codex <entry id>`` through ``cli_runner``."""
        runner = getattr(self.deps, "cli", None)
        if runner is None:
            return False
        result = runner.run(["auth", "refresh", "openai-codex", entry_id], timeout=60)
        return bool(getattr(result, "ok", False))

    def close(self) -> None:
        self._rows.clear()
        self._refs.clear()


def _choose_row(entries: Sequence[tuple[str, Mapping[str, Any]]]) -> tuple[str, Mapping[str, Any]]:
    """Pick the row to use for one account id: ok status, then newest refresh."""

    def sort_key(item: tuple[str, Mapping[str, Any]]) -> tuple:
        entry_id, row = item
        status = str(row.get("last_status") or "").lower()
        ok_rank = 0 if status in ("ok", "active", "") else 1
        refreshed = row.get("last_refresh") or row.get("obtained_at") or ""
        return (ok_rank, _neg_text(str(refreshed)), entry_id)

    return sorted(entries, key=sort_key)[0]


def _neg_text(text: str) -> str:
    """Sort newest ISO timestamp first using plain string ordering."""
    return "".join(chr(0x10FFFF - ord(ch)) if ord(ch) < 0x10FFFF else ch for ch in text)


def _workspace_id(row: Mapping[str, Any], token: str) -> str:
    """The **workspace** id a request must name in ``ChatGPT-Account-ID`` (R3).

    The local identity is ``workspace:user`` (S2) so two members of one
    workspace keep separate keys; the HTTP header is not that string. A pool
    row's stored ``account_id`` is used when it is a plain workspace id, and the
    token's own claim is the fallback — never the composite.
    """
    stored = str(row.get("account_id") or "").strip()
    if stored and ":" not in stored:
        return stored
    return chatgpt_account_id(token)


def _label_for(row: Mapping[str, Any], key: str) -> str:
    """Masked email claim, else the masked pool label, else ``Codex account <key4>``."""
    token = str(row.get("access_token") or "")
    claims = jwt_claims(token)
    email = claims.get("email") or _auth_claims(token).get("email")
    if isinstance(email, str) and email.strip():
        masked = mask_email(email.strip())
        if masked:
            return masked
    return mask_label(str(row.get("label") or ""), fallback_key=key)


def _full_label_for(row: Mapping[str, Any]) -> str:
    """The unmasked label for D-013, or ``""`` when the row has none.

    Prefers the row's own stored label (what Hermes shows in its provider
    list), then the token's email claim. Only read when ``mask_labels`` is off;
    never written to the store or a log.
    """
    stored = str(row.get("label") or "").strip()
    if stored:
        return stored
    token = str(row.get("access_token") or "")
    claims = jwt_claims(token)
    email = claims.get("email") or _auth_claims(token).get("email")
    return email.strip() if isinstance(email, str) else ""


def _url_allowed(url: str) -> bool:
    """Only ``https://chatgpt.com`` and never a redirect target (I12)."""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").lower() == "chatgpt.com"
        and parsed.port in (None, 443)
    )


def json_dumps(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _default_read_pool() -> Sequence[Mapping[str, Any]]:
    """``agent.credential_pool.read_credential_pool('openai-codex')``, read-only.

    Imported lazily so the module loads outside Hermes (tests, lint) and so a
    missing Hermes runtime is a clean ``pool_unreadable`` rather than an
    import crash at plugin load.
    """
    from agent.credential_pool import read_credential_pool  # noqa: PLC0415

    rows = read_credential_pool("openai-codex")
    return rows if isinstance(rows, list) else []


# ------------------------------------------------------- active sign-in source


class ActiveSignInSource(AccountSource):
    """The single account Hermes is signed in to, through Hermes' own helper.

    ``request_fn`` is injected (default: ``credentialed_provider_request``) so
    this module never imports Hermes at load time and a test can fake it.
    """

    kind = "active_sign_in"

    def __init__(
        self,
        *,
        salt: str,
        deps: Optional[Deps] = None,
        request_fn: Optional[Callable[..., Any]] = None,
        retry_delay_s: float = 2.0,
    ) -> None:
        self.salt = salt
        self.deps = deps or Deps()
        self._request_fn = request_fn or _default_provider_request
        self._retry_delay_s = retry_delay_s
        self._account: Optional[AccountRef] = None
        self._account_id: str = ""
        self._full_label: str = ""

    def accounts(self) -> list[AccountRef]:
        if self._account is not None:
            return [self._account]
        response = self._call("GET", OFFICIAL_ORIGIN + USAGE_PATH)
        claims = _response_identity(response)
        account_id = claims.get("account_id") or ""
        if not account_id:
            # Without an identity there is no stable key; the account still
            # exists, so a deterministic placeholder keeps state rows alive.
            account_id = "active-sign-in"
        # S2: the same user-plus-workspace rule as the pool source, so one
        # account keeps one key whichever source served it.
        identity = identity_material(account_id, claims.get("user_id") or "")
        key = account_key_for(self.salt, identity)
        label = mask_email(claims.get("email") or "") or f"Codex account {key[:4]}"
        self._account_id = identity
        self._account = AccountRef(
            account_key=key, label=label, source_kind=self.kind, entry_id=None
        )
        self._full_label = str(claims.get("email") or "").strip()
        return [self._account]

    def full_label(self, account_key: str) -> str:
        """The unmasked label for D-013 (``""`` when unknown)."""
        if self._account is not None and self._account.account_key == account_key:
            return self._full_label
        return ""

    def request(
        self,
        account: AccountRef,
        method: str,
        url: str,
        *,
        json: object | None = None,
        timeout: float = 30.0,
    ) -> Response:
        if not _url_allowed(url):
            raise SourceUnavailable(ERR_UNSUPPORTED_ENDPOINT)
        if self._account is not None and account.account_key != self._account.account_key:
            raise AccountMismatch()
        response = self._call(method, url, json=json, timeout=timeout)
        if response.status == 401:
            # One delayed retry through Hermes' own helper; never a plugin
            # refresh (A§4.3).
            self.deps.wait(self._retry_delay_s)
            response = self._call(method, url, json=json, timeout=timeout)
            if response.status == 401:
                raise AuthExpired(account.label)
        return response

    def _call(
        self, method: str, url: str, *, json: object | None = None, timeout: float = 30.0
    ) -> Response:
        try:
            raw = self._request_fn("openai-codex", method, url, json=json, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - mapped to a fixed code
            code = _provider_error_code(exc)
            raise SourceUnavailable(code, detail=type(exc).__name__) from None
        return _coerce_response(raw)

    def close(self) -> None:
        self._account = None


def _provider_error_code(exc: Exception) -> str:
    name = type(exc).__name__
    if name == "ProviderNotSignedIn":
        return ERR_PROVIDER_NOT_SIGNED_IN
    if isinstance(exc, PermissionError) or name == "PermissionError":
        return ERR_PROVIDER_PERMISSION
    if isinstance(exc, TimeoutError):
        return ERR_TIMEOUT
    return ERR_NETWORK


def _coerce_response(raw: Any) -> Response:
    """Accept a ``Response`` or the helper's ``(status, headers, body)`` shape."""
    if isinstance(raw, Response):
        return raw
    if isinstance(raw, tuple) and len(raw) == 3:
        status, headers, body = raw
        parsed = (
            body
            if isinstance(body, dict)
            else _parse_json(body if isinstance(body, bytes) else str(body or "").encode("utf-8"))
        )
        return Response(
            status=int(status or 0),
            body=parsed,
            headers={str(k).lower(): str(v) for k, v in (headers or {}).items()},
        )
    status = getattr(raw, "status", None) or getattr(raw, "status_code", None) or 0
    body = getattr(raw, "body", None)
    if isinstance(body, (bytes, str)):
        body = _parse_json(body if isinstance(body, bytes) else body.encode("utf-8"))
    return Response(status=int(status or 0), body=body if isinstance(body, dict) else None)


def _response_identity(response: Response) -> dict:
    """Best-effort identity from a usage response (never persisted raw)."""
    body = response.body or {}
    identity: dict[str, str] = {}
    for source in (body, body.get("account") if isinstance(body.get("account"), dict) else {}):
        if not isinstance(source, dict):
            continue
        for name in ("account_id", "chatgpt_account_id"):
            value = source.get(name)
            if isinstance(value, str) and value.strip():
                identity.setdefault("account_id", value.strip())
        for name in ("user_id", "chatgpt_user_id"):
            value = source.get(name)
            if isinstance(value, str) and value.strip():
                identity.setdefault("user_id", value.strip())
        email = source.get("email")
        if isinstance(email, str) and email.strip():
            identity.setdefault("email", email.strip())
    return identity


def _default_provider_request(
    provider: str, method: str, url: str, *, json: object | None = None, timeout: float = 30.0
) -> Any:
    """``credentialed_provider_request`` — Hermes attaches and refreshes the token."""
    from hermes_cli.plugin_provider_requests import credentialed_provider_request  # noqa: PLC0415

    kwargs: dict[str, Any] = {"timeout": timeout}
    if json is not None:
        kwargs["json"] = json
    return credentialed_provider_request(provider, method, url, **kwargs)


# ------------------------------------------------------------------ selection


def select_source(deps: Deps, settings: Any = None) -> AccountSource:
    """Pick the source for this operation (A§4.3).

    ``account_source = active_sign_in`` forces the fallback; ``pool`` (default)
    uses the pool when it has at least one usable row and falls back to the
    active sign-in when the slice is empty.
    """
    salt = str(getattr(deps, "salt", "") or "")
    requested = str(getattr(settings, "account_source", "pool") or "pool")
    if requested == "active_sign_in":
        return ActiveSignInSource(salt=salt, deps=deps)
    pool = PoolAccountSource(salt=salt, deps=deps)
    try:
        rows = pool.accounts()
    except SourceUnavailable:
        rows = []
    if rows:
        return pool
    return ActiveSignInSource(salt=salt, deps=deps)
