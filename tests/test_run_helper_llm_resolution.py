"""Gateway-side generations (run Ask/chat, run summary) resolve their provider
exactly like a run does (operator field report 2026-09-28: Ask on an
automation failed at once with "(error: failed to generate answer)" while its
occurrences ran fine; the gateway said "Unknown provider: endpoint:<id>").

The provider here is a gateway endpoint profile (`endpoint:<id>`) pointing at a
local OpenAI-compatible server started by the test: no network, no key."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import ECHO_FLOW_ID, HEADERS, gateway_env, wait_until, write_echo_bundle


class _FakeOpenAI:
    """A minimal OpenAI-compatible chat server that records what it was asked."""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:
                pass

            def _json(self, obj: Dict[str, Any]) -> None:
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # model listing
                self._json({"object": "list", "data": [{"id": "fake-model", "object": "model"}]})

            def do_POST(self) -> None:
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                req = json.loads(raw or b"{}")
                owner.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": req})
                self._json({
                    "id": "c1", "object": "chat.completion", "created": 0, "model": req.get("model"),
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ANSWER FROM THE PROFILE"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                })

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture()
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway_env(monkeypatch, tmp_path, runner=True)
    ref = write_echo_bundle(tmp_path / "bundles")
    fake = _FakeOpenAI()
    from abstractgateway.app import app

    with TestClient(app) as c:
        c.bundle_ref = ref  # type: ignore[attr-defined]
        c.fake = fake  # type: ignore[attr-defined]
        r = c.post("/api/gateway/config/provider-endpoint-profiles", headers=HEADERS, json={
            "id": "vpsllm", "display_name": "VPS LLM", "base_url": fake.url, "api_key": "profile-key",
            "scope": "gateway", "capabilities": ["text"], "allowed_models": ["fake-model"],
        })
        assert r.status_code == 200, r.text
        yield c
    fake.close()


def _completed_run(c: TestClient) -> str:
    r = c.post("/api/gateway/runs/start", headers=HEADERS, json={
        "bundle_id": c.bundle_ref, "flow_id": ECHO_FLOW_ID, "input_data": {"prompt": "the news of the day"}})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    wait_until(lambda: c.get(f"/api/gateway/runs/{run_id}", headers=HEADERS).json().get("status") == "completed")
    return run_id


def test_ask_on_a_run_uses_the_gateway_endpoint_profile(live: TestClient) -> None:
    run_id = _completed_run(live)
    r = live.post(f"/api/gateway/runs/{run_id}/chat", headers=HEADERS, json={
        "provider": "endpoint:vpsllm", "model": "fake-model",
        "messages": [{"role": "user", "content": "detail the news"}]})
    assert r.status_code == 200, r.text
    assert r.json()["answer"] == "ANSWER FROM THE PROFILE"
    sent = live.fake.requests[-1]  # type: ignore[attr-defined]
    assert sent["path"].endswith("/chat/completions")
    assert sent["auth"] == "Bearer profile-key"          # the profile's key, from the gateway store
    assert sent["body"]["model"] == "fake-model"
    grounding = json.dumps(sent["body"]["messages"])
    assert "RUN_CONTEXT" in grounding and run_id in grounding and "detail the news" in grounding


def test_run_summary_uses_the_gateway_endpoint_profile(live: TestClient) -> None:
    run_id = _completed_run(live)
    r = live.post(f"/api/gateway/runs/{run_id}/summary", headers=HEADERS, json={"provider": "endpoint:vpsllm", "model": "fake-model"})
    assert r.status_code == 200, r.text
    assert r.json()["summary"] == "ANSWER FROM THE PROFILE"


def test_an_unknown_endpoint_profile_is_a_named_400(live: TestClient) -> None:
    run_id = _completed_run(live)
    r = live.post(f"/api/gateway/runs/{run_id}/chat", headers=HEADERS, json={
        "provider": "endpoint:nope", "model": "fake-model", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 400
    assert "endpoint:nope" in r.text
