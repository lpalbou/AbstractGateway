"""Event-loop watchdog: a hung gateway exits so its service manager restarts it.

Why (2026-10-04 03:15): a text-to-speech stream blocked the event loop for
10+ minutes at 95% CPU. The process stayed alive, so nothing restarted it —
launchd's `KeepAlive` and systemd's `Restart=on-failure` restart a process
that EXITS, never one that hangs, and the local supervisor only counted its
failed health probes. A gateway whose loop cannot run answers nothing; the
honest thing is to say why and leave.

How:

- a tick coroutine on the event loop stamps `time.monotonic()` every
  `tick_s` (at most 1 s);
- a daemon thread checks the stamp; when it is older than `limit_s` (the
  `serve --watchdog-seconds` flag, default 30) it re-checks one tick later and
  fires only if the loop made NO progress meanwhile (a tick counter, not a
  clock delta): a laptop waking from sleep can make the monotonic clock jump
  by minutes while the loop is perfectly alive, and that jump alone must never
  restart the gateway (one ``[WARN]`` line says the loop resumed). When it does
  fire it first stands the backstop down, writes the incident file, then one
  ``[FATAL]`` line, the stack of the event-loop thread (the code that blocks
  it) and a faulthandler dump of every thread to stderr — the gateway log —
  and calls ``os._exit(WATCHDOG_EXIT_CODE)``;
- a GIL-independent backstop (POSIX): a tiny separate process reads one
  heartbeat byte per tick from a pipe. If no heartbeat came for
  ``limit_s + BACKSTOP_GRACE_S`` it re-checks for one more heartbeat (the same
  clock-jump rule), then asks the gateway's faulthandler to dump every thread
  (``SIGUSR1``, handled in C, no GIL needed), records a backstop incident and
  kills the gateway. This covers native code that blocks the loop while
  HOLDING the GIL, when the Python thread above cannot run at all. It exits
  when the pipe closes (the gateway stopped, exited, or its own watchdog took
  over). Platforms without ``fork``/``pass_fds`` keep faulthandler's timer.

Both exits are non-zero, which is what `KeepAlive: {SuccessfulExit: false}`
(launchd), `Restart=on-failure` (systemd) and the root installer/supervisor
loops act on.

The watchdog runs only under `abstractgateway serve` (the CLI configures it
before the server starts); an app imported elsewhere (tests, `--reload`'s
child) has it off and `/api/health` says so.

Incident file (R13.1): before exiting, the watchdog writes
``<data_dir>/incidents/watchdog-<UTC stamp>.json`` (when it was blocked, for
how long, the innermost frame of the loop thread and the innermost gateway
frame, the requests in flight, the loop stack) and
``watchdog-<stamp>.threads.txt`` (the faulthandler dump of every thread). The
next process reads the newest one at startup (`last_incident()`); the console's
Resources page shows "Gateway restarted at <time> after a hang — <reason>" and
the terminal console the same line. The log keeps the full dump too.
"""

from __future__ import annotations

import asyncio
import datetime
import faulthandler
import json
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

DEFAULT_WATCHDOG_SECONDS = 30.0
# EX_TEMPFAIL (sysexits.h): "temporary failure, the invoker should retry".
WATCHDOG_EXIT_CODE = 75
BACKSTOP_GRACE_S = 15.0

INCIDENT_SCHEMA = "abstractgateway.watchdog_incident.v1"

_configured_limit_s: Optional[float] = None
_incident_dir: Optional[Path] = None
_last_incident: Optional[Dict[str, Any]] = None
_active: Optional["LoopWatchdog"] = None
_lock = threading.Lock()

# Requests the event loop is serving now: {token: (method, path, monotonic start)}.
# Written on the loop thread, read by the watchdog thread when it fires (a
# snapshot copy; a torn read costs one line of forensics, never correctness).
_inflight: Dict[int, tuple] = {}


def configure(limit_s: Optional[float], *, incident_dir: Optional[Path] = None) -> None:
    """Called by `serve` before uvicorn starts: the limit for THIS process
    (None or <= 0 = off) and where an incident file goes (``<data_dir>/incidents``)."""
    global _configured_limit_s, _incident_dir
    _configured_limit_s = float(limit_s) if limit_s is not None and float(limit_s) > 0 else None
    _incident_dir = Path(incident_dir) if incident_dir is not None else None


