from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
        "created_at": "2026-01-26T00:00:00+00:00",
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


@pytest.mark.integration
def test_gateway_voice_audio_durable_artifacts_and_ledger_survive_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-voice-audio-durable", flow_id="root")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    from abstractgateway.app import app
    import abstractgateway.routes.gateway as gateway_routes
    from abstractruntime.core.models import RunStatus

    headers = {"Authorization": f"Bearer {token}"}

    tts_audio = b"tts-bytes"
    transcript_text = "hello from stt"

    class _RunFacade:
        def generate_voice(self, parent_run_id: str, *, text: str, output: dict[str, Any], params: dict[str, Any], child_vars=None):
            _ = (text, params, child_vars)
            child_run_id = "child-tts-durable"
            store = gateway_routes.get_gateway_service().stores.artifact_store
            meta = store.store(tts_audio, content_type="audio/wav", run_id=child_run_id, tags={"kind": "generated_media", "modality": "voice"})
            return SimpleNamespace(
                run_id=child_run_id,
                status=RunStatus.COMPLETED,
                error=None,
                output={
                    "result": {
                        "outputs": {
                            "voice": [
                                {
                                    "modality": "voice",
                                    "task": "tts",
                                    "provider": output.get("provider"),
                                    "model": output.get("model"),
                                    "content_type": "audio/wav",
                                    "format": "wav",
                                    "artifact_ref": {
                                        "$artifact": meta.artifact_id,
                                        "artifact_id": meta.artifact_id,
                                        "content_type": "audio/wav",
                                        "size_bytes": len(tts_audio),
                                    },
                                }
                            ]
                        }
                    }
                },
            )

        def transcribe_audio(self, parent_run_id: str, *, media, prompt=None, output=None, params=None, child_vars=None):
            _ = (parent_run_id, media, prompt, output, params, child_vars)
            return SimpleNamespace(
                run_id="child-stt-durable",
                status=RunStatus.COMPLETED,
                error=None,
                output={"result": {"text": transcript_text}},
            )

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_run_facade", lambda: (_RunFacade(), None))

    run_id = "session_memory_s1"
    transcript_artifact_id: str
    transcript_sha256: str
    tts_artifact_id: str

    with TestClient(app) as client:
        upload = client.post(
            "/api/gateway/attachments/upload",
            data={"session_id": "s1"},
            files={"file": ("clip.wav", b"RIFF....", "audio/wav")},
            headers=headers,
        )
        assert upload.status_code == 200, upload.text
        audio_ref = upload.json()["attachment"]
        assert isinstance(audio_ref.get("$artifact"), str) and audio_ref["$artifact"]

        stt = client.post(
            f"/api/gateway/runs/{run_id}/audio/transcribe",
            json={"audio_artifact": audio_ref, "request_id": "req-stt"},
            headers=headers,
        )
        assert stt.status_code == 200, stt.text
        stt_body = stt.json()
        assert stt_body.get("ok") is True
        assert stt_body.get("text") == transcript_text
        transcript_ref = stt_body.get("transcript_artifact") or {}
        assert isinstance(transcript_ref, dict)
        transcript_artifact_id = transcript_ref.get("$artifact")
        assert isinstance(transcript_artifact_id, str) and transcript_artifact_id
        transcript_sha256 = str(transcript_ref.get("sha256") or "")
        assert transcript_sha256 == hashlib.sha256(transcript_text.encode("utf-8")).hexdigest()

        tts = client.post(
            f"/api/gateway/runs/{run_id}/voice/tts",
            json={"text": "hello", "request_id": "req-tts"},
            headers=headers,
        )
        assert tts.status_code == 200, tts.text
        tts_body = tts.json()
        audio_out = tts_body.get("audio_artifact") or {}
        assert isinstance(audio_out, dict)
        tts_artifact_id = audio_out.get("$artifact")
        assert isinstance(tts_artifact_id, str) and tts_artifact_id

        # Sanity: artifacts downloadable before restart.
        transcript_content = client.get(f"/api/gateway/runs/{run_id}/artifacts/{transcript_artifact_id}/content", headers=headers)
        assert transcript_content.status_code == 200, transcript_content.text
        assert transcript_content.content == transcript_text.encode("utf-8")

        tts_content = client.get(f"/api/gateway/runs/{run_id}/artifacts/{tts_artifact_id}/content", headers=headers)
        assert tts_content.status_code == 200, tts_content.text
        assert tts_content.content == tts_audio

        # Parent ledger remains clean; the durable truth is the child run id returned by the direct routes.
        ledger = client.get(f"/api/gateway/runs/{run_id}/ledger?after=0&limit=500", headers=headers)
        assert ledger.status_code == 200, ledger.text
        items = ledger.json().get("items") or []
        emitted = {
            ((it.get("effect") or {}).get("payload") or {}).get("name")
            for it in items
            if isinstance(it, dict) and isinstance(it.get("effect"), dict)
        }
        assert "abstract.audio.transcript" not in emitted
        assert "abstract.voice.tts" not in emitted

    # Restart simulation: new TestClient should reload file-backed stores.
    with TestClient(app) as client2:
        # Artifacts still downloadable.
        transcript_content2 = client2.get(f"/api/gateway/runs/{run_id}/artifacts/{transcript_artifact_id}/content", headers=headers)
        assert transcript_content2.status_code == 200, transcript_content2.text
        assert transcript_content2.content == transcript_text.encode("utf-8")

        tts_content2 = client2.get(f"/api/gateway/runs/{run_id}/artifacts/{tts_artifact_id}/content", headers=headers)
        assert tts_content2.status_code == 200, tts_content2.text
        assert tts_content2.content == tts_audio

        # Parent ledger replay still stays free of synthetic media events after restart.
        ledger2 = client2.get(f"/api/gateway/runs/{run_id}/ledger?after=0&limit=500", headers=headers)
        assert ledger2.status_code == 200, ledger2.text
        items2 = ledger2.json().get("items") or []
        names2 = {
            ((it.get("effect") or {}).get("payload") or {}).get("name")
            for it in items2
            if isinstance(it, dict) and isinstance(it.get("effect"), dict)
        }
        assert "abstract.audio.transcript" not in names2
        assert "abstract.voice.tts" not in names2
        assert transcript_sha256 == hashlib.sha256(transcript_text.encode("utf-8")).hexdigest()
