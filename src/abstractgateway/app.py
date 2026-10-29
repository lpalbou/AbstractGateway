"""AbstractGateway FastAPI application."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, RedirectResponse

from .console import gateway_console_html

from .routes import automations_router, email_router, engines_router, entities_router, entity_replay_router, gateway_router, triage_router
from .security import GatewaySecurityMiddleware, load_gateway_auth_policy_from_env


def _warm_route_table(app_: FastAPI) -> None:
    """Build FastAPI's per-route state before the first request does it ON the event loop.

    FastAPI (0.142+) builds each included router's route state (dependants, pydantic
    adapters, compiled paths) lazily, on the first request that matches through it: ~140 ms
    for this app's routes, inside the event loop, on the first request after boot (health
    included). Matching a path no route answers walks every route once and builds them all;
    the lifespan runs this in a worker before serving (tests/test_perf_routes_off_loop.py)."""
    for route in app_.router.routes:
        route.matches({"type": "http", "path": "/__route-table-warm-up__", "method": "GET",
                       "root_path": "", "headers": [], "query_string": b""})


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Boot the service on a BACKGROUND thread and yield immediately
    # (resilience wave 2026-07-21, supervisor seam c4063): uvicorn accepts no
    # connections until lifespan yields, so a synchronous heavy boot
    # (entity-home load) left /api/health connection-refused — the
    # supervisor counted misses against a healthy-but-loading gateway and a
    # >60s boot meant a false recycle. Health answers status="starting"
    # while boot runs; API routes gate on boot completion off the loop.
    from .service import begin_gateway_boot, reset_gateway_boot_state, stop_gateway_runner
    from . import loop_watchdog

    # Event-loop watchdog (2026-10-04): a loop blocked past
    # `serve --watchdog-seconds` dumps stacks and exits non-zero so the
    # service manager restarts the gateway (loop_watchdog.py). Off unless
    # `serve` configured it.
    loop_watchdog.start_configured()
    # The previous process's newest hang (R13.1): read once, shown by the
    # console's Resources page and the terminal console.
    loop_watchdog.load_last_incident()
    # Worker pools by kind of work (gateway load 2026-10-26): light / heavy / watchers,
    # sized by `serve --pool-light/--pool-heavy/--pool-watchers` (work_pools.py).
    try:
        import logging

        from .work_pools import pool_sizes

        logging.getLogger("abstractgateway").info("worker pools: %s", pool_sizes())
    except Exception:
        pass
    begin_gateway_boot()
    # The route table, built off the loop before the first request (see _warm_route_table).
    try:
        from .work_pools import off_loop

        await off_loop("light", _warm_route_table, _app)
    except Exception:
        pass
    # Browser apps marked enabled start with the gateway, as its children
    # (never launchd/systemd), on a background thread; they stop with it.
    try:
        from .apps_manager import start_apps_on_boot

        start_apps_on_boot()
    except Exception:
        pass
    # The addresses this machine answers on (LAN, Bonjour, Tailscale): their
    # origins are accepted without a manual entry. Discovered off the loop.
    try:
        from .network_exposure import detected_hosts

        detected_hosts()
    except Exception:
        pass
    try:
        yield
    finally:
        loop_watchdog.stop_active()
        try:
            from .apps_manager import stop_apps_on_shutdown

            stop_apps_on_shutdown()
        except Exception:
            pass
        stop_gateway_runner()
        reset_gateway_boot_state()
        # Desktop tray helper (2026-09-05): lifespan shutdown is the one
        # place that runs on EVERY exit path — uvicorn re-raises a captured
        # SIGTERM after serve() returns, so the CLI's own `finally` never
        # sees `systemctl stop`/`kill`. Best-effort, no-op when no helper.
        try:
            from .tray_supervisor import get_tray_supervisor

            get_tray_supervisor().stop()
        except Exception:
            pass
        # Serve record: same reason as the tray —
        # the CLI's `finally` never runs after a re-raised SIGTERM. Removes
        # <data>/run/gateway-serve.json only when it names THIS pid.
        try:
            from .first_run import clear_serve_record
            from .users import gateway_data_dir_from_env

            clear_serve_record(gateway_data_dir_from_env())
        except Exception:
            pass
        # The serve launch flags (`--backlog-root`, `--exec-runner`) of THIS
        # process, same reason.
        try:
            import os as _os

            from .runtime_config import clear_launch_settings
            from .users import gateway_data_dir_from_env

            clear_launch_settings(gateway_data_dir_from_env(), pid=_os.getpid())
        except Exception:
            pass


app = FastAPI(
    title="AbstractGateway",
    description="Durable Run Gateway for AbstractRuntime (commands + ledger replay/stream).",
    version="0.18.0",
    lifespan=_lifespan,
    docs_url=None,
    redoc_url=None,
)

# Per-principal service pre-warm: added BEFORE the security middleware so it
# runs INSIDE it (principal contextvar set) — a multi-user first-touch boots
# the heavy service on a worker thread instead of stalling the event loop
# (resilience wave 2026-07-21).
from .service import ServicePrewarmMiddleware  # noqa: E402

app.add_middleware(ServicePrewarmMiddleware)


# A route that reaches a service still being built, on the event loop, never builds it there
# (service.py, backlog 1054 round 3): the build runs in its own thread and the request answers
# 503 + Retry-After, so the loop (and /api/health) keeps answering.
from .service import ServiceStarting  # noqa: E402


@app.exception_handler(ServiceStarting)
async def _service_starting_handler(_request, exc: ServiceStarting):  # noqa: ANN001 - Starlette handler signature
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=503,
        content={"detail": {"reason_code": "service_starting", "message": "The gateway is still starting this account's service. Retry in a moment."}},
        headers={"Retry-After": "1"},
    )

# Gateway security (backlog 309).
app.add_middleware(GatewaySecurityMiddleware, policy=load_gateway_auth_policy_from_env())

# Automations error envelope (contract C9): OUTSIDE the security layer, so the
# auth layer's early 401/403 on /api/gateway/automations* and
# /api/gateway/trigger-sources carry {"detail":{"reason_code","message"}} like
# every other error there. Scoped by path prefix; no other route changes.
from .automation_errors import AutomationErrorEnvelopeMiddleware  # noqa: E402

app.add_middleware(AutomationErrorEnvelopeMiddleware)

# CORS for browser clients. In production, prefer configuring exact origins and terminating TLS at a reverse proxy.
#
# IMPORTANT: add after GatewaySecurityMiddleware so CORS headers are present even on early security rejections
# (otherwise browsers surface a generic "NetworkError").
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# Wire gzip for large control-plane payloads (replay-integrity incident,
# code-tui c5547/c5552 R1; operator: whole turns lost when a single-turn
# history_bundle exceeded a client's 10 MiB reader). Measured 3.7-4.8x on the
# live incident bundles (14.29 -> 2.97 MB), which de-risks every thin client's
# fetch. SSE LIVE TAILS ARE SAFE: starlette 0.52.1's GZipMiddleware carries
# DEFAULT_EXCLUDED_CONTENT_TYPES = ("text/event-stream",), so the run-ledger
# stream and the entity /replay/stream (both text/event-stream) are never
# compressed — verified in the installed source before wiring. Compression is
# CLIENT-OPT-IN (only when the request carries Accept-Encoding: gzip) and only
# above minimum_size, so tiny responses and non-gzip clients are untouched.
# NDJSON bounded reads (application/x-ndjson) and JSON bundles DO compress —
# exactly the incident target. Content-Encoding: gzip is transparent to any
# standard HTTP client. Added after CORS so CORS headers are set before the
# body is compressed.
from starlette.middleware.gzip import GZipMiddleware  # noqa: E402

app.add_middleware(GZipMiddleware, minimum_size=1024)

# Outermost (R13.1): the requests the event loop is serving, named in the
# watchdog's incident file if the loop ever hangs (loop_watchdog.py).
from .loop_watchdog import InflightRequests  # noqa: E402

app.add_middleware(InflightRequests)

# Entities routers first: their literal paths (/gateway/entities/...) must win
# over any parametrized /gateway/... routes in the main gateway router.
from .routes.session_tools import router as session_tools_router  # noqa: E402
from .routes.message_stats import router as message_stats_router  # noqa: E402

app.include_router(session_tools_router, prefix="/api")
app.include_router(message_stats_router, prefix="/api")
app.include_router(entities_router, prefix="/api")
app.include_router(entity_replay_router, prefix="/api")
# Local engines (status, user-level installs, needs_admin/needs_tools jobs,
# start/stop): routes/engines.py, the only home of /gateway/engines/... routes.
app.include_router(engines_router, prefix="/api")
# "Delete download" on the Models page: routes/model_download_delete.py.
from .routes.model_download_delete import router as model_download_delete_router  # noqa: E402

app.include_router(model_download_delete_router, prefix="/api")
# Archive / unarchive a session (conversation): routes/session_archive.py.
from .routes.session_archive import router as session_archive_router  # noqa: E402

app.include_router(session_archive_router, prefix="/api")
# A conversation's standing tool decision (approve / deny without asking), read by the runtime
# before any approval wait: routes/session_tool_policy.py, /gateway/sessions/{id}/tool-policy.
from .routes.session_tool_policy import router as session_tool_policy_router  # noqa: E402

app.include_router(session_tool_policy_router, prefix="/api")
# Per-account client preferences (round 14: default workflow per app, admin default +
# account override): routes/account_preferences.py, /gateway/accounts/{account}/preferences.
from .routes.account_preferences import router as account_preferences_router  # noqa: E402

app.include_router(account_preferences_router, prefix="/api")
# Browser apps (Node runtime, npm installs, supervised app servers): literal
# /gateway/apps/... paths, included before the parametrized gateway router.
# The handover route lives at /apps/handover/{code}, outside /api/gateway, so
# a plain browser navigation reaches it (its one-time code is the credential).
from .routes.apps import handover_router as apps_handover_router  # noqa: E402
from .routes.apps import router as apps_router  # noqa: E402

app.include_router(apps_router, prefix="/api")
app.include_router(apps_handover_router)
# The apps themselves, served through the gateway at /apps/<id>/ (HTTP, SSE,
# WebSocket; gated on the app's gateway session): app_proxy.py. AFTER the
# handover routes, whose literal /apps/handover/... paths must win.
from .app_proxy import router as app_proxy_router  # noqa: E402

app.include_router(app_proxy_router)
# Network exposure (localhost / lan / internet) + reachable addresses:
# routes/network.py, literal /gateway/network... paths (gateway_network_v1).
from .routes.network import router as network_router  # noqa: E402

app.include_router(network_router, prefix="/api")
from .core_endpoint import CoreEndpoint, LegacyCoreRedirect  # noqa: E402
from .routes.core_endpoint import router as core_endpoint_router  # noqa: E402
from .routes.core_endpoint import user_router as openai_api_router  # noqa: E402
from .routes.core_endpoint import keys_router as openai_keys_router  # noqa: E402

app.include_router(core_endpoint_router, prefix="/api")
app.include_router(openai_api_router, prefix="/api")
app.include_router(openai_keys_router, prefix="/api")
# The OpenAI-compatible API (standard layout: /v1/models, /v1/chat/completions, ...);
# /core/v1 answers 308 to it for one release (deprecated).
app.mount("/v1", CoreEndpoint(), name="openai-api")
app.mount("/core", LegacyCoreRedirect(), name="core-endpoint-legacy")
# Start at login (routes/start_at_login.py): literal /gateway/host/start-at-login
# (gateway_start_at_login_v1), admin-only; the tray's and CLI's same switch.
from .routes.start_at_login import router as start_at_login_router  # noqa: E402

app.include_router(start_at_login_router, prefix="/api")
# Automations v1 façade (routes/automations.py): literal /gateway/automations...
# and /gateway/trigger-sources paths, before the parametrized gateway router.
app.include_router(automations_router, prefix="/api")
# Triggers (routes/triggers.py, framework backlog 1041/1042): /gateway/triggers/... literal
# paths (connections, the public signed webhook ingress), before the parametrized gateway router.
from .routes.triggers import router as triggers_router  # noqa: E402

app.include_router(triggers_router, prefix="/api")
# Per-user email (framework backlog 0992): /gateway/me/email*, /me/notifications,
# /admin/users/{id}/email, the sign-in recovery routes and the legacy /email/* aliases.
app.include_router(email_router, prefix="/api")
# First-run onboarding and password sign-in (0.18.0): routes/session_passwords.py.
from .routes.session_passwords import router as session_passwords_router  # noqa: E402

app.include_router(session_passwords_router, prefix="/api")
# The caller's own event stream (framework backlog 1046): /gateway/me/events (catch-up),
# /gateway/me/events/stream (SSE), /gateway/me/events/notifications; literal paths.
from .routes.me_events import router as me_events_router  # noqa: E402

app.include_router(me_events_router, prefix="/api")
# The replay view of a run (framework backlog 0993, AbstractRuntime ADR-0009):
# /gateway/runs/{run_id}/view and /gateway/runs/{run_id}/view/stream.
from .routes.replay_view import router as replay_view_router  # noqa: E402

app.include_router(replay_view_router, prefix="/api")
# Apple push devices (framework backlog 1049): /gateway/me/push/devices; literal paths.
from .routes.push import router as push_router  # noqa: E402
from .routes.push import settings_router as push_settings_router  # noqa: E402

app.include_router(push_router, prefix="/api")
app.include_router(push_settings_router, prefix="/api")
# AbstractNote's read API (optional, abstractnote_seam.py): /api/gateway/note/..., literal
# paths, before the parametrized gateway router. Absent when abstractnote is not installed.
from .abstractnote_seam import note_router  # noqa: E402

_note_router = note_router()
if _note_router is not None:
    app.include_router(_note_router, prefix="/api/gateway")
# The voice library (languages, cloned voices, cloning, voice design, the capability catalog);
# literal /gateway/voice/... paths, mounted before the main router.
from .routes.voice_library import router as voice_library_router  # noqa: E402

app.include_router(voice_library_router, prefix="/api")
# The memory graph (/gateway/memory/*: query, history, sources, reconstruct, derived) over the
# AbstractRuntime memory facade; literal paths, mounted before the main router.
from .routes.memory import router as memory_router  # noqa: E402

app.include_router(memory_router, prefix="/api")
app.include_router(gateway_router, prefix="/api")
app.include_router(triage_router, prefix="/api")


# Server-side timing evidence (operator incident 2026-07-13: a client sat
# 10-15s on "Connecting…" against a gateway answering /api/health in <1ms —
# the investigation needs to split server time from client/stack time without
# guessing). Every response carries X-Gateway-Duration-Ms = time spent INSIDE
# the gateway app (through the security/CORS stack down to the handler), so
# browser devtools show server-vs-network directly. Requests slower than
# ABSTRACTGATEWAY_SLOW_REQUEST_MS (default 1000) log a warning; set
# ABSTRACTGATEWAY_REQUEST_TIMING=1 to log EVERY request's duration at INFO
# for waterfall correlation. Streams (SSE) get the header at response start —
# it measures time-to-first-byte, not stream lifetime, which is the number
# the connect investigation needs.
@app.middleware("http")
async def _note_mutations(request, call_next):  # noqa: ANN001 - Starlette middleware signature
    """Every mutating request bumps the runs-listing write generation (routes.gateway):
    a listing asked after a write never shares an in-flight computation that began before it."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        from .routes.gateway import note_http_write

        note_http_write()
    return await call_next(request)


