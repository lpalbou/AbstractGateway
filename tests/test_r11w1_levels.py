"""Round 11 (DESIGN.md "R11.1 FINAL"): the SESSION and RUN levels, and run start on every door.

- SESSION: GET/PUT /api/gateway/sessions/{id}/workspaces — stored by the gateway in the owner's plane
  (works before the session's first run, survives a restart), owner or admin only, same validation
  as the account level (clamped to the GATEWAY's eligible set, not to the account default).
- EFFECTIVE: GET /workspace/effective/{account}?session= and the DRY RUN POST /workspace/effective.
- RUN START: one-off `workspace` > session > account > gateway on POST /runs/start, host.start_run
  and the automation guard; a client can never widen (a path outside the eligible set is refused at
  the HTTP door and dropped at the in-process doors); the run records its level.
- AUTOMATIONS store `target.input_data.workspace`; a round-9 `workspace_allowed_paths` list becomes it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from test_gateway_session_workspace_reuse import _client as _bundle_client
from test_gateway_session_workspace_reuse import _start

pytestmark = pytest.mark.basic

_TOKEN = "workspace-r11-admin-secret"


def _dirs(tmp_path: Path) -> dict:
    out = {}
    for name in ("pictures", "documents", "downloads", "project", "archive", "secrets", "outside"):
        p = tmp_path / "disk" / name
        p.mkdir(parents=True, exist_ok=True)
        out[name] = str(p.resolve())
    return out


def _gateway_any(c: TestClient, d: dict, headers: dict | None = None) -> None:
    """The operator's default posture with a cap and a refusal."""
    r = c.put(
        "/api/gateway/workspace/policy",
        json={
            "posture": "any_except_denied",
            "default_mode": "rw",
            "folders": [{"path": d["archive"], "mode": "ro"}, {"path": d["secrets"], "mode": "deny"}],
        },
        headers=headers or {},
    )
    assert r.status_code == 200, r.text


def _refusal(r) -> dict:
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["reason"] == "workspace_refused", detail
    return detail


# ================================================================ user-auth harness (owner isolation)


