"""Mailbox form defaults (DESIGN-v2 §3, §6, items 6-7): `POST /me/email/discover` carries the
pre-filled server fields from AbstractCore's `server_defaults`; Connect needs no user name and
no display name (stored name kept, else the address's local part); connecting sets "Your email
address" when it is empty (audited), never when it is set."""

from __future__ import annotations

import json

import pytest

from email_fixtures import *  # noqa: F401,F403 - fixtures
from email_fixtures import ALICE, BOB, PASSWORDS, connect_body

pytestmark = pytest.mark.integration


@pytest.fixture
def no_discovery_network(monkeypatch):
    from abstractcore.comms.email import discovery

    monkeypatch.setattr(discovery._net, "http_get", lambda url, timeout: None)
    monkeypatch.setattr(discovery._net, "resolve_srv", lambda name, timeout: [])
    monkeypatch.setattr(discovery._net, "resolve_mx", lambda domain, timeout: [])


def test_discover_carries_the_form_defaults(gateway, no_discovery_network) -> None:
    c = gateway["client"]
    known = c.post("/api/gateway/me/email/discover", headers=gateway["alice"], json={"address": "me@fastmail.com"}).json()
    d = known["defaults"]
    assert d["source"] == "discovered" and d["imap"]["host"] == "imap.fastmail.com" and d["smtp"]["host"] == "smtp.fastmail.com"
    assert d["login"] and d["message"] == "Settings found for fastmail.com."
    unknown = c.post("/api/gateway/me/email/discover", headers=gateway["alice"], json={"address": ALICE}).json()
    assert unknown["found"] is False
    assert unknown["defaults"] == {
        "imap": {"host": "imap.example.test", "port": 993, "security": "ssl"},
        "smtp": {"host": "smtp.example.test", "port": 465, "security": "ssl"},
        "login": ALICE,
        "source": "standard",
        "provider": None,
        "message": "Standard settings for example.test — change them if your provider uses others.",
    }


def test_connect_sets_an_empty_address_and_defaults_the_display_name(gateway, imap, smtp) -> None:
    from abstractgateway.users import GatewayUserRegistry

    c = gateway["client"]
    _rec, token = GatewayUserRegistry().create_user(user_id="carol", roles=["user"])
    carol = {"Authorization": f"Bearer {token}"}
    assert c.get("/api/gateway/me/email", headers=carol).json()["email_address"] == ""

    body = c.put("/api/gateway/me/email", headers=carol, json=connect_body(ALICE, imap, smtp)).json()
    assert body["email_address"] == ALICE and body["display_name"] == "alice" and body["username"] == ALICE
    assert GatewayUserRegistry().get_user("carol").email == ALICE
    events = [json.loads(l) for l in (gateway["data_dir"] / "audit_log.jsonl").read_text().splitlines()]
    changed = [e for e in events if e.get("event") == "email.address_changed" and e.get("user_id") == "carol"]
    assert changed and changed[-1]["reason"] == "mailbox_connected" and changed[-1]["outcome"] == "set"

    # A stored display name is kept when the form sends none.
    named = dict(connect_body(ALICE, imap, smtp), display_name="Alice A.")
    assert c.put("/api/gateway/me/email", headers=carol, json=named).json()["display_name"] == "Alice A."
    assert c.put("/api/gateway/me/email", headers=carol, json=connect_body(ALICE, imap, smtp)).json()["display_name"] == "Alice A."


def test_connect_never_replaces_a_set_address(gateway, imap, smtp) -> None:
    c = gateway["client"]
    body = c.put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(BOB, imap, smtp)).json()
    assert body["address"] == BOB and body["email_address"] == ALICE
