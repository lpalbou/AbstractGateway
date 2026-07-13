"""Per-data-root entity creation quota (adversary F2, 2026-07-12).

Entities are PERMANENT (never-purge is structural) and each create mints a
door-global principal into the shared users registry — so user-level
creation (the P1-1 asymmetry) needs a bound: without one, any authenticated
principal can flood the host disk and users.json with un-prunable records.

The quota counts existing homes in the caller's registry root before any
write; new-home creation past it refuses loudly (429 at the route);
idempotent re-creates of existing entities are never refused.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")

_TOKEN = "entity-quota-secret"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _spark(client: TestClient, name: str) -> dict:
    # A lint-clean spark: the served framework-default template with the name
    # filled — the exact payload the console template fast-path sends.
    gallery = client.get("/api/gateway/entities/templates").json()
    doc = dict(next(t for t in gallery["templates"] if t["id"] == "framework-default")["spark"])
    doc["name"] = name
    return doc


# ---------------------------------------------------------------------------
# The knob
# ---------------------------------------------------------------------------


def test_quota_defaults_to_20(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.config import entity_create_quota

    monkeypatch.delenv("ABSTRACTGATEWAY_ENTITY_CREATE_QUOTA", raising=False)
    assert entity_create_quota() == 20


def test_quota_is_operator_customizable(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.config import entity_create_quota

    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CREATE_QUOTA", "3")
    assert entity_create_quota() == 3


@pytest.mark.parametrize("spelling", ["0", "off", "none", "disabled", "false", "OFF"])
def test_quota_disable_spellings(monkeypatch: pytest.MonkeyPatch, spelling: str) -> None:
    from abstractgateway.config import entity_create_quota

    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CREATE_QUOTA", spelling)
    assert entity_create_quota() is None


def test_invalid_quota_env_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.config import entity_create_quota

    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CREATE_QUOTA", "not-a-number")
    assert entity_create_quota() == 20
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CREATE_QUOTA", "-5")
    assert entity_create_quota() is None


# ---------------------------------------------------------------------------
# Enforcement through the served route
# ---------------------------------------------------------------------------


def test_new_home_past_quota_refuses_429_but_recreate_stays_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CREATE_QUOTA", "2")
    with _client() as client:
        for name in ("alpha", "beta"):
            r = client.post("/api/gateway/entities", json={"name": name, "spark": _spark(client, name)})
            assert r.status_code == 201, r.text

        # Third NEW home refuses with the quota named.
        r = client.post("/api/gateway/entities", json={"name": "gamma", "spark": _spark(client, "gamma")})
        assert r.status_code == 429, r.text
        assert "quota" in r.json()["detail"].lower()
        listed = client.get("/api/gateway/entities").json()
        assert not any(e.get("slug") == "gamma" for e in listed["entities"])

        # Idempotent re-create of an EXISTING home is never quota-refused.
        r = client.post("/api/gateway/entities", json={"name": "alpha", "spark": _spark(client, "alpha")})
        assert r.status_code == 201, r.text
        assert r.json()["created"] is False


def test_disabled_quota_places_no_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CREATE_QUOTA", "off")
    with _client() as client:
        for name in ("one", "two", "three"):
            r = client.post("/api/gateway/entities", json={"name": name, "spark": _spark(client, name)})
            assert r.status_code == 201, r.text


def test_quota_refusal_happens_before_any_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The refused create must leave NO home directory behind (the check runs
    # before mkdir — a half-created dir would poison the next attempt).
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CREATE_QUOTA", "1")
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "solo", "spark": _spark(client, "solo")}).status_code == 201
        assert client.post("/api/gateway/entities", json={"name": "extra", "spark": _spark(client, "extra")}).status_code == 429

    entities_dir = tmp_path / "runtime" / "entities"
    children = {c.name for c in entities_dir.iterdir() if c.is_dir()}
    assert children == {"solo"}, f"refused create must not leave a home dir: {children}"
