"""Opt-in LIVE check of per-user email against a real test mailbox (framework backlog 0992).

Skipped unless the AF_TEST_EMAIL_* variables are present in the test process (load them only
there: `set -a; . ~/.config/abstractframework-test/email.env; set +a`). This module reads ONLY
those variables, never prints or records their values (assertion messages carry status codes and
typed error codes, never response bodies), and sends only to the test account's own address.
The OS keychain is replaced by an in-memory backend; everything else stays under tmp_path.
"""

from __future__ import annotations

import os
from typing import Dict

import pytest

_NAMES = (
    "AF_TEST_EMAIL_ADDRESS",
    "AF_TEST_EMAIL_USERNAME",
    "AF_TEST_EMAIL_PASSWORD",
    "AF_TEST_EMAIL_IMAP_HOST",
    "AF_TEST_EMAIL_IMAP_PORT",
    "AF_TEST_EMAIL_IMAP_SECURITY",
    "AF_TEST_EMAIL_SMTP_HOST",
    "AF_TEST_EMAIL_SMTP_PORT",
    "AF_TEST_EMAIL_SMTP_SECURITY",
)
_REQUIRED = ("AF_TEST_EMAIL_ADDRESS", "AF_TEST_EMAIL_PASSWORD", "AF_TEST_EMAIL_IMAP_HOST", "AF_TEST_EMAIL_SMTP_HOST")
_ENV: Dict[str, str] = {k: os.environ.get(k, "") for k in _NAMES}

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.network("opt-in live mailbox test: real IMAP/SMTP of the AbstractFramework test account"),
    pytest.mark.skipif(not all(_ENV[k] for k in _REQUIRED), reason="AF_TEST_EMAIL_* not set (opt-in live test)"),
]


def _server(prefix: str) -> dict:
    out = {"host": _ENV[f"AF_TEST_EMAIL_{prefix}_HOST"], "security": _ENV[f"AF_TEST_EMAIL_{prefix}_SECURITY"] or "ssl"}
    if _ENV[f"AF_TEST_EMAIL_{prefix}_PORT"]:
        out["port"] = int(_ENV[f"AF_TEST_EMAIL_{prefix}_PORT"])
    if prefix == "IMAP":
        out["folder"] = "INBOX"
    return out


def test_live_connect_test_notify_and_disconnect(tmp_path, monkeypatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    for name in list(os.environ):
        if name.startswith("ABSTRACT_EMAIL_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "gw"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "live-admin-token")
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUDIT_LOG", "1")

    from abstractgateway.routes import email_router, gateway_router
    from abstractgateway.security import GatewaySecurityMiddleware, load_gateway_auth_policy_from_env
    from abstractgateway.users import GatewayUserRegistry

    app = FastAPI()
    app.add_middleware(GatewaySecurityMiddleware, policy=load_gateway_auth_policy_from_env())
    app.include_router(email_router, prefix="/api")
    app.include_router(gateway_router, prefix="/api")
    address = _ENV["AF_TEST_EMAIL_ADDRESS"]
    _rec, token = GatewayUserRegistry().create_user(user_id="livetester", roles=["user"], email=address)
    c = TestClient(app)
    h = {"Authorization": f"Bearer {token}"}

    body = {
        "address": address,
        "username": _ENV["AF_TEST_EMAIL_USERNAME"],
        "password": _ENV["AF_TEST_EMAIL_PASSWORD"],
        "imap": _server("IMAP"),
        "smtp": _server("SMTP"),
    }
    r = c.put("/api/gateway/me/email", headers=h, json=body)
    code = (r.json().get("detail") or {}).get("reason_code") if r.status_code != 200 else "ok"
    assert r.status_code == 200, f"connect answered {r.status_code} ({code})"
    assert _ENV["AF_TEST_EMAIL_PASSWORD"] not in r.text

    r = c.post("/api/gateway/me/email/test", headers=h)
    assert r.status_code == 200 and r.json().get("ok") is True, f"test answered {r.status_code}"

    # One notice to the account's own address, through its own account and the outbox.
    r = c.post("/api/gateway/me/notifications/test", headers=h)
    assert r.status_code == 200 and r.json().get("state") == "sent", f"test notification state: {r.json().get('state')}"

    # The password never lands in clear anywhere under the data dir.
    secret = _ENV["AF_TEST_EMAIL_PASSWORD"].encode()
    leaked = [p.name for p in (tmp_path / "gw").rglob("*") if p.is_file() and secret in p.read_bytes()]
    assert leaked == [], "the password appears in clear in a gateway file"

    r = c.delete("/api/gateway/me/email", headers=h)
    assert r.status_code == 200 and r.json()["configured"] is False
