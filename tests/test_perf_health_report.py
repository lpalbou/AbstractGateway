"""`/api/health` carries the load signals of backlog 1054: `pools` (work_pools.py, 429dfcf5),
`run_index` (mode + the counters of every plane's run store, f88c3d8e / 5167d9e1) and
`gc_tuning` (what `serve --gc-tuning` applied, b1f1c7ae). Each block is filled in a try/except
in app.py: a broken one would vanish silently, so this test names every key it expects."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict

import pytest

pytestmark = pytest.mark.basic


def _health() -> Dict[str, Any]:
    import httpx

    from abstractgateway.app import app

    async def go() -> Dict[str, Any]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            r = await client.get("/api/health", timeout=10)
            assert r.status_code == 200
            return r.json()

    return asyncio.run(go())


@pytest.fixture()
def svc_mod(monkeypatch: pytest.MonkeyPatch):
    import abstractgateway.service as svc_mod

    monkeypatch.setattr(svc_mod, "_service", None)
    monkeypatch.setattr(svc_mod, "_boot_state", "ready")
    monkeypatch.setattr(svc_mod, "_services_by_principal", {})
    monkeypatch.setattr(svc_mod, "_gc_tuning_report", {})
    monkeypatch.setattr(svc_mod, "_gc_tuning_on", None)
    return svc_mod


def test_health_reports_pools(svc_mod) -> None:
    from abstractgateway.work_pools import KINDS, pool_sizes

    body = _health()
    assert set(body["pools"]) == set(KINDS)
    for kind, size in pool_sizes().items():
        assert body["pools"][kind]["size"] == size
        assert {"threads", "queued"} <= set(body["pools"][kind])


def test_health_reports_run_index_mode_and_counters_of_every_plane(svc_mod, tmp_path, monkeypatch) -> None:
    from abstractruntime.core.models import RunState, RunStatus

    from abstractgateway.stores import build_file_stores, run_index_mode

    # No service yet: the mode alone, zero planes (health never builds a service).
    body = _health()
    assert body["run_index"]["mode"] == run_index_mode() and body["run_index"]["planes"] == 0

    # A real file store on the default plane, a second (fake) plane: counters summed.
    stores = build_file_stores(base_dir=tmp_path / "runtime")
    rs = stores.run_store
    for i in range(3):
        rs.save(RunState(run_id=f"r{i}", workflow_id="w@1:root", status=RunStatus.COMPLETED, current_node="end", vars={},
                         created_at="2026-10-26T09:00:00+00:00", updated_at="2026-10-26T09:00:00+00:00"))
    rs.list_runs(limit=10)
    real = rs.run_index_stats()
    assert isinstance(real.get("entries"), int)
    monkeypatch.setattr(svc_mod, "_service", SimpleNamespace(stores=stores, runner=None))
    other = SimpleNamespace(run_index_stats=lambda: {"mode": real.get("mode"), "entries": 7, "scanned_listings": 2})
    svc_mod._services_by_principal["default:bob"] = SimpleNamespace(stores=SimpleNamespace(run_store=other), runner=None)

    ri = _health()["run_index"]
    assert ri["mode"] == run_index_mode()
    assert ri["planes"] == 2
    assert ri["entries"] == real["entries"] + 7 >= 10
    assert ri["scanned_listings"] == int(real.get("scanned_listings") or 0) + 2


def test_health_reports_gc_tuning_once_serve_chose(svc_mod) -> None:
    assert "gc_tuning" not in _health()  # not started by serve: nothing to report
    svc_mod.set_gc_tuning(True, source="serve")
    assert _health()["gc_tuning"] == {"requested": "on", "source": "serve"}
    svc_mod._gc_tuning_report.update({"applied": True, "thresholds": [50000, 50, 100], "frozen": 12, "line": "gc: froze"})
    gc = _health()["gc_tuning"]
    assert gc["applied"] is True and gc["thresholds"] == [50000, 50, 100] and "line" not in gc
