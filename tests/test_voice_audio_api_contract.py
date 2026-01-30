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

    from abstractcore.capabilities.errors import CapabilityUnavailableError

    headers = {"Authorization": f"Bearer {token}"}

    class _UnavailableReg:
        class _Voice:
            def tts(self, _text: str, **_kwargs):
                raise CapabilityUnavailableError(capability="voice", reason="voice capability unavailable")

        class _Audio:
            def transcribe(self, _audio: object, **_kwargs):
                raise CapabilityUnavailableError(capability="audio", reason="audio capability unavailable")

        voice = _Voice()
        audio = _Audio()

    monkeypatch.setattr(gateway_routes, "_get_gateway_capability_registry", lambda: _UnavailableReg())

    with TestClient(app) as client:
        # Auth required.
        unauth = client.post("/api/gateway/runs/session_memory_s1/voice/tts", json={"text": "hello"})
        assert unauth.status_code in {401, 403}, unauth.text

        # Capability unavailable: explicit error.
        tts_unavail = client.post("/api/gateway/runs/session_memory_s1/voice/tts", json={"text": "hello"}, headers=headers)
        assert tts_unavail.status_code == 400, tts_unavail.text
        assert "unavailable" in str(tts_unavail.json().get("detail") or "").lower()

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
        assert "unavailable" in str(stt_unavail.json().get("detail") or "").lower()

    # Success path: deterministic stub registry.
    class _OkReg:
        class _Voice:
            def tts(self, text: str, **kwargs):
                store = kwargs["artifact_store"]
                run_id = kwargs.get("run_id")
                fmt = str(kwargs.get("format") or "wav").strip().lower() or "wav"
                ct = "audio/wav" if fmt == "wav" else "audio/mpeg"
                content = f"tts:{text}".encode("utf-8")
                meta = store.store(content, content_type=ct, run_id=str(run_id))
                return {"$artifact": meta.artifact_id, "content_type": ct, "filename": f"tts.{fmt}"}

        class _Audio:
            def transcribe(self, _audio: object, **_kwargs):
                return "hello world"

        voice = _Voice()
        audio = _Audio()

    monkeypatch.setattr(gateway_routes, "_get_gateway_capability_registry", lambda: _OkReg())

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
