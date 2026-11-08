"""Socket-free HTTP client for the plugin router (tests only).

The offline guard refuses every ``socket.__new__``, including the AF_UNIX
socketpair asyncio uses as its self-pipe, so neither ``TestClient`` nor a
stock event loop can start under it. This module provides:

* :class:`PipeLoop` — a selector event loop whose wake-up channel is an
  ``os.pipe`` instead of a socketpair (the loop still wakes when an executor
  thread finishes, which ``run_in_executor`` needs);
* :class:`AsgiClient` — sync ``get``/``post`` over ``httpx.ASGITransport``,
  the same in-process transport Hermes' plugin host child uses to serve a
  dashboard API (``hermes_cli/plugin_host_child.py:op_asgi``).
"""

from __future__ import annotations

import asyncio
import os
import selectors
from typing import Any

import httpx
from fastapi import FastAPI


class _PipeEnd:
    def __init__(self, fd: int):
        self._fd = fd
        os.set_blocking(fd, False)

    def fileno(self) -> int:
        return self._fd

    def recv(self, size: int) -> bytes:
        # BlockingIOError on an empty pipe ends asyncio's drain loop.
        return os.read(self._fd, size)

    def send(self, data: bytes) -> int:
        return os.write(self._fd, data)

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


class PipeLoop(asyncio.SelectorEventLoop):
    def __init__(self) -> None:
        super().__init__(selectors.DefaultSelector())

    # asyncio's private self-pipe hooks (selector_events.BaseSelectorEventLoop).
    def _make_self_pipe(self) -> None:
        read_fd, write_fd = os.pipe()
        self._ssock = _PipeEnd(read_fd)
        self._csock = _PipeEnd(write_fd)
        self._internal_fds += 1
        self._add_reader(self._ssock.fileno(), self._read_from_self)  # type: ignore[attr-defined]

    def _close_self_pipe(self) -> None:
        ssock, csock = self._ssock, self._csock
        self._remove_reader(ssock.fileno())  # type: ignore[attr-defined]
        ssock.close()
        self._ssock = None
        csock.close()
        self._csock = None
        self._internal_fds -= 1


class AsgiClient:
    def __init__(self, app: FastAPI):
        self._app = app
        self._loop = PipeLoop()

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def go() -> httpx.Response:
            transport = httpx.ASGITransport(app=self._app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://plugin-host"
            ) as client:
                return await client.request(method, url, **kwargs)

        return self._loop.run_until_complete(go())

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        loop = self._loop
        try:
            loop.run_until_complete(loop.shutdown_default_executor())
        finally:
            loop.close()
