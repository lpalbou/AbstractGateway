"""A memory record's stored understanding, served under memory's own names.

AbstractMemory rewrites a record's words (its digest) in place and keeps every earlier reading as a version
(abstractmemory backlog 0059: `MemorySystem.record_understanding`, `MemorySystem.versions`). The Entity app shows who
wrote the words it displays, with which model, when and why, instead of guessing from the text's shape. This module
copies memory's fields, one to one and never renamed, onto the three surfaces the app reads:

- the record read (`GET .../records/{graph_id}/verbatim`): the record's current reading plus `versions`;
- recall snapshots in the replay stream: `display.recalled`, one block per recalled record;
- the card (`GET .../card`): each record brief in `discoveries.interests`, `lessons.lessons` and
  `key_moments.moments`.

Rules:
- every block carries every key of `UNDERSTANDING_KEYS`; a value memory does not hold for the record is `null`, never
  an absent key;
- diary records (kind or scope `diary`) get nothing here: their words leave only through the diary door (the book
  read that is recorded in the entity's history), exactly as before. Any other record carries the fields whatever its
  visibility (a private lesson included);
- the gateway never imports AbstractMemory: the home's `MemorySystem` methods are the read.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

#: Memory's display-block fields for a record's reading, in memory's names (abstractmemory
#: `understanding.understanding_fields` plus the current `digest`). Cards fill target..source_count, dreams
#: narrated/narration_version, a superseded record replaced_by; every other record serves those as null.
UNDERSTANDING_KEYS = (
    "digest",
    "digest_method",
    "digest_version",
    "version_count",
    "authored_by",
    "reader",
    "understood_at",
    "cause",
    "target",
    "target_type",
    "card_revision",
    "source_count",
    "narrated",
    "narration_version",
    "replaced_by",
)

#: `cut` (FINDINGS 2026-11-09 §(4)): the words shown were cut when they were saved, by a writer that is retired.
#: Derived from the WRITER of the reading, never from its text (no marker is looked for):
#: - a retired truncating writer's stamp: the flow brain's `mechanical-flow-v1` episodes (about 140 characters a
#:   side, cut mid-text);
#: - a session reflection (look-back) still in the words it was formed with, under no recorded author: until
#:   ADR 0054 (AbstractRuntime 2a1c698) that writer cut its digest at 280 characters and stamped nobody; since then
#:   a reflection is stored whole and authored at formation.
#: The Entity app then shows the title and "Older record: its summary was cut when saved; he rewrites it in his
#: sleep." instead of the cut words.
RETIRED_CUTTING_WRITERS = frozenset({"mechanical-flow-v1"})
REFLECTION_WRITER_SOURCES = frozenset({"entity-visit-run-reflection-v0", "entity-reflection-v1", "entity-chat-reflection-v1"})


def cut_when_saved(reading: Mapping[str, Any], source: Any) -> bool:
    """`cut` for one reading (memory's fields: digest_method, digest_version, authored_by) of a record whose
    formation provenance names `source`."""
    authored = reading.get("authored_by") if isinstance(reading.get("authored_by"), Mapping) else {}
    writer = str(reading.get("digest_method") or authored.get("id") or "").strip()
    if writer in RETIRED_CUTTING_WRITERS:
        return True
    original = reading.get("digest_version") in (None, 0)
    unrecorded = not authored or str(authored.get("kind") or "").strip() in ("", "unrecorded")
    return bool(original and unrecorded and str(source or "").strip() in REFLECTION_WRITER_SOURCES)


#: The card sections whose record briefs carry the fields (`(section, list key)`). Questions and problems are diary
#: rows and stay as they are.
CARD_RECORD_LISTS = (("discoveries", "interests"), ("lessons", "lessons"), ("key_moments", "moments"))
#: The identity section's statement lists and the record kind each statement is (memory's entity card: one statement
#: per self record, named by its title; limits are traits).
CARD_IDENTITY_LISTS = (("values", "value"), ("purposes", "purpose"), ("traits", "trait"), ("limits", "trait"))


def is_diary_record(assertion: Any) -> bool:
    """A diary record (kind or scope `diary`): the only records whose understanding is never served."""
    attrs = assertion.attributes if isinstance(getattr(assertion, "attributes", None), dict) else {}
    return (
        str(attrs.get("record_kind") or "") == "diary"
        or str(getattr(assertion, "scope", "") or "").strip().lower() == "diary"
    )


def is_diary_shaped(assertion: Any) -> bool:
    """A diary record by kind, scope, the private flag or a book entry id: the verbatim route's own test."""
    attrs = assertion.attributes if isinstance(getattr(assertion, "attributes", None), dict) else {}
    return (
        str(attrs.get("record_kind") or "") == "diary"
        or str(getattr(assertion, "scope", "") or "").strip().lower() == "diary"
        or bool(attrs.get("private"))
        or bool(str(attrs.get("entry_id") or "").strip())
    )


def understanding_fields(understood: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Every key of `UNDERSTANDING_KEYS` from memory's `record_understanding` answer; null where memory has none."""
    source = understood if isinstance(understood, Mapping) else {}
    return {key: source.get(key) for key in UNDERSTANDING_KEYS}


def _reading_at(versions: List[Dict[str, Any]], seq: int) -> Dict[str, Any]:
    """The version row that was the record's reading at journal position `seq`: the newest row written before it,
    else version 0 (the original, captured at the first rewrite)."""
    before = [v for v in versions if int(v.get("seq", -1)) < int(seq)]
    if before:
        return max(before, key=lambda v: int(v.get("seq", -1)))
    originals = [v for v in versions if int(v.get("version", -1)) == 0]
    return originals[0] if originals else min(versions, key=lambda v: int(v.get("seq", -1)))


class RecordUnderstandings:
    """Per-response reads of record understandings (one `record_understanding` and at most one `versions` call per
    distinct record), so a replay that recalls the same memory a thousand times reads it once. Scoped to one response
    or one stream chunk, never kept across them: a fresh read always sees the store as it is."""

    __slots__ = ("_home", "_understood", "_versions", "_sources", "_identity")

    def __init__(self, home: Any) -> None:
        self._home = home
        self._understood: Dict[str, Optional[Dict[str, Any]]] = {}
        self._versions: Dict[str, List[Dict[str, Any]]] = {}
        self._sources: Dict[str, str] = {}
        self._identity: Optional[Dict[tuple, str]] = None

    def identity_record_id(self, kind: str, title: Any) -> Optional[str]:
        """The self record an identity statement of the card shows (record kind + title, as memory's card names it);
        None when the home holds no such record (one `self_records` read per response)."""
        if self._identity is None:
            from .entities import SELF_SCOPE

            rows = self._home.memory.self_records(scope=SELF_SCOPE, owner_id=self._home.entity_id)
            self._identity = {}
            for row in rows:
                attrs = row.attributes if isinstance(row.attributes, dict) else {}
                self._identity.setdefault((str(attrs.get("record_kind") or ""), str(attrs.get("title") or "")),
                                          str(row.subject))
        return self._identity.get((str(kind), str(title or "")))

    def source(self, record_id: Any) -> str:
        """The writer the record's formation provenance names (`provenance.source`), "" when none (read with it)."""
        rid = str(record_id or "").strip()
        if rid not in self._understood:
            self.read(rid)
        return self._sources.get(rid, "")

    def cut(self, record_id: Any, reading: Mapping[str, Any]) -> bool:
        return cut_when_saved(reading, self.source(record_id))

    def read(self, record_id: Any) -> Optional[Dict[str, Any]]:
        """Memory's `record_understanding` for a formed, non-diary record (private ones included); None for a diary
        record, an edge row, a plain triple or an unknown id."""
        rid = str(record_id or "").strip()
        if not rid:
            return None
        if rid in self._understood:
            return self._understood[rid]
        from abstractruntime.integrations.abstractmemory.memory_facade import resolve_digest_assertion

        assertion = resolve_digest_assertion(self._home.store, rid)
        out: Optional[Dict[str, Any]] = None
        if assertion is not None and not is_diary_record(assertion):
            attrs = assertion.attributes if isinstance(assertion.attributes, dict) else {}
            if attrs.get("record_kind"):
                out = self._home.memory.record_understanding(rid)
                provenance = getattr(assertion, "provenance", None)
                self._sources[rid] = str((provenance or {}).get("source") or "") if isinstance(provenance, dict) else ""
        self._understood[rid] = out
        return out

    def versions(self, understood: Mapping[str, Any]) -> List[Dict[str, Any]]:
        """Memory's `versions` of a record, oldest first (empty when it was never rewritten: one reading, no rows)."""
        gid = str(understood.get("graph_id") or "")
        if gid not in self._versions:
            count = understood.get("version_count")
            self._versions[gid] = self._home.memory.versions(gid) if int(count or 0) > 1 else []
        return self._versions[gid]

    def recalled_block(self, record_id: str, seq: int) -> Optional[Dict[str, Any]]:
        """The block for one record of a recall snapshot at journal position `seq`. The reading fields (digest,
        digest_method, digest_version, authored_by, reader, understood_at, cause) are the reading that entered that
        context; version_count, replaced_by and the card and dream fields describe the record now."""
        understood = self.read(record_id)
        if understood is None:
            return None
        block: Dict[str, Any] = {
            "record_id": str(record_id),
            "graph_id": understood.get("graph_id"),
            "kind": understood.get("kind"),
            **understanding_fields(understood),
        }
        versions = self.versions(understood)
        if versions:
            reading = _reading_at(versions, seq)
            cause = reading.get("cause") if isinstance(reading.get("cause"), Mapping) else {}
            block.update({
                "digest": reading.get("text"),
                "digest_method": reading.get("digest_method") or None,
                "digest_version": reading.get("version"),
                "authored_by": reading.get("author") or None,
                "reader": reading.get("reader") or None,
                "understood_at": reading.get("understood_at") or None,
                "cause": cause.get("cause") or None,
            })
        block["cut"] = self.cut(record_id, block)
        return block


def with_recalled(envelope: Dict[str, Any], reads: RecordUnderstandings) -> Dict[str, Any]:
    """A recall snapshot envelope with `display.recalled`: one block per recalled formed non-diary record, in the
    order the snapshot lists them (diary records and edge rows are not listed). Other envelopes pass unchanged; the
    payload (the journal record) is never touched."""
    if envelope.get("family") != "snapshot":
        return envelope
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    rows = payload.get("display") if isinstance(payload.get("display"), list) else []
    seq = int(float(envelope.get("seq") or 0))
    recalled = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        block = reads.recalled_block(str(row.get("record_id") or ""), seq)
        if block is not None:
            recalled.append(block)
    display = envelope.get("display") if isinstance(envelope.get("display"), dict) else {}
    return {**envelope, "display": {**display, "recalled": recalled}}


def with_cut(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """A record's own display block in the replay stream (its formation, a rewrite: the hover card's words) carries
    `cut`, from the writer its row names (`payload.provenance.source`) and the block's reading. Other envelopes, and
    diary blocks (no digest is served on them), pass unchanged; the payload is never touched."""
    display = envelope.get("display")
    if not isinstance(display, dict) or not display.get("record_id") or "digest" not in display:
        return envelope
    if str(display.get("kind") or "") == "diary":
        return envelope
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    provenance = payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {}
    return {**envelope, "display": {**display, "cut": cut_when_saved(display, provenance.get("source"))}}


def card_with_understanding(card: Dict[str, Any], reads: RecordUnderstandings) -> Dict[str, Any]:
    """The card with each record brief of `CARD_RECORD_LISTS` carrying its current reading's fields (the brief's
    `statement` is that reading's words). Diary briefs (book lessons) stay as they are: `RecordUnderstandings.read`
    answers None for them. The card's sections may be shared with the engine-card cache: they are copied, never
    changed in place."""
    out = dict(card)
    for section_key, list_key in CARD_RECORD_LISTS:
        section = out.get(section_key)
        if not isinstance(section, dict) or not isinstance(section.get(list_key), list):
            continue
        briefs = []
        for brief in section[list_key]:
            understood = reads.read(brief.get("record_id")) if isinstance(brief, dict) else None
            briefs.append({**brief, **understanding_fields(understood), "cut": reads.cut(brief.get("record_id"), understood)}
                          if understood is not None else brief)
        out[section_key] = {**section, list_key: briefs}
    identity = out.get("identity")
    if isinstance(identity, dict):
        # The identity card's statement blocks carry `cut` too, from the writer of each statement's own record
        # (its current reading), never from the words.
        flagged = dict(identity)
        for list_key, kind in CARD_IDENTITY_LISTS:
            if not isinstance(identity.get(list_key), list):
                continue
            rows = []
            for entry in identity[list_key]:
                rid = reads.identity_record_id(kind, entry.get("title")) if isinstance(entry, dict) else None
                understood = reads.read(rid) if rid else None
                rows.append({**entry, "cut": reads.cut(rid, understood)} if understood is not None else entry)
            flagged[list_key] = rows
        out["identity"] = flagged
    return out
