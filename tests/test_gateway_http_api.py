from __future__ import annotations

import json
import time
from pathlib import Path
import zipfile

import pytest
from fastapi.testclient import TestClient


def _write_test_bundle(*, bundles_dir: Path) -> tuple[str, str]:
    """Write a minimal `.flow` bundle that asks the user and waits (VisualFlow-based)."""
    bundles_dir.mkdir(parents=True, exist_ok=True)
    bundle_id = "bundle-test"
    flow_id = "a803f4bd"

    # Optional: include the VisualFlow JSON source (useful for UIs).
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
    flow_bytes = json.dumps(flow, indent=2).encode("utf-8")

    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-01-08T00:00:00+00:00",
        "entrypoints": [{"flow_id": flow_id, "name": "test", "description": "", "interfaces": []}],
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }
    bundle_path = bundles_dir / f"{bundle_id}.flow"
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr(f"flows/{flow_id}.json", flow_bytes)

    return bundle_id, flow_id


def _write_subflow_bundle(*, bundles_dir: Path) -> tuple[str, str, str]:
    """Write a `.flow` bundle with a root workflow that starts a subflow.

    This specifically validates bundle-id namespacing + subflow reference rewriting.
    """
    bundles_dir.mkdir(parents=True, exist_ok=True)
    bundle_id = "bundle-subflow"
    root_id = "root"
    child_id = "child"

    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-01-08T00:00:00+00:00",
        "entrypoints": [{"flow_id": root_id, "name": "root", "description": "", "interfaces": []}],
        "flows": {
            root_id: f"flows/{root_id}.json",
            child_id: f"flows/{child_id}.json",
        },
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }

    root_flow = {
        "id": root_id,
        "name": "root",
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
                "type": "subflow",
                "position": {"x": 288.0, "y": 224.0},
                "data": {
                    "nodeType": "subflow",
                    "label": "Subflow",
                    "subflowId": child_id,  # intentionally un-namespaced; host must rewrite
                    "inputs": [{"id": "exec-in", "label": "", "type": "execution"}],
                    "outputs": [{"id": "exec-out", "label": "", "type": "execution"}],
                },
            },
            {
                "id": "node-3",
                "type": "on_flow_end",
                "position": {"x": 512.0, "y": 224.0},
                "data": {"nodeType": "on_flow_end", "label": "On Flow End", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []},
            },
        ],
        "edges": [
            {"id": "edge-1", "source": "node-1", "sourceHandle": "exec-out", "target": "node-2", "targetHandle": "exec-in"},
            {"id": "edge-2", "source": "node-2", "sourceHandle": "exec-out", "target": "node-3", "targetHandle": "exec-in"},
        ],
        "entryNode": "node-1",
    }

    child_flow = {
        "id": child_id,
        "name": "child",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "c1",
                "type": "on_flow_start",
                "position": {"x": 32.0, "y": 224.0},
                "data": {"nodeType": "on_flow_start", "label": "On Flow Start", "inputs": [], "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]},
            },
            {
                "id": "c2",
                "type": "on_flow_end",
                "position": {"x": 288.0, "y": 224.0},
                "data": {"nodeType": "on_flow_end", "label": "On Flow End", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []},
            },
        ],
        "edges": [{"id": "edge-c", "source": "c1", "sourceHandle": "exec-out", "target": "c2", "targetHandle": "exec-in"}],
        "entryNode": "c1",
    }

    bundle_path = bundles_dir / f"{bundle_id}.flow"
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr(f"flows/{root_id}.json", json.dumps(root_flow, indent=2))
        zf.writestr(f"flows/{child_id}.json", json.dumps(child_flow, indent=2))

    return bundle_id, root_id, child_id


def _wait_until(predicate, *, timeout_s: float = 5.0, poll_s: float = 0.05):
    end = time.time() + timeout_s
    while time.time() < end:
        if predicate():
            return
        time.sleep(poll_s)
    raise AssertionError("timeout waiting for condition")


def test_gateway_start_wait_resume_completes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    bundle_id, flow_id = _write_test_bundle(bundles_dir=bundles_dir)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    # Contract: bundle mode must not require importing AbstractFlow at runtime.
    import sys
    assert "abstractflow" not in sys.modules

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": bundle_id, "flow_id": flow_id, "input_data": {}},
            headers=headers,
        )
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


def test_gateway_bundle_namespaces_subflows_and_rewrites_references(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    bundle_id, root_id, _child_id = _write_subflow_bundle(bundles_dir=bundles_dir)    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    # Contract: bundle mode must not require importing AbstractFlow at runtime.
    import sys
    assert "abstractflow" not in sys.modules

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": bundle_id, "flow_id": root_id, "input_data": {}},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]

        def _is_completed():
            rr = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
            assert rr.status_code == 200, rr.text
            return rr.json().get("status") == "completed"

        _wait_until(_is_completed, timeout_s=10.0, poll_s=0.1)

        ledger = client.get(f"/api/gateway/runs/{run_id}/ledger?after=0&limit=200", headers=headers)
        assert ledger.status_code == 200, ledger.text
        items = ledger.json().get("items") or []

        # Ensure the START_SUBWORKFLOW effect used the namespaced workflow id (host rewrite).
        started = [
            i
            for i in items
            if isinstance(i, dict)
            and isinstance(i.get("effect"), dict)
            and i["effect"].get("type") == "start_subworkflow"
        ]
        assert started, "Expected a start_subworkflow effect in the ledger"
        payload = started[0]["effect"].get("payload") if isinstance(started[0].get("effect"), dict) else {}
        assert isinstance(payload, dict)
        assert payload.get("workflow_id") == f"{bundle_id}:child"
