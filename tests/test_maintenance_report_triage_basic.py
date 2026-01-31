from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.basic
def test_triage_reports_creates_decisions_and_detects_duplicates(tmp_path: Path) -> None:
    from abstractgateway.maintenance.triage import triage_reports
    from abstractgateway.maintenance.triage_queue import decisions_dir, iter_decisions

    # Fake gateway data dir with a single bug report.
    gw_dir = tmp_path / "gateway"
    bug_dir = gw_dir / "bug_reports"
    bug_dir.mkdir(parents=True, exist_ok=True)

    bug = bug_dir / "2026-01-31_something-broken.md"
    bug.write_text(
        "\n".join(
            [
                "# Bug: Something broken in AbstractCode Web",
                "",
                "> Created: 2026-01-31T12:00:00Z",
                "> Bug ID: bug-1",
                "> Session ID: session-1",
                "> Session memory run ID: mem-1",
                "> Relevant run ID: run-1",
                "> Workflow ID: wf-1",
                "",
                "## User Description",
                "    Clicking X crashes the page.",
                "",
                "## Impact",
                "- Who is affected?",
                "- How bad is it? (data loss, wrong answer, UX friction, security risk, etc)",
                "",
                "## Steps to Reproduce",
                "1.",
                "2.",
                "",
                "## Extra Context (JSON)",
                "```json",
                '{"client":"abstractcode/web","url":"https://example"}',
                "```",
                "",
            ]
        ),
        encoding="utf-8",
    )

    # Fake repo with backlog dirs and one planned item that should be similar.
    repo_root = tmp_path / "repo"
    (repo_root / "docs" / "backlog" / "planned").mkdir(parents=True, exist_ok=True)
    (repo_root / "docs" / "backlog" / "completed").mkdir(parents=True, exist_ok=True)
    (repo_root / "docs" / "backlog" / "proposed").mkdir(parents=True, exist_ok=True)
    (repo_root / "docs" / "backlog" / "README.md").write_text("# Backlog\n", encoding="utf-8")

    planned_item = repo_root / "docs" / "backlog" / "planned" / "900-abstractcode-fix-something-broken.md"
    planned_item.write_text(
        "\n".join(
            [
                "# 900-abstractcode: Fix something broken in AbstractCode Web",
                "",
                "> Created: 2026-01-01 00:00:00 +0000",
                "",
                "## Summary",
                "Fix a crash when clicking X.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    out = triage_reports(gateway_data_dir=gw_dir, repo_root=repo_root, write_drafts=False, enable_llm=False)
    assert out["reports"] == 1

    qdir = decisions_dir(gateway_data_dir=gw_dir)
    decisions = iter_decisions(qdir)
    assert len(decisions) == 1

    d = decisions[0]
    assert d.report_relpath.endswith("bug_reports/2026-01-31_something-broken.md")
    assert d.missing_fields  # template placeholders should be detected
    assert any(c.kind == "backlog_planned" for c in d.duplicates)


@pytest.mark.basic
def test_triage_action_endpoint_approve_writes_draft(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from abstractgateway.maintenance.action_tokens import build_action_payload, sign_action_token
    from abstractgateway.maintenance.triage_queue import decisions_dir, iter_decisions
    from abstractgateway.maintenance.triage import triage_reports
    from abstractgateway.routes.triage import router as triage_router

    secret = "test-secret"
    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_ACTION_SECRET", secret)

    gw_dir = tmp_path / "gateway"
    bug_dir = gw_dir / "bug_reports"
    bug_dir.mkdir(parents=True, exist_ok=True)
    (gw_dir / "feature_requests").mkdir(parents=True, exist_ok=True)

    bug = bug_dir / "2026-01-31_click-crash.md"
    bug.write_text(
        "\n".join(
            [
                "# Bug: Click crash",
                "",
                "> Created: 2026-01-31T12:00:00Z",
                "> Bug ID: bug-2",
                "> Session ID: session-2",
                "> Session memory run ID: mem-2",
                "> Relevant run ID: run-2",
                "> Workflow ID: wf-2",
                "",
                "## User Description",
                "    Crash on click.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    repo_root = tmp_path / "repo"
    (repo_root / "docs" / "backlog" / "planned").mkdir(parents=True, exist_ok=True)
    (repo_root / "docs" / "backlog" / "completed").mkdir(parents=True, exist_ok=True)
    (repo_root / "docs" / "backlog" / "proposed").mkdir(parents=True, exist_ok=True)
    (repo_root / "docs" / "backlog" / "README.md").write_text("# Backlog\n", encoding="utf-8")

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(gw_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(repo_root))

    triage_reports(gateway_data_dir=gw_dir, repo_root=repo_root, write_drafts=False, enable_llm=False)
    qdir = decisions_dir(gateway_data_dir=gw_dir)
    decisions = iter_decisions(qdir)
    assert len(decisions) == 1
    did = decisions[0].decision_id

    token = sign_action_token(payload=build_action_payload(decision_id=did, action="approve", ttl_s=3600), secret=secret)

    app = FastAPI()
    app.include_router(triage_router, prefix="/api")
    client = TestClient(app)

    # Preview is safe (no mutation).
    r = client.get(f"/api/triage/action/{token}")
    assert r.status_code == 200
    assert did in r.text

    # Apply approval (writes draft).
    r2 = client.post(f"/api/triage/action/{token}")
    assert r2.status_code == 200

    decisions2 = iter_decisions(qdir)
    assert decisions2[0].status == "approved"
    assert decisions2[0].draft_relpath
    draft_path = repo_root / decisions2[0].draft_relpath
    assert draft_path.exists()

