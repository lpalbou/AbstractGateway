"""`/api/gateway/memory/*`: the plane's memory graph, read through the AbstractRuntime memory facade.

Thin-client route contract (memory transition, contract §6; AbstractMemory ADR 0003; runtime ADR 0009):

- `POST /gateway/memory/query`                 the KG query shape over the plane's memory HOME
- `GET  /gateway/memory/{memory_id}/history`   asserted / closed / narrowed, in record order
- `GET  /gateway/memory/{memory_id}/sources`   the ledger SourceRefs (`?transitive=true` follows derived_from)
- `POST /gateway/memory/sources/reconstruct`   `{"ref": SourceRef}` -> the verbatim (ok | source_gone | unsupported)
- `GET  /gateway/memory/derived?ref_key=`      memory ids grounded in one ledger source (`step:<run_id>#<seq>`, ...)

Plane: the caller's service (`get_gateway_service()`), its home from `memory_store.resolve_memory_home`.
Owner rule: an admin reads every owner of the plane; any other principal reads only the owners it can
derive itself: the plane's global owner, plus the run/session/global owners of a `run_id` it can load
in its own plane, plus the session owner of a `session_id` it names (the same derivation as
`/kg/query`). An explicit `owner_id` outside that set, or `all_owners`, is refused with 403.
Every list is bounded by `limit` and says `truncated` when it cut; ids the owner rule hides are
counted in `withheld`, never dropped silently.

AbstractMemory is never imported here: the store, graph and SourceRef come from
`abstractruntime.integrations.abstractmemory.memory_facade` (gateway ADR 0001).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..memory_store import (
    global_memory_owner_id,
    open_memory_home,
    resolve_memory_home,
    resolve_memory_store_config,
    session_memory_owner_id,
)
from ..service import get_gateway_service
from .gateway import KGQueryRequest, _principal_from_request

router = APIRouter(prefix="/gateway/memory", tags=["memory"])

#: The largest page any `/memory/*` list returns (the graph's own read bound is 1000; one extra row
#: is fetched to know whether the page was cut).
MAX_LIMIT = 500
DEFAULT_LIMIT = 200


class MemoryQueryRequest(KGQueryRequest):
    """`KGQueryRequest`, bounded: `/memory/*` never returns an unbounded list."""

    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description=f"Max results (1..{MAX_LIMIT}).")


class ReconstructRequest(BaseModel):
    ref: Dict[str, Any] = Field(..., description="A SourceRef dict (kind + its id fields).")


# -- plane, home, owners --------------------------------------------------------------------------------------------


def _facade() -> Any:
    from abstractruntime.integrations.abstractmemory import memory_facade

    return memory_facade


def _plane(svc: Any) -> Path:
    return Path(svc.stores.base_dir)


def _home_info(svc: Any) -> Dict[str, Any]:
    home = resolve_memory_home(resolve_memory_store_config(base_dir=_plane(svc)))
    return {"path": str(home), "backend": "sqlite", "exists": home.exists()}


def _open(svc: Any) -> Any:
    """The plane's MemoryFacade over its home and its runtime stores; None when the home does not exist
    yet (a read never creates it)."""
    return open_memory_home(
        resolve_memory_store_config(base_dir=_plane(svc)),
        create=False,
        ledger_store=svc.stores.ledger_store,
        run_store=svc.stores.run_store,
        artifact_store=svc.stores.artifact_store,
    )


def _close(facade: Any) -> None:
    if facade is None:
        return
    for part in (facade.journal, facade.store):
        close = getattr(part, "close", None)
        if callable(close):
            close()


def _load_run(svc: Any, run_id: str) -> Any:
    try:
        run = svc.host.run_store.load(run_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load run: {e}") from e
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    return run


def _clean(value: Optional[str]) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _caller_owners(request: Request, svc: Any, *, run_id: Optional[str], session_id: Optional[str]) -> Optional[Set[str]]:
    """None for an admin (every owner); else the owners this caller can derive (see the module docstring)."""
    principal = _principal_from_request(request)
    if principal.is_admin():
        return None
    from abstractruntime.integrations.abstractmemory.effect_handlers import resolve_scope_owner_id

    owners: Set[str] = {global_memory_owner_id()}
    if run_id:
        run = _load_run(svc, run_id)
        rs = svc.host.run_store
        for scope in ("run", "session", "global"):
            owners.add(resolve_scope_owner_id(run, scope=scope, run_store=rs))
    if session_id:
        try:
            owners.add(session_memory_owner_id(session_id))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    return owners


def _forbidden(owner: str) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={
            "reason": "owner_forbidden",
            "message": f"owner {owner!r} is not one of this caller's owners (name its run_id or session_id; "
            "other owners need an admin principal)",
        },
    )


def _rows_by_id(facade: Any, ids: Iterable[str]) -> Dict[str, Any]:
    wanted = tuple(dict.fromkeys(i for i in ids if isinstance(i, str) and i))
    if not wanted:
        return {}
    TripleQuery = _facade().TripleQuery
    rows = facade.store.query(TripleQuery(assertion_ids=wanted, limit=len(wanted)))
    return {str(getattr(r, "assertion_id", "")): r for r in rows}


def _require_readable(facade: Any, memory_id: str, owners: Optional[Set[str]]) -> Optional[Dict[str, Any]]:
    """The memory's row (as a dict) when the caller may read it. An admin may read any id the graph knows
    (including a record's graph id); any other caller only an assertion row whose owner is theirs."""
    row = _rows_by_id(facade, [memory_id]).get(memory_id)
    if owners is None:
        return row.to_dict() if row is not None else None
    if row is None:
        raise HTTPException(status_code=404, detail=f"memory {memory_id!r} not found")
    owner = str(getattr(row, "owner_id", "") or "")
    if owner not in owners:
        raise _forbidden(owner)
    return row.to_dict()


def _bounded(limit: int) -> int:
    return max(1, min(int(limit), MAX_LIMIT))


def _source_ref(raw: Any) -> Any:
    """A validated SourceRef or a 422 naming what is wrong."""
    try:
        return _facade().SourceRef.from_dict(raw)
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=422, detail={"reason": "invalid_source_ref", "message": str(e)}) from e


