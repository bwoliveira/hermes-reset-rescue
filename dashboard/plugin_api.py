"""Reset Rescue — backend routes for the Desktop popup (A§5).

Hermes imports this file on its own (``hermes_dashboard_plugin_reset-rescue``)
and mounts ``router`` under ``/api/plugins/reset-rescue/``. Host auth, consent
and profile scoping belong to Hermes: **no route, query or body field here is
named ``profile``** (Hermes consumes ``?profile=`` itself).

Rules this module keeps:

* Every response body is a DTO's allow-listed ``to_dict()`` (``reset_rescue.dto``),
  schema-backed settings payload, or a fixed sanitized error with ``error`` and
  ``detail`` (and compatible ``message_key``). Unknown paths return only
  ``{"error": "not_found"}``. Exception/provider text is never returned.
* The blocking service call runs through ``asyncio.to_thread`` on
  :func:`_invoke`, a function defined in this plugin file, so a credential
  helper's caller-frame check sees plugin code (A§5, anatomy note).
  ``to_thread`` and not ``run_in_executor`` on purpose: Hermes scopes a request
  to its profile with a ContextVar (``hermes_constants``' HERMES_HOME override,
  set by ``_plugin_route_secret_scope``), and only ``to_thread`` copies the
  context into the worker. With ``run_in_executor`` the worker read and wrote
  the LAUNCH profile's ``config.yaml`` even for a ``?profile=B`` request.
* A handler answers within the UI's timeout (10 s for reads, 45 s for
  ``/scan`` and ``/redeem``) instead of hanging the request.
* Mutations take a JSON body validated by an explicit schema: an unknown field
  is a 422 and never reaches the service. An empty body counts as ``{}``.

Popup routes (D-012, D-014, D-015, D-025, D-030, D-033):

* ``GET /status`` — ``StatusDTO``, a pure read.
* ``POST /scan`` — ensures the owned jobs (idempotent; removes them when
  ``automatic_rescue`` is off), then scans; returns ``StatusDTO``. Folding
  ``ensure`` in keeps first-open zero-setup without a route of its own.
* ``POST /redeem`` — ``{"credit_key", "confirm": true}``; ``RedeemResultDTO``.
* ``GET /history?limit=1..200`` — ``HistoryDTO``, newest first.
* ``GET /settings`` — every ``config_schema`` key with its control type,
  bounds, choices and current value (D-025). The popup's Settings tab renders
  from this instead of a second copy of the schema in JavaScript.
* ``POST /settings`` — ``{"key", "value"}``; validated by
  ``reset_rescue.settings`` and written with Hermes' own
  ``hermes_cli.plugins_settings.save_plugin_settings`` (the same writer the
  Hermes form uses), then the fresh ``StatusDTO`` (D-025).
* ``POST /settings/account`` and ``/settings/account/reset`` — change or clear
  an account's overrides; fresh settings and status payload (D-030).
* ``POST /settings/reset`` — restore global defaults, preserving destination,
  overrides and exclusions; same payload as ``GET /settings`` (D-033).
* ``POST /settings/account/reset-all`` — clear overrides and exclusions;
  same payload as ``GET /settings`` (D-033).

This file is also the **stable route shim** (D-026/D-033). Hermes mounts a
plugin's router once. Explicit endpoints and the final GET/POST catch-all look
up handlers from this file at request time, reloading when mtime_ns or size
changes. Engine files still reload independently through :func:`_engine`.
Pre-D-033 installs need one initial dashboard restart to mount the catch-all;
future route/handler changes need none.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
import sys
import threading
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    ValidationError,
    field_validator,
)

__all__ = [
    "router",
    "set_service_factory",
    "ROUTES",
    "STATUS_TIMEOUT_S",
    "SCAN_TIMEOUT_S",
    "HISTORY_DEFAULT_LIMIT",
    "engine_version",
]

_log = logging.getLogger("reset_rescue.api")

# Handler declarations are rebuilt on reload; only the stable dispatch router
# constructed at the end is mounted into Hermes.
_handlers_router = APIRouter()

#: The UI's ``timeoutMs`` for reads is 10 000; answer a little before it.
STATUS_TIMEOUT_S = 9.0

#: The UI's ``timeoutMs`` for ``/scan`` and ``/redeem`` is 45 000.
SCAN_TIMEOUT_S = 42.0

#: The popup's History view shows a short list.
HISTORY_DEFAULT_LIMIT = 20

#: (method, path) of every route this module serves. Tests pin it.
ROUTES = (
    ("GET", "/status"),
    ("POST", "/scan"),
    ("POST", "/redeem"),
    ("GET", "/history"),
    ("GET", "/settings"),
    ("POST", "/settings"),
    ("POST", "/settings/account"),
    ("POST", "/settings/account/reset"),
    ("POST", "/settings/reset"),
    ("POST", "/settings/account/reset-all"),
    ("GET", "/{path:path}"),
    ("POST", "/{path:path}"),
)

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_ENGINE = "reset_rescue"

_factory_lock = threading.Lock()
_service_factory: Optional[Callable[[], Any]] = None


# --------------------------------------------------------------------- errors


class ApiError(Exception):
    """A fixed, user-safe error code with its HTTP status."""

    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


_ERROR_DETAILS = {
    "busy": "Another operation is running.",
    "timeout": "The operation timed out.",
    "not_ready": "The plugin is not ready.",
    "invalid_body": "Invalid request body.",
    "invalid_query": "Invalid query parameters.",
    "invalid_setting": "Invalid setting.",
    "settings_read_only": "Settings are read-only.",
    "unknown_account": "Account not found.",
    "internal_error": "The operation failed.",
}


def _error(status: int, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": code,
            "detail": _ERROR_DETAILS.get(code, "The request failed."),
            "message_key": f"errors.{code}",
        },
    )


# --------------------------------------------------------------------- engine

#: Guards the import/reload path (D-026). One lock for the whole plugin: the
#: reload drops every ``reset_rescue.*`` entry, so two concurrent requests must
#: never interleave inside it.
_engine_lock = threading.RLock()

#: ``(mtime_ns, size)`` per engine ``.py`` file at the last import, and the
#: package directory they were taken from.
_engine_signature: Optional[tuple] = None
_engine_root: Optional[Path] = None
_engine_checked_at = float("-inf")
_ENGINE_CHECK_INTERVAL_S = 1.0


def _engine_files(pkg_dir: Path) -> list[Path]:
    """Every ``.py`` file of the engine package, recursively, sorted."""
    return sorted(pkg_dir.rglob("*.py"))


def _signature(pkg_dir: Path) -> tuple:
    """``((relative path, mtime_ns, size), ...)`` for the engine package.

    A file that disappears or appears changes the tuple, so an update that
    removes or adds a module reloads too. ``st_mtime_ns`` is the cheap half;
    ``st_size`` catches a same-second rewrite on a coarse filesystem.
    """
    parts = []
    for path in _engine_files(pkg_dir):
        try:
            stat = path.stat()
        except OSError:
            continue
        parts.append((str(path.relative_to(pkg_dir)), stat.st_mtime_ns, stat.st_size))
    return tuple(parts)


def _drop_engine_modules() -> None:
    """Forget every ``reset_rescue`` module so the next import is a fresh one."""
    for name in [n for n in sys.modules if n == _ENGINE or n.startswith(f"{_ENGINE}.")]:
        del sys.modules[name]


def _import_engine(pkg_dir: Path) -> None:
    """Import the engine package from ``pkg_dir`` (by file location, no sys.path)."""
    spec = importlib.util.spec_from_file_location(
        _ENGINE, pkg_dir / "__init__.py", submodule_search_locations=[str(pkg_dir)]
    )
    if spec is None or spec.loader is None:
        raise ApiError(503, "not_ready")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_ENGINE] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_ENGINE, None)
        raise


def _ensure_engine() -> Path:
    """Import the engine, or re-import it when a file changed (D-026).

    Cheap when nothing changed: the signature is checked at most once per
    second, not once per helper import. A changed tree
    drops the ``reset_rescue.*`` entries and imports again, under
    :data:`_engine_lock`, so a request never sees a half-swapped package.

    A package already imported by the process (a test that imported
    ``reset_rescue.service`` directly, the CLI in the same interpreter) is
    **adopted**, not reloaded: its identity is what every other holder of a
    class object expects, and the first request must never invalidate it.
    """
    global _engine_signature, _engine_root, _engine_checked_at
    pkg_dir = _PLUGIN_ROOT / _ENGINE
    with _engine_lock:
        now = monotonic()
        if _engine_root != pkg_dir:
            _engine_root = pkg_dir
            _engine_signature = None
            _engine_checked_at = float("-inf")
        if (
            _ENGINE in sys.modules
            and _engine_signature is not None
            and now - _engine_checked_at < _ENGINE_CHECK_INTERVAL_S
        ):
            return pkg_dir
        current = _signature(pkg_dir)
        _engine_checked_at = now
        if _ENGINE in sys.modules:
            if _engine_signature is None:
                _engine_signature = current
            elif _engine_signature == current:
                return pkg_dir
            else:
                _drop_engine_modules()
                _import_engine(pkg_dir)
                _engine_signature = current
            return pkg_dir
        _import_engine(pkg_dir)
        _engine_signature = current
        return pkg_dir


def engine_version() -> str:
    """The engine's ``VERSION``, loaded through the hot-reloading loader."""
    _ensure_engine()
    return str(getattr(importlib.import_module(_ENGINE), "VERSION", ""))


