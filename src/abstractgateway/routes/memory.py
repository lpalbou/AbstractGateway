"""`/api/gateway/memory/*`: the plane's memory graph, read through the AbstractRuntime memory facade.

Thin-client route contract (memory transition, contract §6; AbstractMemory ADR 0003; runtime ADR 0011):

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
    resolve_memory_home,
    resolve_memory_store_config,
    session_memory_owner_id,
)
from ..memory_access import caller_owner_ids, owner_forbidden
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
    run_id: Optional[str] = Field(default=None, description="Names the caller's run (its owners), as on the GET routes.")
    session_id: Optional[str] = Field(default=None, description="Names the caller's session (its owner).")
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=1000, description="A `run` source: at most this many ledger records (1..1000).")


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
    """The plane's MemoryFacade over its home and its runtime stores, opened once per service
    (`svc.memory_home`, review C1); None when the home does not exist yet (a read never creates it)."""
    return svc.memory_home.get(
        resolve_memory_store_config(base_dir=_plane(svc)),
        ledger_store=svc.stores.ledger_store,
        run_store=svc.stores.run_store,
        artifact_store=svc.stores.artifact_store,
    )


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
    """None for an admin (every owner); else the caller's own owner ids (`memory_access.caller_owner_ids`,
    operator decision 5: own runs/sessions, entities it created, its plane's global owner when the plane is its own)."""
    return caller_owner_ids(_principal_from_request(request), svc, run_id=run_id, session_id=session_id)


def _forbidden() -> HTTPException:
    return owner_forbidden()


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
        raise _forbidden()
    return row.to_dict()


#: How many memories a lineage walk visits at most (the graph's own walk is bounded the same way).
MAX_LINEAGE_VISITS = 1000


def _readable(facade: Any, ids: Iterable[str], owners: Optional[Set[str]]) -> Set[str]:
    """The ids among `ids` this caller may read: all of them for an admin; else the assertion rows whose owner
    is the caller's (a record's graph id, or any id without a row, is not readable to a non-admin)."""
    wanted = [i for i in dict.fromkeys(ids) if isinstance(i, str) and i]
    if owners is None:
        return set(wanted)
    rows = _rows_by_id(facade, wanted)
    return {i for i in wanted if i in rows and str(getattr(rows[i], "owner_id", "") or "") in owners}


def _parents(facade: Any) -> Any:
    """`memory id -> the ids it was derived from`, read from its `asserted` history events."""

    def parents_of(memory_id: str) -> List[str]:
        out: List[str] = []
        for event in facade.history(memory_id, limit=MAX_LINEAGE_VISITS):
            if event.get("event") == "asserted":
                for parent in (event.get("detail") or {}).get("derived_from") or ():
                    if isinstance(parent, str) and parent not in out:
                        out.append(parent)
        return out

    return parents_of


def _owned_lineage_sources(facade: Any, memory_id: str, owners: Set[str], limit: int) -> tuple:
    """(SourceRef dicts, withheld): the memory's sources and, breadth-first, those of the memories it was derived
    from, walking ONLY through memories this caller may read. A parent of another owner is not followed (its refs
    stay hidden) and is counted in `withheld`. At most `limit` refs, `MAX_LINEAGE_VISITS` memories."""
    parents_of = _parents(facade)
    out: Dict[str, Dict[str, Any]] = {}
    seen, queue, withheld, visits = {memory_id}, [memory_id], set(), 0
    SourceRef = _facade().SourceRef
    while queue and len(out) < limit and visits < MAX_LINEAGE_VISITS:
        current = queue.pop(0)
        visits += 1
        for ref in facade.sources_of(current, transitive=False, limit=limit):
            out.setdefault(SourceRef.from_dict(ref).key(), ref)
            if len(out) >= limit:
                break
        parents = [p for p in parents_of(current) if p not in seen]
        seen.update(parents)
        allowed = _readable(facade, parents, owners)
        for parent in parents:
            if parent in allowed:
                queue.append(parent)
            else:
                withheld.add(parent)
    return list(out.values()), len(withheld)


_ID_LIST_DETAIL_KEYS = ("derived_from", "replacement_ids")


