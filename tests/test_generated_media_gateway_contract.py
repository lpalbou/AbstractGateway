from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi.testclient import TestClient


_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\xf8\x0f"
    b"\x00\x01\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)


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


class _StubImageGatewayLLMClient:
    def __init__(self, provider: str, model: str, llm_kwargs: Optional[Dict[str, Any]] = None, artifact_store: Any = None):
        self.provider = provider
        self.model = model
        self.artifact_store = artifact_store
        self.calls: list[Dict[str, Any]] = []
        self._llm = self
        _ = llm_kwargs

    def get_model_capabilities(self) -> Dict[str, Any]:
        return {"max_tokens": 1024, "max_output_tokens": 256}

    def generate(
        self,
        *,
        prompt: str,
        messages: Any = None,
        system_prompt: Any = None,
        tools: Any = None,
        media: Any = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        _ = (messages, system_prompt, tools, media)
        params = dict(params or {})
        self.calls.append({"prompt": prompt, "params": params})
        output = params.get("output")
        assert isinstance(output, dict)
        assert output.get("modality") == "image"
        assert output.get("task") == "image_generation"
        run_id = str(output.get("run_id") or "")
        tags = output.get("tags") if isinstance(output.get("tags"), dict) else {}
        meta = self.artifact_store.store(_PNG_BYTES, content_type="image/png", run_id=run_id, tags=tags)
        return {
            "outputs": {
                "image": [
                    {
                        "modality": "image",
                        "task": "image_generation",
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
            },
            "metadata": {"provider": "stub", "model": "stub-image"},
            "model": "stub-image",
        }


def _wait_until(fn, *, timeout_s: float = 5.0, poll_s: float = 0.05) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if fn():
            return
        time.sleep(poll_s)
    raise AssertionError("condition did not become true before timeout")


def test_gateway_direct_image_generation_stores_artifact_and_emits_event(tmp_path: Path, monkeypatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_image_bundle(bundles_dir=bundles_dir, bundle_id="image-contract", flow_id="root")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    from abstractruntime.integrations.abstractcore import factory as ac_factory

    monkeypatch.setattr(ac_factory, "MultiLocalAbstractCoreLLMClient", _StubImageGatewayLLMClient)

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
        assert direct["endpoint"] == "/api/gateway/runs/{run_id}/images/generate"
        assert direct["event_name"] == "abstract.media.image.generated"

        generated = client.post(
            f"/api/gateway/runs/{run_id}/images/generate",
            json={
                "prompt": "a one pixel generated test image",
                "provider": "stub",
                "model": "stub-image",
                "format": "png",
                "request_id": "img-1",
            },
            headers=headers,
        )
        assert generated.status_code == 200, generated.text
        body = generated.json()
        assert body["ok"] is True, body
        assert body["supported"] is True
        assert body["event_name"] == "abstract.media.image.generated"
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
        assert any(
            (((item.get("effect") or {}).get("payload") or {}).get("name") == "abstract.media.image.generated")
            for item in events
        )
