from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
import zipfile

import pytest
from fastapi.testclient import TestClient


def _wait_until(predicate, *, timeout_s: float = 20.0, poll_s: float = 0.05):
    end = time.time() + timeout_s
    while time.time() < end:
        if predicate():
            return
        time.sleep(poll_s)
    raise AssertionError("timeout waiting for condition")


def _write_bundle(*, bundles_dir: Path, bundle_id: str, flows: dict[str, dict], entrypoint: str) -> tuple[str, str]:
    bundles_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-01-17T00:00:00+00:00",
        "entrypoints": [{"flow_id": entrypoint, "name": "test", "description": "", "interfaces": []}],
        "flows": {fid: f"flows/{fid}.json" for fid in flows.keys()},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }

    bundle_path = bundles_dir / f"{bundle_id}.flow"
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        for fid, flow in flows.items():
            zf.writestr(f"flows/{fid}.json", json.dumps(flow, indent=2))

    return bundle_id, entrypoint


@pytest.mark.e2e
def test_gateway_sqlite_wait_index_offloading_dedupe_lmstudio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Level C: real gateway + sqlite stores + wait_index + tool execution + LMStudio inference.

    Enable with:
      ABSTRACT_E2E_LMSTUDIO=1
    """
    if os.environ.get("ABSTRACT_E2E_LMSTUDIO") != "1":
        pytest.skip("Set ABSTRACT_E2E_LMSTUDIO=1 to run this test.")

    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir(parents=True, exist_ok=True)

    big = "MAGIC" + ("x" * 200_000)
    big_path = sandbox_dir / "big.txt"
    big_path.write_text(big, encoding="utf-8")

    token = "t"
    db_path = runtime_dir / "gateway.sqlite3"

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("ABSTRACTGATEWAY_DB_PATH", str(db_path))
    monkeypatch.setenv("ABSTRACTGATEWAY_TOOL_MODE", "local")
    monkeypatch.setenv("ABSTRACTRUNTIME_MAX_INLINE_BYTES", "1024")

    base_url = os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")
    model = os.environ.get("LMSTUDIO_MODEL", "qwen/qwen3-next-80b")
    monkeypatch.setenv("LMSTUDIO_BASE_URL", base_url)
    monkeypatch.setenv("ABSTRACTGATEWAY_PROVIDER", "lmstudio")
    monkeypatch.setenv("ABSTRACTGATEWAY_MODEL", model)

    flow_id = "root"
    flow = {
        "id": flow_id,
        "name": "e2e-sqlite-offload",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "start",
                "type": "on_flow_start",
                "position": {"x": 0.0, "y": 0.0},
                "data": {"nodeType": "on_flow_start", "label": "On Flow Start", "inputs": [], "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]},
            },
            {
                "id": "wait",
                "type": "wait_until",
                "position": {"x": 220.0, "y": 0.0},
                "data": {
                    "nodeType": "wait_until",
                    "label": "Wait Until",
                    "inputs": [{"id": "exec-in", "label": "", "type": "execution"}, {"id": "duration", "label": "duration", "type": "number"}],
                    "outputs": [{"id": "exec-out", "label": "", "type": "execution"}, {"id": "result", "label": "result", "type": "string"}],
                    "pinDefaults": {"duration": 0.5},
                    "effectConfig": {"durationType": "seconds"},
                },
            },
            {
                "id": "tool",
                "type": "tool_calls",
                "position": {"x": 440.0, "y": 0.0},
                "data": {
                    "nodeType": "tool_calls",
                    "label": "Tool Calls",
                    "inputs": [{"id": "exec-in", "label": "", "type": "execution"}, {"id": "tool_calls", "label": "tool_calls", "type": "array"}],
                    "outputs": [{"id": "exec-out", "label": "", "type": "execution"}, {"id": "results", "label": "results", "type": "array"}],
                    "pinDefaults": {"tool_calls": [{"name": "read_file", "arguments": {"file_path": str(big_path)}}]},
                },
            },
            {
                "id": "llm",
                "type": "llm_call",
                "position": {"x": 660.0, "y": 0.0},
                "data": {
                    "nodeType": "llm_call",
                    "label": "LLM Call",
                    "inputs": [{"id": "exec-in", "label": "", "type": "execution"}, {"id": "prompt", "label": "prompt", "type": "string"}],
                    "outputs": [{"id": "exec-out", "label": "", "type": "execution"}, {"id": "response", "label": "response", "type": "string"}],
                    "pinDefaults": {"prompt": "Reply with exactly: OK"},
                    "effectConfig": {"provider": "lmstudio", "model": model},
                },
            },
            {
                "id": "end",
                "type": "on_flow_end",
                "position": {"x": 880.0, "y": 0.0},
                "data": {"nodeType": "on_flow_end", "label": "On Flow End", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []},
            },
        ],
        "edges": [
            {"id": "e1", "source": "start", "sourceHandle": "exec-out", "target": "wait", "targetHandle": "exec-in"},
            {"id": "e2", "source": "wait", "sourceHandle": "exec-out", "target": "tool", "targetHandle": "exec-in"},
            {"id": "e3", "source": "tool", "sourceHandle": "exec-out", "target": "llm", "targetHandle": "exec-in"},
            {"id": "e4", "source": "llm", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"},
        ],
        "entryNode": "start",
    }

    bundle_id, _entry = _write_bundle(bundles_dir=bundles_dir, bundle_id="bundle-e2e-sqlite", flows={flow_id: flow}, entrypoint=flow_id)

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}

    def _wait_index_contains(run_id: str) -> bool:
        if not db_path.exists():
            return False
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute("SELECT next_due_iso FROM wait_index WHERE run_id = ?;", (run_id,)).fetchone()
            return row is not None
        finally:
            conn.close()

    def _wait_index_absent(run_id: str) -> bool:
        if not db_path.exists():
            return True
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute("SELECT 1 FROM wait_index WHERE run_id = ?;", (run_id,)).fetchone()
            return row is None
        finally:
            conn.close()

    def _run_completed(client: TestClient, run_id: str) -> bool:
        rr = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
        assert rr.status_code == 200, rr.text
        body = rr.json()
        return body.get("status") == "completed" and not body.get("error")

    def _run_waiting(client: TestClient, run_id: str) -> bool:
        rr = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
        assert rr.status_code == 200, rr.text
        return rr.json().get("status") == "waiting"

    with TestClient(app) as client:
        r1 = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": bundle_id, "flow_id": flow_id, "input_data": {"workspace_root": str(sandbox_dir)}},
            headers=headers,
        )
        assert r1.status_code == 200, r1.text
        run_id1 = r1.json()["run_id"]

        _wait_until(lambda: _run_waiting(client, run_id1), timeout_s=10.0, poll_s=0.05)
        _wait_until(lambda: _wait_index_contains(run_id1), timeout_s=5.0, poll_s=0.05)
        _wait_until(lambda: _run_completed(client, run_id1), timeout_s=120.0, poll_s=0.1)
        assert _wait_index_absent(run_id1)

        # RunHistoryBundle: single-call history + workflow snapshot reference.
        hb1 = client.get(
            f"/api/gateway/runs/{run_id1}/history_bundle",
            headers=headers,
            params={"include_subruns": "false", "include_session": "false", "ledger_mode": "tail", "ledger_max_items": 50},
        )
        assert hb1.status_code == 200, hb1.text
        b1 = hb1.json()
        assert b1.get("version") == 1
        ws1 = b1.get("workflow_snapshot")
        assert isinstance(ws1, dict)
        assert str(ws1.get("artifact_id") or "").strip()
        assert str(ws1.get("sha256") or "").strip()

        r2 = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": bundle_id, "flow_id": flow_id, "input_data": {"workspace_root": str(sandbox_dir)}},
            headers=headers,
        )
        assert r2.status_code == 200, r2.text
        run_id2 = r2.json()["run_id"]

        _wait_until(lambda: _run_completed(client, run_id2), timeout_s=120.0, poll_s=0.1)

        hb2 = client.get(
            f"/api/gateway/runs/{run_id2}/history_bundle",
            headers=headers,
            params={"include_subruns": "false", "include_session": "false", "ledger_mode": "tail", "ledger_max_items": 50},
        )
        assert hb2.status_code == 200, hb2.text
        b2 = hb2.json()
        assert b2.get("version") == 1
        ws2 = b2.get("workflow_snapshot")
        assert isinstance(ws2, dict)
        assert str(ws2.get("artifact_id") or "").strip()
        assert str(ws2.get("sha256") or "").strip()

    # Offloading assertions: sqlite records must not inline the big content.
    assert db_path.exists()
    conn = sqlite3.connect(str(db_path))
    try:
        row1 = conn.execute("SELECT run_json FROM runs WHERE run_id = ?;", (run_id1,)).fetchone()
        row2 = conn.execute("SELECT run_json FROM runs WHERE run_id = ?;", (run_id2,)).fetchone()
        assert row1 is not None and row2 is not None
        run_json1 = str(row1[0] or "")
        run_json2 = str(row2[0] or "")
        assert "MAGIC" not in run_json1
        assert "MAGIC" not in run_json2

        ledger_rows1 = conn.execute("SELECT record_json FROM ledger WHERE run_id = ?;", (run_id1,)).fetchall()
        ledger_rows2 = conn.execute("SELECT record_json FROM ledger WHERE run_id = ?;", (run_id2,)).fetchall()
        assert ledger_rows1 and ledger_rows2
        assert all("MAGIC" not in str(r[0] or "") for r in ledger_rows1)
        assert all("MAGIC" not in str(r[0] or "") for r in ledger_rows2)
    finally:
        conn.close()

    # Dedupe assertion: the offloaded payload bytes should be stored once at the blob layer.
    blobs_dir = runtime_dir / "artifacts" / "blobs"
    assert blobs_dir.exists()
    blobs = list(blobs_dir.glob("*.bin"))
    assert blobs, "Expected at least one artifact blob to be materialized"

    magic_blobs = [p for p in blobs if b"MAGIC" in p.read_bytes()]
    assert len(magic_blobs) == 1, f"Expected exactly one blob containing MAGIC (deduped), got {len(magic_blobs)}"