def _filter_event_ids(facade: Any, events: List[Dict[str, Any]], owners: Optional[Set[str]]) -> tuple:
    """History events with the memory ids they name (`derived_from`, `replacement_ids`, and the `derived_from`
    of an adoption's `previous_provenance`) cut to what this caller may read; (events, withheld ids count)."""
    if owners is None:
        return events, 0
    named: List[str] = []
    for event in events:
        detail = event.get("detail") or {}
        for key in _ID_LIST_DETAIL_KEYS:
            named.extend(i for i in detail.get(key) or () if isinstance(i, str))
        prev = detail.get("previous_provenance")
        if isinstance(prev, dict):
            named.extend(i for i in prev.get("derived_from") or () if isinstance(i, str))
    allowed = _readable(facade, named, owners)
    hidden = {i for i in named if i not in allowed}
    if not hidden:
        return events, 0
    out = []
    for event in events:
        detail = dict(event.get("detail") or {})
        for key in _ID_LIST_DETAIL_KEYS:
            if isinstance(detail.get(key), (list, tuple)):
                detail[key] = [i for i in detail[key] if i not in hidden]
        prev = detail.get("previous_provenance")
        if isinstance(prev, dict) and isinstance(prev.get("derived_from"), (list, tuple)):
            detail["previous_provenance"] = {**prev, "derived_from": [i for i in prev["derived_from"] if i not in hidden]}
        out.append({**event, "detail": detail})
    return out, len(hidden)


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

    notes: List[str] = []
    if owners_allowed is not None:
        if scope == "all":
            # The implicit expansion of scope=all keeps only the caller's owners and says what it left out
            # (the plane's global owner on a runtime the caller does not own alone); never an explicit owner.
            kept = [(label, owner) for label, owner in targets if owner in owners_allowed]
            notes.extend(f"{label}: not yours on this runtime, left out" for label, owner in targets if owner not in owners_allowed)
            targets = kept
        else:
            for _, owner in targets:
                if owner not in owners_allowed:
                    raise _forbidden()

    limit = _bounded(req.limit)
    home = _home_info(svc)
    items: List[Dict[str, Any]] = []
    truncated = False
    facade = _open(svc)
    if facade is None:
        return {
            "ok": True, "scope": scope, "owner_id": None, "count": 0, "items": [], "truncated": False,
            "warnings": [f"memory home does not exist at {home['path']} (nothing recorded yet)", *notes], "home": home,
        }
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
    reverse = str(req.order or "desc").strip().lower() != "asc"
    items.sort(key=lambda d: str(d.get("observed_at") or ""), reverse=reverse)
    if len(items) > limit:
        truncated = True
        items = items[:limit]
    single_owner = targets[0][1] if len(targets) == 1 else None
    return {
        "ok": True, "scope": scope, "owner_id": single_owner, "count": len(items), "items": items,
        "truncated": truncated, "warnings": notes or None, "home": home,
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
    ids = facade.derived_from_source(ref, limit=limit + 1)
    truncated = len(ids) > limit
    ids = ids[:limit]
    withheld = 0
    if owners is not None:
        rows = _rows_by_id(facade, ids)
        visible = [i for i in ids if i in rows and str(getattr(rows[i], "owner_id", "") or "") in owners]
        withheld = len(ids) - len(visible)
        ids = visible
    return {"ok": True, "ref": ref, "ref_key": ref_key, "count": len(ids), "items": ids, "truncated": truncated, "withheld": withheld}


def _names_ref_for_caller(facade: Any, ref: Dict[str, Any], owners: Set[str]) -> bool:
    """True when a memory owned by one of the caller's owners names `ref` as its own source: AbstractMemory's
    `MemoryGraph.names_ref` (one covering-index probe on (ref_key, owner_id), right at any number of memories
    naming the ref; a `run` ref matches by run id). Lineage never crosses owners (AbstractMemory refuses a
    cross-owner derived_from), so a ref reached only through another owner's memory is never the caller's."""
    return facade.graph.names_ref(ref, sorted(owners))


@router.post("/sources/reconstruct")
def memory_reconstruct_source(request: Request, req: ReconstructRequest) -> Dict[str, Any]:
    """The verbatim a SourceRef points at, from this plane's ledger/run/artifact stores. HTTP 200 for every
    answer (`ok`, `source_gone`, `unsupported`); 422 for a malformed ref; 403 when no memory the caller may read
    names the ref (an admin reconstructs any ref of its plane). A `run` source returns at most `limit` ledger
    records, oldest first, and its metadata says how many were left out (`ledger_left_out`, `truncated`)."""
    svc = get_gateway_service()
    source = _source_ref(req.ref)
    ref = source.to_dict()
    owners = _caller_owners(request, svc, run_id=_clean(req.run_id), session_id=_clean(req.session_id))
    if owners is not None:
        home = _open(svc)
        allowed = home is not None and _names_ref_for_caller(home, ref, owners)
        if not allowed:
            raise _forbidden()
    MemoryFacade = _facade().MemoryFacade
    facade = MemoryFacade(
        ledger_store=svc.stores.ledger_store,
        run_store=svc.stores.run_store,
        artifact_store=svc.stores.artifact_store,
        # The plane's runtime, the id the bundle host stamps on refs (build_gateway_memory_kg_handlers): a ref
        # stamped by another runtime (an entity home's) is not read from this ledger.
        runtime_id=svc.host.catalog_runtime_id,
    )
    return facade.reconstruct_source(ref, limit=int(req.limit))


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
        return {"ok": True, "memory_id": memory_id, "owner_id": None, "count": 0, "items": [], "truncated": False,
                "withheld": 0}
    row = _require_readable(facade, memory_id, owners)
    events = facade.history(memory_id, limit=limit + 1)
    truncated = len(events) > limit
    events, withheld = _filter_event_ids(facade, events[:limit], owners)
    return {
        "ok": True, "memory_id": memory_id, "owner_id": (row or {}).get("owner_id"), "count": len(events),
        "items": events, "truncated": truncated, "withheld": withheld,
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
        return {"ok": True, "memory_id": memory_id, "transitive": transitive, "count": 0, "items": [], "truncated": False,
                "withheld": 0}
    _require_readable(facade, memory_id, owners)
    withheld = 0
    if owners is None or not transitive:
        refs = facade.sources_of(memory_id, transitive=bool(transitive), limit=limit + 1)
    else:
        # The graph's own transitive walk crosses owners; a non-admin walks only through its own memories.
        refs, withheld = _owned_lineage_sources(facade, memory_id, owners, limit + 1)
    truncated = len(refs) > limit
    SourceRef = _facade().SourceRef
    items = [{**r, "key": SourceRef.from_dict(r).key()} for r in refs[:limit]]
    return {"ok": True, "memory_id": memory_id, "transitive": bool(transitive), "count": len(items), "items": items,
            "truncated": truncated, "withheld": withheld}
