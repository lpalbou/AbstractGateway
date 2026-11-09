"""Idle -> sleep by the Mind's thresholds (contract-phases v3 D2, operator 2026-11-09: "idle -> sleep
after 24 h or 500 events, a Mind setting, triggered by the gateway").

Only an IDLE entity (no visit, no job, no own time running, not stopped, not asleep) is put to sleep,
and only by this sweep: once `sleep.after_hours` have passed since his last completed sleep, or
`sleep.after_events` new memories have formed since it (the cognition profile, AbstractRuntime
`sleep_thresholds`). The sleep is his own (kind self) and runs its maintenance pass; when the pass is
done he is idle again — own time is never resumed by a sleep that did not interrupt it.

The boundary is the later of memory's last completed night (`records_since_maintenance`) and the
home's own record of its last completed maintenance (the loop's `maintenance_done` transitions in
its state history, the gateway's own maintenance receipts): without it every existing home would
read "never slept" and sleep at the first sweep, all at once. The receipt names which clock fired
(`boundary_source`). Several homes due in one sweep sleep one after another, never in parallel.

An idle home whose thresholds are not reached gets the authoring drainer instead (pending titles
and summaries), the same bounded step a durable visit's close runs."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

__all__ = ["idle_sweep_one", "sleep_due"]


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


def idle_sweep_one(registry: Any, slug: str) -> Optional[Dict[str, Any]]:
    """One idle home's turn in the sweep: its threshold sleep (run to its end here, so homes sleep
    one after another), else the authoring drainer. None when the home is not idle."""
    from .entity_phase import IDLE_SWEEP_WRITER, default_hosts, derive, gather, read_record

    chat_host, visit_host = default_hosts()
    facts = gather(registry, slug, chat_host=chat_host, visit_host=visit_host)
    snap = derive(facts, read_record(Path(registry.entities_dir), slug))
    if snap["phase"] != "idle" or snap["stopped"] or facts.get("archived"):
        return None
    due = sleep_due(registry, slug)
    if due is None:
        from .entity_authoring import drain_after_close

        drained = drain_after_close(registry, slug)
        return {"slug": slug, "action": "authoring", **drained} if drained.get("considered") else None
    manifest = registry.manifest_for(slug)
    try:
        from .entity_replay import record_host_marker

        record_host_marker(entities_dir=registry.entities_dir, slug=slug, entity_id=manifest.entity_id,
                           kind="idle_sleep", journal_seq=int(registry.get_home(slug).memory.current_seq()),
                           details={"channel": "idle_sweep", **due})
    except Exception:  # noqa: BLE001 - the receipt is best-effort, the sleep is the act
        logger.exception("idle sleep receipt failed for %s", slug)
    result = registry.set_state(name=slug, state="asleep", reason=due["reason"], dream=True,
                                written_by=IDLE_SWEEP_WRITER)
    # The pass returned: he wakes (idle) through the same completion as the operator's sleep, unless a
    # Stop or a visit took the home meanwhile.
    dream_out = result.get("dream") if isinstance(result, dict) and isinstance(result.get("dream"), dict) else None
    outcome = None
    if dream_out and dream_out.get("generation") is not None:
        from .entity_phase import maintenance_done

        outcome = maintenance_done(registry, slug, int(dream_out["generation"]),
                                   failed=(str(dream_out.get("error")) if dream_out.get("error") else None),
                                   superseded=bool(dream_out.get("superseded")))
    return {"slug": slug, "action": "idle_sleep", "maintenance": outcome, **due}
