"""Round 9 (DESIGN.md R9.1): the workspace model is the tools model — the admin allows, the account
fine-tunes within what is allowed.

- Gateway policy: shared workspace (always one, required), allowed folders, allow any folder,
  never allowed, launch-folder trust. Admin writes; old-model fields refused by name.
- Account policy: enabled_folders ⊆ allowed_folders; own_folders only while any folder is allowed.
- Effective set (server-side): shared always first; never allowed wins; an account never exceeds the admin.
- Enforcement: run starts (400 when wider; narrowing honoured), the host's tool sandbox, the run
  workspace browser and the server file routes all read the effective set.
- Migration: the old whitelist/blacklist + launch trust + any-folder store, deterministically.
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
def folders(tmp_path: Path) -> dict:
    out = {}
    for name in ("shared", "projects", "notes", "secrets", "mine", "other"):
        p = tmp_path / "f" / name
        p.mkdir(parents=True)
        out[name] = str(p.resolve())
    inner = Path(out["projects"]) / "private"
    inner.mkdir()
    out["projects_private"] = str(inner.resolve())
    return out


def _data_dir(tmp_path: Path) -> Path:
    return tmp_path / "runtime"


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _user(user_id: str) -> dict:
    from abstractgateway.users import GatewayUserRegistry

    _rec, token = GatewayUserRegistry().create_user(user_id=user_id, roles=["user"])
    return {"Authorization": f"Bearer {token}"}


def _setup_gateway(c: TestClient, folders: dict, **extra) -> dict:
    body = {
        "shared_workspace": folders["shared"],
        "allowed_folders": [folders["projects"], folders["notes"]],
        "never_allowed": [folders["secrets"]],
    }
    body.update(extra)
    r = c.put("/api/gateway/workspace/policy", json=body)
    assert r.status_code == 200, r.text
    return r.json()["policy"]


# ---------------------------------------------------------------- gateway policy


def test_gateway_policy_read_write_and_refusals(folders: dict, tmp_path: Path) -> None:
    with _client() as c:
        g = c.get("/api/gateway/workspace/policy").json()["policy"]
        assert set(g) >= {"shared_workspace", "allowed_folders", "allow_any_folder", "never_allowed", "launch_folder_trust", "builtin_never_allowed"}
        assert g["allow_any_folder"] is False and g["launch_folder_trust"] is True and g["allowed_folders"] == []

        g = _setup_gateway(c, folders)
        assert g["shared_workspace"] == folders["shared"]
        assert g["allowed_folders"] == [folders["projects"], folders["notes"]]
        assert g["never_allowed"] == [folders["secrets"]]

        # Partial: one switch = one PUT, the rest keeps.
        g = c.put("/api/gateway/workspace/policy", json={"allow_any_folder": True}).json()["policy"]
        assert g["allow_any_folder"] is True and g["allowed_folders"] == [folders["projects"], folders["notes"]]

        def refused(body: dict, needle: str) -> None:
            r = c.put("/api/gateway/workspace/policy", json=body)
            assert r.status_code == 400, (body, r.text)
            assert needle in r.json()["detail"], r.json()["detail"]

        refused({"shared_workspace": ""}, "shared workspace is required")
        refused({"shared_workspace": None}, "shared workspace is required")
        refused({"shared_workspace": str(tmp_path / "missing")}, "No folder at this path")
        refused({"allowed_folders": ["relative/x"]}, "Use a full path")
        refused({"client_workspace_scope_overrides": True}, "Any folder (old clients)")
        refused({"mode": "blacklist"}, "no longer exist")
        refused({"workspace_default_mode": "blacklist"}, "unknown")
        refused({"never_allowed": [str(Path(folders["shared"]).parent)]}, "contains the shared workspace")
        refused({"never_allowed": [folders["projects"]]}, "never allowed wins")
        refused({"allow_any_folder": "yes"}, "true or false")
        refused({"shared_workspace": str(_data_dir(tmp_path).resolve())}, "never a workspace")
        # Nothing landed from the refused writes.
        assert c.get("/api/gateway/workspace/policy").json()["policy"]["never_allowed"] == [folders["secrets"]]

        alice = _user("alice")
        assert c.get("/api/gateway/workspace/policy", headers=alice).status_code == 200
        r = c.put("/api/gateway/workspace/policy", json={"allow_any_folder": False}, headers=alice)
        assert r.status_code == 403, r.text
        assert c.get("/api/gateway/workspace/policy").json()["policy"]["allow_any_folder"] is True


def test_runtime_config_refuses_the_old_workspace_keys(folders: dict) -> None:
    with _client() as c:
        for key, value in (
            ("workspace_root", folders["shared"]),
            ("workspace_mounts", f"p={folders['projects']}"),
            ("workspace_allowed_paths", [folders["projects"]]),
            ("workspace_blocked_paths", [folders["secrets"]]),
            ("client_workspace_scope_overrides", True),
            ("trust_client_launch_folder", False),
            ("workspace_default_mode", "blacklist"),
            ("user_workspace_policies", {}),
        ):
            r = c.post("/api/gateway/admin/runtime-config", json={key: value})
            assert r.status_code == 400, (key, r.text)
            assert "/api/gateway/workspace/policy" in r.json()["detail"], r.text
        cfg = c.get("/api/gateway/admin/runtime-config").json()
        for key in ("workspace_root", "workspace_mounts", "client_workspace_scope_overrides", "user_workspace_policies", "workspace_default_mode"):
            assert key not in cfg, key
        assert cfg["workspace_policy"]["endpoint"] == "/api/gateway/workspace/policy"
        # The old routes are gone.
        assert c.get("/api/gateway/workspace/policy/self").status_code == 410
        assert c.get("/api/gateway/admin/user-workspace-policy", params={"user_id": "admin"}).status_code == 404


# ---------------------------------------------------------------- account policy + effective


def test_account_policy_within_admin_allowance(folders: dict) -> None:
    with _client() as c:
        _setup_gateway(c, folders)
        alice = _user("alice")
        _user("bob")

        eff = c.get("/api/gateway/workspace/effective/me", headers=alice).json()
        assert eff["account"] == "default:alice"
        assert eff["folders"] == [{"path": folders["shared"], "source": "shared"}]  # extras default OFF
        assert [a["enabled"] for a in eff["available_folders"]] == [False, False]
        assert eff["summary"] == "Private session folder + Shared workspace (shared). Never: 1 folder."

        r = c.put("/api/gateway/workspace/policy/me", json={"enabled_folders": [folders["projects"]]}, headers=alice)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["policy"] == {"account": "default:alice", "enabled_folders": [folders["projects"]], "own_folders": []}
        assert [f["path"] for f in body["effective"]["folders"]] == [folders["shared"], folders["projects"]]
        assert folders["secrets"] in body["effective"]["never_allowed"]

        # An account never exceeds the admin's allowance.
        r = c.put("/api/gateway/workspace/policy/me", json={"enabled_folders": [folders["other"]]}, headers=alice)
        assert r.status_code == 400 and "only switch on what the admin allows" in r.json()["detail"]
        r = c.put("/api/gateway/workspace/policy/me", json={"own_folders": [folders["mine"]]}, headers=alice)
        assert r.status_code == 400 and "Allow any folder" in r.json()["detail"]
        r = c.put("/api/gateway/workspace/policy/me", json={"mode": "blacklist"}, headers=alice)
        assert r.status_code == 400

        # Self or admin only; `alice` by name works for herself and for the admin.
        assert c.get("/api/gateway/workspace/policy/bob", headers=alice).status_code == 403
        assert c.put("/api/gateway/workspace/policy/bob", json={"enabled_folders": []}, headers=alice).status_code == 403
        assert c.get("/api/gateway/workspace/effective/bob", headers=alice).status_code == 403
        assert c.get("/api/gateway/workspace/policy/alice", headers=alice).status_code == 200
        assert c.get("/api/gateway/workspace/policy/default:alice").status_code == 200
        assert c.get("/api/gateway/workspace/policy/nobody").status_code == 404
        r = c.put("/api/gateway/workspace/policy/bob", json={"enabled_folders": [folders["notes"]]})
        assert r.status_code == 200, r.text
        # The admin's own account.
        r = c.put("/api/gateway/workspace/policy/me", json={"enabled_folders": [folders["notes"]]})
        assert r.status_code == 200 and r.json()["policy"]["account"] == "default:admin"

        # Allow any folder: own folders become possible; never allowed still wins.
        c.put("/api/gateway/workspace/policy", json={"allow_any_folder": True})
        r = c.put("/api/gateway/workspace/policy/me", json={"own_folders": [folders["mine"]]}, headers=alice)
        assert r.status_code == 200, r.text
        assert [f["source"] for f in r.json()["effective"]["folders"]] == ["shared", "allowed", "own"]
        r = c.put("/api/gateway/workspace/policy/me", json={"own_folders": [folders["secrets"]]}, headers=alice)
        assert r.status_code == 400 and "never allowed wins" in r.json()["detail"]

        # Admin turns any-folder off: own folders stay stored but inactive.
        c.put("/api/gateway/workspace/policy", json={"allow_any_folder": False})
        eff = c.get("/api/gateway/workspace/effective/me", headers=alice).json()
        assert [f["source"] for f in eff["folders"]] == ["shared", "allowed"]
        assert eff["own_folders_inactive"] is True and eff["own_folders"] == [folders["mine"]]

        # Admin removes an allowed folder: no account keeps it switched on.
        c.put("/api/gateway/workspace/policy", json={"allowed_folders": [folders["notes"]]})
        eff = c.get("/api/gateway/workspace/effective/alice").json()
        assert [f["path"] for f in eff["folders"]] == [folders["shared"]]
        assert c.get("/api/gateway/workspace/policy/alice").json()["policy"]["enabled_folders"] == []


def test_never_allowed_wins_over_a_switched_on_folder(folders: dict, tmp_path: Path) -> None:
    from abstractgateway.workspace_policy import effective_policy

    with _client() as c:
        _setup_gateway(c, folders, allowed_folders=[folders["projects"], folders["notes"]])
        alice = _user("alice")
        c.put("/api/gateway/workspace/policy/me", json={"enabled_folders": [folders["projects"], folders["notes"]]}, headers=alice)
        # The admin later refuses the notes folder itself: it is dropped from the effective set.
        c.put("/api/gateway/workspace/policy", json={"allowed_folders": [folders["projects"]], "never_allowed": [folders["secrets"], folders["notes"]]})
        eff = effective_policy(_data_dir(tmp_path), tenant_id="default", user_id="alice")
        assert [f["path"] for f in eff["folders"]] == [folders["shared"], folders["projects"]]
        assert folders["notes"] in eff["never_allowed"]


def test_policy_changes_are_audited(folders: dict, tmp_path: Path) -> None:
    with _client() as c:
        _setup_gateway(c, folders)
        alice = _user("alice")
        c.put("/api/gateway/workspace/policy/me", json={"enabled_folders": [folders["notes"]]}, headers=alice)
    lines = [json.loads(ln) for ln in (_data_dir(tmp_path) / "audit_log.jsonl").read_text().splitlines() if ln.strip()]
    events = [e for e in lines if e.get("event") == "workspace_policy_changed"]
    assert {"scope": "gateway", "actor": "person:admin"}.items() <= events[0].items()
    assert sorted(events[0]["changed"]) == ["allowed_folders", "never_allowed", "shared_workspace"]
    assert events[-1]["scope"] == "account" and events[-1]["account"] == "default:alice" and events[-1]["actor"] == "person:alice"


# ---------------------------------------------------------------- enforcement


class _P:
    def __init__(self, user_id: str, tenant_id: str = "default") -> None:
        self.user_id = user_id
        self.tenant_id = tenant_id


def test_run_start_follows_the_effective_set(folders: dict, tmp_path: Path) -> None:
    from abstractgateway.routes.gateway import _sanitize_run_workspace_policy

    with _client() as c:
        _setup_gateway(c, folders, launch_folder_trust=False)
        alice = _user("alice")
        c.put("/api/gateway/workspace/policy/me", json={"enabled_folders": [folders["projects"]]}, headers=alice)

        p = _P("alice")
        # No workspace keys: the full effective set, never allowed in the sandbox's deny list.
        out = _sanitize_run_workspace_policy({}, principal=p)
        assert out["workspace_access_mode"] == "workspace_or_allowed"
        assert out["workspace_allowed_paths"] == [folders["shared"], folders["projects"]]
        assert folders["secrets"] in out["workspace_ignored_paths"].splitlines()

        # A client list NARROWS (shared always kept).
        out = _sanitize_run_workspace_policy({"workspace_allowed_paths": [folders["projects_private"]]}, principal=p)
        assert out["workspace_allowed_paths"] == [folders["shared"], folders["projects_private"]]

        def refused(data: dict, needle: str) -> None:
            with pytest.raises(HTTPException) as exc:
                _sanitize_run_workspace_policy(dict(data), principal=p)
            assert exc.value.status_code == 400 and needle in str(exc.value.detail), exc.value.detail

        refused({"workspace_access_mode": "all_except_ignored"}, "no longer exists")
        refused({"workspaceAccessMode": "all_except_ignored"}, "no longer exists")
        refused({"workspace_allowed_paths": [folders["notes"]]}, "outside the folders")  # allowed by admin, not switched on
        refused({"workspace_allowed_paths": [folders["other"]]}, "outside the folders")
        refused({"workspace_root": folders["other"]}, "outside the folders")
        refused({"workspace_root": folders["secrets"]}, "never allows")
        assert _sanitize_run_workspace_policy({"workspace_root": folders["projects_private"]}, principal=p)["workspace_root"] == folders["projects_private"]

        # Launch-folder trust: the folder the app was started from may be the root (never allowed still wins).
        c.put("/api/gateway/workspace/policy", json={"launch_folder_trust": True})
        assert _sanitize_run_workspace_policy({"workspace_root": folders["other"]}, principal=p)["workspace_root"] == folders["other"]
        refused({"workspace_root": folders["secrets"]}, "never allows")
        # ... but trust never widens the allowed folders.
        refused({"workspace_root": folders["other"], "workspace_allowed_paths": [folders["mine"]]}, "outside the folders")

        # Bob switched nothing on: shared only.
        assert _sanitize_run_workspace_policy({}, principal=_P("bob"))["workspace_allowed_paths"] == [folders["shared"]]


def test_host_guard_binds_every_run_to_the_effective_set(folders: dict, tmp_path: Path) -> None:
    from abstractgateway.run_workspace_guard import apply_workspace_policy

    with _client() as c:
        _setup_gateway(c, folders)
        alice = _user("alice")
        c.put("/api/gateway/workspace/policy/me", json={"enabled_folders": [folders["projects"]]}, headers=alice)
    data = _data_dir(tmp_path)
    v = {"workspace_access_mode": "all_except_ignored", "workspace_allowed_paths": [folders["other"], folders["projects"]]}
    apply_workspace_policy(v, root_data_dir=data, tenant_id="default", user_id="alice")
    assert v["workspace_access_mode"] == "workspace_or_allowed"
    assert v["workspace_allowed_paths"] == [folders["shared"], folders["projects"]]
    assert v["workspace_ignored_paths"].splitlines() == [folders["secrets"]]
    v = {}
    apply_workspace_policy(v, root_data_dir=data, tenant_id="default", user_id="bob")
    assert v["workspace_allowed_paths"] == [folders["shared"]]
    v = {"workspace_access_mode": "workspace_only", "workspace_ignored_paths": "/x"}
    apply_workspace_policy(v, root_data_dir=data, tenant_id="default", user_id="alice")
    assert v["workspace_access_mode"] == "workspace_only" and "workspace_allowed_paths" not in v
    assert v["workspace_ignored_paths"].splitlines() == ["/x", folders["secrets"]]


def test_server_file_routes_follow_the_effective_set(folders: dict) -> None:
    """The /files routes are admin-gated; they follow the ADMIN account's own effective set."""
    (Path(folders["shared"]) / "hello.txt").write_text("hi")
    (Path(folders["projects"]) / "plan.md").write_text("plan")
    with _client() as c:
        _setup_gateway(c, folders)
        r = c.get("/api/gateway/files/list")
        assert r.status_code == 200, r.text
        listed = json.dumps(r.json()["items"])
        assert "hello.txt" in listed and "plan.md" not in listed, listed
        assert c.get("/api/gateway/files/list", params={"workspace_access_mode": "all_except_ignored"}).status_code == 400
        assert c.get("/api/gateway/files/list", params={"workspace_root": folders["projects"]}).status_code == 400  # not switched on
        assert c.get("/api/gateway/files/list", params={"workspace_root": folders["other"]}).status_code == 400
        c.put("/api/gateway/workspace/policy/me", json={"enabled_folders": [folders["projects"]]})
        r = c.get("/api/gateway/files/list", params={"workspace_root": folders["projects"]})
        assert r.status_code == 200 and "plan.md" in json.dumps(r.json()["items"]), r.text
        alice = _user("alice")
        assert c.get("/api/gateway/files/list", headers=alice).status_code == 403


