"""Which of his topics a memory is in, on the surfaces that show one memory (COMMUNITIES-DESIGN 2026-11-10 §3).

The record read (`GET .../records/{graph_id}/verbatim`, the Detail tab) and the card's record briefs carry
`community`: `{key, title, pending, provisional, via}` (the stable key, the topic's current title or null while it
is being written, `pending` = what of it is still being written, `provisional` when memory placed the record at
formation since the last version, memory's `via`), or null when no version holds the record (no version yet, or
an unplaced record). The record read asks memory's `community_of`; the card, with many briefs, reads the same
communities payload `/communities` serves (one cached read), so both name the same topic."""

from __future__ import annotations

from typing import Any, Dict, Optional

__all__ = ["card_with_topics", "record_community", "topics_by_member"]


def _topic(c: Dict[str, Any], *, provisional: bool, via: Optional[str]) -> Dict[str, Any]:
    return {"key": c["key"], "title": c.get("title"), "pending": list(c.get("pending") or []),
            "provisional": bool(provisional), "via": via}


def topics_by_member(home: Any) -> Dict[str, Dict[str, Any]]:
    """record graph id -> its `community` block, from the communities payload (members, then provisional ids)."""
    from .routes.entities import communities_payload

    out: Dict[str, Dict[str, Any]] = {}
    for c in communities_payload(home).get("communities") or ():
        for m in c.get("members") or ():
            out[str(m["id"])] = _topic(c, provisional=False, via=str(m.get("via") or "") or None)
        for pid in c.get("provisional_ids") or ():
            out.setdefault(str(pid), _topic(c, provisional=True, via="provisional"))
    return out


def card_with_topics(card: Dict[str, Any], home: Any) -> Dict[str, Any]:
    """The card with each record brief (`entity_understanding.CARD_RECORD_LISTS`: interests, lessons, moments)
    carrying `community` (None when no version holds it). Sections are copied, never changed in place."""
    from .entity_understanding import CARD_RECORD_LISTS

    by_member = topics_by_member(home)
    out = dict(card)
    for section_key, list_key in CARD_RECORD_LISTS:
        section = out.get(section_key)
        if not isinstance(section, dict) or not isinstance(section.get(list_key), list):
            continue
        out[section_key] = {**section, list_key: [
            {**b, "community": by_member.get(str(b.get("record_id") or ""))} if isinstance(b, dict) else b
            for b in section[list_key]]}
    return out


def record_community(home: Any, record_id: str) -> Optional[Dict[str, Any]]:
    """The `community` block of one record, from memory's `community_of` (through the runtime facade), its title from
    the communities payload; None when no version holds it. ValueError (memory's) for an unknown record."""
    from abstractruntime.integrations.abstractmemory.memory_facade import community_of

    from .routes.entities import communities_payload

    try:
        found = community_of(home.memory, record_id)
    except ValueError:
        return None  # memory's answer for a community record itself (read it with /communities)
    key = found.get("community_key")
    if not key:
        return None
    by_key = {str(c["key"]): c for c in communities_payload(home).get("communities") or ()}
    c = by_key.get(str(key)) or {"key": key}
    return _topic(c, provisional=bool(found.get("provisional")), via=found.get("via"))
