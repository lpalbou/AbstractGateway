"""Background sampler: polls the gateway and keeps the ring buffers the icon,
menu and monitor window draw from.

Cadence (two daemon threads, never the GUI thread):
- FAST lane, 1 Hz: ONE call to `GET /host/metrics/live` (GPU + memory +
  paused/in-flight, cached 1 s server-side); older gateways without that
  route fall back to `/host/metrics/gpu` + `/host/metrics/memory`.
  Capabilities and step-gate facts (`/host/runner`) refresh every 5 s.
- SLOW lane, own thread, every 5 s: resident models (`/host/state`) — this
  walks every provider and may reach LM Studio/Ollama over HTTP, so it can
  never stall the icon or the graphs. The last day's runs (`GET /runs`) ride
  the same thread on their own 20 s throttle: the Workflows submenu is read at
  human speed, and a list nobody has the menu open to see is not worth a poll
  per five seconds.
- Gateway reachability: derived; three consecutive failed fast polls flip the
  state to "unreachable" (one blip never repaints the icon red). Two 401/403
  answers in a row mean the token is dead (a gateway that restarted without
  us, or another gateway on the port): polling STOPS so the auth lockout never
  trips for every other loopback client.

`snapshot()` hands the GUI an immutable copy; listeners are called on the
sampler thread and must only schedule GUI work, never do it.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from .client import GatewayClient

HISTORY_SAMPLES = 120  # 2 minutes at 1 Hz
RUNS_WINDOW_S = 24 * 3600.0  # "the last day" — the Workflows submenu's horizon
RUNS_LIMIT = 25  # what we ASK for; the window then decides what is shown


@dataclass(frozen=True)
class ModelRow:
    key: str
    name: str
    provider: str
    size_bytes: Optional[int]
    size_source: str  # "reported" | "estimated" | ""
    locked: bool
    lockable: bool
    resident: Optional[bool]
    target: Dict[str, Any]  # what /models/unload wants: {runtime_id} or {provider, model}
    modalities: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunRow:
    """One workflow run, as the Workflows submenu shows it."""

    run_id: str
    workflow_id: str
    status: str  # running | waiting | completed | failed | cancelled | ...
    steps: Optional[int]  # ledger entries — the run's turns
    duration_s: Optional[float]
    started_at: Optional[float]  # epoch seconds, for the 24 h window


@dataclass(frozen=True)
class Snapshot:
    ts: float
    gateway_state: str  # starting | running | paused | pausing | unreachable | restarting | updating | stopping
    reachable: bool
    paused: bool
    inflight_ticks: int
    paused_by: Optional[str]
    pause_reason: Optional[str]
    gpu_supported: bool
    gpu_reason: Optional[str]
    gpu_pct: Optional[float]
    gpu_name: Optional[str]
    gpu_history: tuple[Optional[float], ...]
    mem_supported: bool
    mem_reason: Optional[str]
    mem_used: Optional[int]
    mem_total: Optional[int]
    mem_pct: Optional[float]
    mem_history: tuple[Optional[float], ...]
    process_rss: Optional[int]
    process_history: tuple[Optional[float], ...]
    device_backend: Optional[str]
    models: tuple[ModelRow, ...]
    models_total_bytes: Optional[int]
    models_error: Optional[str]
    can_restart: bool
    can_shutdown: bool
    restart_block_reason: Optional[str]
    update_job_running: bool
    step_gate_supported: Optional[bool]
    version: str
    last_error: Optional[str]
    consecutive_failures: int
    unauthorized: bool = False
    runs: tuple[RunRow, ...] = ()
    runs_error: Optional[str] = None
    # Accelerator memory THIS gateway process pins (MLX live + cached buffers),
    # independent of what the model list attributes. None when unknown.
    device_held_bytes: Optional[int] = None
    # macOS physical footprint of the gateway process (includes Metal buffers).
    process_footprint: Optional[int] = None
    # HOW device_held_bytes was measured, in words (`held_basis_words`), e.g.
    # "metal device counter" / "sum of MLX + llama.cpp (estimated)". None
    # when no held figure is known.
    device_held_basis: Optional[str] = None
    # WHAT the process holds, "[backend] model × N holders" per resident row
    # (`memory.resident.models`, else the MLX-only `memory.held.models`).
    held_by: tuple[str, ...] = ()
    # Default-switch / failed-load ejects (`residency_diagnostics` of GET
    # /host/state) as (tone, text): tone is "pending" | "failed" | "kept" |
    # "done" | "unreported". Empty = nothing to say (or not sampled yet).
    eject_status: tuple[tuple[str, str], ...] = ()


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value == value and value not in (float("inf"), float("-inf")):
        return float(value)
    return None


def _int(value: Any) -> Optional[int]:
    v = _num(value)
    return int(v) if v is not None and v >= 0 else None


_SUM_BASIS_WORDS = {
    "mlx_held_bytes": "MLX",
    "llama_cpp_bytes(estimated)": "llama.cpp (estimated)",
    "llama_cpp_bytes": "llama.cpp",
    "allocated_bytes": "live allocations",
}


def held_basis_words(basis: Any) -> str:
    """`device.process_held_basis` (abstractcore utils/memory) in words --
    the same wording as the console's `processHeldBasisWords`. An unknown
    spelling is returned verbatim; a missing basis says so."""
    b = basis.strip() if isinstance(basis, str) else ""
    if not b:
        return "basis not reported"
    if b == "metal_device_counter":
        return "metal device counter"
    if b.startswith("cuda_device_counter"):
        return "cuda device counter" + (" + llama.cpp (estimated)" if "llama_cpp_bytes" in b else "")
    if b.startswith("sum:"):
        parts = [_SUM_BASIS_WORDS.get(p, p) for p in b[4:].split("+") if p]
        return f"sum of {' + '.join(parts)}" if parts else b
    return b


def held_by_from_memory(mem: Dict[str, Any]) -> tuple[str, ...]:
    """"[backend] model × N holders" for every row the process holds:
    `resident.models` (every in-process backend) when it has rows, else the
    MLX-only `held.models` of older cores."""
    resident = mem.get("resident") if isinstance(mem.get("resident"), dict) else None
    block = resident if resident and isinstance(resident.get("models"), list) and resident.get("models") else None
    if block is None:
        block = mem.get("held") if isinstance(mem.get("held"), dict) else None
    rows = block.get("models") if block and isinstance(block.get("models"), list) else []
    out: List[str] = []
    for m in rows:
        if not isinstance(m, dict):
            continue
        holders = _int(m.get("holders")) or 0
        names = m.get("models")
        name = ", ".join(str(x) for x in names) if isinstance(names, list) and names else str(m.get("model_path") or m.get("model") or "?")
        backend = f"[{m.get('backend')}] " if m.get("backend") else ""
        copies = " (full copies)" if m.get("shared_weights") is False and holders > 1 else ""
        out.append(f"{backend}{name} × {holders} holder{'' if holders == 1 else 's'}{copies}")
    return tuple(out)


def eject_status_from_host_state(payload: Dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """(tone, text) lines from `residency_diagnostics` -- the same rules as
    the console's `ejectStatusLines`: pending → "Will eject X when the
    in-flight call ends", failure → "X: eject failed: <reason>", kept → why,
    done → "X ejected". A snapshot without the block says so (never an
    implied "nothing pending"); a degraded model list says nothing here."""
    if not isinstance(payload.get("models"), list):
        return ()
    diag = payload.get("residency_diagnostics")
    if not isinstance(diag, dict):
        return (("unreported", "Eject status: this gateway's host snapshot does not report residency diagnostics."),)

    def label(e: Dict[str, Any]) -> str:
        parts = [str(e.get(k)) for k in ("provider", "model") if e.get(k) not in (None, "")]
        return "/".join(parts) or "a model"

    out: List[tuple[str, str]] = []
    pending = [e for e in (diag.get("pending_ejects") or []) if isinstance(e, dict)]
    pending_keys = {label(e) for e in pending}
    for e in pending:
        out.append(("pending", f"Will eject {label(e)} when the in-flight call ends."))
    for e in [e for e in (diag.get("last_switch_ejects") or []) if isinstance(e, dict)]:
        name = label(e)
        if e.get("deferred") is True:
            if name not in pending_keys:
                out.append(("pending", f"Will eject {name} when the in-flight call ends."))
        elif e.get("ok") is False:
            out.append(("failed", f"{name}: eject failed: {e.get('error') or e.get('reason') or 'no reason reported'}"))
        elif e.get("skipped") is True:
            out.append(("kept", f"{name} kept in memory: {e.get('reason') or 'still in use'}"))
        else:
            n = _int(e.get("holders_found"))
            out.append(("done", f"{name} ejected" + (f" from {n} holder{'' if n == 1 else 's'}" if n else "") + "."))
    return tuple(out)


def model_rows_from_host_state(payload: Dict[str, Any]) -> tuple[List[ModelRow], Optional[int]]:
    """Rows a tray can act on: RESIDENT models only (a configured-but-cold
    row has nothing in memory to unload — the console shows those behind a
    toggle; the tray shows what actually eats memory)."""
    rows: List[ModelRow] = []
    total: Optional[int] = None
    for rec in payload.get("models") or []:
        if not isinstance(rec, dict):
            continue
        if rec.get("resident") is not True:
            continue
        provider = str(rec.get("provider") or "?")
        model = str(rec.get("model") or "?")
        runtime_id = rec.get("runtime_id")
        target = {"runtime_id": runtime_id} if isinstance(runtime_id, str) and runtime_id else {"provider": provider, "model": model}
        size = _int(rec.get("size_bytes"))
        source = "reported"
        if size is None:
            size = _int(rec.get("size_vram_bytes"))
        if size is None:
            size = _int(rec.get("est_weights_bytes"))
            source = "estimated" if size is not None else ""
        if size is not None:
            total = (total or 0) + size
        mods = rec.get("modalities")
        rows.append(
            ModelRow(
                key=str(runtime_id or f"{provider}/{model}"),
                name=model,
                provider=provider,
                size_bytes=size,
                size_source=source,
                locked=bool(rec.get("locked")),
                lockable=bool(rec.get("lockable")),
                resident=rec.get("resident"),
                target=target,
                modalities=tuple(str(m) for m in mods) if isinstance(mods, list) else (),
            )
        )
    rows.sort(key=lambda r: (-(r.size_bytes or 0), r.name.lower()))
    return rows, total


def _epoch(value: Any) -> Optional[float]:
    """Epoch seconds from whatever `created_at` carries.

    Run stores write ISO-8601 (with `Z`, an offset, or none at all); some
    write a number. A timestamp we cannot read is None, and a row with no
    readable start is shown rather than dropped -- the 24 h window is a
    kindness to the reader, not a filter worth losing a run to.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # Naive means local, which is what a local run store writes.
        parsed = parsed.astimezone()
    return parsed.timestamp()


