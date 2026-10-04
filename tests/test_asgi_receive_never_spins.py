"""R13.1: a replayed request body never turns `receive()` into a busy loop.

Root cause of the 2026-10-04 watchdog incident: after the buffered body, the
security middleware's replay `receive` answered "empty body" forever without
suspending; StreamingResponse's disconnect listener spun on it on the event
loop. Every synthetic receive in the gateway is now built by
`asgi_receive.replay_body_receive`, which suspends after the body.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from abstractgateway.asgi_receive import replay_body_receive

pytestmark = pytest.mark.basic

SRC = Path(__file__).resolve().parents[1] / "src" / "abstractgateway"


def test_after_the_body_a_replay_hands_over_to_the_connection_receive() -> None:
    async def scenario() -> list:
        calls = []

        async def connection_receive():
            calls.append("connection")
            await asyncio.sleep(0.01)
            return {"type": "http.disconnect"}

        receive = replay_body_receive(b'{"text":"hi"}', then=connection_receive)
        first = await receive()
        second = await receive()
        return [first, second, calls]

    first, second, calls = asyncio.run(scenario())
    assert first == {"type": "http.request", "body": b'{"text":"hi"}', "more_body": False}
    assert second == {"type": "http.disconnect"}
    assert calls == ["connection"]


def test_without_a_client_the_replay_suspends_instead_of_answering_forever() -> None:
    async def scenario() -> bool:
        receive = replay_body_receive(b"")
        await receive()
        try:
            await asyncio.wait_for(receive(), timeout=0.2)
        except asyncio.TimeoutError:
            return True
        return False

    assert asyncio.run(scenario()) is True


def test_a_disconnect_listener_on_a_replay_lets_the_loop_run() -> None:
    """Starlette's listener loop (`while True: await receive()`) must yield to other tasks."""

    async def scenario() -> int:
        receive = replay_body_receive(b"body")
        ticks = 0

        async def listener():
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return

        async def other():
            nonlocal ticks
            for _ in range(5):
                await asyncio.sleep(0.01)
                ticks += 1

        task = asyncio.ensure_future(listener())
        await asyncio.wait_for(other(), timeout=2)
        task.cancel()
        return ticks

    assert asyncio.run(scenario()) == 5


def test_no_other_module_builds_its_own_replay_receive() -> None:
    """The structural guard: a hand-rolled `{"more_body": False}` replay is how the hole came in."""
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name in {"asgi_receive.py", "console_islands.py"}:
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"[\"']more_body[\"']\s*:\s*False", text):
            offenders.append(str(path.relative_to(SRC)))
    assert offenders == [], f"use asgi_receive.replay_body_receive instead: {offenders}"
