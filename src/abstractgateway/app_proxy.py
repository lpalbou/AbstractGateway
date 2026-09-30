"""Browser apps served THROUGH the gateway at `/apps/<id>/`.

One port, one tunnel: the browser reaches every app on the gateway's own
origin, so a remote or headless gateway needs nothing more than the one
address (or reverse proxy, or tunnel) it already has. Each app still runs its
own small Node server on 127.0.0.1 (apps_manager.py); this module relays to
it.

Only an app that announces `X-AbstractFramework-App: <id>; mount=1` is
served (apps_manager.AppsManager.app_mount). That app uses the abstractuic
app-server kit's mount.js, which reads the headers below from a loopback
peer and decides everything about "who is asking" from them. An older app
would read every visitor as local (this proxy is its loopback peer), so it
is never exposed here.

Every request to `/apps/<id>/...`:
- is refused (403) when it carries an Origin other than the gateway's own
  address (another site, or another port on this host);
- is GATED on a valid gateway session for that app: the app's own
  `<cookie_prefix>_gateway_session` cookie (set by the sign-in handover,
  routes/apps.py, with `Path=/apps/<id>/`). Without one, a page navigation
  goes to the console (`/console#apps?open=<id>&path=...`), which signs the
  person in and opens the app through the usual one-time handover; anything
  else is 401. A WebSocket without one is refused before it is accepted,
  and one whose Origin is another site is refused too.
  The one exception: the PUBLIC ASSETS a browser fetches WITHOUT cookies
  (`public_asset_path`: the web app manifest, favicons, the home-screen
  icons; `<link rel=manifest>` is fetched credentials-omitted unless the
  page says `crossorigin="use-credentials"`). Those paths, GET/HEAD only,
  are relayed with no cookie at all and answered only when the app returns
  200/304 with a manifest or image Content-Type (anything else, e.g. the
  app's HTML shell for a missing file, is the usual 401); their Set-Cookie
  headers are dropped. Nothing under `api/`, no page, no script bundle.
- is relayed with the `/apps/<id>` prefix stripped, and
    X-Forwarded-Prefix: /apps/<id>
    X-Forwarded-For:    <the effective peer> (overwritten, never appended)
    X-Forwarded-Proto / X-Forwarded-Host: what the browser used (ALWAYS
    both: a Host this proxy cannot forward as X-Forwarded-Host is refused
    with 400 `invalid_host`, never relayed without it — the app would see
    only the gateway's loopback Host and treat a remote page as local);
- carries ONLY the app's own cookies (`<cookie_prefix>_*`): never the
  console's session cookie, never an Authorization header, never a gateway
  session header;
- and the app may set only its own cookies in return.
Streaming responses (SSE) are relayed as they arrive; WebSockets are
relayed frame by frame.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import weakref
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse
from starlette.websockets import WebSocketDisconnect

from .apps_manager import APP_BY_ID, AppSpec, get_apps_manager

logger = logging.getLogger(__name__)

router = APIRouter(tags=["apps"])

APPS_PREFIX = "/apps"
SESSION_CACHE_TTL_S = 5.0
CONNECT_TIMEOUT_S = 10.0

# Hop-by-hop headers (RFC 9110 7.6.1) never cross the proxy.
_HOP_BY_HOP = frozenset(
    {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection", "te", "trailer", "trailers", "transfer-encoding", "upgrade"}
)
# Request headers the app never receives from the browser: credentials of the
# gateway, and every forwarding header (the proxy writes its own).
_REQUEST_DROP = frozenset(
    {
        "host",
        "cookie",
        "authorization",
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-forwarded-prefix",
        "x-forwarded-port",
        "x-real-ip",
        "x-abstractframework-app-proxy",
        "x-abstractgateway-session",
        "x-abstractgateway-csrf",
        "content-length",
    }
)
_HOST_RE = re.compile(r"^(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(:\d{1,5})?$")

# Paths (after `/apps/<id>`, raw and percent-encoded as sent) a browser
# fetches WITHOUT cookies: the web app manifest, and the icons a page or a
# manifest names (favicon, home-screen / apple-touch icons). One segment,
# or `icons/<one segment>`; no `%`, so nothing encoded (no `%2F`, `%2e`).
_PUBLIC_ASSET_RE = re.compile(
    r"^/(?:"
    r"manifest\.webmanifest|manifest\.json|site\.webmanifest"
    r"|favicon(?:-[A-Za-z0-9_.-]+)?\.(?:ico|svg|png)"
    r"|icon(?:-[A-Za-z0-9_.-]+)?\.(?:svg|png|webp)"
    r"|apple-touch-icon(?:-[A-Za-z0-9_.-]+)?\.png"
    r"|icons/[A-Za-z0-9_.-]+\.(?:png|svg|ico|webp)"
    r")$"
)
# ... and answered only with one of these types (the app's HTML shell, which
# a single-page app returns for any unknown path, never passes).
_PUBLIC_ASSET_TYPES = frozenset(
    {
        "application/manifest+json",
        "application/json",
        "image/svg+xml",
        "image/png",
        "image/webp",
        "image/x-icon",
        "image/vnd.microsoft.icon",
    }
)


def public_asset_path(method: str, rest: Optional[str]) -> bool:
    """`rest` (the path after `/apps/<id>`, with its query) is a public asset
    a browser fetches without cookies: served without the session gate."""
    if method not in ("GET", "HEAD") or not rest:
        return False
    path = rest.split("?", 1)[0]
    return bool(_PUBLIC_ASSET_RE.match(path)) and ".." not in path


def _public_asset_answer(status: int, content_type: Optional[str]) -> bool:
    if status == 304:
        return True
    ctype = str(content_type or "").split(";", 1)[0].strip().lower()
    return status == 200 and ctype in _PUBLIC_ASSET_TYPES


def app_prefix(app_id: str) -> str:
    return f"{APPS_PREFIX}/{app_id}"


def app_cookie_names(spec: AppSpec) -> Tuple[str, str]:
    """(the app's cookie-name prefix, its gateway session cookie)."""
    return f"{spec.cookie_prefix}_", f"{spec.cookie_prefix}_gateway_session"


def parse_cookie_pairs(header: Optional[str]) -> List[Tuple[str, str]]:
    """Every `name=value` pair of a Cookie header, in the browser's order
    (the most specific Path first)."""
    out: List[Tuple[str, str]] = []
    for part in str(header or "").split(";"):
        name, sep, value = part.partition("=")
        name = name.strip()
        if sep and name:
            out.append((name, value.strip()))
    return out


def first_cookie(pairs: List[Tuple[str, str]], name: str) -> Optional[str]:
    """First value wins, like the kit's parseCookies: the app's
    `Path=/apps/<id>/` cookie beats a `Path=/` one of the same name."""
    for n, v in pairs:
        if n == name:
            return v
    return None


def forwarded_client(request: Any) -> Optional[str]:
    """The address the app is told the browser has: the effective peer
    (security/same_machine.effective_peer: X-Forwarded-For believed from a
    loopback peer only), else the connection's peer. None when unknown."""
    from .security.same_machine import effective_peer

    peer = effective_peer(request)
    if peer:
        return peer
    client = getattr(request, "client", None)
    host = str(getattr(client, "host", "") or "") if client is not None else ""
    return host or None


# -- the session gate -----------------------------------------------------------

_session_cache: Dict[str, float] = {}
_session_lock = threading.Lock()


def _session_valid_uncached(cookie_value: str) -> bool:
    from urllib.parse import unquote

    from .security.gateway_security import load_gateway_auth_policy_from_env
    from .security.sessions import GatewaySessionStore, gateway_session_id_from_value, legacy_token_fingerprints

    sid = gateway_session_id_from_value(unquote(cookie_value))
    if not sid:
        return False
    fps = legacy_token_fingerprints(tuple(load_gateway_auth_policy_from_env().tokens or ()))
    return GatewaySessionStore().authenticate_session(sid, legacy_token_fingerprints=fps) is not None


def session_valid(cookie_value: Optional[str]) -> bool:
    """A live gateway session. Positive answers are cached SESSION_CACHE_TTL_S
    (a page load is dozens of asset requests; the session store is a file).
    The app's own API calls are checked again by the gateway itself."""
    value = str(cookie_value or "").strip()
    if not value:
        return False
    now = time.monotonic()
    with _session_lock:
        exp = _session_cache.get(value)
        if exp is not None and exp > now:
            return True
    ok = _session_valid_uncached(value)
    with _session_lock:
        if ok:
            if len(_session_cache) > 4096:
                _session_cache.clear()
            _session_cache[value] = now + SESSION_CACHE_TTL_S
        else:
            _session_cache.pop(value, None)
    return ok


# -- one HTTP client per event loop -------------------------------------------------

_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Any]" = weakref.WeakKeyDictionary()


