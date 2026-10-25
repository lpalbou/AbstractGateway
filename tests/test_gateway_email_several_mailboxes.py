"""Several mailboxes per user, read-only at minimum (framework backlog 1044).

`account_id` is a parameter where the plane assumed `default`: a user (alice) holds her primary
mailbox AND a further read-only one (`work`, IMAP only). Both are listed with `can_read` /
`can_send`, both are watched (each its own cursor and inbox stream
`email:<tenant>:<user>:<account_id>:<folder>`), a watcher reads a mailbox only for automations
bound to it and wakes only those, a run bound to `work` resolves `work` (and its sends are refused:
read-only), the host refuses an `email.received@1` binding on a mailbox the owner does not have, and
the single-mailbox layout is unchanged (the primary keeps id `default` and its files). Hermetic:
AbstractCore's fake IMAP/SMTP servers, a scratch data folder."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from email_fixtures import *  # noqa: F401,F403 - fixtures
from email_fixtures import ALICE, BOB, PASSWORDS, connect_body, message, plane_of

pytestmark = pytest.mark.integration

WORK = "work"


def _connect_primary(gateway, imap, smtp) -> None:
    r = gateway["client"].put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(ALICE, imap, smtp))
    assert r.status_code == 200, r.text


def _connect_work(gateway, imap_bob, **extra):
    body = {"address": BOB, "password": PASSWORDS[BOB], "imap": {"host": "localhost", "port": imap_bob.port, "security": "ssl"}, **extra}
    return gateway["client"].put(f"/api/gateway/me/email/accounts/{WORK}", headers=gateway["alice"], json=body)


def test_one_user_lists_a_primary_and_a_read_only_mailbox(gateway, imap, imap_bob, smtp) -> None:
    c = gateway["client"]
    _connect_primary(gateway, imap, smtp)
    r = _connect_work(gateway, imap_bob)
    assert r.status_code == 200, r.text
    row = r.json()
    assert (row["account_id"], row["primary"], row["address"], row["can_read"], row["can_send"]) == (WORK, False, BOB, True, False)
    # Read only is the person's choice, not a fault: the mailbox is "connected", its access says the rest.
    assert row["account_ref"] == "default:alice:work" and row["mailbox"]["state"] == "connected" and row["access"] == "read"

    items = c.get("/api/gateway/me/email/accounts", headers=gateway["alice"]).json()["items"]
    assert [(i["account_id"], i["address"], i["can_read"], i["can_send"]) for i in items] == [
        ("default", ALICE, True, True), (WORK, BOB, True, False)]
    # A read-only mailbox cannot be changed either (AbstractCore's can_write); the primary is read and write.
    assert [(i["account_id"], i["can_write"], i["access"]) for i in items] == [("default", True, "write"), (WORK, False, "read")]
    me = c.get("/api/gateway/me/email", headers=gateway["alice"]).json()
    assert [a["account_id"] for a in me["accounts"]] == ["default", WORK]
    assert me["address"] == ALICE and me["account_ref"] == "default:alice:default"  # the primary is unchanged
    assert c.get(f"/api/gateway/me/email/accounts/{WORK}", headers=gateway["alice"]).json()["address"] == BOB

    # Bob sees none of it (his own plane: a primary not connected, nothing else).
    bob = c.get("/api/gateway/me/email/accounts", headers=gateway["bob"]).json()["items"]
    assert [(i["account_id"], i["configured"]) for i in bob] == [("default", False)]
    assert c.get(f"/api/gateway/me/email/accounts/{WORK}", headers=gateway["bob"]).status_code == 404

    # Stored under its own folder; the JSON holds no secret.
    plane = plane_of("alice")
    cfg = plane.email_dir / "accounts" / WORK / "abstractcore.json"
    assert cfg.is_file() and PASSWORDS[BOB] not in cfg.read_text()
    # Connecting a further mailbox never changes the user's email address.
    assert me["email_address"] == ALICE


def test_the_single_mailbox_layout_is_unchanged(gateway, imap, smtp) -> None:
    _connect_primary(gateway, imap, smtp)
    plane = plane_of("alice")
    assert plane.account_id == "default" and plane.account_ref == "default:alice:default"
    assert plane.account_config_file == plane.email_dir / "account" / "abstractcore.json" and plane.account_config_file.is_file()
    from abstractgateway.mail.watcher import _state_path

    assert _state_path(plane) == plane.email_dir / "watcher.json"
    assert _state_path(plane.for_account(WORK)) == plane.email_dir / "accounts" / WORK / "watcher.json"
    from abstractgateway.mail.accounts import mailbox_ids

    assert mailbox_ids(plane) == ["default"]


def test_write_true_stores_smtp_and_bad_ids_are_refused(gateway, imap_bob, smtp) -> None:
    c = gateway["client"]
    r = _connect_work(gateway, imap_bob, write=True, smtp={"host": "localhost", "port": smtp.port, "security": "starttls"})
    assert r.status_code == 200, r.text
    assert (r.json()["can_send"], r.json()["can_write"], r.json()["access"], r.json()["mailbox"]["state"]) == (True, True, "write", "connected")
    # `send` is still accepted as the older name of `write`.
    r = c.put("/api/gateway/me/email/accounts/older", headers=gateway["alice"],
              json={"address": BOB, "password": PASSWORDS[BOB], "imap": {"host": "localhost", "port": imap_bob.port, "security": "ssl"},
                    "send": True, "smtp": {"host": "localhost", "port": smtp.port, "security": "starttls"}})
    assert r.status_code == 200 and r.json()["access"] == "write" and r.json()["can_send"] is True, r.text
    # Read only (the default) never stores an outgoing server, even one given.
    r = c.put("/api/gateway/me/email/accounts/plain", headers=gateway["alice"],
              json={"address": BOB, "password": PASSWORDS[BOB], "imap": {"host": "localhost", "port": imap_bob.port, "security": "ssl"},
                    "smtp": {"host": "localhost", "port": smtp.port, "security": "starttls"}})
    assert (r.json()["access"], r.json()["can_send"]) == ("read", False)
    bad = c.put("/api/gateway/me/email/accounts/Bad..Id", headers=gateway["alice"],
                json={"address": BOB, "password": "x", "imap": {"host": "localhost", "port": imap_bob.port}})
    assert bad.status_code == 400 and bad.json()["detail"]["reason_code"] == "email_invalid_settings"


def test_both_mailboxes_are_watched_each_into_its_own_stream(gateway, imap, imap_bob, smtp) -> None:
    from abstractgateway.mail.accounts import mailbox_planes
    from abstractgateway.mail.watcher import MailWatcher

    _connect_primary(gateway, imap, smtp)
    assert _connect_work(gateway, imap_bob).status_code == 200
    watchers = [MailWatcher(p, has_consumers=lambda: True) for p in mailbox_planes(plane_of("alice"))]
    assert [w.plane.account_id for w in watchers] == ["default", WORK]
    assert [w.poll_once()["new"] for w in watchers] == [0, 0]  # baselines
    imap.add_message("INBOX", message("to the primary"))
    imap_bob.add_message("INBOX", message("to work", to=BOB))
    imap_bob.add_message("INBOX", message("to work again", to=BOB))
    assert [w.poll_once(force=True)["new"] for w in watchers] == [1, 2]
    streams = {w.plane.account_id: [r["payload"]["subject"] for r in w.inbox.read(stream=w.feeder.stream())] for w in watchers}
    assert streams == {"default": ["to the primary"], WORK: ["to work", "to work again"]}
    assert watchers[1].feeder.stream() == "email:default:alice:work:INBOX"
    status = gateway["client"].get(f"/api/gateway/me/email/accounts/{WORK}", headers=gateway["alice"]).json()["watcher"]
    assert status["state"] == "watching" and status["received"] == 2


def _controller(account):
    """An idle email automation bound to `account` (the controller's contract shapes)."""
    from abstractruntime.automations.models import wake_wait_key
    from abstractruntime.core.models import RunStatus, WaitReason, WaitState

    from automations_fixtures import controller_run

    run = controller_run(title=f"On mail to {account}")
    run.vars["_meta"]["automation"]["trigger"] = {"binding_id": "b", "source_id": "email.received", "source_version": 1,
                                                  "config": {"account": account} if account else {}}
    run.status = RunStatus.WAITING
    run.waiting = WaitState(reason=WaitReason.EVENT, wait_key=wake_wait_key(run.run_id))
    return run


def test_the_worker_reads_a_mailbox_only_for_its_automations_and_wakes_only_them(gateway, imap, imap_bob, smtp) -> None:
    from abstractruntime import Runtime
    from abstractruntime.core.models import RunStatus
    from abstractruntime.email import JsonFileEventInbox
    from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore

    from abstractgateway.mail.worker import EmailWorker

    _connect_primary(gateway, imap, smtp)
    assert _connect_work(gateway, imap_bob).status_code == 200
    plane = plane_of("alice")
    rt = Runtime(run_store=InMemoryRunStore(), ledger_store=InMemoryLedgerStore())
    rt.set_event_inbox(JsonFileEventInbox(plane.runtime_data_dir / "event_inbox"))
    on_work = _controller(WORK)
    rt.run_store.save(on_work)
    worker = EmailWorker(SimpleNamespace(host=SimpleNamespace(runtime=rt, run_store=rt.run_store), runner=None), plane)

    first = {w.plane.account_id: w.poll_once(force=True) for w in worker._watchers()}
    assert first["default"]["state"].startswith("idle")  # nobody watches the primary
    assert first[WORK]["state"] == "watching" and first[WORK]["new"] == 0  # baseline
    imap.add_message("INBOX", message("primary mail"))
    imap_bob.add_message("INBOX", message("work mail", to=BOB))
    second = {w.plane.account_id: w.poll_once(force=True) for w in worker._watchers()}
    assert second["default"]["new"] == 0 and second[WORK]["new"] == 1
    assert rt.run_store.load(on_work.run_id).status != RunStatus.WAITING  # woken by its own mailbox's mail

    # An automation on the primary is not woken by work mail.
    on_primary = _controller(None)
    rt.run_store.save(on_primary)
    imap_bob.add_message("INBOX", message("more work mail", to=BOB))
    worker._watcher(plane.for_account(WORK)).poll_once(force=True)
    assert rt.run_store.load(on_primary.run_id).status == RunStatus.WAITING


def test_a_run_bound_to_the_work_mailbox_resolves_it_and_cannot_send(gateway, imap, imap_bob, smtp) -> None:
    from abstractcore.comms.email.context import guarded_send
    from abstractcore.comms.email import EmailMailboxReadOnly, EmailNotConfigured
    from abstractruntime.email import EmailBinding, email_binding_for_trigger

    from abstractgateway.mail.core_mail import OutgoingMessage
    from abstractgateway.mail.runtime_wiring import current_binding, make_email_resolver

    _connect_primary(gateway, imap, smtp)
    assert _connect_work(gateway, imap_bob).status_code == 200
    plane = plane_of("alice")
    resolve = make_email_resolver(plane)
    primary = current_binding(plane.for_account(WORK))  # runs are bound to the PRIMARY whatever plane value
    assert primary.account_ref == "default:alice:default"
    binding = email_binding_for_trigger(primary, {"source_id": "email.received", "source_version": 1, "config": {"account": WORK}})
    ctx = resolve(binding, use="action")
    assert ctx.account.address == BOB and ctx.account.can_read and not ctx.account.can_send
    with pytest.raises(EmailMailboxReadOnly, match="read-only"):
        guarded_send(ctx, OutgoingMessage(to=(ALICE,), subject="hi", text="x"))
    assert resolve(primary, use="action").account.address == ALICE
    for foreign in ("default:alice:nope", "default:bob:default", "default:alice:../x"):
        with pytest.raises(EmailNotConfigured):
            resolve(EmailBinding(foreign), use="action")


def test_the_host_refuses_a_binding_on_a_mailbox_the_owner_does_not_have(gateway, imap, imap_bob, smtp) -> None:
    from abstractgateway.automation_errors import AutomationError
    from abstractgateway.routes.automations import _check_email_account
    from abstractgateway.service import _config_for_principal

    _connect_primary(gateway, imap, smtp)
    assert _connect_work(gateway, imap_bob).status_code == 200
    from abstractgateway.users import GatewayUserRegistry

    svc = SimpleNamespace(config=_config_for_principal(GatewayUserRegistry().get_user("alice").to_principal()))
    email = lambda cfg: {"source_id": "email.received", "source_version": 1, "config": cfg}  # noqa: E731
    for ok in ({}, {"account": "self"}, {"account": "default"}, {"account": WORK}):
        _check_email_account(svc, email(ok), field="trigger.config.account")
    _check_email_account(svc, {"source_id": "schedule", "source_version": 2, "config": {"account": "nope"}}, field="f")
    with pytest.raises(AutomationError) as exc:
        _check_email_account(svc, email({"account": "nope"}), field="trigger.config.account")
    detail = exc.value.detail
    assert exc.value.status_code == 422 and detail["reason_code"] == "email_account_not_found" and detail["field"] == "trigger.config.account"
    # Bob cannot bind Alice's mailbox.
    bob_svc = SimpleNamespace(config=_config_for_principal(GatewayUserRegistry().get_user("bob").to_principal()))
    with pytest.raises(AutomationError):
        _check_email_account(bob_svc, email({"account": WORK}), field="trigger.config.account")


def test_disconnecting_the_work_mailbox_removes_it_from_the_list(gateway, imap, imap_bob, smtp) -> None:
    c = gateway["client"]
    _connect_primary(gateway, imap, smtp)
    assert _connect_work(gateway, imap_bob).status_code == 200
    assert c.post(f"/api/gateway/me/email/accounts/{WORK}/test", headers=gateway["alice"]).json()["imap"]["ok"] is True
    off = c.put(f"/api/gateway/me/email/accounts/{WORK}/enabled", headers=gateway["alice"], json={"enabled": False}).json()
    assert off["enabled"] is False and off["mailbox"]["state"] == "paused"
    r = c.delete(f"/api/gateway/me/email/accounts/{WORK}", headers=gateway["alice"])
    assert r.status_code == 200 and r.json()["disconnected"] is True
    assert [a["account_id"] for a in r.json()["accounts"]] == ["default"]
    assert c.get(f"/api/gateway/me/email/accounts/{WORK}", headers=gateway["alice"]).status_code == 404
    # The primary is untouched.
    assert c.get("/api/gateway/me/email", headers=gateway["alice"]).json()["mailbox"]["state"] == "connected"
    audit = [json.loads(x) for x in (gateway["data_dir"] / "audit_log.jsonl").read_text().splitlines() if '"event"' in x]
    assert any(e["event"] == "email.disconnected" and e.get("account_id") == WORK for e in audit)


def _oauth_body(oauth_server, imap, ca, **extra) -> dict:
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
        "ca_file": str(ca.ca_pem),
        **extra,
    }


