"""OpenAPI docs metadata tests for gateway bearer auth."""

from __future__ import annotations

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
        "created_at": "2026-02-21T00:00:00+00:00",
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


def _make_client(*, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-openapi", flow_id="root")

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")

    from abstractgateway.app import app

    return TestClient(app)


def test_openapi_advertises_gateway_bearer_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(tmp_path=tmp_path, monkeypatch=monkeypatch)
    with client:
        resp = client.get("/openapi.json")
        assert resp.status_code == 200, resp.text

        spec = resp.json()
        schemes = (spec.get("components") or {}).get("securitySchemes") or {}
        assert "gatewayBearerAuth" in schemes
        scheme = schemes["gatewayBearerAuth"]
        assert scheme.get("type") == "http"
        assert scheme.get("scheme") == "bearer"

        paths = spec.get("paths") or {}
        gateway_paths = {path: ops for path, ops in paths.items() if str(path).startswith("/api/gateway")}
        assert gateway_paths, "Expected /api/gateway paths in the OpenAPI schema"

        for path, ops in gateway_paths.items():
            if not isinstance(ops, dict):
                continue
            for method, op_schema in ops.items():
                if str(method).lower() not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                    continue
                if not isinstance(op_schema, dict):
                    continue
                security = op_schema.get("security") or []
                assert any(
                    isinstance(item, dict) and "gatewayBearerAuth" in item for item in security
                ), f"Missing bearer auth for {path} {method}"

        health_ops = paths.get("/api/health") or {}
        if isinstance(health_ops, dict):
            for method, op_schema in health_ops.items():
                if str(method).lower() not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                    continue
                if not isinstance(op_schema, dict):
                    continue
                security = op_schema.get("security") or []
                assert not any(
                    isinstance(item, dict) and "gatewayBearerAuth" in item for item in security
                ), "Health endpoint should not require bearer auth in OpenAPI"
