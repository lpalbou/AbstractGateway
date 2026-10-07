"""R14.2: per-account client preferences (GET/PUT /api/gateway/accounts/{me|account}/preferences).

Drives the REAL app over HTTP on a multi-user gateway (hosted user auth ON): a static admin
token, two users (alice, bob), an entity created by alice (aster), shared workflows declaring
the two app interfaces, a shared one the admin made unavailable, and alice's own workflow.

Rules proven here:
- `default_workflow` is a map per app interface; null = the gateway's per-app default, which is
  the existing admin setting `agents.default_workflow.<interface>` (never duplicated: changing it
  changes what "Gateway default (<name>)" names, the account entry stays null).
- self: anyone reads/writes their own; admins any account; an entity's: admins and its creator;
  another user is refused with a sentence; an unknown account answers 404.
- Only declared keys/interfaces are accepted; a workflow the account may not run is refused
  with the gateway's sentence and nothing is stored.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import shell_agent_flow

ADMIN = {"Authorization": "Bearer admin-token"}
CODE = "abstractcode.agent.v1"
ASSIST = "abstractassistant.agent.v1"


def _bundle(path: Path, *, bundle_id: str, name: str, interfaces: list, version: str = "1.0.0", flow_id: str = "agent") -> None:
    flow = dict(shell_agent_flow(), id=flow_id, name=name, interfaces=list(interfaces))
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": version,
        "created_at": "2026-10-07T00:00:00+00:00",
        "default_entrypoint": flow_id,
        "entrypoints": [{"flow_id": flow_id, "name": name, "description": f"{name} (test)", "interfaces": list(interfaces)}],
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr(f"flows/{flow_id}.json", json.dumps(flow))


@pytest.fixture()
def gw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import abstractgateway.config as cfg

    shared = tmp_path / "bundles"
    shared.mkdir(parents=True)
    (shared / "basic-agent.flow").write_bytes((Path(cfg._default_flows_dir()) / "basic-agent.flow").read_bytes())
    _bundle(shared / "coder-two@1.0.0.flow", bundle_id="coder-two", name="Coder Two", interfaces=[CODE])
    _bundle(shared / "hidden-coder@1.0.0.flow", bundle_id="hidden-coder", name="Hidden Coder", interfaces=[CODE])
    _bundle(shared / "helper@1.0.0.flow", bundle_id="helper", name="Helper", interfaces=[ASSIST])
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
    _bundle(data / "users" / "default" / "alice" / "flows" / "alice-coder@1.0.0.flow", bundle_id="alice-coder", name="Alice Coder", interfaces=[CODE])
    # An entity alice created: a home whose manifest names her, then its account (minted at boot).
    home = data / "entities" / "aster"
    home.mkdir(parents=True)
    (home / "manifest.json").write_text(json.dumps({"slug": "aster", "name": "Aster", "created_by": {"tenant_id": "default", "user_id": "alice"}}))
    from abstractgateway.entity_accounts import ensure_entity_accounts

    ensure_entity_accounts()
    from abstractgateway.app import app

    with TestClient(app) as c:
        assert c.put("/api/gateway/admin/workflows/hidden-coder/availability", headers=ADMIN, json={"available": False}).status_code == 200
        yield {"c": c, "alice": {"Authorization": f"Bearer {alice}"}, "bob": {"Authorization": f"Bearer {bob}"}, "data": data}


def _get(c, who, account="me"):
    r = c.get(f"/api/gateway/accounts/{account}/preferences", headers=who)
    assert r.status_code == 200, r.text
    return r.json()


def _row(answer, iface):
    return next(a for a in answer["apps"] if a["interface"] == iface)


def _store(gw) -> dict:
    path = gw["data"] / "runtime_config.json"
    if not path.exists():
        path = next(gw["data"].rglob("runtime_config.json"))
    return json.loads(path.read_text()).get("account_preferences") or {}


def test_get_me_starts_on_the_gateway_default_per_app(gw):
    a = _get(gw["c"], gw["alice"])
    assert a["account"] == "default:alice" and a["can_edit"] is True
    assert a["preferences"] == {"default_workflow": {CODE: None, ASSIST: None}}
    assert set(a["declared"]) == {"default_workflow"}
    code = _row(a, CODE)
    assert code["state"] == "default" and code["value"] is None
    assert code["gateway_default"]["available"] is True
    name = code["gateway_default"]["name"]
    assert code["gateway_default_label"] == f"Gateway default ({name})"
    assert code["effective"]["source"] == "gateway" and code["effective"]["bundle_id"] == "basic-agent"
    values = {c["value"] for c in code["choices"]}
    # The shared ones she may run + her own; never the one the admin made unavailable.
    assert {"coder-two:agent", "alice-coder:agent"} <= values
    assert "hidden-coder:agent" not in values
    assert {c["value"] for c in _row(a, ASSIST)["choices"]} == {"helper:agent"}


def test_put_me_overrides_one_app_and_null_goes_back_to_the_default(gw):
    c = gw["c"]
    r = c.put("/api/gateway/accounts/me/preferences", headers=gw["alice"], json={"default_workflow": {CODE: "coder-two@1.0.0:agent"}})
    assert r.status_code == 200, r.text
    code = _row(r.json(), CODE)
    # Stored version-less: it follows new versions.
    assert code["value"] == "coder-two:agent" and code["state"] == "set"
    assert code["effective"] == {**code["effective"], "source": "account", "bundle_id": "coder-two", "name": "Coder Two"}
    assert _row(r.json(), ASSIST)["value"] is None, "unnamed apps keep their value"
    assert _store(gw) == {"default:alice": {"default_workflow": {CODE: "coder-two:agent"}}}
    # Her own private workflow is hers to pick.
    r = c.put("/api/gateway/accounts/me/preferences", headers=gw["alice"], json={"default_workflow": {CODE: "alice-coder:agent"}})
    assert r.status_code == 200 and _row(r.json(), CODE)["state"] == "set"
    r = c.put("/api/gateway/accounts/me/preferences", headers=gw["alice"], json={"default_workflow": {CODE: None}})
    assert r.status_code == 200 and _row(r.json(), CODE)["state"] == "default"
    assert _store(gw) == {}, "an account with nothing set has no entry"
    # Bob is untouched throughout.
    assert _get(c, gw["bob"])["preferences"]["default_workflow"] == {CODE: None, ASSIST: None}


def test_unknown_keys_interfaces_and_unrunnable_workflows_are_refused_with_a_sentence(gw):
    c, alice = gw["c"], gw["alice"]

    def refused(body, who=alice):
        r = c.put("/api/gateway/accounts/me/preferences", headers=who, json=body)
        assert r.status_code == 400, r.text
        d = r.json()["detail"]
        assert d["reason"] == "preference_refused"
        return d["message"]

    assert refused({"theme": "dark"}) == "Unknown preference 'theme': this gateway declares default_workflow."
    assert "is not an app this gateway sets a default workflow for" in refused({"default_workflow": {"abstractcode.coding.v1": None}})
    assert refused({"default_workflow": "coder-two:agent"}).startswith("default_workflow is one entry per app")
    msg = refused({"default_workflow": {CODE: "hidden-coder:agent"}})
    assert msg.startswith("default_workflow.abstractcode.agent.v1 = 'hidden-coder:agent' refused:"), msg
    msg = refused({"default_workflow": {CODE: "helper:agent"}})
    assert "declares abstractassistant.agent.v1, not abstractcode.agent.v1" in msg, msg
    assert "is not on this gateway" in refused({"default_workflow": {CODE: "nope:agent"}})
    # Bob may not pick alice's private workflow.
    assert "refused" in refused({"default_workflow": {CODE: "alice-coder:agent"}}, who=gw["bob"])
    # A refused write stores nothing (not even the valid part of the body).
    r = c.put("/api/gateway/accounts/me/preferences", headers=alice, json={"default_workflow": {ASSIST: "helper:agent", CODE: "nope:agent"}})
    assert r.status_code == 400
    assert _store(gw) == {}


def test_authz_self_admin_and_entity_creator(gw):
    c = gw["c"]
    # Another user: refused, both ways.
    for method in ("get", "put"):
        r = getattr(c, method)("/api/gateway/accounts/alice/preferences", headers=gw["bob"], **({"json": {"default_workflow": {}}} if method == "put" else {}))
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["message"] == "Only an admin or the account itself can read or change its preferences."
    # An admin reads and writes anyone's, among the gateway's shared workflows.
    a = _get(c, ADMIN, "default:alice")
    assert a["account"] == "default:alice" and a["can_edit"] is True
    values = {x["value"] for x in _row(a, CODE)["choices"]}
    assert "coder-two:agent" in values and "hidden-coder:agent" not in values, "alice may not run hidden-coder"
    assert "alice-coder:agent" not in values, "an admin is never offered a user's private workflows"
    r = c.put("/api/gateway/accounts/alice/preferences", headers=ADMIN, json={"default_workflow": {CODE: "coder-two:agent"}})
    assert r.status_code == 200, r.text
    assert _row(_get(c, gw["alice"]), CODE)["value"] == "coder-two:agent", "alice sees what the admin set"
    # The entity: its creator and admins; bob is refused.
    r = c.put("/api/gateway/accounts/aster/preferences", headers=gw["alice"], json={"default_workflow": {ASSIST: "helper:agent"}})
    assert r.status_code == 200, r.text
    # Its creator picks among the gateway's shared workflows only: never her own private one.
    aster = _get(c, gw["alice"], "aster")
    assert aster["account"] == "default:aster" and aster["can_edit"] is True
    aster_values = {x["value"] for x in _row(aster, CODE)["choices"]}
    assert "coder-two:agent" in aster_values and "alice-coder:agent" not in aster_values, aster_values
    r = c.put("/api/gateway/accounts/aster/preferences", headers=gw["alice"], json={"default_workflow": {CODE: "alice-coder:agent"}})
    assert r.status_code == 400 and "not among the workflows aster may run" in r.json()["detail"]["message"], r.text
    assert _row(_get(c, ADMIN, "aster"), ASSIST)["value"] == "helper:agent"
    r = c.get("/api/gateway/accounts/aster/preferences", headers=gw["bob"])
    assert r.status_code == 403 and r.json()["detail"]["message"] == "Only an admin or aster's creator can read or change its preferences."
    # Unknown account.
    assert c.get("/api/gateway/accounts/nobody/preferences", headers=ADMIN).status_code == 404
    # The admin's own (`me`) works too.
    assert _get(c, ADMIN)["account"] == "default:admin"


def test_the_gateway_default_is_the_admin_per_app_setting_never_a_copy(gw):
    c = gw["c"]
    before = _row(_get(c, gw["alice"]), CODE)
    assert before["effective"]["bundle_id"] == "basic-agent"
    r = c.post("/api/gateway/admin/runtime-config", headers=ADMIN, json={"agents.default_workflow.abstractcode.agent.v1": "coder-two:agent"})
    assert r.status_code == 200, r.text
    after = _row(_get(c, gw["alice"]), CODE)
    assert after["value"] is None and after["state"] == "default"
    assert after["gateway_default_label"] == "Gateway default (Coder Two)"
    assert after["effective"] == {**after["effective"], "source": "gateway", "bundle_id": "coder-two"}
    assert _store(gw) == {}


def test_a_value_that_stops_running_is_reported_broken_with_the_reason(gw):
    c = gw["c"]
    assert c.put("/api/gateway/accounts/alice/preferences", headers=ADMIN, json={"default_workflow": {CODE: "coder-two:agent"}}).status_code == 200
    assert c.put("/api/gateway/admin/workflows/coder-two/availability", headers=ADMIN, json={"available": False}).status_code == 200
    row = _row(_get(c, gw["alice"]), CODE)
    assert row["state"] == "broken" and row["value"] == "coder-two:agent"
    assert row["reason"] == "coder-two:agent no longer runs for alice: it is not among the workflows you may run for this app. Pick another workflow or Gateway default."
    assert row["effective"]["available"] is False


def test_account_rows_carry_the_preferences_action(gw):
    c = gw["c"]
    rows = {a["id"]: a for a in c.get("/api/gateway/admin/accounts", headers=ADMIN).json()["accounts"]}
    for key in ("alice", "bob", "aster"):
        assert rows[key]["actions"]["preferences"] == {"available": True, "reason": None}
    mine = {a["id"]: a for a in c.get("/api/gateway/me/accounts", headers=gw["alice"]).json()["accounts"]}
    assert mine["alice"]["actions"]["preferences"]["available"] is True