@pytest.fixture()
def ua(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
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


def test_session_level_round_trip_isolation_and_restart(ua: TestClient, tmp_path: Path) -> None:
    d = _dirs(tmp_path)
    _gateway_any(ua, d)
    alice, bob = _user("alice"), _user("bob")
    url = "/api/gateway/sessions/chat-1/workspaces"

    # Nothing stored, no run yet: 200, "Use my default".
    body = ua.get(url, headers=alice).json()
    assert body["policy"] == {
        "session_id": "chat-1", "account": "default:alice", "configured": False,
        "posture": "any_except_denied", "default_mode": "rw", "folders": [],
    }
    assert body["effective"]["level"] == "gateway" and body["account_default"]["level"] == "gateway"

    # Her account default: Pictures only. The conversation: Documents + Downloads (not in her default).
    assert ua.put("/api/gateway/workspace/policy/me", json={"posture": "allowed_only", "folders": [{"path": d["pictures"], "mode": "rw"}]}, headers=alice).status_code == 200
    r = ua.put(url, json={"posture": "allowed_only", "folders": [{"path": d["documents"], "mode": "rw"}, {"path": d["downloads"], "mode": "ro"}]}, headers=alice)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["policy"]["configured"] is True
    assert body["effective"]["level"] == "session" and body["effective"]["session_id"] == "chat-1"
    assert body["effective"]["summary"] == f"Deny everything, allow listed workspaces · {d['documents']} (rw) · {d['downloads']} (ro)"
    assert body["effective"]["default_mode"] in ("ro", "rw") and body["account_default"]["default_mode"] in ("ro", "rw")
    assert body["account_default"]["summary"] == f"Deny everything, allow listed workspaces · {d['pictures']} (rw)"
    eff = ua.get("/api/gateway/workspace/effective/me", params={"session": "chat-1"}, headers=alice).json()
    assert eff["summary"] == body["effective"]["summary"] and eff["gateway_summary"] == body["gateway"]["summary"]
    assert {"path": d["documents"], "mode": "rw", "cap": "rw", "source": "session"} in eff["folders"]
    # Without ?session= the account default applies.
    assert ua.get("/api/gateway/workspace/effective/me", headers=alice).json()["level"] == "account"

    # Refusals: above the cap, outside the eligible set; nothing lands.
    d1 = _refusal(ua.put(url, json={"folders": [{"path": d["archive"], "mode": "rw"}]}, headers=alice))
    assert d1["message"] == f"The gateway allows this workspace read-only: {d['archive']}." and d1["path"] == d["archive"]
    d2 = _refusal(ua.put(url, json={"folders": [{"path": d["secrets"], "mode": "ro"}]}, headers=alice))
    assert "outside the workspaces the gateway allows" in d2["message"]
    assert ua.get(url, headers=alice).json()["effective"]["summary"] == body["effective"]["summary"]

    # Owner only: bob's plane does not hold alice's conversation; an admin addresses it with ?account=.
    assert ua.get(url, headers=bob).json()["policy"]["configured"] is False
    assert ua.get(url, params={"account": "alice"}, headers=bob).status_code == 403
    admin_view = ua.get(url, params={"account": "alice"}).json()
    assert admin_view["policy"]["configured"] is True and admin_view["policy"]["account"] == "default:alice"

    # Stored by the gateway in alice's plane: survives a restart (a new app/client).
    stored = json.loads((tmp_path / "runtime" / "users" / "default" / "alice" / "runtime" / "session_workspaces.json").read_text())
    assert stored["sessions"]["chat-1"]["folders"][0]["path"] == d["documents"]
    from abstractgateway.app import app

    with TestClient(app, headers=alice) as again:
        assert again.get(url).json()["policy"]["configured"] is True

    # "Use my default": the display base is the ACCOUNT default's posture.
    r = ua.put(url, json={"configured": False}, headers=alice)
    assert r.status_code == 200 and r.json()["policy"]["configured"] is False and r.json()["effective"]["level"] == "account"
    assert r.json()["policy"]["posture"] == "allowed_only" and r.json()["policy"]["folders"] == []

    lines = [json.loads(ln) for ln in (tmp_path / "runtime" / "audit_log.jsonl").read_text().splitlines() if ln.strip()]
    assert any(e.get("scope") == "session" and e.get("session_id") == "chat-1" for e in lines)


def test_effective_dry_run(ua: TestClient, tmp_path: Path) -> None:
    d = _dirs(tmp_path)
    _gateway_any(ua, d)
    alice = _user("alice")
    payload = {"posture": "allowed_only", "default_mode": "rw", "folders": [{"path": d["pictures"], "mode": "rw"}]}
    r = ua.post("/api/gateway/workspace/effective/me", json={"workspace": payload}, headers=alice)
    assert r.status_code == 200, r.text
    assert r.json()["level"] == "run" and r.json()["summary"] == f"Deny everything, allow listed workspaces · {d['pictures']} (rw)"
    assert r.json()["folders"] == [{"path": d["pictures"], "mode": "rw", "cap": "rw", "source": "run"}]
    # null = what a run would get (here: the gateway level); nothing was stored.
    r = ua.post("/api/gateway/workspace/effective/me", json={"workspace": None}, headers=alice)
    assert r.json()["level"] == "gateway"
    assert ua.get("/api/gateway/workspace/policy/me", headers=alice).json()["policy"]["configured"] is False
    detail = _refusal(ua.post("/api/gateway/workspace/effective/me", json={"workspace": {**payload, "folders": [{"path": d["archive"], "mode": "rw"}]}}, headers=alice))
    assert detail["path"] == d["archive"]
    _refusal(ua.post("/api/gateway/workspace/effective/me", json={"workspace": "everything"}, headers=alice))


# ================================================================ run start on every door (bundle harness)


def _vars(run_id: str) -> dict:
    from abstractgateway.service import get_gateway_service

    run = get_gateway_service().host.run_store.load(run_id)
    assert run is not None
    return dict(run.vars or {})


def _host_start(session_id: str | None, input_data: dict) -> dict:
    from abstractgateway.service import get_gateway_service

    return _vars(get_gateway_service().host.start_run(flow_id="root", bundle_id="bundle-ws", input_data=input_data, session_id=session_id))


def _automation_guard(tmp_path: Path, session_id: str | None, input_data: dict) -> dict:
    from abstractgateway.run_workspace_guard import guard_run_vars

    data = dict(input_data)
    guard_run_vars(data, data_dir=tmp_path / "runtime", root_data_dir=tmp_path / "runtime", session_id=session_id, tenant_id="", user_id="")
    return data


def _scope(run_vars: dict):
    from abstractruntime.integrations.abstractcore.workspace_scoped_tools import WorkspaceScope

    return WorkspaceScope.from_input_data(run_vars)


def _can(run_vars: dict, tool: str, path: Path) -> bool:
    from abstractruntime.integrations.abstractcore.workspace_scoped_tools import rewrite_tool_arguments

    args = {"file_path": str(path)} if tool == "read_file" else {"file_path": str(path), "content": "x"}
    try:
        rewrite_tool_arguments(tool_name=tool, args=args, scope=_scope(run_vars))
        return True
    except ValueError:
        return False


def _starts(client, headers, tmp_path):
    """door name -> start(session_id, input_data) -> stored/frozen run vars."""
    return {
        "http": lambda sid, data: _vars(_start(client, headers, session_id=sid, input_data=data)),
        "host": lambda sid, data: _host_start(sid, dict(data)),
        "automation": lambda sid, data: _automation_guard(tmp_path, sid, data),
    }


@pytest.mark.parametrize("door", ["http", "host", "automation"])
def test_every_door_resolves_one_off_then_session_then_account_then_gateway(door: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _dirs(tmp_path)
    for name in ("pictures", "documents", "downloads", "outside"):
        (Path(d[name]) / "seed.txt").write_text("x")
    client, headers = _bundle_client(tmp_path, monkeypatch)
    with client:
        _gateway_any(client, d, headers)
        start = _starts(client, headers, tmp_path)[door]

        # gateway level: anything but the refusal, archive read-only.
        v = start("chat-g", {})
        assert v["_gateway_workspace"]["level"] == "gateway" and v["workspace_access_mode"] == "all_except_ignored"
        assert _can(v, "write_file", Path(d["outside"]) / "a.txt") and not _can(v, "read_file", Path(d["secrets"]) / "seed.txt")

        # account level: only Pictures.
        r = client.put("/api/gateway/workspace/policy/me", json={"posture": "allowed_only", "folders": [{"path": d["pictures"], "mode": "rw"}]}, headers=headers)
        assert r.status_code == 200, r.text
        v = start("chat-a", {})
        assert v["_gateway_workspace"]["level"] == "account" and v["workspace_allowed_paths"] == [d["pictures"]]
        assert _can(v, "write_file", Path(d["pictures"]) / "a.txt") and not _can(v, "read_file", Path(d["outside"]) / "seed.txt")

        # session level: Documents rw + Downloads ro (not in the account default).
        r = client.put("/api/gateway/sessions/chat-s/workspaces", json={"posture": "allowed_only", "folders": [{"path": d["documents"], "mode": "rw"}, {"path": d["downloads"], "mode": "ro"}]}, headers=headers)
        assert r.status_code == 200, r.text
        v = start("chat-s", {})
        assert v["_gateway_workspace"]["level"] == "session"
        assert v["_gateway_workspace"]["summary"] == f"Deny everything, allow listed workspaces · {d['documents']} (rw) · {d['downloads']} (ro)"
        assert _can(v, "write_file", Path(d["documents"]) / "a.txt") and _can(v, "read_file", Path(d["downloads"]) / "seed.txt")
        assert not _can(v, "write_file", Path(d["downloads"]) / "a.txt") and not _can(v, "read_file", Path(d["pictures"]) / "seed.txt")

        # one-off: wins over the session for this run.
        one_off = {"posture": "allowed_only", "default_mode": "rw", "folders": [{"path": d["outside"], "mode": "ro"}]}
        v = start("chat-s", {"workspace": one_off})
        assert v["_gateway_workspace"]["level"] == "run" and "workspace" not in v
        assert _can(v, "read_file", Path(d["outside"]) / "seed.txt") and not _can(v, "write_file", Path(d["outside"]) / "a.txt")
        assert not _can(v, "read_file", Path(d["documents"]) / "seed.txt")


@pytest.mark.parametrize("door", ["http", "host", "automation"])
def test_a_client_can_never_widen(door: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MUTANT GUARD: a client sending a path outside the eligible set (or above its cap) is refused
    at the HTTP door and dropped/lowered at the in-process doors — never reached."""
    d = _dirs(tmp_path)
    for name in ("secrets", "archive", "pictures"):
        (Path(d[name]) / "seed.txt").write_text("x")
    client, headers = _bundle_client(tmp_path, monkeypatch)
    with client:
        _gateway_any(client, d, headers)
        start = _starts(client, headers, tmp_path)[door]
        widening = {"posture": "allowed_only", "default_mode": "rw", "folders": [{"path": d["secrets"], "mode": "rw"}, {"path": d["archive"], "mode": "rw"}, {"path": d["pictures"], "mode": "rw"}]}
        legacy = {"workspace_allowed_paths": [d["secrets"]]}
        if door == "http":
            for data in ({"workspace": widening}, legacy):
                res = client.post("/api/gateway/runs/start", headers=headers, json={"bundle_id": "bundle-ws", "flow_id": "root", "input_data": data, "session_id": "chat-w"})
                detail = _refusal(res)
                assert detail["path"] in (d["secrets"], d["archive"]), detail
            res = client.post("/api/gateway/runs/start", headers=headers, json={"bundle_id": "bundle-ws", "flow_id": "root", "workspace": widening})
            assert _refusal(res)["path"] in (d["secrets"], d["archive"])
            return
        v = start("chat-w", {"workspace": widening})
        assert not _can(v, "read_file", Path(d["secrets"]) / "seed.txt")
        assert _can(v, "read_file", Path(d["archive"]) / "seed.txt") and not _can(v, "write_file", Path(d["archive"]) / "a.txt")
        assert _can(v, "write_file", Path(d["pictures"]) / "a.txt")
        v = start("chat-w2", dict(legacy))
        assert not _can(v, "read_file", Path(d["secrets"]) / "seed.txt") and d["secrets"] not in v.get("workspace_allowed_paths", [])


def test_runs_start_body_workspace_is_saved_on_a_new_session_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _dirs(tmp_path)
    client, headers = _bundle_client(tmp_path, monkeypatch)
    with client:
        _gateway_any(client, d, headers)
        first = {"posture": "allowed_only", "default_mode": "rw", "folders": [{"path": d["pictures"], "mode": "rw"}]}
        res = client.post("/api/gateway/runs/start", headers=headers, json={"bundle_id": "bundle-ws", "flow_id": "root", "session_id": "flow-1", "workspace": first})
        assert res.status_code == 200, res.text
        assert _vars(res.json()["run_id"])["_gateway_workspace"]["level"] == "run"
        stored = client.get("/api/gateway/sessions/flow-1/workspaces", headers=headers).json()
        assert stored["policy"]["configured"] is True and stored["policy"]["folders"] == [{"path": d["pictures"], "mode": "rw"}]
        # A later run with no body follows the saved session choice; another body does not overwrite it.
        assert _vars(_start(client, headers, session_id="flow-1"))["_gateway_workspace"]["level"] == "session"
        second = {"posture": "allowed_only", "default_mode": "rw", "folders": [{"path": d["documents"], "mode": "ro"}]}
        res = client.post("/api/gateway/runs/start", headers=headers, json={"bundle_id": "bundle-ws", "flow_id": "root", "session_id": "flow-1", "workspace": second})
        assert res.status_code == 200
        assert client.get("/api/gateway/sessions/flow-1/workspaces", headers=headers).json()["policy"]["folders"] == [{"path": d["pictures"], "mode": "rw"}]


def test_automations_store_input_data_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.routes.automations import _guarded_input_data
    from abstractgateway.service import get_gateway_service

    d = _dirs(tmp_path)
    for name in ("pictures", "archive", "outside"):
        (Path(d[name]) / "seed.txt").write_text("x")
    client, headers = _bundle_client(tmp_path, monkeypatch)
    with client:
        _gateway_any(client, d, headers)
        svc = get_gateway_service()
        chosen = {"posture": "allowed_only", "default_mode": "rw", "folders": [{"path": d["pictures"], "mode": "rw"}]}
        data = _guarded_input_data(svc, None, {"workspace": chosen, "prompt": "hi"}, automation_id="auto-1")
        assert data["workspace"] == chosen  # kept in the definition for the clients
        assert data["_gateway_workspace"]["level"] == "run"
        assert _can(data, "write_file", Path(d["pictures"]) / "a.txt") and not _can(data, "read_file", Path(d["outside"]) / "seed.txt")
        # A round-9 definition's list becomes the object, each workspace at its gateway cap.
        data = _guarded_input_data(svc, None, {"workspace_allowed_paths": [d["archive"], d["pictures"]], "prompt": "hi"}, automation_id="auto-2")
        assert data["workspace"] == {
            "posture": "allowed_only", "default_mode": "rw",
            "folders": [{"path": d["archive"], "mode": "ro"}, {"path": d["pictures"], "mode": "rw"}],
        }
        assert not _can(data, "write_file", Path(d["archive"]) / "a.txt") and _can(data, "read_file", Path(d["archive"]) / "seed.txt")


@pytest.mark.parametrize("door", ["host", "automation"])
def test_an_in_process_clamp_is_recorded_with_its_sentence(door: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ADVERSARY F1: a stored/forwarded one-off narrowed by the gateway since is never clamped silently:
    each dropped or lowered row is on the run (`_gateway_workspace.clamped`) with the sentence the HTTP
    door would have refused it with, and GET /runs/{id}/workspace shows it."""
    d = _dirs(tmp_path)
    client, headers = _bundle_client(tmp_path, monkeypatch)
    with client:
        _gateway_any(client, d, headers)  # archive ro (cap), secrets refused
        stored = {"posture": "allowed_only", "default_mode": "rw", "folders": [
            {"path": d["secrets"], "mode": "rw"},   # now outside the eligible set -> dropped
            {"path": d["archive"], "mode": "rw"},   # above its cap -> lowered to ro
            {"path": d["pictures"], "mode": "rw"},  # kept
        ]}
        v = _starts(client, headers, tmp_path)[door]("chat-c", {"workspace": stored})
        clamped = v["_gateway_workspace"]["clamped"]
        assert clamped == [
            {"path": d["secrets"], "asked": "rw", "got": None,
             "sentence": f"{d['secrets']} is outside the workspaces the gateway allows ({client.get('/api/gateway/workspace/policy', headers=headers).json()['policy']['summary']})."},
            {"path": d["archive"], "asked": "rw", "got": "ro", "sentence": f"The gateway allows this workspace read-only: {d['archive']}."},
        ]
        # The same sentences the HTTP door refuses with.
        r = client.post("/api/gateway/workspace/effective/me", headers=headers, json={"workspace": {**stored, "folders": stored["folders"][1:2]}})
        assert r.json()["detail"]["message"] == clamped[1]["sentence"]
        if door == "host":
            from abstractgateway.service import get_gateway_service

            run_id = next(r.run_id for r in get_gateway_service().host.run_store.list_runs(limit=50) if (r.vars or {}).get("_gateway_workspace", {}).get("clamped"))
            shown = client.get(f"/api/gateway/runs/{run_id}/workspace", headers=headers).json()
            assert shown["workspace_level"] == "run" and shown["workspace_clamped"] == clamped
        # A one-off within the set records nothing.
        v = _starts(client, headers, tmp_path)[door]("chat-c2", {"workspace": {**stored, "folders": stored["folders"][2:]}})
        assert "clamped" not in v["_gateway_workspace"]
