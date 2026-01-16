"""Run Gateway API (HTTP + SSE).

Backlog 307: Durable Run Gateway (Command Inbox + Ledger Stream)

This is intentionally replay-first:
- The durable ledger is the source of truth.
- SSE is an optimization; clients must be able to reconnect and replay by cursor.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
import mimetypes
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from abstractruntime.storage.commands import CommandRecord
from abstractruntime.storage.base import QueryableRunStore
from abstractruntime.core.models import Effect, EffectType, RunState, RunStatus, StepRecord

from .. import host_metrics
from ..service import get_gateway_service, run_summary


router = APIRouter(prefix="/gateway", tags=["gateway"])
logger = logging.getLogger(__name__)


class StartRunRequest(BaseModel):
    bundle_id: Optional[str] = Field(
        default=None,
        description="Bundle id (when workflow source is 'bundle'). Optional if flow_id is already namespaced as 'bundle:flow'.",
    )
    bundle_version: Optional[str] = Field(
        default=None,
        description="Optional bundle version to run. If omitted, defaults to the latest loaded version for the selected bundle_id.",
    )
    flow_id: Optional[str] = Field(
        default=None,
        description=(
            "Workflow id to start (flow id or 'bundle:flow'). Optional when bundle_id is provided and the bundle has a single entrypoint "
            "or declares manifest.default_entrypoint."
        ),
    )
    input_data: Dict[str, Any] = Field(default_factory=dict)
    session_id: Optional[str] = Field(
        default=None,
        description="Optional session id to group related runs (e.g. a chat session).",
    )


class StartRunResponse(BaseModel):
    run_id: str


class ScheduleRunRequest(BaseModel):
    bundle_id: str = Field(..., description="Target bundle id to execute.")
    bundle_version: Optional[str] = Field(
        default=None,
        description="Optional bundle version to run. If omitted, defaults to the latest loaded version for the selected bundle_id.",
    )
    flow_id: str = Field(..., description="Target entry flow id (or namespaced bundle:flow).")
    input_data: Dict[str, Any] = Field(default_factory=dict, description="Target flow input payload.")

    start_at: Optional[str] = Field(
        default=None,
        description="Start time: ISO 8601 timestamp (recommended) or 'now'/null for immediate start.",
    )
    interval: Optional[str] = Field(
        default=None,
        description="Optional recurrence interval (e.g. '1h', '1d', '7d', '30d'). If omitted, runs once.",
    )
    repeat_count: Optional[int] = Field(
        default=None,
        description="Optional number of executions. If omitted and interval is set, repeats forever.",
    )
    repeat_until: Optional[str] = Field(
        default=None,
        description=(
            "Optional termination time (ISO 8601). When provided with interval and repeat_count is omitted, "
            "the gateway derives a repeat_count so the schedule runs up to (and including) the last execution "
            "at or before this timestamp."
        ),
    )
    share_context: bool = Field(
        default=True,
        description="If true, scheduled executions share the parent run session context; if false, each execution is isolated.",
    )
    session_id: Optional[str] = Field(
        default=None,
        description="Optional session id to group related runs. If omitted, defaults to the scheduled parent run id.",
    )


class SubmitCommandRequest(BaseModel):
    command_id: str = Field(..., description="Client-supplied idempotency key (UUID recommended).")
    run_id: str = Field(..., description="Target run id (or session id for emit_event).")
    type: str = Field(..., description="pause|resume|cancel|emit_event|update_schedule|compact_memory")
    payload: Dict[str, Any] = Field(default_factory=dict)
    ts: Optional[str] = Field(default=None, description="ISO timestamp (optional).")
    client_id: Optional[str] = None


class SubmitCommandResponse(BaseModel):
    accepted: bool
    duplicate: bool
    seq: int


class GenerateRunSummaryRequest(BaseModel):
    provider: str = Field(default="lmstudio", description="AbstractCore provider name.")
    model: str = Field(default="qwen/qwen3-next-80b", description="Model id/name.")
    include_subruns: bool = Field(default=True, description="Include child/subworkflow runs in the summary context.")


class GenerateRunSummaryResponse(BaseModel):
    ok: bool
    run_id: str
    provider: str
    model: str
    generated_at: str
    summary: str


class EmbeddingsRequest(BaseModel):
    input: Any = Field(..., description="Text or list of texts to embed (OpenAI-compatible field name).")
    provider: Optional[str] = Field(default=None, description="Optional provider override (must match gateway embedding config).")
    model: Optional[str] = Field(default=None, description="Optional model override (must match gateway embedding config).")


class EmbeddingItem(BaseModel):
    object: str = Field(default="embedding")
    index: int
    embedding: list[float]


class EmbeddingsResponse(BaseModel):
    object: str = Field(default="list")
    provider: str
    model: str
    dimension: int
    data: list[EmbeddingItem]


class RunChatRequest(BaseModel):
    provider: str = Field(default="lmstudio", description="AbstractCore provider name.")
    model: str = Field(default="qwen/qwen3-next-80b", description="Model id/name.")
    include_subruns: bool = Field(default=True, description="Include child/subworkflow runs in the chat context.")
    messages: list[Dict[str, Any]] = Field(default_factory=list, description="Chat messages (role/content).")
    persist: bool = Field(default=False, description="If true, persist the Q/A to the parent run ledger as abstract.chat.")


class LedgerBatchRun(BaseModel):
    run_id: str
    after: int = Field(default=0, ge=0)


class LedgerBatchRequest(BaseModel):
    runs: list[LedgerBatchRun] = Field(default_factory=list)
    limit: int = Field(default=200, ge=1, le=2000)


class RunChatResponse(BaseModel):
    ok: bool
    run_id: str
    provider: str
    model: str
    generated_at: str
    answer: str


class ArtifactListItem(BaseModel):
    artifact_id: str
    content_type: Optional[str] = None
    size_bytes: Optional[int] = None
    created_at: Optional[str] = None
    tags: Dict[str, str] = Field(default_factory=dict)


class ArtifactListResponse(BaseModel):
    items: list[ArtifactListItem] = Field(default_factory=list)


class AttachmentIngestRequest(BaseModel):
    session_id: str = Field(..., description="Session id (attachments are stored under the session memory owner run).")
    path: str = Field(..., description="Workspace-relative path (preferred) or absolute path under workspace root.")
    filename: Optional[str] = Field(default=None, description="Optional filename override (defaults to basename of path).")
    content_type: Optional[str] = Field(
        default=None,
        description="Optional content type override (defaults to best-effort guess from filename).",
    )


def _require_bundle_host(svc: Any) -> Any:
    host = getattr(svc, "host", None)
    bundles = getattr(host, "bundles", None)
    if not isinstance(bundles, dict):
        raise HTTPException(status_code=400, detail="Bundle workflow source is not enabled on this gateway")
    return host


def _build_scheduled_wrapper_visualflow(
    *,
    workflow_id: str,
    target_workflow_id: str,
    start_schedule: str,
    interval: Optional[str],
    repeat_count: Optional[int],
    share_context: bool,
    session_prefix: Optional[str],
) -> Dict[str, Any]:
    """Build a minimal wrapper VisualFlow that schedules and runs a target workflow as subflows."""
    bid = str(target_workflow_id or "").strip()
    if not bid:
        raise ValueError("Missing target_workflow_id")

    start_schedule2 = str(start_schedule or "").strip()
    if not start_schedule2:
        start_schedule2 = "0s"

    interval2 = str(interval or "").strip() if isinstance(interval, str) and str(interval).strip() else None
    repeats = int(repeat_count) if isinstance(repeat_count, int) else None

    # Layout is intentionally simple: left-to-right.
    def _node(node_id: str, type_: str, x: float, y: float, data: Dict[str, Any]) -> Dict[str, Any]:
        return {"id": node_id, "type": type_, "position": {"x": float(x), "y": float(y)}, "data": data}

    def _exec_out() -> Dict[str, Any]:
        return {"id": "exec-out", "label": "", "type": "execution"}

    def _exec_in() -> Dict[str, Any]:
        return {"id": "exec-in", "label": "", "type": "execution"}

    nodes: list[Dict[str, Any]] = [
        _node(
            "start",
            "on_flow_start",
            0,
            0,
            {"nodeType": "on_flow_start", "label": "On Flow Start", "inputs": [], "outputs": [_exec_out()]},
        )
    ]
    edges: list[Dict[str, Any]] = []

    def _edge(edge_id: str, source: str, sh: str, target: str, th: str) -> Dict[str, Any]:
        return {"id": edge_id, "source": source, "sourceHandle": sh, "target": target, "targetHandle": th}

    share = bool(share_context)
    prefix = str(session_prefix or "").strip() if isinstance(session_prefix, str) else ""
    if not share and not prefix:
        raise ValueError("session_prefix is required when share_context is false")

    # Fixed-count schedule: use For + If to choose first wait vs interval wait.
    if repeats is not None and repeats > 0:
        if repeats > 10_000:
            raise ValueError("repeat_count is too large (max 10000)")
        if not interval2 and repeats > 1:
            raise ValueError("repeat_count > 1 requires interval")

        nodes.append(
            _node(
                "repeat",
                "for",
                220,
                0,
                {
                    "nodeType": "for",
                    "label": "Repeat",
                    "inputs": [
                        _exec_in(),
                        {"id": "start", "label": "start", "type": "number"},
                        {"id": "end", "label": "end", "type": "number"},
                        {"id": "step", "label": "step", "type": "number"},
                    ],
                    "outputs": [
                        {"id": "loop", "label": "loop", "type": "execution"},
                        {"id": "done", "label": "done", "type": "execution"},
                        {"id": "index", "label": "index", "type": "number"},
                    ],
                    "pinDefaults": {"start": 0, "end": int(repeats), "step": 1},
                },
            )
        )
        nodes.append(
            _node(
                "is_first",
                "compare",
                420,
                -70,
                {
                    "nodeType": "compare",
                    "label": "Compare",
                    "inputs": [{"id": "a", "label": "a", "type": "number"}, {"id": "b", "label": "b", "type": "number"}, {"id": "op", "label": "op", "type": "string"}],
                    "outputs": [{"id": "result", "label": "result", "type": "boolean"}],
                    "pinDefaults": {"b": 0, "op": "=="},
                },
            )
        )
        nodes.append(
            _node(
                "pick_wait",
                "if",
                420,
                0,
                {
                    "nodeType": "if",
                    "label": "If",
                    "inputs": [_exec_in(), {"id": "condition", "label": "condition", "type": "boolean"}],
                    "outputs": [{"id": "true", "label": "true", "type": "execution"}, {"id": "false", "label": "false", "type": "execution"}],
                },
            )
        )
        nodes.append(
            _node(
                "wait_start",
                "on_schedule",
                620,
                -40,
                {
                    "nodeType": "on_schedule",
                    "label": "Wait (start)",
                    "eventConfig": {"schedule": start_schedule2, "recurrent": False},
                    "inputs": [_exec_in()],
                    "outputs": [_exec_out()],
                },
            )
        )
        nodes.append(
            _node(
                "wait_interval",
                "on_schedule",
                620,
                40,
                {
                    "nodeType": "on_schedule",
                    "label": "Wait (interval)",
                    "eventConfig": {"schedule": str(interval2 or "1h"), "recurrent": False},
                    "inputs": [_exec_in()],
                    "outputs": [_exec_out()],
                },
            )
        )
        if not share:
            nodes.append(
                _node(
                    "idx_str",
                    "stringify_json",
                    420,
                    110,
                    {
                        "nodeType": "stringify_json",
                        "label": "Stringify",
                        "inputs": [{"id": "value", "label": "value", "type": "any"}],
                        "outputs": [{"id": "result", "label": "result", "type": "string"}],
                        "pinDefaults": {"mode": "minified"},
                    },
                )
            )
            nodes.append(
                _node(
                    "prefix_colon",
                    "concat",
                    620,
                    110,
                    {
                        "nodeType": "concat",
                        "label": "Concat",
                        "inputs": [{"id": "a", "label": "a", "type": "string"}, {"id": "b", "label": "b", "type": "string"}],
                        "outputs": [{"id": "result", "label": "result", "type": "string"}],
                        "pinDefaults": {"b": ":"},
                    },
                )
            )
            nodes.append(
                _node(
                    "child_sid",
                    "concat",
                    820,
                    110,
                    {
                        "nodeType": "concat",
                        "label": "Concat",
                        "inputs": [{"id": "a", "label": "a", "type": "string"}, {"id": "b", "label": "b", "type": "string"}],
                        "outputs": [{"id": "result", "label": "result", "type": "string"}],
                    },
                )
            )
            nodes.append(
                _node(
                    "set_child_sid",
                    "set_var",
                    820,
                    0,
                    {
                        "nodeType": "set_var",
                        "label": "Set Variable",
                        "inputs": [
                            _exec_in(),
                            {"id": "name", "label": "name", "type": "string"},
                            {"id": "value", "label": "value", "type": "string"},
                        ],
                        "outputs": [
                            {"id": "exec-out", "label": "", "type": "execution"},
                            {"id": "value", "label": "value", "type": "string"},
                        ],
                        "pinDefaults": {"name": "child_session_id"},
                    },
                )
            )
        nodes.append(
            _node(
                "run_target",
                "subflow",
                820,
                0,
                {
                    "nodeType": "subflow",
                    "label": "Run workflow",
                    "subflowId": target_workflow_id,
                    "inputs": [_exec_in()],
                    "outputs": [],
                },
            )
        )
        nodes.append(
            _node(
                "end",
                "on_flow_end",
                1040,
                0,
                {"nodeType": "on_flow_end", "label": "On Flow End", "inputs": [_exec_in()], "outputs": []},
            )
        )

        base_edges = [
            _edge("e1", "start", "exec-out", "repeat", "exec-in"),
            _edge("e2", "repeat", "loop", "pick_wait", "exec-in"),
            _edge("e3", "repeat", "index", "is_first", "a"),
            _edge("e4", "is_first", "result", "pick_wait", "condition"),
            _edge("e5", "pick_wait", "true", "wait_start", "exec-in"),
            _edge("e6", "pick_wait", "false", "wait_interval", "exec-in"),
            _edge("e9", "repeat", "done", "end", "exec-in"),
            # Always pass the target input payload (captured on start) into each execution.
            _edge("d_vars", "start", "vars", "run_target", "vars"),
        ]

        if share:
            base_edges.extend(
                [
                    _edge("e7", "wait_start", "exec-out", "run_target", "exec-in"),
                    _edge("e8", "wait_interval", "exec-out", "run_target", "exec-in"),
                ]
            )
        else:
            base_edges.extend(
                [
                    _edge("e7", "wait_start", "exec-out", "set_child_sid", "exec-in"),
                    _edge("e8", "wait_interval", "exec-out", "set_child_sid", "exec-in"),
                    _edge("e10", "set_child_sid", "exec-out", "run_target", "exec-in"),
                    _edge("d1", "repeat", "index", "idx_str", "value"),
                    _edge("d2", "start", "session_prefix", "prefix_colon", "a"),
                    _edge("d3", "prefix_colon", "result", "child_sid", "a"),
                    _edge("d4", "idx_str", "result", "child_sid", "b"),
                    _edge("d5", "child_sid", "result", "set_child_sid", "value"),
                    _edge("d6", "set_child_sid", "value", "run_target", "child_session_id"),
                ]
            )

        edges.extend(base_edges)

        return {"id": workflow_id, "name": "scheduled", "nodes": nodes, "edges": edges, "entryNode": "start"}

    # One-shot schedule.
    nodes.append(
        _node(
            "wait_start",
            "on_schedule",
            220,
            0,
            {
                "nodeType": "on_schedule",
                "label": "Wait",
                "eventConfig": {"schedule": start_schedule2, "recurrent": False},
                "inputs": [_exec_in()],
                "outputs": [_exec_out()],
            },
        )
    )
    nodes.append(
        _node(
            "run_target",
            "subflow",
            620 if not share else 420,
            0,
            {
                "nodeType": "subflow",
                "label": "Run workflow",
                "subflowId": target_workflow_id,
                "inputs": [_exec_in()],
                "outputs": [_exec_out()] if interval2 else [_exec_out()],
            },
        )
    )

    if interval2:
        # Repeat forever: explicit cycle (subflow -> interval wait -> subflow).
        if not share:
            nodes.append(
                _node(
                    "dt",
                    "system_datetime",
                    420,
                    110,
                    {"nodeType": "system_datetime", "label": "Now", "inputs": [], "outputs": [{"id": "iso", "label": "iso", "type": "string"}]},
                )
            )
            nodes.append(
                _node(
                    "prefix_colon",
                    "concat",
                    620,
                    110,
                    {
                        "nodeType": "concat",
                        "label": "Concat",
                        "inputs": [{"id": "a", "label": "a", "type": "string"}, {"id": "b", "label": "b", "type": "string"}],
                        "outputs": [{"id": "result", "label": "result", "type": "string"}],
                        "pinDefaults": {"b": ":"},
                    },
                )
            )
            nodes.append(
                _node(
                    "child_sid",
                    "concat",
                    820,
                    110,
                    {
                        "nodeType": "concat",
                        "label": "Concat",
                        "inputs": [{"id": "a", "label": "a", "type": "string"}, {"id": "b", "label": "b", "type": "string"}],
                        "outputs": [{"id": "result", "label": "result", "type": "string"}],
                    },
                )
            )
            nodes.append(
                _node(
                    "set_child_sid",
                    "set_var",
                    420,
                    0,
                    {
                        "nodeType": "set_var",
                        "label": "Set Variable",
                        "inputs": [_exec_in(), {"id": "name", "label": "name", "type": "string"}, {"id": "value", "label": "value", "type": "string"}],
                        "outputs": [
                            {"id": "exec-out", "label": "", "type": "execution"},
                            {"id": "value", "label": "value", "type": "string"},
                        ],
                        "pinDefaults": {"name": "child_session_id"},
                    },
                )
            )
        nodes.append(
            _node(
                "wait_interval",
                "on_schedule",
                620,
                0,
                {
                    "nodeType": "on_schedule",
                    "label": "Wait (interval)",
                    "eventConfig": {"schedule": interval2, "recurrent": False},
                    "inputs": [_exec_in()],
                    "outputs": [_exec_out()],
                },
            )
        )
        if share:
            edges.extend(
                [
                    _edge("e1", "start", "exec-out", "wait_start", "exec-in"),
                    _edge("e2", "wait_start", "exec-out", "run_target", "exec-in"),
                    _edge("e3", "run_target", "exec-out", "wait_interval", "exec-in"),
                    _edge("e4", "wait_interval", "exec-out", "run_target", "exec-in"),
                    _edge("d_vars", "start", "vars", "run_target", "vars"),
                ]
            )
        else:
            edges.extend(
                [
                    _edge("e1", "start", "exec-out", "wait_start", "exec-in"),
                    _edge("e2", "wait_start", "exec-out", "set_child_sid", "exec-in"),
                    _edge("e3", "set_child_sid", "exec-out", "run_target", "exec-in"),
                    _edge("e4", "run_target", "exec-out", "wait_interval", "exec-in"),
                    _edge("e5", "wait_interval", "exec-out", "set_child_sid", "exec-in"),
                    _edge("d_vars", "start", "vars", "run_target", "vars"),
                    _edge("d1", "start", "session_prefix", "prefix_colon", "a"),
                    _edge("d2", "prefix_colon", "result", "child_sid", "a"),
                    _edge("d3", "dt", "iso", "child_sid", "b"),
                    _edge("d4", "child_sid", "result", "set_child_sid", "value"),
                    _edge("d5", "set_child_sid", "value", "run_target", "child_session_id"),
                ]
            )
        return {"id": workflow_id, "name": "scheduled", "nodes": nodes, "edges": edges, "entryNode": "start"}

    nodes.append(
        _node(
            "end",
            "on_flow_end",
            820 if not share else 620,
            0,
            {"nodeType": "on_flow_end", "label": "On Flow End", "inputs": [_exec_in()], "outputs": []},
        )
    )
    if not share:
        nodes.append(
            _node(
                "prefix_colon",
                "concat",
                420,
                110,
                {
                    "nodeType": "concat",
                    "label": "Concat",
                    "inputs": [{"id": "a", "label": "a", "type": "string"}, {"id": "b", "label": "b", "type": "string"}],
                    "outputs": [{"id": "result", "label": "result", "type": "string"}],
                    "pinDefaults": {"b": ":"},
                },
            )
        )
        nodes.append(
            _node(
                "child_sid",
                "concat",
                620,
                110,
                {
                    "nodeType": "concat",
                    "label": "Concat",
                    "inputs": [{"id": "a", "label": "a", "type": "string"}, {"id": "b", "label": "b", "type": "string"}],
                    "outputs": [{"id": "result", "label": "result", "type": "string"}],
                    "pinDefaults": {"b": "0"},
                },
            )
        )
        nodes.append(
            _node(
                "set_child_sid",
                "set_var",
                420,
                0,
                {
                    "nodeType": "set_var",
                    "label": "Set Variable",
                    "inputs": [_exec_in(), {"id": "name", "label": "name", "type": "string"}, {"id": "value", "label": "value", "type": "string"}],
                    "outputs": [
                        {"id": "exec-out", "label": "", "type": "execution"},
                        {"id": "value", "label": "value", "type": "string"},
                    ],
                    "pinDefaults": {"name": "child_session_id"},
                },
            )
        )

        edges.extend(
            [
                _edge("e1", "start", "exec-out", "wait_start", "exec-in"),
                _edge("e2", "wait_start", "exec-out", "set_child_sid", "exec-in"),
                _edge("e3", "set_child_sid", "exec-out", "run_target", "exec-in"),
                _edge("e4", "run_target", "exec-out", "end", "exec-in"),
                _edge("d_vars", "start", "vars", "run_target", "vars"),
                _edge("d1", "start", "session_prefix", "prefix_colon", "a"),
                _edge("d2", "prefix_colon", "result", "child_sid", "a"),
                _edge("d3", "child_sid", "result", "set_child_sid", "value"),
                _edge("d4", "set_child_sid", "value", "run_target", "child_session_id"),
            ]
        )
        return {"id": workflow_id, "name": "scheduled", "nodes": nodes, "edges": edges, "entryNode": "start"}

    edges.extend(
        [
            _edge("e1", "start", "exec-out", "wait_start", "exec-in"),
            _edge("e2", "wait_start", "exec-out", "run_target", "exec-in"),
            _edge("e3", "run_target", "exec-out", "end", "exec-in"),
            _edge("d_vars", "start", "vars", "run_target", "vars"),
        ]
    )
    return {"id": workflow_id, "name": "scheduled", "nodes": nodes, "edges": edges, "entryNode": "start"}


def _extract_entrypoint_inputs_from_visualflow(raw: Any) -> list[Dict[str, Any]]:
    """Best-effort derive start input definitions from a VisualFlow JSON object."""
    if not isinstance(raw, dict):
        return []
    nodes = raw.get("nodes")
    if not isinstance(nodes, list):
        return []

    start_node: Optional[Dict[str, Any]] = None
    for n in nodes:
        if not isinstance(n, dict):
            continue
        t = n.get("type")
        t_str = t.value if hasattr(t, "value") else str(t or "")
        data = n.get("data") if isinstance(n.get("data"), dict) else {}
        node_type = str(data.get("nodeType") or t_str or "").strip()
        if node_type == "on_flow_start" or t_str == "on_flow_start":
            start_node = n
            break

    if start_node is None:
        return []

    data = start_node.get("data") if isinstance(start_node.get("data"), dict) else {}
    outputs = data.get("outputs")
    pin_defaults = data.get("pinDefaults") if isinstance(data.get("pinDefaults"), dict) else {}
    if not isinstance(outputs, list):
        return []

    out: list[Dict[str, Any]] = []
    for p in outputs:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or "").strip()
        if not pid:
            continue
        ptype = str(p.get("type") or "").strip()
        if ptype == "execution" or pid in {"exec-out", "exec"}:
            continue
        label = str(p.get("label") or pid).strip() or pid
        item: Dict[str, Any] = {"id": pid, "label": label, "type": ptype or "unknown"}
        if pid in pin_defaults:
            item["default"] = pin_defaults.get(pid)
        out.append(item)
    return out


def _extract_node_index_from_visualflow(raw: Any) -> Dict[str, Dict[str, Any]]:
    """Best-effort node metadata index {node_id -> {type,label,headerColor}}."""
    if not isinstance(raw, dict):
        return {}
    nodes = raw.get("nodes")
    if not isinstance(nodes, list):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for n in nodes:
        if not isinstance(n, dict):
            continue
        node_id = str(n.get("id") or "").strip()
        if not node_id:
            continue
        t = n.get("type")
        t_str = t.value if hasattr(t, "value") else str(t or "")
        data = n.get("data") if isinstance(n.get("data"), dict) else {}
        node_type = str(data.get("nodeType") or t_str or "").strip() or "unknown"
        label = str(data.get("label") or n.get("label") or node_id).strip() or node_id
        header_color = data.get("headerColor") or n.get("headerColor")
        item: Dict[str, Any] = {"type": node_type, "label": label}
        if isinstance(header_color, str) and header_color.strip():
            item["headerColor"] = header_color.strip()
        out[node_id] = item
    return out


def _parse_namespaced_workflow_id(workflow_id: str) -> Optional[tuple[str, str]]:
    s = str(workflow_id or "").strip()
    if not s:
        return None
    if ":" not in s:
        return None
    a, b = s.split(":", 1)
    a = a.strip()
    b = b.strip()
    if not a or not b:
        return None
    return (a, b)


def _split_bundle_ref(raw: str) -> tuple[str, Optional[str]]:
    s = str(raw or "").strip()
    if not s:
        return ("", None)
    if "@" not in s:
        return (s, None)
    a, b = s.split("@", 1)
    a = a.strip()
    b = b.strip()
    if not a:
        return ("", None)
    if not b:
        return (a, None)
    return (a, b)


def _resolve_bundle_from_host(*, host: Any, bundle_id: str, bundle_version: Optional[str]) -> tuple[str, Any]:
    """Return (selected_bundle_version, bundle) for a bundle_id (+ optional bundle_version)."""
    bundles0 = getattr(host, "bundles", None)
    if not isinstance(bundles0, dict):
        raise HTTPException(status_code=400, detail="Bundle workflow source is not enabled on this gateway")
    latest0 = getattr(host, "latest_bundle_versions", None)
    latest = latest0 if isinstance(latest0, dict) else {}

    bid_base, bid_ver = _split_bundle_ref(str(bundle_id or ""))
    if not bid_base:
        raise HTTPException(status_code=400, detail="bundle_id is required")

    req_ver = str(bundle_version or "").strip() if isinstance(bundle_version, str) and str(bundle_version).strip() else ""
    if bid_ver and req_ver and bid_ver != req_ver:
        raise HTTPException(status_code=400, detail="bundle_version conflicts with bundle_id (bundle_id already includes '@version')")

    versions = bundles0.get(bid_base)
    if not isinstance(versions, dict) or not versions:
        raise HTTPException(status_code=404, detail=f"Bundle '{bid_base}' not found")

    selected_ver = req_ver or bid_ver or str(latest.get(bid_base) or "").strip()
    if not selected_ver:
        raise HTTPException(status_code=404, detail=f"Bundle '{bid_base}' has no versions loaded")

    bundle = versions.get(selected_ver)
    if bundle is None:
        raise HTTPException(status_code=404, detail=f"Bundle '{bid_base}@{selected_ver}' not found")

    return (selected_ver, bundle)


def _workspace_root() -> Path:
    raw = str(os.getenv("ABSTRACTGATEWAY_WORKSPACE_DIR", "") or "").strip()
    base = Path(raw).expanduser() if raw else Path.cwd()
    try:
        return base.resolve()
    except Exception:
        return base


_DEFAULT_SESSION_MEMORY_RUN_PREFIX = "session_memory_"
_SAFE_RUN_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")


def _session_memory_run_id(session_id: str) -> str:
    sid = str(session_id or "").strip()
    if not sid:
        raise ValueError("session_id is required")
    if _SAFE_RUN_ID_PATTERN.match(sid):
        rid = f"{_DEFAULT_SESSION_MEMORY_RUN_PREFIX}{sid}"
        if _SAFE_RUN_ID_PATTERN.match(rid):
            return rid
    digest = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:32]
    return f"{_DEFAULT_SESSION_MEMORY_RUN_PREFIX}sha_{digest}"


_FILE_INDEX_CACHE: dict[str, Any] = {"base": "", "built_at": 0.0, "paths": []}
_TOOL_SPECS_CACHE: dict[str, Any] = {"built_at": 0.0, "specs": {}}


def _get_tool_specs_by_name(*, ttl_s: float = 30.0) -> Dict[str, Dict[str, Any]]:
    """Return ToolSpecs indexed by name (best-effort, cached).

    Source of truth is AbstractCore ToolDefinitions (via AbstractRuntime integration).
    """
    global _TOOL_SPECS_CACHE
    now = time.time()
    built_at = float(_TOOL_SPECS_CACHE.get("built_at") or 0.0)
    cached = _TOOL_SPECS_CACHE.get("specs") if isinstance(_TOOL_SPECS_CACHE.get("specs"), dict) else {}
    if cached and (now - built_at) < float(ttl_s):
        return dict(cached)

    items: list[Any] = []
    try:
        from abstractruntime.integrations.abstractcore.default_tools import list_default_tool_specs

        items = list_default_tool_specs()
    except Exception:
        items = []

    out: Dict[str, Dict[str, Any]] = {}
    for s in items:
        if not isinstance(s, dict):
            continue
        name = s.get("name")
        if isinstance(name, str) and name.strip():
            out[name.strip()] = dict(s)

    _TOOL_SPECS_CACHE = {"built_at": now, "specs": out}
    return out


def _build_file_index(*, base: Path, max_files: int) -> list[str]:
    from abstractcore.tools.abstractignore import AbstractIgnore

    ignore = AbstractIgnore.for_path(base)
    out: list[str] = []

    for root, dirs, files in os.walk(base):
        root_path = Path(root)
        keep_dirs: list[str] = []
        for d in list(dirs):
            p = root_path / d
            if ignore.is_ignored(p, is_dir=True):
                continue
            keep_dirs.append(d)
        dirs[:] = keep_dirs

        for fn in files:
            p = root_path / fn
            if ignore.is_ignored(p, is_dir=False):
                continue
            try:
                rel = p.resolve().relative_to(base).as_posix()
            except Exception:
                continue
            out.append(rel)
            if len(out) >= max_files:
                return out
    return out


def _get_file_index(*, base: Path, ttl_s: float = 30.0, max_files: int = 50000) -> list[str]:
    global _FILE_INDEX_CACHE
    now = time.time()
    cached_base = str(_FILE_INDEX_CACHE.get("base") or "")
    built_at = float(_FILE_INDEX_CACHE.get("built_at") or 0.0)
    cached_paths = _FILE_INDEX_CACHE.get("paths") if isinstance(_FILE_INDEX_CACHE.get("paths"), list) else []
    if cached_base == str(base) and cached_paths and (now - built_at) < ttl_s:
        return list(cached_paths)
    paths = _build_file_index(base=base, max_files=max_files)
    _FILE_INDEX_CACHE = {"base": str(base), "built_at": now, "paths": paths}
    return paths


def _resolve_workspace_path(*, base: Path, raw_path: str) -> Path:
    p_raw = str(raw_path or "").strip()
    if not p_raw:
        raise HTTPException(status_code=400, detail="path is required")
    p = Path(p_raw).expanduser()
    resolved = (base / p).resolve() if not p.is_absolute() else p.resolve()
    try:
        resolved.relative_to(base)
    except Exception:
        raise HTTPException(status_code=403, detail="path is outside workspace root")
    return resolved


def _clamp_text(text: str, *, max_len: int) -> str:
    s = str(text or "")
    if max_len <= 0:
        return ""
    if len(s) <= max_len:
        return s
    return f"{s[: max(0, max_len - 1)]}…"


def _extract_digest_from_ledger(ledger: list[Dict[str, Any]]) -> Dict[str, Any]:
    stats: Dict[str, Any] = {
        "steps": 0,
        "llm_calls": 0,
        "tool_calls_effects": 0,
        "tool_calls": 0,
        "unique_tools": 0,
        "tokens": {"prompt": 0, "completion": 0, "total": 0},
        "started_at": None,
        "ended_at": None,
    }
    tools_used: set[str] = set()
    files: list[Dict[str, Any]] = []
    commands: list[Dict[str, Any]] = []
    web: list[Dict[str, Any]] = []
    tool_calls_detail: list[Dict[str, Any]] = []
    llm_calls_detail: list[Dict[str, Any]] = []
    errors: list[str] = []

    started_at: Optional[str] = None
    ended_at: Optional[str] = None

    tool_specs_by_name = _get_tool_specs_by_name(ttl_s=30.0)

    def _format_arg_value(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str):
            return _clamp_text(value.strip(), max_len=160)
        try:
            txt = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        except Exception:
            txt = str(value)
        return _clamp_text(txt, max_len=160)

    def _ordered_args(name: str, args: Dict[str, Any]) -> list[tuple[str, Any]]:
        spec = tool_specs_by_name.get(str(name or "").strip())
        params = spec.get("parameters") if isinstance(spec, dict) else None
        order = list(params.keys()) if isinstance(params, dict) else []
        if not order:
            order = [k for k in args.keys() if isinstance(k, str) and k]
        out: list[tuple[str, Any]] = []
        seen: set[str] = set()
        for k in order:
            if k in seen:
                continue
            seen.add(k)
            if k in args:
                out.append((k, args.get(k)))
        # Include any extra args not present in the schema (dynamic tools / older runs).
        for k, v in (args or {}).items():
            if not isinstance(k, str) or not k or k in seen:
                continue
            out.append((k, v))
        return out

    def _tool_signature(name: str, args: Dict[str, Any]) -> str:
        n = str(name or "").strip()
        if not n:
            n = "tool"
        a = args if isinstance(args, dict) else {}
        pairs = _ordered_args(n, a)
        shown = pairs[:2]
        if not shown:
            return f"{n}()"
        if len(shown) == 1:
            return f"{n}({_format_arg_value(shown[0][1])})"
        inner = ", ".join([f"{k}={_format_arg_value(v)}" for k, v in shown])
        return f"{n}({inner})"

    def _toolset(name: str) -> str:
        spec = tool_specs_by_name.get(str(name or "").strip())
        if isinstance(spec, dict):
            v = spec.get("toolset")
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    def _primary_arg_value(name: str, args: Dict[str, Any]) -> str:
        a = args if isinstance(args, dict) else {}
        pairs = _ordered_args(str(name or "").strip(), a)
        if not pairs:
            return ""
        return _format_arg_value(pairs[0][1])

    def _extract_llm_prompt(payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        p = payload.get("prompt")
        if isinstance(p, str) and p.strip():
            return p.strip()
        msgs = payload.get("messages")
        if isinstance(msgs, list):
            # Prefer the last user message.
            for m in reversed(msgs):
                if not isinstance(m, dict):
                    continue
                if str(m.get("role") or "") != "user":
                    continue
                c = m.get("content")
                if isinstance(c, str) and c.strip():
                    return c.strip()
        return ""

    for rec in ledger or []:
        if not isinstance(rec, dict):
            continue
        stats["steps"] += 1
        ts = str(rec.get("ended_at") or rec.get("started_at") or "").strip()
        if ts:
            if started_at is None or ts < started_at:
                started_at = ts
            if ended_at is None or ts > ended_at:
                ended_at = ts

        if rec.get("error"):
            errors.append(_clamp_text(str(rec.get("error")), max_len=400))

        eff = rec.get("effect")
        eff_type = str(eff.get("type") or "").strip() if isinstance(eff, dict) else ""
        node_id = str(rec.get("node_id") or "").strip()

        if eff_type == "llm_call":
            stats["llm_calls"] += 1
            payload = eff.get("payload") if isinstance(eff, dict) else None
            res = rec.get("result")
            usage = None
            pt_i = 0
            ct_i = 0
            total_i = 0
            if isinstance(res, dict):
                usage = res.get("usage") or res.get("token_usage")
            if isinstance(usage, dict):
                pt = usage.get("prompt_tokens", usage.get("input_tokens", 0))
                ct = usage.get("completion_tokens", usage.get("output_tokens", 0))
                try:
                    pt_i = int(pt or 0)
                except Exception:
                    pt_i = 0
                try:
                    ct_i = int(ct or 0)
                except Exception:
                    ct_i = 0
                stats["tokens"]["prompt"] += pt_i
                stats["tokens"]["completion"] += ct_i
                try:
                    total_i = int(usage.get("total_tokens", pt_i + ct_i) or (pt_i + ct_i))
                except Exception:
                    total_i = pt_i + ct_i
                stats["tokens"]["total"] += total_i
            if len(llm_calls_detail) < 60:
                prompt = _extract_llm_prompt(payload)
                content = ""
                if isinstance(res, dict):
                    content = str(res.get("content") or res.get("response") or "").strip()
                elif isinstance(res, str):
                    content = res.strip()
                llm_calls_detail.append(
                    {
                        "ts": ts,
                        "node_id": node_id or None,
                        "provider": str(payload.get("provider") or "").strip() if isinstance(payload, dict) else None,
                        "model": str(payload.get("model") or "").strip() if isinstance(payload, dict) else None,
                        "prompt": _clamp_text(prompt, max_len=800),
                        "response": _clamp_text(content, max_len=800),
                        "missing_response": not bool(content),
                        "tokens": {"prompt": pt_i, "completion": ct_i, "total": total_i},
                    }
                )

        if eff_type != "tool_calls":
            continue
        stats["tool_calls_effects"] += 1
        payload = eff.get("payload") if isinstance(eff, dict) else None
        calls = payload.get("tool_calls") if isinstance(payload, dict) else None
        if not isinstance(calls, list):
            continue
        stats["tool_calls"] += len(calls)

        results_by_id: Dict[str, Dict[str, Any]] = {}
        results_by_index: list[Dict[str, Any]] = []
        res_obj = rec.get("result")
        if isinstance(res_obj, dict):
            raw_results = res_obj.get("results")
            if isinstance(raw_results, list):
                for r in raw_results:
                    if not isinstance(r, dict):
                        continue
                    results_by_index.append(r)
                    cid = str(r.get("call_id") or r.get("id") or "").strip()
                    if cid and cid not in results_by_id:
                        results_by_id[cid] = r

        def _format_output_preview(value: Any) -> str:
            if value is None:
                return ""
            if isinstance(value, str):
                return value
            try:
                return json.dumps(value, ensure_ascii=False, indent=2)
            except Exception:
                return str(value)

        for idx, c in enumerate(calls):
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or "").strip()
            if not name:
                continue
            tools_used.add(name)
            args = c.get("arguments") if isinstance(c.get("arguments"), dict) else {}
            call_id = str(c.get("call_id") or c.get("id") or "").strip()
            toolset = _toolset(name)
            primary = _primary_arg_value(name, args)

            if len(tool_calls_detail) < 200:
                result = results_by_id.get(call_id) if call_id else None
                if result is None:
                    # Fallback: align by index if call_id isn't present.
                    if 0 <= idx < len(results_by_index):
                        result = results_by_index[idx]
                ok = result.get("success") if isinstance(result, dict) else None
                out = result.get("output") if isinstance(result, dict) else None
                err = result.get("error") if isinstance(result, dict) else None
                tool_calls_detail.append(
                    {
                        "ts": ts,
                        "node_id": node_id or None,
                        "name": name,
                        "signature": _tool_signature(name, args),
                        "success": bool(ok) if isinstance(ok, bool) else None,
                        "error": _clamp_text(str(err), max_len=500) if err else None,
                        "output": _clamp_text(_format_output_preview(out), max_len=50000) if out is not None else None,
                        "toolset": toolset or None,
                    }
                )

            # Categorize best-effort by toolset (derived from ToolDefinitions via default toolsets).
            if toolset == "files" and primary:
                files.append({"tool": name, "file_path": primary, "ts": ts})
            elif toolset == "system" and primary:
                commands.append({"command": primary, "ts": ts})
            elif toolset == "web" and primary:
                web.append({"tool": name, "value": primary, "ts": ts})

    stats["unique_tools"] = len(tools_used)
    stats["started_at"] = started_at
    stats["ended_at"] = ended_at

    return {
        "stats": stats,
        "tools_used": sorted(list(tools_used)),
        "files": files[:200],
        "commands": commands[:200],
        "web": web[:200],
        "tool_calls_detail": tool_calls_detail,
        "llm_calls_detail": llm_calls_detail,
        "errors": errors[:50],
    }


def _list_descendant_run_ids(run_store: Any, root_run_id: str, *, limit: int = 2000) -> list[str]:
    out: list[str] = []
    queue: list[str] = [str(root_run_id)]
    seen: set[str] = set()
    list_children = getattr(run_store, "list_children", None)
    while queue and len(out) < int(limit):
        rid = str(queue.pop(0) or "").strip()
        if not rid or rid in seen:
            continue
        seen.add(rid)
        out.append(rid)
        if not callable(list_children):
            continue
        try:
            children = list_children(parent_run_id=rid) or []
        except Exception:
            children = []
        for c in children:
            cid = getattr(c, "run_id", None)
            if isinstance(cid, str) and cid and cid not in seen:
                queue.append(cid)
    return out


def _generate_summary_text(*, provider: str, model: str, context: Dict[str, Any]) -> str:
    """Generate a human-readable summary for a run (patchable in tests)."""
    from abstractruntime.integrations.abstractcore.llm_client import LocalAbstractCoreLLMClient

    system = (
        "You are AbstractObserver. You are given an execution digest derived from an append-only workflow ledger.\n"
        "Write a concise human-readable SUMMARY of what the workflow did.\n\n"
        "Requirements:\n"
        "- Start with 1 line: outcome (success/failure/cancelled/unknown)\n"
        "- Then 3-8 short bullets: key actions, important files/commands/URLs, and any issues.\n"
        "- Mention relevant tool/LLM activity (counts + notable calls); do not paste huge payloads.\n"
        "- If any LLM call has `missing_response=true`, flag it as a likely runtime/model issue.\n"
        "- Mention the original prompt.\n"
        "- If the run failed, include the likely reason.\n"
        "- Keep it compact and factual; do not invent actions.\n"
    )

    user = json.dumps(context, ensure_ascii=False, indent=2)
    llm = LocalAbstractCoreLLMClient(provider=str(provider), model=str(model))
    res = llm.generate(
        prompt="",
        messages=[{"role": "user", "content": _clamp_text(user, max_len=180_000)}],
        system_prompt=system,
        params={"temperature": 0.2, "max_output_tokens": 700},
    )
    text = str(res.get("content") or "").strip()
    return text


def _generate_chat_text(*, provider: str, model: str, context: Dict[str, Any], messages: list[Dict[str, Any]]) -> str:
    """Generate a read-only chat response grounded in a run ledger (patchable in tests)."""
    from abstractruntime.integrations.abstractcore.llm_client import LocalAbstractCoreLLMClient

    system = (
        "You are AbstractObserver Chat.\n"
        "You are given:\n"
        "- RUN_CONTEXT: a JSON object derived from an append-only workflow ledger (parent + subruns).\n"
        "- CHAT_MESSAGES: a short chat history.\n\n"
        "Your job:\n"
        "- Answer the user's latest question using ONLY facts from RUN_CONTEXT.\n"
        "- If RUN_CONTEXT does not contain enough information, say you don't know.\n"
        "- You may provide a best-effort guess, but label it explicitly as a guess.\n"
        "- Be concise, structured, and do not paste large raw payloads.\n"
        "- If the run appears failed, explain the likely reason.\n"
        "- Do not suggest running tools (this chat is read-only).\n"
    )

    ctx = json.dumps(context, ensure_ascii=False, indent=2)
    prompt_msgs: list[Dict[str, str]] = [{"role": "user", "content": "RUN_CONTEXT:\n" + _clamp_text(ctx, max_len=180_000)}]

    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = m.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        prompt_msgs.append({"role": role, "content": _clamp_text(content.strip(), max_len=12_000)})

    llm = LocalAbstractCoreLLMClient(provider=str(provider), model=str(model))
    res = llm.generate(
        prompt="",
        messages=prompt_msgs,
        system_prompt=system,
        params={"temperature": 0.2, "max_output_tokens": 900},
    )
    text = str(res.get("content") or "").strip()
    return text


@router.get("/bundles")
async def list_bundles(all_versions: bool = Query(default=False, description="If true, return one item per bundle version.")) -> Dict[str, Any]:
    svc = get_gateway_service()
    host = _require_bundle_host(svc)

    items: list[Dict[str, Any]] = []
    bundles_by_id = getattr(host, "bundles", {}) or {}
    latest0 = getattr(host, "latest_bundle_versions", None)
    latest = latest0 if isinstance(latest0, dict) else {}

    for bid, versions in (bundles_by_id or {}).items():
        if not isinstance(versions, dict) or not versions:
            continue

        selected_versions: list[str]
        if all_versions:
            selected_versions = [str(v) for v in versions.keys() if isinstance(v, str)]
        else:
            v0 = latest.get(str(bid))
            v = str(v0).strip() if isinstance(v0, str) and str(v0).strip() else ""
            if not v:
                # Best-effort fallback.
                v = sorted([str(x) for x in versions.keys() if isinstance(x, str)])[-1]
            selected_versions = [v]

        for ver in selected_versions:
            b = versions.get(ver)
            man = getattr(b, "manifest", None) if b is not None else None
            if man is None:
                continue
            entrypoints = getattr(man, "entrypoints", None) or []
            eps: list[Dict[str, Any]] = []
            for ep in entrypoints:
                eps.append(
                    {
                        "flow_id": getattr(ep, "flow_id", None),
                        "name": getattr(ep, "name", None),
                        "description": getattr(ep, "description", "") or "",
                        "interfaces": list(getattr(ep, "interfaces", None) or []),
                    }
                )
            items.append(
                {
                    "bundle_id": str(bid),
                    "bundle_version": getattr(man, "bundle_version", ver),
                    "bundle_ref": f"{bid}@{getattr(man, 'bundle_version', ver)}",
                    "created_at": getattr(man, "created_at", ""),
                    "default_entrypoint": str(getattr(man, "default_entrypoint", "") or "") or None,
                    "entrypoints": eps,
                }
            )

    items.sort(key=lambda x: str(x.get("bundle_id") or ""))
    return {"items": items, "default_bundle_id": getattr(host, "_default_bundle_id", None)}


@router.post("/bundles/reload")
async def reload_bundles() -> Dict[str, Any]:
    """Reload bundle directory (best-effort; intended for local dev)."""
    svc = get_gateway_service()
    host = _require_bundle_host(svc)

    reload_fn = getattr(host, "reload_bundles_from_disk", None)
    if not callable(reload_fn):
        raise HTTPException(status_code=400, detail="Bundle reload is not supported on this gateway host")
    try:
        return dict(reload_fn() or {})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reload bundles: {e}")


@router.get("/bundles/{bundle_id}")
async def get_bundle(bundle_id: str, bundle_version: Optional[str] = Query(default=None, description="Optional bundle version (defaults to latest).")) -> Dict[str, Any]:
    svc = get_gateway_service()
    host = _require_bundle_host(svc)

    bid = str(bundle_id or "").strip()
    if not bid:
        raise HTTPException(status_code=400, detail="bundle_id is required")

    selected_ver, bundle = _resolve_bundle_from_host(host=host, bundle_id=bid, bundle_version=bundle_version)
    bid_base, _bid_ver = _split_bundle_ref(bid)

    man = bundle.manifest
    entrypoints_out: list[Dict[str, Any]] = []
    for ep in list(man.entrypoints or []):
        fid = str(getattr(ep, "flow_id", "") or "").strip()
        rel = man.flow_path_for(fid) if fid else None
        raw = bundle.read_json(rel) if isinstance(rel, str) and rel.strip() else None
        entrypoints_out.append(
            {
                "flow_id": fid,
                "workflow_id": f"{bid_base}@{selected_ver}:{fid}" if fid else None,
                "name": getattr(ep, "name", None),
                "description": str(getattr(ep, "description", "") or ""),
                "interfaces": list(getattr(ep, "interfaces", None) or []),
                "inputs": _extract_entrypoint_inputs_from_visualflow(raw),
                "node_index": _extract_node_index_from_visualflow(raw),
            }
        )

    flow_ids = sorted([str(k) for k in (man.flows or {}).keys() if isinstance(k, str) and k.strip()])
    return {
        "bundle_id": str(bid_base),
        "bundle_version": str(selected_ver),
        "bundle_ref": f"{bid_base}@{selected_ver}",
        "created_at": str(man.created_at or ""),
        "default_entrypoint": str(getattr(man, "default_entrypoint", "") or "") or None,
        "entrypoints": entrypoints_out,
        "flows": flow_ids,
        "metadata": dict(getattr(man, "metadata", None) or {}),
    }


@router.get("/bundles/{bundle_id}/flows/{flow_id}")
async def get_bundle_flow(
    bundle_id: str,
    flow_id: str,
    bundle_version: Optional[str] = Query(default=None, description="Optional bundle version (defaults to latest)."),
) -> Dict[str, Any]:
    """Return a VisualFlow JSON from a bundle (best-effort; intended for thin clients)."""
    svc = get_gateway_service()
    host = _require_bundle_host(svc)

    bid = str(bundle_id or "").strip()
    if not bid:
        raise HTTPException(status_code=400, detail="bundle_id is required")

    bid_base, bid_ver = _split_bundle_ref(bid)

    fid = str(flow_id or "").strip()
    if not fid:
        raise HTTPException(status_code=400, detail="flow_id is required")

    # Accept "bundle:flow" for convenience but enforce matching bundle_id.
    if ":" in fid:
        parsed = fid.split(":", 1)
        if len(parsed) == 2 and parsed[0] and parsed[1]:
            prefix_base, prefix_ver = _split_bundle_ref(parsed[0])
            if prefix_base != bid_base:
                raise HTTPException(status_code=400, detail="flow_id bundle prefix does not match bundle_id")
            if prefix_ver and bundle_version and prefix_ver != bundle_version:
                raise HTTPException(status_code=400, detail="flow_id version does not match bundle_version")
            if prefix_ver and bid_ver and prefix_ver != bid_ver:
                raise HTTPException(status_code=400, detail="flow_id version does not match bundle_id")
            if bid_ver and bundle_version and bid_ver != bundle_version:
                raise HTTPException(status_code=400, detail="bundle_id version does not match bundle_version")
            bundle_version = prefix_ver or bundle_version or bid_ver
            fid = parsed[1].strip()
        else:
            raise HTTPException(status_code=400, detail="Invalid flow_id")
    elif bid_ver and bundle_version and bid_ver != bundle_version:
        raise HTTPException(status_code=400, detail="bundle_id version does not match bundle_version")

    selected_ver, bundle = _resolve_bundle_from_host(host=host, bundle_id=bid_base, bundle_version=bundle_version or bid_ver)

    rel = bundle.manifest.flow_path_for(fid)
    if not isinstance(rel, str) or not rel.strip():
        raise HTTPException(status_code=404, detail=f"Flow '{fid}' not found in bundle '{bid_base}@{selected_ver}'")

    raw = bundle.read_json(rel)
    if not isinstance(raw, dict):
        raise HTTPException(status_code=500, detail="Flow JSON is invalid")

    return {
        "bundle_id": bid_base,
        "bundle_version": selected_ver,
        "bundle_ref": f"{bid_base}@{selected_ver}",
        "flow_id": fid,
        "workflow_id": f"{bid_base}@{selected_ver}:{fid}",
        "flow": raw,
    }


@router.get("/workflows/{workflow_id}/flow")
async def get_workflow_flow(workflow_id: str) -> Dict[str, Any]:
    """Return a VisualFlow JSON by workflow_id.

    Supports gateway-generated dynamic workflows (e.g. scheduled wrapper flows) and, as a
    convenience, bundle workflows in the form `bundle_id:flow_id`.
    """
    svc = get_gateway_service()
    host = _require_bundle_host(svc)

    wid = str(workflow_id or "").strip()
    if not wid:
        raise HTTPException(status_code=400, detail="workflow_id is required")

    dyn_path_fn = getattr(host, "_dynamic_flow_path", None)
    dyn_path = dyn_path_fn(wid) if callable(dyn_path_fn) else None
    if dyn_path is not None:
        try:
            p = Path(dyn_path)
            if p.exists() and p.is_file():
                raw = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise HTTPException(status_code=500, detail="Dynamic flow JSON is invalid")
                return {"workflow_id": wid, "flow": raw, "source": "dynamic"}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to read dynamic flow: {e}")

    parsed = wid.split(":", 1)
    if len(parsed) == 2 and parsed[0] and parsed[1]:
        bid_ref, fid = parsed[0].strip(), parsed[1].strip()
        bid_base, bid_ver = _split_bundle_ref(bid_ref)
        bundles = getattr(host, "bundles", {}) or {}
        if isinstance(bundles, dict) and bid_base in bundles:
            return await get_bundle_flow(bid_base, fid, bundle_version=bid_ver)

    raise HTTPException(status_code=404, detail=f"Workflow '{wid}' not found")


@router.post("/runs/start", response_model=StartRunResponse)
async def start_run(req: StartRunRequest) -> StartRunResponse:
    svc = get_gateway_service()
    svc.runner.start()

    flow_id = str(req.flow_id or "").strip()
    bundle_id = str(req.bundle_id).strip() if isinstance(req.bundle_id, str) and str(req.bundle_id).strip() else None
    bundle_version = (
        str(req.bundle_version).strip() if isinstance(req.bundle_version, str) and str(req.bundle_version).strip() else None
    )
    if not flow_id and not bundle_id:
        raise HTTPException(status_code=400, detail="flow_id is required (or provide bundle_id to start a bundle entrypoint)")

    try:
        session_id = str(req.session_id).strip() if isinstance(req.session_id, str) and str(req.session_id).strip() else None
        input_data = dict(req.input_data or {})
        # Default workspace_root behavior (cross-client):
        # - If omitted, create a per-run workspace under the gateway data_dir.
        # - Clients can still override by explicitly setting input_data["workspace_root"].
        raw_ws = input_data.get("workspace_root")
        if not (isinstance(raw_ws, str) and raw_ws.strip()):
            base = Path(svc.config.data_dir) / "workspaces"
            base.mkdir(parents=True, exist_ok=True)
            ws_dir = base / uuid.uuid4().hex
            ws_dir.mkdir(parents=True, exist_ok=True)
            input_data["workspace_root"] = str(ws_dir)
        run_id = svc.host.start_run(
            flow_id=flow_id,
            bundle_id=bundle_id,
            bundle_version=bundle_version,
            input_data=input_data,
            actor_id="gateway",
            session_id=session_id,
        )
    except KeyError:
        # Best-effort error message: in bundle mode, KeyError can refer to either a bundle or a flow.
        msg = f"Flow '{flow_id}' not found" if flow_id else "Bundle not found"
        raise HTTPException(status_code=404, detail=msg)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to start run: {e}")
    return StartRunResponse(run_id=str(run_id))


@router.post("/runs/schedule", response_model=StartRunResponse)
async def start_scheduled_run(req: ScheduleRunRequest) -> StartRunResponse:
    """Start a durable scheduled run that triggers a target workflow over time.

    Implementation notes:
    - The gateway generates a small wrapper VisualFlow and registers it as a dynamic workflow.
    - The scheduled parent run starts that wrapper workflow and launches the target workflow as child runs.
    """
    svc = get_gateway_service()
    svc.runner.start()

    host = getattr(svc, "host", None)
    if host is None or not callable(getattr(host, "register_dynamic_visualflow", None)):
        raise HTTPException(status_code=400, detail="Dynamic workflow registration is not supported on this gateway host")

    bundle_id_ref = str(req.bundle_id or "").strip()
    flow_id_raw = str(req.flow_id or "").strip()
    if not bundle_id_ref or not flow_id_raw:
        raise HTTPException(status_code=400, detail="bundle_id and flow_id are required")

    bundle_id_base, bundle_id_ver = _split_bundle_ref(bundle_id_ref)
    if not bundle_id_base:
        raise HTTPException(status_code=400, detail="bundle_id is required")

    requested_ver = str(req.bundle_version or "").strip() if isinstance(req.bundle_version, str) and str(req.bundle_version).strip() else ""
    if bundle_id_ver and requested_ver and bundle_id_ver != requested_ver:
        raise HTTPException(status_code=400, detail="bundle_version conflicts with bundle_id (bundle_id already includes '@version')")

    latest0 = getattr(host, "latest_bundle_versions", None)
    latest = latest0 if isinstance(latest0, dict) else {}

    # Normalize target workflow id (bundle@ver:flow).
    if ":" in flow_id_raw:
        parsed = _parse_namespaced_workflow_id(flow_id_raw)
        if not parsed:
            raise HTTPException(status_code=400, detail="Invalid flow_id")
        prefix_base, prefix_ver = _split_bundle_ref(parsed[0])
        if prefix_base != bundle_id_base:
            raise HTTPException(status_code=400, detail="flow_id bundle prefix does not match bundle_id")
        if prefix_ver and requested_ver and prefix_ver != requested_ver:
            raise HTTPException(status_code=400, detail="flow_id version does not match bundle_version")
        if prefix_ver and bundle_id_ver and prefix_ver != bundle_id_ver:
            raise HTTPException(status_code=400, detail="flow_id version does not match bundle_id")
        selected_ver = prefix_ver or requested_ver or bundle_id_ver or str(latest.get(bundle_id_base) or "").strip()
        if not selected_ver:
            raise HTTPException(status_code=404, detail=f"Bundle '{bundle_id_base}' has no versions loaded")
        target_flow_id = parsed[1]
        target_workflow_id = f"{bundle_id_base}@{selected_ver}:{target_flow_id}"
    else:
        target_flow_id = flow_id_raw
        selected_ver = requested_ver or bundle_id_ver or str(latest.get(bundle_id_base) or "").strip()
        if not selected_ver:
            raise HTTPException(status_code=404, detail=f"Bundle '{bundle_id_base}' has no versions loaded")
        target_workflow_id = f"{bundle_id_base}@{selected_ver}:{target_flow_id}"

    specs = getattr(host, "specs", None)
    if not isinstance(specs, dict) or target_workflow_id not in specs:
        raise HTTPException(status_code=404, detail=f"Target workflow '{target_workflow_id}' not found")

    start_at = str(req.start_at or "").strip().lower()
    if not start_at or start_at == "now":
        start_schedule = "0s"
        start_at_iso: Optional[str] = None
    else:
        start_schedule = str(req.start_at or "").strip()
        start_at_iso = start_schedule

    interval = str(req.interval or "").strip()
    interval = interval if interval else ""
    interval_opt: Optional[str] = interval or None

    repeat_count = int(req.repeat_count) if isinstance(req.repeat_count, int) else None
    if repeat_count is not None and repeat_count <= 0:
        raise HTTPException(status_code=400, detail="repeat_count must be >= 1")

    if repeat_count is not None and repeat_count > 1 and not interval_opt:
        raise HTTPException(status_code=400, detail="repeat_count > 1 requires interval")

    repeat_until_raw = str(req.repeat_until).strip() if isinstance(req.repeat_until, str) and str(req.repeat_until).strip() else None
    if repeat_until_raw and repeat_count is not None:
        raise HTTPException(status_code=400, detail="repeat_until cannot be combined with repeat_count")
    if repeat_until_raw and not interval_opt:
        raise HTTPException(status_code=400, detail="repeat_until requires interval")

    def _parse_iso_datetime_utc(s: str) -> datetime.datetime:
        v = str(s or "").strip()
        if not v:
            raise ValueError("empty datetime")
        v2 = v.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(v2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)

    def _parse_interval_seconds(s: str) -> int:
        v = str(s or "").strip().lower()
        m = re.match(r"^(\d+)\s*([smhd])$", v)
        if not m:
            raise ValueError("invalid interval (expected like '15m', '1h', '2d')")
        n = int(m.group(1))
        unit = m.group(2)
        if n <= 0:
            raise ValueError("invalid interval (must be >= 1)")
        if unit == "s":
            mul = 1
        elif unit == "m":
            mul = 60
        elif unit == "h":
            mul = 3600
        else:
            mul = 86400  # 'd'
        return n * mul

    if repeat_until_raw and repeat_count is None and interval_opt:
        try:
            until_dt = _parse_iso_datetime_utc(repeat_until_raw)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid repeat_until: {e}")

        # For "now", approximate start as request time in UTC (good enough for UX).
        try:
            start_dt = datetime.datetime.now(tz=datetime.timezone.utc) if start_at_iso is None else _parse_iso_datetime_utc(start_at_iso)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid start_at: {e}")

        if until_dt < start_dt:
            raise HTTPException(status_code=400, detail="repeat_until must be >= start_at")
        try:
            interval_s = _parse_interval_seconds(interval_opt)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid interval: {e}")

        # Inclusive: include the execution at start_dt, then every interval_s while <= until_dt.
        span_s = int((until_dt - start_dt).total_seconds())
        derived = 1 + max(0, span_s // interval_s)
        if derived > 10_000:
            raise HTTPException(status_code=400, detail="repeat_until implies too many executions (max 10000)")
        repeat_count = int(derived)

    # If interval is set and repeat_count omitted, repeat forever.
    if interval_opt and repeat_count == 1:
        # Treat as a one-shot; interval isn't needed.
        interval_opt = None

    share_context = bool(req.share_context)
    scheduled_workflow_id = f"scheduled:{uuid.uuid4()}"
    session_prefix = scheduled_workflow_id if not share_context else None
    try:
        wrapper = _build_scheduled_wrapper_visualflow(
            workflow_id=scheduled_workflow_id,
            target_workflow_id=target_workflow_id,
            start_schedule=start_schedule,
            interval=interval_opt,
            repeat_count=repeat_count,
            share_context=share_context,
            session_prefix=session_prefix,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to build schedule wrapper: {e}")

    try:
        host.register_dynamic_visualflow(wrapper, persist=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to register schedule wrapper workflow: {e}")

    input_data = dict(req.input_data or {})
    schedule_meta: Dict[str, Any] = {
        "kind": "scheduled_run",
        "target_workflow_id": target_workflow_id,
        "target_bundle_id": bundle_id_base,
        "target_bundle_version": selected_ver,
        "target_bundle_ref": f"{bundle_id_base}@{selected_ver}",
        "target_flow_id": target_flow_id,
        "start_at": start_at_iso,
        "interval": interval_opt,
        "repeat_count": repeat_count,
        "repeat_until": repeat_until_raw,
        "share_context": share_context,
        "session_prefix": session_prefix,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
    }
    wrapper_vars: Dict[str, Any] = {
        "vars": input_data,
        **({"session_prefix": session_prefix} if isinstance(session_prefix, str) and session_prefix.strip() else {}),
        "_meta": {"schedule": schedule_meta},
    }
    # Best-effort: lift a common prompt string to the parent run for UX/digest.
    prompt_text = input_data.get("prompt")
    if isinstance(prompt_text, str) and prompt_text.strip():
        wrapper_vars["prompt"] = prompt_text.strip()

    try:
        session_id = str(req.session_id).strip() if isinstance(req.session_id, str) and str(req.session_id).strip() else None
        run_id = host.start_run(flow_id=scheduled_workflow_id, bundle_id=None, input_data=wrapper_vars, actor_id="gateway", session_id=session_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to start scheduled run: {e}")

    return StartRunResponse(run_id=str(run_id))


@router.get("/runs/{run_id}")
async def get_run(run_id: str) -> Dict[str, Any]:
    svc = get_gateway_service()
    rs = svc.host.run_store
    try:
        run = rs.load(str(run_id))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load run: {e}")
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    return run_summary(run)


@router.get("/runs")
async def list_runs(
    limit: int = Query(50, ge=1, le=500, description="Maximum number of runs (most recent first)."),
    status: Optional[str] = Query(None, description="Optional status filter: running|waiting|completed|failed|cancelled"),
    workflow_id: Optional[str] = Query(None, description="Optional workflow id filter (e.g. bundle:flow)"),
    session_id: Optional[str] = Query(None, description="Optional session id filter (durable run.session_id)."),
    root_only: bool = Query(False, description="If true, return only root/parent runs (parent_run_id is empty)."),
) -> Dict[str, Any]:
    """List recent runs (summary only; never returns full run.vars)."""
    svc = get_gateway_service()
    rs = svc.host.run_store

    if not isinstance(rs, QueryableRunStore):
        raise HTTPException(status_code=400, detail="Run store does not support listing runs")

    status_enum: Optional[RunStatus] = None
    if isinstance(status, str) and status.strip():
        s = status.strip().lower()
        try:
            status_enum = RunStatus(s)  # type: ignore[arg-type]
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid status (expected: running|waiting|completed|failed|cancelled)")

    wid = str(workflow_id).strip() if isinstance(workflow_id, str) and workflow_id.strip() else None
    sid = str(session_id).strip() if isinstance(session_id, str) and session_id.strip() else None
    filter_internal = wid is None

    try:
        internal_limit = 10_000 if (sid or bool(root_only)) else int(limit)
        runs = rs.list_runs(status=status_enum, workflow_id=wid, limit=internal_limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list runs: {e}")

    if sid:
        runs = [r for r in (runs or []) if str(getattr(r, "session_id", "") or "").strip() == sid]

    if bool(root_only):
        runs = [r for r in (runs or []) if not str(getattr(r, "parent_run_id", "") or "").strip()]

    ledger_store = getattr(getattr(svc, "host", None), "ledger_store", None)
    items: list[Dict[str, Any]] = []
    for r in (runs or []):
        if filter_internal:
            wf_id = getattr(r, "workflow_id", None)
            if isinstance(wf_id, str) and wf_id.startswith("__"):
                # Internal runtime bookkeeping runs (e.g. __global_memory__/__session_memory__).
                continue
        item = run_summary(r)
        rid = str(item.get("run_id") or getattr(r, "run_id", "") or "").strip()
        if rid and ledger_store is not None:
            try:
                count_fn = getattr(ledger_store, "count", None)
                if callable(count_fn):
                    item["ledger_len"] = int(count_fn(rid))
                else:
                    records = ledger_store.list(rid)
                    item["ledger_len"] = int(len(records) if isinstance(records, list) else 0)
            except Exception:
                item["ledger_len"] = None
        items.append(item)
        if len(items) >= int(limit):
            break

    return {"items": items}


@router.get("/runs/{run_id}/input_data")
async def get_run_input_data(run_id: str) -> Dict[str, Any]:
    """Return a best-effort view of the original start inputs for a run.

    Security note:
    - This endpoint is protected by the same gateway auth layer as other read endpoints.
    - We avoid returning full `run.vars`; when possible (bundle mode), we filter to the entrypoint pin ids.
    """
    svc = get_gateway_service()
    rs = svc.host.run_store
    try:
        run = rs.load(str(run_id))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load run: {e}")
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    workflow_id = getattr(run, "workflow_id", None)
    vars_obj = getattr(run, "vars", None)
    if not isinstance(vars_obj, dict):
        vars_obj = {}

    # Scheduled wrapper runs: expose the target bundle/flow + the target input payload.
    try:
        meta0 = vars_obj.get("_meta") if isinstance(vars_obj, dict) else None
        meta = meta0 if isinstance(meta0, dict) else None
        schedule = meta.get("schedule") if isinstance(meta, dict) else None
        if isinstance(schedule, dict):
            tb = schedule.get("target_bundle_id")
            tbv = schedule.get("target_bundle_version")
            tbr = schedule.get("target_bundle_ref")
            tf = schedule.get("target_flow_id")
            if isinstance(tb, str) and tb.strip() and isinstance(tf, str) and tf.strip():
                bundle_id2 = tb.strip()
                flow_id2 = tf.strip()
                target_vars = vars_obj.get("vars") if isinstance(vars_obj.get("vars"), dict) else {}

                # Best-effort pin filtering for the target flow.
                host = getattr(svc, "host", None)
                try:
                    selected_ver, bundle = _resolve_bundle_from_host(
                        host=host,
                        bundle_id=str(tbr or bundle_id2),
                        bundle_version=str(tbv).strip() if isinstance(tbv, str) and str(tbv).strip() else None,
                    )
                    rel = bundle.manifest.flow_path_for(flow_id2)
                    raw = bundle.read_json(rel) if isinstance(rel, str) and rel.strip() else None
                    pins = _extract_entrypoint_inputs_from_visualflow(raw)
                    pin_ids = [str(p.get("id")) for p in pins if isinstance(p, dict) and isinstance(p.get("id"), str)]
                    if pin_ids:
                        allowed = set(pin_ids)
                        filtered = {k: v for k, v in target_vars.items() if isinstance(k, str) and k in allowed}
                        return {
                            "run_id": str(getattr(run, "run_id", run_id)),
                            "workflow_id": str(workflow_id or ""),
                            "bundle_id": bundle_id2,
                            "bundle_version": selected_ver,
                            "flow_id": flow_id2,
                            "pin_ids": pin_ids,
                            "input_data": filtered,
                        }
                except Exception:
                    pass

                # Fallback: return the raw target vars.
                return {
                    "run_id": str(getattr(run, "run_id", run_id)),
                    "workflow_id": str(workflow_id or ""),
                    "bundle_id": bundle_id2,
                    "flow_id": flow_id2,
                    "pin_ids": [],
                    "input_data": target_vars if isinstance(target_vars, dict) else {},
                }
    except Exception:
        pass

    bundle_id: Optional[str] = None
    flow_id: Optional[str] = None
    pin_ids: list[str] = []

    parsed = _parse_namespaced_workflow_id(str(workflow_id or ""))
    if parsed is not None:
        bundle_id, flow_id = parsed

        host = getattr(svc, "host", None)
        try:
            selected_ver, bundle = _resolve_bundle_from_host(host=host, bundle_id=str(bundle_id), bundle_version=None)
            rel = bundle.manifest.flow_path_for(flow_id)
            raw = bundle.read_json(rel) if isinstance(rel, str) and rel.strip() else None
            pins = _extract_entrypoint_inputs_from_visualflow(raw)
            pin_ids = [str(p.get("id")) for p in pins if isinstance(p, dict) and isinstance(p.get("id"), str)]
            if pin_ids:
                allowed = set(pin_ids)
                filtered = {k: v for k, v in vars_obj.items() if isinstance(k, str) and k in allowed}
                bid_base, _bid_ver = _split_bundle_ref(str(bundle_id))
                return {
                    "run_id": str(getattr(run, "run_id", run_id)),
                    "workflow_id": str(workflow_id or ""),
                    "bundle_id": bid_base,
                    "bundle_version": selected_ver,
                    "flow_id": flow_id,
                    "pin_ids": pin_ids,
                    "input_data": filtered,
                }
        except Exception:
            # Fall back to generic filtering below.
            pin_ids = []

    # Fallback: exclude private namespaces (e.g. _runtime/_temp).
    filtered2 = {k: v for k, v in vars_obj.items() if isinstance(k, str) and not k.startswith("_")}
    return {
        "run_id": str(getattr(run, "run_id", run_id)),
        "workflow_id": str(workflow_id or ""),
        "bundle_id": bundle_id,
        "flow_id": flow_id,
        "pin_ids": pin_ids,
        "input_data": filtered2,
    }


@router.get("/runs/{run_id}/artifacts", response_model=ArtifactListResponse)
async def list_run_artifacts(
    run_id: str,
    limit: int = Query(200, ge=1, le=2000, description="Maximum number of artifacts (most recent first)."),
) -> ArtifactListResponse:
    """List artifacts associated with a run (read-only, v0)."""
    svc = get_gateway_service()
    rs = svc.host.run_store
    try:
        run = rs.load(str(run_id))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load run: {e}")
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    store = getattr(getattr(svc, "stores", None), "artifact_store", None)
    list_fn = getattr(store, "list_by_run", None)
    if not callable(list_fn):
        raise HTTPException(status_code=400, detail="Artifact store does not support listing by run")

    try:
        items0 = list_fn(str(run_id))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list artifacts: {e}")

    rows: list[ArtifactListItem] = []
    for m in items0 or []:
        try:
            artifact_id = str(getattr(m, "artifact_id", "") or "").strip()
            if not artifact_id:
                continue
            rows.append(
                ArtifactListItem(
                    artifact_id=artifact_id,
                    content_type=getattr(m, "content_type", None),
                    size_bytes=getattr(m, "size_bytes", None),
                    created_at=getattr(m, "created_at", None),
                    tags=dict(getattr(m, "tags", None) or {}) if isinstance(getattr(m, "tags", None), dict) else {},
                )
            )
        except Exception:
            continue

    rows.sort(key=lambda r: str(r.created_at or ""), reverse=True)
    return ArtifactListResponse(items=rows[: int(limit)])


@router.get("/runs/{run_id}/artifacts/{artifact_id}")
async def get_run_artifact_metadata(run_id: str, artifact_id: str) -> Dict[str, Any]:
    """Return artifact metadata without downloading content (read-only, v0)."""
    svc = get_gateway_service()
    rs = svc.host.run_store
    try:
        run = rs.load(str(run_id))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load run: {e}")
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    store = getattr(getattr(svc, "stores", None), "artifact_store", None)
    meta_fn = getattr(store, "get_metadata", None)
    if not callable(meta_fn):
        raise HTTPException(status_code=400, detail="Artifact store does not support metadata reads")

    try:
        meta = meta_fn(str(artifact_id))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid artifact id: {e}")
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Artifact '{artifact_id}' not found")

    rid = str(getattr(meta, "run_id", "") or "").strip()
    if rid != str(run_id):
        raise HTTPException(status_code=404, detail=f"Artifact '{artifact_id}' not found")

    to_dict = getattr(meta, "to_dict", None)
    out = to_dict() if callable(to_dict) else {}
    if not isinstance(out, dict):
        out = {}
    return out


@router.get("/runs/{run_id}/artifacts/{artifact_id}/content")
async def download_run_artifact_content(run_id: str, artifact_id: str) -> StreamingResponse:
    """Download artifact bytes (streaming best-effort)."""
    svc = get_gateway_service()
    rs = svc.host.run_store
    try:
        run = rs.load(str(run_id))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load run: {e}")
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    store = getattr(getattr(svc, "stores", None), "artifact_store", None)
    meta_fn = getattr(store, "get_metadata", None)
    if not callable(meta_fn):
        raise HTTPException(status_code=400, detail="Artifact store does not support metadata reads")

    try:
        meta = meta_fn(str(artifact_id))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid artifact id: {e}")
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Artifact '{artifact_id}' not found")

    rid = str(getattr(meta, "run_id", "") or "").strip()
    if rid != str(run_id):
        raise HTTPException(status_code=404, detail=f"Artifact '{artifact_id}' not found")

    content_type = str(getattr(meta, "content_type", None) or "application/octet-stream")

    # Best-effort streaming for file-backed stores.
    content_path = None
    try:
        path_fn = getattr(store, "_content_path", None)
        if callable(path_fn):
            content_path = path_fn(str(artifact_id))
    except Exception:
        content_path = None

    if content_path is not None:
        try:
            path = Path(content_path)
            if not path.exists():
                raise HTTPException(status_code=404, detail=f"Artifact '{artifact_id}' not found")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to resolve artifact content path: {e}")

        def _iterfile():
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk

        return StreamingResponse(_iterfile(), media_type=content_type)

    # Fallback: load into memory (portable but not streaming).
    load_fn = getattr(store, "load", None)
    if not callable(load_fn):
        raise HTTPException(status_code=400, detail="Artifact store does not support content reads")
    artifact = load_fn(str(artifact_id))
    if artifact is None:
        raise HTTPException(status_code=404, detail=f"Artifact '{artifact_id}' not found")

    def _single_chunk():
        yield getattr(artifact, "content", b"") or b""

    return StreamingResponse(_single_chunk(), media_type=content_type)


@router.get("/runs/{run_id}/ledger")
async def get_ledger(
    run_id: str,
    after: int = Query(0, ge=0, description="Cursor: number of records already consumed."),
    limit: int = Query(200, ge=1, le=2000),
) -> Dict[str, Any]:
    svc = get_gateway_service()
    ledger = svc.host.ledger_store.list(str(run_id))
    if not isinstance(ledger, list):
        ledger = []
    a = int(after or 0)
    items = ledger[a : a + int(limit)]
    next_after = a + len(items)
    return {"items": items, "next_after": next_after}


@router.post("/runs/ledger/batch")
async def get_ledger_batch(req: LedgerBatchRequest) -> Dict[str, Any]:
    """Fetch incremental ledger pages for multiple runs in a single request.

    This exists to reduce client→gateway request fanout when observing runs with many subflows.
    """
    svc = get_gateway_service()
    limit = int(req.limit or 200)
    out: Dict[str, Any] = {}

    for it in req.runs or []:
        rid = str(getattr(it, "run_id", "") or "").strip()
        if not rid:
            continue
        after = int(getattr(it, "after", 0) or 0)
        if after < 0:
            after = 0

        ledger = svc.host.ledger_store.list(rid)
        if not isinstance(ledger, list):
            ledger = []

        items = ledger[after : after + limit]
        next_after = after + len(items)
        out[rid] = {"items": items, "next_after": next_after}

    return {"runs": out}


@router.get("/runs/{run_id}/ledger/stream")
async def stream_ledger(
    run_id: str,
    after: int = Query(0, ge=0, description="Cursor: number of records already consumed."),
    heartbeat_s: float = Query(5.0, gt=0.1, le=60.0),
) -> StreamingResponse:
    svc = get_gateway_service()
    run_id2 = str(run_id)

    async def _gen():
        cursor = int(after or 0)
        last_emit = asyncio.get_event_loop().time()
        while True:
            ledger = svc.host.ledger_store.list(run_id2)
            if not isinstance(ledger, list):
                ledger = []
            if cursor < 0:
                cursor = 0
            if cursor < len(ledger):
                while cursor < len(ledger):
                    item = ledger[cursor]
                    data = json.dumps({"cursor": cursor + 1, "record": item}, ensure_ascii=False)
                    yield f"id: {cursor + 1}\n".encode("utf-8")
                    yield b"event: step\n"
                    yield f"data: {data}\n\n".encode("utf-8")
                    cursor += 1
                    last_emit = asyncio.get_event_loop().time()
            else:
                now = asyncio.get_event_loop().time()
                if (now - last_emit) >= float(heartbeat_s):
                    yield b": keep-alive\n\n"
                    last_emit = now
                await asyncio.sleep(0.25)

    return StreamingResponse(_gen(), media_type="text/event-stream")


@router.post("/runs/{run_id}/summary", response_model=GenerateRunSummaryResponse)
async def generate_run_summary(run_id: str, req: GenerateRunSummaryRequest) -> GenerateRunSummaryResponse:
    """Generate and persist a human-readable run summary based on the durable ledger."""
    svc = get_gateway_service()
    rs = svc.host.run_store
    rid = str(run_id or "").strip()
    if not rid:
        raise HTTPException(status_code=400, detail="run_id is required")

    try:
        run = rs.load(rid)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load run: {e}")
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{rid}' not found")

    include_subruns = bool(req.include_subruns)
    run_ids = _list_descendant_run_ids(rs, rid) if include_subruns else [rid]

    ledgers: Dict[str, list[Dict[str, Any]]] = {}
    for r2 in run_ids:
        try:
            ledger = svc.host.ledger_store.list(str(r2))
            ledgers[str(r2)] = ledger if isinstance(ledger, list) else []
        except Exception:
            ledgers[str(r2)] = []

    per_run: Dict[str, Any] = {k: _extract_digest_from_ledger(v) for k, v in ledgers.items()}
    overall: Dict[str, Any] = _extract_digest_from_ledger([x for v in ledgers.values() for x in (v or [])])

    prompt_text: Optional[str] = None
    try:
        rv = getattr(run, "vars", None)
        if isinstance(rv, dict):
            p2 = rv.get("prompt")
            if isinstance(p2, str) and p2.strip():
                prompt_text = p2.strip()
    except Exception:
        prompt_text = None

    status_str = getattr(getattr(run, "status", None), "value", None) or str(getattr(run, "status", "") or "")
    context: Dict[str, Any] = {
        "run_id": rid,
        "workflow_id": str(getattr(run, "workflow_id", "") or ""),
        "status": str(status_str or ""),
        "prompt": prompt_text,
        "overall": overall,
        "per_run": per_run,
    }

    provider = str(req.provider or "lmstudio").strip() or "lmstudio"
    model = str(req.model or "qwen/qwen3-next-80b").strip() or "qwen/qwen3-next-80b"

    try:
        summary_text = await asyncio.to_thread(_generate_summary_text, provider=provider, model=model, context=context)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to generate summary: {e}")

    generated_at = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    payload = {
        "text": summary_text,
        "generated_at": generated_at,
        "provider": provider,
        "model": model,
        "source": {
            "run_id": rid,
            "include_subruns": include_subruns,
            "ledger_len_by_run": {k: len(v or []) for k, v in ledgers.items()},
        },
    }

    try:
        eff = Effect(type=EffectType.EMIT_EVENT, payload={"name": "abstract.summary", "scope": "run", "run_id": rid, "payload": payload})
        rec = StepRecord.start(run=run, node_id="observer", effect=eff, idempotency_key=f"observer:summary:{generated_at}")
        rec.finish_success({"emitted": True, "name": "abstract.summary", "payload": payload})
        svc.host.ledger_store.append(rec)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to persist summary: {e}")

    return GenerateRunSummaryResponse(
        ok=True,
        run_id=rid,
        provider=provider,
        model=model,
        generated_at=generated_at,
        summary=summary_text,
    )


@router.post("/runs/{run_id}/chat", response_model=RunChatResponse)
async def run_chat(run_id: str, req: RunChatRequest) -> RunChatResponse:
    """Generate a read-only chat answer grounded in the durable ledger (optionally persisted)."""
    svc = get_gateway_service()
    rs = svc.host.run_store
    rid = str(run_id or "").strip()
    if not rid:
        raise HTTPException(status_code=400, detail="run_id is required")

    try:
        run = rs.load(rid)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load run: {e}")
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{rid}' not found")

    include_subruns = bool(req.include_subruns)
    run_ids = _list_descendant_run_ids(rs, rid) if include_subruns else [rid]

    ledgers: Dict[str, list[Dict[str, Any]]] = {}
    for r2 in run_ids:
        try:
            ledger = svc.host.ledger_store.list(str(r2))
            ledgers[str(r2)] = ledger if isinstance(ledger, list) else []
        except Exception:
            ledgers[str(r2)] = []

    def _slim_text(value: Any, *, max_len: int) -> str:
        return _clamp_text(str(value or ""), max_len=max_len)

    def _slim_json(value: Any, *, max_len: int) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return _slim_text(value, max_len=max_len)
        try:
            return _clamp_text(json.dumps(value, ensure_ascii=False, indent=2), max_len=max_len)
        except Exception:
            return _slim_text(value, max_len=max_len)

    def _extract_llm_prompt(payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        p = payload.get("prompt")
        if isinstance(p, str) and p.strip():
            return p.strip()
        msgs = payload.get("messages")
        if isinstance(msgs, list):
            for m in reversed(msgs):
                if not isinstance(m, dict):
                    continue
                if str(m.get("role") or "") != "user":
                    continue
                c = m.get("content")
                if isinstance(c, str) and c.strip():
                    return c.strip()
        return ""

    def _slim_ledger_record(rec: Any) -> Dict[str, Any]:
        if not isinstance(rec, dict):
            return {}
        eff = rec.get("effect") if isinstance(rec.get("effect"), dict) else {}
        eff_type = str(eff.get("type") or "").strip()
        payload = eff.get("payload") if isinstance(eff.get("payload"), dict) else {}
        result = rec.get("result")
        node_id = str(rec.get("node_id") or "").strip() or None
        ts = str(rec.get("ended_at") or rec.get("started_at") or "").strip() or None
        out: Dict[str, Any] = {
            "ts": ts,
            "node_id": node_id,
            "status": str(rec.get("status") or "").strip() or None,
            "effect_type": eff_type or None,
            "error": _slim_text(rec.get("error"), max_len=400) if rec.get("error") else None,
        }

        if eff_type == "llm_call":
            prompt = _extract_llm_prompt(payload)
            content = ""
            usage = None
            if isinstance(result, dict):
                content = str(result.get("content") or result.get("response") or "").strip()
                usage = result.get("usage") or result.get("token_usage")
            elif isinstance(result, str):
                content = result.strip()
            out["llm"] = {
                "provider": str(payload.get("provider") or "").strip() or None,
                "model": str(payload.get("model") or "").strip() or None,
                "prompt": _slim_text(prompt, max_len=2400),
                "response": _slim_text(content, max_len=2400),
                "missing_response": not bool(content),
                "usage": usage if isinstance(usage, dict) else None,
            }
            return out

        if eff_type == "tool_calls":
            calls_raw = payload.get("tool_calls")
            calls = calls_raw if isinstance(calls_raw, list) else []
            results = []
            if isinstance(result, dict) and isinstance(result.get("results"), list):
                results = [r for r in result.get("results") if isinstance(r, dict)]
            out["tools"] = {
                "tool_calls": [
                    {
                        "name": str(c.get("name") or "").strip() or None,
                        "call_id": str(c.get("call_id") or c.get("id") or "").strip() or None,
                        "arguments": c.get("arguments") if isinstance(c.get("arguments"), dict) else {},
                    }
                    for c in calls[:20]
                    if isinstance(c, dict)
                ],
                "results": [
                    {
                        "call_id": str(r.get("call_id") or r.get("id") or "").strip() or None,
                        "success": r.get("success") if isinstance(r.get("success"), bool) else None,
                        "error": _slim_text(r.get("error"), max_len=800) if r.get("error") else None,
                        "output": _slim_json(r.get("output"), max_len=50000) if r.get("output") is not None else None,
                    }
                    for r in results[:20]
                ],
            }
            return out

        if eff_type == "emit_event":
            out["event"] = {
                "name": str(payload.get("name") or "").strip() or None,
                "payload": _slim_json(payload.get("payload"), max_len=4000) if payload.get("payload") is not None else None,
            }
            return out

        if isinstance(payload, dict) and payload:
            out["payload"] = _slim_json(payload, max_len=1500)
        if result is not None:
            out["result"] = _slim_json(result, max_len=1500)
        return out

    def _ledger_excerpt(items: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
        if not items:
            return []
        if len(items) <= 120:
            return [_slim_ledger_record(x) for x in items if isinstance(x, dict)]
        head = items[:30]
        tail = items[-90:]
        merged = head + tail
        return [_slim_ledger_record(x) for x in merged if isinstance(x, dict)]

    ledger_excerpt_by_run: Dict[str, Any] = {k: _ledger_excerpt(v or []) for k, v in ledgers.items()}

    per_run: Dict[str, Any] = {k: _extract_digest_from_ledger(v) for k, v in ledgers.items()}
    overall: Dict[str, Any] = _extract_digest_from_ledger([x for v in ledgers.values() for x in (v or [])])

    prompt_text: Optional[str] = None
    try:
        rv = getattr(run, "vars", None)
        if isinstance(rv, dict):
            p2 = rv.get("prompt")
            if isinstance(p2, str) and p2.strip():
                prompt_text = p2.strip()
    except Exception:
        prompt_text = None

    status_str = getattr(getattr(run, "status", None), "value", None) or str(getattr(run, "status", "") or "")
    context: Dict[str, Any] = {
        "run_id": rid,
        "workflow_id": str(getattr(run, "workflow_id", "") or ""),
        "status": str(status_str or ""),
        "prompt": prompt_text,
        "overall": overall,
        "per_run": per_run,
        # Best-effort: include a compact excerpt of the ledger for grounding (keeps the JSON valid under clamping).
        "ledger_len_by_run": {k: len(v or []) for k, v in ledgers.items()},
        "ledger_excerpt_by_run": ledger_excerpt_by_run,
    }

    provider = str(req.provider or "lmstudio").strip() or "lmstudio"
    model = str(req.model or "qwen/qwen3-next-80b").strip() or "qwen/qwen3-next-80b"
    messages = list(req.messages or [])

    try:
        answer_text = await asyncio.to_thread(_generate_chat_text, provider=provider, model=model, context=context, messages=messages)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to generate chat answer: {e}")

    generated_at = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())

    if bool(req.persist):
        # Persist a single Q/A exchange (last user message + assistant answer).
        question: str = ""
        for m in reversed(messages):
            if isinstance(m, dict) and str(m.get("role") or "") == "user":
                c = m.get("content")
                if isinstance(c, str) and c.strip():
                    question = c.strip()
                    break
        payload = {
            "generated_at": generated_at,
            "provider": provider,
            "model": model,
            "question": question,
            "answer": answer_text,
            "source": {"run_id": rid, "include_subruns": include_subruns},
        }
        try:
            eff = Effect(type=EffectType.EMIT_EVENT, payload={"name": "abstract.chat", "scope": "run", "run_id": rid, "payload": payload})
            rec = StepRecord.start(run=run, node_id="observer", effect=eff, idempotency_key=f"observer:chat:{generated_at}")
            rec.finish_success({"emitted": True, "name": "abstract.chat", "payload": payload})
            svc.host.ledger_store.append(rec)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to persist chat: {e}")

    return RunChatResponse(
        ok=True,
        run_id=rid,
        provider=provider,
        model=model,
        generated_at=generated_at,
        answer=answer_text,
    )


@router.get("/discovery/tools")
async def discovery_tools() -> Dict[str, Any]:
    """List available tool specs for thin clients (best-effort).

    Notes:
    - This is meant as UI help for constructing `input_data.tools` allowlists.
    - Tool execution mode may still vary by deployment (local vs passthrough).
    """
    try:
        from abstractruntime.integrations.abstractcore.default_tools import list_default_tool_specs

        specs = list_default_tool_specs()
    except Exception as e:
        return {"items": [], "error": str(e)}

    items: list[Dict[str, Any]] = []
    for s in specs:
        if isinstance(s, dict):
            name = s.get("name")
            if isinstance(name, str) and name.strip():
                items.append(dict(s))
    return {"items": items}


@router.get("/discovery/providers")
async def discovery_providers(include_models: bool = Query(False, description="Include model lists (may be slow).")) -> Dict[str, Any]:
    """List available providers (and optionally their models) for UI helper dropdowns."""
    try:
        from abstractcore.providers.registry import get_all_providers_with_models

        providers = get_all_providers_with_models(include_models=bool(include_models))
    except Exception as e:
        return {"items": [], "error": str(e)}

    items: list[Dict[str, Any]] = []
    if isinstance(providers, list):
        for p in providers:
            if isinstance(p, dict):
                name = p.get("name")
                if isinstance(name, str) and name.strip():
                    items.append(dict(p))

    items.sort(key=lambda x: str(x.get("name") or ""))
    return {"items": items}


@router.get("/discovery/providers/{provider_name}/models")
async def discovery_provider_models(provider_name: str) -> Dict[str, Any]:
    """List available models for a provider (best-effort; may require provider connectivity)."""
    prov = str(provider_name or "").strip()
    if not prov:
        raise HTTPException(status_code=400, detail="provider_name is required")

    try:
        from abstractcore.providers.registry import get_available_models_for_provider

        models = get_available_models_for_provider(prov)
        if not isinstance(models, list):
            models = []
        out = [str(m) for m in models if isinstance(m, str) and str(m).strip()]
        out.sort()
        return {"provider": prov, "models": out}
    except Exception as e:
        return {"provider": prov, "models": [], "error": str(e)}


@router.get("/discovery/models/capabilities")
async def discovery_model_capabilities(model_name: str = Query(..., description="Model id/name (may include provider prefix like 'lmstudio/...')")) -> Dict[str, Any]:
    """Best-effort model capability lookup for UI context meters and defaults."""
    name = str(model_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="model_name is required")

    try:
        from abstractcore.architectures.detection import get_model_capabilities

        caps = get_model_capabilities(name)
        if not isinstance(caps, dict):
            caps = {}
        return {"model": name, "capabilities": caps}
    except Exception as e:
        return {"model": name, "capabilities": {}, "error": str(e)}


@router.get("/host/metrics/gpu")
async def host_gpu_metrics() -> Dict[str, Any]:
    return host_metrics.get_host_gpu_metrics()


@router.post("/embeddings", response_model=EmbeddingsResponse)
async def embeddings(req: EmbeddingsRequest) -> EmbeddingsResponse:
    """Generate embeddings for text inputs using the gateway-wide embedding model.

    Contract:
    - Single embedding space per gateway instance (provider+model are stable).
    - Request overrides are rejected unless they match the configured provider/model.
    """
    svc = get_gateway_service()
    client = getattr(svc, "embeddings_client", None)
    if client is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Embeddings are not available (missing AbstractCore embedding integration). "
                "Install `abstractruntime[abstractcore]` and ensure an embedding provider/model is configured."
            ),
        )

    configured_provider = str(getattr(svc, "embedding_provider", "") or getattr(client, "provider", "") or "").strip().lower()
    configured_model = str(getattr(svc, "embedding_model", "") or getattr(client, "model", "") or "").strip()

    req_provider = str(req.provider or "").strip().lower() if isinstance(req.provider, str) else ""
    req_model = str(req.model or "").strip() if isinstance(req.model, str) else ""

    if req_provider and configured_provider and req_provider != configured_provider:
        raise HTTPException(
            status_code=400,
            detail=f"Embedding provider is fixed for this gateway instance (expected '{configured_provider}', got '{req_provider}')",
        )
    if req_model and configured_model and req_model != configured_model:
        raise HTTPException(
            status_code=400,
            detail=f"Embedding model is fixed for this gateway instance (expected '{configured_model}', got '{req_model}')",
        )

    raw = req.input
    if isinstance(raw, list):
        texts = [str(x or "") for x in raw]
    else:
        texts = [str(raw or "")]

    if not texts:
        return EmbeddingsResponse(provider=configured_provider, model=configured_model, dimension=0, data=[])

    try:
        result = client.embed_texts(texts)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Embedding generation failed: {e}") from e

    data = [EmbeddingItem(index=i, embedding=list(vec)) for i, vec in enumerate(result.embeddings)]
    return EmbeddingsResponse(
        provider=str(result.provider),
        model=str(result.model),
        dimension=int(result.dimension or 0),
        data=data,
    )


@router.get("/embeddings/config")
async def embeddings_config() -> Dict[str, Any]:
    """Return the gateway-wide embedding configuration (singleton per instance)."""
    svc = get_gateway_service()
    client = getattr(svc, "embeddings_client", None)

    configured_provider = str(getattr(svc, "embedding_provider", "") or getattr(client, "provider", "") or "").strip().lower()
    configured_model = str(getattr(svc, "embedding_model", "") or getattr(client, "model", "") or "").strip()

    out: Dict[str, Any] = {
        "ok": client is not None,
        "provider": configured_provider,
        "model": configured_model,
        "dimension": 0,
    }

    if client is None:
        out["error"] = (
            "Embeddings are not available (missing AbstractCore embedding integration). "
            "Install `abstractruntime[abstractcore]` and configure ABSTRACTGATEWAY_EMBEDDING_PROVIDER/MODEL."
        )
        return out

    # Best-effort dimension probe (do not fail the endpoint).
    try:
        result = client.embed_texts(["dimension probe"])
        dim = int(getattr(result, "dimension", 0) or 0)
        if not dim:
            emb = getattr(result, "embeddings", None)
            if isinstance(emb, list) and emb and isinstance(emb[0], list):
                dim = len(emb[0])
        out["dimension"] = dim
    except Exception as e:
        out["error"] = str(e)
    return out


@router.get("/files/search")
async def files_search(
    query: str = Query(..., description="Case-insensitive substring match on file path/name."),
    limit: int = Query(20, ge=1, le=200),
) -> Dict[str, Any]:
    """Search workspace files for @file mentions (best-effort).

    This is intentionally lightweight:
    - uses `.abstractignore` + defaults (same policy as AbstractCore filesystem tools)
    - returns only paths (no contents)
    - caches a file index for a short TTL to keep typing latency reasonable
    """
    q = str(query or "").strip()
    if not q:
        return {"items": []}

    base = _workspace_root()

    try:
        # Index build can be slow on large workspaces; keep async endpoints responsive.
        paths = await asyncio.to_thread(_get_file_index, base=base)
    except Exception as e:
        return {"items": [], "error": str(e)}

    ql = q.lower()
    scored: list[tuple[int, int, str]] = []
    for rel in paths:
        s = str(rel)
        low = s.lower()
        name = s.rsplit("/", 1)[-1]
        name_low = name.lower()
        if ql in name_low:
            score = 0 if name_low.startswith(ql) else 1
        elif ql in low:
            score = 2
        else:
            continue
        scored.append((score, len(s), s))

    scored.sort(key=lambda x: (x[0], x[1], x[2]))
    out = [{"path": s} for _, _, s in scored[: int(limit)]]
    return {"items": out}


@router.get("/files/read")
async def files_read(
    path: str = Query(..., description="Workspace-relative path (preferred) or absolute path under workspace root."),
    start_line: int = Query(1, ge=1),
    end_line: Optional[int] = Query(None, ge=1),
) -> Dict[str, Any]:
    """Read a workspace file for @file mentions (best-effort).

    Uses the same implementation as AbstractCore's `read_file` tool (including `.abstractignore`).
    """
    base = _workspace_root()
    resolved = _resolve_workspace_path(base=base, raw_path=path)

    try:
        from abstractcore.tools.common_tools import read_file

        content = read_file(str(resolved), start_line=start_line, end_line=end_line)
    except Exception as e:
        content = f"Error: Failed to read '{resolved}': {e}"

    try:
        rel = resolved.relative_to(base).as_posix()
    except Exception:
        rel = str(resolved)

    return {"path": rel, "content": content}


@router.post("/attachments/ingest")
async def attachments_ingest(req: AttachmentIngestRequest) -> Dict[str, Any]:
    """Ingest a workspace file as an artifact-backed attachment.

    This is a write path for attachments that preserves durability:
    - bytes are stored in ArtifactStore
    - run/ledger state stores only JSON-safe artifact refs

    Attachments are stored under the session memory owner run id so they can be
    listed/downloaded via the existing run-scoped Artifact API.
    """
    svc = get_gateway_service()

    sid = str(req.session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="session_id is required")

    base = _workspace_root()
    resolved = _resolve_workspace_path(base=base, raw_path=str(req.path or ""))

    try:
        from abstractcore.tools.abstractignore import AbstractIgnore

        ignore = AbstractIgnore.for_path(base)
        if ignore.is_ignored(resolved, is_dir=False):
            raise HTTPException(status_code=403, detail="File is ignored by .abstractignore policy")
    except HTTPException:
        raise
    except Exception:
        # Best-effort; do not block ingestion if ignore policy fails to load.
        pass

    if not resolved.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if not resolved.is_file():
        raise HTTPException(status_code=400, detail="Path is not a file")

    try:
        max_bytes_raw = str(os.getenv("ABSTRACTGATEWAY_MAX_ATTACHMENT_BYTES", "") or "").strip()
        max_bytes = int(max_bytes_raw) if max_bytes_raw else 25 * 1024 * 1024
        if max_bytes <= 0:
            max_bytes = 25 * 1024 * 1024
    except Exception:
        max_bytes = 25 * 1024 * 1024

    try:
        size = int(resolved.stat().st_size)
    except Exception:
        size = -1
    if size >= 0 and size > max_bytes:
        raise HTTPException(status_code=413, detail=f"Attachment too large ({size} bytes > {max_bytes} bytes)")

    try:
        content = resolved.read_bytes()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read file: {e}")

    filename = str(req.filename or "").strip() or resolved.name
    content_type = str(req.content_type or "").strip().lower()
    if not content_type:
        guessed, _enc = mimetypes.guess_type(filename)
        content_type = str(guessed or "application/octet-stream")

    try:
        rel = resolved.relative_to(base).as_posix()
    except Exception:
        rel = str(resolved)

    try:
        rid = _session_memory_run_id(sid)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Ensure the session memory owner run exists so the artifact API can enforce run scoping.
    rs = svc.host.run_store
    try:
        existing = rs.load(str(rid))
    except Exception:
        existing = None
    if existing is None:
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        run = RunState(
            run_id=str(rid),
            workflow_id="__session_memory__",
            status=RunStatus.COMPLETED,
            current_node="done",
            vars={
                "context": {"task": "", "messages": []},
                "scratchpad": {},
                "_runtime": {"memory_spans": []},
                "_temp": {},
                "_limits": {},
            },
            waiting=None,
            output={"messages": []},
            error=None,
            created_at=now_iso,
            updated_at=now_iso,
            actor_id=None,
            session_id=sid,
            parent_run_id=None,
        )
        try:
            rs.save(run)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to create session memory run: {e}")

    store = getattr(getattr(svc, "stores", None), "artifact_store", None)
    store_fn = getattr(store, "store", None)
    if not callable(store_fn):
        raise HTTPException(status_code=500, detail="Artifact store is not available")

    tags: Dict[str, str] = {
        "kind": "attachment",
        "source": "workspace",
        "path": str(rel),
        "filename": str(filename),
        "session_id": sid,
    }
    try:
        meta = store_fn(bytes(content), content_type=str(content_type), run_id=str(rid), tags=tags)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to store attachment artifact: {e}")

    to_dict = getattr(meta, "to_dict", None)
    meta_dict = to_dict() if callable(to_dict) else {}
    if not isinstance(meta_dict, dict):
        meta_dict = {}

    attachment_ref: Dict[str, Any] = {
        "$artifact": str(getattr(meta, "artifact_id", "") or ""),
        "filename": filename,
        "content_type": content_type,
        "source_path": rel,
    }

    return {"ok": True, "run_id": str(rid), "attachment": attachment_ref, "metadata": meta_dict}


@router.post("/commands", response_model=SubmitCommandResponse)
async def submit_command(req: SubmitCommandRequest) -> SubmitCommandResponse:
    svc = get_gateway_service()
    typ = str(req.type or "").strip()
    if typ not in {"pause", "resume", "cancel", "emit_event", "update_schedule", "compact_memory"}:
        raise HTTPException(
            status_code=400,
            detail="type must be one of pause|resume|cancel|emit_event|update_schedule|compact_memory",
        )

    record = CommandRecord(
        command_id=str(req.command_id),
        run_id=str(req.run_id),
        type=typ,
        payload=dict(req.payload or {}),
        ts=str(req.ts) if isinstance(req.ts, str) and req.ts else "",
        client_id=str(req.client_id) if isinstance(req.client_id, str) and req.client_id else None,
        seq=0,
    )

    try:
        res = svc.runner.command_store.append(record)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to append command: {e}")

    try:
        logger.info(
            "gateway_command accepted=%s dup=%s seq=%s run_id=%s type=%s command_id=%s client_id=%s",
            bool(res.accepted),
            bool(res.duplicate),
            int(res.seq),
            str(req.run_id),
            str(req.type),
            str(req.command_id),
            str(req.client_id) if req.client_id else "",
        )
    except Exception:
        pass

    return SubmitCommandResponse(accepted=bool(res.accepted), duplicate=bool(res.duplicate), seq=int(res.seq))
