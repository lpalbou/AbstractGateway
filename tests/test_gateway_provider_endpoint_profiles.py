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


def _without_local_autoprobes(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        call
        for call in calls
        if not (
            call.get("provider_name") in {"lmstudio", "ollama"}
            and call.get("timeout_s") == 1.5
        )
    ]


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

    assert [call for call in calls if call.get("method") == "list_providers"] == [
        {"method": "list_providers", "include_models": False, "provider_api_key": None}
    ]


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

    assert _without_local_autoprobes(calls) == [
        {
            "provider_name": "openai-compatible",
            "base_url": "https://remote.example.test/v1",
            "provider_api_key": "profile-key",
            "input_type": None,
            "output_type": None,
            "capability_route": None,
            "timeout_s": 30.0,
        }
    ]


def test_endpoint_profile_allowed_models_are_intersected_with_capability_filters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class StubDiscoveryFacade:
        def list_providers(self, *, include_models: bool = False, **kwargs: Any) -> dict[str, Any]:
            return {"items": []}

        def list_provider_models(self, provider_name: str, **kwargs: Any) -> dict[str, Any]:
            calls.append({"provider_name": provider_name, **kwargs})
            return {"provider": provider_name, "models": ["vlm-model", "other-compatible"]}

    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_discovery_facade", lambda: (StubDiscoveryFacade(), None))

    headers = {"Authorization": "Bearer admin-token"}
    with _app_client(tmp_path, monkeypatch) as client:
        created = client.post(
            "/api/gateway/config/provider-endpoint-profiles",
            headers=headers,
            json={
                "id": "office",
                "display_name": "Office",
                "provider_family": "openai-compatible",
                "base_url": "https://office.example.test/v1",
                "api_key": "office-key",
                "scope": "gateway",
                "allowed_models": ["text-only-model", "vlm-model"],
            },
        )
        assert created.status_code == 200, created.text

        models = client.get(
            "/api/gateway/discovery/providers/endpoint%3Aoffice/models?input_type=image&output_type=text",
            headers=headers,
        )

    assert models.status_code == 200, models.text
    assert models.json()["models"] == ["vlm-model"]
    assert _without_local_autoprobes(calls) == [
        {
            "provider_name": "openai-compatible",
            "base_url": "https://office.example.test/v1",
            "provider_api_key": "office-key",
            "input_type": "image",
            "output_type": "text",
            "capability_route": None,
            "timeout_s": 30.0,
        }
    ]


def test_endpoint_profile_allowed_models_are_intersected_with_capability_routes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class StubDiscoveryFacade:
        def list_providers(self, *, include_models: bool = False, **kwargs: Any) -> dict[str, Any]:
            return {"items": []}

        def list_provider_models(self, provider_name: str, **kwargs: Any) -> dict[str, Any]:
            calls.append({"provider_name": provider_name, **kwargs})
            return {"provider": provider_name, "models": ["vlm-model", "other-compatible"]}

    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_discovery_facade", lambda: (StubDiscoveryFacade(), None))

    headers = {"Authorization": "Bearer admin-token"}
    with _app_client(tmp_path, monkeypatch) as client:
        created = client.post(
            "/api/gateway/config/provider-endpoint-profiles",
            headers=headers,
            json={
                "id": "office",
                "display_name": "Office",
                "provider_family": "openai-compatible",
                "base_url": "https://office.example.test/v1",
                "api_key": "office-key",
                "scope": "gateway",
                "allowed_models": ["text-only-model", "vlm-model"],
            },
        )
        assert created.status_code == 200, created.text

        models = client.get(
            "/api/gateway/discovery/providers/endpoint%3Aoffice/models?capability_route=input.image,output.text",
            headers=headers,
        )

    assert models.status_code == 200, models.text
    assert models.json()["models"] == ["vlm-model"]
    assert _without_local_autoprobes(calls) == [
        {
            "provider_name": "openai-compatible",
            "base_url": "https://office.example.test/v1",
            "provider_api_key": "office-key",
            "input_type": None,
            "output_type": None,
            "capability_route": ["input.image,output.text"],
            "timeout_s": 30.0,
        }
    ]


