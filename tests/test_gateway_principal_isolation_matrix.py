from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _write_min_bundle(*, bundles_dir: Path, bundle_id: str = "iso-bundle", flow_id: str = "root") -> None:
    bundles_dir.mkdir(parents=True, exist_ok=True)
    flow = {
        "id": flow_id,
        "name": "isolation",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "start",
                "type": "on_flow_start",
                "position": {"x": 32.0, "y": 128.0},
                "data": {
                    "nodeType": "on_flow_start",
                    "label": "On Flow Start",
                    "inputs": [],
                    "outputs": [{"id": "exec-out", "label": "", "type": "execution"}],
                },
            },
            {
                "id": "end",
                "type": "on_flow_end",
                "position": {"x": 288.0, "y": 128.0},
                "data": {
                    "nodeType": "on_flow_end",
                    "label": "On Flow End",
                    "inputs": [{"id": "exec-in", "label": "", "type": "execution"}],
                    "outputs": [],
                },
            },
        ],
        "edges": [{"id": "edge-1", "source": "start", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"}],
        "entryNode": "start",
    }
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-05-30T00:00:00+00:00",
        "entrypoints": [{"flow_id": flow_id, "name": "root", "description": "", "interfaces": []}],
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }
    with zipfile.ZipFile(bundles_dir / f"{bundle_id}.flow", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr(f"flows/{flow_id}.json", json.dumps(flow, indent=2))


def _visualflow_payload(*, name: str = "private flow") -> dict:
    return {
        "name": name,
        "description": "Private VisualFlow used by the principal isolation matrix.",
        "interfaces": [],
        "nodes": [
            {
                "id": "start",
                "type": "on_flow_start",
                "position": {"x": 32.0, "y": 128.0},
                "data": {
                    "nodeType": "on_flow_start",
                    "label": "On Flow Start",
                    "inputs": [],
                    "outputs": [{"id": "exec-out", "label": "", "type": "execution"}],
                },
            },
            {
                "id": "end",
                "type": "on_flow_end",
                "position": {"x": 288.0, "y": 128.0},
                "data": {
                    "nodeType": "on_flow_end",
                    "label": "On Flow End",
                    "inputs": [{"id": "exec-in", "label": "", "type": "execution"}],
                    "outputs": [],
                },
            },
        ],
        "edges": [{"id": "edge-1", "source": "start", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"}],
        "entryNode": "start",
    }


def _wait_until(predicate, *, timeout_s: float = 5.0, poll_s: float = 0.05) -> None:
    end = time.time() + timeout_s
    while time.time() < end:
        if predicate():
            return
        time.sleep(poll_s)
    raise AssertionError("timeout waiting for condition")


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "admin-token")
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")

    from abstractgateway.app import app

    return TestClient(app)


def _create_user(client: TestClient, user_id: str) -> dict[str, str]:
    response = client.post(
        "/api/gateway/admin/users",
        headers={"Authorization": "Bearer admin-token"},
        json={"user_id": user_id, "tenant_id": "default", "roles": ["user"], "runtime_id": user_id},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_alice_bob_runs_ledgers_and_artifacts_are_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch)
    with client:
        alice_headers = _create_user(client, "alice")
        bob_headers = _create_user(client, "bob")

        alice_flows = tmp_path / "runtime" / "users" / "default" / "alice" / "flows"
        _write_min_bundle(bundles_dir=alice_flows)

        start = client.post(
            "/api/gateway/runs/start",
            headers=alice_headers,
            json={"bundle_id": "iso-bundle", "flow_id": "root", "session_id": "alice-session", "input_data": {"prompt": "secret"}},
        )
        assert start.status_code == 200, start.text
        alice_run_id = start.json()["run_id"]

        def _alice_run_visible() -> bool:
            run = client.get(f"/api/gateway/runs/{alice_run_id}", headers=alice_headers)
            assert run.status_code == 200, run.text
            return str(run.json().get("status") or "") in {"completed", "running", "waiting"}

        _wait_until(_alice_run_visible)

        from abstractruntime.storage.artifacts import FileArtifactStore

        alice_runtime = tmp_path / "runtime" / "users" / "default" / "alice" / "runtime"
        artifact = FileArtifactStore(alice_runtime).store(
            b"alice artifact",
            content_type="text/plain",
            run_id=alice_run_id,
            tags={"owner": "alice", "kind": "isolation"},
        )

        alice_runs = client.get("/api/gateway/runs?limit=25&include_drafts=true", headers=alice_headers)
        bob_runs = client.get("/api/gateway/runs?limit=25&include_drafts=true", headers=bob_headers)
        assert alice_runs.status_code == 200, alice_runs.text
        assert bob_runs.status_code == 200, bob_runs.text
        assert any(item.get("run_id") == alice_run_id for item in alice_runs.json().get("items") or [])
        assert all(item.get("run_id") != alice_run_id for item in bob_runs.json().get("items") or [])

        bob_run_detail = client.get(f"/api/gateway/runs/{alice_run_id}", headers=bob_headers)
        bob_history = client.get(f"/api/gateway/runs/{alice_run_id}/history_bundle", headers=bob_headers)
        bob_input = client.get(f"/api/gateway/runs/{alice_run_id}/input_data", headers=bob_headers)
        bob_ledger = client.get(f"/api/gateway/runs/{alice_run_id}/ledger?after=0&limit=25", headers=bob_headers)
        bob_ledger_stream = client.get(f"/api/gateway/runs/{alice_run_id}/ledger/stream?after=0&heartbeat_s=0.2", headers=bob_headers)
        assert bob_run_detail.status_code == 404
        assert bob_history.status_code == 404
        assert bob_input.status_code == 404
        assert bob_ledger.status_code == 404
        assert bob_ledger_stream.status_code == 404

        batch = client.post(
            "/api/gateway/runs/ledger/batch",
            headers=bob_headers,
            json={"runs": [{"run_id": alice_run_id, "after": 0}], "limit": 25},
        )
        assert batch.status_code == 200, batch.text
        assert alice_run_id not in (batch.json().get("runs") or {})

        alice_artifact = client.get(f"/api/gateway/runs/{alice_run_id}/artifacts/{artifact.artifact_id}", headers=alice_headers)
        bob_artifact = client.get(f"/api/gateway/runs/{alice_run_id}/artifacts/{artifact.artifact_id}", headers=bob_headers)
        bob_artifact_content = client.get(
            f"/api/gateway/runs/{alice_run_id}/artifacts/{artifact.artifact_id}/content",
            headers=bob_headers,
        )
        assert alice_artifact.status_code == 200, alice_artifact.text
        assert bob_artifact.status_code == 404
        assert bob_artifact_content.status_code == 404

        alice_search = client.get("/api/gateway/artifacts/search?scope=all&query=alice", headers=alice_headers)
        bob_search = client.get("/api/gateway/artifacts/search?scope=all&query=alice", headers=bob_headers)
        assert alice_search.status_code == 200, alice_search.text
        assert bob_search.status_code == 200, bob_search.text
        assert any(item.get("artifact_id") == artifact.artifact_id for item in alice_search.json().get("items") or [])
        assert all(item.get("artifact_id") != artifact.artifact_id for item in bob_search.json().get("items") or [])


def test_alice_bob_workflows_defaults_session_artifacts_and_prompt_cache_are_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(tmp_path, monkeypatch)
    with client:
        alice_headers = _create_user(client, "alice")
        bob_headers = _create_user(client, "bob")

        alice_flows = tmp_path / "runtime" / "users" / "default" / "alice" / "flows"
        _write_min_bundle(bundles_dir=alice_flows, bundle_id="alice-private", flow_id="root")

        alice_bundles = client.get("/api/gateway/bundles", headers=alice_headers)
        bob_bundles = client.get("/api/gateway/bundles", headers=bob_headers)
        assert alice_bundles.status_code == 200, alice_bundles.text
        assert bob_bundles.status_code == 200, bob_bundles.text
        assert any(item.get("bundle_id") == "alice-private" for item in alice_bundles.json().get("items") or [])
        assert all(item.get("bundle_id") != "alice-private" for item in bob_bundles.json().get("items") or [])

        bob_private_bundle = client.get("/api/gateway/bundles/alice-private", headers=bob_headers)
        assert bob_private_bundle.status_code == 404

        create_flow = client.post("/api/gateway/visualflows", headers=alice_headers, json=_visualflow_payload(name="alice only"))
        assert create_flow.status_code == 200, create_flow.text
        alice_flow_id = str(create_flow.json().get("id") or "")
        assert alice_flow_id
        alice_flow = client.get(f"/api/gateway/visualflows/{alice_flow_id}", headers=alice_headers)
        bob_flow = client.get(f"/api/gateway/visualflows/{alice_flow_id}", headers=bob_headers)
        bob_flow_list = client.get("/api/gateway/visualflows", headers=bob_headers)
        assert alice_flow.status_code == 200, alice_flow.text
        assert bob_flow.status_code == 404
        assert bob_flow_list.status_code == 200, bob_flow_list.text
        assert all(item.get("id") != alice_flow_id for item in bob_flow_list.json())

        saved = client.put(
            "/api/gateway/config/capability-defaults/output/text",
            headers=alice_headers,
            json={"provider": "openrouter", "model": "alice-model"},
        )
        assert saved.status_code == 200, saved.text
        alice_defaults = client.get("/api/gateway/config/capability-defaults", headers=alice_headers)
        bob_defaults = client.get("/api/gateway/config/capability-defaults", headers=bob_headers)
        alice_text = [r for r in alice_defaults.json()["routes"] if r.get("key") == "output.text"][0]
        bob_text = [r for r in bob_defaults.json()["routes"] if r.get("key") == "output.text"][0]
        assert alice_text["provider"] == "openrouter"
        assert alice_text["model"] == "alice-model"
        assert bob_text.get("provider") != "openrouter"
        assert bob_text.get("model") != "alice-model"

        upload = client.post(
            "/api/gateway/attachments/upload",
            headers=alice_headers,
            data={"session_id": "shared-session"},
            files={"file": ("alice.txt", b"alice session attachment", "text/plain")},
        )
        assert upload.status_code == 200, upload.text
        alice_session_artifacts = client.get("/api/gateway/sessions/shared-session/artifacts", headers=alice_headers)
        bob_session_artifacts = client.get("/api/gateway/sessions/shared-session/artifacts", headers=bob_headers)
        assert alice_session_artifacts.status_code == 200, alice_session_artifacts.text
        assert bob_session_artifacts.status_code == 200, bob_session_artifacts.text
        uploaded_id = str((upload.json().get("artifact") or {}).get("artifact_id") or "")
        assert uploaded_id
        assert any(item.get("artifact_id") == uploaded_id for item in alice_session_artifacts.json().get("items") or [])
        assert all(item.get("artifact_id") != uploaded_id for item in bob_session_artifacts.json().get("items") or [])

        prompt_cache_params = {
            "provider": "stub-provider",
            "model": "stub-model",
            "bundle_id": "basic-agent",
            "flow_id": "root",
        }
        alice_cache = client.get(
            "/api/gateway/sessions/shared-session/prompt_cache/status",
            headers=alice_headers,
            params=prompt_cache_params,
        )
        bob_cache = client.get(
            "/api/gateway/sessions/shared-session/prompt_cache/status",
            headers=bob_headers,
            params=prompt_cache_params,
        )
        assert alice_cache.status_code == 200, alice_cache.text
        assert bob_cache.status_code == 200, bob_cache.text
        assert alice_cache.json()["prompt_cache_key"] != bob_cache.json()["prompt_cache_key"]
        assert alice_cache.json()["identity"] == bob_cache.json()["identity"]
        assert "_principal_scope" not in alice_cache.json()["identity"]


def test_alice_bob_kg_memory_is_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("abstractmemory")
    monkeypatch.setenv("ABSTRACTGATEWAY_MEMORY_STORE_BACKEND", "sqlite")

    client = _client(tmp_path, monkeypatch)
    with client:
        alice_headers = _create_user(client, "alice")
        bob_headers = _create_user(client, "bob")

        from abstractmemory import SQLiteTripleStore, TripleAssertion

        alice_runtime = tmp_path / "runtime" / "users" / "default" / "alice" / "runtime"
        store = SQLiteTripleStore(alice_runtime / "abstractmemory" / "kg.sqlite3")
        try:
            store.add(
                [
                    TripleAssertion(
                        subject="ex:alice-secret",
                        predicate="rdf:type",
                        object="schema:secret",
                        scope="session",
                        owner_id="session_memory_shared-session",
                    )
                ]
            )
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

        alice_kg = client.post(
            "/api/gateway/kg/query",
            headers=alice_headers,
            json={"scope": "session", "session_id": "shared-session", "limit": 25},
        )
        bob_kg = client.post(
            "/api/gateway/kg/query",
            headers=bob_headers,
            json={"scope": "session", "session_id": "shared-session", "limit": 25},
        )
        assert alice_kg.status_code == 200, alice_kg.text
        assert bob_kg.status_code == 200, bob_kg.text
        assert any(item.get("subject") == "ex:alice-secret" for item in alice_kg.json().get("items") or [])
        assert all(item.get("subject") != "ex:alice-secret" for item in bob_kg.json().get("items") or [])


def test_regular_user_discovery_and_workspace_helpers_do_not_advertise_admin_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(tmp_path, monkeypatch)
    with client:
        user_headers = _create_user(client, "user")
        admin_headers = {"Authorization": "Bearer admin-token"}

        user_caps = client.get("/api/gateway/discovery/capabilities", headers=user_headers)
        admin_caps = client.get("/api/gateway/discovery/capabilities", headers=admin_headers)
        assert user_caps.status_code == 200, user_caps.text
        assert admin_caps.status_code == 200, admin_caps.text

        user_common = user_caps.json()["capabilities"]["contracts"]["common"]
        admin_common = admin_caps.json()["capabilities"]["contracts"]["common"]
        assert user_common["artifacts"]["import"]["available"] is False
        assert user_common["artifacts"]["import"]["denied_reason"] == "admin_required"
        assert user_common["artifacts"]["export"]["available"] is False
        assert user_common["artifacts"]["export"]["resource_class"] == "workspace"
        assert user_common["prompt_cache"]["provider_controls_available"] is False
        assert user_common["prompt_cache"]["provider_controls_denied_reason"] == "admin_required"
        assert admin_common["artifacts"]["import"]["available"] is True
        assert admin_common["artifacts"]["export"]["available"] is True

        for method, path, kwargs in [
            ("get", "/api/gateway/files/search", {"params": {"query": "x"}}),
            ("get", "/api/gateway/files/read", {"params": {"path": "x"}}),
            ("get", "/api/gateway/files/skim", {"params": {"path": "x"}}),
            ("post", "/api/gateway/artifacts/import", {"json": {"sources": []}}),
            ("post", "/api/gateway/attachments/ingest", {"json": {"session_id": "s", "path": "x"}}),
        ]:
            response = getattr(client, method)(path, headers=user_headers, **kwargs)
            assert response.status_code == 403, f"{method.upper()} {path}: {response.text}"
            assert response.json()["resource_class"] == "workspace"
