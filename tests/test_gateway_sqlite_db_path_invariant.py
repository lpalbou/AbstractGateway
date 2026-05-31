from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.basic
def test_gateway_host_config_rejects_sqlite_db_outside_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from abstractgateway.config import GatewayHostConfig

    uat = tmp_path / "gateway_uat"
    prod = tmp_path / "gateway"
    uat.mkdir()
    prod.mkdir()

    monkeypatch.setenv("ABSTRACTGATEWAY_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(uat))
    monkeypatch.setenv("ABSTRACTGATEWAY_DB_PATH", str(prod / "gateway.sqlite3"))

    with pytest.raises(SystemExit) as e:
        GatewayHostConfig.from_env()
    assert "ABSTRACTGATEWAY_DB_PATH must point to a file under ABSTRACTGATEWAY_DATA_DIR" in str(e.value)


@pytest.mark.basic
def test_gateway_host_config_defaults_to_shipped_basic_agent_bundles(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from abstractgateway.config import GatewayHostConfig

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("ABSTRACTGATEWAY_FLOWS_DIR", raising=False)
    monkeypatch.delenv("ABSTRACTFRAMEWORK_WORKFLOWS_DIR", raising=False)
    monkeypatch.delenv("ABSTRACTFLOW_FLOWS_DIR", raising=False)

    cfg = GatewayHostConfig.from_env()

    assert cfg.flows_dir.name == "bundles"
    assert (cfg.flows_dir / "basic-agent.flow").is_file() or (cfg.flows_dir / "basic-agent@0.0.1.flow").is_file()


@pytest.mark.basic
def test_build_sqlite_stores_rejects_db_outside_base_dir(tmp_path: Path) -> None:
    from abstractgateway.stores import build_sqlite_stores

    base = tmp_path / "base"
    other = tmp_path / "other"
    base.mkdir()
    other.mkdir()

    with pytest.raises(ValueError) as e:
        build_sqlite_stores(base_dir=base, db_path=other / "gateway.sqlite3")
    assert "db_path must be under base_dir" in str(e.value)