@pytest.mark.parametrize("provider_family", ["openai", "anthropic"])
def test_named_provider_connection_discovery_uses_profile_url_and_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_family: str,
) -> None:
    calls: list[dict[str, Any]] = []

    class StubDiscoveryFacade:
        def list_providers(self, *, include_models: bool = False, **kwargs: Any) -> dict[str, Any]:
            return {"items": []}

        def list_provider_models(self, provider_name: str, **kwargs: Any) -> dict[str, Any]:
            calls.append({"provider_name": provider_name, **kwargs})
            return {"provider": provider_name, "models": [f"{provider_family}-model"]}

    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_discovery_facade", lambda: (StubDiscoveryFacade(), None))

    headers = {"Authorization": "Bearer admin-token"}
    base_url = f"https://{provider_family}.example.test/v1"
    with _app_client(tmp_path, monkeypatch) as client:
        created = client.post(
            "/api/gateway/config/provider-endpoint-profiles",
            headers=headers,
            json={
                "id": provider_family,
                "display_name": provider_family.title(),
                "provider_family": provider_family,
                "base_url": base_url,
                "api_key": f"{provider_family}-key",
                "scope": "gateway",
                "allowed_models": [],
            },
        )
        assert created.status_code == 200, created.text

        models = client.get(f"/api/gateway/discovery/providers/endpoint%3A{provider_family}/models", headers=headers)
        assert models.status_code == 200, models.text
        assert models.json()["models"] == [f"{provider_family}-model"]
        assert f"{provider_family}-key" not in models.text

    assert _without_local_autoprobes(calls) == [
        {
            "provider_name": provider_family,
            "base_url": base_url,
            "provider_api_key": f"{provider_family}-key",
            "input_type": None,
            "output_type": None,
            "capability_route": None,
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
    assert _without_local_autoprobes(calls) == [
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
    assert _without_local_autoprobes(calls) == [
        {
            "provider_name": "openai-compatible",
            "base_url": "https://saved.example.test/v1",
            "provider_api_key": "saved-key",
            "timeout_s": 30.0,
        }
    ]


def test_configured_builtin_provider_surfaces_without_manual_endpoint_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class StubDiscoveryFacade:
        def list_providers(self, *, include_models: bool = False, **kwargs: Any) -> dict[str, Any]:
            return {"items": [], "default_provider": None, "default_model": None}

        def list_provider_models(self, provider_name: str, **kwargs: Any) -> dict[str, Any]:
            calls.append({"provider_name": provider_name, **kwargs})
            return {"provider": provider_name, "models": ["claude-haiku-4-5"]}

    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_discovery_facade", lambda: (StubDiscoveryFacade(), None))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-env-key")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)

    headers = {"Authorization": "Bearer admin-token"}
    with _app_client(tmp_path, monkeypatch) as client:
        profiles = client.get("/api/gateway/config/provider-endpoint-profiles", headers=headers)
        assert profiles.status_code == 200, profiles.text
        row = next(item for item in profiles.json()["profiles"] if item["provider_id"] == "anthropic")
        assert row["display_name"] == "Anthropic"
        assert row["managed"] is False
        assert row["api_key_set"] is True
        assert row["scope"] == "environment"
        assert "anthropic-env-key" not in profiles.text

        providers = client.get("/api/gateway/discovery/providers?include_models=false", headers=headers)
        assert providers.status_code == 200, providers.text
        provider_item = next(item for item in providers.json()["items"] if item["name"] == "anthropic")
        assert provider_item["display_name"] == "Anthropic"
        assert provider_item["provider_endpoint_profile"]["managed"] is False
        assert "anthropic-env-key" not in providers.text

        models = client.get("/api/gateway/discovery/providers/anthropic/models", headers=headers)
        assert models.status_code == 200, models.text
        assert models.json()["models"] == ["claude-haiku-4-5"]
        assert "anthropic-env-key" not in models.text

    assert _without_local_autoprobes(calls) == [
        {
            "provider_name": "anthropic",
            "base_url": None,
            "provider_api_key": "anthropic-env-key",
            "input_type": None,
            "output_type": None,
            "capability_route": None,
            "timeout_s": 30.0,
        }
    ]


def test_configured_builtin_provider_can_use_gateway_core_config_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class StubDiscoveryFacade:
        def list_providers(self, *, include_models: bool = False, **kwargs: Any) -> dict[str, Any]:
            return {"items": []}

        def list_provider_models(self, provider_name: str, **kwargs: Any) -> dict[str, Any]:
            calls.append({"provider_name": provider_name, **kwargs})
            return {"provider": provider_name, "models": ["gpt-5-nano"]}

    from abstractcore.config.manager import ConfigurationManager
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_discovery_facade", lambda: (StubDiscoveryFacade(), None))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    runtime_dir = tmp_path / "runtime"
    manager = ConfigurationManager(config_file=runtime_dir / "config" / "abstractcore.json", apply_env=False)
    assert manager.set_api_key("openai", "openai-config-key")

    headers = {"Authorization": "Bearer admin-token"}
    with _app_client(tmp_path, monkeypatch) as client:
        profiles = client.get("/api/gateway/config/provider-endpoint-profiles", headers=headers)
        assert profiles.status_code == 200, profiles.text
        row = next(item for item in profiles.json()["profiles"] if item["provider_id"] == "openai")
        assert row["scope"] == "user"
        assert row["api_key_set"] is True
        assert "openai-config-key" not in profiles.text

        models = client.get("/api/gateway/discovery/providers/openai/models", headers=headers)
        assert models.status_code == 200, models.text
        assert models.json()["models"] == ["gpt-5-nano"]
        assert "openai-config-key" not in models.text

    assert _without_local_autoprobes(calls) == [
        {
            "provider_name": "openai",
            "base_url": None,
            "provider_api_key": "openai-config-key",
            "input_type": None,
            "output_type": None,
            "capability_route": None,
            "timeout_s": 30.0,
        }
    ]


def test_reachable_local_default_provider_surfaces_without_manual_endpoint_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class StubDiscoveryFacade:
        def list_provider_models(self, provider_name: str, **kwargs: Any) -> dict[str, Any]:
            calls.append({"provider_name": provider_name, **kwargs})
            if provider_name == "lmstudio":
                return {"provider": provider_name, "models": ["local-qwen"]}
            return {"provider": provider_name, "models": []}

    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_discovery_facade", lambda: (StubDiscoveryFacade(), None))
    monkeypatch.delenv("LMSTUDIO_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)

    headers = {"Authorization": "Bearer admin-token"}
    with _app_client(tmp_path, monkeypatch) as client:
        profiles = client.get("/api/gateway/config/provider-endpoint-profiles", headers=headers)

    assert profiles.status_code == 200, profiles.text
    row = next(item for item in profiles.json()["profiles"] if item["provider_id"] == "lmstudio")
    assert row["display_name"] == "LM Studio"
    assert row["base_url"] == "http://localhost:1234/v1"
    assert row["managed"] is False
    assert row["api_key_set"] is False
    assert row["discovered_model_count"] == 1
    assert calls[0]["provider_name"] == "lmstudio"
    assert calls[0]["base_url"] == "http://localhost:1234/v1"


def test_gateway_sandbox_text_generation_uses_server_side_endpoint_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class FakeLLM:
        def __init__(self, *, provider: str, model: str, llm_kwargs: dict[str, Any] | None = None, **_: Any) -> None:
            calls.append({"provider": provider, "model": model, "llm_kwargs": llm_kwargs or {}})

        def generate(self, **kwargs: Any) -> dict[str, Any]:
            calls.append({"generate": kwargs})
            return {"content": "sandbox ok", "usage": {"total_tokens": 3}}

    import abstractruntime.integrations.abstractcore.llm_client as llm_client

    monkeypatch.setattr(llm_client, "LocalAbstractCoreLLMClient", FakeLLM)
    headers = {"Authorization": "Bearer admin-token"}
    with _app_client(tmp_path, monkeypatch) as client:
        created = client.post(
            "/api/gateway/config/provider-endpoint-profiles",
            headers=headers,
            json={
                "id": "office-vllm",
                "display_name": "Office vLLM",
                "provider_family": "openai-compatible",
                "base_url": "https://llm.example.test/v1",
                "api_key": "profile-key",
                "scope": "gateway",
            },
        )
        assert created.status_code == 200, created.text

        res = client.post(
            "/api/gateway/sandbox/generate",
            headers=headers,
            json={
                "capability": "output.text",
                "provider": "endpoint:office-vllm",
                "model": "qwen/qwen3",
                "prompt": "hello",
                "attachments": [{"$artifact": "file-1", "content_type": "image/png", "filename": "brief.png"}],
                "client_context": {
                    "source": "browser",
                    "local_datetime": "2026-06-01T11:22:33-04:00",
                    "utc_datetime": "2026-06-01T15:22:33.000Z",
                    "timezone": "America/New_York",
                    "locale": "en-US",
                    "country": "US",
                    "timezone_offset_minutes": -240,
                    "ignored": "not-forwarded",
                },
            },
        )

    assert res.status_code == 200, res.text
    assert res.json()["response"] == "sandbox ok"
    assert "profile-key" not in res.text
    assert calls[0] == {
        "provider": "openai-compatible",
        "model": "qwen/qwen3",
        "llm_kwargs": {"base_url": "https://llm.example.test/v1", "api_key": "profile-key"},
    }
    assert calls[1]["generate"]["messages"][-1] == {"role": "user", "content": "hello"}
    assert calls[1]["generate"]["media"] == [
        {
            "$artifact": "file-1",
            "artifact_id": "file-1",
            "content_type": "image/png",
            "filename": "brief.png",
            "mime_type": "image/png",
            "modality": "image",
            "type": "image",
        }
    ]
    trace_metadata = calls[1]["generate"]["params"]["trace_metadata"]
    assert trace_metadata["source"] == "gateway_console_sandbox"
    assert trace_metadata["user_id"] == "local-admin"
    assert trace_metadata["client_context"] == {
        "source": "browser_untrusted",
        "local_datetime": "2026-06-01T11:22:33-04:00",
        "utc_datetime": "2026-06-01T15:22:33.000Z",
        "timezone": "America/New_York",
        "locale": "en-US",
        "country": "US",
        "timezone_offset_minutes": -240,
    }


def test_gateway_sandbox_text_generation_does_not_apply_console_prompt_caps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class FakeLLM:
        def __init__(self, *, provider: str, model: str, **_: Any) -> None:
            calls.append({"provider": provider, "model": model})

        def generate(self, **kwargs: Any) -> dict[str, Any]:
            calls.append({"generate": kwargs})
            return {"content": "long prompt ok", "usage": {"total_tokens": 1}}

    import abstractruntime.integrations.abstractcore.llm_client as llm_client

    monkeypatch.setattr(llm_client, "LocalAbstractCoreLLMClient", FakeLLM)
    long_prior = "p" * 13000
    long_prompt = "x" * 21358
    long_system = "s" * 13000
    headers = {"Authorization": "Bearer admin-token"}

    with _app_client(tmp_path, monkeypatch) as client:
        res = client.post(
            "/api/gateway/sandbox/generate",
            headers=headers,
            json={
                "capability": "output.text",
                "provider": "lmstudio",
                "model": "qwen/qwen3.6-35b-a3b",
                "prompt": long_prompt,
                "system_prompt": long_system,
                "max_tokens": 82000,
                "messages": [
                    {"role": "user", "content": long_prior},
                    {"role": "assistant", "content": "ack"},
                ],
            },
        )

    assert res.status_code == 200, res.text
    generate_call = calls[1]["generate"]
    assert generate_call["system_prompt"] == long_system
    assert generate_call["params"]["max_tokens"] == 82000
    assert generate_call["messages"][0] == {"role": "user", "content": long_prior}
    assert generate_call["messages"][-1] == {"role": "user", "content": long_prompt}


def test_console_presents_provider_connections_without_capability_form() -> None:
    from abstractgateway.console import gateway_console_html

    html = gateway_console_html()
    assert "Provider Connections" in html
    assert "LM Studio" in html
    assert "Ollama" in html
    assert "Portkey" in html
    assert "Custom OpenAI-compatible" in html
    assert 'id="endpoint-capabilities"' not in html


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
