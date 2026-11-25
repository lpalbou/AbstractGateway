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
silently put every archived conversation back in the list. Reads, locking and the
atomic write are `json_store.py`'s.
"""

from __future__ import annotations

import datetime
from typing import Any, Dict

from .json_store import JsonStore, StoreBusy, StoreUnreadable, StoreUnwritable

SCHEMA = "gateway_session_archive_v1"
FILENAME = "session_archive.json"
# How long a cached read is trusted (gateway load 2026-10-26: `GET /runs` reads this on every call);
# another process's write is seen within it, a write through this module at once.
REVALIDATE_S = 1.0


class SessionArchiveUnreadable(RuntimeError):
    """The archive file exists but cannot be read as the schema."""


class SessionArchiveUnwritable(RuntimeError):
    """The archive file could not be saved; it was left as it was."""


class SessionArchiveBusy(SessionArchiveUnwritable):
    """Another writer held the archive file past json_store.LOCK_WAIT_S; try again."""


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _valid(doc: Any) -> bool:
    return isinstance(doc, dict) and doc.get("schema") == SCHEMA and isinstance(doc.get("sessions"), dict)


def _load(data_dir: Any) -> Dict[str, Any]:
    doc = _STORE.load(data_dir)
    doc.setdefault("history", [])
    return doc


_STORE = JsonStore(FILENAME, empty=lambda: {"schema": SCHEMA, "sessions": {}, "history": []}, valid=_valid,
                   reader=lambda d: _load(d), revalidate_s=lambda: REVALIDATE_S)


def _read(data_dir: Any) -> Dict[str, Any]:
    try:
        return _STORE.read(data_dir)
    except StoreUnreadable as exc:
        raise SessionArchiveUnreadable(str(exc)) from None


def _update(data_dir: Any, change: Any) -> Dict[str, Any]:
    def run(doc: Dict[str, Any]) -> Any:
        doc.setdefault("history", [])
        return change(doc)

    try:
        return _STORE.update(data_dir, run)[0]
    except StoreUnreadable as exc:
        raise SessionArchiveUnreadable(str(exc)) from None
    except StoreBusy as exc:
        raise SessionArchiveBusy(str(exc)) from None
    except StoreUnwritable as exc:
        raise SessionArchiveUnwritable(str(exc)) from None


def archived_sessions(data_dir: Any) -> Dict[str, Dict[str, Any]]:
    """`{session_id: {archived_at, archived_by}}` for this plane."""
    return {str(k): dict(v) for k, v in _read(data_dir)["sessions"].items() if isinstance(v, dict)}


def archive_session(data_dir: Any, session_id: str, *, by: str) -> Dict[str, Any]:
    """Mark `session_id` archived. Archiving an archived session changes nothing (`changed: false`)."""
    sid = str(session_id)

    def change(data: Dict[str, Any]) -> Any:
        current = data["sessions"].get(sid)
        if isinstance(current, dict):
            return {"session_id": sid, "archived": True, **current, "changed": False}, False
        at = _now()
        rec = {"archived_at": at, "archived_by": str(by)}
        data["sessions"][sid] = rec
        data["history"].append({"event": "archived", "session_id": sid, "at": at, "by": str(by)})
        return {"session_id": sid, "archived": True, **rec, "changed": True}, True

    return _update(data_dir, change)


def unarchive_session(data_dir: Any, session_id: str, *, by: str) -> Dict[str, Any]:
    """Lift the mark. Unarchiving a session that is not archived changes nothing (`changed: false`)."""
    sid = str(session_id)

    def change(data: Dict[str, Any]) -> Any:
        if data["sessions"].pop(sid, None) is None:
            return {"session_id": sid, "archived": False, "archived_at": None, "archived_by": None, "changed": False}, False
        data["history"].append({"event": "unarchived", "session_id": sid, "at": _now(), "by": str(by)})
        return {"session_id": sid, "archived": False, "archived_at": None, "archived_by": None, "changed": True}, True

    return _update(data_dir, change)


def forget_archived_session(data_dir: Any, session_id: str) -> bool:
    """Drop the mark of a session that is gone (its last run purged), so a later conversation
    reusing the id is not born archived. Recorded in `history` as `purged`. True when one was dropped."""
    sid = str(session_id)

    def change(data: Dict[str, Any]) -> Any:
        if data["sessions"].pop(sid, None) is None:
            return False, False
        data["history"].append({"event": "purged", "session_id": sid, "at": _now(), "by": "draft purge"})
        return True, True

    return bool(_update(data_dir, change))


__all__ = [
    "FILENAME",
    "SCHEMA",
    "SessionArchiveBusy",
    "SessionArchiveUnreadable",
    "SessionArchiveUnwritable",
    "forget_archived_session",
    "archive_session",
    "archived_sessions",
    "unarchive_session",
]
