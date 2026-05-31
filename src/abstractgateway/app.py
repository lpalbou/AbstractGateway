"""AbstractGateway FastAPI application."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, RedirectResponse

from .console import gateway_console_html

from .routes import gateway_router, triage_router
from .security import GatewaySecurityMiddleware, load_gateway_auth_policy_from_env


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Start the background worker that polls the durable command inbox and ticks runs.
    from .service import start_gateway_runner, stop_gateway_runner

    start_gateway_runner()
    try:
        yield
    finally:
        stop_gateway_runner()


app = FastAPI(
    title="AbstractGateway",
    description="Durable Run Gateway for AbstractRuntime (commands + ledger replay/stream).",
    version="0.2.23",
    lifespan=_lifespan,
    docs_url=None,
    redoc_url=None,
)

# Gateway security (backlog 309).
app.add_middleware(GatewaySecurityMiddleware, policy=load_gateway_auth_policy_from_env())

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

app.include_router(gateway_router, prefix="/api")
app.include_router(triage_router, prefix="/api")

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
    return {"status": "healthy", "service": "abstractgateway"}
