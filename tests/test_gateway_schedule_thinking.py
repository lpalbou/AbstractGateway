"""/runs/schedule must speak the reasoning lane (2026-08-04).

Before this, `ScheduleRunRequest` had no `thinking` field and the wrapper-root
`_runtime` lift was catalog-only — scheduled targets ran at the provider/relay
default with no way to say otherwise (handoff report §8-Q3 family: the value
was absent at the ROOT, so the runtime's child-spawn riders had nothing to
carry).

The lane is pinned at both stops:
- FOLD: `req.thinking` lands in the captured input payload's
  `_runtime.thinking` (same fold and precedence as /runs/start — the explicit
  top-level field beats an embedded value), which the wrapper passes to every
  execution via the `vars` pin;
- LIFT: the WRAPPER root run's own `_runtime.thinking` carries it too, so the
  runtime's START_SUBWORKFLOW rider covers every execution child even outside
  the vars-pin payload.
"""

from __future__ import annotations

import json
from pathlib import Path
import zipfile

import pytest
from fastapi.testclient import TestClient


def _make_min_flow(flow_id: str) -> dict:
    fid = str(flow_id or "").strip() or "root"
    return {
        "id": fid,
        "name": fid,
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
                "id": "end",
                "type": "on_flow_end",
                "position": {"x": 10.0, "y": 0.0},
                "data": {"nodeType": "on_flow_end", "label": "On Flow End", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []},
            },
        ],
        "edges": [
            {"id": "e1", "source": "start", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in", "animated": True},
        ],
        "entryNode": "start",
    }