@app.middleware("http")
async def _request_timing(request, call_next):  # noqa: ANN001 - Starlette middleware signature
    import logging
    import os
    import time

    t0 = time.perf_counter()
    response = await call_next(request)
    dt_ms = (time.perf_counter() - t0) * 1000.0
    try:
        response.headers["X-Gateway-Duration-Ms"] = f"{dt_ms:.1f}"
    except Exception:
        pass  # a response type without mutable headers must not break serving
    try:
        slow_ms = float(os.getenv("ABSTRACTGATEWAY_SLOW_REQUEST_MS") or 1000.0)
    except ValueError:
        slow_ms = 1000.0
    logger = logging.getLogger("abstractgateway.timing")
    if dt_ms >= slow_ms:
        logger.warning("SLOW %s %s -> %s in %.1fms", request.method, request.url.path, response.status_code, dt_ms)
    elif (os.getenv("ABSTRACTGATEWAY_REQUEST_TIMING") or "").strip().lower() in {"1", "true", "on", "yes"}:
        logger.info("%s %s -> %s in %.1fms", request.method, request.url.path, response.status_code, dt_ms)
    return response


# Unhandled-exception responses must still carry CORS headers. Starlette's
# ServerErrorMiddleware sits OUTSIDE user middleware, so a raw 500 from an
# unhandled exception bypasses CORSMiddleware — browsers then mask the real
# status as a generic "Failed to fetch" (live incident: identity-record
# verbatim clicks 500'd unhandled and the maintainer saw a network error
# instead of the status). Handling the exception HERE keeps the response
# inside the middleware stack, so CORS headers ride it.
# Maintenance window hold (Castor doctoring): the registry raises on EVERY
# door path while a home is held — one SPECIFIC handler maps it to an honest
# 409 everywhere (visits, chat, summon, cognition, card). Deliberately not
# folded into the generic Exception handler: Starlette's ServerErrorMiddleware
# ALWAYS re-raises after a generic handler (so servers/tests can see it),
# while a specific handler returns normally from ExceptionMiddleware.
from .entities import MaintenanceHoldActive as _MaintenanceHoldActive  # noqa: E402