# ---------------------------------------------------------------- migration


def _write_old_store(data_dir: Path, stored: dict) -> None:
    path = data_dir / "config" / "runtime_config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stored))


def _read_store(data_dir: Path) -> dict:
    return json.loads((data_dir / "config" / "runtime_config.json").read_text())


def test_migration_whitelist_mounts_and_per_user_lists(folders: dict, tmp_path: Path) -> None:
    from abstractgateway.workspace_policy import effective_policy, gateway_policy

    data = _data_dir(tmp_path)
    old = {
        "workspace_root": folders["shared"],
        "workspace_mounts": [{"name": "projects", "path": folders["projects"]}, {"name": "gone", "path": str(tmp_path / "gone")}],
        "workspace_blocked_paths": [folders["secrets"]],
        "client_workspace_scope_overrides": True,
        "trust_client_launch_folder": False,
        "workspace_default_mode": "whitelist",
        "user_workspace_policies": {
            "default:bob": {"workspace_allowed_paths": [folders["notes"]], "client_workspace_scope_overrides": True, "trust_client_launch_folder": True},
            "alice": {"workspace_allowed_paths": [folders["projects"], folders["mine"]], "workspace_blocked_paths": [folders["projects_private"]]},
        },
        "executor": "codex",
    }
    _write_old_store(data, old)
    from abstractgateway.users import GatewayUserRegistry

    GatewayUserRegistry(data / "auth" / "users.json").create_user(user_id="carol", roles=["user"])
    g = gateway_policy(data)
    assert g["shared_workspace"] == folders["shared"]
    # mounts first, then accounts in key order (default:alice, default:bob); the missing mount dropped
    assert g["allowed_folders"] == [folders["projects"], folders["mine"], folders["notes"]]
    assert g["allow_any_folder"] is False  # whitelist everywhere
    assert g["never_allowed"] == [folders["secrets"], folders["projects_private"]]  # per-account refusals become gateway-wide
    assert g["launch_folder_trust"] is False  # gateway value; bob's per-account trust dropped
    stored = _read_store(data)
    for key in ("workspace_root", "workspace_mounts", "workspace_blocked_paths", "client_workspace_scope_overrides",
                "trust_client_launch_folder", "workspace_default_mode", "user_workspace_policies"):
        assert key not in stored, key
    assert stored["executor"] == "codex"  # other settings untouched
    record = stored["_migrated"]["workspace_policy_v1"]
    assert record["old"]["user_workspace_policies"] == old["user_workspace_policies"]
    assert str(tmp_path / "gone") in record["dropped_missing_or_conflicting"]
    # The gateway's extra workspace applied to every existing account: it stays ON for each of them
    # (carol is in the registry); an account's own allowed folders are ON for that account only.
    assert stored["account_workspace_policies"] == {
        "default:admin": {"enabled_folders": [folders["projects"]], "own_folders": []},
        "default:alice": {"enabled_folders": [folders["projects"], folders["mine"]], "own_folders": []},
        "default:bob": {"enabled_folders": [folders["projects"], folders["notes"]], "own_folders": []},
        "default:carol": {"enabled_folders": [folders["projects"]], "own_folders": []},
    }
    eff = effective_policy(data, tenant_id="default", user_id="alice")
    assert [f["path"] for f in eff["folders"]] == [folders["shared"], folders["projects"], folders["mine"]]
    # An account created after the migration starts with the extras off.
    assert [f["path"] for f in effective_policy(data, tenant_id="default", user_id="dave")["folders"]] == [folders["shared"]]
    # Idempotent: a second read changes nothing.
    before = _read_store(data)
    gateway_policy(data)
    assert _read_store(data) == before


