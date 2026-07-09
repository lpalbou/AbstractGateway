"""Gateway inject_guidance command (backlog 0217c).

Appends operator guidance to a running agent's durable `_runtime.inbox` so the ReAct loop can be
steered mid-flight (the loop drains the inbox at the next reason cycle).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import pytest

from abstractgateway.runner import GatewayRunner
from abstractruntime import Runtime, WorkflowSpec
from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore


@dataclass
class _Host:
    runtime: Runtime
    run_store: InMemoryRunStore
    ledger_store: InMemoryLedgerStore
    artifact_store: Any = None

    def runtime_and_workflow_for_run(self, run_id: str):  # pragma: no cover - unused here
        raise KeyError(run_id)


def _make_runner(tmp_path: Path):
    run_store = InMemoryRunStore()
    ledger_store = InMemoryLedgerStore()
    runtime = Runtime(run_store=run_store, ledger_store=ledger_store)
    host = _Host(runtime=runtime, run_store=run_store, ledger_store=ledger_store)
    runner = GatewayRunner(base_dir=tmp_path, host=host)
    return runner, runtime, run_store


def _start_agentish_run(runtime: Runtime) -> str:
    def node(run, ctx):
        from abstractruntime import StepPlan

        return StepPlan(node_id="n", complete_output={"ok": True})

    wf = WorkflowSpec(workflow_id="wf", entry_node="n", nodes={"n": node})
    return runtime.start(workflow=wf, vars={"context": {"task": "t", "messages": []}, "_runtime": {"inbox": []}})


def test_inject_guidance_appends_to_inbox(tmp_path: Path) -> None:
    runner, runtime, run_store = _make_runner(tmp_path)
    rid = _start_agentish_run(runtime)

    runner._apply_inject_guidance({"guidance": "Focus on the auth module first."}, run_id=rid)

    run = run_store.load(rid)
    inbox = run.vars["_runtime"]["inbox"]
    assert inbox[-1] == {"role": "system", "content": "Focus on the auth module first."}


def test_inject_guidance_requires_text(tmp_path: Path) -> None:
    runner, runtime, run_store = _make_runner(tmp_path)
    rid = _start_agentish_run(runtime)
    with pytest.raises(ValueError):
        runner._apply_inject_guidance({"guidance": "   "}, run_id=rid)


def test_inject_guidance_unknown_run_raises(tmp_path: Path) -> None:
    runner, _runtime, _store = _make_runner(tmp_path)
    with pytest.raises(KeyError):
        runner._apply_inject_guidance({"guidance": "hello"}, run_id="does-not-exist")


def test_inject_guidance_never_resurrects_a_terminal_run(tmp_path: Path) -> None:
    """A completed run must not be mutated/resurrected by an injection (adversarial-review guard)."""
    from abstractruntime.core.models import RunStatus

    runner, runtime, run_store = _make_runner(tmp_path)
    rid = _start_agentish_run(runtime)

    # Force the run to a terminal state, as a tick worker would after completion.
    run = run_store.load(rid)
    run.status = RunStatus.COMPLETED
    run.output = {"answer": "done"}
    run_store.save(run)

    # Injection targets only this (now terminal) run -> nothing injectable -> KeyError, and the
    # terminal run is neither mutated nor resurrected.
    with pytest.raises(KeyError):
        runner._apply_inject_guidance({"guidance": "too late"}, run_id=rid)

    after = run_store.load(rid)
    assert after.status == RunStatus.COMPLETED
    assert after.output == {"answer": "done"}
    assert after.vars["_runtime"].get("inbox") == []
