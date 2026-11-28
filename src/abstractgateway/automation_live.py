"""The live line of an automation's run in progress (automations wave 2026-11-27, spec C).

`GET /automations/{id}` serves `summary.current_occurrence.live`:

    {"started_at": iso, "state": "starting" | "thinking" | "tool" | "waiting",
     "tool": {"name", "arguments"} | null, "attempt": int, "steps_done": int}

folded ONCE here from the replay view the gateway already keeps for the attempt's run
(`replay_view.ViewEntry`, runtime `replay/view.py`), from typed fields only:

- `waiting`: the run tree holds a person-facing interaction (a question, a tool approval);
- else the newest `llm` or `tool` activity that has started and not ended: `thinking` (llm) or
  `tool` (with the call that is running); a container sub-workflow activity never counts;
- else `thinking` when an `llm`/`tool` activity already ended (between two steps), else
  `starting`.
- `steps_done`: the tool calls of this attempt's run tree that ended (completed, failed, denied).
- `started_at`: when the attempt's run was created; `attempt`: the attempt that is running.

Absent (None, never an error) when it cannot be computed: it exists only while the occurrence
is `running` (clients word `admitted` and `backoff` from `current_occurrence.status`), and only
for a run the store holds.

Tool arguments are bounded to what a one-line description needs (`bounded_arguments`): at most
`MAX_ARGUMENTS` top-level arguments, scalar values only (an object or a list is left out), each
string cut to its first `MAX_ARGUMENT_CHARS` characters.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MAX_ARGUMENTS = 8
MAX_ARGUMENT_CHARS = 200
_ENDED_CALL = frozenset({"completed", "failed", "denied"})
_COUNTED_KINDS = ("llm", "tool")

# (run id) -> (view cursor token, live without started_at/attempt): a poll whose view did not
# move is answered without parsing the view again.
_CACHE: "OrderedDict[str, Tuple[str, Dict[str, Any]]]" = OrderedDict()
_CACHE_MAX = 256
_CACHE_LOCK = threading.Lock()


def bounded_arguments(arguments: Any) -> Dict[str, Any]:
    if not isinstance(arguments, dict):
        return {}
    out: Dict[str, Any] = {}
    for key, value in arguments.items():
        if len(out) >= MAX_ARGUMENTS:
            break
        if isinstance(value, str):
            out[str(key)] = value[:MAX_ARGUMENT_CHARS]
        elif isinstance(value, (bool, int, float)) or value is None:
            out[str(key)] = value
    return out


def _running_tool(activity: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    rows = [r for r in activity.get("tool_calls") or [] if isinstance(r, dict)]
    row = next((r for r in rows if r.get("status") == "running"), None) or (rows[0] if rows else None)
    if row is None or not row.get("name"):
        return None
    return {"name": str(row["name"]), "arguments": bounded_arguments(row.get("arguments"))}


def fold_live(view: Dict[str, Any]) -> Dict[str, Any]:
    """`{state, tool, steps_done}` of a run tree's replay view (`ReplayView.to_json()`), pure."""
    activities: List[Dict[str, Any]] = [a for a in view.get("activities") or [] if isinstance(a, dict)]
    steps_done = sum(
        1
        for a in activities
        if a.get("kind") == "tool"
        for r in a.get("tool_calls") or []
        if isinstance(r, dict) and r.get("status") in _ENDED_CALL
    )
    if view.get("interaction"):
        return {"state": "waiting", "tool": None, "steps_done": steps_done}
    counted = [a for a in activities if a.get("kind") in _COUNTED_KINDS]
    open_ = [a for a in counted if a.get("status") == "running" and not a.get("ended_at")]
    if not open_:
        # Between two steps (one ended, the next not started yet) the run is thinking.
        return {"state": "thinking" if counted else "starting", "tool": None, "steps_done": steps_done}
    # `order` is the view's own `[started ts, run id, seq]` of the activity.
    newest = max(open_, key=lambda a: tuple(a.get("order") or ()))
    if newest["kind"] == "llm":
        return {"state": "thinking", "tool": None, "steps_done": steps_done}
    return {"state": "tool", "tool": _running_tool(newest), "steps_done": steps_done}


def live_for(svc: Any, current: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The `live` block of `current_occurrence` (None when it cannot be computed)."""
    if not isinstance(current, dict) or current.get("status") != "running" or not current.get("run_id"):
        return None
    run_id = str(current["run_id"])
    try:
        from .replay_view import get_view_hub, read_view

        run_store = svc.host.run_store
        run = run_store.load(run_id)
        if run is None:
            return None
        entry = get_view_hub().entry(ledger_store=svc.host.ledger_store, run_store=run_store, root_run_id=run_id)
        token, body, _delta = read_view(entry)
        with _CACHE_LOCK:
            hit = _CACHE.get(run_id)
        if hit is not None and hit[0] == token:
            folded = hit[1]
        else:
            folded = fold_live(json.loads(body))
            with _CACHE_LOCK:
                _CACHE[run_id] = (token, folded)
                _CACHE.move_to_end(run_id)
                while len(_CACHE) > _CACHE_MAX:
                    _CACHE.popitem(last=False)
    except Exception:  # noqa: BLE001 - the live line is a hint: absent, never a broken detail read
        logger.debug("automation live line of %s not computed", run_id, exc_info=True)
        return None
    return {
        "started_at": getattr(run, "created_at", None),
        "state": folded["state"],
        "tool": dict(folded["tool"]) if folded["tool"] else None,
        "attempt": int(current.get("attempt") or 1),
        "steps_done": int(folded["steps_done"]),
    }


__all__ = ["MAX_ARGUMENTS", "MAX_ARGUMENT_CHARS", "bounded_arguments", "fold_live", "live_for"]
