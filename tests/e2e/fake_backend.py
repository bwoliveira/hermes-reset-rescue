"""A loopback stand-in for the Codex backend, reached through a CONNECT proxy.

Why a proxy instead of a plain ``http.server``
----------------------------------------------
The engine's transport (``reset_rescue/accounts._urllib_request``) is not
parameterised by a base URL, and it must not be: ``_url_allowed`` pins every
request to ``https://chatgpt.com`` (invariant I12), and a test-only base-URL
setting in production code would be a hole a real deployment could open. So the
test bends the *environment*, not the code:

* a loopback **CONNECT proxy** (``HTTPS_PROXY``) accepts the tunnel;
* it terminates TLS with a throw-away CA (``SSL_CERT_FILE``) whose certificate
  says ``CN=chatgpt.com``;
* every request the engine makes therefore arrives here, unmodified, with the
  real headers and the real body.

That keeps the production path exactly as shipped — same opener, same
``_NoRedirect`` handler, same TLS verification — while the bytes never leave
``127.0.0.1``. ``offline_guard.allow_loopback()`` is what makes the listener
legal under the guard; the guard still refuses any non-loopback address, so
this cannot silently become a real network call.

The fake records every request (method, path, selected headers) and answers
from a small scripted state, which is what lets a test assert *what the plugin
sent* rather than only what it printed.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

__all__ = [
    "CREDITS_PATH",
    "CONSUME_PATH",
    "USAGE_PATH",
    "FakeBackend",
    "RecordedRequest",
    "jwt_for",
]

CREDITS_PATH = "/backend-api/wham/rate-limit-reset-credits"
CONSUME_PATH = "/backend-api/wham/rate-limit-reset-credits/consume"
USAGE_PATH = "/backend-api/wham/usage"

#: Headers the fake keeps. Everything else is dropped unread: a test must never
#: hold a token it does not need, and the assertion surface stays small.
_KEPT_HEADERS = ("authorization", "chatgpt-account-id", "content-type", "user-agent")


@dataclass(frozen=True)
class RecordedRequest:
    """One request the engine made, reduced to what a test may assert on."""

    method: str
    path: str
    headers: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)

    @property
    def has_bearer(self) -> bool:
        return self.headers.get("authorization", "").startswith("Bearer ")

    def __repr__(self) -> str:  # never print the token
        return f"RecordedRequest({self.method} {self.path}, auth={self.has_bearer})"


def _b64(value: dict) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def jwt_for(
    account_id: str,
    email: str,
    *,
    user_id: str = "",
    expires_in: float = 86_400.0,
) -> str:
    """An unsigned JWT shaped like the pool's ``access_token``.

    Only the claims the plugin reads are present: ``chatgpt_account_id`` inside
    the OpenAI auth namespace (the *workspace*), ``sub`` (the stable per-user
    claim), and ``email`` (the label). The signature is never verified by the
    plugin or by this fake.

    ``user_id`` defaults to ``""``, which omits ``sub`` — the shape the first
    version of this fixture had, and the reason the e2e missed R3: with no
    subject, the local identity collapses to the bare workspace id, so an
    engine that sent the *identity* as ``ChatGPT-Account-ID`` still looked
    right. Every caller in this suite now passes a subject.
    """
    header = _b64({"alg": "none", "typ": "JWT"})
    claims: dict[str, Any] = {
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
        "email": email,
        "exp": int(time.time() + expires_in),
    }
    if user_id:
        claims["sub"] = user_id
    payload = _b64(claims)
    return f"{header}.{payload}.signature"


def _bearer(headers: Mapping[str, str]) -> str:
    """The token from an ``Authorization: Bearer …`` header (``""`` when absent)."""
    value = str(headers.get("authorization") or "")
    return value[7:].strip() if value.lower().startswith("bearer ") else ""


def _make_certificate(directory: Path) -> tuple[Path, Path]:
    """A throw-away cert for ``chatgpt.com``, valid for two days."""
    key = directory / "key.pem"
    cert = directory / "cert.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "2",
            "-subj",
            "/CN=chatgpt.com",
            "-addext",
            "subjectAltName=DNS:chatgpt.com",
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return cert, key


class FakeBackend:
    """A loopback CONNECT proxy that answers the three Codex endpoints.

    ``credits`` is the body of the next ``GET .../rate-limit-reset-credits``;
    ``consume`` the body of a ``POST .../consume``. A successful consume clears
    ``credits`` so the engine's readback sees the credit gone — which is the only
    thing that may confirm a redemption (invariant I1).
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.cert, key = _make_certificate(directory)
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._context.load_cert_chain(self.cert, key)

        self.requests: list[RecordedRequest] = []
        self.credits: dict[str, Any] = {"credits": [], "available_count": 0}
        #: Per-token credit lists (R17(b)): two users of one workspace have
        #: separate credits on the real backend, so a fixture that answers one
        #: global list would give both accounts the same credit. When set, the
        #: credits GET answers from here by the request's bearer token; a token
        #: with no entry gets an empty list.
        self.credits_by_token: dict[str, dict[str, Any]] = {}
        self.usage: dict[str, Any] = {"rate_limit": {}}
        self.consume: dict[str, Any] = {"code": "reset", "windows_reset": 1}
        self.consume_status = 200
        self._lock = threading.Lock()

        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(32)
        self.port = int(self._server.getsockname()[1])
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "FakeBackend":
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        try:
            self._server.close()
        except OSError:
            pass

    def env(self) -> dict[str, str]:
        """The environment that routes the engine's HTTPS traffic here."""
        proxy = f"http://127.0.0.1:{self.port}"
        return {
            "HTTPS_PROXY": proxy,
            "https_proxy": proxy,
            "HTTP_PROXY": proxy,
            "http_proxy": proxy,
            "SSL_CERT_FILE": str(self.cert),
            "NO_PROXY": "",
            "no_proxy": "",
        }

    # -- assertions --------------------------------------------------------

    def paths(self, method: Optional[str] = None) -> list[str]:
        with self._lock:
            return [r.path for r in self.requests if method is None or r.method == method]

    def posts(self) -> list[RecordedRequest]:
        with self._lock:
            return [r for r in self.requests if r.method == "POST"]

    def reset(self) -> None:
        with self._lock:
            self.requests.clear()

    # -- serving -----------------------------------------------------------

    def _accept_loop(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            # Read the CONNECT header **one byte at a time**: anything read past
            # the blank line would be the first bytes of the TLS ClientHello, and
            # those cannot be handed back to the ssl layer. Over-reading here
            # makes the handshake fail intermittently, only when the client's
            # header and hello land in the same TCP segment.
            head = self._read_line_block_slowly(conn)
            if not head:
                return
            first = head.split(b"\r\n", 1)[0].decode("latin-1")
            if not first.startswith("CONNECT "):
                conn.close()
                return
            conn.sendall(b"HTTP/1.0 200 Connection established\r\n\r\n")
            tls = self._context.wrap_socket(conn, server_side=True)
            try:
                self._handle(tls)
            finally:
                try:
                    tls.close()
                except OSError:
                    pass
        except (OSError, ssl.SSLError, ValueError):
            try:
                conn.close()
            except OSError:
                pass

    @staticmethod
    def _read_line_block_slowly(sock: socket.socket) -> bytes:
        """Read up to and including ``\\r\\n\\r\\n`` without over-reading."""
        data = b""
        while not data.endswith(b"\r\n\r\n"):
            chunk = sock.recv(1)
            if not chunk:
                return b""
            data += chunk
            if len(data) > 65_536:
                return b""
        return data

    @staticmethod
    def _read_until(sock: socket.socket, marker: bytes) -> bytes:
        data = b""
        while marker not in data:
            chunk = sock.recv(65536)
            if not chunk:
                return b""
            data += chunk
        return data

    def _handle(self, tls: ssl.SSLSocket) -> None:
        raw = self._read_until(tls, b"\r\n\r\n")
        if not raw:
            return
        head, _, body = raw.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        try:
            method, path, _ = lines[0].decode("latin-1").split(" ", 2)
        except ValueError:
            return
        headers: dict[str, str] = {}
        content_length = 0
        for line in lines[1:]:
            if b":" not in line:
                continue
            name, _, value = line.partition(b":")
            key = name.decode("latin-1").strip().lower()
            text = value.strip().decode("latin-1")
            # ``content-length`` is read for framing but never kept in the
            # record: the body is what a test asserts on, not its byte count.
            if key == "content-length":
                try:
                    content_length = int(text)
                except ValueError:
                    content_length = 0
                continue
            if key in _KEPT_HEADERS:
                headers[key] = text
        length = content_length
        while len(body) < length:
            chunk = tls.recv(65536)
            if not chunk:
                break
            body += chunk
        payload: dict[str, Any] = {}
        if body[:length]:
            try:
                parsed = json.loads(body[:length])
                payload = parsed if isinstance(parsed, dict) else {}
            except (ValueError, UnicodeDecodeError):
                payload = {}

        with self._lock:
            status, answer = self._answer(method, path, headers, payload)
            self.requests.append(
                RecordedRequest(method=method, path=path, headers=headers, body=payload)
            )
        encoded = json.dumps(answer).encode("utf-8")
        tls.sendall(
            b"HTTP/1.1 " + str(status).encode() + b" OK\r\n"
            b"Content-Type: application/json\r\n"
            b"Connection: close\r\n"
            b"Content-Length: " + str(len(encoded)).encode() + b"\r\n\r\n" + encoded
        )

    def _answer(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        token = _bearer(headers or {})
        if method == "POST" and path == CONSUME_PATH:
            status, answer = self.consume_status, dict(self.consume)
            if status in (200, 201) and answer.get("code") == "reset":
                # Spend only the requested credit; other banked credits must survive.
                current = self.credits_by_token.get(token, self.credits)
                credit_id = (payload or {}).get("credit_id")
                remaining = [r for r in current.get("credits", []) if r.get("id") != credit_id]
                updated = credits_payload(*remaining)
                if token and token in self.credits_by_token:
                    self.credits_by_token[token] = updated
                else:
                    self.credits = updated
            return status, answer
        if path == USAGE_PATH:
            return 200, dict(self.usage)
        if path == CREDITS_PATH:
            if self.credits_by_token:
                return 200, dict(self.credits_by_token.get(token, {"credits": []}))
            return 200, dict(self.credits)
        return 404, {"error": "not_found"}


def credit_row(
    credit_id: str, *, expires_in: float, status: str = "available", title: str = "Full reset"
) -> dict[str, Any]:
    """One row of the backend's reset-credit list."""
    now = time.time()
    return {
        "id": credit_id,
        "title": title,
        "reset_type": "codex_rate_limits",
        "status": status,
        "granted_at": now - 3_600.0,
        "expires_at": now + expires_in,
    }


def credits_payload(*rows: dict[str, Any]) -> dict[str, Any]:
    items = [dict(row) for row in rows]
    return {
        "credits": items,
        "available_count": sum(1 for r in items if r.get("status") == "available"),
    }


def pool_row(entry_id: str, token: str, account_id: str, label: str) -> dict[str, Any]:
    """One ``openai-codex`` row as Hermes stores it in ``auth.json``."""
    return {
        "id": entry_id,
        "provider": "openai-codex",
        "access_token": token,
        "refresh_token": f"refresh-{entry_id}",
        "account_id": account_id,
        "label": label,
        "base_url": "",
        "last_status": "ok",
    }


def write_auth(home: Path, rows: list[dict[str, Any]]) -> Path:
    """Write the temp profile's ``auth.json`` (0600, like Hermes' own)."""
    path = home / "auth.json"
    path.write_text(json.dumps({"credential_pool": {"openai-codex": rows}}), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def copy_plugin(repo: Path, home: Path, name: str = "reset-rescue") -> Path:
    """Copy the plugin into ``<home>/plugins/<name>/`` exactly as Hermes loads it."""
    target = home / "plugins" / name
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for entry in ("plugin.yaml", "__init__.py", "reset_rescue", "dashboard", "desktop"):
        source = repo / entry
        if not source.exists():
            continue
        if source.is_dir():
            shutil.copytree(
                source, target / entry, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
            )
        else:
            shutil.copy2(source, target / entry)
    return target