def test_migration_blacklist_means_allow_any_folder(folders: dict, tmp_path: Path) -> None:
    from abstractgateway.workspace_policy import gateway_policy

    data = _data_dir(tmp_path)
    _write_old_store(data, {"workspace_root": folders["shared"], "workspace_default_mode": "blacklist", "workspace_blocked_paths": [folders["secrets"]]})
    g = gateway_policy(data)
    assert g["allow_any_folder"] is True and g["never_allowed"] == [folders["secrets"]] and g["allowed_folders"] == []
    assert g["launch_folder_trust"] is True  # default


def test_migration_per_user_blacklist_and_shared_conflict(folders: dict, tmp_path: Path) -> None:
    from abstractgateway.workspace_policy import gateway_policy

    data = _data_dir(tmp_path)
    parent = str(Path(folders["shared"]).parent)
    _write_old_store(data, {
        "workspace_root": folders["shared"],
        "workspace_blocked_paths": [parent],  # contains the shared workspace: cannot stay (shared always present)
        "user_workspace_policies": {"default:eve": {"mode": "blacklist", "workspace_blocked_paths": [folders["secrets"]]}},
    })
    g = gateway_policy(data)
    assert g["allow_any_folder"] is True
    assert g["never_allowed"] == [folders["secrets"]]
    assert parent in _read_store(data)["_migrated"]["workspace_policy_v1"]["dropped_missing_or_conflicting"]


