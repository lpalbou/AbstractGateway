"""The public webhook ingress (framework backlog 1042): `POST /api/gateway/triggers/webhooks/{id}`.

No bearer token: the Standard Webhooks signature with the connection's secret is the auth
(verified and appended by AbstractRuntime's `webhook.received@1`, `append_webhook_event`). A
signed call appends one CloudEvents-shaped event to the connection's stream
`trigger:<connection_id>` and wakes the automation bound to it; a tampered signature (401), a
timestamp outside the window (409), a replayed webhook-id (409), an unknown connection (404), an
oversized body (413) and a flood (429) are refused and recorded. Proven through the real app and
security middleware with two accounts (the connection lives in alice's plane, never bob's)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from test_gateway_principal_isolation_matrix import _client, _create_user

SECRET = "whsec_" + base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()


def _sign(body: bytes, *, wid: str, ts: int, secret: str = SECRET) -> Dict[str, str]:
    key = base64.b64decode(secret[len("whsec_"):])
    sig = base64.b64encode(hmac.new(key, f"{wid}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return {"webhook-id": wid, "webhook-timestamp": str(ts), "webhook-signature": f"v1,{sig}", "content-type": "application/json"}


def _principal(user_id: str):
    from abstractgateway.security.principal import GatewayPrincipal

    return GatewayPrincipal(user_id=user_id, tenant_id="default", roles=("user",), runtime_id=user_id)


@pytest.fixture()
def gateway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from abstractgateway import trigger_connections as tc

    tc.webhook_rate_limiter.reset()
    monkeypatch.setenv("ABSTRACTGATEWAY_AUDIT_LOG", "1")
    client = _client(tmp_path, monkeypatch)
    with client:
        alice = _create_user(client, "alice")
        bob = _create_user(client, "bob")
        yield client, alice, bob
    tc.webhook_rate_limiter.reset()


def _alice_connection_and_automation():
    """A webhook connection in alice's plane and an idle automation bound to it."""
    from abstractruntime.automations.models import wake_wait_key
    from abstractruntime.core.models import RunStatus, WaitReason, WaitState

    from abstractgateway import trigger_connections as tc
    from abstractgateway.service import get_gateway_service_for_principal

    from automations_fixtures import controller_run

    svc = get_gateway_service_for_principal(_principal("alice"))
    conn = tc.store_for_service(svc).create(principal="default:alice", source_id="webhook.received", source_version=1, config={"secret": SECRET})
    run = controller_run(title="On webhook")
    run.vars["_meta"]["automation"]["trigger"] = {"binding_id": "b1", "source_id": "webhook.received", "source_version": 1,
                                                  "config": {"connection_id": conn.connection_id}}
    run.status = RunStatus.WAITING
    run.waiting = WaitState(reason=WaitReason.EVENT, wait_key=wake_wait_key(run.run_id))
    svc.host.run_store.save(run)
    return svc, conn, run.run_id


def test_a_signed_call_appends_one_event_and_wakes_the_bound_automation(gateway) -> None:
    client, _alice, _bob = gateway
    svc, conn, automation_id = _alice_connection_and_automation()
    body = json.dumps({"action": "opened", "number": 7}).encode()
    wid = "msg_" + uuid.uuid4().hex
    r = client.post(f"/api/gateway/triggers/webhooks/{conn.connection_id}", content=body, headers=_sign(body, wid=wid, ts=int(time.time())))
    assert r.status_code == 202, r.text
    out = r.json()
    assert out["accepted"] is True and out["webhook_id"] == wid and out["woken"] == 1 and out["seq"] >= 1

    [event] = svc.host.runtime.event_inbox.read(stream=f"trigger:{conn.connection_id}")
    payload = event["payload"]
    assert payload["data"] == {"webhook_id": wid, "content_type": "application/json", "body": {"action": "opened", "number": 7}}
    assert payload["subject"] == wid and payload["dedupekey"] == wid
    assert out["event_id"] == f"trigger:{conn.connection_id}:{payload['id']}"
    assert payload["type"] == "abstract.webhook.received.v1"
    assert payload["specversion"] == "1.0" and payload["connectionid"] == conn.connection_id
    assert payload["abstracttrust"] == "untrusted"
    from abstractruntime.core.models import RunStatus

    assert svc.host.run_store.load(automation_id).status != RunStatus.WAITING  # woken
    assert tc_store_get(svc, conn.connection_id).last_event_at is not None


def tc_store_get(svc, cid):
    from abstractgateway import trigger_connections as tc

    return tc.store_for_service(svc).get(cid)


