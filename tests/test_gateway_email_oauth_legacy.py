"""OAuth2 sign-in per user (device flow against the hermetic OAuth server), the admin's
bring-your-own OAuth clients (secret never returned), the one-time import of the retired
ABSTRACT_EMAIL_* configuration into the admin's account, the legacy /email/* aliases on the
caller's own account, the maintenance notifier through the admin's outbox, and the runtime
wiring (binding + resolver answering only for the plane's own account)."""

from __future__ import annotations

import pytest

from email_fixtures import *  # noqa: F401,F403 - fixtures
from email_fixtures import ADMIN, ADMIN_ADDR, ALICE, PASSWORDS, connect_body, message, plane_of, smtp_bodies

pytestmark = pytest.mark.integration


def _oauth_body(oauth_server, imap, smtp, ca) -> dict:
    return {
        "address": ALICE,
        "provider": "custom",
        "client_id": oauth_server.client_id,
        "client_secret": oauth_server.client_secret,
        "flow": "device",
        "token_endpoint": f"{oauth_server.base_url}/token",
        "device_authorization_endpoint": f"{oauth_server.base_url}/device",
        "scopes": ["mail"],
        "imap": {"host": "localhost", "port": imap.port, "security": "ssl"},
        "smtp": {"host": "localhost", "port": smtp.port, "security": "starttls"},
        "ca_file": str(ca.ca_pem),
    }


def test_custom_oauth_provider_and_ca_file_are_admin_settings(gateway, imap, smtp, oauth_server, ca) -> None:
    r = gateway["client"].post("/api/gateway/me/email/oauth/start", headers=gateway["alice"], json=_oauth_body(oauth_server, imap, smtp, ca))
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["reason_code"] == "email_oauth_override_refused"


def test_device_flow_connects_and_flows_are_per_user(gateway, imap, smtp, oauth_server, ca, monkeypatch) -> None:
    from abstractgateway.users import GatewayUserRegistry

    # The OAuth test user is the gateway's admin here (custom endpoints are an admin setting).
    GatewayUserRegistry().create_user(user_id="admin", roles=["admin", "user"], email=ALICE)
    c = gateway["client"]
    r = c.post("/api/gateway/me/email/oauth/start", headers=ADMIN, json=_oauth_body(oauth_server, imap, smtp, ca))
    assert r.status_code == 200, r.text
    flow = r.json()
    assert flow["flow"] == "device" and flow["user_code"] == "WDJB-MJHT" and "device_code" not in flow

    pending = c.post("/api/gateway/me/email/oauth/poll", headers=ADMIN, json={"flow_id": flow["flow_id"]})
    assert pending.status_code == 200 and pending.json()["pending"] is True

    # Another user cannot finish (or even see) this flow.
    stolen = c.post("/api/gateway/me/email/oauth/finish", headers=gateway["bob"], json={"flow_id": flow["flow_id"]})
    assert stolen.status_code == 422 and stolen.json()["detail"]["reason_code"] == "email_oauth_failed"
    assert c.post("/api/gateway/me/email/oauth/cancel", headers=gateway["bob"], json={"flow_id": flow["flow_id"]}).json()["cancelled"] is False

    oauth_server.approve_all_devices()
    done = c.post("/api/gateway/me/email/oauth/finish", headers=ADMIN, json={"flow_id": flow["flow_id"], "wait_s": 5})
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["configured"] is True and body["auth_kind"] == "oauth2" and body["oauth"]["client_source"] == "own"
    assert oauth_server.client_secret not in done.text
    assert c.post("/api/gateway/me/email/test", headers=ADMIN).json()["ok"] is True


