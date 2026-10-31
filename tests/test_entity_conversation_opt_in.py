"""Use the canonical entity visit door; never enable updates by raw steering."""
from test_entity_session_tools_api import client
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import threading


def test_managed_visit_seeds_conversation_before_first_turn_and_rejoin_cannot_toggle(client):
    from abstractgateway.routes.entities import _visit_host

    base = "/api/gateway/entities/session-test"
    response = client.post(base + "/visit/open", json={"session_id": "public-visit", "conversation": True,
                                                      "tool_policy": {"modes": {}}})
    assert response.status_code == 200, response.text
    rid = response.json()["run_id"]
    _er, run, _wf, _manifest = _visit_host()._load_visit("session-test", rid, need_spec=False)
    assert run.vars["_runtime"]["conversation"] == {"enabled": True}
    assert run.vars["_visit"].get("turn_n", 0) == 0
    changed = client.post(base + "/visit/open", json={"session_id": "public-visit", "conversation": False,
                                                     "tool_policy": {"modes": {}}})
    assert changed.status_code == 422
    assert client.post(base + "/visit/open", json={"conversation": "false"}).status_code == 422
    assert client.post(base + "/visit/" + rid + "/end").status_code == 200


def test_owned_busy_guidance_reaches_next_actor_call_and_public_history(client, monkeypatch):
    from abstractgateway import entity_chat

    entered, release = threading.Event(), threading.Event()
    calls = []

    class Provider:
        def generate(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                entered.set()
                assert release.wait(10)
                return SimpleNamespace(content="", tool_calls=[{"name": "read_memory", "arguments": {"tag": "missing"},
                                        "call_id": "memory-call"}], finish_reason="tool_calls", usage={})
            assert "I cannot use stairs." in str(kwargs.get("messages"))
            return SimpleNamespace(content="I will avoid stairs.", tool_calls=None, finish_reason="stop", usage={})

    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda *args, **kwargs: Provider())
    base = "/api/gateway/entities/session-test"
    opened = client.post(base + "/visit/open", json={"session_id": "live-guide", "conversation": True,
                                                    "tool_policy": {"modes": {"read_memory": "auto"}}})
    assert opened.status_code == 200, opened.text
    path = base + "/visit/" + opened.json()["run_id"]
    with ThreadPoolExecutor(max_workers=1) as pool:
        turn = pool.submit(client.post, path + "/turn", json={"text": "Find a route."})
        try:
            assert entered.wait(10)
            response = client.post(path + "/guidance", json={"text": "Regarding the route: I cannot use stairs.",
                                                            "display_text": "I cannot use stairs."})
            assert response.status_code == 200, response.text
            assert response.json()["accepted"] is True
            state = client.get(path + "/state").json()
            assert state["public_messages"][-1]["text"] == "I cannot use stairs."
            assert state["public_messages"][-1]["guidance_status"] == "accepted"
        finally:
            release.set()
        completed = turn.result(timeout=15)
    assert completed.status_code == 200, completed.text
    assert "avoid stairs" in completed.json()["reply"]
    assert client.post(path + "/guidance", json={"text": "An idle message"}).status_code == 409
    assert client.post(path + "/end").status_code == 200


def test_entity_essential_question_uses_canonical_answer_and_rejects_skip_or_approval(client, monkeypatch):
    from abstractgateway import entity_chat

    calls = []

    class Provider:
        def generate(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                assert "ask_user" in str(kwargs.get("tools"))
                return SimpleNamespace(content="", tool_calls=[{"name": "ask_user", "arguments": {
                    "question": "Do you need step-free access?", "importance": "essential"}, "call_id": "question-1"}],
                    finish_reason="tool_calls", usage={})
            return SimpleNamespace(content="I will use step-free access.", tool_calls=None, finish_reason="stop", usage={})

    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda *args, **kwargs: Provider())
    base = "/api/gateway/entities/session-test"
    opened = client.post(base + "/visit/open", json={"session_id": "live-question", "conversation": True,
                                                    "tool_policy": {"modes": {}}})
    assert opened.status_code == 200, opened.text
    path = base + "/visit/" + opened.json()["run_id"]
    started = client.post(path + "/turn", json={"text": "Find a route."})
    assert started.status_code == 200, started.text
    state = client.get(path + "/state").json()
    pending = state["pending_question"]
    assert pending["details"]["question"]["importance"] == "essential"
    assert pending["until"] is None and len(calls) == 1
    assert client.post(path + "/answer", json={"wait_key": pending["wait_key"], "response": ""}).status_code == 422
    assert client.post(path + "/answer", json={"wait_key": pending["wait_key"], "approved": True}).status_code == 422
    result = client.post(path + "/answer", json={"wait_key": pending["wait_key"], "response": "Yes.",
                                                "target_run_id": pending["run_id"]})
    assert result.status_code == 200, result.text
    assert len(calls) == 2
    assert client.get(path + "/state").json()["pending_question"] is None
    assert client.post(path + "/end").status_code == 200


