"""The entity LLM path resolves `endpoint:<profile>` virtual providers
(build-phase regression caught by agency's gate, c747).

The per-entity runtime's LLM client (unlike bundle_host's workflow client)
has no attached endpoint-profile resolver, so an entity whose substrate
names `endpoint:ovh-provider` sent the raw string into create_llm →
"Unknown provider: endpoint:ovh-provider" and the visit turn failed. The
door must resolve the profile to (provider_family, base_url, api_key)
BEFORE building the client — the same store bundle_host reads — and FAIL
LOUD on a named-but-missing profile (no silent mind swap).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")

from abstractgateway.entities import EntityRegistry  # noqa: E402
from abstractgateway.entity_chat import ChatOpenRefused  # noqa: E402
from abstractgateway.provider_endpoint_profiles import ProviderEndpointProfileStore  # noqa: E402


def _registry(tmp_path: Path) -> EntityRegistry:
    return EntityRegistry(data_dir=tmp_path / "runtime", embedder_factory=lambda: None)


def test_plain_provider_passes_through_lowercased(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    provider, kwargs = reg._resolve_entity_provider("LMStudio")
    assert provider == "lmstudio"
    assert kwargs == {}


def test_endpoint_profile_resolves_to_family_and_route(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    store = ProviderEndpointProfileStore(base_dir=reg.data_dir)
    store.upsert_profile(
        profile_id="ovh-provider",
        display_name="OVH",
        provider_family="openai-compatible",
        base_url="https://ovh.example/v1",
        api_key="sk-secret",
        scope="gateway",
    )

    provider, kwargs = reg._resolve_entity_provider("endpoint:ovh-provider")
    assert provider == "openai-compatible"
    assert kwargs["base_url"] == "https://ovh.example/v1"
    assert kwargs["api_key"] == "sk-secret"


def test_missing_endpoint_profile_fails_loud(tmp_path: Path) -> None:
    """No-fallback: a substrate naming an unconfigured endpoint refuses, never
    silently swaps to a default provider."""
    reg = _registry(tmp_path)
    with pytest.raises(ChatOpenRefused) as exc:
        reg._resolve_entity_provider("endpoint:ghost")
    assert "not configured" in exc.value.detail


def test_disabled_endpoint_profile_fails_loud(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    store = ProviderEndpointProfileStore(base_dir=reg.data_dir)
    store.upsert_profile(
        profile_id="ovh-provider",
        display_name="OVH",
        provider_family="openai-compatible",
        base_url="https://ovh.example/v1",
        scope="gateway",
        enabled=False,
    )
    with pytest.raises(ChatOpenRefused):
        reg._resolve_entity_provider("endpoint:ovh-provider")


def test_root_scoped_profile_visible_to_per_principal_registry(tmp_path: Path) -> None:
    """agency c753: under user auth the registry data_dir is a per-principal
    runtime root, but gateway-scoped endpoint profiles live at the gateway
    ROOT. A registry whose data_dir != root_data_dir must still resolve a
    root-scoped profile (create validates at root; run must agree)."""
    root = tmp_path / "runtime"
    principal_root = tmp_path / "users" / "local" / "local-admin" / "runtime"
    # The gateway-scoped profile lives at the ROOT store only.
    ProviderEndpointProfileStore(base_dir=root).upsert_profile(
        profile_id="ovh-provider",
        display_name="OVH",
        provider_family="openai-compatible",
        base_url="https://ovh.example/v1",
        api_key="sk-secret",
        scope="gateway",
    )

    reg = EntityRegistry(
        data_dir=principal_root, embedder_factory=lambda: None, root_data_dir=root
    )
    provider, kwargs = reg._resolve_entity_provider("endpoint:ovh-provider")
    assert provider == "openai-compatible"
    assert kwargs["base_url"] == "https://ovh.example/v1"

    # Without the root threaded (the pre-c753 bug), the per-principal registry
    # cannot see the root-scoped profile and refuses loud.
    blind = EntityRegistry(data_dir=principal_root, embedder_factory=lambda: None)
    with pytest.raises(ChatOpenRefused):
        blind._resolve_entity_provider("endpoint:ovh-provider")
