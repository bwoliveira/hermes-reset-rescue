"""Every user-facing sentence Reset Rescue sends (simplification §5, T-20/T-51/T-74).

One catalog, two languages, no other place where notice text is written. The
rules this module enforces so a reviewer can check them mechanically:

* **Same placeholders in both languages.** :func:`validate_catalog` compares
  the ``{field}`` sets of the ``en`` and ``pt-BR`` templates of every kind;
  the test suite fails if they differ.
* **No ids, paths or internal names.** Templates may only use the fields
  listed in :data:`PARAMS` for their kind; the renderer refuses anything else,
  so a caller cannot smuggle a credit id, a chat id or a file path into a
  notice by adding a parameter.
* **One line, friendly, an emoji** (D-012, §5). Nothing here explains the
  plugin's internals; the README does.
* **Times in the user's zone** (D-011). Every ``*_at`` value is an epoch
  second rendered through :class:`TimeRenderer`, which uses Hermes' configured
  zone when there is one and the host's zone otherwise. ``MMM D, HH:MM`` in
  English; ``DD/MM às HH:MM`` in pt-BR. Each kind declares which of its fields
  is a full stamp and which is a bare time, because the §5 examples use both.
* **No quota percentage anywhere** (D-010) and **no masking** (R-006): the
  label arrives as Hermes stores it and is printed verbatim.
* **Dry run is a prefix, not a kind** (§5): ``🧪 [Dry run] `` / ``🧪 [Simulação] ``.
* **A derived field is never a caller's string.** ``account_error``'s ``{reason}``
  is a fixed phrase chosen by the account's error *code*
  (:func:`account_error_text`): auth, network, rate-limited and unknown each
  have their own sentence, so a network failure can never read as an expired
  sign-in. The code arrives as ``reason_code``, which :data:`DERIVED` records as
  the *source* of ``{reason}`` and which is therefore accepted by the renderer
  without being a parameter the sentence shows. A caller that supplies no code
  gets the unknown phrase, never a hole.

The module imports nothing from the rest of the package and never touches the
network or the filesystem at import time.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any, Mapping, Optional

__all__ = [
    "KINDS",
    "PARAMS",
    "DERIVED",
    "TIME_KEYS",
    "STAMP_KEYS",
    "LANGUAGES",
    "DEFAULT_LANGUAGE",
    "DRY_RUN_PREFIX",
    "NoticeError",
    "TimeRenderer",
    "resolve_language",
    "local_zone",
    "render",
    "reason_text",
    "account_error_text",
    "validate_catalog",
    "template_for",
    "KIND_ALIASES",
]

LANGUAGES = ("en", "pt-BR")
DEFAULT_LANGUAGE = "en"

#: The notice kinds of §5. Everything else was cut in the simplification pass.
KINDS = (
    "reminder",
    "success",
    "early_redeem",
    "info_nothing_to_reset",
    "retry_pending",
    "final_failure",
    "account_error",
)

#: Prefix a dry-run notice carries (§5), so a test run is unmistakable.
DRY_RUN_PREFIX = {"en": "🧪 [Dry run] ", "pt-BR": "🧪 [Simulação] "}

#: Older/looser kind names the flows may still pass; mapped onto a §5 kind.
#:
#: ``scheduled`` maps to ``reminder`` because that is what it says — the rescue
#: time — and §5 keeps exactly that sentence, only without the discovery-time
#: spam. There is deliberately no ``rehearsal`` entry: §6 cut that world, and a
#: dry run is a *prefix* on a real kind, not a kind of its own.
KIND_ALIASES = {
    "info": "info_nothing_to_reset",
    "vanished": "info_nothing_to_reset",
    "already_redeemed": "info_nothing_to_reset",
    "nothing_to_reset": "info_nothing_to_reset",
    "failure": "final_failure",
    "final": "final_failure",
    "missed": "final_failure",
    "retry": "retry_pending",
    "account_sign_in": "account_error",
    "scheduled": "reminder",
}

#: Parameters each kind accepts. Anything else is a programming error, not a
#: notice: the renderer raises instead of leaking a value into a message.
PARAMS: Mapping[str, tuple[str, ...]] = {
    "reminder": ("label", "expires_at", "in_hours", "fire_at"),
    "success": ("label", "redeemed_at", "expires_at"),
    "early_redeem": ("label", "redeemed_at", "expires_at"),
    "info_nothing_to_reset": ("label", "expires_at"),
    "retry_pending": ("label", "expires_at", "retry_at"),
    "final_failure": ("label", "expires_at", "reason"),
    "account_error": ("label", "expires_at"),
}

#: Placeholders a template uses that are **not** caller-supplied parameters:
#: ``{placeholder} -> (source parameter, phrase table)``. The renderer accepts
#: the source parameter and fills the placeholder from the fixed phrase table,
#: so a caller can never put free text into a sentence and a missing source is
#: a generic phrase instead of an empty slot.
DERIVED: Mapping[str, Mapping[str, tuple[str, str]]] = {
    "account_error": {"reason": ("reason_code", "account_error")},
}

#: Fields rendered as a local time.
TIME_KEYS = frozenset({"expires_at", "fire_at", "retry_at", "redeemed_at"})

#: Which time fields are a full date+time stamp, per kind (§5 examples).
STAMP_KEYS: Mapping[str, frozenset[str]] = {
    "reminder": frozenset({"expires_at"}),
    "success": frozenset(),
    "early_redeem": frozenset({"expires_at"}),
    "info_nothing_to_reset": frozenset(),
    "retry_pending": frozenset(),
    "final_failure": frozenset(),
    "account_error": frozenset(),
}

#: Minimal English month names: the notice must not depend on the host locale.
_MONTHS_EN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

#: Templates. Both languages must use the same placeholders (checked by tests).
_TEMPLATES: Mapping[str, Mapping[str, str]] = {
    "reminder": {
        "en": "⏰ Reminder: your Codex reset for account {label} expires {expires_at} "
        "(in {in_hours}) — rescue time: {fire_at}.",
        "pt-BR": "⏰ Lembrete: seu reset do Codex da conta {label} vence {expires_at} "
        "(em {in_hours}) — horário do resgate: {fire_at}.",
    },
    "success": {
        "en": "✅ Rescued! Codex reset for account {label} redeemed at {redeemed_at} "
        "(was expiring {expires_at}).",
        "pt-BR": "✅ Salvo! Reset do Codex da conta {label} resgatado às {redeemed_at} "
        "(vencia às {expires_at}).",
    },
    "early_redeem": {
        "en": "⚡ Early redemption: redeemed the next Codex reset to expire for account "
        "{label} at {redeemed_at} (weekly quota threshold reached; it was expiring {expires_at}).",
        "pt-BR": "⚡ Resgate antecipado: resgatamos o próximo reset a vencer da conta {label} "
        "às {redeemed_at} (limite semanal atingido; vencia {expires_at}).",
    },
    "info_nothing_to_reset": {
        "en": "ℹ️ Codex reset for account {label} (expiring {expires_at}) wasn't needed — "
        "usage windows were already clear.",
        "pt-BR": "ℹ️ O reset do Codex da conta {label} (vencia às {expires_at}) não precisou "
        "ser resgatado — a cota já estava livre.",
    },
    "retry_pending": {
        "en": "⚠️ Rescue retry pending for account {label} (expires {expires_at}) — "
        "temporary error, next try at {retry_at}.",
        "pt-BR": "⚠️ Falha temporária no resgate da conta {label} (vence às {expires_at}) "
        "— nova tentativa às {retry_at}.",
    },
    "final_failure": {
        "en": "❌ Couldn't rescue Codex reset for account {label} (expires {expires_at}): "
        "{reason} — sign in again if needed, or redeem it manually.",
        "pt-BR": "❌ Não deu para resgatar o reset da conta {label} (vence às {expires_at}): "
        "{reason} — refaça login se preciso, ou resgate manualmente.",
    },
    "account_error": {
        "en": "❌ Couldn't rescue Codex reset for account {label} (expires {expires_at}): "
        "{reason}.",
        "pt-BR": "❌ Não deu para resgatar o reset da conta {label} (vence às {expires_at}): "
        "{reason}.",
    },
}

#: Fixed failure reasons. A code, never provider text (I8). One entry per
#: line pair, so no line grows past the linter's limit.
_REASONS: Mapping[str, Mapping[str, str]] = {
    "auth_expired": {"en": "sign-in expired", "pt-BR": "login expirado"},
    "network": {"en": "network error", "pt-BR": "erro de rede"},
    "timeout": {"en": "timeout", "pt-BR": "tempo esgotado"},
    "http_5xx": {"en": "OpenAI is unavailable", "pt-BR": "OpenAI indisponível"},
    "http_4xx": {"en": "OpenAI refused the request", "pt-BR": "a OpenAI recusou o pedido"},
    "unconfirmed": {
        "en": "could not confirm the result",
        "pt-BR": "não deu para confirmar o resultado",
    },
    "lock_timeout": {
        "en": "another run was still busy",
        "pt-BR": "outra execução ainda estava ocupada",
    },
    "state_unreadable": {
        "en": "local state is unreadable",
        "pt-BR": "o estado local está ilegível",
    },
    "schema_newer": {
        "en": "local state was written by a newer version — update the plugin",
        "pt-BR": "o estado local foi gravado por uma versão mais nova — atualize o plugin",
    },
    "launcher_not_found": {
        "en": "the hermes command was not found",
        "pt-BR": "o comando hermes não foi encontrado",
    },
    "cron_failed": {"en": "the scheduler refused the job", "pt-BR": "o agendador recusou o job"},
    "cron_job_not_live": {
        "en": "the scheduled scan job is gone or finished",
        "pt-BR": "o job de varredura sumiu ou terminou",
    },
    "cron_foreign_job": {
        "en": "a job with this plugin's name belongs to someone else",
        "pt-BR": "um job com o nome do plugin pertence a outra pessoa",
    },
    "cron_unavailable": {
        "en": "the scheduler could not be read",
        "pt-BR": "não deu para ler o agendador",
    },
    "send_failed": {
        "en": "the notice could not be delivered",
        "pt-BR": "não deu para entregar o aviso",
    },
    "credit_expired": {"en": "the credit expired first", "pt-BR": "o crédito venceu antes"},
    # Flow codes that used to fall through to "unexpected error".
    "expired_before_redemption": {
        "en": "it expired before it could be rescued",
        "pt-BR": "venceu antes de dar tempo de resgatar",
    },
    "unavailable_unknown": {
        "en": "the backend listed it in an unknown state",
        "pt-BR": "o backend o listou num estado desconhecido",
    },
    "account_missing": {
        "en": "the account is no longer in the pool",
        "pt-BR": "a conta não está mais no pool",
    },
    "already_redeemed": {
        "en": "it had already been redeemed",
        "pt-BR": "ele já tinha sido resgatado",
    },
    "vanished": {"en": "the backend no longer lists it", "pt-BR": "o backend não o lista mais"},
    "nothing_to_reset": {
        "en": "the usage windows were already clear",
        "pt-BR": "as janelas de uso já estavam livres",
    },
    "unknown": {"en": "unexpected error", "pt-BR": "erro inesperado"},
}

#: The account-error sentence, by category. Short, friendly, and honest about
#: what the user can do: only an auth failure asks for a new sign-in.
_ACCOUNT_ERRORS: Mapping[str, Mapping[str, str]] = {
    "auth": {
        "en": "sign-in expired — sign in again or redeem it manually",
        "pt-BR": "login expirado — refaça login ou resgate manualmente",
    },
    "network": {
        "en": "couldn't reach the account — it will retry on the next scan",
        "pt-BR": "não deu para falar com a conta — nova tentativa na próxima varredura",
    },
    "rate_limited": {
        "en": "the backend is rate-limiting requests — it will retry on the next scan",
        "pt-BR": "o backend está limitando os pedidos — nova tentativa na próxima varredura",
    },
    "unknown": {
        "en": "unexpected error — it will retry on the next scan",
        "pt-BR": "erro inesperado — nova tentativa na próxima varredura",
    },
}

#: Error code → account-error category. Codes come from ``accounts`` and
#: ``codex_api`` (``auth_expired``, ``network``, ``timeout``, ``transient``,
#: ``http_error``, ``forbidden`` …); an unmapped code is ``unknown``.
_ACCOUNT_ERROR_CATEGORY: Mapping[str, str] = {
    "auth_expired": "auth",
    "provider_not_signed_in": "auth",
    "provider_permission": "auth",
    "forbidden": "auth",
    "network": "network",
    "timeout": "network",
    "http_error": "network",
    "transient": "rate_limited",
    "rate_limited": "rate_limited",
    "rate_limit_exceeded": "rate_limited",
}

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


class NoticeError(ValueError):
    """A notice was requested with an unknown kind or an undeclared parameter."""


# --------------------------------------------------------------------- times


@dataclass(frozen=True)
class TimeRenderer:
    """Render epoch seconds in the user's zone (D-011)."""

    tz: Optional[tzinfo] = None

    def _local(self, epoch: float) -> datetime:
        return datetime.fromtimestamp(float(epoch), tz=self.tz)

    def time(self, epoch: float, language: str = DEFAULT_LANGUAGE) -> str:
        """``21:40`` — for a moment the sentence already dates."""
        return self._local(epoch).strftime("%H:%M")

    def stamp(self, epoch: float, language: str = DEFAULT_LANGUAGE) -> str:
        """``Nov 9, 21:40`` (en) / ``09/11 às 21:40`` (pt-BR).

        Always a full date: §5's reminder example reads "expires Nov 9, 21:40"
        even when the notice goes out on the 9th, and a bare time would be
        ambiguous for the 24 h reminder.
        """
        moment = self._local(epoch)
        clock = moment.strftime("%H:%M")
        if language == "pt-BR":
            return f"{moment.day:02d}/{moment.month:02d} às {clock}"
        return f"{_MONTHS_EN[moment.month - 1]} {moment.day}, {clock}"


