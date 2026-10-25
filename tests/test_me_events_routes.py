"""GET /api/gateway/me/events (catch-up) and /me/events/stream (SSE), backlog 1046.

Through the real app + auth middleware with two real principals (hermetic data dir, tmp HOME from
conftest): each sees only their own stream; the SSE tail resumes from Last-Event-ID without a miss
or a duplicate; the stream wakes on another process's append (stat poll)."""

from __future__ import annotations

import asyncio
import http.client
import json
import socket
import threading
import time
from pathlib import Path
from typing import Dict, List

import pytest
from fastapi.testclient import TestClient

from test_gateway_principal_isolation_matrix import _client, _create_user, _write_min_bundle

from abstractgateway.events_stream import UserEventStream, stream_for
from abstractgateway.routes.me_events import resolve_start, sse_frames


def _plane(tmp_path: Path, user: str) -> Path:
    return tmp_path / "runtime" / "users" / "default" / user / "runtime"


def _wait(pred, timeout: float = 15.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def _start(client: TestClient, headers: dict) -> str:
    r = client.post("/api/gateway/runs/start", headers=headers, json={"bundle_id": "iso-bundle", "flow_id": "root", "input_data": {"prompt": "hi"}})
    assert r.status_code == 200, r.text
    return r.json()["run_id"]


def _events(client: TestClient, headers: dict, **params) -> dict:
    r = client.get("/api/gateway/me/events", headers=headers, params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_each_principal_reads_only_their_own_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch)
    with client:
        alice, bob = _create_user(client, "alice"), _create_user(client, "bob")
        for user in ("alice", "bob"):
            _write_min_bundle(bundles_dir=tmp_path / "runtime" / "users" / "default" / user / "flows")
        a_run, b_run = _start(client, alice), _start(client, bob)

        def _finished(headers: dict, run_id: str) -> bool:
            ids = [e["id"] for e in _events(client, headers)["events"]]
            return f"run.finished:{run_id}" in ids

        _wait(lambda: _finished(alice, a_run) and _finished(bob, b_run))
        a_page, b_page = _events(client, alice), _events(client, bob)
        assert a_page["stream"] == "user:default:alice" and b_page["stream"] == "user:default:bob"
        assert [e["type"] for e in a_page["events"]] == ["run.started", "run.finished"]
        assert a_page["events"][0]["data"]["origin"]["actor"] == "alice"
        assert b_run not in json.dumps(a_page) and a_run not in json.dumps(b_page)

        # Cursor semantics: after = last consumed sequence; paging with limit.
        one = _events(client, alice, after=0, limit=1)
        assert [e["sequence"] for e in one["events"]] == [1] and one["next_after"] == 1 and one["more"] is True
        rest = _events(client, alice, after=one["next_after"])
        assert [e["sequence"] for e in rest["events"]] == [2] and rest["more"] is False and rest["head"] == 2

        # The tray channel's report lands in the caller's own stream only.
        r = client.post("/api/gateway/me/events/notifications", headers=bob, json={"channel": "tray", "outcome": "sent", "record_key": "automation:completed:a:1"})
        assert r.status_code == 200 and r.json()["created"] is True
        assert _events(client, bob)["events"][-1]["type"] == "notification.sent"
        assert all(e["type"] != "notification.sent" for e in _events(client, alice)["events"])
        assert client.post("/api/gateway/me/events/notifications", headers=bob, json={"channel": "push", "outcome": "sent", "record_key": "k"}).status_code == 422

        assert client.get("/api/gateway/me/events").status_code == 401  # no principal, no stream


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _read_sse(port: int, headers: dict, *, want: int, path: str = "/api/gateway/me/events/stream?heartbeat_s=0.3", timeout: float = 15.0) -> List[dict]:
    """Read `want` events (id, event, data) then close the connection."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    assert resp.status == 200, resp.read()
    assert resp.getheader("content-type", "").startswith("text/event-stream")
    out: List[dict] = []
    cur: dict = {}
    heartbeats = 0
    try:
        while len(out) < want:
            line = resp.fp.readline().decode("utf-8").rstrip("\n")
            if line.startswith(": keep-alive"):
                heartbeats += 1
            elif line.startswith("id: "):
                cur["id"] = int(line[4:])
            elif line.startswith("event: "):
                cur["event"] = line[7:]
            elif line.startswith("data: "):
                cur["data"] = json.loads(line[6:])
            elif line == "" and cur:
                out.append(cur)
                cur = {}
    finally:
        conn.close()
    return out


def test_sse_tail_resumes_from_last_event_id_without_miss_or_duplicate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    client = _client(tmp_path, monkeypatch)
    with client:
        alice, bob = _create_user(client, "alice"), _create_user(client, "bob")
        _events(client, alice)  # build alice's service (her plane)
        _events(client, bob)
        stream = stream_for(_plane(tmp_path, "alice"), "default", "alice")
        for i in range(3):
            stream.append("attention.seen", event_id=f"seen:{i}", data={"automation_id": "a", "seq": i + 1})
        stream_for(_plane(tmp_path, "bob"), "default", "bob").append("attention.seen", event_id="bob-only", data={"automation_id": "b", "seq": 1})

        from abstractgateway.app import app

        port = _free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            _wait(lambda: server.started, 10)
            first = _read_sse(port, alice, want=3)
            assert [e["id"] for e in first] == [1, 2, 3]
            assert [e["data"]["id"] for e in first] == ["seen:0", "seen:1", "seen:2"]
            assert all(e["event"] == "attention.seen" and e["data"]["sequence"] == e["id"] for e in first)

            # Disconnected while two more happen; one more while connected (live wake).
            stream.append("attention.seen", event_id="seen:3", data={"automation_id": "a", "seq": 4})
            stream.append("attention.seen", event_id="seen:4", data={"automation_id": "a", "seq": 5})
            timer = threading.Timer(0.5, lambda: stream.append("run.started", event_id="run.started:r9", data={"run_id": "r9"}))
            timer.start()
            resumed = _read_sse(port, {**alice, "Last-Event-ID": str(first[-1]["id"])}, want=3)
            assert [e["id"] for e in resumed] == [4, 5, 6]
            assert resumed[-1]["event"] == "run.started"

            # `after` wins over Last-Event-ID; bob's stream never carries alice's events.
            again = _read_sse(port, {**alice, "Last-Event-ID": "1"}, want=1, path="/api/gateway/me/events/stream?after=5")
            assert [e["id"] for e in again] == [6]
            bobs = _read_sse(port, bob, want=1)
            assert [e["data"]["id"] for e in bobs] == ["bob-only"]
        finally:
            server.should_exit = True
            thread.join(10)


def test_resolve_start_rules() -> None:
    assert resolve_start(0, None) == 0
    assert resolve_start(0, "7") == 7
    assert resolve_start(3, "7") == 3
    assert resolve_start(0, "garbage") == 0


def test_sse_frames_heartbeat_and_cross_process_wake(tmp_path: Path) -> None:
    writer = UserEventStream(tmp_path, "default", "alice")  # "another process": no shared listener
    reader = stream_for(tmp_path, "default", "alice")
    assert reader is not writer

    async def _run() -> List[bytes]:
        frames: List[bytes] = []
        stop = {"v": False}

        async def disconnected() -> bool:
            return stop["v"]

        gen = sse_frames(reader, after=0, heartbeat_s=0.2, disconnected=disconnected, poll_s=0.1)
        loop = asyncio.get_running_loop()
        loop.call_later(0.5, lambda: writer.append("run.started", event_id="run.started:x", data={"run_id": "x"}))
        deadline = loop.time() + 5
        async for chunk in gen:
            frames.append(chunk)
            if b"run.started:x" in chunk or loop.time() > deadline:
                stop["v"] = True
        return frames

    frames = asyncio.run(_run())
    joined = b"".join(frames)
    assert b": keep-alive" in joined
    assert b"id: 1\nevent: run.started\n" in joined


def test_routes_are_in_the_openapi_document() -> None:
    from abstractgateway.app import app

    paths = app.openapi()["paths"]
    for path in ("/api/gateway/me/events", "/api/gateway/me/events/stream", "/api/gateway/me/events/notifications"):
        assert path in paths, path
    assert "Last-Event-ID" in paths["/api/gateway/me/events/stream"]["get"]["description"]
