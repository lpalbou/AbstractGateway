"""Per-home task inbox — the G3 door half (plan v18, gateway §1).

One append-only EVENT LOG per entity home (`<home>/task_inbox.jsonl`): the
durable record of tasks left with an entity, written by door surfaces (the
tasks endpoint, visit close) and — when runtime's R-C loop half lands —
status-advanced by the day-open reader. Living in the HOME means the inbox
travels on directory copy like tool_policy.yaml/substrate.yaml.

Design rules:

- APPEND-ONLY EVENTS, state is a fold. Two legitimate writer PROCESSES
  exist (the gateway door and the entity's own-time loop), so read-modify-
  write on a single JSON document would lose updates. Instead every write
  is one flock-guarded appended line; `read_task_inbox` folds the log into
  current task state. This is the same shape as the host marker stream and
  the loop's durable inboxes — the append is the act, the fold is the view.
- RULING-NEUTRAL under D1 (auto-vs-elect work entry): this module records
  FACTS about tasks; who opens the work phase — and when — is runtime's
  ruled behavior, not encoded here.
- The file schema is a CROSS-PACKAGE CONTRACT: runtime's R-C reads this
  file (they cannot import abstractgateway — dependency direction), exactly
  like the gateway reads runtime-owned home files (phases.yaml,
  loop_spend.json). Schema changes must be coordinated on the room record.

Event shapes (one JSON object per line):

    {"event": "added", "task_id", "title", "brief", "origin", "at", "by",
     "workflow"?: {...}, "backlog_ref"?: str}
    {"event": "status", "task_id", "status": pending|taken|done|parked,
     "at", "by", "note"?: str}

`origin` is stamped by the CALLER-SIDE door from the authenticated
principal or the verified visit (never from payload claims — deposit-gate
discipline). Task status words reuse the plain-word work register
(pending|taken|done|parked — collision-swept by semantics,
decision:g3-marker-spellings). The work-entry CAUSE slot on the
phase_changed axis stays RESERVED AND UNSPELLED here — its word gets its
own semantics pass when runtime's loop half lands (two axes, two
spellings; nothing in this module pre-rules the cause word).
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

TASK_INBOX_FILENAME = "task_inbox.jsonl"

TASK_STATUSES = ("pending", "taken", "done", "parked")

# Ceilings keep a fat-fingered paste from becoming every day-open's read tax.
_MAX_TITLE_CHARS = 500
_MAX_BRIEF_CHARS = 20000


def _utc_now_iso() -> str:
    from abstractruntime.core.runtime import utc_now_iso

    return utc_now_iso()


def task_inbox_path(home_dir: Any) -> Path:
    return Path(home_dir) / TASK_INBOX_FILENAME


def _append_event(home_dir: Any, event: Dict[str, Any]) -> None:
    """One flock-guarded appended line — safe under the two-process reality
    (gateway door + own-time loop). The lock is held only for the single
    write+flush; readers never lock (a torn tail line is skipped by the
    fold, and the next append repairs nothing because appends never rewrite)."""
    path = task_inbox_path(home_dir)
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        try:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            # Non-POSIX platforms degrade to plain append — still atomic for
            # single-line writes on local filesystems.
            pass
        try:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            try:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass


def append_task(
    home_dir: Any,
    *,
    title: str,
    brief: str = "",
    origin: str,
    by: str,
    workflow: Optional[Dict[str, Any]] = None,
    backlog_ref: Optional[str] = None,
) -> Dict[str, Any]:
    """Record a task left with the entity. Returns the added event.

    `origin`/`by` are the door's stamped truth (authenticated principal or
    verified visit id) — validation of WHO happens at the door, never here.
    """
    title2 = str(title or "").strip()
    if not title2:
        raise ValueError("a task needs a non-empty title")
    if len(title2) > _MAX_TITLE_CHARS:
        raise ValueError(f"task title exceeds {_MAX_TITLE_CHARS} chars")
    brief2 = str(brief or "").strip()
    if len(brief2) > _MAX_BRIEF_CHARS:
        raise ValueError(f"task brief exceeds {_MAX_BRIEF_CHARS} chars")
    origin2 = str(origin or "").strip()
    if not origin2:
        raise ValueError("origin is required (door-stamped, never payload-claimed)")
    event: Dict[str, Any] = {
        "event": "added",
        "task_id": uuid.uuid4().hex[:12],
        "title": title2,
        "brief": brief2,
        "origin": origin2,
        "at": _utc_now_iso(),
        "by": str(by or "").strip() or origin2,
    }
    if isinstance(workflow, dict) and workflow:
        event["workflow"] = dict(workflow)
    if isinstance(backlog_ref, str) and backlog_ref.strip():
        event["backlog_ref"] = backlog_ref.strip()
    _append_event(home_dir, event)
    return event


def set_task_status(
    home_dir: Any,
    *,
    task_id: str,
    status: str,
    by: str,
    note: Optional[str] = None,
) -> Dict[str, Any]:
    """Advance a task's status (operator repair or the loop's own progress).

    Raises KeyError for an unknown task — recording a status event for a
    task that never existed would make the fold lie.
    """
    status2 = str(status or "").strip().lower()
    if status2 not in TASK_STATUSES:
        raise ValueError(f"unknown task status {status!r} (one of {TASK_STATUSES})")
    tid = str(task_id or "").strip()
    known = {t["task_id"] for t in read_task_inbox(home_dir)["tasks"]}
    if tid not in known:
        raise KeyError(f"unknown task {task_id!r}")
    event: Dict[str, Any] = {
        "event": "status",
        "task_id": tid,
        "status": status2,
        "at": _utc_now_iso(),
        "by": str(by or "").strip() or "operator",
    }
    if isinstance(note, str) and note.strip():
        event["note"] = note.strip()
    _append_event(home_dir, event)
    return event


def read_task_inbox(home_dir: Any) -> Dict[str, Any]:
    """Fold the event log into current task state (chronological).

    Absent file => {"exists": False, "tasks": [], "pending": 0} — consumers
    feature-detect on `exists` (render-when-present; an entity that was
    never handed a task shows NO task surface, not an empty one).
    Unparseable lines are skipped with a warning count, never a crash — a
    poll-shaped read must survive a torn tail (scan-lane rule).
    """
    path = task_inbox_path(home_dir)
    if not path.is_file():
        return {"exists": False, "tasks": [], "pending": 0}
    tasks: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    skipped = 0
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return {"exists": True, "tasks": [], "pending": 0, "warning": "#FALLBACK task inbox unreadable"}
    for raw in raw_lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except Exception:
            skipped += 1
            continue
        if not isinstance(ev, dict):
            skipped += 1
            continue
        kind = str(ev.get("event") or "")
        tid = str(ev.get("task_id") or "")
        if not tid:
            skipped += 1
            continue
        if kind == "added":
            entry = {
                "task_id": tid,
                "title": str(ev.get("title") or ""),
                "brief": str(ev.get("brief") or ""),
                "origin": str(ev.get("origin") or ""),
                "status": "pending",
                "created_at": str(ev.get("at") or ""),
                "updated_at": str(ev.get("at") or ""),
                "by": str(ev.get("by") or ""),
            }
            if isinstance(ev.get("workflow"), dict):
                entry["workflow"] = dict(ev["workflow"])
            if isinstance(ev.get("backlog_ref"), str) and ev["backlog_ref"].strip():
                entry["backlog_ref"] = ev["backlog_ref"].strip()
            if tid not in tasks:
                order.append(tid)
            tasks[tid] = entry
        elif kind == "status":
            entry = tasks.get(tid)
            if entry is None:
                # A status event for an unknown task: the add line was torn
                # or pruned — surface honestly rather than fabricating.
                skipped += 1
                continue
            status2 = str(ev.get("status") or "").strip().lower()
            if status2 in TASK_STATUSES:
                entry["status"] = status2
                entry["updated_at"] = str(ev.get("at") or entry["updated_at"])
                if isinstance(ev.get("note"), str) and ev["note"].strip():
                    entry["status_note"] = ev["note"].strip()
        else:
            skipped += 1
    folded = [tasks[tid] for tid in order]
    out: Dict[str, Any] = {
        "exists": True,
        "tasks": folded,
        "pending": sum(1 for t in folded if t["status"] == "pending"),
    }
    if skipped:
        out["warning"] = f"#FALLBACK {skipped} unparseable/orphan inbox line(s) skipped"
    return out


def count_pending(home_dir: Any) -> Optional[int]:
    """pending count for summaries — None when NO inbox exists (the
    render-when-present contract: absent field, not zero)."""
    folded = read_task_inbox(home_dir)
    if not folded.get("exists"):
        return None
    return int(folded.get("pending") or 0)