def configured_limit_s() -> Optional[float]:
    return _configured_limit_s


def _dump_file(stream: Any) -> Any:
    """A real file for faulthandler (it writes to a file descriptor)."""
    try:
        stream.fileno()
        return stream
    except Exception:
        return sys.stderr


def _write(stream: Any, text: str) -> None:
    try:
        stream.write(text)
        stream.flush()
    except Exception:
        pass


def _frame_dict(fs: traceback.FrameSummary) -> Dict[str, Any]:
    return {"file": _short_path(fs.filename), "line": fs.lineno, "function": fs.name}


def _short_path(path: str) -> str:
    """`…/site-packages/starlette/responses.py` → `starlette/responses.py`; a gateway
    file → `abstractgateway/…` (no home directory in the console)."""
    norm = str(path or "").replace("\\", "/")
    for marker in ("/site-packages/", "/src/", "/lib/python"):
        if marker in norm:
            tail = norm.rsplit(marker, 1)[1]
            if marker == "/lib/python":
                tail = tail.split("/", 1)[1] if "/" in tail else tail
            return tail
    return norm.rsplit("/", 1)[-1]


def _frame_text(fd: Optional[Dict[str, Any]]) -> str:
    return f"{fd['file']}:{fd['line']} {fd['function']}" if fd else "unknown"


def incident_reason(incident: Dict[str, Any]) -> str:
    """One sentence: where the loop was blocked (and from which gateway code, for which request)."""
    top = incident.get("top_frame")
    gw = incident.get("gateway_frame")
    text = f"the event loop was blocked in {_frame_text(top)}"
    if gw and gw != top:
        text += f" (called from {_frame_text(gw)})"
    reqs = incident.get("requests_in_flight") or []
    if reqs:
        oldest = max(reqs, key=lambda r: float(r.get("age_s") or 0))
        text += f" while serving {oldest.get('method')} {oldest.get('path')}"
    return text


def build_incident(watchdog: "LoopWatchdog", age_s: float) -> Dict[str, Any]:
    tid = watchdog._loop_thread_id
    frame = sys._current_frames().get(tid) if tid is not None else None
    stack = traceback.extract_stack(frame) if frame is not None else []
    frames = [_frame_dict(fs) for fs in stack]
    gateway_frames = [f for f in frames if str(f["file"]).startswith("abstractgateway/") and f["file"] != "abstractgateway/loop_watchdog.py"]
    now = time.monotonic()
    requests = [
        {"method": m, "path": p, "age_s": round(now - t0, 1)}
        for (m, p, t0) in list(_inflight.values())
    ]
    incident: Dict[str, Any] = {
        "schema": INCIDENT_SCHEMA,
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "pid": os.getpid(),
        "limit_s": watchdog.limit_s,
        "blocked_s": round(float(age_s), 1),
        "exit_code": WATCHDOG_EXIT_CODE,
        "top_frame": frames[-1] if frames else None,
        "gateway_frame": gateway_frames[-1] if gateway_frames else None,
        "requests_in_flight": sorted(requests, key=lambda r: -float(r["age_s"]))[:10],
        "loop_stack": frames[-40:],
    }
    incident["reason"] = incident_reason(incident)
    return incident


def write_incident(watchdog: "LoopWatchdog", age_s: float, *, directory: Optional[Path] = None) -> Optional[Path]:
    """Write ``watchdog-<stamp>.json`` (+ ``.threads.txt``) into the incident directory; never raises."""
    target = directory if directory is not None else _incident_dir
    if target is None:
        return None
    try:
        target.mkdir(parents=True, exist_ok=True)
        incident = build_incident(watchdog, age_s)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        incident["stamp"] = stamp
        dump = target / f"watchdog-{stamp}.threads.txt"
        try:
            with open(dump, "w", encoding="utf-8") as fh:
                fh.write(f"gateway watchdog {incident['at']}: {incident['reason']}\n")
                fh.flush()
                faulthandler.dump_traceback(file=fh, all_threads=True)
            incident["dump_path"] = str(dump)
        except Exception:
            pass
        path = target / f"watchdog-{stamp}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(incident, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        return path
    except Exception:
        return None


