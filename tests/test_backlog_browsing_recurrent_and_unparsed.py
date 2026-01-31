from __future__ import annotations

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

    app = FastAPI()
    app.include_router(gateway_routes.router, prefix="/api")
    return app


def _write_backlog_item(path: Path, *, item_id: int, pkg: str, title: str) -> None:
    path.write_text(
        "\n".join(
            [
                f"# {item_id}-{pkg}: {title}",
                "",
                "> Created: 2026-01-31 00:00:00 +0000",
                "",
                "## Summary",
                "Test item.",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.basic
def test_backlog_endpoints_support_recurrent_and_unparsed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway_dir = tmp_path / "gateway"
    gateway_dir.mkdir(parents=True, exist_ok=True)

    repo_root = tmp_path / "repo"
    backlog_root = repo_root / "docs" / "backlog"
    (backlog_root / "planned").mkdir(parents=True, exist_ok=True)
    (backlog_root / "completed").mkdir(parents=True, exist_ok=True)
    (backlog_root / "proposed").mkdir(parents=True, exist_ok=True)
    (backlog_root / "recurrent").mkdir(parents=True, exist_ok=True)

    _write_backlog_item(backlog_root / "recurrent" / "700-framework-example-recurrent.md", item_id=700, pkg="framework", title="Example recurrent")
    # Write an unparsed backlog file (doesn't match required # {id}-{pkg}: {title} format).
    (backlog_root / "planned" / "unparsed.md").write_text("hello\n", encoding="utf-8")

    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(repo_root))

    app = _make_app(monkeypatch=monkeypatch, gateway_base_dir=gateway_dir)
    with TestClient(app) as client:
        recurrent = client.get("/api/gateway/backlog/recurrent")
        assert recurrent.status_code == 200
        items = recurrent.json()["items"]
        assert any(i["filename"] == "700-framework-example-recurrent.md" and i["item_id"] == 700 for i in items)

        planned = client.get("/api/gateway/backlog/planned")
        assert planned.status_code == 200
        pitems = planned.json()["items"]
        unparsed = next((i for i in pitems if i["filename"] == "unparsed.md"), None)
        assert unparsed is not None
        assert unparsed["item_id"] == 0
        assert unparsed.get("parsed") is False

        content = client.get("/api/gateway/backlog/recurrent/700-framework-example-recurrent.md/content")
        assert content.status_code == 200
        assert "# 700-framework: Example recurrent" in content.json()["content"]

        raw = client.get("/api/gateway/backlog/planned/unparsed.md/content")
        assert raw.status_code == 200
        assert raw.json()["content"].strip() == "hello"

