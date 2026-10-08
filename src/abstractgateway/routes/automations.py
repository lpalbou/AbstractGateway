"""Automations v1 HTTP façade (contract F), under `/api/gateway`.

The gateway PROJECTS runtime truth: an automation is a durable runtime root
run (`automation_id == run_id`) driven by the shipped controller bundle; its
occurrences are child runs. Nothing here schedules or executes work.

Every non-2xx answer on these paths carries
`{"detail": {"reason_code", "message", "field"?, "command_id"?}}`
(`automation_errors.py`).
"""

from __future__ import annotations

import copy
import datetime
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

# Runtime seams (automations contracts A-E): imported directly, so a runtime
# without them fails at import, never silently.
from abstractruntime.automation_queries import ChangedSinceUnsupported, InvalidCursor, automation_summary, list_automations
from abstractruntime.automations.attention import list_attention, normalize_occurrence_output, pending_waits
from abstractruntime.automations.commands import command_digest
from abstractruntime.automations.ledger import automation_records, find_by_idempotency_key, record_key, record_payload
from abstractruntime.automations.models import (
    AUTOMATION_STATUSES,
    AutomationError as RuntimeAutomationError,
    automation_id_for,
    discussion_ids,
    automation_session_id,
    automation_status,
    revise_definition,
)
from abstractruntime.automations.service import (
    adopt_legacy_schedule_projection,
    create_automation,
    get_automation,
    list_occurrences,
    start_discussion,
)
from abstractruntime.core.models import RunStatus
from abstractruntime.core.run_attribution import filter_values
from abstractruntime.session_history import SessionHistoryError
from abstractruntime.session_turns import OccurrenceNotInSession
from abstractruntime.storage.commands import CommandRecord

from ..automation_attention import (
    AttentionCursorError,
    attention_store_for,
    format_attention_cursor,
    parse_attention_cursor,
)
from ..automation_command_types import AUTOMATION_COMMAND_TYPES, AUTOMATION_SUMMARY_CAPABILITIES
from ..automation_defaults import (
    AutomationDefaultsError,
    manifest_automation_defaults,
    strip_server_owned_input,
    validate_flow_automation_defaults,
)
from ..automation_errors import AutomationError
from ..automation_pause_reasons import clear_pause_reason, pause_reason
from ..automation_schedule import first_run_sentence, is_schedule_v2, owner_time_zone, schedule_fields, trigger_time_zone, with_time_zone
from ..run_retention import resolve_gateway_run_workspace, write_gateway_workspace_marker
from ..run_workspace_guard import guard_run_vars
from ..service import get_gateway_service
from .gateway import (
    DEFAULT_AGENT_SENTINEL,
    _off_the_event_loop,
    _parse_any_string_list,
    _principal_from_request,
    _require_bundle_host,
    _resolve_bundle_from_host,
    _resolve_default_agent_or_409,
    _sanitize_run_workspace_policy,
    _split_bundle_ref,
    _strip_client_workflow_policy,
    _workflow_gate,
)

router = APIRouter(prefix="/gateway", tags=["automations"])
logger = logging.getLogger(__name__)


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




# ---------------------------------------------------------------------------
# PART 2: the façade over abstractruntime.automations
# ---------------------------------------------------------------------------

_STATUS_BY_REASON = {
    "automation_not_found": 404,
    "occurrence_not_found": 404,
    "revision_conflict": 409,
    "automation_busy": 409,
    "invalid_state": 409,
    "identity_conflict": 409,
    "cursor_expired": 409,
    "history_unavailable": 409,
}
_ITEMS_MAX = 20
_EXCERPT_MAX = 280


def _domain_error(exc: RuntimeAutomationError, *, command_id: Optional[str] = None) -> AutomationError:
    reason = str(getattr(exc, "reason_code", "") or "invalid_definition")
    return AutomationError(_STATUS_BY_REASON.get(reason, 422), reason, str(exc), field=getattr(exc, "field", None), command_id=command_id)


def _principal_tuple(principal: Any) -> tuple[str, str]:
    """The (tenant, user) an automation id is derived from (same tuple as the seen store)."""
    return str(getattr(principal, "tenant_id", None) or "default"), str(getattr(principal, "user_id", None) or "")


def _nudge(svc: Any, run_id: str) -> None:
    svc.runner.start()
    svc.runner.nudge(str(run_id))


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------- summaries


def _interval_label(every: Any) -> str:
    """`8h` -> "every 8 hours" (the Automation panel's wording; fixed UTC intervals)."""
    words = {"s": ("second", "seconds"), "m": ("minute", "minutes"), "h": ("hour", "hours"), "d": ("day", "days")}
    text = str(every or "")
    unit = text[-1:]
    amount = text[:-1]
    if unit not in words or not amount.isdigit():
        return f"every {text}"
    n = int(amount)
    return f"every {words[unit][0]}" if n == 1 else f"every {n} {words[unit][1]}"


def _trigger_summary(envelope: Dict[str, Any], definition_trigger: Dict[str, Any]) -> str:
    source_id = str(envelope.get("source_id") or "")
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    if source_id == "manual":
        return f"manual: run now ({payload.get('command_id')})"
    if source_id == "schedule":
        config = definition_trigger.get("config") if isinstance(definition_trigger.get("config"), dict) else {}
        if is_schedule_v2(definition_trigger):
            from ..automation_schedule import rule_text

            zone = trigger_time_zone(definition_trigger, "UTC")
            return f"schedule: {rule_text(definition_trigger, zone)}, tick {payload.get('tick')}"
        cadence = f"{_interval_label(config['every'])} (UTC)" if config.get("every") else "once"
        return f"schedule: {cadence}, tick {payload.get('tick')}"
    return f"{source_id}@{envelope.get('source_version')}"


def _live_status(status: str, has_human_wait: bool) -> str:
    """The ledger status; a running occurrence reads `waiting` only while it
    (or a run below it) waits on a PERSON (a typed `pending_waits` entry:
    ask_user, tool_approval, event). An occurrence root parked on its agent
    subworkflow is still running."""
    if status == "running" and has_human_wait:
        return "waiting"
    return status


