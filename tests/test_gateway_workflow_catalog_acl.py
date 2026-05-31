from __future__ import annotations

import io
import json
from pathlib import Path
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest


def _make_min_flow(flow_id: str, *, name: str = "") -> dict:
    fid = str(flow_id or "").strip() or "root"
    return {
        "id": fid,
        "name": name or fid,
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "start",
                "type": "on_flow_start",
                "position": {"x": 0.0, "y": 0.0},
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
                "position": {"x": 10.0, "y": 0.0},
                "data": {
                    "nodeType": "on_flow_end",
                    "label": "On Flow End",
                    "inputs": [{"id": "exec-in", "label": "", "type": "execution"}],
                    "outputs": [],
                },
            },
        ],
        "edges": [
            {"id": "e1", "source": "start", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"},
        ],
        "entryNode": "start",
    }


def _bundle_bytes(*, bundle_id: str, bundle_version: str, flow_id: str = "root", flow_name: str = "") -> bytes:
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": bundle_version,
        "created_at": "2026-05-30T00:00:00Z",
        "entrypoints": [{"flow_id": flow_id, "name": flow_id, "description": "", "interfaces": []}],
        "default_entrypoint": flow_id,
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "metadata": {"test": True},
    }
    buf = io.BytesIO()

    def write_json(zf: zipfile.ZipFile, name: str, value: dict) -> None:
        info = zipfile.ZipInfo(name)
        info.date_time = (2026, 1, 1, 0, 0, 0)
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, json.dumps(value, ensure_ascii=False, indent=2))

    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        write_json(zf, "manifest.json", manifest)
        write_json(zf, f"flows/{flow_id}.json", _make_min_flow(flow_id, name=flow_name))
    return buf.getvalue()


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "admin-token")
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")

    from abstractgateway.service import stop_gateway_runner

    stop_gateway_runner()

    from abstractgateway.routes import gateway_router
    from abstractgateway.security import GatewaySecurityMiddleware, load_gateway_auth_policy_from_env

    app = FastAPI()
    app.add_middleware(GatewaySecurityMiddleware, policy=load_gateway_auth_policy_from_env())
    app.include_router(gateway_router, prefix="/api")
    return TestClient(app)


def _create_user(client: TestClient, *, user_id: str = "alice", roles: list[str] | None = None) -> str:
    response = client.post(
        "/api/gateway/admin/users",
        headers={"Authorization": "Bearer admin-token"},
        json={"user_id": user_id, "tenant_id": "default", "roles": roles or ["user"]},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["token"])


