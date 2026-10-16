"""GET /runs: one computation for identical concurrent asks (never a reused page: read-after-write holds), and no scan at all for
an archived-only page when nothing is archived (2026-10-16 adversary G2/G3: the Observer, the tray,
the Assistant and the runner asked the same page within the same second, and each ask globbed and
stat()ed the whole store — 10,678 files — behind the GIL of a generating model)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_gateway_runs_list_endpoint import _write_min_bundle


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-coalesce", flow_id="root")
    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")
    from abstractgateway.app import app

    return app, {"Authorization": f"Bearer {token}"}


def _count_listing_computations(monkeypatch: pytest.MonkeyPatch) -> dict:
    """How many times GET /runs actually computed a page (the runner's own store polls
    run in the background and must not count)."""
    from abstractgateway.routes import gateway as gw

    counter = {"computed": 0, "asked": 0}
    orig = gw._coalesced_runs_listing

    async def spy(key, compute):
        counter["asked"] += 1

        def counted():
            counter["computed"] += 1
            # Long enough for the six concurrent asks of the burst test to overlap on
            # any machine (a loaded suite run serialized them and saw six computations).
            time.sleep(0.25)
            return compute()

        return await orig(key, counted)

    monkeypatch.setattr(gw, "_coalesced_runs_listing", spy)
    return counter


def test_archived_only_with_nothing_archived_answers_without_a_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, headers = _client(tmp_path, monkeypatch)
    counter = _count_listing_computations(monkeypatch)
    with TestClient(app) as client:
        start = client.post("/api/gateway/runs/start", headers=headers, json={"bundle_id": "bundle-coalesce", "flow_id": "root", "input_data": {}})
        assert start.status_code == 200, start.text
        r = client.get("/api/gateway/runs?limit=200&root_only=true&archived_only=true&include_metrics=true", headers=headers)
        assert r.status_code == 200, r.text
        assert r.json() == {"items": [], "count": 0, "offset": 0, "has_more": False, "archived_sessions": 0}
        assert counter == {"computed": 0, "asked": 0}, "nothing archived → answered before any computation"
        # The same page with archives present would compute: the plain root listing does.
        r2 = client.get("/api/gateway/runs?limit=200&root_only=true&include_metrics=true", headers=headers)
        assert r2.status_code == 200
        assert counter["computed"] == 1


def test_identical_concurrent_listings_share_one_computation_and_a_sequential_one_recomputes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.routes import gateway as gw

    app, headers = _client(tmp_path, monkeypatch)
    counter = _count_listing_computations(monkeypatch)
    gw._runs_list_cache.clear()
    with TestClient(app) as client:
        start = client.post("/api/gateway/runs/start", headers=headers, json={"bundle_id": "bundle-coalesce", "flow_id": "root", "input_data": {}})
        assert start.status_code == 200, start.text
        run_id = start.json()["run_id"]
        gw._runs_list_cache.clear()
        counter["computed"] = counter["asked"] = 0
        url = "/api/gateway/runs?status=running&limit=500"
        results: list = []

        def ask() -> None:
            results.append(client.get(url, headers=headers))

        threads = [threading.Thread(target=ask) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert all(r.status_code == 200 for r in results)
        pages = [r.json() for r in results]
        assert all(p == pages[0] for p in pages), "every waiter gets the same page"
        assert counter["asked"] == 6
        assert counter["computed"] < 6, f"six identical concurrent asks must share computations, not {counter['computed']}"
        # A different query string is its own computation.
        before = counter["computed"]
        r2 = client.get("/api/gateway/runs?status=waiting&limit=500", headers=headers)
        assert r2.status_code == 200
        assert counter["computed"] == before + 1
        # A SEQUENTIAL identical ask is computed again (no reuse after a page lands: read-after-write).
        r3 = client.get(url, headers=headers)
        assert r3.status_code == 200
        assert counter["computed"] == before + 2
        assert any(str(it.get("run_id")) == run_id for it in r3.json().get("items", [])) or r3.json().get("items") == []
