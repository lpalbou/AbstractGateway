from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.mark.basic
def test_process_env_endpoints_disabled_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        r = client.get("/api/gateway/processes/env", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("enabled") is False
        assert body.get("vars") == []

        r2 = client.post("/api/gateway/processes/env", headers=headers, json={"set": {"ABSTRACT_EMAIL_FROM": "x"}})
        # process manager is disabled => 404
        assert r2.status_code == 404, r2.text


@pytest.mark.integration
def test_process_env_endpoints_write_only_and_allowlisted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir(parents=True, exist_ok=True)

    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")

    monkeypatch.setenv("ABSTRACTGATEWAY_ENABLE_PROCESS_MANAGER", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(repo_root))

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        # Initial list includes allowlisted keys.
        r0 = client.get("/api/gateway/processes/env", headers=headers)
        assert r0.status_code == 200, r0.text
        body0 = r0.json()
        assert body0.get("enabled") is True
        keys = {it.get("key") for it in (body0.get("vars") or []) if isinstance(it, dict)}
        assert "ABSTRACT_EMAIL_FROM" in keys

        # Set a value: response must not contain it.
        secret_value = "secret@example.com"
        r1 = client.post("/api/gateway/processes/env", headers=headers, json={"set": {"ABSTRACT_EMAIL_FROM": secret_value}})
        assert r1.status_code == 200, r1.text
        body1 = r1.json()
        dumped = json.dumps(body1)
        assert secret_value not in dumped

        # Verify source updated.
        items1 = [it for it in (body1.get("vars") or []) if isinstance(it, dict) and it.get("key") == "ABSTRACT_EMAIL_FROM"]
        assert items1 and items1[0].get("source") == "override"

        # Persisted on disk (gateway host store).
        path = runtime_dir / "process_manager" / "env_overrides.json"
        assert path.exists()
        obj = json.loads(path.read_text(encoding="utf-8"))
        v = obj.get("vars", {}).get("ABSTRACT_EMAIL_FROM", {})
        assert v.get("enabled") is True
        assert v.get("value") == secret_value

        # Unset clears stored value.
        r2 = client.post("/api/gateway/processes/env", headers=headers, json={"unset": ["ABSTRACT_EMAIL_FROM"]})
        assert r2.status_code == 200, r2.text
        body2 = r2.json()
        items2 = [it for it in (body2.get("vars") or []) if isinstance(it, dict) and it.get("key") == "ABSTRACT_EMAIL_FROM"]
        assert items2 and items2[0].get("source") == "unset"

        obj2 = json.loads(path.read_text(encoding="utf-8"))
        v2 = obj2.get("vars", {}).get("ABSTRACT_EMAIL_FROM", {})
        assert v2.get("enabled") is False
        assert v2.get("value") == ""

        # Disallowed keys rejected.
        r3 = client.post("/api/gateway/processes/env", headers=headers, json={"set": {"PATH": "/tmp"}})
        assert r3.status_code == 400, r3.text

