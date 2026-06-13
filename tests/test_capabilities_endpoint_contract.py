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
        for k in ("abstractgateway", "abstractruntime", "abstractcore", "abstractvoice", "abstractvision", "multimodal", "capability_plugins"):
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
        assert common.get("runs", {}).get("input_data", {}).get("available") is True
        assert common.get("runs", {}).get("input_data", {}).get("endpoint") == "/api/gateway/runs/{run_id}/input_data"
        assert common.get("runs", {}).get("history_bundle", {}).get("available") is True
        assert common.get("runs", {}).get("history_bundle", {}).get("endpoint") == "/api/gateway/runs/{run_id}/history_bundle"
        assert common.get("runs", {}).get("purge_drafts", {}).get("endpoint") == "/api/gateway/runs/purge_drafts"
        assert common.get("ledger", {}).get("stream", {}).get("transport") == "sse"
        assert common.get("artifacts", {}).get("search", {}).get("endpoint") == "/api/gateway/artifacts/search"
        assert common.get("artifacts", {}).get("session_list", {}).get("endpoint") == "/api/gateway/sessions/{session_id}/artifacts"
        assert common.get("artifacts", {}).get("import", {}).get("endpoint") == "/api/gateway/artifacts/import"
        assert common.get("artifacts", {}).get("export", {}).get("endpoint") == "/api/gateway/runs/{run_id}/artifacts/{artifact_id}/export"
        assert common.get("artifacts", {}).get("content", {}).get("endpoint") == "/api/gateway/runs/{run_id}/artifacts/{artifact_id}/content"
        assert common.get("discovery", {}).get("voice_voices") == "/api/gateway/voice/voices"
        assert common.get("discovery", {}).get("audio_speech_models") == "/api/gateway/audio/speech/models"
        assert common.get("discovery", {}).get("audio_transcription_models") == "/api/gateway/audio/transcriptions/models"
        assert common.get("discovery", {}).get("audio_music_providers") == "/api/gateway/audio/music/providers"
        assert common.get("discovery", {}).get("audio_music_models") == "/api/gateway/audio/music/models"
        assert common.get("discovery", {}).get("embedding_models") == "/api/gateway/embeddings/models"
        assert common.get("discovery", {}).get("vision_provider_models") == "/api/gateway/vision/provider_models"
        assert common.get("discovery", {}).get("vision_models") == "/api/gateway/vision/models"
        assert common.get("discovery", {}).get("vision_adapters") == "/api/gateway/vision/adapters"
        code_execution = common.get("execution", {}).get("code")
        assert code_execution.get("contract") == "code_execution_policy_v1"
        assert code_execution.get("default_mode") == "sandbox"
        modes = {m.get("id"): m for m in code_execution.get("modes", [])}
        assert modes["sandbox"]["available"] is True
        assert modes["full_access"]["available"] is False
        assert contracts.get("flow_editor", {}).get("execution", {}).get("code") == code_execution
        catalog_contract = common.get("discovery", {}).get("catalog_contract")
        assert catalog_contract == {
            "contract": "gateway_catalog_v1",
            "version": 1,
            "metadata_field": "catalog",
            "primary_items_field": "items",
            "items_field": "items",
            "provider_item_fields": ["id", "label", "provider"],
            "model_item_fields": ["id", "label", "provider", "tasks", "parameters"],
            "adapter_item_fields": ["id", "label", "provider", "model", "tasks", "compatible_models"],
            "voice_item_fields": ["id", "label", "provider", "model", "voice_kind"],
        }
        readiness = common.get("readiness")
        assert readiness.get("contract") == "gateway_surface_readiness_v1"
        assert readiness.get("version") == 1
        assert readiness.get("truth_scope") == "gateway_contract_surface"
        surfaces = readiness.get("surfaces", {})
        assert surfaces.get("runs", {}).get("start") is True
        assert surfaces.get("artifacts", {}).get("content") is True
        assert surfaces.get("artifacts", {}).get("search") is True
        assert surfaces.get("artifacts", {}).get("session_list") is True
        assert surfaces.get("artifacts", {}).get("import") is True
        assert surfaces.get("artifacts", {}).get("export") is True
        assert surfaces.get("attachments", {}).get("upload") is True
        assert surfaces.get("workspace", {}).get("policy") is True
        assert surfaces.get("discovery", {}).get("audio_music_models") is True
        assert surfaces.get("discovery", {}).get("vision_adapters") is True
        assert surfaces.get("visualflows", {}).get("crud_available") is True
        assert surfaces.get("prompt_cache", {}).get("provider_controls") is True
        assert surfaces.get("model_residency", {}).get("route_available") is True
        memory = common.get("memory")
        assert isinstance(memory, dict)
        assert memory.get("route_available") is True
        if memory.get("installed") is True and not memory.get("error"):
            assert memory.get("available") is True
            assert surfaces.get("memory", {}).get("available") is True
        assert common.get("prompt_cache", {}).get("provider_controls") is True
        assert isinstance(common.get("prompt_cache", {}).get("provider_controls_available"), bool)
        assert common.get("prompt_cache", {}).get("session_lifecycle") is True
        assert isinstance(common.get("prompt_cache", {}).get("session_lifecycle_available"), bool)
        assert common.get("prompt_cache", {}).get("session_endpoints", {}).get("prepare") == "/api/gateway/sessions/{session_id}/prompt_cache/prepare"
        residency = common.get("model_residency")
        assert isinstance(residency, dict)
        assert residency.get("route_available") is True
        assert residency.get("endpoints", {}).get("loaded") == "/api/gateway/models/loaded"
        assert residency.get("endpoints", {}).get("load") == "/api/gateway/models/load"
        assert residency.get("endpoints", {}).get("unload") == "/api/gateway/models/unload"

        flow_editor = contracts.get("flow_editor")
        assert isinstance(flow_editor, dict)
        assert flow_editor.get("version") == 1
        assert flow_editor.get("available") is True
        assert flow_editor.get("run_input_schema", {}).get("available") is True
        assert flow_editor.get("run_input_schema", {}).get("endpoint") == "/api/gateway/bundles/{bundle_id}/flows/{flow_id}/input_schema"
        assert flow_editor.get("media", {}).get("generated_image", {}).get("direct_endpoint", {}).get("endpoint") == "/api/gateway/runs/{run_id}/images/generate"
        assert flow_editor.get("media", {}).get("edited_image", {}).get("direct_endpoint", {}).get("endpoint") == "/api/gateway/runs/{run_id}/images/edit"
        assert flow_editor.get("media", {}).get("upscaled_image", {}).get("direct_endpoint", {}).get("endpoint") == "/api/gateway/runs/{run_id}/images/upscale"
        assert flow_editor.get("media", {}).get("generated_video", {}).get("direct_endpoint", {}).get("endpoint") == "/api/gateway/runs/{run_id}/videos/generate"
        assert flow_editor.get("media", {}).get("image_to_video", {}).get("direct_endpoint", {}).get("endpoint") == "/api/gateway/runs/{run_id}/videos/from_image"
        assert flow_editor.get("media", {}).get("generated_voice", {}).get("direct_endpoint", {}).get("endpoint") == "/api/gateway/runs/{run_id}/voice/tts"
        assert flow_editor.get("media", {}).get("generated_music", {}).get("direct_endpoint", {}).get("endpoint") == "/api/gateway/runs/{run_id}/music/generate"
        assert flow_editor.get("voice", {}).get("stt", {}).get("endpoint") == "/api/gateway/runs/{run_id}/audio/transcribe"
        assert flow_editor.get("voice", {}).get("listen", {}).get("commands_type") == "emit_event"

        assistant = contracts.get("assistant")
        assert isinstance(assistant, dict)
        assert assistant.get("version") == 1
        assert assistant.get("voice", {}).get("tts", {}).get("endpoint") == "/api/gateway/runs/{run_id}/voice/tts"
        assert assistant.get("voice", {}).get("stt", {}).get("endpoint") == "/api/gateway/runs/{run_id}/audio/transcribe"
        assert assistant.get("voice", {}).get("listen", {}).get("host_capture_required") is True
        assert isinstance(assistant.get("media", {}).get("generated_image", {}).get("direct_endpoint", {}).get("route_available"), bool)
        assert assistant.get("media", {}).get("generated_image", {}).get("direct_endpoint", {}).get("endpoint") == "/api/gateway/runs/{run_id}/images/generate"
        assert assistant.get("media", {}).get("generated_image", {}).get("direct_endpoint", {}).get("adapter_catalog_endpoint") == "/api/gateway/vision/adapters"
        assert assistant.get("media", {}).get("generated_image", {}).get("direct_endpoint", {}).get("supports_batch") is True
        assert assistant.get("media", {}).get("generated_image", {}).get("direct_endpoint", {}).get("supports_lora_adapters") is True
        assert assistant.get("media", {}).get("generated_image", {}).get("direct_endpoint", {}).get("artifact_list_field") == "image_artifacts"
        assert assistant.get("media", {}).get("edited_image", {}).get("direct_endpoint", {}).get("endpoint") == "/api/gateway/runs/{run_id}/images/edit"
        assert assistant.get("media", {}).get("edited_image", {}).get("direct_endpoint", {}).get("supports_batch") is True
        assert assistant.get("media", {}).get("edited_image", {}).get("direct_endpoint", {}).get("supports_lora_adapters") is True
        assert assistant.get("media", {}).get("upscaled_image", {}).get("direct_endpoint", {}).get("endpoint") == "/api/gateway/runs/{run_id}/images/upscale"
        assert assistant.get("media", {}).get("upscaled_image", {}).get("direct_endpoint", {}).get("provider_models_task") == "image_upscale"
        assert assistant.get("media", {}).get("upscaled_image", {}).get("direct_endpoint", {}).get("artifact_list_field") == "image_artifacts"
        assert assistant.get("media", {}).get("generated_video", {}).get("direct_endpoint", {}).get("provider_models_task") == "text_to_video"
        assert assistant.get("media", {}).get("generated_video", {}).get("direct_endpoint", {}).get("adapter_catalog_endpoint") == "/api/gateway/vision/adapters"
        assert assistant.get("media", {}).get("generated_video", {}).get("direct_endpoint", {}).get("supports_batch") is True
        assert assistant.get("media", {}).get("generated_video", {}).get("direct_endpoint", {}).get("supports_lora_adapters") is True
        assert assistant.get("media", {}).get("generated_video", {}).get("direct_endpoint", {}).get("supports_flow_shift") is True
        assert assistant.get("media", {}).get("generated_video", {}).get("direct_endpoint", {}).get("artifact_list_field") == "video_artifacts"
        assert assistant.get("media", {}).get("image_to_video", {}).get("direct_endpoint", {}).get("provider_models_task") == "image_to_video"
        assert assistant.get("media", {}).get("image_to_video", {}).get("direct_endpoint", {}).get("supports_batch") is True
        assert assistant.get("media", {}).get("image_to_video", {}).get("direct_endpoint", {}).get("supports_lora_adapters") is True
        assert assistant.get("media", {}).get("image_to_video", {}).get("direct_endpoint", {}).get("supports_flow_shift") is True
        assert assistant.get("media", {}).get("image_to_video", {}).get("direct_endpoint", {}).get("artifact_list_field") == "video_artifacts"
        assert assistant.get("media", {}).get("generated_music", {}).get("direct_endpoint", {}).get("providers_endpoint") == "/api/gateway/audio/music/providers"
        assert assistant.get("media", {}).get("generated_music", {}).get("direct_endpoint", {}).get("provider_models_endpoint") == "/api/gateway/audio/music/models"
        assert assistant.get("prompt_cache", {}).get("provider_controls") is True
        assert assistant.get("prompt_cache", {}).get("session_lifecycle") is True


