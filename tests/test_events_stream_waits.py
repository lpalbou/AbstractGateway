"""Interactive waits in the per-user stream (backlog 1046, operator: "where is the gating to humans").

A run parked on a person (a question, a tool approval) writes `attention.raised {kind: question |
approval, run_id, session_id, wait_key, step_id, record_key, prompt_ref}` (no prompt copied); its
resolution writes `attention.seen {kind, run_id, wait_key, step_id, outcome}`. Ids are idempotent
(`attention.raised:<run_id>:<wait_key>:<step_id>`); a boot pass repairs both after a crash."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import pytest

from abstractgateway.events_stream import stream_for, wait_attention_from_record, wait_outcome
from abstractgateway.runner import GatewayRunner
from abstractruntime import Effect, EffectType, Runtime, StepPlan, WorkflowSpec
from abstractruntime.core.models import RunStatus
from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore
from abstractruntime.storage.observable import ObservableLedgerStore


@dataclass
class _Host:
    runtime: Runtime
    registry: Dict[str, WorkflowSpec]
    run_store: Any
    ledger_store: Any
    artifact_store: Any = None

    def runtime_and_workflow_for_run(self, run_id: str):
        run = self.run_store.load(str(run_id))
        return self.runtime, self.registry[str(run.workflow_id)]


def _ask_wf() -> WorkflowSpec:
    def ask(run, ctx) -> StepPlan:
        return StepPlan(node_id="ask", effect=Effect(type=EffectType.ASK_USER, payload={"prompt": "SECRET QUESTION?"}, result_key="answer"), next_node="done")

    def done(run, ctx) -> StepPlan:
        return StepPlan(node_id="done", complete_output={"answer": run.vars.get("answer")})

    return WorkflowSpec(workflow_id="ask", entry_node="ask", nodes={"ask": ask, "done": done})


def _make(tmp_path: Path, *, run_store=None, ledger=None, attach: bool = True):
    run_store = run_store or InMemoryRunStore()
    ledger = ledger or ObservableLedgerStore(InMemoryLedgerStore())
    runtime = Runtime(run_store=run_store, ledger_store=ledger)
    wf = _ask_wf()
    runner = GatewayRunner(base_dir=tmp_path, host=_Host(runtime, {"ask": wf}, run_store, ledger), enable=False)
    runner._subscribe_ledger()
    stream = stream_for(tmp_path / "data", "default", "alice")
    if attach:
        runner.attach_user_stream(stream)
        runner._user_stream_rebuilt = True
    return runtime, wf, runner, stream, run_store, ledger


def _waits(stream) -> list:
    return [(e["type"], e["data"]) for e in stream.read(after=0, limit=100) if e["data"].get("kind") in ("question", "approval")]


def test_a_question_raises_then_its_answer_is_seen(tmp_path: Path) -> None:
    runtime, wf, runner, stream, _rs, _l = _make(tmp_path)
    rid = runtime.start(workflow=wf, actor_id="gateway", session_id="s1")
    runner._tick_run(rid)
    runner._drain_user_stream()
    (raised,) = [d for t, d in _waits(stream) if t == "attention.raised"]
    assert raised["kind"] == "question" and raised["run_id"] == rid and raised["session_id"] == "s1"
    assert raised["wait_key"] == f"user:{rid}:ask" and raised["step_id"] and raised["prompt_ref"] == f"/api/gateway/runs/{rid}"
    assert "SECRET QUESTION" not in json.dumps(stream.read(after=0, limit=100))
    ids = [e["id"] for e in stream.read(after=0, limit=100)]
    assert f"attention.raised:{rid}:user:{rid}:ask:{raised['step_id']}" in ids

    runner._drain_user_stream()  # idempotent
    runtime.resume(workflow=wf, run_id=rid, wait_key=raised["wait_key"], payload={"response": "yes"}, max_steps=0)
    runner._tick_run(rid)
    seen = [d for t, d in _waits(stream) if t == "attention.seen"]
    assert seen == [{"kind": "question", "run_id": rid, "wait_key": raised["wait_key"], "step_id": raised["step_id"], "outcome": "answered"}]
    assert runner._open_waits == {} and stream.state()["open_waits"] == {}


def test_a_cancelled_wait_is_seen_as_cancelled(tmp_path: Path) -> None:
    runtime, wf, runner, stream, rs, _l = _make(tmp_path)
    rid = runtime.start(workflow=wf, actor_id="gateway", session_id="s1")
    runner._tick_run(rid)
    runner._drain_user_stream()
    runner._apply_run_control("cancel", run_id=rid, payload={}, apply_to_tree=False)
    assert [d["outcome"] for t, d in _waits(stream) if t == "attention.seen"] == ["cancelled"]


def test_approval_records_and_outcomes() -> None:
    record = {"run_id": "r1", "step_id": "st1", "idempotency_key": "k1", "result": {"wait": {
        "reason": "user", "wait_key": "approve:r1", "result_key": "_temp.approval", "details": {"mode": "approval_required", "tool_calls": [{"name": "rm"}]}}}}
    item = wait_attention_from_record(record)
    assert item["kind"] == "approval" and item["wait_kind"] == "tool_approval" and item["record_key"] == "k1"
    assert "tool_calls" not in json.dumps(item)

    class _Run:
        status = RunStatus.RUNNING
        vars = {"_temp": {"approval": {"approved": False}}}

    assert wait_outcome(_Run(), item) == "denied"
    _Run.vars = {"_temp": {"approval": {"approved": True}}}
    assert wait_outcome(_Run(), item) == "approved"
    # Not a person: pauses, controller wakes, plain event waits.
    assert wait_attention_from_record({**record, "result": {"wait": {"reason": "user", "wait_key": "pause:r1"}}}) is None
    assert wait_attention_from_record({**record, "result": {"wait": {"reason": "event", "wait_key": "automation:a:wake"}}}) is None
    assert wait_attention_from_record({**record, "result": {"wait": {"reason": "event", "wait_key": "evt:x"}}}) is None
    assert wait_attention_from_record({**record, "result": {"wait": {"reason": "event", "wait_key": "evt:x", "prompt": "?"}}})["kind"] == "question"


def test_waits_are_repaired_at_boot(tmp_path: Path) -> None:
    from abstractruntime.storage.json_files import JsonFileRunStore, JsonlLedgerStore

    rs = JsonFileRunStore(tmp_path / "runs")
    ledger = ObservableLedgerStore(JsonlLedgerStore(tmp_path / "runs"))
    runtime, wf, runner, stream, _rs, _l = _make(tmp_path, run_store=rs, ledger=ledger, attach=False)
    stream.append("attention.seen", event_id="seed", data={"automation_id": "a", "seq": 1})  # a stream in use
    rid = runtime.start(workflow=wf, actor_id="gateway", session_id="s9")
    runner._tick_run(rid)  # parked on a person; the process "died" before the append

    runner.attach_user_stream(stream)
    runner._drain_user_stream()  # boot repair raises it, with the step from the ledger
    (raised,) = [d for t, d in _waits(stream) if t == "attention.raised"]
    assert raised["run_id"] == rid and raised["step_id"]

    # Answered while no runner watched (another process); the next boot closes it.
    runner2 = GatewayRunner(base_dir=tmp_path, host=runner._host, enable=False)
    runtime.resume(workflow=wf, run_id=rid, wait_key=raised["wait_key"], payload={"response": "ok"}, max_steps=0)
    runtime.tick(workflow=wf, run_id=rid)
    assert rs.load(rid).status == RunStatus.COMPLETED
    runner2.attach_user_stream(stream)
    runner2._drain_user_stream()
    assert [d["outcome"] for t, d in _waits(stream) if t == "attention.seen"] == ["answered"]
    runner2.attach_user_stream(stream)
    runner2._drain_user_stream()  # idempotent
    assert len(_waits(stream)) == 2


# --- two principals through the real app ------------------------------------------------------

from test_gateway_principal_isolation_matrix import _client, _create_user  # noqa: E402
from test_me_events_routes import _wait  # noqa: E402


def test_each_principal_gets_only_their_own_waits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from automations_fixtures import ECHO_BUNDLE_ID, write_echo_bundle

    client = _client(tmp_path, monkeypatch)
    with client:
        alice, bob = _create_user(client, "alice"), _create_user(client, "bob")
        runs = {}
        for user, headers in (("alice", alice), ("bob", bob)):
            write_echo_bundle(tmp_path / "runtime" / "users" / "default" / user / "flows")
            r = client.post("/api/gateway/runs/start", headers=headers, json={"bundle_id": ECHO_BUNDLE_ID, "flow_id": "ask", "input_data": {}})
            assert r.status_code == 200, r.text
            runs[user] = r.json()["run_id"]

        def _raised(headers: dict) -> list:
            events = client.get("/api/gateway/me/events", headers=headers).json()["events"]
            return [e for e in events if e["type"] == "attention.raised" and e["data"].get("kind") == "question"]

        _wait(lambda: len(_raised(alice)) == 1 and len(_raised(bob)) == 1)
        a, b = _raised(alice)[0]["data"], _raised(bob)[0]["data"]
        assert a["run_id"] == runs["alice"] and b["run_id"] == runs["bob"]

        r = client.post("/api/gateway/commands", headers=alice, json={
            "command_id": "c-answer-1", "run_id": a["run_id"], "type": "resume", "payload": {"wait_key": a["wait_key"], "payload": {"response": "yes"}}})
        assert r.status_code == 200, r.text

        def _seen(headers: dict) -> list:
            events = client.get("/api/gateway/me/events", headers=headers).json()["events"]
            return [e["data"] for e in events if e["type"] == "attention.seen" and e["data"].get("kind") == "question"]

        _wait(lambda: len(_seen(alice)) == 1)
        assert _seen(alice)[0]["outcome"] == "answered" and _seen(alice)[0]["step_id"] == a["step_id"]
        assert _seen(bob) == []  # bob's question is still open, and never in alice's stream
        assert runs["bob"] not in json.dumps(client.get("/api/gateway/me/events", headers=alice).json())


def test_a_draft_test_run_parked_on_a_person_raises_no_attention(tmp_path: Path) -> None:
    """A flow editor draft test (`_run_lifecycle.purpose == draft_test`) is hidden from the run
    listings, so a wait on it would be a badge nobody can act on (watch seat, 2026-10-26): it
    never raises attention, and a stale open wait for one is closed at boot."""
    from abstractruntime.storage.json_files import JsonFileRunStore, JsonlLedgerStore

    rs = JsonFileRunStore(tmp_path / "runs")
    ledger = ObservableLedgerStore(JsonlLedgerStore(tmp_path / "runs"))
    runtime, wf, runner, stream, _rs, _l = _make(tmp_path, run_store=rs, ledger=ledger, attach=False)
    stream.append("attention.seen", event_id="seed", data={"automation_id": "a", "seq": 1})
    rid = runtime.start(
        workflow=wf, actor_id="gateway", session_id="s9",
        vars={"_run_lifecycle": {"source": "abstractflow.editor", "purpose": "draft_test", "visibility": "private"}},
    )
    runner._tick_run(rid)  # parked on a person, but hidden from every listing
    runner.attach_user_stream(stream)
    runner._drain_user_stream()  # boot repair: nothing raised for it
    assert [t for t, _ in _waits(stream) if t == "attention.raised"] == []
    # A live park (not at boot) raises nothing either.
    runner._raise_wait({"run_id": rid, "wait_key": "k", "step_id": "st", "kind": "question"})
    assert [t for t, _ in _waits(stream) if t == "attention.raised"] == []
    # A stale open wait for it from an older gateway is closed at boot, so the badge goes away.
    with runner._waits_lock:
        runner._open_waits[rid] = {"run_id": rid, "wait_key": "k", "step_id": "st", "kind": "question", "session_id": "s9"}
        runner._save_open_waits()
    runner2 = GatewayRunner(base_dir=tmp_path, host=runner._host, enable=False)
    runner2.attach_user_stream(stream)
    runner2._drain_user_stream()
    assert [d["outcome"] for t, d in _waits(stream) if t == "attention.seen"] == ["cancelled"]
    assert rid not in runner2._open_waits
