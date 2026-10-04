"""Archived sessions (conversations): a mark, never a deletion.

A client archives a conversation to take it out of its list. Nothing about the
session changes: its runs, ledgers, artifacts and memory stay where they are,
and `GET /runs?session_id=...` still reads it. The mark lives in the
principal's runtime data dir (`<data_dir>/session_archive.json`, the same
plane as the session's runs, so a principal can only mark sessions it can
read):

    {"schema": "gateway_session_archive_v1",
     "sessions": {session_id: {"archived_at", "archived_by"}},
     "history":  [{"event": "archived"|"unarchived", "session_id", "at", "by"}]}

`history` keeps every archive / unarchive, newest last. An unreadable file is an
error (the routes answer 500), never an empty archive: an empty archive would
silently put every archived conversation back in the list.
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict

SCHEMA = "gateway_session_archive_v1"
FILENAME = "session_archive.json"

_LOCK = threading.RLock()


class SessionArchiveUnreadable(RuntimeError):
    """The archive file exists but cannot be read as the schema."""


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _path(data_dir: Any) -> Path:
    return Path(data_dir) / FILENAME


def _load(data_dir: Any) -> Dict[str, Any]:
    path = _path(data_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema": SCHEMA, "sessions": {}, "history": []}
    except Exception as exc:  # noqa: BLE001 - re-raised typed, with the path
        raise SessionArchiveUnreadable(f"{path} is not readable: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA or not isinstance(raw.get("sessions"), dict):
        raise SessionArchiveUnreadable(f"{path} is not a {SCHEMA} file")
    raw.setdefault("history", [])
    return raw


def _save(data_dir: Any, data: Dict[str, Any]) -> None:
    path = _path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, str(path))


def archived_sessions(data_dir: Any) -> Dict[str, Dict[str, Any]]:
    """`{session_id: {archived_at, archived_by}}` for this plane."""
    with _LOCK:
        return {str(k): dict(v) for k, v in _load(data_dir)["sessions"].items() if isinstance(v, dict)}


def archive_session(data_dir: Any, session_id: str, *, by: str) -> Dict[str, Any]:
    """Mark `session_id` archived. Archiving an archived session changes nothing (`changed: false`)."""
    sid = str(session_id)
    with _LOCK:
        data = _load(data_dir)
        current = data["sessions"].get(sid)
        if isinstance(current, dict):
            return {"session_id": sid, "archived": True, **current, "changed": False}
        at = _now()
        rec = {"archived_at": at, "archived_by": str(by)}
        data["sessions"][sid] = rec
        data["history"].append({"event": "archived", "session_id": sid, "at": at, "by": str(by)})
        _save(data_dir, data)
        return {"session_id": sid, "archived": True, **rec, "changed": True}


def unarchive_session(data_dir: Any, session_id: str, *, by: str) -> Dict[str, Any]:
    """Lift the mark. Unarchiving a session that is not archived changes nothing (`changed: false`)."""
    sid = str(session_id)
    with _LOCK:
        data = _load(data_dir)
        if data["sessions"].pop(sid, None) is None:
            return {"session_id": sid, "archived": False, "archived_at": None, "archived_by": None, "changed": False}
        data["history"].append({"event": "unarchived", "session_id": sid, "at": _now(), "by": str(by)})
        _save(data_dir, data)
        return {"session_id": sid, "archived": False, "archived_at": None, "archived_by": None, "changed": True}


__all__ = [
    "FILENAME",
    "SCHEMA",
    "SessionArchiveUnreadable",
    "archive_session",
    "archived_sessions",
    "unarchive_session",
]
