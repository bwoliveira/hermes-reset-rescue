"""The one entry point for the CLI and the HTTP routes (A§4.11, T-23a/T-71).

``Service`` builds nothing and owns no state: it receives a :class:`Deps` whose
collaborators are already wired (real in production, fakes in tests), calls one
flow and returns a DTO. Every method is safe to call from a cron run, a CLI
invocation or an HTTP handler.

Rules that shape this module:

* **A missing collaborator is a ``problem_code``, never an exception.** A
  half-wired process (``doctor``, a fresh install, a lane that has not landed
  yet) must still answer ``status``.
* **Mutations are guarded here, not in the UI.** ``redeem_now`` refuses without
  ``confirm=True`` and refuses in a dry run; ``uninstall`` removes only jobs
  listed in ``owned_jobs`` and only wrappers that prove they are ours (I7/I10).
* **``status`` is banked-only** (D-010): an account with no unsettled credit is
  not in the payload at all, and the compact quota summary (D-017) rides on the
  accounts that are.
* **``build_service()``** is the factory the dashboard route imports; it wires
  the real collaborators lazily so importing this module stays inert (I11).

The flows themselves (scan, fire, quota check) live in L2's
``planner``/``fire``; this module calls them through ``Deps`` and degrades to a
``problem_code`` when they are absent, so L1 can land and be tested before L2
exists.
"""

from __future__ import annotations

import importlib
from dataclasses import replace
from typing import Any, Iterable, Mapping, Optional, Sequence

from . import version as version_module
from .accounts import canonical_label, mask_label
from .classify import AVAILABLE, PENDING
from .clock import Deps
from .cron_gateway import SCAN_JOB_NAME, build_scan_spec
from .dto import (
    AccountDTO,
    AttentionDTO,
    CreditDTO,
    EnsureDTO,
    HistoryDTO,
    HistoryItemDTO,
    JobDTO,
    LastResultDTO,
    QuotaCheckDTO,
    QuotaDTO,
    RedeemResultDTO,
    ScheduleDTO,
    SettingsViewDTO,
    SlotDTO,
    StatusDTO,
    WrapperStateDTO,
    iso_z,
)
from .keys import is_valid_key
from .settings import Settings, parse_settings
from .store import LockTimeout, StateError

__all__ = [
    "Service",
    "PROBLEM_CODES",
    "OWNED_JOB_KINDS",
    "build_service",
    "refuse_reason",
    "resolve_language",
]

#: Fixed problem codes ``/status`` can report (never a sentence).
PROBLEM_CODES = frozenset(
    {
        "state_unreadable",
        "schema_newer",
        "no_account",
        "no_source",
        "cron_unavailable",
        "cron_foreign_job",
        "cron_job_not_live",
        "wrappers_unavailable",
        "store_unavailable",
        "settings_invalid",
        "not_armed",
        "lock_timeout",
        "busy",
        "not_implemented",
    }
)

#: The only job kinds the plugin may ever remove (I10). One vocabulary: the
#: store records the *slot role* as the kind, so the two reminders appear as
#: ``reminder1``/``reminder2`` — the same names the slots table uses (R13).
OWNED_JOB_KINDS = frozenset({"scan", "primary", "backup", "reminder1", "reminder2", "quota"})

#: Reasons ``redeem_now`` refuses with (fixed codes, i18n keys in messages.py).
REFUSE_CONFIRM = "confirm_required"
REFUSE_DRY_RUN = "dry_run"
REFUSE_UNKNOWN_CREDIT = "unknown_credit"
REFUSE_BUSY = "busy"
REFUSE_INVALID_KEY = "invalid_credit_key"

#: Credit statuses that mean "already settled": never re-post.
_SETTLED_STATUSES = frozenset({"redeemed", "already_redeemed", "vanished", "expired"})

#: Credit states that count as a banked reset the popup should show (D-010).
_BANKED_STATES = frozenset({AVAILABLE, PENDING})

#: How long ``/status`` may spend on one account's usage read (D-017). The
#: route's whole budget is 9 s, so this leaves room for several accounts.
USAGE_TIMEOUT_S = 2.0


def _rounded(value: Any) -> Optional[int]:
    """A remaining percent rounded for display, or ``None`` when absent."""
    if value is None:
        return None
    return int(round(max(0.0, min(100.0, float(value)))))


def deliver_target(deps: Any) -> str:
    """The cron delivery target: the configured one, else ``local``.

    One rule for every job the plugin creates (scan, quota, slots): the
    settings' ``notify_target``, which ``parse_settings`` already validated.
    """
    settings = getattr(deps, "settings", None)
    return str(getattr(settings, "notify_target", "") or "local")


def resolve_language(settings: Settings, host_language: str = "") -> str:
    """The bundle the popup and notices use (D-016).

    ``auto`` follows Hermes' ``display.language``; anything that is neither
    ``pt-BR`` nor ``en`` falls back to English, exactly as D-016 says. Pure and
    exported so the CLI, the notifier and the tests agree on one rule.
    """
    chosen = str(getattr(settings, "language", "auto") or "auto")
    if chosen in ("en", "pt-BR"):
        return chosen
    host = str(host_language or "").strip()
    if host.lower().startswith("pt"):
        return "pt-BR"
    return "en"


def refuse_reason(settings: Settings, *, confirm: Any, credit_key: str) -> Optional[str]:
    """Why ``redeem_now`` would refuse, or ``None`` when it may proceed.

    Pure and exported so the HTTP layer can answer 422 without running a flow.
    """
    if not is_valid_key(credit_key):
        return REFUSE_INVALID_KEY
    if confirm is not True:
        # `confirm` must be literally true: a truthy string is not a click.
        return REFUSE_CONFIRM
    if settings.dry_run:
        return REFUSE_DRY_RUN
    return None


