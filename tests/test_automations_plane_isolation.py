"""Two principals, two planes (automations contract F + service.py plane selection):
neither lists, reads, commands, discusses nor acknowledges the other's
automations; normal chat lists never show automation sessions."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import ECHO_FLOW_ID, wait_until, write_echo_bundle


@pytest.fixture()
def two_users(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bundles = tmp_path / "bundles"
    ref = write_echo_bundle(bundles)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles))
    monkeypatch.setenv("ABSTRACTGATEWAY_USERS_FILE", str(tmp_path / "users.json"))
    monkeypatch.setenv("ABSTRACTGATEWAY_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    from abstractgateway.service import reset_gateway_boot_state
    from abstractgateway.users import GatewayUserRegistry

    reset_gateway_boot_state()
    reg = GatewayUserRegistry()
    _a, alice = reg.create_user(user_id="alice", roles=["user"])
    _b, bob = reg.create_user(user_id="bob", roles=["user"])
    from abstractgateway.app import app

    with TestClient(app) as c:
        yield c, ref, {"Authorization": f"Bearer {alice}"}, {"Authorization": f"Bearer {bob}"}


def test_principals_never_see_each_others_automations(two_users) -> None:
    c, ref, alice, bob = two_users
    body = {"request_id": "same-request", "title": "Mine", "target": {"bundle_ref": ref, "flow_id": ECHO_FLOW_ID, "input_data": {"prompt": "p"}},
            "trigger": {"source_id": "manual", "source_version": 1, "config": {}}}
    ra = c.post("/api/gateway/automations", headers=alice, json=body)
    assert ra.status_code == 200, ra.text
    aid = ra.json()["automation_id"]
    # Same request_id from another principal: a different automation (ids are per (tenant, user)).
    rb = c.post("/api/gateway/automations", headers=bob, json=body)
    assert rb.status_code == 200, rb.text
    assert rb.json()["automation_id"] != aid

    assert [s["automation_id"] for s in c.get("/api/gateway/automations", headers=alice).json()["items"]] == [aid]
    assert aid not in [s["automation_id"] for s in c.get("/api/gateway/automations", headers=bob).json()["items"]]
    for method, path, payload in [
        ("get", f"/api/gateway/automations/{aid}", None),
        ("get", f"/api/gateway/automations/{aid}/occurrences", None),
        ("get", f"/api/gateway/automations/{aid}/attention", None),
        ("post", f"/api/gateway/automations/{aid}/commands", {"command_id": "x", "type": "automation.pause"}),
        ("patch", f"/api/gateway/automations/{aid}", {"command_id": "y", "changes": {"title": "stolen"}}),
        ("post", f"/api/gateway/automations/{aid}/discuss", {"request_id": "d", "occurrence_index": 1, "prompt": "hi"}),
        ("post", f"/api/gateway/automations/{aid}/seen", {"attention_cursor": "att1:0"}),
    ]:
        r = getattr(c, method)(path, headers=bob, **({"json": payload} if payload is not None else {}))
        assert r.status_code == 404, (path, r.text)
        assert r.json()["detail"]["reason_code"] == "automation_not_found"
    r = c.post("/api/gateway/commands", headers=bob, json={"command_id": "z", "run_id": aid, "type": "automation.pause", "payload": {}})
    assert r.status_code == 404

    # Alice's own occurrence runs; her chat list does not show it, her automation list does.
    r = c.post(f"/api/gateway/automations/{aid}/commands", headers=alice, json={"command_id": "go", "type": "automation.run_now"})
    assert r.status_code == 200, r.text

    def completed():
        rows = c.get(f"/api/gateway/automations/{aid}/occurrences", headers=alice).json()["items"]
        return rows if rows and rows[0]["status"] == "completed" else None

    occ = wait_until(completed, timeout_s=20)[0]
    chats = c.get("/api/gateway/runs?root_only=true&session_kind=chat,discussion&include_ledger_len=false", headers=alice).json()["items"]
    assert occ["run_id"] not in {r["run_id"] for r in chats}
    bob_runs = c.get("/api/gateway/runs?include_ledger_len=false", headers=bob).json()["items"]
    assert occ["run_id"] not in {r["run_id"] for r in bob_runs} and aid not in {r["run_id"] for r in bob_runs}
