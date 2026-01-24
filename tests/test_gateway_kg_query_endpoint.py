from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic


def _write_min_bundle(*, bundles_dir: Path, bundle_id: str, flow_id: str) -> None:
    bundles_dir.mkdir(parents=True, exist_ok=True)

    flow = {
        "id": flow_id,
        "name": "minimal",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "node-1",
                "type": "on_flow_start",
                "position": {"x": 32.0, "y": 128.0},
                "data": {
                    "nodeType": "on_flow_start",
                    "label": "On Flow Start",
                    "inputs": [],
                    "outputs": [{"id": "exec-out", "label": "", "type": "execution"}],
                },
            },
            {
                "id": "node-2",
                "type": "on_flow_end",
                "position": {"x": 288.0, "y": 128.0},
                "data": {
                    "nodeType": "on_flow_end",
                    "label": "On Flow End",
                    "inputs": [{"id": "exec-in", "label": "", "type": "execution"}],
                    "outputs": [],
                },
            },
        ],
        "edges": [
            {
                "id": "e1",
                "source": "node-1",
                "sourceHandle": "exec-out",
                "target": "node-2",
                "targetHandle": "exec-in",
            }
        ],
        "entryNode": "node-1",
    }

    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-01-21T00:00:00+00:00",
        "entrypoints": [{"flow_id": flow_id, "name": "root", "description": "", "interfaces": []}],
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }

    bundle_path = bundles_dir / f"{bundle_id}.flow"
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr(f"flows/{flow_id}.json", json.dumps(flow, indent=2))


def _make_client(*, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict[str, str], Path]:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-kg", flow_id="root")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")

    from abstractgateway.app import app

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    return client, headers, runtime_dir


