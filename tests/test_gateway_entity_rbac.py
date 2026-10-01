"""Entity visibility by role (operator ruling 2026-10-01): an ADMIN sees all users and entities;
a NON-ADMIN sees only themself and the entities THEY created — enforced in the API.

Setup: alice creates E1 ("Aster"), bob creates E2 ("Borea") through `POST /entities`; E0
("Olden") is a legacy home created with no recorded creator. All three live in ONE entity
registry (the worst case: no per-runtime separation helps), so every assertion below proves the
`created_by` filter itself. A hidden entity answers exactly like a missing one (404, the same
sentence), so a non-admin can't probe which names exist.

Real gateway + entities routers behind the real security middleware; no embedder."""

from __future__ import annotations

import copy

import pytest

from email_fixtures import *  # noqa: F401,F403 - fixtures
from email_fixtures import ADMIN

pytestmark = pytest.mark.integration


def _spark(name: str) -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


@pytest.fixture
def world(gateway, monkeypatch):
    pytest.importorskip("abstractmemory")
    pytest.importorskip("yaml")
    from fastapi.testclient import TestClient

    from abstractgateway.entities import EntityRegistry
    from abstractgateway.routes import entities_router, entity_replay_router
    from abstractgateway.users import GatewayUserRegistry
    import abstractgateway.routes.entities as entities_routes
    import abstractgateway.routes.entity_replay as replay_routes

    app = gateway["app"]
    app.include_router(entities_router, prefix="/api")
    app.include_router(entity_replay_router, prefix="/api")
    registry = EntityRegistry(data_dir=gateway["data_dir"], embedder_factory=lambda: None)
    monkeypatch.setattr(entities_routes, "_registry", lambda: registry)
    monkeypatch.setattr(replay_routes, "_registry", lambda: registry)
    c = TestClient(app)
    _rec, admin_token = GatewayUserRegistry().create_user(user_id="admin", roles=["admin", "user"], runtime_id="default")
    admin = {"Authorization": f"Bearer {admin_token}"}

    # E0: legacy home, no creator recorded.
    registry.create(name="Olden", spark=_spark("Olden"))
    r = c.post("/api/gateway/entities", headers=gateway["alice"], json={"name": "Aster", "spark": _spark("Aster"), "skills": []})
    assert r.status_code == 201, r.text
    r = c.post("/api/gateway/entities", headers=gateway["bob"], json={"name": "Borea", "spark": _spark("Borea"), "skills": []})
    assert r.status_code == 201, r.text
    return {"c": c, "alice": gateway["alice"], "bob": gateway["bob"], "admin": admin, "registry": registry}


def _slugs(resp) -> set:
    assert resp.status_code == 200, resp.text
    return {e["slug"] for e in resp.json()["entities"]}


def test_create_records_the_creator_and_legacy_homes_have_none(world) -> None:
    reg = world["registry"]
    assert reg.manifest_for("aster").created_by == {"tenant_id": "default", "user_id": "alice"}
    assert reg.manifest_for("borea").created_by == {"tenant_id": "default", "user_id": "bob"}
    assert reg.manifest_for("olden").created_by is None
    # Additive: a legacy manifest file carries no created_by key at all (never rewritten).
    import json

    doc = json.loads((reg.entities_dir / "olden" / "manifest.json").read_text())
    assert "created_by" not in doc


def test_list_entities_is_scoped_by_creator(world) -> None:
    c = world["c"]
    assert _slugs(c.get("/api/gateway/entities", headers=world["alice"])) == {"aster"}
    assert _slugs(c.get("/api/gateway/entities", headers=world["bob"])) == {"borea"}
    assert _slugs(c.get("/api/gateway/entities", headers=world["admin"])) == {"aster", "borea", "olden"}
    assert _slugs(c.get("/api/gateway/entities", headers=ADMIN)) == {"aster", "borea", "olden"}


