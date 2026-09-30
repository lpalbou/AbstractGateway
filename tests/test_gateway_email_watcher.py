"""The per-user mail watcher (framework backlog 0992 WP3, C4) over AbstractRuntime's feeder:
read-only, the durable cursor moves only after the event is stored, a UIDVALIDITY reset
loses nothing and doubles nothing, one bad message never blocks the mailbox, each user's
watcher feeds only that user's inbox, and the gateway's gates (admin switch, user switch,
no consumer) keep the mailbox untouched."""

from __future__ import annotations

import json

import pytest

from email_fixtures import *  # noqa: F401,F403 - fixtures
from email_fixtures import ADMIN, ALICE, BOB, connect_body, message, plane_of

pytestmark = pytest.mark.integration


def _connect(gateway, who: str, imap, smtp, address: str) -> None:
    r = gateway["client"].put("/api/gateway/me/email", headers=gateway[who], json=connect_body(address, imap, smtp))
    assert r.status_code == 200, r.text


def _watcher(user: str, **kw):
    from abstractgateway.mail.watcher import MailWatcher

    return MailWatcher(plane_of(user), has_consumers=kw.pop("has_consumers", lambda: True), **kw)


def _events(w) -> list:
    return [rec["payload"] for rec in w.inbox.read(stream=w.feeder.stream())]


def _audit(gateway) -> list:
    path = gateway["data_dir"] / "audit_log.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if '"event"' in line]


def test_baseline_then_new_mail_once_and_read_only(gateway, imap, smtp) -> None:
    imap.add_message("INBOX", message("old mail"))
    _connect(gateway, "alice", imap, smtp, ALICE)
    woken = []
    w = _watcher("alice", on_appended=woken.append)

    first = w.poll_once()
    assert first["state"] == "watching" and first["new"] == 0  # baseline: history is not an event
    long_text = "x" * 60000 + " END"
    uid = imap.add_message("INBOX", message("hello there", text=long_text))
    second = w.poll_once()
    assert second["new"] == 1 and len(woken) == 1  # the email automations are woken
    events = _events(w)
    assert [e["subject"] for e in events] == ["hello there"]
    assert events[0]["account_ref"] == "default:alice:default" and events[0]["uid"] == uid
    assert events[0]["body_text"].strip().endswith("END") and len(events[0]["body_text"]) >= 60000  # whole body

    # A fresh watcher (restart) sees nothing new: the cursor is durable.
    assert _watcher("alice").poll_once()["new"] == 0
    assert len(_events(w)) == 1

    # Read-only (D12): no \\Seen was set, no mailbox-changing command was sent.
    assert "\\Seen" not in imap.flags_of("INBOX", uid)
    assert not {"STORE", "SELECT", "EXPUNGE", "COPY", "MOVE", "APPEND", "DELETE"} & set(imap.commands)

    status = gateway["client"].get("/api/gateway/me/email", headers=gateway["alice"]).json()["watcher"]
    assert status["state"] == "watching" and status["received"] == 1 and status["cursor"]["last_uid"] == uid


def test_cursor_advances_only_after_the_event_is_durable(gateway, imap, smtp, monkeypatch) -> None:
    from abstractruntime.email import JsonFileEventInbox

    _connect(gateway, "alice", imap, smtp, ALICE)
    w = _watcher("alice")
    w.poll_once()
    imap.add_message("INBOX", message("m1"))

    def broken_append(self, **kwargs):
        raise OSError("disk full")

    with monkeypatch.context() as m:
        m.setattr(JsonFileEventInbox, "append", broken_append)
        with pytest.raises(OSError):
            w.poll_once()
    # The failed store did not move the cursor: the message is read again and stored now.
    assert w.poll_once()["new"] == 1
    assert [e["subject"] for e in _events(w)] == ["m1"]


def test_crash_between_store_and_cursor_admits_once(gateway, imap, smtp, monkeypatch) -> None:
    from abstractruntime.email import JsonFileEventInbox

    _connect(gateway, "alice", imap, smtp, ALICE)
    w = _watcher("alice")
    w.poll_once()
    imap.add_message("INBOX", message("m1"))
    real_set = JsonFileEventInbox.set_stream_state

    def crash_on_cursor(self, stream, state):
        raise RuntimeError("crash after the durable append, before the cursor")

    with monkeypatch.context() as m:
        m.setattr(JsonFileEventInbox, "set_stream_state", crash_on_cursor)
        with pytest.raises(RuntimeError):
            w.poll_once()
    assert len(_events(w)) == 1  # stored before the crash
    assert JsonFileEventInbox.set_stream_state is real_set
    out = w.poll_once()
    assert out["new"] == 0 and out["skipped"] == 1  # re-read, admitted once (unique event id)
    assert len(_events(w)) == 1


