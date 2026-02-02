from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_repo_root_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Prevent accidental writes to a developer’s real repo when running tests in an
    # environment where the gateway is configured for backlog browsing/triage.
    monkeypatch.delenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", raising=False)
    monkeypatch.delenv("ABSTRACT_TRIAGE_REPO_ROOT", raising=False)