def test_client_capability_contracts_are_explicit_when_optional_features_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    for key in (
        "ABSTRACTVISION_BACKEND",
        "ABSTRACTVISION_BASE_URL",
        "ABSTRACTVISION_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "ABSTRACTCORE_SERVER_BASE_URL",
        "ABSTRACTCORE_VISION_MODEL_ID",
    ):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setattr(
        gateway_routes,
        "_gateway_abstractcore_run_facade",
        lambda: (None, "Gateway runtime is not wired to AbstractCore durable media helpers."),
    )
    monkeypatch.setattr(
        gateway_routes,
        "_gateway_abstractcore_host_facade",
        lambda: (None, "Gateway runtime is not wired to AbstractCore host controls."),
    )

    contracts = gateway_routes._build_client_capability_contracts(
        {
            "abstractvoice": {"installed": False, "error": "missing"},
            "abstractvision": {"installed": False, "error": "missing"},
            "voice": {"installed": False, "install_hint": "pip install abstractgateway"},
            "visualflow": {"installed": False, "error": "missing"},
            "capability_plugins": {
                "installed": True,
                "capabilities": {
                    "voice": {"available": False, "install_hint": "install voice"},
                    "audio": {"available": False, "install_hint": "install audio"},
                    "vision": {"available": False, "install_hint": "install vision"},
                    "music": {"available": False, "configured": False, "route_available": False, "config_hint": "install music"},
                },
            },
        }
    )

    assert contracts["version"] == 1
    assert contracts["common"]["runs"]["input_data"] == {
        "available": True,
        "endpoint": "/api/gateway/runs/{run_id}/input_data",
    }
    assert contracts["common"]["runs"]["history_bundle"] == {
        "available": True,
        "endpoint": "/api/gateway/runs/{run_id}/history_bundle",
    }
    code_execution = contracts["common"]["execution"]["code"]
    assert code_execution["contract"] == "code_execution_policy_v1"
    assert code_execution["simulate"]["endpoint"] == "/api/gateway/visualflows/code/simulate"
    modes = {m["id"]: m for m in code_execution["modes"]}
    assert modes["sandbox"]["available"] is True
    assert modes["full_access"]["available"] is False
    assert contracts["flow_editor"]["execution"]["code"] == code_execution
    assert contracts["flow_editor"]["runs"]["input_data"] == contracts["common"]["runs"]["input_data"]
    assert contracts["assistant"]["runs"]["history_bundle"] == contracts["common"]["runs"]["history_bundle"]
    assert contracts["abstractcode"]["runs"]["history_bundle"] == contracts["common"]["runs"]["history_bundle"]
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
    assert media["generated_image"]["direct_endpoint"]["route_available"] is False
    assert media["generated_image"]["direct_endpoint"]["available"] is False
    assert media["edited_image"]["workflow"]["available"] is False
    assert media["edited_image"]["direct_endpoint"]["route_available"] is False
    assert media["edited_image"]["direct_endpoint"]["available"] is False
    assert media["upscaled_image"]["workflow"]["available"] is False
    assert media["upscaled_image"]["direct_endpoint"]["route_available"] is False
    assert media["upscaled_image"]["direct_endpoint"]["available"] is False
    assert media["generated_video"]["direct_endpoint"]["route_available"] is False
    assert media["generated_video"]["direct_endpoint"]["available"] is False
    assert media["image_to_video"]["direct_endpoint"]["route_available"] is False
    assert media["image_to_video"]["direct_endpoint"]["available"] is False
    assert media["generated_music"]["direct_endpoint"]["route_available"] is False
    assert media["generated_music"]["direct_endpoint"]["available"] is False
    assert media["generated_music"]["direct_endpoint"]["providers_endpoint"] == "/api/gateway/audio/music/providers"
    assert media["generated_music"]["direct_endpoint"]["provider_models_endpoint"] == "/api/gateway/audio/music/models"
    assert contracts["assistant"]["prompt_cache"]["provider_controls"] is True
    assert contracts["assistant"]["prompt_cache"]["provider_controls_available"] is False
    assert contracts["assistant"]["prompt_cache"]["session_lifecycle"] is True
    assert contracts["assistant"]["prompt_cache"]["session_lifecycle_available"] is False
    residency = contracts["common"]["model_residency"]
    assert residency["route_available"] is True
    assert residency["available"] is False
    assert residency["source"] == "abstractruntime.host_facade"
    assert residency["supports"]["text_generation"] is False
    assert residency["supports"]["image_generation"] is False
    assert residency["supports"]["image_to_image"] is False
    assert residency["supports"]["image_upscale"] is False
    assert residency["supports"]["text_to_video"] is False
    assert residency["supports"]["image_to_video"] is False
    assert residency["supports"]["video_generation"] is False
    assert residency["supports"]["tts"] is False
    assert residency["supports"]["stt"] is False
    assert residency["supports"]["music_generation"] is False
    assert residency["endpoints"]["loaded"] == "/api/gateway/models/loaded"
    assert contracts["flow_editor"]["model_residency"] == residency
    assert contracts["assistant"]["model_residency"] == residency

    flow_editor = contracts["flow_editor"]
    assert flow_editor["visualflows"]["crud"]["available"] is True
    assert flow_editor["visualflows"]["publish"]["available"] is False
    assert "install_hint" in flow_editor["visualflows"]["publish"]
    assert flow_editor["media"] == contracts["assistant"]["media"]
    assert contracts["common"]["discovery"]["vision_models"] == "/api/gateway/vision/models"
    readiness = contracts["common"]["readiness"]
    assert readiness["contract"] == "gateway_surface_readiness_v1"
    assert readiness["surfaces"]["prompt_cache"]["provider_controls_available"] is False
    assert readiness["surfaces"]["prompt_cache"]["session_lifecycle_available"] is False
    assert readiness["surfaces"]["media"]["generated_image"]["route_available"] is False
    assert readiness["surfaces"]["media"]["generated_image"]["available"] is False
    assert readiness["surfaces"]["media"]["edited_image"]["route_available"] is False
    assert readiness["surfaces"]["media"]["generated_video"]["route_available"] is False
    assert readiness["surfaces"]["media"]["image_to_video"]["route_available"] is False
    assert readiness["surfaces"]["media"]["stt"]["route_available"] is False
    assert readiness["surfaces"]["media"]["listen"]["host_capture_required"] is True
    assert readiness["surfaces"]["media"]["generated_music"]["route_available"] is False
    assert readiness["surfaces"]["model_residency"]["supported_tasks"] == []
    assert readiness["surfaces"]["memory"]["route_available"] is True
    assert contracts["assistant"]["voice"]["stt"]["models_endpoint"] == "/api/gateway/audio/transcriptions/models"
    assert contracts["flow_editor"]["voice"]["listen"]["transcription_available"] is False


