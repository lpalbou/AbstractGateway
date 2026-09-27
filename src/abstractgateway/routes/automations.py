"""Automations v1 HTTP façade (contract F), under `/api/gateway`.

The gateway PROJECTS runtime truth: an automation is a durable runtime root
run (`automation_id == run_id`) driven by the shipped controller bundle; its
occurrences are child runs. Nothing here schedules or executes work.

Every non-2xx answer on these paths carries
`{"detail": {"reason_code", "message", "field"?, "command_id"?}}`
(`automation_errors.py`).
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from ..automation_attention import (
    AttentionCursorError,
    attention_store_for,
    format_attention_cursor,
    parse_attention_cursor,
)
from ..automation_errors import AutomationError
from ..service import get_gateway_service
from .gateway import _principal_from_request

router = APIRouter(prefix="/gateway", tags=["automations"])


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def load_automation_controller(svc: Any, automation_id: str) -> Any:
    """The controller run of `automation_id` in THIS principal's store, or 404.

    Another principal's automation is absent from this store (the plane is
    selected per principal), so it reads as `automation_not_found`.
    """
    run = svc.runner.run_store.load(str(automation_id))
    meta = (run.vars or {}).get("_meta") if run is not None and isinstance(run.vars, dict) else None
    if not (isinstance(meta, dict) and isinstance(meta.get("automation"), dict)):
        raise AutomationError(404, "automation_not_found", f"Automation {automation_id} does not exist.")
    return run


def automation_attention_seq(controller: Any) -> int:
    """The latest attention seq the controller allocated (`_runtime.automation.attention_seq`, contract A)."""
    runtime_ns = (controller.vars or {}).get("_runtime")
    state = runtime_ns.get("automation") if isinstance(runtime_ns, dict) else None
    seq = state.get("attention_seq", 0) if isinstance(state, dict) else 0
    return int(seq) if isinstance(seq, int) and not isinstance(seq, bool) else 0


# ---------------------------------------------------------------------------
# Trigger sources
# ---------------------------------------------------------------------------


@router.get("/trigger-sources")
async def list_trigger_sources() -> Dict[str, Any]:
    """Every trigger source the runtime registry knows (contract C/F).

    Built-ins missing or broken raise (a required seam: 500 `internal_error`);
    a broken third-party source is listed `available: false` with a reason.
    """
    from abstractruntime.triggers.registry import trigger_sources

    items: List[Dict[str, Any]] = []
    for row in trigger_sources():
        descriptor = row.get("descriptor")
        if isinstance(descriptor, dict):
            item = {**descriptor, "available": bool(row.get("available"))}
        else:
            item = {"id": str(row.get("name") or ""), "available": False}
        if row.get("unavailable_reason"):
            item["unavailable_reason"] = str(row["unavailable_reason"])
        items.append(item)
    return {"items": items}


# ---------------------------------------------------------------------------
# Attention: per-principal seen cursor
# ---------------------------------------------------------------------------


class SeenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attention_cursor: str


@router.post("/automations/{automation_id}/seen")
async def mark_automation_seen(request: Request, automation_id: str, req: SeenRequest) -> Dict[str, Any]:
    """Record that this principal has seen attention items up to `attention_cursor`.

    Monotonic: an older cursor is ignored and the stored (newer) one returned.
    Clients send the cursor of the last item they DISPLAYED, never the
    summary's latest, so items they did not show stay unseen. A cursor beyond
    the automation's latest attention item is refused (422 invalid_request).
    Pending human waits are not cleared by `/seen`.
    """
    principal = _principal_from_request(request)
    svc = get_gateway_service()
    controller = load_automation_controller(svc, automation_id)
    try:
        seq = parse_attention_cursor(req.attention_cursor)
    except AttentionCursorError as e:
        raise AutomationError(422, "invalid_request", str(e), field="attention_cursor")
    latest = automation_attention_seq(controller)
    if seq > latest:
        raise AutomationError(
            422,
            "invalid_request",
            f"attention_cursor {req.attention_cursor} is beyond the automation's latest attention item ({format_attention_cursor(latest)}).",
            field="attention_cursor",
        )
    stored, _changed = attention_store_for(svc, principal).mark_seen(str(controller.run_id), seq)
    return {"attention_cursor": format_attention_cursor(stored)}


__all__ = ["automation_attention_seq", "load_automation_controller", "router"]
