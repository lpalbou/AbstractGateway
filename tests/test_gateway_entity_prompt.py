"""The system-prompt door (maintainer, 2026-07-11: "a tab/badge for system
prompt that we could rewrite"): GET shows the layered truth (rendered
identity prelude read-only, editable layers with source, defaults, the
composed preview); PUT persists the operator overlay in the home
(<home>/system_prompt.yaml), unknown keys refused loudly.
"""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")

_TOKEN = "entity-prompt-shared-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)


def _spark(name: str = "Castor") -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def test_prompt_get_put_roundtrip():
    from abstractruntime.identity.chat import CONTRACT_PARAGRAPH

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        before = client.get("/api/gateway/entities/Castor/prompt").json()
        # Layer truth: defaults active, sources say so, prelude rendered.
        assert before["layers"]["conversation"]["source"] == "default"
        assert before["layers"]["conversation"]["text"] == CONTRACT_PARAGRAPH
        assert before["layers"]["operator"]["text"] == ""
        assert "You are Castor." in before["prelude"]
        # The preview is the actual next-summon composition (prelude + layers).
        assert before["preview"].startswith(before["prelude"][:40])
        assert CONTRACT_PARAGRAPH in before["preview"]
        assert set(before["editable"]) == {"conversation", "visit", "personal", "operator"}, (
            "ruled overlay spellings (runtime renamed the layer key same-wave)"
        )

        put = client.put(
            "/api/gateway/entities/Castor/prompt",
            json={"overlay": {"conversation": "Say what you know plainly.", "operator": "Prefer short answers."}},
        )
        assert put.status_code == 200, put.text
        after = put.json()
        assert after["layers"]["conversation"] == {"text": "Say what you know plainly.", "source": "overlay"}
        assert after["layers"]["operator"]["source"] == "overlay"
        assert "Say what you know plainly." in after["preview"]
        assert CONTRACT_PARAGRAPH not in after["preview"]
        assert after["preview"].endswith("STANDING INSTRUCTIONS FROM YOUR OPERATOR:\nPrefer short answers.")
        # visit layer untouched: still the default.
        assert after["layers"]["visit"]["source"] == "default"

        # The overlay reaches the NEXT summon composition (the home file is
        # what sessions read — assert through the runtime reader).
        from abstractgateway.service import get_gateway_service
        from abstractruntime.identity.prompt_overlay import read_prompt_overlay

        home_dir = get_gateway_service().entity_registry.entities_dir / "castor"
        assert read_prompt_overlay(home_dir) == {
            "conversation": "Say what you know plainly.",
            "operator": "Prefer short answers.",
        }

        # Revert: empty values delete layers; all-empty deletes the file.
        revert = client.put(
            "/api/gateway/entities/Castor/prompt",
            json={"overlay": {"conversation": "", "operator": ""}},
        )
        assert revert.status_code == 200
        assert revert.json()["layers"]["conversation"]["source"] == "default"
        assert read_prompt_overlay(home_dir) == {}


def test_prompt_put_lands_a_host_marker():
    """Operator prompt changes are host acts on the entity's story — the
    replay stream must show WHEN the standing instructions changed (layer
    names + content hashes; never the words themselves)."""
    import json

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        put = client.put(
            "/api/gateway/entities/Castor/prompt",
            json={"overlay": {"operator": "Prefer short answers.", "conversation": ""}},
        )
        assert put.status_code == 200

        from abstractgateway.service import get_gateway_service

        stream_path = get_gateway_service().entity_registry.entities_dir / ".host_stream" / "castor.jsonl"
        lines = [json.loads(l) for l in stream_path.read_text(encoding="utf-8").splitlines() if l.strip()]
        markers = [l for l in lines if l.get("payload", {}).get("kind") == "prompt_overlay_changed"]
        assert markers, "prompt change must land on the host stream"
        payload = markers[-1]["payload"]  # details splat into the payload (marker envelope shape)
        assert payload["channel"] == "operator"
        assert list(payload["layers"].keys()) == ["operator"]  # only the live overlay layers
        assert len(payload["layers"]["operator"]) == 8  # short hash, never words
        assert "Prefer short answers" not in json.dumps(payload)
        assert payload["reverted"] == ["conversation"]


def test_prompt_put_unknown_key_refuses():
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        refused = client.put(
            "/api/gateway/entities/Castor/prompt",
            json={"overlay": {"identity": "I am someone else now"}},
        )
        assert refused.status_code == 400
        assert "identity" in refused.json()["detail"]


def test_prompt_put_default_text_is_not_a_rewrite():
    """'Copy to editor' then save unchanged must keep the default LIVE
    (source=default), not freeze the built-in text as an overlay."""
    from abstractruntime.identity.chat import CONTRACT_PARAGRAPH

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        put = client.put(
            "/api/gateway/entities/Castor/prompt",
            json={"overlay": {"conversation": CONTRACT_PARAGRAPH}},
        )
        assert put.status_code == 200
        assert put.json()["layers"]["conversation"]["source"] == "default"

        from abstractgateway.service import get_gateway_service
        from abstractruntime.identity.prompt_overlay import read_prompt_overlay

        home_dir = get_gateway_service().entity_registry.entities_dir / "castor"
        assert read_prompt_overlay(home_dir) == {}


def test_prompt_put_oversized_layer_refuses():
    from abstractruntime.identity.prompt_overlay import MAX_LAYER_CHARS

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        refused = client.put(
            "/api/gateway/entities/Castor/prompt",
            json={"overlay": {"operator": "x" * (MAX_LAYER_CHARS + 1)}},
        )
        assert refused.status_code == 400
        assert "cap" in refused.json()["detail"]


def test_prompt_conversation_rewrite_without_diary_syntax_warns():
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        put = client.put(
            "/api/gateway/entities/Castor/prompt",
            json={"overlay": {"conversation": "Just talk. No election mechanics explained."}},
        )
        assert put.status_code == 200
        assert any("```diary" in w for w in put.json()["warnings"])


def test_prompt_unknown_entity_404s():
    with _client() as client:
        assert client.get("/api/gateway/entities/Ghost/prompt").status_code == 404
