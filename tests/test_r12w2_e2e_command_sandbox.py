"""Round 12 end to end (the operator's scenario): a run whose workspaces refuse a folder runs
`ls` of it through the REAL tool path (POST /runs/start → host → runtime stamp → AbstractCore
execute_command) and the OS sandbox refuses it; an allowed nested child of a refused parent is
listed; `~/.ssh` is unreadable; the run ledger keeps the per-command sandbox evidence.

macOS only for the positive sandbox assertions (sandbox-exec); elsewhere the test asserts the
fail-closed answer (no sandbox on this host) instead of silently passing.
"""

from __future__ import annotations

import json
import os
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import HEADERS, gateway_env, wait_until

pytestmark = pytest.mark.basic

_X = {"id": "exec-in", "label": "", "type": "execution"}
_XO = {"id": "exec-out", "label": "", "type": "execution"}


def _cmd_flow(fid: str, command: str) -> dict:
    return {"id": fid, "name": fid, "entryNode": "start", "nodes": [
        {"id": "start", "type": "on_flow_start", "data": {"nodeType": "on_flow_start", "outputs": [_XO]}},
        {"id": "tools", "type": "tool_calls", "data": {"nodeType": "tool_calls", "inputs": [_X, {"id": "tool_calls", "label": "tool_calls", "type": "array"}],
                                                       "outputs": [_XO, {"id": "results", "label": "results", "type": "array"}],
                                                       "pinDefaults": {"tool_calls": [{"name": "execute_command", "arguments": {"command": command}}]}}},
        {"id": "end", "type": "on_flow_end", "data": {"nodeType": "on_flow_end", "inputs": [_X, {"id": "results", "label": "results", "type": "array"}]}},
    ], "edges": [
        {"source": "start", "sourceHandle": "exec-out", "target": "tools", "targetHandle": "exec-in"},
        {"source": "tools", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"},
        {"source": "tools", "sourceHandle": "results", "target": "end", "targetHandle": "results"},
    ]}


def _bundle(bundles: Path, flows: dict) -> str:
    bundles.mkdir(parents=True, exist_ok=True)
    manifest = {
        "bundle_format_version": "1", "bundle_id": "r12w2-cmd", "bundle_version": "1.0.0",
        "created_at": "2026-10-04T00:00:00+00:00",
        "entrypoints": [{"flow_id": fid, "name": fid, "description": "", "interfaces": []} for fid in flows],
        "flows": {fid: f"flows/{fid}.json" for fid in flows}, "artifacts": {}, "assets": {}, "metadata": {},
    }
    with zipfile.ZipFile(bundles / "r12w2-cmd.flow", "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        for fid, command in flows.items():
            zf.writestr(f"flows/{fid}.json", json.dumps(_cmd_flow(fid, command)))
    return "r12w2-cmd@1.0.0"


@pytest.fixture()
def disk(tmp_path: Path) -> dict:
    home = tmp_path / "userhome"
    d = {
        "home": home,
        "refused": home / "Desktop",
        "parent": home / "work",
        "child": home / "work" / "project",
        "ssh": home / ".ssh",
    }
    for p in d.values():
        p.mkdir(parents=True, exist_ok=True)
    (d["refused"] / "secret-desktop.txt").write_text("desktop secret")
    (d["parent"] / "parent-only.txt").write_text("parent")
    (d["child"] / "child-file.txt").write_text("child")
    (d["ssh"] / "id_marker").write_text("ssh marker")
    return {k: Path(os.path.realpath(str(v))) for k, v in d.items()}


@pytest.fixture()
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disk: dict):
    from abstractgateway import command_sandbox

    command_sandbox._reset_for_tests()
    monkeypatch.setenv("HOME", str(disk["home"]))
    gateway_env(monkeypatch, tmp_path, runner=True)
    ref = _bundle(tmp_path / "bundles", {
        "ls_refused": f"ls -la {disk['refused']}",
        "cat_refused_cd": f"cd {disk['home']} && cat Desktop/secret-desktop.txt",
        "ls_child": f"ls {disk['child']}",
        "ls_parent": f"ls {disk['parent']}",
        "cat_ssh": f"cat {disk['ssh']}/id_marker",
    })
    from abstractgateway.app import app

    with TestClient(app) as c:
        c.bundle_ref = ref  # type: ignore[attr-defined]
        yield c
    command_sandbox._reset_for_tests()


def _store():
    from abstractgateway.service import get_gateway_service

    return get_gateway_service().host.run_store


def _run(c: TestClient, flow: str, workspace: dict) -> tuple:
    r = c.post("/api/gateway/runs/start", headers=HEADERS, json={"bundle_id": c.bundle_ref, "flow_id": flow, "input_data": {}, "workspace": workspace})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    done = lambda: (lambda x: x if x is not None and x.status.value in ("completed", "failed", "waiting") else None)(_store().load(run_id))  # noqa: E731
    run = wait_until(done, timeout_s=30)
    if run.status.value == "waiting":
        c.post("/api/gateway/commands", headers=HEADERS, json={"command_id": f"ok-{run_id}", "run_id": run_id, "type": "resume",
                                                             "payload": {"wait_key": run.waiting.wait_key, "payload": {"approved": True}}})
        run = wait_until(lambda: (lambda x: x if x.status.value in ("completed", "failed") else None)(_store().load(run_id)), timeout_s=30)
    out = run.output or {}
    results = out.get("results") if "results" in out else (out.get("result") or {}).get("results")
    assert isinstance(results, list) and results, (run.status, run.error, out)
    print(f"[r12w2-e2e] {flow}: {json.dumps(results[0], default=str)[:900]}")
    return run_id, results[0]


def _ledger_text(run_id: str) -> str:
    from abstractgateway.service import get_gateway_service

    store = get_gateway_service().stores.ledger_store
    return json.dumps([getattr(rec, "__dict__", rec) for rec in store.list(run_id)], default=str)


def test_the_operators_scenario(live: TestClient, disk: dict) -> None:
    # The admin: allow everything, refuse ~/Desktop and ~/work, but allow ~/work/project (rw).
    r = live.put("/api/gateway/workspace/policy", headers=HEADERS, json={"posture": "any_except_denied", "default_mode": "rw", "folders": [
        {"path": str(disk["refused"]), "mode": "deny"}, {"path": str(disk["parent"]), "mode": "deny"}, {"path": str(disk["child"]), "mode": "rw"}]})
    assert r.status_code == 200, r.text
    ws = {"posture": "any_except_denied", "default_mode": "rw", "folders": []}
    darwin = sys.platform == "darwin"

    run_id, res = _run(live, "ls_refused", ws)
    text = json.dumps(res)
    assert "secret-desktop.txt" not in text, text
    if darwin:
        assert res.get("sandbox", {}).get("kind") == "macos-sandbox-exec" or '"macos-sandbox-exec"' in text, text
        assert str(disk["refused"]) in json.dumps(res.get("sandbox", {}).get("refused") or text)
    else:
        assert "not sandboxed" in text.lower() or "refused" in text.lower(), text
    # The ledger keeps the per-command sandbox evidence (the stamp and the result's `sandbox`).
    ledger = _ledger_text(run_id)
    assert "_sandbox" in ledger or '"sandbox"' in ledger, ledger[:2000]
    assert str(disk["refused"]) in ledger

    _rid, res = _run(live, "cat_refused_cd", ws)
    assert "desktop secret" not in json.dumps(res)

    _rid, res = _run(live, "cat_ssh", ws)
    assert "ssh marker" not in json.dumps(res)

    _rid, res = _run(live, "ls_parent", ws)
    assert "parent-only.txt" not in json.dumps(res)

    _rid, res = _run(live, "ls_child", ws)
    if darwin:
        assert "child-file.txt" in json.dumps(res), res  # positive control: the reopened child is listed


def test_the_in_process_door_too(live: TestClient, disk: dict) -> None:
    """host.start_run (bridges, schedules, automations, entities): a one-off run workspace refusing a
    folder reaches the sandbox exactly like the HTTP door."""
    from abstractgateway.service import get_gateway_service

    ws = {"posture": "any_except_denied", "default_mode": "rw", "folders": [{"path": str(disk["refused"]), "mode": "deny"}]}
    host = get_gateway_service().host
    run_id = host.start_run(flow_id="ls_refused", bundle_id=live.bundle_ref, input_data={"workspace": ws}, session_id=None)
    done = lambda: (lambda x: x if x is not None and x.status.value in ("completed", "failed", "waiting") else None)(_store().load(run_id))  # noqa: E731
    run = wait_until(done, timeout_s=30)
    if run.status.value == "waiting":
        live.post("/api/gateway/commands", headers=HEADERS, json={"command_id": f"ok-{run_id}", "run_id": run_id, "type": "resume",
                                                                "payload": {"wait_key": run.waiting.wait_key, "payload": {"approved": True}}})
        run = wait_until(lambda: (lambda x: x if x.status.value in ("completed", "failed") else None)(_store().load(run_id)), timeout_s=30)
    out = run.output or {}
    results = out.get("results") if "results" in out else (out.get("result") or {}).get("results")
    text = json.dumps(results, default=str)
    assert "secret-desktop.txt" not in text, text
    if sys.platform == "darwin":
        assert "Operation not permitted" in text and "macos-sandbox-exec" in text, text
    assert str(disk["refused"]) in _ledger_text(run_id)
