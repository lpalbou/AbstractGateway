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
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from abstractruntime.storage.commands import CommandRecord
from abstractruntime.storage.base import QueryableRunIndexStore, QueryableRunStore
from abstractruntime.core.models import Effect, EffectType, RunState, RunStatus, StepRecord

from .. import host_metrics
from ..service import get_gateway_service, run_summary
from ..workflow_deprecations import WorkflowDeprecatedError


router = APIRouter(prefix="/gateway", tags=["gateway"])
logger = logging.getLogger(__name__)

_CAP_REGISTRY = None
_CAP_REGISTRY_LOCK = threading.Lock()


class _GatewayCapabilityOwner:
    def __init__(self, config: Dict[str, Any]):
        self.config = config


def _get_gateway_capability_registry():
    """Return a process-wide capability registry (lazy, best-effort).

    This keeps gateway deployments dependency-light while allowing operators to
    install optional modality packages (e.g. abstractvoice) in the same env.
    """
    global _CAP_REGISTRY
    with _CAP_REGISTRY_LOCK:
        if _CAP_REGISTRY is not None:
            return _CAP_REGISTRY

        try:
            from abstractcore.capabilities.registry import CapabilityRegistry
        except Exception as e:  # pragma: no cover
            raise HTTPException(status_code=400, detail=f"AbstractCore capabilities are not available: {e}")

        cfg: Dict[str, Any] = {}
        lang = str(os.getenv("ABSTRACTGATEWAY_VOICE_LANGUAGE", "") or "").strip()
        if lang:
            cfg["voice_language"] = lang
        allow_downloads_raw = str(os.getenv("ABSTRACTGATEWAY_VOICE_ALLOW_DOWNLOADS", "") or "").strip().lower()
        if allow_downloads_raw:
            cfg["voice_allow_downloads"] = allow_downloads_raw in {"1", "true", "yes", "y", "on"}

        owner = _GatewayCapabilityOwner(cfg)
        _CAP_REGISTRY = CapabilityRegistry(owner)
        return _CAP_REGISTRY


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


class DeprecateWorkflowRequest(BaseModel):
    flow_id: Optional[str] = Field(
        default=None,
        description="Optional entrypoint flow_id to deprecate (default: all entrypoints for the bundle).",
    )
    reason: Optional[str] = Field(default=None, description="Optional reason to record for operators.")


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


class KGQueryRequest(BaseModel):
    run_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional run id used to resolve scope owner ids when owner_id/session_id are omitted. "
            "If scope=all, run_id enables querying run+session+global."
        ),
    )
    session_id: Optional[str] = Field(
        default=None,
        description=(
            "Optional session id used to resolve the session memory owner id. "
            "If scope=all and run_id is missing/unresolvable, session_id enables querying session+global."
        ),
    )
    scope: str = Field(default="session", description="run|session|global|all")
    owner_id: Optional[str] = Field(
        default=None,
        description="Optional explicit owner_id override (bypasses run/session/global owner resolution).",
    )

    subject: Optional[str] = Field(default=None)
    predicate: Optional[str] = Field(default=None)
    object: Optional[str] = Field(default=None)

    since: Optional[str] = Field(default=None, description="observed_at >= since (ISO 8601 string compare)")
    until: Optional[str] = Field(default=None, description="observed_at <= until (ISO 8601 string compare)")
    active_at: Optional[str] = Field(default=None, description="valid_from/valid_until window intersection")

    query_text: Optional[str] = Field(default=None, description="Optional semantic query (requires embedder configured on the store).")
    min_score: Optional[float] = Field(default=None, description="Semantic similarity threshold (0..1).")

    all_owners: bool = Field(
        default=False,
        description="If true, query across all owner_ids within the selected scope(s) (debug/audit).",
    )

    limit: int = Field(default=500, ge=-1, le=10_000, description="Max results; 0 or -1 means unlimited (debug/audit).")
    order: str = Field(default="desc", description="asc|desc (observed_at for non-semantic queries)")


class KGQueryResponse(BaseModel):
    ok: bool = Field(default=True)
    scope: str
    owner_id: Optional[str] = None
    count: int = 0
    items: list[Dict[str, Any]] = Field(default_factory=list)
    warnings: Optional[list[str]] = None


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
    workspace_root: Optional[str] = Field(default=None, description="Optional workspace root override (enables custom scopes).")
    workspace_access_mode: Optional[str] = Field(default=None, description="Workspace access mode (workspace_only|workspace_or_allowed).")
    workspace_allowed_paths: Optional[str] = Field(default=None, description="Newline-separated allowed root directories (mounted).")
    workspace_ignored_paths: Optional[str] = Field(default=None, description="Newline-separated ignored paths (blocked).")


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
    if not raw:
        # Default to a stable repo root instead of whatever directory the server was launched from.
        # This improves @file search latency and keeps gateway behavior consistent across clients.
        try:
            from abstractruntime.integrations.abstractcore.workspace_scoped_tools import resolve_workspace_base_dir

            base = resolve_workspace_base_dir()
        except Exception:
            base = Path.cwd()
    else:
        base = Path(raw).expanduser()
    try:
        return base.resolve()
    except Exception:
        return base


_MOUNT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")
_WORKSPACE_MOUNTS_CACHE: dict[str, Any] = {"raw": None, "mounts": {}}


def _workspace_mounts() -> Dict[str, Path]:
    """Return configured workspace mounts (best-effort).

    Env:
      ABSTRACTGATEWAY_WORKSPACE_MOUNTS

    Format (v0): newline-separated `name=/abs/path` entries.
    """
    global _WORKSPACE_MOUNTS_CACHE
    raw = str(os.getenv("ABSTRACTGATEWAY_WORKSPACE_MOUNTS", "") or "")
    cached_raw = _WORKSPACE_MOUNTS_CACHE.get("raw")
    cached_mounts = _WORKSPACE_MOUNTS_CACHE.get("mounts")
    if raw == cached_raw and isinstance(cached_mounts, dict):
        out0: Dict[str, Path] = {}
        for k, v in cached_mounts.items():
            if isinstance(k, str) and k and isinstance(v, Path):
                out0[k] = v
        return out0

    out: Dict[str, Path] = {}
    for ln in raw.splitlines():
        line = str(ln or "").strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            logger.warning("workspace_mounts: invalid line (expected name=/abs/path): %s", line)
            continue
        name, path = line.split("=", 1)
        name = name.strip()
        path = path.strip()
        if not name or not _MOUNT_NAME_RE.match(name):
            logger.warning("workspace_mounts: invalid mount name: %s", name)
            continue
        if not path:
            logger.warning("workspace_mounts: missing path for mount '%s'", name)
            continue
        try:
            p = Path(path).expanduser()
            if not p.is_absolute():
                logger.warning("workspace_mounts: mount '%s' path must be absolute: %s", name, path)
                continue
            resolved = p.resolve()
        except Exception:
            logger.warning("workspace_mounts: mount '%s' path invalid: %s", name, path)
            continue
        try:
            if not resolved.exists():
                logger.warning("workspace_mounts: mount '%s' path does not exist: %s", name, str(resolved))
                continue
            if not resolved.is_dir():
                logger.warning("workspace_mounts: mount '%s' path is not a directory: %s", name, str(resolved))
                continue
        except Exception:
            logger.warning("workspace_mounts: mount '%s' path not accessible: %s", name, str(resolved))
            continue
        out[name] = resolved

    _WORKSPACE_MOUNTS_CACHE = {"raw": raw, "mounts": dict(out)}
    return dict(out)


def _parse_lines_or_json_list(raw: Optional[str]) -> list[str]:
    """Parse a newline-separated string or a JSON array of strings (best-effort)."""
    if raw is None:
        return []
    text = str(raw or "").strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if isinstance(x, str) and str(x).strip()]
        except Exception:
            pass
    lines = [ln.strip() for ln in text.splitlines()]
    return [ln for ln in lines if ln]


def _server_workspace_policy_public() -> Dict[str, Any]:
    mounts = _workspace_mounts()
    try:
        max_bytes_raw = str(os.getenv("ABSTRACTGATEWAY_MAX_ATTACHMENT_BYTES", "") or "").strip()
        max_bytes = int(max_bytes_raw) if max_bytes_raw else 25 * 1024 * 1024
        if max_bytes <= 0:
            max_bytes = 25 * 1024 * 1024
    except Exception:
        max_bytes = 25 * 1024 * 1024

    return {
        "target": "server",
        # Do not expose absolute server paths to thin clients.
        "mounts": sorted([{"name": name} for name in mounts.keys()], key=lambda x: x["name"]),
        "max_attachment_bytes": int(max_bytes),
        "client_workspace_scope_overrides": bool(_client_workspace_scope_overrides_enabled()),
        "allowed_access_modes": ["workspace_only", "workspace_or_allowed"]
        + (["all_except_ignored"] if _client_workspace_scope_overrides_enabled() else []),
    }


def _flag_enabled(value: Any) -> bool:
    s = str(value or "").strip().lower()
    return s in {"1", "true", "yes", "on"}


def _client_workspace_scope_overrides_enabled() -> bool:
    """Whether to honor client-provided workspace_* scoping knobs for server filesystem access.

    Security note:
    In non-local tool mode, the gateway treats web clients as "thin" clients and clamps
    workspace scope to operator-controlled roots. When running tools locally (dev mode),
    it is useful to allow the UI to drive workspace scoping directly.
    """
    if _flag_enabled(os.getenv("ABSTRACTGATEWAY_ALLOW_CLIENT_WORKSPACE_SCOPE")):
        return True
    if _flag_enabled(os.getenv("ABSTRACTGATEWAY_TRUST_CLIENT_WORKSPACE_SCOPE")):
        return True
    tool_mode = str(os.getenv("ABSTRACTGATEWAY_TOOL_MODE") or "").strip().lower()
    return tool_mode == "local"


_VALID_WORKSPACE_ACCESS_MODES: set[str] = {"workspace_only", "workspace_or_allowed", "all_except_ignored"}


def _normalize_workspace_access_mode(raw: Any) -> str:
    mode = str(raw or "").strip().lower()
    if mode in _VALID_WORKSPACE_ACCESS_MODES:
        return mode
    return "workspace_only"


def _slug_mount_name(name: str) -> str:
    """Stable mount name (<= 32 chars, lower-case, [a-z0-9_-])."""
    raw = str(name or "").strip().lower()
    if not raw:
        return "mount"
    out: list[str] = []
    prev_dash = False
    for ch in raw:
        if "a" <= ch <= "z" or "0" <= ch <= "9" or ch in {"_", "-"}:
            out.append(ch)
            prev_dash = ch == "-"
            continue
        if not prev_dash:
            out.append("-")
            prev_dash = True
    s = "".join(out).strip("-")
    if not s:
        return "mount"
    return s[:32]


def _mounts_from_allowed_paths(*, allowed_dirs: list[Path], used_names: set[str]) -> Dict[str, Path]:
    """Build deterministic {mount_name -> root} for allowed roots outside workspace_root."""
    out: Dict[str, Path] = {}
    for d in allowed_dirs:
        if not isinstance(d, Path):
            continue
        try:
            resolved = d.resolve()
        except Exception:
            resolved = d
        base = _slug_mount_name(getattr(resolved, "name", "") or "mount")
        name = base
        i = 2
        while name in used_names:
            name = f"{base}-{i}"
            i += 1
        used_names.add(name)
        out[name] = resolved
    return out


def _parse_any_string_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        out: list[str] = []
        for x in raw:
            if isinstance(x, str) and x.strip():
                out.append(x.strip())
        return out
    if isinstance(raw, str):
        return _parse_lines_or_json_list(raw)
    return []


def _resolve_user_path(raw: str, *, base: Path) -> Path:
    p = Path(str(raw or "").strip()).expanduser()
    if not p.is_absolute():
        p = base / p
    try:
        return p.resolve()
    except Exception:
        return p


def _is_under_allowed_roots(p: Path, allowed_roots: list[Path]) -> bool:
    try:
        rp = p.resolve()
    except Exception:
        rp = p
    for root in allowed_roots:
        try:
            rr = root.resolve()
        except Exception:
            rr = root
        try:
            rp.relative_to(rr)
            return True
        except Exception:
            continue
    return False


