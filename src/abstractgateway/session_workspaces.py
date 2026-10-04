"""A conversation's workspaces (round 11, DESIGN R11.1 FINAL: the SESSION level).

The person picks, for ONE conversation, which of the eligible workspaces its runs use and each
one's mode; "Use my default" clears it. The gateway stores the choice (thin clients hold no state:
every app that opens the conversation sees the same one) in the owner's plane, next to the
session's runs, so only the owner (or an admin addressing that plane) reads or changes it:

    <plane data dir>/session_workspaces.json
    {"schema": "gateway_session_workspaces_v1",
     "sessions": {session_id: {"account", "posture", "default_mode", "folders": [{path, mode}],
                               "updated_at", "updated_by"}},
     "history":  [{"session_id", "at", "by", "configured"}]}

A session may be configured before its first run. Every run records the level it ran with under its
vars ``_gateway_workspace.{level, summary}`` (run_workspace_guard), so the ledger/replay shows it.
An unreadable file is an error, never "nothing configured" (that would silently widen a run).
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional

SCHEMA = "gateway_session_workspaces_v1"
FILENAME = "session_workspaces.json"

_LOCK = threading.RLock()


class SessionWorkspacesUnreadable(RuntimeError):
    """The file exists but cannot be read as the schema."""


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _path(plane_dir: Any) -> Path:
    return Path(plane_dir) / FILENAME


def _load(plane_dir: Any) -> Dict[str, Any]:
    path = _path(plane_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema": SCHEMA, "sessions": {}, "history": []}
    except Exception as exc:  # noqa: BLE001 - re-raised typed, with the path
        raise SessionWorkspacesUnreadable(f"{path} is not readable: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA or not isinstance(raw.get("sessions"), dict):
        raise SessionWorkspacesUnreadable(f"{path} is not a {SCHEMA} file")
    raw.setdefault("history", [])
    return raw


def _save(plane_dir: Any, data: Dict[str, Any]) -> None:
    path = _path(plane_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def session_layer(plane_dir: Any, session_id: str) -> Optional[Dict[str, Any]]:
    """The stored layer of this session, or None ("Use my default")."""
    from .workspace_policy import stored_layer

    with _LOCK:
        entry = _load(plane_dir)["sessions"].get(str(session_id))
    return stored_layer(entry)


def set_session_layer(plane_dir: Any, session_id: str, layer: Optional[Dict[str, Any]], *, account: str, by: str) -> None:
    """Store (or clear, with None) this session's layer — already validated by the caller."""
    sid = str(session_id)
    with _LOCK:
        data = _load(plane_dir)
        if layer is None:
            data["sessions"].pop(sid, None)
        else:
            data["sessions"][sid] = {
                "account": account,
                "posture": layer["posture"],
                "default_mode": layer["default_mode"],
                "folders": [dict(r) for r in layer["folders"]],
                "updated_at": _now(),
                "updated_by": by,
            }
        data["history"].append({"session_id": sid, "at": _now(), "by": by, "configured": layer is not None})
        _save(plane_dir, data)