def _ref_from_key(ref_key: str) -> Dict[str, Any]:
    """`run:<run_id>` | `step:<run_id>#<seq>` | `artifact:<id>` | `message:<id>` | `event:<id>` | `diary_entry:<id>`."""
    kind, sep, rest = str(ref_key or "").strip().partition(":")
    if not sep or not rest:
        raise HTTPException(status_code=422, detail={"reason": "invalid_ref_key", "message": f"not a SourceRef key: {ref_key!r}"})
    if kind == "step":
        run_id, hsep, seq = rest.rpartition("#")
        if not hsep or not run_id or not seq.isdigit():
            raise HTTPException(status_code=422, detail={"reason": "invalid_ref_key", "message": "a step key is step:<run_id>#<seq>"})
        return {"kind": "step", "run_id": run_id, "seq": int(seq)}
    field = {"run": "run_id", "artifact": "artifact_id", "message": "message_id", "event": "event_id", "diary_entry": "entry_id"}.get(kind)
    if field is None:
        raise HTTPException(status_code=422, detail={"reason": "invalid_ref_key", "message": f"unknown SourceRef kind {kind!r}"})
    return {"kind": kind, field: rest}


# -- routes ---------------------------------------------------------------------------------------------------------


@router.post("/query")
def memory_query(request: Request, req: MemoryQueryRequest) -> Dict[str, Any]:
    """The `/kg/query` shape over the plane's memory home (structured; `query_text` is refused: the home is
    SQLite, structured-only in this phase)."""
    svc = get_gateway_service()
    scope = str(req.scope or "session").strip().lower() or "session"
    if scope not in {"run", "session", "global", "all"}:
        raise HTTPException(status_code=400, detail="scope must be one of run|session|global|all")
    rid, sid, owner_override = _clean(req.run_id), _clean(req.session_id), _clean(req.owner_id)
    if scope == "all" and owner_override:
        raise HTTPException(status_code=400, detail="owner_id is not supported when scope=all")
    if owner_override and req.all_owners:
        raise HTTPException(status_code=400, detail="all_owners cannot be combined with owner_id")
    if _clean(req.query_text):
        raise HTTPException(
            status_code=400,
            detail="Semantic query is not available on the memory home (SQLite, structured-only); omit query_text.",
        )
    owners_allowed = _caller_owners(request, svc, run_id=rid, session_id=sid)
    if owners_allowed is not None and req.all_owners:
        raise HTTPException(status_code=403, detail={"reason": "owner_forbidden", "message": "all_owners needs an admin principal"})

    # (scope label, owner id or None for every owner)
    targets: List[tuple] = []
    if scope == "all":
        if req.all_owners:
            targets = [(label, None) for label in ("run", "session", "global")]
        elif rid:
            run = _load_run(svc, rid)
            from abstractruntime.integrations.abstractmemory.effect_handlers import resolve_scope_owner_id

            targets = [(label, resolve_scope_owner_id(run, scope=label, run_store=svc.host.run_store)) for label in ("run", "session", "global")]
        elif sid:
            targets = [("session", session_memory_owner_id(sid)), ("global", global_memory_owner_id())]
        else:
            raise HTTPException(status_code=400, detail="run_id or session_id is required when scope=all (or set all_owners=true)")
    else:
        if req.all_owners:
            owner = None
        elif owner_override:
            owner = owner_override
        elif scope == "global":
            owner = global_memory_owner_id()
        elif scope == "run":
            if not rid:
                raise HTTPException(status_code=400, detail="run_id is required for scope=run (or provide owner_id)")
            owner = rid
        elif sid:
            owner = session_memory_owner_id(sid)
        elif rid:
            from abstractruntime.integrations.abstractmemory.effect_handlers import resolve_scope_owner_id

            owner = resolve_scope_owner_id(_load_run(svc, rid), scope="session", run_store=svc.host.run_store)
        else:
            raise HTTPException(status_code=400, detail="run_id or session_id is required for scope=session (or provide owner_id)")
        targets = [(scope, owner)]

    if owners_allowed is not None:
        for _, owner in targets:
            if owner not in owners_allowed:
                raise _forbidden(str(owner))

    limit = _bounded(req.limit)
    home = _home_info(svc)
    items: List[Dict[str, Any]] = []
    truncated = False
    facade = _open(svc)
    if facade is None:
        return {
            "ok": True, "scope": scope, "owner_id": None, "count": 0, "items": [], "truncated": False,
            "warnings": [f"memory home does not exist at {home['path']} (nothing recorded yet)"], "home": home,
        }
    try:
        TripleQuery = _facade().TripleQuery
        for label, owner in targets:
            rows = facade.store.query(
                TripleQuery(
                    subject=req.subject, predicate=req.predicate, object=req.object,
                    scope=label, owner_id=owner, since=req.since, until=req.until, active_at=req.active_at,
                    limit=limit + 1, order=str(req.order or "desc"),
                )
            )
            if len(rows) > limit:
                truncated = True
            items.extend(r.to_dict() for r in rows[:limit])
    finally:
        _close(facade)
    reverse = str(req.order or "desc").strip().lower() != "asc"
    items.sort(key=lambda d: str(d.get("observed_at") or ""), reverse=reverse)
    if len(items) > limit:
        truncated = True
        items = items[:limit]
    single_owner = targets[0][1] if len(targets) == 1 else None
    return {
        "ok": True, "scope": scope, "owner_id": single_owner, "count": len(items), "items": items,
        "truncated": truncated, "warnings": None, "home": home,
    }


