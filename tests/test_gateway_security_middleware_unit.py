from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncIterator

import pytest
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

    @app.post("/api/gateway/attachments/upload")
    async def upload_attachment(request: Request):
        # The security middleware enforces body limits based on content-length; this endpoint just
        # confirms the request reached the app.
        body = await request.body()
        return {"ok": True, "size": len(body)}

    @app.post("/api/gateway/bundles/upload")
    async def upload_bundle(request: Request):
        body = await request.body()
        return {"ok": True, "size": len(body)}

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


def test_origin_allowlist_supports_glob_patterns() -> None:
    app = _make_app(
        policy=GatewayAuthPolicy(
            enabled=True,
            tokens=("t",),
            allowed_origins=("https://*.ngrok-free.app",),
        )
    )
    with TestClient(app) as client:
        r = client.get(
            "/api/gateway/runs/x",
            headers={"Authorization": "Bearer t", "Origin": "https://cd37c7c7a7de.ngrok-free.app"},
        )
        assert r.status_code == 200, r.text


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


def test_write_body_limit_rejects_large_payloads() -> None:
    app = _make_app(
        policy=GatewayAuthPolicy(
            enabled=True,
            tokens=("t",),
            max_body_bytes=2_000,
            max_upload_body_bytes=50_000,
        )
    )
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer t"}
        big = {"x": "a" * 10_000}
        r = client.post("/api/gateway/commands", json=big, headers=headers)
        assert r.status_code == 413


def test_upload_uses_upload_body_limit_for_attachments() -> None:
    app = _make_app(
        policy=GatewayAuthPolicy(
            enabled=True,
            tokens=("t",),
            max_body_bytes=2_000,
            max_upload_body_bytes=100_000,
        )
    )
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer t"}
        payload = b"x" * 20_000
        r = client.post(
            "/api/gateway/attachments/upload",
            files={"file": ("notes.txt", payload, "text/plain")},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.json().get("ok") is True


def test_upload_uses_upload_body_limit_for_bundles() -> None:
    app = _make_app(
        policy=GatewayAuthPolicy(
            enabled=True,
            tokens=("t",),
            max_body_bytes=2_000,
            max_upload_body_bytes=100_000,
        )
    )
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer t"}
        payload = b"x" * 20_000
        r = client.post(
            "/api/gateway/bundles/upload",
            files={"file": ("bundle.flow", payload, "application/octet-stream")},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.json().get("ok") is True


def test_upload_body_limit_rejects_when_exceeded() -> None:
    app = _make_app(
        policy=GatewayAuthPolicy(
            enabled=True,
            tokens=("t",),
            max_body_bytes=2_000,
            max_upload_body_bytes=10_000,
        )
    )
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer t"}
        payload = b"x" * 20_000
        r = client.post(
            "/api/gateway/attachments/upload",
            files={"file": ("notes.txt", payload, "text/plain")},
            headers=headers,
        )
        assert r.status_code == 413


def test_mutating_requests_write_audit_log_and_include_request_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUDIT_LOG", "1")

    app = _make_app(policy=GatewayAuthPolicy(enabled=True, tokens=("t",)))
    with TestClient(app) as client:
        r = client.post("/api/gateway/commands", json={"x": 1}, headers={"Authorization": "Bearer t"})
        assert r.status_code == 200, r.text
        rid = str(r.headers.get("x-request-id") or "").strip()
        assert rid

    audit_path = tmp_path / "audit_log.jsonl"
    assert audit_path.exists()
    lines = [ln for ln in audit_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines
    entry = json.loads(lines[-1])
    assert entry.get("request_id") == rid
    assert entry.get("method") == "POST"
    assert entry.get("path") == "/api/gateway/commands"
    assert entry.get("status") == 200
    assert entry.get("auth_required") is True
    assert "Bearer t" not in lines[-1]


def test_unauthorized_mutating_requests_are_audited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUDIT_LOG", "1")

    app = _make_app(policy=GatewayAuthPolicy(enabled=True, tokens=("t",)))
    with TestClient(app) as client:
        r = client.post("/api/gateway/commands", json={"x": 1})
        assert r.status_code == 401
        rid = str(r.headers.get("x-request-id") or "").strip()
        assert rid

    audit_path = tmp_path / "audit_log.jsonl"
    assert audit_path.exists()
    lines = [ln for ln in audit_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines
    entry = json.loads(lines[-1])
    assert entry.get("request_id") == rid
    assert entry.get("method") == "POST"
    assert entry.get("path") == "/api/gateway/commands"
    assert entry.get("status") == 401
    assert entry.get("auth_required") is True
