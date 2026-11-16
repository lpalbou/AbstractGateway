"""A run's live streams say when the gateway is paused (`event: host`).

2026-11-16: with the gateway paused, AbstractCode showed "working" and
"Thinking" for 25 minutes on a run that could not take a step. The ledger
stream now sends `event: host` (`{paused, ...}`) when it opens while paused
and again on every change, so a client can show "paused" and go back to
"working" on resume.
"""

from __future__ import annotations

import datetime
import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from abstractgateway import host_control


@pytest.fixture(autouse=True)
def _reset_host_control():
    host_control._reset_for_tests()
    yield
    host_control._reset_for_tests()


def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_STORE_BACKEND", "file")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    (tmp_path / "flows").mkdir(parents=True, exist_ok=True)
    return {"Authorization": "Bearer t"}


def _run(run_id: str, status):
    from abstractruntime import RunState

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return RunState(
        run_id=run_id, workflow_id="test", status=status, current_node="n", vars={"context": {"task": "", "messages": []}},
        waiting=None, output=None, error=None, created_at=now_iso, updated_at=now_iso, actor_id=None, session_id="s", parent_run_id=None,
    )


def _events(lines):
    """(event, data) pairs from SSE lines."""
    name = None
    for line in lines:
        if line.startswith("event: "):
            name = line[len("event: "):]
        elif line.startswith("data: ") and name is not None:
            yield name, json.loads(line[len("data: "):])
            name = None


def test_the_ledger_stream_says_paused_then_resumed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    headers = _env(tmp_path, monkeypatch)
    from abstractgateway.app import app
    from abstractgateway.service import get_gateway_service
    from abstractruntime import RunStatus

    with TestClient(app) as client:
        svc = get_gateway_service()
        svc.host.run_store.save(_run("run_paused", RunStatus.RUNNING))
        host_control.pause(by="test")
        # This TestClient hands a stream back only once it ends: resume, then
        # finish the run, from timers while the stream is open.
        resume = threading.Timer(0.6, lambda: host_control.resume(by="test"))
        finish = threading.Timer(1.4, lambda: svc.host.run_store.save(_run("run_paused", RunStatus.COMPLETED)))
        resume.start(); finish.start()
        try:
            resp = client.get("/api/gateway/runs/run_paused/ledger/stream?after=0&heartbeat_s=0.2", headers=headers)
        finally:
            resume.cancel(); finish.cancel()
        assert resp.status_code == 200
        events = list(_events(resp.text.splitlines()))
    assert [(name, data.get("paused")) for name, data in events] == [("host", True), ("host", False), ("done", None)]
    assert events[0][1]["paused_by"] == "test"


def test_no_host_event_while_the_gateway_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    headers = _env(tmp_path, monkeypatch)
    from abstractgateway.app import app
    from abstractgateway.service import get_gateway_service
    from abstractruntime import RunStatus

    with TestClient(app) as client:
        get_gateway_service().host.run_store.save(_run("run_done", RunStatus.COMPLETED))
        resp = client.get("/api/gateway/runs/run_done/ledger/stream?after=0&heartbeat_s=0.2", headers=headers)
        assert resp.status_code == 200
        assert "event: host" not in resp.text and "event: done" in resp.text


def test_the_view_stream_says_paused_then_resumed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Assistant follows `/runs/{id}/view/stream`: the same `event: host` rule."""
    headers = _env(tmp_path, monkeypatch)
    from abstractgateway.app import app
    from abstractgateway.service import get_gateway_service
    from abstractruntime import RunStatus

    with TestClient(app) as client:
        svc = get_gateway_service()
        svc.host.run_store.save(_run("run_view", RunStatus.RUNNING))
        host_control.pause(by="test")
        resume = threading.Timer(0.6, lambda: host_control.resume(by="test"))
        finish = threading.Timer(1.4, lambda: svc.host.run_store.save(_run("run_view", RunStatus.COMPLETED)))
        resume.start(); finish.start()
        try:
            resp = client.get("/api/gateway/runs/run_view/view/stream?heartbeat_s=0.2", headers=headers)
        finally:
            resume.cancel(); finish.cancel()
        assert resp.status_code == 200, resp.text
        names = [(name, data.get("paused")) for name, data in _events(resp.text.splitlines()) if name in {"host", "done"}]
    assert names == [("host", True), ("host", False), ("done", None)]
