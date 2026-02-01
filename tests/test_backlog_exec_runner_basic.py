from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.basic
def test_backlog_exec_runner_processes_queue_and_writes_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.maintenance.backlog_exec_runner import BacklogExecRunnerConfig, process_next_backlog_exec_request

    gateway_dir = tmp_path / "gateway"
    queue_dir = gateway_dir / "backlog_exec_queue"
    queue_dir.mkdir(parents=True, exist_ok=True)

    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)

    request_id = "req1"
    req_path = queue_dir / f"{request_id}.json"
    req_obj = {
        "created_at": "2026-01-31T00:00:00+00:00",
        "request_id": request_id,
        "status": "queued",
        "backlog": {"kind": "planned", "filename": "001-framework-example.md", "relpath": "docs/backlog/planned/001-framework-example.md"},
        "prompt": "hello world",
        "target_agent": "codex:gpt-5.2",
    }
    req_path.write_text(json.dumps(req_obj, indent=2) + "\n", encoding="utf-8")

    seen_cmd = {"cmd": None}

    def fake_run(cmd, stdout=None, stderr=None, cwd=None, check=None, timeout=None, stdin=None, **_kw):  # type: ignore
        seen_cmd["cmd"] = list(cmd) if isinstance(cmd, (list, tuple)) else cmd
        if stdout is not None:
            stdout.write(b'{"event":"final"}\n')
        if stderr is not None:
            stderr.write(b"")
        # Write last-message file (argument after --output-last-message).
        try:
            idx = cmd.index("--output-last-message")
            out_path = Path(cmd[idx + 1])
            out_path.write_text("done", encoding="utf-8")
        except Exception:
            pass
        return SimpleNamespace(returncode=0)

    import abstractgateway.maintenance.backlog_exec_runner as mod

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    cfg = BacklogExecRunnerConfig(enabled=True, poll_interval_s=0.01, executor="codex_cli", notify=False, codex_model="gpt-5.2-xhigh")
    processed, rid = process_next_backlog_exec_request(gateway_data_dir=gateway_dir, repo_root=repo_root, cfg=cfg)
    assert processed is True
    assert rid == request_id

    cmd = seen_cmd["cmd"]
    assert isinstance(cmd, list)
    assert "--ask-for-approval" not in cmd
    midx = cmd.index("--model")
    assert cmd[midx + 1] == "gpt-5.2"
    assert cmd[-2] == "--"
    assert cmd[-1] == "hello world"

    updated = json.loads(req_path.read_text(encoding="utf-8"))
    assert updated["status"] == "completed"
    assert updated.get("started_at")
    assert updated.get("finished_at")
    assert updated.get("run_dir_relpath") == f"backlog_exec_runs/{request_id}"
    assert updated.get("result", {}).get("ok") is True
    assert updated.get("result", {}).get("last_message") == "done"

    run_dir = gateway_dir / "backlog_exec_runs" / request_id
    assert (run_dir / "codex_events.jsonl").exists()
    assert (run_dir / "codex_last_message.txt").exists()
    assert (run_dir / "codex_stderr.log").exists()

    assert not (queue_dir / f"{request_id}.lock").exists()
