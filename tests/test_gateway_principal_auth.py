from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_gateway_user_registry_direct_import_is_not_order_sensitive() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "from abstractgateway.users import GatewayUserRegistry; print(GatewayUserRegistry.__name__)"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "GatewayUserRegistry" in result.stdout


def _client(tmp_path: Path, monkeypatch, *, user_auth: bool = True) -> TestClient:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "admin-token")
    if user_auth:
        monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    else:
        monkeypatch.delenv("ABSTRACTGATEWAY_USER_AUTH", raising=False)
        monkeypatch.delenv("ABSTRACTGATEWAY_MULTI_USER", raising=False)
        monkeypatch.delenv("ABSTRACTGATEWAY_AUTH_MODE", raising=False)

    from abstractgateway.routes import gateway_router
    from abstractgateway.security import GatewaySecurityMiddleware, load_gateway_auth_policy_from_env

    app = FastAPI()
    app.add_middleware(GatewaySecurityMiddleware, policy=load_gateway_auth_policy_from_env())
    app.include_router(gateway_router, prefix="/api")
    return TestClient(app)


def _set_cookie_headers(response) -> list[str]:
    get_list = getattr(response.headers, "get_list", None)
    if callable(get_list):
        return list(get_list("set-cookie"))
    raw = response.headers.get("set-cookie", "")
    return [str(raw)] if raw else []


def _set_cookie(response, name: str) -> str:
    prefix = f"{name}="
    for header in _set_cookie_headers(response):
        if str(header).startswith(prefix):
            return str(header)
    raise AssertionError(f"missing Set-Cookie header for {name}")


