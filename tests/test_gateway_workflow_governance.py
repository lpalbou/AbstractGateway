"""Workflow governance (DESIGN-v3 §5, §13.4): ownership, admin availability, archive (never delete).

Drives the REAL app over HTTP on a multi-user gateway (hosted user auth ON) with a real
admin token and real non-admin users, the configuration where "Shared by the gateway"
and "Mine" both exist: the shared flows dir is the admin's registry, each user has their
own `<data>/users/default/<user>/flows` with the shared dir loaded read-only.

Every app picker (AbstractCode web/TUI, the Assistant, AbstractFlow, the console) lists
workflows through `GET /bundles` (grep of their sources: `/bundles`,
`/bundles?all_versions=false`), so the /bundles assertions are the picker assertions.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import ECHO_BUNDLE_ID, ECHO_FLOW_ID, wait_until, write_echo_bundle
from test_gateway_workflow_registry_honesty import _write_bundle

ADMIN = {"Authorization": "Bearer admin-token"}
UNAVAILABLE = "This workflow isn't available to users on this gateway. Ask an admin."
PAUSED_REASON = "Paused: this workflow is no longer available to users — ask an admin."


@pytest.fixture()
def gw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import abstractgateway.config as cfg

    shared = tmp_path / "bundles"
    ref = write_echo_bundle(shared)
    (shared / "basic-agent.flow").write_bytes((Path(cfg._default_flows_dir()) / "basic-agent.flow").read_bytes())
    data = tmp_path / "data"
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(shared))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "admin-token")
    monkeypatch.setenv("ABSTRACTGATEWAY_USERS_FILE", str(tmp_path / "users.json"))
    monkeypatch.setenv("ABSTRACTGATEWAY_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    from abstractgateway.service import reset_gateway_boot_state
    from abstractgateway.users import GatewayUserRegistry

    reset_gateway_boot_state()
    reg = GatewayUserRegistry()
    _a, alice = reg.create_user(user_id="alice", roles=["user"], runtime_id="alice")
    _b, bob = reg.create_user(user_id="bob", roles=["user"], runtime_id="bob")
    alice_flows = data / "users" / "default" / "alice" / "flows"
    _write_bundle(alice_flows / "alice-wf@1.0.0.flow", bundle_id="alice-wf", bundle_version="1.0.0")
    from abstractgateway.app import app

    with TestClient(app) as c:
        yield {
            "c": c,
            "ref": ref,
            "alice": {"Authorization": f"Bearer {alice}"},
            "bob": {"Authorization": f"Bearer {bob}"},
            "data": data,
            "shared": shared,
            "alice_flows": alice_flows,
        }


def _items(c, headers, **params):
    qs = "&".join(f"{k}={v}" for k, v in {"all_versions": "true", **params}.items())
    res = c.get(f"/api/gateway/bundles?{qs}", headers=headers)
    assert res.status_code == 200, res.text
    return {it["bundle_id"]: it for it in res.json()["items"]}


def _set_available(c, bundle_id, available, headers=ADMIN):
    return c.put(f"/api/gateway/admin/workflows/{bundle_id}/availability", headers=headers, json={"available": available})


def test_items_carry_owner_and_shipped(gw):
    c = gw["c"]
    admin = _items(c, ADMIN)
    assert admin[ECHO_BUNDLE_ID]["owner"] == {"kind": "gateway", "user_id": None}
    assert admin[ECHO_BUNDLE_ID]["shipped"] is False
    assert admin["basic-agent"]["shipped"] is True and admin["basic-agent"]["owner"]["kind"] == "gateway"
    assert admin["basic-agent"]["actions"]["can_archive"] is False, "shipped: no Archive"
    assert admin[ECHO_BUNDLE_ID]["actions"]["can_remove"] is False, "nothing is deletable any more"

    alice = _items(c, gw["alice"])
    assert alice["alice-wf"]["owner"] == {"kind": "user", "user_id": "alice"}
    assert alice[ECHO_BUNDLE_ID]["owner"]["kind"] == "gateway"
    assert alice[ECHO_BUNDLE_ID]["actions"]["can_set_availability"] is False
    assert alice[ECHO_BUNDLE_ID]["actions"]["can_archive"] is False, "a user cannot archive a shared workflow"
    assert alice["alice-wf"]["actions"]["can_archive"] is True
    # A user never sees another user's bundles (A16).
    assert "alice-wf" not in _items(c, gw["bob"])


def test_availability_switch_is_admin_only(gw):
    c = gw["c"]
    res = _set_available(c, ECHO_BUNDLE_ID, False, headers=gw["alice"])
    assert res.status_code == 403, res.text
    assert _items(c, ADMIN)[ECHO_BUNDLE_ID]["available"] is True, "default: available"
    res = _set_available(c, ECHO_BUNDLE_ID, False)
    assert res.status_code == 200, res.text
    stored = json.loads((gw["data"] / "config" / "workflow_availability.json").read_text())
    assert stored["bundles"][ECHO_BUNDLE_ID]["available"] is False
    assert stored["bundles"][ECHO_BUNDLE_ID]["updated_by"] == "admin"


def test_unavailable_is_hidden_from_users_and_refused_at_run_start(gw):
    c, alice = gw["c"], gw["alice"]
    assert ECHO_BUNDLE_ID in _items(c, alice)
    assert _set_available(c, ECHO_BUNDLE_ID, False).status_code == 200

    # Lists (= every app picker) and reads.
    assert ECHO_BUNDLE_ID not in _items(c, alice)
    assert ECHO_BUNDLE_ID not in _items(c, alice, all_versions="false")
    read = c.get(f"/api/gateway/bundles/{ECHO_BUNDLE_ID}", headers=alice)
    assert read.status_code == 403 and read.json()["detail"]["message"] == UNAVAILABLE
    flow = c.get(f"/api/gateway/bundles/{ECHO_BUNDLE_ID}/flows/{ECHO_FLOW_ID}", headers=alice)
    assert flow.status_code == 403
    # Run start, scheduling and automation creation.
    start = c.post("/api/gateway/runs/start", headers=alice, json={"bundle_id": ECHO_BUNDLE_ID, "flow_id": ECHO_FLOW_ID, "input_data": {"prompt": "p"}})
    assert start.status_code == 403, start.text
    assert start.json()["detail"]["message"] == UNAVAILABLE
    auto = c.post("/api/gateway/automations", headers=alice, json={
        "request_id": "r1", "title": "t", "target": {"bundle_ref": gw["ref"], "flow_id": ECHO_FLOW_ID, "input_data": {"prompt": "p"}},
        "trigger": {"source_id": "manual", "source_version": 1, "config": {}}})
    assert auto.status_code == 403, auto.text

    # Admins always see and run it.
    admin_row = _items(c, ADMIN)[ECHO_BUNDLE_ID]
    assert admin_row["available"] is False
    ok = c.post("/api/gateway/runs/start", headers=ADMIN, json={"bundle_id": ECHO_BUNDLE_ID, "flow_id": ECHO_FLOW_ID, "input_data": {"prompt": "p"}})
    assert ok.status_code == 200, ok.text

    assert _set_available(c, ECHO_BUNDLE_ID, True).status_code == 200
    assert ECHO_BUNDLE_ID in _items(c, alice)


def test_the_per_app_default_keeps_running_when_hidden(gw, monkeypatch):
    """§5.2 exception: the admin's per-app default workflow stays runnable (and readable)
    for everyone even when it is hidden from users' lists."""
    import abstractgateway.routes.gateway as g

    c, alice = gw["c"], gw["alice"]
    assert _set_available(c, ECHO_BUNDLE_ID, False).status_code == 200
    monkeypatch.setattr(g, "_agent_default_bundle_ids", lambda *_a, **_k: {ECHO_BUNDLE_ID})
    assert ECHO_BUNDLE_ID not in _items(c, alice), "still hidden from lists"
    start = c.post("/api/gateway/runs/start", headers=alice, json={"bundle_id": ECHO_BUNDLE_ID, "flow_id": ECHO_FLOW_ID, "input_data": {"prompt": "p"}})
    assert start.status_code == 200, start.text


