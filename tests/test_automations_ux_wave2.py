"""Operator field reports 2026-09-28 (Observer 0.1.14 + gateway 0.6.0, remote VPS):

- the automation's context mode is the ONE history control: `use_context` is
  server-owned for automation targets and discussion turns;
- a gateway-made workspace is never handed back as a start input, and reusing
  one is refused with a message that says what to do.
"""

from __future__ import annotations

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


def _create(c: TestClient, request_id: str, *, mode: str = "independent", input_data: dict | None = None):
    return c.post("/api/gateway/automations", headers=HEADERS, json={
        "request_id": request_id, "title": f"T {request_id}",
        "target": {"bundle_ref": c.bundle_ref, "flow_id": ECHO_FLOW_ID, "input_data": {"prompt": "news", **(input_data or {})}},
        "trigger": {"source_id": "manual", "source_version": 1, "config": {}},
        "context": {"mode": mode},
    })


def _definition(c: TestClient, aid: str) -> dict:
    return c.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["definition"]


@pytest.mark.parametrize("mode", ["independent", "growing"])
def test_use_context_is_owned_by_the_automation_not_the_client(live: TestClient, mode: str) -> None:
    r = _create(live, f"ctx-{mode}", mode=mode, input_data={"use_context": False})
    assert r.status_code == 200, r.text
    assert _definition(live, r.json()["automation_id"])["target"]["input_data"]["use_context"] is True


def test_a_revised_target_keeps_use_context_server_owned(live: TestClient) -> None:
    aid = _create(live, "rev", mode="growing").json()["automation_id"]
    r = live.patch(f"/api/gateway/automations/{aid}", headers=HEADERS, json={
        "command_id": "rev1", "changes": {"target": {"bundle_ref": live.bundle_ref, "flow_id": ECHO_FLOW_ID,
                                                     "input_data": {"prompt": "news 2", "use_context": False}}}})
    assert r.status_code == 200, r.text
    wait_until(lambda: _definition(live, aid)["revision"] == 2)
    assert _definition(live, aid)["target"]["input_data"]["use_context"] is True


def test_a_later_discussion_turn_always_reads_its_history(live: TestClient) -> None:
    aid = _create(live, "disc", mode="growing", input_data={"provider": "echo-provider", "model": "echo-model"}).json()["automation_id"]
    live.post(f"/api/gateway/automations/{aid}/commands", headers=HEADERS, json={"command_id": "go", "type": "automation.run_now"})
    wait_until(lambda: (lambda o: o and o[0]["status"] == "completed")(
        live.get(f"/api/gateway/automations/{aid}/occurrences", headers=HEADERS).json()["items"]), timeout_s=20)
    out = live.post(f"/api/gateway/automations/{aid}/discuss", headers=HEADERS,
                    json={"request_id": "d1", "occurrence_index": 1, "prompt": "why?"}).json()
    r = live.post("/api/gateway/runs/start", headers=HEADERS, json={
        "bundle_id": live.bundle_ref, "flow_id": ECHO_FLOW_ID, "session_id": out["session_id"],
        "input_data": {"prompt": "and then?", "use_context": False}})
    assert r.status_code == 200, r.text
    from abstractgateway.service import get_gateway_service

    run = get_gateway_service().host.run_store.load(r.json()["run_id"])
    assert run.vars["use_context"] is True and run.vars["use_session_history"] is True
    # A follow-up that names no model answers with the fork's model.
    assert (run.vars["provider"], run.vars["model"]) == ("echo-provider", "echo-model")


def test_input_data_never_offers_a_gateway_made_workspace(live: TestClient) -> None:
    aid = _create(live, "ws").json()["automation_id"]
    folder = _definition(live, aid)["workspace_root"]
    body = live.get(f"/api/gateway/runs/{aid}/input_data", headers=HEADERS).json()
    assert "workspace_root" not in body["input_data"]
    assert body["workspace"]["workspace_root"] == folder          # where it works stays readable
    # A folder the user chose (outside the data folder) is a real start input.
    r = live.post("/api/gateway/runs/start", headers=HEADERS, json={
        "bundle_id": live.bundle_ref, "flow_id": ECHO_FLOW_ID, "input_data": {"prompt": "x"}})
    run_id = r.json()["run_id"]
    wait_until(lambda: live.get(f"/api/gateway/runs/{run_id}", headers=HEADERS).json().get("status") == "completed")
    assert "workspace_root" not in live.get(f"/api/gateway/runs/{run_id}/input_data", headers=HEADERS).json()["input_data"]


def test_reusing_another_automations_folder_says_what_to_do(live: TestClient) -> None:
    first = _create(live, "one").json()["automation_id"]
    folder = _definition(live, first)["workspace_root"]
    r = _create(live, "two", input_data={"workspace_root": folder})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    message = detail["message"] if isinstance(detail, dict) else str(detail)
    assert "another conversation, run or automation" in message
    assert "Leave the workspace empty" in message
    # And empty is accepted: the gateway makes the second one its own folder.
    second = _create(live, "three")
    assert second.status_code == 200, second.text
    assert _definition(live, second.json()["automation_id"])["workspace_root"] != folder
