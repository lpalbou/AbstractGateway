"""Titles and summaries still to be authored, retried when a durable visit closes (AbstractRuntime
4eefddb, operator rulings 2026-11-09).

Every record an entity forms is authored with its title and summary in the formation step; only a
failed authoring call forms it PENDING (memory's `title.author` / `digest.author` tasks). The
runtime's drainer retries them at the own-time loop's tick boundary and at a hosted chat session's
close. A home with no loop running and a DURABLE visit closing has neither: this is its turn.

Bounded like every other site (the cognition profile's `rebuild.cap_per_turn`; 0 = nothing here),
run beside the close, never in it (the close has answered), under the home's writer lease taken
NOW or not at all: a home someone else is writing (a loop, a new visit, a sleep pass) is theirs,
and their own boundary drains. An unreachable summaries model defers without spending an attempt."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

__all__ = ["drain_after_close", "schedule_drain_after_close"]

# The drain started for each home, joinable (tests; a second close while one runs starts none).
_RUNNING: Dict[str, threading.Thread] = {}
_RUNNING_LOCK = threading.Lock()


def drain_after_close(registry: Any, slug: str) -> Dict[str, Any]:
    """Run the authoring drainer once over a loop-less home. Never raises; returns the receipt or
    `{"skipped": <why>}`."""
    from abstractruntime.identity.authoring import drain_authoring
    from abstractruntime.identity.cognition_profile import rebuild_policy
    from abstractruntime.identity.life import read_entity_state
    from abstractruntime.storage.lease import DirectoryLeaseHeld, acquire_directory_lease

    from .entity_loop import loop_status

    home_dir = Path(registry.entities_dir) / slug
    try:
        if str(read_entity_state(home_dir).get("state") or "") == "paused":
            return {"skipped": "stopped"}
        if bool(loop_status(home_dir).get("running")):
            return {"skipped": "loop"}  # the loop drains at its own tick boundary
        cap = int(rebuild_policy(home_dir)["cap_per_turn"])
        if cap <= 0:
            return {"skipped": "cap_zero"}
        try:
            lease = acquire_directory_lease(home_dir, holder="gateway-authoring")
        except DirectoryLeaseHeld:
            return {"skipped": "home_busy"}
        try:
            from .entity_phase import generation_of

            home = registry.get_entity_runtime(slug).home
            entry_generation = generation_of(registry.entities_dir, slug)

            def _diary_text(entry_id: str) -> Optional[str]:
                entry = home.diary.get_entry(entry_id) if entry_id else None
                return str((entry or {}).get("text") or "") or None

            receipt = drain_authoring(
                home.ms,
                llm_for_capability=registry.capability_llm_factory(slug),
                artifacts=home.artifacts,
                home_dir=home.home_dir,
                entity=str(home.entity_id),
                holder="gateway:visit-close",
                cap=cap,
                window="visit",
                diary_text=_diary_text,
                out=lambda line: logger.info("authoring drain %s: %s", slug, line),
                # A Stop since the drain began: no further call, and nothing written after it.
                should_stop=lambda: generation_of(registry.entities_dir, slug) != entry_generation,
            )
        finally:
            lease.release()
        if receipt.get("considered"):
            logger.info("authoring drain after a visit closed on %s: %s", slug, receipt)
        return receipt
    except Exception as e:  # noqa: BLE001 - a drain never breaks a close; the records stay pending
        logger.exception("authoring drain after a visit closed on %s failed", slug)
        return {"skipped": "error", "error": f"{type(e).__name__}: {e}"}


def schedule_drain_after_close(registry: Any, slug: str) -> Optional[threading.Thread]:
    """Start `drain_after_close` beside the close (one at a time per home)."""
    with _RUNNING_LOCK:
        running = _RUNNING.get(slug)
        if running is not None and running.is_alive():
            return None
        thread = threading.Thread(target=drain_after_close, args=(registry, slug),
                                  name=f"authoring-drain-{slug}", daemon=True)
        _RUNNING[slug] = thread
        thread.start()
        return thread
