"""A visitor's tool choices, scoped to one durable entity visit.

These choices only narrow the home's grant. Approval is a persisted runtime
wait, never a blocking thread or a client-side promise. Legacy visits without
this namespace retain their existing behavior.
"""
from __future__ import annotations

import copy
import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Optional
from fastapi import HTTPException, Request

SESSION_TOOLS_KEY = "entity_session_tools"
MODES = frozenset({"off", "auto", "approve"})
_approved_batch: ContextVar[Optional[tuple[str, str]]] = ContextVar("entity_approved_batch", default=None)


def principal_owner() -> Dict[str, str]:
    from .security.principal import current_gateway_principal, local_admin_principal

    principal = current_gateway_principal() or local_admin_principal()
    return {"tenant_id": principal.tenant_id, "user_id": principal.user_id}


def tool_options(registry: Any, name: str) -> list[dict]:
    from abstractruntime import resolve_tool_grant
    from .entity_visits import _entity_tool_declarations
    from .mail.runtime_wiring import entity_email_tool_specs

    manifest = registry.manifest_for(name)
    grant = resolve_tool_grant(registry.entities_dir / manifest.slug, "visit")
    declarations = _entity_tool_declarations()
    offered = {n: declarations[n] for n in grant.tools if n in declarations}
    for row in entity_email_tool_specs(manifest.slug):
        offered[row["name"]] = row
    return [{"name": name, "description": row.get("description", ""), "approval_default": "auto"}
            for name, row in sorted(offered.items())]


def normalize_policy(modes: Any, *, options: list[dict], owner: Optional[dict] = None) -> Dict[str, Any]:
    names = {row["name"] for row in options}
    if modes is None:
        values = {name: "auto" for name in names}
    elif not isinstance(modes, dict) or len(modes) > 512:
        raise ValueError("Tool modes must be an object of tool names and off, auto or approve.")
    else:
        values = {}
        for name, mode in modes.items():
            if not isinstance(name, str) or name not in names:
                raise ValueError(f"Tool {name!r} is not available for this entity.")
            if not isinstance(mode, str) or mode not in MODES:
                raise ValueError(f"Invalid mode for {name}: choose off, auto or approve.")
            values[name] = mode
    return {"version": 1, "owner": dict(owner or principal_owner()), "modes": values}


def install_policy(vars_: dict, policy: dict) -> None:
    runtime = vars_.setdefault("_runtime", {})
    runtime[SESSION_TOOLS_KEY] = copy.deepcopy(policy)
    # The model sees the same restricted tools that the execution gate allows.
    runtime["allowed_tools"] = sorted(name for name, mode in policy["modes"].items() if mode != "off")


def require_owner(run: Any) -> dict:
    from .entity_visits import VisitRefused

    policy = ((run.vars or {}).get("_runtime") or {}).get(SESSION_TOOLS_KEY)
    if not isinstance(policy, dict) or policy.get("owner") != principal_owner():
        raise VisitRefused(404, "No managed visit for this account.", code="visit_not_found")
    return policy


def guard_managed_visit(run: Any) -> None:
    if SESSION_TOOLS_KEY in ((run.vars or {}).get("_runtime") or {}):
        require_owner(run)


def managed_visit_http_guard(request: Request) -> None:
    """Protect old HTTP entry points without disabling trusted janitor ticks."""
    name, run_id = request.path_params.get("name"), request.path_params.get("run_id")
    if name is None or run_id is None or "/visit/" not in request.url.path:
        return
    from .entity_visits import VisitRefused
    from .routes.entities import _visit_host

    try:
        _er, run, _wf, _manifest = _visit_host()._load_visit(str(name), str(run_id), need_spec=False)
    except (VisitRefused, KeyError):
        # Let the original endpoint retain its structured missing/stamp
        # refusal contract; only a known managed run needs the new guard.
        return
    try:
        guard_managed_visit(run)
    except VisitRefused as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc


def _call_name(call: Any) -> str:
    if not isinstance(call, dict):
        return ""
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    return str(call.get("name") or fn.get("name") or "").strip().lower()