def _sanitize_run_workspace_policy(input_data: Dict[str, Any]) -> Dict[str, Any]:
    """Clamp client-provided run workspace knobs to the operator policy.

    Goal: prevent thin clients from expanding server filesystem access via run vars.
    """
    allow_overrides = _client_workspace_scope_overrides_enabled()
    base = _workspace_root()
    mounts = _workspace_mounts()
    allowed_roots = [base] + list(mounts.values())
    root_for_rel = base

    # workspace_root: allow only under operator roots (workspace root + mounts).
    raw_wr = input_data.get("workspace_root")
    if isinstance(raw_wr, str) and raw_wr.strip():
        resolved = _resolve_user_path(raw_wr, base=base)
        if allow_overrides or _is_under_allowed_roots(resolved, allowed_roots):
            input_data["workspace_root"] = str(resolved)
            root_for_rel = resolved
        else:
            input_data.pop("workspace_root", None)

    # workspace_access_mode: forbid "all_except_ignored" (can escape to arbitrary abs paths).
    raw_mode = input_data.get("workspace_access_mode")
    if raw_mode is None:
        raw_mode = input_data.get("workspaceAccessMode")
    if raw_mode is not None:
        mode = _normalize_workspace_access_mode(raw_mode)
        if not allow_overrides and mode == "all_except_ignored":
            mode = "workspace_only"
        if mode in _VALID_WORKSPACE_ACCESS_MODES:
            input_data["workspace_access_mode"] = mode
        else:
            input_data.pop("workspace_access_mode", None)
        input_data.pop("workspaceAccessMode", None)

    # workspace_allowed_paths: allow only operator roots (workspace root + mounts).
    raw_allowed = input_data.get("workspace_allowed_paths")
    if raw_allowed is None:
        raw_allowed = input_data.get("workspaceAllowedPaths")
    if raw_allowed is not None:
        allowed_items = _parse_any_string_list(raw_allowed)
        kept: list[str] = []
        for item in allowed_items:
            s = str(item or "").strip()
            if not s:
                continue
            resolved = _resolve_user_path(s, base=root_for_rel)
            if allow_overrides or _is_under_allowed_roots(resolved, allowed_roots):
                kept.append(str(resolved))
        if kept:
            # Preserve shape (list vs newline string) for UI friendliness.
            input_data["workspace_allowed_paths"] = kept if isinstance(raw_allowed, list) else "\n".join(kept)
        else:
            input_data.pop("workspace_allowed_paths", None)
        input_data.pop("workspaceAllowedPaths", None)

    # workspace_ignored_paths: denylist only; accept but normalize to newline-separated string for stability.
    raw_ignored = input_data.get("workspace_ignored_paths")
    if raw_ignored is None:
        raw_ignored = input_data.get("workspaceIgnoredPaths")
    if raw_ignored is not None:
        ignored_items = _parse_any_string_list(raw_ignored)
        if ignored_items:
            input_data["workspace_ignored_paths"] = "\n".join(ignored_items)
        else:
            input_data.pop("workspace_ignored_paths", None)
        input_data.pop("workspaceIgnoredPaths", None)

    return input_data


def _effective_workspace_scope(
    *,
    default_base: Path,
    workspace_root: Optional[str],
    workspace_access_mode: Optional[str],
    workspace_allowed_paths: Optional[str],
    workspace_ignored_paths: Optional[str],
) -> tuple[Path, Dict[str, Path], tuple[Path, ...], str]:
    """Compute the effective (base, mounts, blocked_paths, access_mode) for file endpoints."""
    base = default_base
    raw_wr = str(workspace_root or "").strip()
    if raw_wr:
        base = _resolve_user_path(raw_wr, base=default_base)

    access_mode = _normalize_workspace_access_mode(workspace_access_mode)
    allowed_raw = _parse_lines_or_json_list(workspace_allowed_paths)
    ignored_raw = _parse_lines_or_json_list(workspace_ignored_paths)

    blocked: list[Path] = []
    for item in ignored_raw:
        try:
            blocked.append(_resolve_user_path(item, base=base))
        except Exception:
            continue

    mounts: Dict[str, Path] = {}
    used_names: set[str] = set()

    # In scoped mode, do not automatically include operator mounts; the client can add extra
    # roots via workspace_allowed_paths (workspace_or_allowed) or absolute paths (all_except_ignored).
    if access_mode == "workspace_or_allowed" and allowed_raw:
        allowed_abs: list[Path] = []
        for item in allowed_raw:
            try:
                p = _resolve_user_path(item, base=base)
            except Exception:
                continue
            try:
                if not p.exists() or not p.is_dir():
                    continue
            except Exception:
                continue
            # Only mount roots outside the base; inside-base directories are already searchable.
            try:
                if p.resolve().is_relative_to(base.resolve()):  # type: ignore[attr-defined]
                    continue
            except Exception:
                try:
                    p.resolve().relative_to(base.resolve())
                    continue
                except Exception:
                    pass
            allowed_abs.append(p)
        mounts = _mounts_from_allowed_paths(allowed_dirs=allowed_abs, used_names=used_names)

    return base, mounts, tuple(blocked), access_mode


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


def _ensure_session_memory_owner_run_exists(*, run_store: Any, session_id: str) -> str:
    """Ensure the internal session memory owner run exists (best-effort, durable)."""
    sid = str(session_id or "").strip()
    rid = _session_memory_run_id(sid)
    try:
        existing = run_store.load(str(rid))
    except Exception:
        existing = None
    if existing is not None:
        return str(rid)

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
    run_store.save(run)
    return str(rid)


def _load_or_create_session_memory_owner_run(*, run_store: Any, run_id: str) -> Optional[RunState]:
    """Load a run; for `session_memory_*` ids, create a placeholder owner run when missing."""
    rid = str(run_id or "").strip()
    if not rid:
        return None

    try:
        existing = run_store.load(rid)
    except Exception:
        existing = None
    if existing is not None:
        return existing

    if not rid.startswith(_DEFAULT_SESSION_MEMORY_RUN_PREFIX):
        return None
    if not _SAFE_RUN_ID_PATTERN.match(rid):
        return None

    suffix = rid[len(_DEFAULT_SESSION_MEMORY_RUN_PREFIX) :]
    session_id: Optional[str] = None
    if suffix and not suffix.startswith("sha_"):
        session_id = suffix

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    run = RunState(
        run_id=rid,
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
        session_id=session_id,
        parent_run_id=None,
    )
    try:
        run_store.save(run)
    except Exception:
        return None
    return run


_FILE_INDEX_CACHE: dict[str, Any] = {"key": "", "built_at": 0.0, "paths": []}
_FILE_INDEX_BUILD_LOCK = threading.Lock()
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


def _build_file_index_for_root(
    *, root: Path, mount: Optional[str], max_files: int, blocked: tuple[Path, ...] = ()
) -> list[str]:
    """Build a file index for a single root, yielding virtual paths (best-effort).

    Performance note:
    This function runs on every cold cache rebuild for `/files/search`. Avoid per-path
    `.resolve()` calls inside the hot loop; `os.walk(root)` already yields absolute paths
    rooted under `root` (which itself is resolved by callers).
    """
    try:
        from abstractcore.tools.abstractignore import AbstractIgnore

        ignore = AbstractIgnore.for_path(root)
    except Exception:
        ignore = None

    try:
        root_abs = root.resolve()
    except Exception:
        root_abs = root

    blocked_paths: tuple[Path, ...] = ()
    if blocked:
        resolved_blocked: list[Path] = []
        for b in blocked:
            if not isinstance(b, Path):
                continue
            try:
                resolved_blocked.append(b.resolve())
            except Exception:
                resolved_blocked.append(b)
        blocked_paths = tuple(resolved_blocked)

    def _is_under_fast(child: Path, parent: Path) -> bool:
        try:
            child.relative_to(parent)
            return True
        except Exception:
            return False
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root_abs):
        cur = Path(dirpath)
        if blocked_paths:
            if any(cur == b or _is_under_fast(cur, b) for b in blocked_paths):
                dirnames[:] = []
                continue

        kept: list[str] = []
        for d in dirnames:
            p = cur / d
            if blocked_paths:
                if any(p == b or _is_under_fast(p, b) for b in blocked_paths):
                    continue
            try:
                if ignore is not None and ignore.is_ignored(p, is_dir=True):
                    continue
            except Exception:
                pass
            kept.append(d)
        dirnames[:] = kept

        for fn in filenames:
            p = cur / fn
            if blocked_paths:
                if any(p == b or _is_under_fast(p, b) for b in blocked_paths):
                    continue
            try:
                if ignore is not None and ignore.is_ignored(p, is_dir=False):
                    continue
            except Exception:
                pass
            try:
                rel = p.relative_to(root_abs).as_posix()
            except Exception:
                continue
            if not rel:
                continue
            if mount:
                out.append(f"{mount}/{rel}")
            else:
                out.append(rel)
            if len(out) >= int(max_files):
                return out
    return out


def _get_file_index(
    *,
    base: Path,
    mounts: Dict[str, Path],
    blocked: tuple[Path, ...] = (),
    ttl_s: float = 30.0,
    max_files: int = 50000,
) -> list[str]:
    global _FILE_INDEX_CACHE
    now = time.time()

    # Allow coarse tuning for large workspaces (typing latency in @file search).
    try:
        ttl_env = str(os.getenv("ABSTRACTGATEWAY_FILE_INDEX_TTL_S", "") or "").strip()
        if ttl_env:
            ttl_s = max(1.0, float(ttl_env))
    except Exception:
        pass
    try:
        max_env = str(os.getenv("ABSTRACTGATEWAY_FILE_INDEX_MAX_FILES", "") or "").strip()
        if max_env:
            max_files = max(1000, int(max_env))
    except Exception:
        pass

    mounts_key = "\n".join([f"{k}={mounts[k]}" for k in sorted(mounts.keys())])
    blocked_key = "\n".join(sorted([str(p) for p in (blocked or ())]))
    key = f"{str(base)}\n{mounts_key}\nblocked={blocked_key}"
    cached_key = str(_FILE_INDEX_CACHE.get("key") or "")
    built_at = float(_FILE_INDEX_CACHE.get("built_at") or 0.0)
    cached_paths = _FILE_INDEX_CACHE.get("paths") if isinstance(_FILE_INDEX_CACHE.get("paths"), list) else []
    if cached_key == key and cached_paths and (now - built_at) < ttl_s:
        return list(cached_paths)

    # Coalesce concurrent cold-cache rebuilds (typing can trigger multiple overlapping calls).
    with _FILE_INDEX_BUILD_LOCK:
        cached_key = str(_FILE_INDEX_CACHE.get("key") or "")
        built_at = float(_FILE_INDEX_CACHE.get("built_at") or 0.0)
        cached_paths = _FILE_INDEX_CACHE.get("paths") if isinstance(_FILE_INDEX_CACHE.get("paths"), list) else []
        if cached_key == key and cached_paths and (now - built_at) < ttl_s:
            return list(cached_paths)

        out: list[str] = []
        remaining = int(max_files)

        # Primary root (no prefix for backward compatibility).
        try:
            base_paths = _build_file_index_for_root(root=base, mount=None, max_files=remaining, blocked=tuple(blocked or ()))
        except Exception:
            base_paths = []
        out.extend(base_paths)
        remaining -= len(base_paths)

        # Mounts (prefixed as `<mount>/<relpath>`).
        for name in sorted(mounts.keys()):
            if remaining <= 0:
                break
            root = mounts.get(name)
            if not isinstance(root, Path):
                continue
            try:
                items = _build_file_index_for_root(root=root, mount=name, max_files=remaining, blocked=tuple(blocked or ()))
            except Exception:
                items = []
            out.extend(items)
            remaining -= len(items)

        _FILE_INDEX_CACHE = {"key": key, "built_at": now, "paths": out}
        return out


def _resolve_workspace_path(*, base: Path, mounts: Dict[str, Path], raw_path: str) -> tuple[Path, str, Optional[str], Path]:
    """Resolve a user-supplied path against the primary workspace root + mounts.

    Accepts:
    - virtual paths (preferred): `docs/readme.md` or `mount/path/to/file.md`
    - absolute paths: allowed only if under base or a mount root

    Returns:
        (resolved_path, virtual_path_normalized, mount_name_or_none, root_used)
    """
    p_raw0 = str(raw_path or "").strip()
    # Tolerate "@path" handles (used by attachments and some clients).
    if p_raw0.startswith("@"):
        p_raw0 = p_raw0[1:].lstrip()
    if not p_raw0:
        raise HTTPException(status_code=400, detail="path is required")

    p_raw = p_raw0.replace("\\", "/")
    p = Path(p_raw).expanduser()

    if p.is_absolute():
        try:
            resolved = p.resolve()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid absolute path")

        # Prefer the most specific root (longest path) that contains the resolved path.
        candidates: list[tuple[int, Optional[str], Path]] = []
        try:
            resolved.relative_to(base)
            candidates.append((len(str(base)), None, base))
        except Exception:
            pass
        for name, root in (mounts or {}).items():
            if not isinstance(root, Path):
                continue
            try:
                resolved.relative_to(root)
                candidates.append((len(str(root)), str(name), root))
            except Exception:
                continue
        if not candidates:
            raise HTTPException(status_code=403, detail="path is outside workspace roots")
        candidates.sort(key=lambda x: x[0], reverse=True)
        _len, mount, root = candidates[0]
        try:
            rel = resolved.relative_to(root).as_posix()
        except Exception:
            rel = ""
        if mount:
            virt = f"{mount}/{rel}" if rel else str(mount)
        else:
            virt = rel
        return resolved, virt, mount, root

    # Virtual path (relative): interpret first segment as a mount name when prefixed like `mount/...`.
    virt_raw = p_raw.strip()
    while virt_raw.startswith("./"):
        virt_raw = virt_raw[2:]

    parts = [seg for seg in virt_raw.split("/") if seg not in ("", ".")]
    mount: Optional[str] = None
    root = base
    rel_part = virt_raw

    # Require `mount/...` (at least one `/`) to avoid collisions with root-level filenames.
    if len(parts) >= 2:
        candidate = parts[0]
        if candidate in (mounts or {}):
            mount = candidate
            root = mounts[candidate]
            rel_part = "/".join(parts[1:])

    try:
        resolved = (root / Path(rel_part)).resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid path")
    try:
        resolved.relative_to(root)
    except Exception:
        raise HTTPException(status_code=403, detail="path escapes workspace root")

    try:
        rel_norm = resolved.relative_to(root).as_posix()
    except Exception:
        rel_norm = Path(rel_part).as_posix()
    if mount:
        virt_norm = f"{mount}/{rel_norm}" if rel_norm else str(mount)
    else:
        virt_norm = rel_norm

    return resolved, virt_norm, mount, root


