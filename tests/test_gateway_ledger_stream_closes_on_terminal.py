from __future__ import annotations

import datetime
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _write_min_bundle(*, bundles_dir: Path, bundle_id: str, flow_id: str) -> None:
    bundles_dir.mkdir(parents=True, exist_ok=True)

    flow = {
        "id": flow_id,
        "name": "minimal",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "node-1",
                "type": "on_flow_start",
                "position": {"x": 32.0, "y": 128.0},
                "data": {
                    "nodeType": "on_flow_start",
                    "label": "On Flow Start",
                    "inputs": [],
                    "outputs": [{"id": "exec-out", "label": "", "type": "execution"}],
                },
            },
            {
                "id": "node-2",
                "type": "on_flow_end",
                "position": {"x": 288.0, "y": 128.0},
                "data": {"nodeType": "on_flow_end", "label": "On Flow End", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []},
            },
        ],
        "edges": [{"id": "e1", "source": "node-1", "sourceHandle": "exec-out", "target": "node-2", "targetHandle": "exec-in"}],
        "entryNode": "node-1",
    }

    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-01-09T00:00:00+00:00",
        "entrypoints": [{"flow_id": flow_id, "name": "root", "description": "", "interfaces": []}],
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }

    bundle_path = bundles_dir / f"{bundle_id}.flow"
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr(f"flows/{flow_id}.json", json.dumps(flow, indent=2))


def test_gateway_ledger_stream_closes_on_terminal_run(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    flows_dir = tmp_path / "bundles"
    flows_dir.mkdir(parents=True, exist_ok=True)
    _write_min_bundle(bundles_dir=flows_dir, bundle_id="bundle-stream", flow_id="root")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_STORE_BACKEND", "file")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    from abstractgateway.app import app
    from abstractgateway.service import get_gateway_service
    from abstractruntime import Effect, EffectType, RunState, RunStatus
    from abstractruntime.core.models import StepRecord

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        svc = get_gateway_service()

        run_id = "run_stream_done"
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        run = RunState(
            run_id=run_id,
            workflow_id="test",
            status=RunStatus.COMPLETED,
            current_node="end",
            vars={"context": {"task": "", "messages": []}},
            waiting=None,
            output={"output": {"response": "ok"}},
            error=None,
            created_at=now_iso,
            updated_at=now_iso,
            actor_id=None,
            session_id="s",
            parent_run_id=None,
        )
        svc.host.run_store.save(run)

        rec = StepRecord.start(
            run=run,
            node_id="node-1",
            effect=Effect(type=EffectType.EMIT_EVENT, payload={"name": "abstract.message", "payload": {"text": "hi"}}),
        )
        rec.finish_success({"ok": True})
        svc.host.ledger_store.append(rec)

        resp = client.get(f"/api/gateway/runs/{run_id}/ledger/stream?after=0&heartbeat_s=0.2", headers=headers)
        assert resp.status_code == 200, resp.text
        text = resp.text
        assert "event: step" in text
        assert "event: done" in text
