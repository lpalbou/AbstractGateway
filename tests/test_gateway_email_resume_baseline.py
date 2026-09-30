"""Resuming a paused email automation takes the watcher's baseline right away (0.9.0 follow-up).

0.9.0 woke the email worker when an email automation was CREATED (041ccbb). A RESUMED one was
left to the worker's next tick: after a time with no active email automation the watcher's next
read is a fresh baseline (0b42d8d), so mail arriving between the resume and that tick (up to
15 s) was taken as history and never triggered the resumed automation. Now the runner tells the
email worker when an automation command is applied, and a resume of an email automation wakes
it at once.

Real gateway app (runner on), AbstractCore's hermetic IMAP/SMTP servers; the worker's own tick is
an hour, so only the resume can wake it. Red without `EmailWorker.on_automation_command` (or
without the runner's listener call).
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import ECHO_FLOW_ID, HEADERS, gateway_env, wait_until, write_echo_bundle
from email_fixtures import *  # noqa: F401,F403 - fixtures
from email_fixtures import ADMIN_ADDR, connect_body

pytestmark = pytest.mark.integration


@pytest.fixture()
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway_env(monkeypatch, tmp_path, runner=True)
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(tmp_path / "core" / "abstractcore.json"))
    ref = write_echo_bundle(tmp_path / "bundles")
    from abstractgateway.app import app

    with TestClient(app) as c:
        c.bundle_ref = ref  # type: ignore[attr-defined]
        yield c


def _person_mail(subject: str) -> bytes:
    from email.message import EmailMessage

    m = EmailMessage()
    m["From"] = "Carol <carol@example.test>"
    m["To"] = ADMIN_ADDR
    m["Subject"] = subject
    m["Message-ID"] = f"<carol-{uuid.uuid4().hex}@example.test>"
    m.set_content("Is the resumed automation listening?")
    return m.as_bytes()


def _command(live: TestClient, aid: str, type_: str) -> None:
    r = live.post(f"/api/gateway/automations/{aid}/commands", headers=HEADERS, json={"command_id": f"{type_}-{uuid.uuid4().hex[:8]}", "type": f"automation.{type_}"})
    assert r.status_code == 200, r.text


def _status(live: TestClient, aid: str) -> str:
    r = live.get(f"/api/gateway/automations/{aid}", headers=HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    return str((body.get("summary") or body).get("status") or "")


def test_resuming_an_email_automation_takes_the_baseline_at_once(live: TestClient, imap, smtp) -> None:
    from abstractgateway.mail.watcher import _write_state, read_watcher_state
    from abstractgateway.service import get_gateway_service

    svc = get_gateway_service()
    worker = svc.email_worker
    r = live.put("/api/gateway/me/email", headers=HEADERS, json=connect_body(ADMIN_ADDR, imap, smtp))
    assert r.status_code == 200, r.text

    body = {
        "request_id": "resume-email-automation",
        "title": "Resume watch",
        "target": {"bundle_ref": live.bundle_ref, "flow_id": ECHO_FLOW_ID, "input_data": {"prompt": "triage"}},
        "trigger": {"source_id": "email.received", "source_version": 1,
                    "config": {"uses_model": False, "filter": {"subject_contains": "resumed automation"}}},
    }
    r = live.post("/api/gateway/automations", headers=HEADERS, json=body)
    assert r.status_code == 200, r.text
    aid = r.json()["automation_id"]
    wait_until(lambda: worker._watcher().feeder.status("INBOX").get("cursor"), timeout_s=10)

    # From here on only the resume may wake the worker: its own tick is an hour away.
    worker.stop()
    worker.tick_s = 3600.0
    worker.start()

    _command(live, aid, "pause")
    wait_until(lambda: _status(live, aid) == "paused", timeout_s=10)
    # A while later the watcher checks and finds no email automation (what its tick does once the
    # 60 s cadence is due): it stops reading and marks the cursor stale.
    doc = read_watcher_state(worker.plane)
    doc["last_poll_ts"] = time.time() - 600
    _write_state(worker.plane, doc)
    assert worker._watcher().poll_once()["state"].startswith("idle")
    assert read_watcher_state(worker.plane).get("without_consumers") is True

    _command(live, aid, "resume")
    # The baseline follows the resume within seconds (it used to wait for the worker's tick).
    assert wait_until(lambda: read_watcher_state(worker.plane).get("without_consumers") is None
                      and str(read_watcher_state(worker.plane).get("state") or "") == "watching", timeout_s=5)

    # Mail sent right after the resume is new mail, not history.
    imap.add_message("INBOX", _person_mail("Checking the resumed automation"))
    assert worker._watcher().poll_once(force=True)["new"] == 1

    def completed():
        r = live.get(f"/api/gateway/automations/{aid}/occurrences", headers=HEADERS)
        return [o for o in r.json()["items"] if o["status"] == "completed"]

    assert len(wait_until(completed, timeout_s=20)) == 1
    worker.stop()