def _engine(name: str = _ENGINE):
    """Import an engine module through the shared loader (D-026).

    Hermes loads this file without package context, so the engine is always
    imported from the plugin root by file location (no ``sys.path`` change).
    """
    _ensure_engine()
    return importlib.import_module(name)


def set_service_factory(factory: Optional[Callable[[], Any]]) -> None:
    """Install the callable that builds a ``Service`` per request (tests, or
    the host wiring). ``None`` restores the default lookup."""
    global _service_factory
    with _factory_lock:
        _service_factory = factory


def _default_factory() -> Callable[[], Any]:
    """``reset_rescue.service.build_service()`` once the host wiring lands."""
    service_module = _engine(f"{_ENGINE}.service")
    build = getattr(service_module, "build_service", None)
    if not callable(build):
        raise ApiError(503, "not_ready")
    return build


def _build_service() -> Any:
    with _factory_lock:
        factory = _service_factory
    if factory is None:
        factory = _default_factory()
    service = factory()
    if service is None:
        raise ApiError(503, "not_ready")
    return service


# ------------------------------------------------------------------- dispatch


def _invoke(method: str, kwargs: dict) -> dict:
    """Runs in the executor thread: build a fresh service (fresh settings,
    A§8.7), call one method and serialize its DTO. Plugin code on the stack
    for the credential frame check."""
    service = _build_service()
    if method == "scan":
        _ensure_quietly(service)
    result = getattr(service, method)(**kwargs)
    dto_module = _engine(f"{_ENGINE}.dto")
    if not isinstance(result, dto_module.DTO):
        # Only allow-listed DTOs ever leave; anything else is a bug, not data.
        raise ApiError(500, "internal_error")
    return result.to_dict()


