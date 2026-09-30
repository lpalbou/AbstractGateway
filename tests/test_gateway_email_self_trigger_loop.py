"""An "email me the result" automation never triggers itself (AbstractFramework 0.7.1).

The 0.7.0 end-to-end proof on a Mac: an automation on `email.received@1` whose filter matched
its own title, set to "Email me the result", re-triggered itself: the result notice
(`[AbstractFramework] <title>: result`) went from the user's account to the user's inbox with
no marker and the watcher admitted it (5 occurrences in 4 minutes).

Here, on the real gateway app (runner on, AbstractCore's hermetic IMAP/SMTP servers): exactly
one occurrence runs, the notice carries RFC 3834 `Auto-Submitted` + the framework marker, its
Message-ID is recorded in the outbox, and the watcher never turns it into an inbox event.
"""

from __future__ import annotations

import email
import email.policy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import ECHO_FLOW_ID, HEADERS, gateway_env, wait_until, write_echo_bundle
from email_fixtures import *  # noqa: F401,F403 - fixtures
from email_fixtures import ADMIN_ADDR, connect_body

pytestmark = pytest.mark.integration

TITLE = "Invoice watch 7f3a"


@pytest.fixture()
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway_env(monkeypatch, tmp_path, runner=True)
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(tmp_path / "core" / "abstractcore.json"))
    ref = write_echo_bundle(tmp_path / "bundles")
    from abstractgateway.app import app

    with TestClient(app) as c:
        c.bundle_ref = ref  # type: ignore[attr-defined]
        yield c


def _svc():
    from abstractgateway.service import get_gateway_service

    return get_gateway_service()


def _occurrences(c: TestClient, aid: str) -> list:
    r = c.get(f"/api/gateway/automations/{aid}/occurrences", headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _person_mail(subject: str) -> bytes:
    from email.message import EmailMessage

    m = EmailMessage()
    m["From"] = "Carol <carol@example.test>"
    m["To"] = ADMIN_ADDR
    m["Subject"] = subject
    m["Message-ID"] = "<carol-1@example.test>"
    m.set_content("Please pay invoice 7.")
    return m.as_bytes()


def test_the_result_notice_never_triggers_its_automation(live: TestClient, imap, smtp) -> None:
    from abstractgateway.mail.notifications import NotificationCollector, NotificationOutbox

    from abstractgateway.service import wait_for_gateway_boot

    # The app's boot thread starts the email worker: wait for it, or it would start the worker again
    # after the stop below and its own ticks would race the asserts.
    assert wait_for_gateway_boot(60) == "ready"
    svc = _svc()
    worker = svc.email_worker
    worker.stop()  # driven step by step below (the worker's own 15 s tick would race the asserts)
    assert worker._thread is None or not worker._thread.is_alive()
    plane = worker.plane

    r = live.put("/api/gateway/me/email", headers=HEADERS, json=connect_body(ADMIN_ADDR, imap, smtp))
    assert r.status_code == 200, r.text
    r = live.put("/api/gateway/me/notifications", headers=HEADERS, json={"email": {"automation_result": True}})
    assert r.status_code == 200, r.text

    body = {
        "request_id": "loop-1",
        "title": TITLE,
        "target": {"bundle_ref": live.bundle_ref, "flow_id": ECHO_FLOW_ID, "input_data": {"prompt": "triage", "notify": True}},
        # The filter matches the automation's own title, so it also matches the notice's
        # subject "[AbstractFramework] <title>: result": the exact 0.7.0 shape.
        "trigger": {"source_id": "email.received", "source_version": 1,
                    "config": {"uses_model": False, "filter": {"subject_contains": TITLE}}},
        "notify": {"channels": ["console", "email"]},
    }
    r = live.post("/api/gateway/automations", headers=HEADERS, json=body)
    assert r.status_code == 200, r.text
    aid = r.json()["automation_id"]

    watcher = worker._watcher()
    assert watcher.poll_once(force=True)["state"] == "watching"  # baseline
    NotificationCollector(plane, svc).collect()  # the collector's baseline (history is never mailed)

    imap.add_message("INBOX", _person_mail(f"{TITLE}: please pay"))
    first = watcher.poll_once(force=True)
    assert first["new"] == 1, first
    wait_until(lambda: [o for o in _occurrences(live, aid) if o["status"] == "completed"], timeout_s=20)

    assert NotificationCollector(plane, svc).collect()["queued"] == 1
    assert NotificationOutbox(plane).deliver()["sent"] == 1
    [sent] = smtp.messages
    notice = email.message_from_bytes(sent["data"], policy=email.policy.default)
    assert str(notice["Subject"]) == f"[AbstractFramework] {TITLE}: result"
    assert notice["Auto-Submitted"] == "auto-generated"
    # The marker header is long enough to be folded onto its own line; Python < 3.12's parser
    # keeps the fold's leading space in the value (AbstractCore's reader strips it).
    assert str(notice["X-AbstractFramework-Automation"]).strip().startswith("notification:")
    assert NotificationOutbox(plane).was_sent(str(notice["Message-ID"]))

    # The mail server delivers the notice to the same inbox; the watcher never admits it.
    imap.add_message("INBOX", sent["data"])
    second = watcher.poll_once(force=True)
    assert second["new"] == 0 and second["own_automatic"] == 1, second
    events = [rec["payload"]["subject"] for rec in watcher.inbox.read(stream=watcher.feeder.stream())]
    assert events == [f"{TITLE}: please pay"]
    assert len(_occurrences(live, aid)) == 1

    # Second layer: the same notice with every marker header stripped by a server is still
    # recognised by its recorded Message-ID.
    stripped = email.message_from_bytes(sent["data"], policy=email.policy.default)
    del stripped["X-AbstractFramework-Automation"]
    del stripped["Auto-Submitted"]
    imap.add_message("INBOX", stripped.as_bytes())
    third = watcher.poll_once(force=True)
    assert third["new"] == 0 and third["own_automatic"] == 1, third
    assert len(_occurrences(live, aid)) == 1


def test_automatic_sends_are_recorded_and_a_person_s_are_not(live: TestClient, imap, smtp) -> None:
    from dataclasses import replace

    from abstractgateway.mail.accounts import email_context
    from abstractgateway.mail.core_mail import OutgoingMessage, guarded_send
    from abstractgateway.mail.notifications import NotificationOutbox

    plane = _svc().email_worker.plane
    r = live.put("/api/gateway/me/email", headers=HEADERS, json=connect_body(ADMIN_ADDR, imap, smtp))
    assert r.status_code == 200, r.text
    person = guarded_send(email_context(plane), OutgoingMessage(to=(ADMIN_ADDR,), subject="by hand", text="x"))
    auto_ctx = replace(email_context(plane), automation_marker="automation:a1/run:r1")
    auto = guarded_send(auto_ctx, OutgoingMessage(to=(ADMIN_ADDR,), subject="by an automation", text="x"))
    outbox = NotificationOutbox(plane)
    assert outbox.was_sent(auto.message_id) and not outbox.was_sent(person.message_id)
    by_hand, by_automation = (email.message_from_bytes(m["data"], policy=email.policy.default) for m in smtp.messages)
    assert by_hand["Auto-Submitted"] is None and by_hand["X-AbstractFramework-Automation"] is None
    assert by_automation["Auto-Submitted"] == "auto-generated"
    assert by_automation["X-AbstractFramework-Automation"] == "automation:a1/run:r1"
