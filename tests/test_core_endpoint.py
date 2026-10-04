"""Managed serving boundary: real Core routes, host controls and isolated tokens."""
import importlib
import json
import stat
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from abstractgateway import core_endpoint as ce
from abstractruntime.integrations.abstractcore import server_facade
from abstractgateway.security.gateway_security import GatewayAuthPolicy, GatewaySecurityMiddleware
from abstractgateway.security.principal import GatewayPrincipal

pytestmark = pytest.mark.basic


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ce, "gateway_data_dir_from_env", lambda: tmp_path)
    monkeypatch.setattr(ce, "resolve_network_setting", lambda path: {"mode": "lan"})
    monkeypatch.setattr(ce.ne, "live_reverse_proxy", lambda path: SimpleNamespace(trust_proxy=False))
    monkeypatch.setattr(ce.ne, "effective_bind", lambda path, **kw: {})
    monkeypatch.setattr(ce.ne, "tailscale_status", lambda: None)
    for key in ["OPENAI_API_KEY", "ABSTRACTCORE_AUTH_TOKEN", "ABSTRACTCORE_SERVER_ALLOW_UNAUTHENTICATED"]:
        monkeypatch.delenv(key, raising=False)
    app = FastAPI()
    app.mount("/v1", ce.CoreEndpoint())
    return tmp_path, app


def client(app, peer="192.168.1.20"):
    return TestClient(app, client=(peer, 50000))


def body():
    return {"model": "openai/gpt-4", "messages": [{"role": "user", "content": "hi"}]}


def test_defaults_disable_and_secret_persistence_rotation_restart(host):
    data_dir, app = host
    assert client(app).post("/v1/chat/completions", json=body()).status_code == 404
    enabled = ce.change_settings(data_dir, enabled=True, reach="network")
    path = data_dir / "config/core_endpoint.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert ce.read_settings(data_dir).token == enabled.token
    assert enabled.token not in repr(enabled)
    rotated = ce.change_settings(data_dir, rotate=True)
    assert rotated.token != enabled.token
    assert ce.read_settings(data_dir).token == rotated.token
    assert client(app).post("/v1/chat/completions", headers={"Authorization": f"Bearer {enabled.token}"}, json=body()).status_code == 401
    ce.change_settings(data_dir, enabled=False)
    assert client(app).post("/v1/chat/completions", headers={"Authorization": f"Bearer {rotated.token}"}, json=body()).status_code == 404


def test_allowlist_and_token_rejection_happen_before_core_import(host, monkeypatch):
    data_dir, app = host
    ce.change_settings(data_dir, enabled=True, reach="network")
    monkeypatch.setattr(server_facade.importlib, "import_module", lambda name: pytest.fail("Unauthorized traffic must not import Core"))
    http = client(app)
    assert http.post("/v1/chat/completions", json=body()).status_code == 401
    assert http.get("/v1/models", headers={"Authorization": "Bearer gateway-user-token"}).status_code == 401
    assert http.put("/v1/config/capability-defaults/vision/default", json={}).status_code == 404
    assert http.get("/v1/docs").status_code == 404
    assert http.get("/v1/chat/completions").status_code == 404


