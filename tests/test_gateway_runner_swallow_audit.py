"""Exception-swallowing audit pins (backlog 0070, M half).

The runner loop must SURVIVE durable-write failures (a broken disk must not
kill ticking), but it must never swallow them silently — the incident class is
archaeology: commands replayed after restart with no trace of why (cursor save
failed silently), or a parent stuck forever on a finished child with no log
line (wait-repair pass failed silently).

These tests pin the two paths the backlog item names: the command-cursor save
and the terminal-subworkflow wait repair. Each must (a) not propagate — the
loop continues — and (b) emit a log record with enough context to diagnose.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from abstractgateway.runner import GatewayRunner, GatewayRunnerConfig
from abstractruntime import Runtime, StepPlan, WorkflowSpec
from abstractruntime.storage.commands import CommandRecord, InMemoryCommandStore
from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore

pytestmark = pytest.mark.basic


class _BrokenCursorStore:
    """Cursor store whose save always fails (disk full / permissions class)."""

    def __init__(self) -> None:
        self.save_attempts = 0

    def load(self) -> int:
        return 0

    def save(self, cursor: int) -> None:
        self.save_attempts += 1
        raise OSError("disk full")


def _workflow() -> WorkflowSpec:
    def start(run, ctx) -> StepPlan:
        return StepPlan(node_id="start", complete_output={"ok": True})

    return WorkflowSpec(workflow_id="trivial", entry_node="start", nodes={"start": start})


class _Host:
    def __init__(self) -> None:
        self.run_store = InMemoryRunStore()
        self.ledger_store = InMemoryLedgerStore()
        self.artifact_store = None
        self.runtime = Runtime(run_store=self.run_store, ledger_store=self.ledger_store)
        self.wf = _workflow()

    def runtime_and_workflow_for_run(self, run_id: str):
        return self.runtime, self.wf


def test_cursor_save_failure_is_loud_and_does_not_stop_the_command_stream(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    host = _Host()
    command_store = InMemoryCommandStore()
    cursor_store = _BrokenCursorStore()
    runner = GatewayRunner(
        base_dir=tmp_path,
        host=host,
        config=GatewayRunnerConfig(),
        command_store=command_store,
        cursor_store=cursor_store,
    )

    # Two commands that apply cleanly (emit_event delivering to zero runs is a
    # success), so the ONLY failure in the pass is the cursor save.
    for n in (1, 2):
        command_store.append(
            CommandRecord(
                command_id=f"cmd-{n}",
                run_id=f"session-{n}",
                type="emit_event",
                payload={"name": "ping", "session_id": f"session-{n}", "payload": {}},
                ts="2026-07-21T00:00:00Z",
            )
        )

    with caplog.at_level(logging.ERROR, logger="abstractgateway.runner"):
        next_cursor = runner._poll_commands(0)

    # (a) The stream advanced past BOTH commands despite every save failing.
    assert next_cursor >= 2
    assert cursor_store.save_attempts == 2
    # (b) The failure is loud and carries context (what + consequence).
    cursor_logs = [r for r in caplog.records if "command cursor" in r.getMessage()]
    assert cursor_logs, "a failing cursor save must log, not pass silently"
    assert any("replay" in r.getMessage() for r in cursor_logs), (
        "the log must name the consequence (command replay after restart)"
    )


def test_wait_repair_failure_is_loud_and_does_not_kill_the_scheduling_pass(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = _Host()
    runner = GatewayRunner(base_dir=tmp_path, host=host, config=GatewayRunnerConfig())

    def _boom(*args, **kwargs):
        raise RuntimeError("repair pass exploded")

    monkeypatch.setattr(runner, "_repair_terminal_subworkflow_waits", _boom)

    with caplog.at_level(logging.ERROR, logger="abstractgateway.runner"):
        # Must not propagate: the scheduling pass survives a broken repair.
        runner._schedule_ticks()

    repair_logs = [r for r in caplog.records if "wait repair pass failed" in r.getMessage()]
    assert repair_logs, "a failing wait-repair pass must log, not pass silently"
    assert any("retry" in r.getMessage() for r in repair_logs), (
        "the log must say the pass retries next poll (recoverable, not fatal)"
    )
