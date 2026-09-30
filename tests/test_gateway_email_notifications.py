"""Email notifications (framework backlog 0992 WP3, C5): preferences, the durable outbox
(queued once, a crash mid-send is `unknown` and never resent, 4xx retried, 5xx/auth
surfaced), the send limits' digest, the recipient policy, and the collector (automation
results/failures delivered to email only when the automation asks, approval waits, jobs
that asked `_runtime.notify`)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from email_fixtures import *  # noqa: F401,F403 - fixtures
from email_fixtures import ALICE, connect_body, plane_of, smtp_bodies

pytestmark = pytest.mark.integration


ALL_ON = {"automation_result": True, "automation_failed": True, "approval_needed": True, "job_finished": True, "job_failed": True}


def _connect(gateway, imap, smtp, *, notify: bool = True) -> None:
    r = gateway["client"].put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(ALICE, imap, smtp))
    assert r.status_code == 200, r.text
    if notify:
        r = gateway["client"].put("/api/gateway/me/notifications", headers=gateway["alice"], json={"email": ALL_ON})
        assert r.status_code == 200, r.text


def test_preferences_round_trip_and_availability(gateway, imap, smtp) -> None:
    c = gateway["client"]
    r = c.get("/api/gateway/me/notifications", headers=gateway["alice"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["channels"]["email"]["available"] is False and body["unavailable_reason"]
    assert {e["id"] for e in body["events"]} == {"automation_result", "automation_failed", "approval_needed", "job_finished", "job_failed"}

    # Every email event is OFF by default (the console stays the default channel).
    assert set(body["email"].values()) == {False}
    _connect(gateway, imap, smtp, notify=False)
    assert set(c.get("/api/gateway/me/notifications", headers=gateway["alice"]).json()["email"].values()) == {False}
    r = c.put("/api/gateway/me/notifications", headers=gateway["alice"], json={"email": {"job_failed": True}})
    assert r.status_code == 200, r.text
    assert r.json()["email"]["job_failed"] is True and r.json()["email"]["job_finished"] is False
    assert r.json()["channels"]["email"] == {"available": True, "to": ALICE}
    r = c.put("/api/gateway/me/notifications", headers=gateway["alice"], json={"email": {"bogus": True}})
    assert r.status_code == 400
    # Bob's preferences are his own.
    assert c.get("/api/gateway/me/notifications", headers=gateway["bob"]).json()["email"]["job_failed"] is False


def test_test_notification_goes_to_self_through_own_account(gateway, imap, smtp) -> None:
    _connect(gateway, imap, smtp)
    r = gateway["client"].post("/api/gateway/me/notifications/test", headers=gateway["alice"])
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["state"] == "sent"
    mails = smtp_bodies(smtp)
    assert len(mails) == 1 and mails[0]["to"] == [ALICE] and mails[0]["from"] == ALICE
    assert mails[0]["subject"].startswith("[AbstractFramework]")


def test_outbox_queues_once_and_sends_once(gateway, imap, smtp) -> None:
    from abstractgateway.mail.notifications import NotificationOutbox, idempotency_key, queue_notice

    _connect(gateway, imap, smtp)
    plane = plane_of("alice")
    key = idempotency_key("automation_result", "auto-1", 7)
    assert queue_notice(plane, "automation_result", key, {"title": "Invoices"}) is True
    assert queue_notice(plane, "automation_result", key, {"title": "Invoices"}) is False
    box = NotificationOutbox(plane)
    assert box.deliver()["sent"] == 1
    assert box.deliver()["sent"] == 0
    assert queue_notice(plane, "automation_result", key, {"title": "Invoices"}) is False  # a retry never re-queues
    assert box.deliver()["sent"] == 0
    assert len(smtp.messages) == 1
    assert "Invoices" in smtp_bodies(smtp)[0]["subject"]


def test_crash_mid_send_is_unknown_and_never_resent(gateway, imap, smtp) -> None:
    from abstractcore.comms.email import guarded_send

    from abstractgateway.mail.notifications import NotificationOutbox, idempotency_key, queue_notice

    _connect(gateway, imap, smtp)
    plane = plane_of("alice")
    queue_notice(plane, "job_failed", idempotency_key("job_failed", "run-1"), {"title": "Nightly build"})

    class ProcessDied(BaseException):
        pass

    def send_then_die(ctx, msg):
        guarded_send(ctx, msg)  # the SMTP exchange completed ...
        raise ProcessDied()  # ... and the process died before recording it

    with pytest.raises(ProcessDied):
        NotificationOutbox(plane).deliver(send=send_then_die)
    assert len(smtp.messages) == 1
    assert [r["state"] for r in NotificationOutbox(plane).rows()] == ["sending"]

    restarted = NotificationOutbox(plane)
    assert restarted.recover_interrupted() == 1
    assert [r["state"] for r in restarted.rows()] == ["unknown"]
    assert restarted.deliver()["sent"] == 0
    assert len(smtp.messages) == 1  # never resent automatically


def test_transient_failure_retries_auth_failure_is_surfaced(gateway, imap, smtp) -> None:
    from abstractcore.comms.email import EmailTransient

    from abstractgateway.mail.notifications import NotificationOutbox, idempotency_key, queue_notice

    _connect(gateway, imap, smtp)
    plane = plane_of("alice")
    queue_notice(plane, "job_failed", idempotency_key("job_failed", "run-2"), {"title": "A"})

    def busy(ctx, msg):
        raise EmailTransient("The SMTP server refused temporarily.", "Retry later.", details={"smtp_code": 451})

    box = NotificationOutbox(plane)
    assert box.deliver(send=busy)["deferred"] == 1
    row = box.rows()[0]
    assert row["state"] == "queued" and row["next_attempt_ts"] > 0 and row["error_code"] == "email_transient"

    smtp.users[ALICE] = "rotated-password"
    later = NotificationOutbox(plane, clock=lambda: row["next_attempt_ts"] + 1)
    assert later.deliver()["failed"] == 1
    row = later.rows()[0]
    assert row["state"] == "failed" and row["error_code"] == "email_auth_failed" and row["error_fix"]
    summary = gateway["client"].get("/api/gateway/me/notifications", headers=gateway["alice"]).json()["outbox"]
    assert summary["failed"] == 1 and summary["last_failure"]["code"] == "email_auth_failed"


def test_send_limits_coalesce_into_one_digest(gateway, imap, smtp) -> None:
    from abstractgateway.mail.accounts import account_store
    from abstractgateway.mail.notifications import NotificationOutbox, idempotency_key, queue_notice

    _connect(gateway, imap, smtp)
    plane = plane_of("alice")
    account_store(plane).set_limits(per_hour=1, per_day=100)
    for i in range(3):
        queue_notice(plane, "automation_result", idempotency_key("automation_result", "a", i), {"title": f"Report {i}"})
    out = NotificationOutbox(plane).deliver()
    assert out["sent"] == 1 and out["deferred"] == 2
    assert len(smtp.messages) == 1

    # The window passes (limits raised here instead of waiting an hour): ONE digest for the rest.
    account_store(plane).set_limits(per_hour=20, per_day=100)
    rows = NotificationOutbox(plane).rows(state="queued")
    later = NotificationOutbox(plane, clock=lambda: max(r["next_attempt_ts"] for r in rows) + 1)
    assert later.deliver()["sent"] == 2
    assert len(smtp.messages) == 2
    digest = smtp_bodies(smtp)[1]
    assert "2 notifications" in digest["subject"]
    assert "Report 1" in digest["text"] and "Report 2" in digest["text"]


def test_recipient_policy_applies_to_notifications(gateway, imap, smtp) -> None:
    from abstractgateway.mail.notifications import NotificationOutbox, idempotency_key, queue_notice

    _connect(gateway, imap, smtp)
    gateway["client"].put("/api/gateway/me/email/policy", headers=gateway["alice"], json={"mode": "allowlist", "entries": ["example.org"]})
    plane = plane_of("alice")
    queue_notice(plane, "job_failed", idempotency_key("job_failed", "run-3"), {"title": "A"})
    assert NotificationOutbox(plane).deliver()["failed"] == 1
    row = NotificationOutbox(plane).rows()[0]
    assert row["state"] == "failed" and row["error_code"] == "email_policy_refused"
    assert smtp.messages == []


def _fake_svc(*, automations=(), records=None, waits=None, runs=None):
    run_store = SimpleNamespace(list_runs=lambda status=None, wait_reason=None, limit=100: list((runs or {}).get(getattr(status, "value", status), [])))
    return SimpleNamespace(host=SimpleNamespace(run_store=run_store, ledger_store=object(), runtime=None)), {
        "automations": list(automations),
        "records": records or {},
        "waits": waits or {},
    }


def _patch_runtime_sources(monkeypatch, data):
    import abstractruntime.automation_queries as aq
    import abstractruntime.automations.attention as att
    import abstractruntime.automations.ledger as led

    monkeypatch.setattr(aq, "list_automations", lambda run_store, status=None, cursor=None, limit=50: SimpleNamespace(items=list(data["automations"]), next_cursor=None))
    monkeypatch.setattr(led, "automation_records", lambda ledger_store, aid, kind: list(data["records"].get(aid, [])))
    monkeypatch.setattr(att, "pending_waits", lambda run_store, aid, limit=20: list(data["waits"].get(aid, [])))


def _completed(seq: int, kind: str, channels, body: str = "") -> dict:
    return {"payload": {"index": seq, "run_id": f"occ-{seq}", "attention": {"kind": kind, "seq": seq, "title": "Daily digest", "body": body, "channels": channels}}}


def test_collector_emails_automation_results_only_when_the_automation_asks(gateway, imap, smtp, monkeypatch) -> None:
    from abstractgateway.mail.notifications import NotificationCollector, NotificationOutbox

    _connect(gateway, imap, smtp)
    plane = plane_of("alice")
    svc, data = _fake_svc(automations=[{"automation_id": "auto-1", "title": "Daily digest", "status": "active"}])
    data["records"]["auto-1"] = [_completed(1, "notify", ["console", "email"], "old news")]
    _patch_runtime_sources(monkeypatch, data)

    assert NotificationCollector(plane, svc).collect()["queued"] == 0  # baseline: history is not mailed
    data["records"]["auto-1"] += [
        _completed(2, "notify", ["console", "email"], "3 invoices arrived"),
        _completed(3, "notify", ["console"], "console only"),
        _completed(4, "failure", ["console", "email"], "The model endpoint refused the request."),
    ]
    assert NotificationCollector(plane, svc).collect()["queued"] == 2
    assert NotificationCollector(plane, svc).collect()["queued"] == 0  # once per item
    NotificationOutbox(plane).deliver()
    mails = smtp_bodies(smtp)
    assert len(mails) == 2
    subjects = sorted(m["subject"] for m in mails)
    assert subjects == ["[AbstractFramework] Daily digest: failed", "[AbstractFramework] Daily digest: result"]
    result = next(m for m in mails if m["subject"].endswith("result"))
    assert "3 invoices arrived" in result["text"] and "model-authored" in result["text"]
    assert all("console only" not in m["text"] for m in mails)

    # The user's preference switches an event off.
    gateway["client"].put("/api/gateway/me/notifications", headers=gateway["alice"], json={"email": {"automation_result": False}})
    data["records"]["auto-1"] += [_completed(5, "notify", ["console", "email"], "muted")]
    assert NotificationCollector(plane, svc).collect()["queued"] == 0


def test_collector_approval_waits_and_jobs_that_asked(gateway, imap, smtp, monkeypatch) -> None:
    from abstractruntime.core.models import RunState, RunStatus, WaitReason, WaitState

    from abstractgateway.mail.notifications import NotificationCollector, NotificationOutbox

    _connect(gateway, imap, smtp)
    plane = plane_of("alice")
    svc, data = _fake_svc(automations=[{"automation_id": "auto-1", "title": "Mail triage", "status": "active"}])
    _patch_runtime_sources(monkeypatch, data)
    NotificationCollector(plane, svc).collect()  # baseline

    data["waits"]["auto-1"] = [{"run_id": "occ-9", "wait_key": "w-1", "kind": "tool_approval", "details": [{"name": "send_email"}]}]

    def run(run_id, status, notify=None, error=None):
        vars0 = {"_runtime": {"notify": notify}} if notify else {}
        return RunState(run_id=run_id, workflow_id="Nightly build", status=status, current_node="n", vars=vars0,
                        error=error, updated_at="2999-01-01T00:00:00+00:00")

    waiting = RunState(run_id="run-w", workflow_id="Research", status=RunStatus.WAITING, current_node="n", vars={},
                       waiting=WaitState(reason=WaitReason.USER, wait_key="ask-1", prompt="Which folder?"))
    runs = {
        "waiting": [waiting],
        "completed": [run("run-ok", RunStatus.COMPLETED, {"on": ["finished"], "channels": ["email"]}), run("run-quiet", RunStatus.COMPLETED)],
        "failed": [run("run-bad", RunStatus.FAILED, {"on": ["failed"], "channels": ["email"]}, error="The tool timed out.")],
    }
    svc.host.run_store.list_runs = lambda status=None, wait_reason=None, limit=100: list(runs.get(getattr(status, "value", status), []))

    assert NotificationCollector(plane, svc).collect()["queued"] == 4
    assert NotificationCollector(plane, svc).collect()["queued"] == 0
    NotificationOutbox(plane).deliver()
    subjects = sorted(m["subject"] for m in smtp_bodies(smtp))
    assert subjects == [
        "[AbstractFramework] Mail triage: needs your action",
        "[AbstractFramework] Nightly build: failed",
        "[AbstractFramework] Nightly build: finished",
        "[AbstractFramework] Research: needs your action",
    ]
    approval = next(m for m in smtp_bodies(smtp) if "Mail triage" in m["subject"])
    assert "send_email" in approval["text"] and "Replying to this email does nothing" in approval["text"]
    failed = next(m for m in smtp_bodies(smtp) if m["subject"].endswith("failed"))
    assert "The tool timed out." in failed["text"]


def test_no_account_no_notifications(gateway, monkeypatch) -> None:
    from abstractgateway.mail.notifications import NotificationOutbox, idempotency_key, queue_notice

    plane = plane_of("alice")
    queue_notice(plane, "job_failed", idempotency_key("job_failed", "run-x"), {"title": "A"})
    assert NotificationOutbox(plane).deliver()["failed"] == 1
    row = NotificationOutbox(plane).rows()[0]
    assert row["state"] == "failed" and row["error_code"] == "email_not_configured"


def test_nothing_is_emailed_by_default(gateway, imap, smtp, monkeypatch) -> None:
    from abstractgateway.mail.notifications import NotificationCollector

    _connect(gateway, imap, smtp, notify=False)
    plane = plane_of("alice")
    svc, data = _fake_svc(automations=[{"automation_id": "auto-1", "title": "Digest", "status": "active"}])
    _patch_runtime_sources(monkeypatch, data)
    NotificationCollector(plane, svc).collect()
    data["records"]["auto-1"] = [_completed(1, "notify", ["console", "email"], "x")]
    data["waits"]["auto-1"] = [{"run_id": "occ-1", "wait_key": "w", "kind": "ask_user"}]
    assert NotificationCollector(plane, svc).collect()["queued"] == 0
