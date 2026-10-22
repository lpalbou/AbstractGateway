"""Fresh accounts execute an unpinned workflow via the admin's shared default.

Only the upstream model is a local HTTP fixture. Auth, config persistence,
workflow compilation, provider resolution, SDK transport and the runner are real.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time

import pytest

from test_gateway_bundle_llm_tools_agents import _write_bundle
from test_gateway_provider_endpoint_profiles import _app_client


@contextmanager
def _provider():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            data = json.dumps({"object": "list", "data": [{"id": "proof-model", "object": "model", "owned_by": "fixture"}]}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.end_headers(); self.wfile.write(data)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            calls.append({"path": self.path, "authorization": self.headers.get("Authorization"), "body": body})
            if self.headers.get("Authorization") != "Bearer fixture-provider-secret":
                self.send_response(401); self.end_headers(); return
            self.send_response(200)
            if body.get("stream"):
                self.send_header("Content-Type", "text/event-stream"); self.end_headers()
                chunk = {"id": "proof", "object": "chat.completion.chunk", "model": "proof-model", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "shared-default-proof-ok"}, "finish_reason": None}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())
            else:
                self.send_header("Content-Type", "application/json"); self.end_headers()
                self.wfile.write(json.dumps({"id": "proof", "object": "chat.completion", "model": "proof-model", "choices": [{"index": 0, "message": {"role": "assistant", "content": "shared-default-proof-ok"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", calls
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


@pytest.mark.parametrize("stored_scope", ["gateway", "user"])
def test_fresh_user_completes_workflow_with_admin_default_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stored_scope: str):
    for key in ("ABSTRACTCORE_SERVER_BASE_URL", "ABSTRACTGATEWAY_PROVIDER", "ABSTRACTGATEWAY_MODEL", "ABSTRACTGATEWAY_LLM_PROVIDER", "ABSTRACTGATEWAY_LLM_MODEL"):
        monkeypatch.delenv(key, raising=False)
    flow = {
        "id": "root", "entryNode": "start",
        "nodes": [
            {"id": "start", "type": "on_flow_start", "data": {"nodeType": "on_flow_start", "outputs": [{"id": "exec-out", "type": "execution"}]}},
            {"id": "model", "type": "llm_call", "data": {"nodeType": "llm_call", "inputs": [{"id": "exec-in", "type": "execution"}, {"id": "prompt", "type": "string"}], "outputs": [{"id": "exec-out", "type": "execution"}, {"id": "response", "type": "string"}], "pinDefaults": {"prompt": "Confirm that the shared gateway default works."}, "effectConfig": {}}},
            {"id": "end", "type": "on_flow_end", "data": {"nodeType": "on_flow_end", "inputs": [{"id": "exec-in", "type": "execution"}, {"id": "answer", "type": "string"}]}},
        ],
        "edges": [
            {"id": "a", "source": "start", "sourceHandle": "exec-out", "target": "model", "targetHandle": "exec-in"},
            {"id": "b", "source": "model", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"},
            {"id": "c", "source": "model", "sourceHandle": "response", "target": "end", "targetHandle": "answer"},
        ],
    }
    _write_bundle(bundles_dir=tmp_path / "bundles", bundle_id="shared-default-proof", flows={"root": flow}, entrypoint="root")
    app_client = _app_client(tmp_path, monkeypatch, user_auth=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.02")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")
    admin = {"Authorization": "Bearer admin-token"}
    with _provider() as (url, calls), app_client as client:
        profile = client.post("/api/gateway/config/provider-endpoint-profiles", headers=admin, json={"id": "admin-default", "display_name": "Shared default", "provider_family": "openai-compatible", "base_url": url, "api_key": "fixture-provider-secret"})
        assert profile.status_code == 200, profile.text
        if stored_scope == "user":
            from abstractgateway.provider_endpoint_profiles import ProviderEndpointProfileStore
            ProviderEndpointProfileStore(base_dir=tmp_path / "runtime").upsert_profile(profile_id="admin-default", scope="user")
        saved = client.put("/api/gateway/config/capability-defaults/output/text", headers=admin, json={"provider": "endpoint:admin-default", "model": "proof-model"})
        assert saved.status_code == 200, saved.text
        created = client.post("/api/gateway/admin/users", headers=admin, json={"user_id": "brand-new-user", "roles": ["user"]})
        assert created.status_code == 200, created.text
        user = {"Authorization": "Bearer " + created.json()["token"]}
        # No provider, model, URL or upstream key in either the workflow or request.
        started = client.post("/api/gateway/runs/start", headers=user, json={"bundle_id": "shared-default-proof", "flow_id": "root", "input_data": {}})
        assert started.status_code == 200, started.text
        run_id = started.json()["run_id"]
        deadline = time.monotonic() + 15
        while True:
            run = client.get(f"/api/gateway/runs/{run_id}", headers=user)
            assert run.status_code == 200, run.text
            status = run.json().get("status")
            if status in {"completed", "failed", "cancelled"} or time.monotonic() > deadline:
                break
            time.sleep(0.05)
        assert status == "completed", run.text
        assert calls, "workflow must reach the real HTTP provider boundary"
        assert calls[-1]["path"] == "/v1/chat/completions"
        assert calls[-1]["authorization"] == "Bearer fixture-provider-secret"
        assert calls[-1]["body"]["model"] == "proof-model"
        ledger = client.get(f"/api/gateway/runs/{run_id}/ledger?after=0&limit=100", headers=user)
        assert ledger.status_code == 200, ledger.text
        assert "shared-default-proof-ok" in ledger.text
        assert "fixture-provider-secret" not in ledger.text and "fixture-provider-secret" not in run.text
        user_runtime = tmp_path / "runtime/users/default/brand-new-user/runtime"
        assert not (user_runtime / "config/provider_endpoint_profiles.json").exists()
        assert not (user_runtime / "config/abstractcore.json").exists()
