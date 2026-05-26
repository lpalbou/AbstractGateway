from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_gateway_runs_list_endpoint import _wait_until, _write_min_bundle


def _configure_gateway(monkeypatch: pytest.MonkeyPatch, *, runtime_dir: Path, bundles_dir: Path, token: str, sqlite: bool = False) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")
    if sqlite:
        monkeypatch.setenv("ABSTRACTGATEWAY_STORE_BACKEND", "sqlite")
    else:
        monkeypatch.delenv("ABSTRACTGATEWAY_STORE_BACKEND", raising=False)


def _start_min_run(client: TestClient, headers: dict[str, str], *, lifecycle: dict | None = None) -> str:
    payload = {"bundle_id": "bundle-runs", "flow_id": "root", "input_data": {}}
    if lifecycle is not None:
        payload["run_lifecycle"] = lifecycle
    start = client.post("/api/gateway/runs/start", headers=headers, json=payload)
    assert start.status_code == 200, start.text
    return str(start.json()["run_id"])


def _wait_completed(client: TestClient, headers: dict[str, str], *run_ids: str) -> None:
    def _done() -> bool:
        for run_id in run_ids:
            rr = client.get(f"/api/gateway/runs/{run_id}", headers=headers)
            assert rr.status_code == 200, rr.text
            if rr.json().get("status") != "completed":
                return False
        return True

    _wait_until(_done, timeout_s=5.0, poll_s=0.05)


def _expired_draft_lifecycle() -> dict:
    return {
        "source": "abstractflow.editor",
        "purpose": "draft_test",
        "visibility": "private",
        "retention": {"mode": "ephemeral", "expires_at": "2000-01-01T00:00:00+00:00"},
    }


def test_purge_draft_runs_dry_run_then_delete_file_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-runs", flow_id="root")

    token = "t"
    _configure_gateway(monkeypatch, runtime_dir=runtime_dir, bundles_dir=bundles_dir, token=token)

    from abstractgateway.app import app
    from abstractgateway.service import get_gateway_service
    from abstractruntime.storage.commands import CommandRecord

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        published_run_id = _start_min_run(client, headers)
        draft_run_id = _start_min_run(client, headers, lifecycle=_expired_draft_lifecycle())
        _wait_completed(client, headers, published_run_id, draft_run_id)

        svc = get_gateway_service()
        draft_run = svc.host.run_store.load(draft_run_id)
        assert draft_run is not None
        workspace = Path(str(draft_run.vars["workspace_root"]))
        assert workspace.exists()
        assert (workspace / ".abstractgateway-workspace.json").exists()

        artifact = svc.stores.artifact_store.store(b"draft bytes", content_type="text/plain", run_id=draft_run_id)
        svc.stores.command_store.append(CommandRecord(command_id="cmd-draft", run_id=draft_run_id, type="pause", payload={}, ts="t"))

        dry = client.post("/api/gateway/runs/purge_drafts", headers=headers, json={"dry_run": True, "run_ids": [draft_run_id]})
        assert dry.status_code == 200, dry.text
        dry_body = dry.json()
        assert dry_body["eligible"] == 1
        assert dry_body["purged"] == 0
        assert client.get(f"/api/gateway/runs/{draft_run_id}", headers=headers).status_code == 200
        assert svc.stores.artifact_store.exists(artifact.artifact_id)
        assert workspace.exists()

        purge = client.post("/api/gateway/runs/purge_drafts", headers=headers, json={"dry_run": False, "run_ids": [draft_run_id]})
        assert purge.status_code == 200, purge.text
        body = purge.json()
        assert body["eligible"] == 1
        assert body["purged"] == 1
        deleted = body["items"][0]["purge"]["deleted"]
        assert deleted["runs"] == 1
        assert deleted["ledger_records"] >= 1
        assert deleted["commands"] == 1
        assert deleted["artifacts"] >= 1
        assert deleted["workspaces"] == 1

        assert client.get(f"/api/gateway/runs/{draft_run_id}", headers=headers).status_code == 404
        assert client.get(f"/api/gateway/runs/{draft_run_id}/history_bundle", headers=headers).status_code == 404
        assert client.get(f"/api/gateway/runs/{published_run_id}", headers=headers).status_code == 200
        assert svc.host.ledger_store.list(draft_run_id) == []
        assert svc.stores.artifact_store.list_by_run(draft_run_id) == []
        assert not svc.stores.artifact_store.exists(artifact.artifact_id)
        assert not workspace.exists()
        commands, _cursor = svc.stores.command_store.list_after(after=0, limit=10)
        assert all(c.run_id != draft_run_id for c in commands)


