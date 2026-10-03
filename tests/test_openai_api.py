"""The OpenAI API at /v1: gateway-token keys, who can connect, the request log
(audit lines, no new store), Stop/Restart ending open requests, the /core/v1
308 alias, and the detected-address origins (LAN, Tailscale) of the Network page.

Hermetic: scratch data dir, stubbed interface discovery and Tailscale, Core's
serving app replaced by a stub (no provider keys, no model loads)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from abstractgateway import core_endpoint as ce
from abstractgateway import network_exposure as ne
from abstractruntime.integrations.abstractcore import server_facade

pytestmark = pytest.mark.basic

ADMIN = {"Authorization": "Bearer admin-token-long-enough"}
USER_TOKEN = "openai-user-token-0001"
CHAT = {"model": "ollama/qwen3:4b", "messages": [{"role": "user", "content": "hi"}]}


def _discover():
    return [ne.IfaceAddr("en0", "192.168.1.50", "ipv4", True), ne.IfaceAddr("utun4", "100.101.102.103", "ipv4", True)], "stub"


TS = {"dns_name": "studio.tail1234.ts.net", "ips": ["100.101.102.103"]}


class CoreStub:
    """Stands in for Core's serving app: records what reached it."""

    def __init__(self):
        self.calls = []
        self.stream = False

    async def app(self, scope, receive, send):
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body") or b""
            if not msg.get("more_body"):
                break
        self.calls.append({"path": scope["path"], "headers": dict(scope["headers"]), "body": body})
        if scope["path"] == "/v1/models":
            payload = json.dumps({"object": "list", "data": [{"id": "ollama/qwen3:4b"}]}).encode()
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": payload})
            return
        if self.stream:
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
            await send({"type": "http.response.body", "body": b'data: {"choices":[{"delta":{"content":"he"}}]}\n\n', "more_body": True})
            await send({"type": "http.response.body", "body": b'data: {"model":"ollama/qwen3:4b","usage":{"prompt_tokens":7,', "more_body": True})
            await send({"type": "http.response.body", "body": b'"completion_tokens":3}}\n\ndata: [DONE]\n\n', "more_body": False})
            return
        payload = json.dumps({"model": "ollama/qwen3:4b", "choices": [{"message": {"content": "ok"}}],
                              "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}}).encode()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": payload})