def _triggers_by_revision(ledger_store: Any, automation_id: str) -> Dict[int, Dict[str, Any]]:
    """{revision: the trigger binding in force at that revision}, from the
    automation's `created` / `revised` ledger records (the definition each
    one committed)."""
    out: Dict[int, Dict[str, Any]] = {}
    for rec in automation_records(ledger_store, str(automation_id), "automation.created", "automation.revised"):
        definition = rec["payload"]["definition"]
        out[int(definition["revision"])] = definition["trigger"]
    return out


def _occurrence_answer(child: Any, artifact_store: Any) -> Dict[str, Any]:
    if child is None or child.status not in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
        return {"answer": "", "error": None}
    return normalize_occurrence_output(child, artifact_store=artifact_store)


def _attention_block(svc: Any, principal: Any, controller: Any, waits: List[Dict[str, Any]]) -> Dict[str, Any]:
    latest = automation_attention_seq(controller)
    seen = attention_store_for(svc, principal).seen_seq(str(controller.run_id))
    page = list_attention(svc.host.ledger_store, str(controller.run_id), after_seq=seen, limit=_ITEMS_MAX)
    items = [_attention_item(i) for i in page["items"]]
    unseen = max(latest - seen, len(items), 0)
    return {
        "pending_waits": len(waits),
        "unread": unseen > 0,
        "unseen_count": unseen,
        "cursor": format_attention_cursor(latest),
        "items": items,
        # Typed waits (decision D1): {run_id, wait_key, kind, reason, index,
        # prompt?, choices?, details?} exactly as the runtime types them.
        "waits": [dict(w) for w in waits[:_ITEMS_MAX]],
    }


def _attention_item(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {k: raw[k] for k in ("kind", "automation_id", "run_id", "index", "at", "title", "body", "cursor") if raw.get(k) is not None}


def _capabilities(status: str) -> List[str]:
    if status == "archived":
        return ["unarchive", "discuss"]
    if status in ("completed", "failed"):
        return ["discuss"]
    return [c for c in AUTOMATION_SUMMARY_CAPABILITIES if c != "unarchive"]


def principal_time_zone(svc: Any, principal: Any) -> str:
    """The automation owner's (= this principal's) time zone: the account preference, else this
    host's zone (R16.1)."""
    tenant, user = _principal_tuple(principal)
    root_data_dir = getattr(svc.config, "root_data_dir", None) or svc.config.data_dir
    return owner_time_zone(root_data_dir, tenant_id=tenant, user_id=user or "admin")


def automation_summary_row(svc: Any, principal: Any, controller: Any, *, owner_zone: Optional[str] = None) -> Dict[str, Any]:
    """Contract F `AutomationSummary` of one controller run."""
    runtime = svc.host.runtime
    base = automation_summary(controller)
    status = automation_status(controller)
    waits = pending_waits(svc.host.run_store, str(controller.run_id), limit=_ITEMS_MAX)
    out: Dict[str, Any] = {
        "automation_id": str(controller.run_id),
        "title": base["title"],
        "status": status,
        "trigger": base["trigger"],
        "context_mode": base["context_mode"],
        "growing_max_tokens": base["growing_max_tokens"],
        # The automation's folder (= definition.workspace_root): apps open it
        # from the row without fetching the definition.
        "workspace_root": base["workspace_root"],
        # The runtime's one projection: the next scheduled fire time of an
        # active scheduled automation (also while an occurrence runs), and the
        # occurrence in progress `{index, run_id, attempt, status}` or null.
        "next_fire_at": base["next_fire_at"],
        "current_occurrence": base["current_occurrence"],
        "occurrence_count": base["occurrence_count"],
        "attention": _attention_block(svc, principal, controller, waits),
        "legacy": False,
        "revision": base["revision"],
        "updated_at": controller.updated_at,
        "capabilities": _capabilities(status),
        "session_kind": "automation",
    }
    if out["next_fire_at"] is None:
        del out["next_fire_at"]
    # R16.1 A4: the served next run + THE sentence every client shows (never computed client-side).
    out.update(schedule_fields(base["trigger"], out.get("next_fire_at"), owner_zone or principal_time_zone(svc, principal)))
    if status == "paused":
        # Why the GATEWAY paused it (e.g. its workflow was made unavailable, DESIGN-v3 §13.4).
        reason = pause_reason(svc.config.data_dir, str(controller.run_id))
        if reason:
            out["paused_reason"] = reason
    latest = list_occurrences(runtime, str(controller.run_id), limit=1)["items"]
    if latest:
        occ = latest[0]
        child = svc.host.run_store.load(str(occ["run_id"]))
        answer = _occurrence_answer(child, svc.host.artifact_store)
        last = {
            "run_id": occ["run_id"],
            "index": occ["index"],
            "status": _live_status(str(occ["status"]), any(w.get("index") == occ["index"] for w in waits)),
            "attempts": occ["attempts"],
            "fired_at": occ["fired_at"],
            "excerpt": (answer.get("answer") or "")[:_EXCERPT_MAX],
            "notify": occ.get("notify"),
        }
        if occ.get("finished_at"):
            last["finished_at"] = occ["finished_at"]
        out["last_occurrence"] = last
    return out


def legacy_summary_row(run: Any, run_store: Any, *, owner_zone: Optional[str] = None) -> Dict[str, Any]:
    """A legacy `scheduled:*` wrapper root, projected read-only (`legacy: true`)."""
    legacy = adopt_legacy_schedule_projection(run)
    config = dict(legacy["trigger"].get("config") or {})
    children = sorted(
        list(run_store.list_children(parent_run_id=str(run.run_id)) or []),
        key=lambda c: (str(c.created_at or ""), str(c.run_id)),
    )
    out: Dict[str, Any] = {
        "automation_id": legacy["automation_id"],
        "title": legacy["title"],
        "status": legacy["status"],
        # No binding exists for a legacy schedule: its root run id stands in.
        "trigger": {"binding_id": str(run.run_id), "source_id": "schedule", "source_version": 1, "config": config},
        "context_mode": legacy["context_mode"],
        "occurrence_count": len(children),
        "attention": {"pending_waits": 0, "unread": False, "unseen_count": 0, "cursor": format_attention_cursor(0), "items": [], "waits": []},
        "legacy": True,
        "revision": None,
        "updated_at": legacy["updated_at"],
        "capabilities": ["legacy"],
        "session_kind": "automation",
    }
    legacy_ws = (run.vars or {}).get("workspace_root") if isinstance(run.vars, dict) else None
    if isinstance(legacy_ws, str) and legacy_ws.strip():
        out["workspace_root"] = legacy_ws
    if run.status == RunStatus.WAITING and run.waiting is not None and run.waiting.until:
        out["next_fire_at"] = run.waiting.until
    if owner_zone is None:
        from ..automation_schedule import host_time_zone

        owner_zone = host_time_zone()
    out.update(schedule_fields(out["trigger"], out.get("next_fire_at"), owner_zone))
    if children:
        # The wrapper's latest child is its last occurrence (index = ordinal).
        child = children[-1]
        terminal = child.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED)
        answer = normalize_occurrence_output(child) if terminal else {"answer": ""}
        last: Dict[str, Any] = {
            "run_id": str(child.run_id),
            "index": len(children),
            "status": child.status.value,
            "attempts": 1,
            "fired_at": child.created_at,
            "excerpt": (answer.get("answer") or "")[:_EXCERPT_MAX],
            "notify": None,
        }
        if terminal:
            last["finished_at"] = child.updated_at
        out["last_occurrence"] = last
    return out


