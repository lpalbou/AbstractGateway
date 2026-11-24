"""Round 12 end to end (the operator's scenario): a run whose workspaces refuse a folder runs
`ls` of it through the REAL tool path (POST /runs/start → host → runtime stamp → AbstractCore
execute_command) and the OS sandbox refuses it; an allowed nested child of a refused parent is
listed; `~/.ssh` is unreadable; the run ledger keeps the per-command sandbox evidence.

macOS (sandbox-exec) and Windows (restricted token) for the positive sandbox assertions; elsewhere
the test asserts the fail-closed answer (no sandbox on this host) instead of silently passing.

On Windows, the two other workspace shapes ("Deny everything, allow listed workspaces", and "Allow
everything" with read-only by default) each have a scenario of their own, run when AbstractCore's
Windows sandbox enforces them (``host_sandbox_kind("allowed_only")``, read through the runtime's
facade) and skipped, naming the AbstractCore they need, before.
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

WINDOWS = sys.platform == "win32"
# The hosts whose sandbox this test proves, with the kind their results must carry.
KIND = {"darwin": "macos-sandbox-exec", "win32": "windows-restricted-token"}.get(sys.platform)


def _windows_enforces_every_shape() -> bool:
    if not WINDOWS:
        return False
    from abstractruntime.integrations.abstractcore.command_sandbox_host import host_sandbox_kind

    return host_sandbox_kind("allowed_only") == KIND


# The Windows sandbox of AbstractCore backlog 1341 enforces "Deny everything, allow listed workspaces"
# and the read-only default together; abstractcore 2.38.0 enforces neither.
WINDOWS_EVERY_SHAPE = _windows_enforces_every_shape()


def _in_json(path: Path) -> str:
    """A path as it reads inside JSON text (Windows separators are escaped there)."""
    return json.dumps(str(path))[1:-1]

_X = {"id": "exec-in", "label": "", "type": "execution"}
_XO = {"id": "exec-out", "label": "", "type": "execution"}


def _cmd_flow(fid: str, command) -> dict:
    """One tool_calls node running ``command`` (a list: one execute_command call each, in order)."""
    commands = command if isinstance(command, list) else [command]
    calls = [{"name": "execute_command", "arguments": {"command": cmd}} for cmd in commands]
    return {"id": fid, "name": fid, "entryNode": "start", "nodes": [
        {"id": "start", "type": "on_flow_start", "data": {"nodeType": "on_flow_start", "outputs": [_XO]}},
        {"id": "tools", "type": "tool_calls", "data": {"nodeType": "tool_calls", "inputs": [_X, {"id": "tool_calls", "label": "tool_calls", "type": "array"}],
                                                       "outputs": [_XO, {"id": "results", "label": "results", "type": "array"}],
                                                       "pinDefaults": {"tool_calls": calls}}},
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


# The Windows scenario of the two other shapes: one run, one execute_command call per step.
SHAPE_SCENARIO = [
    ("ls_refused", 'dir "{refused}"'),
    ("cat_refused_cd", 'cd /d "{home}" && type Desktop\\secret-desktop.txt'),
    ("cat_ssh", 'type "{ssh}\\id_marker"'),
    ("ls_parent", 'dir /b "{parent}"'),
    ("cat_parent", 'type "{parent}\\parent-only.txt"'),
    ("write_refused", 'echo x> "{refused}\\new.txt"'),
    ("ls_child", 'dir /b "{child}"'),
    ("write_child", 'echo w> "{child}\\new.txt" && type "{child}\\new.txt"'),
    ("read_ro", 'type "{docs}\\doc.txt"'),
    ("write_ro", 'echo x> "{docs}\\doc.txt" & echo x> "{docs}\\new.txt"'),
    ("read_outside", 'type "{outside}\\f.txt"'),
    ("write_outside", 'echo w> "{outside}\\w.txt"'),
    ("write_own", "echo mine> note.txt && type note.txt && echo TEMP=%TEMP%"),
]


@pytest.fixture()
def disk(tmp_path: Path) -> dict:
    home = tmp_path / "userhome"
    d = {
        "home": home,
        "refused": home / "Desktop",
        "parent": home / "work",
        "child": home / "work" / "project",
        "ssh": home / ".ssh",
        "docs": home / "Documents",
        "outside": home / "Music",
    }
    for p in d.values():
        p.mkdir(parents=True, exist_ok=True)
    (d["docs"] / "doc.txt").write_text("docs ok")
    (d["outside"] / "f.txt").write_text("outside ok")
    (d["refused"] / "secret-desktop.txt").write_text("desktop secret")
    (d["parent"] / "parent-only.txt").write_text("parent-only marker")
    (d["child"] / "child-file.txt").write_text("child")
    (d["ssh"] / "id_marker").write_text("ssh marker")
    return {k: Path(os.path.realpath(str(v))) for k, v in d.items()}


@pytest.fixture(autouse=True)
def _machine_is_the_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disk: dict):
    """No rule, integrity label or state file of these tests may land outside tmp_path.
    AbstractCore's Windows sandbox reads three views of the real machine (the user data roots
    refused under "Deny everything", the folders Windows already labels Low, its own state
    folder): each is replaced by a folder of the test tree, and every rule set is checked
    before the launcher runs (the commands run in this process: TestClient, runner thread)."""
    if not WINDOWS:
        return
    import abstractcore.tools.sandbox as core_sandbox

    root = Path(os.path.realpath(str(tmp_path)))
    if hasattr(core_sandbox, "_windows_user_data_roots"):
        monkeypatch.setattr(core_sandbox, "_windows_user_data_roots", lambda *a, **k: (str(disk["home"]),))
    if hasattr(core_sandbox, "_windows_low_places"):
        (disk["home"] / "AppData" / "LocalLow").mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(core_sandbox, "_windows_low_places", lambda *a, **k: (str(disk["home"] / "AppData" / "LocalLow"),))
    if hasattr(core_sandbox, "_windows_state_folder"):
        monkeypatch.setattr(core_sandbox, "_windows_state_folder", lambda *a, **k: str(root / "machine" / "state"))
    real_rules = core_sandbox.windows_rules

    def inside(p: str) -> bool:
        return os.path.normcase(os.path.realpath(p)).startswith(os.path.normcase(str(root)).rstrip(os.sep) + os.sep)

    def confined(spec):
        rules = real_rules(spec)
        outside = [r for r in rules for p in r[1:] if not inside(p)]
        assert not outside, f"SAFETY: a sandbox rule outside the test tree, nothing was launched: {outside}"
        return rules

    monkeypatch.setattr(core_sandbox, "windows_rules", confined)


@pytest.fixture()
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disk: dict):
    from abstractgateway import command_sandbox

    command_sandbox._reset_for_tests()
    monkeypatch.setenv("HOME", str(disk["home"]))
    monkeypatch.setenv("USERPROFILE", str(disk["home"]))  # the home folder, as Windows names it
    gateway_env(monkeypatch, tmp_path, runner=True)
    if WINDOWS:
        commands = {
            "ls_refused": f'dir "{disk["refused"]}"',
            "cat_refused_cd": f'cd /d "{disk["home"]}" && type Desktop\\secret-desktop.txt',
            "ls_child": f'dir /b "{disk["child"]}"',
            "ls_parent": f'dir /b "{disk["parent"]}"',
            "cat_ssh": f'type "{disk["ssh"]}\\id_marker"',
            "shape_scenario": [template.format(**{k: str(v) for k, v in disk.items()}) for _name, template in SHAPE_SCENARIO],
        }
    else:
        commands = {
            "ls_refused": f"ls -la {disk['refused']}",
            "cat_refused_cd": f"cd {disk['home']} && cat Desktop/secret-desktop.txt",
            "ls_child": f"ls {disk['child']}",
            "ls_parent": f"ls {disk['parent']}",
            "cat_ssh": f"cat {disk['ssh']}/id_marker",
        }
    ref = _bundle(tmp_path / "bundles", commands)
    from abstractgateway.app import app

    with TestClient(app) as c:
        c.bundle_ref = ref  # type: ignore[attr-defined]
        yield c
    command_sandbox._reset_for_tests()


def _store():
    from abstractgateway.service import get_gateway_service

    return get_gateway_service().host.run_store


def _run(c: TestClient, flow: str, workspace: dict) -> tuple:
    run_id, results = _run_all(c, flow, workspace)
    return run_id, results[0]


def _run_all(c: TestClient, flow: str, workspace: dict) -> tuple:
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
    return run_id, results


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

    run_id, res = _run(live, "ls_refused", ws)
    text = json.dumps(res)
    assert "secret-desktop.txt" not in text, text
    if KIND:
        assert res.get("sandbox", {}).get("kind") == KIND or f'"{KIND}"' in text, text
        assert _in_json(disk["refused"]) in text  # the result names the refused folder it enforced
    else:
        assert "not sandboxed" in text.lower() or "refused" in text.lower(), text
    # The ledger keeps the per-command sandbox evidence (the stamp and the result's `sandbox`).
    ledger = _ledger_text(run_id)
    assert "_sandbox" in ledger or '"sandbox"' in ledger, ledger[:2000]
    assert _in_json(disk["refused"]) in ledger or str(disk["refused"]) in ledger

    _rid, res = _run(live, "cat_refused_cd", ws)
    assert "desktop secret" not in json.dumps(res)

    _rid, res = _run(live, "cat_ssh", ws)
    assert "ssh marker" not in json.dumps(res)

    _rid, res = _run(live, "ls_parent", ws)
    assert "parent-only.txt" not in json.dumps(res)

    _rid, res = _run(live, "ls_child", ws)
    if KIND:
        assert "child-file.txt" in json.dumps(res), res  # positive control: the reopened child is listed


@pytest.mark.skipif(not WINDOWS, reason="the Windows scenario of the two other workspace shapes (macOS and Linux: AbstractCore's own tests)")
@pytest.mark.skipif(
    WINDOWS and not WINDOWS_EVERY_SHAPE,
    reason='needs an AbstractCore whose Windows sandbox enforces "Deny everything, allow listed workspaces" and the '
    "read-only default (the release after abstractcore 2.38.0, backlog 1341)",
)
@pytest.mark.parametrize("posture,default_mode", [("allowed_only", "rw"), ("any_except_denied", "ro")], ids=["allow-list", "any-ro"])
def test_the_operators_scenario_in_the_other_shapes(live: TestClient, disk: dict, posture: str, default_mode: str) -> None:
    # Refuse ~/Desktop and ~/work, reopen ~/work/project read & write, ~/Documents read-only;
    # ~/Music is listed nowhere.
    folders = [{"path": str(disk["refused"]), "mode": "deny"}, {"path": str(disk["parent"]), "mode": "deny"},
               {"path": str(disk["child"]), "mode": "rw"}, {"path": str(disk["docs"]), "mode": "ro"}]  # fmt: skip
    r = live.put("/api/gateway/workspace/policy", headers=HEADERS, json={"posture": posture, "default_mode": default_mode, "folders": folders})
    assert r.status_code == 200, r.text
    # The run's one-off workspace picks the same rows (an empty one-off under "Deny everything"
    # reaches no row at all, and under a read-only default makes no row writable).
    _run_id, results = _run_all(live, "shape_scenario", {"posture": posture, "default_mode": default_mode, "folders": folders})
    assert len(results) == len(SHAPE_SCENARIO), results
    by: dict = {}
    for (name, _template), res in zip(SHAPE_SCENARIO, results):
        out = res.get("output") if isinstance(res.get("output"), dict) else res
        assert "return_code" in out, f"{name} never ran under {posture}/{default_mode}: {json.dumps(res, default=str)[:600]}"
        assert (out.get("sandbox") or {}).get("kind") == KIND and out["sandbox"].get("posture") == posture, (name, out.get("sandbox"))
        assert out["sandbox"].get("default_mode") == default_mode or posture == "allowed_only", (name, out.get("sandbox"))
        by[name] = json.dumps(out, default=str)
    # Refused rows and the built-in refusal: unreadable and unwritable.
    evidence = json.dumps((json.loads(by["ls_child"]).get("sandbox") or {}), default=str)
    assert "secret-desktop.txt" not in by["ls_refused"] and "desktop secret" not in by["cat_refused_cd"], evidence
    assert "ssh marker" not in by["cat_ssh"], f"{by['cat_ssh'][:800]}\n{evidence}"
    # ~/work holds the reopened child: under these two shapes the NAMES directly inside it are
    # listed (AbstractCore's accepted limit: the folders on the way to a row list their names,
    # git needs it), its content never.
    assert "parent-only.txt" in by["ls_parent"], f"the names on the way to a row are no longer listed: {by['ls_parent'][:800]}"
    assert "parent-only marker" not in by["cat_parent"], f"{by['cat_parent'][:800]}\n{evidence}"
    assert not (disk["refused"] / "new.txt").exists()
    # Positive controls: the reopened child is listed and written, the read-only row is read, the
    # run's own folder is written and TEMP is its own .tmp.
    assert "child-file.txt" in by["ls_child"] and (disk["child"] / "new.txt").exists(), f"{by['ls_child'][:800]}\n{evidence}"
    assert "docs ok" in by["read_ro"] and "mine" in by["write_own"] and "\\\\.tmp" in by["write_own"], by["write_own"]
    # Read-only row, and an unlisted folder: never written under these two shapes.
    assert (disk["docs"] / "doc.txt").read_text() == "docs ok" and not (disk["docs"] / "new.txt").exists()
    assert not (disk["outside"] / "w.txt").exists(), f"an unlisted folder was written under {posture}/{default_mode}"
    # An unlisted folder of the user's home: readable under "Allow everything", hidden under the allow-list.
    assert ("outside ok" in by["read_outside"]) is (posture == "any_except_denied"), by["read_outside"]


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
        assert "Operation not permitted" in text, text
    if KIND:
        assert KIND in text, text
    ledger = _ledger_text(run_id)
    assert _in_json(disk["refused"]) in ledger or str(disk["refused"]) in ledger
