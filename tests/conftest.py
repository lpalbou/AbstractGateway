from __future__ import annotations

from pathlib import Path
import sys

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
    # USER_AUTH=1 flips entity homes to per-principal roots — 6 replay tests
    # fail under a launcher shell that exported it (env-poisoning class;
    # adversary finding, 2026-07-17).
    monkeypatch.delenv("ABSTRACTGATEWAY_USER_AUTH", raising=False)
    # `serve` exports these provenance markers into os.environ (first-run,
    # 2026-09-23); a test that ran serve must not leak them into the next.
    monkeypatch.delenv("ABSTRACTGATEWAY_AUTH_MODE_SOURCE", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_DATA_DIR_SOURCE", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_BIND_HOST", raising=False)
    # AbstractCore's host job registry persists job snapshots under the user's
    # ~/.abstractcore by default; a test that reaches the real registry keeps
    # its jobs in memory instead.
    monkeypatch.setenv("ABSTRACTCORE_JOBS_PERSIST", "0")

    # Provide safe defaults so tests that forget to set these still write only under tmp.
    base = Path(str(tmp_path_factory.mktemp("abstractgateway-test-env")))
    (base / "runtime").mkdir(parents=True, exist_ok=True)
    (base / "flows").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(base / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(base / "flows"))
    # Isolate the MACHINE-LEVEL data registry (~/.abstractframework/
    # data_registry.json): service boot registers data homes
    # (register_gateway_data_homes), so an unisolated suite run pollutes the
    # operator's REAL registry with hundreds of phantom tmp-dir rows (live
    # incident 2026-07-14: 1000+ rows from one evening's suites). Every test
    # writes its own throwaway registry file instead.
    monkeypatch.setenv("ABSTRACTFRAMEWORK_DATA_REGISTRY", str(base / "data_registry.json"))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    # Isolate THE AbstractCore store. Since the operator's one-store ruling
    # (2026-08-01) a Gateway write to a capability default or a provider
    # profile lands in AbstractCore's own config file, so an unisolated suite
    # would edit the developer's real ~/.abstractcore/config/abstractcore.json
    # -- the very store these tests assert about. The path deliberately does
    # NOT exist: "no file at the Core path" is what a fresh install looks like,
    # which is what the seed contract is stated against.
    monkeypatch.setenv("ABSTRACTCORE_CONFIG_FILE", str(base / "abstractcore" / "abstractcore.json"))
    monkeypatch.delenv("ABSTRACTCORE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("ABSTRACTCORE_SERVER_BASE_URL", raising=False)


@pytest.fixture(autouse=True)
def _reset_gateway_service_between_tests(_isolate_gateway_runtime_env: None):
    from abstractgateway.service import stop_gateway_runner

    stop_gateway_runner()
    sys.modules.pop("abstractgateway.app", None)
    yield
    stop_gateway_runner()
    sys.modules.pop("abstractgateway.app", None)
