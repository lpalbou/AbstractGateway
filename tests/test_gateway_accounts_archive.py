"""Round 3 §2: accounts are ARCHIVED, never deleted (operator decision).

Users: archive switches the account off and `authenticate()` / the session check refuse the
archived record (401); unarchive brings it back INACTIVE. Entities: archive suspends (paused,
door credential off) and every wake entry point refuses through ONE predicate
(`entity_access.entity_archived`). Archived rows are hidden unless `include_archived=true`
(admin) and never listed on `/me/accounts`. Real gateway + entity routers behind the real
security middleware, entities from a real EntityRegistry (no embedder, no model)."""

from __future__ import annotations

import copy
import json

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
    from abstractgateway.routes import entities_router
    from abstractgateway.users import GatewayUserRegistry
    import abstractgateway.routes.entities as entities_routes

    app = gateway["app"]
    app.include_router(entities_router, prefix="/api")
    registry = EntityRegistry(data_dir=gateway["data_dir"], embedder_factory=lambda: None)
    monkeypatch.setattr(entities_routes, "_registry", lambda: registry)
    c = TestClient(app)
    _rec, admin_token = GatewayUserRegistry().create_user(user_id="admin", roles=["admin", "user"], runtime_id="default")
    admin = {"Authorization": f"Bearer {admin_token}"}
    for who, name in (("alice", "Aster"), ("bob", "Borea")):
        r = c.post("/api/gateway/entities", headers=gateway[who], json={"name": name, "spark": _spark(name), "skills": []})
        assert r.status_code == 201, r.text
    return {"c": c, "admin": admin, "registry": registry, **gateway}


def _ids(resp) -> set:
    assert resp.status_code == 200, resp.text
    return {row["id"] for row in resp.json()["accounts"]}


# ---------------------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------------------


