"""A thread holding the interpreter's import lock never freezes the event loop (gateway load
2026-10-26, the 04:07:46 watchdog exit, runtime/incidents/watchdog-20261026T030746Z.json).

The loop was blocked 31.5 s in `importlib._bootstrap` `__enter__`: `GET /discovery/capabilities`
(Flow and the console poll it) built its payload ON the loop, through `core_config` ->
AbstractCore's `list_capability_defaults` -> engine importability checks
(`importlib.util.find_spec`), which wait on the global import lock while another thread
imports. (The incident file named `/me/events/stream`, the OLDEST request in flight, a stream
open for 33 minutes; the loop stack is the capabilities route.) The payload, and
`GET /config/capability-defaults` (same chain), now run in the watchers pool.

Here a thread holds the import lock for 2 s while `/discovery/capabilities` is asked (its payload
is made to need the lock, as the engine checks do); `/api/health` and the event stream
(`GET /me/events/stream`, response headers) keep answering within 50 ms meanwhile.
"""

from __future__ import annotations

import _imp
import http.client
import importlib.util
import socket
import threading
import time
from pathlib import Path
from typing import List

import pytest

pytestmark = pytest.mark.basic

HOLD_S = 2.0
H = {"Authorization": "Bearer t"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _get(port: int, path: str, *, timeout: float = 30.0, headers_only: bool = False) -> tuple[int, float]:
    t0 = time.perf_counter()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path, headers=H)
        resp = conn.getresponse()  # returns once the status line and headers arrived
        if not headers_only:
            resp.read()
        return resp.status, time.perf_counter() - t0
    finally:
        conn.close()


def test_the_import_lock_held_2s_never_blocks_health_or_the_event_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    (tmp_path / "flows").mkdir()
    import abstractgateway.routes.gateway as gw
    from abstractgateway.app import app

    # The engine importability checks of the incident, made certain: the payload asks the
    # finder for a module nobody imported, which takes the global import lock.
    real_contracts = gw._build_client_capability_contracts
    seen_threads: List[str] = []

    def contracts_needing_the_import_lock(caps):
        importlib.util.find_spec("abstractgateway_perf_no_such_engine")
        seen_threads.append(threading.current_thread().name)
        return real_contracts(caps)

    monkeypatch.setattr(gw, "_build_client_capability_contracts", contracts_needing_the_import_lock)
    monkeypatch.setattr(gw, "_gateway_music_capability_probe", lambda: {"available": False})

    with TestClient(app):  # the boot (service built), then a real server for the stream
        port = _free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 15
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.02)
            assert server.started
            # Untimed first calls: lazy imports of the routes themselves are not the case here.
            assert _get(port, "/api/health")[0] == 200
            assert _get(port, "/api/gateway/discovery/capabilities", timeout=240)[0] == 200  # cold: real probes, can take 40 s
            assert _get(port, "/api/gateway/me/events/stream?heartbeat_s=30", headers_only=True)[0] == 200
            seen_threads.clear()

            held, release = threading.Event(), threading.Event()

            def hold_the_import_lock() -> None:
                _imp.acquire_lock()
                try:
                    held.set()
                    release.wait(HOLD_S)
                finally:
                    _imp.release_lock()

            holder = threading.Thread(target=hold_the_import_lock, daemon=True)
            holder.start()
            assert held.wait(5)
            t_hold = time.perf_counter()
            caps_result: List[tuple] = []
            caps = threading.Thread(target=lambda: caps_result.append(_get(port, "/api/gateway/discovery/capabilities")), daemon=True)
            caps.start()
            time.sleep(0.1)  # the capabilities payload is now waiting on the import lock

            lat: List[float] = []
            while time.perf_counter() - t_hold < HOLD_S - 0.2:
                code, dt = _get(port, "/api/health")
                assert code == 200
                lat.append(dt)
                code, dt = _get(port, "/api/gateway/me/events/stream?heartbeat_s=30", headers_only=True)
                assert code == 200
                lat.append(dt)
                time.sleep(0.05)
            release.set()
            holder.join(5)
            caps.join(30)
        finally:
            server.should_exit = True
            thread.join(10)

    # The capabilities answer waited for the lock (it ran into it), in the watchers pool.
    assert caps_result and caps_result[0][0] == 200, caps_result
    assert caps_result[0][1] >= HOLD_S - 0.4, f"the capabilities payload never waited on the lock: {caps_result}"
    assert seen_threads and all(n.startswith("gw-watchers") for n in seen_threads), seen_threads
    # Meanwhile health and the stream answered: under 50 ms, one OS-scheduling outlier tolerated
    # (the old code held the loop for the whole 2 s).
    ms = [round(x * 1000) for x in lat]
    assert len(lat) >= 6, ms
    assert sorted(lat)[-2] < 0.05 and max(lat) < 0.5, f"health / stream latencies while the import lock was held (ms): {ms}"
