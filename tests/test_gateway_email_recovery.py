"""Account recovery by email (framework backlog 0992, operator addition 2026-09-29):
"Forgot your token?" and "Email me a sign-in code" — single use, 10-minute expiry, stored
hashed, rate-limited per account and per client address, the same answer whether or not the
account exists or has email (no enumeration), audited without the code, sent through the
user's own account, offered only when some account has email."""

from __future__ import annotations

import json

import pytest

from email_fixtures import *  # noqa: F401,F403 - fixtures
from email_fixtures import ADMIN, ALICE, code_from, connect_body, smtp_bodies

pytestmark = pytest.mark.integration


def _connect(gateway, imap, smtp) -> None:
    r = gateway["client"].put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(ALICE, imap, smtp))
    assert r.status_code == 200, r.text


def _request(client, user_id: str, purpose: str = "sign_in"):
    from abstractgateway.mail import recovery

    r = client.post("/api/gateway/session/recovery/request", json={"user_id": user_id, "purpose": purpose})
    recovery.drain()
    return r


def _redeem(client, user_id: str, code: str, purpose: str = "sign_in"):
    return client.post("/api/gateway/session/recovery/redeem", json={"user_id": user_id, "purpose": purpose, "code": code})


def _fresh_client(gateway):
    from fastapi.testclient import TestClient

    return TestClient(gateway["app"])


def test_options_are_offered_only_when_some_account_has_email(gateway, imap, smtp) -> None:
    c = _fresh_client(gateway)
    assert c.get("/api/gateway/session/recovery").json()["available"] is False
    _connect(gateway, imap, smtp)
    body = c.get("/api/gateway/session/recovery").json()
    assert body["available"] is True and body["purposes"] == ["sign_in", "reset_token"]
    gateway["client"].put("/api/gateway/admin/users/alice/email", headers=ADMIN, json={"enabled": False})
    assert c.get("/api/gateway/session/recovery").json()["available"] is False


def test_constant_answer_no_enumeration(gateway, imap, smtp) -> None:
    _connect(gateway, imap, smtp)
    c = _fresh_client(gateway)
    answers = [_request(c, uid).json() for uid in ("alice", "bob", "nobody-here")]
    assert answers[0] == answers[1] == answers[2]
    assert answers[0]["ok"] is True
    # Only Alice (email configured) actually got a code.
    mails = smtp_bodies(smtp)
    assert len(mails) == 1 and mails[0]["to"] == [ALICE] and mails[0]["from"] == ALICE


def test_sign_in_code_opens_a_session_once(gateway, imap, smtp) -> None:
    _connect(gateway, imap, smtp)
    c = _fresh_client(gateway)
    assert _request(c, "alice").status_code == 200
    code = code_from(smtp_bodies(smtp)[-1]["text"])

    r = _redeem(c, "alice", code)
    assert r.status_code == 200, r.text
    assert r.json()["principal"]["user_id"] == "alice" and "token" not in r.json()
    assert c.get("/api/gateway/me").json()["principal"]["user_id"] == "alice"  # the session works

    again = _redeem(_fresh_client(gateway), "alice", code)
    assert again.status_code == 401 and again.json()["detail"]["reason_code"] == "recovery_code_refused"


def test_code_is_stored_hashed_and_audited_without_the_code(gateway, imap, smtp) -> None:
    _connect(gateway, imap, smtp)
    c = _fresh_client(gateway)
    _request(c, "alice")
    code = code_from(smtp_bodies(smtp)[-1]["text"])
    stored = (gateway["data_dir"] / "auth" / "recovery_codes.json").read_text(encoding="utf-8")
    assert code not in stored
    assert json.loads(stored)["codes"][0]["hash"]
    assert _redeem(c, "alice", code).status_code == 200
    audit = (gateway["data_dir"] / "audit_log.jsonl").read_text(encoding="utf-8")
    assert code not in audit
    events = [json.loads(line).get("event") for line in audit.splitlines()]
    assert "email.recovery_code_issued" in events and "email.recovery_code_used" in events


def test_code_expires_after_ten_minutes(gateway, imap, smtp, monkeypatch) -> None:
    import time as time_mod

    from abstractgateway.mail import recovery

    _connect(gateway, imap, smtp)
    c = _fresh_client(gateway)
    _request(c, "alice")
    code = code_from(smtp_bodies(smtp)[-1]["text"])
    real = time_mod.time
    monkeypatch.setattr(recovery.time, "time", lambda: real() + recovery.CODE_TTL_S + 1)
    assert _redeem(c, "alice", code).status_code == 401


def test_wrong_codes_burn_the_code(gateway, imap, smtp) -> None:
    _connect(gateway, imap, smtp)
    c = _fresh_client(gateway)
    _request(c, "alice")
    code = code_from(smtp_bodies(smtp)[-1]["text"])
    wrong = "00000000" if code != "00000000" else "11111111"
    for _ in range(5):
        assert _redeem(c, "alice", wrong).status_code == 401
    assert _redeem(c, "alice", code).status_code == 401  # 5 wrong tries: the code is gone


def test_rate_limited_per_account_and_per_client(gateway, imap, smtp, monkeypatch) -> None:
    from abstractgateway.mail import recovery

    _connect(gateway, imap, smtp)
    c = _fresh_client(gateway)
    for _ in range(recovery.ACCOUNT_MAX_REQUESTS + 2):
        assert _request(c, "alice").status_code == 200
    assert len(smtp.messages) == recovery.ACCOUNT_MAX_REQUESTS

    # Per client address: a burst over other names is capped too (and answers the same).
    monkeypatch.setattr(recovery, "ACCOUNT_MAX_REQUESTS", 1000)
    for i in range(recovery.IP_MAX_REQUESTS + 3):
        assert _request(c, "alice" if i % 2 else f"ghost{i}").status_code == 200
    audit = (gateway["data_dir"] / "audit_log.jsonl").read_text(encoding="utf-8")
    reasons = [json.loads(line).get("reason") for line in audit.splitlines() if '"email.recovery_code_refused"' in line]
    assert "rate_limited_account" in reasons and "rate_limited_client" in reasons


def test_reset_token_rotates_the_token_once(gateway, imap, smtp) -> None:
    _connect(gateway, imap, smtp)
    c = _fresh_client(gateway)
    old_headers = gateway["alice"]
    _request(c, "alice", "reset_token")
    mail = smtp_bodies(smtp)[-1]
    assert "new gateway token" in mail["subject"]
    code = code_from(mail["text"])
    r = _redeem(c, "alice", code, "reset_token")
    assert r.status_code == 200, r.text
    new_token = r.json()["token"]
    assert new_token and new_token != gateway["alice_token"]
    other = _fresh_client(gateway)
    assert other.get("/api/gateway/me/email", headers=old_headers).status_code == 401
    assert other.get("/api/gateway/me/email", headers={"Authorization": f"Bearer {new_token}"}).status_code == 200
    # A sign-in code is not a reset code.
    _request(c, "alice", "sign_in")
    code2 = code_from(smtp_bodies(smtp)[-1]["text"])
    assert _redeem(_fresh_client(gateway), "alice", code2, "reset_token").status_code == 401


def test_no_email_no_code_and_disabled_users_get_nothing(gateway, imap, smtp) -> None:
    from abstractgateway.users import GatewayUserRegistry

    _connect(gateway, imap, smtp)
    GatewayUserRegistry().update_user(user_id="alice", enabled=False)
    c = _fresh_client(gateway)
    _request(c, "alice")
    _request(c, "bob")
    assert smtp.messages == []
