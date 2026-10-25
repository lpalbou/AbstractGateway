"""`POST /api/gateway/triggers/sample`: what a triggered flow would receive, from the caller's own
real data (AbstractRuntime's `sample_occurrence_inputs`), for Flow's Run dialog: read-only (no
cursor moves, no event appended, nothing marked read), principal-scoped (another account's
mailbox or connection answers 404), validated like an automation's creation."""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from email_fixtures import *  # noqa: F401,F403 - fixtures (fake IMAP/SMTP, CA, keychain guard)
from email_fixtures import ALICE, connect_body, message
from test_gateway_trigger_connections import BASE, RSS, _create_webhook, _sign, gw  # noqa: F401 - fixture

URL = "/api/gateway/triggers/sample"


def _sample(c, headers, trigger, **extra):
    return c.post(URL, headers=headers, json={"trigger": trigger, **extra})


def test_webhook_sample_is_a_documented_call_until_one_arrives_then_the_real_ones(gw) -> None:
    c, _ref, alice, bob = gw
    created = _create_webhook(c, alice)
    cid = created["connection_id"]
    trig = {"source_id": "webhook.received", "source_version": 1, "config": {"connection_id": cid}}
    first = _sample(c, alice, trig)
    assert first.status_code == 200, first.text
    out = first.json()
    assert out["summary"] == "A sample call (this connection has not received one yet)" and out["truncated"] is False
    assert out["inputs"]["event"]["abstracttest"] is True and isinstance(out["inputs"]["calls"], list)

    body = b'{"order": 7}'
    assert c.post(f"/api/gateway/triggers/webhooks/{cid}", content=body, headers=_sign(created["secret"], body, "s-1")).status_code == 202
    real = _sample(c, alice, trig).json()
    assert real["summary"] == "Last 1 call received by this webhook" and real["count"] == 1
    assert real["inputs"]["calls"][0]["body"] == {"order": 7}
    # Read-only: the stream still holds exactly the one real call.
    assert len(c.get(f"{BASE}/{cid}/events", headers=alice).json()["events"]) == 1
    # Another account's connection reads like an unknown one.
    stolen = _sample(c, bob, trig)
    assert (stolen.status_code, stolen.json()["detail"]["reason_code"]) == (404, "connection_not_found")


def test_feed_sample_reads_the_feed_without_moving_anything(gw) -> None:
    class Feed(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
            self.end_headers()
            self.wfile.write(RSS)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Feed)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        c, _ref, alice, _bob = gw
        url = f"http://127.0.0.1:{server.server_address[1]}/feed.xml"
        cid = c.post(BASE, headers=alice, json={"source_id": "feed.item", "source_version": 1, "config": {"url": url}}).json()["connection_id"]
        out = _sample(c, alice, {"source_id": "feed.item", "source_version": 1, "config": {"connection_id": cid}}, count=1).json()
        assert out["count"] == 1 and len(out["inputs"]["items"]) == 1 and out["summary"] == "Latest 1 item of t"
        assert c.get(f"{BASE}/{cid}/events", headers=alice).json()["events"] == []  # nothing appended
    finally:
        server.shutdown()


def test_schedule_and_validation(gw) -> None:
    c, _ref, alice, _bob = gw
    out = _sample(c, alice, {"source_id": "schedule", "source_version": 1, "config": {"every": "1h"}}).json()
    assert out["summary"] == "One occurrence, as if the schedule fired now" and out["inputs"]["scheduled_at"]
    bad_source = _sample(c, alice, {"source_id": "nope", "source_version": 1, "config": {}})
    assert (bad_source.status_code, bad_source.json()["detail"]["reason_code"]) == (422, "invalid_sample")
    assert _sample(c, alice, {"source_id": "schedule", "source_version": 1, "config": {"every": "1h"}}, count=501).status_code == 422
    assert _sample(c, alice, {"source_id": "schedule"}).status_code == 422
    assert c.post(URL, json={"trigger": {"source_id": "schedule", "source_version": 1, "config": {}}}).status_code == 401


def test_email_sample_reads_the_newest_messages_read_only(gw, imap, smtp) -> None:
    c, _ref, alice, bob = gw
    assert c.put("/api/gateway/me/email", headers=alice, json=connect_body(ALICE, imap, smtp)).status_code == 200
    uids = [imap.add_message("INBOX", message(f"m{i}")) for i in range(3)]
    trig = {"source_id": "email.received", "source_version": 1, "config": {"account": "self"}}
    r = _sample(c, alice, trig, count=2)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["summary"] == f"Last 2 messages of {ALICE}" and out["count"] == 2
    assert [m["subject"] for m in out["inputs"]["messages"]] == ["m2", "m1"]  # newest first, bodies resolved
    assert isinstance(out["inputs"]["messages"][0].get("body_text"), str)
    for uid in uids:
        assert "\\Seen" not in imap.flags_of("INBOX", uid)
    # A mailbox the caller does not have: 404; bob has none at all: 409.
    other = _sample(c, alice, {**trig, "config": {"account": "work"}})
    assert (other.status_code, other.json()["detail"]["reason_code"]) == (404, "email_account_not_found")
    none = _sample(c, bob, trig)
    assert (none.status_code, none.json()["detail"]["reason_code"]) == (409, "email_not_configured")


def test_a_further_mailbox_sample_names_its_address(gw, imap, imap_bob, smtp) -> None:
    from email_fixtures import BOB, PASSWORDS

    c, _ref, alice, _bob = gw
    assert c.put("/api/gateway/me/email", headers=alice, json=connect_body(ALICE, imap, smtp)).status_code == 200
    work = {"address": BOB, "password": PASSWORDS[BOB], "imap": {"host": "localhost", "port": imap_bob.port, "security": "ssl"}}
    assert c.put("/api/gateway/me/email/accounts/work", headers=alice, json=work).status_code == 200
    imap_bob.add_message("INBOX", message("to work", to=BOB))
    out = _sample(c, alice, {"source_id": "email.received", "source_version": 1, "config": {"account": "work"}}, count=5).json()
    assert out["summary"] == f"Last 1 message of {BOB}", out
    assert [m["subject"] for m in out["inputs"]["messages"]] == ["to work"]