def _clamp_text(text: str, *, max_len: int) -> str:
    s = str(text or "")
    if max_len <= 0:
        #[WARNING:TRUNCATION] explicit hard clamp (callers must treat as lossy)
        logger.warning("clamp_text invoked with max_len<=0; dropping content")
        return ""
    if len(s) <= max_len:
        return s
    #[WARNING:TRUNCATION] bounded clamp for LLM-visible text
    marker = "… (truncated)"
    keep = max(0, int(max_len) - len(marker))
    if keep <= 0:
        # If the budget is too small to carry content, still carry the explicit marker.
        return marker[: max(0, int(max_len))].rstrip()
    return s[:keep].rstrip() + marker


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
        params={"temperature": 0.2},
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
        params={"temperature": 0.2},
    )
    text = str(res.get("content") or "").strip()
    return text


@router.get("/bundles")
async def list_bundles(
    all_versions: bool = Query(default=False, description="If true, return one item per bundle version."),
    include_deprecated: bool = Query(default=False, description="If true, include deprecated entrypoints in discovery."),
) -> Dict[str, Any]:
    svc = get_gateway_service()
    host = _require_bundle_host(svc)
    dep_store = getattr(host, "deprecation_store", None)

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
                fid = str(getattr(ep, "flow_id", "") or "").strip()
                if not fid:
                    continue
                rec = dep_store.get_record(bundle_id=str(bid), flow_id=fid) if dep_store is not None else None
                deprecated = rec is not None
                if deprecated and not include_deprecated:
                    continue
                eps.append(
                    {
                        "flow_id": fid,
                        "name": getattr(ep, "name", None),
                        "description": getattr(ep, "description", "") or "",
                        "interfaces": list(getattr(ep, "interfaces", None) or []),
                        "deprecated": bool(deprecated),
                        "deprecated_at": (str(rec.get("deprecated_at") or "").strip() if isinstance(rec, dict) else "") or None,
                        "deprecated_reason": (str(rec.get("reason") or "").strip() if isinstance(rec, dict) else "") or None,
                    }
                )
            if not eps:
                continue
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
    default_bundle_id = getattr(host, "_default_bundle_id", None)
    if not include_deprecated and isinstance(default_bundle_id, str) and default_bundle_id.strip():
        visible = {str(it.get("bundle_id") or "").strip() for it in items}
        if default_bundle_id.strip() not in visible:
            default_bundle_id = None
    return {"items": items, "default_bundle_id": default_bundle_id}


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


@router.post("/bundles/upload")
async def upload_bundle(
    file: UploadFile = File(..., description="WorkflowBundle (.flow) to install."),
    overwrite: bool = Form(False, description="If true, overwrite an existing bundle_id@version."),
    reload: bool = Form(True, description="If true, reload bundles after install (best-effort; dev-friendly)."),
) -> Dict[str, Any]:
    """Upload and install a WorkflowBundle into the gateway's bundles_dir.

    This is intended for thin clients where the UI cannot write into the server's filesystem.
    """
    svc = get_gateway_service()
    host = _require_bundle_host(svc)

    try:
        max_bytes_raw = str(os.getenv("ABSTRACTGATEWAY_MAX_BUNDLE_BYTES", "") or "").strip()
        max_bytes = int(max_bytes_raw) if max_bytes_raw else 75 * 1024 * 1024
        if max_bytes <= 0:
            max_bytes = 75 * 1024 * 1024
    except Exception:
        max_bytes = 75 * 1024 * 1024

    try:
        content = await file.read()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to read upload: {e}")

    size = len(content or b"")
    if size > max_bytes:
        raise HTTPException(status_code=413, detail=f"Bundle too large ({size} bytes > {max_bytes} bytes)")

    try:
        from abstractruntime.workflow_bundle import WorkflowBundleRegistry, WorkflowBundleRegistryError

        reg = WorkflowBundleRegistry(getattr(host, "bundles_dir", None))
        installed = reg.install_bytes(
            bytes(content or b""),
            filename_hint=str(getattr(file, "filename", "") or "upload.flow"),
            overwrite=bool(overwrite),
        )
    except WorkflowBundleRegistryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed installing bundle: {e}")

    gateway_reloaded = False
    gateway_reload_error: Optional[str] = None
    if bool(reload):
        reload_fn = getattr(host, "reload_bundles_from_disk", None)
        if not callable(reload_fn):
            gateway_reload_error = "Bundle reload is not supported on this gateway host"
        else:
            try:
                reload_fn()
                gateway_reloaded = True
            except Exception as e:
                gateway_reload_error = str(e)

    man = installed.manifest
    eps: list[Dict[str, Any]] = []
    for ep in list(getattr(man, "entrypoints", None) or []):
        eps.append(
            {
                "flow_id": getattr(ep, "flow_id", None),
                "name": getattr(ep, "name", None),
                "description": str(getattr(ep, "description", "") or ""),
                "interfaces": list(getattr(ep, "interfaces", None) or []),
            }
        )

    return {
        "ok": True,
        "bundle_id": str(installed.bundle_id),
        "bundle_version": str(installed.bundle_version),
        "bundle_ref": str(installed.bundle_ref),
        "sha256": str(installed.sha256 or ""),
        "default_entrypoint": str(getattr(man, "default_entrypoint", "") or "") or None,
        "entrypoints": eps,
        "gateway_reloaded": bool(gateway_reloaded),
        "gateway_reload_error": gateway_reload_error,
    }


@router.delete("/bundles/{bundle_id}")
async def remove_bundle(
    bundle_id: str,
    bundle_version: Optional[str] = Query(default=None, description="Optional bundle version (defaults to removing all versions)."),
    reload: bool = Query(default=True, description="If true, reload bundles after removal (best-effort; dev-friendly)."),
) -> Dict[str, Any]:
    svc = get_gateway_service()
    host = _require_bundle_host(svc)

    bid_raw = str(bundle_id or "").strip()
    if not bid_raw:
        raise HTTPException(status_code=400, detail="bundle_id is required")
    bid_base, bid_ver = _split_bundle_ref(bid_raw)
    if bid_ver and bundle_version and bid_ver != bundle_version:
        raise HTTPException(status_code=400, detail="bundle_id version does not match bundle_version")

    target_ver = str(bundle_version or "").strip() if isinstance(bundle_version, str) and str(bundle_version).strip() else bid_ver
    bundle_ref = f"{bid_base}@{target_ver}" if target_ver else bid_base

    try:
        from abstractruntime.workflow_bundle import WorkflowBundleRegistry, WorkflowBundleRegistryError

        reg = WorkflowBundleRegistry(getattr(host, "bundles_dir", None))
        removed = int(reg.remove(bundle_ref))
    except WorkflowBundleRegistryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed removing bundle: {e}")

    if removed <= 0:
        raise HTTPException(status_code=404, detail=f"Bundle '{bundle_ref}' not found")

    gateway_reloaded = False
    gateway_reload_error: Optional[str] = None
    if bool(reload):
        reload_fn = getattr(host, "reload_bundles_from_disk", None)
        if not callable(reload_fn):
            gateway_reload_error = "Bundle reload is not supported on this gateway host"
        else:
            try:
                reload_fn()
                gateway_reloaded = True
            except Exception as e:
                gateway_reload_error = str(e)

    return {
        "ok": True,
        "bundle_ref": str(bundle_ref),
        "removed": int(removed),
        "gateway_reloaded": bool(gateway_reloaded),
        "gateway_reload_error": gateway_reload_error,
    }


@router.post("/bundles/{bundle_id}/deprecate")
async def deprecate_bundle(bundle_id: str, req: DeprecateWorkflowRequest) -> Dict[str, Any]:
    """Mark a bundle entrypoint (or entire bundle) as deprecated.

    Deprecated workflows are excluded from discovery by default and cannot be launched.
    """
    svc = get_gateway_service()
    host = _require_bundle_host(svc)

    bid = str(bundle_id or "").strip()
    if not bid:
        raise HTTPException(status_code=400, detail="bundle_id is required")

    versions = getattr(host, "bundles", {}).get(bid)
    if not isinstance(versions, dict) or not versions:
        raise HTTPException(status_code=404, detail=f"Bundle '{bid}' not found")

    fid = str(getattr(req, "flow_id", "") or "").strip()
    if fid:
        latest0 = getattr(host, "latest_bundle_versions", None)
        latest = latest0 if isinstance(latest0, dict) else {}
        ver = str(latest.get(bid) or "").strip() or sorted([str(x) for x in versions.keys() if isinstance(x, str)])[-1]
        bundle = versions.get(ver)
        entrypoints = list(getattr(getattr(bundle, "manifest", None), "entrypoints", None) or []) if bundle is not None else []
        if fid not in {str(getattr(ep, "flow_id", "") or "").strip() for ep in entrypoints}:
            raise HTTPException(status_code=404, detail=f"Entrypoint '{bid}:{fid}' not found")

    dep = getattr(host, "deprecation_store", None)
    if dep is None:
        raise HTTPException(status_code=400, detail="Deprecations are not supported on this gateway host")
    rec = dep.set_deprecated(bundle_id=bid, flow_id=fid or None, reason=str(getattr(req, "reason", "") or "").strip() or None)
    return {"ok": True, **(rec or {})}


@router.post("/bundles/{bundle_id}/undeprecate")
async def undeprecate_bundle(bundle_id: str, req: DeprecateWorkflowRequest) -> Dict[str, Any]:
    svc = get_gateway_service()
    host = _require_bundle_host(svc)

    bid = str(bundle_id or "").strip()
    if not bid:
        raise HTTPException(status_code=400, detail="bundle_id is required")

    dep = getattr(host, "deprecation_store", None)
    if dep is None:
        raise HTTPException(status_code=400, detail="Deprecations are not supported on this gateway host")
    fid = str(getattr(req, "flow_id", "") or "").strip()
    removed = bool(dep.clear_deprecated(bundle_id=bid, flow_id=fid or None))
    return {"ok": True, "bundle_id": bid, "flow_id": fid or "*", "removed": removed}


