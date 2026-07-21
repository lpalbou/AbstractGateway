"""Run Gateway runner worker (AbstractGateway).

Backlog: 307-Framework: Durable Run Gateway (Command Inbox + Ledger Stream)

Key properties (v0):
- Commands are accepted by being appended to a durable JSONL inbox (idempotent by command_id).
- A background worker polls the inbox and applies commands to persisted runs.
- A tick loop progresses RUNNING runs by calling Runtime.tick(...) and appending StepRecords.
- Clients render by replaying the durable ledger (cursor/offset semantics), not by relying on live RPC.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Protocol

from abstractruntime import Runtime
from abstractruntime.core.event_keys import build_event_wait_key
from abstractruntime.core.models import Effect, EffectType, RunStatus, StepRecord, WaitReason
from abstractruntime.scheduler.scheduler import utc_now_iso
from abstractruntime.storage.commands import (
    CommandCursorStore,
    CommandRecord,
    CommandStore,
    JsonFileCommandCursorStore,
    JsonlCommandStore,
)


logger = logging.getLogger(__name__)


def _is_pause_wait(waiting: Any, *, run_id: str) -> bool:
    if waiting is None:
        return False
    wait_key = getattr(waiting, "wait_key", None)
    if isinstance(wait_key, str) and wait_key == f"pause:{run_id}":
        return True
    details = getattr(waiting, "details", None)
    if isinstance(details, dict) and details.get("kind") == "pause":
        return True
    return False


class GatewayHost(Protocol):
    """Host capability needed by GatewayRunner to tick/resume runs."""

    @property
    def run_store(self) -> Any: ...

    @property
    def ledger_store(self) -> Any: ...

    @property
    def artifact_store(self) -> Any: ...

    def runtime_and_workflow_for_run(self, run_id: str) -> tuple[Runtime, Any]: ...


def _default_lock_stale_after_s() -> float:
    """Heartbeat staleness threshold for the singleton lock (seconds).

    A holder heartbeats the lock file mtime every loop iteration; a reader that
    sees a heartbeat older than this concludes "nobody is ticking this data_dir"
    (wedged or foreign holder) and surfaces it loudly. Env-tunable so tests and
    unusual deployments can tighten/relax it without code changes.
    """
    raw = os.getenv("ABSTRACTGATEWAY_RUNNER_LOCK_STALE_S")
    if raw is None or not str(raw).strip():
        return 10.0
    try:
        val = float(str(raw).strip())
        return val if val > 0 else 10.0
    except Exception:
        return 10.0


@dataclass(frozen=True)
class GatewayRunnerConfig:
    poll_interval_s: float = 0.25
    command_batch_limit: int = 200
    tick_max_steps: int = 100
    tick_workers: int = 4
    run_scan_limit: int = 200
    # Consecutive workflow-resolution failures tolerated before a RUNNING run is
    # promoted to FAILED (~10s at the default 0.25s poll). Tolerates transient
    # startup races (catalog bundles still loading) while refusing to leave a
    # run silently spinning RUNNING forever with zero ledger.
    workflow_resolution_failure_limit: int = 40
    # Minimum seconds between store-fingerprint probes while the store is
    # QUIET (fingerprint unchanged). Commands keep the full poll_interval_s
    # cadence regardless; a processed command forces the next pass, and
    # runner.nudge() (run-start routes) skips the wait entirely.
    scan_gate_idle_interval_s: float = 0.5


def file_store_fingerprint(base_dir: Path) -> tuple:
    """Cheap change fingerprint over a JsonFileRunStore directory.

    (count, max mtime_ns, sum mtime_ns) over run_*.json: any save/create/
    delete moves it (saves are atomic tmp->replace, so mtime always bumps).
    ~30ms warm at 3k files vs the FULL-STORE JSON PARSE it gates (seconds).

    2026-07-15 incident (entity's profile, commons c2394): a 3,241-file /
    659MB store with ZERO running runs pegged the gateway at ~100% CPU
    forever — every 0.25s poll ran three scarce-match scans, each parsing
    every file because matches were scarce and the 512-entry LRU cannot
    hold 3,241 entries (the scan itself evicts everything it caches). The
    fingerprint gate skips the scans entirely while nothing changes; the
    store-side terminal-skip fix is runtime's half (entity ask 3).
    """
    count = 0
    max_ns = 0
    sum_ns = 0
    for p in Path(base_dir).glob("run_*.json"):
        try:
            ns = int(p.stat().st_mtime_ns)
        except OSError:
            continue
        count += 1
        sum_ns += ns
        if ns > max_ns:
            max_ns = ns
    return (count, max_ns, sum_ns)


_UNRESOLVED = object()


def _epoch_from_iso(value: str) -> Optional[float]:
    """Aware-UTC epoch seconds from an ISO string, None when unparseable."""
    try:
        dt = datetime.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def _file_store_base(run_store: Any) -> Optional[Path]:
    """The run_*.json directory when the store chain bottoms out in a
    JsonFileRunStore (walking Offloading-style wrappers via `.inner`), else
    None. None DISABLES the scan gate — indexed stores (sqlite, in-memory)
    scan cheaply and a constant fingerprint over a fileless dir would skip
    their scans forever."""
    obj = run_store
    for _ in range(5):
        if obj is None:
            return None
        if type(obj).__name__ == "JsonFileRunStore":
            base = getattr(obj, "_base", None)
            return Path(base) if base is not None else None
        nxt = getattr(obj, "inner", None)
        if nxt is None:
            nxt = getattr(obj, "_inner", None)
        obj = nxt
    return None


class GatewayRunner:
    """Background worker: poll command inbox + tick runs forward."""

    def __init__(
        self,
        *,
        base_dir: Path,
        host: GatewayHost,
        config: Optional[GatewayRunnerConfig] = None,
        enable: bool = True,
        command_store: CommandStore | None = None,
        cursor_store: CommandCursorStore | None = None,
    ) -> None:
        self._base_dir = Path(base_dir)
        self._host = host
        self._cfg = config or GatewayRunnerConfig()
        self._enable = bool(enable)

        self._command_store: CommandStore = command_store or JsonlCommandStore(self._base_dir)
        self._cursor_store: CommandCursorStore = cursor_store or JsonFileCommandCursorStore(
            self._base_dir / "commands_cursor.json"
        )

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._executor = ThreadPoolExecutor(max_workers=max(1, int(self._cfg.tick_workers or 1)))
        self._inflight: set[str] = set()
        self._inflight_lock = threading.Lock()

        self._singleton_lock_path = self._base_dir / "gateway_runner.lock"
        self._singleton_lock_fh = None
        self._takeover_path = self._base_dir / "gateway_runner.takeover"
        self._lock_stale_after_s = _default_lock_stale_after_s()

        # Singleton-lock observability state. The incident class this guards:
        # a run-ACCEPTING gateway process whose runner lost the lock race to an
        # orphaned older process — every run it creates is ticked by nobody and
        # hangs forever on the entry node with zero ledger records, while the
        # only trace is a single logger.warning. State transitions here feed
        # runner_status() (health surface) and inactive_warning() (run-start).
        self._state_lock = threading.Lock()
        self._lock_held = False
        self._lock_refused_flag = False
        self._lock_holder_pid: Optional[int] = None
        self._lock_acquired_at: Optional[str] = None
        self._takeover_requested = False
        self._takeover_requested_at: Optional[str] = None
        self._yielded_to_pid: Optional[int] = None
        self._last_lock_error: Optional[str] = None
        self._loop_running = False

        # run_id -> consecutive runtime_and_workflow_for_run failures.
        # Entries exist only while a run keeps failing resolution; cleared on
        # success, on promotion to FAILED, and when the run leaves RUNNING.
        self._resolution_failures: Dict[str, int] = {}
        self._resolution_failures_lock = threading.Lock()

        # Scan gate state (2026-07-15 CPU incident, commons c2394): on file
        # stores, the three per-poll scarce-match scans re-parse the WHOLE
        # store; the gate skips the pass while a cheap mtime fingerprint says
        # nothing changed and no wait deadline is due. `_scan_gate_base` is
        # resolved lazily from the store chain (None = non-file store = gate
        # disabled, scans stay unconditional).
        self._scan_gate_base: Any = _UNRESOLVED
        self._scan_force = True  # first pass always scans
        self._scan_fingerprint: Optional[tuple] = None
        self._scan_last_probe = 0.0
        self._next_due_epoch: Optional[float] = None

    @property
    def enabled(self) -> bool:
        return self._enable

    @property
    def lock_refused(self) -> bool:
        """True while this enabled runner is locked out by another process."""
        with self._state_lock:
            return bool(self._lock_refused_flag)

    @property
    def lock_held(self) -> bool:
        with self._state_lock:
            return bool(self._lock_held)

    @property
    def command_store(self) -> CommandStore:
        return self._command_store

    def nudge(self) -> None:
        """Tell the runner something changed (a run started/resumed through
        the HTTP surface): the next poll runs the scheduling pass without
        waiting for a fingerprint probe. Keeps interactive run-start latency
        at poll_interval_s under the scan gate. Safe from any thread."""
        self._scan_force = True

    @property
    def run_store(self) -> Any:
        return self._host.run_store

    @property
    def ledger_store(self) -> Any:
        return self._host.ledger_store

    @property
    def artifact_store(self) -> Any:
        return self._host.artifact_store

    def start(self) -> None:
        """Start (or ensure) the runner worker thread.

        The thread owns the whole singleton-lock lifecycle: it retries
        acquisition until it wins (so a lock freed by a dying holder is picked
        up within one retry interval instead of waiting for the next
        run-start), requests a one-shot takeover from a live holder (newest
        process wins — gateway startup semantics are "replace"), and runs the
        tick loop only while actually holding the kernel lock. A refused lock
        is therefore a visible, recoverable state — never a silent dead end.
        """
        if not self._enable:
            logger.info("GatewayRunner disabled by config/env")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="abstractgateway-runner", daemon=True)
        self._thread.start()
        logger.info("GatewayRunner worker started (base_dir=%s)", self._base_dir)

    def stop(self, timeout_s: float = 5.0, *, drain_timeout_s: float = 30.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)
        self._thread = None
        # No NEW ticks will be scheduled (the loop thread is gone); cancel
        # QUEUED futures. cancel_futures does NOT interrupt an ALREADY-RUNNING
        # tick.
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)  # type: ignore[call-arg]
        except Exception:
            pass
        # DRAIN BEFORE RELEASE (adversary find, 2026-07-11 — the wave's own
        # central invariant): releasing the flock while a tick is still
        # executing on an executor thread lets another runner (a live standby
        # peer, or a same-data_dir twin after invalidate_gateway_service_for_
        # runtime) acquire and tick the SAME run concurrently — duplicated
        # StepRecords, double-executed effects (LLM spend / tool calls), and
        # last-writer-wins clobbering a WAITING transition back to RUNNING.
        # The yield path already drains before releasing; stop() must too.
        # _stop is set, so ignore it here (an unbounded stop-blocked drain
        # would defeat itself).
        drained = self._drain_inflight(timeout_s=max(0.0, float(drain_timeout_s)), ignore_stop=True)
        if drained:
            self._release_singleton_lock()
        else:
            # A genuinely wedged tick (e.g. a no-timeout provider call). Keep
            # HOLDING the flock rather than double-tick: on uvicorn shutdown
            # the fd closes on process exit (kernel releases); the rare
            # same-process teardown that outlives a wedged tick leaks this
            # lock until exit — the lesser evil vs ledger corruption, and
            # loud so an operator sees it.
            logger.error(
                "GatewayRunner.stop: in-flight ticks did not drain within %ss; HOLDING singleton "
                "lock %s to avoid concurrent double-ticking (released on process exit)",
                drain_timeout_s,
                self._singleton_lock_path,
            )
        with self._state_lock:
            self._loop_running = False
            self._lock_refused_flag = False

    def emit_event(
        self,
        *,
        name: str,
        payload: Any,
        session_id: str,
        scope: str = "session",
        workflow_id: Optional[str] = None,
        run_id: Optional[str] = None,
        event_id: Optional[str] = None,
        emitted_at: Optional[str] = None,
        client_id: Optional[str] = None,
        durable: bool = False,
    ) -> Dict[str, int]:
        """Emit an external event into the runtime (resume matching WAIT_EVENT runs).

        This is a thin wrapper around the internal emit_event command handling so
        integrations (Telegram bridge, webhooks, etc.) don't need to know the
        command-store format. `durable=True` additionally appends the envelope
        to the `events_inbox` of every non-terminal run declaring the mailbox
        (the resident-agent drain contract) — a busy resident receives the
        event at its next loop boundary instead of dropping it.

        Returns receiver counts {"resumed": n, "appended": m} — callers whose
        correctness depends on SOMEONE receiving the event (the agora bridge's
        cursor) must treat 0+0 as non-delivery, never as success.
        """

        name2 = str(name or "").strip()
        if not name2:
            raise ValueError("name is required")
        sid = str(session_id or "").strip()
        if not sid:
            raise ValueError("session_id is required")

        body: Dict[str, Any] = {
            "name": name2,
            "scope": str(scope or "session").strip().lower() or "session",
            "session_id": sid,
            "payload": payload,
        }
        if durable:
            body["durable"] = True
        if isinstance(workflow_id, str) and workflow_id.strip():
            body["workflow_id"] = workflow_id.strip()
        if isinstance(run_id, str) and run_id.strip():
            body["run_id"] = run_id.strip()
        if isinstance(event_id, str) and event_id.strip():
            body["event_id"] = event_id.strip()
        if isinstance(emitted_at, str) and emitted_at.strip():
            body["emitted_at"] = emitted_at.strip()

        counts = self._apply_emit_event(body, default_session_id=sid, client_id=client_id)
        return counts if isinstance(counts, dict) else {"resumed": 0, "appended": 0}

    def _acquire_singleton_lock(self) -> bool:
        """Best-effort process singleton lock (prevents multi-worker double ticking).

        flock() semantics (verified by test_flock_autoreleases_when_holder_process_dies):
        the kernel releases the lock the instant the holding process exits, even
        on SIGKILL — a *dead* holder never blocks acquisition. Refusal therefore
        always means a LIVE process holds the lock; staleness handling reduces
        to (a) retrying (dead holder → next attempt wins) and (b) the takeover
        handshake for a live-but-wrong holder. Content is diagnostics only —
        the flock itself is the single source of mutual-exclusion truth.
        """
        try:
            import fcntl  # Unix only
        except Exception:  # pragma: no cover
            with self._state_lock:
                self._lock_held = True
                self._lock_refused_flag = False
                self._last_lock_error = "flock unsupported on this platform (no mutual exclusion)"
            return True
        try:
            self._singleton_lock_path.parent.mkdir(parents=True, exist_ok=True)
            fh = self._singleton_lock_path.open("a", encoding="utf-8")
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except Exception:
                # Read holder diagnostics from the file we just failed to lock.
                holder_pid = self._read_lock_holder_pid()
                try:
                    fh.close()
                except Exception:
                    pass
                with self._state_lock:
                    self._lock_held = False
                    self._lock_refused_flag = True
                    self._lock_holder_pid = holder_pid
                return False
            # Acquired: truncate stale holder info and write ours. Truncation
            # must happen AFTER flock — an O_TRUNC open before holding would
            # clobber a live holder's diagnostics.
            try:
                fh.seek(0)
                fh.truncate()
                fh.write(f"pid={os.getpid()}\nacquired_at={utc_now_iso()}\n")
                fh.flush()
            except Exception:
                pass
            self._singleton_lock_fh = fh
            with self._state_lock:
                self._lock_held = True
                self._lock_refused_flag = False
                self._lock_holder_pid = os.getpid()
                self._lock_acquired_at = utc_now_iso()
                self._yielded_to_pid = None
                self._last_lock_error = None
            return True
        except Exception as e:
            try:
                if self._singleton_lock_fh is not None:
                    self._singleton_lock_fh.close()
            except Exception:
                pass
            self._singleton_lock_fh = None
            with self._state_lock:
                self._lock_held = False
                self._lock_refused_flag = True
                self._last_lock_error = f"{type(e).__name__}: {e}"
            return False

    def _release_singleton_lock(self) -> None:
        try:
            if self._singleton_lock_fh is not None:
                self._singleton_lock_fh.close()
        except Exception:
            pass
        self._singleton_lock_fh = None
        with self._state_lock:
            self._lock_held = False

    def _read_lock_holder_pid(self) -> Optional[int]:
        """Parse the holder pid from the lock file (diagnostics only)."""
        try:
            text = self._singleton_lock_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return None
        pid: Optional[int] = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("pid="):
                try:
                    pid = int(line[len("pid="):].strip())
                except Exception:
                    continue
        return pid

    def _lock_heartbeat_age_s(self) -> Optional[float]:
        """Seconds since the lock holder last heartbeat (mtime), or None."""
        try:
            return max(0.0, time.time() - self._singleton_lock_path.stat().st_mtime)
        except Exception:
            return None

    @staticmethod
    def _pid_alive(pid: Optional[int]) -> bool:
        if not isinstance(pid, int) or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Exists but owned by another user — treat as alive.
            return True
        except Exception:
            return False

    # -- takeover handshake -------------------------------------------------
    #
    # A live-but-wrong holder (orphaned older gateway on the same data_dir)
    # cannot be stolen from at the flock level without killing it. Instead the
    # NEW process writes a one-shot takeover request; the holder's loop checks
    # it each iteration, drains in-flight ticks, releases the flock, and drops
    # to passive standby (it may re-acquire a FREE lock later but never issues
    # takeover requests itself). Ownership only ever transfers through the
    # kernel lock, so two concurrent tickers remain structurally impossible,
    # and simultaneous multi-worker startups converge to exactly one stable
    # ticker (the newest requester) instead of ping-ponging.

    def _request_takeover_once(self) -> None:
        with self._state_lock:
            if self._takeover_requested:
                return
            self._takeover_requested = True
            self._takeover_requested_at = utc_now_iso()
        payload = {"pid": os.getpid(), "requested_at": utc_now_iso()}
        try:
            tmp = self._takeover_path.with_suffix(f".tmp.{os.getpid()}.{random.randint(0, 1_000_000)}")
            tmp.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            os.replace(tmp, self._takeover_path)
            logger.warning(
                "GatewayRunner: singleton lock held by live pid %s; requested takeover of %s (newest process wins)",
                self._read_lock_holder_pid(),
                self._singleton_lock_path,
            )
        except Exception as e:
            logger.warning("GatewayRunner: failed to write takeover request %s: %s", self._takeover_path, e)

    def _read_takeover_request(self) -> Optional[int]:
        try:
            raw = self._takeover_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except Exception:
            return None
        try:
            pid = int((json.loads(raw) or {}).get("pid"))
            return pid if pid > 0 else None
        except Exception:
            # Unreadable request file: treat as garbage and remove it so it
            # cannot wedge the handshake forever.
            self._clear_takeover_request(only_pid=None)
            return None

    def _clear_takeover_request(self, *, only_pid: Optional[int]) -> None:
        """Remove the takeover file (optionally only when it names only_pid)."""
        try:
            if only_pid is not None:
                pid = self._read_takeover_request()
                if pid is not None and pid != only_pid:
                    return
            self._takeover_path.unlink(missing_ok=True)
        except Exception:
            pass

    def _takeover_yield_requested(self) -> bool:
        """Holder-side check: should this loop yield the lock to a newer process?"""
        pid = self._read_takeover_request()
        if pid is None:
            return False
        if pid == os.getpid():
            # Our own leftover request (we since acquired) — clean it up.
            self._clear_takeover_request(only_pid=pid)
            return False
        if not self._pid_alive(pid):
            # Requester died before acquiring; drop the stale request.
            self._clear_takeover_request(only_pid=pid)
            return False
        with self._state_lock:
            self._yielded_to_pid = pid
        return True

    def _drain_inflight(self, *, timeout_s: Optional[float] = None, ignore_stop: bool = False) -> bool:
        """Wait for in-flight tick futures to finish (no new ones are scheduled).

        Yielding the lock while ticks are still executing would let the new
        holder tick the same runs concurrently — the exact double-ticking the
        lock exists to prevent. Unbounded by default: correctness beats speed,
        and the requester keeps retrying while we drain.

        `ignore_stop`: the yield path drains while the loop keeps running, so
        it stops early if a shutdown is requested; the stop() path drains
        AFTER setting _stop, so it must ignore _stop (bounded by timeout_s)
        or it would return immediately and abandon a running tick.
        """
        started = time.time()
        while ignore_stop or not self._stop.is_set():
            with self._inflight_lock:
                if not self._inflight:
                    return True
            if timeout_s is not None and (time.time() - started) > timeout_s:
                return False
            time.sleep(0.05)
        return False

    def runner_status(self) -> Dict[str, Any]:
        """Snapshot of runner liveness for health/status surfaces.

        `status` values:
        - disabled: runner intentionally off for this process (split mode)
        - active: this process holds the lock and its loop is ticking
        - standby_peer_active: lock held by another live process with a fresh
          heartbeat (legitimate co-worker/split-runner is ticking)
        - degraded_no_ticker: enabled but locked out with no fresh peer
          heartbeat — runs accepted by this process may hang; loud state
        - starting: worker thread not (yet) in a settled state
        """
        with self._state_lock:
            lock_held = bool(self._lock_held)
            refused = bool(self._lock_refused_flag)
            holder_pid = self._lock_holder_pid
            acquired_at = self._lock_acquired_at
            takeover_at = self._takeover_requested_at
            yielded_to = self._yielded_to_pid
            last_error = self._last_lock_error
            loop_running = bool(self._loop_running)

        thread_alive = bool(self._thread is not None and self._thread.is_alive())
        heartbeat_age = self._lock_heartbeat_age_s()
        if holder_pid is None and not lock_held:
            # Opportunistic peer visibility (e.g. split mode: this API process
            # never contends for the lock but operators still want to see the
            # dedicated runner process on /api/health).
            holder_pid = self._read_lock_holder_pid()
        holder_alive = self._pid_alive(holder_pid) if (holder_pid and not lock_held) else None
        heartbeat_fresh = heartbeat_age is not None and heartbeat_age <= float(self._lock_stale_after_s)

        if not self._enable:
            status = "disabled"
        elif lock_held and loop_running:
            status = "active"
        elif refused and heartbeat_fresh:
            status = "standby_peer_active"
        elif refused:
            status = "degraded_no_ticker"
        else:
            status = "starting" if thread_alive else "inactive"

        out: Dict[str, Any] = {
            "enabled": bool(self._enable),
            "status": status,
            "active": bool(lock_held and loop_running),
            "thread_alive": thread_alive,
            "lock_held": lock_held,
            "lock_refused": refused,
            "lock_holder_pid": holder_pid,
            "lock_holder_alive": holder_alive,
            "lock_heartbeat_age_s": round(heartbeat_age, 3) if heartbeat_age is not None else None,
            "lock_stale_after_s": float(self._lock_stale_after_s),
            "lock_acquired_at": acquired_at,
            "takeover_requested_at": takeover_at,
            "yielded_to_pid": yielded_to,
            "pid": os.getpid(),
        }
        if last_error:
            out["lock_error"] = last_error
        return out

    def inactive_warning(self) -> Optional[str]:
        """Loud, honest warning when runs accepted here may not be ticked.

        Returns a message ONLY in the dangerous state: runner enabled, lock
        refused, and no fresh holder heartbeat proving someone else ticks this
        data_dir. A live co-worker with a fresh heartbeat (legit multi-worker /
        split deployment) stays quiet — runs are ticked by the peer.
        """
        if not self._enable:
            return None
        st = self.runner_status()
        if st.get("status") != "degraded_no_ticker":
            return None
        holder = st.get("lock_holder_pid")
        age = st.get("lock_heartbeat_age_s")
        return (
            "gateway runner is NOT ticking runs in this process: singleton lock "
            f"{self._singleton_lock_path.name} is held by pid {holder} "
            f"(heartbeat {'unknown' if age is None else f'{age}s old'}); runs may hang until the "
            "lock holder yields or is stopped. See /api/health runner status."
        )

    # ---------------------------------------------------------------------
    # Main loop
    # ---------------------------------------------------------------------

    def _run(self) -> None:
        """Worker thread: acquire (with retry + one-shot takeover) then loop.

        Lifecycle: acquire-retry phase -> tick loop (heartbeats the lock file,
        watches for takeover requests) -> on yield: drain in-flight ticks,
        release the flock, grace-sleep, back to acquire-retry (passive: a
        yielded/standby runner re-acquires only a FREE lock; it never requests
        takeover — that right belongs to newly-starting processes).
        """
        retry_interval = min(1.0, max(0.1, float(self._cfg.poll_interval_s or 0.25)))
        dead_holder_logged: Optional[int] = None
        while not self._stop.is_set():
            if not self._acquire_singleton_lock():
                holder = self._read_lock_holder_pid()
                with self._state_lock:
                    ever_held = self._lock_acquired_at is not None
                if holder is not None and holder != os.getpid() and self._pid_alive(holder) and not ever_held:
                    # Takeover is exclusively a FRESH process's right ("newest
                    # wins"). A runner that ever held the lock and later lost
                    # it (it yielded) must stand by passively, otherwise the
                    # yielded holder immediately steals the lock back and the
                    # two processes ping-pong ownership forever.
                    self._request_takeover_once()
                elif holder is not None and not self._pid_alive(holder) and holder != dead_holder_logged:
                    # Dead holder: flock auto-frees on process death, so the
                    # next retry normally wins. Reaching this branch while the
                    # lock stays refused means the lock CONTENT is stale/lying
                    # (e.g. inherited fd) — log once per holder, keep retrying.
                    dead_holder_logged = holder
                    logger.warning(
                        "GatewayRunner: lock %s refused but recorded holder pid %s is dead; retrying acquisition",
                        self._singleton_lock_path,
                        holder,
                    )
                self._stop.wait(timeout=retry_interval)
                continue

            # Acquired: we own ticking until we stop or yield. Clear the
            # takeover file UNCONDITIONALLY (adversary find, 2026-07-11): a
            # stale request naming a REUSED-alive pid (a leftover file plus a
            # reboot/pid-reshuffle) would otherwise make every loop iteration
            # yield to a process that never actually requested — drain,
            # release, re-acquire the freed lock, yield again: a silent
            # permanent yield-loop (the original incident, invisibly). The
            # acquirer has won the kernel lock; any pending request is moot
            # (a peer that still wants takeover is refused and re-observes a
            # live ticker, reporting standby_peer_active, never degraded). No
            # legitimate handshake is harmed: a live holder never re-acquires
            # between a requester's write and its own yield, so nothing clears
            # a fresh request out from under the handshake.
            self._clear_takeover_request(only_pid=None)
            logger.info("GatewayRunner started (base_dir=%s)", self._base_dir)
            yielded = self._loop()
            if not yielded:
                return
            # Yield path: hand the lock to the requesting process and drop to
            # passive standby. Grace-sleep so the requester (retrying at
            # <=1s cadence) acquires before we re-attempt.
            self._drain_inflight()
            self._release_singleton_lock()
            with self._state_lock:
                target = self._yielded_to_pid
            logger.warning(
                "GatewayRunner: yielded singleton lock %s to requesting pid %s; standing by",
                self._singleton_lock_path,
                target,
            )
            self._stop.wait(timeout=max(2.0 * retry_interval, 1.0))

    def _loop(self) -> bool:
        """Tick loop while holding the lock. Returns True when yielding to a takeover."""
        with self._state_lock:
            self._loop_running = True
        try:
            cursor = int(self._cursor_store.load() or 0)
            while not self._stop.is_set():
                # Heartbeat: prove to other processes that this holder is alive
                # AND polling (a wedged holder stops heartbeating and readers
                # report degraded_no_ticker instead of trusting the flock).
                try:
                    os.utime(self._singleton_lock_path, None)
                except Exception:
                    pass
                if self._takeover_yield_requested():
                    return True
                try:
                    cursor = self._poll_commands(cursor)
                except Exception as e:
                    logger.exception("GatewayRunner command poll error: %s", e)
                try:
                    if self._scan_pass_due():
                        self._schedule_ticks()
                except Exception as e:
                    logger.exception("GatewayRunner tick scheduling error: %s", e)
                self._stop.wait(timeout=float(self._cfg.poll_interval_s or 0.25))
            return False
        finally:
            with self._state_lock:
                self._loop_running = False

    def _poll_commands(self, cursor: int) -> int:
        items, next_cursor = self._command_store.list_after(after=int(cursor or 0), limit=int(self._cfg.command_batch_limit))
        if not items:
            return int(cursor or 0)

        cur = int(cursor or 0)
        for rec in items:
            try:
                self._apply_command(rec)
            except Exception as e:
                # Durable inbox: we advance cursor even if a command fails so it does not block the stream.
                logger.exception("GatewayRunner failed applying command %s: %s", rec.command_id, e)
            cur = max(cur, int(rec.seq or cur))
            # Persist after each command for restart safety (at-least-once acceptance).
            try:
                self._cursor_store.save(cur)
            except Exception:
                # A failing cursor save means a restart REPLAYS commands from
                # the stale cursor (at-least-once becomes visibly-more-than-
                # once). The loop must survive it, but silence made a full
                # disk / permission break invisible until the replay incident
                # (backlog 0070 exception audit).
                logger.exception(
                    "GatewayRunner: failed to persist command cursor %s to %s "
                    "(restart will replay commands from the last saved cursor)",
                    cur,
                    self._base_dir / "commands_cursor.json",
                )
        # A processed command may have changed run state (pause/resume/cancel/
        # emit_event all write runs) — force the next scheduling pass so its
        # effects tick without waiting for a fingerprint probe.
        self._scan_force = True
        return max(int(next_cursor or 0), cur)

    # ---------------------------------------------------------------------
    # Scan gate (see file_store_fingerprint for the incident this exists for)
    # ---------------------------------------------------------------------

    def _resolve_scan_base(self) -> Optional[Path]:
        if self._scan_gate_base is _UNRESOLVED:
            try:
                self._scan_gate_base = _file_store_base(self.run_store)
            except Exception:
                self._scan_gate_base = None
        base = self._scan_gate_base
        return base if isinstance(base, Path) else None

    def _scan_pass_due(self) -> bool:
        """Whether this iteration should run the (expensive) scheduling scans.

        True unconditionally on non-file stores (indexed scans are cheap and
        a fileless fingerprint would be constant — skipping forever). On file
        stores: forced passes (first run, post-command), due wait deadlines,
        and fingerprint changes scan; a QUIET store skips, probing at most
        every `scan_gate_idle_interval_s`.
        """
        base = self._resolve_scan_base()
        if base is None:
            return True
        if self._scan_force:
            self._scan_force = False
            self._scan_last_probe = 0.0  # re-probe promptly after the pass
            return True
        now = time.time()
        if self._next_due_epoch is not None and now >= float(self._next_due_epoch):
            return True
        if (now - self._scan_last_probe) < max(0.05, float(self._cfg.scan_gate_idle_interval_s)):
            return False
        self._scan_last_probe = now
        fp = file_store_fingerprint(base)
        if fp != self._scan_fingerprint:
            # Captured BEFORE the pass runs: any write landing during the
            # pass yields a different fingerprint at the next probe, so a
            # change can delay a scan by one probe but never suppress one.
            self._scan_fingerprint = fp
            return True
        return False

    def _note_next_due(self, waiting_runs: Any) -> None:
        """Record the earliest FUTURE wait deadline as an epoch timestamp so
        the scan gate wakes for it by TIME (a deadline passing changes no
        bytes on disk — the fingerprint alone would sleep through it)."""
        horizon: Optional[float] = None
        truncated = False
        try:
            runs = list(waiting_runs or [])
        except Exception:
            runs = []
        if len(runs) >= int(self._cfg.run_scan_limit):
            # The list may be truncated: an unseen deadline could be earlier
            # than anything we saw. Degrade to periodic scanning, never skip.
            truncated = True
        now = time.time()
        for r in runs:
            wait = getattr(r, "waiting", None)
            until = getattr(wait, "until", None) if wait is not None else None
            if not until:
                continue
            epoch = _epoch_from_iso(str(until))
            if epoch is None:
                truncated = True  # unparseable deadline: scan periodically
                continue
            if epoch <= now:
                horizon = now  # already due; scan next pass
                break
            horizon = epoch if horizon is None else min(horizon, epoch)
        if truncated and horizon is None:
            horizon = now + max(0.05, float(self._cfg.scan_gate_idle_interval_s))
        self._next_due_epoch = horizon

    def _schedule_ticks(self) -> None:
        list_runs = getattr(self.run_store, "list_runs", None)
        if callable(list_runs):
            runs = list_runs(status=RunStatus.RUNNING, limit=int(self._cfg.run_scan_limit))
        else:
            runs = []

        list_due = getattr(self.run_store, "list_due_wait_until", None)
        if callable(list_due):
            try:
                due = list_due(now_iso=utc_now_iso(), limit=int(self._cfg.run_scan_limit))
            except Exception:
                due = []
        else:
            due = []

        def _is_gateway_owned(run: Any) -> bool:
            return bool(getattr(run, "actor_id", None) == "gateway")

        for r in list(runs or []) + list(due or []):
            rid = getattr(r, "run_id", None)
            if not isinstance(rid, str) or not rid:
                continue
            if not _is_gateway_owned(r):
                continue
            self._submit_tick(rid)

        # ONE unfiltered WAITING fetch serves both the scan gate's deadline
        # horizon and the repair pass below (on a file store each filtered
        # query is a full parse — three per poll was the c2394 burn). The
        # unfiltered list truncates at run_scan_limit like the filtered one
        # did; _note_next_due degrades to periodic scanning when truncated.
        waiting_all: list = []
        if callable(list_runs):
            try:
                waiting_all = list(list_runs(status=RunStatus.WAITING, limit=int(self._cfg.run_scan_limit)) or [])
            except Exception:
                waiting_all = []
        self._note_next_due(waiting_all)

        # Best-effort recovery: if we restart after a child run reaches a terminal state,
        # parents blocked on WAITING(SUBWORKFLOW) can remain stuck because we don't tick
        # terminal child runs. Detect such cases and resume parents.
        try:
            self._repair_terminal_subworkflow_waits(waiting=waiting_all)
        except Exception:
            # The pass retries next poll, but a repeatedly-failing repair is
            # exactly the "parent stuck forever on a finished child" incident
            # this pass exists to prevent — it must be visible (0070 audit).
            logger.exception("GatewayRunner: terminal-subworkflow wait repair pass failed (will retry next poll)")

    def _repair_terminal_subworkflow_waits(self, waiting: Any = None) -> None:
        if waiting is None:
            list_runs = getattr(self.run_store, "list_runs", None)
            if not callable(list_runs):
                return
            try:
                waiting = list_runs(status=RunStatus.WAITING, wait_reason=WaitReason.SUBWORKFLOW, limit=int(self._cfg.run_scan_limit))
            except TypeError:
                # Older/alternate stores may not support wait_reason filtering.
                waiting = list_runs(status=RunStatus.WAITING, limit=int(self._cfg.run_scan_limit))
            except Exception:
                waiting = []

        for r in waiting or []:
            # Only repair gateway-owned run trees.
            if getattr(r, "actor_id", None) != "gateway":
                continue
            wait = getattr(r, "waiting", None)
            if wait is None or getattr(wait, "reason", None) != WaitReason.SUBWORKFLOW:
                continue
            details = getattr(wait, "details", None)
            if not isinstance(details, dict):
                continue
            sub_run_id = details.get("sub_run_id")
            if not isinstance(sub_run_id, str) or not sub_run_id.strip():
                continue
            child = self.run_store.load(sub_run_id.strip())
            if child is None:
                continue

            st = getattr(child, "status", None)
            if st not in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
                continue

            child_out_raw: Any = getattr(child, "output", None)
            if isinstance(child_out_raw, dict):
                child_out: Dict[str, Any] = dict(child_out_raw)
            else:
                child_out = {"result": child_out_raw}

            if st != RunStatus.COMPLETED:
                child_out.setdefault("success", False)
                if st == RunStatus.CANCELLED:
                    child_out.setdefault("cancelled", True)
                err = getattr(child, "error", None)
                if isinstance(err, str) and err.strip():
                    child_out.setdefault("error", err.strip())

            try:
                runtime, wf = self._host.runtime_and_workflow_for_run(r.run_id)
            except Exception as e:
                # One unresolvable parent must not abort repair for every other
                # waiting parent behind it in this loop (the caller swallows).
                logger.debug("GatewayRunner: repair skip %s (workflow unresolvable): %s", r.run_id, e)
                continue
            payload: Dict[str, Any] = {"sub_run_id": sub_run_id.strip(), "output": child_out}
            try:
                include_traces = bool(details.get("include_traces") or details.get("includeTraces"))
            except Exception:
                include_traces = False
            if include_traces:
                try:
                    payload["node_traces"] = runtime.get_node_traces(sub_run_id.strip()) or {}
                except Exception:
                    payload["node_traces"] = {}
            try:
                runtime.resume(
                    workflow=wf,
                    run_id=r.run_id,
                    wait_key=getattr(wait, "wait_key", None),
                    payload=payload,
                    max_steps=0,
                )
            except Exception:
                # Best-effort recovery only; avoid blocking the runner loop on a single bad tree.
                continue

    def _submit_tick(self, run_id: str) -> None:
        with self._inflight_lock:
            if run_id in self._inflight:
                return
            self._inflight.add(run_id)

        def _done(_f: Any) -> None:
            with self._inflight_lock:
                self._inflight.discard(run_id)
            # A finished tick usually changed run state; force the next pass
            # so continuing runs reschedule promptly AND a tick that crashed
            # BEFORE saving (no file change for the fingerprint to see) still
            # gets its retry — the pre-gate 0.25s rescan was that retry loop.
            self._scan_force = True

        fut = self._executor.submit(self._tick_run, run_id)
        try:
            fut.add_done_callback(_done)
        except Exception:
            _done(fut)

    # ---------------------------------------------------------------------
    # Command application
    # ---------------------------------------------------------------------

    def _apply_command(self, rec: CommandRecord) -> None:
        typ = str(rec.type or "").strip().lower()
        if typ not in {"pause", "resume", "cancel", "emit_event", "update_schedule", "compact_memory", "inject_guidance"}:
            raise ValueError(f"Unknown command type '{typ}'")

        payload = dict(rec.payload or {})
        run_id = str(rec.run_id or "").strip()
        if not run_id:
            raise ValueError("Command.run_id is required")

        # pause/cancel are durability operations; apply to full run tree.
        if typ in {"pause", "cancel"}:
            self._apply_run_control(typ, run_id=run_id, payload=payload, apply_to_tree=True)
            return

        # resume can mean either:
        # - resume a paused run (no payload.payload provided)   [tree-wide]
        # - resume a WAITING run with a payload (payload.payload provided) [single run]
        if typ == "resume":
            wants_wait_resume = "payload" in payload
            self._apply_run_control(typ, run_id=run_id, payload=payload, apply_to_tree=not wants_wait_resume)
            return

        # emit_event: host-side signal -> resume matching WAIT_EVENT runs
        if typ == "emit_event":
            self._apply_emit_event(payload, default_session_id=run_id, client_id=rec.client_id)
            return

        if typ == "update_schedule":
            self._apply_update_schedule(payload, run_id=run_id, command_id=str(rec.command_id), client_id=rec.client_id)
            return

        if typ == "compact_memory":
            self._apply_compact_memory(payload, run_id=run_id, command_id=str(rec.command_id), client_id=rec.client_id)
            return

        if typ == "inject_guidance":
            self._apply_inject_guidance(payload, run_id=run_id)
            return

    def _apply_inject_guidance(self, payload: Dict[str, Any], *, run_id: str) -> None:
        """Steer a running agent through the DURABLE steer sidecar (H4, hooks plan).

        The gateway no longer writes run vars for steering: `Runtime.steer()`
        (runtime 2ce4a60) appends to the sidecar; the run's OWN tick drains
        pending steers into `_runtime.inbox` at the next iteration boundary
        (exactly-once via the run-owned `_runtime.steer_watermark`) and acks
        with an `abstract.steer_seen` ledger record. This closes the
        load→append→save loss/resurrection window the old body documented
        (backlog 0217 follow-up: DONE by adopting the single-tick-writer).

        Entity visit runs REFUSE raw steers (runtime's H5 interim guard
        raises PermissionError; the HTTP door also pre-refuses with 403) —
        the command fails loudly with the rite message, never a silent drop.

        #FALLBACK: when abstractruntime predates the sidecar, the legacy
        direct-write path applies (labeled), preserving the old semantics.
        """
        text = payload.get("guidance")
        if not isinstance(text, str) or not text.strip():
            text = payload.get("text") if isinstance(payload.get("text"), str) else None
        if not isinstance(text, str) or not text.strip():
            raise ValueError("inject_guidance requires payload.guidance (non-empty string)")
        guidance = text.strip()

        from .steering import gateway_steer_sidecar

        sidecar = gateway_steer_sidecar(self._base_dir)
        runtime = Runtime(
            run_store=self.run_store,
            ledger_store=self.ledger_store,
            artifact_store=self.artifact_store,
            steer_store=sidecar,
        )
        # Target the run and its descendants so the actual agent loop (a child run) is reached.
        targets = self._list_descendant_run_ids(runtime, run_id)
        _TERMINAL = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}

        def _is_terminal(r: Any) -> bool:
            return getattr(r, "status", None) in _TERMINAL

        if sidecar is None:
            self._apply_inject_guidance_legacy(guidance, targets=targets, run_id=run_id)
            return

        applied = 0
        found_injectable = False
        refusals: list[str] = []
        for rid in targets:
            run = self.run_store.load(rid)
            if run is None:
                continue
            vars_obj = getattr(run, "vars", None)
            if not isinstance(vars_obj, dict):
                continue
            runtime_ns = vars_obj.get("_runtime")
            if not isinstance(runtime_ns, dict):
                # Guard for runs without a _runtime namespace. NOTE (adversary
                # F7): Runtime.start seeds _runtime into EVERY run, so in
                # practice this filters nothing — parent workflow shells DO
                # receive steers and steer_seen acks (same behavior as the
                # legacy body). Kept as a cheap shape guard, not targeting.
                continue
            if _is_terminal(run):
                continue
            found_injectable = True
            try:
                runtime.steer(rid, guidance)
                applied += 1
            except PermissionError as e:
                # Entity visit run: the H5 refusal is the correct outcome and
                # must surface on the command record, not vanish.
                refusals.append(str(e))
            except ValueError:
                # Run went terminal between load and steer — not an error.
                continue
        if refusals and applied:
            # Mixed tree (adversary F7): some runs steered, some refused —
            # the command succeeds for the steerables, but the refusals must
            # not vanish silently ("never a silent drop" is H4's line).
            logger.warning(
                "inject_guidance partial refusal for '%s': %d steered, %d refused (%s)",
                run_id,
                applied,
                len(refusals),
                "; ".join(refusals),
            )
        if refusals and applied == 0:
            raise PermissionError("; ".join(refusals))
        if applied == 0 and found_injectable:
            # Injectable runs existed but all finished before the steer — not an error.
            return
        if applied == 0:
            raise KeyError(f"No inbox-bearing run found for '{run_id}' to inject guidance into")

    def _apply_inject_guidance_legacy(self, guidance: str, *, targets: list, run_id: str) -> None:
        """Pre-sidecar direct-write path (#FALLBACK, version-skew only): the
        old load→append→save with anti-resurrection re-check. Kept verbatim
        so a gateway over an older abstractruntime still steers."""
        logger.warning("#FALLBACK inject_guidance using direct run-var writes (no steer sidecar in runtime)")
        _TERMINAL = {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}

        def _is_terminal(r: Any) -> bool:
            return getattr(r, "status", None) in _TERMINAL

        applied = 0
        found_injectable = False
        for rid in targets:
            run = self.run_store.load(rid)
            if run is None:
                continue
            vars_obj = getattr(run, "vars", None)
            if not isinstance(vars_obj, dict):
                continue
            runtime_ns = vars_obj.get("_runtime")
            if not isinstance(runtime_ns, dict):
                continue
            if _is_terminal(run):
                continue
            found_injectable = True
            inbox = runtime_ns.get("inbox")
            if not isinstance(inbox, list):
                inbox = []
                runtime_ns["inbox"] = inbox
            inbox.append({"role": "system", "content": guidance})
            latest = self.run_store.load(rid)
            if latest is not None and _is_terminal(latest):
                continue
            self.run_store.save(run)
            applied += 1
        if applied == 0 and found_injectable:
            return
        if applied == 0:
            raise KeyError(f"No inbox-bearing run found for '{run_id}' to inject guidance into")

    def _apply_run_control(self, typ: str, *, run_id: str, payload: Dict[str, Any], apply_to_tree: bool) -> None:
        runtime = Runtime(run_store=self.run_store, ledger_store=self.ledger_store, artifact_store=self.artifact_store)

        reason = payload.get("reason")
        reason_str = str(reason).strip() if isinstance(reason, str) and reason.strip() else None

        targets = self._list_descendant_run_ids(runtime, run_id) if apply_to_tree else [run_id]
        for rid in targets:
            if typ == "pause":
                runtime.pause_run(rid, reason=reason_str)
            elif typ == "resume":
                # Resume WAITING runs when the client provides a durable resume payload.
                if "payload" in payload:
                    resume_payload = payload.get("payload")
                    if not isinstance(resume_payload, dict):
                        raise ValueError("resume command requires payload.payload to be an object")
                    wait_key = payload.get("wait_key") or payload.get("waitKey")
                    wait_key2 = str(wait_key).strip() if isinstance(wait_key, str) and wait_key.strip() else None
                    rt2, wf2 = self._host.runtime_and_workflow_for_run(rid)
                    rt2.resume(workflow=wf2, run_id=rid, wait_key=wait_key2, payload=resume_payload, max_steps=0)
                    continue

                # Otherwise, interpret resume as "resume paused run".
                runtime.resume_run(rid)
            else:
                runtime.cancel_run(rid, reason=reason_str or "Cancelled")

        # UX affordance for scheduled runs: resuming a paused schedule should trigger the next
        # WAIT_UNTIL immediately so the schedule "wakes up" right away.
        if typ == "resume" and apply_to_tree and "payload" not in payload:
            try:
                self._maybe_trigger_scheduled_wait_now(run_id)
            except Exception:
                pass

    def _maybe_trigger_scheduled_wait_now(self, run_id: str) -> None:
        run = self.run_store.load(str(run_id))
        if run is None:
            return

        root = run if self._is_scheduled_parent_run(run) else self._find_scheduled_root(run)
        if root is None or not self._scheduled_parent_is_recurrent(root):
            return

        waiting = getattr(root, "waiting", None)
        if getattr(root, "status", None) != RunStatus.WAITING or waiting is None:
            return
        if getattr(waiting, "reason", None) != WaitReason.UNTIL:
            return
        until = getattr(waiting, "until", None)
        if not isinstance(until, str) or not until.strip():
            return

        now = utc_now_iso()
        waiting.until = now  # type: ignore[attr-defined]
        root.updated_at = now
        self.run_store.save(root)

    def _apply_emit_event(
        self, payload: Dict[str, Any], *, default_session_id: str, client_id: Optional[str]
    ) -> Dict[str, int]:
        name = payload.get("name")
        name2 = str(name or "").strip()
        if not name2:
            raise ValueError("emit_event requires payload.name")

        scope = payload.get("scope") or "session"
        scope2 = str(scope or "session").strip().lower() or "session"
        session_id = payload.get("session_id") or payload.get("sessionId") or default_session_id
        workflow_id = payload.get("workflow_id") or payload.get("workflowId")
        run_id = payload.get("run_id") or payload.get("runId")
        event_payload = payload.get("payload")
        if isinstance(event_payload, dict):
            payload2 = dict(event_payload)
        else:
            payload2 = {"value": event_payload}

        wait_key = build_event_wait_key(
            scope=scope2,
            name=name2,
            session_id=str(session_id) if isinstance(session_id, str) and session_id else None,
            workflow_id=str(workflow_id) if isinstance(workflow_id, str) and workflow_id else None,
            run_id=str(run_id) if isinstance(run_id, str) and run_id else None,
        )

        envelope: Dict[str, Any] = {
            "event_id": payload.get("event_id") or payload.get("eventId"),
            "name": name2,
            "scope": scope2,
            "session_id": session_id,
            "payload": payload2,
            "emitted_at": payload.get("emitted_at") or payload.get("emittedAt"),
            "emitter": {"source": "external", "client_id": client_id},
        }

        # Find matching WAIT_EVENT runs and resume them.
        list_runs = getattr(self.run_store, "list_runs", None)
        if not callable(list_runs):
            return {"resumed": 0, "appended": 0}

        resumed = 0
        waiting_runs = list_runs(status=RunStatus.WAITING, wait_reason=WaitReason.EVENT, limit=10_000)
        for r in waiting_runs or []:
            if getattr(r, "waiting", None) is None:
                continue
            if getattr(r.waiting, "wait_key", None) != wait_key:
                continue
            if _is_pause_wait(getattr(r, "waiting", None), run_id=str(getattr(r, "run_id", "") or "")):
                continue
            try:
                runtime, wf = self._host.runtime_and_workflow_for_run(r.run_id)
            except Exception as e:
                # One unresolvable listener must not break event delivery to
                # every other matching WAIT_EVENT run behind it.
                logger.warning("GatewayRunner: emit_event skip %s (workflow unresolvable): %s", r.run_id, e)
                continue
            runtime.resume(workflow=wf, run_id=r.run_id, wait_key=wait_key, payload=envelope, max_steps=0)
            resumed += 1

        # Durable mailbox delivery (opt-in via payload.durable, backlog: event-inbox agents).
        #
        # Plain emit_event only reaches runs *currently parked* on the wait key; an event sent
        # while a listener is busy (mid-LLM/tool cycle) would be silently dropped. With
        # `durable: true`, the envelope is ALSO appended to the `events_inbox` run var of every
        # non-terminal run that declares the matching mailbox (`events_mailbox` var == event
        # name; str or list-of-str). Listener loops drain the inbox with a monotonic `seq`
        # cursor (append-only on this side, cursor-only on the reader side), so no
        # read-then-clear race exists between the runner thread and run ticks.
        #
        # Same concurrency posture as inject_guidance: commands apply on the runner loop
        # thread; a concurrent tick save can race the append (best-effort, re-send if needed).
        appended = 0
        if payload.get("durable") is True or str(payload.get("durable") or "").strip().lower() in {"1", "true", "yes"}:
            appended = self._deliver_durable_event(name=name2, envelope=envelope)
        # RECEIVER COUNTS (bridge adversary F2): a caller that must not lose
        # messages (the agora bridge's cursor) needs to KNOW whether anything
        # received this event — zero receivers used to look identical to
        # success.
        return {"resumed": resumed, "appended": appended}

    _EVENTS_INBOX_CAP = 500

    def _deliver_durable_event(self, *, name: str, envelope: Dict[str, Any]) -> int:
        """Append an event envelope to the `events_inbox` of mailbox-declaring
        runs. Returns the number of runs appended to. Idempotent per run by
        `event_id` (bridge adversary F4): a crash-replayed emit with the same
        event_id lands at most once per inbox — the at-least-once transport
        composes into exactly-once delivery when the producer sets stable ids."""
        list_runs = getattr(self.run_store, "list_runs", None)
        if not callable(list_runs):
            return 0

        limit = int(self._cfg.run_scan_limit)
        candidates: list[Any] = []
        try:
            candidates.extend(list_runs(status=RunStatus.RUNNING, limit=limit) or [])
        except Exception:
            pass
        try:
            candidates.extend(list_runs(status=RunStatus.WAITING, limit=limit) or [])
        except Exception:
            pass

        def _declares_mailbox(vars_obj: Any) -> bool:
            if not isinstance(vars_obj, dict):
                return False
            declared = vars_obj.get("events_mailbox")
            if isinstance(declared, str):
                return declared.strip() == name
            if isinstance(declared, list):
                return any(isinstance(m, str) and m.strip() == name for m in declared)
            return False

        appended = 0
        event_id = str(envelope.get("event_id") or "").strip()
        seen: set[str] = set()
        for r in candidates:
            rid = str(getattr(r, "run_id", "") or "")
            if not rid or rid in seen:
                continue
            seen.add(rid)
            if not _declares_mailbox(getattr(r, "vars", None)):
                continue

            # Re-load fresh state: the resume pass above may have just saved this run.
            run = self.run_store.load(rid)
            if run is None:
                continue
            vars_obj = getattr(run, "vars", None)
            if not isinstance(vars_obj, dict) or not _declares_mailbox(vars_obj):
                continue

            inbox = vars_obj.get("events_inbox")
            if not isinstance(inbox, list):
                inbox = []
                vars_obj["events_inbox"] = inbox

            # event_id idempotency (F4): an at-least-once producer re-sending
            # the same event lands once. Bounded scan — the inbox is capped.
            if event_id and any(
                isinstance(e, dict) and str(e.get("event_id") or "") == event_id for e in inbox
            ):
                appended += 1  # already delivered = received, not a zero-receiver signal
                continue

            try:
                seq = int(vars_obj.get("events_inbox_seq") or 0) + 1
            except Exception:
                seq = 1
            vars_obj["events_inbox_seq"] = seq

            entry = dict(envelope)
            entry["seq"] = seq
            inbox.append(entry)

            # Bounded mailbox: drop-oldest beyond the cap, visibly counted.
            if len(inbox) > self._EVENTS_INBOX_CAP:
                overflow = len(inbox) - self._EVENTS_INBOX_CAP
                del inbox[:overflow]
                try:
                    dropped = int(vars_obj.get("events_inbox_dropped") or 0) + overflow
                except Exception:
                    dropped = overflow
                vars_obj["events_inbox_dropped"] = dropped
                logger.warning(
                    "GatewayRunner: events_inbox overflow for run %s (mailbox=%s): dropped %s oldest",
                    rid,
                    name,
                    overflow,
                )

            run.updated_at = utc_now_iso()
            self.run_store.save(run)
            appended += 1
        return appended

    def _list_descendant_run_ids(self, runtime: Runtime, root_run_id: str) -> list[str]:
        """Return root + descendants (best-effort)."""
        out: list[str] = []
        queue: list[str] = [root_run_id]
        seen: set[str] = set()
        list_children = getattr(runtime.run_store, "list_children", None)
        while queue:
            rid = queue.pop(0)
            if rid in seen:
                continue
            seen.add(rid)
            out.append(rid)
            if callable(list_children):
                try:
                    children = list_children(parent_run_id=rid) or []
                except Exception:
                    children = []
                for c in children:
                    cid = getattr(c, "run_id", None)
                    if isinstance(cid, str) and cid and cid not in seen:
                        queue.append(cid)
        return out

    # ---------------------------------------------------------------------
    # Tick execution + subworkflow parent resumption
    # ---------------------------------------------------------------------

    def _clear_resolution_failure(self, run_id: str) -> None:
        with self._resolution_failures_lock:
            self._resolution_failures.pop(run_id, None)

    def _note_resolution_failure(self, run_id: str, exc: Exception) -> None:
        with self._resolution_failures_lock:
            count = int(self._resolution_failures.get(run_id, 0)) + 1
            self._resolution_failures[run_id] = count

        limit = max(1, int(self._cfg.workflow_resolution_failure_limit or 40))
        if count == 1:
            # One visible warning per failure streak; per-poll repeats stay at
            # debug (warning-noise rule: the runner re-submits every 0.25s).
            logger.warning(
                "GatewayRunner: cannot resolve workflow for run %s (%s: %s); "
                "run stays RUNNING and will be FAILED after %s consecutive failures",
                run_id,
                type(exc).__name__,
                exc,
                limit,
            )
        else:
            logger.debug("GatewayRunner: cannot build runtime for %s (attempt %s): %s", run_id, count, exc)
        if count < limit:
            return

        err = f"WorkflowResolutionError: {type(exc).__name__}: {exc} (after {count} consecutive attempts)"
        try:
            latest = self.run_store.load(run_id)
            if latest is None:
                self._clear_resolution_failure(run_id)
                return
            if getattr(latest, "status", None) != RunStatus.RUNNING:
                # Only RUNNING runs are promoted (parity with the tick-exception
                # path). Reset the counter so non-RUNNING due-waits re-accrue
                # instead of re-attempting promotion on every poll.
                self._clear_resolution_failure(run_id)
                return
            latest.status = RunStatus.FAILED
            latest.error = err
            latest.updated_at = utc_now_iso()
            self.run_store.save(latest)
            logger.error("GatewayRunner: failing run %s — workflow unresolvable: %s", run_id, err)
            try:
                rec = StepRecord.start(
                    run=latest,
                    node_id=str(getattr(latest, "current_node", None) or "runtime"),
                    effect=None,
                    idempotency_key=f"system:workflow_resolution:{run_id}",
                )
                rec.finish_failure(err)
                self.ledger_store.append(rec)
            except Exception:
                logger.exception("GatewayRunner: failed to append workflow_resolution record for %s", run_id)
            self._clear_resolution_failure(run_id)
        except Exception:
            logger.exception("GatewayRunner: failed to promote unresolvable run %s to FAILED", run_id)

    def _tick_run(self, run_id: str) -> None:
        try:
            runtime, wf = self._host.runtime_and_workflow_for_run(run_id)
        except Exception as e:
            # A RUNNING run whose workflow cannot be resolved (deleted draft,
            # tombstoned catalog version, principal-scoped bundle not loaded)
            # is re-submitted every poll and would otherwise spin RUNNING
            # forever with zero ledger and no error — the same user-visible
            # symptom as a dead runner. Count consecutive failures and promote
            # the run to FAILED (with a ledger record) once the limit is hit.
            self._note_resolution_failure(run_id, e)
            return
        self._clear_resolution_failure(run_id)

        try:
            state = runtime.tick(workflow=wf, run_id=run_id, max_steps=int(self._cfg.tick_max_steps or 100))
        except Exception as e:
            # Never leave runs stuck in RUNNING due to an unhandled exception.
            #
            # Rationale: VisualFlow node planning can raise (e.g. missing optional deps),
            # and effects can raise before the runtime has a chance to persist status.
            # In gateway mode, a stuck RUNNING run can deadlock parents waiting on
            # SUBWORKFLOW completion (KG ingest).
            logger.exception("GatewayRunner: tick failed for %s", run_id)
            err = f"{type(e).__name__}: {e}"
            try:
                latest = runtime.run_store.load(run_id)
                if latest is None:
                    return
                if getattr(latest, "status", None) == RunStatus.RUNNING:
                    latest.status = RunStatus.FAILED
                    latest.error = err
                    latest.updated_at = utc_now_iso()
                    runtime.run_store.save(latest)
                    try:
                        rec = StepRecord.start(
                            run=latest,
                            node_id=str(getattr(latest, "current_node", None) or "runtime"),
                            effect=None,
                            idempotency_key=f"system:tick_exception:{run_id}",
                        )
                        rec.finish_failure(err)
                        self.ledger_store.append(rec)
                    except Exception:
                        logger.exception("GatewayRunner: failed to append tick_exception record for %s", run_id)
                state = latest
            except Exception:
                # Load/save failed AFTER the tick exception: the run stays
                # RUNNING and will re-tick, but the failed promotion must not
                # be silent — this is the path that turns a persistent store
                # fault into an invisible infinite retry loop (0070 audit).
                logger.exception(
                    "GatewayRunner: failed to promote run %s to FAILED after tick exception (run stays RUNNING)",
                    run_id,
                )
                return

        # Auto-compaction for scheduled workflows (best-effort).
        try:
            self._maybe_auto_compact(state)
        except Exception:
            logger.debug("GatewayRunner: auto-compact pass failed for %s", run_id, exc_info=True)

        # If this run completed, it may unblock a parent WAITING(SUBWORKFLOW).
        if getattr(state, "status", None) in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
            try:
                child_out_raw: Any = getattr(state, "output", None)
                child_out: Dict[str, Any]
                if isinstance(child_out_raw, dict):
                    child_out = dict(child_out_raw)
                else:
                    child_out = {"result": child_out_raw}

                if getattr(state, "status", None) != RunStatus.COMPLETED:
                    # Preserve a stable shape so visual subflow nodes can proceed.
                    child_out.setdefault("success", False)
                    if getattr(state, "status", None) == RunStatus.CANCELLED:
                        child_out.setdefault("cancelled", True)
                    err = getattr(state, "error", None)
                    if isinstance(err, str) and err.strip():
                        child_out.setdefault("error", err.strip())

                self._resume_subworkflow_parents(child_run_id=run_id, child_output=child_out)
            except Exception:
                # The repair pass retries stuck parents on later polls, so
                # this is recoverable — but a parent left WAITING on a
                # finished child is durability-relevant and must be seen.
                logger.exception(
                    "GatewayRunner: failed resuming parents of terminal child %s (repair pass will retry)",
                    run_id,
                )

    def _resume_subworkflow_parents(self, *, child_run_id: str, child_output: Dict[str, Any]) -> None:
        list_runs = getattr(self.run_store, "list_runs", None)
        if not callable(list_runs):
            return
        waiting = list_runs(status=RunStatus.WAITING, limit=2000)
        for r in waiting or []:
            wait = getattr(r, "waiting", None)
            if wait is None or getattr(wait, "reason", None) != WaitReason.SUBWORKFLOW:
                continue
            details = getattr(wait, "details", None)
            if not isinstance(details, dict) or details.get("sub_run_id") != child_run_id:
                continue
            if _is_pause_wait(wait, run_id=str(getattr(r, "run_id", "") or "")):
                continue
            try:
                runtime, wf = self._host.runtime_and_workflow_for_run(r.run_id)
            except Exception as e:
                # One unresolvable parent must not abort resumption of other
                # parents waiting on the same child (the caller swallows).
                logger.warning("GatewayRunner: parent-resume skip %s (workflow unresolvable): %s", r.run_id, e)
                continue
            payload: Dict[str, Any] = {"sub_run_id": child_run_id, "output": child_output}
            try:
                include_traces = bool(details.get("include_traces") or details.get("includeTraces"))
            except Exception:
                include_traces = False
            if include_traces:
                try:
                    payload["node_traces"] = runtime.get_node_traces(child_run_id) or {}
                except Exception:
                    payload["node_traces"] = {}
            runtime.resume(
                workflow=wf,
                run_id=r.run_id,
                wait_key=getattr(wait, "wait_key", None),
                payload=payload,
                max_steps=0,
            )

    # ---------------------------------------------------------------------
    # Scheduled workflow commands
    # ---------------------------------------------------------------------

    _INTERVAL_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)\s*$", re.IGNORECASE)
    _UNIT_SECONDS: Dict[str, float] = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}

    def _is_scheduled_parent_run(self, run: Any) -> bool:
        wid = getattr(run, "workflow_id", None)
        if isinstance(wid, str) and wid.startswith("scheduled:"):
            return True
        vars_obj = getattr(run, "vars", None)
        meta = vars_obj.get("_meta") if isinstance(vars_obj, dict) else None
        schedule = meta.get("schedule") if isinstance(meta, dict) else None
        if isinstance(schedule, dict) and schedule.get("kind") == "scheduled_run":
            return True
        return False

    def _scheduled_parent_is_recurrent(self, run: Any) -> bool:
        vars_obj = getattr(run, "vars", None)
        meta = vars_obj.get("_meta") if isinstance(vars_obj, dict) else None
        schedule = meta.get("schedule") if isinstance(meta, dict) else None
        if not isinstance(schedule, dict):
            return False
        interval = schedule.get("interval")
        return isinstance(interval, str) and interval.strip() != ""

    def _find_scheduled_root(self, run: Any) -> Optional[Any]:
        """Return the scheduled parent run (root) for a run tree, if any."""
        cur = run
        seen: set[str] = set()
        while True:
            rid = getattr(cur, "run_id", None)
            if isinstance(rid, str) and rid:
                if rid in seen:
                    break
                seen.add(rid)
            parent_id = getattr(cur, "parent_run_id", None)
            if not isinstance(parent_id, str) or not parent_id.strip():
                return cur if self._is_scheduled_parent_run(cur) else None
            parent = self.run_store.load(parent_id.strip())
            if parent is None:
                return None
            cur = parent
        return None

    def _parse_interval_seconds(self, raw: str) -> Optional[float]:
        s = str(raw or "").strip()
        if not s:
            return None
        m = self._INTERVAL_RE.match(s)
        if not m:
            # ISO timestamps are accepted by on_schedule but are one-shot; treat as non-interval.
            return None
        amount = float(m.group(1))
        unit = str(m.group(2)).lower()
        return float(amount) * float(self._UNIT_SECONDS.get(unit, 1.0))

    def _mutate_schedule_interval_in_visualflow(self, raw: Dict[str, Any], *, interval: str) -> bool:
        nodes = raw.get("nodes")
        if not isinstance(nodes, list):
            return False
        changed = False
        for n in nodes:
            if not isinstance(n, dict):
                continue
            if str(n.get("id") or "") != "wait_interval":
                continue
            data = n.get("data")
            if not isinstance(data, dict):
                data = {}
                n["data"] = data
            event_cfg = data.get("eventConfig")
            if not isinstance(event_cfg, dict):
                event_cfg = {}
                data["eventConfig"] = event_cfg
            event_cfg["schedule"] = str(interval)
            changed = True
        return changed

    def _apply_update_schedule(
        self, payload: Dict[str, Any], *, run_id: str, command_id: str, client_id: Optional[str]
    ) -> None:
        del client_id
        requested_run_id = str(run_id or "").strip()
        raw_interval = payload.get("interval")
        if raw_interval is None:
            raw_interval = payload.get("schedule")
        interval = str(raw_interval or "").strip()
        if not interval:
            raise ValueError("update_schedule requires payload.interval")

        # Validate interval is a relative duration (not an ISO timestamp).
        interval_s = self._parse_interval_seconds(interval)
        if interval_s is None or interval_s <= 0:
            raise ValueError("update_schedule interval must be a relative duration like '20m', '1h', '0.5s'")

        parent = self.run_store.load(run_id)
        if parent is None:
            raise KeyError(f"Run '{run_id}' not found")
        if not self._is_scheduled_parent_run(parent):
            root = self._find_scheduled_root(parent)
            if root is None:
                raise ValueError("update_schedule is only supported for scheduled runs (or runs inside a scheduled run tree)")
            parent = root
            run_id = str(getattr(parent, "run_id", run_id))
        if not self._scheduled_parent_is_recurrent(parent):
            raise ValueError("update_schedule requires a recurrent scheduled run (interval must be set)")

        workflow_id = getattr(parent, "workflow_id", None)
        if not isinstance(workflow_id, str) or not workflow_id.strip():
            raise ValueError("Scheduled run missing workflow_id")

        # Update durable schedule metadata on the run (for UI).
        vars_obj = getattr(parent, "vars", None)
        if not isinstance(vars_obj, dict):
            vars_obj = {}
            parent.vars = vars_obj  # type: ignore[attr-defined]
        meta = vars_obj.get("_meta")
        if not isinstance(meta, dict):
            meta = {}
            vars_obj["_meta"] = meta
        sched = meta.get("schedule")
        if not isinstance(sched, dict):
            sched = {}
            meta["schedule"] = sched
        sched["interval"] = interval
        sched["updated_at"] = utc_now_iso()
        parent.updated_at = utc_now_iso()
        self.run_store.save(parent)

        # Update the persisted dynamic wrapper flow + registry entry (wait_interval node).
        load_raw = getattr(self._host, "load_dynamic_visualflow", None)
        upsert = getattr(self._host, "upsert_dynamic_visualflow", None)
        if not callable(load_raw) or not callable(upsert):
            raise RuntimeError("Host does not support editing dynamic workflows (load_dynamic_visualflow/upsert_dynamic_visualflow)")
        raw_flow = load_raw(workflow_id)
        if raw_flow is None:
            raise RuntimeError(f"Dynamic wrapper flow not found on disk for workflow_id={workflow_id}")
        if not self._mutate_schedule_interval_in_visualflow(raw_flow, interval=interval):
            raise RuntimeError("Failed to locate wait_interval node in scheduled wrapper flow")

        # Re-register so subsequent ticks use the updated spec.
        upsert(raw_flow, persist=True)

        # Optional: if currently blocked on the interval wait, recompute the concrete until timestamp.
        apply_immediately = payload.get("apply_immediately")
        apply_immediately_flag = True if apply_immediately is None else bool(apply_immediately)
        waiting = getattr(parent, "waiting", None)
        if (
            apply_immediately_flag
            and getattr(parent, "status", None) == RunStatus.WAITING
            and waiting is not None
            and getattr(waiting, "reason", None) == WaitReason.UNTIL
            and str(getattr(parent, "current_node", "") or "") == "wait_interval"
        ):
            now = datetime.datetime.now(datetime.timezone.utc)
            until = (now + datetime.timedelta(seconds=float(interval_s))).isoformat()
            waiting.until = until  # type: ignore[attr-defined]
            parent.updated_at = utc_now_iso()
            self.run_store.save(parent)

        # Best-effort observability marker.
        try:
            runtime_ns = vars_obj.get("_runtime")
            if not isinstance(runtime_ns, dict):
                runtime_ns = {}
                vars_obj["_runtime"] = runtime_ns
            runtime_ns["last_schedule_update"] = {
                "command_id": command_id,
                "interval": interval,
                "updated_at": utc_now_iso(),
                "requested_run_id": requested_run_id,
                "scheduled_root_run_id": str(run_id),
            }
            self.run_store.save(parent)
        except Exception:
            # Marker-only write: the schedule change itself already persisted.
            logger.debug("GatewayRunner: failed to record last_schedule_update marker for %s", run_id, exc_info=True)

    def _resolve_compaction_target_run_id(self, root_run_id: str) -> Optional[str]:
        """Pick the best-effort run_id whose vars contain context.messages to compact."""

        def _has_messages(r: Any) -> bool:
            vars_obj = getattr(r, "vars", None)
            ctx = vars_obj.get("context") if isinstance(vars_obj, dict) else None
            msgs = ctx.get("messages") if isinstance(ctx, dict) else None
            return isinstance(msgs, list) and len(msgs) > 0

        cur = self.run_store.load(root_run_id)
        if cur is None:
            return None
        if _has_messages(cur):
            return str(getattr(cur, "run_id"))

        # Prefer following active SUBWORKFLOW wait chains (deepest active run).
        seen: set[str] = set()
        while True:
            rid = str(getattr(cur, "run_id", "") or "")
            if not rid or rid in seen:
                break
            seen.add(rid)
            waiting = getattr(cur, "waiting", None)
            details = getattr(waiting, "details", None) if waiting is not None else None
            sub_id = details.get("sub_run_id") if isinstance(details, dict) else None
            if not isinstance(sub_id, str) or not sub_id.strip():
                break
            nxt = self.run_store.load(sub_id.strip())
            if nxt is None:
                break
            cur = nxt
            if _has_messages(cur):
                return str(getattr(cur, "run_id"))

        # Fallback: compact most recent descendant that has messages (best-effort).
        list_children = getattr(self.run_store, "list_children", None)
        if not callable(list_children):
            return None
        try:
            children = list_children(parent_run_id=root_run_id) or []
        except Exception:
            children = []
        if not children:
            return None

        def _ts(r: Any) -> str:
            return str(getattr(r, "updated_at", None) or getattr(r, "created_at", None) or "")

        for child in sorted(children, key=_ts, reverse=True):
            cid = getattr(child, "run_id", None)
            if not isinstance(cid, str) or not cid:
                continue
            target = self._resolve_compaction_target_run_id(cid)
            if target:
                return target
        return None

    def _apply_compact_memory(
        self, payload: Dict[str, Any], *, run_id: str, command_id: str, client_id: Optional[str]
    ) -> None:
        del client_id
        requested_run_id = str(run_id or "").strip()
        parent = self.run_store.load(run_id)
        if parent is None:
            raise KeyError(f"Run '{run_id}' not found")
        if not self._is_scheduled_parent_run(parent):
            root = self._find_scheduled_root(parent)
            if root is None:
                raise ValueError("compact_memory is only supported for scheduled runs (or runs inside a scheduled run tree)")
            parent = root
            run_id = str(getattr(parent, "run_id", run_id))

        target_run_id = payload.get("target_run_id") or payload.get("targetRunId")
        if isinstance(target_run_id, str) and target_run_id.strip():
            target_id = target_run_id.strip()
        else:
            target_id = self._resolve_compaction_target_run_id(run_id) or ""
        if not target_id:
            raise RuntimeError("No compactable run found (no context.messages in the scheduled run tree)")

        target = self.run_store.load(target_id)
        if target is None:
            raise KeyError(f"Target run '{target_id}' not found")

        # Build effect payload.
        preserve_recent_raw = payload.get("preserve_recent")
        if preserve_recent_raw is None:
            preserve_recent_raw = payload.get("preserveRecent")
        try:
            preserve_recent = int(preserve_recent_raw) if preserve_recent_raw is not None else 6
        except Exception:
            preserve_recent = 6
        if preserve_recent < 0:
            preserve_recent = 0
        compression_mode = str(payload.get("compression_mode") or payload.get("compressionMode") or "standard").strip().lower()
        if compression_mode not in {"light", "standard", "heavy"}:
            compression_mode = "standard"
        focus = payload.get("focus")
        focus_text = str(focus).strip() if isinstance(focus, str) and focus.strip() else None

        eff_payload: Dict[str, Any] = {
            "preserve_recent": preserve_recent,
            "compression_mode": compression_mode,
        }
        if focus_text is not None:
            eff_payload["focus"] = focus_text

        # Execute the memory_compact effect as an out-of-band action on the target run.
        runtime = Runtime(run_store=self.run_store, ledger_store=self.ledger_store, artifact_store=self.artifact_store)
        # Enable subworkflow lookups in MEMORY_COMPACT (it spawns a small LLM sub-run).
        try:
            runtime.set_workflow_registry(getattr(self._host, "workflow_registry", None))
        except Exception:
            pass

        eff = Effect(type=EffectType.MEMORY_COMPACT, payload=eff_payload, result_key="_temp.command.compact_memory")
        idem = f"command:compact_memory:{command_id}"
        outcome = runtime._execute_effect_with_retry(  # type: ignore[attr-defined]
            run=target,
            node_id="compact_memory",
            effect=eff,
            idempotency_key=idem,
            default_next_node=None,
        )

        # MEMORY_COMPACT mutates run.vars but only saves when targeting a different run. When compacting
        # the target itself out-of-band, explicitly persist the updated checkpoint.
        try:
            target.updated_at = utc_now_iso()
            self.run_store.save(target)
        except Exception:
            # A lost save here silently discards the whole compaction (the
            # LLM spend happened; the compacted vars never landed) — 0070.
            logger.exception("GatewayRunner: failed to persist compacted vars for run %s", target_id)

        if getattr(outcome, "status", None) == "failed":
            raise RuntimeError(getattr(outcome, "error", None) or "compact_memory failed")

        # Best-effort observability marker (on the scheduled parent/root run).
        try:
            vars_obj = getattr(parent, "vars", None)
            if not isinstance(vars_obj, dict):
                vars_obj = {}
                parent.vars = vars_obj  # type: ignore[attr-defined]
            runtime_ns = vars_obj.get("_runtime")
            if not isinstance(runtime_ns, dict):
                runtime_ns = {}
                vars_obj["_runtime"] = runtime_ns
            runtime_ns["last_compact_memory"] = {
                "command_id": command_id,
                "updated_at": utc_now_iso(),
                "requested_run_id": requested_run_id,
                "scheduled_root_run_id": str(run_id),
                "target_run_id": str(target_id),
            }
            parent.updated_at = utc_now_iso()
            self.run_store.save(parent)
        except Exception:
            # Marker-only write: the compaction itself already persisted above.
            logger.debug("GatewayRunner: failed to record last_compact_memory marker for %s", run_id, exc_info=True)

    # ---------------------------------------------------------------------
    # Auto-compaction for scheduled workflows
    # ---------------------------------------------------------------------

    def _maybe_auto_compact(self, run: Any) -> None:
        """Auto-compact scheduled workflows when nearing context limits (best-effort)."""
        root = self._find_scheduled_root(run)
        if root is None or not self._scheduled_parent_is_recurrent(root):
            return

        vars_obj = getattr(run, "vars", None)
        if not isinstance(vars_obj, dict):
            return
        ctx = vars_obj.get("context")
        msgs = ctx.get("messages") if isinstance(ctx, dict) else None
        if not isinstance(msgs, list) or len(msgs) < 12:
            return

        limits = vars_obj.get("_limits")
        if not isinstance(limits, dict):
            return
        used = limits.get("estimated_tokens_used")
        if used is None or isinstance(used, bool):
            return
        try:
            used_i = int(used)
        except Exception:
            return
        if used_i <= 0:
            return

        budget = limits.get("max_input_tokens")
        if budget is None:
            budget = limits.get("max_tokens")
        try:
            budget_i = int(budget) if budget is not None else 0
        except Exception:
            budget_i = 0
        if budget_i <= 0:
            return

        pct = used_i / float(budget_i)
        if pct < 0.9:
            return

        runtime_ns = vars_obj.get("_runtime")
        if not isinstance(runtime_ns, dict):
            runtime_ns = {}
            vars_obj["_runtime"] = runtime_ns
        auto = runtime_ns.get("auto_compact")
        if not isinstance(auto, dict):
            auto = {}
            runtime_ns["auto_compact"] = auto
        last_used = auto.get("last_trigger_tokens_used")
        try:
            last_used_i = int(last_used) if last_used is not None else -1
        except Exception:
            last_used_i = -1
        if used_i <= last_used_i:
            return

        # Record guard before running to avoid thrash if compaction fails.
        auto["last_trigger_tokens_used"] = used_i
        auto["last_triggered_at"] = utc_now_iso()
        try:
            self.run_store.save(run)
        except Exception:
            # A lost guard save means the trigger can thrash (re-fire every
            # tick at the same token count) — visible, not fatal.
            logger.warning(
                "GatewayRunner: failed to persist auto-compact guard for %s (trigger may re-fire)",
                getattr(run, "run_id", "?"),
                exc_info=True,
            )

        runtime = Runtime(run_store=self.run_store, ledger_store=self.ledger_store, artifact_store=self.artifact_store)
        try:
            runtime.set_workflow_registry(getattr(self._host, "workflow_registry", None))
        except Exception:
            pass

        eff = Effect(
            type=EffectType.MEMORY_COMPACT,
            payload={"preserve_recent": 6, "compression_mode": "standard", "focus": None},
            result_key="_temp.runtime.auto_compact",
        )
        idem = f"runtime:auto_compact:{utc_now_iso()}:{used_i}"
        outcome = runtime._execute_effect_with_retry(  # type: ignore[attr-defined]
            run=run,
            node_id="auto_compact",
            effect=eff,
            idempotency_key=idem,
            default_next_node=None,
        )
        try:
            run.updated_at = utc_now_iso()
            self.run_store.save(run)
        except Exception:
            # Same class as the out-of-band compact save: losing this save
            # discards the compaction the run just paid for (0070 audit).
            logger.exception(
                "GatewayRunner: failed to persist auto-compacted vars for run %s",
                getattr(run, "run_id", "?"),
            )
        if getattr(outcome, "status", None) == "failed":
            # Best-effort: record the error for debuggability but do not fail ticking.
            try:
                auto["last_error"] = getattr(outcome, "error", None)
                auto["last_error_at"] = utc_now_iso()
                self.run_store.save(run)
            except Exception:
                logger.debug("GatewayRunner: failed to record auto-compact error marker", exc_info=True)
