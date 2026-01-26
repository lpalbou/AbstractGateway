from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _write_bundle(*, bundles_dir: Path, bundle_id: str, flows: dict[str, dict], entrypoint: str) -> tuple[str, str]:
    bundles_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-01-17T00:00:00+00:00",
        "entrypoints": [{"flow_id": entrypoint, "name": "test", "description": "", "interfaces": []}],
        "flows": {fid: f"flows/{fid}.json" for fid in flows.keys()},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }

    bundle_path = bundles_dir / f"{bundle_id}.flow"
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        for fid, flow in flows.items():
            zf.writestr(f"flows/{fid}.json", json.dumps(flow, indent=2))

    return bundle_id, entrypoint


def _wait_until(predicate, *, timeout_s: float = 10.0, poll_s: float = 0.05) -> None:
    end = time.time() + timeout_s
    while time.time() < end:
        if predicate():
            return
        time.sleep(poll_s)
    raise AssertionError("timeout waiting for condition")


@pytest.mark.integration
def test_runner_process_keeps_ticking_while_api_restarts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"

    token = "t"

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_STORE_BACKEND", "file")
    # Avoid slow/blocked network calls during startup (embeddings are not needed for this test).
    monkeypatch.setenv("ABSTRACTGATEWAY_EMBEDDING_PROVIDER", "disabled")

    # Bundle: a durable WAIT_UNTIL that should complete even if the API process restarts.
    flow_id = "root"
    flow = {
        "id": flow_id,
        "name": "split-api-runner",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "start",
                "type": "on_flow_start",
                "position": {"x": 0.0, "y": 0.0},
                "data": {"nodeType": "on_flow_start", "label": "On Flow Start", "inputs": [], "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]},
            },
            {
                "id": "wait",
                "type": "wait_until",
                "position": {"x": 220.0, "y": 0.0},
                "data": {
                    "nodeType": "wait_until",
                    "label": "Wait Until",
                    "inputs": [{"id": "exec-in", "label": "", "type": "execution"}, {"id": "duration", "label": "duration", "type": "number"}],
                    "outputs": [{"id": "exec-out", "label": "", "type": "execution"}, {"id": "result", "label": "result", "type": "string"}],
                    "pinDefaults": {"duration": 0.8},
                    "effectConfig": {"durationType": "seconds"},
                },
            },
            {
                "id": "end",
                "type": "on_flow_end",
                "position": {"x": 440.0, "y": 0.0},
                "data": {"nodeType": "on_flow_end", "label": "On Flow End", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []},
            },
        ],
        "edges": [
            {"id": "e1", "source": "start", "sourceHandle": "exec-out", "target": "wait", "targetHandle": "exec-in"},
            {"id": "e2", "source": "wait", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"},
        ],
        "entryNode": "start",
    }

    bundle_id, _entry = _write_bundle(bundles_dir=bundles_dir, bundle_id="bundle-split", flows={flow_id: flow}, entrypoint=flow_id)

    # Start runner as a separate OS process (simulates split deployment).
    runner_env = os.environ.copy()
    runner_env["ABSTRACTGATEWAY_RUNNER"] = "1"

    proc = subprocess.Popen(
        [sys.executable, "-m", "abstractgateway.cli", "runner"],
        env=runner_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        lock_path = runtime_dir / "gateway_runner.lock"
        # Runner startup can involve VisualFlow compilation + imports; allow a wider window so this
        # integration test is not flaky on slower machines.
        start = time.time()
        while time.time() - start < 15.0:
            if lock_path.exists():
                break
            rc = proc.poll()
            if rc is not None:
                out = ""
                try:
                    out = proc.stdout.read() if proc.stdout is not None else ""
                except Exception:
                    out = ""
                raise AssertionError(f"runner exited before acquiring lock (rc={rc})\n{out}")
            time.sleep(0.05)
        else:
            raise AssertionError("timeout waiting for runner lock file")

        # API mode: runner disabled for this process (simulates API-only container).
        monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
        from abstractgateway.app import app

        headers = {"Authorization": f"Bearer {token}"}

        with TestClient(app) as client:
            r1 = client.post(
                "/api/gateway/runs/start",
                json={"bundle_id": bundle_id, "flow_id": flow_id, "input_data": {}},
                headers=headers,
            )
            assert r1.status_code == 200, r1.text
            run_id = r1.json()["run_id"]

        # "API restart": new client context while runner keeps ticking in the other process.
        time.sleep(1.2)
        with TestClient(app) as client2:
            def _completed() -> bool:
                rr = client2.get(f"/api/gateway/runs/{run_id}", headers=headers)
                assert rr.status_code == 200, rr.text
                body = rr.json()
                return body.get("status") == "completed" and not body.get("error")

            _wait_until(_completed, timeout_s=10.0, poll_s=0.05)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except Exception:
            proc.kill()
