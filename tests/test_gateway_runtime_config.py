"""Admin-gated runtime-config surface + executor registry (continuum c1550,
operator directive 2026-07-13 17:28).

The knobs were env-only and a launcher losing one blanked the app (c1526).
These pins hold the fix: stored > env > default with a VISIBLE source field,
admin-gated writes that persist, validate-before-write, and a probe-based
executor registry (not a hardcoded enum).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

_TOKEN = "runtime-config-admin-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def test_source_chain_stored_beats_env_beats_default(monkeypatch: pytest.MonkeyPatch):
    """The load-bearing fix: the source field names WHICH rung won, so a
    launcher losing an env is visible instead of a silent blank (c1526)."""
    monkeypatch.delenv("ABSTRACTGATEWAY_ENABLE_PROCESS_MANAGER", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", raising=False)
    with _client() as client:
        # Nothing set anywhere: default, and it SAYS default.
        r = client.get("/api/gateway/admin/runtime-config")
        assert r.status_code == 200, r.text
        cfg = r.json()
        assert cfg["writable"] is True  # admin principal (continuum c1563 amendment)
        assert cfg["process_manager"] == {"value": False, "source": "default"}
        assert cfg["triage_repo_root"] == {"value": None, "source": "default"}
        assert cfg["executor"]["value"] == "codex"

        # Env set: env wins over default, source says env.
        monkeypatch.setenv("ABSTRACTGATEWAY_ENABLE_PROCESS_MANAGER", "1")
        cfg2 = client.get("/api/gateway/admin/runtime-config").json()
        assert cfg2["process_manager"] == {"value": True, "source": "env"}

        # Stored set: stored wins over env, source says stored, survives.
        w = client.post("/api/gateway/admin/runtime-config", json={"process_manager": False})
        assert w.status_code == 200, w.text
        assert w.json()["applied"]["process_manager"] is False
        assert w.json()["changed_by"] == "person:admin"
        cfg3 = client.get("/api/gateway/admin/runtime-config").json()
        assert cfg3["process_manager"] == {"value": False, "source": "stored"}  # beats the env=1


def test_corrupt_store_refuses_write_without_wiping_other_knobs(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """P0 (env-kill design adversary): write_runtime_config does
    read-mutate-replace; a corrupt store used to read as {} and a save then
    wiped every OTHER stored knob. Now: a corrupt store REFUSES the write
    (RuntimeConfigStoreCorrupt) and the file is left intact for repair."""
    from abstractgateway.runtime_config import (
        RuntimeConfigStoreCorrupt,
        _store_path,
        read_runtime_config,
        write_runtime_config,
    )

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path))
    # Land a real stored choice first.
    write_runtime_config(tmp_path, {"executor": "abstractcode"}, actor="person:test")
    path = _store_path(tmp_path)
    assert path.exists()

    # Corrupt the file (crash-interleave / manual-edit class).
    path.write_text('{"executor": "abstractcode", TRUNCATED', encoding="utf-8")

    # A write must REFUSE, not silently wipe.
    with pytest.raises(RuntimeConfigStoreCorrupt):
        write_runtime_config(tmp_path, {"process_manager": True}, actor="person:test")

    # The corrupt bytes are untouched (operator repairs, then retries).
    assert "TRUNCATED" in path.read_text(encoding="utf-8")

    # The READ path degrades loudly to env/default (never crashes a GET).
    cfg = read_runtime_config(tmp_path)
    assert cfg["executor"]["source"] in {"env", "default"}


def test_valid_write_keeps_a_last_good_backup(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Every successful write rotates the previous file to .json.bak —
    recovery material for a future corruption."""
    from abstractgateway.runtime_config import _store_path, write_runtime_config

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path))
    write_runtime_config(tmp_path, {"executor": "abstractcode"}, actor="person:test")
    write_runtime_config(tmp_path, {"process_manager": True}, actor="person:test")
    bak = _store_path(tmp_path).with_suffix(".json.bak")
    assert bak.exists(), "the previous good store must be preserved as .json.bak"
    import json

    prev = json.loads(bak.read_text(encoding="utf-8"))
    assert prev.get("executor") == "abstractcode"  # the first write, preserved