def local_zone() -> Optional[tzinfo]:
    """Hermes' configured zone, else the host's (D-011).

    Resolution mirrors Hermes' own clock (``HERMES_TIMEZONE``, then ``timezone``
    in the profile's ``config.yaml``, else server local) but reads it through
    the **published** plugin API only — ``hermes_cli.config`` and
    ``agent.secret_scope`` are in ``data/plugin_api.json``, so no private core
    import is needed (catalog rule 9).

    ``HERMES_TIMEZONE`` is consulted only when the process is not multiplexing
    profiles: under the multiplexed gateway that variable holds the *default*
    profile's zone, and every routed profile must read its own config instead
    (the same trap ``hermes_time._env_timezone`` documents).
    """
    name = ""
    try:
        from agent.secret_scope import is_multiplex_active

        multiplexed = bool(is_multiplex_active())
    except Exception:
        multiplexed = False
    if not multiplexed:
        name = str(os.environ.get("HERMES_TIMEZONE") or "").strip()
    if not name:
        try:
            from hermes_cli.config import load_config_readonly

            config = load_config_readonly() or {}
            candidate = config.get("timezone")
            if isinstance(candidate, str):
                name = candidate.strip()
        except Exception:
            name = ""
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo


def resolve_language(setting: Any = "auto", host_language: Any = None) -> str:
    """``en`` or ``pt-BR`` from the host's display language (S7, §2).

    The ``language`` setting was cut: the plugin follows Hermes. ``pt``,
    ``pt-BR`` and ``pt_BR`` all resolve to the Portuguese bundle; anything else
    (including unset) is English.
    """
    value = str(setting or "auto").strip()
    if value in LANGUAGES:
        return value
    host = str(host_language or "").strip().lower().replace("_", "-")
    if host == "pt" or host.startswith("pt-"):
        return "pt-BR"
    return DEFAULT_LANGUAGE


