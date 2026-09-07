"""Host pause / resume (system tray + console, 2026-09-05).

Pins:
- `host_control` is process-wide, persisted, and nudges runners on resume.
- A paused runner schedules NOTHING (command lane included) but still applies
  commands; a resume drains what queued.
- `_tick_run` passes the host step gate to a runtime that accepts it and
  omits it for one that does not (older runtimes pause at tick boundaries).
- `runner_status()` says `paused` (status "paused", never degraded);
  `/api/health` carries `paused: true` with status "healthy".
- HTTP: reads are user-level, pause/resume are admin-only, restart/shutdown
  answer 409 with a reason when this process cannot do it, and 200 + a
  `should_exit` flip when a server handle is registered.
- The ephemeral tray token is admin from a loopback peer and nothing from
  anywhere else, and it is never cached.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from abstractruntime.core.models import RunStatus

from abstractgateway import host_control
from abstractgateway.runner import GatewayRunner, GatewayRunnerConfig, _runtime_supports_step_gate

pytestmark = pytest.mark.basic


@pytest.fixture(autouse=True)
def _reset_host_control():
    host_control._reset_for_tests()
    yield
    host_control._reset_for_tests()


# ------------------------------------------------------------- host_control


def test_pause_state_persists_and_reloads(tmp_path: Path) -> None:
    host_control.configure(tmp_path)
    assert host_control.is_paused() is False
    assert host_control.step_gate() is True

    snap = host_control.pause(by="default/admin", reason="need the GPU")
    assert snap["paused"] is True and snap["paused_by"] == "default/admin" and snap["reason"] == "need the GPU"
    assert host_control.step_gate() is False
    assert host_control.pause_file_path(tmp_path).exists()

    # A fresh process (module reset) reloads the persisted pause.
    host_control._reset_for_tests()
    assert host_control.is_paused() is False
    loaded = host_control.configure(tmp_path)
    assert loaded["paused"] is True and loaded["paused_by"] == "default/admin"

    host_control.resume(by="default/admin")
    assert host_control.is_paused() is False
    assert not host_control.pause_file_path(tmp_path).exists()


def test_resume_nudges_registered_listeners_and_pause_is_idempotent(tmp_path: Path) -> None:
    host_control.configure(tmp_path)
    hits: List[str] = []

    class _R:
        def nudge(self) -> None:
            hits.append("nudged")

    r = _R()
    host_control.add_resume_listener(r.nudge)
    first = host_control.pause(by="a")
    second = host_control.pause(by="b")  # keeps the first stamp
    assert second["paused_at"] == first["paused_at"] and second["paused_by"] == "a"
    host_control.resume(by="a")
    assert hits == ["nudged"]
    assert host_control.paused_warning() is None
    host_control.pause(by="a")
    assert "paused" in str(host_control.paused_warning())


# ------------------------------------------------------------------ runner


@dataclass
class _Run:
    run_id: str
    actor_id: str = "gateway"
    status: Any = RunStatus.RUNNING
    waiting: Any = None
    created_at: str = "2020-01-01T00:00:00+00:00"
    vars: Dict[str, Any] = field(default_factory=dict)


class _Store:
    def __init__(self, runs: List[_Run]) -> None:
        self._runs = runs
        self._by_id = {r.run_id: r for r in runs}

    def list_runs(self, *, status=None, wait_reason=None, workflow_id=None, limit=100):
        return [r for r in self._runs if status is None or r.status == status][: int(limit)]

    def list_due_wait_until(self, *, now_iso=None, limit=100):
        return []

    def load(self, run_id: str) -> Optional[_Run]:
        return self._by_id.get(str(run_id))


class _GatedRuntime:
    """Accepts tick(step_gate=...) like the real runtime and records it."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.run_store = None

    def tick(self, *, workflow, run_id, max_steps=100, step_gate=None):
        self.calls.append({"run_id": run_id, "step_gate": step_gate})
        return _Run(run_id=run_id, status=RunStatus.WAITING)


class _LegacyRuntime:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.run_store = None

    def tick(self, *, workflow, run_id, max_steps=100):
        self.calls.append({"run_id": run_id})
        return _Run(run_id=run_id, status=RunStatus.WAITING)


class _Host:
    def __init__(self, runtime: Any, store: Any) -> None:
        self.runtime = runtime
        self.run_store = store

    def runtime_and_workflow_for_run(self, run_id: str):
        return self.runtime, object()


def _runner(runtime: Any, runs: List[_Run]) -> tuple[GatewayRunner, _Host]:
    store = _Store(runs)
    host = _Host(runtime, store)
    cfg = GatewayRunnerConfig(tick_workers=1, command_tick_workers=1, run_scan_limit=10)
    runner = GatewayRunner(base_dir=Path(tempfile.mkdtemp()), host=host, config=cfg, enable=False)
    runner._scan_gate_base = None  # non-file store double: scans stay unconditional
    return runner, host


