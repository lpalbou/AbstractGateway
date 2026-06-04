from __future__ import annotations

import json
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient


def _write_min_bundle(*, bundles_dir: Path, bundle_id: str = "bundle-thinking", flow_id: str = "root") -> tuple[str, str]:
    bundles_dir.mkdir(parents=True, exist_ok=True)
    flow = {
        "id": flow_id,
        "name": "thinking",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "start",
                "type": "on_flow_start",
                "position": {"x": 0, "y": 0},
                "data": {"nodeType": "on_flow_start", "label": "On Flow Start", "inputs": [], "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]},
            },
            {
                "id": "end",
                "type": "on_flow_end",
                "position": {"x": 200, "y": 0},
                "data": {"nodeType": "on_flow_end", "label": "On Flow End", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []},
            },
        ],
        "edges": [{"id": "e1", "source": "start", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"}],
        "entryNode": "start",
    }
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-06-03T00:00:00+00:00",
        "entrypoints": [{"flow_id": flow_id, "name": "thinking", "description": "", "interfaces": []}],
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }
    with zipfile.ZipFile(bundles_dir / f"{bundle_id}.flow", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr(f"flows/{flow_id}.json", json.dumps(flow, indent=2))
    return bundle_id, flow_id


def test_gateway_runs_start_maps_top_level_thinking_to_runtime_namespace(tmp_path: Path, monkeypatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    bundle_id, flow_id = _write_min_bundle(bundles_dir=bundles_dir)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    from abstractgateway.app import app
    from abstractgateway.service import get_gateway_service

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        resp = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": bundle_id, "flow_id": flow_id, "thinking": "high", "input_data": {}},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        run_id = resp.json()["run_id"]

        state = get_gateway_service().host.runtime.get_state(run_id)
        assert state.vars["_runtime"]["thinking"] == "high"


def test_gateway_capability_contract_advertises_thinking_control(tmp_path: Path, monkeypatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        resp = client.get("/api/gateway/discovery/capabilities", headers=headers)
        assert resp.status_code == 200, resp.text
        start = resp.json()["capabilities"]["contracts"]["common"]["runs"]["start"]
        assert start["thinking_control"]["field"] == "thinking"
        assert start["thinking_control"]["runtime_field"] == "_runtime.thinking"
