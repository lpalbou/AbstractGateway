from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _write_min_bundle(*, bundles_dir: Path) -> None:
    bundles_dir.mkdir(parents=True, exist_ok=True)
    flow = {
        "id": "root",
        "nodes": [
            {
                "id": "start",
                "type": "on_flow_start",
                "data": {"nodeType": "on_flow_start", "outputs": [{"id": "exec-out", "type": "execution"}]},
            },
            {
                "id": "end",
                "type": "on_flow_end",
                "data": {"nodeType": "on_flow_end", "inputs": [{"id": "exec-in", "type": "execution"}]},
            },
        ],
        "edges": [{"id": "e", "source": "start", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"}],
        "entryNode": "start",
    }
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": "basic-agent",
        "bundle_version": "0.0.0",
        "entrypoints": [{"flow_id": "root", "name": "root", "description": "", "interfaces": []}],
        "flows": {"root": "flows/root.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }
    with zipfile.ZipFile(bundles_dir / "basic-agent.flow", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("flows/root.json", json.dumps(flow))


def _app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, user_auth: bool = False) -> TestClient:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "admin-token")
    if user_auth:
        monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    else:
        monkeypatch.delenv("ABSTRACTGATEWAY_USER_AUTH", raising=False)
        monkeypatch.delenv("ABSTRACTGATEWAY_MULTI_USER", raising=False)

    if user_auth:
        from abstractgateway.routes import gateway_router
        from abstractgateway.security import GatewaySecurityMiddleware, load_gateway_auth_policy_from_env

        app = FastAPI()
        app.add_middleware(GatewaySecurityMiddleware, policy=load_gateway_auth_policy_from_env())
        app.include_router(gateway_router, prefix="/api")
        return TestClient(app)

    from abstractgateway.app import app

    return TestClient(app)


def test_endpoint_profile_crud_surfaces_virtual_provider_without_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class StubDiscoveryFacade:
        def list_providers(self, *, include_models: bool = False, **kwargs: Any) -> dict[str, Any]:
            calls.append({"method": "list_providers", "include_models": include_models, **kwargs})
            return {"items": [{"name": "lmstudio"}], "default_provider": "lmstudio", "default_model": "local-model"}

        def list_provider_models(self, provider_name: str, **kwargs: Any) -> dict[str, Any]:
            calls.append({"method": "list_provider_models", "provider_name": provider_name, **kwargs})
            return {"provider": provider_name, "models": ["remote-model"]}

    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_discovery_facade", lambda: (StubDiscoveryFacade(), None))

    headers = {"Authorization": "Bearer admin-token"}
    with _app_client(tmp_path, monkeypatch) as client:
        created = client.post(
            "/api/gateway/config/provider-endpoint-profiles",
            headers=headers,
            json={
                "id": "office-vllm",
                "display_name": "Office vLLM",
                "description": "Internal OpenAI-compatible endpoint for office workflows.",
                "provider_family": "openai-compatible",
                "base_url": "https://llm.example.test/v1",
                "api_key": "secret-key",
                "scope": "gateway",
                "capabilities": ["text", "embeddings"],
                "allowed_models": ["qwen/qwen3"],
            },
        )
        assert created.status_code == 200, created.text
        profile = created.json()["profile"]
        assert profile["virtual_provider"] == "endpoint:office-vllm"
        assert profile["description"] == "Internal OpenAI-compatible endpoint for office workflows."
        assert profile["api_key_set"] is True
        assert "api_key" not in profile
        assert "secret-key" not in created.text

        providers = client.get("/api/gateway/discovery/providers?include_models=false", headers=headers)
        assert providers.status_code == 200, providers.text
        endpoint_item = next(item for item in providers.json()["items"] if item["name"] == "endpoint:office-vllm")
        assert endpoint_item["display_name"] == "Office vLLM"
        assert endpoint_item["provider_family"] == "openai-compatible"
        assert endpoint_item["provider_endpoint_profile"]["api_key_set"] is True
        assert "secret-key" not in providers.text

        models = client.get("/api/gateway/discovery/providers/endpoint%3Aoffice-vllm/models", headers=headers)
        assert models.status_code == 200, models.text
        body = models.json()
        assert body["provider"] == "endpoint:office-vllm"
        assert body["routed_provider"] == "openai-compatible"
        assert body["models"] == ["qwen/qwen3"]
        assert "secret-key" not in models.text

    assert calls == [{"method": "list_providers", "include_models": False, "provider_api_key": None}]


def test_endpoint_profile_model_discovery_uses_profile_url_and_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class StubDiscoveryFacade:
        def list_providers(self, *, include_models: bool = False, **kwargs: Any) -> dict[str, Any]:
            return {"items": []}

        def list_provider_models(self, provider_name: str, **kwargs: Any) -> dict[str, Any]:
            calls.append({"provider_name": provider_name, **kwargs})
            return {"provider": provider_name, "models": ["m2", "m1"]}

    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_discovery_facade", lambda: (StubDiscoveryFacade(), None))

    headers = {"Authorization": "Bearer admin-token"}
    with _app_client(tmp_path, monkeypatch) as client:
        created = client.post(
            "/api/gateway/config/provider-endpoint-profiles",
            headers=headers,
            json={
                "id": "remote",
                "display_name": "Remote Endpoint",
                "description": "Discovers models from the remote endpoint.",
                "provider_family": "openai-compatible",
                "base_url": "https://remote.example.test/v1",
                "api_key": "profile-key",
                "scope": "gateway",
                "capabilities": ["text"],
                "allowed_models": [],
            },
        )
        assert created.status_code == 200, created.text

        models = client.get("/api/gateway/discovery/providers/endpoint%3Aremote/models", headers=headers)
        assert models.status_code == 200, models.text
        assert models.json()["models"] == ["m1", "m2"]
        assert "profile-key" not in models.text

    assert calls == [
        {
            "provider_name": "openai-compatible",
            "base_url": "https://remote.example.test/v1",
            "provider_api_key": "profile-key",
            "timeout_s": 30.0,
        }
    ]


def test_endpoint_profile_console_model_preview_uses_entered_endpoint_without_echoing_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class StubDiscoveryFacade:
        def list_provider_models(self, provider_name: str, **kwargs: Any) -> dict[str, Any]:
            calls.append({"provider_name": provider_name, **kwargs})
            return {"provider": provider_name, "models": ["zeta", "alpha", "alpha"], "source": "stub"}

    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_discovery_facade", lambda: (StubDiscoveryFacade(), None))

    headers = {"Authorization": "Bearer admin-token"}
    with _app_client(tmp_path, monkeypatch) as client:
        preview = client.post(
            "/api/gateway/config/provider-endpoint-profiles/discover-models",
            headers=headers,
            json={
                "provider_family": "openai-compatible",
                "base_url": "https://preview.example.test/v1",
                "api_key": "preview-key",
            },
        )

    assert preview.status_code == 200, preview.text
    assert preview.json()["models"] == ["alpha", "zeta"]
    assert preview.json()["base_url_configured"] is True
    assert preview.json()["api_key_set"] is True
    assert "preview-key" not in preview.text
    assert calls == [
        {
            "provider_name": "openai-compatible",
            "base_url": "https://preview.example.test/v1",
            "provider_api_key": "preview-key",
            "timeout_s": 30.0,
        }
    ]


def test_endpoint_profile_console_model_preview_can_use_saved_profile_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class StubDiscoveryFacade:
        def list_provider_models(self, provider_name: str, **kwargs: Any) -> dict[str, Any]:
            calls.append({"provider_name": provider_name, **kwargs})
            return {"provider": provider_name, "models": ["saved-model"]}

    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_discovery_facade", lambda: (StubDiscoveryFacade(), None))

    headers = {"Authorization": "Bearer admin-token"}
    with _app_client(tmp_path, monkeypatch) as client:
        created = client.post(
            "/api/gateway/config/provider-endpoint-profiles",
            headers=headers,
            json={
                "id": "saved",
                "display_name": "Saved Endpoint",
                "provider_family": "openai-compatible",
                "base_url": "https://saved.example.test/v1",
                "api_key": "saved-key",
                "scope": "gateway",
            },
        )
        assert created.status_code == 200, created.text

        preview = client.post(
            "/api/gateway/config/provider-endpoint-profiles/discover-models",
            headers=headers,
            json={"profile_id": "saved"},
        )

    assert preview.status_code == 200, preview.text
    assert preview.json()["models"] == ["saved-model"]
    assert "saved-key" not in preview.text
    assert calls == [
        {
            "provider_name": "openai-compatible",
            "base_url": "https://saved.example.test/v1",
            "provider_api_key": "saved-key",
            "timeout_s": 30.0,
        }
    ]


def test_non_admin_cannot_create_gateway_scoped_endpoint_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    from abstractgateway.users import GatewayUserRegistry

    _record, token = GatewayUserRegistry().create_user(user_id="alice", roles=["user"])
    with _app_client(tmp_path, monkeypatch, user_auth=True) as client:
        denied = client.post(
            "/api/gateway/config/provider-endpoint-profiles",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "id": "tenant-endpoint",
                "display_name": "Tenant Endpoint",
                "description": "Should require admin.",
                "provider_family": "openai-compatible",
                "base_url": "https://remote.example.test/v1",
                "api_key": "profile-key",
                "scope": "gateway",
            },
        )
        allowed = client.post(
            "/api/gateway/config/provider-endpoint-profiles",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "id": "personal-endpoint",
                "display_name": "Personal Endpoint",
                "description": "User-owned endpoint.",
                "provider_family": "openai-compatible",
                "base_url": "https://remote.example.test/v1",
                "api_key": "profile-key",
                "scope": "user",
            },
        )

    assert denied.status_code == 403, denied.text
    assert allowed.status_code == 200, allowed.text


