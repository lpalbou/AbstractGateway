"""Decision D1: unattended tool approval (policy.tool_approval) and typed waits.

The target is the gateway's default agent for abstractcode.agent.v1 (`@default`),
here a deterministic stand-in whose run calls `execute_command`. No client
sends a `tool_policy`: creating the automation is the consent (`auto`, the
default); `ask` parks every tool batch on a typed `tool_approval` wait, which
only `{approved: true|false}` answers.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import HEADERS, gateway_env, wait_until, write_shell_agent_bundle


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway_env(monkeypatch, tmp_path, runner=True)
    write_shell_agent_bundle(tmp_path / "bundles")
    from abstractgateway.app import app

    with TestClient(app) as c:
        yield c


def _create(c: TestClient, request_id: str, policy: dict | None = None) -> str:
    body = {
        "request_id": request_id,
        "title": "Shell tick",
        "target": {"flow_id": "@default", "interface": "abstractcode.agent.v1", "input_data": {"prompt": "run the check"}},
        "trigger": {"source_id": "manual", "source_version": 1, "config": {}},
    }
    if policy is not None:
        body["policy"] = policy
    r = c.post("/api/gateway/automations", headers=HEADERS, json=body)
    assert r.status_code == 200, r.text
    aid = r.json()["automation_id"]
    definition = c.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["definition"]
    assert "tool_policy" not in definition["target"]["input_data"].get("_runtime", {})
    return aid


def _run_now(c: TestClient, aid: str) -> None:
    r = c.post(f"/api/gateway/automations/{aid}/commands", headers=HEADERS, json={"command_id": f"go-{aid}", "type": "automation.run_now"})
    assert r.status_code == 200, r.text


def _occurrence(c: TestClient, aid: str) -> dict | None:
    rows = c.get(f"/api/gateway/automations/{aid}/occurrences", headers=HEADERS).json()["items"]
    return rows[0] if rows else None


def test_default_policy_runs_tools_unattended(client: TestClient) -> None:
    aid = _create(client, "auto")
    assert client.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["definition"]["policy"]["tool_approval"] == "auto"
    _run_now(client, aid)
    row = wait_until(lambda: (lambda o: o if o and o["status"] in ("completed", "failed") else None)(_occurrence(client, aid)), timeout_s=30)
    assert row["status"] == "completed", row
    assert row["waits"] == []
    ledger = client.get(f"/api/gateway/runs/{row['run_id']}/ledger?after=0&limit=500", headers=HEADERS).json()["items"]
    executed = [i for i in ledger if (i.get("effect") or {}).get("type") == "tool_calls" and i.get("status") == "completed"]
    assert executed and executed[0]["result"]["mode"] == "executed"
    assert "automation-shell-ok" in str(executed[0]["result"]["results"][0].get("output"))


def test_ask_policy_parks_on_a_typed_approval_answered_by_kind(client: TestClient) -> None:
    aid = _create(client, "ask", policy={"tool_approval": "ask"})
    _run_now(client, aid)
    row = wait_until(lambda: (lambda o: o if o and o["waits"] else None)(_occurrence(client, aid)), timeout_s=30)
    (wait,) = row["waits"]
    assert wait["kind"] == "tool_approval" and row["status"] == "waiting"
    assert [c["name"] for c in wait["details"]] == ["execute_command"]
    att = client.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["summary"]["attention"]
    assert att["pending_waits"] == 1 and att["waits"][0]["kind"] == "tool_approval" and att["waits"][0]["details"] == wait["details"]

    # The wrong shape for this kind is refused at the door, not recorded as a tool result.
    bad = client.post("/api/gateway/commands", headers=HEADERS, json={
        "command_id": "bad", "run_id": wait["run_id"], "type": "resume",
        "payload": {"wait_key": wait["wait_key"], "payload": {"response": "approve"}}})
    assert bad.status_code == 422, bad.text
    assert bad.json()["detail"]["reason_code"] == "invalid_request" and bad.json()["detail"]["field"] == "payload"

    ok = client.post("/api/gateway/commands", headers=HEADERS, json={
        "command_id": "ok", "run_id": wait["run_id"], "type": "resume",
        "payload": {"wait_key": wait["wait_key"], "payload": {"approved": True}}})
    assert ok.status_code == 200, ok.text
    done = wait_until(lambda: (lambda o: o if o and o["status"] == "completed" else None)(_occurrence(client, aid)), timeout_s=30)
    assert done["waits"] == []


def test_invalid_tool_approval_policy_is_refused(client: TestClient) -> None:
    r = client.post("/api/gateway/automations", headers=HEADERS, json={
        "request_id": "bad", "title": "x", "target": {"flow_id": "@default", "interface": "abstractcode.agent.v1"},
        "trigger": {"source_id": "manual", "source_version": 1, "config": {}}, "policy": {"tool_approval": "sometimes"}})
    assert r.status_code == 422
    assert r.json()["detail"] == {"reason_code": "invalid_definition", "message": r.json()["detail"]["message"], "field": "policy.tool_approval"}


def _revise(c: TestClient, aid: str, changes: dict, command_id: str) -> None:
    rev = c.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["summary"]["revision"]
    r = c.patch(f"/api/gateway/automations/{aid}", headers=HEADERS,
                json={"command_id": command_id, "expected_revision": rev, "changes": changes})
    assert r.status_code == 200, r.text


def test_a_revision_that_grants_the_waiting_tools_resolves_the_wait(client: TestClient) -> None:
    """Operator QA 2026-10-29: a waiting automation edited to "Run without asking" (any client:
    here the plain revise route) stayed "waiting for you". The gateway now answers the wait the
    revision grants, by the person who saved it, and the occurrence completes."""
    aid = _create(client, "ask-then-auto", policy={"tool_approval": "ask"})
    _run_now(client, aid)
    row = wait_until(lambda: (lambda o: o if o and o["waits"] else None)(_occurrence(client, aid)), timeout_s=30)
    assert row["status"] == "waiting"
    _revise(client, aid, {"policy": {"tool_approval": "auto"}}, "to-auto")
    done = wait_until(lambda: (lambda o: o if o and o["status"] in ("completed", "failed") else None)(_occurrence(client, aid)), timeout_s=30)
    assert done["status"] == "completed" and done["waits"] == [], done
    ledger = client.get(f"/api/gateway/runs/{done['run_id']}/ledger?after=0&limit=500", headers=HEADERS).json()["items"]
    executed = [i for i in ledger if (i.get("effect") or {}).get("type") == "tool_calls" and i.get("status") == "completed"]
    assert executed and "automation-shell-ok" in str(executed[-1]["result"])


def test_a_revision_that_still_asks_for_a_tool_leaves_the_wait(client: TestClient) -> None:
    from abstractgateway.automation_wait_resolver import still_asks

    aid = _create(client, "ask-named", policy={"tool_approval": "ask"})
    _run_now(client, aid)
    row = wait_until(lambda: (lambda o: o if o and o["waits"] else None)(_occurrence(client, aid)), timeout_s=30)
    # Per-tool approvals that keep execute_command on Ask: nothing is answered for the person.
    _revise(client, aid, {"policy": {"tool_policy": {"auto_approve_tools": [], "require_approval_tools": ["execute_command"]}}}, "named-ask")
    import time

    time.sleep(1.5)
    again = _occurrence(client, aid)
    assert again["status"] == "waiting" and again["waits"], again
    definition = client.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["definition"]
    assert still_asks(definition, again["waits"][0]) == ["execute_command"]
