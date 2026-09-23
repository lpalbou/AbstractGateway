"""Event-driven runner wake (chat-turn orchestration overhead, 2026-09-22).

The incident these pin: a no-tool chat turn on the `basic-agent` bundle spent
~6.8s OUTSIDE the model call. ~3.9s of it was pure runner latency — six hops
(spawn a status subflow, resume the parent, spawn the agent child, resume,
spawn the second status subflow, resume) each waiting out the runner's blind
`_stop.wait(poll_interval_s)` plus, on a quiet store, a scan-gate probe.

The fix keeps the scan gate (the c2394 100%-CPU incident it exists for) and
adds an in-process WAKE: the loop sleeps on a `threading.Event` with
poll_interval_s as the UPPER bound, and every in-process run-state transition
(observed through the ledger subscription seam) sets it. Runs an event names
precisely are ticked directly — no store scan.

What must stay true, and is asserted here:
- a nudge/wake is served in milliseconds, not at the poll interval;
- a BURST of wakes costs ONE extra pass (an Event is a flag, not a queue);
- an IDLE runner never sets the wake, so idle CPU is exactly what it was;
- a past-due wait deadline never shortens the sleep to zero (that would be a
  new busy-scan loop — the very failure the scan gate prevents);
- direct ticks are load-verified, gateway-owned, RUNNING-gated, held across a
  host pause, and never reachable from a standby (lock-refused) runner.
"""

from __future__ import annotations

import datetime
import threading
import time
from pathlib import Path
from typing import Any, List

import pytest

pytestmark = pytest.mark.basic

from abstractruntime.core.models import RunState, RunStatus, WaitReason, WaitState  # noqa: E402
from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore  # noqa: E402
from abstractruntime.storage.observable import ObservableLedgerStore  # noqa: E402

from abstractgateway import host_control  # noqa: E402
from abstractgateway.runner import GatewayRunner, GatewayRunnerConfig  # noqa: E402


