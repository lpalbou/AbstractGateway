"""Host status (tray, Observer, console) computes its expensive parts once per short TTL, off
the event loop (gateway load 2026-10-26).

- GPU: an `ioreg`/`nvidia-smi` subprocess; the 1 s cache was stamped when the probe STARTED,
  so the tray's 1 Hz poll missed every time, and concurrent pollers each forked their own.
- Memory snapshot (`ioreg` + `sysctl`): keyed on the facade OBJECT, but the runtime builds a
  new facade per call, so it never hit live.
- `/host/state`: the residency walk (a provider call each) and the session-cache enumeration
  ran on every poll.
- `/host/runs`: new stores (artifact catalog, a cold run-index scan) per plane per call.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Dict

import pytest

from timing import patient  # noqa: E402 - tests/timing.py (wall-clock budgets)
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic


@pytest.fixture(autouse=True)
def _fresh_caches():
    from abstractgateway import host_status_cache

    host_status_cache.clear_all()
    yield
    host_status_cache.clear_all()


class _FakeClock:
    """The TTL cache's clock, advanced by the test: a loaded machine pausing the process between
    two samples cannot push the second one out of the TTL."""

    def __init__(self) -> None:
        self.now = 1000.0
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        with self._lock:
            return self.now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


def test_two_gpu_probes_within_the_ttl_run_one_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from abstractgateway import host_metrics, host_status_cache

    clock = _FakeClock()
    monkeypatch.setattr(host_status_cache, "time", SimpleNamespace(monotonic=clock.monotonic))
    calls = []

    def probe(*, timeout_s: float = 1.0) -> Dict[str, Any]:
        calls.append(clock.monotonic())
        time.sleep(0.05)  # real: the eight pollers below overlap this probe
        clock.advance(0.05)
        return {"supported": True, "source": "test", "utilization_gpu_pct": 1.0, "gpus": []}

    monkeypatch.setattr(host_metrics, "_read_host_gpu_metrics_best_effort", probe)
    a = host_metrics.get_host_gpu_metrics()
    clock.advance(1.1)  # the tray's next 1 Hz sample: inside the TTL, stamped when the probe ENDED
    b = host_metrics.get_host_gpu_metrics()
    assert len(calls) == 1 and a == b

    # Eight pollers at once on a cold cache: one subprocess, all eight get its answer.
    from abstractgateway.host_status_cache import gpu_cache

    gpu_cache.clear()
    calls.clear()
    out = []
    threads = [threading.Thread(target=lambda: out.append(host_metrics.get_host_gpu_metrics())) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(patient(5))
    assert len(calls) == 1 and len(out) == 8
    assert host_metrics.get_host_gpu_metrics(cache_ttl_s=0) and len(calls) == 2  # ttl 0 = probe now


class _Client:
    """The runtime's AbstractCore client behind every facade the runtime builds."""

    def __init__(self) -> None:
        self.calls: Dict[str, int] = {"memory": 0, "residency": 0, "caches": 0, "unload": 0}

    def get_memory_snapshot(self, **_: Any) -> Dict[str, Any]:
        self.calls["memory"] += 1
        time.sleep(0.02)
        return {"ts": 1.0, "ram": {"total_bytes": 1}, "process": {}, "device": {}}

    def list_model_residency(self, **_: Any) -> Dict[str, Any]:
        self.calls["residency"] += 1
        return {"ok": True, "supported": True, "models": [{"runtime_id": "r1", "provider": "mlx", "model": "qwen", "loaded": True}]}

    def list_session_prompt_caches(self, **_: Any) -> Dict[str, Any]:
        self.calls["caches"] += 1
        return {"ok": True, "caches": []}

    def unload_model_residency(self, **_: Any) -> Dict[str, Any]:
        self.calls["unload"] += 1
        return {"ok": True, "supported": True, "affected_models": []}


def _new_facade(client: _Client):
    """What `get_abstractcore_host_facade(runtime)` returns: a NEW facade per call."""
    from abstractruntime.integrations.abstractcore.host_facade import AbstractCoreHostFacade

    facade = object.__new__(AbstractCoreHostFacade)
    facade._client = client
    return facade


