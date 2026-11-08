"""Runtime primitives shared by every module of the engine.

This is the lowest layer of the package: the ``Clock``, the ``CliResult`` value
object, the collaborator protocols and the ``Deps`` injection container. Every
other module may import it without creating a cycle (the architecture's
dependency direction puts ``clock`` at the bottom, A§4).

The protocols are ``typing.Protocol`` definitions, not ABCs: a fake does not
have to inherit from anything and a real implementation in another lane
satisfies them structurally. ``AccountSource``/``AccountRef``/``Response`` are
defined in ``accounts.py`` (the module that also implements the pool source)
and re-exported here, so a lane can import every contract from one place.

Nothing here touches the network, the filesystem or ``~/.hermes``.
"""

from __future__ import annotations

import datetime
import time
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Iterable,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    runtime_checkable,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from pathlib import Path
    from zoneinfo import ZoneInfo

    from .settings import Settings

__all__ = [
    "Clock",
    "SystemClock",
    "CliResult",
    "Paths",
    "Deps",
    "TransportProtocol",
    "CronGatewayProtocol",
    "WrapperProtocol",
    "NotifierProtocol",
    "StoreProtocol",
    "hermes_timezone",
    "hermes_timezone_name",
    "host_timezone",
    "display_timezone",
    "to_display",
    "format_when",
]


# --------------------------------------------------------------------- clock


@runtime_checkable
class Clock(Protocol):
    """Wall clock plus sleep, injected so tests never wait."""

    def now(self) -> float:
        """Epoch seconds (UTC)."""
        ...

    def sleep(self, seconds: float) -> None:
        """Block for ``seconds``."""
        ...


class SystemClock:
    """The real clock."""

    __slots__ = ()

    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "SystemClock()"


# ------------------------------------------------------------------ time zone


def hermes_timezone() -> Optional["ZoneInfo"]:
    """Hermes' configured ``ZoneInfo`` for this profile, or ``None``.

    D-011: messages are rendered in Hermes' configured zone, else the host's.
    Hermes already resolves exactly that (``HERMES_TIMEZONE``, then
    ``timezone`` in the profile's ``config.yaml``, else server-local) and
    caches it per profile identity, so this is a thin, read-only call into
    ``hermes_time.get_timezone`` (``/usr/local/lib/hermes-agent/hermes_time.py``
    lines 130-133, resolution at 83-127). Imported lazily: the engine must stay
    importable outside the Hermes runtime (pure unit tests, tooling).
    """
    try:
        from hermes_time import get_timezone  # noqa: PLC0415 - optional dependency
    except Exception:  # noqa: BLE001 - not running under Hermes: host zone wins
        return None
    try:
        return get_timezone()
    except Exception:  # noqa: BLE001 - a broken config never breaks a notice
        return None


def hermes_timezone_name() -> str:
    """The configured IANA name (``""`` when the host zone applies)."""
    try:
        from hermes_time import get_timezone_name  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return ""
    try:
        return str(get_timezone_name() or "")
    except Exception:  # noqa: BLE001
        return ""


def host_timezone() -> datetime.tzinfo:
    """The host's own zone, as a fixed-offset-aware ``tzinfo`` for right now."""
    return datetime.datetime.now().astimezone().tzinfo or datetime.timezone.utc


def display_timezone() -> datetime.tzinfo:
    """Hermes' configured zone when it has one, else the host's (D-011)."""
    return hermes_timezone() or host_timezone()


def to_display(epoch: Optional[float]) -> Optional[datetime.datetime]:
    """Epoch seconds → an aware datetime in the display zone, or ``None``."""
    if epoch is None:
        return None
    return datetime.datetime.fromtimestamp(float(epoch), datetime.timezone.utc).astimezone(
        display_timezone()
    )


def format_when(epoch: Optional[float], language: str = "en") -> str:
    """``09/11 18:30`` (pt-BR) / ``Nov 9, 18:30`` (en), in the display zone.

    D-011: the popup formats in the Desktop's own local zone from the ISO UTC
    value it receives; this helper is for the messages the engine renders.
    """
    moment = to_display(epoch)
    if moment is None:
        return ""
    if str(language).lower().startswith("pt"):
        return moment.strftime("%d/%m %H:%M")
    return f"{moment.strftime('%b')} {moment.day}, {moment.strftime('%H:%M')}"


# ------------------------------------------------------------------ cli run


@dataclass(frozen=True)
class CliResult:
    """Outcome of one ``hermes`` invocation.

    ``exec_error`` is a fixed category (``spawn_failed``), never a captured
    message: a child's stderr can carry a path or a token fragment and must
    never reach a log, a DTO or a notice (I8).
    """

    argv: tuple = ()
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    exec_error: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.exec_error


