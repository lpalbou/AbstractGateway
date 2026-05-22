from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict[str, str]]:
    token = "t"
    flows = tmp_path / "flows"
    flows.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")

    from abstractgateway.app import app

    return TestClient(app), {"Authorization": f"Bearer {token}"}


def _patch_discovery_facade(monkeypatch: pytest.MonkeyPatch, *, facade: object) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_discovery_facade", lambda: (facade, None))


def test_voice_catalog_uses_local_capability_profiles_without_core_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ABSTRACTCORE_SERVER_BASE_URL", raising=False)

    class StubDiscoveryFacade:
        def get_voice_catalog(self, **_kwargs) -> Dict[str, Any]:
            return {
                "available": True,
                "profiles": [{"profile_id": "coral"}, {"profile_id": "verse"}],
                "tts_models": ["gpt-4o-mini-tts"],
            }

    _patch_discovery_facade(monkeypatch, facade=StubDiscoveryFacade())

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/voice/voices", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "abstractruntime.discovery_facade"
    assert body["route_available"] is True
    ids = {item.get("id") or item.get("profile_id") or item.get("voice_id") for item in body["profiles"]}
    assert {"coral", "verse"} <= ids


def test_voice_catalog_static_fallback_surfaces_configured_env_voices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubDiscoveryFacade:
        def get_voice_catalog(self, **_kwargs) -> Dict[str, Any]:
            return {"available": False, "profiles": [], "error": "voice unavailable"}

    _patch_discovery_facade(monkeypatch, facade=StubDiscoveryFacade())

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/voice/voices", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "abstractruntime.discovery_facade"
    assert body["route_available"] is True
    assert body["available"] is False
    assert body["profiles"] == []


def test_voice_catalog_static_fallback_surfaces_supertonic_builtin_profiles_without_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_module_available", lambda _name: False)
    monkeypatch.setattr(
        gateway_routes,
        "_builtin_voice_profile_records",
        lambda engine: (
            [{"id": voice_id, "profile_id": voice_id, "label": voice_id, "provider": "supertonic", "engine_id": "supertonic"} for voice_id in ["M1", "M2", "M3", "M4", "M5", "F1", "F2", "F3", "F4", "F5"]]
            if str(engine).strip().lower() == "supertonic"
            else []
        ),
    )

    body = gateway_routes._static_voice_catalog_response(provider="supertonic")
    profile_ids = {item.get("profile_id") for item in body["profiles"]}

    assert body["source"] == "gateway_static"
    assert body["available"] is True
    assert body["tts_providers"] == ["supertonic"]
    assert {"M1", "M2", "M3", "M4", "M5", "F1", "F2", "F3", "F4", "F5"} <= profile_ids
    assert body["tts_models_by_provider"] == {"supertonic": ["supertonic-3"]}
    assert body["tts_voices_by_provider"]["supertonic"] == ["M1", "M2", "M3", "M4", "M5", "F1", "F2", "F3", "F4", "F5"]

    speech_models = gateway_routes._static_speech_models_response(provider="supertonic")
    assert speech_models["providers"] == ["supertonic"]
    assert speech_models["models"] == ["supertonic-3"]

    providers = gateway_routes._static_voice_providers_only_response()
    assert "supertonic" in providers["tts_providers"]


