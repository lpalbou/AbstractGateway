from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _cleanup_optional_abstractflow_modules_after_test():
    yield
    for name in list(sys.modules):
        if name == "abstractflow" or name.startswith("abstractflow."):
            sys.modules.pop(name, None)


def _editor_flow_payload() -> dict:
    return {
        "name": "Gateway Editor Contract",
        "description": "Draft flow used by the gateway-first editor contract test.",
        "interfaces": ["abstractflow.editor.test.v1"],
        "nodes": [
            {
                "id": "start",
                "type": "on_flow_start",
                "position": {"x": 32.0, "y": 128.0},
                "data": {
                    "nodeType": "on_flow_start",
                    "label": "On Flow Start",
                    "inputs": [],
                    "outputs": [
                        {"id": "exec-out", "label": "", "type": "execution"},
                        {"id": "prompt", "label": "Prompt", "type": "string"},
                        {"id": "max_iterations", "label": "Max Iterations", "type": "integer"},
                    ],
                    "pinDefaults": {"prompt": "hello from the editor", "max_iterations": 3},
                },
            },
            {
                "id": "end",
                "type": "on_flow_end",
                "position": {"x": 320.0, "y": 128.0},
                "data": {
                    "nodeType": "on_flow_end",
                    "label": "On Flow End",
                    "inputs": [{"id": "exec-in", "label": "", "type": "execution"}],
                    "outputs": [],
                },
            },
        ],
        "edges": [
            {
                "id": "edge-start-end",
                "source": "start",
                "sourceHandle": "exec-out",
                "target": "end",
                "targetHandle": "exec-in",
            }
        ],
        "entryNode": "start",
    }


def _wait_until(fn, *, timeout_s: float = 5.0, poll_s: float = 0.05) -> None:
    deadline = time.monotonic() + timeout_s
    last_error: AssertionError | None = None
    while time.monotonic() < deadline:
        try:
            if fn():
                return
        except AssertionError as e:
            last_error = e
        time.sleep(poll_s)
    if last_error is not None:
        raise last_error
    raise AssertionError("condition did not become true before timeout")


