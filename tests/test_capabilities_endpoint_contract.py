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
        "created_at": "2026-01-26T00:00:00+00:00",
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


def test_discovery_capabilities_requires_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-capabilities", flow_id="root")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        r1 = client.get("/api/gateway/discovery/capabilities")
        assert r1.status_code in {401, 403}, r1.text

        r2 = client.get("/api/gateway/discovery/capabilities", headers=headers)
        assert r2.status_code == 200, r2.text
        body = r2.json()
        caps = body.get("capabilities")
        assert isinstance(caps, dict)
        for k in ("voice", "tools", "visualflow", "vision_fallback", "media"):
            assert isinstance(caps.get(k), dict)
            assert isinstance(caps[k].get("installed"), bool)
        for k in ("abstractruntime", "abstractcore", "abstractvoice", "abstractvision", "multimodal", "capability_plugins"):
            assert isinstance(caps.get(k), dict)
            assert isinstance(caps[k].get("installed"), bool)
        plugin_caps = caps["capability_plugins"].get("capabilities")
        if isinstance(plugin_caps, dict):
            for capability in ("voice", "audio", "vision", "music"):
                assert isinstance(plugin_caps.get(capability), dict)

        contracts = caps.get("contracts")
        assert isinstance(contracts, dict)
        assert contracts.get("version") == 1

        common = contracts.get("common")
        assert isinstance(common, dict)
        assert common.get("runs", {}).get("start", {}).get("endpoint") == "/api/gateway/runs/start"
        assert common.get("ledger", {}).get("stream", {}).get("transport") == "sse"
        assert common.get("artifacts", {}).get("content", {}).get("endpoint") == "/api/gateway/runs/{run_id}/artifacts/{artifact_id}/content"
        assert common.get("prompt_cache", {}).get("provider_controls") is True
        assert common.get("prompt_cache", {}).get("session_lifecycle") is True
        assert common.get("prompt_cache", {}).get("session_endpoints", {}).get("prepare") == "/api/gateway/sessions/{session_id}/prompt_cache/prepare"

        flow_editor = contracts.get("flow_editor")
        assert isinstance(flow_editor, dict)
        assert flow_editor.get("version") == 1
        assert flow_editor.get("available") is True
        assert flow_editor.get("run_input_schema", {}).get("available") is True
        assert flow_editor.get("run_input_schema", {}).get("endpoint") == "/api/gateway/bundles/{bundle_id}/flows/{flow_id}/input_schema"

        assistant = contracts.get("assistant")
        assert isinstance(assistant, dict)
        assert assistant.get("version") == 1
        assert assistant.get("voice", {}).get("tts", {}).get("endpoint") == "/api/gateway/runs/{run_id}/voice/tts"
        assert assistant.get("voice", {}).get("stt", {}).get("endpoint") == "/api/gateway/runs/{run_id}/audio/transcribe"
        assert assistant.get("media", {}).get("generated_image", {}).get("direct_endpoint", {}).get("route_available") is True
        assert assistant.get("media", {}).get("generated_image", {}).get("direct_endpoint", {}).get("endpoint") == "/api/gateway/runs/{run_id}/images/generate"
        assert assistant.get("prompt_cache", {}).get("provider_controls") is True
        assert assistant.get("prompt_cache", {}).get("session_lifecycle") is True


def test_client_capability_contracts_are_explicit_when_optional_features_are_missing() -> None:
    from abstractgateway.routes.gateway import _build_client_capability_contracts

    contracts = _build_client_capability_contracts(
        {
            "abstractvoice": {"installed": False, "error": "missing"},
            "abstractvision": {"installed": False, "error": "missing"},
            "voice": {"installed": False, "install_hint": 'pip install "abstractgateway[voice]"'},
            "visualflow": {"installed": False, "error": "missing"},
            "capability_plugins": {
                "installed": True,
                "capabilities": {
                    "voice": {"available": False, "install_hint": "install voice"},
                    "audio": {"available": False, "install_hint": "install audio"},
                    "vision": {"available": False, "install_hint": "install vision"},
                },
            },
        }
    )

    assert contracts["version"] == 1
    tts = contracts["assistant"]["voice"]["tts"]
    stt = contracts["assistant"]["voice"]["stt"]
    assert tts["available"] is False
    assert tts["installed"] is False
    assert tts["unsupported"] is True
    assert "install_hint" in tts
    assert stt["available"] is False
    assert stt["unsupported"] is True

    media = contracts["assistant"]["media"]
    assert media["generated_image"]["workflow"]["available"] is False
    assert media["generated_image"]["direct_endpoint"]["route_available"] is True
    assert media["generated_image"]["direct_endpoint"]["available"] is False
    assert contracts["assistant"]["prompt_cache"]["provider_controls"] is True
    assert contracts["assistant"]["prompt_cache"]["session_lifecycle"] is True

    flow_editor = contracts["flow_editor"]
    assert flow_editor["visualflows"]["crud"]["available"] is True
    assert flow_editor["visualflows"]["publish"]["available"] is False
    assert "install_hint" in flow_editor["visualflows"]["publish"]
