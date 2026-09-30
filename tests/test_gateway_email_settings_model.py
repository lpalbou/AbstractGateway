"""The email settings model of 2026-09-30 (DESIGN day-review §1, §5.2, §6, §9; CONTRACT §5.4):
the admin's one switch ("Mailboxes for users") and the capabilities.json v2 -> v3 migration, the
account page's additive `GET /me/email` fields, mailbox server discovery and the one-call
Connect (save + test, the failing step named), the user's email address route, and the Active
switch's own-account guard. Hermetic: fake IMAP/SMTP on localhost, discovery lookups injected."""

from __future__ import annotations

import json
import socket

import pytest

from email_fixtures import *  # noqa: F401,F403 - fixtures
from email_fixtures import ADMIN, ALICE, BOB, PASSWORDS, connect_body, plane_of

pytestmark = pytest.mark.integration


@pytest.fixture
def no_discovery_network(monkeypatch):
    """Discovery's network steps answer "nothing here" (no test reaches DNS or HTTPS)."""

    from abstractcore.comms.email import discovery

    calls = []
    monkeypatch.setattr(discovery._net, "http_get", lambda url, timeout: calls.append(url) or None)
    monkeypatch.setattr(discovery._net, "resolve_srv", lambda name, timeout: calls.append(name) or [])
    monkeypatch.setattr(discovery._net, "resolve_mx", lambda domain, timeout: calls.append(domain) or [])
    return calls


def _autoconfig(imap_port: int, smtp_port: int) -> bytes:
    return (
        '<?xml version="1.0"?><clientConfig version="1.1"><emailProvider id="example.test">'
        f'<incomingServer type="imap"><hostname>localhost</hostname><port>{imap_port}</port>'
        "<socketType>SSL</socketType><username>%EMAILADDRESS%</username></incomingServer>"
        f'<outgoingServer type="smtp"><hostname>localhost</hostname><port>{smtp_port}</port>'
        "<socketType>STARTTLS</socketType><username>%EMAILADDRESS%</username></outgoingServer>"
        "</emailProvider></clientConfig>"
    ).encode("utf-8")


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _audit(gateway) -> list:
    path = gateway["data_dir"] / "audit_log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if '"event"' in line]


# ---------------------------------------------------------------------------------------
# Capabilities: one admin switch, v2 -> v3 migration
# ---------------------------------------------------------------------------------------


def _switch_on(user_id: str) -> None:
    plane = plane_of(user_id)
    plane.email_dir.mkdir(parents=True, exist_ok=True)
    (plane.email_dir / "agent_tools.json").write_text(json.dumps({"version": 1, "enabled": True}), encoding="utf-8")


def _write_caps(gateway, doc: dict) -> None:
    path = gateway["data_dir"] / "auth" / "capabilities.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_migration_v2_to_v3_gives_nobody_tools_they_could_not_use(gateway) -> None:
    from abstractgateway.mail.accounts import agent_tools_available
    from abstractgateway.users import GatewayUserRegistry

    GatewayUserRegistry().create_user(user_id="carol", roles=["user"], email="carol@example.test")
    # v2: the built-in default was OFF. Alice was made available by a per-user override and
    # switched the tools on; Bob switched them on while available, then lost availability (no
    # override, no gateway default: the v2 built-in OFF); Carol never switched them on.
    _write_caps(gateway, {"version": 2, "defaults": {}, "users": {"default:alice": {"email_agent_tools": True}}})
    _switch_on("alice")
    _switch_on("bob")

    assert agent_tools_available(plane_of("alice")) is True
    assert agent_tools_available(plane_of("bob")) is False  # pinned off by the migration
    assert agent_tools_available(plane_of("carol")) is True  # the new default: she may opt in (her switch is off)

    doc = json.loads((gateway["data_dir"] / "auth" / "capabilities.json").read_text(encoding="utf-8"))
    assert doc["version"] == 3
    assert doc["users"]["default:bob"]["email_agent_tools"] is False
    assert doc["users"]["default:bob"]["by"] == "migration:capabilities-v3"
    assert doc["users"]["default:alice"] == {"email_agent_tools": True}
    assert "default:carol" not in doc["users"]
    migrated = [e for e in _audit(gateway) if e["event"] == "email.capabilities_migrated"]
    assert len(migrated) == 1 and migrated[0]["pinned_off"] == ["default:bob"] and migrated[0]["from_version"] == 2

    # Once: a second read neither rescans nor audits again.
    agent_tools_available(plane_of("bob"))
    assert len([e for e in _audit(gateway) if e["event"] == "email.capabilities_migrated"]) == 1
    # The admin's Users table shows the note + Reset: `inherit` clears the pinned override.
    r = gateway["client"].put("/api/gateway/admin/users/bob/email", headers=ADMIN, json={"inherit": ["email_agent_tools"]})
    assert r.json()["capabilities"]["email_agent_tools"] == {"value": True, "source": "built-in"}


