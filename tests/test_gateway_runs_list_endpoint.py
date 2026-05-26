from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _wait_until(predicate, *, timeout_s: float = 5.0, poll_s: float = 0.05):
    end = time.time() + timeout_s
    while time.time() < end:
        if predicate():
            return
        time.sleep(poll_s)
    raise AssertionError("timeout waiting for condition")


def _write_min_bundle(
    *,
    bundles_dir: Path,
    bundle_id: str,
    flow_id: str,
    bundle_version: str = "0.0.0",
    created_at: str = "2026-01-09T00:00:00+00:00",
) -> None:
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
                "data": {"nodeType": "on_flow_start", "label": "On Flow Start", "inputs": [], "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]},
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
        "bundle_version": bundle_version,
        "created_at": created_at,
        "entrypoints": [{"flow_id": flow_id, "name": "root", "description": "", "interfaces": []}],
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {"lifecycle": {"channel": "draft" if bundle_version.startswith("draft.") else "published"}},
    }

    safe_version = bundle_version.replace(".", "-").replace("/", "-")
    bundle_path = bundles_dir / f"{bundle_id}-{safe_version}.flow"
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr(f"flows/{flow_id}.json", json.dumps(flow, indent=2))


def test_list_runs_requires_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-runs", flow_id="root")

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
        r = client.get("/api/gateway/runs?limit=10")
        assert r.status_code in {401, 403}, r.text

        r2 = client.get("/api/gateway/runs?limit=10", headers=headers)
        assert r2.status_code == 200, r2.text
        assert isinstance(r2.json().get("items"), list)


def test_list_runs_includes_recent_runs_and_filters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-runs", flow_id="root")

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
        start = client.post("/api/gateway/runs/start", headers=headers, json={"bundle_id": "bundle-runs", "flow_id": "root", "input_data": {}})
        assert start.status_code == 200, start.text
        run_id = start.json()["run_id"]
        draft_lifecycle = {
            "source": "abstractflow.editor",
            "purpose": "draft_test",
            "visibility": "private",
            "retention": {"mode": "ephemeral", "ttl_s": "3600"},
            "editor_session_id": "editor-session",
            "flow_id": "root",
            "ignored": {"private": True},
        }
        draft_start = client.post(
            "/api/gateway/runs/start",
            headers=headers,
            json={
                "bundle_id": "bundle-runs",
                "flow_id": "root",
                "input_data": {},
                "run_lifecycle": draft_lifecycle,
            },
        )
        assert draft_start.status_code == 200, draft_start.text
        draft_run_id = draft_start.json()["run_id"]

        def _is_completed():
            rr = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
            assert rr.status_code == 200, rr.text
            draft_rr = client.get(f"/api/gateway/runs/{draft_run_id}", headers=headers)
            assert draft_rr.status_code == 200, draft_rr.text
            return rr.json().get("status") == "completed" and draft_rr.json().get("status") == "completed"

        _wait_until(_is_completed, timeout_s=5.0, poll_s=0.05)

        draft_detail = client.get(f"/api/gateway/runs/{draft_run_id}", headers=headers)
        assert draft_detail.status_code == 200, draft_detail.text
        draft_detail_json = draft_detail.json()
        assert draft_detail_json.get("is_draft") is True
        assert draft_detail_json.get("run_lifecycle") == {
            "source": "abstractflow.editor",
            "purpose": "draft_test",
            "visibility": "private",
            "editor_session_id": "editor-session",
            "flow_id": "root",
            "retention": {"mode": "ephemeral", "ttl_s": 3600},
        }

        listed = client.get("/api/gateway/runs?limit=25", headers=headers)
        assert listed.status_code == 200, listed.text
        items = listed.json().get("items") or []
        match = next((i for i in items if i.get("run_id") == run_id), None)
        assert match is not None
        assert next((i for i in items if i.get("run_id") == draft_run_id), None) is None
        assert isinstance(match.get("ledger_len"), int)
        assert match.get("ledger_len") >= 1

        listed_with_drafts = client.get("/api/gateway/runs?limit=25&include_drafts=true", headers=headers)
        assert listed_with_drafts.status_code == 200, listed_with_drafts.text
        draft_match = next((i for i in (listed_with_drafts.json().get("items") or []) if i.get("run_id") == draft_run_id), None)
        assert draft_match is not None
        assert draft_match.get("is_draft") is True
        assert draft_match.get("run_lifecycle", {}).get("purpose") == "draft_test"

        filtered = client.get("/api/gateway/runs?limit=25&status=completed", headers=headers)
        assert filtered.status_code == 200, filtered.text
        items2 = filtered.json().get("items") or []
        assert any(i.get("run_id") == run_id and i.get("status") == "completed" for i in items2)