# ------------------------------------------------------------------ catalog


def template_for(kind: str, language: str = DEFAULT_LANGUAGE) -> str:
    """The raw template for one kind (tests compare placeholders through it)."""
    canonical = KIND_ALIASES.get(kind, kind)
    if canonical not in _TEMPLATES:
        raise NoticeError(f"unknown notice kind: {kind!r}")
    language = language if language in LANGUAGES else DEFAULT_LANGUAGE
    return _TEMPLATES[canonical][language]


def validate_catalog() -> list[str]:
    """Return every catalog problem (empty list = the catalog is consistent)."""
    problems: list[str] = []
    for kind in KINDS:
        entry = _TEMPLATES.get(kind)
        if not entry:
            problems.append(f"{kind}: missing templates")
            continue
        declared = set(PARAMS.get(kind, ()))
        derived = dict(DERIVED.get(kind, {}))
        used: dict[str, set[str]] = {}
        for language in LANGUAGES:
            template = entry.get(language)
            if not template:
                problems.append(f"{kind}/{language}: missing template")
                continue
            if not template.strip():
                problems.append(f"{kind}/{language}: empty template")
            placeholders = set(_PLACEHOLDER.findall(template))
            used[language] = placeholders
            undeclared = placeholders - declared - set(derived)
            if undeclared:
                problems.append(
                    f"{kind}/{language}: undeclared placeholder(s) {sorted(undeclared)}"
                )
        if len(used) == len(LANGUAGES) and len({frozenset(v) for v in used.values()}) > 1:
            problems.append(f"{kind}: placeholders differ between languages: {used}")
        if declared and used:
            unused = (declared | set(derived)) - set().union(*used.values())
            if unused:
                problems.append(f"{kind}: declared but unused {sorted(unused)}")
        unknown_stamp = set(STAMP_KEYS.get(kind, ())) - declared
        if unknown_stamp:
            problems.append(f"{kind}: stamp key(s) not declared {sorted(unknown_stamp)}")
    for alias, target in KIND_ALIASES.items():
        if target not in KINDS:
            problems.append(f"alias {alias!r} points at unknown kind {target!r}")
    return problems


