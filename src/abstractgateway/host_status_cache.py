"""Short-TTL, single-flight caches for the host status the tray and the Observer poll.

The tray polls `GET /host/metrics/live` every second, `/host/state` and `/host/runner` every
5 s, `/host/runs` every 20 s; the Observer's GPU and memory widgets poll `/host/metrics/gpu`
and `/host/metrics/memory`; a console may poll `/host/state` too. Behind them sit the
expensive probes: an `ioreg` (or `nvidia-smi`) subprocess for the GPU (~20 ms, a fork of a
multi-gigabyte process), the memory snapshot (`ioreg` + `sysctl`, ~25 ms), the model
residency walk (a provider HTTP call each, up to 2 s), the session prompt-cache enumeration.

`TtlSingleFlight.get(key, compute)` answers from the last value younger than the TTL; on a miss
ONE caller computes while the others for the same key wait for that value (never a second
probe in parallel). A computation that raises is not cached (the next call tries again). Every
caller runs in a worker thread (the routes call these off the event loop); the waiting is a
lock wait in that thread, never on the loop.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Hashable, Optional, Tuple


class TtlSingleFlight:
    def __init__(self, ttl_s: float, *, name: str = "") -> None:
        self.ttl_s = float(ttl_s)
        self.name = name
        self._lock = threading.Lock()
        self._entries: Dict[Hashable, Tuple[float, Any]] = {}
        self._flights: Dict[Hashable, threading.Lock] = {}
        self.computes = 0  # how many times `compute` ran (tests, diagnostics)

    def get(self, key: Hashable, compute: Callable[[], Any], *, ttl_s: Optional[float] = None, cache_if: Optional[Callable[[Any], bool]] = None) -> Any:
        ttl = self.ttl_s if ttl_s is None else max(0.0, float(ttl_s))
        hit, value = self._fresh(key, ttl)
        if hit:
            return value
        with self._lock:
            flight = self._flights.setdefault(key, threading.Lock())
        with flight:  # one computation per key; the others wait here, then read its value
            hit, value = self._fresh(key, ttl)
            if hit:
                return value
            value = compute()
            with self._lock:
                self.computes += 1
                if ttl > 0 and (cache_if is None or cache_if(value)):
                    # Stamped when the value is READY: a probe that took 1 s still serves the
                    # full TTL (stamping at the start made a 1 s TTL at a 1 Hz poll a miss
                    # every time).
                    self._entries[key] = (time.monotonic(), value)
            return value

    def _fresh(self, key: Hashable, ttl: float) -> Tuple[bool, Any]:
        if ttl <= 0:
            return False, None
        with self._lock:
            entry = self._entries.get(key)
        if entry is not None and (time.monotonic() - entry[0]) <= ttl:
            return True, entry[1]
        return False, None

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


# The probes' TTLs. The tray's fast lane polls at 1 Hz: a 2 s TTL is one probe per two
# seconds whatever the number of pollers (the graph moves every other second).
GPU_TTL_S = 2.0
MEMORY_TTL_S = 2.0
# Residency and session caches: /host/state pollers (tray 5 s, console, Observer) share one walk.
RESIDENCY_TTL_S = 4.0
SESSION_CACHES_TTL_S = 4.0
# The install block (data dir, auth posture, login service, first-run state): file reads only,
# changes on an operator action.
INSTALL_BLOCK_TTL_S = 10.0

gpu_cache = TtlSingleFlight(GPU_TTL_S, name="gpu")
memory_cache = TtlSingleFlight(MEMORY_TTL_S, name="memory")
residency_cache = TtlSingleFlight(RESIDENCY_TTL_S, name="residency")
session_caches_cache = TtlSingleFlight(SESSION_CACHES_TTL_S, name="session_caches")
install_block_cache = TtlSingleFlight(INSTALL_BLOCK_TTL_S, name="install_block")


def invalidate_residency() -> None:
    """A load / eject / pin changed what is resident: the next /host/state walks again."""
    residency_cache.clear()
    session_caches_cache.clear()
    memory_cache.clear()


def clear_all() -> None:
    for c in (gpu_cache, memory_cache, residency_cache, session_caches_cache, install_block_cache):
        c.clear()


__all__ = [
    "TtlSingleFlight",
    "clear_all",
    "gpu_cache",
    "install_block_cache",
    "invalidate_residency",
    "memory_cache",
    "residency_cache",
    "session_caches_cache",
]
