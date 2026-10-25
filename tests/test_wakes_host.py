"""The gateway's SCHEDULE_RUN host hook (backlog 1045 host half, 1046 events).

An agent-style run (a SCHEDULE_RUN effect carrying `tool_name`/`call_id`, exactly what the
`schedule_run` builtin emits) asks for a reminder; it ends; the GATEWAY RUNNER (the real scheduling
pass and ticks, no test driver) fires the one-shot automation, and the new run appears in the SAME
session with the `[Trigger …]` header. The user stream shows `wake.scheduled`, then the
occurrence's `trigger.event` / `run.started`; archiving a wake before it fires writes
`wake.cancelled` and nothing runs."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from abstractgateway.events_stream import stream_for
from abstractgateway.runner import GatewayRunner, GatewayRunnerConfig
from abstractgateway.wakes_host import ScheduleRunRefused, target_for, wire_schedule_run
from abstractruntime import Effect, EffectType, Runtime, StepPlan, WorkflowSpec
from abstractruntime.automations import register_controller_bundle
from abstractruntime.core.models import RunState, RunStatus
from abstractruntime.email.inbox import InMemoryEventInbox
from abstractruntime.scheduler.registry import WorkflowRegistry
from abstractruntime.storage.artifacts import FileArtifactStore
from abstractruntime.storage.json_files import JsonFileRunStore, JsonlLedgerStore
from abstractruntime.storage.observable import ObservableLedgerStore

CHAT = "fixtures@1.0.0:chat"


def _echo(run, ctx) -> StepPlan:
    return StepPlan(node_id="answer", complete_output={"response": f"echo: {run.vars.get('prompt')}"})


def _chat_go(run, ctx) -> StepPlan:
    if run.vars.get("schedule") is None:  # the occurrence: a plain answer
        return StepPlan(node_id="go", complete_output={"response": f"echo: {run.vars.get('prompt')}"})
    payload = {**run.vars["schedule"], "tool_name": "schedule_run", "call_id": "call-1"}
    return StepPlan(node_id="go", effect=Effect(type=EffectType.SCHEDULE_RUN, payload=payload, result_key="wake"), next_node="done")


def _chat_done(run, ctx) -> StepPlan:
    return StepPlan(node_id="done", complete_output={"response": "I will remind you.", "wake": run.vars.get("wake")})


class _Host:
    def __init__(self, runtime: Runtime, registry: WorkflowRegistry) -> None:
        self.runtime, self.registry = runtime, registry
        self.run_store, self.ledger_store, self.artifact_store = runtime.run_store, runtime.ledger_store, runtime.artifact_store

    def runtime_and_workflow_for_run(self, run_id: str):
        run = self.run_store.load(str(run_id))
        return self.runtime, self.registry.get(str(run.workflow_id))


@pytest.fixture
def env(tmp_path: Path):
    registry = WorkflowRegistry()
    registry.register(WorkflowSpec(workflow_id=CHAT, entry_node="go", nodes={"go": _chat_go, "done": _chat_done}))
    register_controller_bundle(registry)
    runs = tmp_path / "data"
    runtime = Runtime(run_store=JsonFileRunStore(runs), ledger_store=ObservableLedgerStore(JsonlLedgerStore(runs)),
                      workflow_registry=registry, artifact_store=FileArtifactStore(runs))
    runtime.set_event_inbox(InMemoryEventInbox())
    wire_schedule_run(runtime, data_dir=runs, root_data_dir=runs, tenant="default", user="alice")
    runner = GatewayRunner(base_dir=runs, host=_Host(runtime, registry), config=GatewayRunnerConfig(scan_gate_idle_interval_s=0.05), enable=False)
    stream = stream_for(runs, "default", "alice")
    runner.attach_user_stream(stream)
    runner._user_stream_rebuilt = True
    runner._subscribe_ledger()
    runner._submit_tick = lambda rid, **_kw: runner._tick_run(rid)  # type: ignore[method-assign]
    return runtime, runner, stream


def _pass(runner: GatewayRunner) -> None:
    """One runner loop iteration (the parts that schedule and tick)."""
    runner._drain_direct_ticks()
    if runner._scan_pass_due():
        runner._schedule_ticks()
    runner._drain_user_stream()


def _ask(runtime: Runtime, runner: GatewayRunner, schedule: dict) -> RunState:
    rid = runtime.start(workflow=runtime.workflow_registry.get(CHAT), vars={"schedule": schedule, "_runtime": {"allowed_tools": ["read_file", "schedule_run"]}},
                        session_id="chat-S", actor_id="gateway")
    runner._tick_run(rid)
    return runtime.run_store.load(rid)


def test_a_reminder_fires_later_as_a_new_turn_of_the_same_session(env) -> None:
    runtime, runner, stream = env
    caller = _ask(runtime, runner, {"after_s": 1, "note": "check the oven"})
    assert caller.status == RunStatus.COMPLETED  # the agent's run ENDS; nothing waits in the chat
    (result,) = caller.output["wake"]["results"]
    assert result["success"] is True, result
    aid = result["output"]["automation_id"]
    assert result["output"]["session_id"] == "chat-S" and result["output"]["kind"] == "note"
    runner._drain_user_stream()
    scheduled = [e for e in stream.read(after=0, limit=100) if e["type"] == "wake.scheduled"]
    assert len(scheduled) == 1 and scheduled[0]["id"] == f"wake.scheduled:{aid}"
    assert scheduled[0]["data"]["session_id"] == "chat-S" and scheduled[0]["data"]["origin_run_id"] == caller.run_id

    occurrence = None
    deadline = time.time() + 20
    while time.time() < deadline and occurrence is None:
        _pass(runner)
        kids = [r for r in runtime.run_store.list_children(parent_run_id=aid)]
        done = [k for k in kids if k.status == RunStatus.COMPLETED]
        occurrence = done[0] if done else None
        time.sleep(0.05)
    assert occurrence is not None, "the gateway runner never fired the wake"
    assert occurrence.session_id == "chat-S"
    assert occurrence.vars["prompt"].startswith("[Trigger schedule@2") and "check the oven" in occurrence.vars["prompt"]
    assert occurrence.workflow_id == CHAT  # the session's own agent
    assert occurrence.vars["_runtime"]["allowed_tools"] == []  # a note gets no tools
    assert isinstance(occurrence.vars.get("workspace_root"), str)  # the gateway's guard ran

    _pass(runner)
    types = [e["type"] for e in stream.read(after=0, limit=100)]
    i = types.index("wake.scheduled")
    assert "trigger.event" in types[i:] and types.count("run.started") == 2  # the chat run, then the wake's run
    started = [e for e in stream.read(after=0, limit=100) if e["type"] == "run.started"]
    assert started[-1]["data"]["run_id"] == occurrence.run_id and started[-1]["data"]["session_id"] == "chat-S"
    assert started[-1]["data"]["origin"]["kind"] == "agent"


def test_archiving_a_wake_before_it_fires_writes_wake_cancelled(env) -> None:
    runtime, runner, stream = env
    caller = _ask(runtime, runner, {"after_s": 300, "note": "never"})
    aid = caller.output["wake"]["results"][0]["output"]["automation_id"]
    _pass(runner)
    runner._apply_automation_command(SimpleNamespace(command_id="arch-1", client_id="test"), typ="automation.archive", automation_id=aid, payload={})
    cancelled = [e for e in stream.read(after=0, limit=100) if e["type"] == "wake.cancelled"]
    assert [e["id"] for e in cancelled] == [f"wake.cancelled:{aid}"]
    assert cancelled[0]["data"] == {"automation_id": aid, "session_id": "chat-S", "origin_run_id": caller.run_id}
    for _ in range(5):
        _pass(runner)
    assert runtime.run_store.list_children(parent_run_id=aid) == []


def test_a_session_without_a_bundle_workflow_is_refused() -> None:
    with pytest.raises(ScheduleRunRefused):
        target_for(SimpleNamespace(workflow_id="plain-workflow", vars={}))
    t = target_for(SimpleNamespace(workflow_id="b@2.1.0:agent", vars={"provider": "lmstudio", "model": "m", "secret": "x"}))
    assert t == {"workflow_id": "b@2.1.0:agent", "bundle_ref": "b@2.1.0", "flow_id": "agent", "input_data": {"provider": "lmstudio", "model": "m"}}


def test_a_runtime_without_the_seam_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="set_schedule_run_handler"):
        wire_schedule_run(object(), data_dir=tmp_path, root_data_dir=tmp_path, tenant="default", user="alice")
