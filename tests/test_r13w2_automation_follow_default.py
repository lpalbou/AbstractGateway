"""Round 13 (R13-W2 follow-up): an automation saved with "Use my default" FOLLOWS its owner's default
workspaces at each run — the definition stores ``workspace: {"configured": false}``, never a
snapshot of the default; each occurrence's admission resolves session > account > gateway through
the runtime's ``set_occurrence_input_resolver`` hook (wired per plane by the bundle host).

On the real automation door (POST /automations, run now, the runner ticking the controller):
- widen the account default after creation -> the NEXT occurrence sees the wider set;
- a revision echoing an old snapshot (derived keys) is ignored, never a narrowing;
- a pre-rule definition (no payload, gateway-derived keys at the account level) is recognised as
  following;
- an explicit workspace payload is kept and applied as before (no change).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import HEADERS, gateway_env, wait_until, write_echo_bundle

pytestmark = pytest.mark.basic


@pytest.fixture()
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway_env(monkeypatch, tmp_path, runner=True)
    ref = write_echo_bundle(tmp_path / "bundles")
    from abstractgateway.app import app

    with TestClient(app) as c:
        c.bundle_ref = ref  # type: ignore[attr-defined]
        yield c


def _dirs(tmp_path: Path) -> dict:
    out = {}
    for name in ("pictures", "downloads", "documents"):
        p = tmp_path / "disk" / name
        p.mkdir(parents=True, exist_ok=True)
        out[name] = os.path.realpath(str(p))
    return out


def _store():
    from abstractgateway.service import get_gateway_service

    return get_gateway_service().host.run_store


def _set_default(c: TestClient, folders: list) -> None:
    r = c.put("/api/gateway/workspace/policy/me", headers=HEADERS, json={"configured": True, "posture": "allowed_only", "default_mode": "rw", "folders": folders})
    assert r.status_code == 200, r.text


def _definition(c: TestClient, aid: str) -> dict:
    r = c.get(f"/api/gateway/automations/{aid}", headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


def _run_now(c: TestClient, aid: str, n: int) -> dict:
    """Run one occurrence now; answer the vars its run started with (the frozen admission)."""
    r = c.post(f"/api/gateway/automations/{aid}/commands", headers=HEADERS, json={"command_id": f"go-{n}", "type": "automation.run_now"})
    assert r.status_code == 200, r.text
    rows = wait_until(
        lambda: (lambda items: items if len(items) >= n and items[0]["status"] in ("completed", "failed") else None)(
            c.get(f"/api/gateway/automations/{aid}/occurrences", headers=HEADERS).json()["items"]
        ),
        timeout_s=30,
    )
    newest = max(rows, key=lambda row: row["index"])
    run = _store().load(newest["run_id"])
    assert run is not None
    return dict(run.vars or {})


def _create(c: TestClient, request_id: str, input_data: dict) -> str:
    r = c.post("/api/gateway/automations", headers=HEADERS, json={
        "request_id": request_id, "title": request_id,
        "target": {"bundle_ref": c.bundle_ref, "flow_id": "echo", "input_data": input_data},
        "trigger": {"source_id": "manual", "source_version": 1, "config": {}}})
    assert r.status_code == 200, r.text
    return r.json()["automation_id"]


def test_use_my_default_follows_the_account_default_at_each_run(live: TestClient, tmp_path: Path) -> None:
    d = _dirs(tmp_path)
    _set_default(live, [{"path": d["pictures"], "mode": "rw"}])
    aid = _create(live, "follow", {"prompt": "sort the photos"})
    stored = _definition(live, aid)["definition"]["target"]["input_data"]
    # The definition says "follow", and carries no snapshot of the default.
    assert stored["workspace"] == {"configured": False}
    assert "workspace_allowed_paths" not in stored and "_gateway_workspace" not in stored
    assert stored["workspace_access_mode"] == "workspace_only"  # fail-closed without the resolver

    first = _run_now(live, aid, 1)
    assert first["workspace_allowed_paths"] == [d["pictures"]]
    assert first["_gateway_workspace"]["level"] == "account"
    assert first["workspace"] == {"configured": False}

    # The owner widens the default: the NEXT run sees the wider set (no revision needed).
    _set_default(live, [{"path": d["pictures"], "mode": "rw"}, {"path": d["downloads"], "mode": "rw"}])
    second = _run_now(live, aid, 2)
    assert sorted(second["workspace_allowed_paths"]) == sorted([d["pictures"], d["downloads"]])
    assert _definition(live, aid)["definition"]["revision"] == 1


def test_an_echoed_old_snapshot_is_ignored(live: TestClient, tmp_path: Path) -> None:
    d = _dirs(tmp_path)
    _set_default(live, [{"path": d["pictures"], "mode": "rw"}])
    aid = _create(live, "echo-snapshot", {"prompt": "x"})
    body = _definition(live, aid)
    target = body["definition"]["target"]
    # A client edits another field and echoes an OLD snapshot (derived keys of a narrower default).
    echoed = {**target["input_data"], "prompt": "y", "workspace_allowed_paths": [d["pictures"]],
              "workspace_access_mode": "workspace_or_allowed", "_gateway_workspace": {"level": "account", "summary": "old"}}
    r = live.patch(f"/api/gateway/automations/{aid}", headers=HEADERS, json={
        "command_id": "edit-1", "expected_revision": body["summary"]["revision"],
        "changes": {"target": {"bundle_ref": target["bundle_ref"], "flow_id": target["flow_id"], "input_data": echoed}}})
    assert r.status_code == 200, r.text
    wait_until(lambda: _definition(live, aid)["definition"]["revision"] == 2, timeout_s=20)
    stored = _definition(live, aid)["definition"]["target"]["input_data"]
    assert stored["prompt"] == "y"
    assert stored["workspace"] == {"configured": False}
    assert "workspace_allowed_paths" not in stored
    # Widen after the echo: the run is NOT narrowed to the echoed snapshot.
    _set_default(live, [{"path": d["pictures"], "mode": "rw"}, {"path": d["documents"], "mode": "ro"}])
    run_vars = _run_now(live, aid, 1)
    assert sorted(run_vars["workspace_allowed_paths"]) == sorted([d["pictures"], d["documents"]])


def test_an_explicit_choice_is_kept_as_before(live: TestClient, tmp_path: Path) -> None:
    d = _dirs(tmp_path)
    _set_default(live, [{"path": d["pictures"], "mode": "rw"}])
    chosen = {"posture": "allowed_only", "default_mode": "rw", "folders": [{"path": d["downloads"], "mode": "rw"}]}
    aid = _create(live, "explicit", {"prompt": "x", "workspace": chosen})
    assert _definition(live, aid)["definition"]["target"]["input_data"]["workspace"] == chosen
    _set_default(live, [{"path": d["pictures"], "mode": "rw"}, {"path": d["documents"], "mode": "rw"}])
    run_vars = _run_now(live, aid, 1)
    assert run_vars["workspace_allowed_paths"] == [d["downloads"]]
    assert run_vars["_gateway_workspace"]["level"] == "run"


def test_follows_default_recognises_pre_rule_definitions_and_the_host_wires_the_hook(live: TestClient, tmp_path: Path) -> None:
    from abstractgateway.automation_workspace_resolver import follows_default
    from abstractgateway.service import get_gateway_service

    assert follows_default({"workspace": {"configured": False}})
    assert follows_default({"workspace_allowed_paths": ["/x"], "_gateway_workspace": {"level": "account"}})
    assert not follows_default({"workspace_allowed_paths": ["/x"]})  # a round-9 client list: explicit
    assert not follows_default({"workspace": {"posture": "allowed_only", "default_mode": "rw", "folders": []}})
    assert not follows_default({"_gateway_workspace": {"level": "run"}})
    assert callable(get_gateway_service().host.runtime.occurrence_input_resolver)


def test_a_runtime_without_the_hook_is_refused_loudly() -> None:
    from abstractgateway.automation_workspace_resolver import wire_occurrence_workspaces

    with pytest.raises(RuntimeError, match="set_occurrence_input_resolver"):
        wire_occurrence_workspaces(object(), data_dir="/tmp", root_data_dir="/tmp", tenant_id="default", user_id="admin")