def build_service(*, deps: Optional[Deps] = None) -> "Service":
    """The service the dashboard route and the CLI drive (D-014/§5).

    With no ``deps`` the real host wiring is used (``host.build_service``),
    which resolves the profile, the store, the credential pool and the flows.
    Imported lazily and by module name so this module stays inert on import
    (I11) and so a tree without the host lane still answers ``status`` with a
    ``no_source`` problem code instead of failing the route.
    """
    if deps is not None:
        return Service(deps)
    try:
        host = importlib.import_module(f"{__package__}.host")
    except Exception:  # noqa: BLE001 - host wiring not installed yet
        return Service(Deps())
    factory = getattr(host, "build_service", None)
    if not callable(factory):
        return Service(Deps())
    return factory()


def _effective_view(scoped: Any) -> dict:
    """The four overridable values an account is decided with (D-030)."""
    return {
        "auto_redeem_enabled": bool(scoped.auto_redeem_enabled),
        "early_redeem_enabled": bool(scoped.early_redeem_enabled),
        "early_redeem_remaining_pct": int(scoped.early_redeem_remaining_pct),
        "reminders_enabled": bool(scoped.reminders_enabled),
    }


class Service:
    """Stateless facade over one :class:`Deps`."""

    def __init__(self, deps: Deps) -> None:
        self.deps = deps

    # -- settings ----------------------------------------------------------

    def settings(self) -> Settings:
        """Fresh settings for this operation (A§8.7: no cached settings).

        ``Deps.settings`` is typed ``Optional[Settings]``, but a caller may
        hand in the raw mapping (a config read, a test fixture) — reading it
        through ``Any`` keeps that documented path reachable instead of
        letting a type checker call it dead code.
        """
        settings: Any = self.deps.settings
        if isinstance(settings, Settings):
            return settings
        if isinstance(settings, Mapping):
            return parse_settings(settings)
        return parse_settings(None)

    def _store(self) -> Any:
        return getattr(self.deps, "store", None)

    # -- status (read-only) ------------------------------------------------

    def status(self) -> StatusDTO:
        """A pure read of state, settings and the owned-job view (A§5).

        Never takes the run lock and never mutates: the Desktop polls this.
        Only accounts holding a banked reset are listed (D-010); each of those
        carries the compact quota summary when it can be read (D-017).
        """
        settings = self.settings()
        problems: list[str] = []
        store = self._store()
        pool_rows = self._pool_rows(store)
        if pool_rows is not None:
            settings = settings.with_pool(
                (str(row.get("account_key") or ""), str(row.get("label") or ""))
                for row in pool_rows
            )

        source_kind = "pool"
        coverage_key = "coverage.pool"
        try:
            source = getattr(self.deps, "source", None)
            if source is not None:
                source_kind = str(getattr(source, "kind", "pool") or "pool")
                coverage_key = str(source.coverage_note())
        except Exception:  # noqa: BLE001 - status must never raise
            problems.append("no_source")

        accounts = self._status_accounts(store, settings, problems)
        schedule = self._status_schedule(store, settings, problems)
        account_count = self._account_count(store, settings)
        per_account = self._per_account(store, settings)

        attention = AttentionDTO(
            failed=sum(1 for account in accounts if account.last_error_code),
            pending=sum(
                1
                for account in accounts
                for credit in (getattr(account, "credits", None) or [])
                if credit.state in (AVAILABLE, PENDING)
            ),
            next_fire_at=self._next_fire(accounts),
        )
        return StatusDTO(
            version=version_module.VERSION,
            schema_version=version_module.SCHEMA_VERSION,
            source_kind=source_kind,
            coverage_note_key=coverage_key,
            dry_run=bool(settings.dry_run),
            auto_redeem_enabled=bool(settings.auto_redeem_enabled),
            language=self._status_language(settings),
            settings=SettingsViewDTO(
                scan_interval_hours=settings.scan_interval_hours,
                lead_minutes=settings.lead_minutes,
                backup_lead_minutes=settings.effective_backup_lead_minutes,
                notify_target_display=settings.notify_target_display(),
                excluded_accounts_count=len(settings.excluded_accounts),
                mask_labels=bool(settings.mask_labels),
                auto_redeem_enabled=bool(settings.auto_redeem_enabled),
                early_redeem_enabled=bool(settings.early_redeem_enabled),
                early_redeem_remaining_pct=settings.early_redeem_remaining_pct,
            ),
            settings_warnings=list(settings.warnings),
            schedule=schedule,
            accounts=accounts,
            attention=attention,
            account_count=account_count,
            pool_account_count=per_account["pool_account_count"],
            per_account_available=per_account["per_account_available"],
        )

    def _status_language(self, settings: Settings) -> str:
        """The ``language`` setting, unresolved (D-016).

        ``auto`` is sent as ``auto``: only the Desktop knows its own locale, so
        resolving it here would force English on a pt-BR Desktop whenever
        Hermes' ``display.language`` is unset. Notices still resolve ``auto``
        against Hermes through :func:`resolve_language`, because a cron run has
        no Desktop to ask.
        """
        chosen = str(getattr(settings, "language", "auto") or "auto")
        return chosen if chosen in ("auto", "en", "pt-BR") else "auto"

    def _pool_rows(self, store: Any) -> Optional[list]:
        """Every stored pool account row, or ``None`` when the store cannot be read (R4).

        Status must never raise, so an unreadable store answers ``None``.
        """
        if store is None:
            return None
        try:
            return list(store.account_list())
        except Exception:  # noqa: BLE001 - status must never raise
            return None

    def _per_account(self, store: Any, settings: Settings) -> dict:
        """Pool-wide per-account facts for the popup (D-030).

        ``accounts`` in the status is banked-only (D-010); this walks every
        stored pool account, paused ones included, so the Per-account view can
        list them. An unreadable store answers ``None`` and "not available".
        """
        rows = self._pool_rows(store)
        if rows is None:
            return {"pool_account_count": None, "per_account_available": False, "accounts": []}
        accounts = []
        for row in rows:
            key = str(row.get("account_key") or "")
            if not key:
                continue
            label = str(row.get("label") or "")
            overrides = settings.account_override_fields(key, label)
            accounts.append(
                {
                    "account_key": key,
                    "label": self._visible_label(store, key, label, settings),
                    "excluded": settings.excluded_by_list(key, label),
                    "monitored": settings.account_included(key, label),
                    "overrides": overrides,
                    "effective": _effective_view(settings.for_account(key, label)),
                }
            )
        pool = len(accounts)
        available = pool >= 2 or any(item["overrides"] for item in accounts)
        return {
            "pool_account_count": pool,
            "per_account_available": available,
            "accounts": accounts,
        }

    def canonical_account_label(self, account_key: str) -> str:
        """The label an override is saved with: canonical and masked (D-028, R4).

        Read from the stored row, never from the presentation label, so a full
        e-mail shown with masking off is never written to ``config.yaml``.
        """
        store = self._store()
        if store is None:
            return ""
        try:
            rows = list(store.account_list())
        except Exception:  # noqa: BLE001 - an unreadable store saves no label
            return ""
        for row in rows:
            if str(row.get("account_key") or "") == account_key:
                stored = str(row.get("label") or "")
                return canonical_label(mask_label(stored, fallback_key=account_key))
        return ""

    def per_account_view(self) -> dict:
        """The Per-account view for ``GET /settings`` (D-030)."""
        return self._per_account(self._store(), self.settings())

    def _account_count(self, store: Any, settings: Settings) -> Optional[int]:
        """How many accounts the next scan covers (``None`` when unknown).

        ``accounts`` is banked-only (D-010), so the popup cannot count the pool
        itself and its empty state needs this number. Only accounts the scan
        would actually visit count: excluded ones do not.

        The stored rows are the cheap answer once a scan has run. Before the
        first scan there are none, so the live source is asked (a read-only
        pool read, the same one the quota summary already does).

        ``0`` is reserved for a genuinely empty pool — the popup renders it as
        "No Codex account in Hermes". Every case where that sentence would be
        wrong or unproven answers ``None`` instead: an unreadable store or
        pool, and a pool whose every account the user excluded.
        """
        if store is None:
            return None
        try:
            rows = store.account_list()
        except Exception:  # noqa: BLE001 - status must never raise
            return None
        if rows:
            count = self._count_rows(rows, settings)
            return count or None
        source: Any = getattr(self.deps, "source", None)
        lister: Any = getattr(source, "accounts", None)
        if not callable(lister):
            return None
        try:
            live: list[Any] = list(lister())
        except Exception:  # noqa: BLE001 - a dead pool is "unknown", not "none"
            return None
        return self._count_rows(
            (
                {
                    "account_key": getattr(account, "account_key", ""),
                    "label": getattr(account, "label", ""),
                }
                for account in live
            ),
            settings,
        )

    @staticmethod
    def _count_rows(rows: Iterable[Mapping[str, Any]], settings: Settings) -> int:
        count = 0
        for row in rows:
            key = str(row.get("account_key") or "")
            if not key:
                continue
            if settings.account_included(key, str(row.get("label") or "")):
                count += 1
        return count

    def _status_accounts(self, store: Any, settings: Settings, problems: list[str]) -> list:
        rows: Sequence[Mapping[str, Any]] = ()
        if store is not None:
            try:
                rows = store.account_list()
            except Exception:  # noqa: BLE001
                problems.append("state_unreadable")
                rows = ()
        accounts = []
        for row in rows:
            key = str(row.get("account_key") or "")
            if not key:
                continue
            # D-027: every pool account is listed, including one with no reset
            # (its credits list is empty and the popup says "No resets").
            credits = self._status_credits(store, key)
            label = str(row.get("label") or "")
            accounts.append(
                AccountDTO(
                    account_key=key,
                    label=self._visible_label(store, key, label, settings),
                    included=settings.account_included(key, label),
                    excluded=settings.excluded_by_list(key, label),
                    monitored=settings.account_included(key, label),
                    overrides=settings.account_override_fields(key, label),
                    effective=_effective_view(settings.for_account(key, label)),
                    reachable=not bool(row.get("last_error_code")),
                    last_error_code=row.get("last_error_code"),
                    last_checked_at=iso_z(row.get("last_list_ok_at")),
                    quota=self._quota(key, label),
                    credits=credits,
                )
            )
        accounts.sort(key=lambda account: account.account_key)
        return accounts

    def _visible_label(self, store: Any, account_key: str, label: str, settings: Settings) -> str:
        """The label this DTO may send (D-013).

        Masking is a setting and ON by default, so the default path re-masks
        whatever the store holds: the store is *supposed* to keep masked
        labels, but nothing in SQLite enforces it, and a row written before
        masking existed must not reach the popup or the HTTP body verbatim.
        Masking is idempotent (``b***@example.com`` stays as it is), so the
        normal path is unchanged.

        With masking off the full label comes from the live source when it is
        available; otherwise the stored label is what we have and is returned
        unchanged (never a guess).
        """
        if settings.mask_labels:
            return mask_label(label, fallback_key=account_key)
        return self._full_label(store, account_key, label)

    def _full_label(self, store: Any, account_key: str, label: str) -> str:
        """The unmasked label when ``mask_labels`` is off (D-013).

        The store keeps the masked label, so the full one comes from the live
        source when it is available; otherwise the stored label is what we
        have and is returned unchanged (never a guess).
        """
        source = getattr(self.deps, "source", None)
        getter = getattr(source, "full_label", None)
        if callable(getter):
            try:
                full = getter(account_key)
            except Exception:  # noqa: BLE001
                full = ""
            if isinstance(full, str) and full.strip():
                return full.strip()
        return label

    def _quota(self, account_key: str, label: str) -> Optional[QuotaDTO]:
        """The compact quota summary for one banked account (D-017).

        Only accounts already in the payload are queried, so an idle pool costs
        zero HTTP calls. An unreadable summary is simply absent: the popup must
        render without it.
        """
        api = getattr(self.deps, "api", None)
        source = getattr(self.deps, "source", None)
        if api is None or source is None:
            return None
        account = self._account_ref(source, account_key, label)
        if account is None:
            return None
        try:
            summary = api.usage(account, timeout=USAGE_TIMEOUT_S)
        except TypeError:
            try:
                summary = api.usage(account)
            except Exception:  # noqa: BLE001
                return None
        except Exception:  # noqa: BLE001
            return None
        plan = getattr(summary, "plan_type", None)
        windows = {
            window.label: window.remaining_pct for window in getattr(summary, "windows", ()) or ()
        }
        if not windows and not plan:
            return None
        return QuotaDTO(
            plan_type=plan,
            five_hour_remaining_pct=_rounded(windows.get("session")),
            weekly_remaining_pct=_rounded(windows.get("weekly")),
        )

    def _account_ref(self, source: Any, account_key: str, label: str) -> Any:
        """The live ``AccountRef`` for a stored key, reusing the source's list."""
        try:
            refs = list(source.accounts())
        except Exception:  # noqa: BLE001
            return None
        for ref in refs:
            if str(getattr(ref, "account_key", "")) == account_key:
                return ref
        return None

    def _status_credits(self, store: Any, account_key: str) -> list:
        if store is None:
            return []
        try:
            rows = store.credit_list(account_key=account_key)
        except TypeError:  # a store without the kwarg
            rows = [row for row in store.credit_list() if row.get("account_key") == account_key]
        except Exception:  # noqa: BLE001
            return []
        credits = []
        for row in rows:
            key = str(row.get("credit_key") or "")
            if not key:
                continue
            credits.append(
                CreditDTO(
                    credit_key=key,
                    title=str(row.get("title") or ""),
                    reset_type=str(row.get("reset_type") or ""),
                    granted_at=iso_z(row.get("granted_at")),
                    expires_at=iso_z(row.get("expires_at")),
                    state=self._credit_state(row),
                    plan=self._credit_plan(row, store, key),
                    primary=self._slot(store, key, "primary"),
                    backup=self._slot(store, key, "backup"),
                    last_result=self._last_result(row),
                    can_redeem_now=self._can_redeem_now(row),
                )
            )
        # D-010: soonest expiry first.
        credits.sort(key=lambda credit: (credit.expires_at is None, credit.expires_at or ""))
        return credits

    def _credit_state(self, row: Mapping[str, Any]) -> str:
        status = str(row.get("status") or "").lower()
        if status in _SETTLED_STATUSES:
            return "redeemed" if status in ("redeemed", "already_redeemed") else status
        backend = str(row.get("backend_state") or "").lower()
        if backend in ("available", "pending", "redeemed"):
            return backend
        if status == "redeeming":
            return "pending"
        return "unknown"

    def _credit_plan(self, row: Mapping[str, Any], store: Any = None, credit_key: str = "") -> str:
        """``scheduled`` / ``beyond_horizon`` / ``too_late`` / ``none``.

        A live slot is the authority; the credit row's own hint is only a
        fallback, because the schedule is written to ``slots``.
        """
        status = str(row.get("status") or "").lower()
        if status in _SETTLED_STATUSES:
            return "none"
        if store is not None and credit_key:
            for role in ("primary", "backup"):
                slot = self._slot(store, credit_key, role)
                if slot is not None and slot.fire_at:
                    return "scheduled"
        if row.get("plan"):
            return str(row["plan"])
        if row.get("fire_at") is not None or row.get("primary_fire_at") is not None:
            return "scheduled"
        return "none"

    def _slot(self, store: Any, credit_key: str, role: str) -> Optional[SlotDTO]:
        if store is None:
            return None
        getter = getattr(store, "slot_get", None)
        if getter is None:
            return None
        try:
            row = getter(credit_key, role)
        except Exception:  # noqa: BLE001
            return None
        if not row:
            return None
        return SlotDTO(
            role=role,
            fire_at=iso_z(row.get("fire_at")),
            job_state=str(row.get("state") or "planned"),
        )

    def _last_result(self, row: Mapping[str, Any]) -> Optional[LastResultDTO]:
        code = row.get("last_code")
        if not code:
            return None
        return LastResultDTO(
            code=str(code), at=iso_z(row.get("updated_at")), reason_code=row.get("last_reason")
        )

    def _can_redeem_now(self, row: Mapping[str, Any]) -> bool:
        return self._credit_state(row) in (AVAILABLE, PENDING)

    def _next_fire(self, accounts: Iterable[AccountDTO]) -> Optional[str]:
        times = []
        for account in accounts:
            for credit in getattr(account, "credits", None) or []:
                for slot in (credit.primary, credit.backup):
                    if slot is not None and slot.fire_at:
                        times.append(slot.fire_at)
        return min(times) if times else None

    def _status_schedule(self, store: Any, settings: Settings, problems: list[str]) -> ScheduleDTO:
        jobs = self._owned_jobs(problems)
        scan_row = next((job for job in jobs if job.get("kind") == "scan"), None)
        quota_job = next((job for job in jobs if job.get("kind") == "quota"), None)
        wrappers = self._wrapper_states(problems)
        live_scan = self._live_scan_job(scan_row, problems)
        problem_code = problems[0] if problems else None
        # D-004: rescue is always on while the plugin is enabled, so a *live*
        # scan job plus no problem is the whole "armed" rule (R7). A row in
        # ``owned_jobs`` is a memory, not a job: only the scheduler's own
        # listing proves the scan will run again. The internal pause seam still
        # reports not-armed when something pinned it off.
        armed = live_scan is not None and settings.automatic_rescue and problem_code is None
        return ScheduleDTO(
            armed=armed,
            scan_job=JobDTO(
                name=live_scan.name,
                job_id=live_scan.job_id,
                schedule_display=f"every {settings.scan_interval_hours}h",
                enabled=True,
            )
            if live_scan is not None
            else None,
            early_job=JobDTO(
                name=str(quota_job.get("job_name") or ""),
                job_id=str(quota_job.get("job_id") or ""),
                schedule_display="every 15m",
                enabled=True,
            )
            if quota_job
            else None,
            next_scan_at=iso_z(self._meta_float(store, "next_scan_at")),
            last_scan_at=self._last_scan_at(store),
            last_scan_outcome=self._last_scan_outcome(store),
            wrappers=wrappers,
            problem_code=problem_code,
        )

    def _live_scan_job(self, row: Optional[Mapping[str, Any]], problems: list[str]) -> Any:
        """The scan job as the scheduler lists it, or ``None`` when it is not live.

        The listing is the only proof that the scan will run again (R7):

        * the scheduler cannot be read → ``cron_unavailable`` (unknown, never
          armed);
        * a recorded row the listing does not show, or shows finished, is
          ``cron_job_not_live``: the protection the user was promised is gone;
        * a same-name job that is not ours is ``cron_foreign_job`` (R14);
        * **no row and no job** is not a problem: nothing was ever set up, and
          the Desktop's first-open scan is what arms it (D-012).
        """
        cron = getattr(self.deps, "cron", None)
        if cron is None:
            if row is not None:
                problems.append("cron_unavailable")
            return None
        try:
            job = cron.find(SCAN_JOB_NAME)
        except Exception:  # noqa: BLE001 - "cannot tell" is never armed
            problems.append("cron_unavailable")
            return None
        if job is None:
            if row is not None:
                problems.append("cron_job_not_live")
            return None
        try:
            owned = cron.owns(job, self._owned_ids())
        except Exception:  # noqa: BLE001 - an unreadable store proves nothing
            problems.append("state_unreadable")
            return None
        if not owned:
            problems.append("cron_foreign_job")
            return None
        if not job.live:
            problems.append("cron_job_not_live")
            return None
        return job

    def _owned_jobs(self, problems: list[str]) -> list:
        """The jobs this plugin still owns and has not verified removed (R7/R13).

        Only active rows count: a row marked removed is history, and treating
        it as ownership would make ``status`` report an unarmed job as armed and
        ``uninstall`` try to delete something that is already gone.
        """
        store = self._store()
        if store is None:
            return []
        getter = getattr(store, "list_owned_jobs", None)
        if getter is None:
            return []
        try:
            return [dict(row) for row in getter(active_only=True)]
        except TypeError:
            try:
                return [dict(row) for row in getter()]
            except Exception:  # noqa: BLE001
                problems.append("state_unreadable")
                return []
        except Exception:  # noqa: BLE001
            problems.append("state_unreadable")
            return []

    def _wrapper_states(self, problems: list[str]) -> list:
        wrappers = getattr(self.deps, "wrappers", None)
        if wrappers is None:
            return []
        verifier = getattr(wrappers, "states_view", None) or getattr(wrappers, "states", None)
        try:
            states = verifier() if callable(verifier) else verifier
        except Exception:  # noqa: BLE001
            problems.append("wrappers_unavailable")
            return []
        if not isinstance(states, Mapping):
            return []
        return [
            WrapperStateDTO(name=str(name), state=str(state))
            for name, state in sorted(states.items())
        ]

    def _meta_float(self, store: Any, key: str) -> Optional[float]:
        raw = self._meta_str(store, key)
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def _meta_str(self, store: Any, key: str) -> Optional[str]:
        if store is None:
            return None
        getter = getattr(store, "meta_get", None)
        if getter is None:
            return None
        try:
            return getter(key)
        except Exception:  # noqa: BLE001
            return None

    # -- the scan clock (P2) -----------------------------------------------

    def _last_scan_row(self, store: Any) -> Optional[Mapping[str, Any]]:
        """The newest **finished** ``scan`` run, or ``None``.

        The ``meta['last_scan_at']`` key is what a scan writes now, but every
        install that scanned before that writer existed has only the ``runs``
        table — which is exactly the population whose popup said "Not checked
        yet" while the scans were plainly running. Reading the newest finished
        scan is not a second source of truth: it is the same event, recorded
        in the table that always had it.
        """
        if store is None:
            return None
        lister = getattr(store, "list_runs", None)
        if not callable(lister):
            return None
        try:
            rows = lister(limit=50)
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(rows, (list, tuple)):
            return None
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            if str(row.get("kind") or "") != "scan":
                continue
            if row.get("ended_at") is None:
                continue  # still running: not a check yet
            return row
        return None

    def _last_scan_at(self, store: Any) -> Optional[str]:
        recorded = self._meta_float(store, "last_scan_at")
        if recorded is not None:
            return iso_z(recorded)
        row = self._last_scan_row(store)
        if row is None:
            return None
        ended = row.get("ended_at")
        try:
            return iso_z(float(ended)) if ended is not None else None
        except (TypeError, ValueError):
            return None

    def _last_scan_outcome(self, store: Any) -> Optional[str]:
        recorded = self._meta_str(store, "last_scan_outcome")
        if recorded:
            return recorded
        row = self._last_scan_row(store)
        if row is None:
            return None
        outcome = str(row.get("outcome") or "")
        return outcome or None

    # -- ensure ------------------------------------------------------------

    def ensure(self) -> EnsureDTO:
        """Install/verify wrappers and the scan job; idempotent (A§8.1, R7).

        A recorded job is not a live job: the scan job is joined against the
        scheduler's own listing, and a missing, terminal, renamed, retimed or
        re-delivered job is recreated. Only a job that the list shows, armed
        and matching the spec, counts as armed (R7).

        D-004: rescue is always on while the plugin is enabled, so nothing in
        the settings form can disarm it — ``dry_run`` and ``hermes plugins
        disable`` are the pause switches. The internal ``automatic_rescue``
        field stays as the pause seam a test (or a future host surface) can pin;
        :func:`parse_settings` never produces ``False`` for it, so a config file
        cannot turn rescue off by accident.
        """
        settings = self.settings()
        created: list[str] = []
        updated: list[str] = []
        removed: list[str] = []
        problems: list[str] = []

        wrappers = getattr(self.deps, "wrappers", None)
        if wrappers is None:
            problems.append("wrappers_unavailable")
        else:
            try:
                states = wrappers.install()
                for name, state in dict(states).items():
                    if state in ("created", "installed"):
                        created.append(str(name))
                    elif state in ("updated", "ok"):
                        updated.append(str(name))
            except Exception:  # noqa: BLE001
                problems.append("wrappers_unavailable")

        if not settings.automatic_rescue:
            # The internal pause seam (D-004): never produced by parse_settings,
            # but a host surface or a test may pin it to disarm every owned job.
            removed.extend(self._remove_all_owned_jobs(problems))
            return EnsureDTO(
                armed=False, created=created, updated=updated, removed=removed, problems=problems
            )

        cron = getattr(self.deps, "cron", None)
        if cron is None:
            problems.append("cron_unavailable")
            return EnsureDTO(
                armed=False, created=created, updated=updated, removed=removed, problems=problems
            )

        spec = build_scan_spec(
            interval_hours=settings.scan_interval_hours,
            deliver=deliver_target(self.deps),
        )
        owned = self._owned_ids()
        try:
            action, job = cron.ensure(spec, owned=owned)
        except Exception:  # noqa: BLE001 - a cron problem is a code, never a crash
            problems.append("cron_unavailable")
            return EnsureDTO(
                armed=False, created=created, updated=updated, removed=removed, problems=problems
            )
        if action == "foreign":
            # A same-name job that is not ours: never adopted, never deleted
            # (R14). The scan job cannot be armed until the user clears it.
            problems.append("cron_foreign_job")
        elif action == "failed":
            problems.append("cron_unavailable")
        elif job is not None:
            self._record_job(spec.name, "scan", job.job_id)
            if action == "created":
                created.append(spec.name)
            elif action == "recreated":
                updated.append(spec.name)

        armed = not problems and self._has_live_scan_job(spec, cron)
        return EnsureDTO(
            armed=armed, created=created, updated=updated, removed=removed, problems=problems
        )

    def _owned_ids(self) -> set[str]:
        return {
            str(job.get("job_id") or "")
            for job in self._owned_jobs([])
            if str(job.get("job_id") or "")
        }

    def _has_live_scan_job(self, spec: Any, cron: Any) -> bool:
        """True only when the scheduler itself lists our scan job, armed."""
        try:
            job = cron.find(spec.name)
        except Exception:  # noqa: BLE001 - "cannot tell" is not armed
            return False
        if job is None or not cron.owns(job, self._owned_ids()):
            return False
        return job.live

    def _record_job(self, job_name: str, kind: str, job_id: str) -> None:
        store = self._store()
        recorder = getattr(store, "record_job", None) if store is not None else None
        if recorder is None:
            return
        try:
            recorder(job_id, job_name, kind)
        except Exception:  # noqa: BLE001 - recording is best effort
            pass

    def _remove_all_owned_jobs(self, problems: list[str]) -> list[str]:
        """Remove every job in ``owned_jobs`` and nothing else (I10, R13).

        A removal that the scheduler does not confirm keeps its ownership row
        (and therefore the credit and its UUID) and is reported: the next
        uninstall retries it instead of forgetting a job that is still live.
        """
        cron = getattr(self.deps, "cron", None)
        store = self._store()
        removed: list[str] = []
        stuck = False
        for job in self._owned_jobs(problems):
            if str(job.get("kind") or "") not in OWNED_JOB_KINDS:
                continue
            job_id = str(job.get("job_id") or "")
            name = str(job.get("job_name") or job_id)
            if cron is not None and job_id:
                if not self._remove_job(cron, job, problems):
                    stuck = True
                    continue
            removed.append(name)
            marker = getattr(store, "mark_job_removed", None) if store is not None else None
            if marker is not None:
                try:
                    marker(job_id)
                except Exception:  # noqa: BLE001
                    pass
        if stuck and "cron_unavailable" not in problems:
            problems.append("cron_unavailable")
        return [name for name in removed if name]

    def _remove_job(self, cron: Any, job: Mapping[str, Any], problems: list[str]) -> bool:
        """Remove one owned job; ``True`` only when the scheduler confirms it."""
        job_id = str(job.get("job_id") or "")
        if not job_id:
            return False
        try:
            return bool(cron.remove(job_id, owned=self._owned_ids()))
        except Exception:  # noqa: BLE001
            problems.append("cron_unavailable")
            return False

    # -- scan --------------------------------------------------------------

    def scan(self, *, dry_run: Optional[bool] = None) -> StatusDTO:
        """Run the scan flow and return the resulting status (A§8.2).

        The flow itself is L2's ``planner``; when it is absent the service
        reports ``not_implemented`` in the schedule rather than pretending the
        scan ran.
        """
        settings = self.settings()
        planner = getattr(self.deps, "planner", None)
        if planner is None:
            return self._status_with_problem("not_implemented")
        effective_dry = settings.dry_run if dry_run is None else bool(dry_run)
        try:
            result = planner.scan(dry_run=effective_dry)
        except Exception:  # noqa: BLE001 - a scan never crashes its caller
            return self._status_with_problem("state_unreadable")
        code = str(getattr(result, "problem_code", "") or "")
        if code:
            return self._status_with_problem(code)
        if int(getattr(result, "exit_code", 0) or 0) != 0:
            # A scan that could not protect a credit is not a healthy scan: the
            # popup must not say "armed" after the scheduler refused a job (R6).
            return self._status_with_problem("cron_unavailable")
        return self.status()

    def _status_with_problem(self, code: str) -> StatusDTO:
        status = self.status()
        schedule = getattr(status, "schedule", None)
        if schedule is not None:
            schedule.problem_code = code
            schedule.armed = False
        return status

    # -- redeem now --------------------------------------------------------

    def redeem_now(self, credit_key: str, *, confirm: Any) -> RedeemResultDTO:
        """Explicit redemption of one credit (A§8.4, T-23b).

        Refuses without ``confirm is True`` and in a dry run, before touching
        anything. Uses the persisted ``redeem_request_id`` so a later
        primary/backup sees the credit settled (I2).
        """
        settings = self.settings()
        refusal = refuse_reason(settings, confirm=confirm, credit_key=credit_key)
        if refusal == REFUSE_INVALID_KEY:
            return RedeemResultDTO(
                credit_key=str(credit_key),
                outcome="failed",
                reason_code=REFUSE_INVALID_KEY,
                windows_reset=None,
            )
        if refusal == REFUSE_CONFIRM:
            return RedeemResultDTO(
                credit_key=credit_key,
                outcome="failed",
                reason_code=REFUSE_CONFIRM,
                windows_reset=None,
            )
        if refusal == REFUSE_DRY_RUN:
            return RedeemResultDTO(
                credit_key=credit_key,
                outcome="dry_run",
                reason_code=REFUSE_DRY_RUN,
                windows_reset=None,
            )

        store = self._store()
        row = self._credit_row(store, credit_key)
        if row is None:
            return RedeemResultDTO(
                credit_key=credit_key,
                outcome="failed",
                reason_code=REFUSE_UNKNOWN_CREDIT,
                windows_reset=None,
            )

        fire = getattr(self.deps, "fire", None)
        if fire is None:
            return RedeemResultDTO(
                credit_key=credit_key,
                outcome="failed",
                reason_code="not_implemented",
                windows_reset=None,
            )
        try:
            outcome = fire.redeem_one(credit_key, role="manual", confirm=True)
        except Exception:  # noqa: BLE001 - a fixed code, never a traceback
            return RedeemResultDTO(
                credit_key=credit_key,
                outcome="failed",
                reason_code="internal_error",
                windows_reset=None,
            )
        return self._redeem_dto(credit_key, outcome)

    def _redeem_dto(self, credit_key: str, outcome: Any) -> RedeemResultDTO:
        if isinstance(outcome, RedeemResultDTO):
            return outcome
        if isinstance(outcome, Mapping):
            return RedeemResultDTO(
                credit_key=credit_key,
                outcome=str(outcome.get("outcome") or "failed"),
                reason_code=outcome.get("reason_code"),
                windows_reset=outcome.get("windows_reset"),
            )
        return RedeemResultDTO(
            credit_key=credit_key,
            outcome=str(getattr(outcome, "outcome", "failed")),
            reason_code=getattr(outcome, "reason_code", None),
            windows_reset=getattr(outcome, "windows_reset", None),
        )

    def _credit_row(self, store: Any, credit_key: str) -> Optional[Mapping[str, Any]]:
        if store is None:
            return None
        getter = getattr(store, "credit_get", None)
        if getter is None:
            return None
        try:
            return getter(credit_key)
        except Exception:  # noqa: BLE001
            return None

    # -- exclusion ---------------------------------------------------------

    def set_excluded(self, account_key: str, excluded: bool) -> StatusDTO:
        """Add or remove one account from ``excluded_accounts`` (D-012 §2).

        The popup's Include switch was cut; ``excluded_accounts`` is the one
        list, written through the plugin config writer so the next operation
        reads it fresh (A§8.7).
        """
        settings = self.settings()
        current = [entry for entry in settings.excluded_accounts if entry != account_key]
        if excluded and account_key:
            current.append(account_key)
        writer = getattr(self.deps, "config", None)
        if writer is None:
            return self._status_with_problem("settings_invalid")
        try:
            writer.set_setting("excluded_accounts", ",".join(current))
        except Exception:  # noqa: BLE001
            return self._status_with_problem("settings_invalid")
        self.deps.settings = replace(settings, excluded_accounts=tuple(current))
        return self.status()

    # -- history -----------------------------------------------------------

    def history(self, limit: int = 50) -> HistoryDTO:
        """The most recent history rows, newest first (A§5)."""
        # `limit or 50` would turn an explicit 0 into 50; clamp the real value.
        try:
            requested = int(limit)
        except (TypeError, ValueError):
            requested = 50
        bounded = max(1, min(requested, 200))
        store = self._store()
        rows: Iterable[Mapping[str, Any]] = ()
        if store is not None:
            tail = getattr(store, "history_tail", None)
            if tail is not None:
                try:
                    rows = tail(bounded)
                except Exception:  # noqa: BLE001
                    rows = ()
        items = [
            HistoryItemDTO(
                at=iso_z(row.get("at")),
                action=str(row.get("action") or ""),
                credit_key=row.get("credit_key"),
                account_key=row.get("account_key"),
                outcome=str(row.get("outcome") or ""),
                reason_code=row.get("reason_code"),
            )
            for row in rows
        ]
        return HistoryDTO(items=items)

    # -- quota check (D-009, T-71/T-73) -----------------------------------

    def quota_check(self) -> QuotaCheckDTO:
        """Run the conditional early-redemption check once (D-009).

        A one-method passthrough to ``Planner.quota_check`` (L2 owns the flow):
        the service adds no policy of its own, only the DTO shape the CLI and
        the HTTP layer serialize. A missing planner is ``not_implemented``; a
        flow exception is a fixed code, never a traceback in a cron run.
        """
        planner = getattr(self.deps, "planner", None)
        runner = getattr(planner, "quota_check", None)
        if not callable(runner):
            return QuotaCheckDTO(
                exit_code=1,
                problem_code="not_implemented",
                accounts=0,
                cooled=0,
                redeemed=0,
                failed=0,
                results=[],
            )
        try:
            result = runner()
        except Exception:  # noqa: BLE001 - a fixed code, never a traceback
            return QuotaCheckDTO(
                exit_code=1,
                problem_code="internal_error",
                accounts=0,
                cooled=0,
                redeemed=0,
                failed=0,
                results=[],
            )
        return self._quota_check_dto(result)

    def _quota_check_dto(self, result: Any) -> QuotaCheckDTO:
        """Map a ``QuotaCheckResult`` (or a mapping) onto the allow-list."""

        def field(name: str, default: Any = None) -> Any:
            if isinstance(result, Mapping):
                return result.get(name, default)
            return getattr(result, name, default)

        outcomes = []
        for outcome in field("results", ()) or ():
            credit_key = (
                outcome.get("credit_key")
                if isinstance(outcome, Mapping)
                else getattr(outcome, "credit_key", "")
            )
            outcome_name = (
                outcome.get("outcome")
                if isinstance(outcome, Mapping)
                else getattr(outcome, "outcome", "failed")
            )
            reason = (
                outcome.get("reason_code")
                if isinstance(outcome, Mapping)
                else getattr(outcome, "reason", None)
            )
            windows = (
                outcome.get("windows_reset")
                if isinstance(outcome, Mapping)
                else getattr(outcome, "windows_reset", None)
            )
            outcomes.append(
                RedeemResultDTO(
                    credit_key=str(credit_key or ""),
                    outcome=str(outcome_name or "failed"),
                    reason_code=reason,
                    windows_reset=windows,
                )
            )
        return QuotaCheckDTO(
            exit_code=int(field("exit_code", 0) or 0),
            problem_code=str(field("problem_code", "") or ""),
            accounts=int(field("accounts", 0) or 0),
            cooled=int(field("cooled", 0) or 0),
            redeemed=int(field("redeemed", 0) or 0),
            failed=int(field("failed", 0) or 0),
            results=outcomes,
        )

    # -- uninstall ---------------------------------------------------------

    def uninstall(self, *, keep_data: bool = False) -> EnsureDTO:
        """Remove owned jobs and our wrappers; keep data unless asked (A§7.5).

        Runs under the same cross-process ``run.lock`` as scan/fire/redeem-now
        (R12): a live redemption must finish before its state can be erased,
        and a second uninstall waits instead of racing the first. Only jobs
        recorded in ``owned_jobs`` are removed (I10), a removal is only
        believed when the scheduler confirms it, and the store is cleared only
        after every owned job is verifiably gone — a job that survives keeps
        its ownership row, its credit and its idempotency UUID for the retry
        (R13).
        """
        store = self._store()
        locker = getattr(store, "run_lock", None) if store is not None else None
        if not callable(locker):
            return self._uninstall_locked(keep_data=keep_data)
        try:
            with locker():
                return self._uninstall_locked(keep_data=keep_data)
        except LockTimeout:
            return EnsureDTO(
                armed=False, created=[], updated=[], removed=[], problems=["lock_timeout"]
            )
        except StateError:
            return EnsureDTO(
                armed=False, created=[], updated=[], removed=[], problems=["state_unreadable"]
            )

    def _uninstall_locked(self, *, keep_data: bool) -> EnsureDTO:
        problems: list[str] = []
        removed = self._remove_all_owned_jobs(problems)
        wrappers = getattr(self.deps, "wrappers", None)
        if wrappers is not None and not problems:
            # The wrappers are what the surviving jobs would run: deleting them
            # under a job that is still armed would turn a live rescue into a
            # script-not-found at fire time (R13).
            remover = getattr(wrappers, "remove_owned", None)
            if remover is not None:
                try:
                    removed.extend(str(name) for name in (remover() or []))
                except Exception:  # noqa: BLE001
                    problems.append("wrappers_unavailable")
        if not keep_data and not problems:
            store = self._store()
            clearer = getattr(store, "clear_all", None) if store is not None else None
            if callable(clearer):
                try:
                    clearer()
                except Exception:  # noqa: BLE001
                    problems.append("state_unreadable")
        return EnsureDTO(armed=False, created=[], updated=[], removed=removed, problems=problems)
