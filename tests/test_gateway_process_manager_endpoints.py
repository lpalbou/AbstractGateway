from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _wait_until(predicate, *, timeout_s: float = 6.0, poll_s: float = 0.05) -> None:
    end = time.time() + float(timeout_s)
    while time.time() < end:
        if predicate():
            return
        time.sleep(float(poll_s))
    raise AssertionError("timeout waiting for condition")


@pytest.mark.basic
def test_process_manager_endpoints_disabled_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir(parents=True, exist_ok=True)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_ENABLE_PROCESS_MANAGER", "0")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        r = client.get("/api/gateway/processes", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("enabled") is False
        assert body.get("processes") == []

        r2 = client.post("/api/gateway/processes/does-not-exist/start", headers=headers)
        assert r2.status_code == 404


@pytest.mark.basic
def test_process_manager_list_is_gracefully_disabled_without_repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir(parents=True, exist_ok=True)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")

    # Enabled, but no repo root configured.
    monkeypatch.setenv("ABSTRACTGATEWAY_ENABLE_PROCESS_MANAGER", "1")
    monkeypatch.delenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", raising=False)
    monkeypatch.delenv("ABSTRACT_TRIAGE_REPO_ROOT", raising=False)

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        # Process control is repo-root scoped -> should be disabled, not a hard error.
        r = client.get("/api/gateway/processes", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("enabled") is False
        assert body.get("processes") == []

        # Env-var management should still work (no repo root required).
        r2 = client.get("/api/gateway/processes/env", headers=headers)
        assert r2.status_code == 200, r2.text
        body2 = r2.json()
        assert body2.get("enabled") is True
        keys = {it.get("key") for it in (body2.get("vars") or []) if isinstance(it, dict)}
        assert "ABSTRACT_EMAIL_FROM" in keys


@pytest.mark.integration
def test_process_manager_can_start_stop_and_tail_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir(parents=True, exist_ok=True)

    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)

    cfg = {
        "processes": [
            {
                "id": "sleeper",
                "label": "Sleeper",
                "kind": "service",
                "cwd": ".",
                "command": [
                    sys.executable,
                    "-c",
                    "import time; print('ready', flush=True); time.sleep(60)",
                ],
            }
        ]
    }
    cfg_path = tmp_path / "proc_manager.json"
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")

    monkeypatch.setenv("ABSTRACTGATEWAY_ENABLE_PROCESS_MANAGER", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(repo_root))
    monkeypatch.setenv("ABSTRACTGATEWAY_PROCESS_MANAGER_CONFIG", str(cfg_path))

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        # Start
        r = client.post("/api/gateway/processes/sleeper/start", headers=headers)
        assert r.status_code == 200, r.text
        st = r.json().get("state") or {}
        assert st.get("status") == "running"
        pid = st.get("pid")
        assert isinstance(pid, int) and pid > 0

        try:
            # Log should include the first line.
            def _log_has_ready() -> bool:
                rr = client.get("/api/gateway/processes/sleeper/logs/tail?max_bytes=8000", headers=headers)
                if rr.status_code != 200:
                    return False
                return "ready" in str(rr.json().get("content") or "")

            _wait_until(_log_has_ready, timeout_s=4.0)

            # Stop
            r2 = client.post("/api/gateway/processes/sleeper/stop", headers=headers)
            assert r2.status_code == 200, r2.text

            def _is_stopped() -> bool:
                rr = client.get("/api/gateway/processes", headers=headers)
                if rr.status_code != 200:
                    return False
                items = rr.json().get("processes") or []
                for it in items:
                    if it.get("id") == "sleeper":
                        return str(it.get("status") or "").lower() == "stopped"
                return False

            _wait_until(_is_stopped, timeout_s=4.0)
        finally:
            # Best-effort cleanup (idempotent).
            client.post("/api/gateway/processes/sleeper/stop", headers=headers)
