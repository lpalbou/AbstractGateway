"""The model host child process (ADR 0002): `python -m abstractgateway.model_host_child`.

Started by `model_host.ModelHostSupervisor`, never by hand. Its stdin carries one JSON handshake
line (socket address, the per-boot key, the data dir) and then stays open: EOF means the gateway is
gone, and the child exits. Its stdout carries one readiness line. It serves:

- the RPC listener (`multiprocessing.connection`, authenticated with the key): the gateway's
  `ModelHostClient` proxies build AbstractRuntime LLM clients here and call them;
- Core's serving app on a loopback port with a per-boot secret (`split_mode.ModelHost`, address in
  `<data dir>/split/model_host.json`), where the gateway forwards `/v1`.

Everything that loads weights runs in this process; the gateway's routes and sessions do not.
"""
from __future__ import annotations

import contextlib
import contextvars
import io
import itertools
import json
import logging
import os
import pickle
import queue
import signal
import sys
import threading
import time
import traceback
import types
import weakref
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .model_host import CONFIG_ATTRS, ModelHostRemoteError, _portable_exception

SYNC_CALLBACK_TIMEOUT_S = 60.0
_STRONG_HANDLES_MAX = 256

logger = logging.getLogger("abstractgateway.model_host.child")

_current_channel: "contextvars.ContextVar[Optional[_Channel]]" = contextvars.ContextVar("model_host_channel",
                                                                                        default=None)
# Objects the gateway may hold a handle to: provider instances and nested clients stay here.
_HANDLE_MODULE_PREFIXES = ("abstractcore.providers", "abstractruntime.integrations.abstractcore.llm_client")


class _Host:
    def __init__(self, data_dir: Path, generation: int = 0) -> None:
        self.data_dir = data_dir
        self.generation = int(generation)
        self.clients: Dict[str, Any] = {}
        self._handles: "weakref.WeakValueDictionary[str, Any]" = weakref.WeakValueDictionary()
        self._strong: Dict[str, Any] = {}
        self._handle_ids: Dict[int, str] = {}
        self._ids = itertools.count(1)
        self._stores: Dict[str, Any] = {}
        self.lock = threading.Lock()

    def artifact_store(self, base: str) -> Any:
        with self.lock:
            store = self._stores.get(base)
            if store is None:
                from abstractruntime import FileArtifactStore

                store = self._stores[base] = FileArtifactStore(base)
            return store

    def handle_for(self, obj: Any) -> str:
        with self.lock:
            hid = self._handle_ids.get(id(obj))
            if hid is not None and (self._handles.get(hid) is obj or self._strong.get(hid) is obj):
                return hid
            hid = f"h{next(self._ids)}"
            try:
                self._handles[hid] = obj
                key = id(obj)
                weakref.finalize(obj, self._forget, key, hid)
            except TypeError:  # not weakly referenceable: kept, bounded (oldest dropped)
                self._strong[hid] = obj
                while len(self._strong) > _STRONG_HANDLES_MAX:
                    old_hid = next(iter(self._strong))
                    old = self._strong.pop(old_hid)
                    self._handle_ids.pop(id(old), None)
            self._handle_ids[id(obj)] = hid
            return hid

    def _forget(self, key: int, hid: str) -> None:
        with self.lock:
            if self._handle_ids.get(key) == hid:
                self._handle_ids.pop(key, None)

    def resolve(self, target: Tuple[str, str]) -> Any:
        kind, ident = target
        if kind == "c":
            obj = self.clients.get(ident)
        else:
            obj = self._handles.get(ident) or self._strong.get(ident)
        if obj is None:
            raise LookupError(f"the model host holds no object {ident!r} (it was freed or never built)")
        return obj