def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: _Client):
    flows = tmp_path / "flows"
    flows.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    import abstractgateway.routes.gateway as gateway_routes
    from abstractgateway import host_metrics

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (_new_facade(client), None))
    gpu = {"n": 0}

    def probe(*, timeout_s: float = 1.0) -> Dict[str, Any]:
        gpu["n"] += 1
        return {"supported": True, "source": "test", "utilization_gpu_pct": 3.0, "gpus": []}

    monkeypatch.setattr(host_metrics, "_read_host_gpu_metrics_best_effort", probe)
    from abstractgateway.app import app

    return TestClient(app), gpu


def test_two_host_state_calls_within_the_ttl_compute_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    import abstractgateway.routes.gateway as gateway_routes
    from abstractgateway import host_status_cache

    # A clock that does not move: the four calls are "within the TTL" however long the boot takes.
    monkeypatch.setattr(host_status_cache, "time", SimpleNamespace(monotonic=_FakeClock().monotonic))

    client = _Client()
    tc, gpu = _app(tmp_path, monkeypatch, client)
    install = {"n": 0}
    real_install = gateway_routes._gateway_install_block

    def counted_install():
        install["n"] += 1
        return real_install()

    monkeypatch.setattr(gateway_routes, "_gateway_install_block", counted_install)
    h = {"Authorization": "Bearer t"}
    with tc:
        first = tc.get("/api/gateway/host/state", headers=h)
        second = tc.get("/api/gateway/host/state", headers=h)
        live = tc.get("/api/gateway/host/metrics/live", headers=h)
        mem = tc.get("/api/gateway/host/metrics/memory", headers=h)
        assert first.status_code == second.status_code == live.status_code == mem.status_code == 200
        a, b = first.json(), second.json()
        assert a["models"] == b["models"] and a["memory"] == b["memory"] and a["degraded"] == b["degraded"] == []
        # Four status calls over facades built per call: ONE of each expensive part.
        assert client.calls["memory"] == 1, client.calls  # red before: the key was the facade object
        assert client.calls["residency"] == 1 and client.calls["caches"] == 1, client.calls
        assert gpu["n"] == 1 and install["n"] == 1

        # A residency change through the gateway drops the residency and memory entries.
        r = gateway_routes._gateway_model_residency_client_call(method_name="unload_model_residency", operation="unload")
        assert r.get("ok") is True and client.calls["unload"] == 1
        assert tc.get("/api/gateway/host/state", headers=h).status_code == 200
        assert client.calls["residency"] == 2 and client.calls["memory"] == 2 and gpu["n"] == 1


def test_host_status_probes_run_off_the_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    client = _Client()
    on_loop = []
    real = client.get_memory_snapshot

    def snapshot(**kw: Any) -> Dict[str, Any]:
        try:
            asyncio.get_running_loop()
            on_loop.append(True)
        except RuntimeError:
            on_loop.append(False)
        return real(**kw)

    client.get_memory_snapshot = snapshot  # type: ignore[method-assign]
    tc, _gpu = _app(tmp_path, monkeypatch, client)
    with tc:
        for path in ("/api/gateway/host/metrics/memory", "/api/gateway/host/state", "/api/gateway/host/metrics/live", "/api/gateway/host/runner"):
            from abstractgateway.host_status_cache import clear_all

            clear_all()
            assert tc.get(path, headers={"Authorization": "Bearer t"}).status_code == 200, path
    assert on_loop and not any(on_loop)


def test_host_runs_keeps_one_store_set_per_plane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import admin_runtimes, stores

    data = tmp_path / "data"
    (data / "users" / "default" / "alice" / "runtime").mkdir(parents=True)
    built = []
    real = stores.build_file_stores

    def counted(*, base_dir):
        built.append(str(base_dir))
        return real(base_dir=base_dir)

    monkeypatch.setattr(stores, "build_file_stores", counted)
    admin_runtimes.forget_stores()
    try:
        for _ in range(3):
            out = admin_runtimes.recent_runs_host_wide(data_dir=data, limit=25)
            assert out["ok"] is True and out["planes"] == ["default", "default/alice"]
        assert len(built) == 2, built  # once per plane, not once per plane per call
    finally:
        admin_runtimes.forget_stores()
