"""A visitor's tool choices, scoped to one durable entity visit.

These choices only narrow the home's grant. Approval is a persisted runtime
wait, never a blocking thread or a client-side promise. Legacy visits without
this namespace retain their existing behavior.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Optional
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict

SESSION_TOOLS_KEY = "entity_session_tools"
MODES = frozenset({"off", "auto", "approve"})
_approved_batch: ContextVar[Optional[tuple[str, str]]] = ContextVar("entity_approved_batch", default=None)


class SessionToolPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    modes: Optional[Dict[str, str]] = None


def open_session(host: Any, name: str, *, session_id: Optional[str], modes: Any,
                 conversation: Optional[bool] = None) -> dict:
    """Open or rejoin through the existing durable visit host, with a local policy."""
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
        raise ValueError("A managed visit needs a session_id of 1–128 letters, digits, underscores or hyphens.")
    policy = normalize_policy(modes, options=tool_options(host._registry, name))
    live = host.status(name)
    if live.get("open") and live.get("session_id") == session_id:
        _er, run, _wf, _manifest = host._load_visit(name, live["run_id"], need_spec=False)
        require_owner(run)
        current = ((run.vars or {}).get("_runtime") or {}).get("conversation") or {}
        if conversation is not None and current.get("enabled", False) != conversation:
            raise ValueError("Conversation mode is fixed when a visit opens; close it before changing this choice.")
        return session_state(host, name, run.run_id)
    owner = principal_owner()
    options = {"conversation": conversation} if conversation is not None else {}
    opened = host.open(name, participants=["person:" + owner["user_id"]],
                       session_id=session_id, session_tools=policy, **options)
    return session_state(host, name, opened["run_id"])


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
    if (runtime.get("conversation") or {}).get("enabled") is True:
        # ASK_USER is a runtime interaction effect, not an executable home tool.
        runtime["allowed_tools"].append("ask_user")


def require_owner(run: Any) -> dict:
    from .entity_visits import VisitRefused

    policy = ((run.vars or {}).get("_runtime") or {}).get(SESSION_TOOLS_KEY)
    if not isinstance(policy, dict) or policy.get("owner") != principal_owner():
        raise VisitRefused(404, "No managed visit for this account.", code="visit_not_found")
    return policy


def guard_managed_visit(run: Any) -> None:
    if SESSION_TOOLS_KEY in ((run.vars or {}).get("_runtime") or {}):
        require_owner(run)


def _participant(run: Any) -> str:
    """The authenticated visitor, never a speaker claimed in request prose."""
    from .entity_visits import VisitRefused

    guard_managed_visit(run)
    speaker = "person:" + principal_owner()["user_id"]
    participants = (((run.vars or {}).get("_runtime") or {}).get("entity") or {}).get("participants") or []
    if speaker not in participants:
        raise VisitRefused(404, "This conversation does not belong to this visitor.", code="visit_not_found")
    return speaker


def _has_private_fence(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_has_private_fence(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_private_fence(item) for item in value)
    return isinstance(value, str) and re.search(r"```\s*diary\b|<\s*diary\b|<\s*think\b", value, flags=re.I) is not None


def _public_question_history(records_by_run: dict, runs: list, root_id: str) -> tuple[list, int]:
    """Project existing ASK_USER interactions, never the home's generic messages."""
    from abstractruntime.replay import replay

    sources, keys, identities = set(), set(), set()
    for rid, records in records_by_run.items():
        for record in records:
            if (record.get("effect") or {}).get("type") != "ask_user" or record.get("status") != "waiting":
                continue
            wait = (record.get("result") or {}).get("wait") or {}
            key = wait.get("wait_key")
            if not isinstance(key, str) or not key:
                continue
            sources.add((rid, record.get("step_id")))
            keys.add((rid, key))
            question = (wait.get("details") or {}).get("question") or {}
            if question.get("id"):
                identities.add((rid, question["id"]))
    if not keys:
        return [], 0
    view = replay(records_by_run, {run.run_id: run for run in runs}, root_run_id=root_id).to_json()
    history, withheld = [], 0
    fields = ("id", "text", "run_id", "role", "kind", "source", "step_ids", "seqs", "order",
              "wait_key", "question", "choices", "question_id", "reply_status", "reason")
    sessions = {run.run_id: run.session_id for run in runs}
    for message in view["messages"]:
        kind, rid = message.get("kind"), message.get("run_id")
        if kind == "question":
            public = ((rid, message.get("wait_key")) in keys and message.get("source") == "wait"
                      and any((rid, step) in sources for step in message.get("step_ids") or []))
        elif kind == "answer":
            public = (rid, message.get("wait_key")) in keys and message.get("source") == "resume"
        elif kind == "question_outcome":
            public = (rid, (message.get("question") or {}).get("id")) in identities
        elif kind == "retained_reply":
            public = (rid, message.get("question_id")) in identities and message.get("reply_status") == "retained"
        else:
            continue
        if not public:
            continue
        row = {key: copy.deepcopy(message[key]) for key in fields if key in message}
        row["session_id"] = sessions.get(rid)
        row["published_at"] = (row.get("order") or [None])[0]
        if _has_private_fence(row):
            withheld += 1
            continue
        history.append(row)
    return history[-64:], withheld + max(0, len(history) - 64)