def test_admin_oauth_clients_secret_never_returned(gateway) -> None:
    c = gateway["client"]
    assert c.put("/api/gateway/admin/email/oauth-clients/google", headers=gateway["alice"], json={"client_id": "x"}).status_code == 403
    r = c.put("/api/gateway/admin/email/oauth-clients/microsoft", headers=ADMIN, json={"client_id": "cid-123", "client_secret": "csecret-XYZ", "tenant": "common"})
    assert r.status_code == 200, r.text
    ms = r.json()["providers"]["microsoft"]
    assert ms == {"configured": True, "client_id": "cid-123", "client_secret_set": True, "tenant": "common", "builtin_available": False}
    assert "csecret-XYZ" not in r.text
    # Users see which providers have a client (no secret), and the secret is sealed at rest.
    view = c.get("/api/gateway/me/email/oauth/clients", headers=gateway["alice"])
    assert view.status_code == 200 and "csecret-XYZ" not in view.text
    for p in (gateway["data_dir"] / "email" / "oauth_clients").rglob("*"):
        if p.is_file():
            assert b"csecret-XYZ" not in p.read_bytes()
    # Same client id without a secret keeps the stored secret; empty id clears the provider.
    r = c.put("/api/gateway/admin/email/oauth-clients/microsoft", headers=ADMIN, json={"client_id": "cid-123"})
    assert r.json()["providers"]["microsoft"]["client_secret_set"] is True
    from abstractgateway.mail.accounts import choose_oauth_client

    chosen = choose_oauth_client("microsoft")
    assert chosen["client_id"] == "cid-123" and chosen["client_secret"] == "csecret-XYZ"
    r = c.put("/api/gateway/admin/email/oauth-clients/microsoft", headers=ADMIN, json={"client_id": ""})
    assert r.json()["providers"]["microsoft"]["configured"] is False
    from abstractcore.comms.email import EmailInvalidSettings

    with pytest.raises(EmailInvalidSettings) as exc:
        choose_oauth_client("microsoft")
    assert "administrator" in exc.value.fix and "app password" in exc.value.fix


def test_legacy_env_is_imported_once_into_the_admin_account_then_ignored(gateway, imap, smtp) -> None:
    from abstractgateway.mail.accounts import admin_plane, import_legacy_env_once, public_status

    env = {
        "ABSTRACT_EMAIL_IMAP_HOST": "localhost",
        "ABSTRACT_EMAIL_IMAP_PORT": str(imap.port),
        "ABSTRACT_EMAIL_IMAP_USERNAME": ADMIN_ADDR,
        "ABSTRACT_EMAIL_SMTP_HOST": "localhost",
        "ABSTRACT_EMAIL_SMTP_PORT": str(smtp.port),
        "ABSTRACT_EMAIL_SMTP_USERNAME": ADMIN_ADDR,
        "ABSTRACT_EMAIL_FROM": ADMIN_ADDR,
        "ABSTRACT_EMAIL_IMAP_PASSWORD_ENV_VAR": "LEGACY_MAIL_PW",
        "ABSTRACT_EMAIL_SMTP_PASSWORD_ENV_VAR": "LEGACY_MAIL_PW",
        "LEGACY_MAIL_PW": PASSWORDS[ADMIN_ADDR],
        "ABSTRACT_EMAIL_BRIDGE": "1",
    }
    notes = import_legacy_env_once(env)
    assert any("Imported" in n and "My email" in n for n in notes)
    assert any(n.startswith("ABSTRACT_EMAIL_BRIDGE is set but ignored") for n in notes)
    assert any(n.startswith("ABSTRACT_EMAIL_IMAP_HOST is set but ignored") and "My email" in n for n in notes)
    status = public_status(admin_plane())
    assert status["configured"] is True and status["address"] == ADMIN_ADDR and status["secret_set"] is True
    assert status["legacy_import"]["source"] == "environment"

    # Second boot: no re-import, only the "ignored" notices.
    notes2 = import_legacy_env_once({**env, "ABSTRACT_EMAIL_FROM": "someone-else@example.test"})
    assert not any("Imported" in n for n in notes2)
    assert public_status(admin_plane())["address"] == ADMIN_ADDR

    # The admin sees the notices in GET /me/email; users never do.
    assert gateway["client"].get("/api/gateway/me/email", headers=ADMIN).json()["notices"]
    assert "notices" not in gateway["client"].get("/api/gateway/me/email", headers=gateway["alice"]).json()


