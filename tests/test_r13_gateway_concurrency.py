"""R13.1: no request can block the gateway's event loop (the 2026-10-04 21:23 watchdog incident).

The incident: the operator pressed Read aloud in AbstractCode (opened through
the console's /apps/code/ proxy) while a turn was generating. The proxy
streams request bodies (chunked, no Content-Length); the security middleware
buffered the body and replayed it with a `receive` that, after the body,
answered "empty body" forever without suspending. The Read-aloud endpoint is
a StreamingResponse, whose disconnect listener loops on `receive()` until
`http.disconnect`: a busy loop on the event loop. Health probes failed, the
watchdog exited with code 75 after 30 s.

These tests run a REAL uvicorn server (as a client sees it) with FAKE engines
doing REAL CPU-bound work: a text-to-speech engine that burns CPU per
sentence, and a "generation" thread that burns CPU the whole time.
"""

from __future__ import annotations

import base64
import http.client
import json
import socket
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

pytestmark = pytest.mark.basic

TOKEN = "r13-test-token"
RUN = "session_memory_r13"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _burn(seconds: float) -> None:
    end = time.process_time() + seconds
    while time.process_time() < end:
        sum(i * i for i in range(2000))


class _CpuEngine:
    """Streams one wav segment per sentence, each costing `cpu_s` of real CPU on the engine thread."""

    def __init__(self, *, cpu_s: float = 0.25, setup_s: float = 0.0, setup_gate: Optional[threading.Event] = None):
        self.cpu_s = cpu_s
        self.setup_s = setup_s
        self.setup_gate = setup_gate
        self.started = 0
        self.closed = 0
        self.finished = 0
        self.lock = threading.Lock()

    def stream_voice(self, parent_run_id, *, text, output, params, child_vars=None):
        if self.setup_gate is not None:
            assert self.setup_gate.wait(20), "setup gate never opened"
        if self.setup_s:
            _burn(self.setup_s)
        engine = self
        sentences = [s for s in text.split(".") if s.strip()]
        with self.lock:
            self.started += 1

        def events():
            try:
                yield {"type": "runtime_start", "ok": True, "run_id": parent_run_id}
                for i, _ in enumerate(sentences):
                    _burn(engine.cpu_s)
                    yield {"type": "audio", "sequence": i, "audio_b64": base64.b64encode(b"RIFF" + bytes([i])).decode()}
                yield {"type": "done", "ok": True, "chunks": len(sentences)}
                with engine.lock:
                    engine.finished += 1
            except GeneratorExit:
                with engine.lock:
                    engine.closed += 1
                raise

        return events()


class _Recorder:
    def __init__(self) -> None:
        self.fired: List[float] = []

    def __call__(self, watchdog: Any, age_s: float) -> None:
        self.fired.append(age_s)


def _serve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: _CpuEngine, *, max_concurrency: int = 4,
           watchdog_s: float = 3.0):
    import uvicorn

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    flows = tmp_path / "flows"
    flows.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_VOICE_TTS_TIMEOUT_S", "30")
    monkeypatch.setenv("ABSTRACTGATEWAY_VOICE_MAX_CONCURRENCY", str(max_concurrency))

    from abstractgateway import loop_watchdog
    from abstractgateway.app import app
    import abstractgateway.routes.gateway as gateway_routes

    recorder = _Recorder()

    def _start() -> Any:
        wd = loop_watchdog.LoopWatchdog(watchdog_s, on_stall=recorder, backstop=False)
        wd.start()
        loop_watchdog._active = wd
        return wd

    monkeypatch.setattr(loop_watchdog, "start_configured", _start)
    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_run_facade", lambda: (engine, None))
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
    return server, thread, port, recorder


def _read_aloud(port: int, text: str, *, chunked: bool, timeout: float = 30.0, stop_after: Optional[int] = None) -> Dict[str, Any]:
    """The request AbstractCode web sends (kit streamTtsJsonl): POST JSON, read JSON Lines.

    chunked=True is how it reaches the gateway behind the console's /apps/code/ proxy."""
    body = json.dumps({"text": text, "request_id": f"r13-{time.monotonic_ns()}", "format": "wav", "provider": "supertonic",
                       "model": "supertonic-3", "voice": "M3"}).encode()
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"}
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    t0 = time.monotonic()
    out: Dict[str, Any] = {"events": [], "status": None, "ttfa_ms": None}
    try:
        path = f"/api/gateway/runs/{RUN}/voice/tts/stream"
        if chunked:
            conn.request("POST", path, body=iter([body]), headers=headers, encode_chunked=True)
        else:
            conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        out["status"] = resp.status
        if resp.status != 200:
            out["detail"] = json.loads(resp.read() or b"{}").get("detail")
            return out
        for raw in resp:
            evt = json.loads(raw)
            out["events"].append(evt)
            if evt.get("type") == "audio" and out["ttfa_ms"] is None:
                out["ttfa_ms"] = (time.monotonic() - t0) * 1000
            if stop_after is not None and len(out["events"]) >= stop_after:
                break
    except Exception as exc:  # noqa: BLE001 - recorded for the assertion message
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()
    return out


