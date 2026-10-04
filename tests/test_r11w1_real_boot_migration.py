"""Round 11 at a REAL boot (`abstractgateway serve`, hermetic: scratch HOME and data dir, runner off,
no tray, loopback, no provider keys) on a copy of the operator's :8080 store SHAPE (round 9, read
2026-10-04 with GET only): `workspace_policy {shared_workspace, posture: allowed_only, default_mode,
folders: []}`, the `_migrated.workspace_policy_v1` record, and two entity homes that predate entity
accounts (castor, mnemosyne: a manifest, no user record) next to a minted one.

After the boot: posture "Allow everything, refuse listed workspaces", the old shared workspace is a
listed rw row, round-9 account entries are configured layers, every entity home has an account
(audited once), and a second boot changes nothing.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _serve_once(tmp_path: Path, data: Path) -> dict:
    home = tmp_path / "home"
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    port = _free_port()
    env = {
        "HOME": str(home),
        "TMPDIR": str(home / "tmp"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        # Absolute: the subprocess runs in tmp_path, where a relative `src` would silently import an
        # installed gateway instead of the one under test.
        "PYTHONPATH": os.pathsep.join(
            [str(Path(__import__("abstractgateway").__file__).resolve().parent.parent)]
            + [str(Path(x).resolve()) for x in os.environ.get("PYTHONPATH", "").split(os.pathsep) if x]
        ),
        "LANG": "en_US.UTF-8",
        "HF_HOME": str(home / "hf"),
        "HF_HUB_OFFLINE": "1",
        "ABSTRACTGATEWAY_DATA_DIR": str(data),
        "ABSTRACTGATEWAY_RUNNER": "0",
        "OLLAMA_BASE_URL": "http://127.0.0.1:9/",
        "LMSTUDIO_BASE_URL": "http://127.0.0.1:9/v1",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        "NO_COLOR": "1",
    }
    log = (tmp_path / f"serve-{port}.log").open("w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "abstractgateway", "serve", "--host", "127.0.0.1", "--port", str(port), "--no-tray"],
        env=env, stdout=log, stderr=subprocess.STDOUT, cwd=str(tmp_path),
    )
    try:
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                    if r.status == 200:
                        break
            except Exception:
                time.sleep(0.5)
        else:
            raise AssertionError((tmp_path / f"serve-{port}.log").read_text()[-3000:])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
    return json.loads((data / "config" / "runtime_config.json").read_text())



def _seed_8080_shape(tmp_path: Path, data: Path) -> dict:
    root = tmp_path / "abstractframework"  # the operator's workspace root (shared workspace) on :8080
    (root / "notes").mkdir(parents=True)
    (data / "config").mkdir(parents=True)
    store = {
        "_last_changed_at": "2026-10-04T14:48:28.581476+00:00",
        "_last_changed_by": "system:workspace_policy_migration",
        "_migrated": {
            "workspace_policy_v1": {
                "at": "2026-10-04T14:48:28.581476+00:00",
                "old": {
                    "workspace_root": str(root),
                    "client_workspace_scope_overrides": True,
                    "workspace_default_mode": "whitelist",
                    "user_workspace_policies": {"default:user": {"mode": "whitelist", "trust_client_launch_folder": True}},
                },
                "env_mounts": None,
                "dropped_missing_or_conflicting": [],
                "narrowed_accounts": [],
                "launch_folder_trust_dropped": True,
            }
        },
        "executor": "codex",
        "workspace_policy": {"shared_workspace": str(root.resolve()), "posture": "allowed_only", "default_mode": "rw", "folders": []},
        # A round-9 account narrowing (not on :8080 today; the shape every round-9 store may hold).
        "account_workspace_policies": {"default:user": {"default_mode": None, "folders": [{"path": str((root / "notes").resolve()), "mode": "deny"}]}},
    }
    (data / "config" / "runtime_config.json").write_text(json.dumps(store, indent=2))
    from abstractgateway.users import GatewayUserRegistry

    reg = GatewayUserRegistry(data / "auth" / "users.json")
    reg.create_user(user_id="admin", roles=["admin", "user"], runtime_id="default")
    reg.create_user(user_id="user", roles=["user"])
    reg.create_user(user_id="doorcheck", roles=["entity"], scopes=["entity:doorcheck"], runtime_id="doorcheck")
    for slug, name in (("castor", "castor"), ("mnemosyne", "Mnemosyne"), ("doorcheck", "doorcheck")):
        home = data / "entities" / slug
        home.mkdir(parents=True)
        (home / "manifest.json").write_text(json.dumps({"slug": slug, "name": name, "entity_id": f"entity:{slug}@home-1", "home_id": "home-1", "format_version": 1}))
    return {"root": str(root.resolve()), "notes": str((root / "notes").resolve())}


def _audit(data: Path) -> list:
    path = data / "audit_log.jsonl"
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()] if path.exists() else []


def test_a_real_boot_migrates_the_8080_store_shape_and_gives_every_entity_an_account(tmp_path: Path) -> None:
    data = tmp_path / "data"
    seeded = _seed_8080_shape(tmp_path, data)
    stored = _serve_once(tmp_path, data)

    assert stored["workspace_policy"] == {"posture": "any_except_denied", "default_mode": "rw", "folders": [{"path": seeded["root"], "mode": "rw"}]}
    assert stored["account_workspace_policies"] == {
        "default:user": {"posture": "any_except_denied", "default_mode": "rw", "folders": [{"path": seeded["notes"], "mode": "deny"}]}
    }
    record = stored["_migrated"]["workspace_policy_v2"]
    assert record["shared_workspace_row"] == seeded["root"] and record["posture"] == {"from": "allowed_only", "to": "any_except_denied"}
    assert record["old"]["workspace_policy"]["shared_workspace"] == seeded["root"]
    assert stored["_migrated"]["workspace_policy_v1"]["old"]["workspace_root"]  # the v1 record is kept
    assert stored["executor"] == "codex"

    from abstractgateway.users import GatewayUserRegistry
    from abstractgateway.workspace_policy import effective_policy

    reg = GatewayUserRegistry(data / "auth" / "users.json")
    for slug in ("castor", "mnemosyne", "doorcheck"):
        rec = reg.get_user(slug)
        assert rec is not None and rec.principal_kind == "entity" and "admin" not in rec.roles, slug
    created = [e for e in _audit(data) if e.get("event") == "entity_account_created"]
    assert sorted(e["entity"] for e in created) == ["castor", "mnemosyne"]
    assert all(e["reason"] == "migration" for e in created)
    migrations = [e for e in _audit(data) if e.get("event") == "workspace_policy_changed" and e.get("scope") == "migration"]
    assert len(migrations) == 1

    import os

    os.environ["ABSTRACTGATEWAY_DATA_DIR"] = str(data)
    try:
        assert effective_policy(data, tenant_id="default", user_id="user")["summary"] == (
            f"Allow everything, refuse listed workspaces (rw) · {seeded['notes']} (refused) · {seeded['root']} (rw)"
        )
        assert effective_policy(data, tenant_id="default", user_id="admin")["summary"] == (
            f"Allow everything, refuse listed workspaces (rw) · {seeded['root']} (rw)"
        )
    finally:
        os.environ.pop("ABSTRACTGATEWAY_DATA_DIR", None)

    # Idempotent: a second boot changes neither the store's model nor the accounts, and audits nothing new.
    before_users = (data / "auth" / "users.json").read_text()
    again = _serve_once(tmp_path, data)
    for key in ("workspace_policy", "account_workspace_policies"):
        assert again[key] == stored[key], key
    assert again["_migrated"]["workspace_policy_v2"] == record
    assert len([e for e in _audit(data) if e.get("event") == "entity_account_created"]) == 2
    assert len([e for e in _audit(data) if e.get("event") == "workspace_policy_changed" and e.get("scope") == "migration"]) == 1
    users_after = json.loads((data / "auth" / "users.json").read_text())["users"]
    assert sorted(u["user_id"] for u in users_after) == sorted(u["user_id"] for u in json.loads(before_users)["users"])
