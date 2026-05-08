from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_repo_root_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Prevent accidental writes to a developer’s real repo when running tests in an
    # environment where the gateway is configured for backlog browsing/triage.
    monkeypatch.delenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", raising=False)
    monkeypatch.delenv("ABSTRACT_TRIAGE_REPO_ROOT", raising=False)


@pytest.fixture(autouse=True)
def _isolate_gateway_runtime_env(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    # Prevent accidental writes to a developer’s real gateway DB/runtime dir when running
    # tests in an environment where `agw.sh` (or similar) exported durable paths.
    monkeypatch.delenv("ABSTRACTGATEWAY_DB_PATH", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_STORE_BACKEND", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_AUTH_TOKEN", raising=False)

    # Provide safe defaults so tests that forget to set these still write only under tmp.
    base = Path(str(tmp_path_factory.mktemp("abstractgateway-test-env")))
    (base / "runtime").mkdir(parents=True, exist_ok=True)
    (base / "flows").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(base / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(base / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")


@pytest.fixture(autouse=True)
def _reset_gateway_service_between_tests(_isolate_gateway_runtime_env: None):
    from abstractgateway.service import stop_gateway_runner

    stop_gateway_runner()
    yield
    stop_gateway_runner()