def test_code_execution_contract_reports_full_access_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setenv("ABSTRACTRUNTIME_CODE_FULL_ACCESS", "1")
    from abstractgateway.security.principal import GatewayPrincipal, reset_current_gateway_principal, set_current_gateway_principal

    token = set_current_gateway_principal(
        GatewayPrincipal(user_id="admin", tenant_id="default", roles=("admin", "user"), runtime_id="default")
    )
    try:
        contracts = gateway_routes._build_client_capability_contracts({})
    finally:
        reset_current_gateway_principal(token)
    code_execution = contracts["common"]["execution"]["code"]
    modes = {m["id"]: m for m in code_execution["modes"]}
    assert modes["sandbox"]["available"] is True
    assert modes["full_access"]["available"] is True
    assert contracts["flow_editor"]["execution"]["code"] == code_execution


def test_generated_image_contract_separates_light_package_from_backend_config(monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    for key in (
        "ABSTRACTVISION_BACKEND",
        "ABSTRACTVISION_BASE_URL",
        "ABSTRACTVISION_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "ABSTRACTCORE_SERVER_BASE_URL",
        "ABSTRACTCORE_VISION_MODEL_ID",
    ):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setattr(
        gateway_routes,
        "_gateway_abstractcore_run_facade",
        lambda: (None, "Gateway runtime is not wired to AbstractCore durable media helpers."),
    )

    contracts = gateway_routes._build_client_capability_contracts(
        {
            "abstractvoice": {"installed": True, "version": "0.9.0"},
            "abstractvision": {"installed": True, "version": "0.3.1"},
            "capability_plugins": {
                "installed": True,
                "capabilities": {
                    "vision": {
                        "available": False,
                        "install_hint": "Configure AbstractVision or an AbstractCore vision backend.",
                    },
                },
            },
        }
    )

    media = contracts["assistant"]["media"]["generated_image"]
    assert media["workflow"]["available"] is False
    assert media["direct_endpoint"]["route_available"] is False
    assert media["direct_endpoint"]["available"] is False
    assert "config_hint" in media["direct_endpoint"]
    edited = contracts["assistant"]["media"]["edited_image"]
    assert edited["workflow"]["available"] is False
    assert edited["direct_endpoint"]["route_available"] is False
    assert edited["direct_endpoint"]["available"] is False
    assert edited["direct_endpoint"]["provider_models_task"] == "image_to_image"
    video = contracts["assistant"]["media"]["generated_video"]
    assert video["workflow"]["available"] is False
    assert video["direct_endpoint"]["route_available"] is False
    assert video["direct_endpoint"]["available"] is False
    assert video["direct_endpoint"]["provider_models_task"] == "text_to_video"
    image_to_video = contracts["assistant"]["media"]["image_to_video"]
    assert image_to_video["workflow"]["available"] is False
    assert image_to_video["direct_endpoint"]["route_available"] is False
    assert image_to_video["direct_endpoint"]["available"] is False
    assert image_to_video["direct_endpoint"]["provider_models_task"] == "image_to_video"


def test_generated_image_contract_uses_openai_key_as_default_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.delenv("ABSTRACTVISION_BASE_URL", raising=False)
    monkeypatch.delenv("ABSTRACTVISION_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_run_facade", lambda: (object(), None))

    contracts = gateway_routes._build_client_capability_contracts(
        {
            "abstractvision": {"installed": True, "version": "0.3.1"},
            "capability_plugins": {
                "installed": True,
                "capabilities": {"vision": {"available": True, "selected_backend": "abstractvision:openai"}},
            },
        }
    )

    media = contracts["assistant"]["media"]["generated_image"]
    assert media["workflow"]["available"] is True
    assert media["direct_endpoint"]["available"] is True
    assert contracts["assistant"]["media"]["generated_video"]["direct_endpoint"]["route_available"] is False


def test_generated_music_contract_uses_runtime_music_capability_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_run_facade", lambda: (object(), None))

    contracts = gateway_routes._build_client_capability_contracts(
        {
            "capability_plugins": {
                "installed": True,
                "capabilities": {
                    "music": {
                        "available": True,
                        "route_available": True,
                        "configured": True,
                        "selected_backend": "acemusic",
                    },
                },
            },
        }
    )

    music = contracts["assistant"]["media"]["generated_music"]
    assert music["workflow"]["available"] is True
    assert music["workflow"]["backend"] == "acemusic"
    assert music["direct_endpoint"]["available"] is True
    assert music["direct_endpoint"]["route_available"] is True
    assert music["direct_endpoint"]["selected_backend"] == "acemusic"
    assert music["direct_endpoint"]["endpoint"] == "/api/gateway/runs/{run_id}/music/generate"
    assert music["direct_endpoint"]["providers_endpoint"] == "/api/gateway/audio/music/providers"
    assert music["direct_endpoint"]["provider_models_endpoint"] == "/api/gateway/audio/music/models"


def test_model_residency_contract_advertises_configured_core_server(monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.routes.gateway as gateway_routes

    monkeypatch.setenv("ABSTRACTCORE_SERVER_BASE_URL", "http://core.test/v1")

    class StubHostFacade:
        def get_model_residency_capabilities(self, **kwargs):
            assert kwargs == {}
            return {
                "ok": True,
                "supported": True,
                "operation": "capabilities",
                "mode": "remote_core_server",
                "source": "abstractruntime.remote",
                "relay_only": True,
                "tasks": {
                    "text_generation": {"supported": True},
                    "image_generation": {"supported": True},
                    "image_to_image": {"supported": True},
                    "image_upscale": {"supported": True},
                    "text_to_video": {"supported": True},
                    "image_to_video": {"supported": True},
                    "video_generation": {"supported": True},
                    "tts": {"supported": True},
                    "stt": {"supported": True},
                    "music_generation": {"supported": False},
                },
            }

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_host_facade", lambda: (StubHostFacade(), None))
    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_run_facade", lambda: (object(), None))

    from abstractgateway.security.principal import GatewayPrincipal, reset_current_gateway_principal, set_current_gateway_principal

    token = set_current_gateway_principal(
        GatewayPrincipal(user_id="admin", tenant_id="default", roles=("admin", "user"), runtime_id="default")
    )
    try:
        contracts = gateway_routes._build_client_capability_contracts({})
    finally:
        reset_current_gateway_principal(token)
    residency = contracts["common"]["model_residency"]
    media = contracts["assistant"]["media"]["generated_image"]

    assert residency["route_available"] is True
    assert residency["available"] is True
    assert residency["source"] == "abstractruntime.host_facade"
    assert residency["mode"] == "remote_core_server"
    assert residency["supports"]["image_generation"] is True
    assert residency["supports"]["image_to_image"] is True
    assert residency["supports"]["image_upscale"] is True
    assert residency["supports"]["text_to_video"] is True
    assert residency["supports"]["image_to_video"] is True
    assert residency["supports"]["video_generation"] is True
    assert residency["supports"]["tts"] is True
    assert residency["supports"]["stt"] is True
    assert residency["supports"]["music_generation"] is False
    assert residency["tasks"] == [
        "text_generation",
        "image_generation",
        "image_to_image",
        "image_upscale",
        "text_to_video",
        "image_to_video",
        "video_generation",
        "tts",
        "stt",
        "music_generation",
    ]
    assert residency["endpoints"] == {
        "loaded": "/api/gateway/models/loaded",
        "load": "/api/gateway/models/load",
        "unload": "/api/gateway/models/unload",
    }
    assert contracts["flow_editor"]["model_residency"] == residency
    assert contracts["assistant"]["model_residency"] == residency
    assert contracts["assistant"]["prompt_cache"]["provider_controls_available"] is True
    assert contracts["assistant"]["prompt_cache"]["session_lifecycle_available"] is True
    readiness = contracts["common"]["readiness"]
    assert readiness["surfaces"]["model_residency"]["available"] is True
    assert readiness["surfaces"]["model_residency"]["mode"] == "remote_core_server"
    assert readiness["surfaces"]["model_residency"]["supports"]["music_generation"] is False
    assert media["direct_endpoint"]["available"] is True
    assert media["direct_endpoint"]["endpoint"] == "/api/gateway/runs/{run_id}/images/generate"
    assert contracts["assistant"]["media"]["edited_image"]["direct_endpoint"]["endpoint"] == "/api/gateway/runs/{run_id}/images/edit"
    assert contracts["assistant"]["media"]["upscaled_image"]["direct_endpoint"]["endpoint"] == "/api/gateway/runs/{run_id}/images/upscale"
    assert contracts["assistant"]["media"]["upscaled_image"]["direct_endpoint"]["provider_models_task"] == "image_upscale"
