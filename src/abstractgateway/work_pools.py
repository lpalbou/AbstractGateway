"""Bounded worker pools by KIND of work (gateway load 2026-10-26, backlog 1054).

Every `asyncio.to_thread` rode the event loop's ONE default executor (min(32, cpu+4) = 22
threads on the operator's Mac) for ~270 call sites, so `/me` bookkeeping, the SSE streams
and health waited behind run and artifact scans, and up to 22 scans at once stat-stormed
the GIL (every `stat()` drops it; getting it back waits up to the 5 ms switch interval
behind each busy thread). The gateway now runs blocking work in three bounded pools:

- ``light``: auth, `/me`, health, SSE bookkeeping, one-record reads and small writes.
  Never waits behind a scan.
- ``heavy``: run and artifact scans, listings, host rebuilds, everything that reads many
  files. Few threads, so a burst of scans queues instead of fighting for the GIL.
- ``watchers``: long waits on something outside the process: mail and feed polling,
  provider endpoints and model generation, remote probes. They hold a thread for seconds
  to minutes while mostly releasing the GIL, so they must never fill the heavy pool.

Sizes come from `serve --pool-light N --pool-heavy N --pool-watchers N` (no environment
variable). `off_loop(kind, fn, ...)` is the one way in: it copies the caller's context
variables (the request principal), exactly like `asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional

KINDS = ("light", "heavy", "watchers")
DEFAULT_SIZES: Dict[str, int] = {"light": 8, "heavy": 4, "watchers": 16}
MAX_SIZE = 256

_sizes: Dict[str, int] = dict(DEFAULT_SIZES)
_pools: Dict[str, ThreadPoolExecutor] = {}
_guard = threading.Lock()


def _check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown work pool kind {kind!r} (expected one of {', '.join(KINDS)})")
    return kind


def _check_size(kind: str, value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"--pool-{kind} must be a whole number, got {value!r}") from None
    if n < 1 or n > MAX_SIZE:
        raise ValueError(f"--pool-{kind} must be between 1 and {MAX_SIZE}, got {n}")
    return n


def configure_pools(*, light: Optional[int] = None, heavy: Optional[int] = None, watchers: Optional[int] = None) -> Dict[str, int]:
    """Set pool sizes (None keeps the current one). A resized pool is replaced: work already
    queued on the old one finishes there, new work goes to the new one. Returns the sizes."""
    wanted = {"light": light, "heavy": heavy, "watchers": watchers}
    with _guard:
        for kind, value in wanted.items():
            if value is None:
                continue
            n = _check_size(kind, value)
            if n == _sizes[kind]:
                continue
            _sizes[kind] = n
            old = _pools.pop(kind, None)
            if old is not None:
                old.shutdown(wait=False)
        return dict(_sizes)


def pool_sizes() -> Dict[str, int]:
    with _guard:
        return dict(_sizes)


def get_pool(kind: str) -> ThreadPoolExecutor:
    _check_kind(kind)
    pool = _pools.get(kind)
    if pool is not None:
        return pool
    with _guard:
        pool = _pools.get(kind)
        if pool is None:
            pool = ThreadPoolExecutor(max_workers=_sizes[kind], thread_name_prefix=f"gw-{kind}")
            _pools[kind] = pool
        return pool


def shutdown_pools(*, timeout_s: float = 2.0) -> None:
    """At process stop: cancel queued work and let idle workers leave (their threads are
    non-daemon, so an idle pool otherwise counts as a stop-path straggler). A worker still running
    a call after `timeout_s` is left to the straggler belt in the CLI."""
    import time

    with _guard:
        pools = list(_pools.values())
        _pools.clear()
    threads = []
    for pool in pools:
        pool.shutdown(wait=False, cancel_futures=True)
        threads.extend(list(getattr(pool, "_threads", ()) or ()))
    end = time.monotonic() + max(0.0, float(timeout_s))
    for t in threads:
        t.join(max(0.0, end - time.monotonic()))


async def off_loop(kind: str, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Run `fn(*args, **kwargs)` in the `kind` pool and await its result (exceptions
    propagate). The caller's context variables travel with it, as with asyncio.to_thread."""
    loop = asyncio.get_running_loop()
    ctx = contextvars.copy_context()
    call = functools.partial(ctx.run, fn, *args, **kwargs)
    return await loop.run_in_executor(get_pool(kind), call)


def pool_stats() -> Dict[str, Dict[str, int]]:
    """Size, threads started and queued work per pool (a peek; never blocks)."""
    out: Dict[str, Dict[str, int]] = {}
    for kind in KINDS:
        pool = _pools.get(kind)
        out[kind] = {
            "size": _sizes[kind],
            "threads": len(getattr(pool, "_threads", ()) or ()) if pool is not None else 0,
            "queued": int(pool._work_queue.qsize()) if pool is not None else 0,
        }
    return out