def test_no_old_keys_means_no_migration_write(tmp_path: Path) -> None:
    from abstractgateway.workspace_policy import ensure_migrated, gateway_policy

    data = _data_dir(tmp_path)
    _write_old_store(data, {"executor": "codex"})
    assert ensure_migrated(data) is False
    gateway_policy(data)
    assert _read_store(data) == {"executor": "codex"}


def test_migration_pure_function_is_deterministic(folders: dict) -> None:
    from abstractgateway.workspace_policy import migrate_store

    old = {
        "workspace_root": folders["shared"],
        "user_workspace_policies": {"b": {"workspace_allowed_paths": [folders["notes"]]}, "a": {"workspace_allowed_paths": [folders["projects"]]}},
    }
    one, two = migrate_store(dict(old)), migrate_store(dict(old))
    for d in (one, two):
        d["_migrated"]["workspace_policy_v1"].pop("at")
    assert one == two
    assert one["workspace_policy"]["allowed_folders"] == [folders["projects"], folders["notes"]]


def test_migration_reports_missing_per_account_folders(folders: dict, tmp_path: Path) -> None:
    from abstractgateway.workspace_policy import gateway_policy

    data = _data_dir(tmp_path)
    gone = str(tmp_path / "gone-user-folder")
    _write_old_store(data, {
        "workspace_root": folders["shared"],
        "workspace_blocked_paths": f"{folders['secrets']}\n",
        "user_workspace_policies": json.dumps({"alice": {"workspace_allowed_paths": [folders["notes"], gone], "mode": "Whitelist"}}),
    })
    g = gateway_policy(data)
    assert g["allowed_folders"] == [folders["notes"]] and g["never_allowed"] == [folders["secrets"]]
    assert g["allow_any_folder"] is False
    assert gone in _read_store(data)["_migrated"]["workspace_policy_v1"]["dropped_missing_or_conflicting"]