def test_a_further_mailbox_signs_in_with_oauth_read_only(gateway, imap_bob, smtp, oauth_server, ca) -> None:
    """Gmail refuses account passwords over IMAP: a further mailbox signs in with OAuth2 (the
    primary's flow), its tokens sealed in its own store, IMAP XOAUTH2 only unless `send`."""
    from email_fixtures import ADMIN

    from abstractgateway.mail.accounts import default_plane
    from abstractgateway.users import GatewayUserRegistry

    GatewayUserRegistry().create_user(user_id="admin", roles=["admin", "user"], email=ALICE)  # custom endpoints: admin only
    c = gateway["client"]
    base = f"/api/gateway/me/email/accounts/{WORK}/oauth"
    # An SMTP leg in the body is dropped: without `send` the mailbox is read-only.
    r = c.post(f"{base}/start", headers=ADMIN, json=_oauth_body(oauth_server, imap_bob, ca, smtp={"host": "localhost", "port": smtp.port, "security": "starttls"}))
    assert r.status_code == 200, r.text
    flow = r.json()
    assert flow["flow"] == "device" and "device_code" not in flow
    assert c.post(f"{base}/poll", headers=ADMIN, json={"flow_id": flow["flow_id"]}).json()["pending"] is True
    # The flow belongs to this mailbox of this user: not the primary's route, not another user.
    other_box = c.post("/api/gateway/me/email/oauth/finish", headers=ADMIN, json={"flow_id": flow["flow_id"]})
    assert other_box.status_code == 422 and other_box.json()["detail"]["reason_code"] == "email_oauth_failed"
    other_user = c.post(f"{base}/finish", headers=gateway["bob"], json={"flow_id": flow["flow_id"]})
    assert other_user.status_code == 422

    oauth_server.approve_all_devices()
    done = c.post(f"{base}/finish", headers=ADMIN, json={"flow_id": flow["flow_id"], "wait_s": 5})
    assert done.status_code == 200, done.text
    row = done.json()
    assert (row["ok"], row["account_id"], row["address"], row["can_read"], row["can_send"]) == (True, WORK, ALICE, True, False)
    assert oauth_server.client_secret not in done.text
    items = c.get("/api/gateway/me/email/accounts", headers=ADMIN).json()["items"]
    assert [(i["account_id"], i["configured"]) for i in items] == [("default", False), (WORK, True)]  # the primary untouched
    assert c.post(f"/api/gateway/me/email/accounts/{WORK}/test", headers=ADMIN).json()["imap"]["ok"] is True

    plane = default_plane(gateway["data_dir"])
    sealed = plane.email_dir / "accounts" / WORK / "email" / "secret.enc"
    cfg = (plane.email_dir / "accounts" / WORK / "abstractcore.json").read_text()
    assert sealed.is_file() and '"oauth2"' in cfg and oauth_server.client_secret not in cfg
    assert not (plane.email_dir / "account" / "email" / "secret.enc").exists()


