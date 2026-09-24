"""Sign-in when user accounts are OFF (mission BB, 2026-09-24).

THE HOLE (found by mission AA, reproduced by `untracked/missionAA/probe_tokenonly.py 0`):
with user accounts off the gateway runs ONE service, the operator's
(`service.get_gateway_service`). `/session/login` nevertheless authenticated
ANY user-registry token, and the session check never looked at the mode. A
non-admin `bob` (role `user`, created by the admin) signed in and then set
the gateway-wide `output/text` default and created an endpoint profile in the
admin's own list. His bearer token was refused (401); only the session lane
was open.

THE RULE (security/sessions.py `principal_barred_from_shared_runtime`): with
user accounts off only ADMIN registry identities may hold a session. It is
enforced at three points, each pinned here: the login route (401 with a plain
message), `GatewaySessionStore.create_session` (every other session minter),
and `GatewaySessionStore.authenticate_session` (a session minted before the
fix, or while user accounts were on). Admin registry identities keep working
in both modes. The admin users lane refuses to mint a non-admin account the
gateway could never let sign in, and refuses to remove the LAST admin.

Every HTTP test drives the real gateway router behind the real middleware.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ADMIN_TOKEN = "admin-token-bb"
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, user_accounts: bool) -> FastAPI:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", ADMIN_TOKEN)
    monkeypatch.delenv("ABSTRACTGATEWAY_MULTI_USER", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_AUTH_MODE", raising=False)
    monkeypatch.delenv("ABSTRACTFLOW_GATEWAY_USER_AUTH", raising=False)
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1" if user_accounts else "0")
    # A refused session is a presented-and-invalid credential: the lockout
    # counts it. Keep it out of the way so every refusal below reads 401.
    monkeypatch.setenv("ABSTRACTGATEWAY_LOCKOUT_AFTER", "1000")

    from abstractgateway.routes import entities_router, entity_replay_router, gateway_router, triage_router
    from abstractgateway.security import GatewaySecurityMiddleware, load_gateway_auth_policy_from_env

    app = FastAPI()
    app.add_middleware(GatewaySecurityMiddleware, policy=load_gateway_auth_policy_from_env())
    for router in (entities_router, entity_replay_router, gateway_router, triage_router):
        app.include_router(router, prefix="/api")
    return app


def _registry_user(user_id: str, roles: list[str]) -> str:
    """A registry account as it exists on disk — e.g. created while user
    accounts were on, before the operator turned them off."""
    from abstractgateway.users import GatewayUserRegistry

    _rec, token = GatewayUserRegistry().create_user(user_id=user_id, roles=roles)
    return str(token)


def _mint_pre_fix_session(monkeypatch: pytest.MonkeyPatch, user_id: str) -> tuple[str, str]:
    """A session minted by the code BEFORE this fix (the minting-side rule
    switched off for the one call), i.e. a cookie already in a browser."""
    import abstractgateway.security.sessions as sessions
    from abstractgateway.users import GatewayUserRegistry

    principal = GatewayUserRegistry().get_user(user_id).to_principal()
    with monkeypatch.context() as m:
        m.setattr(sessions, "principal_barred_from_shared_runtime", lambda *_a, **_k: False)
        cookie, csrf, _record = sessions.GatewaySessionStore().create_session(principal)
    return cookie, csrf


def _use_session(client: TestClient, cookie: str, csrf: str) -> dict[str, str]:
    from abstractgateway.security.sessions import (
        gateway_csrf_cookie_name,
        gateway_csrf_header_name,
        gateway_session_cookie_name,
    )

    client.cookies.set(gateway_session_cookie_name(), cookie)
    client.cookies.set(gateway_csrf_cookie_name(), csrf)
    return {gateway_csrf_header_name(): csrf}


def _login(client: TestClient, user_id: str, token: str) -> Any:
    return client.post("/api/gateway/session/login", json={"user_id": user_id, "token": token})


def _csrf_after_login(client: TestClient) -> dict[str, str]:
    from abstractgateway.security.sessions import gateway_csrf_cookie_name, gateway_csrf_header_name

    csrf = client.cookies.get(gateway_csrf_cookie_name()) or ""
    assert csrf, "login issued no CSRF cookie"
    return {gateway_csrf_header_name(): csrf}


def _session_rows() -> list[dict[str, Any]]:
    from abstractgateway.security.sessions import gateway_session_store_path_from_env

    path = gateway_session_store_path_from_env()
    if not path.exists():
        return []
    return list(json.loads(path.read_text(encoding="utf-8")).get("sessions") or [])


# ---------------------------------------------------------------------------
# The rule itself (unit)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "roles", "user_accounts", "barred"),
    [
        ("user-registry", ("user",), False, True),
        ("user-registry", ("readonly",), False, True),
        ("user-registry", ("admin", "user"), False, False),
        ("user-registry", ("user",), True, False),
        ("user-registry", ("admin",), True, False),
        ("legacy-token", ("admin",), False, False),
    ],
)
def test_the_rule_bars_only_non_admin_registry_identities_with_user_accounts_off(source, roles, user_accounts, barred):
    from abstractgateway.security.principal import GatewayPrincipal
    from abstractgateway.security.sessions import principal_barred_from_shared_runtime

    principal = GatewayPrincipal(user_id="p", roles=roles, source=source)
    assert principal_barred_from_shared_runtime(principal, user_auth_enabled=user_accounts) is barred


def test_the_rule_reads_the_same_mode_the_service_routing_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.security.principal import GatewayPrincipal
    from abstractgateway.security.sessions import principal_barred_from_shared_runtime
    from abstractgateway.service import gateway_multi_user_enabled

    bob = GatewayPrincipal(user_id="bob", roles=("user",), source="user-registry")
    for raw in ("0", "1"):
        monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", raw)
        assert principal_barred_from_shared_runtime(bob) is (not gateway_multi_user_enabled())


def test_create_session_refuses_a_non_admin_with_user_accounts_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The store is the chokepoint every minter (login, app handover) shares."""
    _app(tmp_path, monkeypatch, user_accounts=False)
    from abstractgateway.security.sessions import GatewaySessionStore, SessionRefusedError
    from abstractgateway.users import GatewayUserRegistry

    _registry_user("bob", ["user"])
    _registry_user("root", ["admin", "user"])
    with pytest.raises(SessionRefusedError, match="user accounts off"):
        GatewaySessionStore().create_session(GatewayUserRegistry().get_user("bob").to_principal())
    assert _session_rows() == []
    GatewaySessionStore().create_session(GatewayUserRegistry().get_user("root").to_principal())
    assert [r["user_id"] for r in _session_rows()] == ["root"]


