"""Round 6 (R6.1): ONE gateway API answers the effective voice routes.

The operator's Code showed "Gateway default · openai" for both text→speech and
speech→text on a gateway whose `output.voice` route is supertonic/supertonic-3
and whose `input.voice` route is faster-whisper/large-v3: the voice catalog
forwarded the speech engine's own opinion (AbstractVoice's hardcoded "openai"
fallback, listed first whenever OPENAI_API_KEY is in the environment).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

ROUTES = {
    "tts": {"provider": "supertonic", "model": "supertonic-3", "voice": "M3"},
    "stt": {"provider": "faster-whisper", "model": "large-v3"},
}


class _EngineSaysOpenAI:
    """The engine-side catalog the live gateway returned (2026-10-04 03:27)."""

    def get_voice_catalog(self, **_kwargs) -> Dict[str, Any]:
        return {
            "available": True,
            "engine_id": "openai",
            "active_tts_provider": "openai",
            "active_stt_provider": "openai",
            "tts_providers": ["openai", "supertonic", "piper"],
            "stt_providers": ["openai", "faster-whisper"],
            "profiles": [{"profile_id": "M3", "provider": "supertonic"}],
        }


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, routes: Dict[str, Dict[str, str]]):
    token = "t"
    flows = tmp_path / "flows"
    flows.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    # The live stack exports a key: openai is then listed first by the engine.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-used")
    monkeypatch.delenv("ABSTRACTCORE_SERVER_BASE_URL", raising=False)

    from abstractgateway.app import app
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_configured_voice_output_defaults", lambda kind: dict(routes.get(kind) or {}))
    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_discovery_facade", lambda: (_EngineSaysOpenAI(), None))
    # The served speech-input hint is AbstractCore's, computed from THIS host; these tests pin
    # it (the hint tests below set their own).
    import abstractcore.config.recommendations as core_rec

    monkeypatch.setattr(core_rec, "voice_input_hint", lambda route, host=None: None)
    return TestClient(app), {"Authorization": f"Bearer {token}"}


def test_voice_defaults_answers_the_configured_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, headers = _client(tmp_path, monkeypatch, ROUTES)
    with client:
        resp = client.get("/api/gateway/voice/defaults", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tts"] == {
        "route": "output.voice", "configured": True, "provider": "supertonic", "model": "supertonic-3", "voice": "M3",
    }
    assert body["stt"] == {"route": "input.voice", "configured": True, "provider": "faster-whisper", "model": "large-v3"}
    assert "openai" not in json.dumps(body)


def test_voice_catalog_never_says_openai_when_the_route_is_supertonic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, headers = _client(tmp_path, monkeypatch, ROUTES)
    with client:
        for query in ("compact=true", "providers_only=true&compact=true", ""):
            resp = client.get(f"/api/gateway/voice/voices?{query}", headers=headers)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["active_tts_provider"] == "supertonic", query
            assert body["active_stt_provider"] == "faster-whisper", query
            assert body["gateway_defaults"]["tts"]["provider"] == "supertonic", query
            assert body["gateway_defaults"]["stt"]["model"] == "large-v3", query


def test_unconfigured_routes_say_so_instead_of_the_engine_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, headers = _client(tmp_path, monkeypatch, {})
    with client:
        defaults = client.get("/api/gateway/voice/defaults", headers=headers).json()
        catalog = client.get("/api/gateway/voice/voices?compact=true", headers=headers).json()
    for kind in ("tts", "stt"):
        assert defaults[kind]["configured"] is False
        assert defaults[kind]["provider"] is None
        assert defaults[kind]["note"].startswith("No gateway default is set")
    assert "active_tts_provider" not in catalog
    assert "active_stt_provider" not in catalog
    assert catalog["gateway_defaults"]["tts"]["configured"] is False


# --- round 16: the served speech-input hint -----------------------------------------------


def test_voice_defaults_carries_cores_hint_verbatim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, headers = _client(tmp_path, monkeypatch, ROUTES)
    import abstractcore.config.recommendations as core_rec

    seen = []
    hint = {
        "code": "apple_gpu_engine",
        "sentence": "Runs on the processor: faster-whisper has no Apple GPU backend. mlx-whisper runs large-v3 ...",
        "route": {"key": "input.voice", "provider": "mlx-whisper", "model": "large-v3"},
    }

    def fake(route, host=None):
        seen.append(dict(route))
        return hint

    monkeypatch.setattr(core_rec, "voice_input_hint", fake)
    with client:
        body = client.get("/api/gateway/voice/defaults", headers=headers).json()
    assert body["stt"]["hint"] == hint
    assert {"provider": "faster-whisper", "model": "large-v3"} in seen
    assert "hint" not in body["tts"]
    # Nothing is applied: the route is still the stored one.
    assert (body["stt"]["provider"], body["stt"]["model"]) == ("faster-whisper", "large-v3")


def test_the_hint_is_cores_real_answer_on_an_apple_gpu_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractcore.config.recommendations as core_rec

    real = core_rec.voice_input_hint  # before _client pins it
    client, headers = _client(tmp_path, monkeypatch, ROUTES)
    import abstractcore.utils.host_profile as hp

    monkeypatch.setattr(
        hp, "host_profile",
        lambda **_k: {"os": "darwin", "arch": "arm64", "accelerator": "metal", "engines_installed": {"mlx-whisper": False}},
    )
    monkeypatch.setattr(core_rec, "voice_input_hint", real)
    with client:
        body = client.get("/api/gateway/voice/defaults", headers=headers).json()
    assert body["stt"]["hint"]["code"] == "apple_gpu_engine_not_installed"
    assert body["stt"]["hint"]["route"] is None


def test_static_transcription_list_offers_turbo_and_keeps_large_v3(monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    for engine in ("faster-whisper", "mlx-whisper"):
        monkeypatch.setattr(gateway_routes, "_resolved_voice_engine", lambda kind, e=engine: e)
        for key in ("ABSTRACTGATEWAY_VOICE_STT_MODEL", "ABSTRACTVOICE_STT_MODEL"):
            monkeypatch.delenv(key, raising=False)
        models = gateway_routes._static_transcription_models_response()["models"]
        assert "large-v3" in models and "large-v3-turbo" in models, engine