@app.exception_handler(_MaintenanceHoldActive)
async def _maintenance_hold_409(request, exc):  # noqa: ANN001 - FastAPI handler signature
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(Exception)
async def _unhandled_exception_cors_safe(request, exc):  # noqa: ANN001 - FastAPI handler signature
    import logging

    from fastapi.responses import JSONResponse

    logging.getLogger("abstractgateway").exception(
        "Unhandled exception on %s %s", getattr(request, "method", "?"), getattr(getattr(request, "url", None), "path", "?")
    )
    return JSONResponse(status_code=500, content={"detail": f"Internal error: {type(exc).__name__}: {exc}"})

# OpenAPI docs: advertise bearer auth for Swagger UI.
def _apply_gateway_bearer_auth_docs(openapi_schema: dict) -> dict:
    """Attach bearer auth metadata for /api/gateway endpoints."""
    components = openapi_schema.setdefault("components", {})
    schemes = components.setdefault("securitySchemes", {})
    scheme_name = "gatewayBearerAuth"
    if scheme_name not in schemes:
        schemes[scheme_name] = {"type": "http", "scheme": "bearer"}

    paths = openapi_schema.get("paths") or {}
    for path, ops in paths.items():
        if not str(path).startswith("/api/gateway"):
            continue
        if not isinstance(ops, dict):
            continue
        for method, op_schema in ops.items():
            if str(method).lower() not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                continue
            if not isinstance(op_schema, dict):
                continue
            security = op_schema.get("security")
            if not security:
                op_schema["security"] = [{scheme_name: []}]
            elif isinstance(security, list) and not any(
                isinstance(item, dict) and scheme_name in item for item in security
            ):
                security.append({scheme_name: []})

    return openapi_schema


