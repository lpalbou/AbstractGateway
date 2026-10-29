"""The model host: AbstractCore's providers and model pool in a supervised child process (ADR 0002).

In the `process` mode (`serve --model-host process`, the default on Linux) the gateway process loads
no model and calls no provider. One child, `python -m abstractgateway.model_host_child`, holds them:

- the runtime's LLM clients. In the gateway each one is a `ModelHostClient`: a proxy with the same
  API as the AbstractRuntime client it stands for (`MultiLocalAbstractCoreLLMClient` in the bundle
  runtimes, `LocalAbstractCoreLLMClient` for the gateway-side generations). Every method and
  attribute is forwarded; callbacks cross the boundary (live token deltas, progress, the cancel
  event, the endpoint-profile resolver); results and exceptions come back as the same objects.
- Core's serving app (`/v1`), on a loopback port with a per-boot secret (`split_mode.ModelHost`),
  so runs and `/v1` share one model pool. The gateway admits `/v1` requests and forwards them.

The gateway supervises the child (`ModelHostSupervisor`): readiness, pings, restarts with backoff
and a limit per window, a memory ceiling, `nice`, and one incident file per unexpected exit
(`<data dir>/incidents/model-host-<stamp>-<kind>.json`, schema `abstractgateway.incident.v1`).

Transport: `multiprocessing.connection` over a Unix socket in a 0700 folder (a named pipe on
Windows), authenticated with a per-boot 32-byte key handed to the child over stdin; messages are
pickles exchanged between two copies of the same installed code, never reachable from the network.

Wire protocol (each message one pickle; parent -> child requests run one at a time per connection):

    parent -> child   ("ping",) | ("new", client_id, kind, ctor_kwargs) | ("drop", client_id)
                      ("call", target, method, args, kwargs) | ("getattr", target, name)
                      ("set", event_id)                      the parent's Event was set (a Stop)
                      ("cbr", request_id, ok, value)         answer to a callback the child called
    child -> parent   ("ok", value) | ("err", exception, cause) | ("callable",)
                      ("iter",) then ("item", value)* and ("end",) or ("err", ...)
                      ("cb", callback_id, method, args, kwargs)            fire and forget
                      ("cbq", request_id, callback_id, method, args, kwargs)   needs an answer

`target` is ("c", client_id) or ("h", handle_id): a handle names an object that cannot be copied
(a Core provider instance, a nested client) and stays in the child.
"""
from __future__ import annotations

import contextlib
import io
import itertools
import json
import logging
import os
import pickle
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import weakref
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

logger = logging.getLogger("abstractgateway.model_host")

MODE_PROCESS = "process"
MODE_IN_PROCESS = "in-process"
MODES = (MODE_PROCESS, MODE_IN_PROCESS)

