"""What a session is FOR: `conversation` (the default) or `docs` (a Docs
assistant chat, round 8 R8.3).

A purpose, never a client: every app and the console list the same pool of
sessions. Docs-assistant chats live in that pool (ledgered, archivable,
readable by `session_id`), and the turn listings (`GET /runs?root_only=true`)
default to conversations, so a docs chat never shows in a conversation list;
`kind=docs` lists them for the drawer's history.

The mark lives beside the session archive in the principal's runtime data dir
(`<data_dir>/session_kinds.json`); a session without a mark is a conversation
(that is the migration):

    {"schema": "gateway_session_kinds_v1",
     "sessions": {session_id: {"kind": "docs", "marked_at"}}}

Sessions of the shipped docs-qa workflow are docs chats whatever client
started them: `POST /runs/start` marks a docs-qa run's session, and a
one-time migration (`docs_qa_bundle_v1`, recorded in the file) marks every
existing session whose turn root ran docs-qa (matched on the workflow's
bundle id, never on text), so older docs chats leave the conversation lists.

An unreadable file is an error (the routes answer 500), never "no docs
sessions": that would put every docs chat back in the conversation lists.
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

SCHEMA = "gateway_session_kinds_v1"
FILENAME = "session_kinds.json"
KINDS = ("conversation", "docs")
DEFAULT_KIND = "conversation"
DOCS_QA_BUNDLE_ID = "docs-qa"
DOCS_QA_MIGRATION = "docs_qa_bundle_v1"

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
        for sid, rec in _load(data_dir)["sessions"].items():
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
    ref = wid.split(":", 1)[0]
    base = ref.rsplit("@", 1)[0] if "@" in ref else ref
    from .workflow_catalog import parse_catalog_internal_bundle_id

    parsed = parse_catalog_internal_bundle_id(base)
    return parsed[2] if parsed else (base or None)


def _write(data_dir: Any, data: Dict[str, Any]) -> None:
    path = _path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, str(path))


def docs_qa_migration_done(data_dir: Any) -> bool:
    with _LOCK:
        return DOCS_QA_MIGRATION in (_load(data_dir).get("migrations") or {})


def migrate_docs_qa_sessions(data_dir: Any, root_rows: Iterable[Dict[str, Any]]) -> int:
    """Mark (once) every session whose turn root ran the docs-qa bundle.
    Returns how many sessions it marked; a second call changes nothing."""
    with _LOCK:
        data = _load(data_dir)
        migrations = data.setdefault("migrations", {})
        if DOCS_QA_MIGRATION in migrations:
            return 0
        now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        marked = 0
        for row in root_rows:
            sid = str(row.get("session_id") or "").strip()
            if not sid or row.get("parent_run_id") or workflow_bundle_id(row.get("workflow_id")) != DOCS_QA_BUNDLE_ID:
                continue
            if (data["sessions"].get(sid) or {}).get("kind") != "docs":
                data["sessions"][sid] = {"kind": "docs", "marked_at": now, "by": DOCS_QA_MIGRATION}
                marked += 1
        migrations[DOCS_QA_MIGRATION] = {"at": now, "marked": marked}
        _write(data_dir, data)
        return marked


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
