"""Round 9 (DESIGN.md "Round 9 — FINAL workspace wording"): exactly two dimensions.

1. WHAT — the posture: "Deny everything, allow listed workspaces" (everything denied, the listed folders allowed) or
   "Allow everything, refuse listed workspaces" (everything allowed at ONE default mode, the listed folders denied or
   with their own mode). Plus the shared workspace, always in, always read & write.
2. HOW — per folder, read-only or read & write, granular. Accounts narrow only.

Covered: the gateway and account routes and their refusals, the effective set and its one line
(byte-exact), enforcement at the run start, at the host and at the runtime's own tool scope, the
account's own data plane vs another account's, the server file routes, audit, and the one-time
deterministic migration from the old model.
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
    for name in ("shared", "project", "archive", "secrets", "other", "notes"):
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
        "shared_workspace": f["shared"],
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


# ---------------------------------------------------------------- gateway policy


def test_gateway_policy_shape_writes_and_refusals(f: dict, tmp_path: Path) -> None:
    with _client() as c:
        g = c.get("/api/gateway/workspace/policy").json()["policy"]
        assert g["posture"] == "allowed_only" and g["default_mode"] == "rw" and g["folders"] == []
        for gone in ("allowed_folders", "never_allowed", "allow_any_folder", "launch_folder_trust"):
            assert gone not in g, gone

        g = _gateway(c, f)
        assert g["shared_workspace"] == f["shared"]
        assert g["folders"] == [{"path": f["project"], "mode": "rw"}, {"path": f["archive"], "mode": "ro"}]
        g = _put(c, {"posture": "any_except_denied", "default_mode": "ro"}).json()["policy"]
        assert g["posture"] == "any_except_denied" and g["default_mode"] == "ro" and len(g["folders"]) == 2

        def refused(body: dict, needle: str) -> None:
            r = _put(c, body)
            assert r.status_code == 400, (body, r.text)
            assert needle in r.json()["detail"], r.json()["detail"]

        refused({"shared_workspace": ""}, "shared workspace is required")
        refused({"shared_workspace": str(tmp_path / "missing")}, "No folder at this path")
        refused({"shared_workspace": str(_data(tmp_path).resolve())}, "never a workspace")
        refused({"folders": [f["notes"]]}, "each row is {path, mode}")
        refused({"folders": [{"path": f["notes"]}]}, "mode must be")
        refused({"folders": [{"path": f["notes"], "mode": "write"}]}, "mode must be")
        refused({"folders": [{"path": "relative/x", "mode": "rw"}]}, "Use a full path")
        refused({"folders": [{"path": f["notes"], "mode": "rw"}, {"path": f["notes"] + "/", "mode": "ro"}]}, "listed twice")
        refused({"folders": [{"path": f["project"], "mode": "deny"}, {"path": f["project_private"], "mode": "rw"}]}, "nothing re-opens under a deny")
        refused({"folders": [{"path": str(Path(f["shared"]).parent), "mode": "deny"}]}, "contains the shared workspace")
        refused({"folders": [{"path": f["shared"], "mode": "ro"}]}, "inside the shared workspace")
        refused({"posture": "whitelist"}, "posture must be")
        refused({"default_mode": "deny"}, "default_mode must be")
        for old in ("allowed_folders", "never_allowed", "allow_any_folder", "launch_folder_trust", "client_workspace_scope_overrides"):
            refused({old: True}, "Two dimensions only")
        assert c.get("/api/gateway/workspace/policy").json()["policy"]["default_mode"] == "ro"  # nothing landed

        alice = _user("alice")
        r = c.get("/api/gateway/workspace/policy", headers=alice)
        assert r.status_code == 200 and r.json()["policy"]["builtin_never_allowed"] == []
        assert r.json()["policy"]["builtin_never_allowed_hidden"] is True
        assert str(_data(tmp_path).resolve()) not in r.text
        assert _put(c, {"posture": "allowed_only"}, alice).status_code == 403


def test_runtime_config_refuses_the_old_workspace_keys_and_old_routes_are_gone(f: dict) -> None:
    with _client() as c:
        for key, value in (
            ("workspace_root", f["shared"]),
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


# ---------------------------------------------------------------- account policy + effective line


def test_accounts_narrow_only_and_the_line_says_it(f: dict) -> None:
    with _client() as c:
        _gateway(c, f)
        alice = _user("alice")
        _user("bob")

        eff = c.get("/api/gateway/workspace/effective/me", headers=alice).json()
        assert eff["account"] == "default:alice" and eff["posture"] == "allowed_only" and eff["default_mode"] is None
        assert eff["folders"] == [
            {"path": f["shared"], "mode": "rw", "source": "shared"},
            {"path": f["project"], "mode": "rw", "source": "gateway"},
            {"path": f["archive"], "mode": "ro", "source": "gateway"},
        ]
        assert eff["summary"] == f"Deny everything, allow listed workspaces · Shared workspace (rw) · {f['project']} (rw) · {f['archive']} (ro)"

        r = c.put("/api/gateway/workspace/policy/me", json={"folders": [{"path": f["project"], "mode": "ro"}, {"path": f["archive"], "mode": "deny"}]}, headers=alice)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["policy"] == {"account": "default:alice", "default_mode": None, "folders": [{"path": f["project"], "mode": "ro"}, {"path": f["archive"], "mode": "deny"}]}
        assert body["effective"]["summary"] == f"Deny everything, allow listed workspaces · Shared workspace (rw) · {f['project']} (ro) · {f['archive']} (refused)"

        def refused(body: dict, needle: str, headers=alice) -> None:
            r = c.put("/api/gateway/workspace/policy/me", json=body, headers=headers)
            assert r.status_code == 400 and needle in r.json()["detail"], (body, r.text)

        refused({"folders": [{"path": f["archive"], "mode": "rw"}]}, "never raises it")
        refused({"default_mode": "rw"}, "cannot raise the default")
        refused({"posture": "any_except_denied"}, "unknown")
        refused({"enabled_folders": []}, "Two dimensions only")

        # Self or admin only; `alice` by name for herself and for the admin.
        assert c.get("/api/gateway/workspace/policy/bob", headers=alice).status_code == 403
        assert c.put("/api/gateway/workspace/policy/bob", json={"folders": []}, headers=alice).status_code == 403
        assert c.get("/api/gateway/workspace/effective/bob", headers=alice).status_code == 403
        assert c.get("/api/gateway/workspace/policy/alice", headers=alice).status_code == 200
        assert c.get("/api/gateway/workspace/policy/default:alice").status_code == 200
        assert c.get("/api/gateway/workspace/policy/nobody").status_code == 404
        assert c.put("/api/gateway/workspace/policy/bob", json={"folders": [{"path": f["project"], "mode": "deny"}]}).status_code == 200
        bob = c.get("/api/gateway/workspace/effective/bob").json()
        assert bob["summary"] == f"Deny everything, allow listed workspaces · Shared workspace (rw) · {f['project']} (refused) · {f['archive']} (ro)"


def test_any_folder_except_denied_with_its_default_mode(f: dict) -> None:
    with _client() as c:
        _gateway(c, f, posture="any_except_denied", default_mode="rw",
                 folders=[{"path": f["secrets"], "mode": "deny"}, {"path": f["archive"], "mode": "ro"}])
        alice = _user("alice")
        eff = c.get("/api/gateway/workspace/effective/me", headers=alice).json()
        assert eff["default_mode"] == "rw"
        assert eff["summary"] == f"Allow everything, refuse listed workspaces (rw) · Shared workspace (rw) · {f['secrets']} (refused) · {f['archive']} (ro)"
        # The account lowers the default (and only lowers it); the admin's own stays rw.
        r = c.put("/api/gateway/workspace/policy/me", json={"default_mode": "ro"}, headers=alice)
        assert r.status_code == 200 and r.json()["effective"]["summary"].startswith("Allow everything, refuse listed workspaces (ro) · Shared workspace (rw)")
        assert c.get("/api/gateway/workspace/effective/me").json()["default_mode"] == "rw"
        _put(c, {"default_mode": "ro"})
        assert c.get("/api/gateway/workspace/effective/me").json()["default_mode"] == "ro"


def test_an_entity_cannot_change_its_own_folders(f: dict) -> None:
    from abstractgateway.users import GatewayUserRegistry

    with _client() as c:
        _gateway(c, f)
        entity = _user("castor", roles=["entity"])
        assert GatewayUserRegistry().get_user("castor").principal_kind == "entity"
        assert c.put("/api/gateway/workspace/policy/me", json={"folders": [{"path": f["project"], "mode": "ro"}]}, headers=entity).status_code == 403
        r = c.put("/api/gateway/workspace/policy/castor", json={"folders": [{"path": f["project"], "mode": "ro"}]})
        assert r.status_code == 200 and f"{f['project']} (ro)" in r.json()["effective"]["summary"]


def test_policy_changes_are_audited(f: dict, tmp_path: Path) -> None:
    with _client() as c:
        _gateway(c, f)
        alice = _user("alice")
        c.put("/api/gateway/workspace/policy/me", json={"folders": [{"path": f["archive"], "mode": "deny"}]}, headers=alice)
    lines = [json.loads(ln) for ln in (_data(tmp_path) / "audit_log.jsonl").read_text().splitlines() if ln.strip()]
    events = [e for e in lines if e.get("event") == "workspace_policy_changed"]
    assert {"scope": "gateway", "actor": "person:admin"}.items() <= events[0].items()
    assert sorted(events[0]["changed"]) == ["folders", "shared_workspace"]
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
        assert out["workspace_allowed_paths"] == [f["shared"], f["project"], f["archive"]]
        assert out["workspace_ignored_paths"].splitlines() == [f["project_private"]]
        out = _sanitize_run_workspace_policy({"workspace_allowed_paths": [f["archive"]]}, principal=p)
        assert out["workspace_allowed_paths"] == [f["shared"], f["archive"]]  # narrowed, shared kept

        def refused(data: dict, needle: str) -> None:
            with pytest.raises(HTTPException) as exc:
                _sanitize_run_workspace_policy(dict(data), principal=p)
            assert exc.value.status_code == 400 and needle in str(exc.value.detail), exc.value.detail

        refused({"workspace_access_mode": "all_except_ignored"}, "no longer exists")
        refused({"workspace_root": f["other"]}, "Deny everything, allow listed workspaces")  # the launch folder, not listed
        refused({"workspace_root": f["project_private"]}, "a refused workspace")
        refused({"workspace_allowed_paths": [f["notes"]]}, "Deny everything, allow listed workspaces")
        assert _sanitize_run_workspace_policy({"workspace_root": f["project"]}, principal=p)["workspace_root"] == f["project"]

        # "Allow everything, refuse listed workspaces": any launch folder unless denied; the gateway sets the mode itself.
        _put(c, {"posture": "any_except_denied"})
        out = _sanitize_run_workspace_policy({"workspace_root": f["other"]}, principal=p)
        assert out["workspace_root"] == f["other"] and out["workspace_access_mode"] == "all_except_ignored"
        refused({"workspace_root": f["project_private"]}, "a refused workspace")
        refused({"workspace_access_mode": "all_except_ignored"}, "no longer exists")  # still never from a client


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
    """ADVERSARY V14: one policy with folder1 read-only and folder2 read & write — at the host, a
    write into folder1 is refused with a sentence, a write into folder2 succeeds, both read."""
    from abstractgateway.run_workspace_guard import apply_workspace_policy

    with _client() as c:
        _gateway(c, f, folders=[{"path": f["archive"], "mode": "ro"}, {"path": f["project"], "mode": "rw"}, {"path": f["project_private"], "mode": "deny"}])
    session = tmp_path / "session"
    session.mkdir()
    for name in ("archive", "project", "project_private", "notes", "shared"):
        (Path(f[name]) / "seed.txt").write_text("x")
    v = {"workspace_root": str(session)}
    apply_workspace_policy(v, root_data_dir=_data(tmp_path), tenant_id="default", user_id="admin")
    scope = _scope(v)
    _write(scope, Path(f["project"]) / "new.txt")
    _write(scope, Path(f["shared"]) / "new.txt")
    _write(scope, session / "new.txt")
    with pytest.raises(ValueError, match="read-only"):
        _write(scope, Path(f["archive"]) / "new.txt")
    _read(scope, Path(f["archive"]) / "seed.txt")
    _read(scope, Path(f["project"]) / "seed.txt")
    for refused in (Path(f["project_private"]) / "seed.txt", Path(f["notes"]) / "seed.txt"):
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
    _write(scope, Path(f["shared"]) / "a.txt")
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
    (Path(f["shared"]) / "hello.txt").write_text("hi")
    (Path(f["project"]) / "plan.md").write_text("plan")
    with _client() as c:
        _gateway(c, f, folders=[])
        r = c.get("/api/gateway/files/list")
        assert r.status_code == 200, r.text
        listed = json.dumps(r.json()["items"])
        assert "hello.txt" in listed and "plan.md" not in listed
        assert c.get("/api/gateway/files/list", params={"workspace_access_mode": "all_except_ignored"}).status_code == 400
        assert c.get("/api/gateway/files/list", params={"workspace_root": f["project"]}).status_code == 400
        _put(c, {"folders": [{"path": f["project"], "mode": "ro"}]})
        r = c.get("/api/gateway/files/list", params={"workspace_root": f["project"]})
        assert r.status_code == 200 and "plan.md" in json.dumps(r.json()["items"]), r.text
        assert c.get("/api/gateway/files/list", headers=_user("alice")).status_code == 403


# ---------------------------------------------------------------- migration


def _write_old_store(data_dir: Path, stored: dict) -> None:
    path = data_dir / "config" / "runtime_config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stored))


def _read_store(data_dir: Path) -> dict:
    return json.loads((data_dir / "config" / "runtime_config.json").read_text())


def test_migration_whitelist_never_widens_anyone(f: dict, tmp_path: Path) -> None:
    from abstractgateway.users import GatewayUserRegistry
    from abstractgateway.workspace_policy import effective_policy, gateway_policy

    data = _data(tmp_path)
    gone = str(tmp_path / "gone")
    old = {
        "workspace_root": f["shared"],
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
    assert g["shared_workspace"] == f["shared"] and g["posture"] == "allowed_only" and g["default_mode"] == "rw"
    assert g["folders"] == [
        {"path": f["project"], "mode": "rw"},
        {"path": f["notes"], "mode": "rw"},
        {"path": f["secrets"], "mode": "deny"},
    ]
    stored = _read_store(data)
    for key in ("workspace_root", "workspace_mounts", "workspace_blocked_paths", "client_workspace_scope_overrides",
                "trust_client_launch_folder", "workspace_default_mode", "user_workspace_policies"):
        assert key not in stored, key
    assert stored["executor"] == "codex"
    record = stored["_migrated"]["workspace_policy_v1"]
    assert record["old"]["user_workspace_policies"] == old["user_workspace_policies"]
    assert gone in record["dropped_missing_or_conflicting"]
    assert record["narrowed_accounts"] == ["default:bob"] and record["launch_folder_trust_dropped"] is True
    # Only alice had notes: everyone else gets a deny override for it; alice's own refusal is hers.
    line = {who: effective_policy(data, tenant_id="default", user_id=who)["summary"] for who in ("admin", "alice", "bob", "carol")}
    assert line["alice"] == f"Deny everything, allow listed workspaces · Shared workspace (rw) · {f['project']} (rw) · {f['notes']} (rw) · {f['secrets']} (refused) · {f['project_private']} (refused)"
    for who in ("admin", "bob", "carol"):
        assert line[who] == f"Deny everything, allow listed workspaces · Shared workspace (rw) · {f['project']} (rw) · {f['notes']} (refused) · {f['secrets']} (refused)", who
    # Idempotent.
    before = _read_store(data)
    gateway_policy(data)
    assert _read_store(data) == before


def test_migration_blacklist_is_any_folder_except_denied(f: dict, tmp_path: Path) -> None:
    from abstractgateway.workspace_policy import effective_policy, gateway_policy

    data = _data(tmp_path)
    _write_old_store(data, {
        "workspace_root": f["shared"],
        "workspace_default_mode": "blacklist",
        "workspace_blocked_paths": [f["secrets"]],
        "user_workspace_policies": {"alice": {"workspace_blocked_paths": [f["archive"]]}},
    })
    g = gateway_policy(data)
    assert g["posture"] == "any_except_denied" and g["default_mode"] == "rw"
    assert g["folders"] == [{"path": f["secrets"], "mode": "deny"}]
    assert effective_policy(data, tenant_id="default", user_id="alice")["summary"] == (
        f"Allow everything, refuse listed workspaces (rw) · Shared workspace (rw) · {f['secrets']} (refused) · {f['archive']} (refused)"
    )


def test_migration_pure_function_is_deterministic_and_audited(f: dict, tmp_path: Path) -> None:
    from abstractgateway.workspace_policy import gateway_policy, migrate_store

    old = {
        "workspace_root": f["shared"],
        "user_workspace_policies": {"b": {"workspace_allowed_paths": [f["notes"]]}, "a": {"workspace_allowed_paths": [f["project"]]}},
    }
    one, two = migrate_store(dict(old)), migrate_store(dict(old))
    for d in (one, two):
        d["_migrated"]["workspace_policy_v1"].pop("at")
    assert one == two
    assert [r["path"] for r in one["workspace_policy"]["folders"]] == [f["project"], f["notes"]]
    data = _data(tmp_path)
    _write_old_store(data, old)
    gateway_policy(data)
    gateway_policy(data)
    lines = [json.loads(ln) for ln in (data / "audit_log.jsonl").read_text().splitlines() if ln.strip()]
    assert len([e for e in lines if e.get("event") == "workspace_policy_changed" and e.get("scope") == "migration"]) == 1


def test_no_old_keys_means_no_migration_write(tmp_path: Path) -> None:
    from abstractgateway.workspace_policy import ensure_migrated, gateway_policy

    data = _data(tmp_path)
    _write_old_store(data, {"executor": "codex"})
    assert ensure_migrated(data) is False
    gateway_policy(data)
    assert _read_store(data) == {"executor": "codex"}


def test_the_most_specific_rule_and_a_deny_above_it(tmp_path: Path) -> None:
    from abstractgateway.workspace_policy import _rule

    a = tmp_path / "a"
    (a / "b" / "c").mkdir(parents=True)
    rows = [{"path": str(a / "b"), "mode": "rw"}, {"path": str(a), "mode": "ro"}]
    assert _rule(rows, a / "b" / "c") == "rw" and _rule(rows, a / "x") == "ro" and _rule(rows, tmp_path) is None
    # A store that reached a nested state (e.g. through the migration): nothing re-opens under a deny.
    assert _rule([{"path": str(a), "mode": "deny"}, {"path": str(a / "b"), "mode": "rw"}], a / "b" / "c") == "deny"


def test_the_shared_workspace_is_always_reachable_read_write(f: dict, tmp_path: Path) -> None:
    from abstractgateway.routes.gateway import _sanitize_run_workspace_policy

    sub = Path(f["shared"]) / "deliverables"
    sub.mkdir()
    with _client() as c:
        _gateway(c, f, folders=[])
        assert _sanitize_run_workspace_policy({"workspace_root": str(sub)}, principal=_P("admin"))["workspace_root"] == str(sub)
        _put(c, {"posture": "any_except_denied", "default_mode": "ro"})
        eff = c.get("/api/gateway/workspace/effective/me").json()
        assert eff["folders"][0] == {"path": f["shared"], "mode": "rw", "source": "shared"}
    from abstractgateway.workspace_policy import effective_folder_paths

    assert effective_folder_paths(_data(tmp_path), tenant_id="default", user_id="admin").mode(sub / "x.txt") == "rw"


def test_an_account_default_read_only_binds_its_runs(f: dict, tmp_path: Path) -> None:
    from abstractgateway.run_workspace_guard import apply_workspace_policy

    with _client() as c:
        _gateway(c, f, posture="any_except_denied", default_mode="rw", folders=[])
        alice = _user("alice")
        assert c.put("/api/gateway/workspace/policy/me", json={"default_mode": "ro"}, headers=alice).status_code == 200
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
        # With read-only rows the host sets its own exceptions; a rw row equal to the host mount
        # still does not reopen it (read-only wins on a tie, AbstractRuntime).
        _put(c, {"folders": [{"path": f["notes"], "mode": "rw"}, {"path": f["archive"], "mode": "ro"}]})
        v = {"workspace_root": str(session), "_runtime": {"workspace_read_only_paths": [f["notes"]]}}
        apply_workspace_policy(v, root_data_dir=_data(tmp_path), tenant_id="default", user_id="admin")
        assert f["notes"] in v["workspace_writable_paths"]
        with pytest.raises(ValueError, match="read-only"):
            _write(_scope(v), Path(f["notes"]) / "x.txt")