def _http_client() -> Any:
    import httpx

    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None:
        # trust_env=False: an HTTP(S)_PROXY in the gateway's environment must
        # never route a loopback relay through another machine.
        client = httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(connect=CONNECT_TIMEOUT_S, read=None, write=None, pool=CONNECT_TIMEOUT_S),
        )
        _clients[loop] = client
    return client


# -- helpers ----------------------------------------------------------------------------


def _page(status: int, title: str, message: str, *, link: Optional[Tuple[str, str]] = None) -> HTMLResponse:
    from html import escape

    href, text = link or ("/console#apps", "Open the console")
    body = (
        "<!doctype html><meta charset=utf-8><title>" + escape(title) + "</title>"
        "<body style=\"font:15px/1.5 -apple-system,system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem\">"
        f"<h1 style=\"font-size:1.3rem\">{escape(title)}</h1><p>{escape(message)}</p>"
        f"<p><a href=\"{escape(href)}\">{escape(text)}</a></p></body>"
    )
    return HTMLResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


def _wants_page(request: Request) -> bool:
    """A top-level page load (a person, not a script): Sec-Fetch-Mode says
    so; without it (older browsers), an HTML Accept on a GET/HEAD."""
    if request.method not in ("GET", "HEAD"):
        return False
    mode = str(request.headers.get("sec-fetch-mode") or "").strip().lower()
    if mode:
        return mode == "navigate"
    return "text/html" in str(request.headers.get("accept") or "").lower()


