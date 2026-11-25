"""Session (conversation) titles: the name a person gives a conversation.

Without one, every client names a conversation after its opening message. A stored
title replaces that name everywhere `GET /runs` is read; clearing it brings the
automatic name back. Nothing about the session changes: its runs, ledgers and the
opening message stay as they are. The titles live in the principal's runtime data
dir (`<data_dir>/session_titles.json`, the same plane as the session's runs, like
`session_archive.json`):

    {"schema": "gateway_session_titles_v1",
     "sessions": {session_id: {"title", "set_at", "set_by": {"kind": "person", "user_id"}}}}

`set_by.kind` says who named the conversation ("person" for the rename route), so a
later writer (a model-proposed title, gateway backlog 0937) can tell a person's name
from its own. An unreadable file is an error (the routes answer 500), never "no
titles": no titles would silently rename every conversation back.
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

SCHEMA = "gateway_session_titles_v1"
FILENAME = "session_titles.json"
MAX_CHARS = 120

_LOCK = threading.RLock()


class SessionTitlesUnreadable(RuntimeError):
    """The titles file exists but cannot be read as the schema."""


def normalize_title(raw: Optional[str]) -> Optional[str]:
    """One line, at most MAX_CHARS characters: control characters (line breaks, tabs) and
    unpaired surrogates become spaces, runs of whitespace one space, then trimmed and cut on
    a character boundary. None when nothing is left (the title is cleared)."""
    if raw is None:
        return None
    text = "".join(" " if (ord(c) < 0x20 or 0x7F <= ord(c) < 0xA0 or 0xD800 <= ord(c) < 0xE000) else c for c in raw)
    text = " ".join(text.split())
    text = text[:MAX_CHARS].rstrip()
    return text or None


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _path(data_dir: Any) -> Path:
    return Path(data_dir) / FILENAME


def _load(data_dir: Any) -> Dict[str, Any]:
    path = _path(data_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema": SCHEMA, "sessions": {}}
    except Exception as exc:  # noqa: BLE001 - re-raised typed, with the path
        raise SessionTitlesUnreadable(f"{path} is not readable: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA or not isinstance(raw.get("sessions"), dict):
        raise SessionTitlesUnreadable(f"{path} is not a {SCHEMA} file")
    return raw


# Read cache, as session_archive.py: `GET /runs` reads this on every call. Readers get the last
# parsed content; the file is stat-ed again at most every REVALIDATE_S and a write through this
# module drops the entry at once. An unreadable file is never cached (it raises on every read).
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


def _save(data_dir: Any, data: Dict[str, Any]) -> None:
    _forget(data_dir)
    path = _path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, str(path))
    _forget(data_dir)


def session_titles(data_dir: Any) -> Dict[str, str]:
    """`{session_id: title}` for this plane."""
    with _LOCK:
        out: Dict[str, str] = {}
        for sid, rec in _load_cached(data_dir)["sessions"].items():
            title = rec.get("title") if isinstance(rec, dict) else None
            if isinstance(title, str) and title:
                out[str(sid)] = title
        return out


def set_session_title(data_dir: Any, session_id: str, title: Optional[str], *, by: str) -> Dict[str, Any]:
    """Store a person's title for `session_id` (normalized; None or blank clears it).
    Storing the title already stored, or clearing none, changes nothing (`changed: false`)."""
    sid = str(session_id)
    value = normalize_title(title)
    with _LOCK:
        data = _load(data_dir)
        current = data["sessions"].get(sid)
        stored = current.get("title") if isinstance(current, dict) else None
        if stored == value:
            return {"session_id": sid, "title": value, "changed": False}
        if value is None:
            data["sessions"].pop(sid, None)
        else:
            data["sessions"][sid] = {"title": value, "set_at": _now(), "set_by": {"kind": "person", "user_id": str(by) or None}}
        _save(data_dir, data)
        return {"session_id": sid, "title": value, "changed": True}


__all__ = [
    "FILENAME",
    "MAX_CHARS",
    "SCHEMA",
    "SessionTitlesUnreadable",
    "normalize_title",
    "session_titles",
    "set_session_title",
]