@router.get("/bundles/{bundle_id}")
async def get_bundle(bundle_id: str, bundle_version: Optional[str] = Query(default=None, description="Optional bundle version (defaults to latest).")) -> Dict[str, Any]:
    svc = get_gateway_service()
    host = _require_bundle_host(svc)
    dep_store = getattr(host, "deprecation_store", None)

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
        dep_rec = dep_store.get_record(bundle_id=bid_base, flow_id=fid) if dep_store is not None and fid else None
        deprecated = dep_rec is not None
        entrypoints_out.append(
            {
                "flow_id": fid,
                "workflow_id": f"{bid_base}@{selected_ver}:{fid}" if fid else None,
                "name": getattr(ep, "name", None),
                "description": str(getattr(ep, "description", "") or ""),
                "interfaces": list(getattr(ep, "interfaces", None) or []),
                "inputs": _extract_entrypoint_inputs_from_visualflow(raw),
                "node_index": _extract_node_index_from_visualflow(raw),
                "deprecated": bool(deprecated),
                "deprecated_at": (str(dep_rec.get("deprecated_at") or "").strip() if isinstance(dep_rec, dict) else "") or None,
                "deprecated_reason": (str(dep_rec.get("reason") or "").strip() if isinstance(dep_rec, dict) else "") or None,
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
        input_data = _sanitize_run_workspace_policy(input_data)
        # Default workspace_root behavior (cross-client):
        # - If omitted, create a per-run workspace under the gateway data_dir.
        # - Clients may override only within the operator-configured server workspace roots.
        raw_ws = input_data.get("workspace_root")
        if not (isinstance(raw_ws, str) and raw_ws.strip()):
            base = Path(svc.config.data_dir) / "workspaces"
            base.mkdir(parents=True, exist_ok=True)
            ws_dir = base / uuid.uuid4().hex
            ws_dir.mkdir(parents=True, exist_ok=True)
            input_data["workspace_root"] = str(ws_dir)

        # Ensure the session attachment store exists early so clients can list/preview
        # session-scoped artifacts even before any attachments are ingested.
        if session_id:
            try:
                _ensure_session_memory_owner_run_exists(run_store=svc.host.run_store, session_id=session_id)
            except Exception as e:
                logger.warning("Failed to ensure session memory owner run exists", extra={"error": str(e)})

        run_id = svc.host.start_run(
            flow_id=flow_id,
            bundle_id=bundle_id,
            bundle_version=bundle_version,
            input_data=input_data,
            actor_id="gateway",
            session_id=session_id,
        )
    except WorkflowDeprecatedError as e:
        raise HTTPException(status_code=409, detail=str(e))
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

    # Prevent scheduling deprecated target workflows (host-side enforcement also blocks child launches).
    try:
        dep_store = getattr(host, "deprecation_store", None)
        if dep_store is not None and dep_store.is_deprecated(bundle_id=bundle_id_base, flow_id=target_flow_id):
            rec = dep_store.get_record(bundle_id=bundle_id_base, flow_id=target_flow_id) or {}
            reason = str(rec.get("reason") or "").strip()
            msg = f"Workflow '{bundle_id_base}:{target_flow_id}' is deprecated"
            if reason:
                msg = f"{msg}: {reason}"
            raise HTTPException(status_code=409, detail=msg)
    except HTTPException:
        raise
    except Exception:
        # Best-effort: deprecation is a gateway-owned policy; do not fail scheduling if the store is unreadable.
        pass

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
        if session_id:
            try:
                _ensure_session_memory_owner_run_exists(run_store=svc.host.run_store, session_id=session_id)
            except Exception as e:
                logger.warning("Failed to ensure session memory owner run exists (schedule)", extra={"error": str(e)})
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
    include_ledger_len: bool = Query(True, description="If true, include ledger_len (may be slow for file-backed ledgers)."),
    include_metrics: bool = Query(False, description="If true, include best-effort llm/tool counts (aggregated across child runs)."),
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

    ledger_store = getattr(getattr(svc, "host", None), "ledger_store", None)

    def _summary_from_index_row(row: Dict[str, Any]) -> Dict[str, Any]:
        status0 = str(row.get("status") or "").strip()
        out: Dict[str, Any] = {
            "run_id": row.get("run_id"),
            "workflow_id": row.get("workflow_id"),
            "status": status0,
            "current_node": None,
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "actor_id": row.get("actor_id"),
            "session_id": row.get("session_id"),
            "parent_run_id": row.get("parent_run_id"),
            "error": None,
            "paused": False,
            "pause_reason": None,
            "paused_at": None,
            "resumed_at": None,
            "waiting": None,
            "is_scheduled": False,
            "schedule": None,
            "limits": None,
        }
        if status0 == "waiting":
            reason = str(row.get("wait_reason") or "").strip()
            until = str(row.get("wait_until") or "").strip()
            if reason:
                out["waiting"] = {"reason": reason, **({"until": until} if until else {})}
        return out

    items: list[Dict[str, Any]] = []
    used_index = False
    runs_all_for_metrics: Optional[List[Any]] = None
    if isinstance(rs, QueryableRunIndexStore):
        try:
            # Overfetch a bit to account for filtering internal runs or malformed rows.
            internal_limit = max(200, int(limit) * 5) if (bool(root_only) or sid or filter_internal) else int(limit)
            rows = rs.list_run_index(status=status_enum, workflow_id=wid, session_id=sid, root_only=bool(root_only), limit=internal_limit)
            for row in rows or []:
                wf_id = str(row.get("workflow_id") or "").strip()
                if filter_internal and wf_id.startswith("__"):
                    continue
                if bool(root_only) and str(row.get("parent_run_id") or "").strip():
                    continue
                items.append(_summary_from_index_row(row))
                if len(items) >= int(limit):
                    break
            used_index = True
        except Exception:
            items = []
            used_index = False

    if not used_index:
        try:
            internal_limit = max(200, int(limit) * 5) if (sid or bool(root_only)) else int(limit)
            runs = rs.list_runs(status=status_enum, workflow_id=wid, limit=internal_limit)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to list runs: {e}")

        runs_all = list(runs or [])
        runs_all_for_metrics = runs_all
        if sid:
            runs_all = [r for r in runs_all if str(getattr(r, "session_id", "") or "").strip() == sid]

        def _parent_id(run_obj: Any) -> str:
            return str(getattr(run_obj, "parent_run_id", "") or "").strip()

        runs_root = [r for r in runs_all if not _parent_id(r)] if bool(root_only) else runs_all

        for r in runs_root:
            if filter_internal:
                wf_id = getattr(r, "workflow_id", None)
                if isinstance(wf_id, str) and wf_id.startswith("__"):
                    continue
            items.append(run_summary(r))
            if len(items) >= int(limit):
                break

    if bool(include_metrics):
        run_ids = [str(it.get("run_id") or "").strip() for it in items if str(it.get("run_id") or "").strip()]
        metrics_many = getattr(ledger_store, "metrics_many", None) if ledger_store is not None else None
        metrics: Dict[str, Any] = {}
        if callable(metrics_many):
            try:
                raw = metrics_many(run_ids)
                metrics = raw if isinstance(raw, dict) else {}
            except Exception:
                metrics = {}
        if not metrics and runs_all_for_metrics is not None:
            # Fallback: derive per-run metrics from runtime-owned traces when a ledger metrics API isn't available.
            def _rid(run_obj: Any) -> str:
                return str(getattr(run_obj, "run_id", "") or "").strip()

            def _parent_id(run_obj: Any) -> str:
                return str(getattr(run_obj, "parent_run_id", "") or "").strip()

            def _extract_run_trace_metrics(run_obj: Any) -> tuple[int, int, int]:
                steps_done = 0
                llm_calls = 0
                tool_calls = 0
                try:
                    vars_obj = getattr(run_obj, "vars", None)
                    runtime_ns = vars_obj.get("_runtime") if isinstance(vars_obj, dict) else None
                    traces = runtime_ns.get("node_traces") if isinstance(runtime_ns, dict) else None
                    if not isinstance(traces, dict):
                        return (0, 0, 0)
                    for node_trace in traces.values():
                        steps = node_trace.get("steps") if isinstance(node_trace, dict) else None
                        if not isinstance(steps, list):
                            continue
                        for s in steps:
                            if not isinstance(s, dict):
                                continue
                            st = str(s.get("status") or "").strip()
                            if st != "completed":
                                continue
                            steps_done += 1
                            eff = s.get("effect") if isinstance(s.get("effect"), dict) else None
                            eff_type = str((eff or {}).get("type") or "").strip()
                            if eff_type == "llm_call":
                                llm_calls += 1
                                continue
                            if eff_type == "tool_calls":
                                payload = eff.get("payload") if isinstance(eff, dict) and isinstance(eff.get("payload"), dict) else None
                                calls = payload.get("tool_calls") if isinstance(payload, dict) else None
                                if isinstance(calls, list):
                                    tool_calls += len([c for c in calls if c is not None])
                except Exception:
                    return (0, 0, 0)
                return (int(steps_done), int(llm_calls), int(tool_calls))

            metrics_self: Dict[str, tuple[int, int, int]] = {}
            children_by_parent: Dict[str, list[str]] = {}
            for r in runs_all_for_metrics:
                rid0 = _rid(r)
                if not rid0:
                    continue
                metrics_self[rid0] = _extract_run_trace_metrics(r)
                pid = _parent_id(r)
                if pid:
                    children_by_parent.setdefault(pid, []).append(rid0)

            def _aggregate(root_id: str) -> tuple[int, int, int]:
                if not root_id:
                    return (0, 0, 0)
                steps = 0
                llm = 0
                tools = 0
                from collections import deque

                queue = deque([root_id])
                seen: set[str] = set()
                while queue and len(seen) < 5000:
                    rid0 = str(queue.popleft() or "").strip()
                    if not rid0 or rid0 in seen:
                        continue
                    seen.add(rid0)
                    s, l, t = metrics_self.get(rid0, (0, 0, 0))
                    steps += int(s)
                    llm += int(l)
                    tools += int(t)
                    for cid in children_by_parent.get(rid0, []):
                        if cid not in seen:
                            queue.append(cid)
                return (int(steps), int(llm), int(tools))

            for item in items:
                rid = str(item.get("run_id") or "").strip()
                if not rid:
                    continue
                s, l, t = _aggregate(rid)
                metrics[rid] = {"steps": s, "llm_calls": l, "tool_calls": t}
        for item in items:
            rid = str(item.get("run_id") or "").strip()
            m = metrics.get(rid) if rid else None
            if isinstance(m, dict):
                item["steps"] = m.get("steps")
                item["llm_calls"] = m.get("llm_calls")
                item["tool_calls"] = m.get("tool_calls")
                item["tokens_total"] = m.get("tokens_total")
            else:
                item.setdefault("steps", None)
                item.setdefault("llm_calls", None)
                item.setdefault("tool_calls", None)
                item.setdefault("tokens_total", None)

    if bool(include_ledger_len) and ledger_store is not None:
        run_ids = [str(it.get("run_id") or "").strip() for it in items if str(it.get("run_id") or "").strip()]
        counts: Dict[str, Any] = {}
        try:
            count_many = getattr(ledger_store, "count_many", None)
            if callable(count_many):
                raw = count_many(run_ids)
                counts = raw if isinstance(raw, dict) else {}
        except Exception:
            counts = {}
        for item in items:
            rid = str(item.get("run_id") or "").strip()
            if not rid:
                continue
            if rid in counts:
                try:
                    item["ledger_len"] = int(counts.get(rid) or 0)
                except Exception:
                    item["ledger_len"] = None
                continue
            try:
                count_fn = getattr(ledger_store, "count", None)
                if callable(count_fn):
                    item["ledger_len"] = int(count_fn(rid))
                else:
                    records = ledger_store.list(rid)
                    item["ledger_len"] = int(len(records) if isinstance(records, list) else 0)
            except Exception:
                item["ledger_len"] = None

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


@router.get("/runs/{run_id}/history_bundle")
async def get_run_history_bundle(
    run_id: str,
    include_subruns: bool = Query(True, description="Include descendant runs (subworkflows) in the bundle."),
    include_session: bool = Query(False, description="Include a best-effort session turn list (root runs)."),
    session_turn_limit: int = Query(200, ge=1, le=500, description="Max session turns to include when include_session=true."),
    ledger_mode: str = Query("tail", description="Ledger export mode: tail|full."),
    ledger_max_items: int = Query(2000, ge=0, le=20000, description="Max ledger items per run when ledger_mode=tail (0 disables tailing)."),
) -> Dict[str, Any]:
    """Return a versioned RunHistoryBundle (runtime-owned contract).

    Intended for thin clients to render/replay without stitching multiple endpoints.
    """
    svc = get_gateway_service()
    rid = str(run_id or "").strip()
    if not rid:
        raise HTTPException(status_code=400, detail="run_id is required")

    try:
        from abstractruntime import export_run_history_bundle
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail=f"RunHistoryBundle export is unavailable: {e}")

    mode = str(ledger_mode or "tail").strip().lower()
    if mode not in {"tail", "full"}:
        raise HTTPException(status_code=400, detail="ledger_mode must be 'tail' or 'full'")
    max_items = int(ledger_max_items)
    if mode == "tail" and max_items <= 0:
        mode = "full"
        max_items = 0

    try:
        store = getattr(getattr(svc, "stores", None), "artifact_store", None)
        bundle = export_run_history_bundle(
            run_id=rid,
            run_store=svc.host.run_store,
            ledger_store=svc.host.ledger_store,
            artifact_store=store,
            include_subruns=bool(include_subruns),
            include_session=bool(include_session),
            session_turn_limit=int(session_turn_limit),
            ledger_mode=mode,
            ledger_max_items=max_items,
        )
        if not isinstance(bundle, dict):
            raise RuntimeError("export_run_history_bundle returned non-dict")
        return bundle
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to export history bundle: {e}")


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
        # Internal session-memory runs are created lazily; for UX, create a placeholder
        # when the client asks for session artifacts before any attachments exist.
        run = _load_or_create_session_memory_owner_run(run_store=rs, run_id=str(run_id))
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
        run = _load_or_create_session_memory_owner_run(run_store=rs, run_id=str(run_id))
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
        run = _load_or_create_session_memory_owner_run(run_store=rs, run_id=str(run_id))
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
    rs = svc.host.run_store

    # Fail fast: streaming a non-existent run should not hold open a keep-alive connection forever.
    try:
        run0 = rs.load(run_id2)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load run: {e}")
    if run0 is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id2}' not found")

    def _is_terminal(status_any: Any) -> bool:
        s = getattr(status_any, "value", None) or str(status_any or "")
        s = str(s).strip().lower()
        return s in {"completed", "failed", "cancelled"}

    async def _gen():
        cursor = int(after or 0)
        last_emit = asyncio.get_event_loop().time()
        last_status_check = last_emit
        terminal = _is_terminal(getattr(run0, "status", None))
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

                # When the run is terminal and we've streamed all known records, close the stream
                # so clients can finalize their UI state (don't hang forever on keep-alives).
                if terminal:
                    payload = json.dumps({"run_id": run_id2, "cursor": cursor, "status": "terminal"}, ensure_ascii=False)
                    yield b"event: done\n"
                    yield f"data: {payload}\n\n".encode("utf-8")
                    break

                # Poll run status while idle (best-effort, bounded).
                if (now - last_status_check) >= 0.75:
                    last_status_check = now
                    try:
                        cur_run = rs.load(run_id2)
                        terminal = _is_terminal(getattr(cur_run, "status", None))
                    except Exception:
                        # If status lookup fails, keep streaming keep-alives.
                        terminal = False

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


class VoiceTTSRequest(BaseModel):
    text: str = Field(..., description="Text to synthesize.")
    voice: Optional[str] = Field(default=None, description="Optional voice selector (backend-specific).")
    format: str = Field(default="wav", description="Audio container/codec (wav|mp3, backend-dependent).")
    request_id: Optional[str] = Field(default=None, description="Optional idempotency key (UUID recommended).")


class VoiceTTSResponse(BaseModel):
    ok: bool = Field(default=True)
    run_id: str
    request_id: str
    audio_artifact: Dict[str, Any]


@router.post("/runs/{run_id}/voice/tts", response_model=VoiceTTSResponse)
async def voice_tts(run_id: str, req: VoiceTTSRequest) -> VoiceTTSResponse:
    """Deterministic TTS: store audio as artifact + emit a durable ledger event."""
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
        run = _load_or_create_session_memory_owner_run(run_store=rs, run_id=rid)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{rid}' not found")

    store = getattr(getattr(svc, "stores", None), "artifact_store", None)
    if store is None:
        raise HTTPException(status_code=500, detail="Artifact store is not available")

    request_id = str(getattr(req, "request_id", "") or "").strip() or str(uuid.uuid4())
    text = str(getattr(req, "text", "") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    voice = getattr(req, "voice", None)
    fmt = str(getattr(req, "format", "wav") or "wav").strip().lower()

    try:
        from abstractcore.capabilities.errors import CapabilityUnavailableError

        reg = _get_gateway_capability_registry()
        audio_ref = reg.voice.tts(
            text,
            voice=str(voice) if isinstance(voice, str) and voice.strip() else None,
            format=fmt,
            artifact_store=store,
            run_id=str(getattr(run, "run_id", rid)),
            tags={"request_id": request_id, "session_id": str(getattr(run, "session_id", "") or "")},
            metadata={"request_id": request_id, "text": text, "voice": voice, "format": fmt},
        )
    except CapabilityUnavailableError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ImportError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"TTS failed: {e}")

    if not isinstance(audio_ref, dict) or not isinstance(audio_ref.get("$artifact"), str) or not audio_ref.get("$artifact"):
        raise HTTPException(status_code=500, detail="TTS backend returned an invalid artifact ref")

    payload = {
        "run_id": str(getattr(run, "run_id", rid)),
        "request_id": request_id,
        "text": text,
        "voice": voice,
        "format": fmt,
        "audio_artifact": audio_ref,
    }

    try:
        eff = Effect(type=EffectType.EMIT_EVENT, payload={"name": "abstract.voice.tts", "scope": "run", "run_id": str(getattr(run, "run_id", rid)), "payload": payload})
        rec = StepRecord.start(run=run, node_id="gateway", effect=eff, idempotency_key=f"gateway:voice_tts:{request_id}")
        rec.finish_success({"emitted": True, "name": "abstract.voice.tts", "payload": payload})
        svc.host.ledger_store.append(rec)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to persist TTS event: {e}")

    return VoiceTTSResponse(ok=True, run_id=str(getattr(run, "run_id", rid)), request_id=request_id, audio_artifact=audio_ref)