def _write_bundle(path: Path, *, bundle_id: str, bundle_version: str, flow_id: str) -> None:
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": bundle_version,
        "created_at": "2026-01-24T00:00:00Z",
        "entrypoints": [{"flow_id": flow_id, "name": flow_id, "description": "", "interfaces": []}],
        "default_entrypoint": flow_id,
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "metadata": {"test": True},
    }
    flow = _make_min_flow(flow_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr(f"flows/{flow_id}.json", json.dumps(flow, ensure_ascii=False, indent=2))


def _load_run_json(runtime_dir: Path, run_id: str) -> dict:
    matches = list(runtime_dir.rglob(f"run_{run_id}.json"))
    assert matches, f"run file for {run_id} not found under {runtime_dir}"
    return json.loads(matches[0].read_text(encoding="utf-8"))


@pytest.mark.integration
def test_schedule_thinking_folds_into_payload_and_lifts_to_wrapper_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir(parents=True, exist_ok=True)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")

    bundle_path = tmp_path / "demo@0.0.1.flow"
    _write_bundle(bundle_path, bundle_id="demo", bundle_version="0.0.1", flow_id="root")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        up = client.post(
            "/api/gateway/bundles/upload",
            headers=headers,
            data={"overwrite": "false", "reload": "true"},
            files={"file": (bundle_path.name, bundle_path.read_bytes(), "application/octet-stream")},
        )
        assert up.status_code == 200, up.text

        sched = client.post(
            "/api/gateway/runs/schedule",
            headers=headers,
            json={
                "bundle_id": "demo",
                "flow_id": "root",
                "input_data": {"prompt": "x"},
                "thinking": "medium",
                "start_at": "now",
            },
        )
        assert sched.status_code == 200, sched.text
        run_id = sched.json().get("run_id")
        assert isinstance(run_id, str) and run_id

        wrapper = _load_run_json(runtime_dir, run_id)
        vars_obj = wrapper.get("vars") or {}

        # LIFT: the wrapper root's own `_runtime` carries the dial, so the
        # START_SUBWORKFLOW rider reaches every execution child.
        assert (vars_obj.get("_runtime") or {}).get("thinking") == "medium"

        # FOLD: the captured target payload carries it explicitly too (the
        # `vars` pin hands it to each execution as its own `_runtime`).
        assert (vars_obj.get("vars") or {}).get("_runtime", {}).get("thinking") == "medium"


@pytest.mark.integration
def test_schedule_request_field_beats_embedded_runtime_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same precedence as /runs/start: the explicit top-level field wins."""
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir(parents=True, exist_ok=True)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")

    bundle_path = tmp_path / "demo@0.0.1.flow"
    _write_bundle(bundle_path, bundle_id="demo", bundle_version="0.0.1", flow_id="root")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        up = client.post(
            "/api/gateway/bundles/upload",
            headers=headers,
            data={"overwrite": "false", "reload": "true"},
            files={"file": (bundle_path.name, bundle_path.read_bytes(), "application/octet-stream")},
        )
        assert up.status_code == 200, up.text

        sched = client.post(
            "/api/gateway/runs/schedule",
            headers=headers,
            json={
                "bundle_id": "demo",
                "flow_id": "root",
                "input_data": {"prompt": "x", "_runtime": {"thinking": "low"}},
                "thinking": "high",
                "start_at": "now",
            },
        )
        assert sched.status_code == 200, sched.text
        wrapper = _load_run_json(runtime_dir, sched.json()["run_id"])
        vars_obj = wrapper.get("vars") or {}
        assert (vars_obj.get("vars") or {}).get("_runtime", {}).get("thinking") == "high"
        assert (vars_obj.get("_runtime") or {}).get("thinking") == "high"


@pytest.mark.integration
def test_schedule_thinking_false_rides_as_off_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """False is a decision ("reasoning off"), not an absence: it must fold,
    lift, and survive as a real boolean — never be dropped by a truthiness
    gate."""
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir(parents=True, exist_ok=True)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")

    bundle_path = tmp_path / "demo@0.0.1.flow"
    _write_bundle(bundle_path, bundle_id="demo", bundle_version="0.0.1", flow_id="root")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        up = client.post(
            "/api/gateway/bundles/upload",
            headers=headers,
            data={"overwrite": "false", "reload": "true"},
            files={"file": (bundle_path.name, bundle_path.read_bytes(), "application/octet-stream")},
        )
        assert up.status_code == 200, up.text

        sched = client.post(
            "/api/gateway/runs/schedule",
            headers=headers,
            json={
                "bundle_id": "demo",
                "flow_id": "root",
                "input_data": {"prompt": "x"},
                "thinking": False,
                "start_at": "now",
            },
        )
        assert sched.status_code == 200, sched.text
        wrapper = _load_run_json(runtime_dir, sched.json()["run_id"])
        vars_obj = wrapper.get("vars") or {}
        assert (vars_obj.get("_runtime") or {}).get("thinking") is False
        assert (vars_obj.get("vars") or {}).get("_runtime", {}).get("thinking") is False


@pytest.mark.integration
def test_schedule_without_thinking_invents_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent stays absent — downstream defaults own the fallback."""
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    bundles_dir.mkdir(parents=True, exist_ok=True)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")

    bundle_path = tmp_path / "demo@0.0.1.flow"
    _write_bundle(bundle_path, bundle_id="demo", bundle_version="0.0.1", flow_id="root")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        up = client.post(
            "/api/gateway/bundles/upload",
            headers=headers,
            data={"overwrite": "false", "reload": "true"},
            files={"file": (bundle_path.name, bundle_path.read_bytes(), "application/octet-stream")},
        )
        assert up.status_code == 200, up.text

        sched = client.post(
            "/api/gateway/runs/schedule",
            headers=headers,
            json={"bundle_id": "demo", "flow_id": "root", "input_data": {"prompt": "x"}, "start_at": "now"},
        )
        assert sched.status_code == 200, sched.text
        wrapper = _load_run_json(runtime_dir, sched.json()["run_id"])
        vars_obj = wrapper.get("vars") or {}
        assert "thinking" not in (vars_obj.get("_runtime") or {})
        assert "thinking" not in ((vars_obj.get("vars") or {}).get("_runtime") or {})
