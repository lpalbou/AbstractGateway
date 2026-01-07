from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _write_test_flow(*, flows_dir: Path) -> str:
    """Write a minimal VisualFlow that asks the user and waits."""
    flows_dir.mkdir(parents=True, exist_ok=True)
    flow_id = "a803f4bd"
    flow = {
        "id": flow_id,
        "name": "test",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "node-1",
                "type": "on_flow_start",
                "position": {"x": 32.0, "y": 224.0},
                "data": {"nodeType": "on_flow_start", "label": "On Flow Start", "inputs": [], "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]},
            },
            {
                "id": "node-2",
                "type": "ask_user",
                "position": {"x": 288.0, "y": 224.0},
                "data": {
                    "nodeType": "ask_user",
                    "label": "Ask User",
                    "inputs": [
                        {"id": "exec-in", "label": "", "type": "execution"},
                        {"id": "prompt", "label": "prompt", "type": "string"},
                        {"id": "choices", "label": "choices", "type": "array"},
                    ],
                    "outputs": [
                        {"id": "exec-out", "label": "", "type": "execution"},
                        {"id": "response", "label": "response", "type": "string"},
                    ],
                    "pinDefaults": {"prompt": "who are you ?"},
                },
            },
        ],
        "edges": [
            {
                "id": "edge-1",
                "source": "node-1",
                "sourceHandle": "exec-out",
                "target": "node-2",
                "targetHandle": "exec-in",
                "animated": True,
            }
        ],
        "entryNode": "node-1",
    }
    (flows_dir / f"{flow_id}.json").write_text(json.dumps(flow, indent=2), encoding="utf-8")
    return flow_id


def _wait_until(predicate, *, timeout_s: float = 5.0, poll_s: float = 0.05):
    end = time.time() + timeout_s
    while time.time() < end:
        if predicate():
            return
        time.sleep(poll_s)
    raise AssertionError("timeout waiting for condition")


def test_gateway_start_wait_resume_completes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    flows_dir = tmp_path / "flows"
    flow_id = _write_test_flow(flows_dir=flows_dir)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        r = client.post("/api/gateway/runs/start", json={"flow_id": flow_id, "input_data": {}}, headers=headers)
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]

        wait_key_holder: dict[str, str] = {}

        def _has_wait():
            rr = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
            assert rr.status_code == 200, rr.text
            body = rr.json()
            w = body.get("waiting")
            if isinstance(w, dict) and w.get("wait_key"):
                wait_key_holder["k"] = str(w.get("wait_key"))
                return True
            return False

        _wait_until(_has_wait, timeout_s=10.0, poll_s=0.1)
        wait_key = wait_key_holder["k"]

        cmd = client.post(
            "/api/gateway/commands",
            json={
                "command_id": "cmd-1",
                "run_id": run_id,
                "type": "resume",
                "payload": {"wait_key": wait_key, "payload": {"response": "I am Albou"}},
            },
            headers=headers,
        )
        assert cmd.status_code == 200, cmd.text

        def _is_completed():
            rr = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
            assert rr.status_code == 200, rr.text
            return rr.json().get("status") == "completed"

        _wait_until(_is_completed, timeout_s=10.0, poll_s=0.1)

        ledger = client.get(f"/api/gateway/runs/{run_id}/ledger?after=0&limit=200", headers=headers)
        assert ledger.status_code == 200, ledger.text
        items = ledger.json().get("items") or []
        assert any(isinstance(i, dict) and i.get("status") == "completed" for i in items)


