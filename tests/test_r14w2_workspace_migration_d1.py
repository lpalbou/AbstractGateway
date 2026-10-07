"""Round 14, D1 (W6's hermetic 0.9.6 -> 0.10.0 upgrade): gateway 0.13.0's pre-round-9 -> v1 workspace
migration filtered the old folders by a GUESSED root (the old runtime's fallback: the process cwd,
normally $HOME for an installer-started gateway), then kept no row for that guess. Every allowed folder
under it vanished from the policy without being listed as dropped.

Proven here:
- the migration never uses the guess: a 0.9.6 user's allowed folder under the cwd becomes a listed row;
  a configured root (stored or env) still becomes the rw row that covers what sits under it;
- a store 0.13.0 already migrated is repaired ONCE from `_migrated.workspace_policy_v1.old` (rows
  restored, a path the policy already lists keeps its mode, other rows kept, recorded and audited,
  idempotent, nothing to do on a store migrated by this gateway);
- both at a REAL boot (`abstractgateway serve`, cwd = the folders' parent).
"""

from __future__ import annotations

import copy
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


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    home = (tmp_path / "home").resolve()
    proj = home / "projects" / "alice-proj"
    proj.mkdir(parents=True)
    (home / "private").mkdir()
    other = (tmp_path / "elsewhere").resolve()
    other.mkdir()
    data = tmp_path / "data"
    (data / "config").mkdir(parents=True)
    for name in ("ABSTRACTGATEWAY_WORKSPACE_ROOT", "ABSTRACTGATEWAY_WORKSPACE_DIR", "ABSTRACTGATEWAY_WORKSPACE_MOUNTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    monkeypatch.chdir(home)
    # The old runtime's guess, deterministically = the cwd (an installer-started gateway runs in $HOME).
    import abstractgateway.runtime_config as rc

    monkeypatch.setattr(rc, "_workspace_root_fallback", lambda: str(home))
    from abstractgateway.users import GatewayUserRegistry

    reg = GatewayUserRegistry(data / "auth" / "users.json")
    reg.create_user(user_id="alice", roles=["user"])
    reg.create_user(user_id="bob", roles=["user"])
    return {"home": str(home), "proj": str(proj.resolve()), "private": str((home / "private").resolve()), "other": str(other), "data": data}


def _old_096(w: dict) -> dict:
    """W6's 0.9.6 store (upgrade-evidence/runtime_config.before.json) + a gateway extra folder elsewhere
    and bob refusing the guessed folder itself."""
    return {
        "network": {"exposure": "localhost", "port": 19461},
        "_last_changed_by": "person:alice",
        "user_workspace_policies": {
            "default:alice": {"mode": "whitelist", "workspace_allowed_paths": [w["proj"]]},
            "default:bob": {"mode": "whitelist", "workspace_blocked_paths": [w["home"]]},
        },
        "workspace_mounts": [{"name": "elsewhere", "path": w["other"]}],
    }


def _write(w: dict, store: dict) -> None:
    (w["data"] / "config" / "runtime_config.json").write_text(json.dumps(store, indent=2))


def _read(w: dict) -> dict:
    return json.loads((w["data"] / "config" / "runtime_config.json").read_text())


def _as_0130_migrated(w: dict, old: dict) -> dict:
    """Exactly what gateway 0.13.0 wrote: v1 migration with shared = the guess, then shared_workspace
    blanked (no configured root), then v2; the v1 record keeps the old block verbatim."""
    from abstractgateway.workspace_policy import MIGRATION_MARKER, POLICY_KEY, migrate_store, migrate_store_v2

    buggy = migrate_store({**copy.deepcopy(old), "workspace_root": w["home"]}, accounts=["default:alice", "default:bob"])
    buggy[POLICY_KEY]["shared_workspace"] = ""
    buggy["_migrated"][MIGRATION_MARKER]["old"] = copy.deepcopy(old)
    out = migrate_store_v2(buggy)
    out["_last_changed_by"] = "system:workspace_policy_migration"
    return out


def _audit(w: dict) -> list:
    path = w["data"] / "audit_log.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []


def test_the_migration_keeps_folders_under_the_cwd_as_rows(world) -> None:
    from abstractgateway.workspace_policy import account_policy, ensure_migrated, gateway_policy

    _write(world, _old_096(world))
    assert ensure_migrated(world["data"]) is True
    # The migration itself records that nothing is left to repair (no write at the next read).
    right_after = _read(world)
    assert right_after["_migrated"]["workspace_policy_v1_repair"]["restored"] == []
    assert ensure_migrated(world["data"]) is False
    assert _read(world) == right_after
    g = gateway_policy(world["data"])
    assert g["posture"] == "any_except_denied"
    paths = {r["path"]: r["mode"] for r in g["folders"]}
    assert paths.get(world["proj"]) == "rw", g["folders"]  # alice's allowed folder: a listed row, not lost
    assert paths.get(world["other"]) == "rw"
    assert world["home"] not in paths, "the guess never becomes a row"
    bob = account_policy(world["data"], tenant_id="default", user_id="bob")
    assert {"path": world["home"], "mode": "deny"} in bob["folders"], bob  # bob's refusal of the cwd folder kept
    stored = _read(world)
    assert stored["_migrated"]["workspace_policy_v1"]["dropped_missing_or_conflicting"] == []
    assert stored["_migrated"]["workspace_policy_v1_repair"]["restored"] == []
    # Nothing left for the repair: a later read writes nothing.
    before = _read(world)
    assert ensure_migrated(world["data"]) is False
    gateway_policy(world["data"])
    assert _read(world) == before


def test_a_configured_root_still_covers_what_sits_under_it(world) -> None:
    from abstractgateway.workspace_policy import ensure_migrated, gateway_policy

    _write(world, {**_old_096(world), "workspace_root": world["home"]})
    ensure_migrated(world["data"])
    rows = gateway_policy(world["data"])["folders"]
    assert rows[0] == {"path": world["home"], "mode": "rw"}
    assert all(r["path"] != world["proj"] for r in rows), "already reachable under the listed root"


def test_a_store_migrated_by_0130_is_repaired_once(world) -> None:
    from abstractgateway.workspace_policy import account_policy, dropped_by_guessed_root, ensure_migrated, gateway_policy

    old = _old_096(world)
    buggy = _as_0130_migrated(world, old)
    # The bug, as W6 saw it: alice's folder is not listed, and not recorded as dropped.
    assert all(r["path"] != world["proj"] for r in buggy["workspace_policy"]["folders"])
    # (0.13.0 recorded bob's refusal of the guessed folder itself as dropped; alice's folder: not at all.)
    assert world["proj"] not in buggy["_migrated"]["workspace_policy_v1"]["dropped_missing_or_conflicting"]
    lost, lost_accounts = dropped_by_guessed_root(buggy, accounts=["default:alice", "default:bob"])
    assert lost == [{"path": world["proj"], "mode": "rw"}]
    # The fixed migration's rule for a whitelist gateway (nobody gains access): every account that did
    # not have alice's folder refuses it; bob also keeps his refusal of the guessed folder itself.
    expected_accounts = {
        "default:admin": [{"path": world["proj"], "mode": "deny"}],
        "default:bob": [{"path": world["proj"], "mode": "deny"}, {"path": world["home"], "mode": "deny"}],
    }
    assert lost_accounts == expected_accounts
    # Since the upgrade an admin added a row of their own: the repair keeps it.
    buggy["workspace_policy"]["folders"].append({"path": world["private"], "mode": "ro"})
    _write(world, buggy)

    assert ensure_migrated(world["data"]) is True
    rows = {r["path"]: r["mode"] for r in gateway_policy(world["data"])["folders"]}
    assert rows[world["proj"]] == "rw" and rows[world["private"]] == "ro" and rows[world["other"]] == "rw"
    bob = account_policy(world["data"], tenant_id="default", user_id="bob")
    assert {"path": world["home"], "mode": "deny"} in bob["folders"]
    stored = _read(world)
    rec = stored["_migrated"]["workspace_policy_v1_repair"]
    assert rec["restored"] == [{"path": world["proj"], "mode": "rw"}]
    assert rec["restored_accounts"] == expected_accounts
    assert stored["_migrated"]["workspace_policy_v1"]["old"] == old, "the old block stays verbatim"
    audits = [e for e in _audit(world) if e.get("event") == "workspace_policy_changed" and e.get("scope") == "migration_repair"]
    assert len(audits) == 1 and audits[0]["restored"] == [world["proj"]], audits
    # Once: a second pass changes nothing, even after the admin removes the restored row on purpose.
    from abstractgateway.workspace_policy import write_gateway_policy

    keep = [r for r in gateway_policy(world["data"])["folders"] if r["path"] != world["proj"]]
    write_gateway_policy(world["data"], {"folders": keep}, actor="person:admin")
    before = _read(world)
    assert ensure_migrated(world["data"]) is False
    assert _read(world) == before
    assert len([e for e in _audit(world) if e.get("scope") == "migration_repair"]) == 1


def test_a_path_the_policy_already_lists_keeps_its_mode(world) -> None:
    from abstractgateway.workspace_policy import ensure_migrated, gateway_policy

    buggy = _as_0130_migrated(world, _old_096(world))
    buggy["workspace_policy"]["folders"].append({"path": world["proj"], "mode": "ro"})  # re-added by an admin, ro
    _write(world, buggy)
    ensure_migrated(world["data"])
    rows = [r for r in gateway_policy(world["data"])["folders"] if r["path"] == world["proj"]]
    assert rows == [{"path": world["proj"], "mode": "ro"}]
    assert _read(world)["_migrated"]["workspace_policy_v1_repair"]["restored"] == []


def test_w6_literal_after_store_is_repaired(world) -> None:
    """The literal shape W6 captured after the 0.10.0 upgrade (runtime_config.after.json), paths swapped."""
    from abstractgateway.workspace_policy import ensure_migrated, gateway_policy

    old = {"user_workspace_policies": {"default:alice": {"mode": "whitelist", "workspace_allowed_paths": [world["proj"]]}}}
    _write(world, {
        "network": {"exposure": "localhost", "port": 19461},
        "_last_changed_by": "system:workspace_policy_migration",
        "workspace_policy": {"posture": "any_except_denied", "default_mode": "rw", "folders": []},
        "_migrated": {
            "workspace_policy_v1": {"at": "2026-10-06T23:58:00+00:00", "old": old, "env_mounts": None,
                                    "dropped_missing_or_conflicting": [], "narrowed_accounts": [], "launch_folder_trust_dropped": False},
            "workspace_policy_v2": {"at": "2026-10-06T23:58:00+00:00",
                                    "old": {"workspace_policy": {"shared_workspace": "", "posture": "allowed_only", "default_mode": "rw", "folders": []},
                                            "account_workspace_policies": {}},
                                    "shared_workspace_row": None, "posture": {"from": "allowed_only", "to": "any_except_denied"},
                                    "configured_accounts": [], "dropped_missing": []},
        },
    })
    assert ensure_migrated(world["data"]) is True
    assert gateway_policy(world["data"])["folders"] == [{"path": world["proj"], "mode": "rw"}]


# ------------------------------------------------------------------------------ real boots


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _serve_once(tmp: Path, data: Path, cwd: str) -> dict:
    home = Path(cwd)
    port = _free_port()
    env = {
        "HOME": str(home), "TMPDIR": str(tmp / "tmpdir"), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": os.pathsep.join(
            [str(Path(__import__("abstractgateway").__file__).resolve().parent.parent)]
            + [str(Path(x).resolve()) for x in os.environ.get("PYTHONPATH", "").split(os.pathsep) if x]
        ),
        "LANG": "en_US.UTF-8", "HF_HOME": str(tmp / "hf"), "HF_HUB_OFFLINE": "1",
        "ABSTRACTGATEWAY_DATA_DIR": str(data), "ABSTRACTGATEWAY_RUNNER": "0",
        "OLLAMA_BASE_URL": "http://127.0.0.1:9/", "LMSTUDIO_BASE_URL": "http://127.0.0.1:9/v1",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring", "NO_COLOR": "1",
    }
    (tmp / "tmpdir").mkdir(exist_ok=True)
    log_path = tmp / f"serve-{port}.log"
    with log_path.open("w") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "abstractgateway", "serve", "--host", "127.0.0.1", "--port", str(port), "--no-tray"],
            env=env, stdout=log, stderr=subprocess.STDOUT, cwd=cwd,
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
                raise AssertionError(log_path.read_text()[-3000:])
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
    return json.loads((data / "config" / "runtime_config.json").read_text())


