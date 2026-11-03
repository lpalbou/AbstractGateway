"""voice_stream.py: keep-alive lines, the inter-event watchdog, and the stats the audit reads.

2026-11-01 (watch "Voice unavailable" mid-answer): the engine was fine, the
client's idle timer was not. While the engine has produced nothing for
KEEPALIVE_S the body now writes a `keepalive` line; the watchdog still counts
ENGINE silence only; `stats` tells the audit what the stream did.
"""
from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List

import pytest

from abstractgateway import voice_stream
from abstractgateway.voice_stream import new_stream_stats, open_voice_stream
from timing import timing_budget  # noqa: E402 - tests/timing.py (wall-clock budgets)

pytestmark = pytest.mark.basic


class _Engine:
    """A blocking engine iterator: `plan` is a list of (delay_s, event)."""

    def __init__(self, plan: List[Any]) -> None:
        self.plan = plan
        self.closed = threading.Event()

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        try:
            for delay, event in self.plan:
                time.sleep(delay)
                yield event
        finally:
            self.closed.set()

    def close(self) -> None:
        self.closed.set()


def _encode(event: Dict[str, Any]) -> bytes:
    """The route's encoder shape: WAV bytes leave as base64 (routes/gateway.py `_voice_stream_jsonl`)."""
    payload = dict(event)
    audio = payload.pop("audio", None)
    if isinstance(audio, (bytes, bytearray)):
        payload["audio_b64"] = base64.b64encode(bytes(audio)).decode("ascii")
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")


async def _collect(engine: _Engine, *, timeout_s: float, keepalive_s: float, stats: Dict[str, Any] | None = None, max_lines: int | None = None):
    sem = asyncio.Semaphore(4)
    response = await open_voice_stream(
        setup=lambda: engine,
        request_id="req-1",
        timeout_s=timeout_s,
        sem=sem,
        max_concurrency=4,
        on_idle_timeout=lambda: "watchdog: the engine said nothing",
        setup_error=lambda exc: RuntimeError(f"setup failed: {exc}"),
        busy_error=lambda n: RuntimeError("busy"),
        encode=_encode,
        keepalive_s=keepalive_s,
        stats=stats,
    )
    lines: List[Dict[str, Any]] = []
    stamps: List[float] = []
    async for raw in response.body_iterator:
        lines.append(json.loads(raw.decode("utf-8")))
        stamps.append(time.monotonic())
        if max_lines is not None and len(lines) >= max_lines:
            break
    return lines, stamps


def _audio(i: int) -> Dict[str, Any]:
    return {"type": "audio", "sequence": i, "audio": b"RIFF" + bytes(100), "size_bytes": 104}


def test_a_slow_engine_gets_keepalive_lines_then_its_audio() -> None:
    # No event for 0.6 s with a 0.05 s keep-alive interval: >= 5 keep-alives, then the audio, then done.
    engine = _Engine([(0.6, _audio(0)), (0.0, {"type": "done", "ok": True, "chunks": 1})])
    stats = new_stream_stats()
    lines, _ = asyncio.run(_collect(engine, timeout_s=30.0, keepalive_s=0.05, stats=stats))

    kinds = [line["type"] for line in lines]
    assert kinds.count("keepalive") >= 5, kinds
    assert kinds[-2:] == ["audio", "done"], kinds
    assert all(k == "keepalive" for k in kinds[:-2]), kinds
    keep = next(line for line in lines if line["type"] == "keepalive")
    assert keep == {"type": "keepalive", "ok": True, "request_id": "req-1"}
    assert stats["keepalives"] == kinds.count("keepalive")
    assert stats["audio_lines"] == 1 and stats["audio_bytes"] == 104
    assert stats["lines"] == len(lines) and stats["ended"] == "done"
    assert engine.closed.wait(2.0)


def test_keepalives_never_fire_the_watchdog() -> None:
    # Engine events every 0.2 s, watchdog at 0.5 s, keep-alives every 0.05 s: 2 s of stream, no error line.
    plan = [(0.2, _audio(i)) for i in range(10)] + [(0.0, {"type": "done", "ok": True, "chunks": 10})]
    lines, _ = asyncio.run(_collect(_Engine(plan), timeout_s=0.5, keepalive_s=0.05))

    kinds = [line["type"] for line in lines]
    assert "error" not in kinds, kinds
    assert kinds.count("audio") == 10 and kinds[-1] == "done"
    assert kinds.count("keepalive") >= 10  # keep-alives were flowing the whole time