INCIDENT_SCHEMA = "abstractgateway.incident.v1"
INCIDENT_PREFIX = "model-host-"
INCIDENT_KEEP = 200
READY_TIMEOUT_S = 60.0
PING_INTERVAL_S = 5.0
PING_TIMEOUT_S = 5.0
PING_MISSES_TO_KILL = 3
MEMORY_SAMPLE_S = 2.0
DEFAULT_MEMORY_FRACTION = 0.8
DEFAULT_NICE = 5
MAX_RESTARTS = 5
RESTART_WINDOW_S = 600.0
BACKOFF_S = (0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
STOP_GRACE_S = 10.0
LOG_MAX_BYTES = 5_000_000
LOG_TAIL_BYTES = 4000
POOL_IDLE_MAX = 16
CONNECT_TIMEOUT_S = 5.0
# Plain callables are called back synchronously (the child waits for the answer); these keys name
# best-effort notifications the child does not wait for.
_NOTIFY_KEYS = frozenset({"on_progress", "progress_callback", "progress_event_callback", "on_token"})


# ---- Mode ----------------------------------------------------------------------------------------

_mode: Optional[str] = None
_mode_source = "default"
_supervisor: Optional["ModelHostSupervisor"] = None
_state_lock = threading.Lock()


def default_mode(*, platform: str = sys.platform) -> str:
    """`process` on Linux (servers), `in-process` elsewhere (the macOS tray, Apple unified memory,
    Windows desktops)."""
    return MODE_PROCESS if str(platform).startswith("linux") else MODE_IN_PROCESS


def set_mode(value: Optional[str], *, source: str) -> str:
    global _mode, _mode_source
    resolved = value if value in MODES else default_mode()
    with _state_lock:
        _mode, _mode_source = resolved, source if value in MODES else f"{source} (platform default)"
    return resolved


def mode() -> str:
    """The mode this process chose; `in-process` when no CLI chose one (tests, an embedded app)."""
    return _mode or MODE_IN_PROCESS


def mode_source() -> str:
    return _mode_source


def supervisor() -> Optional["ModelHostSupervisor"]:
    return _supervisor


def active() -> bool:
    """Whether this process sends its model work to a model host child."""
    return _supervisor is not None


def start(data_dir: Path, *, memory_ceiling_bytes: Optional[int] = None, nice: int = DEFAULT_NICE,
          process_label: str = "gateway") -> "ModelHostSupervisor":
    """Start the model host child for this process (once) and route model work there."""
    global _supervisor
    with _state_lock:
        if _supervisor is not None:
            return _supervisor
        sup = ModelHostSupervisor(Path(data_dir), memory_ceiling_bytes=memory_ceiling_bytes, nice=nice,
                                  process_label=process_label)
        _supervisor = sup
    from abstractruntime.integrations.abstractcore.llm_client import default_model_warmup, set_default_model_warmup

    global _warmup_before
    _warmup_before = default_model_warmup()
    # This process holds no model: a runtime built here never warms one (the child does).
    set_default_model_warmup(False)
    import atexit

    atexit.register(stop)
    sup.start()
    return sup


_warmup_before: Optional[bool] = None


def stop() -> None:
    """Stop the child and give this process back its in-process defaults (mode, warm-up)."""
    global _supervisor, _mode, _mode_source, _warmup_before
    with _state_lock:
        sup, _supervisor = _supervisor, None
        warmup, _warmup_before = _warmup_before, None
        _mode, _mode_source = None, "default"
    if sup is not None:
        sup.stop()
    if warmup is not None:
        from abstractruntime.integrations.abstractcore.llm_client import set_default_model_warmup

        set_default_model_warmup(warmup)


def health_snapshot() -> Dict[str, Any]:
    sup = _supervisor
    if sup is None:
        return {"mode": mode(), "source": _mode_source,
                "state": "in-process" if mode() == MODE_IN_PROCESS else "not_started"}
    return {"mode": MODE_PROCESS, "source": _mode_source, **sup.snapshot()}


def physical_memory_bytes() -> Optional[int]:
    try:
        return int(os.sysconf("SC_PAGE_SIZE")) * int(os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, ValueError, OSError):
        try:
            import psutil  # type: ignore

            return int(psutil.virtual_memory().total)
        except Exception:  # noqa: BLE001 - no reading on this platform: no default ceiling
            return None


def default_memory_ceiling_bytes() -> Optional[int]:
    total = physical_memory_bytes()
    return int(total * DEFAULT_MEMORY_FRACTION) if total else None


def rss_bytes(pid: int) -> Optional[int]:
    """Resident anonymous memory of `pid` (/proc RssAnon on Linux; psutil's RSS elsewhere); None when
    unreadable."""
    try:
        with open(f"/proc/{int(pid)}/status", "r", encoding="ascii", errors="replace") as fh:
            for line in fh:
                # Anonymous memory only (heap, KV cache, tensors): a memory-mapped GGUF's file pages
                # are reclaimable and must not trip the ceiling.
                if line.startswith("RssAnon:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    try:
        import psutil  # type: ignore

        return int(psutil.Process(int(pid)).memory_info().rss)
    except Exception:  # noqa: BLE001 - gone, or no psutil: unknown
        return None


# ---- Errors --------------------------------------------------------------------------------------

class ModelHostUnavailable(RuntimeError):
    """The model host is not answering (starting, restarting, or stopped restarting)."""

    status_code = 503


class ModelHostCrashed(RuntimeError):
    """The model host died while it served this request. Not retried automatically: the request may
    be what crashed it (424: a dependency of the request failed)."""

    status_code = 424


class ModelHostRemoteError(RuntimeError):
    """An exception raised in the model host that could not be copied as itself."""

    def __init__(self, message: str, *, remote_type: str = "", status_code: Optional[int] = None,
                 remote_traceback: str = "") -> None:
        super().__init__(message)
        self.remote_type = remote_type
        self.remote_traceback = remote_traceback
        if status_code is not None:
            self.status_code = status_code

    def __reduce__(self):
        return (_rebuild_remote_error, (str(self), self.remote_type, getattr(self, "status_code", None),
                                        self.remote_traceback))


def _rebuild_remote_error(message, remote_type, status_code, remote_traceback):
    return ModelHostRemoteError(message, remote_type=remote_type, status_code=status_code,
                                remote_traceback=remote_traceback)


_CALLABLE = ("callable",)


# ---- Parent-side pickling ------------------------------------------------------------------------

def _is_delta_emitter(obj: Any) -> bool:
    from abstractruntime.core.live_deltas import LiveDeltaEmitter

    return isinstance(obj, LiveDeltaEmitter)


def _file_artifact_store_base(obj: Any) -> Optional[str]:
    from abstractruntime import FileArtifactStore

    if type(obj) is FileArtifactStore:
        return str(getattr(obj, "_base"))
    return None


_CALLBACK_IDS = itertools.count(1)


class _CallContext:
    """Callbacks and events of ONE request, by id (the parent answers the child's callbacks)."""

    def __init__(self, persistent: Dict[str, Callable[..., Any]]) -> None:
        self.callbacks: Dict[str, Any] = {}
        self.events: Dict[str, threading.Event] = {}
        self.persistent = persistent
    def new_id(self, prefix: str) -> str:
        # Unique across requests: a callback the child makes late (a provider thread finishing after
        # its request ended) must never match the next request's callback on a pooled connection.
        return f"{prefix}{next(_CALLBACK_IDS)}"

    def callback(self, cid: str) -> Any:
        if cid in self.callbacks:
            return self.callbacks[cid]
        return self.persistent.get(cid)


class _ParentPickler(pickle.Pickler):
    def __init__(self, buf, ctx: _CallContext, *, persistent_callbacks: bool) -> None:
        super().__init__(buf, protocol=pickle.HIGHEST_PROTOCOL)
        self._ctx = ctx
        self._persistent = persistent_callbacks
        self._notify_ids: set = set()

    def mark_notify(self, obj: Any) -> None:
        self._notify_ids.add(id(obj))

    def persistent_id(self, obj: Any):  # noqa: D401 - pickle hook
        if isinstance(obj, (str, bytes, int, float, bool, type(None), dict, list, tuple)):
            return None
        if isinstance(obj, threading.Event):
            eid = self._ctx.new_id("e")
            self._ctx.events[eid] = obj
            return ("event", eid, obj.is_set())
        if isinstance(obj, ModelHostClient):
            return ("client", obj._mh_id)
        if isinstance(obj, _RemoteHandle):
            return ("handle", obj._mh_hid, obj._mh_generation)
        base = _file_artifact_store_base(obj)
        if base is not None:
            return ("artifact_store", base)
        if _is_delta_emitter(obj):
            cid = self._ctx.new_id("d")
            self._ctx.callbacks[cid] = obj
            return ("emitter", cid)
        if callable(obj) and not isinstance(obj, type):
            if self._persistent:
                cid = f"p{uuid.uuid4().hex[:12]}"
                self._ctx.persistent[cid] = obj
                return ("cb", cid, True, False)
            cid = self._ctx.new_id("c")
            self._ctx.callbacks[cid] = obj
            return ("cb", cid, False, id(obj) in self._notify_ids)
        return None


def _dumps_request(msg: Tuple[Any, ...], ctx: _CallContext, *, persistent_callbacks: bool = False) -> bytes:
    buf = io.BytesIO()
    pickler = _ParentPickler(buf, ctx, persistent_callbacks=persistent_callbacks)
    if msg and msg[0] == "call":
        kwargs = msg[4] if len(msg) > 4 and isinstance(msg[4], dict) else {}
        for scope in (kwargs, kwargs.get("params") if isinstance(kwargs.get("params"), dict) else {}):
            for key in _NOTIFY_KEYS:
                if callable(scope.get(key)):
                    pickler.mark_notify(scope[key])
    pickler.dump(msg)
    return buf.getvalue()


class _ParentUnpickler(pickle.Unpickler):
    def __init__(self, data: bytes, host: "ModelHostSupervisor", generation: int) -> None:
        super().__init__(io.BytesIO(data))
        self._host = host
        self._generation = generation

    def persistent_load(self, pid):
        if pid[0] == "handle":
            return _RemoteHandle(self._host, pid[1], self._generation, pid[2])
        raise pickle.UnpicklingError(f"unknown model host reference {pid[0]!r}")


def _plain_dumps(msg: Any) -> bytes:
    return pickle.dumps(msg, protocol=pickle.HIGHEST_PROTOCOL)


# ---- Parent side: one request on one connection ---------------------------------------------------

class _Connection:
    """One authenticated connection to the child (generation-tagged)."""

    def __init__(self, conn: Any, generation: int) -> None:
        self.conn = conn
        self.generation = generation
        self.broken = False

    def close(self) -> None:
        self.broken = True
        with contextlib.suppress(Exception):
            self.conn.close()


def _describe_target(owner: Any, method: str) -> Dict[str, Any]:
    row: Dict[str, Any] = {"method": method}
    ctor = getattr(owner, "_mh_ctor", None)
    if isinstance(ctor, dict):
        row["provider"] = ctor.get("provider")
        row["model"] = ctor.get("model")
    return row


def _invoke_callback(target: Any, method: Optional[str], args: Any, kwargs: Any) -> Any:
    fn = target if method is None else getattr(target, method)
    return fn(*args, **kwargs)


class _Exchange:
    """Runs one request: sends it, answers callbacks, relays Stops, and returns or raises the
    child's answer. A stream answer (`("iter",)`) keeps the connection until it is consumed."""

    def __init__(self, host: "ModelHostSupervisor", owner: Any, msg: Tuple[Any, ...], *,
                 persistent: Optional[Dict[str, Any]] = None, persistent_callbacks: bool = False,
                 what: Optional[Dict[str, Any]] = None) -> None:
        self.host = host
        self.owner = owner
        self.ctx = _CallContext(persistent if persistent is not None else {})
        self.payload = _dumps_request(msg, self.ctx, persistent_callbacks=persistent_callbacks)
        self.what = what or {"method": str(msg[0])}
        self.conn: Optional[_Connection] = None
        self._sent_events: set = set()

    def run(self, *, timeout_s: Optional[float] = None) -> Any:
        self.conn = self.host._acquire(connect_timeout_s=timeout_s)
        token = self.host._inflight_add(self.what)
        streaming = False
        try:
            try:
                self.conn.conn.send_bytes(self.payload)
            except (OSError, EOFError, ValueError) as exc:
                raise self.host._crashed_error(self.conn.generation, during="sending the request") from exc
            msg = self._next(timeout_s=timeout_s)
            if msg[0] == "iter":
                streaming = True
                return _Stream(self, token)
            return self._finish(msg)
        finally:
            if not streaming:
                self.host._inflight_remove(token)
                self._release()

    def _release(self) -> None:
        conn, self.conn = self.conn, None
        if conn is not None:
            self.host._release(conn)

    def _finish(self, msg: Tuple[Any, ...]) -> Any:
        kind = msg[0]
        if kind == "ok":
            return msg[1]
        if kind == "callable":
            return _CALLABLE
        if kind == "err":
            exc, cause = msg[1], msg[2]
            if isinstance(cause, BaseException):
                exc.__cause__ = cause
            raise exc
        raise ModelHostRemoteError(f"the model host answered an unknown message {kind!r}")


    def _watch_events(self) -> None:
        for eid, ev in self.ctx.events.items():
            if eid not in self._sent_events and ev.is_set():
                self._sent_events.add(eid)
                with contextlib.suppress(Exception):
                    self.conn.conn.send_bytes(_plain_dumps(("set", eid)))

    def _next(self, *, timeout_s: Optional[float]) -> Tuple[Any, ...]:
        conn = self.conn
        assert conn is not None
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        while True:
            if self.ctx.events:
                self._watch_events()
                wait = 0.05
            else:
                wait = None
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    conn.close()
                    raise ModelHostUnavailable("The model host did not answer in time.")
                wait = left if wait is None else min(wait, left)
            try:
                if not conn.conn.poll(wait):
                    continue
                data = conn.conn.recv_bytes()
            except (OSError, EOFError) as exc:
                conn.close()
                raise self.host._crashed_error(conn.generation, during="answering") from exc
            msg = _ParentUnpickler(data, self.host, conn.generation).load()
            kind = msg[0]
            if kind == "cb":
                _, cid, method, args, kwargs = msg
                target = self.ctx.callback(cid)
                if target is not None:
                    try:
                        _invoke_callback(target, method, args, kwargs)
                    except Exception:  # noqa: BLE001 - a notification is best-effort, like in-process
                        logger.debug("model host: a callback raised", exc_info=True)
                continue
            if kind == "cbq":
                _, rid, cid, method, args, kwargs = msg
                target = self.ctx.callback(cid)
                try:
                    if target is None:
                        raise RuntimeError("the callback is no longer registered in the gateway")
                    reply = ("cbr", rid, True, _invoke_callback(target, method, args, kwargs))
                except Exception as exc:  # noqa: BLE001 - the child raises it where Core called
                    reply = ("cbr", rid, False, _portable_exception(exc))
                try:
                    conn.conn.send_bytes(_dumps_request(reply, self.ctx))
                except (OSError, EOFError) as exc:
                    conn.close()
                    raise self.host._crashed_error(conn.generation, during="answering") from exc
                continue
            return msg


class _Stream:
    """A stream answer (a generator in the child): items arrive as the consumer pulls them. Closed,
    abandoned or garbage-collected before its end, it drops its connection (the child stops on the
    broken pipe) and its in-flight entry."""

    def __init__(self, exchange: "_Exchange", token: Any) -> None:
        self._exchange = exchange
        self._token = token
        self._done = False

    def __iter__(self) -> "_Stream":
        return self

    def __next__(self) -> Any:
        if self._done:
            raise StopIteration
        try:
            msg = self._exchange._next(timeout_s=None)
        except BaseException:
            self._end(clean=False)
            raise
        if msg[0] == "item":
            return msg[1]
        if msg[0] == "end":
            self._end(clean=True)
            raise StopIteration
        self._end(clean=True)
        self._exchange._finish(msg)
        raise StopIteration

    def _end(self, *, clean: bool) -> None:
        if self._done:
            return
        self._done = True
        ex = self._exchange
        if not clean and ex.conn is not None:
            ex.conn.close()
        ex.host._inflight_remove(self._token)
        ex._release()

    def close(self) -> None:
        self._end(clean=False)

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self._end(clean=False)


def _portable_exception(exc: BaseException) -> BaseException:
    """`exc` if it survives a pickle round trip, else a ModelHostRemoteError naming it."""
    try:
        pickle.loads(pickle.dumps(exc, protocol=pickle.HIGHEST_PROTOCOL))
        return exc
    except Exception:  # noqa: BLE001
        status = getattr(exc, "status_code", None)
        return ModelHostRemoteError(f"{type(exc).__name__}: {exc}", remote_type=type(exc).__name__,
                                    status_code=status if isinstance(status, int) else None,
                                    remote_traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))


# ---- Proxies ---------------------------------------------------------------------------------------

class _RemoteMethod:
    __slots__ = ("_owner", "_name")

    def __init__(self, owner: Any, name: str) -> None:
        self._owner = owner
        self._name = name

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._owner._mh_call(self._name, args, kwargs)

    def __repr__(self) -> str:
        return f"<model host method {self._name}>"


class _RemoteHandle:
    """An object that stays in the model host (a Core provider instance); attribute reads and
    method calls are forwarded."""

    def __init__(self, host: "ModelHostSupervisor", hid: str, generation: int, type_name: str) -> None:
        self._mh_host = host
        self._mh_hid = hid
        self._mh_generation = generation
        self._mh_type = type_name

    def _mh_target(self) -> Tuple[str, str]:
        if self._mh_generation != self._mh_host.generation:
            raise ModelHostUnavailable(
                f"This {self._mh_type} lived in a model host that has restarted since; ask for it again.")
        return ("h", self._mh_hid)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") or name.startswith("_mh_"):
            raise AttributeError(name)
        value = _Exchange(self._mh_host, self, ("getattr", self._mh_target(), name),
                          what={"method": f"{self._mh_type}.{name}"}).run()
        if value is _CALLABLE or value == _CALLABLE:
            return _RemoteMethod(self, name)
        return value

    def _mh_call(self, name: str, args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> Any:
        return _Exchange(self._mh_host, self, ("call", self._mh_target(), name, args, kwargs),
                         what={"method": f"{self._mh_type}.{name}"}).run()

    def __repr__(self) -> str:
        return f"<{self._mh_type} in the model host>"


# Configuration attributes the gateway reads off the client (`getattr(client, "_llm_kwargs")`, the
# effect handlers' `_provider`/`_model`): kept here, refreshed at every build and after every
# successful `set_*`, so reading them never waits for the child (or blocks the event loop).
CONFIG_ATTRS = ("_llm_kwargs", "_default_provider", "_default_model", "_provider", "_model",
                "_core_config_file", "_bloc_root_dir", "_prompt_cache_export_root_dir",
                "_capability_defaults", "_capability_defaults_explicit")


def _class_callables(kind: str) -> frozenset:
    from abstractruntime.integrations.abstractcore import llm_client as lc

    cls = lc.MultiLocalAbstractCoreLLMClient if kind == "multi" else lc.LocalAbstractCoreLLMClient
    names = set()
    for name in dir(cls):
        if name.startswith("__"):
            continue
        attr = getattr(cls, name, None)
        if callable(attr) and not isinstance(attr, type):
            names.add(name)
    return frozenset(names)


class ModelHostClient:
    """An AbstractRuntime LLM client that lives in the model host (ADR 0002).

    `kind` is `multi` (`MultiLocalAbstractCoreLLMClient`) or `local` (`LocalAbstractCoreLLMClient`);
    `ctor` are the constructor's keyword arguments. The child builds the client when this proxy is
    created (warming the default model there, in the loading phase) and again after every restart.

    `set_*` calls are configuration: one that succeeds in the child is recorded (the last call per
    name) and replayed, in order, at every rebuild; one the child refuses raises and is not
    recorded. Made before the first build, it is recorded and checked by that build: a replay the
    child refuses is logged and dropped, never retried forever. Method names come from the runtime
    class, the configuration attributes (CONFIG_ATTRS) from a local copy: neither needs the child.
    """

    def __init__(self, host: "ModelHostSupervisor", *, kind: str, ctor: Dict[str, Any]) -> None:
        if kind not in ("multi", "local"):
            raise ValueError(f"unknown model host client kind {kind!r}")
        self._mh_host = host
        self._mh_kind = kind
        self._mh_ctor = dict(ctor)
        self._mh_id = uuid.uuid4().hex
        self._mh_built_generation: Optional[int] = None
        self._mh_lock = threading.Lock()  # one build at a time
        self._mh_setters_lock = threading.Lock()
        self._mh_setters_version = 0
        self._mh_setters: Dict[str, Tuple[Tuple[Any, ...], Dict[str, Any]]] = {}
        self._mh_assigned: Dict[str, Any] = {}
        self._mh_persistent: Dict[str, Any] = {}
        self._mh_callable_names = _class_callables(kind)
        self._mh_config: Dict[str, Any] = {}
        if isinstance(ctor.get("llm_kwargs"), dict):
            self._mh_config["_llm_kwargs"] = dict(ctor["llm_kwargs"])
        if kind == "multi":
            self._mh_config["_default_provider"] = str(ctor.get("provider") or "").strip().lower()
            self._mh_config["_default_model"] = str(ctor.get("model") or "").strip()
        # Read in the gateway by the tool executor's late-bound resolver getter
        # (`build_effect_handlers`), so it is kept here as well as forwarded.
        self._provider_endpoint_profile_resolver: Any = None
        host._register(self)
        # The child's copy goes when this proxy does (gateway-side clients are made per request).
        weakref.finalize(self, host._drop_later, self._mh_id)

    def _mh_built(self) -> bool:
        return self._mh_built_generation == self._mh_host.generation and self._mh_host.generation_alive()

    def _mh_refresh_config(self, attrs: Any) -> None:
        if isinstance(attrs, dict):
            self._mh_config.update(attrs)

    # -- building in the child
    def _mh_ensure(self) -> None:
        if self._mh_built():
            return
        host = self._mh_host
        with self._mh_lock:
            if self._mh_built():
                return
            generation = host.generation
            info = _Exchange(host, self, ("new", self._mh_id, self._mh_kind, self._mh_ctor),
                             what={"method": "build", **_describe_target(self, "build")}).run()
            self._mh_refresh_config((info or {}).get("attrs") if isinstance(info, dict) else None)
            replayed = -1
            for _ in range(3):  # setters recorded while replaying are replayed too
                with self._mh_setters_lock:
                    version = self._mh_setters_version
                    items = list(self._mh_setters.items())
                if version == replayed:
                    break
                for name, (args, kwargs) in items:
                    try:
                        _Exchange(host, self, ("call", ("c", self._mh_id), name, args, kwargs),
                                  persistent=self._mh_persistent, persistent_callbacks=True,
                                  what=_describe_target(self, name)).run()
                    except (ModelHostUnavailable, ModelHostCrashed):
                        raise
                    except Exception as exc:  # noqa: BLE001 - a refused configuration is dropped, loudly
                        logger.warning("model host: %s refused by the child (%s); dropped from the replay", name, exc)
                        with self._mh_setters_lock:
                            if self._mh_setters.get(name) == (args, kwargs):
                                self._mh_setters.pop(name, None)
                replayed = version
            # Plain attribute assignments made on the proxy (`client.residency_owner_label = ...`).
            for name, value in list(self._mh_assigned.items()):
                _Exchange(host, self, ("setattr", ("c", self._mh_id), name, value),
                          what={"method": f"set {name}"}).run()
            attrs = _Exchange(host, self, ("getattrs", ("c", self._mh_id), CONFIG_ATTRS),
                              what={"method": "getattrs"}).run()
            self._mh_refresh_config(attrs)
            self._mh_built_generation = generation

    def _mh_call(self, name: str, args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> Any:
        if name.startswith("set_"):
            if not self._mh_built():
                # Not built yet (the warm thread is building it, or the host restarts): recorded,
                # and the build applies and checks it. Never waits here (this may be the loop).
                with self._mh_setters_lock:
                    self._mh_setters.pop(name, None)
                    self._mh_setters[name] = (tuple(args), dict(kwargs))
                    self._mh_setters_version += 1
                return None
            result = _Exchange(self._mh_host, self, ("call", ("c", self._mh_id), name, args, kwargs),
                               persistent=self._mh_persistent, persistent_callbacks=True,
                               what=_describe_target(self, name)).run()
            with self._mh_setters_lock:
                self._mh_setters.pop(name, None)
                self._mh_setters[name] = (tuple(args), dict(kwargs))
                self._mh_setters_version += 1
            with contextlib.suppress(ModelHostUnavailable, ModelHostCrashed):
                self._mh_refresh_config(_Exchange(self._mh_host, self, ("getattrs", ("c", self._mh_id), CONFIG_ATTRS),
                                                  what={"method": "getattrs"}).run())
            return result
        self._mh_ensure()
        return _Exchange(self._mh_host, self, ("call", ("c", self._mh_id), name, args, kwargs),
                         persistent=self._mh_persistent, what=_describe_target(self, name)).run()

    def __setattr__(self, name: str, value: Any) -> None:
        """An attribute the host assigns on its client (bundle_host's `residency_owner_label`) is
        the child's client's: forwarded now when it is built, and replayed at every rebuild."""
        if name.startswith("_mh_") or name == "_provider_endpoint_profile_resolver":
            object.__setattr__(self, name, value)
            return
        self._mh_assigned[name] = value
        if self._mh_built():
            try:
                _Exchange(self._mh_host, self, ("setattr", ("c", self._mh_id), name, value),
                          what={"method": f"set {name}"}).run()
            except (ModelHostUnavailable, ModelHostCrashed) as exc:
                # The child is restarting: the value is recorded and the rebuild delivers it. An
                # assignment never fails the caller (a service being built) for a transient state.
                logger.debug("model host: %s recorded, delivered at the rebuild (%s)", name, exc)

    def set_provider_endpoint_profile_resolver(self, resolver: Any) -> Any:
        self._provider_endpoint_profile_resolver = resolver
        return self._mh_call("set_provider_endpoint_profile_resolver", (resolver,), {})

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") or name.startswith("_mh_"):
            raise AttributeError(name)
        if name in self._mh_callable_names:
            return _RemoteMethod(self, name)
        assigned = self.__dict__.get("_mh_assigned") or {}
        if name in assigned:
            return assigned[name]
        if name in CONFIG_ATTRS:
            config = self._mh_config
            if name in config:
                value = config[name]
                return dict(value) if isinstance(value, dict) else value
            if self._mh_built():
                raise AttributeError(name)  # the child's client has no such attribute
        self._mh_ensure()
        value = _Exchange(self._mh_host, self, ("getattr", ("c", self._mh_id), name),
                          what=_describe_target(self, name)).run()
        if value is _CALLABLE or value == _CALLABLE:
            return _RemoteMethod(self, name)
        return value

    def __repr__(self) -> str:
        return (f"<ModelHostClient {self._mh_kind} {self._mh_ctor.get('provider')}/"
                f"{self._mh_ctor.get('model')} in the model host>")


# ---- Supervisor ------------------------------------------------------------------------------------

def _signal_name(code: Optional[int]) -> Optional[str]:
    if code is None or code >= 0:
        return None
    try:
        return signal.Signals(-code).name
    except ValueError:
        return f"signal {-code}"


def describe_exit(code: Optional[int]) -> str:
    if code is None:
        return "it stopped answering"
    name = _signal_name(code)
    if name:
        return f"killed by signal {-code} ({name})"
    return f"exit code {code}"


def _tail(path: Path, size: int = LOG_TAIL_BYTES) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            fh.seek(max(0, end - size))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def incidents_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "incidents"


def write_incident(data_dir: Path, doc: Dict[str, Any]) -> Optional[Path]:
    """One file per incident (atomic, 0600); keeps the newest INCIDENT_KEEP model-host files."""
    directory = incidents_dir(data_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(directory, 0o700)
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(doc.get("ended_at") or time.time()))
        path = directory / f"{INCIDENT_PREFIX}{stamp}-{secrets.token_hex(3)}-{doc.get('kind', 'exit')}.json"
        tmp = path.with_name(f".{path.name}.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, default=str)
        os.replace(tmp, path)
        files = sorted(directory.glob(f"{INCIDENT_PREFIX}*.json"))
        for old in files[:-INCIDENT_KEEP]:
            with contextlib.suppress(OSError):
                old.unlink()
        return path
    except OSError:
        logger.exception("model host: the incident record could not be written")
        return None


def read_incidents(data_dir: Path, *, limit: int = 20) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for path in sorted(incidents_dir(data_dir).glob(f"{INCIDENT_PREFIX}*.json"), reverse=True)[:limit]:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict):
            doc["file"] = path.name
            out.append(doc)
    return out


_UNIX_PATH_MAX = 100  # sun_path is 104 bytes on macOS, 108 on Linux; leave room for the file name


def _socket_dir() -> Path:
    """A fresh 0700 folder for the RPC socket: under the temp dir, or /tmp when the temp dir's path
    is too long for a Unix socket address (a macOS per-user TMPDIR, a test's scratch TMPDIR)."""
    for base in (tempfile.gettempdir(), "/tmp"):
        if len(os.path.join(base, "agw-mh-xxxxxxxx", "rpc-0000.sock")) <= _UNIX_PATH_MAX or base == "/tmp":
            path = Path(tempfile.mkdtemp(prefix="agw-mh-", dir=base if os.path.isdir(base) else None))
            os.chmod(path, 0o700)
            return path
    raise RuntimeError("unreachable")


class ModelHostSupervisor:
    """Starts, watches and restarts the model host child; hands out connections to it."""

    def __init__(self, data_dir: Path, *, memory_ceiling_bytes: Optional[int] = None, nice: int = DEFAULT_NICE,
                 process_label: str = "gateway", max_restarts: int = MAX_RESTARTS,
                 restart_window_s: float = RESTART_WINDOW_S, backoff_s: Tuple[float, ...] = BACKOFF_S,
                 ping_interval_s: float = PING_INTERVAL_S, ping_timeout_s: float = PING_TIMEOUT_S,
                 ping_misses_to_kill: int = PING_MISSES_TO_KILL, memory_sample_s: float = MEMORY_SAMPLE_S,
                 ready_timeout_s: float = READY_TIMEOUT_S, child_argv: Optional[List[str]] = None) -> None:
        self.data_dir = Path(data_dir)
        self.memory_ceiling_bytes = memory_ceiling_bytes if memory_ceiling_bytes else None
        self.nice = int(nice)
        self.process_label = process_label
        self.max_restarts = int(max_restarts)
        self.restart_window_s = float(restart_window_s)
        self.backoff_s = tuple(backoff_s) or BACKOFF_S
        self.ping_interval_s = float(ping_interval_s)
        self.ping_timeout_s = float(ping_timeout_s)
        self.ping_misses_to_kill = int(ping_misses_to_kill)
        self.memory_sample_s = float(memory_sample_s)
        self.ready_timeout_s = float(ready_timeout_s)
        self._child_argv = list(child_argv) if child_argv else [sys.executable, "-m", "abstractgateway.model_host_child"]
        self._authkey = secrets.token_bytes(32)
        self._sock_dir = _socket_dir()
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._proc: Optional[subprocess.Popen] = None
        self._address: Optional[str] = None
        self._family: Optional[str] = None
        self.generation = 0
        self._alive_generation = -1
        self.state = "stopped"
        self.started_at: Optional[float] = None
        self.last_exit: Optional[Dict[str, Any]] = None
        self.last_incident: Optional[Dict[str, Any]] = None
        self.last_error: Optional[str] = None
        self.last_rss: Optional[int] = None
        self._restarts: Deque[float] = deque()
        self._stopping = False
        self._kill_reason: Optional[str] = None
        self._recorded_generation = 0
        self._idle: List[_Connection] = []
        self._inflight: Dict[int, Dict[str, Any]] = {}
        self._inflight_ids = itertools.count(1)
        self._clients: "weakref.WeakSet[ModelHostClient]" = weakref.WeakSet()
        self._warm_queue: Deque[Any] = deque()
        self._threads: List[threading.Thread] = []
        self._wake = threading.Event()
        self.log_path = self.data_dir / "logs" / "model-host.log"

    # -- lifecycle
    def start(self) -> "ModelHostSupervisor":
        with self._lock:
            self._stopping = False
        self._spawn()
        for target, name in ((self._sample_loop, "model-host-sampler"), (self._warm_loop, "model-host-warm")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            proc = self._proc
            self.state = "stopped"
            idle, self._idle = self._idle, []
            self._cond.notify_all()
        self._wake.set()
        for conn in idle:
            conn.close()
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(Exception):
                if proc.stdin:
                    proc.stdin.close()  # the child exits when its stdin closes
            try:
                proc.wait(timeout=STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(Exception):
                    proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        with contextlib.suppress(OSError):
            for p in self._sock_dir.iterdir():
                p.unlink()
            self._sock_dir.rmdir()

    def _open_log(self):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            if self.log_path.stat().st_size > LOG_MAX_BYTES:
                os.replace(self.log_path, self.log_path.with_suffix(".log.1"))
        return open(self.log_path, "ab")

    def _spawn(self) -> None:
        with self._lock:
            if self._stopping:
                return
            self.generation += 1
            generation = self.generation
            self.state = "starting"
            idle, self._idle = self._idle, []
        for conn in idle:
            conn.close()
        if sys.platform == "win32":
            family, address = "AF_PIPE", rf"\\.\pipe\agw-mh-{secrets.token_hex(8)}"
        else:
            family, address = "AF_UNIX", str(self._sock_dir / f"rpc-{generation}.sock")
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        env["ABSTRACTGATEWAY_RUNNER"] = "0"
        log = self._open_log()
        log.write(f"\n--- model host generation {generation} starting {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n".encode())
        log.flush()
        try:
            proc = subprocess.Popen(self._child_argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                    env=env, cwd=str(self.data_dir), close_fds=True)
        except OSError as exc:
            log.close()
            self._on_spawn_failed(generation, f"could not start: {exc}")
            return
        finally:
            with contextlib.suppress(Exception):
                log.close()
        handshake = {"authkey": self._authkey.hex(), "family": family, "address": address,
                     "data_dir": str(self.data_dir), "parent_pid": os.getpid(), "generation": generation,
                     "process_label": self.process_label, "nice": self.nice}
        try:
            assert proc.stdin is not None
            proc.stdin.write((json.dumps(handshake) + "\n").encode())
            proc.stdin.flush()
        except (OSError, ValueError):
            pass  # it died at once: the monitor records the exit
        with self._lock:
            self._proc = proc
            self._family, self._address = family, address
            self.started_at = time.time()
        threading.Thread(target=self._monitor, args=(proc, generation), name=f"model-host-monitor-{generation}",
                         daemon=True).start()
        ready = self._read_ready(proc)
        if ready.get("ready"):
            # The child points its stdout at its log after the readiness line; nothing more to read.
            with contextlib.suppress(Exception):
                if proc.stdout:
                    proc.stdout.close()
        with self._lock:
            if generation != self.generation or self._stopping:
                return
            if ready.get("ready"):
                self.state = "up"
                self._alive_generation = generation
                self.last_error = None
                self._cond.notify_all()
                logger.info("model host: pid %s ready (generation %s)", proc.pid, generation)
                for client in list(self._clients):
                    self._warm_queue.append(weakref.ref(client))
                self._wake.set()
            else:
                self.last_error = str(ready.get("reason") or "the model host did not report ready")
                logger.error("model host: %s", self.last_error)
                with contextlib.suppress(Exception):
                    proc.kill()

    def _read_ready(self, proc: subprocess.Popen) -> Dict[str, Any]:
        box: Dict[str, Any] = {}

        def _read() -> None:
            try:
                assert proc.stdout is not None
                line = proc.stdout.readline()
                box.update(json.loads(line.decode("utf-8")) if line.strip() else
                           {"ready": False, "reason": "the model host exited before it was ready"})
            except Exception as exc:  # noqa: BLE001
                box.update({"ready": False, "reason": f"unreadable readiness line: {exc}"})

        t = threading.Thread(target=_read, name="model-host-ready", daemon=True)
        t.start()
        t.join(self.ready_timeout_s)
        if t.is_alive():
            return {"ready": False, "reason": f"the model host was not ready within {self.ready_timeout_s:.0f} s"}
        return box

    def _on_spawn_failed(self, generation: int, reason: str) -> None:
        with self._lock:
            self.last_error = reason
        self._after_exit(generation, None, reason_kind="exit", detail=reason, pid=None)

    def _monitor(self, proc: subprocess.Popen, generation: int) -> None:
        code = proc.wait()
        with contextlib.suppress(Exception):
            if proc.stdout:
                proc.stdout.close()
        self._after_exit(generation, code, reason_kind=None, detail=None, pid=proc.pid)

    def _after_exit(self, generation: int, code: Optional[int], *, reason_kind: Optional[str],
                    detail: Optional[str], pid: Optional[int]) -> None:
        now = time.time()
        with self._lock:
            if generation != self.generation:
                return
            self._alive_generation = -1
            idle, self._idle = self._idle, []
            stopping = self._stopping
            kind = reason_kind or self._kill_reason or "exit"
            self._kill_reason = None
            inflight = [dict(v) for v in self._inflight.values()]
            started = self.started_at
            self.last_exit = {"pid": pid, "code": code, "signal": _signal_name(code), "at": now,
                              "description": describe_exit(code) if detail is None else detail, "kind": kind}
            self._cond.notify_all()
        for conn in idle:
            conn.close()
        if stopping:
            with self._lock:
                self._recorded_generation = generation
                self._cond.notify_all()
            return
        while self._restarts and now - self._restarts[0] > self.restart_window_s:
            self._restarts.popleft()
        give_up = len(self._restarts) >= self.max_restarts
        backoff = self.backoff_s[min(len(self._restarts), len(self.backoff_s) - 1)]
        doc = {
            "schema": INCIDENT_SCHEMA, "kind": kind, "process": "model_host", "parent": self.process_label,
            "pid": pid, "generation": generation, "started_at": started, "ended_at": now,
            "exit_code": code, "signal": _signal_name(code), "description": self.last_exit["description"],
            "rss_bytes_last": self.last_rss, "memory_ceiling_bytes": self.memory_ceiling_bytes,
            "inflight": inflight,
            "restart": {"will_restart": not give_up, "backoff_s": None if give_up else backoff,
                        "restarts_in_window": len(self._restarts), "max_restarts": self.max_restarts,
                        "window_s": self.restart_window_s},
            "log_tail": _tail(self.log_path),
        }
        path = write_incident(self.data_dir, doc)
        with self._lock:
            self.last_incident = {"kind": kind, "at": now, "file": path.name if path else None,
                                  "description": doc["description"], "generation": generation}
            self._recorded_generation = generation
            self._cond.notify_all()
        logger.error("model host: pid %s stopped (%s) with %d request(s) in flight; %s", pid, doc["description"],
                     len(inflight), "giving up after %d restarts in %.0f s" % (len(self._restarts), self.restart_window_s)
                     if give_up else f"restarting in {backoff:g} s")
        if give_up:
            with self._lock:
                self.state = "failed"
            gave = dict(doc, kind="gave_up", log_tail="", inflight=[])
            path = write_incident(self.data_dir, gave)
            with self._lock:
                self.last_incident = {"kind": "gave_up", "at": now, "file": path.name if path else None,
                                      "description": f"stopped restarting after {len(self._restarts)} restarts in "
                                                     f"{self.restart_window_s:.0f} s"}
            return
        with self._lock:
            self.state = "restarting"
        self._restarts.append(now)

        def _later() -> None:
            if not self._wake_wait(backoff):
                self._spawn()

        threading.Thread(target=_later, name="model-host-restart", daemon=True).start()

    def _wake_wait(self, seconds: float) -> bool:
        """Sleep `seconds` unless stopping; True when stopping."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self._stopping:
                return True
            time.sleep(min(0.05, max(0.0, end - time.monotonic())))
        return self._stopping

    def kill(self, reason: str) -> None:
        """Kill the child now (memory ceiling, unresponsive, an operator restart); the monitor
        records the incident under `reason` and restarts it."""
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                return
            self._kill_reason = reason
        with contextlib.suppress(Exception):
            proc.kill()

    def generation_alive(self) -> bool:
        return self._alive_generation == self.generation

    @property
    def pid(self) -> Optional[int]:
        proc = self._proc
        return proc.pid if proc is not None and proc.poll() is None else None

    # -- watching
    def _sample_loop(self) -> None:
        misses = 0
        next_ping = time.monotonic() + self.ping_interval_s
        while not self._stopping:
            self._wake.wait(self.memory_sample_s)
            self._wake.clear()
            if self._stopping:
                return
            pid = self.pid
            if pid is None or not self.generation_alive():
                misses = 0
                continue
            rss = rss_bytes(pid)
            if rss is not None:
                self.last_rss = rss
                if self.memory_ceiling_bytes and rss > self.memory_ceiling_bytes:
                    logger.error("model host: resident memory %.1f GB is over the ceiling %.1f GB: killing it",
                                 rss / 1e9, self.memory_ceiling_bytes / 1e9)
                    self.kill("memory_ceiling")
                    continue
            if time.monotonic() >= next_ping:
                next_ping = time.monotonic() + self.ping_interval_s
                try:
                    _Exchange(self, None, ("ping",), what={"method": "ping"}).run(timeout_s=self.ping_timeout_s)
                    misses = 0
                except ModelHostUnavailable:
                    misses += 1
                except Exception:  # noqa: BLE001 - a crash is the monitor's to record
                    misses = 0
                if misses >= self.ping_misses_to_kill:
                    logger.error("model host: %d pings unanswered: killing it", misses)
                    misses = 0
                    self.kill("unresponsive")

    def _warm_loop(self) -> None:
        while not self._stopping:
            self._wake.wait(0.5)
            while self._warm_queue and not self._stopping:
                item = self._warm_queue.popleft()
                if isinstance(item, str):  # a client id to drop in the child
                    if self.generation_alive():
                        with contextlib.suppress(Exception):
                            _Exchange(self, None, ("drop", item), what={"method": "drop"}).run(timeout_s=10.0)
                    continue
                client = item() if isinstance(item, weakref.ref) else item
                if client is None:
                    continue
                try:
                    client._mh_ensure()
                except Exception as exc:  # noqa: BLE001 - its first request reports the cause again
                    logger.warning("model host: building %r failed: %s", client, exc)

    def _drop_later(self, client_id: str) -> None:
        if not self._stopping:
            self._warm_queue.append(client_id)
            self._wake.set()

    def _register(self, client: ModelHostClient) -> None:
        self._clients.add(client)
        self._warm_queue.append(weakref.ref(client))
        self._wake.set()

    # -- connections
    def _acquire(self, *, connect_timeout_s: Optional[float] = None) -> _Connection:
        with self._lock:
            if self._stopping:
                raise ModelHostUnavailable("The model host is stopped (the gateway is shutting down).")
            if self.state == "failed":
                raise ModelHostUnavailable(
                    "The model host (the process that runs the models) stopped restarting after "
                    f"{len(self._restarts)} crashes in {self.restart_window_s / 60:.0f} minutes; restart the gateway. "
                    "The incidents are in <data dir>/incidents.")
            if self.state == "starting" and self.generation == 1:
                # First boot: wait for the child rather than failing the gateway's first requests.
                self._cond.wait_for(lambda: self.state != "starting" or self._stopping, timeout=self.ready_timeout_s)
            if not self.generation_alive():
                raise ModelHostUnavailable(
                    "The model host (the process that runs the models) is restarting; send it again in a few seconds.")
            generation = self.generation
            while self._idle:
                conn = self._idle.pop()
                if conn.generation == generation and not conn.broken:
                    return conn
                conn.close()
            address, family = self._address, self._family
        return _Connection(self._connect(address, family, timeout_s=connect_timeout_s), generation)

    def _connect(self, address: Optional[str], family: Optional[str], *, timeout_s: Optional[float] = None) -> Any:
        """A new authenticated connection, or ModelHostUnavailable within CONNECT_TIMEOUT_S: the
        key exchange needs the child to answer, and a frozen child must not freeze the caller (the
        watcher kills it; the helper thread then ends on the closed socket)."""
        from multiprocessing.connection import Client

        box: Dict[str, Any] = {}

        def _dial() -> None:
            try:
                box["conn"] = Client(address, family=family, authkey=self._authkey)
            except BaseException as exc:  # noqa: BLE001 - reported below
                box["error"] = exc

        t = threading.Thread(target=_dial, name="model-host-connect", daemon=True)
        t.start()
        t.join(min(CONNECT_TIMEOUT_S, timeout_s) if timeout_s else CONNECT_TIMEOUT_S)
        if "conn" in box:
            return box["conn"]
        if t.is_alive():
            def _close_late() -> None:
                t.join()
                if "conn" in box:
                    with contextlib.suppress(Exception):
                        box["conn"].close()

            threading.Thread(target=_close_late, name="model-host-connect-late", daemon=True).start()
            raise ModelHostUnavailable("The model host (the process that runs the models) is not answering.")
        raise ModelHostUnavailable(
            "The model host (the process that runs the models) is not answering; it is being restarted.") from box.get("error")

    def _release(self, conn: _Connection) -> None:
        with self._lock:
            if conn.broken or conn.generation != self.generation or len(self._idle) >= POOL_IDLE_MAX:
                conn.close()
                return
            self._idle.append(conn)

    def _inflight_add(self, what: Dict[str, Any]) -> int:
        token = next(self._inflight_ids)
        with self._lock:
            self._inflight[token] = dict(what, since=time.time())
        return token

    def _inflight_remove(self, token: int) -> None:
        with self._lock:
            self._inflight.pop(token, None)

    def _crashed_error(self, generation: int, *, during: str) -> RuntimeError:
        proc = self._proc
        if proc is not None and generation == self.generation:
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                # Still running: this connection broke, the model host did not crash.
                return ModelHostUnavailable(
                    f"The connection to the model host broke while {during}; the model host is still running. "
                    "Send it again.")
        # Let the monitor record the exit first (a kill -9 is seen in milliseconds), so the
        # sentence names it and the incident file exists when the caller reads it.
        with self._lock:
            self._cond.wait_for(lambda: self._recorded_generation >= generation, timeout=1.5)
            recorded = self._recorded_generation >= generation
            last_exit = dict(self.last_exit or {}) if recorded else {}
            incident = dict(self.last_incident or {}) if recorded else {}
        what = last_exit.get("description") or "the connection broke"
        where = f"<data dir>/incidents/{incident['file']}" if incident.get("file") else "<data dir>/incidents"
        return ModelHostCrashed(
            f"The model host (the process that runs the models) stopped while {during} ({what}). "
            f"The gateway is fine and is restarting it; send the message again in a few seconds. "
            f"The incident is recorded in {where}.")

    # -- reporting
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            recent = [t for t in self._restarts if now - t <= self.restart_window_s]
            return {
                "state": self.state, "pid": self.pid, "generation": self.generation,
                "started_at": self.started_at, "restarts_in_window": len(recent),
                "max_restarts": self.max_restarts, "window_s": self.restart_window_s,
                "rss_bytes": self.last_rss, "memory_ceiling_bytes": self.memory_ceiling_bytes, "nice": self.nice,
                "inflight": len(self._inflight), "last_exit": self.last_exit, "last_incident": self.last_incident,
                "last_error": self.last_error,
            }


# ---- Building clients ------------------------------------------------------------------------------

def multi_client(**ctor: Any) -> ModelHostClient:
    sup = _supervisor
    if sup is None:
        raise RuntimeError("model_host.multi_client: no model host in this process")
    return ModelHostClient(sup, kind="multi", ctor=ctor)


def local_client(**ctor: Any) -> Any:
    """A `LocalAbstractCoreLLMClient` for a gateway-side generation: in the model host when this
    process has one, else in this process (the `in-process` mode)."""
    sup = _supervisor
    if sup is not None:
        return ModelHostClient(sup, kind="local", ctor=ctor)
    from abstractruntime.integrations.abstractcore.llm_client import LocalAbstractCoreLLMClient

    return LocalAbstractCoreLLMClient(**ctor)


_MULTI_CLIENT_FIELDS = (("provider", "_default_provider"), ("model", "_default_model"), ("llm_kwargs", "_llm_kwargs"),
                        ("artifact_store", "_artifact_store"), ("bloc_root_dir", "_bloc_root_dir"),
                        ("prompt_cache_export_root_dir", "_prompt_cache_export_root_dir"),
                        ("core_config_file", "_core_config_file"))


def mirror_multi_client(built: Any, *, capability_defaults: Any,
                        host: Optional["ModelHostSupervisor"] = None) -> ModelHostClient:
    """A model-host copy of the `MultiLocalAbstractCoreLLMClient` that `create_local_runtime` built
    in this process (with warm-up off, so it holds no model): the same constructor arguments,
    including the timeouts the factory added."""
    ctor: Dict[str, Any] = {}
    for arg, attr in _MULTI_CLIENT_FIELDS:
        if not hasattr(built, attr):
            raise TypeError(f"model host: {type(built).__name__} has no {attr!r}; this AbstractRuntime's pooled "
                            "client changed shape (tests/test_model_host_runtime_install.py)")
        value = getattr(built, attr)
        ctor[arg] = dict(value) if isinstance(value, dict) else value
    ctor["capability_defaults"] = capability_defaults
    if host is not None:
        return ModelHostClient(host, kind="multi", ctor=ctor)
    return multi_client(**ctor)


def move_runtime_client_to_host(runtime: Any, *, tools: Any, artifact_store: Any, run_store: Any,
                                extra_effect_handlers: Optional[Dict[Any, Any]], capability_defaults: Any) -> ModelHostClient:
    """In the `process` mode: the runtime `create_local_runtime` just built runs its model work in
    the model host from now on. Call it before anything else edits the runtime's handlers."""
    if tools is None:
        raise TypeError("model host: the runtime's tool executor must be passed explicitly")
    client = mirror_multi_client(getattr(runtime, "_abstractcore_llm_client"), capability_defaults=capability_defaults)
    install_runtime_client(runtime, client, tools=tools, artifact_store=artifact_store, run_store=run_store,
                           extra_effect_handlers=extra_effect_handlers)
    return client


def install_runtime_client(runtime: Any, client: ModelHostClient, *, tools: Any, artifact_store: Any,
                           run_store: Any, extra_effect_handlers: Optional[Dict[Any, Any]] = None) -> None:
    """Make a runtime built by `create_local_runtime` use `client` (in the model host) for every
    AbstractCore effect, the host facade (residency, prompt caches, catalogs) and chat summaries.

    Reads AbstractRuntime attributes (`_handlers`, `_abstractcore_llm_client`, `_chat_summarizer`):
    `tests/test_model_host_runtime_install.py` fails when they move (ADR 0002, Consequences)."""
    from abstractruntime.integrations.abstractcore.effect_handlers import build_effect_handlers

    handlers = getattr(runtime, "_handlers")
    if not isinstance(handlers, dict):
        raise TypeError("model host: this AbstractRuntime keeps its effect handlers differently")
    handlers.update(build_effect_handlers(llm=client, tools=tools, artifact_store=artifact_store, run_store=run_store))
    if extra_effect_handlers:
        handlers.update(dict(extra_effect_handlers))
    if not hasattr(runtime, "_abstractcore_llm_client"):
        raise TypeError("model host: this AbstractRuntime does not carry `_abstractcore_llm_client`")
    setattr(runtime, "_abstractcore_llm_client", client)
    summarizer = getattr(runtime, "_chat_summarizer", None)
    if summarizer is not None:
        if not hasattr(summarizer, "_llm_resolver"):
            raise TypeError("model host: this AbstractRuntime's chat summarizer has no `_llm_resolver`")

        def _resolve(provider: Optional[str] = None, model: Optional[str] = None) -> Any:
            if provider and model:
                return client.get_provider_instance(provider=provider, model=model)
            return getattr(client, "_llm", None)

        summarizer._llm_resolver = _resolve
