"""The setup guide's cloud provider cards (Providers tab too): a pasted key is CHECKED before
anything is stored, an existing connection is NEVER overwritten, and the key never comes back.

The network is stubbed: the discovery facade answers as AbstractCore does (OpenAI's 401 quotes
a masked key; OpenRouter's GET /key refuses a wrong key). HOME is isolated by conftest, so the
Core-backed store written here is a scratch one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from test_gateway_provider_endpoint_profiles import _app_client

KEY = "sk-proj-cloudcard" + "7" * 24
HEADERS = {"Authorization": "Bearer admin-token"}
PROFILES = "/api/gateway/config/provider-endpoint-profiles"


def _stub(monkeypatch: pytest.MonkeyPatch, answer: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    class StubDiscoveryFacade:
        def list_providers(self, *, include_models: bool = False, **kwargs: Any) -> dict[str, Any]:
            return {"items": []}

        def list_provider_models(self, provider_name: str, **kwargs: Any) -> dict[str, Any]:
            calls.append({"provider_name": provider_name, **kwargs})
            if callable(answer):
                return answer(provider_name, kwargs)
            return dict(answer)

    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_discovery_facade", lambda: (StubDiscoveryFacade(), None))
    return calls


def _ids(client) -> list[str]:
    return [str(p.get("id")) for p in client.get(PROFILES, headers=HEADERS).json()["profiles"]]


def test_a_key_check_stores_nothing_and_never_echoes_the_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    masked = KEY[:8] + "*" * 20 + KEY[-4:]
    calls = _stub(monkeypatch, lambda name, kw: {"provider": name, "models": [], "error": f"Error code: 401 - Incorrect API key provided: {masked}. You can find your key at https://platform.openai.com"})
    with _app_client(tmp_path, monkeypatch) as client:
        before = _ids(client)
        res = client.post(f"{PROFILES}/discover-models", headers=HEADERS, json={"provider_family": "openai", "api_key": KEY})
        after = _ids(client)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is False and body["models"] == []
    assert body["error_kind"] == "auth"
    # The provider's masked echo and every fragment of the key are gone from the answer.
    assert KEY not in res.text and KEY[:8] not in res.text and KEY[-8:] not in res.text and "****" not in res.text
    assert "[key]" in body["error"]
    # The key reached the check, as a draft (no profile), and nothing was stored.
    assert [c["provider_api_key"] for c in calls if c["provider_name"] == "openai"] == [KEY]
    assert after == before
    assert not any(i.startswith("openai-key") for i in after)


def test_a_working_key_reports_its_model_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {"provider": "anthropic", "models": [f"claude-{i}" for i in range(48)]})
    with _app_client(tmp_path, monkeypatch) as client:
        res = client.post(f"{PROFILES}/discover-models", headers=HEADERS, json={"provider_family": "anthropic", "api_key": KEY})
    body = res.json()
    assert body["ok"] is True and len(body["models"]) == 48 and "error_kind" not in body
    assert KEY not in res.text


@pytest.mark.parametrize(
    "error, kind",
    [
        ("OpenRouter API error (401): User not found.", "auth"),
        ("Anthropic API error (401): invalid x-api-key", "auth"),
        ("Portkey API error (429): rate limit exceeded", "rate_limited"),
        ("Failed to connect: [Errno 8] nodename nor servname provided", "unreachable"),
        ("something else entirely", "other"),
        (None, "no_models"),
    ],
)
def test_a_failed_check_says_why(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Any, kind: str) -> None:
    _stub(monkeypatch, {"provider": "x", "models": [], "error": error})
    with _app_client(tmp_path, monkeypatch) as client:
        res = client.post(f"{PROFILES}/discover-models", headers=HEADERS, json={"provider_family": "openrouter", "api_key": KEY})
    assert res.json()["error_kind"] == kind


def test_create_only_never_overwrites_an_existing_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, {"provider": "openai", "models": ["gpt-x"]})
    first = {"id": "openai-key", "display_name": "OpenAI (API key)", "provider_family": "openai", "api_key": "sk-first-key-000000000000", "scope": "gateway"}
    with _app_client(tmp_path, monkeypatch) as client:
        assert client.post(PROFILES, headers=HEADERS, json=first).status_code == 200
        fp_before = next(p for p in client.get(PROFILES, headers=HEADERS).json()["profiles"] if p["id"] == "openai-key")["api_key_fingerprint"]
        clash = client.post(PROFILES, headers=HEADERS, json={**first, "display_name": "Overwritten", "api_key": KEY, "create_only": True})
        rows = client.get(PROFILES, headers=HEADERS).json()["profiles"]
        fresh = client.post(PROFILES, headers=HEADERS, json={**first, "id": "openai-key-2", "api_key": KEY, "create_only": True})
    assert clash.status_code == 409, clash.text
    assert clash.json()["detail"]["reason_code"] == "connection_exists"
    assert KEY not in clash.text
    kept = next(p for p in rows if p["id"] == "openai-key")
    assert kept["display_name"] == "OpenAI (API key)" and kept["api_key_fingerprint"] == fp_before
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["profile"]["id"] == "openai-key-2"
    # The saved answer carries a fingerprint, never the key.
    assert KEY not in fresh.text and fresh.json()["profile"]["api_key_set"] is True


def test_without_create_only_a_post_still_updates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The connection modal's Save keeps its upsert (backward compatible)."""
    _stub(monkeypatch, {"provider": "openai", "models": []})
    body = {"id": "custom", "display_name": "Custom", "provider_family": "openai-compatible", "base_url": "https://a.example.test/v1", "scope": "gateway"}
    with _app_client(tmp_path, monkeypatch) as client:
        assert client.post(PROFILES, headers=HEADERS, json=body).status_code == 200
        again = client.post(PROFILES, headers=HEADERS, json={**body, "display_name": "Custom 2"})
    assert again.status_code == 200 and again.json()["profile"]["display_name"] == "Custom 2"
