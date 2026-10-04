"""Round 12, R12.2 (DESIGN.md): the MOST SPECIFIC row wins (the longest real-path prefix).

- Refusing ``home`` while allowing ``home/project`` (rw) is VALID at the gateway, account and session
  levels; the child is reachable, the rest of ``home`` refused; a refused row inside an allowed one
  refuses that subtree. The round-11 sentence "nothing re-opens under a refusal" is gone.
- Built-in refusals are absolute: an ro/rw row inside one is refused with
  "'X' is inside the built-in refused workspace 'Y'." (gateway, account, session, one-off run).
- Caps: a child under a listed gateway row never exceeds that row's cap; a child under a REFUSED
  gateway row takes its own mode only when the gateway lists it.
- The effective payload, its summary, the dry run and the run-start args on every door (POST
  /runs/start, host.start_run, the automation guard) carry the same answer; the refused rows always
  reach the runtime as ``workspace_ignored_paths`` and the reopened child as an allowed path, which the
  runtime resolves with the same rule ("R12 NESTING RULE — FINAL"; runtime test at the end).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_gateway_session_workspace_reuse import _client as _bundle_client
from test_r11w1_levels import _can, _starts

pytestmark = pytest.mark.basic

_TOKEN = "workspace-r12-admin-secret"


def _dirs(tmp_path: Path) -> dict:
    out = {}
    for rel in ("home", "home/project", "home/project/private", "home/Desktop", "data", "data/archive", "data/archive/scratch", "elsewhere"):
        p = tmp_path / "disk" / rel
        p.mkdir(parents=True, exist_ok=True)
        (p / "seed.txt").write_text(rel)
        out[rel.replace("/", "_")] = str(p.resolve())
    return out


@pytest.fixture()
def ua(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    home = tmp_path / "userhome"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"}) as c:
        yield c


def _user(user_id: str) -> dict:
    from abstractgateway.users import GatewayUserRegistry

    _rec, token = GatewayUserRegistry().create_user(user_id=user_id, roles=["user"])
    return {"Authorization": f"Bearer {token}"}


def _refusal(r) -> dict:
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["reason"] == "workspace_refused", detail
    return detail


def _put_gateway(c: TestClient, body: dict, headers: dict | None = None):
    return c.put("/api/gateway/workspace/policy", json=body, headers=headers or {})


NESTED_SUMMARY = "Allow everything, refuse listed workspaces (rw) · {home} (refused) · {home_project} (rw)"


def test_gateway_level_refused_parent_allowed_child_is_valid(ua: TestClient, tmp_path: Path) -> None:
    d = _dirs(tmp_path)
    r = _put_gateway(ua, {"posture": "any_except_denied", "default_mode": "rw", "folders": [
        {"path": d["home"], "mode": "deny"}, {"path": d["home_project"], "mode": "rw"}]})
    assert r.status_code == 200, r.text
    g = r.json()["policy"]
    assert g["summary"] == NESTED_SUMMARY.format(**d)

    from abstractgateway.workspace_policy import effective_folder_paths

    eff = effective_folder_paths(tmp_path / "runtime", tenant_id="default", user_id="admin")
    assert eff.mode(Path(d["home_project"]) / "seed.txt") == "rw"
    assert eff.mode(Path(d["home_project_private"])) == "rw"
    assert eff.mode(Path(d["home_Desktop"]) / "seed.txt") == "deny"
    assert eff.mode(Path(d["home"]) / "seed.txt") == "deny"
    assert eff.mode(Path(d["elsewhere"])) == "rw"
    assert eff.refusal(Path(d["home_project"])) is None and eff.refusal(Path(d["home_Desktop"])) is not None

    # A refused row inside an allowed one refuses that subtree (and the order of rows is irrelevant).
    r = _put_gateway(ua, {"posture": "allowed_only", "folders": [
        {"path": d["home_project_private"], "mode": "deny"}, {"path": d["home_project"], "mode": "rw"}]})
    assert r.status_code == 200, r.text
    eff = effective_folder_paths(tmp_path / "runtime", tenant_id="default", user_id="admin")
    assert eff.mode(Path(d["home_project"]) / "seed.txt") == "rw"
    assert eff.mode(Path(d["home_project_private"]) / "seed.txt") == "deny"
    assert eff.mode(Path(d["home_Desktop"])) == "deny"


def test_the_round_11_sentence_is_deleted() -> None:
    src = Path(__file__).resolve().parents[1] / "src" / "abstractgateway"
    hits = [p for p in src.rglob("*.py") if "nothing re-opens" in p.read_text(encoding="utf-8")]
    assert hits == []


def test_account_and_session_levels_accept_the_nested_pair(ua: TestClient, tmp_path: Path) -> None:
    d = _dirs(tmp_path)
    assert _put_gateway(ua, {"posture": "any_except_denied", "default_mode": "rw", "folders": []}).status_code == 200
    alice = _user("alice")
    nested = {"posture": "any_except_denied", "default_mode": "rw", "folders": [
        {"path": d["home"], "mode": "deny"}, {"path": d["home_project"], "mode": "rw"}]}

    r = ua.put("/api/gateway/workspace/policy/me", json=nested, headers=alice)
    assert r.status_code == 200, r.text
    eff = ua.get("/api/gateway/workspace/effective/me", headers=alice).json()
    assert eff["level"] == "account" and eff["summary"] == NESTED_SUMMARY.format(**d)
    assert {"path": d["home"], "mode": "deny", "cap": "rw", "source": "account"} in eff["folders"]
    assert {"path": d["home_project"], "mode": "rw", "cap": "rw", "source": "account"} in eff["folders"]

    sess = {"posture": "allowed_only", "default_mode": "rw", "folders": [
        {"path": d["home_project"], "mode": "rw"}, {"path": d["home_project_private"], "mode": "deny"}]}
    r = ua.put("/api/gateway/sessions/chat-n/workspaces", json=sess, headers=alice)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["effective"]["level"] == "session"
    assert body["effective"]["summary"] == (
        f"Deny everything, allow listed workspaces · {d['home_project']} (rw) · {d['home_project_private']} (refused)"
    )
    # The same session the other way round: refused parent, allowed child.
    r = ua.put("/api/gateway/sessions/chat-n/workspaces", json=nested, headers=alice)
    assert r.status_code == 200, r.text
    assert r.json()["effective"]["summary"] == NESTED_SUMMARY.format(**d)


def test_caps_bind_a_nested_child(ua: TestClient, tmp_path: Path) -> None:
    d = _dirs(tmp_path)
    # The gateway: data read-only, home refused with home/project listed read-only.
    r = _put_gateway(ua, {"posture": "any_except_denied", "default_mode": "rw", "folders": [
        {"path": d["data"], "mode": "ro"}, {"path": d["home"], "mode": "deny"}, {"path": d["home_project"], "mode": "ro"}]})
    assert r.status_code == 200, r.text
    alice = _user("alice")

    def put(rows):
        return ua.put("/api/gateway/workspace/policy/me", json={"posture": "allowed_only", "folders": rows}, headers=alice)

    # A child of a listed read-only parent cannot be read & write…
    detail = _refusal(put([{"path": d["data_archive"], "mode": "rw"}]))
    assert detail["message"] == f"The gateway allows this workspace read-only: {d['data_archive']}."
    assert put([{"path": d["data_archive"], "mode": "ro"}]).status_code == 200
    # …nor can a grandchild under a read-only child of a refused parent.
    detail = _refusal(put([{"path": d["home_project_private"], "mode": "rw"}]))
    assert detail["message"] == f"The gateway allows this workspace read-only: {d['home_project_private']}."
    assert put([{"path": d["home_project_private"], "mode": "ro"}]).status_code == 200
    # Under the refused parent and not listed by the gateway: outside the eligible set.
    detail = _refusal(put([{"path": d["home_Desktop"], "mode": "ro"}]))
    assert "outside the workspaces the gateway allows" in detail["message"] and detail["path"] == d["home_Desktop"]
    # The account's own refusal over an allowed child narrows; its own allowed child stays ≤ cap.
    r = put([{"path": d["data"], "mode": "deny"}, {"path": d["data_archive_scratch"], "mode": "ro"}])
    assert r.status_code == 200, r.text
    eff = ua.get("/api/gateway/workspace/effective/me", headers=alice).json()
    assert {"path": d["data_archive_scratch"], "mode": "ro", "cap": "ro", "source": "account"} in eff["folders"]


def test_builtin_refusals_are_absolute_at_every_level(ua: TestClient, tmp_path: Path) -> None:
    d = _dirs(tmp_path)
    data = (tmp_path / "runtime").resolve()
    (data / "config").mkdir(parents=True, exist_ok=True)
    ssh = (tmp_path / "userhome" / ".ssh").resolve()
    (ssh / "keys").mkdir(exist_ok=True)
    alice = _user("alice")

    for path, hit in ((str(data / "config"), str(data)), (str(ssh / "keys"), str(ssh)), (str(ssh), str(ssh))):
        sentence = f"'{path}' is inside the built-in refused workspace '{hit}'."
        # gateway (even with a refused parent row around it)
        detail = _refusal(_put_gateway(ua, {"folders": [{"path": str(tmp_path), "mode": "deny"}, {"path": path, "mode": "ro"}]}))
        assert detail["message"] == f"Workspaces: {sentence}" and detail["path"] == path
        # account
        detail = _refusal(ua.put("/api/gateway/workspace/policy/me", json={"posture": "allowed_only", "folders": [{"path": path, "mode": "ro"}]}, headers=alice))
        assert detail["message"] == f"Workspaces: {sentence}"
        # session
        detail = _refusal(ua.put("/api/gateway/sessions/chat-b/workspaces", json={"posture": "allowed_only", "folders": [{"path": path, "mode": "rw"}]}, headers=alice))
        assert detail["message"] == f"Workspaces: {sentence}"
        # dry run (one-off)
        detail = _refusal(ua.post("/api/gateway/workspace/effective/me", json={"workspace": {"posture": "allowed_only", "folders": [{"path": path, "mode": "ro"}]}}, headers=alice))
        assert detail["message"] == f"Workspaces: {sentence}"
    # A refusal row there never widens: accepted.
    assert ua.put("/api/gateway/workspace/policy/me", json={"posture": "any_except_denied", "folders": [{"path": str(ssh / "keys"), "mode": "deny"}]}, headers=alice).status_code == 200

    # In-process doors clamp with the same sentence (recorded, never silent).
    from abstractgateway.workspace_policy import caps_for, clamp_layer

    clamped: list = []
    out = clamp_layer(caps_for(data, tenant_id="default", user_id="alice"), {"posture": "allowed_only", "folders": [{"path": str(ssh / "keys"), "mode": "rw"}]}, clamped)
    assert out["folders"] == [] and clamped[0]["sentence"] == f"'{ssh / 'keys'}' is inside the built-in refused workspace '{ssh}'."


def test_dry_run_and_summary_follow_the_rule(ua: TestClient, tmp_path: Path) -> None:
    d = _dirs(tmp_path)
    assert _put_gateway(ua, {"posture": "any_except_denied", "default_mode": "rw", "folders": [{"path": d["home"], "mode": "deny"}, {"path": d["home_project"], "mode": "rw"}]}).status_code == 200
    alice = _user("alice")
    payload = {"posture": "allowed_only", "default_mode": "rw", "folders": [{"path": d["home_project"], "mode": "rw"}, {"path": d["home_project_private"], "mode": "deny"}]}
    r = ua.post("/api/gateway/workspace/effective/me", json={"workspace": payload}, headers=alice)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["level"] == "run"
    assert body["summary"] == f"Deny everything, allow listed workspaces · {d['home_project']} (rw) · {d['home_project_private']} (refused)"
    assert body["gateway_summary"] == NESTED_SUMMARY.format(**d)
    # The gateway's refused parent is NOT an eligible row for the user: refused at the dry run.
    detail = _refusal(ua.post("/api/gateway/workspace/effective/me", json={"workspace": {"posture": "allowed_only", "folders": [{"path": d["home_Desktop"], "mode": "ro"}]}}, headers=alice))
    assert detail["path"] == d["home_Desktop"]


@pytest.mark.parametrize("door", ["http", "host", "automation"])
def test_run_start_args_carry_the_nested_set_on_every_door(door: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _dirs(tmp_path)
    client, headers = _bundle_client(tmp_path, monkeypatch)
    with client:
        r = client.put("/api/gateway/workspace/policy", json={"posture": "any_except_denied", "default_mode": "rw", "folders": [
            {"path": d["home"], "mode": "deny"}, {"path": d["home_project"], "mode": "rw"}, {"path": d["home_project_private"], "mode": "deny"},
            {"path": d["data"], "mode": "ro"}]}, headers=headers)
        assert r.status_code == 200, r.text
        start = _starts(client, headers, tmp_path)[door]

        v = start("chat-r12", {})
        assert v["workspace_access_mode"] == "all_except_ignored"
        ignored = [ln for ln in str(v.get("workspace_ignored_paths") or "").splitlines() if ln]
        assert d["home"] in ignored and d["home_project_private"] in ignored
        assert d["home_project"] in v["workspace_allowed_paths"] and d["data"] in v["workspace_allowed_paths"]
        assert d["data"] in v["workspace_read_only_paths"]
        assert v["_gateway_workspace"]["summary"] == (
            f"Allow everything, refuse listed workspaces (rw) · {d['home']} (refused) · {d['home_project']} (rw) · "
            f"{d['home_project_private']} (refused) · {d['data']} (ro)"
        )

        # A one-off run that refuses a folder of its own: the refusal reaches the runtime too.
        v = start("chat-r12b", {"workspace": {"posture": "any_except_denied", "default_mode": "rw", "folders": [{"path": d["elsewhere"], "mode": "deny"}]}})
        ignored = [ln for ln in str(v.get("workspace_ignored_paths") or "").splitlines() if ln]
        assert d["elsewhere"] in ignored and d["home"] in ignored


@pytest.mark.parametrize("door", ["http", "host", "automation"])
def test_the_runtime_enforces_the_same_rule(door: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The file tools of the run (runtime scope) answer exactly as the gateway: needs the round-12
    runtime (R12-W1, "R12 NESTING RULE — FINAL"); the round-11 runtime refuses the reopened child."""
    d = _dirs(tmp_path)
    client, headers = _bundle_client(tmp_path, monkeypatch)
    with client:
        r = client.put("/api/gateway/workspace/policy", json={"posture": "any_except_denied", "default_mode": "rw", "folders": [
            {"path": d["home"], "mode": "deny"}, {"path": d["home_project"], "mode": "rw"}, {"path": d["home_project_private"], "mode": "deny"}]}, headers=headers)
        assert r.status_code == 200, r.text
        v = _starts(client, headers, tmp_path)[door]("chat-rt", {})
    from abstractgateway.workspace_policy import effective_folder_paths

    eff = effective_folder_paths(tmp_path / "runtime", tenant_id="default", user_id="admin")
    for path, readable in (
        (Path(d["home_project"]) / "seed.txt", True),
        (Path(d["home_project_private"]) / "seed.txt", False),
        (Path(d["home_Desktop"]) / "seed.txt", False),
        (Path(d["home"]) / "seed.txt", False),
        (Path(d["elsewhere"]) / "seed.txt", True),
    ):
        assert (eff.mode(path) != "deny") is readable, path
        assert _can(v, "read_file", path) is readable, (door, path)
    assert _can(v, "write_file", Path(d["home_project"]) / "new.txt")


def test_server_file_routes_follow_the_rule(ua: TestClient, tmp_path: Path) -> None:
    """/files/*: a reopened child of a refused parent is served; a refused grandchild is not."""
    d = _dirs(tmp_path)
    (Path(d["home_project_private"]) / "hidden.txt").write_text("no")
    r = _put_gateway(ua, {"posture": "allowed_only", "folders": [
        {"path": d["home"], "mode": "deny"}, {"path": d["home_project"], "mode": "rw"}, {"path": d["home_project_private"], "mode": "deny"}]})
    assert r.status_code == 200, r.text
    r = ua.get("/api/gateway/files/list")
    assert r.status_code == 200, r.text
    listed = json.dumps(r.json()["items"])
    assert "seed.txt" in listed and "private" not in listed, listed
    r = ua.get("/api/gateway/files/list", params={"workspace_root": d["home_project"]})
    assert r.status_code == 200, r.text
    # The refused subtree inside the reopened child stays refused.
    assert ua.get("/api/gateway/files/list", params={"workspace_root": d["home_project_private"]}).status_code in (400, 403)
    assert ua.get("/api/gateway/files/list", params={"workspace_root": d["home_Desktop"]}).status_code in (400, 403)
