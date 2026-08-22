"""A workflow version may be absent — it may never be absent SILENTLY.

Three behaviours are pinned here, all of them consequences of the operator's
standing rule that old versions are kept so old experiments stay reproducible:

1. A version the host refuses to serve (runtime floor, compile failure) is
   still ACCOUNTED FOR, with a reason, instead of vanishing at every reload.
2. `POST /bundles/upload` does not answer `{"ok": true}` for a bundle the host
   then refused — "the file was written" is not "you can run it".
3. The route-authorization contract's own route enumeration must not silently
   return nothing (the failure mode that let an ungated write route through).
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _min_flow(flow_id: str) -> dict:
    return {
        "id": flow_id,
        "name": flow_id,
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "start",
                "type": "on_flow_start",
                "position": {"x": 0.0, "y": 0.0},
                "data": {"nodeType": "on_flow_start", "label": "On Flow Start", "inputs": [], "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]},
            },
            {
                "id": "end",
                "type": "on_flow_end",
                "position": {"x": 10.0, "y": 0.0},
                "data": {"nodeType": "on_flow_end", "label": "On Flow End", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []},
            },
        ],
        "edges": [
            {"id": "e1", "source": "start", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in", "animated": True},
        ],
        "entryNode": "start",
    }


def _write_bundle(
    path: Path,
    *,
    bundle_id: str,
    bundle_version: str,
    flow_id: str = "root",
    min_runtime: str | None = None,
) -> None:
    manifest: dict = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": bundle_version,
        "created_at": "2026-08-21T00:00:00Z",
        "entrypoints": [{"flow_id": flow_id, "name": flow_id, "description": "", "interfaces": []}],
        "default_entrypoint": flow_id,
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "metadata": {},
    }
    if min_runtime:
        # The gate reads `metadata.min_runtime` (not a top-level key). An
        # impossible major version keeps this a skip whatever runtime is
        # installed.
        manifest["metadata"]["min_runtime"] = min_runtime
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest))
        z.writestr(f"flows/{flow_id}.json", json.dumps(_min_flow(flow_id)))


AUTH_TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {AUTH_TOKEN}"}


@pytest.fixture()
def gateway_env(tmp_path, monkeypatch):
    """A gateway whose bundles dir we own, carrying a real basic-agent copy.

    basic-agent is copied in because boot verifies it is loadable from the
    effective flows dir; without it `from_env()` refuses to start.
    """
    import abstractgateway.config as cfg

    flows = tmp_path / "flows"
    flows.mkdir(parents=True, exist_ok=True)
    shipped = Path(cfg._default_flows_dir()) / "basic-agent.flow"
    (flows / "basic-agent.flow").write_bytes(shipped.read_bytes())

    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows))
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    return flows


def _fresh_host(flows: Path, data: Path):
    from abstractgateway.hosts.bundle_host import WorkflowBundleGatewayHost
    from abstractgateway.stores import build_file_stores

    stores = build_file_stores(base_dir=data)
    return WorkflowBundleGatewayHost.load_from_dir(
        bundles_dir=flows,
        data_dir=data,
        run_store=stores.run_store,
        ledger_store=stores.ledger_store,
        artifact_store=stores.artifact_store,
    )


def test_a_version_refused_by_the_runtime_floor_is_recorded_not_erased(gateway_env, tmp_path):
    """The absence is correct; the SILENCE was the defect."""
    flows = gateway_env
    _write_bundle(flows / "floored@1.0.0.flow", bundle_id="floored", bundle_version="1.0.0", min_runtime="9999.0.0")

    host = _fresh_host(flows, tmp_path / "runtime")

    # Still absent from the served set — a bundle that cannot run must never be
    # listed as runnable.
    assert "floored" not in (host.bundles or {})

    rows = host.skipped_bundle_rows()
    match = [r for r in rows if r.get("bundle_id") == "floored"]
    assert match, f"the refused version left no record; rows={rows}"
    rec = match[0]
    assert rec["bundle_version"] == "1.0.0"
    assert rec["skip_kind"] == "min_runtime"
    assert rec["reason"], "a skip record with no reason explains nothing"
    assert rec["path"].endswith("floored@1.0.0.flow"), "the record must name the file still sitting on disk"

    # And addressable for one exact version.
    assert host.bundle_version_skip_reason("floored", "1.0.0") is not None
    assert host.bundle_version_skip_reason("floored", "2.0.0") is None


def test_skip_records_are_recomputed_by_a_reload_not_carried_forward(gateway_env, tmp_path):
    """A version fixed on disk must stop being reported as skipped."""
    flows = gateway_env
    target = flows / "floored@1.0.0.flow"
    _write_bundle(target, bundle_id="floored", bundle_version="1.0.0", min_runtime="9999.0.0")

    host = _fresh_host(flows, tmp_path / "runtime")
    assert host.bundle_version_skip_reason("floored", "1.0.0") is not None

    # Repack the SAME version without the impossible floor, then reload.
    target.unlink()
    _write_bundle(target, bundle_id="floored", bundle_version="1.0.0")
    out = host.reload_bundles_from_disk()

    assert host.bundle_version_skip_reason("floored", "1.0.0") is None, "a fixed version stayed on the skip list"
    assert "floored" in (host.bundles or {}), "a fixed version did not come back"
    assert not [r for r in out.get("skipped", []) or [] if r.get("bundle_id") == "floored"]


def test_reload_result_reports_what_it_dropped(gateway_env, tmp_path):
    flows = gateway_env
    _write_bundle(flows / "floored@1.0.0.flow", bundle_id="floored", bundle_version="1.0.0", min_runtime="9999.0.0")
    host = _fresh_host(flows, tmp_path / "runtime")

    out = host.reload_bundles_from_disk()
    assert out.get("ok") is True
    skipped = out.get("skipped") or []
    assert [r for r in skipped if r.get("bundle_id") == "floored"], f"reload hid its own drops: {out}"
    assert out.get("skipped_count") == len(skipped)


def test_upload_does_not_claim_success_for_a_bundle_the_host_refused(gateway_env, tmp_path):
    """`ok` must mean LOADED, not "bytes reached the disk"."""
    from abstractgateway.app import app

    staged = tmp_path / "staged.flow"
    _write_bundle(staged, bundle_id="refused", bundle_version="1.0.0", min_runtime="9999.0.0")

    with TestClient(app) as client:
        res = client.post(
            "/api/gateway/bundles/upload",
            headers=HEADERS,
            files={"file": ("refused@1.0.0.flow", staged.read_bytes(), "application/octet-stream")},
            data={"overwrite": "true", "reload": "true"},
        )
        assert res.status_code == 200, res.text
        body = res.json()

        assert body["gateway_reloaded"] is True
        assert body["loaded"] is False, "upload reported a workflow as usable when the host refused it"
        assert body["ok"] is False, "ok must track loadability, not bytes-written"
        assert body["skipped"] and body["skipped"]["skip_kind"] == "min_runtime"
        assert body["skipped"]["reason"]

        listing = client.get("/api/gateway/bundles", headers=HEADERS).json()
        assert not [i for i in listing["items"] if i["bundle_id"] == "refused"], "a refused bundle must not list as runnable"
        assert [r for r in listing["skipped"] if r["bundle_id"] == "refused"], "a refused bundle must still be accounted for"


def test_upload_of_a_loadable_bundle_still_reports_success(gateway_env, tmp_path):
    """The honesty gate must not turn healthy installs red."""
    from abstractgateway.app import app

    staged = tmp_path / "good.flow"
    _write_bundle(staged, bundle_id="accepted", bundle_version="1.0.0")

    with TestClient(app) as client:
        res = client.post(
            "/api/gateway/bundles/upload",
            headers=HEADERS,
            files={"file": ("accepted@1.0.0.flow", staged.read_bytes(), "application/octet-stream")},
            data={"overwrite": "true", "reload": "true"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["ok"] is True and body["loaded"] is True
        assert body["skipped"] is None

        listing = client.get("/api/gateway/bundles", headers=HEADERS).json()
        assert [i for i in listing["items"] if i["bundle_id"] == "accepted"]


def test_the_route_contract_enumeration_cannot_go_blind() -> None:
    """Regression guard for the failure that let an ungated route through.

    FastAPI >= 0.141 stopped flattening included routers into `app.routes`, so
    the contract module's enumeration returned ZERO gateway routes and every
    assertion built on it passed vacuously. If this ever returns nothing again,
    fail HERE rather than silently approving the whole write surface.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_contract_probe", str(Path(__file__).with_name("test_gateway_route_authorization_contract.py"))
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    rows = module._live_route_table()
    gateway_rows = [p for _m, p in rows if p.startswith("/api/gateway")]
    assert len(gateway_rows) > 100, f"route enumeration collapsed to {len(gateway_rows)} gateway routes"
    assert any(p.startswith("/api/gateway/admin") for p in gateway_rows)
    assert any(p == "/api/gateway/bundles/{bundle_id}" for p in gateway_rows)


