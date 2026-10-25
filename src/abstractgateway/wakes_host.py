"""The gateway's half of `SCHEDULE_RUN` (framework backlog 1045, AbstractRuntime `automations.wakes`).

A run (an agent's `schedule_run` call, a flow node) asks for work later. The runtime plans the
wake (`plan_schedule_run`: when, task or note, tools = at most the caller's, session); this host
creates it, because only the host knows:

- the TARGET: the workflow of the caller's session (its turn root's `bundle@version:flow`), so the
  wake runs the same agent, with the same provider and model;
- the OWNER: this plane's (tenant, user);
- the PROTECTIONS: the run workspace, the account's workspace policy and the built-in tool deny
  rule (`run_workspace_guard.guard_run_vars`, the guard `/runs/start` and automations apply),
  computed for the session the occurrence will run in.

It then appends `wake.scheduled` to the owner's event stream (`events_stream`, id
`wake.scheduled:<automation_id>`) and nudges the runner. The automation is an ordinary one-shot
automation (its controller sleeps, the occurrence is a new turn of the caller's session with the
`[Trigger …]` header); cancelling = archiving it, which the runner reports as `wake.cancelled`
when it never fired.
"""

from __future__ import annotations

import copy
import logging
import re
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

_BUNDLE_WORKFLOW_RE = re.compile(r"^(?P<bundle>[^@:\s]+)@(?P<version>[^:\s]+):(?P<flow>\S+)$")
# What the wake keeps of its session's agent configuration (client-level inputs only; every
# protected key is recomputed by the guard).
CARRIED_INPUTS = ("provider", "model")
_MAX_PARENT_HOPS = 64


class ScheduleRunRefused(RuntimeError):
    """The host cannot create this wake (the tool reports the message)."""


def session_root(run_store: Any, run: Any) -> Any:
    """The caller's turn root (parent chain to the top)."""
    cur = run
    for _ in range(_MAX_PARENT_HOPS):
        parent_id = str(getattr(cur, "parent_run_id", "") or "").strip()
        if not parent_id:
            return cur
        parent = run_store.load(parent_id)
        if parent is None:
            return cur
        cur = parent
    return cur


def target_for(root: Any) -> Dict[str, Any]:
    """`{workflow_id, bundle_ref, flow_id, input_data}` of the session's workflow."""
    workflow_id = str(getattr(root, "workflow_id", "") or "")
    m = _BUNDLE_WORKFLOW_RE.match(workflow_id)
    if m is None:
        raise ScheduleRunRefused(f"this session's workflow ({workflow_id or 'unknown'}) is not a published bundle workflow, so it cannot be scheduled to run again")
    vars0 = getattr(root, "vars", None) if isinstance(getattr(root, "vars", None), dict) else {}
    carried = {k: copy.deepcopy(vars0[k]) for k in CARRIED_INPUTS if isinstance(vars0.get(k), str) and vars0[k].strip()}
    return {
        "workflow_id": workflow_id,
        "bundle_ref": f"{m.group('bundle')}@{m.group('version')}",
        "flow_id": m.group("flow"),
        "input_data": carried,
    }


class GatewayScheduleRun:
    """`Runtime.set_schedule_run_handler` implementation for one plane's runtime."""

    def __init__(
        self,
        runtime: Any,
        *,
        data_dir: Path,
        root_data_dir: Path,
        tenant: str,
        user: str,
        nudge: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.runtime = runtime
        self.data_dir = Path(data_dir)
        self.root_data_dir = Path(root_data_dir)
        self.tenant = str(tenant)
        self.user = str(user)
        self.nudge = nudge

    def __call__(self, run: Any, plan: Mapping[str, Any]) -> Dict[str, Any]:
        from abstractruntime.automations.models import automation_id_for, automation_session_id
        from abstractruntime.automations.wakes import create_scheduled_run, wake_scheduled_event

        from .events_stream import stream_for
        from .run_workspace_guard import guard_run_vars

        root = session_root(self.runtime.run_store, run)
        target = target_for(root)
        automation_id = automation_id_for(tenant=self.tenant, user=self.user, request_id=str(plan["request_id"]))
        # The occurrence's session: the caller's (same_session) or the automation's own.
        occurrence_session = plan.get("session_id") or automation_session_id(automation_id)
        protected: Dict[str, Any] = dict(target["input_data"])
        protected["use_context"] = True  # the automation's context mode decides the history
        guard_run_vars(
            protected,
            data_dir=self.data_dir,
            root_data_dir=self.root_data_dir,
            session_id=str(occurrence_session),
            tenant_id=self.tenant,
            user_id=self.user,
        )
        workspace_root = protected.get("workspace_root")
        if not isinstance(workspace_root, str) or not workspace_root:
            raise ScheduleRunRefused("no workspace could be established for the scheduled run")
        target["input_data"] = protected
        created = create_scheduled_run(
            self.runtime,
            plan,
            target=target,
            tenant=self.tenant,
            user=self.user,
            workspace_root=workspace_root,
            actor_id="gateway",
        )
        aid = str(created["automation_id"])
        event = wake_scheduled_event(plan, aid)
        try:
            stream_for(self.data_dir, self.tenant, self.user).append(
                "wake.scheduled",
                event_id=f"wake.scheduled:{aid}",
                subject=aid,
                source=f"/abstractgateway/automations/{aid}",
                data=event["data"],
            )
        except Exception:  # the wake exists; a missing stream event is logged, never a lost wake
            logger.exception("wake.scheduled stream append failed for %s", aid)
        if self.nudge is not None:
            try:
                self.nudge(aid)
            except Exception:
                logger.debug("schedule_run: runner nudge failed", exc_info=True)
        return created


def wire_schedule_run(runtime: Any, *, data_dir: Path, root_data_dir: Path, tenant: str, user: str, nudge: Optional[Callable[[str], None]] = None) -> GatewayScheduleRun:
    """Register the handler on a freshly built runtime (every host build, like the email wiring).
    Fails loudly on a runtime without the seam: `schedule_run` would answer "not available"
    for every agent, silently."""
    setter = getattr(runtime, "set_schedule_run_handler", None)
    if not callable(setter):
        raise RuntimeError(
            "This AbstractRuntime has no Runtime.set_schedule_run_handler (SCHEDULE_RUN, backlog 1045): "
            "upgrade abstractruntime."
        )
    handler = GatewayScheduleRun(runtime, data_dir=data_dir, root_data_dir=root_data_dir, tenant=tenant, user=user, nudge=nudge)
    setter(handler)
    return handler


def wake_cancel_event(run_store: Any, automation_id: str) -> Optional[Dict[str, Any]]:
    """The `wake.cancelled` event of an archived automation that is a wake and never fired, else
    None (an ordinary automation, or a wake whose occurrence already started)."""
    from abstractruntime.automations.ledger import definition_of
    from abstractruntime.automations.wakes import wake_cancelled_event

    controller = run_store.load(str(automation_id))
    if controller is None:
        return None
    try:
        definition = definition_of(controller)
    except LookupError:
        return None
    if not definition.get("origin_run_id") or not definition.get("one_shot"):
        return None
    latest = getattr(run_store, "latest_occurrence_row", None)
    if callable(latest) and latest(str(automation_id)) is not None:
        return None  # it fired: archiving it now cancels nothing
    return wake_cancelled_event(
        automation_id=str(automation_id),
        session_id=definition.get("session_id"),
        origin_run_id=definition.get("origin_run_id"),
    )


__all__ = ["CARRIED_INPUTS", "GatewayScheduleRun", "ScheduleRunRefused", "session_root", "target_for", "wake_cancel_event", "wire_schedule_run"]
