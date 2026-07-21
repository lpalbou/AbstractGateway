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
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ..entities import EntityRegistry
from ..entity_replay import merged_replay, validate_families
from ..service import get_gateway_service

router = APIRouter(prefix="/gateway/entities", tags=["entities"])

# Max envelopes per off-loop collection pass on the live tail (H7b). Each
# chunk borrows a worker thread briefly and hands control back to the event
# loop between chunks — a whole-life backlog can never pin the loop. Module
# constant so tests can narrow it.
_STREAM_CHUNK = 200

# Response bytes buffered per ASGI send (entity's profile, commons c2394):
# one envelope per send paid a threadpool hop + a BaseHTTPMiddleware chunk
# crossing + an ASGI send PER LINE — a 14-20 MB/s ceiling that served a
# 12.5MB life in ~14s while its generator produced it in ~1.1s. Batched
# ~256KB chunks measured ~100x faster on the same middleware stack. Bytes
# on the wire are identical; only chunk boundaries change.
_SEND_CHUNK_BYTES = 256 * 1024


def _batch_bytes(lines, chunk_bytes: int = _SEND_CHUNK_BYTES):
    """Regroup an iterable of bytes into ~chunk_bytes buffers, preserving
    content byte-for-byte. The final partial buffer always flushes."""
    buf = bytearray()
    for line in lines:
        buf += line
        if len(buf) >= int(chunk_bytes):
            yield bytes(buf)
            buf.clear()
    if buf:
        yield bytes(buf)


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


async def _home_or_404_offloop(registry: EntityRegistry, name: str):
    """First-touch home open runs OFF the event loop (adversary F5): a cold
    open constructs three SQLite stores and resolves the registry embedder,
    which can probe an HTTP route or load a local model — seconds of loop
    blockage at connection setup, the same starvation class as c975. The
    registry's _open_lock makes the threaded call safe."""
    return await asyncio.to_thread(_home_or_404, registry, name)


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
    home = await _home_or_404_offloop(registry, name)
    try:
        family_list = validate_families(families)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Sweep runtime's night narrations into the host stream before the read
    # (the seam runtime ruled at c3750: record-when-present, idempotent).
    if family_list is None or "host" in family_list:
        from ..entity_replay import sweep_night_narrations

        await asyncio.to_thread(sweep_night_narrations, registry.entities_dir, home.manifest.slug, home)

    def _lines():
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

    return StreamingResponse(_batch_bytes(_lines()), media_type="application/x-ndjson")


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
    home = await _home_or_404_offloop(registry, name)

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
        # Born-digest kinds (a2a 0007 round 2; widened c2529 on laurent's
        # "we should be able to view the world model cards"): interests,
        # dreams, world-model cards, and reflection-formed lessons are
        # BORN AS WORDS — their digest IS their complete text, never a
        # compression (world_model.py forms cards with no payload_ref by
        # design; a verbatim-BACKED lesson carries payload_ref and never
        # reaches this branch). "The words you see are all the words there
        # are" is an answer, not an error. Other kinds without a
        # payload_ref stay an honest 404 (we cannot know their digest is
        # complete).
        record_kind = str(attrs.get("record_kind") or "").strip().lower()
        if record_kind in ("interest", "dream", "world_model", "lesson"):
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
    home = await _home_or_404_offloop(registry, name)

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
    out = {
        "entry": entry,
        "read_recorded_at_seq": marker["seq"],
        "reason": str(reason),
    }
    # THE TRAIL (diary---verbatims room, lane C — laurent: "the diary entry
    # MUST contain those references to enable to trace back to the
    # verbatims"): the entry's graph projection + its edges, so the entity
    # app renders entry -> verbatim click-through with the EXISTING
    # /records/{graph_id}/verbatim endpoint. Pure reads over edges that
    # already stand (written_amid at write time; reflected_in at
    # formation). RENDER-WHEN-PRESENT: a graph hiccup degrades to a
    # labeled warning — the book read must never fail because the trail
    # fold did.
    try:
        trail = _diary_entry_trail(home, str(entry_id or "").strip())
        if trail is not None:
            out["trail"] = trail
    except Exception as e:  # noqa: BLE001 - the trail is garnish on the book read
        out["warnings"] = [f"#FALLBACK trail unavailable: {e}"]
    return out


