"""The entity CONVERSATION SEAT — door half (conversation-seat plan, slice 2).

THE RULE, one sentence (three-seat-sealed design, operator-mandated
2026-07-25): machinery yields to humans; humans wait for each other.

The incident this exists for: the operator was mid-conversation with his
entity when an agent probe took the one-life-one-summon guard BETWEEN his
messages (his turn's run had completed, so the day-one liveness rule — "run
non-terminal" — read the seat as free); his next message was refused with a
raw 409. The seat generalizes the guard from "a live run" to "a held
conversation":

- SEAT RECORD: ``entities/.live_summons/<slug>.json`` carries
  {run_id, session_id, holder, holder_kind, client, held_since, renewed_at,
  idle_ttl_s}. Same-session summons SLIDE the TTL (renewed_at advances,
  held_since is preserved) — a conversation's short per-turn runs keep one
  seat.
- LIVENESS: the seat is held while its run is NON-TERMINAL **or**
  now < renewed_at + idle_ttl_s (sliding 300s). The TTL is what protects a
  human's inter-turn gaps; it applies to every holder so agent-vs-agent
  keeps first-wins continuity too.
- PRIORITY (door_decision): agent-vs-anyone -> 409 + retry_after_s (an
  agent never displaces a held seat). Human-vs-agent -> the human WINS:
  preempt at the next turn boundary (the door cancels the holder's run
  tree via runtime.cancel_run — runtime's shipped semantics guarantee the
  honest CANCELLED terminal and the between-steps abort; formations stand
  as lived). Human-vs-human -> wait (409 + retry_after_s; the mailbox slice
  adds the visible queue), EXCEPT the same principal reclaiming their own
  idle seat (one human cannot queue behind themself).
- HOLDER KIND is the caller's DECLARATION (``caller_kind`` on the summon
  request) until GW-H per-agent principals make it structural: absent or
  unknown reads as AGENT — the door fails toward the human (an undeclared
  caller never preempts; an undeclared holder is preemptable). Declarations
  are etiquette-bound and audit-trailed (markers carry principal + kind).

Cross-lane (item 5, one helper three doors): ``build_summon_seat_probe``
gives the visit/chat opens a read of this seat; those doors refuse only on
a LIVE summon run (a turn mid-flight) — a TTL-idle seat does not block the
operator's own drawer surfaces, while an open visit/chat blocks summons
absolutely (the summon door checks their occupancy probes).

This module is deliberately route-free: pure record I/O + pure decisions,
unit-testable without an app. The routes own markers and HTTP shapes.
"""

from __future__ import annotations

import json
import logging
import math
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# The per-slug door guard (moved here from routes/entities.py when the queue
# landed — seat, queue and summon all serialize on it). RLock DELIBERATELY:
# queue admission holds it across head-read -> summon-core (which re-acquires)
# so two tickers racing one freed seat cannot both admit (contract invariant
# 12, idempotent-by-queue_id — the in-process half; cross-process exclusion
# stays the home lease's job).
_GUARD_LOCKS: Dict[str, threading.RLock] = {}
_GUARD_LOCKS_LOCK = threading.Lock()


def summon_guard_lock(slug: str) -> threading.RLock:
    with _GUARD_LOCKS_LOCK:
        lock = _GUARD_LOCKS.get(slug)
        if lock is None:
            lock = threading.RLock()
            _GUARD_LOCKS[slug] = lock
        return lock

# Sliding idle TTL (seconds) — the plan's 300s. One constant, deliberately
# not an env knob (env-kill wave); a settings-registry dial can front it
# later without changing record shapes (idle_ttl_s is engraved per record,
# so in-flight seats keep the TTL they were taken with).
SEAT_IDLE_TTL_S = 300

# Poll hint (seconds) returned as retry_after_s while the holding run is
# LIVE — remaining time is unknowable then (probe runs are sub-minute), so
# this is a hint, never a promise. TTL-held seats return the exact remainder.
LIVE_RUN_RETRY_HINT_S = 15