# ------------------------------------------------------------------ targets


def _resolve_target(svc: Any, principal: Any, target: Any, *, field: str = "target") -> Dict[str, Any]:
    """`{bundle_ref, flow_id, input_data?}` or `{flow_id:"@default", interface, input_data?}`
    -> the concrete `{workflow_id, bundle_ref, flow_id, input_data}` this host serves."""
    if not isinstance(target, dict):
        raise AutomationError(422, "invalid_definition", f"{field} must be an object", field=field)
    allowed = {"bundle_ref", "flow_id", "interface", "input_data"}
    unknown = sorted(k for k in target if k not in allowed)
    if unknown:
        raise AutomationError(422, "invalid_definition", f"unknown {field} field(s) {unknown}", field=f"{field}.{unknown[0]}")
    input_data = target.get("input_data", {})
    if not isinstance(input_data, dict):
        raise AutomationError(422, "invalid_definition", f"{field}.input_data must be an object", field=f"{field}.input_data")
    host = _require_bundle_host(svc)
    flow_id = str(target.get("flow_id") or "").strip()
    try:
        if flow_id == DEFAULT_AGENT_SENTINEL:
            if target.get("bundle_ref"):
                raise AutomationError(422, "invalid_definition", "flow_id '@default' chooses the workflow itself; do not send bundle_ref", field=f"{field}.bundle_ref")
            resolved = _resolve_default_agent_or_409(svc, principal, target.get("interface"))
            if resolved.registry_scope != "private":
                raise AutomationError(
                    422,
                    "unsupported_feature",
                    f"the default {resolved.interface} workflow is a shared-catalog workflow; automations v1 run workflows of this gateway's own registry",
                    field=f"{field}.flow_id",
                )
            bundle_id, bundle_version, flow_id = resolved.bundle_id, resolved.bundle_version, resolved.flow_id
        else:
            if "interface" in target:
                raise AutomationError(422, "invalid_definition", "interface is only used with flow_id '@default'", field=f"{field}.interface")
            if not flow_id:
                raise AutomationError(422, "invalid_definition", f"{field}.flow_id is required", field=f"{field}.flow_id")
            bundle_id, bundle_version = _split_bundle_ref(str(target.get("bundle_ref") or ""))
            if not bundle_id:
                raise AutomationError(422, "invalid_definition", f"{field}.bundle_ref is required", field=f"{field}.bundle_ref")
        selected_version, bundle = _resolve_bundle_from_host(host=host, bundle_id=bundle_id, bundle_version=bundle_version)
    except AutomationError:
        raise
    except HTTPException as e:
        raise AutomationError(422, "invalid_definition", str(e.detail if not isinstance(e.detail, dict) else e.detail.get("message") or e.detail), field=field)
    # Governance (DESIGN-v3 §5): an automation on an unavailable or archived workflow would
    # be a run start by other means (the per-app default stays exempt).
    try:
        _workflow_gate(svc, host, principal, bundle_id, selected_version, start=True, agent_default=str(target.get("flow_id") or "").strip() == DEFAULT_AGENT_SENTINEL)
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, dict) else {"reason_code": "workflow_refused", "message": str(e.detail)}
        raise AutomationError(int(e.status_code), str(detail.get("reason_code")), str(detail.get("message")), field=field)
    entry_ids = {str(getattr(ep, "flow_id", "") or "") for ep in list(bundle.manifest.entrypoints or [])}
    if flow_id not in entry_ids:
        raise AutomationError(422, "invalid_definition", f"'{bundle_id}@{selected_version}' has no entrypoint '{flow_id}'", field=f"{field}.flow_id")
    workflow_id = f"{bundle_id}@{selected_version}:{flow_id}"
    if workflow_id not in host.specs:
        raise AutomationError(422, "invalid_definition", f"workflow {workflow_id} is not loaded on this gateway", field=field)
    return {
        "workflow_id": workflow_id,
        "bundle_ref": f"{bundle_id}@{selected_version}",
        "flow_id": flow_id,
        "input_data": dict(input_data),
        "_bundle": bundle,
    }


