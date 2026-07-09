from __future__ import annotations

from types import SimpleNamespace

from abstractruntime.core.models import RunStatus

from abstractgateway.service import run_summary


def test_run_summary_exposes_bounded_final_output() -> None:
    run = SimpleNamespace(
        run_id="run-1",
        workflow_id="wf",
        status=RunStatus.COMPLETED,
        current_node="end",
        created_at="2026-06-28T00:00:00Z",
        updated_at="2026-06-28T00:00:01Z",
        actor_id="gateway",
        session_id="session-1",
        parent_run_id=None,
        error=None,
        waiting=None,
        vars={},
        output={
            "response": "ok",
            "md_path": "reports/out.md",
            "long": "x" * 60_000,
        },
    )

    summary = run_summary(run)

    assert summary["output"]["response"] == "ok"
    assert summary["output"]["md_path"] == "reports/out.md"
    assert len(summary["output"]["long"]) < 60_000
    assert "#TRUNCATION: output string truncated from 60000 chars" in summary["output"]["long"]
