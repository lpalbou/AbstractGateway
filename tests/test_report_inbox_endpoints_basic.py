from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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
    # Patch gateway service discovery to avoid starting the real runner/service in unit tests.
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "get_gateway_service", lambda: _Service(stores=_Stores(base_dir=gateway_base_dir)))

    app = FastAPI()
    app.include_router(gateway_routes.router, prefix="/api")
    return app


def _write_bug_report(path: Path, *, title: str) -> None:
    path.write_text(
        "\n".join(
            [
                f"# Bug: {title}",
                "",
                "> Created: 2026-01-31T12:00:00Z",
                "> Bug ID: bug-1",
                "> Session ID: session-1",
                "> Session memory run ID: mem-1",
                "> Relevant run ID: run-1",
                "> Workflow ID: wf-1",
                "",
                "## User Description",
                "    Something is wrong.",
                "",
                "## Impact",
                "- Who is affected?",
                "- How bad is it? (data loss, wrong answer, UX friction, security risk, etc)",
                "",
                "## Steps to Reproduce",
                "1.",
                "2.",
                "",
                "## Expected Behavior",
                "",
                "## Actual Behavior",
                "",
                "## Reproducibility",
                "- [ ] always",
                "- [ ] often",
                "- [ ] sometimes",
                "- [ ] once",
                "- [ ] unable to reproduce yet",
                "",
                "## Severity",
                "- [ ] blocker",
                "- [ ] major",
                "- [ ] minor",
                "- [ ] polish",
                "",
                "## Workaround",
                "(if any)",
                "",
                "## Extra Context (JSON)",
                "```json",
                '{"client":"abstractobserver/web"}',
                "```",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.basic
def test_report_inbox_and_triage_endpoints_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway_dir = tmp_path / "gateway"
    (gateway_dir / "bug_reports").mkdir(parents=True, exist_ok=True)
    (gateway_dir / "feature_requests").mkdir(parents=True, exist_ok=True)

    report_filename = "2026-01-31_example-bug.md"
    _write_bug_report(gateway_dir / "bug_reports" / report_filename, title="Example bug")

    # Minimal repo root for backlog browsing (used by later endpoints, even if write_drafts is false).
    repo_root = tmp_path / "repo"
    (repo_root / "docs" / "backlog" / "planned").mkdir(parents=True, exist_ok=True)
    (repo_root / "docs" / "backlog" / "completed").mkdir(parents=True, exist_ok=True)
    (repo_root / "docs" / "backlog" / "proposed").mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(repo_root))

    app = _make_app(monkeypatch=monkeypatch, gateway_base_dir=gateway_dir)
    with TestClient(app) as client:
        triage = client.post("/api/gateway/triage/run", json={"write_drafts": False, "enable_llm": False})
        assert triage.status_code == 200
        body = triage.json()
        assert body["reports"] == 1
        assert body["updated_decisions"] == 1

        decisions = client.get("/api/gateway/triage/decisions?status=pending")
        assert decisions.status_code == 200
        dec = decisions.json()
        assert len(dec["decisions"]) == 1
        decision_id = dec["decisions"][0]["decision_id"]
        assert isinstance(decision_id, str) and decision_id

        bugs = client.get("/api/gateway/reports/bugs")
        assert bugs.status_code == 200
        items = bugs.json()["items"]
        assert len(items) == 1
        assert items[0]["filename"] == report_filename
        assert items[0]["decision_id"] == decision_id
        assert items[0]["triage_status"] == "pending"

        content = client.get(f"/api/gateway/reports/bugs/{report_filename}/content")
        assert content.status_code == 200
        assert "## User Description" in content.json()["content"]


@pytest.mark.basic
def test_triage_approve_writes_draft_and_backlog_is_browsable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway_dir = tmp_path / "gateway"
    (gateway_dir / "bug_reports").mkdir(parents=True, exist_ok=True)
    (gateway_dir / "feature_requests").mkdir(parents=True, exist_ok=True)

    report_filename = "2026-01-31_example-bug.md"
    _write_bug_report(gateway_dir / "bug_reports" / report_filename, title="Example bug")

    repo_root = tmp_path / "repo"
    backlog_root = repo_root / "docs" / "backlog"
    (backlog_root / "planned").mkdir(parents=True, exist_ok=True)
    (backlog_root / "completed").mkdir(parents=True, exist_ok=True)
    (backlog_root / "proposed").mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(repo_root))

    app = _make_app(monkeypatch=monkeypatch, gateway_base_dir=gateway_dir)
    with TestClient(app) as client:
        triage = client.post("/api/gateway/triage/run", json={"write_drafts": False, "enable_llm": False})
        assert triage.status_code == 200

        decisions = client.get("/api/gateway/triage/decisions?status=pending")
        assert decisions.status_code == 200
        decision_id = decisions.json()["decisions"][0]["decision_id"]

        applied = client.post(f"/api/gateway/triage/decisions/{decision_id}/apply", json={"action": "approve"})
        assert applied.status_code == 200
        out = applied.json()
        assert out["status"] == "approved"
        draft_relpath = str(out.get("draft_relpath") or "").strip()
        assert draft_relpath

        proposed = client.get("/api/gateway/backlog/proposed")
        assert proposed.status_code == 200
        items = proposed.json()["items"]
        assert any(i["filename"] == Path(draft_relpath).name for i in items)

        draft_name = Path(draft_relpath).name
        draft = client.get(f"/api/gateway/backlog/proposed/{draft_name}/content")
        assert draft.status_code == 200
        assert "Source report:" in draft.json()["content"]

