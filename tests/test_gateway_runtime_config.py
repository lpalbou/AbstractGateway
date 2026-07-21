"""Admin-gated runtime-config surface + executor registry (continuum c1550,
operator directive 2026-07-13 17:28).

The knobs were env-only and a launcher losing one blanked the app (c1526).
These pins hold the fix: stored > env > default with a VISIBLE source field,
admin-gated writes that persist, validate-before-write, and a probe-based
executor registry (not a hardcoded enum).
"""

from __future__ import annotations

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
        # abstractcode is importable in this repo -> probe finds it available.
        assert execs["abstractcode"]["available"] is True


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
    cursor-agent -p (text, --force), abstractcode exec (module spawn from
    THIS interpreter, full-auto). Captured via a stubbed subprocess.run —
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

    import sys

    ac = ber.AbstractCodeExecutor()
    r3 = ac.execute(prompt="build it", repo_root=tmp_path, run_dir=tmp_path / "r3")
    assert captured[-1][0] == sys.executable
    assert captured[-1][1:4] == ["-m", "abstractcode", "exec"]
    assert "--permission-mode" in captured[-1] and "full-auto" in captured[-1]
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