def test_bundle_host_resolves_endpoint_profiles_for_runtime_without_exposing_secret(tmp_path: Path) -> None:
    from abstractgateway.hosts.bundle_host import (
        _attach_provider_endpoint_profile_resolver,
        _resolve_gateway_default_endpoint_profile,
    )
    from abstractgateway.provider_endpoint_profiles import ProviderEndpointProfileStore

    data_root = tmp_path / "runtime"
    root_data = tmp_path / "root"
    ProviderEndpointProfileStore(base_dir=root_data).upsert_profile(
        profile_id="office-vllm",
        display_name="Office vLLM",
        description="Gateway-scoped profile.",
        provider_family="openai-compatible",
        base_url="https://llm.example.test/v1",
        api_key="secret-key",
        scope="gateway",
    )

    provider, kwargs, override = _resolve_gateway_default_endpoint_profile(
        provider="endpoint:office-vllm",
        data_root=data_root,
        catalog_root=root_data,
    )
    assert provider == "openai-compatible"
    assert kwargs == {"base_url": "https://llm.example.test/v1", "api_key": "secret-key"}
    assert override == "endpoint:office-vllm"

    class FakeLLM:
        pass

    class FakeRuntime:
        pass

    runtime = FakeRuntime()
    runtime._abstractcore_llm_client = FakeLLM()
    _attach_provider_endpoint_profile_resolver(runtime=runtime, data_root=data_root, catalog_root=root_data)  # type: ignore[arg-type]
    resolved = runtime._abstractcore_llm_client.resolve_provider_endpoint_profile("endpoint:office-vllm")
    assert resolved["provider"] == "openai-compatible"
    assert resolved["base_url"] == "https://llm.example.test/v1"
    assert resolved["api_key"] == "secret-key"
    assert resolved["api_key_set"] is True
