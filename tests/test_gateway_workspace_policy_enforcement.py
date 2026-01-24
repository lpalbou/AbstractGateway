from __future__ import annotations

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
        "bundle_version": "0.0.0",
        "created_at": "2026-01-19T00:00:00+00:00",
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


def _make_client(*, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict[str, str]]:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-policy", flow_id="root")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    from abstractgateway.app import app

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    return client, headers


def test_server_file_endpoints_ignore_client_scope_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "inside.txt").write_text("inside\n", encoding="utf-8")

    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)
    secret = outside / "secret.txt"
    secret.write_text("secret\n", encoding="utf-8")

    monkeypatch.setenv("ABSTRACTGATEWAY_WORKSPACE_DIR", str(ws))

    client, headers = _make_client(tmp_path=tmp_path, monkeypatch=monkeypatch)
    with client:
        r1 = client.get(
            "/api/gateway/files/search",
            params={"query": "secret", "limit": 20, "workspace_root": str(outside)},
            headers=headers,
        )
        assert r1.status_code == 200, r1.text
        items = r1.json().get("items") or []
        paths = {it.get("path") for it in items if isinstance(it, dict)}
        assert "secret.txt" not in paths

        r2 = client.get(
            "/api/gateway/files/read",
            params={"path": str(secret), "workspace_root": str(outside)},
            headers=headers,
        )
        assert r2.status_code == 403, r2.text

        r3 = client.post(
            "/api/gateway/attachments/ingest",
            json={"session_id": "s1", "path": str(secret), "workspace_root": str(outside)},
            headers=headers,
        )
        assert r3.status_code == 403, r3.text


def test_workspace_policy_endpoint_exposes_mount_names_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    notes = tmp_path / "notes"
    notes.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("ABSTRACTGATEWAY_WORKSPACE_DIR", str(ws))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKSPACE_MOUNTS", f"notes={notes}\n")

    client, headers = _make_client(tmp_path=tmp_path, monkeypatch=monkeypatch)
    with client:
        r = client.get("/api/gateway/workspace/policy", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("ok") is True
        policy = body.get("policy") or {}
        assert isinstance(policy, dict)
        assert policy.get("target") == "server"
        mounts = policy.get("mounts") or []
        assert {"name": "notes"} in mounts
        assert str(notes) not in json.dumps(body)


def test_sanitize_run_workspace_policy_rejects_outside_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("ABSTRACTGATEWAY_WORKSPACE_DIR", str(ws))

    from abstractgateway.routes.gateway import _sanitize_run_workspace_policy

    sanitized = _sanitize_run_workspace_policy(
        {
            "workspace_root": str(outside),
            "workspace_access_mode": "all_except_ignored",
            "workspace_allowed_paths": [str(outside)],
        }
    )
    assert "workspace_root" not in sanitized
    assert sanitized.get("workspace_access_mode") == "workspace_only"
    assert "workspace_allowed_paths" not in sanitized
