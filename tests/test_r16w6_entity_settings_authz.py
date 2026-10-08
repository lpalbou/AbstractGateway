"""R16.5 (operator ruling 2026-10-08): "the creator configures their entity".

Roles are exactly two, admin and member (a human or an entity account). An entity's SETTINGS —
mind (substrate), voice, tools per phase, instructions (prompt), skills, archive / unarchive and
its Active switch (plus workspaces and preferences since rounds 11 and 14) — are changed by an
admin or the entity's CREATOR, within what the admin authorised; never by another member, never
by the entity itself.

Setup: alice (member) creates Aster; bob (member) creates nothing of alice's; an admin account;
the entity principal `aster` itself. Real gateway + entities routers behind the real security
middleware, ONE entity registry (the worst case: no per-runtime separation helps), no embedder.
The offered model / voice lists are stubbed (no provider is reachable here); one test runs the
real discovery path with every provider unreachable.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from email_fixtures import *  # noqa: F401,F403 - fixtures

pytestmark = pytest.mark.integration


def _spark(name: str) -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


OFFERED_MODELS = {"lmstudio": ["qwen3-4b", "qwen3-30b"], "openai": ["gpt-5-mini"]}
OFFERED_VOICES = {"kokoro": (["af_heart", "am_adam"], ["kokoro-82m"])}


def _knock_as_entity(users, slug: str) -> str:
    """A bearer for the entity's OWN principal. Production discards it at creation (an entity
    bearer must not exist); the test writes one into the registry file so it can knock as the
    entity itself and prove it never configures itself."""
    from abstractgateway.users import hash_gateway_token, token_fingerprint

    rec = users.get_user(slug)
    assert rec is not None and rec.principal_kind == "entity", "POST /entities mints the entity's account"
    token = "entity-knock-" + slug
    path = Path(users.path)
    doc = json.loads(path.read_text())

    def patch(node):
        if isinstance(node, dict):
            if node.get("user_id") == slug and "token_hash" in node:
                node["token_hash"] = hash_gateway_token(token)
                node["token_fingerprint"] = token_fingerprint(token)
                return True
            return any(patch(v) for v in node.values())
        if isinstance(node, list):
            return any(patch(v) for v in node)
        return False

    assert patch(doc), "the entity record was not found in the registry file"
    path.write_text(json.dumps(doc))
    return token


@pytest.fixture
def world(gateway, monkeypatch):
    pytest.importorskip("abstractmemory")
    pytest.importorskip("yaml")
    from fastapi.testclient import TestClient

    import abstractgateway.entity_settings_access as esa
    import abstractgateway.routes.entities as entities_routes
    import abstractgateway.routes.entity_replay as replay_routes
    from abstractgateway.entities import EntityRegistry
    from abstractgateway.routes import entities_router, entity_replay_router
    from abstractgateway.users import GatewayUserRegistry

    app = gateway["app"]
    app.include_router(entities_router, prefix="/api")
    app.include_router(entity_replay_router, prefix="/api")
    registry = EntityRegistry(data_dir=gateway["data_dir"], embedder_factory=lambda: None)
    monkeypatch.setattr(entities_routes, "_registry", lambda: registry)
    monkeypatch.setattr(replay_routes, "_registry", lambda: registry)
    real_offered_text_models = esa.offered_text_models
    # The lists the pickers show a member (GET /discovery/providers/{p}/models, GET /voice/voices).
    monkeypatch.setattr(esa, "offered_text_models", lambda p: (list(OFFERED_MODELS.get(p, [])), None if p in OFFERED_MODELS else "unknown provider"))
    monkeypatch.setattr(esa, "offered_voices", lambda p, m: (*OFFERED_VOICES.get(p, ([], [])), None))
    c = TestClient(app)
    users = GatewayUserRegistry()
    _rec, admin_token = users.create_user(user_id="root", roles=["admin", "user"], runtime_id="default")
    r = c.post("/api/gateway/entities", headers=gateway["alice"], json={"name": "Aster", "spark": _spark("Aster"), "skills": []})
    assert r.status_code == 201, r.text
    # The entity's own principal (its door credential is discarded in production; minted here so
    # the test can knock as the entity itself).
    entity_token = _knock_as_entity(users, "aster")
    return {
        "c": c,
        "registry": registry,
        "admin": {"Authorization": f"Bearer {admin_token}"},
        "alice": gateway["alice"],
        "bob": gateway["bob"],
        "entity": {"Authorization": f"Bearer {entity_token}"},
        "data_dir": Path(gateway["data_dir"]),
        "real_offered_text_models": real_offered_text_models,
    }


# One VALID body per settings route (within the offered lists), so a refusal can only be authz.
SETTINGS_ROUTES = [
    ("substrate", {"provider": "lmstudio", "model": "qwen3-4b"}),
    ("voice", {"provider": "kokoro", "model": "kokoro-82m", "voice": "af_heart"}),
    ("tool-policy", {"policy": {"visit": ["read_memory", "search_memory"]}}),
    ("prompt", {"overlay": {"operator": "Be brief."}}),
    ("skills", {"skills": []}),
]


def _audit_lines(data_dir: Path) -> list[dict]:
    path = data_dir / "audit_log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.mark.parametrize("setting,body", SETTINGS_ROUTES, ids=[s for s, _ in SETTINGS_ROUTES])
def test_the_creator_and_an_admin_change_a_setting_others_never(world, setting, body) -> None:
    c = world["c"]
    url = f"/api/gateway/entities/aster/{setting}"
    # The entity itself: 403 with the sentence (it may see itself, never configure itself).
    r = c.put(url, headers=world["entity"], json=body)
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert detail["reason_code"] == "admin_or_creator_required"
    assert detail["message"].startswith("Only an admin or aster's creator can ")
    # Another member: the entity is hidden from bob, so it answers exactly like a missing one.
    r = c.put(url, headers=world["bob"], json=body)
    assert r.status_code == 404, r.text
    # Its creator and an admin: the write lands.
    r = c.put(url, headers=world["alice"], json=body)
    assert r.status_code == 200, r.text
    r = c.put(url, headers=world["admin"], json=body)
    assert r.status_code == 200, r.text


def test_a_member_who_can_see_but_did_not_create_gets_the_sentence(world, monkeypatch) -> None:
    """Visibility and configuration are separate rules: should a member see an entity they did not
    create (here: a gateway where every caller sees every entity), the settings guard still says no."""
    import abstractgateway.entity_access as entity_access

    monkeypatch.setattr(entity_access, "sees_every_entity", lambda principal: True)
    r = world["c"].put("/api/gateway/entities/aster/substrate", headers=world["bob"], json={"provider": "lmstudio", "model": "qwen3-4b"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["message"] == "Only an admin or aster's creator can change its mind."


def test_lifecycle_acts_stay_admin_only_for_the_creator(world) -> None:
    c = world["c"]
    for method, path, body in (
        ("POST", "state", {"state": "asleep"}),
        ("POST", "loop/start", {}),
        ("PUT", "work-order", {"order": "x"}),
        ("POST", "reembed", {"model": "x", "confirm_token": "y"}),
        ("PUT", "workspace/mounts", {"mounts": []}),
    ):
        r = c.request(method, f"/api/gateway/entities/aster/{path}", headers=world["alice"], json=body)
        assert r.status_code == 403, (path, r.text)
        assert r.json().get("required_role") == "admin", (path, r.text)


def test_access_route_says_who_may_configure(world) -> None:
    c = world["c"]
    a = c.get("/api/gateway/entities/aster/access", headers=world["alice"]).json()
    assert a["can_configure"] is True and a["as"] == "creator" and a["reason"] is None
    assert a["admin_only"]["available"] is False and a["admin_only"]["reason"].startswith("Only an admin can ")
    root = c.get("/api/gateway/entities/aster/access", headers=world["admin"]).json()
    assert root["can_configure"] is True and root["as"] == "admin" and root["admin_only"]["available"] is True
    it = c.get("/api/gateway/entities/aster/access", headers=world["entity"]).json()
    assert it["can_configure"] is False and it["as"] is None
    assert it["reason"] == "Only an admin or aster's creator can change its settings."
    assert c.get("/api/gateway/entities/aster/access", headers=world["bob"]).status_code == 404


def test_creator_mind_is_bounded_by_the_offered_models_admin_is_not(world) -> None:
    c = world["c"]
    r = c.put("/api/gateway/entities/aster/substrate", headers=world["alice"], json={"provider": "lmstudio", "model": "llama-unlisted"})
    assert r.status_code == 403, r.text
    d = r.json()["detail"]
    assert d["reason_code"] == "not_offered"
    assert d["message"] == "llama-unlisted isn't a lmstudio model this gateway offers. Offered: qwen3-4b, qwen3-30b."
    r = c.put("/api/gateway/entities/aster/substrate", headers=world["alice"], json={"provider": "ghost", "model": "m"})
    assert r.status_code == 403 and "offers no ghost model right now" in r.json()["detail"]["message"], r.text
    # Nothing was written by the refusals.
    assert c.get("/api/gateway/entities/aster/substrate", headers=world["alice"]).json()["source"] != "entity"
    # Back to the Gateway default is always allowed; an admin may name any model.
    assert c.put("/api/gateway/entities/aster/substrate", headers=world["alice"], json={"clear": True}).status_code == 200
    r = c.put("/api/gateway/entities/aster/substrate", headers=world["admin"], json={"provider": "lmstudio", "model": "llama-unlisted"})
    assert r.status_code == 200, r.text
    assert r.json()["model"] == "llama-unlisted"


def test_creator_voice_is_bounded_by_the_offered_voices(world) -> None:
    c = world["c"]
    r = c.put("/api/gateway/entities/aster/voice", headers=world["alice"], json={"provider": "kokoro", "model": "kokoro-82m", "voice": "zz_nobody"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["message"] == "zz_nobody isn't a kokoro voice this gateway offers. Offered: af_heart, am_adam."
    r = c.put("/api/gateway/entities/aster/voice", headers=world["alice"], json={"provider": "kokoro", "model": "other", "voice": "af_heart"})
    assert r.status_code == 403 and r.json()["detail"]["message"].startswith("other isn't a kokoro speech model"), r.text
    assert c.put("/api/gateway/entities/aster/voice", headers=world["alice"], json={"clear": True}).status_code == 200
    r = c.put("/api/gateway/entities/aster/voice", headers=world["admin"], json={"provider": "kokoro", "model": "kokoro-82m", "voice": "zz_nobody"})
    assert r.status_code == 200, r.text


def test_creator_tools_stay_within_the_tiers_an_admin_offers(world) -> None:
    c = world["c"]
    r = c.put("/api/gateway/entities/aster/tool-policy", headers=world["alice"], json={"policy": {"visit": ["read_memory", "execute_command"]}})
    assert r.status_code == 403, r.text
    d = r.json()["detail"]
    assert d["reason_code"] == "admin_only_tool"
    assert d["message"].startswith("Only an admin can give aster execute_command: a tier-2 tool")
    assert "Offered to you: " in d["message"]
    # An admin grants it; the creator may then keep it (re-save) or remove it.
    assert c.put("/api/gateway/entities/aster/tool-policy", headers=world["admin"], json={"policy": {"visit": ["read_memory", "execute_command"]}}).status_code == 200
    assert c.put("/api/gateway/entities/aster/tool-policy", headers=world["alice"], json={"policy": {"visit": ["read_memory", "execute_command"]}}).status_code == 200
    r = c.put("/api/gateway/entities/aster/tool-policy", headers=world["alice"], json={"policy": {"visit": ["read_memory"]}})
    assert r.status_code == 200, r.text
    assert r.json()["phases"]["visit"]["tools"] == ["read_memory"]


def test_markers_and_audit_lines_name_the_actor_and_the_role(world) -> None:
    c = world["c"]
    assert c.put("/api/gateway/entities/aster/substrate", headers=world["alice"], json={"provider": "lmstudio", "model": "qwen3-4b"}).status_code == 200
    assert c.put("/api/gateway/entities/aster/prompt", headers=world["entity"], json={"overlay": {"operator": "x"}}).status_code == 403
    lines = [ln for ln in _audit_lines(world["data_dir"]) if isinstance(ln.get("entity_settings"), dict)]
    allowed = [ln["entity_settings"] for ln in lines if ln["entity_settings"]["outcome"] == "allowed"]
    refused = [ln["entity_settings"] for ln in lines if ln["entity_settings"]["outcome"] == "refused"]
    assert {"entity": "aster", "setting": "substrate", "actor": "person:alice", "as": "creator", "outcome": "allowed"} in allowed
    assert {"entity": "aster", "setting": "prompt", "actor": "entity:aster", "as": None, "outcome": "refused"} in refused
    # The durable marker in the entity's own history says the same.
    from abstractgateway.entity_replay import read_host_markers

    markers = [m["payload"] for m in read_host_markers(world["registry"].entities_dir, "aster", since_seq=-1.0) if (m.get("payload") or {}).get("kind") == "substrate_changed"]
    assert markers, "the mind change must be recorded in its history"
    assert markers[-1]["by"] == "person:alice" and markers[-1]["as"] == "creator", markers[-1]


def test_creator_archives_unarchives_and_turns_it_on_and_off(world) -> None:
    c = world["c"]
    # The creator's row: configure / archive / suspend available.
    rows = {a["id"]: a for a in c.get("/api/gateway/me/accounts", headers=world["alice"]).json()["accounts"]}
    acts = rows["aster"]["actions"]
    assert acts["configure"] == {"available": True, "reason": None}
    assert acts["suspend"] == {"available": True, "reason": None}
    assert acts["archive"] == {"available": True, "reason": None}
    # Active off/on (suspend / resume) by the creator; not by bob, not by the entity.
    assert c.put("/api/gateway/me/accounts/aster/active", headers=world["bob"], json={"active": False}).status_code == 404
    assert c.put("/api/gateway/me/accounts/aster/active", headers=world["entity"], json={"active": False}).status_code == 404
    r = c.put("/api/gateway/me/accounts/aster/active", headers=world["alice"], json={"active": False})
    assert r.status_code == 200 and r.json()["active"] is False, r.text
    r = c.put("/api/gateway/me/accounts/aster/active", headers=world["alice"], json={"active": True})
    assert r.status_code == 200 and r.json()["active"] is True, r.text
    # A user account's Active switch stays an admin's.
    assert c.put("/api/gateway/me/accounts/bob/active", headers=world["alice"], json={"active": False}).status_code == 404
    # Archive, then it is listed only with include_archived; unarchive comes back inactive.
    assert c.post("/api/gateway/me/accounts/aster/archive", headers=world["alice"]).status_code == 200
    assert "aster" not in {a["id"] for a in c.get("/api/gateway/me/accounts", headers=world["alice"]).json()["accounts"]}
    archived = {a["id"]: a for a in c.get("/api/gateway/me/accounts?include_archived=true", headers=world["alice"]).json()["accounts"]}
    assert archived["aster"]["archived"] is True
    assert archived["aster"]["actions"]["unarchive"] == {"available": True, "reason": None}
    assert archived["aster"]["actions"]["configure"]["available"] is False
    assert c.post("/api/gateway/me/accounts/aster/unarchive", headers=world["bob"]).status_code == 404
    # The archived entity's own credential no longer signs in at all.
    assert c.post("/api/gateway/me/accounts/aster/unarchive", headers=world["entity"]).status_code == 401
    r = c.post("/api/gateway/me/accounts/aster/unarchive", headers=world["alice"])
    assert r.status_code == 200, r.text
    assert r.json()["archived"] is False and r.json()["active"] is False
    # A user account is never unarchived through /me.
    assert c.post("/api/gateway/me/accounts/bob/unarchive", headers=world["alice"]).status_code == 404
    lines = [ln["entity_settings"] for ln in _audit_lines(world["data_dir"]) if isinstance(ln.get("entity_settings"), dict)]
    assert {"entity": "aster", "setting": "unarchive", "actor": "person:alice", "as": "creator", "outcome": "allowed"} in lines
    assert {"entity": "aster", "setting": "active", "actor": "person:bob", "as": None, "outcome": "refused"} in lines


def test_real_discovery_path_refuses_a_creator_when_no_provider_answers(world, monkeypatch) -> None:
    """No stub: the bound asks the SAME discovery route the picker uses; with every provider
    unreachable (hermetic env) nothing is offered, so the creator is refused with the sentence."""
    import abstractgateway.entity_settings_access as esa

    monkeypatch.setattr(esa, "offered_text_models", world["real_offered_text_models"])
    r = world["c"].put("/api/gateway/entities/aster/substrate", headers=world["alice"], json={"provider": "lmstudio", "model": "anything"})
    assert r.status_code == 403, r.text
    assert "offers no lmstudio model right now" in r.json()["detail"]["message"]


def test_the_entity_itself_never_configures_itself_even_if_named_its_own_creator() -> None:
    """A manifest naming the entity as its own creator (hand-edited, or an entity that created
    an entity of the same name) never lets the entity principal configure itself."""
    from abstractgateway.entity_settings_access import configure_role
    from abstractgateway.security.principal import GatewayPrincipal

    me = GatewayPrincipal(user_id="aster", tenant_id="default", roles=("entity",), scopes=(), runtime_id="aster", source="test")
    assert configure_role(me, {"tenant_id": "default", "user_id": "aster"}) is None
    alice = GatewayPrincipal(user_id="alice", tenant_id="default", roles=("user",), scopes=(), runtime_id="alice", source="test")
    assert configure_role(alice, {"tenant_id": "default", "user_id": "alice"}) == "creator"
    assert configure_role(alice, None) is None  # a legacy home without a creator: admins only
