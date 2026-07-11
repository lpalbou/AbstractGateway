"""Singleton-lock hardening for GatewayRunner (silent-hang incident class).

Incident: an orphaned older `abstractgateway serve` process kept holding
`<data_dir>/gateway_runner.lock`, so the current port-serving process's runner
was refused with one invisible logger.warning — every run it accepted was
ticked by NOBODY and hung forever on its entry node with zero ledger records.

These tests pin the layered fix:
1. flock semantics ground truth: the kernel releases the lock when the holder
   dies (even SIGKILL) — a dead holder never blocks acquisition.
2. A refused runner records visible state and RETRIES until the lock frees.
3. A newly-starting process requests a one-shot takeover from a live holder;
   the holder yields and stands by (never double-ticking, never ping-ponging).
4. /api/health reports the refused/degraded state; StartRunResponse carries a
   runner_warning; the run completes once the lock is released (regression).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict

import pytest

fcntl = pytest.importorskip("fcntl", reason="singleton lock uses fcntl.flock (Unix only)")

from test_gateway_runs_list_endpoint import _wait_until, _write_min_bundle


def _make_runner(base_dir: Path, *, poll_s: float = 0.05):
    """A real GatewayRunner over in-memory stores (no runs, no workflows)."""
    from abstractgateway.runner import GatewayRunner, GatewayRunnerConfig
    from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore

    class _Host:
        run_store = InMemoryRunStore()
        ledger_store = InMemoryLedgerStore()
        artifact_store = None

        def runtime_and_workflow_for_run(self, run_id: str):  # pragma: no cover - no runs in these tests
            raise KeyError(run_id)

    return GatewayRunner(base_dir=base_dir, host=_Host(), config=GatewayRunnerConfig(poll_interval_s=poll_s))


# ---------------------------------------------------------------------------
# 1. flock ground truth
# ---------------------------------------------------------------------------


@pytest.mark.basic
def test_flock_autoreleases_when_holder_process_dies(tmp_path: Path) -> None:
    """The design rests on this: a DEAD holder's flock is already free.

    Therefore 'stale lock' handling reduces to (a) retrying acquisition and
    (b) a cooperative takeover for a LIVE wrong holder — no pid-file stealing.
    """
    lock_path = tmp_path / "gateway_runner.lock"
    lock_path.touch()

    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, sys, time\n"
                f"fh = open({str(lock_path)!r}, 'a')\n"
                "fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                "print('locked', flush=True)\n"
                "time.sleep(60)\n"
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "locked"

        fh = lock_path.open("a")
        with pytest.raises(BlockingIOError):
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=5.0)

        deadline = time.time() + 5.0
        acquired = False
        while time.time() < deadline:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                time.sleep(0.01)
        assert acquired, "flock was not released by the kernel after the holder was SIGKILLed"
        fh.close()
    finally:
        if child.poll() is None:
            child.kill()


# ---------------------------------------------------------------------------
# 2. refused runner: visible state + retry-until-acquired
# ---------------------------------------------------------------------------


def test_refused_runner_records_state_and_acquires_after_holder_stops(tmp_path: Path) -> None:
    holder = _make_runner(tmp_path)
    contender = _make_runner(tmp_path)
    try:
        holder.start()
        _wait_until(lambda: holder.runner_status()["active"], timeout_s=5.0)

        contender.start()
        _wait_until(lambda: contender.runner_status()["lock_refused"], timeout_s=5.0)

        st = contender.runner_status()
        assert st["active"] is False
        assert st["lock_refused"] is True
        assert st["lock_holder_pid"] == os.getpid()  # holder runs in this same process
        # The holder heartbeats, so the contender must classify this as a live
        # peer (legit multi-worker posture), NOT a degraded gateway...
        _wait_until(lambda: contender.runner_status()["status"] == "standby_peer_active", timeout_s=5.0)
        # ...and must NOT raise a false alarm on run-start.
        assert contender.inactive_warning() is None
        # Same-pid holder (in-process peer) must not trigger a takeover request.
        assert contender.runner_status()["takeover_requested_at"] is None

        # Holder stops -> flock freed -> contender's retry loop must win the
        # lock WITHOUT any new start() call (the one-shot-acquire bug).
        holder.stop()
        _wait_until(lambda: contender.runner_status()["active"], timeout_s=5.0)
        st2 = contender.runner_status()
        assert st2["status"] == "active"
        assert st2["lock_refused"] is False
    finally:
        contender.stop()
        holder.stop()


# ---------------------------------------------------------------------------
# 3. live wrong holder: cooperative takeover across real processes
# ---------------------------------------------------------------------------


_CHILD_HOLDER_SCRIPT = """
import sys, time
from pathlib import Path

