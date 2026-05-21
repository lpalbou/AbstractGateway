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


def test_gateway_model_residency_uses_runtime_host_facade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Dict[str, Any]]] = []

    class StubHostFacade:
        def list_model_residency(self, **kwargs: Any) -> Dict[str, Any]:
            calls.append(("list", dict(kwargs)))
            return {"ok": True, "supported": True, "models": [{"runtime_id": "local:text_generation:mlx:qwen"}], "filters": kwargs}

        def load_model_residency(self, **kwargs: Any) -> Dict[str, Any]:
            calls.append(("load", dict(kwargs)))
            return {"ok": True, "supported": True, "loaded_new": True, "runtime": {"runtime_id": "local:text_generation:mlx:qwen"}, "request": kwargs}

        def unload_model_residency(self, **kwargs: Any) -> Dict[str, Any]:
            calls.append(("unload", dict(kwargs)))
            return {"ok": True, "supported": True, "unloaded": True, "request": kwargs}

    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (StubHostFacade(), None))

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        loaded = client.get("/api/gateway/models/loaded?task=text_generation&provider=mlx", headers=headers)
        load = client.post("/api/gateway/models/load", json={"task": "text_generation", "provider": "mlx", "model": "qwen"}, headers=headers)
        unload = client.post("/api/gateway/models/unload", json={"provider": "mlx", "model": "qwen"}, headers=headers)

    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["models"][0]["runtime_id"] == "local:text_generation:mlx:qwen"
    assert loaded.json()["filters"] == {"task": "text_generation", "provider": "mlx"}
    assert loaded.json()["source"] == "abstractruntime.host_facade"

    assert load.status_code == 200, load.text
    assert load.json()["loaded_new"] is True
    assert load.json()["request"]["task"] == "text_generation"

    assert unload.status_code == 200, unload.text
    assert unload.json()["unloaded"] is True

    assert calls == [
        ("list", {"task": "text_generation", "provider": "mlx"}),
        ("load", {"task": "text_generation", "provider": "mlx", "model": "qwen", "pin": True}),
        ("unload", {"provider": "mlx", "model": "qwen"}),
    ]


def test_gateway_model_residency_reports_runtime_unavailable(
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
            "/api/gateway/models/load",
            json={"task": "image_generation", "provider": "mflux", "model": "flux"},
            headers=headers,
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["supported"] is False
    assert body["code"] == "model_residency_unavailable"
    assert "host controls" in body["error"]


def test_gateway_model_residency_preserves_runtime_unsupported_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubHostFacade:
        def load_model_residency(self, **kwargs: Any) -> Dict[str, Any]:
            return {
                "ok": False,
                "supported": False,
                "operation": "load",
                "code": "model_residency_unsupported",
                "task": kwargs.get("task"),
                "provider": kwargs.get("provider"),
                "model": kwargs.get("model"),
                "error": "Local image/audio residency is unsupported in AbstractRuntime. Use a long-lived remote AbstractCore server for media model warmup.",
            }

    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (StubHostFacade(), None))

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.post(
            "/api/gateway/models/load",
            json={"task": "image_generation", "provider": "huggingface", "model": "black-forest-labs/FLUX.2-klein-9B"},
            headers=headers,
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["supported"] is False
    assert body["operation"] == "load"
    assert body["code"] == "model_residency_unsupported"
    assert "remote AbstractCore server" in body["error"]


def test_gateway_model_residency_capability_contract_comes_from_runtime_host_facade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubHostFacade:
        pass

    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (StubHostFacade(), None))

    client, headers = _client(tmp_path, monkeypatch)
    with client:
        resp = client.get("/api/gateway/discovery/capabilities", headers=headers)

    assert resp.status_code == 200, resp.text
    residency = resp.json()["capabilities"]["contracts"]["common"]["model_residency"]
    assert residency["route_available"] is True
    assert residency["available"] is True
    assert residency["source"] == "abstractruntime.host_facade"
    assert residency["supports"] == {
        "text_generation": True,
        "image_generation": True,
        "tts": True,
        "stt": True,
    }