def test_default_bundle_resolution_prefers_published_versions_over_drafts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(
        bundles_dir=bundles_dir,
        bundle_id="bundle-runs",
        flow_id="root",
        bundle_version="0.0.0",
        created_at="2026-01-09T00:00:00+00:00",
    )
    _write_min_bundle(
        bundles_dir=bundles_dir,
        bundle_id="bundle-runs",
        flow_id="root",
        bundle_version="draft.editor",
        created_at="2026-01-10T00:00:00+00:00",
    )

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
        default_bundles = client.get("/api/gateway/bundles", headers=headers)
        assert default_bundles.status_code == 200, default_bundles.text
        default_items = default_bundles.json().get("items") or []
        default_match = next((i for i in default_items if i.get("bundle_id") == "bundle-runs"), None)
        assert default_match is not None
        assert default_match.get("bundle_version") == "0.0.0"
        assert default_match.get("is_draft") is False
        assert default_match.get("latest_published_version") == "0.0.0"

        all_bundles = client.get("/api/gateway/bundles?all_versions=true&include_drafts=true", headers=headers)
        assert all_bundles.status_code == 200, all_bundles.text
        all_items = all_bundles.json().get("items") or []
        draft_match = next((i for i in all_items if i.get("bundle_version") == "draft.editor"), None)
        assert draft_match is not None
        assert draft_match.get("version_channel") == "draft"
        assert draft_match.get("is_draft") is True
        assert draft_match.get("latest_published_version") == "0.0.0"

        rejected = client.post(
            "/api/gateway/runs/start",
            headers=headers,
            json={"bundle_id": "bundle-runs", "bundle_version": "draft.editor", "flow_id": "root", "input_data": {}},
        )
        assert rejected.status_code == 400, rejected.text
        assert "run_lifecycle.purpose='draft_test'" in rejected.text

        start = client.post(
            "/api/gateway/runs/start",
            headers=headers,
            json={"bundle_id": "bundle-runs", "flow_id": "root", "input_data": {}},
        )
        assert start.status_code == 200, start.text
        run_id = start.json()["run_id"]

        def _is_completed():
            rr = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
            assert rr.status_code == 200, rr.text
            return rr.json().get("status") == "completed"

        _wait_until(_is_completed, timeout_s=5.0, poll_s=0.05)

        input_data = client.get(f"/api/gateway/runs/{run_id}/input_data", headers=headers)
        assert input_data.status_code == 200, input_data.text
        input_body = input_data.json()
        assert input_body.get("bundle_id") == "bundle-runs@0.0.0"
        assert input_body.get("flow_id") == "root"


def test_list_runs_filters_internal_memory_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-runs", flow_id="root")

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
        start = client.post("/api/gateway/runs/start", headers=headers, json={"bundle_id": "bundle-runs", "flow_id": "root", "input_data": {}})
        assert start.status_code == 200, start.text
        run_id = start.json()["run_id"]

        def _is_completed():
            rr = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
            assert rr.status_code == 200, rr.text
            return rr.json().get("status") == "completed"

        _wait_until(_is_completed, timeout_s=5.0, poll_s=0.05)

        # Inject internal runtime bookkeeping runs that would otherwise pollute the /runs list.
        from abstractgateway.service import get_gateway_service
        from abstractruntime.core.models import RunState, RunStatus

        svc = get_gateway_service()
        rs = svc.host.run_store

        internal_vars = {
            "context": {"task": "", "messages": []},
            "scratchpad": {},
            "_runtime": {"memory_spans": []},
            "_temp": {},
            "_limits": {},
        }
        rs.save(
            RunState(
                run_id="global_memory",
                workflow_id="__global_memory__",
                status=RunStatus.COMPLETED,
                current_node="done",
                vars=dict(internal_vars),
                waiting=None,
                output={"messages": []},
                error=None,
                created_at="9999-01-01T00:00:00+00:00",
                updated_at="9999-01-01T00:00:00+00:00",
                actor_id=None,
                session_id=None,
                parent_run_id=None,
            )
        )
        rs.save(
            RunState(
                run_id="session_memory_sess_1",
                workflow_id="__session_memory__",
                status=RunStatus.COMPLETED,
                current_node="done",
                vars=dict(internal_vars),
                waiting=None,
                output={"messages": []},
                error=None,
                created_at="9999-01-01T00:00:01+00:00",
                updated_at="9999-01-01T00:00:01+00:00",
                actor_id=None,
                session_id="sess_1",
                parent_run_id=None,
            )
        )

        listed = client.get("/api/gateway/runs?limit=50", headers=headers)
        assert listed.status_code == 200, listed.text
        items = listed.json().get("items") or []
        assert any(i.get("run_id") == run_id for i in items)
        assert all(not str(i.get("workflow_id") or "").startswith("__") for i in items)


