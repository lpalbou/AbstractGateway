from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.integration


def test_backlog_exec_runner_persists_logs_to_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_STORE_BACKEND", "file")

    gateway_data_dir = tmp_path / "gateway"
    request_id = "00000000-0000-0000-0000-000000000123"
    run_dir = gateway_data_dir / "backlog_exec_runs" / request_id
    run_dir.mkdir(parents=True, exist_ok=True)

    events_text = '{"type":"test","msg":"EVENTS"}\n'
    stderr_text = "STDERR\n"
    last_msg_text = "LAST_MESSAGE\n"
    (run_dir / "codex_events.jsonl").write_text(events_text, encoding="utf-8")
    (run_dir / "codex_stderr.log").write_text(stderr_text, encoding="utf-8")
    (run_dir / "codex_last_message.txt").write_text(last_msg_text, encoding="utf-8")

    from abstractgateway.maintenance.backlog_exec_runner import _store_backlog_exec_logs_to_ledger  # type: ignore
    from abstractruntime.storage.artifacts import FileArtifactStore  # type: ignore

    req = {
        "request_id": request_id,
        "started_at": "2026-02-02T00:00:00+00:00",
        "finished_at": "2026-02-02T00:00:01+00:00",
        "run_dir_relpath": f"backlog_exec_runs/{request_id}",
        "backlog": {"kind": "planned", "filename": "123-framework-test.md", "relpath": "docs/backlog/planned/123-framework-test.md"},
        "executor": {"type": "codex_cli", "version": "v0"},
    }
    result = {"ok": True, "exit_code": 0, "error": None, "started_at": req["started_at"], "finished_at": req["finished_at"]}

    out = _store_backlog_exec_logs_to_ledger(
        gateway_data_dir=gateway_data_dir,
        request_id=request_id,
        req=req,
        prompt="test prompt",
        run_dir=run_dir,
        result=result,
    )

    ledger = out.get("ledger_run_id")
    assert ledger == f"backlog_exec_{request_id}"

    # Run + ledger files exist (file-backed stores).
    assert (gateway_data_dir / f"run_{ledger}.json").exists()
    assert (gateway_data_dir / f"ledger_{ledger}.jsonl").exists()

    artifacts = out.get("log_artifacts") or {}
    events_ref = artifacts.get("events") or {}
    stderr_ref = artifacts.get("stderr") or {}
    last_ref = artifacts.get("last_message") or {}

    events_id = str(events_ref.get("$artifact") or "").strip()
    stderr_id = str(stderr_ref.get("$artifact") or "").strip()
    last_id = str(last_ref.get("$artifact") or "").strip()
    assert events_id and stderr_id and last_id

    store = FileArtifactStore(gateway_data_dir)
    assert store.load_text(events_id) == events_text
    assert store.load_text(stderr_id) == stderr_text
    assert store.load_text(last_id) == last_msg_text
