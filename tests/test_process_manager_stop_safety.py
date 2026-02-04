from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.basic
def test_pid_commandline_uses_wide_ps(monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.maintenance.process_manager as pm

    calls: list[list[str]] = []

    def _fake_run(args, **kwargs):
        calls.append(list(args))
        return SimpleNamespace(stdout="python -c pass\n", returncode=0)

    monkeypatch.setattr(pm.subprocess, "run", _fake_run)

    out = pm._pid_commandline(12345)
    assert out == "python -c pass"
    assert calls
    assert calls[0][0] == "ps"
    assert "-ww" in calls[0]
    assert "-p" in calls[0]


@pytest.mark.basic
def test_default_process_specs_pin_uat_env_defaults(tmp_path: Path) -> None:
    from abstractgateway.maintenance.process_manager import default_process_specs

    specs = default_process_specs(repo_root=tmp_path)
    for pid, expected in [
        ("gateway_uat", {"ABSTRACTGATEWAY_UAT_PORT": "6081", "ABSTRACTGATEWAY_UAT_DATA_DIR": "runtime/gateway_uat"}),
        ("abstractobserver_uat", {"ABSTRACTOBSERVER_UAT_PORT": "6082"}),
        ("abstractcode_web_uat", {"ABSTRACTCODE_WEB_UAT_PORT": "6083"}),
        ("abstractflow_frontend_uat", {"ABSTRACTFLOW_FRONTEND_UAT_PORT": "6084", "ABSTRACTFLOW_BACKEND_UAT_PORT": "6080"}),
        ("abstractflow_backend_uat", {"ABSTRACTFLOW_BACKEND_UAT_PORT": "6080", "ABSTRACTFLOW_RUNTIME_DIR": "runtime/abstractflow_uat"}),
    ]:
        spec = specs.get(pid)
        assert spec is not None
        env = spec.env or {}
        for k, v in expected.items():
            assert env.get(k) == v

