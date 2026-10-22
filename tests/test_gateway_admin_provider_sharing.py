"""Shared admin providers: discovery and execution agree across private runtimes."""
from pathlib import Path
from typing import Any

import pytest

from test_gateway_provider_endpoint_profiles import _app_client


@pytest.mark.parametrize("scope", [None, "user"])
def test_separate_admin_runtime_creates_shared_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str | None) -> None:
    import abstractgateway.routes.gateway as routes
    from abstractgateway.users import GatewayUserRegistry

    # Avoid probing the operator's provider servers; no upstream calls are needed.
    monkeypatch.setattr(routes, "_gateway_abstractcore_discovery_facade", lambda: (None, "fixture"))
    with _app_client(tmp_path, monkeypatch, user_auth=True) as client:
        _, admin = GatewayUserRegistry().create_user(user_id="second-admin", roles=["admin", "user"])
        _, user = GatewayUserRegistry().create_user(user_id="alice", roles=["user"])
        body = {"id": "shared", "display_name": "Shared", "base_url": "https://provider.example/v1", "api_key": "shared-secret"}
        if scope is not None:
            body["scope"] = scope
        created = client.post("/api/gateway/config/provider-endpoint-profiles", headers={"Authorization": f"Bearer {admin}"}, json=body)
        assert created.status_code == 200, created.text
        assert created.json()["profile"]["scope"] == "gateway"
        headers = {"Authorization": f"Bearer {user}"}
        visible = client.get("/api/gateway/config/provider-endpoint-profiles", headers=headers)
        assert visible.status_code == 200, visible.text
        assert any(p.get("virtual_provider") == "endpoint:shared" for p in visible.json()["profiles"])
        assert "shared-secret" not in visible.text
        for method in ("put", "delete"):
            response = getattr(client, method)("/api/gateway/config/provider-endpoint-profiles/shared", headers=headers, **({"json": {"display_name": "Hijacked"}} if method == "put" else {}))
            assert response.status_code == 403, response.text


def test_legacy_admin_provider_is_discoverable_and_executable_for_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as routes
    import abstractruntime.integrations.abstractcore.llm_client as llm_client
    from abstractgateway.provider_endpoint_profiles import ProviderEndpointProfileStore
    from abstractgateway.users import GatewayUserRegistry

    calls: list[dict[str, Any]] = []

    class Discovery:
        def list_providers(self, **_: Any) -> dict[str, Any]:
            return {"items": []}

        def list_provider_models(self, provider_name: str, **kwargs: Any) -> dict[str, Any]:
            if kwargs.get("base_url") == "https://provider.example/v1":
                assert provider_name == "openai-compatible"
                assert kwargs["provider_api_key"] == "shared-secret"
            return {"models": ["shared-model"]}

    class FakeLLM:
        def __init__(self, **kwargs: Any) -> None:
            calls.append(kwargs)

        def set_provider_endpoint_profile_resolver(self, resolver: Any) -> None:
            self.resolver = resolver

        def generate(self, **_: Any) -> dict[str, Any]:
            return {"content": "shared provider works"}

    monkeypatch.setattr(routes, "_gateway_abstractcore_discovery_facade", lambda: (Discovery(), None))
    monkeypatch.setattr(llm_client, "LocalAbstractCoreLLMClient", FakeLLM)
    with _app_client(tmp_path, monkeypatch, user_auth=True) as client:
        _, user = GatewayUserRegistry().create_user(user_id="alice", roles=["user"])
        ProviderEndpointProfileStore(base_dir=tmp_path / "runtime").upsert_profile(
            profile_id="legacy", display_name="Legacy admin endpoint", provider_family="openai-compatible",
            base_url="https://provider.example/v1", api_key="shared-secret", scope="user",
        )
        headers = {"Authorization": f"Bearer {user}"}
        providers = client.get("/api/gateway/discovery/providers", headers=headers)
        assert providers.status_code == 200, providers.text
        assert any(p["name"] == "endpoint:legacy" for p in providers.json()["items"])
        models = client.get("/api/gateway/discovery/providers/endpoint:legacy/models", headers=headers)
        assert models.status_code == 200 and models.json()["models"] == ["shared-model"], models.text
        result = client.post("/api/gateway/sandbox/generate", headers=headers, json={
            "capability": "output.text", "provider": "endpoint:legacy", "model": "shared-model", "prompt": "hello",
        })
        assert result.status_code == 200, result.text
        assert result.json()["response"] == "shared provider works"
        assert calls[-1]["provider"] == "openai-compatible"
        assert calls[-1]["llm_kwargs"] == {"base_url": "https://provider.example/v1", "api_key": "shared-secret"}
        for response in (providers, models, result):
            assert "shared-secret" not in response.text
        for method in ("put", "delete"):
            response = getattr(client, method)("/api/gateway/config/provider-endpoint-profiles/legacy", headers=headers, **({"json": {"display_name": "Hijacked"}} if method == "put" else {}))
            assert response.status_code == 403, response.text


