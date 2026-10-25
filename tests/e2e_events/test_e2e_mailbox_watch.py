"""Example 1 (framework backlog 1048): "watch my mailbox and tell me when something important
arrives".

1. the person's mailbox (`PUT /api/gateway/me/email`; a further read-only one is
   `PUT /api/gateway/me/email/accounts/<id>`, IMAP only) — the `email.received@1` source's
   connection is a mailbox (`config.account`, `self` = the primary);
2. an automation through `POST /api/gateway/automations` on `email.received@1` running the "judge"
   flow (the model decides whether the mail matters — no keyword rule);
3. a mail lands in the mailbox; the watcher reads it (here: one forced poll; the gateway
   polls on its cadence); one occurrence runs with the mail framed in its prompt;
4. the model says it matters: an attention item, `attention.raised` on `GET /api/gateway/me/events`,
   and the gateway tray — following that stream over HTTP on a scratch port — shows ONE desktop
   notification and records it (`notification.sent {channel: "tray"}`).

Hermetic: AbstractCore's fake IMAP/SMTP servers (example.test addresses, a throwaway CA), the
scripted model double, a scratch data folder and HOME. The live run between the operator's own two
mailboxes is described in docs/events-examples.md. The SECOND-mailbox form (`config.account: "second"`)
is accepted by the gateway (backlog 1044) and by AbstractRuntime's `email.received@1` (runtime f88a216)."""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from e2e_support import (
    HEADERS,
    ScriptedModel,
    TrayOnScratchPort,
    attention_items,
    completed_occurrences,
    create_automation,
    events_of,
    gateway_env,
    tray_recorded,
    wait_until,
)
from e2e_support import write_judge_bundle
from email_fixtures import *  # noqa: F401,F403 - fixtures (fake mail servers, hermetic env, CA)
from email_fixtures import ADMIN_ADDR, BOB, PASSWORDS, connect_body

pytestmark = pytest.mark.integration

SECOND = "second"


@pytest.fixture()
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway_env(monkeypatch, tmp_path)
    write_judge_bundle(tmp_path / "bundles")
    model = ScriptedModel()
    model.install(monkeypatch)
    from abstractgateway.app import app
    from abstractgateway.service import wait_for_gateway_boot

    with TestClient(app) as c:
        assert wait_for_gateway_boot(60) == "ready"
        yield c, model


def _mail(subject: str, text: str, message_id: str, to: str) -> bytes:
    m = EmailMessage()
    m["From"] = "Dana Accounts <dana@supplier.example.test>"
    m["To"] = to
    m["Subject"] = subject
    m["Message-ID"] = message_id
    m.set_content(text)
    return m.as_bytes()


def _watch_and_reach_the_tray(client, model, *, account: str, server, address: str) -> None:
    from abstractgateway.service import get_gateway_service

    worker = get_gateway_service().email_worker
    config = {"uses_model": True, "every": "60s"}
    if account != "self":
        config["account"] = account
    aid = create_automation(client, title="Mailbox watch", request_id=f"e2e-mail-{account}",
                            trigger={"source_id": "email.received", "source_version": 1, "config": config},
                            prompt="A new mail arrived. Tell me only if it needs me today.")
    watcher = worker._watcher() if account == "self" else worker._watcher(worker.plane.for_account(account))
    wait_until(lambda: watcher.feeder.status("INBOX").get("cursor"), timeout_s=10)  # the baseline, at creation

    tray = TrayOnScratchPort()
    try:
        wait_until(lambda: tray.knows_automation(aid), timeout_s=15)
        model.answer({"title": "Invoice overdue: payment due today",
                      "body": "Dana from the supplier says invoice 2231 is overdue and service stops tonight."})
        server.add_message("INBOX", _mail("Invoice 2231 overdue", "Hello, invoice 2231 is overdue; service stops tonight.",
                                          f"<inv-2231-{account}@supplier.example.test>", address))
        assert watcher.poll_once(force=True)["new"] == 1

        [occ] = wait_until(lambda: completed_occurrences(client, aid), timeout_s=30)
        assert any("Invoice 2231 overdue" in p for p in model.prompts), "the model read the mail"
        [item] = attention_items(client, aid)
        assert item["title"] == "Invoice overdue: payment due today"

        raised = wait_until(lambda: events_of(client, "attention.raised", automation_id=aid))
        record_key = raised[0]["data"]["record_key"]
        assert wait_until(lambda: events_of(client, "run.finished", run_id=occ["run_id"]))

        # The tray: one desktop notification for the live item, and the stream records it.
        wait_until(lambda: tray.icon.shown, timeout_s=15)
        assert len(tray.icon.shown) == 1 and tray.icon.shown[0][0] == "Mailbox watch"
        sent = wait_until(lambda: tray_recorded(client, record_key), timeout_s=15)
        assert sent["data"]["notice"] == f"attention:{record_key}"
        assert tray.tray._attention.unseen == 1

        # Opening the automation marks it seen: the stream says so and the tray's badge goes down.
        cursor = client.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["summary"]["attention"]["items"][-1]["cursor"]
        assert client.post(f"/api/gateway/automations/{aid}/seen", headers=HEADERS, json={"attention_cursor": cursor}).status_code == 200
        assert wait_until(lambda: events_of(client, "attention.seen", automation_id=aid))
        wait_until(lambda: tray.tray._attention.unseen == 0, timeout_s=15)
    finally:
        tray.close()
        worker.stop()


def test_an_important_mail_reaches_the_tray(live, imap, smtp) -> None:
    client, model = live
    assert client.put("/api/gateway/me/email", headers=HEADERS, json=connect_body(ADMIN_ADDR, imap, smtp)).status_code == 200
    _watch_and_reach_the_tray(client, model, account="self", server=imap, address=ADMIN_ADDR)


def test_an_important_mail_in_the_second_mailbox_reaches_the_tray(live, imap, imap_bob, smtp) -> None:
    client, model = live
    assert client.put("/api/gateway/me/email", headers=HEADERS, json=connect_body(ADMIN_ADDR, imap, smtp)).status_code == 200
    r = client.put(f"/api/gateway/me/email/accounts/{SECOND}", headers=HEADERS,
                   json={"address": BOB, "password": PASSWORDS[BOB], "imap": {"host": "localhost", "port": imap_bob.port, "security": "ssl"}})
    assert r.status_code == 200, r.text
    assert (r.json()["can_read"], r.json()["can_send"]) == (True, False)
    _watch_and_reach_the_tray(client, model, account=SECOND, server=imap_bob, address=BOB)
