from __future__ import annotations

import json
from pathlib import Path
import zipfile

import pytest
from fastapi.testclient import TestClient


def _write_test_bundle(*, bundles_dir: Path) -> tuple[str, str]:
    bundles_dir.mkdir(parents=True, exist_ok=True)
    bundle_id = "bundle-artifacts"
    flow_id = "flow"

    flow = {
        "id": flow_id,
        "name": "test",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "node-1",
                "type": "on_flow_start",
                "position": {"x": 32.0, "y": 224.0},
                "data": {"nodeType": "on_flow_start", "label": "On Flow Start", "inputs": [], "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]},
            },
            {
                "id": "node-2",
                "type": "ask_user",
                "position": {"x": 288.0, "y": 224.0},
                "data": {
                    "nodeType": "ask_user",
                    "label": "Ask User",
                    "inputs": [{"id": "exec-in", "label": "", "type": "execution"}],
                    "outputs": [
                        {"id": "exec-out", "label": "", "type": "execution"},
                        {"id": "response", "label": "response", "type": "string"},
                    ],
                    "pinDefaults": {"prompt": "hello"},
                },
            },
        ],
        "edges": [
            {
                "id": "edge-1",
                "source": "node-1",
                "sourceHandle": "exec-out",
                "target": "node-2",
                "targetHandle": "exec-in",
            }
        ],
        "entryNode": "node-1",
    }
    flow_bytes = json.dumps(flow, indent=2).encode("utf-8")

    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-01-12T00:00:00+00:00",
        "entrypoints": [{"flow_id": flow_id, "name": "test", "description": "", "interfaces": []}],
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }
    bundle_path = bundles_dir / f"{bundle_id}.flow"
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr(f"flows/{flow_id}.json", flow_bytes)

    return bundle_id, flow_id


