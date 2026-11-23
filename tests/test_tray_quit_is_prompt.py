"""Quit from the tray is prompt and takes its icon with it (operator report, Windows 10, 2026-11-22).

Before: the tray's Quit took more than 13 s and left a ghost icon in the notification area.

1. A requested shutdown (`POST /host/shutdown`, what Quit sends) set uvicorn's `should_exit` but
   never began `shutdown_drain`: the tray's own `GET /me/events/stream` held the exit for the
   whole requested drain (10 s, "Cancel 1 running task(s), timeout graceful shutdown exceeded").
2. The tray's `stop()` stopped the event follower first, and `EventFollower.stop()` called
   `resp.close()` while the follower thread was blocked reading the stream: that close waits for
   the buffered reader's lock the read holds. Windows does not wake that read on `shutdown()`, so
   `stop()` never reached `Icon.stop()`; the gateway terminated the helper after 3 s, and a
   terminated process leaves its notification-area icon behind.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from typing import Any, List

import pytest

from abstractgateway import host_control, shutdown_drain
from abstractgateway.tray.client import GatewayClient
from abstractgateway.tray.events import EventFollower

pytestmark = pytest.mark.basic


@pytest.fixture(autouse=True)
def _reset():
    host_control._reset_for_tests()
    yield
    host_control._reset_for_tests()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait(pred, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


class _FakeServer:
    def __init__(self) -> None:
        self.should_exit = False


@pytest.mark.parametrize("request_exit", ["shutdown", "restart"])
def test_a_requested_exit_begins_the_drain_like_a_signal(request_exit: str) -> None:
    server = _FakeServer()
    host_control.register_server(server, restartable=True, relaunch_argv=["serve"])
    assert not shutdown_drain.began()
    getattr(host_control, f"request_{request_exit}")(by="tray")
    assert server.should_exit is True
    assert shutdown_drain.began() and shutdown_drain.reason() == request_exit


def test_quit_with_the_trays_event_stream_open_exits_before_the_drain_times_out() -> None:
    """A real uvicorn shaped like `serve` (EndStreamsOnShutdown outermost, the requested drain
    bound): one endless SSE response, as the tray holds, must not hold the requested shutdown."""
    uvicorn = pytest.importorskip("uvicorn")

    async def sse(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": b": hello\n\n", "more_body": True})
        # Until the client leaves, as Starlette's StreamingResponse listens (and the gateway's
        # streams poll `request.is_disconnected`); the stream never ends by itself.
        while (await receive())["type"] != "http.disconnect":
            pass
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(shutdown_drain.EndStreamsOnShutdown(sse), host="127.0.0.1", port=port, log_level="warning"))
    host_control.register_server(server, restartable=False, block_reason="test")
    shutdown_drain.reset()
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    conn = None
    try:
        _wait(lambda: server.started)
        conn = socket.create_connection(("127.0.0.1", port), timeout=5)
        conn.sendall(b"GET /me/events/stream HTTP/1.1\r\nHost: x\r\nAccept: text/event-stream\r\n\r\n")
        got = b""
        while b"hello" not in got:
            chunk = conn.recv(4096)
            assert chunk, got
            got += chunk
        t0 = time.monotonic()
        host_control.request_shutdown(by="tray", reason="tray")
        thread.join(host_control.REQUESTED_EXIT_GRACEFUL_S + 10)
        took = time.monotonic() - t0
        assert not thread.is_alive()
        assert took < host_control.REQUESTED_EXIT_GRACEFUL_S / 2, f"the open stream held the Quit for {took:.1f} s"
    finally:
        if conn is not None:
            conn.close()
        server.should_exit = True
        thread.join(10)
        shutdown_drain.reset()


def _stalled_sse_server() -> tuple:
    """A loopback server that answers ONE SSE request with headers and a first chunk, then
    stays silent with the connection open (a gateway that is busy shutting down)."""
    lsock = socket.socket()
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(4)
    held: List[socket.socket] = []

    def _serve() -> None:
        while True:
            try:
                conn, _ = lsock.accept()
            except OSError:
                return
            held.append(conn)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            if b"/me/events/stream" in data:
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n"
                             b"8\r\n: hello\n\r\n")
            else:
                body = b'{"events": [], "more": false}'
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\n\r\n" + body)
                conn.close()

    threading.Thread(target=_serve, daemon=True).start()
    return lsock, held


def test_event_follower_stop_returns_while_its_read_is_blocked() -> None:
    lsock, held = _stalled_sse_server()
    port = lsock.getsockname()[1]
    follower = EventFollower(GatewayClient(f"http://127.0.0.1:{port}", "t"), on_event=lambda *_: None)
    try:
        follower.start()
        _wait(lambda: follower.state == "live" and follower._response is not None)
        time.sleep(0.2)  # the follower thread is now parked in readline()
        done = threading.Event()
        threading.Thread(target=lambda: (follower.stop(), done.set()), daemon=True).start()
        assert done.wait(2.0), "EventFollower.stop() blocked behind the stream read (the tray's Quit hang)"
    finally:
        lsock.close()
        for c in held:
            c.close()


def test_tray_stop_removes_the_icon_even_when_the_follower_cannot_stop() -> None:
    from abstractgateway.tray.app import TrayApp

    calls: List[str] = []
    release = threading.Event()

    class _Icon:
        def stop(self) -> None:
            calls.append("icon.stop")

    class _StuckFollower:
        def stop(self) -> None:
            calls.append("events.stop")
            release.wait(5)

    tray = TrayApp({"base_url": "http://127.0.0.1:9", "token": "t", "parent_pid": 0})
    tray._icon = _Icon()
    tray._events = _StuckFollower()  # type: ignore[assignment]
    t = threading.Thread(target=tray.stop, daemon=True)
    t.start()
    try:
        _wait(lambda: "events.stop" in calls or "icon.stop" in calls, 2)
        time.sleep(0.1)
        assert calls[0] == "icon.stop", calls
    finally:
        release.set()
        t.join(5)