def test_legacy_aliases_act_on_the_calling_admins_own_account(gateway, imap, smtp) -> None:
    c = gateway["client"]
    r = c.put("/api/gateway/me/email", headers=ADMIN, json=connect_body(ADMIN_ADDR, imap, smtp))
    assert r.status_code == 200, r.text
    imap.add_message("INBOX", message("for the admin", to=ADMIN_ADDR))
    accounts = c.get("/api/gateway/email/accounts", headers=ADMIN).json()
    assert [a["email"] for a in accounts["accounts"]] == [ADMIN_ADDR]
    listed = c.get("/api/gateway/email/messages", headers=ADMIN).json()
    assert [m["subject"] for m in listed["messages"]] == ["for the admin"]
    uid = listed["messages"][0]["uid"]
    one = c.get(f"/api/gateway/email/messages/{uid}", headers=ADMIN).json()
    assert one["subject"] == "for the admin" and one["content_trust"] == "untrusted"

    # Sending goes through the recipient policy (default: only the admin's own address).
    ok = c.post("/api/gateway/email/send", headers=ADMIN, json={"to": ADMIN_ADDR, "subject": "note", "body_text": "hi"})
    assert ok.status_code == 200, ok.text
    refused = c.post("/api/gateway/email/send", headers=ADMIN, json={"to": "stranger@example.test", "subject": "x", "body_text": "y"})
    assert refused.status_code == 400 and refused.json()["detail"]["reason_code"] == "email_policy_refused"
    assert len(smtp.messages) == 1 and "smtp" not in ok.json()


def test_maintenance_notices_go_through_the_admin_outbox(gateway, imap, smtp) -> None:
    from abstractgateway.maintenance.notifier import send_email_notification

    ok, err = send_email_notification(subject="Triage pending (2)", body_text="Two reports wait.")
    assert ok is False and "My email" in str(err)

    gateway["client"].put("/api/gateway/me/email", headers=ADMIN, json=connect_body(ADMIN_ADDR, imap, smtp))
    ok, err = send_email_notification(subject="[AbstractFramework] Triage pending (2)", body_text="Two reports wait.")
    assert ok is True and err is None
    ok2, _ = send_email_notification(subject="[AbstractFramework] Triage pending (2)", body_text="Two reports wait.")
    mails = smtp_bodies(smtp)
    assert len(mails) == 1 and mails[0]["to"] == [ADMIN_ADDR]
    assert mails[0]["subject"] == "[AbstractFramework] Triage pending (2)"
    assert ok2 is True  # the same notice is recorded once and not sent again

    # Once per UTC day, not once ever: the same condition still true tomorrow is reported again.
    import abstractgateway.maintenance.notifier as notifier

    today = notifier._notice_bucket()
    notifier_bucket = lambda: today + 1  # noqa: E731
    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(notifier, "_notice_bucket", notifier_bucket)
        ok3, err3 = send_email_notification(subject="[AbstractFramework] Triage pending (2)", body_text="Two reports wait.")
        assert ok3 is True and err3 is None
        send_email_notification(subject="[AbstractFramework] Triage pending (2)", body_text="Two reports wait.")
    assert len(smtp_bodies(smtp)) == 2


def test_runtime_wiring_binds_and_resolves_only_the_planes_account(gateway, imap, smtp) -> None:
    from abstractcore.comms.email import EmailNotConfigured
    from abstractruntime import Runtime
    from abstractruntime.email import EmailBinding
    from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore

    from abstractgateway.mail.runtime_wiring import current_binding, make_email_resolver, wire_runtime_email

    plane = plane_of("alice")
    runtime = Runtime(run_store=InMemoryRunStore(), ledger_store=InMemoryLedgerStore())
    wire_runtime_email(runtime, plane)
    assert runtime.email_binding is None and runtime.event_inbox is not None

    gateway["client"].put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(ALICE, imap, smtp))
    # The binding follows the ACCOUNT (connected + enabled + allowed), not the agent-tools choice.
    assert current_binding(plane) == EmailBinding(account_ref="default:alice:default", address=ALICE)
    from abstractcore.comms.email import EmailDisabled

    resolve = make_email_resolver(plane)
    # Agent/workflow tool calls need "Agent email tools" (not available by default) ...
    with pytest.raises(EmailDisabled) as off:
        resolve(EmailBinding(account_ref="default:alice:default"))
    assert "administrator" in off.value.fix
    # ... the runtime's own send-email action does not.
    assert resolve(EmailBinding(account_ref="default:alice:default"), use="action").account.address == ALICE
    gateway["client"].put("/api/gateway/admin/users/alice/email", headers=ADMIN, json={"agent_tools": True})
    with pytest.raises(EmailDisabled) as off2:
        resolve(EmailBinding(account_ref="default:alice:default"))
    assert "Agent email tools" in off2.value.fix
    r = gateway["client"].put("/api/gateway/me/email/agent-tools", headers=gateway["alice"], json={"enabled": True})
    assert r.status_code == 200 and r.json()["agent_tools"] == {"enabled": True, "available": True, "active": True, "reason": ""}
    wire_runtime_email(runtime, plane)
    assert runtime.email_binding.account_ref == "default:alice:default"

    assert resolve(EmailBinding(account_ref="default:alice:default")).account.address == ALICE
    with pytest.raises(EmailNotConfigured):
        resolve(EmailBinding(account_ref="default:bob:default"))

    gateway["client"].put("/api/gateway/admin/users/alice/email", headers=ADMIN, json={"enabled": False})
    assert current_binding(plane) is None
    for use in ("agent_tool", "action"):
        with pytest.raises(EmailDisabled):
            resolve(EmailBinding(account_ref="default:alice:default"), use=use)
    status = gateway["client"].get("/api/gateway/me/email", headers=gateway["alice"]).json()["agent_tools"]
    assert status["enabled"] is True and status["active"] is False and "administrator" in status["reason"]


