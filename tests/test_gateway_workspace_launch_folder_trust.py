"""Launch-folder trust + per-user workspace policies (operator ruling
2026-08-19: "wherever an agent is started, it should be able to write in
the folder it starts" — and this class of knob is a SETTING, per user,
admin-modifiable in both consoles, never a new env var).

Pins:
- trust_client_launch_folder defaults TRUE with NO env rung (stored > default);
  a run's client-named workspace_root (the launch folder) is accepted and
  writable by default — the red 400 the old default produced is gone.
- The gateway deny list still beats trust; trust never widens the separate
  allowed-paths clamp beyond the accepted root's subtree.
- user_workspace_policies: admin-set per-principal overrides (trust,
  scope-overrides, extra allowed/blocked roots) resolved at run start.
- Refusal texts teach the console settings, never env var names.
- Non-admin reads redact per-user policies to a count posture.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic


def _principal(user_id: str = "alice", tenant_id: str = "default"):
    from abstractgateway.security.principal import GatewayPrincipal

    return GatewayPrincipal(user_id=user_id, tenant_id=tenant_id, roles=("user",))


def _lockdown_env(monkeypatch: pytest.MonkeyPatch, *, ws: Path, data_dir: Path) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKSPACE_DIR", str(ws))
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOW_CLIENT_WORKSPACE_SCOPE", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_TRUST_CLIENT_WORKSPACE_SCOPE", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_TOOL_MODE", "passthrough")


# ------------------------------------------------ the knob itself (store)


def test_trust_knob_defaults_true_and_has_no_env_rung(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.runtime_config import read_runtime_config, resolve_trust_client_launch_folder

    # No store, and even a hostile env spelling must not matter — this knob
    # deliberately has no env rung (the env era for this class is over).
    monkeypatch.setenv("ABSTRACTGATEWAY_TRUST_CLIENT_LAUNCH_FOLDER", "0")
    cfg = read_runtime_config(tmp_path)
    assert cfg["trust_client_launch_folder"] == {"value": True, "source": "default"}
    assert resolve_trust_client_launch_folder(tmp_path) is True


def test_trust_knob_roundtrip_and_clear(tmp_path) -> None:
    from abstractgateway.runtime_config import (
        read_runtime_config,
        resolve_trust_client_launch_folder,
        write_runtime_config,
    )

    out = write_runtime_config(tmp_path, {"trust_client_launch_folder": False}, actor="person:admin")
    assert out["applied"]["trust_client_launch_folder"] is False
    assert read_runtime_config(tmp_path)["trust_client_launch_folder"] == {"value": False, "source": "stored"}
    assert resolve_trust_client_launch_folder(tmp_path) is False

    # Clearing falls back to the TRUE default, and says so.
    write_runtime_config(tmp_path, {"trust_client_launch_folder": None}, actor="person:admin")
    assert read_runtime_config(tmp_path)["trust_client_launch_folder"] == {"value": True, "source": "default"}


def test_user_policies_roundtrip_validation_and_redaction(tmp_path) -> None:
    from abstractgateway.runtime_config import (
        RuntimeConfigError,
        read_runtime_config,
        resolve_trust_client_launch_folder,
        resolve_user_workspace_paths,
        write_runtime_config,
    )

    proj = tmp_path / "proj"
    proj.mkdir()
    write_runtime_config(
        tmp_path,
        {
            "trust_client_launch_folder": False,
            "user_workspace_policies": {
                # Bare user folds onto the default tenant (single-operator spelling).
                "alice": {"trust_client_launch_folder": True, "workspace_allowed_paths": [str(proj)]},
                "acme:bob": {"workspace_blocked_paths": [str(proj)]},
            },
        },
        actor="person:admin",
    )

    cfg = read_runtime_config(tmp_path)
    policies = cfg["user_workspace_policies"]
    assert policies["source"] == "stored"
    assert set(policies["value"]) == {"default:alice", "acme:bob"}

    # Per-user override beats the global knob; other users keep the global.
    assert resolve_trust_client_launch_folder(tmp_path, tenant_id="default", user_id="alice") is True
    assert resolve_trust_client_launch_folder(tmp_path, tenant_id="default", user_id="carol") is False
    allowed, blocked = resolve_user_workspace_paths(tmp_path, tenant_id="acme", user_id="bob")
    assert allowed == () and blocked == (proj.resolve(),)

    # Non-admin read: counts only — other users' names/paths never leak.
    user_view = read_runtime_config(tmp_path, is_admin=False)
    assert user_view["user_workspace_policies"] == {"configured": True, "count": 2, "source": "stored"}

    # Validate-before-write: unknown fields and relative paths refuse whole.
    with pytest.raises(RuntimeConfigError):
        write_runtime_config(tmp_path, {"user_workspace_policies": {"x": {"nope": 1}}}, actor="t")
    with pytest.raises(RuntimeConfigError):
        write_runtime_config(
            tmp_path,
            {"user_workspace_policies": {"x": {"workspace_allowed_paths": ["rel/path"]}}},
            actor="t",
        )
    # A JSON-object STRING is accepted (console textareas send text).
    out = write_runtime_config(tmp_path, {"user_workspace_policies": "{}"}, actor="t")
    assert out["applied"]["user_workspace_policies"] == {}
    assert read_runtime_config(tmp_path)["user_workspace_policies"]["count"] == 0


def test_user_mode_validation_and_resolution(tmp_path) -> None:
    from abstractgateway.runtime_config import (
        RuntimeConfigError,
        resolve_user_workspace_mode,
        write_runtime_config,
    )

    with pytest.raises(RuntimeConfigError):
        write_runtime_config(
            tmp_path, {"user_workspace_policies": {"alice": {"mode": "wide-open"}}}, actor="t"
        )
    write_runtime_config(
        tmp_path, {"user_workspace_policies": {"alice": {"mode": "blacklist"}}}, actor="t"
    )
    assert resolve_user_workspace_mode(tmp_path, tenant_id="default", user_id="alice") == "blacklist"
    # Everyone else (and unset) defaults to whitelist — deny everything,
    # allow the configured roots.
    assert resolve_user_workspace_mode(tmp_path, tenant_id="default", user_id="carol") == "whitelist"


def test_gateway_default_mode_is_the_inherited_posture(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The gateway-wide default posture (operator order 2026-08-19: 'we are
    the gateway, where do we set the default'): users without their own mode
    inherit it; a stored per-user mode still wins."""
    from abstractgateway.runtime_config import (
        RuntimeConfigError,
        read_runtime_config,
        resolve_user_workspace_mode,
        resolve_workspace_default_mode,
        write_runtime_config,
    )

    assert read_runtime_config(tmp_path)["workspace_default_mode"] == {
        "value": "whitelist",
        "source": "default",
    }
    with pytest.raises(RuntimeConfigError):
        write_runtime_config(tmp_path, {"workspace_default_mode": "wide-open"}, actor="t")

    write_runtime_config(
        tmp_path,
        {
            "workspace_default_mode": "blacklist",
            "user_workspace_policies": {"carol": {"mode": "whitelist"}},
        },
        actor="person:admin",
    )
    assert resolve_workspace_default_mode(tmp_path) == "blacklist"
    assert resolve_user_workspace_mode(tmp_path, tenant_id="default", user_id="alice") == "blacklist"
    assert resolve_user_workspace_mode(tmp_path, tenant_id="default", user_id="carol") == "whitelist"

    # And the sanitize lane honors the inherited blacklist posture.
    ws = tmp_path / "workspace"
    ws.mkdir()
    # Outside the gateway data folder (tmp_path here): a data-folder path is
    # never a workspace, whatever the posture.
    anywhere = tmp_path.parent / f"{tmp_path.name}-anywhere"
    anywhere.mkdir()
    _lockdown_env(monkeypatch, ws=ws, data_dir=tmp_path)
    from abstractgateway.routes.gateway import _sanitize_run_workspace_policy

    write_runtime_config(tmp_path, {"trust_client_launch_folder": False}, actor="person:admin")
    ok = _sanitize_run_workspace_policy({"workspace_root": str(anywhere)}, principal=_principal("alice"))
    assert ok["workspace_root"] == str(anywhere.resolve())
    assert ok["workspace_access_mode"] == "all_except_ignored"

    # Clearing returns the shipped deny-all default.
    write_runtime_config(tmp_path, {"workspace_default_mode": None}, actor="person:admin")
    assert resolve_workspace_default_mode(tmp_path) == "whitelist"


