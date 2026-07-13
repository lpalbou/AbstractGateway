"""Own-time loop control through the gateway (maintainer ruling 2026-07-08:
"we were working on a command on the gateway, it shouldn't work with the
file system directly").

- stop is a DURABLE COMMAND in the home's inbox (home.sqlite3), consumed by
  the loop at its next boundary — the gateway writes NO sentinel files.
- status is pid-cross-checked (a crashed loop reads stopped) and surfaces
  stop_requested for both brake channels (file + command).
- a stale stop addressed to a previous life dies at the next start
  (fast-forward at the start-request moment).
"""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractmemory.replay")

_TOKEN = "entity-loop-shared-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    # Substrate ruling (2026-07-09): NO code default — these env vars are
    # the operator's explicit choice for this suite.
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER", "lmstudio")
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CHAT_MODEL", "test-model")


def _spark(name: str = "Castor") -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def test_loop_stop_is_a_durable_command_not_a_sentinel_file():
    from abstractgateway.service import get_gateway_service

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        registry = get_gateway_service().entity_registry
        home_dir = registry.entities_dir / "castor"

        # Nothing runs: status is honest.
        r = client.get("/api/gateway/entities/Castor/loop")
        assert r.status_code == 200
        assert r.json()["running"] is False

        # Stop: enqueues the durable command; NO STOP file appears.
        r2 = client.post("/api/gateway/entities/Castor/loop/stop")
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert body["stop_requested"] is True
        assert body["status"]["stop_command"]["accepted"] is True
        assert not (home_dir / "STOP").exists()

        # The pending command is visible on the status surface... for a
        # RUNNING loop only; with no loop alive it must not read as pending
        # forever — but the inbox does hold it until the next start.
        from abstractruntime.identity.life import loop_stop_pending

        assert loop_stop_pending(home_dir) is True

        # A later start fast-forwards the stale stop (it was addressed to a
        # life that never existed); the inbox is clean for the new life.
        from abstractruntime.identity.life import fast_forward_loop_commands

        skipped = fast_forward_loop_commands(home_dir)
        assert skipped >= 1
        assert loop_stop_pending(home_dir) is False

        # The stop landed as a host marker (part of his biography).
        replay = client.get("/api/gateway/entities/Castor/replay?families=host")
        kinds = [
            __import__("json").loads(line)["payload"]["kind"]
            for line in replay.text.splitlines()
            if line.strip()
        ]
        assert "personal_stop_requested" in kinds, "ruled spelling (c786): new writes are personal_*"


def test_loop_freeze_kills_now_pauses_state_and_marks_biography(monkeypatch: pytest.MonkeyPatch):
    """FREEZE (maintainer ruling): admin hibernation — process killed now (no
    boundary wait), entity paused (door closed until admin wake), biography
    marked with personal_frozen. The graceful path stays untouched."""
    import abstractruntime.identity.life as life_mod
    from abstractgateway.service import get_gateway_service

    frozen_calls = {}

    def _fake_hard_stop(home_dir, *, reason="", requested_by="admin"):
        frozen_calls.update({"home": str(home_dir), "reason": reason, "requested_by": requested_by})
        return {
            "frozen": True, "pid": 4242, "was_running": True,
            "escalated_to_sigkill": False, "reason": reason, "requested_by": requested_by,
            "status": {"phase": "stopped", "running": False, "stop_requested": False},
        }

    monkeypatch.setattr(life_mod, "hard_stop_loop", _fake_hard_stop)

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        r = client.post(
            "/api/gateway/entities/Castor/loop/stop",
            json={"mode": "freeze", "reason": "imminent threat drill"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["frozen"] is True
        assert frozen_calls["requested_by"] == "admin"
        assert "imminent threat drill" in frozen_calls["reason"]

        # The door is closed: paused state with the FROZEN reason.
        registry = get_gateway_service().entity_registry
        from abstractruntime.identity.life import read_entity_state

        state = read_entity_state(registry.entities_dir / "castor")
        assert state["state"] == "paused"
        assert "FROZEN" in state["reason"]

        # No STOP file, no graceful command involved.
        assert not (registry.entities_dir / "castor" / "STOP").exists()

        # Biography carries the freeze.
        replay = client.get("/api/gateway/entities/Castor/replay?families=host")
        kinds = [
            __import__("json").loads(line)["payload"]["kind"]
            for line in replay.text.splitlines()
            if line.strip()
        ]
        assert "personal_frozen" in kinds, "ruled spelling (c786): new writes are personal_*"

        # Unknown mode refuses loudly.
        r2 = client.post("/api/gateway/entities/Castor/loop/stop", json={"mode": "violent"})
        assert r2.status_code == 400


def test_loop_start_resolves_attention_defaults_from_env(monkeypatch: pytest.MonkeyPatch):
    """The loop start route resolves shelf/context like the chat surface:
    request > env > wide defaults (36 / 65536; shelf widened 2026-07-09).
    Verified by intercepting the spawn (no real LLM process in tests)."""
    calls = {}

    def _fake_spawn(home_dir, **kwargs):
        calls.update(kwargs)
        return {
            "pid": 4242, "log": str(home_dir / "own_time.log"),
            "provider": kwargs.get("provider"), "model": kwargs.get("model"),
            "tick_seconds": kwargs.get("tick_seconds"), "ticks_per_day": kwargs.get("ticks_per_day"),
            "rest_minutes": kwargs.get("rest_minutes"), "shelf_size": kwargs.get("shelf_size"),
        }

    import abstractruntime.identity.life as life_mod

    monkeypatch.setattr(life_mod, "spawn_loop_process", _fake_spawn)

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        # No env, no request values: the wide defaults.
        r = client.post("/api/gateway/entities/Castor/loop/start", json={})
        assert r.status_code == 200, r.text
        assert calls["shelf_size"] == 36
        assert calls["context_window"] == 65536

        # Env overrides the defaults (the operator's knob).
        monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CHAT_SHELF_SIZE", "32")
        monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CHAT_CONTEXT_WINDOW", "131072")
        calls.clear()
        r2 = client.post("/api/gateway/entities/Castor/loop/start", json={})
        assert r2.status_code == 200, r2.text
        assert calls["shelf_size"] == 32
        assert calls["context_window"] == 131072

        # Request body wins over env.
        calls.clear()
        r3 = client.post(
            "/api/gateway/entities/Castor/loop/start",
            json={"shelf_size": 12, "context_window": 20000},
        )
        assert r3.status_code == 200, r3.text
        assert calls["shelf_size"] == 12
        assert calls["context_window"] == 20000
