"""AbstractGateway FastAPI application."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
    version="0.2.0",
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


@app.get("/api/health")
async def health_check():
    return {"status": "healthy", "service": "abstractgateway"}
