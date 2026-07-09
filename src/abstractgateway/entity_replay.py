"""The gateway serving end of the replay/observability stream (a2a 0005).

Memory froze stream v1 (thread 0005: one envelope shape; replay = bounded
read, live = the same read that doesn't stop; `family="host"` RESERVED for
gateway-authored transport markers). This module owns the gateway half:

- HOST MARKERS: summon / prelude_refused moments are journal-INVISIBLE by
  design (a prelude render is a pure read — nothing happened memory-side,
  that is keystone D1). The gateway records them in its own append-only
  per-entity marker log and interleaves them into the transport stream as
  `family="host"` items. Markers are GATEWAY bookkeeping (like run
  ledgers): they live OUTSIDE the home directory under
  `entities/.host_stream/<slug>.jsonl` and do not travel when a home is
  copied — the journal remains the life's only system of record.
- SEQ POSITIONS (runtime's delta 2): markers take fractional positions
  `base + n/1000` where base is the journal high-water seq at the moment
  the marker was written — they sort after journal item `base` and before
  `base + 1`, and can never collide with a journal seq.
- MERGING: `merged_replay` interleaves memory envelopes (int seqs) with
  host markers (fractional seqs) in strict ascending order; cursors are
  floats so a consumer can resume exactly after a marker.

Audience posture (0005 §5.1): diary display blocks arrive from the engine
already marked `{"redacted": "diary"}` — content never enters the stream at
the source. Every HTTP consumer of these endpoints is a non-entity audience
in v1 (operators/observers), so redaction simply stands; an entity-facing
un-redacted surface would be a new, explicitly-authenticated channel.
"""

from __future__ import annotations

import json
import math
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

from .entities import EntityHome

__all__ = [
    "HOST_MARKER_KINDS",
    "merged_replay",
    "read_host_markers",
    "record_host_marker",
    "validate_families",
]

HOST_STREAM_DIRNAME = ".host_stream"
# Marker kinds are MOMENTS (verbs), not states: sleep/wake/pause are the
# operator state transitions (a2a 0008); summon/prelude_refused/
# session_closed are the session moments (a2a 0005); diary_read is the
# operator's recorded act of reading the book (maintainer ruling, a2a
# 0007 — reads are visible events).
HOST_MARKER_KINDS = (
    "summon",
    "prelude_refused",
    "session_closed",
    "sleep",
    "wake",
    "pause",
    "diary_read",
    # Own-time lifecycle (maintainer, 2026-07-08: starting/stopping someone's
    # own time is part of their biography). NOTE: these were silently lost
    # before this entry existed — the loop routes swallow marker failures so
    # a marker must never block his own time, which also hid the kind gap.
    "own_time_started",
    "own_time_stop_requested",
    # FREEZE (admin hibernation, maintainer ruling 2026-07-08): distinct from
    # sleep — the process died without ceremony; the mark belongs in the
    # biography precisely because he could not write it himself.
    "own_time_frozen",
)

_marker_lock = threading.Lock()


def _utc_now_iso() -> str:
    from abstractruntime.core.runtime import utc_now_iso

    return utc_now_iso()


def _stream_constants() -> tuple:
    from abstractmemory.replay import REPLAY_FAMILIES, REPLAY_STREAM, REPLAY_STREAM_VERSION, RESERVED_FAMILIES

    return REPLAY_STREAM, REPLAY_STREAM_VERSION, REPLAY_FAMILIES, RESERVED_FAMILIES


def validate_families(raw: Optional[str]) -> Optional[List[str]]:
    """Parse a comma-separated families filter. Unknown names raise ValueError
    naming both valid and reserved sets (mirrors the engine's works-or-loud
    rule so HTTP callers get the same education)."""
    if raw is None or not str(raw).strip():
        return None
    _stream, _version, valid, reserved = _stream_constants()
    families = [f.strip() for f in str(raw).split(",") if f.strip()]
    unknown = [f for f in families if f not in valid and f not in reserved]
    if unknown:
        raise ValueError(
            f"unknown replay families {unknown} (valid: {list(valid)}; reserved: {sorted(reserved)})"
        )
    return families


def _marker_path(entities_dir: Path, slug: str) -> Path:
    return Path(entities_dir) / HOST_STREAM_DIRNAME / f"{slug}.jsonl"


