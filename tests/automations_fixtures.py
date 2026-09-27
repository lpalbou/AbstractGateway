"""Shared fixtures for the Automations v1 gateway tests (not a test module).

Runs are written straight into the gateway's run store in the shapes the
runtime contracts define (A: `_meta.automation` / `_runtime.automation`;
B: `_meta.occurrence` / `_meta.discussion`; legacy `_meta.schedule`), so
the gateway's projection is tested against the contract, independent of
the controller that normally writes them.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

TOKEN = "t"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
CONTROLLER_WORKFLOW_ID = "abstractframework.automation-controller@1.0.0:controller"


def gateway_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, runner: bool = False, origins: str = "*") -> Path:
    data_dir = tmp_path / "runtime"
    flows_dir = tmp_path / "bundles"
    data_dir.mkdir(parents=True, exist_ok=True)
    flows_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows_dir))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", origins)
    monkeypatch.setenv("ABSTRACTGATEWAY_POLL_S", "0.05")
    monkeypatch.setenv("ABSTRACTGATEWAY_TICK_WORKERS", "1")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "1" if runner else "0")
    return data_dir


def _run(
    *,
    run_id: str,
    workflow_id: str,
    vars: Dict[str, Any],
    session_id: Optional[str] = None,
    parent_run_id: Optional[str] = None,
    created_at: str = "2026-09-27T10:00:00+00:00",
) -> Any:
    from abstractruntime.core.models import RunState, RunStatus

    return RunState(
        run_id=run_id,
        workflow_id=workflow_id,
        status=RunStatus.COMPLETED,
        current_node="end",
        vars=vars,
        output={"success": True},
        created_at=created_at,
        updated_at=created_at,
        session_id=session_id,
        parent_run_id=parent_run_id,
    )


def controller_run(
    automation_id: Optional[str] = None,
    *,
    title: str = "Memory monitor",
    attention_seq: int = 0,
    session_id: Optional[str] = None,
    created_at: str = "2026-09-27T09:00:00+00:00",
) -> Any:
    aid = automation_id or str(uuid.uuid4())
    definition = {
        "schema_version": 1,
        "revision": 1,
        "title": title,
        "controller": {"bundle_ref": "abstractframework.automation-controller@1.0.0", "flow_id": "controller"},
        "target": {"workflow_id": "bundle-target@0.0.0:root", "bundle_ref": "bundle-target@0.0.0", "flow_id": "root", "input_data": {}},
        "trigger": {"binding_id": str(uuid.uuid4()), "source_id": "schedule", "source_version": 1, "config": {"every": "2m"}},
        "context": {"mode": "independent", "growing": {}},
        "policy": {"serial": True, "misfire": "coalesce", "failure": "continue", "retry": {"max_attempts": 3, "backoff": {"initial": "30s", "factor": 2, "max": "10m"}}},
        "session_id": session_id or f"automation-session:{aid}",
        "workspace_root": "/tmp/ws",
        "created_at": created_at,
        "archived_at": None,
    }
    state = {"state_version": 1, "active_revision": 1, "pending_occurrence": None, "anchor": None, "tick": 0, "next_index": 1,
             "scheduled_count": 0, "paused": False, "attention_seq": int(attention_seq), "exhausted": False, "manual_pending": None,
             "last_outcome": None}
    return _run(
        run_id=aid,
        workflow_id=CONTROLLER_WORKFLOW_ID,
        vars={"_meta": {"automation": definition}, "_runtime": {"automation": state}},
        session_id=definition["session_id"],
        created_at=created_at,
    )


def occurrence_run(automation_id: str, *, index: int, session_id: str, session_kind: str = "occurrence", role: str = "occurrence",
                   parent_run_id: Optional[str] = None, created_at: str = "2026-09-27T10:00:00+00:00", attempt: int = 1) -> Any:
    return _run(
        run_id=str(uuid.uuid4()),
        workflow_id="bundle-target@0.0.0:root",
        vars={"_meta": {"occurrence": {"automation_id": automation_id, "occurrence_index": index, "attempt": attempt, "event_id": f"e{index}",
                                        "revision": 1, "role": role, "session_kind": session_kind, "fired_at": created_at,
                                        "trigger_envelope": {}}}},
        session_id=session_id,
        parent_run_id=parent_run_id or automation_id,
        created_at=created_at,
    )


def chat_run(*, session_id: str, created_at: str = "2026-09-27T08:00:00+00:00", parent_run_id: Optional[str] = None) -> Any:
    return _run(run_id=str(uuid.uuid4()), workflow_id="bundle-target@0.0.0:root", vars={}, session_id=session_id,
                parent_run_id=parent_run_id, created_at=created_at)


def discussion_run(automation_id: str, *, index: int, created_at: str = "2026-09-27T11:00:00+00:00") -> Any:
    rid = str(uuid.uuid4())
    return _run(run_id=rid, workflow_id="bundle-target@0.0.0:root",
                vars={"_meta": {"discussion": {"automation_id": automation_id, "occurrence_index": index, "revision": 1,
                                               "seed_run_id": str(uuid.uuid4()), "request_id": "r1", "discussion_root_run_id": rid}}},
                session_id=f"discussion-session:{rid}", created_at=created_at)


def legacy_schedule_run(*, created_at: str = "2026-09-27T07:00:00+00:00") -> Any:
    rid = str(uuid.uuid4())
    return _run(run_id=rid, workflow_id=f"scheduled:{uuid.uuid4()}",
                vars={"_meta": {"schedule": {"kind": "scheduled_run", "interval": "2m", "share_context": False}}},
                session_id=rid, created_at=created_at)


def save_runs(*runs: Any) -> None:
    from abstractgateway.service import get_gateway_service

    store = get_gateway_service().runner.run_store
    for run in runs:
        store.save(run)


# --------------------------------------------------------------- live target


ECHO_FLOW_ID = "echo"
ECHO_BUNDLE_ID = "automation-echo"

# Deterministic target (no provider, no tools): answers with the prompt it got
# and the history it was given, and sets `notify` when its input asks for it.
_ECHO_CODE = """msgs = (context or {}).get('messages') if isinstance(context, dict) else None
msgs = msgs if isinstance(msgs, list) else []
history = [str(m.get('role')) + ':' + str(m.get('content')) for m in msgs if isinstance(m, dict)]
answer = 'ECHO[' + str(len(history)) + '] ' + str(prompt) + ' || ' + ' | '.join(history)
out = {'response': answer, 'success': True}
if notify:
    out['notify'] = {'title': 'Echo notify', 'body': answer[:200]}
