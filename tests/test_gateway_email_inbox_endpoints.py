from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def test_gateway_email_inbox_endpoints_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    from abstractcore.tools import comms_tools

    monkeypatch.setattr(
        comms_tools,
        "list_email_accounts",
        lambda: {
            "success": True,
            "source": "yaml",
            "config_path": "configs/emails.yaml",
            "default_account": "default",
            "accounts": [
                {
                    "account": "default",
                    "email": "alpha@example.com",
                    "from_email": "alpha@example.com",
                    "can_read": True,
                    "can_send": True,
                    "imap_password_set": True,
                    "smtp_password_set": True,
                },
                {
                    "account": "contact",
                    "email": "contact@example.com",
                    "from_email": "contact@example.com",
                    "can_read": True,
                    "can_send": True,
                    "imap_password_set": True,
                    "smtp_password_set": True,
                },
            ],
        },
    )

    monkeypatch.setattr(
        comms_tools,
        "list_emails",
        lambda **kwargs: {
            "success": True,
            "account": kwargs.get("account") or "default",
            "mailbox": kwargs.get("mailbox") or "INBOX",
            "filter": {
                "since": "2026-02-04T00:00:00+00:00",
                "status": kwargs.get("status") or "all",
                "limit": kwargs.get("limit") or 20,
            },
            "counts": {"returned": 1, "unread": 1, "read": 0},
            "messages": [
                {
                    "uid": "123",
                    "message_id": "<m1@example.com>",
                    "subject": "Hello",
                    "from": "sender@example.com",
                    "to": "alpha@example.com",
                    "date": "Tue, 04 Feb 2026 00:00:00 +0000",
                    "flags": ["\\Seen"],
                    "seen": False,
                    "size": 42,
                }
            ],
        },
    )

    monkeypatch.setattr(
        comms_tools,
        "read_email",
        lambda **kwargs: {
            "success": True,
            "account": kwargs.get("account") or "default",
            "mailbox": kwargs.get("mailbox") or "INBOX",
            "uid": kwargs.get("uid") or "",
            "message_id": "<m1@example.com>",
            "subject": "Hello",
            "from": "sender@example.com",
            "to": "alpha@example.com",
            "cc": "",
            "date": "Tue, 04 Feb 2026 00:00:00 +0000",
            "flags": ["\\Seen"],
            "seen": True,
            "body_text": "Hi",
            "body_html": "<p>Hi</p>",
            "attachments": [{"filename": "a.txt", "content_type": "text/plain"}],
        },
    )

    monkeypatch.setattr(
        comms_tools,
        "send_email",
        lambda **kwargs: {
            "success": True,
            "account": kwargs.get("account") or "default",
            "message_id": "<sent@example.com>",
            "from": "alpha@example.com",
            "to": ["to@example.com"],
            "cc": [],
            "bcc": [],
            "smtp": {"host": "smtp.example.com", "port": 587, "username": "alpha@example.com", "starttls": True},
        },
    )

    from abstractgateway.app import app

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}

    resp = client.get("/api/gateway/email/accounts", headers=headers)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["default_account"] == "default"
    assert {a["account"] for a in payload["accounts"]} == {"default", "contact"}

    resp2 = client.get("/api/gateway/email/messages", params={"account": "default", "since": "1d", "status": "unread", "limit": 5}, headers=headers)
    assert resp2.status_code == 200, resp2.text
    payload2 = resp2.json()
    assert payload2["ok"] is True
    assert payload2["account"] == "default"
    assert payload2["messages"][0]["uid"] == "123"
    assert payload2["messages"][0]["from"] == "sender@example.com"

    resp3 = client.get("/api/gateway/email/messages/123", params={"account": "default", "max_body_chars": 5000}, headers=headers)
    assert resp3.status_code == 200, resp3.text
    payload3 = resp3.json()
    assert payload3["ok"] is True
    assert payload3["uid"] == "123"
    assert payload3["from"] == "sender@example.com"
    assert payload3["attachments"][0]["filename"] == "a.txt"

    resp4 = client.post(
        "/api/gateway/email/send",
        json={"account": "default", "to": "to@example.com", "subject": "Hi", "body_text": "Hi"},
        headers=headers,
    )
    assert resp4.status_code == 200, resp4.text
    payload4 = resp4.json()
    assert payload4["ok"] is True
    assert payload4["account"] == "default"
    assert payload4["from"] == "alpha@example.com"
    assert payload4["to"] == ["to@example.com"]