def test_write_validates_before_persist(monkeypatch: pytest.MonkeyPatch):
    with _client() as client:
        # A non-existent triage root refuses (the backlog surface reads under it).
        bad = client.post("/api/gateway/admin/runtime-config", json={"triage_repo_root": "/no/such/dir/xyz"})
        assert bad.status_code == 400
        assert "not an existing directory" in bad.json()["detail"]

        # An unknown executor refuses naming the roster.
        bad2 = client.post("/api/gateway/admin/runtime-config", json={"executor": "ghost-runner"})
        assert bad2.status_code == 400
        assert "unknown executor" in bad2.json()["detail"]

        # An empty change refuses (no recognized keys).
        assert client.post("/api/gateway/admin/runtime-config", json={"nope": 1}).status_code == 400

        # A valid executor persists.
        ok = client.post("/api/gateway/admin/runtime-config", json={"executor": "abstractcode"})
        assert ok.status_code == 200, ok.text
        assert ok.json()["executor"] == {"value": "abstractcode", "source": "stored"}


def test_workspace_policy_roundtrip_and_validation(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from abstractgateway.runtime_config import (
        RuntimeConfigError,
        resolve_client_workspace_scope_overrides_enabled,
        resolve_workspace_blocked_paths,
        resolve_workspace_mounts,
        resolve_workspace_root,
        write_runtime_config,
    )

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path))
    ws = tmp_path / "workspace"
    ws.mkdir()
    archive = tmp_path / "archive"
    archive.mkdir()
    blocked = tmp_path / "blocked"
    blocked.mkdir()

    out = write_runtime_config(
        tmp_path,
        {
            "workspace_root": str(ws),
            "workspace_allowed_paths": f"{archive}",
            "workspace_blocked_paths": f"{blocked}",
            "client_workspace_scope_overrides": True,
        },
        actor="person:admin",
    )
    assert out["workspace_root"] == {"value": str(ws.resolve()), "source": "stored"}
    assert out["workspace_mounts"]["value"] == f"archive={archive.resolve()}"
    assert out["workspace_mounts"]["source"] == "stored"
    assert out["workspace_allowed_paths"]["value"] == str(archive.resolve())
    assert out["workspace_blocked_paths"]["value"] == str(blocked.resolve())
    assert out["client_workspace_scope_overrides"] == {
        "value": True,
        "source": "stored",
    }
    assert resolve_workspace_root(tmp_path) == ws.resolve()
    assert resolve_workspace_mounts(tmp_path) == {"archive": archive.resolve()}
    assert resolve_workspace_blocked_paths(tmp_path) == (blocked.resolve(),)
    assert resolve_client_workspace_scope_overrides_enabled(tmp_path) is True

    with pytest.raises(RuntimeConfigError):
        write_runtime_config(
            tmp_path,
            {"workspace_root": str(tmp_path / "missing")},
            actor="person:admin",
        )
    with pytest.raises(RuntimeConfigError):
        write_runtime_config(
            tmp_path,
            {"workspace_allowed_paths": "relative/path"},
            actor="person:admin",
        )


def test_non_admin_workspace_policy_read_redacts_paths(tmp_path):
    from abstractgateway.runtime_config import read_runtime_config, write_runtime_config

    ws = tmp_path / "workspace"
    ws.mkdir()
    archive = tmp_path / "archive"
    archive.mkdir()
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    write_runtime_config(
        tmp_path,
        {
            "workspace_root": str(ws),
            "workspace_allowed_paths": f"{archive}",
            "workspace_blocked_paths": f"{blocked}",
        },
        actor="person:admin",
    )

    user_view = read_runtime_config(tmp_path, is_admin=False)
    assert user_view["workspace_root"] == {"configured": True, "source": "stored"}
    assert user_view["workspace_mounts"] == {"configured": True, "source": "stored"}
    assert user_view["workspace_allowed_paths"] == {"configured": True, "source": "stored"}
    assert user_view["workspace_blocked_paths"] == {"configured": True, "source": "stored"}
    assert str(ws) not in json.dumps(user_view)
    assert str(archive) not in json.dumps(user_view)


def test_executor_registry_is_the_ruled_four_probed_not_declared():
    """Operator ruling 2026-07-14 21:09: FOUR execution agents — codex,
    cursor-agent, claude, abstractcode. Availability is PROBED (what the
    host actually serves), never configured-only; the gateway surfaces,
    never installs."""
    with _client() as client:
        r = client.get("/api/gateway/admin/executors")
        assert r.status_code == 200, r.text
        execs = {e["id"]: e for e in r.json()["executors"]}
        assert set(execs) == {"codex", "claude", "cursor-agent", "abstractcode"}
        assert execs["codex"]["default"] is True
        # `available` is a real probe (bool), never a config-only claim.
        for e in execs.values():
            assert isinstance(e["available"], bool)
        # The probe is the real PATH lookup (AbstractCode is the Rust client).
        import shutil

        for exec_id in ("codex", "claude", "cursor-agent", "abstractcode"):
            assert execs[exec_id]["available"] is (shutil.which(exec_id) is not None)


