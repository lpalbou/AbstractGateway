"""Gateway half of the H4 steer door (hooks plan, 2026-07-12).

One durable steer sidecar per data root. The HTTP door accepts an
`inject_guidance` command; the runner queues it through `Runtime.steer()`
(runtime 2ce4a60) into this sidecar; the run's OWN tick drains it into
`_runtime.inbox` at the next iteration boundary and acks with an
`abstract.steer_seen` ledger record. The tick thread stays the only writer
of run state — the gateway never writes run vars for steering.

The sidecar db lives beside the run stores (`<data_root>/steer_sidecar.sqlite3`)
so per-principal data roots get per-principal steer isolation for free.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("abstractgateway.steering")

_SIDECARS: Dict[str, Any] = {}
_LOCK = threading.Lock()


def gateway_steer_sidecar(base_dir: Path | str) -> Optional[Any]:
    """The durable steer sidecar for one data root (cached per path).

    Returns None with a labeled warning when the installed abstractruntime
    predates the sidecar (version skew) — callers degrade to their legacy
    path, never crash the door.
    """
    key = str(Path(base_dir).expanduser().resolve())
    with _LOCK:
        if key in _SIDECARS:
            return _SIDECARS[key]
        try:
            from abstractruntime.storage.steer_sidecar import SqliteSteerSidecar
        except Exception as e:  # pragma: no cover - version-skew path
            logger.warning(
                "#FALLBACK abstractruntime has no steer sidecar (%s); "
                "inject_guidance falls back to direct run-var writes",
                e,
            )
            _SIDECARS[key] = None
            return None
        sidecar = SqliteSteerSidecar(str(Path(key) / "steer_sidecar.sqlite3"))
        _SIDECARS[key] = sidecar
        return sidecar


def evict_steer_sidecar(base_dir: Path | str) -> None:
    """Drop the cached sidecar for a data root (adversary F3: after a
    root purge+recreate, a cached instance points at a db whose table was
    created in the OLD directory — appends fail forever until process
    restart). Called from the service invalidation path; the next
    gateway_steer_sidecar() call rebuilds against the current disk state."""
    key = str(Path(base_dir).expanduser().resolve())
    with _LOCK:
        _SIDECARS.pop(key, None)


def attach_steer_store(runtime: Any, base_dir: Path | str) -> None:
    """Attach the data root's sidecar to a Runtime so its TICKS drain steers.

    The runtime factory does not (yet) take a steer_store kwarg, so this
    sets the attribute the Runtime constructor would have set — flagged to
    the runtime seat for a first-class factory param. No-op (labeled) when
    the sidecar is unavailable or the runtime predates steering.
    """
    sidecar = gateway_steer_sidecar(base_dir)
    if sidecar is None:
        return
    if not hasattr(runtime, "_steer_store"):
        logger.warning(
            "#FALLBACK runtime %s has no _steer_store attribute (pre-H4 abstractruntime); steers will not drain",
            type(runtime).__name__,
        )
        return
    if getattr(runtime, "_steer_store", None) is None:
        setattr(runtime, "_steer_store", sidecar)