def _guarded_input_data(svc: Any, principal: Any, input_data: Dict[str, Any], *, automation_id: str) -> Dict[str, Any]:
    """The target's input data under the gateway's run protections, frozen into
    the definition: client workspace knobs clamped to the operator policy, the
    automation's gateway-owned workspace, and the built-in tool deny rule
    (occurrences and discussions are started by the runtime, so they carry
    the protection in their inputs rather than through `host.start_run`)."""
    from ..automation_workspace_resolver import FOLLOW_DEFAULT, follows_default, strip_derived

    raw_input = dict(input_data)
    # "Use my default" (round 13): no payload and no narrowing from the client, `workspace:
    # {"configured": false}`, or a pre-rule definition echoed back with the gateway's derived keys.
    # The automation then FOLLOWS its owner's default at each run (automation_workspace_resolver);
    # the derived keys a client echoes (an old snapshot) are ignored, never a narrowing.
    following = follows_default(raw_input) or (
        raw_input.get("workspace") is None
        and raw_input.get("workspace_allowed_paths") is None
        and raw_input.get("workspaceAllowedPaths") is None
        and str(raw_input.get("workspace_access_mode") or "").strip().lower() != "workspace_only"
    )
    data = _strip_client_workflow_policy(copy.deepcopy(raw_input))
    if following:
        strip_derived(data)
    # Server-owned keys never come from a client (reviews 47 P2-1, 52 R52-1):
    # `_meta.*`, the read-only mount, and every `_runtime` key outside the
    # client allowlist (`CLIENT_RUNTIME_KEYS`).
    dropped = strip_server_owned_input(data)
    if dropped:
        logger.warning("automation %s: dropped server-owned input keys %s from the client's target input", automation_id, dropped)
    session_id = automation_session_id(automation_id)
    try:
        data = _sanitize_run_workspace_policy(data, principal=principal, session_id=session_id)
    except HTTPException as e:
        message = e.detail.get("message") if isinstance(e.detail, dict) else e.detail
        raise AutomationError(422, "invalid_definition", str(message), field="target.input_data")
    tenant, user = _principal_tuple(principal)
    root_data_dir = getattr(svc.config, "root_data_dir", None) or svc.config.data_dir
    # Round 11: the automation's workspaces are ONE object, `input_data.workspace` {posture,
    # default_mode, folders} (absent = the account default, frozen at creation like every protected
    # input). A round-9 definition's `workspace_allowed_paths` list (already checked above) becomes
    # "Deny everything, allow listed workspaces" with each workspace at its gateway cap.
    if not following and not isinstance(data.get("workspace"), dict) and isinstance(dict(input_data).get("workspace_allowed_paths"), (list, str)):
        from ..workspace_policy import caps_for, legacy_paths_to_layer

        data["workspace"] = legacy_paths_to_layer(
            caps_for(Path(str(root_data_dir)), tenant_id=tenant or "default", user_id=user or "admin"),
            _parse_any_string_list(dict(input_data).get("workspace_allowed_paths")),
        )
    chosen_workspace = copy.deepcopy(data.get("workspace")) if isinstance(data.get("workspace"), dict) else None
    # The automation's context mode is the ONE history control (operator
    # 2026-09-28): the runtime replays prior occurrences into
    # `context.messages` for "growing" and none for "independent"; the flow
    # always reads what it is given. A client `use_context` (the flow pin,
    # default False on basic-agent) silently dropped a growing automation's
    # history and a discussion's seed, so it is server-owned here.
    data["use_context"] = True
    # The registered user's email, the one address the send_email recipient
    # refiner treats as "self" (framework backlog 0992 WP0). The automation
    # grant never pre-approves send_email, so without this value an
    # occurrence's mail to its own owner would wait for approval. Chat runs
    # get it from `host.start_run`; occurrences are started by the runtime,
    # so it is frozen here with the rest of the protected inputs (a client
    # value was dropped above; a revision of the target refreshes it). No
    # registered email = key absent = every send asks.
    # Same value as the user's email settings (registered email, else the connected mailbox's
    # own address: mail/accounts.py `self_address`, 0.7.0 E2E F1a).
    from ..mail.accounts import plane_for_service_config, self_address

    runtime_ns = data.get("_runtime") if isinstance(data.get("_runtime"), dict) else {}
    try:
        operator_email = self_address(plane_for_service_config(svc.config))
    except Exception:  # noqa: BLE001 - no plane: no "self" (every send asks)
        operator_email = ""
    if isinstance(operator_email, str) and operator_email:
        runtime_ns["operator_email"] = operator_email
    if runtime_ns:
        data["_runtime"] = runtime_ns
    guard_run_vars(
        data,
        data_dir=svc.config.data_dir,
        root_data_dir=root_data_dir,
        session_id=session_id,
        tenant_id=tenant,
        user_id=user,
    )
    if following:
        # No snapshot of the default in the definition: the runtime resolves it at each
        # occurrence's admission. What stays is FAIL-CLOSED (the built-in deny rule and the
        # automation's own workspace only), for a runtime that would run it without the resolver.
        for key in ("workspace_allowed_paths", "workspace_read_only_paths", "workspace_ignored_paths", "_gateway_workspace"):
            data.pop(key, None)
        from abstractruntime.utils.workspace_paths import WRITABLE_PATHS_KEY

        data.pop(WRITABLE_PATHS_KEY, None)
        data["workspace_access_mode"] = "workspace_only"
        data["workspace"] = dict(FOLLOW_DEFAULT)
    elif chosen_workspace is not None:
        # Kept in the definition (clients show and edit it); the guard above applied it, clamped.
        data["workspace"] = chosen_workspace
    return data


# ------------------------------------------------------------------- routes


class CreateAutomationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    title: Optional[str] = None
    target: Dict[str, Any]
    trigger: Optional[Dict[str, Any]] = None
    context: Optional[Dict[str, Any]] = None
    policy: Optional[Dict[str, Any]] = None
    # Where attention items are delivered (definition schema v2): {"channels": ["console"] |
    # ["console", "email"]} — validated by the runtime like an edit's `notify`.
    notify: Optional[Dict[str, Any]] = None


def _create(svc: Any, principal: Any, body: CreateAutomationBody) -> Dict[str, Any]:
    tenant, user = _principal_tuple(principal)
    target = _resolve_target(svc, principal, body.target)
    bundle = target.pop("_bundle")
    title, trigger, context = body.title, body.trigger, body.context
    if trigger is None or title is None:
        # A request may lean on the target's published defaults (contract C6),
        # re-checked by the same validator that checked them at save time.
        raw_defaults = manifest_automation_defaults(bundle.manifest).get(target["flow_id"])
        if raw_defaults is None:
            missing = "trigger" if trigger is None else "title"
            raise AutomationError(422, "invalid_request", f"{missing} is required (the target publishes no automation_defaults)", field=missing)
        try:
            defaults = validate_flow_automation_defaults(raw_defaults)
        except AutomationDefaultsError as e:
            raise AutomationError(422, e.reason_code, f"the target's published automation_defaults are invalid: {e}", field=e.field)
        trigger = trigger if trigger is not None else defaults["trigger"]
        context = context if context is not None else defaults["context"]
        target["input_data"] = {**defaults["input_data"], **target["input_data"]}
        title = title if title is not None else defaults.get("title")
        if not title:
            raise AutomationError(422, "invalid_request", "title is required", field="title")
    automation_id = automation_id_for(tenant=tenant, user=user, request_id=str(body.request_id))
    # R16.1 A2: a schedule@2 rule without a time zone runs on the OWNER's account time zone.
    owner_zone = principal_time_zone(svc, principal)
    trigger = with_time_zone(trigger, owner_zone)
    target["input_data"] = _guarded_input_data(svc, principal, target["input_data"], automation_id=automation_id)
    request: Dict[str, Any] = {
        "request_id": body.request_id,
        "tenant": tenant,
        "user": user,
        "title": title,
        "target": target,
        "trigger": trigger,
        "workspace_root": target["input_data"]["workspace_root"],
    }
    if context is not None:
        request["context"] = context
    if body.policy is not None:
        request["policy"] = body.policy
    if body.notify is not None:
        request["notify"] = body.notify
    runtime = svc.host.runtime
    try:
        # Gateway-owned from its creation: the runner ticks only actor
        # "gateway" runs, and children inherit the actor (review 47 P2-3).
        created_id, revision = create_automation(runtime, request, actor_id="gateway")
    except RuntimeAutomationError as e:
        raise _domain_error(e)
    _nudge(svc, created_id)
    if str((trigger or {}).get("source_id") or "") == "email.received":
        # The mail watcher reads nothing until an email automation exists and its first read is a
        # baseline (mail already there is history): take it now, not at the next 15 s tick.
        worker = getattr(svc, "email_worker", None)
        if worker is not None:
            worker.nudge()
    controller = load_automation_controller(svc, created_id)
    return {"automation_id": created_id, "revision": revision, "summary": automation_summary_row(svc, principal, controller, owner_zone=owner_zone)}


