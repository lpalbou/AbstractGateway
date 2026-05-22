from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

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


def _patch_gateway_capabilities(
    monkeypatch: pytest.MonkeyPatch,
    gateway_routes,
    *,
    tts_bytes: bytes = b"tts:hello",
    transcript: str = "hello world",
    calls: dict | None = None,
) -> None:
    calls = calls if calls is not None else {}
    calls.setdefault("tts", [])
    calls.setdefault("stt", [])

    from abstractruntime.core.models import RunStatus

    class _RunFacade:
        def generate_voice(self, parent_run_id: str, *, text: str, output: Dict[str, Any], params: Dict[str, Any], child_vars=None):
            child_run_id = "child-tts-1"
            calls["tts"].append(
                {
                    "parent_run_id": parent_run_id,
                    "text": text,
                    "voice": output.get("voice"),
                    "profile": output.get("profile"),
                    "provider": output.get("provider"),
                    "model": output.get("model"),
                    "format": output.get("format"),
                    "speed": output.get("speed"),
                    "quality_preset": output.get("quality_preset"),
                    "instructions": output.get("instructions"),
                    "params": params,
                    "child_vars": child_vars,
                }
            )
            svc = gateway_routes.get_gateway_service()
            store = svc.stores.artifact_store
            meta = store.store(tts_bytes, content_type="audio/wav", run_id=child_run_id, tags={"kind": "generated_media", "modality": "voice"})
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
                                    "voice": output.get("voice"),
                                    "profile": output.get("profile"),
                                    "content_type": "audio/wav",
                                    "format": "wav",
                                    "artifact_ref": {
                                        "$artifact": meta.artifact_id,
                                        "artifact_id": meta.artifact_id,
                                        "content_type": "audio/wav",
                                        "size_bytes": len(tts_bytes),
                                    },
                                }
                            ]
                        }
                    }
                },
            )

        def transcribe_audio(self, parent_run_id: str, *, media, prompt=None, output=None, params=None, child_vars=None):
            calls["stt"].append(
                {
                    "parent_run_id": parent_run_id,
                    "media": media,
                    "prompt": prompt,
                    "output": dict(output or {}),
                    "params": params,
                    "child_vars": child_vars,
                }
            )
            return SimpleNamespace(
                run_id="child-stt-1",
                status=RunStatus.COMPLETED,
                error=None,
                output={"result": {"text": transcript}},
            )

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_run_facade", lambda: (_RunFacade(), None))


