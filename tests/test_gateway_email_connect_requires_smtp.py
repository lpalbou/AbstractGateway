"""A connected mailbox can always send (operator report 2026-10-01, Mac mini): "Connected as …"
followed by "Not sent: This account has no SMTP (send) settings." on the first test.

Connect stores BOTH legs: a leg the form or the discovery left out is the domain's standard one
(imap./smtp.<domain>, 993/465 SSL — `server_defaults`) and the connect test signs in to both —
an unreachable SMTP server refuses the connect with its step, nothing is stored. A mailbox
stored before this rule (no SMTP leg) reads as `receive_only` wherever the mailbox state is
shown.
"""
from __future__ import annotations

import socket
from typing import Any, Dict

import pytest
from email_fixtures import *  # noqa: F401,F403 - fixtures
from email_fixtures import ALICE, PASSWORDS, plane_of

pytestmark = pytest.mark.integration


def _imap_only_body(imap) -> Dict[str, Any]:
    return {"address": ALICE, "password": PASSWORDS[ALICE], "imap": {"host": "localhost", "port": imap.port, "security": "ssl"}}


def _free_loopback_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def standard_smtp_is(monkeypatch: pytest.MonkeyPatch):
    """Point the domain's "standard" SMTP leg (what fills a missing one) at a loopback port."""

    import abstractgateway.mail.accounts as mail_accounts

    real = mail_accounts.server_defaults

    def install(port: int, security: str) -> None:
        def defaults(address: str, discovered: Any = None) -> Dict[str, Any]:
            out = dict(real(address, discovered=discovered if isinstance(discovered, dict) else {"found": False}))
            out["smtp"] = {"host": "localhost", "port": port, "security": security}
            return out

        monkeypatch.setattr(mail_accounts, "server_defaults", defaults)

    return install


def test_connect_with_imap_only_fills_the_smtp_leg_and_tests_it(gateway, imap, smtp, standard_smtp_is) -> None:
    standard_smtp_is(smtp.port, "starttls")
    c = gateway["client"]
    body = c.put("/api/gateway/me/email", headers=gateway["alice"], json=_imap_only_body(imap)).json()
    assert body["ok"] is True, body
    # Stored WITH the filled leg; the status says it can send; the test signed in to both.
    assert isinstance(body.get("smtp"), dict) and body["smtp"]["port"] == smtp.port
    assert body["send_capable"] is True and body["mailbox"]["state"] == "connected"
    probe = c.post("/api/gateway/me/email/test", headers=gateway["alice"]).json()
    assert probe["ok"] is True and probe["smtp"]["ok"] is True, probe


def test_connect_refuses_when_the_filled_smtp_leg_cannot_be_reached(gateway, imap, standard_smtp_is) -> None:
    standard_smtp_is(_free_loopback_port(), "ssl")
    c = gateway["client"]
    r = c.put("/api/gateway/me/email", headers=gateway["alice"], json=_imap_only_body(imap))
    # Never a "Connected" mailbox that cannot send: the connect fails on its SMTP step and
    # stores nothing.
    assert r.status_code in (400, 422), r.text
    detail = r.json().get("detail") or {}
    assert detail.get("step") == "smtp", detail
    me = c.get("/api/gateway/me/email", headers=gateway["alice"]).json()
    assert me["configured"] is False and me["mailbox"]["state"] == "not_connected"


def test_discovery_without_an_smtp_server_still_connects_both_legs(gateway, imap, smtp, standard_smtp_is, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.mail.accounts as mail_accounts

    def imap_only(address: str, **_: Any) -> Dict[str, Any]:
        return {
            "found": True,
            "imap": {"host": "localhost", "port": imap.port, "security": "ssl"},
            "smtp": None,
            "username": address,
            "source": "autoconfig",
            "provider": None,
            "tried": ["autoconfig"],
        }

    monkeypatch.setattr(mail_accounts, "require_servers", imap_only)
    standard_smtp_is(smtp.port, "starttls")
    c = gateway["client"]
    body = c.put("/api/gateway/me/email", headers=gateway["alice"], json={"address": ALICE, "password": PASSWORDS[ALICE]}).json()
    assert body["ok"] is True and body["discovery"]["source"] == "autoconfig", body
    assert isinstance(body.get("smtp"), dict) and body["send_capable"] is True


def test_a_mailbox_stored_without_smtp_reads_receive_only(gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.mail.accounts as mail_accounts

    class _Store:
        def public(self) -> Dict[str, Any]:
            return {"configured": True, "enabled": True, "address": ALICE, "imap": {"host": "localhost"}, "smtp": None, "oauth": None}

    monkeypatch.setattr(mail_accounts, "account_store", lambda plane: _Store())
    monkeypatch.setattr(mail_accounts, "admin_email_enabled", lambda plane: True)
    view = mail_accounts.mailbox_view(plane_of("alice"))
    assert view["state"] == "receive_only" and view["address"] == ALICE
    assert "receive only" in view["reason"]
