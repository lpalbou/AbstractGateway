"""ASGI `receive` callables that replay an already-read request body.

Why this module exists (R13.1, the 2026-10-04 21:23 watchdog incident): the
security middleware buffered a request body that arrived WITHOUT a
Content-Length (chunked — the console's /apps/code/ proxy streams bodies) and
handed the app a replay `receive` that, once the body was delivered, returned
``{"type": "http.request", "body": b"", "more_body": False}`` immediately on
every later call. Starlette's ``StreamingResponse`` listens for the client
leaving with ``while True: message = await receive()`` until it sees
``http.disconnect``. A receive that never suspends turns that loop into a
busy loop ON THE EVENT LOOP: the gateway stopped answering everything (health
included) until the watchdog killed it 30 s later.

The rule: after the replayed body, a receive must SUSPEND — hand over to the
real connection's receive (which waits and reports ``http.disconnect`` when
the client goes), or, for an internal sub-request with no client, wait
forever (the response's own task group cancels the wait when it finishes).

Every synthetic receive in the gateway is built here (a structural test keeps
it that way: ``tests/test_asgi_receive_never_spins.py``).
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, MutableMapping, Optional

Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]


def replay_body_receive(body: bytes, then: Optional[Receive] = None) -> Receive:
    """A receive that yields ``body`` once, then suspends.

    ``then``: the connection's own receive (a real request: later calls wait
    on it and see ``http.disconnect`` when the client leaves). ``None`` for an
    internal sub-request without a client: later calls wait until cancelled.
    """

    state: Dict[str, bool] = {"sent": False}
    idle: Dict[str, Optional[asyncio.Event]] = {"event": None}

    async def receive() -> Message:
        if not state["sent"]:
            state["sent"] = True
            return {"type": "http.request", "body": body or b"", "more_body": False}
        if then is not None:
            return await then()
        # No client behind this request: nothing more will ever arrive. Wait
        # (suspending the task, never spinning) until the caller cancels.
        event = idle["event"]
        if event is None:
            event = idle["event"] = asyncio.Event()
        await event.wait()
        return {"type": "http.disconnect"}  # pragma: no cover - the event is never set

    return receive
