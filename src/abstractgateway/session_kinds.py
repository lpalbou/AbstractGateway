"""What a session is FOR: `conversation` (the default), `docs` (a Docs
assistant chat, round 8 R8.3) or `notes` (AbstractNote's own background work).

A purpose, never a client: every app and the console list the same pool of
sessions. Docs-assistant chats and note sessions live in that pool (ledgered,
archivable, readable by `session_id`), and the turn listings
(`GET /runs?root_only=true`) default to conversations, so neither shows in a
conversation list; `kind=docs` / `kind=notes` list them.

The mark lives beside the session archive in the principal's runtime data dir
(`<data_dir>/session_kinds.json`); a session without a mark is a conversation
(that is the migration), except a note session (below):

    {"schema": "gateway_session_kinds_v1",
     "sessions": {session_id: {"kind": "docs" | "notes", "marked_at"}}}

A purpose follows the WORKFLOW's bundle id (exact, never text), whatever client
started the run:

- `docs-qa` -> docs. `POST /runs/start` marks a docs-qa run's session, and a
  one-time migration (`docs_qa_bundle_v1`, recorded in the file) marked every
  existing session whose turn root ran docs-qa.
- `abstractnote` (AbstractNote's workflows, `abstractnote@<version>:<flow>`) ->
  notes. `POST /runs/start` marks the session of a note run (a capture from
  AbstractNote, its phone or watch), and a one-time migration
  (`abstractnote_bundle_v1`) marks every existing session whose parent-less
  turn root ran one. The sessions an AUTOMATION opens (Note's processor,
  reflect and consolidate automations: one session per occurrence in their
  fresh-context mode) never pass through `POST /runs/start`; they are notes by
  their root's workflow at read time (`kind_of`), so the file does not grow by
  one entry per background occurrence.

An unreadable file is an error (the routes answer 500), never "no docs
sessions": that would put every docs chat back in the conversation lists.
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

SCHEMA = "gateway_session_kinds_v1"
FILENAME = "session_kinds.json"
KINDS = ("conversation", "docs", "notes")
DEFAULT_KIND = "conversation"
DOCS_QA_BUNDLE_ID = "docs-qa"
DOCS_QA_MIGRATION = "docs_qa_bundle_v1"
#: AbstractNote's workflows (`abstractnote.flows.build.BUNDLE_ID`, loaded by abstractnote_seam.py).
NOTES_BUNDLE_ID = "abstractnote"
NOTES_MIGRATION = "abstractnote_bundle_v1"
#: The bundle a session's purpose follows.
BUNDLE_KINDS = {DOCS_QA_BUNDLE_ID: "docs", NOTES_BUNDLE_ID: "notes"}
#: The one-time migrations, in order: (name, bundle id, kind).
MIGRATIONS = ((DOCS_QA_MIGRATION, DOCS_QA_BUNDLE_ID, "docs"), (NOTES_MIGRATION, NOTES_BUNDLE_ID, "notes"))

_LOCK = threading.RLock()


class SessionKindsUnreadable(RuntimeError):
    """The file exists but cannot be read as the schema."""


def _path(data_dir: Any) -> Path:
    return Path(data_dir) / FILENAME


def _load(data_dir: Any) -> Dict[str, Any]:
    path = _path(data_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema": SCHEMA, "sessions": {}}
    except Exception as exc:  # noqa: BLE001 - re-raised typed, with the path
        raise SessionKindsUnreadable(f"{path} is not readable: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA or not isinstance(raw.get("sessions"), dict):
        raise SessionKindsUnreadable(f"{path} is not a {SCHEMA} file")
    return raw


def session_kinds(data_dir: Any) -> Dict[str, str]:
    """`{session_id: kind}` for every session that is not a conversation."""
    with _LOCK:
        out: Dict[str, str] = {}
        for sid, rec in _load_cached(data_dir)["sessions"].items():
            kind = rec.get("kind") if isinstance(rec, dict) else None
            if kind in KINDS and kind != DEFAULT_KIND:
                out[str(sid)] = str(kind)
        return out


def workflow_bundle_id(workflow_id: Any) -> Optional[str]:
    """The public bundle id of a run's workflow id (`<bundle>@<version>:<flow>`,
    a catalog bundle's internal id decoded), or None."""
    wid = str(workflow_id or "").strip()
    if ":" not in wid:
        return None
    return bundle_base_id(wid.split(":", 1)[0])


def bundle_base_id(bundle_ref: Any) -> Optional[str]:
    """The public bundle id of a bundle reference (`<bundle>[@<version>]`, a catalog bundle's
    internal id decoded), or None."""
    ref = str(bundle_ref or "").strip()
    if not ref:
        return None
    base = ref.rsplit("@", 1)[0] if "@" in ref else ref
    from .workflow_catalog import parse_catalog_internal_bundle_id

    parsed = parse_catalog_internal_bundle_id(base)
    return parsed[2] if parsed else (base or None)


def start_kind(bundle_id: Any, flow_id: Any) -> Optional[str]:
    """The purpose a run start implies (`POST /runs/start`): the kind of the requested bundle
    (`bundle_id`, else the bundle of a namespaced `flow_id`), None for a conversation."""
    bid = bundle_base_id(bundle_id) or workflow_bundle_id(flow_id)
    return BUNDLE_KINDS.get(bid or "")


