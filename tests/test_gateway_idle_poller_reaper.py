"""Idle-poller reaper pins (code-tui c4757 storm; runtime c4764 criterion).

The reaper cancels a WAIT_UNTIL run ONLY with positive spin proof
(`_runtime.wait_until_streak >= streak_min`) + an age belt, and NEVER a
WAIT_EVENT park (residents/visits). Deny-safe: absent/low streak = no reap.
The named failure classes are pinned here (reap-a-visit / reap-a-resident /
reap-a-scheduler) plus the deny-safe-without-counter posture.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from abstractruntime.core.models import RunStatus, WaitReason

from abstractgateway.runner import GatewayRunner, GatewayRunnerConfig


@dataclass
class _Wait:
    reason: Any
    wait_key: str = "w"


@dataclass
class _Run:
    run_id: str
    actor_id: str = "gateway"
    status: Any = RunStatus.WAITING
    wait: Optional[_Wait] = None
    created_at: str = "2020-01-01T00:00:00+00:00"  # ancient by default
    vars: Dict[str, Any] = field(default_factory=dict)


class _Store:
    """Minimal run store double: serves a fixed candidate list, records the
    filter it was asked for."""

    def __init__(self, runs: List[_Run], *, support_reason_filter: bool = True) -> None:
        self._runs = runs
        self._support_reason_filter = support_reason_filter
        self.last_filter: Dict[str, Any] = {}

    def list_runs(self, *, status=None, wait_reason=None, workflow_id=None, limit=100):
        if wait_reason is not None and not self._support_reason_filter:
            raise TypeError("this store does not support wait_reason filtering")
        self.last_filter = {"status": status, "wait_reason": wait_reason, "limit": limit}
        out = list(self._runs)
        if status is not None:
            out = [r for r in out if r.status == status]
        if wait_reason is not None:
            out = [r for r in out if r.wait is not None and r.wait.reason == wait_reason]
        return out[: int(limit)]


class _Runtime:
    def __init__(self) -> None:
        self.cancelled: List[str] = []

    def cancel_run(self, run_id: str, *, reason: str = "") -> None:
        self.cancelled.append(str(run_id))


class _Host:
    def __init__(self, runtime: _Runtime, store: Any) -> None:
        self.runtime = runtime
        self.run_store = store


def _runner(runs: List[_Run], *, support_reason_filter: bool = True, **cfg_over) -> tuple:
    store = _Store(runs, support_reason_filter=support_reason_filter)
    rt = _Runtime()
    host = _Host(rt, store)
    base = {
        "poller_reap_enabled": True,
        "poller_reap_streak_min": 800,
        "poller_reap_min_age_s": 3600.0,
        "poller_reap_interval_s": 0.0,  # no cadence gate in tests
    }
    base.update(cfg_over)
    cfg = GatewayRunnerConfig(**base)
    runner = GatewayRunner(base_dir=_tmp(), host=host, config=cfg, enable=False)
    # run_store is a property reading host.run_store — the host double serves it.
    return runner, rt, store


def _tmp():
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp())


def _spinner(rid: str, streak: int) -> _Run:
    return _Run(
        run_id=rid,
        wait=_Wait(reason=WaitReason.UNTIL),
        vars={"_runtime": {"wait_until_streak": streak}},
    )


def test_reaps_a_proven_spinner() -> None:
    runner, rt, _ = _runner([_spinner("spin-1", 5000)])
    n = runner._reap_idle_pollers(now=time.time())
    assert n == 1 and rt.cancelled == ["spin-1"]


def test_deny_safe_without_counter_reaps_nothing() -> None:
    """No runtime counter yet = no `wait_until_streak` in vars = NEVER reap
    (the reaper can land before the counter and cancel nothing)."""
    run = _Run(run_id="no-counter", wait=_Wait(reason=WaitReason.UNTIL), vars={"_runtime": {}})
    runner, rt, _ = _runner([run])
    assert runner._reap_idle_pollers(now=time.time()) == 0
    assert rt.cancelled == []


def test_low_streak_is_never_reaped() -> None:
    runner, rt, _ = _runner([_spinner("low", 799)])  # one below the floor
    assert runner._reap_idle_pollers(now=time.time()) == 0
    assert rt.cancelled == []


def test_wait_event_is_never_reaped_even_with_huge_streak() -> None:
    """reap-a-resident / reap-a-visit: a WAIT_EVENT park (every resident
    agent, every parked visit) is structurally excluded — even a bogus huge
    streak on it must never reap."""
    run = _Run(
        run_id="resident",
        wait=_Wait(reason=WaitReason.EVENT),
        vars={"_runtime": {"wait_until_streak": 99999}},
    )
    runner, rt, store = _runner([run])
    assert runner._reap_idle_pollers(now=time.time()) == 0
    assert rt.cancelled == []
    # The scan asked the store for WAIT_UNTIL only (structural exclusion).
    assert store.last_filter["wait_reason"] == WaitReason.UNTIL


def test_young_spinner_is_not_reaped_before_min_age() -> None:
    """reap-a-scheduler belt: a run created seconds ago is spared regardless
    of streak (created_at is immutable — never run.updated_at)."""
    import datetime

    run = _spinner("young", 5000)
    run.created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    runner, rt, _ = _runner([run])
    assert runner._reap_idle_pollers(now=time.time()) == 0
    assert rt.cancelled == []


def test_non_gateway_run_is_never_reaped() -> None:
    run = _spinner("foreign", 5000)
    run.actor_id = "telegram"
    runner, rt, _ = _runner([run])
    assert runner._reap_idle_pollers(now=time.time()) == 0


def test_bool_streak_is_not_a_valid_count() -> None:
    """`True` is an int subclass — a stray boolean must not read as a huge
    streak and trigger a reap."""
    run = _Run(run_id="b", wait=_Wait(reason=WaitReason.UNTIL), vars={"_runtime": {"wait_until_streak": True}})
    runner, rt, _ = _runner([run])
    assert runner._reap_idle_pollers(now=time.time()) == 0


def test_disabled_reaper_is_a_noop() -> None:
    runner, rt, _ = _runner([_spinner("spin", 5000)], poller_reap_enabled=False)
    assert runner._reap_idle_pollers(now=time.time()) == 0
    assert rt.cancelled == []


def test_cadence_gate_bounds_sweep_frequency() -> None:
    runner, rt, store = _runner([_spinner("spin", 5000)], poller_reap_interval_s=300.0)
    t = time.time()
    assert runner._reap_idle_pollers(now=t) == 1  # first sweep runs
    # A second sweep within the interval is skipped (even with a fresh spinner).
    store._runs.append(_spinner("spin2", 5000))
    assert runner._reap_idle_pollers(now=t + 10.0) == 0


def test_older_store_without_reason_filter_gates_per_run() -> None:
    """A store that cannot filter by wait_reason must still never reap a
    WAIT_EVENT run — the per-run belt catches it."""
    spinner = _spinner("spin", 5000)
    resident = _Run(run_id="res", wait=_Wait(reason=WaitReason.EVENT),
                    vars={"_runtime": {"wait_until_streak": 99999}})
    runner, rt, _ = _runner([spinner, resident], support_reason_filter=False)
    n = runner._reap_idle_pollers(now=time.time())
    assert n == 1 and rt.cancelled == ["spin"]


def test_status_surfaces_reap_totals() -> None:
    runner, rt, _ = _runner([_spinner("s1", 5000), _spinner("s2", 5000)])
    runner._reap_idle_pollers(now=time.time())
    st = runner.runner_status()
    assert st.get("idle_pollers_reaped_total") == 2
    assert set(st.get("idle_pollers_reaped_recent") or []) == {"s1", "s2"}


# ------------------------------------------------------------ real-store pin
# Adversary F1/F4: the criterion above rides a hand-written _Run double whose
# field is `wait`; the REAL RunState field is `waiting`. This integration pin
# reaps a spinner through a REAL InMemoryRunStore + real RunState/WaitState +
# real Runtime.cancel_run — so the shape can never silently drift into a
# no-op reaper again (the hand-written-double-of-the-other-package lesson).


def test_reaps_a_real_runstate_spinner_end_to_end() -> None:
    import datetime

    from abstractruntime.core.models import RunState, RunStatus, WaitReason, WaitState
    from abstractruntime.storage.in_memory import InMemoryRunStore

    store = InMemoryRunStore()
    ancient = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=2)).isoformat()
    spinner = RunState(
        run_id="real-spin",
        workflow_id="basic-agent@0.0.3:node9",
        status=RunStatus.WAITING,
        current_node="node-9",
        actor_id="gateway",
        created_at=ancient,
        waiting=WaitState(reason=WaitReason.UNTIL, wait_key="poll"),
        vars={"_runtime": {"wait_until_streak": 5000}},
    )
    # A real WAIT_EVENT resident with a bogus huge streak must survive.
    resident = RunState(
        run_id="real-resident",
        workflow_id="resident@1",
        status=RunStatus.WAITING,
        current_node="park",
        actor_id="gateway",
        created_at=ancient,
        waiting=WaitState(reason=WaitReason.EVENT, wait_key="evt:x"),
        vars={"_runtime": {"wait_until_streak": 99999}},
    )
    store.save(spinner)
    store.save(resident)

    rt = _Runtime()
    host = _Host(rt, store)
    cfg = GatewayRunnerConfig(poller_reap_enabled=True, poller_reap_streak_min=800,
                              poller_reap_min_age_s=3600.0, poller_reap_interval_s=0.0)
    runner = GatewayRunner(base_dir=_tmp(), host=host, config=cfg, enable=False)

    n = runner._reap_idle_pollers(now=time.time())
    assert n == 1, "the real-shaped WAIT_UNTIL spinner must reap"
    assert rt.cancelled == ["real-spin"]
    assert "real-resident" not in rt.cancelled  # WAIT_EVENT never reaped


def test_inflight_run_is_not_reaped_during_a_tick() -> None:
    """Adversary F2: a run the tick pool is actively resuming is skipped
    (aliased-RunState cancel/tick race) — re-reaped next sweep once idle."""
    runner, rt, _ = _runner([_spinner("busy", 5000)])
    t = time.time()
    with runner._inflight_lock:
        runner._inflight["busy"] = t
    assert runner._reap_idle_pollers(now=t) == 0
    assert rt.cancelled == []
    # Once it leaves flight, the next sweep reaps it (advance past the 1s
    # cadence floor so this sweep is not gated).
    with runner._inflight_lock:
        runner._inflight.clear()
    assert runner._reap_idle_pollers(now=t + 2.0) == 1