# ---------------------------------------------------------------------------
# Login, both modes
# ---------------------------------------------------------------------------


def test_login_refuses_a_non_admin_with_user_accounts_off_and_says_why(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(tmp_path, monkeypatch, user_accounts=False)
    token = _registry_user("bob", ["user"])
    with TestClient(app) as client:
        res = _login(client, "bob", token)
        assert res.status_code == 401, res.text
        detail = res.json()["detail"]
        assert detail["reason_code"] == "user_accounts_off_admin_only"
        message = detail["message"]
        assert "user accounts off" in message
        assert "'bob'" in message
        assert "turns user accounts on" in message and "admin account" in message
        # Describes state, never instructs an env var.
        assert "ABSTRACTGATEWAY" not in message and "=" not in message
        assert "set-cookie" not in {k.lower() for k in res.headers.keys()}
        assert client.get("/api/gateway/me").status_code == 401
    assert _session_rows() == []


def test_login_accepts_a_non_admin_with_user_accounts_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(tmp_path, monkeypatch, user_accounts=True)
    token = _registry_user("bob", ["user"])
    with TestClient(app) as client:
        res = _login(client, "bob", token)
        assert res.status_code == 200, res.text
        assert res.json()["routing"]["mode"] == "per-principal"
        assert res.json()["auth"]["user_auth_enabled"] is True
        me = client.get("/api/gateway/me")
        assert me.status_code == 200 and me.json()["principal"]["user_id"] == "bob"


@pytest.mark.parametrize("user_accounts", [False, True])
def test_an_admin_registry_account_signs_in_in_both_modes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user_accounts: bool) -> None:
    app = _app(tmp_path, monkeypatch, user_accounts=user_accounts)
    token = _registry_user("root", ["admin", "user"])
    with TestClient(app) as client:
        res = _login(client, "root", token)
        assert res.status_code == 200, res.text
        # The login answer reports the REAL mode (it used to claim "users").
        assert res.json()["auth"]["user_auth_enabled"] is user_accounts
        csrf = _csrf_after_login(client)
        me = client.get("/api/gateway/me")
        assert me.status_code == 200 and me.json()["principal"]["admin"] is True
        # And can administer: a gateway-wide admin write through the session.
        created = client.post("/api/gateway/admin/users", headers=csrf, json={"user_id": "second", "roles": ["admin"]})
        assert created.status_code == 200, created.text