def test_archived_user_gets_401_and_comes_back_inactive(gateway) -> None:
    c = gateway["client"]
    assert c.get("/api/gateway/me", headers=gateway["alice"]).status_code == 200

    r = c.post("/api/gateway/admin/accounts/alice/archive", headers=ADMIN)
    assert r.status_code == 200, r.text
    row = r.json()
    assert row["archived"] is True and row["active"] is False and row["archived_at"]
    # Archived rows: only Logs, and Unarchive for an admin.
    assert {k for k, v in row["actions"].items() if v["available"]} == {"logs", "unarchive"}
    assert "delete" not in row["actions"]

    # The token answers 401, a new sign-in too.
    assert c.get("/api/gateway/me", headers=gateway["alice"]).status_code == 401
    r = c.post("/api/gateway/session/login", json={"user_id": "alice", "token": gateway["alice_token"]})
    assert r.status_code == 401
    # Active can't be switched on while archived (unarchive first).
    r = c.put("/api/gateway/admin/accounts/alice/active", headers=ADMIN, json={"active": True})
    assert r.status_code == 409 and "unarchive it first" in r.json()["detail"]["message"]
    r = c.patch("/api/gateway/admin/users/alice", headers=ADMIN, json={"enabled": True})
    assert r.status_code == 409 and "unarchive it first" in r.json()["detail"]["message"]
    # Records kept: users.json still holds alice, archived.
    rec = json.loads((gateway["data_dir"] / "auth" / "users.json").read_text())["users"]
    alice = next(u for u in rec if u["user_id"] == "alice")
    assert alice["archived"] is True and alice["enabled"] is False and alice["token_hash"]

    r = c.post("/api/gateway/admin/accounts/alice/unarchive", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["archived"] is False and r.json()["active"] is False  # back, inactive
    assert c.get("/api/gateway/me", headers=gateway["alice"]).status_code == 401
    r = c.put("/api/gateway/admin/accounts/alice/active", headers=ADMIN, json={"active": True})
    assert r.status_code == 200 and r.json()["active"] is True
    assert c.get("/api/gateway/me", headers=gateway["alice"]).status_code == 200


def test_archived_user_session_ends(gateway) -> None:
    c = gateway["client"]
    r = c.post("/api/gateway/session/login", json={"user_id": "alice", "token": gateway["alice_token"]})
    assert r.status_code == 200, r.text
    assert c.get("/api/gateway/me").status_code == 200  # the session cookie
    from abstractgateway.users import GatewayUserRegistry

    GatewayUserRegistry().set_archived(user_id="alice", archived=True, actor="test")
    assert c.get("/api/gateway/me").status_code == 401


def test_authenticate_refuses_an_archived_record_even_if_enabled(tmp_path) -> None:
    """The predicate itself: an archived record never authenticates (a hand-edited file that
    left `enabled: true` included)."""
    from abstractgateway.users import GatewayUserRegistry

    reg = GatewayUserRegistry(path=tmp_path / "users.json")
    _rec, token = reg.create_user(user_id="carol", roles=["user"])
    assert reg.authenticate(token) is not None
    doc = json.loads((tmp_path / "users.json").read_text())
    doc["users"][0]["archived"] = True  # enabled stays true
    (tmp_path / "users.json").write_text(json.dumps(doc))
    assert reg.authenticate(token) is None


def test_admin_list_hides_archived_unless_asked_and_guards_hold(gateway) -> None:
    c = gateway["client"]
    assert c.post("/api/gateway/admin/accounts/bob/archive", headers=ADMIN).status_code == 200
    assert "bob" not in _ids(c.get("/api/gateway/admin/accounts", headers=ADMIN))
    assert "bob" in _ids(c.get("/api/gateway/admin/accounts?include_archived=true", headers=ADMIN))
    # Already archived / not archived / unknown.
    assert c.post("/api/gateway/admin/accounts/bob/archive", headers=ADMIN).status_code == 409
    assert c.post("/api/gateway/admin/accounts/alice/unarchive", headers=ADMIN).status_code == 409
    assert c.post("/api/gateway/admin/accounts/nobody/archive", headers=ADMIN).status_code == 404
    # Your own account: refused with the sentence.
    from abstractgateway.users import GatewayUserRegistry

    _rec, tok = GatewayUserRegistry().create_user(user_id="root", roles=["admin", "user"])
    r = c.post("/api/gateway/admin/accounts/root/archive", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 409 and r.json()["detail"]["message"] == "You can't archive your own account."
    # Non-admins: no admin archive route, no user archive through /me.
    assert c.post("/api/gateway/admin/accounts/alice/archive", headers=gateway["bob"]).status_code in (401, 403)
    assert c.post("/api/gateway/me/accounts/alice/archive", headers=gateway["alice"]).status_code == 404


def test_logs_show_archived_and_unarchived(gateway) -> None:
    c = gateway["client"]
    assert c.post("/api/gateway/admin/accounts/alice/archive", headers=ADMIN).status_code == 200
    assert c.post("/api/gateway/admin/accounts/alice/unarchive", headers=ADMIN).status_code == 200
    events = c.get("/api/gateway/admin/accounts/alice/activity?kind=account", headers=ADMIN).json()["events"]
    assert [e["title"] for e in events][:2] == ["Unarchived", "Archived"]


def test_delete_and_purge_answer_410_with_the_sentence(gateway) -> None:
    c = gateway["client"]
    sentence = (
        "Accounts are archived, never deleted: use Archive (POST /api/gateway/admin/accounts/{id}/archive). "
        "Runs and history are kept."
    )
    r = c.delete("/api/gateway/admin/users/alice", headers=ADMIN)
    assert r.status_code == 410 and r.json()["detail"]["message"] == sentence
    r = c.post("/api/gateway/admin/runtime-reservations/alice/purge", headers=ADMIN, json={})
    assert r.status_code == 410 and r.json()["detail"]["message"] == sentence
    assert c.get("/api/gateway/me", headers=gateway["alice"]).status_code == 200


# ---------------------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------------------


def test_creator_archives_own_entity_but_never_unarchives_or_touches_others(world) -> None:
    c = world["c"]
    assert "aster" in _ids(c.get("/api/gateway/me/accounts", headers=world["alice"]))

    # Not hers: bob's entity and bob himself answer 404 like a missing account.
    assert c.post("/api/gateway/me/accounts/borea/archive", headers=world["alice"]).status_code == 404
    assert c.post("/api/gateway/me/accounts/bob/archive", headers=world["alice"]).status_code == 404

    r = c.post("/api/gateway/me/accounts/aster/archive", headers=world["alice"])
    assert r.status_code == 200, r.text
    assert r.json()["archived"] is True and r.json()["actions"]["unarchive"]["available"] is False
    # A16: archived rows never appear on /me/accounts.
    assert "aster" not in _ids(c.get("/api/gateway/me/accounts", headers=world["alice"]))
    # Only an admin unarchives.
    assert c.post("/api/gateway/admin/accounts/aster/unarchive", headers=world["alice"]).status_code == 403
    assert "aster" in _ids(c.get("/api/gateway/admin/accounts?include_archived=true", headers=world["admin"]))
    r = c.post("/api/gateway/admin/accounts/aster/unarchive", headers=world["admin"])
    assert r.status_code == 200 and r.json()["archived"] is False and r.json()["active"] is False


def test_archived_entity_never_wakes(world, monkeypatch) -> None:
    from abstractgateway.entity_access import EntityArchivedError, entity_archived
    from abstractgateway.entity_loop import start_loop
    from abstractgateway.users import GatewayUserRegistry

    c, registry = world["c"], world["registry"]
    home = registry.entities_dir / "aster"
    files_before = sorted(p.relative_to(home) for p in home.rglob("*") if p.is_file())

    r = c.post("/api/gateway/admin/accounts/aster/archive", headers=world["admin"])
    assert r.status_code == 200, r.text
    assert r.json()["entity_state"] == "paused" and r.json()["active"] is False
    assert entity_archived("aster") is True
    assert GatewayUserRegistry().get_user("aster").enabled is False
    # Records kept: the home's files are all still there.
    files_after = {p.relative_to(home) for p in home.rglob("*") if p.is_file()}
    assert set(files_before) <= files_after

    # The wake door (operator state verb) refuses with the sentence.
    for state in ("awake", "restore", "asleep"):
        r = c.post("/api/gateway/entities/aster/state", headers=world["admin"], json={"state": state})
        assert r.status_code == 409, (state, r.text)
        assert r.json()["detail"]["reason_code"] == "entity_archived"
    assert registry.state_of("aster")["state"] == "paused"
    # Summon / loop start refuse (admin-gated routes, admin caller).
    r = c.post("/api/gateway/entities/aster/summon", headers=world["admin"], json={"prompt": "hello"})
    assert r.status_code == 409 and r.json()["detail"]["reason_code"] == "entity_archived", r.text
    r = c.post("/api/gateway/entities/aster/loop/start", headers=world["admin"], json={})
    assert r.status_code == 409 and r.json()["detail"]["reason_code"] == "entity_archived", r.text
    # The loop spawner every caller shares (route, crash repair, need-check).
    with pytest.raises(EntityArchivedError):
        start_loop(home, provider="lmstudio", model="none")
    # Talk and visits.
    from abstractgateway.entity_chat import ChatOpenRefused, EntityChatHost
    from abstractgateway.entity_visits import EntityVisitHost, VisitRefused

    with pytest.raises(ChatOpenRefused) as chat:
        EntityChatHost(registry).open("aster")
    assert "archived" in chat.value.detail
    with pytest.raises(VisitRefused) as visit:
        EntityVisitHost(registry).open("aster", participants=["person:admin"])
    assert visit.value.code == "entity_archived"
    # The repair sweeper skips it.
    from abstractgateway.entity_repair import _need_check_one

    assert _need_check_one(registry, "aster", "aster", home, cadence_h=0.0) is None
    # Active can't resume it while archived.
    r = c.put("/api/gateway/admin/accounts/aster/active", headers=world["admin"], json={"active": True})
    assert r.status_code == 409

    # Unarchive: still paused + disabled until an admin turns Active on.
    assert c.post("/api/gateway/admin/accounts/aster/unarchive", headers=world["admin"]).status_code == 200
    assert registry.state_of("aster")["state"] == "paused"
    r = c.put("/api/gateway/admin/accounts/aster/active", headers=world["admin"], json={"active": True})
    assert r.status_code == 200 and r.json()["active"] is True
    assert registry.state_of("aster")["state"] != "paused"
