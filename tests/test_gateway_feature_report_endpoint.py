from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.integration


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
                "data": {
                    "nodeType": "on_flow_end",
                    "label": "On Flow End",
                    "inputs": [{"id": "exec-in", "label": "", "type": "execution"}],
                    "outputs": [],
                },
            },
        ],
        "edges": [{"id": "e1", "source": "node-1", "sourceHandle": "exec-out", "target": "node-2", "targetHandle": "exec-in"}],
        "entryNode": "node-1",
    }

    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-01-31T00:00:00+00:00",
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


def test_gateway_feature_report_endpoint_creates_template_and_collision_safe_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-features", flow_id="root")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    description = "../etc/passwd should support foo"

    with TestClient(app) as client:
        r1 = client.post(
            "/api/gateway/features/report",
            headers=headers,
            json={"session_id": "s1", "description": description, "active_run_id": "r1", "workflow_id": "bundle-features:root"},
        )
        assert r1.status_code == 200, r1.text
        body1 = r1.json()
        assert body1.get("ok") is True
        assert body1.get("session_id") == "s1"
        assert body1.get("session_memory_run_id") == "session_memory_s1"

        filename1 = str(body1.get("filename") or "").strip()
        assert filename1
        assert "/" not in filename1
        assert "\\" not in filename1

        feature_dir = runtime_dir / "feature_requests"
        template_path = feature_dir / "template.md"
        assert template_path.exists()

        report1 = feature_dir / filename1
        assert report1.exists()
        content = report1.read_text(encoding="utf-8")
        assert "Session ID: s1" in content
        assert "Session memory run ID: session_memory_s1" in content
        assert "User Request" in content
        assert f"    {description}" in content

        # Collision safety: calling twice must not overwrite the first file.
        r2 = client.post(
            "/api/gateway/features/report",
            headers=headers,
            json={"session_id": "s1", "description": description, "active_run_id": "r1", "workflow_id": "bundle-features:root"},
        )
        assert r2.status_code == 200, r2.text
        body2 = r2.json()
        filename2 = str(body2.get("filename") or "").strip()
        assert filename2
        assert filename2 != filename1
        report2 = feature_dir / filename2
        assert report2.exists()


def test_gateway_feature_report_endpoint_auto_bridges_to_typed_proposed_backlog_when_repo_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-features-bridge", flow_id="root")

    repo_root = tmp_path / "repo"
    (repo_root / "docs" / "backlog").mkdir(parents=True, exist_ok=True)
    (repo_root / "docs" / "backlog" / "template.md").write_text("# {ID}-{Package}: [TASK] {Title}\n", encoding="utf-8")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(repo_root))

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    description = "This is a very long feature description " + ("x" * 160) + " TAIL-123"

    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/features/report",
            headers=headers,
            json={
                "session_id": "s1",
                "description": description,
                "active_run_id": "r1",
                "workflow_id": "bundle-features-bridge:root",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("ok") is True

        report_filename = str(body.get("filename") or "").strip()
        assert report_filename
        report_path = runtime_dir / "feature_requests" / report_filename
        assert report_path.exists()
        report_md = report_path.read_text(encoding="utf-8")
        assert "TAIL-123" in report_md

        backlog_rel = str(body.get("proposed_backlog_relpath") or "").strip()
        backlog_id = body.get("proposed_backlog_item_id")
        assert backlog_rel
        assert isinstance(backlog_id, int) and backlog_id > 0

        backlog_path = (repo_root / backlog_rel).resolve()
        assert backlog_path.exists()
        md = backlog_path.read_text(encoding="utf-8")
        assert "> Type: feature" in md
        assert f"> Source report relpath: feature_requests/{report_filename}" in md
        assert "TAIL-123" in md
        assert f"    {description}" in md
