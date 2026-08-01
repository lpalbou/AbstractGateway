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



def test_the_triage_assistant_reads_the_settings_abstractcore_stores(monkeypatch) -> None:
    """One assistant, one store.

    AbstractCore holds the triage LLM's provider settings in its `maintenance`
    config section. An operator who set the model there has configured this
    assistant; requiring the same six values again as Gateway environment
    variables would make the two entry points disagree about one feature.
    """
    import abstractgateway.maintenance.llm_assist as llm_assist

    for name in (
        "ABSTRACT_TRIAGE_LLM",
        "ABSTRACTGATEWAY_TRIAGE_LLM",
        "ABSTRACT_TRIAGE_LLM_BASE_URL",
        "ABSTRACTGATEWAY_TRIAGE_LLM_BASE_URL",
        "ABSTRACT_TRIAGE_LLM_MODEL",
        "ABSTRACTGATEWAY_TRIAGE_LLM_MODEL",
        "ABSTRACT_TRIAGE_LLM_TEMPERATURE",
        "ABSTRACTGATEWAY_TRIAGE_LLM_TEMPERATURE",
        "ABSTRACT_TRIAGE_LLM_MAX_TOKENS",
        "ABSTRACTGATEWAY_TRIAGE_LLM_MAX_TOKENS",
        "ABSTRACT_TRIAGE_LLM_TIMEOUT_S",
        "ABSTRACTGATEWAY_TRIAGE_LLM_TIMEOUT_S",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setattr(
        llm_assist,
        "_core_maintenance_settings",
        lambda: {
            "triage_llm_enabled": True,
            "triage_llm_base_url": "http://127.0.0.1:1234",
            "triage_llm_model": "qwen/qwen3-next-80b",
            "triage_llm_temperature": 0.4,
            "triage_llm_max_tokens": 512,
            "triage_llm_timeout_s": 12.0,
        },
    )

    config = llm_assist.load_llm_assist_config()
    assert config["enabled"] is True
    assert config["base_url"] == "http://127.0.0.1:1234"
    assert config["model"] == "qwen/qwen3-next-80b"
    assert config["temperature"] == 0.4
    assert config["max_tokens"] == 512
    assert config["timeout_s"] == 12.0


def test_a_gateway_environment_variable_still_overrides_the_stored_triage_model(monkeypatch) -> None:
    """The environment stays the override rung, not the only rung."""
    import abstractgateway.maintenance.llm_assist as llm_assist

    monkeypatch.setenv("ABSTRACT_TRIAGE_LLM_MODEL", "override/model")
    monkeypatch.delenv("ABSTRACTGATEWAY_TRIAGE_LLM_MODEL", raising=False)
    monkeypatch.setattr(
        llm_assist,
        "_core_maintenance_settings",
        lambda: {"triage_llm_enabled": True, "triage_llm_model": "stored/model"},
    )

    assert llm_assist.load_llm_assist_config()["model"] == "override/model"
