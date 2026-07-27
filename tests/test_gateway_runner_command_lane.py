"""Command-lane isolation pins (backlog 0152, framework c4988 wedge; gateway
claim c4998).

The gateway tick loop runs a fixed-size worker pool. A tick holds its worker
for the whole effect, and a hung effect — a tool subprocess that outlives its
timeout (an un-reaped headless Chrome) or a no-progress LLM call — pins a
worker until the OS process/socket dies (a Python thread is not killable from
outside). When every general worker is pinned, an operator's resume was queued
behind the hung ticks and never ran ("accepted, not ticked"); and above
run_scan_limit RUNNING runs, a resumed run outside the window was never even
submitted.

The command lane is the gateway-side MITIGATION (not the cure — reaping child
trees + an LLM no-progress timeout is runtime/core's, c4998): command-triggered
runs get a RESERVED executor and BYPASS the scan window, so a resume/cancel
makes progress regardless of the general backlog.
"""

from __future__ import annotations

import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from abstractruntime.core.models import RunStatus

from abstractgateway.runner import GatewayRunner, GatewayRunnerConfig


@dataclass
class _Run:
    run_id: str
    actor_id: str = "gateway"
    status: Any = RunStatus.RUNNING
    waiting: Any = None
    created_at: str = "2020-01-01T00:00:00+00:00"
    vars: Dict[str, Any] = field(default_factory=dict)


class _Store:
    """Run-store double: serves a RUNNING window (respecting `limit` so the
    scan-window bypass is testable) and a load(run_id) map."""

    def __init__(self, runs: List[_Run]) -> None:
        self._by_id = {r.run_id: r for r in runs}
        self._runs = runs

    def list_runs(self, *, status=None, wait_reason=None, workflow_id=None, limit=100):
        out = [r for r in self._runs if status is None or r.status == status]
        return out[: int(limit)]

    def list_due_wait_until(self, *, now_iso=None, limit=100):
        return []

    def load(self, run_id: str) -> Optional[_Run]:
        return self._by_id.get(str(run_id))


class _Runtime:
    def __init__(self) -> None:
        self.cancelled: List[str] = []

    def cancel_run(self, run_id: str, *, reason: str = "") -> None:
        self.cancelled.append(str(run_id))


class _Host:
    def __init__(self, runtime: _Runtime, store: Any, *, tick_hook=None) -> None:
        self.runtime = runtime
        self.run_store = store
        self._tick_hook = tick_hook

    def runtime_and_workflow_for_run(self, run_id: str):
        # _tick_run calls this first; the hook records/blocks per run so the
        # end-to-end isolation test can pin a general worker without a real
        # workflow. Returns a runtime whose tick is a no-op.
        if self._tick_hook is not None:
            self._tick_hook(run_id)

        class _WF:  # minimal workflow object
            pass

        class _RT:
            def tick(self, *, workflow, run_id, max_steps):
                return None

        return _RT(), _WF()


def _runner(runs: List[_Run], *, tick_hook=None, **cfg_over) -> tuple:
    store = _Store(runs)
    rt = _Runtime()
    host = _Host(rt, store, tick_hook=tick_hook)
    base: Dict[str, Any] = {"run_scan_limit": 200, "tick_workers": 4, "command_tick_workers": 1}
    base.update(cfg_over)
    cfg = GatewayRunnerConfig(**base)
    runner = GatewayRunner(base_dir=Path(tempfile.mkdtemp()), host=host, config=cfg, enable=False)
    return runner, rt, store


def test_priority_run_bypasses_the_scan_window() -> None:
    """A resumed run OUTSIDE the run_scan_limit window is still ticked — via
    the command-lane drain — while the windowed run ticks the general lane.
    This is the "accepted, not ticked above 200 RUNNING" face."""
    # Two RUNNING runs; window of 1 sees only the FIRST. The second is only
    # reachable via the priority drain.
    a, b = _Run("run-a"), _Run("run-b")
    runner, _rt, _store = _runner([a, b], run_scan_limit=1)

    submitted: List[tuple] = []
    runner._submit_tick = lambda rid, *, priority=False: submitted.append((rid, priority))  # type: ignore

    # b is command-marked (a resume landed on it); it is outside the window.
    runner._priority_tick_ids.add("run-b")
    runner._schedule_ticks()

    by_id = dict(submitted)
    assert by_id.get("run-b") is True, "priority run must be submitted via the command lane"
    assert by_id.get("run-a") is False, "windowed run rides the general lane"
    # The priority set is drained (one-shot boost; the general scan carries it after).
    assert runner._priority_tick_ids == set()


