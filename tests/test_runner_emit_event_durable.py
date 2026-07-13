"""GatewayRunner durable event delivery (`emit_event` with durable=true).

Plain emit_event only resumes runs parked on the wait key; events sent while a
listener is busy are dropped. With `durable: true` the runner ALSO appends the
envelope (with a per-run monotonic `seq`) to the `events_inbox` run var of every
non-terminal run declaring the mailbox (`events_mailbox` var == event name).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from abstractgateway.runner import GatewayRunner
from abstractruntime import Effect, EffectType, Runtime, StepPlan, WorkflowSpec
from abstractruntime.core.models import RunStatus, WaitReason
from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore

MAILBOX = "box-a"
WAIT_KEY = f"evt:global:global:{MAILBOX}"


@dataclass
class _Host:
    runtime: Runtime
    registry: Dict[str, WorkflowSpec]
    run_store: InMemoryRunStore
    ledger_store: InMemoryLedgerStore
    artifact_store: Any = None

    def runtime_and_workflow_for_run(self, run_id: str) -> tuple[Runtime, WorkflowSpec]:
        run = self.run_store.load(str(run_id))
        if run is None:
            raise KeyError(f"Run '{run_id}' not found")
        spec = self.registry.get(str(run.workflow_id))
        if spec is None:
            raise KeyError(f"Workflow '{run.workflow_id}' not registered")
        return self.runtime, spec


def _listener_workflow() -> WorkflowSpec:
    def park(run, ctx) -> StepPlan:
        return StepPlan(
            node_id="park",
            effect=Effect(type=EffectType.WAIT_EVENT, payload={"wait_key": WAIT_KEY}, result_key="_temp.evt"),
            next_node="done",
        )

    def done(run, ctx) -> StepPlan:
        return StepPlan(node_id="done", complete_output={"evt": run.vars.get("_temp", {}).get("evt")})

    return WorkflowSpec(workflow_id="listener", entry_node="park", nodes={"park": park, "done": done})


def _make(tmp_path: Path):
    run_store = InMemoryRunStore()
    ledger_store = InMemoryLedgerStore()
    runtime = Runtime(run_store=run_store, ledger_store=ledger_store)
    wf = _listener_workflow()
    host = _Host(runtime=runtime, registry={"listener": wf}, run_store=run_store, ledger_store=ledger_store)
    runner = GatewayRunner(base_dir=tmp_path, host=host)
    return run_store, runtime, wf, runner


def test_durable_emit_queues_to_busy_run_and_resumes_parked_run(tmp_path: Path) -> None:
    run_store, runtime, wf, runner = _make(tmp_path)

    # Run A: parked on the channel key, declares the mailbox.
    run_a = runtime.start(workflow=wf, vars={"events_mailbox": MAILBOX}, actor_id="gateway")
    state_a = runtime.tick(workflow=wf, run_id=run_a)
    assert state_a.status == RunStatus.WAITING and state_a.waiting.wait_key == WAIT_KEY

    # Run B: busy (RUNNING, not waiting), declares the same mailbox.
    run_b = runtime.start(workflow=wf, vars={"events_mailbox": MAILBOX}, actor_id="gateway")
    assert run_store.load(run_b).status == RunStatus.RUNNING

    # Run C: busy but declares a DIFFERENT mailbox - must not receive anything.
    run_c = runtime.start(workflow=wf, vars={"events_mailbox": "other-box"}, actor_id="gateway")

    runner._apply_emit_event(
        {
            "name": MAILBOX,
            "scope": "global",
            "durable": True,
            "payload": {"kind": "message", "from": "laurent", "body": "hello residents"},
        },
        default_session_id="ignored",
        client_id="test-client",
    )

    # Parked run A: resumed (wait satisfied) AND got the envelope in its mailbox.
    a = run_store.load(run_a)
    assert a.status in (RunStatus.RUNNING, RunStatus.COMPLETED)  # resume(max_steps=0) leaves it ready to tick
    inbox_a = a.vars.get("events_inbox")
    assert isinstance(inbox_a, list) and len(inbox_a) == 1
    assert inbox_a[0]["seq"] == 1
    assert inbox_a[0]["payload"]["body"] == "hello residents"

    # Busy run B: envelope queued durably (this is the interleave path).
    b = run_store.load(run_b)
    inbox_b = b.vars.get("events_inbox")
    assert isinstance(inbox_b, list) and len(inbox_b) == 1
    assert inbox_b[0]["seq"] == 1
    assert b.vars.get("events_inbox_seq") == 1

    # Unrelated mailbox: untouched.
    c = run_store.load(run_c)
    assert c.vars.get("events_inbox") is None

    # Second event: per-run seq is monotonic.
    runner._apply_emit_event(
        {"name": MAILBOX, "scope": "global", "durable": True, "payload": {"body": "second"}},
        default_session_id="ignored",
        client_id=None,
    )
    b2 = run_store.load(run_b)
    assert [e["seq"] for e in b2.vars["events_inbox"]] == [1, 2]


def test_non_durable_emit_does_not_queue(tmp_path: Path) -> None:
    run_store, runtime, wf, runner = _make(tmp_path)
    run_b = runtime.start(workflow=wf, vars={"events_mailbox": MAILBOX}, actor_id="gateway")

    runner._apply_emit_event(
        {"name": MAILBOX, "scope": "global", "payload": {"body": "dropped for busy runs"}},
        default_session_id="ignored",
        client_id=None,
    )
    assert run_store.load(run_b).vars.get("events_inbox") is None


def test_emit_returns_receiver_counts_and_dedups_by_event_id(tmp_path: Path) -> None:
    """The bridge-adversary F2/F4 contract, proven through the real runner:
    emit_event returns {resumed, appended}; a zero-receiver emit reports 0+0
    (the bridge's cursor-hold signal); an at-least-once replay with the same
    event_id lands ONCE in the inbox (exactly-once mailbox delivery)."""
    run_store, runtime, wf, runner = _make(tmp_path)

    # Zero receivers: no run declares this mailbox.
    counts = runner.emit_event(
        name="empty-box", scope="global", payload={"body": "into the void"},
        session_id="s", durable=True,
    )
    assert counts == {"resumed": 0, "appended": 0}, "zero receivers must be visible to the caller"

    # One busy resident declares the mailbox.
    run_b = runtime.start(workflow=wf, vars={"events_mailbox": MAILBOX}, actor_id="gateway")

    first = runner.emit_event(
        name=MAILBOX, scope="global", payload={"body": "hi"},
        session_id="s", event_id="agora:resident-a:commons:7", durable=True,
    )
    assert first["appended"] == 1, "a declaring resident is a receiver"

    # Replay the SAME event_id (crash-between-emit-and-cursor-save): dedups.
    replay = runner.emit_event(
        name=MAILBOX, scope="global", payload={"body": "hi"},
        session_id="s", event_id="agora:resident-a:commons:7", durable=True,
    )
    assert replay["appended"] == 1, "replay still counts as received (cursor may advance)"
    inbox = run_store.load(run_b).vars["events_inbox"]
    assert len(inbox) == 1, f"event_id dedup must land the replay once, got {len(inbox)}"

    # A DISTINCT event_id appends a second entry.
    runner.emit_event(
        name=MAILBOX, scope="global", payload={"body": "next"},
        session_id="s", event_id="agora:resident-a:commons:8", durable=True,
    )
    assert len(run_store.load(run_b).vars["events_inbox"]) == 2


def test_three_residents_each_receive_a_targeted_event(tmp_path: Path) -> None:
    """Fleet shape (P2 done-bar, in-process proof): three residents on three
    mailboxes; a per-mailbox durable emit reaches exactly its own resident —
    the N-agents-one-runner topology the agora bridge drives."""
    run_store, runtime, wf, runner = _make(tmp_path)

    residents = {
        "alice": runtime.start(workflow=wf, vars={"events_mailbox": "alice-box"}, actor_id="gateway"),
        "bob": runtime.start(workflow=wf, vars={"events_mailbox": "bob-box"}, actor_id="gateway"),
        "carol": runtime.start(workflow=wf, vars={"events_mailbox": "carol-box"}, actor_id="gateway"),
    }

    for name, run_id in residents.items():
        counts = runner.emit_event(
            name=f"{name}-box", scope="global",
            payload={"kind": "agora_message", "from": "chair", "body": f"task for {name}"},
            session_id=f"agora:{name}", event_id=f"agora:{name}:commons:1", durable=True,
        )
        assert counts["appended"] == 1

    # Each resident got its OWN task and nothing else.
    for name, run_id in residents.items():
        inbox = run_store.load(run_id).vars["events_inbox"]
        assert len(inbox) == 1, f"{name} should have exactly one message"
        assert inbox[0]["payload"]["body"] == f"task for {name}"


def test_durable_emit_respects_list_declarations_and_cap(tmp_path: Path) -> None:
    run_store, runtime, wf, runner = _make(tmp_path)

    # Mailbox declared as a list (agent listening on several channels).
    run_b = runtime.start(workflow=wf, vars={"events_mailbox": ["other", MAILBOX]}, actor_id="gateway")

    # Pre-fill the inbox to the cap to exercise drop-oldest accounting.
    run = run_store.load(run_b)
    run.vars["events_inbox"] = [{"seq": i + 1, "payload": {"body": f"old-{i}"}} for i in range(GatewayRunner._EVENTS_INBOX_CAP)]
    run.vars["events_inbox_seq"] = GatewayRunner._EVENTS_INBOX_CAP
    run_store.save(run)

    runner._apply_emit_event(
        {"name": MAILBOX, "scope": "global", "durable": True, "payload": {"body": "newest"}},
        default_session_id="ignored",
        client_id=None,
    )

    b = run_store.load(run_b)
    inbox = b.vars["events_inbox"]
    assert len(inbox) == GatewayRunner._EVENTS_INBOX_CAP
    assert inbox[0]["payload"]["body"] == "old-1"  # oldest dropped
    assert inbox[-1]["payload"]["body"] == "newest"
    assert b.vars.get("events_inbox_dropped") == 1
