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

    @app.get("/api/gateway/admin/probe")
    async def admin_probe():
        return {"ok": True}

    @app.get("/api/gateway/audit/events")
    async def audit_events():
        return {"ok": True}

    @app.get("/api/gateway/processes")
    async def processes():
        return {"ok": True}

    @app.get("/api/gateway/backlog/items")
    async def backlog_items():
        return {"ok": True}

    @app.get("/api/gateway/triage/items")
    async def triage_items():
        return {"ok": True}

    @app.get("/api/gateway/reports/items")
    async def reports_items():
        return {"ok": True}

    @app.get("/api/gateway/email/messages")
    async def email_messages():
        return {"ok": True}

    @app.get("/api/gateway/host/metrics/gpu")
    async def host_gpu_metrics():
        return {"ok": True}

    @app.get("/api/gateway/models/loaded")
    async def models_loaded():
        return {"ok": True}

    @app.post("/api/gateway/models/load")
    async def models_load(payload: dict):
        return {"ok": True, "payload": payload}

    @app.get("/api/gateway/files/read")
    async def files_read():
        return {"ok": True}

    @app.post("/api/gateway/artifacts/import")
    async def artifacts_import(payload: dict):
        return {"ok": True, "payload": payload}

    @app.post("/api/gateway/runs/{run_id}/artifacts/{artifact_id}/export")
    async def artifact_export(run_id: str, artifact_id: str, payload: dict):
        return {"ok": True, "run_id": run_id, "artifact_id": artifact_id, "payload": payload}

    @app.put("/api/gateway/config/capability-defaults/{kind}/{modality}")
    async def capability_default_put(kind: str, modality: str, payload: dict):
        return {"ok": True, "kind": kind, "modality": modality, "payload": payload}

    @app.post("/api/gateway/commands")
    async def post_commands(payload: dict):
        return {"ok": True, "payload": payload}

    @app.post("/api/gateway/blocs/upsert_text")
    async def blocs_upsert_text(payload: dict):
        return {"ok": True, "payload": payload}

    @app.post("/api/gateway/prompt_cache/sessions")
    async def prompt_cache_session(payload: dict):
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


def test_dev_read_no_auth_loopback_does_not_grant_admin_reads() -> None:
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
        allowed = client.get("/api/gateway/runs/x")
        denied = client.get("/api/gateway/admin/probe")
        assert allowed.status_code == 200
        assert denied.status_code == 403
        assert denied.json()["reason_code"] == "admin_required"


def test_non_admin_user_token_cannot_access_operator_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path))
    from abstractgateway.users import GatewayUserRegistry

    _record, user_token = GatewayUserRegistry().create_user(user_id="alice", roles=["user"])
    app = _make_app(policy=GatewayAuthPolicy(enabled=True, tokens=("admin-token",), user_auth_enabled=True))
    with TestClient(app) as client:
        denied = client.get("/api/gateway/processes", headers={"Authorization": f"Bearer {user_token}"})
        allowed = client.get("/api/gateway/processes", headers={"Authorization": "Bearer admin-token"})

    assert denied.status_code == 403
    assert denied.json()["resource_class"] == "processes"
    assert allowed.status_code == 200


def test_non_admin_user_token_cannot_access_server_workspace_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path))
    from abstractgateway.users import GatewayUserRegistry

    _record, user_token = GatewayUserRegistry().create_user(user_id="alice", roles=["user"])
    app = _make_app(policy=GatewayAuthPolicy(enabled=True, tokens=("admin-token",), user_auth_enabled=True))
    headers = {"Authorization": f"Bearer {user_token}"}
    with TestClient(app) as client:
        denied_read = client.get("/api/gateway/files/read?path=pyproject.toml", headers=headers)
        denied_import = client.post("/api/gateway/artifacts/import", headers=headers, json={"path": "x"})
        denied_export = client.post("/api/gateway/runs/r1/artifacts/a1/export", headers=headers, json={"path": "x"})
        admin_allowed = client.get("/api/gateway/files/read?path=pyproject.toml", headers={"Authorization": "Bearer admin-token"})

    assert denied_read.status_code == 403
    assert denied_read.json()["resource_class"] == "workspace"
    assert denied_import.status_code == 403
    assert denied_export.status_code == 403
    assert admin_allowed.status_code == 200


def test_non_admin_user_token_can_update_own_capability_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path))
    from abstractgateway.users import GatewayUserRegistry

    _record, user_token = GatewayUserRegistry().create_user(user_id="alice", roles=["user"])
    app = _make_app(policy=GatewayAuthPolicy(enabled=True, tokens=("admin-token",), user_auth_enabled=True))
    with TestClient(app) as client:
        response = client.put(
            "/api/gateway/config/capability-defaults/output/text",
            headers={"Authorization": f"Bearer {user_token}"},
            json={"provider": "openrouter", "model": "model-a"},
        )

    assert response.status_code == 200


def test_admin_required_routes_ignore_user_scopes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path))
    from abstractgateway.users import GatewayUserRegistry

    _record, user_token = GatewayUserRegistry().create_user(user_id="alice", roles=["user"], scopes=["processes:read", "*"])
    app = _make_app(policy=GatewayAuthPolicy(enabled=True, tokens=("admin-token",), user_auth_enabled=True))
    with TestClient(app) as client:
        denied = client.get("/api/gateway/processes", headers={"Authorization": f"Bearer {user_token}"})

    assert denied.status_code == 403
    assert denied.json()["required_role"] == "admin"


def test_non_admin_wildcard_scope_cannot_access_any_admin_route_family(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path))
    from abstractgateway.users import GatewayUserRegistry

    _record, user_token = GatewayUserRegistry().create_user(user_id="alice", roles=["user"], scopes=["*"])
    app = _make_app(policy=GatewayAuthPolicy(enabled=True, tokens=("admin-token",), user_auth_enabled=True))
    headers = {"Authorization": f"Bearer {user_token}"}
    routes = [
        ("GET", "/api/gateway/admin/probe", None, "admin"),
        ("GET", "/api/gateway/audit/events", None, "audit"),
        ("GET", "/api/gateway/processes", None, "processes"),
        ("GET", "/api/gateway/backlog/items", None, "backlog"),
        ("GET", "/api/gateway/triage/items", None, "triage"),
        ("GET", "/api/gateway/reports/items", None, "reports"),
        ("GET", "/api/gateway/email/messages", None, "email"),
        ("GET", "/api/gateway/host/metrics/gpu", None, "host"),
        ("GET", "/api/gateway/models/loaded", None, "models"),
        ("POST", "/api/gateway/models/load", {"model": "x"}, "models"),
        ("GET", "/api/gateway/files/read?path=pyproject.toml", None, "workspace"),
        ("POST", "/api/gateway/artifacts/import", {"path": "x"}, "workspace"),
        ("POST", "/api/gateway/runs/r1/artifacts/a1/export", {"path": "x"}, "workspace"),
        ("POST", "/api/gateway/blocs/upsert_text", {"text": "x"}, "blocs"),
        ("POST", "/api/gateway/prompt_cache/sessions", {"session_id": "s"}, "prompt_cache"),
    ]
    with TestClient(app) as client:
        for method, path, body, resource in routes:
            response = client.request(method, path, headers=headers, json=body)
            assert response.status_code == 403, (method, path, response.text)
            payload = response.json()
            assert payload["required_role"] == "admin"
            assert payload["resource_class"] == resource


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