def test_further_mailbox_oauth_with_send_keeps_smtp(gateway, imap_bob, smtp, oauth_server, ca) -> None:
    from email_fixtures import ADMIN

    from abstractgateway.users import GatewayUserRegistry

    GatewayUserRegistry().create_user(user_id="admin", roles=["admin", "user"], email=ALICE)
    c = gateway["client"]
    base = f"/api/gateway/me/email/accounts/{WORK}/oauth"
    body = _oauth_body(oauth_server, imap_bob, ca, send=True, smtp={"host": "localhost", "port": smtp.port, "security": "starttls"})
    flow = c.post(f"{base}/start", headers=ADMIN, json=body).json()
    oauth_server.approve_all_devices()
    row = c.post(f"{base}/finish", headers=ADMIN, json={"flow_id": flow["flow_id"], "wait_s": 5}).json()
    assert row["can_send"] is True and row["can_read"] is True, row
    assert c.post(f"{base}/cancel", headers=ADMIN, json={"flow_id": flow["flow_id"]}).json()["cancelled"] is False


def test_a_password_refused_by_gmail_says_what_works() -> None:
    from abstractgateway.mail.accounts import GMAIL_PASSWORD_REFUSED, _named_step_error
    from abstractgateway.mail.core_mail import EmailError

    def refused(host: str) -> EmailError:
        return EmailError("Authentication failed.", "Check the password.", code="email_auth_failed",
                          details={"protocol": "imap", "host": host, "port": 993})

    gmail = _named_step_error(refused("imap.gmail.com"), password_sign_in=True)
    assert gmail.details["step_message"] == GMAIL_PASSWORD_REFUSED and gmail.details["provider"] == "google"
    assert "Sign in with Google" in GMAIL_PASSWORD_REFUSED and "app password" in GMAIL_PASSWORD_REFUSED
    smtp_gmail = _named_step_error(EmailError("x", "y", code="email_auth_failed", details={"protocol": "smtp", "host": "smtp.gmail.com", "port": 465}),
                                   password_sign_in=True)
    assert smtp_gmail.details["step_message"] == GMAIL_PASSWORD_REFUSED
    other = _named_step_error(refused("imap.example.test"), password_sign_in=True)
    assert other.details["step_message"].startswith("Sign-in refused by imap.example.test")
    oauth = _named_step_error(refused("imap.gmail.com"), password_sign_in=False)  # an OAuth sign-in: no app-password advice
    assert oauth.details["step_message"].startswith("Sign-in refused by imap.gmail.com")