def _diary_entry_trail(home: Any, entry_id: str) -> Optional[Dict[str, Any]]:
    """entry_id -> projection -> written_amid / reflected_in episodes, each
    with verbatim availability. None when the entry has no projection (a
    projection-less book entry is possible on old vintages — honest
    absence, never an invented trail). Episode titles/dates are workplace
    content, not diary words; private entries keep their edge trail per
    the 2026-07-07 ruling (the edge is act-frame)."""
    from abstractmemory import TripleQuery

    eid = home.entity_id
    projection_id = None
    for a in home.store.query(TripleQuery(predicate="dcterms:abstract", scope="diary", owner_id=eid, limit=0)):
        attrs = a.attributes if isinstance(a.attributes, dict) else {}
        if str(attrs.get("entry_id") or "") == entry_id:
            projection_id = str(a.subject)
            break
    if projection_id is None:
        return None

    def _episode_brief(graph_id: str) -> Dict[str, Any]:
        brief: Dict[str, Any] = {"graph_id": graph_id}
        rows = home.store.query(TripleQuery(subject=graph_id, predicate="dcterms:abstract", limit=1))
        if rows:
            a = rows[0]
            attrs = a.attributes if isinstance(a.attributes, dict) else {}
            brief["kind"] = str(attrs.get("record_kind") or "memory")
            brief["title"] = str(attrs.get("title") or "")[:160]
            brief["observed_at"] = str(a.observed_at or "")
            # Click-through readiness: the verbatim endpoint serves when a
            # payload_ref stands (born-digest kinds serve their digest).
            brief["verbatim_available"] = bool(attrs.get("payload_ref"))
        return brief

    written_amid = [
        _episode_brief(str(a.object))
        for a in home.store.query(TripleQuery(subject=projection_id, predicate="written_amid", limit=0))
        if a.object
    ]
    # The birth conversation: formation authors episode -> entry projection.
    reflected_in = [
        _episode_brief(str(a.subject))
        for a in home.store.query(TripleQuery(predicate="reflected_in", object=projection_id, limit=0))
        if a.subject
    ]
    return {
        "projection_id": projection_id,
        "written_amid": written_amid,
        "reflected_in": reflected_in,
        "verbatim_endpoint": "/records/{graph_id}/verbatim",
    }


def _collect_replay_chunk(
    home: Any,
    entities_dir: Any,
    slug: str,
    cursor: float,
    family_list: Optional[List[str]],
    enrich: bool,
    limit: int,
) -> Tuple[List[Dict[str, Any]], float, bool]:
    """Pull up to `limit` non-host envelopes strictly after `cursor`.

    H7b (starvation incident c975/c991): this runs on a WORKER THREAD via
    asyncio.to_thread — memory's export_replay is synchronous BY CONTRACT
    (c995: "consumers must not iterate this on an event loop"). Cross-thread
    use of the home's stores is safe by THEIR contract (0013:
    check_same_thread=False + one internal RLock around all cursor use).
    Each call opens a fresh merged_replay AT THE CURSOR and reads one
    bounded chunk, so poll re-entry is a cursored continuation, never a
    full-journal walk on the loop. Returns (envelopes, new_cursor,
    exhausted)."""
    out: List[Dict[str, Any]] = []
    pos = float(cursor)
    exhausted = True
    it = merged_replay(
        home,
        entities_dir=entities_dir,
        slug=slug,
        since_seq=pos,
        until_seq=None,
        families=family_list,
        enrich=bool(enrich),
    )
    try:
        for envelope in it:
            if str(envelope.get("family") or "") == "host":
                continue  # the host lane owns marker delivery
            seq = float(envelope.get("seq") or 0.0)
            out.append(envelope)
            pos = max(pos, seq)
            if len(out) >= int(limit):
                exhausted = False
                break
    finally:
        close = getattr(it, "close", None)
        if callable(close):
            close()
    return out, pos, exhausted


