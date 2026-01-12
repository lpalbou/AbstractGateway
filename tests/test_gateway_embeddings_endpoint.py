from __future__ import annotations

import json
from pathlib import Path
import zipfile

import pytest
from fastapi.testclient import TestClient


def _write_minimal_bundle(*, bundles_dir: Path) -> None:
    bundles_dir.mkdir(parents=True, exist_ok=True)
    bundle_id = "bundle-emb"
    flow_id = "root"

    flow = {
        "id": flow_id,
        "name": "root",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "node-1",
                "type": "on_flow_start",
                "position": {"x": 32.0, "y": 224.0},
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
                "position": {"x": 288.0, "y": 224.0},
                "data": {
                    "nodeType": "on_flow_end",
                    "label": "On Flow End",
                    "inputs": [{"id": "exec-in", "label": "", "type": "execution"}],
                    "outputs": [],
                },
            },
        ],
        "edges": [{"id": "e1", "source": "node-1", "sourceHandle": "exec-out", "target": "node-2", "targetHandle": "exec-in"}],
        "entryNode": "node-1",
    }

    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-01-12T00:00:00+00:00",
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


def test_gateway_embeddings_endpoint_returns_vectors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_minimal_bundle(bundles_dir=bundles_dir)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")

    monkeypatch.setenv("ABSTRACTGATEWAY_EMBEDDING_PROVIDER", "lmstudio")
    monkeypatch.setenv("ABSTRACTGATEWAY_EMBEDDING_MODEL", "text-embedding-nomic-embed-text-v1.5@q6_k")

    # Patch the embeddings client used by the gateway service so the test runs offline.
    from abstractruntime.integrations.abstractcore import embeddings_client as emb_mod

    class _DummyEmbeddingsClient:
        def __init__(self, *, provider: str, model: str, manager_kwargs=None):
            self._provider = provider
            self._model = model

        @property
        def provider(self) -> str:
            return self._provider

        @property
        def model(self) -> str:
            return self._model

        def embed_texts(self, texts):
            # Deterministic tiny vectors (dimension=3).
            out = []
            for t in list(texts):
                s = str(t or "")
                out.append([float(len(s)), 1.0, 0.0])
            return emb_mod.EmbeddingsResult(provider=self._provider, model=self._model, embeddings=out, dimension=3)

    monkeypatch.setattr(emb_mod, "AbstractCoreEmbeddingsClient", _DummyEmbeddingsClient)

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/embeddings",
            json={"input": ["hello", "world"]},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["provider"] == "lmstudio"
        assert body["model"] == "text-embedding-nomic-embed-text-v1.5@q6_k"
        assert body["dimension"] == 3
        assert len(body["data"]) == 2
        assert body["data"][0]["embedding"] == [5.0, 1.0, 0.0]


def test_gateway_embeddings_endpoint_rejects_model_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_minimal_bundle(bundles_dir=bundles_dir)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")

    monkeypatch.setenv("ABSTRACTGATEWAY_EMBEDDING_PROVIDER", "lmstudio")
    monkeypatch.setenv("ABSTRACTGATEWAY_EMBEDDING_MODEL", "text-embedding-nomic-embed-text-v1.5@q6_k")

    from abstractruntime.integrations.abstractcore import embeddings_client as emb_mod

    class _DummyEmbeddingsClient:
        def __init__(self, *, provider: str, model: str, manager_kwargs=None):
            self._provider = provider
            self._model = model

        @property
        def provider(self) -> str:
            return self._provider

        @property
        def model(self) -> str:
            return self._model

        def embed_texts(self, texts):
            out = [[0.0, 0.0, 0.0] for _ in list(texts)]
            return emb_mod.EmbeddingsResult(provider=self._provider, model=self._model, embeddings=out, dimension=3)

    monkeypatch.setattr(emb_mod, "AbstractCoreEmbeddingsClient", _DummyEmbeddingsClient)

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/embeddings",
            json={"input": "hello", "model": "other-model"},
            headers=headers,
        )
        assert r.status_code == 400, r.text
        assert "fixed" in r.text.lower()
