from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

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
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "get_gateway_service", lambda: _Service(stores=_Stores(base_dir=gateway_base_dir)))

    # Patch LLM-backed assist to stay deterministic in unit tests.
    monkeypatch.setattr(
        gateway_routes,
        "_generate_backlog_assist_json",
        lambda **_kwargs: {"reply": "ok", "draft_markdown": "# draft\n\n> Created: now\n"},
    )

    app = FastAPI()
    app.include_router(gateway_routes.router, prefix="/api")
    return app


def _sha256_hex(text: str) -> str:
    h = hashlib.sha256()
    h.update(text.encode("utf-8", errors="ignore"))
    return h.hexdigest()


def _write_backlog_template(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                "# {ID}-{Package}: {Title}",
                "",
                "> Created: YYYY-MM-DD HH:MM:SS +/-TZ",
                "",
                "## Summary",
                "One paragraph describing what this task accomplishes (user value + outcome).",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.basic
def test_backlog_create_move_update_execute_and_assist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway_dir = tmp_path / "gateway"
    gateway_dir.mkdir(parents=True, exist_ok=True)

    repo_root = tmp_path / "repo"
    backlog_root = repo_root / "docs" / "backlog"
    for d in ["planned", "completed", "proposed", "recurrent", "deprecated", "trash"]:
        (backlog_root / d).mkdir(parents=True, exist_ok=True)
    _write_backlog_template(backlog_root / "template.md")

    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(repo_root))

    app = _make_app(monkeypatch=monkeypatch, gateway_base_dir=gateway_dir)
    with TestClient(app) as client:
        created = client.post(
            "/api/gateway/backlog/create",
            json={"kind": "proposed", "package": "framework", "title": "Example task", "summary": "Hello"},
        )
        assert created.status_code == 200
        body = created.json()
        assert body["kind"] == "proposed"
        filename = body["filename"]
        assert isinstance(filename, str) and filename.endswith(".md")
        item_id = int(body["item_id"])
        assert item_id > 0

        content_resp = client.get(f"/api/gateway/backlog/proposed/{filename}/content")
        assert content_resp.status_code == 200
        content = content_resp.json()["content"]
        assert f"# {item_id:03d}-framework: [TASK] Example task" in content
        assert "> Type: task" in content

        bad_update = client.post(
            f"/api/gateway/backlog/proposed/{filename}/update",
            json={"content": content + "\nextra\n", "expected_sha256": "deadbeef"},
        )
        assert bad_update.status_code == 409

        ok_update = client.post(
            f"/api/gateway/backlog/proposed/{filename}/update",
            json={"content": content + "\nextra\n", "expected_sha256": _sha256_hex(content)},
        )
        assert ok_update.status_code == 200
        assert ok_update.json()["sha256"] == _sha256_hex(content + "\nextra\n")

        moved = client.post("/api/gateway/backlog/move", json={"from_kind": "proposed", "to_kind": "planned", "filename": filename})
        assert moved.status_code == 200
        assert moved.json()["to_relpath"].endswith(f"/planned/{filename}")

        # Execute queues a request file under gateway data dir.
        exec_resp = client.post(f"/api/gateway/backlog/planned/{filename}/execute")
        assert exec_resp.status_code == 200
        rid = exec_resp.json()["request_id"]
        assert isinstance(rid, str) and rid
        qpath = gateway_dir / "backlog_exec_queue" / f"{rid}.json"
        assert qpath.exists()

        assist = client.post(
            "/api/gateway/backlog/assist",
            json={"kind": "proposed", "package": "framework", "title": "Example task", "summary": "Hello", "messages": [{"role": "user", "content": "help"}]},
        )
        assert assist.status_code == 200
        assert assist.json()["reply"] == "ok"
        assert "draft" in assist.json()["draft_markdown"]
