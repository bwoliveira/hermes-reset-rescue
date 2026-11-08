"""Offline guard for the Reset Rescue test suites.

``scripts/run_tests.py`` installs this hook before any test module is imported,
so a test that reaches for the network, starts a process that is not on the
allow-list, or writes into the real Hermes profile fails instead of quietly
passing on this machine and breaking on a reviewer's.

The hook is an audit hook (``sys.addaudithook``), not a mock: it sees the real
``socket`` / ``subprocess`` / ``open`` calls the code under test makes, whether
they come from the plugin, from the Hermes runtime or from a test helper.

Rules
-----
* **AF_UNIX is always allowed.** An ``AF_UNIX`` socketpair is the self-pipe
  every ``asyncio`` event loop creates in ``_make_self_pipe``; it is local IPC
  with no address family that can reach the network, so blocking it only forced
  callers into private-loop workarounds. A path-bound ``AF_UNIX`` socket is
  allowed too: it is a file, and the write rules below still apply to it.
* **INET/INET6 sockets are refused by default**, at creation. A test that
  genuinely needs a loopback listener opts in with :func:`allow_loopback`, and
  the opt-in is scoped: inside it, ``bind``/``connect``/``sendto`` must name a
  loopback address and name resolution must be a loopback literal. Anything
  else raises, so the opt-in cannot become a hole that reaches the internet.
* ``http.client`` / ``urllib`` request events raise as well (defence in depth:
  they would need a socket anyway);
* a process start raises unless its executable base name is on the allow-list
  (``RR_SUBPROCESS_ALLOWLIST``, comma separated, or ``allow_subprocess(...)``);
* a write into the real Hermes profile (``auth.json``, ``config.yaml``,
  ``state.db``, ``cron/``, ``plugin-data/``, ``plugins/``, ``scripts/``,
  ``logs/``, ``skills/``, ``memories/``, ``profiles/``) raises unless the path
  is under the scratch root (``TMPDIR``), ``/tmp`` or the worktree.

Reads are never blocked: importing the Hermes runtime from its install
directory is legitimate, writing to it is not.
"""

from __future__ import annotations

import ipaddress
import os
import sys
import tempfile
import urllib.parse
from pathlib import Path

__all__ = [
    "OfflineViolation",
    "allow_loopback",
    "allow_subprocess",
    "installed",
    "install",
    "loopback_allowed",
    "protected_paths",
]


class OfflineViolation(RuntimeError):
    """A test tried to leave the machine, start a process or write real state."""


#: Address families that are local IPC by construction. ``AF_UNIX`` is 1 on
#: Linux and macOS (``AF_LOCAL`` is an alias where it exists).
_LOCAL_FAMILIES = frozenset({1})

#: Families that can reach a network and are therefore gated by the loopback
#: opt-in. ``AF_INET`` is 2; ``AF_INET6`` is 10 on Linux and 30 on macOS.
_NETWORK_FAMILIES = frozenset({2, 10, 30})

#: Events carrying a ``(host, port)`` tuple or an ``AF_UNIX`` path in ``args[1]``.
_SOCKET_ADDRESS_EVENTS = frozenset(
    {
        "socket.bind",
        "socket.connect",
        "socket.connect_ex",
        "socket.sendto",
    }
)

#: Events carrying a host name or literal in ``args[0]``.
_SOCKET_NAME_EVENTS = frozenset(
    {
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.gethostbyaddr",
        "socket.getnameinfo",
    }
)

_HTTP_EVENTS = frozenset(
    {
        "http.client.connect",
        "http.client.send",
    }
)

#: ``urllib.Request`` carries the URL as ``args[0]``, so it can be judged by
#: host instead of refused outright: inside :class:`allow_loopback` a loopback
#: URL is legitimate, anything else is not.
_URLLIB_EVENT = "urllib.Request"

