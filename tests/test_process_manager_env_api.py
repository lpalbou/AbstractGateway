from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _managed_test_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """The allowlist is empty in the product since the email variables were retired
    (framework backlog 0992); these tests exercise the mechanism with one test key."""
    import abstractgateway.maintenance.process_manager as pm

    spec = pm.ManagedEnvVarSpec(key="ABSTRACT_TEST_MANAGED_VAR", label="test", description="test variable", category="test")
    spec_cfg = pm.ManagedEnvVarSpec(key="ABSTRACT_TEST_MANAGED_PATH", label="test path", description="test path variable", category="test")
    monkeypatch.setattr(pm, "managed_env_var_allowlist", lambda: {spec.key: spec, spec_cfg.key: spec_cfg})


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
    monkeypatch.setenv("ABSTRACTGATEWAY_ENABLE_PROCESS_MANAGER", "0")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        r = client.get("/api/gateway/processes/env", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("enabled") is False
        assert body.get("vars") == []

        r2 = client.post("/api/gateway/processes/env", headers=headers, json={"set": {"ABSTRACT_TEST_MANAGED_VAR": "x"}})
        # process manager is disabled => 404
        assert r2.status_code == 404, r2.text


@pytest.mark.integration
def test_process_env_endpoints_write_only_and_allowlisted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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

    monkeypatch.setenv("ABSTRACTGATEWAY_ENABLE_PROCESS_MANAGER", "1")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        # Initial list includes allowlisted keys.
        r0 = client.get("/api/gateway/processes/env", headers=headers)
        assert r0.status_code == 200, r0.text
        body0 = r0.json()
        assert body0.get("enabled") is True
        keys = {it.get("key") for it in (body0.get("vars") or []) if isinstance(it, dict)}
        assert "ABSTRACT_TEST_MANAGED_VAR" in keys
        assert "ABSTRACT_TEST_MANAGED_PATH" in keys

        # Set a value: response must not contain it.
        secret_value = "secret@example.com"
        config_path = "configs/emails.yaml"
        r1 = client.post(
            "/api/gateway/processes/env",
            headers=headers,
            json={"set": {"ABSTRACT_TEST_MANAGED_VAR": secret_value, "ABSTRACT_TEST_MANAGED_PATH": config_path}},
        )
        assert r1.status_code == 200, r1.text
        body1 = r1.json()
        dumped = json.dumps(body1)
        assert secret_value not in dumped
        assert config_path not in dumped

        # Verify source updated.
        items1 = [it for it in (body1.get("vars") or []) if isinstance(it, dict) and it.get("key") == "ABSTRACT_TEST_MANAGED_VAR"]
        assert items1 and items1[0].get("source") == "override"
        items1b = [
            it for it in (body1.get("vars") or []) if isinstance(it, dict) and it.get("key") == "ABSTRACT_TEST_MANAGED_PATH"
        ]
        assert items1b and items1b[0].get("source") == "override"

        # Persisted on disk (gateway host store).
        path = runtime_dir / "process_manager" / "env_overrides.json"
        assert path.exists()
        obj = json.loads(path.read_text(encoding="utf-8"))
        v = obj.get("vars", {}).get("ABSTRACT_TEST_MANAGED_VAR", {})
        assert v.get("enabled") is True
        assert v.get("value") == secret_value
        v_cfg = obj.get("vars", {}).get("ABSTRACT_TEST_MANAGED_PATH", {})
        assert v_cfg.get("enabled") is True
        assert v_cfg.get("value") == config_path

        # Unset clears stored value.
        r2 = client.post("/api/gateway/processes/env", headers=headers, json={"unset": ["ABSTRACT_TEST_MANAGED_VAR"]})
        assert r2.status_code == 200, r2.text
        body2 = r2.json()
        items2 = [it for it in (body2.get("vars") or []) if isinstance(it, dict) and it.get("key") == "ABSTRACT_TEST_MANAGED_VAR"]
        assert items2 and items2[0].get("source") == "unset"

        obj2 = json.loads(path.read_text(encoding="utf-8"))
        v2 = obj2.get("vars", {}).get("ABSTRACT_TEST_MANAGED_VAR", {})
        assert v2.get("enabled") is False
        assert v2.get("value") == ""

        # Disallowed keys rejected.
        r3 = client.post("/api/gateway/processes/env", headers=headers, json={"set": {"PATH": "/tmp"}})
        assert r3.status_code == 400, r3.text


@pytest.mark.basic
def test_env_overrides_are_applied_on_gateway_startup_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir(parents=True, exist_ok=True)

    # Persist an override before the gateway starts.
    state_dir = runtime_dir / "process_manager"
    state_dir.mkdir(parents=True, exist_ok=True)
    overrides_path = state_dir / "env_overrides.json"
    overrides_path.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": "2026-02-06T00:00:00Z",
                "vars": {"ABSTRACT_TEST_MANAGED_VAR": {"enabled": True, "value": "persisted@example.com", "updated_at": "2026-02-06T00:00:00Z"}},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    # Ensure the host environment doesn't already provide it.
    monkeypatch.delenv("ABSTRACT_TEST_MANAGED_VAR", raising=False)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_ENABLE_PROCESS_MANAGER", "1")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    try:
        with TestClient(app) as client:
            r = client.get("/api/gateway/processes/env", headers=headers)
            assert r.status_code == 200, r.text
            assert os.getenv("ABSTRACT_TEST_MANAGED_VAR") == "persisted@example.com"
    finally:
        os.environ.pop("ABSTRACT_TEST_MANAGED_VAR", None)
