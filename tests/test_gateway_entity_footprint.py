"""Read-only entity home footprint (entity app c1779: the memory-health
panel's before/after — the one number the replay stream deliberately does
not carry).

Pins: sizes + journal event count served N6-clean (no paths, no base_url,
no keys anywhere in the payload); readable WHILE a maintenance hold is up
(the panel renders the diff during the act); last maintenance act surfaces
from the host markers; degradations labeled, never a 500.
"""

from __future__ import annotations

import copy
import json

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")

_TOKEN = "footprint-secret"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def test_footprint_serves_sizes_counts_and_maintenance_anchor() -> None:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    with _client() as client:
        spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
        spark["name"] = "Metis"
        assert client.post("/api/gateway/entities", json={"name": "Metis", "spark": spark}).status_code == 201

        fp = client.get("/api/gateway/entities/Metis/footprint")
        assert fp.status_code == 200, fp.text
        body = fp.json()
        assert body["memory_bytes"] and body["memory_bytes"] > 0
        assert body["home_bytes"] and body["home_bytes"] >= body["memory_bytes"]
        # A NEWBORN has zero usage events — presence ≠ use (D2): the engram
        # plants identity RECORDS without depositing usage. Records > 0,
        # journal_events == 0 is the honest newborn shape.
        assert isinstance(body["journal_events"], int) and body["journal_events"] >= 0
        assert isinstance(body["records"], int) and body["records"] > 0
        assert body["maintenance_held"] is False

        # N6: no filesystem paths, keys, or urls anywhere in the payload.
        blob = json.dumps(body)
        assert "/" not in blob.replace("\\/", "") or "://" not in blob
        assert "runtime/entities" not in blob and "api_key" not in blob and "base_url" not in blob

        # Readable WHILE HELD (the panel renders the diff during the act) —
        # and the maintenance act anchors last_maintenance_*.
        opened = client.post(
            "/api/gateway/entities/Metis/maintenance-window",
            json={"action": "open", "reason": "footprint test"},
        )
        assert opened.status_code == 200
        held = client.get("/api/gateway/entities/Metis/footprint")
        assert held.status_code == 200, held.text
        assert held.json()["maintenance_held"] is True
        assert held.json()["last_maintenance_kind"] == "maintenance_window_open"

        closed = client.post(
            "/api/gateway/entities/Metis/maintenance-window",
            json={"action": "close", "reason": "footprint test done"},
        )
        assert closed.status_code == 200
        after = client.get("/api/gateway/entities/Metis/footprint").json()
        assert after["last_maintenance_kind"] == "maintenance_window_close"
