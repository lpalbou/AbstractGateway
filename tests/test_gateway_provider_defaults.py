from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic


def _clear_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("ABSTRACTGATEWAY_PROVIDER", "ABSTRACTGATEWAY_MODEL", "ABSTRACTFLOW_PROVIDER", "ABSTRACTFLOW_MODEL"):
        monkeypatch.delenv(key, raising=False)


def _write_min_bundle(*, bundles_dir: Path) -> tuple[str, str]:
    bundles_dir.mkdir(parents=True, exist_ok=True)
    bundle_id = "bundle-provider-defaults"
    flow_id = "root"
    flow = {
        "id": flow_id,
        "name": "root",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "start",
                "type": "on_flow_start",
                "position": {"x": 32, "y": 128},
                "data": {"nodeType": "on_flow_start", "inputs": [], "outputs": [{"id": "exec-out", "type": "execution"}]},
            },
            {
                "id": "end",
                "type": "on_flow_end",
                "position": {"x": 288, "y": 128},
                "data": {"nodeType": "on_flow_end", "inputs": [{"id": "exec-in", "type": "execution"}], "outputs": []},
            },
        ],
        "edges": [{"id": "e", "source": "start", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"}],
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
    return bundle_id, flow_id


def test_provider_model_resolver_prefers_request_then_gateway_env_then_flow_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import provider_defaults

    _clear_provider_env(monkeypatch)

    req = provider_defaults.resolve_gateway_provider_model(provider="OLLAMA", model="llama3", purpose="test").require()
    assert req == ("ollama", "llama3")

    monkeypatch.setenv("ABSTRACTGATEWAY_PROVIDER", "lmstudio")
    monkeypatch.setenv("ABSTRACTGATEWAY_MODEL", "qwen-local")
    env = provider_defaults.resolve_gateway_provider_model(purpose="test")
    assert (env.provider, env.model, env.source) == ("lmstudio", "qwen-local", "gateway_env")

    _clear_provider_env(monkeypatch)
    flow = provider_defaults.resolve_gateway_provider_model(
        flow_defaults=("ollama", "qwen3:8b"),
        purpose="test",
    )
    assert (flow.provider, flow.model, flow.source) == ("ollama", "qwen3:8b", "flow_defaults")


def test_provider_model_resolver_reports_clear_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import provider_defaults

    _clear_provider_env(monkeypatch)

    resolved = provider_defaults.resolve_gateway_provider_model(purpose="summary helper")
    assert resolved.provider is None
    assert resolved.model is None
    assert "No provider/model is configured for summary helper" in str(resolved.error)


def test_summary_helper_rejects_missing_provider_model_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.provider_defaults as provider_defaults

    _clear_provider_env(monkeypatch)

    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    bundle_id, flow_id = _write_min_bundle(bundles_dir=bundles_dir)
    token = "t"

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        started = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": bundle_id, "flow_id": flow_id, "input_data": {}},
            headers=headers,
        )
        assert started.status_code == 200, started.text
        run_id = started.json()["run_id"]

        resp = client.post(f"/api/gateway/runs/{run_id}/summary", json={}, headers=headers)
        assert resp.status_code == 400, resp.text
        assert "No provider/model is configured for run summary generation" in resp.text


def test_discovery_providers_reports_default_error_without_hardcoded_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import abstractgateway.provider_defaults as provider_defaults
    from abstractgateway.routes import gateway as gateway_routes

    _clear_provider_env(monkeypatch)

    class StubDiscoveryFacade:
        def list_providers(self, *, include_models: bool = False, **_kwargs):
            assert include_models is False
            return {"items": []}

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_discovery_facade", lambda: (StubDiscoveryFacade(), None))

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    app = FastAPI()
    app.include_router(gateway_routes.router, prefix="/api")
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    with client:
        resp = client.get("/api/gateway/discovery/providers", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["default_provider"] is None
    assert body["default_model"] is None
    assert "No provider/model is configured" in body["default_error"]
