from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from abstractruntime.core.models import RunStatus


@dataclass
class _Config:
    runner_enabled: bool = True
    data_dir: Path = Path(".")


class _Runner:
    def start(self) -> None:
        return


@dataclass
class _Run:
    status: RunStatus
    output: Dict[str, Any]
    error: str = ""
    waiting: Any = None


class _RunStore:
    def __init__(self) -> None:
        self._runs: Dict[str, _Run] = {}

    def load(self, run_id: str) -> Optional[_Run]:
        return self._runs.get(str(run_id))


class _Host:
    def __init__(self) -> None:
        self.bundles: Dict[str, Any] = {}
        self.run_store = _RunStore()
        self.starts: List[Dict[str, Any]] = []

    def start_run(
        self,
        *,
        flow_id: str,
        bundle_id: Optional[str],
        bundle_version: Optional[str],
        input_data: Dict[str, Any],
        actor_id: str,
        session_id: Optional[str],
    ) -> str:
        del flow_id, bundle_id, bundle_version, actor_id, session_id
        self.starts.append({"input_data": dict(input_data)})
        run_id = f"run-{len(self.starts)}"

        schema = input_data.get("resp_schema") or {}
        required = set(schema.get("required") or [])
        if "draft_markdown" in required:
            resp = {"reply": "ok", "draft_markdown": "# draft\n"}
        else:
            resp = {"reply": "ok"}

        self.run_store._runs[run_id] = _Run(status=RunStatus.COMPLETED, output={"response": json.dumps(resp)})
        return run_id


@dataclass
class _Service:
    config: _Config
    runner: _Runner
    host: _Host
    stores: Any = None


def _make_app(*, monkeypatch: pytest.MonkeyPatch, svc: _Service) -> FastAPI:
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "get_gateway_service", lambda: svc)

    app = FastAPI()
    app.include_router(gateway_routes.router, prefix="/api")
    return app


@pytest.mark.basic
def test_backlog_advisor_readonly_tools_and_allowed_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "docs" / "backlog").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(repo_root))

    gateway_dir = tmp_path / "gateway_data"
    gateway_dir.mkdir(parents=True, exist_ok=True)

    host = _Host()
    svc = _Service(config=_Config(data_dir=gateway_dir), runner=_Runner(), host=host)
    app = _make_app(monkeypatch=monkeypatch, svc=svc)

    with TestClient(app) as client:
        r = client.post("/api/gateway/backlog/advisor", json={"message": "hi", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200
        assert r.json().get("ok") is True

    assert host.starts, "expected advisor to start a run"
    input_data = host.starts[-1]["input_data"]

    # Read-only tool allowlist should include repo inspection tools (and exclude writes/exec).
    tools = list(input_data.get("tools") or [])
    assert "read_file" in tools
    assert "search_files" in tools
    assert "list_files" in tools
    assert "skim_folders" in tools
    assert "skim_files" in tools
    assert "analyze_code" in tools
    assert "write_file" not in tools
    assert "edit_file" not in tools
    assert "execute_command" not in tools

    # Gateway data dir should be available via allowed paths even if outside the repo root.
    assert input_data.get("workspace_access_mode") == "workspace_or_allowed"
    allowed = list(input_data.get("workspace_allowed_paths") or [])
    assert str(gateway_dir.resolve()) in allowed

    # Advisor prompt context should mention gateway_data_dir.
    prompt = str(input_data.get("prompt") or "")
    assert "gateway_data_dir" in prompt


@pytest.mark.basic
def test_backlog_maintain_readonly_tools_and_allowed_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path / "repo"
    backlog_root = repo_root / "docs" / "backlog"
    (backlog_root / "proposed").mkdir(parents=True, exist_ok=True)
    (backlog_root / "template.md").write_text("# {ID}-{Package}: {Title}\n", encoding="utf-8")
    (backlog_root / "proposed" / "672-framework-test.md").write_text("# 672-framework: [TASK] Test\n", encoding="utf-8")
    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(repo_root))

    gateway_dir = tmp_path / "gateway_data"
    gateway_dir.mkdir(parents=True, exist_ok=True)

    host = _Host()
    svc = _Service(config=_Config(data_dir=gateway_dir), runner=_Runner(), host=host)
    app = _make_app(monkeypatch=monkeypatch, svc=svc)

    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/backlog/maintain",
            json={
                "kind": "proposed",
                "filename": "672-framework-test.md",
                "package": "framework",
                "title": "Test",
                "summary": "Test",
                "draft_markdown": "# 672-framework: [TASK] Test\n",
                "messages": [{"role": "user", "content": "please improve"}],
            },
        )
        assert r.status_code == 200
        assert r.json().get("ok") is True

    assert host.starts, "expected maintainer to start a run"
    input_data = host.starts[-1]["input_data"]

    tools = list(input_data.get("tools") or [])
    assert "read_file" in tools
    assert "search_files" in tools
    assert "list_files" in tools
    assert "skim_folders" in tools
    assert "skim_files" in tools
    assert "analyze_code" in tools
    assert "web_search" in tools
    assert "fetch_url" in tools
    assert "write_file" not in tools
    assert "edit_file" not in tools
    assert "execute_command" not in tools

    assert input_data.get("workspace_access_mode") == "workspace_or_allowed"
    allowed = list(input_data.get("workspace_allowed_paths") or [])
    assert str(gateway_dir.resolve()) in allowed