@pytest.mark.basic
def test_gateway_env_bool_supports_voice_multi_key_and_legacy_default(monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.delenv("AGW_TEST_BOOL_A", raising=False)
    monkeypatch.delenv("AGW_TEST_BOOL_B", raising=False)

    assert gateway_routes._env_bool("AGW_TEST_BOOL_A", "AGW_TEST_BOOL_B", default=True) is True
    assert gateway_routes._env_bool("AGW_TEST_BOOL_A", True) is True

    monkeypatch.setenv("AGW_TEST_BOOL_B", "off")
    assert gateway_routes._env_bool("AGW_TEST_BOOL_A", "AGW_TEST_BOOL_B", default=True) is False

    monkeypatch.setenv("AGW_TEST_BOOL_A", "yes")
    assert gateway_routes._env_bool("AGW_TEST_BOOL_A", "AGW_TEST_BOOL_B", default=False) is True


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
    calls: dict[str, list[dict[str, Any]]] = {}

    monkeypatch.setattr(
        gateway_routes,
        "_gateway_abstractcore_run_facade",
        lambda: (None, "Voice/audio support is not available. Install/repair with: pip install abstractgateway"),
    )

    with TestClient(app) as client:
        # Auth required.
        unauth = client.post("/api/gateway/runs/session_memory_s1/voice/tts", json={"text": "hello"})
        assert unauth.status_code in {401, 403}, unauth.text

        # Capability unavailable: explicit error.
        tts_unavail = client.post("/api/gateway/runs/session_memory_s1/voice/tts", json={"text": "hello"}, headers=headers)
        assert tts_unavail.status_code == 503, tts_unavail.text
        assert "pip install abstractgateway" in str(tts_unavail.json().get("detail") or "").lower()

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
        assert stt_unavail.status_code == 503, stt_unavail.text
        assert "pip install abstractgateway" in str(stt_unavail.json().get("detail") or "").lower()

    _patch_gateway_capabilities(monkeypatch, gateway_routes, tts_bytes=b"tts:hello", transcript="hello world", calls=calls)

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
            json={
                "audio_artifact": audio_ref2,
                "prompt": "French product names may appear.",
                "response_format": "verbose_json",
                "temperature": 0.2,
                "format": "wav",
                "request_id": "req-stt-1",
            },
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
        assert calls["stt"][0]["prompt"] == "French product names may appear."
        assert calls["stt"][0]["output"]["prompt"] == "French product names may appear."
        assert calls["stt"][0]["output"]["response_format"] == "verbose_json"
        assert calls["stt"][0]["output"]["temperature"] == 0.2
        assert calls["stt"][0]["output"]["format"] == "wav"

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

    _patch_gateway_capabilities(monkeypatch, gateway_routes, transcript="hello world")

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


@pytest.mark.basic
def test_voice_tts_offloads_synthesis_to_threadpool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-voice-threadpool", flow_id="root")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    from abstractgateway.app import app
    import abstractgateway.routes.gateway as gateway_routes

    headers = {"Authorization": f"Bearer {token}"}
    offloaded: list[str] = []

    async def _fake_to_thread(func, /, *args, **kwargs):
        offloaded.append(getattr(func, "__name__", repr(func)))
        return func(*args, **kwargs)

    _patch_gateway_capabilities(monkeypatch, gateway_routes, tts_bytes=b"tts:hello")
    monkeypatch.setattr(gateway_routes.asyncio, "to_thread", _fake_to_thread)

    with TestClient(app) as client:
        tts = client.post(
            "/api/gateway/runs/session_memory_s1/voice/tts",
            json={"text": "hello", "request_id": "req-tts-threadpool"},
            headers=headers,
        )
        assert tts.status_code == 200, tts.text

    assert offloaded


@pytest.mark.basic
def test_voice_tts_treats_selected_profile_voice_as_profile_not_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-voice-profile-routing", flow_id="root")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    from abstractgateway.app import app
    import abstractgateway.routes.gateway as gateway_routes

    headers = {"Authorization": f"Bearer {token}"}
    calls: dict = {}
    _patch_gateway_capabilities(monkeypatch, gateway_routes, tts_bytes=b"tts:hello:wav", calls=calls)

    with TestClient(app) as client:
        tts = client.post(
            "/api/gateway/runs/session_memory_s1/voice/tts",
            json={"text": "hello", "voice": "amy", "request_id": "req-tts-profile"},
            headers=headers,
        )

    assert tts.status_code == 200, tts.text
    assert calls["tts"][0]["voice"] == "amy"
    assert calls["tts"][0]["profile"] is None
    body = tts.json()
    assert body["audio_artifact"]["content_type"] == "audio/wav"


@pytest.mark.basic
def test_voice_tts_applies_explicit_provider_before_profile_voice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-voice-provider-routing", flow_id="root")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    from abstractgateway.app import app
    import abstractgateway.routes.gateway as gateway_routes

    headers = {"Authorization": f"Bearer {token}"}
    calls: dict = {}
    _patch_gateway_capabilities(monkeypatch, gateway_routes, tts_bytes=b"tts:hello:wav", calls=calls)

    with TestClient(app) as client:
        tts = client.post(
            "/api/gateway/runs/session_memory_s1/voice/tts",
            json={
                "text": "hello",
                "provider": "supertonic",
                "model": "supertonic-3",
                "voice": "M1",
                "request_id": "req-tts-provider-profile",
            },
            headers=headers,
        )

    assert tts.status_code == 200, tts.text
    assert calls["tts"][0]["provider"] == "supertonic"
    assert calls["tts"][0]["model"] == "supertonic-3"
    assert calls["tts"][0]["voice"] == "M1"
