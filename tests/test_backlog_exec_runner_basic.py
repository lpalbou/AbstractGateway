from __future__ import annotations

import json
import threading
import time
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

    cfg = BacklogExecRunnerConfig(
        enabled=True,
        poll_interval_s=0.01,
        executor="codex_cli",
        notify=False,
        codex_model="gpt-5.2-xhigh",
        exec_mode_default="inplace",
    )
    processed, rid = process_next_backlog_exec_request(gateway_data_dir=gateway_dir, repo_root=repo_root, cfg=cfg)
    assert processed is True
    assert rid == request_id

    cmd = seen_cmd["cmd"]
    assert isinstance(cmd, list)
    assert "--ask-for-approval" not in cmd
    midx = cmd.index("--model")
    assert cmd[midx + 1] == "gpt-5.2"
    cidx = cmd.index("-c")
    assert cmd[cidx + 1] == 'model_reasoning_effort="xhigh"'
    assert cmd[-2] == "--"
    assert cmd[-1] == "hello world"

    updated = json.loads(req_path.read_text(encoding="utf-8"))
    assert updated["status"] == "awaiting_qa"
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


@pytest.mark.basic
def test_backlog_exec_runner_can_execute_multiple_requests_in_parallel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.maintenance.backlog_exec_runner import BacklogExecRunner, BacklogExecRunnerConfig

    gateway_dir = tmp_path / "gateway"
    queue_dir = gateway_dir / "backlog_exec_queue"
    queue_dir.mkdir(parents=True, exist_ok=True)

    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(repo_root))

    def _write_req(request_id: str) -> Path:
        req_path = queue_dir / f"{request_id}.json"
        req_obj = {
            "created_at": "2026-02-02T00:00:00+00:00",
            "request_id": request_id,
            "status": "queued",
            "backlog": {
                "kind": "planned",
                "filename": f"{request_id}-framework-example.md",
                "relpath": f"docs/backlog/planned/{request_id}-framework-example.md",
            },
            "prompt": f"hello {request_id}",
            "target_agent": "codex:gpt-5.2",
        }
        req_path.write_text(json.dumps(req_obj, indent=2) + "\n", encoding="utf-8")
        return req_path

    r1 = _write_req("req1")
    r2 = _write_req("req2")

    first_started = threading.Event()
    second_started = threading.Event()
    allow_finish = threading.Event()
    counter_lock = threading.Lock()
    call_count = 0

    def fake_run(cmd, stdout=None, stderr=None, cwd=None, check=None, timeout=None, stdin=None, **_kw):  # type: ignore
        nonlocal call_count
        with counter_lock:
            call_count += 1
            idx = call_count
        if idx == 1:
            first_started.set()
        if idx == 2:
            second_started.set()

        if not allow_finish.wait(timeout=5.0):
            raise RuntimeError("Timed out waiting for allow_finish")

        if stdout is not None:
            stdout.write(b'{"event":"final"}\n')
        if stderr is not None:
            stderr.write(b"")
        try:
            midx = cmd.index("--output-last-message")
            out_path = Path(cmd[midx + 1])
            out_path.write_text("done", encoding="utf-8")
        except Exception:
            pass
        return SimpleNamespace(returncode=0)

    import abstractgateway.maintenance.backlog_exec_runner as mod

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    cfg = BacklogExecRunnerConfig(enabled=True, poll_interval_s=0.01, workers=2, executor="codex_cli", notify=False, exec_mode_default="inplace")
    runner = BacklogExecRunner(gateway_data_dir=gateway_dir, cfg=cfg)
    runner.start()
    try:
        assert first_started.wait(timeout=2.0)
        assert second_started.wait(timeout=2.0), "Expected a second concurrent execution worker to start"
        allow_finish.set()

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            s1 = json.loads(r1.read_text(encoding="utf-8")).get("status")
            s2 = json.loads(r2.read_text(encoding="utf-8")).get("status")
            if s1 == "awaiting_qa" and s2 == "awaiting_qa":
                break
            time.sleep(0.02)

        assert json.loads(r1.read_text(encoding="utf-8")).get("status") == "awaiting_qa"
        assert json.loads(r2.read_text(encoding="utf-8")).get("status") == "awaiting_qa"
    finally:
        allow_finish.set()
        runner.stop()


@pytest.mark.basic
def test_candidate_backlog_cleanup_deletes_planned_when_completed_exists(tmp_path: Path) -> None:
    from abstractgateway.maintenance.backlog_exec_runner import _maybe_fix_backlog_move_in_candidate

    candidate_root = tmp_path / "candidate"
    planned_dir = candidate_root / "docs" / "backlog" / "planned"
    completed_dir = candidate_root / "docs" / "backlog" / "completed"
    planned_dir.mkdir(parents=True, exist_ok=True)
    completed_dir.mkdir(parents=True, exist_ok=True)

    planned = planned_dir / "720-foo.md"
    planned.write_text("# 720-framework: [TASK] Foo\n", encoding="utf-8")
    completed = completed_dir / "720-bar.md"
    completed.write_text("# 720-framework: [TASK] Bar\n", encoding="utf-8")

    req = {"backlog": {"relpath": "docs/backlog/planned/720-foo.md"}}
    out = _maybe_fix_backlog_move_in_candidate(candidate_root=candidate_root, req=req)
    assert out.get("ok") is True
    assert out.get("action") == "deleted_planned_duplicate"
    assert not planned.exists()

    # No-op when no completed file exists.
    planned2 = planned_dir / "721-foo.md"
    planned2.write_text("# 721-framework: [TASK] Foo\n", encoding="utf-8")
    req2 = {"backlog": {"relpath": "docs/backlog/planned/721-foo.md"}}
    out2 = _maybe_fix_backlog_move_in_candidate(candidate_root=candidate_root, req=req2)
    assert out2.get("ok") is False
    assert planned2.exists()