def test_list_runs_with_sqlite_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-runs", flow_id="root")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_STORE_BACKEND", "sqlite")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        start = client.post("/api/gateway/runs/start", headers=headers, json={"bundle_id": "bundle-runs", "flow_id": "root", "input_data": {}})
        assert start.status_code == 200, start.text
        run_id = start.json()["run_id"]
        draft_start = client.post(
            "/api/gateway/runs/start",
            headers=headers,
            json={
                "bundle_id": "bundle-runs",
                "flow_id": "root",
                "input_data": {},
                "run_lifecycle": {"source": "abstractflow.editor", "purpose": "draft_test", "visibility": "private", "retention": {"mode": "ephemeral"}},
            },
        )
        assert draft_start.status_code == 200, draft_start.text
        draft_run_id = draft_start.json()["run_id"]

        def _is_completed():
            rr = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
            assert rr.status_code == 200, rr.text
            draft_rr = client.get(f"/api/gateway/runs/{draft_run_id}", headers=headers)
            assert draft_rr.status_code == 200, draft_rr.text
            return rr.json().get("status") == "completed" and draft_rr.json().get("status") == "completed"

        _wait_until(_is_completed, timeout_s=5.0, poll_s=0.05)

        listed = client.get("/api/gateway/runs?limit=25", headers=headers)
        assert listed.status_code == 200, listed.text
        items = listed.json().get("items") or []
        match = next((i for i in items if i.get("run_id") == run_id), None)
        assert match is not None
        assert next((i for i in items if i.get("run_id") == draft_run_id), None) is None
        assert match.get("status") == "completed"
        assert isinstance(match.get("ledger_len"), int)
        assert match.get("ledger_len") >= 1

        listed_drafts = client.get("/api/gateway/runs?limit=25&include_drafts=true", headers=headers)
        assert listed_drafts.status_code == 200, listed_drafts.text
        draft_match = next((i for i in (listed_drafts.json().get("items") or []) if i.get("run_id") == draft_run_id), None)
        assert draft_match is not None
        assert draft_match.get("is_draft") is True
        assert draft_match.get("run_lifecycle", {}).get("retention", {}).get("mode") == "ephemeral"


def test_list_runs_include_metrics_with_sqlite_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-runs", flow_id="root")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_STORE_BACKEND", "sqlite")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        start = client.post("/api/gateway/runs/start", headers=headers, json={"bundle_id": "bundle-runs", "flow_id": "root", "input_data": {}})
        assert start.status_code == 200, start.text
        run_id = start.json()["run_id"]

        def _is_completed():
            rr = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
            assert rr.status_code == 200, rr.text
            return rr.json().get("status") == "completed"

        _wait_until(_is_completed, timeout_s=5.0, poll_s=0.05)

        # Inject synthetic completed llm/tool ledger records so /runs?include_metrics can surface counts.
        from abstractgateway.service import get_gateway_service
        from abstractruntime.core.models import StepRecord, StepStatus

        svc = get_gateway_service()
        ledger = svc.host.ledger_store
        ledger.append(
            StepRecord(
                run_id=run_id,
                step_id="m1",
                node_id="n",
                status=StepStatus.COMPLETED,
                effect={"type": "llm_call", "payload": {}, "result_key": "_tmp"},
                result={"usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}},
            )
        )
        ledger.append(
            StepRecord(
                run_id=run_id,
                step_id="t1",
                node_id="n",
                status=StepStatus.COMPLETED,
                effect={"type": "tool_calls", "payload": {"tool_calls": [{"name": "a"}, {"name": "b"}]}, "result_key": "_tmp"},
                result={},
            )
        )

        listed = client.get("/api/gateway/runs?limit=25&include_metrics=true&include_ledger_len=false", headers=headers)
        assert listed.status_code == 200, listed.text
        items = listed.json().get("items") or []
        match = next((i for i in items if i.get("run_id") == run_id), None)
        assert match is not None
        assert match.get("llm_calls") == 1
        assert match.get("tool_calls") == 2
        assert match.get("tokens_total") == 12
