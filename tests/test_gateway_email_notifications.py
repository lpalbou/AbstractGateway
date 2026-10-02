"""Email notifications (framework backlog 0992 WP3, C5; two switches since 2026-09-30):
preferences (Job failed, Approval needed; both ON by default; v1 bodies and files mapped), the
durable outbox (queued once, a crash mid-send is `unknown` and never resent, 4xx retried,
5xx/auth surfaced), the send limits' digest, the recipient policy, and the collector
(automation results delivered to email only when the automation asks, failures on "Job
failed", approval waits on "Approval needed", jobs that asked `_runtime.notify` on their own)."""

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
    assert body["channels"]["email"]["available"] is False
    assert body["unavailable_reason"] == "Connect a mailbox first."
    assert [(e["id"], e["label"]) for e in body["events"]] == [("job_failed", "Job failed"), ("approval_needed", "Approval needed")]

    # Both switches are ON by default; they only send once a mailbox is connected.
    assert body["email"] == {"job_failed": True, "approval_needed": True}
    me = c.get("/api/gateway/me/email", headers=gateway["alice"]).json()
    assert me["notifications"] == {"job_failed": True, "approval_needed": True}
    assert me["notifications_unavailable_reason"] == "Connect a mailbox first."
    _connect(gateway, imap, smtp, notify=False)
    assert c.get("/api/gateway/me/email", headers=gateway["alice"]).json()["notifications_unavailable_reason"] is None

    # The new route: one switch at a time, answered like GET /me/email.
    r = c.put("/api/gateway/me/email/notifications", headers=gateway["alice"], json={"job_failed": False})
    assert r.status_code == 200, r.text
    assert r.json()["notifications"] == {"job_failed": False, "approval_needed": True}
    assert c.get("/api/gateway/me/notifications", headers=gateway["alice"]).json()["email"] == {"job_failed": False, "approval_needed": True}
    r = c.get("/api/gateway/me/notifications", headers=gateway["alice"])
    assert r.json()["channels"]["email"] == {"available": True, "to": ALICE}
    assert c.put("/api/gateway/me/email/notifications", headers=gateway["alice"], json={"bogus": True}).status_code == 422

    # The old five-kind body is still accepted and mapped: automation_failed counts for job_failed.
    r = c.put("/api/gateway/me/notifications", headers=gateway["alice"], json={"email": {"automation_failed": True, "job_finished": False}})
    assert r.status_code == 200, r.text
    assert r.json()["email"] == {"job_failed": True, "approval_needed": True}
    r = c.put("/api/gateway/me/email/notifications", headers=gateway["alice"], json={"email": {"approval_needed": False, "automation_result": True}})
    assert r.status_code == 200 and r.json()["notifications"] == {"job_failed": True, "approval_needed": False}
    r = c.put("/api/gateway/me/notifications", headers=gateway["alice"], json={"email": {"bogus": True}})
    assert r.status_code == 400
    # Bob's preferences are his own.
    assert c.get("/api/gateway/me/notifications", headers=gateway["bob"]).json()["email"] == {"job_failed": True, "approval_needed": True}


