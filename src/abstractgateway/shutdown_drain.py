"""Bounded shutdown on SIGTERM (2026-10-29).

A stop signal used to wait for every open request to finish, and a live stream (the console's
`/me/events/stream`, a run's ledger stream, the tray's polling) never finishes by itself: one open
stream held a SIGTERM for the whole `timeout_graceful_shutdown` (120 s) before uvicorn gave up, and
the operator's supervisor killed the gateway first. Now:

1. `begin()` runs the moment uvicorn receives the signal (`cli._serve_with_host_controls` wraps
   `Server.handle_exit`).
2. `EndStreamsOnShutdown` (ASGI middleware, outermost) answers `http.disconnect` to every GET
   request's `receive()` from that moment: Starlette's `StreamingResponse` ends the stream at once,
   as if the client had left; a client reconnects to the next process with its cursor.
3. Other requests in flight get `REQUEST_DRAIN_S` (default 10 s,
   `ABSTRACTGATEWAY_GRACEFUL_SHUTDOWN_S`) to finish, then uvicorn cancels them; the lifespan
   shutdown (runner drain, entity homes, memory) has its own bounds.
4. The process exits 0: a SIGTERM is a requested stop, not a crash (`cli` installs a handler so
   uvicorn's re-raise of the signal after the drain does not kill the process with 143).
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Dict, Optional

REQUEST_DRAIN_S = 10

_began = threading.Event()
_began_at: Optional[float] = None
_reason: Optional[str] = None
_events: Dict[Any, asyncio.Event] = {}
_lock = threading.Lock()


def begin(reason: str) -> None:
    """The stop began: wake every stream waiting in `receive()` (safe from a signal handler)."""
    global _began_at, _reason
    with _lock:
        if not _began.is_set():
            _began_at, _reason = time.monotonic(), reason
        _began.set()
        pending = list(_events.items())
    for loop, event in pending:
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:  # that loop is closed
            pass


def began() -> bool:
    return _began.is_set()


def reason() -> Optional[str]:
    return _reason


def reset() -> None:
    """For tests and in-process restarts: a new serve starts not shutting down."""
    global _began_at, _reason
    with _lock:
        _began.clear()
        _began_at, _reason = None, None
        _events.clear()


def _event_for_running_loop() -> asyncio.Event:
    loop = asyncio.get_running_loop()
    with _lock:
        event = _events.get(loop)
        if event is None:
            event = _events[loop] = asyncio.Event()
            if _began.is_set():
                event.set()
        return event


_DISCONNECT = {"type": "http.disconnect"}


class EndStreamsOnShutdown:
    """GET requests see the client leave when the stop begins (see the module docstring)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or str(scope.get("method") or "").upper() != "GET":
            return await self.app(scope, receive, send)
        stopping = _event_for_running_loop()

        async def _receive():
            if stopping.is_set():
                return dict(_DISCONNECT)
            real = asyncio.ensure_future(receive())
            stop = asyncio.ensure_future(stopping.wait())
            try:
                done, _ = await asyncio.wait({real, stop}, return_when=asyncio.FIRST_COMPLETED)
            except BaseException:
                real.cancel()
                stop.cancel()
                raise
            if real in done:
                stop.cancel()
                return real.result()
            real.cancel()
            return dict(_DISCONNECT)

        return await self.app(scope, _receive, send)
