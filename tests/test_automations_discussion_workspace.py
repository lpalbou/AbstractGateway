"""Operator ruling 2026-09-27: a discussion works in its OWN writable workspace
(a gateway session folder) with the automation's workspace mounted read-only."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import HEADERS, gateway_env, wait_until, write_echo_bundle


@pytest.fixture()
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway_env(monkeypatch, tmp_path, runner=True)
    ref = write_echo_bundle(tmp_path / "bundles")
    from abstractgateway.app import app

    with TestClient(app) as c:
        c.bundle_ref = ref  # type: ignore[attr-defined]
        yield c


def _store():
    from abstractgateway.service import get_gateway_service

    return get_gateway_service().host.run_store


def _finished(run_id: str):
    return wait_until(lambda: (lambda r: r if r is not None and r.status.value in ("completed", "failed", "waiting") else None)(_store().load(run_id)), timeout_s=20)


def _writer_automation(c: TestClient) -> tuple[str, Path]:
    r = c.post("/api/gateway/automations", headers=HEADERS, json={
        "request_id": "w", "title": "writer", "target": {"bundle_ref": c.bundle_ref, "flow_id": "writer",
                                                         "input_data": {"prompt": "from the automation", "path": "note.txt"}},
        "trigger": {"source_id": "manual", "source_version": 1, "config": {}}})
    assert r.status_code == 200, r.text
    aid = r.json()["automation_id"]
    c.post(f"/api/gateway/automations/{aid}/commands", headers=HEADERS, json={"command_id": "go", "type": "automation.run_now"})
    wait_until(lambda: (lambda o: o and o[0]["status"] == "completed")(
        c.get(f"/api/gateway/automations/{aid}/occurrences", headers=HEADERS).json()["items"]), timeout_s=20)
    ws = Path(c.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["definition"]["workspace_root"])
    assert (ws / "note.txt").read_text() == "[Trigger manual@1 · occurrence 1 · fired " + (ws / "note.txt").read_text().split("fired ", 1)[1]
    return aid, ws


def test_discussion_gets_its_own_folder_and_the_mount(live: TestClient) -> None:
    aid, automation_ws = _writer_automation(live)
    before = (automation_ws / "note.txt").read_text()
    r = live.post(f"/api/gateway/automations/{aid}/discuss", headers=HEADERS,
                  json={"request_id": "d1", "occurrence_index": 1, "prompt": "written by the discussion"})
    assert r.status_code == 200, r.text
    out = r.json()
    own = Path(out["workspace_root"])
    assert own.is_dir() and own.parent.name == "workspaces" and own != automation_ws
    assert os.path.realpath(out["mounted_workspace"]) == os.path.realpath(str(automation_ws))

    # Turn 1 (the writer flow, relative path) writes into its OWN folder.
    first = _finished(out["run_id"])
    assert first.status.value == "completed", first.error
    assert (own / "note.txt").read_text() == "written by the discussion"
    assert (automation_ws / "note.txt").read_text() == before

    # A later turn through /runs/start writing INTO the mount is refused.
    r = live.post("/api/gateway/runs/start", headers=HEADERS, json={
        "bundle_id": live.bundle_ref, "flow_id": "writer", "session_id": out["session_id"],
        "input_data": {"prompt": "overwrite", "path": str(automation_ws / "note.txt"), "workspace_root": "/tmp", "_runtime": {}}})
    assert r.status_code == 200, r.text
    second = _finished(r.json()["run_id"])
    assert (automation_ws / "note.txt").read_text() == before
    assert second.vars["workspace_root"] == str(own)
    assert second.vars["_runtime"]["workspace_read_only_paths"] == [os.path.realpath(str(automation_ws))]
    output = second.output or {}
    assert second.status.value == "failed" or output.get("success") is False, (second.status, output)

    # The host guard lets the mount be read: the later turn's built-in allow
    # list holds its own folder AND the mount (inside the data folder).
    assert set(second.vars["workspace_builtin_allow"]) == {str(own), os.path.realpath(str(automation_ws))}
    # ...and a further turn still writes in its own folder.
    r = live.post("/api/gateway/runs/start", headers=HEADERS, json={
        "bundle_id": live.bundle_ref, "flow_id": "writer", "session_id": out["session_id"],
        "input_data": {"prompt": "again", "path": "second.txt"}})
    third = _finished(r.json()["run_id"])
    assert third.status.value == "completed" and (own / "second.txt").read_text() == "again"


def test_execute_command_runs_in_the_own_folder(live: TestClient) -> None:
    aid, _automation_ws = _writer_automation(live)
    out = live.post(f"/api/gateway/automations/{aid}/discuss", headers=HEADERS,
                    json={"request_id": "d2", "occurrence_index": 1, "prompt": "p"}).json()
    _finished(out["run_id"])
    r = live.post("/api/gateway/runs/start", headers=HEADERS, json={
        "bundle_id": live.bundle_ref, "flow_id": "pwd", "session_id": out["session_id"], "input_data": {}})
    run_id = r.json()["run_id"]
    run = _finished(run_id)
    if run.status.value == "waiting":            # a discussion asks before tools, like any chat
        live.post("/api/gateway/commands", headers=HEADERS, json={"command_id": "ok", "run_id": run_id, "type": "resume",
                                                                  "payload": {"wait_key": run.waiting.wait_key, "payload": {"approved": True}}})
        run = wait_until(lambda: (lambda x: x if x.status.value in ("completed", "failed") else None)(_store().load(run_id)), timeout_s=20)
    assert run.status.value == "completed", run.error
    results = run.output["results"] if "results" in (run.output or {}) else run.output["result"]["results"]
    assert os.path.realpath(out["workspace_root"]) in str(results[0].get("output"))


def test_own_workspace_equal_to_the_mount_is_impossible_from_the_door(live: TestClient) -> None:
    """The own folder is allocated by the gateway (the client names none)."""
    aid, _ = _writer_automation(live)
    r = live.post(f"/api/gateway/automations/{aid}/discuss", headers=HEADERS,
                  json={"request_id": "d3", "occurrence_index": 1, "prompt": "p", "workspace_root": "/tmp"})
    assert r.status_code == 422 and r.json()["detail"]["field"] == "workspace_root"


def test_the_guard_allows_only_declared_read_only_mounts(tmp_path: Path) -> None:
    from abstractgateway.run_workspace_guard import apply_builtin_tool_deny

    data = tmp_path / "data"
    own, mount = data / "workspaces" / "own", data / "workspaces" / "automation"
    own.mkdir(parents=True)
    mount.mkdir(parents=True)
    declared = {"workspace_root": str(own), "_runtime": {"workspace_read_only_paths": [str(mount)]}}
    apply_builtin_tool_deny(declared, root_data_dir=data, read_only_mounts=[str(mount)])
    assert declared["workspace_builtin_allow"] == [str(own.resolve()), os.path.realpath(str(mount))]
    # A mount the run does not declare read-only is never opened.
    with pytest.raises(ValueError):
        apply_builtin_tool_deny({"workspace_root": str(own)}, root_data_dir=data, read_only_mounts=[str(mount)])
    # And a client key alone opens nothing (mounts come only from the host argument).
    alone = {"workspace_root": str(own), "_runtime": {"workspace_read_only_paths": [str(data)]}}
    apply_builtin_tool_deny(alone, root_data_dir=data)
    assert alone["workspace_builtin_allow"] == [str(own.resolve())]


def test_entry_restamp_and_strip_units(live: TestClient) -> None:
    """The gateway's entry line of defence on its own (the runtime anchor
    re-stamps too, so the end-to-end test cannot tell them apart)."""
    from types import SimpleNamespace

    from abstractgateway.routes.gateway import _restamp_discussion_turn, _strip_client_automation_attribution

    aid, automation_ws = _writer_automation(live)
    out = live.post(f"/api/gateway/automations/{aid}/discuss", headers=HEADERS,
                    json={"request_id": "u1", "occurrence_index": 1, "prompt": "p"}).json()
    _finished(out["run_id"])
    from abstractgateway.service import get_gateway_service

    forged = {"workspace_root": "/", "workspace_access_mode": "all_except_ignored", "workspace_allowed_paths": ["/"],
              "workspace_read_only": False, "_runtime": {"workspace_read_only": False}}
    _strip_client_automation_attribution(forged)
    assert "workspace_read_only" not in forged and "workspace_read_only" not in forged["_runtime"]
    mounts = _restamp_discussion_turn(get_gateway_service(), session_id=out["session_id"], input_data=forged)
    assert forged["workspace_root"] == out["workspace_root"]
    assert forged["workspace_access_mode"] == "workspace_or_allowed"
    assert os.path.realpath(str(automation_ws)) in [os.path.realpath(p) for p in forged["workspace_allowed_paths"]]
    assert mounts == [os.path.realpath(str(automation_ws))] == forged["_runtime"]["workspace_read_only_paths"]
    # A client may ADD a read-only mount (the runtime unions them); the host
    # guard still opens nothing for it.
    added = {"_runtime": {"workspace_read_only_paths": [str(automation_ws.parent)]}}
    _restamp_discussion_turn(get_gateway_service(), session_id=out["session_id"], input_data=added)
    assert os.path.realpath(str(automation_ws.parent)) in added["_runtime"]["workspace_read_only_paths"]
    assert _restamp_discussion_turn(SimpleNamespace(host=SimpleNamespace(run_store=get_gateway_service().host.run_store)),
                                    session_id="plain-chat-session", input_data={}) == []