@pytest.mark.parametrize(
    "stored, expected",
    [
        (None, {"job_failed": True, "approval_needed": True}),  # never saved: the new defaults
        ({"automation_result": True, "automation_failed": False, "approval_needed": False, "job_finished": True, "job_failed": False},
         {"job_failed": False, "approval_needed": False}),
        ({"automation_result": False, "automation_failed": True, "approval_needed": True, "job_finished": False, "job_failed": False},
         {"job_failed": True, "approval_needed": True}),
        ({"automation_result": False, "automation_failed": False, "approval_needed": False, "job_finished": False, "job_failed": True},
         {"job_failed": True, "approval_needed": False}),
    ],
)
def test_v1_preferences_file_is_read_as_the_two_switches(gateway, stored, expected) -> None:
    import json as json_mod

    from abstractgateway.mail.notifications import read_preferences, write_preferences

    plane = plane_of("alice")
    path = plane.email_dir / "notifications.json"
    if stored is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json_mod.dumps({"version": 1, "email": stored}), encoding="utf-8")
    assert read_preferences(plane) == expected
    assert gateway["client"].get("/api/gateway/me/email", headers=gateway["alice"]).json()["notifications"] == expected
    # The next save writes v2 and keeps the migrated values it does not change.
    write_preferences(plane, {})
    doc = json_mod.loads(path.read_text(encoding="utf-8"))
    assert doc["version"] == 2 and doc["email"] == expected


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
    """Notifications go through the same recipient rules as agent sends. They go to the user's
    own address, which the rules always allow (DESIGN-v3 §13.3: self -> allowed first), so a
    notification is delivered even when the Allowed list does not name the user and the Denied
    list names their domain; before round 3 the own address was refused here."""
    from abstractgateway.mail.notifications import NotificationOutbox, idempotency_key, queue_notice

    _connect(gateway, imap, smtp)
    own_domain = ALICE.split("@", 1)[1]
    r = gateway["client"].put(
        "/api/gateway/me/email/policy",
        headers=gateway["alice"],
        json={"mode": "allowlist", "always_allow": ["example.org"], "always_deny": [own_domain]},
    )
    assert r.status_code == 200, r.text
    plane = plane_of("alice")
    queue_notice(plane, "job_failed", idempotency_key("job_failed", "run-3"), {"title": "A"})
    assert NotificationOutbox(plane).deliver()["sent"] == 1
    assert len(smtp.messages) == 1


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

    def fake_list_attention(ledger_store, aid, *, after_seq=0, cursor=None, limit=50):
        items = [i for i in data["records"].get(aid, []) if i["seq"] > int(after_seq or 0)]
        return {"items": items[:limit], "next_cursor": None}

    monkeypatch.setattr(aq, "list_automations", lambda run_store, status=None, cursor=None, limit=50: SimpleNamespace(items=list(data["automations"]), next_cursor=None))
    monkeypatch.setattr(att, "list_attention", fake_list_attention)
    monkeypatch.setattr(att, "pending_waits", lambda run_store, aid, limit=20: list(data["waits"].get(aid, [])))


def _completed(seq: int, kind: str, channels, body: str = "") -> dict:
    """An attention item as AbstractRuntime's `list_attention` pages it (channels included)."""
    return {"kind": kind, "seq": seq, "title": "Daily digest", "body": body, "channels": channels, "index": seq, "run_id": f"occ-{seq}"}


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

    # "Job failed" covers every automation's failures after the retries, console-only ones too.
    data["records"]["auto-1"] += [_completed(5, "failure", ["console"], "The disk is full.")]
    assert NotificationCollector(plane, svc).collect()["queued"] == 1

    # With "Job failed" off: no failure mail, but "Email me the result" still delivers on its own
    # (no global preference gates it any more).
    r = gateway["client"].put("/api/gateway/me/email/notifications", headers=gateway["alice"],
                              json={"job_failed": False, "approval_needed": False})
    assert r.status_code == 200, r.text
    data["records"]["auto-1"] += [
        _completed(6, "failure", ["console", "email"], "muted failure"),
        _completed(7, "notify", ["console", "email"], "still wanted"),
    ]
    assert NotificationCollector(plane, svc).collect()["queued"] == 1
    NotificationOutbox(plane).deliver()
    texts = [m["text"] for m in smtp_bodies(smtp)]
    assert any("still wanted" in t for t in texts) and not any("muted failure" in t for t in texts)


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


def test_defaults_mail_approvals_and_opted_in_results_only(gateway, imap, smtp, monkeypatch) -> None:
    from abstractgateway.mail.notifications import NotificationCollector

    _connect(gateway, imap, smtp, notify=False)  # never saved preferences: the defaults
    plane = plane_of("alice")
    svc, data = _fake_svc(automations=[{"automation_id": "auto-1", "title": "Digest", "status": "active"}])
    _patch_runtime_sources(monkeypatch, data)
    NotificationCollector(plane, svc).collect()
    data["records"]["auto-1"] = [
        _completed(1, "notify", ["console", "email"], "x"),   # "Email me the result": mailed
        _completed(2, "notify", ["console"], "console only"),  # console only: never mailed
    ]
    data["waits"]["auto-1"] = [{"run_id": "occ-1", "wait_key": "w", "kind": "ask_user"}]  # Approval needed: ON
    assert NotificationCollector(plane, svc).collect()["queued"] == 2