@router.post("/automations")
async def create_automation_route(request: Request, body: CreateAutomationBody) -> Dict[str, Any]:
    """Create an automation (same `request_id` + same request -> the same automation)."""
    principal = _principal_from_request(request)
    svc = get_gateway_service()
    out = await _off_the_event_loop(_create, svc, principal, body)
    _audit_automation(request, automation_id=str((out or {}).get("automation_id") or ""), command="automation.create")
    return out


class SchedulePreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trigger: Dict[str, Any]


def _schedule_preview(svc: Any, principal: Any, body: SchedulePreviewBody, now_iso: Optional[str] = None) -> Dict[str, Any]:
    """What a trigger WOULD do if saved now: the normalized binding (time zone filled exactly as a
    create fills it), the sentences and the first run — computed by the runtime adapter, never by
    the dialog. Refusals are the create's 422 shape (same validator)."""
    from abstractruntime.automations.models import validate_trigger_request
    from abstractruntime.automations.controller import adapter_kind
    from abstractruntime.triggers.registry import get_trigger_adapter

    now_iso = now_iso or _now_iso()
    owner_zone = principal_time_zone(svc, principal)
    trigger = with_time_zone(body.trigger, owner_zone)
    try:
        binding = validate_trigger_request(trigger, now=now_iso, binding_id="preview")
    except RuntimeAutomationError as e:
        raise _domain_error(e)
    adapter = get_trigger_adapter(binding["source_id"], binding["source_version"])
    next_run_at: Optional[str] = None
    if adapter_kind(adapter) == "time":
        wait = adapter.prepare(binding, state=adapter.initial_state(binding["config"]), now=now_iso)
        if wait.get("kind") == "until":
            next_run_at = str(wait["until"])
    fields = schedule_fields(binding, next_run_at, owner_zone)
    now_dt = datetime.datetime.fromisoformat(now_iso)
    zone = fields["time_zone"]
    return {
        "trigger": {k: binding[k] for k in ("source_id", "source_version", "config")},
        "time_zone": zone,
        "schedule_rule_text": fields["schedule_rule_text"],
        "schedule_text": fields["schedule_text"],
        "next_run_at": fields.get("next_run_at"),
        "next_run_local": fields.get("next_run_local"),
        "first_run_sentence": first_run_sentence(binding, zone, next_run_at, now=now_dt),
    }


@router.post("/automations/schedule-preview")
async def schedule_preview_route(request: Request, body: SchedulePreviewBody) -> Dict[str, Any]:
    """Preview a trigger before saving it (R16.1): `{trigger, time_zone, schedule_rule_text,
    schedule_text, next_run_at, next_run_local, first_run_sentence}`. Nothing is stored."""
    principal = _principal_from_request(request)
    svc = get_gateway_service()
    return await _off_the_event_loop(_schedule_preview, svc, principal, body)


def _audit_automation(request: Request, *, automation_id: str, command: str) -> None:
    """Name the automation and command on this request's audit line (`automation`
    {automation_id, command}): the account Logs read it (account_activity.py)."""
    try:
        detail = getattr(request.state, "audit_detail", None)
        detail = dict(detail) if isinstance(detail, dict) else {}
        detail["automation"] = {"automation_id": automation_id, "command": command}
        request.state.audit_detail = detail
    except Exception:  # noqa: BLE001 - auditing never breaks the request
        pass


def _archived_count(svc: Any, principal: Any, run_store: Any) -> int:
    """How many of this principal's automations are archived (legacy roots included)."""
    n = len(list_automations(run_store, status="archived", limit=1_000_000).items)
    rows = run_store.list_run_index(role="legacy_schedule", limit=100_000)
    for row in rows:
        if str(row.get("parent_run_id") or "").strip():
            continue
        run = run_store.load(str(row["run_id"]))
        if run is not None and legacy_summary_row(run, run_store)["status"] == "archived":
            n += 1
    return n