base_dir = Path(sys.argv[1])
ready_file = Path(sys.argv[2])
yielded_file = Path(sys.argv[3])

from abstractgateway.runner import GatewayRunner, GatewayRunnerConfig
from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore

class _Host:
    run_store = InMemoryRunStore()
    ledger_store = InMemoryLedgerStore()
    artifact_store = None
    def runtime_and_workflow_for_run(self, run_id):
        raise KeyError(run_id)

runner = GatewayRunner(base_dir=base_dir, host=_Host(), config=GatewayRunnerConfig(poll_interval_s=0.05))
runner.start()

deadline = time.time() + 10.0
while time.time() < deadline:
    st = runner.runner_status()
    if st["active"] and not ready_file.exists():
        ready_file.write_text("active")
    if st["yielded_to_pid"] and not yielded_file.exists():
        yielded_file.write_text(str(st["yielded_to_pid"]))
    time.sleep(0.05)
"""


@pytest.mark.integration
def test_new_process_takes_over_from_live_holder(tmp_path: Path) -> None:
    """The incident shape: a LIVE orphaned gateway holds the lock.

    The new process must request takeover once; the orphan must yield (not
    die) and stand by; the new process must end up the one and only ticker.
    """
    base_dir = tmp_path / "runtime"
    base_dir.mkdir()
    ready_file = tmp_path / "holder_ready"
    yielded_file = tmp_path / "holder_yielded"
    script = tmp_path / "holder.py"
    script.write_text(_CHILD_HOLDER_SCRIPT, encoding="utf-8")

    child = subprocess.Popen(
        [sys.executable, str(script), str(base_dir), str(ready_file), str(yielded_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    contender = None
    try:
        _wait_until(ready_file.exists, timeout_s=15.0)

        contender = _make_runner(base_dir)
        contender.start()

        # New process wins the lock from the live holder via the takeover handshake.
        _wait_until(lambda: contender.runner_status()["active"], timeout_s=15.0)
        st = contender.runner_status()
        assert st["status"] == "active"
        assert st["takeover_requested_at"] is not None

        # The old holder yielded (recorded our pid) and is STILL ALIVE in standby.
        _wait_until(yielded_file.exists, timeout_s=5.0)
        assert yielded_file.read_text().strip() == str(os.getpid())
        assert child.poll() is None, "holder process must yield, not die"

        # No ping-pong: the yielded holder never re-steals from a live winner.
        time.sleep(1.5)
        assert contender.runner_status()["active"] is True

        # Takeover request file is consumed after acquisition.
        assert not (base_dir / "gateway_runner.takeover").exists()
    finally:
        if contender is not None:
            contender.stop()
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5.0)


# ---------------------------------------------------------------------------
# 4. HTTP surfaces: health + run-start warning + post-release recovery
# ---------------------------------------------------------------------------


def _configure_gateway_env(monkeypatch: pytest.MonkeyPatch, *, runtime_dir: Path, bundles_dir: Path, token: str) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")
    # Tight heartbeat-staleness so the "nobody ticks" classification settles fast.
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER_LOCK_STALE_S", "0.4")


def _runner_block(health_body: Dict[str, Any]) -> Dict[str, Any]:
    runners = (health_body.get("runner") or {}).get("runners") or []
    assert runners, f"health body has no runner statuses: {health_body}"
    return runners[0]


def test_health_and_run_start_report_locked_out_runner_then_recover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end regression for the incident:

    lock held elsewhere (holder pid unusable/dead, never heartbeats) ->
    health says degraded + run-start warns -> lock released -> runner
    acquires WITHOUT restart -> the previously-accepted run completes.
    """
    from fastapi.testclient import TestClient

    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    runtime_dir.mkdir(parents=True)
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-lock", flow_id="root")

    token = "t"
    _configure_gateway_env(monkeypatch, runtime_dir=runtime_dir, bundles_dir=bundles_dir, token=token)

    # Blocker: hold the flock from this test with unusable holder metadata
    # (a dead pid — the 'lock content is stale/lying' shape). It never
    # heartbeats, so the gateway must classify it as degraded_no_ticker.
    lock_path = runtime_dir / "gateway_runner.lock"
    blocker_fh = lock_path.open("w")
    fcntl.flock(blocker_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    blocker_fh.write("pid=99999999\n")
    blocker_fh.flush()

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    try:
        with TestClient(app) as client:
            # Health must surface the refused runner loudly (was: one log line).
            def _degraded() -> bool:
                body = client.get("/api/health").json()
                if not (body.get("runner") or {}).get("initialized"):
                    return False
                blk = _runner_block(body)
                return body.get("status") == "degraded" and blk.get("status") == "degraded_no_ticker"

            _wait_until(_degraded, timeout_s=10.0)
            blk = _runner_block(client.get("/api/health").json())
            assert blk["lock_refused"] is True
            assert blk["lock_holder_pid"] == 99999999
            assert blk["lock_holder_alive"] is False
            assert blk["enabled"] is True

            # A run accepted in this state must carry the loud warning.
            r = client.post(
                "/api/gateway/runs/start",
                json={"bundle_id": "bundle-lock", "flow_id": "root", "input_data": {}},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            body = r.json()
            run_id = body["run_id"]
            assert isinstance(body.get("runner_warning"), str) and "NOT ticking" in body["runner_warning"]

            # Release the lock: the runner's retry loop must acquire WITHOUT a
            # process restart and the stuck run must then complete.
            fcntl.flock(blocker_fh.fileno(), fcntl.LOCK_UN)
            blocker_fh.close()

            def _healthy_active() -> bool:
                b = client.get("/api/health").json()
                return b.get("status") == "healthy" and _runner_block(b).get("status") == "active"

            _wait_until(_healthy_active, timeout_s=10.0)

            def _completed() -> bool:
                rr = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
                assert rr.status_code == 200, rr.text
                return rr.json().get("status") == "completed"

            _wait_until(_completed, timeout_s=10.0)

            # Healthy state: no more run-start warnings.
            r2 = client.post(
                "/api/gateway/runs/start",
                json={"bundle_id": "bundle-lock", "flow_id": "root", "input_data": {}},
                headers=headers,
            )
            assert r2.status_code == 200, r2.text
            assert r2.json().get("runner_warning") is None
    finally:
        try:
            blocker_fh.close()
        except Exception:
            pass


def test_health_reports_disabled_runner_as_healthy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Split deployment (serve --no-runner) is intentional — never 'degraded'."""
    from fastapi.testclient import TestClient

    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-lock", flow_id="root")

    token = "t"
    _configure_gateway_env(monkeypatch, runtime_dir=runtime_dir, bundles_dir=bundles_dir, token=token)
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")

    from abstractgateway.app import app

    with TestClient(app) as client:
        body = client.get("/api/health").json()
        assert body["status"] == "healthy"
        blk = _runner_block(body)
        assert blk["status"] == "disabled"
        assert blk["enabled"] is False