def run_rows_from_listing(payload: Any, *, now: Optional[float] = None, window_s: float = RUNS_WINDOW_S) -> List[RunRow]:
    """`GET /host/runs` items → the Workflows submenu's rows, newest first.

    The gateway already filtered to the window and decoded a readable `label`
    (a catalog-published workflow runs under a base64 internal id, which is
    the right key for the store and an unreadable string for a menu). The
    window is applied again here because it costs nothing and a cached payload
    ages while the menu stays open.

    A run whose timestamp cannot be read is KEPT (see `_epoch`): it may be the
    one running right now.
    """
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    cutoff = (time.time() if now is None else float(now)) - float(window_s)
    rows: List[RunRow] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        run_id = str(item.get("run_id") or "").strip()
        if not run_id:
            continue
        started = _epoch(item.get("created_at"))
        if started is not None and started < cutoff:
            continue
        ended = _epoch(item.get("updated_at"))
        duration = (ended - started) if (started is not None and ended is not None and ended >= started) else None
        rows.append(
            RunRow(
                run_id=run_id,
                workflow_id=str(item.get("label") or item.get("workflow_id") or "").strip() or run_id,
                status=str(item.get("status") or "unknown").strip().lower(),
                steps=_int(item.get("ledger_len")),
                duration_s=duration,
                started_at=started,
            )
        )
    return rows


