"""abstractgateway.live_deltas -- the gateway half of live token streaming.

The runtime (``abstractruntime.core.live_deltas``) hands a registered sink one
plain dict per coalesced text fragment of an LLM call and one when the call
ends::

    {"kind": "llm.delta", "run_id", "parent_run_id", "node_id", "call_id", "seq", "text", "channel"}
    {"kind": "llm.delta_end", "run_id", "parent_run_id", "node_id", "call_id", "seq", "reason" [, "detail"]}

This module turns that into what a client sees on ``GET /runs/{id}/ledger/stream``:

- ``LiveDeltaHub`` (in memory) keeps, per ROOT run, the text of every call that
  is still open. A subscriber gets one snapshot frame per open call (and
  channel), then every later event. Frames carry the emitting ``run_id`` and
  the ``root_run_id``; a subscription to a run receives the events of that run
  and of every run below it (a root subscription sees the whole tree, a child
  subscription only its own subtree).
- Nothing is dropped and nothing is capped: a subscriber's pending work is at
  most ONE entry per call (a cursor into that call's events), so a slow
  client catches up from the call's own text instead of overflowing a frame
  queue. A call's text is freed when the call ends; a run's state is freed
  when the run reaches a terminal status. At that moment every call still
  open gets a synthetic ``llm.delta_end`` (reason ``cancelled`` or
  ``failed``, ``synthetic: true``) so no client is left with a frozen bubble.
- Split deployments (``serve --no-runner`` + ``abstractgateway runner``) run
  the model calls in another process. There the runner writes the events to
  ``<data dir>/live/<root_run_id>.deltas.jsonl`` (``FileDeltaSink``: 0600,
  one JSON line per event, deleted when the root run ends) and the API
  process tails that file (``_FileTail``: partial lines are buffered, a file
  deleted or recreated under the reader is detected) into its own hub while
  someone is watching. A startup sweep deletes the files of runs that are
  finished (or gone); it never touches a file whose run may still write.

Nothing here is durable, and nothing here reaches the ledger. Losing a delta
loses a live preview, never an answer: the durable LLM_CALL record is written
before the call's ``llm.delta_end``.

The process role (``combined`` by default, ``api`` for ``serve --no-runner``,
``runner`` for ``abstractgateway runner``) is set by the CLI with
``set_process_role``; it decides whether this process's runtimes publish into
the hub or into files, and whether the hub tails files.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

DELTA_KINDS = ("llm.delta", "llm.delta_end")
LIVE_DIR_NAME = "live"
LIVE_FILE_SUFFIX = ".deltas.jsonl"
TERMINAL_STATUSES = ("completed", "failed", "cancelled")

ROLE_COMBINED = "combined"
ROLE_API = "api"
ROLE_RUNNER = "runner"
_ROLES = (ROLE_COMBINED, ROLE_API, ROLE_RUNNER)

# How often the API process's tailer thread looks at the files it follows (only
# while at least one client is watching one of them). The runtime batches
# deltas every 40 ms, so a shorter poll would only burn stat() calls.
FILE_TAIL_POLL_S = 0.03

_process_role = ROLE_COMBINED
_role_lock = threading.Lock()

# The AbstractRuntime release this gateway needs (pyproject `AbstractRuntime>=`
# says the same; tests/test_gateway_live_delta_stream.py keeps the two equal).
# It carries: live token deltas with `parent_run_id` (set_live_delta_sink),
# the S-2 parity gate, and the host's built-in tool deny
# (`workspace_builtin_deny_prefixes` / `workspace_builtin_allow`, enforced and
# never written into the prompt: runtime 6567ed4). An older runtime would
# silently ignore the deny keys, so `require_runtime_features` refuses to
# build a host on it.
ABSTRACTRUNTIME_FLOOR = "0.5.0"


class LiveDeltaError(RuntimeError):
    """A live-delta event or seam that does not match the contract."""


def set_process_role(role: str) -> None:
    """Declare what this process is: ``combined`` (API + runner, the default),
    ``api`` (``serve --no-runner``) or ``runner`` (``abstractgateway runner``).
    Set once by the CLI before any runtime is built."""

    global _process_role
    if role not in _ROLES:
        raise ValueError(f"unknown gateway process role {role!r}; expected one of {_ROLES}")
    with _role_lock:
        _process_role = role


def process_role() -> str:
    return _process_role


def scope_key(data_dir: Any) -> str:
    """The tenancy key of a data folder: every run store is per data folder,
    so hub state is too (two users never share a key, even for equal ids)."""

    return str(Path(str(data_dir)).expanduser().resolve())


def _absolute_data_dir(data_dir: Any) -> Path:
    """Live files are only ever written inside an ABSOLUTE data folder: a
    relative scope would land wherever the process happens to run."""
    p = Path(str(scope_key(data_dir)))
    if not p.is_absolute():
        raise LiveDeltaError(f"live delta files need an absolute data folder, got {str(data_dir)!r}")
    return p


def live_dir(data_dir: Any) -> Path:
    return _absolute_data_dir(data_dir) / LIVE_DIR_NAME


def live_file_path(data_dir: Any, root_run_id: str) -> Path:
    rid = str(root_run_id or "").strip()
    if not rid or "/" in rid or "\\" in rid or rid in {".", ".."}:
        raise LiveDeltaError(f"invalid root run id for a live delta file: {root_run_id!r}")
    return live_dir(data_dir) / f"{rid}{LIVE_FILE_SUFFIX}"


def _status_str(status: Any) -> str:
    s = getattr(status, "value", None) or str(status or "")
    return str(s).strip().lower()


def synthetic_end_reason(status: Any) -> str:
    """A run that ended with calls still open: `failed` when the run failed,
    `cancelled` otherwise (a stop, a kill switch, a run that completed
    without its call ever closing)."""

    return "failed" if _status_str(status) == "failed" else "cancelled"


# ---------------------------------------------------------------------------
# Root resolution (once per run, through the run store's parent chain)
# ---------------------------------------------------------------------------


class RootResolver:
    """`chain(run_id)` = [run_id, parent, grandparent, ..., root], resolved
    once per run from the run store and cached until the root ends."""

    def __init__(self, run_store: Any) -> None:
        self._run_store = run_store
        self._lock = threading.Lock()
        self._chains: Dict[str, Tuple[str, ...]] = {}

    def chain(self, run_id: str, parent_run_id: Optional[str] = None, *, known_parent: bool = False) -> Tuple[str, ...]:
        rid = str(run_id or "").strip()
        if not rid:
            raise LiveDeltaError("live delta event without a run_id")
        with self._lock:
            hit = self._chains.get(rid)
        if hit is not None:
            return hit
        out: List[str] = [rid]
        seen = {rid}
        if known_parent:
            parent = str(parent_run_id).strip() if parent_run_id else None
        else:
            run = self._run_store.load(rid)
            parent = str(getattr(run, "parent_run_id", None) or "").strip() or None if run is not None else None
        while parent:
            if parent in seen:
                raise LiveDeltaError(f"run {rid} has a cycle in its parent chain at {parent}")
            with self._lock:
                cached = self._chains.get(parent)
            if cached is not None:
                out.extend(cached)
                break
            out.append(parent)
            seen.add(parent)
            run = self._run_store.load(parent)
            parent = (str(getattr(run, "parent_run_id", None) or "").strip() or None) if run is not None else None
        chain = tuple(out)
        with self._lock:
            # Every ancestor's own chain is a suffix of this one: cache them all.
            for i in range(len(chain)):
                self._chains.setdefault(chain[i], chain[i:])
        return chain

    def forget_root(self, root_run_id: str) -> None:
        with self._lock:
            for k in [k for k, v in self._chains.items() if v and v[-1] == root_run_id]:
                del self._chains[k]


_resolvers: Dict[str, RootResolver] = {}
_resolvers_lock = threading.Lock()


def resolver_for(data_dir: Any, run_store: Any) -> RootResolver:
    key = scope_key(data_dir)
    with _resolvers_lock:
        r = _resolvers.get(key)
        if r is None:
            r = RootResolver(run_store)
            _resolvers[key] = r
        return r


def validate_event(event: Any) -> Dict[str, Any]:
    if not isinstance(event, dict):
        raise LiveDeltaError(f"live delta event must be a dict, got {type(event).__name__}")
    kind = event.get("kind")
    if kind not in DELTA_KINDS:
        raise LiveDeltaError(f"unknown live delta kind {kind!r}; expected one of {DELTA_KINDS}")
    for k in ("run_id", "call_id"):
        if not isinstance(event.get(k), str) or not event.get(k):
            raise LiveDeltaError(f"live delta event without {k}: {event!r}")
    if not isinstance(event.get("seq"), int) or isinstance(event.get("seq"), bool):
        raise LiveDeltaError(f"live delta event without an integer seq: {event!r}")
    return event


# ---------------------------------------------------------------------------
# The in-memory hub
# ---------------------------------------------------------------------------


class _Call:
    __slots__ = ("run_id", "call_id", "node_id", "parent_run_id", "root_run_id", "chain", "frames", "last_seq", "ended")

    def __init__(self, *, event: Dict[str, Any], chain: Tuple[str, ...]) -> None:
        self.run_id = str(event["run_id"])
        self.call_id = str(event["call_id"])
        self.node_id = event.get("node_id")
        self.parent_run_id = event.get("parent_run_id")
        self.root_run_id = chain[-1]
        self.chain = chain
        self.frames: List[Dict[str, Any]] = []
        self.last_seq = -1
        self.ended = False

    def snapshot_frames(self) -> List[Dict[str, Any]]:
        """One frame per channel that has text, the accumulated text, in the
        order the channels first appeared; `seq` = the last delta's seq."""

        texts: Dict[str, List[str]] = {}
        last_seq = -1
        for f in self.frames:
            if f.get("kind") != "llm.delta":
                continue
            texts.setdefault(str(f.get("channel") or "content"), []).append(str(f.get("text") or ""))
            last_seq = int(f["seq"])
        out: List[Dict[str, Any]] = []
        for channel, parts in texts.items():
            out.append(
                {
                    "kind": "llm.delta",
                    "run_id": self.run_id,
                    "parent_run_id": self.parent_run_id,
                    "root_run_id": self.root_run_id,
                    "node_id": self.node_id,
                    "call_id": self.call_id,
                    "seq": last_seq,
                    "text": "".join(parts),
                    "channel": channel,
                    "snapshot": True,
                }
            )
        return out


