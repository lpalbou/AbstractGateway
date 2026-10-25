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
    assert row["account_ref"] == "default:alice:work" and row["mailbox"]["state"] == "receive_only"

    items = c.get("/api/gateway/me/email/accounts", headers=gateway["alice"]).json()["items"]
    assert [(i["account_id"], i["address"], i["can_read"], i["can_send"]) for i in items] == [
        ("default", ALICE, True, True), (WORK, BOB, True, False)]
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


def test_send_true_stores_smtp_and_bad_ids_are_refused(gateway, imap_bob, smtp) -> None:
    c = gateway["client"]
    r = _connect_work(gateway, imap_bob, send=True, smtp={"host": "localhost", "port": smtp.port, "security": "starttls"})
    assert r.status_code == 200, r.text
    assert r.json()["can_send"] is True and r.json()["mailbox"]["state"] == "connected"
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
    from abstractcore.comms.email import EmailNotConfigured
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
    with pytest.raises(EmailNotConfigured, match="no SMTP"):
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
