from __future__ import annotations

import json
from pathlib import Path
import zipfile

import pytest
from fastapi.testclient import TestClient


def _write_test_bundle(*, bundles_dir: Path) -> tuple[str, str]:
    bundles_dir.mkdir(parents=True, exist_ok=True)
    bundle_id = "bundle-artifacts"
    flow_id = "flow"

    flow = {
        "id": flow_id,
        "name": "test",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "node-1",
                "type": "on_flow_start",
                "position": {"x": 32.0, "y": 224.0},
                "data": {"nodeType": "on_flow_start", "label": "On Flow Start", "inputs": [], "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]},
            },
            {
                "id": "node-2",
                "type": "ask_user",
                "position": {"x": 288.0, "y": 224.0},
                "data": {
                    "nodeType": "ask_user",
                    "label": "Ask User",
                    "inputs": [{"id": "exec-in", "label": "", "type": "execution"}],
                    "outputs": [
                        {"id": "exec-out", "label": "", "type": "execution"},
                        {"id": "response", "label": "response", "type": "string"},
                    ],
                    "pinDefaults": {"prompt": "hello"},
                },
            },
        ],
        "edges": [
            {
                "id": "edge-1",
                "source": "node-1",
                "sourceHandle": "exec-out",
                "target": "node-2",
                "targetHandle": "exec-in",
            }
        ],
        "entryNode": "node-1",
    }
    flow_bytes = json.dumps(flow, indent=2).encode("utf-8")

    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-01-12T00:00:00+00:00",
        "entrypoints": [{"flow_id": flow_id, "name": "test", "description": "", "interfaces": []}],
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }
    bundle_path = bundles_dir / f"{bundle_id}.flow"
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr(f"flows/{flow_id}.json", flow_bytes)

    return bundle_id, flow_id


def test_gateway_artifacts_api_list_metadata_and_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    bundle_id, flow_id = _write_test_bundle(bundles_dir=bundles_dir)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    from abstractgateway.app import app
    from abstractruntime.storage.artifacts import FileArtifactStore

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": bundle_id, "flow_id": flow_id, "input_data": {}},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]

        store = FileArtifactStore(runtime_dir)
        meta = store.store(b"hello", content_type="text/plain", run_id=run_id)
        artifact_id = meta.artifact_id

        resp = client.get(f"/api/gateway/runs/{run_id}/artifacts", headers=headers)
        assert resp.status_code == 200, resp.text
        items = resp.json().get("items")
        assert isinstance(items, list)
        assert any(it.get("artifact_id") == artifact_id for it in items)

        resp = client.get(f"/api/gateway/runs/{run_id}/artifacts/{artifact_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        meta_out = resp.json()
        assert meta_out.get("artifact_id") == artifact_id
        assert meta_out.get("run_id") == run_id
        assert meta_out.get("content_type") == "text/plain"

        resp = client.get(f"/api/gateway/runs/{run_id}/artifacts/{artifact_id}/content", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.content == b"hello"

        other = store.store(b"world", content_type="text/plain", run_id="other-run")
        resp = client.get(f"/api/gateway/runs/{run_id}/artifacts/{other.artifact_id}", headers=headers)
        assert resp.status_code == 404
