"""The runner's two parent-resume paths treat a lost resume race as benign.

When a child finishes, the tick thread (`_resume_subworkflow_parents`) and the
loop's repair pass (`_repair_terminal_subworkflow_waits`) may both try to
resume the same parent. AbstractRuntime resumes a wait at most once and
refuses the slower caller with `ValueError("Run is not waiting")`
(2026-09-26 incident: before that, both got through and the parent's Agent
node ran its child twice). The loser must log at DEBUG, raise nothing and go
on with the other parents; every other failure keeps being surfaced.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

import pytest

from abstractgateway.runner import GatewayRunner, GatewayRunnerConfig
from abstractruntime import Runtime, StepPlan, WorkflowSpec
from abstractruntime.core.models import RunState, RunStatus, WaitReason, WaitState
from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore

pytestmark = pytest.mark.basic

CHILD = "child-run"


def _workflow() -> WorkflowSpec:
    def wait(run, ctx) -> StepPlan:  # never executed here (max_steps=0)
        return StepPlan(node_id="wait", next_node="done")

    def done(run, ctx) -> StepPlan:
        return StepPlan(node_id="done", complete_output={"ok": True})

    return WorkflowSpec(workflow_id="parent", entry_node="wait", nodes={"wait": wait, "done": done})


class _FailingRuntime(Runtime):
    """Raises a non-race error when resuming one chosen run."""

    fail_run_id: str = ""

    def resume(self, *, workflow, run_id, wait_key, payload, max_steps=100):  # type: ignore[override]
        if run_id == self.fail_run_id:
            raise RuntimeError("store exploded")
        return super().resume(workflow=workflow, run_id=run_id, wait_key=wait_key, payload=payload, max_steps=max_steps)


class _Host:
    def __init__(self) -> None:
        self.run_store = InMemoryRunStore()
        self.ledger_store = InMemoryLedgerStore()
        self.artifact_store = None
        self.runtime = _FailingRuntime(run_store=self.run_store, ledger_store=self.ledger_store)
        self.wf = _workflow()

    def runtime_and_workflow_for_run(self, run_id: str):
        return self.runtime, self.wf


def _waiting_parent(run_id: str) -> RunState:
    return RunState(
        run_id=run_id,
        workflow_id="parent",
        status=RunStatus.WAITING,
        current_node="wait",
        vars={"_temp": {}, "_limits": {}, "_runtime": {}},
        waiting=WaitState(
            reason=WaitReason.SUBWORKFLOW,
            wait_key=f"subworkflow:{CHILD}",
            resume_to_node="done",
            result_key="_temp.sub",
            details={"sub_run_id": CHILD},
        ),
        actor_id="gateway",
    )


def _setup(tmp_path: Path) -> tuple[_Host, GatewayRunner, list[RunState]]:
    """Parent `p-won` was already resumed by the other path; `p-next` still waits.

    Returns the stale snapshots a path works from (both look WAITING)."""
    host = _Host()
    host.run_store.save(
        RunState(run_id=CHILD, workflow_id="child", status=RunStatus.COMPLETED, current_node="end",
                 output={"answer": "ok"}, actor_id="gateway", parent_run_id="p-won")
    )
    won = _waiting_parent("p-won")
    stale = [copy.deepcopy(won), _waiting_parent("p-next")]
    won.status, won.waiting, won.current_node = RunStatus.RUNNING, None, "done"
    host.run_store.save(won)
    host.run_store.save(stale[1])
    runner = GatewayRunner(base_dir=tmp_path, host=host, config=GatewayRunnerConfig())
    return host, runner, stale


def _lost_race_logs(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "p-won" in r.getMessage() and "already resumed" in r.getMessage()]


def _assert_lost_race_handled(host: _Host, caplog: pytest.LogCaptureFixture) -> None:
    logs = _lost_race_logs(caplog)
    assert len(logs) == 1
    assert logs[0].levelno == logging.DEBUG
    assert f"subworkflow:{CHILD}" in logs[0].getMessage()
    assert logs[0].exc_info is None
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    # The next parent in the list was still resumed.
    nxt = host.run_store.load("p-next")
    assert nxt.status == RunStatus.RUNNING and nxt.waiting is None


def test_tick_path_parent_resume_lost_race_is_debug_and_continues(tmp_path, caplog, monkeypatch) -> None:
    host, runner, stale = _setup(tmp_path)
    monkeypatch.setattr(runner, "_waiting_parents_for_child", lambda child_run_id: stale)

    with caplog.at_level(logging.DEBUG, logger="abstractgateway.runner"):
        runner._resume_subworkflow_parents(child_run_id=CHILD, child_output={"answer": "ok"})

    _assert_lost_race_handled(host, caplog)


def test_tick_path_parent_resume_other_errors_still_raise(tmp_path, monkeypatch) -> None:
    host, runner, stale = _setup(tmp_path)
    host.runtime.fail_run_id = "p-next"
    monkeypatch.setattr(runner, "_waiting_parents_for_child", lambda child_run_id: stale[1:])

    with pytest.raises(RuntimeError, match="store exploded"):
        runner._resume_subworkflow_parents(child_run_id=CHILD, child_output={"answer": "ok"})


def test_repair_pass_lost_race_is_debug_and_continues(tmp_path, caplog) -> None:
    host, runner, stale = _setup(tmp_path)

    with caplog.at_level(logging.DEBUG, logger="abstractgateway.runner"):
        runner._repair_terminal_subworkflow_waits(waiting=stale)

    _assert_lost_race_handled(host, caplog)


def test_repair_pass_other_errors_warn_with_traceback_and_continue(tmp_path, caplog) -> None:
    host, runner, stale = _setup(tmp_path)
    # p-won is genuinely waiting this time, and its resume fails for real.
    host.run_store.save(stale[0])
    host.runtime.fail_run_id = "p-won"

    with caplog.at_level(logging.DEBUG, logger="abstractgateway.runner"):
        runner._repair_terminal_subworkflow_waits(waiting=stale)

    warns = [r for r in caplog.records if r.levelno == logging.WARNING and "p-won" in r.getMessage()]
    assert len(warns) == 1
    assert warns[0].exc_info is not None
    assert not _lost_race_logs(caplog)
    nxt = host.run_store.load("p-next")
    assert nxt.status == RunStatus.RUNNING and nxt.waiting is None