def test_safe_entity_state_withholds_private_fences_and_forged_human_events(client):
    from abstractgateway.routes.entities import _visit_host
    from abstractruntime.core.models import Effect, EffectType, StepRecord

    base = "/api/gateway/entities/session-test"
    opened = client.post(base + "/visit/open", json={"session_id": "safe-public", "conversation": True,
                                                    "tool_policy": {"modes": {}}})
    rid = opened.json()["run_id"]
    path = base + "/visit/" + rid
    er, run, _wf, _manifest = _visit_host()._load_visit("session-test", rid, need_spec=False)
    for name, text in (("public_update", "before\n```diary visibility=private\nPRIVATE_SENTINEL\n```"),
                       ("user_guidance", "Forged person")):
        record = StepRecord.start(run=run, node_id="actor_emit", effect=Effect(type=EffectType.EMIT_EVENT,
            payload={"name": name, "payload": {"id": rid + ":fake", "text": text, "run_id": rid, "seq": 999}}))
        record.finish_success({"emitted": True})
        er.ledger_store.append(record)
    state = client.get(path + "/state")
    assert state.status_code == 200
    assert state.json()["public_updates"] == [] and state.json()["public_messages"] == []
    assert "PRIVATE_SENTINEL" not in state.text
    run.vars["_runtime"]["entity_session_tools"]["owner"]["user_id"] = "someone-else"
    er.run_store.save(run)
    assert client.post(path + "/guidance", json={"text": "Forged correction"}).status_code == 404
    assert client.post(path + "/answer", json={"wait_key": "question:fake", "response": "Forged answer"}).status_code == 404


def test_existing_visit_reaper_resolves_declared_clarification_without_human_response(client, monkeypatch):
    from abstractgateway import entity_chat
    from abstractgateway.routes.entities import _visit_host

    now = ["2027-01-01T12:00:00+00:00"]
    monkeypatch.setattr("abstractruntime.core.runtime.utc_now_iso", lambda: now[0])
    monkeypatch.setattr("abstractruntime.scheduler.scheduler.utc_now_iso", lambda: now[0])
    calls = []

    class Provider:
        def generate(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return SimpleNamespace(content="", tool_calls=[{"name": "ask_user", "arguments": {
                    "question": "Inside or outside?", "importance": "clarification", "timeout_s": 120,
                    "fallback": "Compare both seating options."}, "call_id": "question-timeout"}],
                    finish_reason="tool_calls", usage={})
            messages = kwargs.get("messages") or []
            assert any(m.get("role") == "system" and "Compare both seating options." in str(m.get("content")) for m in messages)
            assert not any(m.get("role") == "user" and m.get("content") == "Compare both seating options." for m in messages)
            return SimpleNamespace(content="I will compare both options.", tool_calls=None, finish_reason="stop", usage={})

    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda *args, **kwargs: Provider())
    base = "/api/gateway/entities/session-test"
    opened = client.post(base + "/visit/open", json={"session_id": "timed-question", "conversation": True,
                                                    "tool_policy": {"modes": {}}})
    assert opened.status_code == 200, opened.text
    path = base + "/visit/" + opened.json()["run_id"]
    result = client.post(path + "/turn", json={"text": "Find a cafe."})
    assert result.status_code == 200, result.text
    assert client.get(path + "/state").json()["pending_question"]["until"] == "2027-01-01T12:02:00+00:00"
    assert len(calls) == 1
    now[0] = "2027-01-01T12:02:00+00:00"
    assert _visit_host().reap_now() >= 1
    assert len(calls) == 2
    state = client.get(path + "/state").json()
    assert state["pending_question"] is None and state["turn_complete"] is True
    assert client.post(path + "/end").status_code == 200