def test_self_service_policy_write_reads_back_and_scopes_to_self(tmp_path) -> None:
    from abstractgateway.runtime_config import (
        read_user_workspace_policy,
        write_user_workspace_policy,
    )

    proj = tmp_path / "proj"
    proj.mkdir()
    out = write_user_workspace_policy(
        tmp_path,
        tenant_id="default",
        user_id="alice",
        policy={"mode": "blacklist", "workspace_blocked_paths": [str(proj)]},
        actor="person:alice",
    )
    assert out["effective"]["mode"] == "blacklist"
    assert out["effective"]["workspace_blocked_paths"] == [str(proj.resolve())]

    # bob's read is untouched by alice's write — the self lane is per-key.
    bob = read_user_workspace_policy(tmp_path, tenant_id="default", user_id="bob")
    assert bob["policy"] == {}
    assert bob["effective"]["mode"] == "whitelist"
    assert bob["effective"]["trust_client_launch_folder"] is True  # global default

    # Clearing returns alice to the inherited posture.
    cleared = write_user_workspace_policy(
        tmp_path, tenant_id="default", user_id="alice", policy=None, actor="person:alice"
    )
    assert cleared["policy"] == {}
    assert cleared["effective"]["mode"] == "whitelist"


# ------------------------------------------- enforcement (run-start sanitize)