class AudioTranscribeRequest(BaseModel):
    audio_artifact: Dict[str, Any] = Field(..., description="Audio artifact ref dict like {'$artifact': '...'} (from /attachments/*).")
    language: Optional[str] = Field(default=None, description="Optional language hint (backend-specific).")
    request_id: Optional[str] = Field(default=None, description="Optional idempotency key (UUID recommended).")


class AudioTranscribeResponse(BaseModel):
    ok: bool = Field(default=True)
    run_id: str
    request_id: str
    text: str
    transcript_artifact: Dict[str, Any]


@router.post("/runs/{run_id}/audio/transcribe", response_model=AudioTranscribeResponse)
async def audio_transcribe(run_id: str, req: AudioTranscribeRequest) -> AudioTranscribeResponse:
    """Deterministic STT: transcribe an uploaded audio artifact into text + artifact + ledger event."""
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
        run = _load_or_create_session_memory_owner_run(run_store=rs, run_id=rid)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run '{rid}' not found")

    store = getattr(getattr(svc, "stores", None), "artifact_store", None)
    if store is None:
        raise HTTPException(status_code=500, detail="Artifact store is not available")

    request_id = str(getattr(req, "request_id", "") or "").strip() or str(uuid.uuid4())
    audio_ref = getattr(req, "audio_artifact", None)
    if not (isinstance(audio_ref, dict) and isinstance(audio_ref.get("$artifact"), str) and audio_ref.get("$artifact")):
        raise HTTPException(status_code=400, detail="audio_artifact must be an artifact ref dict like {'$artifact': '...'}")

    audio_artifact_id = str(audio_ref.get("$artifact") or "").strip()
    try:
        meta_in = store.get_metadata(audio_artifact_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load audio artifact metadata: {e}")
    if meta_in is None:
        raise HTTPException(status_code=404, detail="audio_artifact not found")

    expected_run_id = str(getattr(run, "run_id", rid))
    if meta_in.run_id and str(meta_in.run_id) != expected_run_id:
        # Do not leak cross-run artifact existence to callers.
        raise HTTPException(status_code=404, detail="audio_artifact not found")

    content_type_in = str(getattr(meta_in, "content_type", "") or "")
    if content_type_in and not content_type_in.startswith("audio/") and content_type_in != "application/octet-stream":
        raise HTTPException(status_code=400, detail=f"audio_artifact must be audio/* (got '{content_type_in}')")

    language = getattr(req, "language", None)

    try:
        from abstractcore.capabilities.errors import CapabilityUnavailableError

        reg = _get_gateway_capability_registry()
        text = reg.audio.transcribe(audio_ref, language=str(language) if isinstance(language, str) and language.strip() else None, artifact_store=store)
        text = str(text or "")
    except CapabilityUnavailableError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ImportError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"STT failed: {e}")

    # Store transcript as an artifact (durable; avoids inlining large strings in ledger-only mode).
    store_fn = getattr(store, "store", None)
    if not callable(store_fn):
        raise HTTPException(status_code=500, detail="Artifact store does not support storing bytes")

    transcript_bytes = str(text).encode("utf-8")
    sha256 = hashlib.sha256(transcript_bytes).hexdigest()
    tags: Dict[str, str] = {
        "kind": "derived_text",
        "modality": "audio",
        "task": "stt",
        "request_id": request_id,
        "sha256": sha256,
        "session_id": str(getattr(run, "session_id", "") or ""),
        "source_audio_artifact": str(audio_ref.get("$artifact")),
    }
    try:
        meta = store_fn(
            transcript_bytes,
            content_type="text/plain; charset=utf-8",
            run_id=str(getattr(run, "run_id", rid)),
            tags=tags,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to store transcript artifact: {e}")

    artifact_id = str(getattr(meta, "artifact_id", "") or "") if meta is not None else ""
    if not artifact_id:
        artifact_id = str(meta.get("artifact_id", "") or "") if isinstance(meta, dict) else ""
    if not artifact_id:
        raise HTTPException(status_code=500, detail="Artifact store did not return an artifact_id for transcript")

    transcript_ref: Dict[str, Any] = {
        "$artifact": artifact_id,
        "content_type": "text/plain; charset=utf-8",
        "filename": "transcript.txt",
        "sha256": sha256,
        "size_bytes": len(transcript_bytes),
    }

    payload = {
        "run_id": str(getattr(run, "run_id", rid)),
        "request_id": request_id,
        "audio_artifact": audio_ref,
        "language": language,
        "text": text,
        "transcript_artifact": transcript_ref,
    }

    try:
        eff = Effect(type=EffectType.EMIT_EVENT, payload={"name": "abstract.audio.transcript", "scope": "run", "run_id": str(getattr(run, "run_id", rid)), "payload": payload})
        rec = StepRecord.start(run=run, node_id="gateway", effect=eff, idempotency_key=f"gateway:audio_transcribe:{request_id}")
        rec.finish_success({"emitted": True, "name": "abstract.audio.transcript", "payload": payload})
        svc.host.ledger_store.append(rec)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to persist transcript event: {e}")

    return AudioTranscribeResponse(ok=True, run_id=str(getattr(run, "run_id", rid)), request_id=request_id, text=text, transcript_artifact=transcript_ref)


@router.post("/kg/query", response_model=KGQueryResponse)
async def kg_query(req: KGQueryRequest) -> KGQueryResponse:
    """Query the persisted AbstractMemory KG store (ground truth), scoped to run/session/global."""
    svc = get_gateway_service()
    rs = svc.host.run_store

    scope = str(getattr(req, "scope", "") or "session").strip().lower() or "session"
    if scope not in {"run", "session", "global", "all"}:
        raise HTTPException(status_code=400, detail="scope must be one of run|session|global|all")

    rid = getattr(req, "run_id", None)
    rid = rid.strip() if isinstance(rid, str) and rid.strip() else None
    sid = getattr(req, "session_id", None)
    sid = sid.strip() if isinstance(sid, str) and sid.strip() else None
    owner_override = getattr(req, "owner_id", None)
    owner_override = owner_override.strip() if isinstance(owner_override, str) and owner_override.strip() else None
    all_owners = bool(getattr(req, "all_owners", False))

    if scope == "all" and owner_override:
        raise HTTPException(status_code=400, detail="owner_id is not supported when scope=all")
    if owner_override and all_owners:
        raise HTTPException(status_code=400, detail="all_owners cannot be combined with owner_id")

    store_path = Path(svc.stores.base_dir) / "abstractmemory" / "kg"
    if not store_path.exists():
        return KGQueryResponse(
            ok=True,
            scope=scope,
            owner_id=None,
            count=0,
            items=[],
            warnings=[f"KG store does not exist at {store_path} (no persisted assertions yet)."],
        )

    try:
        from abstractmemory import LanceDBTripleStore, TripleQuery  # type: ignore
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail=f"AbstractMemory is not available: {e}")

    try:
        from abstractruntime.integrations.abstractmemory.effect_handlers import resolve_scope_owner_id  # type: ignore
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail=f"AbstractRuntime AbstractMemory integration is not available: {e}")

    items: list[Dict[str, Any]] = []
    warnings: list[str] = []
    resolved_owner_id: Optional[str] = None
    run: Optional[RunState] = None

    def _require_run(run_id: str) -> RunState:
        try:
            loaded = rs.load(run_id)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to load run: {e}")
        if loaded is None:
            raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
        return loaded

    _SAFE_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

    def _session_owner_id(session_id: str) -> str:
        sid2 = str(session_id or "").strip()
        if not sid2:
            raise HTTPException(status_code=400, detail="session_id must be a non-empty string")
        if _SAFE_RUN_ID_RE.match(sid2):
            oid = f"session_memory_{sid2}"
            if _SAFE_RUN_ID_RE.match(oid):
                return oid
        digest = hashlib.sha256(sid2.encode("utf-8")).hexdigest()[:32]
        return f"session_memory_sha_{digest}"

    def _global_owner_id() -> str:
        raw = os.environ.get("ABSTRACTRUNTIME_GLOBAL_MEMORY_RUN_ID")
        rid2 = str(raw or "").strip()
        if rid2 and _SAFE_RUN_ID_RE.match(rid2):
            return rid2
        return "global_memory"

    def _query_scope(*, store: Any, scope_label: str, owner_id: Optional[str]) -> list[Dict[str, Any]]:
        q = TripleQuery(
            subject=req.subject,
            predicate=req.predicate,
            object=req.object,
            scope=scope_label,
            owner_id=owner_id,
            since=req.since,
            until=req.until,
            active_at=req.active_at,
            query_text=req.query_text,
            min_score=req.min_score,
            limit=int(req.limit),
            order=str(req.order or "desc"),
        )
        out: list[Dict[str, Any]] = []
        for row in store.query(q):
            to_dict = getattr(row, "to_dict", None)
            if callable(to_dict):
                d = to_dict()
                if isinstance(d, dict):
                    out.append(d)
        return out

    store = None
    try:
        embedder = getattr(svc, "embeddings_client", None)
        store = LanceDBTripleStore(store_path, embedder=embedder)

        if scope == "all":
            if all_owners:
                for label in ("run", "session", "global"):
                    try:
                        items.extend(_query_scope(store=store, scope_label=label, owner_id=None))
                    except HTTPException:
                        raise
                    except Exception as e:
                        warnings.append(f"{label}: {e}")
            else:
                owners: list[tuple[str, str]] = []
                if rid:
                    try:
                        run = _require_run(rid)
                    except HTTPException as e:
                        if int(getattr(e, "status_code", 0) or 0) == 404 and sid:
                            warnings.append(f"Run '{rid}' not found; querying session+global only for session_id='{sid}'")
                        else:
                            raise
                    else:
                        owners.append(("run", resolve_scope_owner_id(run, scope="run", run_store=rs)))
                        owners.append(("session", resolve_scope_owner_id(run, scope="session", run_store=rs)))
                        owners.append(("global", resolve_scope_owner_id(run, scope="global", run_store=rs)))

                if not owners:
                    if not sid:
                        raise HTTPException(
                            status_code=400,
                            detail="run_id or session_id is required when scope=all (or set all_owners=true)",
                        )
                    owners.append(("session", _session_owner_id(sid)))
                    owners.append(("global", _global_owner_id()))
                    warnings.append("run scope omitted (no run_id available)")

                for label, oid in owners:
                    try:
                        items.extend(_query_scope(store=store, scope_label=label, owner_id=oid))
                    except HTTPException:
                        raise
                    except Exception as e:
                        warnings.append(f"{label}: {e}")
        else:
            try:
                if all_owners:
                    resolved_owner_id = None
                else:
                    if owner_override:
                        resolved_owner_id = owner_override
                    elif scope == "global":
                        resolved_owner_id = _global_owner_id()
                    elif scope == "run":
                        if not rid:
                            raise HTTPException(status_code=400, detail="run_id is required for scope=run (or provide owner_id)")
                        resolved_owner_id = rid
                    elif scope == "session":
                        if sid:
                            resolved_owner_id = _session_owner_id(sid)
                        else:
                            if not rid:
                                raise HTTPException(
                                    status_code=400,
                                    detail="run_id or session_id is required for scope=session (or provide owner_id)",
                                )
                            run = _require_run(rid)
                            resolved_owner_id = resolve_scope_owner_id(run, scope="session", run_store=rs)
                    else:
                        raise HTTPException(status_code=400, detail=f"Unsupported scope: {scope}")

                items = _query_scope(store=store, scope_label=scope, owner_id=None if all_owners else resolved_owner_id)
            except HTTPException:
                raise
            except Exception as e:
                warnings.append(str(e))
                items = []

    finally:
        try:
            if store is not None:
                store.close()
        except Exception:
            pass

    # Deterministic ordering for callers (especially scope=all).
    reverse = str(getattr(req, "order", "desc") or "desc").strip().lower() != "asc"
    items.sort(key=lambda d: str(d.get("observed_at") or ""), reverse=reverse)
    limit_value = int(getattr(req, "limit", 0) or 0)
    if limit_value > 0:
        items = items[: max(1, limit_value)]

    return KGQueryResponse(
        ok=True,
        scope=scope,
        owner_id=resolved_owner_id if scope != "all" and not all_owners else None,
        count=len(items),
        items=items,
        warnings=warnings or None,
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


@router.get("/discovery/capabilities")
async def discovery_capabilities() -> Dict[str, Any]:
    """List installed AbstractCore capability plugins (voice/audio/vision) for thin clients."""
    try:
        reg = _get_gateway_capability_registry()
        status = reg.status()
        if isinstance(status, dict):
            return status
        return {"capabilities": {}, "error": "Capability registry returned non-dict status"}
    except HTTPException:
        raise
    except Exception as e:
        return {"capabilities": {}, "error": str(e)}


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


# ---------------------------------------------------------------------------
# Prompt cache control plane (gateway-local, provider-dependent)
# ---------------------------------------------------------------------------

class _GatewayPromptCacheTarget(BaseModel):
    provider: str = Field(..., description="AbstractCore provider name (e.g. mlx, huggingface, openai)")
    model: str = Field(..., description="Model id/name")


class _GatewayPromptCacheSetRequest(_GatewayPromptCacheTarget):
    key: str = Field(..., description="Prompt cache key to create/select")
    make_default: bool = Field(default=True, description="Set this key as default on the provider instance")
    ttl_s: Optional[float] = Field(default=None, description="Optional in-process TTL (seconds) for this key")


class _GatewayPromptCacheUpdateRequest(_GatewayPromptCacheTarget):
    key: str = Field(..., description="Prompt cache key to update/append into")
    prompt: Optional[str] = Field(default=None, description="Raw prompt text (treated as a user message for chat templates)")
    messages: Optional[list[Dict[str, Any]]] = Field(default=None, description="Optional message list to append (provider-dependent)")
    system_prompt: Optional[str] = Field(default=None, description="Optional system prompt to append")
    tools: Optional[list[Dict[str, Any]]] = Field(default=None, description="Optional tool definitions to append")
    add_generation_prompt: bool = Field(default=False, description="If true, append an assistant preamble (backend-dependent)")
    ttl_s: Optional[float] = Field(default=None, description="Optional TTL update (seconds)")


class _GatewayPromptCacheForkRequest(_GatewayPromptCacheTarget):
    from_key: str = Field(..., description="Source prompt cache key")
    to_key: str = Field(..., description="Destination prompt cache key")
    make_default: bool = Field(default=False, description="Set the new key as default on the provider instance")
    ttl_s: Optional[float] = Field(default=None, description="Optional TTL for the new key (seconds)")


class _GatewayPromptCacheClearRequest(_GatewayPromptCacheTarget):
    key: Optional[str] = Field(default=None, description="If omitted, clears all in-process caches for this provider/model worker")


class _GatewayPromptCachePrepareModulesRequest(_GatewayPromptCacheTarget):
    namespace: str = Field(description="Namespace used as a stable prefix for derived keys (e.g. tenant_id:model_id)")
    modules: list[Dict[str, Any]] = Field(description="Ordered list of cache modules (see abstractcore.providers.base.PromptCacheModule)")
    make_default: bool = Field(default=False, description="Set the final derived key as default")
    ttl_s: Optional[float] = Field(default=None, description="Optional TTL for derived keys (seconds)")
    version: int = Field(default=1, description="Hash version for key derivation (bump on formatting changes)")


class _GatewayPromptCacheSaveRequest(_GatewayPromptCacheTarget):
    name: str = Field(..., description="Cache filename label (no extension; stored under the gateway data dir)")
    key: str = Field(..., description="In-memory prompt cache key to save")
    q8: bool = Field(default=False, description="If true and supported, quantize cache before saving (smaller, lossy)")


class _GatewayPromptCacheLoadRequest(_GatewayPromptCacheTarget):
    name: str = Field(..., description="Cache filename label to load (no extension; stored under the gateway data dir)")
    key: Optional[str] = Field(default=None, description="Destination in-memory key (defaults to a new generated key)")
    make_default: bool = Field(default=False, description="Set the loaded key as the provider default key")
    clear_existing: bool = Field(default=False, description="If true, clear all in-process caches before loading")


def _gateway_runtime_llm_client() -> tuple[Optional[Any], Optional[str]]:
    svc = get_gateway_service()
    host = getattr(svc, "host", None)
    runtime = getattr(host, "runtime", None)
    if runtime is None:
        return None, "Gateway host does not expose a shared runtime (prompt caching is unavailable in this mode)."
    llm_client = getattr(runtime, "_abstractcore_llm_client", None)
    if llm_client is None:
        return None, "Gateway runtime has no in-process AbstractCore LLM client (no local KV cache state to manage)."
    return llm_client, None


def _gateway_prompt_cache_dir(*, provider: str, model: str) -> Path:
    svc = get_gateway_service()
    base = Path(getattr(getattr(svc, "stores", None), "base_dir", Path("."))).expanduser().resolve()
    prov = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(provider or "").strip().lower()) or "provider"
    model_label = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(model or "").strip().replace("/", "__")) or "model"
    # Keep path segments bounded to avoid filesystem issues.
    prov = prov[:80]
    model_label = model_label[:120]
    out = base / "prompt_cache" / prov / model_label
    out.mkdir(parents=True, exist_ok=True)
    return out