@router.get("/{name}/replay/stream")
async def stream_entity_replay(
    request: Request,
    name: str,
    since_seq: float = Query(0.0, ge=0.0, description="Exclusive resume cursor."),
    families: Optional[str] = Query(None, description="Comma-separated family filter."),
    enrich: bool = Query(True),
    heartbeat_s: float = Query(5.0, gt=0.1, le=60.0),
    poll_s: float = Query(0.5, gt=0.05, le=10.0),
    last_event_id: Optional[str] = Header(None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """Live tail as SSE. Replay = the same read that doesn't stop: each poll
    continues from the cursor; `id:` carries the resume cursor so a
    reconnecting client's `Last-Event-ID` (which wins over `since_seq`)
    resumes exactly. A life has no terminal state — the client closes.

    COMPOSITE CURSOR (adversary F4, shape chosen by observer c1040): the id
    field is `<journal_seq>|<marker_line>` — journal resumes strictly after
    seq; host markers redeliver where their append-order FILE LINE exceeds
    the marker cursor. A low-SEQ marker written during a disconnect has a
    HIGH line (the file is append-only), so it is never lost — the loss the
    old `seq <= cursor` pre-marking created. Plain-float ids (old clients /
    bounded replays) stay accepted: the marker half is optional, and absent
    means the legacy seq-based catch-up.

    H7b LOOP DISCIPLINE (the c975 starvation fix, observer's repro): the
    journal walk happens OFF the event loop in bounded chunks
    (`_collect_replay_chunk` via asyncio.to_thread); the loop regains
    control between chunks; abandoned clients are detected between chunks
    (`request.is_disconnected`) so a dead tail stops burning instead of
    producing the whole backlog into a dead socket's buffer."""
    registry = _registry()
    home = await _home_or_404_offloop(registry, name)
    try:
        family_list = validate_families(families)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    cursor = float(since_seq)
    marker_cursor: Optional[int] = None  # None = derive legacy catch-up from seq
    if last_event_id is not None and str(last_event_id).strip():
        raw_id = str(last_event_id).strip()
        seq_half, sep, marker_half = raw_id.partition("|")
        try:
            cursor = float(seq_half)
            if sep:
                marker_cursor = int(marker_half)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Last-Event-ID is not a replay cursor: {last_event_id!r}")

    from ..entity_replay import read_host_markers, sweep_night_narrations

    # Sweep runtime's night narrations into the host stream once at open
    # (record-when-present; the live loop below re-reads markers each tick,
    # so a narration written mid-stream is picked up by the next open —
    # night cadence makes a per-tick sweep unnecessary).
    include_host_pre = "host" in (family_list if family_list is not None else ["host"])
    if include_host_pre:
        await asyncio.to_thread(sweep_night_narrations, registry.entities_dir, home.manifest.slug, home)

    async def _gen():
        pos = cursor
        # Host markers ride a SEPARATE cursor (append-order file line), not
        # the seq cursor: a marker's fractional seq anchors to a JOURNAL base
        # the tail may already have passed (summon markers at historical
        # as_of). Filtering those by `seq <= pos` dropped them from the LIVE
        # tail while the bounded replay showed them (2026-07-08), and
        # pre-marking on reconnect LOST low-seq markers written during the
        # disconnect (adversary F4). The line cursor is delivery-order truth;
        # clients order by seq and dedup seq-keyed (redelivery is sanctioned).
        include_host = "host" in (family_list if family_list is not None else ["host"])
        marker_floor = 0  # deliver markers with _line > marker_floor (contiguous prefix)
        delivered_lines: set = set()
        if include_host:
            if marker_cursor is not None:
                marker_floor = int(marker_cursor)
            else:
                # Legacy/fresh open: markers at seq <= the resume cursor are
                # the already-consumed past (unchanged catch-up semantics).
                # Pre-populate the delivered-LINES set — never jump the floor
                # itself, or a high-seq marker sitting at a LOWER file line
                # than some consumed marker would be skipped.
                for m in await asyncio.to_thread(
                    read_host_markers, registry.entities_dir, home.manifest.slug, include_lines=True
                ):
                    if float(m.get("seq") or 0.0) <= pos:
                        delivered_lines.add(int(m.get("_line") or 0))
                while (marker_floor + 1) in delivered_lines:
                    marker_floor += 1
        last_emit = asyncio.get_event_loop().time()
        while True:
            if await request.is_disconnected():
                return
            emitted = False
            # Drain the journal backlog in bounded off-loop chunks; control
            # returns to the event loop between chunks by construction.
            while True:
                envs, pos, exhausted = await asyncio.to_thread(
                    _collect_replay_chunk,
                    home,
                    registry.entities_dir,
                    home.manifest.slug,
                    pos,
                    family_list,
                    bool(enrich),
                    _STREAM_CHUNK,
                )
                if envs:
                    # One buffered send per collect chunk (c2394 streaming
                    # tax; same batching as the bounded replay). Each event's
                    # id carries ITS OWN seq — the old chunk-final `pos` id
                    # let a mid-chunk disconnect resume PAST envelopes the
                    # client never received.
                    parts = bytearray()
                    for envelope in envs:
                        data = json.dumps(envelope, ensure_ascii=False)
                        env_seq = float(envelope.get("seq") or 0.0)
                        parts += f"id: {env_seq}|{marker_floor}\n".encode("utf-8")
                        parts += b"event: replay\n"
                        parts += f"data: {data}\n\n".encode("utf-8")
                        if len(parts) >= _SEND_CHUNK_BYTES:
                            yield bytes(parts)
                            parts.clear()
                    if parts:
                        yield bytes(parts)
                    emitted = True
                    last_emit = asyncio.get_event_loop().time()
                if exhausted:
                    break
                if await request.is_disconnected():
                    return  # dead tail mid-backlog: stop, don't finish the walk
            if include_host:
                for envelope in await asyncio.to_thread(
                    read_host_markers, registry.entities_dir, home.manifest.slug, include_lines=True
                ):
                    line = int(envelope.get("_line") or 0)
                    if line <= marker_floor or line in delivered_lines:
                        continue
                    delivered_lines.add(line)
                    wire = {k: v for k, v in envelope.items() if k != "_line"}
                    seq = float(wire.get("seq") or 0.0)
                    data = json.dumps(wire, ensure_ascii=False)
                    # The floor only advances past CONTIGUOUS delivered lines
                    # so a reconnect can never skip an interleaved line.
                    while (marker_floor + 1) in delivered_lines:
                        marker_floor += 1
                    # id: seq half stays monotonic (never backwards past
                    # journal events already emitted).
                    yield f"id: {max(pos, seq)}|{marker_floor}\n".encode("utf-8")
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