def _list(svc: Any, principal: Any, status: Optional[str], cursor: Optional[str], limit: int, archived_only: bool = False) -> Dict[str, Any]:
    if archived_only and status:
        raise AutomationError(422, "invalid_request", "archived_only and status cannot be combined", field="archived_only")
    wanted = filter_values(status) if status else None
    if wanted is not None:
        unknown = sorted(s for s in wanted if s not in AUTOMATION_STATUSES)
        if unknown or not wanted:
            raise AutomationError(422, "invalid_request", f"unknown status {unknown or status!r} (expected {'|'.join(AUTOMATION_STATUSES)})", field="status")
    elif archived_only:
        wanted = ["archived"]
    else:
        # Archived automations leave the default listing (archived_only=true lists them).
        wanted = [s for s in AUTOMATION_STATUSES if s != "archived"]
    run_store = svc.host.run_store
    owner_zone = principal_time_zone(svc, principal)
    try:
        # The status filter rides the runtime's page so a page is never short of what it hides.
        page = list_automations(run_store, status=list(wanted), cursor=cursor, limit=limit)
    except InvalidCursor as e:
        raise AutomationError(422, "invalid_request", str(e), field="cursor")
    items: List[Dict[str, Any]] = []
    for row in page.items:
        controller = run_store.load(str(row["automation_id"]))
        try:
            summary = automation_summary_row(svc, principal, controller, owner_zone=owner_zone)
        except (RuntimeAutomationError, LookupError, ValueError, KeyError, TypeError) as e:
            # One malformed row must never take the whole list down (review 47 P2-1).
            logger.warning("automations list: skipping malformed automation %s: %s", row.get("automation_id"), e)
            continue
        if wanted is None or summary["status"] in wanted:
            items.append(summary)
    if page.next_cursor is None:
        # Legacy scheduled roots ride the LAST page: a client paging through
        # every page sees each of them exactly once.
        rows = run_store.list_run_index(role="legacy_schedule", limit=100_000)
        rows = [r for r in rows if not str(r.get("parent_run_id") or "").strip()]
        rows.sort(key=lambda r: (str(r.get("created_at") or ""), str(r.get("run_id") or "")), reverse=True)
        for row in rows:
            run = run_store.load(str(row["run_id"]))
            if run is None:
                continue
            summary = legacy_summary_row(run, run_store, owner_zone=owner_zone)
            if wanted is None or summary["status"] in wanted:
                items.append(summary)
    return {"items": items, "next_cursor": page.next_cursor, "archived_automations": _archived_count(svc, principal, run_store)}


@router.get("/automations")
async def list_automations_route(
    request: Request,
    status: Optional[str] = Query(None, description="Comma-separated statuses: active|paused|completed|failed|archived."),
    cursor: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    changed_since: Optional[str] = Query(None, description="Not supported in v1 (422 unsupported_feature): poll full pages."),
    archived_only: bool = Query(False, description="If true, list only archived automations. Without it (and without `status`), archived automations are left out. The response carries `archived_automations` (how many this principal has archived)."),
) -> Dict[str, Any]:
    """Every automation of this principal, newest first, as full pages; legacy
    `scheduled:*` roots follow on the last page with `legacy: true`. Archived
    automations are listed only with `archived_only=true` (or an explicit
    `status` naming `archived`)."""
    principal = _principal_from_request(request)
    if changed_since is not None:
        raise AutomationError(422, "unsupported_feature", str(ChangedSinceUnsupported()), field="changed_since")
    svc = get_gateway_service()
    return await _off_the_event_loop(_list, svc, principal, status, cursor, limit, bool(archived_only))


def _get(svc: Any, principal: Any, automation_id: str) -> Dict[str, Any]:
    controller = load_automation_controller(svc, automation_id)
    detail = get_automation(svc.host.run_store, str(controller.run_id))
    return {
        "definition": detail["definition"],
        "active_revision": detail["active_revision"],
        "summary": automation_summary_row(svc, principal, controller),
    }


@router.get("/automations/{automation_id}")
async def get_automation_route(request: Request, automation_id: str) -> Dict[str, Any]:
    principal = _principal_from_request(request)
    svc = get_gateway_service()
    return await _off_the_event_loop(_get, svc, principal, automation_id)


def _append_command(svc: Any, *, automation_id: str, command_id: str, type_: str, payload: Dict[str, Any], client_id: Optional[str]) -> Dict[str, Any]:
    record = CommandRecord(
        command_id=str(command_id),
        run_id=str(automation_id),
        type=type_,
        payload=dict(payload),
        ts=_now_iso(),
        client_id=client_id,
        seq=0,
    )
    res = svc.runner.command_store.append(record)
    try:
        svc.runner.nudge(str(automation_id))
    except Exception:  # noqa: BLE001 - the runner's poll applies it anyway
        pass
    return {"command_id": str(command_id), "accepted": bool(res.accepted), "duplicate": bool(res.duplicate), "seq": int(res.seq)}


def _check_revise(svc: Any, principal: Any, controller: Any, payload: Dict[str, Any], *, command_id: str, decided: bool = False) -> Dict[str, Any]:
    """Door checks for a revise: stale `expected_revision` (409) and invalid
    changes (422) are refused before the command is queued; the runtime
    re-checks both when it applies the command. A `target` change is resolved
    to a concrete, protected target here, like at creation. `decided`: this
    command_id was already applied, so the retry is answered as a duplicate
    and the revision it moved past is not a conflict."""
    definition = (controller.vars.get("_meta") or {})["automation"]
    expected = payload.get("expected_revision")
    if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int)):
        raise AutomationError(422, "invalid_request", "expected_revision must be an integer", field="expected_revision", command_id=command_id)
    if not decided and expected is not None and int(expected) != int(definition["revision"]):
        raise AutomationError(409, "revision_conflict", f"Automation is at revision {definition['revision']}, not {expected}.", field="expected_revision", command_id=command_id)
    changes = payload.get("changes")
    if not isinstance(changes, dict) or not changes:
        raise AutomationError(422, "invalid_request", "changes must be a non-empty object", field="changes", command_id=command_id)
    changes = dict(changes)
    if "trigger" in changes and is_schedule_v2(changes["trigger"]):
        # R16.1 A2: a revised schedule@2 rule without a time zone keeps the binding's zone (an
        # older binding without one: the owner's account time zone).
        old = definition.get("trigger") if isinstance(definition.get("trigger"), dict) else {}
        keep = (old.get("config") or {}).get("time_zone") if is_schedule_v2(old) else None
        changes["trigger"] = with_time_zone(changes["trigger"], keep or principal_time_zone(svc, principal))
    if "target" in changes:
        target = _resolve_target(svc, principal, changes["target"], field="changes.target")
        target.pop("_bundle")
        target["input_data"] = _guarded_input_data(svc, principal, target["input_data"], automation_id=str(controller.run_id))
        changes["target"] = target
    try:
        revise_definition(definition, changes, automation_id=str(controller.run_id), now=_now_iso())
    except RuntimeAutomationError as e:
        raise _domain_error(e, command_id=command_id)
    out = {"changes": changes}
    if expected is not None:
        out["expected_revision"] = int(expected)
    return out


class PatchAutomationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str
    expected_revision: Optional[int] = None
    changes: Dict[str, Any]


class AutomationCommandBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str
    type: str
    payload: Dict[str, Any] = {}


