"""Route authorization contract test (backlog 0070, S half).

THE INVARIANT, in three layers that must each stay pinned:

1. BOUNDARY — the security middleware only guards paths under
   ``/api/gateway`` (``GatewaySecurityMiddleware.__call__`` returns early for
   everything else). A route mounted OUTSIDE that prefix bypasses
   authentication entirely, so every route the app serves must either live
   under ``/api/gateway`` or be on the EXPLICIT public allowlist below with a
   recorded reason.

2. DECISION — every WRITE route under ``/api/gateway`` must have an explicit
   authorization decision: either a ``GATEWAY_ROUTE_POLICIES`` row admin-gates
   it, or it appears on the user-level allowlist below (safe because the
   per-principal service resolution in ``service.get_gateway_service`` scopes
   the mutation to the caller's OWN runtime, or because the handler gates
   in-handler). A new write route lands RED here until its author decides
   which side it belongs to — the one-table auditability 0070 exists for.

3. SERVED SURFACE — the table is only real if the middleware enforces it:
   a non-admin principal is 403'd on a representative route of EVERY policy
   row (refusal names the required role), and representative user-level
   writes pass authorization (downstream 4xx is scoping/validation speaking,
   never the authz layer).

The entities-only twin (`test_gateway_entities_admin_gate.py`) keeps its
richer per-route proofs; this file is the whole-app superset.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("yaml")


# ---------------------------------------------------------------------------
# Layer 1: routes allowed OUTSIDE the /api/gateway middleware boundary.
# Every entry needs a reason — these are served with NO bearer/session check.
# ---------------------------------------------------------------------------
PUBLIC_ROUTES: set[tuple[str, str]] = {
    ("GET", "/"),  # redirect to /console (no data)
    ("GET", "/console"),  # static operator HTML; every API call it makes is authenticated
    ("GET", "/docs"),  # offline OpenAPI viewer (static HTML)
    ("GET", "/redoc"),  # offline OpenAPI viewer (static HTML)
    ("GET", "/openapi.json"),  # FastAPI schema (route shapes only, no data)
    ("GET", "/api/health"),  # liveness contract for Docker/launcher probes
    # Email triage action links are CAPABILITY URLS: the {token} is an
    # HMAC-signed credential verified against TRIAGE_ACTION_SECRET, and the
    # whole surface is 503-disabled unless the operator configured the
    # secret. The token IS the auth; bearer auth cannot ride an email link.
    ("GET", "/api/triage/action/{token}"),
    ("POST", "/api/triage/action/{token}"),
}


# ---------------------------------------------------------------------------
# Layer 2: WRITE routes under /api/gateway that are deliberately user-level
# (no GATEWAY_ROUTE_POLICIES row). Grouped by the reason they are safe.
# ---------------------------------------------------------------------------
USER_LEVEL_WRITES: set[tuple[str, str]] = {
    # --- session lifecycle -------------------------------------------------
    # login is the ONE public write (`_public_auth_path`); logout needs the
    # session it destroys.
    ("POST", "/api/gateway/session/login"),
    ("POST", "/api/gateway/session/logout"),
    # First-run claim (2026-09-23): the second public write. It mints a
    # session only for a one-time code written by the LOCAL CLI into the data
    # dir, and the route itself refuses any non-loopback or proxied peer.
    ("POST", "/api/gateway/session/claim"),
    # --- self-service surfaces: the caller acts only on itself -------------
    # A polite dequeue of the CALLER from a queue it joined (interaction
    # surface, same class as chat/visit/summon).
    ("POST", "/api/gateway/entities/{name}/queue/{queue_id}/leave"),
    # The caller's OWN workspace policy. Its GET sibling is documented
    # "user-level by design — every principal may read their own policy"; the
    # PUT writes that same per-user entry and nobody else's.
    ("PUT", "/api/gateway/workspace/policy/self"),
    # --- run lifecycle: per-principal service scoping ----------------------
    # `get_gateway_service()` resolves the CALLER's runtime via the principal
    # contextvar — these mutate the caller's own runs, flows, and stores,
    # never another principal's.
    #
    # THIS JUSTIFICATION USED TO BE CONDITIONAL ("when user auth is on") and
    # that caveat was the hole: with user auth OFF the dispatch skipped
    # per-principal resolution entirely and handed a registry-issued non-admin
    # the SHARED service, so these writes reached the operator's own bundles
    # and runs. `_principal_requires_isolation` now keys the split on the
    # identity's origin rather than a mode flag, which is what makes the
    # sentence above true unconditionally. If that function is ever narrowed,
    # every entry below stops being safe.
    ("POST", "/api/gateway/runs/start"),
    ("POST", "/api/gateway/runs/schedule"),
    ("POST", "/api/gateway/runs/purge_drafts"),
    ("POST", "/api/gateway/runs/ledger/batch"),
    ("POST", "/api/gateway/commands"),
    ("POST", "/api/gateway/runs/{run_id}/chat"),
    ("POST", "/api/gateway/runs/{run_id}/chat_threads"),
    ("POST", "/api/gateway/runs/{run_id}/summary"),
    ("POST", "/api/gateway/runs/{run_id}/workspace/open"),
    # --- capability execution: run-scoped or stateless ---------------------
    ("POST", "/api/gateway/runs/{run_id}/audio/transcribe"),
    ("POST", "/api/gateway/runs/{run_id}/images/generate"),
    ("POST", "/api/gateway/runs/{run_id}/images/edit"),
    ("POST", "/api/gateway/runs/{run_id}/images/upscale"),
    ("POST", "/api/gateway/runs/{run_id}/music/generate"),
    ("POST", "/api/gateway/runs/{run_id}/videos/generate"),
    ("POST", "/api/gateway/runs/{run_id}/videos/from_image"),
    ("POST", "/api/gateway/runs/{run_id}/voice/tts"),
    ("POST", "/api/gateway/runs/{run_id}/voice/tts/stream"),
    ("POST", "/api/gateway/embeddings"),
    ("POST", "/api/gateway/sandbox/generate"),
    ("POST", "/api/gateway/kg/query"),  # read-shaped POST (query in body)
    # --- uploads / reports: principal-scoped stores ------------------------
    ("POST", "/api/gateway/attachments/upload"),
    ("POST", "/api/gateway/bugs/report"),
    ("POST", "/api/gateway/features/report"),
    # --- catalog authoring: per-principal flows/bundles dir ----------------
    ("POST", "/api/gateway/bundles/reload"),
    ("POST", "/api/gateway/bundles/upload"),
    ("DELETE", "/api/gateway/bundles/{bundle_id}"),
    ("POST", "/api/gateway/bundles/{bundle_id}/deprecate"),
    ("POST", "/api/gateway/bundles/{bundle_id}/undeprecate"),
    ("POST", "/api/gateway/visualflows"),
    ("POST", "/api/gateway/visualflows/code/simulate"),
    ("PUT", "/api/gateway/visualflows/{flow_id}"),
    ("DELETE", "/api/gateway/visualflows/{flow_id}"),
    ("POST", "/api/gateway/visualflows/{flow_id}/publish"),
    # --- config: per-principal data dir or in-handler scope gating ---------
    # capability-defaults write to the CALLER's service data dir (the
    # gateway-global copy is only reachable by the admin identity).
    ("PUT", "/api/gateway/config/capability-defaults/{kind}/{modality}"),
    ("DELETE", "/api/gateway/config/capability-defaults/{kind}/{modality}"),
    ("PUT", "/api/gateway/config/capability-defaults/{kind}/{modality}/{task}"),
    ("DELETE", "/api/gateway/config/capability-defaults/{kind}/{modality}/{task}"),
    # endpoint profiles gate IN-HANDLER: gateway-scoped mutations require
    # admin, user-scoped ones don't (test_gateway_provider_endpoint_profiles
    # pins that split).
    ("POST", "/api/gateway/config/provider-endpoint-profiles"),
    ("POST", "/api/gateway/config/provider-endpoint-profiles/discover-models"),
    ("PUT", "/api/gateway/config/provider-endpoint-profiles/{profile_id}"),
    ("DELETE", "/api/gateway/config/provider-endpoint-profiles/{profile_id}"),
    # --- prompt-cache SESSION lane (the /prompt_cache CONTROL plane and
    # /blocs are admin-gated by policy rows; the per-session lane is scoped
    # to the caller's session) ----------------------------------------------
    ("POST", "/api/gateway/sessions/{session_id}/prompt_cache/prepare"),
    ("POST", "/api/gateway/sessions/{session_id}/prompt_cache/clear"),
    ("POST", "/api/gateway/sessions/{session_id}/prompt_cache/rebuild"),
    # --- entity interaction surfaces (chat class) --------------------------
    # The richer rationale lives in test_gateway_entities_admin_gate.py;
    # entity MUTATION routes (state/substrate/skills/voice/...) are policy-
    # gated and therefore absent from this list.
    ("POST", "/api/gateway/entities"),
    ("POST", "/api/gateway/entities/auth/probe"),
    ("POST", "/api/gateway/entities/{name}/validate"),
    ("POST", "/api/gateway/entities/{name}/summon"),
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
    ("POST", "/api/gateway/entities/{name}/workspace/file"),
    ("POST", "/api/gateway/entities/{name}/voice/tts"),
    ("POST", "/api/gateway/entities/{name}/voice/tts/stream"),
}


def _live_route_table() -> set[tuple[str, str]]:
    """Every (method, path) the real app serves, straight from app.routes.

    HEAD/OPTIONS are implementation noise (FastAPI adds them); the middleware
    passes OPTIONS through deliberately (CORS preflight carries no data).
    """
    from abstractgateway.app import app

    rows: set[tuple[str, str]] = set()

    def _walk(routes: Any, prefix: str = "") -> None:
        # FastAPI >= 0.141 does NOT flatten an included router into
        # `app.routes` any more: `include_router` appends ONE `_IncludedRouter`
        # wrapper holding the original router plus the prefix it was mounted
        # under. A flat scan of `app.routes` therefore returns ZERO gateway
        # routes, and every assertion in this module that loops over them
        # passes VACUOUSLY — the contract stops catching new ungated write
        # routes precisely when it is most needed. Recurse through the wrapper
        # so this table means what its docstring says again.
        for route in routes or []:
            original = getattr(route, "original_router", None)
            if original is not None:
                ctx = getattr(route, "include_context", None)
                _walk(getattr(original, "routes", None), prefix + str(getattr(ctx, "prefix", "") or ""))
                continue
            path = getattr(route, "path", None)
            if not path:
                continue
            for method in getattr(route, "methods", None) or {"GET"}:
                if method in {"HEAD", "OPTIONS"}:
                    continue
                rows.add((str(method), prefix + str(path)))

    _walk(app.routes)
    assert any(p.startswith("/api/gateway") for _m, p in rows), (
        "the live route table found ZERO /api/gateway routes — the enumeration "
        "broke (framework upgrade?) and every contract below would pass vacuously"
    )
    return rows


def _concrete(path: str) -> str:
    """Substitute path params so policy patterns match a real request shape."""
    return re.sub(r"\{[^}]+\}", "x", path)


def test_every_route_is_behind_the_security_middleware_or_explicitly_public() -> None:
    """BOUNDARY: the middleware guards only /api/gateway/* — anything mounted
    elsewhere is served unauthenticated. A new route outside the prefix lands
    RED here until it is either moved under the boundary or recorded above
    with the reason it may be public."""
    for method, path in sorted(_live_route_table()):
        if path.startswith("/api/gateway"):
            continue
        assert (method, path) in PUBLIC_ROUTES, (
            f"{method} {path} is served OUTSIDE the /api/gateway security boundary "
            "and is not on the explicit public allowlist — it bypasses auth entirely. "
            "Move it under /api/gateway or record it in PUBLIC_ROUTES with a reason."
        )


PUBLIC_WRITES = {
    ("POST", "/api/gateway/session/login"),
    # First-run claim (2026-09-23): loopback-peer-only, one-time code from the
    # local CLI; see test_gateway_first_run.py for the peer/replay pins.
    ("POST", "/api/gateway/session/claim"),
}


def test_the_public_write_exemption_is_exactly_session_login() -> None:
    """Inside the boundary, exactly TWO writes skip authentication: the login
    that mints credentials and the first-run claim that redeems a one-time
    local code. Widening `_public_auth_path` widens the unauthenticated
    surface — it must never happen silently."""
    from abstractgateway.security import load_gateway_auth_policy_from_env
    from abstractgateway.security.gateway_security import GatewaySecurityMiddleware

    middleware = GatewaySecurityMiddleware(lambda *_: None, policy=load_gateway_auth_policy_from_env())
    for method, path in PUBLIC_WRITES:
        assert middleware._public_auth_path(path, method) is True
    for method, path in sorted(_live_route_table()):
        if not path.startswith("/api/gateway"):
            continue
        if (method, path) in PUBLIC_WRITES:
            continue
        assert middleware._public_auth_path(_concrete(path), method) is False, (
            f"{method} {path} is exempted from authentication by _public_auth_path — "
            "only session/login and session/claim may be public inside the boundary"
        )


def test_every_write_route_has_an_explicit_authorization_decision() -> None:
    """DECISION: every write under /api/gateway is either admin-gated by a
    GATEWAY_ROUTE_POLICIES row or on the user-level allowlist above. Both
    directions are pinned so the allowlist stays honest: a listed route that
    GAINS a policy row must be removed from the list."""
    from abstractgateway.security.authorization import gateway_route_authorization_requirement

    for method, path in sorted(_live_route_table()):
        if not path.startswith("/api/gateway"):
            continue
        if method not in {"POST", "PUT", "PATCH", "DELETE"}:
            continue
        requirement = gateway_route_authorization_requirement(_concrete(path), method)
        if (method, path) in USER_LEVEL_WRITES:
            assert requirement is None, (
                f"{method} {path} is on the user-level allowlist but a policy row now "
                "gates it — remove it from USER_LEVEL_WRITES so the list stays truthful"
            )
        else:
            assert requirement is not None, (
                f"{method} {path} is a NEW write route with no authorization decision — "
                "add a GATEWAY_ROUTE_POLICIES row (admin) or record it in "
                "USER_LEVEL_WRITES with the reason it is safe for any principal"
            )
            assert requirement.required_role == "admin"


def test_no_policy_row_is_dead() -> None:
    """Every GATEWAY_ROUTE_POLICIES row must match at least one live route.
    A dead row means the surface it guarded moved or was renamed — the
    protection silently stopped applying (drift in the other direction)."""
    from abstractgateway.security.authorization import GATEWAY_ROUTE_POLICIES

    live = [(_concrete(path), method) for method, path in _live_route_table()]
    for index, policy in enumerate(GATEWAY_ROUTE_POLICIES):
        hits = [x for x in live if policy.matches(x[0], x[1])]
        assert hits, (
            f"GATEWAY_ROUTE_POLICIES[{index}] (resource={policy.resource!r}, "
            f"prefixes={policy.prefixes!r}, exact={policy.exact!r}, pattern={policy.pattern!r}) "
            "matches ZERO live routes — the surface it guarded moved; update or remove the row"
        )


# ---------------------------------------------------------------------------
# Layer 3: served-surface proof through the real middleware.
# ---------------------------------------------------------------------------


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "admin-token")
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")

    from abstractgateway.routes import (
        entities_router,
        entity_replay_router,
        gateway_router,
        triage_router,
    )
    from abstractgateway.security import GatewaySecurityMiddleware, load_gateway_auth_policy_from_env

    app = FastAPI()
    app.add_middleware(GatewaySecurityMiddleware, policy=load_gateway_auth_policy_from_env())
    # Mirror app.py's mount order (entities first: literal paths win).
    app.include_router(entities_router, prefix="/api")
    app.include_router(entity_replay_router, prefix="/api")
    app.include_router(gateway_router, prefix="/api")
    app.include_router(triage_router, prefix="/api")
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


def _representative_route_per_policy_row() -> list[tuple[int, str, str]]:
    """One live (method, concrete path) per policy row, deterministically.

    Firing a request per ROW (not per route) keeps the served-surface proof
    fast while still exercising every protection family through the real
    middleware. Refusal happens in the middleware BEFORE the handler runs, so
    write representatives execute no handler side effects.
    """
    from abstractgateway.security.authorization import GATEWAY_ROUTE_POLICIES

    live = sorted(_live_route_table())
    out: list[tuple[int, str, str]] = []
    for index, policy in enumerate(GATEWAY_ROUTE_POLICIES):
        for method, path in live:
            concrete = _concrete(path)
            if policy.matches(concrete, method):
                out.append((index, method, concrete))
                break
    return out


def test_served_surface_non_admin_is_refused_on_every_policy_family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    representatives = _representative_route_per_policy_row()
    assert representatives, "policy table must not be empty"
    with _client(tmp_path, monkeypatch) as client:
        headers = _non_admin_bearer(client)
        for index, method, path in representatives:
            response = client.request(method, path, headers=headers, json={})
            assert response.status_code == 403, (
                f"policy row {index}: {method} {path} answered {response.status_code} "
                f"for a non-admin principal, expected 403: {response.text}"
            )
            payload = response.json()
            named = payload.get("required_role") or payload.get("detail", "")
            assert "admin" in str(named).lower(), (
                f"policy row {index}: {method} {path} refusal must name the required role: {payload}"
            )


def test_served_surface_user_level_writes_pass_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Representative user-level writes reach the route table for a non-admin
    principal — downstream 4xx (validation, unknown ids, empty stores) is the
    ROUTE speaking, which proves the authorization layer passed."""
    representatives = [
        ("POST", "/api/gateway/attachments/upload"),
        ("POST", "/api/gateway/runs/start"),
        ("POST", "/api/gateway/kg/query"),
        ("POST", "/api/gateway/visualflows"),
        ("POST", "/api/gateway/entities/Nobody/chat/open"),
    ]
    with _client(tmp_path, monkeypatch) as client:
        headers = _non_admin_bearer(client)
        for method, path in representatives:
            response = client.request(method, path, headers=headers, json={})
            assert response.status_code not in (401, 403), (
                f"{method} {path} must stay user-level, got {response.status_code}: {response.text}"
            )


def test_served_surface_unauthenticated_requests_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No credential = 401 on both reads and writes (fail-closed defaults),
    while the public login write is served without one."""
    with _client(tmp_path, monkeypatch) as client:
        assert client.get("/api/gateway/runs").status_code == 401
        assert client.post("/api/gateway/runs/start", json={}).status_code == 401
        # Login is public: a garbage body must reach the HANDLER (its own
        # 4xx), never the middleware's bearer demand.
        response = client.post("/api/gateway/session/login", json={})
        assert response.status_code != 503, response.text
        www = response.headers.get("www-authenticate", "")
        assert "bearer" not in www.lower(), "login must not demand a bearer token"