def _safe_cache_label(label: str) -> str:
    raw = str(label or "").strip()
    if not raw:
        raise ValueError("name is required")
    # Disallow path traversal and normalize to a conservative filename.
    raw = raw.replace("\\", "/").split("/", 1)[0]
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", raw).strip("._") or "cache"
    return safe[:120]


def _is_mlx_provider(obj: Any) -> bool:
    try:
        prov = str(getattr(obj, "provider", "") or "").strip().lower()
        if prov == "mlx":
            return True
    except Exception:
        pass
    return obj is not None and obj.__class__.__name__.lower().startswith("mlx")


def _is_huggingface_gguf_provider(obj: Any) -> bool:
    """Return True for the local HuggingFace GGUF/llama.cpp provider (in-process KV state)."""
    try:
        prov = str(getattr(obj, "provider", "") or "").strip().lower()
        if prov != "huggingface":
            return False
        model_type = str(getattr(obj, "model_type", "") or "").strip().lower()
        return model_type == "gguf"
    except Exception:
        return False


@router.get("/prompt_cache/stats")
async def prompt_cache_stats(
    provider: str = Query(..., description="AbstractCore provider name"),
    model: str = Query(..., description="Model id/name"),
) -> Dict[str, Any]:
    llm_client, err = _gateway_runtime_llm_client()
    if err:
        return {"supported": False, "error": err}

    try:
        getter = getattr(llm_client, "get_provider_instance", None)
        if not callable(getter):
            return {"supported": False, "error": "Runtime LLM client does not expose provider instances"}
        prov = getter(provider=provider, model=model)
    except Exception as e:
        return {"supported": False, "error": str(e)}

    if prov is None:
        return {"supported": False, "error": "Provider instance not available"}

    try:
        supported = bool(getattr(prov, "supports_prompt_cache", lambda: False)())
    except Exception:
        supported = False
    if not supported:
        return {"supported": False, "error": "Provider does not support prompt cache control in this deployment"}

    try:
        stats = getattr(prov, "get_prompt_cache_stats")() if hasattr(prov, "get_prompt_cache_stats") else {}
        return {"supported": True, "stats": stats}
    except Exception as e:
        return {"supported": False, "error": str(e)}


@router.post("/prompt_cache/set")
async def prompt_cache_set(req: _GatewayPromptCacheSetRequest) -> Dict[str, Any]:
    llm_client, err = _gateway_runtime_llm_client()
    if err:
        return {"supported": False, "error": err}
    try:
        prov = llm_client.get_provider_instance(provider=req.provider, model=req.model)
    except Exception as e:
        return {"supported": False, "error": str(e)}

    if prov is None or not bool(getattr(prov, "supports_prompt_cache", lambda: False)()):
        return {"supported": False, "error": "Provider does not support prompt cache control"}
    if not hasattr(prov, "prompt_cache_set"):
        return {"supported": False, "error": "Provider does not expose prompt cache API"}
    try:
        ok = prov.prompt_cache_set(req.key, make_default=bool(req.make_default), ttl_s=req.ttl_s)
        return {"supported": True, "ok": bool(ok)}
    except Exception as e:
        return {"supported": False, "error": str(e)}


@router.post("/prompt_cache/update")
async def prompt_cache_update(req: _GatewayPromptCacheUpdateRequest) -> Dict[str, Any]:
    llm_client, err = _gateway_runtime_llm_client()
    if err:
        return {"supported": False, "error": err}
    try:
        prov = llm_client.get_provider_instance(provider=req.provider, model=req.model)
    except Exception as e:
        return {"supported": False, "error": str(e)}

    if prov is None or not bool(getattr(prov, "supports_prompt_cache", lambda: False)()):
        return {"supported": False, "error": "Provider does not support prompt cache control"}
    if not hasattr(prov, "prompt_cache_update"):
        return {"supported": False, "error": "Provider does not expose prompt cache API"}
    try:
        ok = prov.prompt_cache_update(
            req.key,
            prompt=str(req.prompt or ""),
            messages=req.messages,
            system_prompt=req.system_prompt,
            tools=req.tools,
            add_generation_prompt=bool(req.add_generation_prompt),
            ttl_s=req.ttl_s,
        )
        return {"supported": True, "ok": bool(ok)}
    except Exception as e:
        return {"supported": False, "error": str(e)}


@router.post("/prompt_cache/fork")
async def prompt_cache_fork(req: _GatewayPromptCacheForkRequest) -> Dict[str, Any]:
    llm_client, err = _gateway_runtime_llm_client()
    if err:
        return {"supported": False, "error": err}
    try:
        prov = llm_client.get_provider_instance(provider=req.provider, model=req.model)
    except Exception as e:
        return {"supported": False, "error": str(e)}

    if prov is None or not bool(getattr(prov, "supports_prompt_cache", lambda: False)()):
        return {"supported": False, "error": "Provider does not support prompt cache control"}
    if not hasattr(prov, "prompt_cache_fork"):
        return {"supported": False, "error": "Provider does not expose prompt cache API"}
    try:
        ok = prov.prompt_cache_fork(req.from_key, req.to_key, make_default=bool(req.make_default), ttl_s=req.ttl_s)
        return {"supported": True, "ok": bool(ok)}
    except Exception as e:
        return {"supported": False, "error": str(e)}


@router.post("/prompt_cache/clear")
async def prompt_cache_clear(req: _GatewayPromptCacheClearRequest) -> Dict[str, Any]:
    llm_client, err = _gateway_runtime_llm_client()
    if err:
        return {"supported": False, "error": err}
    try:
        prov = llm_client.get_provider_instance(provider=req.provider, model=req.model)
    except Exception as e:
        return {"supported": False, "error": str(e)}

    if prov is None or not bool(getattr(prov, "supports_prompt_cache", lambda: False)()):
        return {"supported": False, "error": "Provider does not support prompt cache control"}
    if not hasattr(prov, "prompt_cache_clear"):
        return {"supported": False, "error": "Provider does not expose prompt cache API"}
    try:
        ok = prov.prompt_cache_clear(req.key)
        return {"supported": True, "ok": bool(ok)}
    except Exception as e:
        return {"supported": False, "error": str(e)}


@router.post("/prompt_cache/prepare_modules")
async def prompt_cache_prepare_modules(req: _GatewayPromptCachePrepareModulesRequest) -> Dict[str, Any]:
    llm_client, err = _gateway_runtime_llm_client()
    if err:
        return {"supported": False, "error": err}
    try:
        prov = llm_client.get_provider_instance(provider=req.provider, model=req.model)
    except Exception as e:
        return {"supported": False, "error": str(e)}

    if prov is None or not bool(getattr(prov, "supports_prompt_cache", lambda: False)()):
        return {"supported": False, "error": "Provider does not support prompt cache control"}
    if not hasattr(prov, "prompt_cache_prepare_modules"):
        return {"supported": False, "error": "Provider does not expose prompt cache module API"}
    try:
        result = prov.prompt_cache_prepare_modules(
            namespace=req.namespace,
            modules=req.modules,
            make_default=bool(req.make_default),
            ttl_s=req.ttl_s,
            version=int(req.version),
        )
        return result if isinstance(result, dict) else {"supported": True, "result": result}
    except Exception as e:
        return {"supported": False, "error": str(e)}