def _refuse_at_the_door(controller: Any, type_: str, *, command_id: str) -> None:
    """Refuse synchronously (409) what the persisted state already rules out,
    instead of a 200 receipt whose rejection only reaches the ledger. The
    runtime re-checks at application (the state may move in between)."""
    state = (controller.vars.get("_runtime") or {})["automation"]
    status = automation_status(controller)
    if type_ == "automation.unarchive":
        # Back to paused, history kept (the runtime revives a controller that ended when archived).
        if status != "archived":
            raise AutomationError(409, "invalid_state", "Automation is not archived.", command_id=command_id)
        return
    if status == "archived":
        if type_ != "automation.archive":
            raise AutomationError(409, "invalid_state", "Automation is archived; only unarchive is possible.", command_id=command_id)
        return
    if status in ("completed", "failed") and type_ != "automation.archive":
        raise AutomationError(409, "invalid_state", f"Automation is {status}; only archive is possible.", command_id=command_id)
    if type_ == "automation.run_now":
        if state.get("pending_occurrence") is not None or state.get("manual_pending") is not None:
            raise AutomationError(409, "automation_busy", "An occurrence is already running or waiting to run.", command_id=command_id)
        if state.get("exhausted"):
            raise AutomationError(409, "invalid_state", "The automation's trigger is exhausted.", command_id=command_id)
    elif type_ == "automation.pause" and state.get("paused"):
        raise AutomationError(409, "invalid_state", "Automation is already paused.", command_id=command_id)
    elif type_ == "automation.resume" and not state.get("paused"):
        raise AutomationError(409, "invalid_state", "Automation is not paused.", command_id=command_id)
    elif type_ == "automation.stop_current" and state.get("pending_occurrence") is None:
        raise AutomationError(409, "invalid_state", "No occurrence is running.", command_id=command_id)