def _refusal(request: Request, status: int, reason: str, title: str, message: str) -> Response:
    if _wants_page(request):
        return _page(status, title, message)
    return JSONResponse(status_code=status, content={"ok": False, "reason": reason, "message": message}, headers={"Cache-Control": "no-store"})


def _upstream_path(scope: Dict[str, Any], app_id: str) -> Optional[str]:
    """The request path with `/apps/<id>` stripped, kept percent-encoded as
    the browser sent it, plus the query. None when it is not under the
    prefix."""
    raw = scope.get("raw_path")
    prefix = app_prefix(app_id)
    if isinstance(raw, (bytes, bytearray)):
        path = bytes(raw).decode("latin-1")
    else:
        path = quote(str(scope.get("path") or ""), safe="/%:@!$&'()*+,;=-._~")
    if not path.startswith(prefix + "/"):
        return None
    rest = path[len(prefix):]
    qs = scope.get("query_string") or b""
    if qs:
        rest += "?" + bytes(qs).decode("latin-1")
    return rest


def _forward_headers(request_headers: Any, *, spec: AppSpec, client: str, proto: str, host: str, cookies_allowed: bool = True) -> List[Tuple[str, str]]:
    prefix, _session = app_cookie_names(spec)
    out: List[Tuple[str, str]] = []
    for k, v in request_headers.items():
        lk = k.lower()
        if lk in _HOP_BY_HOP or lk in _REQUEST_DROP or lk.startswith("sec-websocket-"):
            continue
        out.append((k, v))
    cookies = [f"{n}={v}" for n, v in parse_cookie_pairs(request_headers.get("cookie")) if n.startswith(prefix)] if cookies_allowed else []
    if cookies:
        out.append(("cookie", "; ".join(cookies)))
    out.append(("x-forwarded-for", client))
    out.append(("x-forwarded-prefix", app_prefix(spec.id)))
    out.append(("x-forwarded-proto", proto))
    if not _forwardable_host(host):
        # Callers refuse such a request first (400 invalid_host); a request
        # relayed without X-Forwarded-Host must be impossible, not unlikely.
        raise ValueError(f"host {host!r} cannot be forwarded as X-Forwarded-Host")
    out.append(("x-forwarded-host", host))
    return out