class Sampler:
    def __init__(self, client: GatewayClient, *, version: str = "", fast_interval_s: float = 1.0, slow_interval_s: float = 5.0, runner_interval_s: float = 2.0, runs_interval_s: float = 20.0) -> None:
        self._client = client
        self._version = version
        self._fast = float(fast_interval_s)
        self._slow = float(slow_interval_s)
        self._runner_every = float(runner_interval_s)
        self._runs_every = float(runs_interval_s)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._listeners: List[Callable[[Snapshot], None]] = []
        self._gpu_hist: Deque[Optional[float]] = deque([None] * HISTORY_SAMPLES, maxlen=HISTORY_SAMPLES)
        self._mem_hist: Deque[Optional[float]] = deque([None] * HISTORY_SAMPLES, maxlen=HISTORY_SAMPLES)
        self._rss_hist: Deque[Optional[float]] = deque([None] * HISTORY_SAMPLES, maxlen=HISTORY_SAMPLES)
        self._state: Dict[str, Any] = {
            "reachable": False,
            "ever_reachable": False,
            "failures": 0,
            "paused": False,
            "inflight": 0,
            "paused_by": None,
            "pause_reason": None,
            "gpu": {"supported": False, "reason": None, "pct": None, "name": None},
            "mem": {"supported": False, "reason": None, "used": None, "total": None, "pct": None, "rss": None, "backend": None},
            "models": [],
            "models_total": None,
            "models_error": None,
            "eject_status": (),
            "runs": [],
            "runs_error": None,
            "caps": {"restart": False, "shutdown": False, "reason": None, "update_job_running": False},
            "step_gate": None,
            "runner_in_process": None,
            "last_error": None,
            "override": None,  # restarting | updating | stopping (set by actions)
        }
        self._last_slow = 0.0
        self._last_runner = 0.0
        self._last_runs = 0.0
        self._slow_thread: Optional[threading.Thread] = None
        self._slow_wake = threading.Event()
        self._live_supported: Optional[bool] = None  # GET /host/metrics/live exists on this gateway?
        self._auth_failures = 0
        self._unauthorized = False

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="tray-sampler", daemon=True)
        self._thread.start()
        # The slow lane (residency walks every provider, up to 20 s) gets its
        # OWN thread so the icon and graphs never freeze while it runs.
        self._slow_thread = threading.Thread(target=self._slow_loop, name="tray-sampler-slow", daemon=True)
        self._slow_thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._slow_wake.set()

    def poke(self) -> None:
        """Ask for an immediate resample (after an action)."""
        self._last_runner = 0.0
        self._last_runs = 0.0
        self._wake.set()
        self._slow_wake.set()

    @property
    def unauthorized(self) -> bool:
        """The token was refused: this helper belongs to a gateway that is
        gone (or another gateway owns the port). Polling stopped — ten
        refusals would lock every loopback client out (auth lockout)."""
        return self._unauthorized

    def add_listener(self, fn: Callable[[Snapshot], None]) -> None:
        with self._lock:
            self._listeners.append(fn)

    def set_override(self, state: Optional[str]) -> None:
        """Actions announce transitional states the polls cannot see yet."""
        with self._lock:
            self._state["override"] = state
        self._notify()

    # -- sampling ------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._unauthorized:
                self._notify()
                self._wake.wait(timeout=30.0)
                self._wake.clear()
                continue
            t0 = time.monotonic()
            try:
                self._sample_fast()
                if self._live_supported is not True and (t0 - self._last_runner) >= self._runner_every:
                    self._last_runner = t0
                    self._sample_runner()
                elif self._live_supported is True and (t0 - self._last_runner) >= max(self._runner_every, 5.0):
                    # The live payload carries paused/inflight; capabilities +
                    # step-gate facts change rarely — refresh them every 5 s.
                    self._last_runner = t0
                    self._sample_runner()
            except Exception as exc:  # noqa: BLE001 - the sampler must never die
                with self._lock:
                    self._state["last_error"] = f"{type(exc).__name__}: {exc}"
            self._notify()
            elapsed = time.monotonic() - t0
            self._wake.wait(timeout=max(0.05, self._fast - elapsed))
            self._wake.clear()

    def _slow_loop(self) -> None:
        while not self._stop.is_set():
            if not self._unauthorized:
                try:
                    self._sample_slow()
                except Exception as exc:  # noqa: BLE001
                    with self._lock:
                        self._state["models_error"] = f"{type(exc).__name__}: {exc}"
                self._notify()
            self._slow_wake.wait(timeout=self._slow)
            self._slow_wake.clear()

    def _note_auth(self, *results: Any) -> None:
        """Two consecutive 401/403 answers = our token is dead for good."""
        statuses = [r.status for r in results if r is not None]
        if any(s in (401, 403) for s in statuses):
            self._auth_failures += 1
            if self._auth_failures >= 2:
                self._unauthorized = True
                with self._lock:
                    self._state["last_error"] = "not authorised — this helper's gateway is gone (or another gateway owns the port)"
        elif any(200 <= s < 300 for s in statuses):
            self._auth_failures = 0

    def _apply_gpu_payload(self, d: Dict[str, Any]) -> None:
        supported = d.get("supported") is True
        pct = _num(d.get("utilization_gpu_pct")) if supported else None
        name = None
        gpus = d.get("gpus")
        if isinstance(gpus, list) and gpus:
            # Several GPUs: the icon shows the BUSIEST one (an average hides
            # a single saturated card behind idle siblings).
            per = [_num(g.get("utilization_gpu_pct")) for g in gpus if isinstance(g, dict)]
            per = [p for p in per if p is not None]
            if per:
                pct = max(per)
            first = gpus[0] if isinstance(gpus[0], dict) else {}
            name = str(first.get("name") or "") or None
            if len(gpus) > 1:
                name = f"{name} (+{len(gpus) - 1})" if name else f"{len(gpus)} GPUs"
        self._state["gpu"] = {"supported": supported, "reason": None if supported else str(d.get("reason") or "not available"), "pct": pct, "name": name}
        self._gpu_hist.append(pct)

    def _apply_memory_payload(self, d: Dict[str, Any]) -> None:
        supported = d.get("supported") is True
        ram = d.get("ram") if isinstance(d.get("ram"), dict) else {}
        proc = d.get("process") if isinstance(d.get("process"), dict) else {}
        dev = d.get("device") if isinstance(d.get("device"), dict) else {}
        total = _int(ram.get("total_bytes"))
        available = _int(ram.get("available_bytes"))
        used = _int(ram.get("used_bytes"))
        # "In use" = total − available: the number that predicts pressure
        # (file cache and memory-mapped weights are reclaimable; a raw
        # `used` on macOS/Linux is not what a person means by "full").
        if total and available is not None:
            used = max(0, total - available)
        pct = (100.0 * used / total) if (used is not None and total) else _num(ram.get("percent"))
        rss = _int(proc.get("rss_bytes"))
        # What THIS gateway process pins in accelerator memory: MLX live +
        # freed-but-cached buffers (newer gateways), else the live figure
        # older gateways report. The tray shows it whenever it is known, and
        # says so LOUDLY when the model list is empty -- "No models loaded"
        # over 92 GB of held MLX memory is the lie this exists to end.
        held = _int(dev.get("process_held_bytes"))  # every in-process allocator (see core utils/memory)
        basis = held_basis_words(dev.get("process_held_basis")) if held is not None else None
        if held is None:
            held = _int(dev.get("mlx_held_bytes"))
            basis = "MLX buffers only (this AbstractCore reports no process-wide figure)" if held is not None else None
        if held is None:
            held = _int(dev.get("allocated_bytes"))
            basis = "live allocations only (this AbstractCore reports no process-wide figure)" if held is not None else None
        self._state["mem"] = {
            "supported": supported and (pct is not None or used is not None),
            "reason": None if supported else str(d.get("reason") or "not available"),
            "used": used,
            "total": total,
            "pct": pct,
            "rss": rss,
            "backend": str(dev.get("backend")) if dev.get("backend") else None,
            "held": held,
            "held_basis": basis,
            "held_by": held_by_from_memory(d),
            "footprint": _int(proc.get("footprint_bytes")),
        }
        self._mem_hist.append(pct)
        self._rss_hist.append((100.0 * rss / total) if (rss is not None and total) else None)

    def _sample_fast(self) -> None:
        # Preferred: ONE cached call (/host/metrics/live). Older gateways
        # answer 404 → fall back to the two separate probes for good.
        if self._live_supported is not False:
            live = self._client.live()
            if live.status == 404:
                self._live_supported = False
            elif live.ok and isinstance(live.data, dict):
                self._live_supported = True
                self._note_auth(live)
                d = live.data
                with self._lock:
                    gpu = d.get("gpu") if isinstance(d.get("gpu"), dict) else {"supported": False, "reason": "no gpu section"}
                    mem = d.get("memory") if isinstance(d.get("memory"), dict) else {"supported": False, "reason": "no memory section"}
                    self._apply_gpu_payload(gpu)
                    self._apply_memory_payload(mem)
                    runner = d.get("runner") if isinstance(d.get("runner"), dict) else {}
                    if runner:
                        self._state["paused"] = bool(runner.get("paused"))
                        self._state["inflight"] = int(_num(runner.get("inflight_ticks")) or 0)
                        self._state["paused_by"] = runner.get("paused_by")
                        self._state["pause_reason"] = runner.get("reason")
                        if self._state.get("override") in {"pausing", "resuming"}:
                            self._state["override"] = None
                    self._state["reachable"] = True
                    self._state["ever_reachable"] = True
                    self._state["failures"] = 0
                    self._state["last_error"] = None
                return
            else:
                self._note_auth(live)
                with self._lock:
                    self._gpu_hist.append(None)
                    self._mem_hist.append(None)
                    self._rss_hist.append(None)
                    self._state["failures"] = int(self._state["failures"]) + 1
                    self._state["last_error"] = live.error
                    if self._state["failures"] >= 3:
                        self._state["reachable"] = False
                if self._live_supported is True:
                    return
        gpu = self._client.gpu()
        mem = self._client.memory()
        self._note_auth(gpu, mem)
        with self._lock:
            if gpu.ok and isinstance(gpu.data, dict):
                self._apply_gpu_payload(gpu.data)
            else:
                self._gpu_hist.append(None)
            if mem.ok and isinstance(mem.data, dict):
                self._apply_memory_payload(mem.data)
            else:
                self._mem_hist.append(None)
                self._rss_hist.append(None)
            if gpu.ok or mem.ok:
                self._state["reachable"] = True
                self._state["ever_reachable"] = True
                self._state["failures"] = 0
                self._state["last_error"] = None
            else:
                self._state["failures"] = int(self._state["failures"]) + 1
                self._state["last_error"] = gpu.error or mem.error
                if self._state["failures"] >= 3:
                    self._state["reachable"] = False

    def _sample_runner(self) -> None:
        r = self._client.runner()
        self._note_auth(r)
        if not (r.ok and isinstance(r.data, dict)):
            return
        d = r.data
        caps = d.get("capabilities") if isinstance(d.get("capabilities"), dict) else {}
        with self._lock:
            self._state["paused"] = bool(d.get("paused"))
            self._state["inflight"] = int(_num(d.get("inflight_ticks")) or 0)
            self._state["paused_by"] = d.get("paused_by")
            self._state["pause_reason"] = d.get("reason")
            self._state["step_gate"] = d.get("step_gate_supported")
            self._state["runner_in_process"] = d.get("runner_in_process")
            self._state["caps"] = {"restart": bool(caps.get("restart")), "shutdown": bool(caps.get("shutdown")), "reason": caps.get("reason"), "update_job_running": bool(caps.get("update_job_running"))}
            # A real answer after an action's override means the action is over.
            if self._state.get("override") in {"pausing", "resuming"}:
                self._state["override"] = None

    def _sample_slow(self) -> None:
        r = self._client.host_state()
        self._note_auth(r)
        with self._lock:
            if r.ok and isinstance(r.data, dict):
                rows, total = model_rows_from_host_state(r.data)
                degraded = r.data.get("degraded") if isinstance(r.data.get("degraded"), list) else []
                reasons = r.data.get("reasons") if isinstance(r.data.get("reasons"), dict) else {}
                self._state["models"] = rows
                self._state["models_total"] = total
                self._state["models_error"] = str(reasons.get("models") or "model list unavailable") if "models" in degraded else None
                self._state["eject_status"] = eject_status_from_host_state(r.data)
            else:
                self._state["models_error"] = r.error or "model list unavailable"
        self._sample_runs()

    def _sample_runs(self) -> None:
        """The last day's runs, at most every `runs_interval_s`.

        On the SLOW thread by design: `/runs` reads the run index and one
        ledger count per row, which is cheap but not free, and nothing on the
        icon depends on it -- only a submenu a person has to open to read.
        """
        now = time.monotonic()
        if (now - self._last_runs) < self._runs_every:
            return
        self._last_runs = now
        r = self._client.recent_runs(limit=RUNS_LIMIT)
        self._note_auth(r)
        with self._lock:
            if r.ok:
                self._state["runs"] = run_rows_from_listing(r.data)
                self._state["runs_error"] = None
            else:
                # The rows we already have stay: a blip must not empty the menu.
                self._state["runs_error"] = r.error or "run list unavailable"

    # -- output --------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        with self._lock:
            s = self._state
            override = s.get("override")
            if self._unauthorized:
                state = "unreachable"
            elif override:
                state = str(override)
            elif not s["ever_reachable"]:
                state = "starting"
            elif not s["reachable"]:
                state = "unreachable"
            elif s["paused"] and int(s["inflight"]) > 0:
                state = "pausing"
            elif s["paused"]:
                state = "paused"
            else:
                state = "running"
            gpu, mem = s["gpu"], s["mem"]
            return Snapshot(
                ts=time.time(),
                gateway_state=state,
                reachable=bool(s["reachable"]),
                paused=bool(s["paused"]),
                inflight_ticks=int(s["inflight"]),
                paused_by=s["paused_by"],
                pause_reason=s["pause_reason"],
                gpu_supported=bool(gpu["supported"]),
                gpu_reason=gpu["reason"],
                gpu_pct=gpu["pct"],
                gpu_name=gpu["name"],
                gpu_history=tuple(self._gpu_hist),
                mem_supported=bool(mem["supported"]),
                mem_reason=mem["reason"],
                mem_used=mem["used"],
                mem_total=mem["total"],
                mem_pct=mem["pct"],
                mem_history=tuple(self._mem_hist),
                process_rss=mem["rss"],
                process_history=tuple(self._rss_hist),
                device_backend=mem["backend"],
                models=tuple(s["models"]),
                models_total_bytes=s["models_total"],
                models_error=s["models_error"],
                can_restart=bool(s["caps"]["restart"]),
                can_shutdown=bool(s["caps"]["shutdown"]),
                restart_block_reason=s["caps"]["reason"],
                update_job_running=bool(s["caps"].get("update_job_running")),
                step_gate_supported=s.get("step_gate"),
                version=self._version,
                last_error=s["last_error"],
                consecutive_failures=int(s["failures"]),
                unauthorized=bool(self._unauthorized),
                runs=tuple(s["runs"]),
                runs_error=s["runs_error"],
                device_held_bytes=mem.get("held"),
                process_footprint=mem.get("footprint"),
                device_held_basis=mem.get("held_basis"),
                held_by=tuple(mem.get("held_by") or ()),
                eject_status=tuple(s.get("eject_status") or ()),
            )

    def _notify(self) -> None:
        snap = self.snapshot()
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(snap)
            except Exception:  # noqa: BLE001 - a listener bug must not stop sampling
                pass


# ---------------------------------------------------------------------------
# Formatting helpers shared by the menu, the icon tooltip and the monitor
# ---------------------------------------------------------------------------


def fmt_bytes(n: Optional[int]) -> str:
    if n is None:
        return "size unknown"
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}" if value < 100 else f"{value:.0f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"


def fmt_pct(v: Optional[float]) -> str:
    return "—" if v is None else f"{int(round(v))}%"


