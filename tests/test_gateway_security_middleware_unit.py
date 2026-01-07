from __future__ import annotations

import asyncio
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from abstractgateway.security.gateway_security import GatewayAuthPolicy, GatewaySecurityMiddleware


def _make_app(*, policy: GatewayAuthPolicy) -> FastAPI:
    app = FastAPI()
    app.add_middleware(GatewaySecurityMiddleware, policy=policy)

    @app.get("/api/gateway/runs/{run_id}")
    async def get_run(run_id: str):
        return {"ok": True, "run_id": run_id}

    @app.post("/api/gateway/commands")
    async def post_commands(payload: dict):
        return {"ok": True, "payload": payload}

    @app.get("/api/gateway/slow")
    async def slow():
        # Used by concurrency limit tests: keeps the request open while still yielding the event loop.
        await asyncio.sleep(0.25)
        return {"ok": True}

    @app.get("/api/gateway/runs/{run_id}/ledger/stream")
    async def ledger_stream(run_id: str, request: Request):
        async def _sse_gen() -> AsyncIterator[bytes]:
            # Emit one message, then keep the stream open until the client disconnects.
            yield b"event: step\ndata: {\"ok\":true}\n\n"
            while not await request.is_disconnected():
                await asyncio.sleep(0.1)
                yield b": keep-alive\n\n"

        return StreamingResponse(_sse_gen(), media_type="text/event-stream")

    return app


def test_read_requires_token_by_default() -> None:
    app = _make_app(policy=GatewayAuthPolicy(enabled=True, tokens=("t",)))
    with TestClient(app) as client:
        r = client.get("/api/gateway/runs/x")
        assert r.status_code == 401


def test_write_requires_token_by_default() -> None:
    app = _make_app(policy=GatewayAuthPolicy(enabled=True, tokens=("t",)))
    with TestClient(app) as client:
        r = client.post("/api/gateway/commands", json={"x": 1})
        assert r.status_code == 401


def test_dev_read_no_auth_loopback_allows_reads() -> None:
    app = _make_app(
        policy=GatewayAuthPolicy(
            enabled=True,
            tokens=("t",),
            protect_read_endpoints=True,
            protect_write_endpoints=True,
            dev_allow_unauthenticated_reads_on_loopback=True,
        )
    )
    with TestClient(app) as client:
        r = client.get("/api/gateway/runs/x")
        assert r.status_code == 200


def test_can_disable_write_protection_for_local_dev() -> None:
    app = _make_app(
        policy=GatewayAuthPolicy(
            enabled=True,
            tokens=(),
            protect_read_endpoints=True,
            protect_write_endpoints=False,
        )
    )
    with TestClient(app) as client:
        r = client.post("/api/gateway/commands", json={"x": 1})
        assert r.status_code == 200


def test_origin_allowlist_blocks_untrusted_origin() -> None:
    app = _make_app(
        policy=GatewayAuthPolicy(
            enabled=True,
            tokens=("t",),
            allowed_origins=("http://localhost:*",),
        )
    )
    with TestClient(app) as client:
        r = client.get(
            "/api/gateway/runs/x",
            headers={"Authorization": "Bearer t", "Origin": "http://evil.example"},
        )
        assert r.status_code == 403


def test_sse_requires_token() -> None:
    app = _make_app(policy=GatewayAuthPolicy(enabled=True, tokens=("t",)))
    with TestClient(app) as client:
        r = client.get("/api/gateway/runs/x/ledger/stream")
        assert r.status_code == 401


def test_concurrency_limit_returns_429_for_overload() -> None:
    # Use an async client to issue truly concurrent requests against the same ASGI app.
    import httpx

    app = _make_app(
        policy=GatewayAuthPolicy(
            enabled=True,
            tokens=("t",),
            max_concurrency=1,
        )
    )

    async def _run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": "Bearer t"}

            async def req():
                return await client.get("/api/gateway/slow", headers=headers)

            r1, r2 = await asyncio.gather(req(), req())
            codes = sorted([r1.status_code, r2.status_code])
            assert codes == [200, 429]

    asyncio.run(_run())


