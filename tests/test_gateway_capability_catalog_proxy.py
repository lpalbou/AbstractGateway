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


def test_voice_catalog_uses_local_capability_profiles_without_core_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_BASE_URL", raising=False)
    monkeypatch.delenv("ABSTRACTCORE_SERVER_BASE_URL", raising=False)

    class FakeVoice:
        def voice_catalog(self) -> Dict[str, Any]:
            return {
                "profiles": [{"profile_id": "coral"}, {"profile_id": "verse"}],
                "tts_models": ["gpt-4o-mini-tts"],
            }

    class FakeRegistry:
        voice = FakeVoice()

    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_capability_registry", lambda **_kwargs: FakeRegistry())

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/voice/voices", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "abstractvoice_local"
    assert body["route_available"] is True
    ids = {item.get("id") or item.get("profile_id") or item.get("voice_id") for item in body["profiles"]}
    assert {"coral", "verse"} <= ids


def test_voice_catalog_static_fallback_surfaces_configured_env_voices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_BASE_URL", raising=False)
    monkeypatch.delenv("ABSTRACTCORE_SERVER_BASE_URL", raising=False)
    monkeypatch.setenv("ABSTRACTVOICE_OPENAI_TTS_VOICES", "coral,verse")

    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(
        gateway_routes,
        "_gateway_capability_registry",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("local voice unavailable")),
    )

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/voice/voices", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "gateway_static"
    assert body["route_available"] is True
    ids = {item.get("id") or item.get("profile_id") or item.get("voice_id") for item in body["profiles"]}
    assert {"coral", "verse"} <= ids


def test_voice_catalog_proxies_configured_core_catalog_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Dict[str, Any]] = []

    def fake_fetch(path: str, *, query: Optional[dict] = None, provider_api_key: Optional[str] = None) -> Dict[str, Any]:
        calls.append({"path": path, "query": dict(query or {}), "provider_api_key": provider_api_key})
        return {"available": True, "profiles": [{"profile_id": "coral"}], "source": "abstractvoice"}

    monkeypatch.setenv("ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_BASE_URL", "http://core.test/v1")

    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "fetch_core_catalog_json", fake_fetch)

    client, headers = _client(tmp_path, monkeypatch)
    headers = {**headers, "X-AbstractCore-Provider-API-Key": "provider-secret"}
    with client:
        resp = client.get("/api/gateway/voice/voices?base_url=http://provider.test/v1", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "abstractcore_server"
    assert body["route_available"] is True
    assert body["profiles"] == [{"profile_id": "coral"}]
    assert calls == [
        {
            "path": "/audio/voices",
            "query": {"base_url": "http://provider.test/v1"},
            "provider_api_key": "provider-secret",
        }
    ]


def test_catalog_proxy_preserves_core_auth_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.capability_catalog import CoreCatalogProxyError

    def fake_fetch(*_args: Any, **_kwargs: Any) -> Dict[str, Any]:
        raise CoreCatalogProxyError(status_code=401, detail={"error": {"message": "core auth required"}})

    monkeypatch.setenv("ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_BASE_URL", "http://core.test/v1")

    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "fetch_core_catalog_json", fake_fetch)

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/audio/speech/models", headers=headers)

    assert resp.status_code == 401, resp.text
    assert "core auth required" in resp.text


def test_vision_catalog_rejects_unknown_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/vision/provider_models?task=unknown", headers=headers)

    assert resp.status_code == 400, resp.text


def test_vision_models_catalog_proxies_configured_core_catalog_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Dict[str, Any]] = []

    def fake_fetch(path: str, *, query: Optional[dict] = None, provider_api_key: Optional[str] = None) -> Dict[str, Any]:
        calls.append({"path": path, "query": dict(query or {}), "provider_api_key": provider_api_key})
        return {"available": True, "models": [{"model_id": "flux-local"}], "source": "abstractvision"}

    monkeypatch.setenv("ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_BASE_URL", "http://core.test/v1")

    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "fetch_core_catalog_json", fake_fetch)

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/vision/models", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "abstractcore_server"
    assert body["route_available"] is True
    assert body["models"] == [{"model_id": "flux-local"}]
    assert calls == [{"path": "/vision/models", "query": {}, "provider_api_key": None}]


def test_audio_transcription_models_catalog_proxies_configured_core_catalog_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Dict[str, Any]] = []

    def fake_fetch(path: str, *, query: Optional[dict] = None, provider_api_key: Optional[str] = None) -> Dict[str, Any]:
        calls.append({"path": path, "query": dict(query or {}), "provider_api_key": provider_api_key})
        return {"available": True, "models": ["stt-test"], "source": "abstractvoice"}

    monkeypatch.setenv("ABSTRACTGATEWAY_ABSTRACTCORE_SERVER_BASE_URL", "http://core.test/v1")

    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "fetch_core_catalog_json", fake_fetch)

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/audio/transcriptions/models", headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "abstractcore_server"
    assert body["models"] == ["stt-test"]
    assert calls == [{"path": "/audio/transcriptions/models", "query": {}, "provider_api_key": None}]