@dataclass(frozen=True)
class Paths:
    """Every file Reset Rescue owns, resolved for one profile at one moment.

    Never cached across operations: one process serves many profiles
    (architecture A§4, plugin-dev core rule 4).
    """

    data_dir: "Path"
    db_path: "Path"
    lock_path: "Path"


# ------------------------------------------------------------------ protocols


@runtime_checkable
class TransportProtocol(Protocol):
    """The HTTP surface ``codex_api`` needs from an account source (A§4.3)."""

    def request(
        self,
        account: Any,
        method: str,
        url: str,
        *,
        json: object | None = None,
        timeout: float = 30.0,
    ) -> Any: ...


@runtime_checkable
class CronGatewayProtocol(Protocol):
    """Owned cron jobs (A§7.3-7.4). Implemented by ``cron_gateway.py`` (L3).

    One dialect, the one the gateway actually implements (R18): a spec in and a
    job out. There is no argv-level ``create_job``/``remove_job`` here — that
    pair described an API the gateway abandoned, so a caller reading this
    protocol would have written against a contract nothing satisfies.
    """

    def list_jobs(self, *, include_all: bool = True) -> Sequence[Mapping[str, Any]]: ...

    def create(self, spec: Any, *, timeout: Optional[float] = None) -> Optional[Any]:
        """Create ``spec`` and return the job, or ``None`` when creation failed."""
        ...

    def remove(self, job: Any, *, owned: Iterable[str]) -> bool:
        """Remove one owned job; ``True`` only when the listing confirms it."""
        ...


@runtime_checkable
class WrapperProtocol(Protocol):
    """Launcher scripts in ``<HERMES_HOME>/scripts`` (A§7.2-7.3, L3)."""

    def install(self) -> Mapping[str, str]:
        """Ensure every wrapper; returns ``{name: state}``."""
        ...

    def remove_owned(self) -> Sequence[str]:
        """Remove only wrappers whose header and hash prove they are ours."""
        ...


@runtime_checkable
class NotifierProtocol(Protocol):
    """Outbox enqueue and delivery (A§8.6, L3)."""

    def enqueue(
        self,
        kind: str,
        params: Mapping[str, Any],
        *,
        credit_key: Optional[str] = None,
        dedupe_key: Optional[str] = None,
    ) -> None: ...

    def flush(self) -> Sequence[Any]:
        """Deliver queued notices; returns one result per attempted row."""
        ...


@runtime_checkable
class StoreProtocol(Protocol):
    """Persistence the flows use (A§6, L2). Kept narrow on purpose."""

    def meta_get(self, key: str) -> Optional[str]: ...

    def meta_set(self, key: str, value: str) -> None: ...

    def history_add(
        self,
        action: str,
        *,
        outcome: str,
        credit_key: Optional[str] = None,
        account_key: Optional[str] = None,
        role: Optional[str] = None,
        reason_code: Optional[str] = None,
    ) -> None: ...

    def history_tail(self, limit: int = 50) -> Iterable[Mapping[str, Any]]: ...


# --------------------------------------------------------------------- deps


@dataclass
class Deps:
    """The one injection container every flow receives (A§4).

    Every collaborator is optional so a partially wired process (``doctor``,
    the CLI before ``ensure``) can still answer ``status`` instead of
    crashing. A flow that needs a missing collaborator reports a
    ``problem_code``; it never raises into a cron run.
    """

    clock: Clock = field(default_factory=SystemClock)
    sleep: Callable[[float], None] = time.sleep
    source: Optional[Any] = None  # accounts.AccountSource
    api: object = None  # codex_api.CodexResetApi
    cli: object = None  # cli_runner.CliRunner
    store: object = None  # store.Store
    cron: object = None  # cron_gateway.CronGateway
    wrappers: object = None  # wrappers.Wrappers
    notifier: object = None  # notify.Notifier
    planner: object = None  # planner.Planner
    fire: object = None  # fire.Fire
    config: object = None  # settings writer (plugin config)
    lock: object = None  # run-lock factory (store.RunLock)
    settings: Optional["Settings"] = None
    salt: str = ""
    paths: Optional[Paths] = None
    hermes_home: Optional["Path"] = None
    #: Extra process facts a flow may need (never secrets).
    meta: Mapping[str, object] = field(default_factory=dict)

    def now(self) -> float:
        return self.clock.now()

    def wait(self, seconds: float) -> None:
        """Sleep through the injected clock so tests stay instant."""
        self.clock.sleep(seconds)