def test_duplicates_are_refused_by_name(folders: dict) -> None:
    with _client() as c:
        r = c.put("/api/gateway/workspace/policy", json={"shared_workspace": folders["shared"], "allowed_folders": [folders["notes"], folders["notes"] + "/"]})
        assert r.status_code == 400 and "listed twice" in r.json()["detail"], r.text
        _setup_gateway(c, folders)
        r = c.put("/api/gateway/workspace/policy/me", json={"enabled_folders": [folders["notes"], folders["notes"]]})
        assert r.status_code == 400 and "listed twice" in r.json()["detail"], r.text


def test_an_entity_cannot_change_its_own_folders(folders: dict) -> None:
    from abstractgateway.users import GatewayUserRegistry

    with _client() as c:
        _setup_gateway(c, folders)
        _rec, token = GatewayUserRegistry().create_user(user_id="castor", roles=["entity"])
        entity = {"Authorization": f"Bearer {token}"}
        assert GatewayUserRegistry().get_user("castor").principal_kind == "entity"
        r = c.put("/api/gateway/workspace/policy/me", json={"enabled_folders": [folders["notes"]]}, headers=entity)
        assert r.status_code == 403, r.text
        r = c.put("/api/gateway/workspace/policy/castor", json={"enabled_folders": [folders["notes"]]})
        assert r.status_code == 200, r.text
        assert [f["path"] for f in r.json()["effective"]["folders"]] == [folders["shared"], folders["notes"]]


