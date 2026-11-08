"""Composition root and the CLI-facing service adapter (simplification §3, T-21/T-74).

``Service`` (L1) is the facade for status/ensure/scan. The CLI needs verbs that
only exist because of *host* surfaces — ``fire`` (what a cron wrapper calls) and
``quota-check`` (the D-009 trigger) — plus an ``ensure`` that also owns the
quota job. Rather than editing L1's module, those live here, in the lane that
owns the CLI, cron and wrappers: :class:`HostService` adds them and delegates
everything else unchanged.

Everything is resolved per call (``HERMES_HOME``, settings, launcher): one
process serves many profiles, so nothing may be cached (plugin-dev core rule 4).
A missing piece degrades to a ``problem_code``; ``status`` always answers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from . import messages
from .accounts import canonical_label, select_source
from .cli_runner import CliRunner
from .clock import Deps, SystemClock
from .codex_api import CodexResetApi
from .cron_gateway import (
    QUOTA_INTERVAL_MINUTES,
    QUOTA_JOB_NAME,
    CronGateway,
    quota_spec,
)
from .dto import EnsureDTO
from .notify import LOCAL_TARGET, Notifier, resolve_target
from .service import deliver_target
from .settings import Settings, parse_settings

__all__ = [
    "PLUGIN_ID",
    "QUOTA_JOB_NAME",
    "QUOTA_INTERVAL_MINUTES",
    "QUOTA_COOLDOWN_S",
    "HostService",
    "hermes_home",
    "plugin_data_dir",
    "read_plugin_settings",
    "plugin_language",
    "plugin_mask_labels",
    "label_resolver",
    "build_deps",
    "build_service",
    "quota_spec",
]

#: Registry id of this plugin (the settings subtree key).
PLUGIN_ID = "reset-rescue"

#: Per-account cooldown between two early redemptions (§3): guards against a
#: stale ``/wham/usage`` cache triggering a second rescue minutes later.
QUOTA_COOLDOWN_S = 15 * 60.0


def hermes_home() -> Path:
    """The active profile's home, resolved per call (never cached).

    Only Hermes' own resolver is used. There is deliberately **no** fallback
    that expands a home directory from the environment: a plugin that derives
    a home path itself is one typo away from reading another profile's
    credentials (I8), so a missing home is an error the caller reports.
    """
    from hermes_constants import get_hermes_home  # published module

    return Path(get_hermes_home())


def plugin_data_dir() -> Path:
    """``<HERMES_HOME>/plugin-data/reset-rescue`` (created by Hermes' helper)."""
    try:
        from plugins.plugin_storage import plugin_data_dir as storage_dir

        return Path(storage_dir(PLUGIN_ID))
    except Exception:
        path = hermes_home() / "plugin-data" / PLUGIN_ID
        path.mkdir(parents=True, exist_ok=True)
        return path


def read_plugin_settings() -> dict[str, Any]:
    """``plugins.entries.reset-rescue.settings`` as a plain dict (empty on failure).

    Read-only and fail-soft: a broken ``config.yaml`` must not stop ``status``
    from answering, and the settings layer already bounds every value.
    """
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
    except Exception:
        return {}
    plugins = config.get("plugins") if isinstance(config, Mapping) else None
    entries = plugins.get("entries") if isinstance(plugins, Mapping) else None
    entry = entries.get(PLUGIN_ID) if isinstance(entries, Mapping) else None
    if not isinstance(entry, Mapping):
        return {}
    settings = entry.get("settings")
    return dict(settings) if isinstance(settings, Mapping) else {}


def _host_language() -> str:
    """Hermes' own display language (spike S7: ``display.language``)."""
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
    except Exception:
        return ""
    display = config.get("display") if isinstance(config, Mapping) else None
    if isinstance(display, Mapping):
        return str(display.get("language") or "")
    return ""


def plugin_language(setting: Any = None) -> str:
    """The notice language (D-016): the setting, else Hermes' display language.

    ``auto`` follows Hermes (``pt*`` → ``pt-BR``) and falls back to English when
    Hermes' language is neither — which is why the fallback lives in
    :func:`messages.resolve_language` and not here.
    """
    if setting is None:
        setting = read_plugin_settings().get("language", "auto")
    return messages.resolve_language(setting, _host_language())


def _settings() -> Settings:
    return parse_settings(read_plugin_settings())


def _flag(name: str, default: bool) -> bool:
    """A boolean setting read straight from the plugin entry.

    ``mask_labels`` (D-013) is declared by this lane's manifest and consumed
    here; ``Settings`` (L1) does not coerce it yet, so reading the raw entry is
    what keeps the switch honest in the meantime. A non-bool value falls back
    to the default instead of being truthy-coerced from a string.
    """
    value = read_plugin_settings().get(name, default)
    return value if isinstance(value, bool) else default


def plugin_mask_labels(setting: Any = None) -> bool:
    """Whether notices and the popup show the full label (D-013, on by default).

    ``setting`` is the value from the plugin entry when the caller already has
    it; omitted, it is read fresh. Only a real ``bool`` is honoured — a
    hand-edited ``"false"`` string falls back to the default rather than
    reading as truthy, which would silently unmask every notice.
    """
    if setting is None:
        return _flag("mask_labels", True)
    return setting if isinstance(setting, bool) else True


def label_resolver(source: Any) -> Optional[Callable[[str], str]]:
    """The D-013 unmask the notifier calls at send time (R16).

    The outbox stores only the masked label, so the full one has to come from
    the live source when the notice is actually rendered. The resolver takes the
    **masked label** (that is what the row holds) and answers with the full one
    for the account that carries it, or ``""`` when it cannot tell — a failed
    unmask is a masked notice, never a guess at another account.

    ``None`` when the source cannot unmask at all, which keeps the notifier's
    behaviour exactly as it was for a source without ``full_label``.
    """
    getter = getattr(source, "full_label", None)
    if not callable(getter):
        return None
    accounts = getattr(source, "accounts", None)

    def resolve(masked: str) -> str:
        wanted = str(masked or "").strip()
        if not wanted:
            return ""
        try:
            refs = list(accounts()) if callable(accounts) else []
        except Exception:  # noqa: BLE001 - a failed unmask is a masked notice
            return ""
        for ref in refs:
            if canonical_label(str(getattr(ref, "label", "") or "")) != wanted:
                continue
            try:
                full = getter(str(getattr(ref, "account_key", "") or ""))
            except Exception:  # noqa: BLE001
                return ""
            if isinstance(full, str) and full.strip():
                return full.strip()
            return ""
        return ""

    return resolve


def build_deps(
    *, settings: Optional[Settings] = None, cli: Optional[CliRunner] = None, store: Any = None
) -> Deps:
    """Wire every collaborator for one operation.

    ``settings``, ``cli`` and ``store`` are injectable so a test can build the
    real graph without a real profile.
    """
    resolved = settings if settings is not None else _settings()
    home = hermes_home()
    data_dir = plugin_data_dir()

    if store is None:
        from .store import Store

        store = Store(data_dir)

    runner = cli or CliRunner(hermes_home=home, recorded_launcher=_recorded_launcher(store))
    cron = CronGateway(runner)
    target, _explicit = resolve_target(resolved.notify_target)

    deps = Deps(
        cli=runner,
        store=store,
        cron=cron,
        settings=resolved,
        hermes_home=home,
        salt=_salt(store),
    )
    deps.wrappers = _Wrappers(home, runner)
    deps.source = select_source(deps, resolved)
    deps.api = CodexResetApi(deps.source)
    # The notifier is built **after** the source because its unmask (R16) reads
    # the live source at send time: the outbox holds only the masked label.
    deps.notifier = Notifier(
        runner,
        target=target or LOCAL_TARGET,
        language=plugin_language(resolved.language),
        mask_labels=plugin_mask_labels(),
        store=store,
        clock=SystemClock().now,
        resolve_label=label_resolver(deps.source),
    )
    from .fire import Fire
    from .planner import Planner

    deps.planner = Planner(deps, store)
    deps.fire = Fire(deps, store)
    return deps


def build_service(*, deps: Optional[Deps] = None) -> "HostService":
    """The service the CLI drives (``build_service()`` with no arguments)."""
    return HostService(deps if deps is not None else build_deps())


class HostService:
    """``Service`` plus the verbs that need host surfaces (fire, quota-check).

    Every other method is delegated unchanged, so the CLI sees one object and
    the dashboard keeps using ``Service`` directly.
    """

    def __init__(self, deps: Deps) -> None:
        from .service import Service

        self.deps = deps
        self._service = Service(deps)

    # -- delegated ---------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        return getattr(self._service, name)

    # -- ensure (adds the quota job) ---------------------------------------

    def ensure(self) -> EnsureDTO:
        """Install wrappers + the scan job, then arm or disarm the quota job."""
        dto = self._service.ensure()
        self._sync_quota_job(
            created=dto.created, updated=dto.updated, removed=dto.removed, problems=dto.problems
        )
        return dto

    def scan(self, *, dry_run: Optional[bool] = None) -> Any:
        """Run the scan, then reconcile the quota job against what it found.

        The quota job follows the credits (D-009): a scan that discovers the
        first credit while early redemption is on must arm it **now**, not on
        some later ``ensure`` that may never come. The same seam disarms it when
        the setting is turned off or the last credit settles.

        A dry run mutates nothing — no redemption, no job — so it never touches
        the scheduler either.
        """
        status = self._service.scan(dry_run=dry_run)
        settings = self._service.settings()
        effective_dry = settings.dry_run if dry_run is None else bool(dry_run)
        if effective_dry:
            return status
        if getattr(getattr(status, "schedule", None), "problem_code", None):
            # A scan that could not arm its own jobs is not evidence about the
            # quota job: report what happened, do not add a second mutation.
            return status
        created: list[str] = []
        updated: list[str] = []
        removed: list[str] = []
        problems: list[str] = []
        self._sync_quota_job(created=created, updated=updated, removed=removed, problems=problems)
        if problems:
            schedule = getattr(status, "schedule", None)
            if schedule is not None:
                schedule.problem_code = problems[0]
                schedule.armed = False
        return status

    def _sync_quota_job(
        self,
        *,
        created: list[str],
        updated: list[str],
        removed: list[str],
        problems: list[str],
    ) -> None:
        """Arm the quota job only while it can do something (§3).

        Armed while the planner's per-account rule holds (see ``_quota_wanted``);
        removed the moment it stops holding. That keeps a
        zero-work job from running every 15 minutes forever, which is the
        difference between "a feature" and "a background process that costs
        money for nothing".

        The three lists are the caller's result sinks: ``ensure`` folds them
        into its ``EnsureDTO``, ``scan`` into the schedule it returns. One
        implementation, so the two entry points cannot drift.
        """
        cron = getattr(self.deps, "cron", None)
        if cron is None:
            return
        wanted = self._quota_wanted()
        if wanted is None:
            # The rule could not be read (a store error): leave the job as it is.
            return
        owned = self._owned_ids()
        try:
            existing = cron.find(QUOTA_JOB_NAME)
        except Exception:  # noqa: BLE001 - a cron problem never fails ensure
            if "cron_unavailable" not in problems:
                problems.append("cron_unavailable")
            return
        if not wanted:
            if existing is not None and self._owned(existing, owned):
                if cron.remove(existing, owned=owned):
                    self._forget_job(existing.job_id)
                    removed.append(QUOTA_JOB_NAME)
            return
        spec = quota_spec(deliver=deliver_target(self.deps))
        try:
            action, job = cron.ensure(spec, owned=owned)
        except Exception:  # noqa: BLE001
            if "cron_unavailable" not in problems:
                problems.append("cron_unavailable")
            return
        if action in {"created", "recreated"} and job is not None:
            self._record_job(spec.name, "quota", job.job_id)
            (created if action == "created" else updated).append(spec.name)
        elif action == "failed" and "cron_unavailable" not in problems:
            problems.append("cron_unavailable")

    def _owned(self, job: Any, owned: set[str]) -> bool:
        return str(getattr(job, "job_id", "")) in owned

    def _quota_wanted(self) -> Optional[bool]:
        """The planner's per-account arming rule (D-030), or ``None`` if it cannot be read.

        One rule for ``ensure``, ``scan`` and the settings writers: an account
        counts only when it is monitored, not excluded, has early redemption on
        for itself (its override, else the global switch) and holds an unsettled
        credit. A process without a planner has nothing to run.
        """
        planner = getattr(self.deps, "planner", None)
        armed = getattr(planner, "quota_armed", None)
        if not callable(armed):
            return False
        try:
            return bool(armed())
        except Exception:  # noqa: BLE001 - a read error must not arm or disarm the job
            return None

    def sync_quota_job(self) -> None:
        """Arm or disarm the quota job against the current settings, nothing else.

        No scan and no network call: only the cron job list and the job itself.
        The settings writers call this after each save.
        """
        self._sync_quota_job(created=[], updated=[], removed=[], problems=[])

    # -- fire --------------------------------------------------------------

    def fire(self, *, role: str, dry_run: Optional[bool] = None) -> Any:
        """Run one scheduled attempt (§3); the wrapper pins the role (I3).

        Returns an object with ``exit_code``/``stdout``/``stderr`` so the CLI
        can pass the notice text through byte for byte (I6). ``dry_run`` is the
        operation's explicit flag and is handed to the flow, which ORs it with
        the persistent setting (R1): a flag can pause a run, never unpause one.
        """
        fire = getattr(self.deps, "fire", None)
        if fire is None:
            return _RunOutput(
                exit_code=1,
                stderr="reset-rescue: the rescue engine is not available in this process.\n",
            )
        if role == "remind":
            # ``remind`` is GET-only (I5-lite): the flow reads the list, sends
            # the due reminders and never posts.
            runner = getattr(fire, "run_remind", None)
            if not callable(runner):
                return _RunOutput(exit_code=0)
            try:
                result = runner()
            except Exception:  # noqa: BLE001 - a cron run never prints a traceback
                return _RunOutput(exit_code=1, stderr="reset-rescue: the reminder run failed.\n")
            return _RunOutput(exit_code=int(getattr(result, "exit_code", 0)))
        if role not in ("primary", "backup"):
            return _RunOutput(exit_code=2, stderr=f"reset-rescue: unknown role {role!r}.\n")
        try:
            result = fire.run(role, dry_run=bool(dry_run))
        except ValueError:
            return _RunOutput(exit_code=2, stderr=f"reset-rescue: unknown role {role!r}.\n")
        except Exception:  # noqa: BLE001 - a cron run never prints a traceback
            return _RunOutput(exit_code=1, stderr="reset-rescue: the rescue run failed.\n")
        code = int(getattr(result, "exit_code", 1))
        problem = str(getattr(result, "problem_code", "") or "")
        stderr = ""
        if code != 0:
            stderr = f"reset-rescue: {messages.reason_text(problem or 'unknown')}.\n"
        return _RunOutput(exit_code=code, stderr=stderr)

    # -- quota-check (D-009) ----------------------------------------------

    def quota_check(self) -> Any:
        """Run the early-redemption trigger once (§3, D-009, R15).

        Zero HTTP calls when nothing qualifies: the check reads local state
        first and only asks ``/wham/usage`` for accounts that still have a
        banked credit outside its lead window and no recent early redemption.

        The flow lives in ``Planner.quota_check`` (T-73) and is reached through
        ``Service.quota_check`` — the same path the dashboard uses. Round 1
        (R15) found this method looking for a ``fire.run_quota_check`` that
        never existed and answering exit 0, so an opted-in user's cron job did
        nothing; there is now exactly one implementation and one caller.

        A process with no planner at all (``doctor``, a partially wired CLI) has
        nothing to run: that is the one case where "nothing to do" is honest.
        """
        planner = getattr(self.deps, "planner", None)
        if not callable(getattr(planner, "quota_check", None)):
            return _RunOutput(exit_code=0)
        dto = self._service.quota_check()
        code = int(getattr(dto, "exit_code", 1))
        stderr = ""
        if code != 0:
            problem = str(getattr(dto, "problem_code", "") or "unknown")
            stderr = f"reset-rescue: {messages.reason_text(problem)}.\n"
        return _RunOutput(exit_code=code, stderr=stderr)

    # -- internals ---------------------------------------------------------

    def _owned_ids(self) -> set[str]:
        store = getattr(self.deps, "store", None)
        if store is None:
            return set()
        try:
            return {
                str(row.get("job_id"))
                for row in store.list_owned_jobs(active_only=True)
                if row.get("job_id")
            }
        except Exception:  # noqa: BLE001
            return set()

    def _record_job(self, name: str, kind: str, job_id: str) -> None:
        store = getattr(self.deps, "store", None)
        recorder = getattr(store, "record_job", None) if store is not None else None
        if not callable(recorder):
            return
        try:
            recorder(job_id, name, kind)
        except Exception:  # noqa: BLE001
            pass

    def _forget_job(self, job_id: str) -> None:
        store = getattr(self.deps, "store", None)
        forget = getattr(store, "mark_job_removed", None) if store is not None else None
        if not callable(forget):
            return
        try:
            forget(job_id)
        except Exception:  # noqa: BLE001
            pass


class _RunOutput:
    """``exit_code``/``stdout``/``stderr`` — the shape the CLI passes through."""

    __slots__ = ("exit_code", "stdout", "stderr")

    def __init__(self, *, exit_code: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


def _salt(store: Any) -> str:
    """The profile's key salt, created once and kept in ``meta`` (A§4.5)."""
    from .keys import is_valid_salt, new_salt

    try:
        current = store.meta_get("salt")
    except Exception:
        current = None
    if is_valid_salt(current):
        return str(current)
    salt = new_salt()
    try:
        store.meta_set("salt", salt)
    except Exception:
        return ""
    return salt


def _recorded_launcher(store: Any) -> Optional[str]:
    """The launcher recorded at the last successful ``ensure`` (spike S3)."""
    try:
        value = store.meta_get("hermes_launcher")
    except Exception:
        return None
    return str(value) if value else None


class _Wrappers:
    """The ``WrapperProtocol`` shape over :mod:`reset_rescue.wrappers`.

    ``install()`` returns ``{file name: state}`` in the service's vocabulary
    (``created``/``updated``/``ok``/``conflict``/``foreign``): the service
    reports what it did, and a conflict must surface as a problem rather than
    as a success.
    """

    def __init__(self, home: Path, cli: CliRunner) -> None:
        self.home = home
        self.cli = cli

    def _launcher(self) -> str:
        return self.cli.launcher() or "hermes"

    def install(self) -> Mapping[str, str]:
        from . import wrappers

        before = {status.name: status.state for status in wrappers.verify_wrappers(self.home)}
        state = wrappers.install_wrappers(self.home, self._launcher())
        result: dict[str, str] = {}
        for status in state.statuses:
            if status.state in {"conflict", "foreign"}:
                result[status.name] = status.state
            elif before.get(status.name) == "missing":
                result[status.name] = "created"
            elif before.get(status.name) == "stale":
                result[status.name] = "updated"
            else:
                result[status.name] = "ok"
        return result

    def remove_owned(self) -> list[str]:
        from . import wrappers

        return wrappers.remove_owned_wrappers(self.home)

    def states_view(self) -> Mapping[str, str]:
        from . import wrappers

        return {status.name: status.state for status in wrappers.verify_wrappers(self.home)}
