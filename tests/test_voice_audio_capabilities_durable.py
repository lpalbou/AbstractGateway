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

    headers = {"Authorization": f"Bearer {token}"}

    tts_audio = b"tts-bytes"
    transcript_text = "hello from stt"

    class _Reg:
        class _Voice:
            def tts(self, _text: str, **kwargs):
                store = kwargs["artifact_store"]
                run_id = kwargs.get("run_id")
                meta = store.store(tts_audio, content_type="audio/wav", run_id=str(run_id), tags={"kind": "tts"})
                return {"$artifact": meta.artifact_id, "content_type": "audio/wav", "filename": "tts.wav"}

        class _Audio:
            def transcribe(self, _audio: object, **_kwargs):
                return transcript_text

        voice = _Voice()
        audio = _Audio()

    monkeypatch.setattr(gateway_routes, "_get_gateway_capability_registry", lambda: _Reg())

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

        # Ledger includes both events.
        ledger = client.get(f"/api/gateway/runs/{run_id}/ledger?after=0&limit=500", headers=headers)
        assert ledger.status_code == 200, ledger.text
        items = ledger.json().get("items") or []
        emitted: set[str] = set()
        for it in items:
            eff = it.get("effect") if isinstance(it, dict) else None
            if not isinstance(eff, dict) or eff.get("type") != "emit_event":
                continue
            payload = eff.get("payload")
            if isinstance(payload, dict):
                name = payload.get("name")
                if isinstance(name, str) and name:
                    emitted.add(name)
        assert {"abstract.audio.transcript", "abstract.voice.tts"} <= emitted

    # Restart simulation: new TestClient should reload file-backed stores.
    with TestClient(app) as client2:
        # Artifacts still downloadable.
        transcript_content2 = client2.get(f"/api/gateway/runs/{run_id}/artifacts/{transcript_artifact_id}/content", headers=headers)
        assert transcript_content2.status_code == 200, transcript_content2.text
        assert transcript_content2.content == transcript_text.encode("utf-8")

        tts_content2 = client2.get(f"/api/gateway/runs/{run_id}/artifacts/{tts_artifact_id}/content", headers=headers)
        assert tts_content2.status_code == 200, tts_content2.text
        assert tts_content2.content == tts_audio

        # Ledger replay still contains events.
        ledger2 = client2.get(f"/api/gateway/runs/{run_id}/ledger?after=0&limit=500", headers=headers)
        assert ledger2.status_code == 200, ledger2.text
        items2 = ledger2.json().get("items") or []

        def _names(items: list[object]) -> set[str]:
            out: set[str] = set()
            for it in items:
                eff = it.get("effect") if isinstance(it, dict) else None
                if not isinstance(eff, dict):
                    continue
                if eff.get("type") != "emit_event":
                    continue
                payload = eff.get("payload")
                if isinstance(payload, dict):
                    name = payload.get("name")
                    if isinstance(name, str) and name:
                        out.add(name)
            return out

        names2 = _names(items2)
        assert {"abstract.audio.transcript", "abstract.voice.tts"} <= names2
        assert transcript_sha256 == hashlib.sha256(transcript_text.encode("utf-8")).hexdigest()
