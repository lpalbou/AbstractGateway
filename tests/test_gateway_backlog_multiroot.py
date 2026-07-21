"""Multi-root backlog serving (continuum c3583; laurent dm#110 "I must have
proper access to everything in the board"): the read routes fold the umbrella
+ every seat repo carrying a docs/backlog; `package` is the repo DIRECTORY
name (one authority — file-header labels diverge); content resolves across
roots with `?package=` as the collision disambiguator. Write/exec lanes stay
umbrella-scoped deliberately."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


_ITEM = """# 001-{pkg}: [task] A {pkg} item

> Type: task
> Priority: P2

## Summary

A real summary line for {pkg}.

## Acceptance Criteria

- [ ] serves across roots

## Testing

- `pytest -q`
"""


def _make_workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    # The umbrella's own backlog.
    (root / "docs" / "backlog" / "planned").mkdir(parents=True)
    (root / "docs" / "backlog" / "planned" / "001_workspace_item.md").write_text(
        _ITEM.format(pkg="workspace"), encoding="utf-8"
    )
    # Two seat repos with their own backlogs; one carries a COLLIDING filename.
    for pkg in ("alphapkg", "betapkg"):
        d = root / pkg / "docs" / "backlog" / "planned"
        d.mkdir(parents=True)
        (d / f"001_{pkg}_item.md").write_text(_ITEM.format(pkg=pkg), encoding="utf-8")
    (root / "alphapkg" / "docs" / "backlog" / "planned" / "shared_name.md").write_text(
        "alpha's shared\n", encoding="utf-8"
    )
    (root / "betapkg" / "docs" / "backlog" / "planned" / "shared_name.md").write_text(
        "beta's shared\n", encoding="utf-8"
    )
    # A repo WITHOUT a backlog must not become a root.
    (root / "nobacklog").mkdir()
    return root


def _app(monkeypatch: pytest.MonkeyPatch, root: Path) -> FastAPI:
    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(root))
    monkeypatch.delenv("ABSTRACTGATEWAY_BACKLOG_ROOTS", raising=False)
    from abstractgateway.routes import gateway as gateway_routes

    app = FastAPI()
    app.include_router(gateway_routes.router, prefix="/api")
    return app


def test_list_folds_all_roots_with_directory_name_packages(tmp_path, monkeypatch) -> None:
    root = _make_workspace(tmp_path)
    with TestClient(_app(monkeypatch, root)) as client:
        r = client.get("/api/gateway/backlog/planned")
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        by_file = {i["filename"]: i for i in items}
        # All three repos' items serve; package = repo DIRECTORY name.
        assert by_file["001_workspace_item.md"]["package"] == "workspace"
        assert by_file["001_alphapkg_item.md"]["package"] == "alphapkg"
        assert by_file["001_betapkg_item.md"]["package"] == "betapkg"
        # The colliding basename appears once per root, distinguishable.
        shared = [i for i in items if i["filename"] == "shared_name.md"]
        assert {i["package"] for i in shared} == {"alphapkg", "betapkg"}


def test_content_resolves_across_roots_and_package_disambiguates(tmp_path, monkeypatch) -> None:
    root = _make_workspace(tmp_path)
    with TestClient(_app(monkeypatch, root)) as client:
        # A seat-repo file no longer 404s (the c3583 incident).
        r = client.get("/api/gateway/backlog/planned/001_alphapkg_item.md/content")
        assert r.status_code == 200, r.text
        assert "alphapkg" in r.json()["content"]

        # Collision without a disambiguator: first root in order wins
        # (documented); with ?package= each side is reachable exactly.
        ra = client.get("/api/gateway/backlog/planned/shared_name.md/content?package=alphapkg")
        rb = client.get("/api/gateway/backlog/planned/shared_name.md/content?package=betapkg")
        assert "alpha's shared" in ra.json()["content"]
        assert "beta's shared" in rb.json()["content"]

        # Unknown package refuses honestly.
        assert client.get("/api/gateway/backlog/planned/shared_name.md/content?package=ghost").status_code == 404
        # Missing everywhere = 404 naming the multi-root search.
        r404 = client.get("/api/gateway/backlog/planned/never_written.md/content")
        assert r404.status_code == 404 and "any backlog root" in r404.json()["detail"]


def test_explicit_roots_env_overrides_discovery(tmp_path, monkeypatch) -> None:
    root = _make_workspace(tmp_path)
    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(root))
    monkeypatch.setenv("ABSTRACTGATEWAY_BACKLOG_ROOTS", str(root / "alphapkg"))
    from abstractgateway.routes import gateway as gateway_routes

    app = FastAPI()
    app.include_router(gateway_routes.router, prefix="/api")
    with TestClient(app) as client:
        items = client.get("/api/gateway/backlog/planned").json()["items"]
        assert {i["package"] for i in items} == {"alphapkg"}, "explicit roots list is the whole scope"
