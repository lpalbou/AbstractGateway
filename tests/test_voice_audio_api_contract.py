from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException
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


@pytest.mark.basic
def test_voice_audio_routes_auth_and_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-voice-audio-contract", flow_id="root")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    from abstractgateway.app import app
    import abstractgateway.routes.gateway as gateway_routes

    headers = {"Authorization": f"Bearer {token}"}

    def _unavailable_voice_manager():
        raise HTTPException(
            status_code=400,
            detail='Voice/audio support is not available. Install with: pip install "abstractgateway[voice]" (or "abstractgateway[all]")',
        )

    monkeypatch.setattr(gateway_routes, "_get_gateway_voice_manager", _unavailable_voice_manager)

    with TestClient(app) as client:
        # Auth required.
        unauth = client.post("/api/gateway/runs/session_memory_s1/voice/tts", json={"text": "hello"})
        assert unauth.status_code in {401, 403}, unauth.text

        # Capability unavailable: explicit error.
        tts_unavail = client.post("/api/gateway/runs/session_memory_s1/voice/tts", json={"text": "hello"}, headers=headers)
        assert tts_unavail.status_code == 400, tts_unavail.text
        assert "abstractgateway[voice]" in str(tts_unavail.json().get("detail") or "").lower()

        # Upload an audio artifact for STT.
        upload = client.post(
            "/api/gateway/attachments/upload",
            data={"session_id": "s1"},
            files={"file": ("clip.wav", b"RIFF....", "audio/wav")},
            headers=headers,
        )
        assert upload.status_code == 200, upload.text
        audio_ref = upload.json()["attachment"]

        stt_unavail = client.post(
            "/api/gateway/runs/session_memory_s1/audio/transcribe",
            json={"audio_artifact": audio_ref},
            headers=headers,
        )
        assert stt_unavail.status_code == 400, stt_unavail.text
        assert "abstractgateway[voice]" in str(stt_unavail.json().get("detail") or "").lower()

    # Success path: deterministic stub voice manager.
    class _OkVoiceManager:
        def speak_to_bytes(self, text: str, *, format: str = "wav", voice: str | None = None) -> bytes:  # noqa: ARG002
            return f"tts:{text}".encode("utf-8")

        def transcribe_from_bytes(self, _audio_bytes: bytes, *, language: str | None = None) -> str:  # noqa: ARG002
            return "hello world"

    monkeypatch.setattr(gateway_routes, "_get_gateway_voice_manager", lambda: _OkVoiceManager())

    with TestClient(app) as client2:
        upload2 = client2.post(
            "/api/gateway/attachments/upload",
            data={"session_id": "s1"},
            files={"file": ("clip.wav", b"RIFF....", "audio/wav")},
            headers=headers,
        )
        assert upload2.status_code == 200, upload2.text
        audio_ref2 = upload2.json()["attachment"]

        stt = client2.post(
            "/api/gateway/runs/session_memory_s1/audio/transcribe",
            json={"audio_artifact": audio_ref2, "request_id": "req-stt-1"},
            headers=headers,
        )
        assert stt.status_code == 200, stt.text
        stt_body = stt.json()
        assert stt_body.get("ok") is True
        assert stt_body.get("run_id") == "session_memory_s1"
        assert stt_body.get("request_id") == "req-stt-1"
        assert stt_body.get("text") == "hello world"
        transcript = stt_body.get("transcript_artifact") or {}
        assert isinstance(transcript, dict)
        assert isinstance(transcript.get("$artifact"), str) and transcript["$artifact"]

        tts = client2.post(
            "/api/gateway/runs/session_memory_s1/voice/tts",
            json={"text": "hello", "request_id": "req-tts-1"},
            headers=headers,
        )
        assert tts.status_code == 200, tts.text
        tts_body = tts.json()
        assert tts_body.get("ok") is True
        assert tts_body.get("run_id") == "session_memory_s1"
        assert tts_body.get("request_id") == "req-tts-1"
        audio_out = tts_body.get("audio_artifact") or {}
        assert isinstance(audio_out, dict)
        assert isinstance(audio_out.get("$artifact"), str) and audio_out["$artifact"]


@pytest.mark.basic
def test_audio_transcribe_accepts_session_scoped_artifacts_for_any_run_in_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-audio-transcribe-scope", flow_id="root")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    from abstractgateway.app import app
    import abstractgateway.routes.gateway as gateway_routes

    headers = {"Authorization": f"Bearer {token}"}

    class _OkVoiceManager:
        def speak_to_bytes(self, text: str, *, format: str = "wav", voice: str | None = None) -> bytes:  # noqa: ARG002
            return f"tts:{text}".encode("utf-8")

        def transcribe_from_bytes(self, _audio_bytes: bytes, *, language: str | None = None) -> str:  # noqa: ARG002
            return "hello world"

    monkeypatch.setattr(gateway_routes, "_get_gateway_voice_manager", lambda: _OkVoiceManager())

    with TestClient(app) as client:
        # Start a normal run with session_id=s1.
        started = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": "bundle-audio-transcribe-scope", "session_id": "s1", "input_data": {"prompt": "hi"}},
            headers=headers,
        )
        assert started.status_code == 200, started.text
        run_id = started.json().get("run_id")
        assert isinstance(run_id, str) and run_id

        # Upload an audio attachment (stored under the session memory owner run id).
        upload = client.post(
            "/api/gateway/attachments/upload",
            data={"session_id": "s1"},
            files={"file": ("clip.wav", b"RIFF....", "audio/wav")},
            headers=headers,
        )
        assert upload.status_code == 200, upload.text
        audio_ref = upload.json()["attachment"]
        assert isinstance(audio_ref.get("$artifact"), str) and audio_ref["$artifact"]

        # Transcribe the session-scoped audio while targeting the started run id.
        stt = client.post(
            f"/api/gateway/runs/{run_id}/audio/transcribe",
            json={"audio_artifact": audio_ref, "request_id": "req-stt-1"},
            headers=headers,
        )
        assert stt.status_code == 200, stt.text
        body = stt.json()
        assert body.get("ok") is True
        assert body.get("run_id") == run_id
        assert body.get("request_id") == "req-stt-1"
        assert body.get("text") == "hello world"