# ---------------------------------------------------------------------------
# The session check: sessions that already exist
# ---------------------------------------------------------------------------


def test_a_session_minted_before_the_fix_is_refused_and_dropped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(tmp_path, monkeypatch, user_accounts=False)
    _registry_user("bob", ["user"])
    cookie, csrf = _mint_pre_fix_session(monkeypatch, "bob")
    assert [r["user_id"] for r in _session_rows()] == ["bob"], "precondition: a live pre-fix session"
    with TestClient(app) as client:
        headers = _use_session(client, cookie, csrf)
        assert client.get("/api/gateway/me").status_code == 401
        assert client.put(
            "/api/gateway/config/capability-defaults/output/text",
            headers=headers,
            json={"provider": "openai", "model": "gpt-bob"},
        ).status_code == 401
    assert _session_rows() == [], "a refused session must not linger in the store"


def test_a_session_minted_with_user_accounts_on_dies_when_they_are_turned_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(tmp_path, monkeypatch, user_accounts=True)
    token = _registry_user("bob", ["user"])
    with TestClient(app) as client:
        assert _login(client, "bob", token).status_code == 200
        assert client.get("/api/gateway/me").status_code == 200
        monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "0")
        assert client.get("/api/gateway/me").status_code == 401


def test_an_admin_session_survives_the_mode_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(tmp_path, monkeypatch, user_accounts=True)
    token = _registry_user("root", ["admin", "user"])
    with TestClient(app) as client:
        assert _login(client, "root", token).status_code == 200
        monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "0")
        assert client.get("/api/gateway/me").status_code == 200


# ---------------------------------------------------------------------------
# Regression harness (AA's probe as a pytest)
# ---------------------------------------------------------------------------

#: bob's writes, each aimed at the operator's gateway-wide state. Bodies are
#: the ones the probe used; with the session refused none reaches a handler.
_BOB_WRITES: list[tuple[str, str, dict[str, Any]]] = [
    ("PUT", "/api/gateway/config/capability-defaults/output/text", {"provider": "openai", "model": "gpt-bob"}),
    ("DELETE", "/api/gateway/config/capability-defaults/input/text", {}),
    ("PUT", "/api/gateway/config/capability-defaults/output/text/text_generation", {"provider": "openai", "model": "gpt-bob"}),
    (
        "POST",
        "/api/gateway/config/provider-endpoint-profiles",
        {"id": "bobprof", "display_name": "Bob", "provider_family": "openai-compatible", "base_url": "http://127.0.0.1:9/v1"},
    ),
    ("PUT", "/api/gateway/config/provider-endpoint-profiles/bobprof", {"display_name": "Bob"}),
    ("DELETE", "/api/gateway/config/provider-endpoint-profiles/bobprof", {}),
    ("POST", "/api/gateway/config/provider-endpoint-profiles/discover-models", {"base_url": "http://127.0.0.1:9/v1"}),
    ("POST", "/api/gateway/config/capability-defaults/apply-recommended", {}),
    ("POST", "/api/gateway/admin/runtime-config", {}),
    ("POST", "/api/gateway/bundles/reload", {}),
    ("POST", "/api/gateway/bundles/nonexistent/deprecate", {}),
    ("POST", "/api/gateway/bundles/nonexistent/undeprecate", {}),
    ("POST", "/api/gateway/admin/users", {"user_id": "bob2", "roles": ["admin"]}),
    ("PATCH", "/api/gateway/admin/users/bob", {"roles": ["admin"]}),
]


