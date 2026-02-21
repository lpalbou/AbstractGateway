"""AbstractGateway FastAPI application."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

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
    version="0.2.1",
    lifespan=_lifespan,
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


@app.get("/api/health")
async def health_check():
    return {"status": "healthy", "service": "abstractgateway"}