def test_migration_keeps_users_the_gateway_default_already_allowed(gateway) -> None:
    from abstractgateway.mail.accounts import agent_tools_available

    _write_caps(gateway, {"version": 2, "defaults": {"email_agent_tools": True}, "users": {}})
    _switch_on("bob")
    assert agent_tools_available(plane_of("bob")) is True
    doc = json.loads((gateway["data_dir"] / "auth" / "capabilities.json").read_text(encoding="utf-8"))
    assert doc["version"] == 3 and doc["users"] == {} and doc["defaults"] == {"email_agent_tools": True}


def test_fresh_gateway_gets_v3_with_the_new_default(gateway) -> None:
    from abstractgateway.mail.accounts import capability_for

    assert capability_for(plane_of("alice"), "email_agent_tools") == {"value": True, "source": "built-in"}
    doc = json.loads((gateway["data_dir"] / "auth" / "capabilities.json").read_text(encoding="utf-8"))
    assert doc["version"] == 3
    assert [e for e in _audit(gateway) if e["event"] == "email.capabilities_migrated"] == []  # nothing to record


def test_admin_one_switch_applies_to_every_user(gateway) -> None:
    c = gateway["client"]
    r = c.put("/api/gateway/admin/email/capabilities", headers=ADMIN, json={"email": False})
    assert r.status_code == 200
    for who in ("alice", "bob"):
        me = c.get("/api/gateway/me/email", headers=gateway[who]).json()
        assert me["email_available"] is False
        assert me["agent_tools"]["unavailable_reason"] == "Your admin turned mailboxes off."
        assert me["notifications_unavailable_reason"] == "Your admin turned mailboxes off."
    assert c.put("/api/gateway/admin/email/capabilities", headers=ADMIN, json={"reset": ["email"]}).status_code == 200
    assert c.get("/api/gateway/me/email", headers=gateway["alice"]).json()["email_available"] is True


# ---------------------------------------------------------------------------------------
# GET /me/email additive fields
# ---------------------------------------------------------------------------------------


def test_me_email_carries_the_account_page_fields(gateway, imap, smtp) -> None:
    c = gateway["client"]
    me = c.get("/api/gateway/me/email", headers=gateway["alice"]).json()
    assert me["email_address"] == ALICE and me["registered_address"] == ALICE  # the email address, before any mailbox
    assert me["email_available"] is True
    assert me["notifications"] == {"job_failed": True, "approval_needed": True}
    assert me["agent_tools"]["on"] is False and me["agent_tools"]["available"] is False
    assert me["agent_tools"]["unavailable_reason"] == "Connect a mailbox first."
    providers = {p["id"]: p for p in me["oauth_providers"]}
    assert set(providers) == {"google", "microsoft"}
    for p in providers.values():
        assert isinstance(p["available"], bool)
        assert (p["reason"] is None) == p["available"]

    assert c.put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(ALICE, imap, smtp)).status_code == 200
    at = c.get("/api/gateway/me/email", headers=gateway["alice"]).json()["agent_tools"]
    assert at["available"] is True and at["unavailable_reason"] is None
    # "Use this mailbox" off: the switches name that reason.
    assert c.put("/api/gateway/me/email/enabled", headers=gateway["alice"], json={"enabled": False}).status_code == 200
    me = c.get("/api/gateway/me/email", headers=gateway["alice"]).json()
    assert me["agent_tools"]["unavailable_reason"] == me["notifications_unavailable_reason"]
    assert "Use this mailbox" in me["agent_tools"]["unavailable_reason"]


def test_gateway_oauth_client_makes_a_provider_available(gateway) -> None:
    c = gateway["client"]
    r = c.put("/api/gateway/admin/email/oauth-clients/google", headers=ADMIN, json={"client_id": "cid.apps.example.test", "client_secret": "s"})
    assert r.status_code == 200, r.text
    google = next(p for p in c.get("/api/gateway/me/email", headers=gateway["bob"]).json()["oauth_providers"] if p["id"] == "google")
    assert google == {"id": "google", "available": True, "reason": None}


