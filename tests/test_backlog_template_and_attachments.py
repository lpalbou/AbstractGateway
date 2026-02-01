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

    app = FastAPI()
    app.include_router(gateway_routes.router, prefix="/api")
    return app


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
                "## Related",
                "- ADRs:",
                "- Code:",
                "- Reports:",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.basic
def test_backlog_template_and_attachments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        templ = client.get("/api/gateway/backlog/template")
        assert templ.status_code == 200
        body = templ.json()
        assert body["relpath"] == "docs/backlog/template.md"
        assert "## Summary" in body["content"]
        assert isinstance(body["sha256"], str) and len(body["sha256"]) == 64

        created = client.post("/api/gateway/backlog/create", json={"kind": "proposed", "package": "framework", "title": "Example task"})
        assert created.status_code == 200
        created_body = created.json()
        filename = created_body["filename"]
        item_id = int(created_body["item_id"])
        pid = f"{item_id:03d}"

        # Upload attachment (name sanitization + collision suffixing).
        payload = b"hello"
        sha = hashlib.sha256(payload).hexdigest()
        files = {"file": ("screenshot 1.png", payload, "image/png")}
        up = client.post(f"/api/gateway/backlog/proposed/{filename}/attachments/upload", files=files)
        assert up.status_code == 200
        up_body = up.json()
        assert up_body["item_id"] == item_id
        stored = up_body["stored"]
        assert stored["sha256"] == sha
        assert stored["relpath"].startswith(f"docs/backlog/assets/{pid}/")
        assert stored["filename"] == "screenshot_1.png"

        on_disk = repo_root / stored["relpath"]
        assert on_disk.exists()
        assert on_disk.read_bytes() == payload

        # Second upload with same name should suffix.
        up2 = client.post(f"/api/gateway/backlog/proposed/{filename}/attachments/upload", files=files)
        assert up2.status_code == 200
        stored2 = up2.json()["stored"]
        assert stored2["filename"].startswith("screenshot_1-2")
        on_disk2 = repo_root / stored2["relpath"]
        assert on_disk2.exists()