def test_legacy_gateway_token_resolves_local_admin(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch, user_auth=False)
    response = client.get("/api/gateway/me", headers={"Authorization": "Bearer admin-token"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["principal"]["user_id"] == "local-admin"
    assert payload["principal"]["admin"] is True
    assert payload["auth"]["mode"] == "legacy-token"
    assert payload["routing"]["mode"] == "single-user"


def test_admin_routes_fail_closed_without_security_middleware(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_SECURITY", "1")

    from abstractgateway.routes import gateway_router

    app = FastAPI()
    app.include_router(gateway_router, prefix="/api")

    with TestClient(app) as client:
        response = client.get("/api/gateway/admin/users")

    assert response.status_code == 401
    assert response.json()["detail"] == "Gateway principal unavailable"


def test_admin_user_crud_and_user_principal_auth(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    admin_headers = {"Authorization": "Bearer admin-token"}

    created = client.post(
        "/api/gateway/admin/users",
        headers=admin_headers,
        json={"user_id": "alice", "tenant_id": "team-a", "email": "alice@example.com", "roles": ["user"]},
    )
    assert created.status_code == 200
    created_payload = created.json()
    alice_token = created_payload["token"]
    assert alice_token.startswith("agw_")
    assert created_payload["user"]["user_id"] == "alice"
    assert created_payload["user"]["email"] == "alice@example.com"
    assert "token_hash" not in created_payload["user"]

    users = client.get("/api/gateway/admin/users", headers=admin_headers)
    assert users.status_code == 200
    listed = users.json()["users"]
    assert [u["user_id"] for u in listed] == ["alice"]
    assert listed[0]["email"] == "alice@example.com"
    assert "token_hash" not in listed[0]

    alice_me = client.get("/api/gateway/me", headers={"Authorization": f"Bearer {alice_token}"})
    assert alice_me.status_code == 200
    alice_principal = alice_me.json()["principal"]
    assert alice_principal["user_id"] == "alice"
    assert alice_principal["tenant_id"] == "team-a"
    assert alice_principal["admin"] is False
    assert alice_me.json()["routing"]["mode"] == "per-principal"

    forbidden = client.get("/api/gateway/admin/users", headers={"Authorization": f"Bearer {alice_token}"})
    assert forbidden.status_code == 403

    disabled = client.patch(
        "/api/gateway/admin/users/alice?tenant_id=team-a",
        headers=admin_headers,
        json={"enabled": False, "email": "alice.ops@example.com"},
    )
    assert disabled.status_code == 200
    assert disabled.json()["user"]["enabled"] is False
    assert disabled.json()["user"]["email"] == "alice.ops@example.com"

    alice_after_disable = client.get("/api/gateway/me", headers={"Authorization": f"Bearer {alice_token}"})
    assert alice_after_disable.status_code == 401

    deleted = client.delete("/api/gateway/admin/users/alice?tenant_id=team-a", headers=admin_headers)
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True

    missing = client.get("/api/gateway/admin/users/alice?tenant_id=team-a", headers=admin_headers)
    assert missing.status_code == 404


def test_registry_admin_user_can_manage_users(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    bootstrap_headers = {"Authorization": "Bearer admin-token"}

    admin_created = client.post(
        "/api/gateway/admin/users",
        headers=bootstrap_headers,
        json={"user_id": "admin", "tenant_id": "default", "roles": ["admin", "user"]},
    )
    assert admin_created.status_code == 200
    admin_user_token = admin_created.json()["token"]

    admin_me = client.get("/api/gateway/me", headers={"Authorization": f"Bearer {admin_user_token}"})
    assert admin_me.status_code == 200
    assert admin_me.json()["principal"]["user_id"] == "admin"
    assert admin_me.json()["principal"]["source"] == "user-registry"
    assert admin_me.json()["principal"]["admin"] is True

    registry_admin_headers = {"Authorization": f"Bearer {admin_user_token}"}
    bob_created = client.post(
        "/api/gateway/admin/users",
        headers=registry_admin_headers,
        json={"user_id": "bob", "tenant_id": "default", "roles": ["user"]},
    )
    assert bob_created.status_code == 200
    assert bob_created.json()["user"]["user_id"] == "bob"
    assert "token_hash" not in bob_created.json()["user"]

    users = client.get("/api/gateway/admin/users", headers=registry_admin_headers)
    assert users.status_code == 200
    assert [u["user_id"] for u in users.json()["users"]] == ["admin", "bob"]

    bob_disabled = client.patch(
        "/api/gateway/admin/users/bob?tenant_id=default",
        headers=registry_admin_headers,
        json={"enabled": False},
    )
    assert bob_disabled.status_code == 200
    assert bob_disabled.json()["user"]["enabled"] is False

    deleted = client.delete("/api/gateway/admin/users/bob?tenant_id=default", headers=registry_admin_headers)
    assert deleted.status_code == 200
    assert deleted.json()["deleted"] is True


def test_gateway_user_runtime_ids_are_unique_per_tenant(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    admin_headers = {"Authorization": "Bearer admin-token"}

    alice = client.post(
        "/api/gateway/admin/users",
        headers=admin_headers,
        json={"user_id": "alice", "tenant_id": "default", "roles": ["user"], "runtime_id": "shared-runtime"},
    )
    assert alice.status_code == 200, alice.text

    duplicate_create = client.post(
        "/api/gateway/admin/users",
        headers=admin_headers,
        json={"user_id": "bob", "tenant_id": "default", "roles": ["user"], "runtime_id": "shared-runtime"},
    )
    assert duplicate_create.status_code == 400
    assert "runtime already assigned" in duplicate_create.json()["detail"]

    other_tenant = client.post(
        "/api/gateway/admin/users",
        headers=admin_headers,
        json={"user_id": "bob", "tenant_id": "team-b", "roles": ["user"], "runtime_id": "shared-runtime"},
    )
    assert other_tenant.status_code == 200, other_tenant.text

    bob = client.post(
        "/api/gateway/admin/users",
        headers=admin_headers,
        json={"user_id": "bob", "tenant_id": "default", "roles": ["user"], "runtime_id": "bob-runtime"},
    )
    assert bob.status_code == 200, bob.text
    duplicate_update = client.patch(
        "/api/gateway/admin/users/bob?tenant_id=default",
        headers=admin_headers,
        json={"runtime_id": "shared-runtime"},
    )
    assert duplicate_update.status_code == 400
    assert "runtime already assigned" in duplicate_update.json()["detail"]


def test_deleted_gateway_user_runtime_id_stays_reserved(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    admin_headers = {"Authorization": "Bearer admin-token"}

    alice = client.post(
        "/api/gateway/admin/users",
        headers=admin_headers,
        json={"user_id": "alice", "tenant_id": "default", "roles": ["user"], "runtime_id": "shared-runtime"},
    )
    assert alice.status_code == 200, alice.text

    deleted = client.delete("/api/gateway/admin/users/alice?tenant_id=default", headers=admin_headers)
    assert deleted.status_code == 200, deleted.text

    bob_same_runtime = client.post(
        "/api/gateway/admin/users",
        headers=admin_headers,
        json={"user_id": "bob", "tenant_id": "default", "roles": ["user"], "runtime_id": "shared-runtime"},
    )
    assert bob_same_runtime.status_code == 400
    assert "runtime already assigned or retained" in bob_same_runtime.json()["detail"]

    users_file = tmp_path / "runtime" / "auth" / "users.json"
    data = json.loads(users_file.read_text(encoding="utf-8"))
    reservations = data.get("runtime_reservations") or []
    assert reservations == [
        {
            "created_at": reservations[0]["created_at"],
            "owner_key": "default:alice",
            "owner_user_id": "alice",
            "reason": "deleted-user",
            "runtime_id": "shared-runtime",
            "tenant_id": "default",
            "updated_at": reservations[0]["updated_at"],
        }
    ]

    alice_recreated = client.post(
        "/api/gateway/admin/users",
        headers=admin_headers,
        json={"user_id": "alice", "tenant_id": "default", "roles": ["user"], "runtime_id": "shared-runtime"},
    )
    assert alice_recreated.status_code == 200, alice_recreated.text
    data_after_recreate = json.loads(users_file.read_text(encoding="utf-8"))
    assert data_after_recreate.get("runtime_reservations") == []


def test_runtime_reservation_purge_deletes_data_before_runtime_reuse(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    admin_headers = {"Authorization": "Bearer admin-token"}

    alice = client.post(
        "/api/gateway/admin/users",
        headers=admin_headers,
        json={"user_id": "alice", "tenant_id": "default", "roles": ["user"], "runtime_id": "shared-runtime"},
    )
    assert alice.status_code == 200, alice.text
    bob = client.post(
        "/api/gateway/admin/users",
        headers=admin_headers,
        json={"user_id": "bob", "tenant_id": "default", "roles": ["user"], "runtime_id": "bob-runtime"},
    )
    assert bob.status_code == 200, bob.text
    bob_token = bob.json()["token"]

    runtime_root = tmp_path / "runtime" / "users" / "default" / "shared-runtime"
    (runtime_root / "runtime").mkdir(parents=True)
    (runtime_root / "runtime" / "artifact.txt").write_text("alice data", encoding="utf-8")

    deleted = client.delete("/api/gateway/admin/users/alice?tenant_id=default", headers=admin_headers)
    assert deleted.status_code == 200, deleted.text

    forbidden = client.get(
        "/api/gateway/admin/runtime-reservations",
        headers={"Authorization": f"Bearer {bob_token}"},
    )
    assert forbidden.status_code == 403

    reservations = client.get("/api/gateway/admin/runtime-reservations", headers=admin_headers)
    assert reservations.status_code == 200, reservations.text
    listed = reservations.json()["runtime_reservations"]
    assert [(r["tenant_id"], r["runtime_id"], r["data_exists"]) for r in listed] == [
        ("default", "shared-runtime", True)
    ]

    bad_confirm = client.post(
        "/api/gateway/admin/runtime-reservations/shared-runtime/purge",
        headers=admin_headers,
        json={"tenant_id": "default", "confirm_runtime_id": "other-runtime", "delete_data": True},
    )
    assert bad_confirm.status_code == 400

    forbidden_purge = client.post(
        "/api/gateway/admin/runtime-reservations/shared-runtime/purge",
        headers={"Authorization": f"Bearer {bob_token}"},
        json={"tenant_id": "default", "confirm_runtime_id": "shared-runtime", "delete_data": True},
    )
    assert forbidden_purge.status_code == 403

    purged = client.post(
        "/api/gateway/admin/runtime-reservations/shared-runtime/purge",
        headers=admin_headers,
        json={"tenant_id": "default", "confirm_runtime_id": "shared-runtime", "delete_data": True},
    )
    assert purged.status_code == 200, purged.text
    assert purged.json()["purged"] is True
    assert purged.json()["deleted_data"] is True
    assert not runtime_root.exists()

    charlie = client.post(
        "/api/gateway/admin/users",
        headers=admin_headers,
        json={"user_id": "charlie", "tenant_id": "default", "roles": ["user"], "runtime_id": "shared-runtime"},
    )
    assert charlie.status_code == 200, charlie.text


def test_runtime_reservation_transfer_assigns_retained_runtime_to_existing_user(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    admin_headers = {"Authorization": "Bearer admin-token"}

    alice = client.post(
        "/api/gateway/admin/users",
        headers=admin_headers,
        json={"user_id": "alice", "tenant_id": "default", "roles": ["user"], "runtime_id": "shared-runtime"},
    )
    assert alice.status_code == 200, alice.text
    bob = client.post(
        "/api/gateway/admin/users",
        headers=admin_headers,
        json={"user_id": "bob", "tenant_id": "default", "roles": ["user"], "runtime_id": "bob-runtime"},
    )
    assert bob.status_code == 200, bob.text
    bob_token = bob.json()["token"]

    deleted = client.delete("/api/gateway/admin/users/alice?tenant_id=default", headers=admin_headers)
    assert deleted.status_code == 200, deleted.text

    forbidden_transfer = client.post(
        "/api/gateway/admin/runtime-reservations/shared-runtime/transfer",
        headers={"Authorization": f"Bearer {bob_token}"},
        json={"tenant_id": "default", "target_user_id": "bob", "confirm_runtime_id": "shared-runtime"},
    )
    assert forbidden_transfer.status_code == 403

    transferred = client.post(
        "/api/gateway/admin/runtime-reservations/shared-runtime/transfer",
        headers=admin_headers,
        json={"tenant_id": "default", "target_user_id": "bob", "confirm_runtime_id": "shared-runtime"},
    )
    assert transferred.status_code == 200, transferred.text
    assert transferred.json()["user"]["runtime_id"] == "shared-runtime"
    assert transferred.json()["previous_runtime_id"] == "bob-runtime"

    bob_me = client.get("/api/gateway/me", headers={"Authorization": f"Bearer {bob_token}"})
    assert bob_me.status_code == 200, bob_me.text
    assert bob_me.json()["principal"]["runtime_id"] == "shared-runtime"

    reservations = client.get("/api/gateway/admin/runtime-reservations", headers=admin_headers)
    assert reservations.status_code == 200, reservations.text
    listed = reservations.json()["runtime_reservations"]
    assert [(r["runtime_id"], r["owner_user_id"], r["reason"]) for r in listed] == [
        ("bob-runtime", "bob", "runtime-transferred")
    ]

    alice_reuse_live_runtime = client.post(
        "/api/gateway/admin/users",
        headers=admin_headers,
        json={"user_id": "alice", "tenant_id": "default", "roles": ["user"], "runtime_id": "shared-runtime"},
    )
    assert alice_reuse_live_runtime.status_code == 400
    assert "runtime already assigned" in alice_reuse_live_runtime.json()["detail"]

    charlie_reuse_previous_runtime = client.post(
        "/api/gateway/admin/users",
        headers=admin_headers,
        json={"user_id": "charlie", "tenant_id": "default", "roles": ["user"], "runtime_id": "bob-runtime"},
    )
    assert charlie_reuse_previous_runtime.status_code == 400
    assert "runtime already assigned or retained" in charlie_reuse_previous_runtime.json()["detail"]


def test_runtime_reservation_purging_state_blocks_transfer_until_purge_finishes(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    admin_headers = {"Authorization": "Bearer admin-token"}

    alice = client.post(
        "/api/gateway/admin/users",
        headers=admin_headers,
        json={"user_id": "alice", "tenant_id": "default", "roles": ["user"], "runtime_id": "shared-runtime"},
    )
    assert alice.status_code == 200, alice.text
    bob = client.post(
        "/api/gateway/admin/users",
        headers=admin_headers,
        json={"user_id": "bob", "tenant_id": "default", "roles": ["user"], "runtime_id": "bob-runtime"},
    )
    assert bob.status_code == 200, bob.text
    runtime_root = tmp_path / "runtime" / "users" / "default" / "shared-runtime"
    runtime_root.mkdir(parents=True)

    deleted = client.delete("/api/gateway/admin/users/alice?tenant_id=default", headers=admin_headers)
    assert deleted.status_code == 200, deleted.text

    from abstractgateway.users import GatewayUserRegistry

    purging = GatewayUserRegistry().mark_runtime_reservation_purging(
        tenant_id="default",
        runtime_id="shared-runtime",
    )
    assert purging.reason == "purging"

    transfer = client.post(
        "/api/gateway/admin/runtime-reservations/shared-runtime/transfer",
        headers=admin_headers,
        json={"tenant_id": "default", "target_user_id": "bob", "confirm_runtime_id": "shared-runtime"},
    )
    assert transfer.status_code == 400
    assert "purge is already in progress" in transfer.json()["detail"]

    purge_retry = client.post(
        "/api/gateway/admin/runtime-reservations/shared-runtime/purge",
        headers=admin_headers,
        json={"tenant_id": "default", "confirm_runtime_id": "shared-runtime", "delete_data": True},
    )
    assert purge_retry.status_code == 200, purge_retry.text
    assert purge_retry.json()["purged"] is True
    assert not runtime_root.exists()


def test_gateway_user_token_exchanges_for_revocable_browser_session(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    admin_headers = {"Authorization": "Bearer admin-token"}

    created = client.post(
        "/api/gateway/admin/users",
        headers=admin_headers,
        json={"user_id": "alice", "tenant_id": "default", "roles": ["user"]},
    )
    assert created.status_code == 200
    alice_token = created.json()["token"]

    login = client.post(
        "/api/gateway/session/login",
        json={"user_id": "alice", "token": alice_token, "remember": True},
    )
    assert login.status_code == 200, login.text
    payload = login.json()
    assert "session_id" not in payload.get("session", {})
    assert "csrf_token" not in payload.get("session", {})
    assert "abstractgateway_session=" in login.headers.get("set-cookie", "")
    assert "abstractgateway_csrf=" in login.headers.get("set-cookie", "")
    assert alice_token not in login.headers.get("set-cookie", "")
    session = client.cookies.get("abstractgateway_session")
    csrf = client.cookies.get("abstractgateway_csrf")
    assert session and session.startswith("agws_")
    assert csrf and csrf.startswith("agcsrf_")

    me = client.get("/api/gateway/me", headers={"X-AbstractGateway-Session": session})
    assert me.status_code == 200, me.text
    assert me.json()["principal"]["user_id"] == "alice"
    assert me.json()["principal"]["source"] == "user-registry"

    forbidden = client.get("/api/gateway/admin/users", headers={"X-AbstractGateway-Session": session})
    assert forbidden.status_code == 403

    no_csrf = client.post(
        "/api/gateway/session/logout",
        headers={"X-AbstractGateway-Session": session},
    )
    assert no_csrf.status_code == 403

    logout = client.post(
        "/api/gateway/session/logout",
        headers={"X-AbstractGateway-Session": session, "X-AbstractGateway-CSRF": csrf},
    )
    assert logout.status_code == 200
    assert logout.json()["deleted"] is True

    after_logout = client.get("/api/gateway/me", headers={"X-AbstractGateway-Session": session})
    assert after_logout.status_code == 401


def test_gateway_browser_session_invalidated_by_user_token_rotation(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    admin_headers = {"Authorization": "Bearer admin-token"}

    created = client.post(
        "/api/gateway/admin/users",
        headers=admin_headers,
        json={"user_id": "alice", "tenant_id": "default", "roles": ["user"]},
    )
    assert created.status_code == 200
    alice_token = created.json()["token"]

    login = client.post("/api/gateway/session/login", json={"user_id": "alice", "token": alice_token})
    assert login.status_code == 200, login.text
    session = client.cookies.get("abstractgateway_session")
    assert session and session.startswith("agws_")

    before_rotate = client.get("/api/gateway/me", headers={"X-AbstractGateway-Session": session})
    assert before_rotate.status_code == 200

    rotated = client.patch(
        "/api/gateway/admin/users/alice?tenant_id=default",
        headers=admin_headers,
        json={"rotate_token": True},
    )
    assert rotated.status_code == 200
    assert rotated.json()["token"] != alice_token

    after_rotate = client.get("/api/gateway/me", headers={"X-AbstractGateway-Session": session})
    assert after_rotate.status_code == 401


def test_gateway_browser_session_cookie_transport_matrix(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    admin_headers = {"Authorization": "Bearer admin-token"}
    created = client.post(
        "/api/gateway/admin/users",
        headers=admin_headers,
        json={"user_id": "alice", "tenant_id": "default", "roles": ["user"]},
    )
    assert created.status_code == 200
    alice_token = created.json()["token"]

    http_login = client.post(
        "/api/gateway/session/login",
        json={"user_id": "alice", "token": alice_token, "remember": False},
    )
    assert http_login.status_code == 200, http_login.text
    http_session_cookie = _set_cookie(http_login, "abstractgateway_session").lower()
    http_csrf_cookie = _set_cookie(http_login, "abstractgateway_csrf").lower()
    assert "httponly" in http_session_cookie
    assert "httponly" not in http_csrf_cookie
    assert "samesite=lax" in http_session_cookie
    assert "samesite=lax" in http_csrf_cookie
    assert "secure" not in http_session_cookie
    assert "secure" not in http_csrf_cookie
    assert "max-age=" not in http_session_cookie

    https_login = client.post(
        "/api/gateway/session/login",
        headers={"X-Forwarded-Proto": "https"},
        json={"user_id": "alice", "token": alice_token, "remember": True},
    )
    assert https_login.status_code == 200, https_login.text
    https_session_cookie = _set_cookie(https_login, "abstractgateway_session").lower()
    https_csrf_cookie = _set_cookie(https_login, "abstractgateway_csrf").lower()
    assert "secure" in https_session_cookie
    assert "secure" in https_csrf_cookie
    assert "max-age=" in https_session_cookie
    assert "max-age=" in https_csrf_cookie


def test_gateway_browser_session_origin_expiry_logout_and_revocation_matrix(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "https://flow.abstractframework.ai,http://localhost:*")
    client = _client(tmp_path, monkeypatch)
    admin_headers = {"Authorization": "Bearer admin-token"}
    created = client.post(
        "/api/gateway/admin/users",
        headers=admin_headers,
        json={"user_id": "alice", "tenant_id": "default", "roles": ["user"]},
    )
    assert created.status_code == 200
    alice_token = created.json()["token"]

    blocked_origin = client.post(
        "/api/gateway/session/login",
        headers={"Origin": "https://evil.example"},
        json={"user_id": "alice", "token": alice_token},
    )
    assert blocked_origin.status_code == 403

    same_origin = client.post(
        "/api/gateway/session/login",
        headers={"Origin": "http://localhost:3000"},
        json={"user_id": "alice", "token": alice_token},
    )
    assert same_origin.status_code == 200, same_origin.text

    cross_origin_allowlisted = client.post(
        "/api/gateway/session/login",
        headers={"Origin": "https://flow.abstractframework.ai"},
        json={"user_id": "alice", "token": alice_token},
    )
    assert cross_origin_allowlisted.status_code == 200, cross_origin_allowlisted.text
    session = client.cookies.get("abstractgateway_session")
    csrf = client.cookies.get("abstractgateway_csrf")
    assert session and csrf

    sessions_file = tmp_path / "runtime" / "auth" / "sessions.json"
    data = json.loads(sessions_file.read_text(encoding="utf-8"))
    assert data.get("sessions")
    data["sessions"][0]["expires_at"] = "2000-01-01T00:00:00+00:00"
    sessions_file.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    expired = client.get("/api/gateway/me", headers={"X-AbstractGateway-Session": session})
    assert expired.status_code == 401

    fresh_login = client.post("/api/gateway/session/login", json={"user_id": "alice", "token": alice_token})
    assert fresh_login.status_code == 200, fresh_login.text
    fresh_session = client.cookies.get("abstractgateway_session")
    fresh_csrf = client.cookies.get("abstractgateway_csrf")
    assert fresh_session and fresh_csrf

    logout = client.post(
        "/api/gateway/session/logout",
        headers={"X-AbstractGateway-Session": fresh_session, "X-AbstractGateway-CSRF": fresh_csrf},
    )
    assert logout.status_code == 200
    assert client.get("/api/gateway/me", headers={"X-AbstractGateway-Session": fresh_session}).status_code == 401

    revoked_login = client.post("/api/gateway/session/login", json={"user_id": "alice", "token": alice_token})
    assert revoked_login.status_code == 200, revoked_login.text
    revoked_session = client.cookies.get("abstractgateway_session")
    assert revoked_session
    rotated = client.patch(
        "/api/gateway/admin/users/alice?tenant_id=default",
        headers=admin_headers,
        json={"rotate_token": True},
    )
    assert rotated.status_code == 200
    assert client.get("/api/gateway/me", headers={"X-AbstractGateway-Session": revoked_session}).status_code == 401


def test_gateway_service_router_uses_distinct_principal_data_planes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")

    from abstractgateway.security.principal import (
        GatewayPrincipal,
        reset_current_gateway_principal,
        set_current_gateway_principal,
    )
    from abstractgateway.service import get_gateway_service, get_gateway_service_for_principal, stop_gateway_runner

    alice = GatewayPrincipal(user_id="alice", tenant_id="team-a", roles=("user",), runtime_id="alice-runtime")
    bob = GatewayPrincipal(user_id="bob", tenant_id="team-a", roles=("user",), runtime_id="bob-runtime")

    try:
        alice_service = get_gateway_service_for_principal(alice)
        bob_service = get_gateway_service_for_principal(bob)

        assert alice_service is not bob_service
        assert alice_service.stores.base_dir != bob_service.stores.base_dir
        assert alice_service.stores.base_dir == tmp_path / "runtime" / "users" / "team-a" / "alice-runtime" / "runtime"
        assert bob_service.stores.base_dir == tmp_path / "runtime" / "users" / "team-a" / "bob-runtime" / "runtime"

        token = set_current_gateway_principal(alice)
        try:
            assert get_gateway_service() is alice_service
        finally:
            reset_current_gateway_principal(token)
    finally:
        stop_gateway_runner()


def test_gateway_service_router_keeps_admin_on_default_data_plane(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")

    from abstractgateway.security.principal import GatewayPrincipal
    from abstractgateway.service import get_gateway_service_for_principal, stop_gateway_runner

    admin = GatewayPrincipal(
        user_id="admin",
        tenant_id="default",
        roles=("admin", "user"),
        runtime_id="default",
        source="user-registry",
    )
    alice = GatewayPrincipal(user_id="alice", tenant_id="default", roles=("user",), runtime_id="alice")

    try:
        admin_service = get_gateway_service_for_principal(admin)
        alice_service = get_gateway_service_for_principal(alice)

        assert admin_service is not alice_service
        assert admin_service.stores.base_dir == tmp_path / "runtime"
        assert admin_service.config.flows_dir == tmp_path / "flows"
        assert alice_service.stores.base_dir == tmp_path / "runtime" / "users" / "default" / "alice" / "runtime"
    finally:
        stop_gateway_runner()


def test_capability_defaults_are_isolated_by_gateway_principal(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    admin_headers = {"Authorization": "Bearer admin-token"}

    alice = client.post(
        "/api/gateway/admin/users",
        headers=admin_headers,
        json={"user_id": "alice", "tenant_id": "default", "roles": ["user"], "runtime_id": "alice"},
    )
    bob = client.post(
        "/api/gateway/admin/users",
        headers=admin_headers,
        json={"user_id": "bob", "tenant_id": "default", "roles": ["user"], "runtime_id": "bob"},
    )
    assert alice.status_code == 200
    assert bob.status_code == 200
    alice_headers = {"Authorization": f"Bearer {alice.json()['token']}"}
    bob_headers = {"Authorization": f"Bearer {bob.json()['token']}"}

    saved = client.put(
        "/api/gateway/config/capability-defaults/output/text",
        headers=alice_headers,
        json={"provider": "openrouter", "model": "alice-model"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["authority"] == "abstractcore.runtime"
    alice_core_config = tmp_path / "runtime" / "users" / "default" / "alice" / "runtime" / "config" / "abstractcore.json"
    assert alice_core_config.exists()
    assert not (alice_core_config.parent / "capability_defaults.json").exists()

    alice_defaults = client.get("/api/gateway/config/capability-defaults", headers=alice_headers)
    bob_defaults = client.get("/api/gateway/config/capability-defaults", headers=bob_headers)
    assert alice_defaults.status_code == 200
    assert bob_defaults.status_code == 200

    alice_text = [r for r in alice_defaults.json()["routes"] if r.get("key") == "output.text"][0]
    bob_text = [r for r in bob_defaults.json()["routes"] if r.get("key") == "output.text"][0]
    assert alice_text["provider"] == "openrouter"
    assert alice_text["model"] == "alice-model"
    assert bob_text.get("provider") != "openrouter"
    assert bob_text.get("model") != "alice-model"

    cleared = client.delete("/api/gateway/config/capability-defaults/output/text", headers=alice_headers)
    assert cleared.status_code == 200
    cleared_text = [r for r in cleared.json()["routes"] if r.get("key") == "output.text"][0]
    assert cleared_text.get("provider") != "openrouter"


def test_sandbox_generation_receives_scoped_capability_defaults(tmp_path: Path, monkeypatch) -> None:
    import abstractruntime.integrations.abstractcore.llm_client as llm_client_mod

    captured: dict[str, Any] = {}

    class FakeSandboxLLM:
        def __init__(
            self,
            *,
            provider: str,
            model: str,
            llm_kwargs=None,
            artifact_store=None,
            core_config_file=None,
            capability_defaults=None,
            **_kwargs: Any,
        ) -> None:
            _ = llm_kwargs, artifact_store
            captured["provider"] = provider
            captured["model"] = model
            captured["core_config_file"] = str(core_config_file)
            captured["capability_defaults"] = capability_defaults

        def set_provider_endpoint_profile_resolver(self, resolver) -> None:
            captured["resolver_attached"] = callable(resolver)

        def generate(self, *, prompt, messages=None, media=None, system_prompt=None, params=None):
            captured["generate"] = {
                "prompt": prompt,
                "messages": messages,
                "media": media,
                "system_prompt": system_prompt,
                "params": params,
            }
            return {"content": "ok"}

    monkeypatch.setattr(llm_client_mod, "LocalAbstractCoreLLMClient", FakeSandboxLLM)

    client = _client(tmp_path, monkeypatch)
    admin_headers = {"Authorization": "Bearer admin-token"}
    alice = client.post(
        "/api/gateway/admin/users",
        headers=admin_headers,
        json={"user_id": "alice", "tenant_id": "default", "roles": ["user"], "runtime_id": "alice"},
    )
    assert alice.status_code == 200, alice.text
    alice_headers = {"Authorization": f"Bearer {alice.json()['token']}"}

    saved = client.put(
        "/api/gateway/config/capability-defaults/input/voice",
        headers=alice_headers,
        json={"provider": "faster-whisper", "model": "large-v3"},
    )
    assert saved.status_code == 200, saved.text

    resp = client.post(
        "/api/gateway/sandbox/generate",
        headers=alice_headers,
        json={
            "capability": "input.text",
            "provider": "lmstudio",
            "model": "qwen/qwen3.5-9b",
            "prompt": "what does it say?",
            "attachments": [{"$artifact": "audio-1", "content_type": "audio/wav"}],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["response"] == "ok"
    assert captured["resolver_attached"] is True
    assert "users/default/alice/runtime/config/abstractcore.json" in captured["core_config_file"]
    routes = {
        row["key"]: row
        for row in captured["capability_defaults"]["routes"]
        if isinstance(row, dict) and row.get("key")
    }
    assert routes["input.voice"]["provider"] == "faster-whisper"
    assert routes["input.voice"]["model"] == "large-v3"


def test_gateway_defaults_are_inherited_by_users_until_user_override(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    bootstrap_headers = {"Authorization": "Bearer admin-token"}

    admin_created = client.post(
        "/api/gateway/admin/users",
        headers=bootstrap_headers,
        json={"user_id": "admin", "tenant_id": "default", "roles": ["admin", "user"], "runtime_id": "default"},
    )
    assert admin_created.status_code == 200
    admin_headers = {"Authorization": f"Bearer {admin_created.json()['token']}"}

    saved_gateway = client.put(
        "/api/gateway/config/capability-defaults/output/text",
        headers=admin_headers,
        json={"provider": "openrouter", "model": "gateway-model"},
    )
    assert saved_gateway.status_code == 200, saved_gateway.text
    assert saved_gateway.json()["authority"] == "abstractcore.gateway_runtime"
    assert (tmp_path / "runtime" / "config" / "abstractcore.json").exists()

    alice = client.post(
        "/api/gateway/admin/users",
        headers=admin_headers,
        json={"user_id": "alice", "tenant_id": "default", "roles": ["user"], "runtime_id": "alice"},
    )
    bob = client.post(
        "/api/gateway/admin/users",
        headers=admin_headers,
        json={"user_id": "bob", "tenant_id": "default", "roles": ["user"], "runtime_id": "bob"},
    )
    assert alice.status_code == 200
    assert bob.status_code == 200
    alice_headers = {"Authorization": f"Bearer {alice.json()['token']}"}
    bob_headers = {"Authorization": f"Bearer {bob.json()['token']}"}

    alice_inherited = client.get("/api/gateway/config/capability-defaults", headers=alice_headers)
    bob_inherited = client.get("/api/gateway/config/capability-defaults", headers=bob_headers)
    assert alice_inherited.status_code == 200
    assert bob_inherited.status_code == 200
    alice_text = [r for r in alice_inherited.json()["routes"] if r.get("key") == "output.text"][0]
    bob_text = [r for r in bob_inherited.json()["routes"] if r.get("key") == "output.text"][0]
    assert alice_text["provider"] == "openrouter"
    assert alice_text["model"] == "gateway-model"
    assert alice_text["source"] == "abstractcore.gateway_runtime"
    assert bob_text["provider"] == "openrouter"
    assert bob_text["model"] == "gateway-model"

    alice_override = client.put(
        "/api/gateway/config/capability-defaults/output/text",
        headers=alice_headers,
        json={"provider": "anthropic", "model": "alice-model"},
    )
    assert alice_override.status_code == 200
    alice_text = [r for r in alice_override.json()["routes"] if r.get("key") == "output.text"][0]
    assert alice_text["provider"] == "anthropic"
    assert alice_text["model"] == "alice-model"
    assert alice_text["source"] == "abstractcore.runtime"

    bob_still_inherits = client.get("/api/gateway/config/capability-defaults", headers=bob_headers)
    bob_text = [r for r in bob_still_inherits.json()["routes"] if r.get("key") == "output.text"][0]
    assert bob_text["provider"] == "openrouter"
    assert bob_text["model"] == "gateway-model"
    assert bob_text["source"] == "abstractcore.gateway_runtime"


def test_user_runtime_legacy_capability_defaults_file_is_ignored(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    admin_headers = {"Authorization": "Bearer admin-token"}

    alice = client.post(
        "/api/gateway/admin/users",
        headers=admin_headers,
        json={"user_id": "alice", "tenant_id": "default", "roles": ["user"], "runtime_id": "alice"},
    )
    assert alice.status_code == 200
    legacy = tmp_path / "runtime" / "users" / "default" / "alice" / "runtime" / "config" / "capability_defaults.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        json.dumps({"version": 1, "routes": {"output.text": {"provider": "legacy-provider", "model": "legacy-model"}}}),
        encoding="utf-8",
    )

    alice_headers = {"Authorization": f"Bearer {alice.json()['token']}"}
    response = client.get("/api/gateway/config/capability-defaults", headers=alice_headers)
    assert response.status_code == 200
    text_route = [r for r in response.json()["routes"] if r.get("key") == "output.text"][0]
    assert text_route["configured"] is False
    assert text_route.get("provider") != "legacy-provider"


def test_removed_capability_default_overlay_is_ignored(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    bootstrap_headers = {"Authorization": "Bearer admin-token"}

    admin_created = client.post(
        "/api/gateway/admin/users",
        headers=bootstrap_headers,
        json={"user_id": "admin", "tenant_id": "default", "roles": ["admin", "user"], "runtime_id": "default"},
    )
    assert admin_created.status_code == 200
    admin_headers = {"Authorization": f"Bearer {admin_created.json()['token']}"}

    saved_gateway = client.put(
        "/api/gateway/config/capability-defaults/output/text",
        headers=admin_headers,
        json={"provider": "openrouter", "model": "gateway-model"},
    )
    assert saved_gateway.status_code == 200

    alice = client.post(
        "/api/gateway/admin/users",
        headers=admin_headers,
        json={"user_id": "alice", "tenant_id": "default", "roles": ["user"], "runtime_id": "alice"},
    )
    assert alice.status_code == 200
    alice_headers = {"Authorization": f"Bearer {alice.json()['token']}"}

    old_overlay = tmp_path / "runtime" / "users" / "default" / "alice" / "runtime" / "config" / "capability_defaults.json"
    old_overlay.parent.mkdir(parents=True)
    old_overlay.write_text(
        json.dumps({"version": 1, "routes": {"output.text": {"provider": "legacy", "model": "legacy-model"}}}),
        encoding="utf-8",
    )

    alice_defaults = client.get("/api/gateway/config/capability-defaults", headers=alice_headers)
    assert alice_defaults.status_code == 200
    alice_text = [r for r in alice_defaults.json()["routes"] if r.get("key") == "output.text"][0]
    assert alice_text["provider"] == "openrouter"
    assert alice_text["model"] == "gateway-model"
    assert alice_text["source"] == "abstractcore.gateway_runtime"
