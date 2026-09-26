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
    # Browser-app sign-in handover (routes/apps.py): a CAPABILITY URL. The
    # {code} is minted by the authenticated `POST /api/gateway/apps/{id}/open`,
    # works once, for 2 minutes, only on the host it was minted for, and
    # creates a session for the principal who minted it — never another.
    ("GET", "/apps/handover/{code}"),
    # Terminal-app sign-in handover (routes/apps.py, mission Y): the one-time
    # {code} in the body is minted by the ADMIN-gated
    # `POST /api/gateway/apps/{id}/launch-tui`, works once, for 2 minutes, only
    # from a loopback socket peer with no proxy headers, and yields a
    # loopback-only token acting as the principal who minted it.
    ("POST", "/apps/tui-handover"),
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
    # Desktop hand-over (CONTRACTS A1, 2026-09-25): the third public write.
    # It mints a session only for a one-time code the gateway wrote into a
    # 0600 file of its data dir when a person opened the Assistant, and the
    # route refuses any non-loopback, proxied or app-server-relayed caller.
    ("POST", "/api/gateway/apps/desktop-handover"),
    # --- self-service surfaces: the caller acts only on itself -------------
    # A polite dequeue of the CALLER from a queue it joined (interaction
    # surface, same class as chat/visit/summon).
    ("POST", "/api/gateway/entities/{name}/queue/{queue_id}/leave"),
    # The caller's OWN workspace policy. Its GET sibling is documented
    # "user-level by design — every principal may read their own policy"; the
    # PUT writes that same per-user entry and nobody else's.
    ("PUT", "/api/gateway/workspace/policy/self"),
    # Opening a RUNNING browser app mints a one-time sign-in link for the
    # CALLER only (routes/apps.py); it starts nothing and installs nothing.
    ("POST", "/api/gateway/apps/{app_id}/open"),
    # --- run lifecycle: per-principal service scoping ----------------------
    # `get_gateway_service()` resolves the CALLER's runtime via the principal
    # contextvar — these mutate the caller's own runs, flows, and stores,
    # never another principal's.
    #
    # THIS JUSTIFICATION IS CONDITIONAL ON THE MODE, and that is fine only
    # because of the rule that closes the other half. With user accounts ON,
    # `get_gateway_service()` resolves a per-principal service. With user
    # accounts OFF it hands EVERY principal the ONE shared service, the
    # operator's — so a non-admin registry identity reaching these routes
    # would act on the operator's own runs, bundles and gateway-wide config
    # (mission AA reproduced exactly that through `/session/login`, 2026-09-24).
    # What makes the sentence above true in both modes is that such an
    # identity can no longer be AUTHENTICATED with user accounts off: bearer
    # auth only consults the registry when user accounts are on, and every
    # session path (login, `create_session`, `authenticate_session`) refuses
    # it via `security/sessions.py::principal_barred_from_shared_runtime`,
    # which reads the same mode the service routing reads. (An earlier comment
    # here credited a `_principal_requires_isolation` function; it never
    # existed.) Layer 4 at the bottom of this file pins both modes; if that
    # rule is ever narrowed, every entry below stops being safe.
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
    # gateway-global copy is only reachable by the admin identity) — with
    # user accounts on; with them off only an admin can be signed in at all
    # (Layer 4).
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
    # Desktop hand-over (2026-09-25): direct-loopback-only, one-time code the
    # gateway wrote into a 0600 file; see test_gateway_desktop_handover.py.
    ("POST", "/api/gateway/apps/desktop-handover"),
    # Not a write: the one public READ inside the boundary (versions only,
    # no paths or secrets; see test_gateway_about.py).
    ("GET", "/api/gateway/about"),
}


