"""A step's tools on the replay stream (root backlog 1204, the gateway half; the Entity app's ledger rows read it,
its backlog 0035).

ONE `step_tools` host marker per finished step that ran at least one tool, after the step's own rows:

    payload {kind: "step_tools", session_id: null, run_id, turn_id, tools: [names, call order],
             tool_count, files_read, files_written}

so a ledger row reads "⚙ 4 · 📄 2 · ✎ 1" with no fetch. `tools` and `tool_count` are the calls that RAN (a call the
grant or the turn's budget refused did not run); a step that ran no tool has no marker. Dedup on run_id + turn_id.

Two lanes, one marker:
- Runs on the gateway's entity runtime (durable visits, and every other run the door drives): the door's TOOL_CALLS
  handler notes each call that ran under the run's turn id (`note_executed_tools`, in run vars, so it persists with
  the run like the turn's tool budget); the step ends at its ANSWER_USER (answered, or failed with what it already
  did), whose wrap records the marker at the journal high-water (`wrap_answer_user`).
- Home-direct steps (own time and work ticks in the runtime's loop process, the hosted chat lane): AbstractRuntime's
  ChatSession forms each step's episode with `attributes.tools_used` (the tools that ran, in call order). The loop is
  another process with no run ledger, so the gateway sweeps the formations since its cursor (`sweep_step_tools`, on
  each stream read) and records the marker at the episode's formation seq. The cursor starts at the home's first
  sweep: the life lived before it is not backfilled (every marker append reads the marker file, so a backfill of a
  long life would cost its square).
Sleep: the night's passes (memory's maintenance, the topics, the self brief) run no tool today, so a sleep has no
marker; a home-direct sleep step that ran tools would form its episode like any other step and be swept.

Hot-path cost: the visit lane adds one list append per call that ran and one marker append per step with tools. The
sweep adds one `current_seq()` read per stream read or live poll; only when the journal moved, one binding-family
range read since the cursor and one record read per new formation.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "FILE_EFFECT_BY_TOOL",
    "STEP_TOOLS",
    "note_executed_tools",
    "record_step_tools",
    "step_tools_details",
    "sweep_step_tools",
    "sweep_step_tools_quietly",
    "wrap_answer_user",
]

STEP_TOOLS = "step_tools"

# THE ONE TABLE (root backlog 1204): whether a tool reads or writes a file's content. The core half of 1204 puts a
# `file_effect` fact on AbstractCore's tool definitions; until it exists this table is the only place where a tool
# name decides a count (no other name check anywhere), and it goes when the declaration lands. list_files,
# search_files and skim_folders list or search, they read no content; execute_command can do either (undecidable
# from the call): a tool, never a file.
FILE_EFFECT_BY_TOOL: Dict[str, str] = {
    "read_file": "read",
    "skim_files": "read",
    "write_file": "write",
    "edit_file": "write",
}

# Run vars key (under `_visit`, beside the turn's `tool_budget`): {turn_id, tools} for the turn in progress.
STEP_TOOLS_VAR = "step_tools"


def step_tools_details(run_id: str, turn_id: str, tools: Sequence[str]) -> Dict[str, Any]:
    """The marker's payload fields for one step: the names in call order and the three counts."""
    names = [str(t) for t in tools]
    return {
        "run_id": str(run_id),
        "turn_id": str(turn_id),
        "tools": names,
        "tool_count": len(names),
        "files_read": sum(1 for n in names if FILE_EFFECT_BY_TOOL.get(n) == "read"),
        "files_written": sum(1 for n in names if FILE_EFFECT_BY_TOOL.get(n) == "write"),
    }


