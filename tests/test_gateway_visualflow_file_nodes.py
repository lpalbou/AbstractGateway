from __future__ import annotations

import json
import time
from pathlib import Path
import zipfile

from fastapi.testclient import TestClient


def _wait_until(predicate, *, timeout_s: float = 5.0, poll_s: float = 0.05):
    end = time.time() + timeout_s
    while time.time() < end:
        if predicate():
            return
        time.sleep(poll_s)
    raise AssertionError("timeout waiting for condition")


def _write_bundle(*, bundles_dir: Path, bundle_id: str, flow_id: str, flow: dict) -> None:
    bundles_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-06-05T00:00:00+00:00",
        "entrypoints": [{"flow_id": flow_id, "name": "test", "description": "", "interfaces": []}],
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }
    with zipfile.ZipFile(bundles_dir / f"{bundle_id}.flow", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr(f"flows/{flow_id}.json", json.dumps(flow, indent=2))


def test_gateway_visualflow_write_file_uses_run_workspace(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "gateway-data"
    bundles_dir = tmp_path / "bundles"
    bundle_id = "bundle-write-file"
    flow_id = "root"
    relpath = "reports/deep-research.md"

    flow = {
        "id": flow_id,
        "name": "Write File Workspace",
        "nodes": [
            {"id": "start", "type": "on_flow_start", "data": {"nodeType": "on_flow_start"}},
            {
                "id": "build_markdown",
                "type": "code",
                "data": {"nodeType": "code", "codeBody": "return '# Gateway Report\\n\\nWritten in the run workspace.\\n'"},
            },
            {
                "id": "write_markdown",
                "type": "write_file",
                "data": {"nodeType": "write_file", "pinDefaults": {"file_path": relpath}},
            },
            {
                "id": "end",
                "type": "on_flow_end",
                "data": {
                    "nodeType": "on_flow_end",
                    "inputs": [
                        {"id": "exec-in", "label": "", "type": "execution"},
                        {"id": "markdown_path", "label": "markdown_path", "type": "string"},
                    ],
                },
            },
        ],
        "edges": [
            {"source": "start", "sourceHandle": "exec-out", "target": "build_markdown", "targetHandle": "exec-in"},
            {"source": "build_markdown", "sourceHandle": "exec-out", "target": "write_markdown", "targetHandle": "exec-in"},
            {"source": "write_markdown", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"},
            {"source": "build_markdown", "sourceHandle": "output", "target": "write_markdown", "targetHandle": "content"},
            {"source": "write_markdown", "sourceHandle": "file_path", "target": "end", "targetHandle": "markdown_path"},
        ],
        "entryNode": "start",
    }
    _write_bundle(bundles_dir=bundles_dir, bundle_id=bundle_id, flow_id=flow_id, flow=flow)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        start = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": bundle_id, "flow_id": flow_id, "input_data": {}},
            headers=headers,
        )
        assert start.status_code == 200, start.text
        run_id = start.json()["run_id"]

        def _is_completed():
            run = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
            assert run.status_code == 200, run.text
            return run.json().get("status") == "completed"

        _wait_until(_is_completed, timeout_s=10.0, poll_s=0.1)
        input_data = client.get(f"/api/gateway/runs/{run_id}/input_data", headers=headers)
        assert input_data.status_code == 200, input_data.text
        workspace_payload = input_data.json().get("workspace") or {}

    workspace = Path(str(workspace_payload.get("workspace_root") or ""))
    assert workspace.is_dir()
    report = workspace / relpath
    assert report.read_text(encoding="utf-8") == "# Gateway Report\n\nWritten in the run workspace.\n"


def test_gateway_visualflow_write_pdf_and_read_pdf_use_run_workspace(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "gateway-data"
    bundles_dir = tmp_path / "bundles"
    bundle_id = "bundle-write-read-pdf"
    flow_id = "root"
    relpath = "reports/deep-research.pdf"

    flow = {
        "id": flow_id,
        "name": "Write Read PDF Workspace",
        "nodes": [
            {"id": "start", "type": "on_flow_start", "data": {"nodeType": "on_flow_start"}},
            {
                "id": "build_markdown",
                "type": "code",
                "data": {
                    "nodeType": "code",
                    "codeBody": "return '# Gateway PDF Report\\n\\nA workspace-scoped PDF report.\\n\\n- Source A\\n'",
                },
            },
            {
                "id": "write_pdf",
                "type": "write_pdf",
                "data": {"nodeType": "write_pdf", "pinDefaults": {"file_path": relpath, "title": "Gateway PDF Report"}},
            },
            {"id": "read_pdf", "type": "read_pdf", "data": {"nodeType": "read_pdf"}},
            {
                "id": "end",
                "type": "on_flow_end",
                "data": {
                    "nodeType": "on_flow_end",
                    "inputs": [
                        {"id": "exec-in", "label": "", "type": "execution"},
                        {"id": "content", "label": "content", "type": "string"},
                        {"id": "pages", "label": "pages", "type": "number"},
                        {"id": "pdf_path", "label": "pdf_path", "type": "string"},
                        {"id": "sha256", "label": "sha256", "type": "string"},
                    ],
                },
            },
        ],
        "edges": [
            {"source": "start", "sourceHandle": "exec-out", "target": "build_markdown", "targetHandle": "exec-in"},
            {"source": "build_markdown", "sourceHandle": "exec-out", "target": "write_pdf", "targetHandle": "exec-in"},
            {"source": "write_pdf", "sourceHandle": "exec-out", "target": "read_pdf", "targetHandle": "exec-in"},
            {"source": "read_pdf", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"},
            {"source": "build_markdown", "sourceHandle": "output", "target": "write_pdf", "targetHandle": "content"},
            {"source": "write_pdf", "sourceHandle": "file_path", "target": "read_pdf", "targetHandle": "file_path"},
            {"source": "read_pdf", "sourceHandle": "content", "target": "end", "targetHandle": "content"},
            {"source": "read_pdf", "sourceHandle": "pages", "target": "end", "targetHandle": "pages"},
            {"source": "write_pdf", "sourceHandle": "file_path", "target": "end", "targetHandle": "pdf_path"},
            {"source": "write_pdf", "sourceHandle": "sha256", "target": "end", "targetHandle": "sha256"},
        ],
        "entryNode": "start",
    }
    _write_bundle(bundles_dir=bundles_dir, bundle_id=bundle_id, flow_id=flow_id, flow=flow)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        start = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": bundle_id, "flow_id": flow_id, "input_data": {}},
            headers=headers,
        )
        assert start.status_code == 200, start.text
        run_id = start.json()["run_id"]

        def _is_completed():
            run = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
            assert run.status_code == 200, run.text
            return run.json().get("status") == "completed"

        _wait_until(_is_completed, timeout_s=10.0, poll_s=0.1)
        input_data = client.get(f"/api/gateway/runs/{run_id}/input_data", headers=headers)
        assert input_data.status_code == 200, input_data.text
        workspace_payload = input_data.json().get("workspace") or {}

        ledger = client.get(f"/api/gateway/runs/{run_id}/ledger?after=0&limit=500", headers=headers)
        assert ledger.status_code == 200, ledger.text
        ledger_text = json.dumps(ledger.json().get("items") or [], sort_keys=True)

    workspace = Path(str(workspace_payload.get("workspace_root") or ""))
    assert workspace.is_dir()
    report = workspace / relpath
    assert report.read_bytes().startswith(b"%PDF-")
    assert "Gateway PDF Report" in ledger_text
    assert "workspace-scoped PDF report" in ledger_text


def test_gateway_server_file_mount_alias_round_trips_across_search_import_run_and_export(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "gateway-data"
    bundles_dir = tmp_path / "bundles"
    bundle_id = "bundle-mounted-read-file"
    flow_id = "root"

    flow = {
        "id": flow_id,
        "name": "Read Mounted Server File",
        "nodes": [
            {
                "id": "start",
                "type": "on_flow_start",
                "data": {
                    "nodeType": "on_flow_start",
                    "outputs": [
                        {"id": "exec-out", "label": "", "type": "execution"},
                        {"id": "file_path", "label": "file_path", "type": "string"},
                    ],
                },
            },
            {"id": "read_file", "type": "read_file", "data": {"nodeType": "read_file"}},
            {
                "id": "end",
                "type": "on_flow_end",
                "data": {
                    "nodeType": "on_flow_end",
                    "inputs": [
                        {"id": "exec-in", "label": "", "type": "execution"},
                        {"id": "content", "label": "content", "type": "string"},
                    ],
                },
            },
        ],
        "edges": [
            {"source": "start", "sourceHandle": "exec-out", "target": "read_file", "targetHandle": "exec-in"},
            {"source": "start", "sourceHandle": "file_path", "target": "read_file", "targetHandle": "file_path"},
            {"source": "read_file", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"},
            {"source": "read_file", "sourceHandle": "content", "target": "end", "targetHandle": "content"},
        ],
        "entryNode": "start",
    }
    _write_bundle(bundles_dir=bundles_dir, bundle_id=bundle_id, flow_id=flow_id, flow=flow)

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    first = tmp_path / "team-a" / "reports"
    second = tmp_path / "team-b" / "reports"
    first.mkdir(parents=True, exist_ok=True)
    second.mkdir(parents=True, exist_ok=True)
    (first / "alpha.txt").write_text("alpha mounted\n", encoding="utf-8")
    (second / "beta.txt").write_text("beta mounted\n", encoding="utf-8")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOW_CLIENT_WORKSPACE_SCOPE", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        scope = {
            "workspace_root": str(workspace),
            "workspace_access_mode": "workspace_or_allowed",
            "workspace_allowed_paths": f"{first}\n{second}",
        }
        search = client.get(
            "/api/gateway/files/search",
            params={**scope, "query": "beta", "limit": 20},
            headers=headers,
        )
        assert search.status_code == 200, search.text
        items = search.json().get("items") or []
        mounted_item = next((item for item in items if isinstance(item, dict) and str(item.get("path") or "").endswith("/beta.txt")), None)
        assert isinstance(mounted_item, dict)
        mounted_path = str(mounted_item.get("path") or "")
        mount_alias = mounted_path.split("/", 1)[0]
        assert mount_alias

        imported = client.post(
            "/api/gateway/artifacts/import",
            json={
                "session_id": "mounted-session",
                "source": {"kind": "workspace_path", "path": mounted_path},
                "pin_id": "document",
                **scope,
            },
            headers=headers,
        )
        assert imported.status_code == 200, imported.text
        imported_ref = imported.json().get("artifact") or {}
        artifact_id = imported_ref.get("$artifact")
        assert isinstance(artifact_id, str) and artifact_id
        assert imported_ref.get("source_path") == mounted_path

        start = client.post(
            "/api/gateway/runs/start",
            json={
                "bundle_id": bundle_id,
                "flow_id": flow_id,
                "session_id": "mounted-session",
                "input_data": {
                    "file_path": mounted_path,
                    "workspace_root": str(workspace),
                    "workspace_access_mode": "workspace_or_allowed",
                    "workspace_allowed_paths": [str(first), str(second)],
                },
            },
            headers=headers,
        )
        assert start.status_code == 200, start.text
        run_id = start.json()["run_id"]

        def _is_completed():
            run = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
            assert run.status_code == 200, run.text
            return run.json().get("status") == "completed"

        _wait_until(_is_completed, timeout_s=10.0, poll_s=0.1)
        run = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
        assert run.status_code == 200, run.text
        ledger = client.get(f"/api/gateway/runs/{run_id}/ledger?after=0&limit=500", headers=headers)
        assert ledger.status_code == 200, ledger.text
        assert "beta mounted" in json.dumps(ledger.json().get("items") or [], sort_keys=True)

        export = client.post(
            f"/api/gateway/runs/session_memory_mounted-session/artifacts/{artifact_id}/export",
            json={
                "path": f"{mount_alias}/exports/copy.txt",
                "create_parent_dirs": True,
                **scope,
            },
            headers=headers,
        )
        assert export.status_code == 200, export.text

    assert (second / "exports" / "copy.txt").read_text(encoding="utf-8") == "beta mounted\n"


def test_gateway_files_list_browses_root_and_mount_filters(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "gateway-data"
    workspace = tmp_path / "workspace"
    mounted = tmp_path / "mounted" / "reports"
    workspace.mkdir(parents=True, exist_ok=True)
    mounted.mkdir(parents=True, exist_ok=True)
    (workspace / "notes.txt").write_text("root\n", encoding="utf-8")
    (mounted / "alpha.md").write_text("# alpha\n", encoding="utf-8")
    (mounted / "beta.json").write_text('{"beta":true}\n', encoding="utf-8")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOW_CLIENT_WORKSPACE_SCOPE", "1")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        scope = {
            "workspace_root": str(workspace),
            "workspace_access_mode": "workspace_or_allowed",
            "workspace_allowed_paths": str(mounted),
        }
        root_list = client.get("/api/gateway/files/list", params=scope, headers=headers)
        assert root_list.status_code == 200, root_list.text
        root_items = root_list.json().get("items") or []
        mount_item = next((item for item in root_items if isinstance(item, dict) and item.get("mount")), None)
        assert isinstance(mount_item, dict)
        mount_alias = str(mount_item.get("path") or "")
        assert mount_alias

        filtered = client.get(
            "/api/gateway/files/list",
            params={**scope, "path": mount_alias, "recursive": "true", "extensions": "md"},
            headers=headers,
        )
        assert filtered.status_code == 200, filtered.text
        items = filtered.json().get("items") or []
        paths = [str(item.get("path") or "") for item in items if isinstance(item, dict) and item.get("kind") == "file"]
        assert paths == [f"{mount_alias}/alpha.md"]


def test_gateway_visualflow_import_read_and_export_artifact_nodes(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "gateway-data"
    bundles_dir = tmp_path / "bundles"
    bundle_id = "bundle-import-read-export-artifact"
    flow_id = "root"
    workspace = tmp_path / "workspace"
    (workspace / "source").mkdir(parents=True, exist_ok=True)
    (workspace / "exports").mkdir(parents=True, exist_ok=True)
    (workspace / "source" / "notes.txt").write_text("gateway artifact node\n", encoding="utf-8")

    flow = {
        "id": flow_id,
        "name": "Import Read Export Artifact",
        "nodes": [
            {"id": "start", "type": "on_flow_start", "data": {"nodeType": "on_flow_start"}},
            {
                "id": "import_file",
                "type": "import_workspace_file",
                "data": {"nodeType": "import_workspace_file", "pinDefaults": {"file_path": "source/notes.txt"}},
            },
            {"id": "read_artifact", "type": "read_artifact", "data": {"nodeType": "read_artifact"}},
            {
                "id": "export_artifact",
                "type": "export_artifact",
                "data": {"nodeType": "export_artifact", "pinDefaults": {"file_path": "exports/copy.txt"}},
            },
            {
                "id": "end",
                "type": "on_flow_end",
                "data": {
                    "nodeType": "on_flow_end",
                    "inputs": [
                        {"id": "exec-in", "label": "", "type": "execution"},
                        {"id": "text", "label": "text", "type": "string"},
                        {"id": "artifact_id", "label": "artifact_id", "type": "string"},
                        {"id": "export_path", "label": "export_path", "type": "string"},
                    ],
                },
            },
        ],
        "edges": [
            {"source": "start", "sourceHandle": "exec-out", "target": "import_file", "targetHandle": "exec-in"},
            {"source": "import_file", "sourceHandle": "exec-out", "target": "read_artifact", "targetHandle": "exec-in"},
            {"source": "read_artifact", "sourceHandle": "exec-out", "target": "export_artifact", "targetHandle": "exec-in"},
            {"source": "export_artifact", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"},
            {"source": "import_file", "sourceHandle": "artifact", "target": "read_artifact", "targetHandle": "artifact"},
            {"source": "import_file", "sourceHandle": "artifact", "target": "export_artifact", "targetHandle": "artifact"},
            {"source": "read_artifact", "sourceHandle": "text", "target": "end", "targetHandle": "text"},
            {"source": "read_artifact", "sourceHandle": "artifact_id", "target": "end", "targetHandle": "artifact_id"},
            {"source": "export_artifact", "sourceHandle": "file_path", "target": "end", "targetHandle": "export_path"},
        ],
        "entryNode": "start",
    }
    _write_bundle(bundles_dir=bundles_dir, bundle_id=bundle_id, flow_id=flow_id, flow=flow)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOW_CLIENT_WORKSPACE_SCOPE", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        start = client.post(
            "/api/gateway/runs/start",
            json={
                "bundle_id": bundle_id,
                "flow_id": flow_id,
                "session_id": "artifact-node-session",
                "input_data": {
                    "workspace_root": str(workspace),
                    "workspace_access_mode": "workspace_only",
                },
            },
            headers=headers,
        )
        assert start.status_code == 200, start.text
        run_id = start.json()["run_id"]

        def _is_completed():
            run = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
            assert run.status_code == 200, run.text
            return run.json().get("status") == "completed"

        _wait_until(_is_completed, timeout_s=10.0, poll_s=0.1)
        run = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
        assert run.status_code == 200, run.text
        ledger = client.get(f"/api/gateway/runs/{run_id}/ledger?after=0&limit=500", headers=headers)
        assert ledger.status_code == 200, ledger.text
        ledger_text = json.dumps(ledger.json().get("items") or [], sort_keys=True)
        artifacts = client.get(f"/api/gateway/runs/{run_id}/artifacts", headers=headers)
        assert artifacts.status_code == 200, artifacts.text
        items = artifacts.json().get("items") or []

    assert "gateway artifact node" in ledger_text
    assert any(isinstance(item, dict) and str(item.get("source_path") or "") == "source/notes.txt" for item in items)
    assert any(isinstance(item, dict) and str(item.get("content_type") or "") == "text/plain" for item in items)
    assert (workspace / "exports" / "copy.txt").read_text(encoding="utf-8") == "gateway artifact node\n"
