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


# Cloud voice providers are ALWAYS listed (wave 2): marked needs_key until a
# key is configured (environment or the Providers screen).
def _cloud_items() -> list[dict[str, Any]]:
    return [
        {
            "id": pid, "provider": pid, "name": pid, "display_name": name,
            "label": name, "status": "needs an API key (add it under Providers)",
            "cloud": True, "needs_key": True, "key_source": None, "state": "needs_key",
            "reason": f"{name} is a cloud service: add its API key under Providers to use it",
        }
        for pid, name in (("openai", "OpenAI"), ("openai-compatible", "OpenAI-compatible"))
    ]


def _without_cloud_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OPENAI_API_KEY", "ABSTRACTVOICE_OPENAI_API_KEY", "ABSTRACTVOICE_REMOTE_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def _voice_runtimes(monkeypatch: pytest.MonkeyPatch, *installed: str) -> None:
    """Only `installed` local voice engines have their runtime (AbstractVoice's
    own status records, with `installed` and `reason` overridden)."""
    import dataclasses

    import abstractgateway.routes.gateway as gateway_routes
    from abstractvoice import engine_runtime as er

    def fake(engine: str, kind: str):
        st = er.engine_runtime_status(engine, kind=kind)
        on = st.remote or st.engine in installed
        return dataclasses.replace(
            st,
            installed=on,
            missing_modules=() if on else st.required_modules,
            reason=None if on else f"{st.label} is not installed. Install it with: {st.install_command}",
        )

    monkeypatch.setattr(gateway_routes, "_voice_engine_runtime", fake)


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


