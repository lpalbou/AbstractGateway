"""Incremental ledger tail readers (backlog 0075, operator top priority).

The SSE ledger stream used to re-materialize the ENTIRE ledger (full file
read + JSON parse of every record) on every news event, per client — the
c2394 100%-CPU class. These readers serve the same records with O(new bytes)
incremental reads:

- `JsonlLedgerTail`: byte-offset tail over the JSONL file, using EXACTLY the
  parse discipline of `JsonlLedgerStore.list()` (one JSON object per line;
  best-effort recovery of concatenated objects on one line, labeled
  #FALLBACK). Replay equivalence is the contract: the sequence of records a
  tail produces from index 0 equals `list()` — pinned by tests.
- `SeqLedgerTail`: cursor read over a store exposing `list_after(run_id,
  after, limit)` (the SQLite backend; seq is dense 1..N per run, so the
  record-index cursor and seq coincide — also test-pinned).
- `ListSliceTail`: honest fallback for stores with neither surface (e.g.
  in-memory test doubles): `list()[cursor:]` per read — the pre-0075
  behavior, correct just not cheap.

Cursor semantics (wire-compatible with the existing API): the cursor is the
NUMBER OF RECORDS CONSUMED (a record index, not a seq). `resolve_ledger_tail`
walks the store decorator chain (Offloading -> Observable -> backend) to pick
the cheapest reader.

Partial-line honesty (JSONL): a writer may be mid-append when we read — the
tail only consumes COMPLETE lines (ending in "\n") and holds its byte offset
at the start of any trailing fragment, so a torn read never drops or
duplicates a record; the fragment is re-read whole on the next poll. NEVER
splitlines() here (U+2028/U+2029/U+0085 are raw in JSON under
ensure_ascii=False and splitlines() fragments records — the 2026-07-14
replay-adversary class); the writer's line discipline is "\n" only.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _parse_ledger_line(line: str, decoder: json.JSONDecoder, path: Path) -> List[Dict[str, Any]]:
    """One line -> records, mirroring JsonlLedgerStore.list() exactly:
    plain parse first, then concatenated-object recovery (#FALLBACK)."""
    line = line.strip()
    if not line:
        return []
    try:
        record = json.loads(line)
        return [record] if isinstance(record, dict) else []
    except json.JSONDecodeError:
        pass
    recovered: List[Dict[str, Any]] = []
    i = 0
    while i < len(line):
        while i < len(line) and line[i].isspace():
            i += 1
        if i >= len(line):
            break
        try:
            obj, end = decoder.raw_decode(line, idx=i)
        except json.JSONDecodeError:
            break
        if isinstance(obj, dict):
            recovered.append(obj)
        i = end
    if recovered:
        logger.warning(
            "JsonlLedgerTail #FALLBACK recovered %s JSON objects from one line in %s",
            len(recovered),
            str(path),
        )
    return recovered


class JsonlLedgerTail:
    """Byte-offset incremental reader over one run's JSONL ledger file."""

    # Per-read byte ceiling: a reconnect-from-0 on a multi-GB ledger must
    # not buffer the whole backlog in one read. The stream re-drains
    # immediately while records keep coming, so a capped read only shapes
    # memory, never delivery.
    _READ_MAX_BYTES = 8 * 1024 * 1024

    def __init__(self, path: Path, *, start_index: int = 0) -> None:
        self._path = Path(path)
        self._offset = 0  # byte offset of the first UNCONSUMED complete line
        self._index = 0  # record index produced so far (== wire cursor)
        self._skip = max(0, int(start_index))  # catch-up records to swallow
        self._decoder = json.JSONDecoder()

    @property
    def index(self) -> int:
        """Records consumed so far INCLUDING skipped catch-up records."""
        return self._index

    def read_new(self, *, max_records: int = 10_000) -> List[Tuple[int, Dict[str, Any]]]:
        """Return [(cursor, record), ...] for records past the current
        position; cursor is the 1-based wire cursor AFTER consuming the
        record (matches the existing SSE `id:` lines). O(new bytes).

        Truncation honesty: a ledger file SHRINKING under a live tail means
        an external rewrite (retention prune + recreate, manual edit) — held
        byte offsets and record indices cannot be mapped onto the new
        content (survivors vs new records is undecidable from size alone).
        The reader resets to a FRESH ledger (offset 0, cursor 1..N of the
        current file) and warns loudly. Delivery is at-least-once by
        contract; clients dedupe by the emitted cursor/seq.
        """
        p = self._path
        try:
            size = p.stat().st_size
        except OSError:
            return []
        if size < self._offset:
            logger.warning(
                "JsonlLedgerTail: %s shrank under a live tail (%s < %s bytes); external rewrite — "
                "resetting to a fresh ledger (at-least-once: earlier records may re-emit)",
                p.name,
                size,
                self._offset,
            )
            self._offset = 0
            self._index = 0
            self._skip = 0
        if size == self._offset:
            return []

        out: List[Tuple[int, Dict[str, Any]]] = []
        want = min(size - self._offset, self._READ_MAX_BYTES)
        with p.open("rb") as f:
            f.seek(self._offset)
            buf = f.read(want)
        # Only complete lines: hold the offset at the start of a trailing
        # fragment (no "\n" yet — a torn concurrent append, or a byte-capped
        # read that stopped mid-line; either way the fragment re-reads whole
        # next pass). "\n" (0x0A) can never appear inside a UTF-8 multi-byte
        # sequence (continuation bytes are >= 0x80), so splitting on it never
        # cuts a character.
        end = buf.rfind(b"\n")
        if end < 0:
            return []
        chunk = buf[: end + 1]
        consumed_bytes = end + 1

        text = chunk.decode("utf-8", errors="replace")
        for line in text.split("\n"):
            if not line.strip():
                continue
            for record in _parse_ledger_line(line, self._decoder, p):
                if self._skip > 0:
                    self._skip -= 1
                    self._index += 1
                    continue
                self._index += 1
                out.append((self._index, record))
        # NOTE: max_records is deliberately not enforced here — the byte
        # offset advances over the WHOLE consumed chunk, so an early break
        # would silently drop the parsed-but-unreturned records. The
        # _READ_MAX_BYTES cap is the real per-read bound (memory AND record
        # count); callers drain repeatedly.
        self._offset += consumed_bytes
        return out


class SeqLedgerTail:
    """Cursor reader over a store with `list_after` (SQLite: seq is dense
    1..N per run, so the record-index wire cursor and the seq coincide)."""

    def __init__(self, store: Any, run_id: str, *, start_index: int = 0) -> None:
        self._store = store
        self._run_id = str(run_id)
        self._cursor = max(0, int(start_index))

    @property
    def index(self) -> int:
        return self._cursor

    def read_new(self, *, max_records: int = 10_000) -> List[Tuple[int, Dict[str, Any]]]:
        try:
            records, next_cursor = self._store.list_after(
                run_id=self._run_id, after=self._cursor, limit=int(max_records)
            )
        except Exception:
            logger.exception("SeqLedgerTail: list_after failed for %s", self._run_id)
            return []
        out: List[Tuple[int, Dict[str, Any]]] = []
        cur = self._cursor
        for rec in records or []:
            cur += 1
            out.append((cur, rec))
        # Seq is DENSE per run (atomic ledger_heads increment, no mid-run
        # deletes) so the dense wire index and the store seq coincide —
        # pinned by the replay-equivalence harness. Trust the store's
        # next_cursor as the authoritative resume point (max seq read): if
        # density ever broke, resuming from seq skips the gap instead of
        # silently re-emitting records.
        try:
            self._cursor = max(cur, int(next_cursor or 0))
        except Exception:
            self._cursor = cur
        return out


class ListSliceTail:
    """Fallback: full list() + slice per read (pre-0075 behavior).

    Correct for any LedgerStore; kept for stores with neither a file path
    nor list_after (in-memory doubles). The stream still only calls this
    when the news gate says there IS news, so idle cost stays low.
    """

    def __init__(self, store: Any, run_id: str, *, start_index: int = 0) -> None:
        self._store = store
        self._run_id = str(run_id)
        self._cursor = max(0, int(start_index))

    @property
    def index(self) -> int:
        return self._cursor

    def read_new(self, *, max_records: int = 10_000) -> List[Tuple[int, Dict[str, Any]]]:
        # Cheap news gate first (the pre-0075 discipline): only materialize
        # the full list when the count moved past the cursor. Count/list can
        # disagree on corrupt lines (adversary F1) — emission truth stays the
        # materialized list; a news signal producing nothing is just an
        # empty read.
        count_fn = getattr(self._store, "count", None)
        if callable(count_fn):
            try:
                if int(count_fn(self._run_id)) <= self._cursor:
                    return []
            except Exception:
                pass
        try:
            ledger = self._store.list(self._run_id)
        except Exception:
            logger.exception("ListSliceTail: list failed for %s", self._run_id)
            return []
        if not isinstance(ledger, list):
            return []
        out: List[Tuple[int, Dict[str, Any]]] = []
        while self._cursor < len(ledger) and len(out) < max_records:
            rec = ledger[self._cursor]
            self._cursor += 1
            out.append((self._cursor, rec))
        return out


def _unwrap_chain(store: Any) -> List[Any]:
    """The store + its decorator chain, outermost first (Offloading ->
    Observable -> backend). Bounded walk; `inner` (property) and `_inner`
    are the two shipped spellings."""
    chain: List[Any] = []
    cur = store
    for _ in range(8):
        if cur is None or cur in chain:
            break
        chain.append(cur)
        nxt = getattr(cur, "inner", None)
        if nxt is None:
            nxt = getattr(cur, "_inner", None)
        cur = nxt
    return chain


def find_observable(store: Any) -> Optional[Any]:
    """The first chain layer exposing subscribe() (wakeup pub/sub), or None."""
    for layer in _unwrap_chain(store):
        if callable(getattr(layer, "subscribe", None)):
            return layer
    return None


def resolve_ledger_tail(store: Any, run_id: str, *, start_index: int = 0) -> Any:
    """Pick the cheapest correct incremental reader for this store chain.

    IMPORTANT (offloading contract): list() on the gateway chain serves
    refs-as-refs (OffloadingLedgerStore deliberately does NOT rehydrate on
    list — 2026-07-14 measurement). The JSONL/SQLite backends hold the
    PERSISTED (offloaded) records, so a byte-tail over the backend file
    yields byte-identical records to chain.list() — same read surface.
    """
    for layer in _unwrap_chain(store):
        # JSONL backend: file path per run.
        path_fn = getattr(layer, "_path", None)
        if callable(path_fn):
            try:
                p = path_fn(str(run_id))
                if isinstance(p, Path):
                    return JsonlLedgerTail(p, start_index=start_index)
            except Exception:
                pass
        # SQLite backend: seq-cursor read.
        if callable(getattr(layer, "list_after", None)):
            return SeqLedgerTail(layer, str(run_id), start_index=start_index)
    return ListSliceTail(store, str(run_id), start_index=start_index)
