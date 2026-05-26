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
        compact_resp = client.get("/api/gateway/voice/voices?compact=true", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "abstractruntime.discovery_facade"
    assert body["route_available"] is True
    expected_catalog = {
        "contract": "gateway_catalog_v1",
        "version": 1,
        "kind": "voices",
        "scope": "tts",
        "primary_items_field": "items",
        "source": "abstractgateway.catalog",
        "route_source": "abstractruntime.discovery_facade",
        "available": True,
        "route_available": True,
        "providers_only": False,
    }
    for key, value in expected_catalog.items():
        assert body["catalog"].get(key) == value
    assert "compact" not in body["catalog"]
    ids = {item.get("id") or item.get("profile_id") or item.get("voice_id") for item in body["profiles"]}
    assert {"coral", "verse"} <= ids
    item_ids = {item.get("id") for item in body["items"] if isinstance(item, dict)}
    assert {"coral", "verse"} <= item_ids
    assert all(item.get("voice_kind") == "profile" for item in body["items"])

    assert compact_resp.status_code == 200, compact_resp.text
    compact_body = compact_resp.json()
    assert compact_body["catalog"]["compact"] is True
    assert compact_body["catalog"]["kind"] == "voices"
    assert {item.get("id") for item in compact_body["items"] if isinstance(item, dict)} >= {"coral", "verse"}


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
    assert body["catalog"]["kind"] == "voices"
    assert body["catalog"]["scope"] == "tts"
    assert body["items"] == []


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


def test_voice_catalog_static_fallback_surfaces_omnivoice_languages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_module_available", lambda name: name == "omnivoice")
    monkeypatch.setattr(gateway_routes, "_omnivoice_language_ids", lambda: ["en", "fr"])

    body = gateway_routes._static_voice_catalog_response(provider="omnivoice")
    assert body["tts_providers"] == ["omnivoice"]
    assert body["tts_models_by_provider"] == {"omnivoice": ["en", "fr"]}
    assert body["tts_model_roles_by_provider"] == {"omnivoice": "language"}

    speech_models = gateway_routes._static_speech_models_response(provider="omnivoice")
    assert speech_models["models"] == ["en", "fr"]
    assert speech_models["tts_models_by_provider"] == {"omnivoice": ["en", "fr"]}
    assert speech_models["tts_model_roles_by_provider"] == {"omnivoice": "language"}


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
    assert body["catalog"]["route_source"] == "abstractruntime.discovery_facade"
    assert body["catalog"]["upstream_source"] == "abstractvoice"
    assert body["items"] == [{"profile_id": "coral", "id": "coral", "label": "coral", "voice_kind": "profile", "voice_kinds": ["profile"]}]
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


def test_speech_provider_only_catalog_uses_fast_static_provider_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    class StubDiscoveryFacade:
        def list_tts_models(self, **_kwargs: Any) -> Dict[str, Any]:
            raise AssertionError("provider-only lookup must not load the TTS model catalog")

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ABSTRACTVOICE_OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(gateway_routes, "_module_available", lambda name: name == "omnivoice")
    monkeypatch.setattr(gateway_routes, "_has_builtin_voice_profiles", lambda _engine: False)
    _patch_discovery_facade(monkeypatch, facade=StubDiscoveryFacade())

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/audio/speech/models?providers_only=true", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["catalog"]["kind"] == "providers"
    assert body["catalog"]["scope"] == "tts"
    assert body["catalog"]["providers_only"] is True
    assert body["models"] == []
    assert body["providers"] == ["omnivoice"]
    assert body["items"] == [{"id": "omnivoice", "label": "omnivoice", "provider": "omnivoice", "name": "omnivoice"}]


def test_transcription_provider_only_catalog_uses_fast_static_stt_provider_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    class StubDiscoveryFacade:
        def list_stt_models(self, **_kwargs: Any) -> Dict[str, Any]:
            raise AssertionError("provider-only lookup must not load the STT model catalog")

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ABSTRACTVOICE_OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(gateway_routes, "_module_available", lambda name: name == "faster_whisper")
    monkeypatch.setattr(gateway_routes, "_has_builtin_voice_profiles", lambda _engine: False)
    _patch_discovery_facade(monkeypatch, facade=StubDiscoveryFacade())

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/audio/transcriptions/models?providers_only=true", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["catalog"]["kind"] == "providers"
    assert body["catalog"]["scope"] == "stt"
    assert body["catalog"]["providers_only"] is True
    assert body["models"] == []
    assert body["providers"] == ["faster-whisper"]
    assert body["tts_providers"] == []
    assert body["stt_providers"] == ["faster-whisper"]
    assert body["items"] == [
        {"id": "faster-whisper", "label": "faster-whisper", "provider": "faster-whisper", "name": "faster-whisper"}
    ]


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
    assert speech.json()["catalog"]["kind"] == "models"
    assert speech.json()["items"] == [{"id": "tts-test", "label": "tts-test", "provider": "fake-tts"}]
    assert transcription.json()["items"] == [{"id": "stt-test", "label": "stt-test", "provider": "fake-stt"}]


def test_audio_speech_models_filter_removes_other_provider_items(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class StubDiscoveryFacade:
        def list_tts_models(self, **_kwargs: Any) -> Dict[str, Any]:
            return {
                "available": True,
                "providers": ["openai", "omnivoice"],
                "available_providers": ["openai", "omnivoice"],
                "models": ["tts-1"],
                "models_by_provider": {"openai": ["tts-1"]},
                "tts_models_by_provider": {"openai": ["tts-1"]},
                "provider_models": [{"provider": "openai", "model": "tts-1", "id": "openai/tts-1"}],
            }

    _patch_discovery_facade(monkeypatch, facade=StubDiscoveryFacade())

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/audio/speech/models?provider=omnivoice", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider"] == "omnivoice"
    assert body["models"] == []
    assert body["models_by_provider"] == {}
    assert body["tts_models_by_provider"] == {}
    assert body["provider_models"] == []
    assert body["items"] == []


def test_vision_catalog_rejects_unknown_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/vision/provider_models?task=unknown", headers=headers)

    assert resp.status_code == 400, resp.text


def test_vision_provider_catalog_items_use_available_providers_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubDiscoveryFacade:
        def list_vision_provider_models(self, **_kwargs) -> Dict[str, Any]:
            return {
                "available": True,
                "providers": ["openai", "mflux", "mlx-gen"],
                "available_providers": ["mlx-gen"],
                "models": [],
                "models_by_provider": {},
                "provider_models": [],
            }

    _patch_discovery_facade(monkeypatch, facade=StubDiscoveryFacade())

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get(
            "/api/gateway/vision/provider_models?task=text_to_image&providers_only=true",
            headers=headers,
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["available_providers"] == ["mlx-gen"]
    assert [item["id"] for item in body["items"]] == ["mlx-gen"]


def test_gateway_vision_catalog_routes_local_mflux_without_diffusers_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_has_local_mflux_preset", lambda model_id: str(model_id).endswith("flux.2-klein-9b-4bit"))

    item = gateway_routes._gateway_vision_provider_model_item(
        provider="huggingface",
        model_id="AbstractFramework/flux.2-klein-9b-4bit",
        task="text_to_image",
    )

    assert item["provider"] == "mlx-gen"
    assert item["backend"] == "mlx-gen"
    assert item["model"] == "mlx-gen/AbstractFramework/flux.2-klein-9b-4bit"
    assert item["routed_model"] == "mlx-gen/AbstractFramework/flux.2-klein-9b-4bit"
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
    assert body["catalog"]["kind"] == "models"
    assert body["catalog"]["scope"] == "vision"
    assert body["catalog"]["upstream_source"] == "abstractvision"
    assert body["items"] == [{"model_id": "flux-local", "id": "flux-local", "label": "flux-local"}]
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
    assert body["catalog"]["scope"] == "stt"
    assert body["catalog"]["upstream_source"] == "abstractvoice"
    assert body["items"] == [{"id": "stt-test", "label": "stt-test"}]
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
    assert body["catalog"] == {
        "contract": "gateway_catalog_v1",
        "version": 1,
        "kind": "providers",
        "scope": "music",
        "primary_items_field": "items",
        "source": "abstractgateway.catalog",
        "route_source": "abstractruntime.discovery_facade",
        "available": True,
        "route_available": True,
        "task": "text_to_music",
    }
    assert body["items"] == [{"provider": "acemusic", "tasks": ["text_to_music"], "id": "acemusic", "label": "acemusic", "name": "acemusic"}]
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
    assert body["catalog"]["kind"] == "models"
    assert body["catalog"]["scope"] == "music"
    assert body["catalog"]["provider"] == "acemusic"
    assert body["catalog"]["task"] == "text_to_music"
    assert body["items"] == [{"provider": "acemusic", "model": "ace-step", "id": "ace-step", "label": "ace-step", "tasks": ["text_to_music"]}]
    assert calls == [
        {
            "task": "text_to_music",
            "base_url": None,
            "provider_api_key": None,
            "provider": "acemusic",
        }
    ]


def test_embedding_model_catalog_filters_embedding_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Dict[str, Any]] = []

    class StubDiscoveryFacade:
        def list_embedding_models(self, **kwargs: Any) -> Dict[str, Any]:
            calls.append(dict(kwargs))
            return {
                "available": True,
                "scope": "embedding.text",
                "providers": ["lmstudio"],
                "available_providers": ["lmstudio"],
                "embedding_providers": ["lmstudio"],
                "models": ["bge-small-en-v1.5"],
                "embedding_models": ["bge-small-en-v1.5"],
                "models_by_provider": {"lmstudio": ["bge-small-en-v1.5"]},
                "embedding_models_by_provider": {"lmstudio": ["bge-small-en-v1.5"]},
                "provider_models": [{"provider": "lmstudio", "model": "bge-small-en-v1.5", "id": "lmstudio/bge-small-en-v1.5"}],
            }

    _patch_discovery_facade(monkeypatch, facade=StubDiscoveryFacade())

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/embeddings/models?provider=lmstudio", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "abstractruntime.discovery_facade"
    assert body["catalog"]["kind"] == "models"
    assert body["catalog"]["scope"] == "embedding.text"
    assert body["catalog"]["provider"] == "lmstudio"
    assert body["models"] == ["bge-small-en-v1.5"]
    assert body["items"] == [
        {
            "provider": "lmstudio",
            "model": "bge-small-en-v1.5",
            "id": "bge-small-en-v1.5",
            "label": "bge-small-en-v1.5",
            "tasks": ["embedding.text"],
        }
    ]
    assert calls == [
        {
            "base_url": None,
            "provider_api_key": None,
            "provider": "lmstudio",
            "providers_only": False,
        }
    ]


def test_embedding_provider_only_catalog_keeps_provider_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubDiscoveryFacade:
        def list_embedding_models(self, **_kwargs: Any) -> Dict[str, Any]:
            return {
                "available": True,
                "providers": ["huggingface", "lmstudio"],
                "available_providers": ["huggingface", "lmstudio"],
                "embedding_providers": ["huggingface", "lmstudio"],
                "provider_details": [
                    {"provider": "huggingface", "label": "HuggingFace"},
                    {"provider": "lmstudio", "label": "LMStudio"},
                ],
                "models": [],
                "embedding_models": [],
                "models_by_provider": {},
                "embedding_models_by_provider": {},
            }

    _patch_discovery_facade(monkeypatch, facade=StubDiscoveryFacade())

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/embeddings/models?providers_only=true", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["catalog"]["kind"] == "providers"
    assert body["catalog"]["scope"] == "embedding.text"
    assert body["catalog"]["providers_only"] is True
    assert body["models"] == []
    assert body["items"] == [
        {"provider": "huggingface", "label": "HuggingFace", "id": "huggingface", "name": "huggingface"},
        {"provider": "lmstudio", "label": "LMStudio", "id": "lmstudio", "name": "lmstudio"},
    ]
