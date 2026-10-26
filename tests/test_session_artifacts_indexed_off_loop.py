"""GET /sessions/{id}/artifacts (and the other artifact listings) look a session up through the
artifact store's indexes and run off the event loop (2026-10-26: the route read all 106k
artifacts ON the loop, 2 s per call, and the watchdog restarted the gateway twice)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "bundles").mkdir()
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "bundles"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def test_session_artifacts_use_the_index_off_the_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env(tmp_path, monkeypatch)
    from abstractgateway.app import app
    from abstractruntime.storage.artifacts import FileArtifactStore

    seen = {"candidates_on_loop": [], "list_all": 0}
    real_candidates = FileArtifactStore.list_session_candidates

    def candidates(self, *a, **kw):
        seen["candidates_on_loop"].append(_on_loop())
        return real_candidates(self, *a, **kw)

    def list_all(self, *a, **kw):  # the full-catalog read this route must never make
        seen["list_all"] += 1
        raise AssertionError("session artifacts read the whole catalog")

    monkeypatch.setattr(FileArtifactStore, "list_session_candidates", candidates)
    # The catalog's session indexes are built by the first lookup (0.14-0.28 s at 106k
    # artifacts), never by the store's open: that first build must run off the loop too.
    builds_on_loop = []
    real_ensure = FileArtifactStore._ensure_session_indexes

    def ensure(self):
        if not self._session_indexes_ready:
            builds_on_loop.append(_on_loop())
        return real_ensure(self)

    monkeypatch.setattr(FileArtifactStore, "_ensure_session_indexes", ensure)
    monkeypatch.setattr(FileArtifactStore, "list_all", list_all)

    headers = {"Authorization": "Bearer t"}
    with TestClient(app) as client:
        store = FileArtifactStore(tmp_path / "runtime")
        upload = store.store(b"u", content_type="text/plain", run_id="session_memory_s1").artifact_id
        tagged = store.store(b"t", content_type="text/plain", run_id="run-a", tags={"session_id": "s1"}).artifact_id
        shared = store.store(b"s", content_type="text/plain", run_id="run-b", tags={"shared": "user"}).artifact_id
        foreign = store.store(b"o", content_type="text/plain", run_id="run-c", tags={"session_id": "s2"}).artifact_id
        foreign_shared = store.store(b"f", content_type="text/plain", run_id="run-d", tags={"shared": "nobody"}).artifact_id

        r = client.get("/api/gateway/sessions/s1/artifacts", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        got = {it["artifact_id"] for it in body["items"]}
        assert got == {upload, tagged, shared}
        assert foreign not in got and foreign_shared not in got
        assert body["total"] == 3 and body["has_more"] is False

        r = client.get("/api/gateway/artifacts/search", params={"scope": "session", "session_id": "s1"}, headers=headers)
        assert r.status_code == 200, r.text
        assert {it["artifact_id"] for it in r.json()["items"]} == {upload, tagged, shared}

    assert seen["list_all"] == 0
    assert builds_on_loop and not any(builds_on_loop)
    assert seen["candidates_on_loop"] and not any(seen["candidates_on_loop"])


def test_run_artifacts_listing_runs_off_the_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env(tmp_path, monkeypatch)
    from abstractgateway.app import app
    from abstractruntime.storage.artifacts import FileArtifactStore

    on_loop = []
    real_search = FileArtifactStore.search

    def search(self, *a, **kw):
        on_loop.append(_on_loop())
        return real_search(self, *a, **kw)

    monkeypatch.setattr(FileArtifactStore, "search", search)
    headers = {"Authorization": "Bearer t"}
    with TestClient(app) as client:
        FileArtifactStore(tmp_path / "runtime").store(b"x", content_type="text/plain", run_id="session_memory_s9")
        for path, params in (
            ("/api/gateway/runs/session_memory_s9/artifacts", {}),
            ("/api/gateway/artifacts/stats", {"scope": "run", "run_id": "session_memory_s9"}),
            ("/api/gateway/artifacts/search", {"scope": "run", "run_id": "session_memory_s9"}),
        ):
            r = client.get(path, params=params, headers=headers)
            assert r.status_code == 200, (path, r.text)
    assert on_loop and not any(on_loop)


def test_runs_children_listing_runs_off_the_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _env(tmp_path, monkeypatch)
    from abstractgateway.app import app
    from abstractruntime.storage.json_files import JsonFileRunStore

    on_loop = []
    real = JsonFileRunStore.list_children

    def list_children(self, *a, **kw):
        on_loop.append(_on_loop())
        return real(self, *a, **kw)

    monkeypatch.setattr(JsonFileRunStore, "list_children", list_children)
    with TestClient(app) as client:
        r = client.get("/api/gateway/runs", params={"parent_run_id": "nobody"}, headers={"Authorization": "Bearer t"})
        assert r.status_code == 200, r.text
        assert r.json()["items"] == []
    assert on_loop and not any(on_loop)