def _admin_view(client: TestClient) -> dict[str, Any]:
    caps = client.get("/api/gateway/config/capability-defaults", headers=ADMIN)
    profiles = client.get("/api/gateway/config/provider-endpoint-profiles", headers=ADMIN)
    users = client.get("/api/gateway/admin/users", headers=ADMIN)
    assert caps.status_code == profiles.status_code == users.status_code == 200
    return {
        "routes": caps.json().get("routes"),
        "profiles": sorted(str(p.get("id")) for p in profiles.json().get("profiles") or []),
        "users": sorted((u["user_id"], tuple(u["roles"])) for u in users.json().get("users") or []),
    }


@pytest.mark.parametrize("session_kind", ["fresh-login", "pre-fix-cookie"])
def test_regression_bob_cannot_touch_the_operators_config_with_user_accounts_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session_kind: str
) -> None:
    app = _app(tmp_path, monkeypatch, user_accounts=False)
    token = _registry_user("bob", ["user"])
    with TestClient(app) as client:
        before = _admin_view(client)
        if session_kind == "fresh-login":
            res = _login(client, "bob", token)
            assert res.status_code == 401, res.text
            # Whatever the browser still holds, it is no session.
            from abstractgateway.security.sessions import gateway_csrf_header_name

            headers = {gateway_csrf_header_name(): "agcsrf_none"}
        else:
            headers = _use_session(client, *_mint_pre_fix_session(monkeypatch, "bob"))
        answers = {}
        for method, path, body in _BOB_WRITES:
            answers[f"{method} {path}"] = client.request(method, path, headers=headers, json=body).status_code
        assert set(answers.values()) == {401}, answers
        # His bearer stays refused too (it always was).
        assert client.get("/api/gateway/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
        after = _admin_view(client)
    assert after == before, "bob changed the operator's gateway-wide state"


def test_regression_bob_writes_his_own_runtime_with_user_accounts_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mode ON: bob signs in and his config writes land in HIS runtime; the
    admin's view is unchanged, and the admin-only routes answer 403."""
    app = _app(tmp_path, monkeypatch, user_accounts=True)
    token = _registry_user("bob", ["user"])
    with TestClient(app) as client:
        before = _admin_view(client)
        assert _login(client, "bob", token).status_code == 200
        csrf = _csrf_after_login(client)
        own = client.put(
            "/api/gateway/config/capability-defaults/output/text",
            headers=csrf,
            json={"provider": "openai", "model": "gpt-bob"},
        )
        assert own.status_code == 200, own.text
        assert own.json()["authority"] == "abstractcore.runtime", own.text
        prof = client.post(
            "/api/gateway/config/provider-endpoint-profiles",
            headers=csrf,
            json={"id": "bobprof", "display_name": "Bob", "provider_family": "openai-compatible", "base_url": "http://127.0.0.1:9/v1"},
        )
        assert prof.status_code == 200, prof.text
        for method, path, body in [
            ("POST", "/api/gateway/admin/runtime-config", {}),
            ("POST", "/api/gateway/admin/users", {"user_id": "bob2", "roles": ["admin"]}),
            ("PATCH", "/api/gateway/admin/users/bob", {"roles": ["admin"]}),
            ("POST", "/api/gateway/config/capability-defaults/apply-recommended", {}),
        ]:
            assert client.request(method, path, headers=csrf, json=body).status_code == 403, (method, path)
        after = _admin_view(client)
    assert after == before, "bob's own-runtime writes leaked into the operator's gateway-wide state"


# ---------------------------------------------------------------------------
# The admin users lane
# ---------------------------------------------------------------------------


def test_creating_a_non_admin_with_user_accounts_off_is_refused_in_plain_words(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(tmp_path, monkeypatch, user_accounts=False)
    with TestClient(app) as client:
        for roles in (["user"], ["readonly"]):
            res = client.post("/api/gateway/admin/users", headers=ADMIN, json={"user_id": "bob", "roles": roles})
            assert res.status_code == 409, res.text
            detail = res.json()["detail"]
            assert detail["reason_code"] == "user_accounts_off_admin_only"
            assert "user accounts off" in detail["message"] and "turns user accounts on" in detail["message"]
            assert "ABSTRACTGATEWAY" not in detail["message"]
        # The role defaults to "user" when omitted: same refusal.
        assert client.post("/api/gateway/admin/users", headers=ADMIN, json={"user_id": "bob"}).status_code == 409
        users = client.get("/api/gateway/admin/users", headers=ADMIN).json()["users"]
        assert [u["user_id"] for u in users] == []
        ok = client.post("/api/gateway/admin/users", headers=ADMIN, json={"user_id": "root", "roles": ["admin", "user"]})
        assert ok.status_code == 200, ok.text


def test_creating_a_non_admin_with_user_accounts_on_still_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(tmp_path, monkeypatch, user_accounts=True)
    with TestClient(app) as client:
        res = client.post("/api/gateway/admin/users", headers=ADMIN, json={"user_id": "bob", "roles": ["user"]})
        assert res.status_code == 200, res.text


def test_demoting_an_admin_with_user_accounts_off_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(tmp_path, monkeypatch, user_accounts=False)
    _registry_user("root", ["admin", "user"])
    _registry_user("ops", ["admin"])
    with TestClient(app) as client:
        res = client.patch("/api/gateway/admin/users/ops", headers=ADMIN, json={"roles": ["user"]})
        assert res.status_code == 409, res.text
        assert res.json()["detail"]["reason_code"] == "user_accounts_off_admin_only"


def test_the_last_admin_cannot_be_deleted_disabled_or_demoted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(tmp_path, monkeypatch, user_accounts=True)
    _registry_user("root", ["admin", "user"])
    _registry_user("bob", ["user"])
    with TestClient(app) as client:
        for method, path, body, action in [
            ("DELETE", "/api/gateway/admin/users/root", None, "deleting"),
            ("PATCH", "/api/gateway/admin/users/root", {"enabled": False}, "disabling"),
            ("PATCH", "/api/gateway/admin/users/root", {"roles": ["user"]}, "demoting"),
        ]:
            res = client.request(method, path, headers=ADMIN, json=body)
            assert res.status_code == 409, (method, body, res.text)
            detail = res.json()["detail"]
            assert detail["reason_code"] == "last_admin"
            assert "'root' is the last enabled admin account" in detail["message"]
            assert action in detail["message"]
        root = client.get("/api/gateway/admin/users/root", headers=ADMIN).json()["user"]
        assert root["enabled"] is True and "admin" in root["roles"]
        # Non-admin accounts are never protected by this guard.
        assert client.delete("/api/gateway/admin/users/bob", headers=ADMIN).status_code == 200


def test_with_a_second_admin_the_first_can_go(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(tmp_path, monkeypatch, user_accounts=True)
    _registry_user("root", ["admin", "user"])
    _registry_user("ops", ["admin"])
    with TestClient(app) as client:
        assert client.patch("/api/gateway/admin/users/root", headers=ADMIN, json={"roles": ["user"]}).status_code == 200
        # ops is now the last one.
        assert client.delete("/api/gateway/admin/users/ops", headers=ADMIN).status_code == 409


def test_a_disabled_admin_does_not_count_as_the_remaining_admin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(tmp_path, monkeypatch, user_accounts=True)
    _registry_user("root", ["admin", "user"])
    from abstractgateway.users import GatewayUserRegistry

    GatewayUserRegistry().create_user(user_id="ghost", roles=["admin"], enabled=False)
    with TestClient(app) as client:
        assert client.delete("/api/gateway/admin/users/root", headers=ADMIN).status_code == 409