def _custom_openapi() -> dict:
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    app.openapi_schema = _apply_gateway_bearer_auth_docs(schema)
    return app.openapi_schema


app.openapi = _custom_openapi


def _offline_openapi_docs_html(*, title: str, openapi_url: str = "/openapi.json") -> str:
    """Return a dependency-free OpenAPI viewer for offline installs."""
    safe_title = str(title or "API docs")
    safe_openapi_url = str(openapi_url or "/openapi.json")
    return (
        """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__TITLE__</title>
  <style>
    :root { color-scheme: dark; --bg:#0b1020; --panel:#121a30; --text:#e8edff; --muted:#9ca8cc; --line:#2b3860; --accent:#35d0ff; --green:#39d98a; --blue:#6ea8fe; --orange:#ffb454; --red:#ff6b6b; }
    * { box-sizing: border-box; }
    body { margin:0; min-height:100vh; font:15px/1.55 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--text); background:radial-gradient(circle at 20% 10%,rgba(53,208,255,.14),transparent 30rem),radial-gradient(circle at 80% 0%,rgba(255,107,107,.12),transparent 28rem),var(--bg); }
    main { max-width:1180px; margin:0 auto; padding:40px 24px 64px; }
    header { display:flex; align-items:flex-start; justify-content:space-between; gap:24px; margin-bottom:26px; }
    h1 { margin:0 0 8px; font-size:clamp(2rem,4vw,3.4rem); letter-spacing:-.04em; }
    p { margin:0; color:var(--muted); }
    a { color:var(--accent); text-decoration:none; }
    a:hover { text-decoration:underline; }
    .actions { display:flex; gap:10px; flex-wrap:wrap; justify-content:flex-end; }
    .button { display:inline-flex; align-items:center; border:1px solid var(--line); border-radius:999px; padding:10px 14px; background:rgba(255,255,255,.05); color:var(--text); font-weight:700; }
    .notice { border:1px solid rgba(53,208,255,.35); border-radius:18px; padding:16px 18px; background:rgba(53,208,255,.08); margin-bottom:24px; }
    .toolbar { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:12px; margin-bottom:18px; }
    input { width:100%; border:1px solid var(--line); border-radius:14px; padding:13px 14px; background:rgba(0,0,0,.18); color:var(--text); font:inherit; }
    .status { color:var(--muted); align-self:center; white-space:nowrap; }
    .endpoint { border:1px solid var(--line); border-radius:18px; background:rgba(18,26,48,.88); margin:12px 0; overflow:hidden; }
    .endpoint summary { cursor:pointer; padding:15px 18px; display:grid; grid-template-columns:82px minmax(0,1fr); gap:14px; align-items:center; }
    .method { display:inline-flex; justify-content:center; border-radius:999px; padding:5px 10px; font:800 12px/1 ui-monospace,SFMono-Regular,Menlo,monospace; letter-spacing:.08em; color:#06101f; background:var(--blue); }
    .method.POST { background:var(--green); } .method.PUT,.method.PATCH { background:var(--orange); } .method.DELETE { background:var(--red); }
    .path { font:700 15px/1.35 ui-monospace,SFMono-Regular,Menlo,monospace; overflow-wrap:anywhere; }
    .details { padding:0 18px 18px 114px; color:var(--muted); }
    .details code { display:inline-block; margin:4px 6px 4px 0; padding:4px 8px; border-radius:999px; background:rgba(255,255,255,.07); color:var(--text); }
    .error { border:1px solid rgba(255,107,107,.55); background:rgba(255,107,107,.1); border-radius:18px; padding:18px; color:#ffd0d0; }
    @media (max-width:720px) { header,.toolbar { display:block; } .actions { justify-content:flex-start; margin-top:18px; } .status { margin-top:10px; display:block; } .endpoint summary { grid-template-columns:1fr; } .details { padding-left:18px; } }
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>__TITLE__</h1>
        <p id="description">Offline OpenAPI documentation. No CDN, no internet required.</p>
      </div>
      <nav class="actions">
        <a class="button" href="__OPENAPI_URL__">Open JSON</a>
        <a class="button" href="/api/health">Health</a>
      </nav>
    </header>
    <section class="notice">This lightweight documentation view lists endpoints from <a href="__OPENAPI_URL__">__OPENAPI_URL__</a>. Authenticated Gateway routes still require a Bearer token when called by clients.</section>
    <section class="toolbar"><input id="filter" autocomplete="off" placeholder="Filter endpoints, methods, summaries..."><span class="status" id="status">Loading OpenAPI...</span></section>
    <section id="routes"></section>
  </main>
  <script>
    const openapiUrl = "__OPENAPI_URL__";
    const methods = new Set(["get","post","put","patch","delete","head","options"]);
    const state = { entries: [] };
    const text = (value) => value == null ? "" : String(value);
    function render() {
      const q = document.getElementById("filter").value.trim().toLowerCase();
      const routes = document.getElementById("routes");
      routes.textContent = "";
      const filtered = state.entries.filter((entry) => !q || [entry.method, entry.path, entry.summary, entry.description, entry.tags.join(" ")].join(" ").toLowerCase().includes(q));
      document.getElementById("status").textContent = `${filtered.length} / ${state.entries.length} endpoints`;
      for (const entry of filtered) {
        const details = document.createElement("details");
        details.className = "endpoint";
        const summary = document.createElement("summary");
        const method = document.createElement("span");
        method.className = `method ${entry.method}`;
        method.textContent = entry.method;
        const path = document.createElement("span");
        path.className = "path";
        path.textContent = entry.path;
        summary.append(method, path);
        const body = document.createElement("div");
        body.className = "details";
        const line = document.createElement("p");
        line.textContent = entry.summary || entry.description || "No summary.";
        body.append(line);
        if (entry.tags.length) {
          const tags = document.createElement("p");
          tags.append("Tags: ");
          for (const tag of entry.tags) {
            const code = document.createElement("code");
            code.textContent = tag;
            tags.append(code);
          }
          body.append(tags);
        }
        if (entry.parameters.length) {
          const params = document.createElement("p");
          params.append("Parameters: ");
          for (const param of entry.parameters) {
            const code = document.createElement("code");
            code.textContent = `${param.name || "?"}:${param.in || "?"}`;
            params.append(code);
          }
          body.append(params);
        }
        if (entry.requestBody) {
          const req = document.createElement("p");
          req.textContent = "Request body: yes";
          body.append(req);
        }
        details.append(summary, body);
        routes.append(details);
      }
    }
    async function load() {
      try {
        const response = await fetch(openapiUrl, { headers: { "Accept": "application/json" } });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const schema = await response.json();
        document.title = `${schema.info?.title || "__TITLE__"} - API docs`;
        document.getElementById("description").textContent = schema.info?.description || "Offline OpenAPI documentation.";
        const entries = [];
        for (const [path, operations] of Object.entries(schema.paths || {})) {
          for (const [method, op] of Object.entries(operations || {})) {
            if (!methods.has(method)) continue;
            entries.push({ method: method.toUpperCase(), path, summary: text(op.summary), description: text(op.description), tags: Array.isArray(op.tags) ? op.tags.map(text) : [], parameters: Array.isArray(op.parameters) ? op.parameters : [], requestBody: Boolean(op.requestBody) });
          }
        }
        entries.sort((a, b) => a.path.localeCompare(b.path) || a.method.localeCompare(b.method));
        state.entries = entries;
        render();
      } catch (error) {
        const routes = document.getElementById("routes");
        routes.textContent = "";
        const box = document.createElement("div");
        box.className = "error";
        box.textContent = `Could not load ${openapiUrl}: ${error}`;
        routes.append(box);
        document.getElementById("status").textContent = "OpenAPI load failed";
      }
    }
    document.getElementById("filter").addEventListener("input", render);
    load();
  </script>
</body>
</html>"""
        .replace("__TITLE__", safe_title)
        .replace("__OPENAPI_URL__", safe_openapi_url)
    )