class _Channel:
    """One gateway connection: requests run one at a time; callbacks go back on it."""

    def __init__(self, conn: Any, host: _Host) -> None:
        self.conn = conn
        self.host = host
        self.send_lock = threading.Lock()
        self.events: Dict[str, threading.Event] = {}
        self.replies: Dict[int, "queue.Queue[Tuple[bool, Any]]"] = {}
        self.requests: "queue.Queue[Optional[Tuple[Any, ...]]]" = queue.Queue()
        self.closed = threading.Event()
        self._rids = itertools.count(1)

    def send(self, msg: Tuple[Any, ...]) -> None:
        buf = io.BytesIO()
        _ChildPickler(buf, self.host).dump(msg)
        data = buf.getvalue()
        with self.send_lock:
            self.conn.send_bytes(data)

    def call_back(self, cid: str, method: Optional[str], args: Tuple[Any, ...], kwargs: Dict[str, Any], *,
                  wait: bool) -> Any:
        if self.closed.is_set():
            raise RuntimeError("model host: the gateway connection for this callback is closed")
        if not wait:
            with contextlib.suppress(Exception):
                self.send(("cb", cid, method, args, kwargs))
            return None
        rid = next(self._rids)
        box: "queue.Queue[Tuple[bool, Any]]" = queue.Queue(maxsize=1)
        self.replies[rid] = box
        deadline = time.monotonic() + SYNC_CALLBACK_TIMEOUT_S
        try:
            self.send(("cbq", rid, cid, method, args, kwargs))
            while True:
                try:
                    ok, value = box.get(timeout=0.5)
                    break
                except queue.Empty:
                    if self.closed.is_set():
                        raise RuntimeError("model host: the gateway went away during a callback")
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"model host: the gateway did not answer a callback within "
                                           f"{SYNC_CALLBACK_TIMEOUT_S:.0f} s (its request has ended)")
        finally:
            self.replies.pop(rid, None)
        if ok:
            return value
        raise value


class _CallbackStub:
    """A gateway callable. Per-request stubs use their own connection; persistent ones (passed to a
    `set_*` method) use the connection of the request running when they are called."""

    def __init__(self, channel: _Channel, cid: str, persistent: bool, notify: bool) -> None:
        self._channel = channel
        self._cid = cid
        self._persistent = persistent
        self._notify = notify

    def _route(self) -> _Channel:
        if self._persistent:
            current = _current_channel.get()
            if current is None:
                raise RuntimeError("model host: a gateway callback was called outside a gateway request")
            return current
        return self._channel

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._route().call_back(self._cid, None, args, kwargs, wait=not self._notify)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        def _method(*args: Any, **kwargs: Any) -> Any:
            return self._route().call_back(self._cid, name, args, kwargs, wait=not self._notify)

        return _method


def _emitter_stub(chan: _Channel, cid: str) -> Any:
    """A LiveDeltaEmitter here (the client checks `isinstance`) whose calls reach the gateway's own
    emitter; `has_streamed_text` and `ended` are tracked here."""
    from abstractruntime.core.live_deltas import LiveDeltaEmitter

    class _RemoteDeltaEmitter(LiveDeltaEmitter):
        def __init__(self) -> None:  # noqa: D401 - deliberately not the base initializer
            self._streamed = False
            self._ended_here = False
            self.unavailable_detail = None
            self.record_only_detail = None

        def _send(self, method: Optional[str], *args: Any) -> None:
            chan.call_back(cid, method, args, {}, wait=False)

        def __call__(self, text: Any, channel: str = "content") -> None:  # type: ignore[override]
            if text:
                self._streamed = True
            self._send(None, text, channel)

        def mark_unavailable(self, detail: str) -> None:
            self.unavailable_detail = detail
            self._send("mark_unavailable", detail)

        @property
        def has_streamed_text(self) -> bool:
            return self._streamed

        def note_for_record(self, detail: str) -> None:
            self.record_only_detail = detail
            self._send("note_for_record", detail)

        def seal(self) -> None:
            self._send("seal")

        def flush(self) -> None:
            self._send("flush")

        def end(self, reason: str, detail: Optional[str] = None) -> None:
            self._ended_here = True
            self._send("end", reason, detail)

        @property
        def ended(self) -> bool:
            return self._ended_here

    return _RemoteDeltaEmitter()


class _ChildUnpickler(pickle.Unpickler):
    def __init__(self, data: bytes, channel: _Channel) -> None:
        super().__init__(io.BytesIO(data))
        self._channel = channel

    def persistent_load(self, pid):
        kind = pid[0]
        if kind == "event":
            ev = threading.Event()
            if pid[2]:
                ev.set()
            self._channel.events[pid[1]] = ev
            return ev
        if kind == "emitter":
            return _emitter_stub(self._channel, pid[1])
        if kind == "cb":
            return _CallbackStub(self._channel, pid[1], bool(pid[2]), bool(pid[3]))
        if kind == "artifact_store":
            return self._channel.host.artifact_store(pid[1])
        if kind == "client":
            return self._channel.host.resolve(("c", pid[1]))
        if kind == "handle":
            if len(pid) > 2 and int(pid[2]) != self._channel.host.generation:
                raise LookupError("this object lived in an earlier model host process (it restarted since)")
            return self._channel.host.resolve(("h", pid[1]))
        raise pickle.UnpicklingError(f"unknown gateway reference {kind!r}")


