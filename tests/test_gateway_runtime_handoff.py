from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic


def _write_bundle(*, bundles_dir: Path, bundle_id: str, flow_id: str) -> None:
    bundles_dir.mkdir(parents=True, exist_ok=True)
    flow = {
        "id": flow_id,
        "name": "handoff",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "start",
                "type": "on_flow_start",
                "position": {"x": 32.0, "y": 128.0},
                "data": {
                    "nodeType": "on_flow_start",
                    "label": "On Flow Start",
                    "inputs": [],
                    "outputs": [{"id": "exec-out", "label": "", "type": "execution"}],
                },
            },
            {
                "id": "end",
                "type": "on_flow_end",
                "position": {"x": 288.0, "y": 128.0},
                "data": {
                    "nodeType": "on_flow_end",
                    "label": "On Flow End",
                    "inputs": [{"id": "exec-in", "label": "", "type": "execution"}],
                    "outputs": [],
                },
            },
        ],
        "edges": [
            {
                "id": "e",
                "source": "start",
                "sourceHandle": "exec-out",
                "target": "end",
                "targetHandle": "exec-in",
            }
        ],
        "entryNode": "start",
    }
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-05-08T00:00:00+00:00",
        "entrypoints": [{"flow_id": flow_id, "name": "root", "description": "", "interfaces": []}],
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }
    with zipfile.ZipFile(bundles_dir / f"{bundle_id}.flow", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr(f"flows/{flow_id}.json", json.dumps(flow))


def test_gateway_translates_env_defaults_into_runtime_namespace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_bundle(bundles_dir=bundles_dir, bundle_id="handoff", flow_id="root")
    token = "t"

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_PROMPT_CACHE", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_MAX_ATTACHMENT_BYTES", "12345")

    from abstractgateway.app import app
    from abstractgateway.service import get_gateway_service

    with TestClient(app) as client:
        resp = client.post(
            "/api/gateway/runs/start",
            headers={"Authorization": f"Bearer {token}"},
            json={"bundle_id": "handoff", "flow_id": "root", "input_data": {}},
        )
        assert resp.status_code == 200, resp.text
        run_id = resp.json()["run_id"]
        run = get_gateway_service().host.run_store.load(run_id)

    assert run is not None
    runtime_ns = run.vars.get("_runtime")
    assert runtime_ns["prompt_cache"] == {"enabled": True, "version": 1}
    assert runtime_ns["max_attachment_bytes"] == 12345
    assert runtime_ns["workflow_bundles_dir"] == str(bundles_dir.resolve())
