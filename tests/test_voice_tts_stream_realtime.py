"""Round 6 (R6.1): streamed speech is real streaming and never blocks the gateway.

A live uvicorn server (not TestClient) so the HTTP stream is observed as a
client sees it: the FIRST audio event reaches the client while the engine has
not synthesised the second segment yet, and /api/health answers during
synthesis — both while the engine is blocked inside its generator, and while
the engine's SETUP blocks (the run facade's `stream_voice` call takes the
engine lock before returning its iterator).
"""
from __future__ import annotations

import base64
import json
import socket
import threading
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _reply(words: int) -> str:
    sentence = "The gateway reads this reply aloud while the next sentence is synthesised."
    return " ".join([sentence] * (words // len(sentence.split()) + 1))


class _Engine:
    """Yields one wav segment per sentence; segment 2 waits for the test's go."""

    def __init__(self, *, block_setup: bool = False):
        self.synthesised = 0
        self.go = threading.Event()
        self.setup_go = threading.Event()
        self.block_setup = block_setup
        self.text = ""

    def stream_voice(self, parent_run_id, *, text, output, params, child_vars=None):
        self.text = text
        if self.block_setup:
            # A real engine takes its lock here (a second reply waits for the first).
            assert self.setup_go.wait(10), "setup never released"
        engine = self

        def _events():
            yield {"type": "runtime_start", "ok": True, "run_id": parent_run_id}
            for i in range(3):
                if i == 1:
                    assert engine.go.wait(10), "the client never received the first segment"
                engine.synthesised += 1
                yield {"type": "audio", "sequence": i, "audio_b64": base64.b64encode(b"RIFF" + bytes([i])).decode()}
            yield {"type": "done", "ok": True, "chunks": 3}

        return _events()


def _serve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: _Engine):
    import uvicorn

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    flows = tmp_path / "flows"
    flows.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_VOICE_TTS_TIMEOUT_S", "30")

    from abstractgateway.app import app
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_run_facade", lambda: (engine, None))
    monkeypatch.setattr(gateway_routes, "_configured_voice_output_defaults", lambda kind: {})
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(200):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1).read()
            break
        except Exception:
            time.sleep(0.05)
    return server, thread, base


def _health_ms(base: str) -> float:
    t0 = time.monotonic()
    with urllib.request.urlopen(base + "/api/health", timeout=5) as r:
        assert r.status == 200
    return (time.monotonic() - t0) * 1000


def _open_stream(base: str, text: str):
    req = urllib.request.Request(
        base + "/api/gateway/runs/session_memory_s1/voice/tts/stream",
        data=json.dumps({"text": text, "format": "wav", "provider": "supertonic"}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer t"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=20)


def test_first_audio_reaches_the_client_before_the_second_segment_is_synthesised(tmp_path, monkeypatch):
    engine = _Engine()
    server, thread, base = _serve(tmp_path, monkeypatch, engine)
    try:
        with _open_stream(base, _reply(2000)) as resp:
            first_audio = None
            for raw in resp:
                evt = json.loads(raw)
                if evt.get("type") == "audio":
                    first_audio = evt
                    break
            assert first_audio is not None and first_audio["sequence"] == 0
            assert engine.synthesised == 1, "the second segment was synthesised before the first reached the client"
            # The gateway answers while synthesis is in progress.
            assert _health_ms(base) < 1000
            engine.go.set()
            rest = [json.loads(raw) for raw in resp]
        assert [e.get("sequence") for e in rest if e.get("type") == "audio"] == [1, 2]
        assert rest[-1]["type"] == "done"
        assert len(engine.text.split()) >= 2000
    finally:
        engine.go.set()
        server.should_exit = True
        thread.join(10)


def test_health_answers_while_the_engine_setup_blocks(tmp_path, monkeypatch):
    engine = _Engine(block_setup=True)
    engine.go.set()
    server, thread, base = _serve(tmp_path, monkeypatch, engine)
    try:
        result: dict = {}

        def _client():
            with _open_stream(base, "Hello there. Second sentence.") as resp:
                result["events"] = [json.loads(raw) for raw in resp]

        t = threading.Thread(target=_client, daemon=True)
        t.start()
        time.sleep(0.3)  # the request is now inside stream_voice's blocked setup
        assert _health_ms(base) < 1000, "the event loop is blocked by the voice engine's setup"
        engine.setup_go.set()
        t.join(10)
        assert result["events"][-1]["type"] == "done"
    finally:
        engine.setup_go.set()
        server.should_exit = True
        thread.join(10)
