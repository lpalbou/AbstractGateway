"""Split mode: `/v1` in the API process forwards to the runner's model host; the model loads ONCE.

The fake model host stands in for AbstractCore's serving app (the facade both processes call):
it "loads the model" on its first request and counts loads. The API process's own path to Core
is replaced by a tripwire, so any request served locally in the API process fails the test.
Both "processes" live in this test process (the role is what decides), the model host on a real
loopback port behind a real uvicorn server, the API side driven through `CoreEndpoint`.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest

from abstractgateway import core_endpoint as ce
from abstractgateway import split_mode
from abstractgateway.live_deltas import ROLE_API, ROLE_COMBINED, process_role, set_process_role

TOKEN = "endpoint-token-0123456789"


class FakeCore:
    """The model host's view of Core: one model load, however many requests."""

    def __init__(self) -> None:
        self.loads = 0
        self.requests: List[Dict[str, Any]] = []

    async def __call__(self, scope, receive, send, *, token: str, allow_unauthenticated: bool = False) -> None:
        if self.loads == 0:
            self.loads += 1  # the 15 GB load, once per process
        body = b""
        while True:
            m = await receive()
            body += m.get("body") or b""
            if not m.get("more_body"):
                break
        headers = {bytes(k).decode().lower(): bytes(v).decode() for k, v in scope.get("headers") or []}
        self.requests.append({"path": scope["path"], "auth": headers.get("authorization"), "token": token,
                              "allow": allow_unauthenticated, "headers": headers, "body": body})
        if scope["path"] == "/v1/models":
            out = json.dumps({"object": "list", "data": [{"id": "fake-27b", "object": "model", "created": 1,
                                                          "owned_by": "test"}]}).encode()
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": out})
            return
        doc = json.loads(body or b"{}")
        if doc.get("stream"):
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/event-stream")]})
            for i, piece in enumerate(["Hel", "lo"]):
                chunk = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "fake-27b",
                         "choices": [{"index": 0, "delta": ({"role": "assistant", "content": piece} if i == 0 else {"content": piece}),
                                      "finish_reason": None}]}
                await send({"type": "http.response.body", "body": f"data: {json.dumps(chunk)}\n\n".encode(), "more_body": True})
            last = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": "fake-27b",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            await send({"type": "http.response.body", "body": f"data: {json.dumps(last)}\n\ndata: [DONE]\n\n".encode()})
            return
        out = json.dumps({"id": "c1", "object": "chat.completion", "created": 1, "model": "fake-27b",
                          "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello"},
                                       "finish_reason": "stop"}],
                          "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": out})


@pytest.fixture()
def split_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "runtime"
    (data_dir / "config").mkdir(parents=True)
    (data_dir / "config" / "core_endpoint.json").write_text(json.dumps(
        {"enabled": True, "access": "token", "reach": "machine", "token": TOKEN}), encoding="utf-8")
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data_dir))
    fake = FakeCore()
    # The runner side's Core (the model host imports the facade at call time).
    import abstractruntime.integrations.abstractcore.server_facade as facade

    monkeypatch.setattr(facade, "serve_core_request", fake)

    async def tripwire(*_a, **_k):
        raise AssertionError("the API process served /v1 itself: a second model load")

    monkeypatch.setattr(ce, "serve_core_request", tripwire)
    prev = process_role()
    set_process_role(ROLE_API)
    host = split_mode.ModelHost(data_dir).start()
    try:
        yield data_dir, fake, host
    finally:
        host.stop()
        set_process_role(prev)