_TERMINAL = {"completed", "failed", "cancelled"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    try:
        s = str(value or "").strip()
        if not s:
            return None
        dt = datetime.fromisoformat(s)
        # Naive stamps read as UTC (the WAIT_UNTIL normalization lesson:
        # every comparison in this module is aware-UTC).
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def seat_path(entities_dir: Path, slug: str) -> Path:
    return Path(entities_dir) / ".live_summons" / f"{slug}.json"


def read_seat(entities_dir: Path, slug: str) -> Optional[Dict[str, Any]]:
    path = seat_path(entities_dir, slug)
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None  # unreadable bookkeeping never blocks a summon


def record_seat(
    entities_dir: Path,
    slug: str,
    *,
    run_id: str,
    session_id: str,
    holder: str = "",
    holder_kind: str = "unknown",
    client: str = "",
    held_since: Optional[str] = None,
) -> None:
    """Take (or slide) the seat. ``held_since`` is passed on a same-session
    slide so the record keeps the conversation's true start; a fresh take
    stamps now for both."""
    path = seat_path(entities_dir, slug)
    now = _now_iso()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp-{secrets.token_hex(4)}")
        tmp.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "session_id": session_id,
                    "recorded_at": now,  # kept for back-compat readers
                    "holder": holder,
                    # Declared kind (human|agent) or 'unknown' — GW-H makes it
                    # structural; door_decision reads unknown as agent.
                    "holder_kind": holder_kind,
                    "client": client,
                    "held_since": held_since or now,
                    "renewed_at": now,
                    "idle_ttl_s": SEAT_IDLE_TTL_S,
                }
            ),
            encoding="utf-8",
        )
        tmp.replace(path)
    except Exception:
        logger.warning("seat record failed for %s (guard degrades to open)", slug, exc_info=True)


def _run_status(run_store: Any, run_id: str) -> Optional[str]:
    """Lowercase run status, or None when the run is unknown/unreadable
    (pruned runs read as not-live; the TTL still holds the seat)."""
    if not run_id:
        return None
    try:
        run = run_store.load(run_id)
    except Exception:
        return None
    if run is None:
        return None
    status = getattr(run, "status", None)
    s = str(getattr(status, "value", status) or "").strip().lower()
    return s or None


