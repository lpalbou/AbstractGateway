"""Entity mutation routes are ADMIN-GATED (config-object plan N1, signed
2026-07-11; proof row R4).

GW-H makes entities (and users) non-admin principals of the same door.
Before this gate any AUTHENTICATED principal could reembed another
entity's vectors, rewrite its mind substrate, its tool grants, its prompt
overlay, flip its lifecycle state, or start its own-time loop. The rows in
GATEWAY_ROUTE_POLICIES make the split auditable in ONE table.

R4 proof shape (agency c675): served-surface, GW-B pattern — a NON-admin
principal hits every entity MUTATION route: all refuse with the authz
error naming the required role; the SAME principal's GETs + chat stay
authorized (the middleware lets them through to the route table). Real
HTTP through the real middleware + route table, never store-level asserts.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("yaml")


# Every entity MUTATION route named by the signed N1 finding, plus
# workspace/mounts (host-filesystem exposure — same rule that admin-gates
# /api/gateway/files). Method + path + a minimal body that would parse.
_MUTATION_CASES = [
    ("POST", "/api/gateway/entities/Castor/state", {"state": "asleep"}),
    ("POST", "/api/gateway/entities/Castor/reembed", {"model": "x", "confirm_token": "y"}),
    ("PUT", "/api/gateway/entities/Castor/tool-policy", {"policy": {"visit": []}}),
    ("PUT", "/api/gateway/entities/Castor/prompt", {"overlay": {"operator": "hi"}}),
    ("PUT", "/api/gateway/entities/Castor/substrate", {"provider": "p", "model": "m"}),
    ("POST", "/api/gateway/entities/Castor/loop/start", {}),
    ("POST", "/api/gateway/entities/Castor/loop/stop", {}),
    ("PUT", "/api/gateway/entities/Castor/workspace/mounts", {"mounts": []}),
]

# User-level surfaces that must stay AUTHORIZED for the same non-admin
# principal (the authz layer passes; downstream 404s are fine — the
# principal's own runtime has no such entity, which is scoping, not authz).
_USER_LEVEL_CASES = [
    ("GET", "/api/gateway/entities", None),
    ("GET", "/api/gateway/entities/Castor", None),
    ("GET", "/api/gateway/entities/Castor/state", None),
    ("GET", "/api/gateway/entities/Castor/tool-policy", None),
    ("GET", "/api/gateway/entities/Castor/prompt", None),
    ("GET", "/api/gateway/entities/Castor/substrate", None),
    ("GET", "/api/gateway/entities/Castor/loop", None),
    ("GET", "/api/gateway/entities/Castor/workspace/mounts", None),
    ("POST", "/api/gateway/entities/Castor/chat/open", {}),
    ("POST", "/api/gateway/entities/Castor/visit/open", {}),
    ("POST", "/api/gateway/entities/Castor/summon", {}),
    ("POST", "/api/gateway/entities/auth/probe", {}),
    # Creation-modal read/dry-run surfaces (plan (b), P1-1): birth-with-
    # defaults + read-only gallery/inventory are USER acts — a user may create
    # a default-configured entity and see the templates/tools; only MUTATING
    # substrate/tools/mind (the _MUTATION_CASES above) is admin. The modal's
    # full expert flow needs admin because its config steps do; the template
    # fast-path create works for any authenticated user.
    ("POST", "/api/gateway/entities/Castor/validate", {"name": "Castor"}),
    ("GET", "/api/gateway/entities/templates", None),
    ("GET", "/api/gateway/entities/inventory/tools", None),
    ("GET", "/api/gateway/entities/inventory/capability-matrix", None),
]


def _routes_declared_in_source() -> set[tuple[str, str]]:
    """The mutation-route inventory straight from the served route table so a
    NEW mutation route added without a policy row fails THIS test, not a
    production incident."""
    # BOTH routers serve the /api/gateway/entities surface (routes/__init__.py;
    # app.py mounts both under /api). The drift pin must walk both or a
    # mutating route added to entity_replay.py would ship ungated + undetected
    # (adversary find, 2026-07-11) — the exact one-table-auditability the
    # policy exists for.
    from abstractgateway.routes import entities_router, entity_replay_router

    out: set[tuple[str, str]] = set()
    for router in (entities_router, entity_replay_router):
        for route in router.routes:
            # The routers carry "/gateway/entities/..." themselves; the app
            # mounts them under "/api" (app.py) — mirror that exact mount here.
            path = "/api" + str(getattr(route, "path", ""))
            for method in getattr(route, "methods", None) or ():
                if method in {"POST", "PUT", "PATCH", "DELETE"}:
                    out.add((method, path))
    return out


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "admin-token")
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")

    from abstractgateway.routes import entities_router, gateway_router
    from abstractgateway.security import GatewaySecurityMiddleware, load_gateway_auth_policy_from_env

    app = FastAPI()
    app.add_middleware(GatewaySecurityMiddleware, policy=load_gateway_auth_policy_from_env())
    app.include_router(entities_router, prefix="/api")
    app.include_router(gateway_router, prefix="/api")
    return TestClient(app)


def _non_admin_bearer(client: TestClient) -> dict[str, str]:
    created = client.post(
        "/api/gateway/admin/users",
        headers={"Authorization": "Bearer admin-token"},
        json={"user_id": "mallory", "tenant_id": "default", "roles": ["user"]},
    )
    assert created.status_code == 200, created.text
    token = created.json().get("token")
    assert token, "user creation must issue a token"
    return {"Authorization": f"Bearer {token}"}


def test_every_entity_mutation_route_refuses_non_admin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _client(tmp_path, monkeypatch) as client:
        headers = _non_admin_bearer(client)
        for method, path, body in _MUTATION_CASES:
            response = client.request(method, path, headers=headers, json=body)
            assert response.status_code == 403, f"{method} {path}: {response.status_code} {response.text}"
            payload = response.json()
            requirement = payload.get("requirement") or {}
            named = (
                requirement.get("required_role")
                or payload.get("required_role")
                or payload.get("detail", "")
            )
            assert "admin" in str(named).lower(), f"{method} {path} refusal must name the required role: {payload}"


def test_user_level_entity_surfaces_stay_authorized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same non-admin principal is never 401/403'd on GETs, chat, visit,
    summon, or the auth probe — those may 404/400/409 downstream (no such
    entity in the principal's own runtime), which proves authorization
    passed and only scoping/validation spoke."""
    with _client(tmp_path, monkeypatch) as client:
        headers = _non_admin_bearer(client)
        for method, path, body in _USER_LEVEL_CASES:
            response = client.request(method, path, headers=headers, json=body)
            assert response.status_code not in (401, 403), (
                f"{method} {path} must stay user-level, got {response.status_code}: {response.text}"
            )


