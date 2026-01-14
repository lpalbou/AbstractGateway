from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

from abstractgateway.runner import GatewayRunner
from abstractruntime import Effect, EffectType, Runtime, StepPlan, WorkflowSpec
from abstractruntime.core.models import RunStatus, WaitReason
from abstractruntime.integrations.abstractcore.effect_handlers import build_effect_handlers
from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore


class _StubLLM:
    def generate(self, **kwargs: Any) -> Dict[str, Any]:
        return {"content": "ok", "tool_calls": []}


class _StubTools:
    def execute(self, *, tool_calls: Any) -> Dict[str, Any]:
        return {"mode": "executed", "results": []}


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


def test_gateway_runner_resumes_subworkflow_parent_with_node_traces(tmp_path: Path) -> None:
    run_store = InMemoryRunStore()
    ledger_store = InMemoryLedgerStore()

    runtime = Runtime(
        run_store=run_store,
        ledger_store=ledger_store,
        effect_handlers=build_effect_handlers(llm=_StubLLM(), tools=_StubTools()),
    )

    def child_node(run, ctx) -> StepPlan:
        return StepPlan(
            node_id="child_node",
            effect=Effect(type=EffectType.LLM_CALL, payload={"prompt": "hi"}, result_key="_temp.child"),
            next_node=None,
        )

    child = WorkflowSpec(workflow_id="child", entry_node="child_node", nodes={"child_node": child_node})

    def start(run, ctx) -> StepPlan:
        return StepPlan(
            node_id="start",
            effect=Effect(
                type=EffectType.START_SUBWORKFLOW,
                payload={
                    "workflow_id": "child",
                    "vars": {},
                    "async": True,
                    "wait": True,
                    "include_traces": True,
                },
                result_key="sub",
            ),
            next_node="done",
        )

    def done(run, ctx) -> StepPlan:
        return StepPlan(node_id="done", complete_output={"sub": run.vars.get("sub")})

    parent = WorkflowSpec(workflow_id="parent", entry_node="start", nodes={"start": start, "done": done})

    registry: Dict[str, WorkflowSpec] = {"parent": parent, "child": child}
    runtime.set_workflow_registry(registry)

    parent_run_id = runtime.start(workflow=parent, vars={})
    parent_state = runtime.tick(workflow=parent, run_id=parent_run_id, max_steps=5)

    assert parent_state.status == RunStatus.WAITING
    assert parent_state.waiting is not None
    assert parent_state.waiting.reason == WaitReason.SUBWORKFLOW
    assert parent_state.waiting.details.get("include_traces") is True

    child_run_id = parent_state.waiting.details.get("sub_run_id")
    assert isinstance(child_run_id, str) and child_run_id

    host = _Host(runtime=runtime, registry=registry, run_store=run_store, ledger_store=ledger_store)
    runner = GatewayRunner(base_dir=tmp_path, host=host)

    # Drive the child to completion and ensure the parent is resumed with traces.
    runner._tick_run(child_run_id)

    resumed_parent = runtime.get_state(parent_run_id)
    assert resumed_parent.status == RunStatus.RUNNING

    sub = resumed_parent.vars.get("sub")
    assert isinstance(sub, dict)
    assert sub.get("sub_run_id") == child_run_id

    traces = sub.get("node_traces")
    assert isinstance(traces, dict)
    assert traces.get("child_node", {}).get("steps"), "expected child node traces to be propagated"