def test_the_public_write_exemption_is_exactly_session_login() -> None:
    """Inside the boundary, exactly the listed routes skip authentication: the
    login that mints credentials, the first-run claim and the desktop
    hand-over that redeem one-time local codes, and the public version read
    (GET /about). Widening `_public_auth_path` widens the unauthenticated
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
            "only the PUBLIC_WRITES routes may be public inside the boundary"
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


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, user_accounts: bool = True) -> TestClient:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "admin-token")
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1" if user_accounts else "0")

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


# ---------------------------------------------------------------------------
# Layer 4: a NON-admin session never reaches the operator's gateway-wide
# state, in EITHER mode (mission BB, 2026-09-24).
#
# User accounts OFF: one shared service, so the non-admin must not be signed
# in at all — every write route answers 401 to a session that already exists
# (minted before the fix), and login refuses. User accounts ON: the admin
# families answer 403, the user-level ones act on the caller's OWN runtime,
# and the admin's gateway-wide view is byte-identical afterwards.
# ---------------------------------------------------------------------------

#: Route families whose writes reach gateway-wide state (config, admin,
#: the workflow registry). Enumerated from the LIVE table, never by hand.
GATEWAY_WIDE_PREFIXES = ("/api/gateway/config/", "/api/gateway/admin/", "/api/gateway/bundles")

#: The routes the brief names; the enumeration must keep finding each one,
#: so a rename can never silently empty the families above.
GATEWAY_WIDE_SENTINELS = {
    ("PUT", "/api/gateway/config/capability-defaults/{kind}/{modality}"),
    ("POST", "/api/gateway/config/provider-endpoint-profiles"),
    ("PUT", "/api/gateway/config/provider-endpoint-profiles/{profile_id}"),
    ("POST", "/api/gateway/admin/runtime-config"),
    ("POST", "/api/gateway/bundles/reload"),
    ("POST", "/api/gateway/bundles/{bundle_id}/deprecate"),
    ("POST", "/api/gateway/bundles/{bundle_id}/undeprecate"),
    ("POST", "/api/gateway/admin/users"),
    ("PATCH", "/api/gateway/admin/users/{user_id}"),
    ("DELETE", "/api/gateway/admin/users/{user_id}"),
}

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _gateway_wide_writes() -> list[tuple[str, str]]:
    rows = sorted(
        (method, path)
        for method, path in _live_route_table()
        if method in _WRITE_METHODS and path.startswith(GATEWAY_WIDE_PREFIXES)
    )
    missing = GATEWAY_WIDE_SENTINELS - set(rows)
    assert not missing, f"the gateway-wide enumeration lost named routes: {sorted(missing)}"
    return rows


def _mode_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, user_accounts: bool) -> TestClient:
    # Refused sessions count as presented-invalid credentials; keep the
    # lockout out of the way so every refusal reads as itself.
    monkeypatch.setenv("ABSTRACTGATEWAY_LOCKOUT_AFTER", "100000")
    return _client(tmp_path, monkeypatch, user_accounts=user_accounts)


def _admin_gateway_view(client: TestClient) -> dict:
    admin = {"Authorization": "Bearer admin-token"}
    caps = client.get("/api/gateway/config/capability-defaults", headers=admin)
    profiles = client.get("/api/gateway/config/provider-endpoint-profiles", headers=admin)
    users = client.get("/api/gateway/admin/users", headers=admin)
    runtime_config = client.get("/api/gateway/admin/runtime-config", headers=admin)
    bundles = client.get("/api/gateway/bundles?include_deprecated=true", headers=admin)
    for res in (caps, profiles, users, runtime_config, bundles):
        assert res.status_code == 200, res.text
    return {
        "capability_routes": caps.json().get("routes"),
        "profiles": sorted(str(p.get("id")) for p in profiles.json().get("profiles") or []),
        "users": sorted((u["user_id"], tuple(u["roles"]), u["enabled"]) for u in users.json().get("users") or []),
        "runtime_config": runtime_config.json(),
        "bundles": bundles.json().get("items"),
    }


def test_user_accounts_off_a_non_admin_session_reaches_no_write_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User accounts OFF: login refuses the non-admin, and a session that
    already exists is refused on EVERY write route of the live table (the
    gateway-wide families included) — authentication fails before any
    handler, so nothing downstream can be reached."""
    import abstractgateway.security.sessions as sessions
    from abstractgateway.security.sessions import (
        gateway_csrf_cookie_name,
        gateway_csrf_header_name,
        gateway_session_cookie_name,
    )
    from abstractgateway.users import GatewayUserRegistry

    wide = _gateway_wide_writes()
    with _mode_client(tmp_path, monkeypatch, user_accounts=False) as client:
        _rec, token = GatewayUserRegistry().create_user(user_id="mallory", roles=["user"])
        before = _admin_gateway_view(client)
        login = client.post("/api/gateway/session/login", json={"user_id": "mallory", "token": token})
        assert login.status_code == 401, login.text
        assert login.json()["detail"]["reason_code"] == "user_accounts_off_admin_only"

        answers: dict[str, int] = {}
        for method, path in sorted(_live_route_table()):
            if not path.startswith("/api/gateway") or method not in _WRITE_METHODS or (method, path) in PUBLIC_WRITES:
                continue
            # A fresh pre-fix session per request: the first refusal drops the
            # record, and every route must be refused on its own merits.
            principal = GatewayUserRegistry().get_user("mallory").to_principal()
            with monkeypatch.context() as m:
                m.setattr(sessions, "principal_barred_from_shared_runtime", lambda *_a, **_k: False)
                cookie, csrf, _r = sessions.GatewaySessionStore().create_session(principal)
            client.cookies.set(gateway_session_cookie_name(), cookie)
            client.cookies.set(gateway_csrf_cookie_name(), csrf)
            res = client.request(method, _concrete(path), headers={gateway_csrf_header_name(): csrf}, json={})
            answers[f"{method} {path}"] = res.status_code
        client.cookies.clear()
        refused_wide = {k: v for k, v in answers.items() if tuple(k.split(" ", 1)) in set(wide)}
        assert len(refused_wide) == len(wide), "every gateway-wide write must have been exercised"
        not_refused = {k: v for k, v in answers.items() if v != 401}
        assert not not_refused, f"a non-admin session got past authentication with user accounts off: {not_refused}"
        after = _admin_gateway_view(client)
    assert after == before


