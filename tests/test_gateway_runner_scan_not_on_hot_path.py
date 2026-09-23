"""A finished tick must not put a whole-store scan in front of the next hop.

Mission B made the runner event-driven; mission B2 measured what was left at
the operator's store size (9,326 run files): 0.83s of a 1.5s no-tool chat turn
was scheduling latency, and its shape was the SCAN PASS. Two causes, both
fixed here and pinned below:

1. `_submit_tick`'s done-callback forced a full pass after EVERY tick
   (`_scan_force = True`) — four whole-directory walks, ~0.18s at operator
   scale — when the only run that pass had to find was the one that had just
   ticked. It now re-queues THAT run for a direct tick instead. The one case
   that still needs the forced pass is an UNRESOLVABLE workflow: that tick
   writes nothing, so neither the ledger seam nor the store fingerprint would
   ever wake it again, and it must keep retrying to reach the FAILED
   promotion at workflow_resolution_failure_limit.

2. `_schedule_ticks` runs on the LOOP thread, the same thread that serves the
   direct-tick queue, so an in-process event landing mid-pass waited for the
   whole pass. It now drains between its walks.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, List

import pytest

pytestmark = pytest.mark.basic

from abstractruntime.core.models import RunState, RunStatus  # noqa: E402
from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore  # noqa: E402
from abstractruntime.storage.observable import ObservableLedgerStore  # noqa: E402

from abstractgateway.runner import GatewayRunner, GatewayRunnerConfig  # noqa: E402


def _run(run_id: str, *, status: RunStatus = RunStatus.RUNNING, actor_id: str = "gateway") -> RunState:
    return RunState(
        run_id=run_id,
        workflow_id="wf",
        status=status,
        current_node="n",
        vars={},
        actor_id=actor_id,
    )


class _Host:
    def __init__(self, run_store: Any, ledger_store: Any) -> None:
        self._rs = run_store
        self._ls = ledger_store

    @property
    def run_store(self) -> Any:
        return self._rs

    @property
    def ledger_store(self) -> Any:
        return self._ls

    @property
    def artifact_store(self) -> Any:  # pragma: no cover - unused here
        return None

    def runtime_and_workflow_for_run(self, run_id: str):  # pragma: no cover
        raise KeyError(run_id)


def _runner(tmp_path: Path, *, runs: List[RunState] | None = None) -> GatewayRunner:
    run_store = InMemoryRunStore()
    for r in runs or []:
        run_store.save(r)
    ledger = ObservableLedgerStore(InMemoryLedgerStore())
    return GatewayRunner(
        base_dir=tmp_path,
        host=_Host(run_store, ledger),
        config=GatewayRunnerConfig(poll_interval_s=2.0),
        enable=True,
    )


def _done_callback_of(runner: GatewayRunner, run_id: str):
    """Drive `_submit_tick`'s real done-callback without running a tick."""
    captured: dict = {}

    class _Executor:
        def submit(self, _fn, _rid):
            class _Fut:
                def add_done_callback(self, cb):
                    captured["cb"] = cb

            return _Fut()

    runner._executor = _Executor()  # type: ignore[assignment]
    runner._submit_tick(run_id)
    assert "cb" in captured, "_submit_tick did not register a done callback"
    return captured["cb"]


def test_a_finished_tick_requeues_its_own_run_instead_of_forcing_a_scan(tmp_path: Path) -> None:
    runner = _runner(tmp_path, runs=[_run("r1")])
    runner._scan_force = False
    cb = _done_callback_of(runner, "r1")

    cb(None)

    with runner._inflight_lock:
        queued = set(runner._direct_tick_ids)
    assert queued == {"r1"}, "the just-ticked run must be re-queued directly"
    assert runner._scan_force is False, "a normal tick must not force a whole-store pass"
    assert runner._wake.is_set(), "the loop must still be woken"


def test_an_unresolvable_workflow_keeps_the_forced_pass_and_no_tight_requeue(tmp_path: Path) -> None:
    """Its tick writes nothing: no ledger record, no run file change. Only the
    forced pass can retry it, and a direct re-queue would turn a poll-cadence
    retry into a spin that burns workflow_resolution_failure_limit in
    milliseconds."""
    runner = _runner(tmp_path, runs=[_run("r2")])
    runner._scan_force = False
    with runner._resolution_failures_lock:
        runner._resolution_failures["r2"] = 3
    cb = _done_callback_of(runner, "r2")

    cb(None)

    assert runner._scan_force is True, "an unresolvable run still forces the retry pass"
    with runner._inflight_lock:
        queued = set(runner._direct_tick_ids)
    assert queued == set(), "an unresolvable run must NOT be re-queued directly (spin guard)"


