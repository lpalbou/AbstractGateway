from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.integration


def _wait_until(predicate, *, timeout_s: float = 8.0, poll_s: float = 0.05):
    end = time.time() + timeout_s
    while time.time() < end:
        if predicate():
            return
        time.sleep(poll_s)
    raise AssertionError("timeout waiting for condition")


def _write_min_bundle(*, bundles_dir: Path, bundle_id: str, flow_id: str) -> None:
    bundles_dir.mkdir(parents=True, exist_ok=True)

    flow = {
        "id": flow_id,
        "name": "minimal",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "node-1",
                "type": "on_flow_start",
                "position": {"x": 32.0, "y": 128.0},
                "data": {"nodeType": "on_flow_start", "label": "On Flow Start", "inputs": [], "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]},
            },
            {
                "id": "node-2",
                "type": "on_flow_end",
                "position": {"x": 288.0, "y": 128.0},
                "data": {"nodeType": "on_flow_end", "label": "On Flow End", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []},
            },
        ],
        "edges": [{"id": "e1", "source": "node-1", "sourceHandle": "exec-out", "target": "node-2", "targetHandle": "exec-in"}],
        "entryNode": "node-1",
    }

    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-01-09T00:00:00+00:00",
        "entrypoints": [{"flow_id": flow_id, "name": "root", "description": "", "interfaces": []}],
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }

    bundle_path = bundles_dir / f"{bundle_id}.flow"
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr(f"flows/{flow_id}.json", json.dumps(flow, indent=2))


def test_history_bundle_endpoint_includes_snapshot_and_replays_after_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    db_path = runtime_dir / "gateway.sqlite3"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-history", flow_id="root")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_DB_PATH", str(db_path))
    monkeypatch.setenv("ABSTRACTGATEWAY_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    session_id = "sess_history_1"

    def _start_and_wait(client: TestClient) -> str:
        start = client.post(
            "/api/gateway/runs/start",
            headers=headers,
            json={
                "bundle_id": "bundle-history",
                "flow_id": "root",
                "session_id": session_id,
                "input_data": {"prompt": "hello", "context": {"messages": [{"role": "user", "content": "hello"}], "attachments": []}},
            },
        )
        assert start.status_code == 200, start.text
        rid = start.json()["run_id"]

        def _is_completed() -> bool:
            rr = client.get(f"/api/gateway/runs/{rid}", headers=headers)
            assert rr.status_code == 200, rr.text
            return rr.json().get("status") == "completed"

        _wait_until(_is_completed, timeout_s=8.0, poll_s=0.05)
        return rid

    with TestClient(app) as client:
        run_id = _start_and_wait(client)
        r = client.get(
            f"/api/gateway/runs/{run_id}/history_bundle",
            headers=headers,
            params={"include_subruns": "false", "include_session": "true", "session_turn_limit": 50, "ledger_mode": "tail", "ledger_max_items": 50},
        )
        assert r.status_code == 200, r.text
        bundle = r.json()
        assert bundle.get("version") == 1
        assert bundle.get("run", {}).get("run_id") == run_id

        ws = bundle.get("workflow_snapshot")
        assert isinstance(ws, dict)
        artifact_id = str(ws.get("artifact_id") or "").strip()
        assert artifact_id
        assert str(ws.get("sha256") or "").strip()

        session = bundle.get("session") or {}
        assert session.get("session_id") == session_id
        turns = session.get("turns") or []
        assert any(t.get("run_id") == run_id and t.get("kind") == "chat" for t in turns)

        arts = client.get(f"/api/gateway/runs/{run_id}/artifacts", headers=headers)
        assert arts.status_code == 200, arts.text
        items = arts.json().get("items") or []
        assert any(str(i.get("artifact_id") or "") == artifact_id for i in items)

    # Restart simulation: new service process (same data_dir/db) should serve the same snapshot ref.
    with TestClient(app) as client2:
        r2 = client2.get(
            f"/api/gateway/runs/{run_id}/history_bundle",
            headers=headers,
            params={"include_subruns": "false", "include_session": "true", "session_turn_limit": 50, "ledger_mode": "tail", "ledger_max_items": 50},
        )
        assert r2.status_code == 200, r2.text
        bundle2 = r2.json()
        ws2 = bundle2.get("workflow_snapshot")
        assert isinstance(ws2, dict)
        assert str(ws2.get("artifact_id") or "").strip() == artifact_id