def test_gateway_kg_query_endpoint_returns_persisted_triples(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        import lancedb  # type: ignore  # noqa: F401
    except Exception:
        pytest.skip("lancedb is not installed")

    from abstractmemory import LanceDBTripleStore, TripleAssertion
    from abstractruntime.integrations.abstractmemory.effect_handlers import resolve_scope_owner_id

    client, headers, runtime_dir = _make_client(tmp_path=tmp_path, monkeypatch=monkeypatch)
    with client:
        r = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": "bundle-kg", "flow_id": "root", "input_data": {}, "session_id": "sess-1"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]

        from abstractgateway.service import get_gateway_service

        svc = get_gateway_service()
        run_store = svc.host.run_store
        run = run_store.load(run_id)
        assert run is not None

        session_owner = resolve_scope_owner_id(run, scope="session", run_store=run_store)
        run_owner = resolve_scope_owner_id(run, scope="run", run_store=run_store)
        global_owner = resolve_scope_owner_id(run, scope="global", run_store=run_store)

        store_path = runtime_dir / "abstractmemory" / "kg"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store = LanceDBTripleStore(store_path)
        try:
            store.add(
                [
                    TripleAssertion(
                        subject="ex:person-laurent",
                        predicate="rdf:type",
                        object="schema:person",
                        scope="session",
                        owner_id=session_owner,
                    ),
                    TripleAssertion(
                        subject="ex:run-entity",
                        predicate="rdf:type",
                        object="schema:thing",
                        scope="run",
                        owner_id=run_owner,
                    ),
                    TripleAssertion(
                        subject="ex:global-entity",
                        predicate="rdf:type",
                        object="schema:thing",
                        scope="global",
                        owner_id=global_owner,
                    ),
                ]
            )
        finally:
            store.close()

        q1 = client.post(
            "/api/gateway/kg/query",
            json={"run_id": run_id, "scope": "session", "limit": 0},
            headers=headers,
        )
        assert q1.status_code == 200, q1.text
        body1 = q1.json()
        assert body1.get("scope") == "session"
        assert body1.get("owner_id") == session_owner
        items1 = body1.get("items") or []
        assert any(
            isinstance(i, dict)
            and i.get("scope") == "session"
            and i.get("owner_id") == session_owner
            and i.get("subject") == "ex:person-laurent"
            for i in items1
        )

        q2 = client.post(
            "/api/gateway/kg/query",
            json={"run_id": run_id, "scope": "all", "limit": 0},
            headers=headers,
        )
        assert q2.status_code == 200, q2.text
        body2 = q2.json()
        assert body2.get("scope") == "all"
        items2 = body2.get("items") or []
        scopes = {i.get("scope") for i in items2 if isinstance(i, dict)}
        assert {"run", "session", "global"} <= scopes


def test_gateway_kg_query_supports_session_id_without_run_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        import lancedb  # type: ignore  # noqa: F401
    except Exception:
        pytest.skip("lancedb is not installed")

    from abstractmemory import LanceDBTripleStore, TripleAssertion

    client, headers, runtime_dir = _make_client(tmp_path=tmp_path, monkeypatch=monkeypatch)
    with client:
        store_path = runtime_dir / "abstractmemory" / "kg"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store = LanceDBTripleStore(store_path)
        try:
            store.add(
                [
                    TripleAssertion(
                        subject="ex:person-laurent",
                        predicate="rdf:type",
                        object="schema:person",
                        scope="session",
                        owner_id="session_memory_sess-xyz",
                    )
                ]
            )
        finally:
            store.close()

        q = client.post(
            "/api/gateway/kg/query",
            json={"session_id": "sess-xyz", "scope": "session", "limit": 0},
            headers=headers,
        )
        assert q.status_code == 200, q.text
        body = q.json()
        assert body.get("scope") == "session"
        assert body.get("owner_id") == "session_memory_sess-xyz"
        items = body.get("items") or []
        assert any(isinstance(i, dict) and i.get("subject") == "ex:person-laurent" for i in items)


def test_gateway_kg_query_scope_all_works_with_session_id_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        import lancedb  # type: ignore  # noqa: F401
    except Exception:
        pytest.skip("lancedb is not installed")

    from abstractmemory import LanceDBTripleStore, TripleAssertion

    client, headers, runtime_dir = _make_client(tmp_path=tmp_path, monkeypatch=monkeypatch)
    with client:
        store_path = runtime_dir / "abstractmemory" / "kg"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store = LanceDBTripleStore(store_path)
        try:
            store.add(
                [
                    TripleAssertion(
                        subject="ex:person-laurent",
                        predicate="rdf:type",
                        object="schema:person",
                        scope="session",
                        owner_id="session_memory_sess-xyz",
                    ),
                    TripleAssertion(
                        subject="ex:global-thing",
                        predicate="rdf:type",
                        object="schema:thing",
                        scope="global",
                        owner_id="global_memory",
                    ),
                ]
            )
        finally:
            store.close()

        q = client.post(
            "/api/gateway/kg/query",
            json={"session_id": "sess-xyz", "scope": "all", "limit": 0},
            headers=headers,
        )
        assert q.status_code == 200, q.text
        body = q.json()
        assert body.get("scope") == "all"
        items = body.get("items") or []
        scopes = {i.get("scope") for i in items if isinstance(i, dict)}
        assert "session" in scopes
        assert "global" in scopes
        warnings = body.get("warnings") or []
        assert any(isinstance(w, str) and "run scope omitted" in w for w in warnings)


def test_gateway_kg_query_all_owners_session_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        import lancedb  # type: ignore  # noqa: F401
    except Exception:
        pytest.skip("lancedb is not installed")

    from abstractmemory import LanceDBTripleStore, TripleAssertion

    client, headers, runtime_dir = _make_client(tmp_path=tmp_path, monkeypatch=monkeypatch)
    with client:
        store_path = runtime_dir / "abstractmemory" / "kg"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store = LanceDBTripleStore(store_path)
        try:
            store.add(
                [
                    TripleAssertion(subject="ex:a", predicate="rdf:type", object="schema:thing", scope="session", owner_id="session_memory_s1"),
                    TripleAssertion(subject="ex:b", predicate="rdf:type", object="schema:thing", scope="session", owner_id="session_memory_s2"),
                ]
            )
        finally:
            store.close()

        q = client.post(
            "/api/gateway/kg/query",
            json={"scope": "session", "all_owners": True, "limit": 0},
            headers=headers,
        )
        assert q.status_code == 200, q.text
        body = q.json()
        assert body.get("scope") == "session"
        assert body.get("owner_id") is None
        items = body.get("items") or []
        owners = {i.get("owner_id") for i in items if isinstance(i, dict)}
        assert "session_memory_s1" in owners
        assert "session_memory_s2" in owners


def test_gateway_kg_query_all_owners_all_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        import lancedb  # type: ignore  # noqa: F401
    except Exception:
        pytest.skip("lancedb is not installed")

    from abstractmemory import LanceDBTripleStore, TripleAssertion

    client, headers, runtime_dir = _make_client(tmp_path=tmp_path, monkeypatch=monkeypatch)
    with client:
        store_path = runtime_dir / "abstractmemory" / "kg"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store = LanceDBTripleStore(store_path)
        try:
            store.add(
                [
                    TripleAssertion(subject="ex:run", predicate="rdf:type", object="schema:thing", scope="run", owner_id="run-1"),
                    TripleAssertion(subject="ex:sess", predicate="rdf:type", object="schema:thing", scope="session", owner_id="session_memory_s1"),
                    TripleAssertion(subject="ex:glob", predicate="rdf:type", object="schema:thing", scope="global", owner_id="global_memory"),
                ]
            )
        finally:
            store.close()

        q = client.post(
            "/api/gateway/kg/query",
            json={"scope": "all", "all_owners": True, "limit": 0},
            headers=headers,
        )
        assert q.status_code == 200, q.text
        body = q.json()
        assert body.get("scope") == "all"
        items = body.get("items") or []
        scopes = {i.get("scope") for i in items if isinstance(i, dict)}
        assert {"run", "session", "global"} <= scopes


def test_gateway_kg_query_rejects_all_owners_with_owner_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, headers, _ = _make_client(tmp_path=tmp_path, monkeypatch=monkeypatch)
    with client:
        q = client.post(
            "/api/gateway/kg/query",
            json={"scope": "session", "owner_id": "o1", "all_owners": True, "limit": 1},
            headers=headers,
        )
        assert q.status_code == 400, q.text


def test_gateway_kg_query_supports_query_text_when_embedder_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        import lancedb  # type: ignore  # noqa: F401
    except Exception:
        pytest.skip("lancedb is not installed")

    from abstractgateway.service import GatewayService, get_gateway_service
    from abstractmemory import LanceDBTripleStore, TripleAssertion

    class _StubEmbedder:
        def embed_texts(self, texts):  # type: ignore[no-untyped-def]
            out = []
            for t in texts:
                raw = hashlib.sha256(str(t or "").encode("utf-8")).digest()
                out.append([b / 255.0 for b in raw[:8]])
            return out

    embedder = _StubEmbedder()

    client, headers, runtime_dir = _make_client(tmp_path=tmp_path, monkeypatch=monkeypatch)
    with client:
        store_path = runtime_dir / "abstractmemory" / "kg"
        store_path.parent.mkdir(parents=True, exist_ok=True)
        store = LanceDBTripleStore(store_path, embedder=embedder)
        try:
            store.add(
                [
                    TripleAssertion(
                        subject="ex:person-laurent",
                        predicate="skos:definition",
                        object="laurent is a person",
                        scope="session",
                        owner_id="session_memory_sess-xyz",
                        observed_at="2026-01-21T00:00:00+00:00",
                    )
                ]
            )
        finally:
            store.close()

        svc = get_gateway_service()
        svc2 = GatewayService(**{**svc.__dict__, "embeddings_client": embedder})
        import abstractgateway.routes.gateway as routes_gateway

        monkeypatch.setattr(routes_gateway, "get_gateway_service", lambda: svc2)

        q = client.post(
            "/api/gateway/kg/query",
            json={"session_id": "sess-xyz", "scope": "session", "query_text": "laurent", "limit": 10},
            headers=headers,
        )
        assert q.status_code == 200, q.text
        body = q.json()
        assert body.get("scope") == "session"
        items = body.get("items") or []
        assert any(isinstance(i, dict) and i.get("subject") == "ex:person-laurent" for i in items)
        warnings = body.get("warnings") or []
        assert not any(isinstance(w, str) and "embedder" in w.lower() for w in warnings)
