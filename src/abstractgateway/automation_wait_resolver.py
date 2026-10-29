"""A revised automation resolves the tool approvals it now covers (operator QA 2026-10-29).

The operator set a waiting automation's tools to "Run without asking" in the web console and
saved; the occurrence stayed "waiting for you". The desktop app resumed such waits itself at save
time, which only covered that one client. The rule lives here, once, for every client (web
console, Observer, desktop, iPhone, CLI): after an `automation.revise` is APPLIED, each
tool-approval wait of that automation's runs whose every tool the revised definition now grants
is answered "approved", by the person who saved the revision (the command's `client_id`), as a
durable `resume` command in the same command store every answer goes through. A wait still
partly blocked stays; `still_asks(...)` names the tools that keep it waiting.

What "granted" means is the runtime's own rule, computed with its own functions on a copy of the
target's inputs: `apply_named_tool_policy` for a definition with per-tool approvals
(`policy.tool_policy`), `grant_tool_approval` for `tool_approval: "auto"`. Tools the runtime
never grants (message-sending tools, MCP tools on untrusted input) therefore never resolve a wait.
"""

from __future__ import annotations

import copy
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Set

logger = logging.getLogger(__name__)

__all__ = ["granted_tools", "resolve_covered_waits", "still_asks"]


def granted_tools(definition: Mapping[str, Any], *, untrusted_input: bool = False) -> Set[str]:
    """The tools an occurrence of ``definition`` runs without asking (the controller's grant)."""
    from abstractruntime.automations.controller import (
        apply_named_tool_policy,
        definition_untrusted_input_tools,
        grant_tool_approval,
    )

    policy = definition.get("policy") if isinstance(definition.get("policy"), Mapping) else {}
    target = definition.get("target") if isinstance(definition.get("target"), Mapping) else {}
    data = copy.deepcopy(dict(target.get("input_data") or {}))
    named = policy.get("tool_policy")
    approval = str(policy.get("tool_approval") or "auto")
    if isinstance(named, Mapping):
        apply_named_tool_policy(data, dict(named), untrusted_input=untrusted_input)
    elif approval == "auto" or untrusted_input:
        grant_tool_approval(
            data,
            untrusted_input=untrusted_input,
            named_tools=definition_untrusted_input_tools(definition),
            approval="ask" if approval != "auto" else "auto",
        )
    else:
        return set()
    grant = (data.get("_runtime") or {}).get("tool_policy") or {}
    return set(grant.get("auto_approve_tools") or []) - set(grant.get("require_approval_tools") or [])


def _call_names(wait: Mapping[str, Any]) -> List[str]:
    calls = wait.get("details") if isinstance(wait.get("details"), list) else []
    return [str((c or {}).get("name") or "") for c in calls if isinstance(c, Mapping) and (c or {}).get("name")]


def _untrusted(run_store: Any, run_id: str) -> bool:
    try:
        run = run_store.load(run_id)
    except Exception:  # noqa: BLE001
        run = None
    rt = (run.vars or {}).get("_runtime") if run is not None and isinstance(run.vars, dict) else {}
    pol = (rt or {}).get("tool_policy") if isinstance((rt or {}).get("tool_policy"), dict) else {}
    return (rt or {}).get("untrusted_input") is True or pol.get("untrusted_input") is True


def still_asks(definition: Mapping[str, Any], wait: Mapping[str, Any], *, untrusted_input: bool = False) -> List[str]:
    """The tools of a tool-approval wait the definition does not grant (sorted, unique)."""
    granted = granted_tools(definition, untrusted_input=untrusted_input)
    return sorted({name for name in _call_names(wait) if name not in granted})


def resolve_covered_waits(run_store: Any, command_store: Any, automation_id: str, *, actor: Optional[str]) -> List[Dict[str, Any]]:
    """Answer "approved" every tool-approval wait of ``automation_id`` its definition now grants.

    Returns the waits resolved. Idempotent: the resume command's id is derived from the
    automation, the definition's revision and the wait key, so a replayed revise answers nothing
    twice (the command store refuses the duplicate id)."""
    from abstractruntime.automations.attention import pending_waits
    from abstractruntime.automations.ledger import definition_of
    from abstractruntime.storage.commands import CommandRecord

    run = run_store.load(str(automation_id))
    if run is None:
        return []
    definition = definition_of(run)
    resolved: List[Dict[str, Any]] = []
    for wait in pending_waits(run_store, str(automation_id)):
        if str(wait.get("kind") or "") != "tool_approval":
            continue
        names = _call_names(wait)
        if not names:
            continue
        if still_asks(definition, wait, untrusted_input=_untrusted(run_store, str(wait.get("run_id") or ""))):
            continue
        digest = hashlib.sha256(f"{automation_id}|{definition.get('revision')}|{wait.get('wait_key')}".encode()).hexdigest()[:24]
        record = CommandRecord(
            command_id=f"revise-grants:{digest}",
            run_id=str(wait["run_id"]),
            type="resume",
            payload={"wait_key": str(wait["wait_key"]), "payload": {"approved": True}},
            ts=datetime.now(timezone.utc).isoformat(),
            client_id=actor,
            seq=0,
        )
        try:
            res = command_store.append(record)
        except Exception:  # noqa: BLE001 - the wait stays, answerable by hand
            logger.warning("automation %s: could not answer the wait %s its revision grants", automation_id, wait.get("wait_key"), exc_info=True)
            continue
        if getattr(res, "accepted", True):
            resolved.append(dict(wait))
            logger.info(
                "automation %s revision %s grants %s: wait %s of run %s answered approved for %s",
                automation_id, definition.get("revision"), ", ".join(sorted(set(names))), wait.get("wait_key"), wait.get("run_id"), actor or "the owner",
            )
    return resolved
