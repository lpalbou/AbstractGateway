from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.basic


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict[str, str]]:
    token = "t"
    flows = tmp_path / "flows"
    flows.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")

    from abstractgateway.app import app

    return TestClient(app), {"Authorization": f"Bearer {token}"}


def test_gateway_durable_bloc_routes_use_runtime_host_facade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Dict[str, Any]]] = []

    class StubHostFacade:
        def upsert_text_bloc(self, **kwargs: Any) -> Dict[str, Any]:
            calls.append(("upsert_text", dict(kwargs)))
            return {"ok": True, "record": {"bloc_id": 7, "sha256": "sha-1"}}

        def get_bloc_record(self, **kwargs: Any) -> Dict[str, Any]:
            calls.append(("record", dict(kwargs)))
            return {"ok": True, "record": {"bloc_id": 7, "sha256": kwargs.get("sha256") or "sha-1"}}

        def list_blocs(self, **kwargs: Any) -> Dict[str, Any]:
            calls.append(("list", dict(kwargs)))
            return {"ok": True, "records": [{"bloc_id": 7, "sha256": "sha-1"}]}

        def get_bloc_kv_manifest(self, **kwargs: Any) -> Dict[str, Any]:
            calls.append(("kv_manifest", dict(kwargs)))
            return {"ok": True, "manifest": {"binding_id": "bind-1", "key": "work:orbit"}}

        def ensure_bloc_kv_artifact(self, **kwargs: Any) -> Dict[str, Any]:
            calls.append(("kv_ensure", dict(kwargs)))
            return {"ok": True, "artifact": {"artifact_path": "/tmp/orbit.kv"}}

        def load_bloc_kv_artifact(self, **kwargs: Any) -> Dict[str, Any]:
            calls.append(("kv_load", dict(kwargs)))
            return {"ok": True, "artifact": {"prompt_cache_binding": {"binding_id": "bind-1", "key": kwargs.get("key") or "work:orbit"}}}

        def list_bloc_kv_artifacts(self, **kwargs: Any) -> Dict[str, Any]:
            calls.append(("kv_list", dict(kwargs)))
            return {"ok": True, "artifacts": [{"artifact_path": "/tmp/orbit.kv", "provider": "mlx", "model": "qwen3:4b"}]}

        def delete_bloc_kv_artifact(self, **kwargs: Any) -> Dict[str, Any]:
            calls.append(("kv_delete", dict(kwargs)))
            return {"ok": True, "result": {"deleted": True, "artifact_path": kwargs.get("artifact_path")}}

        def prune_bloc_kv_artifacts(self, **kwargs: Any) -> Dict[str, Any]:
            calls.append(("kv_prune", dict(kwargs)))
            return {"ok": True, "results": [{"deleted": True, "artifact_path": "/tmp/orbit.kv"}]}

        def delete_bloc(self, **kwargs: Any) -> Dict[str, Any]:
            calls.append(("delete", dict(kwargs)))
            return {"ok": True, "result": {"deleted": True, "record": {"bloc_id": kwargs.get("bloc_id")}}}

    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (StubHostFacade(), None))

    client, headers = _client(tmp_path, monkeypatch)
    headers = {**headers, "X-AbstractCore-Provider-API-Key": "sekret"}
    with client:
        upsert = client.post(
            "/api/gateway/blocs/upsert_text",
            json={"path": "notes/orbit.txt", "content": "Orbit notes", "summary": "brief"},
            headers=headers,
        )
        record = client.get(
            "/api/gateway/blocs/record?bloc_id=7&base_url=http://provider.test/v1",
            headers=headers,
        )
        listed = client.get("/api/gateway/blocs?sha256=sha-1", headers=headers)
        manifest = client.get("/api/gateway/blocs/kv/manifest?bloc_id=7", headers=headers)
        ensured = client.post(
            "/api/gateway/blocs/kv/ensure",
            json={"bloc_id": 7, "force_rebuild": True, "debug": True},
            headers=headers,
        )
        loaded = client.post(
            "/api/gateway/blocs/kv/load",
            json={"bloc_id": 7, "key": "work:orbit", "make_default": True},
            headers=headers,
        )
        artifacts = client.get(
            "/api/gateway/blocs/kv/list?bloc_id=7&provider=mlx&model=qwen3:4b",
            headers=headers,
        )
        deleted_artifact = client.post(
            "/api/gateway/blocs/kv/delete",
            json={"bloc_id": 7, "artifact_path": "/tmp/orbit.kv", "clear_loaded": True, "dry_run": True},
            headers=headers,
        )
        pruned = client.post(
            "/api/gateway/blocs/kv/prune",
            json={"bloc_id": 7, "provider": "mlx", "model": "qwen3:4b", "force": True},
            headers=headers,
        )
        deleted = client.post(
            "/api/gateway/blocs/delete",
            json={"bloc_id": 7, "clear_loaded": True, "dry_run": True},
            headers=headers,
        )

    assert upsert.status_code == 200, upsert.text
    assert upsert.json()["record"]["bloc_id"] == 7
    assert upsert.json()["source"] == "abstractruntime.host_facade"

    assert record.status_code == 200, record.text
    assert record.json()["record"]["bloc_id"] == 7

    assert listed.status_code == 200, listed.text
    assert listed.json()["records"][0]["sha256"] == "sha-1"

    assert manifest.status_code == 200, manifest.text
    assert manifest.json()["manifest"]["binding_id"] == "bind-1"

    assert ensured.status_code == 200, ensured.text
    assert ensured.json()["artifact"]["artifact_path"] == "/tmp/orbit.kv"

    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["artifact"]["prompt_cache_binding"]["key"] == "work:orbit"

    assert artifacts.status_code == 200, artifacts.text
    assert artifacts.json()["artifacts"][0]["provider"] == "mlx"

    assert deleted_artifact.status_code == 200, deleted_artifact.text
    assert deleted_artifact.json()["result"]["deleted"] is True

    assert pruned.status_code == 200, pruned.text
    assert pruned.json()["results"][0]["deleted"] is True

    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["result"]["record"]["bloc_id"] == 7

    assert calls == [
        ("upsert_text", {"path": "notes/orbit.txt", "content": "Orbit notes", "media_type": "text", "summary": "brief", "provider_api_key": "sekret"}),
        ("record", {"bloc_id": 7, "base_url": "http://provider.test/v1", "provider_api_key": "sekret"}),
        ("list", {"sha256": "sha-1", "provider_api_key": "sekret"}),
        ("kv_manifest", {"bloc_id": 7, "provider_api_key": "sekret"}),
        ("kv_ensure", {"bloc_id": 7, "force_rebuild": True, "debug": True, "provider_api_key": "sekret"}),
        ("kv_load", {"bloc_id": 7, "force_rebuild": False, "debug": False, "key": "work:orbit", "make_default": True, "provider_api_key": "sekret"}),
        ("kv_list", {"bloc_id": 7, "provider": "mlx", "model": "qwen3:4b", "provider_api_key": "sekret"}),
        ("kv_delete", {"bloc_id": 7, "artifact_path": "/tmp/orbit.kv", "clear_loaded": True, "force": False, "dry_run": True, "debug": False, "provider_api_key": "sekret"}),
        ("kv_prune", {"bloc_id": 7, "provider": "mlx", "model": "qwen3:4b", "clear_loaded": False, "force": True, "dry_run": False, "debug": False, "provider_api_key": "sekret"}),
        ("delete", {"bloc_id": 7, "delete_kv": True, "clear_loaded": True, "force": False, "dry_run": True, "provider_api_key": "sekret"}),
    ]