class _ChildPickler(pickle.Pickler):
    def __init__(self, buf, host: _Host) -> None:
        super().__init__(buf, protocol=pickle.HIGHEST_PROTOCOL)
        self._host = host

    def persistent_id(self, obj: Any):
        if isinstance(obj, (str, bytes, int, float, bool, type(None), dict, list, tuple, BaseException)):
            return None
        cls = type(obj)
        module = getattr(cls, "__module__", "") or ""
        if module.startswith(_HANDLE_MODULE_PREFIXES) and not isinstance(obj, type):
            name = cls.__name__
            if name.endswith(("Provider", "LLMClient", "Client")) or hasattr(obj, "generate"):
                return ("handle", self._host.handle_for(obj), name)
        return None


def _is_stream(value: Any) -> bool:
    return isinstance(value, (types.GeneratorType, types.AsyncGeneratorType)) or (
        hasattr(value, "__next__") and hasattr(value, "__iter__")
        and not isinstance(value, (str, bytes, bytearray, dict, list, tuple, io.IOBase)))


def _error(exc: BaseException) -> Tuple[Any, ...]:
    portable = _portable_exception(exc)
    if portable is exc:
        with contextlib.suppress(Exception):
            exc.model_host_traceback = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-8000:]
    cause = exc.__cause__ if isinstance(exc.__cause__, BaseException) else None
    return ("err", portable, _portable_exception(cause) if cause is not None else None)


def _config_attrs(obj: Any, names: Any = CONFIG_ATTRS) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name in names:
        if not hasattr(obj, name):
            continue
        value = getattr(obj, name)
        try:
            pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
        except Exception:  # noqa: BLE001 - not a plain value: read through the child instead
            continue
        out[name] = value
    return out


def _build_client(kind: str, ctor: Dict[str, Any]) -> Any:
    from abstractruntime.integrations.abstractcore import llm_client as lc

    if kind == "multi":
        return lc.MultiLocalAbstractCoreLLMClient(**ctor)
    if kind == "local":
        return lc.LocalAbstractCoreLLMClient(**ctor)
    raise ValueError(f"unknown client kind {kind!r}")


def _execute(channel: _Channel, req: Tuple[Any, ...]) -> None:
    host = channel.host
    op = req[0]
    token = _current_channel.set(channel)
    try:
        if op == "ping":
            channel.send(("ok", {"pid": os.getpid()}))
        elif op == "new":
            _, cid, kind, ctor = req
            client = _build_client(kind, ctor)
            with host.lock:
                host.clients[cid] = client
            channel.send(("ok", {"attrs": _config_attrs(client)}))
        elif op == "setattr":
            _, target, name, value = req
            setattr(host.resolve(target), name, value)
            channel.send(("ok", None))
        elif op == "getattrs":
            _, target, names = req
            channel.send(("ok", _config_attrs(host.resolve(target), names)))
        elif op == "drop":
            with host.lock:
                host.clients.pop(req[1], None)
            channel.send(("ok", None))
        elif op == "getattr":
            _, target, name = req
            value = getattr(host.resolve(target), name)
            if callable(value) and not isinstance(value, type):
                channel.send(("callable",))
            else:
                channel.send(("ok", value))
        elif op == "call":
            _, target, method, args, kwargs = req
            result = getattr(host.resolve(target), method)(*args, **kwargs)
            if _is_stream(result):
                channel.send(("iter",))
                for item in result:
                    if channel.closed.is_set():
                        with contextlib.suppress(Exception):
                            result.close()
                        return
                    channel.send(("item", item))
                channel.send(("end",))
            else:
                try:
                    channel.send(("ok", result))
                except (pickle.PicklingError, TypeError, AttributeError) as exc:
                    channel.send(("err", ModelHostRemoteError(
                        f"the result of {method} ({type(result).__name__}) cannot cross to the gateway: {exc}",
                        remote_type=type(exc).__name__), None))
        elif op == "unreadable":
            channel.send(("err", ModelHostRemoteError(f"the model host could not read this request: {req[1]}",
                                                      remote_type=type(req[1]).__name__), None))
        else:
            channel.send(("err", ModelHostRemoteError(f"unknown model host request {op!r}"), None))
    except (OSError, EOFError):
        channel.closed.set()
    except BaseException as exc:  # noqa: BLE001 - every failure is the request's, never the process's
        with contextlib.suppress(OSError, EOFError):
            channel.send(_error(exc))
    finally:
        _current_channel.reset(token)
        channel.events.clear()


