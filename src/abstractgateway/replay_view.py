"""The replay view served to every client: one fold per record, fanned out (framework 0993).

AbstractRuntime owns the reducer (`abstractruntime.replay`, ADR-0009): a run tree's ledger
records plus its durable snapshots folded into one view (status, the person-facing
interaction, messages, activities, artifacts, usage). The gateway only SERVES it:

- `ViewEntry` holds one `ReplayView` per (ledger store, root run): the caller's own plane,
  because every principal's service has its own stores. A refresh reads only the records
  after each run's folded seq (`LedgerStore.list_after`: an indexed read on SQLite, a seek
  to the remembered byte offset on JSONL), folds them once (`ReplayView.apply`) and fans the
  same delta out to every stream subscribed to that run. N clients cost one fold per record.
- `ViewHub` is the bounded cache of entries (LRU; an entry with live subscribers is never
  evicted) and the GET path's cache of the serialized view per cursor token.
- Wake-ups: while an entry has subscribers it is subscribed ONCE to the in-process
  `ObservableLedgerStore` appends (`on_append`: a set lookup on the run id, never a read); an
  append to the tree schedules one fold (the entry's pump, coalesced) whose delta every stream
  receives. Where appends come from another process (the split API process) or no observable
  store exists, each stream checks `list_after` every 0.25 s (no new bytes = nothing parsed);
  otherwise a slow safety check re-reads open runs' snapshots. Idle = zero parses.

Thread model: `refresh` and `subscribe` run in worker threads (`off_loop`) under the entry's
lock; deltas reach each subscriber's asyncio queue through `loop.call_soon_threadsafe`.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from abstractruntime.replay import ReplayView, diff_views, replay, resolve_cursor_token
from abstractruntime.replay.delta import LIST_KEYS, SCALAR_KEYS

# Records read per run per refresh pass (a pass loops until a run is drained).
_PAGE = 2000
_MAX_ENTRIES = 64


def _encode(event: str, data: Dict[str, Any], event_id: Optional[str] = None) -> bytes:
    head = f"id: {event_id}\n" if event_id else ""
    return (head + f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n").encode("utf-8")


@dataclass
class _Subscriber:
    """One stream: its loop, its frames, and the event that wakes it."""

    loop: asyncio.AbstractEventLoop
    queue: "asyncio.Queue[bytes]" = field(default_factory=asyncio.Queue)
    wake: asyncio.Event = field(default_factory=asyncio.Event)

    def deliver(self, frame: bytes) -> None:  # on the subscriber's loop
        self.queue.put_nowait(frame)
        self.wake.set()


@dataclass
class ViewEntry:
    """One run tree's view, its readers and its subscribers (see module docstring)."""

    root_run_id: str
    ledger_store: Any
    run_store: Any
    view: ReplayView
    lock: threading.Lock = field(default_factory=threading.Lock)
    subscribers: List[_Subscriber] = field(default_factory=list)
    json_cache: Optional[Tuple[str, bytes]] = None
    loaded_snapshots: set = field(default_factory=set)
    unsubscribe: Optional[Callable[[], None]] = None  # the ledger append subscription (while followed)
    tree_ids: frozenset = frozenset()  # the runs whose appends concern this view
    terminal: bool = False  # the tree is terminal (as of the last fold)
    pump_loop: Optional[asyncio.AbstractEventLoop] = None
    pump_lock: threading.Lock = field(default_factory=threading.Lock)
    pump_scheduled: bool = False
    pump_again: bool = False
    last_check: float = 0.0  # the last periodic check by any stream (monotonic)
    last_reload: float = 0.0  # the last snapshot re-read by any stream's safety check
    last_used: float = field(default_factory=time.monotonic)
    folds: int = 0  # apply() calls that folded records (observability, tests)
    records_folded: int = 0  # every record is folded once per entry, however many clients follow
    last_scalars: Optional[Dict[str, Any]] = None  # the scalars the last delta carried

    # ----------------------------------------------------------------- reads (worker thread)

    def _load_snapshots(self, run_ids: List[str], *, force: bool = False) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for rid in run_ids:
            if rid in self.loaded_snapshots and not force:
                continue
            try:
                snap = self.run_store.load(rid)
            except Exception:  # noqa: BLE001 - a missing snapshot leaves the ledger's word
                snap = None
            self.loaded_snapshots.add(rid)
            if snap is not None:
                out[rid] = snap
        return out

    def refresh_locked(self, *, reload_open_snapshots: bool = False) -> Optional[Dict[str, Any]]:
        """Fold every new record of the tree (caller holds `lock`); the delta, or None."""
        changed = False
        delta: Optional[Dict[str, Any]] = None
        snaps: Dict[str, Any] = {}
        if reload_open_snapshots:
            # A run the ledger has not ended yet: its snapshot may know more (a legacy ledger
            # without terminal records, a wait saved without a record).
            open_runs = [rid for rid, st in self.view.run_statuses().items() if st not in ("completed", "failed", "cancelled")]
            snaps.update(self._load_snapshots(open_runs, force=True))
        for _ in range(64):  # a pass can reveal children; bounded
            run_ids = self.view.run_ids
            snaps.update(self._load_snapshots(run_ids))
            batch: List[Dict[str, Any]] = []
            for rid in run_ids:
                after = self.view.last_seq(rid)
                while True:
                    try:
                        recs, _last = self.ledger_store.list_after(run_id=rid, after=after, limit=_PAGE)
                    except Exception:  # noqa: BLE001 - an unreadable ledger folds nothing
                        recs = []
                    if not recs:
                        break
                    batch.extend(recs)
                    after = int(recs[-1].get("seq") or after + len(recs))
                    if len(recs) < _PAGE:
                        break
            if not batch and not snaps:
                break
            d = self.view.apply(batch, runs_by_id=snaps)
            if batch:
                self.folds += 1
                self.records_folded += len(batch)
            snaps = {}
            changed = True
            delta = d if delta is None else _merge_deltas(delta, d)
            if self.view.run_ids == run_ids:
                break
        self.tree_ids = frozenset(self.view.run_ids)
        self.terminal = self.view.is_terminal()
        if delta is not None and not self._changes(delta):
            return None  # a re-read snapshot that changed nothing: no delta, no broadcast
        if changed:
            self.json_cache = None
        return delta

    def _changes(self, delta: Dict[str, Any]) -> bool:
        """Whether `delta` changes anything a client holds (an item, or a scalar's value)."""
        scalars = {k: delta.get(k) for k in SCALAR_KEYS if k in delta}
        same = scalars == self.last_scalars
        self.last_scalars = scalars
        return not same or any(delta.get(k) for k in LIST_KEYS)

    def view_bytes_locked(self) -> Tuple[str, bytes]:
        token = self.view.cursor["token"]
        if self.json_cache is None or self.json_cache[0] != token:
            self.json_cache = (token, json.dumps(self.view.to_json(), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        return self.json_cache

    def broadcast(self, delta: Dict[str, Any]) -> None:
        frame = _encode("delta", delta, delta.get("cursor", {}).get("token"))
        for sub in list(self.subscribers):
            try:
                sub.loop.call_soon_threadsafe(sub.deliver, frame)
            except RuntimeError:
                pass  # that subscriber's loop is gone; it unsubscribes on its way out

    # ----------------------------------------------------------------- the append pump

    def on_append(self, record: Any) -> None:
        """Ledger append callback (the runner's thread): never a read, only a coalesced wake-up.

        An append to a run of this tree schedules ONE fold for every subscriber (the pump); an
        append while a fold runs schedules one more pass. Appends to other runs cost a set
        lookup. A child the tree does not know yet is linked by its parent's own record, whose
        append wakes the pump."""
        rid = record.get("run_id") if isinstance(record, dict) else getattr(record, "run_id", None)
        if rid not in self.tree_ids:
            return
        with self.pump_lock:
            if self.pump_scheduled:
                self.pump_again = True
                return
            self.pump_scheduled = True
        loop = self.pump_loop
        try:
            if loop is None:
                raise RuntimeError("no loop")
            loop.call_soon_threadsafe(_start_pump, self)
        except RuntimeError:
            with self.pump_lock:
                self.pump_scheduled = False

    async def pump(self) -> None:
        from .work_pools import off_loop

        try:
            while True:
                await off_loop("heavy", poll, self)
                with self.pump_lock:
                    if not self.pump_again:
                        self.pump_scheduled = False
                        return
                    self.pump_again = False
        except BaseException:
            with self.pump_lock:
                self.pump_scheduled = False
            raise

    def view_at(self, token: str, *, as_of: bool = False) -> Optional[ReplayView]:
        """The view at an older cursor: each run's records up to the token's seq (None when the
        token is not one of this view's: an unknown run, a seq beyond what is folded).

        `as_of=False` (a reconnect) folds the runs' current snapshots, so the difference to the
        current view is only what the ledger added. `as_of=True` (`GET /view?at=`, a push
        opened at its seq) keeps only what a snapshot says that does not change as a run
        progresses (its workflow, parent, input, session): the view as the ledger stood then."""
        token_map = resolve_cursor_token(token, self.view.run_ids)
        if not token_map:
            return None
        records: Dict[str, List[Dict[str, Any]]] = {}
        for rid, seq in token_map.items():
            if seq > self.view.last_seq(rid):
                return None
            if seq <= 0:
                continue
            got: List[Dict[str, Any]] = []
            after = 0
            while after < seq:
                recs, _ = self.ledger_store.list_after(run_id=rid, after=after, limit=min(_PAGE, seq - after))
                if not recs:
                    break
                got.extend(r for r in recs if int(r.get("seq") or 0) <= seq)
                after = int(recs[-1].get("seq") or seq)
            records[rid] = got
        snaps = {}
        for rid in self.view.run_ids:
            try:
                snap = self.run_store.load(rid)
            except Exception:  # noqa: BLE001
                snap = None
            if snap is not None:
                snaps[rid] = _static_snapshot(snap) if as_of else snap
        return replay(records, snaps, root_run_id=self.root_run_id)


def _static_snapshot(run: Any) -> Dict[str, Any]:
    """A run snapshot without what changes as the run progresses (status, wait, output)."""
    d = run if isinstance(run, dict) else {k: getattr(run, k, None) for k in (
        "run_id", "workflow_id", "parent_run_id", "vars", "created_at", "session_id")}
    out = {k: d.get(k) for k in ("run_id", "workflow_id", "parent_run_id", "created_at", "session_id")}
    vars_ = d.get("input_data") if isinstance(d.get("input_data"), dict) else d.get("vars")
    if isinstance(vars_, dict):
        out["vars"] = vars_
    out["status"] = "running"
    return out


def _start_pump(entry: ViewEntry) -> None:  # on the pump loop
    asyncio.ensure_future(entry.pump())


def _attach_locked(entry: ViewEntry, loop: asyncio.AbstractEventLoop) -> None:
    """First subscriber: subscribe the entry (once) to the ledger's appends (caller holds lock)."""
    if entry.unsubscribe is not None:
        return
    from .ledger_tail import find_observable

    entry.pump_loop = loop
    observable = find_observable(entry.ledger_store)
    if observable is None:
        return
    try:
        entry.unsubscribe = observable.subscribe(entry.on_append)
    except Exception:  # noqa: BLE001 - the stream's poll still covers it
        entry.unsubscribe = None


def _merge_deltas(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """Two consecutive deltas as one (scalars from the later, items upserted by id)."""
    out = dict(b)
    for key, idk in (("runs", "run_id"), ("messages", "id"), ("activities", "id"), ("artifacts", "id")):
        items = {str(it.get(idk)): it for it in a.get(key) or []}
        for it in b.get(key) or []:
            items[str(it.get(idk))] = it
        out[key] = list(items.values())
    out["changes"] = list(a.get("changes") or []) + list(b.get("changes") or [])
    return out


class ViewHub:
    """The process's cache of view entries (bounded LRU)."""

    def __init__(self, max_entries: int = _MAX_ENTRIES) -> None:
        self._lock = threading.Lock()
        self._entries: "OrderedDict[Tuple[int, str], ViewEntry]" = OrderedDict()
        self._max = int(max_entries)

    def entry(self, *, ledger_store: Any, run_store: Any, root_run_id: str) -> ViewEntry:
        key = (id(ledger_store), str(root_run_id))
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.ledger_store is not ledger_store:
                entry = ViewEntry(root_run_id=str(root_run_id), ledger_store=ledger_store, run_store=run_store,
                                  view=ReplayView(str(root_run_id)))
                self._entries[key] = entry
            self._entries.move_to_end(key)
            entry.last_used = time.monotonic()
            self._evict_locked()
            return entry

    def _evict_locked(self) -> None:
        if len(self._entries) <= self._max:
            return
        for key in list(self._entries):
            if len(self._entries) <= self._max:
                break
            if not self._entries[key].subscribers:
                del self._entries[key]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


_HUB = ViewHub()


def get_view_hub() -> ViewHub:
    return _HUB


# --------------------------------------------------------------------------- helpers for routes


def read_view(entry: ViewEntry) -> Tuple[str, bytes, Optional[Dict[str, Any]]]:
    """Worker thread: fold what is new, return (token, view json bytes, delta folded)."""
    with entry.lock:
        delta = entry.refresh_locked(reload_open_snapshots=True)
        token, body = entry.view_bytes_locked()
        if delta is not None and entry.subscribers:
            entry.broadcast(delta)
    return token, body, delta


def subscribe(entry: ViewEntry, sub: _Subscriber, last_token: Optional[str]) -> Tuple[List[bytes], bool]:
    """Worker thread: register `sub` and return the frames that bring it to the current view.

    The registration and the frames are taken under the entry lock, so every later delta
    follows the state those frames describe. Returns (frames, terminal)."""
    with entry.lock:
        delta = entry.refresh_locked(reload_open_snapshots=True)
        if delta is not None and entry.subscribers:
            entry.broadcast(delta)
        token, body = entry.view_bytes_locked()
        entry.subscribers.append(sub)
        _attach_locked(entry, sub.loop)
        terminal = entry.view.is_terminal()
        current = entry.view.to_json() if last_token and last_token != token else None
    if last_token and last_token == token:
        return [], terminal
    if last_token and current is not None:
        try:
            with entry.lock:
                old = entry.view_at(last_token)
        except Exception:  # noqa: BLE001 - a reconnect we cannot rebuild gets the whole view
            old = None
        if old is not None:
            delta = diff_views(old.to_json(), current)
            # What a snapshot contributed may differ between the client's copy (folded with the
            # snapshot of its time) and this rebuild (today's snapshot): send those whole.
            delta["runs"] = current.get("runs") or []
            ids = {m.get("id") for m in delta.get("messages") or []}
            delta["messages"] = (delta.get("messages") or []) + [
                m for m in current.get("messages") or []
                if (m.get("source") in ("run_input", "run_output") or m.get("kind") == "final") and m.get("id") not in ids]
            return [_encode("delta", delta, token)], terminal
    return [b"id: " + token.encode("utf-8") + b"\nevent: view\ndata: " + body + b"\n\n"], terminal


def read_view_at(entry: ViewEntry, token: str) -> Optional[bytes]:
    """Worker thread: the view at a cursor token (JSON bytes), None when the token is not one."""
    with entry.lock:
        entry.refresh_locked()
        try:
            old = entry.view_at(token, as_of=True)
        except Exception:  # noqa: BLE001 - an unreadable ledger: no view at that cursor
            old = None
    if old is None:
        return None
    return json.dumps(old.to_json(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def unsubscribe(entry: ViewEntry, sub: _Subscriber) -> None:
    with entry.lock:
        try:
            entry.subscribers.remove(sub)
        except ValueError:
            pass
        if not entry.subscribers and entry.unsubscribe is not None:
            try:
                entry.unsubscribe()
            except Exception:  # noqa: BLE001
                pass
            entry.unsubscribe = None
            entry.pump_loop = None


def followed(entry: ViewEntry) -> bool:
    """Whether appends reach this entry by subscription (else its streams poll)."""
    return entry.unsubscribe is not None


def poll(entry: ViewEntry, *, reload_open_snapshots: bool = False, check_every: float = 0.0,
         reload_every: float = 0.0) -> Tuple[bool, bool]:
    """Worker thread: fold and broadcast what is new. Returns (folded anything, terminal).

    `check_every` / `reload_every` make a periodic check shared by the entry's streams: when
    another stream of the same view checked (re-read snapshots) less than that many seconds ago,
    this one skips it, so N idle streams cost one check per period, not N."""
    with entry.lock:
        now = time.monotonic()
        if check_every and now - entry.last_check < check_every:
            return False, entry.terminal
        entry.last_check = now
        if reload_open_snapshots and reload_every:
            if now - entry.last_reload < reload_every:
                reload_open_snapshots = False
            else:
                entry.last_reload = now
        delta = entry.refresh_locked(reload_open_snapshots=reload_open_snapshots)
        if delta is not None:
            entry.broadcast(delta)
        return delta is not None, entry.view.is_terminal()


def tree_run_ids(entry: ViewEntry) -> set:
    with entry.lock:
        return set(entry.view.run_ids)
