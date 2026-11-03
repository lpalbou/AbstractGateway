from __future__ import annotations

import hashlib
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
        "created_at": "2026-01-16T00:00:00+00:00",
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


def test_gateway_attachments_upload_creates_session_scoped_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-attachments-upload", flow_id="root")

    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKSPACE_DIR", str(ws))

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    # Regression: security middleware must not apply its small default write-body cap (256KB)
    # to multipart uploads. Use a payload >256KB to catch 413 regressions.
    payload = b"a" * 300_000
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/attachments/upload",
            data={"session_id": "s1"},
            files={"file": ("notes.txt", payload, "text/plain")},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("ok") is True
        assert body.get("run_id") == "session_memory_s1"

        attachment = body.get("attachment") or {}
        assert isinstance(attachment, dict)
        artifact_id = attachment.get("$artifact")
        assert isinstance(artifact_id, str) and artifact_id.strip()
        assert attachment.get("target") == "client"
        assert attachment.get("source_path") == "client:notes.txt"
        sha256 = attachment.get("sha256")
        assert isinstance(sha256, str) and sha256.strip()
        assert sha256 == hashlib.sha256(payload).hexdigest()

        r2 = client.get("/api/gateway/runs/session_memory_s1/artifacts", headers=headers)
        assert r2.status_code == 200, r2.text
        items = r2.json().get("items") or []
        found = next((it for it in items if isinstance(it, dict) and it.get("artifact_id") == artifact_id), None)
        assert isinstance(found, dict)
        tags = found.get("tags") or {}
        assert isinstance(tags, dict)
        assert tags.get("target") == "client"
        assert tags.get("path") == "client:notes.txt"

        r3 = client.get(f"/api/gateway/runs/session_memory_s1/artifacts/{artifact_id}/content", headers=headers)
        assert r3.status_code == 200, r3.text
        assert r3.content == payload


def test_gateway_attachments_upload_preserves_client_relative_source_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-attachments-upload-path", flow_id="root")

    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKSPACE_DIR", str(ws))

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/attachments/upload",
            data={"session_id": "s2", "source_path": r"folder\\sub/../sub/report.txt"},
            files={"file": ("report.txt", b"hello", "text/plain")},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        attachment = body.get("attachment") or {}
        assert isinstance(attachment, dict)
        assert attachment.get("source_path") == "client:folder/sub/report.txt"

        artifact_id = attachment.get("$artifact")
        assert isinstance(artifact_id, str) and artifact_id.strip()

        listing = client.get("/api/gateway/runs/session_memory_s2/artifacts", headers=headers)
        assert listing.status_code == 200, listing.text
        items = listing.json().get("items") or []
        found = next((it for it in items if isinstance(it, dict) and it.get("artifact_id") == artifact_id), None)
        assert isinstance(found, dict)
        tags = found.get("tags") or {}
        assert isinstance(tags, dict)
        assert tags.get("path") == "client:folder/sub/report.txt"


def test_gateway_attachments_upload_stamps_dictation_recordings_as_voice_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR 0047: a spoken question uploaded under the reserved voice/dictation/ path is voice transport —
    tagged purpose=voice_dictation / artifact_space=voice and hidden from the default listing; a normal upload is not."""
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-attachments-dictation", flow_id="root")
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    from abstractgateway.app import app

    headers = {"Authorization": "Bearer t"}
    with TestClient(app) as client:
        spoken = client.post(
            "/api/gateway/attachments/upload",
            data={"session_id": "s3", "source_path": "voice/dictation/2f1c9d3e-1111-4222-8333-444455556666.wav"},
            files={"file": ("2f1c9d3e-1111-4222-8333-444455556666.wav", b"RIFF-spoken", "audio/wav")},
            headers=headers,
        )
        assert spoken.status_code == 200, spoken.text
        spoken_id = spoken.json()["attachment"]["$artifact"]
        assert spoken.json()["attachment"]["source_path"] == "client:voice/dictation/2f1c9d3e-1111-4222-8333-444455556666.wav"
        memo = client.post(
            "/api/gateway/attachments/upload",
            data={"session_id": "s3", "source_path": "memos/my-recording.wav"},
            files={"file": ("my-recording.wav", b"RIFF-memo", "audio/wav")},
            headers=headers,
        )
        assert memo.status_code == 200, memo.text
        memo_id = memo.json()["attachment"]["$artifact"]

        default_items = client.get("/api/gateway/runs/session_memory_s3/artifacts", headers=headers).json()["items"]
        assert [it["artifact_id"] for it in default_items] == [memo_id], default_items
        everything = client.get("/api/gateway/runs/session_memory_s3/artifacts", params={"include_voice": True}, headers=headers).json()["items"]
        by_id = {it["artifact_id"]: it["tags"] for it in everything}
        assert by_id[spoken_id]["purpose"] == "voice_dictation" and by_id[spoken_id]["artifact_space"] == "voice"
        assert by_id[spoken_id]["kind"] == "attachment" and by_id[spoken_id]["path"] == "client:voice/dictation/2f1c9d3e-1111-4222-8333-444455556666.wav"
        assert "purpose" not in by_id[memo_id] and "artifact_space" not in by_id[memo_id]
        # The recording itself still resolves (transport is hidden, never deleted).
        content = client.get(f"/api/gateway/runs/session_memory_s3/artifacts/{spoken_id}/content", headers=headers)
        assert content.status_code == 200 and content.content == b"RIFF-spoken"