def _fingerprint(calls: list) -> str:
    return hashlib.sha256(json.dumps(calls, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def refusal(call: Any, message: str) -> dict:
    call = call if isinstance(call, dict) else {}
    return {"call_id": call.get("call_id") or call.get("id"), "runtime_call_id": call.get("runtime_call_id"),
            "name": _call_name(call), "success": False, "output": None, "error": message,
            "executed": False, "blocked": True}


@contextmanager
def approve_batch(run_id: str, calls: list):
    """Server-owned capability for exactly the verified pending batch."""
    token = _approved_batch.set((run_id, _fingerprint(calls)))
    try:
        yield
    finally:
        _approved_batch.reset(token)


def wrap_entity_tool_handler(delegate: Any):
    def handler(run: Any, effect: Any, default_next_node: Any = None):
        from abstractruntime.core.event_keys import build_tool_approval_wait_key
        from abstractruntime.core.models import Effect, WaitReason, WaitState
        from abstractruntime.core.runtime import EffectOutcome

        runtime = (run.vars or {}).get("_runtime") or {}
        if SESSION_TOOLS_KEY not in runtime:
            return delegate(run, effect, default_next_node)
        policy = runtime.get(SESSION_TOOLS_KEY)
        modes = policy.get("modes") if isinstance(policy, dict) else None
        # A malformed persisted restriction never becomes an unrestricted grant.
        modes = modes if isinstance(modes, dict) else {}
        payload = dict(effect.payload or {})
        calls = payload.get("tool_calls")
        if not isinstance(calls, list):
            return EffectOutcome.failed("tool_calls must be a list")
        allowed = [c for c in calls if modes.get(_call_name(c), "off") in ("auto", "approve")]
        approved = _approved_batch.get() == (run.run_id, _fingerprint(calls))
        needs_approval = any(modes.get(_call_name(c)) == "approve" for c in allowed)
        if needs_approval and not approved:
            wait_key = build_tool_approval_wait_key(run_id=run.run_id, node_id=run.current_node,
                effect_seq=payload.get("act_seq"), tool_calls=calls)
            return EffectOutcome.waiting(WaitState(reason=WaitReason.USER, wait_key=wait_key,
                resume_to_node=default_next_node, result_key=effect.result_key,
                prompt="Allow these tools for this step?", choices=["Approve", "Deny"],
                details={"mode": "approval_required", "tool_calls": copy.deepcopy(allowed),
                         "entity_session_tools": True, "original_payload": copy.deepcopy(payload)}))
        if not allowed:
            return EffectOutcome.completed({"mode": "executed", "results": [refusal(c, "Tool is off for this conversation.") for c in calls]})
        forwarded = {**payload, "tool_calls": allowed}
        # The delegate still re-resolves the home's grant and mailbox switch.
        outcome = delegate(run, Effect(type=effect.type, payload=forwarded, result_key=effect.result_key), default_next_node)
        result = outcome.result
        if isinstance(result, dict) and isinstance(result.get("results"), list):
            rows = iter(result["results"])
            result = {**result, "results": [next(rows) if modes.get(_call_name(c), "off") in ("auto", "approve")
                                             else refusal(c, "Tool is off for this conversation.") for c in calls]}
            return EffectOutcome.completed(result)
        return outcome
    return handler


def session_state(host: Any, name: str, run_id: str) -> dict:
    from abstractruntime.identity.visit_workflow import VISITOR_WAIT_KEY

    er, run, _wf, _manifest = host._load_visit(name, run_id, need_spec=False)
    policy = require_owner(run)
    status = str(getattr(run.status, "value", run.status))
    wait = getattr(run, "waiting", None)
    visit = (run.vars or {}).get("_visit") or {}
    idle = status == "waiting" and wait is not None and wait.wait_key == VISITOR_WAIT_KEY
    turn_n = int(visit.get("turn_n") or 0)
    reply = None
    if idle and turn_n > 0:
        for message in reversed(visit.get("history") or []):
            if isinstance(message, dict) and message.get("role") == "assistant":
                reply = str(message.get("content") or "")
                break
    pending = None
    if status == "waiting" and wait is not None and not idle:
        details = wait.details or {}
        # Do not send the internal replay payload to clients.
        details = {k: v for k, v in details.items() if k != "original_payload"}
        pending = {"reason": str(getattr(wait.reason, "value", wait.reason)), "wait_key": wait.wait_key,
                   "prompt": wait.prompt, "details": details}
    out = {"run_id": run.run_id, "session_id": run.session_id, "status": status, "turn_n": turn_n,
            "reply": reply, "turn_complete": idle and turn_n > 0, "ready_for_input": idle,
            "pending_wait": pending, "tool_policy": {"modes": dict(policy.get("modes") or {})},
            "error": str(getattr(run, "error", "") or "") if status == "failed" else None}
    if idle and turn_n > 0:
        from .message_stats import visit_message_stats

        try:
            out["stats"] = visit_message_stats(er.ledger_store, run.run_id)
        except Exception:
            # Optional observability must not turn a completed answer into a failed turn.
            out["stats"] = None
    return out


def decide(host: Any, name: str, run_id: str, *, wait_key: str, approved: bool) -> dict:
    """Verify, execute and resume inside the same home lease; replay cannot execute twice."""
    from abstractruntime.core.models import Effect, EffectType
    from .entity_visits import VisitRefused

    er, _run, wf, manifest = host._load_visit(name, run_id)
    host._refuse_if_paused(manifest, verb="approve tools")
    lease = host._acquire_lease(er)
    try:
        run = er.run_store.load(run_id)
        policy = require_owner(run)
        wait = getattr(run, "waiting", None)
        details = wait.details if wait is not None else None
        if (str(getattr(run.status, "value", run.status)) != "waiting" or wait is None
                or wait.wait_key != wait_key or not isinstance(details, dict)
                or details.get("entity_session_tools") is not True):
            raise VisitRefused(409, "This approval is no longer pending. Refresh the conversation.", code="approval_not_pending")
        payload = details.get("original_payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("tool_calls"), list):
            raise VisitRefused(409, "This approval cannot be replayed safely.", code="invalid_approval")
        calls = payload["tool_calls"]
        receipt = policy.get("last_decision")
        if isinstance(receipt, dict) and receipt.get("wait_key") == wait_key:
            if receipt.get("state") != "completed" or not isinstance(receipt.get("result"), dict):
                raise VisitRefused(409, "The previous tool decision was interrupted. Its outcome is unknown; end this conversation before starting another.", code="decision_outcome_unknown")
            result = copy.deepcopy(receipt["result"])
        elif approved:
            # Write intent BEFORE side effects. After a process failure an
            # interrupted decision is never blindly executed a second time.
            policy["last_decision"] = {"wait_key": wait_key, "state": "executing"}
            er.run_store.save(run)
            with approve_batch(run_id, calls):
                effect = Effect(type=EffectType.TOOL_CALLS, payload=payload, result_key=wait.result_key)
                # Use the already-composed handler: its identity/stamp wall is
                # just as mandatory on approval as on the original effect.
                outcome = er.runtime._handlers[EffectType.TOOL_CALLS](run, effect, wait.resume_to_node)
            if str(getattr(outcome.status, "value", outcome.status)) != "completed":
                raise VisitRefused(409, "The tool batch could not complete safely.", code="tool_execution_failed")
            result = dict(outcome.result or {})
        else:
            result = {"mode": "executed", "results": [refusal(c, "Denied by user.") for c in calls]}
        policy["last_decision"] = {"wait_key": wait_key, "state": "completed", "result": copy.deepcopy(result)}
        # Persist delegate-owned counters/tag maps before Runtime.resume reloads the run.
        er.run_store.save(run)
        from .entity_visits import _TURN_MAX_TICKS

        er.runtime.resume(workflow=wf, run_id=run_id, wait_key=wait_key, payload=result, max_steps=_TURN_MAX_TICKS)
    finally:
        if lease is not None:
            lease.release()
    return session_state(host, name, run_id)


def end_session(host: Any, name: str, run_id: str) -> dict:
    from abstractruntime.identity.visit_workflow import VISITOR_WAIT_KEY

    er, run, _wf, manifest = host._load_visit(name, run_id, need_spec=False)
    require_owner(run)
    status = str(getattr(run.status, "value", run.status))
    if status in ("completed", "failed", "cancelled"):
        return session_state(host, name, run_id)
    if status == "waiting" and run.waiting is not None and run.waiting.wait_key == VISITOR_WAIT_KEY:
        host.close(name, run_id, closed_by="pause", reason="Visitor ended this conversation.")
    else:
        lease = host._acquire_lease(er)
        try:
            run = er.run_store.load(run_id)
            require_owner(run)
            state = er.runtime.cancel_run(run_id, reason="Visitor ended this conversation.", cancelled_by="visitor")
            host._finalize_terminal(er, manifest, run_id, state)
        finally:
            if lease is not None:
                lease.release()
    return session_state(host, name, run_id)