def test_voice_catalog_proxies_configured_core_catalog_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Dict[str, Any]] = []

    class StubDiscoveryFacade:
        def get_voice_catalog(self, **kwargs: Any) -> Dict[str, Any]:
            calls.append(dict(kwargs))
            return {"available": True, "profiles": [{"profile_id": "coral"}], "source": "abstractvoice"}

    _patch_discovery_facade(monkeypatch, facade=StubDiscoveryFacade())

    client, headers = _client(tmp_path, monkeypatch)
    headers = {**headers, "X-AbstractCore-Provider-API-Key": "provider-secret"}
    with client:
        resp = client.get("/api/gateway/voice/voices?base_url=http://provider.test/v1", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "abstractruntime.discovery_facade"
    assert body["route_available"] is True
    assert body["profiles"] == [{"profile_id": "coral"}]
    assert calls == [
        {
            "base_url": "http://provider.test/v1",
            "provider_api_key": "provider-secret",
            "provider": None,
            "model": None,
            "providers_only": False,
        }
    ]


def test_catalog_proxy_preserves_core_auth_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class StubDiscoveryFacade:
        def list_tts_models(self, **_kwargs: Any) -> Dict[str, Any]:
            raise RuntimeError("core auth required")

    _patch_discovery_facade(monkeypatch, facade=StubDiscoveryFacade())

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/audio/speech/models", headers=headers)

    assert resp.status_code == 502, resp.text
    assert "core auth required" in resp.text


def test_audio_model_catalogs_include_local_voice_providers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ABSTRACTCORE_SERVER_BASE_URL", raising=False)

    class StubDiscoveryFacade:
        def list_tts_models(self, **_kwargs) -> Dict[str, Any]:
            return {"providers": ["fake-tts"], "models": ["tts-test"], "active_provider": "fake-tts"}

        def list_stt_models(self, **_kwargs) -> Dict[str, Any]:
            return {"providers": ["fake-stt"], "models": ["stt-test"], "active_provider": "fake-stt"}

    _patch_discovery_facade(monkeypatch, facade=StubDiscoveryFacade())

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        speech = client.get("/api/gateway/audio/speech/models", headers=headers)
        transcription = client.get("/api/gateway/audio/transcriptions/models", headers=headers)

    assert speech.status_code == 200, speech.text
    assert transcription.status_code == 200, transcription.text
    assert "fake-tts" in speech.json()["providers"]
    assert transcription.json()["providers"] == ["fake-stt"]
    assert transcription.json()["active_provider"] == "fake-stt"


def test_vision_catalog_rejects_unknown_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/vision/provider_models?task=unknown", headers=headers)

    assert resp.status_code == 400, resp.text


def test_gateway_vision_catalog_routes_local_mflux_without_diffusers_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_has_local_mflux_preset", lambda model_id: str(model_id).endswith("FLUX.2-klein-9B"))

    item = gateway_routes._gateway_vision_provider_model_item(
        provider="huggingface",
        model_id="black-forest-labs/FLUX.2-klein-9B",
        task="text_to_image",
    )

    assert item["provider"] == "mflux"
    assert item["backend"] == "mflux"
    assert item["model"] == "black-forest-labs/FLUX.2-klein-9B"
    assert item["routed_model"] == "black-forest-labs/FLUX.2-klein-9B"
    assert not str(item["model"]).startswith("diffusers/")


def test_gateway_direct_image_available_with_downloaded_mflux(monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.delenv("ABSTRACTVISION_BACKEND", raising=False)
    monkeypatch.delenv("ABSTRACTCORE_VISION_BACKEND", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ABSTRACTVISION_API_KEY", raising=False)
    monkeypatch.setattr(gateway_routes, "_gateway_has_local_mflux_preset", lambda model_id: model_id == "")

    assert gateway_routes._gateway_direct_image_configured() is True


def test_vision_models_catalog_proxies_configured_core_catalog_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Dict[str, Any]] = []

    class StubDiscoveryFacade:
        def list_cached_vision_models(self, **kwargs: Any) -> Dict[str, Any]:
            calls.append(dict(kwargs))
            return {"available": True, "models": [{"model_id": "flux-local"}], "source": "abstractvision"}

    _patch_discovery_facade(monkeypatch, facade=StubDiscoveryFacade())

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/vision/models", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "abstractruntime.discovery_facade"
    assert body["route_available"] is True
    assert body["models"] == [{"model_id": "flux-local"}]
    assert calls == [{"provider_api_key": None}]


def test_audio_transcription_models_catalog_proxies_configured_core_catalog_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Dict[str, Any]] = []

    class StubDiscoveryFacade:
        def list_stt_models(self, **kwargs: Any) -> Dict[str, Any]:
            calls.append(dict(kwargs))
            return {"available": True, "models": ["stt-test"], "source": "abstractvoice"}

    _patch_discovery_facade(monkeypatch, facade=StubDiscoveryFacade())

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/audio/transcriptions/models", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "abstractruntime.discovery_facade"
    assert body["models"] == ["stt-test"]
    assert calls == [{"base_url": None, "provider_api_key": None, "provider": None}]


def test_audio_music_providers_catalog_proxies_runtime_music_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Dict[str, Any]] = []

    class StubDiscoveryFacade:
        def list_music_providers(self, **kwargs: Any) -> Dict[str, Any]:
            calls.append(dict(kwargs))
            return {
                "available": True,
                "task": "text_to_music",
                "providers": ["acemusic"],
                "available_providers": ["acemusic"],
                "provider_details": [{"provider": "acemusic", "tasks": ["text_to_music"]}],
            }

    _patch_discovery_facade(monkeypatch, facade=StubDiscoveryFacade())

    client, headers = _client(tmp_path, monkeypatch)
    headers = {**headers, "X-AbstractCore-Provider-API-Key": "provider-secret"}
    with client:
        resp = client.get("/api/gateway/audio/music/providers?task=text_to_music&base_url=http://provider.test/v1", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "abstractruntime.discovery_facade"
    assert body["providers"] == ["acemusic"]
    assert body["provider_details"] == [{"provider": "acemusic", "tasks": ["text_to_music"]}]
    assert calls == [
        {
            "task": "text_to_music",
            "base_url": "http://provider.test/v1",
            "provider_api_key": "provider-secret",
        }
    ]


def test_audio_music_models_catalog_filters_by_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Dict[str, Any]] = []

    class StubDiscoveryFacade:
        def list_music_models(self, **kwargs: Any) -> Dict[str, Any]:
            calls.append(dict(kwargs))
            return {
                "available": True,
                "task": "text_to_music",
                "models": [{"provider": "acemusic", "id": "ace-step"}],
                "providers": ["acemusic"],
                "available_providers": ["acemusic"],
                "models_by_provider": {"acemusic": ["ace-step"]},
                "provider_models": [{"provider": "acemusic", "model": "ace-step", "id": "acemusic/ace-step"}],
            }

    _patch_discovery_facade(monkeypatch, facade=StubDiscoveryFacade())

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/audio/music/models?provider=acemusic", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "abstractruntime.discovery_facade"
    assert body["providers"] == ["acemusic"]
    assert body["models"] == ["ace-step"]
    assert calls == [
        {
            "task": "text_to_music",
            "base_url": None,
            "provider_api_key": None,
            "provider": "acemusic",
        }
    ]