def _run(
    run_id: str,
    *,
    status: RunStatus = RunStatus.RUNNING,
    actor_id: str = "gateway",
    parent_run_id: str | None = None,
    waiting: WaitState | None = None,
) -> RunState:
    return RunState(
        run_id=run_id,
        workflow_id="wf",
        status=status,
        current_node="n",
        vars={},
        waiting=waiting,
        actor_id=actor_id,
        parent_run_id=parent_run_id,
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

    def runtime_and_workflow_for_run(self, run_id: str):  # pragma: no cover - no real workflows
        raise KeyError(run_id)


class _ObservedRunner(GatewayRunner):
    """A runner that records when its loop iterated and what it submitted."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.iterations: List[float] = []
        self.submitted: List[str] = []
        self._obs_lock = threading.Lock()

    def _scan_pass_due(self) -> bool:
        # LOOP-ITERATION PROBE. This used to hook `_drain_direct_ticks`, which
        # was called exactly once per iteration; mission B2 made the scan pass
        # drain BETWEEN its store walks too (so an event landing mid-pass is
        # not held for the whole pass), and the drain is therefore no longer a
        # 1:1 proxy for "the loop went round". `_scan_pass_due` still is: the
        # loop calls it once, right after the pre-pass drain.
        with self._obs_lock:
            self.iterations.append(time.monotonic())
        return super()._scan_pass_due()

    def _submit_tick(self, run_id: str, *, priority: bool = False) -> None:
        with self._obs_lock:
            self.submitted.append(run_id)

    def iteration_count(self) -> int:
        with self._obs_lock:
            return len(self.iterations)

    def last_iteration(self) -> float:
        with self._obs_lock:
            return self.iterations[-1] if self.iterations else 0.0


def _runner(tmp_path: Path, *, poll_s: float = 2.0, enable: bool = True, runs: List[RunState] | None = None):
    run_store = InMemoryRunStore()
    for r in runs or []:
        run_store.save(r)
    ledger = ObservableLedgerStore(InMemoryLedgerStore())
    return _ObservedRunner(
        base_dir=tmp_path,
        host=_Host(run_store, ledger),
        config=GatewayRunnerConfig(poll_interval_s=poll_s),
        enable=enable,
    )


def _wait_until(pred, *, timeout_s: float = 5.0, interval_s: float = 0.005) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval_s)
    return False


def _subworkflow_wait_record(child_run_id: str) -> dict:
    """The ledger record a parent appends when start_subworkflow parks it."""
    return {
        "run_id": "parent",
        "node_id": "node-4",
        "status": "waiting",
        "effect": {"type": "start_subworkflow", "payload": {}},
        "result": {
            "wait": {
                "reason": "subworkflow",
                "wait_key": f"subworkflow:{child_run_id}",
                "until": None,
                "details": {"sub_run_id": child_run_id, "async": True},
            }
        },
    }


# ---------------------------------------------------------------------------
# 1. The wake beats the poll interval
# ---------------------------------------------------------------------------


def test_nudge_wakes_the_loop_well_before_the_poll_interval_elapses(tmp_path: Path) -> None:
    r = _runner(tmp_path, poll_s=2.0)
    r.start()
    try:
        assert _wait_until(lambda: r.iteration_count() >= 1), "runner loop never started"
        # Let it settle into the 2s sleep.
        time.sleep(0.15)
        before = r.iteration_count()
        t_nudge = time.monotonic()
        r.nudge()
        assert _wait_until(lambda: r.iteration_count() > before, timeout_s=1.0), (
            "nudge did not wake the loop within 1s (poll interval is 2s)"
        )
        latency = r.last_iteration() - t_nudge
        # Generous bound: the point is "milliseconds, not 2 seconds".
        assert latency < 0.5, f"nudge latency {latency:.3f}s is not sub-poll-interval"
    finally:
        r.stop(timeout_s=3.0)


def test_a_finished_tick_wakes_the_loop_for_the_next_one(tmp_path: Path) -> None:
    """A run still RUNNING after its tick is runnable NOW: the executor's
    done-callback must WAKE the loop, not only force the next scan (which
    would still be a poll interval away).

    Mission B2 changed WHAT the callback schedules: forcing a whole-store pass
    after every tick cost four directory walks (~0.18s at 9,326 run files) on
    the loop thread to find the ONE run that had just ticked, and that pass
    stood in front of the next hop's wake. The callback now re-queues that run
    for a direct tick instead (load-verified and RUNNING-gated in the drain),
    and only an UNRESOLVABLE workflow still forces the pass — see
    test_gateway_runner_scan_not_on_hot_path.py. The wake itself, which is
    what this test is about, is unchanged.
    """
    run_store = InMemoryRunStore()
    run_store.save(_run("ok"))
    ticked = threading.Event()

    class _TickProbe(GatewayRunner):
        def _tick_run(self, run_id: str) -> None:
            ticked.set()

    r = _TickProbe(
        base_dir=tmp_path,
        host=_Host(run_store, ObservableLedgerStore(InMemoryLedgerStore())),
        config=GatewayRunnerConfig(poll_interval_s=2.0),
        enable=False,
    )
    r._wake.clear()
    r._scan_force = False
    r._submit_tick("ok")  # the REAL submit path, including add_done_callback
    assert ticked.wait(timeout=5.0), "tick never ran"
    assert _wait_until(lambda: r._wake.is_set(), timeout_s=5.0), "finished tick did not wake the loop"
    with r._inflight_lock:
        assert "ok" in r._direct_tick_ids, "the just-ticked run must be queued for the next tick"
    assert r._scan_force is False, "a resolvable tick must not force a whole-store pass"


# ---------------------------------------------------------------------------
# 2. Coalescing: N wakes in a burst cost ONE pass
# ---------------------------------------------------------------------------


def test_a_burst_of_nudges_coalesces_into_a_single_pass(tmp_path: Path) -> None:
    r = _runner(tmp_path, poll_s=2.0)
    r.start()
    try:
        assert _wait_until(lambda: r.iteration_count() >= 1)
        time.sleep(0.15)
        before = r.iteration_count()
        for i in range(200):
            r.nudge(f"absent-run-{i}")
        assert _wait_until(lambda: r.iteration_count() > before, timeout_s=1.0)
        time.sleep(0.4)  # still far inside the 2s poll interval
        extra = r.iteration_count() - before
        # One pass is the contract; allow one more for a nudge landing in the
        # clear() window. 200 would mean the wake is a queue, not a flag.
        assert 1 <= extra <= 2, f"burst of 200 nudges produced {extra} passes"
    finally:
        r.stop(timeout_s=3.0)


# ---------------------------------------------------------------------------
# 3. Idle: the wake is never set, so idle cost is unchanged
# ---------------------------------------------------------------------------


def test_an_idle_runner_never_sets_the_wake(tmp_path: Path) -> None:
    """Idle CPU safety: with nothing happening, the loop must sleep out the
    full poll interval exactly as it did before the wake existed."""
    r = _runner(tmp_path, poll_s=0.1)
    r.start()
    try:
        assert _wait_until(lambda: r.iteration_count() >= 1)
        start_count = r.iteration_count()
        t0 = time.monotonic()
        time.sleep(0.6)
        elapsed = time.monotonic() - t0
        passes = r.iteration_count() - start_count
        expected = elapsed / 0.1
        # A wake-storm would show as passes >> expected. Generous upper bound.
        assert passes <= expected * 2 + 2, f"idle runner ran {passes} passes in {elapsed:.2f}s (poll 0.1s)"
        assert passes >= 1, "idle runner stopped polling entirely"
    finally:
        r.stop(timeout_s=3.0)


# ---------------------------------------------------------------------------
# 4. The ledger seam
# ---------------------------------------------------------------------------


def test_ledger_subworkflow_wait_queues_the_child_for_a_direct_tick(tmp_path: Path) -> None:
    r = _runner(tmp_path, poll_s=2.0)
    r._wake.clear()
    r._on_ledger_record(_subworkflow_wait_record("child-1"))
    with r._inflight_lock:
        assert r._direct_tick_ids == {"child-1"}
    assert r._wake.is_set() is True


def test_ledger_wait_until_pulls_the_next_wake_forward(tmp_path: Path) -> None:
    r = _runner(tmp_path, poll_s=2.0)
    r._next_due_epoch = None
    until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=0.4)
    r._on_ledger_record(
        {
            "run_id": "x",
            "status": "waiting",
            "effect": {"type": "wait_until", "payload": {}},
            "result": {"wait": {"reason": "until", "wait_key": None, "until": until.isoformat()}},
        }
    )
    assert r._next_due_epoch is not None
    # ...and the loop's sleep is shortened to it instead of the 2s poll.
    assert r._wake_timeout() < 0.6


def test_any_ledger_record_wakes_the_loop(tmp_path: Path) -> None:
    r = _runner(tmp_path, poll_s=2.0)
    r._wake.clear()
    r._on_ledger_record({"run_id": "x", "status": "completed", "effect": None, "result": {"completed": True}})
    assert r._wake.is_set() is True


def test_a_malformed_ledger_record_still_wakes_and_never_raises(tmp_path: Path) -> None:
    """The callback runs on the tick thread inside the durability path."""
    r = _runner(tmp_path, poll_s=2.0)
    for bad in (None, "not-a-dict", 17, {"result": "not-a-dict"}, {"result": {"wait": 5}}):
        r._wake.clear()
        r._on_ledger_record(bad)
        assert r._wake.is_set() is True


def test_start_subscribes_to_an_observable_ledger(tmp_path: Path) -> None:
    r = _runner(tmp_path, poll_s=2.0)
    r.start()
    try:
        assert callable(r._ledger_unsubscribe), "runner did not subscribe to the ledger"
        r._wake.clear()
        from abstractruntime.core.models import StepRecord

        rec = StepRecord.start(run=_run("some-run"), node_id="n", effect=None, idempotency_key="k")
        rec.finish_success({"ok": True})
        r.ledger_store.append(rec)
        assert _wait_until(lambda: r.iteration_count() >= 1)
    finally:
        r.stop(timeout_s=3.0)
    assert r._ledger_unsubscribe is None, "stop() must unsubscribe"


# ---------------------------------------------------------------------------
# 5. The anti-spin belt on the deadline-aware timeout
# ---------------------------------------------------------------------------


def test_wake_timeout_never_spins_on_a_past_due_deadline(tmp_path: Path) -> None:
    r = _runner(tmp_path, poll_s=0.25)
    r._next_due_epoch = None
    assert r._wake_timeout() == pytest.approx(0.25, abs=1e-6)

    # A deadline INSIDE the interval shortens the sleep...
    r._next_due_epoch = time.time() + 0.05
    assert 0.0 < r._wake_timeout() <= 0.06

    # ...one BEYOND it does not lengthen it...
    r._next_due_epoch = time.time() + 30.0
    assert r._wake_timeout() == pytest.approx(0.25, abs=1e-6)

    # ...and a PAST-due one keeps the FULL interval. Shortening here would
    # busy-spin the loop on any waiting run nobody can tick (foreign actor,
    # unresolvable workflow) — a new c2394.
    r._next_due_epoch = time.time() - 60.0
    assert r._wake_timeout() == pytest.approx(0.25, abs=1e-6)


# ---------------------------------------------------------------------------
# 6. Direct-tick drain: verified, gated, pause-respecting
# ---------------------------------------------------------------------------


def test_direct_drain_ticks_only_gateway_owned_running_runs(tmp_path: Path) -> None:
    runs = [
        _run("ok"),
        _run("foreign", actor_id="entity"),
        _run("finished", status=RunStatus.COMPLETED),
        _run("parked", status=RunStatus.WAITING, waiting=WaitState(reason=WaitReason.EVENT, wait_key="e")),
    ]
    r = _runner(tmp_path, runs=runs)
    for rid in ("ok", "foreign", "finished", "parked", "never-existed"):
        r._queue_direct_tick(rid)
    r._drain_direct_ticks()
    assert r.submitted == ["ok"]
    with r._inflight_lock:
        assert r._direct_tick_ids == set()


def test_direct_ticks_survive_a_host_pause_and_run_on_resume(tmp_path: Path) -> None:
    r = _runner(tmp_path, runs=[_run("ok")])
    r._queue_direct_tick("ok")
    host_control.pause(reason="test")
    try:
        r._drain_direct_ticks()
        assert r.submitted == []
        with r._inflight_lock:
            assert r._direct_tick_ids == {"ok"}, "a pause must HOLD queued ticks, not drop them"
    finally:
        host_control.resume()
    r._drain_direct_ticks()
    assert r.submitted == ["ok"]


def test_queued_direct_ticks_are_bounded(tmp_path: Path) -> None:
    r = _runner(tmp_path)
    for i in range(10050):
        r._queue_direct_tick(f"r{i}")
    with r._inflight_lock:
        assert len(r._direct_tick_ids) <= 10000


def test_a_standby_runner_never_drains_direct_ticks(tmp_path: Path) -> None:
    """Lock-not-held path unchanged: only the lock HOLDER's loop ticks."""
    holder = _runner(tmp_path, poll_s=0.05)
    holder.start()
    try:
        assert _wait_until(lambda: holder.runner_status().get("lock_held") is True)
        standby = _runner(tmp_path, poll_s=0.05)
        standby.start()
        try:
            assert _wait_until(lambda: standby.runner_status().get("lock_refused") is True, timeout_s=5.0)
            standby._queue_direct_tick("anything")
            time.sleep(0.4)
            assert standby.submitted == [], "a standby runner ticked a run"
            with standby._inflight_lock:
                assert standby._direct_tick_ids == {"anything"}
        finally:
            standby.stop(timeout_s=3.0)
    finally:
        holder.stop(timeout_s=3.0)


# ---------------------------------------------------------------------------
# 7. Parent lookup: the direct parent replaces a full-store scan
# ---------------------------------------------------------------------------


def test_parent_lookup_uses_parent_run_id_instead_of_scanning(tmp_path: Path) -> None:
    child = _run("child", status=RunStatus.COMPLETED, parent_run_id="parent")
    parent = _run(
        "parent",
        status=RunStatus.WAITING,
        waiting=WaitState(
            reason=WaitReason.SUBWORKFLOW,
            wait_key="subworkflow:child",
            details={"sub_run_id": "child"},
        ),
    )
    r = _runner(tmp_path, runs=[child, parent])

    scans: List[int] = []
    original = r.run_store.list_runs

    def _counted(**kwargs):
        scans.append(1)
        return original(**kwargs)

    r.run_store.list_runs = _counted  # type: ignore[method-assign]
    found = list(r._waiting_parents_for_child("child"))
    assert [p.run_id for p in found] == ["parent"]
    assert scans == [], "the direct-parent fast path still scanned the store"


def test_parent_lookup_falls_back_to_the_scan_when_the_parent_does_not_match(tmp_path: Path) -> None:
    # Child with no parent_run_id: only the scan can answer.
    child = _run("child", status=RunStatus.COMPLETED)
    other = _run(
        "waiter",
        status=RunStatus.WAITING,
        waiting=WaitState(
            reason=WaitReason.SUBWORKFLOW,
            wait_key="subworkflow:child",
            details={"sub_run_id": "child"},
        ),
    )
    r = _runner(tmp_path, runs=[child, other])
    scans: List[int] = []
    original = r.run_store.list_runs

    def _counted(**kwargs):
        scans.append(1)
        return original(**kwargs)

    r.run_store.list_runs = _counted  # type: ignore[method-assign]
    found = list(r._waiting_parents_for_child("child"))
    assert scans == [1], "the fallback scan did not run"
    assert "waiter" in [p.run_id for p in found]