def test_a_password_connect_to_gmail_answers_the_gmail_sentence(gateway, monkeypatch) -> None:
    """Through the route: the connect test's refusal (AbstractCore's store) reaches the client as
    the provider-aware sentence (`detail.message`), nothing stored."""
    from abstractgateway.mail import accounts as mail_accounts
    from abstractgateway.mail.core_mail import EmailError

    def refuse(self, account, secret, **kw):
        raise EmailError("Authentication failed.", "Check the password.", code="email_auth_failed",
                         details={"protocol": "imap", "host": "imap.gmail.com", "port": 993})

    monkeypatch.setattr(mail_accounts.EmailAccountStore, "connect", refuse)
    r = gateway["client"].put(f"/api/gateway/me/email/accounts/{WORK}", headers=gateway["alice"],
                              json={"address": "me@gmail.com", "password": "account-password",
                                    "imap": {"host": "imap.gmail.com", "port": 993, "security": "ssl"}})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["message"] == mail_accounts.GMAIL_PASSWORD_REFUSED and detail["step"] == "imap"
    assert gateway["client"].get(f"/api/gateway/me/email/accounts/{WORK}", headers=gateway["alice"]).status_code == 404


def test_folder_picker_lists_folders_with_roles_and_counts(gateway, imap, imap_bob, smtp) -> None:
    from abstractgateway.mail import accounts as mail_accounts

    c = gateway["client"]
    _connect_primary(gateway, imap, smtp)
    assert _connect_work(gateway, imap_bob).status_code == 200
    imap.add_message("INBOX", message("one"))
    imap.add_message("INBOX", message("two"))
    imap_bob.add_message("INBOX", message("to work", to=BOB))
    mail_accounts._FOLDERS_CACHE.clear()
    r = c.get("/api/gateway/me/email/folders", headers=gateway["alice"])
    assert r.status_code == 200, r.text
    assert r.json() == {"folders": [{"name": "INBOX", "special": "inbox", "messages": 2}, {"name": "Sent", "special": "sent", "messages": 0}]}
    work = c.get(f"/api/gateway/me/email/accounts/{WORK}/folders", headers=gateway["alice"]).json()
    assert work["folders"][0] == {"name": "INBOX", "special": "inbox", "messages": 1}
    # Cached a minute: a second call opens no connection.
    logins = len(imap.logins)
    assert c.get("/api/gateway/me/email/folders", headers=gateway["alice"]).json()["folders"][0]["messages"] == 2
    assert len(imap.logins) == logins
    # Not the caller's mailbox: 404; nobody signed in: 401.
    assert c.get(f"/api/gateway/me/email/accounts/{WORK}/folders", headers=gateway["bob"]).status_code == 404
    assert c.get("/api/gateway/me/email/folders").status_code == 401
    assert c.get("/api/gateway/me/email/folders", headers=gateway["bob"]).status_code == 404  # bob has no mailbox


