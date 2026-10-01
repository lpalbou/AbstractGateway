"""The text route's `base_url` reaches the run's LLM call (backlog 0994 item 64).

Released 0.10.0: `PUT /config/capability-defaults/input/text {provider, model,
base_url}` stored and served the base URL, but `core_config.text_default()`
dropped it, so the bundle host built the default client against the
provider's built-in address (an LM Studio run called localhost:1234).

Hermetic: the "LM Studio" here is an OpenAI-compatible server started by the
test on an ephemeral 127.0.0.1 port; the conftest network guard refuses
:1234/:11434 (and anything non-loopback), so a fallback to the built-in
address fails the run instead of reaching a real provider.
"""

from __future__ import annotations

import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import HEADERS, gateway_env, wait_until

FLOW_ID = "root"
BUNDLE_ID = "bundle-baseurl"


class _FakeLMStudio:
    """A minimal OpenAI-compatible server that records every chat request."""

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
                owner.requests.append({"path": self.path, "body": req})
                self._json({
                    "id": "c1", "object": "chat.completion", "created": 0, "model": req.get("model"),
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ANSWER FROM THE ROUTE"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                })

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def chat_requests(self) -> List[Dict[str, Any]]:
        return [r for r in self.requests if r["path"].endswith("/chat/completions")]

    def close(self) -> None:
        self.server.shutdown()


def _scoped_core_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import abstractgateway.core_config as core_config

    path = tmp_path / "config" / "abstractcore.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"capability_defaults": {"routes": {}}}), encoding="utf-8")
    monkeypatch.delenv("ABSTRACTCORE_SERVER_BASE_URL", raising=False)
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(path))
    monkeypatch.setattr(core_config, "core_server_base_url", lambda: "")
    return path


def _write_auto_llm_bundle(bundles_dir: Path) -> None:
    """One llm_call node with NO provider/model: it runs on the text default."""
    flow = {
        "id": FLOW_ID, "name": "baseurl", "description": "", "interfaces": [],
        "nodes": [
            {"id": "n1", "type": "on_flow_start", "position": {"x": 0.0, "y": 0.0},
             "data": {"nodeType": "on_flow_start", "label": "Start", "inputs": [],
                      "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]}},
            {"id": "n2", "type": "llm_call", "position": {"x": 200.0, "y": 0.0},
             "data": {"nodeType": "llm_call", "label": "LLM Call",
                      "inputs": [{"id": "exec-in", "label": "", "type": "execution"},
                                 {"id": "prompt", "label": "prompt", "type": "string"}],
                      "outputs": [{"id": "exec-out", "label": "", "type": "execution"},
                                  {"id": "response", "label": "response", "type": "string"}],
                      "pinDefaults": {"prompt": "hello from the base_url test"},
                      "effectConfig": {}}},
            {"id": "n3", "type": "on_flow_end", "position": {"x": 400.0, "y": 0.0},
             "data": {"nodeType": "on_flow_end", "label": "End",
                      "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "sourceHandle": "exec-out", "target": "n2", "targetHandle": "exec-in"},
            {"id": "e2", "source": "n2", "sourceHandle": "exec-out", "target": "n3", "targetHandle": "exec-in"},
        ],
    }
    manifest = {
        "bundle_format_version": "1", "bundle_id": BUNDLE_ID, "bundle_version": "0.0.0",
        "created_at": "2026-10-01T00:00:00+00:00",
        "entrypoints": [{"flow_id": FLOW_ID, "name": "baseurl", "description": "", "interfaces": []}],
        "flows": {FLOW_ID: f"flows/{FLOW_ID}.json"}, "artifacts": {}, "assets": {}, "metadata": {},
    }
    bundles_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(bundles_dir / f"{BUNDLE_ID}.flow", "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr(f"flows/{FLOW_ID}.json", json.dumps(flow))


def _run_to_end(c: TestClient) -> Dict[str, Any]:
    r = c.post("/api/gateway/runs/start", headers=HEADERS,
               json={"bundle_id": BUNDLE_ID, "flow_id": FLOW_ID, "input_data": {}})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    wait_until(lambda: c.get(f"/api/gateway/runs/{run_id}", headers=HEADERS).json().get("status")
               in ("completed", "failed"))
    return c.get(f"/api/gateway/runs/{run_id}", headers=HEADERS).json()


# --- (1) the seam ----------------------------------------------------------


def test_text_default_returns_the_route_base_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _scoped_core_store(tmp_path, monkeypatch)
    from abstractgateway.core_config import save_gateway_capability_default, text_default
    from abstractgateway.provider_defaults import default_text_route_connection_kwargs

    save_gateway_capability_default("input", "text", provider="lmstudio", model="m",
                                    base_url="http://127.0.0.1:9/v1", base_dir=tmp_path)
    row = text_default(base_dir=tmp_path)
    assert row["provider"] == "lmstudio" and row["model"] == "m"
    assert row["base_url"] == "http://127.0.0.1:9/v1"
    # Only for the route's own provider; never for another one.
    assert default_text_route_connection_kwargs("lmstudio", base_dir=tmp_path) == {"base_url": "http://127.0.0.1:9/v1"}
    assert default_text_route_connection_kwargs("ollama", base_dir=tmp_path) == {}


def test_unset_base_url_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _scoped_core_store(tmp_path, monkeypatch)
    from abstractgateway.core_config import save_gateway_capability_default, text_default
    from abstractgateway.provider_defaults import default_text_route_connection_kwargs

    save_gateway_capability_default("output", "text", provider="lmstudio", model="m", base_dir=tmp_path)
    assert text_default(base_dir=tmp_path)["base_url"] is None
    assert default_text_route_connection_kwargs("lmstudio", base_dir=tmp_path) == {}


# --- (2) the run path, end to end -----------------------------------------


@pytest.fixture()
def fake() -> Any:
    server = _FakeLMStudio()
    yield server
    server.close()


def test_run_reaches_the_route_base_url_after_a_console_put(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: _FakeLMStudio
) -> None:
    """The verifier's repro: PUT input/text with a base_url on a RUNNING gateway
    (live refresh path), then a run on the default must call that endpoint."""
    _scoped_core_store(tmp_path, monkeypatch)
    gateway_env(monkeypatch, tmp_path, runner=True)
    _write_auto_llm_bundle(tmp_path / "bundles")
    from abstractgateway.app import app

    with TestClient(app) as c:
        r = c.put("/api/gateway/config/capability-defaults/input/text", headers=HEADERS,
                  json={"provider": "lmstudio", "model": "fake-model", "base_url": fake.url})
        assert r.status_code == 200, r.text
        run = _run_to_end(c)
    assert fake.chat_requests(), f"the route's base_url was never called; run={json.dumps(run)[:2000]}"
    assert fake.chat_requests()[-1]["body"]["model"] == "fake-model"
    assert run.get("status") == "completed", json.dumps(run)[:2000]


def test_run_reaches_the_route_base_url_configured_before_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: _FakeLMStudio
) -> None:
    """Same contract on the LOAD path: the route was saved before the gateway started."""
    _scoped_core_store(tmp_path, monkeypatch)
    gateway_env(monkeypatch, tmp_path, runner=True)
    _write_auto_llm_bundle(tmp_path / "bundles")
    from abstractgateway.core_config import save_gateway_capability_default

    save_gateway_capability_default("output", "text", provider="lmstudio", model="fake-model",
                                    base_url=fake.url, base_dir=tmp_path / "runtime")
    from abstractgateway.app import app

    with TestClient(app) as c:
        run = _run_to_end(c)
    assert fake.chat_requests(), f"the route's base_url was never called; run={json.dumps(run)[:2000]}"
    assert run.get("status") == "completed", json.dumps(run)[:2000]


def test_gateway_side_generation_uses_the_route_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: _FakeLMStudio
) -> None:
    """Run summary / Ask / sandbox build their client in `_gateway_llm_client`:
    the route's provider gets the route's base_url there too."""
    _scoped_core_store(tmp_path, monkeypatch)
    gateway_env(monkeypatch, tmp_path, runner=True)
    _write_auto_llm_bundle(tmp_path / "bundles")
    from abstractgateway.app import app

    with TestClient(app) as c:
        r = c.put("/api/gateway/config/capability-defaults/output/text", headers=HEADERS,
                  json={"provider": "lmstudio", "model": "fake-model", "base_url": fake.url})
        assert r.status_code == 200, r.text
        run = _run_to_end(c)
        before = len(fake.chat_requests())
        r = c.post(f"/api/gateway/runs/{run['run_id']}/summary", headers=HEADERS,
                   json={"provider": "lmstudio", "model": "fake-model"})
        assert r.status_code == 200, r.text
        assert r.json()["summary"] == "ANSWER FROM THE ROUTE"
    assert len(fake.chat_requests()) == before + 1
