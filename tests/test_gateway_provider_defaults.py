from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic


def _clear_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "ABSTRACTGATEWAY_PROVIDER",
        "ABSTRACTGATEWAY_MODEL",
        "ABSTRACTFLOW_PROVIDER",
        "ABSTRACTFLOW_MODEL",
        "ABSTRACTCORE_SERVER_BASE_URL",
        "ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_AUTH_TOKEN",
        "ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_API_KEY",
        "ABSTRACTCORE_AUTH_TOKEN",
        "ABSTRACTCORE_SERVER_API_KEY",
    ):
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


def test_provider_model_resolver_prefers_request_then_flow_defaults_then_capability_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from abstractcore.config.manager import ConfigurationManager
    from abstractgateway import provider_defaults

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    req = provider_defaults.resolve_gateway_provider_model(provider="OLLAMA", model="llama3", purpose="test").require()
    assert req == ("ollama", "llama3")

    flow = provider_defaults.resolve_gateway_provider_model(
        flow_defaults=("ollama", "qwen3:8b"),
        purpose="test",
    )
    assert (flow.provider, flow.model, flow.source) == ("ollama", "qwen3:8b", "flow_defaults")

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path / "home-route"))
    manager = ConfigurationManager()
    assert manager.set_capability_default("output.text", provider="lmstudio", model="qwen-local")

    route = provider_defaults.resolve_gateway_provider_model(purpose="test")
    assert (route.provider, route.model, route.source) == (
        "lmstudio",
        "qwen-local",
        "abstractcore.capability_defaults",
    )


def test_provider_model_resolver_does_not_cross_fill_partial_pairs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from abstractcore.config.manager import ConfigurationManager
    from abstractgateway import provider_defaults

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))

    manager = ConfigurationManager()
    assert manager.set_capability_default("output.text", provider="ollama", model="route-model")

    provider_only = provider_defaults.resolve_gateway_provider_model(
        provider="openai",
        flow_defaults=("lmstudio", "flow-model"),
        purpose="test",
    )
    assert provider_only.provider == "openai"
    assert provider_only.model is None
    assert "No provider/model is configured for test" in str(provider_only.error)

    model_only = provider_defaults.resolve_gateway_provider_model(
        model="request-model",
        flow_defaults=("lmstudio", "flow-model"),
        purpose="test",
    )
    assert model_only.provider is None
    assert model_only.model == "request-model"
    assert "No provider/model is configured for test" in str(model_only.error)


def test_provider_model_resolver_ignores_partial_capability_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from abstractcore.config.manager import ConfigurationManager
    from abstractgateway import provider_defaults

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))

    manager = ConfigurationManager()
    assert manager.set_capability_default("output.text", provider="lmstudio", model=None)

    resolved = provider_defaults.resolve_gateway_provider_model(purpose="test")

    assert resolved.provider is None
    assert resolved.model is None
    assert "No provider/model is configured for test" in str(resolved.error)


def test_core_server_token_accepts_core_auth_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.capability_defaults import core_server_token

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("ABSTRACTCORE_AUTH_TOKEN", "core-token")

    assert core_server_token() == "core-token"


def test_provider_model_resolver_reports_clear_config_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from abstractgateway import provider_defaults

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))

    resolved = provider_defaults.resolve_gateway_provider_model(purpose="summary helper")
    assert resolved.provider is None
    assert resolved.model is None
    assert "No provider/model is configured for summary helper" in str(resolved.error)


def test_provider_model_resolver_uses_execution_host_capability_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from abstractcore.config.manager import ConfigurationManager
    from abstractgateway import provider_defaults

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))

    manager = ConfigurationManager()
    assert manager.set_capability_default("output.text", provider="lmstudio", model="qwen-local")

    resolved = provider_defaults.resolve_gateway_provider_model(purpose="test")

    assert (resolved.provider, resolved.model, resolved.source) == (
        "lmstudio",
        "qwen-local",
        "abstractcore.capability_defaults",
    )


def test_provider_model_resolver_falls_back_to_abstractcore_capability_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from abstractcore.config.manager import ConfigurationManager
    from abstractgateway import provider_defaults

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))

    manager = ConfigurationManager()
    assert manager.set_capability_default("output.text", provider="ollama", model="qwen3:8b")

    resolved = provider_defaults.resolve_gateway_provider_model(purpose="test")

    assert (resolved.provider, resolved.model, resolved.source) == (
        "ollama",
        "qwen3:8b",
        "abstractcore.capability_defaults",
    )


def test_provider_model_resolver_does_not_use_gateway_host_core_config_when_remote_core_is_configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from abstractcore.config.manager import ConfigurationManager
    import abstractgateway.capability_defaults as capability_defaults
    from abstractgateway import provider_defaults

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ABSTRACTCORE_SERVER_BASE_URL", "http://core.example/v1")

    manager = ConfigurationManager()
    assert manager.set_capability_default("output.text", provider="lmstudio", model="local-gateway-host-model")

    def fail_urlopen(*_args, **_kwargs):
        raise capability_defaults.urllib.error.URLError("remote core unavailable")

    monkeypatch.setattr(capability_defaults.urllib.request, "urlopen", fail_urlopen)

    resolved = provider_defaults.resolve_gateway_provider_model(purpose="test")

    assert resolved.provider is None
    assert resolved.model is None
    assert "No provider/model is configured for test" in str(resolved.error)


def test_summary_helper_rejects_missing_provider_model_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.provider_defaults as provider_defaults

    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("HOME", str(tmp_path))

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
    monkeypatch.setenv("HOME", str(tmp_path))

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
