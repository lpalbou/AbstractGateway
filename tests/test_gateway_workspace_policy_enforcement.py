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
        "created_at": "2026-01-19T00:00:00+00:00",
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


def _make_client(*, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict[str, str]]:
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-policy", flow_id="root")

    token = "t"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(runtime_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    from abstractgateway.app import app

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    return client, headers


def test_run_start_over_http_binds_the_effective_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Round 9 end to end: POST /runs/start refuses the removed any-folder mode and a wider folder,
    and the started run's own vars carry the account's effective set (the host's tool sandbox)."""
    shared, extra, secret, other = (tmp_path / n for n in ("shared", "extra", "secret", "other"))
    for d in (shared, extra, secret, other):
        d.mkdir()
    client, headers = _make_client(tmp_path=tmp_path, monkeypatch=monkeypatch)
    with client:
        r = client.put(
            "/api/gateway/workspace/policy",
            json={"shared_workspace": str(shared), "allowed_folders": [str(extra)], "never_allowed": [str(secret)], "launch_folder_trust": False},
            headers=headers,
        )
        assert r.status_code == 200, r.text

        def start(input_data: dict):
            return client.post("/api/gateway/runs/start", json={"bundle_id": "bundle-policy", "flow_id": "root", "input_data": input_data}, headers=headers)

        r = start({"workspace_access_mode": "all_except_ignored"})
        assert r.status_code == 400 and "no longer exists" in r.text, r.text
        r = start({"workspace_allowed_paths": [str(extra.resolve())]})  # allowed, but not switched on
        assert r.status_code == 400 and "outside the folders" in r.text, r.text
        r = start({"workspace_root": str(other.resolve())})
        assert r.status_code == 400, r.text
        r = start({"workspace_root": str(secret.resolve())})
        assert r.status_code == 400 and "never allows" in r.text, r.text

        assert client.put("/api/gateway/workspace/policy/me", json={"enabled_folders": [str(extra.resolve())]}, headers=headers).status_code == 200
        r = start({})
        assert r.status_code == 200, r.text
        run = client.get(f"/api/gateway/runs/{r.json()['run_id']}", headers=headers)
        assert run.status_code == 200, run.text
        from abstractgateway.service import get_gateway_service

        vars0 = get_gateway_service().host.run_store.load(r.json()["run_id"]).vars
        assert vars0["workspace_access_mode"] == "workspace_or_allowed"
        assert vars0["workspace_allowed_paths"] == [str(shared.resolve()), str(extra.resolve())]
        assert str(secret.resolve()) in str(vars0["workspace_ignored_paths"]).splitlines()


def test_open_run_workspace_uses_the_run_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    client, headers = _make_client(tmp_path=tmp_path, monkeypatch=monkeypatch)

    import abstractgateway.routes.gateway as gateway_routes

    opened: list[list[str]] = []

    def _fake_command(path: Path) -> list[str]:
        return ["open-test", str(path)]

    async def _fake_launch(command: list[str]) -> None:
        opened.append(list(command))

    monkeypatch.setattr(gateway_routes, "_workspace_open_command", _fake_command)
    monkeypatch.setattr(gateway_routes, "_launch_workspace_opener", _fake_launch)

    with client:
        assert client.put("/api/gateway/workspace/policy", json={"shared_workspace": str(ws)}, headers=headers).status_code == 200
        start = client.post(
            "/api/gateway/runs/start",
            json={"bundle_id": "bundle-policy", "flow_id": "root", "input_data": {}},
            headers=headers,
        )
        assert start.status_code == 200, start.text
        run_id = start.json()["run_id"]

        resp = client.post(f"/api/gateway/runs/{run_id}/workspace/open", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        workspace_root = Path(body["workspace_root"])
        assert body["opened"] is True
        assert workspace_root.exists()
        assert workspace_root.is_dir()
        assert opened == [["open-test", str(workspace_root)]]
