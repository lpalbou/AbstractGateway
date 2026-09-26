"""The Stop kill switch: a cancelled run whose model call has not stopped after
an operator-chosen deadline has that INFERENCE killed — in process, never the
gateway.

WHY (incident)
-------------------------
A runaway generation kept decoding for an hour after Stop because nothing
passed the cancel to the in-flight model call. The soft path now does
(AbstractRuntime `core.effect_cancellation` → AbstractCore `cancel_event` →
the provider stops within one token). This module is the BACKSTOP for a call
whose loop does not observe the event (a provider lane without cancel support,
a non-streaming HTTP request, a bug in the soft path).

OPERATOR RULE: the kill switch kills the INFERENCE ONLY. It never
terminates or restarts the gateway process; other runs, other sessions, the
HTTP surface and the runner keep working throughout.

CONTRACT
--------
- Armed by the runner every time a `cancel` command is applied, over the run
  tree the command cancelled. Deadline: `stop_kill_switch_s` (runtime config
  key; env `ABSTRACTGATEWAY_STOP_KILL_SWITCH_S`; default 10 s), re-read at
  every Stop. 0 disables it and is logged at ERROR at every Stop.
- Only INFERENCE counts: an `llm_call` of the tree still registered in the
  runtime's in-flight registry at the deadline fires the switch. A tool still
  running is named in the log and never escalated (its result is never fed to
  another model call: the run is CANCELLED).
- A soft stop that succeeds disarms the incident; the switch never fires then.
- FIRING, per stuck model call:
    1. an ERROR log line naming run, step, node, provider, model, elapsed,
       time since cancel and `killed_by=kill_switch`;
    2. `kill_inflight_effect(step)`: the runtime injects `EffectKilled` into
       the ONE thread executing that call. CPython delivers it at the thread's
       next bytecode boundary, so a Python-level decode loop (mlx-lm, mlx-vlm,
       the native scheduler's consumer) stops within one token, unwinding the
       provider through its `finally`/`with` blocks (generators closed, locks
       released, the native job cancelled); the runtime records the step
       `cancelled` with `killed_by: kill_switch` and `stopped_after_kill_s`.
       Only that thread is touched.
    3. the switch waits up to `KILL_GRACE_S` (named in the log) for the call to
       leave the registry. A call still there is blocked inside ONE native
       operation that never returned: that is reported, ERROR and in the
       ledger, as "could not be interrupted" with the thread named — never
       papered over (the injected exception is still pending and fires when
       the native call returns);
    4. an `abstract.status` record on every run of the tree with the UI text
       ("Stop forced at N s: inference killed") and the full attribution;
    5. every run of the tree ends CANCELLED with that reason.
"""

from __future__ import annotations

import datetime
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_DEADLINE_S = 10.0
ENV_DEADLINE = "ABSTRACTGATEWAY_STOP_KILL_SWITCH_S"
#: How long a killed call may take to unwind before it is reported as blocked
#: inside a native operation. Named in every log line that uses it.
KILL_GRACE_S = 5.0
INFERENCE_EFFECT_TYPES = frozenset({"llm_call"})
WATCH_INTERVAL_S = 0.1


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def forced_stop_text(deadline_s: float, *, interrupted: bool = True) -> str:
    """The UI string for a forced stop (the web client keys on `killed_by`, not on this text)."""

    if interrupted:
        return f"Stop forced at {deadline_s:g} s: inference killed"
    return f"Stop forced at {deadline_s:g} s: inference could not be interrupted (blocked in a native call)"


@dataclass
class _Incident:
    root_run_id: str
    run_ids: List[str]
    deadline_s: float
    armed_monotonic: float = field(default_factory=time.monotonic)
    armed_at: str = field(default_factory=_utc_now_iso)
    command_id: Optional[str] = None


def _inflight_effects(run_ids: List[str]) -> List[Dict[str, Any]]:
    # The runtime's registry is the ONLY truth about what is executing.
    from abstractruntime.core.effect_cancellation import inflight_effects

    return inflight_effects(run_ids)