def test_agent_email_tools_are_per_user_and_off_by_default(gateway, imap, smtp) -> None:
    c = gateway["client"]

    def email_rows(headers):
        items = c.get("/api/gateway/discovery/tools", headers=headers).json()["items"]
        rows = [(t["name"], t["enabled"]) for t in items if t["name"] in ("send_email", "read_email", "search_emails")]
        assert len(rows) == len({n for n, _ in rows}), f"an email tool is listed twice: {rows}"
        return dict(rows)

    assert c.get("/api/gateway/me/email", headers=gateway["alice"]).json()["agent_tools"]["enabled"] is False
    r = c.put("/api/gateway/me/email/agent-tools", headers=gateway["alice"], json={"enabled": True})
    assert r.status_code == 409  # not available (the administrator decides)
    c.put("/api/gateway/admin/users/alice/email", headers=ADMIN, json={"agent_tools": True})
    r = c.put("/api/gateway/me/email/agent-tools", headers=gateway["alice"], json={"enabled": True})
    assert r.json()["agent_tools"]["active"] is False and r.json()["agent_tools"]["reason"]  # no account yet
    assert email_rows(gateway["alice"]) == {"send_email": False, "read_email": False, "search_emails": False}
    c.put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(ALICE, imap, smtp))
    assert email_rows(gateway["alice"]) == {"send_email": True, "read_email": True, "search_emails": True}
    # Bob (no account, toggle off) never sees Alice's state.
    assert email_rows(gateway["bob"]) == {"send_email": False, "read_email": False, "search_emails": False}
    c.put("/api/gateway/me/email/agent-tools", headers=gateway["alice"], json={"enabled": False})
    assert email_rows(gateway["alice"]) == {"send_email": False, "read_email": False, "search_emails": False}


def test_core_local_account_is_imported_once_for_the_admin(gateway, imap, smtp, tmp_path, monkeypatch) -> None:
    from abstractcore.comms.email import EmailAccount, EmailAccountStore, EmailSecret, ImapSettings, SmtpSettings

    from abstractgateway.mail.accounts import admin_plane, import_core_account_once, public_status

    core_file = tmp_path / "core" / "abstractcore.json"
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(core_file))
    account = EmailAccount.build(
        address=ADMIN_ADDR,
        imap=ImapSettings.build("localhost", port=imap.port, security="ssl"),
        smtp=SmtpSettings.build("localhost", port=smtp.port, security="starttls"),
    )
    EmailAccountStore(config_file=core_file).connect(account, EmailSecret(PASSWORDS[ADMIN_ADDR]), test=False)
    notes = import_core_account_once()
    assert any("AbstractCore's local email account" in n for n in notes)
    pub = public_status(admin_plane())
    assert pub["configured"] is True and pub["address"] == ADMIN_ADDR and pub["secret_set"] is True
    assert "configured separately" in pub["store"]["label"]
    assert gateway["client"].post("/api/gateway/me/email/test", headers=ADMIN).json()["ok"] is True
    assert import_core_account_once() == []  # once
