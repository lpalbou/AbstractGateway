"""Dry-run entity validate (plan (b) P0-2, agency c909 ask 1).

POST /api/gateway/entities/{name}/validate runs the create pre-checks — spark
lint + name resolution + spark-drift — WITHOUT writing anything, so the
console modal can validate before the IRREVERSIBLE create (no DELETE, spark
v1-for-life; a failed create still burns the name). A green validate means
create will not refuse for lint/name/drift reasons.
"""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")

_TOKEN = "entity-validate-secret"


@pytest.fixture(autouse=True)
def _auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)


def _spark(name: str = "Castor") -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def test_valid_spark_validates_clean_and_writes_nothing() -> None:
    with _client() as client:
        r = client.post(
            "/api/gateway/entities/Castor/validate",
            json={"name": "Castor", "spark": _spark()},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["errors"] == []
        assert body["exists"] is False and body["would_conflict"] is False
        assert body["name"] == "Castor" and body["slug"] == "castor"

        # DRY-RUN: nothing was created — the entity list has no castor.
        listed = client.get("/api/gateway/entities").json()
        assert all(e.get("slug") != "castor" for e in listed.get("entities", []))


def test_lint_error_reports_not_creates() -> None:
    # A framework spark stripped of its core values fails the framework lint
    # (shared_vulnerability requirement) — validate reports it, create is
    # never reached.
    bad = _spark()
    bad["values"] = []  # remove the core values the framework lint requires
    with _client() as client:
        r = client.post(
            "/api/gateway/entities/Castor/validate",
            json={"name": "Castor", "spark": bad, "framework": True},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is False
        assert body["errors"], "a failing lint must report errors"
        # Still wrote nothing.
        listed = client.get("/api/gateway/entities").json()
        assert all(e.get("slug") != "castor" for e in listed.get("entities", []))


def test_drift_conflict_detected_against_an_existing_home() -> None:
    with _client() as client:
        assert client.post(
            "/api/gateway/entities", json={"name": "Castor", "spark": _spark()}
        ).status_code == 201

        # Same spark: idempotent, no conflict.
        same = client.post(
            "/api/gateway/entities/Castor/validate",
            json={"name": "Castor", "spark": _spark()},
        ).json()
        assert same["exists"] is True and same["would_conflict"] is False and same["ok"] is True

        # Changed spark under the same name: create would 409 — validate says so.
        drifted = _spark()
        drifted["purpose"] = "a deliberately different purpose that changes the hash"
        conflict = client.post(
            "/api/gateway/entities/Castor/validate",
            json={"name": "Castor", "spark": drifted},
        ).json()
        assert conflict["exists"] is True
        assert conflict["would_conflict"] is True
        assert conflict["ok"] is False


def test_path_body_name_mismatch_is_reported() -> None:
    with _client() as client:
        r = client.post(
            "/api/gateway/entities/Castor/validate",
            json={"name": "Pollux", "spark": _spark("Pollux")},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is False
        assert any("does not match" in e for e in body["errors"])


def test_validate_requires_auth() -> None:
    from abstractgateway.app import app

    with TestClient(app) as client:  # no Authorization header
        r = client.post(
            "/api/gateway/entities/Castor/validate", json={"name": "Castor", "spark": _spark()}
        )
        assert r.status_code in (401, 403), r.text