def is_notes_workflow(workflow_id: Any) -> bool:
    """True when `workflow_id` is one of AbstractNote's workflows (`abstractnote@<version>:<flow>`)."""
    return workflow_bundle_id(workflow_id) == NOTES_BUNDLE_ID


def kind_of(kinds_map: Dict[str, str], session_id: Any, workflow_id: Any = None) -> str:
    """The purpose of a listed row: the session's mark, else `notes` when the row runs an
    AbstractNote workflow (the sessions Note's automations open are never marked, module doc),
    else a conversation."""
    mark = kinds_map.get(str(session_id or ""))
    if mark:
        return mark
    return "notes" if is_notes_workflow(workflow_id) else DEFAULT_KIND


# Read cache (gateway load 2026-10-26): `GET /runs` read this file on every call, on the event
# loop. Readers get the last parsed content; the file is stat-ed again at most every
# REVALIDATE_S (another process's write is seen within that) and a write through this module
# drops the entry at once. An unreadable file is never cached (it raises on every read).
REVALIDATE_S = 1.0
_READ_CACHE: Dict[str, Tuple[float, Any, Dict[str, Any]]] = {}


def _load_cached(data_dir: Any) -> Dict[str, Any]:
    """`_load` for READERS (never mutate the result)."""
    path = _path(data_dir)
    key = str(path)
    now = time.monotonic()
    hit = _READ_CACHE.get(key)
    if hit is not None and (now - hit[0]) < REVALIDATE_S:
        return hit[2]
    try:
        st = os.stat(path)
        sig: Any = (st.st_mtime_ns, st.st_size, st.st_ino)
    except FileNotFoundError:
        sig = None
    except OSError:
        sig = object()  # never equal: read (and raise) below
    if hit is not None and hit[1] == sig:
        _READ_CACHE[key] = (now, sig, hit[2])
        return hit[2]
    data = _load(data_dir)
    _READ_CACHE[key] = (now, sig, data)
    return data


def _forget(data_dir: Any) -> None:
    _READ_CACHE.pop(str(_path(data_dir)), None)


def _write(data_dir: Any, data: Dict[str, Any]) -> None:
    _forget(data_dir)
    path = _path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, str(path))
    _forget(data_dir)


def pending_migrations(data_dir: Any) -> Tuple[str, ...]:
    """The one-time migrations (MIGRATIONS) this plane has not run yet."""
    with _LOCK:
        done = _load_cached(data_dir).get("migrations") or {}
        return tuple(name for name, _bundle, _kind in MIGRATIONS if name not in done)


def migrate_sessions(data_dir: Any, root_rows: Iterable[Dict[str, Any]], names: Optional[Iterable[str]] = None) -> Dict[str, int]:
    """Run (once each) the pending migrations among `names` (all by default) over the turn roots
    `root_rows`: every session whose PARENT-LESS turn root ran the migration's bundle gets its
    kind, unless the session already has one. Returns `{migration: sessions marked}` for the
    migrations that ran now; a second call changes nothing. One read, one write."""
    rows = list(root_rows)
    with _LOCK:
        data = _load(data_dir)
        migrations = data.setdefault("migrations", {})
        wanted = set(names) if names is not None else {name for name, _b, _k in MIGRATIONS}
        now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        out: Dict[str, int] = {}
        for name, bundle, kind in MIGRATIONS:
            if name not in wanted or name in migrations:
                continue
            marked = 0
            for row in rows:
                sid = str(row.get("session_id") or "").strip()
                if not sid or row.get("parent_run_id") or workflow_bundle_id(row.get("workflow_id")) != bundle:
                    continue
                if (data["sessions"].get(sid) or {}).get("kind") in (None, DEFAULT_KIND):
                    data["sessions"][sid] = {"kind": kind, "marked_at": now, "by": name}
                    marked += 1
            migrations[name] = {"at": now, "marked": marked}
            out[name] = marked
        if out:
            _write(data_dir, data)
        return out


_MIGRATION_SCAN_LOCK = threading.Lock()


def run_pending_migrations(data_dir: Any, list_root_rows: Any) -> Dict[str, int]:
    """Run the pending migrations once: `list_root_rows()` (a whole-index scan) is called only
    when one is pending, and concurrent callers (every listing after an upgrade) wait for the
    first scan instead of repeating it. Blocking: call it off the event loop."""
    if not pending_migrations(data_dir):
        return {}
    with _MIGRATION_SCAN_LOCK:
        pending = pending_migrations(data_dir)
        if not pending:
            return {}
        return migrate_sessions(data_dir, list_root_rows(), pending)


def mark_session_kind(data_dir: Any, session_id: str, kind: str) -> bool:
    """Record `kind` for `session_id` (idempotent). A conversation needs no
    mark. Returns True when the file changed."""
    if kind not in KINDS:
        raise ValueError(f"session kind must be one of {', '.join(KINDS)} (got {kind!r})")
    if kind == DEFAULT_KIND:
        return False
    sid = str(session_id)
    with _LOCK:
        data = _load(data_dir)
        if (data["sessions"].get(sid) or {}).get("kind") == kind:
            return False
        data["sessions"][sid] = {"kind": kind, "marked_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")}
        _write(data_dir, data)
        return True
