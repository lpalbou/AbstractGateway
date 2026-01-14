"""Run Gateway runner worker (AbstractGateway).

Backlog: 307-Framework: Durable Run Gateway (Command Inbox + Ledger Stream)

Key properties (v0):
- Commands are accepted by being appended to a durable JSONL inbox (idempotent by command_id).
- A background worker polls the inbox and applies commands to persisted runs.
- A tick loop progresses RUNNING runs by calling Runtime.tick(...) and appending StepRecords.
- Clients render by replaying the durable ledger (cursor/offset semantics), not by relying on live RPC.
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Protocol

from abstractruntime import JsonFileCommandCursorStore, JsonlCommandStore, Runtime
from abstractruntime.core.event_keys import build_event_wait_key
from abstractruntime.core.models import RunStatus, WaitReason
from abstractruntime.storage.commands import CommandRecord


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
    tick_workers: int = 2
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
    ) -> None:
        self._base_dir = Path(base_dir)
        self._host = host
        self._cfg = config or GatewayRunnerConfig()
        self._enable = bool(enable)

        self._command_store = JsonlCommandStore(self._base_dir)
        self._cursor_store = JsonFileCommandCursorStore(self._base_dir / "commands_cursor.json")

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
    def command_store(self) -> JsonlCommandStore:
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
                from abstractruntime.scheduler.scheduler import utc_now_iso

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
        if typ not in {"pause", "resume", "cancel", "emit_event"}:
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

        state = runtime.tick(workflow=wf, run_id=run_id, max_steps=int(self._cfg.tick_max_steps or 100))

        # If this run completed, it may unblock a parent WAITING(SUBWORKFLOW).
        if getattr(state, "status", None) == RunStatus.COMPLETED:
            try:
                self._resume_subworkflow_parents(child_run_id=run_id, child_output=state.output or {})
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