def test_private_user_provider_stays_private_and_overrides_shared_only_locally(tmp_path: Path) -> None:
    from abstractgateway.provider_endpoint_profiles import ProviderEndpointProfileStore, resolve_effective_endpoint_profile

    root, alice, bob = (tmp_path / name for name in ("root", "alice", "bob"))
    for path, name, key in ((root, "shared", "admin-key"), (alice, "shared", "alice-key"), (alice, "personal", "personal-key")):
        ProviderEndpointProfileStore(base_dir=path, core_backed=False).upsert_profile(
            profile_id=name, provider_family="openai-compatible", base_url="https://provider.example/v1", api_key=key,
        )
    assert resolve_effective_endpoint_profile("endpoint:shared", base_dir=alice, root_base_dir=root).api_key == "alice-key"
    assert resolve_effective_endpoint_profile("endpoint:shared", base_dir=bob, root_base_dir=root).api_key == "admin-key"
    assert resolve_effective_endpoint_profile("endpoint:personal", base_dir=bob, root_base_dir=root) is None
    ProviderEndpointProfileStore(base_dir=root, core_backed=False).upsert_profile(profile_id="shared", enabled=False)
    assert resolve_effective_endpoint_profile("endpoint:shared", base_dir=bob, root_base_dir=root) is None


def test_admin_edit_promotes_old_private_connection_without_losing_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as routes
    from abstractgateway.provider_endpoint_profiles import ProviderEndpointProfileStore
    from abstractgateway.users import GatewayUserRegistry

    monkeypatch.setattr(routes, "_gateway_abstractcore_discovery_facade", lambda: (None, "fixture"))
    with _app_client(tmp_path, monkeypatch, user_auth=True) as client:
        _, admin = GatewayUserRegistry().create_user(user_id="second-admin", roles=["admin", "user"])
        root = tmp_path / "runtime"
        local_store = ProviderEndpointProfileStore(base_dir=root / "users/default/second-admin/runtime")
        local_store.upsert_profile(
            profile_id="old-private", display_name="Old", description="Keep me", provider_family="openai-compatible",
            base_url="https://provider.example/v1", api_key="admin-secret", allowed_models=["model-a"],
        )
        response = client.put("/api/gateway/config/provider-endpoint-profiles/old-private", headers={"Authorization": f"Bearer {admin}"}, json={"display_name": "Now shared", "scope": "user"})
        assert response.status_code == 200, response.text
        assert response.json()["profile"]["scope"] == "gateway"
        assert "admin-secret" not in response.text
        shared = ProviderEndpointProfileStore(base_dir=root).get_profile("old-private")
        assert shared is not None
        assert shared.api_key == "admin-secret" and shared.base_url == "https://provider.example/v1"
        assert shared.description == "Keep me" and shared.allowed_models == ("model-a",)
        assert local_store.get_profile("old-private") is None
