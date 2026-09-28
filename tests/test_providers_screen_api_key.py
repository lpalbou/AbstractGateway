"""`providers_screen_api_key`: which saved key counts as the provider's own.

The voice listings and the voice runtime send this key to the provider's API
(OpenAI's voice endpoints for "openai"). An endpoint profile of the openai
family that points at another base URL (a proxy, a self-hosted server) holds a
key for THAT endpoint: it must never be sent to OpenAI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import abstractgateway.provider_connections as pc
import abstractgateway.provider_endpoint_profiles as pep
from abstractgateway.provider_endpoint_profiles import ProviderEndpointProfile

pytestmark = pytest.mark.basic


def _with_profiles(monkeypatch: pytest.MonkeyPatch, profiles: list) -> None:
    monkeypatch.setattr(pc, "configured_provider_api_key", lambda *a, **k: ("", None))
    monkeypatch.setattr(pep, "effective_endpoint_profiles", lambda **_k: list(profiles))


def _key(tmp_path: Path) -> str:
    return pc.providers_screen_api_key("openai", current_base_dir=tmp_path, root_base_dir=tmp_path)


def test_a_profile_on_a_custom_base_url_is_never_the_providers_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _with_profiles(monkeypatch, [
        ProviderEndpointProfile(id="proxy", display_name="Proxy", provider_family="openai",
                                base_url="https://llm-proxy.example.com/v1", api_key="sk-proxy"),
    ])
    assert _key(tmp_path) == ""


@pytest.mark.parametrize("base_url", ["", "https://api.openai.com/v1", "https://api.openai.com/v1/"])
def test_a_profile_on_the_default_endpoint_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base_url: str) -> None:
    _with_profiles(monkeypatch, [
        ProviderEndpointProfile(id="proxy", display_name="Proxy", provider_family="openai",
                                base_url="https://llm-proxy.example.com/v1", api_key="sk-proxy"),
        ProviderEndpointProfile(id="openai-main", display_name="OpenAI", provider_family="openai",
                                base_url=base_url, api_key="sk-openai"),
    ])
    assert _key(tmp_path) == "sk-openai"


def test_the_core_key_still_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _with_profiles(monkeypatch, [])
    monkeypatch.setattr(pc, "configured_provider_api_key", lambda *a, **k: ("sk-core", "abstractcore.config"))
    assert _key(tmp_path) == "sk-core"
