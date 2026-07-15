"""Data & Caches writer wave + management view (operator priority 2026-07-13
18:19; agency c1580 ask 1; decision:cache-management-split).

Pins: the gateway registers its data homes (artifacts load-bearing, logs
purgeable, workspaces protected, EVERY entity home safe_to_purge=False by
construction); entity creation registers-at-first-write; /admin/data-homes
serves rows with live sizes (admin-gated); purge refusals propagate the
registry's words VERBATIM (owner-protected rows 409 naming the owner); real
purges require confirm_name; a missing facade degrades LABELED, never
crashes.

BOUNDARY NOTE: gateway source reaches the registry only through the runtime
facade (boundary 0059). These tests shim the facade with core's real
registry primitive (tests may import abstractcore; src may not), so the
behavior is proven end-to-end while runtime ships the facade.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
core_registry = pytest.importorskip("abstractcore.utils.data_registry")

_TOKEN = "data-homes-secret"


class _FacadeShim:
    """What runtime's data_registry_facade will re-export (exact contract
    posted to the runtime seat): three callables, core semantics verbatim."""

    ensure_data_home_registered = staticmethod(core_registry.ensure_data_home_registered)
    list_data_homes = staticmethod(core_registry.list_data_homes)
    purge_data_home = staticmethod(core_registry.purge_data_home)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    # Isolated machine registry per test + fresh per-process ensure state.
    monkeypatch.setenv("ABSTRACTFRAMEWORK_DATA_REGISTRY", str(tmp_path / "machine" / "data_registry.json"))
    core_registry._reset_ensure_state()

    import abstractgateway.data_homes as dh

    monkeypatch.setattr(dh, "_facade", lambda: (_FacadeShim, None))


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _spark(name: str) -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    doc = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    doc["name"] = name
    return doc


def test_writer_wave_registers_gateway_homes(tmp_path) -> None:
    from abstractgateway.data_homes import register_gateway_data_homes

    base = tmp_path / "runtime"
    (base / "artifacts").mkdir(parents=True)
    (base / "logs").mkdir()
    (base / "workspaces").mkdir()
    (base / "entities" / "castor").mkdir(parents=True)

    register_gateway_data_homes(base)
    rows = {r["name"]: r for r in core_registry.list_data_homes()}
    by_kind = {r["kind"]: r for r in rows.values()}

    # Artifacts: load-bearing (offloaded ledger payloads) — never purgeable.
    assert by_kind["artifacts"]["safe_to_purge"] is False
    # Logs: regenerable — purgeable.
    assert by_kind["logs"]["safe_to_purge"] is True
    # Entity home: safe_to_purge=False BY CONSTRUCTION (never-purge visible).
    entity_rows = [r for r in rows.values() if r["kind"] == "entity-home"]
    assert len(entity_rows) == 1
    assert entity_rows[0]["safe_to_purge"] is False
    assert "castor" in entity_rows[0]["name"]


def test_entity_create_registers_at_first_write() -> None:
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Vesta", "spark": _spark("Vesta")}).status_code == 201
    rows = core_registry.list_data_homes()
    assert any(r["kind"] == "entity-home" and "vesta" in r["name"] for r in rows), rows


def test_admin_route_lists_sizes_and_purge_refuses_verbatim(tmp_path) -> None:
    with _client() as client:
        # Seed: one entity (protected) + the boot homes via the route's
        # re-register pass (logs dir must exist to land a purgeable row).
        (tmp_path / "runtime" / "logs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "runtime" / "logs" / "old.log").write_text("x" * 512, encoding="utf-8")
        assert client.post("/api/gateway/entities", json={"name": "Juno", "spark": _spark("Juno")}).status_code == 201

        listed = client.get("/api/gateway/admin/data-homes")
        assert listed.status_code == 200, listed.text
        homes = {h["name"]: h for h in listed.json()["homes"]}
        entity_row = next(h for h in homes.values() if h["kind"] == "entity-home")
        logs_row = next(h for h in homes.values() if h["kind"] == "logs")
        assert logs_row["size_bytes"] >= 512  # live sizes ride the rows

        # Owner-protected purge refuses 409 with the registry's own words.
        refused = client.post("/api/gateway/admin/data-homes/purge",
                              json={"name": entity_row["name"], "confirm_name": entity_row["name"]})
        assert refused.status_code == 409, refused.text
        assert "safe_to_purge=false" in refused.json()["detail"]
        assert "abstractgateway" in refused.json()["detail"]  # names the owner

        # Real purge without confirm_name refuses; dry-run needs none.
        bad = client.post("/api/gateway/admin/data-homes/purge", json={"name": logs_row["name"]})
        assert bad.status_code == 400
        dry = client.post("/api/gateway/admin/data-homes/purge", json={"name": logs_row["name"], "dry_run": True})
        assert dry.status_code == 200 and dry.json()["bytes_freed"] >= 512
        assert (tmp_path / "runtime" / "logs" / "old.log").exists()  # dry-run deleted nothing

        purged = client.post("/api/gateway/admin/data-homes/purge",
                             json={"name": logs_row["name"], "confirm_name": logs_row["name"]})
        assert purged.status_code == 200, purged.text
        assert purged.json()["files_deleted"] >= 1
        assert purged.json()["purged_by"].startswith("person:")
        assert not (tmp_path / "runtime" / "logs" / "old.log").exists()
        assert (tmp_path / "runtime" / "logs").is_dir()  # the home dir survives


def test_missing_facade_degrades_labeled(monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.data_homes as dh

    monkeypatch.setattr(dh, "_facade", lambda: (None, dh.FACADE_MISSING_NOTE))
    rows, warnings = dh.list_homes_with_sizes()
    assert rows == [] and any("#FALLBACK" in w for w in warnings)
    assert dh.register_gateway_data_homes(Path("/tmp/nowhere")) == []
    with _client() as client:
        r = client.get("/api/gateway/admin/data-homes")
        assert r.status_code == 200
        assert any("#FALLBACK" in w for w in r.json()["warnings"])
