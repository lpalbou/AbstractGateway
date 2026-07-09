"""HTTP serving of the entity replay/observability stream (a2a 0005).

Two endpoints over the SAME merged stream (memory's frozen v1 envelopes +
gateway host markers), mirroring the gateway's replay-first discipline for
run ledgers:

- `GET  .../replay`         bounded read (history scrub) as NDJSON;
- `GET  .../replay/stream`  live tail as SSE (`id:` = seq, so SSE
                            `Last-Event-ID` reconnect resumes exactly).

One entity home = one life = one stream. Diary display blocks arrive from
the engine already `{"redacted": "diary"}` — every HTTP consumer here is a
non-entity audience in v1, so redaction stands unconditionally.

Seq-gap honesty (0005 delta 3): under family filters, consumers see gaps
in seq — expected, meaningless, never data loss.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..entities import EntityRegistry
from ..entity_replay import merged_replay, validate_families
from ..service import get_gateway_service

router = APIRouter(prefix="/gateway/entities", tags=["entities"])


def _registry() -> EntityRegistry:
    svc = get_gateway_service()
    registry = getattr(svc, "entity_registry", None)
    if isinstance(registry, EntityRegistry):
        return registry
    return EntityRegistry(data_dir=svc.config.data_dir)


def _home_or_404(registry: EntityRegistry, name: str):
    try:
        return registry.get_home(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{name}/replay")
async def replay_entity_stream(
    name: str,
    since_seq: float = Query(0.0, ge=0.0, description="Exclusive resume cursor (fractional = after a host marker)."),
    until_seq: Optional[int] = Query(None, ge=0, description="Inclusive upper bound (default: journal high-water at call time)."),
    families: Optional[str] = Query(None, description="Comma-separated family filter (event,binding,closure,trace,snapshot,valence,host)."),
    enrich: bool = Query(True, description="Include display blocks (diary blocks are always redacted)."),
) -> StreamingResponse:
    """Bounded history read, one envelope per NDJSON line, strict seq order."""
    registry = _registry()
    home = _home_or_404(registry, name)
    try:
        family_list = validate_families(families)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    def _gen():
        for envelope in merged_replay(
            home,
            entities_dir=registry.entities_dir,
            slug=home.manifest.slug,
            since_seq=float(since_seq),
            until_seq=until_seq,
            families=family_list,
            enrich=bool(enrich),
        ):
            yield (json.dumps(envelope, ensure_ascii=False) + "\n").encode("utf-8")

    return StreamingResponse(_gen(), media_type="application/x-ndjson")


@router.get("/{name}/records/{graph_id}/verbatim")
async def read_record_verbatim(name: str, graph_id: str) -> Dict[str, Any]:
    """Verbatim-on-click (maintainer ask, a2a 0007): the lossless exchange
    behind a memory record, served from the HOME's own artifact store.

    MAINTAINER RULING (2026-07-08 00:59, overrides the prior refusal
    design): the operator sees EVERYTHING. "You built the worst of
    systems: very complex and nobody has the right to look." The prior
    403s (diary-shaped refusal, unstripped-diary-fence leak backstop)
    blocked the operator from verifying who the entity is and from
    finding bugs — they are gone. What remains:
    - PURE READ for non-diary records: no `record_access` is recorded —
      an operator reading a verbatim is not the entity using its memory.
    - DIARY-SHAPED records serve the book entry through the same
      transparency contract as the diary door: the read is host-marked
      into the stream (kind="diary_read") so the entity's biography
      still shows it was read — visibility preserved, friction removed.
    - Honest absence is 404: no payload_ref, or the artifact is gone.
    """
    registry = _registry()
    home = _home_or_404(registry, name)

    from abstractmemory.records import resolve_digest_assertion
    from abstractruntime.storage.artifacts import FileArtifactStore

    gid = str(graph_id or "").strip()
    if not gid:
        raise HTTPException(status_code=400, detail="graph_id is required")

    assertion = resolve_digest_assertion(home.store, gid)
    if assertion is None:
        raise HTTPException(status_code=404, detail=f"no record {gid!r} in this home")
    attrs = assertion.attributes if isinstance(assertion.attributes, dict) else {}

    entry_id = str(attrs.get("entry_id") or "").strip()
    is_diary_shaped = (
        str(attrs.get("record_kind") or "") == "diary"
        or str(getattr(assertion, "scope", "") or "").strip().lower() == "diary"
        or bool(attrs.get("private"))
        or bool(entry_id)
    )
    if is_diary_shaped and entry_id:
        entry = home.diary.get_entry(entry_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"diary entry {entry_id!r} not found in the book")
        from ..entity_replay import record_host_marker

        record_host_marker(
            entities_dir=registry.entities_dir,
            slug=home.manifest.slug,
            entity_id=home.entity_id,
            kind="diary_read",
            journal_seq=int(home.memory.current_seq()),
            details={
                "entry_id": entry.get("entry_id"),
                "entry_kind": entry.get("kind"),
                "visibility": entry.get("visibility"),
                "reason": "operator review (verbatim endpoint)",
                "channel": "operator",
            },
        )
        return {
            "record_id": gid,
            "title": str(attrs.get("title") or ""),
            "text": str(entry.get("text") or entry.get("gist") or ""),
            "content_type": "text/plain",
            "turn_id": None,
            "run_id": None,
            "created_at": entry.get("written_at"),
        }

    payload_ref = str(attrs.get("payload_ref") or "").strip()
    if not payload_ref:
        # Born-digest kinds (a2a 0007 round 2): interests and dreams are
        # BORN AS WORDS — their digest IS their complete text, never a
        # compression. "The words you see are all the words there are" is
        # an answer, not an error. Other kinds without a payload_ref stay
        # an honest 404 (we cannot know their digest is complete).
        record_kind = str(attrs.get("record_kind") or "").strip().lower()
        if record_kind in ("interest", "dream"):
            return {
                "record_id": gid,
                "title": str(attrs.get("title") or ""),
                "text": str(getattr(assertion, "object", "") or ""),
                "content_type": "text/plain",
                "born_digest": True,
                "turn_id": None,
                "run_id": None,
                "created_at": assertion.observed_at,
            }
        raise HTTPException(status_code=404, detail=f"record {gid!r} carries no verbatim (no payload_ref)")

    # Identity records carry payload_ref = the attested spark FILE (the
    # engram's documented behavior: their verbatim IS the seed document).
    # Clicking a value/purpose/trait serves the spark — operator-planted,
    # already rendered by inspect, nothing sealed about it.
    from ..entities import SPARK_FILENAME

    if payload_ref == SPARK_FILENAME:
        return {
            "record_id": gid,
            "title": str(attrs.get("title") or ""),
            "text": home.spark_bytes().decode("utf-8"),
            "content_type": "text/plain",
            "turn_id": None,
            "run_id": None,
            "created_at": assertion.observed_at,
        }

    try:
        text = FileArtifactStore(str(home.home_dir / "artifacts")).load_text(payload_ref)
    except ValueError as e:
        # A payload_ref that is neither the spark file nor an artifact id —
        # honest absence, never a traceback (the 500s this replaced).
        raise HTTPException(
            status_code=404,
            detail=f"record {gid!r} payload_ref {payload_ref!r} is not a loadable verbatim: {e}",
        )
    if text is None:
        raise HTTPException(
            status_code=404,
            detail=f"verbatim artifact {payload_ref!r} is not in the home's artifact store",
        )

    # Maintainer ruling 2026-07-08: the former unstripped-diary-fence 403
    # (leak backstop) is removed — the operator reads everything. The
    # formation-side fix for the leak class stays runtime's item.
    provenance = getattr(assertion, "provenance", None)
    provenance = provenance if isinstance(provenance, dict) else {}
    return {
        "record_id": gid,
        "title": str(attrs.get("title") or ""),
        "text": text,
        "content_type": "text/plain",
        "turn_id": provenance.get("turn_id"),
        "run_id": provenance.get("run_id"),
        "created_at": assertion.observed_at,
    }


@router.get("/{name}/diary/{entry_id}")
async def operator_read_diary_entry(
    name: str,
    entry_id: str,
    reason: str = Query("operator review", description="Why the operator is reading — recorded in the stream"),
) -> Dict[str, Any]:
    """The OPERATOR's diary read (maintainer rulings, a2a 0007 + 2026-07-08:
    the operator reads the book without ceremony).

    The read stays a VISIBLE EVENT — it is host-marked into the replay
    stream (kind="diary_read", entry_id + reason) BEFORE the words are
    returned, so the entity's biography records that it was read. The
    `reason` is optional (defaults to "operator review"): visibility is
    truth-keeping; a mandatory form field was friction.
    Reads disclose; failed lookups do not — only disclosures are marked.
    """
    registry = _registry()
    home = _home_or_404(registry, name)

    entry = home.diary.get_entry(str(entry_id or "").strip())
    if entry is None:
        raise HTTPException(status_code=404, detail=f"no entry {entry_id!r} in {home.entity_id}'s book")

    from ..entity_replay import record_host_marker

    marker = record_host_marker(
        entities_dir=registry.entities_dir,
        slug=home.manifest.slug,
        entity_id=home.entity_id,
        kind="diary_read",
        journal_seq=int(home.memory.current_seq()),
        details={
            "entry_id": entry.get("entry_id"),
            "entry_kind": entry.get("kind"),
            "visibility": entry.get("visibility"),
            "reason": str(reason),
            "channel": "operator",
        },
    )
    return {
        "entry": entry,
        "read_recorded_at_seq": marker["seq"],
        "reason": str(reason),
    }


@router.get("/{name}/replay/stream")
async def stream_entity_replay(
    name: str,
    since_seq: float = Query(0.0, ge=0.0, description="Exclusive resume cursor."),
    families: Optional[str] = Query(None, description="Comma-separated family filter."),
    enrich: bool = Query(True),
    heartbeat_s: float = Query(5.0, gt=0.1, le=60.0),
    poll_s: float = Query(0.5, gt=0.05, le=10.0),
    last_event_id: Optional[str] = Header(None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """Live tail as SSE. Replay = the same read that doesn't stop: each poll
    continues from the cursor; `id:` carries the seq so a reconnecting
    client's `Last-Event-ID` (which wins over `since_seq`) resumes exactly.
    A life has no terminal state — the client closes the stream."""
    registry = _registry()
    home = _home_or_404(registry, name)
    try:
        family_list = validate_families(families)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    cursor = float(since_seq)
    if last_event_id is not None and str(last_event_id).strip():
        try:
            cursor = float(str(last_event_id).strip())
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Last-Event-ID is not a seq cursor: {last_event_id!r}")

    from ..entity_replay import read_host_markers

    async def _gen():
        pos = cursor
        # Host markers ride a SEPARATE cursor (their own count), not the seq
        # cursor: a marker's fractional seq anchors to a JOURNAL base that
        # the tail may already have passed (e.g. a summon marker at its
        # historical as_of, or a marker written just after journal events
        # landed). Filtering those by `seq <= pos` silently dropped them
        # from the LIVE tail while the bounded replay showed them — the
        # maintainer's "I have to refresh to see it evolve" (adversarial
        # realtime review, 2026-07-08). Clients order by seq; dedup is
        # seq-keyed client-side.
        include_host = "host" in (family_list if family_list is not None else ["host"])
        # Delivered-set by seq (unique per marker: base + n/1000 under the
        # write lock). The list from read_host_markers is seq-SORTED, so
        # positional cursors would shift when a late marker lands with a
        # small seq — exactly the case this lane exists for.
        delivered: set = set()
        if include_host:
            # Catch-up semantics unchanged: markers at seq <= the resume
            # cursor are the already-consumed past.
            for m in read_host_markers(registry.entities_dir, home.manifest.slug):
                if float(m.get("seq") or 0.0) <= pos:
                    delivered.add(float(m.get("seq") or 0.0))
        last_emit = asyncio.get_event_loop().time()
        while True:
            emitted = False
            for envelope in merged_replay(
                home,
                entities_dir=registry.entities_dir,
                slug=home.manifest.slug,
                since_seq=pos,
                until_seq=None,
                families=family_list,
                enrich=bool(enrich),
            ):
                if str(envelope.get("family") or "") == "host":
                    continue  # the host lane below owns marker delivery
                seq = float(envelope.get("seq") or 0.0)
                data = json.dumps(envelope, ensure_ascii=False)
                yield f"id: {seq}\n".encode("utf-8")
                yield b"event: replay\n"
                yield f"data: {data}\n\n".encode("utf-8")
                pos = max(pos, seq)
                emitted = True
                last_emit = asyncio.get_event_loop().time()
            if include_host:
                for envelope in read_host_markers(registry.entities_dir, home.manifest.slug):
                    seq = float(envelope.get("seq") or 0.0)
                    if seq in delivered:
                        continue
                    delivered.add(seq)
                    data = json.dumps(envelope, ensure_ascii=False)
                    # id: stays monotonic (the reconnect cursor must never
                    # move backwards past journal events already emitted).
                    yield f"id: {max(pos, seq)}\n".encode("utf-8")
                    yield b"event: replay\n"
                    yield f"data: {data}\n\n".encode("utf-8")
                    emitted = True
                    last_emit = asyncio.get_event_loop().time()
            if not emitted:
                now = asyncio.get_event_loop().time()
                if (now - last_emit) >= float(heartbeat_s):
                    yield b": keep-alive\n\n"
                    last_emit = now
            await asyncio.sleep(float(poll_s))

    return StreamingResponse(_gen(), media_type="text/event-stream")