def test_folder_roles_come_from_special_use(ca, tokens, monkeypatch) -> None:
    from types import SimpleNamespace

    from abstractcore.comms.email import EmailAccount, EmailClient, EmailSecret, ImapSettings
    from abstractcore.testing.mailserver import FakeImapServer

    from abstractgateway.mail import accounts as mail_accounts

    server = FakeImapServer(ca, users=dict(PASSWORDS), security="ssl", tokens=tokens, folders=("INBOX", "Sent", "Bin", "Archive"), trash="Bin")
    try:
        acct = EmailAccount.build(address=ALICE, imap=ImapSettings.build("localhost", port=server.port, security="ssl"))
        client = EmailClient(acct, EmailSecret(PASSWORDS[ALICE]), timeout=10)
        monkeypatch.setattr(mail_accounts, "email_context", lambda _plane: SimpleNamespace(client=lambda: client))
        monkeypatch.setattr(mail_accounts, "_FOLDERS_CACHE", {})
        out = mail_accounts.mailbox_folders(SimpleNamespace(root="r", account_id="t"))
        assert [(f["name"], f["special"]) for f in out["folders"]] == [("INBOX", "inbox"), ("Archive", None), ("Bin", "trash"), ("Sent", "sent")]
        logins = len(server.logins)
        mail_accounts.mailbox_folders(SimpleNamespace(root="r", account_id="t"), now=__import__("time").time() + 61)
        assert len(server.logins) == logins + 1  # the cache lasts a minute
    finally:
        server.close()