def _forwardable_host(host: Optional[str]) -> bool:
    """The browser's Host is one this proxy forwards as X-Forwarded-Host.
    Browsers accept names this pattern does not (Chrome: `a_b.attacker.com`);
    such a request is refused, because the app decides "on this machine?"
    from the loopback peer AND a loopback Host / X-Forwarded-Host (the
    abstractuic kit's clientIsLoopback), and without X-Forwarded-Host it
    would see only the gateway's own loopback Host."""
    return bool(host) and bool(_HOST_RE.match(str(host)))


def _invalid_host_response() -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"ok": False, "reason": "invalid_host", "message": "The Host this request was sent to is not a host name or address the gateway can pass on to the app."},
        headers={"Cache-Control": "no-store"},
    )


def _response_headers(raw: List[Tuple[bytes, bytes]], spec: AppSpec, *, cookies_allowed: bool = True) -> List[Tuple[bytes, bytes]]:
    """The app's response headers, minus hop-by-hop ones and any cookie that
    is not the app's own (an app must never set the console's session); a
    public asset (no session) sets no cookie at all."""
    prefix = app_cookie_names(spec)[0].encode("latin-1")
    out: List[Tuple[bytes, bytes]] = []
    for k, v in raw:
        lk = k.lower()
        if lk.decode("latin-1") in _HOP_BY_HOP:
            continue
        if lk == b"set-cookie" and not cookies_allowed:
            continue
        if lk == b"set-cookie" and not v.lstrip().startswith(prefix):
            logger.warning("app %s tried to set a cookie outside its own names (%r): dropped", spec.id, v.split(b"=", 1)[0][:64])
            continue
        out.append((k, v))
    return out


def _same_origin(origin: Optional[str], host: Optional[str], scheme: str) -> bool:
    """A browser Origin names the host the request was sent to."""
    if not origin:
        return True  # not a browser (or a same-origin request that omits it)
    try:
        o = urlsplit(origin)
    except ValueError:
        return False
    if not o.scheme or not o.netloc:
        return False

    def norm(netloc: str, sch: str) -> str:
        n = netloc.lower()
        default = ":443" if sch in ("https", "wss") else ":80"
        return n[: -len(default)] if n.endswith(default) else n

    return norm(o.netloc, o.scheme) == norm(str(host or ""), scheme)


async def _target(app_id: str) -> Tuple[Optional[AppSpec], Optional[int], Optional[str]]:
    """(spec, port, refusal reason) for `/apps/<app_id>/`."""
    spec = APP_BY_ID.get(str(app_id or "").lower())
    if spec is None:
        return None, None, "unknown_app"
    m = get_apps_manager()
    port = await run_in_threadpool(m.serving_port, spec)
    if not port:
        return spec, None, "not_running"
    if not await run_in_threadpool(m.app_mount, spec, port):
        return spec, port, "not_mountable"
    return spec, port, None