@pytest.fixture
def gw(tmp_path, monkeypatch):
    data = tmp_path / "runtime"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "admin-token-long-enough")
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    for name in ("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "ABSTRACTGATEWAY_TRUST_PROXY", ne.NETWORK_EXPORTS_ENV,
                 "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ne, "discover_interfaces", _discover)
    monkeypatch.setattr(ne, "bonjour_hostname", lambda: None)
    monkeypatch.setattr(ne, "tailscale_status", lambda: TS)
    monkeypatch.setattr(ne, "_DETECTED", {"at": 0.0, "value": None, "busy": False})
    stub = CoreStub()
    monkeypatch.setattr(server_facade.importlib, "import_module", lambda name: SimpleNamespace(app=stub.app))
    from abstractgateway.routes import gateway_router
    from abstractgateway.routes.core_endpoint import router, user_router
    from abstractgateway.routes.network import router as network_router
    from abstractgateway.security import GatewaySecurityMiddleware, load_gateway_auth_policy_from_env

    app = FastAPI()
    app.add_middleware(GatewaySecurityMiddleware, policy=load_gateway_auth_policy_from_env())
    app.include_router(network_router, prefix="/api")
    app.include_router(gateway_router, prefix="/api")
    app.include_router(router, prefix="/api")
    app.include_router(user_router, prefix="/api")
    app.mount("/v1", ce.CoreEndpoint())
    app.mount("/core", ce.LegacyCoreRedirect())
    admin = TestClient(app, client=("127.0.0.1", 50000))
    made = admin.post("/api/gateway/admin/users", headers=ADMIN,
                      json={"user_id": "alice", "tenant_id": "default", "roles": ["user"], "token": USER_TOKEN})
    assert made.status_code == 200, made.text
    return SimpleNamespace(app=app, data=data, stub=stub, admin=admin)


def _peer(gw, ip):
    return TestClient(gw.app, client=(ip, 50000))


def _audit(gw):
    path = gw.data / "audit_log.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines() if '"openai_api"' in x] if path.exists() else []


def _set(gw, **body):
    r = gw.admin.post("/api/gateway/admin/core-endpoint", headers=ADMIN, json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ---- base URL and alias --------------------------------------------------

def test_base_url_is_v1_and_core_v1_redirects_308(gw):
    status = _set(gw, enabled=True)
    assert status["base_url"].endswith("/v1") and not status["base_url"].endswith("/core/v1")
    assert status["legacy_base_url"].endswith("/core/v1")
    me = {"Authorization": f"Bearer {USER_TOKEN}"}
    ok = gw.admin.get("/v1/models", headers=me)
    assert ok.status_code == 200, ok.text
    moved = gw.admin.post("/core/v1/chat/completions?x=1", headers=me, json=CHAT, follow_redirects=False)
    assert moved.status_code == 308 and moved.headers["location"] == "/v1/chat/completions?x=1"
    followed = gw.admin.post("/core/v1/chat/completions", headers=me, json=CHAT)
    assert followed.status_code == 200 and followed.json()["choices"][0]["message"]["content"] == "ok"
    assert gw.admin.get("/core/docs", follow_redirects=False).status_code == 404


# ---- keys ----------------------------------------------------------------

def test_api_key_is_the_callers_gateway_token_and_never_reaches_core(gw):
    settings = ce.read_settings(gw.data)
    _set(gw, enabled=True)
    settings = ce.read_settings(gw.data)
    r = gw.admin.post("/v1/chat/completions", headers={"Authorization": f"Bearer {USER_TOKEN}"}, json=CHAT)
    assert r.status_code == 200, r.text
    forwarded = gw.stub.calls[-1]["headers"][b"authorization"].decode()
    assert forwarded == f"Bearer {settings.token}" and USER_TOKEN not in forwarded
    assert gw.admin.post("/v1/chat/completions", json=CHAT).status_code == 401
    assert gw.admin.post("/v1/chat/completions", headers={"Authorization": "Bearer wrong-token"}, json=CHAT).status_code == 401
    # The 0.12.0 endpoint key keeps working for one release.
    assert gw.admin.post("/v1/chat/completions", headers={"Authorization": f"Bearer {settings.token}"}, json=CHAT).status_code == 200


def test_open_mode_accepts_any_key_text_as_anonymous_without_core_credential(gw):
    _set(gw, enabled=True, reach="network")
    _set(gw, access="open")
    lan = _peer(gw, "192.168.1.20")
    r = lan.post("/v1/chat/completions", headers={"Authorization": "Bearer not-needed"}, json=CHAT)
    assert r.status_code == 200, r.text
    assert b"authorization" not in gw.stub.calls[-1]["headers"]
    assert _audit(gw)[-1]["openai_api"]["client"] == "anonymous"


def test_refused_keys_count_toward_the_lockout(gw):
    _set(gw, enabled=True)
    for _ in range(12):
        last = gw.admin.post("/v1/chat/completions", headers={"Authorization": "Bearer guess"}, json=CHAT)
    assert last.status_code == 429
    # A valid key is never locked out.
    assert gw.admin.post("/v1/chat/completions", headers={"Authorization": f"Bearer {USER_TOKEN}"}, json=CHAT).status_code == 200


# ---- who can connect -----------------------------------------------------

@pytest.mark.parametrize("reach,peer,want", [
    ("machine", "127.0.0.1", 200), ("machine", "192.168.1.20", 403), ("machine", "100.101.1.2", 403),
    ("network", "192.168.1.20", 200), ("network", "100.101.1.2", 403), ("network", "8.8.8.8", 403),
    ("tailnet", "100.101.1.2", 200), ("tailnet", "192.168.1.20", 200), ("tailnet", "8.8.8.8", 403),
])
def test_who_can_connect_filters_on_the_client_address(gw, reach, peer, want):
    _set(gw, enabled=True, reach=reach)
    r = _peer(gw, peer).post("/v1/chat/completions", headers={"Authorization": f"Bearer {USER_TOKEN}"}, json=CHAT)
    assert r.status_code == want, r.text


def test_a_local_proxy_names_the_client(gw):
    # tailscale serve: loopback peer + X-Forwarded-For of the tailnet device.
    _set(gw, enabled=True, reach="network")
    headers = {"Authorization": f"Bearer {USER_TOKEN}", "X-Forwarded-For": "100.101.1.2"}
    assert gw.admin.post("/v1/chat/completions", headers=headers, json=CHAT).status_code == 403
    _set(gw, reach="tailnet")
    assert gw.admin.post("/v1/chat/completions", headers=headers, json=CHAT).status_code == 200
    # A proxy that hides the client is treated as the internet.
    hidden = {"Authorization": f"Bearer {USER_TOKEN}", "X-Forwarded-Proto": "https"}
    assert gw.admin.post("/v1/chat/completions", headers=hidden, json=CHAT).status_code == 403
    assert ce.classify_client({"client": ("192.168.1.9", 1), "headers": [(b"x-forwarded-for", b"127.0.0.1")]},
                              trust_proxy=False) == "network"


def test_anywhere_needs_the_internet_acknowledgement_and_a_key(gw):
    r = gw.admin.post("/api/gateway/admin/core-endpoint", headers=ADMIN, json={"reach": "anywhere"})
    assert r.status_code == 409 and "Network page" in r.json()["detail"]
    status = gw.admin.get("/api/gateway/openai-api", headers=ADMIN).json()
    anywhere = next(o for o in status["reach_options"] if o["id"] == "anywhere")
    assert anywhere["available"] is False and anywhere["reason"]
    tailnet = next(o for o in status["reach_options"] if o["id"] == "tailnet")
    assert tailnet["shown"] is True


def test_open_mode_warning_names_who_can_use_it(gw):
    status = _set(gw, enabled=True, reach="network", access="open")
    texts = [w["text"] for w in status["warnings"]]
    assert any(t.startswith("Without a key, any device on your network") for t in texts)


# ---- request log ---------------------------------------------------------

def test_requests_are_logged_from_the_audit_file_newest_first(gw):
    _set(gw, enabled=True)
    me = {"Authorization": f"Bearer {USER_TOKEN}"}
    assert gw.admin.post("/v1/chat/completions", headers=me, json=CHAT).status_code == 200
    gw.stub.stream = True
    streamed = gw.admin.post("/v1/chat/completions", headers={**me, "X-AbstractCore-Run-Id": "run-42"},
                             json={**CHAT, "stream": True})
    assert streamed.status_code == 200 and b"[DONE]" in streamed.content
    gw.stub.stream = False
    assert gw.admin.post("/v1/chat/completions", headers=ADMIN, json=CHAT).status_code == 200
    lines = _audit(gw)
    assert [x["openai_api"].get("prompt_tokens") for x in lines[-3:]] == [11, 7, 11]
    rows = gw.admin.get("/api/gateway/openai-api/logs", headers=ADMIN).json()["rows"]
    assert [r["client"] for r in rows[:3]] == ["admin", "alice", "alice"]
    s = rows[1]
    assert (s["model"], s["prompt_tokens"], s["completion_tokens"], s["stream"], s["status"]) == ("ollama/qwen3:4b", 7, 3, True, 200)
    assert s["run_id"] == "run-42" and s["observer_path"].endswith("#run/run-42")
    assert rows[2]["observer_path"] is None
    # Anyone else sees only their own requests.
    own = gw.admin.get("/api/gateway/openai-api/logs", headers=me).json()
    assert own["scope"] == "own" and {r["client"] for r in own["rows"]} == {"alice"}


def test_usage_capture_handles_responses_api_and_split_lines():
    cap = ce.UsageCapture()
    cap.request_chunk(b'{"model":"lmstudio/q","input":"hi","stream":true}')
    cap.response_start({"headers": [(b"content-type", b"text/event-stream; charset=utf-8")]})
    cap.response_chunk(b'event: response.completed\ndata: {"type":"response.completed","response":{"usage":{"input_tokens":4,')
    cap.response_chunk(b'"output_tokens":9}}}\n\n')
    assert cap.summary() == {"stream": True, "model": "lmstudio/q", "prompt_tokens": 4, "completion_tokens": 9}


# ---- Stop / Restart ------------------------------------------------------

def test_stop_and_restart_end_open_requests():
    ended = []

    async def run():
        sent = []
        started = asyncio.Event()

        async def core(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            started.set()
            for _ in range(50):
                await asyncio.sleep(0.01)
                await send({"type": "http.response.body", "body": b"x", "more_body": True})

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        async def serve(scope, receive_, send_, **kw):
            await core(scope, receive_, send_)

        ce_serve = ce.serve_core_request
        ce.serve_core_request = serve
        try:
            ep = ce.CoreEndpoint()
            original = ce.read_settings
            ce.read_settings = lambda d: ce.EndpointSettings(True, "token", "machine", "k")
            try:
                task = asyncio.create_task(ep({"type": "http", "method": "POST", "path": "/v1/chat/completions",
                                               "headers": [(b"authorization", b"Bearer k")], "client": ("127.0.0.1", 1)},
                                              receive, send))
                await started.wait()
                assert ce.inflight_count() == 1
                ended.append(ce.end_inflight_requests())
                await asyncio.wait_for(task, 2)
            finally:
                ce.read_settings = original
        finally:
            ce.serve_core_request = ce_serve
        return sent

    sent = asyncio.run(run())
    assert ended == [1] and ce.inflight_count() == 0
    assert len(sent) < 10  # the stream stopped instead of running to its 50 chunks


def test_restart_route_needs_a_running_api(gw):
    assert gw.admin.post("/api/gateway/admin/core-endpoint/restart", headers=ADMIN).status_code == 409
    _set(gw, enabled=True)
    r = gw.admin.post("/api/gateway/admin/core-endpoint/restart", headers=ADMIN)
    assert r.status_code == 200 and r.json()["ended_requests"] == 0


def test_turning_the_endpoint_off_ends_open_requests(gw):
    _set(gw, enabled=True)
    ev = asyncio.Event()
    with ce._INFLIGHT_LOCK:
        ce._INFLIGHT.add(ev)
    try:
        assert _set(gw, enabled=False)["ended_requests"] == 1 and ev.is_set()
    finally:
        with ce._INFLIGHT_LOCK:
            ce._INFLIGHT.discard(ev)


def test_browser_origin_rules_for_the_api(gw):
    _set(gw, enabled=True)
    evil = "https://some-web-app.example"
    keyed = gw.admin.get("/v1/models", headers={"Origin": evil, "Authorization": f"Bearer {USER_TOKEN}"})
    assert keyed.status_code == 200, keyed.text  # a browser SDK with a key
    bare = gw.admin.get("/v1/models", headers={"Origin": evil})
    assert bare.status_code == 403 and bare.json()["error"]["code"] == "origin_not_allowed"


def test_check_setup_lists_models_through_core(gw):
    _set(gw, enabled=True)
    r = gw.admin.post("/api/gateway/admin/core-endpoint/check", headers=ADMIN)
    assert r.status_code == 200, r.text
    checks = {c["id"]: c for c in r.json()["checks"]}
    assert checks["running"]["ok"] and checks["core"]["ok"]
    assert checks["models"]["ok"] and checks["models"]["text"].startswith("1 model ")


def test_status_is_readable_by_every_account_but_changes_are_admin_only(gw):
    me = {"Authorization": f"Bearer {USER_TOKEN}"}
    status = gw.admin.get("/api/gateway/openai-api", headers=me)
    assert status.status_code == 200 and status.json()["writable"] is False
    assert status.json()["key"] == {"own_token": True, "user_id": "alice"}
    assert gw.admin.post("/api/gateway/admin/core-endpoint", headers=me, json={"enabled": True}).status_code == 403
    assert gw.admin.get("/api/gateway/openai-api", headers=ADMIN).json()["key"]["own_token"] is False


# ---- Network: detected addresses and origins -----------------------------

def test_tailscale_status_parse_and_absent_binary(monkeypatch):
    text = json.dumps({"BackendState": "Running", "Self": {"DNSName": "Studio.tail1234.ts.net.",
                       "TailscaleIPs": ["100.101.102.103", "fd7a:115c:a1e0::1"]}, "MagicDNSSuffix": "tail1234.ts.net"})
    assert ne.parse_tailscale_status(text) == {"dns_name": "studio.tail1234.ts.net",
                                              "ips": ["100.101.102.103", "fd7a:115c:a1e0::1"]}
    stopped = json.loads(text)
    stopped["BackendState"] = "Stopped"
    assert ne.parse_tailscale_status(json.dumps(stopped)) is None
    assert ne.parse_tailscale_status("not json") is None
    monkeypatch.setattr(ne, "_TAILSCALE_CACHE", {"at": 0.0, "value": None})
    monkeypatch.setattr(ne, "tailscale_binary", lambda: None)
    assert ne.tailscale_status() is None
    # The macOS app's bundle binary is the CLI only under its lowercase name.
    assert "/Applications/Tailscale.app/Contents/MacOS/tailscale" in ne._TAILSCALE_CANDIDATES


def test_network_lists_the_tailscale_name(gw):
    d = gw.admin.get("/api/gateway/network", headers=ADMIN).json()
    ts = [a for a in d["addresses"] if a["kind"] == "tailscale"]
    assert ts and ts[0]["host"] == "studio.tail1234.ts.net" and ts[0]["https_url"] == "https://studio.tail1234.ts.net"
    vpn = next(a for a in d["addresses"] if a.get("host") == "100.101.102.103")
    assert vpn["interface_label"] == "Tailscale"
    assert d["tailscale"]["dns_name"] == "studio.tail1234.ts.net"


def test_detected_addresses_are_accepted_origins_without_manual_entry(gw):
    ne.detected_hosts(wait=True)
    http = TestClient(gw.app, client=("192.168.1.20", 50000), base_url="http://192.168.1.50:8080")
    for origin in ("http://192.168.1.50:8080", "http://studio.tail1234.ts.net:8080", "https://studio.tail1234.ts.net"):
        assert http.get("/api/gateway/network", headers={**ADMIN, "Origin": origin}).status_code == 200, origin
    for origin in ("http://192.168.1.50:9999", "http://evil.example:8080", "https://192.168.1.50"):
        assert http.get("/api/gateway/network", headers={**ADMIN, "Origin": origin}).status_code == 403, origin


def test_forwarded_client_address_is_believed_from_a_local_proxy_only(gw):
    xff = {**ADMIN, "X-Forwarded-For": "100.101.1.2"}
    assert gw.admin.post("/api/gateway/network", headers=xff, json={"trust_proxy": False}).status_code == 200
    lines = [json.loads(x) for x in (gw.data / "audit_log.jsonl").read_text().splitlines()]
    assert lines[-1]["ip"] == "100.101.1.2"
    remote = _peer(gw, "192.168.1.20")
    assert remote.post("/api/gateway/network", headers=xff, json={"trust_proxy": False}).status_code == 200
    lines = [json.loads(x) for x in (gw.data / "audit_log.jsonl").read_text().splitlines()]
    assert lines[-1]["ip"] == "192.168.1.20"
