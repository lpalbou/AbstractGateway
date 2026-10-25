"""Revising an automation onto a trigger connection through the gateway runner (seam from
the trigger-connections lane): the runner passes the automation's OWNER (`_meta.automation_owner`)
as `principal=`, so a schedule automation revised onto its owner's feed connection is applied, and
onto another person's connection is refused."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from abstractgateway.runner import GatewayRunner
from abstractruntime import Runtime, StepPlan, WorkflowSpec
from abstractruntime.automations import create_automation, register_controller_bundle
from abstractruntime.automations.ledger import definition_of
from abstractruntime.email.inbox import InMemoryEventInbox
from abstractruntime.scheduler.registry import WorkflowRegistry
from abstractruntime.storage.artifacts import FileArtifactStore
from abstractruntime.storage.json_files import JsonFileRunStore, JsonlLedgerStore
from abstractruntime.triggers import TriggerConnectionStore, connection_principal
from abstractruntime.automations.ledger import automation_records

ALICE = connection_principal("default", "alice")
BOB = connection_principal("default", "bob")


class _Host:
    def __init__(self, runtime: Runtime, registry: WorkflowRegistry) -> None:
        self.runtime, self.registry = runtime, registry
        self.run_store, self.ledger_store, self.artifact_store = runtime.run_store, runtime.ledger_store, runtime.artifact_store

    def runtime_and_workflow_for_run(self, run_id: str):
        run = self.run_store.load(str(run_id))
        return self.runtime, self.registry.get(str(run.workflow_id))


def _env(tmp_path: Path):
    registry = WorkflowRegistry()
    registry.register(WorkflowSpec(workflow_id="fixtures@1.0.0:echo", entry_node="a",
                                   nodes={"a": lambda run, ctx: StepPlan(node_id="a", complete_output={"response": "ok"})}))
    register_controller_bundle(registry)
    data = tmp_path / "data"
    runtime = Runtime(run_store=JsonFileRunStore(data), ledger_store=JsonlLedgerStore(data),
                      workflow_registry=registry, artifact_store=FileArtifactStore(data))
    runtime.set_event_inbox(InMemoryEventInbox())
    store = TriggerConnectionStore(tmp_path / "plane", sealing_key_file=tmp_path / "secrets" / "sealing.key")
    runtime.set_trigger_connection_store(store)
    runner = GatewayRunner(base_dir=data, host=_Host(runtime, registry), enable=False)
    automation_id, _ = create_automation(runtime, {
        "request_id": "sched-1", "tenant": "default", "user": "alice", "title": "Hourly",
        "target": {"workflow_id": "fixtures@1.0.0:echo", "bundle_ref": "fixtures@1.0.0", "flow_id": "echo", "input_data": {"prompt": "hi"}},
        "trigger": {"source_id": "schedule", "source_version": 1, "config": {"every": "1h"}},
        "workspace_root": str(tmp_path / "ws"),
    }, actor_id="gateway")
    return runtime, runner, store, automation_id


def _feed(store, principal):
    return store.create(principal=principal, source_id="feed.item", source_version=1,
                        config={"url": "http://127.0.0.1:9/feed.xml"}, label="feed")


def _revise(runner, automation_id, connection_id, command_id):
    runner._apply_automation_command(
        SimpleNamespace(command_id=command_id, client_id="test"), typ="automation.revise", automation_id=automation_id,
        payload={"changes": {"trigger": {"source_id": "feed.item", "source_version": 1, "config": {"connection_id": connection_id}}}},
    )


def _result(runtime, aid, command_id):
    for rec in automation_records(runtime.ledger_store, aid, "automation.command_result"):
        if rec["payload"].get("command_id") == command_id:
            return rec["payload"]
    raise AssertionError(f"no command result for {command_id}")


def test_revise_onto_the_owners_feed_connection_is_applied(tmp_path: Path) -> None:
    runtime, runner, store, aid = _env(tmp_path)
    assert runner._automation_owner(aid) == ALICE
    mine = _feed(store, ALICE)
    _revise(runner, aid, mine.connection_id, "rev-1")
    assert _result(runtime, aid, "rev-1")["status"] == "applied", _result(runtime, aid, "rev-1")
    trigger = definition_of(runtime.run_store.load(aid))["trigger"]
    assert trigger["source_id"] == "feed.item" and trigger["config"]["connection_id"] == mine.connection_id


def test_revise_onto_another_persons_connection_is_refused(tmp_path: Path) -> None:
    runtime, runner, store, aid = _env(tmp_path)
    bobs = _feed(store, BOB)
    _revise(runner, aid, bobs.connection_id, "rev-2")
    out = _result(runtime, aid, "rev-2")
    assert out["status"] == "rejected" and out["error"]["reason_code"] == "connection_not_found", out
    trigger = definition_of(runtime.run_store.load(aid))["trigger"]
    assert trigger["source_id"] == "schedule"  # unchanged: the command was rejected
