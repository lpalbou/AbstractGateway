"""Example 3 (framework backlog 1048): "run this flow when an external system calls my webhook".

1. a `webhook.received@1` connection through `POST /api/gateway/triggers/connections` (the secret is
   shown once);
2. an automation through `POST /api/gateway/automations` bound to it, running the "judge" flow;
3. the external system's call, signed per Standard Webhooks (`webhook-id`, `webhook-timestamp`,
   `webhook-signature: v1,<base64 HMAC-SHA256 of "<id>.<ts>.<body>">`), to the public ingress
   `POST /api/gateway/triggers/webhooks/{connection_id}` — no bearer token; a tampered body is 401;
4. one occurrence runs with the call's body at `event.data.events[0].data.body`; the model judges
   it needs the person; `attention.raised` and `run.finished` land on `GET /api/gateway/me/events`.

The docs page `docs/events-examples.md` gives the same steps as shell commands."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from e2e_support import (
    HEADERS,
    ScriptedModel,
    attention_items,
    completed_occurrences,
    create_automation,
    events_of,
    gateway_env,
    wait_until,
    write_judge_bundle,
)


def sign(secret: str, body: bytes, webhook_id: str, ts: int) -> dict:
    """What any Standard Webhooks sender computes (`whsec_` + base64 key)."""
    key = base64.b64decode(secret.removeprefix("whsec_"))
    mac = hmac.new(key, f"{webhook_id}.{ts}.".encode() + body, hashlib.sha256).digest()
    return {"webhook-id": webhook_id, "webhook-timestamp": str(ts),
            "webhook-signature": "v1," + base64.b64encode(mac).decode(), "content-type": "application/json"}


@pytest.fixture()
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from abstractgateway import trigger_connections as tc

    gateway_env(monkeypatch, tmp_path)
    write_judge_bundle(tmp_path / "bundles")
    model = ScriptedModel()
    model.install(monkeypatch)
    tc.webhook_rate_limiter.reset()
    from abstractgateway.app import app

    with TestClient(app) as c:
        yield c, model
    tc.webhook_rate_limiter.reset()


def test_a_signed_webhook_call_runs_the_flow_and_raises_attention(live) -> None:
    client, model = live
    r = client.post("/api/gateway/triggers/connections", headers=HEADERS,
                    json={"source_id": "webhook.received", "source_version": 1, "label": "Orders", "config": {}})
    assert r.status_code == 201, r.text
    created = r.json()
    cid, secret = created["connection_id"], created["secret"]
    assert secret.startswith("whsec_") and created["ingress_url"].endswith(f"/api/gateway/triggers/webhooks/{cid}")
    aid = create_automation(client, title="Orders desk", request_id="e2e-webhook",
                            trigger={"source_id": "webhook.received", "source_version": 1, "config": {}, "connection_id": cid},
                            prompt="An order system called. Decide whether this needs me.")

    model.answer({"title": "Order 42 is blocked", "body": "The payment provider refused order 42; the customer is waiting."})
    body = json.dumps({"order": 42, "state": "payment_refused"}).encode()
    url = f"/api/gateway/triggers/webhooks/{cid}"
    now = int(time.time())

    # Tampered: the body is not the one signed -> 401, nothing appended, nothing runs.
    tampered = client.post(url, content=b'{"order": 43}', headers=sign(secret, body, "msg_" + uuid.uuid4().hex, now))
    assert (tampered.status_code, tampered.json()["detail"]["reason_code"]) == (401, "webhook_signature_invalid")

    wid = "msg_" + uuid.uuid4().hex
    ok = client.post(url, content=body, headers=sign(secret, body, wid, now))
    assert ok.status_code == 202, ok.text
    assert ok.json()["accepted"] is True and ok.json()["woken"] == 1

    [occ] = wait_until(lambda: completed_occurrences(client, aid), timeout_s=30)

    # The occurrence ran with the call's body as structured input, and the model saw it framed.
    from abstractgateway.service import get_gateway_service

    run = get_gateway_service().host.run_store.load(occ["run_id"])
    event = run.vars["event"]
    assert event["type"] == "abstract.webhook.received.v1" and event["specversion"] == "1.0"
    assert event["data"]["events"][0]["data"]["body"] == {"order": 42, "state": "payment_refused"}
    assert event["data"]["events"][0]["subject"] == wid
    assert any('"order": 42' in p for p in model.prompts), model.prompts

    # The model's judgement became an attention item of the automation...
    [item] = attention_items(client, aid)
    assert (item["kind"], item["title"]) == ("notify", "Order 42 is blocked")
    # ...and both facts are on the person's event stream.
    raised = wait_until(lambda: events_of(client, "attention.raised", automation_id=aid))
    assert raised[0]["data"]["kind"] == "notify" and raised[0]["data"]["record_key"]
    finished = wait_until(lambda: events_of(client, "run.finished", run_id=occ["run_id"]))
    assert finished[0]["data"]["status"] == "completed"
    assert finished[0]["data"]["origin"]["kind"] == "trigger"
    triggered = events_of(client, "trigger.event", automation_id=aid)
    assert triggered and triggered[0]["data"]["run_id"] == occ["run_id"]

    # The same call again (a sender's retry) is refused as a replay and runs nothing more.
    again = client.post(url, content=body, headers=sign(secret, body, wid, now))
    assert (again.status_code, again.json()["detail"]["reason_code"]) == (409, "webhook_replayed")
    time.sleep(0.5)
    assert len(completed_occurrences(client, aid)) == 1