return out"""


def echo_flow() -> dict:
    return {
        "id": ECHO_FLOW_ID,
        "name": "Automation echo",
        "nodes": [
            {"id": "start", "type": "on_flow_start", "data": {"nodeType": "on_flow_start", "outputs": [
                {"id": "exec-out", "label": "", "type": "execution"},
                {"id": "prompt", "label": "prompt", "type": "string"},
                {"id": "context", "label": "context", "type": "object"},
                {"id": "notify", "label": "notify", "type": "boolean"},
            ]}},
            {"id": "code", "type": "code", "data": {"nodeType": "code", "codeBody": _ECHO_CODE, "inputs": [
                {"id": "exec-in", "label": "", "type": "execution"},
                {"id": "prompt", "label": "prompt", "type": "string"},
                {"id": "context", "label": "context", "type": "object"},
                {"id": "notify", "label": "notify", "type": "boolean"},
            ]}},
            {"id": "end", "type": "on_flow_end", "data": {"nodeType": "on_flow_end", "inputs": [
                {"id": "exec-in", "label": "", "type": "execution"},
                {"id": "result", "label": "result", "type": "object"},
            ]}},
        ],
        "edges": [
            {"source": "start", "sourceHandle": "exec-out", "target": "code", "targetHandle": "exec-in"},
            {"source": "code", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"},
            {"source": "start", "sourceHandle": "prompt", "target": "code", "targetHandle": "prompt"},
            {"source": "start", "sourceHandle": "context", "target": "code", "targetHandle": "context"},
            {"source": "start", "sourceHandle": "notify", "target": "code", "targetHandle": "notify"},
            {"source": "code", "sourceHandle": "output", "target": "end", "targetHandle": "result"},
        ],
        "entryNode": "start",
    }


def write_echo_bundle(bundles_dir: Path, *, automation_defaults: Optional[dict] = None) -> str:
    import json
    import zipfile

    bundles_dir.mkdir(parents=True, exist_ok=True)
    metadata: Dict[str, Any] = {}
    if automation_defaults is not None:
        metadata["automation_defaults"] = {ECHO_FLOW_ID: automation_defaults}
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": ECHO_BUNDLE_ID,
        "bundle_version": "1.0.0",
        "created_at": "2026-09-27T00:00:00+00:00",
        "entrypoints": [{"flow_id": ECHO_FLOW_ID, "name": "echo", "description": "", "interfaces": []}],
        "flows": {ECHO_FLOW_ID: f"flows/{ECHO_FLOW_ID}.json"},
        "artifacts": {},
        "assets": {},
        "metadata": metadata,
    }
    with zipfile.ZipFile(bundles_dir / f"{ECHO_BUNDLE_ID}.flow", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr(f"flows/{ECHO_FLOW_ID}.json", json.dumps(echo_flow(), indent=2))
    return f"{ECHO_BUNDLE_ID}@1.0.0"


def wait_until(predicate, *, timeout_s: float = 15.0, poll_s: float = 0.1):
    import time

    end = time.time() + timeout_s
    last = None
    while time.time() < end:
        last = predicate()
        if last:
            return last
        time.sleep(poll_s)
    raise AssertionError(f"timeout waiting for condition (last={last!r})")


# The built-in default agent bundle id for abstractcode.agent.v1: a stand-in
# whose "agent" deterministically calls `execute_command` (no provider).
SHELL_AGENT_BUNDLE_ID = "basic-agent"


def shell_agent_flow(command: str = "echo automation-shell-ok") -> dict:
    return {
        "id": "agent",
        "name": "Shell agent stand-in",
        "interfaces": ["abstractcode.agent.v1"],
        "nodes": [
            {"id": "start", "type": "on_flow_start", "data": {"nodeType": "on_flow_start", "outputs": [
                {"id": "exec-out", "label": "", "type": "execution"}, {"id": "prompt", "label": "prompt", "type": "string"}]}},
            {"id": "tools", "type": "tool_calls", "data": {"nodeType": "tool_calls", "inputs": [
                {"id": "exec-in", "label": "", "type": "execution"}, {"id": "tool_calls", "label": "tool_calls", "type": "array"}],
                "outputs": [{"id": "exec-out", "label": "", "type": "execution"}, {"id": "results", "label": "results", "type": "array"}],
                "pinDefaults": {"tool_calls": [{"name": "execute_command", "arguments": {"command": command}}]}}},
            {"id": "end", "type": "on_flow_end", "data": {"nodeType": "on_flow_end", "inputs": [
                {"id": "exec-in", "label": "", "type": "execution"}, {"id": "results", "label": "results", "type": "array"}]}},
        ],
        "edges": [
            {"source": "start", "sourceHandle": "exec-out", "target": "tools", "targetHandle": "exec-in"},
            {"source": "tools", "sourceHandle": "exec-out", "target": "end", "targetHandle": "exec-in"},
            {"source": "tools", "sourceHandle": "results", "target": "end", "targetHandle": "results"},
        ],
        "entryNode": "start",
    }


def write_shell_agent_bundle(bundles_dir: Path) -> None:
    import json
    import zipfile

    bundles_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "bundle_format_version": "1",
        "bundle_id": SHELL_AGENT_BUNDLE_ID,
        "bundle_version": "9.9.9",
        "created_at": "2026-09-27T00:00:00+00:00",
        "default_entrypoint": "agent",
        "entrypoints": [{"flow_id": "agent", "name": "agent", "description": "", "interfaces": ["abstractcode.agent.v1"]}],
        "flows": {"agent": "flows/agent.json"},
        "artifacts": {},
        "assets": {},
        "metadata": {},
    }
    with zipfile.ZipFile(bundles_dir / f"{SHELL_AGENT_BUNDLE_ID}.flow", "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr("flows/agent.json", json.dumps(shell_agent_flow(), indent=2))
