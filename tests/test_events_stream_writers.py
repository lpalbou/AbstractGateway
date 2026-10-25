"""Per-user event stream writers (backlog 1046): the stream holds NOTHING that is a step of a run.

Run pointers are written after the run store save, idempotently (`run.started:<run_id>`), and a
boot pass rebuilds them from the run index after a crash between save and append; attention
events carry the ledger `record_key`, never a body; seen cursors and notification outcomes land in
the same stream."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import pytest

from abstractgateway.events_stream import EVENT_TYPES, stream_for, to_cloudevent
from abstractgateway.runner import GatewayRunner
from abstractruntime import Runtime, StepPlan, WorkflowSpec
from abstractruntime.core.models import RunStatus
from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore


@dataclass
class _Host:
    runtime: Runtime
    registry: Dict[str, WorkflowSpec]
    run_store: InMemoryRunStore
    ledger_store: InMemoryLedgerStore
    artifact_store: Any = None

    def runtime_and_workflow_for_run(self, run_id: str):
        run = self.run_store.load(str(run_id))
        return self.runtime, self.registry[str(run.workflow_id)]


def _one_step() -> WorkflowSpec:
    def done(run, ctx) -> StepPlan:
        return StepPlan(node_id="done", complete_output={"answer": "ok"})

    return WorkflowSpec(workflow_id="one", entry_node="done", nodes={"done": done})


def _make(tmp_path: Path, *, attach: bool = True):
    run_store, ledger_store = InMemoryRunStore(), InMemoryLedgerStore()
    runtime = Runtime(run_store=run_store, ledger_store=ledger_store)
    wf = _one_step()
    host = _Host(runtime=runtime, registry={"one": wf}, run_store=run_store, ledger_store=ledger_store)
    runner = GatewayRunner(base_dir=tmp_path, host=host, enable=False)
    stream = stream_for(tmp_path / "data", "default", "alice")
    if attach:
        runner.attach_user_stream(stream)
    return runtime, wf, runner, stream


def _types(stream) -> list:
    return [e["type"] for e in stream.read(after=0, limit=1000)]


def test_cloudevent_shape_idempotency_and_vocabulary(tmp_path: Path) -> None:
    stream = stream_for(tmp_path, "default", "alice")
    ev, created = stream.append("run.started", event_id="run.started:r1", subject="r1", data={"run_id": "r1"})
    assert created and set(ev) == {"specversion", "id", "type", "source", "subject", "time", "data", "sequence"}
    assert ev["specversion"] == "1.0" and ev["id"] == "run.started:r1" and ev["sequence"] == 1
    again, created2 = stream.append("run.started", event_id="run.started:r1", data={"run_id": "r1"})
    assert not created2 and again["sequence"] == 1 and stream.head() == 1
    with pytest.raises(ValueError):
        stream.append("llm.call", event_id="x", data={})  # a step is never a stream event
    assert "llm.call" not in EVENT_TYPES and stream.name == "user:default:alice"
    assert to_cloudevent({"seq": 3, "event_id": "e", "payload": {"type": "run.finished", "data": {}}})["sequence"] == 3


def test_tick_writes_run_started_then_finished_after_the_save(tmp_path: Path) -> None:
    runtime, wf, runner, stream = _make(tmp_path)
    rid = runtime.start(workflow=wf, vars={"_meta": {"origin": {"kind": "person", "actor": "alice", "client": None, "ref": {}}}}, actor_id="gateway", session_id="s1")
    runner._tick_run(rid)
    events = stream.read(after=0, limit=10)
    assert [e["type"] for e in events] == ["run.started", "run.finished"]
    assert events[0]["id"] == f"run.started:{rid}"
    assert events[0]["data"] == {"run_id": rid, "session_id": "s1", "origin": {"kind": "person", "actor": "alice", "client": None, "ref": {}}}
    assert events[1]["data"]["status"] == "completed"
    # Nothing that is a step: no answer, no output in any pointer.
    assert all("answer" not in str(e["data"]) for e in events)
    runner._tick_run(rid)  # a re-tick of a finished run appends nothing
    assert stream.head() == 2


def test_child_runs_and_controllers_get_no_pointers(tmp_path: Path) -> None:
    runtime, wf, runner, stream = _make(tmp_path)
    parent = runtime.start(workflow=wf, actor_id="gateway")
    child = runtime.start(workflow=wf, actor_id="gateway", parent_run_id=parent)
    controller = runtime.start(workflow=wf, actor_id="gateway", vars={"_meta": {"automation": {"title": "x"}}})
    runner._tick_run(child)
    runner._tick_run(controller)
    assert stream.head() == 0


def test_a_crash_between_save_and_append_is_repaired_from_the_run_index(tmp_path: Path) -> None:
    runtime, wf, runner, stream = _make(tmp_path, attach=False)
    stream.append("attention.seen", event_id="seed", data={"automation_id": "a", "seq": 1})  # a stream in use
    lost = runtime.start(workflow=wf, actor_id="gateway", session_id="s2")
    runner._tick_run(lost)  # saved COMPLETED; the process "died" before any append
    assert runtime.run_store.load(lost).status == RunStatus.COMPLETED and stream.head() == 1

    runner.attach_user_stream(stream)  # the restarted process
    runner._drain_user_stream()  # boot repair
    assert _types(stream) == ["attention.seen", "run.started", "run.finished"]
    runner.attach_user_stream(stream)
    runner._drain_user_stream()  # a second repair is idempotent
    assert stream.head() == 3


def test_a_new_stream_takes_no_history(tmp_path: Path) -> None:
    runtime, wf, runner, stream = _make(tmp_path, attach=False)
    old = runtime.start(workflow=wf, actor_id="gateway")
    runner._tick_run(old)
    runner.attach_user_stream(stream)
    runner._drain_user_stream()
    assert stream.head() == 0


def _completed_record(aid: str, seq: int) -> Dict[str, Any]:
    return {
        "run_id": aid,
        "idempotency_key": f"automation:completed:{aid}:{seq}",
        "effect": {
            "type": "emit_event",
            "payload": {
                "name": "automation.completed",
                "payload": {"run_id": f"occ-{seq}", "index": seq, "attention": {"kind": "notify", "seq": seq, "title": "T", "body": "SECRET BODY", "channels": ["console", "email"]}},
            },
        },
    }


def test_attention_raised_carries_the_record_key_never_the_body(tmp_path: Path) -> None:
    _runtime, _wf, runner, stream = _make(tmp_path)
    runner._user_stream_rebuilt = True  # skip the boot pass (no automations here)
    runner._on_ledger_record(_completed_record("auto1", 1))
    runner._on_ledger_record({"run_id": "x", "effect": {"type": "emit_event", "payload": {"name": "automation.admitted", "payload": {}}}})
    runner._drain_user_stream()
    events = stream.read(after=0, limit=10)
    assert [e["type"] for e in events] == ["attention.raised", "notification.sent"]
    raised = events[0]["data"]
    assert raised["record_key"] == "automation:completed:auto1:1" and raised["seq"] == 1 and raised["automation_id"] == "auto1"
    assert "SECRET BODY" not in str(events) and "title" not in raised and "body" not in raised
    assert events[1]["data"]["channel"] == "console"
    runner._on_ledger_record(_completed_record("auto1", 1))  # replayed record
    runner._drain_user_stream()
    assert stream.head() == 2
    assert stream.state()["attention_floor"] == {"auto1": 1}


def test_seen_cursor_moves_write_attention_seen(tmp_path: Path) -> None:
    from abstractgateway.automation_attention import AttentionPreferenceStore

    store = AttentionPreferenceStore(tmp_path, tenant="default", user="alice")
    store.mark_seen("auto1", 2)
    store.mark_seen("auto1", 2)  # no move, no event
    store.mark_seen("auto1", 1)
    events = stream_for(tmp_path, "default", "alice").read(after=0, limit=10)
    assert [(e["type"], e["data"]) for e in events] == [("attention.seen", {"automation_id": "auto1", "seq": 2})]
    assert stream_for(tmp_path, "default", "bob").head() == 0


def test_email_notice_outcomes_are_recorded(tmp_path: Path) -> None:
    from abstractgateway.mail.accounts import EmailPlane
    from abstractgateway.mail.core_mail import EmailError
    from abstractgateway.mail.notifications import NotificationOutbox

    plane = EmailPlane("default", "alice", "alice", tmp_path, False)
    outbox = NotificationOutbox(plane)
    outbox.enqueue("k1", "automation_result", "s", "t", subject_ref="automation:a1")
    outbox._mark_failed(["k1"], EmailError("Refused.", "Fix it.", code="email_refused"))
    events = stream_for(plane.runtime_data_dir, "default", "alice").read(after=0, limit=10)
    assert [e["type"] for e in events] == ["notification.failed"]
    assert events[0]["data"] == {"channel": "email", "notice": "k1", "kind": "automation_result", "subject_ref": "automation:a1", "code": "email_refused", "cause": "Refused."}
