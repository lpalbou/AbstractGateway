"""Artifact scope: which artifacts a run may attach (session isolation).

An attachment uploaded in one conversation must never reach another conversation's model, whatever
a client sends (operator report 2026-10-01, Mac mini: a new conversation's first turn carried the
screenshot attached in another conversation — the client resent the reference, and a reference
that named its owner run id passed the start door). The rule, enforced at every run start:

SESSION-PRIVATE artifacts are what a person attaches to a conversation: an artifact whose owner
run is a session memory run (`session_memory_<session id>`, where uploads live) or whose tags say
`kind: attachment`. Such an artifact is visible to a session only when
- its owner run is that session's memory run, or
- it is tagged with that session (`tags.session_id`), or
- its owner run belongs to that session (a run with `session_id == session`), or
- it is explicitly shared with every session of its owner (`tags.shared == "user"`).

Nothing else — not a matching `run_id` on the reference, not the same user.

Artifacts a RUN produced (outputs, generated media) keep the documented hand-off: a reference that
names the owner run (`run_id`) may pass them to a later run of another session of the same user;
the run-start door checks that the named run matches (`routes/gateway.py`). A run started without
a session keeps the older rule (a reference must name its owner run).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SHARED_WITH_USER = "user"
REASON_ARTIFACT_NOT_IN_SESSION = "artifact_not_in_session"
_SESSION_MEMORY_PREFIX = "session_memory_"
_SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


class ForeignSessionArtifact(ValueError):
    """A run start referenced an artifact another session owns."""

    def __init__(self, *, path: str, artifact_id: str, owner_run_id: str, session_id: str) -> None:
        self.path = path
        self.artifact_id = artifact_id
        self.owner_run_id = owner_run_id
        self.session_id = session_id
        super().__init__(
            f"Artifact ref at {path} is not visible to session {session_id}: "
            f"{artifact_id} belongs to {owner_run_id or 'no run'}. Attach the file in this conversation."
        )

    def detail(self) -> Dict[str, Any]:
        return {
            "reason_code": REASON_ARTIFACT_NOT_IN_SESSION,
            "message": str(self),
            "path": self.path,
            "artifact_id": self.artifact_id,
            "owner_run_id": self.owner_run_id or None,
            "session_id": self.session_id,
        }


def session_memory_run_id(session_id: str) -> str:
    sid = str(session_id or "").strip()
    if not sid or not _SAFE_RUN_ID.match(sid):
        return ""
    return f"{_SESSION_MEMORY_PREFIX}{sid}"


def iter_artifact_refs(value: Any, *, path: str = "$") -> List[Tuple[str, Dict[str, Any]]]:
    found: List[Tuple[str, Dict[str, Any]]] = []
    if isinstance(value, dict):
        artifact_id = value.get("$artifact") or value.get("artifact_id")
        if isinstance(artifact_id, str) and artifact_id.strip():
            found.append((path, value))
        for k, v in value.items():
            found.extend(iter_artifact_refs(v, path=f"{path}.{k}"))
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            found.extend(iter_artifact_refs(item, path=f"{path}[{idx}]"))
    return found


def is_session_private(meta: Any) -> bool:
    """Uploads are a conversation's own: owned by a session memory run, or tagged `kind: attachment`."""

    owner = str(getattr(meta, "run_id", "") or "").strip()
    if owner.startswith(_SESSION_MEMORY_PREFIX):
        return True
    tags = getattr(meta, "tags", None)
    return isinstance(tags, dict) and str(tags.get("kind") or "").strip() == "attachment"


def artifact_visible_to_session(meta: Any, session_id: str, *, run_store: Any = None) -> bool:
    sid = str(session_id or "").strip()
    if not sid:
        return False
    owner = str(getattr(meta, "run_id", "") or "").strip()
    if owner and owner == session_memory_run_id(sid):
        return True
    tags = getattr(meta, "tags", None)
    tags = tags if isinstance(tags, dict) else {}
    if str(tags.get("session_id") or "").strip() == sid:
        return True
    if str(tags.get("shared") or "").strip().lower() == SHARED_WITH_USER:
        return True
    if owner and run_store is not None:
        load = getattr(run_store, "load", None)
        if callable(load):
            try:
                run = load(owner)
            except Exception:  # noqa: BLE001 - an unreadable owner is not this session's
                run = None
            if run is not None and str(getattr(run, "session_id", "") or "").strip() == sid:
                return True
    return False


def refuse_foreign_session_artifacts(
    *,
    input_data: Dict[str, Any],
    session_id: Optional[str],
    artifact_store: Any,
    run_store: Any = None,
) -> int:
    """Raise `ForeignSessionArtifact` for the first SESSION-PRIVATE artifact referenced in
    `input_data` that `session_id` may not attach; return how many private references were
    checked. A missing artifact is left to the caller's own 404 (this guard decides scope, not
    existence); a run-produced artifact is the run-start door's run_id check."""

    sid = str(session_id or "").strip()
    if not sid:
        return 0
    refs = iter_artifact_refs(input_data)
    if not refs or artifact_store is None:
        return 0
    meta_fn = getattr(artifact_store, "get_metadata", None)
    checked = 0
    for path, ref in refs:
        artifact_id = str(ref.get("$artifact") or ref.get("artifact_id") or "").strip()
        meta = None
        if callable(meta_fn):
            try:
                meta = meta_fn(artifact_id)
            except Exception:  # noqa: BLE001
                meta = None
        if meta is None:
            continue
        if not is_session_private(meta):
            continue
        checked += 1
        if artifact_visible_to_session(meta, sid, run_store=run_store):
            continue
        owner = str(getattr(meta, "run_id", "") or "").strip()
        logger.warning(
            "run start refused: artifact %s (owner %s) referenced at %s is not visible to session %s",
            artifact_id,
            owner or "-",
            path,
            sid,
        )
        raise ForeignSessionArtifact(path=path, artifact_id=artifact_id, owner_run_id=owner, session_id=sid)
    return checked