@router.get("/prompt_cache/saved")
async def prompt_cache_saved(
    provider: str = Query(..., description="AbstractCore provider name"),
    model: str = Query(..., description="Model id/name"),
) -> Dict[str, Any]:
    """List saved prompt/KV caches stored on the gateway host (provider-dependent)."""
    try:
        base = _gateway_prompt_cache_dir(provider=provider, model=model)
    except Exception as e:
        return {"items": [], "error": str(e)}

    items: list[Dict[str, Any]] = []
    try:
        for meta_path in sorted(base.glob("*.meta.json")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
            name = meta_path.name.replace(".meta.json", "")
            items.append(
                {
                    "name": name,
                    "provider": meta.get("provider") or provider,
                    "model": meta.get("model") or model,
                    "saved_at": meta.get("saved_at"),
                    "token_count": meta.get("token_count"),
                    "key": meta.get("key"),
                    "meta": meta,
                }
            )
    except Exception as e:
        return {"items": [], "error": str(e)}

    items.sort(key=lambda d: str(d.get("saved_at") or d.get("name") or ""), reverse=True)
    return {"items": items}


@router.post("/prompt_cache/save")
async def prompt_cache_save(req: _GatewayPromptCacheSaveRequest) -> Dict[str, Any]:
    llm_client, err = _gateway_runtime_llm_client()
    if err:
        return {"supported": False, "error": err}
    try:
        prov = llm_client.get_provider_instance(provider=req.provider, model=req.model)
    except Exception as e:
        return {"supported": False, "error": str(e)}

    if prov is None or not bool(getattr(prov, "supports_prompt_cache", lambda: False)()):
        return {"supported": False, "error": "Provider does not support prompt cache control"}
    if not (_is_mlx_provider(prov) or _is_huggingface_gguf_provider(prov)):
        return {"supported": False, "error": "Cache save is only supported for in-process providers (mlx, huggingface/gguf) in this release"}  # noqa: E501

    try:
        name = _safe_cache_label(req.name)
    except Exception as e:
        return {"supported": False, "error": str(e)}

    cache_dir = _gateway_prompt_cache_dir(provider=req.provider, model=req.model)
    filename = cache_dir / f"{name}.safetensors"
    meta_path = cache_dir / f"{name}.meta.json"

    key = str(req.key or "").strip()
    if not key:
        return {"supported": False, "error": "key is required"}

    try:
        store = getattr(prov, "_prompt_cache_store", None)
        cache_obj = store.get(key) if store is not None and hasattr(store, "get") else None
    except Exception:
        cache_obj = None

    if cache_obj is None:
        return {"supported": False, "error": f"No in-memory cache found for key '{key}'"}

    meta: Dict[str, Any] = {
        "schema": "abstractgateway-prompt-cache/v1",
        "provider": str(req.provider),
        "model": str(getattr(prov, "model", req.model)),
        "saved_at": datetime.datetime.now().astimezone().isoformat(),
        "key": key,
    }

    if _is_mlx_provider(prov):
        try:
            from mlx_lm.models.cache import save_prompt_cache
        except Exception:
            return {"supported": False, "error": "MLX cache save requires mlx-lm (install: `pip install abstractcore[mlx]`)"}  # noqa: E501

        try:
            tok_fn = getattr(prov, "_prompt_cache_backend_token_count", None)
            if callable(tok_fn):
                tok = tok_fn(cache_obj)
                if isinstance(tok, int) and tok >= 0:
                    meta["token_count"] = tok
        except Exception:
            pass

        cache_to_save = cache_obj
        if bool(req.q8):
            try:
                cache_to_save = [layer.to_quantized(group_size=64, bits=8) for layer in cache_obj]
                meta["quantized"] = "q8"
            except Exception as e:
                # Best-effort: fall back to full-precision save (do not lose the cache).
                meta["quantized"] = "q8_failed"
                meta["quantize_error"] = str(e)
                cache_to_save = cache_obj

        try:
            save_prompt_cache(str(filename), cache_to_save, metadata=meta)
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            return {"supported": False, "error": str(e)}

    else:
        # HuggingFace GGUF/llama.cpp: serialize LlamaRAMCache state to safetensors (gateway-local).
        try:
            import numpy as np
            from safetensors.numpy import save_file as _save_safetensors_numpy
        except Exception as e:
            return {"supported": False, "error": f"HF cache save requires numpy+safetensors: {e}"}

        try:
            from llama_cpp.llama_cache import LlamaRAMCache
        except Exception as e:
            return {"supported": False, "error": f"HF cache save requires llama-cpp-python: {e}"}

        if not isinstance(cache_obj, LlamaRAMCache):
            return {"supported": False, "error": "HF cache save expects llama_cpp.llama_cache.LlamaRAMCache"}

        cache_state = getattr(cache_obj, "cache_state", None)
        if not hasattr(cache_state, "items"):
            return {"supported": False, "error": "HF cache object has no cache_state mapping to serialize"}

        # Best-effort token count: max n_tokens across cached states.
        token_count = None
        try:
            n_tokens_list = [int(getattr(st, "n_tokens", 0) or 0) for st in cache_state.values()]
            token_count = max(n_tokens_list) if n_tokens_list else 0
            if token_count >= 0:
                meta["token_count"] = token_count
        except Exception:
            pass

        cap = int(getattr(cache_obj, "capacity_bytes", 0) or 0)
        if cap > 0:
            meta["capacity_bytes"] = cap
        if bool(req.q8):
            meta["quantized"] = "unsupported"

        tensors: Dict[str, Any] = {}
        tensors["cache_capacity_bytes"] = np.asarray([cap], dtype=np.int64)
        tensors["cache_num_entries"] = np.asarray([len(cache_state)], dtype=np.int64)

        def _as_int(x: Any, *, default: int = 0) -> int:
            try:
                return int(x)
            except Exception:
                return default

        for idx, (_k, st) in enumerate(cache_state.items()):
            input_ids = getattr(st, "input_ids", None)
            scores = getattr(st, "scores", None)
            llama_state = getattr(st, "llama_state", None)
            if input_ids is None or scores is None or llama_state is None:
                continue

            tensors[f"state.{idx}.input_ids"] = np.asarray(input_ids, dtype=np.int32)
            tensors[f"state.{idx}.scores"] = np.asarray(scores, dtype=np.float32)
            tensors[f"state.{idx}.n_tokens"] = np.asarray([_as_int(getattr(st, "n_tokens", 0))], dtype=np.int64)
            tensors[f"state.{idx}.seed"] = np.asarray([_as_int(getattr(st, "seed", 0))], dtype=np.int64)

            raw = bytes(llama_state)
            tensors[f"state.{idx}.llama_state"] = np.frombuffer(raw, dtype=np.uint8)
            tensors[f"state.{idx}.llama_state_size"] = np.asarray(
                [_as_int(getattr(st, "llama_state_size", len(raw)), default=len(raw))],
                dtype=np.int64,
            )

        try:
            file_meta = {
                "schema": str(meta.get("schema") or ""),
                "provider": str(meta.get("provider") or ""),
                "model": str(meta.get("model") or ""),
                "saved_at": str(meta.get("saved_at") or ""),
                "key": str(meta.get("key") or ""),
            }
            if isinstance(meta.get("token_count"), int):
                file_meta["token_count"] = str(int(meta["token_count"]))
            if isinstance(meta.get("capacity_bytes"), int):
                file_meta["capacity_bytes"] = str(int(meta["capacity_bytes"]))
            _save_safetensors_numpy(tensors, str(filename), metadata=file_meta)
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            return {"supported": False, "error": str(e)}

    return {"supported": True, "ok": True, "name": name, "path": str(filename), "meta": meta}


@router.post("/prompt_cache/load")
async def prompt_cache_load(req: _GatewayPromptCacheLoadRequest) -> Dict[str, Any]:
    llm_client, err = _gateway_runtime_llm_client()
    if err:
        return {"supported": False, "error": err}
    try:
        prov = llm_client.get_provider_instance(provider=req.provider, model=req.model)
    except Exception as e:
        return {"supported": False, "error": str(e)}

    if prov is None or not bool(getattr(prov, "supports_prompt_cache", lambda: False)()):
        return {"supported": False, "error": "Provider does not support prompt cache control"}
    if not (_is_mlx_provider(prov) or _is_huggingface_gguf_provider(prov)):
        return {"supported": False, "error": "Cache load is only supported for in-process providers (mlx, huggingface/gguf) in this release"}  # noqa: E501

    try:
        name = _safe_cache_label(req.name)
    except Exception as e:
        return {"supported": False, "error": str(e)}

    cache_dir = _gateway_prompt_cache_dir(provider=req.provider, model=req.model)
    filename = cache_dir / f"{name}.safetensors"
    meta_path = cache_dir / f"{name}.meta.json"
    if not filename.exists():
        return {"supported": False, "error": f"Cache not found: {name}"}

    loaded_cache = None
    meta: Dict[str, Any] = {}
    if _is_mlx_provider(prov):
        try:
            from mlx_lm.models.cache import load_prompt_cache
        except Exception:
            return {"supported": False, "error": "MLX cache load requires mlx-lm (install: `pip install abstractcore[mlx]`)"}  # noqa: E501

        try:
            loaded_cache, meta = load_prompt_cache(str(filename), return_metadata=True)
        except Exception as e:
            return {"supported": False, "error": f"Failed to load cache: {e}"}
    else:
        # HuggingFace GGUF/llama.cpp: deserialize LlamaRAMCache state from safetensors.
        try:
            from safetensors import safe_open as _safe_open
            import numpy as np
        except Exception as e:
            return {"supported": False, "error": f"HF cache load requires numpy+safetensors: {e}"}

        try:
            from llama_cpp.llama_cache import LlamaRAMCache
            from llama_cpp.llama import LlamaState
        except Exception as e:
            return {"supported": False, "error": f"HF cache load requires llama-cpp-python: {e}"}

        # Best-effort: read JSON meta sidecar if present (used for listing + model lock).
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}

        tensors: Dict[str, Any] = {}
        file_meta: Dict[str, str] = {}
        try:
            with _safe_open(str(filename), framework="numpy") as f:
                file_meta = dict(f.metadata() or {})
                for k in f.keys():
                    tensors[k] = f.get_tensor(k)
        except Exception as e:
            return {"supported": False, "error": f"Failed to load cache: {e}"}

        # Merge in-file metadata (prefer explicit meta.json when available).
        if not meta and isinstance(file_meta, dict) and file_meta:
            meta = dict(file_meta)

        cap = 0
        try:
            cap_t = tensors.get("cache_capacity_bytes")
            if cap_t is not None:
                cap = int(np.asarray(cap_t).reshape(-1)[0])
        except Exception:
            cap = 0
        if cap <= 0:
            cap = 512 << 20

        loaded_cache = LlamaRAMCache(capacity_bytes=int(cap))

        # Reconstruct cache entries.
        idxs = set()
        for k in tensors.keys():
            if k.startswith("state.") and ".input_ids" in k:
                try:
                    idxs.add(int(k.split(".")[1]))
                except Exception:
                    continue
        for idx in sorted(idxs):
            try:
                input_ids = np.asarray(tensors[f"state.{idx}.input_ids"], dtype=np.int32)
                scores = np.asarray(tensors[f"state.{idx}.scores"], dtype=np.float32)
                n_tokens = int(np.asarray(tensors[f"state.{idx}.n_tokens"]).reshape(-1)[0])
                seed = int(np.asarray(tensors[f"state.{idx}.seed"]).reshape(-1)[0])
                llama_state_arr = np.asarray(tensors[f"state.{idx}.llama_state"], dtype=np.uint8)
                llama_state_size = int(np.asarray(tensors[f"state.{idx}.llama_state_size"]).reshape(-1)[0])
            except Exception:
                continue

            llama_state_bytes = llama_state_arr.tobytes()
            if llama_state_size != len(llama_state_bytes):
                llama_state_size = len(llama_state_bytes)

            st = LlamaState(
                input_ids=input_ids.astype(np.intc, copy=False),
                scores=scores.astype(np.single, copy=False),
                n_tokens=n_tokens,
                llama_state=llama_state_bytes,
                llama_state_size=llama_state_size,
                seed=seed,
            )
            key_tokens = tuple(int(x) for x in st.input_ids[: st.n_tokens].tolist())
            loaded_cache[key_tokens] = st

    required_model = None
    if isinstance(meta, dict):
        required_model = meta.get("model") or meta.get("model_id")
    current_model = str(getattr(prov, "model", req.model))
    if isinstance(required_model, str) and required_model.strip() and required_model.strip() != current_model:
        return {
            "supported": False,
            "error": "Prompt cache model mismatch",
            "required_model": required_model.strip(),
            "current_model": current_model,
        }

    if bool(req.clear_existing):
        try:
            prov.prompt_cache_clear(None)
        except Exception:
            pass

    key = str(req.key or "").strip() or f"loaded:{uuid.uuid4().hex[:12]}"
    try:
        # Best-effort: allocate a key (ensures default_key wiring is consistent).
        prov.prompt_cache_set(key, make_default=bool(req.make_default))
    except Exception:
        pass

    try:
        store = getattr(prov, "_prompt_cache_store", None)
        if store is None or not hasattr(store, "set"):
            return {"supported": False, "error": "Provider does not expose an in-process cache store"}
        store.set(
            key,
            loaded_cache,
            meta={
                "backend": "mlx" if _is_mlx_provider(prov) else "llama_cpp",
                "loaded_from": str(filename),
                **(meta if isinstance(meta, dict) else {}),
            },
        )
        # Best-effort: activate this cache on the running llama instance (HF only).
        if _is_huggingface_gguf_provider(prov):
            try:
                if getattr(prov, "llm", None) is not None and hasattr(prov.llm, "set_cache"):
                    prov.llm.set_cache(loaded_cache)
            except Exception:
                pass
        if bool(req.make_default):
            try:
                setattr(prov, "_default_prompt_cache_key", key)
            except Exception:
                pass
    except Exception as e:
        return {"supported": False, "error": f"Failed to install cache into provider: {e}"}

    return {"supported": True, "ok": True, "key": key, "meta": meta if isinstance(meta, dict) else {}}


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


@router.get("/workspace/policy")
async def workspace_policy() -> Dict[str, Any]:
    """Return the operator-configured server workspace policy (read-only, safe for thin clients)."""
    return {"ok": True, "policy": _server_workspace_policy_public()}


