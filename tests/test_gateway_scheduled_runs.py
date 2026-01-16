from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import zipfile

import pytest
from fastapi.testclient import TestClient


def _write_simple_end_bundle(*, bundles_dir: Path) -> tuple[str, str]:
    bundles_dir.mkdir(parents=True, exist_ok=True)
    bundle_id = "bundle-simple"
    flow_id = "root"

    flow = {
        "id": flow_id,
        "name": "root",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "n1",
                "type": "on_flow_start",
                "position": {"x": 0.0, "y": 0.0},
                "data": {
                    "nodeType": "on_flow_start",
                    "label": "On Flow Start",
                    "inputs": [],
                    "outputs": [{"id": "exec-out", "label": "", "type": "execution"}],
                },
            },
            {
                "id": "n2",
                "type": "on_flow_end",
                "position": {"x": 240.0, "y": 0.0},
                "data": {"nodeType": "on_flow_end", "label": "On Flow End", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []},
            },
        ],
        "edges": [{"id": "e1", "source": "n1", "sourceHandle": "exec-out", "target": "n2", "targetHandle": "exec-in"}],
        "entryNode": "n1",
    }

    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-01-11T00:00:00+00:00",
        "entrypoints": [{"flow_id": flow_id, "name": "root", "description": "", "interfaces": []}],
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }

    bundle_path = bundles_dir / f"{bundle_id}.flow"
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr(f"flows/{flow_id}.json", json.dumps(flow, indent=2))

    return bundle_id, flow_id


def _wait_until(predicate, *, timeout_s: float = 5.0, poll_s: float = 0.05):
    end = time.time() + timeout_s
    while time.time() < end:
        if predicate():
            return
        time.sleep(poll_s)
    raise AssertionError("timeout waiting for condition")


def test_gateway_scheduled_run_repeats_n_times(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    bundle_id, flow_id = _write_simple_end_bundle(bundles_dir=bundles_dir)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/runs/schedule",
            json={
                "bundle_id": bundle_id,
                "flow_id": flow_id,
                "input_data": {"prompt": "hello"},
                "start_at": "now",
                "interval": "0.1s",
                "repeat_count": 2,
            },
            headers=headers,
        )
        assert r.status_code == 200, r.text
        parent_run_id = r.json()["run_id"]

        def _completed():
            rr = client.get(f"/api/gateway/runs/{parent_run_id}", headers=headers)
            assert rr.status_code == 200, rr.text
            return rr.json().get("status") == "completed"

        _wait_until(_completed, timeout_s=10.0, poll_s=0.1)

        # Validate we spawned 2 children under the scheduled parent (session = parent run id by default).
        runs = client.get(f"/api/gateway/runs?limit=200&session_id={parent_run_id}", headers=headers)
        assert runs.status_code == 200, runs.text
        items = runs.json().get("items") or []
        children = [x for x in items if isinstance(x, dict) and x.get("parent_run_id") == parent_run_id]
        assert len(children) == 2
        assert all(c.get("session_id") == parent_run_id for c in children)


def test_gateway_scheduled_run_can_isolate_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    bundle_id, flow_id = _write_simple_end_bundle(bundles_dir=bundles_dir)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/runs/schedule",
            json={
                "bundle_id": bundle_id,
                "flow_id": flow_id,
                "input_data": {"prompt": "hello"},
                "start_at": "now",
                "interval": "0.1s",
                "repeat_count": 2,
                "share_context": False,
            },
            headers=headers,
        )
        assert r.status_code == 200, r.text
        parent_run_id = r.json()["run_id"]

        def _completed():
            rr = client.get(f"/api/gateway/runs/{parent_run_id}", headers=headers)
            assert rr.status_code == 200, rr.text
            return rr.json().get("status") == "completed"

        _wait_until(_completed, timeout_s=10.0, poll_s=0.1)

        # Children are still grouped via parent_run_id, but each execution uses its own session_id.
        runs = client.get("/api/gateway/runs?limit=200", headers=headers)
        assert runs.status_code == 200, runs.text
        items = runs.json().get("items") or []
        children = [x for x in items if isinstance(x, dict) and x.get("parent_run_id") == parent_run_id]
        assert len(children) == 2

        sessions = [c.get("session_id") for c in children]
        assert all(isinstance(s, str) and s for s in sessions)
        assert len(set(sessions)) == len(sessions)
        assert all(s != parent_run_id for s in sessions)