def _explain(request: Request, spec: Optional[AppSpec], reason: str, app_id: str) -> Response:
    if reason == "unknown_app":
        return _refusal(request, 404, reason, "No such app", f"There is no app '{app_id}' on this gateway.")
    assert spec is not None
    if reason == "not_running":
        return _refusal(request, 409, reason, f"{spec.name} is not running", f"Start {spec.name} from the console's Apps page, then open it again.")
    return _refusal(
        request,
        409,
        reason,
        f"This {spec.name} cannot be opened through the gateway",
        f"The running {spec.name} is a version that cannot be served at /apps/{spec.id}/. Update it from the console's Apps page.",
    )


# -- HTTP ---------------------------------------------------------------------------------


@router.api_route(APPS_PREFIX + "/{app_id}", methods=["GET", "HEAD"], include_in_schema=False)
async def app_root_redirect(request: Request, app_id: str) -> Response:
    if str(app_id or "").lower() not in APP_BY_ID:
        return _refusal(request, 404, "unknown_app", "No such app", f"There is no app '{app_id}' on this gateway.")
    qs = request.url.query
    return RedirectResponse(url=app_prefix(app_id.lower()) + "/" + (f"?{qs}" if qs else ""), status_code=308)


@router.api_route(
    APPS_PREFIX + "/{app_id}/{path:path}",
    methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    include_in_schema=False,
)
async def app_http(request: Request, app_id: str, path: str) -> Response:
    spec, port, reason = await _target(app_id)
    if reason:
        return _explain(request, spec, reason, app_id)
    assert spec is not None and port is not None
    rest = _upstream_path(request.scope, spec.id)
    if rest is None:
        return _refusal(request, 404, "unknown_app", "No such app", f"There is no app '{app_id}' on this gateway.")
    if not _forwardable_host(request.headers.get("host")):
        return _invalid_host_response()
    # Another origin's page (another site, or another port on this host,
    # which is "same-site" and so still carries the app's Lax cookies) never
    # reaches the app: browsers send Origin on every cross-origin request.
    if not _same_origin(request.headers.get("origin"), request.headers.get("host"), request.url.scheme):
        return JSONResponse(status_code=403, content={"ok": False, "reason": "cross_origin", "message": "Requests to an app must come from the gateway's own pages."}, headers={"Cache-Control": "no-store"})
    _prefix, session_cookie = app_cookie_names(spec)
    pairs = parse_cookie_pairs(request.headers.get("cookie"))
    signed_in = await run_in_threadpool(session_valid, first_cookie(pairs, session_cookie))
    public = not signed_in and public_asset_path(request.method, rest)
    if not signed_in and not public:
        if _wants_page(request):
            # The console signs the person in, then opens the app through
            # the one-time handover, landing on the page asked for.
            return RedirectResponse(url=f"/console#apps?open={quote(spec.id)}&path={quote(rest, safe='')}", status_code=303, headers={"Cache-Control": "no-store"})
        return JSONResponse(
            status_code=401,
            content={"ok": False, "reason": "app_sign_in_required", "message": f"Open {spec.name} from the gateway console to sign in."},
            headers={"Cache-Control": "no-store"},
        )
    client = forwarded_client(request)
    if not client:
        return JSONResponse(status_code=400, content={"ok": False, "reason": "unknown_client", "message": "Cannot determine the client address of this connection."})
    headers = _forward_headers(request.headers, spec=spec, client=client, proto=request.url.scheme, host=str(request.headers.get("host") or ""), cookies_allowed=not public)
    has_body = not public and (request.method not in ("GET", "HEAD", "OPTIONS") or bool(request.headers.get("content-length") or request.headers.get("transfer-encoding")))
    http = _http_client()
    upstream = http.build_request(
        request.method,
        f"http://127.0.0.1:{port}{rest}",
        headers=headers,
        content=request.stream() if has_body else None,
    )
    try:
        resp = await http.send(upstream, stream=True)
    except Exception as exc:  # noqa: BLE001 - connection refused, reset, timeout
        logger.info("app %s on port %s did not answer: %s", spec.id, port, exc)
        get_apps_manager()._mount_cache.pop((spec.id, int(port)), None)
        return _refusal(request, 502, "app_not_answering", f"{spec.name} is not answering", f"{spec.name} did not answer ({type(exc).__name__}). Try again in a moment, or restart it from the console.")

    if public and not _public_asset_answer(resp.status_code, resp.headers.get("content-type")):
        # Not a manifest or an icon after all (a missing file answered with
        # the app's shell, a redirect, an error): what a request without a
        # session gets everywhere else.
        await resp.aclose()
        return JSONResponse(
            status_code=401,
            content={"ok": False, "reason": "app_sign_in_required", "message": f"Open {spec.name} from the gateway console to sign in."},
            headers={"Cache-Control": "no-store"},
        )

    async def body():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()

    out = StreamingResponse(body(), status_code=resp.status_code, background=BackgroundTask(resp.aclose))
    out.raw_headers = _response_headers(list(resp.headers.raw), spec, cookies_allowed=not public)
    return out