@dataclass
class _RootState:
    calls: Dict[str, _Call] = field(default_factory=dict)  # open calls only
    subs: List["LiveSubscription"] = field(default_factory=list)
    # Calls closed by the hub (synthetic end) whose real runtime delta_end has
    # not arrived yet: their late events are dropped, never reopened.
    closing: set = field(default_factory=set)
    terminal: bool = False

    def idle(self) -> bool:
        # A finished root with nobody watching is freed even while late ends
        # are still due: a late event recreates a short-lived entry that the
        # runtime's own delta_end then closes.
        return not self.calls and not self.subs and (not self.closing or self.terminal)


class LiveSubscription:
    """One watcher of one run (and its subtree). Created by `LiveDeltaHub.subscribe`.

    `snapshot`: the frames to send first (one per open call and channel).
    `ready`: an asyncio.Event set when `drain()` has something.
    `drain()`: every frame not sent yet, in per-call order.
    """

    def __init__(self, hub: "LiveDeltaHub", key: Tuple[str, str], run_id: str, loop: asyncio.AbstractEventLoop) -> None:
        self._hub = hub
        self.key = key
        self.run_id = run_id
        self._loop = loop
        self.ready = asyncio.Event()
        self.snapshot: List[Dict[str, Any]] = []
        self._cursors: Dict[str, int] = {}
        # ONE pending entry per call (never a frame queue): call_id -> call.
        self._dirty: Dict[str, _Call] = {}
        self._closed = False
        self._file_tail_key: Optional[Tuple[str, str]] = None

    def matches(self, call: _Call) -> bool:
        return self.run_id in call.chain

    def _notify(self, call: _Call) -> None:  # hub lock held
        if self._closed:
            return
        self._dirty[call.call_id] = call
        try:
            self._loop.call_soon_threadsafe(self.ready.set)
        except RuntimeError:
            pass  # loop closed: the stream is ending

    def drain(self) -> List[Dict[str, Any]]:
        self.ready.clear()
        out: List[Dict[str, Any]] = []
        with self._hub._lock:
            dirty, self._dirty = self._dirty, {}
            for call_id, call in dirty.items():
                start = self._cursors.get(call_id, 0)
                out.extend(call.frames[start:])
                if call.ended:
                    self._cursors.pop(call_id, None)
                else:
                    self._cursors[call_id] = len(call.frames)
        return out

    @property
    def pending_calls(self) -> int:
        with self._hub._lock:
            return len(self._dirty)

    def close(self) -> None:
        self._hub._unsubscribe(self)


