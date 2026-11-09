"""Idle -> sleep by the Mind's thresholds (contract-phases v3 D2, operator 2026-11-09: "idle -> sleep
after 24 h or 500 events, a Mind setting, triggered by the gateway").

Only an IDLE entity (no visit, no job queued or starting, no own time running, not stopped, not asleep)
is put to sleep, and only by this sweep: once `sleep.after_hours` have passed since his last completed
sleep, or `sleep.after_events` new memories have formed since it (the cognition profile, AbstractRuntime
`sleep_thresholds`). The sleep is his own (kind self) and runs its maintenance pass; when the pass is
done he is idle again — own time is never resumed by a sleep that did not interrupt it.

ADMISSION (Codex review 3 #1, #10): the generation is read BEFORE the idle check, and the sleep is
reserved through `set_state(admission=...)`: the home's writer lease is taken before the posture
changes (a home someone else is writing is not put to sleep at all, so no sleep is ever left standing
without its pass), then, under the lifecycle lock, the generation is checked against the idle check's
and the idle check runs again (still idle, no job queued or starting, the very state it saw). A Stop, a
visit or a job admitted meanwhile refuses the sleep with nothing written; the next sweep looks again.

ONE SLEEP PER SWEEP (Codex review 3 #11): every idle home is looked at, but only one due home is put
to sleep per sweep: the one waiting longest (the later of its last completed sleep and its last turn in
this process, the oldest first), so a home whose pass did not complete still goes behind the others. A
home refused at its admission keeps its place and the next due home is tried in the same sweep.

The boundary is the later of memory's last completed night (`records_since_maintenance`) and the
home's own record of its last completed maintenance (the loop's `maintenance_done` transitions in
its state history, the gateway's own maintenance receipts): without it every existing home would
read "never slept" and sleep at the first sweep, all at once. The receipt names which clock fired
(`boundary_source`).

An idle home whose thresholds are not reached gets the authoring drainer instead (pending titles
and summaries), the same bounded step a durable visit's close runs."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = ["SleepNotAdmitted", "idle_sweep", "idle_sweep_one", "sleep_due"]


class SleepNotAdmitted(Exception):
    """The idle sleep was refused at its reservation (`reason`); nothing was written."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# Each home's last turn at the idle sleep in this process (the rotation): a home that just had its turn
# goes behind every home still waiting, even when its pass did not complete.
_TURNS: Dict[str, float] = {}
_TURNS_LOCK = threading.Lock()


def _parse(ts: Any) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def last_maintenance_done(home_dir: Path, rec: Dict[str, Any]) -> Optional[str]:
    """The home's own newest completed maintenance (ISO UTC), or None: the loop's
    `phase_changed ... cause maintenance_done` rows and the gateway's completed pass receipt."""
    newest: Optional[datetime] = None
    path = Path(home_dir) / "state_history.jsonl"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("marker") == "phase_changed" and str(row.get("cause") or "") == "maintenance_done":
                at = _parse(row.get("at"))
                if at is not None and (newest is None or at > newest):
                    newest = at
    receipt = rec.get("last_maintenance") if isinstance(rec.get("last_maintenance"), dict) else None
    if receipt and receipt.get("status") == "done":
        at = _parse(receipt.get("at"))
        if at is not None and (newest is None or at > newest):
            newest = at
    return newest.isoformat() if newest is not None else None