def test_paused_runner_schedules_nothing_and_keeps_priority_ids_queued() -> None:
    rt = _GatedRuntime()
    runner, _ = _runner(rt, [_Run("r1")])
    submitted: List[str] = []
    runner._submit_tick = lambda run_id, priority=False: submitted.append(run_id)  # type: ignore[method-assign]
    runner._priority_tick_ids.add("r1")

    host_control.pause(by="test")
    runner._schedule_ticks()
    assert submitted == []
    assert "r1" in runner._priority_tick_ids  # still queued, not lost

    host_control.resume(by="test")
    runner._schedule_ticks()
    assert submitted and submitted[0] == "r1"


def test_tick_run_passes_the_host_step_gate_only_to_runtimes_that_accept_it() -> None:
    gated = _GatedRuntime()
    runner, _ = _runner(gated, [_Run("r1")])
    runner._tick_run("r1")
    assert gated.calls and gated.calls[0]["step_gate"] is host_control.step_gate

    legacy = _LegacyRuntime()
    runner2, _ = _runner(legacy, [_Run("r2")])
    runner2._tick_run("r2")
    assert legacy.calls == [{"run_id": "r2"}]

    from abstractruntime import Runtime

    assert _runtime_supports_step_gate(Runtime(run_store=object(), ledger_store=object())) is True  # type: ignore[arg-type]


def test_runner_status_reports_paused_without_degrading() -> None:
    rt = _GatedRuntime()
    runner, _ = _runner(rt, [])
    runner._enable = True
    with runner._state_lock:
        runner._lock_held = True
        runner._loop_running = True
    assert runner.runner_status()["status"] == "active"
    host_control.pause(by="default/admin", reason="quiet hours")
    st = runner.runner_status()
    assert st["status"] == "paused"
    assert st["paused"] is True and st["paused_by"] == "default/admin" and st["pause_reason"] == "quiet hours"
    host_control.resume(by="default/admin")
    assert runner.runner_status()["status"] == "active"


# -------------------------------------------------------------------- HTTP


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, client: Optional[tuple] = None) -> tuple[TestClient, dict]:
    token = "operator-token-for-tests"
    (tmp_path / "flows").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    from abstractgateway.app import app

    kwargs: Dict[str, Any] = {}
    if client is not None:
        kwargs["client"] = client
    return TestClient(app, **kwargs), {"Authorization": f"Bearer {token}"}


def test_pause_resume_routes_and_health_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c, h = _client(tmp_path, monkeypatch)
    with c:
        r = c.get("/api/gateway/host/runner", headers=h)
        assert r.status_code == 200 and r.json()["paused"] is False

        # Anonymous writes are refused before any state changes.
        assert c.post("/api/gateway/host/pause").status_code == 401

        r = c.post("/api/gateway/host/pause", headers=h, json={"reason": "meeting"})
        assert r.status_code == 200
        body = r.json()
        assert body["paused"] is True and body["reason"] == "meeting" and body["paused_by"] == "default/admin"
        assert "inflight_ticks" in body and "capabilities" in body

        health = c.get("/api/health").json()
        assert health["paused"] is True
        assert health["status"] == "healthy"  # a paused gateway is not a sick one

        r = c.post("/api/gateway/host/resume", headers=h)
        assert r.status_code == 200 and r.json()["paused"] is False
        assert "paused" not in c.get("/api/health").json()


def test_restart_and_shutdown_refuse_without_a_server_then_flip_should_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c, h = _client(tmp_path, monkeypatch)
    with c:
        r = c.post("/api/gateway/host/restart", headers=h)
        assert r.status_code == 409 and "abstractgateway serve" in r.json()["detail"]
        assert c.post("/api/gateway/host/shutdown", headers=h).status_code == 409

        class _Server:
            should_exit = False

        srv = _Server()
        host_control.register_server(srv, restartable=False, block_reason="reload mode")
        r = c.post("/api/gateway/host/restart", headers=h)
        assert r.status_code == 409 and "reload mode" in r.json()["detail"]
        assert srv.should_exit is False

        host_control.register_server(srv, restartable=True)
        caps = c.get("/api/gateway/host/runner", headers=h).json()["capabilities"]
        assert caps["restart"] is True and caps["shutdown"] is True
        r = c.post("/api/gateway/host/restart", headers=h, json={"reason": "update"})
        assert r.status_code == 200 and r.json()["restart"] is True
        assert srv.should_exit is True
        assert host_control.restart_requested() is True

        # Non-admins never reach these (route policy row).
        assert c.post("/api/gateway/host/restart").status_code == 401


