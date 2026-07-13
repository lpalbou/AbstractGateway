"""Spark/template gallery for the creation modal's template tab (plan (b),
gateway c872).

GET /api/gateway/entities/templates serves the shipped framework default +
any operator YAML templates under <data_dir>/entity_templates/ — entity
independent (the modal reads it before the entity exists), read-only, and
degrades past an unreadable operator file with a labeled warning.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")

_TOKEN = "entity-templates-secret"


@pytest.fixture(autouse=True)
def _auth(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def test_gallery_serves_the_framework_default_with_locked_core_values() -> None:
    with _client() as client:
        r = client.get("/api/gateway/entities/templates")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["schema_version"] == 1
        ids = {t["id"] for t in body["templates"]}
        assert "framework-default" in ids

        builtin = next(t for t in body["templates"] if t["id"] == "framework-default")
        assert builtin["source"] == "builtin"
        assert builtin["spark"]["name"] == "", "the operator fills the name in the modal"
        # The core values are surfaced so the modal can LOCK them (non-removable).
        assert "shared_vulnerability" in builtin["core_values"]
        # The full spark document rides along for the expert tab to edit.
        assert "values" in builtin["spark"] and builtin["spark"]["values"]


def test_the_default_template_creates_a_real_entity() -> None:
    # The gallery output is directly usable as a create payload — pick a
    # template, name it, POST. End-to-end proof the template tab's fast path
    # produces a real entity.
    with _client() as client:
        gallery = client.get("/api/gateway/entities/templates").json()
        spark = next(t for t in gallery["templates"] if t["id"] == "framework-default")["spark"]
        spark = dict(spark)
        spark["name"] = "Castor"

        created = client.post("/api/gateway/entities", json={"name": "Castor", "spark": spark})
        assert created.status_code == 201, created.text
        listed = client.get("/api/gateway/entities").json()
        assert any(e.get("slug") == "castor" for e in listed["entities"])


def test_operator_templates_are_served_and_a_bad_one_is_skipped(tmp_path) -> None:
    # Operator drops YAML templates under <data_dir>/entity_templates/; one is
    # valid, one is garbage — the gallery serves the good one and labels the
    # skip, never failing the whole gallery.
    tdir = tmp_path / "runtime" / "entity_templates"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "scholar.yaml").write_text(
        "_template_name: Scholar\n"
        "_template_description: A studious companion\n"
        "name: ''\n"
        "origin: A scholar template\n"
        "values:\n"
        "  - name: shared_vulnerability\n"
        "    class: core\n"
        "    statement: shared substrate\n"
        "  - name: curiosity\n"
        "    class: revisable\n"
        "    statement: follow questions\n",
        encoding="utf-8",
    )
    (tdir / "broken.yaml").write_text(": : not valid yaml : :\n[unclosed", encoding="utf-8")

    with _client() as client:
        body = client.get("/api/gateway/entities/templates").json()
        ids = {t["id"] for t in body["templates"]}
        assert "scholar" in ids and "framework-default" in ids
        scholar = next(t for t in body["templates"] if t["id"] == "scholar")
        assert scholar["source"] == "operator"
        assert scholar["name"] == "Scholar"
        assert "shared_vulnerability" in scholar["core_values"]
        # The internal _template_* keys are stripped from the served spark.
        assert "_template_name" not in scholar["spark"]
        # The broken file is skipped with a labeled warning, gallery still serves.
        assert any("broken.yaml" in w for w in body["warnings"])


def test_templates_route_requires_auth() -> None:
    from abstractgateway.app import app

    with TestClient(app) as client:
        r = client.get("/api/gateway/entities/templates")
        assert r.status_code in (401, 403), r.text


def test_colliding_template_ids_are_skipped_loudly(tmp_path) -> None:
    # Adversary F4: the console picker selects by id, so a second entry with
    # the same id would be silently unreachable. An operator file whose stem
    # collides with the builtin id (or an earlier file) is skipped with a
    # labeled warning instead of shadowing.
    tdir = tmp_path / "runtime" / "entity_templates"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "framework-default.yaml").write_text(
        "name: ''\nvalues: []\n", encoding="utf-8"
    )
    (tdir / "twin.yaml").write_text("name: ''\nvalues: []\n", encoding="utf-8")
    (tdir / "twin.yml").write_text("name: ''\nvalues: []\n", encoding="utf-8")

    with _client() as client:
        body = client.get("/api/gateway/entities/templates").json()
        ids = [t["id"] for t in body["templates"]]
        assert len(ids) == len(set(ids)), f"served ids must be unique: {ids}"
        # The builtin wins its id; exactly one twin is served.
        builtin = next(t for t in body["templates"] if t["id"] == "framework-default")
        assert builtin["source"] == "builtin"
        assert ids.count("twin") == 1
        assert any("framework-default.yaml" in w for w in body["warnings"])
        assert any("twin.yml" in w for w in body["warnings"])


def test_route_literal_segments_are_reserved_entity_names() -> None:
    # Adversary F3: an entity named after a literal segment declared before
    # /{name} (templates, inventory) would be unreachable behind the route —
    # the slug boundary refuses those names, so the trap can never exist.
    from abstractgateway.entities import RESERVED_ENTITY_NAMES, entity_slug

    for reserved in ("templates", "inventory", "auth", "meets"):
        assert reserved in RESERVED_ENTITY_NAMES
        with pytest.raises(ValueError):
            entity_slug(reserved)

    with _client() as client:
        r = client.post("/api/gateway/entities", json={"name": "templates", "spark": {"name": "templates"}})
        assert r.status_code == 400, r.text
        assert "reserved" in r.text.lower() or "templates" in r.text