def test_purge_draft_runs_skips_unexpired_active_and_non_draft_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-runs", flow_id="root")

    token = "t"
    _configure_gateway(monkeypatch, runtime_dir=runtime_dir, bundles_dir=bundles_dir, token=token)

    from abstractgateway.app import app
    from abstractgateway.service import get_gateway_service
    from abstractruntime.core.models import RunState, RunStatus

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        future = RunState.new(workflow_id="wf", entry_node="start")
        future.run_id = "future_draft"
        future.status = RunStatus.COMPLETED
        future.vars["_run_lifecycle"] = {
            "purpose": "draft_test",
            "retention": {"mode": "ephemeral", "expires_at": "2999-01-01T00:00:00+00:00"},
        }

        active = RunState.new(workflow_id="wf", entry_node="start")
        active.run_id = "active_draft"
        active.status = RunStatus.WAITING
        active.vars["_run_lifecycle"] = _expired_draft_lifecycle()

        non_draft = RunState.new(workflow_id="wf", entry_node="start")
        non_draft.run_id = "non_draft"
        non_draft.status = RunStatus.COMPLETED
        non_draft.vars["_run_lifecycle"] = {
            "purpose": "published_run",
            "retention": {"mode": "ephemeral", "expires_at": "2000-01-01T00:00:00+00:00"},
        }

        svc = get_gateway_service()
        for run in (future, active, non_draft):
            svc.host.run_store.save(run)

        purge = client.post(
            "/api/gateway/runs/purge_drafts",
            headers=headers,
            json={"dry_run": False, "run_ids": ["future_draft", "active_draft", "non_draft"]},
        )
        assert purge.status_code == 200, purge.text
        body = purge.json()
        assert body["purged"] == 0
        reasons = {item["run_id"]: item["reason"] for item in body["items"]}
        assert reasons == {
            "future_draft": "not_expired",
            "active_draft": "active_run",
            "non_draft": "not_ephemeral_draft",
        }
        assert svc.host.run_store.load("future_draft") is not None
        assert svc.host.run_store.load("active_draft") is not None
        assert svc.host.run_store.load("non_draft") is not None


def test_purge_draft_runs_sqlite_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-runs", flow_id="root")

    token = "t"
    _configure_gateway(monkeypatch, runtime_dir=runtime_dir, bundles_dir=bundles_dir, token=token, sqlite=True)

    from abstractgateway.app import app
    from abstractgateway.service import get_gateway_service

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        draft_run_id = _start_min_run(client, headers, lifecycle=_expired_draft_lifecycle())
        _wait_completed(client, headers, draft_run_id)

        purge = client.post("/api/gateway/runs/purge_drafts", headers=headers, json={"dry_run": False, "run_ids": [draft_run_id]})
        assert purge.status_code == 200, purge.text
        assert purge.json()["purged"] == 1
        assert client.get(f"/api/gateway/runs/{draft_run_id}", headers=headers).status_code == 404

        svc = get_gateway_service()
        assert svc.host.run_store.load(draft_run_id) is None
        assert svc.host.ledger_store.list(draft_run_id) == []


def test_purge_draft_runs_does_not_delete_external_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-runs", flow_id="root")

    token = "t"
    _configure_gateway(monkeypatch, runtime_dir=runtime_dir, bundles_dir=bundles_dir, token=token)

    from abstractgateway.app import app
    from abstractgateway.service import get_gateway_service
    from abstractruntime.core.models import RunState, RunStatus

    outside = tmp_path / "outside-workspace"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")

    headers = {"Authorization": f"Bearer {token}"}
    with TestClient(app) as client:
        run = RunState.new(workflow_id="wf", entry_node="start")
        run.run_id = "draft_external_workspace"
        run.status = RunStatus.COMPLETED
        run.vars["workspace_root"] = str(outside)
        run.vars["_run_lifecycle"] = _expired_draft_lifecycle()

        svc = get_gateway_service()
        svc.host.run_store.save(run)

        purge = client.post("/api/gateway/runs/purge_drafts", headers=headers, json={"dry_run": False, "run_ids": [run.run_id]})
        assert purge.status_code == 200, purge.text
        assert purge.json()["purged"] == 1
        assert svc.host.run_store.load(run.run_id) is None
        assert outside.exists()
        assert (outside / "keep.txt").exists()
