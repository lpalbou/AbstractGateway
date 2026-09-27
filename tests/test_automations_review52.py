"""Review 52: client `_runtime` allowlist (R52-1), a runtime-paused RUNNING run
does not spin the runner or starve other automations, and the JSON session
index is warmed by the service boot only (W2)."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import ECHO_FLOW_ID, HEADERS, gateway_env, wait_until, write_echo_bundle


@pytest.fixture()
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway_env(monkeypatch, tmp_path, runner=True)
    ref = write_echo_bundle(tmp_path / "bundles")
    from abstractgateway.app import app

    with TestClient(app) as c:
        c.bundle_ref = ref  # type: ignore[attr-defined]
        yield c


DROPPED = ["control", "agora_agent", "automation", "tool_scope", "tool_policy", "workspace_read_only", "workflow_policy", "node_traces"]
KEPT = {"allowed_tools": ["read_file"], "provider": "lmstudio", "model": "m", "thinking": "low", "stream": False}


def _definition(c: TestClient, aid: str) -> dict:
    return c.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["definition"]


@pytest.mark.parametrize("key", DROPPED)
def test_create_drops_every_runtime_key_outside_the_allowlist(live: TestClient, key: str) -> None:
    runtime_ns = {key: {"paused": True} if key == "control" else "x", **KEPT}
    r = live.post("/api/gateway/automations", headers=HEADERS, json={
        "request_id": f"k-{key}", "title": "k", "target": {"bundle_ref": live.bundle_ref, "flow_id": ECHO_FLOW_ID,
                                                           "input_data": {"prompt": "p", "_runtime": runtime_ns}},
        "trigger": {"source_id": "manual", "source_version": 1, "config": {}}})
    assert r.status_code == 200, r.text
    assert _definition(live, r.json()["automation_id"])["target"]["input_data"]["_runtime"] == KEPT


def test_patch_target_goes_through_the_same_allowlist(live: TestClient) -> None:
    r = live.post("/api/gateway/automations", headers=HEADERS, json={
        "request_id": "patch", "title": "k", "target": {"bundle_ref": live.bundle_ref, "flow_id": ECHO_FLOW_ID},
        "trigger": {"source_id": "manual", "source_version": 1, "config": {}}})
    aid = r.json()["automation_id"]
    r = live.patch(f"/api/gateway/automations/{aid}", headers=HEADERS, json={"command_id": "t", "changes": {"target": {
        "bundle_ref": live.bundle_ref, "flow_id": ECHO_FLOW_ID,
        "input_data": {"prompt": "q", "_meta": {"automation": {}}, "_runtime": {"control": {"paused": True}, "agora_agent": "x", "model": "m"}}}}})
    assert r.status_code == 200, r.text
    wait_until(lambda: _definition(live, aid)["revision"] == 2)
    data = _definition(live, aid)["target"]["input_data"]
    assert data["_runtime"] == {"model": "m"} and "_meta" not in data


@pytest.mark.parametrize("bad, field", [
    ({"_runtime": {"control": {"paused": True}}}, "automation_defaults.input_data._runtime.control"),
    ({"_runtime": {"agora_agent": "x"}}, "automation_defaults.input_data._runtime.agora_agent"),
    ({"_meta": {"automation": {}}}, "automation_defaults.input_data._meta"),
    ({"workspace_read_only": False}, "automation_defaults.input_data.workspace_read_only"),
])
def test_flow_defaults_refuse_server_owned_input(live: TestClient, bad: dict, field: str) -> None:
    from automations_fixtures import echo_flow

    flow = {**echo_flow(), "automation_defaults": {"schema_version": 1, "trigger": {"source_id": "manual", "source_version": 1, "config": {}},
                                                   "input_data": {"prompt": "p", **bad}}}
    flow.pop("id")
    r = live.post("/api/gateway/visualflows", headers=HEADERS, json=flow)
    assert r.status_code == 422 and r.json()["detail"]["field"] == field
    ok = {**flow, "automation_defaults": {**flow["automation_defaults"], "input_data": {"prompt": "p", "_runtime": {"allowed_tools": ["read_file"]}}}}
    assert live.post("/api/gateway/visualflows", headers=HEADERS, json=ok).status_code == 200


def test_a_paused_running_run_does_not_spin_or_starve_others(live: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Root cause of R52-1's second half: the runner re-ticked a RUNNING run
    whose vars carry the pause flag in a tight loop (the tick is a no-op)."""
    from abstractruntime.core.models import RunState, RunStatus

    from abstractgateway.runner import GatewayRunner
    from abstractgateway.service import get_gateway_service

    ticks: dict = {}
    real = GatewayRunner._tick_run

    def spy(self, rid):
        ticks[rid] = ticks.get(rid, 0) + 1
        return real(self, rid)

    monkeypatch.setattr(GatewayRunner, "_tick_run", spy)
    svc = get_gateway_service()
    frozen = RunState(run_id=str(uuid.uuid4()), workflow_id=f"{live.bundle_ref}:{ECHO_FLOW_ID}", status=RunStatus.RUNNING,
                      current_node="start", vars={"prompt": "x", "_runtime": {"control": {"paused": True}}}, actor_id="gateway")
    svc.host.run_store.save(frozen)
    svc.runner.nudge(frozen.run_id)

    r = live.post("/api/gateway/automations", headers=HEADERS, json={
        "request_id": "B", "title": "B", "target": {"bundle_ref": live.bundle_ref, "flow_id": ECHO_FLOW_ID, "input_data": {"prompt": "b"}},
        "trigger": {"source_id": "schedule", "source_version": 1, "config": {"every": "1s"}}})
    b = r.json()["automation_id"]
    time.sleep(3.5)
    done = [o for o in live.get(f"/api/gateway/automations/{b}/occurrences", headers=HEADERS).json()["items"] if o["status"] == "completed"]
    assert len(done) >= 2, done                                   # B keeps ticking on schedule
    assert ticks.get(frozen.run_id, 0) <= 5, ticks.get(frozen.run_id)   # no hot loop on the frozen run
    assert svc.host.run_store.load(frozen.run_id).status == RunStatus.RUNNING


def test_only_the_service_boot_warms_the_session_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractruntime import JsonFileRunStore

    from abstractgateway import admin_runtimes
    from abstractgateway.stores import build_file_stores

    warmed: list = []
    monkeypatch.setattr(JsonFileRunStore, "warm_session_index", lambda self: warmed.append(self))
    build_file_stores(base_dir=tmp_path / "plane")
    admin_runtimes._stores_for_dir(tmp_path / "plane")
    assert warmed == []                                            # cross-plane / log-writer builds never warm
