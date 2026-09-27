"""The JSON run store's session/children indexes are built at boot (runtime
J51-1), not by the first chat."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_file_stores_warm_the_session_index_at_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractruntime import JsonFileRunStore

    from abstractgateway.stores import build_file_stores

    warmed: list = []
    real = JsonFileRunStore.warm_session_index

    def spy(self):
        warmed.append(self)
        return real(self)

    monkeypatch.setattr(JsonFileRunStore, "warm_session_index", spy)
    stores = build_file_stores(base_dir=tmp_path / "runtime")
    assert len(warmed) == 1 and stores.run_store.inner is warmed[0]


def test_gateway_boot_warms_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractruntime import JsonFileRunStore
    from fastapi.testclient import TestClient

    from automations_fixtures import HEADERS, gateway_env

    gateway_env(monkeypatch, tmp_path)
    warmed: list = []
    real = JsonFileRunStore.warm_session_index

    def spy(self):
        warmed.append(self)
        return real(self)

    monkeypatch.setattr(JsonFileRunStore, "warm_session_index", spy)
    from abstractgateway.app import app

    with TestClient(app) as c:
        assert c.get("/api/gateway/ping", headers=HEADERS).status_code == 200
    assert warmed