def seat_occupancy(
    run_store: Any,
    entities_dir: Path,
    slug: str,
    *,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """The held seat block, or None when free.

    Liveness = run non-terminal OR now < renewed_at + idle_ttl_s. Legacy
    records (pre-TTL, no renewed_at) fall back to recorded_at; a record
    with no usable timestamp holds only while its run is live (exactly the
    day-one semantics those records were written under)."""
    rec = read_seat(entities_dir, slug)
    if rec is None:
        return None
    run_id = str(rec.get("run_id") or "").strip()
    status = _run_status(run_store, run_id)
    run_live = status is not None and status not in _TERMINAL

    ttl_s = int(rec.get("idle_ttl_s") or SEAT_IDLE_TTL_S)
    anchor = _parse_iso(rec.get("renewed_at")) or _parse_iso(rec.get("recorded_at"))
    now_dt = now or _now()
    if anchor is not None:
        remaining = (anchor + timedelta(seconds=ttl_s) - now_dt).total_seconds()
        ttl_remaining_s = max(0, int(math.ceil(remaining)))
    else:
        ttl_remaining_s = 0

    if not run_live and ttl_remaining_s <= 0:
        return None
    return {
        "lane": "summon",
        "run_id": run_id,
        "session_id": str(rec.get("session_id") or ""),
        "status": status or "unknown",
        "run_live": run_live,
        "holder": str(rec.get("holder") or ""),
        "holder_kind": str(rec.get("holder_kind") or "unknown"),
        "client": str(rec.get("client") or ""),
        "held_since": str(rec.get("held_since") or rec.get("recorded_at") or ""),
        "renewed_at": str(rec.get("renewed_at") or rec.get("recorded_at") or ""),
        "idle_ttl_s": ttl_s,
        "ttl_remaining_s": ttl_remaining_s,
    }


def normalize_caller_kind(value: Any) -> str:
    """'human' | 'agent' — anything else (absent, unknown, junk) is AGENT:
    an undeclared caller never preempts (fails toward the human)."""
    s = str(value or "").strip().lower()
    return "human" if s == "human" else "agent"


def retry_after_s(seat: Dict[str, Any]) -> int:
    """Honest wait hint for a refusal: exact TTL remainder when the seat is
    only TTL-held; a small poll hint while the run is live."""
    if bool(seat.get("run_live")):
        return LIVE_RUN_RETRY_HINT_S
    return max(1, int(seat.get("ttl_remaining_s") or 0))


def door_decision(
    seat: Optional[Dict[str, Any]],
    *,
    caller: str,
    caller_kind: str,
    session_id: str,
) -> Dict[str, Any]:
    """The sealed priority matrix, as a pure decision.

    Returns {"action": "take" | "slide" | "preempt" | "refuse", ...}:
    - take    — seat free (or the caller's own idle seat): proceed fresh.
    - slide   — same session AND same holder: the conversation continuing;
                the recorder preserves held_since. Holder equality is the
                hijack guard: a foreign caller reusing a session id never
                inherits the seat.
    - preempt — human arriver, agent/unknown holder: cancel the holder's
                run tree if live, then proceed ("machinery yields to
                humans"). refuse carries retry_after_s.
    - refuse  — everything else (agents never displace; humans wait for
                other humans until the mailbox slice).
    """
    if seat is None:
        return {"action": "take"}
    ck = normalize_caller_kind(caller_kind)
    same_holder = bool(caller) and str(seat.get("holder") or "") == str(caller)
    if same_holder and str(seat.get("session_id") or "") == str(session_id):
        return {"action": "slide"}
    if ck == "human":
        holder_human = str(seat.get("holder_kind") or "") == "human"
        if not holder_human:
            return {"action": "preempt"}
        # Human holder. The same principal reclaiming an IDLE seat with a
        # fresh session is one human starting a new conversation — never a
        # queue behind themself. A LIVE run of their own (another window
        # mid-turn) still waits: preempting yourself tears your own turn.
        if same_holder and not bool(seat.get("run_live")):
            return {"action": "take"}
        return {"action": "refuse", "retry_after_s": retry_after_s(seat)}
    return {"action": "refuse", "retry_after_s": retry_after_s(seat)}


def cancel_run_tree(runtime: Any, run_store: Any, run_id: str, *, reason: str) -> list[str]:
    """Cancel a run and its descendants (root first — the runner's
    apply-to-tree shape). cancel_run is terminal-guarded runtime-side, so
    the walk is idempotent and a completed child is returned unchanged.
    Returns the run ids actually flipped to cancelled."""
    cancelled: list[str] = []
    seen: set[str] = set()
    queue = [str(run_id)]
    list_children = getattr(run_store, "list_children", None)
    while queue:
        rid = queue.pop(0)
        if rid in seen:
            continue
        seen.add(rid)
        try:
            before = _run_status(run_store, rid)
            runtime.cancel_run(rid, reason=reason)
            if before is not None and before not in _TERMINAL:
                cancelled.append(rid)
        except KeyError:
            continue  # already gone — nothing to preempt
        except Exception:
            logger.warning("preempt cancel failed for run %s (walk continues)", rid, exc_info=True)
        if callable(list_children):
            try:
                for child in list_children(parent_run_id=rid) or []:
                    cid = getattr(child, "run_id", None)
                    if isinstance(cid, str) and cid:
                        queue.append(cid)
            except Exception:
                pass
    return cancelled


def build_summon_seat_probe(run_store: Any, entities_dir: Path) -> Callable[[str], Optional[Dict[str, Any]]]:
    """The visit/chat doors' read of the summon seat (item 5's one helper).

    Returns the seat block only when its run is LIVE — a turn mid-flight is
    the one thing another lane must never interleave with. A TTL-idle seat
    does not block the drawer's own visit/chat opens (the operator switching
    lanes is a deliberate human act; summon-vs-summon keeps the full TTL)."""

    def probe(slug: str) -> Optional[Dict[str, Any]]:
        seat = seat_occupancy(run_store, entities_dir, str(slug or "").strip().lower())
        if seat is not None and bool(seat.get("run_live")):
            return seat
        return None

    return probe