def test_access_flips_without_reconnecting_and_runs_can_then_change_and_send(gateway, imap, imap_bob, smtp) -> None:
    """lpalbou-gmail's case: a mailbox added Read only is turned Read and write in place; a flow's
    Mark as read and Forward then work, with the sign-in kept."""
    from abstractcore.comms.email.context import guarded_send
    from abstractruntime.email import email_binding_for_trigger

    from abstractgateway.mail.core_mail import OutgoingMessage
    from abstractgateway.mail.runtime_wiring import current_binding, make_email_resolver

    c = gateway["client"]
    _connect_primary(gateway, imap, smtp)
    assert _connect_work(gateway, imap_bob).status_code == 200
    route = f"/api/gateway/me/email/accounts/{WORK}/access"
    smtp_leg = {"host": "localhost", "port": smtp.port, "security": "starttls"}
    # A refused outgoing server changes nothing and names its step.
    bad = c.put(route, headers=gateway["alice"], json={"write": True, "smtp": {"host": "localhost", "port": 1, "security": "starttls"}})
    assert bad.status_code >= 400 and bad.json()["detail"].get("details", {}).get("step") == "smtp", bad.text
    assert c.get(f"/api/gateway/me/email/accounts/{WORK}", headers=gateway["alice"]).json()["access"] == "read"

    r = c.put(route, headers=gateway["alice"], json={"write": True, "smtp": smtp_leg})
    assert r.status_code == 200, r.text
    row = r.json()
    assert (row["ok"], row["access"], row["can_write"], row["can_send"], row["mailbox"]["state"]) == (True, "write", True, True, "connected")
    plane = plane_of("alice")
    binding = email_binding_for_trigger(current_binding(plane), {"source_id": "email.received", "source_version": 1, "config": {"account": WORK}})
    ctx = make_email_resolver(plane)(binding, use="action")
    assert ctx.account.can_write and ctx.account.can_send
    guarded_send(ctx, OutgoingMessage(to=(ALICE,), subject="forwarded", text="x"))
    assert len(smtp.messages) == 1
    uid = imap_bob.add_message("INBOX", message("mark me", to=BOB))
    ctx.client().mark([uid], seen=True)
    assert "\\Seen" in imap_bob.flags_of("INBOX", uid)

    # Back to Read only: the outgoing server is kept, unused; runs are refused again.
    r = c.put(route, headers=gateway["alice"], json={"write": False})
    assert (r.json()["access"], r.json()["can_write"], r.json()["can_send"]) == ("read", False, False)
    assert not make_email_resolver(plane)(binding, use="action").account.can_write
    # Turning it on again needs no new outgoing server.
    assert c.put(route, headers=gateway["alice"], json={"write": True}).json()["can_send"] is True

    # The primary is always read and write; another user's or an unknown mailbox is not found.
    prim = c.put("/api/gateway/me/email/accounts/default/access", headers=gateway["alice"], json={"write": False})
    assert prim.status_code == 400 and prim.json()["detail"]["reason_code"] == "email_invalid_settings"
    assert c.put(route, headers=gateway["bob"], json={"write": True}).status_code == 404
    assert c.put("/api/gateway/me/email/accounts/nope/access", headers=gateway["alice"], json={"write": True}).status_code == 404


def test_turning_write_on_finds_the_outgoing_server_when_none_is_given(gateway, imap_bob, monkeypatch) -> None:
    from abstractgateway.mail import accounts as mail_accounts

    monkeypatch.setattr(mail_accounts, "discover_servers", lambda address, **kw: {"found": False})
    assert _connect_work(gateway, imap_bob).status_code == 200
    r = gateway["client"].put(f"/api/gateway/me/email/accounts/{WORK}/access", headers=gateway["alice"], json={"write": True, "test": False})
    assert r.status_code == 200, r.text
    cfg = json.loads((plane_of("alice").email_dir / "accounts" / WORK / "abstractcore.json").read_text())
    acct = cfg["email"]["account"]
    assert acct["access"] == "write" and acct["smtp"]["host"] == "smtp." + BOB.split("@")[1], acct