def _command(svc: Any, principal: Any, automation_id: str, command_id: str, type_: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if type_ not in AUTOMATION_COMMAND_TYPES:
        raise AutomationError(422, "invalid_request", "type must be one of " + "|".join(AUTOMATION_COMMAND_TYPES), field="type", command_id=command_id)
    if not str(command_id or "").strip():
        raise AutomationError(422, "invalid_request", "command_id is required", field="command_id")
    controller = load_automation_controller(svc, automation_id)
    payload = dict(payload or {})
    # A retried command_id that the runtime already decided is answered by the
    # command store as a duplicate. Look it up FIRST: a decided revise moved the
    # revision past its own expected_revision, and a retry of it (a client that
    # lost the first receipt) must get the duplicate receipt, not a 409.
    decided = find_by_idempotency_key(
        svc.host.ledger_store, str(controller.run_id), record_key("automation.command_result", str(controller.run_id), command_id)
    )
    if type_ == "automation.revise":
        payload = _check_revise(svc, principal, controller, payload, command_id=command_id, decided=decided is not None)
    elif decided is None:
        expected = payload.get("expected_revision")
        definition = (controller.vars.get("_meta") or {})["automation"]
        if expected is not None and expected != definition["revision"]:
            raise AutomationError(409, "revision_conflict", f"Automation is at revision {definition['revision']}, not {expected}.", field="expected_revision", command_id=command_id)
    if decided is None:
        _refuse_at_the_door(controller, type_, command_id=command_id)
    else:
        # The same id for a DIFFERENT command (same digest rule as the runtime,
        # over what the runner will hand it: payload without expected_revision).
        body = dict(payload)
        expected = body.pop("expected_revision", None)
        if record_payload(decided).get("command_digest") != command_digest(type_, body, expected):
            raise AutomationError(
                409, "identity_conflict", f"command_id {command_id!r} was already used for a different command.",
                field="command_id", command_id=command_id,
            )
    receipt = _append_command(
        svc,
        automation_id=str(controller.run_id),
        command_id=command_id,
        type_=type_,
        payload=payload,
        client_id=str(getattr(principal, "user_id", "") or "") or None,
    )
    if type_ == "automation.resume":
        clear_pause_reason(svc.config.data_dir, str(controller.run_id))
    return receipt


@router.patch("/automations/{automation_id}")
async def patch_automation_route(request: Request, automation_id: str, body: PatchAutomationBody) -> Dict[str, Any]:
    """Revise (= an `automation.revise` command through the durable command store)."""
    principal = _principal_from_request(request)
    svc = get_gateway_service()
    payload: Dict[str, Any] = {"changes": body.changes}
    if body.expected_revision is not None:
        payload["expected_revision"] = body.expected_revision
    _audit_automation(request, automation_id=automation_id, command="automation.revise")
    return await _off_the_event_loop(_command, svc, principal, automation_id, body.command_id, "automation.revise", payload)


@router.post("/automations/{automation_id}/commands")
async def automation_command_route(request: Request, automation_id: str, body: AutomationCommandBody) -> Dict[str, Any]:
    """Queue one `automation.*` command; the receipt says it was ACCEPTED (queued),
    the automation's ledger records whether it was applied or rejected."""
    principal = _principal_from_request(request)
    svc = get_gateway_service()
    _audit_automation(request, automation_id=automation_id, command=str(body.type))
    return await _off_the_event_loop(_command, svc, principal, automation_id, body.command_id, body.type, body.payload)


def _artifacts(svc: Any, run_id: str) -> List[Dict[str, Any]]:
    store = svc.host.artifact_store
    if store is None:
        return []
    out: List[Dict[str, Any]] = []
    for meta in store.list_by_run(str(run_id)):
        tags = meta.tags if isinstance(meta.tags, dict) else {}
        extra = meta.metadata if isinstance(meta.metadata, dict) else {}
        name = tags.get("filename") or extra.get("filename") or extra.get("name") or meta.artifact_id
        out.append({
            "artifact_id": meta.artifact_id,
            "name": str(name),
            "mime_type": str(meta.content_type or "application/octet-stream"),
            "url": f"/api/gateway/runs/{run_id}/artifacts/{meta.artifact_id}/content",
        })
    return out


def _occurrences(svc: Any, principal: Any, automation_id: str, cursor: Optional[str], limit: int) -> Dict[str, Any]:
    controller = load_automation_controller(svc, automation_id)
    runtime = svc.host.runtime
    triggers = _triggers_by_revision(svc.host.ledger_store, str(controller.run_id))
    try:
        page = list_occurrences(runtime, str(controller.run_id), cursor=cursor, limit=limit)
    except RuntimeAutomationError as e:
        raise _domain_error(e)
    waits_by_index: Dict[Any, List[Dict[str, Any]]] = {}
    for w in pending_waits(svc.host.run_store, str(controller.run_id), limit=1000):
        waits_by_index.setdefault(w.get("index"), []).append({k: v for k, v in w.items() if k != "index"})
    items: List[Dict[str, Any]] = []
    for occ in page["items"]:
        run_id = str(occ["run_id"])
        child = svc.host.run_store.load(run_id)
        occ_meta = ((child.vars or {}).get("_meta") or {}).get("occurrence") if child is not None else None
        envelope = (occ_meta or {}).get("trigger_envelope") if isinstance(occ_meta, dict) else None
        envelope = envelope if isinstance(envelope, dict) else {"source_id": (occ.get("trigger") or {}).get("source_id")}
        answer = _occurrence_answer(child, svc.host.artifact_store)
        status = _live_status(str(occ["status"]), bool(waits_by_index.get(occ["index"])))
        row: Dict[str, Any] = {
            "run_id": run_id,
            "index": occ["index"],
            "attempts": occ["attempts"],
            "fired_at": occ["fired_at"],
            "status": status,
            "trigger": {"source_id": envelope.get("source_id"), "summary": _trigger_summary(envelope, triggers[int(occ["revision"])])},
            "user_turn": occ.get("user_turn") or "",
            "answer": answer.get("answer") or "",
            "notify": occ.get("notify"),
            "artifacts": _artifacts(svc, run_id) if child is not None else [],
            "waits": waits_by_index.get(occ["index"], []),
            "ledger_url": f"/api/gateway/runs/{run_id}/ledger",
        }
        if occ.get("finished_at"):
            row["finished_at"] = occ["finished_at"]
        if status == "failed":
            row["failure"] = {
                "reason_code": "occurrence_failed",
                "message": str(answer.get("error") or (child.error if child is not None else "") or "failed"),
                "attempts": occ["attempts"],
            }
        if child is not None and isinstance((child.vars or {}).get("workspace_root"), str):
            row["workspace_url"] = f"/api/gateway/runs/{run_id}/workspace"
        items.append(row)
    return {"items": items, "next_cursor": page["next_cursor"]}


@router.get("/automations/{automation_id}/occurrences")
async def list_occurrences_route(
    request: Request,
    automation_id: str,
    cursor: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> Dict[str, Any]:
    """Occurrences, newest first by index (one row per logical occurrence; its latest attempt)."""
    principal = _principal_from_request(request)
    svc = get_gateway_service()
    return await _off_the_event_loop(_occurrences, svc, principal, automation_id, cursor, limit)


def _attention(svc: Any, principal: Any, automation_id: str, cursor: Optional[str], limit: int) -> Dict[str, Any]:
    controller = load_automation_controller(svc, automation_id)
    seen = attention_store_for(svc, principal).seen_seq(str(controller.run_id))
    try:
        page = list_attention(svc.host.ledger_store, str(controller.run_id), after_seq=seen, cursor=cursor, limit=limit)
    except ValueError as e:
        raise AutomationError(422, "invalid_request", str(e), field="cursor")
    return {"items": [_attention_item(i) for i in page["items"]], "next_cursor": page["next_cursor"]}


@router.get("/automations/{automation_id}/attention")
async def list_attention_route(
    request: Request,
    automation_id: str,
    cursor: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
) -> Dict[str, Any]:
    """This principal's UNSEEN attention items, oldest first, paged."""
    principal = _principal_from_request(request)
    svc = get_gateway_service()
    return await _off_the_event_loop(_attention, svc, principal, automation_id, cursor, limit)


class DiscussBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    occurrence_index: int
    prompt: str


def _discuss(svc: Any, principal: Any, automation_id: str, body: DiscussBody) -> Dict[str, Any]:
    controller = load_automation_controller(svc, automation_id)
    runtime = svc.host.runtime
    # The discussion's OWN writable workspace (operator ruling 2026-09-27):
    # the gateway-owned folder of its session, where every chat session's
    # folder lives; the automation's workspace is mounted read-only by the
    # runtime.
    session_id = discussion_ids(str(controller.run_id), str(body.request_id))["session_id"]
    tenant, user = _principal_tuple(principal)
    own_workspace, _scoped = resolve_gateway_run_workspace(svc.config.data_dir, session_id=session_id, tenant_id=tenant, user_id=user)
    try:
        out = start_discussion(
            runtime,
            automation_id=str(controller.run_id),
            occurrence_index=int(body.occurrence_index),
            request_id=str(body.request_id),
            prompt=str(body.prompt),
            workspace_root=str(own_workspace),
            actor_id="gateway",
        )
    except RuntimeAutomationError as e:
        raise _domain_error(e)
    except OccurrenceNotInSession as e:
        raise AutomationError(404, "occurrence_not_found", str(e), field="occurrence_index")
    except SessionHistoryError as e:
        raise AutomationError(409, str(getattr(e, "reason_code", "history_unavailable")), str(e))
    write_gateway_workspace_marker(own_workspace, run_id=out["run_id"], session_id=out["session_id"], session_scoped=True)
    _nudge(svc, out["run_id"])
    root = svc.host.run_store.load(out["run_id"])
    mounted = ((root.vars or {}).get("_meta") or {})["discussion"]["mounted_workspace"]
    return {
        "session_id": out["session_id"],
        "run_id": out["run_id"],
        "session_kind": "discussion",
        "workspace_root": str(own_workspace),
        "mounted_workspace": mounted,
    }


@router.post("/automations/{automation_id}/discuss")
async def discuss_route(request: Request, automation_id: str, body: DiscussBody) -> Dict[str, Any]:
    """Fork a discussion from occurrence N: a new root run in its own session,
    seeded with the automation's conversation 1..N, working in its OWN
    writable folder with the automation's folder mounted read-only. Later
    turns go through `POST /runs/start` with that `session_id` (the gateway
    re-stamps the own folder and the mount on each)."""
    principal = _principal_from_request(request)
    svc = get_gateway_service()
    return await _off_the_event_loop(_discuss, svc, principal, automation_id, body)


__all__ = ["automation_attention_seq", "automation_summary_row", "legacy_summary_row", "load_automation_controller", "router"]