def test_refusals_tampered_stale_replayed_unknown_and_unsigned(gateway) -> None:
    client, _alice, _bob = gateway
    svc, conn, _aid = _alice_connection_and_automation()
    url = f"/api/gateway/triggers/webhooks/{conn.connection_id}"
    body = b'{"ok": true}'
    now = int(time.time())

    tampered = _sign(body, wid="w1", ts=now)
    r = client.post(url, content=b'{"ok": false}', headers=tampered)
    assert (r.status_code, r.json()["detail"]["reason_code"]) == (401, "webhook_signature_invalid")
    assert tc_store_get(svc, conn.connection_id).last_error["code"] == "webhook_bad_signature"

    wrong_secret = _sign(body, wid="w1", ts=now, secret="whsec_" + base64.b64encode(b"x" * 32).decode())
    assert client.post(url, content=body, headers=wrong_secret).status_code == 401

    stale = _sign(body, wid="w2", ts=now - 3600)
    r = client.post(url, content=body, headers=stale)
    assert (r.status_code, r.json()["detail"]["reason_code"]) == (409, "webhook_stale")

    r = client.post(url, content=body, headers={"content-type": "application/json"})
    assert (r.status_code, r.json()["detail"]["reason_code"]) == (401, "webhook_unsigned")

    ok = _sign(body, wid="w3", ts=now)
    assert client.post(url, content=body, headers=ok).status_code == 202
    r = client.post(url, content=body, headers=ok)
    assert (r.status_code, r.json()["detail"]["reason_code"]) == (409, "webhook_replayed")
    assert len(svc.host.runtime.event_inbox.read(stream=f"trigger:{conn.connection_id}")) == 1

    for unknown in ("tc_doesnotexist", "bad..id"):
        r = client.post(f"/api/gateway/triggers/webhooks/{unknown}", content=body, headers=_sign(body, wid="w4", ts=now))
        assert r.status_code == 404, r.text

    # The refusals are on the audit lines too (outcome + reason, never the body).
    from abstractgateway.service import get_gateway_service

    audit = Path(get_gateway_service().config.data_dir) / "audit_log.jsonl"
    hooks = [json.loads(x).get("webhook") for x in audit.read_text().splitlines() if '"webhook"' in x]
    assert any(h and h.get("reason_code") == "webhook_stale" for h in hooks)
    assert any(h and h.get("outcome") == "accepted" and h.get("webhook_id") == "w3" for h in hooks)
    assert all('"ok"' not in json.dumps(h) for h in hooks)


def test_no_bearer_needed_but_a_bearer_does_not_replace_the_signature(gateway) -> None:
    client, alice, _bob = gateway
    _svc, conn, _aid = _alice_connection_and_automation()
    r = client.post(f"/api/gateway/triggers/webhooks/{conn.connection_id}", content=b"{}", headers={**alice, "content-type": "application/json"})
    assert r.status_code == 401 and r.json()["detail"]["reason_code"] == "webhook_unsigned"
    # A browser Origin is not refused by the allowlist: the signature decides.
    body = b"{}"
    r = client.post(f"/api/gateway/triggers/webhooks/{conn.connection_id}", content=body,
                    headers={**_sign(body, wid="o1", ts=int(time.time())), "origin": "https://evil.example"})
    assert r.status_code == 202, r.text


def test_disabled_connection_is_refused(gateway) -> None:
    client, _alice, _bob = gateway
    svc, conn, _aid = _alice_connection_and_automation()
    from abstractgateway import trigger_connections as tc

    tc.store_for_service(svc).update(conn.connection_id, principal="default:alice", status="disabled")
    body = b"{}"
    r = client.post(f"/api/gateway/triggers/webhooks/{conn.connection_id}", content=body, headers=_sign(body, wid="d1", ts=int(time.time())))
    assert (r.status_code, r.json()["detail"]["reason_code"]) == (409, "connection_disabled")


def test_body_cap_and_rate_limit(gateway, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _alice, _bob = gateway
    _svc, conn, _aid = _alice_connection_and_automation()
    from abstractgateway import trigger_connections as tc

    url = f"/api/gateway/triggers/webhooks/{conn.connection_id}"
    big = b"x" * (tc.WEBHOOK_MAX_BODY_BYTES + 1)
    r = client.post(url, content=big, headers=_sign(big, wid="big", ts=int(time.time())))
    assert (r.status_code, r.json()["detail"]["reason_code"]) == (413, "webhook_too_large")

    monkeypatch.setattr(tc, "webhook_rate_limiter", tc.WebhookRateLimiter(per_connection=2, per_address=100))
    body = b"{}"
    codes = [client.post(url, content=body, headers=_sign(body, wid=f"r{i}", ts=int(time.time()))).status_code for i in range(3)]
    assert codes == [202, 202, 429]


def test_a_non_json_text_body_is_kept_as_text_and_binary_is_refused(gateway) -> None:
    client, _alice, _bob = gateway
    svc, conn, _aid = _alice_connection_and_automation()
    url = f"/api/gateway/triggers/webhooks/{conn.connection_id}"
    body = b"a=1&b=2"
    h = {**_sign(body, wid="t1", ts=int(time.time())), "content-type": "application/x-www-form-urlencoded"}
    assert client.post(url, content=body, headers=h).status_code == 202
    [event] = svc.host.runtime.event_inbox.read(stream=f"trigger:{conn.connection_id}")
    assert event["payload"]["data"]["body_text"] == "a=1&b=2"
    raw = b"\xff\xfe\x00"
    r = client.post(url, content=raw, headers={**_sign(raw, wid="t2", ts=int(time.time())), "content-type": "application/octet-stream"})
    assert (r.status_code, r.json()["detail"]["reason_code"]) == (415, "webhook_unsupported_body")


def test_a_connection_of_another_source_is_not_a_webhook_endpoint(gateway) -> None:
    client, _alice, _bob = gateway
    from abstractgateway import trigger_connections as tc
    from abstractgateway.service import get_gateway_service_for_principal

    svc = get_gateway_service_for_principal(_principal("alice"))
    feed = tc.store_for_service(svc).create(principal="default:alice", source_id="feed.item", source_version=1,
                                            config={"url": "https://example.test/feed.xml"})
    body = b"{}"
    r = client.post(f"/api/gateway/triggers/webhooks/{feed.connection_id}", content=body, headers=_sign(body, wid="f1", ts=int(time.time())))
    assert (r.status_code, r.json()["detail"]["reason_code"]) == (404, "connection_not_found")