def test_uidvalidity_reset_loses_nothing_and_doubles_nothing(gateway, imap, smtp) -> None:
    _connect(gateway, "alice", imap, smtp, ALICE)
    w = _watcher("alice")
    w.poll_once()
    imap.add_message("INBOX", message("a", message_id="<a@example.test>"))
    imap.add_message("INBOX", message("b", message_id="<b@example.test>"))
    assert w.poll_once()["new"] == 2

    imap.reset_uidvalidity("INBOX")
    imap.add_message("INBOX", message("c", message_id="<c@example.test>"))
    out = w.poll_once()
    assert out["reset"] is True and out["new"] == 1
    assert sorted(e["subject"] for e in _events(w)) == ["a", "b", "c"]
    imap.add_message("INBOX", message("d", message_id="<d@example.test>"))
    assert w.poll_once()["new"] == 1  # continues in the new epoch
    assert any(e["event"] == "email.cursor_reset" and e["user_id"] == "alice" for e in _audit(gateway))


def test_one_bad_message_is_passed_after_three_polls(gateway, imap, smtp, monkeypatch) -> None:
    from abstractcore.comms.email import EmailClient, EmailProtocolError

    _connect(gateway, "alice", imap, smtp, ALICE)
    w = _watcher("alice")
    w.poll_once()
    bad = imap.add_message("INBOX", message("bad"))
    imap.add_message("INBOX", message("good"))
    real_get = EmailClient.get

    def flaky_get(self, uid, **kw):
        if int(uid) == bad:
            raise EmailProtocolError("The IMAP server refused the FETCH command.", "Retry.")
        return real_get(self, uid, **kw)

    monkeypatch.setattr(EmailClient, "get", flaky_get)
    for _ in range(2):
        assert w.poll_once(force=True)["new"] == 0
    out = w.poll_once(force=True)
    assert out["new"] == 1 and out["unprocessable"] == 1
    assert [e["subject"] for e in _events(w)] == ["good"]
    rows = [e for e in _audit(gateway) if e["event"] == "email.message_unprocessable"]
    assert rows and rows[0]["uid"] == bad and rows[0]["code"] == "email_protocol_error" and rows[0]["fix"]


def test_connection_failure_names_cause_and_fix_and_backs_off(gateway, imap, smtp) -> None:
    _connect(gateway, "alice", imap, smtp, ALICE)
    w = _watcher("alice")
    w.poll_once()
    imap.users[ALICE] = "password-changed-at-the-provider"
    out = w.poll_once()
    assert out["error"]["code"] == "email_auth_failed" and out["error"]["cause"] and out["error"]["fix"]
    status = gateway["client"].get("/api/gateway/me/email", headers=gateway["alice"]).json()
    assert status["watcher"]["state"] == "needs action"
    assert status["watcher"]["last_error"]["code"] == "email_auth_failed"
    assert status["watcher"]["next_poll_after"]  # capped backoff, nothing paused
    assert status["status"]["last_error"]["code"] == "email_auth_failed"
    admin_view = gateway["client"].get("/api/gateway/admin/users/alice/email", headers=ADMIN).json()
    assert admin_view["state"] == "needs action"


def test_no_consumer_no_polling(gateway, imap, smtp) -> None:
    _connect(gateway, "alice", imap, smtp, ALICE)
    before = len(imap.commands)
    out = _watcher("alice", has_consumers=lambda: False).poll_once()
    assert out["state"].startswith("idle")
    assert len(imap.commands) == before


def test_the_first_email_automation_gets_its_baseline_at_the_next_tick(gateway, imap, smtp) -> None:
    # 0.7.1 Linux end-to-end: the idle check (no email automation yet) counted as a poll, so the
    # baseline came up to a minute after the first automation was created and a message arriving
    # in that minute was absorbed as history. Gates read nothing and never delay the next check.
    _connect(gateway, "alice", imap, smtp, ALICE)
    now = [1000.0]
    wanted = [False]
    w = _watcher("alice", has_consumers=lambda: wanted[0], clock=lambda: now[0])

    assert w.poll_once()["state"].startswith("idle")
    wanted[0] = True  # the user creates an email automation
    now[0] += 5
    assert w.due(), "an idle check must not start the 60 s cadence"
    assert w.poll_once()["state"] == "watching"  # the baseline, seconds after the automation
    imap.add_message("INBOX", message("right after the automation"))
    now[0] += 5
    assert not w.due(), "a real read starts the 60 s cadence"
    now[0] += 60
    assert w.due() and w.poll_once()["new"] == 1
    assert [e["subject"] for e in _events(w)] == ["right after the automation"]