def _upload_catalog(
    client: TestClient,
    *,
    data: bytes,
    filename: str,
    token: str = "admin-token",
    roles: str = "user,admin",
    make_default: bool = True,
) -> dict:
    response = client.post(
        "/api/gateway/admin/workflow-catalog/upload",
        headers={"Authorization": f"Bearer {token}"},
        data={"roles": roles, "make_default": "true" if make_default else "false"},
        files={"file": (filename, data, "application/octet-stream")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _catalog_internal_bundle_id(bundle_id: str) -> str:
    from abstractgateway.workflow_catalog import catalog_internal_bundle_id

    return catalog_internal_bundle_id(scope="tenant_catalog", tenant_id="default", bundle_id=bundle_id)


def test_workflow_catalog_admin_upload_acl_defaults_and_immutability(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    from abstractgateway.service import stop_gateway_runner

    try:
        with client:
            alice_token = _create_user(client)
            alice_headers = {"Authorization": f"Bearer {alice_token}"}

            forbidden = client.post(
                "/api/gateway/admin/workflow-catalog/upload",
                headers=alice_headers,
                data={"roles": "user", "make_default": "true"},
                files={"file": ("demo.flow", _bundle_bytes(bundle_id="demo", bundle_version="0.0.1"), "application/octet-stream")},
            )
            assert forbidden.status_code == 403

            _upload_catalog(
                client,
                data=_bundle_bytes(bundle_id="demo", bundle_version="0.0.1", flow_name="v1"),
                filename="demo@0.0.1.flow",
            )
            _upload_catalog(
                client,
                data=_bundle_bytes(bundle_id="demo", bundle_version="0.0.2", flow_name="v2"),
                filename="demo@0.0.2.flow",
            )

            catalog = client.get("/api/gateway/workflow-catalog", headers=alice_headers)
            assert catalog.status_code == 200, catalog.text
            items = catalog.json()["items"]
            assert [item["bundle_version"] for item in items if item["bundle_id"] == "demo"] == ["0.0.1", "0.0.2"]
            assert all(item["actions"]["can_run"] is True for item in items)
            assert [item["is_default"] for item in items if item["bundle_id"] == "demo"] == [False, True]

            default_start = client.post(
                "/api/gateway/runs/start",
                headers=alice_headers,
                json={"registry_scope": "tenant_catalog", "bundle_id": "demo", "flow_id": "root", "input_data": {}},
            )
            assert default_start.status_code == 200, default_start.text
            default_run = client.get(f"/api/gateway/runs/{default_start.json()['run_id']}", headers=alice_headers)
            assert default_run.status_code == 200, default_run.text
            assert "@0.0.2:" in default_run.json()["workflow_id"]

            implicit_catalog_start = client.post(
                "/api/gateway/runs/start",
                headers=alice_headers,
                json={"bundle_id": "demo", "flow_id": "root", "input_data": {}},
            )
            assert implicit_catalog_start.status_code == 404

            exact_old = client.post(
                "/api/gateway/runs/start",
                headers=alice_headers,
                json={
                    "registry_scope": "tenant_catalog",
                    "bundle_id": "demo",
                    "bundle_version": "0.0.1",
                    "flow_id": "root",
                    "input_data": {},
                },
            )
            assert exact_old.status_code == 200, exact_old.text
            exact_old_run = client.get(f"/api/gateway/runs/{exact_old.json()['run_id']}", headers=alice_headers)
            assert exact_old_run.status_code == 200, exact_old_run.text
            assert "@0.0.1:" in exact_old_run.json()["workflow_id"]

            denied_upload = _upload_catalog(
                client,
                data=_bundle_bytes(bundle_id="demo-private", bundle_version="1.0.0"),
                filename="demo-private@1.0.0.flow",
                roles="admin",
                make_default=True,
            )
            assert denied_upload["record"]["acl"]["roles"] == ["admin"]

            denied_start = client.post(
                "/api/gateway/runs/start",
                headers=alice_headers,
                json={"registry_scope": "tenant_catalog", "bundle_id": "demo-private", "flow_id": "root", "input_data": {}},
            )
            assert denied_start.status_code == 403

            denied_internal = _catalog_internal_bundle_id("demo-private")
            inspect_denied = client.get(f"/api/gateway/bundles/{denied_internal}", headers=alice_headers)
            assert inspect_denied.status_code == 403
            inspect_flow_denied = client.get(f"/api/gateway/bundles/{denied_internal}/flows/root?bundle_version=1.0.0", headers=alice_headers)
            assert inspect_flow_denied.status_code == 403
            safe_inspect_denied = client.get(
                "/api/gateway/workflow-catalog/demo-private/versions/1.0.0/flows/root",
                headers=alice_headers,
            )
            assert safe_inspect_denied.status_code == 403

            safe_inspect = client.get(
                "/api/gateway/workflow-catalog/demo/versions/0.0.2/flows/root",
                headers=alice_headers,
            )
            assert safe_inspect.status_code == 200, safe_inspect.text
            assert safe_inspect.json()["workflow_id"] == "demo@0.0.2:root"

            framework_upload = client.post(
                "/api/gateway/admin/workflow-catalog/upload",
                headers={"Authorization": "Bearer admin-token"},
                data={"scope": "framework_catalog", "roles": "user,admin", "make_default": "true"},
                files={"file": ("framework.flow", _bundle_bytes(bundle_id="framework-demo", bundle_version="0.0.1"), "application/octet-stream")},
            )
            assert framework_upload.status_code == 501

            changed_same_version = client.post(
                "/api/gateway/admin/workflow-catalog/upload",
                headers={"Authorization": "Bearer admin-token"},
                data={"roles": "user,admin", "make_default": "true"},
                files={
                    "file": (
                        "demo@0.0.2.flow",
                        _bundle_bytes(bundle_id="demo", bundle_version="0.0.2", flow_name="changed"),
                        "application/octet-stream",
                    )
                },
            )
            assert changed_same_version.status_code == 409

            from abstractgateway.service import get_gateway_service_for_principal
            from abstractgateway.users import GatewayUserRegistry

            principal = GatewayUserRegistry().authenticate(alice_token)
            assert principal is not None
            svc = get_gateway_service_for_principal(principal)
            forged_policy = {
                "host_workflow_id": f"{denied_internal}@1.0.0:root",
                "allowed_host_workflow_ids": [f"{denied_internal}@1.0.0:root"],
            }
            with pytest.raises(PermissionError):
                svc.host.start_run(
                    flow_id="root",
                    bundle_id=denied_internal,
                    bundle_version="1.0.0",
                    input_data={"_runtime": {"workflow_policy": forged_policy}},
                    actor_id="gateway",
                )
    finally:
        stop_gateway_runner()


def test_workflow_catalog_deprecates_only_one_version_and_tombstones_without_deleting_bytes(tmp_path: Path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    from abstractgateway.service import stop_gateway_runner

    try:
        with client:
            alice_token = _create_user(client)
            headers = {"Authorization": f"Bearer {alice_token}"}
            admin_headers = {"Authorization": "Bearer admin-token"}

            _upload_catalog(client, data=_bundle_bytes(bundle_id="demo", bundle_version="1.0.0"), filename="demo@1.0.0.flow")
            _upload_catalog(client, data=_bundle_bytes(bundle_id="demo", bundle_version="2.0.0"), filename="demo@2.0.0.flow")

            dep = client.post(
                "/api/gateway/admin/workflow-catalog/demo/versions/2.0.0/deprecate",
                headers=admin_headers,
                json={"reason": "replaced"},
            )
            assert dep.status_code == 200, dep.text

            reupload_inactive = _upload_catalog(
                client,
                data=_bundle_bytes(bundle_id="demo", bundle_version="2.0.0"),
                filename="demo@2.0.0.flow",
                make_default=True,
            )
            assert reupload_inactive["record"]["status"] == "deprecated"
            assert reupload_inactive["record"]["is_default"] is False

            deprecated_start = client.post(
                "/api/gateway/runs/start",
                headers=headers,
                json={
                    "registry_scope": "tenant_catalog",
                    "bundle_id": "demo",
                    "bundle_version": "2.0.0",
                    "flow_id": "root",
                    "input_data": {},
                },
            )
            assert deprecated_start.status_code == 409

            old_start = client.post(
                "/api/gateway/runs/start",
                headers=headers,
                json={
                    "registry_scope": "tenant_catalog",
                    "bundle_id": "demo",
                    "bundle_version": "1.0.0",
                    "flow_id": "root",
                    "input_data": {},
                },
            )
            assert old_start.status_code == 200, old_start.text

            scheduled = client.post(
                "/api/gateway/runs/schedule",
                headers=headers,
                json={
                    "registry_scope": "tenant_catalog",
                    "bundle_id": "demo",
                    "bundle_version": "1.0.0",
                    "flow_id": "root",
                    "input_data": {"prompt": "scheduled"},
                    "start_at": "now",
                },
            )
            assert scheduled.status_code == 200, scheduled.text
            from abstractgateway.service import get_gateway_service_for_principal
            from abstractgateway.users import GatewayUserRegistry

            principal = GatewayUserRegistry().authenticate(alice_token)
            assert principal is not None
            svc = get_gateway_service_for_principal(principal)
            scheduled_run = svc.host.run_store.load(scheduled.json()["run_id"])
            assert scheduled_run is not None
            assert scheduled_run.vars.get("_runtime", {}).get("workflow_policy", {}).get("bundle_version") == "1.0.0"

            tombstone = client.delete("/api/gateway/admin/workflow-catalog/demo/versions/1.0.0", headers=admin_headers)
            assert tombstone.status_code == 200, tombstone.text
            assert tombstone.json()["deleted_bytes"] is False

            tombstoned_start = client.post(
                "/api/gateway/runs/start",
                headers=headers,
                json={
                    "registry_scope": "tenant_catalog",
                    "bundle_id": "demo",
                    "bundle_version": "1.0.0",
                    "flow_id": "root",
                    "input_data": {},
                },
            )
            assert tombstoned_start.status_code == 409
    finally:
        stop_gateway_runner()
