"""R13.1: a chunked POST to a streaming endpoint never blocks the event loop.

The 2026-10-04 21:23 incident: Read aloud from Code opened through the console's
/apps/code/ proxy reached `POST /runs/{id}/voice/tts/stream` chunked (no
Content-Length). The security middleware buffered the body and replayed it with
a `receive` that, after the body, answered "empty body" forever without
suspending; StreamingResponse's disconnect listener spun on it ON the event
loop, health stopped answering and the watchdog exited the gateway (code 75).

A real uvicorn server, a fake speech engine (no model), the request sent
chunked like the proxy sends it, /api/health probed throughout.
"""

from __future__ import annotations

import base64
import http.client
import json
import socket
import threading
import time
import urllib.request

import pytest

pytestmark = pytest.mark.basic

TOKEN = "r13-min-token"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Engine:
    def stream_voice(self, parent_run_id, *, text, output, params, child_vars=None):
        def events():
            yield {"type": "runtime_start", "ok": True, "run_id": parent_run_id}
            for i in range(4):
                time.sleep(0.4)  # synthesis of one sentence, off the loop
                yield {"type": "audio", "sequence": i, "audio_b64": base64.b64encode(b"RIFF" + bytes([i])).decode()}
            yield {"type": "done", "ok": True, "chunks": 4}

        return events()


def test_a_chunked_read_aloud_stream_keeps_health_answering(tmp_path, monkeypatch):
    import uvicorn

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    flows = tmp_path / "flows"
    flows.mkdir()
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_VOICE_TTS_TIMEOUT_S", "30")

    from abstractgateway.app import app
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_run_facade", lambda: (_Engine(), None))
    monkeypatch.setattr(gateway_routes, "_configured_voice_output_defaults", lambda kind: {})
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(400):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1).read()
            break
        except Exception:
            time.sleep(0.05)

    samples: list = []
    stop = threading.Event()

    def hammer() -> None:
        while not stop.is_set():
            t0 = time.monotonic()
            try:
                with urllib.request.urlopen(base + "/api/health", timeout=2) as r:
                    r.read()
                samples.append((time.monotonic() - t0) * 1000)
            except Exception:
                samples.append(None)
            time.sleep(0.05)

    h = threading.Thread(target=hammer, daemon=True)
    h.start()
    events: list = []
    try:
        body = json.dumps({"text": "One. Two. Three. Four.", "format": "wav", "provider": "supertonic"}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        conn.request("POST", "/api/gateway/runs/session_memory_r13min/voice/tts/stream", body=iter([body]),
                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"}, encode_chunked=True)
        resp = conn.getresponse()
        assert resp.status == 200
        events = [json.loads(raw)["type"] for raw in resp]
        conn.close()
        time.sleep(0.3)
    finally:
        stop.set()
        h.join(5)
        server.should_exit = True
        thread.join(10)
    assert events == ["runtime_start", "audio", "audio", "audio", "audio", "done"], events
    assert samples and None not in samples, f"health stopped answering during the stream: {samples}"
    assert max(samples) < 1000, samples
