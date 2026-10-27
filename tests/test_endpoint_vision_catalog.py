"""Endpoint profiles as image candidates in the Vision catalog.

Regression (operator report 2026-10-18): an OpenAI-compatible endpoint
connection serving `gpt-image-2` was never offered for text-to-image or image
edits in either console, because `GET /vision/provider_models` listed only
AbstractVision's own backends.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from abstractgateway.endpoint_vision_catalog import (
    declared_image_tasks,
    endpoint_profile_image_models,
    known_image_model_tasks,
    merge_endpoint_vision_catalog,
)

pytestmark = pytest.mark.basic

RELAY_MODELS = ["claude-opus-5", "gpt-5.6-sol", "gpt-image-2", "dall-e-3", "codex-auto-review"]


def _profile(**kw: Any) -> SimpleNamespace:
    base = dict(
        id="relay",
        virtual_provider_id="endpoint:relay",
        display_name="Relay",
        provider_family="openai-compatible",
        capabilities=("text",),
        enabled=True,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_known_image_families_and_their_tasks() -> None:
    assert known_image_model_tasks("gpt-image-2") == {"text_to_image", "image_to_image"}
    assert known_image_model_tasks("openai/gpt-image-1") == {"text_to_image", "image_to_image"}
    assert known_image_model_tasks("dall-e-2") == {"text_to_image", "image_to_image"}
    assert known_image_model_tasks("dall-e-3") == {"text_to_image"}
    for chat in ("gpt-5.6-sol", "claude-opus-5", "gpt-4o", ""):
        assert known_image_model_tasks(chat) == frozenset()


def test_declared_capabilities() -> None:
    assert declared_image_tasks(["text"]) == frozenset()
    assert declared_image_tasks(["text", "image"]) == {"text_to_image", "image_to_image"}
    assert declared_image_tasks(["Text-To-Image"]) == {"text_to_image"}
    assert declared_image_tasks(["output.image.image_to_image"]) == {"image_to_image"}


def test_profile_with_an_image_model_is_a_candidate_with_only_its_image_models() -> None:
    p = _profile()
    assert endpoint_profile_image_models(p, RELAY_MODELS, task="text_to_image") == ["dall-e-3", "gpt-image-2"]
    assert endpoint_profile_image_models(p, RELAY_MODELS, task="image_to_image") == ["gpt-image-2"]
    assert endpoint_profile_image_models(p, RELAY_MODELS, task=None) == ["dall-e-3", "gpt-image-2"]


def test_profile_without_image_evidence_is_not_a_candidate() -> None:
    p = _profile()
    assert endpoint_profile_image_models(p, ["claude-opus-5", "gpt-5.6-sol"], task="text_to_image") is None
    # Video and upscale are not served through an endpoint profile.
    assert endpoint_profile_image_models(p, RELAY_MODELS, task="text_to_video") is None
    assert endpoint_profile_image_models(p, RELAY_MODELS, task="image_upscale") is None


def test_family_and_enabled_gate_the_candidate() -> None:
    assert endpoint_profile_image_models(_profile(provider_family="openai"), ["gpt-image-1"], task="text_to_image") == ["gpt-image-1"]
    for family in ("anthropic", "lmstudio", "ollama", "openrouter", "vllm"):
        assert endpoint_profile_image_models(_profile(provider_family=family), ["gpt-image-1"], task="text_to_image") is None
    assert endpoint_profile_image_models(_profile(enabled=False), ["gpt-image-1"], task="text_to_image") is None


def test_declared_image_capability_offers_every_model() -> None:
    p = _profile(capabilities=("text", "image"))
    assert endpoint_profile_image_models(p, ["sd-turbo", "flux-dev"], task="image_to_image") == ["flux-dev", "sd-turbo"]
    # Declared but the model list could not be read: still a candidate (type the model).
    assert endpoint_profile_image_models(p, [], task="text_to_image") == []


def test_merge_adds_provider_rows_and_models() -> None:
    p = _profile()
    base = {"providers": ["huggingface"], "available_providers": ["huggingface"], "models_by_provider": {"huggingface": ["sd"]}, "models": []}
    out = merge_endpoint_vision_catalog(base, [(p, ["gpt-image-2"])], providers_only=False, task="text_to_image")
    assert out["available_providers"] == ["huggingface", "endpoint:relay"]
    assert out["models_by_provider"]["endpoint:relay"] == ["gpt-image-2"]
    assert [(m["provider"], m["id"]) for m in out["models"]] == [("endpoint:relay", "gpt-image-2")]
    assert out["endpoint_providers"][0]["label"] == "Relay"
    only = merge_endpoint_vision_catalog(base, [(p, ["gpt-image-2"])], providers_only=True, task="text_to_image")
    assert only["models"] == []


# ---------------------------------------------------------------------------
# The route
# ---------------------------------------------------------------------------


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


class _StubDiscovery:
    def __init__(self) -> None:
        self.vision_calls: List[Dict[str, Any]] = []
        self.model_calls: List[tuple] = []

    def list_vision_provider_models(self, **kwargs: Any) -> Dict[str, Any]:
        self.vision_calls.append(dict(kwargs))
        return {
            "available": True,
            "providers": ["openai", "openai-compatible", "huggingface"],
            "available_providers": ["huggingface"],
            "models": [],
            "models_by_provider": {"huggingface": ["stabilityai/sd-turbo"]},
            "provider_models": [],
        }

    def list_provider_models(self, provider: str, **kwargs: Any) -> Dict[str, Any]:
        self.model_calls.append((provider, kwargs.get("base_url"), kwargs.get("provider_api_key")))
        if kwargs.get("base_url") == "http://127.0.0.1:9/v1":
            return {"models": ["llama-3"]}
        return {"models": list(RELAY_MODELS)}


def _add_profile(client: TestClient, headers: dict, pid: str, base_url: str, **extra: Any) -> None:
    resp = client.post(
        "/api/gateway/config/provider-endpoint-profiles",
        headers=headers,
        json={"id": pid, "display_name": pid.title(), "provider_family": "openai-compatible",
              "base_url": base_url, "api_key": "sk-scratch-not-real", "scope": "gateway", **extra},
    )
    assert resp.status_code == 200, resp.text


def test_vision_catalog_offers_an_endpoint_profile_that_serves_an_image_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    stub = _StubDiscovery()
    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_discovery_facade", lambda: (stub, None))
    client, headers = _client(tmp_path, monkeypatch)
    with client:
        _add_profile(client, headers, "relay", "http://127.0.0.1:8317/v1")
        _add_profile(client, headers, "chatonly", "http://127.0.0.1:9/v1")
        for task in ("text_to_image", "image_to_image"):
            body = client.get(f"/api/gateway/vision/provider_models?task={task}&providers_only=true", headers=headers).json()
            ids = [item["id"] for item in body["items"]]
            assert "endpoint:relay" in ids and "huggingface" in ids, body
            assert "endpoint:chatonly" not in ids
            relay = next(item for item in body["items"] if item["id"] == "endpoint:relay")
            assert relay["label"] == "Relay"

        models = client.get(
            "/api/gateway/vision/provider_models?task=image_to_image&provider=endpoint:relay", headers=headers
        ).json()
        assert [item["id"] for item in models["items"]] == ["gpt-image-2"]
        assert models["models_by_provider"] == {"endpoint:relay": ["gpt-image-2"]}

        t2i = client.get(
            "/api/gateway/vision/provider_models?task=text_to_image&provider=endpoint:relay", headers=headers
        ).json()
        assert sorted(item["id"] for item in t2i["items"]) == ["dall-e-3", "gpt-image-2"]

        # A filter on an AbstractVision backend carries no endpoint rows.
        hf = client.get("/api/gateway/vision/provider_models?task=text_to_image&provider=huggingface", headers=headers).json()
        assert "endpoint:relay" not in hf["models_by_provider"]

        # Video tasks are untouched and probe nothing.
        stub.model_calls.clear()
        video = client.get("/api/gateway/vision/provider_models?task=text_to_video&providers_only=true", headers=headers).json()
        assert "endpoint:relay" not in [item["id"] for item in video["items"]]
        assert stub.model_calls == []


def test_vision_catalog_endpoint_probe_uses_profile_connection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    stub = _StubDiscovery()
    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_discovery_facade", lambda: (stub, None))
    client, headers = _client(tmp_path, monkeypatch)
    with client:
        _add_profile(client, headers, "relay", "http://127.0.0.1:8317/v1")
        stub.model_calls.clear()
        client.get("/api/gateway/vision/provider_models?task=text_to_image&providers_only=true", headers=headers)
    assert stub.model_calls == [("openai-compatible", "http://127.0.0.1:8317/v1", "sk-scratch-not-real")]


def test_vision_catalog_endpoint_allowed_models_need_no_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    stub = _StubDiscovery()
    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_discovery_facade", lambda: (stub, None))
    client, headers = _client(tmp_path, monkeypatch)
    with client:
        _add_profile(client, headers, "pinned", "http://127.0.0.1:8317/v1", allowed_models=["gpt-image-1", "gpt-5"])
        stub.model_calls.clear()
        body = client.get("/api/gateway/vision/provider_models?task=text_to_image&provider=endpoint:pinned", headers=headers).json()
    assert [item["id"] for item in body["items"]] == ["gpt-image-1"]
    assert stub.model_calls == []  # the allowed models are the list: no model listing
    # A NAMED endpoint's own catalog is read through the profile's connection, for its per-model word.
    assert [(c["provider"], c["base_url"], c["provider_api_key"]) for c in stub.vision_calls] == [
        ("openai-compatible", "http://127.0.0.1:8317/v1", "sk-scratch-not-real")
    ]


# ---------------------------------------------------------------------------
# A named endpoint: its own catalog's word first, the model name otherwise
# ---------------------------------------------------------------------------


def _relay_row(model: str, images: Any = None, **extra: Any) -> Dict[str, Any]:
    row: Dict[str, Any] = {"id": model, **extra}
    if images is not None:
        row["raw"] = {"airelays": {"capabilities": {"routes": {"images": images}}}}
    return row


def test_endpoint_evidence_reads_relay_routes_then_declared_tasks() -> None:
    from abstractgateway.endpoint_vision_catalog import endpoint_model_evidence

    assert endpoint_model_evidence(_relay_row("gpt-image-2", True), "text_to_image") == ("gpt-image-2", True)
    assert endpoint_model_evidence(_relay_row("gpt-image-2", False), "text_to_image") == ("gpt-image-2", False)
    assert endpoint_model_evidence({"id": "my-painter", "capabilities": ["text_to_image"]}, "text_to_image") == ("my-painter", True)
    assert endpoint_model_evidence({"id": "my-painter", "capabilities": ["text_to_image"]}, "image_to_image") == ("my-painter", False)
    assert endpoint_model_evidence({"model": "openai-compatible/gpt-image-2"}, None) == ("gpt-image-2", None)
    assert endpoint_model_evidence({"id": "unknown"}, "text_to_image") == ("unknown", None)


def test_endpoint_evidence_adds_removes_and_otherwise_keeps_the_name_rule() -> None:
    from abstractgateway.endpoint_vision_catalog import apply_endpoint_evidence

    class _Profile:
        allowed_models: tuple = ()

    rows = [
        _relay_row("gpt-image-2", False),  # the name says image, the relay says no: removed
        {"id": "painter-x", "tasks": ["text_to_image"]},  # an unusual name the relay vouches for: added
        {"id": "dall-e-3"},  # the relay says nothing: the name rule's answer stands
    ]
    named = ["dall-e-3", "gpt-image-2"]
    assert apply_endpoint_evidence(_Profile(), named, rows, task="text_to_image") == ["dall-e-3", "painter-x"]
    pinned = _Profile()
    pinned.allowed_models = ("painter-x",)
    assert apply_endpoint_evidence(pinned, named, rows, task="text_to_image") == ["painter-x"]
    assert apply_endpoint_evidence(_Profile(), named, [], task="text_to_image") == ["dall-e-3", "gpt-image-2"]


def test_a_named_endpoint_that_does_not_exist_is_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    stub = _StubDiscovery()
    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_discovery_facade", lambda: (stub, None))
    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/vision/provider_models?task=text_to_image&provider=endpoint:nobody", headers=headers)
    assert resp.status_code in (400, 404), resp.text
    assert stub.vision_calls == []


def test_endpoint_evidence_keeps_an_organisation_prefixed_model_id_whole() -> None:
    from abstractgateway.endpoint_vision_catalog import apply_endpoint_evidence, endpoint_model_evidence

    assert endpoint_model_evidence({"id": "black-forest-labs/FLUX.1-schnell", "capabilities": ["text_to_image"]}, "text_to_image") == (
        "black-forest-labs/FLUX.1-schnell", True
    )
    assert endpoint_model_evidence({"id": "openai-compatible/gpt-image-2"}, None) == ("gpt-image-2", None)
    assert endpoint_model_evidence({"id": "myrelay/gpt-image-2", "provider": "myrelay"}, None) == ("gpt-image-2", None)

    class _Profile:
        allowed_models = ("black-forest-labs/FLUX.1-schnell",)

    rows = [_relay_row("black-forest-labs/FLUX.1-schnell", True), {"id": "stabilityai/sdxl", "capabilities": ["chat"]}]
    assert apply_endpoint_evidence(_Profile(), [], rows, task="text_to_image") == ["black-forest-labs/FLUX.1-schnell"]
