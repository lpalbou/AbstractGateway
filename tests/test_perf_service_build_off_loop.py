"""Every service build runs off the event loop; the loop awaits it (gateway load 2026-10-26,
backlog 1054, round 3).

A cache miss in `get_gateway_service[_for_principal]` built the service INLINE, under
`_service_lock`, on whatever thread called it, and routes call it ON the loop. The pre-warm
middleware built off the loop first, but only for multi-user `/api/gateway` requests and only for
the caller's own plane: a single-user miss, any other path, an admin served by another plane or a
retry after a failed pre-warm parked the whole loop for the length of the build (seconds to
minutes), `/api/health` included. Now a build runs in its own thread, single-flight per service,
and the loop awaits its future; a miss that still reaches the loop answers 503 at once.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import Any, List

import pytest

pytestmark = pytest.mark.basic

BUILD_S = 5.0


def _fake_service() -> Any:
    return SimpleNamespace(config=SimpleNamespace(runner_enabled=False), runner=None, host=SimpleNamespace(run_store=None))


@pytest.fixture()
def svc_mod(monkeypatch: pytest.MonkeyPatch, tmp_path):
    import abstractgateway.service as svc_mod

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setattr(svc_mod, "_service", None)
    monkeypatch.setattr(svc_mod, "_boot_state", "ready")
    with svc_mod._service_lock:
        saved = dict(svc_mod._services_by_principal)
        svc_mod._services_by_principal.clear()
        svc_mod._building_keys.clear()
    svc_mod._build_futures.clear()
    svc_mod._build_errors.clear()
    yield svc_mod
    with svc_mod._service_lock:
        svc_mod._services_by_principal.clear()
        svc_mod._services_by_principal.update(saved)
        svc_mod._building_keys.clear()
    svc_mod._build_futures.clear()
    svc_mod._build_errors.clear()


def test_a_5s_build_during_a_request_never_blocks_health_beyond_50ms(svc_mod, monkeypatch) -> None:
    import httpx

    from abstractgateway.app import app

    builds: List[str] = []

    def _slow_create(config=None):  # a cold build: provider imports, store scans, bundle compilation
        builds.append(threading.current_thread().name)
        time.sleep(BUILD_S)
        return _fake_service()

    monkeypatch.setattr(svc_mod, "gateway_multi_user_enabled", lambda: False)
    monkeypatch.setattr(svc_mod, "create_default_gateway_service", _slow_create)

    async def scenario() -> tuple[Any, float, List[float]]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            assert (await client.get("/api/health", timeout=30)).status_code == 200  # untimed: lazy imports
            t0 = time.perf_counter()
            req = asyncio.ensure_future(client.get("/api/gateway/runs?limit=1", headers={"Authorization": "Bearer t"}, timeout=30))
            lat: List[float] = []
            while not req.done():
                h0 = time.perf_counter()
                r = await client.get("/api/health", timeout=5)
                lat.append(time.perf_counter() - h0)
                assert r.status_code == 200
                await asyncio.sleep(0.05)
            resp = await req
            return resp, time.perf_counter() - t0, lat

    resp, took, lat = asyncio.run(scenario())
    # The request waited for the build (it ran, it was not refused) ...
    assert took >= BUILD_S * 0.9 and resp.status_code != 503, (took, resp.status_code, resp.text)
    assert len(builds) == 1 and builds[0].startswith("gw-service-build"), builds
    # ... while the loop kept answering health: the old code built inline and froze it 5 s.
    assert len(lat) >= 20, f"only {len(lat)} health answers during a {BUILD_S:.0f} s build"
    # Every answer under 50 ms; ONE outlier up to 500 ms is tolerated for OS scheduling on a
    # loaded machine (the build itself, on the loop, delayed the first probe by the whole 5 s).
    ms = [round(x * 1000) for x in lat]
    assert sorted(lat)[-2] < 0.05 and max(lat) < 0.5, f"/api/health during the build (ms): {ms}"


def _principal(user_id: str):
    from abstractgateway.security.principal import GatewayPrincipal

    return GatewayPrincipal(user_id=user_id, tenant_id="default", roles=("user",), runtime_id=user_id, source="test")


def test_a_miss_on_the_loop_answers_at_once_and_builds_once_in_its_own_thread(svc_mod, monkeypatch) -> None:
    monkeypatch.setattr(svc_mod, "gateway_multi_user_enabled", lambda: True)
    monkeypatch.setattr(svc_mod, "_config_for_principal", lambda p: None)
    release = threading.Event()
    builds: List[str] = []
    svc = _fake_service()

    def _create(config=None):
        builds.append(threading.current_thread().name)
        release.wait(10)
        return svc

    monkeypatch.setattr(svc_mod, "create_default_gateway_service", _create)
    bob = _principal("bob")

    async def scenario() -> Any:
        svc_mod._PREWARM_ACTIVE.set(True)  # inside the app (the pre-warm middleware ran)
        t0 = time.perf_counter()
        with pytest.raises(svc_mod.ServiceStarting):
            svc_mod.get_gateway_service_for_principal(bob)  # on the loop: refused, never built here
        assert time.perf_counter() - t0 < 0.05
        waiters = [asyncio.ensure_future(svc_mod.ensure_gateway_service_for_principal(bob)) for _ in range(10)]
        await asyncio.sleep(0.1)
        assert not any(w.done() for w in waiters)
        release.set()
        got = await asyncio.gather(*waiters)
        assert all(g is svc for g in got)
        return svc_mod.get_gateway_service_for_principal(bob)  # cached now: answers on the loop

    assert asyncio.run(scenario()) is svc
    assert len(builds) == 1 and builds[0].startswith("gw-service-build"), builds


def test_a_worker_thread_still_builds_inline(svc_mod, monkeypatch) -> None:
    monkeypatch.setattr(svc_mod, "gateway_multi_user_enabled", lambda: True)
    monkeypatch.setattr(svc_mod, "_config_for_principal", lambda p: None)
    names: List[str] = []
    svc = _fake_service()
    monkeypatch.setattr(svc_mod, "create_default_gateway_service", lambda config=None: names.append(threading.current_thread().name) or svc)
    out: List[Any] = []
    t = threading.Thread(target=lambda: out.append(svc_mod.get_gateway_service_for_principal(_principal("carol"))), name="worker-x")
    t.start()
    t.join(5)
    assert out == [svc] and names == ["worker-x"]


def test_a_failed_build_reaches_the_request_with_its_own_error_then_retries(svc_mod, monkeypatch) -> None:
    monkeypatch.setattr(svc_mod, "gateway_multi_user_enabled", lambda: False)
    calls = {"n": 0}

    def _broken(config=None):
        calls["n"] += 1
        raise RuntimeError("bundle compile failed")

    monkeypatch.setattr(svc_mod, "create_default_gateway_service", _broken)

    async def scenario() -> None:
        svc_mod._PREWARM_ACTIVE.set(True)
        with pytest.raises(RuntimeError, match="bundle compile failed"):
            await svc_mod.ensure_gateway_service()  # what the pre-warm middleware awaits (and logs)
        with pytest.raises(RuntimeError, match="bundle compile failed"):
            svc_mod.get_gateway_service()  # the route's inline call: the real error, once
        with pytest.raises(svc_mod.ServiceStarting):
            svc_mod.get_gateway_service()  # the next request retries the build (off the loop)
        for _ in range(100):
            if not svc_mod._build_futures:
                break
            await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert calls["n"] == 2


def test_a_dead_runner_restarts_off_the_loop_and_the_service_keeps_serving(svc_mod, monkeypatch) -> None:
    monkeypatch.setattr(svc_mod, "gateway_multi_user_enabled", lambda: True)
    started: List[str] = []
    release = threading.Event()

    class _Runner:
        def start(self):
            started.append(threading.current_thread().name)
            release.wait(10)

        def runner_status(self):
            return {"enabled": True, "status": "dead_worker"}

    svc = SimpleNamespace(config=SimpleNamespace(runner_enabled=True), runner=_Runner())
    dave = _principal("dave")
    svc_mod._services_by_principal[svc_mod._principal_service_key(dave)] = svc

    async def scenario() -> float:
        t0 = time.perf_counter()
        assert svc_mod.get_gateway_service_for_principal(dave) is svc
        return time.perf_counter() - t0

    try:
        took = asyncio.run(scenario())
        for _ in range(200):
            if started:
                break
            time.sleep(0.01)
    finally:
        release.set()
    assert took < 0.05 and started and started[0].startswith("gw-service-build"), (took, started)


def test_an_app_without_the_prewarm_middleware_keeps_the_inline_build(svc_mod, monkeypatch) -> None:
    """A bare FastAPI app with the gateway router (tests, embedders): a miss on its loop builds
    inline as before (and warns once), instead of answering 503 forever."""
    monkeypatch.setattr(svc_mod, "gateway_multi_user_enabled", lambda: False)
    svc = _fake_service()
    monkeypatch.setattr(svc_mod, "create_default_gateway_service", lambda config=None: svc)

    async def scenario() -> Any:
        return svc_mod.get_gateway_service()

    assert asyncio.run(scenario()) is svc
