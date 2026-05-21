from __future__ import annotations

import pytest


def test_send_email_notification_uses_runtime_host_facade(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.maintenance import notifier
    from abstractruntime.integrations import abstractcore as runtime_abstractcore

    calls: list[dict[str, object]] = []

    def _send_email(**kwargs: object) -> dict[str, object]:
        calls.append(dict(kwargs))
        return {
            "success": True,
            "account": kwargs.get("account") or "default",
            "message_id": "<sent@example.com>",
            "from": "alpha@example.com",
            "to": list(kwargs.get("to") or []),
            "cc": [],
            "bcc": [],
            "smtp": {"host": "smtp.example.com", "port": 587, "username": "alpha@example.com", "starttls": True},
        }

    monkeypatch.setenv("ABSTRACT_BACKLOG_EMAIL_TO", "a@example.com;b@example.com")
    monkeypatch.setenv("ABSTRACT_BACKLOG_EMAIL_ACCOUNT", "ops")
    monkeypatch.setattr(runtime_abstractcore, "send_email", _send_email)

    ok, err = notifier.send_email_notification(subject="Hello", body_text="Body")

    assert ok is True
    assert err is None
    assert calls == [
        {
            "account": "ops",
            "to": ["a@example.com", "b@example.com"],
            "subject": "Hello",
            "body_text": "Body",
        }
    ]
