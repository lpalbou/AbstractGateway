"""Strict memory ownership (operator decision 5): the plane is NOT a sharing scope.

carol and dave are two accounts (user accounts on). The gateway gives every account its own runtime
plane and refuses a second account on a runtime id, which is what makes a plane's global owner the
account's own (pinned below). carol starts a run, writes memories under her session owner and creates
an entity; dave reads none of it through any route (`/memory/*`, `/kg/query`), and the ownership rule
itself refuses him even when it is evaluated against carol's plane and stores (a principal on a plane
that is not its own); carol reads her own memories and her entity's graph. Real app, real auth, real
stores (the `sqlite` KG backend so `/kg/query` and `/memory/*` read the same home)."""

from __future__ import annotations

import copy
import urllib.parse
from pathlib import Path

import pytest

from test_gateway_principal_isolation_matrix import _client, _write_min_bundle
from test_gateway_memory_routes import _claim, _close, _plane_facade, _revision

pytestmark = pytest.mark.basic

ADMIN = {"Authorization": "Bearer admin-token"}
SESSION = "carol-session"


def _user(client, user_id: str, runtime_id: str) -> dict:
    r = client.post(
        "/api/gateway/admin/users", headers=ADMIN,
        json={"user_id": user_id, "tenant_id": "default", "roles": ["user"], "runtime_id": runtime_id},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _principal(user_id: str, runtime_id: str):
    from abstractgateway.security.principal import GatewayPrincipal

    return GatewayPrincipal(user_id=user_id, tenant_id="default", roles=("user",), runtime_id=runtime_id)


@pytest.fixture()
def team(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from abstractruntime.integrations.abstractmemory.memory_facade import DEFAULT_SPARK_TEMPLATE

    from abstractgateway.entities import EntityRegistry
    from abstractgateway.memory_store import session_memory_owner_id
    from abstractgateway.service import get_gateway_service_for_principal

    monkeypatch.setenv("ABSTRACTGATEWAY_MEMORY_STORE_BACKEND", "sqlite")
    monkeypatch.delenv("ABSTRACTGATEWAY_MEMORY_STORE_PATH", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_MEMORY_PATH", raising=False)
    client = _client(tmp_path, monkeypatch)
    with client:
        carol = _user(client, "carol", "carol")
        dave = _user(client, "dave", "dave")
        # The guarantee the global-owner rule rests on: no second account on a runtime.
        r = client.post("/api/gateway/admin/users", headers=ADMIN,
                        json={"user_id": "eve", "tenant_id": "default", "roles": ["user"], "runtime_id": "carol"})
        assert r.status_code == 400 and "already assigned" in r.text, r.text
        _write_min_bundle(bundles_dir=tmp_path / "runtime" / "users" / "default" / "carol" / "flows")
        r = client.post("/api/gateway/runs/start", headers=carol,
                        json={"bundle_id": "iso-bundle", "flow_id": "root", "session_id": SESSION, "input_data": {"prompt": "hi"}})
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]

        svc = get_gateway_service_for_principal(_principal("carol", "carol"))
        artifact_id = svc.stores.artifact_store.store(b"carol's verbatim", content_type="text/plain", run_id=run_id).artifact_id

        spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
        spark["name"], spark["spark"] = "Aster", 1
        registry = EntityRegistry(data_dir=Path(svc.config.data_dir), embedder_factory=lambda: None)
        registry.create(name="Aster", spark=spark, created_by={"tenant_id": "default", "user_id": "carol"})
        entity_owner = registry.manifest_for("aster").entity_id
        assert entity_owner

        owner = session_memory_owner_id(SESSION)
        art_ref = {"kind": "artifact", "artifact_id": artifact_id, "run_id": run_id}
        facade = _plane_facade(svc)
        try:
            facade.apply_revision(_revision("rev-c", [_claim("mem-c", owner)], owner=owner,
                                            provenance={"sources": [art_ref], "actor": "runtime"}))
            facade.apply_revision(_revision("rev-e", [_claim("mem-e", entity_owner, scope="global", s="ex:aster")],
                                            owner=entity_owner, scope="global",
                                            provenance={"sources": [art_ref], "actor": "runtime"}))
            facade.apply_revision(_revision("rev-g", [_claim("mem-g", "global_memory", scope="global", s="ex:plane")],
                                            owner="global_memory", scope="global",
                                            provenance={"sources": [art_ref], "actor": "runtime"}))
        finally:
            _close(facade)
        yield {"c": client, "carol": carol, "dave": dave, "run_id": run_id, "owner": owner,
               "entity_owner": entity_owner, "art_ref": art_ref, "svc": svc}


def _ids(r) -> set:
    assert r.status_code == 200, r.text
    return {i["assertion_id"] for i in r.json()["items"]}


def test_the_owner_reads_her_memories_through_every_route(team) -> None:
    c, carol = team["c"], team["carol"]
    assert _ids(c.post("/api/gateway/memory/query", headers=carol, json={"scope": "session", "session_id": SESSION})) == {"mem-c"}
    assert _ids(c.post("/api/gateway/memory/query", headers=carol, json={"scope": "session", "run_id": team["run_id"]})) == {"mem-c"}
    assert c.get("/api/gateway/memory/mem-c/history", headers=carol, params={"session_id": SESSION}).status_code == 200
    assert c.get("/api/gateway/memory/mem-c/sources", headers=carol, params={"session_id": SESSION}).status_code == 200
    r = c.post("/api/gateway/memory/sources/reconstruct", headers=carol, json={"ref": team["art_ref"], "session_id": SESSION})
    assert r.status_code == 200 and r.json()["status"] == "ok", r.text
    assert _ids(c.post("/api/gateway/kg/query", headers=carol, json={"scope": "session", "session_id": SESSION})) == {"mem-c"}


def test_another_account_reads_none_of_it_through_any_route(team) -> None:
    c, dave = team["c"], team["dave"]
    key = urllib.parse.quote(f"artifact:{team['art_ref']['artifact_id']}", safe=":")
    attempts = {
        "memory/query session": c.post("/api/gateway/memory/query", headers=dave, json={"scope": "session", "session_id": SESSION}),
        "memory/query run": c.post("/api/gateway/memory/query", headers=dave, json={"scope": "session", "run_id": team["run_id"]}),
        "memory/query owner_id": c.post("/api/gateway/memory/query", headers=dave, json={"scope": "session", "owner_id": team["owner"]}),
        "memory/query entity": c.post("/api/gateway/memory/query", headers=dave, json={"scope": "global", "owner_id": team["entity_owner"]}),
        "memory/query all_owners": c.post("/api/gateway/memory/query", headers=dave, json={"scope": "all", "all_owners": True}),
        "history": c.get("/api/gateway/memory/mem-c/history", headers=dave, params={"session_id": SESSION}),
        "history bare": c.get("/api/gateway/memory/mem-c/history", headers=dave),
        "sources": c.get("/api/gateway/memory/mem-c/sources", headers=dave),
        "reconstruct": c.post("/api/gateway/memory/sources/reconstruct", headers=dave, json={"ref": team["art_ref"], "session_id": SESSION}),
        "reconstruct bare": c.post("/api/gateway/memory/sources/reconstruct", headers=dave, json={"ref": team["art_ref"]}),
        "derived": c.get(f"/api/gateway/memory/derived?ref_key={key}&session_id={SESSION}", headers=dave),
        "kg session": c.post("/api/gateway/kg/query", headers=dave, json={"scope": "session", "session_id": SESSION}),
        "kg run": c.post("/api/gateway/kg/query", headers=dave, json={"scope": "run", "run_id": team["run_id"]}),
        "kg owner_id": c.post("/api/gateway/kg/query", headers=dave, json={"scope": "session", "owner_id": team["owner"]}),
        "kg entity": c.post("/api/gateway/kg/query", headers=dave, json={"scope": "global", "owner_id": team["entity_owner"]}),
        "kg all_owners": c.post("/api/gateway/kg/query", headers=dave, json={"scope": "session", "all_owners": True}),
    }
    for name, r in attempts.items():
        # 403 owner_forbidden, or 404 for an id that does not exist in dave's own plane: never data.
        assert r.status_code in (403, 404), (name, r.status_code, r.text)
        if r.status_code == 403:
            assert r.json()["detail"]["reason"] == "owner_forbidden", (name, r.text)
    assert attempts["memory/query owner_id"].status_code == 403
    assert attempts["kg owner_id"].status_code == 403 and attempts["kg all_owners"].status_code == 403
    assert attempts["memory/query entity"].status_code == 403 and attempts["kg entity"].status_code == 403


def test_the_rule_refuses_another_principal_even_on_the_owners_plane(team) -> None:
    """The plane is not a sharing scope: evaluated against carol's plane and stores, the rule gives dave
    none of carol's owners (her run, her session, her entity, her plane's global owner)."""
    from fastapi import HTTPException

    from abstractgateway.memory_access import caller_owner_ids

    svc, dave = team["svc"], _principal("dave", "dave")
    for kwargs in ({"run_id": team["run_id"]}, {"session_id": SESSION}):
        with pytest.raises(HTTPException) as refused:
            caller_owner_ids(dave, svc, **kwargs)
        assert refused.value.status_code == 403 and refused.value.detail["reason"] == "owner_forbidden"
    assert caller_owner_ids(dave, svc) == set()
    carol = caller_owner_ids(_principal("carol", "carol"), svc, session_id=SESSION)
    assert carol == {"global_memory", team["owner"], team["entity_owner"]}
    from abstractgateway.security.principal import local_admin_principal

    assert caller_owner_ids(local_admin_principal(), svc) is None  # admin: every owner


def test_the_plane_global_owner_is_the_accounts_own(team) -> None:
    c = team["c"]
    assert _ids(c.post("/api/gateway/memory/query", headers=team["carol"], json={"scope": "global"})) == {"mem-g"}
    assert _ids(c.post("/api/gateway/kg/query", headers=team["carol"], json={"scope": "global"})) == {"mem-g"}
    # dave's own plane has its own (empty) global memory; carol's never reaches him.
    r = c.post("/api/gateway/memory/query", headers=team["dave"], json={"scope": "global"})
    assert r.status_code == 200 and r.json()["items"] == [], r.text


def test_a_principal_reads_the_graph_of_the_entity_she_created(team) -> None:
    c, carol, dave = team["c"], team["carol"], team["dave"]
    body = {"scope": "global", "owner_id": team["entity_owner"]}
    assert _ids(c.post("/api/gateway/memory/query", headers=carol, json=body)) == {"mem-e"}
    assert _ids(c.post("/api/gateway/kg/query", headers=carol, json=body)) == {"mem-e"}
    assert c.get("/api/gateway/memory/mem-e/history", headers=carol).status_code == 200
    assert c.post("/api/gateway/memory/query", headers=dave, json=body).status_code == 403
    assert c.post("/api/gateway/kg/query", headers=dave, json=body).status_code == 403
    assert c.get("/api/gateway/memory/mem-e/history", headers=dave).status_code in (403, 404)  # never data
