from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


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
                "data": {"nodeType": "on_flow_start", "label": "On Flow Start", "inputs": [], "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]},
            },
            {
                "id": "node-2",
                "type": "on_flow_end",
                "position": {"x": 288.0, "y": 128.0},
                "data": {"nodeType": "on_flow_end", "label": "On Flow End", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []},
            },
        ],
        "edges": [{"id": "e1", "source": "node-1", "sourceHandle": "exec-out", "target": "node-2", "targetHandle": "exec-in"}],
        "entryNode": "node-1",
    }

    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-01-09T00:00:00+00:00",
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


def _make_client(*, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict[str, str]]:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-discovery", flow_id="root")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    from abstractgateway.app import app

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    return client, headers


def test_discovery_requires_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, headers = _make_client(tmp_path=tmp_path, monkeypatch=monkeypatch)
    with client:
        r1 = client.get("/api/gateway/discovery/tools")
        assert r1.status_code in {401, 403}, r1.text

        r2 = client.get("/api/gateway/discovery/tools", headers=headers)
        assert r2.status_code == 200, r2.text


def test_discovery_tools_and_providers_are_deterministic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractcore.providers import registry as provider_registry

    monkeypatch.setattr(
        provider_registry,
        "get_all_providers_with_models",
        lambda include_models=False: [
            {"name": "lmstudio", "models": ["qwen"] if include_models else []},
            {"name": "ollama", "models": ["llama3"] if include_models else []},
        ],
    )
    monkeypatch.setattr(provider_registry, "get_available_models_for_provider", lambda _name: ["m1", "m2"])

    client, headers = _make_client(tmp_path=tmp_path, monkeypatch=monkeypatch)
    with client:
        tools = client.get("/api/gateway/discovery/tools", headers=headers)
        assert tools.status_code == 200, tools.text
        tool_items = tools.json().get("items") or []
        tool_names = {t.get("name") for t in tool_items if isinstance(t, dict)}
        assert {"list_files", "read_file", "write_file", "execute_command"} <= tool_names

        providers = client.get("/api/gateway/discovery/providers?include_models=false", headers=headers)
        assert providers.status_code == 200, providers.text
        items = providers.json().get("items") or []
        assert [p.get("name") for p in items] == ["lmstudio", "ollama"]

        providers2 = client.get("/api/gateway/discovery/providers?include_models=true", headers=headers)
        assert providers2.status_code == 200, providers2.text
        items2 = providers2.json().get("items") or []
        assert items2[0].get("models") == ["qwen"]

        models = client.get("/api/gateway/discovery/providers/lmstudio/models", headers=headers)
        assert models.status_code == 200, models.text
        assert models.json().get("provider") == "lmstudio"
        assert models.json().get("models") == ["m1", "m2"]