# -- WebSocket ------------------------------------------------------------------------------


@router.websocket(APPS_PREFIX + "/{app_id}/{path:path}")
async def app_websocket(websocket: WebSocket, app_id: str, path: str) -> None:
    import websockets
    from websockets.asyncio.client import connect

    spec, port, reason = await _target(app_id)
    if reason or spec is None or port is None:
        await websocket.close(code=1008)
        return
    headers = websocket.headers
    scheme = "https" if websocket.url.scheme in ("wss", "https") else "http"
    host = str(headers.get("host") or "")
    if not _forwardable_host(host) or not _same_origin(headers.get("origin"), host, scheme):
        await websocket.close(code=1008)
        return
    _prefix, session_cookie = app_cookie_names(spec)
    if not await run_in_threadpool(session_valid, first_cookie(parse_cookie_pairs(headers.get("cookie")), session_cookie)):
        await websocket.close(code=1008)
        return
    rest = _upstream_path(websocket.scope, spec.id)
    client = forwarded_client(websocket)
    if rest is None or not client:
        await websocket.close(code=1008)
        return
    fwd = [(k, v) for k, v in _forward_headers(headers, spec=spec, client=client, proto=scheme, host=host) if k.lower() not in ("origin", "user-agent")]
    subprotocols = [p.strip() for p in str(headers.get("sec-websocket-protocol") or "").split(",") if p.strip()]
    try:
        upstream = await connect(
            f"ws://127.0.0.1:{port}{rest}",
            additional_headers=fwd,
            origin=headers.get("origin") or None,
            subprotocols=subprotocols or None,
            open_timeout=CONNECT_TIMEOUT_S,
            max_size=None,
            compression=None,
            user_agent_header=headers.get("user-agent") or None,
        )
    except Exception as exc:  # noqa: BLE001 - refused, rejected handshake, timeout
        logger.info("app %s refused the WebSocket %s: %s", spec.id, rest, exc)
        await websocket.close(code=1011)
        return
    await websocket.accept(subprotocol=upstream.subprotocol)

    async def browser_to_app() -> None:
        while True:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                await upstream.close(code=int(msg.get("code") or 1000))
                return
            if msg.get("text") is not None:
                await upstream.send(msg["text"])
            elif msg.get("bytes") is not None:
                await upstream.send(msg["bytes"])

    async def app_to_browser() -> None:
        try:
            async for data in upstream:
                if isinstance(data, str):
                    await websocket.send_text(data)
                else:
                    await websocket.send_bytes(data)
        except websockets.ConnectionClosed:
            pass
        code = upstream.close_code or 1000
        try:
            await websocket.close(code=code if code not in (1005, 1006) else 1000)
        except RuntimeError:
            pass  # already closed by the browser

    tasks = [asyncio.create_task(browser_to_app()), asyncio.create_task(app_to_browser())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await upstream.close()
