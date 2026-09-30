"""Apps served through the gateway at `/apps/<id>/` (app_proxy.py) and the
open/handover contract that leads there (routes/apps.py).

A real upstream app (a small Starlette app under uvicorn on a scratch
loopback port) plays a mount-capable app: it answers
`X-AbstractFramework-App: observer; mount=1` and echoes what it receives.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from abstractgateway import apps_manager as am

_TOKEN = "app-proxy-test-token-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {_TOKEN}"}


# ---------------------------------------------------------------------------
# The upstream app
# ---------------------------------------------------------------------------


class Upstream:
    def __init__(self) -> None:
        from starlette.applications import Starlette
        from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
        from starlette.routing import Route, WebSocketRoute

        self.mount = True
        self.seen: List[Dict[str, Any]] = []
        up = self

        def headers_of(scope) -> Dict[str, str]:
            return {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}

        async def echo(request):
            body = await request.body()
            rec = {"method": request.method, "path": request.scope["raw_path"].decode(), "query": request.url.query, "headers": headers_of(request.scope), "body": body.decode(),
                   "xff_all": [v.decode() for k, v in request.scope["headers"] if k == b"x-forwarded-for"]}
            up.seen.append(rec)
            return JSONResponse(rec)

        async def page(request):
            up.seen.append({"path": "/", "headers": headers_of(request.scope)})
            return HTMLResponse("<!doctype html><title>AbstractObserver</title><p>upstream</p>")

        async def sse(request):
            async def gen():
                import asyncio

                for i in range(3):
                    yield f"event: tick\ndata: {i}\n\n".encode()
                    await asyncio.sleep(0.05)

            return StreamingResponse(gen(), media_type="text/event-stream")

        async def cookie(request):
            r = Response("ok")
            r.raw_headers.append((b"set-cookie", b"abstractobserver_pref=1; Path=/apps/observer/"))
            r.raw_headers.append((b"set-cookie", b"abstractgateway_session=hostile; Path=/"))
            return r

        async def ws(websocket):
            hdrs = headers_of(websocket.scope)
            await websocket.accept(subprotocol=(websocket.scope.get("subprotocols") or [None])[0])
            await websocket.send_text(json.dumps({"hello": hdrs.get("x-forwarded-for"), "prefix": hdrs.get("x-forwarded-prefix"), "cookie": hdrs.get("cookie"), "path": websocket.scope["path"]}))
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if msg.get("text") is not None:
                    await websocket.send_text("echo:" + msg["text"])
                else:
                    await websocket.send_bytes(b"echo:" + msg["bytes"])

        # The public assets a real app's dist/ carries (Code: manifest.webmanifest,
        # icon.svg, favicon.svg), with the app servers' Content-Types; a
        # single-page app answers a MISSING file with its HTML shell.
        def asset(ctype: str, body: bytes):
            async def serve(request):
                up.seen.append({"path": request.scope["raw_path"].decode(), "headers": headers_of(request.scope), "method": request.method})
                r = Response(body, media_type=ctype)
                r.raw_headers.append((b"set-cookie", b"abstractobserver_pref=1; Path=/apps/observer/"))
                return r

            return serve

        async def shell(request):
            up.seen.append({"path": request.scope["raw_path"].decode(), "headers": headers_of(request.scope), "method": request.method})
            return HTMLResponse("<!doctype html><script>window.__ABSTRACT_UI_CONFIG__={}</script><div id=root></div>")

        assets = [
            Route("/manifest.webmanifest", asset("application/manifest+json", b'{"name":"AbstractObserver","icons":[{"src":"icon.svg"}]}'), methods=["GET", "HEAD", "POST"]),
            Route("/icon.svg", asset("image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'/>")),
            Route("/favicon.ico", asset("image/x-icon", b"\x00\x00\x01\x00")),
            Route("/apple-touch-icon-180x180.png", asset("image/png", b"\x89PNG")),
            Route("/icons/icon-512.png", asset("image/png", b"\x89PNG")),
            Route("/favicon.svg", shell),  # missing file: the SPA shell
            Route("/icon-192.png", asset("text/html; charset=utf-8", b"<p>not an image</p>")),
        ]
        app = Starlette(routes=[Route("/", page), Route("/sse", sse), Route("/cookie", cookie), WebSocketRoute("/ws", ws), *assets, Route("/{rest:path}", echo, methods=["GET", "POST", "PUT", "DELETE", "HEAD"])])

        async def with_identity(scope, receive, send):
            async def send2(message):
                if message["type"] == "http.response.start" and up.mount:
                    message = dict(message)
                    message["headers"] = list(message.get("headers") or []) + [(b"x-abstractframework-app", b"observer; mount=1")]
                await send(message)

            await app(scope, receive, send2)

        import uvicorn

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.server = uvicorn.Server(uvicorn.Config(with_identity, host="127.0.0.1", port=self.port, log_level="warning", lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            time.sleep(0.02)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    up = Upstream()
    m = am.AppsManager(tmp_path / "runtime", urlopen=lambda *a, **k: (_ for _ in ()).throw(OSError("offline")), install_allowed=lambda: True)
    m.external_probe = lambda **kw: {"observer": am.ExternalApp("observer", up.port, f"http://127.0.0.1:{up.port}/", version="9.9.9")}
    m.gateway_url = "http://127.0.0.1:18823"
    import abstractgateway.app_proxy as proxy
    import abstractgateway.routes.apps as routes

    monkeypatch.setattr(routes, "get_apps_manager", lambda: m)
    monkeypatch.setattr(proxy, "get_apps_manager", lambda: m)
    proxy._session_cache.clear()
    from abstractgateway.app import app

    yield app, m, up
    up.stop()


def _signed_in(app, host: str = "127.0.0.1:18823") -> TestClient:
    """A browser that went through Open -> handover."""
    admin = TestClient(app, headers=AUTH)
    body = admin.post("/api/gateway/apps/observer/open", json={}, headers={"host": host}).json()
    browser = TestClient(app, base_url=f"http://{host}")
    h = browser.get(body["open_url"], follow_redirects=False)
    assert h.status_code == 303, h.text
    return browser


# ---------------------------------------------------------------------------
# Open / handover
# ---------------------------------------------------------------------------


def test_open_returns_app_path_and_works_from_another_address(env) -> None:
    app, m, _up = env
    admin = TestClient(app, headers=AUTH)
    row = [a for a in admin.get("/api/gateway/apps?latest=false").json()["apps"] if a["id"] == "observer"][0]
    assert row["mounted"] is True and row["app_path"] == "/apps/observer/"
    # A browser on the LAN (or behind a tunnel): no loopback-only 409 any more.
    r = admin.post("/api/gateway/apps/observer/open", json={"path": "/#runs", "origin": "https://gw.example"}, headers={"host": "192.168.1.20:8080"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mounted"] is True and body["app_path"] == "/apps/observer/#runs"
    assert body["app_url"] == "https://gw.example/apps/observer/#runs"
    # Without origin: what the request came in on.
    body = admin.post("/api/gateway/apps/observer/open", json={}, headers={"host": "192.168.1.20:8080"}).json()
    assert body["app_url"] == "http://192.168.1.20:8080/apps/observer/"


@pytest.mark.parametrize("origin", ["https://gw.example/path", "javascript:alert(1)", "https://u:p@gw.example", "https://gw.example?x", "gw.example", "https://gw.example:99999", "https://gw example"])
def test_open_refuses_an_origin_that_is_not_one(env, origin) -> None:
    app, m, _up = env
    r = TestClient(app, headers=AUTH).post("/api/gateway/apps/observer/open", json={"origin": origin}, headers={"host": "127.0.0.1:18823"})
    assert r.status_code == 400 and r.json()["reason"] == "invalid_origin"
    assert m._handover == {}


def test_handover_is_relative_and_scopes_cookies_to_the_app_path(env) -> None:
    app, _m, _up = env
    admin = TestClient(app, headers=AUTH)
    body = admin.post("/api/gateway/apps/observer/open", json={"path": "/#new"}, headers={"host": "gw.example"}).json()
    h = TestClient(app, base_url="https://gw.example").get(body["open_url"], follow_redirects=False, headers={"x-forwarded-proto": "https"})
    assert h.status_code == 303
    assert h.headers["location"] == "/apps/observer/#new"  # relative: any address the browser used
    cookies = h.headers.get_list("set-cookie")
    assert {c.split("=", 1)[0] for c in cookies} == {"abstractobserver_gateway_url", "abstractobserver_gateway_session", "abstractobserver_gateway_csrf"}
    assert all("Path=/apps/observer/" in c for c in cookies), cookies


def test_a_non_mountable_app_keeps_the_direct_link_and_the_loopback_rule(env) -> None:
    app, m, up = env
    up.mount = False
    m._mount_cache.clear()
    admin = TestClient(app, headers=AUTH)
    r = admin.post("/api/gateway/apps/observer/open", json={}, headers={"host": "192.168.1.20:8080"})
    assert r.status_code == 409 and r.json()["reason"] == "app_loopback_only"
    body = admin.post("/api/gateway/apps/observer/open", json={}, headers={"host": "127.0.0.1:18823"}).json()
    assert body["mounted"] is False and body["app_path"] is None
    assert body["app_url"] == f"http://127.0.0.1:{up.port}/"
    # ...and the gateway never serves it at /apps/observer/.
    browser = TestClient(app)
    assert browser.get("/apps/observer/x", headers={"accept": "application/json"}).json()["reason"] == "not_mountable"


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_without_a_session_navigation_goes_to_the_console_and_scripts_get_401(env) -> None:
    app, _m, up = env
    anon = TestClient(app)
    nav = anon.get("/apps/observer/runs/x?tab=1", headers={"sec-fetch-mode": "navigate"}, follow_redirects=False)
    assert nav.status_code == 303
    assert nav.headers["location"] == "/console#apps?open=observer&path=%2Fruns%2Fx%3Ftab%3D1"
    xhr = anon.get("/apps/observer/api/gateway/runs", headers={"sec-fetch-mode": "cors"})
    assert xhr.status_code == 401 and xhr.json()["reason"] == "app_sign_in_required"
    # A console session cookie is not an app session.
    bad = anon.get("/apps/observer/assets/a.js", headers={"cookie": "abstractgateway_session=whatever; abstractobserver_gateway_session=forged"})
    assert bad.status_code == 401
    assert all(s["path"] != "/assets/a.js" for s in up.seen), "nothing reached the app"


def test_root_without_slash_redirects_and_unknown_apps_are_404(env) -> None:
    app, _m, _up = env
    anon = TestClient(app)
    r = anon.get("/apps/observer?x=1", follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "/apps/observer/?x=1"
    assert anon.get("/apps/nope/x", headers={"accept": "application/json"}).status_code == 404


# ---------------------------------------------------------------------------
# Relay
# ---------------------------------------------------------------------------


def test_signed_in_requests_reach_the_app_prefix_stripped_with_only_its_cookies(env) -> None:
    app, _m, up = env
    browser = _signed_in(app)
    browser.cookies.set("abstractgateway_session", "console-cookie", domain="127.0.0.1", path="/")
    browser.cookies.set("other_app", "1", domain="127.0.0.1", path="/")
    r = browser.post(
        "/apps/observer/api/thing%20one?q=1",
        content=b'{"a":1}',
        headers={
            "content-type": "application/json",
            "authorization": "Bearer stolen",
            "x-forwarded-for": "10.9.9.9",
            "x-forwarded-prefix": "/evil",
            "x-abstractgateway-session": "agws_x.y",
        },
    )
    assert r.status_code == 200, r.text
    seen = r.json()
    assert seen["path"] == "/api/thing%20one" and seen["query"] == "q=1" and seen["body"] == '{"a":1}'
    h = seen["headers"]
    assert "authorization" not in h and "x-abstractgateway-session" not in h
    assert h["x-forwarded-prefix"] == "/apps/observer"
    assert seen["xff_all"] == ["testclient"]  # ONE value: the peer, never the browser-sent one
    assert h["x-forwarded-proto"] == "http" and h["x-forwarded-host"] == "127.0.0.1:18823"
    names = {c.split("=", 1)[0].strip() for c in h["cookie"].split(";")}
    assert "console-cookie" not in h["cookie"] and "other_app" not in h["cookie"]
    assert names == {"abstractobserver_gateway_url", "abstractobserver_gateway_session", "abstractobserver_gateway_csrf"}
    assert h["host"] == f"127.0.0.1:{up.port}"


def test_the_app_may_set_only_its_own_cookies(env) -> None:
    app, _m, _up = env
    r = _signed_in(app).get("/apps/observer/cookie")
    got = r.headers.get_list("set-cookie")
    assert got == ["abstractobserver_pref=1; Path=/apps/observer/"]


def test_sse_is_relayed(env) -> None:
    app, _m, _up = env
    browser = _signed_in(app)
    with browser.stream("GET", "/apps/observer/sse") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        text = b"".join(r.iter_raw()).decode()
    assert text.count("event: tick") == 3


def test_websocket_is_relayed_and_gated(env) -> None:
    app, _m, _up = env
    browser = _signed_in(app)
    # The test client's WebSocket carries neither the jar nor base_url: say them.
    jar = "; ".join(f"{c.name}={c.value}" for c in browser.cookies.jar)
    ok = {"host": "127.0.0.1:18823", "cookie": jar}
    with browser.websocket_connect("/apps/observer/ws", headers={**ok, "origin": "http://127.0.0.1:18823"}, subprotocols=["v1"]) as ws:
        hello = json.loads(ws.receive_text())
        assert hello["prefix"] == "/apps/observer" and hello["hello"] == "testclient" and hello["path"] == "/ws"
        assert "abstractobserver_gateway_session=" in hello["cookie"]
        ws.send_text("ping")
        assert ws.receive_text() == "echo:ping"
        ws.send_bytes(b"\x00\x01")
        assert ws.receive_bytes() == b"echo:\x00\x01"
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):  # another site's page
        with browser.websocket_connect("/apps/observer/ws", headers={**ok, "origin": "https://evil.example"}) as ws:
            ws.receive_text()
    with pytest.raises(WebSocketDisconnect):  # no session
        with TestClient(app).websocket_connect("/apps/observer/ws", headers={"host": "127.0.0.1:18823", "origin": "http://127.0.0.1:18823"}) as ws:
            ws.receive_text()


def test_handover_routes_still_win_over_the_app_routes(env) -> None:
    app, _m, _up = env
    r = TestClient(app).get("/apps/handover/not-a-code", follow_redirects=False)
    assert r.status_code == 410


def test_another_origin_never_reaches_the_app(env) -> None:
    """A page on another origin (another site, or another port on this host:
    same-site, so the Lax cookies ride along) cannot read or write the app."""
    app, _m, up = env
    browser = _signed_in(app)
    before = len(up.seen)
    for origin in ("http://127.0.0.1:9999", "https://evil.example"):
        r = browser.post("/apps/observer/api/thing", content=b"{}", headers={"origin": origin})
        assert r.status_code == 403 and r.json()["reason"] == "cross_origin", origin
    assert len(up.seen) == before
    ok = browser.post("/apps/observer/api/thing", content=b"{}", headers={"origin": "http://127.0.0.1:18823"})
    assert ok.status_code == 200


@pytest.mark.parametrize("bad_host", ["a_b.attacker.com", "a_b.attacker.com:18823", "evil host", "x.example/path", ""])
def test_a_host_the_proxy_cannot_forward_is_refused_never_relayed_without_x_forwarded_host(env, bad_host) -> None:
    """The app decides "is this browser on the gateway machine?" from the
    loopback peer AND a loopback Host / X-Forwarded-Host (abstractuic kit
    0.1.14 clientIsLoopback). Browsers accept Host names this proxy's pattern
    does not (Chrome: `a_b.attacker.com`); relaying such a request WITHOUT
    X-Forwarded-Host would leave the app only the gateway's own loopback Host,
    i.e. a remote page treated as local. The proxy answers 400 instead, for
    HTTP and WebSocket alike, and the app never sees the request."""
    from starlette.websockets import WebSocketDisconnect

    app, _m, up = env
    browser = _signed_in(app)
    jar = "; ".join(f"{c.name}={c.value}" for c in browser.cookies.jar)
    before = len(up.seen)
    r = browser.get("/apps/observer/api/thing", headers={"host": bad_host})
    assert r.status_code == 400 and r.json()["reason"] == "invalid_host", (bad_host, r.status_code, r.text[:200])
    assert len(up.seen) == before, "the app must never receive a request without X-Forwarded-Host"
    with pytest.raises(WebSocketDisconnect):
        with browser.websocket_connect("/apps/observer/ws", headers={"host": bad_host, "cookie": jar}) as ws:
            ws.receive_text()


def test_every_relayed_request_carries_x_forwarded_host(env) -> None:
    from abstractgateway import app_proxy as proxy

    spec = am.APP_BY_ID["observer"]
    fwd = dict(proxy._forward_headers({"host": "gw.example:8443"}, spec=spec, client="203.0.113.9", proto="https", host="gw.example:8443"))
    assert fwd["x-forwarded-host"] == "gw.example:8443"
    for bad in ("a_b.attacker.com", "", "evil host"):
        with pytest.raises(ValueError):
            proxy._forward_headers({"host": bad}, spec=spec, client="203.0.113.9", proto="https", host=bad)


# ---------------------------------------------------------------------------
# Public assets: what a browser fetches WITHOUT cookies (manifest, icons)
# ---------------------------------------------------------------------------

# Headers a browser sends for `<link rel=manifest>` (credentials omitted) behind
# `tailscale serve`: Host preserved, same-origin Origin, no cookie.
TS_HOST = "mac-mini.tail43a344.ts.net"


@pytest.mark.parametrize(
    "path,ctype",
    [
        ("/manifest.webmanifest", "application/manifest+json"),
        ("/icon.svg", "image/svg+xml"),
        ("/favicon.ico", "image/x-icon"),
        ("/apple-touch-icon-180x180.png", "image/png"),
        ("/icons/icon-512.png", "image/png"),
    ],
)
@pytest.mark.parametrize(
    "host,origin,base",
    [
        ("127.0.0.1:18823", None, "http://127.0.0.1:18823"),
        (TS_HOST, f"https://{TS_HOST}", f"https://{TS_HOST}"),
        ("192.168.1.20:8080", "http://192.168.1.20:8080", "http://192.168.1.20:8080"),
    ],
)
def test_manifest_and_icons_are_served_without_a_session(env, path, ctype, host, origin, base) -> None:
    """The manifest 401 behind a proxy (operator P1, 2026-09-30): browsers fetch
    `<link rel=manifest>` (and a manifest's icons, and iOS its home-screen
    icon) without cookies. Those paths answer without the app session, with
    NO cookie forwarded and NO cookie set; everything else stays gated."""
    app, _m, up = env
    anon = TestClient(app, base_url=base)
    headers = {"host": host, "sec-fetch-mode": "cors", "sec-fetch-dest": "manifest" if "manifest" in path else "image"}
    if origin:
        headers["origin"] = origin
    # A stale app cookie (expired session) and the console's own: neither may reach the app.
    headers["cookie"] = "abstractobserver_gateway_session=expired; abstractgateway_session=console"
    r = anon.get("/apps/observer" + path, headers=headers)
    assert r.status_code == 200, (path, r.status_code, r.text[:200])
    assert r.headers["content-type"].startswith(ctype)
    assert r.headers.get_list("set-cookie") == [], "a public asset never sets a cookie"
    seen = up.seen[-1]
    assert seen["path"] == path
    assert "cookie" not in seen["headers"], "no cookie is forwarded for a public asset"
    assert seen["headers"]["x-forwarded-host"] == host and seen["headers"]["x-forwarded-prefix"] == "/apps/observer"
    head = anon.head("/apps/observer" + path, headers=headers)
    assert head.status_code == 200


@pytest.mark.parametrize(
    "method,path,why",
    [
        ("GET", "/favicon.svg", "a missing icon answered with the app's HTML shell"),
        ("GET", "/icon-192.png", "an allowlisted name the app answers as text/html"),
        ("GET", "/", "the page"),
        ("GET", "/index.html", "the page by name"),
        ("GET", "/assets/index-abc.js", "the bundle"),
        ("GET", "/sw.js", "the service worker"),
        ("GET", "/api/gateway/runs", "the API"),
        ("GET", "/api/icon.svg", "an icon name under api/"),
        ("GET", "/api/manifest.webmanifest", "a manifest name under api/"),
        ("GET", "/x/manifest.webmanifest", "a manifest name in a subdirectory"),
        ("GET", "/icons/../api/x.png", "dot segments"),
        ("GET", "/icons/%2e%2e%2fapi.png", "encoded dot segments"),
        ("GET", "/icon%2Esvg", "an encoded name"),
        ("GET", "/icons/a/b.png", "two levels under icons/"),
        ("POST", "/manifest.webmanifest", "a write"),
        ("GET", "/manifest.webmanifest.js", "a suffix"),
    ],
)
def test_everything_else_still_needs_the_app_session(env, method, path, why) -> None:
    app, _m, up = env
    anon = TestClient(app, base_url=f"https://{TS_HOST}")
    r = anon.request(method, "/apps/observer" + path, headers={"host": TS_HOST, "origin": f"https://{TS_HOST}", "sec-fetch-mode": "cors"})
    assert r.status_code == 401 and r.json()["reason"] == "app_sign_in_required", (why, r.status_code, r.text[:200])
    assert "__ABSTRACT_UI_CONFIG__" not in r.text


def test_public_asset_path_matrix() -> None:
    from abstractgateway.app_proxy import public_asset_path

    for ok in ("/manifest.webmanifest", "/manifest.webmanifest?v=3", "/manifest.json", "/site.webmanifest", "/favicon.ico", "/favicon-32x32.png",
               "/icon.svg", "/icon-192.png", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png", "/icons/maskable_512.webp"):
        assert public_asset_path("GET", ok), ok
        assert public_asset_path("HEAD", ok), ok
        assert not public_asset_path("POST", ok), ok
        assert not public_asset_path("OPTIONS", ok), ok
    for bad in ("", "/", "/index.html", "/api/icon.svg", "/icons/../x.png", "/icon..svg/..", "/icons/a/b.png", "/icon%2esvg", "/ICON.SVG", "/favicon.ico/x"):
        assert not public_asset_path("GET", bad), bad


def test_a_signed_in_browser_still_gets_its_cookies_on_the_same_paths(env) -> None:
    """With a session the request is an ordinary one: the app's cookies go
    through and the app may set its own."""
    app, _m, up = env
    browser = _signed_in(app)
    r = browser.get("/apps/observer/manifest.webmanifest")
    assert r.status_code == 200
    assert "abstractobserver_gateway_session=" in up.seen[-1]["headers"].get("cookie", "")
    assert r.headers.get_list("set-cookie") == ["abstractobserver_pref=1; Path=/apps/observer/"]


# ---------------------------------------------------------------------------
# Behind `tailscale serve` (Host preserved, X-Forwarded-Proto https from a
# loopback peer, so the request's scheme is https)
# ---------------------------------------------------------------------------


def test_the_whole_app_flow_behind_a_tls_proxy_on_this_machine(env) -> None:
    """Console-side open -> handover -> app page -> API -> SSE -> WebSocket,
    all on https://<host>.ts.net with the browser's Origin (the operator's
    P1 path). Before the gateway accepted an https page asking its own
    address, every POST/WebSocket with that Origin was 403 (origin not
    allowed) at the security middleware."""
    app, _m, up = env
    base = f"https://{TS_HOST}"
    origin = {"origin": base}
    admin = TestClient(app, headers=AUTH, base_url=base)
    body = admin.post("/api/gateway/apps/observer/open", json={"origin": base}, headers=origin)
    assert body.status_code == 200, body.text
    assert body.json()["app_url"] == f"{base}/apps/observer/"
    browser = TestClient(app, base_url=base)
    h = browser.get(body.json()["open_url"], follow_redirects=False)
    assert h.status_code == 303 and h.headers["location"] == "/apps/observer/"
    cookies = h.headers.get_list("set-cookie")
    assert cookies and all("Secure" in c and "Path=/apps/observer/" in c and "SameSite=lax" in c for c in cookies), cookies
    r = browser.post("/apps/observer/api/thing", content=b"{}", headers={**origin, "content-type": "application/json"})
    assert r.status_code == 200, r.text
    assert r.json()["headers"]["x-forwarded-proto"] == "https" and r.json()["headers"]["x-forwarded-host"] == TS_HOST
    with browser.stream("GET", "/apps/observer/sse", headers=origin) as s:
        assert b"".join(s.iter_raw()).decode().count("event: tick") == 3
    jar = "; ".join(f"{c.name}={c.value}" for c in browser.cookies.jar)
    with browser.websocket_connect("/apps/observer/ws", headers={"host": TS_HOST, "cookie": jar, **origin}) as ws:
        assert json.loads(ws.receive_text())["prefix"] == "/apps/observer"
        ws.send_text("ping")
        assert ws.receive_text() == "echo:ping"


def test_apps_overview_names_the_callers_address(env) -> None:
    """`gateway_url` stays where the app servers reach the gateway;
    `browser_gateway_url` is the https origin the remote caller uses."""
    app, _m, _up = env
    d = TestClient(app, headers=AUTH, base_url=f"https://{TS_HOST}").get("/api/gateway/apps?latest=false", headers={"x-forwarded-proto": "https"}).json()
    assert d["gateway_url"] == "http://127.0.0.1:18823"
    assert d["browser_gateway_url"] == f"https://{TS_HOST}"
    d = TestClient(app, headers=AUTH, base_url="http://127.0.0.1:18823").get("/api/gateway/apps?latest=false").json()
    assert d["browser_gateway_url"] == "http://127.0.0.1:18823"
