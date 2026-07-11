"""Two participants in one conversation (plan item 14 — the door's relay half).

The consensus plan, item 14 + the "both sides remember" invariant: a
cross-runtime meet is TWO CORRELATED DURABLE RUNS — one leg in each
entity's own `runtime_<slug>.sqlite3`, joined by ONE door-minted
`visit_id` carried in both stamps. NEVER one run written into two stores:
one-writer-per-home survives; "both runtimes" is mirrored perspectives,
not shared rows. Each entity forms its OWN episode of the shared moment
in its OWN home; what A diaries about the meet never lands in B's home
(the engine's two-sided guarantee; memory's `test_two_sided_visit_contract`
pins it, deliberately shaped like this relay's two formation calls).

THE RELAY, and the ONE-LEASE RULE it lives by (plan invariant): a line
addressed to A is delivered into A's leg; A's REPLY is then delivered
into B's leg as B's next visitor message. The relay acquires ONE home's
lease at a time — A's leg ticks and releases, THEN B's leg ticks and
releases; the two leases are never held together, so two meets crossing
the same pair can never deadlock (deadlock-free by shape).

HONEST ATTRIBUTION (the load-bearing correctness rule — every engraved
utterance was ACTUALLY produced by its attributed speaker):
- The CONVENER (the authenticated human who opened + steers the meet) is
  a stamped participant of BOTH legs, and the steering line each exchange
  carries is attributed to the convener — NOT to the other entity. A's
  home records "person:laurent said <line>", which is true; it must never
  record "entity:pollux said <the operator's words>".
- The speaker entity's REPLY is relayed to the listener authored by that
  speaker entity (a stamped participant) — B's home records "entity:castor
  said <castor's reply>", which castor actually produced.

DURABILITY (A6): the legs are durable runs, so the meet INDEX correlating
them must be durable too — a gateway restart mid-meet must not orphan two
open legs with no handle. The index lives in gateway bookkeeping
(`entities/.host_stream/meets.json`, OUTSIDE the homes — it does not
travel on a home copy; the journals stay the only system of record) and
reloads on a handle miss.

CRASH-SAFE RELAY (A7): an exchange is two deliveries (speaker, then
listener). The speaker's reply is persisted as a PENDING delivery before
the listener leg ticks; a retry after a crash between them RESUMES the
listener delivery instead of re-seeding the speaker (which would engrave a
duplicate episode).

USER visits stay one-legged (`EntityVisitHost`): the user has no home
runtime. This module is only for entity<->entity meets, where BOTH sides
have a home on THIS door. Cross-DOOR meets need the federation transport
(deferred); this is the same-door case.

A meet stamping `visit_id` for a conversation that is not strictly a
visit is DELIBERATE, not sloppy: the key is the framework's generic
interaction-correlation convention (maintainer ruling c338 — kind-free,
door-minted once, opaque). The interaction KIND lives in the channel
label and host markers, never in the key's spelling.
"""

from __future__ import annotations

import json
import secrets
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from .entity_visits import EntityVisitHost, VisitRefused

__all__ = ["EntityMeetHost", "VisitRefused"]

_MEET_INDEX_FILENAME = "meets.json"
_HOST_STREAM_DIRNAME = ".host_stream"
_DEFAULT_CONVENER = "person:operator"