def test_the_runtime_tool_scope_refuses_what_the_account_may_not_use(folders: dict, tmp_path: Path) -> None:
    """Host layer (ADVERSARY V6): the run vars the gateway emits, resolved by the runtime's own tool
    scope — an account cannot reach a folder the admin allowed but it did not switch on, nor a
    never-allowed folder inside a switched-on one, nor anything else."""
    from abstractruntime.integrations.abstractcore.workspace_scoped_tools import WorkspaceScope, rewrite_tool_arguments

    from abstractgateway.run_workspace_guard import apply_workspace_policy

    with _client() as c:
        _setup_gateway(c, folders, never_allowed=[folders["secrets"], folders["projects_private"]])
        alice = _user("alice")
        c.put("/api/gateway/workspace/policy/me", json={"enabled_folders": [folders["projects"]]}, headers=alice)
    session = tmp_path / "session-folder"
    session.mkdir()
    for rel in ("shared/a.txt", "projects/b.txt", "projects/private/c.txt", "notes/d.txt", "other/e.txt", "secrets/f.txt"):
        (Path(folders["shared"]).parent / rel).write_text("x")
    v = {"workspace_root": str(session)}
    apply_workspace_policy(v, root_data_dir=_data_dir(tmp_path), tenant_id="default", user_id="alice")
    scope = WorkspaceScope.from_input_data(v)
    base = Path(folders["shared"]).parent
    for ok in ("shared/a.txt", "projects/b.txt"):
        out = rewrite_tool_arguments(tool_name="read_file", args={"file_path": str(base / ok)}, scope=scope)
        assert Path(out["file_path"]).resolve() == (base / ok).resolve()
    for refused in ("projects/private/c.txt", "notes/d.txt", "other/e.txt", "secrets/f.txt"):
        with pytest.raises(ValueError):
            rewrite_tool_arguments(tool_name="read_file", args={"file_path": str(base / refused)}, scope=scope)


def test_migration_is_audited(folders: dict, tmp_path: Path) -> None:
    from abstractgateway.workspace_policy import gateway_policy

    data = _data_dir(tmp_path)
    _write_old_store(data, {"workspace_root": folders["shared"]})
    gateway_policy(data)
    gateway_policy(data)
    lines = [json.loads(ln) for ln in (data / "audit_log.jsonl").read_text().splitlines() if ln.strip()]
    migrations = [e for e in lines if e.get("event") == "workspace_policy_changed" and e.get("scope") == "migration"]
    assert len(migrations) == 1, migrations
