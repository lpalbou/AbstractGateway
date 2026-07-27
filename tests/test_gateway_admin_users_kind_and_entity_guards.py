"""Operator order c5305 + backlog 0089 pins.

(1) Clean listing: every /admin/users row carries a first-class
`principal_kind` ("human"|"entity" — derived ONCE from the roles convention,
never re-derived by clients) and the route takes ?kind=human|entity|all.
The census asymmetry is deliberate: /entities lists HOMES, ?kind=entity
lists PRINCIPALS — homes created before GW-H minting have no row.

(2) 0089 guards: PATCH token/rotate_token/roles/runtime_id and DELETE on
entity principals refuse 403 naming the entities lane — a rotation mints a
live entity bearer that must not exist; a delete removes the name-collision
guard and invites identity capture. `enabled` stays editable (the door-side
disable). The guard lives at the REGISTRY chokepoint so the config CLI
refuses too.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "admin-token")
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_USERS_FILE", str(tmp_path / "runtime" / "auth" / "users.json"))

    from abstractgateway.routes import gateway_router
    from abstractgateway.security import GatewaySecurityMiddleware, load_gateway_auth_policy_from_env

    app = FastAPI()
    app.add_middleware(GatewaySecurityMiddleware, policy=load_gateway_auth_policy_from_env())
    app.include_router(gateway_router, prefix="/api")
    return TestClient(app)


_ADMIN = {"Authorization": "Bearer admin-token"}


def _seed(client: TestClient) -> None:
    r1 = client.post(
        "/api/gateway/admin/users",
        headers=_ADMIN,
        json={"user_id": "alice", "roles": ["user"]},
    )
    assert r1.status_code == 200, r1.text
    # Entity principals are minted by the ENTITIES lane (the registry
    # directly), never the generic admin POST (which now refuses role=entity
    # — adversary P1). Mint hypnos the way entities.py does.
    from abstractgateway.users import GatewayUserRegistry

    GatewayUserRegistry().create_user(user_id="hypnos", roles=["entity"], scopes=["entity:hypnos"])


def test_rows_carry_principal_kind_and_kind_filter_works(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    _seed(client)

    all_rows = client.get("/api/gateway/admin/users", headers=_ADMIN)
    assert all_rows.status_code == 200, all_rows.text
    body = all_rows.json()
    assert body["kind"] == "all"
    by_id = {r["user_id"]: r for r in body["users"]}
    assert by_id["alice"]["principal_kind"] == "human"
    assert by_id["hypnos"]["principal_kind"] == "entity"

    humans = client.get("/api/gateway/admin/users?kind=human", headers=_ADMIN).json()["users"]
    assert {r["user_id"] for r in humans} >= {"alice"}
    assert all(r["principal_kind"] == "human" for r in humans)
    assert "hypnos" not in {r["user_id"] for r in humans}

    entities = client.get("/api/gateway/admin/users?kind=entity", headers=_ADMIN).json()["users"]
    assert {r["user_id"] for r in entities} == {"hypnos"}

    bad = client.get("/api/gateway/admin/users?kind=robot", headers=_ADMIN)
    assert bad.status_code == 400
    assert "human | entity | all" in bad.json()["detail"]


def test_entity_principal_patch_guards_refuse_403(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    _seed(client)

    for payload in (
        {"rotate_token": True},
        {"token": "agw_attacker"},
        {"roles": ["user"]},
        {"runtime_id": "elsewhere"},
    ):
        r = client.patch("/api/gateway/admin/users/hypnos", headers=_ADMIN, json=payload)
        assert r.status_code == 403, (payload, r.text)
        assert "summoned entity 'hypnos'" in r.json()["detail"]
        assert "/entities" in r.json()["detail"]

    # The editable-fields trio stays editable on entity principals (spec:
    # enabled=door-side disable, email, scopes). All three, not just enabled.
    ok = client.patch("/api/gateway/admin/users/hypnos", headers=_ADMIN, json={"enabled": False})
    assert ok.status_code == 200, ok.text
    assert ok.json()["user"]["enabled"] is False
    ok2 = client.patch("/api/gateway/admin/users/hypnos", headers=_ADMIN, json={"email": "h@x.test"})
    assert ok2.status_code == 200, ok2.text
    assert ok2.json()["user"]["email"] == "h@x.test"
    ok3 = client.patch(
        "/api/gateway/admin/users/hypnos", headers=_ADMIN, json={"scopes": ["entity:hypnos", "extra"]}
    )
    assert ok3.status_code == 200, ok3.text
    assert "extra" in ok3.json()["user"]["scopes"]

    # Human principals keep the full surface (rotate works, returns a token).
    rot = client.patch("/api/gateway/admin/users/alice", headers=_ADMIN, json={"rotate_token": True})
    assert rot.status_code == 200, rot.text
    assert rot.json().get("token")


def test_entity_principal_delete_refuses_403_humans_still_delete(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    _seed(client)

    r = client.delete("/api/gateway/admin/users/hypnos", headers=_ADMIN)
    assert r.status_code == 403, r.text
    assert "name-collision guard" in r.json()["detail"]

    # Still listed after the refusal (nothing was deleted).
    entities = client.get("/api/gateway/admin/users?kind=entity", headers=_ADMIN).json()["users"]
    assert {row["user_id"] for row in entities} == {"hypnos"}

    ok = client.delete("/api/gateway/admin/users/alice", headers=_ADMIN)
    assert ok.status_code == 200, ok.text


def test_generic_create_refuses_entity_role(tmp_path: Path, monkeypatch) -> None:
    """Adversary P1 front door: the generic admin POST must not mint a
    role=entity principal (it would hand out a live entity bearer + seed an
    identity-capture record). Entity principals are the entities lane's."""
    client = _client(tmp_path, monkeypatch)
    r = client.post(
        "/api/gateway/admin/users",
        headers=_ADMIN,
        json={"user_id": "ghost", "roles": ["entity"]},
    )
    assert r.status_code == 403, r.text
    assert "/entities" in r.json()["detail"]
    # Case-insensitively too.
    r2 = client.post(
        "/api/gateway/admin/users",
        headers=_ADMIN,
        json={"user_id": "ghost2", "roles": ["User", "ENTITY"]},
    )
    assert r2.status_code == 403, r2.text


