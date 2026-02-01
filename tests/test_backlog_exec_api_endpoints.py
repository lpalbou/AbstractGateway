from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@dataclass
class _Stores:
    base_dir: Path


@dataclass
class _Service:
    stores: _Stores


def _make_app(*, monkeypatch: pytest.MonkeyPatch, gateway_base_dir: Path) -> FastAPI:
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "get_gateway_service", lambda: _Service(stores=_Stores(base_dir=gateway_base_dir)))
    monkeypatch.setattr(gateway_routes, "backlog_exec_runner_status", lambda: {"alive": True, "error": None})

    app = FastAPI()
    app.include_router(gateway_routes.router, prefix="/api")
    return app


@pytest.mark.basic
def test_backlog_exec_config_and_requests_endpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway_dir = tmp_path / "gateway"
    gateway_dir.mkdir(parents=True, exist_ok=True)
    (gateway_dir / "backlog_exec_queue").mkdir(parents=True, exist_ok=True)

    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(repo_root))

    # Enable runner config for endpoint.
    monkeypatch.setenv("ABSTRACTGATEWAY_BACKLOG_EXEC_RUNNER", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_BACKLOG_EXECUTOR", "codex_cli")
    monkeypatch.setenv("ABSTRACTGATEWAY_BACKLOG_CODEX_BIN", sys.executable)
    monkeypatch.setenv("ABSTRACTGATEWAY_BACKLOG_CODEX_MODEL", "gpt-5.2")

    # Seed a few request JSON files.
    qdir = gateway_dir / "backlog_exec_queue"
    (qdir / "aaaaaaaaaaaaaaaa.json").write_text(
        "\n".join(
            [
                "{",
                '  "created_at": "2026-01-31T00:00:00Z",',
                '  "request_id": "aaaaaaaaaaaaaaaa",',
                '  "status": "queued",',
                '  "backlog": {"kind": "planned", "filename": "650-framework-test.md", "relpath": "docs/backlog/planned/650-framework-test.md"}',
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (qdir / "bbbbbbbbbbbbbbbb.json").write_text(
        "\n".join(
            [
                "{",
                '  "created_at": "2026-01-31T01:00:00Z",',
                '  "request_id": "bbbbbbbbbbbbbbbb",',
                '  "status": "running",',
                '  "started_at": "2026-01-31T01:00:10Z",',
                '  "backlog": {"kind": "planned", "filename": "650-framework-test.md", "relpath": "docs/backlog/planned/650-framework-test.md"}',
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (qdir / "cccccccccccccccc.json").write_text(
        "\n".join(
            [
                "{",
                '  "created_at": "2026-01-31T02:00:00Z",',
                '  "request_id": "cccccccccccccccc",',
                '  "status": "failed",',
                '  "finished_at": "2026-01-31T02:02:00Z",',
                '  "backlog": {"kind": "planned", "filename": "650-framework-test.md", "relpath": "docs/backlog/planned/650-framework-test.md"},',
                '  "result": {"ok": false, "exit_code": 1, "error": "boom"}',
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    app = _make_app(monkeypatch=monkeypatch, gateway_base_dir=gateway_dir)
    with TestClient(app) as client:
        cfg = client.get("/api/gateway/backlog/exec/config")
        assert cfg.status_code == 200
        body = cfg.json()
        assert body["runner_enabled"] is True
        assert body["runner_alive"] is True
        assert body["executor"] == "codex_cli"
        assert body["can_execute"] is True

        lst = client.get("/api/gateway/backlog/exec/requests?status=queued,running&limit=10")
        assert lst.status_code == 200
        items = lst.json()["requests"]
        assert len(items) == 2
        statuses = {it["status"] for it in items}
        assert statuses == {"queued", "running"}

        failed = client.get("/api/gateway/backlog/exec/requests?status=failed")
        assert failed.status_code == 200
        items = failed.json()["requests"]
        assert len(items) == 1
        assert items[0]["request_id"] == "cccccccccccccccc"
        assert items[0]["error"] == "boom"

        detail = client.get("/api/gateway/backlog/exec/requests/cccccccccccccccc")
        assert detail.status_code == 200
        payload = detail.json()["payload"]
        assert payload.get("request_id") == "cccccccccccccccc"
        assert "prompt" not in payload

        # Log tail endpoint (best-effort).
        run_dir = gateway_dir / "backlog_exec_runs" / "cccccccccccccccc"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "codex_events.jsonl").write_text("line1\nline2\n", encoding="utf-8")
        (run_dir / "codex_stderr.log").write_text("stderr\n", encoding="utf-8")
        tail = client.get("/api/gateway/backlog/exec/requests/cccccccccccccccc/logs/tail?name=events&max_bytes=2048")
        assert tail.status_code == 200
        t = tail.json()
        assert t["name"] == "events"
        assert "line2" in t["content"]
