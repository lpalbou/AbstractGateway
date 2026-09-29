from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _managed_test_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """The allowlist is empty in the product since the email variables were retired
    (framework backlog 0992); these tests exercise the mechanism with one test key."""
    import abstractgateway.maintenance.process_manager as pm

    spec = pm.ManagedEnvVarSpec(key="ABSTRACT_TEST_MANAGED_VAR", label="test", description="test variable", category="test")
    spec_cfg = pm.ManagedEnvVarSpec(key="ABSTRACT_TEST_MANAGED_PATH", label="test path", description="test path variable", category="test")
    monkeypatch.setattr(pm, "managed_env_var_allowlist", lambda: {spec.key: spec, spec_cfg.key: spec_cfg})


def _wait_until(predicate, *, timeout_s: float = 4.0, poll_s: float = 0.05) -> None:
    end = time.time() + float(timeout_s)
    while time.time() < end:
        if predicate():
            return
        time.sleep(float(poll_s))
    raise AssertionError("timeout waiting for condition")


@pytest.mark.integration
def test_process_manager_env_overrides_apply_before_spec_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base_dir = tmp_path / "runtime"
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)

    # Baseline env value.
    monkeypatch.setenv("ABSTRACT_TEST_MANAGED_VAR", "baseline@example.com")

    # Persisted override store.
    state_dir = base_dir / "process_manager"
    state_dir.mkdir(parents=True, exist_ok=True)
    env_path = state_dir / "env_overrides.json"
    env_path.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": "now",
                "vars": {
                    "ABSTRACT_TEST_MANAGED_VAR": {
                        "enabled": True,
                        "value": "override@example.com",
                        "updated_at": "now",
                    }
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    from abstractgateway.maintenance.process_manager import ProcessManager, ProcessSpec

    # Process that prints the env var then sleeps so we can tail logs.
    cmd = [
        sys.executable,
        "-c",
        "import os, time; print(os.getenv('ABSTRACT_TEST_MANAGED_VAR',''), flush=True); time.sleep(60)",
    ]

    override_only = ProcessSpec(id="override_only", label="Override only", kind="service", cwd=".", command=cmd, env={})
    override_only.validate()

    spec_wins = ProcessSpec(
        id="spec_wins",
        label="Spec wins",
        kind="service",
        cwd=".",
        command=cmd,
        env={"ABSTRACT_TEST_MANAGED_VAR": "spec@example.com"},
    )
    spec_wins.validate()

    mgr = ProcessManager(base_dir=base_dir, repo_root=repo_root, specs={"override_only": override_only, "spec_wins": spec_wins})

    # override_only should see the override value.
    st = mgr.start("override_only")
    assert st.get("status") == "running"
    try:
        def _has_override() -> bool:
            out = mgr.log_tail("override_only", max_bytes=8000)
            return "override@example.com" in str(out.get("content") or "")

        _wait_until(_has_override)
    finally:
        mgr.stop("override_only")

    # spec_wins should see the spec.env value (wins over overrides).
    st2 = mgr.start("spec_wins")
    assert st2.get("status") == "running"
    try:
        def _has_spec() -> bool:
            out = mgr.log_tail("spec_wins", max_bytes=8000)
            return "spec@example.com" in str(out.get("content") or "")

        _wait_until(_has_spec)
    finally:
        mgr.stop("spec_wins")