def test_gateway_scheduled_run_survives_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    bundle_id, flow_id = _write_simple_end_bundle(bundles_dir=bundles_dir)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    start_at = (datetime.now(timezone.utc) + timedelta(seconds=0.8)).isoformat()

    parent_run_id: str
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/runs/schedule",
            json={
                "bundle_id": bundle_id,
                "flow_id": flow_id,
                "input_data": {"prompt": "hello"},
                "start_at": start_at,
            },
            headers=headers,
        )
        assert r.status_code == 200, r.text
        parent_run_id = r.json()["run_id"]

        rr = client.get(f"/api/gateway/runs/{parent_run_id}", headers=headers)
        assert rr.status_code == 200, rr.text
        assert rr.json().get("status") in {"waiting", "running"}

        # Ensure dynamic wrapper flow persisted on disk.
        dyn = runtime_dir / "dynamic_flows"
        assert dyn.exists()
        assert len(list(dyn.glob("*.json"))) == 1

    # Simulate "restart": TestClient lifespan stops the runner and resets the global service.
    # After the start time passes, a new service instance should load the dynamic wrapper
    # and the runner should complete the scheduled run.
    time.sleep(1.0)

    with TestClient(app) as client2:
        def _completed():
            rr2 = client2.get(f"/api/gateway/runs/{parent_run_id}", headers=headers)
            assert rr2.status_code == 200, rr2.text
            return rr2.json().get("status") == "completed"

        _wait_until(_completed, timeout_s=10.0, poll_s=0.1)


def test_gateway_workflow_flow_endpoint_returns_scheduled_wrapper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    bundle_id, flow_id = _write_simple_end_bundle(bundles_dir=bundles_dir)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/runs/schedule",
            json={
                "bundle_id": bundle_id,
                "flow_id": flow_id,
                "input_data": {"prompt": "hello"},
                "start_at": "now",
                "interval": "0.1s",
                "repeat_count": 1,
            },
            headers=headers,
        )
        assert r.status_code == 200, r.text
        parent_run_id = r.json()["run_id"]

        rr = client.get(f"/api/gateway/runs/{parent_run_id}", headers=headers)
        assert rr.status_code == 200, rr.text
        workflow_id = rr.json().get("workflow_id") or ""
        assert isinstance(workflow_id, str) and workflow_id.startswith("scheduled:")

        wf = client.get(f"/api/gateway/workflows/{workflow_id}/flow", headers=headers)
        assert wf.status_code == 200, wf.text
        body = wf.json()
        assert body.get("workflow_id") == workflow_id
        flow = body.get("flow")
        assert isinstance(flow, dict)
        assert flow.get("id") == workflow_id
        assert isinstance(flow.get("nodes"), list) and flow["nodes"]
        assert isinstance(flow.get("edges"), list) and flow["edges"]


def test_gateway_scheduled_run_can_reschedule_interval_in_place(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    bundle_id, flow_id = _write_simple_end_bundle(bundles_dir=bundles_dir)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/runs/schedule",
            json={
                "bundle_id": bundle_id,
                "flow_id": flow_id,
                "input_data": {"prompt": "hello"},
                "start_at": "now",
                "interval": "10s",
                "repeat_count": 2,
            },
            headers=headers,
        )
        assert r.status_code == 200, r.text
        parent_run_id = r.json()["run_id"]

        # Wait for the schedule to reach its interval wait (after the first child execution).
        def _waiting_on_interval():
            rr = client.get(f"/api/gateway/runs/{parent_run_id}", headers=headers)
            assert rr.status_code == 200, rr.text
            body = rr.json()
            waiting = body.get("waiting") or {}
            return (
                body.get("status") == "waiting"
                and body.get("current_node") == "wait_interval"
                and isinstance(waiting, dict)
                and waiting.get("reason") == "until"
            )

        _wait_until(_waiting_on_interval, timeout_s=10.0, poll_s=0.1)

        # Send the update_schedule command to a child run id; the runner resolves the scheduled root.
        runs = client.get(f"/api/gateway/runs?limit=200&session_id={parent_run_id}", headers=headers)
        assert runs.status_code == 200, runs.text
        items = runs.json().get("items") or []
        children = [x for x in items if isinstance(x, dict) and x.get("parent_run_id") == parent_run_id]
        assert children
        child_run_id = children[0].get("run_id")
        assert isinstance(child_run_id, str) and child_run_id

        cmd = client.post(
            "/api/gateway/commands",
            json={
                "command_id": "cmd-resched-1",
                "run_id": child_run_id,
                "type": "update_schedule",
                "payload": {"interval": "0.1s", "apply_immediately": True},
            },
            headers=headers,
        )
        assert cmd.status_code == 200, cmd.text

        def _completed():
            rr = client.get(f"/api/gateway/runs/{parent_run_id}", headers=headers)
            assert rr.status_code == 200, rr.text
            return rr.json().get("status") == "completed"

        _wait_until(_completed, timeout_s=10.0, poll_s=0.1)

        # Verify the persisted wrapper updated.
        dyn = runtime_dir / "dynamic_flows"
        assert dyn.exists()
        files = list(dyn.glob("*.json"))
        assert len(files) == 1
        raw = json.loads(files[0].read_text(encoding="utf-8"))
        nodes = raw.get("nodes") if isinstance(raw, dict) else None
        assert isinstance(nodes, list) and nodes
        wait_interval = next((n for n in nodes if isinstance(n, dict) and n.get("id") == "wait_interval"), None)
        assert isinstance(wait_interval, dict)
        data = wait_interval.get("data")
        assert isinstance(data, dict)
        event_cfg = data.get("eventConfig")
        assert isinstance(event_cfg, dict)
        assert event_cfg.get("schedule") == "0.1s"