def test_jobs_that_asked_are_mailed_without_any_global_preference(gateway, imap, smtp, monkeypatch) -> None:
    from abstractruntime.core.models import RunState, RunStatus

    from abstractgateway.mail.notifications import NotificationCollector, NotificationOutbox

    _connect(gateway, imap, smtp, notify=False)
    r = gateway["client"].put("/api/gateway/me/email/notifications", headers=gateway["alice"],
                              json={"job_failed": False, "approval_needed": False})
    assert r.status_code == 200, r.text
    plane = plane_of("alice")
    svc, data = _fake_svc()
    _patch_runtime_sources(monkeypatch, data)
    NotificationCollector(plane, svc).collect()  # baseline

    def run(run_id, status, notify=None):
        vars0 = {"_runtime": {"notify": notify}} if notify else {}
        return RunState(run_id=run_id, workflow_id="Nightly build", status=status, current_node="n", vars=vars0,
                        error="boom" if status == RunStatus.FAILED else None, updated_at="2999-01-01T00:00:00+00:00")

    runs = {
        "completed": [run("run-ok", RunStatus.COMPLETED, {"on": ["finished"], "channels": ["email"]}), run("run-quiet", RunStatus.COMPLETED)],
        "failed": [run("run-bad", RunStatus.FAILED, {"on": ["failed"], "channels": ["email"]}), run("run-quiet-bad", RunStatus.FAILED)],
    }
    svc.host.run_store.list_runs = lambda status=None, wait_reason=None, limit=100: list(runs.get(getattr(status, "value", status), []))
    assert NotificationCollector(plane, svc).collect()["queued"] == 2
    NotificationOutbox(plane).deliver()
    subjects = sorted(m["subject"] for m in smtp_bodies(smtp))
    assert subjects == ["[AbstractFramework] Nightly build: failed", "[AbstractFramework] Nightly build: finished"]
    assert all("email me when done" in m["text"] for m in smtp_bodies(smtp))


def test_outbox_exists_only_once_something_is_queued(gateway, imap, smtp) -> None:
    from abstractgateway.mail.worker import EmailWorker

    plane = plane_of("alice")
    svc, _data = _fake_svc()
    EmailWorker(svc, plane).tick()
    r = gateway["client"].post("/api/gateway/me/notifications/test", headers=gateway["alice"])
    assert r.json()["ok"] is False and r.json()["error"]["code"] == "email_not_configured"
    assert not (plane.email_dir / "outbox.sqlite3").exists()
    _connect(gateway, imap, smtp, notify=False)
    assert not (plane.email_dir / "outbox.sqlite3").exists()


def test_explicit_result_on_first_collection_delivers_full_body_to_selected_recipients(gateway, imap, smtp, monkeypatch):
    from abstractgateway.mail.notifications import NotificationCollector, NotificationOutbox

    _connect(gateway, imap, smtp)
    allowed = gateway["client"].put("/api/gateway/me/email/policy", headers=gateway["alice"],
        json={"mode": "allowlist", "always_allow": ["recipient@example.test"]})
    assert allowed.status_code == 200, allowed.text
    plane = plane_of("alice")
    full_result = "A complete paragraph of the result.\n" * 400
    svc, data = _fake_svc(automations=[{"automation_id": "result", "title": "Report", "status": "active"}])
    data["records"]["result"] = [
        _completed(1, "notify", ["console", "email"], "Old notice must stay quiet"),
        {**_completed(2, "notify", ["console", "email"], "Truncated excerpt"),
         "email_result": full_result, "recipients": ["self", "recipient@example.test"]},
    ]
    _patch_runtime_sources(monkeypatch, data)
    assert NotificationCollector(plane, svc).collect()["queued"] == 1
    assert NotificationCollector(plane, svc).collect()["queued"] == 0
    assert NotificationOutbox(plane).deliver()["sent"] == 1
    [mail] = smtp_bodies(smtp)
    assert set(mail["to"]) == {ALICE, "recipient@example.test"}
    assert full_result.strip() in mail["text"].replace("\r\n", "\n")
    assert "Old notice" not in mail["text"] and "Truncated excerpt" not in mail["text"]