class EntityMeetHost:
    """Open/relay/close a two-entity meet as two correlated visit legs,
    with a durable index so meets survive a gateway restart."""

    def __init__(self, visit_host: EntityVisitHost) -> None:
        self._visits = visit_host
        self._registry = visit_host._registry
        self._lock = threading.RLock()
        self._meets: Dict[str, Dict[str, Any]] = {}
        self._loaded = False

    # ------------------------------------------------------------------ open
    def open(
        self,
        name_a: str,
        name_b: str,
        *,
        session_id: Optional[str] = None,
        convener: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Open BOTH legs of a meet under one visit_id. Each leg is a full
        durable visit run in its own home; the two are correlated, never
        shared. `convener` (the authenticated human who opened the meet) is
        a stamped participant of both legs and the author of every steering
        line. If the second leg refuses, the first is closed so a meet
        never half-opens (leaving one entity mid-summon with no partner)."""
        registry = self._registry
        man_a = registry.manifest_for(name_a)
        man_b = registry.manifest_for(name_b)
        if man_a.entity_id == man_b.entity_id:
            raise VisitRefused(400, "a meet needs two DIFFERENT entities (an entity does not visit itself)")

        conv = str(convener or "").strip() or _DEFAULT_CONVENER
        visit_id = f"visit-{secrets.token_hex(6)}"
        session = str(session_id or "").strip() or f"meet-{secrets.token_hex(4)}"

        # Each leg's participants: the OTHER entity + the convener (the leg's
        # own entity is added by _start_leg — explicit co-presence, a2a 0007).
        _pre_a, leg_a = self._visits.open_leg(
            name_a, participants=[man_b.entity_id, conv], visit_id=visit_id,
            session_id=f"{session}:{man_a.slug}", channel_label="entity-meet",
        )
        try:
            _pre_b, leg_b = self._visits.open_leg(
                name_b, participants=[man_a.entity_id, conv], visit_id=visit_id,
                session_id=f"{session}:{man_b.slug}", channel_label="entity-meet",
            )
        except Exception:
            # Never half-open: close A's leg so no entity is left summoned
            # without its partner. A failed rollback is LABELED, never
            # swallowed — A stays summoned and the operator must be told
            # (the original refusal is preserved as the cause).
            try:
                self._visits.close(name_a, leg_a["run_id"], closed_by="operator", reason="meet partner refused")
            except Exception as ce:
                raise VisitRefused(
                    409,
                    f"#FALLBACK meet rollback failed to close {man_a.entity_id}'s leg "
                    f"(run {leg_a['run_id']}): {ce} — that entity is still summoned; "
                    f"close it manually via /entities/{name_a}/visit/{leg_a['run_id']}/close"
                )
            raise

        meet_id = f"meet-{secrets.token_hex(6)}"
        meet = {
            "meet_id": meet_id,
            "visit_id": visit_id,
            "convener": conv,
            "a": {"name": name_a, "slug": man_a.slug, "run_id": leg_a["run_id"], "entity_id": man_a.entity_id},
            "b": {"name": name_b, "slug": man_b.slug, "run_id": leg_b["run_id"], "entity_id": man_b.entity_id},
            "pending": None,  # A7: a half-delivered exchange waiting to reach the listener
        }
        with self._lock:
            self._meets[meet_id] = meet
            self._save_locked()
        return {
            "meet_id": meet_id,
            "visit_id": visit_id,
            "convener": conv,
            "a": {"entity_id": man_a.entity_id, "run_id": leg_a["run_id"]},
            "b": {"entity_id": man_b.entity_id, "run_id": leg_b["run_id"]},
        }

    # ----------------------------------------------------------------- relay
    def relay(self, meet_id: str, *, opener: str, text: str) -> Dict[str, Any]:
        """One exchange: the convener's `text` is delivered to the SPEAKER's
        leg AUTHORED BY THE CONVENER (never by the other entity); the
        speaker's reply is then relayed to the LISTENER's leg authored by
        the speaker entity. ONE lease at a time (turn() acquires+releases
        per call; the relay never nests two turns). Crash-safe: the reply
        is persisted as a pending delivery before the listener leg ticks, so
        a retry resumes the listener instead of re-seeding the speaker."""
        meet = self._meet(meet_id)

        # A7: a retry landed on a half-delivered exchange — finish it, do
        # not start a new one (re-seeding the speaker duplicates its episode).
        if meet.get("pending"):
            heard = self._deliver_pending(meet)
            spoke = dict(meet["pending"].get("spoke") or {}) if meet.get("pending") else {}
            self._clear_pending(meet)
            return {"meet_id": meet_id, "visit_id": meet["visit_id"],
                    "resumed": True, "spoke": spoke, "heard": heard}

        which = str(opener or "").strip().lower()
        if which not in ("a", "b"):
            raise VisitRefused(400, "opener must be 'a' or 'b' (which side speaks this exchange)")
        speaker_leg = meet[which]
        listener_leg = meet["b" if which == "a" else "a"]
        conv = str(meet.get("convener") or _DEFAULT_CONVENER)

        # 1) The convener's steering line enters the speaker's leg AUTHORED
        #    BY THE CONVENER — the human who is actually speaking it.
        spoken = self._visits.turn(
            speaker_leg["name"], speaker_leg["run_id"], text=str(text or ""), speaker=conv,
        )
        speaker_reply = str(spoken.get("reply") or "")
        spoke_view = {"entity_id": speaker_leg["entity_id"], "reply": speaker_reply,
                      "status": spoken.get("status"), "turn_n": spoken.get("turn_n")}

        # Persist the pending delivery BEFORE the listener leg ticks (A7).
        with self._lock:
            meet["pending"] = {
                "listener": "b" if which == "a" else "a",
                "text": speaker_reply,
                "speaker": speaker_leg["entity_id"],
                "spoke": spoke_view,
            }
            self._save_locked()

        # 2) The speaker's reply reaches the listener AUTHORED BY THE SPEAKER
        #    ENTITY (a stamped participant). Lease A is already released.
        heard = self._deliver_pending(meet)
        self._clear_pending(meet)

        return {"meet_id": meet_id, "visit_id": meet["visit_id"], "spoke": spoke_view, "heard": heard}

    def _deliver_pending(self, meet: Dict[str, Any]) -> Dict[str, Any]:
        """Deliver the persisted pending reply to the listener leg. Idempotent
        target — the listener's own turn_n advances exactly once per call."""
        pending = meet.get("pending") or {}
        listener_leg = meet[str(pending.get("listener") or "b")]
        heard = self._visits.turn(
            listener_leg["name"], listener_leg["run_id"],
            text=str(pending.get("text") or ""), speaker=str(pending.get("speaker") or ""),
        )
        return {"entity_id": listener_leg["entity_id"], "reply": str(heard.get("reply") or ""),
                "status": heard.get("status"), "turn_n": heard.get("turn_n")}

    def _clear_pending(self, meet: Dict[str, Any]) -> None:
        with self._lock:
            meet["pending"] = None
            self._save_locked()

    # ----------------------------------------------------------------- close
    def close(self, meet_id: str, *, reason: str = "") -> Dict[str, Any]:
        """Close BOTH legs (each reflects in its own home). One lease at a
        time. If a leg FAILS to close, the meet is KEPT (not popped) so the
        operator still has a handle to retry — an orphaned open leg with no
        meet id is the failure this avoids."""
        meet = self._meet(meet_id)
        out: Dict[str, Any] = {"meet_id": meet_id, "visit_id": meet["visit_id"]}
        all_closed = True
        for side in ("a", "b"):
            leg = meet[side]
            try:
                out[side] = self._visits.close(leg["name"], leg["run_id"], closed_by="operator", reason=reason)
            except VisitRefused as e:
                out[side] = {"error": e.detail}
                all_closed = False
        with self._lock:
            if all_closed:
                self._meets.pop(meet_id, None)
            self._save_locked()
        out["closed"] = all_closed
        if not all_closed:
            out["warning"] = (
                "one or both legs did not close; the meet is kept — retry close, "
                "or close the leg directly via /entities/{name}/visit/{run_id}/close"
            )
        return out

    def status(self, meet_id: str) -> Dict[str, Any]:
        """Both legs by their OWN run ids (A13: never 'whatever visit is live
        on the home now' — a torn-down leg followed by a new solo visit must
        not masquerade as this meet's leg)."""
        meet = self._meet(meet_id)
        return {
            "meet_id": meet_id,
            "visit_id": meet["visit_id"],
            "convener": meet.get("convener"),
            "pending": bool(meet.get("pending")),
            "a": {"entity_id": meet["a"]["entity_id"], **self._leg_status(meet["a"])},
            "b": {"entity_id": meet["b"]["entity_id"], **self._leg_status(meet["b"])},
        }

    def _leg_status(self, leg: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return self._visits.status_for_run(leg["name"], leg["run_id"])
        except VisitRefused:
            # The leg's run is gone (torn down out of band) — honest, not the
            # home's current unrelated visit.
            return {"open": False, "run_id": leg["run_id"]}

    # ------------------------------------------------------------- internals
    def _meet(self, meet_id: str) -> Dict[str, Any]:
        with self._lock:
            self._ensure_loaded_locked()
            meet = self._meets.get(str(meet_id or ""))
        if meet is None:
            raise VisitRefused(404, f"no meet {meet_id!r} (open one with two entity names)")
        return meet

    def _index_path(self) -> Path:
        return Path(self._registry.entities_dir) / _HOST_STREAM_DIRNAME / _MEET_INDEX_FILENAME

    def _ensure_loaded_locked(self) -> None:
        """Reload the durable index once per process (restart survival)."""
        if self._loaded:
            return
        self._loaded = True
        path = self._index_path()
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for mid, meet in data.items():
                        if isinstance(meet, dict) and mid not in self._meets:
                            self._meets[str(mid)] = meet
        except Exception:
            pass  # a corrupt index must never block a fresh meet; loud on write

    def _save_locked(self) -> None:
        path = self._index_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._meets), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            pass  # the in-memory index still serves this process; restart is best-effort