def test_a_real_boot_in_home_keeps_the_096_folder(world, tmp_path: Path) -> None:
    _write(world, _old_096(world))
    stored = _serve_once(tmp_path, world["data"], world["home"])
    rows = {r["path"]: r["mode"] for r in stored["workspace_policy"]["folders"]}
    assert rows.get(world["proj"]) == "rw", stored["workspace_policy"]
    assert stored["_migrated"]["workspace_policy_v1_repair"]["restored"] == []
    again = _serve_once(tmp_path, world["data"], world["home"])
    assert {k: v for k, v in again.items()} == stored, "a second boot changes nothing"


def test_a_real_boot_repairs_a_store_0130_migrated(world, tmp_path: Path) -> None:
    _write(world, _as_0130_migrated(world, _old_096(world)))
    stored = _serve_once(tmp_path, world["data"], world["home"])
    rows = {r["path"]: r["mode"] for r in stored["workspace_policy"]["folders"]}
    assert rows.get(world["proj"]) == "rw", stored["workspace_policy"]
    assert stored["_migrated"]["workspace_policy_v1_repair"]["restored"] == [{"path": world["proj"], "mode": "rw"}]
    again = _serve_once(tmp_path, world["data"], world["home"])
    assert again == stored


def test_a_round9_store_whose_root_was_kept_needs_no_repair(world) -> None:
    """Round 9 KEPT its (guessed) root as the shared workspace, which v2 lists as a rw row: the folders
    under it were never lost, so the repair restores nothing there (no redundant rows)."""
    from abstractgateway.workspace_policy import MIGRATION_MARKER, ensure_migrated, gateway_policy, migrate_store, migrate_store_v2

    old = _old_096(world)
    r9 = migrate_store({**copy.deepcopy(old), "workspace_root": world["home"]}, accounts=["default:alice", "default:bob"])
    r9["_migrated"][MIGRATION_MARKER]["old"] = copy.deepcopy(old)
    _write(world, migrate_store_v2(r9))
    ensure_migrated(world["data"])
    rows = gateway_policy(world["data"])["folders"]
    assert {"path": world["home"], "mode": "rw"} in rows
    assert all(r["path"] != world["proj"] for r in rows)
    assert _read(world)["_migrated"]["workspace_policy_v1_repair"]["restored"] == []