def test_deleting_the_boot_agent_is_refused(gateway_env, tmp_path):
    """Removing basic-agent.flow does not degrade the gateway — it stops the
    NEXT boot, with an error naming a directory instead of the delete that
    emptied it, and `remove()` is a bare unlink with no undo."""
    from abstractgateway.app import app

    flows = gateway_env
    boot_file = flows / "basic-agent.flow"
    assert boot_file.is_file()

    with TestClient(app) as client:
        res = client.delete("/api/gateway/bundles/basic-agent", headers=HEADERS)
        assert res.status_code == 409, res.text
        assert "boot" in res.json()["detail"].lower()

    assert boot_file.is_file(), "the boot-critical bundle was deleted anyway"


def test_the_boot_guard_does_not_block_ordinary_bundles(gateway_env, tmp_path):
    """The guard must be narrow: only the boot file itself is protected."""
    from abstractgateway.app import app

    flows = gateway_env
    _write_bundle(flows / "ordinary@1.0.0.flow", bundle_id="ordinary", bundle_version="1.0.0")

    with TestClient(app) as client:
        assert client.post("/api/gateway/bundles/reload", headers=HEADERS).status_code == 200
        res = client.delete("/api/gateway/bundles/ordinary?bundle_version=1.0.0", headers=HEADERS)
        assert res.status_code == 200, res.text
        assert res.json()["removed"] == 1

    assert not (flows / "ordinary@1.0.0.flow").exists()
    assert (flows / "basic-agent.flow").is_file()


