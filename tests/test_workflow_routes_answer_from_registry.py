"""Download, promote and publish's existing-versions check answer from the registry the plane RUNS
(every source: the plane's own folder, the shared folder, the AbstractNote packs), never from a scan
of the plane's own folder — so what an app is told exists is exactly what the gateway can run
("the gateway is the source of truth for the apps", operator 2026-11-28)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_gateway_workflow_registry_honesty import _min_flow, _write_bundle  # noqa: F401
from abstractgateway import abstractnote_seam, workflow_folder

ADMIN_TOKEN = "admin-token"


@pytest.fixture()
def planes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import abstractgateway.config as cfg

    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "basic-agent.flow").write_bytes((Path(cfg._default_flows_dir()) / "basic-agent.flow").read_bytes())
    _write_bundle(shared / "shared-wf@1.0.0.flow", bundle_id="shared-wf", bundle_version="1.0.0")
    # An AbstractNote pack (`extensions/abstractnote/<hash>/`, abstractnote_seam.py): a stand-in with no
    # model nodes, so no test gateway warms a model (conftest keeps the real packs out).
    pack = tmp_path / "data" / "extensions" / "abstractnote" / "0123456789abcdef" / "abstractnote@0.9.0.flow"
    pack.parent.mkdir(parents=True)
    _write_bundle(pack, bundle_id="abstractnote", bundle_version="0.9.0")
    monkeypatch.setattr(abstractnote_seam, "note_bundle_paths", lambda _root: [pack])
    monkeypatch.setattr(workflow_folder, "chosen", shared.resolve())
    monkeypatch.setattr(workflow_folder, "serving", {})
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ABSTRACTGATEWAY_USERS_FILE", str(tmp_path / "users.json"))
    monkeypatch.setenv("ABSTRACTGATEWAY_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    from abstractgateway.service import reset_gateway_boot_state
    from abstractgateway.users import GatewayUserRegistry

    reset_gateway_boot_state()
    reg = GatewayUserRegistry()
    _o, ops = reg.create_user(user_id="ops", roles=["admin"])  # an admin with a plane of its own
    from abstractgateway.app import app

    with TestClient(app) as c:
        yield c, shared, {"Authorization": f"Bearer {ops}"}, {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def test_a_user_plane_downloads_what_its_registry_runs_from_the_shared_folder(planes) -> None:
    c, shared, ops, _root = planes
    r = c.get("/api/gateway/bundles/shared-wf/download", headers=ops, params={"bundle_version": "1.0.0"})
    assert r.status_code == 200, r.text
    assert r.content == (shared / "shared-wf@1.0.0.flow").read_bytes()
    assert r.headers["X-AbstractGateway-Bundle-Sha256"] == hashlib.sha256(r.content).hexdigest()


def test_a_user_plane_downloads_and_promotes_an_abstractnote_pack(planes) -> None:
    c, _shared, ops, _root = planes
    ver = "0.9.0"
    r = c.get("/api/gateway/bundles/abstractnote/download", headers=ops, params={"bundle_version": ver})
    assert r.status_code == 200, r.text
    r = c.post("/api/gateway/admin/workflow-catalog/promote", headers=ops, json={"bundle_id": "abstractnote", "bundle_version": ver})
    assert r.status_code == 200, r.text


def test_a_user_plane_promotes_a_shared_folder_version(planes) -> None:
    c, _shared, ops, _root = planes
    r = c.post("/api/gateway/admin/workflow-catalog/promote", headers=ops, json={"bundle_id": "shared-wf", "bundle_version": "1.0.0"})
    assert r.status_code == 200, r.text


def test_publish_counts_a_version_loaded_from_another_source(planes) -> None:
    c, _shared, ops, _root = planes
    created = c.post("/api/gateway/visualflows", headers=ops, json={k: v for k, v in _min_flow("wf").items() if k != "id"})
    assert created.status_code == 200, created.text
    fid = created.json()["id"]
    r = c.post(f"/api/gateway/visualflows/{fid}/publish", headers=ops, json={"bundle_id": "shared-wf", "bundle_version": "1.0.0"})
    assert r.status_code == 400 and "already exists" in r.text, r.text  # 1.0.0 runs here, from the shared folder
    r = c.post(f"/api/gateway/visualflows/{fid}/publish", headers=ops, json={"bundle_id": "shared-wf"})
    assert r.status_code == 200, r.text
    assert r.json()["bundle_version"] == "1.0.1"


def test_the_root_plane_downloads_and_promotes_its_folder_as_before(planes) -> None:
    c, shared, _ops, root = planes
    r = c.get("/api/gateway/bundles/shared-wf/download", headers=root)
    assert r.status_code == 200, r.text
    assert r.content == (shared / "shared-wf@1.0.0.flow").read_bytes()
    r = c.post("/api/gateway/admin/workflow-catalog/promote", headers=root, json={"bundle_id": "shared-wf"})
    assert r.status_code == 200, r.text
    r = c.get("/api/gateway/bundles/nope/download", headers=root)
    assert r.status_code == 404


def _publishable(c: TestClient, headers: dict) -> str:
    created = c.post("/api/gateway/visualflows", headers=headers, json={k: v for k, v in _min_flow("wf").items() if k != "id"})
    assert created.status_code == 200, created.text
    return created.json()["id"]


def test_publish_never_overwrites_a_file_the_registry_skipped(planes) -> None:
    c, shared, _ops, root = planes
    # In the root plane's own folder: opens, but the min-runtime gate skips it (not in the registry).
    skipped = shared / "skipped-wf.flow"
    _write_bundle(skipped, bundle_id="skipped-wf", bundle_version="1.0.0", min_runtime="999.0.0")
    assert c.post("/api/gateway/bundles/reload", headers=root).status_code == 200
    from abstractgateway import service

    assert "skipped-wf" not in service.get_gateway_service().host.bundles  # the registry skipped it
    before = skipped.read_bytes()
    fid = _publishable(c, root)
    r = c.post(f"/api/gateway/visualflows/{fid}/publish", headers=root, json={"bundle_id": "skipped-wf", "bundle_version": "1.0.0"})
    assert r.status_code == 400 and "already exists" in r.text, r.text
    assert not (shared / "skipped-wf@1.0.0.flow").exists() and skipped.read_bytes() == before
    r = c.post(f"/api/gateway/visualflows/{fid}/publish", headers=root, json={"bundle_id": "skipped-wf"})
    assert r.status_code == 200 and r.json()["bundle_version"] == "1.0.1", r.text


def test_publish_answers_an_error_when_its_folder_cannot_be_listed(planes, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractruntime.workflow_bundle import WorkflowBundleRegistry

    c, shared, _ops, root = planes
    fid = _publishable(c, root)

    def unlistable(self):
        raise PermissionError(13, "Permission denied", str(self.bundles_dir))

    monkeypatch.setattr(WorkflowBundleRegistry, "scan", unlistable)
    before = sorted(p.name for p in shared.iterdir())
    r = c.post(f"/api/gateway/visualflows/{fid}/publish", headers=root, json={"bundle_id": "new-wf"})
    assert r.status_code == 500 and "Cannot list the workflow folder" in r.text, r.text
    assert sorted(p.name for p in shared.iterdir()) == before


def test_a_catalog_entry_is_not_downloadable_by_its_internal_id(planes) -> None:
    c, _shared, ops, root = planes
    from abstractgateway import service

    host = service.get_gateway_service().host
    internal = next(bid for bid, per in host.bundle_sources.items()
                    if any(m.get("registry_scope") == "tenant_catalog" for m in per.values()))
    for headers in (root, ops):
        r = c.get(f"/api/gateway/bundles/{internal}/download", headers=headers)
        assert r.status_code in (403, 404), (r.status_code, r.text)
        r = c.post("/api/gateway/admin/workflow-catalog/promote", headers=headers, json={"bundle_id": internal})
        assert r.status_code in (403, 404), (r.status_code, r.text)
