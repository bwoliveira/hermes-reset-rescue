"""Settings: one declarative table, coerced and bounded per operation (T-04/T-71).

Hermes' plugin settings form performs no min/max or integer validation and
renders ``list`` as a JSON textarea, so every value is coerced here. An
out-of-range or wrong-typed value becomes the default plus a warning code; it
never raises and never blocks a run.

**The table is the whole contract.** One :class:`Spec` row per setting; adding,
removing or re-bounding a key is a one-line change and nothing else in this
module needs to move (D-012). ``plugin.yaml``'s ``config_schema`` is generated
from the same rows by :func:`config_schema`, so the form and the engine cannot
drift.

The surface is the simplification pass' nine keys plus the two restored by
D-013/D-016:

``scan_interval_hours``, ``lead_minutes``, ``reminder_1_hours``,
``reminder_2_hours``, ``notify_target``, ``early_redeem_enabled``,
``early_redeem_remaining_pct``, ``excluded_accounts``, ``dry_run``,
``mask_labels``, ``language``.

Values that are no longer settings stay *internal* (never in the form):

* **backup lead** — ``min(4, lead_minutes - 3)`` minutes, so the backup always
  lands at least three minutes after the primary (the 120 s cron one-shot
  grace) and never later than four minutes before expiry.
* **account source** — always the pool, with the active sign-in as the
  automatic fallback when the pool slice is empty (D-001).
* **automatic rescue** — always on while the plugin is enabled (D-004);
  ``dry_run`` and ``hermes plugins disable`` are the pause switches.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Optional, Sequence

from .accounts import canonical_label

__all__ = [
    "Settings",
    "SettingsError",
    "Spec",
    "SETTINGS_TABLE",
    "SETTING_NAMES",
    "DEFAULTS",
    "WARN_CODES",
    "MIN_BACKUP_GAP_MINUTES",
    "MAX_BACKUP_LEAD_MINUTES",
    "MAX_EXCLUDED_ACCOUNTS",
    "DEFAULT_AUTO_REDEEM_ENABLED",
    "backup_lead_for",
    "parse_settings",
    "config_schema",
    "validate_notify_target",
    "mask_notify_target",
    "coerce_int",
    "coerce_bool",
    "coerce_strict_bool",
    "coerce_str",
    "coerce_excluded_accounts",
    "coerce_key_list",
]

#: One-shot grace is a fixed 120 s, so the backup must be >= 3 min after primary.
MIN_BACKUP_GAP_MINUTES = 3

#: The backup never fires later than this many minutes before expiry.
MAX_BACKUP_LEAD_MINUTES = 4

#: ``excluded_accounts`` is a CSV of at most this many entries (D-012 §2).
MAX_EXCLUDED_ACCOUNTS = 50

#: D-023: automatic redemption at expiry is on unless the user turns it off.
#: The switch is symmetric with ``early_redeem_enabled``: it gates the
#: *automatic* consume only, never the manual one and never the reminders.
DEFAULT_AUTO_REDEEM_ENABLED = True

ACCOUNT_SOURCES = ("pool", "active_sign_in")
LANGUAGES = ("auto", "en", "pt-BR")
NOTIFY_PLATFORMS = frozenset(
    {"telegram", "discord", "slack", "signal", "whatsapp", "email", "matrix", "local"}
)

_KEY16 = re.compile(r"^[0-9a-f]{16}$")
_CHAT_ID = re.compile(r"^-?\d{1,32}$")
_THREAD_ID = re.compile(r"^\d{1,32}$")
_EXCLUDED_SPLIT = re.compile(r"[,;\s]+")


class SettingsError(Exception):
    """Only raised by :meth:`Settings.require`, never during normal coercion."""


# --------------------------------------------------------------------- coercers


def coerce_int(value: Any, default: int, bounds: tuple[int, int]) -> tuple[int, bool]:
    """``(value, ok)``; bool is not an int here, floats truncate only if whole."""
    low, high = bounds
    if isinstance(value, bool):
        return default, False
    candidate: Optional[int] = None
    if isinstance(value, int):
        candidate = value
    elif isinstance(value, float):
        if value.is_integer():
            candidate = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[+-]?\d{1,9}", text):
            candidate = int(text)
    if candidate is None or candidate < low or candidate > high:
        return default, False
    return candidate, True


def coerce_bool(value: Any, default: bool) -> tuple[bool, bool]:
    if isinstance(value, bool):
        return value, True
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True, True
        if text in ("false", "0", "no", "off"):
            return False, True
    if isinstance(value, int) and value in (0, 1):
        return bool(value), True
    return default, False


def coerce_strict_bool(value: Any, default: bool) -> tuple[bool, bool]:
    """Only a real ``bool`` counts; anything else is the default (D-013).

    ``mask_labels`` decides whether an email address is printed, so a
    hand-edited ``"false"`` string must never read as truthy and silently
    unmask every notice. The host reads this setting through the same rule
    (``host.plugin_mask_labels``), so the two cannot disagree.
    """
    if isinstance(value, bool):
        return value, True
    return default, False


def coerce_str(value: Any, default: str, allowed: Sequence[str]) -> tuple[str, bool]:
    if isinstance(value, str):
        text = value.strip()
        if text in allowed:
            return text, True
    return default, False


def coerce_key_list(value: Any) -> tuple[tuple[str, ...], bool]:
    """A list of 16-hex account keys; a JSON textarea string is accepted.

    ``None`` and ``""`` mean "every account". Hex is case-insensitive, so an
    upper-case key is normalized rather than rejected. Anything else that is
    not a list of valid keys is dropped wholesale rather than partially
    applied: a half-parsed list would silently rescue the wrong accounts.

    Kept for the internal ``included_accounts`` compatibility field; the
    settings form uses :func:`coerce_excluded_accounts` instead.
    """
    items: Any = value
    if value is None:
        return (), True
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return (), True
        try:
            items = json.loads(text)
        except ValueError:
            return (), False
        if items is None:
            return (), True
    if not isinstance(items, (list, tuple)):
        return (), False
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            return (), False
        key = item.strip().lower()
        if not _KEY16.match(key):
            return (), False
        if key not in out:
            out.append(key)
    return tuple(out), True


def coerce_excluded_accounts(value: Any) -> tuple[tuple[str, ...], bool]:
    """Account keys or masked labels, at most ``MAX_EXCLUDED_ACCOUNTS``.

    Stored settings use a list; legacy comma-separated strings remain readable.
    Keys are lower-cased; labels are kept verbatim. An empty value excludes nothing.
    """
    items: list[str] = []
    if value is None:
        return (), True
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return (), True
        items = [part for part in _EXCLUDED_SPLIT.split(text) if part]
    elif isinstance(value, (list, tuple)):
        for item in value:
            if not isinstance(item, str):
                return (), False
            items.extend(part for part in _EXCLUDED_SPLIT.split(item.strip()) if part)
    else:
        return (), False
    out: list[str] = []
    for item in items:
        token = item.strip().lower() if _KEY16.match(item.strip().lower()) else item.strip()
        if not token or len(token) > 64:
            return (), False
        if token not in out:
            out.append(token)
    if len(out) > MAX_EXCLUDED_ACCOUNTS:
        return (), False
    return tuple(out), True


def validate_notify_target(value: Any) -> tuple[str, bool]:
    """Validate against the ``hermes send`` grammar (A§3, D-008).

    Accepted: ``""`` (automatic home channel), ``local``, ``platform``,
    ``platform:chat_id``, ``platform:chat_id:thread_id``. Anything else is
    invalid and behaves as ``local`` with a warning.
    """
    if not isinstance(value, str):
        return "", False
    text = value.strip()
    if not text:
        return "", True
    if any(char.isspace() for char in text):
        # A space is not part of the `hermes send` grammar; accepting it would
        # produce a target the CLI rejects at send time.
        return "", False
    parts = text.split(":")
    platform = parts[0].strip().lower()
    if platform not in NOTIFY_PLATFORMS:
        return "", False
    if len(parts) == 1:
        return platform, True
    if len(parts) > 3:
        return "", False
    chat = parts[1].strip()
    if platform == "local":
        return "", False
    if not _CHAT_ID.match(chat):
        return "", False
    if len(parts) == 3:
        thread = parts[2].strip()
        if not _THREAD_ID.match(thread):
            return "", False
        return f"{platform}:{chat}:{thread}", True
    return f"{platform}:{chat}", True


def mask_notify_target(target: str) -> str:
    """``telegram:...4588:...131`` — never the full chat id (A§5)."""
    text = (target or "").strip()
    if not text:
        return "local"
    if text == "local":
        return "local"
    parts = text.split(":")
    out = [parts[0]]
    for part in parts[1:]:
        out.append(f"…{part[-4:]}" if len(part) > 4 else "…")
    return ":".join(out)


def backup_lead_for(lead_minutes: int) -> int:
    """The internal backup lead: ``min(4, lead_minutes - 3)`` whole minutes.

    ``lead_minutes`` is bounded to ``5..240``, so the result is always at least
    two minutes and always at least three minutes after the primary.
    """
    return max(1, min(MAX_BACKUP_LEAD_MINUTES, int(lead_minutes) - MIN_BACKUP_GAP_MINUTES))


# ------------------------------------------------------------------- the table


@dataclass(frozen=True)
class Spec:
    """One setting: its name, type, default, bounds and warning code.

    ``kind`` selects the coercer. ``bounds`` is inclusive and only meaningful
    for ``int``. ``choices`` is only meaningful for ``enum``. ``extra`` carries
    the ``config_schema`` presentation fields so the manifest can be generated
    from this table instead of maintained by hand.
    """

    name: str
    kind: str
    default: Any
    warn: str
    bounds: Optional[tuple[int, int]] = None
    choices: Optional[tuple[str, ...]] = None
    label: str = ""
    description: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    def coerce(self, raw: Any) -> tuple[Any, bool]:
        if self.kind == "int":
            return coerce_int(raw, int(self.default), self.bounds or (0, 0))
        if self.kind == "bool":
            return coerce_bool(raw, bool(self.default))
        if self.kind == "strict_bool":
            return coerce_strict_bool(raw, bool(self.default))
        if self.kind == "enum":
            return coerce_str(raw, str(self.default), self.choices or ())
        if self.kind == "key_list":
            return coerce_key_list(raw)
        if self.kind == "excluded":
            return coerce_excluded_accounts(raw)
        if self.kind == "target":
            return validate_notify_target(raw)
        if self.kind == "overrides":
            return coerce_overrides(raw)
        raise AssertionError(f"unknown setting kind {self.kind!r}")  # pragma: no cover


#: The complete settings surface. Order is the form order.
#:
#: D-012 keeps this small on purpose. The simplification pass may drop rows;
#: nothing outside this tuple needs to change when it does.
SETTINGS_TABLE: tuple[Spec, ...] = (
    Spec(
        name="scan_interval_hours",
        kind="int",
        default=12,
        bounds=(1, 24),
        warn="settings_scan_interval_invalid",
        label="Scan interval (hours)",
        description=(
            "How often Hermes checks each account in the pool for reset credits, in "
            "hours. Default 12; allowed 1-24."
        ),
    ),
    Spec(
        name="lead_minutes",
        kind="int",
        default=10,
        bounds=(5, 240),
        warn="settings_lead_invalid",
        label="Rescue time (minutes before expiry)",
        description=(
            "The first redemption runs this many minutes before expiry, with a backup "
            "a few minutes later. Default 10; allowed 5-240."
        ),
    ),
    Spec(
        name="reminder_1_hours",
        kind="int",
        default=24,
        bounds=(0, 168),
        warn="settings_reminder_1_invalid",
        label="First reminder (hours before expiry)",
        description=(
            "Sends the first reminder this many hours before expiry. Default 24; "
            "allowed 0-168; 0 turns it off."
        ),
    ),
    Spec(
        name="reminder_2_hours",
        kind="int",
        default=1,
        bounds=(0, 72),
        warn="settings_reminder_2_invalid",
        label="Second reminder (hours before expiry)",
        description=(
            "Sends the second reminder this many hours before expiry. Default 1; "
            "allowed 0-72; 0 turns it off."
        ),
    ),
    Spec(
        name="notify_target",
        kind="target",
        default="",
        warn="settings_notify_target_invalid",
        label="Notice destination",
        description=(
            "Sends notices to the specified platform, platform:chat or "
            "platform:chat:thread. Default is empty, which uses Hermes' configured "
            "home channel."
        ),
    ),
    Spec(
        name="auto_redeem_enabled",
        kind="bool",
        default=True,
        warn="settings_auto_redeem_invalid",
        label="Redeem automatically before expiry",
        description=(
            "Redeems credits automatically before expiry by default. When off, no "
            "automatic redemption is scheduled; reminders and Redeem now still work."
        ),
    ),
    Spec(
        name="early_redeem_enabled",
        kind="bool",
        default=False,
        warn="settings_early_redeem_invalid",
        label="Redeem early when quota is nearly exhausted",
        description=(
            "When on, redeems the soonest-expiring available credit of an account "
            "once its weekly quota remaining is at or below the threshold. Default "
            "off."
        ),
    ),
    Spec(
        name="early_redeem_remaining_pct",
        kind="int",
        default=0,
        bounds=(0, 100),
        warn="settings_early_redeem_pct_invalid",
        label="Early redemption threshold (percent remaining)",
        description=(
            "Sets the weekly quota percentage at or below which early redemption can "
            "occur. Default 0 means only when nothing is left; allowed 0-100."
        ),
    ),
    Spec(
        name="excluded_accounts",
        kind="excluded",
        default=(),
        warn="settings_excluded_accounts_invalid",
        label="Excluded accounts",
        description=(
            "Accounts to skip, as a list of account keys or masked labels. "
            "Default is empty, which covers every account in the pool."
        ),
    ),
    Spec(
        name="dry_run",
        kind="bool",
        default=False,
        warn="settings_dry_run_invalid",
        label="Dry run",
        description=(
            "Scans and schedules as normal but never redeems; notices say what "
            "would happen. Default off."
        ),
    ),
    Spec(
        name="mask_labels",
        kind="strict_bool",
        default=True,
        warn="settings_mask_labels_invalid",
        label="Mask account labels",
        description=(
            "Masks email names and domains in the popup and notices, for example "
            "example@example.com -> e***@e***.com. On by default; turn off to show "
            "full labels there, while files and logs stay masked."
        ),
    ),
    Spec(
        name="language",
        kind="enum",
        default="auto",
        choices=LANGUAGES,
        warn="settings_language_invalid",
        label="Language",
        description=(
            "Language of the popup and notices: auto, en or pt-BR. Default auto, "
            "which follows Hermes."
        ),
    ),
    Spec(
        name="account_overrides",
        kind="overrides",
        default={},
        warn="settings_account_overrides_invalid",
        label="Per-account settings",
        description=(
            "Per-account changes to the global settings, keyed by account key. Written "
            "from the Per account view; leave it empty to follow the global settings."
        ),
    ),
)

SETTING_NAMES: tuple[str, ...] = tuple(spec.name for spec in SETTINGS_TABLE)
SETTINGS_BY_NAME: Mapping[str, Spec] = {spec.name: spec for spec in SETTINGS_TABLE}
DEFAULTS: Mapping[str, Any] = {spec.name: spec.default for spec in SETTINGS_TABLE}
WARN_CODES: frozenset = frozenset(spec.warn for spec in SETTINGS_TABLE)

#: Names an older build wrote that now map onto a current row. Read-only
#: compatibility: the form never shows them.
ALIASES: Mapping[str, str] = {
    "early_redeem_weekly_remaining_pct": "early_redeem_remaining_pct",
}


def config_schema() -> dict:
    """The ``config_schema`` block of ``plugin.yaml``, generated from the table."""
    schema: dict[str, dict] = {}
    for spec in SETTINGS_TABLE:
        default: Any = spec.default
        if isinstance(default, tuple):
            default = list(default)
        entry: dict[str, Any] = {
            "type": {
                "int": "int",
                "bool": "bool",
                "strict_bool": "bool",
                "enum": "str",
                "key_list": "list",
                "excluded": "list",
                "target": "str",
                "overrides": "dict",
            }[spec.kind],
            "label": spec.label,
            "description": spec.description,
            "default": default,
        }
        if spec.choices:
            entry["choices"] = list(spec.choices)
        entry.update(dict(spec.extra))
        schema[spec.name] = entry
    return schema


#: The keys "Restore defaults" (POST /settings/reset) leaves as they are: the
#: private notification target and the per-account choices (D-033).
RESET_PRESERVED: frozenset[str] = frozenset(
    {"notify_target", "account_overrides", "excluded_accounts"}
)


def reset_defaults() -> dict:
    """``{key: default}`` for every key POST /settings/reset writes.

    The one source for both the reset and the ``defaults`` map of GET
    /settings, so the popup can show the result on the confirming click.
    Static schema defaults only: nothing read from config, nothing private.
    """
    return {
        key: spec["default"] for key, spec in config_schema().items() if key not in RESET_PRESERVED
    }


#: The control each setting kind needs in a form, and the JSON type its value
#: has on the wire (D-025). ``bool`` is a toggle, ``int`` a number field,
#: ``enum`` a select, anything else a text field.
CONTROLS: Mapping[str, str] = {
    "int": "int",
    "bool": "bool",
    "strict_bool": "bool",
    "enum": "enum",
    "key_list": "str",
    "excluded": "str",
    "target": "str",
    "overrides": "json",
}


def settings_fields(current: Optional[Mapping[str, Any]] = None) -> list[dict]:
    """Every setting as a renderable field, in form order (D-025).

    ``current`` is the raw ``plugins.entries.reset-rescue.settings`` mapping;
    a key it does not carry reports its default, exactly as the form does.
    The label and description are the English schema text: the popup's tab
    translates by key, so a locale never depends on this text.
    """
    raw: Mapping[str, Any] = current if isinstance(current, Mapping) else {}
    fields: list[dict] = []
    for spec in SETTINGS_TABLE:
        if spec.kind == "overrides":
            continue  # written per account through /settings/account (D-030)
        value = raw.get(spec.name, spec.default)
        if isinstance(value, tuple) or (spec.kind == "excluded" and isinstance(value, list)):
            value = ",".join(str(item) for item in value)
        field: dict[str, Any] = {
            "key": spec.name,
            "type": CONTROLS[spec.kind],
            "value": value,
            "label": spec.label,
            "description": spec.description,
        }
        if spec.bounds is not None:
            field["min"], field["max"] = spec.bounds
        if spec.choices:
            field["choices"] = list(spec.choices)
        fields.append(field)
    return fields


def validate_setting(key: Any, value: Any) -> tuple[str, Any]:
    """``(key, coerced value)`` for one popup edit, or ``ValueError`` (D-025).

    The popup's Settings tab saves one key at a time, so the route validates
    exactly one value against the same table the engine reads. A value that
    the engine would silently fall back from is refused here instead: the
    user sees "Not saved" rather than a setting that looks applied and is not.

    Booleans are strict (a string ``"false"`` must never read as truthy), and
    an integer must be a real ``int`` inside its bounds — the tab sends a JSON
    number, never a string.
    """
    if not isinstance(key, str) or key not in SETTINGS_BY_NAME:
        raise ValueError("unknown setting")
    spec = SETTINGS_BY_NAME[key]
    if spec.kind == "overrides":
        raise ValueError("account_overrides is written per account")
    if spec.kind in ("bool", "strict_bool"):
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be a boolean")
        return key, value
    if spec.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be an integer")
        low, high = spec.bounds or (0, 0)
        if value < low or value > high:
            raise ValueError(f"{key} must be between {low} and {high}")
        return key, value
    if spec.kind == "enum":
        if not isinstance(value, str) or value not in (spec.choices or ()):
            raise ValueError(f"{key} must be one of the declared choices")
        return key, value
    # str-shaped rows: the engine's own coercer decides, so the route and the
    # engine can never disagree about what a valid target or CSV is.
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    coerced, ok = spec.coerce(value)
    if not ok:
        raise ValueError(f"{key} is not a valid value")
    if spec.kind == "excluded":
        return key, list(coerced)
    return key, str(coerced)


# ------------------------------------------------------- per-account overrides

#: Fields an account may override (D-030). Everything else stays global.
OVERRIDE_BOOL_FIELDS: tuple[str, ...] = (
    "monitored",
    "auto_redeem_enabled",
    "early_redeem_enabled",
    "reminders_enabled",
)
OVERRIDE_FIELDS: tuple[str, ...] = OVERRIDE_BOOL_FIELDS + ("early_redeem_remaining_pct",)


# ------------------------------------------------- reset write order (R6, P1-03)

#: Write groups of :func:`spend_safe_order`, applied in ascending order.
_G_REDUCE_GLOBAL, _G_REDUCE_ACCOUNT, _G_NEUTRAL = 0, 1, 2
_G_EARLY_ACCOUNT, _G_UNPAUSE, _G_INCREASE_GLOBAL = 3, 4, 5
_G_AUTO_ACCOUNT, _G_AUTO_GLOBAL = 6, 7
_MISSING = object()

#: Global keys that change how much a consume may spend.
_SPEND_GLOBALS: tuple[str, ...] = (
    "dry_run",
    "auto_redeem_enabled",
    "early_redeem_enabled",
    "early_redeem_remaining_pct",
    "lead_minutes",
)
#: Per-account override fields that change spend authority (reminders are neutral).
_SPEND_OVERRIDES: tuple[str, ...] = (
    "monitored",
    "auto_redeem_enabled",
    "early_redeem_enabled",
    "early_redeem_remaining_pct",
)
_AUTO_FIELDS = frozenset({"auto_redeem_enabled"})


def _authority(field: str, value: Any) -> int:
    """Spend authority of one value: a higher number may spend more."""
    if field == "dry_run":
        return 0 if value else 1
    return int(value)  # bools and bounded ints


def _direction(field: str, before_value: Any, after_value: Any) -> str:
    """``reduce``, ``neutral``, ``increase`` or ``auto`` (an auto-redeem increase)."""
    old = _authority(field, before_value)
    new = _authority(field, after_value)
    if new < old:
        return "reduce"
    if new > old:
        return "auto" if field in _AUTO_FIELDS else "increase"
    return "neutral"


def _effective(settings: Any, overrides: Mapping[str, Any], account_key: str, field: str) -> Any:
    """What one account uses for ``field``: its override, else the global value.

    ``monitored`` has no global field; without an override the account is
    monitored unless its key is in ``excluded_accounts``. Label-written
    exclusions are not resolved here, so they fall in the un-pause group.
    """
    if field in overrides:
        return overrides[field]
    if field == "monitored":
        return account_key not in settings.excluded_accounts
    return getattr(settings, field)


def spend_safe_order(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> list[tuple[tuple[str, ...], Any]]:
    """Writes that move plugin settings from ``before`` to ``after``, spend-safe ordered.

    Hermes writes one key at a time, so a reset passes through intermediate states.
    Each group is applied after the previous one, in this order:

    0. global reductions (excluded-account additions included);
    1. per-account reductions (``monitored`` off, early fields lowered);
    2. neutral writes (reminders, scan interval, labels);
    3. per-account early increases, before the globals they could otherwise
       inherit, so an account is never briefly switched to a global value that
       is looser than both endpoints;
    4. un-pause (``monitored`` on, exclusions removed);
    5. global increases;
    6. per-account auto-redeem increases;
    7. global auto-redeem increases, always last.

    Each intermediate state is then never looser than both endpoints.

    Returns ``[(segments, value), ...]`` for ``save_plugin_setting``. Each path is
    ``(key,)`` or ``("account_overrides", account_key, field)``.
    """
    old = parse_settings(before)
    new = parse_settings(after)
    old_over, _ = coerce_overrides(before.get("account_overrides"))
    new_over, _ = coerce_overrides(after.get("account_overrides"))
    planned: list[tuple[int, int, tuple[str, ...], Any]] = []

    def plan(group: int, path: tuple[str, ...], value: Any) -> None:
        planned.append((group, len(planned), path, value))

    for key, value in after.items():
        if key in ("account_overrides", "excluded_accounts"):
            continue
        if before.get(key, _MISSING) == value:
            continue
        group = _G_NEUTRAL
        if key in _SPEND_GLOBALS:
            direction = _direction(key, getattr(old, key), getattr(new, key))
            group = {
                "reduce": _G_REDUCE_GLOBAL,
                "neutral": _G_NEUTRAL,
                "increase": _G_INCREASE_GLOBAL,
                "auto": _G_AUTO_GLOBAL,
            }[direction]
        plan(group, (key,), value)

    if "excluded_accounts" in after:
        was = tuple(old.excluded_accounts)
        now = tuple(new.excluded_accounts)
        if set(now) - set(was):
            # Union with the old list: never looser than before, and not looser than after.
            plan(_G_REDUCE_GLOBAL, ("excluded_accounts",), list(dict.fromkeys(was + now)))
        if set(was) - set(now):
            plan(_G_UNPAUSE, ("excluded_accounts",), list(now))
        if (
            set(was) == set(now)
            and before.get("excluded_accounts", _MISSING) != after["excluded_accounts"]
        ):
            plan(_G_NEUTRAL, ("excluded_accounts",), list(now))

    for account in dict.fromkeys([*old_over, *new_over]):
        before_fields = old_over.get(account, {})
        after_fields = new_over.get(account, {})
        for name in OVERRIDE_FIELDS:
            if before_fields.get(name) == after_fields.get(name):
                continue
            group = _G_NEUTRAL
            if name in _SPEND_OVERRIDES:
                direction = _direction(
                    name,
                    _effective(old, before_fields, account, name),
                    _effective(new, after_fields, account, name),
                )
                if name == "monitored":
                    increase_group = _G_UNPAUSE
                else:
                    increase_group = _G_EARLY_ACCOUNT
                group = {
                    "reduce": _G_REDUCE_ACCOUNT,
                    "neutral": _G_NEUTRAL,
                    "increase": increase_group,
                    "auto": _G_AUTO_ACCOUNT,
                }[direction]
            plan(group, ("account_overrides", account, name), after_fields.get(name))

    planned.sort(key=lambda item: item[:2])
    return [(path, value) for _group, _seq, path, value in planned]


def is_account_key(value: Any) -> bool:
    """True for a 16-hex account key (never a label, never PII)."""
    return isinstance(value, str) and bool(_KEY16.match(value))


def _coerce_override_value(name: str, value: Any) -> tuple[Any, bool]:
    if name in OVERRIDE_BOOL_FIELDS:
        return (value, True) if isinstance(value, bool) else (None, False)
    if name == "early_redeem_remaining_pct":
        low, high = SETTINGS_BY_NAME[name].bounds or (0, 0)
        if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
            return value, True
    return None, False


def coerce_overrides(raw: Any) -> tuple[dict, bool]:
    """``account_overrides`` cleaned to ``(entries, ok)``.

    An unknown field, a wrong type, an out-of-bounds value, a malformed key or a
    non-mapping entry is dropped and makes ``ok`` false (one settings warning).
    Unknown but well-formed account keys are kept: they are ignored until the
    account returns to the pool. ``null`` values are skipped (they mean "clear").
    """
    if raw is None:
        return {}, True
    if not isinstance(raw, Mapping):
        return {}, False
    clean: dict[str, dict] = {}
    ok = True
    for key, entry in raw.items():
        if not is_account_key(key) or not isinstance(entry, Mapping):
            ok = False
            continue
        label = entry.get("label", "")
        if not isinstance(label, str):
            ok = False
            continue
        kept: dict[str, Any] = {"label": canonical_label(label)}
        for name, value in entry.items():
            if name == "label":
                continue
            if name not in OVERRIDE_FIELDS:
                ok = False
                continue
            if value is None:
                continue
            clean_value, good = _coerce_override_value(name, value)
            if good:
                kept[name] = clean_value
            else:
                ok = False
        clean[str(key)] = kept
    return clean, ok


def validate_override(key: Any, field_name: Any, value: Any) -> tuple[str, Any]:
    """``(field, coerced value)`` for one per-account edit, or ``ValueError``.

    ``None`` is the clear value. The account key must be well formed; whether
    the account is in the pool is the caller's question, not this function's.
    """
    if not is_account_key(key):
        raise ValueError("unknown account")
    if not isinstance(field_name, str) or field_name not in OVERRIDE_FIELDS:
        raise ValueError("unknown account setting")
    if value is None:
        return field_name, None
    clean, good = _coerce_override_value(field_name, value)
    if not good:
        raise ValueError(f"{field_name} is not a valid value")
    return field_name, clean


# ------------------------------------------------------------------- the value


@dataclass(frozen=True)
class Settings:
    """Coerced, bounded settings for one operation.

    Construct directly with valid values (tests do), or use
    :meth:`from_raw`/:func:`parse_settings` for anything that came from config.

    The fields after ``language`` are **internal**: they are not in the form,
    they exist so the flows and the tests can pin a value, and
    :func:`parse_settings` always derives them from the table.
    """

    scan_interval_hours: int = 12
    lead_minutes: int = 10
    reminder_1_hours: int = 24
    reminder_2_hours: int = 1
    notify_target: str = ""
    auto_redeem_enabled: bool = True
    early_redeem_enabled: bool = False
    early_redeem_remaining_pct: int = 0
    excluded_accounts: tuple[str, ...] = ()
    dry_run: bool = False
    mask_labels: bool = True
    language: str = "auto"
    #: Per-account overrides keyed by account key (D-030); empty follows the globals.
    account_overrides: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    # -- internal (never in the form) --------------------------------------
    #: Per-account override of the reminders (D-030); not a form field.
    reminders_enabled: bool = True
    #: ``None`` means "use the formula"; a test may pin an exact value.
    backup_lead_minutes: Optional[int] = None
    #: Compatibility allow-list used by ``with_included``; empty means all.
    included_accounts: tuple[str, ...] = ()
    #: Always on while the plugin is enabled (D-004); kept pinnable for tests.
    automatic_rescue: bool = True
    #: Always ``pool`` with an automatic fallback (D-001); kept for tests.
    account_source: str = "pool"
    warnings: tuple[str, ...] = field(default=())

    # -- derived -----------------------------------------------------------

    @property
    def lead_seconds(self) -> float:
        return float(self.lead_minutes) * 60.0

    @property
    def effective_backup_lead_minutes(self) -> int:
        """The internal backup lead actually used by the flows."""
        if self.backup_lead_minutes is not None:
            return int(self.backup_lead_minutes)
        return backup_lead_for(self.lead_minutes)

    @property
    def backup_lead_seconds(self) -> float:
        return float(self.effective_backup_lead_minutes) * 60.0

    @property
    def primary_fire_offset_s(self) -> float:
        """Seconds before expiry the primary fires."""
        return self.lead_seconds

    @property
    def backup_fire_offset_s(self) -> float:
        """Seconds before expiry the backup fires."""
        return self.backup_lead_seconds

    @property
    def scan_interval_seconds(self) -> float:
        return float(self.scan_interval_hours) * 3600.0

    @property
    def reminder_offsets_s(self) -> tuple[tuple[str, float], ...]:
        """``(role, seconds before expiry)`` for each enabled reminder (D-007).

        ``0`` disables a reminder; the two are independent, so a user may keep
        only the 1-hour one.
        """
        out: list[tuple[str, float]] = []
        for role, hours in (
            ("reminder1", self.reminder_1_hours),
            ("reminder2", self.reminder_2_hours),
        ):
            if hours > 0:
                out.append((role, float(hours) * 3600.0))
        return tuple(out)

    @property
    def early_redeem_threshold_pct(self) -> Optional[float]:
        """Weekly *remaining* percent at or below which an early redemption runs.

        ``None`` when the feature is off, so a caller cannot accidentally treat
        ``0`` as "always redeem".
        """
        if not self.early_redeem_enabled:
            return None
        return float(self.early_redeem_remaining_pct)

    def account_included(self, account_key: str, label: str = "") -> bool:
        """Whether this account is in scope.

        ``excluded_accounts`` matches an account key or a masked label; the
        compatibility ``included_accounts`` allow-list (set by
        :meth:`with_included`) wins when it is non-empty.
        """
        override = self.account_override_fields(account_key, label).get("monitored")
        if override is not None:
            return bool(override)
        if self.included_accounts:
            return account_key in self.included_accounts
        return not self.excluded_by_list(account_key, label)

    def excluded_by_list(self, account_key: str, label: str = "") -> bool:
        """Whether ``excluded_accounts`` names this account, by key or by label (D-028).

        This is the one matcher: :meth:`account_included` and the per-account
        ``excluded`` flag both use it, so the popup never re-derives it.
        An entry may be the account key, the legacy masked form
        (``b***@gmail.com``), the current one (``b***@g***.com``) or a full
        address; the masked forms and the full address reduce to the same
        canonical label.
        """
        if not self.excluded_accounts:
            return False
        if account_key in self.excluded_accounts:
            return True
        if label:
            wanted = canonical_label(label)
            for entry in self.excluded_accounts:
                if canonical_label(entry) == wanted:
                    return True
        return False

    def override_entry(self, account_key: str, label: str = "") -> Optional[Mapping[str, Any]]:
        """The override entry of one account, matched by account key only (D-030, R4).

        The ``label`` argument is accepted for call-site compatibility and ignored:
        a masked label can collide between two accounts, so it never selects an override.
        The stored ``label`` is a human hint only.
        """
        if account_key and account_key in self.account_overrides:
            return self.account_overrides[account_key]
        return None

    def with_pool(self, rows: Iterable[tuple[str, str]]) -> "Settings":
        """A copy that reports override entries whose key is not in the pool (R4).

        Such an entry is an orphan: it is ignored and the account follows the global
        settings until it is set again. It is reported once as
        ``settings_account_override_orphan``.
        """
        pool_keys = {str(key) for key, _label in rows}
        warnings = list(self.warnings)
        orphan = any(key not in pool_keys for key in self.account_overrides)
        if orphan and "settings_account_override_orphan" not in warnings:
            warnings.append("settings_account_override_orphan")
        return replace(self, warnings=tuple(warnings))

    def account_override_fields(self, account_key: str, label: str = "") -> dict:
        """Only the overridable fields this account sets (never the label)."""
        entry = self.override_entry(account_key, label) or {}
        return {name: entry[name] for name in OVERRIDE_FIELDS if entry.get(name) is not None}

    def for_account(self, account_key: str, label: str = "") -> "Settings":
        """A copy holding this account's effective values (D-030).

        ``reminders_enabled: false`` zeroes both reminder hours in the copy, so
        the planner drops that account's reminder one-shots like any disabled
        reminder. Every per-account decision reads this copy.
        """
        overrides = self.account_override_fields(account_key, label)
        if not overrides:
            return self
        changes: dict[str, Any] = {}
        for name in (
            "auto_redeem_enabled",
            "early_redeem_enabled",
            "early_redeem_remaining_pct",
            "reminders_enabled",
        ):
            if name in overrides:
                changes[name] = overrides[name]
        if overrides.get("reminders_enabled") is False:
            changes["reminder_1_hours"] = 0
            changes["reminder_2_hours"] = 0
        return replace(self, **changes) if changes else self

    def with_included(self, keys: Iterable[str]) -> "Settings":
        """A copy that only covers ``keys`` (compatibility helper for tests)."""
        cleaned = tuple(dict.fromkeys(str(key).strip().lower() for key in keys))
        return replace(self, included_accounts=cleaned)

    def with_excluded(self, entries: Iterable[str]) -> "Settings":
        cleaned = tuple(dict.fromkeys(str(entry).strip() for entry in entries))
        return replace(self, excluded_accounts=cleaned)

    def notify_target_display(self) -> str:
        return mask_notify_target(self.notify_target)

    # -- construction ------------------------------------------------------

    @classmethod
    def from_raw(cls, raw: Optional[Mapping[str, Any]]) -> "Settings":
        return parse_settings(raw)

    def require(self) -> "Settings":
        """Return self; kept so a caller can assert a settings object is usable."""
        return self


def parse_settings(raw: Optional[Mapping[str, Any]]) -> Settings:
    """Coerce every row of :data:`SETTINGS_TABLE`, one warning per bad value."""
    data: dict[str, Any] = dict(raw) if isinstance(raw, Mapping) else {}
    for legacy, current in ALIASES.items():
        if legacy in data and current not in data:
            data[current] = data[legacy]

    warnings: list[str] = []
    values: dict[str, Any] = {}
    for spec in SETTINGS_TABLE:
        present = spec.name in data
        value, ok = spec.coerce(data[spec.name] if present else spec.default)
        if not ok:
            warnings.append(spec.warn)
        values[spec.name] = value

    # A reminder that fires after the redemption would be noise: reset it to
    # its default instead of failing the whole settings object.
    lead_hours = values["lead_minutes"] / 60.0
    for name in ("reminder_1_hours", "reminder_2_hours"):
        if values[name] > 0 and values[name] < lead_hours:
            values[name] = int(DEFAULTS[name])
            warnings.append("settings_reminder_before_lead")

    values["backup_lead_minutes"] = backup_lead_for(values["lead_minutes"])
    values["warnings"] = tuple(warnings)
    return Settings(**values)