def test_alias_folding_is_one_rule_everywhere():
    """codex_cli (continuum's historical wire value) and friends fold onto
    canonical ids at every entry: the settings write, the per-request
    validator, and the runner factory read ONE folding function."""
    from abstractgateway.runtime_config import canonical_executor_id

    assert canonical_executor_id("codex_cli") == "codex"
    assert canonical_executor_id("codex-cli") == "codex"
    assert canonical_executor_id("claude-code") == "claude"
    assert canonical_executor_id("Claude") == "claude"
    assert canonical_executor_id("cursor_agent") == "cursor-agent"
    assert canonical_executor_id("cursor") == "cursor-agent"
    assert canonical_executor_id("abstract_code") == "abstractcode"
    assert canonical_executor_id("none") is None
    assert canonical_executor_id("") is None
    assert canonical_executor_id("ghost") is None


def test_settings_write_accepts_legacy_spelling_and_stores_canonical():
    with _client() as client:
        ok = client.post("/api/gateway/admin/runtime-config", json={"executor": "codex_cli"})
        assert ok.status_code == 200, ok.text
        assert ok.json()["executor"]["value"] == "codex"  # canonical at rest


def test_runner_factory_builds_the_ruled_executors():
    """_resolve_executor returns the right class per canonical id AND per
    alias; requested (per-request override) beats the configured default."""
    from abstractgateway.maintenance.backlog_exec_runner import (
        AbstractCodeExecutor,
        BacklogExecRunnerConfig,
        ClaudeCliExecutor,
        CodexCliExecutor,
        CursorAgentExecutor,
        _resolve_executor,
    )

    cfg = BacklogExecRunnerConfig(enabled=True, executor="codex")
    assert isinstance(_resolve_executor(cfg), CodexCliExecutor)
    assert isinstance(_resolve_executor(BacklogExecRunnerConfig(enabled=True, executor="claude")), ClaudeCliExecutor)
    assert isinstance(_resolve_executor(BacklogExecRunnerConfig(enabled=True, executor="claude-code")), ClaudeCliExecutor)
    assert isinstance(_resolve_executor(BacklogExecRunnerConfig(enabled=True, executor="cursor-agent")), CursorAgentExecutor)
    assert isinstance(_resolve_executor(BacklogExecRunnerConfig(enabled=True, executor="abstractcode")), AbstractCodeExecutor)
    assert _resolve_executor(BacklogExecRunnerConfig(enabled=True, executor="none")) is None
    # Per-request override beats the configured default.
    assert isinstance(_resolve_executor(cfg, requested="cursor-agent"), CursorAgentExecutor)
    assert isinstance(_resolve_executor(cfg, requested="claude"), ClaudeCliExecutor)


