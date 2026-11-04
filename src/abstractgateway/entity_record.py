"""Track record, reputation and acquaintances, read through the door (ADR 0052 sections 5, 6, 11).

The runtime computes all three from records the home (or the host) already keeps; nothing is
written or stored here. This module only picks the right stores for the subject and passes the
runtime's typed answer through unchanged (`basis: "completion-rate only"`, `not_counted` and every
other field as the runtime wrote them). Plain words for an empty record ("No experience yet")
belong to the client, never to this door.

Subjects:

- an entity: its home's own run store and ledger (`EntityRegistry.get_entity_runtime`, the same
  cached runtime the visit and cognition doors use), its home directory (task inbox, work orders,
  `identity.json`) and its diary chain head (for the fingerprint, ADR section 4). A home under a
  maintenance hold refuses with the standard 409 (`MaintenanceHoldActive`).
- a regular agent's session: the caller's plane's host run store and ledger. A session the
  caller's plane has no run of answers 404 `session_not_found`, exactly like
  `/sessions/{id}/archive` (another account's session is absent from the caller's plane).

Read cost: one run-index listing plus one run load per turn root, one token query (the runtime's
note). Measured on a 3000-run home (2 ledger calls a run, SQLite): 53 ms for a track record or a
reputation, 35 ms for acquaintances; 172 ms / 143 ms with 50 KB of variables in every run. No cache
is kept: the Entity app loads these on demand, never on its poll.

A record the runtime cannot read in the shape its writer guarantees (`TrackRecordError`, an
unreadable `identity.json`) answers 500 `record_unreadable` with the runtime's own sentence:
never an empty record in its place.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException

from abstractruntime.identity import (
    IdentityUnreadable,
    TrackRecordError,
    compute_reputation,
    entity_acquaintances,
    entity_track_record,
    session_acquaintances,
    session_track_record,
)


def parse_since(value: Optional[str]) -> Optional[str]:
    """`?since=` as the runtime compares it: ISO-8601 normalised to UTC with its offset (stored
    times are written that way and compared as text). Absent = the whole record. 400 otherwise."""
    from .session_history_bloc import parse_created_at_cursor

    try:
        return parse_created_at_cursor(value)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={"reason_code": "invalid_since", "message": f"since must be an ISO-8601 date or time, got {value!r}"},
        ) from None


def _read(compute: Callable[[], Any]) -> Dict[str, Any]:
    try:
        return compute().to_dict()
    except (TrackRecordError, IdentityUnreadable) as exc:
        raise HTTPException(status_code=500, detail={"reason_code": "record_unreadable", "message": str(exc)}) from None


# --- entities ------------------------------------------------------------------------------------


def _entity_sources(registry: Any, name: str):
    """(home_dir, entity runtime) for `name`; 404 when there is no such home."""
    try:
        manifest = registry.manifest_for(name)
        er = registry.get_entity_runtime(manifest.slug)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\"")) from None
    return registry.entities_dir / manifest.slug, er


def _entity_record(registry: Any, name: str, since: Optional[str]):
    home_dir, er = _entity_sources(registry, name)
    return entity_track_record(home_dir, er.run_store, er.ledger_store, book_head=er.home.diary.head(), since=since)


def entity_track_record_answer(registry: Any, name: str, *, since: Optional[str]) -> Dict[str, Any]:
    return _read(lambda: _entity_record(registry, name, since))


def entity_reputation_answer(registry: Any, name: str) -> Dict[str, Any]:
    return _read(lambda: compute_reputation(_entity_record(registry, name, None)))


def entity_acquaintances_answer(registry: Any, name: str) -> Dict[str, Any]:
    def compute():
        home_dir, er = _entity_sources(registry, name)
        return entity_acquaintances(home_dir, er.run_store)

    return _read(compute)


# --- a regular agent's session -------------------------------------------------------------------


def _session_stores(svc: Any, session_id: str):
    from .routes.session_archive import session_exists

    run_store = svc.host.run_store
    if not session_exists(run_store, session_id):
        raise HTTPException(
            status_code=404, detail={"reason_code": "session_not_found", "message": f"Session {session_id} does not exist."}
        )
    return run_store, svc.host.ledger_store


def session_track_record_answer(svc: Any, session_id: str, *, since: Optional[str]) -> Dict[str, Any]:
    def compute():
        run_store, ledger_store = _session_stores(svc, session_id)
        return session_track_record(session_id, run_store, ledger_store, since=since)

    return _read(compute)


def session_reputation_answer(svc: Any, session_id: str) -> Dict[str, Any]:
    def compute():
        run_store, ledger_store = _session_stores(svc, session_id)
        return compute_reputation(session_track_record(session_id, run_store, ledger_store))

    return _read(compute)


def session_acquaintances_answer(svc: Any, session_id: str) -> Dict[str, Any]:
    def compute():
        run_store, _ledger = _session_stores(svc, session_id)
        return session_acquaintances(session_id, run_store)

    return _read(compute)
