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
import logging
import threading
from typing import Any, Dict, Iterable, Optional, Tuple

from .json_store import JsonStore, StoreBusy, StoreUnreadable, StoreUnwritable

_log = logging.getLogger("abstractgateway.session_kinds")

SCHEMA = "gateway_session_kinds_v1"
FILENAME = "session_kinds.json"
# Backlog 1166 (operator 2026-11-08 11:45): `automation` = an occurrence of an automation the
# user created; `process` = internal work the framework runs for its own treatments (Note's
# processor/reflect/consolidate occurrences, model-host verifications, the phone and watch
# voice probes — a probe is a process, never a kind of its own). An entity visit is a
# conversation with an entity: no `visit` kind.
KINDS = ("conversation", "docs", "notes", "automation", "process")
DEFAULT_KIND = "conversation"
DOCS_QA_BUNDLE_ID = "docs-qa"
DOCS_QA_MIGRATION = "docs_qa_bundle_v1"
#: AbstractNote's workflows (`abstractnote.flows.build.BUNDLE_ID`, loaded by abstractnote_seam.py).
NOTES_BUNDLE_ID = "abstractnote"
NOTES_MIGRATION = "abstractnote_bundle_v1"
#: AbstractNote's flows that are the framework's own treatments (exact flow ids,
#: `abstractnote.flows.build.BUILDERS`): processes, not notes a person captured.
PROCESS_NOTE_FLOWS = ("note-process", "note-reflect", "note-consolidate")
PROCESS_MIGRATION = "process_sessions_v1"
#: The run-index attribution (`abstractruntime.core.run_attribution`) that makes a row an
#: automation session: the controller and its occurrences. A `discussion` is a conversation.
AUTOMATION_SESSION_KINDS = ("automation", "occurrence")
AUTOMATION_MIGRATION = "automation_sessions_v1"
#: The bundle a session's purpose follows (Note's process flows are refined by `workflow_kind`).
BUNDLE_KINDS = {DOCS_QA_BUNDLE_ID: "docs", NOTES_BUNDLE_ID: "notes"}


def workflow_flow_id(workflow_id: Any) -> Optional[str]:
    """The flow id of a run's workflow id (`<bundle>@<version>:<flow>` -> `<flow>`), or None."""
    wid = str(workflow_id or "").strip()
    if ":" not in wid:
        return None
    return wid.split(":", 1)[1].strip() or None


def workflow_kind(workflow_id: Any, session_kind: Any = None) -> Optional[str]:
    """The purpose a row's OWN workflow and attribution imply, None for a conversation:
    `docs-qa` -> docs; an AbstractNote process flow (PROCESS_NOTE_FLOWS) -> process, any other
    AbstractNote flow -> notes; an automation controller or occurrence (`session_kind`) ->
    automation. Exact ids, never text."""
    bid = workflow_bundle_id(workflow_id)
    if bid == NOTES_BUNDLE_ID:
        return "process" if workflow_flow_id(workflow_id) in PROCESS_NOTE_FLOWS else "notes"
    if bid in BUNDLE_KINDS:
        return BUNDLE_KINDS[bid]
    if str(session_kind or "") in AUTOMATION_SESSION_KINDS:
        return "automation"
    return None


def _migration_matchers():
    def by_root_bundle(kind: str):
        # Sessions started through the door: their PARENT-LESS turn root ran the bundle.
        return lambda row: kind if not row.get("parent_run_id") and workflow_kind(row.get("workflow_id")) == kind else None

    def by_row(kind: str):
        # Occurrence sessions have a parent (their controller): every turn row counts.
        return lambda row: kind if workflow_kind(row.get("workflow_id"), row.get("session_kind")) == kind else None

    return (
        (DOCS_QA_MIGRATION, by_root_bundle("docs")),
        (NOTES_MIGRATION, by_root_bundle("notes")),
        (PROCESS_MIGRATION, by_row("process")),
        (AUTOMATION_MIGRATION, by_row("automation")),
    )


#: The one-time migrations, in order: (name, matcher(row) -> kind | None).
MIGRATIONS = _migration_matchers()


class SessionKindsUnreadable(RuntimeError):
    """The file exists but cannot be read as the schema."""


class SessionKindsUnwritable(RuntimeError):
    """The file could not be saved (json_store.py); it was left as it was."""


class SessionKindsBusy(SessionKindsUnwritable):
    """Another writer held the file past json_store.LOCK_WAIT_S; try again."""


def _valid(doc: Any) -> bool:
    return isinstance(doc, dict) and doc.get("schema") == SCHEMA and isinstance(doc.get("sessions"), dict)


def _load(data_dir: Any) -> Dict[str, Any]:
    try:
        return _STORE.load(data_dir)
    except StoreUnreadable as exc:
        raise SessionKindsUnreadable(str(exc)) from None


# Reads (cached: `GET /runs` reads this on every call; another process's write is seen within
# REVALIDATE_S, a write through this module at once), locking and the atomic write are
# json_store.py's: a folder that refuses new files is an immediate error, never a retry loop.
REVALIDATE_S = 1.0
_STORE = JsonStore(FILENAME, empty=lambda: {"schema": SCHEMA, "sessions": {}}, valid=_valid,
                   reader=lambda d: _load(d), revalidate_s=lambda: REVALIDATE_S)


def _load_cached(data_dir: Any) -> Dict[str, Any]:
    """The cached document, for READERS (never mutate the result)."""
    try:
        return _STORE.read(data_dir)
    except StoreUnreadable as exc:
        raise SessionKindsUnreadable(str(exc)) from None