def test_upload_reports_a_shadowed_bundle_as_not_loaded(gateway_env, tmp_path):
    """The duplicate-id case: `<id>.flow` sorts before `<id>@<ver>.flow`, the
    registry resolves to the LAST match and the host keeps the FIRST, so the
    upload lands on disk while the host keeps serving the other file. There is
    no skip record for this, so asking the host what it SERVES is what catches
    it."""
    from abstractgateway.app import app

    flows = gateway_env
    # An unversioned file claiming bundle id "agentx" (same shape as the
    # shipped basic-agent.flow).
    _write_bundle(flows / "agentx.flow", bundle_id="agentx", bundle_version="0.0.1")

    staged = tmp_path / "agentx.flow"
    _write_bundle(staged, bundle_id="agentx", bundle_version="1.0.0")

    with TestClient(app) as client:
        res = client.post(
            "/api/gateway/bundles/upload",
            headers=HEADERS,
            files={"file": ("agentx@1.0.0.flow", staged.read_bytes(), "application/octet-stream")},
            data={"overwrite": "true", "reload": "true"},
        )
        assert res.status_code == 200, res.text
        body = res.json()

        served = client.get("/api/gateway/bundles?all_versions=true", headers=HEADERS).json()
        versions = {i["bundle_version"] for i in served["items"] if i["bundle_id"] == "agentx"}

        if "1.0.0" in versions:
            assert body["loaded"] is True and body["ok"] is True
        else:
            assert body["loaded"] is False, f"upload claimed a shadowed bundle was usable: {body}"
            assert body["ok"] is False
            assert body["skipped"]["skip_kind"] == "shadowed"


def test_upload_without_a_reload_reports_loadability_as_unverified(gateway_env, tmp_path):
    """Not reloading means we do not KNOW — and a guess in either direction is
    the thing this whole change exists to stop."""
    from abstractgateway.app import app

    staged = tmp_path / "later.flow"
    _write_bundle(staged, bundle_id="later", bundle_version="1.0.0")

    with TestClient(app) as client:
        res = client.post(
            "/api/gateway/bundles/upload",
            headers=HEADERS,
            files={"file": ("later@1.0.0.flow", staged.read_bytes(), "application/octet-stream")},
            data={"overwrite": "true", "reload": "false"},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["gateway_reloaded"] is False
        assert body["loaded"] is None, "claimed a loadability verdict without reloading"
        assert body["ok"] is True, "the install itself succeeded; only loadability is unknown"
