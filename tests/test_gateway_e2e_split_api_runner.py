from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import httpx
import pytest


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_until(predicate, *, timeout_s: float = 20.0, poll_s: float = 0.05) -> None:
    end = time.time() + timeout_s
    while time.time() < end:
        if predicate():
            return
        time.sleep(poll_s)
    raise AssertionError("timeout waiting for condition")


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


@pytest.mark.e2e
def test_gateway_split_api_runner_two_process_restart_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Level C: real runner process + real HTTP server process.

    Enable with:
      ABSTRACT_E2E_GATEWAY_SPLIT=1

    This does not require an external LLM provider; it uses a WAIT_UNTIL-only bundle.
    """
    if os.environ.get("ABSTRACT_E2E_GATEWAY_SPLIT") != "1":
        pytest.skip("Set ABSTRACT_E2E_GATEWAY_SPLIT=1 to run this test.")

    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"

    token = "t"
    port = _pick_free_port()
    base_url = f"http://127.0.0.1:{port}"

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

    flow_id = "root"
    flow = {
        "id": flow_id,
        "name": "e2e-split-api-runner",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "start",
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
                "id": "wait",
                "type": "wait_until",
                "position": {"x": 220.0, "y": 0.0},
                "data": {
                    "nodeType": "wait_until",
                    "label": "Wait Until",
                    "inputs": [
                        {"id": "exec-in", "label": "", "type": "execution"},
                        {"id": "duration", "label": "duration", "type": "number"},
                    ],
                    "outputs": [
                        {"id": "exec-out", "label": "", "type": "execution"},
                        {"id": "result", "label": "result", "type": "string"},
                    ],
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

    bundle_id, _entry = _write_bundle(bundles_dir=bundles_dir, bundle_id="bundle-e2e-split", flows={flow_id: flow}, entrypoint=flow_id)

    # Start the runner as a separate OS process.
    base_env = os.environ.copy()
    runner_env = dict(base_env)
    runner_env["ABSTRACTGATEWAY_RUNNER"] = "1"
    runner = subprocess.Popen(
        [sys.executable, "-m", "abstractgateway.cli", "runner"],
        env=runner_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    api: subprocess.Popen[str] | None = None
    try:
        lock_path = runtime_dir / "gateway_runner.lock"

        def _runner_ready() -> bool:
            if lock_path.exists():
                return True
            if runner.poll() is not None:
                out = ""
                if runner.stdout is not None:
                    try:
                        out = runner.stdout.read()
                    except Exception:
                        out = ""
                raise AssertionError(f"Runner process exited early (code={runner.returncode}).\n{out}")
            return False

        _wait_until(_runner_ready, timeout_s=10.0, poll_s=0.05)

        def _start_api() -> subprocess.Popen[str]:
            api_env = dict(base_env)
            api_env["ABSTRACTGATEWAY_RUNNER"] = "0"
            return subprocess.Popen(
                [sys.executable, "-m", "abstractgateway.cli", "serve", "--no-runner", "--host", "127.0.0.1", "--port", str(port)],
                env=api_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

        api = _start_api()

        client = httpx.Client(base_url=base_url, timeout=5.0)
        try:
            def _healthy() -> bool:
                if api is not None and api.poll() is not None:
                    out = ""
                    if api.stdout is not None:
                        try:
                            out = api.stdout.read()
                        except Exception:
                            out = ""
                    raise AssertionError(f"API process exited early (code={api.returncode}).\n{out}")
                try:
                    return client.get("/api/health").status_code == 200
                except Exception:
                    return False

            _wait_until(_healthy, timeout_s=10.0, poll_s=0.05)

            headers = {"Authorization": f"Bearer {token}"}
            r1 = client.post(
                "/api/gateway/runs/start",
                headers=headers,
                json={"bundle_id": bundle_id, "flow_id": flow_id, "input_data": {}},
            )
            assert r1.status_code == 200, r1.text
            run_id = r1.json()["run_id"]

            # Restart the API process while the runner keeps ticking.
            api.terminate()
            try:
                api.wait(timeout=5.0)
            except Exception:
                api.kill()
            api = _start_api()
            _wait_until(_healthy, timeout_s=10.0, poll_s=0.05)

            def _completed() -> bool:
                rr = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
                assert rr.status_code == 200, rr.text
                body = rr.json()
                return body.get("status") == "completed" and not body.get("error")

            _wait_until(_completed, timeout_s=20.0, poll_s=0.05)
        finally:
            client.close()

    finally:
        if api is not None:
            api.terminate()
            try:
                api.wait(timeout=5.0)
            except Exception:
                api.kill()

        runner.terminate()
        try:
            runner.wait(timeout=5.0)
        except Exception:
            runner.kill()