def test_turning_availability_off_pauses_users_automations_with_a_reason(gw):
    c, alice = gw["c"], gw["alice"]
    created = c.post("/api/gateway/automations", headers=alice, json={
        "request_id": "keep", "title": "Daily", "target": {"bundle_ref": gw["ref"], "flow_id": ECHO_FLOW_ID, "input_data": {"prompt": "p"}},
        "trigger": {"source_id": "manual", "source_version": 1, "config": {}}})
    assert created.status_code == 200, created.text
    aid = created.json()["automation_id"]

    res = _set_available(c, ECHO_BUNDLE_ID, False)
    assert res.status_code == 200, res.text
    assert [p["automation_id"] for p in res.json()["paused_automations"]] == [aid]

    def paused_row():
        rows = {r["automation_id"]: r for r in c.get("/api/gateway/automations", headers=alice).json()["items"]}
        row = rows.get(aid)
        return row if row and row["status"] == "paused" else None

    row = wait_until(paused_row, timeout_s=15)
    assert row["paused_reason"] == PAUSED_REASON

    # Back on: NOT auto-resumed.
    assert _set_available(c, ECHO_BUNDLE_ID, True).status_code == 200
    rows = {r["automation_id"]: r for r in c.get("/api/gateway/automations", headers=alice).json()["items"]}
    assert rows[aid]["status"] == "paused"


