"""Per-user email accounts over HTTP (framework backlog 0992 WP3): connect / test / disconnect,
typed failures with cause and fix, RBAC (a user reaches only their own account), the admin's
per-user switch without content, the sentinel-password sweep.

Every test drives the real email + gateway routers behind the real security middleware,
against AbstractCore's hermetic IMAP/SMTP servers (verified TLS, throwaway CA)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from email_fixtures import *  # noqa: F401,F403 - fixtures
from email_fixtures import ADMIN, ALICE, BOB, PASSWORDS, all_files_bytes, connect_body, plane_of

pytestmark = pytest.mark.integration


def test_connect_test_disconnect_round_trip(gateway, imap, smtp) -> None:
    c = gateway["client"]
    r = c.get("/api/gateway/me/email", headers=gateway["alice"])
    assert r.status_code == 200, r.text
    assert r.json()["configured"] is False

    r = c.put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(ALICE, imap, smtp))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True and body["address"] == ALICE
    assert body["secret_set"] is True
    assert body["effective_enabled"] is True
    assert body["registered_address"] == ALICE
    # The default recipient policy: an allowlist holding the registered address.
    assert body["policy"]["mode"] == "allowlist" and body["policy"]["entries"] == [ALICE]
    assert body["limits"]["per_hour"] == 100 and body["limits"]["per_day"] == 1000
    assert body["limits"]["source"] == "default"
    assert PASSWORDS[ALICE] not in r.text
    assert "config_file" not in body

    r = c.post("/api/gateway/me/email/test", headers=gateway["alice"])
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["imap"]["ok"] is True and r.json()["smtp"]["ok"] is True

    plane = plane_of("alice")
    assert (plane.email_dir / "account" / "email" / "secret.enc").is_file()

    r = c.delete("/api/gateway/me/email", headers=gateway["alice"])
    assert r.status_code == 200, r.text
    assert r.json()["configured"] is False and r.json()["secret_set"] is False
    assert not (plane.email_dir / "account" / "email" / "secret.enc").exists()


def test_wrong_password_names_cause_and_fix_and_stores_nothing(gateway, imap, smtp) -> None:
    c = gateway["client"]
    r = c.put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(ALICE, imap, smtp, password="wrong-password"))
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["reason_code"] == "email_auth_failed"
    assert detail["cause"] and detail["fix"]
    assert "wrong-password" not in r.text
    assert c.get("/api/gateway/me/email", headers=gateway["alice"]).json()["configured"] is False


def test_user_reaches_only_their_own_account(gateway, imap, smtp) -> None:
    c = gateway["client"]
    assert c.put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(ALICE, imap, smtp)).status_code == 200

    # Bob's /me is Bob's plane: nothing of Alice's is visible, testable or removable.
    bob_view = c.get("/api/gateway/me/email", headers=gateway["bob"]).json()
    assert bob_view["configured"] is False and bob_view["address"] == ""
    assert ALICE not in json.dumps(bob_view)
    r = c.post("/api/gateway/me/email/test", headers=gateway["bob"])
    assert r.status_code == 404 and r.json()["detail"]["reason_code"] == "email_not_configured"
    c.delete("/api/gateway/me/email", headers=gateway["bob"])
    assert c.get("/api/gateway/me/email", headers=gateway["alice"]).json()["configured"] is True

    # No path or body id selects another user's account: admin routes refuse non-admins,
    # the legacy aliases are admin-only and act on the CALLER's own account.
    assert c.get("/api/gateway/admin/users/alice/email", headers=gateway["bob"]).status_code == 403
    assert c.put("/api/gateway/admin/users/alice/email", headers=gateway["bob"], json={"enabled": False}).status_code == 403
    assert c.get("/api/gateway/email/accounts", headers=gateway["bob"]).status_code == 403
    assert c.get("/api/gateway/email/messages", headers=gateway["bob"]).status_code == 403
    assert c.post("/api/gateway/email/send", headers=gateway["bob"], json={"to": ALICE, "subject": "x"}).status_code == 403

    # The two planes are different directories.
    assert plane_of("alice").root != plane_of("bob").root


def test_entities_have_no_mailbox(gateway) -> None:
    from abstractgateway.users import GatewayUserRegistry

    _rec, token = GatewayUserRegistry().create_user(user_id="luna", roles=["entity"])
    r = gateway["client"].get("/api/gateway/me/email", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["reason_code"] == "email_principal_refused"


def test_admin_sees_status_never_content_or_correspondents(gateway, imap, smtp) -> None:
    c = gateway["client"]
    assert c.put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(ALICE, imap, smtp)).status_code == 200
    assert c.put("/api/gateway/me/email/policy", headers=gateway["alice"], json={"mode": "allowlist", "entries": [ALICE, "friend@example.test"]}).status_code == 200

    r = c.get("/api/gateway/admin/users/alice/email", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {
        "tenant_id", "user_id", "configured", "address", "auth_kind", "user_enabled", "admin_enabled",
        "effective_enabled", "status", "capabilities", "agent_tools", "watcher", "state",
    }
    assert body["configured"] is True and body["address"] == ALICE and body["state"] == "connected"
    text = r.text
    assert "friend@example.test" not in text  # the user's correspondents stay private
    assert PASSWORDS[ALICE] not in text
    assert "policy" not in body and "imap" not in body

    # The admin's legacy mail routes read the ADMIN's own mailbox (none here), never Alice's.
    r = c.get("/api/gateway/email/messages", headers=ADMIN)
    assert r.status_code == 404 and r.json()["detail"]["reason_code"] == "email_not_configured"
    assert c.get("/api/gateway/admin/users/nobody/email", headers=ADMIN).status_code == 404


def test_admin_switch_turns_email_off_and_keeps_settings(gateway, imap, smtp) -> None:
    c = gateway["client"]
    assert c.put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(ALICE, imap, smtp)).status_code == 200
    r = c.put("/api/gateway/admin/users/alice/email", headers=ADMIN, json={"enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["admin_enabled"] is False and r.json()["state"] == "turned off by an administrator"

    me = c.get("/api/gateway/me/email", headers=gateway["alice"]).json()
    assert me["configured"] is True and me["admin_enabled"] is False and me["effective_enabled"] is False
    assert me["admin_disabled"]["cause"] and me["admin_disabled"]["fix"]

    # Sending (a test notification) refuses with the admin's cause.
    r = c.post("/api/gateway/me/notifications/test", headers=gateway["alice"])
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False and r.json()["error"]["code"] == "email_disabled"
    assert smtp.messages == []

    # Connect, Test and OAuth sign-in open connections to hosts the user chose: all refused
    # while the admin has email off, before any connection (the fake servers see no sign-in).
    imap.logins.clear()
    smtp.logins.clear()
    for method, path, body in (
        ("put", "/api/gateway/me/email", connect_body(ALICE, imap, smtp)),
        ("post", "/api/gateway/me/email/test", None),
        ("post", "/api/gateway/me/email/oauth/start", {"address": ALICE, "provider": "microsoft", "client_id": "cid"}),
    ):
        r = getattr(c, method)(path, headers=gateway["alice"], **({"json": body} if body is not None else {}))
        assert r.status_code == 409, (path, r.text)
        assert r.json()["detail"]["reason_code"] == "email_disabled" and "Your admin turned mailboxes off" in r.json()["detail"]["message"]
    assert imap.logins == [] and smtp.logins == []

    # The user cannot turn it back on themselves: their switch is a separate one.
    c.put("/api/gateway/me/email/enabled", headers=gateway["alice"], json={"enabled": True})
    assert c.get("/api/gateway/me/email", headers=gateway["alice"]).json()["effective_enabled"] is False

    assert c.put("/api/gateway/admin/users/alice/email", headers=ADMIN, json={"enabled": True}).status_code == 200
    assert c.get("/api/gateway/me/email", headers=gateway["alice"]).json()["effective_enabled"] is True

    audit = (gateway["data_dir"] / "audit_log.jsonl").read_text(encoding="utf-8")
    events = [json.loads(line) for line in audit.splitlines() if '"event"' in line]
    assert any(e["event"] == "email.capability_changed" and e["user_id"] == "alice" and e["enabled"] is False for e in events)


def test_policy_limits_and_user_switch(gateway, imap, smtp) -> None:
    c = gateway["client"]
    assert c.put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(ALICE, imap, smtp)).status_code == 200

    r = c.put("/api/gateway/me/email/policy", headers=gateway["alice"], json={"mode": "allowlist", "entries": [ALICE, "example.org"]})
    assert r.status_code == 200, r.text
    assert r.json()["policy"] == {"mode": "allowlist", "entries": [ALICE, "example.org"], "always_allow": [ALICE, "example.org"], "always_deny": [], "default": False, "self_addresses": [ALICE]}
    r = c.post("/api/gateway/me/email/policy/check", headers=gateway["alice"], json={"addresses": ["a@example.org", "x@elsewhere.test"]})
    assert r.status_code == 200, r.text
    verdicts = {v["address"]: v["allowed"] for v in r.json()["recipients"]}
    assert verdicts == {"a@example.org": True, "x@elsewhere.test": False}

    r = c.put("/api/gateway/me/email/policy", headers=gateway["alice"], json={"mode": "nonsense", "entries": []})
    assert r.status_code == 400 and r.json()["detail"]["reason_code"] == "email_invalid_settings"

    r = c.put("/api/gateway/me/email/limits", headers=gateway["alice"], json={"per_hour": 5, "per_day": 50})
    assert r.status_code == 200 and r.json()["limits"]["per_hour"] == 5 and r.json()["limits"]["per_day"] == 50

    r = c.put("/api/gateway/me/email/enabled", headers=gateway["alice"], json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["effective_enabled"] is False


def test_ca_file_is_an_admin_setting(gateway, imap, smtp, ca) -> None:
    body = connect_body(ALICE, imap, smtp)
    body["imap"]["ca_file"] = str(ca.ca_pem)
    r = gateway["client"].put("/api/gateway/me/email", headers=gateway["alice"], json=body)
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["reason_code"] == "email_invalid_settings"


def test_sentinel_password_never_leaves_the_sealed_file(gateway, imap, smtp) -> None:
    sentinel = "S3NTINEL-pw-7f3a9c"
    imap.users[ALICE] = sentinel
    smtp.users[ALICE] = sentinel
    c = gateway["client"]
    responses = []
    r = c.put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(ALICE, imap, smtp, password=sentinel))
    assert r.status_code == 200, r.text
    responses.append(r.text)
    for method, path in (("get", "/api/gateway/me/email"), ("post", "/api/gateway/me/email/test"), ("get", "/api/gateway/me/notifications")):
        responses.append(getattr(c, method)(path, headers=gateway["alice"]).text)
    responses.append(c.get("/api/gateway/admin/users/alice/email", headers=ADMIN).text)
    for text in responses:
        assert sentinel not in text
    hits = [str(p) for p, data in all_files_bytes(Path(gateway["data_dir"])) if sentinel.encode() in data]
    assert hits == [], f"the password appears in clear in: {hits}"
    # And the sealed file does exist (the check above is not vacuous).
    assert (plane_of("alice").email_dir / "account" / "email" / "secret.enc").is_file()


def test_bob_connects_his_own_account_independently(gateway, imap, imap_bob, smtp) -> None:
    c = gateway["client"]
    assert c.put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(ALICE, imap, smtp)).status_code == 200
    assert c.put("/api/gateway/me/email", headers=gateway["bob"], json=connect_body(BOB, imap_bob, smtp)).status_code == 200
    assert c.get("/api/gateway/me/email", headers=gateway["alice"]).json()["address"] == ALICE
    assert c.get("/api/gateway/me/email", headers=gateway["bob"]).json()["address"] == BOB
    c.delete("/api/gateway/me/email", headers=gateway["bob"])
    assert c.get("/api/gateway/me/email", headers=gateway["alice"]).json()["configured"] is True


def test_gateway_defaults_and_per_user_overrides_decide_what_is_available(gateway, imap, smtp) -> None:
    c = gateway["client"]
    caps = {x["id"]: x for x in c.get("/api/gateway/admin/email/capabilities", headers=ADMIN).json()["capabilities"]}
    # capabilities.json v3: agent email tools are available by default (each user still opts in).
    assert caps["email"]["default"] is True and caps["email_agent_tools"]["default"] is True and caps["email_recovery"]["default"] is True
    assert caps["email_agent_tools"]["built_in_default"] is True
    # DESIGN §1/§5.2 words: the admin's one switch, the rest under Advanced.
    assert caps["email"]["label"] == "Mailboxes for users" and caps["email"]["advanced"] is False
    assert caps["email_agent_tools"]["label"] == "Agent email tools for users" and caps["email_agent_tools"]["advanced"] is True
    assert caps["email_recovery"]["label"] == "Sign-in by email" and caps["email_recovery"]["advanced"] is True
    assert c.get("/api/gateway/admin/email/capabilities", headers=gateway["alice"]).status_code == 403
    assert c.put("/api/gateway/admin/email/capabilities", headers=gateway["alice"], json={"email_agent_tools": True}).status_code == 403

    # Available by default, but the user's own switch is OFF until they opt in.
    assert c.put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(ALICE, imap, smtp)).status_code == 200
    at = c.get("/api/gateway/me/email", headers=gateway["alice"]).json()["agent_tools"]
    assert at["on"] is False and at["available"] is True and at["unavailable_reason"] is None and at["active"] is False

    # The admin makes them unavailable gateway-wide.
    assert c.put("/api/gateway/admin/email/capabilities", headers=ADMIN, json={"email_agent_tools": False}).status_code == 200
    at = c.get("/api/gateway/me/email", headers=gateway["alice"]).json()["agent_tools"]
    assert at["on"] is False and at["available"] is False and at["active"] is False
    assert at["unavailable_reason"] == "Your admin turned agent email tools off."
    # The toolset listing names the same case with the runtime's typed reason, not "turned off".
    from abstractruntime.integrations.abstractcore.default_tools import EMAIL_OFF_REASONS

    from abstractgateway.mail.accounts import agent_tools_off_reason

    assert agent_tools_off_reason(plane_of("alice")) == "not_available"
    items = c.get("/api/gateway/discovery/tools", headers=gateway["alice"]).json()["items"]
    gates = {t["enable_gate"] for t in items if t["name"] in ("send_email", "list_email_folders")}
    assert gates == {EMAIL_OFF_REASONS["not_available"]}
    r = c.put("/api/gateway/me/email/agent-tools", headers=gateway["alice"], json={"enabled": True})
    assert r.status_code == 409 and r.json()["detail"]["reason_code"] == "email_disabled"
    assert "admin" in r.json()["detail"]["fix"]

    # Gateway-wide default on: available to everyone; a per-user override wins.
    r = c.put("/api/gateway/admin/email/capabilities", headers=ADMIN, json={"email_agent_tools": True})
    assert r.status_code == 200
    assert agent_tools_off_reason(plane_of("alice")) == "agent_tools_off"
    assert c.put("/api/gateway/me/email/agent-tools", headers=gateway["alice"], json={"enabled": True}).json()["agent_tools"]["active"] is True
    assert agent_tools_off_reason(plane_of("alice")) is None
    # Available but email itself turned off for the user by the administrator: "admin_disabled".
    assert c.put("/api/gateway/admin/users/alice/email", headers=ADMIN, json={"enabled": False}).status_code == 200
    assert agent_tools_off_reason(plane_of("alice")) == "admin_disabled"
    assert c.put("/api/gateway/admin/users/alice/email", headers=ADMIN, json={"enabled": True}).status_code == 200
    assert agent_tools_off_reason(plane_of("alice")) is None
    r = c.put("/api/gateway/admin/users/alice/email", headers=ADMIN, json={"agent_tools": False})
    assert r.json()["capabilities"]["email_agent_tools"] == {"value": False, "source": "user"}
    at = c.get("/api/gateway/me/email", headers=gateway["alice"]).json()["agent_tools"]
    assert at["enabled"] is True and at["available"] is False and at["active"] is False
    assert agent_tools_off_reason(plane_of("alice")) == "not_available"
    # Enforced where toolsets are listed and where tools run, not only in the status view.
    items = c.get("/api/gateway/discovery/tools", headers=gateway["alice"]).json()["items"]
    assert {t["name"]: t["enabled"] for t in items if t["name"] == "send_email"} == {"send_email": False}
    from abstractcore.comms.email import EmailDisabled
    from abstractruntime.email import EmailBinding

    from abstractgateway.mail.runtime_wiring import make_email_resolver

    with pytest.raises(EmailDisabled):
        make_email_resolver(plane_of("alice"))(EmailBinding(account_ref="default:alice:default"))
    r = c.put("/api/gateway/admin/users/alice/email", headers=ADMIN, json={"inherit": ["email_agent_tools"]})
    assert r.json()["capabilities"]["email_agent_tools"] == {"value": True, "source": "gateway"}
    rows = {u["user_id"]: u for u in c.get("/api/gateway/admin/users", headers=ADMIN).json()["users"]}
    assert rows["alice"]["email_account"]["state"] == "connected"


def test_send_limits_defaults_user_values_and_a_stored_legacy_value(gateway, imap, smtp) -> None:
    """Defaults 100/1000; a user's 20/100 is kept as theirs; a 20/100 an older version stored at
    connect (no set_by marker) is the old default and follows the new defaults; any other unmarked
    value is kept as legacy."""

    c = gateway["client"]
    r = c.put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(ALICE, imap, smtp))
    assert r.status_code == 200, r.text
    lim = r.json()["limits"]
    assert (lim["per_hour"], lim["per_day"], lim["source"]) == (100, 1000, "default")

    r = c.put("/api/gateway/me/email/limits", headers=gateway["alice"], json={"per_hour": 20, "per_day": 100})
    assert r.status_code == 200, r.text
    lim = r.json()["limits"]
    assert (lim["per_hour"], lim["per_day"], lim["source"]) == (20, 100, "user")
    stored = json.loads(plane_of("alice").account_config_file.read_text())["email"]["limits"]
    assert stored == {"per_hour": 20, "per_day": 100, "set_by": "user"}

    # Bob's mailbox as AbstractCore 2.21 left it: the then-defaults stored without a marker.
    r = c.put("/api/gateway/me/email", headers=gateway["bob"], json=connect_body(BOB, imap, smtp))
    assert r.status_code == 200, r.text
    path = plane_of("bob").account_config_file
    doc = json.loads(path.read_text())
    doc["email"]["limits"] = {"per_hour": 20, "per_day": 100}
    path.write_text(json.dumps(doc))
    lim = c.get("/api/gateway/me/email", headers=gateway["bob"]).json()["limits"]
    assert (lim["per_hour"], lim["per_day"], lim["source"]) == (100, 1000, "default")
    doc["email"]["limits"] = {"per_hour": 30, "per_day": 300}
    path.write_text(json.dumps(doc))
    lim = c.get("/api/gateway/me/email", headers=gateway["bob"]).json()["limits"]
    assert (lim["per_hour"], lim["per_day"], lim["source"]) == (30, 300, "legacy")