def record_step_tools(*, entities_dir: Path, slug: str, entity_id: str, run_id: str, turn_id: str,
                      tools: Sequence[str], journal_seq: int) -> Optional[Dict[str, Any]]:
    """Append the step's marker anchored at `journal_seq` (deduped on run_id + turn_id): its envelope, or None
    when the step ran no tool."""
    if not tools:
        return None
    from .entity_replay import record_host_marker

    return record_host_marker(
        entities_dir=entities_dir, slug=slug, entity_id=entity_id, kind=STEP_TOOLS, journal_seq=int(journal_seq),
        run_id=str(run_id), details=step_tools_details(run_id, turn_id, tools), dedup_field=("run_id", "turn_id"),
    )


# ------------------------------------------------------------------ the lane of the gateway's entity runtime


def note_executed_tools(visit_ns: Dict[str, Any], turn_id: str, names: Sequence[str]) -> None:
    """Add the calls that ran in one TOOL_CALLS effect to the turn's list (`_visit.step_tools`, started afresh when
    the turn id changes). Called inside the owning tick, like the turn's tool budget."""
    if not names:
        return
    acc = visit_ns.get(STEP_TOOLS_VAR)
    if not isinstance(acc, dict) or str(acc.get("turn_id") or "") != str(turn_id):
        acc = {"turn_id": str(turn_id), "tools": []}
        visit_ns[STEP_TOOLS_VAR] = acc
    acc["tools"] = [*list(acc.get("tools") or []), *[str(n) for n in names]]


def wrap_answer_user(entity_runtime: Any, *, entities_dir: Path, slug: str) -> None:
    """The step ends at its ANSWER_USER: once it completed, the turn's noted tools become the step's marker, at the
    journal high-water (after the step's formation). A marker that cannot be written is logged and never fails the
    answer."""
    from abstractruntime.core.models import EffectType

    handlers = getattr(getattr(entity_runtime, "runtime", None), "_handlers", None)
    if not isinstance(handlers, dict) or EffectType.ANSWER_USER not in handlers:
        raise TypeError("the entity runtime exposes no ANSWER_USER handler to wrap (step_tools, root backlog 1204)")
    raw = handlers[EffectType.ANSWER_USER]
    home = entity_runtime.home
    entity_id = str(home.entity_id)

    def answer_user(run: Any, effect: Any, default_next_node: Any = None) -> Any:
        outcome = raw(run, effect, default_next_node)
        if str(getattr(outcome.status, "value", outcome.status)) != "completed":
            return outcome
        turn_id = str((effect.payload or {}).get("turn_id") or "")
        run_vars = run.vars if isinstance(run.vars, dict) else {}
        acc = (run_vars.get("_visit") or {}).get(STEP_TOOLS_VAR) if isinstance(run_vars.get("_visit"), dict) else None
        if not turn_id or not isinstance(acc, dict) or str(acc.get("turn_id") or "") != turn_id or not acc.get("tools"):
            return outcome
        try:
            record_step_tools(entities_dir=entities_dir, slug=slug, entity_id=entity_id, run_id=str(run.run_id),
                              turn_id=turn_id, tools=list(acc["tools"]), journal_seq=int(home.ms.current_seq()))
        except Exception:  # noqa: BLE001 - a marker never fails the step it annotates
            logger.exception("step_tools marker for %s (run %s, turn %s) failed", slug, run.run_id, turn_id)
        return outcome

    handlers[EffectType.ANSWER_USER] = answer_user


# ------------------------------------------------------------------ the home-direct lane (the loop's process)

_LOCKS_GUARD = threading.Lock()
_LOCKS: Dict[str, threading.Lock] = {}
# Per cursor file: the journal seq up to which this process has swept (the file holds the same, across restarts).
_SWEPT_TO: Dict[str, int] = {}


def _cursor_path(entities_dir: Path, slug: str) -> Path:
    from .entity_replay import HOST_STREAM_DIRNAME

    return Path(entities_dir) / HOST_STREAM_DIRNAME / f"{slug}.step_tools_cursor.json"