class _Hammer:
    """/api/health every 100 ms (timeout 2 s), latencies in ms (None = failed)."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.samples: List[Optional[float]] = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/health", timeout=2) as r:
                    r.read()
                self.samples.append((time.monotonic() - t0) * 1000)
            except Exception:
                self.samples.append(None)
            time.sleep(0.1)

    def __enter__(self) -> "_Hammer":
        self._t.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        self._t.join(5)

    def p99(self) -> float:
        ok = sorted(x for x in self.samples if x is not None)
        return ok[min(len(ok) - 1, int(0.99 * len(ok)))] if ok else float("inf")

    def failures(self) -> int:
        return sum(1 for x in self.samples if x is None)


TEXT = "First sentence here. Second sentence here. Third sentence here. Fourth sentence here."


def _stop(server: Any, thread: threading.Thread) -> None:
    server.should_exit = True
    thread.join(10)


def test_health_stays_fast_under_generation_plus_concurrent_read_aloud_and_the_watchdog_never_fires(tmp_path, monkeypatch):
    engine = _CpuEngine(cpu_s=0.25)
    server, thread, port, recorder = _serve(tmp_path, monkeypatch, engine)
    generating = threading.Event()

    def _fake_generation() -> None:  # the model streaming a reply on the runner thread
        while not generating.is_set():
            _burn(0.05)

    gen = threading.Thread(target=_fake_generation, daemon=True)
    gen.start()
    try:
        results: List[Dict[str, Any]] = []
        with _Hammer(port) as hammer:
            time.sleep(0.5)
            clients = [
                threading.Thread(target=lambda c=c: results.append(_read_aloud(port, TEXT, chunked=c)), daemon=True)
                for c in (True, True, False)  # two behind the app proxy (chunked), one direct
            ]
            for c in clients:
                c.start()
            for c in clients:
                c.join(40)
            time.sleep(0.5)
        assert len(results) == 3, results
        for r in results:
            assert r["status"] == 200 and r["events"] and r["events"][-1]["type"] == "done", r
            assert [e["sequence"] for e in r["events"] if e["type"] == "audio"] == [0, 1, 2, 3], r
        assert hammer.failures() == 0, hammer.samples
        assert hammer.p99() < 500, f"p99 health latency {hammer.p99():.0f} ms: {hammer.samples}"
        assert recorder.fired == [], f"watchdog fired: {recorder.fired}"
    finally:
        generating.set()
        _stop(server, thread)


def test_a_chunked_read_aloud_whose_client_leaves_frees_the_engine_and_the_loop(tmp_path, monkeypatch):
    engine = _CpuEngine(cpu_s=0.3)
    server, thread, port, recorder = _serve(tmp_path, monkeypatch, engine)
    try:
        with _Hammer(port) as hammer:
            r = _read_aloud(port, TEXT * 3, chunked=True, stop_after=2)  # runtime_start + first audio, then hang up
            assert r["status"] == 200 and [e["type"] for e in r["events"]] == ["runtime_start", "audio"], r
            deadline = time.monotonic() + 10
            while engine.closed == 0 and time.monotonic() < deadline:
                time.sleep(0.05)
        assert engine.closed == 1, "the engine's stream was never closed after the client left"
        assert engine.finished == 0
        assert hammer.failures() == 0 and hammer.p99() < 500, hammer.samples
        assert recorder.fired == []
    finally:
        _stop(server, thread)


def test_read_aloud_past_the_admission_bound_is_refused_with_a_sentence_and_the_permit_returns(tmp_path, monkeypatch):
    gate = threading.Event()
    engine = _CpuEngine(cpu_s=0.05, setup_gate=gate)
    server, thread, port, recorder = _serve(tmp_path, monkeypatch, engine, max_concurrency=1)
    try:
        first: Dict[str, Any] = {}
        t = threading.Thread(target=lambda: first.update(_read_aloud(port, TEXT, chunked=True)), daemon=True)
        t.start()
        time.sleep(1.5)  # the first stream holds the only permit (its engine setup waits on the gate)
        second = _read_aloud(port, TEXT, chunked=False)
        assert second["status"] == 503, second
        assert "Read aloud is busy" in str(second.get("detail")), second
        gate.set()
        t.join(20)
        # The first stream told its client why speech had not started yet.
        assert first["events"][0]["type"] == "queued" and "voice engine" in first["events"][0]["message"], first
        assert first["events"][-1]["type"] == "done", first
        third = _read_aloud(port, TEXT, chunked=True)
        assert third["status"] == 200 and third["events"][-1]["type"] == "done", third
        assert recorder.fired == []
    finally:
        gate.set()
        _stop(server, thread)


def test_first_audio_still_arrives_after_one_sentence_ttfa(tmp_path, monkeypatch):
    """Round-6 TTFA is kept: the first segment reaches the client after ONE sentence of synthesis."""
    engine = _CpuEngine(cpu_s=0.3)
    server, thread, port, recorder = _serve(tmp_path, monkeypatch, engine)
    try:
        r = _read_aloud(port, TEXT, chunked=True)
        assert r["status"] == 200 and r["events"][-1]["type"] == "done", r
        assert r["ttfa_ms"] is not None and r["ttfa_ms"] < 300 + 700, r["ttfa_ms"]
        assert not any(e["type"] == "queued" for e in r["events"]), "a free engine must not announce a queue"
    finally:
        _stop(server, thread)