_PROCESS_EVENTS = frozenset(
    {
        "subprocess.Popen",
        "os.system",
        "os.exec",
        "os.posix_spawn",
        "os.spawn",
        "os.fork",
        "os.forkpty",
        "pty.spawn",
    }
)

#: File names / directories under the real profile that must never be written.
_PROTECTED_NAMES = (
    "auth.json",
    "config.yaml",
    "config.yml",
    "state.db",
    "cron",
    "plugin-data",
    "plugins",
    "scripts",
    "logs",
    "skills",
    "memories",
    "profiles",
    "desktop-plugins",
    "sessions",
    "environments",
)

_WRITE_MODES = frozenset("wax+")

_LOOPBACK_V4 = ipaddress.ip_network("127.0.0.0/8")
_LOOPBACK_V6 = ipaddress.ip_network("::1/128")


def _env_list(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


class _State:
    installed = False
    real_home: Path | None = None
    allowed_roots: tuple[Path, ...] = ()
    allowlist: list[str] = []
    loopback_depth = 0
    request_hosts: list[str] = []
    violations: list[str] = []


_state = _State()


def _resolve(path: str | os.PathLike[str]) -> Path:
    try:
        return Path(os.fspath(path)).expanduser().resolve()
    except (OSError, ValueError):
        return Path(os.fspath(path)).expanduser()


def _under(path: Path, root: Path) -> bool:
    try:
        return path.is_relative_to(root)
    except (OSError, ValueError):
        return False


# --------------------------------------------------------------------- loopback


def loopback_allowed() -> bool:
    """True while an :func:`allow_loopback` block is active."""
    return _state.loopback_depth > 0


def _is_loopback_host(host: object) -> bool:
    """True when *host* is a loopback literal (never a name that must resolve)."""
    if isinstance(host, (bytes, bytearray)):
        try:
            host = bytes(host).decode("ascii", "ignore")
        except Exception:  # pragma: no cover - defensive
            return False
    if not isinstance(host, str):
        return False
    text = host.strip().strip("[]")
    if not text:
        return False
    # ``localhost`` is the one name that is loopback by definition and resolves
    # without a DNS query on every platform we support.
    if text.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    return address in _LOOPBACK_V4 or address in _LOOPBACK_V6


def _address_is_loopback(args: tuple) -> bool:
    """Inspect a ``socket.bind``/``connect``/``sendto`` argument tuple.

    The address is ``args[1]``: a ``(host, port)`` tuple for INET families, or a
    filesystem path (``str``/``bytes``) for ``AF_UNIX``. A path is local IPC and
    always allowed; a tuple must name a loopback host.
    """
    if len(args) < 2:
        return False
    address = args[1]
    if isinstance(address, (str, bytes, bytearray)):
        return True  # AF_UNIX path: a file, not a network address
    if isinstance(address, (tuple, list)) and address:
        return _is_loopback_host(address[0])
    return False


class allow_loopback:
    """Scope in which ``AF_INET``/``AF_INET6`` sockets may exist.

    Inside the block sockets may be created, but every ``bind``, ``connect``,
    ``sendto`` and name lookup must name a loopback address. Leaving the block
    restores the default (any INET socket creation raises), even on failure.

    *hosts* names URL hosts that a request may **address** while the socket
    layer is still forced to loopback. It exists for one case: an end-to-end
    test that must exercise the shipped transport, whose URL is pinned to
    ``https://chatgpt.com`` (I12), while an ``HTTPS_PROXY`` routes the actual
    connection to ``127.0.0.1``. The guarantee is unchanged — the connection
    itself is gated by :func:`_check_socket`, so a host listed here that is not
    proxied to loopback still raises instead of reaching the internet.
    """

    def __init__(self, *hosts: str) -> None:
        self.hosts = tuple(host.strip().lower() for host in hosts if host.strip())
        self._added: list[str] = []

    def __enter__(self) -> "allow_loopback":
        _state.loopback_depth += 1
        for host in self.hosts:
            if host not in _state.request_hosts:
                _state.request_hosts.append(host)
                self._added.append(host)
        return self

    def __exit__(self, *exc_info: object) -> None:
        for host in self._added:
            try:
                _state.request_hosts.remove(host)
            except ValueError:
                pass
        self._added.clear()
        _state.loopback_depth = max(0, _state.loopback_depth - 1)


def protected_paths(real_home: Path | None = None) -> tuple[Path, ...]:
    """Return the real-profile paths the guard refuses to write."""
    home = real_home or _state.real_home
    if home is None:
        return ()
    return tuple(home / name for name in _PROTECTED_NAMES)


def _write_allowed(path: Path) -> bool:
    for root in _state.allowed_roots:
        if _under(path, root):
            return True
    return False


def _check_write(path: Path) -> None:
    home = _state.real_home
    if home is None or not _under(path, home) or _write_allowed(path):
        return
    for protected in protected_paths(home):
        if _under(path, protected):
            raise OfflineViolation(
                f"offline guard: refusing to write {path} inside the real Hermes "
                f"profile ({home}). Tests must use a temporary HERMES_HOME under "
                f"the scratch root."
            )


def _check_process(event: str, args: tuple) -> None:
    name = ""
    if event == "subprocess.Popen":
        argv = args[1] if len(args) > 1 else (args[0] if args else None)
        if isinstance(argv, (list, tuple)) and argv:
            name = os.path.basename(str(argv[0]))
        elif argv:
            name = os.path.basename(str(argv).split(" ")[0])
    elif args:
        name = os.path.basename(str(args[0]))
    if name in _state.allowlist:
        return
    raise OfflineViolation(
        f"offline guard: unexpected process start ({event}: {name or '<unknown>'}). "
        f"Offline tests use fakes; add the executable to RR_SUBPROCESS_ALLOWLIST "
        f"only for a test that must prove the real contract."
    )


def _check_socket(event: str, args: tuple) -> None:
    """Gate one ``socket.*`` audit event.

    ``AF_UNIX`` is local IPC and always allowed. Network families are allowed
    only inside :class:`allow_loopback`, and even then only for a loopback
    address or an ``AF_UNIX`` path.
    """
    if event == "socket.__new__":
        family = args[1] if len(args) > 1 else None
        if family in _LOCAL_FAMILIES:
            return
        if family in _NETWORK_FAMILIES and not loopback_allowed():
            _state.violations.append(event)
            raise OfflineViolation(
                f"offline guard: network access attempted ({event}, family={family}). "
                f"Reset Rescue tests run offline; use tests/fakes.FakeTransport, or "
                f"wrap a genuine loopback listener in offline_guard.allow_loopback()."
            )
        return  # an unknown family cannot be judged here; its address events are
    if event in _SOCKET_ADDRESS_EVENTS:
        if loopback_allowed() and _address_is_loopback(args):
            return
        _state.violations.append(event)
        if loopback_allowed():
            raise OfflineViolation(
                f"offline guard: {event} refused a non-loopback address. Inside "
                f"allow_loopback() only 127.0.0.0/8, ::1 and AF_UNIX paths are reachable."
            )
        raise OfflineViolation(
            f"offline guard: network access attempted ({event}). Reset Rescue "
            f"tests run offline; use tests/fakes.FakeTransport."
        )
    if event in _SOCKET_NAME_EVENTS:
        host = args[0] if args else None
        if loopback_allowed() and _is_loopback_host(host):
            return
        _state.violations.append(event)
        if loopback_allowed():
            raise OfflineViolation(
                f"offline guard: {event} refused to resolve {host!r}. Inside "
                f"allow_loopback() only loopback literals may be resolved; a real "
                f"hostname here would be a DNS query."
            )
        raise OfflineViolation(
            f"offline guard: network access attempted ({event}). Reset Rescue "
            f"tests run offline; use tests/fakes.FakeTransport."
        )
    # Any other ``socket.*`` event (``sethostname`` and friends) is not something
    # the plugin or its tests do; leave it to the platform.
    return


def _url_is_loopback(url: object) -> bool:
    """True when *url* names a loopback host or a host this scope declared."""
    if not isinstance(url, str):
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if _is_loopback_host(host):
        return True
    return bool(host) and host in _state.request_hosts


def _hook(event: str, args: tuple) -> None:
    if event.startswith("socket."):
        _check_socket(event, args)
        return
    if event == _URLLIB_EVENT:
        url = args[0] if args else None
        if loopback_allowed() and _url_is_loopback(url):
            return
        _state.violations.append(event)
        if loopback_allowed():
            raise OfflineViolation(
                "offline guard: urllib refused a non-loopback URL. Inside "
                "allow_loopback() only 127.0.0.0/8 and ::1 are reachable."
            )
        raise OfflineViolation(
            f"offline guard: network access attempted ({event}). Reset Rescue "
            f"tests run offline; use tests/fakes.FakeTransport."
        )
    if event in _HTTP_EVENTS:
        # Both ``http.client`` events carry the connection object first:
        # ``connect(self, host, port)`` and ``send(self, data)``. The connection's
        # own ``host`` is what the request is addressed to (the proxy, when one is
        # configured), so it gets the same loopback/declared rule as urllib.
        if loopback_allowed():
            host = None
            if event == "http.client.connect" and len(args) > 1:
                host = args[1]
            else:
                connection = args[0] if args else None
                host = getattr(connection, "host", None)
            if _is_loopback_host(host) or (
                isinstance(host, str) and host.lower() in _state.request_hosts
            ):
                return
        _state.violations.append(event)
        raise OfflineViolation(
            f"offline guard: network access attempted ({event}). Reset Rescue "
            f"tests run offline; use tests/fakes.FakeTransport."
        )
    if event in _PROCESS_EVENTS:
        _state.violations.append(event)
        _check_process(event, args)
        return
    if event == "open":
        path, mode, flags = (tuple(args) + (None, None, None))[:3]
        if path is None or isinstance(path, int):
            return
        writing = False
        if isinstance(mode, str) and mode:
            writing = any(ch in _WRITE_MODES for ch in mode)
        elif isinstance(flags, int):
            write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            writing = bool(flags & write_flags)
        if writing:
            _check_write(_resolve(path))


def install(
    *,
    real_home: str | os.PathLike[str] | None = None,
    allowed_roots: tuple[str | os.PathLike[str], ...] = (),
    allow_subprocess_names: tuple[str, ...] = (),
) -> None:
    """Install the audit hook. Idempotent; the first call wins."""
    if _state.installed:
        return
    home = real_home or os.environ.get("HERMES_HOME") or "~/.hermes"
    _state.real_home = _resolve(home)
    roots = [Path(os.fspath(root)).expanduser().resolve() for root in allowed_roots]
    scratch = os.environ.get("TMPDIR") or tempfile.gettempdir()
    roots.append(_resolve(scratch))
    roots.append(_resolve("/tmp"))
    _state.allowed_roots = tuple(dict.fromkeys(roots))
    _state.allowlist = list(allow_subprocess_names) + list(_env_list("RR_SUBPROCESS_ALLOWLIST"))
    sys.addaudithook(_hook)
    _state.installed = True


class allow_subprocess:
    """Context manager that lets one executable through the guard.

    ``with allow_subprocess("sleep"):`` — used by a test that has to prove the
    real ``cli_runner`` timeout contract instead of a fake.
    """

    def __init__(self, *names: str):
        self.names = [os.path.basename(name) for name in names]
        self._added: list[str] = []

    def __enter__(self) -> "allow_subprocess":
        for name in self.names:
            if name not in _state.allowlist:
                _state.allowlist.append(name)
                self._added.append(name)
        return self

    def __exit__(self, *exc_info: object) -> None:
        for name in self._added:
            try:
                _state.allowlist.remove(name)
            except ValueError:
                pass
        self._added.clear()


def installed() -> bool:
    return _state.installed
