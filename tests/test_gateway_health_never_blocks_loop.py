"""/api/health must never park the event loop behind a service build
(boot-time lane, 2026-10-01).

The health route is `async def` and peeks at the service cache under
`_service_lock`. The eager rehydration (and any first-touch build) holds that
lock while it constructs a service — on a cold checkout that is minutes of
provider imports, and a build can also sit on a hung remote endpoint. With a
0.5 s bounded acquire, every probe froze the WHOLE loop for 0.5 s: a
supervisor plus five apps polling at once queued past their 3 s timeouts and
the apps exited with "gateway not reachable" (adversary stack 13:09-13:13).

These tests hold the lock in another thread (a build stuck on a hung remote
probe) and require health to answer at once, many times, concurrently.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

pytestmark = pytest.mark.basic


@pytest.fixture()
def build_holding_the_lock():
    import abstractgateway.service as svc_mod

    held = threading.Event()
    release = threading.Event()

    def _stuck_build() -> None:
        with svc_mod._service_lock:
            held.set()
            release.wait(30)  # a build waiting on a remote endpoint that never answers

    t = threading.Thread(target=_stuck_build, daemon=True)
    t.start()
    assert held.wait(5)
    try:
        yield
    finally:
        release.set()
        t.join(5)


def test_health_snapshot_returns_immediately_while_a_build_holds_the_lock(build_holding_the_lock) -> None:
    from abstractgateway.service import gateway_runner_health_snapshot

    t0 = time.monotonic()
    for _ in range(10):
        snap = gateway_runner_health_snapshot()
        assert snap["building"] is True
    elapsed = time.monotonic() - t0
    # The bounded 0.5 s acquire made this 5 s; non-blocking is microseconds.
    assert elapsed < 0.5, f"10 health snapshots took {elapsed:.2f}s behind a held build lock"


def test_concurrent_health_probes_answer_within_a_second_during_a_stuck_build(build_holding_the_lock) -> None:
    import httpx

    from abstractgateway.app import app

    async def _probe_many(n: int) -> tuple[float, list[dict]]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            t0 = time.monotonic()
            responses = await asyncio.gather(*[client.get("/api/health", timeout=5) for _ in range(n)])
            return time.monotonic() - t0, [r.json() for r in responses if r.status_code == 200]

    # Supervisor + five apps, each polling: 12 probes in flight at once.
    elapsed, bodies = asyncio.run(_probe_many(12))
    assert len(bodies) == 12
    assert all(b["runner"].get("building") is True for b in bodies)
    assert all(b.get("warming_up") is True for b in bodies)
    assert elapsed < 1.0, f"12 concurrent /api/health probes took {elapsed:.2f}s during a stuck build"
