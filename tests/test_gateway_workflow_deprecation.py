from __future__ import annotations

import json
from pathlib import Path
import zipfile

import pytest
from fastapi.testclient import TestClient


def _make_min_flow(flow_id: str) -> dict:
    fid = str(flow_id or "").strip() or "root"
    return {
        "id": fid,
        "name": fid,
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "start",
                "type": "on_flow_start",
                "position": {"x": 0.0, "y": 0.0},
                "data": {"nodeType": "on_flow_start", "label": "On Flow Start", "inputs": [], "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]},
            },
            {
                "id": "end",
                "type": "on_flow_end",
                "position": {"x": 10.0, "y": 0.0},
                "data": {"nodeType": "on_flow_end", "label": "On Flow End", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []},
            },
        ],
        "edges": [
            {"id": "e1", "source": "start", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in", "animated": True},
        ],
        "entryNode": "start",
    }


def _write_bundle(path: Path, *, bundle_id: str, bundle_version: str, flow_id: str) -> None:
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": bundle_version,
        "created_at": "2026-01-24T00:00:00Z",
        "entrypoints": [{"flow_id": flow_id, "name": flow_id, "description": "", "interfaces": []}],
        "default_entrypoint": flow_id,
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "metadata": {"test": True},
    }
    flow = _make_min_flow(flow_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr(f"flows/{flow_id}.json", json.dumps(flow, ensure_ascii=False, indent=2))


@pytest.mark.integration
def test_gateway_workflow_deprecate_filters_discovery_and_blocks_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir(parents=True, exist_ok=True)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")

    bundle_path = tmp_path / "demo@0.0.1.flow"
    _write_bundle(bundle_path, bundle_id="demo", bundle_version="0.0.1", flow_id="root")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        up = client.post(
            "/api/gateway/bundles/upload",
            headers=headers,
            data={"overwrite": "false", "reload": "true"},
            files={"file": (bundle_path.name, bundle_path.read_bytes(), "application/octet-stream")},
        )
        assert up.status_code == 200, up.text

        r0 = client.get("/api/gateway/bundles", headers=headers)
        assert r0.status_code == 200
        assert any(it.get("bundle_id") == "demo" for it in (r0.json().get("items") or []))

        dep = client.post("/api/gateway/bundles/demo/deprecate", headers=headers, json={"flow_id": "root", "reason": "retired"})
        assert dep.status_code == 200, dep.text

        r1 = client.get("/api/gateway/bundles", headers=headers)
        assert r1.status_code == 200
        assert not any(it.get("bundle_id") == "demo" for it in (r1.json().get("items") or []))

        r1i = client.get("/api/gateway/bundles?include_deprecated=true", headers=headers)
        assert r1i.status_code == 200
        items = r1i.json().get("items") or []
        demo = next((it for it in items if it.get("bundle_id") == "demo"), None)
        assert demo is not None
        eps = demo.get("entrypoints") or []
        assert eps and eps[0].get("deprecated") is True

        start = client.post("/api/gateway/runs/start", headers=headers, json={"bundle_id": "demo", "flow_id": "root", "input_data": {}})
        assert start.status_code == 409

        sched = client.post(
            "/api/gateway/runs/schedule",
            headers=headers,
            json={"bundle_id": "demo", "flow_id": "root", "input_data": {"prompt": "x"}, "start_at": "now", "interval": "1h"},
        )
        assert sched.status_code == 409

        undep = client.post("/api/gateway/bundles/demo/undeprecate", headers=headers, json={"flow_id": "root"})
        assert undep.status_code == 200, undep.text
        assert undep.json().get("removed") is True

        r2 = client.get("/api/gateway/bundles", headers=headers)
        assert r2.status_code == 200
        assert any(it.get("bundle_id") == "demo" for it in (r2.json().get("items") or []))

        start2 = client.post("/api/gateway/runs/start", headers=headers, json={"bundle_id": "demo", "flow_id": "root", "input_data": {}})
        assert start2.status_code == 200, start2.text
        assert isinstance(start2.json().get("run_id"), str) and start2.json().get("run_id")