def read_last_incident(directory: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """The newest incident file (by its UTC stamp), or None. Never raises."""
    target = directory if directory is not None else _incident_dir
    if target is None:
        return None
    try:
        files = sorted(p for p in Path(target).glob("watchdog-*.json") if p.is_file())
        for path in reversed(files):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict) and data.get("schema") == INCIDENT_SCHEMA:
                data["file"] = str(path)
                data.setdefault("reason", incident_reason(data))
                return data
    except Exception:
        return None
    return None


def load_last_incident() -> Optional[Dict[str, Any]]:
    """Startup (off the loop's hot path, once): remember the previous process's newest incident."""
    global _last_incident
    _last_incident = read_last_incident()
    return _last_incident


def last_incident() -> Optional[Dict[str, Any]]:
    return _last_incident


def last_incident_view() -> Optional[Dict[str, Any]]:
    """What the console and the terminal console show (`/api/gateway/host/runner` → `last_hang`)."""
    inc = _last_incident
    if not inc:
        return None
    at = str(inc.get("at") or "")
    return {
        "at": at,
        "stamp": inc.get("stamp"),
        "blocked_s": inc.get("blocked_s"),
        "reason": str(inc.get("reason") or ""),
        "top_frame": _frame_text(inc.get("top_frame")),
        "dump_path": inc.get("dump_path"),
        "file": inc.get("file"),
        "line": f"Gateway restarted at {at} after a hang — {inc.get('reason') or 'unknown'}",
    }


class InflightRequests:
    """Outermost ASGI wrapper: remembers which requests the loop is serving, for the incident file."""

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope, receive, send):  # noqa: ANN001 - ASGI signature
        if scope.get("type") != "http":
            return await self._app(scope, receive, send)
        token = id(scope)
        _inflight[token] = (str(scope.get("method") or ""), str(scope.get("path") or ""), time.monotonic())
        try:
            return await self._app(scope, receive, send)
        finally:
            _inflight.pop(token, None)


def _exit_after_stall(watchdog: "LoopWatchdog", age_s: float) -> None:
    """Default stall action: stand the backstop down, write the incident, dump, exit 75.

    Order matters: the backstop is stood down FIRST (two killers racing left an
    exit 1 and no incident file), and the incident file is written BEFORE the
    long stack dumps, so it exists even if writing the log is slow."""
    watchdog.stand_down_backstop()
    stream = watchdog.stream
    incident_path = write_incident(watchdog, age_s)
    _write(
        stream,
        f"[FATAL] gateway watchdog: the event loop has not run for {age_s:.1f}s (limit {watchdog.limit_s:g}s, "
        f"`serve --watchdog-seconds`); the gateway answers nothing while it is blocked. Dumping stacks and exiting "
        f"with code {WATCHDOG_EXIT_CODE} so the service manager restarts it.\n",
    )
    if incident_path is not None:
        _write(stream, f"[FATAL] gateway watchdog: incident written to {incident_path}\n")
    _write(stream, watchdog.format_loop_stack())
    try:
        _write(stream, "[FATAL] gateway watchdog: all threads (faulthandler):\n")
        faulthandler.dump_traceback(file=stream, all_threads=True)
    except Exception:
        pass
    _write(stream, f"[FATAL] gateway watchdog: exiting with code {WATCHDOG_EXIT_CODE}\n")
    os._exit(WATCHDOG_EXIT_CODE)