class LiveDeltaHub:
    """Per-(data folder, root run) live call state and its subscribers."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._roots: Dict[Tuple[str, str], _RootState] = {}
        self._tails = _TailRegistry(self)

    # -- producer side (any thread) -----------------------------------------
    def publish(self, scope: str, event: Dict[str, Any], chain: Sequence[str]) -> None:
        validate_event(event)
        chain_t = tuple(str(c) for c in chain)
        if not chain_t or chain_t[0] != event["run_id"]:
            raise LiveDeltaError(f"live delta chain {chain_t!r} does not start at the event's run {event['run_id']!r}")
        key = (scope, chain_t[-1])
        call_id = str(event["call_id"])
        kind = event["kind"]
        with self._lock:
            st = self._roots.get(key)
            if st is None:
                st = _RootState()
                self._roots[key] = st
            if call_id in st.closing:
                # The hub already closed this call (the run ended first); the
                # runtime's own end arrives late. Nothing reopens.
                if kind == "llm.delta_end":
                    st.closing.discard(call_id)
                self._gc_locked(key, st)
                return
            call = st.calls.get(call_id)
            if call is None:
                if st.terminal and kind == "llm.delta":
                    self._gc_locked(key, st)
                    return
                call = _Call(event=event, chain=chain_t)
                if kind == "llm.delta":
                    st.calls[call_id] = call
            if int(event["seq"]) <= call.last_seq:
                return  # already seen (a file replayed from the start)
            frame = dict(event)
            frame["root_run_id"] = chain_t[-1]
            frame["snapshot"] = False
            call.frames.append(frame)
            call.last_seq = int(event["seq"])
            if kind == "llm.delta_end":
                call.ended = True
                st.calls.pop(call_id, None)
            for sub in st.subs:
                if sub.matches(call):
                    sub._notify(call)
            self._gc_locked(key, st)

    def run_terminal(self, scope: str, run_id: str, status: Any, chain: Sequence[str]) -> int:
        """The run reached a terminal status: every call of it (and of its
        subtree) still open gets a synthetic `llm.delta_end`; a ROOT run's
        state is then freed. Returns the number of calls closed."""

        chain_t = tuple(str(c) for c in chain)
        key = (scope, chain_t[-1])
        reason = synthetic_end_reason(status)
        closed = 0
        with self._lock:
            st = self._roots.get(key)
            if st is None:
                return 0
            for call in [c for c in st.calls.values() if run_id in c.chain]:
                frame = {
                    "kind": "llm.delta_end",
                    "run_id": call.run_id,
                    "parent_run_id": call.parent_run_id,
                    "root_run_id": call.root_run_id,
                    "node_id": call.node_id,
                    "call_id": call.call_id,
                    "seq": call.last_seq + 1,
                    "reason": reason,
                    "synthetic": True,
                    "snapshot": False,
                }
                call.frames.append(frame)
                call.last_seq += 1
                call.ended = True
                st.calls.pop(call.call_id, None)
                st.closing.add(call.call_id)
                closed += 1
                for sub in st.subs:
                    if sub.matches(call):
                        sub._notify(call)
            if run_id == chain_t[-1]:
                st.terminal = True
            self._gc_locked(key, st)
        return closed

    def _gc_locked(self, key: Tuple[str, str], st: _RootState) -> None:
        if st.idle() and self._roots.get(key) is st:
            del self._roots[key]

    # -- consumer side (event loop) -----------------------------------------
    def subscribe(
        self,
        scope: str,
        chain: Sequence[str],
        *,
        loop: asyncio.AbstractEventLoop,
        data_dir: Optional[Any] = None,
    ) -> LiveSubscription:
        """Watch `chain[0]` (a run) and its subtree; `chain` is that run's
        parent chain up to its root (resolved by the caller in the caller's
        own run store: tenancy is checked before this is ever reached).

        With `data_dir` in the API role of a split deployment, the root's
        live file is tailed (and caught up) before the snapshot is taken.
        Blocking (file I/O): call it off the event loop."""

        chain_t = tuple(str(c) for c in chain)
        key = (scope, chain_t[-1])
        sub = LiveSubscription(self, key, chain_t[0], loop)
        with self._lock:
            st = self._roots.get(key)
            if st is None:
                st = _RootState()
                self._roots[key] = st
            st.subs.append(sub)
        if data_dir is not None and process_role() == ROLE_API:
            sub._file_tail_key = key
            self._tails.acquire(key, live_file_path(data_dir, chain_t[-1]))
        with self._lock:
            st = self._roots.get(key)
            snap: List[Dict[str, Any]] = []
            if st is not None:
                for call in st.calls.values():
                    if sub.matches(call):
                        snap.extend(call.snapshot_frames())
                        sub._cursors[call.call_id] = len(call.frames)
            # The snapshot is authoritative: anything that landed while the
            # subscription was being set up is either in it (open calls) or
            # already closed (the ledger tells the client).
            sub._dirty.clear()
            sub.snapshot = snap
        return sub

    def _drop_unwatched(self, key: Tuple[str, str]) -> None:
        """API role: a root's state is only what its file said; once nobody
        tails the file any more, that state is stale and is dropped (the
        next subscriber replays the file from its start)."""

        with self._lock:
            st = self._roots.get(key)
            if st is not None and not st.subs:
                del self._roots[key]

    def _unsubscribe(self, sub: LiveSubscription) -> None:
        with self._lock:
            sub._closed = True
            st = self._roots.get(sub.key)
            if st is not None and sub in st.subs:
                st.subs.remove(sub)
                self._gc_locked(sub.key, st)
        if sub._file_tail_key is not None:
            key, sub._file_tail_key = sub._file_tail_key, None
            self._tails.release(key)

    # -- introspection (tests, health) --------------------------------------
    def open_calls(self, scope: str, root_run_id: str) -> List[str]:
        with self._lock:
            st = self._roots.get((scope, root_run_id))
            return sorted(st.calls) if st is not None else []

    def tracked_roots(self) -> List[Tuple[str, str]]:
        with self._lock:
            return sorted(self._roots)


# ---------------------------------------------------------------------------
# Split mode: the runner writes files, the API process tails them
# ---------------------------------------------------------------------------


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


class FileDeltaSink:
    """Runner-process sink: appends each event (with its chain) as one JSON
    line to `<data dir>/live/<root_run_id>.deltas.jsonl`, created 0600. The
    file of a root run is deleted when that run ends (after a terminal line
    the API's tailer reads from its still-open descriptor)."""

    def __init__(self, data_dir: Any) -> None:
        self._data_dir = str(_absolute_data_dir(data_dir))
        self._lock = threading.Lock()
        self._fds: Dict[str, int] = {}
        self._open_calls: Dict[str, set] = {}
        # Roots that ended while calls were still open: their late events are
        # dropped (the file is gone and must not come back).
        self._closing: Dict[str, set] = {}

    def _fd_locked(self, root: str) -> int:
        fd = self._fds.get(root)
        if fd is not None:
            return fd
        path = live_file_path(self._data_dir, root)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
        self._fds[root] = fd
        return fd

    def publish(self, scope: str, event: Dict[str, Any], chain: Sequence[str]) -> None:
        validate_event(event)
        chain_t = [str(c) for c in chain]
        root = chain_t[-1]
        call_id = str(event["call_id"])
        line = json.dumps({"event": event, "chain": chain_t}, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            closing = self._closing.get(root)
            if closing is not None:
                if call_id in closing and event["kind"] == "llm.delta_end":
                    closing.discard(call_id)
                    if not closing:
                        del self._closing[root]
                return
            opened = self._open_calls.setdefault(root, set())
            if event["kind"] == "llm.delta":
                opened.add(call_id)
            else:
                opened.discard(call_id)
            _write_all(self._fd_locked(root), line.encode("utf-8"))

    def run_terminal(self, scope: str, run_id: str, status: Any, chain: Sequence[str]) -> int:
        chain_t = [str(c) for c in chain]
        root = chain_t[-1]
        path = live_file_path(self._data_dir, root)
        with self._lock:
            if root not in self._fds and not path.exists():
                return 0
            line = json.dumps(
                {"terminal": {"run_id": str(run_id), "status": _status_str(status)}, "chain": chain_t},
                ensure_ascii=False,
                separators=(",", ":"),
            ) + "\n"
            _write_all(self._fd_locked(root), line.encode("utf-8"))
            if run_id != root:
                return 1
            fd = self._fds.pop(root, None)
            if fd is not None:
                os.close(fd)
            opened = self._open_calls.pop(root, set())
            if opened:
                self._closing[root] = set(opened)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        return 1

    def close(self) -> None:
        with self._lock:
            for fd in self._fds.values():
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._fds.clear()


class _FileTail:
    """Follows one live file: buffers a partial last line, and notices when
    the file is deleted (stop at its end) or recreated (switch to the new
    file from its start once the old one is read to its end)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: Optional[int] = None
        self._ino: Optional[Tuple[int, int]] = None
        self._buf = b""
        self.refs = 0
        self.lock = threading.Lock()

    def read_lines(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for _ in range(4):  # at most: finish the old file, then the new one
            if self._fd is None:
                try:
                    fd = os.open(str(self.path), os.O_RDONLY)
                except FileNotFoundError:
                    return out
                st = os.fstat(fd)
                self._fd, self._ino, self._buf = fd, (st.st_dev, st.st_ino), b""
            while True:
                chunk = os.read(self._fd, 1 << 16)
                if not chunk:
                    break
                self._buf += chunk
            *lines, self._buf = self._buf.split(b"\n")
            for raw in lines:
                if not raw.strip():
                    continue
                try:
                    obj = json.loads(raw.decode("utf-8"))
                except Exception:
                    logger.warning("live delta file %s: unreadable line skipped", self.path)
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
            try:
                st = os.stat(self.path)
                same = (st.st_dev, st.st_ino) == self._ino
            except FileNotFoundError:
                same = None
            if same is True:
                return out
            # Deleted (None) or recreated (False): this file is finished.
            self.close()
            if same is None:
                return out
        return out

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
        self._fd, self._ino, self._buf = None, None, b""


class _TailRegistry:
    """The API process's file tails (one per watched root) and the single
    daemon thread that pumps them while at least one is active."""

    def __init__(self, hub: LiveDeltaHub) -> None:
        self._hub = hub
        self._lock = threading.Lock()
        self._tails: Dict[Tuple[str, str], _FileTail] = {}
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def acquire(self, key: Tuple[str, str], path: Path) -> None:
        with self._lock:
            tail = self._tails.get(key)
            if tail is None:
                tail = _FileTail(path)
                self._tails[key] = tail
            tail.refs += 1
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="live-delta-tail", daemon=True)
                self._thread.start()
            self._wake.set()
        self.pump(key)  # catch up now, before the caller takes its snapshot

    def release(self, key: Tuple[str, str]) -> None:
        with self._lock:
            tail = self._tails.get(key)
            if tail is None:
                return
            tail.refs -= 1
            if tail.refs > 0:
                return
            del self._tails[key]
        with tail.lock:
            tail.close()
        self._hub._drop_unwatched(key)

    def pump(self, key: Tuple[str, str]) -> None:
        with self._lock:
            tail = self._tails.get(key)
        if tail is None:
            return
        # Read AND publish under the tail's lock: two pumps (the thread and a
        # new subscriber's catch-up) must never publish out of order.
        with tail.lock:
            self._publish_lines(key[0], tail, tail.read_lines())

    def _publish_lines(self, scope: str, tail: _FileTail, lines: List[Dict[str, Any]]) -> None:
        for obj in lines:
            chain = obj.get("chain")
            if not isinstance(chain, list) or not chain:
                logger.warning("live delta file %s: line without a chain skipped", tail.path)
                continue
            try:
                if isinstance(obj.get("event"), dict):
                    self._hub.publish(scope, obj["event"], chain)
                elif isinstance(obj.get("terminal"), dict):
                    t = obj["terminal"]
                    self._hub.run_terminal(scope, str(t.get("run_id") or ""), t.get("status"), chain)
            except LiveDeltaError as exc:
                logger.warning("live delta file %s: %s", tail.path, exc)

    def _loop(self) -> None:
        while True:
            with self._lock:
                keys = list(self._tails)
                if not keys:
                    self._wake.clear()
            if not keys:
                self._wake.wait()
                continue
            for key in keys:
                try:
                    self.pump(key)
                except Exception:  # noqa: BLE001 - one bad file never stops the others
                    logger.exception("live delta tail of %s failed", key)
            self._wake.wait(FILE_TAIL_POLL_S)


# ---------------------------------------------------------------------------
# Process wiring
# ---------------------------------------------------------------------------

_hub = LiveDeltaHub()
_file_sinks: Dict[str, FileDeltaSink] = {}
_file_sinks_lock = threading.Lock()


def get_hub() -> LiveDeltaHub:
    return _hub


def _file_sink_for(data_dir: Any) -> FileDeltaSink:
    key = scope_key(data_dir)
    with _file_sinks_lock:
        sink = _file_sinks.get(key)
        if sink is None:
            sink = FileDeltaSink(key)
            _file_sinks[key] = sink
        return sink


def _backend_for(data_dir: Any) -> Any:
    return _file_sink_for(data_dir) if process_role() == ROLE_RUNNER else _hub


def close_run_live_state(run: Any, *, data_dir: Any, run_store: Any) -> int:
    """A run reached a terminal status: close its live state in this process
    (hub: a synthetic `llm.delta_end` for every call of it, or of its subtree,
    still open, and a root's state freed; runner process: a terminal line in
    the root's live file, the file closed and deleted when the run is the
    root). Returns the number of calls closed (hub) or lines written (file).

    Every code path that ends a run WITHOUT a Runtime (the stop kill switch,
    the runner's unresolvable-workflow and tick-exception promotions) must
    call this after saving the status; runs ended through a Runtime get it
    from `attach_terminal_hook`."""

    rid = str(getattr(run, "run_id", "") or "").strip()
    if not rid:
        raise LiveDeltaError("cannot close the live state of a run without an id")
    scope = scope_key(data_dir)
    resolver = resolver_for(scope, run_store)
    chain = resolver.chain(rid, getattr(run, "parent_run_id", None), known_parent=True)
    try:
        return _backend_for(scope).run_terminal(scope, rid, getattr(run, "status", None), chain)
    finally:
        if chain[-1] == rid:
            resolver.forget_root(rid)


def attach_terminal_hook(runtime: Any, *, data_dir: Any, run_store: Any) -> None:
    """Close the live state of a run when it reaches a terminal status on
    `runtime` (every Runtime object that can end a run in this process: the
    host's, and the ones a runner builds to apply cancel/pause commands)."""

    scope = scope_key(data_dir)

    def _on_terminal(run: Any) -> None:
        if str(getattr(run, "run_id", "") or "").strip():
            close_run_live_state(run, data_dir=scope, run_store=run_store)

    runtime.add_terminal_hook(_on_terminal)


class RuntimeTooOld(RuntimeError):
    """The installed AbstractRuntime lacks a feature this gateway relies on."""


def require_runtime_features(runtime: Any) -> None:
    """Refuse, loudly and at host build, a runtime without the features the
    gateway depends on (probed by their symbols, not the version string, so a
    source checkout works as soon as it has them)."""

    missing: List[str] = []
    if not callable(getattr(runtime, "set_live_delta_sink", None)) or not callable(getattr(runtime, "add_terminal_hook", None)):
        missing.append("live token deltas (Runtime.set_live_delta_sink / add_terminal_hook)")
    try:
        from abstractruntime.integrations.abstractcore import workspace_scoped_tools as wst

        fields = getattr(wst.WorkspaceScope, "__dataclass_fields__", {}) or {}
        if "builtin_deny_prefixes" not in fields or "builtin_allow" not in fields:
            missing.append("the host's built-in tool deny (WorkspaceScope.builtin_deny_prefixes / builtin_allow)")
    except Exception as exc:  # noqa: BLE001 - a missing module is the same answer
        missing.append(f"the workspace-scoped tools ({type(exc).__name__}: {exc})")
    if missing:
        try:
            from importlib.metadata import version

            installed = version("abstractruntime")
        except Exception:  # noqa: BLE001
            installed = "unknown"
        raise RuntimeTooOld(
            f"this gateway needs abstractruntime>={ABSTRACTRUNTIME_FLOOR}; the installed abstractruntime "
            f"({installed}) lacks: {'; '.join(missing)}. Upgrade it: pip install -U 'abstractruntime>={ABSTRACTRUNTIME_FLOOR}'"
        )


def install_live_delta_sink(runtime: Any, *, data_dir: Any, run_store: Any) -> None:
    """Register this process's live-delta sink on a freshly built runtime.

    The seam fails loudly: a runtime without `set_live_delta_sink` /
    `add_terminal_hook` is a runtime this gateway cannot stream with."""

    require_runtime_features(runtime)
    scope = scope_key(data_dir)
    resolver = resolver_for(scope, run_store)
    backend = _backend_for(scope)

    def _sink(event: Dict[str, Any]) -> None:
        validate_event(event)
        chain = resolver.chain(event["run_id"], event.get("parent_run_id"), known_parent=True)
        backend.publish(scope, event, chain)

    runtime.set_live_delta_sink(_sink)
    attach_terminal_hook(runtime, data_dir=scope, run_store=run_store)


def resolve_chain_in_store(run_store: Any, run: Any, *, run_id: Optional[str] = None) -> Tuple[str, ...]:
    """The parent chain of `run` in THIS run store (the caller's): used by the
    SSE route after its tenancy check, never a cache another user filled."""

    rid = str(run_id or getattr(run, "run_id", "") or "").strip()
    if not rid:
        raise LiveDeltaError("cannot resolve the live-delta chain of a run without an id")
    out = [rid]
    seen = {rid}
    parent = str(getattr(run, "parent_run_id", None) or "").strip() or None
    while parent:
        if parent in seen:
            raise LiveDeltaError(f"run {rid} has a cycle in its parent chain at {parent}")
        out.append(parent)
        seen.add(parent)
        p = run_store.load(parent)
        parent = (str(getattr(p, "parent_run_id", None) or "").strip() or None) if p is not None else None
    return tuple(out)


def sweep_finished_live_files(data_dir: Any, run_store: Any) -> List[str]:
    """Delete the live files of runs that are finished (terminal status) or
    no longer exist. A file whose run may still be written by a runner in
    another process is never touched. Returns the deleted paths."""

    base = live_dir(data_dir)
    deleted: List[str] = []
    try:
        entries = list(os.scandir(base))
    except FileNotFoundError:
        return deleted
    for entry in entries:
        name = entry.name
        if not name.endswith(LIVE_FILE_SUFFIX) or not entry.is_file(follow_symlinks=False):
            continue
        rid = name[: -len(LIVE_FILE_SUFFIX)]
        try:
            run = run_store.load(rid)
        except Exception:
            logger.warning("live delta sweep: could not read run %s; its file is kept", rid, exc_info=True)
            continue
        if run is not None and _status_str(getattr(run, "status", None)) not in TERMINAL_STATUSES:
            continue
        try:
            os.unlink(entry.path)
            deleted.append(entry.path)
        except FileNotFoundError:
            pass
    return deleted


def sse_frame(frame: Dict[str, Any]) -> bytes:
    """One SSE event for a hub frame. No `id:` line: deltas are not part of
    the ledger cursor, so `Last-Event-ID` never moves because of them."""

    name = "llm.delta_end" if frame.get("kind") == "llm.delta_end" else "llm.delta"
    data = json.dumps(frame, ensure_ascii=False)
    return f"event: {name}\ndata: {data}\n\n".encode("utf-8")


__all__ = [
    "DELTA_KINDS",
    "FileDeltaSink",
    "LiveDeltaError",
    "LiveDeltaHub",
    "LiveSubscription",
    "RootResolver",
    "ROLE_API",
    "ROLE_COMBINED",
    "ROLE_RUNNER",
    "attach_terminal_hook",
    "get_hub",
    "install_live_delta_sink",
    "live_file_path",
    "process_role",
    "resolve_chain_in_store",
    "scope_key",
    "set_process_role",
    "sse_frame",
    "sweep_finished_live_files",
    "synthetic_end_reason",
]