def public_conversation(host: Any, name: str, run_id: str) -> dict:
    """Safe explicit-public projection. Never return the home ledger/diary."""
    from abstractruntime.core.questions import question_of

    er, run, _wf, _manifest = host._load_visit(name, run_id, need_spec=False)
    _participant(run)
    runs, cursor = [run], 0
    while cursor < len(runs) and cursor < 32:
        parent = runs[cursor]
        cursor += 1
        lookup = getattr(er.run_store, "list_children", None)
        if callable(lookup):
            known = {r.run_id for r in runs}
            for child in lookup(parent_run_id=parent.run_id):
                if child.run_id not in known:
                    runs.append(child)
                    known.add(child.run_id)
    updates, guidance, pending, records_by_run = {}, {}, None, {}
    for owned in runs[:32]:
        records_by_run[owned.run_id] = er.ledger_store.list(owned.run_id)
        for record in records_by_run[owned.run_id]:
            effect = record.get("effect") or {}
            envelope = effect.get("payload") or {}
            body = envelope.get("payload") or {}
            published_at = record.get("ended_at") or record.get("started_at") or ""
            order = [published_at, owned.run_id, record.get("seq") or 0]
            if (record.get("status") == "completed" and effect.get("type") == "emit_event"
                    and envelope.get("name") == "user_guidance" and isinstance(body, dict)
                    and record.get("node_id") == "__host_user_guidance__"
                    and type(body.get("seq")) is int and body["seq"] > 0
                    and body.get("status") == "accepted"
                    and body.get("id") == f"{owned.run_id}:guidance:{body['seq']}"
                    and record.get("idempotency_key") == f"system:user_guidance:{body.get('seq')}"
                    and body.get("run_id") == owned.run_id and isinstance(body.get("id"), str)
                    and isinstance(body.get("text"), str)):
                guidance[body["id"]] = {"id": body["id"], "text": body["text"], "role": "user", "kind": "guidance",
                                        "run_id": owned.run_id, "session_id": owned.session_id,
                                        "speaker": body.get("speaker"), "guidance_status": "accepted",
                                        "published_at": published_at, "order": order}
            if (record.get("status") != "completed" or effect.get("type") != "emit_event"
                    or envelope.get("name") != "public_update" or not isinstance(body, dict)
                    or body.get("run_id") != owned.run_id):
                continue
            text, identity = body.get("text"), body.get("id")
            if not isinstance(text, str) or not text.strip() or not isinstance(identity, str) or not identity:
                continue
            if re.search(r"```\s*diary\b|<\s*diary\b|<\s*think\b", text, flags=re.I):
                continue
            identity = identity if identity.startswith(owned.run_id + ":") else owned.run_id + ":" + identity
            calls = body.get("source_call_ids")
            # The producer types the update; only an explicit "finding" is one, as in the replay view.
            updates[identity] = {"id": identity, "text": text, "run_id": owned.run_id,
                                 "update_kind": "finding" if body.get("kind") == "finding" else "progress",
                                 "session_id": owned.session_id, "iteration": body.get("iteration"),
                                 "published_at": published_at, "order": order,
                                 "source_call_ids": [v for v in calls if isinstance(v, str)] if isinstance(calls, list) else []}
        question = question_of(owned.waiting)
        if question is not None and pending is None:
            pending = {"run_id": owned.run_id, "wait_key": owned.waiting.wait_key, "reason": "user",
                       "prompt": owned.waiting.prompt, "choices": owned.waiting.choices,
                       "allow_free_text": owned.waiting.allow_free_text, "until": owned.waiting.until,
                       "details": {"question": copy.deepcopy(question)}}
    rows = sorted(updates.values(), key=lambda row: row["order"])
    # Who wrote the updates (framework ADR 0052 §3): the identity the visit opened with, stamped at
    # serve time; no field when the visit carries none (a ref is never fabricated).
    from abstractruntime.identity.digital_identity import read_run_identity_ref

    visit_ref = read_run_identity_ref(run.vars or {})
    if visit_ref is not None:
        rows = [{**row, "identity_ref": visit_ref.to_dict()} for row in rows]
    human = sorted(guidance.values(), key=lambda row: row["order"])
    questions, questions_omitted = _public_question_history(records_by_run, runs[:32], run_id)
    return {"public_updates": rows[-64:], "public_updates_omitted": max(0, len(rows) - 64),
            "public_messages": human[-64:], "public_messages_omitted": max(0, len(human) - 64),
            "public_questions": questions, "public_questions_omitted": questions_omitted,
            "conversation_runs_omitted": max(0, len(runs) - 32), "pending_question": pending}


