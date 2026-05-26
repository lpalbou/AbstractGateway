from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict[str, str]]:
    token = "t"
    flows = tmp_path / "flows"
    flows.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")

    from abstractgateway.app import app

    return TestClient(app), {"Authorization": f"Bearer {token}"}


def test_visualflow_code_simulation_runs_runtime_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.post(
            "/api/gateway/visualflows/code/simulate",
            headers=headers,
            json={
                "code": "def transform(_input):\n    return {'x': _input.get('x') + 1}\n",
                "input": {"x": 2},
                "function_name": "transform",
                "permissions": "sandbox",
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["success"] is True
    assert body["output"] == {"x": 3}
    assert body["execution"]["duration_ms"] >= 0
    assert body["execution"]["cpu_time_ms"] >= 0
    assert body["execution"]["permissions"] == "sandbox"
    assert body["error"] is None
    assert body["diagnostics"]["source"] == "abstractruntime.code_executor"
    assert body["diagnostics"]["requested_mode"] == "sandbox"
    assert body["diagnostics"]["effective_mode"] == "sandbox"
    assert body["diagnostics"]["allowed"] is True


def test_visualflow_code_simulation_reports_sandbox_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.post(
            "/api/gateway/visualflows/code/simulate",
            headers=headers,
            json={
                "code": "import os\n\ndef transform(_input):\n    return os.getcwd()\n",
                "input": {},
                "function_name": "transform",
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["success"] is False
    assert body["output"] is None
    assert "Imports are not allowed" in body["error"]
    assert body["execution"]["permissions"] == "sandbox"
    assert body["diagnostics"]["phase"] == "compile"


def test_visualflow_code_simulation_rejects_full_access_without_host_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ABSTRACTRUNTIME_CODE_FULL_ACCESS", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_CODE_FULL_ACCESS", raising=False)
    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.post(
            "/api/gateway/visualflows/code/simulate",
            headers=headers,
            json={
                "code": "import os\n\ndef transform(_input):\n    return os.getcwd()\n",
                "input": {},
                "function_name": "transform",
                "permissions": "full_access",
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["success"] is False
    assert body["output"] is None
    assert "full_access code execution is disabled" in body["error"]
    assert body["execution"]["permissions"] == "full_access"
    assert body["diagnostics"]["phase"] == "compile"
    assert body["diagnostics"]["requested_mode"] == "full_access"
    assert body["diagnostics"]["effective_mode"] == "full_access"
    assert body["diagnostics"]["allowed"] is False


def test_visualflow_code_simulation_runs_full_access_when_host_policy_enables_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ABSTRACTRUNTIME_CODE_FULL_ACCESS", "1")
    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.post(
            "/api/gateway/visualflows/code/simulate",
            headers=headers,
            json={
                "code": "import os\n\ndef transform(_input):\n    return {'cwd': os.getcwd(), 'x': _input.get('x')}\n",
                "input": {"x": 3},
                "function_name": "transform",
                "permissions": "full_access",
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["success"] is True
    assert isinstance(body["output"]["cwd"], str)
    assert body["output"]["x"] == 3
    assert body["execution"]["permissions"] == "full_access"
    assert body["diagnostics"]["requested_mode"] == "full_access"
    assert body["diagnostics"]["effective_mode"] == "full_access"
    assert body["diagnostics"]["allowed"] is True