def test_gateway_artifacts_api_list_metadata_and_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    bundle_id, flow_id = _write_test_bundle(bundles_dir=bundles_dir)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    from abstractgateway.app import app
    from abstractruntime.storage.artifacts import FileArtifactStore

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": bundle_id, "flow_id": flow_id, "input_data": {}},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]

        store = FileArtifactStore(runtime_dir)
        meta = store.store(b"hello", content_type="text/plain", run_id=run_id)
        meta2 = store.store(b"hello-2", content_type="text/plain", run_id=run_id)
        meta3 = store.store(b"hello-3", content_type="audio/wav", run_id=run_id, tags={"modality": "voice", "task": "tts"})
        meta4 = store.store(b"print('hello')\n", content_type="text/x-python", run_id=run_id, tags={"kind": "source_code"})
        artifact_id = meta.artifact_id

        resp = client.get(f"/api/gateway/runs/{run_id}/artifacts", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        items = body.get("items")
        assert isinstance(items, list)
        assert body.get("total", 0) >= 4
        assert body.get("has_more") is False
        assert any(it.get("artifact_id") == artifact_id for it in items)

        resp = client.get(f"/api/gateway/runs/{run_id}/artifacts", params={"limit": 1, "offset": 1}, headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("total", 0) >= 3
        assert body.get("limit") == 1
        assert body.get("offset") == 1
        assert body.get("has_more") is True
        assert len(body.get("items") or []) == 1

        resp = client.get("/api/gateway/artifacts/search", params={"scope": "all", "limit": 0}, headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("total", 0) == len(body.get("items") or [])
        assert body.get("total", 0) >= 4
        assert body.get("has_more") is False
        assert any(it.get("artifact_id") == meta3.artifact_id and it.get("modality") in {"audio", "voice"} for it in body.get("items") or [])

        resp = client.get("/api/gateway/artifacts/search", params={"scope": "all", "artifact_kind": "code", "limit": 10}, headers=headers)
        assert resp.status_code == 200, resp.text
        code_items = resp.json().get("items") or []
        assert any(it.get("artifact_id") == meta4.artifact_id and it.get("semantic_kind") == "code" for it in code_items)

        resp = client.get(f"/api/gateway/runs/{run_id}/artifacts/{artifact_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        meta_out = resp.json()
        assert meta_out.get("artifact_id") == artifact_id
        assert meta_out.get("run_id") == run_id
        assert meta_out.get("content_type") == "text/plain"

        resp = client.get(f"/api/gateway/runs/{run_id}/artifacts/{artifact_id}/content", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.content == b"hello"

        other = store.store(b"world", content_type="text/plain", run_id="other-run")
        resp = client.get(f"/api/gateway/runs/{run_id}/artifacts/{other.artifact_id}", headers=headers)
        assert resp.status_code == 404


def test_gateway_artifact_envelopes_stats_filters_and_access_stats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    bundle_id, flow_id = _write_test_bundle(bundles_dir=bundles_dir)

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    from abstractgateway.app import app
    from abstractruntime.storage.artifacts import ArtifactDescriptor, FileArtifactStore

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        r = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": bundle_id, "flow_id": flow_id, "session_id": "obs-session", "input_data": {}},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        run_id = r.json()["run_id"]

        store = FileArtifactStore(runtime_dir)
        music_1 = store.store(
            b"music-1",
            content_type="audio/wav",
            run_id=run_id,
            tags={"filename": "song-a.wav", "task": "music_generation"},
            descriptor=ArtifactDescriptor(
                semantic_kind="music",
                render_kind="audio",
                modality="music",
                task="music_generation",
                classification_source="producer",
                session_id="obs-session",
                workflow_id="wf_music",
                node_id="call",
                turn_id="turn-1",
                generation={"prompt": "slow piano"},
                producer={"provider": "abstractcore", "model": "music-test"},
                media={"duration_s": 30.0, "sample_rate": 44100},
            ),
        )
        music_2 = store.store(
            b"music-2",
            content_type="audio/wav",
            run_id=run_id,
            tags={"filename": "song-b.wav", "task": "music_generation"},
            descriptor={
                "semantic_kind": "music",
                "render_kind": "audio",
                "modality": "music",
                "task": "music_generation",
                "classification_source": "producer",
                "session_id": "obs-session",
                "workflow_id": "wf_music",
                "node_id": "call",
                "turn_id": "turn-2",
                "links": {
                    "provider_trace": "https://provider.example/traces/unsafe-open",
                    "audit": "/api/gateway/audit",
                },
            },
        )
        voice = store.store(
            b"voice",
            content_type="audio/wav",
            run_id=run_id,
            tags={"filename": "speech.wav", "task": "tts"},
            descriptor={
                "semantic_kind": "voice",
                "render_kind": "audio",
                "modality": "voice",
                "task": "tts",
                "classification_source": "producer",
                "session_id": "obs-session",
                "workflow_id": "wf_voice",
                "node_id": "tts",
            },
        )
        markdown = store.store(
            b"# report\n\nGenerated note.",
            content_type="text/markdown",
            run_id=run_id,
            tags={"filename": "report.md", "task": "evidence"},
            descriptor={
                "semantic_kind": "markdown",
                "render_kind": "markdown",
                "modality": "text",
                "task": "evidence",
                "classification_source": "producer",
                "session_id": "obs-session",
                "workflow_id": "wf_report",
                "node_id": "write_report",
            },
        )

        resp = client.get(
            "/api/gateway/artifacts/search",
            params={"scope": "run", "run_id": run_id, "include_stats": "true", "limit": 1},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] >= 3
        assert len(body["items"]) == 1
        assert body["has_more"] is True
        assert body.get("next_cursor")
        first_page_ids = {item.get("artifact_id") for item in body["items"]}
        cursor = body.get("next_cursor")
        stats = body.get("stats") or {}
        assert stats.get("total") >= 4
        assert stats.get("total_bytes", 0) >= len(b"music-1") + len(b"music-2") + len(b"voice") + len(b"# report\n\nGenerated note.")
        semantic_counts = ((stats.get("facets") or {}).get("semantic_kind") or {})
        assert semantic_counts.get("music") == 2
        assert semantic_counts.get("voice") == 1
        assert semantic_counts.get("markdown") == 1

        row = next((item for item in body["items"] if item.get("artifact_id") == music_2.artifact_id), body["items"][0])
        envelope = row.get("artifact_envelope_v1") or {}
        assert envelope.get("schema") == "abstractgateway.artifact_envelope.v1"
        assert row.get("semantic_kind") in {"music", "voice", "markdown"}
        assert row.get("render_kind") in {"audio", "markdown"}
        assert isinstance(envelope.get("links"), dict)

        resp = client.get(
            "/api/gateway/artifacts/search",
            params={"scope": "run", "run_id": run_id, "include_stats": "true", "limit": 1, "cursor": cursor},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] >= 4
        assert len(body["items"]) == 1
        assert {item.get("artifact_id") for item in body["items"]}.isdisjoint(first_page_ids)
        assert body.get("offset") == 0

        resp = client.get(
            "/api/gateway/artifacts/search",
            params={"scope": "run", "run_id": run_id, "include_stats": "true", "limit": 1, "offset": 1},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["offset"] == 1
        assert len(body["items"]) == 1
        assert body.get("stats", {}).get("total") >= 4
        assert body.get("has_more") is True

        resp = client.get(
            "/api/gateway/artifacts/search",
            params={"scope": "run", "run_id": run_id, "semantic_kind": "music", "include_stats": "true", "limit": 10},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 2
        assert body.get("stats", {}).get("total_bytes") == len(b"music-1") + len(b"music-2")
        assert {item.get("artifact_id") for item in body["items"]} == {music_1.artifact_id, music_2.artifact_id}
        assert all((item.get("artifact_envelope_v1") or {}).get("semantic_kind") == "music" for item in body["items"])
        music_2_row = next(item for item in body["items"] if item.get("artifact_id") == music_2.artifact_id)
        music_2_links = (music_2_row.get("artifact_envelope_v1") or {}).get("links") or {}
        assert "provider_trace" not in music_2_links
        assert music_2_links.get("audit") == "/api/gateway/audit"

        resp = client.get(
            "/api/gateway/artifacts/search",
            params={"scope": "run", "run_id": run_id, "artifact_kind": "music", "include_stats": "true", "limit": 10},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 2
        assert body.get("stats", {}).get("source") == "artifact_catalog"
        assert "query_or_scope_requires_gateway_post_filter" not in (body.get("warnings") or [])
        assert {item.get("artifact_id") for item in body["items"]} == {music_1.artifact_id, music_2.artifact_id}

        resp = client.get(
            "/api/gateway/artifacts/search",
            params={"scope": "run", "run_id": run_id, "artifact_kind": "music,voice", "include_stats": "true", "limit": 10},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 3
        assert {item.get("artifact_id") for item in body["items"]} == {music_1.artifact_id, music_2.artifact_id, voice.artifact_id}

        resp = client.get(
            "/api/gateway/artifacts/search",
            params={"scope": "run", "run_id": run_id, "artifact_kind": "music,markdown", "include_stats": "true", "limit": 10},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 3
        assert {item.get("artifact_id") for item in body["items"]} == {music_1.artifact_id, music_2.artifact_id, markdown.artifact_id}

        resp = client.get(
            "/api/gateway/artifacts/search",
            params={"scope": "run", "run_id": run_id, "artifact_kind": "audio", "include_stats": "true", "limit": 10},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 0
        assert body["items"] == []

        metadata = client.get(f"/api/gateway/runs/{run_id}/artifacts/{music_1.artifact_id}", headers=headers)
        assert metadata.status_code == 200, metadata.text
        preview = client.get(
            f"/api/gateway/runs/{run_id}/artifacts/{music_1.artifact_id}/content",
            params={"access_action": "preview"},
            headers=headers,
        )
        assert preview.status_code == 200, preview.text
        download = client.get(
            f"/api/gateway/runs/{run_id}/artifacts/{music_1.artifact_id}/content",
            params={"access_action": "download"},
            headers=headers,
        )
        assert download.status_code == 200, download.text

        updated = FileArtifactStore(runtime_dir).get_metadata(music_1.artifact_id)
        assert updated is not None
        assert updated.access.metadata_access_count == 1
        assert updated.access.preview_count == 1
        assert updated.access.download_count == 1
        assert updated.access.access_count == 3


def test_gateway_projected_generated_media_preserves_descriptor_and_metadata(tmp_path: Path) -> None:
    from abstractgateway.routes.gateway import _gateway_project_artifact_to_parent_run
    from abstractruntime.storage.artifacts import ArtifactDescriptor, FileArtifactStore

    store = FileArtifactStore(tmp_path / "runtime")
    child_meta = store.store(
        b"wav-child",
        content_type="audio/wav",
        run_id="child-run",
        tags={"kind": "generated_media", "modality": "music", "task": "music_generation"},
        metadata={
            "schema": "abstractruntime.generated_media_metadata.v1",
            "generation": {"prompt": "slow piano", "requested_format": "wav"},
            "producer": {"provider": "acemusic", "model": "ace-step"},
        },
        descriptor=ArtifactDescriptor(
            semantic_kind="music",
            render_kind="audio",
            modality="music",
            task="music_generation",
            classification_source="producer",
            session_id="child-session",
            workflow_id="child_wf_music",
            node_id="child_call",
            generation={"prompt": "slow piano", "requested_format": "wav"},
            producer={"provider": "acemusic", "model": "ace-step"},
            provenance={"source": "llm_call", "run_id": "child-run", "request_id": "req-child"},
        ),
    )

    projected_id, projected_meta, projected_blob = _gateway_project_artifact_to_parent_run(
        store=store,
        artifact_id=child_meta.artifact_id,
        run_id="parent-run",
        content_type="audio/wav",
        tags={"source": "gateway_direct_music", "session_id": "parent-session", "workflow_id": "parent_wf", "node_id": "parent_node"},
    )

    assert projected_id != child_meta.artifact_id
    assert projected_blob == b"wav-child"
    assert projected_meta.run_id == "parent-run"
    assert projected_meta.metadata["schema"] == "abstractruntime.generated_media_metadata.v1"
    assert projected_meta.metadata["generation"]["prompt"] == "slow piano"
    assert projected_meta.descriptor.semantic_kind == "music"
    assert projected_meta.descriptor.render_kind == "audio"
    assert projected_meta.descriptor.session_id == "parent-session"
    assert projected_meta.descriptor.workflow_id == "parent_wf"
    assert projected_meta.descriptor.node_id == "parent_node"
    assert projected_meta.descriptor.generation["prompt"] == "slow piano"
    assert projected_meta.descriptor.producer["provider"] == "acemusic"
    assert projected_meta.descriptor.provenance["run_id"] == "parent-run"
    assert projected_meta.descriptor.provenance["projected_from_run_id"] == "child-run"
    assert projected_meta.descriptor.provenance["projected_from_artifact_id"] == child_meta.artifact_id
    assert any(
        ref.get("role") == "projected_from" and ref.get("artifact_id") == child_meta.artifact_id
        for ref in projected_meta.descriptor.source_refs
    )


def test_gateway_artifact_import_session_list_export_and_run_start_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    bundle_id, flow_id = _write_test_bundle(bundles_dir=bundles_dir)

    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "input.txt").write_text("artifact handoff\n", encoding="utf-8")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKSPACE_DIR", str(workspace))
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    from abstractgateway.app import app
    from abstractruntime.storage.artifacts import FileArtifactStore

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        imported = client.post(
            "/api/gateway/artifacts/import",
            json={
                "session_id": "s-artifacts",
                "source": {"kind": "workspace_path", "path": "input.txt"},
                "pin_id": "image",
            },
            headers=headers,
        )
        assert imported.status_code == 200, imported.text
        body = imported.json()
        ref = body.get("artifact") or {}
        assert isinstance(ref, dict)
        artifact_id = ref.get("$artifact")
        assert isinstance(artifact_id, str) and artifact_id
        assert ref.get("artifact_id") == artifact_id
        assert ref.get("run_id") == "session_memory_s-artifacts"
        assert ref.get("source_path") == "input.txt"
        assert ref.get("content_type") == "text/plain"

        listed = client.get("/api/gateway/sessions/s-artifacts/artifacts", headers=headers)
        assert listed.status_code == 200, listed.text
        items = listed.json().get("items") or []
        found = next((item for item in items if item.get("artifact_id") == artifact_id), None)
        assert isinstance(found, dict)
        assert found.get("run_id") == "session_memory_s-artifacts"
        assert (found.get("ref") or {}).get("$artifact") == artifact_id

        searched = client.get(
            "/api/gateway/artifacts/search",
            params={"scope": "all", "modality": "text", "query": "input.txt", "tags": '{"pin_id":"image"}'},
            headers=headers,
        )
        assert searched.status_code == 200, searched.text
        search_items = searched.json().get("items") or []
        assert any(item.get("artifact_id") == artifact_id for item in search_items)

        session_search = client.get(
            "/api/gateway/artifacts/search",
            params={"scope": "session", "session_id": "s-artifacts", "content_type": "text/*"},
            headers=headers,
        )
        assert session_search.status_code == 200, session_search.text
        assert any(item.get("artifact_id") == artifact_id for item in session_search.json().get("items") or [])

        producer = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": bundle_id, "flow_id": flow_id, "session_id": "producer-session", "input_data": {}},
            headers=headers,
        )
        assert producer.status_code == 200, producer.text
        producer_run_id = producer.json()["run_id"]
        prior_meta = FileArtifactStore(runtime_dir).store(
            b"png",
            content_type="image/png",
            run_id=producer_run_id,
            tags={"filename": "prior.png", "modality": "image"},
        )
        prior_ref = {
            "$artifact": prior_meta.artifact_id,
            "artifact_id": prior_meta.artifact_id,
            "run_id": producer_run_id,
            "content_type": "image/png",
            "modality": "image",
            "filename": "prior.png",
        }
        prior_search = client.get(
            "/api/gateway/artifacts/search",
            params={"scope": "all", "modality": "image", "query": "prior.png"},
            headers=headers,
        )
        assert prior_search.status_code == 200, prior_search.text
        assert any(item.get("artifact_id") == prior_meta.artifact_id for item in prior_search.json().get("items") or [])

        started = client.post(
            "/api/gateway/runs/start",
            json={
                "bundle_id": bundle_id,
                "flow_id": flow_id,
                "session_id": "s-artifacts",
                "input_data": {"image": ref},
            },
            headers=headers,
        )
        assert started.status_code == 200, started.text

        explicit_prior_ref = client.post(
            "/api/gateway/runs/start",
            json={
                "bundle_id": bundle_id,
                "flow_id": flow_id,
                "session_id": "s-artifacts",
                "input_data": {"image": prior_ref},
            },
            headers=headers,
        )
        assert explicit_prior_ref.status_code == 200, explicit_prior_ref.text

        prior_artifact_run_ref = dict(prior_ref)
        prior_artifact_run_ref["artifact_run_id"] = prior_artifact_run_ref.pop("run_id")
        explicit_prior_artifact_run_ref = client.post(
            "/api/gateway/runs/start",
            json={
                "bundle_id": bundle_id,
                "flow_id": flow_id,
                "session_id": "s-artifacts",
                "input_data": {"image": prior_artifact_run_ref},
            },
            headers=headers,
        )
        assert explicit_prior_artifact_run_ref.status_code == 200, explicit_prior_artifact_run_ref.text

        explicit_session_memory_ref = client.post(
            "/api/gateway/runs/start",
            json={
                "bundle_id": bundle_id,
                "flow_id": flow_id,
                "session_id": "other-session",
                "input_data": {"image": ref},
            },
            headers=headers,
        )
        assert explicit_session_memory_ref.status_code == 200, explicit_session_memory_ref.text

        rejected = client.post(
            "/api/gateway/runs/start",
            json={
                "bundle_id": bundle_id,
                "flow_id": flow_id,
                "session_id": "other-session",
                "input_data": {"image": {"$artifact": artifact_id, "artifact_id": artifact_id}},
            },
            headers=headers,
        )
        assert rejected.status_code == 404, rejected.text
        assert "not visible to session" in rejected.json().get("detail", "")

        exported = client.post(
            f"/api/gateway/runs/session_memory_s-artifacts/artifacts/{artifact_id}/export",
            json={"path": "exports/copy.txt", "create_parent_dirs": True},
            headers=headers,
        )
        assert exported.status_code == 200, exported.text
        assert (workspace / "exports" / "copy.txt").read_text(encoding="utf-8") == "artifact handoff\n"

        blocked_overwrite = client.post(
            f"/api/gateway/runs/session_memory_s-artifacts/artifacts/{artifact_id}/export",
            json={"path": "exports/copy.txt"},
            headers=headers,
        )
        assert blocked_overwrite.status_code == 409, blocked_overwrite.text
