"""Account recovery by email (framework backlog 0992, operator addition 2026-09-29):
"Forgot your token?" and "Email me a sign-in code" — single use, 10-minute expiry, stored
hashed, rate-limited per account and per client address, an HONEST answer (DESIGN 2026-09-30
§4.1: sent to a masked address / no_email_address / too_many_requests with retry_after_s; an
unknown account reads like one without an address), audited without the code, sent through the
user's own account, offered only when some account has email; 404 when the admin turned it off."""

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


def test_honest_answers_sent_masked_or_no_email_address(gateway, imap, smtp) -> None:
    from abstractgateway.mail import recovery

    _connect(gateway, imap, smtp)
    c = _fresh_client(gateway)
    sent = _request(c, "alice").json()
    assert sent == {
        "ok": True,
        "sent": True,
        "to": "a\u2022\u2022\u2022@\u2022\u2022\u2022",
        "expires_in_s": 600,
        "message": "A sign-in code is on its way to a\u2022\u2022\u2022@\u2022\u2022\u2022. It expires in 10 minutes.",
    }
    assert "example.test" not in json.dumps(sent)  # never the domain
    # Bob has an email address but no mailbox to send with; "nobody-here" does not exist: the
    # same answer for both (an unknown account is not told apart).
    no_bob = _request(c, "bob").json()
    no_ghost = _request(c, "nobody-here").json()
    assert no_bob == no_ghost == {
        "ok": True,
        "sent": False,
        "reason_code": "no_email_address",
        "message": recovery.NO_EMAIL_ADDRESS_MESSAGE,
    }
    assert no_bob["message"] == "This account has no email address, so a code can't be sent. Ask your gateway admin for a token."
    # Only Alice (email configured) actually got a code.
    mails = smtp_bodies(smtp)
    assert len(mails) == 1 and mails[0]["to"] == [ALICE] and mails[0]["from"] == ALICE


def test_purpose_defaults_to_sign_in(gateway, imap, smtp) -> None:
    from abstractgateway.mail import recovery

    _connect(gateway, imap, smtp)
    c = _fresh_client(gateway)
    r = c.post("/api/gateway/session/recovery/request", json={"user_id": "alice"})
    recovery.drain()
    assert r.status_code == 200 and r.json()["sent"] is True
    mail = smtp_bodies(smtp)[-1]
    assert mail["subject"] == "[AbstractFramework] Your sign-in code"
    r = c.post("/api/gateway/session/recovery/redeem", json={"user_id": "alice", "code": code_from(mail["text"])})
    assert r.status_code == 200, r.text


def test_mask_keeps_the_first_character_only() -> None:
    from abstractgateway.mail.recovery import mask_address

    assert mask_address("laurent@abstractframework.ai") == "l\u2022\u2022\u2022@\u2022\u2022\u2022"
    assert mask_address("x@y.z") == "x\u2022\u2022\u2022@\u2022\u2022\u2022"


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
    for _ in range(recovery.ACCOUNT_MAX_REQUESTS):
        assert _request(c, "alice").json()["sent"] is True
    for _ in range(2):
        r = _request(c, "alice")
        assert r.status_code == 200
        body = r.json()
        assert body["sent"] is False and body["reason_code"] == "too_many_requests"
        assert 0 < body["retry_after_s"] <= recovery.ACCOUNT_WINDOW_S
        minutes = -(-body["retry_after_s"] // 60)
        assert body["message"] == f"Too many codes requested for this account. Try again in {minutes} minutes."
    assert len(smtp.messages) == recovery.ACCOUNT_MAX_REQUESTS

    # Per client address: a burst over other names is capped too (and answers the same).
    monkeypatch.setattr(recovery, "ACCOUNT_MAX_REQUESTS", 1000)
    for i in range(recovery.IP_MAX_REQUESTS + 3):
        assert _request(c, "alice" if i % 2 else f"ghost{i}").status_code == 200
    audit = (gateway["data_dir"] / "audit_log.jsonl").read_text(encoding="utf-8")
    reasons = [json.loads(line).get("reason") for line in audit.splitlines() if '"email.recovery_code_refused"' in line]
    assert "rate_limited_account" in reasons and "rate_limited_client" in reasons


def test_rate_limited_requests_spawn_no_thread(gateway, imap, smtp, monkeypatch) -> None:
    # The rate check runs before any worker exists: once the account's budget is spent, a
    # flood of requests answers the same and starts no thread at all.
    import threading

    from abstractgateway.mail import recovery

    _connect(gateway, imap, smtp)
    c = _fresh_client(gateway)
    for _ in range(recovery.ACCOUNT_MAX_REQUESTS):
        assert _request(c, "alice").status_code == 200
    started = []
    real_thread = threading.Thread

    def counting_thread(*a, **kw):
        # `recovery.threading` IS the threading module: count only the code-mail workers (the
        # request itself runs in the server's thread pool).
        if kw.get("name") == "gateway-recovery-code":
            started.append(kw.get("name"))
        return real_thread(*a, **kw)

    monkeypatch.setattr(recovery.threading, "Thread", counting_thread)
    for _ in range(5):
        r = _request(c, "alice")
        assert r.status_code == 200 and r.json()["reason_code"] == "too_many_requests"
    assert started == []
    assert len(smtp.messages) == recovery.ACCOUNT_MAX_REQUESTS


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


def test_admin_can_turn_sign_in_by_email_off(gateway, imap, smtp) -> None:
    _connect(gateway, imap, smtp)
    c = _fresh_client(gateway)
    assert c.get("/api/gateway/session/recovery").json()["available"] is True
    _request(c, "alice")
    code = code_from(smtp_bodies(smtp)[-1]["text"])
    r = gateway["client"].put("/api/gateway/admin/email/capabilities", headers=ADMIN, json={"email_recovery": False})
    assert r.status_code == 200
    assert c.get("/api/gateway/session/recovery").json()["available"] is False
    assert _redeem(c, "alice", code).status_code == 401  # outstanding codes stop working too
    before = len(smtp.messages)
    r = _request(c, "alice")
    assert r.status_code == 404 and r.json()["detail"]["reason_code"] == "recovery_off"  # the link is not shown
    assert len(smtp.messages) == before