def test_voice_static_listings_never_list_an_engine_whose_runtime_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Supertonic's built-in voice styles WITHOUT onnxruntime were listed as a
    voice that could never speak (diag 2026-09-28: "Supertonic requires ONNX
    Runtime" at synthesis). AbstractVoice's engine_runtime is the one truth:
    missing runtime = not listed, and the reason is carried."""
    import abstractgateway.routes.gateway as gateway_routes

    _voice_runtimes(monkeypatch)  # nothing local installed
    # Isolate from the HOST's real abstractcore config (ambient-escape class).
    monkeypatch.setattr(gateway_routes, "_configured_voice_engine", lambda _kind: None)
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
    assert body["tts_providers"] == [] and body["profiles"] == []
    assert gateway_routes._static_speech_models_response(provider="supertonic")["providers"] == []
    providers = gateway_routes._static_voice_providers_only_response()
    assert "supertonic" not in providers["tts_providers"]
    record = providers["unavailable_providers"]["tts"]["supertonic"]
    assert record["code"] == "runtime_missing"
    assert record["reason"] == 'Supertonic is not installed. Install it with: pip install "abstractvoice[supertonic]"'
    assert record["runtime"]["extra"] == "supertonic"

    # With its runtime: listed, with the built-in voices.
    _voice_runtimes(monkeypatch, "supertonic")
    body = gateway_routes._static_voice_catalog_response(provider="supertonic")
    assert body["tts_providers"] == ["supertonic"]
    assert {"M1", "F5"} <= {item.get("profile_id") for item in body["profiles"]}
    assert body["tts_models_by_provider"] == {"supertonic": ["supertonic-3"]}
    assert gateway_routes._static_speech_models_response(provider="supertonic")["models"] == ["supertonic-3"]
    providers = gateway_routes._static_voice_providers_only_response()
    assert "supertonic" in providers["tts_providers"]
    assert "supertonic" not in providers["unavailable_providers"]["tts"]


def test_voice_static_listing_fails_loudly_without_the_voice_runtime_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    from fastapi import HTTPException

    import abstractgateway.routes.gateway as gateway_routes

    real_import = builtins.__import__

    def no_engine_runtime(name, *args, **kwargs):
        if name == "abstractvoice.engine_runtime":
            raise ImportError("No module named 'abstractvoice.engine_runtime'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_engine_runtime)
    with pytest.raises(HTTPException) as err:
        gateway_routes._voice_engine_installed("supertonic", "tts")
    assert err.value.status_code == 503 and "abstractvoice.engine_runtime" in err.value.detail


def test_voice_catalog_static_fallback_surfaces_omnivoice_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    _voice_runtimes(monkeypatch, "omnivoice")

    body = gateway_routes._static_voice_catalog_response(provider="omnivoice")
    assert body["tts_providers"] == ["omnivoice"]
    assert body["tts_models_by_provider"] == {"omnivoice": ["k2-fsa/OmniVoice"]}
    assert body["tts_model_roles_by_provider"] == {"omnivoice": "model"}

    speech_models = gateway_routes._static_speech_models_response(provider="omnivoice")
    assert speech_models["models"] == ["k2-fsa/OmniVoice"]
    assert speech_models["tts_models_by_provider"] == {"omnivoice": ["k2-fsa/OmniVoice"]}
    assert speech_models["tts_model_roles_by_provider"] == {"omnivoice": "model"}


def test_voice_catalog_static_fallback_surfaces_piper_and_audiodit_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    _voice_runtimes(monkeypatch, "piper", "audiodit")
    monkeypatch.setattr(
        gateway_routes,
        "_static_tts_model_ids_for_provider",
        lambda provider: {
            "piper": ["en_US-amy-medium"],
            "audiodit": ["meituan-longcat/LongCat-AudioDiT-1B"],
        }.get(str(provider), []),
    )
    monkeypatch.setattr(
        gateway_routes,
        "_static_piper_profile_records",
        lambda: [
            {
                "id": "amy",
                "profile_id": "amy",
                "voice_id": "amy",
                "label": "Piper amy",
                "provider": "piper",
                "engine_id": "piper",
            }
        ],
    )

    piper = gateway_routes._static_voice_catalog_response(provider="piper")
    assert piper["tts_models_by_provider"] == {"piper": ["en_US-amy-medium"]}
    assert piper["tts_voices_by_provider"] == {"piper": ["amy"]}

    audiodit = gateway_routes._static_speech_models_response(provider="audiodit")
    assert audiodit["models"] == ["meituan-longcat/LongCat-AudioDiT-1B"]
    assert audiodit["tts_models_by_provider"] == {"audiodit": ["meituan-longcat/LongCat-AudioDiT-1B"]}


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
            "voice_openai_api_key": None,  # no OpenAI key saved in Providers
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
    _voice_runtimes(monkeypatch, "omnivoice")
    # Host-config isolation (ambient-escape class): config-first engine
    # resolution reads the REAL capability defaults — a machine whose
    # output.voice names an engine (e.g. supertonic) would leak it into
    # the exact provider-list assertion below.
    monkeypatch.setattr(gateway_routes, "_configured_voice_engine", lambda _kind: None)
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
    assert body["providers"] == ["omnivoice", "openai", "openai-compatible"]
    assert body["items"] == [{"id": "omnivoice", "label": "omnivoice", "provider": "omnivoice", "name": "omnivoice"}, *_cloud_items()]


def test_speech_provider_only_catalog_uses_runtime_voice_catalog_when_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Dict[str, Any]] = []

    class StubDiscoveryFacade:
        def get_voice_catalog(self, **kwargs: Any) -> Dict[str, Any]:
            calls.append(dict(kwargs))
            return {
                "available": True,
                "providers": ["remote-tts"],
                "tts_providers": ["remote-tts"],
                "stt_providers": [],
            }

    _patch_discovery_facade(monkeypatch, facade=StubDiscoveryFacade())
    _without_cloud_keys(monkeypatch)

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/audio/speech/models?providers_only=true", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["providers"] == ["remote-tts", "openai", "openai-compatible"]
    assert body["tts_providers"] == ["remote-tts", "openai", "openai-compatible"]
    assert body["items"] == [{"id": "remote-tts", "label": "remote-tts", "provider": "remote-tts", "name": "remote-tts"}, *_cloud_items()]
    assert calls == [
        {
            "base_url": None,
            "provider_api_key": None,
            "voice_openai_api_key": None,  # no OpenAI key saved in Providers
            "provider": None,
            "model": None,
            "providers_only": True,
        }
    ]


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
    _voice_runtimes(monkeypatch, "faster-whisper")
    # Host-config isolation (ambient-escape class): a machine with a
    # configured input.voice/output.voice route would leak its engine into
    # the exact provider-list assertions below (this test passes on an
    # unconfigured-STT host by accident otherwise).
    monkeypatch.setattr(gateway_routes, "_configured_voice_engine", lambda _kind: None)
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
    assert body["providers"] == ["faster-whisper", "openai", "openai-compatible"]
    assert body["tts_providers"] == []
    assert body["stt_providers"] == ["faster-whisper", "openai", "openai-compatible"]
    assert body["items"] == [
        {"id": "faster-whisper", "label": "faster-whisper", "provider": "faster-whisper", "name": "faster-whisper"},
        *_cloud_items(),
    ]


def test_transcription_provider_only_catalog_uses_runtime_voice_catalog_when_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Dict[str, Any]] = []

    class StubDiscoveryFacade:
        def get_voice_catalog(self, **kwargs: Any) -> Dict[str, Any]:
            calls.append(dict(kwargs))
            return {
                "available": True,
                "providers": ["remote-tts"],
                "tts_providers": ["remote-tts"],
                "stt_providers": ["remote-stt"],
            }

    _patch_discovery_facade(monkeypatch, facade=StubDiscoveryFacade())
    _without_cloud_keys(monkeypatch)

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/audio/transcriptions/models?providers_only=true", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["providers"] == ["remote-stt", "openai", "openai-compatible"]
    assert body["available_providers"] == ["remote-stt", "openai", "openai-compatible"]
    assert body["tts_providers"] == []
    assert body["stt_providers"] == ["remote-stt", "openai", "openai-compatible"]
    assert body["items"] == [{"id": "remote-stt", "label": "remote-stt", "provider": "remote-stt", "name": "remote-stt"}, *_cloud_items()]
    assert calls == [
        {
            "base_url": None,
            "provider_api_key": None,
            "voice_openai_api_key": None,  # no OpenAI key saved in Providers
            "provider": None,
            "model": None,
            "providers_only": True,
        }
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


def test_vision_provider_catalog_accepts_image_upscale_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Dict[str, Any]] = []

    class StubDiscoveryFacade:
        def list_vision_provider_models(self, **kwargs) -> Dict[str, Any]:
            calls.append(dict(kwargs))
            return {
                "available": True,
                "task": "image_upscale",
                "providers": ["mlx-gen"],
                "available_providers": ["mlx-gen"],
                "models": [
                    {
                        "provider": "mlx-gen",
                        "model": "AbstractFramework/seedvr2-3b-8bit",
                        "id": "mlx-gen/AbstractFramework/seedvr2-3b-8bit",
                        "task": "image_upscale",
                    }
                ],
                "models_by_provider": {"mlx-gen": ["AbstractFramework/seedvr2-3b-8bit"]},
                "provider_models": [{"provider": "mlx-gen", "model": "AbstractFramework/seedvr2-3b-8bit"}],
            }

    _patch_discovery_facade(monkeypatch, facade=StubDiscoveryFacade())

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/vision/provider_models?task=image_upscale&provider=mlx-gen", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert calls[0]["task"] == "image_upscale"
    assert body["models_by_provider"] == {"mlx-gen": ["AbstractFramework/seedvr2-3b-8bit"]}
    assert body["catalog"]["task"] == "image_upscale"
    assert body["items"][0]["provider"] == "mlx-gen"


def test_vision_adapter_catalog_proxies_runtime_adapter_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Dict[str, Any]] = []

    class StubDiscoveryFacade:
        def list_vision_adapters(self, **kwargs: Any) -> Dict[str, Any]:
            calls.append(dict(kwargs))
            return {
                "available": True,
                "provider": "mlx-gen",
                "task": "text_to_video",
                "model": "AbstractFramework/wan2.2-t2v-a14b-diffusers-8bit",
                "adapters": [
                    {
                        "id": "documentary-motion",
                        "label": "Documentary Motion",
                        "provider": "mlx-gen",
                        "model": "AbstractFramework/wan2.2-t2v-a14b-diffusers-8bit",
                        "tasks": ["text_to_video", "image_to_video"],
                        "compatible_models": [
                            "AbstractFramework/wan2.2-t2v-a14b-diffusers-8bit",
                            "AbstractFramework/wan2.2-i2v-a14b-diffusers-8bit",
                        ],
                    }
                ],
            }

    _patch_discovery_facade(monkeypatch, facade=StubDiscoveryFacade())

    client, headers = _client(tmp_path, monkeypatch)
    headers = {**headers, "X-AbstractCore-Provider-API-Key": "provider-secret"}
    with client:
        resp = client.get(
            "/api/gateway/vision/adapters?task=text_to_video&provider=mlx-gen&model=AbstractFramework/wan2.2-t2v-a14b-diffusers-8bit&base_url=http://provider.test/v1",
            headers=headers,
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "abstractruntime.discovery_facade"
    assert body["route_available"] is True
    assert body["available"] is True
    assert body["catalog"]["kind"] == "adapters"
    assert body["catalog"]["scope"] == "vision"
    assert body["catalog"]["task"] == "text_to_video"
    assert body["catalog"]["provider"] == "mlx-gen"
    assert body["catalog"]["model"] == "AbstractFramework/wan2.2-t2v-a14b-diffusers-8bit"
    assert body["items"] == [
        {
            "id": "documentary-motion",
            "label": "Documentary Motion",
            "provider": "mlx-gen",
            "model": "AbstractFramework/wan2.2-t2v-a14b-diffusers-8bit",
            "tasks": ["text_to_video", "image_to_video"],
            "compatible_models": [
                "AbstractFramework/wan2.2-t2v-a14b-diffusers-8bit",
                "AbstractFramework/wan2.2-i2v-a14b-diffusers-8bit",
            ],
        }
    ]
    assert calls == [
        {
            "model": "AbstractFramework/wan2.2-t2v-a14b-diffusers-8bit",
            "task": "text_to_video",
            "base_url": "http://provider.test/v1",
            "provider_api_key": "provider-secret",
            "provider": "mlx-gen",
        }
    ]


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


def test_gateway_direct_image_configured_by_route_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Image availability is defined ONLY by the configured `output.image` route.

    Env vars neither enable it (no route + every ABSTRACTVISION_* export -> False)
    nor block it (route configured + no export at all -> True)."""
    import abstractgateway.routes.gateway as gateway_routes

    for name in (
        "ABSTRACTCORE_SERVER_BASE_URL",
        "ABSTRACTVISION_BACKEND",
        "ABSTRACTCORE_VISION_BACKEND",
        "ABSTRACTVISION_MFLUX_MODEL",
        "ABSTRACTVISION_MODEL_ID",
        "ABSTRACTVISION_BASE_URL",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "ABSTRACTVISION_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setattr(gateway_routes, "_configured_modality_route_provider", lambda modality, **_kw: None)
    monkeypatch.setenv("ABSTRACTVISION_BACKEND", "mlx-gen")
    monkeypatch.setenv("ABSTRACTVISION_MFLUX_MODEL", "flux")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert gateway_routes._gateway_direct_image_configured() is False

    for name in ("ABSTRACTVISION_BACKEND", "ABSTRACTVISION_MFLUX_MODEL", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        gateway_routes,
        "_configured_modality_route_provider",
        lambda modality, **_kw: "mlx-gen" if modality == "image" else None,
    )
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


def test_voice_providers_list_cloud_providers_needs_key_until_a_key_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /voice/voices?providers_only: openai / openai-compatible always
    listed; a key in the environment, or one stored through the Providers
    screen (an endpoint profile of that family), flips needs_key off."""

    class StubDiscoveryFacade:
        def get_voice_catalog(self, **_kwargs: Any) -> Dict[str, Any]:
            return {"available": True, "providers": ["supertonic"], "tts_providers": ["supertonic"], "stt_providers": []}

    _patch_discovery_facade(monkeypatch, facade=StubDiscoveryFacade())
    _without_cloud_keys(monkeypatch)
    client, headers = _client(tmp_path, monkeypatch)
    url = "/api/gateway/voice/voices?providers_only=true&compact=true"

    def states(body: Dict[str, Any]) -> Dict[str, tuple]:
        return {i["id"]: (i.get("needs_key"), i.get("key_source")) for i in body["items"]}

    with client:
        body = client.get(url, headers=headers).json()
        assert body["tts_providers"] == ["supertonic", "openai", "openai-compatible"]
        assert states(body) == {"supertonic": (None, None), "openai": (True, None), "openai-compatible": (True, None)}
        assert [d["provider"] for d in body["cloud_providers"]] == ["openai", "openai-compatible"]

        # A key stored through the Providers screen (endpoint profile, family openai).
        created = client.post(
            "/api/gateway/config/provider-endpoint-profiles",
            headers=headers,
            json={"id": "my-openai", "display_name": "My OpenAI", "provider_family": "openai",
                  "base_url": "https://api.openai.com/v1", "api_key": "sk-scratch-not-real", "scope": "gateway"},
        )
        assert created.status_code == 200, created.text
        try:
            body = client.get(url, headers=headers).json()
            assert states(body)["openai"] == (False, "providers")
            item = next(i for i in body["items"] if i["id"] == "openai")
            assert item["label"] == "OpenAI" and item["status"] == "ready" and item["state"] == "ready" and "Providers screen" in item["reason"]
            assert states(body)["openai-compatible"] == (True, None)
        finally:
            assert client.delete("/api/gateway/config/provider-endpoint-profiles/my-openai", headers=headers).status_code == 200

        monkeypatch.setenv("ABSTRACTVOICE_REMOTE_API_KEY", "scratch-not-real")
        body = client.get(url, headers=headers).json()
        assert states(body)["openai-compatible"] == (False, "environment")
        assert states(body)["openai"] == (True, None)

        # A provider filter keeps only that cloud provider.
        body = client.get(url + "&provider=openai", headers=headers).json()
        assert [i["id"] for i in body["items"]] == ["openai"]


def test_voice_cloud_provider_counts_an_abstractcore_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    _without_cloud_keys(monkeypatch)
    seen: list[tuple] = []

    def fake_key(provider_id: str, **kw: Any) -> tuple[str, str]:
        seen.append((provider_id, kw.get("include_env")))
        return ("sk-core", "abstractcore.config") if provider_id == "openai" else ("", "")

    import abstractgateway.provider_connections as provider_connections

    monkeypatch.setattr(provider_connections, "configured_provider_api_key", fake_key)
    client, _headers = _client(tmp_path, monkeypatch)
    with client:
        details = {d["provider"]: d for d in gateway_routes._voice_cloud_provider_details()}
    assert details["openai"]["needs_key"] is False and details["openai"]["key_source"] == "providers"
    assert details["openai-compatible"]["needs_key"] is True
    assert ("openai", False) in seen  # env is checked on its own, first


def test_voice_listings_pass_abstractvoice_unavailable_reasons_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AbstractVoice's `unavailable_providers` / `unavailable_reason` reach the
    consoles unchanged (compact included): they render the reason instead of
    "no voices"."""
    reason = 'Supertonic is not installed: the Python package onnxruntime is missing. Install it with: pip install "abstractvoice[supertonic]"'
    unavailable = {"tts": {"supertonic": {"provider": "supertonic", "code": "runtime_missing", "reason": reason}}, "stt": {}, "cloning": {}}

    class StubDiscoveryFacade:
        def get_voice_catalog(self, **_kwargs: Any) -> Dict[str, Any]:
            return {"available": False, "providers": [], "tts_providers": [], "profiles": [],
                    "unavailable_providers": unavailable, "unavailable_reason": reason}

    _patch_discovery_facade(monkeypatch, facade=StubDiscoveryFacade())
    _without_cloud_keys(monkeypatch)
    client, headers = _client(tmp_path, monkeypatch)
    with client:
        for url in (
            "/api/gateway/voice/voices?provider=supertonic&model=supertonic-3&compact=true",
            "/api/gateway/voice/voices?provider=supertonic",
            "/api/gateway/voice/voices?providers_only=true&compact=true",
        ):
            body = client.get(url, headers=headers).json()
            assert body["unavailable_reason"] == reason, url
            assert body["unavailable_providers"]["tts"]["supertonic"]["code"] == "runtime_missing", url


def test_a_cloud_voice_without_a_key_says_where_the_key_goes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class StubDiscoveryFacade:
        def get_voice_catalog(self, **_kwargs: Any) -> Dict[str, Any]:
            return {"available": False, "profiles": [], "unavailable_reason": "no OpenAI API key is configured"}

    _patch_discovery_facade(monkeypatch, facade=StubDiscoveryFacade())
    _without_cloud_keys(monkeypatch)
    client, headers = _client(tmp_path, monkeypatch)
    with client:
        body = client.get("/api/gateway/voice/voices?provider=openai&model=tts-1&compact=true", headers=headers).json()
        assert body["unavailable_reason"] == "OpenAI: needs an API key (add it under Providers)"
        body = client.get("/api/gateway/voice/voices?provider=supertonic&compact=true", headers=headers).json()
        assert body["unavailable_reason"] == "no OpenAI API key is configured"  # not a cloud filter: untouched



def test_a_providers_openai_key_reaches_the_voice_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Key saved through the Providers screen only (no env): every voice
    catalog call hands it to the runtime as AbstractVoice's host setting
    `voice_openai_api_key`, so OpenAI voices are listed; a key rotated in
    Providers reaches the next call."""
    seen: list = []

    class StubDiscoveryFacade:  # the runtime + AbstractVoice, faked at the seam
        def get_voice_catalog(self, **kwargs: Any) -> Dict[str, Any]:
            seen.append(kwargs.get("voice_openai_api_key"))
            if kwargs.get("voice_openai_api_key"):
                return {"available": True, "providers": ["openai"], "tts_providers": ["openai"],
                        "profiles": [{"id": "alloy", "profile_id": "alloy", "provider": "openai", "model": "tts-1"}]}
            return {"available": False, "providers": [], "tts_providers": [], "profiles": [],
                    "unavailable_reason": "no OpenAI API key is configured"}

    _patch_discovery_facade(monkeypatch, facade=StubDiscoveryFacade())
    _without_cloud_keys(monkeypatch)
    client, headers = _client(tmp_path, monkeypatch)
    url = "/api/gateway/voice/voices?provider=openai&model=tts-1&compact=true"
    profiles = "/api/gateway/config/provider-endpoint-profiles"
    with client:
        assert client.get(url, headers=headers).json()["items"] == [] and seen[-1] is None
        assert client.post(profiles, headers=headers, json={
            "id": "my-openai", "display_name": "My OpenAI", "provider_family": "openai",
            "base_url": "https://api.openai.com/v1", "api_key": "sk-providers-only", "scope": "gateway"}).status_code == 200
        try:
            body = client.get(url, headers=headers).json()
            assert seen[-1] == "sk-providers-only"
            assert [i["id"] for i in body["items"]] == ["alloy"]
            client.get("/api/gateway/audio/speech/models?providers_only=true", headers=headers)
            assert seen[-1] == "sk-providers-only"
            client.get("/api/gateway/audio/transcriptions/models?providers_only=true", headers=headers)
            assert seen[-1] == "sk-providers-only"
            # Rotated in Providers: the next call carries the new key.
            assert client.put(f"{profiles}/my-openai", headers=headers, json={"api_key": "sk-rotated"}).status_code == 200
            client.get(url, headers=headers)
            assert seen[-1] == "sk-rotated"
        finally:
            client.delete(f"{profiles}/my-openai", headers=headers)