def test_stale_priority_id_is_a_safe_noop() -> None:
    """A priority id that does not resolve to a gateway-owned run (a client
    typo, or an emit_event SESSION id that leaked in) must NOT be submitted —
    otherwise the resolution-failure promoter could false-FAIL it."""
    real = _Run("run-real")
    foreign = _Run("run-foreign", actor_id="someone-else")
    runner, _rt, _store = _runner([real, foreign], run_scan_limit=0)

    submitted: List[tuple] = []
    runner._submit_tick = lambda rid, *, priority=False: submitted.append((rid, priority))  # type: ignore

    runner._priority_tick_ids.update({"run-real", "run-foreign", "run-does-not-exist"})
    runner._schedule_ticks()

    ids = {rid for rid, _p in submitted}
    assert "run-real" in ids
    assert "run-foreign" not in ids, "a non-gateway run is never priority-ticked"
    assert "run-does-not-exist" not in ids, "a bogus id is a no-op, never a false failure"


def test_priority_tick_runs_through_a_fully_starved_general_pool() -> None:
    """THE INCIDENT: every general worker pinned by a hung tick; a resume must
    still make progress. Uses REAL executors — the command lane's own worker
    ticks the resumed run while the general pool is blocked."""
    started = threading.Event()
    release = threading.Event()
    priority_ticked = threading.Event()

    def hook(run_id: str) -> None:
        if run_id == "hung":
            started.set()
            release.wait(timeout=10)  # pin the single general worker
        elif run_id == "resumed":
            priority_ticked.set()

    # ONE general worker so a single hung tick fully starves the general pool.
    runner, _rt, store = _runner(
        [_Run("hung"), _Run("resumed")], tick_hook=hook,
        tick_workers=1, command_tick_workers=1, run_scan_limit=200,
    )
    try:
        # Pin the general worker: submit the hung run's tick directly.
        runner._submit_tick("hung")
        assert started.wait(timeout=5), "the hung tick never started"

        # The general pool is now starved. A resume lands on 'resumed'.
        runner._priority_tick_ids.add("resumed")
        runner._schedule_ticks()

        # The command lane ticks it despite the starved general pool.
        assert priority_ticked.wait(timeout=5), (
            "the resumed run was starved behind the hung general worker — command lane failed"
        )
    finally:
        release.set()
        try:
            runner._drain_inflight(timeout_s=5.0, ignore_stop=True)
        except Exception:
            pass


def test_command_lane_and_general_lane_never_double_tick() -> None:
    """The shared in-flight set is the single-tick guard across BOTH lanes: a
    run marked priority AND present in the general scan window is ticked
    exactly once (the priority drain claims the slot first)."""
    r = _Run("run-x")
    runner, _rt, _store = _runner([r], run_scan_limit=200)

    submitted: List[tuple] = []

    orig = runner._submit_tick

    def _spy(rid, *, priority=False):
        submitted.append((rid, priority))
        return orig(rid, priority=priority)

    runner._submit_tick = _spy  # type: ignore

    runner._priority_tick_ids.add("run-x")
    try:
        runner._schedule_ticks()
        # Both lanes ATTEMPTED (priority drain + general scan), but the shared
        # in-flight dedup means only the FIRST (priority) actually enqueues a
        # tick — run-x appears once as priority, and the general attempt is a
        # dedup no-op (it still calls _submit_tick, which returns early).
        priority_attempts = [p for rid, p in submitted if rid == "run-x" and p]
        assert len(priority_attempts) == 1, "priority lane should claim run-x first"
    finally:
        try:
            runner._drain_inflight(timeout_s=5.0, ignore_stop=True)
        except Exception:
            pass
