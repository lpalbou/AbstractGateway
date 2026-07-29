from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from abstractgateway.session_history_bloc import (
    parse_created_at_cursor,
    select_bloc_turns,
)


def test_parse_created_at_cursor_accepts_z_suffix() -> None:
    assert parse_created_at_cursor("2026-07-28T06:00:00Z") == "2026-07-28T06:00:00+00:00"


def test_parse_created_at_cursor_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="Invalid ISO-8601"):
        parse_created_at_cursor("not-a-date")


def test_select_bloc_turns_uses_iso_cursor_not_counts() -> None:
    turns = [
        {"run_id": "r3", "created_at": "2026-07-28T03:00:00+00:00"},
        {"run_id": "r2", "created_at": "2026-07-28T02:00:00+00:00"},
        {"run_id": "r1", "created_at": "2026-07-28T01:00:00+00:00"},
    ]
    bloc, cursor_after, older = select_bloc_turns(turns, before="2026-07-28T04:00:00+00:00", limit=2)
    assert [t["run_id"] for t in bloc] == ["r3", "r2"]
    assert cursor_after == "2026-07-28T02:00:00+00:00"
    assert older == 1

    older_bloc, older_cursor, older_remaining = select_bloc_turns(
        turns,
        before=cursor_after,
        limit=5,
    )
    assert [t["run_id"] for t in older_bloc] == ["r1"]
    assert older_cursor == "2026-07-28T01:00:00+00:00"
    assert older_remaining == 0


def _wait_until(predicate, *, timeout_s: float = 8.0, poll_s: float = 0.05):
    end = time.time() + timeout_s
    while time.time() < end:
        if predicate():
            return
        time.sleep(poll_s)
    raise AssertionError("timeout waiting for condition")


def _write_min_bundle(*, bundles_dir: Path, bundle_id: str, flow_id: str) -> None:
    bundles_dir.mkdir(parents=True, exist_ok=True)
    flow = {
        "id": flow_id,
        "name": "minimal",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "node-1",
                "type": "on_flow_start",
                "position": {"x": 32.0, "y": 128.0},
                "data": {"nodeType": "on_flow_start", "label": "On Flow Start", "inputs": [], "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]},
            },
            {
                "id": "node-2",
                "type": "on_flow_end",
                "position": {"x": 288.0, "y": 128.0},
                "data": {"nodeType": "on_flow_end", "label": "On Flow End", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []},
            },
        ],
        "edges": [{"id": "e1", "source": "node-1", "sourceHandle": "exec-out", "target": "node-2", "targetHandle": "exec-in"}],
        "entryNode": "node-1",
    }
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-01-09T00:00:00+00:00",
        "entrypoints": [{"flow_id": flow_id, "name": "root", "description": "", "interfaces": []}],
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }
    bundle_path = bundles_dir / f"{bundle_id}.flow"
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr(f"flows/{flow_id}.json", json.dumps(flow, indent=2))


pytestmark = pytest.mark.integration


def test_session_history_bloc_endpoint_returns_cursor_bloc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    db_path = runtime_dir / "gateway.sqlite3"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-bloc", flow_id="root")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_DB_PATH", str(db_path))
    monkeypatch.setenv("ABSTRACTGATEWAY_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    session_id = "sess_bloc_1"

    def _start_and_wait(client: TestClient, prompt: str) -> str:
        start = client.post(
            "/api/gateway/runs/start",
            headers=headers,
            json={
                "bundle_id": "bundle-bloc",
                "flow_id": "root",
                "session_id": session_id,
                "input_data": {"prompt": prompt, "context": {"messages": [{"role": "user", "content": prompt}], "attachments": []}},
            },
        )
        assert start.status_code == 200, start.text
        rid = start.json()["run_id"]

        def _is_completed() -> bool:
            rr = client.get(f"/api/gateway/runs/{rid}", headers=headers)
            assert rr.status_code == 200, rr.text
            return rr.json().get("status") == "completed"

        _wait_until(_is_completed, timeout_s=8.0, poll_s=0.05)
        return rid

    with TestClient(app) as client:
        run_ids = [_start_and_wait(client, f"turn-{i}") for i in range(3)]
        assert len(set(run_ids)) == 3

        first = client.get(
            f"/api/gateway/sessions/{session_id}/history/bloc",
            headers=headers,
            params={"limit": 2, "detail": "replay", "include_subruns": "false"},
        )
        assert first.status_code == 200, first.text
        body = first.json()
        assert body.get("session_id") == session_id
        assert body.get("cursor_before") is None
        assert isinstance(body.get("warnings"), list)
        turns = body.get("turns") or []
        assert len(turns) == 2
        assert all(isinstance(t.get("bundle"), dict) for t in turns)
        assert body.get("older_remaining") == 1
        cursor_after = body.get("cursor_after")
        assert isinstance(cursor_after, str) and cursor_after

        second = client.get(
            f"/api/gateway/sessions/{session_id}/history/bloc",
            headers=headers,
            params={"before": cursor_after, "limit": 5, "detail": "replay", "include_subruns": "false"},
        )
        assert second.status_code == 200, second.text
        body2 = second.json()
        assert body2.get("cursor_before") == cursor_after
        assert body2.get("older_remaining") == 0
        assert len(body2.get("turns") or []) == 1

        bad = client.get(
            f"/api/gateway/sessions/{session_id}/history/bloc",
            headers=headers,
            params={"before": "yesterday"},
        )
        assert bad.status_code == 400

        unknown = client.get(
            f"/api/gateway/sessions/{session_id}/history/bloc",
            headers=headers,
            params={"offset": "0"},
        )
        assert unknown.status_code == 400
        assert "Unknown query parameter" in unknown.text