def test_admin_principal_passes_the_gate_to_the_route_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The static admin token (default/admin identity, is_admin) reaches the route
    table on every gated path — refusals from here down are the ROUTE's
    (404 unknown entity), never the authorization layer's 403."""
    with _client(tmp_path, monkeypatch) as client:
        headers = {"Authorization": "Bearer admin-token"}
        for method, path, body in _MUTATION_CASES:
            response = client.request(method, path, headers=headers, json=body)
            assert response.status_code != 403, f"{method} {path}: admin must never be authz-refused"


def test_policy_rows_cover_the_live_mutation_route_table() -> None:
    """Drift pin: every WRITE route under /api/gateway/entities is either
    admin-gated by GATEWAY_ROUTE_POLICIES or on the EXPLICIT user-level
    allowlist below. A new mutation route lands RED here until its author
    decides which side it belongs to — the one-table auditability N1 exists
    for."""
    from abstractgateway.security.authorization import gateway_route_authorization_requirement

    # Interaction surfaces (chat class): deliberately user-level.
    user_level_writes = {
        ("POST", "/api/gateway/entities"),  # creation mints a NON-admin principal; per-principal scoped
        ("POST", "/api/gateway/entities/{name}/validate"),  # DRY-RUN lint: READ-ONLY (writes nothing), same level as create
        ("POST", "/api/gateway/entities/auth/probe"),
        ("POST", "/api/gateway/entities/{name}/summon"),
        # The summon queue's step-away (contract §6): the waiter's own button;
        # the queue_id is the capability, same interaction class as summon.
        ("POST", "/api/gateway/entities/{name}/queue/{queue_id}/leave"),
        ("POST", "/api/gateway/entities/{name}/chat/open"),
        ("POST", "/api/gateway/entities/{name}/chat/{chat_id}/turn"),
        ("POST", "/api/gateway/entities/{name}/chat/{chat_id}/close"),
        ("POST", "/api/gateway/entities/{name}/visit/open"),
        ("POST", "/api/gateway/entities/{name}/visit/{run_id}/turn"),
        ("POST", "/api/gateway/entities/{name}/visit/{run_id}/close"),
        ("POST", "/api/gateway/entities/{name}/visit/{run_id}/tick"),
        ("POST", "/api/gateway/entities/meets/open"),
        ("POST", "/api/gateway/entities/meets/{meet_id}/relay"),
        ("POST", "/api/gateway/entities/meets/{meet_id}/close"),
        ("POST", "/api/gateway/entities/{name}/workspace/file"),  # contained drop-a-file collaboration surface
        # Audition/speech lane (laurent dm#10): speaking AS the entity is an
        # interaction surface like chat/visit — user-level; the voice CHOICE
        # (PUT /{name}/voice) stays admin-gated in the policy table.
        ("POST", "/api/gateway/entities/{name}/voice/tts"),
        ("POST", "/api/gateway/entities/{name}/voice/tts/stream"),
    }

    for method, path in sorted(_routes_declared_in_source()):
        concrete = (
            path.replace("{name}", "castor")
            .replace("{chat_id}", "chat-x")
            .replace("{run_id}", "run-x")
            .replace("{meet_id}", "meet-x")
        )
        requirement = gateway_route_authorization_requirement(concrete, method)
        if (method, path) in user_level_writes:
            assert requirement is None, f"{method} {path} is on the user-level allowlist but a policy row gates it"
        else:
            assert requirement is not None, (
                f"{method} {path} is a NEW entity mutation route with no policy row — "
                "add it to GATEWAY_ROUTE_POLICIES (admin) or the explicit user-level allowlist in this test"
            )
            assert requirement.required_role == "admin"