def test_real_core_route_token_auth_and_open_cloud_key_guard(host, monkeypatch):
    data_dir, app = host
    core = importlib.import_module("abstractcore.server.app")
    from abstractcore.core.types import GenerateResponse
    calls = []
    class LLM:
        def generate(self, **kw):
            return GenerateResponse(content="mounted answer", model="stub", finish_reason="stop", usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
    def create(*args, **kw):
        calls.append(kw)
        return LLM()
    monkeypatch.setattr(core, "create_llm", create)
    monkeypatch.setenv("ABSTRACTCORE_AUTH_TOKEN", "different-standalone-token")
    monkeypatch.setenv("OPENAI_API_KEY", "stored-cloud-secret")
    settings = ce.change_settings(data_dir, enabled=True, reach="network")
    http = client(app)
    result = http.post("/v1/chat/completions", json=body(), headers={"Authorization": f"Bearer {settings.token}"})
    assert result.status_code == 200, result.text
    assert http.post("/v1/chat/completions", json=body(), headers={"Authorization": f"bearer {settings.token}"}).status_code == 200
    assert result.json()["choices"][0]["message"]["content"] == "mounted answer"
    response = http.post("/v1/responses", json={"model": "openai/gpt-4", "input": "hi"}, headers={"Authorization": f"Bearer {settings.token}"})
    assert response.status_code == 200, response.text
    ce.change_settings(data_dir, access="open")
    calls.clear()
    blocked = http.post("/v1/chat/completions", json=body())
    assert blocked.status_code == 401, blocked.text
    assert calls == []
    explicit = http.post("/v1/chat/completions", json=body(), headers={"X-AbstractCore-Provider-API-Key": "client-owned-key"})
    assert explicit.status_code == 200, explicit.text
    assert any(call.get("api_key") == "client-owned-key" for call in calls)
    assert http.post("/v1/chat/completions", json=body(), headers={"Authorization": "Bearer different-standalone-token"}).status_code == 401


@pytest.mark.parametrize("peer,headers,want", [
    ("192.168.1.2", [], True), ("::ffff:192.168.1.2", [], True),
    ("8.8.8.8", [], False), ("127.0.0.1", [(b"x-forwarded-for", b"8.8.8.8")], False),
])
def test_open_peer_boundary(host, peer, headers, want):
    data_dir, _ = host
    assert ce.open_access_allowed({"client": (peer, 123), "headers": headers}, data_dir) is want


def test_open_denied_by_internet_or_trusted_proxy(host, monkeypatch):
    data_dir, _ = host
    scope = {"client": ("192.168.1.2", 123), "headers": []}
    monkeypatch.setattr(ce, "resolve_network_setting", lambda path: {"mode": "internet"})
    assert not ce.open_access_allowed(scope, data_dir)
    with pytest.raises(ValueError): ce.change_settings(data_dir, enabled=True, access="open", reach="network")
    monkeypatch.setattr(ce, "resolve_network_setting", lambda path: {"mode": "lan"})
    monkeypatch.setattr(ce.ne, "live_reverse_proxy", lambda path: SimpleNamespace(trust_proxy=True))
    assert not ce.open_access_allowed(scope, data_dir)


def test_browser_origins_checked_before_core_dispatch(host, monkeypatch):
    _, app = host
    app.add_middleware(GatewaySecurityMiddleware, policy=GatewayAuthPolicy(enabled=True, allowed_origins=("https://allowed.test",)))
    http = client(app)
    assert http.get("/v1/models", headers={"Origin": "https://evil.test"}).status_code == 403
    assert http.get("/v1/models", headers={"Origin": "https://allowed.test"}).status_code == 404


def test_admin_controls_reject_user_and_never_disclose_token_in_status(host, monkeypatch):
    data_dir, app = host
    routes = importlib.import_module("abstractgateway.routes.core_endpoint")
    monkeypatch.setattr(routes, "gateway_data_dir_from_env", lambda: data_dir)
    app.include_router(routes.router, prefix="/api")
    @app.middleware("http")
    async def principal(request, call_next):
        request.state.gateway_principal = GatewayPrincipal("tester", roles=("admin",) if request.headers.get("x-test-admin") else ())
        return await call_next(request)
    http = client(app)
    for path in ["", "/token/rotate"]:
        assert http.post("/api/gateway/admin/core-endpoint" + path, json={}).status_code == 403
    assert http.get("/api/gateway/admin/core-endpoint").status_code == 403
    headers = {"x-test-admin": "yes"}
    rotated = http.post("/api/gateway/admin/core-endpoint/token/rotate", headers=headers)
    assert rotated.status_code == 200, rotated.text
    token = rotated.json()["token"]
    status = http.get("/api/gateway/admin/core-endpoint", headers=headers)
    assert token not in status.text
    assert status.headers["cache-control"] == "no-store"
    assert status.json()["base_url"].endswith("/v1") and not status.json()["base_url"].endswith("/core/v1")
    # Round 5: no route reveals a stored key (the rotate answer above is the only showing).
    reveal = http.post("/api/gateway/admin/core-endpoint/token/reveal", headers=headers)
    assert reveal.status_code in (404, 405) and token not in reveal.text
    assert http.post("/api/gateway/admin/core-endpoint", headers=headers, json={"enabled": "false"}).status_code == 422


def test_direct_asgi_stream_preserves_chunks_and_policy_until_stream_finishes(host, monkeypatch):
    import asyncio
    from abstractcore.server.auth_policy import current_server_auth_policy, server_auth_token
    data_dir, _ = host
    settings = ce.change_settings(data_dir, enabled=True, reach="network")
    messages = []
    async def serving(scope, receive, send):
        assert scope["path"] == "/v1/chat/completions"
        assert server_auth_token() == settings.token
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
        await send({"type": "http.response.body", "body": b"data: first\n\n", "more_body": True})
        assert len(messages) == 2  # First chunk reaches the caller before generation finishes.
        await asyncio.sleep(0)
        assert server_auth_token() == settings.token
        await send({"type": "http.response.body", "body": b"data: [DONE]\n\n", "more_body": False})
    monkeypatch.setattr(server_facade.importlib, "import_module", lambda name: SimpleNamespace(app=serving))
    async def receive(): return {"type": "http.request", "body": b"", "more_body": False}
    async def send(message): messages.append(message)
    async def run():
        await ce.CoreEndpoint()({"type": "http", "method": "POST", "path": "/v1/chat/completions", "root_path": "/core", "raw_path": b"/v1/chat/completions", "headers": [(b"authorization", f"Bearer {settings.token}".encode())], "client": ("192.168.1.2", 123)}, receive, send)
        assert current_server_auth_policy() is None
    asyncio.run(run())
    assert [item.get("body") for item in messages[1:]] == [b"data: first\n\n", b"data: [DONE]\n\n"]


def test_malformed_config_fails_closed(host):
    data_dir, app = host
    path = data_dir / "config/core_endpoint.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"enabled": "false", "access": "open", "token": "key"}))
    assert client(app).get("/v1/models").status_code == 503


def test_pending_internet_downgrade_and_public_explicit_bind_keep_open_access_denied(host, monkeypatch):
    data_dir, _ = host
    scope = {"client": ("192.168.1.20", 123), "headers": []}
    monkeypatch.setattr(ce.ne, "effective_bind", lambda path: {"mode": "internet", "bind_host": "0.0.0.0"})
    assert not ce.open_access_allowed(scope, data_dir)
    monkeypatch.setattr(ce.ne, "effective_bind", lambda path: {"mode": "lan", "bind_host": "8.8.8.8"})
    assert not ce.open_access_allowed(scope, data_dir)
    monkeypatch.setattr(ce.ne, "effective_bind", lambda path: {"mode": "lan", "bind_host": "0.0.0.0"})
    assert ce.open_access_allowed(scope, data_dir)


def test_persisted_open_settings_can_disable_or_rotate_after_internet_change(host, monkeypatch):
    data_dir, _ = host
    initial = ce.change_settings(data_dir, enabled=True, access="open", reach="network")
    monkeypatch.setattr(ce, "resolve_network_setting", lambda path: {"mode": "internet"})
    rotated = ce.change_settings(data_dir, rotate=True)
    assert rotated.token != initial.token
    assert ce.change_settings(data_dir, enabled=False).enabled is False
    assert ce.change_settings(data_dir, access="token").access == "token"
    with pytest.raises(ValueError): ce.change_settings(data_dir, access="open")


def test_running_network_record_retains_internet_mode_until_restart(tmp_path, monkeypatch):
    import os
    from abstractgateway import network_exposure as ne
    from abstractgateway.runtime_config import BIND_HOST_ENV
    ne.write_run_record(tmp_path, {"schema": ne.RUN_RECORD_SCHEMA, "pid": os.getpid(), "host": "0.0.0.0", "port": 8080, "mode": "internet", "host_source": "setting", "port_source": "setting"})
    monkeypatch.setenv(BIND_HOST_ENV, "0.0.0.0")
    monkeypatch.setenv(ne.BIND_PORT_ENV, "8080")
    assert ne.effective_bind(tmp_path)["mode"] == "internet"
    ne.write_run_record(tmp_path, {"schema": ne.RUN_RECORD_SCHEMA, "pid": os.getpid(), "host": "0.0.0.0", "port": 8080, "mode": "lan", "host_source": "setting", "port_source": "setting"})
    assert ne.effective_bind(tmp_path)["mode"] == "lan"
