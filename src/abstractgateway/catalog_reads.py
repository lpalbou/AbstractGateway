"""Share concurrent host catalog reads without caching completed answers.

These reads describe the same host for every authorized console user. Request
parameters identify a read; the route's authorization still runs for each caller.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable
from weakref import WeakKeyDictionary

_pending: WeakKeyDictionary = WeakKeyDictionary()


async def catalog_read(key: tuple, read: Callable[[], Awaitable[Any]]) -> Any:
    pending = _pending.setdefault(asyncio.get_running_loop(), {})
    task = pending.get(key)
    if task is None:
        task = asyncio.create_task(read())
        pending[key] = task

        def finished(done):
            if pending.get(key) is done:
                pending.pop(key)
            if not done.cancelled():
                done.exception()  # Consume failures even if every waiter disconnected.

        task.add_done_callback(finished)
    # One disconnected browser must not cancel another browser's shared read.
    return await asyncio.shield(task)
