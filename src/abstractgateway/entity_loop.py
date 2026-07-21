"""Own-time loop management through the gateway (maintainer ask, 2026-07-08:
"we should be able to activate this active mode from the webapp"; ruling,
same day: "we were working on a command on the gateway, it shouldn't work
with the file system directly").

This module is a THIN ADAPTER over the runtime's life-loop control surface
(`abstractruntime.identity.life`). The runtime owns the home's files —
single-writer discipline — so the gateway never touches STOP files, status
files, or logs itself:

- start:  `spawn_loop_process` (runtime) — detached process, stale STOP
          cleared, command inbox fast-forwarded at the start-request moment.
- stop:   `request_loop_stop` (runtime) — a DURABLE COMMAND in the home's
          inbox (home.sqlite3), consumed by the loop at its next boundary.
          The running thought completes or fails whole; nothing is killed
          mid-air. The STOP file remains the LOCAL manual brake only.
- status: `loop_process_status` (runtime) — the loop's own status file,
          pid-cross-checked (a crashed loop reads stopped, never a phantom
          "day"), with `stop_requested` covering both brake channels.

Both acts land as host markers in the replay stream — starting or stopping
someone's own time is part of their biography.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

__all__ = ["loop_status", "start_loop", "stop_loop"]


def loop_status(home_dir: Path) -> Dict[str, Any]:
    from abstractruntime.identity.life import loop_process_status

    return loop_process_status(Path(home_dir))


def start_loop(
    home_dir: Path,
    *,
    provider: str,
    model: str,
    base_url: Optional[str] = None,
    tick_seconds: float = 20.0,
    ticks_per_day: int = 8,
    rest_minutes: float = 30.0,
    shelf_size: Optional[int] = None,
    context_window: Optional[int] = None,
) -> Dict[str, Any]:
    from abstractruntime.identity.life import spawn_loop_process

    # One source for the wide-posture defaults (entity_chat) — a second
    # hardcoded copy here drifted once already (24 vs the ruled 36).
    from .entity_chat import DEFAULT_ENTITY_CHAT_CONTEXT_WINDOW, DEFAULT_ENTITY_CHAT_SHELF_SIZE

    if shelf_size is None:
        shelf_size = DEFAULT_ENTITY_CHAT_SHELF_SIZE
    if context_window is None:
        context_window = DEFAULT_ENTITY_CHAT_CONTEXT_WINDOW

    # Faithful-respawn record (self-repair, laurent 2026-07-21): the repair
    # sweeper replays THESE parameters — a repaired loop must run the same
    # schedule the operator started, never a default guess.
    from .entity_repair import record_spawn_params

    record_spawn_params(Path(home_dir), {
        "provider": provider, "model": model, "base_url": base_url,
        "tick_seconds": tick_seconds, "ticks_per_day": ticks_per_day,
        "rest_minutes": rest_minutes, "shelf_size": shelf_size,
        "context_window": context_window,
    })

    return spawn_loop_process(
        Path(home_dir),
        provider=provider,
        model=model,
        base_url=base_url,
        tick_seconds=tick_seconds,
        ticks_per_day=ticks_per_day,
        rest_minutes=rest_minutes,
        shelf_size=shelf_size,
        context_window=context_window,
    )


def stop_loop(home_dir: Path, *, reason: str = "", requested_by: str = "operator") -> Dict[str, Any]:
    """Enqueue the durable stop command and return the loop's current status
    (so callers can show 'still finishing its tick' honestly)."""
    from abstractruntime.identity.life import loop_process_status, request_loop_stop

    home_dir = Path(home_dir)
    enqueued = request_loop_stop(home_dir, reason=reason, requested_by=requested_by)
    status = loop_process_status(home_dir)
    status["stop_requested"] = True
    status["stop_command"] = enqueued
    return status


def freeze_loop(home_dir: Path, *, reason: str = "", requested_by: str = "admin") -> Dict[str, Any]:
    """FREEZE (hibernation): kill the loop process NOW — no boundary wait, no
    ceremony, no further cognition or writes. Admin-only by construction (the
    entity has no tool that reaches this). See `life.hard_stop_loop`."""
    from abstractruntime.identity.life import hard_stop_loop

    return hard_stop_loop(Path(home_dir), reason=reason, requested_by=requested_by)
