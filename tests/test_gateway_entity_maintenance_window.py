"""Maintenance window hold (Castor doctoring, operator GO 2026-07-13 21:12).

Pins: arming the hold refuses EVERY door path with an honest 409 (visit
open, chat open, summon, cognition — anything that opens the home);
the hold is FILE-based so it survives a serve restart (a fresh registry
over the same data root still refuses); open/close land host markers;
close releases and the doors serve again; the operator state underneath
is untouched (asleep stays asleep — no wake at completion, per the GO).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")

_TOKEN = "maintenance-window-secret"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _create(client: TestClient, name: str = "Castor") -> None:
    import copy

    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    assert client.post("/api/gateway/entities", json={"name": name, "spark": spark}).status_code == 201


def test_hold_refuses_every_door_and_survives_restart(tmp_path) -> None:
    with _client() as client:
        _create(client)

        opened = client.post(
            "/api/gateway/entities/Castor/maintenance-window",
            json={"action": "open", "reason": "doctoring"},
        )
        assert opened.status_code == 200, opened.text
        assert opened.json()["held"] is True

        # Every home-opening door refuses 409 naming the window.
        chat = client.post("/api/gateway/entities/Castor/chat/open", json={"provider": "lmstudio", "model": "x"})
        assert chat.status_code == 409, chat.text
        assert "maintenance window" in chat.json()["detail"]
        cog = client.get("/api/gateway/entities/Castor/cognition")
        assert cog.status_code == 409, cog.text
        card = client.get("/api/gateway/entities/Castor/card")
        assert card.status_code == 409, card.text

        # Status stays readable while held (the route opens no home).
        status = client.get("/api/gateway/entities/Castor/maintenance-window").json()
        assert status["held"] is True and status["reason"] == "doctoring"

        # The operator state file is untouched underneath (newborn=asleep).
        state = client.get("/api/gateway/entities/Castor/state").json()
        assert state["state"] == "asleep"

    # RESTART: a fresh app/registry over the same data root still refuses —
    # the hold is a file in the home, not process memory.
    with _client() as client2:
        cog2 = client2.get("/api/gateway/entities/Castor/cognition")
        assert cog2.status_code == 409

        closed = client2.post(
            "/api/gateway/entities/Castor/maintenance-window",
            json={"action": "close", "reason": "verify green"},
        )
        assert closed.status_code == 200 and closed.json()["held"] is False

        # Doors serve again; Castor is still asleep (no wake at completion).
        cog3 = client2.get("/api/gateway/entities/Castor/cognition")
        assert cog3.status_code == 200, cog3.text
        assert cog3.json()["state"]["state"] == "asleep"


def test_window_moments_land_as_host_markers(tmp_path) -> None:
    with _client() as client:
        _create(client, "Pollux")
        client.post("/api/gateway/entities/Pollux/maintenance-window", json={"action": "open", "reason": "op"})
        client.post("/api/gateway/entities/Pollux/maintenance-window", json={"action": "close", "reason": "done"})

        from abstractgateway.entity_replay import read_host_markers

        entities_dir = tmp_path / "runtime" / "entities"
        kinds = [m.get("payload", {}).get("kind") for m in read_host_markers(entities_dir, "pollux")]
        assert "maintenance_window_open" in kinds
        assert "maintenance_window_close" in kinds