def test_outbox_migrates_existing_owner_only_notice(gateway, imap, smtp):
    import sqlite3
    from abstractgateway.mail.notifications import NotificationOutbox

    _connect(gateway, imap, smtp)
    box = NotificationOutbox(plane_of("alice"))
    box.enqueue("old-notice", "job_finished", "Legacy result", "Saved before recipients existed")
    # Recreate the actual old schema while retaining an already-queued notice.
    with sqlite3.connect(box.path) as conn:
        conn.execute("ALTER TABLE notices DROP COLUMN recipients_json")
    assert NotificationOutbox(box.plane).deliver()["sent"] == 1
    [mail] = smtp_bodies(smtp)
    assert mail["to"] == [ALICE] and "Saved before" in mail["text"]
    assert NotificationOutbox(box.plane).deliver()["sent"] == 0


def test_rate_limited_digests_never_mix_recipient_groups(gateway, imap, smtp):
    from abstractgateway.mail.accounts import account_store
    from abstractgateway.mail.notifications import NotificationOutbox, queue_notice

    _connect(gateway, imap, smtp)
    allowed = gateway["client"].put("/api/gateway/me/email/policy", headers=gateway["alice"],
        json={"mode": "allowlist", "always_allow": ["a@example.test", "b@example.test"]})
    assert allowed.status_code == 200, allowed.text
    plane = plane_of("alice")
    account_store(plane).set_limits(per_hour=1, per_day=100)
    queue_notice(plane, "test", "consume-limit", {"title": "Initial"})
    assert NotificationOutbox(plane).deliver()["sent"] == 1
    for i, recipient in enumerate(["a@example.test", "b@example.test", "a@example.test"]):
        queue_notice(plane, "automation_result", f"group-{i}", {
            "title": f"Report {i}", "model_body": f"Private result {i}", "recipients": [recipient]})
    assert NotificationOutbox(plane).deliver()["deferred"] == 3
    rows = NotificationOutbox(plane).rows(state="queued")
    account_store(plane).set_limits(per_hour=20, per_day=100)
    later = NotificationOutbox(plane, clock=lambda: max(r["next_attempt_ts"] for r in rows) + 1)
    assert later.deliver()["sent"] == 3
    mails = smtp_bodies(smtp)[1:]
    assert len(mails) == 2
    a = next(mail for mail in mails if mail["to"] == ["a@example.test"])
    b = next(mail for mail in mails if mail["to"] == ["b@example.test"])
    assert "Private result 0" in a["text"] and "Private result 2" in a["text"] and "Private result 1" not in a["text"]
    assert "Private result 1" in b["text"] and "Private result 0" not in b["text"] and "Private result 2" not in b["text"]
    assert later.deliver()["sent"] == 0


def test_explicit_result_recipients_still_obey_mailbox_policy(gateway, imap, smtp):
    from abstractgateway.mail.notifications import NotificationOutbox, queue_notice

    _connect(gateway, imap, smtp)
    blocked = gateway["client"].put("/api/gateway/me/email/policy", headers=gateway["alice"],
        json={"mode": "denylist", "always_deny": ["denied@example.test"]})
    assert blocked.status_code == 200, blocked.text
    plane = plane_of("alice")
    queue_notice(plane, "automation_result", "blocked-result", {
        "title": "Result", "model_body": "Must stay private", "recipients": ["self", "denied@example.test"]})
    box = NotificationOutbox(plane)
    assert box.deliver()["failed"] == 1
    [row] = box.rows()
    assert row["state"] == "failed" and row["error_code"] == "email_policy_refused"
    assert smtp.messages == []