def _ensure_quietly(service: Any) -> None:
    """Arm (or, with automatic rescue off, remove) the owned jobs before a
    scan. A held run lock still answers 409; any other ensure failure shows up
    as the scan status's ``problem_code`` instead of failing the scan."""
    try:
        service.ensure()
    except Exception as exc:  # noqa: BLE001 - only the lock is the caller's concern
        if getattr(exc, "code", None) in ("lock_timeout", "busy"):
            raise
        _log.warning("reset-rescue ensure before scan failed: %s", type(exc).__name__)


async def _call(method: str, timeout_s: float, **kwargs: Any) -> JSONResponse:
    future = asyncio.to_thread(_invoke, method, kwargs)
    try:
        body = await asyncio.wait_for(asyncio.shield(future), timeout=timeout_s)
    except asyncio.TimeoutError:
        # The executor call keeps running and finishes on its own; the
        # request answers now so the popup never hangs.
        return _error(504, "timeout")
    except ApiError as exc:
        return _error(exc.status, exc.code)
    except Exception as exc:  # noqa: BLE001 - never echo provider text
        code = getattr(exc, "code", None)
        if code in ("lock_timeout", "busy"):
            return _error(409, "busy")
        _log.warning("reset-rescue %s failed: %s", method, type(exc).__name__)
        return _error(500, "internal_error")
    return JSONResponse(status_code=200, content=body)


# --------------------------------------------------------------------- bodies


class ResetBody(BaseModel):
    """Reset commands accept only an empty object (D-033)."""

    model_config = ConfigDict(extra="forbid")


class ScanBody(BaseModel):
    """``POST /scan``: an optional one-off dry run; default follows settings."""

    model_config = ConfigDict(extra="forbid")
    dry_run: Optional[StrictBool] = None