def _reader(channel: _Channel) -> None:
    """Reads everything the gateway sends: requests are queued for the worker; Stops and callback
    answers are applied at once (they arrive while a request runs)."""
    try:
        while True:
            data = channel.conn.recv_bytes()
            try:
                msg = _ChildUnpickler(data, channel).load()
            except Exception as exc:  # noqa: BLE001 - that request fails; the connection stays in step
                channel.requests.put(("unreadable", exc))
                continue
            kind = msg[0]
            if kind == "set":
                ev = channel.events.get(msg[1])
                if ev is not None:
                    ev.set()
            elif kind == "cbr":
                box = channel.replies.get(msg[1])
                if box is not None:
                    box.put((bool(msg[2]), msg[3]))
            else:
                channel.requests.put(msg)
    except (OSError, EOFError):
        pass
    except Exception:  # noqa: BLE001
        logger.exception("model host: unreadable message; closing the connection")
    finally:
        channel.closed.set()
        # The gateway left mid-request (it timed out, the client hung up): stop the generation.
        for ev in list(channel.events.values()):
            ev.set()
        channel.requests.put(None)


def _serve_connection(conn: Any, host: _Host) -> None:
    channel = _Channel(conn, host)
    threading.Thread(target=_reader, args=(channel,), name="model-host-reader", daemon=True).start()
    try:
        while True:
            req = channel.requests.get()
            if req is None:
                return
            _execute(channel, req)
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _watch_parent(stdin) -> None:
    """EOF on stdin: the gateway is gone (or stopping us). Exit without waiting for generations."""
    with contextlib.suppress(Exception):
        while stdin.read(1024):
            pass
    os._exit(0)


def main() -> int:
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl-C in a terminal is the gateway's to handle
    # A service manager's stop signals the whole group: the gateway stops this child itself (it
    # closes our stdin), so a SIGTERM here is not a crash to record and restart.
    with contextlib.suppress(AttributeError, ValueError, OSError):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    line = sys.stdin.buffer.readline()
    try:
        hs = json.loads(line.decode("utf-8"))
        authkey = bytes.fromhex(hs["authkey"])
        family, address, data_dir = hs["family"], hs["address"], Path(hs["data_dir"])
        generation, nice = int(hs.get("generation") or 0), int(hs.get("nice") or 0)
    except Exception as exc:  # noqa: BLE001
        sys.stdout.write(json.dumps({"ready": False, "reason": f"bad handshake: {exc}"}) + "\n")
        sys.stdout.flush()
        return 2
    threading.Thread(target=_watch_parent, args=(sys.stdin.buffer,), name="model-host-parent", daemon=True).start()
    os.environ["ABSTRACTGATEWAY_DATA_DIR"] = str(data_dir)
    from multiprocessing.connection import Listener

    if nice and hasattr(os, "nice"):
        with contextlib.suppress(OSError):
            os.nice(nice)
    host = _Host(data_dir, generation)
    try:
        listener = Listener(address, family=family, authkey=authkey, backlog=64)
        if family == "AF_UNIX":
            os.chmod(address, 0o600)
        from .split_mode import ModelHost

        http = ModelHost(data_dir).start()
    except Exception as exc:  # noqa: BLE001 - reported to the supervisor, which records it
        traceback.print_exc()
        sys.stdout.write(json.dumps({"ready": False, "reason": f"{type(exc).__name__}: {exc}"}) + "\n")
        sys.stdout.flush()
        return 3
    logger.info("model host pid %s: RPC on %s, Core's app on %s", os.getpid(), address, http.url)
    sys.stdout.write(json.dumps({"ready": True, "pid": os.getpid(), "http": http.url}) + "\n")
    sys.stdout.flush()
    # From here on, anything printed goes to the log: the gateway reads no more of our stdout, and
    # a full pipe would block whichever thread printed.
    with contextlib.suppress(OSError, ValueError):
        os.dup2(sys.stderr.fileno(), 1)
    while True:
        try:
            conn = listener.accept()
        except Exception as exc:  # noqa: BLE001 - a failed handshake (wrong key) costs that connection only
            logger.warning("model host: refused a connection: %s", exc)
            continue
        threading.Thread(target=_serve_connection, args=(conn, host), name="model-host-conn", daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())
