"""Trigger connections API (framework backlog 1041) and connection-bound automations (1040/1047).

`GET/POST /api/gateway/triggers/connections`, `GET/PUT/DELETE .../{id}`, `GET .../{id}/events`,
`POST .../{id}/test-fire`: principal-scoped (alice never reads bob's connection; a foreign id reads
like an unknown one), the admin lists every account's with `all=true` and may revoke any, rows in
the one contract shape the console page and Flow's selector parse (source_version int, status str,
created_at / last_event_at str|null, config_public object, never a secret), the webhook secret
shown once at creation, a test-fire visible in the events and in `last_event_at`. Then automations:
`trigger.connection_id` (flow defaults and requests) is folded into `config.connection_id`, a
connection that is not the owner's is refused, and a shared workflow's defaults naming the
author's connection never let someone else bind it."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import ECHO_FLOW_ID, write_echo_bundle

ADMIN = {"Authorization": "Bearer admin-token"}
BASE = "/api/gateway/triggers/connections"
AUTHOR_CONNECTION = "tc_authorhook"


def _sign(secret: str, body: bytes, wid: str) -> dict:
    ts = int(time.time())
    key = base64.b64decode(secret[len("whsec_"):])
    sig = base64.b64encode(hmac.new(key, f"{wid}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return {"webhook-id": wid, "webhook-timestamp": str(ts), "webhook-signature": f"v1,{sig}", "content-type": "application/json"}


@pytest.fixture()
def gw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bundles = tmp_path / "bundles"
    defaults = {"schema_version": 1, "title": "On the author's webhook", "context": {"mode": "independent"}, "input_data": {"prompt": "p"},
                "trigger": {"source_id": "webhook.received", "source_version": 1, "config": {}, "connection_id": AUTHOR_CONNECTION}}
    ref = write_echo_bundle(bundles, automation_defaults=defaults)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "admin-token")
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    from abstractgateway import trigger_connections as tc
    from abstractgateway.service import reset_gateway_boot_state
    from abstractgateway.users import GatewayUserRegistry

    reset_gateway_boot_state()
    tc.webhook_rate_limiter.reset()
    reg = GatewayUserRegistry()
    _a, alice = reg.create_user(user_id="alice", roles=["user"])
    _b, bob = reg.create_user(user_id="bob", roles=["user"])
    from abstractgateway.app import app

    with TestClient(app) as c:
        yield c, ref, {"Authorization": f"Bearer {alice}"}, {"Authorization": f"Bearer {bob}"}


def _create_webhook(c, headers, label="Orders") -> dict:
    r = c.post(BASE, headers=headers, json={"source_id": "webhook.received", "source_version": 1, "label": label, "config": {}})
    assert r.status_code == 201, r.text
    return r.json()


def _assert_row_contract(row: dict) -> None:
    assert isinstance(row["connection_id"], str) and row["connection_id"]
    assert isinstance(row["source_id"], str) and isinstance(row["source_version"], int) and not isinstance(row["source_version"], bool)
    assert isinstance(row["status"], str) and isinstance(row["label"], str)
    for key in ("created_at", "last_event_at"):
        assert row[key] is None or isinstance(row[key], str)
    assert isinstance(row["config_public"], dict) and isinstance(row["principal"], str)


def test_crud_is_principal_scoped_and_rows_keep_the_contract(gw) -> None:
    c, _ref, alice, bob = gw
    created = _create_webhook(c, alice)
    cid = created["connection_id"]
    _assert_row_contract(created)
    assert created["principal"] == "default:alice" and created["status"] == "active" and created["last_event_at"] is None
    assert created["secret"].startswith("whsec_") and created["ingress_url"].endswith(f"/api/gateway/triggers/webhooks/{cid}")
    assert created["secret_fields"] == ["secret"] and "secret" not in created["config_public"]

    listed = c.get(BASE, headers=alice).json()["connections"]
    assert [r["connection_id"] for r in listed] == [cid]
    _assert_row_contract(listed[0])
    got = c.get(f"{BASE}/{cid}", headers=alice).json()
    assert "secret" not in got and created["secret"] not in json.dumps(got)  # shown once, never again
    assert got["ingress_url"] == created["ingress_url"]

    assert c.get(BASE, headers=bob).json()["connections"] == []
    for method, path, body in [("get", f"{BASE}/{cid}", None), ("put", f"{BASE}/{cid}", {"label": "mine"}),
                               ("delete", f"{BASE}/{cid}", None), ("get", f"{BASE}/{cid}/events", None),
                               ("post", f"{BASE}/{cid}/test-fire", {})]:
        r = getattr(c, method)(path, headers=bob, **({"json": body} if body is not None else {}))
        assert (r.status_code, r.json()["detail"]["reason_code"]) == (404, "connection_not_found"), (method, path, r.text)

    renamed = c.put(f"{BASE}/{cid}", headers=alice, json={"label": "Shop orders", "status": "disabled"}).json()
    assert (renamed["label"], renamed["status"]) == ("Shop orders", "disabled")
    bad = c.put(f"{BASE}/{cid}", headers=alice, json={"status": "error"})
    assert bad.status_code == 422 and bad.json()["detail"]["field"] == "status"
    assert c.delete(f"{BASE}/{cid}", headers=alice).json() == {"deleted": True, "connection_id": cid}
    assert c.get(f"{BASE}/{cid}", headers=alice).status_code == 404


def test_create_refusals(gw) -> None:
    c, _ref, alice, _bob = gw
    for body, code in [
        ({"source_id": "nope.source", "source_version": 1, "config": {}}, "unknown_trigger_source"),
        ({"source_id": "email.received", "source_version": 1, "config": {}}, "invalid_connection"),
        ({"source_id": "schedule", "source_version": 2, "config": {}}, "invalid_connection"),
        ({"source_id": "webhook.received", "source_version": 1, "config": {"tolerance_s": 5}}, "invalid_connection"),
    ]:
        r = c.post(BASE, headers=alice, json=body)
        assert (r.status_code, r.json()["detail"]["reason_code"]) == (422, code), (body, r.text)


def test_test_fire_and_a_real_webhook_show_in_the_events(gw) -> None:
    c, _ref, alice, _bob = gw
    created = _create_webhook(c, alice)
    cid = created["connection_id"]
    fire = c.post(f"{BASE}/{cid}/test-fire", headers=alice, json={})
    assert fire.status_code == 200, fire.text
    assert fire.json()["event_id"] and fire.json()["sequence"] >= 1
    body = b'{"order": 42}'
    assert c.post(f"/api/gateway/triggers/webhooks/{cid}", content=body, headers=_sign(created["secret"], body, "w-1")).status_code == 202

    events = c.get(f"{BASE}/{cid}/events?limit=20", headers=alice).json()["events"]
    assert [e["data"].get("test") for e in events] == [True, None]
    assert events[0]["sequence"] == fire.json()["sequence"] and events[0]["specversion"] == "1.0"
    assert events[1]["data"]["body"] == {"order": 42} and events[1]["subject"] == "w-1"
    assert events[1]["type"] == "abstract.webhook.received.v1" and isinstance(events[1]["recordedtime"], str)
    page = c.get(f"{BASE}/{cid}/events?after={events[0]['sequence']}&limit=1", headers=alice).json()
    assert [e["sequence"] for e in page["events"]] == [events[1]["sequence"]] and page["next_after"] == events[1]["sequence"]
    assert c.get(f"{BASE}/{cid}/events?limit=1", headers=alice).json()["events"][0]["sequence"] == events[1]["sequence"]  # newest
    assert c.get(f"{BASE}/{cid}", headers=alice).json()["last_event_at"] is not None


def test_the_admin_lists_every_account_and_may_revoke(gw) -> None:
    c, _ref, alice, bob = gw
    a = _create_webhook(c, alice, "alice's")["connection_id"]
    b = _create_webhook(c, bob, "bob's")["connection_id"]
    assert c.get(f"{BASE}?all=true", headers=alice).status_code == 403
    rows = c.get(f"{BASE}?all=true", headers=ADMIN).json()["connections"]
    assert {(r["connection_id"], r["principal"]) for r in rows} >= {(a, "default:alice"), (b, "default:bob")}
    for r in rows:
        _assert_row_contract(r)
    assert c.get(BASE, headers=ADMIN).json()["connections"] == []  # the admin's OWN list
    assert c.get(f"{BASE}/{a}", headers=ADMIN).json()["principal"] == "default:alice"
    assert c.delete(f"{BASE}/{a}", headers=ADMIN).status_code == 200
    assert c.get(BASE, headers=alice).json()["connections"] == []


def _automation_body(ref: str, *, trigger=None, request_id="r1") -> dict:
    body = {"request_id": request_id, "title": "On my webhook", "target": {"bundle_ref": ref, "flow_id": ECHO_FLOW_ID, "input_data": {"prompt": "p"}}}
    if trigger is not None:
        body["trigger"] = trigger
    return body


def test_automations_bind_only_their_owners_connection(gw) -> None:
    c, ref, alice, bob = gw
    cid = _create_webhook(c, alice)["connection_id"]
    trig = {"source_id": "webhook.received", "source_version": 1, "config": {}, "connection_id": cid}
    ok = c.post("/api/gateway/automations", headers=alice, json=_automation_body(ref, trigger=trig))
    assert ok.status_code == 200, ok.text
    definition = c.get(f"/api/gateway/automations/{ok.json()['automation_id']}", headers=alice).json()["definition"]
    assert definition["trigger"]["config"]["connection_id"] == cid and "connection_id" not in definition["trigger"]

    stolen = c.post("/api/gateway/automations", headers=bob, json=_automation_body(ref, trigger=trig))
    assert (stolen.status_code, stolen.json()["detail"]["reason_code"]) == (404, "connection_not_found"), stolen.text
    missing = c.post("/api/gateway/automations", headers=alice,
                     json=_automation_body(ref, trigger={"source_id": "webhook.received", "source_version": 1, "config": {}}, request_id="r2"))
    assert (missing.status_code, missing.json()["detail"]["reason_code"]) == (422, "invalid_definition"), missing.text
    assert missing.json()["detail"]["field"] == "trigger.config.connection_id"


def test_a_shared_workflow_naming_the_authors_connection_cannot_be_bound_by_someone_else(gw) -> None:
    c, ref, alice, bob = gw
    from abstractgateway import trigger_connections as tc
    from abstractgateway.service import get_gateway_service_for_principal
    from abstractgateway.users import GatewayUserRegistry

    author = GatewayUserRegistry().get_user("alice").to_principal()
    tc.store_for_service(get_gateway_service_for_principal(author)).create(
        principal="default:alice", source_id="webhook.received", source_version=1, config={}, connection_id=AUTHOR_CONNECTION)
    mine = c.post("/api/gateway/automations", headers=alice, json=_automation_body(ref))  # trigger from the defaults
    assert mine.status_code == 200, mine.text
    theirs = c.post("/api/gateway/automations", headers=bob, json=_automation_body(ref))
    assert (theirs.status_code, theirs.json()["detail"]["reason_code"]) == (422, "connection_not_yours"), theirs.text
    assert "not yours" in theirs.json()["detail"]["message"]


def test_flow_defaults_carry_connection_id_iff_the_source_needs_one() -> None:
    from abstractgateway.automation_defaults import AutomationDefaultsError, fold_connection_id, validate_flow_automation_defaults

    base = {"schema_version": 1, "context": {"mode": "independent"}, "input_data": {}}
    ok = validate_flow_automation_defaults({**base, "trigger": {"source_id": "webhook.received", "source_version": 1, "config": {}, "connection_id": "tc_x"}})
    assert ok["trigger"]["connection_id"] == "tc_x" and ok["trigger"]["config"] == {}
    for trigger, field, code in [
        ({"source_id": "webhook.received", "source_version": 1, "config": {}}, "automation_defaults.trigger.connection_id", "connection_required"),
        ({"source_id": "schedule", "source_version": 1, "config": {"every": "1h"}, "connection_id": "tc_x"}, "automation_defaults.trigger.connection_id", "invalid_definition"),
        ({"source_id": "webhook.received", "source_version": 1, "config": {}, "connection_id": ""}, "automation_defaults.trigger.connection_id", "invalid_definition"),
        ({"source_id": "webhook.received", "source_version": 1, "config": {"connection_id": "tc_y"}, "connection_id": "tc_x"}, "automation_defaults.trigger.connection_id", "invalid_definition"),
    ]:
        with pytest.raises(AutomationDefaultsError) as exc:
            validate_flow_automation_defaults({**base, "trigger": trigger})
        assert (exc.value.field, exc.value.reason_code) == (field, code), trigger
    assert fold_connection_id({"source_id": "s", "config": {"a": 1}, "connection_id": "tc_x"}) == {"source_id": "s", "config": {"a": 1, "connection_id": "tc_x"}}


def test_a_foreign_row_in_the_same_plane_is_never_listed_or_read(gw) -> None:
    """Defence in depth: the store filters by owner even inside one plane (a plane shared by
    two principals, e.g. user accounts turned off later, must not leak a connection)."""
    c, _ref, alice, _bob = gw
    from abstractgateway import trigger_connections as tc
    from abstractgateway.service import get_gateway_service_for_principal
    from abstractgateway.users import GatewayUserRegistry

    plane = tc.store_for_service(get_gateway_service_for_principal(GatewayUserRegistry().get_user("alice").to_principal()))
    other = plane.create(principal="default:mallory", source_id="webhook.received", source_version=1, config={})
    assert c.get(BASE, headers=alice).json()["connections"] == []
    assert c.get(f"{BASE}/{other.connection_id}", headers=alice).status_code == 404