def test_user_accounts_on_a_non_admin_session_never_writes_gateway_wide_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User accounts ON: through a real browser session, every gateway-wide
    write that has a policy row answers 403; every user-level one passes
    authorization (its own runtime); and the admin's gateway-wide view is
    unchanged afterwards."""
    from abstractgateway.security.authorization import gateway_route_authorization_requirement
    from abstractgateway.security.sessions import gateway_csrf_cookie_name, gateway_csrf_header_name
    from abstractgateway.users import GatewayUserRegistry

    wide = _gateway_wide_writes()
    with _mode_client(tmp_path, monkeypatch, user_accounts=True) as client:
        _rec, token = GatewayUserRegistry().create_user(user_id="mallory", roles=["user"])
        before = _admin_gateway_view(client)
        login = client.post("/api/gateway/session/login", json={"user_id": "mallory", "token": token})
        assert login.status_code == 200, login.text
        csrf = {gateway_csrf_header_name(): client.cookies.get(gateway_csrf_cookie_name()) or ""}
        for method, path in wide:
            concrete = _concrete(path)
            res = client.request(method, concrete, headers=csrf, json={})
            if gateway_route_authorization_requirement(concrete, method) is not None:
                assert res.status_code == 403, f"{method} {path}: {res.status_code} {res.text}"
            else:
                assert res.status_code not in (401, 403), f"{method} {path}: {res.status_code} {res.text}"
        # The substantive writes the brief names, with real bodies.
        own = client.put(
            "/api/gateway/config/capability-defaults/output/text",
            headers=csrf,
            json={"provider": "openai", "model": "gpt-mallory"},
        )
        assert own.status_code == 200, own.text
        prof = client.post(
            "/api/gateway/config/provider-endpoint-profiles",
            headers=csrf,
            json={"id": "malprof", "display_name": "M", "provider_family": "openai-compatible", "base_url": "http://127.0.0.1:9/v1"},
        )
        assert prof.status_code == 200, prof.text
        after = _admin_gateway_view(client)
    assert after == before, "a non-admin session changed the operator's gateway-wide state"
