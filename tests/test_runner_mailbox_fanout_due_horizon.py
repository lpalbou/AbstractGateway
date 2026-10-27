"""Durable fan-out and the wake horizon past `run_scan_limit` (backlog 1046,
design round-1 F3).

Both used to read the newest `run_scan_limit` (200) active runs: with more
parked runs than that, a durable `emit_event` silently missed an older
mailbox, and the file-store scan gate slept through a due wake whose run sat
outside the window. Each test parks 300 automation-shaped controller runs
(WAIT_EVENT `automation:<id>:wake` with a far deadline) AFTER the run that
matters, so that run is provably outside the window.
"""

from __future__ import annotations

import datetime
import time
from pathlib import Path

import pytest

from timing import timing_budget  # noqa: E402 - tests/timing.py (wall-clock budgets)

from abstractruntime.core.models import RunState, RunStatus, WaitReason, WaitState
from abstractruntime.storage.artifacts import InMemoryArtifactStore
from abstractruntime.storage.json_files import JsonFileRunStore
from abstractruntime.storage.offloading import OffloadingRunStore
from abstractruntime.storage.sqlite import SqliteDatabase, SqliteRunStore

from abstractgateway.runner import GatewayRunner, GatewayRunnerConfig, _epoch_from_iso

PARKED = 300


def _iso_in(seconds: float) -> str:
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)).isoformat()


def _parked(run_id: str, *, wait_key: str, until: str | None, vars: dict | None = None, updated_at: str | None = None) -> RunState:
    return RunState(
        run_id=run_id,
        workflow_id="wf",
        status=RunStatus.WAITING,
        current_node="n",
        vars=dict(vars or {}),
        waiting=WaitState(reason=WaitReason.EVENT, wait_key=wait_key, until=until),
        actor_id="gateway",
        updated_at=updated_at,
    )


class _Host:
    def __init__(self, run_store) -> None:
        self.run_store = run_store
        self.ledger_store = None
        self.artifact_store = None

    def runtime_and_workflow_for_run(self, run_id: str):  # pragma: no cover - no waiter on the emitted key
        raise KeyError(run_id)


def _runner(tmp_path: Path, store) -> GatewayRunner:
    return GatewayRunner(
        base_dir=tmp_path,
        host=_Host(store),
        config=GatewayRunnerConfig(scan_gate_idle_interval_s=0.05),
        enable=False,
    )


def _store(tmp_path: Path, backend: str):
    if backend == "file":
        inner = JsonFileRunStore(tmp_path)
    else:
        inner = SqliteRunStore(SqliteDatabase(tmp_path / "gateway.sqlite3"))
    return OffloadingRunStore(inner, artifact_store=InMemoryArtifactStore())


def _stamp(i: int) -> str:
    return f"2026-10-25T10:{i // 60:02d}:{i % 60:02d}.000000+00:00"


@pytest.mark.parametrize("backend", ["file", "sqlite"])
def test_durable_emit_reaches_a_mailbox_behind_300_parked_automations(tmp_path: Path, backend: str) -> None:
    store = _store(tmp_path, backend)
    store.save(_parked("mailbox_run", wait_key="evt:session:s1:other", until=None, vars={"events_mailbox": "box"}, updated_at=_stamp(0)))
    for i in range(PARKED):
        store.save(_parked(f"auto_{i:03d}", wait_key=f"automation:a{i}:wake", until=_iso_in(3600 + i), updated_at=_stamp(i + 1)))

    # The recency window the old fan-out used provably excludes the mailbox run.
    window = {r.run_id for r in store.list_runs(status=RunStatus.WAITING, limit=200)}
    assert "mailbox_run" not in window

    out = _runner(tmp_path, store)._apply_emit_event(
        {"name": "box", "scope": "global", "durable": True, "event_id": "evt-1", "payload": {"x": 1}},
        default_session_id="s1",
        client_id=None,
    )
    assert out == {"resumed": 0, "appended": 1}
    inbox = store.load("mailbox_run").vars["events_inbox"]
    assert [e["event_id"] for e in inbox] == ["evt-1"] and inbox[0]["seq"] == 1

    # A finished run leaves the mailbox index: nothing is appended to it.
    done = store.load("mailbox_run")
    done.status = RunStatus.COMPLETED
    done.waiting = None
    store.save(done)
    again = _runner(tmp_path, store)._apply_emit_event(
        {"name": "box", "scope": "global", "durable": True, "event_id": "evt-2"}, default_session_id="s1", client_id=None
    )
    assert again["appended"] == 0


def test_a_wake_behind_300_parked_automations_fires_on_time(tmp_path: Path) -> None:
    store = _store(tmp_path, "file")
    wake_until = _iso_in(0.6)
    store.save(_parked("wake_run", wait_key="automation:wake:wake", until=wake_until))
    for i in range(PARKED):
        store.save(_parked(f"auto_{i:03d}", wait_key=f"automation:a{i}:wake", until=_iso_in(3600 + i)))
    assert "wake_run" not in {r.run_id for r in store.list_runs(status=RunStatus.WAITING, limit=200)}

    r = _runner(tmp_path, store)
    submitted: list[str] = []
    r._submit_tick = lambda rid, **_kw: submitted.append(rid)  # type: ignore[method-assign]

    due_epoch = _epoch_from_iso(wake_until)
    fired_at = None
    stop = time.time() + 4.0
    while time.time() < stop:  # the runner loop's gate + scheduling pass
        if r._scan_pass_due():
            r._schedule_ticks()
        if "wake_run" in submitted:
            fired_at = time.time()
            break
        time.sleep(0.02)
    assert fired_at is not None, "the due wake was never scheduled (gate slept past it)"
    timing_budget(fired_at - due_epoch, 0.5, "due wake after its due time")
    assert submitted == ["wake_run"]  # the 300 far deadlines are not due


@pytest.mark.parametrize("backend", ["file", "sqlite"])
def test_next_wait_deadline_sees_every_waiting_run(tmp_path: Path, backend: str) -> None:
    store = _store(tmp_path, backend)
    assert store.next_wait_deadline() is None
    early = _iso_in(5)
    store.save(_parked("early", wait_key="automation:e:wake", until=early))
    for i in range(PARKED):
        store.save(_parked(f"auto_{i:03d}", wait_key=f"automation:a{i}:wake", until=_iso_in(3600 + i)))
    assert store.next_wait_deadline() == early
    r = _runner(tmp_path, store)
    r._note_next_due([])  # the window is ignored when the store answers
    assert r._next_due_epoch == pytest.approx(_epoch_from_iso(early), abs=0.01)
