from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


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
                "data": {
                    "nodeType": "on_flow_start",
                    "label": "On Flow Start",
                    "inputs": [],
                    "outputs": [{"id": "exec-out", "label": "", "type": "execution"}],
                },
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
        "created_at": "2026-01-16T00:00:00+00:00",
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


def test_gateway_attachments_upload_creates_session_scoped_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-attachments-upload", flow_id="root")

    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKSPACE_DIR", str(ws))

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    payload = b"hello\nworld\n"
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/attachments/upload",
            data={"session_id": "s1"},
            files={"file": ("notes.txt", payload, "text/plain")},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("ok") is True
        assert body.get("run_id") == "session_memory_s1"

        attachment = body.get("attachment") or {}
        assert isinstance(attachment, dict)
        artifact_id = attachment.get("$artifact")
        assert isinstance(artifact_id, str) and artifact_id.strip()
        assert attachment.get("target") == "client"
        assert attachment.get("source_path") == "client:notes.txt"
        sha256 = attachment.get("sha256")
        assert isinstance(sha256, str) and sha256.strip()
        assert sha256 == hashlib.sha256(payload).hexdigest()

        r2 = client.get("/api/gateway/runs/session_memory_s1/artifacts", headers=headers)
        assert r2.status_code == 200, r2.text
        items = r2.json().get("items") or []
        found = next((it for it in items if isinstance(it, dict) and it.get("artifact_id") == artifact_id), None)
        assert isinstance(found, dict)
        tags = found.get("tags") or {}
        assert isinstance(tags, dict)
        assert tags.get("target") == "client"
        assert tags.get("path") == "client:notes.txt"

        r3 = client.get(f"/api/gateway/runs/session_memory_s1/artifacts/{artifact_id}/content", headers=headers)
        assert r3.status_code == 200, r3.text
        assert b"hello" in r3.content
