from __future__ import annotations

import json
from pathlib import Path
import zipfile

import pytest
from fastapi.testclient import TestClient


def _write_test_bundle(*, bundles_dir: Path) -> tuple[str, str]:
    bundles_dir.mkdir(parents=True, exist_ok=True)
    bundle_id = "bundle-artifacts"
    flow_id = "flow"

    flow = {
        "id": flow_id,
        "name": "test",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "node-1",
                "type": "on_flow_start",
                "position": {"x": 32.0, "y": 224.0},
                "data": {"nodeType": "on_flow_start", "label": "On Flow Start", "inputs": [], "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]},
            },
            {
                "id": "node-2",
                "type": "ask_user",
                "position": {"x": 288.0, "y": 224.0},
                "data": {
                    "nodeType": "ask_user",
                    "label": "Ask User",
                    "inputs": [{"id": "exec-in", "label": "", "type": "execution"}],
                    "outputs": [
                        {"id": "exec-out", "label": "", "type": "execution"},
                        {"id": "response", "label": "response", "type": "string"},
                    ],
                    "pinDefaults": {"prompt": "hello"},
                },
            },
        ],
        "edges": [
            {
                "id": "edge-1",
                "source": "node-1",
                "sourceHandle": "exec-out",
                "target": "node-2",
                "targetHandle": "exec-in",
            }
        ],
        "entryNode": "node-1",
    }
    flow_bytes = json.dumps(flow, indent=2).encode("utf-8")

    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-01-12T00:00:00+00:00",
        "entrypoints": [{"flow_id": flow_id, "name": "test", "description": "", "interfaces": []}],
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }
    bundle_path = bundles_dir / f"{bundle_id}.flow"
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr(f"flows/{flow_id}.json", flow_bytes)

    return bundle_id, flow_id


