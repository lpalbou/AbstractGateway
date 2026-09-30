"""The first email automation's mail watcher baseline is taken right away (0.7.1 end-to-end).

On the Linux end-to-end run a user connected a mailbox, created an `email.received@1`
automation and 8 s later sent the mail meant to trigger it: nothing ran. The watcher reads
nothing while no email automation exists, and its first read is a baseline (mail already there
is history). That read came up to a minute after the automation was created: the idle checks
counted as polls, and nothing woke the worker. Mail arriving in that minute was absorbed as
history. Now creating an email automation wakes the worker and the baseline is taken at once.

Real gateway app (runner on), AbstractCore's hermetic IMAP/SMTP servers; the worker's own tick
is set to an hour so only the creation can wake it.
"""

from __future__ import annotations

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
    m["Message-ID"] = "<carol-first@example.test>"
    m.set_content("Testing my new automation.")
    return m.as_bytes()


def test_creating_the_first_email_automation_takes_the_baseline_at_once(live: TestClient, imap, smtp) -> None:
    from abstractgateway.mail.watcher import read_watcher_state
    from abstractgateway.service import get_gateway_service

    svc = get_gateway_service()
    worker = svc.email_worker
    r = live.put("/api/gateway/me/email", headers=HEADERS, json=connect_body(ADMIN_ADDR, imap, smtp))
    assert r.status_code == 200, r.text

    # Only the creation may wake the worker from here on: its own tick is an hour away.
    worker.stop()
    worker.tick_s = 3600.0
    worker.start()
    wait_until(lambda: str(read_watcher_state(worker.plane).get("state") or "").startswith("idle"), timeout_s=10)

    body = {
        "request_id": "first-email-automation",
        "title": "Testing watch",
        "target": {"bundle_ref": live.bundle_ref, "flow_id": ECHO_FLOW_ID, "input_data": {"prompt": "triage"}},
        "trigger": {"source_id": "email.received", "source_version": 1,
                    "config": {"uses_model": False, "filter": {"subject_contains": "my new automation"}}},
    }
    r = live.post("/api/gateway/automations", headers=HEADERS, json=body)
    assert r.status_code == 200, r.text
    aid = r.json()["automation_id"]

    # The baseline follows the creation within seconds (it used to wait for the 60 s cadence).
    wait_until(lambda: worker._watcher().feeder.status("INBOX").get("cursor"), timeout_s=5)

    # Mail sent to test the automation right after is new mail, not history.
    imap.add_message("INBOX", _person_mail("Checking my new automation"))
    assert worker._watcher().poll_once(force=True)["new"] == 1

    def completed():
        r = live.get(f"/api/gateway/automations/{aid}/occurrences", headers=HEADERS)
        return [o for o in r.json()["items"] if o["status"] == "completed"]

    assert len(wait_until(completed, timeout_s=20)) == 1
    worker.stop()
