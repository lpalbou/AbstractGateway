from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic


def _make_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict[str, str], Path]:
    runtime_dir = tmp_path / "runtime"
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir(parents=True, exist_ok=True)
    token = "t"

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")

    from abstractgateway.app import app

    return TestClient(app), {"Authorization": f"Bearer {token}"}, runtime_dir


def test_memory_store_config_defaults_to_lancedb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ABSTRACTGATEWAY_MEMORY_STORE_BACKEND", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_MEMORY_STORE_PATH", raising=False)

    from abstractgateway.memory_store import resolve_memory_store_config

    cfg = resolve_memory_store_config(base_dir=tmp_path / "runtime")
    assert cfg.backend == "lancedb"
    assert cfg.path == (tmp_path / "runtime" / "abstractmemory" / "kg").resolve()


def test_memory_backend_check_reports_missing_selected_store_class(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_abstractmemory = types.ModuleType("abstractmemory")
    fake_abstractmemory.InMemoryTripleStore = object
    fake_abstractmemory.LanceDBTripleStore = object
    monkeypatch.setitem(sys.modules, "abstractmemory", fake_abstractmemory)
    monkeypatch.setitem(sys.modules, "lancedb", types.ModuleType("lancedb"))

    from abstractgateway.memory_store import memory_backend_unavailable_reason

    assert memory_backend_unavailable_reason("memory") is None
    assert memory_backend_unavailable_reason("lancedb") is None
    sqlite_reason = memory_backend_unavailable_reason("sqlite")
    assert sqlite_reason is not None
    assert "SQLiteTripleStore" in sqlite_reason


def test_open_memory_backend_does_not_import_unselected_sqlite_class(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeInMemoryTripleStore:
        def __init__(self, *, embedder=None):
            self.embedder = embedder

    fake_abstractmemory = types.ModuleType("abstractmemory")
    fake_abstractmemory.InMemoryTripleStore = FakeInMemoryTripleStore
    monkeypatch.setitem(sys.modules, "abstractmemory", fake_abstractmemory)

    from abstractgateway.memory_store import open_gateway_memory_store

    resolution = open_gateway_memory_store(base_dir=tmp_path, backend="memory")

    assert isinstance(resolution.store, FakeInMemoryTripleStore)
    assert resolution.config.backend == "memory"


def test_kg_query_uses_sqlite_memory_store_for_structured_queries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        from abstractmemory import SQLiteTripleStore, TripleAssertion
    except Exception:
        pytest.skip("abstractmemory is not installed")

    monkeypatch.setenv("ABSTRACTGATEWAY_MEMORY_STORE_BACKEND", "sqlite")
    client, headers, runtime_dir = _make_client(tmp_path, monkeypatch)

    store_path = runtime_dir / "abstractmemory" / "kg.sqlite3"
    store = SQLiteTripleStore(store_path)
    try:
        store.add(
            [
                TripleAssertion(
                    subject="ex:person-laurent",
                    predicate="rdf:type",
                    object="schema:person",
                    scope="session",
                    owner_id="owner-1",
                )
            ]
        )
    finally:
        store.close()

    with client:
        resp = client.post(
            "/api/gateway/kg/query",
            json={"scope": "session", "owner_id": "owner-1", "limit": 10},
            headers=headers,
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["store"]["backend"] == "sqlite"
    assert body["store"]["capabilities"]["structured_query"] is True
    assert body["store"]["capabilities"]["semantic_query"] is False
    assert body["items"][0]["subject"] == "ex:person-laurent"


def test_kg_query_rejects_semantic_query_on_sqlite_memory_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        from abstractmemory import SQLiteTripleStore, TripleAssertion
    except Exception:
        pytest.skip("abstractmemory is not installed")

    monkeypatch.setenv("ABSTRACTGATEWAY_MEMORY_STORE_BACKEND", "sqlite")
    client, headers, runtime_dir = _make_client(tmp_path, monkeypatch)

    store_path = runtime_dir / "abstractmemory" / "kg.sqlite3"
    store = SQLiteTripleStore(store_path)
    try:
        store.add(
            [
                TripleAssertion(
                    subject="ex:person-laurent",
                    predicate="skos:definition",
                    object="laurent is a person",
                    scope="session",
                    owner_id="owner-1",
                )
            ]
        )
    finally:
        store.close()

    with client:
        resp = client.post(
            "/api/gateway/kg/query",
            json={"scope": "session", "owner_id": "owner-1", "query_text": "laurent", "limit": 10},
            headers=headers,
        )
    assert resp.status_code == 400, resp.text
    assert "Semantic KG query is not available" in resp.text


def test_kg_query_rejects_semantic_query_on_sqlite_before_store_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_MEMORY_STORE_BACKEND", "sqlite")
    client, headers, _runtime_dir = _make_client(tmp_path, monkeypatch)

    with client:
        resp = client.post(
            "/api/gateway/kg/query",
            json={"scope": "session", "owner_id": "owner-1", "query_text": "laurent", "limit": 10},
            headers=headers,
        )
    assert resp.status_code == 400, resp.text
    assert "Semantic KG query is not available" in resp.text