def test_gateway_artifacts_api_list_metadata_and_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    bundle_id, flow_id = _write_test_bundle(bundles_dir=bundles_dir)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    from abstractgateway.app import app
    from abstractruntime.storage.artifacts import FileArtifactStore

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": bundle_id, "flow_id": flow_id, "input_data": {}},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]

        store = FileArtifactStore(runtime_dir)
        meta = store.store(b"hello", content_type="text/plain", run_id=run_id)
        artifact_id = meta.artifact_id

        resp = client.get(f"/api/gateway/runs/{run_id}/artifacts", headers=headers)
        assert resp.status_code == 200, resp.text
        items = resp.json().get("items")
        assert isinstance(items, list)
        assert any(it.get("artifact_id") == artifact_id for it in items)

        resp = client.get(f"/api/gateway/runs/{run_id}/artifacts/{artifact_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        meta_out = resp.json()
        assert meta_out.get("artifact_id") == artifact_id
        assert meta_out.get("run_id") == run_id
        assert meta_out.get("content_type") == "text/plain"

        resp = client.get(f"/api/gateway/runs/{run_id}/artifacts/{artifact_id}/content", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.content == b"hello"

        other = store.store(b"world", content_type="text/plain", run_id="other-run")
        resp = client.get(f"/api/gateway/runs/{run_id}/artifacts/{other.artifact_id}", headers=headers)
        assert resp.status_code == 404


def test_gateway_artifact_import_session_list_export_and_run_start_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    bundle_id, flow_id = _write_test_bundle(bundles_dir=bundles_dir)

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "input.txt").write_text("artifact handoff\n", encoding="utf-8")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    from abstractgateway.app import app
    from abstractruntime.storage.artifacts import FileArtifactStore

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        imported = client.post(
            "/api/gateway/artifacts/import",
            json={
                "session_id": "s-artifacts",
                "source": {"kind": "workspace_path", "path": "input.txt"},
                "pin_id": "image",
            },
            headers=headers,
        )
        assert imported.status_code == 200, imported.text
        body = imported.json()
        ref = body.get("artifact") or {}
        assert isinstance(ref, dict)
        artifact_id = ref.get("$artifact")
        assert isinstance(artifact_id, str) and artifact_id
        assert ref.get("artifact_id") == artifact_id
        assert ref.get("run_id") == "session_memory_s-artifacts"
        assert ref.get("source_path") == "input.txt"
        assert ref.get("content_type") == "text/plain"

        listed = client.get("/api/gateway/sessions/s-artifacts/artifacts", headers=headers)
        assert listed.status_code == 200, listed.text
        items = listed.json().get("items") or []
        found = next((item for item in items if item.get("artifact_id") == artifact_id), None)
        assert isinstance(found, dict)
        assert found.get("run_id") == "session_memory_s-artifacts"
        assert (found.get("ref") or {}).get("$artifact") == artifact_id

        searched = client.get(
            "/api/gateway/artifacts/search",
            params={"scope": "all", "modality": "text", "query": "input.txt", "tags": '{"pin_id":"image"}'},
            headers=headers,
        )
        assert searched.status_code == 200, searched.text
        search_items = searched.json().get("items") or []
        assert any(item.get("artifact_id") == artifact_id for item in search_items)

        session_search = client.get(
            "/api/gateway/artifacts/search",
            params={"scope": "session", "session_id": "s-artifacts", "content_type": "text/*"},
            headers=headers,
        )
        assert session_search.status_code == 200, session_search.text
        assert any(item.get("artifact_id") == artifact_id for item in session_search.json().get("items") or [])

        producer = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": bundle_id, "flow_id": flow_id, "session_id": "producer-session", "input_data": {}},
            headers=headers,
        )
        assert producer.status_code == 200, producer.text
        producer_run_id = producer.json()["run_id"]
        prior_meta = FileArtifactStore(runtime_dir).store(
            b"png",
            content_type="image/png",
            run_id=producer_run_id,
            tags={"filename": "prior.png", "modality": "image"},
        )
        prior_ref = {
            "$artifact": prior_meta.artifact_id,
            "artifact_id": prior_meta.artifact_id,
            "run_id": producer_run_id,
            "content_type": "image/png",
            "modality": "image",
            "filename": "prior.png",
        }
        prior_search = client.get(
            "/api/gateway/artifacts/search",
            params={"scope": "all", "modality": "image", "query": "prior.png"},
            headers=headers,
        )
        assert prior_search.status_code == 200, prior_search.text
        assert any(item.get("artifact_id") == prior_meta.artifact_id for item in prior_search.json().get("items") or [])

        started = client.post(
            "/api/gateway/runs/start",
            json={
                "bundle_id": bundle_id,
                "flow_id": flow_id,
                "session_id": "s-artifacts",
                "input_data": {"image": ref},
            },
            headers=headers,
        )
        assert started.status_code == 200, started.text

        explicit_prior_ref = client.post(
            "/api/gateway/runs/start",
            json={
                "bundle_id": bundle_id,
                "flow_id": flow_id,
                "session_id": "s-artifacts",
                "input_data": {"image": prior_ref},
            },
            headers=headers,
        )
        assert explicit_prior_ref.status_code == 200, explicit_prior_ref.text

        rejected = client.post(
            "/api/gateway/runs/start",
            json={
                "bundle_id": bundle_id,
                "flow_id": flow_id,
                "session_id": "other-session",
                "input_data": {"image": ref},
            },
            headers=headers,
        )
        assert rejected.status_code == 404, rejected.text
        assert "not visible to session" in rejected.json().get("detail", "")

        exported = client.post(
            f"/api/gateway/runs/session_memory_s-artifacts/artifacts/{artifact_id}/export",
            json={"path": "exports/copy.txt", "create_parent_dirs": True},
            headers=headers,
        )
        assert exported.status_code == 200, exported.text
        assert (workspace / "exports" / "copy.txt").read_text(encoding="utf-8") == "artifact handoff\n"

        blocked_overwrite = client.post(
            f"/api/gateway/runs/session_memory_s-artifacts/artifacts/{artifact_id}/export",
            json={"path": "exports/copy.txt"},
            headers=headers,
        )
        assert blocked_overwrite.status_code == 409, blocked_overwrite.text