def test_default_trust_accepts_the_launch_folder(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The kill shot for the red error: with everything env-locked-down and
    an EMPTY settings store, a run started from a folder outside the operator
    scope is ACCEPTED — the agent can write where it was started."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    launch = tmp_path / "somewhere" / "else"
    launch.mkdir(parents=True)
    _lockdown_env(monkeypatch, ws=ws, data_dir=tmp_path / "runtime")

    from abstractgateway.routes.gateway import _sanitize_run_workspace_policy

    sanitized = _sanitize_run_workspace_policy(
        {
            "workspace_root": str(launch),
            # Entries under the trusted root ride along (relative to it) …
            "workspace_allowed_paths": [str(launch)],
            # … but trust is NOT full overrides: the escape-mode downgrade stands.
            "workspace_access_mode": "all_except_ignored",
        },
        principal=_principal(),
    )
    assert sanitized["workspace_root"] == str(launch.resolve())
    assert str(launch.resolve()) in str(sanitized.get("workspace_allowed_paths") or "")
    assert sanitized["workspace_access_mode"] == "workspace_only"


def test_trust_never_legitimizes_other_absolute_paths(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    launch = tmp_path / "launch"
    launch.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _lockdown_env(monkeypatch, ws=ws, data_dir=tmp_path / "runtime")

    from fastapi import HTTPException

    from abstractgateway.routes.gateway import _sanitize_run_workspace_policy

    with pytest.raises(HTTPException) as excinfo:
        _sanitize_run_workspace_policy(
            {"workspace_root": str(launch), "workspace_allowed_paths": [str(elsewhere)]},
            principal=_principal(),
        )
    assert excinfo.value.status_code == 400
    assert str(elsewhere.resolve()) in str(excinfo.value.detail)


def test_deny_list_beats_trust(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    blocked = tmp_path / "private"
    blocked.mkdir()
    runtime_dir = tmp_path / "runtime"
    _lockdown_env(monkeypatch, ws=ws, data_dir=runtime_dir)

    from fastapi import HTTPException

    from abstractgateway.routes.gateway import _sanitize_run_workspace_policy
    from abstractgateway.runtime_config import write_runtime_config

    write_runtime_config(runtime_dir, {"workspace_blocked_paths": str(blocked)}, actor="person:admin")

    with pytest.raises(HTTPException) as excinfo:
        _sanitize_run_workspace_policy({"workspace_root": str(blocked)}, principal=_principal())
    assert excinfo.value.status_code == 400
    assert "deny list" in str(excinfo.value.detail)


def test_refusal_teaches_settings_not_env_vars(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    runtime_dir = tmp_path / "runtime"
    _lockdown_env(monkeypatch, ws=ws, data_dir=runtime_dir)

    from fastapi import HTTPException

    from abstractgateway.routes.gateway import _sanitize_run_workspace_policy
    from abstractgateway.runtime_config import write_runtime_config

    write_runtime_config(runtime_dir, {"trust_client_launch_folder": False}, actor="person:admin")

    with pytest.raises(HTTPException) as excinfo:
        _sanitize_run_workspace_policy({"workspace_root": str(outside)}, principal=_principal())
    detail = str(excinfo.value.detail)
    assert "console settings" in detail
    assert "ABSTRACTGATEWAY_" not in detail, "errors must teach settings, not env vars (2026-08-19 ruling)"


def test_per_user_trust_override_beats_the_global_knob(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    launch = tmp_path / "launch"
    launch.mkdir()
    runtime_dir = tmp_path / "runtime"
    _lockdown_env(monkeypatch, ws=ws, data_dir=runtime_dir)

    from fastapi import HTTPException

    from abstractgateway.routes.gateway import _sanitize_run_workspace_policy
    from abstractgateway.runtime_config import write_runtime_config

    # Global OFF, alice ON: alice's launch folder is accepted, carol's refuses.
    write_runtime_config(
        runtime_dir,
        {
            "trust_client_launch_folder": False,
            "user_workspace_policies": {"alice": {"trust_client_launch_folder": True}},
        },
        actor="person:admin",
    )
    ok = _sanitize_run_workspace_policy({"workspace_root": str(launch)}, principal=_principal("alice"))
    assert ok["workspace_root"] == str(launch.resolve())
    with pytest.raises(HTTPException):
        _sanitize_run_workspace_policy({"workspace_root": str(launch)}, principal=_principal("carol"))

    # Global ON (default), bob explicitly OFF: bob is the one refused.
    write_runtime_config(
        runtime_dir,
        {
            "trust_client_launch_folder": None,
            "user_workspace_policies": {"bob": {"trust_client_launch_folder": False}},
        },
        actor="person:admin",
    )
    ok2 = _sanitize_run_workspace_policy({"workspace_root": str(launch)}, principal=_principal("carol"))
    assert ok2["workspace_root"] == str(launch.resolve())
    with pytest.raises(HTTPException):
        _sanitize_run_workspace_policy({"workspace_root": str(launch)}, principal=_principal("bob"))


def test_per_user_allowed_and_blocked_roots_apply(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "workspace"
    ws.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    runtime_dir = tmp_path / "runtime"
    _lockdown_env(monkeypatch, ws=ws, data_dir=runtime_dir)

    from fastapi import HTTPException

    from abstractgateway.routes.gateway import _sanitize_run_workspace_policy
    from abstractgateway.runtime_config import write_runtime_config

    write_runtimeconfig_args = {
        "trust_client_launch_folder": False,
        "user_workspace_policies": {
            "alice": {"workspace_allowed_paths": [str(proj)]},
            "bob": {"workspace_blocked_paths": [str(proj)]},
        },
    }
    write_runtime_config(runtime_dir, write_runtimeconfig_args, actor="person:admin")

    # alice: proj is an extra allowed root (trust globally off).
    ok = _sanitize_run_workspace_policy({"workspace_root": str(proj)}, principal=_principal("alice"))
    assert ok["workspace_root"] == str(proj.resolve())
    # carol has no such grant.
    with pytest.raises(HTTPException):
        _sanitize_run_workspace_policy({"workspace_root": str(proj)}, principal=_principal("carol"))
    # bob: proj is per-user BLOCKED — the deny list beats even default trust.
    write_runtime_config(runtime_dir, {"trust_client_launch_folder": None}, actor="person:admin")
    with pytest.raises(HTTPException) as excinfo:
        _sanitize_run_workspace_policy({"workspace_root": str(proj)}, principal=_principal("bob"))
    assert "deny list" in str(excinfo.value.detail)


def test_blacklist_mode_allows_everything_except_the_deny_lists(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The user-chosen ALLOW-posture: any root is accepted, all_except_ignored
    survives, and the deny lists ride into the run's own tool sandbox — but a
    denied root still refuses, in every mode."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    anywhere = tmp_path / "totally" / "unrelated"
    anywhere.mkdir(parents=True)
    private = tmp_path / "private"
    private.mkdir()
    runtime_dir = tmp_path / "runtime"
    _lockdown_env(monkeypatch, ws=ws, data_dir=runtime_dir)

    from fastapi import HTTPException

    from abstractgateway.routes.gateway import _sanitize_run_workspace_policy
    from abstractgateway.runtime_config import write_runtime_config

    write_runtime_config(
        runtime_dir,
        {
            "trust_client_launch_folder": False,  # trust plays no part here
            "user_workspace_policies": {
                "alice": {"mode": "blacklist", "workspace_blocked_paths": [str(private)]}
            },
        },
        actor="person:admin",
    )

    sanitized = _sanitize_run_workspace_policy(
        {"workspace_root": str(anywhere), "workspace_access_mode": "all_except_ignored"},
        principal=_principal("alice"),
    )
    assert sanitized["workspace_root"] == str(anywhere.resolve())
    assert sanitized["workspace_access_mode"] == "all_except_ignored"
    # The deny lists bind the run's own sandbox, not just this check.
    assert str(private.resolve()) in str(sanitized.get("workspace_ignored_paths") or "")

    # No client-stated mode: the posture supplies it.
    defaulted = _sanitize_run_workspace_policy(
        {"workspace_root": str(anywhere)}, principal=_principal("alice")
    )
    assert defaulted["workspace_access_mode"] == "all_except_ignored"

    # The deny list still refuses — allow-everything never includes it.
    with pytest.raises(HTTPException) as excinfo:
        _sanitize_run_workspace_policy(
            {"workspace_root": str(private)}, principal=_principal("alice")
        )
    assert "deny list" in str(excinfo.value.detail)

    # carol stays whitelist-mode: the same root refuses for her.
    with pytest.raises(HTTPException):
        _sanitize_run_workspace_policy(
            {"workspace_root": str(anywhere)}, principal=_principal("carol")
        )


# ------------------------------------------------- end-to-end (runs/start)


def _write_min_bundle(*, bundles_dir: Path, bundle_id: str, flow_id: str) -> None:
    bundles_dir.mkdir(parents=True, exist_ok=True)
    flow = {
        "id": flow_id,
        "name": "minimal",
        "description": "",
        "interfaces": [],
        "nodes": [
            {
                "id": "node-1",
                "type": "on_flow_start",
                "position": {"x": 32.0, "y": 128.0},
                "data": {"nodeType": "on_flow_start", "label": "On Flow Start", "inputs": [], "outputs": [{"id": "exec-out", "label": "", "type": "execution"}]},
            },
            {
                "id": "node-2",
                "type": "on_flow_end",
                "position": {"x": 288.0, "y": 128.0},
                "data": {"nodeType": "on_flow_end", "label": "On Flow End", "inputs": [{"id": "exec-in", "label": "", "type": "execution"}], "outputs": []},
            },
        ],
        "edges": [{"id": "e1", "source": "node-1", "sourceHandle": "exec-out", "target": "node-2", "targetHandle": "exec-in"}],
        "entryNode": "node-1",
    }
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": bundle_id,
        "bundle_version": "0.0.0",
        "created_at": "2026-01-19T00:00:00+00:00",
        "entrypoints": [{"flow_id": flow_id, "name": "root", "description": "", "interfaces": []}],
        "flows": {flow_id: f"flows/{flow_id}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }
    bundle_path = bundles_dir / f"{bundle_id}.flow"
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr(f"flows/{flow_id}.json", json.dumps(flow, indent=2))


def test_run_start_accepts_launch_folder_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: /runs/start from a folder outside every configured root
    returns 200 and the run lives in that folder — the exact request that
    used to die with the red 400."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    launch = tmp_path / "wherever-the-agent-started"
    launch.mkdir()
    runtime_dir = tmp_path / "runtime"
    bundles_dir = tmp_path / "bundles"
    _write_min_bundle(bundles_dir=bundles_dir, bundle_id="bundle-trust", flow_id="root")

    _lockdown_env(monkeypatch, ws=ws, data_dir=runtime_dir)
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(bundles_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    from abstractgateway.app import app

    with TestClient(app) as client:
        headers = {"Authorization": "Bearer t"}
        start = client.post(
            "/api/gateway/runs/start",
            json={
                "bundle_id": "bundle-trust",
                "flow_id": "root",
                "input_data": {"workspace_root": str(launch)},
            },
            headers=headers,
        )
        assert start.status_code == 200, start.text


def test_self_service_policy_endpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GET/PUT /workspace/policy/self: any authenticated principal manages
    their OWN posture; the admin-classed scope-overrides grant refuses."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    runtime_dir = tmp_path / "runtime"
    _lockdown_env(monkeypatch, ws=ws, data_dir=runtime_dir)
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    from abstractgateway.app import app

    with TestClient(app) as client:
        headers = {"Authorization": "Bearer t"}
        r = client.get("/api/gateway/workspace/policy/self", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["effective"]["mode"] == "whitelist"
        assert body["effective"]["trust_client_launch_folder"] is True

        w = client.put(
            "/api/gateway/workspace/policy/self",
            json={"mode": "blacklist", "workspace_blocked_paths": [str(proj)]},
            headers=headers,
        )
        assert w.status_code == 200, w.text
        assert w.json()["effective"]["mode"] == "blacklist"

        # Round-trips through a fresh GET (persisted, not echoed).
        r2 = client.get("/api/gateway/workspace/policy/self", headers=headers)
        assert r2.json()["effective"]["mode"] == "blacklist"
        assert r2.json()["effective"]["workspace_blocked_paths"] == [str(proj.resolve())]

        # The operator-classed grant refuses loudly on the self lane.
        deny = client.put(
            "/api/gateway/workspace/policy/self",
            json={"client_workspace_scope_overrides": True},
            headers=headers,
        )
        assert deny.status_code == 400
        assert "admin" in deny.json()["detail"]

        # Bad mode: the store's validation surfaces as a 400.
        bad = client.put(
            "/api/gateway/workspace/policy/self",
            json={"mode": "wide-open"},
            headers=headers,
        )
        assert bad.status_code == 400, bad.text

        # Clear: back to inherited.
        clear = client.put("/api/gateway/workspace/policy/self", json={}, headers=headers)
        assert clear.status_code == 200, clear.text
        assert clear.json()["effective"]["mode"] == "whitelist"


def test_admin_single_entry_policy_endpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-runtime modal's lane: GET/PUT one user's entry. Single-entry
    semantics — a write never touches other users' entries (adversary B2) —
    and an unknown target 404s instead of minting a dead entry (S2)."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    runtime_dir = tmp_path / "runtime"
    _lockdown_env(monkeypatch, ws=ws, data_dir=runtime_dir)
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")

    from abstractgateway.runtime_config import read_runtime_config, write_runtime_config

    # A pre-existing OTHER user's entry that must survive every write below.
    write_runtime_config(
        runtime_dir,
        {"user_workspace_policies": {"survivor": {"mode": "blacklist"}}},
        actor="person:admin",
    )

    from abstractgateway.app import app

    with TestClient(app) as client:
        headers = {"Authorization": "Bearer t"}
        # default:admin is the static-token operator — targetable without a
        # registry record.
        r = client.get(
            "/api/gateway/admin/user-workspace-policy",
            params={"tenant_id": "default", "user_id": "admin"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["customized"] is False

        w = client.put(
            "/api/gateway/admin/user-workspace-policy?tenant_id=default&user_id=admin",
            json={"policy": {"mode": "blacklist", "workspace_blocked_paths": [str(proj)]}},
            headers=headers,
        )
        assert w.status_code == 200, w.text
        assert w.json()["customized"] is True
        assert w.json()["effective"]["mode"] == "blacklist"

        # Unknown principal: refused, no dead entry minted.
        missing = client.put(
            "/api/gateway/admin/user-workspace-policy?tenant_id=default&user_id=nobody-here",
            json={"policy": {"mode": "whitelist"}},
            headers=headers,
        )
        assert missing.status_code == 404, missing.text

        # Clear the admin's entry again.
        clear = client.put(
            "/api/gateway/admin/user-workspace-policy?tenant_id=default&user_id=admin",
            json={"policy": None},
            headers=headers,
        )
        assert clear.status_code == 200, clear.text
        assert clear.json()["customized"] is False

    # The survivor's entry was never touched by any of the writes above.
    policies = read_runtime_config(runtime_dir)["user_workspace_policies"]["value"]
    assert set(policies) == {"default:survivor"}


def test_self_write_preserves_admin_classed_grant(tmp_path) -> None:
    """Adversary B4: a user saving their own card must never erase the
    admin-set scope-overrides grant on their entry."""
    from abstractgateway.runtime_config import (
        read_user_workspace_policy,
        write_user_workspace_policy,
    )

    # Admin grants alice scope overrides (admin lane: no preserve needed).
    write_user_workspace_policy(
        tmp_path,
        tenant_id="default",
        user_id="alice",
        policy={"client_workspace_scope_overrides": True, "mode": "whitelist"},
        actor="person:admin",
    )
    # Alice re-saves her own posture through the self lane (which refuses
    # the grant field in its body, so it is absent here).
    out = write_user_workspace_policy(
        tmp_path,
        tenant_id="default",
        user_id="alice",
        policy={"mode": "blacklist"},
        actor="person:alice",
        preserve_fields=("client_workspace_scope_overrides",),
    )
    assert out["policy"]["client_workspace_scope_overrides"] is True
    assert out["policy"]["mode"] == "blacklist"
    # Effective trust reflects the grant (overrides imply trust — B5a).
    assert out["effective"]["trust_client_launch_folder"] is True
    assert out["effective"]["client_workspace_scope_overrides"] is True

    # Even a full self-clear keeps the admin's grant on the entry.
    cleared = write_user_workspace_policy(
        tmp_path,
        tenant_id="default",
        user_id="alice",
        policy=None,
        actor="person:alice",
        preserve_fields=("client_workspace_scope_overrides",),
    )
    assert cleared["policy"] == {"client_workspace_scope_overrides": True}
    admin_view = read_user_workspace_policy(tmp_path, tenant_id="default", user_id="alice")
    assert admin_view["effective"]["client_workspace_scope_overrides"] is True