def test_delete_is_gone_and_shipped_is_never_archivable(gw):
    c = gw["c"]
    res = c.delete(f"/api/gateway/bundles/{ECHO_BUNDLE_ID}", headers=ADMIN)
    assert res.status_code == 410, res.text
    assert res.json()["detail"]["message"] == "Workflows are archived, never deleted: use Archive."
    assert (gw["shared"] / f"{ECHO_BUNDLE_ID}.flow").is_file()
    shipped = c.post("/api/gateway/bundles/basic-agent/archive", headers=ADMIN, json={})
    assert shipped.status_code == 409, shipped.text
    assert shipped.json()["detail"]["reason_code"] == "workflow_shipped"


def test_archived_is_hidden_still_loaded_and_refuses_new_runs(gw):
    c, alice = gw["c"], gw["alice"]
    start_body = {"bundle_id": "alice-wf", "flow_id": "root", "input_data": {}}
    # A user cannot archive a shared workflow, nor another user's.
    assert c.post(f"/api/gateway/bundles/{ECHO_BUNDLE_ID}/archive", headers=alice, json={}).status_code == 403
    assert c.post("/api/gateway/bundles/alice-wf/archive", headers=gw["bob"], json={}).status_code == 404

    res = c.post("/api/gateway/bundles/alice-wf/archive", headers=alice, json={})
    assert res.status_code == 200, res.text
    stored = json.loads((gw["data"] / "users" / "default" / "alice" / "config" / "workflow_archive.json").read_text())
    assert stored["bundles"]["alice-wf"]["versions"] == "all"

    assert "alice-wf" not in _items(c, alice), "archived: hidden from lists"
    shown = _items(c, alice, include_archived="true")["alice-wf"]
    assert shown["archived"] is True and shown["actions"]["can_run"] is False
    # Still LOADED (existing runs resume/replay): the host serves its flow.
    assert c.get("/api/gateway/bundles/alice-wf/flows/root", headers=alice).status_code == 200
    refused = c.post("/api/gateway/runs/start", headers=alice, json=start_body)
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["reason_code"] == "workflow_archived"
    assert (gw["alice_flows"] / "alice-wf@1.0.0.flow").is_file()

    assert c.post("/api/gateway/bundles/alice-wf/unarchive", headers=alice, json={}).status_code == 200
    assert "alice-wf" in _items(c, alice)
    assert c.post("/api/gateway/runs/start", headers=alice, json=start_body).status_code == 200


def test_a_users_import_lands_in_mine_with_an_owner_stamp(gw, tmp_path):
    c, alice = gw["c"], gw["alice"]
    staged = tmp_path / "mine.flow"
    _write_bundle(staged, bundle_id="mine-wf", bundle_version="2.0.0")
    up = c.post(
        "/api/gateway/bundles/upload",
        headers=alice,
        files={"file": ("mine-wf@2.0.0.flow", staged.read_bytes(), "application/octet-stream")},
        data={"overwrite": "false", "reload": "true"},
    )
    assert up.status_code == 200, up.text
    installed = gw["alice_flows"] / "mine-wf@2.0.0.flow"
    with zipfile.ZipFile(io.BytesIO(installed.read_bytes())) as z:
        owner = json.loads(z.read("manifest.json"))["metadata"]["owner"]
    assert owner["user_id"] == "alice" and owner["tenant_id"] == "default" and owner["at"]
    row = _items(c, alice)["mine-wf"]
    assert row["owner"] == {"kind": "user", "user_id": "alice"}
    assert row["metadata"]["owner"]["user_id"] == "alice"
    assert row["source"] == "imported"


def test_the_bundle_read_route_says_shipped_and_owner_for_flows_deep_link(gw):
    """AbstractFlow's `?bundle=<id>&version=<v>` loader reads GET /bundles/{id} (manifest +
    governance facts) and GET /bundles/{id}/flows/{flow_id} (the VisualFlow)."""
    c, alice = gw["c"], gw["alice"]
    shipped = c.get("/api/gateway/bundles/basic-agent", headers=alice)
    assert shipped.status_code == 200, shipped.text
    assert shipped.json()["shipped"] is True and shipped.json()["owner"]["kind"] == "gateway"
    mine = c.get("/api/gateway/bundles/alice-wf?bundle_version=1.0.0", headers=alice)
    assert mine.status_code == 200, mine.text
    assert mine.json()["shipped"] is False and mine.json()["owner"] == {"kind": "user", "user_id": "alice"}
    flow = c.get("/api/gateway/bundles/alice-wf/flows/root?bundle_version=1.0.0", headers=alice)
    assert flow.status_code == 200 and flow.json()["flow"]["id"] == "root"
    assert c.get("/api/gateway/bundles/alice-wf", headers=gw["bob"]).status_code == 404, "another user's bundle is not on bob's gateway view"


