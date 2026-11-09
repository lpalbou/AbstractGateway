"""The phase bar's live surface is the replay stream the Entity app already holds (operator ruling
2026-11-08; root untracked/entity-phases-2026-11-08/CONTRACT-phases.md v2 §5): a change made where
the gateway has no write point (inside the own-time loop: a state file written by the loop) reaches
an open stream as one `phase_changed` host envelope, and a reconnect with `Last-Event-ID` redelivers
it. The stream's generator is driven directly (a life has no terminal state)."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractmemory.replay")

_TOKEN = "entity-phase-live-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
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


def _home_dir():
    from pathlib import Path

    from abstractgateway.service import get_gateway_service

    return Path(get_gateway_service().config.data_dir) / "entities" / "castor"


class _Req:
    async def is_disconnected(self) -> bool:
        return False


def _events(chunk: str):
    for block in chunk.split("\n\n"):
        if "data: " in block:
            ident = block.split("id: ", 1)[1].split("\n", 1)[0] if "id: " in block else None
            yield ident, json.loads(block.split("data: ", 1)[1])


async def _read_until(gen, pred, *, seconds: float = 10.0):
    """Collect (id, envelope) until `pred(envelope)`; fail (never hang) after `seconds`."""
    seen = []
    buffer = ""

    async def _go():
        nonlocal buffer
        while True:
            buffer += (await gen.__anext__()).decode("utf-8")
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                for ident, env in _events(block + "\n\n"):
                    seen.append((ident, env))
                    if pred(env):
                        return

    await asyncio.wait_for(_go(), timeout=seconds)
    return seen


def _is_phase(env, to):
    p = env.get("payload") or {}
    return env.get("family") == "host" and p.get("kind") == "phase_changed" and p.get("to") == to


def test_a_change_made_inside_the_loop_reaches_an_open_stream_and_survives_a_reconnect():
    from abstractruntime.identity.life import write_entity_state

    from abstractgateway.routes.entity_replay import stream_entity_replay

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        assert client.get("/api/gateway/entities/Castor/life_state").json()["phase"] == "idle"  # a newborn starts idle

        async def _scenario():
            resp = await stream_entity_replay(_Req(), "Castor", since_seq=0.0, families=None, enrich=False,
                                              heartbeat_s=5.0, poll_s=0.05, last_event_id=None)
            gen = resp.body_iterator
            try:
                # The backlog (the newborn's own phase marker included) drains first.
                await _read_until(gen, lambda e: _is_phase(e, "idle"))
                # Another process writes the state file itself: no gateway route is involved.
                write_entity_state(_home_dir(), "asleep", reason="put to rest outside the gateway")
                seen = await _read_until(gen, lambda e: _is_phase(e, "sleep"))
                ident, env = seen[-1]
                payload = env["payload"]
                assert payload["from"] == "idle" and payload["phase"] == "sleep"
                assert payload["cause"] == "observed" and payload["rev"] >= 2
                assert payload["sentence"] == "Asleep until you wake him."
                before_ident = seen[-2][0] if len(seen) > 1 else None
            finally:
                await gen.aclose()
            # Reconnect from the event before the marker: the marker is delivered again.
            resp2 = await stream_entity_replay(_Req(), "Castor", since_seq=0.0, families=None, enrich=False,
                                               heartbeat_s=5.0, poll_s=0.05, last_event_id=before_ident)
            gen2 = resp2.body_iterator
            try:
                again = await _read_until(gen2, lambda e: _is_phase(e, "sleep"))
                assert again[-1][1]["payload"]["rev"] == payload["rev"]
            finally:
                await gen2.aclose()

        asyncio.run(_scenario())
