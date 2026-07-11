"""GatewayRunner silent-swallow hardening: workflow-resolution failures.

`_tick_run` used to catch `runtime_and_workflow_for_run` exceptions at DEBUG
and return — a run whose workflow cannot be resolved (deleted draft,
tombstoned catalog version, principal-scoped bundle not loaded) stayed RUNNING
forever with zero ledger and no error: the exact stuck-on-"On Flow Start"
incident symptom, reachable even with a healthy runner lock.

Hardened behavior:
- consecutive resolution failures are counted per run;
- after `workflow_resolution_failure_limit` consecutive failures a RUNNING run
  is promoted to FAILED with a `system:workflow_resolution:<run_id>` ledger
  record (parity with the tick-exception path);
- a successful resolution resets the counter;
- non-RUNNING runs are never promoted (parity with the tick-exception guard);
- one unresolvable parent must not abort `_resume_subworkflow_parents` for
  other parents waiting on the same child.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

from abstractgateway.runner import GatewayRunner, GatewayRunnerConfig
from abstractruntime import Effect, EffectType, Runtime, StepPlan, WorkflowSpec
from abstractruntime.core.models import RunStatus, WaitReason, WaitState
from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore


def _trivial_workflow() -> WorkflowSpec:
    def start(run, ctx) -> StepPlan:
        return StepPlan(node_id="start", complete_output={"ok": True})

    return WorkflowSpec(workflow_id="trivial", entry_node="start", nodes={"start": start})


@dataclass
class _FlakyHost:
    """Host whose workflow resolution fails for run_ids listed in `broken`.

    `fail_counts` records how often resolution was attempted per run.
    """

    runtime: Runtime
    wf: WorkflowSpec
    run_store: InMemoryRunStore
    ledger_store: InMemoryLedgerStore
    broken: set[str] = field(default_factory=set)
    fail_counts: Dict[str, int] = field(default_factory=dict)
    artifact_store: Any = None

    def runtime_and_workflow_for_run(self, run_id: str) -> tuple[Runtime, WorkflowSpec]:
        if run_id in self.broken:
            self.fail_counts[run_id] = self.fail_counts.get(run_id, 0) + 1
            raise KeyError(f"Workflow for run '{run_id}' is not registered")
        return self.runtime, self.wf


def _make(tmp_path: Path, *, failure_limit: int = 3):
    run_store = InMemoryRunStore()
    ledger_store = InMemoryLedgerStore()
    runtime = Runtime(run_store=run_store, ledger_store=ledger_store)
    wf = _trivial_workflow()
    host = _FlakyHost(runtime=runtime, wf=wf, run_store=run_store, ledger_store=ledger_store)
    runner = GatewayRunner(
        base_dir=tmp_path,
        host=host,
        config=GatewayRunnerConfig(workflow_resolution_failure_limit=failure_limit),
    )
    return run_store, ledger_store, runtime, wf, host, runner


def test_unresolvable_running_run_fails_after_limit(tmp_path: Path) -> None:
    run_store, ledger_store, runtime, wf, host, runner = _make(tmp_path, failure_limit=3)

    run_id = runtime.start(workflow=wf, actor_id="gateway")
    assert run_store.load(run_id).status == RunStatus.RUNNING
    host.broken.add(run_id)

    # Below the limit: the run stays RUNNING (transient failures tolerated).
    runner._tick_run(run_id)
    runner._tick_run(run_id)
    assert run_store.load(run_id).status == RunStatus.RUNNING
    assert ledger_store.list(run_id) == []

    # At the limit: promoted to FAILED with a ledger record.
    runner._tick_run(run_id)
    state = run_store.load(run_id)
    assert state.status == RunStatus.FAILED
    assert "WorkflowResolutionError" in str(state.error)
    records = ledger_store.list(run_id)
    assert len(records) == 1
    rec = records[0]
    assert rec.get("idempotency_key") == f"system:workflow_resolution:{run_id}"
    assert str(getattr(rec.get("status"), "value", rec.get("status"))) == "failed"
    assert "WorkflowResolutionError" in str(rec.get("error"))

    # Counter cleared after promotion: no repeated promotion attempts.
    assert runner._resolution_failures == {}


def test_successful_resolution_resets_failure_counter(tmp_path: Path) -> None:
    run_store, ledger_store, runtime, wf, host, runner = _make(tmp_path, failure_limit=3)

    run_id = runtime.start(workflow=wf, actor_id="gateway")
    host.broken.add(run_id)

    runner._tick_run(run_id)
    runner._tick_run(run_id)
    assert runner._resolution_failures.get(run_id) == 2

    # Resolution recovers (e.g. catalog finished loading): counter resets and
    # the run ticks to completion instead of being failed.
    host.broken.discard(run_id)
    runner._tick_run(run_id)
    assert runner._resolution_failures.get(run_id) is None
    assert run_store.load(run_id).status == RunStatus.COMPLETED

    # A later new failure streak starts from zero (needs the full limit again).
    other = runtime.start(workflow=wf, actor_id="gateway")
    host.broken.add(other)
    runner._tick_run(other)
    runner._tick_run(other)
    assert run_store.load(other).status == RunStatus.RUNNING


def test_non_running_run_is_never_promoted(tmp_path: Path) -> None:
    """Parity with the tick-exception guard: only RUNNING runs flip to FAILED."""
    run_store, ledger_store, runtime, wf, host, runner = _make(tmp_path, failure_limit=2)

    def park(run, ctx) -> StepPlan:
        return StepPlan(
            node_id="park",
            effect=Effect(type=EffectType.WAIT_EVENT, payload={"wait_key": "evt:test"}, result_key="_temp.evt"),
            next_node="park",
        )

    waiting_wf = WorkflowSpec(workflow_id="parker", entry_node="park", nodes={"park": park})
    run_id = runtime.start(workflow=waiting_wf, actor_id="gateway")
    state = runtime.tick(workflow=waiting_wf, run_id=run_id)
    assert state.status == RunStatus.WAITING

    host.broken.add(run_id)
    for _ in range(4):  # well past the limit
        runner._tick_run(run_id)

    latest = run_store.load(run_id)
    assert latest.status == RunStatus.WAITING
    assert latest.error is None
    # Counter reset instead of unbounded growth/repeated promotion attempts.
    assert runner._resolution_failures.get(run_id) is None


def test_one_unresolvable_parent_does_not_block_other_parents(tmp_path: Path) -> None:
    """`_resume_subworkflow_parents` must continue past a broken parent."""
    run_store, ledger_store, runtime, wf, host, runner = _make(tmp_path)

    child_id = "child-run-1"

    def park(run, ctx) -> StepPlan:
        # Reaching this node after resume means the wait was satisfied.
        return StepPlan(node_id="park", complete_output={"resumed": True})

    parent_wf = WorkflowSpec(workflow_id="parent", entry_node="park", nodes={"park": park})

    def _make_waiting_parent() -> str:
        rid = runtime.start(workflow=parent_wf, actor_id="gateway")
        run = run_store.load(rid)
        run.status = RunStatus.WAITING
        run.waiting = WaitState(
            reason=WaitReason.SUBWORKFLOW,
            wait_key=f"subworkflow:{child_id}",
            resume_to_node="park",
            result_key="_temp.sub",
            details={"sub_run_id": child_id},
        )
        run_store.save(run)
        return rid

    broken_parent = _make_waiting_parent()
    healthy_parent = _make_waiting_parent()

    host.wf = parent_wf
    host.broken.add(broken_parent)

    # In-memory list_runs preserves insertion order, so the broken parent is
    # visited first — before the fix its resolution error aborted the whole
    # loop and the healthy parent stayed WAITING forever (caller swallows).
    runner._resume_subworkflow_parents(child_run_id=child_id, child_output={"success": True})

    assert run_store.load(broken_parent).status == RunStatus.WAITING  # skipped, not crashed
    assert run_store.load(healthy_parent).status != RunStatus.WAITING  # resumed