@router.get("/derived")
def memory_derived(
    request: Request,
    ref_key: str = Query(..., description="A SourceRef key: step:<run_id>#<seq> (URL-encode '#' as %23), run:<id>, artifact:<id>, ..."),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    run_id: Optional[str] = Query(None),
    session_id: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """Memory ids grounded in one ledger source (those naming it, then everything derived from them)."""
    svc = get_gateway_service()
    ref = _source_ref(_ref_from_key(ref_key)).to_dict()
    owners = _caller_owners(request, svc, run_id=_clean(run_id), session_id=_clean(session_id))
    facade = _open(svc)
    if facade is None:
        return {"ok": True, "ref": ref, "ref_key": ref_key, "count": 0, "items": [], "truncated": False, "withheld": 0}
    try:
        ids = facade.derived_from_source(ref, limit=limit + 1)
        truncated = len(ids) > limit
        ids = ids[:limit]
        withheld = 0
        if owners is not None:
            rows = _rows_by_id(facade, ids)
            visible = [i for i in ids if i in rows and str(getattr(rows[i], "owner_id", "") or "") in owners]
            withheld = len(ids) - len(visible)
            ids = visible
    finally:
        _close(facade)
    return {"ok": True, "ref": ref, "ref_key": ref_key, "count": len(ids), "items": ids, "truncated": truncated, "withheld": withheld}


@router.post("/sources/reconstruct")
def memory_reconstruct_source(request: Request, req: ReconstructRequest) -> Dict[str, Any]:
    """The verbatim a SourceRef points at, from this plane's ledger/run/artifact stores. HTTP 200 for every
    answer (`ok`, `source_gone`, `unsupported`); 422 for a malformed ref."""
    _principal_from_request(request)
    svc = get_gateway_service()
    ref = _source_ref(req.ref).to_dict()
    MemoryFacade = _facade().MemoryFacade
    facade = MemoryFacade(
        ledger_store=svc.stores.ledger_store,
        run_store=svc.stores.run_store,
        artifact_store=svc.stores.artifact_store,
    )
    return facade.reconstruct_source(ref)


@router.get("/{memory_id}/history")
def memory_history(
    request: Request,
    memory_id: str,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    run_id: Optional[str] = Query(None),
    session_id: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """`{event: asserted|closed|narrowed, at, revision_id, cause, detail}` in record order."""
    svc = get_gateway_service()
    owners = _caller_owners(request, svc, run_id=_clean(run_id), session_id=_clean(session_id))
    facade = _open(svc)
    if facade is None:
        if owners is not None:
            raise HTTPException(status_code=404, detail=f"memory {memory_id!r} not found")
        return {"ok": True, "memory_id": memory_id, "owner_id": None, "count": 0, "items": [], "truncated": False}
    try:
        row = _require_readable(facade, memory_id, owners)
        events = facade.history(memory_id, limit=limit + 1)
    finally:
        _close(facade)
    truncated = len(events) > limit
    events = events[:limit]
    return {
        "ok": True, "memory_id": memory_id, "owner_id": (row or {}).get("owner_id"), "count": len(events),
        "items": events, "truncated": truncated,
    }


@router.get("/{memory_id}/sources")
def memory_sources(
    request: Request,
    memory_id: str,
    transitive: bool = Query(True, description="Also the sources of the memories this one was derived from."),
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    run_id: Optional[str] = Query(None),
    session_id: Optional[str] = Query(None),
) -> Dict[str, Any]:
    """The memory's ledger sources as SourceRef dicts, each with its canonical `key`."""
    svc = get_gateway_service()
    owners = _caller_owners(request, svc, run_id=_clean(run_id), session_id=_clean(session_id))
    facade = _open(svc)
    if facade is None:
        if owners is not None:
            raise HTTPException(status_code=404, detail=f"memory {memory_id!r} not found")
        return {"ok": True, "memory_id": memory_id, "transitive": transitive, "count": 0, "items": [], "truncated": False}
    try:
        _require_readable(facade, memory_id, owners)
        refs = facade.sources_of(memory_id, transitive=bool(transitive), limit=limit + 1)
    finally:
        _close(facade)
    truncated = len(refs) > limit
    SourceRef = _facade().SourceRef
    items = [{**r, "key": SourceRef.from_dict(r).key()} for r in refs[:limit]]
    return {"ok": True, "memory_id": memory_id, "transitive": bool(transitive), "count": len(items), "items": items, "truncated": truncated}