def test_mail_from_a_time_without_email_automations_is_history(gateway, imap, smtp) -> None:
    # The watcher reads nothing while no email automation exists, so its cursor goes stale; the
    # next automation's first read delivered everything since then as new mail (stamped after
    # the new automation's start), and it ran on mail older than itself.
    _connect(gateway, "alice", imap, smtp, ALICE)
    wanted = [True]
    w = _watcher("alice", has_consumers=lambda: wanted[0])
    assert w.poll_once()["state"] == "watching"  # baseline with a first automation
    imap.add_message("INBOX", message("while watched"))
    assert w.poll_once()["new"] == 1

    wanted[0] = False  # the only email automation is archived (or paused)
    assert w.poll_once()["state"].startswith("idle")
    imap.add_message("INBOX", message("while nobody watched"))
    wanted[0] = True  # a new one is created (or the paused one resumed)
    assert w.poll_once()["new"] == 0, "mail from the time without automations is history"
    imap.add_message("INBOX", message("after the new automation"))
    assert w.poll_once()["new"] == 1
    assert [e["subject"] for e in _events(w)] == ["while watched", "after the new automation"]


def test_a_failed_consumer_probe_keeps_the_cursor(gateway, imap, smtp) -> None:
    # A probe that fails says nothing about the automations: the cursor is kept and nothing
    # that arrived meanwhile is dropped.
    _connect(gateway, "alice", imap, smtp, ALICE)
    state = {"fail": False}

    def probe() -> bool:
        if state["fail"]:
            raise RuntimeError("runtime not ready")
        return True

    w = _watcher("alice", has_consumers=probe)
    assert w.poll_once()["state"] == "watching"
    state["fail"] = True
    assert w.poll_once()["state"].startswith("idle")
    imap.add_message("INBOX", message("during the failed probe"))
    state["fail"] = False
    assert w.poll_once()["new"] == 1


def test_user_and_admin_switches_keep_the_mailbox_untouched(gateway, imap, smtp) -> None:
    _connect(gateway, "alice", imap, smtp, ALICE)
    before = len(imap.commands)
    gateway["client"].put("/api/gateway/me/email/enabled", headers=gateway["alice"], json={"enabled": False})
    assert _watcher("alice").poll_once()["state"] == "off (turned off by the user)"
    gateway["client"].put("/api/gateway/me/email/enabled", headers=gateway["alice"], json={"enabled": True})
    gateway["client"].put("/api/gateway/admin/users/alice/email", headers=ADMIN, json={"enabled": False})
    assert _watcher("alice").poll_once()["state"] == "off (turned off by an administrator)"
    assert len(imap.commands) == before


def test_each_watcher_feeds_only_its_own_user(gateway, imap, imap_bob, smtp) -> None:
    _connect(gateway, "alice", imap, smtp, ALICE)
    _connect(gateway, "bob", imap_bob, smtp, BOB)
    wa, wb = _watcher("alice"), _watcher("bob")
    wa.poll_once()
    wb.poll_once()
    imap.add_message("INBOX", message("for alice"))
    imap_bob.add_message("INBOX", message("for bob", to=BOB))
    wa.poll_once()
    wb.poll_once()
    assert [e["subject"] for e in _events(wa)] == ["for alice"]
    assert [e["subject"] for e in _events(wb)] == ["for bob"]
    assert wa.inbox.base_dir != wb.inbox.base_dir


def test_reconnecting_starts_a_fresh_baseline(gateway, imap, smtp) -> None:
    _connect(gateway, "alice", imap, smtp, ALICE)
    w = _watcher("alice")
    w.poll_once()
    imap.add_message("INBOX", message("before reconnect"))
    _connect(gateway, "alice", imap, smtp, ALICE)  # connect again: a new baseline
    assert w.poll_once()["new"] == 0
    imap.add_message("INBOX", message("after reconnect"))
    assert w.poll_once()["new"] == 1
    assert [e["subject"] for e in _events(w)] == ["after reconnect"]