@app.get("/docs", include_in_schema=False)
async def offline_docs() -> HTMLResponse:
    return HTMLResponse(_offline_openapi_docs_html(title=f"{app.title} API Docs"))


@app.get("/redoc", include_in_schema=False)
async def offline_redoc() -> HTMLResponse:
    return HTMLResponse(_offline_openapi_docs_html(title=f"{app.title} API Docs"))


@app.get("/", include_in_schema=False)
async def gateway_root() -> RedirectResponse:
    return RedirectResponse(url="/console", status_code=307)


@app.get("/console", include_in_schema=False)
async def gateway_console() -> HTMLResponse:
    return HTMLResponse(gateway_console_html())


@app.get("/api/health")
async def health_check():
    """Liveness + runner visibility.

    Always HTTP 200 (liveness contract for Docker/launcher probes), but
    `status` degrades to "degraded" when a runner is enabled yet locked out of
    the singleton tick lock with no fresh peer heartbeat — the state where
    runs are accepted but ticked by nobody (invisible before this surface).
    """
    from .service import gateway_runner_health_snapshot

    body = {"status": "healthy", "service": "abstractgateway"}
    # Event-loop watchdog (2026-10-04): {enabled, limit_s, last_tick_age_s}.
    try:
        from .loop_watchdog import health_snapshot as _watchdog_snapshot

        body["watchdog"] = _watchdog_snapshot()
    except Exception:
        pass
    # Model host (ADR 0002): where the models run; in the `process` mode the child's state, pid,
    # restarts in the window, memory, and the newest incident. `failed` (it stopped restarting)
    # degrades the status; the gateway itself still answers.
    try:
        from . import model_host as _model_host

        body["model_host"] = _model_host.health_snapshot()
        if body["model_host"].get("state") == "failed":
            body["status"] = "degraded"
    except Exception as e:  # noqa: BLE001 - health stays answerable; the error is shown
        body["model_host"] = {"error": f"{type(e).__name__}: {e}"}
    # Worker pools (gateway load 2026-10-26): size, threads started, queued work per kind.
    try:
        from .work_pools import pool_stats

        body["pools"] = pool_stats()
    except Exception:
        pass
    # GC tuning (gateway load 2026-10-26): what `serve --gc-tuning` applied after the boot, since the
    # service logger's INFO line does not reach every log file.
    try:
        from . import service as _svc_mod

        rep = getattr(_svc_mod, "_gc_tuning_report", None)
        if isinstance(rep, dict) and rep:
            body["gc_tuning"] = {k: v for k, v in rep.items() if k != "line"}
    except Exception:
        pass
    # Run index (gateway load 2026-10-26): mode and the shadow counters (verify mode compares the scan
    # with the in-memory index; `shadow_mismatches` must stay 0 before `--run-index trusted` is the default).
    try:
        from .stores import run_index_mode

        from . import service as _service_mod

        # Every service this process holds (the default plane and the per-account ones), never built here.
        candidates = [getattr(_service_mod, "_service", None)] + list(getattr(_service_mod, "_services_by_principal", {}).values())
        stats: Dict[str, Any] = {}
        planes = 0
        for svc in candidates:
            store = getattr(getattr(svc, "stores", None), "run_store", None) or getattr(svc, "run_store", None)
            if store is None or not hasattr(store, "run_index_stats"):
                continue
            one = store.run_index_stats()
            if not isinstance(one, dict):
                continue
            planes += 1
            for k, v in one.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    stats[k] = stats.get(k, 0) + v
                elif k not in stats:
                    stats[k] = v
        stats["planes"] = planes
        body["run_index"] = {"mode": run_index_mode(), **(dict(stats) if isinstance(stats, dict) else {})}
    except Exception:
        pass
    # Boot honesty (supervisor seam, c4063): while the background boot runs,
    # answer status="starting" — a supervisor must never count a loading
    # gateway as unresponsive (false recycle) NOR as fully healthy. A failed
    # boot is loudly "degraded" with the error.
    try:
        from .service import gateway_boot_state

        boot = gateway_boot_state()
        if boot.get("state") == "starting":
            body["status"] = "starting"
            body["boot"] = boot
            return body
        if boot.get("state") == "failed":
            body["status"] = "degraded"
            body["boot"] = boot
    except Exception:
        pass
    try:
        runner_snapshot = gateway_runner_health_snapshot()
    except Exception as e:
        runner_snapshot = {"initialized": False, "degraded": False, "error": f"{type(e).__name__}: {e}"}
    body["runner"] = runner_snapshot
    # Warming up (boot-time lane 2026-10-01): a service build is in progress
    # (eager rehydration / first touch). Alive and serving; some API calls
    # wait for the build. Never degrades `status`.
    if runner_snapshot.get("building"):
        body["warming_up"] = True
    # Host pause (tray/console, 2026-09-05): visible on the liveness probe,
    # but `status` stays healthy — a paused gateway is an operator's choice
    # and a supervisor must never recycle it for that.
    try:
        from .host_control import pause_snapshot

        if pause_snapshot().get("paused"):
            body["paused"] = True
    except Exception:
        pass
    # Long-lived worker liveness (resilience wave 2026-07-21): reapers,
    # sweepers, bridges. A dead worker degrades honesty-first — the operator's
    # supervisor can key on it, and the label names WHICH worker died.
    try:
        from .worker_registry import workers_snapshot

        workers = workers_snapshot()
        if workers:
            body["workers"] = workers
            dead = sorted(name for name, st in workers.items() if not st.get("alive"))
            if dead:
                body["dead_workers"] = dead
                body["status"] = "degraded"
    except Exception:
        pass
    # Backlog exec runner (opt-in subsystem): status existed but health never
    # asked (adversary B P0-3). Only rendered when the feature is on.
    try:
        from .service import backlog_exec_runner_status

        st = backlog_exec_runner_status()
        if st.get("alive") or st.get("error"):
            body["backlog_exec_runner"] = st
    except Exception:
        pass
    # Eager rehydration outcome (backlog 0063, adversary P1-3): a failed warm
    # is otherwise invisible — the idle tenants this serves never send the
    # request that would retry. Surfaced (not degrading: a warm failure is a
    # per-principal stall the self-heal retries on that principal's access).
    try:
        from .service import rehydration_status

        reh = rehydration_status()
        if reh:
            body["rehydration"] = reh
    except Exception:
        pass
    if runner_snapshot.get("degraded"):
        body["status"] = "degraded"
    return body