# ------------------------------------------------------------------- render


def account_error_text(code: Any, language: str = DEFAULT_LANGUAGE) -> str:
    """The account-error sentence for an error *code* (auth/network/rate/unknown).

    The code is mapped to one of four categories first, so the sentence never
    claims a sign-in problem for a network failure. An unmapped code is the
    unknown category, never an empty phrase.
    """
    language = language if language in LANGUAGES else DEFAULT_LANGUAGE
    category = _ACCOUNT_ERROR_CATEGORY.get(str(code or "").strip(), "unknown")
    return _ACCOUNT_ERRORS[category][language]


def render(
    kind: str,
    params: Optional[Mapping[str, Any]] = None,
    *,
    language: str = DEFAULT_LANGUAGE,
    tz: Optional[tzinfo] = None,
    dry_run: bool = False,
) -> str:
    """Render one notice.

    ``params`` values for the keys in :data:`TIME_KEYS` are epoch seconds and
    are formatted in ``tz`` (default: Hermes' zone, else the host's). Any other
    value is inserted as text. A parameter the kind does not declare raises
    :class:`NoticeError` — that is the guard against leaking an id or a path
    into a message. ``dry_run`` prepends the §5 marker.

    A placeholder in :data:`DERIVED` is filled from its fixed phrase table, not
    from the caller's text: ``account_error`` takes a ``reason_code`` and the
    sentence is chosen here, so a network error can never read as an expired
    sign-in.
    """
    canonical = KIND_ALIASES.get(kind, kind)
    marked = bool(dry_run)
    if canonical not in _TEMPLATES:
        raise NoticeError(f"unknown notice kind: {kind!r}")
    language = language if language in LANGUAGES else DEFAULT_LANGUAGE
    derived = DERIVED.get(canonical, {})
    declared = set(PARAMS.get(canonical, ()))
    accepted = declared | {source for source, _ in derived.values()}
    supplied = dict(params or {})
    unknown = set(supplied) - accepted
    if unknown:
        raise NoticeError(f"notice {canonical!r} does not accept {sorted(unknown)}")

    renderer = TimeRenderer(tz=tz if tz is not None else local_zone())
    stamps = STAMP_KEYS.get(canonical, frozenset())
    values: dict[str, str] = {}
    for key in declared:
        raw = supplied.get(key)
        if key in TIME_KEYS:
            if raw is None:
                values[key] = ""
            elif key in stamps:
                values[key] = renderer.stamp(float(raw), language)
            else:
                values[key] = renderer.time(float(raw), language)
        elif raw is None:
            values[key] = ""
        else:
            values[key] = str(raw)
    for placeholder, (source, table) in derived.items():
        code = supplied.get(source)
        if table == "account_error":
            values[placeholder] = account_error_text(code, language)
        else:  # pragma: no cover - a new table must add its branch here
            raise NoticeError(f"notice {canonical!r} has an unknown phrase table")

    text = _tidy(_fill(_TEMPLATES[canonical][language], values))
    if marked:
        text = DRY_RUN_PREFIX[language] + text
    return text


def _fill(template: str, values: Mapping[str, str]) -> str:
    def replace_field(match: "re.Match[str]") -> str:
        return values.get(match.group(1), "")

    return _PLACEHOLDER.sub(replace_field, template)


def _tidy(text: str) -> str:
    """Collapse the whitespace an empty field leaves behind."""
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    return text.strip()


# ------------------------------------------------------------------ fragments


def reason_text(code: Any, language: str = DEFAULT_LANGUAGE) -> str:
    """A fixed, friendly phrase for a reason code (never provider text)."""
    language = language if language in LANGUAGES else DEFAULT_LANGUAGE
    entry = _REASONS.get(str(code or "unknown"))
    if entry is None:
        entry = _REASONS["unknown"]
    return entry[language]