def test_the_requeued_run_is_still_load_verified_and_running_gated(tmp_path: Path) -> None:
    """The re-queue is safe precisely because the drain re-checks the run: a
    run that FINISHED in its tick is a no-op, not a second tick."""
    submitted: List[str] = []
    runner = _runner(tmp_path, runs=[_run("done-run", status=RunStatus.COMPLETED), _run("live-run")])
    runner._submit_tick = lambda rid, priority=False: submitted.append(rid)  # type: ignore[assignment]

    runner._queue_direct_tick("done-run")
    runner._queue_direct_tick("live-run")
    runner._drain_direct_ticks()

    assert submitted == ["live-run"]


def test_a_foreign_run_is_never_requeued_into_a_tick(tmp_path: Path) -> None:
    submitted: List[str] = []
    runner = _runner(tmp_path, runs=[_run("theirs", actor_id="someone-else")])
    runner._submit_tick = lambda rid, priority=False: submitted.append(rid)  # type: ignore[assignment]

    runner._queue_direct_tick("theirs")
    runner._drain_direct_ticks()

    assert submitted == []


def test_the_command_lane_emit_event_uses_the_store_index(tmp_path: Path) -> None:
    """An external event must not walk the whole store to find its listener.

    The command lane carries its own copy of emit_event's listener lookup; it
    gets the same O(waiters) index, with the whole-store scan as the fallback,
    and every per-run filter (wait_key, paused, pause-wait) unchanged.
    """
    from abstractruntime.core.models import WaitReason, WaitState
    from abstractruntime.storage.json_files import JsonFileRunStore

    run_store = JsonFileRunStore(tmp_path / "store")
    key = "evt:session:s1:ready"
    waiter = _run("listener", status=RunStatus.WAITING)
    waiter.session_id = "s1"
    waiter.waiting = WaitState(reason=WaitReason.EVENT, wait_key=key, resume_to_node="next")
    run_store.save(waiter)
    for i in range(20):
        run_store.save(_run(f"noise-{i}", status=RunStatus.COMPLETED))

    ledger = ObservableLedgerStore(InMemoryLedgerStore())
    runner = GatewayRunner(
        base_dir=tmp_path,
        host=_Host(run_store, ledger),
        config=GatewayRunnerConfig(poll_interval_s=2.0),
        enable=True,
    )

    scans: List[dict] = []
    real_list_runs = run_store.list_runs

    def counting_list_runs(**kw):
        scans.append(dict(kw))
        return real_list_runs(**kw)

    run_store.list_runs = counting_list_runs  # type: ignore[assignment]

    out = runner._apply_emit_event({"name": "ready", "scope": "session"}, default_session_id="s1", client_id=None)

    assert scans == [], f"the command lane fell back to a whole-store scan: {scans}"
    # The listener's workflow is unresolvable in this fixture (_Host raises), so
    # it cannot be resumed — what is pinned here is that it was FOUND without a
    # scan; resume semantics are covered by test_runner_emit_event_durable.py.
    assert isinstance(out, dict)


def test_the_scan_pass_drains_direct_ticks_between_its_walks(tmp_path: Path) -> None:
    """An event landing DURING the pass waits for ONE walk, not the pass.

    The pass makes three whole-directory walks. An id queued while the FIRST
    one is running must be submitted as soon as that walk returns — counting
    the walks completed at submit time is what makes this test notice a drain
    that moved or disappeared, which a wall-clock bound does not.
    """
    run_store = InMemoryRunStore()
    run_store.save(_run("mid-pass"))
    ledger = ObservableLedgerStore(InMemoryLedgerStore())

    in_walk = threading.Event()
    walks = {"n": 0}
    real_list_runs = run_store.list_runs
    real_list_due = run_store.list_due_wait_until

    def _slow(fn):
        def wrapped(**kw):
            walks["n"] += 1
            if walks["n"] == 1:
                in_walk.set()
            time.sleep(0.05)  # a whole-directory walk at operator scale
            return fn(**kw)

        return wrapped

    run_store.list_runs = _slow(real_list_runs)  # type: ignore[assignment]
    run_store.list_due_wait_until = _slow(real_list_due)  # type: ignore[assignment]

    runner = GatewayRunner(
        base_dir=tmp_path,
        host=_Host(run_store, ledger),
        config=GatewayRunnerConfig(poll_interval_s=2.0),
        enable=True,
    )
    walks_at_submit: List[int] = []
    runner._submit_tick = lambda rid, priority=False: walks_at_submit.append(walks["n"])  # type: ignore[assignment]

    def _feed() -> None:
        in_walk.wait(2.0)
        runner._queue_direct_tick("mid-pass")

    th = threading.Thread(target=_feed, daemon=True)
    th.start()
    runner._schedule_ticks()
    th.join(timeout=2.0)

    assert walks_at_submit, "the mid-pass run was never submitted"
    assert walks_at_submit[0] == 1, (
        f"the run queued during walk 1 was submitted after {walks_at_submit[0]} walks — "
        "the drain between the walks is missing or moved"
    )