def guide_visit(host: Any, name: str, run_id: str, *, text: str, display_text: Optional[str] = None) -> dict:
    from .entity_visits import VisitRefused
    from .steering import attach_steer_store

    er, run, _wf, manifest = host._load_visit(name, run_id, need_spec=False)
    speaker = _participant(run)
    host._refuse_if_paused(manifest, verb="take a correction")
    attach_steer_store(er.runtime, er.home.home_dir)
    try:
        seq = er.runtime.queue_visit_guidance(run_id, text=text, speaker=speaker, display_text=display_text)
    except (ValueError, PermissionError) as exc:
        raise VisitRefused(409, str(exc), code="guidance_not_pending") from exc
    return {"accepted": True, "seq": seq, "run_id": run_id}


def answer_question(host: Any, name: str, run_id: str, *, wait_key: str, response: str,
                    target_run_id: Optional[str] = None) -> dict:
    from abstractruntime.core.questions import question_of
    from .entity_visits import VisitRefused

    er, run, wf, manifest = host._load_visit(name, run_id)
    _participant(run)
    host._refuse_if_paused(manifest, verb="answer a question")
    if target_run_id is None:
        pending = public_conversation(host, name, run_id).get("pending_question")
        if isinstance(pending, dict):
            target_run_id = pending["run_id"]
    target = run
    if target_run_id and target_run_id != run_id:
        target = er.run_store.load(target_run_id)
        ancestor, seen = target, set()
        while ancestor is not None and ancestor.run_id != run_id and ancestor.run_id not in seen:
            seen.add(ancestor.run_id)
            ancestor = er.run_store.load(ancestor.parent_run_id) if ancestor.parent_run_id else None
        if ancestor is None or ancestor.run_id != run_id:
            raise VisitRefused(404, "No question in this conversation.", code="question_not_found")
        registry = er.runtime.workflow_registry
        wf = registry.get(target.workflow_id) if registry is not None else None
        if wf is None:
            raise VisitRefused(409, "This question's workflow cannot be resumed here.", code="question_workflow_missing")
    if not isinstance(response, str) or not response.strip():
        raise VisitRefused(422, "Please provide an answer.", code="empty_question_answer")
    question = question_of(target.waiting)
    if question is None and not wait_key.startswith("question:"):
        raise VisitRefused(409, "This is not a conversational question.", code="question_not_found")
    if question and wait_key != target.waiting.wait_key and not wait_key.startswith("question:"):
        raise VisitRefused(409, "This question has changed.", code="stale_question")
    state = host._resume(er, wf, target.run_id, payload={"response": response},
                         max_steps=0, wait_key=wait_key, manifest=manifest)
    # Drive the same owned runtime through the canonical lease after committing.
    if str(getattr(state.status, "value", state.status)) == "running":
        host._drive(er, wf, state.run_id, max_steps=200)
    out = {"accepted": True, "run_id": run_id, "target_run_id": target.run_id}
    out.update(public_conversation(host, name, run_id))
    return out


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
        public_action = None
        if "public_action" in payload:
            from abstractruntime.integrations.abstractcore.effect_handlers import approval_public_action

            public_action = approval_public_action(run, payload, calls, allowed)
        approved = _approved_batch.get() == (run.run_id, _fingerprint(calls))
        needs_approval = any(modes.get(_call_name(c)) == "approve" for c in allowed)
        if needs_approval and not approved:
            wait_key = build_tool_approval_wait_key(run_id=run.run_id, node_id=run.current_node,
                effect_seq=payload.get("act_seq"), tool_calls=calls)
            details = {"mode": "approval_required", "tool_calls": copy.deepcopy(allowed),
                       "entity_session_tools": True, "original_payload": copy.deepcopy(payload)}
            if public_action is not None:
                details["public_action"] = public_action
            return EffectOutcome.waiting(WaitState(reason=WaitReason.USER, wait_key=wait_key,
                resume_to_node=default_next_node, result_key=effect.result_key,
                prompt="Allow these tools for this step?", choices=["Approve", "Deny"],
                details=details))
        if not allowed:
            return EffectOutcome.completed({"mode": "executed", "results": [refusal(c, "Tool is off for this conversation.") for c in calls]})
        forwarded = {**payload, "tool_calls": allowed}
        forwarded.pop("public_action", None)
        if public_action is not None:
            forwarded["public_action"] = public_action
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
    if SESSION_TOOLS_KEY in ((run.vars or {}).get("_runtime") or {}):
        policy = require_owner(run)
    else:
        _participant(run)
        policy = {}
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
        pending = {"run_id": run_id, "reason": str(getattr(wait.reason, "value", wait.reason)), "wait_key": wait.wait_key,
                   "prompt": wait.prompt, "choices": wait.choices, "allow_free_text": wait.allow_free_text,
                   "until": wait.until, "details": details}
    out = {"run_id": run.run_id, "session_id": run.session_id, "status": status, "turn_n": turn_n,
            "reply": reply, "turn_complete": idle and turn_n > 0, "ready_for_input": idle,
            "pending_wait": pending, "tool_policy": {"modes": dict(policy.get("modes") or {})},
            "error": str(getattr(run, "error", "") or "") if status == "failed" else None}
    conversation = ((run.vars or {}).get("_runtime") or {}).get("conversation") or {}
    if conversation.get("enabled") is True:
        out.update(public_conversation(host, name, run_id))
        if out.get("pending_question") is not None:
            out["pending_wait"] = out["pending_question"]
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


def end_session(host: Any, name: str, run_id: str, *, reason: str = "Visitor ended this conversation.") -> dict:
    from abstractruntime.identity.visit_workflow import VISITOR_WAIT_KEY

    er, run, _wf, manifest = host._load_visit(name, run_id, need_spec=False)
    require_owner(run)
    status = str(getattr(run.status, "value", run.status))
    if status in ("completed", "failed", "cancelled"):
        return session_state(host, name, run_id)
    if status == "waiting" and run.waiting is not None and run.waiting.wait_key == VISITOR_WAIT_KEY:
        host.close(name, run_id, closed_by="pause", reason=reason)
    else:
        lease = host._acquire_lease(er)
        try:
            run = er.run_store.load(run_id)
            require_owner(run)
            state = er.runtime.cancel_run(run_id, reason=reason, cancelled_by="visitor")
            host._finalize_terminal(er, manifest, run_id, state)
        finally:
            if lease is not None:
                lease.release()
    return session_state(host, name, run_id)
