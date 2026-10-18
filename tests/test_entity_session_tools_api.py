"""Real ASGI + entity home + durable visit + scripted provider contract proofs."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "entity-session-tests")
    monkeypatch.setattr("abstractgateway.entity_chat.gateway_text_mind", lambda: {
        "provider": "lmstudio", "model": "test-model", "base_url": None, "reasoning": None})
    from abstractgateway import entity_chat

    class LLM:
        def __init__(self):
            self.calls = 0

        def generate(self, **kwargs):
            self.calls += 1
            # One native call in the first reasoning step; the second answers.
            tools = [{"name": "read_memory", "arguments": {"tag": "missing"}, "call_id": "memory-call"}] if self.calls == 1 else None
            return SimpleNamespace(content="" if tools else "A completed answer.", tool_calls=tools,
                                   finish_reason="tool_calls" if tools else "stop", model="test-model",
                                   usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})

    llm = LLM()
    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: llm)
    from abstractgateway.app import app
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    with TestClient(app, headers={"Authorization": "Bearer entity-session-tests"}) as c:
        spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE)); spark["name"] = "Session Test"; spark["spark"] = 1
        response = c.post("/api/gateway/entities", json={"name": "Session Test", "spark": spark})
        assert response.status_code == 201, response.text
        yield c


def test_real_entity_session_approval_policy_rejoin_end_and_turn_metrics(client):
    base = "/api/gateway/entities/session-test"
    options = client.get(base + "/session-tools")
    assert options.status_code == 200, options.text
    assert "read_memory" in {row["name"] for row in options.json()["items"]}
    opened = client.post(base + "/sessions", json={"session_id": "phone-session", "tool_policy": {"modes": {"read_memory": "approve"}}})
    assert opened.status_code == 200, opened.text
    run = opened.json()["run_id"]; path = base + "/visit/" + run
    assert opened.json()["ready_for_input"] and opened.json()["turn_complete"] is False
    rejoined = client.post(base + "/sessions", json={"session_id": "phone-session", "tool_policy": {"modes": {}}})
    assert rejoined.status_code == 200 and rejoined.json()["run_id"] == run
    assert rejoined.json()["tool_policy"]["modes"] == {"read_memory": "approve"}
    conflict = client.post(base + "/sessions", json={"session_id": "other-session"})
    assert conflict.status_code == 409
    turn = client.post(path + "/turn", json={"text": "Read that memory."})
    assert turn.status_code == 200, turn.text
    state = client.get(path + "/state").json()
    assert state["pending_wait"] is not None and state["reply"] is None and not state["turn_complete"]
    key = state["pending_wait"]["wait_key"]
    assert client.put(path + "/tool-policy", json={"modes": {}}).status_code == 409
    assert client.post(path + "/decision", json={"wait_key": key, "approved": "false"}).status_code == 422
    decided = client.post(path + "/decision", json={"wait_key": key, "approved": True})
    assert decided.status_code == 200, decided.text
    first = decided.json()
    assert first["turn_complete"] and first["reply"] == "A completed answer."
    assert first["stats"]["input_tokens"] == 14 and first["stats"]["output_tokens"] == 6
    assert first["stats"]["tool_calls"] == 1
    assert client.post(path + "/decision", json={"wait_key": key, "approved": True}).status_code == 409
    assert client.put(path + "/tool-policy", json={"modes": {}}).json()["tool_policy"] == {"modes": {}}
    assert client.post(path + "/turn", json={"text": "Answer again, without tools."}).status_code == 200
    second = client.get(path + "/state").json()
    assert second["turn_n"] == 2 and second["turn_complete"]
    assert second["stats"]["input_tokens"] == 7 and second["stats"]["output_tokens"] == 3
    assert second["stats"]["tool_calls"] == 0
    ended = client.post(path + "/end")
    assert ended.status_code == 200 and ended.json()["status"] == "completed", ended.text
    assert client.get(base + "/visit").json()["open"] is False


def test_real_approval_deny_and_pending_end_never_execute(client):
    base = "/api/gateway/entities/session-test"
    opened = client.post(base + "/sessions", json={"session_id": "phone-deny", "tool_policy": {"modes": {"read_memory": "approve"}}})
    assert opened.status_code == 200, opened.text
    path = base + "/visit/" + opened.json()["run_id"]
    assert client.post(path + "/turn", json={"text": "Read memory"}).status_code == 200
    state = client.get(path + "/state").json()
    denied = client.post(path + "/decision", json={"wait_key": state["pending_wait"]["wait_key"], "approved": False})
    assert denied.status_code == 200 and denied.json()["turn_complete"], denied.text
    assert denied.json()["stats"]["tool_calls"] == 0
    assert client.get(base + "/visit/foreign/state").status_code == 404
    assert client.post(path + "/end").status_code == 200


def test_default_metadata_route_precedes_automation_id_route(client):
    from abstractruntime.session_history import HISTORY_REPLAY_MAX_TOKENS

    response = client.get("/api/gateway/automations/defaults")
    assert response.status_code == 200 and response.json()["growing_max_tokens"] == HISTORY_REPLAY_MAX_TOKENS
    capability = client.get("/api/gateway/tool-policy/capabilities")
    assert capability.status_code == 200
    from abstractgateway.live_deltas import ABSTRACTRUNTIME_FLOOR

    assert capability.json() == {"supported": True, "modes": ["off", "auto", "approve"], "runtime_floor": ABSTRACTRUNTIME_FLOOR}


def test_message_stats_routes_have_the_principals_store_boundary(client):
    assert client.get("/api/gateway/runs/not-in-this-store/message-stats").status_code == 404


def test_existing_turn_and_close_cannot_bypass_managed_session_owner(client):
    from abstractgateway.routes.entities import _visit_host

    base = "/api/gateway/entities/session-test"
    opened = client.post(base + "/sessions", json={"session_id": "foreign-owner"})
    assert opened.status_code == 200, opened.text
    run_id = opened.json()["run_id"]; path = base + "/visit/" + run_id
    # The authenticated operator can see this entity, but this visit belongs
    # to someone else. None of the old entry points may bypass its owner.
    host = _visit_host()
    er, run, _wf, _manifest = host._load_visit("session-test", run_id, need_spec=False)
    run.vars["_runtime"]["entity_session_tools"]["owner"] = {"tenant_id": "default", "user_id": "another-user"}
    er.run_store.save(run)
    assert client.get(path + "/state").status_code == 404
    assert client.post(path + "/turn", json={"text": "Inject into another person's conversation"}).status_code == 404
    assert client.post(path + "/close", json={"closed_by": "pause"}).status_code == 404
    assert client.post(path + "/end").status_code == 404
    assert client.post(path + "/tick").status_code == 404
    assert client.get(path + "/ledger").status_code == 404
    assert client.get(path + "/transcript").status_code == 404


def test_end_while_waiting_for_approval_cancels_without_running_tools(client):
    base = "/api/gateway/entities/session-test"
    opened = client.post(base + "/sessions", json={"session_id": "end-pending", "tool_policy": {"modes": {"read_memory": "approve"}}})
    path = base + "/visit/" + opened.json()["run_id"]
    assert client.post(path + "/turn", json={"text": "Read memory"}).status_code == 200
    pending = client.get(path + "/state").json()["pending_wait"]
    assert pending is not None
    ended = client.post(path + "/end")
    assert ended.status_code == 200 and ended.json()["status"] == "cancelled", ended.text
    assert client.post(path + "/decision", json={"wait_key": pending["wait_key"], "approved": True}).status_code == 409
    assert client.get(base + "/visit").json()["open"] is False


def test_actual_entity_grant_is_rechecked_at_approval(client):
    base = "/api/gateway/entities/session-test"
    opened = client.post(base + "/sessions", json={"session_id": "fresh-grant", "tool_policy": {"modes": {"read_memory": "approve"}}})
    assert opened.status_code == 200, opened.text
    path = base + "/visit/" + opened.json()["run_id"]
    assert client.post(path + "/turn", json={"text": "Read memory"}).status_code == 200
    pending = client.get(path + "/state").json()["pending_wait"]
    # Home policy is the authority even after a user already saw Approve.
    revoked = client.put(base + "/tool-policy", json={"policy": {"visit": []}})
    assert revoked.status_code == 200, revoked.text
    decided = client.post(path + "/decision", json={"wait_key": pending["wait_key"], "approved": True})
    assert decided.status_code == 200 and decided.json()["turn_complete"], decided.text
    assert decided.json()["stats"]["tool_calls"] == 0
