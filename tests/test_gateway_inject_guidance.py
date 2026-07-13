"""Gateway inject_guidance command (backlog 0217c; H4 steer door 2026-07-12).

The gateway queues operator guidance through the DURABLE steer sidecar
(`Runtime.steer()`); the run's own tick drains it into `_runtime.inbox` at
the next iteration boundary and acks with an `abstract.steer_seen` ledger
record. The tick thread stays the only writer of run state.
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
    from abstractgateway.steering import gateway_steer_sidecar

    run_store = InMemoryRunStore()
    ledger_store = InMemoryLedgerStore()
    # The tick runtime carries the same per-root sidecar production attaches
    # (bundle_host.attach_steer_store) so drains fire at tick boundaries.
    runtime = Runtime(
        run_store=run_store,
        ledger_store=ledger_store,
        steer_store=gateway_steer_sidecar(tmp_path),
    )
    host = _Host(runtime=runtime, run_store=run_store, ledger_store=ledger_store)
    runner = GatewayRunner(base_dir=tmp_path, host=host)
    return runner, runtime, run_store


def _agentish_workflow() -> WorkflowSpec:
    def node(run, ctx):
        from abstractruntime import StepPlan

        return StepPlan(node_id="n", complete_output={"ok": True})

    return WorkflowSpec(workflow_id="wf", entry_node="n", nodes={"n": node})


def _start_agentish_run(runtime: Runtime, wf: WorkflowSpec | None = None) -> str:
    wf = wf or _agentish_workflow()
    return runtime.start(workflow=wf, vars={"context": {"task": "t", "messages": []}, "_runtime": {"inbox": []}})


def test_inject_guidance_reaches_inbox_at_the_next_tick_boundary(tmp_path: Path) -> None:
    """H4 end-to-end: command → sidecar → tick drain → _runtime.inbox +
    abstract.steer_seen ledger ack. The gateway never writes run vars."""
    runner, runtime, run_store = _make_runner(tmp_path)
    wf = _agentish_workflow()
    rid = _start_agentish_run(runtime, wf)

    runner._apply_inject_guidance({"guidance": "Focus on the auth module first."}, run_id=rid)

    # The steer rests in the SIDECAR, not in run vars (single-tick-writer).
    before_tick = run_store.load(rid)
    assert before_tick.vars["_runtime"]["inbox"] == []

    runtime.tick(workflow=wf, run_id=rid)

    run = run_store.load(rid)
    inbox = run.vars["_runtime"]["inbox"]
    assert inbox[-1] == {"role": "system", "content": "Focus on the auth module first."}
    # The delivery ack is observable in the ledger (records serialize as dicts).
    records = runner.ledger_store.list(rid)

    def _result(r: Any) -> Dict[str, Any]:
        raw = r.get("result") if isinstance(r, dict) else getattr(r, "result", None)
        return raw if isinstance(raw, dict) else {}

    assert any(_result(r).get("steer_seen") for r in records), "abstract.steer_seen ack missing from the ledger"


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