@pytest.mark.basic
def test_abstractflow_gateway_first_editor_contract_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if importlib.util.find_spec("abstractflow") is None:
        pytest.skip("abstractflow is not installed")

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

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        unauth = client.get("/api/gateway/visualflows")
        assert unauth.status_code in {401, 403}, unauth.text

        create = client.post("/api/gateway/visualflows", json=_editor_flow_payload(), headers=headers)
        assert create.status_code == 200, create.text
        draft = create.json()
        flow_id = str(draft.get("id") or "")
        assert flow_id
        assert draft.get("name") == "Gateway Editor Contract"

        update = client.put(
            f"/api/gateway/visualflows/{flow_id}",
            json={"description": "Updated by the editor contract test."},
            headers=headers,
        )
        assert update.status_code == 200, update.text
        assert update.json().get("description") == "Updated by the editor contract test."

        fetched = client.get(f"/api/gateway/visualflows/{flow_id}", headers=headers)
        assert fetched.status_code == 200, fetched.text
        assert fetched.json().get("id") == flow_id

        listed = client.get("/api/gateway/visualflows", headers=headers)
        assert listed.status_code == 200, listed.text
        assert any(isinstance(item, dict) and item.get("id") == flow_id for item in listed.json())

        publish = client.post(
            f"/api/gateway/visualflows/{flow_id}/publish",
            json={"bundle_id": "editor-contract", "bundle_version": "0.0.0", "overwrite": True, "reload_gateway": True},
            headers=headers,
        )
        assert publish.status_code == 200, publish.text
        published = publish.json()
        assert published.get("ok") is True
        assert published.get("bundle_id") == "editor-contract"
        assert published.get("bundle_version") == "0.0.0"

        caps = client.get("/api/gateway/discovery/capabilities", headers=headers)
        assert caps.status_code == 200, caps.text
        flow_editor = caps.json()["capabilities"]["contracts"]["flow_editor"]
        assert flow_editor["visualflows"]["crud"]["available"] is True
        assert flow_editor["visualflows"]["publish"]["available"] is True
        assert flow_editor["run_input_schema"]["endpoint"] == "/api/gateway/bundles/{bundle_id}/flows/{flow_id}/input_schema"

        schema = client.get(
            f"/api/gateway/bundles/editor-contract/flows/{flow_id}/input_schema?bundle_version=0.0.0",
            headers=headers,
        )
        assert schema.status_code == 200, schema.text
        schema_body = schema.json()
        assert schema_body.get("version") == 1
        assert schema_body.get("workflow_id") == f"editor-contract@0.0.0:{flow_id}"
        inputs = schema_body.get("inputs")
        assert isinstance(inputs, list)
        assert {item.get("id") for item in inputs if isinstance(item, dict)} == {"prompt", "max_iterations"}
        assert schema_body.get("defaults") == {"prompt": "hello from the editor", "max_iterations": 3}
        props = schema_body.get("input_data_schema", {}).get("properties")
        assert isinstance(props, dict)
        assert props.get("prompt", {}).get("type") == "string"
        assert props.get("max_iterations", {}).get("type") == "integer"

        start = client.post(
            "/api/gateway/runs/start",
            json={
                "bundle_id": "editor-contract",
                "bundle_version": "0.0.0",
                "flow_id": flow_id,
                "input_data": {"prompt": "run from editor", "max_iterations": 5, "extra": "ignored"},
            },
            headers=headers,
        )
        assert start.status_code == 200, start.text
        run_id = start.json()["run_id"]

        def _completed() -> bool:
            run = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
            assert run.status_code == 200, run.text
            return run.json().get("status") == "completed"

        _wait_until(_completed, timeout_s=10.0)

        ledger = client.get(f"/api/gateway/runs/{run_id}/ledger?after=0&limit=200", headers=headers)
        assert ledger.status_code == 200, ledger.text
        ledger_body = ledger.json()
        assert isinstance(ledger_body.get("items"), list)
        assert ledger_body.get("next_after", 0) >= len(ledger_body["items"])

        batch = client.post(
            "/api/gateway/runs/ledger/batch",
            json={"limit": 200, "runs": [{"run_id": run_id, "after": 0}]},
            headers=headers,
        )
        assert batch.status_code == 200, batch.text
        assert run_id in (batch.json().get("runs") or {})

        input_data = client.get(f"/api/gateway/runs/{run_id}/input_data", headers=headers)
        assert input_data.status_code == 200, input_data.text
        input_body = input_data.json()
        assert input_body.get("bundle_id") == "editor-contract"
        assert input_body.get("bundle_version") == "0.0.0"
        assert input_body.get("flow_id") == flow_id
        assert input_body.get("input_data") == {"prompt": "run from editor", "max_iterations": 5}

        artifacts = client.get(f"/api/gateway/runs/{run_id}/artifacts", headers=headers)
        assert artifacts.status_code == 200, artifacts.text
        assert isinstance(artifacts.json().get("items"), list)

        history = client.get(
            f"/api/gateway/runs/{run_id}/history_bundle?include_subruns=true&ledger_mode=tail&ledger_max_items=200",
            headers=headers,
        )
        assert history.status_code == 200, history.text
        history_body = history.json()
        assert history_body.get("root_run_id") == run_id
        assert isinstance(history_body.get("run"), dict)
        assert history_body["run"].get("run_id") == run_id

        delete = client.delete(f"/api/gateway/visualflows/{flow_id}", headers=headers)
        assert delete.status_code == 200, delete.text
        assert delete.json().get("status") == "deleted"


@pytest.mark.basic
def test_abstractflow_gateway_publish_fails_fast_when_reload_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if importlib.util.find_spec("abstractflow") is None:
        pytest.skip("abstractflow is not installed")

    runtime_dir = tmp_path / "runtime"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    from abstractgateway.app import app
    from abstractgateway.service import get_gateway_service

    headers = {"Authorization": "Bearer t"}
    with TestClient(app) as client:
        create = client.post("/api/gateway/visualflows", json=_editor_flow_payload(), headers=headers)
        assert create.status_code == 200, create.text
        flow_id = str(create.json().get("id") or "")
        assert flow_id

        svc = get_gateway_service()
        host = getattr(svc, "host", None)
        assert host is not None

        def _broken_reload() -> None:
            raise RuntimeError("simulated bundle reload failure")

        monkeypatch.setattr(host, "reload_bundles_from_disk", _broken_reload)

        publish = client.post(
            f"/api/gateway/visualflows/{flow_id}/publish",
            json={"bundle_id": "editor-contract", "bundle_version": "0.0.1", "overwrite": True, "reload_gateway": True},
            headers=headers,
        )
        assert publish.status_code == 503, publish.text
        assert "Failed to reload bundles after publish" in publish.text
