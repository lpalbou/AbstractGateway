"""Every gateway list reaches EVERY item (operator ruling 2026-08-19),
pages of at most 100/200 — pinned on the /runs listing's new `offset`.

Pins:
- offset slices AFTER filtering, most-recent-first, no overlap between pages;
- has_more is honest (true while a next page exists, false on the last);
- offset stays a KNOWN query param (the unknown-param refusal must not
  reject it).
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic


def _write_min_bundle(*, bundles_dir: Path, bundle_id: str, flow_id: str) -> None:
    bundles_dir.mkdir(parents=True, exist_ok=True)
    flow = {
        "id": flow_id,
        "name": "minimal",
        "description": "",
        "interfaces": [],
        "nodes": [
            {"id": "node-1", "type": "on_flow_start", "position": {"x": 32.0, "y": 128.0},
             "data": {"nodeType": "on_flow_start", "label": "On Flow Start", "inputs": [], "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]}},
            {"id": "node-2", "type": "on_flow_end", "position": {"x": 288.0, "y": 128.0},
             "data": {"nodeType": "on_flow_end", "label": "On Flow End", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []}},
        ],
        "edges": [{"id": "e1", "source": "node-1", "sourceHandle": "exec-out", "target": "node-2", "targetHandle": "exec-in"}],
        "entryNode": "node-1",
    }
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-01-19T00:00:00+00:00",
        "entrypoints": [{"flow_id": flow_id, "name": "root", "description": "", "interfaces": []}],
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }
    with zipfile.ZipFile(bundles_dir / f"{bundle_id}.flow", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr(f"flows/{flow_id}.json", json.dumps(flow, indent=2))


def test_runs_listing_pages_reach_every_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-pages", flow_id="root")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        made = []
        for _ in range(7):
            start = client.post(
                "/api/gateway/runs/start",
                headers=headers,
                json={"bundle_id": "bundle-pages", "flow_id": "root", "input_data": {}},
            )
            assert start.status_code == 200, start.text
            made.append(start.json()["run_id"])

        def page(offset: int, limit: int = 3):
            r = client.get(
                "/api/gateway/runs",
                headers=headers,
                params={"limit": limit, "offset": offset, "include_ledger_len": "false"},
            )
            assert r.status_code == 200, r.text
            return r.json()

        p0, p1, p2 = page(0), page(3), page(6)
        ids0 = [it["run_id"] for it in p0["items"]]
        ids1 = [it["run_id"] for it in p1["items"]]
        ids2 = [it["run_id"] for it in p2["items"]]
        assert len(ids0) == 3 and len(ids1) == 3 and len(ids2) >= 1
        assert not (set(ids0) & set(ids1)) and not (set(ids1) & set(ids2)), "pages never overlap"
        assert p0["has_more"] is True and p1["has_more"] is True
        # The union of pages reaches EVERY started run.
        walked = set(ids0) | set(ids1) | set(ids2)
        offset = 9
        guard = 0
        last = p2
        while last.get("has_more") and guard < 10:
            last = page(offset)
            walked |= {it["run_id"] for it in last["items"]}
            offset += 3
            guard += 1
        assert set(made).issubset(walked), "pagination must reach every item"

        # `offset` is a KNOWN param — the unknown-param refusal ignores it.
        ok = client.get("/api/gateway/runs", headers=headers, params={"limit": 1, "offset": 0})
        assert ok.status_code == 200, ok.text


def test_runs_search_reaches_matches_behind_filtered_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The truncation lie (design adversary, 2026-08-19): the store scan is
    bounded, so a listing whose matches sit BEHIND many filtered-out rows
    used to return a short page with has_more=False — the console drew no
    pager and the rest was unreachable. The scan now escalates until the
    page fills or the store is exhausted."""
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-hay", flow_id="root")
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-needle", flow_id="root")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        # One needle FIRST (oldest), then a wall of hay after it: any bounded
        # newest-first scan that stops early would miss the needle.
        needle = client.post(
            "/api/gateway/runs/start",
            headers=headers,
            json={"bundle_id": "bundle-needle", "flow_id": "root", "input_data": {}},
        )
        assert needle.status_code == 200, needle.text
        needle_id = needle.json()["run_id"]
        for _ in range(25):
            hay = client.post(
                "/api/gateway/runs/start",
                headers=headers,
                json={"bundle_id": "bundle-hay", "flow_id": "root", "input_data": {}},
            )
            assert hay.status_code == 200, hay.text

        found = client.get(
            "/api/gateway/runs",
            headers=headers,
            params={"limit": 5, "query": "bundle-needle", "include_ledger_len": "false"},
        )
        assert found.status_code == 200, found.text
        ids = [it["run_id"] for it in found.json()["items"]]
        assert needle_id in ids, "search must reach a match sitting behind newer non-matching runs"
        assert found.json()["has_more"] is False, "a single-match search ends honestly"

        # The query also matches on run_id and composes with a status filter.
        by_id = client.get(
            "/api/gateway/runs",
            headers=headers,
            params={"limit": 5, "query": needle_id[:8], "include_ledger_len": "false"},
        )
        assert by_id.status_code == 200, by_id.text
        assert needle_id in [it["run_id"] for it in by_id.json()["items"]]

        # A query nobody matches is an honest empty page, never a short lie.
        none = client.get(
            "/api/gateway/runs",
            headers=headers,
            params={"limit": 5, "query": "zzz-no-such-run", "include_ledger_len": "false"},
        )
        assert none.status_code == 200, none.text
        assert none.json()["items"] == [] and none.json()["has_more"] is False