def test_gateway_durable_bloc_routes_report_runtime_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(
        gateway_routes,
        "_gateway_abstractcore_host_facade",
        lambda: (None, "Gateway runtime is not wired to AbstractCore host controls."),
    )

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.post(
            "/api/gateway/blocs/kv/load",
            json={"bloc_id": 7, "key": "work:orbit"},
            headers=headers,
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["supported"] is False
    assert body["code"] == "bloc_unavailable"
    assert "host controls" in body["error"]


def test_gateway_durable_bloc_capability_contract_comes_from_runtime_host_facade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubHostFacade:
        def upsert_text_bloc(self, **kwargs: Any) -> Dict[str, Any]:
            return {"ok": True}

        def get_bloc_record(self, **kwargs: Any) -> Dict[str, Any]:
            return {"ok": True}

        def list_blocs(self, **kwargs: Any) -> Dict[str, Any]:
            return {"ok": True}

        def get_bloc_kv_manifest(self, **kwargs: Any) -> Dict[str, Any]:
            return {"ok": True}

        def ensure_bloc_kv_artifact(self, **kwargs: Any) -> Dict[str, Any]:
            return {"ok": True}

        def load_bloc_kv_artifact(self, **kwargs: Any) -> Dict[str, Any]:
            return {"ok": True}

        def list_bloc_kv_artifacts(self, **kwargs: Any) -> Dict[str, Any]:
            return {"ok": True}

        def delete_bloc_kv_artifact(self, **kwargs: Any) -> Dict[str, Any]:
            return {"ok": True}

        def prune_bloc_kv_artifacts(self, **kwargs: Any) -> Dict[str, Any]:
            return {"ok": True}

        def delete_bloc(self, **kwargs: Any) -> Dict[str, Any]:
            return {"ok": True}

    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (StubHostFacade(), None))

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/discovery/capabilities", headers=headers)

    assert resp.status_code == 200, resp.text
    durable = resp.json()["capabilities"]["contracts"]["common"]["prompt_cache"]["durable_blocs"]
    assert durable["route_available"] is True
    assert durable["available"] is True
    assert durable["lifecycle_available"] is True
    assert durable["source"] == "abstractruntime.host_facade"
    assert durable["exact_reuse_binding_param"] == "prompt_cache_binding"
    assert durable["stable_identifiers"] == ["bloc_id", "sha256"]
    assert durable["endpoints"]["upsert_text"] == "/api/gateway/blocs/upsert_text"
    assert durable["endpoints"]["kv_load"] == "/api/gateway/blocs/kv/load"