# ---------------------------------------------------------------------------------------
# Discovery and the one-call Connect
# ---------------------------------------------------------------------------------------


def test_discover_known_provider_without_network(gateway, no_discovery_network) -> None:
    r = gateway["client"].post("/api/gateway/me/email/discover", headers=gateway["alice"], json={"address": "me@fastmail.com"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["found"] is True and body["source"] == "known"
    assert body["imap"]["host"] == "imap.fastmail.com" and body["smtp"]["host"] == "smtp.fastmail.com"
    assert no_discovery_network == []


def test_discover_not_found_lists_what_was_tried(gateway, no_discovery_network) -> None:
    c = gateway["client"]
    r = c.post("/api/gateway/me/email/discover", headers=gateway["alice"], json={"address": ALICE})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["found"] is False and body["domain"] == "example.test"
    assert [t["step"] for t in body["tried"]][:2] == ["known", "autoconfig"]
    r = c.post("/api/gateway/me/email/discover", headers=gateway["alice"], json={"address": "not-an-address"})
    assert r.status_code == 400 and r.json()["detail"]["reason_code"] == "email_invalid_settings"
    # Signed-in users only.
    assert c.post("/api/gateway/me/email/discover", json={"address": ALICE}).status_code == 401


def test_connect_without_servers_discovers_them(gateway, imap, smtp, monkeypatch) -> None:
    from abstractcore.comms.email import discovery

    doc = _autoconfig(imap.port, smtp.port)
    monkeypatch.setattr(discovery._net, "http_get", lambda url, timeout: doc if "autoconfig.example.test" in url else None)
    monkeypatch.setattr(discovery._net, "resolve_srv", lambda name, timeout: [])
    monkeypatch.setattr(discovery._net, "resolve_mx", lambda domain, timeout: [])
    r = gateway["client"].put("/api/gateway/me/email", headers=gateway["alice"], json={"address": ALICE, "password": PASSWORDS[ALICE]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True and body["effective_enabled"] is True
    assert body["imap"]["host"] == "localhost" and body["imap"]["port"] == imap.port
    assert body["smtp"]["port"] == smtp.port and body["smtp"]["security"] == "starttls"
    assert body["username"] == ALICE  # the discovered form: the address
    assert body["discovery"]["source"] == "autoconfig"
    assert imap.logins and smtp.logins  # saved AND tested in the one call


def test_connect_discovery_failure_is_400_with_tried_and_stores_nothing(gateway, no_discovery_network) -> None:
    c = gateway["client"]
    r = c.put("/api/gateway/me/email", headers=gateway["alice"], json={"address": ALICE, "password": "x"})
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["reason_code"] == "email_discovery_failed"
    assert detail["message"] == "Couldn't find the mail servers for example.test. Open Server settings and enter them."
    assert [t["step"] for t in detail["tried"]][0] == "known"
    assert c.get("/api/gateway/me/email", headers=gateway["alice"]).json()["configured"] is False


def test_connect_names_the_failing_step(gateway, imap, smtp) -> None:
    c = gateway["client"]
    r = c.put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(ALICE, imap, smtp, password="wrong-password"))
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["reason_code"] == "email_auth_failed" and detail["step"] == "imap"
    assert detail["message"] == "Sign-in refused by localhost — check the password."

    closed = _closed_port()
    body = connect_body(ALICE, imap, smtp)
    body["smtp"]["port"] = closed
    r = c.put("/api/gateway/me/email", headers=gateway["alice"], json=body)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["step"] == "smtp" and detail["message"] == f"Couldn't reach localhost:{closed}."
    assert c.get("/api/gateway/me/email", headers=gateway["alice"]).json()["configured"] is False


# ---------------------------------------------------------------------------------------
# The email address (users registry = source of truth)
# ---------------------------------------------------------------------------------------


def test_user_sets_their_own_email_address(gateway) -> None:
    from abstractgateway.users import GatewayUserRegistry

    c = gateway["client"]
    r = c.put("/api/gateway/me/email/address", headers=gateway["alice"], json={"address": "Alice.New@Example.test"})
    assert r.status_code == 200, r.text
    assert r.json()["email_address"] == r.json()["registered_address"] == "alice.new@example.test"
    assert GatewayUserRegistry().get_user("alice").email == "alice.new@example.test"
    assert GatewayUserRegistry().get_user("bob").email == BOB  # only their own record
    assert any(e["event"] == "email.address_changed" and e["user_id"] == "alice" for e in _audit(gateway))

    for bad in ("two@a.test, b@c.test", "no-at-sign", "Name <x@y.test>"):
        r = c.put("/api/gateway/me/email/address", headers=gateway["alice"], json={"address": bad})
        assert r.status_code == 400 and r.json()["detail"]["reason_code"] == "email_invalid_settings", bad
    assert GatewayUserRegistry().get_user("alice").email == "alice.new@example.test"

    r = c.put("/api/gateway/me/email/address", headers=gateway["alice"], json={"address": ""})
    assert r.status_code == 200 and r.json()["email_address"] == "" and r.json()["registered_address"] == ""
    assert GatewayUserRegistry().get_user("alice").email == ""


def test_account_less_operator_address_uses_the_gateway_setting(gateway) -> None:
    from abstractgateway.runtime_config import resolve_operator_email

    r = gateway["client"].put("/api/gateway/me/email/address", headers=ADMIN, json={"address": "op@example.test"})
    assert r.status_code == 200, r.text
    assert r.json()["email_address"] == "op@example.test"
    assert resolve_operator_email(gateway["data_dir"])["value"] == "op@example.test"


def test_admin_create_user_body_keeps_email_as_the_email_address(gateway) -> None:
    c = gateway["client"]
    r = c.post("/api/gateway/admin/users", headers=ADMIN, json={"user_id": "dora", "roles": ["user"], "email": "dora@example.test"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    me = c.get("/api/gateway/me/email", headers={"Authorization": f"Bearer {token}"}).json()
    assert me["email_address"] == "dora@example.test" and me["configured"] is False


# ---------------------------------------------------------------------------------------
# Active: an admin can't deactivate their own account
# ---------------------------------------------------------------------------------------


def test_admin_cannot_deactivate_their_own_account(gateway) -> None:
    from abstractgateway.users import GatewayUserRegistry

    c = gateway["client"]
    _rec, root_token = GatewayUserRegistry().create_user(user_id="root", roles=["admin"])
    GatewayUserRegistry().create_user(user_id="root2", roles=["admin"])
    root = {"Authorization": f"Bearer {root_token}"}
    r = c.patch("/api/gateway/admin/users/root", headers=root, json={"enabled": False})
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == {"reason_code": "cannot_deactivate_self", "message": "You can't deactivate your own account."}
    assert GatewayUserRegistry().get_user("root").enabled is True
    # Other accounts: Active off and back on.
    assert c.patch("/api/gateway/admin/users/alice", headers=root, json={"enabled": False}).json()["user"]["enabled"] is False
    assert c.patch("/api/gateway/admin/users/alice", headers=root, json={"enabled": True}).json()["user"]["enabled"] is True
    # Another admin may deactivate root (root2 stays an active admin).
    assert c.patch("/api/gateway/admin/users/root", headers=ADMIN, json={"enabled": False}).status_code == 200


# ---------------------------------------------------------------------------------------
# OpenAPI words: "email address" vs "mailbox"
# ---------------------------------------------------------------------------------------


def test_openapi_summaries_use_the_two_words(gateway) -> None:
    paths = gateway["app"].openapi()["paths"]

    def summary(path: str, method: str) -> str:
        return str(paths[f"/api/gateway{path}"][method].get("summary") or "")

    assert "email address" in summary("/me/email/address", "put")
    assert "mailbox" in summary("/me/email", "put").lower()
    assert "mailbox" in summary("/me/email/discover", "post").lower()
    assert "Job failed" in summary("/me/email/notifications", "put")
    assert "email address" in summary("/admin/users", "post")
    assert "Mailboxes for users" in summary("/admin/email/capabilities", "get")
    desc = str(paths["/api/gateway/session/recovery/request"]["post"].get("description") or "")
    assert "no_email_address" in desc and "too_many_requests" in desc and "retry_after_s" in desc
    for path, ops in paths.items():
        for op in ops.values():
            text = f"{op.get('summary') or ''}"
            assert "Turn on" not in text and "Turn off" not in text and "Save and test" not in text, (path, text)