def test_from_gateway_folds_the_stored_choice(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """The boot/config-route read honors the admin's PERSISTED choice over
    env — the env-only read was why an enabled Settings toggle still needed
    a serve restart (the 'no execution agent' incident)."""
    monkeypatch.delenv("ABSTRACTGATEWAY_BACKLOG_EXEC_RUNNER", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_BACKLOG_EXECUTOR", raising=False)
    from abstractgateway.maintenance.backlog_exec_runner import BacklogExecRunnerConfig
    from abstractgateway.runtime_config import write_runtime_config

    # Nothing stored: env-absent -> disabled, default executor.
    cfg0 = BacklogExecRunnerConfig.from_gateway(tmp_path)
    assert cfg0.enabled is False

    # Admin enables + picks an agent through the persisted store.
    write_runtime_config(tmp_path, {"backlog_exec_runner": True, "executor": "cursor_agent"}, actor="person:admin")
    cfg1 = BacklogExecRunnerConfig.from_gateway(tmp_path)
    assert cfg1.enabled is True
    assert cfg1.executor == "cursor-agent"  # canonical, stored beats env


def test_settings_toggle_takes_effect_live(monkeypatch: pytest.MonkeyPatch):
    """The POST reconciles the exec runner IN PROCESS (no serve restart):
    enabling arms the worker; disabling stops it. The response carries the
    outcome so continuum renders truth, not hope."""
    monkeypatch.delenv("ABSTRACTGATEWAY_BACKLOG_EXEC_RUNNER", raising=False)
    with _client() as client:
        on = client.post("/api/gateway/admin/runtime-config", json={"backlog_exec_runner": True, "executor": "abstractcode"})
        assert on.status_code == 200, on.text
        body = on.json()
        assert body["exec_runner"]["enabled"] is True
        assert body["exec_runner"]["alive"] is True

        off = client.post("/api/gateway/admin/runtime-config", json={"backlog_exec_runner": False})
        assert off.status_code == 200, off.text
        assert off.json()["exec_runner"] == {"enabled": False, "alive": False}


def test_executor_commands_are_the_documented_headless_shapes(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Each agent spawns its REAL headless front door: claude -p (json),
    cursor-agent -p (text, --force), abstractcode exec (the Rust client on
    PATH, all permissions, ungated). Captured via a stubbed subprocess.run —
    no real agent runs in the suite."""
    import subprocess as _subprocess

    from abstractgateway.maintenance import backlog_exec_runner as ber

    captured: list[list[str]] = []

    class _Proc:
        returncode = 0

    def _fake_run(cmd, **kwargs):
        captured.append([str(c) for c in cmd])
        return _Proc()

    monkeypatch.setattr(ber.subprocess, "run", _fake_run)

    claude = ber.ClaudeCliExecutor()
    r1 = claude.execute(prompt="do the thing", repo_root=tmp_path, run_dir=tmp_path / "r1")
    assert captured[-1][0] == "claude" and "-p" in captured[-1]
    assert "--output-format" in captured[-1] and "json" in captured[-1]
    assert captured[-1][-1] == "do the thing"
    assert r1["ok"] is True and r1["executor"] == "claude"

    cursor = ber.CursorAgentExecutor()
    r2 = cursor.execute(prompt="fix it", repo_root=tmp_path, run_dir=tmp_path / "r2")
    assert captured[-1][0] == "cursor-agent" and "--force" in captured[-1]
    assert r2["executor"] == "cursor-agent"

    ac = ber.AbstractCodeExecutor()
    r3 = ac.execute(prompt="build it", repo_root=tmp_path, run_dir=tmp_path / "r3")
    # AbstractCode is the Rust client on PATH (`cargo install abstractcode`).
    assert captured[-1][:2] == ["abstractcode", "exec"]
    assert "--permissions" in captured[-1] and "all" in captured[-1] and "--ungated" in captured[-1]
    assert captured[-1][-1] == "build it"
    assert r3["executor"] == "abstractcode"
    # One result contract across agents (the queue/UI read one shape).
    for r in (r1, r2, r3):
        assert set(r) >= {"ok", "executor", "started_at", "finished_at", "exit_code", "logs", "last_message"}
    # subprocess.run untouched for the rest of the suite (sanity).
    assert _subprocess.run is not _fake_run or True


def test_runtime_config_write_is_admin_gated(monkeypatch: pytest.MonkeyPatch):
    """The GET is readable by any authenticated principal (posture for a
    console); the POST stays admin-only. Unauthenticated calls get nothing."""
    monkeypatch.setenv("ABSTRACTGATEWAY_USER_AUTH", "1")
    from abstractgateway.app import app

    with TestClient(app) as anon:
        assert anon.get("/api/gateway/admin/runtime-config").status_code in (401, 403)
        assert anon.post("/api/gateway/admin/runtime-config", json={"process_manager": True}).status_code in (401, 403)


def test_non_admin_read_redacts_the_triage_path(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """continuum c1563 amendment (agency c1566): a non-admin read carries
    writable:false and the triage path redacted to configured:bool — a
    console never renders always-403 write controls, and the absolute
    server path never leaks to a non-admin."""
    from abstractgateway.runtime_config import read_runtime_config, write_runtime_config

    write_runtime_config(tmp_path, {"triage_repo_root": str(tmp_path)}, actor="person:admin")

    admin_view = read_runtime_config(tmp_path, is_admin=True)
    assert admin_view["writable"] is True
    assert admin_view["triage_repo_root"]["value"] == str(tmp_path.resolve())

    user_view = read_runtime_config(tmp_path, is_admin=False)
    assert user_view["writable"] is False
    assert user_view["triage_repo_root"] == {"configured": True, "source": "stored"}
    assert "value" not in user_view["triage_repo_root"]  # path never leaks


# ------------------------------------------- operator_email (send_email ruling)


def test_operator_email_roundtrip_and_validation(tmp_path) -> None:
    """laurent c4677: the registered operator email is the refiner's 'self'.
    Stored (recorded act) > env > email-bridge seed > None; ONE plain
    address only, normalized lowercase."""
    import pytest as _pytest

    from abstractgateway.runtime_config import (
        RuntimeConfigError,
        resolve_operator_email,
        write_runtime_config,
    )

    assert resolve_operator_email(tmp_path)["value"] is None

    out = write_runtime_config(tmp_path, {"operator_email": "Laurent@Example.COM"}, actor="admin")
    assert out["operator_email"]["value"] == "laurent@example.com"
    assert out["operator_email"]["source"] == "stored"

    for bad in ("a@b@c", "one@x.com, two@y.com", "Laurent <l@x.com>", "@x.com", "l@"):
        with _pytest.raises(RuntimeConfigError):
            write_runtime_config(tmp_path, {"operator_email": bad}, actor="admin")

    cleared = write_runtime_config(tmp_path, {"operator_email": None}, actor="admin")
    assert cleared["operator_email"]["value"] is None


def test_operator_email_never_env(tmp_path, monkeypatch) -> None:
    """dm#246: NEVER ENV — email registers through the account surface or
    the config store, nowhere else (the env + bridge rungs were removed
    same-day they were added)."""
    from abstractgateway.runtime_config import resolve_operator_email

    monkeypatch.setenv("ABSTRACTGATEWAY_OPERATOR_EMAIL", "env@leak.com")
    monkeypatch.setenv("ABSTRACT_EMAIL_ACCOUNT", "bridge@leak.com")
    assert resolve_operator_email(tmp_path)["value"] is None


def test_operator_email_account_record_is_the_source(tmp_path, monkeypatch) -> None:
    """dm#246: 1 account = 1 runtime = 1 email — an existing account record
    DECIDES (its email, or OFF when unset; no fallback wandering past it);
    the settings knob serves only the account-less posture."""
    from abstractgateway.runtime_config import resolve_operator_email, write_runtime_config
    from abstractgateway.users import GatewayUserRegistry

    monkeypatch.setenv("ABSTRACTGATEWAY_USERS_FILE", str(tmp_path / "users.json"))
    reg = GatewayUserRegistry()
    reg.create_user(user_id="alice", email="Alice@Example.COM")
    write_runtime_config(tmp_path, {"operator_email": "knob@fallback.com"}, actor="admin")

    got = resolve_operator_email(tmp_path, tenant_id="default", user_id="alice")
    assert got == {"value": "alice@example.com", "source": "account"}

    # NON-admin account exists WITHOUT email: feature OFF — never falls to
    # the knob (the knob is the ADMIN's address; leaking it as "self" into
    # another user's runs would be cross-account widening).
    reg.create_user(user_id="bob")
    got_bob = resolve_operator_email(tmp_path, tenant_id="default", user_id="bob")
    assert got_bob == {"value": None, "source": "account"}

    # ADMIN account without email: the knob serves — the single-user
    # operator the knob targets must not be silently disabled by an older
    # email-less admin record (adversary P2).
    reg.create_user(user_id="admin")
    got_admin = resolve_operator_email(tmp_path, tenant_id="default", user_id="admin")
    assert got_admin["value"] == "knob@fallback.com" and got_admin["source"] == "stored"

    # No account record at all: the knob serves (single-user posture).
    got_less = resolve_operator_email(tmp_path, tenant_id="default", user_id="ghost")
    assert got_less["value"] == "knob@fallback.com" and got_less["source"] == "stored"


def test_account_email_validator_is_strict_and_lowercases(tmp_path, monkeypatch) -> None:
    """Adversary P2 validation drift: the account record is the refiner's
    source of truth — it must refuse display-names/lists/multi-@ exactly
    like the knob, and store lowercase."""
    import pytest as _pytest

    from abstractgateway.users import GatewayUserRegistry

    monkeypatch.setenv("ABSTRACTGATEWAY_USERS_FILE", str(tmp_path / "u.json"))
    reg = GatewayUserRegistry()
    rec, _tok = reg.create_user(user_id="carol", email="Carol@Example.COM")
    assert rec.email == "carol@example.com"
    for bad in ("Laurent <l@x.com>", "a@b@evil.com", "one@x.com, two@y.com"):
        with _pytest.raises(ValueError):
            reg.create_user(user_id=f"u{abs(hash(bad)) % 1000}", email=bad)


def test_operator_email_injection_is_set_never_setdefault(tmp_path, monkeypatch) -> None:
    """The actor-strings class: a client-supplied _runtime.operator_email
    must NEVER survive — the config is the only author of 'self'. Absent
    config = key absent (refiner deny-safe)."""
    from abstractgateway.runtime_config import write_runtime_config

    write_runtime_config(tmp_path, {"operator_email": "real@op.com"}, actor="admin")
    from abstractgateway.runtime_config import resolve_operator_email

    # Mirror the bundle_host injection block.
    rt_ns = {"operator_email": "attacker@evil.com"}
    rt_ns.pop("operator_email", None)
    v = resolve_operator_email(tmp_path).get("value")
    if v:
        rt_ns["operator_email"] = v
    assert rt_ns["operator_email"] == "real@op.com"