@pytest.mark.parametrize(
    "path",
    [
        "/api/gateway/entities/{n}",
        "/api/gateway/entities/{n}/card",
        "/api/gateway/entities/{n}/state",
        "/api/gateway/entities/{n}/verify",
        "/api/gateway/entities/{n}/communities",
        "/api/gateway/entities/{n}/tool-policy",
        "/api/gateway/entities/{n}/workspace",
        "/api/gateway/entities/{n}/visit",
        "/api/gateway/entities/{n}/replay",
    ],
)
def test_per_entity_routes_hide_other_and_legacy_entities_as_missing(world, path) -> None:
    c = world["c"]
    missing = c.get(path.format(n="nobody-here"), headers=world["alice"])
    for hidden in ("borea", "olden"):
        r = c.get(path.format(n=hidden), headers=world["alice"])
        assert r.status_code == 404, (path, hidden, r.status_code, r.text)
        if path.endswith("{n}"):
            # Hidden reads exactly like missing: same status, same sentence shape.
            assert r.json()["detail"] == missing.json()["detail"].replace("nobody-here", hidden)
    own = c.get(path.format(n="aster"), headers=world["alice"])
    assert own.status_code != 404, (path, own.text)
    for name in ("aster", "borea", "olden"):
        assert c.get(path.format(n=name), headers=world["admin"]).status_code != 404, (path, name)


def test_writes_to_another_users_entity_answer_404_not_403(world) -> None:
    c = world["c"]
    # A user-level write (workspace file) on bob's entity: hidden, not merely forbidden.
    r = c.post("/api/gateway/entities/borea/workspace/file", headers=world["alice"], json={"path": "x.txt", "content": "hi"})
    assert r.status_code == 404, r.text
    # An admin-gated write stays admin-gated for alice's OWN entity (403: visibility is not management).
    r = c.post("/api/gateway/entities/aster/state", headers=world["alice"], json={"state": "paused"})
    assert r.status_code == 403, r.text


def test_create_under_another_accounts_name_is_refused_not_adopted(world) -> None:
    c = world["c"]
    r = c.post("/api/gateway/entities", headers=world["alice"], json={"name": "Borea", "spark": _spark("Borea"), "skills": []})
    assert r.status_code == 409, r.text
    assert "taken" in r.json()["detail"]
    assert world["registry"].manifest_for("borea").created_by["user_id"] == "bob"


def test_meets_need_both_entities_visible(world) -> None:
    c = world["c"]
    r = c.post("/api/gateway/entities/meets/open", headers=world["alice"], json={"entity_a": "aster", "entity_b": "borea"})
    assert r.status_code == 404, r.text


def test_me_accounts_is_self_plus_own_entities(world) -> None:
    c = world["c"]
    rows = c.get("/api/gateway/me/accounts", headers=world["alice"]).json()
    assert rows["scope"] == "own"
    assert [(a["kind"], a["id"]) for a in rows["accounts"]] == [("user", "alice"), ("entity", "aster")]
    me, ent = rows["accounts"]
    assert me["own"] is True and me["actions"]["suspend"]["available"] is False
    assert me["actions"]["rotate"] == {"available": True, "reason": None}  # your own token: POST /me/token/rotate
    assert me["actions"]["workspace"]["available"] is True  # your own workspace policy
    assert ent["created_by"] == {"tenant_id": "default", "user_id": "alice"}
    assert ent["actions"]["suspend"] == {"available": False, "reason": "Only an admin can suspend an entity."}
    bob = c.get("/api/gateway/me/accounts", headers=world["bob"]).json()["accounts"]
    assert [a["id"] for a in bob] == ["bob", "borea"]


def test_admin_accounts_stays_admin_only_and_lists_everything_with_creators(world) -> None:
    c = world["c"]
    assert c.get("/api/gateway/admin/accounts", headers=world["alice"]).status_code == 403
    rows = {a["id"]: a for a in c.get("/api/gateway/admin/accounts", headers=world["admin"]).json()["accounts"]}
    assert {"admin", "alice", "bob", "aster", "borea", "olden"} <= set(rows)
    assert rows["aster"]["created_by"] == {"tenant_id": "default", "user_id": "alice"}
    assert rows["olden"]["created_by"] is None


