"""POST /runs/{id}/voice/tts/stream: the text segment fields reach the client unchanged (exact resume).

AbstractVoice (ADR 0009) names each audio line's text segment (`segment`, `text_start`,
`text_end`: code-point offsets into the request text) and closes each segment with a
`segment_end` line. A client that lost the stream re-requests the text from `text_end` of
the last `segment_end` it received. The gateway must hand the engine the text exactly as
sent (no strip: the offsets index the client's own text) and forward the fields untouched.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic


def test_segment_fields_and_segment_end_lines_pass_through_and_the_text_is_not_altered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    token = "segments-stream-token"
    data = tmp_path / "runtime"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    flows = tmp_path / "flows"
    flows.mkdir()
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUDIT_LOG", "1")

    from abstractgateway.app import app
    from abstractgateway.service import get_gateway_service
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_configured_voice_output_defaults", lambda kind: {"provider": "fake-tts"})

    text = "\n  Bonjour ! Il est 14 h 30 👍🏽.  Déjà fini ?  "
    first, second = text.index("Bonjour"), text.index("Déjà")
    spans = [(first, text.index("👍🏽.") + 3), (second, text.rindex("?") + 1)]
    wav = b"RIFF" + bytes(60)
    seen: dict = {}

    class _FakeSpeech:
        def stream_tts(self, *, text, output=None, params=None):
            seen["text"] = text
            seq = 0
            for index, (start, end) in enumerate(spans):
                for _ in range(2):  # two 2 s slices of one segment
                    yield {"type": "audio", "sequence": seq, "content_type": "audio/wav", "audio": wav,
                           "size_bytes": len(wav), "segment": index, "text_start": start, "text_end": end}
                    seq += 1
                yield {"type": "segment_end", "schema": "abstractvoice.tts_stream.segment_end.v1",
                       "segment": index, "text_start": start, "text_end": end}
            yield {"type": "done", "ok": True, "chunks": seq, "segments": len(spans)}

    with TestClient(app) as client:
        h = {"Authorization": f"Bearer {token}"}
        client.get("/api/gateway/runs/session_memory_segments", headers=h)  # boot gate
        runtime = get_gateway_service().host.runtime
        monkeypatch.setattr(runtime, "_abstractcore_llm_client", _FakeSpeech(), raising=False)
        resp = client.post("/api/gateway/runs/session_memory_segments/voice/tts/stream", headers=h, json={"text": text})
        assert resp.status_code == 200, resp.text
        lines = [json.loads(line) for line in resp.text.splitlines() if line.strip()]

    assert seen["text"] == text  # exactly as sent: the offsets index the client's own text
    assert [line["type"] for line in lines] == [
        "runtime_start", "audio", "audio", "segment_end", "audio", "audio", "segment_end", "done",
    ]
    audio = [line for line in lines if line["type"] == "audio"]
    assert [(a["segment"], a["text_start"], a["text_end"]) for a in audio] == [
        (0, *spans[0]), (0, *spans[0]), (1, *spans[1]), (1, *spans[1]),
    ]
    ends = [line for line in lines if line["type"] == "segment_end"]
    assert [(e["segment"], e["text_start"], e["text_end"], e["schema"]) for e in ends] == [
        (0, *spans[0], "abstractvoice.tts_stream.segment_end.v1"),
        (1, *spans[1], "abstractvoice.tts_stream.segment_end.v1"),
    ]
    assert text[spans[0][0]:spans[0][1]] == "Bonjour ! Il est 14 h 30 👍🏽."
    assert lines[-1]["segments"] == 2

    entries = [json.loads(line) for line in (data / "audit_log.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    detail = next(e for e in entries if str(e.get("path", "")).endswith("/voice/tts/stream"))["voice_stream"]
    assert detail["segments"] == 2 and detail["audio_lines"] == 4 and detail["ended"] == "done", detail
    assert detail["text_chars"] == len(text)


def test_a_blank_text_is_still_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    token = "segments-blank-token"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    flows = tmp_path / "flows"
    flows.mkdir()
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")

    from abstractgateway.app import app

    with TestClient(app) as client:
        h = {"Authorization": f"Bearer {token}"}
        client.get("/api/gateway/runs/session_memory_blank", headers=h)
        resp = client.post("/api/gateway/runs/session_memory_blank/voice/tts/stream", headers=h, json={"text": " \n\t "})
    assert resp.status_code == 400 and "text is required" in resp.text