# The backstop process (POSIX). Arguments: parent pid, heartbeat fd, budget s, tick s,
# incident dir ("" = none). Standard library only; run with -I from "/".
_BACKSTOP_CHILD = r"""
import datetime, json, os, select, signal, sys, time
pid, fd, budget, tick, incident_dir = int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4]), sys.argv[5]
try:
    os.setsid()
except Exception:
    pass
signal.signal(signal.SIGINT, signal.SIG_IGN)

def gateway_alive():
    # The gateway is this process's PARENT. A pid number alone is not an identity (it can
    # be reused), and the pipe may stay open in a forked child after the gateway died, so
    # the moment the parent changes the backstop has nothing left to watch.
    return os.getppid() == pid

def beat(timeout):
    if not gateway_alive():
        sys.exit(0)
    r, _, _ = select.select([fd], [], [], min(timeout, tick))
    if not r:
        return False
    if not os.read(fd, 4096):
        sys.exit(0)  # the gateway closed the pipe: stopped, exited, or its own watchdog took over
    return True

last = time.monotonic()
while True:
    if beat(tick):
        last = time.monotonic()
        continue
    if time.monotonic() - last <= budget:
        continue
    # Past the budget. A clock jump (the machine slept) looks the same, so wait for one
    # more heartbeat: a live loop sends one within a tick.
    if beat(tick) or beat(tick):
        last = time.monotonic()
        continue
    if not gateway_alive():
        sys.exit(0)
    at = datetime.datetime.now(datetime.timezone.utc)
    try:
        os.kill(pid, signal.SIGUSR1)  # the gateway's faulthandler dumps every thread (C level)
    except OSError:
        sys.exit(0)
    time.sleep(1.0)
    if not gateway_alive():
        sys.exit(0)
    if incident_dir:
        try:
            os.makedirs(incident_dir, exist_ok=True)
            stamp = at.strftime("%Y%m%dT%H%M%SZ")
            inc = {"schema": "abstractgateway.watchdog_incident.v1", "at": at.isoformat(timespec="seconds"), "stamp": stamp,
                   "pid": pid, "limit_s": budget, "blocked_s": round(time.monotonic() - last, 1), "exit_code": -9,
                   "kind": "backstop", "top_frame": None, "gateway_frame": None, "requests_in_flight": [], "loop_stack": [],
                   "reason": "the event loop and the watchdog thread were both blocked (native code holding the interpreter lock); every thread's stack is in the gateway log",
                   "dump_path": None}
            path = os.path.join(incident_dir, "watchdog-" + stamp + ".json")
            with open(path + ".tmp", "w") as fh:
                json.dump(inc, fh, indent=2, sort_keys=True)
            os.replace(path + ".tmp", path)
        except Exception:
            pass
    if gateway_alive():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    sys.exit(0)
"""


def _backstop_process_supported() -> bool:
    return os.name == "posix" and hasattr(signal, "SIGUSR1") and hasattr(signal, "SIGKILL")