def _read_cursor(path: Path) -> Optional[int]:
    """The persisted cursor, None when this home was never swept. A file that says no seq raises (its writer is
    this module alone: a bad file is a bug to see, never a reason to start over and drop steps)."""
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    swept = data.get("swept_to") if isinstance(data, dict) else None
    if not isinstance(swept, int) or isinstance(swept, bool) or swept < 0:
        raise ValueError(f"{path} names no swept_to journal seq: {data!r}")
    return swept


def _write_cursor(path: Path, swept_to: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"swept_to": int(swept_to)}) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _tools_used(home: Any, record_id: str) -> List[str]:
    """The tools a formed record's step ran (`attributes.tools_used`, AbstractRuntime ChatSession), [] when none."""
    from abstractruntime.integrations.abstractmemory.memory_facade import resolve_digest_assertion

    assertion = resolve_digest_assertion(home.store, record_id) if record_id else None
    attrs = assertion.attributes if assertion is not None and isinstance(assertion.attributes, dict) else {}
    tools = attrs.get("tools_used")
    if tools is None:
        return []
    if not isinstance(tools, list) or not all(isinstance(t, str) and t.strip() for t in tools):
        logger.warning("step_tools: record %s carries tools_used that is not a list of names (%r); no marker",
                       record_id, tools)
        return []
    return [t.strip() for t in tools]


def sweep_step_tools(entities_dir: Path, slug: str, home: Any, *, until_seq: Optional[int] = None) -> int:
    """Record the `step_tools` marker of every home-direct step formed since the cursor (up to `until_seq` when
    given, else the journal high-water), each at its episode's formation seq, then move the cursor. Returns how
    many markers were newly recorded. The first sweep of a home only sets the cursor (no backfill)."""
    path = _cursor_path(entities_dir, slug)
    key = str(path)
    hi = int(home.memory.current_seq())
    if until_seq is not None:
        hi = min(hi, int(until_seq))
    known = _SWEPT_TO.get(key)
    if known is not None and hi <= known:
        return 0  # the fast path of every live poll: the journal did not move past the cursor
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, threading.Lock())
    with lock:
        swept = _SWEPT_TO.get(key)
        if swept is None:
            swept = _read_cursor(path)
            if swept is None:
                _write_cursor(path, hi)
                _SWEPT_TO[key] = hi
                return 0
            _SWEPT_TO[key] = swept
        if hi <= swept:
            return 0
        recorded = 0
        entity_id = str(home.entity_id)
        for env in home.memory.export_replay(since_seq=swept, until_seq=hi, families=["binding"], enrich=False):
            payload = env.get("payload") or {}
            if payload.get("source") != "remember":
                continue  # a formation's binding; later bindings of the same record are other acts
            tools = _tools_used(home, str(payload.get("record_id") or ""))
            if not tools:
                continue
            run_id, turn_id = str(env.get("run_id") or ""), str(env.get("turn_id") or "")
            if not run_id or not turn_id:
                logger.warning("step_tools: the formation at seq %s of %s ran tools but names no run or turn; "
                               "no marker", env.get("seq"), slug)
                continue
            marker = record_step_tools(entities_dir=entities_dir, slug=slug, entity_id=entity_id, run_id=run_id,
                                       turn_id=turn_id, tools=tools, journal_seq=int(env["seq"]))
            if marker is not None and not marker.get("deduped"):
                recorded += 1
        _write_cursor(path, hi)
        _SWEPT_TO[key] = hi
        return recorded


def sweep_step_tools_quietly(entities_dir: Path, slug: str, home: Any, *, until_seq: Optional[int] = None) -> int:
    """`sweep_step_tools` for a serving path: a failure is logged with its cause and the stream goes on (the cursor
    does not move, so the next read tries again)."""
    try:
        return sweep_step_tools(entities_dir, slug, home, until_seq=until_seq)
    except Exception:  # noqa: BLE001 - a marker never breaks the stream it annotates
        logger.exception("step_tools sweep for %s failed; the stream goes on without its new markers", slug)
        return 0
