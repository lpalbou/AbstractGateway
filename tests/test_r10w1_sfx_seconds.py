"""R10.1 (2026-10-04): the gateway SFX route takes the clip length (`seconds`) and hands it to the
runtime as the output spec's `duration_s`, with the route's model.

Before: the route had no `seconds`, the Sandbox sent no length, and every sound effect came back
30 s long ("laser gunshot" in the console Sandbox and in the Assistant).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

from fastapi.testclient import TestClient

from test_generated_media_gateway_contract import _WAV_BYTES, _wait_until, _write_image_bundle


SFX_MODEL = "stabilityai/stable-audio-3-small-sfx"


def _client(tmp_path: Path, monkeypatch, outputs: List[Dict[str, Any]]):
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_image_bundle(bundles_dir=bundles_dir, bundle_id="sfx-seconds", flow_id="root")
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    import abstractgateway.routes.gateway as gateway_routes
    from abstractruntime.core.models import RunStatus

    class StubRunFacade:
        def generate_music(self, parent_run_id, *, prompt, output, params, child_vars=None):
            outputs.append(dict(output))
            store = gateway_routes.get_gateway_service().stores.artifact_store
            meta = store.store(_WAV_BYTES, content_type="audio/wav", run_id="child-sfx", tags=output.get("tags") or {})
            ref = {"$artifact": meta.artifact_id, "artifact_id": meta.artifact_id, "content_type": "audio/wav"}
            item = {"modality": "sound", "task": "sound_generation", "content_type": "audio/wav", "artifact_ref": ref}
            return SimpleNamespace(
                run_id="child-sfx",
                status=RunStatus.COMPLETED,
                error=None,
                output={"result": {"outputs": {"sound": [item]}}},
            )

    monkeypatch.setattr(gateway_routes, "_gateway_abstractcore_run_facade", lambda: (StubRunFacade(), None))
    from abstractgateway.app import app

    return TestClient(app), {"Authorization": "Bearer t"}


def _start(client, headers) -> str:
    start = client.post(
        "/api/gateway/runs/start",
        json={"bundle_id": "sfx-seconds", "bundle_version": "0.0.0", "flow_id": "root", "input_data": {}},
        headers=headers,
    )
    assert start.status_code == 200, start.text
    run_id = start.json()["run_id"]
    _wait_until(lambda: client.get(f"/api/gateway/runs/{run_id}", headers=headers).json().get("status") == "completed")
    return run_id


def test_sfx_route_passes_seconds_and_the_route_model(tmp_path: Path, monkeypatch) -> None:
    outputs: List[Dict[str, Any]] = []
    client, headers = _client(tmp_path, monkeypatch, outputs)
    with client:
        run_id = _start(client, headers)
        resp = client.post(
            f"/api/gateway/runs/{run_id}/music/generate",
            json={
                "prompt": "laser gunshot",
                "task": "text_to_audio",
                "seconds": 3,
                "music_provider": "stable-audio-3",
                "music_model": SFX_MODEL,
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["ok"] is True, resp.json()
        assert outputs[-1]["duration_s"] == 3.0
        assert outputs[-1]["task"] == "text_to_audio"
        assert outputs[-1]["model"] == SFX_MODEL
        assert outputs[-1]["provider"] == "stable-audio-3"

        # No length: nothing is invented here; the engine applies 5 s (SFX) / 30 s (music).
        resp = client.post(
            f"/api/gateway/runs/{run_id}/music/generate",
            json={"prompt": "laser gunshot", "task": "text_to_audio", "music_provider": "stable-audio-3"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert "duration_s" not in outputs[-1]


def test_sfx_route_refuses_a_bad_length(tmp_path: Path, monkeypatch) -> None:
    outputs: List[Dict[str, Any]] = []
    client, headers = _client(tmp_path, monkeypatch, outputs)
    with client:
        run_id = _start(client, headers)
        url = f"/api/gateway/runs/{run_id}/music/generate"
        for body, status in (
            ({"seconds": 0}, 422),
            ({"seconds": -1}, 422),
            ({"seconds": 4000}, 422),
            ({"seconds": "three"}, 422),
            ({"seconds": 3, "duration_s": 4}, 400),
        ):
            resp = client.post(url, json={"prompt": "laser gunshot", "task": "text_to_audio", **body}, headers=headers)
            assert resp.status_code == status, (body, resp.text)
            assert "seconds" in resp.text, resp.text
        assert outputs == []