def record_host_marker(
    *,
    entities_dir: Path,
    slug: str,
    entity_id: str,
    kind: str,
    journal_seq: int,
    run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Append one host marker and return its stream envelope. The fractional
    seq is assigned under a process lock; markers beyond 999 on one journal
    base raise loudly rather than colliding (unreachable at summon cadence —
    if it ever fires, something is summoning in a loop and SHOULD fail)."""
    if kind not in HOST_MARKER_KINDS:
        raise ValueError(f"unknown host marker kind {kind!r} (one of {HOST_MARKER_KINDS})")
    stream, version, _valid, _reserved = _stream_constants()
    base = int(journal_seq)

    path = _marker_path(entities_dir, slug)
    with _marker_lock:
        existing_at_base = 0
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    if int(math.floor(float(json.loads(line).get("seq") or 0.0))) == base:
                        existing_at_base += 1
                except (ValueError, TypeError):
                    continue
        if existing_at_base >= 999:
            raise RuntimeError(
                f"host marker fan-out exhausted at journal seq {base} for {slug!r} "
                "(999 markers on one base — is something summoning in a loop?)"
            )
        envelope: Dict[str, Any] = {
            "stream": stream,
            "stream_version": version,
            "seq": base + (existing_at_base + 1) / 1000.0,
            "family": "host",
            "observed_at": _utc_now_iso(),
            "scope": "",
            "owner_id": entity_id,
            "trace_id": None,
            "turn_id": None,
            "run_id": run_id,
            "payload": {
                "kind": kind,
                "session_id": session_id,
                **(dict(details) if details else {}),
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(envelope, ensure_ascii=False) + "\n")
    return envelope


def read_host_markers(
    entities_dir: Path,
    slug: str,
    *,
    since_seq: float = 0.0,
    until_seq: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Markers with since_seq < seq <= until_seq, in seq order. Unreadable
    lines are surfaced as a loud placeholder envelope, never silently
    skipped (a gap in gateway-authored bookkeeping is a bug to see)."""
    path = _marker_path(entities_dir, slug)
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        try:
            envelope = json.loads(line)
            seq = float(envelope.get("seq"))
        except (ValueError, TypeError) as e:
            out.append({"family": "host", "seq": -1.0, "payload": {"kind": "corrupt_marker", "line": i + 1, "error": str(e)}})
            continue
        if seq <= float(since_seq):
            continue
        if until_seq is not None and seq > float(until_seq):
            continue
        out.append(envelope)
    out.sort(key=lambda e: float(e.get("seq") or 0.0))
    return out


def _operator_diary_display(home: EntityHome, block: Dict[str, Any], cache: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Operator-audience display for a diary-redacted block (maintainer
    ruling, 2026-07-08 21:39: "the ledger is the ledger — we must see the
    content and ideally a 1-sentence summary"). This is the AUDIENCE seam
    memory designed into 0005 ("the gateway serving end enforces audience;
    memory marks the kind so it can"): the engine keeps marking, and THIS
    serving end — whose consumers are all operator surfaces — resolves the
    mark into the entry's GIST (the one-sentence summary). The full text
    still stays out of the stream (one click away via the diary door, where
    the read lands as a visible diary_read moment); federation surfaces are
    deny-by-default routes that never reach this code path."""
    gid = str(block.get("graph_id") or "")
    if not gid:
        return block
    cached = cache.get(gid)
    if cached is not None:
        return cached
    try:
        from abstractmemory.records import resolve_digest_assertion

        assertion = resolve_digest_assertion(home.store, gid)
        attrs = assertion.attributes if assertion is not None and isinstance(assertion.attributes, dict) else {}
        entry_id = str(attrs.get("entry_id") or "").strip()
        entry = home.diary.get_entry(entry_id) if entry_id else None
        if not entry:
            return block
        gist = str(entry.get("gist") or "").strip()
        text = str(entry.get("text") or "").strip()
        summary = gist or (f"{text[:120]}…" if len(text) > 120 else text)
        out = dict(block)
        out.pop("redacted", None)
        out.update(
            {
                "kind": "diary",
                "diary": True,  # the view still classes it (green node, book icon)
                "title": summary or "a diary entry",
                "gist": summary or None,
                "entry_kind": entry.get("kind"),
                "entry_id": entry_id,
            }
        )
        cache[gid] = out
        return out
    except Exception:
        return block  # best-effort: the redacted mark stands on any failure


def _enrich_operator_displays(home: EntityHome, envelope: Dict[str, Any], cache: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Resolve diary-redacted display blocks (top-level and co_selected
    pair members) into operator-audience blocks. Pure read; idempotent."""
    display = envelope.get("display")
    if isinstance(display, dict):
        if display.get("redacted") == "diary":
            envelope = {**envelope, "display": _operator_diary_display(home, display, cache)}
        elif isinstance(display.get("pair"), list):
            pair = display["pair"]
            if any(isinstance(m, dict) and m.get("redacted") == "diary" for m in pair):
                new_pair = [
                    _operator_diary_display(home, m, cache) if isinstance(m, dict) and m.get("redacted") == "diary" else m
                    for m in pair
                ]
                envelope = {**envelope, "display": {**display, "pair": new_pair}}
    return envelope


def merged_replay(
    home: EntityHome,
    *,
    entities_dir: Path,
    slug: str,
    since_seq: float = 0.0,
    until_seq: Optional[int] = None,
    families: Optional[Sequence[str]] = None,
    enrich: bool = True,
) -> Iterator[Dict[str, Any]]:
    """The transport stream: memory's frozen v1 envelopes merged with the
    gateway's host markers, strictly ascending by seq. `since_seq` is
    EXCLUSIVE and may be fractional (resume exactly after a marker);
    `until_seq=None` anchors to the journal high-water at call time (host
    markers beyond it ride along — they postdate the journal position they
    annotate by construction)."""
    include_host = families is None or "host" in families
    memory_since = int(math.floor(float(since_seq)))
    diary_display_cache: Dict[str, Dict[str, Any]] = {}

    # No scope/owner filter deliberately: an entity home is ONE life in ONE
    # file — every journal record in it belongs to this entity. Filtering by
    # owner would HIDE the rows whose lifted owner resolves to "" (memory's
    # documented unresolvable case), so the home serves its whole file.
    hi = int(until_seq) if until_seq is not None else int(home.memory.current_seq())
    envelopes = home.memory.export_replay(
        since_seq=memory_since,
        until_seq=hi,
        families=[f for f in families if f != "host"] if families is not None else None,
        enrich=enrich,
    )

    markers = read_host_markers(entities_dir, slug, since_seq=float(since_seq), until_seq=None if until_seq is None else float(until_seq) + 0.9999) if include_host else []
    m_idx = 0

    for envelope in envelopes:
        seq = float(envelope.get("seq") or 0.0)
        if seq <= float(since_seq):
            continue
        while m_idx < len(markers) and float(markers[m_idx].get("seq") or 0.0) < seq:
            yield markers[m_idx]
            m_idx += 1
        yield _enrich_operator_displays(home, envelope, diary_display_cache) if enrich else envelope
    while m_idx < len(markers):
        yield markers[m_idx]
        m_idx += 1
