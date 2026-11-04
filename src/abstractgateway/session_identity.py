"""A conversation's temporary identity (framework ADR 0052 section 9: regular agents).

A regular agent has no home: its look and voice, when the person gives one, belong to the SESSION.
`POST /runs/start` takes an optional `identity` (`{avatar: {style, color}, voice: {...}}`), checked by
the same ladder as an entity's `PUT /identity` (the design in the loaded avatar catalog and drawable
on the Watch, the voice offered, its model downloaded; there is no maintenance hold for a session).
It is stored with the session's other choices in the owner's plane, never in an entity home:

    <plane data dir>/session_identities.json
    {"schema": "gateway_session_identities_v1",
     "sessions": {session_id: {"rev", "identity": <identity schema 1 record> | null, "set_at", "set_by",
                               "revisions": [<earlier records, oldest first>]}}}

Every run of the session carries it inline, `vars._runtime.identity` (the record) beside
`vars._runtime.identity_ref` (`session:<sid>#<rev>`): a later turn without `identity` inherits it, a
turn with one changes it (`rev + 1`). The record lives as long as the session's records do; no entity
home ever holds it.

The default, for a session that names none: the Assistant's own identity when the gateway has one;
no gateway setting holds one today, so the session records `identity: null` (rev 0) and no ref rides
its runs. Never a borrowed identity, never a fabricated ref.

An unreadable file is an error, never "no identity" (a run would then speak another voice).
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from abstractruntime.identity.digital_identity import (
    IDENTITY_REF_KEY,
    AvatarChoice,
    DigitalIdentity,
    VoiceChoice,
    identity_ref,
    stamp_run_identity_ref,
)

SCHEMA = "gateway_session_identities_v1"
FILENAME = "session_identities.json"
# Where a run carries its session's record (the ref rides beside it, `_runtime.identity_ref`).
RUN_IDENTITY_KEY = "identity"

_LOCK = threading.RLock()


class SessionIdentityUnreadable(RuntimeError):
    """The file exists but cannot be read as the schema."""


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _path(plane_dir: Any) -> Path:
    return Path(plane_dir) / FILENAME


def _load(plane_dir: Any) -> Dict[str, Any]:
    path = _path(plane_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema": SCHEMA, "sessions": {}}
    except Exception as exc:  # noqa: BLE001 - re-raised typed, with the path
        raise SessionIdentityUnreadable(f"{path} is not readable: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA or not isinstance(raw.get("sessions"), dict):
        raise SessionIdentityUnreadable(f"{path} is not a {SCHEMA} file")
    return raw


def _save(plane_dir: Any, data: Dict[str, Any]) -> None:
    path = _path(plane_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".session_identities-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _record_of(entry: Dict[str, Any]) -> Optional[DigitalIdentity]:
    raw = entry.get("identity")
    return None if raw is None else DigitalIdentity.from_dict(raw)


def session_identity(plane_dir: Any, session_id: str) -> Optional[Dict[str, Any]]:
    """The session's entry `{rev, identity, set_at, set_by}` or None when it has none recorded yet."""
    with _LOCK:
        entry = _load(plane_dir)["sessions"].get(str(session_id))
    return dict(entry) if isinstance(entry, dict) else None


def resolve_session_identity(
    plane_dir: Any,
    session_id: str,
    *,
    avatar: Optional[AvatarChoice],
    voice: Optional[VoiceChoice],
    actor: str,
) -> Optional[DigitalIdentity]:
    """The identity the session's next run carries. `avatar`/`voice` given (already through the
    ladder): recorded as the session's next rev and returned. Not given: the session's recorded one
    (None when it recorded null), or, for a session with nothing recorded, the default (recorded)."""
    sid = str(session_id)
    with _LOCK:
        data = _load(plane_dir)
        entry = data["sessions"].get(sid)
        if avatar is None:
            if isinstance(entry, dict):
                return _record_of(entry)
            # The default is the Assistant's own identity when the gateway has one; no setting holds
            # one today, so the session records none (null, rev 0): no ref rides its runs.
            data["sessions"][sid] = {"rev": 0, "identity": None, "set_at": _now(), "set_by": "default"}
            _save(plane_dir, data)
            return None
        rev = int(entry.get("rev") or 0) + 1 if isinstance(entry, dict) else 1
        record = DigitalIdentity.from_dict(
            DigitalIdentity(rev=rev, avatar=avatar, voice=voice, set_at=_now(), set_by=actor).to_dict()
        )
        earlier = list(entry.get("revisions") or []) if isinstance(entry, dict) else []
        if isinstance(entry, dict) and isinstance(entry.get("identity"), dict):
            earlier.append(entry["identity"])  # the revisions a `session:<sid>#<rev>` may still name
        data["sessions"][sid] = {"rev": rev, "identity": record.to_dict(), "set_at": record.set_at, "set_by": actor,
                                 "revisions": earlier}
        _save(plane_dir, data)
        return record


def strip_client_identity(input_data: Dict[str, Any]) -> None:
    """`_runtime.identity` and `_runtime.identity_ref` are written by the gateway only: a client-sent
    one is dropped (a ref is never taken from the caller)."""
    runtime_ns = input_data.get("_runtime")
    if isinstance(runtime_ns, dict):
        runtime_ns.pop(RUN_IDENTITY_KEY, None)
        runtime_ns.pop(IDENTITY_REF_KEY, None)


def stamp_session_identity(input_data: Dict[str, Any], session_id: str, record: Optional[DigitalIdentity]) -> None:
    """Carry the session's identity on the run: the record at `_runtime.identity` and its ref
    (`session:<sid>#<rev>`) at `_runtime.identity_ref`. Nothing when the session has none."""
    if record is None:
        return
    runtime_ns = input_data.setdefault("_runtime", {})
    runtime_ns[RUN_IDENTITY_KEY] = record.to_dict()
    stamp_run_identity_ref(input_data, identity_ref(f"session:{session_id}", record.rev))


def run_identity(run_vars: Any) -> Optional[DigitalIdentity]:
    """The identity a run carries inline (a session's), or None. A malformed one raises."""
    runtime_ns = run_vars.get("_runtime") if isinstance(run_vars, dict) else None
    raw = runtime_ns.get(RUN_IDENTITY_KEY) if isinstance(runtime_ns, dict) else None
    return None if raw is None else DigitalIdentity.from_dict(raw)


def session_identity_view(plane_dir: Any, session_id: str, *, rev: Optional[int] = None) -> Dict[str, Any]:
    """`GET /sessions/{sid}/identity[?rev=N]`: `{session_id, identity, identity_ref}` in the entity
    shape (`identity.avatar.watch_code`); `identity: null` and no ref when the session has none.
    Raises LookupError for a rev the session never had."""
    from .entity_identity import avatar_catalog, record_payload

    entry = session_identity(plane_dir, session_id) or {}
    current = _record_of(entry) if entry.get("identity") is not None else None
    record = current
    if rev is not None:
        candidates = ([current] if current is not None else []) + [DigitalIdentity.from_dict(r) for r in entry.get("revisions") or []]
        record = next((r for r in candidates if r.rev == rev), None)
        if record is None:
            raise LookupError(f"session {session_id} has no identity rev {rev}")
    if record is None:
        return {"session_id": session_id, "identity": None, "identity_ref": None}
    return {
        "session_id": session_id,
        "identity": record_payload(record, avatar_catalog()),
        "identity_ref": identity_ref(f"session:{session_id}", record.rev).to_dict(),
    }