def _update(data_dir: Any, change: Any) -> Any:
    try:
        return _STORE.update(data_dir, change)[0]
    except StoreUnreadable as exc:
        raise SessionKindsUnreadable(str(exc)) from None
    except StoreBusy as exc:
        raise SessionKindsBusy(str(exc)) from None
    except StoreUnwritable as exc:
        raise SessionKindsUnwritable(str(exc)) from None


def session_kinds(data_dir: Any) -> Dict[str, str]:
    """`{session_id: kind}` for every session that is not a conversation."""
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
    if bid == NOTES_BUNDLE_ID:
        flow = workflow_flow_id(flow_id) or str(flow_id or "").strip()
        return "process" if flow in PROCESS_NOTE_FLOWS else "notes"
    return BUNDLE_KINDS.get(bid or "")


def is_notes_workflow(workflow_id: Any) -> bool:
    """True when `workflow_id` is one of AbstractNote's workflows (`abstractnote@<version>:<flow>`)."""
    return workflow_bundle_id(workflow_id) == NOTES_BUNDLE_ID


def kind_of(kinds_map: Dict[str, str], session_id: Any, workflow_id: Any = None, session_kind: Any = None) -> str:
    """The purpose of a listed row: the session's mark, else what its own workflow and
    attribution imply (`workflow_kind`: Note's flows, docs-qa, an automation's controller or
    occurrence — the sessions automations open are never marked, module doc), else a
    conversation."""
    mark = kinds_map.get(str(session_id or ""))
    if mark:
        return mark
    return workflow_kind(workflow_id, session_kind) or DEFAULT_KIND


def pending_migrations(data_dir: Any) -> Tuple[str, ...]:
    """The one-time migrations (MIGRATIONS) this plane has not run yet."""
    done = _load_cached(data_dir).get("migrations") or {}
    return tuple(name for name, _matcher in MIGRATIONS if name not in done)


def migrate_sessions(data_dir: Any, root_rows: Iterable[Dict[str, Any]], names: Optional[Iterable[str]] = None) -> Dict[str, int]:
    """Run (once each) the pending migrations among `names` (all by default) over the turn rows
    `root_rows`: every session one of the migration's matcher names (docs-qa / Note roots by
    their parent-less turn root's bundle; process and automation sessions by any turn row's
    workflow and attribution) gets its kind, unless the session already has one. Returns
    `{migration: sessions marked}` for the migrations that ran now; a second call changes
    nothing. One read, one write."""
    rows = list(root_rows)

    def change(data: Dict[str, Any]) -> Any:
        migrations = data.setdefault("migrations", {})
        wanted = set(names) if names is not None else {name for name, _m in MIGRATIONS}
        now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        out: Dict[str, int] = {}
        for name, matcher in MIGRATIONS:
            if name not in wanted or name in migrations:
                continue
            marked = 0
            for row in rows:
                sid = str(row.get("session_id") or "").strip()
                kind = matcher(row) if sid else None
                if not kind:
                    continue
                if (data["sessions"].get(sid) or {}).get("kind") in (None, DEFAULT_KIND):
                    data["sessions"][sid] = {"kind": kind, "marked_at": now, "by": name}
                    marked += 1
            migrations[name] = {"at": now, "marked": marked}
            out[name] = marked
        return out, bool(out)

    return _update(data_dir, change)


_MIGRATION_SCAN_LOCK = threading.Lock()
# A plane whose folder refused the migrations' save is not tried again for MIGRATION_RETRY_S
# (every listing would pay the whole-index scan and the failed save): plane -> when it failed.
MIGRATION_RETRY_S = 60.0
_MIGRATION_FAILED: Dict[str, float] = {}


def run_pending_migrations(data_dir: Any, list_root_rows: Any) -> Dict[str, int]:
    """Run the pending migrations once: `list_root_rows()` (a whole-index scan) is called only
    when one is pending, and concurrent callers (every listing after an upgrade) wait for the
    first scan instead of repeating it. Blocking: call it off the event loop. A plane whose
    folder refuses the save keeps its marks as they are (the listing goes on); the migration is
    tried again after MIGRATION_RETRY_S, and the failure is logged once until it succeeds."""
    import time

    key = str(_STORE.path(data_dir))
    failed_at = _MIGRATION_FAILED.get(key)
    if failed_at is not None and time.monotonic() - failed_at < MIGRATION_RETRY_S:
        return {}
    if not pending_migrations(data_dir):
        return {}
    with _MIGRATION_SCAN_LOCK:
        pending = pending_migrations(data_dir)
        if not pending:
            return {}
        try:
            out = migrate_sessions(data_dir, list_root_rows(), pending)
        except SessionKindsUnwritable as exc:
            if key not in _MIGRATION_FAILED:
                _log.warning("session kinds: the one-time migrations could not be saved (%s); tried again every %.0f s", exc, MIGRATION_RETRY_S)
            _MIGRATION_FAILED[key] = time.monotonic()
            return {}
        _MIGRATION_FAILED.pop(key, None)
        return out


def mark_session_kind(data_dir: Any, session_id: str, kind: str) -> bool:
    """Record `kind` for `session_id` (idempotent). A conversation needs no
    mark. Returns True when the file changed."""
    if kind not in KINDS:
        raise ValueError(f"session kind must be one of {', '.join(KINDS)} (got {kind!r})")
    if kind == DEFAULT_KIND:
        return False
    sid = str(session_id)

    def change(data: Dict[str, Any]) -> Any:
        if (data["sessions"].get(sid) or {}).get("kind") == kind:
            return False, False
        data["sessions"][sid] = {"kind": kind, "marked_at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")}
        return True, True

    return bool(_update(data_dir, change))