class RedeemBody(BaseModel):
    """``POST /redeem``: one credit, and ``confirm`` must be literally ``true``."""

    model_config = ConfigDict(extra="forbid")
    credit_key: StrictStr = Field(min_length=1, max_length=64)
    confirm: StrictBool

    @field_validator("confirm")
    @classmethod
    def _must_be_true(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("confirm must be true")
        return value


class AccountSettingBody(BaseModel):
    """``POST /settings/account``: one account, one override field, its value (D-030).

    ``value: null`` clears the override so the account follows the global setting.
    """

    model_config = ConfigDict(extra="forbid")
    account_key: StrictStr = Field(min_length=1, max_length=64)
    field: StrictStr = Field(min_length=1, max_length=64)
    value: Any


class AccountResetBody(BaseModel):
    """``POST /settings/account/reset``: remove every override of one account (D-030)."""

    model_config = ConfigDict(extra="forbid")
    account_key: StrictStr = Field(min_length=1, max_length=64)


class SettingBody(BaseModel):
    """``POST /settings``: one key and its new value (D-025).

    ``value`` is ``Any`` on purpose: the engine's table decides what a valid
    value is for each key, so a wrong type is its 422, not a pydantic message
    that would echo the submitted value. ``extra="forbid"`` keeps a stray
    ``profile`` (or any other field) out of the request.
    """

    model_config = ConfigDict(extra="forbid")
    key: StrictStr = Field(min_length=1, max_length=64)
    value: Any


async def _body(request: Request, model: type) -> Any:
    """Parse the JSON body against ``model``; an empty body is ``{}``.

    Returns the model, or a 422 ``JSONResponse`` that names the field only by
    its code (never echoes the submitted value)."""
    raw = await request.body()
    try:
        return model.model_validate_json(raw or b"{}")
    except ValidationError:
        return _error(422, "invalid_body")


# --------------------------------------------------------------------- routes


@_handlers_router.get("/status")
async def get_status(request: Request) -> JSONResponse:
    """``StatusDTO``: a pure read of state and settings; never takes the run lock."""
    return await _call("status", STATUS_TIMEOUT_S)


@_handlers_router.post("/scan")
async def post_scan(request: Request) -> JSONResponse:
    """``StatusDTO`` after ensuring the owned jobs and one scan of every account."""
    body = await _body(request, ScanBody)
    if isinstance(body, JSONResponse):
        return body
    return await _call("scan", SCAN_TIMEOUT_S, dry_run=body.dry_run)


@_handlers_router.post("/redeem")
async def post_redeem(request: Request) -> JSONResponse:
    """``RedeemResultDTO`` for one explicit, confirmed redemption (D-015)."""
    body = await _body(request, RedeemBody)
    if isinstance(body, JSONResponse):
        return body
    return await _call("redeem_now", SCAN_TIMEOUT_S, credit_key=body.credit_key, confirm=True)


@_handlers_router.get("/history")
async def get_history(request: Request) -> JSONResponse:
    """``HistoryDTO``: the most recent sanitized actions, newest first (D-014).

    ``limit`` is 1..200 (default 20); anything else is a fixed 422 that does
    not echo the value."""
    limit = request.query_params.get("limit")
    if limit is None:
        count = HISTORY_DEFAULT_LIMIT
    elif limit.isascii() and limit.isdigit() and 1 <= int(limit) <= 200:
        count = int(limit)
    else:
        return _error(422, "invalid_query")
    return await _call("history", STATUS_TIMEOUT_S, limit=count)


# ------------------------------------------------------------------- settings


def _read_account_view() -> dict:
    """``account_overrides`` plus every pool account with its effective values (D-030)."""
    service = _build_service()
    view = service.per_account_view()
    settings_module = _engine(f"{_ENGINE}.settings")
    return {
        "account_overrides": _account_overrides_now(settings_module),
        "accounts": view["accounts"],
        "pool_account_count": view["pool_account_count"],
        "per_account_available": view["per_account_available"],
    }


def _read_settings_fields() -> dict:
    """Every setting with its control and current value (D-025).

    Runs in the executor: it reads ``config.yaml`` through the engine's own
    ``host.read_plugin_settings``, so the popup sees exactly what the engine
    reads (and the active profile's file, through Hermes' resolver).
    """
    settings_module = _engine(f"{_ENGINE}.settings")
    host_module = _engine(f"{_ENGINE}.host")
    reader = getattr(host_module, "read_plugin_settings", None)
    current = reader() if callable(reader) else {}
    body = {"fields": settings_module.settings_fields(current)}
    # What "Restore defaults" writes, so the popup applies it on the click.
    defaults = getattr(settings_module, "reset_defaults", None)
    if callable(defaults):
        body["defaults"] = defaults()
    body.update(_read_account_view())
    return body


def _account_overrides_now(settings_module: Any) -> dict:
    """The stored ``account_overrides`` mapping, cleaned like the engine reads it."""
    host_module = _engine(f"{_ENGINE}.host")
    reader = getattr(host_module, "read_plugin_settings", None)
    current = reader() if callable(reader) else {}
    cleaned, _ok = settings_module.coerce_overrides(current.get("account_overrides") or {})
    return cleaned


_SAVE_LOCKS: dict[str, threading.Lock] = {}
_SAVE_LOCKS_GUARD = threading.Lock()


def _profile_save_lock() -> threading.Lock:
    """One lock per resolved request profile, shared across handler reloads (D-033)."""
    from hermes_constants import get_hermes_home

    key = str(Path(get_hermes_home()).resolve(strict=False))
    with _SAVE_LOCKS_GUARD:
        return _SAVE_LOCKS.setdefault(key, threading.Lock())


def _write_account_override(
    account_key: str, field_name: Any, value: Any, *, clear_all: bool
) -> dict:
    """Merge one override (or clear one / all) for an account and save it (D-030, R6).

    The account must be in the pool: an unknown key is a 404, so the config
    never accumulates keys for accounts that are not there. The label kept
    beside the key is the canonical masked label (D-028 fallback). The status
    read and the write run under the profile save lock. A single-field edit
    sends only its leaf (and a changed masked label) as one deep-merge delta;
    multi-field resets still use ``spend_safe_order``.
    """
    import copy

    from hermes_cli import managed_scope
    from hermes_cli.plugins_settings import save_plugin_setting, save_plugin_settings

    settings_module = _engine(f"{_ENGINE}.settings")
    host_module = _engine(f"{_ENGINE}.host")
    coerced: Any = None
    if clear_all:
        if not settings_module.is_account_key(account_key):
            raise ValueError("unknown account")
    else:
        _name, coerced = settings_module.validate_override(account_key, field_name, value)

    with _profile_save_lock():
        service = _build_service()
        accounts = {
            account["account_key"]: account for account in service.per_account_view()["accounts"]
        }
        if account_key not in accounts:
            raise ApiError(404, "unknown_account")

        # A cleared field is written as null, which the engine reads as "not set"
        # (follows global). Reset nulls every field. Other accounts are untouched.
        before = copy.deepcopy(host_module.read_plugin_settings())
        after = copy.deepcopy(before)
        overrides = copy.deepcopy(before.get("account_overrides") or {})
        entry = dict(overrides.get(account_key) or {})
        if clear_all:
            for name in settings_module.OVERRIDE_FIELDS:
                entry[name] = None
        else:
            entry[field_name] = coerced
        overrides[account_key] = entry
        after["account_overrides"] = overrides

        label = service.canonical_account_label(account_key)
        label_changed = entry.get("label") != label
        entry["label"] = label
        if clear_all:
            # A label carries no spend authority. Skip its YAML round-trip if
            # already canonical; every spend-changing reset leaf stays ordered.
            if label_changed:
                save_plugin_setting(
                    "reset-rescue", ("account_overrides", account_key, "label"), label
                )
            for segments, new_value in settings_module.spend_safe_order(before, after):
                save_plugin_setting("reset-rescue", segments, new_value)
            # Deep-merge writers cannot remove leaves: explicit null = inherit.
            # Ordered writes already clear present fields. Only materialize the
            # whole null entry when at least one field was absent on disk.
            old_entry = (before.get("account_overrides") or {}).get(account_key) or {}
            if any(name not in old_entry for name in settings_module.OVERRIDE_FIELDS):
                save_plugin_setting(
                    "reset-rescue",
                    ("account_overrides", account_key),
                    dict.fromkeys(settings_module.OVERRIDE_FIELDS),
                )
        else:
            delta = {field_name: coerced}
            if label_changed:
                delta["label"] = label
            # The schema writer checks the root mapping. Preserve the old
            # leaf-level managed-key refusal as well before batching the delta.
            for name in delta:
                path = (
                    f"plugins.entries.reset-rescue.settings.account_overrides.{account_key}.{name}"
                )
                if managed_scope.is_key_managed(path):
                    raise PermissionError("administrator-managed account setting")
            save_plugin_settings(
                "reset-rescue", _PLUGIN_ROOT, {"account_overrides": {account_key: delta}}
            )

        # Reuse only this request's collaborators, not its old settings. Building
        # a second host graph rereads the entire YAML several times. The exact
        # written snapshot gives the same effective values, still under the lock.
        service.deps.settings = settings_module.parse_settings(after)
        row = next(
            a for a in service.per_account_view()["accounts"] if a["account_key"] == account_key
        )
        status = service.status()
        dto_module = _engine(f"{_ENGINE}.dto")
        if not isinstance(status, dto_module.DTO):
            raise ApiError(500, "internal_error")
        return {
            "account_key": account_key,
            "overrides": row["overrides"],
            "effective": row["effective"],
            "excluded": row["excluded"],
            "status": status.to_dict(),
        }


def _write_setting(key: str, value: Any) -> dict:
    """Validate one edit, write it with Hermes' own writer, return the status.

    ``hermes_cli.plugins_settings.save_plugin_settings`` is the writer the
    Hermes settings form uses (``hermes_cli/plugins_settings.py:130``), so the
    popup and the form cannot disagree about where a setting lives. It raises
    ``ValueError`` for an unknown key, a wrong type or an out-of-choice enum
    (our own validation catches those first, with the engine's bounds), and
    ``PermissionError`` for a managed install or an administrator-managed key.
    """
    settings_module = _engine(f"{_ENGINE}.settings")
    _name, coerced = settings_module.validate_setting(key, value)

    from hermes_cli.plugins_settings import save_plugin_settings

    with _profile_save_lock():
        save_plugin_settings("reset-rescue", _PLUGIN_ROOT, {key: coerced})

    service = _build_service()
    status = service.status()
    dto_module = _engine(f"{_ENGINE}.dto")
    if not isinstance(status, dto_module.DTO):
        raise ApiError(500, "internal_error")
    return {"key": key, "value": coerced, "status": status.to_dict()}


class _SettingsBusy(Exception):
    """The profile save lock is held by a write that has not finished."""


def _read_settings_fields_locked() -> dict:
    """``_read_settings_fields`` under the profile save lock (D-033 R6b).

    A write that answered 504 keeps running under this lock. A read that did
    not wait for it would return the value from before that write. The wait is
    bounded below ``STATUS_TIMEOUT_S`` so a busy profile answers ``busy``, never
    a stale value.
    """
    lock = _profile_save_lock()
    if not lock.acquire(timeout=STATUS_TIMEOUT_S * 0.8):
        raise _SettingsBusy()
    try:
        return _read_settings_fields()
    finally:
        lock.release()


@_handlers_router.get("/settings")
async def get_settings(request: Request) -> JSONResponse:
    """Every ``config_schema`` key with its control type and current value."""
    try:
        body = await asyncio.wait_for(
            asyncio.shield(asyncio.to_thread(_read_settings_fields_locked)),
            timeout=STATUS_TIMEOUT_S,
        )
    except _SettingsBusy:
        return _error(504, "busy")
    except asyncio.TimeoutError:
        return _error(504, "timeout")
    except ApiError as exc:
        return _error(exc.status, exc.code)
    except Exception as exc:  # noqa: BLE001 - never echo a config path
        _log.warning("reset-rescue settings read failed: %s", type(exc).__name__)
        return _error(500, "internal_error")
    return JSONResponse(status_code=200, content=body)


async def _body_400(request: Request, model: type) -> Any:
    """``_body`` with the per-account routes' 400 for a malformed request body."""
    body = await _body(request, model)
    if isinstance(body, JSONResponse):
        return _error(400, "invalid_body")
    return body


@_handlers_router.post("/settings/account")
async def post_settings_account(request: Request) -> JSONResponse:
    """Set or clear one override field of one account; returns the fresh status (D-030)."""
    body = await _body_400(request, AccountSettingBody)
    if isinstance(body, JSONResponse):
        return body
    return await _account_write(
        lambda: _write_account_override(body.account_key, body.field, body.value, clear_all=False),
        sync_quota=body.field in _QUOTA_SETTINGS,
    )


@_handlers_router.post("/settings/account/reset")
async def post_settings_account_reset(request: Request) -> JSONResponse:
    """Remove the whole override entry of one account (D-030)."""
    body = await _body_400(request, AccountResetBody)
    if isinstance(body, JSONResponse):
        return body
    return await _account_write(
        lambda: _write_account_override(body.account_key, None, None, clear_all=True)
    )


_QUOTA_SETTINGS = frozenset(
    {"early_redeem_enabled", "early_redeem_remaining_pct", "monitored", "excluded_accounts"}
)

# Quota-job syncs scheduled by settings saves. Shared with the reloaded handler
# module (see ``_current_handlers``) so tests and shutdown can wait for them.
_quota_sync_tasks: set = set()


def _sync_quota_once() -> None:
    """Re-arm or disarm the quota job from the settings now on disk (worker thread).

    Holds the profile save lock, so a save still in flight finishes first and
    the sync reads its result. Failures are logged by exception type only.
    """
    with _profile_save_lock():
        sync = getattr(_build_service(), "sync_quota_job", None)
        if callable(sync):
            sync()


async def _sync_quota_in_thread() -> None:
    """Log worker and executor failures without leaking paths or exception text."""
    try:
        await asyncio.to_thread(_sync_quota_once)
    except Exception as exc:  # noqa: BLE001 - includes executor shutdown
        _log.warning("reset-rescue quota job sync failed: %s", type(exc).__name__)


def _schedule_quota_sync() -> None:
    """Sync the quota job after a save, off the response path (D-009).

    Fire-and-forget: ``asyncio.to_thread`` copies the request's context, so the
    profile scope follows the sync. Nothing here can fail or delay the save.
    """
    task = asyncio.get_running_loop().create_task(_sync_quota_in_thread())
    _quota_sync_tasks.add(task)
    task.add_done_callback(_quota_sync_tasks.discard)


async def _write_then_sync(call: Callable[[], Any], *, sync_quota: bool = True) -> Any:
    """Keep the sync after a successful write, even when the HTTP wait times out."""
    result = await asyncio.to_thread(call)
    if sync_quota:
        _schedule_quota_sync()
    return result


async def _account_write(call: Callable[[], Any], *, sync_quota: bool = True) -> JSONResponse:
    future = _write_then_sync(call, sync_quota=sync_quota)
    try:
        result = await asyncio.wait_for(asyncio.shield(future), timeout=STATUS_TIMEOUT_S)
    except asyncio.TimeoutError:
        # The shielded writer schedules its sync only after its write finishes.
        return _error(504, "timeout")
    except ValueError:
        return _error(400, "invalid_setting")
    except PermissionError:
        return _error(403, "settings_read_only")
    except ApiError as exc:
        return _error(exc.status, exc.code)
    except Exception as exc:  # noqa: BLE001 - never echo a config path or a message
        _log.warning("reset-rescue account settings write failed: %s", type(exc).__name__)
        return _error(500, "internal_error")
    return JSONResponse(status_code=200, content=result)


@_handlers_router.post("/settings")
async def post_settings(request: Request) -> JSONResponse:
    """Save one setting immediately; the fresh ``StatusDTO`` rides along (D-025)."""
    body = await _body(request, SettingBody)
    if isinstance(body, JSONResponse):
        return body
    future = _write_then_sync(
        lambda: _write_setting(body.key, body.value), sync_quota=body.key in _QUOTA_SETTINGS
    )
    try:
        result = await asyncio.wait_for(asyncio.shield(future), timeout=STATUS_TIMEOUT_S)
    except asyncio.TimeoutError:
        return _error(504, "timeout")
    except ValueError:
        # The engine's own table refused the value: a fixed code, never the text.
        return _error(422, "invalid_setting")
    except PermissionError:
        return _error(403, "settings_read_only")
    except ApiError as exc:
        return _error(exc.status, exc.code)
    except Exception as exc:  # noqa: BLE001 - never echo a config path or a message
        _log.warning("reset-rescue settings write failed: %s", type(exc).__name__)
        return _error(500, "internal_error")
    return JSONResponse(status_code=200, content=result)


def _reset_settings(*, accounts: bool) -> dict:
    """Write the reset delta in spend-safe order, under the profile save lock (D-033, R6)."""
    # The form's CSV validator accepts only strings for excluded_accounts;
    # this command intentionally persists [] (also accepted by the engine).
    # Every write goes through the Hermes single-key writer (managed-key checks
    # and its cross-process lock), one key per call, in the order that
    # spend_safe_order returns: a reset never exposes a spend state that neither
    # endpoint allows (review r6 P1-03).
    import copy

    from hermes_cli.plugins_settings import save_plugin_setting

    settings_module = _engine(f"{_ENGINE}.settings")
    host_module = _engine(f"{_ENGINE}.host")
    with _profile_save_lock():
        before = copy.deepcopy(host_module.read_plugin_settings())
        after = copy.deepcopy(before)
        if accounts:
            # Explicit nulls clear every override, including orphan accounts.
            # Labels are deliberately absent: no display label can be persisted.
            after["account_overrides"] = {
                key: dict.fromkeys(settings_module.OVERRIDE_FIELDS)
                for key in _account_overrides_now(settings_module)
            }
            after["excluded_accounts"] = []
        else:
            # The same map GET /settings exposes as ``defaults``.
            after.update(settings_module.reset_defaults())
        for segments, value in settings_module.spend_safe_order(before, after):
            save_plugin_setting("reset-rescue", segments, value)
        if accounts:
            # Absent and null overrides mean the same thing (follow global), so the
            # ordered writes skip fields that were already absent. This last write only
            # materializes explicit nulls for every field, as the reset-all contract needs.
            for key in _account_overrides_now(settings_module):
                save_plugin_setting(
                    "reset-rescue",
                    ("account_overrides", key),
                    dict.fromkeys(settings_module.OVERRIDE_FIELDS),
                )
        return _read_settings_fields()


@_handlers_router.post("/settings/reset")
async def post_settings_reset(request: Request) -> JSONResponse:
    """Restore global options, preserving destination and account choices."""
    body = await _body(request, ResetBody)
    if isinstance(body, JSONResponse):
        return body
    return await _account_write(lambda: _reset_settings(accounts=False))


@_handlers_router.post("/settings/account/reset-all")
async def post_settings_account_reset_all(request: Request) -> JSONResponse:
    """Clear all overrides and exclusions; every account follows global again."""
    body = await _body(request, ResetBody)
    if isinstance(body, JSONResponse):
        return body
    return await _account_write(lambda: _reset_settings(accounts=True))


# ---------------------------------------------------------- stable route shim

# Request handlers (including new paths) are resolved from the current source,
# not captured by the router Hermes mounted at startup. The handler declarations
# use an APIRouter only as a registry; the host never mounts that router.
_HANDLERS = {
    (method, route.path): route.endpoint
    for route in _handlers_router.routes
    for method in route.methods
}
_handler_lock = threading.RLock()
_handler_module: Any = None
_handler_stat = Path(__file__).stat()
_handler_signature = (_handler_stat.st_mtime_ns, _handler_stat.st_size)


def _current_handlers() -> Any:
    """Reload this handler module when its source changes, without cached pyc.

    The startup module stays the permanent dispatch shim. Reloaded modules share
    its save/factory locks, so an update cannot split an in-flight transaction
    across two lock dictionaries. Publish only a fully compiled/executed module.
    """
    global _handler_module, _handler_signature
    path = Path(__file__)
    with _handler_lock:
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        if signature != _handler_signature:
            name = f"{__name__}__live_{id(_HANDLERS):x}"
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None:
                raise ApiError(503, "not_ready")
            module = importlib.util.module_from_spec(spec)
            previous = sys.modules.get(name)
            sys.modules[name] = module
            try:
                exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
            except Exception:
                if previous is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous
                raise
            active = _handler_module
            for key in (
                "_factory_lock",
                "_SAVE_LOCKS",
                "_SAVE_LOCKS_GUARD",
                "_engine_lock",
                "_quota_sync_tasks",
            ):
                setattr(module, key, globals()[key])
            module._engine_signature = active._engine_signature if active else _engine_signature
            module._engine_root = active._engine_root if active else _engine_root
            module._engine_checked_at = active._engine_checked_at if active else _engine_checked_at
            _handler_module = module
            _handler_signature = signature
        if _handler_module is not None:
            with _factory_lock:
                _handler_module._service_factory = _service_factory
            return _handler_module._HANDLERS
        return _HANDLERS


async def _dispatch(request: Request, path: str) -> JSONResponse:
    try:
        # File I/O/imports never block the dashboard's event loop. The request's
        # profile ContextVars flow into both this lookup and the handler worker.
        handlers = await asyncio.to_thread(_current_handlers)
        handler = handlers.get((request.method, path))
        if handler is None:
            return JSONResponse(status_code=404, content={"error": "not_found"})
        return await handler(request)
    except Exception as exc:  # noqa: BLE001 - reload/handler errors must be JSON too
        _log.warning("reset-rescue route dispatch failed: %s", type(exc).__name__)
        return _error(500, "internal_error")


def _stable_endpoint(path: str) -> Callable:
    async def endpoint(request: Request) -> JSONResponse:
        return await _dispatch(request, path)

    return endpoint


router = APIRouter()
for _method, _path in ROUTES:
    if _path != "/{path:path}":
        router.add_api_route(_path, _stable_endpoint(_path), methods=[_method])


# Last, and only once: a future plugin update can add routes without changing
# the host's startup route table. Existing explicit routes dispatch identically.
@router.api_route("/{path:path}", methods=["GET", "POST"])
async def hot_dispatch(request: Request, path: str) -> JSONResponse:
    return await _dispatch(request, "/" + path)
