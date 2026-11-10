"""His memory graph in one read: `GET /entities/{name}/graph` (COMMUNITIES-DESIGN 2026-11-10 §3, slice 2).

The Entity app drew the graph by folding the whole replay through the live door (castor: 55 s for the bindings).
This snapshot folds it here, once: the nodes (every open formed record, the latest display block of its binding,
resolved for the operator audience exactly as the replay serves it) and their formation edges, with each node's
community key, `via` and seat from memory's communities read, plus that read itself (payload version 2, unchanged).

Folded from memory's replay (families `binding` and `closure`, enriched, through `merged_replay`): the same blocks
the app folds, so nothing is re-derived. Community records are never nodes (their binding draws its revision chain
only; they are the `communities` part). Cached per home directory at the journal high-water it covers: a request
after the journal moved folds only the tail since then (a new formation, a closure, the night's new version), so
the full fold runs once per process per home. `journal_seq` is where the snapshot ends: the app tails the live
stream from there."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

__all__ = ["GRAPH_FAMILIES", "GRAPH_SNAPSHOT_VERSION", "graph_snapshot"]

GRAPH_SNAPSHOT_VERSION = 1
#: The replay families the snapshot folds: a record appears with its binding and goes with its closure.
GRAPH_FAMILIES = ("binding", "closure")
_COMMUNITY_KIND = "community"


@dataclass
class _Folded:
    """One home's folded graph up to `hi` (the journal seq it covers)."""

    hi: int = 0
    nodes: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # graph id -> node (without its community)
    closed: Set[str] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)


_FOLDED: Dict[str, _Folded] = {}
_FOLDED_LOCK = threading.Lock()


def _folded_for(home: Any) -> _Folded:
    key = str(getattr(home, "home_dir", None) or home.entity_id)
    with _FOLDED_LOCK:
        held = _FOLDED.get(key)
        if held is None:
            held = _FOLDED[key] = _Folded()
        return held


def _fold(home: Any, folded: _Folded, *, entities_dir: Path, slug: str, until: int) -> None:
    """Fold memory's binding and closure envelopes after `folded.hi` up to `until` into `folded`."""
    from .entity_replay import merged_replay
    from .entity_understanding import with_cut

    for env in merged_replay(home, entities_dir=entities_dir, slug=slug, since_seq=float(folded.hi),
                             until_seq=int(until), families=list(GRAPH_FAMILIES), enrich=True):
        display = env.get("display")
        if not isinstance(display, dict):
            continue
        gid = str(display.get("graph_id") or "")
        if not gid:
            continue
        family = env.get("family")
        if family == "closure":
            # A closed digest row closes its record (an edge row's closure resolves to no digest block).
            folded.closed.add(gid)
            folded.nodes.pop(gid, None)
            continue
        if family != "binding" or gid in folded.closed:
            continue
        env = with_cut(env)
        display = env.get("display") if isinstance(env.get("display"), dict) else display
        held = folded.nodes.get(gid)
        folded.nodes[gid] = {
            "id": gid,
            "seq": held["seq"] if held else env.get("seq"),
            "observed_at": held["observed_at"] if held else env.get("observed_at"),
            "scope": env.get("scope"),
            "owner_id": env.get("owner_id"),
            "kind": str(display.get("kind") or ""),
            "title": display.get("title"),
            "display": display,
        }
    folded.hi = max(int(folded.hi), int(until))


def _placement(communities: Dict[str, Any]) -> Dict[str, Tuple[str, str, Optional[List[float]]]]:
    """graph id -> (community key, via, seat) from memory's payload: members with their seat, then the records
    placed since the version (provisional, no seat yet)."""
    out: Dict[str, Tuple[str, str, Optional[List[float]]]] = {}
    for c in communities.get("communities") or ():
        key = str(c["key"])
        seats = (c.get("layout") or {}).get("seats") or {}
        for m in c.get("members") or ():
            mid = str(m["id"])
            seat = seats.get(mid)
            out[mid] = (key, str(m.get("via") or ""), list(seat) if seat is not None else None)
        for pid in c.get("provisional_ids") or ():
            out.setdefault(str(pid), (key, "provisional", None))
    return out


def graph_snapshot(home: Any, *, entities_dir: Path, slug: str) -> Dict[str, Any]:
    """`{version, journal_seq, communities_version, communities: <memory's payload v2>, nodes: [{id, seq,
    observed_at, scope, owner_id, kind, title, display, community, via, seat}], edges: [{a, b, relation}]}`.

    `nodes`: every open formed record but the community records, oldest first (binding seq); `display` is the
    latest binding's block as the replay serves it (operator resolutions and `cut` included); `community` the key
    of the community memory put it in (`via` detected | attached | provisional), `seat` its [dx, dy] in that
    community's circle (memory's layout; None for a provisional record), all None when no version holds it.
    `edges`: the formation edges of those blocks between two nodes of the snapshot, once each."""
    from .routes.entities import communities_payload

    folded = _folded_for(home)
    with folded.lock:
        hi = int(home.memory.current_seq())
        if hi > folded.hi or not folded.nodes:
            _fold(home, folded, entities_dir=entities_dir, slug=slug, until=hi)
        nodes = [dict(n) for n in folded.nodes.values() if n["kind"] != _COMMUNITY_KIND]
        covered = folded.hi
    communities = communities_payload(home)
    placed = _placement(communities)
    ids = {n["id"] for n in nodes}
    edges: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str, str]] = set()
    for n in nodes:
        key, via, seat = placed.get(n["id"], (None, None, None))
        n.update(community=key, via=via, seat=seat)
        for e in n["display"].get("edges") or ():
            b, rel = str(e.get("target_graph_id") or ""), str(e.get("relation") or "")
            if b in ids and (n["id"], b, rel) not in seen:
                seen.add((n["id"], b, rel))
                edges.append({"a": n["id"], "b": b, "relation": rel})
    nodes.sort(key=lambda n: (float(n["seq"] or 0.0), n["id"]))
    return {
        "version": GRAPH_SNAPSHOT_VERSION,
        "journal_seq": covered,
        "communities_version": communities.get("version_seq"),
        "communities": communities,
        "nodes": nodes,
        "edges": edges,
    }
