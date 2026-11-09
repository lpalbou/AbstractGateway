"""`POST /sessions/{sid}/mentions` with a name no entity can have (review 2026-11-09 finding 4).

`{"entity": "@Castor"}` (or a reserved word, or any name that does not normalize) used to answer
500 with the internal text `Internal error: ValueError: entity name '@Castor' does not normalize
...` (gw_probe PROBE_A): `manifest_for` raised ValueError and the route caught only KeyError. Such a
name is simply not an entity: the same 404 `entity_not_found` sentence as a missing or hidden one.
A malformed body stays FastAPI's 422."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")

_TOKEN = "mention-invalid-names-secret"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"}, raise_server_exceptions=False) as c:
        yield c


def _mention(client, entity, *, text="hi", **extra):
    return client.post("/api/gateway/sessions/sess-invalid/mentions",
                       json={"entity": entity, "text": text, "mention_index": 0, **extra})


@pytest.mark.parametrize("name", ["@Castor", "auth", "templates", "a/b", "-castor", "x" * 65, "   ", "Cas tor!"])
def test_a_name_no_entity_can_have_is_not_found_never_a_500(client, name) -> None:
    r = _mention(client, name)
    assert r.status_code == 404, (name, r.status_code, r.text)
    body = r.json()
    assert set(body) == {"detail", "code"} and body["code"] == "entity_not_found", body
    assert r.headers.get("X-Gateway-Error-Code") == "entity_not_found"
    assert body["detail"].startswith("There is no entity called ") and body["detail"].endswith(".")
    for internal in ("Error", "normalize", "slug", "reserved", "Traceback"):
        assert internal not in body["detail"], (name, body)


def test_the_sentence_matches_a_missing_entitys(client) -> None:
    missing = _mention(client, "Pollux")
    assert missing.status_code == 404 and missing.json() == {"detail": "There is no entity called 'pollux'.", "code": "entity_not_found"}
    invalid = _mention(client, "@Castor")
    assert invalid.json() == {"detail": "There is no entity called '@Castor'.", "code": "entity_not_found"}


@pytest.mark.parametrize(
    "payload",
    [
        {"entity": "Castor"},  # no text
        {"text": "hi"},  # no entity
        {"entity": ["Castor"], "text": "hi"},
        {"entity": "Castor", "text": "hi", "mention_index": -1},
        {"entity": "", "text": "hi"},
    ],
)
def test_a_malformed_body_is_422(client, payload) -> None:
    r = client.post("/api/gateway/sessions/sess-invalid/mentions", json=payload)
    assert r.status_code == 422, (payload, r.status_code, r.text)


def test_a_body_that_is_not_json_is_422(client) -> None:
    r = client.post("/api/gateway/sessions/sess-invalid/mentions", content=b"@Castor hi",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 422, r.text
