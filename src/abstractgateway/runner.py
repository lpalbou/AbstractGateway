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
import logging
import os
import re
import threading
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


@dataclass(frozen=True)
class GatewayRunnerConfig:
    poll_interval_s: float = 0.25
    command_batch_limit: int = 200
    tick_max_steps: int = 100
    tick_workers: int = 4
    run_scan_limit: int = 200


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

    @property
    def enabled(self) -> bool:
        return self._enable

    @property
    def command_store(self) -> CommandStore:
        return self._command_store

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
        if not self._enable:
            logger.info("GatewayRunner disabled by config/env")
            return
        if self._thread is not None and self._thread.is_alive():
            return
        if not self._acquire_singleton_lock():
            logger.warning("GatewayRunner not started: another process holds %s", self._singleton_lock_path)
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="abstractgateway-runner", daemon=True)
        self._thread.start()
        logger.info("GatewayRunner started (base_dir=%s)", self._base_dir)

    def stop(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)
        self._thread = None
        try:
            self._executor.shutdown(wait=False, cancel_futures=True)  # type: ignore[call-arg]
        except Exception:
            pass
        self._release_singleton_lock()

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
    ) -> None:
        """Emit an external event into the runtime (resume matching WAIT_EVENT runs).

        This is a thin wrapper around the internal emit_event command handling so
        integrations (Telegram bridge, webhooks, etc.) don't need to know the
        command-store format.
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
        if isinstance(workflow_id, str) and workflow_id.strip():
            body["workflow_id"] = workflow_id.strip()
        if isinstance(run_id, str) and run_id.strip():
            body["run_id"] = run_id.strip()
        if isinstance(event_id, str) and event_id.strip():
            body["event_id"] = event_id.strip()
        if isinstance(emitted_at, str) and emitted_at.strip():
            body["emitted_at"] = emitted_at.strip()

        self._apply_emit_event(body, default_session_id=sid, client_id=client_id)

    def _acquire_singleton_lock(self) -> bool:
        """Best-effort process singleton lock (prevents multi-worker double ticking)."""
        try:
            import fcntl  # Unix only
        except Exception:  # pragma: no cover
            return True
        try:
            self._singleton_lock_path.parent.mkdir(parents=True, exist_ok=True)
            fh = self._singleton_lock_path.open("a", encoding="utf-8")
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fh.write(f"pid={os.getpid()}\n")
            fh.flush()
            self._singleton_lock_fh = fh
            return True
        except Exception:
            try:
                if self._singleton_lock_fh is not None:
                    self._singleton_lock_fh.close()
            except Exception:
                pass
            self._singleton_lock_fh = None
            return False

    def _release_singleton_lock(self) -> None:
        try:
            if self._singleton_lock_fh is not None:
                self._singleton_lock_fh.close()
        except Exception:
            pass
        self._singleton_lock_fh = None

    # ---------------------------------------------------------------------
    # Main loop
    # ---------------------------------------------------------------------

    def _loop(self) -> None:
        cursor = int(self._cursor_store.load() or 0)
        while not self._stop.is_set():
            try:
                cursor = self._poll_commands(cursor)
            except Exception as e:
                logger.exception("GatewayRunner command poll error: %s", e)
            try:
                self._schedule_ticks()
            except Exception as e:
                logger.exception("GatewayRunner tick scheduling error: %s", e)
            self._stop.wait(timeout=float(self._cfg.poll_interval_s or 0.25))

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
                pass
        return max(int(next_cursor or 0), cur)

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

        # Best-effort recovery: if we restart after a child run reaches a terminal state,
        # parents blocked on WAITING(SUBWORKFLOW) can remain stuck because we don't tick
        # terminal child runs. Detect such cases and resume parents.
        try:
            self._repair_terminal_subworkflow_waits()
        except Exception:
            pass

    def _repair_terminal_subworkflow_waits(self) -> None:
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

            runtime, wf = self._host.runtime_and_workflow_for_run(r.run_id)
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
        if typ not in {"pause", "resume", "cancel", "emit_event", "update_schedule", "compact_memory"}:
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

    def _apply_emit_event(self, payload: Dict[str, Any], *, default_session_id: str, client_id: Optional[str]) -> None:
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
            return

        waiting_runs = list_runs(status=RunStatus.WAITING, wait_reason=WaitReason.EVENT, limit=10_000)
        for r in waiting_runs or []:
            if getattr(r, "waiting", None) is None:
                continue
            if getattr(r.waiting, "wait_key", None) != wait_key:
                continue
            if _is_pause_wait(getattr(r, "waiting", None), run_id=str(getattr(r, "run_id", "") or "")):
                continue
            runtime, wf = self._host.runtime_and_workflow_for_run(r.run_id)
            runtime.resume(workflow=wf, run_id=r.run_id, wait_key=wait_key, payload=envelope, max_steps=0)

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

    def _tick_run(self, run_id: str) -> None:
        try:
            runtime, wf = self._host.runtime_and_workflow_for_run(run_id)
        except Exception as e:
            logger.debug("GatewayRunner: cannot build runtime for %s: %s", run_id, e)
            return

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
                return

        # Auto-compaction for scheduled workflows (best-effort).
        try:
            self._maybe_auto_compact(state)
        except Exception:
            pass

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
                pass

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
            runtime, wf = self._host.runtime_and_workflow_for_run(r.run_id)
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
            pass

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
            pass

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
            pass

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
            pass

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
            pass
        if getattr(outcome, "status", None) == "failed":
            # Best-effort: record the error for debuggability but do not fail ticking.
            try:
                auto["last_error"] = getattr(outcome, "error", None)
                auto["last_error_at"] = utc_now_iso()
                self.run_store.save(run)
            except Exception:
                pass