def test_the_watchdog_still_fires_after_timeout_s_of_engine_silence() -> None:
    # Keep-alives flow, but the engine is silent for 2 s: the watchdog (0.4 s) ends the stream with its error line.
    engine = _Engine([(2.0, _audio(0))])
    stats = new_stream_stats()
    started = time.monotonic()
    lines, stamps = asyncio.run(_collect(engine, timeout_s=0.4, keepalive_s=0.05, stats=stats))

    kinds = [line["type"] for line in lines]
    assert kinds[-1] == "error" and lines[-1]["watchdog_timeout"] is True, lines[-1]
    assert lines[-1]["error"] == "watchdog: the engine said nothing"
    assert kinds.count("keepalive") >= 3, kinds
    # The engine's first audio comes after 2 s: the watchdog ended the stream before it (no audio
    # line), and never before its own timeout_s (a timer does not fire early).
    assert "audio" not in kinds, "the watchdog fired only after the engine's delay"
    assert stamps[-1] - started >= 0.35, "the watchdog fired before timeout_s"
    timing_budget(stamps[-1] - started, 1.5, "watchdog firing after timeout_s of engine silence", inclusive=True)
    assert stats["ended"] == "watchdog" and stats["keepalives"] == kinds.count("keepalive")
    assert engine.closed.wait(3.0), "the engine iterator is closed once it yields again"


def test_keepalive_disabled_keeps_the_old_silent_wait() -> None:
    engine = _Engine([(0.3, _audio(0)), (0.0, {"type": "done", "ok": True, "chunks": 1})])
    lines, _ = asyncio.run(_collect(engine, timeout_s=5.0, keepalive_s=0))
    assert [line["type"] for line in lines] == ["audio", "done"]


def test_stats_say_disconnected_when_the_client_leaves_mid_stream() -> None:
    plan = [(0.05, _audio(i)) for i in range(20)] + [(0.0, {"type": "done", "ok": True, "chunks": 20})]
    engine = _Engine(plan)
    stats = new_stream_stats()

    async def _leave_early():
        lines, _ = await _collect(engine, timeout_s=5.0, keepalive_s=1.0, stats=stats, max_lines=3)
        return lines

    lines = asyncio.run(_leave_early())
    assert [line["type"] for line in lines] == ["audio", "audio", "audio"]
    # The body generator was closed by the loop shutdown (asyncio.run closes it): finalize ran.
    assert stats["ended"] == "disconnected", stats
    assert stats["audio_lines"] == 3 and stats["audio_bytes"] == 3 * 104
    assert engine.closed.wait(3.0)


def test_default_keepalive_interval_is_ten_seconds() -> None:
    assert voice_stream.KEEPALIVE_S == 10.0


def test_the_audit_entry_carries_the_resolved_voice_and_what_the_stream_sent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Through the real app and security middleware: the audit line of POST .../voice/tts/stream
    shows provider/model/voice, the lines and audio bytes sent, and how the stream ended — filled
    by the body AFTER the route returned (the middleware writes the entry in its `finally`)."""
    from fastapi.testclient import TestClient

    token = "audit-stream-token"
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

    monkeypatch.setattr(gateway_routes, "_configured_voice_output_defaults", lambda kind: {"provider": "fake-tts", "model": "fake-1", "voice": "V9"})

    wav = b"RIFF" + bytes(60)

    class _FakeSpeech:
        def stream_tts(self, *, text, output=None, params=None):
            assert output.get("provider") == "fake-tts" and output.get("voice") == "V9"
            yield {"type": "audio", "sequence": 0, "content_type": "audio/wav", "audio": wav, "size_bytes": len(wav)}
            yield {"type": "audio", "sequence": 1, "content_type": "audio/wav", "audio": wav, "size_bytes": len(wav)}
            yield {"type": "done", "ok": True, "chunks": 2}

    with TestClient(app) as client:
        h = {"Authorization": f"Bearer {token}"}
        client.get("/api/gateway/runs/session_memory_audit", headers=h)  # boot gate
        runtime = get_gateway_service().host.runtime
        monkeypatch.setattr(runtime, "_abstractcore_llm_client", _FakeSpeech(), raising=False)
        resp = client.post("/api/gateway/runs/session_memory_audit/voice/tts/stream", headers=h, json={"text": "Read this aloud, please."})
        assert resp.status_code == 200, resp.text
        kinds = [json.loads(line)["type"] for line in resp.text.splitlines() if line.strip()]
    assert kinds == ["runtime_start", "audio", "audio", "done"], kinds

    entries = [json.loads(line) for line in (data / "audit_log.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    entry = next(e for e in entries if str(e.get("path", "")).endswith("/voice/tts/stream"))
    detail = entry["voice_stream"]
    assert detail["provider"] == "fake-tts" and detail["model"] == "fake-1" and detail["voice"] == "V9"
    assert detail["text_chars"] == len("Read this aloud, please.")
    assert detail["lines"] == 4 and detail["audio_lines"] == 2 and detail["audio_bytes"] == 2 * len(wav)
    assert detail["keepalives"] == 0 and detail["ended"] == "done", detail