async def _call(method: str, path: str, *, body: Any = None, auth: str = TOKEN) -> tuple[int, Dict[str, str], bytes]:
    raw = json.dumps(body).encode() if body is not None else b""
    headers = [(b"authorization", f"Bearer {auth}".encode()), (b"host", b"127.0.0.1:8080")]
    if body is not None:
        headers += [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": path.encode(), "root_path": "", "query_string": b"",
             "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8080), "headers": headers, "state": {}}
    sent = {"done": False}

    async def receive():
        if not sent["done"]:
            sent["done"] = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    status = {"code": 0}
    out_headers: Dict[str, str] = {}
    out = bytearray()

    async def send(message):
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
            out_headers.update({bytes(k).decode().lower(): bytes(v).decode() for k, v in message.get("headers") or []})
        elif message["type"] == "http.response.body":
            out.extend(message.get("body") or b"")

    await ce.CoreEndpoint()(scope, receive, send)
    return status["code"], out_headers, bytes(out)


def test_v1_is_forwarded_to_the_runner_and_the_model_loads_once(split_env) -> None:
    data_dir, fake, host = split_env
    doc = split_mode.read_address(data_dir)
    assert doc is not None and doc["url"] == host.url and doc["url"].startswith("http://127.0.0.1:")
    assert (split_mode.address_path(data_dir).stat().st_mode & 0o777) == 0o600

    status, _h, body = asyncio.run(_call("GET", "/v1/models"))
    assert status == 200 and json.loads(body)["data"][0]["id"] == "fake-27b"

    status, _h, body = asyncio.run(_call("POST", "/v1/chat/completions",
                                         body={"model": "mlx/fake-27b", "messages": [{"role": "user", "content": "hi"}]}))
    assert status == 200, body
    assert json.loads(body)["choices"][0]["message"]["content"] == "Hello"

    status, headers, body = asyncio.run(_call("POST", "/v1/chat/completions", body={
        "model": "mlx/fake-27b", "stream": True, "messages": [{"role": "user", "content": "hi"}]}))
    assert status == 200 and "text/event-stream" in headers.get("content-type", "")
    text = body.decode()
    assert '"Hel"' in text and '"lo"' in text and "[DONE]" in text

    assert fake.loads == 1, "one model, in the runner process"
    assert len(fake.requests) == 3
    # The runner sees the gateway's credential, never the split secret or the caller's own key.
    for req in fake.requests:
        assert req["auth"] == f"Bearer {TOKEN}" and req["token"] == TOKEN and req["allow"] is False
        assert "x-abstractgateway-split-secret" not in req["headers"]


def test_the_model_host_refuses_a_request_without_the_secret(split_env) -> None:
    _data_dir, fake, host = split_env
    r = httpx.get(host.url + "/v1/models", headers={"Authorization": f"Bearer {TOKEN}"}, trust_env=False)
    assert r.status_code == 403 and r.json()["error"]["code"] == "split_secret_required"
    r = httpx.get(host.url + "/v1/models", headers={"x-abstractgateway-split-secret": "guess"}, trust_env=False)
    assert r.status_code == 403
    assert fake.requests == []


def test_no_runner_answers_503_and_never_loads_here(split_env) -> None:
    data_dir, fake, host = split_env
    host.stop()
    assert split_mode.read_address(data_dir) is None, "stop() removes the address it wrote"
    status, _h, body = asyncio.run(_call("POST", "/v1/chat/completions",
                                         body={"model": "mlx/fake-27b", "messages": [{"role": "user", "content": "hi"}]}))
    assert status == 503 and json.loads(body)["error"]["code"] == "runner_unavailable"
    assert fake.loads == 0


def test_a_recorded_address_of_a_dead_process_is_not_used(split_env) -> None:
    data_dir, fake, host = split_env
    doc = json.loads(split_mode.address_path(data_dir).read_text())
    doc["pid"] = 2 ** 22 + 12345  # no such process
    split_mode.address_path(data_dir).write_text(json.dumps(doc))
    status, _h, body = asyncio.run(_call("GET", "/v1/models"))
    assert status == 503 and json.loads(body)["error"]["code"] == "runner_unavailable"
    assert fake.requests == []


def test_combined_serve_keeps_serving_core_in_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[str] = []

    async def local(scope, receive, send, *, token, allow_unauthenticated=False):
        calls.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"object":"list","data":[]}'})

    monkeypatch.setattr(ce, "serve_core_request", local)
    prev = process_role()
    set_process_role(ROLE_COMBINED)
    try:
        status = {}

        async def send(m):
            if m["type"] == "http.response.start":
                status["code"] = m["status"]

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        asyncio.run(ce.serve_core({"type": "http", "path": "/v1/models", "headers": []}, receive, send, token="t"))
        assert calls == ["/v1/models"] and status["code"] == 200
    finally:
        set_process_role(prev)


def test_background_work_stays_out_of_the_api_process() -> None:
    prev = process_role()
    try:
        set_process_role(ROLE_API)
        assert split_mode.background_work_here("mail") is False
        assert split_mode.mode() == "split"
        set_process_role(ROLE_COMBINED)
        assert split_mode.background_work_here("mail") is True
        assert split_mode.mode() == "combined"
    finally:
        set_process_role(prev)


def test_mail_and_push_workers_do_not_start_in_the_api_process(tmp_path: Path) -> None:
    """Placement: the split API process starts no mail worker, entity mail worker or push thread
    (they ran in BOTH processes before: two pollers on one mailbox, pushes sent twice)."""
    from types import SimpleNamespace

    from abstractgateway.events_stream import stream_for
    from abstractgateway.mail.worker import EmailWorker, sync_entity_workers
    from abstractgateway.push.dispatcher import PushDispatcher

    prev = process_role()
    set_process_role(ROLE_API)
    try:
        worker = EmailWorker(SimpleNamespace(runner=None), SimpleNamespace(tenant_id="default", user_id="alice"))
        worker.start()
        assert worker._thread is None
        assert sync_entity_workers() == {"started": [], "stopped": []}
        plane = tmp_path / "plane"
        d = PushDispatcher(plane_dir=plane, root_dir=tmp_path, stream=stream_for(plane, "default", "alice"),
                           relay_url="http://relay.test")
        d.start()
        assert d.running is False
        set_process_role(ROLE_COMBINED)
        d.start()
        assert d.running is True
        d.stop()
    finally:
        set_process_role(prev)
