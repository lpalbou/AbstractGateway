from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from abstractgateway.voice_artifacts import is_voice_playback

pytestmark = pytest.mark.basic


def test_only_stream_provenance_or_explicit_playback_purpose_enters_voice_space():
    tags = {"kind": "generated_media", "modality": "voice", "task": "tts"}
    legacy = SimpleNamespace(tags=tags, metadata={"capability_metadata": {"stream": {"chunk_format": "wav-segment"}}})
    assert is_voice_playback(legacy)
    assert is_voice_playback(SimpleNamespace(tags={"purpose": "voice_playback"}))
    assert not is_voice_playback(SimpleNamespace(tags=tags, metadata={}))  # deliberate audio output
    assert not is_voice_playback(SimpleNamespace(tags={"filename": "tts.wav", "kind": "attachment"}, metadata=legacy.metadata))


def test_file_lists_hide_new_and_legacy_playback_before_pagination_but_keep_audio_deliverables(tmp_path, monkeypatch):
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "voice-test")
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    from abstractgateway.app import app
    from abstractgateway.service import get_gateway_service
    from abstractruntime.core.models import RunState

    with TestClient(app, headers={"Authorization": "Bearer voice-test"}) as client:
        service = get_gateway_service()
        run = RunState.new(workflow_id="test", entry_node="start", session_id="chat")
        service.host.run_store.save(run)
        store = service.stores.artifact_store
        upload = store.store(b"uploaded-wave", run_id=run.run_id, content_type="audio/wav", tags={"kind": "attachment", "filename": "my-recording.wav", "session_id": "chat"})
        output = store.store(b"requested-wave", run_id=run.run_id, content_type="audio/wav", tags={"kind": "generated_media", "modality": "voice", "task": "tts", "filename": "requested.wav", "session_id": "chat"})
        for index in range(8):
            store.store(f"stream-{index}".encode(), run_id=run.run_id, content_type="audio/wav",
                        tags={"kind": "generated_media", "modality": "voice", "task": "tts", "session_id": "chat"},
                        metadata={"capability_metadata": {"stream": {"chunk_format": "wav-segment", "chunks": 2}}})
        new = store.store(b"new-stream", run_id=run.run_id, content_type="audio/wav",
                          tags={"kind": "generated_media", "modality": "voice", "task": "tts", "purpose": "voice_playback", "artifact_space": "voice", "path": "voice/new.wav", "filename": "new.wav", "session_id": "chat"})
        for path in [f"/api/gateway/runs/{run.run_id}/artifacts", "/api/gateway/sessions/chat/artifacts"]:
            response = client.get(path, params={"limit": 1})
            assert response.status_code == 200, response.text
            page = response.json()
            assert page["total"] == 2 and page["has_more"] and len(page["items"]) == 1, page
            second = client.get(path, params={"limit": 1, "offset": 1}).json()
            assert second["total"] == 2 and not second["has_more"]
            assert {page["items"][0]["artifact_id"], second["items"][0]["artifact_id"]} == {upload.artifact_id, output.artifact_id}
            everything = client.get(path, params={"include_voice": True, "limit": 0}).json()
            assert everything["total"] == 11
            playback = [row for row in everything["items"] if row["tags"].get("purpose") == "voice_playback"]
            assert len(playback) == 9
            assert all(row["source_path"].startswith("voice/") for row in playback)
        # Saved playback refs still resolve; the archive/search surface retains all artifacts.
        response = client.get(f"/api/gateway/runs/{run.run_id}/artifacts/{new.artifact_id}/content")
        assert response.status_code == 200 and response.content == b"new-stream"
        archive = client.get("/api/gateway/artifacts/search", params={"scope": "session", "session_id": "chat", "limit": 0}).json()
        assert archive["total"] == 11
        voices = client.get("/api/gateway/artifacts/search", params={"scope": "session", "session_id": "chat", "tags": "artifact_space=voice", "limit": 0}).json()
        assert voices["total"] == 9 and all(row["source_path"].startswith("voice/") for row in voices["items"])
