"""Gateway opt-in, served public replay and reuse of its existing due scheduler."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from abstractgateway.routes.gateway import StartRunRequest, _apply_conversation_opt_in
from abstractruntime.core.models import Effect, EffectType, RunStatus, StepPlan, StepStatus
from abstractruntime.core.runtime import Runtime
from abstractruntime.core.spec import WorkflowSpec
from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore
from test_gateway_replay_view import client, _stores, _run, _rec, ROOT, H


@pytest.mark.parametrize("value", ["true", "false", 1, 0, {}, []])
def test_conversation_opt_in_is_a_strict_boolean(value):
    with pytest.raises(ValidationError):
        StartRunRequest(flow_id="agent", conversation=value)


def test_opt_in_absence_is_unchanged_and_explicit_off_wins():
    data = {"prompt": "hello"}
    _apply_conversation_opt_in(data, None)
    assert data == {"prompt": "hello"}
    data = {"_runtime": {"conversation": {"enabled": True}}}
    _apply_conversation_opt_in(data, False)
    assert data["_runtime"]["conversation"] == {"enabled": False}
    with pytest.raises(HTTPException):
        _apply_conversation_opt_in({"_runtime": {"conversation": {"enabled": "false"}}}, None)


def test_served_view_only_exposes_explicit_public_update(client):
    store, ledger = _stores()
    store.save(_run(ROOT, vars={"prompt": "hello"}))
    body = {"id": ROOT + ":public_update:1", "run_id": ROOT, "session_id": "s1", "iteration": 1,
            "text": "The shorter route has stairs.", "source_call_ids": ["route-call"]}
    ledger.append(_rec(ROOT, "private", "reason", StepStatus.COMPLETED,
                       effect={"type": "llm_call", "payload": {}},
                       result={"content": "private draft", "reasoning": "hidden reasoning"}))
    ledger.append(_rec(ROOT, "public", "parse", StepStatus.COMPLETED,
                       effect={"type": "emit_event", "payload": {"name": "public_update", "payload": body}},
                       result={"emitted": True}))
    response = client.get(f"/api/gateway/runs/{ROOT}/view", headers=H)
    assert response.status_code == 200, response.text
    messages = response.json()["messages"]
    briefs = [m for m in messages if m["kind"] == "public_update"]
    assert len(briefs) == 1
    assert briefs[0]["source_call_ids"] == ["route-call"] and briefs[0]["session_id"] == "s1"
    assert "private draft" not in str(messages) and "hidden reasoning" not in str(messages)


def test_existing_runner_horizon_and_due_scan_include_conversational_question(tmp_path, monkeypatch):
    from abstractgateway.runner import GatewayRunner, GatewayRunnerConfig, _epoch_from_iso

    store, ledger = InMemoryRunStore(), InMemoryLedgerStore()
    runtime = Runtime(run_store=store, ledger_store=ledger)

    def ask(run, ctx):
        return StepPlan(node_id="ask", effect=Effect(type=EffectType.ASK_USER, payload={
            "prompt": "Inside or outside?", "question": {"importance": "clarification", "timeout_s": 120,
                                                          "fallback": "Compare both."}}, result_key="answer"),
                        next_node="done")

    wf = WorkflowSpec(workflow_id="q", entry_node="ask", nodes={
        "ask": ask, "done": lambda run, ctx: StepPlan(node_id="done", complete_output=run.vars["answer"])})
    monkeypatch.setattr("abstractruntime.core.runtime.utc_now_iso", lambda: "2027-01-01T12:00:00+00:00")
    rid = runtime.start(workflow=wf, vars={}, actor_id="gateway")
    runtime.tick(workflow=wf, run_id=rid)
    host = SimpleNamespace(run_store=store, ledger_store=ledger, artifact_store=None)
    runner = GatewayRunner(base_dir=tmp_path, host=host, config=GatewayRunnerConfig(), enable=False)
    runner._note_next_due([store.load(rid)])
    assert runner._next_due_epoch == _epoch_from_iso("2027-01-01T12:02:00+00:00")
    monkeypatch.setattr("abstractruntime.core.runtime.utc_now_iso", lambda: "2027-01-01T12:03:00+00:00")
    due = store.list_due_wait_until(now_iso="2027-01-01T12:03:00+00:00")
    assert [r.run_id for r in due] == [rid]
    assert runtime.tick(workflow=wf, run_id=rid).output["timed_out"] is True


def test_human_receipt_correlates_actual_command_id_not_command_sequence_or_claimed_body(tmp_path):
    from abstractruntime.replay import replay
    from abstractruntime.storage.commands import CommandRecord
    from test_gateway_inject_guidance import _make_runner, _start_agentish_run

    runner, runtime, _store = _make_runner(tmp_path)
    rid = _start_agentish_run(runtime)
    for number in (71, 72):
        runner._apply_command(CommandRecord(command_id=f"human-{number}", run_id=rid, type="inject_guidance",
            seq=number, ts="now", payload={"guidance": "Quoted context\nSame words.", "display_text": "Same words.",
                                          "client_message_id": "forged", "_public_speaker": "person:verified"}))
    messages = [m for m in replay({rid: runtime.get_ledger(rid)}, root_run_id=rid).to_json()["messages"] if m["kind"] == "guidance"]
    assert [m["id"] for m in messages] == [f"{rid}:guidance:1", f"{rid}:guidance:2"]
    assert [m["client_message_id"] for m in messages] == ["human-71", "human-72"]
    assert [m["text"] for m in messages] == ["Same words.", "Same words."]