class LoopWatchdog:
    def __init__(
        self,
        limit_s: float,
        *,
        on_stall: Optional[Callable[["LoopWatchdog", float], None]] = None,
        backstop: bool = True,
        stream: Any = None,
    ) -> None:
        self.limit_s = float(limit_s)
        self.tick_s = max(0.05, min(1.0, self.limit_s / 10.0))
        self._on_stall = on_stall or _exit_after_stall
        self._backstop = bool(backstop)
        self.stream = stream if stream is not None else sys.stderr
        self._last_tick = time.monotonic()
        # Loop progress: incremented by every tick. A hang is "no new tick", never
        # "a large clock delta" (a wake from sleep can jump the monotonic clock).
        self._ticks = 0
        self._loop_thread_id: Optional[int] = None
        self._stop = threading.Event()
        self._task: Optional[asyncio.Task] = None
        self._thread: Optional[threading.Thread] = None
        self._beat_fd: Optional[int] = None
        self._backstop_proc: Optional[subprocess.Popen] = None
        self._faulthandler_timer = False
        self.resumed: List[float] = []  # ages that turned out to be clock jumps with a live loop
        self.fired = False

    # -- event-loop side ------------------------------------------------
    async def _tick(self) -> None:
        while True:
            self._last_tick = time.monotonic()
            self._ticks += 1
            fd = self._beat_fd
            if fd is not None:
                try:
                    os.write(fd, b".")
                except (BlockingIOError, InterruptedError):
                    pass  # the pipe is full (the backstop is behind): a beat is a beat
                except OSError:
                    self._beat_fd = None  # the backstop is gone
            if self._faulthandler_timer:
                try:
                    faulthandler.dump_traceback_later(self.limit_s + BACKSTOP_GRACE_S, exit=True, file=self.stream)
                except Exception:
                    pass
            await asyncio.sleep(self.tick_s)

    def start(self) -> None:
        """Start from INSIDE the running event loop (lifespan startup)."""
        self._loop_thread_id = threading.get_ident()
        self._last_tick = time.monotonic()
        if self._backstop:
            self._start_backstop()
        self._task = asyncio.get_running_loop().create_task(self._tick(), name="gateway-loop-watchdog-tick")
        self._thread = threading.Thread(target=self._watch, name="gateway-loop-watchdog", daemon=True)
        self._thread.start()

    def _start_backstop(self) -> None:
        if not _backstop_process_supported():
            self._faulthandler_timer = True  # no separate process here: faulthandler's own timer
            return
        try:
            faulthandler.register(signal.SIGUSR1, file=_dump_file(self.stream), all_threads=True, chain=False)
        except Exception:
            pass
        r, w = os.pipe()
        try:
            os.set_blocking(w, False)
            self._backstop_proc = subprocess.Popen(
                [sys.executable, "-I", "-c", _BACKSTOP_CHILD, str(os.getpid()), str(r),
                 str(self.limit_s + BACKSTOP_GRACE_S), str(self.tick_s), str(_incident_dir or "")],
                pass_fds=(r,), cwd="/", stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._beat_fd = w
        except Exception as e:
            os.close(w)
            self._faulthandler_timer = True
            _write(self.stream, f"[WARN] gateway watchdog: backstop process unavailable ({type(e).__name__}); using faulthandler's timer\n")
        finally:
            os.close(r)

    def stand_down_backstop(self) -> None:
        """Close the heartbeat pipe (the backstop process exits) and cancel faulthandler's timer."""
        fd, self._beat_fd = self._beat_fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            faulthandler.cancel_dump_traceback_later()
        except Exception:
            pass
        self._faulthandler_timer = False

    def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
        self.stand_down_backstop()
        proc, self._backstop_proc = self._backstop_proc, None
        if proc is not None:
            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    # -- watcher side ---------------------------------------------------
    def last_tick_age_s(self) -> float:
        return max(0.0, time.monotonic() - self._last_tick)

    def check(self, wait: Optional[Callable[[float], bool]] = None) -> Optional[float]:
        """One watcher decision. Returns the stalled age when the loop is hung, else None.

        Over the limit, the watcher waits one tick and fires only when the loop made
        no progress meanwhile; a jump of the clock with a live loop is a resume."""
        age = self.last_tick_age_s()
        if age <= self.limit_s:
            return None
        seen = self._ticks
        if (wait or self._stop.wait)(self.tick_s):
            return None  # stopping
        if self._ticks != seen:
            self.resumed.append(age)
            _write(
                self.stream,
                f"[WARN] gateway watchdog: the event loop resumed after {age:.1f}s; not restarted\n",
            )
            return None
        return self.last_tick_age_s()

    def _watch(self) -> None:
        while not self._stop.wait(self.tick_s):
            age = self.check()
            if age is not None:
                self.fired = True
                self._on_stall(self, age)
                return

    def format_loop_stack(self) -> str:
        tid = self._loop_thread_id
        frame = sys._current_frames().get(tid) if tid is not None else None
        if frame is None:
            return "[FATAL] gateway watchdog: event-loop thread stack unavailable\n"
        return "[FATAL] gateway watchdog: the event-loop thread is blocked here:\n" + "".join(traceback.format_stack(frame))

    def snapshot(self) -> Dict[str, Any]:
        return {"enabled": True, "limit_s": self.limit_s, "last_tick_age_s": round(self.last_tick_age_s(), 3)}


def start_configured() -> Optional[LoopWatchdog]:
    """Lifespan startup: start the watchdog `serve` configured (no-op when off)."""
    global _active
    limit = _configured_limit_s
    if limit is None:
        return None
    with _lock:
        if _active is not None:
            _active.stop()
        _active = LoopWatchdog(limit)
        _active.start()
        return _active


def stop_active() -> None:
    """Lifespan shutdown: a draining gateway is not a hung one (uvicorn's
    graceful-shutdown timeout bounds the drain)."""
    global _active
    with _lock:
        if _active is not None:
            _active.stop()
        _active = None


def health_snapshot() -> Dict[str, Any]:
    wd = _active
    if wd is None:
        return {"enabled": False, "limit_s": _configured_limit_s, "last_tick_age_s": None}
    return wd.snapshot()