@router.get("/files/search")
async def files_search(
    query: str = Query(..., description="Case-insensitive substring match on file path/name."),
    limit: int = Query(20, ge=1, le=200),
    workspace_root: Optional[str] = Query(None, description="Optional workspace root override for this search."),
    workspace_access_mode: Optional[str] = Query(None, description="Workspace access mode (workspace_only|workspace_or_allowed)."),
    workspace_allowed_paths: Optional[str] = Query(None, description="Newline-separated allowed root directories (mounted for search)."),
    workspace_ignored_paths: Optional[str] = Query(None, description="Newline-separated ignored paths (prunes search)."),
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

    base_default = _workspace_root()
    mounts_default = _workspace_mounts()
    base = base_default
    mounts = mounts_default
    blocked: tuple[Path, ...] = ()

    if _client_workspace_scope_overrides_enabled():
        # Opt-in scoped search: allow the UI to drive workspace_* for local/dev flows.
        has_scope = bool(str(workspace_root or "").strip() or str(workspace_access_mode or "").strip() or str(workspace_allowed_paths or "").strip() or str(workspace_ignored_paths or "").strip())
        if has_scope:
            base, mounts, blocked, _mode = _effective_workspace_scope(
                default_base=base_default,
                workspace_root=workspace_root,
                workspace_access_mode=workspace_access_mode,
                workspace_allowed_paths=workspace_allowed_paths,
                workspace_ignored_paths=workspace_ignored_paths,
            )

    try:
        # Index build can be slow on large workspaces; keep async endpoints responsive.
        paths = await asyncio.to_thread(_get_file_index, base=base, mounts=mounts, blocked=blocked)
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
    out: list[Dict[str, Any]] = []
    mount_names = set(mounts.keys())
    for _score, _len, s in scored[: int(limit)]:
        item: Dict[str, Any] = {"path": s}
        if "/" in s:
            head = s.split("/", 1)[0]
            if head in mount_names:
                item["mount"] = head
        try:
            resolved, _virt, _m, _root = _resolve_workspace_path(base=base, mounts=mounts, raw_path=s)
            st = resolved.stat()
            item["size_bytes"] = int(getattr(st, "st_size", 0) or 0)
        except Exception:
            # Best-effort: files may disappear between index build and search.
            pass
        out.append(item)
    return {"items": out}


@router.get("/files/read")
async def files_read(
    path: str = Query(..., description="Workspace-relative path (preferred) or absolute path under workspace root."),
    start_line: int = Query(1, ge=1),
    end_line: Optional[int] = Query(None, ge=1),
    workspace_root: Optional[str] = Query(None, description="Optional workspace root override for this read."),
    workspace_access_mode: Optional[str] = Query(None, description="Workspace access mode (workspace_only|workspace_or_allowed)."),
    workspace_allowed_paths: Optional[str] = Query(None, description="Newline-separated allowed root directories (mounted for reads)."),
    workspace_ignored_paths: Optional[str] = Query(None, description="Newline-separated ignored paths (blocked)."),
) -> Dict[str, Any]:
    """Read a workspace file for @file mentions (best-effort).

    Uses the same implementation as AbstractCore's `read_file` tool (including `.abstractignore`).
    """
    base_default = _workspace_root()
    mounts_default = _workspace_mounts()
    base = base_default
    mounts = mounts_default
    blocked: tuple[Path, ...] = ()
    mode = "workspace_only"

    if _client_workspace_scope_overrides_enabled():
        has_scope = bool(str(workspace_root or "").strip() or str(workspace_access_mode or "").strip() or str(workspace_allowed_paths or "").strip() or str(workspace_ignored_paths or "").strip())
        if has_scope:
            base, mounts, blocked, mode = _effective_workspace_scope(
                default_base=base_default,
                workspace_root=workspace_root,
                workspace_access_mode=workspace_access_mode,
                workspace_allowed_paths=workspace_allowed_paths,
                workspace_ignored_paths=workspace_ignored_paths,
            )

    if mode == "all_except_ignored":
        p_raw = str(path or "").strip()
        if p_raw.startswith("@"):
            p_raw = p_raw[1:].lstrip()
        p2 = Path(p_raw).expanduser()
        if p2.is_absolute():
            try:
                resolved = p2.resolve()
            except Exception:
                raise HTTPException(status_code=400, detail="invalid absolute path")
            virt = str(resolved)
            root = resolved.parent
        else:
            resolved, virt, _mount, root = _resolve_workspace_path(base=base, mounts={}, raw_path=p_raw)
    else:
        resolved, virt, _mount, root = _resolve_workspace_path(base=base, mounts=mounts, raw_path=path)

    if blocked and _is_under_allowed_roots(resolved, list(blocked)):
        raise HTTPException(status_code=403, detail="path is blocked by workspace_ignored_paths")

    try:
        from abstractcore.tools.common_tools import read_file

        content = read_file(str(resolved), start_line=start_line, end_line=end_line)
    except Exception as e:
        content = f"Error: Failed to read '{resolved}': {e}"

    out_path = str(virt) if isinstance(virt, str) and virt.strip() else None
    if not out_path:
        try:
            out_path = resolved.relative_to(root).as_posix()
        except Exception:
            out_path = str(resolved)

    return {"path": out_path, "content": content}


@router.get("/files/skim")
async def files_skim(
    path: str = Query(..., description="Workspace-relative path (preferred) or absolute path under workspace root."),
    target_percent: float = Query(8.0, ge=1.0, le=25.0, description="Percent of lines to sample (default 8)."),
    head_lines: int = Query(25, ge=0, le=500, description="Max lines sampled from the start (default 25)."),
    tail_lines: int = Query(25, ge=0, le=500, description="Max lines sampled from the end (default 25)."),
    workspace_root: Optional[str] = Query(None, description="Optional workspace root override for this skim."),
    workspace_access_mode: Optional[str] = Query(None, description="Workspace access mode (workspace_only|workspace_or_allowed)."),
    workspace_allowed_paths: Optional[str] = Query(None, description="Newline-separated allowed root directories (mounted for reads)."),
    workspace_ignored_paths: Optional[str] = Query(None, description="Newline-separated ignored paths (blocked)."),
) -> Dict[str, Any]:
    """Skim a workspace file for @file mentions (best-effort).

    Uses the same implementation as AbstractCore's `skim_files` tool (including `.abstractignore`).
    """
    base_default = _workspace_root()
    mounts_default = _workspace_mounts()
    base = base_default
    mounts = mounts_default
    blocked: tuple[Path, ...] = ()
    mode = "workspace_only"

    if _client_workspace_scope_overrides_enabled():
        has_scope = bool(str(workspace_root or "").strip() or str(workspace_access_mode or "").strip() or str(workspace_allowed_paths or "").strip() or str(workspace_ignored_paths or "").strip())
        if has_scope:
            base, mounts, blocked, mode = _effective_workspace_scope(
                default_base=base_default,
                workspace_root=workspace_root,
                workspace_access_mode=workspace_access_mode,
                workspace_allowed_paths=workspace_allowed_paths,
                workspace_ignored_paths=workspace_ignored_paths,
            )

    if mode == "all_except_ignored":
        p_raw = str(path or "").strip()
        if p_raw.startswith("@"):
            p_raw = p_raw[1:].lstrip()
        p2 = Path(p_raw).expanduser()
        if p2.is_absolute():
            try:
                resolved = p2.resolve()
            except Exception:
                raise HTTPException(status_code=400, detail="invalid absolute path")
            virt = str(resolved)
            root = resolved.parent
        else:
            resolved, virt, _mount, root = _resolve_workspace_path(base=base, mounts={}, raw_path=p_raw)
    else:
        resolved, virt, _mount, root = _resolve_workspace_path(base=base, mounts=mounts, raw_path=path)

    if blocked and _is_under_allowed_roots(resolved, list(blocked)):
        raise HTTPException(status_code=403, detail="path is blocked by workspace_ignored_paths")

    try:
        from abstractcore.tools.common_tools import skim_files

        content = skim_files(
            [str(resolved)],
            target_percent=target_percent,
            head_lines=head_lines,
            tail_lines=tail_lines,
        )
    except Exception as e:
        content = f"Error: Failed to skim '{resolved}': {e}"

    out_path = str(virt) if isinstance(virt, str) and virt.strip() else None
    if not out_path:
        try:
            out_path = resolved.relative_to(root).as_posix()
        except Exception:
            out_path = str(resolved)

    return {"path": out_path, "content": content}


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

    base_default = _workspace_root()
    mounts_default = _workspace_mounts()
    base = base_default
    mounts = mounts_default
    blocked: tuple[Path, ...] = ()
    mode = "workspace_only"

    if _client_workspace_scope_overrides_enabled():
        has_scope = bool(
            str(req.workspace_root or "").strip()
            or str(req.workspace_access_mode or "").strip()
            or str(req.workspace_allowed_paths or "").strip()
            or str(req.workspace_ignored_paths or "").strip()
        )
        if has_scope:
            base, mounts, blocked, mode = _effective_workspace_scope(
                default_base=base_default,
                workspace_root=req.workspace_root,
                workspace_access_mode=req.workspace_access_mode,
                workspace_allowed_paths=req.workspace_allowed_paths,
                workspace_ignored_paths=req.workspace_ignored_paths,
            )

    raw_path = str(req.path or "")
    if mode == "all_except_ignored":
        p_raw = raw_path.strip()
        if p_raw.startswith("@"):
            p_raw = p_raw[1:].lstrip()
        p2 = Path(p_raw).expanduser()
        if p2.is_absolute():
            try:
                resolved = p2.resolve()
            except Exception:
                raise HTTPException(status_code=400, detail="invalid absolute path")
            virt = str(resolved)
            root = resolved.parent
        else:
            resolved, virt, _mount, root = _resolve_workspace_path(base=base, mounts={}, raw_path=p_raw)
    else:
        resolved, virt, _mount, root = _resolve_workspace_path(base=base, mounts=mounts, raw_path=raw_path)

    if blocked and _is_under_allowed_roots(resolved, list(blocked)):
        raise HTTPException(status_code=403, detail="path is blocked by workspace_ignored_paths")

    try:
        from abstractcore.tools.abstractignore import AbstractIgnore

        ignore = AbstractIgnore.for_path(root)
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

    sha256 = hashlib.sha256(bytes(content)).hexdigest()

    filename = str(req.filename or "").strip() or resolved.name
    content_type = str(req.content_type or "").strip().lower()
    if not content_type:
        guessed, _enc = mimetypes.guess_type(filename)
        content_type = str(guessed or "application/octet-stream")

    rel = str(virt) if isinstance(virt, str) and virt.strip() else str(resolved)

    rs = svc.host.run_store
    try:
        rid = _ensure_session_memory_owner_run_exists(run_store=rs, session_id=sid)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create session memory run: {e}")

    store = getattr(getattr(svc, "stores", None), "artifact_store", None)
    store_fn = getattr(store, "store", None)
    if not callable(store_fn):
        raise HTTPException(status_code=500, detail="Artifact store is not available")

    tags: Dict[str, str] = {
        "kind": "attachment",
        "target": "server",
        "source": "workspace",
        "path": str(rel),
        "filename": str(filename),
        "session_id": sid,
        "sha256": sha256,
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
        "target": "server",
        "filename": filename,
        "content_type": content_type,
        "source_path": rel,
        "sha256": sha256,
    }

    return {"ok": True, "run_id": str(rid), "attachment": attachment_ref, "metadata": meta_dict}


@router.post("/attachments/upload")
async def attachments_upload(
    session_id: str = Form(..., description="Session id (attachments are stored under the session memory owner run)."),
    file: UploadFile = File(..., description="File to upload."),
    filename: Optional[str] = Form(None, description="Optional filename override (defaults to uploaded filename)."),
    content_type: Optional[str] = Form(
        None,
        description="Optional content type override (defaults to uploaded content_type or best-effort guess).",
    ),
) -> Dict[str, Any]:
    """Upload bytes as an artifact-backed attachment.

    This endpoint exists primarily for browser clients (drag & drop) where
    local file paths are not accessible to the UI.
    """
    svc = get_gateway_service()

    sid = str(session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="session_id is required")

    try:
        max_bytes_raw = str(os.getenv("ABSTRACTGATEWAY_MAX_ATTACHMENT_BYTES", "") or "").strip()
        max_bytes = int(max_bytes_raw) if max_bytes_raw else 25 * 1024 * 1024
        if max_bytes <= 0:
            max_bytes = 25 * 1024 * 1024
    except Exception:
        max_bytes = 25 * 1024 * 1024

    try:
        content = await file.read()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to read upload: {e}")

    size = len(content or b"")
    if size > max_bytes:
        raise HTTPException(status_code=413, detail=f"Attachment too large ({size} bytes > {max_bytes} bytes)")

    sha256 = hashlib.sha256(bytes(content or b"")).hexdigest()

    filename_final = str(filename or "").strip() or str(getattr(file, "filename", "") or "").strip() or "upload.bin"
    content_type_final = str(content_type or "").strip().lower() or str(getattr(file, "content_type", "") or "").strip().lower()
    if not content_type_final:
        guessed, _enc = mimetypes.guess_type(filename_final)
        content_type_final = str(guessed or "application/octet-stream")

    rs = svc.host.run_store
    try:
        rid = _ensure_session_memory_owner_run_exists(run_store=rs, session_id=sid)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create session memory run: {e}")

    store = getattr(getattr(svc, "stores", None), "artifact_store", None)
    store_fn = getattr(store, "store", None)
    if not callable(store_fn):
        raise HTTPException(status_code=500, detail="Artifact store is not available")

    # Prefix client uploads to avoid collisions with server workspace virtual paths.
    handle = f"client:{filename_final}"

    tags: Dict[str, str] = {
        "kind": "attachment",
        "target": "client",
        "source": "upload",
        "path": str(handle),
        "filename": str(filename_final),
        "session_id": sid,
        "sha256": sha256,
    }
    try:
        meta = store_fn(bytes(content or b""), content_type=str(content_type_final), run_id=str(rid), tags=tags)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to store attachment artifact: {e}")

    to_dict = getattr(meta, "to_dict", None)
    meta_dict = to_dict() if callable(to_dict) else {}
    if not isinstance(meta_dict, dict):
        meta_dict = {}

    attachment_ref: Dict[str, Any] = {
        "$artifact": str(getattr(meta, "artifact_id", "") or ""),
        "target": "client",
        "filename": filename_final,
        "content_type": content_type_final,
        "source_path": handle,
        "sha256": sha256,
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
