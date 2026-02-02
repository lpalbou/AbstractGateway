from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.integration


def test_gateway_audit_tail_endpoint_returns_recent_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUDIT_LOG", "1")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}

    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/commands",
            headers=headers,
            json={"command_id": "c1", "run_id": "r1", "type": "pause", "payload": {"reason": "test"}},
        )
        assert r.status_code == 200, r.text
        rid = str(r.headers.get("x-request-id") or "").strip()
        assert rid

        tail = client.get("/api/gateway/audit/tail?max_bytes=200000", headers=headers)
        assert tail.status_code == 200, tail.text
        body = tail.json()
        assert body.get("ok") is True
        content = str(body.get("content") or "")
        assert content.strip()

        lines = [ln for ln in content.splitlines() if ln.strip()]
        entry = json.loads(lines[-1])
        assert entry.get("request_id") == rid
        assert entry.get("method") == "POST"
        assert entry.get("path") == "/api/gateway/commands"
