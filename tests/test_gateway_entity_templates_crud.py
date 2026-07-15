"""Versioned spark templates (operator directive 2026-07-13: "if it's a
template, i must be able to create new ones and modify them later on - every
template should be versioned").

Pins: the builtin is the untouchable floor; create writes v1; edit appends
v2 (append-only, history intact, old version still readable); lint refuses a
core-value strip at SAVE (never at a later summon); editing a template never
touches a living entity (blueprint, not a live link); mutations are
admin-gated, views are user-level.
"""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")

_TOKEN = "template-crud-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _spark() -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    return copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))


def test_create_edit_version_and_view():
    with _client() as client:
        # The gallery starts with the builtin floor, marked non-editable.
        gallery = client.get("/api/gateway/entities/templates").json()["templates"]
        builtin = [t for t in gallery if t["id"] == "framework-default"][0]
        assert builtin["editable"] is False and builtin["source"] == "builtin"

        # CREATE a template seeded from the builtin (v1).
        spark = _spark()
        r = client.post("/api/gateway/entities/templates", json={
            "id": "researcher", "spark": spark, "name": "Researcher",
            "description": "curious explorer", "note": "first cut",
        })
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["version"] == 1 and body["editable"] is True

        # VIEW the full spark (not just a description).
        view = client.get("/api/gateway/entities/templates/researcher").json()
        assert view["spark"]["values"], "the full spark document is served"
        assert view["version"] == 1

        # EDIT → appends v2 (append-only); v1 stays readable.
        spark2 = _spark()
        spark2["purposes"] = [{"name": "map-the-unknown", "statement": "chart what is not yet understood"}]
        e = client.put("/api/gateway/entities/templates/researcher", json={
            "id": "researcher", "spark": spark2, "name": "Researcher", "note": "added a purpose",
        })
        assert e.status_code == 200, e.text
        assert e.json()["version"] == 2

        versions = client.get("/api/gateway/entities/templates/researcher/versions").json()["versions"]
        assert [v["version"] for v in versions] == [1, 2]
        assert versions[1]["note"] == "added a purpose"

        # The OLD version is still readable verbatim (append-only history).
        v1 = client.get("/api/gateway/entities/templates/researcher?version=1").json()
        assert v1["version"] == 1
        # v1 had no such purpose; v2 does — history is intact, not overwritten.
        v1_purpose_names = [str(p.get("name")) for p in (v1["spark"].get("purposes") or [])]
        assert "map-the-unknown" not in v1_purpose_names


def test_builtin_is_the_untouchable_floor():
    with _client() as client:
        # Cannot create with the builtin's id, cannot edit the builtin.
        r = client.post("/api/gateway/entities/templates", json={"id": "framework-default", "spark": _spark()})
        assert r.status_code == 400 and "floor" in r.json()["detail"]
        r2 = client.put("/api/gateway/entities/templates/framework-default", json={"id": "framework-default", "spark": _spark()})
        assert r2.status_code == 400


def test_lint_refuses_a_core_value_strip_at_save():
    """The operator's worst UX (discovering a broken template at summon) is
    closed: a spark that strips the shared_vulnerability core value refuses
    at SAVE, loudly, never landing."""
    with _client() as client:
        spark = _spark()
        spark["values"] = [v for v in spark["values"] if str(v.get("class")) != "core"]
        r = client.post("/api/gateway/entities/templates", json={"id": "broken", "spark": spark})
        assert r.status_code == 400, r.text
        assert "lint" in r.json()["detail"].lower()
        # And nothing was written — the gallery has no 'broken'.
        gallery = client.get("/api/gateway/entities/templates").json()["templates"]
        assert not any(t["id"] == "broken" for t in gallery)


def test_edit_does_not_touch_a_living_entity(monkeypatch: pytest.MonkeyPatch):
    """A template is a BLUEPRINT copied at summon, never a live link: editing
    it after an entity is created leaves the entity's engraved spark
    untouched (the create path copies + engrams)."""
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    with _client() as client:
        client.post("/api/gateway/entities/templates", json={"id": "seedtpl", "spark": _spark(), "name": "Seed"})
        seed = client.get("/api/gateway/entities/templates/seedtpl").json()["spark"]
        seed["name"] = "Castor"
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": seed}).status_code == 201
        before = client.get("/api/gateway/entities/Castor/card").json().get("entity_id")

        # Edit the template heavily.
        spark2 = _spark()
        spark2["traits"] = [{"name": "meticulous", "statement": "checks twice"}]
        assert client.put("/api/gateway/entities/templates/seedtpl", json={"id": "seedtpl", "spark": spark2}).status_code == 200

        # The living entity is unchanged (its spark was engraved at summon).
        after = client.get("/api/gateway/entities/Castor/card").json().get("entity_id")
        assert before == after == "entity:castor"
