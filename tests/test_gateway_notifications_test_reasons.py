"""`POST /me/notifications/test` always answers with a sentence and a reason (DESIGN-v2 §3.3,
§6, item 8): sent / no mailbox / paused / hourly limit (with the numbers and the reset time) /
queued behind earlier notices / the server refused (with the cause). The outbox's rate-limit
requeue keeps the reason on the rows so later reads can explain the wait too."""

from __future__ import annotations

import datetime

import pytest

from email_fixtures import *  # noqa: F401,F403 - fixtures
from email_fixtures import ALICE, connect_body, plane_of

pytestmark = pytest.mark.integration

TEST = "/api/gateway/me/notifications/test"


def _connect(gateway, imap, smtp) -> None:
    r = gateway["client"].put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(ALICE, imap, smtp))
    assert r.status_code == 200, r.text


def test_sent_says_where(gateway, imap, smtp) -> None:
    _connect(gateway, imap, smtp)
    body = gateway["client"].post(TEST, headers=gateway["alice"]).json()
    assert body["ok"] is True and body["sent"] is True and body["reason_code"] is None
    assert body["message"] == f"Sent to {ALICE}." and body["limit"] is None


def test_no_mailbox_and_paused_mailbox(gateway, imap, smtp) -> None:
    c = gateway["client"]
    body = c.post(TEST, headers=gateway["alice"]).json()
    assert body["sent"] is False and body["reason_code"] == "no_mailbox"
    assert body["message"] == "Not sent: no mailbox connected."
    _connect(gateway, imap, smtp)
    assert c.put("/api/gateway/me/email/enabled", headers=gateway["alice"], json={"enabled": False}).status_code == 200
    body = c.post(TEST, headers=gateway["alice"]).json()
    assert body["reason_code"] == "mailbox_paused" and body["message"] == "Not sent: your mailbox is paused."
    assert smtp.messages == []


def test_hourly_limit_names_the_numbers_and_the_reset_time_then_queues_behind(gateway, imap, smtp) -> None:
    from abstractgateway.mail.accounts import account_store
    from abstractgateway.mail.notifications import NotificationOutbox

    c = gateway["client"]
    _connect(gateway, imap, smtp)
    account_store(plane_of("alice")).set_limits(per_hour=1, per_day=100)
    assert c.post(TEST, headers=gateway["alice"]).json()["sent"] is True

    body = c.post(TEST, headers=gateway["alice"]).json()
    assert body["sent"] is False and body["reason_code"] == "rate_limited"
    limit = body["limit"]
    assert limit["window"] == "hour" and limit["limit"] == 1 and limit["used"] == 1
    resets = datetime.datetime.fromisoformat(limit["resets_at"])
    hhmm = resets.astimezone().strftime("%H:%M")  # the gateway's local time
    local_day = resets.astimezone().date()
    expected_at = hhmm if local_day == datetime.date.today() else f"tomorrow at {hhmm}"
    assert body["message"] == f"Not sent: hourly limit reached (1 of 1 this hour) — resets at {expected_at}."

    # The row keeps why it waits and when it goes; the outbox summary explains it later.
    row = next(r for r in NotificationOutbox(plane_of("alice")).rows(state="queued"))
    assert row["error_code"] == "email_rate_limited" and row["error_cause"] and row["next_attempt_ts"] > 0
    summary = c.get("/api/gateway/me/notifications", headers=gateway["alice"]).json()["outbox"]
    assert summary["rate_limited"]["count"] == 1 and summary["rate_limited"]["resets_at"]

    # A third click waits behind the second.
    body = c.post(TEST, headers=gateway["alice"]).json()
    assert body["reason_code"] == "queued_behind" and body["queued_behind"] == 1
    assert body["message"] == f"Queued behind 1 earlier notification; they go out when the limit resets at {expected_at}."
    assert len(smtp.messages) == 1


def test_refused_by_the_server_names_the_host_and_the_cause(gateway, imap, smtp) -> None:
    _connect(gateway, imap, smtp)
    smtp.users[ALICE] = "rotated-password"
    body = gateway["client"].post(TEST, headers=gateway["alice"]).json()
    assert body["sent"] is False and body["reason_code"] == "send_failed"
    assert body["message"].startswith("Not sent: localhost refused the sign-in — check the mailbox password (")
    assert body["error"]["code"] == "email_auth_failed"


def test_mailbox_test_answers_with_a_sentence(gateway, imap, smtp) -> None:
    c = gateway["client"]
    _connect(gateway, imap, smtp)
    body = c.post("/api/gateway/me/email/test", headers=gateway["alice"]).json()
    assert body["ok"] is True and body["message"] == "Test passed: signed in to localhost and localhost."
    smtp.users[ALICE] = "rotated-password"
    body = c.post("/api/gateway/me/email/test", headers=gateway["alice"]).json()
    assert body["ok"] is False
    assert body["message"].startswith("Sign-in refused by localhost — check the password.")
    # An unknown user on an admin email route says so in a sentence.
    r = c.get("/api/gateway/admin/users/nobody/email", headers={"Authorization": "Bearer admin-token-email-tests"})
    assert r.status_code == 404 and r.json()["detail"]["message"] == "There is no user named 'nobody' on this gateway."