def _kill_inflight_effect(step_id: str, *, killed_by: str, reason: Optional[str]) -> Dict[str, Any]:
    from abstractruntime.core.effect_cancellation import kill_inflight_effect

    return kill_inflight_effect(step_id, killed_by=killed_by, reason=reason)


class StopKillSwitch:
    """Watches cancelled run trees until their inference stops, or kills it at the deadline."""

    def __init__(
        self,
        *,
        run_store: Any,
        ledger_store: Any,
        settings: Callable[[], Dict[str, Any]],
        kill: Callable[..., Dict[str, Any]] = _kill_inflight_effect,
        inflight: Callable[[List[str]], List[Dict[str, Any]]] = _inflight_effects,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        interval_s: float = WATCH_INTERVAL_S,
        grace_s: float = KILL_GRACE_S,
        watch: bool = True,
        on_terminal: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        # Stores may be given as zero-arg callables (the runner resolves its
        # host's stores lazily); resolved at use, never at construction.
        self._run_store_ref = run_store
        self._ledger_store_ref = ledger_store
        self._settings = settings
        # Called with each run this switch marks CANCELLED straight in the
        # store (no Runtime, so no terminal hooks): the runner passes the
        # live-delta cleanup (live_deltas.close_run_live_state).
        self._on_terminal = on_terminal
        self._kill = kill
        self._inflight = inflight
        self._clock = clock
        self._sleep = sleep
        self._interval_s = float(interval_s)
        self._grace_s = float(grace_s)
        # watch=False: no background thread; the caller drives `check()` (tests).
        self._watch_enabled = bool(watch)
        self._lock = threading.Lock()
        self._incidents: Dict[str, _Incident] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.fired: List[Dict[str, Any]] = []  # diagnostic surface (tests, status)

    @property
    def _run_store(self) -> Any:
        ref = self._run_store_ref
        return ref() if callable(ref) and not hasattr(ref, "load") else ref

    @property
    def _ledger_store(self) -> Any:
        ref = self._ledger_store_ref
        return ref() if callable(ref) and not hasattr(ref, "append") else ref

    # -- arming ---------------------------------------------------------------

    def arm(self, *, root_run_id: str, run_ids: List[str], command_id: Optional[str] = None) -> Optional[_Incident]:
        settings = self._settings() or {}
        deadline = float(settings.get("deadline_s", DEFAULT_DEADLINE_S))
        source = settings.get("source") or "default"
        if deadline <= 0:
            # ERROR: the gateway console is ERROR-only by default, and a
            # disabled backstop must never be silent.
            logger.error(
                "stop kill switch DISABLED (stop_kill_switch_s=%s, source=%s): cancel of run %s is soft-only; "
                "a model call that ignores the cancel keeps decoding",
                deadline, source, root_run_id,
            )
            return None
        incident = _Incident(
            root_run_id=str(root_run_id),
            run_ids=[str(r) for r in run_ids] or [str(root_run_id)],
            deadline_s=deadline,
            command_id=command_id,
            armed_monotonic=self._clock(),
        )
        with self._lock:
            # A second Stop on the same tree keeps the FIRST deadline: the
            # operator's clock started at the first press.
            existing = self._incidents.get(incident.root_run_id)
            if existing is not None:
                existing.run_ids = sorted(set(existing.run_ids) | set(incident.run_ids))
                return existing
            self._incidents[incident.root_run_id] = incident
            if self._watch_enabled and (self._thread is None or not self._thread.is_alive()):
                self._stop.clear()
                self._thread = threading.Thread(target=self._watch, name="gateway-stop-kill-switch", daemon=True)
                self._thread.start()
        logger.info(
            "stop kill switch armed: run %s (+%d descendants), deadline %gs (source=%s)",
            incident.root_run_id, max(0, len(incident.run_ids) - 1), deadline, source,
        )
        return incident

    def stop(self) -> None:
        self._stop.set()

    def pending(self) -> List[str]:
        with self._lock:
            return list(self._incidents)

    # -- watching -------------------------------------------------------------

    def _watch(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                incidents = list(self._incidents.values())
                if not incidents:
                    self._thread = None
                    return
            for incident in incidents:
                try:
                    self.check(incident)
                except Exception:  # noqa: BLE001 - the watchdog must outlive a bad incident
                    logger.exception("stop kill switch: check of run %s failed", incident.root_run_id)
            self._stop.wait(self._interval_s)

    def check(self, incident: _Incident) -> str:
        """One evaluation: 'stopped' | 'waiting' | 'fired'."""

        now = self._clock()
        elapsed = now - incident.armed_monotonic
        live = self._inflight(incident.run_ids)
        inference = [e for e in live if str(e.get("effect_type")) in INFERENCE_EFFECT_TYPES]
        if not inference:
            with self._lock:
                self._incidents.pop(incident.root_run_id, None)
            others = [e for e in live if str(e.get("effect_type")) not in INFERENCE_EFFECT_TYPES]
            logger.info(
                "stop: soft stop complete for run %s — no inference in flight %.3fs after the cancel was applied%s",
                incident.root_run_id, elapsed,
                (f" ({len(others)} non-inference effect(s) still finishing, never escalated: "
                 + ", ".join(f"{e.get('effect_type')}@{e.get('run_id')} {e.get('elapsed_s')}s" for e in others) + ")")
                if others else "",
            )
            return "stopped"
        if elapsed < incident.deadline_s:
            return "waiting"
        with self._lock:
            if self._incidents.pop(incident.root_run_id, None) is None:
                return "fired"  # another check already fired it
        self._fire(incident, inference, live, elapsed)
        return "fired"

    # -- firing: kill the inference, in process --------------------------------

    def _fire(self, incident: _Incident, inference: List[Dict[str, Any]], live: List[Dict[str, Any]], elapsed: float) -> None:
        reason_text = forced_stop_text(incident.deadline_s)
        kills: List[Dict[str, Any]] = []
        for effect in inference:
            logger.error(
                "STOP KILL SWITCH FIRED: run=%s root=%s step=%s node=%s provider=%s model=%s "
                "effect_elapsed=%.1fs since_cancel=%.1fs deadline=%gs killed_by=kill_switch action=kill_inference",
                effect.get("run_id"), incident.root_run_id, effect.get("step_id"), effect.get("node_id"),
                effect.get("provider"), effect.get("model"), float(effect.get("elapsed_s") or 0.0),
                float(effect.get("since_cancel_s") or elapsed), incident.deadline_s,
            )
            try:
                result = dict(self._kill(str(effect.get("step_id")), killed_by="kill_switch", reason=reason_text) or {})
            except Exception as exc:  # noqa: BLE001 - reported below, never swallowed
                result = {"injected": False, "reason": f"{type(exc).__name__}: {exc}"}
            result.setdefault("step_id", effect.get("step_id"))
            kills.append(result)

        # Wait (bounded, named) for the killed calls to unwind out of the registry.
        steps = {str(e.get("step_id")) for e in inference}
        t_kill = self._clock()
        remaining = steps
        while True:
            still = [e for e in self._inflight(incident.run_ids) if str(e.get("step_id")) in steps]
            remaining = {str(e.get("step_id")) for e in still}
            if not remaining or self._clock() - t_kill >= self._grace_s:
                break
            self._sleep(0.05)
        unwind_s = round(self._clock() - t_kill, 3)

        stuck = [e for e in inference if str(e.get("step_id")) in remaining]
        for effect in stuck:
            kill = next((k for k in kills if k.get("step_id") == effect.get("step_id")), {})
            logger.error(
                "STOP KILL SWITCH: inference could NOT be interrupted in process within %gs (KILL_GRACE_S): "
                "run=%s step=%s thread=%s injected=%s (%s) — the thread is blocked inside a single native call; "
                "the GPU stays busy until it returns, when the pending kill fires. The gateway is NOT restarted.",
                self._grace_s, effect.get("run_id"), effect.get("step_id"), effect.get("thread"),
                kill.get("injected"), kill.get("reason") or "delivered at the next bytecode",
            )
        if not stuck:
            logger.error(
                "STOP KILL SWITCH: inference killed in process for run %s (%d call(s) unwound %.3fs after the kill; "
                "gateway process untouched)",
                incident.root_run_id, len(inference), unwind_s,
            )

        text = forced_stop_text(incident.deadline_s, interrupted=not stuck)
        first = inference[0]
        record = {
            "text": text,
            "killed_by": "kill_switch",
            "stop": "forced",
            "action": "kill_inference",
            "interrupted": not stuck,
            "root_run_id": incident.root_run_id,
            "run_id": first.get("run_id"),
            "step_id": first.get("step_id"),
            "node_id": first.get("node_id"),
            "provider": first.get("provider"),
            "model": first.get("model"),
            "effect_elapsed_s": first.get("elapsed_s"),
            "since_cancel_s": round(elapsed, 3),
            "deadline_s": incident.deadline_s,
            "unwind_s": unwind_s,
            "grace_s": self._grace_s,
            "killed_steps": sorted(steps - remaining),
            "stuck_steps": sorted(remaining),
            "stuck_threads": [e.get("thread") for e in stuck],
        }
        self.fired.append({**record, "kills": kills, "inference_in_flight": inference,
                           "other_effects_in_flight": [e for e in live if e not in inference],
                           "armed_at": incident.armed_at, "fired_at": _utc_now_iso()})
        self._append_status(incident, record)
        self._cancel_tree(incident, f"{text} (killed_by=kill_switch)")

    def _load(self, run_id: str) -> Any:
        try:
            return self._run_store.load(str(run_id))
        except Exception:
            return None

    def _append_status(self, incident: _Incident, payload: Dict[str, Any]) -> None:
        from abstractruntime.core.models import Effect, EffectType, StepRecord

        for rid in incident.run_ids:
            run = self._load(rid)
            if run is None:
                continue
            try:
                eff = Effect(type=EffectType.EMIT_EVENT,
                             payload={"name": "abstract.status", "scope": "session", "payload": payload})
                rec = StepRecord.start(run=run, node_id=str(getattr(run, "current_node", None) or "runtime"),
                                       effect=eff, idempotency_key=f"system:stop_kill_switch:{incident.armed_at}")
                rec.finish_success({"emitted": True, "name": "abstract.status", "payload": payload})
                self._ledger_store.append(rec)
            except Exception:  # noqa: BLE001 - each run is independent
                logger.exception("stop kill switch: could not append the status record for run %s", rid)

    def _cancel_tree(self, incident: _Incident, reason: str) -> None:
        from abstractruntime.core.models import RunStatus

        for rid in incident.run_ids:
            run = self._load(rid)
            if run is None or getattr(run, "status", None) in (RunStatus.COMPLETED, RunStatus.FAILED):
                continue
            try:
                run.status = RunStatus.CANCELLED
                run.error = reason
                run.waiting = None
                run.updated_at = _utc_now_iso()
                self._run_store.save(run)
            except Exception:  # noqa: BLE001
                logger.exception("stop kill switch: could not mark run %s CANCELLED", rid)
                continue
            if self._on_terminal is not None:
                try:
                    self._on_terminal(run)
                except Exception:  # noqa: BLE001 - logged loudly; the stop itself stands
                    logger.exception("stop kill switch: closing the live state of run %s failed", rid)


def resolve_settings(data_dir: Any, *, default_deadline_s: float = DEFAULT_DEADLINE_S) -> Dict[str, Any]:
    """{deadline_s, source}: runtime config (stored) > env > default."""

    from .runtime_config import resolve_stop_kill_switch

    return resolve_stop_kill_switch(data_dir, default_deadline_s=default_deadline_s)


__all__ = [
    "DEFAULT_DEADLINE_S",
    "ENV_DEADLINE",
    "KILL_GRACE_S",
    "StopKillSwitch",
    "forced_stop_text",
    "resolve_settings",
]
