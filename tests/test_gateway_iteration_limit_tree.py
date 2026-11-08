"""One iteration budget governs the whole run tree (operator 2026-11-08: "it doesn't look like the
agent is following the max iterations I request ... By default, set it to 50 everywhere").

The shipped basic-agent does its loop in a CHILD run (`visual_react_agent_..._node-2`). Its
On Flow Start node carries a `max_iterations` pin with the authored default 20, wired into the
Agent node: a run that set only `_limits.max_iterations` (the field the gateway default and the
kit write) used to get the pin default 20 in the child instead. The real react loop (abstractagent)
runs here against a scripted model that always asks for another tool step, so the number of
cycles the child performs is the budget it actually obeyed."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from abstractagent.adapters.react_runtime import create_react_workflow
from abstractagent.logic.react import ReActLogic
from abstractcore.tools import ToolDefinition
from abstractruntime import DEFAULT_MAX_ITERATIONS
from abstractruntime.core.models import Effect, EffectType, RunState, RunStatus
from abstractruntime.core.runtime import EffectOutcome, Runtime
from abstractruntime.core.spec import WorkflowSpec
from abstractruntime.scheduler.registry import WorkflowRegistry
from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore
from abstractruntime.visualflow_compiler.compiler import compile_visualflow
from abstractruntime.visualflow_compiler.visual.agent_ids import visual_react_workflow_id

pytestmark = pytest.mark.basic

BUNDLE = Path(__file__).resolve().parents[1] / "flows" / "bundles" / "basic-agent.flow"
ROOT_FLOW = "81795ea9"


_STEP = [0]


def _always_another_step(run: RunState, effect: Effect, default_next_node: Optional[str]) -> EffectOutcome:
    del run, default_next_node
    payload = effect.payload if isinstance(effect.payload, dict) else {}
    if "tools" in payload:  # a normal cycle: ask for one more (new) tool step, forever
        _STEP[0] += 1
        n = _STEP[0]
        return EffectOutcome.completed(
            {"content": "", "finish_reason": "tool_calls",
             "tool_calls": [{"name": "list_files", "arguments": {"directory_path": f"d{n}"}, "call_id": f"c{n}"}]}
        )
    return EffectOutcome.completed({"content": "FINAL", "tool_calls": [], "finish_reason": "stop"})


def _tool_ok(run: RunState, effect: Effect, default_next_node: Optional[str]) -> EffectOutcome:
    del run, default_next_node
    calls = (effect.payload or {}).get("tool_calls") or []
    return EffectOutcome.completed(
        {"mode": "executed",
         "results": [{"call_id": c.get("call_id"), "name": c.get("name"), "success": True, "output": "ok", "error": None}
                     for c in calls]}
    )


def _child_cycles(input_vars: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
    with zipfile.ZipFile(BUNDLE) as z:
        flows = {json.loads(z.read(n))["id"]: json.loads(z.read(n)) for n in z.namelist() if n.startswith("flows/")}
    reg = WorkflowRegistry()
    for raw in flows.values():
        reg.register(compile_visualflow(raw))
    react_id = visual_react_workflow_id(flow_id=ROOT_FLOW, node_id="node-2")
    reg.register(create_react_workflow(
        logic=ReActLogic(tools=[ToolDefinition(name="list_files", description="List", parameters={})]),
        workflow_id=react_id, provider=None, model=None, allowed_tools=["list_files"],
    ))
    rt = Runtime(run_store=InMemoryRunStore(), ledger_store=InMemoryLedgerStore(), workflow_registry=reg,
                 effect_handlers={EffectType.LLM_CALL: _always_another_step, EffectType.TOOL_CALLS: _tool_ok})

    def drive(workflow: WorkflowSpec, run_id: str) -> RunState:
        for _ in range(2000):
            st = rt.tick(workflow=workflow, run_id=run_id, max_steps=1)
            if st.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
                return st
            w = st.waiting
            if st.status == RunStatus.WAITING and w is not None and str(w.wait_key or "").startswith("subworkflow:"):
                child_id = str(w.wait_key).split(":", 1)[1]
                child = rt.run_store.load(child_id)
                child_state = drive(reg.get(child.workflow_id), child_id)
                if child.workflow_id == react_id:
                    seen["child"] = child_state
                rt.resume(workflow=workflow, run_id=run_id, wait_key=w.wait_key,
                          payload={"sub_run_id": child_id, "output": child_state.output or {}}, max_steps=0)
        raise AssertionError("run did not finish")

    seen: Dict[str, RunState] = {}
    root = reg.get(ROOT_FLOW)
    drive(root, rt.start(workflow=root, vars={"prompt": "go", "provider": "stub", "model": "stub", **input_vars}))
    child = seen["child"]
    assert child.status == RunStatus.COMPLETED, child.error
    assert (child.output or {}).get("stop_reason", {}).get("budget_exhausted") is True, (child.output or {}).get("stop_reason")
    return len(child.vars["scratchpad"]["cycles"]), child.vars["_limits"]


@pytest.mark.parametrize("requested", [50, 10])
def test_the_requested_limit_is_the_one_the_react_child_obeys(requested: int) -> None:
    cycles, limits = _child_cycles({"_limits": {"max_iterations": requested}})
    assert limits["max_iterations"] == requested
    assert cycles == requested, f"asked for {requested}, the child ran {cycles} cycles (the start-pin default won)"


def test_the_flat_pin_older_clients_send_still_counts() -> None:
    cycles, limits = _child_cycles({"max_iterations": 7})
    assert (cycles, limits["max_iterations"]) == (7, 7)


def test_no_request_gets_the_framework_default_50() -> None:
    assert DEFAULT_MAX_ITERATIONS == 50
    cycles, limits = _child_cycles({})
    assert (cycles, limits["max_iterations"]) == (50, 50)
