"""`event: done` must follow the run's terminal save, not the idle tick.

The SSE ledger stream learns that records stopped from its ledger subscription,
but a run's TERMINAL state lives in the run file — so the generator has to
re-read the run to know the stream is over. It used to do that only on a 0.75s
idle tick, which put the `done` frame ~0.26s (up to 0.75s) behind the run's
terminal save: 85% of a no-tool chat turn's remaining client-observed overhead
(mission B, section 5).

The fix is a bounded SETTLE WINDOW: a drain that emitted records re-checks the
status on the very next progress-less pass, and keeps checking for
`_SSE_SETTLE_CHECKS` passes at `_SSE_SETTLE_POLL_S` so the
terminal-save/last-append race resolves without a sleep. What these tests pin:

* the window is opened by emitted records and bounded (not a poll loop),
* `done` still comes AFTER a final drain — nothing is dropped,
* the 0.75s idle tick still closes a run that terminates with no new records,
* the 0.25s cross-process fallback poll is unchanged outside the window.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest


def _sse_events(body: str) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    cur: Dict[str, Any] = {}
    for line in body.split("\n"):
        if not line.strip():
            if cur:
                events.append(cur)
                cur = {}
            continue
        if line.startswith(":"):
            continue
        if line.startswith("id: "):
            cur["id"] = line[4:]
        elif line.startswith("event: "):
            cur["event"] = line[7:]
        elif line.startswith("data: "):
            cur["data"] = json.loads(line[6:])
    if cur:
        events.append(cur)
    return events


@pytest.mark.basic
def test_settle_window_constants_are_bounded() -> None:
    """The window is a handful of run loads per record batch — never a poll."""
    from abstractgateway.routes import gateway as gw

    assert 1 <= gw._SSE_SETTLE_CHECKS <= 10
    assert 0.0 < gw._SSE_SETTLE_POLL_S <= 0.05
    assert gw._SSE_SETTLE_POLL_S < 0.25, "the settle poll must be shorter than the idle fallback"


def _mk_record(run_id: str, i: int):
    from abstractruntime.core.models import StepRecord

    return StepRecord(
        run_id=run_id,
        step_id=f"s{i}",
        node_id=f"node-{i}",
        status="completed",
        started_at="2026-09-22T00:00:00+00:00",
        ended_at="2026-09-22T00:00:01+00:00",
        result={"i": i},
    )


def _boot_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    from test_gateway_runs_list_endpoint import _write_min_bundle

    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-b2-latency", flow_id="root")

    token = "t" * 32
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    from abstractgateway.app import app

    return TestClient(app), {"Authorization": f"Bearer {token}"}


@pytest.mark.basic
def test_done_lands_promptly_after_a_run_terminates_mid_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measured behaviour, end to end through the real route.

    A live (RUNNING) run is streamed; records are appended and then the run is
    saved terminal together with its LAST record. The `done` frame must arrive
    in well under the 0.75s idle tick that used to gate it.
    """
    import threading

    from abstractruntime import RunState, RunStatus

    client, headers = _boot_client(tmp_path, monkeypatch)
    with client:
        from abstractgateway.service import get_gateway_service

        svc = get_gateway_service()
        rid = "run-b2-latency"
        run = RunState.new(workflow_id="wf", entry_node="start")
        run.run_id = rid
        run.status = RunStatus.RUNNING
        svc.host.run_store.save(run)
        svc.host.ledger_store.append(_mk_record(rid, 0))

        started = threading.Event()
        finished_at: List[float] = []

        def _finish() -> None:
            started.wait(5.0)
            time.sleep(0.4)  # stream is live and idle on this run
            svc.host.ledger_store.append(_mk_record(rid, 1))
            latest = svc.host.run_store.load(rid)
            latest.status = RunStatus.COMPLETED
            svc.host.run_store.save(latest)
            finished_at.append(time.monotonic())

        th = threading.Thread(target=_finish, daemon=True)
        th.start()

        body_parts: List[str] = []
        with client.stream("GET", f"/api/gateway/runs/{rid}/ledger/stream", headers=headers) as resp:
            started.set()
            for chunk in resp.iter_text():
                body_parts.append(chunk)
        done_at = time.monotonic()
        th.join(timeout=5.0)

        events = _sse_events("".join(body_parts))
        steps = [e for e in events if e.get("event") == "step"]
        dones = [e for e in events if e.get("event") == "done"]
        assert [e["data"]["record"]["node_id"] for e in steps] == ["node-0", "node-1"]
        assert len(dones) == 1, "a terminal run closes with exactly one done frame"
        assert dones[0]["data"]["cursor"] == 2, "done reports the cursor AFTER the final drain"

        assert finished_at, "fixture thread never finished the run"
        lag = done_at - finished_at[0]
        # Pre-fix this was one 0.75s status tick away (measured 0.26s median,
        # 0.51s worst on a warm store). Give CI room but keep the assertion
        # meaningful: it must be far below the old tick.
        assert lag < 0.30, f"done frame lagged the terminal save by {lag:.3f}s"


@pytest.mark.basic
def test_a_run_that_terminates_with_no_new_records_still_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The settle window is an ADDITION: the idle status tick still closes a
    run whose terminal save appends nothing (a cancel, a split-runner
    deployment where this process sees no appends at all)."""
    import threading

    from abstractruntime import RunState, RunStatus

    client, headers = _boot_client(tmp_path, monkeypatch)
    with client:
        from abstractgateway.service import get_gateway_service

        svc = get_gateway_service()
        rid = "run-b2-silent-terminal"
        run = RunState.new(workflow_id="wf", entry_node="start")
        run.run_id = rid
        run.status = RunStatus.RUNNING
        svc.host.run_store.save(run)
        svc.host.ledger_store.append(_mk_record(rid, 0))

        started = threading.Event()

        def _cancel() -> None:
            started.wait(5.0)
            time.sleep(0.3)
            latest = svc.host.run_store.load(rid)
            latest.status = RunStatus.CANCELLED
            svc.host.run_store.save(latest)  # NO ledger append

        th = threading.Thread(target=_cancel, daemon=True)
        th.start()

        body_parts: List[str] = []
        with client.stream("GET", f"/api/gateway/runs/{rid}/ledger/stream", headers=headers) as resp:
            started.set()
            for chunk in resp.iter_text():
                body_parts.append(chunk)
        th.join(timeout=5.0)

        events = _sse_events("".join(body_parts))
        assert [e for e in events if e.get("event") == "done"], "silent terminal must still close the stream"


@pytest.mark.basic
def test_a_terminal_run_streams_replay_then_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Already-terminal at open: unchanged — full replay, one done frame."""
    from abstractruntime import RunState, RunStatus

    client, headers = _boot_client(tmp_path, monkeypatch)
    with client:
        from abstractgateway.service import get_gateway_service

        svc = get_gateway_service()
        rid = "run-b2-already-done"
        run = RunState.new(workflow_id="wf", entry_node="start")
        run.run_id = rid
        run.status = RunStatus.COMPLETED
        svc.host.run_store.save(run)
        for i in range(3):
            svc.host.ledger_store.append(_mk_record(rid, i))

        with client.stream("GET", f"/api/gateway/runs/{rid}/ledger/stream", headers=headers) as resp:
            body = "".join(chunk for chunk in resp.iter_text())
        events = _sse_events(body)
        steps = [e for e in events if e.get("event") == "step"]
        dones = [e for e in events if e.get("event") == "done"]
        assert [e["data"]["record"]["node_id"] for e in steps] == ["node-0", "node-1", "node-2"]
        assert len(dones) == 1
        assert dones[0]["data"]["cursor"] == 3