def test_relaunch_command_uses_module_entry_and_current_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys as _sys

    cmd = host_control.build_relaunch_command(argv=["serve", "--port", "8080"], executable="/py")
    assert cmd == ["/py", "-m", "abstractgateway", "serve", "--port", "8080"]
    # The CLI hands over the parsed argv; the command is rebuilt from the
    # running interpreter (validated) + `-m abstractgateway`.
    host_control.register_server(object(), restartable=True, relaunch_argv=["serve", "--no-runner"])
    assert host_control.build_relaunch_command() == [_sys.executable, "-m", "abstractgateway", "serve", "--no-runner"]
    assert host_control.control_capabilities()["restart"] is True
    # An interpreter that is not a file downgrades restartability with a reason.
    host_control.register_server(object(), restartable=True, relaunch_argv=["serve"], executable="/nonexistent/python")
    caps = host_control.control_capabilities()
    assert caps["restart"] is False and "cannot relaunch" in str(caps["reason"])


def test_relaunch_only_after_a_clean_server_return() -> None:
    class _Server:
        should_exit = False

    host_control.register_server(_Server(), restartable=True, relaunch_argv=["serve"])
    host_control.request_restart(by="test")
    assert host_control.restart_requested() is True
    # A Ctrl-C / exception during the drain clears the request: never a bounce.
    host_control.clear_requests()
    assert host_control.should_relaunch() is False
    # A clean return honours it.
    host_control.request_restart(by="test")
    host_control.mark_clean_exit()
    assert host_control.should_relaunch() is True


def test_restart_and_shutdown_refuse_while_an_update_is_installing() -> None:
    class _Server:
        should_exit = False

    host_control.register_server(_Server(), restartable=True, relaunch_argv=["serve"])
    host_control.set_update_job_probe(lambda: True)
    caps = host_control.control_capabilities()
    assert caps["restart"] is False and caps["shutdown"] is False and caps["update_job_running"] is True
    with pytest.raises(host_control.HostControlError):
        host_control.request_restart(by="test")
    with pytest.raises(host_control.HostControlError):
        host_control.request_shutdown(by="test")
    host_control.set_update_job_probe(lambda: False)
    out = host_control.request_restart(by="test")
    assert out["restart"] is True


def test_pause_file_written_by_another_process_is_picked_up(tmp_path: Path) -> None:
    """Split layout: the API process writes the pause file, the runner
    process (this module instance) must obey it within a stat interval."""
    host_control.configure(tmp_path)
    assert host_control.is_paused() is False
    path = host_control.pause_file_path(tmp_path)
    path.write_text(json.dumps({"paused": True, "paused_at": "2026-09-05T00:00:00+00:00", "paused_by": "api-process", "reason": "split"}), encoding="utf-8")
    import time as _time

    t0 = _time.monotonic() + 10.0  # past the stat interval configure() armed
    assert host_control.maybe_reload(now=t0) is True
    assert host_control.is_paused() is True and host_control.pause_snapshot()["paused_by"] == "api-process"
    # Within the interval nothing is re-stat'ed; past it, a deleted file resumes.
    path.unlink()
    assert host_control.maybe_reload(now=t0 + 0.5) is False
    assert host_control.maybe_reload(now=t0 + 3.0) is True
    assert host_control.is_paused() is False


def test_loopback_matching_accepts_ipv4_mapped_and_aliases() -> None:
    from abstractgateway.security.gateway_security import _is_loopback_ip

    assert _is_loopback_ip("127.0.0.1") and _is_loopback_ip("::1") and _is_loopback_ip("::ffff:127.0.0.1")
    assert _is_loopback_ip("127.0.0.2") and _is_loopback_ip("localhost") and _is_loopback_ip("testclient")
    assert not _is_loopback_ip("10.0.0.9") and not _is_loopback_ip("203.0.113.7") and not _is_loopback_ip("") and not _is_loopback_ip("garbage")


def test_ephemeral_tray_token_is_admin_from_loopback_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.security.gateway_security import (
        ephemeral_loopback_token_valid,
        register_ephemeral_loopback_token,
        revoke_ephemeral_loopback_token,
    )

    token = "tray-ephemeral-token-0123456789"
    register_ephemeral_loopback_token(token, label="desktop-tray")
    try:
        assert ephemeral_loopback_token_valid(token, peer_ip="127.0.0.1") is True
        assert ephemeral_loopback_token_valid(token, peer_ip="10.0.0.9") is False
        assert ephemeral_loopback_token_valid("nope", peer_ip="127.0.0.1") is False

        c, _ = _client(tmp_path, monkeypatch)  # TestClient's peer is the loopback sentinel
        with c:
            r = c.post("/api/gateway/host/pause", headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 200 and r.json()["paused"] is True
            c.post("/api/gateway/host/resume", headers={"Authorization": f"Bearer {token}"})

        remote, _ = _client(tmp_path, monkeypatch, client=("203.0.113.7", 40000))
        with remote:
            r = remote.post("/api/gateway/host/pause", headers={"Authorization": f"Bearer {token}"})
            assert r.status_code == 401
            # ...and the earlier loopback success left nothing in the auth cache.
            assert remote.get("/api/gateway/host/runner", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    finally:
        revoke_ephemeral_loopback_token(token)
    assert ephemeral_loopback_token_valid(token, peer_ip="127.0.0.1") is False
    with pytest.raises(ValueError):
        register_ephemeral_loopback_token("short", label="x")
