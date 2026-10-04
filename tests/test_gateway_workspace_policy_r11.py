"""Round 11 (DESIGN.md "R11.1 FINAL"): three levels, NO shared workspace.

GATEWAY = the eligible set (posture + rows whose mode is the CAP; built-in refusals); ACCOUNT = the
account's own default subset with the same shape (any path inside the eligible set, mode ≤ cap);
SESSION/RUN = the same shape for one conversation or one run (test_r11w1_levels.py). Two postures:
"Deny everything, allow listed workspaces" | "Allow everything, refuse listed workspaces"; modes
Read-only | Read & write | Refused.

Covered here: the gateway and account routes and their refusals (one shape: workspace_refused), the
effective set and its one line (byte-exact), enforcement at the run start, at the host and at the
runtime's own tool scope, the account's own data plane vs another account's, the server file routes,
audit, and the pre-round-9 migration chain.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

_TOKEN = "workspace-r9-admin-secret"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    monkeypatch.delenv("ABSTRACTGATEWAY_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_WORKSPACE_MOUNTS", raising=False)


@pytest.fixture()
def f(tmp_path: Path) -> dict:
    out = {}
    for name in ("pictures", "project", "archive", "secrets", "other", "notes"):
        p = tmp_path / "f" / name
        p.mkdir(parents=True)
        out[name] = str(p.resolve())
    inner = Path(out["project"]) / "private"
    inner.mkdir()
    out["project_private"] = str(inner.resolve())
    return out


def _data(tmp_path: Path) -> Path:
    return tmp_path / "runtime"


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _user(user_id: str, roles: list | None = None) -> dict:
    from abstractgateway.users import GatewayUserRegistry

    _rec, token = GatewayUserRegistry().create_user(user_id=user_id, roles=roles or ["user"])
    return {"Authorization": f"Bearer {token}"}


def _put(c: TestClient, body: dict, headers: dict | None = None):
    return c.put("/api/gateway/workspace/policy", json=body, headers=headers or {})


def _gateway(c: TestClient, f: dict, **extra) -> dict:
    body = {
        "posture": "allowed_only",
        "folders": [{"path": f["project"], "mode": "rw"}, {"path": f["archive"], "mode": "ro"}],
    }
    body.update(extra)
    r = _put(c, body)
    assert r.status_code == 200, r.text
    return r.json()["policy"]


class _P:
    def __init__(self, user_id: str, tenant_id: str = "default") -> None:
        self.user_id = user_id
        self.tenant_id = tenant_id


def _refusal(r) -> dict:
    """The ONE refusal shape (R11 API line)."""
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict) and detail["reason"] == "workspace_refused", detail
    return detail


# ---------------------------------------------------------------- gateway policy


def test_gateway_policy_shape_writes_and_refusals(f: dict, tmp_path: Path) -> None:
    with _client() as c:
        g = c.get("/api/gateway/workspace/policy").json()["policy"]
        # A fresh gateway: "Allow everything, refuse listed workspaces", read & write, nothing listed.
        assert g["posture"] == "any_except_denied" and g["default_mode"] == "rw" and g["folders"] == []
        assert g["summary"] == "Allow everything, refuse listed workspaces (rw)"
        for gone in ("shared_workspace", "allowed_folders", "never_allowed", "allow_any_folder", "launch_folder_trust", "builtin_never_allowed"):
            assert gone not in g, gone
        assert str(_data(tmp_path).resolve()) in g["builtin_refused"]

        g = _gateway(c, f)
        assert g["folders"] == [{"path": f["project"], "mode": "rw"}, {"path": f["archive"], "mode": "ro"}]
        assert g["summary"] == f"Deny everything, allow listed workspaces · {f['project']} (rw) · {f['archive']} (ro)"
        g = _put(c, {"posture": "any_except_denied", "default_mode": "ro"}).json()["policy"]
        assert g["posture"] == "any_except_denied" and g["default_mode"] == "ro" and len(g["folders"]) == 2

        def refused(body: dict, needle: str) -> None:
            detail = _refusal(_put(c, body))
            assert needle in detail["message"], detail

        refused({"shared_workspace": f["pictures"]}, "shared_workspace no longer exists: list it as a workspace")
        refused({"folders": [f["notes"]]}, "each row is {path, mode}")
        refused({"folders": [{"path": f["notes"]}]}, "mode must be")
        refused({"folders": [{"path": f["notes"], "mode": "write"}]}, "mode must be")
        refused({"folders": [{"path": "relative/x", "mode": "rw"}]}, "Use a full path")
        refused({"folders": [{"path": str(tmp_path / "missing"), "mode": "rw"}]}, "No directory at this path")
        refused({"folders": [{"path": f["notes"], "mode": "rw"}, {"path": f["notes"] + "/", "mode": "ro"}]}, "listed twice")
        refused({"folders": [{"path": f["project"], "mode": "deny"}, {"path": f["project_private"], "mode": "rw"}]}, "nothing re-opens under a refusal")
        refused({"posture": "whitelist"}, "posture must be")
        refused({"default_mode": "deny"}, "default_mode must be")
        for old in ("allowed_folders", "never_allowed", "allow_any_folder", "launch_folder_trust", "client_workspace_scope_overrides"):
            refused({old: True}, "Two dimensions only")
        detail = _refusal(_put(c, {"folders": [{"path": f["notes"], "mode": "rw"}, {"path": f["notes"], "mode": "ro"}]}))
        assert detail["path"] == f["notes"]
        assert c.get("/api/gateway/workspace/policy").json()["policy"]["default_mode"] == "ro"  # nothing landed

        alice = _user("alice")
        r = c.get("/api/gateway/workspace/policy", headers=alice)
        assert r.status_code == 200 and r.json()["policy"]["builtin_refused"] == []
        assert r.json()["policy"]["builtin_refused_hidden"] is True
        assert str(_data(tmp_path).resolve()) not in r.text
        assert _put(c, {"posture": "allowed_only"}, alice).status_code == 403


def test_runtime_config_refuses_the_old_workspace_keys_and_old_routes_are_gone(f: dict) -> None:
    with _client() as c:
        for key, value in (
            ("workspace_root", f["pictures"]),
            ("workspace_mounts", f"p={f['project']}"),
            ("workspace_allowed_paths", [f["project"]]),
            ("workspace_blocked_paths", [f["secrets"]]),
            ("client_workspace_scope_overrides", True),
            ("trust_client_launch_folder", False),
            ("workspace_default_mode", "blacklist"),
            ("user_workspace_policies", {}),
        ):
            r = c.post("/api/gateway/admin/runtime-config", json={key: value})
            assert r.status_code == 400 and "/api/gateway/workspace/policy" in r.json()["detail"], (key, r.text)
        cfg = c.get("/api/gateway/admin/runtime-config").json()
        for key in ("workspace_root", "workspace_mounts", "client_workspace_scope_overrides", "user_workspace_policies", "trust_client_launch_folder"):
            assert key not in cfg, key
        assert c.get("/api/gateway/workspace/policy/self").status_code == 410
        assert c.get("/api/gateway/admin/user-workspace-policy", params={"user_id": "admin"}).status_code == 404


# ---------------------------------------------------------------- account level + effective line


def test_an_account_picks_its_own_subset_among_the_eligible_set(f: dict) -> None:
    with _client() as c:
        _gateway(c, f, posture="any_except_denied", default_mode="rw",
                 folders=[{"path": f["archive"], "mode": "ro"}, {"path": f["secrets"], "mode": "deny"}])
        alice = _user("alice")
        _user("bob")

        # Not configured = the gateway policy as is.
        body = c.get("/api/gateway/workspace/policy/me", headers=alice).json()
        assert body["policy"] == {"account": "default:alice", "configured": False, "posture": "any_except_denied", "default_mode": "rw", "folders": []}
        assert body["can_edit"] is True
        eff = body["effective"]
        assert eff["level"] == "gateway" and eff["summary"] == eff["gateway_summary"]
        assert eff["summary"] == f"Allow everything, refuse listed workspaces (rw) · {f['archive']} (ro) · {f['secrets']} (refused)"
        assert eff["folders"] == [
            {"path": f["archive"], "mode": "ro", "cap": "ro", "source": "gateway"},
            {"path": f["secrets"], "mode": "deny", "cap": "deny", "source": "gateway"},
        ]

        # Her OWN posture within the eligible set: only Pictures (rw) and Documents-like archive (ro).
        r = c.put("/api/gateway/workspace/policy/me", json={
            "posture": "allowed_only",
            "folders": [{"path": f["pictures"], "mode": "rw"}, {"path": f["archive"], "mode": "ro"}],
        }, headers=alice)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["policy"]["configured"] is True and body["policy"]["posture"] == "allowed_only"
        assert body["effective"]["level"] == "account"
        assert body["effective"]["summary"] == f"Deny everything, allow listed workspaces · {f['pictures']} (rw) · {f['archive']} (ro)"
        assert body["effective"]["gateway_summary"] == f"Allow everything, refuse listed workspaces (rw) · {f['archive']} (ro) · {f['secrets']} (refused)"
        assert {"path": f["pictures"], "mode": "rw", "cap": "rw", "source": "account"} in body["effective"]["folders"]
        # The gateway's refused row is implied by her "Deny everything…" posture: not listed.
        assert f["secrets"] not in json.dumps(body["effective"]["folders"])

        def refused(payload: dict, needle: str, headers=alice) -> dict:
            detail = _refusal(c.put("/api/gateway/workspace/policy/me", json=payload, headers=headers))
            assert needle in detail["message"], detail
            return detail

        # Above the cap (the gateway allows archive read-only).
        d = refused({"folders": [{"path": f["archive"], "mode": "rw"}]}, "The gateway allows this workspace read-only")
        assert d["path"] == f["archive"]
        # Outside the eligible set: a refused workspace, a sub-folder of it, a protected folder.
        refused({"folders": [{"path": f["secrets"], "mode": "ro"}]}, "outside the workspaces the gateway allows")
        refused({"folders": [{"path": str(_data_from_env()), "mode": "ro"}]}, "outside the workspaces the gateway allows")
        refused({"posture": "maybe"}, "posture must be")
        refused({"configured": False, "posture": "allowed_only"}, "configured: false")
        refused({"enabled_folders": []}, "Two dimensions only")
        refused({"shared_workspace": f["pictures"]}, "list it as a workspace")
        # A refusal row never widens: allowed even on a gateway-refused path.
        assert c.put("/api/gateway/workspace/policy/me", json={"folders": [{"path": f["pictures"], "mode": "rw"}, {"path": f["secrets"], "mode": "deny"}]}, headers=alice).status_code == 200
        # Nothing stored above the gateway when its default becomes read-only: the clamp shows it.
        _put(c, {"default_mode": "ro"})
        eff = c.get("/api/gateway/workspace/effective/me", headers=alice).json()
        assert {"path": f["pictures"], "mode": "ro", "cap": "ro", "source": "account"} in eff["folders"]

        # Follow the gateway policy again.
        r = c.put("/api/gateway/workspace/policy/me", json={"configured": False}, headers=alice)
        assert r.status_code == 200 and r.json()["policy"]["configured"] is False and r.json()["effective"]["level"] == "gateway"

        # Self or admin only for humans; `alice` by name for herself and for the admin.
        assert c.get("/api/gateway/workspace/policy/bob", headers=alice).status_code == 403
        assert c.put("/api/gateway/workspace/policy/bob", json={"folders": []}, headers=alice).status_code == 403
        assert c.get("/api/gateway/workspace/effective/bob", headers=alice).status_code == 403
        assert c.get("/api/gateway/workspace/policy/alice", headers=alice).status_code == 200
        assert c.get("/api/gateway/workspace/policy/default:alice").status_code == 200
        assert c.get("/api/gateway/workspace/policy/nobody").status_code == 404
        assert c.put("/api/gateway/workspace/policy/bob", json={"folders": [{"path": f["notes"], "mode": "ro"}]}).status_code == 200
        bob = c.get("/api/gateway/workspace/effective/bob").json()
        assert bob["level"] == "account" and f"{f['notes']} (ro)" in bob["summary"]


def _data_from_env() -> Path:
    import os

    return Path(os.environ["ABSTRACTGATEWAY_DATA_DIR"]).resolve()


def test_under_deny_everything_the_eligible_set_is_the_listed_rows(f: dict) -> None:
    with _client() as c:
        _gateway(c, f)  # allowed_only: project rw, archive ro
        alice = _user("alice")
        eff = c.get("/api/gateway/workspace/effective/me", headers=alice).json()
        assert eff["summary"] == f"Deny everything, allow listed workspaces · {f['project']} (rw) · {f['archive']} (ro)"
        assert eff["default_mode"] is None
        # A sub-folder of a listed row is eligible; anything else is not.
        assert c.put("/api/gateway/workspace/policy/me", json={"posture": "allowed_only", "folders": [{"path": f["project_private"], "mode": "rw"}]}, headers=alice).status_code == 200
        detail = _refusal(c.put("/api/gateway/workspace/policy/me", json={"folders": [{"path": f["notes"], "mode": "ro"}]}, headers=alice))
        assert "outside the workspaces the gateway allows" in detail["message"] and detail["path"] == f["notes"]
        # The account's "Allow everything…" inside a "Deny everything…" gateway = every listed row at its cap.
        r = c.put("/api/gateway/workspace/policy/me", json={"posture": "any_except_denied", "default_mode": "rw", "folders": []}, headers=alice)
        assert r.status_code == 200
        assert r.json()["effective"]["summary"] == f"Allow everything, refuse listed workspaces (rw) · {f['project']} (rw) · {f['archive']} (ro)"


def test_entity_accounts_are_set_by_admins_and_their_creator(f: dict, tmp_path: Path) -> None:
    from abstractgateway.users import GatewayUserRegistry

    with _client() as c:
        _gateway(c, f)
        alice = _user("alice")
        bob = _user("bob")
        entity = _user("aster", roles=["entity"])
        assert GatewayUserRegistry().get_user("aster").principal_kind == "entity"
        home = _data(tmp_path) / "entities" / "aster"
        home.mkdir(parents=True)
        (home / "manifest.json").write_text(json.dumps({"slug": "aster", "created_by": {"tenant_id": "default", "user_id": "alice"}}))
        body = {"posture": "allowed_only", "folders": [{"path": f["project"], "mode": "ro"}]}
        # The creator may; another user may not; the entity itself may read but not change.
        r = c.put("/api/gateway/workspace/policy/aster", json=body, headers=alice)
        assert r.status_code == 200 and r.json()["can_edit"] is True, r.text
        assert f"{f['project']} (ro)" in r.json()["effective"]["summary"]
        r = c.put("/api/gateway/workspace/policy/aster", json=body, headers=bob)
        assert r.status_code == 403 and r.json()["detail"] == "Only an admin or aster's creator can change its workspaces."
        assert c.get("/api/gateway/workspace/policy/aster", headers=bob).status_code == 403
        assert c.put("/api/gateway/workspace/policy/me", json=body, headers=entity).status_code == 403
        r = c.get("/api/gateway/workspace/policy/me", headers=entity)
        assert r.status_code == 200 and r.json()["can_edit"] is False
        # Admins always.
        assert c.put("/api/gateway/workspace/policy/aster", json={"configured": False}).status_code == 200


def test_policy_changes_are_audited(f: dict, tmp_path: Path) -> None:
    with _client() as c:
        _gateway(c, f)
        alice = _user("alice")
        c.put("/api/gateway/workspace/policy/me", json={"folders": [{"path": f["archive"], "mode": "deny"}]}, headers=alice)
    lines = [json.loads(ln) for ln in (_data(tmp_path) / "audit_log.jsonl").read_text().splitlines() if ln.strip()]
    events = [e for e in lines if e.get("event") == "workspace_policy_changed" and e.get("scope") != "migration"]
    assert {"scope": "gateway", "actor": "person:admin"}.items() <= events[0].items()
    assert sorted(events[0]["changed"]) == ["folders", "posture"]
    assert events[-1]["scope"] == "account" and events[-1]["account"] == "default:alice" and events[-1]["actor"] == "person:alice"


# ---------------------------------------------------------------- enforcement


def test_run_start_follows_the_posture(f: dict) -> None:
    from abstractgateway.routes.gateway import _sanitize_run_workspace_policy

    with _client() as c:
        _gateway(c, f, folders=[{"path": f["project"], "mode": "rw"}, {"path": f["archive"], "mode": "ro"}, {"path": f["project_private"], "mode": "deny"}])
        p = _P("alice")
        _user("alice")
        out = _sanitize_run_workspace_policy({}, principal=p)
        assert out["workspace_access_mode"] == "workspace_or_allowed"
        assert out["workspace_allowed_paths"] == [f["project"], f["archive"]]
        assert out["workspace_ignored_paths"].splitlines() == [f["project_private"]]
        out = _sanitize_run_workspace_policy({"workspace_allowed_paths": [f["archive"]]}, principal=p)
        assert out["workspace_allowed_paths"] == [f["archive"]]  # narrowed

        def refused(data: dict, needle: str) -> None:
            with pytest.raises(HTTPException) as exc:
                _sanitize_run_workspace_policy(dict(data), principal=p)
            assert exc.value.status_code == 400 and exc.value.detail["reason"] == "workspace_refused", exc.value.detail
            assert needle in exc.value.detail["message"], exc.value.detail

        refused({"workspace_access_mode": "all_except_ignored"}, "never by a client")
        refused({"workspace_root": f["other"]}, "outside the workspaces the gateway allows")  # the launch folder, not listed
        refused({"workspace_root": f["project_private"]}, "outside the workspaces the gateway allows")
        refused({"workspace_allowed_paths": [f["notes"]]}, "outside the workspaces the gateway allows")
        assert _sanitize_run_workspace_policy({"workspace_root": f["project"]}, principal=p)["workspace_root"] == f["project"]

        # "Allow everything, refuse listed workspaces": any launch folder unless refused; the gateway sets the mode itself.
        _put(c, {"posture": "any_except_denied"})
        out = _sanitize_run_workspace_policy({"workspace_root": f["other"]}, principal=p)
        assert out["workspace_root"] == f["other"] and out["workspace_access_mode"] == "all_except_ignored"
        refused({"workspace_root": f["project_private"]}, "outside the workspaces the gateway allows")
        refused({"workspace_access_mode": "all_except_ignored"}, "never by a client")  # still never from a client


def _scope(vars0: dict):
    from abstractruntime.integrations.abstractcore.workspace_scoped_tools import WorkspaceScope

    return WorkspaceScope.from_input_data(vars0)


def _write(scope, path: Path) -> None:
    from abstractruntime.integrations.abstractcore.workspace_scoped_tools import rewrite_tool_arguments

    rewrite_tool_arguments(tool_name="write_file", args={"file_path": str(path), "content": "x"}, scope=scope)


def _read(scope, path: Path) -> None:
    from abstractruntime.integrations.abstractcore.workspace_scoped_tools import rewrite_tool_arguments

    rewrite_tool_arguments(tool_name="read_file", args={"file_path": str(path)}, scope=scope)


def test_granular_modes_at_the_tool_scope(f: dict, tmp_path: Path) -> None:
    """One policy with folder1 read-only and folder2 read & write — at the host, a write into folder1
    is refused with a sentence, a write into folder2 succeeds, both read."""
    from abstractgateway.run_workspace_guard import apply_workspace_policy

    with _client() as c:
        _gateway(c, f, folders=[{"path": f["archive"], "mode": "ro"}, {"path": f["project"], "mode": "rw"}, {"path": f["project_private"], "mode": "deny"}])
    session = tmp_path / "session"
    session.mkdir()
    for name in ("archive", "project", "project_private", "notes", "pictures"):
        (Path(f[name]) / "seed.txt").write_text("x")
    v = {"workspace_root": str(session)}
    apply_workspace_policy(v, root_data_dir=_data(tmp_path), tenant_id="default", user_id="admin")
    assert "workspace_shared_path" not in v
    scope = _scope(v)
    _write(scope, Path(f["project"]) / "new.txt")
    _write(scope, session / "new.txt")
    with pytest.raises(ValueError, match="read-only"):
        _write(scope, Path(f["archive"]) / "new.txt")
    _read(scope, Path(f["archive"]) / "seed.txt")
    _read(scope, Path(f["project"]) / "seed.txt")
    for refused in (Path(f["project_private"]) / "seed.txt", Path(f["notes"]) / "seed.txt", Path(f["pictures"]) / "seed.txt"):
        with pytest.raises(ValueError):
            _read(scope, refused)


def test_any_folder_with_a_read_only_default_at_the_tool_scope(f: dict, tmp_path: Path) -> None:
    from abstractgateway.run_workspace_guard import apply_workspace_policy

    with _client() as c:
        _gateway(c, f, posture="any_except_denied", default_mode="ro",
                 folders=[{"path": f["project"], "mode": "rw"}, {"path": f["secrets"], "mode": "deny"}])
    session = tmp_path / "session"
    session.mkdir()
    for name in ("notes", "secrets", "project"):
        (Path(f[name]) / "seed.txt").write_text("x")
    v = {"workspace_root": str(session), "workspace_writable_paths": ["/"]}  # a client never reopens
    apply_workspace_policy(v, root_data_dir=_data(tmp_path), tenant_id="default", user_id="admin")
    assert v["workspace_access_mode"] == "all_except_ignored"
    scope = _scope(v)
    _write(scope, session / "a.txt")
    _write(scope, Path(f["project"]) / "a.txt")
    _read(scope, Path(f["notes"]) / "seed.txt")
    with pytest.raises(ValueError, match="read-only"):
        _write(scope, Path(f["notes"]) / "a.txt")
    with pytest.raises(ValueError):
        _read(scope, Path(f["secrets"]) / "seed.txt")


def test_a_folder_in_the_own_data_plane_works_only_for_its_account(f: dict, tmp_path: Path) -> None:
    """Another conversation folder of the SAME account is reachable once listed; another account's
    plane never is, even listed."""
    from abstractgateway.run_workspace_guard import guard_run_vars

    with _client() as c:
        _user("alice")
        _user("bob")
        data = _data(tmp_path).resolve()
        alice_other = data / "users" / "default" / "alice" / "runtime" / "workspaces" / "session-older"
        alice_other.mkdir(parents=True)
        (alice_other / "notes.md").write_text("mine")
        _gateway(c, f, folders=[{"path": str(alice_other), "mode": "ro"}])
        for who, reach in (("alice", True), ("bob", False)):
            eff = c.get(f"/api/gateway/workspace/effective/{who}").json()
            assert (str(alice_other) in [x["path"] for x in eff["folders"]]) is reach, (who, eff)
            plane = data / "users" / "default" / who / "runtime"
            v: dict = {}
            guard_run_vars(v, data_dir=plane, root_data_dir=data, session_id=f"s-{who}", tenant_id="default", user_id=who)
            scope = _scope(v)
            if reach:
                _read(scope, alice_other / "notes.md")
                assert str(alice_other) in v["workspace_builtin_allow"]
                with pytest.raises(ValueError, match="read-only"):
                    _write(scope, alice_other / "x.md")
            else:
                assert str(alice_other) not in v.get("workspace_builtin_allow", [])
                with pytest.raises(ValueError):
                    _read(scope, alice_other / "notes.md")


def test_server_file_routes_follow_the_effective_set(f: dict) -> None:
    (Path(f["pictures"]) / "hello.txt").write_text("hi")
    (Path(f["project"]) / "plan.md").write_text("plan")
    with _client() as c:
        _gateway(c, f, folders=[{"path": f["pictures"], "mode": "rw"}])
        r = c.get("/api/gateway/files/list")
        assert r.status_code == 200, r.text
        listed = json.dumps(r.json()["items"])
        assert "hello.txt" in listed and "plan.md" not in listed  # base = the first read & write workspace
        assert c.get("/api/gateway/files/list", params={"workspace_access_mode": "all_except_ignored"}).status_code == 400
        assert c.get("/api/gateway/files/list", params={"workspace_root": f["project"]}).status_code == 400
        _put(c, {"folders": [{"path": f["pictures"], "mode": "rw"}, {"path": f["project"], "mode": "ro"}]})
        r = c.get("/api/gateway/files/list", params={"workspace_root": f["project"]})
        assert r.status_code == 200 and "plan.md" in json.dumps(r.json()["items"]), r.text
        assert c.get("/api/gateway/files/list", headers=_user("alice")).status_code == 403


# ---------------------------------------------------------------- the pre-round-9 migration chain (v1 then v2)


def _write_old_store(data_dir: Path, stored: dict) -> None:
    path = data_dir / "config" / "runtime_config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stored))


def _read_store(data_dir: Path) -> dict:
    return json.loads((data_dir / "config" / "runtime_config.json").read_text())


def test_pre_round9_whitelist_migrates_through_v1_then_v2(f: dict, tmp_path: Path) -> None:
    from abstractgateway.users import GatewayUserRegistry
    from abstractgateway.workspace_policy import effective_policy, gateway_policy

    data = _data(tmp_path)
    gone = str(tmp_path / "gone")
    old = {
        "workspace_root": f["pictures"],
        "workspace_mounts": [{"name": "project", "path": f["project"]}, {"name": "gone", "path": gone}],
        "workspace_blocked_paths": [f["secrets"]],
        "client_workspace_scope_overrides": True,
        "trust_client_launch_folder": True,
        "workspace_default_mode": "whitelist",
        "user_workspace_policies": {
            "alice": {"workspace_allowed_paths": [f["notes"]], "workspace_blocked_paths": [f["project_private"]], "client_workspace_scope_overrides": True},
            "default:bob": {"mode": "blacklist", "trust_client_launch_folder": True},
        },
        "executor": "codex",
    }
    _write_old_store(data, old)
    GatewayUserRegistry(data / "auth" / "users.json").create_user(user_id="carol", roles=["user"])
    g = gateway_policy(data)
    assert g["posture"] == "any_except_denied" and g["default_mode"] == "rw"
    assert g["folders"] == [
        {"path": f["pictures"], "mode": "rw"},  # the old workspace root, now a listed workspace
        {"path": f["project"], "mode": "rw"},
        {"path": f["notes"], "mode": "rw"},
        {"path": f["secrets"], "mode": "deny"},
    ]
    stored = _read_store(data)
    for key in ("workspace_root", "workspace_mounts", "workspace_blocked_paths", "client_workspace_scope_overrides",
                "trust_client_launch_folder", "workspace_default_mode", "user_workspace_policies"):
        assert key not in stored, key
    assert stored["executor"] == "codex" and "shared_workspace" not in stored["workspace_policy"]
    record = stored["_migrated"]["workspace_policy_v1"]
    assert record["old"]["user_workspace_policies"] == old["user_workspace_policies"]
    assert gone in record["dropped_missing_or_conflicting"]
    assert stored["_migrated"]["workspace_policy_v2"]["shared_workspace_row"] == f["pictures"]
    # Each account keeps its own refusals as a configured layer under the gateway's posture.
    alice = effective_policy(data, tenant_id="default", user_id="alice")
    assert alice["level"] == "account"
    assert f"{f['project_private']} (refused)" in alice["summary"] and f"{f['notes']} (rw)" in alice["summary"]
    carol = effective_policy(data, tenant_id="default", user_id="carol")
    assert carol["level"] == "account" and f"{f['notes']} (refused)" in carol["summary"]  # never gained notes
    # Idempotent.
    before = _read_store(data)
    gateway_policy(data)
    assert _read_store(data) == before


def test_pre_round9_without_a_configured_root_adds_no_guessed_row(f: dict, tmp_path: Path) -> None:
    from abstractgateway.workspace_policy import gateway_policy

    data = _data(tmp_path)
    _write_old_store(data, {"workspace_default_mode": "blacklist", "workspace_blocked_paths": [f["secrets"]]})
    g = gateway_policy(data)
    assert g["posture"] == "any_except_denied" and g["folders"] == [{"path": f["secrets"], "mode": "deny"}]


def test_no_old_keys_means_no_model_migration(tmp_path: Path) -> None:
    from abstractgateway.workspace_policy import ensure_migrated, gateway_policy

    data = _data(tmp_path)
    _write_old_store(data, {"executor": "codex"})
    assert ensure_migrated(data) is False
    assert gateway_policy(data)["posture"] == "any_except_denied"
    stored = _read_store(data)
    assert stored == {"executor": "codex"}  # a fresh store gets its defaults without a write


def test_the_most_specific_rule_and_a_deny_above_it(tmp_path: Path) -> None:
    from abstractgateway.workspace_policy import _rule

    a = tmp_path / "a"
    (a / "b" / "c").mkdir(parents=True)
    rows = [{"path": str(a / "b"), "mode": "rw"}, {"path": str(a), "mode": "ro"}]
    assert _rule(rows, a / "b" / "c") == "rw" and _rule(rows, a / "x") == "ro" and _rule(rows, tmp_path) is None
    assert _rule([{"path": str(a), "mode": "deny"}, {"path": str(a / "b"), "mode": "rw"}], a / "b" / "c") == "deny"


def test_an_account_default_read_only_binds_its_runs(f: dict, tmp_path: Path) -> None:
    from abstractgateway.run_workspace_guard import apply_workspace_policy

    with _client() as c:
        _gateway(c, f, posture="any_except_denied", default_mode="rw", folders=[])
        alice = _user("alice")
        r = c.put("/api/gateway/workspace/policy/me", json={"posture": "any_except_denied", "default_mode": "ro"}, headers=alice)
        assert r.status_code == 200, r.text
    from abstractgateway.workspace_policy import effective_folder_paths

    # The per-path answer (exports, run-start checks) and the host binding agree.
    assert effective_folder_paths(_data(tmp_path), tenant_id="default", user_id="alice").mode(Path(f["notes"]) / "x.txt") == "ro"
    assert effective_folder_paths(_data(tmp_path), tenant_id="default", user_id="admin").mode(Path(f["notes"]) / "x.txt") == "rw"
    session = tmp_path / "session"
    session.mkdir()
    for who, writable in (("alice", False), ("admin", True)):
        v = {"workspace_root": str(session)}
        apply_workspace_policy(v, root_data_dir=_data(tmp_path), tenant_id="default", user_id=who)
        scope = _scope(v)
        _write(scope, session / "own.txt")
        if writable:
            _write(scope, Path(f["notes"]) / "x.txt")
        else:
            with pytest.raises(ValueError, match="read-only"):
                _write(scope, Path(f["notes"]) / "x.txt")


def test_a_client_cannot_reopen_a_host_read_only_mount(f: dict, tmp_path: Path) -> None:
    """A discussion's automation workspace is mounted read-only by the host; a client-sent
    `workspace_writable_paths` naming it is dropped, so it stays read-only."""
    from abstractgateway.run_workspace_guard import apply_workspace_policy

    session = tmp_path / "session"
    session.mkdir()
    with _client() as c:
        _gateway(c, f, folders=[{"path": f["notes"], "mode": "rw"}])
        v = {
            "workspace_root": str(session),
            "_runtime": {"workspace_read_only_paths": [f["notes"]], "workspace_writable_paths": [f["notes"]]},
            "workspace_writable_paths": [f["notes"]],
        }
        apply_workspace_policy(v, root_data_dir=_data(tmp_path), tenant_id="default", user_id="admin")
        assert "workspace_writable_paths" not in v and "workspace_writable_paths" not in v["_runtime"]
        with pytest.raises(ValueError, match="read-only"):
            _write(_scope(v), Path(f["notes"]) / "x.txt")
        _put(c, {"folders": [{"path": f["notes"], "mode": "rw"}, {"path": f["archive"], "mode": "ro"}]})
        v = {"workspace_root": str(session), "_runtime": {"workspace_read_only_paths": [f["notes"]]}}
        apply_workspace_policy(v, root_data_dir=_data(tmp_path), tenant_id="default", user_id="admin")
        assert f["notes"] in v["workspace_writable_paths"]
        with pytest.raises(ValueError, match="read-only"):
            _write(_scope(v), Path(f["notes"]) / "x.txt")