def sleep_due(registry: Any, slug: str, *, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """The threshold that fired, with its receipt, or None."""
    from abstractruntime.integrations.abstractmemory.memory_facade import records_since_maintenance
    from abstractruntime.identity.cognition_profile import sleep_thresholds

    from .entities import DIARY_SCOPE, LIFE_SCOPE, SELF_SCOPE
    from .entity_phase import read_record

    entities_dir = Path(registry.entities_dir)
    home_dir = entities_dir / slug
    thresholds = sleep_thresholds(home_dir)
    after = last_maintenance_done(home_dir, read_record(entities_dir, slug))
    home = registry.get_home(slug)
    eid = str(home.entity_id)
    lived = records_since_maintenance(home.memory, scopes=[(SELF_SCOPE, eid), (DIARY_SCOPE, eid), (LIFE_SCOPE, eid)],
                                      after=after)
    count = int(lived.get("count") or 0)
    since = _parse(lived.get("since"))
    now = now or datetime.now(timezone.utc)
    hours = (now - since).total_seconds() / 3600.0 if since is not None else 0.0
    receipt = {"count": count, "hours": round(hours, 2), "since": lived.get("since"),
               "boundary_source": lived.get("boundary_source"), "thresholds": thresholds}
    if count >= int(thresholds["after_events"]):
        return {"threshold": "after_events",
                "reason": f"{int(thresholds['after_events'])} new memories since the last sleep", **receipt}
    if since is not None and hours >= float(thresholds["after_hours"]):
        return {"threshold": "after_hours",
                "reason": f"{int(thresholds['after_hours'])} h since the last sleep", **receipt}
    return None


def _state_seen(state: Dict[str, Any]) -> Tuple[str, ...]:
    return tuple(str(state.get(key) or "") for key in ("state", "mode", "written_by", "changed_at"))


def _not_idle(facts: Dict[str, Any], snap: Dict[str, Any], rec: Dict[str, Any]) -> Optional[str]:
    """Why this home may not be put to its idle sleep now, or None. The idle check and its recheck
    under the lifecycle lock are this one predicate."""
    from .entity_phase import SPAWN_CLAIM_TTL_S

    if snap["stopped"]:
        return "stopped"
    if facts.get("archived"):
        return "archived"
    if snap["phase"] != "idle":
        return str(snap["phase"])
    if int(facts.get("jobs_queued") or 0) > 0:
        return "job_queued"  # a queued job moves him to work (v3 D1), never to sleep
    claim = rec.get("spawning") if isinstance(rec.get("spawning"), dict) else None
    if claim and time.time() - float(claim.get("at") or 0) < SPAWN_CLAIM_TTL_S:
        return "starting"  # a loop is being started for him (a job, his own time)
    return None


def _look(registry: Any, slug: str, hosts: Tuple[Any, Any]) -> Optional[Dict[str, Any]]:
    """The idle check: None when the home is not idle; else its admission (the generation read BEFORE
    the check, the state the check saw) and the threshold that fired (`due`, None = under them)."""
    from .entity_phase import derive, gather, read_record

    rec = read_record(Path(registry.entities_dir), slug)
    facts = gather(registry, slug, chat_host=hosts[0], visit_host=hosts[1])
    if _not_idle(facts, derive(facts, rec), rec):
        return None
    return {"slug": slug, "generation": int(rec.get("generation") or 0), "state": _state_seen(facts["state"]),
            "due": sleep_due(registry, slug)}


def _drain(registry: Any, slug: str) -> Optional[Dict[str, Any]]:
    from .entity_authoring import drain_after_close

    drained = drain_after_close(registry, slug)
    return {"slug": slug, "action": "authoring", **drained} if drained.get("considered") else None


def _turn_key(registry: Any, slug: str) -> str:
    return str(Path(registry.entities_dir) / slug)


def _note_turn(registry: Any, slug: str) -> None:
    with _TURNS_LOCK:
        _TURNS[_turn_key(registry, slug)] = time.time()


def _waiting_since(registry: Any, look: Dict[str, Any]) -> float:
    since = _parse(look["due"].get("since"))
    with _TURNS_LOCK:
        turn = _TURNS.get(_turn_key(registry, look["slug"]), 0.0)
    return max(since.timestamp() if since is not None else 0.0, turn)


def _sleep(registry: Any, look: Dict[str, Any], hosts: Tuple[Any, Any]) -> Dict[str, Any]:
    """Reserve the home's sleep and run it to its end (set_state's maintenance pass), then wake him
    (idle) through the same completion as the operator's sleep."""
    from .entity_phase import IDLE_SWEEP_WRITER, derive, gather, maintenance_done, read_record

    slug, due = look["slug"], look["due"]
    entities_dir = Path(registry.entities_dir)

    def _admit() -> Optional[str]:
        # Run by set_state under the lifecycle lock, the generation already checked against the look's.
        rec = read_record(entities_dir, slug)
        facts = gather(registry, slug, chat_host=hosts[0], visit_host=hosts[1])
        refusal = _not_idle(facts, derive(facts, rec), rec)
        if refusal:
            return refusal
        return None if _state_seen(facts["state"]) == look["state"] else "state_changed"

    journal_seq = int(registry.get_home(slug).memory.current_seq())
    try:
        result = registry.set_state(name=slug, state="asleep", reason=due["reason"], dream=True,
                                    written_by=IDLE_SWEEP_WRITER, admission=(look["generation"], _admit))
    except SleepNotAdmitted as e:
        return {"slug": slug, "action": "idle_sleep_refused", "refused": e.reason, "threshold": due["threshold"]}
    _note_turn(registry, slug)
    try:
        from .entity_replay import record_host_marker

        record_host_marker(entities_dir=registry.entities_dir, slug=slug, entity_id=registry.manifest_for(slug).entity_id,
                           kind="idle_sleep", journal_seq=journal_seq, details={"channel": "idle_sweep", **due})
    except Exception:  # noqa: BLE001 - the receipt is best-effort, the sleep is the act
        logger.exception("idle sleep receipt failed for %s", slug)
    # The pass returned: he wakes (idle), unless a Stop or a visit took the home meanwhile. An admitted
    # sleep always ran its pass section, which names its generation.
    dream_out = result["dream"]
    outcome = maintenance_done(registry, slug, int(dream_out["generation"]),
                               failed=(str(dream_out.get("error")) if dream_out.get("error") else None),
                               superseded=bool(dream_out.get("superseded")),
                               cancelled_after=(str(dream_out["cancelled_after"]) if dream_out.get("cancelled_after") else None))
    return {"slug": slug, "action": "idle_sleep", "maintenance": outcome, **due}


def idle_sweep_one(registry: Any, slug: str) -> Optional[Dict[str, Any]]:
    """One idle home's turn: its threshold sleep (run to its end here), else the authoring drainer.
    None when the home is not idle."""
    from .entity_phase import default_hosts

    hosts = default_hosts()
    look = _look(registry, slug, hosts)
    if look is None:
        return None
    if look["due"] is None:
        return _drain(registry, slug)
    return _sleep(registry, look, hosts)


def idle_sweep(registry: Any, slugs: Iterable[str]) -> List[Dict[str, Any]]:
    """The repair sweep's idle step over every home: the authoring drainer for each idle home under
    its thresholds, and ONE threshold sleep, the due home waiting longest first (a home refused at its
    admission keeps its place; the next one is tried). Never raises: a broken home is a labelled row."""
    from .entity_phase import default_hosts

    hosts = default_hosts()
    rows: List[Dict[str, Any]] = []
    due: List[Dict[str, Any]] = []
    for slug in slugs:
        try:
            look = _look(registry, slug, hosts)
            if look is None:
                continue
            if look["due"] is not None:
                due.append(look)
                continue
            row = _drain(registry, slug)
        except Exception as e:  # noqa: BLE001
            row = {"slug": slug, "action": "idle_sweep", "error": f"{type(e).__name__}: {e}"}
            logger.warning("idle sweep failed for %s: %s", slug, e)
        if row is not None:
            rows.append(row)
    for look in sorted(due, key=lambda lk: (_waiting_since(registry, lk), lk["slug"])):
        try:
            row = _sleep(registry, look, hosts)
        except Exception as e:  # noqa: BLE001 - its turn is spent: a failing home never holds the others back
            _note_turn(registry, look["slug"])
            row = {"slug": look["slug"], "action": "idle_sleep", "error": f"{type(e).__name__}: {e}"}
            logger.warning("idle sleep failed for %s: %s", look["slug"], e)
        rows.append(row)
        if row["action"] != "idle_sleep_refused":
            break  # one sleep per sweep
    return rows