def _write_iface_bundle(path: Path, *, bundle_id: str, interfaces: list[str]) -> None:
    """Two entrypoints: `main` declares `interfaces`, `aux` declares nothing."""
    from test_gateway_workflow_registry_honesty import _min_flow

    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "1.0.0",
        "created_at": "2026-10-01T00:00:00Z",
        "entrypoints": [
            {"flow_id": "main", "name": "main", "description": "", "interfaces": interfaces},
            {"flow_id": "aux", "name": "aux", "description": "", "interfaces": []},
        ],
        "default_entrypoint": "main",
        "flows": {"main": "flows/main.json", "aux": "flows/aux.json"},
        "metadata": {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest))
        z.writestr("flows/main.json", json.dumps(_min_flow("main")))
        z.writestr("flows/aux.json", json.dumps(_min_flow("aux")))


def test_executable_for_lists_only_runnable_bundles_declaring_the_interface(gw):
    """The app pickers' query (`?executable_for=<interface>`): only bundles declaring the
    interface that the caller may run — admin: all, a user: available shared + their own,
    archived never — with only the entrypoints that declare it."""
    c, alice = gw["c"], gw["alice"]
    iface = "test.picker.v1"
    _write_iface_bundle(gw["shared"] / "shared-iface@1.0.0.flow", bundle_id="shared-iface", interfaces=[iface])
    _write_iface_bundle(gw["shared"] / "hidden-iface@1.0.0.flow", bundle_id="hidden-iface", interfaces=[iface])
    _write_iface_bundle(gw["shared"] / "other-iface@1.0.0.flow", bundle_id="other-iface", interfaces=["test.other.v1"])
    _write_iface_bundle(gw["alice_flows"] / "alice-iface@1.0.0.flow", bundle_id="alice-iface", interfaces=[iface])
    assert c.post("/api/gateway/bundles/reload", headers=ADMIN).status_code == 200
    assert c.post("/api/gateway/bundles/reload", headers=alice).status_code == 200
    assert _set_available(c, "hidden-iface", False).status_code == 200

    def picker(headers, **extra):
        res = c.get(f"/api/gateway/bundles?executable_for={iface}" + "".join(f"&{k}={v}" for k, v in extra.items()), headers=headers)
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["executable_for"] == iface
        for it in body["items"]:
            assert it["owner"]["kind"] in ("gateway", "user") and isinstance(it["shipped"], bool)
            assert it["entrypoints"] and all(iface in ep["interfaces"] for ep in it["entrypoints"]), it
        return {it["bundle_id"]: it for it in body["items"]}

    mine = picker(alice)
    assert set(mine) == {"shared-iface", "alice-iface"}, sorted(mine)
    assert [ep["flow_id"] for ep in mine["shared-iface"]["entrypoints"]] == ["main"]
    assert mine["alice-iface"]["owner"] == {"kind": "user", "user_id": "alice"}

    admin = picker(ADMIN)
    assert {"shared-iface", "hidden-iface"} <= set(admin) and "other-iface" not in admin, sorted(admin)
    assert "alice-iface" not in admin, "an admin's picker lists the gateway's registry, never a user's own"

    assert c.post("/api/gateway/bundles/alice-iface/archive", headers=alice, json={}).status_code == 200
    assert "alice-iface" not in picker(alice, include_archived="true"), "archived never appears in a picker"


def test_an_owner_stamped_native_loop_import_still_loads(gw):
    """The owner stamp must not break native-loop bundles (react/codeact/memact), whose
    manifest auditor refuses unknown metadata keys."""
    import abstractgateway.config as cfg

    src = Path(cfg._default_flows_dir()) / "react-agent@0.1.0.flow"
    if not src.is_file():
        pytest.skip("this checkout carries no react-agent bundle")
    with zipfile.ZipFile(src) as z:
        assert json.loads(z.read("manifest.json"))["metadata"].get("native_loop_factory"), "precondition: a native-loop bundle"
    up = gw["c"].post(
        "/api/gateway/bundles/upload",
        headers=gw["alice"],
        files={"file": (src.name, src.read_bytes(), "application/octet-stream")},
        data={"overwrite": "false", "reload": "true"},
    )
    assert up.status_code == 200, up.text
    assert up.json()["loaded"] is True, up.json()
    assert _items(gw["c"], gw["alice"])["react-agent"]["owner"] == {"kind": "user", "user_id": "alice"}