def test_me_account_activity_only_for_self_and_own_entities(world) -> None:
    c = world["c"]
    assert c.get("/api/gateway/me/accounts/alice/activity", headers=world["alice"]).status_code == 200
    assert c.get("/api/gateway/me/accounts/aster/activity", headers=world["alice"]).status_code == 200
    for other in ("bob", "borea", "olden", "admin", "nobody"):
        r = c.get(f"/api/gateway/me/accounts/{other}/activity", headers=world["alice"])
        assert r.status_code == 404, (other, r.text)
    assert c.get("/api/gateway/admin/accounts/borea/activity", headers=world["alice"]).status_code == 403


def test_create_of_a_name_living_in_another_plane_is_refused_not_adopted(gateway, monkeypatch) -> None:
    """Entity names are door-global principals (one users file) while homes live per runtime
    plane. Alice creates "Cygnus" in HER plane; bob, whose runtime plane holds no such home,
    asks for the same name: 409, no home in his plane, no credential, alice's entity unchanged.
    A human account's name is refused the same way (create would adopt that user record)."""
    pytest.importorskip("abstractmemory")
    pytest.importorskip("yaml")
    from fastapi.testclient import TestClient

    from abstractgateway.entities import EntityRegistry
    from abstractgateway.routes import entities_router
    from abstractgateway.security.principal import current_gateway_principal
    from abstractgateway.users import GatewayUserRegistry, gateway_user_registry_path_from_env
    import abstractgateway.routes.entities as entities_routes

    app = gateway["app"]
    app.include_router(entities_router, prefix="/api")
    users_file = gateway_user_registry_path_from_env()
    planes = {
        who: EntityRegistry(data_dir=gateway["data_dir"] / "planes" / who, embedder_factory=lambda: None, users_registry_path=users_file)
        for who in ("alice", "bob")
    }
    monkeypatch.setattr(entities_routes, "_registry", lambda: planes[current_gateway_principal().user_id])
    c = TestClient(app)

    r = c.post("/api/gateway/entities", headers=gateway["alice"], json={"name": "Cygnus", "spark": _spark("Cygnus"), "skills": []})
    assert r.status_code == 201, r.text
    assert r.json()["principal"]["minted"] is True
    before = GatewayUserRegistry(path=users_file).get_user("cygnus").to_storage_dict()
    alice_manifest = (planes["alice"].entities_dir / "cygnus" / "manifest.json").read_text()

    # The dry run says the same thing the create will.
    r = c.post("/api/gateway/entities/Cygnus/validate", headers=gateway["bob"], json={"name": "Cygnus", "spark": _spark("Cygnus")})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False and any("taken" in e for e in r.json()["errors"]), r.json()

    r = c.post("/api/gateway/entities", headers=gateway["bob"], json={"name": "Cygnus", "spark": _spark("Cygnus"), "skills": []})
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "That name is taken: another account already has an entity called 'cygnus'. Pick another name."
    assert "principal" not in r.json()
    assert not (planes["bob"].entities_dir / "cygnus").exists()
    assert GatewayUserRegistry(path=users_file).get_user("cygnus").to_storage_dict() == before
    assert (planes["alice"].entities_dir / "cygnus" / "manifest.json").read_text() == alice_manifest

    # Alice re-summoning her own entity stays the idempotent re-create.
    r = c.post("/api/gateway/entities", headers=gateway["alice"], json={"name": "Cygnus", "spark": _spark("Cygnus"), "skills": []})
    assert r.status_code == 201, r.text
    assert r.json()["created"] is False and r.json()["principal"]["minted"] is False

    # A human account's name: refused, never adopted as an entity principal.
    r = c.post("/api/gateway/entities", headers=gateway["bob"], json={"name": "Alice", "spark": _spark("Alice"), "skills": []})
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "That name is taken: an account is already called 'alice'. Pick another name."
    assert not (planes["bob"].entities_dir / "alice").exists()
    assert GatewayUserRegistry(path=users_file).get_user("alice").principal_kind == "human"
