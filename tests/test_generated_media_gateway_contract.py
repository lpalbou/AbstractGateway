from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict

from fastapi.testclient import TestClient


_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\xf8\x0f"
    b"\x00\x01\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)

_WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt music-data"
_MP4_BYTES = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isomvideo-data"


def _write_image_bundle(*, bundles_dir: Path, bundle_id: str, flow_id: str) -> None:
    bundles_dir.mkdir(parents=True, exist_ok=True)
    flow = {
        "id": flow_id,
        "name": "generated-media-contract",
        "nodes": [
            {
                "id": "start",
                "type": "on_flow_start",
                "position": {"x": 0, "y": 0},
                "data": {"nodeType": "on_flow_start", "outputs": [{"id": "exec-out", "type": "execution"}]},
            },
            {
                "id": "end",
                "type": "on_flow_end",
                "position": {"x": 200, "y": 0},
                "data": {"nodeType": "on_flow_end", "inputs": [{"id": "exec-in", "type": "execution"}]},
            },
        ],
        "edges": [{"id": "e", "source": "start", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"}],
        "entryNode": "start",
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
    with zipfile.ZipFile(bundles_dir / f"{bundle_id}.flow", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr(f"flows/{flow_id}.json", json.dumps(flow))


def _wait_until(fn, *, timeout_s: float = 5.0, poll_s: float = 0.05) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if fn():
            return
        time.sleep(poll_s)
    raise AssertionError("condition did not become true before timeout")


def test_gateway_direct_image_generation_uses_runtime_child_run_contract(tmp_path: Path, monkeypatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_image_bundle(bundles_dir=bundles_dir, bundle_id="image-contract", flow_id="root")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTVISION_BACKEND", "mflux")

    import abstractgateway.routes.gateway as gateway_routes
    from abstractruntime.core.models import RunStatus

    monkeypatch.setattr(gateway_routes, "_gateway_has_local_mflux_preset", lambda _model_id: True)

    class StubRunFacade:
        def generate_image(self, parent_run_id: str, *, prompt: str, output: Dict[str, Any], params: Dict[str, Any], child_vars=None):
            _ = child_vars
            child_run_id = "child-image-1"
            assert prompt == "a one pixel generated test image"
            assert output["modality"] == "image"
            assert output["task"] == "image_generation"
            assert output["provider"] == "mflux"
            assert output["model"] == "AbstractFramework/flux.2-klein-4b-4bit"
            svc = gateway_routes.get_gateway_service()
            store = svc.stores.artifact_store
            tags = output.get("tags") if isinstance(output.get("tags"), dict) else {}
            meta = store.store(_PNG_BYTES, content_type="image/png", run_id=child_run_id, tags=tags)
            return SimpleNamespace(
                run_id=child_run_id,
                status=RunStatus.COMPLETED,
                error=None,
                output={
                    "result": {
                        "outputs": {
                            "image": [
                                {
                                    "modality": "image",
                                    "task": "image_generation",
                                    "provider": "mflux",
                                    "model": "AbstractFramework/flux.2-klein-4b-4bit",
                                    "content_type": "image/png",
                                    "format": "png",
                                    "artifact_ref": {
                                        "$artifact": meta.artifact_id,
                                        "artifact_id": meta.artifact_id,
                                        "content_type": "image/png",
                                        "size_bytes": len(_PNG_BYTES),
                                    },
                                }
                            ]
                        }
                    }
                },
            )

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_run_facade", lambda: (StubRunFacade(), None))

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        start = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": "image-contract", "bundle_version": "0.0.0", "flow_id": "root", "input_data": {}},
            headers=headers,
        )
        assert start.status_code == 200, start.text
        run_id = start.json()["run_id"]

        _wait_until(lambda: client.get(f"/api/gateway/runs/{run_id}", headers=headers).json().get("status") == "completed")

        caps = client.get("/api/gateway/discovery/capabilities", headers=headers)
        assert caps.status_code == 200, caps.text
        direct = caps.json()["capabilities"]["contracts"]["assistant"]["media"]["generated_image"]["direct_endpoint"]
        assert direct["route_available"] is True
        assert direct["available"] is True
        assert direct["endpoint"] == "/api/gateway/runs/{run_id}/images/generate"
        assert direct["durability"] == "runtime_child_run"
        assert direct["returns_child_run_id"] is True

        generated = client.post(
            f"/api/gateway/runs/{run_id}/images/generate",
            json={
                "prompt": "a one pixel generated test image",
                "provider": "stub",
                "model": "stub-chat",
                "image_provider": "mflux",
                "image_model": "AbstractFramework/flux.2-klein-4b-4bit",
                "format": "png",
                "request_id": "img-1",
            },
            headers=headers,
        )
        assert generated.status_code == 200, generated.text
        body = generated.json()
        assert body["ok"] is True, body
        assert body["supported"] is True
        assert body["child_run_id"] == "child-image-1"
        assert body["event_name"] == "abstract.progress"
        image_ref = body["image_artifact"]
        assert image_ref["content_type"] == "image/png"
        assert image_ref["filename"] == "generated.png"
        assert image_ref["size_bytes"] == len(_PNG_BYTES)
        assert image_ref["sha256"]

        artifact_id = image_ref["$artifact"]
        content = client.get(f"/api/gateway/runs/{run_id}/artifacts/{artifact_id}/content", headers=headers)
        assert content.status_code == 200, content.text
        assert content.content == _PNG_BYTES

        ledger = client.get(f"/api/gateway/runs/{run_id}/ledger?after=0&limit=200", headers=headers)
        assert ledger.status_code == 200, ledger.text
        events = [item for item in ledger.json().get("items", []) if isinstance(item, dict)]
        assert not any(
            (((item.get("effect") or {}).get("payload") or {}).get("name") == "abstract.media.image.generated")
            for item in events
        )


def test_gateway_direct_image_edit_uses_runtime_child_run_contract(tmp_path: Path, monkeypatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_image_bundle(bundles_dir=bundles_dir, bundle_id="image-edit-contract", flow_id="root")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTVISION_BACKEND", "mflux")

    import abstractgateway.routes.gateway as gateway_routes
    from abstractruntime.core.models import RunStatus

    monkeypatch.setattr(gateway_routes, "_gateway_has_local_mflux_preset", lambda _model_id: True)

    class StubRunFacade:
        def edit_image(self, parent_run_id: str, *, prompt: str, media, output: Dict[str, Any], params: Dict[str, Any], child_vars=None):
            _ = child_vars
            child_run_id = "child-image-edit-1"
            assert prompt == "Turn the coat red."
            assert output["modality"] == "image"
            assert output["task"] == "image_edit"
            assert output["provider"] == "mflux"
            assert output["model"] == "AbstractFramework/flux.2-klein-4b-4bit"
            assert output["strength"] == 0.6
            assert media[0]["role"] == "source"
            assert media[1]["role"] == "mask"
            svc = gateway_routes.get_gateway_service()
            store = svc.stores.artifact_store
            tags = output.get("tags") if isinstance(output.get("tags"), dict) else {}
            meta = store.store(_PNG_BYTES, content_type="image/png", run_id=child_run_id, tags=tags)
            return SimpleNamespace(
                run_id=child_run_id,
                status=RunStatus.COMPLETED,
                error=None,
                output={
                    "result": {
                        "outputs": {
                            "image": [
                                {
                                    "modality": "image",
                                    "task": "image_edit",
                                    "provider": "mflux",
                                    "model": "AbstractFramework/flux.2-klein-4b-4bit",
                                    "content_type": "image/png",
                                    "format": "png",
                                    "artifact_ref": {
                                        "$artifact": meta.artifact_id,
                                        "artifact_id": meta.artifact_id,
                                        "content_type": "image/png",
                                        "size_bytes": len(_PNG_BYTES),
                                    },
                                }
                            ]
                        }
                    }
                },
            )

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_run_facade", lambda: (StubRunFacade(), None))

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        start = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": "image-edit-contract", "bundle_version": "0.0.0", "flow_id": "root", "session_id": "sess-edit", "input_data": {}},
            headers=headers,
        )
        assert start.status_code == 200, start.text
        run_id = start.json()["run_id"]

        _wait_until(lambda: client.get(f"/api/gateway/runs/{run_id}", headers=headers).json().get("status") == "completed")

        svc = gateway_routes.get_gateway_service()
        store = svc.stores.artifact_store
        source_meta = store.store(b"png-source", content_type="image/png", run_id="session_memory_sess-edit", tags={"session_id": "sess-edit"})
        mask_meta = store.store(b"png-mask", content_type="image/png", run_id="session_memory_sess-edit", tags={"session_id": "sess-edit"})

        caps = client.get("/api/gateway/discovery/capabilities", headers=headers)
        assert caps.status_code == 200, caps.text
        direct = caps.json()["capabilities"]["contracts"]["assistant"]["media"]["edited_image"]["direct_endpoint"]
        assert direct["route_available"] is True
        assert direct["available"] is True
        assert direct["endpoint"] == "/api/gateway/runs/{run_id}/images/edit"
        assert direct["provider_models_task"] == "image_to_image"
        assert direct["mask_supported"] is True

        edited = client.post(
            f"/api/gateway/runs/{run_id}/images/edit",
            json={
                "prompt": "Turn the coat red.",
                "provider": "stub",
                "model": "stub-chat",
                "image_artifact": {"$artifact": source_meta.artifact_id, "content_type": "image/png"},
                "mask_artifact": {"$artifact": mask_meta.artifact_id, "content_type": "image/png"},
                "image_provider": "mflux",
                "image_model": "AbstractFramework/flux.2-klein-4b-4bit",
                "strength": 0.6,
                "format": "png",
                "request_id": "img-edit-1",
            },
            headers=headers,
        )
        assert edited.status_code == 200, edited.text
        body = edited.json()
        assert body["ok"] is True, body
        assert body["supported"] is True
        assert body["child_run_id"] == "child-image-edit-1"
        assert body["event_name"] == "abstract.progress"
        image_ref = body["image_artifact"]
        assert image_ref["content_type"] == "image/png"
        assert image_ref["filename"] == "edited.png"
        assert image_ref["size_bytes"] == len(_PNG_BYTES)
        assert image_ref["sha256"]

        artifact_id = image_ref["$artifact"]
        content = client.get(f"/api/gateway/runs/{run_id}/artifacts/{artifact_id}/content", headers=headers)
        assert content.status_code == 200, content.text
        assert content.content == _PNG_BYTES


def test_gateway_direct_video_generation_uses_runtime_child_run_contract(tmp_path: Path, monkeypatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_image_bundle(bundles_dir=bundles_dir, bundle_id="video-contract", flow_id="root")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTVISION_BACKEND", "mflux")

    import abstractgateway.routes.gateway as gateway_routes
    from abstractruntime.core.models import RunStatus

    monkeypatch.setattr(gateway_routes, "_gateway_has_local_mflux_preset", lambda _model_id: True)

    class StubRunFacade:
        def generate_video(self, parent_run_id: str, *, prompt: str, output: Dict[str, Any], params: Dict[str, Any], child_vars=None):
            _ = parent_run_id, params, child_vars
            child_run_id = "child-video-1"
            assert prompt == "a short product reveal"
            assert output["modality"] == "video"
            assert output["task"] == "text_to_video"
            assert output["provider"] == "mlx-gen"
            assert output["model"] == "Wan-AI/Wan2.2-TI2V-5B-Diffusers"
            assert output["frames"] == 41
            assert output["fps"] == 24
            svc = gateway_routes.get_gateway_service()
            store = svc.stores.artifact_store
            tags = output.get("tags") if isinstance(output.get("tags"), dict) else {}
            meta = store.store(_MP4_BYTES, content_type="video/mp4", run_id=child_run_id, tags=tags)
            return SimpleNamespace(
                run_id=child_run_id,
                status=RunStatus.COMPLETED,
                error=None,
                output={
                    "result": {
                        "outputs": {
                            "video": [
                                {
                                    "modality": "video",
                                    "task": "text_to_video",
                                    "provider": "mlx-gen",
                                    "model": "Wan-AI/Wan2.2-TI2V-5B-Diffusers",
                                    "content_type": "video/mp4",
                                    "format": "mp4",
                                    "artifact_ref": {
                                        "$artifact": meta.artifact_id,
                                        "artifact_id": meta.artifact_id,
                                        "content_type": "video/mp4",
                                        "size_bytes": len(_MP4_BYTES),
                                    },
                                }
                            ]
                        }
                    }
                },
            )

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_run_facade", lambda: (StubRunFacade(), None))

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        start = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": "video-contract", "bundle_version": "0.0.0", "flow_id": "root", "input_data": {}},
            headers=headers,
        )
        assert start.status_code == 200, start.text
        run_id = start.json()["run_id"]

        _wait_until(lambda: client.get(f"/api/gateway/runs/{run_id}", headers=headers).json().get("status") == "completed")

        caps = client.get("/api/gateway/discovery/capabilities", headers=headers)
        assert caps.status_code == 200, caps.text
        direct = caps.json()["capabilities"]["contracts"]["assistant"]["media"]["generated_video"]["direct_endpoint"]
        assert direct["route_available"] is True
        assert direct["available"] is True
        assert direct["endpoint"] == "/api/gateway/runs/{run_id}/videos/generate"
        assert direct["provider_models_task"] == "text_to_video"
        assert direct["progress_event_name"] == "abstract.progress"

        generated = client.post(
            f"/api/gateway/runs/{run_id}/videos/generate",
            json={
                "prompt": "a short product reveal",
                "provider": "stub",
                "model": "stub-chat",
                "video_provider": "mlx-gen",
                "video_model": "Wan-AI/Wan2.2-TI2V-5B-Diffusers",
                "frames": 41,
                "fps": 24,
                "format": "mp4",
                "request_id": "video-1",
            },
            headers=headers,
        )
        assert generated.status_code == 200, generated.text
        body = generated.json()
        assert body["ok"] is True, body
        assert body["supported"] is True
        assert body["child_run_id"] == "child-video-1"
        assert body["event_name"] == "abstract.progress"
        video_ref = body["video_artifact"]
        assert video_ref["content_type"] == "video/mp4"
        assert video_ref["filename"] == "video.mp4"
        assert video_ref["size_bytes"] == len(_MP4_BYTES)

        artifact_id = video_ref["$artifact"]
        content = client.get(f"/api/gateway/runs/{run_id}/artifacts/{artifact_id}/content", headers=headers)
        assert content.status_code == 200, content.text
        assert content.content == _MP4_BYTES


def test_gateway_direct_image_to_video_uses_runtime_child_run_contract(tmp_path: Path, monkeypatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_image_bundle(bundles_dir=bundles_dir, bundle_id="image-to-video-contract", flow_id="root")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTVISION_BACKEND", "mflux")

    import abstractgateway.routes.gateway as gateway_routes
    from abstractruntime.core.models import RunStatus

    monkeypatch.setattr(gateway_routes, "_gateway_has_local_mflux_preset", lambda _model_id: True)

    class StubRunFacade:
        def image_to_video(self, parent_run_id: str, *, prompt: str, media, output: Dict[str, Any], params: Dict[str, Any], child_vars=None):
            _ = parent_run_id, params, child_vars
            child_run_id = "child-image-to-video-1"
            assert prompt == "slowly rotate the product"
            assert media[0]["role"] == "source"
            assert output["modality"] == "video"
            assert output["task"] == "image_to_video"
            assert output["provider"] == "mlx-gen"
            assert output["model"] == "Wan-AI/Wan2.2-TI2V-5B-Diffusers"
            svc = gateway_routes.get_gateway_service()
            store = svc.stores.artifact_store
            tags = output.get("tags") if isinstance(output.get("tags"), dict) else {}
            meta = store.store(_MP4_BYTES, content_type="video/mp4", run_id=child_run_id, tags=tags)
            return SimpleNamespace(
                run_id=child_run_id,
                status=RunStatus.COMPLETED,
                error=None,
                output={
                    "result": {
                        "outputs": {
                            "video": [
                                {
                                    "modality": "video",
                                    "task": "image_to_video",
                                    "provider": "mlx-gen",
                                    "model": "Wan-AI/Wan2.2-TI2V-5B-Diffusers",
                                    "content_type": "video/mp4",
                                    "format": "mp4",
                                    "artifact_ref": {
                                        "$artifact": meta.artifact_id,
                                        "artifact_id": meta.artifact_id,
                                        "content_type": "video/mp4",
                                        "size_bytes": len(_MP4_BYTES),
                                    },
                                }
                            ]
                        }
                    }
                },
            )

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_run_facade", lambda: (StubRunFacade(), None))

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        start = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": "image-to-video-contract", "bundle_version": "0.0.0", "flow_id": "root", "session_id": "sess-video", "input_data": {}},
            headers=headers,
        )
        assert start.status_code == 200, start.text
        run_id = start.json()["run_id"]

        _wait_until(lambda: client.get(f"/api/gateway/runs/{run_id}", headers=headers).json().get("status") == "completed")

        svc = gateway_routes.get_gateway_service()
        store = svc.stores.artifact_store
        source_meta = store.store(_PNG_BYTES, content_type="image/png", run_id="session_memory_sess-video", tags={"session_id": "sess-video"})

        caps = client.get("/api/gateway/discovery/capabilities", headers=headers)
        assert caps.status_code == 200, caps.text
        direct = caps.json()["capabilities"]["contracts"]["assistant"]["media"]["image_to_video"]["direct_endpoint"]
        assert direct["route_available"] is True
        assert direct["available"] is True
        assert direct["endpoint"] == "/api/gateway/runs/{run_id}/videos/from_image"
        assert direct["provider_models_task"] == "image_to_video"

        generated = client.post(
            f"/api/gateway/runs/{run_id}/videos/from_image",
            json={
                "prompt": "slowly rotate the product",
                "image_artifact": {"$artifact": source_meta.artifact_id, "content_type": "image/png"},
                "video_provider": "mlx-gen",
                "video_model": "Wan-AI/Wan2.2-TI2V-5B-Diffusers",
                "format": "mp4",
                "request_id": "image-to-video-1",
            },
            headers=headers,
        )
        assert generated.status_code == 200, generated.text
        body = generated.json()
        assert body["ok"] is True, body
        assert body["supported"] is True
        assert body["child_run_id"] == "child-image-to-video-1"
        video_ref = body["video_artifact"]
        assert video_ref["content_type"] == "video/mp4"
        assert video_ref["filename"] == "video.mp4"
        assert video_ref["size_bytes"] == len(_MP4_BYTES)

        artifact_id = video_ref["$artifact"]
        content = client.get(f"/api/gateway/runs/{run_id}/artifacts/{artifact_id}/content", headers=headers)
        assert content.status_code == 200, content.text
        assert content.content == _MP4_BYTES


def test_gateway_direct_music_generation_uses_runtime_child_run_contract(tmp_path: Path, monkeypatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_image_bundle(bundles_dir=bundles_dir, bundle_id="music-contract", flow_id="root")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    import abstractgateway.routes.gateway as gateway_routes
    from abstractruntime.core.models import RunStatus

    class StubRunFacade:
        def generate_music(self, parent_run_id: str, *, prompt: str, output: Dict[str, Any], params: Dict[str, Any], child_vars=None):
            _ = child_vars
            sound_mode = output.get("task") == "text_to_audio"
            child_run_id = "child-sound-1" if sound_mode else "child-music-1"
            assert prompt == ("short office notification chime" if sound_mode else "warm lo-fi piano with brushed drums")
            assert output["modality"] == ("sound" if sound_mode else "music")
            assert output["task"] == ("text_to_audio" if sound_mode else "text_to_music")
            assert output["provider"] == ("stable-audio" if sound_mode else "acemusic")
            assert output["model"] == ("stabilityai/stable-audio-open-small" if sound_mode else "ace-step")
            assert output["tags"]["modality"] == ("sound" if sound_mode else "music")
            assert output["tags"]["task"] == ("sound_generation" if sound_mode else "music_generation")
            if not sound_mode:
                assert output["duration_s"] == 8
            svc = gateway_routes.get_gateway_service()
            store = svc.stores.artifact_store
            tags = output.get("tags") if isinstance(output.get("tags"), dict) else {}
            meta = store.store(_WAV_BYTES, content_type="audio/wav", run_id=child_run_id, tags=tags)
            return SimpleNamespace(
                run_id=child_run_id,
                status=RunStatus.COMPLETED,
                error=None,
                output={
                    "result": {
                        "outputs": {
                            ("sound" if sound_mode else "music"): [
                                {
                                    "modality": "music",
                                    "task": "sound_generation" if sound_mode else "music_generation",
                                    "provider": "stable-audio" if sound_mode else "acemusic",
                                    "model": "stabilityai/stable-audio-open-small" if sound_mode else "ace-step",
                                    "content_type": "audio/wav",
                                    "format": "wav",
                                    "artifact_ref": {
                                        "$artifact": meta.artifact_id,
                                        "artifact_id": meta.artifact_id,
                                        "content_type": "audio/wav",
                                        "size_bytes": len(_WAV_BYTES),
                                    },
                                }
                            ]
                        }
                    }
                },
            )

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_run_facade", lambda: (StubRunFacade(), None))
    monkeypatch.setattr(
        gateway_routes,
        "_gateway_music_capability_probe",
        lambda: {
            "available": True,
            "task": "text_to_music",
            "providers": ["acemusic"],
            "available_providers": ["acemusic"],
            "provider_details": [{"provider": "acemusic", "tasks": ["text_to_music"]}],
        },
    )

    from abstractgateway.app import app

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        start = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": "music-contract", "bundle_version": "0.0.0", "flow_id": "root", "input_data": {}},
            headers=headers,
        )
        assert start.status_code == 200, start.text
        run_id = start.json()["run_id"]

        _wait_until(lambda: client.get(f"/api/gateway/runs/{run_id}", headers=headers).json().get("status") == "completed")

        caps = client.get("/api/gateway/discovery/capabilities", headers=headers)
        assert caps.status_code == 200, caps.text
        direct = caps.json()["capabilities"]["contracts"]["assistant"]["media"]["generated_music"]["direct_endpoint"]
        assert direct["route_available"] is True
        assert direct["available"] is True
        assert direct["endpoint"] == "/api/gateway/runs/{run_id}/music/generate"
        assert direct["providers_endpoint"] == "/api/gateway/audio/music/providers"
        assert direct["provider_models_endpoint"] == "/api/gateway/audio/music/models"
        assert direct["durability"] == "runtime_child_run"
        assert direct["returns_child_run_id"] is True

        generated = client.post(
            f"/api/gateway/runs/{run_id}/music/generate",
            json={
                "prompt": "warm lo-fi piano with brushed drums",
                "provider": "stub",
                "model": "stub-chat",
                "music_provider": "acemusic",
                "music_model": "ace-step",
                "duration_s": 8,
                "format": "wav",
                "request_id": "music-1",
            },
            headers=headers,
        )
        assert generated.status_code == 200, generated.text
        body = generated.json()
        assert body["ok"] is True, body
        assert body["supported"] is True
        assert body["child_run_id"] == "child-music-1"
        music_ref = body["music_artifact"]
        assert music_ref["content_type"] == "audio/wav"
        assert music_ref["filename"] == "music.wav"
        assert music_ref["size_bytes"] == len(_WAV_BYTES)
        assert music_ref["sha256"]

        artifact_id = music_ref["$artifact"]
        content = client.get(f"/api/gateway/runs/{run_id}/artifacts/{artifact_id}/content", headers=headers)
        assert content.status_code == 200, content.text
        assert content.content == _WAV_BYTES

        sound = client.post(
            f"/api/gateway/runs/{run_id}/music/generate",
            json={
                "prompt": "short office notification chime",
                "task": "text_to_audio",
                "music_provider": "stable-audio",
                "music_model": "stabilityai/stable-audio-open-small",
                "format": "wav",
                "request_id": "sound-1",
            },
            headers=headers,
        )
        assert sound.status_code == 200, sound.text
        sound_body = sound.json()
        assert sound_body["ok"] is True, sound_body
        assert sound_body["child_run_id"] == "child-sound-1"
        assert sound_body["music_artifact"]["filename"] == "sound.wav"

        rejected = client.post(
            f"/api/gateway/runs/{run_id}/music/generate",
            json={
                "prompt": "warm lo-fi piano with brushed drums",
                "music_backend": "acemusic",
                "format": "wav",
            },
            headers=headers,
        )
        assert rejected.status_code == 422, rejected.text

        ledger = client.get(f"/api/gateway/runs/{run_id}/ledger?after=0&limit=200", headers=headers)
        assert ledger.status_code == 200, ledger.text
        events = [item for item in ledger.json().get("items", []) if isinstance(item, dict)]
        assert not any(
            (((item.get("effect") or {}).get("payload") or {}).get("name") == "abstract.media.music.generated")
            for item in events
        )