def test_reservation_transfer_cannot_rewrite_an_entity_runtime(tmp_path: Path, monkeypatch) -> None:
    """Adversary P0: transfer_runtime_reservation rewrites the target's
    runtime_id — the same field update_user guards. Transferring a retained
    reservation ONTO an entity principal must refuse 403, or the runtime_id
    guard is sidesteppable and the entity's runtime name becomes a purgeable
    reservation (the 0089 data-purge hazard, reachable with no delete)."""
    client = _client(tmp_path, monkeypatch)
    _seed(client)  # alice (human), hypnos (entity)

    # Mint a retained reservation by deleting a disposable human user.
    client.post("/api/gateway/admin/users", headers=_ADMIN, json={"user_id": "bob", "roles": ["user"]})
    d = client.delete("/api/gateway/admin/users/bob", headers=_ADMIN)
    assert d.status_code == 200, d.text

    # Transfer bob's retained runtime onto the entity hypnos -> refuse.
    r = client.post(
        "/api/gateway/admin/runtime-reservations/bob/transfer",
        headers=_ADMIN,
        json={"target_user_id": "hypnos", "confirm_runtime_id": "bob"},
    )
    assert r.status_code == 403, r.text
    assert "summoned entity 'hypnos'" in r.json()["detail"]

    # hypnos's runtime_id is unchanged (still its own slug).
    row = client.get("/api/gateway/admin/users/hypnos", headers=_ADMIN).json()["user"]
    assert row["runtime_id"] == "hypnos"

    # Transferring onto a HUMAN target still works.
    ok = client.post(
        "/api/gateway/admin/runtime-reservations/bob/transfer",
        headers=_ADMIN,
        json={"target_user_id": "alice", "confirm_runtime_id": "bob"},
    )
    assert ok.status_code == 200, ok.text


def test_registry_chokepoint_guards_cover_the_cli_path(tmp_path: Path, monkeypatch) -> None:
    """The guard must live in GatewayUserRegistry itself (0089: 'neither the
    routes layer nor GatewayUserRegistry checks roles') so the config CLI
    and any future surface refuse too."""
    monkeypatch.setenv("ABSTRACTGATEWAY_USERS_FILE", str(tmp_path / "users.json"))
    from abstractgateway.users import EntityPrincipalGuardError, GatewayUserRegistry

    reg = GatewayUserRegistry()
    reg.create_user(user_id="vera", roles=["entity"], scopes=["entity:vera"])

    with pytest.raises(EntityPrincipalGuardError):
        reg.update_user(user_id="vera", token="")
    with pytest.raises(EntityPrincipalGuardError):
        reg.update_user(user_id="vera", roles=["user"])
    with pytest.raises(EntityPrincipalGuardError):
        reg.update_user(user_id="vera", runtime_id="elsewhere")
    with pytest.raises(EntityPrincipalGuardError):
        reg.delete_user(user_id="vera")

    # enabled / email / scopes pass (spec: door-side disable stays).
    rec, tok = reg.update_user(user_id="vera", enabled=False)
    assert rec.enabled is False and tok is None
    assert rec.principal_kind == "entity"
