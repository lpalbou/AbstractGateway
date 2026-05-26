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
import io
import json
import logging
import mimetypes
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from abstractruntime.core.run_lifecycle import normalize_run_lifecycle_vars
from abstractruntime.storage.commands import CommandRecord
from abstractruntime.storage.base import QueryableRunIndexStore, QueryableRunStore
from abstractruntime.core.models import Effect, EffectType, RunState, RunStatus, StepRecord, WaitReason

from ..memory_store import (
    memory_backend_unavailable_reason,
    memory_store_exists,
    open_gateway_memory_store,
    resolve_memory_store_config,
)
from ..embeddings_config import resolve_embedding_config
from ..capability_defaults import (
    clear_gateway_capability_default,
    gateway_capability_defaults_payload,
    save_gateway_capability_default,
)
from ..provider_defaults import ProviderModelConfigError, resolve_gateway_provider_model
from .. import host_metrics
from ..run_retention import (
    DraftRunPurgeOptions,
    DraftRunPurgeUnsupported,
    purge_ephemeral_draft_runs,
    write_gateway_workspace_marker,
)
from ..service import backlog_exec_runner_status, get_gateway_service, is_draft_run_lifecycle, run_summary
from ..workspace_tools import AbstractIgnore, read_file as read_workspace_file, skim_files as skim_workspace_files
from ..workflow_deprecations import WorkflowDeprecatedError


router = APIRouter(prefix="/gateway", tags=["gateway"])
logger = logging.getLogger(__name__)

_GATEWAY_CATALOG_CONTRACT = "gateway_catalog_v1"
_GATEWAY_CATALOG_VERSION = 1
_OMNIVOICE_FALLBACK_LANGUAGES = ["en", "fr", "de", "es", "ru", "zh", "ja", "ko"]


@router.get("/ping")
async def gateway_ping() -> Dict[str, Any]:
    """Cheap authenticated gateway reachability probe.

    Thin clients must not use capability/model discovery as their login check:
    discovery can legitimately skip offline providers or time out optional
    remote integrations. This route validates Gateway reachability/auth only.
    """
    return {
        "ok": True,
        "status": "healthy",
        "service": "abstractgateway",
        "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

def _env_first(*keys: str, default: Optional[str] = None) -> Optional[str]:
    for key in keys:
        value = os.getenv(str(key))
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def _env_bool(*keys: Any, default: bool = False) -> bool:
    keys0 = list(keys)
    if keys0 and isinstance(keys0[-1], bool):
        default = bool(keys0.pop())
    raw0 = _env_first(*(str(k) for k in keys0))
    if raw0 is None:
        return bool(default)
    raw = str(raw0).strip().lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def _env_float(*keys: str) -> Optional[float]:
    raw = _env_first(*keys)
    if raw is None:
        return None
    try:
        return float(raw)
    except Exception:
        return None


def _discovery_timeout_s(default: float = 1.5) -> float:
    raw = _env_float(
        "ABSTRACTGATEWAY_DISCOVERY_TIMEOUT_S",
        "ABSTRACTCORE_DISCOVERY_TIMEOUT_S",
    )
    value = raw if raw is not None else float(default)
    try:
        value = float(value)
    except Exception:
        value = float(default)
    if value <= 0:
        value = float(default)
    return max(0.2, min(value, 10.0))


def _provider_models_timeout_s(default: float = 30.0) -> float:
    raw = _env_float(
        "ABSTRACTGATEWAY_PROVIDER_MODELS_TIMEOUT_S",
        "ABSTRACTCORE_PROVIDER_MODELS_TIMEOUT_S",
        "ABSTRACTGATEWAY_DISCOVERY_MODEL_TIMEOUT_S",
        "ABSTRACTCORE_DISCOVERY_MODEL_TIMEOUT_S",
    )
    value = raw if raw is not None else float(default)
    try:
        value = float(value)
    except Exception:
        value = float(default)
    if value <= 0:
        value = float(default)
    return max(1.0, min(value, 60.0))


def _resolve_gateway_provider_model_or_400(
    *,
    provider: Optional[str],
    model: Optional[str],
    purpose: str,
) -> tuple[str, str]:
    try:
        return resolve_gateway_provider_model(provider=provider, model=model, purpose=purpose).require()
    except ProviderModelConfigError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _env_is_openai_base_url(value: Optional[str]) -> bool:
    return "api.openai.com" in str(value or "").strip().lower()


def _gateway_direct_image_configured() -> bool:
    if _env_first("ABSTRACTCORE_SERVER_BASE_URL"):
        return True

    backend = str(_env_first("ABSTRACTVISION_BACKEND", "ABSTRACTCORE_VISION_BACKEND", default="openai") or "openai")
    backend = backend.strip().lower().replace("_", "-")

    if backend in {"mlx-gen", "mlxgen", "mflux", "m-flux"}:
        if _env_first("ABSTRACTVISION_MFLUX_MODEL", "ABSTRACTGATEWAY_VISION_MFLUX_MODEL", "ABSTRACTVISION_MODEL_ID"):
            return True
        return _gateway_has_local_mflux_preset("")
    if backend in {"diffusers", "huggingface", "hf", "hf-diffusers"}:
        return True
    if backend in {"sdcpp", "sd-cpp", "stable-diffusion.cpp", "stable-diffusion-cpp"}:
        return bool(_env_first("ABSTRACTVISION_SDCPP_MODEL", "ABSTRACTVISION_SDCPP_DIFFUSION_MODEL"))
    if _gateway_has_local_mflux_preset(""):
        return True

    base_url = _env_first("ABSTRACTVISION_BASE_URL", "OPENAI_BASE_URL")
    api_key = _env_first("ABSTRACTVISION_API_KEY", "OPENAI_API_KEY")
    if base_url:
        if _env_is_openai_base_url(base_url):
            return bool(api_key)
        return True

    # AbstractVision's OpenAI backend defaults to the hosted OpenAI base URL.
    # That endpoint is only usable when an API key is present.
    if backend in {"openai", ""}:
        return bool(api_key)
    return False


def _optional_package_status(module_name: str, dist_name: Optional[str] = None) -> Dict[str, Any]:
    try:
        import importlib.metadata
        import importlib.util

        if importlib.util.find_spec(module_name) is None:
            raise ModuleNotFoundError(f"No module named '{module_name}'")
        version = None
        for candidate in [dist_name, module_name]:
            if not candidate:
                continue
            try:
                version = importlib.metadata.version(candidate)
                break
            except Exception:
                continue
        return {"installed": True, "version": version}
    except Exception as e:
        return {"installed": False, "error": str(e)}


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
    run_lifecycle: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional control-plane lifecycle metadata for this run, exposed as a sanitized run_lifecycle summary.",
    )
    session_id: Optional[str] = Field(
        default=None,
        description="Optional session id to group related runs (e.g. a chat session).",
    )


class StartRunResponse(BaseModel):
    run_id: str


class PurgeDraftRunsRequest(BaseModel):
    """Request a controlled cleanup of ephemeral draft-test run state."""

    model_config = ConfigDict(extra="forbid")

    dry_run: bool = Field(default=True, description="When true, report what would be purged without deleting data.")
    limit: int = Field(default=200, ge=1, le=5000, description="Maximum root runs to scan or purge.")
    session_id: Optional[str] = Field(default=None, description="Optional session id filter.")
    workflow_id: Optional[str] = Field(default=None, description="Optional workflow id filter.")
    run_ids: Optional[List[str]] = Field(default=None, description="Optional exact root run ids to consider.")
    force: bool = Field(default=False, description="Delete matching terminal draft-test runs even when their retention has not expired.")
    include_active: bool = Field(default=False, description="Allow deletion of running/waiting run trees. Defaults off.")
    delete_artifacts: bool = Field(default=True, description="Delete run-associated artifacts and then garbage-collect unreferenced blobs.")
    delete_workspaces: bool = Field(default=True, description="Delete Gateway-owned per-run workspaces under the Gateway data dir.")


# ---------------------------------------------------------------------
# VisualFlow storage + publishing (gateway-first)
# ---------------------------------------------------------------------

class VisualFlowCreateRequest(BaseModel):
    """Create a VisualFlow JSON record (authoring-side)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    interfaces: List[str] = Field(default_factory=list)
    nodes: List[Dict[str, Any]] = Field(default_factory=list)
    edges: List[Dict[str, Any]] = Field(default_factory=list)
    entryNode: Optional[str] = None


class VisualFlowUpdateRequest(BaseModel):
    """Update a VisualFlow JSON record (authoring-side)."""

    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    description: Optional[str] = None
    interfaces: Optional[List[str]] = None
    nodes: Optional[List[Dict[str, Any]]] = None
    edges: Optional[List[Dict[str, Any]]] = None
    entryNode: Optional[str] = None


class VisualFlowCodeSimulateRequest(BaseModel):
    """Run a Python Code-node body through the same Runtime sandbox used by flows."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(default="")
    input: Any = Field(default_factory=dict)
    function_name: str = Field(default="transform")
    permissions: str = Field(default="sandbox")


class PublishVisualFlowRequest(BaseModel):
    bundle_id: Optional[str] = Field(default=None, description="Stable bundle identity (defaults to sanitized flow.name).")
    bundle_version: Optional[str] = Field(default=None, description="Explicit bundle_version (leave empty to auto-bump).")
    overwrite: bool = Field(default=False, description="If true, overwrite an existing bundle_id@version.")
    reload_gateway: bool = Field(default=True, description="If true, reload gateway bundles after publish before returning.")


class PublishVisualFlowResponse(BaseModel):
    ok: bool
    bundle_id: str
    bundle_version: str
    bundle_ref: str
    bundle_path: str
    gateway_reloaded: bool = False
    gateway_reload_error: Optional[str] = None


_VISUALFLOW_ID_RE = re.compile(r"^[a-zA-Z0-9._-]+$")


def _safe_visualflow_id(raw: str) -> str:
    fid = str(raw or "").strip()
    if not fid:
        raise HTTPException(status_code=400, detail="flow_id is required")
    if not _VISUALFLOW_ID_RE.match(fid) or fid in {".", ".."} or "/" in fid or "\\" in fid:
        raise HTTPException(status_code=400, detail="flow_id contains unsafe characters")
    return fid


def _visualflows_dir(*, svc: Any) -> Path:
    raw = os.getenv("ABSTRACTGATEWAY_VISUALFLOWS_DIR")
    base = Path(raw).expanduser().resolve() if isinstance(raw, str) and raw.strip() else Path(svc.config.data_dir) / "visualflows"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create visualflows dir: {e}")
    return base


def _visualflow_path(*, svc: Any, flow_id: str) -> Path:
    fid = _safe_visualflow_id(flow_id)
    return _visualflows_dir(svc=svc) / f"{fid}.json"


def _read_visualflow_json(path: Path) -> Dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load flow file '{path}': {e}")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail=f"Flow file '{path}' is not a JSON object")
    return data


def _write_visualflow_json(path: Path, data: Dict[str, Any]) -> None:
    try:
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        path.write_text(payload, encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write flow file '{path}': {e}")


def _coerce_visualflow(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Validate VisualFlow authoring JSON shape without owning node semantics."""
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="Invalid VisualFlow JSON: root must be an object")

    out = dict(raw)

    flow_id = out.get("id")
    if flow_id is not None and (not isinstance(flow_id, str) or not flow_id.strip()):
        raise HTTPException(status_code=400, detail="Invalid VisualFlow JSON: id must be a non-empty string")

    name = out.get("name")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(status_code=400, detail="Invalid VisualFlow JSON: name is required")

    description = out.get("description")
    if description is not None and not isinstance(description, str):
        raise HTTPException(status_code=400, detail="Invalid VisualFlow JSON: description must be a string")

    interfaces = out.get("interfaces")
    if interfaces is None:
        out["interfaces"] = []
    elif not isinstance(interfaces, list) or any(not isinstance(item, str) for item in interfaces):
        raise HTTPException(status_code=400, detail="Invalid VisualFlow JSON: interfaces must be a list of strings")

    entry_node = out.get("entryNode")
    if entry_node is not None and (not isinstance(entry_node, str) or not entry_node.strip()):
        raise HTTPException(status_code=400, detail="Invalid VisualFlow JSON: entryNode must be a non-empty string")

    nodes = raw.get("nodes")
    if not isinstance(nodes, list):
        raise HTTPException(status_code=400, detail="Invalid VisualFlow JSON: nodes must be a list")
    node_ids: set[str] = set()
    for idx, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise HTTPException(status_code=400, detail=f"Invalid VisualFlow JSON: nodes.{idx} must be an object")
        node_id = node.get("id")
        node_type = node.get("type")
        if not isinstance(node_id, str) or not node_id.strip():
            raise HTTPException(status_code=400, detail=f"Invalid VisualFlow JSON: nodes.{idx}.id is required")
        if node_id in node_ids:
            raise HTTPException(status_code=400, detail=f"Invalid VisualFlow JSON: duplicate node id '{node_id}'")
        node_ids.add(node_id)
        if not isinstance(node_type, str) or not node_type.strip():
            raise HTTPException(status_code=400, detail=f"Invalid VisualFlow JSON: nodes.{idx}.type is required")
        position = node.get("position")
        if position is not None and not isinstance(position, dict):
            raise HTTPException(status_code=400, detail=f"Invalid VisualFlow JSON: nodes.{idx}.position must be an object")
        data = node.get("data")
        if data is not None and not isinstance(data, dict):
            raise HTTPException(status_code=400, detail=f"Invalid VisualFlow JSON: nodes.{idx}.data must be an object")
        for key in ("inputs", "outputs"):
            pins = node.get(key)
            if pins is not None and not isinstance(pins, list):
                raise HTTPException(status_code=400, detail=f"Invalid VisualFlow JSON: nodes.{idx}.{key} must be a list")

    edges = raw.get("edges")
    if not isinstance(edges, list):
        raise HTTPException(status_code=400, detail="Invalid VisualFlow JSON: edges must be a list")
    edge_ids: set[str] = set()
    for idx, edge in enumerate(edges):
        if not isinstance(edge, dict):
            raise HTTPException(status_code=400, detail=f"Invalid VisualFlow JSON: edges.{idx} must be an object")
        edge_id = edge.get("id")
        if edge_id is not None:
            if not isinstance(edge_id, str) or not edge_id.strip():
                raise HTTPException(status_code=400, detail=f"Invalid VisualFlow JSON: edges.{idx}.id must be a non-empty string")
            if edge_id in edge_ids:
                raise HTTPException(status_code=400, detail=f"Invalid VisualFlow JSON: duplicate edge id '{edge_id}'")
            edge_ids.add(edge_id)
        for key in ("source", "target", "sourceHandle", "targetHandle"):
            value = edge.get(key)
            if not isinstance(value, str) or not value.strip():
                raise HTTPException(status_code=400, detail=f"Invalid VisualFlow JSON: edges.{idx}.{key} is required")
        if edge.get("source") not in node_ids:
            raise HTTPException(status_code=400, detail=f"Invalid VisualFlow JSON: edges.{idx}.source references unknown node")
        if edge.get("target") not in node_ids:
            raise HTTPException(status_code=400, detail=f"Invalid VisualFlow JSON: edges.{idx}.target references unknown node")
        animated = edge.get("animated")
        if animated is not None and not isinstance(animated, bool):
            raise HTTPException(status_code=400, detail=f"Invalid VisualFlow JSON: edges.{idx}.animated must be a boolean")

    return out


def _try_parse_semver(v: str) -> Optional[Tuple[int, int, int]]:
    s = str(v or "").strip()
    if not s:
        return None
    parts = [p.strip() for p in s.split(".")]
    if not parts or any(not p for p in parts):
        return None
    nums: list[int] = []
    for p in parts:
        if not p.isdigit():
            return None
        nums.append(int(p))
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def _bump_patch(v: str) -> str:
    sem = _try_parse_semver(v)
    if sem is not None:
        return f"{sem[0]}.{sem[1]}.{sem[2] + 1}"
    s = str(v or "").strip()
    return f"{s}.1" if s else "0.0.1"


def _is_draft_bundle_version(v: Any) -> bool:
    return str(v or "").strip().lower().startswith("draft.")


def _bundle_version_channel(v: Any) -> str:
    return "draft" if _is_draft_bundle_version(v) else "published"


def _latest_version(versions: list[Dict[str, str]]) -> Optional[str]:
    if not versions:
        return None
    vers = [str(v.get("bundle_version") or "").strip() for v in versions if str(v.get("bundle_version") or "").strip()]
    if not vers:
        return None
    if all(_try_parse_semver(v) is not None for v in vers):
        return max(vers, key=lambda x: _try_parse_semver(x) or (0, 0, 0))
    return max(versions, key=lambda x: (str(x.get("created_at") or ""), str(x.get("bundle_version") or ""))).get("bundle_version")


def _origin_version(versions: list[Dict[str, str]]) -> Optional[str]:
    if not versions:
        return None
    return min(versions, key=lambda x: (str(x.get("created_at") or ""), str(x.get("bundle_version") or ""))).get("bundle_version")


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
    provider: Optional[str] = Field(default=None, description="Optional provider override (default: gateway provider).")
    model: Optional[str] = Field(default=None, description="Optional model override (default: gateway model).")
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
    provider: Optional[str] = Field(default=None, description="Optional provider override (must match the execution-host embedding.text route).")
    model: Optional[str] = Field(default=None, description="Optional model override (must match the execution-host embedding.text route).")


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
    provider: Optional[str] = Field(default=None, description="Optional provider override (default: gateway provider).")
    model: Optional[str] = Field(default=None, description="Optional model override (default: gateway model).")
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


class SaveChatThreadRequest(BaseModel):
    provider: Optional[str] = Field(default=None, description="Optional provider override (default: gateway provider).")
    model: Optional[str] = Field(default=None, description="Optional model override (default: gateway model).")
    include_subruns: bool = Field(default=True, description="Include child/subworkflow runs in the chat context.")
    messages: list[Dict[str, Any]] = Field(default_factory=list, description="Chat messages to persist (role/content/ts).")
    title: Optional[str] = Field(default=None, description="Optional thread title (UI label).")


class SaveChatThreadResponse(BaseModel):
    ok: bool = Field(default=True)
    run_id: str
    workflow_id: str
    thread_id: str
    created_at: str
    duplicate: bool = Field(default=False, description="True if an identical chat thread was already saved (no-op).")
    title: Optional[str] = None
    message_count: int = 0
    chat_artifact: Dict[str, Any]


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
    store: Optional[Dict[str, Any]] = None


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


def _json_schema_for_visualflow_pin_type(pin_type: str) -> Dict[str, Any]:
    """Return a small JSON Schema fragment for a VisualFlow pin type."""
    raw = str(pin_type or "").strip()
    t = raw.lower()
    schema: Dict[str, Any] = {}
    if t in {
        "string",
        "str",
        "text",
        "markdown",
        "path",
        "file",
        "directory",
        "provider",
        "model",
        "provider_text",
        "model_text",
        "provider_image",
        "model_image",
        "provider_voice",
        "model_voice",
    }:
        schema["type"] = "string"
    elif t in {"number", "float", "double"}:
        schema["type"] = "number"
    elif t in {"integer", "int"}:
        schema["type"] = "integer"
    elif t in {"boolean", "bool"}:
        schema["type"] = "boolean"
    elif t in {"array", "list"}:
        schema["type"] = "array"
    elif t in {"object", "dict", "map", "json"}:
        schema["type"] = "object"

    if raw:
        schema["x-abstract-type"] = raw
    return schema


def _entrypoint_input_schema_from_visualflow(raw: Any) -> Dict[str, Any]:
    """Build the versioned run input schema contract for a VisualFlow entrypoint."""
    inputs = _extract_entrypoint_inputs_from_visualflow(raw)
    properties: Dict[str, Any] = {}
    required: list[str] = []
    defaults: Dict[str, Any] = {}
    pins: list[Dict[str, Any]] = []

    for item in inputs:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("id") or "").strip()
        if not pid:
            continue
        label = str(item.get("label") or pid).strip() or pid
        pin_type = str(item.get("type") or "unknown").strip() or "unknown"
        has_default = "default" in item

        prop = _json_schema_for_visualflow_pin_type(pin_type)
        if label and label != pid:
            prop["title"] = label
        if has_default:
            prop["default"] = item.get("default")
            defaults[pid] = item.get("default")
        else:
            required.append(pid)
        properties[pid] = prop

        pin = dict(item)
        pin["required"] = not has_default
        pin["schema"] = dict(prop)
        pins.append(pin)

    schema: Dict[str, Any] = {
        "type": "object",
        "additionalProperties": True,
        "properties": properties,
    }
    if required:
        schema["required"] = required

    return {
        "version": 1,
        "inputs": pins,
        "defaults": defaults,
        "input_data_schema": schema,
    }


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
    # Always allow gateway-owned per-run workspaces (even when the data_dir is outside the
    # operator workspace root). This enables safe follow-ups to reuse the same workspace.
    try:
        data_dir_raw = str(os.getenv("ABSTRACTGATEWAY_DATA_DIR") or os.getenv("ABSTRACTFLOW_RUNTIME_DIR") or "").strip()
        if data_dir_raw:
            data_dir = Path(data_dir_raw).expanduser().resolve()
            allowed_roots.append(data_dir / "workspaces")
    except Exception:
        pass
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


def _normalize_run_context_media(input_data: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort normalize attachment/media inputs into `input_data.context.attachments`.

    Rationale:
    - AbstractAgent ReAct adapter expects `context.attachments` (or legacy `context.media`)
      to feed `payload.media` into LLM_CALL effects.
    - Some clients attach media under `input_data.attachments` or per-message fields; lift
      these into the canonical place to enable image fallback / media handling.
    """

    def _norm_items(raw: Any) -> list[Any]:
        items = list(raw) if isinstance(raw, (list, tuple)) else None
        if not items:
            return []
        out: list[Any] = []
        for item in items:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
                continue
            if isinstance(item, dict):
                aid = item.get("$artifact")
                if not (isinstance(aid, str) and aid.strip()):
                    aid = item.get("artifact_id")
                if isinstance(aid, str) and aid.strip():
                    out.append(dict(item))
        return out

    ctx0 = input_data.get("context")
    ctx = dict(ctx0) if isinstance(ctx0, dict) else None

    # Collect media candidates from common shapes.
    merged: list[Any] = []
    if ctx is not None:
        merged.extend(_norm_items(ctx.get("attachments")))
        merged.extend(_norm_items(ctx.get("media")))
        msgs = ctx.get("messages")
        if isinstance(msgs, list):
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                merged.extend(_norm_items(m.get("attachments")))
                merged.extend(_norm_items(m.get("media")))

    merged.extend(_norm_items(input_data.get("attachments")))
    merged.extend(_norm_items(input_data.get("media")))

    if not merged:
        return input_data

    def _key(it: Any) -> Optional[Tuple[str, str]]:
        if isinstance(it, str):
            s = it.strip()
            return ("path", s) if s else None
        if isinstance(it, dict):
            aid = it.get("$artifact") or it.get("artifact_id")
            if isinstance(aid, str) and aid.strip():
                return ("artifact", aid.strip())
        return None

    deduped: list[Any] = []
    seen: set[Tuple[str, str]] = set()
    for it in merged:
        k = _key(it)
        if k is None or k in seen:
            continue
        deduped.append(dict(it) if isinstance(it, dict) else it)
        seen.add(k)

    if ctx is None:
        ctx = {}
    # Prefer canonical key; keep legacy `context.media` untouched for back-compat.
    ctx.setdefault("attachments", deduped)
    # If canonical key exists but was empty/invalid, overwrite with normalized.
    if not _norm_items(ctx.get("attachments")):
        ctx["attachments"] = deduped
    input_data["context"] = ctx
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
    ignore = AbstractIgnore.for_path(root)

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


@router.get("/visualflows")
async def list_visualflows() -> List[Dict[str, Any]]:
    """List VisualFlow JSON records stored in the gateway."""
    svc = get_gateway_service()
    base = _visualflows_dir(svc=svc)
    items: list[Dict[str, Any]] = []
    for p in sorted(base.glob("*.json")):
        if not p.is_file():
            continue
        try:
            data = _read_visualflow_json(p)
        except HTTPException as e:
            logger.warning("Skipping invalid visualflow file %s: %s", p, e.detail)
            continue
        if not isinstance(data.get("id"), str) or not str(data.get("id") or "").strip():
            data["id"] = p.stem
        items.append(data)
    return items


@router.post("/visualflows/code/simulate")
async def simulate_visualflow_code(req: VisualFlowCodeSimulateRequest) -> Dict[str, Any]:
    """Execute Code-node Python in the Runtime sandbox without starting a flow run."""
    function_name = str(req.function_name or "transform").strip() or "transform"
    try:
        from abstractruntime.visualflow_compiler.visual.code_executor import CodeExecutionError, create_code_handler, normalize_code_permissions
        from abstractruntime.visualflow_compiler.visual.execution_metrics import capture_execution_start, finish_execution_metrics

        execution_start = capture_execution_start()
        effective_permissions = str(req.permissions or "sandbox")
        handler_created = False
        try:
            effective_permissions = normalize_code_permissions(req.permissions)
            handler = create_code_handler(str(req.code or ""), function_name, permissions=req.permissions)
            handler_created = True
            output = handler(req.input)
        except CodeExecutionError as e:
            execution = finish_execution_metrics(execution_start)
            execution["permissions"] = effective_permissions
            return {
                "ok": False,
                "success": False,
                "output": None,
                "execution": execution,
                "error": str(e),
                "diagnostics": {
                    "source": "abstractruntime.code_executor",
                    "phase": "runtime" if handler_created else "compile",
                    "requested_mode": req.permissions,
                    "effective_mode": effective_permissions,
                    "allowed": False,
                },
            }
        execution = finish_execution_metrics(execution_start)
        execution["permissions"] = effective_permissions
        return {
            "ok": True,
            "success": True,
            "output": output,
            "execution": execution,
            "error": None,
            "diagnostics": {
                "source": "abstractruntime.code_executor",
                "phase": "runtime",
                "requested_mode": req.permissions,
                "effective_mode": effective_permissions,
                "allowed": True,
            },
        }
    except Exception as e:
        return {
            "ok": False,
            "success": False,
            "output": None,
            "execution": None,
            "error": str(e),
            "diagnostics": {
                "source": "abstractruntime.code_executor",
                "phase": "compile",
                "requested_mode": req.permissions,
                "effective_mode": req.permissions,
                "allowed": False,
            },
        }


@router.post("/visualflows")
async def create_visualflow(req: VisualFlowCreateRequest) -> Dict[str, Any]:
    """Create a new VisualFlow JSON record."""
    svc = get_gateway_service()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    flow_id = str(uuid.uuid4())[:8]
    raw: Dict[str, Any] = {
        "id": flow_id,
        "name": req.name,
        "description": req.description,
        "interfaces": list(req.interfaces or []),
        "nodes": list(req.nodes or []),
        "edges": list(req.edges or []),
        "entryNode": req.entryNode,
        "created_at": now,
        "updated_at": now,
    }
    data = _coerce_visualflow(raw)
    data["id"] = flow_id
    data.setdefault("created_at", now)
    data["updated_at"] = now
    path = _visualflow_path(svc=svc, flow_id=flow_id)
    _write_visualflow_json(path, data)
    return data


@router.get("/visualflows/{flow_id}")
async def get_visualflow(flow_id: str) -> Dict[str, Any]:
    """Fetch a VisualFlow JSON record by id."""
    svc = get_gateway_service()
    path = _visualflow_path(svc=svc, flow_id=flow_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Flow '{flow_id}' not found")
    data = _read_visualflow_json(path)
    data["id"] = str(flow_id)
    return data


@router.put("/visualflows/{flow_id}")
async def update_visualflow(flow_id: str, req: VisualFlowUpdateRequest) -> Dict[str, Any]:
    """Update an existing VisualFlow JSON record."""
    svc = get_gateway_service()
    path = _visualflow_path(svc=svc, flow_id=flow_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Flow '{flow_id}' not found")
    raw = _read_visualflow_json(path)
    if req.name is not None:
        raw["name"] = req.name
    if req.description is not None:
        raw["description"] = req.description
    if req.interfaces is not None:
        raw["interfaces"] = list(req.interfaces or [])
    if req.nodes is not None:
        raw["nodes"] = list(req.nodes or [])
    if req.edges is not None:
        raw["edges"] = list(req.edges or [])
    if req.entryNode is not None:
        raw["entryNode"] = req.entryNode
    raw["id"] = str(flow_id)
    raw["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    data = _coerce_visualflow(raw)
    data["id"] = str(flow_id)
    data.setdefault("created_at", raw.get("created_at"))
    data["updated_at"] = raw["updated_at"]
    _write_visualflow_json(path, data)
    return data


@router.delete("/visualflows/{flow_id}")
async def delete_visualflow(flow_id: str) -> Dict[str, Any]:
    """Delete a VisualFlow JSON record."""
    svc = get_gateway_service()
    path = _visualflow_path(svc=svc, flow_id=flow_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Flow '{flow_id}' not found")
    try:
        path.unlink()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete flow file: {e}")
    return {"status": "deleted", "id": str(flow_id)}


@router.post("/visualflows/{flow_id}/publish", response_model=PublishVisualFlowResponse)
async def publish_visualflow(flow_id: str, req: PublishVisualFlowRequest) -> PublishVisualFlowResponse:
    """Compile a VisualFlow into a WorkflowBundle and install it into the gateway."""
    svc = get_gateway_service()
    host = _require_bundle_host(svc)
    path = _visualflow_path(svc=svc, flow_id=flow_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Flow '{flow_id}' not found")
    flow = _read_visualflow_json(path)

    try:
        from abstractruntime.workflow_bundle import WorkflowBundleRegistry, WorkflowBundleRegistryError
        from abstractruntime.workflow_bundle.packer import pack_workflow_bundle
        from abstractruntime.workflow_bundle.registry import sanitize_bundle_id
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail=f"WorkflowBundle tooling unavailable: {e}")

    bundle_id = sanitize_bundle_id(str(req.bundle_id or "").strip()) or sanitize_bundle_id(str(flow.get("name") or "").strip()) or str(flow_id)
    if not bundle_id:
        raise HTTPException(status_code=400, detail="bundle_id is required")

    reg = WorkflowBundleRegistry(getattr(host, "bundles_dir", None))
    existing: list[Dict[str, str]] = []
    try:
        for b in reg.scan():
            if getattr(b, "bundle_id", None) == bundle_id:
                created = str(getattr(getattr(b, "manifest", None), "created_at", "") or "")
                existing.append({"bundle_version": str(getattr(b, "bundle_version", "") or ""), "created_at": created})
    except Exception:
        existing = []

    requested_ver = str(req.bundle_version or "").strip() if isinstance(req.bundle_version, str) and str(req.bundle_version).strip() else ""
    durable_existing = [v for v in existing if not _is_draft_bundle_version(v.get("bundle_version"))]
    if requested_ver:
        if not req.overwrite and any(str(v.get("bundle_version") or "").strip() == requested_ver for v in existing):
            raise HTTPException(status_code=400, detail=f"bundle_version '{requested_ver}' already exists for bundle '{bundle_id}'")
        new_ver = requested_ver
    else:
        prev = _latest_version(durable_existing)
        new_ver = "0.0.0" if not prev else _bump_patch(prev)
        if not req.overwrite and any(str(v.get("bundle_version") or "").strip() == new_ver for v in durable_existing):
            raise HTTPException(status_code=400, detail=f"bundle_version '{new_ver}' already exists for bundle '{bundle_id}'")

    published_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    is_draft_bundle = _is_draft_bundle_version(new_ver)
    lineage_existing = existing if is_draft_bundle else durable_existing
    previous_version = _latest_version(lineage_existing)
    origin = _origin_version(lineage_existing) or previous_version
    metadata: Dict[str, Any] = {
        "lifecycle": {
            "channel": _bundle_version_channel(new_ver),
            "source": "abstractflow.editor",
        },
        "publisher": {"host": "abstractgateway", "published_at": published_at},
        "source": {"root_flow_id": str(flow.get("id") or flow_id), "root_flow_name": str(flow.get("name") or ""), "root_flow_updated_at": str(flow.get("updated_at") or "")},
        "lineage": {
            "bundle_id": bundle_id,
            "bundle_version": new_ver,
            "origin_bundle_version": str(origin or new_ver),
            **({"previous_bundle_version": str(previous_version)} if previous_version else {}),
        },
    }

    tmp_dir = (Path(svc.config.data_dir) / "tmp").resolve()
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create tmp dir: {e}")

    fd, tmp_path = tempfile.mkstemp(prefix="visualflow_", suffix=".flow", dir=str(tmp_dir))
    os.close(fd)
    tmp = Path(tmp_path)
    try:
        pack_workflow_bundle(
            root_flow_json=path,
            out_path=tmp,
            bundle_id=bundle_id,
            bundle_version=new_ver,
            flows_dir=_visualflows_dir(svc=svc),
            metadata=metadata,
        )
        installed = reg.install(tmp, overwrite=bool(req.overwrite))
    except WorkflowBundleRegistryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to publish bundle: {e}")
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass

    gateway_reloaded = False
    if bool(req.reload_gateway):
        reload_fn = getattr(host, "reload_bundles_from_disk", None)
        if not callable(reload_fn):
            raise HTTPException(status_code=503, detail="Bundle reload is not supported on this gateway host")
        try:
            reload_fn()
            gateway_reloaded = True
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Failed to reload bundles after publish: {e}")
    else:
        gateway_reloaded = False

    return PublishVisualFlowResponse(
        ok=True,
        bundle_id=str(installed.bundle_id),
        bundle_version=str(installed.bundle_version),
        bundle_ref=f"{installed.bundle_id}@{installed.bundle_version}",
        bundle_path=str(installed.path),
        gateway_reloaded=bool(gateway_reloaded),
        gateway_reload_error=None,
    )


@router.get("/bundles")
async def list_bundles(
    all_versions: bool = Query(default=False, description="If true, return one item per bundle version."),
    include_drafts: bool = Query(default=False, description="If true, include draft bundle versions."),
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
        version_rows: list[Dict[str, str]] = []
        for ver0, bundle0 in versions.items():
            ver = str(ver0 or "").strip() if isinstance(ver0, str) else ""
            if not ver:
                continue
            man0 = getattr(bundle0, "manifest", None) if bundle0 is not None else None
            created = str(getattr(man0, "created_at", "") or "")
            version_rows.append({"bundle_version": ver, "created_at": created})
        published_rows = [row for row in version_rows if not _is_draft_bundle_version(row.get("bundle_version"))]
        latest_any_version = _latest_version(version_rows) or ""
        latest_published_version = _latest_version(published_rows) or ""

        selected_versions: list[str]
        if all_versions:
            selected_versions = [
                str(v)
                for v in versions.keys()
                if isinstance(v, str) and (include_drafts or not _is_draft_bundle_version(v))
            ]
        else:
            v0 = latest_published_version or (latest_any_version if include_drafts else "")
            if not v0:
                v0 = latest.get(str(bid))
            v = str(v0).strip() if isinstance(v0, str) and str(v0).strip() else ""
            if v and not include_drafts and _is_draft_bundle_version(v):
                v = ""
            if not v:
                selected_versions = []
            else:
                selected_versions = [v]

        for ver in selected_versions:
            b = versions.get(ver)
            man = getattr(b, "manifest", None) if b is not None else None
            if man is None:
                continue
            if not include_drafts and _is_draft_bundle_version(ver):
                continue
            exact_version = str(getattr(man, "bundle_version", ver) or ver)
            version_channel = _bundle_version_channel(exact_version)
            metadata = getattr(man, "metadata", None)
            metadata_obj = metadata if isinstance(metadata, dict) else {}
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
                        "workflow_id": f"{bid}@{exact_version}:{fid}",
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
                    "bundle_version": exact_version,
                    "bundle_ref": f"{bid}@{exact_version}",
                    "version_channel": version_channel,
                    "is_draft": version_channel == "draft",
                    "is_published": version_channel == "published",
                    "latest_published_version": latest_published_version or None,
                    "latest_any_version": latest_any_version or None,
                    "created_at": getattr(man, "created_at", ""),
                    "metadata": metadata_obj,
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
    bid_base, selected_ver, fid, raw = _resolve_bundle_flow_json(
        host=host,
        bundle_id=bundle_id,
        flow_id=flow_id,
        bundle_version=bundle_version,
    )

    return {
        "bundle_id": bid_base,
        "bundle_version": selected_ver,
        "bundle_ref": f"{bid_base}@{selected_ver}",
        "flow_id": fid,
        "workflow_id": f"{bid_base}@{selected_ver}:{fid}",
        "flow": raw,
    }


def _resolve_bundle_flow_json(
    *,
    host: Any,
    bundle_id: str,
    flow_id: str,
    bundle_version: Optional[str] = None,
) -> Tuple[str, str, str, Dict[str, Any]]:
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

    return bid_base, str(selected_ver), fid, raw


@router.get("/bundles/{bundle_id}/flows/{flow_id}/input_schema")
async def get_bundle_flow_input_schema(
    bundle_id: str,
    flow_id: str,
    bundle_version: Optional[str] = Query(default=None, description="Optional bundle version (defaults to latest)."),
) -> Dict[str, Any]:
    """Return a stable Run Flow input schema for a bundled VisualFlow entrypoint."""
    svc = get_gateway_service()
    host = _require_bundle_host(svc)
    bid_base, selected_ver, fid, raw = _resolve_bundle_flow_json(
        host=host,
        bundle_id=bundle_id,
        flow_id=flow_id,
        bundle_version=bundle_version,
    )
    schema = _entrypoint_input_schema_from_visualflow(raw)
    return {
        "bundle_id": bid_base,
        "bundle_version": selected_ver,
        "bundle_ref": f"{bid_base}@{selected_ver}",
        "flow_id": fid,
        "workflow_id": f"{bid_base}@{selected_ver}:{fid}",
        **schema,
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
    bundle_ref_version = _split_bundle_ref(bundle_id or "")[1] if bundle_id else None
    flow_ref_version = None
    parsed_flow_ref = _parse_namespaced_workflow_id(flow_id) if ":" in flow_id else None
    if parsed_flow_ref:
        flow_ref_version = _split_bundle_ref(parsed_flow_ref[0])[1]
    requested_bundle_version = bundle_version or bundle_ref_version or flow_ref_version
    if _is_draft_bundle_version(requested_bundle_version):
        lifecycle_purpose = (
            str((req.run_lifecycle or {}).get("purpose") or "").strip()
            if isinstance(req.run_lifecycle, dict)
            else ""
        )
        if lifecycle_purpose != "draft_test":
            raise HTTPException(status_code=400, detail="draft bundle versions require run_lifecycle.purpose='draft_test'")

    try:
        session_id = str(req.session_id).strip() if isinstance(req.session_id, str) and str(req.session_id).strip() else None
        input_data = dict(req.input_data or {})
        input_data = _sanitize_run_workspace_policy(input_data)
        input_data = _normalize_run_context_media(input_data)
        if isinstance(req.run_lifecycle, dict):
            input_data["_run_lifecycle"] = dict(req.run_lifecycle)
        normalize_run_lifecycle_vars(input_data)
        # Default workspace_root behavior (cross-client):
        # - If omitted, create a per-run workspace under the gateway data_dir.
        # - Clients may override only within the operator-configured server workspace roots.
        raw_ws = input_data.get("workspace_root")
        gateway_owned_workspace: Optional[Path] = None
        if not (isinstance(raw_ws, str) and raw_ws.strip()):
            base = Path(svc.config.data_dir) / "workspaces"
            base.mkdir(parents=True, exist_ok=True)
            ws_dir = base / uuid.uuid4().hex
            ws_dir.mkdir(parents=True, exist_ok=True)
            input_data["workspace_root"] = str(ws_dir)
            gateway_owned_workspace = ws_dir

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
        if gateway_owned_workspace is not None:
            try:
                write_gateway_workspace_marker(gateway_owned_workspace, run_id=str(run_id))
            except Exception as e:
                logger.warning("Failed to write gateway workspace ownership marker", extra={"error": str(e)})
    except WorkflowDeprecatedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except KeyError as e:
        # Best-effort error message: in bundle mode, KeyError can refer to either a bundle or a flow.
        msg = str(e).strip()
        if msg and msg[0] in {"'", '"'} and msg[-1:] == msg[0]:
            msg = msg[1:-1]
        if not msg:
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


@router.post("/runs/purge_drafts")
async def purge_draft_runs(req: PurgeDraftRunsRequest) -> Dict[str, Any]:
    """Purge terminal ephemeral draft-test run trees.

    This is intentionally Gateway-owned: Runtime provides store-level deletion,
    while Gateway owns the draft-test taxonomy and workspace/artifact cleanup policy.
    """

    try:
        return purge_ephemeral_draft_runs(
            get_gateway_service(),
            DraftRunPurgeOptions(
                dry_run=bool(req.dry_run),
                limit=int(req.limit),
                session_id=req.session_id,
                workflow_id=req.workflow_id,
                run_ids=list(req.run_ids or []) if req.run_ids is not None else None,
                force=bool(req.force),
                include_active=bool(req.include_active),
                delete_artifacts=bool(req.delete_artifacts),
                delete_workspaces=bool(req.delete_workspaces),
            ),
        )
    except DraftRunPurgeUnsupported as e:
        raise HTTPException(status_code=501, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to purge draft runs: {e}")


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
    include_drafts: bool = Query(False, description="If true, include private draft-test runs in the response."),
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
            "run_lifecycle": row.get("run_lifecycle") if isinstance(row.get("run_lifecycle"), dict) else None,
            "is_draft": is_draft_run_lifecycle(row.get("run_lifecycle")),
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
            internal_limit = max(200, int(limit) * 5) if (bool(root_only) or sid or filter_internal or not include_drafts) else int(limit)
            rows = rs.list_run_index(status=status_enum, workflow_id=wid, session_id=sid, root_only=bool(root_only), limit=internal_limit)
            for row in rows or []:
                wf_id = str(row.get("workflow_id") or "").strip()
                if filter_internal and wf_id.startswith("__"):
                    continue
                if bool(root_only) and str(row.get("parent_run_id") or "").strip():
                    continue
                summary = _summary_from_index_row(row)
                if not include_drafts and bool(summary.get("is_draft") is True):
                    continue
                items.append(summary)
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
            summary = run_summary(r)
            if not include_drafts and bool(summary.get("is_draft") is True):
                continue
            items.append(summary)
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

        def _coerce_ledger_count(raw: Any) -> Optional[int]:
            try:
                return int(raw)
            except Exception:
                return None

        def _ledger_len_from_store(rid: str) -> Optional[int]:
            count_fn = getattr(ledger_store, "count", None)
            if callable(count_fn):
                direct = _coerce_ledger_count(count_fn(rid))
                if direct is not None:
                    return direct
            try:
                records = ledger_store.list(rid)
            except Exception:
                return None
            return int(len(records) if isinstance(records, list) else 0)

        for item in items:
            rid = str(item.get("run_id") or "").strip()
            if not rid:
                continue
            if rid in counts:
                ledger_len = _coerce_ledger_count(counts.get(rid))
                if ledger_len is None:
                    ledger_len = _ledger_len_from_store(rid)
            else:
                ledger_len = _ledger_len_from_store(rid)

            status0 = str(item.get("status") or "").strip().lower()
            if ledger_len == 0 and status0 in {"completed", "failed", "cancelled"}:
                # Defensive fallback: some older/newer LedgerStore implementations
                # can return 0 for terminal runs even when ledger rows are present.
                try:
                    ledger_entries = ledger_store.list(rid)
                    if isinstance(ledger_entries, list):
                        ledger_len = int(len(ledger_entries))
                except Exception:
                    pass
            item["ledger_len"] = ledger_len

    return {"items": items}


@router.get("/runs/{run_id}/input_data")
async def get_run_input_data(run_id: str) -> Dict[str, Any]:
    """Return a best-effort view of the original start inputs for a run.

    Security note:
    - This endpoint is protected by the same gateway auth layer as other read endpoints.
    - We avoid returning full `run.vars`; when possible (bundle mode), we filter to the entrypoint pin ids.
    """

    def _public_run_workspace_defaults(vars_obj: Dict[str, Any]) -> Dict[str, Any]:
        """Return minimal workspace knobs needed to continue a run (e.g. Follow Up).

        We prefer relative paths when they are under the operator workspace root to avoid
        leaking absolute server paths to thin clients.
        """

        def _maybe_rel(raw: str) -> str:
            text = str(raw or "").strip()
            if not text:
                return ""
            try:
                p = Path(text).expanduser()
            except Exception:
                return text
            if not p.is_absolute():
                return text
            try:
                resolved = p.resolve()
            except Exception:
                resolved = p
            try:
                base = _workspace_root()
                rel = resolved.relative_to(base)
                rel_s = rel.as_posix()
                return rel_s if rel_s else "."
            except Exception:
                return str(resolved)

        out: Dict[str, Any] = {}
        for key in ("workspace_root", "workspace_access_mode", "workspace_allowed_paths", "workspace_ignored_paths"):
            v = vars_obj.get(key)
            if v is None:
                continue
            if isinstance(v, str):
                s = v.strip()
                if not s:
                    continue
                if key == "workspace_root":
                    out[key] = _maybe_rel(s)
                    continue
                if key == "workspace_access_mode":
                    out[key] = s
                    continue
                # allowed/ignored: normalize per-line to keep follow-ups safe + stable
                lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
                if not lines:
                    continue
                out[key] = "\n".join([_maybe_rel(ln) for ln in lines])
                continue
            if isinstance(v, list):
                items: list[str] = []
                for x in v:
                    sx = str(x or "").strip()
                    if not sx:
                        continue
                    items.append(_maybe_rel(sx) if key != "workspace_access_mode" else sx)
                if items:
                    out[key] = items
                continue
            # Best-effort passthrough for unexpected shapes.
            out[key] = v

        return out

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
    workspace_defaults = _public_run_workspace_defaults(vars_obj)

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
                            "workspace": workspace_defaults,
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
                    "workspace": workspace_defaults,
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
                    "workspace": workspace_defaults,
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
        "workspace": workspace_defaults,
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

    provider, model = _resolve_gateway_provider_model_or_400(
        provider=req.provider,
        model=req.model,
        purpose="run summary generation",
    )

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

    provider, model = _resolve_gateway_provider_model_or_400(
        provider=req.provider,
        model=req.model,
        purpose="run chat generation",
    )
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


@router.post("/runs/{run_id}/chat_threads", response_model=SaveChatThreadResponse)
async def save_chat_thread(run_id: str, req: SaveChatThreadRequest) -> SaveChatThreadResponse:
    """Persist a full observer chat thread as an artifact + durable ledger event."""
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

    store = getattr(getattr(svc, "stores", None), "artifact_store", None)
    store_fn = getattr(store, "store", None)
    if not callable(store_fn):
        raise HTTPException(status_code=500, detail="Artifact store is not available")

    workflow_id = str(getattr(run, "workflow_id", "") or "")
    include_subruns = bool(getattr(req, "include_subruns", True))

    provider, model = _resolve_gateway_provider_model_or_400(
        provider=req.provider,
        model=req.model,
        purpose="chat thread persistence",
    )

    created_at = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    thread_id = str(uuid.uuid4())

    def _derive_title(items: list[Dict[str, Any]]) -> str:
        for m in items or []:
            if not isinstance(m, dict):
                continue
            if str(m.get("role") or "") != "user":
                continue
            c = m.get("content")
            if isinstance(c, str) and c.strip():
                return _clamp_text(c.strip().replace("\n", " "), max_len=80)
        return ""

    title = str(getattr(req, "title", "") or "").strip() or _derive_title(list(req.messages or []))
    if not title:
        title = None

    messages_in = list(req.messages or [])
    messages_out: list[Dict[str, Any]] = []
    for m in messages_in:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "").strip()
        content = m.get("content")
        if not isinstance(content, str):
            continue
        text = content.strip()
        if not text:
            continue
        ts = m.get("ts")
        msg: Dict[str, Any] = {"role": role or "user", "content": text}
        if isinstance(ts, str) and ts.strip():
            msg["ts"] = ts.strip()
        messages_out.append(msg)

    # Keep a hard cap to avoid unbounded storage / memory pressure.
    max_bytes_raw = str(os.getenv("ABSTRACTGATEWAY_MAX_CHAT_THREAD_BYTES", "") or "").strip()
    try:
        max_bytes = int(max_bytes_raw) if max_bytes_raw else 750_000
    except Exception:
        max_bytes = 750_000
    if max_bytes <= 0:
        max_bytes = 750_000

    doc: Dict[str, Any] = {
        "schema": "abstract.chat.thread.v0",
        "thread_id": thread_id,
        "created_at": created_at,
        "workflow_id": workflow_id,
        "run_id": rid,
        "provider": provider,
        "model": model,
        "include_subruns": include_subruns,
        "title": title,
        "messages": messages_out,
    }
    try:
        blob = json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to encode chat thread: {e}")
    if len(blob) > int(max_bytes):
        raise HTTPException(status_code=413, detail=f"Chat thread too large ({len(blob)} bytes > {max_bytes} bytes)")

    sha256 = hashlib.sha256(blob).hexdigest()

    # Best-effort idempotency: if the exact same serialized thread was already saved for this run,
    # return the existing artifact/thread id instead of creating duplicates (common double-click UX).
    try:
        list_fn = getattr(store, "list_by_run", None)
        if callable(list_fn):
            existing_meta = None
            existing_tags: Dict[str, Any] = {}
            for m in list_fn(rid) or []:
                try:
                    aid0 = str(getattr(m, "artifact_id", "") or "").strip()
                    tags0 = getattr(m, "tags", None)
                    tags2 = dict(tags0 or {}) if isinstance(tags0, dict) else {}
                    if not aid0:
                        continue
                    if str(tags2.get("kind") or "") != "chat_thread":
                        continue
                    if str(tags2.get("sha256") or "") != sha256:
                        continue
                    existing_meta = m
                    existing_tags = tags2
                    break
                except Exception:
                    continue

            if existing_meta is not None:
                artifact_id = str(getattr(existing_meta, "artifact_id", "") or "").strip()
                if artifact_id:
                    thread_id2 = str(existing_tags.get("thread_id") or "").strip() or thread_id
                    created_at2 = str(existing_tags.get("created_at") or "").strip() or created_at
                    title2 = str(existing_tags.get("title") or "").strip() or title or ""
                    if not title2:
                        title2 = None
                    try:
                        mc2 = int(existing_tags.get("message_count") or len(messages_out))
                    except Exception:
                        mc2 = int(len(messages_out))

                    chat_artifact: Dict[str, Any] = {
                        "$artifact": artifact_id,
                        "content_type": "application/json; charset=utf-8",
                        "filename": "chat_thread.json",
                        "sha256": sha256,
                        "size_bytes": len(blob),
                    }

                    # Ensure a durable ledger event exists (handles rare "artifact stored but event failed" cases).
                    found_event = False
                    try:
                        ledger = svc.host.ledger_store.list(rid)
                        if not isinstance(ledger, list):
                            ledger = []
                        tail = ledger[-2500:] if len(ledger) > 2500 else ledger
                        for rec0 in reversed(tail):
                            if not isinstance(rec0, dict):
                                continue
                            eff0 = rec0.get("effect")
                            if not isinstance(eff0, dict) or str(eff0.get("type") or "") != "emit_event":
                                continue
                            p0 = eff0.get("payload")
                            if not isinstance(p0, dict) or str(p0.get("name") or "") != "abstract.chat.thread":
                                continue
                            pay0 = p0.get("payload")
                            if not isinstance(pay0, dict):
                                continue
                            if str(pay0.get("thread_id") or "") == thread_id2:
                                found_event = True
                                break
                            art0 = pay0.get("chat_artifact")
                            if isinstance(art0, dict) and str(art0.get("$artifact") or "") == artifact_id:
                                found_event = True
                                break
                    except Exception:
                        found_event = False

                    if not found_event:
                        payload = {
                            "thread_id": thread_id2,
                            "created_at": created_at2,
                            "workflow_id": workflow_id,
                            "run_id": rid,
                            "provider": provider,
                            "model": model,
                            "include_subruns": include_subruns,
                            "title": title2,
                            "message_count": mc2,
                            "chat_artifact": chat_artifact,
                        }
                        eff = Effect(type=EffectType.EMIT_EVENT, payload={"name": "abstract.chat.thread", "scope": "run", "run_id": rid, "payload": payload})
                        rec = StepRecord.start(run=run, node_id="observer", effect=eff, idempotency_key=f"observer:chat_thread:{thread_id2}")
                        rec.finish_success({"emitted": True, "name": "abstract.chat.thread", "payload": payload})
                        svc.host.ledger_store.append(rec)

                    return SaveChatThreadResponse(
                        ok=True,
                        run_id=rid,
                        workflow_id=workflow_id,
                        thread_id=thread_id2,
                        created_at=created_at2,
                        duplicate=True,
                        title=title2,
                        message_count=mc2,
                        chat_artifact=chat_artifact,
                    )
    except HTTPException:
        raise
    except Exception:
        pass
    tags: Dict[str, str] = {
        "kind": "chat_thread",
        "target": "observer",
        "thread_id": thread_id,
        "workflow_id": workflow_id,
        "source_run_id": rid,
        "created_at": created_at,
        "provider": provider,
        "model": model,
        "include_subruns": "true" if include_subruns else "false",
        "message_count": str(len(messages_out)),
        "sha256": sha256,
    }
    if title:
        tags["title"] = _clamp_text(str(title), max_len=140)
    sid = str(getattr(run, "session_id", "") or "").strip()
    if sid:
        tags["session_id"] = sid

    try:
        meta = store_fn(blob, content_type="application/json; charset=utf-8", run_id=rid, tags=tags)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to store chat thread artifact: {e}")

    artifact_id = str(getattr(meta, "artifact_id", "") or "") if meta is not None else ""
    if not artifact_id:
        artifact_id = str(meta.get("artifact_id", "") or "") if isinstance(meta, dict) else ""
    if not artifact_id:
        raise HTTPException(status_code=500, detail="Artifact store did not return an artifact_id for chat thread")

    chat_artifact: Dict[str, Any] = {
        "$artifact": artifact_id,
        "content_type": "application/json; charset=utf-8",
        "filename": "chat_thread.json",
        "sha256": sha256,
        "size_bytes": len(blob),
    }

    payload = {
        "thread_id": thread_id,
        "created_at": created_at,
        "workflow_id": workflow_id,
        "run_id": rid,
        "provider": provider,
        "model": model,
        "include_subruns": include_subruns,
        "title": title,
        "message_count": len(messages_out),
        "chat_artifact": chat_artifact,
    }
    try:
        eff = Effect(type=EffectType.EMIT_EVENT, payload={"name": "abstract.chat.thread", "scope": "run", "run_id": rid, "payload": payload})
        rec = StepRecord.start(run=run, node_id="observer", effect=eff, idempotency_key=f"observer:chat_thread:{thread_id}")
        rec.finish_success({"emitted": True, "name": "abstract.chat.thread", "payload": payload})
        svc.host.ledger_store.append(rec)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to persist chat thread event: {e}")

    return SaveChatThreadResponse(
        ok=True,
        run_id=rid,
        workflow_id=workflow_id,
        thread_id=thread_id,
        created_at=created_at,
        duplicate=False,
        title=title,
        message_count=len(messages_out),
        chat_artifact=chat_artifact,
    )


class VoiceTTSRequest(BaseModel):
    text: str = Field(..., description="Text to synthesize.")
    provider: Optional[str] = Field(default=None, description="Optional TTS provider/engine selector for this synthesis request.")
    voice: Optional[str] = Field(default=None, description="Optional cloned voice selector (backend-specific).")
    profile: Optional[str] = Field(default=None, description="Optional base/profile voice selector for the active TTS engine.")
    model: Optional[str] = Field(default=None, description="Optional TTS model override for this synthesis request.")
    format: str = Field(default="wav", description="Audio container/codec (wav|mp3, backend-dependent).")
    speed: Optional[float] = Field(default=None, description="Optional speech speed multiplier when supported by the backend.")
    quality_preset: Optional[str] = Field(default=None, description="Optional AbstractVoice quality preset: low, standard, or high.")
    quality: Optional[str] = Field(default=None, description="Compatibility alias for quality_preset.")
    instructions: Optional[str] = Field(default=None, description="Optional expressive/style instructions for backends that support them.")
    request_id: Optional[str] = Field(default=None, description="Optional idempotency key (UUID recommended).")


class VoiceTTSResponse(BaseModel):
    ok: bool = Field(default=True)
    run_id: str
    request_id: str
    child_run_id: Optional[str] = None
    audio_artifact: Dict[str, Any]


@router.post("/runs/{run_id}/voice/tts", response_model=VoiceTTSResponse)
async def voice_tts(run_id: str, req: VoiceTTSRequest) -> VoiceTTSResponse:
    """Delegate TTS to Runtime-owned durable child execution."""
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
    profile = getattr(req, "profile", None)
    model = getattr(req, "model", None)
    provider = getattr(req, "provider", None)
    fmt = str(getattr(req, "format", "wav") or "wav").strip().lower()
    quality_preset = str(getattr(req, "quality_preset", None) or getattr(req, "quality", None) or "").strip()
    instructions = str(getattr(req, "instructions", "") or "").strip()
    speed = getattr(req, "speed", None)
    voice_name = str(voice).strip() if isinstance(voice, str) and voice.strip() else None
    profile_name = str(profile).strip() if isinstance(profile, str) and profile.strip() else None
    model_name = str(model).strip() if isinstance(model, str) and model.strip() else None
    provider_name = str(provider).strip() if isinstance(provider, str) and provider.strip() else None
    effective_profile_name = profile_name
    run_facade, err = _gateway_abstractcore_run_facade()
    if err or run_facade is None:
        raise HTTPException(status_code=503, detail=err or "Gateway runtime does not expose AbstractCore durable media helpers.")

    if fmt in {"wav", "wave"}:
        content_type = "audio/wav"
        filename = "tts.wav"
    elif fmt in {"mp3", "mpeg"}:
        content_type = "audio/mpeg"
        filename = "tts.mp3"
    else:
        content_type = "application/octet-stream"
        filename = f"tts.{fmt}" if fmt else "tts.bin"

    output_spec: Dict[str, Any] = {
        "modality": "voice",
        "task": "tts",
        "format": fmt,
    }
    if voice_name:
        output_spec["voice"] = voice_name
    if effective_profile_name:
        output_spec["profile"] = effective_profile_name
    if model_name:
        output_spec["model"] = model_name
    if provider_name:
        output_spec["provider"] = provider_name
    if speed is not None:
        output_spec["speed"] = speed
    if quality_preset:
        output_spec["quality_preset"] = quality_preset
    if instructions:
        output_spec["instructions"] = instructions

    params = {
        "trace_metadata": {
            "run_id": str(getattr(run, "run_id", rid)),
            "workflow_id": str(getattr(run, "workflow_id", "") or ""),
            "session_id": str(getattr(run, "session_id", "") or ""),
            "request_id": request_id,
        }
    }

    try:
        child = await asyncio.to_thread(
            run_facade.generate_voice,
            str(getattr(run, "run_id", rid)),
            text=text,
            output=output_spec,
            params=params,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"TTS failed: {e}")

    result = _gateway_completed_child_result(child, operation="TTS")
    voice_outputs = result.get("outputs") if isinstance(result.get("outputs"), dict) else {}
    items = voice_outputs.get("voice") if isinstance(voice_outputs, dict) else None
    item = next((entry for entry in items if isinstance(entry, dict)), None) if isinstance(items, list) else None
    if not isinstance(item, dict):
        raise HTTPException(status_code=500, detail="TTS completed without an audio artifact")
    audio_ref = _gateway_generated_artifact_ref_from_item(
        item,
        store=store,
        run_id=str(getattr(run, "run_id", rid)),
        request_id=request_id,
        session_id=str(getattr(run, "session_id", "") or ""),
        fallback_content_type=content_type,
        fallback_filename=filename,
        modality="voice",
        task="tts",
        source="gateway_direct_tts",
    )
    if not isinstance(audio_ref, dict):
        raise HTTPException(status_code=500, detail="TTS completed without a stored audio artifact")

    return VoiceTTSResponse(
        ok=True,
        run_id=str(getattr(run, "run_id", rid)),
        request_id=request_id,
        child_run_id=str(child.run_id),
        audio_artifact=audio_ref,
    )


class AudioTranscribeRequest(BaseModel):
    audio_artifact: Dict[str, Any] = Field(..., description="Audio artifact ref dict like {'$artifact': '...'} (from /attachments/*).")
    language: Optional[str] = Field(default=None, description="Optional language hint (backend-specific).")
    provider: Optional[str] = Field(default=None, description="Optional STT provider/engine selector for this transcription request.")
    model: Optional[str] = Field(default=None, description="Optional STT model override for this transcription request.")
    prompt: Optional[str] = Field(default=None, max_length=20000, description="Optional transcription prompt/context hint.")
    response_format: Optional[str] = Field(default=None, max_length=64, description="Optional provider-specific transcription response format.")
    temperature: Optional[float] = Field(default=None, description="Optional transcription temperature when supported.")
    format: Optional[str] = Field(default=None, max_length=32, description="Optional source audio format hint when supported.")
    request_id: Optional[str] = Field(default=None, description="Optional idempotency key (UUID recommended).")


class AudioTranscribeResponse(BaseModel):
    ok: bool = Field(default=True)
    run_id: str
    request_id: str
    child_run_id: Optional[str] = None
    text: str
    transcript_artifact: Dict[str, Any]


class ImageGenerateRequest(BaseModel):
    prompt: str = Field(..., max_length=20000, description="Image generation prompt.")
    provider: Optional[str] = Field(default=None, max_length=80, description="Optional LLM/runtime provider override.")
    model: Optional[str] = Field(default=None, max_length=240, description="Optional LLM/runtime model override.")
    image_provider: Optional[str] = Field(default=None, max_length=80, description="Optional image backend/provider override.")
    image_model: Optional[str] = Field(default=None, max_length=240, description="Optional image model id/name.")
    size: Optional[str] = Field(default=None, max_length=32, description="Optional size selector such as 1024x1024.")
    width: Optional[int] = Field(default=None, ge=1, le=8192, description="Optional image width.")
    height: Optional[int] = Field(default=None, ge=1, le=8192, description="Optional image height.")
    format: str = Field(default="png", max_length=16, description="Desired output format, usually png/jpeg/webp.")
    negative_prompt: Optional[str] = Field(default=None, max_length=8000)
    seed: Optional[int] = Field(default=None)
    steps: Optional[int] = Field(default=None, ge=1, le=500)
    guidance_scale: Optional[float] = Field(default=None)
    quality: Optional[str] = Field(default=None, max_length=32)
    style: Optional[str] = Field(default=None, max_length=64)
    request_id: Optional[str] = Field(default=None, max_length=160, description="Optional idempotency key (UUID recommended).")
    extra: Optional[Dict[str, Any]] = Field(default=None, description="Optional provider-specific image-generation extras.")


class ImageGenerateResponse(BaseModel):
    ok: bool = Field(default=True)
    supported: bool = Field(default=True)
    run_id: str
    request_id: str
    child_run_id: Optional[str] = None
    image_artifact: Optional[Dict[str, Any]] = None
    event_name: Optional[str] = None
    code: Optional[str] = None
    error: Optional[str] = None


class ImageEditRequest(BaseModel):
    prompt: str = Field(..., max_length=20000, description="Image edit prompt.")
    image_artifact: Dict[str, Any] = Field(..., description="Source image artifact ref dict like {'$artifact': '...'}")
    mask_artifact: Optional[Dict[str, Any]] = Field(default=None, description="Optional mask image artifact ref dict like {'$artifact': '...'}")
    provider: Optional[str] = Field(default=None, max_length=80, description="Optional LLM/runtime provider override.")
    model: Optional[str] = Field(default=None, max_length=240, description="Optional LLM/runtime model override.")
    image_provider: Optional[str] = Field(default=None, max_length=80, description="Optional image backend/provider override.")
    image_model: Optional[str] = Field(default=None, max_length=240, description="Optional image model id/name.")
    size: Optional[str] = Field(default=None, max_length=32, description="Optional size selector such as 1024x1024.")
    width: Optional[int] = Field(default=None, ge=1, le=8192, description="Optional image width.")
    height: Optional[int] = Field(default=None, ge=1, le=8192, description="Optional image height.")
    format: str = Field(default="png", max_length=16, description="Desired output format, usually png/jpeg/webp.")
    negative_prompt: Optional[str] = Field(default=None, max_length=8000)
    seed: Optional[int] = Field(default=None)
    steps: Optional[int] = Field(default=None, ge=1, le=500)
    guidance_scale: Optional[float] = Field(default=None)
    strength: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    quality: Optional[str] = Field(default=None, max_length=32)
    style: Optional[str] = Field(default=None, max_length=64)
    request_id: Optional[str] = Field(default=None, max_length=160, description="Optional idempotency key (UUID recommended).")
    extra: Optional[Dict[str, Any]] = Field(default=None, description="Optional provider-specific image-edit extras.")


class MusicGenerateRequest(BaseModel):
    prompt: str = Field(..., max_length=20000, description="Music generation prompt.")
    provider: Optional[str] = Field(default=None, max_length=80, description="Optional LLM/runtime provider override.")
    model: Optional[str] = Field(default=None, max_length=240, description="Optional LLM/runtime model override.")
    music_provider: Optional[str] = Field(default=None, max_length=120, description="Optional music provider override.")
    music_model: Optional[str] = Field(default=None, max_length=240, description="Optional music model id/name.")
    lyrics: Optional[str] = Field(default=None, max_length=20000, description="Optional lyrics for vocal music backends.")
    duration_s: Optional[float] = Field(default=None, gt=0, le=3600, description="Requested output duration in seconds.")
    seed: Optional[int] = Field(default=None, description="Optional deterministic seed.")
    num_inference_steps: Optional[int] = Field(default=None, ge=1, le=2000, description="Optional sampling step count.")
    guidance_scale: Optional[float] = Field(default=None, description="Optional guidance scale when supported.")
    instrumental: Optional[bool] = Field(default=None, description="Request instrumental output when supported.")
    enhance_prompt: Optional[bool] = Field(default=None, description="Enable backend prompt enhancement when supported.")
    structure_prompt: Optional[bool] = Field(default=None, description="Enable backend structure planning when supported.")
    auto_lyrics: Optional[bool] = Field(default=None, description="Allow backend lyric generation when supported.")
    text_planner_mode: Optional[str] = Field(default=None, max_length=32, description="Optional text-planning mode such as auto, on, or off.")
    format: str = Field(default="wav", max_length=16, description="Desired output format, usually wav/mp3/flac.")
    request_id: Optional[str] = Field(default=None, max_length=160, description="Optional idempotency key (UUID recommended).")
    extra: Optional[Dict[str, Any]] = Field(default=None, description="Optional provider-specific music-generation extras.")

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_backend_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        for key in ("music_backend", "musicBackend", "backend_music", "backend"):
            raw = value.get(key)
            if isinstance(raw, str) and raw.strip():
                raise ValueError(
                    "Music generation uses `music_provider` as the backend selector; "
                    "`music_backend` and `backend` are not supported."
                )
        return value


class MusicGenerateResponse(BaseModel):
    ok: bool = Field(default=True)
    supported: bool = Field(default=True)
    run_id: str
    request_id: str
    child_run_id: Optional[str] = None
    music_artifact: Optional[Dict[str, Any]] = None
    code: Optional[str] = None
    error: Optional[str] = None


@router.post("/runs/{run_id}/audio/transcribe", response_model=AudioTranscribeResponse)
async def audio_transcribe(run_id: str, req: AudioTranscribeRequest) -> AudioTranscribeResponse:
    """Delegate STT to Runtime-owned durable child execution."""
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
    _audio_artifact_id, meta_in = _resolve_scoped_input_artifact(
        store=store,
        run=run,
        run_id=rid,
        artifact_ref=audio_ref,
        field_name="audio_artifact",
        expected_content_prefix="audio/",
    )
    session_id = str(getattr(run, "session_id", "") or "")

    language = getattr(req, "language", None)
    language_hint = str(language).strip() if isinstance(language, str) and language.strip() else None
    stt_provider = getattr(req, "provider", None)
    stt_provider_name = str(stt_provider).strip() if isinstance(stt_provider, str) and stt_provider.strip() else None
    stt_model = getattr(req, "model", None)
    stt_model_name = str(stt_model).strip() if isinstance(stt_model, str) and stt_model.strip() else None
    prompt_hint = str(getattr(req, "prompt", "") or "").strip() or None
    response_format = str(getattr(req, "response_format", "") or "").strip() or None
    source_format = str(getattr(req, "format", "") or "").strip() or None
    temperature = getattr(req, "temperature", None)

    run_facade, err = _gateway_abstractcore_run_facade()
    if err or run_facade is None:
        raise HTTPException(status_code=503, detail=err or "Gateway runtime does not expose AbstractCore durable media helpers.")

    output_spec: Dict[str, Any] = {"modality": "text", "task": "transcription"}
    if language_hint:
        output_spec["language"] = language_hint
    if prompt_hint:
        output_spec["prompt"] = prompt_hint
    if response_format:
        output_spec["response_format"] = response_format
    if source_format:
        output_spec["format"] = source_format
    if temperature is not None:
        output_spec["temperature"] = temperature
    if stt_provider_name:
        output_spec["provider"] = stt_provider_name
    if stt_model_name:
        output_spec["model"] = stt_model_name

    params = {
        "trace_metadata": {
            "run_id": str(getattr(run, "run_id", rid)),
            "workflow_id": str(getattr(run, "workflow_id", "") or ""),
            "session_id": str(getattr(run, "session_id", "") or ""),
            "request_id": request_id,
        }
    }

    try:
        child = await asyncio.to_thread(
            run_facade.transcribe_audio,
            str(getattr(run, "run_id", rid)),
            media=audio_ref,
            prompt=prompt_hint,
            output=output_spec,
            params=params,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"STT failed: {e}")

    result = _gateway_completed_child_result(child, operation="STT")
    text = str(result.get("content") or result.get("text") or "")

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
        "source_audio_artifact": str(_audio_artifact_id),
    }
    if stt_provider_name:
        tags["provider"] = stt_provider_name
    if stt_model_name:
        tags["model"] = stt_model_name
    try:
        meta = await asyncio.to_thread(
            store_fn,
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

    return AudioTranscribeResponse(
        ok=True,
        run_id=str(getattr(run, "run_id", rid)),
        request_id=request_id,
        child_run_id=str(child.run_id),
        text=text,
        transcript_artifact=transcript_ref,
    )


def _gateway_generated_image_content_type(fmt: str) -> Tuple[str, str]:
    f = str(fmt or "png").strip().lower() or "png"
    if f in {"jpg", "jpeg"}:
        return "image/jpeg", "jpg"
    if f in {"webp"}:
        return "image/webp", "webp"
    if f in {"png"}:
        return "image/png", "png"
    return f"image/{f}", f


def _gateway_generated_music_content_type(fmt: str) -> Tuple[str, str]:
    f = str(fmt or "wav").strip().lower() or "wav"
    if f in {"wav", "wave"}:
        return "audio/wav", "wav"
    if f in {"mp3", "mpeg"}:
        return "audio/mpeg", "mp3"
    if f == "flac":
        return "audio/flac", "flac"
    if f:
        return f"audio/{f}", f
    return "application/octet-stream", "bin"


def _gateway_image_generation_max_bytes() -> int:
    raw = _env_first("ABSTRACTGATEWAY_IMAGE_MAX_BYTES", "ABSTRACTCORE_IMAGE_MAX_BYTES")
    try:
        value = int(raw) if raw is not None else 20_000_000
    except Exception:
        value = 20_000_000
    return max(1, min(value, 200_000_000))


def _artifact_meta_value(meta: Any, key: str) -> Any:
    if isinstance(meta, dict):
        return meta.get(key)
    return getattr(meta, key, None)


def _artifact_id_from_meta(meta: Any) -> str:
    return str(_artifact_meta_value(meta, "artifact_id") or "").strip()


def _artifact_tags_from_meta(meta: Any) -> Dict[str, Any]:
    tags = _artifact_meta_value(meta, "tags")
    return dict(tags) if isinstance(tags, dict) else {}


def _artifact_content_bytes(store: Any, artifact_id: str) -> Optional[bytes]:
    load_fn = getattr(store, "load", None)
    if not callable(load_fn):
        return None
    loaded = load_fn(str(artifact_id))
    if loaded is None:
        return None
    content = _artifact_meta_value(loaded, "content")
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)
    return None


def _artifact_ref_id_from_request(artifact_ref: Any, *, field_name: str) -> str:
    if not (isinstance(artifact_ref, dict) and isinstance(artifact_ref.get("$artifact"), str) and artifact_ref.get("$artifact")):
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must be an artifact ref dict like {{'$artifact': '...'}}",
        )
    return str(artifact_ref.get("$artifact") or "").strip()


def _assert_artifact_visible_to_run(*, meta: Any, run: Any, run_id: str, field_name: str) -> None:
    expected_run_id = str(getattr(run, "run_id", run_id) or run_id).strip()
    session_id = ""
    try:
        sid0 = getattr(run, "session_id", None)
        session_id = sid0.strip() if isinstance(sid0, str) and sid0.strip() else ""
    except Exception:
        session_id = ""

    allowed_run_ids = {expected_run_id}
    if session_id:
        try:
            allowed_run_ids.add(_session_memory_run_id(session_id))
        except Exception:
            pass

    meta_run_id = ""
    try:
        meta_run_id = str(getattr(meta, "run_id", "") or "").strip()
    except Exception:
        meta_run_id = ""

    if meta_run_id and meta_run_id not in allowed_run_ids:
        tags = getattr(meta, "tags", None)
        tagged_session = str(tags.get("session_id") or "").strip() if isinstance(tags, dict) else ""
        if not session_id or not tagged_session or tagged_session != session_id:
            raise HTTPException(status_code=404, detail=f"{field_name} not found")


def _resolve_scoped_input_artifact(
    *,
    store: Any,
    run: Any,
    run_id: str,
    artifact_ref: Any,
    field_name: str,
    expected_content_prefix: str,
) -> tuple[str, Any]:
    artifact_id = _artifact_ref_id_from_request(artifact_ref, field_name=field_name)
    try:
        meta = store.get_metadata(artifact_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load {field_name} metadata: {e}") from e
    if meta is None:
        raise HTTPException(status_code=404, detail=f"{field_name} not found")

    _assert_artifact_visible_to_run(meta=meta, run=run, run_id=run_id, field_name=field_name)

    content_type = str(getattr(meta, "content_type", "") or "")
    if content_type and not content_type.startswith(expected_content_prefix) and content_type != "application/octet-stream":
        raise HTTPException(
            status_code=400,
            detail=f"{field_name} must be {expected_content_prefix}* (got '{content_type}')",
        )
    return artifact_id, meta


def _gateway_project_artifact_to_parent_run(
    *,
    store: Any,
    artifact_id: str,
    run_id: str,
    content_type: str,
    tags: Dict[str, Any],
) -> tuple[str, Any, Optional[bytes]]:
    meta_fn = getattr(store, "get_metadata", None)
    meta = None
    if callable(meta_fn):
        try:
            meta = meta_fn(str(artifact_id))
        except Exception:
            meta = None

    meta_run_id = str(_artifact_meta_value(meta, "run_id") or "").strip()
    if meta_run_id and meta_run_id != str(run_id):
        blob = _artifact_content_bytes(store, str(artifact_id))
        if blob is None:
            raise HTTPException(
                status_code=500,
                detail="Generated media artifact is not accessible from the parent run",
            )
        store_fn = getattr(store, "store", None)
        if not callable(store_fn):
            raise HTTPException(
                status_code=500,
                detail="Artifact store does not support storing generated media for parent-run access",
            )
        merged_tags = _artifact_tags_from_meta(meta)
        merged_tags.update({str(k): v for k, v in tags.items() if v not in (None, "")})
        merged_tags.setdefault("projected_from_artifact_id", str(artifact_id))
        merged_tags.setdefault("projected_from_run_id", meta_run_id)
        try:
            meta = store_fn(blob, content_type=content_type, run_id=str(run_id), tags=merged_tags)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to project generated media artifact into parent run: {e}") from e
        new_artifact_id = _artifact_id_from_meta(meta)
        if not new_artifact_id:
            raise HTTPException(
                status_code=500,
                detail="Artifact store did not return an artifact_id for projected generated media",
            )
        return new_artifact_id, meta, blob

    return str(artifact_id), meta, None


def _gateway_generated_image_ref_from_result(
    *,
    result: Any,
    store: Any,
    run_id: str,
    request_id: str,
    fmt: str,
    session_id: str,
    task: str = "image_generation",
    source: str = "gateway_direct_image",
    filename_stem: str = "generated",
) -> Optional[Dict[str, Any]]:
    if not isinstance(result, dict):
        return None
    outputs = result.get("outputs")
    if not isinstance(outputs, dict):
        return None
    images = outputs.get("image")
    if not isinstance(images, list) or not images:
        return None

    content_type_default, fmt_norm = _gateway_generated_image_content_type(fmt)
    max_bytes = _gateway_image_generation_max_bytes()
    item0 = next((x for x in images if isinstance(x, dict)), None)
    if not isinstance(item0, dict):
        return None

    content_type = str(item0.get("content_type") or content_type_default).strip() or content_type_default
    ref0 = item0.get("artifact_ref") if isinstance(item0.get("artifact_ref"), dict) else {}
    artifact_id = ""
    if isinstance(ref0, dict):
        artifact_id = str(ref0.get("$artifact") or ref0.get("artifact_id") or ref0.get("id") or "").strip()
    if not artifact_id:
        artifact_id = str(item0.get("$artifact") or item0.get("artifact_id") or item0.get("id") or "").strip()

    data = item0.get("data")
    if not artifact_id and isinstance(data, (bytes, bytearray)):
        image_bytes = bytes(data)
        if len(image_bytes) > max_bytes:
            raise HTTPException(status_code=413, detail=f"Generated image is too large ({len(image_bytes)} bytes; max {max_bytes})")
        store_fn = getattr(store, "store", None)
        if not callable(store_fn):
            raise HTTPException(status_code=500, detail="Artifact store does not support storing generated images")
        sha256 = hashlib.sha256(image_bytes).hexdigest()
        meta = store_fn(
            image_bytes,
            content_type=content_type,
            run_id=run_id,
            tags={
                "kind": "generated_media",
                "modality": "image",
                "task": task,
                "source": source,
                "request_id": request_id,
                "session_id": session_id,
                "sha256": sha256,
                "format": fmt_norm,
            },
        )
        artifact_id = str(getattr(meta, "artifact_id", "") or "")
        if not artifact_id and isinstance(meta, dict):
            artifact_id = str(meta.get("artifact_id") or "")
        if not artifact_id:
            raise HTTPException(status_code=500, detail="Artifact store did not return an artifact_id for generated image")

    if not artifact_id:
        return None

    size_bytes = item0.get("size_bytes")
    if size_bytes is None and isinstance(ref0, dict):
        size_bytes = ref0.get("size_bytes")
    try:
        size_i = int(size_bytes) if size_bytes is not None else None
    except Exception:
        size_i = None
    if size_i is not None and size_i > max_bytes:
        raise HTTPException(status_code=413, detail=f"Generated image is too large ({size_i} bytes; max {max_bytes})")

    artifact_id, meta, projected_blob = _gateway_project_artifact_to_parent_run(
        store=store,
        artifact_id=str(artifact_id),
        run_id=str(run_id),
        content_type=content_type,
        tags={
            "kind": "generated_media",
            "modality": "image",
            "task": task,
            "source": source,
            "request_id": request_id,
            "session_id": session_id,
            "format": fmt_norm,
        },
    )
    if meta is not None:
        meta_content_type = str(_artifact_meta_value(meta, "content_type") or "").strip()
        if meta_content_type:
            content_type = meta_content_type
        if size_i is None:
            raw_size = _artifact_meta_value(meta, "size_bytes")
            try:
                size_i = int(raw_size) if raw_size is not None else None
            except Exception:
                size_i = None

    sha256 = ""
    if isinstance(projected_blob, (bytes, bytearray)):
        blob = bytes(projected_blob)
        if len(blob) > max_bytes:
            raise HTTPException(status_code=413, detail=f"Generated image is too large ({len(blob)} bytes; max {max_bytes})")
        sha256 = hashlib.sha256(blob).hexdigest()
        size_i = len(blob)
    else:
        load_fn = getattr(store, "load", None)
        if callable(load_fn) and (size_i is None or size_i <= max_bytes):
            try:
                loaded = load_fn(str(artifact_id))
                content = getattr(loaded, "content", b"") if loaded is not None else b""
                if isinstance(content, (bytes, bytearray)):
                    blob = bytes(content)
                    if len(blob) > max_bytes:
                        raise HTTPException(status_code=413, detail=f"Generated image is too large ({len(blob)} bytes; max {max_bytes})")
                    sha256 = hashlib.sha256(blob).hexdigest()
                    size_i = len(blob)
            except HTTPException:
                raise
            except Exception:
                sha256 = ""

    out: Dict[str, Any] = {
        "$artifact": artifact_id,
        "content_type": content_type,
        "filename": f"{filename_stem}.{fmt_norm}",
    }
    if sha256:
        out["sha256"] = sha256
    if size_i is not None:
        out["size_bytes"] = int(size_i)
    return out


def _gateway_generated_artifact_ref_from_item(
    item: Dict[str, Any],
    *,
    store: Any,
    run_id: str,
    request_id: str,
    session_id: str,
    fallback_content_type: str,
    fallback_filename: str,
    modality: str,
    task: str,
    source: str,
) -> Optional[Dict[str, Any]]:
    ref = item.get("artifact_ref") if isinstance(item.get("artifact_ref"), dict) else None
    artifact_id = ""
    if isinstance(ref, dict):
        artifact_id = str(ref.get("$artifact") or ref.get("artifact_id") or ref.get("id") or "").strip()
    if not artifact_id:
        artifact_id = str(item.get("$artifact") or item.get("artifact_id") or item.get("id") or "").strip()
    if not artifact_id:
        return None

    content_type = ""
    size_bytes: Optional[int] = None
    if isinstance(ref, dict):
        content_type = str(ref.get("content_type") or "").strip()
        try:
            size_bytes = int(ref.get("size_bytes")) if ref.get("size_bytes") is not None else None
        except Exception:
            size_bytes = None
    if not content_type:
        content_type = str(item.get("content_type") or "").strip() or fallback_content_type

    artifact_id, meta, projected_blob = _gateway_project_artifact_to_parent_run(
        store=store,
        artifact_id=str(artifact_id),
        run_id=str(run_id),
        content_type=content_type,
        tags={
            "kind": "generated_media",
            "modality": modality,
            "task": task,
            "source": source,
            "request_id": request_id,
            "session_id": session_id,
        },
    )
    if meta is not None:
        meta_content_type = str(_artifact_meta_value(meta, "content_type") or "").strip()
        if meta_content_type:
            content_type = meta_content_type
        if size_bytes is None:
            raw_size = _artifact_meta_value(meta, "size_bytes")
            try:
                size_bytes = int(raw_size) if raw_size is not None else None
            except Exception:
                size_bytes = None

    sha256 = ""
    if isinstance(projected_blob, (bytes, bytearray)):
        blob = bytes(projected_blob)
        sha256 = hashlib.sha256(blob).hexdigest()
        size_bytes = len(blob)
    else:
        load_fn = getattr(store, "load", None)
        if callable(load_fn):
            try:
                loaded = load_fn(str(artifact_id))
                content = getattr(loaded, "content", b"") if loaded is not None else b""
                if isinstance(content, (bytes, bytearray)):
                    blob = bytes(content)
                    sha256 = hashlib.sha256(blob).hexdigest()
                    if size_bytes is None:
                        size_bytes = len(blob)
            except Exception:
                pass

    out: Dict[str, Any] = {
        "$artifact": artifact_id,
        "content_type": content_type or fallback_content_type,
        "filename": fallback_filename,
    }
    if size_bytes is not None:
        out["size_bytes"] = int(size_bytes)
    if sha256:
        out["sha256"] = sha256
    return out


def _gateway_completed_child_result(child: RunState, *, operation: str) -> Dict[str, Any]:
    if child.status != RunStatus.COMPLETED:
        raise HTTPException(
            status_code=500,
            detail=f"{operation} child run did not complete successfully: {child.error or child.status.value}",
        )
    result = child.output.get("result") if isinstance(child.output, dict) else None
    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail=f"{operation} child run completed without a structured result")
    return result


@router.post("/runs/{run_id}/images/generate", response_model=ImageGenerateResponse)
async def image_generate(run_id: str, req: ImageGenerateRequest) -> ImageGenerateResponse:
    """Delegate image generation to Runtime-owned durable child execution."""
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
    prompt = str(getattr(req, "prompt", "") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    run_facade, err = _gateway_abstractcore_run_facade()
    if err or run_facade is None:
        return ImageGenerateResponse(
            ok=False,
            supported=False,
            run_id=str(getattr(run, "run_id", rid)),
            request_id=request_id,
            child_run_id=None,
            code="generated_image_unavailable",
            error=err or "Gateway runtime does not expose AbstractCore durable media helpers.",
        )

    fmt = str(getattr(req, "format", "png") or "png").strip().lower() or "png"
    session_id = str(getattr(run, "session_id", "") or "")
    tags = {
        "kind": "generated_media",
        "modality": "image",
        "task": "image_generation",
        "source": "gateway_direct_image",
        "request_id": request_id,
        "session_id": session_id,
        "format": fmt,
    }
    output_spec: Dict[str, Any] = {
        "modality": "image",
        "task": "image_generation",
        "format": fmt,
        "run_id": str(getattr(run, "run_id", rid)),
        "tags": tags,
    }
    for key in ("size", "width", "height", "negative_prompt", "seed", "steps", "guidance_scale", "quality", "style"):
        value = getattr(req, key, None)
        if value is not None:
            output_spec[key] = value
    if isinstance(req.extra, dict) and req.extra:
        output_spec["extra"] = dict(req.extra)
    image_provider = str(getattr(req, "image_provider", "") or "").strip()
    image_model = str(getattr(req, "image_model", "") or "").strip()
    if image_provider:
        output_spec["provider"] = image_provider
    if image_model:
        output_spec["model"] = image_model

    params: Dict[str, Any] = {
        "trace_metadata": {
            "run_id": str(getattr(run, "run_id", rid)),
            "workflow_id": str(getattr(run, "workflow_id", "") or ""),
            "session_id": session_id,
            "request_id": request_id,
        },
    }
    child_vars: Optional[Dict[str, Any]] = None
    runtime_provider = str(getattr(req, "provider", "") or "").strip()
    runtime_model = str(getattr(req, "model", "") or "").strip()
    if runtime_provider or runtime_model:
        runtime_ns: Dict[str, Any] = {}
        if runtime_provider:
            runtime_ns["provider"] = runtime_provider
        if runtime_model:
            runtime_ns["model"] = runtime_model
        child_vars = {"_runtime": runtime_ns}

    try:
        child = await asyncio.to_thread(
            run_facade.generate_image,
            str(getattr(run, "run_id", rid)),
            prompt=prompt,
            output=output_spec,
            params=params,
            child_vars=child_vars,
        )
    except Exception as e:
        return ImageGenerateResponse(
            ok=False,
            supported=False,
            run_id=str(getattr(run, "run_id", rid)),
            request_id=request_id,
            child_run_id=None,
            code="generated_image_error",
            error=str(e),
        )

    result = _gateway_completed_child_result(child, operation="image generation")
    image_ref = _gateway_generated_image_ref_from_result(
        result=result,
        store=store,
        run_id=str(getattr(run, "run_id", rid)),
        request_id=request_id,
        fmt=fmt,
        session_id=session_id,
        task="image_generation",
        source="gateway_direct_image",
        filename_stem="generated",
    )
    if not image_ref:
        return ImageGenerateResponse(
            ok=False,
            supported=False,
            run_id=str(getattr(run, "run_id", rid)),
            request_id=request_id,
            child_run_id=str(child.run_id),
            code="generated_image_no_artifact",
            error="Image generation completed without a stored image artifact.",
        )

    return ImageGenerateResponse(
        ok=True,
        supported=True,
        run_id=str(getattr(run, "run_id", rid)),
        request_id=request_id,
        child_run_id=str(child.run_id),
        image_artifact=image_ref,
        event_name=None,
    )


@router.post("/runs/{run_id}/images/edit", response_model=ImageGenerateResponse)
async def image_edit(run_id: str, req: ImageEditRequest) -> ImageGenerateResponse:
    """Delegate image editing to Runtime-owned durable child execution."""
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
    prompt = str(getattr(req, "prompt", "") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    source_ref = getattr(req, "image_artifact", None)
    source_artifact_id, source_meta = _resolve_scoped_input_artifact(
        store=store,
        run=run,
        run_id=rid,
        artifact_ref=source_ref,
        field_name="image_artifact",
        expected_content_prefix="image/",
    )
    mask_ref = getattr(req, "mask_artifact", None)
    mask_artifact_id = ""
    mask_meta = None
    if mask_ref is not None:
        mask_artifact_id, mask_meta = _resolve_scoped_input_artifact(
            store=store,
            run=run,
            run_id=rid,
            artifact_ref=mask_ref,
            field_name="mask_artifact",
            expected_content_prefix="image/",
        )

    run_facade, err = _gateway_abstractcore_run_facade()
    if err or run_facade is None:
        return ImageGenerateResponse(
            ok=False,
            supported=False,
            run_id=str(getattr(run, "run_id", rid)),
            request_id=request_id,
            child_run_id=None,
            code="edited_image_unavailable",
            error=err or "Gateway runtime does not expose AbstractCore durable media helpers.",
        )

    fmt = str(getattr(req, "format", "png") or "png").strip().lower() or "png"
    session_id = str(getattr(run, "session_id", "") or "")
    tags = {
        "kind": "generated_media",
        "modality": "image",
        "task": "image_edit",
        "source": "gateway_direct_image_edit",
        "request_id": request_id,
        "session_id": session_id,
        "format": fmt,
    }
    output_spec: Dict[str, Any] = {
        "modality": "image",
        "task": "image_edit",
        "format": fmt,
        "run_id": str(getattr(run, "run_id", rid)),
        "tags": tags,
    }
    for key in ("size", "width", "height", "negative_prompt", "seed", "steps", "guidance_scale", "strength", "quality", "style"):
        value = getattr(req, key, None)
        if value is not None:
            output_spec[key] = value
    if isinstance(req.extra, dict) and req.extra:
        output_spec["extra"] = dict(req.extra)
    image_provider = str(getattr(req, "image_provider", "") or "").strip()
    image_model = str(getattr(req, "image_model", "") or "").strip()
    if image_provider:
        output_spec["provider"] = image_provider
    if image_model:
        output_spec["model"] = image_model

    source_item: Dict[str, Any] = {
        "$artifact": source_artifact_id,
        "artifact_id": source_artifact_id,
        "type": "image",
        "role": "source",
    }
    source_content_type = str(getattr(source_meta, "content_type", "") or "").strip()
    if source_content_type:
        source_item["content_type"] = source_content_type
    media_items: List[Dict[str, Any]] = [source_item]
    if mask_artifact_id:
        mask_item: Dict[str, Any] = {
            "$artifact": mask_artifact_id,
            "artifact_id": mask_artifact_id,
            "type": "image",
            "role": "mask",
        }
        mask_content_type = str(getattr(mask_meta, "content_type", "") or "").strip()
        if mask_content_type:
            mask_item["content_type"] = mask_content_type
        media_items.append(mask_item)

    params: Dict[str, Any] = {
        "trace_metadata": {
            "run_id": str(getattr(run, "run_id", rid)),
            "workflow_id": str(getattr(run, "workflow_id", "") or ""),
            "session_id": session_id,
            "request_id": request_id,
        },
    }
    child_vars: Optional[Dict[str, Any]] = None
    runtime_provider = str(getattr(req, "provider", "") or "").strip()
    runtime_model = str(getattr(req, "model", "") or "").strip()
    if runtime_provider or runtime_model:
        runtime_ns: Dict[str, Any] = {}
        if runtime_provider:
            runtime_ns["provider"] = runtime_provider
        if runtime_model:
            runtime_ns["model"] = runtime_model
        child_vars = {"_runtime": runtime_ns}

    try:
        child = await asyncio.to_thread(
            run_facade.edit_image,
            str(getattr(run, "run_id", rid)),
            prompt=prompt,
            media=media_items,
            output=output_spec,
            params=params,
            child_vars=child_vars,
        )
    except Exception as e:
        return ImageGenerateResponse(
            ok=False,
            supported=False,
            run_id=str(getattr(run, "run_id", rid)),
            request_id=request_id,
            child_run_id=None,
            code="edited_image_error",
            error=str(e),
        )

    result = _gateway_completed_child_result(child, operation="image edit")
    image_ref = _gateway_generated_image_ref_from_result(
        result=result,
        store=store,
        run_id=str(getattr(run, "run_id", rid)),
        request_id=request_id,
        fmt=fmt,
        session_id=session_id,
        task="image_edit",
        source="gateway_direct_image_edit",
        filename_stem="edited",
    )
    if not image_ref:
        return ImageGenerateResponse(
            ok=False,
            supported=False,
            run_id=str(getattr(run, "run_id", rid)),
            request_id=request_id,
            child_run_id=str(child.run_id),
            code="edited_image_no_artifact",
            error="Image edit completed without a stored image artifact.",
        )

    return ImageGenerateResponse(
        ok=True,
        supported=True,
        run_id=str(getattr(run, "run_id", rid)),
        request_id=request_id,
        child_run_id=str(child.run_id),
        image_artifact=image_ref,
        event_name=None,
    )


@router.post("/runs/{run_id}/music/generate", response_model=MusicGenerateResponse)
async def music_generate(run_id: str, req: MusicGenerateRequest) -> MusicGenerateResponse:
    """Delegate music generation to Runtime-owned durable child execution."""
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
    prompt = str(getattr(req, "prompt", "") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    run_facade, err = _gateway_abstractcore_run_facade()
    if err or run_facade is None:
        return MusicGenerateResponse(
            ok=False,
            supported=False,
            run_id=str(getattr(run, "run_id", rid)),
            request_id=request_id,
            child_run_id=None,
            code="generated_music_unavailable",
            error=err or "Gateway runtime does not expose AbstractCore durable media helpers.",
        )

    fmt = str(getattr(req, "format", "wav") or "wav").strip().lower() or "wav"
    session_id = str(getattr(run, "session_id", "") or "")
    tags = {
        "kind": "generated_media",
        "modality": "music",
        "task": "music_generation",
        "source": "gateway_direct_music",
        "request_id": request_id,
        "session_id": session_id,
        "format": fmt,
    }
    output_spec: Dict[str, Any] = {
        "modality": "music",
        "task": "music_generation",
        "format": fmt,
        "run_id": str(getattr(run, "run_id", rid)),
        "tags": tags,
    }
    for key in (
        "lyrics",
        "duration_s",
        "seed",
        "num_inference_steps",
        "guidance_scale",
        "instrumental",
        "enhance_prompt",
        "structure_prompt",
        "auto_lyrics",
        "text_planner_mode",
    ):
        value = getattr(req, key, None)
        if value is not None:
            output_spec[key] = value
    if isinstance(req.extra, dict) and req.extra:
        for key, value in req.extra.items():
            if key in output_spec or value is None:
                continue
            output_spec[str(key)] = value

    music_provider = str(getattr(req, "music_provider", "") or "").strip()
    music_model = str(getattr(req, "music_model", "") or "").strip()
    if music_provider:
        output_spec["provider"] = music_provider
    if music_model:
        output_spec["model"] = music_model

    params: Dict[str, Any] = {
        "trace_metadata": {
            "run_id": str(getattr(run, "run_id", rid)),
            "workflow_id": str(getattr(run, "workflow_id", "") or ""),
            "session_id": session_id,
            "request_id": request_id,
        },
    }
    child_vars: Optional[Dict[str, Any]] = None
    runtime_provider = str(getattr(req, "provider", "") or "").strip()
    runtime_model = str(getattr(req, "model", "") or "").strip()
    if runtime_provider or runtime_model:
        runtime_ns: Dict[str, Any] = {}
        if runtime_provider:
            runtime_ns["provider"] = runtime_provider
        if runtime_model:
            runtime_ns["model"] = runtime_model
        child_vars = {"_runtime": runtime_ns}

    try:
        child = await asyncio.to_thread(
            run_facade.generate_music,
            str(getattr(run, "run_id", rid)),
            prompt=prompt,
            output=output_spec,
            params=params,
            child_vars=child_vars,
        )
    except Exception as e:
        return MusicGenerateResponse(
            ok=False,
            supported=False,
            run_id=str(getattr(run, "run_id", rid)),
            request_id=request_id,
            child_run_id=None,
            code="generated_music_error",
            error=str(e),
        )

    result = _gateway_completed_child_result(child, operation="music generation")
    outputs = result.get("outputs") if isinstance(result.get("outputs"), dict) else {}
    items = outputs.get("music") if isinstance(outputs, dict) else None
    item = next((entry for entry in items if isinstance(entry, dict)), None) if isinstance(items, list) else None
    if not isinstance(item, dict):
        return MusicGenerateResponse(
            ok=False,
            supported=False,
            run_id=str(getattr(run, "run_id", rid)),
            request_id=request_id,
            child_run_id=str(child.run_id),
            code="generated_music_no_artifact",
            error="Music generation completed without a stored audio artifact.",
        )

    fallback_content_type, fmt_norm = _gateway_generated_music_content_type(fmt)
    music_ref = _gateway_generated_artifact_ref_from_item(
        item,
        store=store,
        run_id=str(getattr(run, "run_id", rid)),
        request_id=request_id,
        session_id=session_id,
        fallback_content_type=fallback_content_type,
        fallback_filename=f"music.{fmt_norm}",
        modality="music",
        task="music_generation",
        source="gateway_direct_music",
    )
    if not isinstance(music_ref, dict):
        return MusicGenerateResponse(
            ok=False,
            supported=False,
            run_id=str(getattr(run, "run_id", rid)),
            request_id=request_id,
            child_run_id=str(child.run_id),
            code="generated_music_no_artifact",
            error="Music generation completed without a stored audio artifact.",
        )

    return MusicGenerateResponse(
        ok=True,
        supported=True,
        run_id=str(getattr(run, "run_id", rid)),
        request_id=request_id,
        child_run_id=str(child.run_id),
        music_artifact=music_ref,
    )


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

    memory_config = resolve_memory_store_config(base_dir=Path(svc.stores.base_dir))
    has_query_text = isinstance(req.query_text, str) and bool(req.query_text.strip())
    if has_query_text and memory_config.backend == "sqlite":
        raise HTTPException(
            status_code=400,
            detail=(
                "Semantic KG query is not available for memory backend 'sqlite'. "
                "Use a vector-capable backend with the execution-host embedding.text route configured, or omit query_text."
            ),
        )
    if not memory_store_exists(memory_config):
        return KGQueryResponse(
            ok=True,
            scope=scope,
            owner_id=None,
            count=0,
            items=[],
            warnings=[
                f"KG store does not exist at {memory_config.path} (no persisted assertions yet)."
                if memory_config.path is not None
                else "KG store is in-memory and no persisted assertions exist."
            ],
            store=memory_config.public_dict(),
        )

    try:
        from abstractmemory import TripleQuery  # type: ignore
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
    store_info: Optional[Dict[str, Any]] = None
    close_store = True
    try:
        host_store_info = getattr(svc.host, "memory_store_info", None)
        if (
            memory_config.backend == "memory"
            and isinstance(host_store_info, dict)
            and str(host_store_info.get("backend") or "") == "memory"
            and getattr(svc.host, "memory_store", None) is not None
        ):
            store = getattr(svc.host, "memory_store")
            store_info = dict(host_store_info)
            close_store = False
        else:
            embedder = getattr(svc, "embeddings_client", None)
            try:
                memory_resolution = open_gateway_memory_store(base_dir=Path(svc.stores.base_dir), embedder=embedder)
            except RuntimeError as e:
                raise HTTPException(status_code=500, detail=str(e)) from e
            store = memory_resolution.store
            store_info = memory_resolution.public_dict()

        caps = store_info.get("capabilities") if isinstance(store_info, dict) else {}
        if has_query_text and not bool((caps or {}).get("semantic_query")):
            backend = str((store_info or {}).get("backend") or memory_config.backend)
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Semantic KG query is not available for memory backend '{backend}'. "
                    "Use a vector-capable backend with the execution-host embedding.text route configured, or omit query_text."
                ),
            )

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
            if store is not None and close_store:
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
        store=store_info or memory_config.public_dict(),
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
    tool_mode = str(os.getenv("ABSTRACTGATEWAY_TOOL_MODE") or "approval").strip().lower() or "approval"
    return {"items": items, "tool_mode": tool_mode}


@router.get("/semantics")
async def get_semantics_registry() -> Dict[str, Any]:
    """Return the active semantics registry (predicates/types)."""
    try:
        from abstractsemantics import load_semantics_registry  # type: ignore
    except Exception as e:
        raise HTTPException(
            status_code=501,
            detail=(
                "Semantics registry is unavailable. "
                f"Install abstractsemantics or configure ABSTRACTSEMANTICS_REGISTRY_PATH. ({e})"
            ),
        )

    try:
        return load_semantics_registry()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load semantics registry: {e}")


def _api_gateway_path(path: str) -> str:
    suffix = str(path or "").strip()
    if not suffix.startswith("/"):
        suffix = "/" + suffix
    return f"/api/gateway{suffix}"


def _plugin_capability(caps: Dict[str, Any], name: str) -> Dict[str, Any]:
    plugins = caps.get("capability_plugins")
    if not isinstance(plugins, dict):
        return {}
    all_caps = plugins.get("capabilities")
    if not isinstance(all_caps, dict):
        return {}
    item = all_caps.get(str(name))
    return dict(item) if isinstance(item, dict) else {}


def _split_env_csv(raw: Optional[str]) -> list[str]:
    if not isinstance(raw, str):
        return []
    out: list[str] = []
    for part in re.split(r"[,;\n]", raw):
        value = part.strip()
        if value:
            out.append(value)
    return out


def _omnivoice_language_ids() -> list[str]:
    def order(values: list[str]) -> list[str]:
        preferred = {item: index for index, item in enumerate(_OMNIVOICE_FALLBACK_LANGUAGES)}
        return sorted(_dedupe_strings(values), key=lambda item: (preferred.get(item.lower(), len(preferred)), item.lower()))

    try:
        from omnivoice.utils.lang_map import LANG_IDS  # type: ignore

        return order([str(item).strip().lower() for item in LANG_IDS if str(item).strip()])
    except Exception:
        return list(_OMNIVOICE_FALLBACK_LANGUAGES)


def _voice_profile_descriptors() -> list[Dict[str, Any]]:
    """Best-effort voice profile discovery without importing provider packages."""
    profiles: list[Dict[str, Any]] = []
    seen: set[str] = set()

    def _add(profile_id: str, *, label: Optional[str] = None, engine_id: Optional[str] = None, description: Optional[str] = None) -> None:
        pid = str(profile_id or "").strip()
        if not pid:
            return
        eng = str(engine_id or "").strip()
        key = f"{eng}:{pid}".lower()
        if key in seen:
            return
        seen.add(key)
        item: Dict[str, Any] = {"id": pid, "label": str(label or pid).strip() or pid}
        if eng:
            item["engine_id"] = eng
            item["qualified_id"] = f"{eng}:{pid}"
        if isinstance(description, str) and description.strip():
            item["description"] = description.strip()
        profiles.append(item)

    engine = _env_first("ABSTRACTGATEWAY_VOICE_TTS_ENGINE", "ABSTRACTVOICE_TTS_ENGINE")
    engine_id = str(engine or "").strip().lower()
    if engine_id and engine_id not in {"auto", "default"}:
        for item in _builtin_voice_profile_records(engine_id):
            _add(
                str(item.get("profile_id") or item.get("id") or ""),
                label=str(item.get("label") or ""),
                engine_id=str(item.get("engine_id") or engine_id),
                description=item.get("description") if isinstance(item.get("description"), str) else None,
            )

    voices = []
    for key in (
        "ABSTRACTGATEWAY_VOICE_TTS_VOICES",
        "ABSTRACTVOICE_OPENAI_TTS_VOICES",
        "ABSTRACTVOICE_OPENAI_COMPATIBLE_TTS_VOICES",
        "ABSTRACTVOICE_REMOTE_TTS_VOICES",
        "ABSTRACTVOICE_TTS_VOICE",
    ):
        voices.extend(_split_env_csv(os.getenv(key)))
    for voice in voices:
        _add(voice, engine_id=engine)

    return profiles[:100]


def _voice_record_provider(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    tags = item.get("tags") if isinstance(item.get("tags"), dict) else {}
    params = item.get("params") if isinstance(item.get("params"), dict) else {}
    return str(
        item.get("provider")
        or tags.get("provider")
        or params.get("provider")
        or params.get("engine")
        or params.get("engine_id")
        or item.get("engine")
        or item.get("engine_id")
        or tags.get("engine_id")
        or ""
    ).strip()


def _voice_record_model(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    params = item.get("params") if isinstance(item.get("params"), dict) else {}
    return str(
        item.get("model")
        or item.get("model_id")
        or item.get("language")
        or params.get("model")
        or params.get("model_id")
        or params.get("model_filename")
        or params.get("language")
        or ""
    ).strip()


def _voice_record_key(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item)
    provider = _voice_record_provider(item).lower()
    record_id = str(
        item.get("qualified_id")
        or item.get("id")
        or item.get("voice_id")
        or item.get("profile_id")
        or item.get("label")
        or ""
    ).strip().lower()
    model = _voice_record_model(item).lower()
    return f"{provider}:{record_id}:{model}"


def _builtin_voice_profile_records(engine: str) -> list[Dict[str, Any]]:
    engine_id = str(engine or "").strip().lower()
    if not engine_id:
        return []
    builtins: Dict[str, list[str]] = {
        "supertonic": ["M1", "M2", "M3", "M4", "M5", "F1", "F2", "F3", "F4", "F5"],
    }
    voice_ids = builtins.get(engine_id, [])
    records: list[Dict[str, Any]] = []
    for profile_id in voice_ids:
        records.append(
            {
                "id": profile_id,
                "profile_id": profile_id,
                "label": profile_id,
                "provider": engine_id,
                "engine_id": engine_id,
                "qualified_id": f"{engine_id}:{profile_id}",
                "params": {
                    "provider": engine_id,
                    "engine": engine_id,
                    "engine_id": engine_id,
                    "voice": profile_id,
                },
                "tags": {
                    "provider": engine_id,
                    "engine_id": engine_id,
                },
            }
        )
    return records


def _has_builtin_voice_profiles(engine: str) -> bool:
    return bool(_builtin_voice_profile_records(engine))


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _dedupe_strings(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _provider_string_map(value: Any) -> Dict[str, list[str]]:
    out: Dict[str, list[str]] = {}
    if not isinstance(value, dict):
        return out
    for provider, raw_values in value.items():
        provider_id = str(provider or "").strip()
        if not provider_id:
            continue
        values: list[str] = []
        if isinstance(raw_values, str):
            values = [raw_values]
        elif isinstance(raw_values, list):
            for item in raw_values:
                if isinstance(item, str) and item.strip():
                    values.append(item.strip())
                elif isinstance(item, dict):
                    for key in ("model", "model_id", "id", "name"):
                        model_id = item.get(key)
                        if isinstance(model_id, str) and model_id.strip():
                            values.append(model_id.strip())
                            break
        cleaned = _dedupe_strings(values)
        if cleaned:
            out[provider_id] = cleaned
    return out


def _provider_models_from_mapping(mapping: Dict[str, list[str]]) -> list[Dict[str, str]]:
    rows: list[Dict[str, str]] = []
    for provider, models in mapping.items():
        provider_id = str(provider or "").strip()
        if not provider_id:
            continue
        for model in models:
            model_id = str(model or "").strip()
            if model_id:
                rows.append({"provider": provider_id, "model": model_id, "id": f"{provider_id}/{model_id}"})
    return rows


def _vision_models_by_provider(models: list[Dict[str, Any]]) -> Dict[str, list[str]]:
    out: Dict[str, list[str]] = {}
    for item in models:
        provider = str(item.get("provider") or item.get("owned_by") or item.get("backend") or "").strip()
        model = str(item.get("model") or item.get("routed_model") or item.get("model_id") or item.get("id") or "").strip()
        if not provider or not model:
            continue
        current = out.setdefault(provider, [])
        if model.lower() not in {m.lower() for m in current}:
            current.append(model)
    return out


def _request_provider_api_key(request: Request) -> Optional[str]:
    for header in ("x-abstractcore-provider-api-key", "x-provider-api-key"):
        value = request.headers.get(header)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _gateway_runtime() -> tuple[Optional[Any], Optional[str]]:
    svc = get_gateway_service()
    host = getattr(svc, "host", None)
    runtime = getattr(host, "runtime", None)
    if runtime is None:
        return None, "Gateway host does not expose a shared runtime."
    return runtime, None


def _gateway_abstractcore_host_facade() -> tuple[Optional[Any], Optional[str]]:
    runtime, err = _gateway_runtime()
    if err:
        return None, err
    try:
        from abstractruntime.integrations.abstractcore import get_abstractcore_host_facade

        return get_abstractcore_host_facade(runtime), None
    except Exception as e:
        return None, f"Gateway runtime is not wired to AbstractCore host controls: {e}"


def _gateway_abstractcore_discovery_facade() -> tuple[Optional[Any], Optional[str]]:
    runtime, err = _gateway_runtime()
    if err:
        return None, err
    try:
        from abstractruntime.integrations.abstractcore import get_abstractcore_discovery_facade

        return get_abstractcore_discovery_facade(runtime), None
    except Exception as e:
        return None, f"Gateway runtime is not wired to AbstractCore discovery helpers: {e}"


def _gateway_abstractcore_run_facade() -> tuple[Optional[Any], Optional[str]]:
    runtime, err = _gateway_runtime()
    if err:
        return None, err
    try:
        from abstractruntime.integrations.abstractcore import get_abstractcore_run_facade

        return get_abstractcore_run_facade(runtime), None
    except Exception as e:
        return None, f"Gateway runtime is not wired to AbstractCore durable media helpers: {e}"


def _runtime_discovery_payload(payload: Any, *, source: str = "abstractruntime.discovery_facade") -> Dict[str, Any]:
    out = dict(payload) if isinstance(payload, dict) else {}
    upstream_source = out.get("source")
    if isinstance(upstream_source, str) and upstream_source.strip():
        out.setdefault("upstream_source", upstream_source.strip())
    out.setdefault("route_available", True)
    out.setdefault("available", bool(out))
    out["source"] = source
    out.setdefault("stale", False)
    out.setdefault("error", None)
    out.setdefault("refreshed_at", _utc_now_iso())
    return out


def _gateway_catalog_contract_descriptor() -> Dict[str, Any]:
    return {
        "contract": _GATEWAY_CATALOG_CONTRACT,
        "version": _GATEWAY_CATALOG_VERSION,
        "metadata_field": "catalog",
        "primary_items_field": "items",
        "items_field": "items",
        "provider_item_fields": ["id", "label", "provider"],
        "model_item_fields": ["id", "label", "provider", "tasks", "parameters"],
        "voice_item_fields": ["id", "label", "provider", "model", "voice_kind"],
    }


def _catalog_string_list(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if not isinstance(value, list):
        return []
    return _dedupe_strings([str(item).strip() for item in value if isinstance(item, str) and str(item).strip()])


def _gateway_catalog_provider_item(value: Any) -> Optional[Dict[str, Any]]:
    raw = dict(value) if isinstance(value, dict) else {}
    provider_id = ""
    label = ""
    for key in ("provider", "provider_id", "backend_id", "id", "name"):
        candidate = raw.get(key) if raw else value
        if isinstance(candidate, str) and candidate.strip():
            provider_id = candidate.strip()
            break
    if not provider_id and isinstance(value, str) and value.strip():
        provider_id = value.strip()
    if not provider_id:
        return None
    for key in ("label", "display_name", "name", "provider_name", "title"):
        candidate = raw.get(key) if raw else None
        if isinstance(candidate, str) and candidate.strip():
            label = candidate.strip()
            break
    if not label:
        label = provider_id
    out: Dict[str, Any] = raw
    out["id"] = provider_id
    out["label"] = label
    out["provider"] = provider_id
    out.setdefault("name", provider_id)
    tasks = _catalog_string_list(out.get("tasks") if raw else None)
    if not tasks:
        tasks = _catalog_string_list(out.get("capabilities") if raw else None)
    if tasks:
        out["tasks"] = tasks
    models = _catalog_string_list(out.get("models") if raw else None)
    if models:
        out["models"] = models
    return out


def _gateway_catalog_model_item(
    value: Any,
    *,
    provider: Optional[str] = None,
    task: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    raw = dict(value) if isinstance(value, dict) else {}
    fallback_provider = str(provider or "").strip()
    provider_id = str(
        raw.get("provider")
        or raw.get("owned_by")
        or raw.get("backend")
        or raw.get("provider_id")
        or fallback_provider
        or ""
    ).strip()
    model_value = str(
        raw.get("model")
        or raw.get("model_id")
        or raw.get("name")
        or raw.get("routed_model")
        or ""
    ).strip()
    item_id = str(raw.get("id") or "").strip()
    if item_id and model_value and item_id.lower().endswith(f"/{model_value.lower()}"):
        item_id = model_value
    if not item_id:
        item_id = model_value
    if not item_id and isinstance(value, str) and value.strip():
        item_id = value.strip()
    if not model_value and isinstance(value, str) and value.strip():
        model_value = value.strip()
    if not item_id:
        return None
    label = str(raw.get("label") or model_value or item_id).strip() or item_id
    out: Dict[str, Any] = raw
    out["id"] = item_id
    out["label"] = label
    if provider_id:
        out["provider"] = provider_id
    if model_value and model_value != item_id:
        out["model"] = model_value
    tasks = _catalog_string_list(out.get("tasks") if raw else None)
    if not tasks:
        tasks = _catalog_string_list(out.get("capabilities") if raw else None)
    if not tasks and isinstance(task, str) and task.strip():
        tasks = [task.strip()]
    if tasks:
        out["tasks"] = tasks
    if not isinstance(out.get("parameters"), dict):
        defaults = out.get("parameter_defaults")
        constraints = out.get("parameter_constraints")
        if isinstance(defaults, dict) or isinstance(constraints, dict):
            out["parameters"] = {
                "defaults": dict(defaults) if isinstance(defaults, dict) else {},
                "constraints": dict(constraints) if isinstance(constraints, dict) else {},
            }
    return out


def _gateway_catalog_voice_item(value: Any, *, voice_kind: str) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    voice_id = str(
        value.get("id")
        or value.get("voice_id")
        or value.get("profile_id")
        or value.get("label")
        or ""
    ).strip()
    if not voice_id:
        return None
    out = dict(value)
    out["id"] = voice_id
    out["label"] = str(value.get("label") or voice_id).strip() or voice_id
    provider = _voice_record_provider(value)
    model = _voice_record_model(value)
    if provider:
        out["provider"] = provider
    if model:
        out["model"] = model
    out["voice_kind"] = str(out.get("voice_kind") or voice_kind).strip() or voice_kind
    return out


def _gateway_catalog_dedupe_items(
    items: list[Dict[str, Any]],
    *,
    key_fields: tuple[str, ...] = ("provider", "id"),
) -> list[Dict[str, Any]]:
    out: list[Dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        key = tuple(str(item.get(field) or "").strip().lower() for field in key_fields)
        if not any(key):
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _gateway_catalog_provider_items(
    payload: Dict[str, Any],
    *,
    provider_keys: tuple[str, ...],
    detail_keys: tuple[str, ...] = (),
) -> list[Dict[str, Any]]:
    items: list[Dict[str, Any]] = []
    for key in detail_keys:
        values = payload.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            item = _gateway_catalog_provider_item(value)
            if item is not None:
                items.append(item)
    for key in provider_keys:
        values = payload.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            item = _gateway_catalog_provider_item(value)
            if item is not None:
                items.append(item)
    return _gateway_catalog_dedupe_items(items)


def _gateway_catalog_model_items(
    payload: Dict[str, Any],
    *,
    provider: Optional[str] = None,
    task: Optional[str] = None,
    detail_keys: tuple[str, ...] = (),
    map_keys: tuple[str, ...] = (),
    value_keys: tuple[str, ...] = ("models",),
) -> list[Dict[str, Any]]:
    items: list[Dict[str, Any]] = []
    for key in detail_keys:
        values = payload.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            item = _gateway_catalog_model_item(value, provider=provider, task=task)
            if item is not None:
                items.append(item)
    for key in map_keys:
        mapping = _provider_string_map(payload.get(key))
        for provider_id, model_values in mapping.items():
            for model_id in model_values:
                item = _gateway_catalog_model_item(model_id, provider=provider_id, task=task)
                if item is not None:
                    items.append(item)
    for key in value_keys:
        values = payload.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            item = _gateway_catalog_model_item(value, provider=provider, task=task)
            if item is not None:
                items.append(item)
    return _gateway_catalog_dedupe_items(items)


def _gateway_catalog_voice_items(payload: Dict[str, Any]) -> list[Dict[str, Any]]:
    merged: Dict[tuple[str, str, str], Dict[str, Any]] = {}
    for key, voice_kind in (("profiles", "profile"), ("voices", "voice"), ("cloned_voices", "clone")):
        values = payload.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            item = _gateway_catalog_voice_item(value, voice_kind=voice_kind)
            if item is None:
                continue
            merged_key = (
                str(item.get("provider") or "").strip().lower(),
                str(item.get("id") or "").strip().lower(),
                str(item.get("model") or "").strip().lower(),
            )
            existing = merged.get(merged_key)
            if existing is None:
                item["voice_kinds"] = [voice_kind]
                merged[merged_key] = item
                continue
            voice_kinds = _dedupe_strings(
                [str(kind).strip() for kind in list(existing.get("voice_kinds") or []) + [voice_kind] if str(kind).strip()]
            )
            existing["voice_kinds"] = voice_kinds
            if "profile" in voice_kinds:
                existing["voice_kind"] = "profile"
            elif "voice" in voice_kinds:
                existing["voice_kind"] = "voice"
            elif voice_kinds:
                existing["voice_kind"] = voice_kinds[0]
    return list(merged.values())


def _compact_voice_catalog_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Keep Gateway catalog metadata without returning duplicated voice trees."""

    keep_keys = (
        "kind",
        "available",
        "route_available",
        "stale",
        "error",
        "source",
        "upstream_source",
        "refreshed_at",
        "engine_id",
        "provider_id",
        "active_provider",
        "active_model",
        "active_tts_provider",
        "active_stt_provider",
        "providers",
        "tts_providers",
        "stt_providers",
        "models",
        "tts_models",
        "stt_models",
        "models_by_provider",
        "tts_models_by_provider",
        "stt_models_by_provider",
        "tts_model_roles_by_provider",
        "tts_voices_by_provider",
        "tts_profiles_by_provider",
        "tts_formats_by_provider",
        "controls",
    )
    return {key: payload[key] for key in keep_keys if key in payload}


def _gateway_catalog_response(
    payload: Dict[str, Any],
    *,
    kind: str,
    scope: str,
    items: list[Dict[str, Any]],
    task: Optional[str] = None,
    provider: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    out = dict(payload)
    out["items"] = items
    catalog: Dict[str, Any] = {
        "contract": _GATEWAY_CATALOG_CONTRACT,
        "version": _GATEWAY_CATALOG_VERSION,
        "kind": kind,
        "scope": scope,
        "primary_items_field": "items",
        "source": "abstractgateway.catalog",
        "route_source": str(out.get("source") or "").strip() or "abstractruntime.discovery_facade",
        "available": bool(out.get("available")),
        "route_available": bool(out.get("route_available", True)),
    }
    upstream_source = str(out.get("upstream_source") or "").strip()
    if upstream_source:
        catalog["upstream_source"] = upstream_source
    if isinstance(task, str) and task.strip():
        catalog["task"] = task.strip()
    if isinstance(provider, str) and provider.strip():
        catalog["provider"] = provider.strip()
    if isinstance(out.get("error"), str) and str(out.get("error")).strip():
        catalog["error"] = str(out.get("error")).strip()
    if isinstance(metadata, dict):
        for key, value in metadata.items():
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            catalog[key] = value
    out["catalog"] = catalog
    return out


def _descriptor_endpoint_available(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if not isinstance(value, dict):
        return False
    if value.get("available") is False:
        return False
    if value.get("route_available") is False:
        return False
    endpoint = value.get("endpoint")
    return isinstance(endpoint, str) and bool(endpoint.strip())


def _gateway_readiness_descriptor(
    *,
    common: Dict[str, Any],
    flow_editor: Dict[str, Any],
    assistant: Dict[str, Any],
) -> Dict[str, Any]:
    runs = common.get("runs") if isinstance(common.get("runs"), dict) else {}
    ledger = common.get("ledger") if isinstance(common.get("ledger"), dict) else {}
    artifacts = common.get("artifacts") if isinstance(common.get("artifacts"), dict) else {}
    attachments = common.get("attachments") if isinstance(common.get("attachments"), dict) else {}
    workspace = common.get("workspace") if isinstance(common.get("workspace"), dict) else {}
    discovery = common.get("discovery") if isinstance(common.get("discovery"), dict) else {}
    prompt_cache = common.get("prompt_cache") if isinstance(common.get("prompt_cache"), dict) else {}
    memory = common.get("memory") if isinstance(common.get("memory"), dict) else {}
    model_residency = common.get("model_residency") if isinstance(common.get("model_residency"), dict) else {}
    flow_visualflows = flow_editor.get("visualflows") if isinstance(flow_editor.get("visualflows"), dict) else {}
    assistant_voice = assistant.get("voice") if isinstance(assistant.get("voice"), dict) else {}
    assistant_media = assistant.get("media") if isinstance(assistant.get("media"), dict) else {}
    durable_blocs = prompt_cache.get("durable_blocs") if isinstance(prompt_cache.get("durable_blocs"), dict) else {}
    generated_image = assistant_media.get("generated_image") if isinstance(assistant_media.get("generated_image"), dict) else {}
    edited_image = assistant_media.get("edited_image") if isinstance(assistant_media.get("edited_image"), dict) else {}
    generated_voice = assistant_media.get("generated_voice") if isinstance(assistant_media.get("generated_voice"), dict) else {}
    generated_music = assistant_media.get("generated_music") if isinstance(assistant_media.get("generated_music"), dict) else {}
    tts = assistant_voice.get("tts") if isinstance(assistant_voice.get("tts"), dict) else {}
    stt = assistant_voice.get("stt") if isinstance(assistant_voice.get("stt"), dict) else {}
    listen = assistant_voice.get("listen") if isinstance(assistant_voice.get("listen"), dict) else {}
    residency_supports = model_residency.get("supports") if isinstance(model_residency.get("supports"), dict) else {}

    def _surface(value: Dict[str, Any]) -> Dict[str, Any]:
        out = {
            "available": bool(value.get("available")),
            "route_available": bool(value.get("route_available", _descriptor_endpoint_available(value))),
        }
        if value.get("configured") is not None:
            out["configured"] = bool(value.get("configured"))
        if isinstance(value.get("config_hint"), str) and str(value.get("config_hint")).strip():
            out["config_hint"] = str(value.get("config_hint")).strip()
        return out

    return {
        "contract": "gateway_surface_readiness_v1",
        "version": 1,
        "truth_scope": "gateway_contract_surface",
        "limitations": [
            "Derived from Gateway endpoint descriptors and contract wiring only.",
            "Selected backend/provider/model and degraded-state truth remain per-surface and may require Runtime/Core responses.",
        ],
        "surfaces": {
            "runs": {
                "start": _descriptor_endpoint_available(runs.get("start")),
                "schedule": _descriptor_endpoint_available(runs.get("schedule")),
                "summary": _descriptor_endpoint_available(runs.get("summary")),
                "list": _descriptor_endpoint_available(runs.get("list")),
                "input_data": _descriptor_endpoint_available(runs.get("input_data")),
                "history_bundle": _descriptor_endpoint_available(runs.get("history_bundle")),
                "commands": _descriptor_endpoint_available(runs.get("commands")),
            },
            "ledger": {
                "replay": _descriptor_endpoint_available(ledger.get("replay")),
                "batch": _descriptor_endpoint_available(ledger.get("batch")),
                "stream": _descriptor_endpoint_available(ledger.get("stream")),
                "stream_transport": str(ledger.get("stream", {}).get("transport") or "").strip()
                if isinstance(ledger.get("stream"), dict)
                else None,
            },
            "artifacts": {
                "list": _descriptor_endpoint_available(artifacts.get("list")),
                "metadata": _descriptor_endpoint_available(artifacts.get("metadata")),
                "content": _descriptor_endpoint_available(artifacts.get("content")),
            },
            "attachments": {
                "upload": _descriptor_endpoint_available(attachments.get("upload")),
                "max_upload_bytes": int(attachments.get("max_upload_bytes") or 0),
            },
            "workspace": {
                "policy": isinstance(workspace.get("policy_endpoint"), str) and bool(str(workspace.get("policy_endpoint")).strip()),
            },
            "discovery": {
                "providers": isinstance(discovery.get("providers"), str) and bool(str(discovery.get("providers")).strip()),
                "provider_models": isinstance(discovery.get("provider_models"), str) and bool(str(discovery.get("provider_models")).strip()),
                "voice_voices": isinstance(discovery.get("voice_voices"), str) and bool(str(discovery.get("voice_voices")).strip()),
                "audio_speech_models": isinstance(discovery.get("audio_speech_models"), str) and bool(str(discovery.get("audio_speech_models")).strip()),
                "audio_transcription_models": isinstance(discovery.get("audio_transcription_models"), str) and bool(str(discovery.get("audio_transcription_models")).strip()),
                "audio_music_providers": isinstance(discovery.get("audio_music_providers"), str) and bool(str(discovery.get("audio_music_providers")).strip()),
                "audio_music_models": isinstance(discovery.get("audio_music_models"), str) and bool(str(discovery.get("audio_music_models")).strip()),
                "vision_provider_models": isinstance(discovery.get("vision_provider_models"), str) and bool(str(discovery.get("vision_provider_models")).strip()),
                "vision_models": isinstance(discovery.get("vision_models"), str) and bool(str(discovery.get("vision_models")).strip()),
                "tools": isinstance(discovery.get("tools"), str) and bool(str(discovery.get("tools")).strip()),
                "semantics": isinstance(discovery.get("semantics"), str) and bool(str(discovery.get("semantics")).strip()),
            },
            "visualflows": {
                "crud_available": bool(flow_visualflows.get("crud", {}).get("available", False))
                if isinstance(flow_visualflows.get("crud"), dict)
                else False,
                "publish_available": bool(flow_visualflows.get("publish", {}).get("available", False))
                if isinstance(flow_visualflows.get("publish"), dict)
                else False,
            },
            "prompt_cache": {
                "provider_controls": bool(prompt_cache.get("provider_controls")),
                "provider_controls_available": bool(prompt_cache.get("provider_controls_available")),
                "session_lifecycle": bool(prompt_cache.get("session_lifecycle")),
                "session_lifecycle_available": bool(prompt_cache.get("session_lifecycle_available")),
                "durable_blocs_available": bool(durable_blocs.get("available")),
                "durable_blocs_lifecycle_available": bool(durable_blocs.get("lifecycle_available")),
            },
            "memory": {
                "available": bool(memory.get("available")),
                "route_available": bool(memory.get("route_available", _descriptor_endpoint_available(memory))),
                "backend": str(memory.get("backend") or "").strip() or None,
                "structured_query": bool(memory.get("structured_query")),
                "semantic_query": bool(memory.get("semantic_query")),
            },
            "model_residency": {
                "available": bool(model_residency.get("available")),
                "route_available": bool(model_residency.get("route_available", True)),
                "supported_tasks": list(model_residency.get("supported_tasks") or [])
                if isinstance(model_residency.get("supported_tasks"), list)
                else [],
                "unsupported_tasks": list(model_residency.get("unsupported_tasks") or [])
                if isinstance(model_residency.get("unsupported_tasks"), list)
                else [],
                **({"mode": model_residency.get("mode")} if model_residency.get("mode") else {}),
                **({"relay_only": bool(model_residency.get("relay_only"))} if model_residency.get("relay_only") is not None else {}),
                **({"supports": dict(residency_supports)} if residency_supports else {}),
            },
            "media": {
                "generated_image": {
                    **_surface(generated_image.get("direct_endpoint") if isinstance(generated_image.get("direct_endpoint"), dict) else {}),
                    "workflow_available": bool(generated_image.get("workflow", {}).get("available"))
                    if isinstance(generated_image.get("workflow"), dict)
                    else False,
                },
                "edited_image": {
                    **_surface(edited_image.get("direct_endpoint") if isinstance(edited_image.get("direct_endpoint"), dict) else {}),
                    "workflow_available": bool(edited_image.get("workflow", {}).get("available"))
                    if isinstance(edited_image.get("workflow"), dict)
                    else False,
                },
                "generated_voice": {
                    **_surface(generated_voice.get("direct_endpoint") if isinstance(generated_voice.get("direct_endpoint"), dict) else {}),
                    "workflow_available": bool(generated_voice.get("workflow", {}).get("available"))
                    if isinstance(generated_voice.get("workflow"), dict)
                    else False,
                },
                "stt": _surface(stt),
                "listen": {
                    "available": bool(listen.get("available", False)),
                    "host_capture_required": bool(listen.get("host_capture_required", False)),
                    "transcription_available": bool(listen.get("transcription_available", False)),
                    "transcribe_route_available": bool(listen.get("transcribe_route_available", False)),
                },
                "generated_music": {
                    **_surface(generated_music.get("direct_endpoint") if isinstance(generated_music.get("direct_endpoint"), dict) else {}),
                    "workflow_available": bool(generated_music.get("workflow", {}).get("available"))
                    if isinstance(generated_music.get("workflow"), dict)
                    else False,
                },
            },
        },
    }


def _voice_catalog_controls() -> Dict[str, Any]:
    return {
        "speed": {"supported": True, "min": 0.5, "max": 2.0, "default": 1.0},
        "quality_preset": {"supported": True, "values": ["low", "standard", "high"], "default": "standard"},
        "instructions": {"supported": True},
        "profile": {"supported": True},
        "voice_clone": {"supported": True},
    }


def _static_voice_catalog_response(provider: Optional[str] = None) -> Dict[str, Any]:
    wanted = str(provider or "").strip().lower()
    profiles: list[Dict[str, Any]] = []
    tts_models_by_provider: Dict[str, list[str]] = {}
    tts_profiles_by_provider: Dict[str, list[str]] = {}
    tts_voices_by_provider: Dict[str, list[str]] = {}
    tts_providers: list[str] = []

    def include(engine: str) -> bool:
        return not wanted or wanted == engine

    def add_provider(engine: str) -> None:
        if engine not in tts_providers:
            tts_providers.append(engine)

    if include("openai") and _env_first("OPENAI_API_KEY", "ABSTRACTVOICE_OPENAI_API_KEY"):
        add_provider("openai")
        openai_profiles = [
            {"id": voice, "profile_id": voice, "voice_id": voice, "label": voice, "provider": "openai", "engine_id": "openai", "qualified_id": f"openai:{voice}", "params": {"provider": "openai", "engine": "openai", "voice": voice}, "tags": {"provider": "openai", "engine_id": "openai"}}
            for voice in ["alloy", "ash", "ballad", "coral", "echo", "fable", "marin", "nova", "onyx", "sage", "shimmer", "verse", "cedar"]
        ]
        profiles.extend(openai_profiles)
        tts_models_by_provider["openai"] = _dedupe_strings(
            _split_env_csv(os.getenv("ABSTRACTVOICE_OPENAI_TTS_MODEL"))
            + _split_env_csv(os.getenv("ABSTRACTGATEWAY_VOICE_TTS_MODEL"))
            + ["tts-1", "tts-1-hd", "gpt-4o-mini-tts"]
        )
        tts_profiles_by_provider["openai"] = [str(item["id"]) for item in openai_profiles]
        tts_voices_by_provider["openai"] = [str(item["id"]) for item in openai_profiles]

    if include("piper") and (_module_available("piper") or _module_available("piper_phonemize")):
        add_provider("piper")

    if include("omnivoice") and _module_available("omnivoice"):
        omni_profiles = _builtin_voice_profile_records("omnivoice")
        add_provider("omnivoice")
        profiles.extend(omni_profiles)
        tts_models_by_provider["omnivoice"] = _omnivoice_language_ids()
        tts_profiles_by_provider["omnivoice"] = [str(item["id"]) for item in omni_profiles]
        tts_voices_by_provider["omnivoice"] = [str(item["id"]) for item in omni_profiles]

    if include("supertonic"):
        supertonic_profiles = _builtin_voice_profile_records("supertonic")
    else:
        supertonic_profiles = []
    if include("supertonic") and (supertonic_profiles or _module_available("onnxruntime")):
        add_provider("supertonic")
        profiles.extend(supertonic_profiles)
        tts_models_by_provider["supertonic"] = ["supertonic-3"]
        tts_profiles_by_provider["supertonic"] = [str(item["id"]) for item in supertonic_profiles]
        tts_voices_by_provider["supertonic"] = [str(item["id"]) for item in supertonic_profiles]

    env_profiles = _voice_profile_descriptors()
    for item in env_profiles:
        engine = _voice_record_provider(item).lower()
        if wanted and engine and engine != wanted:
            continue
        profiles.append(item)
        if engine:
            add_provider(engine)

    deduped_profiles: list[Dict[str, Any]] = []
    seen_profiles: set[str] = set()
    for item in profiles:
        key = _voice_record_key(item)
        if key in seen_profiles:
            continue
        seen_profiles.add(key)
        deduped_profiles.append(item)
    models = _dedupe_strings([model for values in tts_models_by_provider.values() for model in values])
    return {
        "available": bool(deduped_profiles or models or tts_providers),
        "route_available": True,
        "profiles": deduped_profiles,
        "voices": deduped_profiles,
        "cloned_voices": [],
        "models": models,
        "tts_models": models,
        "tts_providers": tts_providers,
        "providers": tts_providers,
        "tts_models_by_provider": tts_models_by_provider,
        "tts_model_roles_by_provider": {
            provider_id: "language" if provider_id == "omnivoice" else "model"
            for provider_id in tts_providers
        },
        "tts_profiles_by_provider": tts_profiles_by_provider,
        "tts_voices_by_provider": tts_voices_by_provider,
        "controls": _voice_catalog_controls(),
        "source": "gateway_static",
        "stale": False,
        "refreshed_at": _utc_now_iso(),
        "error": None,
        **(
            {}
            if deduped_profiles or models or tts_providers
            else {"config_hint": "Configure AbstractCore server catalog routes or ABSTRACTVOICE_* voice profile settings."}
        ),
    }


def _module_available(module_name: str) -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec(module_name) is not None
    except Exception:
        return False


def _static_voice_providers_only_response() -> Dict[str, Any]:
    tts_providers: List[str] = []
    stt_providers: List[str] = []

    def add(target: List[str], value: Optional[str]) -> None:
        text = str(value or "").strip().lower().replace("_", "-")
        if text and text not in target:
            target.append(text)

    add(tts_providers, _env_first("ABSTRACTGATEWAY_VOICE_TTS_ENGINE", "ABSTRACTVOICE_TTS_ENGINE"))
    add(stt_providers, _env_first("ABSTRACTGATEWAY_VOICE_STT_ENGINE", "ABSTRACTVOICE_STT_ENGINE"))
    if _env_first("OPENAI_API_KEY", "ABSTRACTVOICE_OPENAI_API_KEY"):
        add(tts_providers, "openai")
        add(stt_providers, "openai")
    if _env_first("ABSTRACTGATEWAY_VOICE_REMOTE_BASE_URL", "ABSTRACTVOICE_REMOTE_BASE_URL"):
        add(tts_providers, "openai-compatible")
        add(stt_providers, "openai-compatible")
    if _module_available("piper") or _module_available("piper_phonemize"):
        add(tts_providers, "piper")
    if _module_available("omnivoice"):
        add(tts_providers, "omnivoice")
    if _module_available("onnxruntime") or _has_builtin_voice_profiles("supertonic"):
        add(tts_providers, "supertonic")
    if _module_available("torch") and _module_available("transformers"):
        add(tts_providers, "audiodit")
    if _module_available("f5_tts"):
        add(tts_providers, "f5-tts")
    if _module_available("faster_whisper") or _module_available("whisper"):
        add(stt_providers, "faster-whisper")
    if _module_available("torch") and _module_available("transformers") and _module_available("soundfile"):
        add(stt_providers, "transformers-asr")

    return {
        "kind": "voice_providers",
        "available": bool(tts_providers or stt_providers),
        "route_available": True,
        "providers": tts_providers,
        "tts_providers": tts_providers,
        "stt_providers": stt_providers,
        "profiles": [],
        "voices": [],
        "cloned_voices": [],
        "source": "gateway_static_providers",
        "stale": False,
        "refreshed_at": _utc_now_iso(),
        "error": None,
    }


def _static_transcription_providers_only_response(provider: Optional[str] = None) -> Dict[str, Any]:
    raw = _static_voice_providers_only_response()
    wanted = str(provider or "").strip().lower().replace("_", "-")
    stt_providers = _dedupe_strings(
        [str(x).strip() for x in (raw.get("stt_providers") or []) if isinstance(x, str) and str(x).strip()]
    )
    if wanted:
        stt_providers = [p for p in stt_providers if p.lower().replace("_", "-") == wanted]
    out = dict(raw)
    out["kind"] = "stt_providers"
    out["available"] = bool(stt_providers)
    out["providers"] = stt_providers
    out["available_providers"] = stt_providers
    out["stt_providers"] = stt_providers
    out["tts_providers"] = []
    out["models"] = []
    out["models_by_provider"] = {}
    out["stt_models_by_provider"] = {}
    out["provider_models"] = []
    out["active_provider"] = stt_providers[0] if stt_providers else None
    return out


def _static_speech_models_response(provider: Optional[str] = None) -> Dict[str, Any]:
    values: list[str] = []
    for key in (
        "ABSTRACTGATEWAY_VOICE_TTS_MODEL",
        "ABSTRACTVOICE_TTS_MODEL",
        "ABSTRACTVOICE_OPENAI_TTS_MODEL",
        "ABSTRACTVOICE_OPENAI_COMPATIBLE_TTS_MODEL",
        "ABSTRACTVOICE_REMOTE_TTS_MODEL",
    ):
        values.extend(_split_env_csv(os.getenv(key)))
    engine = str(_env_first("ABSTRACTGATEWAY_VOICE_TTS_ENGINE", "ABSTRACTVOICE_TTS_ENGINE") or "").strip()
    provider_key = str(provider or engine or "").strip().lower()
    models_by_provider: Dict[str, list[str]] = {}
    if not provider_key or provider_key == "openai":
        openai_values = _dedupe_strings(values + ["tts-1", "tts-1-hd", "gpt-4o-mini-tts"])
        if openai_values and (_env_first("OPENAI_API_KEY", "ABSTRACTVOICE_OPENAI_API_KEY") or provider_key == "openai"):
            models_by_provider["openai"] = openai_values
    if (not provider_key or provider_key == "supertonic") and (
        _module_available("onnxruntime") or _has_builtin_voice_profiles("supertonic")
    ):
        models_by_provider["supertonic"] = ["supertonic-3"]
    if (not provider_key or provider_key == "omnivoice") and _module_available("omnivoice"):
        models_by_provider["omnivoice"] = _omnivoice_language_ids()
    if not provider_key and engine and values:
        models_by_provider.setdefault(engine.lower(), _dedupe_strings(values))
    models = _dedupe_strings([model for provider_models in models_by_provider.values() for model in provider_models])
    providers = _dedupe_strings(list(models_by_provider.keys()))
    return {
        "available": bool(models),
        "route_available": True,
        "models": models,
        "active_model": models[0] if models else None,
        "providers": providers,
        "active_provider": providers[0] if providers else None,
        "models_by_provider": models_by_provider,
        "tts_models_by_provider": models_by_provider,
        "tts_model_roles_by_provider": {
            provider_id: "language" if provider_id == "omnivoice" else "model"
            for provider_id in providers
        },
        "controls": _voice_catalog_controls(),
        "source": "gateway_static",
        "stale": False,
        "refreshed_at": _utc_now_iso(),
        "error": None,
        **({} if models else {"config_hint": "Configure AbstractCore server catalog routes or ABSTRACTVOICE_* TTS model settings."}),
    }


def _static_transcription_models_response() -> Dict[str, Any]:
    values: list[str] = []
    for key in (
        "ABSTRACTGATEWAY_VOICE_STT_MODEL",
        "ABSTRACTVOICE_STT_MODEL",
        "ABSTRACTVOICE_OPENAI_STT_MODEL",
        "ABSTRACTVOICE_OPENAI_COMPATIBLE_STT_MODEL",
        "ABSTRACTVOICE_REMOTE_STT_MODEL",
    ):
        values.extend(_split_env_csv(os.getenv(key)))
    engine = str(_env_first("ABSTRACTGATEWAY_VOICE_STT_ENGINE", "ABSTRACTVOICE_STT_ENGINE") or "openai").strip().lower()
    if engine in {"openai", "openai-compatible", "remote"}:
        values.extend(["gpt-4o-transcribe", "gpt-4o-mini-transcribe", "whisper-1"])
    elif engine in {"faster_whisper", "faster-whisper", "whisper", "local"}:
        values.extend(["tiny", "base", "small", "medium", "large-v2", "large-v3"])
    models = _dedupe_strings(values)
    providers = _dedupe_strings([engine])
    return {
        "available": bool(models),
        "route_available": True,
        "models": models,
        "active_model": models[0] if models else None,
        "providers": providers,
        "active_provider": providers[0] if providers else None,
        "source": "gateway_static",
        "stale": False,
        "refreshed_at": _utc_now_iso(),
        "error": None,
        **({} if models else {"config_hint": "Configure AbstractCore server catalog routes or ABSTRACTVOICE_* STT model settings."}),
    }


def _gateway_looks_like_hf_repo_id(value: str) -> bool:
    s = str(value or "").strip()
    if not s or "://" in s or s.startswith(("./", "../", "/", "~")):
        return False
    parts = s.split("/")
    return len(parts) == 2 and bool(parts[0]) and bool(parts[1])


def _gateway_has_local_mflux_preset(model_id: str) -> bool:
    _ = model_id
    # Gateway no longer probes provider package internals for local model state.
    # Runtime discovery/model-residency surfaces are the supported source of truth.
    return False


def _gateway_vision_parameter_metadata(*, provider: str, model_id: str) -> Dict[str, Any]:
    _ = provider, model_id
    # Gateway does not own model-specific generation parameters. Dynamic
    # AbstractVision/Core catalogs include parameter metadata when the selected
    # backend exposes it; static env fallbacks stay intentionally generic.
    return {}


def _gateway_vision_provider_model_item(*, provider: str, model_id: str, task: Optional[str], configured: bool = False, cached: bool = False) -> Dict[str, Any]:
    mid = str(model_id or "").strip()
    provider_s = str(provider or "").strip() or "openai-compatible"
    provider_l = provider_s.lower().replace("_", "-")
    if mid.startswith(("mlx-gen/", "mlxgen/", "mflux/")):
        provider_l = "mlx-gen"
        provider_s = "mlx-gen"
        mid = mid.split("/", 1)[1].strip()
    if provider_l in {"mlx-gen", "mlxgen", "mflux", "m-flux"} or _gateway_has_local_mflux_preset(mid):
        display_provider = "mlx-gen"
        routed = mid if mid.startswith("mlx-gen/") else f"mlx-gen/{mid}"
        backend = "mlx-gen"
    elif provider_l in {"huggingface", "hf", "diffusers", "hf-diffusers"} or _gateway_looks_like_hf_repo_id(mid):
        display_provider = "huggingface" if provider_l in {"huggingface", "hf", "hf-diffusers"} or _gateway_looks_like_hf_repo_id(mid) else provider_s
        routed = mid if mid.startswith("diffusers/") else f"diffusers/{mid}"
        backend = "diffusers"
    elif provider_l in {"sdcpp", "sd-cpp", "stable-diffusion.cpp", "stable-diffusion-cpp"}:
        display_provider = provider_s
        routed = mid if mid.startswith("sdcpp/") else f"sdcpp/{mid}"
        backend = "sdcpp"
    else:
        display_provider = provider_s
        routed = mid if mid.startswith("openai-compatible/") else f"openai-compatible/{mid}"
        backend = "openai-compatible"
    caps = [str(task)] if task else ["text_to_image", "image_generation"]
    parameter_metadata = _gateway_vision_parameter_metadata(provider=display_provider, model_id=mid)
    return {
        "id": mid.removeprefix("mlx-gen/").removeprefix("mflux/").removeprefix("diffusers/").removeprefix("openai-compatible/"),
        "model": routed,
        "provider": display_provider,
        "backend": backend,
        "routed_model": routed,
        "object": "model",
        "owned_by": display_provider,
        "capabilities": caps,
        **parameter_metadata,
        "raw": {
            "id": mid.removeprefix("mlx-gen/").removeprefix("mflux/").removeprefix("diffusers/").removeprefix("openai-compatible/"),
            "provider": display_provider,
            "backend": backend,
            "routed_model": routed,
            **parameter_metadata,
            **({"configured": True} if configured else {}),
            **({"local_cached": True} if cached else {}),
        },
    }


def _provider_models_from_cached_vision_response(cached: Optional[Dict[str, Any]], *, task: Optional[str]) -> Dict[str, Any]:
    models: list[Dict[str, Any]] = []
    values = cached.get("models") if isinstance(cached, dict) else None
    if isinstance(values, list):
        for item in values:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id") or item.get("model") or "").strip()
            if not model_id:
                continue
            tasks = item.get("tasks")
            if task and isinstance(tasks, list) and task not in [str(t) for t in tasks]:
                continue
            provider = str(item.get("provider") or "huggingface").strip() or "huggingface"
            entry = _gateway_vision_provider_model_item(provider=provider, model_id=model_id, task=task, cached=True)
            raw = entry.get("raw")
            if isinstance(raw, dict):
                raw.update(item)
                raw["routed_model"] = entry.get("routed_model")
            models.append(entry)
    models_by_provider = _vision_models_by_provider(models)
    return {
        "available": bool(models),
        "route_available": True,
        "models": models,
        "models_by_provider": models_by_provider,
        "provider_models": _provider_models_from_mapping(models_by_provider),
        "providers": list(models_by_provider.keys()),
        "available_providers": list(models_by_provider.keys()),
        "task": task,
        "source": "abstractvision_local_cache",
        "stale": False,
        "refreshed_at": _utc_now_iso(),
        "error": None,
    }


def _merge_vision_provider_model_responses(*responses: Optional[Dict[str, Any]], task: Optional[str]) -> Dict[str, Any]:
    models: list[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    errors: list[str] = []
    providers: list[str] = []
    available_providers: list[str] = []
    for response in responses:
        if not isinstance(response, dict):
            continue
        err = response.get("error")
        if isinstance(err, str) and err.strip():
            errors.append(err.strip())
        providers.extend(str(x) for x in response.get("providers") or [] if isinstance(x, str))
        available_providers.extend(str(x) for x in response.get("available_providers") or [] if isinstance(x, str))
        items = response.get("models")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            provider = str(item.get("provider") or item.get("owned_by") or "").strip()
            model = str(item.get("model") or item.get("routed_model") or item.get("id") or "").strip()
            if not model:
                continue
            key = (provider, model)
            if key in seen:
                continue
            seen.add(key)
            models.append(dict(item))
    models_by_provider = _vision_models_by_provider(models)
    providers = _dedupe_strings(providers + list(models_by_provider.keys()))
    available_providers = _dedupe_strings(available_providers) or providers
    return {
        "available": bool(models or providers or available_providers),
        "route_available": True,
        "models": models,
        "models_by_provider": models_by_provider,
        "provider_models": _provider_models_from_mapping(models_by_provider),
        "providers": providers or available_providers,
        "available_providers": available_providers or providers,
        "task": task,
        "source": "abstractvision_gateway_provider_catalog",
        "stale": False,
        "refreshed_at": _utc_now_iso(),
        "error": "; ".join(errors) if errors and not models else None,
    }


def _filter_vision_provider_models_response(
    response: Dict[str, Any],
    *,
    provider: Optional[str],
    providers_only: bool,
) -> Dict[str, Any]:
    wanted = str(provider or "").strip().lower()
    items = response.get("models")
    models = [dict(x) for x in items if isinstance(x, dict)] if isinstance(items, list) else []
    models_by_provider = _provider_string_map(response.get("models_by_provider"))
    if not models_by_provider:
        models_by_provider = _vision_models_by_provider(models)
    if wanted:
        models = [
            item
            for item in models
            if str(item.get("provider") or item.get("owned_by") or "").strip().lower() == wanted
        ]
        models_by_provider = {
            key: values for key, values in models_by_provider.items() if str(key or "").strip().lower() == wanted
        }
    providers = _dedupe_strings(
        [str(x) for x in response.get("providers") or [] if isinstance(x, str)]
        + [str(x) for x in response.get("available_providers") or [] if isinstance(x, str)]
        + list(models_by_provider.keys())
        + [
            str(item.get("provider") or item.get("owned_by") or "").strip()
            for item in models
            if str(item.get("provider") or item.get("owned_by") or "").strip()
        ]
    )
    if wanted:
        providers = [p for p in providers if p.lower() == wanted]
    out = dict(response)
    out["providers"] = providers
    out["available_providers"] = [
        p
        for p in _dedupe_strings([str(x) for x in response.get("available_providers") or [] if isinstance(x, str)] or providers)
        if not wanted or p.lower() == wanted
    ] or providers
    out["models_by_provider"] = models_by_provider
    out["provider_models"] = _provider_models_from_mapping(models_by_provider)
    if providers_only:
        out["models"] = []
        out["available"] = bool(providers or out.get("available_providers"))
    else:
        out["models"] = models
        out["available"] = bool(models or models_by_provider)
    return out


def _provider_filtered_values(payload: Dict[str, Any], provider: Optional[str], *map_keys: str) -> list[str]:
    wanted = str(provider or "").strip().lower()
    values: list[str] = []
    for key in map_keys:
        mapping = payload.get(key)
        if not isinstance(mapping, dict):
            continue
        if wanted:
            for map_provider, map_values in mapping.items():
                if str(map_provider or "").strip().lower() != wanted:
                    continue
                values.extend(str(x).strip() for x in (map_values or []) if isinstance(x, str) and str(x).strip())
        else:
            for map_values in mapping.values():
                values.extend(str(x).strip() for x in (map_values or []) if isinstance(x, str) and str(x).strip())
    return _dedupe_strings(values)


def _filter_provider_model_catalog_response(
    response: Dict[str, Any],
    *,
    provider: Optional[str],
    model_keys: tuple[str, ...],
) -> Dict[str, Any]:
    wanted = str(provider or "").strip()
    if not wanted:
        return response
    wanted_key = wanted.lower()
    models = _provider_filtered_values(response, wanted, *model_keys)
    if not models:
        active = str(response.get("active_provider") or response.get("provider") or "").strip().lower()
        if active == wanted_key:
            raw = response.get("models")
            models = _dedupe_strings([str(x).strip() for x in raw if isinstance(x, str) and str(x).strip()]) if isinstance(raw, list) else []
    out = dict(response)
    out["provider"] = wanted
    out["providers"] = [wanted] if models else []
    out["models"] = models
    for key in model_keys:
        mapping = response.get(key)
        if isinstance(mapping, dict):
            out[key] = {
                str(map_provider): map_values
                for map_provider, map_values in mapping.items()
                if str(map_provider or "").strip().lower() == wanted_key
            }
    for key in ("tts_models", "stt_models", "embedding_models", "music_models"):
        if key in out:
            out[key] = list(models)
    active = str(response.get("active_provider") or response.get("provider") or "").strip().lower()
    filtered_provider_models: list[Any] = []
    raw_provider_models = response.get("provider_models")
    if isinstance(raw_provider_models, list):
        for item in raw_provider_models:
            if isinstance(item, dict):
                item_provider = str(
                    item.get("provider")
                    or item.get("owned_by")
                    or item.get("backend")
                    or item.get("provider_id")
                    or ""
                ).strip().lower()
                if item_provider == wanted_key or (not item_provider and active == wanted_key):
                    filtered_provider_models.append(item)
    if "provider_models" in out:
        out["provider_models"] = filtered_provider_models
    out["available"] = bool(models)
    return out


def _merge_provider_model_catalog_responses(
    *responses: Optional[Dict[str, Any]],
    model_keys: tuple[str, ...],
) -> Dict[str, Any]:
    merged: Dict[str, Any] = {
        "available": False,
        "route_available": True,
        "models": [],
        "providers": [],
        "source": "gateway_merged",
        "stale": False,
        "refreshed_at": _utc_now_iso(),
        "error": None,
    }
    for key in model_keys:
        merged[key] = {}
    for response in responses:
        if not isinstance(response, dict):
            continue
        merged["available"] = bool(merged.get("available") or response.get("available"))
        for key in ("models", "providers"):
            values = response.get(key)
            if isinstance(values, list):
                merged[key] = _dedupe_strings(list(merged.get(key) or []) + [str(x) for x in values if isinstance(x, str)])
        for key in model_keys:
            mapping = response.get(key)
            if not isinstance(mapping, dict):
                continue
            target = merged.setdefault(key, {})
            for provider_key, provider_models in mapping.items():
                provider_text = str(provider_key or "").strip()
                if not provider_text or not isinstance(provider_models, list):
                    continue
                current = target.get(provider_text) if isinstance(target, dict) else []
                target[provider_text] = _dedupe_strings(
                    [str(x) for x in (current or []) if isinstance(x, str)]
                    + [str(x) for x in provider_models if isinstance(x, str)]
                )
    merged["available"] = bool(merged.get("available") or merged.get("models") or merged.get("providers"))
    return merged


def _filter_voice_catalog_response(
    response: Dict[str, Any],
    *,
    provider: Optional[str],
    model: Optional[str],
    providers_only: bool,
) -> Dict[str, Any]:
    wanted_provider = str(provider or "").strip().lower()
    wanted_model = str(model or "").strip().lower()
    out = dict(response)
    record_values: list[Any] = []
    for key in ("profiles", "voices", "cloned_voices"):
        values = response.get(key)
        if isinstance(values, list):
            record_values.extend(values)
    derived_providers = [
        _voice_record_provider(item)
        for item in record_values
        if isinstance(item, dict) and _voice_record_provider(item)
    ]
    map_providers: list[str] = []
    for key in ("tts_models_by_provider", "tts_voices_by_provider", "tts_profiles_by_provider"):
        mapping = response.get(key)
        if isinstance(mapping, dict):
            map_providers.extend(str(k).strip() for k in mapping.keys() if str(k).strip())
    providers = _dedupe_strings(
        [
            str(x).strip()
            for x in list(response.get("tts_providers") or response.get("providers") or []) + derived_providers + map_providers
            if isinstance(x, str) and str(x).strip()
        ]
    )
    stt_providers = _dedupe_strings(
        [str(x).strip() for x in (response.get("stt_providers") or []) if isinstance(x, str) and str(x).strip()]
    )
    if wanted_provider:
        providers = [p for p in providers if p.lower() == wanted_provider]
        stt_providers = [p for p in stt_providers if p.lower() == wanted_provider]

    def keep_record(item: Any) -> bool:
        if not isinstance(item, dict):
            return not wanted_provider and not wanted_model
        item_provider = _voice_record_provider(item).lower()
        item_model = _voice_record_model(item).lower()
        if wanted_provider and item_provider and item_provider != wanted_provider:
            return False
        if wanted_model and item_model and item_model != wanted_model:
            return False
        return True

    if providers_only:
        out["providers"] = providers
        out["tts_providers"] = providers
        out["stt_providers"] = stt_providers
        out["profiles"] = []
        out["voices"] = []
        out["cloned_voices"] = []
        out["available"] = bool(providers or stt_providers)
        return out

    for key in ("profiles", "voices", "cloned_voices"):
        values = response.get(key)
        if isinstance(values, list):
            out[key] = [item for item in values if keep_record(item)]
    for key in ("tts_models_by_provider", "stt_models_by_provider", "tts_voices_by_provider", "tts_profiles_by_provider"):
        mapping = response.get(key)
        if isinstance(mapping, dict) and wanted_provider:
            out[key] = {k: v for k, v in mapping.items() if str(k).strip().lower() == wanted_provider}
    if wanted_provider:
        tts_models = _provider_filtered_values(response, wanted_provider, "tts_models_by_provider", "models_by_provider")
        stt_models = _provider_filtered_values(response, wanted_provider, "stt_models_by_provider")
        if not tts_models and str(response.get("active_provider") or response.get("provider") or "").strip().lower() == wanted_provider:
            raw = response.get("tts_models") or response.get("models")
            tts_models = _dedupe_strings([str(x).strip() for x in raw if isinstance(x, str) and str(x).strip()]) if isinstance(raw, list) else []
        if not stt_models and str(response.get("active_stt_provider") or "").strip().lower() == wanted_provider:
            raw = response.get("stt_models")
            stt_models = _dedupe_strings([str(x).strip() for x in raw if isinstance(x, str) and str(x).strip()]) if isinstance(raw, list) else []
        out["models"] = tts_models
        out["tts_models"] = tts_models
        if "stt_models" in out or stt_models:
            out["stt_models"] = stt_models
    out["providers"] = providers or out.get("providers") or []
    out["tts_providers"] = providers or out.get("tts_providers") or []
    out["stt_providers"] = stt_providers or out.get("stt_providers") or []
    return out


def _merge_voice_catalog_responses(*responses: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {
        "available": False,
        "route_available": True,
        "profiles": [],
        "voices": [],
        "cloned_voices": [],
        "models": [],
        "tts_models": [],
        "stt_models": [],
        "providers": [],
        "tts_providers": [],
        "stt_providers": [],
        "tts_models_by_provider": {},
        "stt_models_by_provider": {},
        "tts_voices_by_provider": {},
        "tts_profiles_by_provider": {},
        "controls": _voice_catalog_controls(),
        "source": "gateway_merged",
        "stale": False,
        "refreshed_at": _utc_now_iso(),
        "error": None,
    }
    seen_records: Dict[str, set[str]] = {"profiles": set(), "voices": set(), "cloned_voices": set()}
    for response in responses:
        if not isinstance(response, dict):
            continue
        merged["available"] = bool(merged.get("available") or response.get("available"))
        for key in ("profiles", "voices", "cloned_voices"):
            values = response.get(key)
            if not isinstance(values, list):
                continue
            target = merged.setdefault(key, [])
            for item in values:
                record_key = _voice_record_key(item)
                if record_key in seen_records[key]:
                    continue
                seen_records[key].add(record_key)
                target.append(item)
        for key in ("models", "tts_models", "stt_models", "providers", "tts_providers", "stt_providers"):
            values = response.get(key)
            if isinstance(values, list):
                merged[key] = _dedupe_strings(list(merged.get(key) or []) + [str(x) for x in values if isinstance(x, str)])
        for key in ("tts_models_by_provider", "stt_models_by_provider", "tts_voices_by_provider", "tts_profiles_by_provider"):
            mapping = response.get(key)
            if not isinstance(mapping, dict):
                continue
            target_map = merged.setdefault(key, {})
            for map_provider, map_values in mapping.items():
                provider_key = str(map_provider or "").strip()
                if not provider_key:
                    continue
                current = target_map.get(provider_key) if isinstance(target_map, dict) else []
                if isinstance(map_values, list):
                    target_map[provider_key] = _dedupe_strings(
                        [str(x) for x in (current or []) if isinstance(x, str)]
                        + [str(x) for x in map_values if isinstance(x, str)]
                    )
        if isinstance(response.get("controls"), dict):
            merged["controls"] = {**dict(merged.get("controls") or {}), **dict(response.get("controls") or {})}
    merged["available"] = bool(
        merged.get("available")
        or merged.get("profiles")
        or merged.get("voices")
        or merged.get("cloned_voices")
        or merged.get("models")
        or merged.get("tts_models")
        or merged.get("stt_models")
        or merged.get("providers")
        or merged.get("tts_providers")
        or merged.get("stt_providers")
    )
    return merged


def _static_vision_provider_models_response(*, task: Optional[str]) -> Dict[str, Any]:
    pairs: list[tuple[str, str]] = []
    configured_backend = str(_env_first("ABSTRACTGATEWAY_VISION_BACKEND", "ABSTRACTVISION_BACKEND", "ABSTRACTCORE_VISION_BACKEND") or "").strip().lower().replace("_", "-")
    for key, provider in (
        ("OPENAI_IMAGE_MODEL_ID", "openai"),
        ("OPENAI_IMAGE_MODEL", "openai"),
        ("ABSTRACTCORE_VISION_UPSTREAM_MODEL_ID", "openai-compatible"),
        ("ABSTRACTCORE_VISION_MODEL_ID", "mlx-gen" if configured_backend in {"mlx-gen", "mlxgen", "mflux", "m-flux"} else "openai-compatible"),
        ("ABSTRACTCORE_VISION_MFLUX_MODEL", "mlx-gen"),
        ("ABSTRACTGATEWAY_VISION_MFLUX_MODEL", "mlx-gen"),
        ("ABSTRACTVISION_MFLUX_MODEL", "mlx-gen"),
        ("ABSTRACTGATEWAY_VISION_MODEL_ID", "mlx-gen" if configured_backend in {"mlx-gen", "mlxgen", "mflux", "m-flux"} else "openai-compatible"),
        ("ABSTRACTVISION_MODEL_ID", "mlx-gen" if configured_backend in {"mlx-gen", "mlxgen", "mflux", "m-flux"} else "openai-compatible"),
        ("ABSTRACTVISION_DIFFUSERS_MODEL_ID", "huggingface"),
        ("ABSTRACTVISION_SDCPP_MODEL", "sdcpp"),
        ("ABSTRACTVISION_SDCPP_DIFFUSION_MODEL", "sdcpp"),
    ):
        value = str(os.getenv(key) or "").strip()
        if value:
            pairs.append((provider, value))
    if not any(provider == "openai" for provider, _ in pairs) and str(os.getenv("OPENAI_API_KEY") or os.getenv("ABSTRACTVISION_API_KEY") or "").strip():
        pairs.append(("openai", "gpt-image-1"))
    seen_values: set[tuple[str, str]] = set()
    models: list[Dict[str, Any]] = []
    for provider, value in pairs:
        key = (provider, value)
        if key in seen_values:
            continue
        seen_values.add(key)
        models.append(_gateway_vision_provider_model_item(provider=provider, model_id=value, task=task, configured=True))
    models_by_provider = _vision_models_by_provider(models)
    return {
        "available": bool(models),
        "route_available": True,
        "models": models,
        "models_by_provider": models_by_provider,
        "provider_models": _provider_models_from_mapping(models_by_provider),
        "providers": list(models_by_provider.keys()),
        "available_providers": list(models_by_provider.keys()),
        "task": task,
        "source": "gateway_static",
        "stale": False,
        "refreshed_at": _utc_now_iso(),
        "error": None,
        **(
            {}
            if models
            else {"config_hint": "Configure AbstractCore server catalog routes or ABSTRACTVISION_* model settings."}
        ),
    }


def _voice_contract_descriptor(caps: Dict[str, Any], *, kind: str) -> Dict[str, Any]:
    assert kind in {"tts", "stt"}
    abstractvoice = caps.get("abstractvoice") if isinstance(caps.get("abstractvoice"), dict) else {}
    plugin = _plugin_capability(caps, "voice" if kind == "tts" else "audio")
    plugin_available = bool(plugin.get("available")) if plugin else None
    installed = bool(abstractvoice.get("installed"))
    run_facade, run_err = _gateway_abstractcore_run_facade()
    route_available = run_facade is not None and not run_err
    configured = bool(plugin_available) or bool(
        _env_first(
            "ABSTRACTGATEWAY_VOICE_TTS_ENGINE" if kind == "tts" else "ABSTRACTGATEWAY_VOICE_STT_ENGINE",
            "ABSTRACTVOICE_TTS_ENGINE" if kind == "tts" else "ABSTRACTVOICE_STT_ENGINE",
            "ABSTRACTGATEWAY_VOICE_REMOTE_BASE_URL",
            "ABSTRACTVOICE_REMOTE_BASE_URL",
            "ABSTRACTCORE_SERVER_BASE_URL",
        )
    )
    available = bool(route_available and configured and installed and (plugin_available is not False))

    hint = ""
    if not route_available:
        hint = str(run_err or "Gateway runtime is not wired to AbstractCore durable media helpers.")
    elif not installed:
        voice_cap = caps.get("voice") if isinstance(caps.get("voice"), dict) else {}
        hint = str(voice_cap.get("install_hint") or "pip install abstractgateway")
    elif plugin_available is False:
        hint = str(plugin.get("install_hint") or "Configure AbstractVoice/AbstractCore voice or audio backend settings.")

    if kind == "tts":
        return {
            "available": available,
            "route_available": route_available,
            "installed": installed,
            "configured": configured,
            "unsupported": not installed,
            "endpoint": _api_gateway_path("/runs/{run_id}/voice/tts"),
            "catalog_endpoint": _api_gateway_path("/voice/voices"),
            "models_endpoint": _api_gateway_path("/audio/speech/models"),
            "active_model": _env_first(
                "ABSTRACTGATEWAY_VOICE_TTS_MODEL",
                "ABSTRACTVOICE_TTS_MODEL",
                "ABSTRACTVOICE_OPENAI_TTS_MODEL",
                "ABSTRACTVOICE_OPENAI_COMPATIBLE_TTS_MODEL",
                "ABSTRACTVOICE_REMOTE_TTS_MODEL",
            ),
            "delivery_modes": ["artifact"],
            "formats": ["wav", "mp3"],
            "content_types": {"wav": "audio/wav", "mp3": "audio/mpeg"},
            "controls": {
                "speed": {"supported": True, "min": 0.5, "max": 2.0, "default": 1.0},
                "quality_preset": {"supported": True, "values": ["low", "standard", "high"], "default": "standard"},
                "instructions": {"supported": True},
                "profile": {"supported": True},
                "voice_clone": {"supported": True},
            },
            "voices": _voice_profile_descriptors(),
            "streaming": False,
            "durability": "runtime_child_run",
            "returns_child_run_id": True,
            **({"selected_backend": plugin.get("selected_backend")} if plugin.get("selected_backend") else {}),
            **({"install_hint": hint} if hint and not installed else {}),
            **({"config_hint": hint} if hint and installed else {}),
        }

    policy = _server_workspace_policy_public()
    return {
        "available": available,
        "route_available": route_available,
        "installed": installed,
        "configured": configured,
        "unsupported": not installed,
        "endpoint": _api_gateway_path("/runs/{run_id}/audio/transcribe"),
        "models_endpoint": _api_gateway_path("/audio/transcriptions/models"),
        "active_model": _env_first(
            "ABSTRACTGATEWAY_VOICE_STT_MODEL",
            "ABSTRACTVOICE_STT_MODEL",
            "ABSTRACTVOICE_OPENAI_STT_MODEL",
            "ABSTRACTVOICE_OPENAI_COMPATIBLE_STT_MODEL",
            "ABSTRACTVOICE_REMOTE_STT_MODEL",
        ),
        "input_modes": ["artifact"],
        "upload_endpoint": _api_gateway_path("/attachments/upload"),
        "content_types": ["audio/wav", "audio/mpeg", "audio/mp4", "audio/webm", "audio/ogg", "application/octet-stream"],
        "languages": [],
        "max_upload_bytes": int(policy.get("max_attachment_bytes") or 0),
        "durability": "runtime_child_run",
        "returns_child_run_id": True,
        **({"selected_backend": plugin.get("selected_backend")} if plugin.get("selected_backend") else {}),
        **({"install_hint": hint} if hint and not installed else {}),
        **({"config_hint": hint} if hint and installed else {}),
    }


def _listen_voice_contract_descriptor(*, stt: Dict[str, Any]) -> Dict[str, Any]:
    policy = _server_workspace_policy_public()
    content_types = list(stt.get("content_types") or ["audio/wav", "audio/mpeg", "audio/webm"])
    upload_endpoint = str(stt.get("upload_endpoint") or _api_gateway_path("/attachments/upload"))
    transcribe_endpoint = str(stt.get("endpoint") or _api_gateway_path("/runs/{run_id}/audio/transcribe"))
    models_endpoint = str(stt.get("models_endpoint") or _api_gateway_path("/audio/transcriptions/models"))
    out: Dict[str, Any] = {
        "available": True,
        "host_capture_required": True,
        "transcription_available": bool(stt.get("available")),
        "transcribe_route_available": bool(stt.get("route_available")),
        "upload_endpoint": upload_endpoint,
        "transcribe_endpoint": transcribe_endpoint,
        "models_endpoint": models_endpoint,
        "input_modes": ["artifact"],
        "content_types": content_types,
        "max_upload_bytes": int(policy.get("max_attachment_bytes") or 0),
        "durability": "workflow_wait_event",
        "wait_event_transport": "emit_event",
        "commands_endpoint": _api_gateway_path("/commands"),
        "commands_type": "emit_event",
        "returns_audio_artifact": True,
        "returns_text": True,
        "wait_details": {
            "input_mode": "voice",
            "event_payload": {
                "audio_artifact": {"$artifact": "..."},
                "text": "optional host transcript",
            },
        },
    }
    if stt.get("durability"):
        out["transcription_durability"] = stt.get("durability")
    if stt.get("returns_child_run_id") is not None:
        out["transcription_returns_child_run_id"] = bool(stt.get("returns_child_run_id"))
    hint = str(stt.get("config_hint") or stt.get("install_hint") or "").strip()
    if hint:
        out["transcription_config_hint"] = hint
    return out


def _gateway_music_provider_id(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if not isinstance(value, dict):
        return ""
    for key in ("provider", "provider_id", "backend_id", "id", "name"):
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return ""


def _gateway_music_capability_probe() -> Dict[str, Any]:
    task = "text_to_music"
    discovery, err = _gateway_abstractcore_discovery_facade()
    if err or discovery is None:
        return _runtime_discovery_payload(
            {
                "available": False,
                "task": task,
                "providers": [],
                "available_providers": [],
                "provider_details": [],
                "error": err or "Gateway runtime does not expose AbstractCore discovery helpers.",
            }
        )
    try:
        payload = discovery.list_music_providers(task=task)
    except Exception as exc:
        return _runtime_discovery_payload(
            {
                "available": False,
                "task": task,
                "providers": [],
                "available_providers": [],
                "provider_details": [],
                "error": str(exc),
            }
        )

    out = _runtime_discovery_payload(payload)
    provider_details = [dict(item) for item in out.get("provider_details") or [] if isinstance(item, dict)]
    providers = _dedupe_strings(
        [str(x).strip() for x in out.get("providers") or [] if isinstance(x, str) and str(x).strip()]
        + [str(x).strip() for x in out.get("available_providers") or [] if isinstance(x, str) and str(x).strip()]
        + [_gateway_music_provider_id(item) for item in provider_details if _gateway_music_provider_id(item)]
    )
    out["task"] = str(out.get("task") or task)
    out["providers"] = providers
    out["available_providers"] = _dedupe_strings(
        [str(x).strip() for x in out.get("available_providers") or [] if isinstance(x, str) and str(x).strip()]
        or providers
    )
    out["provider_details"] = provider_details
    out["available"] = bool(out.get("available") or providers or out["available_providers"] or provider_details)
    return out


def _generated_media_contract(caps: Dict[str, Any]) -> Dict[str, Any]:
    vision_plugin = _plugin_capability(caps, "vision")
    abstractvision = caps.get("abstractvision") if isinstance(caps.get("abstractvision"), dict) else {}
    run_facade, run_err = _gateway_abstractcore_run_facade()
    direct_route_available = run_facade is not None and not run_err
    direct_configured = bool(direct_route_available and _gateway_direct_image_configured())
    workflow_image_available = bool(
        direct_route_available and direct_configured and (vision_plugin.get("available") or abstractvision.get("installed"))
    )
    voice_direct = _voice_contract_descriptor(caps, kind="tts")
    music_plugin = _plugin_capability(caps, "music")
    music_route_available = bool(music_plugin.get("route_available")) if music_plugin else bool(direct_route_available)
    music_configured = bool(music_plugin.get("configured")) if music_plugin else False
    music_available = bool(music_plugin.get("available")) if music_plugin else False
    music_hint = str(music_plugin.get("config_hint") or music_plugin.get("error") or "").strip() if music_plugin else ""
    return {
        "generated_image": {
            "workflow": {
                "available": workflow_image_available,
                "backend": vision_plugin.get("selected_backend") if isinstance(vision_plugin, dict) else None,
                "event_contract": "workflow_artifact",
                **(
                    {"config_hint": str(vision_plugin.get("install_hint") or "Install/configure AbstractVision or an AbstractCore vision backend.")}
                    if not workflow_image_available
                    else {}
                ),
            },
            "direct_endpoint": {
                "available": direct_configured,
                "route_available": direct_route_available,
                "endpoint": _api_gateway_path("/runs/{run_id}/images/generate"),
                "provider_models_endpoint": _api_gateway_path("/vision/provider_models"),
                "provider_models_task": "text_to_image",
                "durability": "runtime_child_run",
                "returns_child_run_id": True,
                "formats": ["png", "jpeg", "webp"],
                "max_image_bytes": _gateway_image_generation_max_bytes(),
                **(
                    {"config_hint": str(run_err or "Gateway runtime is not wired to AbstractCore durable media helpers.")}
                    if not direct_route_available
                    else {"config_hint": "Configure AbstractVision or an AbstractCore-backed image runtime for generated images."}
                    if not direct_configured
                    else {}
                ),
            },
        },
        "edited_image": {
            "workflow": {
                "available": workflow_image_available,
                "backend": vision_plugin.get("selected_backend") if isinstance(vision_plugin, dict) else None,
                "event_contract": "workflow_artifact",
                **(
                    {"config_hint": str(vision_plugin.get("install_hint") or "Install/configure AbstractVision or an AbstractCore vision backend.")}
                    if not workflow_image_available
                    else {}
                ),
            },
            "direct_endpoint": {
                "available": direct_configured,
                "route_available": direct_route_available,
                "endpoint": _api_gateway_path("/runs/{run_id}/images/edit"),
                "provider_models_endpoint": _api_gateway_path("/vision/provider_models"),
                "provider_models_task": "image_to_image",
                "input_modes": ["artifact"],
                "mask_supported": True,
                "durability": "runtime_child_run",
                "returns_child_run_id": True,
                "formats": ["png", "jpeg", "webp"],
                "max_image_bytes": _gateway_image_generation_max_bytes(),
                **(
                    {"config_hint": str(run_err or "Gateway runtime is not wired to AbstractCore durable media helpers.")}
                    if not direct_route_available
                    else {"config_hint": "Configure AbstractVision or an AbstractCore-backed image runtime for image edits."}
                    if not direct_configured
                    else {}
                ),
            },
        },
        "generated_voice": {
            "direct_endpoint": {
                "available": bool(voice_direct.get("available")),
                "route_available": bool(voice_direct.get("route_available")),
                "endpoint": _api_gateway_path("/runs/{run_id}/voice/tts"),
                "durability": "runtime_child_run",
                "returns_child_run_id": True,
                **({"config_hint": str(voice_direct.get("config_hint"))} if voice_direct.get("config_hint") else {}),
            },
            "workflow": {"available": bool(_plugin_capability(caps, "voice").get("available"))},
        },
        "generated_music": {
            "workflow": {
                "available": music_available,
                **({"backend": music_plugin.get("selected_backend")} if music_plugin.get("selected_backend") else {}),
                "event_contract": "workflow_artifact",
                **(
                    {"config_hint": music_hint or "Use the direct generated-music endpoint through Runtime-backed Gateway media helpers."}
                    if not music_available
                    else {}
                ),
            },
            "direct_endpoint": {
                "available": music_available,
                "route_available": music_route_available,
                "configured": music_configured,
                "endpoint": _api_gateway_path("/runs/{run_id}/music/generate"),
                "providers_endpoint": _api_gateway_path("/audio/music/providers"),
                "provider_models_endpoint": _api_gateway_path("/audio/music/models"),
                "provider_models_task": "text_to_music",
                "delivery_modes": ["artifact"],
                "durability": "runtime_child_run",
                "returns_child_run_id": True,
                "formats": ["wav", "mp3", "flac"],
                "content_types": {"wav": "audio/wav", "mp3": "audio/mpeg", "flac": "audio/flac"},
                **({"selected_backend": music_plugin.get("selected_backend")} if music_plugin.get("selected_backend") else {}),
                **(
                    {"config_hint": music_hint or "Gateway runtime is not wired to AbstractCore durable media helpers."}
                    if not music_route_available
                    else {"config_hint": music_hint or "Configure an AbstractCore music backend or remote Core server music route."}
                    if not music_configured
                    else {}
                ),
            },
        },
    }


def _memory_contract_descriptor(caps: Dict[str, Any]) -> Dict[str, Any]:
    abstractmemory = caps.get("abstractmemory") if isinstance(caps.get("abstractmemory"), dict) else {}
    try:
        base_dir = Path(os.getenv("ABSTRACTGATEWAY_DATA_DIR", "./runtime")).expanduser().resolve()
        cfg = resolve_memory_store_config(base_dir=base_dir)
        backend = cfg.backend
        embedding_error = ""
        try:
            embedding_route = resolve_embedding_config(base_dir=base_dir)
            embedder_configured = bool(embedding_route.provider and embedding_route.model)
        except Exception as e:
            embedding_route = None
            embedder_configured = False
            embedding_error = str(e)
        vector_capable = backend == "lancedb" or (backend == "memory" and embedder_configured)
        backend_error = memory_backend_unavailable_reason(backend)
        backend_ready = backend_error is None
        return {
            "installed": bool(abstractmemory.get("installed")),
            "route_available": True,
            "available": bool(abstractmemory.get("installed"))
            and backend_ready
            and (memory_store_exists(cfg) or backend == "memory"),
            "endpoint": _api_gateway_path("/kg/query"),
            "backend": backend,
            "path": str(cfg.path) if cfg.path is not None else None,
            "persistent": backend in {"lancedb", "sqlite"},
            "structured_query": bool(abstractmemory.get("installed")) and backend_ready,
            "semantic_query": bool(abstractmemory.get("installed"))
            and backend_ready
            and bool(vector_capable)
            and bool(embedder_configured),
            "vector_capable": bool(vector_capable),
            "embedder_configured": bool(embedder_configured),
            **(
                {
                    "embedding_route": {
                        "key": "embedding.text",
                        "provider": embedding_route.provider,
                        "model": embedding_route.model,
                        "authority": embedding_route.authority,
                    }
                }
                if embedding_route is not None
                else {}
            ),
            **({} if abstractmemory.get("installed") else {"install_hint": "pip install abstractgateway"}),
            **({} if backend_ready else {"error": backend_error}),
            **(
                {
                    "config_hint": (
                        "Set ABSTRACTGATEWAY_MEMORY_STORE_BACKEND=lancedb and configure the execution-host "
                        "embedding.text capability default for semantic KG queries."
                    ),
                    **({"embedding_error": embedding_error} if embedding_error else {}),
                }
                if backend != "lancedb" or not embedder_configured
                else {}
            ),
        }
    except Exception as e:
        return {
            "installed": bool(abstractmemory.get("installed")),
            "route_available": True,
            "available": False,
            "error": str(e),
        }


def _gateway_code_execution_contract_descriptor() -> Dict[str, Any]:
    """Advertise the execution-host policy for VisualFlow Code nodes."""
    fallback = {
        "contract": "code_execution_policy_v1",
        "version": 1,
        "available": True,
        "default_mode": "sandbox",
        "simulate": {
            "available": True,
            "endpoint": _api_gateway_path("/visualflows/code/simulate"),
        },
        "modes": [
            {
                "id": "sandbox",
                "label": "Sandbox",
                "available": True,
                "default": True,
                "safety": "limited_builtins",
                "description": "Protected Python execution with imports and unsafe constructs disabled.",
            }
        ],
    }
    try:
        from abstractruntime.visualflow_compiler.visual.code_executor import get_code_execution_policy

        policy = get_code_execution_policy()
        if not isinstance(policy, dict):
            return fallback
        out = dict(policy)
        out.setdefault("contract", "code_execution_policy_v1")
        out.setdefault("version", 1)
        out.setdefault("available", True)
        out.setdefault("default_mode", "sandbox")
        out["simulate"] = {
            "available": True,
            "endpoint": _api_gateway_path("/visualflows/code/simulate"),
        }
        return out
    except Exception as e:
        out = dict(fallback)
        out["warning"] = str(e)
        return out


def _build_client_capability_contracts(caps: Dict[str, Any]) -> Dict[str, Any]:
    """Build the versioned thin-client capability contract from cheap probes."""
    prompt_cache_control, prompt_cache_err = _gateway_abstractcore_host_facade()
    prompt_cache_available = bool(prompt_cache_control is not None and not prompt_cache_err)
    prompt_cache_endpoints = {
        "capabilities": _api_gateway_path("/prompt_cache/capabilities"),
        "stats": _api_gateway_path("/prompt_cache/stats"),
        "set": _api_gateway_path("/prompt_cache/set"),
        "update": _api_gateway_path("/prompt_cache/update"),
        "fork": _api_gateway_path("/prompt_cache/fork"),
        "clear": _api_gateway_path("/prompt_cache/clear"),
        "prepare_modules": _api_gateway_path("/prompt_cache/prepare_modules"),
    }
    durable_blocs = _gateway_durable_bloc_contract_descriptor()
    flow_publish_available = bool((caps.get("visualflow") if isinstance(caps.get("visualflow"), dict) else {}).get("installed"))

    code_execution = _gateway_code_execution_contract_descriptor()

    common = {
        "runs": {
            "start": {"available": True, "endpoint": _api_gateway_path("/runs/start")},
            "schedule": {"available": True, "endpoint": _api_gateway_path("/runs/schedule")},
            "summary": {"available": True, "endpoint": _api_gateway_path("/runs/{run_id}")},
            "list": {"available": True, "endpoint": _api_gateway_path("/runs")},
            "purge_drafts": {"available": True, "endpoint": _api_gateway_path("/runs/purge_drafts")},
            "input_data": {"available": True, "endpoint": _api_gateway_path("/runs/{run_id}/input_data")},
            "history_bundle": {"available": True, "endpoint": _api_gateway_path("/runs/{run_id}/history_bundle")},
            "commands": {
                "available": True,
                "endpoint": _api_gateway_path("/commands"),
                "types": ["pause", "resume", "cancel", "emit_event", "update_schedule", "compact_memory"],
            },
        },
        "ledger": {
            "replay": {"available": True, "endpoint": _api_gateway_path("/runs/{run_id}/ledger")},
            "batch": {"available": True, "endpoint": _api_gateway_path("/runs/ledger/batch")},
            "stream": {"available": True, "endpoint": _api_gateway_path("/runs/{run_id}/ledger/stream"), "transport": "sse"},
        },
        "artifacts": {
            "list": {"available": True, "endpoint": _api_gateway_path("/runs/{run_id}/artifacts")},
            "metadata": {"available": True, "endpoint": _api_gateway_path("/runs/{run_id}/artifacts/{artifact_id}")},
            "content": {"available": True, "endpoint": _api_gateway_path("/runs/{run_id}/artifacts/{artifact_id}/content")},
        },
        "attachments": {
            "upload": {"available": True, "endpoint": _api_gateway_path("/attachments/upload")},
            "max_upload_bytes": int(_server_workspace_policy_public().get("max_attachment_bytes") or 0),
        },
        "workspace": {"policy_endpoint": _api_gateway_path("/workspace/policy")},
        "configuration": {
            "capability_defaults": {
                "available": True,
                "endpoint": _api_gateway_path("/config/capability-defaults"),
                "item_endpoint": _api_gateway_path("/config/capability-defaults/{kind}/{modality}"),
                "schema": "capability_defaults_v1",
            },
        },
        "execution": {
            "code": code_execution,
        },
        "discovery": {
            "capabilities": _api_gateway_path("/discovery/capabilities"),
            "providers": _api_gateway_path("/discovery/providers"),
            "provider_models": _api_gateway_path("/discovery/providers/{provider_name}/models"),
            "model_capabilities": _api_gateway_path("/discovery/models/capabilities"),
            "voice_voices": _api_gateway_path("/voice/voices"),
            "audio_speech_models": _api_gateway_path("/audio/speech/models"),
            "audio_transcription_models": _api_gateway_path("/audio/transcriptions/models"),
            "audio_music_providers": _api_gateway_path("/audio/music/providers"),
            "audio_music_models": _api_gateway_path("/audio/music/models"),
            "embedding_models": _api_gateway_path("/embeddings/models"),
            "vision_provider_models": _api_gateway_path("/vision/provider_models"),
            "vision_models": _api_gateway_path("/vision/models"),
            "tools": _api_gateway_path("/discovery/tools"),
            "semantics": _api_gateway_path("/semantics"),
            "catalog_contract": _gateway_catalog_contract_descriptor(),
        },
        "prompt_cache": {
            "provider_controls": True,
            "provider_controls_available": prompt_cache_available,
            "session_lifecycle": True,
            "session_lifecycle_available": prompt_cache_available,
            "durable_blocs": durable_blocs,
            "endpoints": prompt_cache_endpoints,
            "session_endpoints": {
                "status": _api_gateway_path("/sessions/{session_id}/prompt_cache/status"),
                "prepare": _api_gateway_path("/sessions/{session_id}/prompt_cache/prepare"),
                "clear": _api_gateway_path("/sessions/{session_id}/prompt_cache/clear"),
                "rebuild": _api_gateway_path("/sessions/{session_id}/prompt_cache/rebuild"),
            },
        },
        "model_residency": _gateway_model_residency_contract_descriptor(),
        "memory": _memory_contract_descriptor(caps),
    }
    generated_media = _generated_media_contract(caps)
    tts_contract = _voice_contract_descriptor(caps, kind="tts")
    stt_contract = _voice_contract_descriptor(caps, kind="stt")
    assistant_voice = {
        "tts": tts_contract,
        "stt": stt_contract,
        "listen": _listen_voice_contract_descriptor(stt=stt_contract),
    }

    flow_editor = {
        "available": True,
        "version": 1,
        "visualflows": {
            "crud": {
                "available": True,
                "collection_endpoint": _api_gateway_path("/visualflows"),
                "item_endpoint": _api_gateway_path("/visualflows/{flow_id}"),
            },
            "publish": {
                "available": flow_publish_available,
                "endpoint": _api_gateway_path("/visualflows/{flow_id}/publish"),
                **({} if flow_publish_available else {"install_hint": "pip install abstractgateway"}),
            },
        },
        "bundles": {
            "list": {"available": True, "endpoint": _api_gateway_path("/bundles")},
            "inspect": {"available": True, "endpoint": _api_gateway_path("/bundles/{bundle_id}")},
            "upload": {"available": True, "endpoint": _api_gateway_path("/bundles/upload")},
            "reload": {"available": True, "endpoint": _api_gateway_path("/bundles/reload")},
            "remove": {"available": True, "endpoint": _api_gateway_path("/bundles/{bundle_id}")},
            "deprecate": {"available": True, "endpoint": _api_gateway_path("/bundles/{bundle_id}/deprecate")},
            "undeprecate": {"available": True, "endpoint": _api_gateway_path("/bundles/{bundle_id}/undeprecate")},
            "flow": {"available": True, "endpoint": _api_gateway_path("/bundles/{bundle_id}/flows/{flow_id}")},
        },
        "run_input_schema": {
            "available": True,
            "endpoint": _api_gateway_path("/bundles/{bundle_id}/flows/{flow_id}/input_schema"),
            "version": 1,
        },
        "runs": common["runs"],
        "ledger": common["ledger"],
        "artifacts": common["artifacts"],
        "voice": assistant_voice,
        "model_residency": common["model_residency"],
        "media": generated_media,
        "helpers": {
            "providers": common["discovery"]["providers"],
            "tools": common["discovery"]["tools"],
            "semantics": common["discovery"]["semantics"],
            "workspace_policy": common["workspace"]["policy_endpoint"],
            "kg_query": _api_gateway_path("/kg/query"),
            "embeddings": _api_gateway_path("/embeddings"),
        },
        "execution": {
            "code": common["execution"]["code"],
        },
    }

    assistant = {
        "available": True,
        "version": 1,
        "runs": common["runs"],
        "ledger": common["ledger"],
        "artifacts": common["artifacts"],
        "voice": assistant_voice,
        "media": generated_media,
        "model_residency": common["model_residency"],
        "prompt_cache": {
            "provider_controls": True,
            "provider_controls_available": prompt_cache_available,
            "session_lifecycle": True,
            "session_lifecycle_available": prompt_cache_available,
            "durable_blocs": durable_blocs,
            "capabilities_endpoint": prompt_cache_endpoints["capabilities"],
            "stats_endpoint": prompt_cache_endpoints["stats"],
            "session_status_endpoint": _api_gateway_path("/sessions/{session_id}/prompt_cache/status"),
            "session_prepare_endpoint": _api_gateway_path("/sessions/{session_id}/prompt_cache/prepare"),
            "session_clear_endpoint": _api_gateway_path("/sessions/{session_id}/prompt_cache/clear"),
            "session_rebuild_endpoint": _api_gateway_path("/sessions/{session_id}/prompt_cache/rebuild"),
        },
    }
    common["readiness"] = _gateway_readiness_descriptor(
        common=common,
        flow_editor=flow_editor,
        assistant=assistant,
    )

    return {
        "version": 1,
        "common": common,
        "flow_editor": flow_editor,
        "assistant": assistant,
        "abstractcode": {
            "available": True,
            "version": 1,
            "runs": common["runs"],
            "ledger": common["ledger"],
            "artifacts": common["artifacts"],
            "workspace": common["workspace"],
            "model_residency": common["model_residency"],
            "prompt_cache": assistant["prompt_cache"],
        },
    }


@router.get("/discovery/capabilities")
async def discovery_capabilities() -> Dict[str, Any]:
    """List optional gateway capabilities for thin clients (best-effort)."""
    caps: Dict[str, Any] = {}

    caps["abstractgateway"] = _optional_package_status("abstractgateway", "abstractgateway")
    caps["abstractruntime"] = _optional_package_status("abstractruntime", "AbstractRuntime")
    caps["abstractcore"] = _optional_package_status("abstractcore", "abstractcore")
    caps["abstractmemory"] = _optional_package_status("abstractmemory", "AbstractMemory")
    caps["abstractvoice"] = _optional_package_status("abstractvoice", "abstractvoice")
    caps["abstractvision"] = _optional_package_status("abstractvision", "abstractvision")

    if bool(caps["abstractvoice"].get("installed")):
        caps["voice"] = {
            "installed": True,
            "version": caps["abstractvoice"].get("version"),
        }
    else:
        caps["voice"] = {
            "installed": False,
            "error": str(caps["abstractvoice"].get("error") or ""),
            "install_hint": "pip install abstractgateway",
        }

    host_facade, host_err = _gateway_abstractcore_host_facade()
    discovery_facade, discovery_err = _gateway_abstractcore_discovery_facade()
    run_facade, run_err = _gateway_abstractcore_run_facade()
    music_probe = _gateway_music_capability_probe()
    music_route_available = bool(run_facade is not None and not run_err)
    music_configured = bool(
        music_probe.get("available")
        or music_probe.get("providers")
        or music_probe.get("available_providers")
        or music_probe.get("provider_details")
    )
    music_hint = ""
    if not music_route_available:
        music_hint = str(run_err or "Gateway runtime is not wired to AbstractCore durable media helpers.")
    elif not music_configured:
        music_hint = str(
            music_probe.get("error")
            or "Configure an AbstractCore music backend or remote Core server music route."
        )
    plugin_errors = [str(err) for err in (host_err, discovery_err, run_err) if isinstance(err, str) and err.strip()]
    music_probe_error = music_probe.get("error")
    if isinstance(music_probe_error, str) and music_probe_error.strip():
        plugin_errors.append(music_probe_error.strip())
    plugin_caps = {
        "voice": {"available": bool(run_facade is not None and not run_err)},
        "audio": {"available": bool(run_facade is not None and not run_err)},
        "vision": {"available": bool((run_facade is not None and not run_err) or (discovery_facade is not None and not discovery_err))},
        "music": {
            "available": bool(music_route_available and music_configured),
            "route_available": music_route_available,
            "configured": music_configured,
            "task": str(music_probe.get("task") or "text_to_music"),
            "providers": list(music_probe.get("providers") or []) if isinstance(music_probe.get("providers"), list) else [],
            "available_providers": list(music_probe.get("available_providers") or [])
            if isinstance(music_probe.get("available_providers"), list)
            else [],
            **({"config_hint": music_hint} if music_hint else {}),
            **({"error": str(music_probe.get("error"))} if music_probe.get("error") else {}),
        },
    }
    caps["capability_plugins"] = {
        "installed": bool(caps["abstractruntime"].get("installed")) and bool(caps["abstractcore"].get("installed")),
        "group": "abstractruntime.integrations.abstractcore",
        "plugins_seen": [],
        "plugin_errors": plugin_errors,
        "capabilities": plugin_caps,
    }
    caps["multimodal"] = {
        "installed": bool(caps["abstractruntime"].get("installed")) and bool(caps["abstractcore"].get("installed")),
        "capabilities": {name: bool(data.get("available")) for name, data in plugin_caps.items()},
        "runtime_output_specs": _optional_package_status(
            "abstractruntime.integrations.abstractcore.output_specs",
            "AbstractRuntime",
        ).get("installed"),
    }

    try:
        from abstractruntime.integrations.abstractcore.default_tools import list_default_tool_specs

        specs = list_default_tool_specs()
        caps["tools"] = {"installed": True, "count": len(specs) if isinstance(specs, list) else None}
    except Exception as e:
        caps["tools"] = {"installed": False, "error": str(e)}

    # VisualFlow editor support: Gateway stores VisualFlow JSON records and publishes `.flow`
    # WorkflowBundles. The Gateway execution plane does not depend on AbstractFlow.
    caps["visualflow"] = {"installed": True}

    caps["vision_fallback"] = {"installed": bool(caps["abstractcore"].get("installed"))}
    caps["media"] = {"installed": bool(caps["abstractcore"].get("installed"))}

    caps["contracts"] = _build_client_capability_contracts(caps)

    return {"capabilities": caps}


@router.get("/voice/voices")
async def voice_voices_catalog(
    request: Request,
    base_url: Optional[str] = Query(
        default=None,
        description="Optional provider/catalog base URL forwarded to the configured AbstractCore catalog route.",
    ),
    provider: Optional[str] = Query(default=None, description="Optional TTS provider/engine filter."),
    model: Optional[str] = Query(default=None, description="Optional TTS model/language filter."),
    providers_only: bool = Query(default=False, description="Return provider names without model/voice lists."),
    compact: bool = Query(default=False, description="Return normalized catalog items without duplicated source catalog trees."),
) -> Dict[str, Any]:
    """List TTS voices through the Gateway Runtime discovery facade."""
    discovery, err = _gateway_abstractcore_discovery_facade()
    if err or discovery is None:
        out = _filter_voice_catalog_response(
            _runtime_discovery_payload(
                {
                    "available": False,
                    "profiles": [],
                    "voices": [],
                    "cloned_voices": [],
                    "models": [],
                    "tts_models": [],
                    "providers": [],
                    "tts_providers": [],
                    "tts_models_by_provider": {},
                    "tts_profiles_by_provider": {},
                    "tts_voices_by_provider": {},
                    "controls": _voice_catalog_controls(),
                    "error": err or "Gateway runtime does not expose AbstractCore discovery helpers.",
                }
            ),
            provider=provider,
            model=model,
            providers_only=providers_only,
        )
        items = (
            _gateway_catalog_provider_items(out, provider_keys=("providers", "tts_providers"))
            if providers_only
            else _gateway_catalog_voice_items(out)
        )
        return _gateway_catalog_response(
            _compact_voice_catalog_payload(out) if compact else out,
            kind="providers" if providers_only else "voices",
            scope="tts",
            items=items,
            provider=provider,
            metadata={"providers_only": providers_only, "model": model, "compact": True if compact else None},
        )
    try:
        payload = discovery.get_voice_catalog(
            base_url=base_url,
            provider_api_key=_request_provider_api_key(request),
            provider=provider,
            model=model,
            providers_only=providers_only,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    out = _filter_voice_catalog_response(
        _runtime_discovery_payload(payload),
        provider=provider,
        model=model,
        providers_only=providers_only,
    )
    items = (
        _gateway_catalog_provider_items(out, provider_keys=("providers", "tts_providers"))
        if providers_only
        else _gateway_catalog_voice_items(out)
    )
    return _gateway_catalog_response(
        _compact_voice_catalog_payload(out) if compact else out,
        kind="providers" if providers_only else "voices",
        scope="tts",
        items=items,
        provider=provider,
        metadata={"providers_only": providers_only, "model": model, "compact": True if compact else None},
    )


@router.get("/audio/speech/models")
async def audio_speech_models_catalog(
    request: Request,
    base_url: Optional[str] = Query(
        default=None,
        description="Optional provider/catalog base URL forwarded to the configured AbstractCore catalog route.",
    ),
    provider: Optional[str] = Query(default=None, description="Optional TTS provider/engine filter."),
    providers_only: bool = Query(default=False, description="Return provider names without loading the model catalog."),
) -> Dict[str, Any]:
    """List TTS model ids through the Gateway Runtime discovery facade."""
    if providers_only:
        out = _filter_voice_catalog_response(
            _static_voice_providers_only_response(),
            provider=provider,
            model=None,
            providers_only=True,
        )
        out["models"] = []
        out["models_by_provider"] = {}
        out["tts_models_by_provider"] = {}
        return _gateway_catalog_response(
            out,
            kind="providers",
            scope="tts",
            items=_gateway_catalog_provider_items(out, provider_keys=("providers", "tts_providers")),
            provider=provider,
            metadata={"providers_only": True},
        )

    discovery, err = _gateway_abstractcore_discovery_facade()
    if err or discovery is None:
        out = _filter_provider_model_catalog_response(
            _runtime_discovery_payload(
                {
                    "available": False,
                    "models": [],
                    "providers": [],
                    "models_by_provider": {},
                    "tts_models_by_provider": {},
                    "error": err or "Gateway runtime does not expose AbstractCore discovery helpers.",
                }
            ),
            provider=provider,
            model_keys=("models_by_provider", "tts_models_by_provider"),
        )
        return _gateway_catalog_response(
            out,
            kind="models",
            scope="tts",
            items=_gateway_catalog_model_items(
                out,
                provider=provider or str(out.get("provider") or out.get("active_provider") or "").strip() or None,
                detail_keys=(),
                map_keys=("models_by_provider", "tts_models_by_provider"),
                value_keys=("tts_models", "models"),
            ),
            provider=provider,
        )
    try:
        payload = discovery.list_tts_models(
            base_url=base_url,
            provider_api_key=_request_provider_api_key(request),
            provider=provider,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    out = _filter_provider_model_catalog_response(
        _runtime_discovery_payload(payload),
        provider=provider,
        model_keys=("models_by_provider", "tts_models_by_provider"),
    )
    return _gateway_catalog_response(
        out,
        kind="models",
        scope="tts",
        items=_gateway_catalog_model_items(
            out,
            provider=provider or str(out.get("provider") or out.get("active_provider") or "").strip() or None,
            detail_keys=(),
            map_keys=("models_by_provider", "tts_models_by_provider"),
            value_keys=("tts_models", "models"),
        ),
        provider=provider,
    )


@router.get("/audio/transcriptions/models")
async def audio_transcription_models_catalog(
    request: Request,
    base_url: Optional[str] = Query(
        default=None,
        description="Optional provider/catalog base URL forwarded to the configured AbstractCore catalog route.",
    ),
    provider: Optional[str] = Query(default=None, description="Optional STT provider/engine filter."),
    providers_only: bool = Query(default=False, description="Return only available STT providers without enumerating model catalogs."),
) -> Dict[str, Any]:
    """List STT model ids through the Gateway Runtime discovery facade."""
    if providers_only:
        out = _static_transcription_providers_only_response(provider)
        return _gateway_catalog_response(
            out,
            kind="providers",
            scope="stt",
            items=_gateway_catalog_provider_items(out, provider_keys=("providers", "stt_providers", "available_providers")),
            provider=provider,
            metadata={"providers_only": True},
        )

    discovery, err = _gateway_abstractcore_discovery_facade()
    if err or discovery is None:
        out = _filter_provider_model_catalog_response(
            _runtime_discovery_payload(
                {
                    "available": False,
                    "models": [],
                    "providers": [],
                    "models_by_provider": {},
                    "stt_models_by_provider": {},
                    "error": err or "Gateway runtime does not expose AbstractCore discovery helpers.",
                }
            ),
            provider=provider,
            model_keys=("models_by_provider", "stt_models_by_provider"),
        )
        return _gateway_catalog_response(
            out,
            kind="models",
            scope="stt",
            items=_gateway_catalog_model_items(
                out,
                provider=provider or str(out.get("provider") or out.get("active_provider") or "").strip() or None,
                detail_keys=(),
                map_keys=("models_by_provider", "stt_models_by_provider"),
                value_keys=("stt_models", "models"),
            ),
            provider=provider,
        )
    try:
        payload = discovery.list_stt_models(
            base_url=base_url,
            provider_api_key=_request_provider_api_key(request),
            provider=provider,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    out = _filter_provider_model_catalog_response(
        _runtime_discovery_payload(payload),
        provider=provider,
        model_keys=("models_by_provider", "stt_models_by_provider"),
    )
    return _gateway_catalog_response(
        out,
        kind="models",
        scope="stt",
        items=_gateway_catalog_model_items(
            out,
            provider=provider or str(out.get("provider") or out.get("active_provider") or "").strip() or None,
            detail_keys=(),
            map_keys=("models_by_provider", "stt_models_by_provider"),
            value_keys=("stt_models", "models"),
        ),
        provider=provider,
    )


@router.get("/audio/music/providers")
async def audio_music_providers_catalog(
    request: Request,
    task: Optional[str] = Query(default=None, description="Optional music capability task filter, usually text_to_music."),
    base_url: Optional[str] = Query(
        default=None,
        description="Optional provider/catalog base URL forwarded to the configured AbstractCore catalog route.",
    ),
) -> Dict[str, Any]:
    """List music providers through the Gateway Runtime discovery facade."""
    task_value = str(task or "").strip() or "text_to_music"
    discovery, err = _gateway_abstractcore_discovery_facade()
    if err or discovery is None:
        out = _runtime_discovery_payload(
            {
                "available": False,
                "task": task_value,
                "providers": [],
                "available_providers": [],
                "provider_details": [],
                "error": err or "Gateway runtime does not expose AbstractCore discovery helpers.",
            }
        )
        return _gateway_catalog_response(
            out,
            kind="providers",
            scope="music",
            items=_gateway_catalog_provider_items(out, provider_keys=("providers", "available_providers"), detail_keys=("provider_details",)),
            task=task_value,
        )
    try:
        payload = discovery.list_music_providers(
            task=task_value,
            base_url=base_url,
            provider_api_key=_request_provider_api_key(request),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    out = _runtime_discovery_payload(payload)
    out.setdefault("task", task_value)
    out.setdefault("providers", [])
    out.setdefault("available_providers", [])
    out.setdefault("provider_details", [])
    return _gateway_catalog_response(
        out,
        kind="providers",
        scope="music",
        items=_gateway_catalog_provider_items(out, provider_keys=("providers", "available_providers"), detail_keys=("provider_details",)),
        task=task_value,
    )


@router.get("/audio/music/models")
async def audio_music_models_catalog(
    request: Request,
    task: Optional[str] = Query(default=None, description="Optional music capability task filter, usually text_to_music."),
    base_url: Optional[str] = Query(
        default=None,
        description="Optional provider/catalog base URL forwarded to the configured AbstractCore catalog route.",
    ),
    provider: Optional[str] = Query(default=None, description="Optional music provider filter."),
) -> Dict[str, Any]:
    """List music model ids through the Gateway Runtime discovery facade."""
    task_value = str(task or "").strip() or "text_to_music"
    discovery, err = _gateway_abstractcore_discovery_facade()
    if err or discovery is None:
        out = _filter_provider_model_catalog_response(
            _runtime_discovery_payload(
                {
                    "available": False,
                    "task": task_value,
                    "provider": provider,
                    "models": [],
                    "providers": [],
                    "available_providers": [],
                    "models_by_provider": {},
                    "provider_models": [],
                    "error": err or "Gateway runtime does not expose AbstractCore discovery helpers.",
                }
            ),
            provider=provider,
            model_keys=("models_by_provider",),
        )
        return _gateway_catalog_response(
            out,
            kind="models",
            scope="music",
            items=_gateway_catalog_model_items(
                out,
                provider=provider,
                task=task_value,
                detail_keys=("provider_models", "models"),
                map_keys=("models_by_provider",),
                value_keys=("models",),
            ),
            task=task_value,
            provider=provider,
        )
    try:
        payload = discovery.list_music_models(
            task=task_value,
            base_url=base_url,
            provider_api_key=_request_provider_api_key(request),
            provider=provider,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    out = _filter_provider_model_catalog_response(
        _runtime_discovery_payload(payload),
        provider=provider,
        model_keys=("models_by_provider",),
    )
    return _gateway_catalog_response(
        out,
        kind="models",
        scope="music",
        items=_gateway_catalog_model_items(
            out,
            provider=provider,
            task=task_value,
            detail_keys=("provider_models", "models"),
            map_keys=("models_by_provider",),
            value_keys=("models",),
        ),
        task=task_value,
        provider=provider,
    )


@router.get("/vision/provider_models")
async def vision_provider_models_catalog(
    request: Request,
    task: Optional[str] = Query(
        default=None,
        description="Optional provider catalog task filter: text_to_image, image_to_image, text_to_video, or image_to_video.",
    ),
    base_url: Optional[str] = Query(
        default=None,
        description="Optional provider/catalog base URL forwarded to the configured AbstractCore catalog route.",
    ),
    provider: Optional[str] = Query(default=None, description="Optional image provider/backend filter."),
    providers_only: bool = Query(default=False, description="Return provider names without model lists."),
) -> Dict[str, Any]:
    """List Vision provider model catalogs through the Gateway Runtime discovery facade."""
    task_value = str(task or "").strip() or None
    if task_value and task_value not in {"text_to_image", "image_to_image", "text_to_video", "image_to_video"}:
        raise HTTPException(
            status_code=400,
            detail="task must be one of: text_to_image, image_to_image, text_to_video, image_to_video",
        )
    discovery, err = _gateway_abstractcore_discovery_facade()
    if err or discovery is None:
        out = _filter_vision_provider_models_response(
            _runtime_discovery_payload(
                {
                    "available": False,
                    "models": [],
                    "providers": [],
                    "available_providers": [],
                    "models_by_provider": {},
                    "provider_models": [],
                    "task": task_value,
                    "error": err or "Gateway runtime does not expose AbstractCore discovery helpers.",
                }
            ),
            provider=provider,
            providers_only=providers_only,
        )
        return _gateway_catalog_response(
            out,
            kind="providers" if providers_only else "models",
            scope="vision",
            items=_gateway_catalog_provider_items(out, provider_keys=("available_providers",))
            if providers_only
            else _gateway_catalog_model_items(
                out,
                provider=provider,
                task=task_value,
                detail_keys=("models", "provider_models"),
                map_keys=("models_by_provider",),
                value_keys=("models",),
            ),
            task=task_value,
            provider=provider,
            metadata={"providers_only": providers_only},
        )
    try:
        payload = discovery.list_vision_provider_models(
            task=task_value,
            base_url=base_url,
            provider_api_key=_request_provider_api_key(request),
            provider=provider,
            providers_only=providers_only,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    out = _filter_vision_provider_models_response(
        _runtime_discovery_payload(payload),
        provider=provider,
        providers_only=providers_only,
    )
    return _gateway_catalog_response(
        out,
        kind="providers" if providers_only else "models",
        scope="vision",
        items=_gateway_catalog_provider_items(out, provider_keys=("available_providers",))
        if providers_only
        else _gateway_catalog_model_items(
            out,
            provider=provider,
            task=task_value,
            detail_keys=("models", "provider_models"),
            map_keys=("models_by_provider",),
            value_keys=("models",),
        ),
        task=task_value,
        provider=provider,
        metadata={"providers_only": providers_only},
    )


@router.get("/vision/models")
async def vision_models_catalog(request: Request) -> Dict[str, Any]:
    """List cached/local AbstractVision models through the Gateway Runtime discovery facade."""
    discovery, err = _gateway_abstractcore_discovery_facade()
    if err or discovery is None:
        out = _runtime_discovery_payload(
            {
                "available": False,
                "models": [],
                "error": err or "Gateway runtime does not expose AbstractCore discovery helpers.",
                "config_hint": "Gateway runtime must be wired to AbstractCore discovery helpers for vision model catalogs.",
            }
        )
        return _gateway_catalog_response(
            out,
            kind="models",
            scope="vision",
            items=_gateway_catalog_model_items(out, detail_keys=("models",), value_keys=("models",)),
        )
    try:
        payload = discovery.list_cached_vision_models(
            provider_api_key=_request_provider_api_key(request),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    out = _runtime_discovery_payload(payload)
    return _gateway_catalog_response(
        out,
        kind="models",
        scope="vision",
        items=_gateway_catalog_model_items(out, detail_keys=("models",), value_keys=("models",)),
    )


@router.get("/embeddings/models")
async def embedding_models_catalog(
    request: Request,
    base_url: Optional[str] = Query(
        default=None,
        description="Optional provider/catalog base URL forwarded to the configured AbstractCore embedding catalog route.",
    ),
    provider: Optional[str] = Query(default=None, description="Optional text embedding provider filter."),
    providers_only: bool = Query(default=False, description="Return embedding provider names without loading model catalogs."),
) -> Dict[str, Any]:
    """List text embedding providers/models through the Gateway Runtime discovery facade."""
    discovery, err = _gateway_abstractcore_discovery_facade()
    if err or discovery is None:
        raw = _runtime_discovery_payload(
            {
                "available": False,
                "kind": "embedding_models",
                "scope": "embedding.text",
                "provider": provider,
                "models": [],
                "embedding_models": [],
                "providers": [],
                "available_providers": [],
                "embedding_providers": [],
                "provider_details": [],
                "models_by_provider": {},
                "embedding_models_by_provider": {},
                "provider_models": [],
                "error": err or "Gateway runtime does not expose AbstractCore discovery helpers.",
            }
        )
        out = raw if providers_only else _filter_provider_model_catalog_response(
            raw,
            provider=provider,
            model_keys=("models_by_provider", "embedding_models_by_provider"),
        )
        return _gateway_catalog_response(
            out,
            kind="providers" if providers_only else "models",
            scope="embedding.text",
            items=_gateway_catalog_provider_items(
                out,
                provider_keys=("providers", "available_providers", "embedding_providers"),
                detail_keys=("provider_details",),
            )
            if providers_only
            else _gateway_catalog_model_items(
                out,
                provider=provider,
                task="embedding.text",
                detail_keys=("provider_models",),
                map_keys=("models_by_provider", "embedding_models_by_provider"),
                value_keys=("embedding_models", "models"),
            ),
            provider=provider,
            metadata={"providers_only": providers_only},
        )
    try:
        payload = discovery.list_embedding_models(
            base_url=base_url,
            provider_api_key=_request_provider_api_key(request),
            provider=provider,
            providers_only=providers_only,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    raw = _runtime_discovery_payload(payload)
    out = raw if providers_only else _filter_provider_model_catalog_response(
        raw,
        provider=provider,
        model_keys=("models_by_provider", "embedding_models_by_provider"),
    )
    return _gateway_catalog_response(
        out,
        kind="providers" if providers_only else "models",
        scope="embedding.text",
        items=_gateway_catalog_provider_items(
            out,
            provider_keys=("providers", "available_providers", "embedding_providers"),
            detail_keys=("provider_details",),
        )
        if providers_only
        else _gateway_catalog_model_items(
            out,
            provider=provider,
            task="embedding.text",
            detail_keys=("provider_models",),
            map_keys=("models_by_provider", "embedding_models_by_provider"),
            value_keys=("embedding_models", "models"),
        ),
        provider=provider,
        metadata={"providers_only": providers_only},
    )


@router.get("/discovery/providers")
async def discovery_providers(
    request: Request,
    include_models: bool = Query(False, description="Include model lists (may be slow)."),
) -> Dict[str, Any]:
    """List available providers (and optionally their models) for UI helper dropdowns."""
    discovery, err = _gateway_abstractcore_discovery_facade()
    if err or discovery is None:
        out = {"items": [], "error": err or "Gateway runtime does not expose AbstractCore discovery helpers.", "available": False, "route_available": True, "source": "abstractgateway.discovery"}
        return _gateway_catalog_response(
            out,
            kind="providers",
            scope="text",
            items=[],
            metadata={"include_models": bool(include_models)},
        )

    try:
        payload = discovery.list_providers(
            include_models=bool(include_models),
            provider_api_key=_request_provider_api_key(request),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    items_raw = payload.get("items") if isinstance(payload, dict) else None

    items: list[Dict[str, Any]] = []
    if isinstance(items_raw, list):
        for p in items_raw:
            if isinstance(p, dict):
                name = p.get("name")
                if isinstance(name, str) and name.strip():
                    items.append(dict(p))

    items.sort(key=lambda x: str(x.get("name") or ""))

    default_provider = payload.get("default_provider") if isinstance(payload, dict) else None
    default_model = payload.get("default_model") if isinstance(payload, dict) else None
    body: Dict[str, Any]
    if isinstance(default_provider, str) and default_provider.strip() and isinstance(default_model, str) and default_model.strip():
        body = {
            "items": items,
            "available": bool(items),
            "route_available": True,
            "source": "abstractruntime.discovery_facade",
            "default_provider": default_provider,
            "default_model": default_model,
            "default_source": payload.get("source") if isinstance(payload, dict) else None,
        }
    else:
        defaults = resolve_gateway_provider_model(purpose="provider discovery defaults")
        if defaults.provider and defaults.model:
            body = {
            "items": items,
            "available": bool(items),
            "route_available": True,
            "source": "abstractruntime.discovery_facade",
            "default_provider": defaults.provider,
            "default_model": defaults.model,
            "default_source": defaults.source,
        }
        else:
            body = {
                "items": items,
                "available": bool(items),
                "route_available": True,
                "source": "abstractruntime.discovery_facade",
                "default_provider": None,
                "default_model": None,
                "default_error": (payload.get("error") if isinstance(payload, dict) else None) or defaults.error,
            }
    if isinstance(payload, dict):
        if isinstance(payload.get("source"), str) and payload.get("source"):
            body["upstream_source"] = str(payload.get("source"))
        if isinstance(payload.get("error"), str) and payload.get("error"):
            body.setdefault("error", str(payload.get("error")))
    return _gateway_catalog_response(
        body,
        kind="providers",
        scope="text",
        items=_gateway_catalog_provider_items(body, provider_keys=(), detail_keys=("items",)),
        metadata={"include_models": bool(include_models)},
    )


@router.get("/discovery/providers/{provider_name}/models")
async def discovery_provider_models(request: Request, provider_name: str) -> Dict[str, Any]:
    """List available models for a provider (best-effort; may require provider connectivity)."""
    prov = str(provider_name or "").strip()
    if not prov:
        raise HTTPException(status_code=400, detail="provider_name is required")
    discovery, err = _gateway_abstractcore_discovery_facade()
    if err or discovery is None:
        out = {
            "provider": prov,
            "models": [],
            "available": False,
            "route_available": True,
            "source": "abstractgateway.discovery",
            "error": err or "Gateway runtime does not expose AbstractCore discovery helpers.",
        }
        return _gateway_catalog_response(
            out,
            kind="models",
            scope="text",
            items=[],
            provider=prov,
        )
    try:
        payload = discovery.list_provider_models(
            prov,
            provider_api_key=_request_provider_api_key(request),
            timeout_s=_provider_models_timeout_s(),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    models = payload.get("models") if isinstance(payload, dict) else None
    out = [str(m) for m in models if isinstance(m, str) and str(m).strip()] if isinstance(models, list) else []
    out = _dedupe_strings(out)
    out.sort()
    body: Dict[str, Any] = {
        "provider": prov,
        "models": out,
        "available": bool(out),
        "route_available": True,
        "source": "abstractruntime.discovery_facade",
    }
    if isinstance(payload, dict) and isinstance(payload.get("source"), str) and payload.get("source"):
        body["upstream_source"] = str(payload.get("source"))
    if isinstance(payload, dict) and payload.get("error"):
        body["error"] = str(payload.get("error"))
    return _gateway_catalog_response(
        body,
        kind="models",
        scope="text",
        items=_gateway_catalog_model_items(body, provider=prov, value_keys=("models",)),
        provider=prov,
    )


@router.get("/discovery/models/capabilities")
async def discovery_model_capabilities(model_name: str = Query(..., description="Model id/name (may include provider prefix like 'lmstudio/...')")) -> Dict[str, Any]:
    """Best-effort model capability lookup for UI context meters and defaults."""
    name = str(model_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="model_name is required")
    discovery, err = _gateway_abstractcore_discovery_facade()
    if err or discovery is None:
        return {"model": name, "capabilities": {}, "error": err or "Gateway runtime does not expose AbstractCore discovery helpers."}
    try:
        payload = discovery.get_model_capabilities(name)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    out = dict(payload) if isinstance(payload, dict) else {"model": name, "capabilities": {}}
    out.setdefault("model", name)
    caps = out.get("capabilities")
    if not isinstance(caps, dict):
        out["capabilities"] = {}
    return out


# ---------------------------------------------------------------------------
# Bug reports (structured Markdown, gateway-local)
# ---------------------------------------------------------------------------

_DEFAULT_BUG_REPORT_TEMPLATE_MD = """# Bug: {{TITLE}}

> Created: {{CREATED_AT}}
> Bug ID: {{BUG_ID}}
> Session ID: {{SESSION_ID}}
> Session memory run ID: {{SESSION_MEMORY_RUN_ID}}
> Relevant run ID: {{ACTIVE_RUN_ID}}
> Workflow ID: {{WORKFLOW_ID}}

## User Description
{{USER_DESCRIPTION}}

## Impact
- Who is affected?
- How bad is it? (data loss, wrong answer, UX friction, security risk, etc)

## Steps to Reproduce
1.
2.

## Expected Behavior

## Actual Behavior

## Reproducibility
- [ ] always
- [ ] often
- [ ] sometimes
- [ ] once
- [ ] unable to reproduce yet

## Severity
- [ ] blocker
- [ ] major
- [ ] minor
- [ ] polish

## Workaround
(if any)

## Attachments (session-scoped)
{{SESSION_ATTACHMENTS}}

## How to Replay / Debug (durable)
- List runs by session: `GET /api/gateway/runs?session_id={{SESSION_ID}}`
- History bundle for the most relevant run (recommended):
  - `GET /api/gateway/runs/{{REPLAY_RUN_ID}}/history_bundle?include_session=true&include_subruns=true`
  - If `REPLAY_RUN_ID` is a placeholder, pick a run id from the session run list.
- Session attachments are stored as artifacts under the session memory run:
  - `GET /api/gateway/runs/{{SESSION_MEMORY_RUN_ID}}/artifacts`

## Environment
- Client: {{CLIENT}}
- Client version: {{CLIENT_VERSION}}
- Browser UA: {{USER_AGENT}}
- Page URL: {{URL}}
- Provider/model: {{PROVIDER}} / {{MODEL}}
- Agent template: {{TEMPLATE}}
- Gateway version: {{GATEWAY_VERSION}}
- Server: {{SERVER_INFO}}
- Python: {{PYTHON_VERSION}}

## Extra Context (JSON)
{{CONTEXT_JSON}}

## Notes / Hypotheses

## Backlog Translation (optional)
- Proposed backlog title:
- Proposed package(s):
- Acceptance criteria:
"""


class BugReportCreateRequest(BaseModel):
    session_id: str = Field(..., description="Session id to correlate durable context (runs + attachments).")
    description: str = Field(..., description="Free-form user description of the bug.")

    active_run_id: Optional[str] = Field(default=None, description="Best-effort active run id at report time.")
    workflow_id: Optional[str] = Field(default=None, description="Best-effort workflow id.")

    client: Optional[str] = Field(default=None, description="Client name (e.g. 'abstractcode-web').")
    client_version: Optional[str] = Field(default=None, description="Client version/build label (best-effort).")
    user_agent: Optional[str] = Field(default=None, description="Browser user agent (best-effort).")
    url: Optional[str] = Field(default=None, description="Client page URL (best-effort).")

    provider: Optional[str] = Field(default=None, description="Provider used for the run (best-effort).")
    model: Optional[str] = Field(default=None, description="Model used for the run (best-effort).")
    template: Optional[str] = Field(default=None, description="Agent/workflow template id (best-effort).")

    context: Dict[str, Any] = Field(default_factory=dict, description="Optional additional client context (JSON).")


class BugReportCreateResponse(BaseModel):
    ok: bool = True
    filename: str
    path: str
    template_path: str
    created_at: str
    session_id: str
    session_memory_run_id: str
    active_run_id: Optional[str] = None
    proposed_backlog_relpath: str = ""
    proposed_backlog_item_id: Optional[int] = None


def _gateway_bug_reports_dir() -> Path:
    svc = get_gateway_service()
    base = Path(getattr(getattr(svc, "stores", None), "base_dir", Path("."))).expanduser().resolve()
    out = base / "bug_reports"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _ensure_bug_report_template(*, bug_dir: Path) -> Path:
    bug_dir.mkdir(parents=True, exist_ok=True)
    path = bug_dir / "template.md"
    if path.exists():
        return path
    try:
        with open(path, "x", encoding="utf-8") as f:
            f.write(_DEFAULT_BUG_REPORT_TEMPLATE_MD)
    except FileExistsError:
        pass
    return path


def _bug_report_title(description: str) -> str:
    first = str(description or "").strip().splitlines()[0].strip()
    title = first or "Bug report"
    title = re.sub(r"\s+", " ", title).strip()
    if title.lower().startswith("/bug"):
        title = title[len("/bug") :].strip() or "Bug report"
    if len(title) > 120:
        title = title[:120].rstrip()
    return title


def _bug_report_slug(description: str) -> str:
    first = str(description or "").strip().splitlines()[0].strip().lower()
    if first.startswith("/bug"):
        first = first[len("/bug") :].strip()
    slug = re.sub(r"[^a-z0-9]+", "-", first).strip("-")
    slug = slug or "bug"
    slug = slug[:80].strip("-") or "bug"
    return slug


def _indent_markdown_literal(text: str) -> str:
    """Indent as a Markdown code block to avoid accidental rendering/injection."""
    s = str(text or "")
    if not s.strip():
        return "    (empty)"
    lines = s.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join([f"    {ln}" for ln in lines])


def _format_session_attachments_md(*, session_memory_run_id: str) -> str:
    svc = get_gateway_service()
    rid = str(session_memory_run_id or "").strip()
    if not rid:
        return "(none)"

    store = getattr(getattr(svc, "stores", None), "artifact_store", None)
    list_fn = getattr(store, "list_by_run", None)
    if not callable(list_fn):
        return "(artifact store unavailable)"

    try:
        items = list_fn(rid)
    except Exception as e:
        return f"(failed to list artifacts: {e})"

    attachments: list[Any] = []
    for meta in items or []:
        tags = getattr(meta, "tags", None)
        if isinstance(tags, dict) and str(tags.get("kind") or "").strip() == "attachment":
            attachments.append(meta)

    if not attachments:
        return "(none)"

    def _key(m: Any) -> str:
        return str(getattr(m, "created_at", "") or "")

    attachments.sort(key=_key, reverse=True)

    lines: list[str] = []
    for meta in attachments[:50]:
        tags = getattr(meta, "tags", None)
        tags = tags if isinstance(tags, dict) else {}
        handle = str(tags.get("path") or tags.get("source_path") or tags.get("filename") or "").strip()
        target = str(tags.get("target") or "").strip()
        sha = str(tags.get("sha256") or "").strip()
        aid = str(getattr(meta, "artifact_id", "") or "").strip()
        size = getattr(meta, "size_bytes", None)
        size_s = f"{int(size)} B" if isinstance(size, int) and size >= 0 else ""

        bits: list[str] = []
        if target:
            bits.append(f"target={target}")
        if aid:
            bits.append(f"id={aid}")
        if sha:
            bits.append(f"sha={sha[:8]}…")
        if size_s:
            bits.append(size_s)

        label = handle or aid or "(attachment)"
        suffix = f" ({', '.join(bits)})" if bits else ""
        lines.append(f"- @{label}{suffix}")

    if len(attachments) > 50:
        lines.append(f"- …and {len(attachments) - 50} more")

    return "\n".join(lines)


def _report_attachment_candidates(report: Any, *, session_memory_run_id: str) -> list[Dict[str, str]]:
    """Best-effort list of artifact-backed attachments relevant to this report.

    Preference order:
    1) attachments_next_run in the report's client_context (this reflects what the client had attached).
    2) most recent session memory artifacts tagged as attachments (fallback).
    """
    out: list[Dict[str, str]] = []
    ctx = getattr(report, "context", None)
    if isinstance(ctx, dict):
        client_ctx = ctx.get("client_context")
        if isinstance(client_ctx, dict):
            items = client_ctx.get("attachments_next_run")
            if isinstance(items, list):
                for it in items[:50]:
                    if not isinstance(it, dict):
                        continue
                    st = str(it.get("status") or "").strip().lower()
                    if st and st != "ok":
                        continue
                    aid = str(it.get("artifact_id") or "").strip()
                    if not aid:
                        continue
                    out.append(
                        {
                            "artifact_id": aid,
                            "filename": str(it.get("filename") or "").strip(),
                            "content_type": str(it.get("content_type") or "").strip().lower(),
                            "sha256": str(it.get("sha256") or "").strip().lower(),
                        }
                    )
    if out:
        return out

    svc = get_gateway_service()
    store = getattr(getattr(svc, "stores", None), "artifact_store", None)
    list_fn = getattr(store, "list_by_run", None)
    if not callable(list_fn):
        return []
    try:
        metas = list_fn(str(session_memory_run_id))
    except Exception:
        return []

    # Pick the most recent attachment artifacts.
    attachments: list[Any] = []
    for meta in metas or []:
        tags = getattr(meta, "tags", None)
        if isinstance(tags, dict) and str(tags.get("kind") or "").strip() == "attachment":
            attachments.append(meta)

    def _key(m: Any) -> str:
        return str(getattr(m, "created_at", "") or "")

    attachments.sort(key=_key, reverse=True)
    for meta in attachments[:8]:
        aid = str(getattr(meta, "artifact_id", "") or "").strip()
        if not aid:
            continue
        tags = getattr(meta, "tags", None)
        tags = tags if isinstance(tags, dict) else {}
        out.append(
            {
                "artifact_id": aid,
                "filename": str(tags.get("filename") or "").strip(),
                "content_type": str(getattr(meta, "content_type", "") or "").strip().lower(),
                "sha256": str(tags.get("sha256") or "").strip().lower(),
            }
        )
    return out


def _detect_safe_attachment_mime(content: bytes) -> str:
    """Best-effort magic detection for common safe media types (stdlib-only).

    This is intentionally conservative: it only returns a MIME type when the content
    is recognizable by magic bytes (and for zip containers, by a minimal structural
    check) to reduce the risk of copying unexpected/scriptable files into the repo.
    """
    b = bytes(content or b"")
    if len(b) >= 8 and b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(b) >= 3 and b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(b) >= 6 and (b[:6] == b"GIF87a" or b[:6] == b"GIF89a"):
        return "image/gif"
    if len(b) >= 12 and b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    if len(b) >= 2 and b[:2] == b"BM":
        return "image/bmp"
    if len(b) >= 4 and b[:4] in {b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"}:
        return "image/tiff"
    if len(b) >= 4 and b[:4] in {b"\x00\x00\x01\x00", b"\x00\x00\x02\x00"}:
        return "image/x-icon"
    if len(b) >= 5 and b[:5] == b"%PDF-":
        return "application/pdf"
    if len(b) >= 5 and b[:5] == b"{\\rtf":
        return "text/rtf"
    if len(b) >= 4 and b[:2] == b"PK":
        # Zip-based documents (docx/xlsx/pptx/odt). Keep checks minimal and stdlib-only.
        try:
            with zipfile.ZipFile(io.BytesIO(b)) as zf:
                names = set(zf.namelist())
                if "[Content_Types].xml" in names:
                    if "word/document.xml" in names:
                        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    if "xl/workbook.xml" in names:
                        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    if "ppt/presentation.xml" in names:
                        return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
                if "mimetype" in names:
                    try:
                        mt = bytes(zf.read("mimetype")[:200])
                    except Exception:
                        mt = b""
                    if mt.strip() == b"application/vnd.oasis.opendocument.text":
                        return "application/vnd.oasis.opendocument.text"
                if "content.xml" in names and "META-INF/manifest.xml" in names:
                    return "application/vnd.oasis.opendocument.text"
        except Exception:
            pass
    if len(b) >= 12 and b[:4] == b"RIFF" and b[8:12] == b"WAVE":
        return "audio/wav"
    if len(b) >= 4 and b[:4] == b"OggS":
        return "audio/ogg"
    if len(b) >= 4 and b[:4] == b"fLaC":
        return "audio/flac"
    if len(b) >= 3 and b[:3] == b"ID3":
        return "audio/mpeg"
    # AAC ADTS: 0xFFF syncword with layer bits == 00.
    if len(b) >= 2 and b[0] == 0xFF and (b[1] & 0xF6) == 0xF0:
        return "audio/aac"
    # MP3: allow frame sync but exclude AAC-like layer==00.
    if len(b) >= 2 and b[0] == 0xFF and (b[1] & 0xE0) == 0xE0 and ((b[1] >> 1) & 0x3) != 0:
        return "audio/mpeg"
    if len(b) >= 12 and b[:4] == b"RIFF" and b[8:12] == b"AVI ":
        return "video/x-msvideo"
    # ISO BMFF (mp4/m4a/mov): size(4) + ftyp(4) + brands...
    if len(b) >= 12 and b[4:8] == b"ftyp":
        brands = b[8:64]
        if b"M4A " in brands or b"M4B " in brands or b"M4P " in brands:
            return "audio/mp4"
        if b"qt  " in brands:
            return "video/quicktime"
        return "video/mp4"
    # ASF (wmv)
    if len(b) >= 16 and b[:16] == b"\x30\x26\xB2\x75\x8E\x66\xCF\x11\xA6\xD9\x00\xAA\x00\x62\xCE\x6C":
        return "video/x-ms-wmv"
    # EBML (webm/mkv)
    if len(b) >= 4 and b[:4] == b"\x1A\x45\xDF\xA3":
        head = b[:512].lower()
        if b"matroska" in head:
            return "video/x-matroska"
        return "video/webm"
    return ""


def _ext_for_mime(mime: str) -> str:
    m = str(mime or "").strip().lower()
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
        "image/tiff": ".tiff",
        "image/x-icon": ".ico",
        "application/pdf": ".pdf",
        "text/rtf": ".rtf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "application/vnd.oasis.opendocument.text": ".odt",
        "audio/wav": ".wav",
        "audio/ogg": ".ogg",
        "audio/flac": ".flac",
        "audio/aac": ".aac",
        "audio/mp4": ".m4a",
        "audio/mpeg": ".mp3",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "video/x-msvideo": ".avi",
        "video/quicktime": ".mov",
        "video/x-matroska": ".mkv",
        "video/x-ms-wmv": ".wmv",
    }.get(m, "")


def _backlog_assets_section_md(*, item_id: int, stored: list[Dict[str, Any]], skipped: list[Dict[str, str]]) -> str:
    if not stored and not skipped:
        return ""
    lines: list[str] = []
    lines.append("## Attachments (repo)")
    lines.append("Copied from the report/session artifacts (treat as untrusted inputs).")
    lines.append("")
    if stored:
        for s in stored[:25]:
            rel = str(s.get("relpath") or "").strip()
            size = int(s.get("bytes") or 0) if str(s.get("bytes") or "").strip() else 0
            sha = str(s.get("sha256") or "").strip()
            ctype = str(s.get("content_type") or "").strip()
            meta = []
            if ctype:
                meta.append(ctype)
            if size > 0:
                meta.append(f"{size} B")
            if sha:
                meta.append(f"sha256={sha[:8]}…")
            suffix = f" ({', '.join(meta)})" if meta else ""
            if rel:
                lines.append(f"- `{rel}`{suffix}")
    if skipped:
        lines.append("")
        lines.append("### Skipped")
        for s in skipped[:25]:
            name = str(s.get("filename") or s.get("artifact_id") or "(attachment)").strip()
            reason = str(s.get("reason") or "skipped").strip()
            lines.append(f"- `{name}` — {reason}")
    lines.append("")
    lines.append(f"Session artifacts remain available under the session memory run (see Context).")
    lines.append("")
    return "\n".join(lines).strip() + "\n"


def _maybe_copy_report_attachments_to_backlog_assets(
    *,
    repo_root: Path,
    item_id: int,
    report: Any,
    session_memory_run_id: str,
    backlog_path: Path,
) -> None:
    """Best-effort: copy safe media attachments into docs/backlog/assets/{id}/ and link them from the backlog item."""
    if not _env_bool("ABSTRACTGATEWAY_REPORT_COPY_ATTACHMENTS_TO_BACKLOG", True):
        return

    rr = Path(repo_root).expanduser().resolve()
    pid = f"{int(item_id):03d}"

    try:
        max_files_raw = str(os.getenv("ABSTRACTGATEWAY_REPORT_BACKLOG_ASSETS_MAX_FILES", "") or "").strip()
        max_files = int(max_files_raw) if max_files_raw else 8
        if max_files <= 0:
            max_files = 8
    except Exception:
        max_files = 8

    try:
        max_bytes_raw = str(os.getenv("ABSTRACTGATEWAY_REPORT_BACKLOG_ASSETS_MAX_BYTES", "") or "").strip()
        max_bytes = int(max_bytes_raw) if max_bytes_raw else 25 * 1024 * 1024
        if max_bytes <= 0:
            max_bytes = 25 * 1024 * 1024
    except Exception:
        max_bytes = 25 * 1024 * 1024

    cand = _report_attachment_candidates(report, session_memory_run_id=str(session_memory_run_id))
    if not cand:
        return

    svc = get_gateway_service()
    store = getattr(getattr(svc, "stores", None), "artifact_store", None)
    load_fn = getattr(store, "load", None)
    if not callable(load_fn):
        return

    assets_root = (rr / "docs" / "backlog" / "assets").resolve()
    assets_dir = (assets_root / pid).resolve()
    try:
        assets_dir.relative_to(assets_root)
    except Exception:
        return
    assets_dir.mkdir(parents=True, exist_ok=True)

    stored: list[Dict[str, Any]] = []
    skipped: list[Dict[str, str]] = []

    for it in cand[: max(1, max_files)]:
        aid = str(it.get("artifact_id") or "").strip()
        if not aid:
            continue
        try:
            art = load_fn(aid)
        except Exception as e:
            skipped.append({"artifact_id": aid, "filename": str(it.get("filename") or ""), "reason": f"load failed: {e}"})
            continue
        if art is None:
            skipped.append({"artifact_id": aid, "filename": str(it.get("filename") or ""), "reason": "not found"})
            continue
        content = bytes(getattr(art, "content", b"") or b"")
        if len(content) > int(max_bytes):
            skipped.append({"artifact_id": aid, "filename": str(it.get("filename") or ""), "reason": f"too large ({len(content)} bytes)"})
            continue

        detected = _detect_safe_attachment_mime(content)
        if not detected:
            # Conservative: only auto-copy types we can validate by magic.
            skipped.append({"artifact_id": aid, "filename": str(it.get("filename") or ""), "reason": "unrecognized/unsafe type"})
            continue

        # Disallow svg explicitly (can carry scripts).
        if detected == "image/svg+xml":
            skipped.append({"artifact_id": aid, "filename": str(it.get("filename") or ""), "reason": "svg disabled"})
            continue

        expected_ext = _ext_for_mime(detected)
        raw_name = str(it.get("filename") or "") or str(getattr(getattr(art, "metadata", None), "tags", {}).get("filename") or "")
        base_name = _sanitize_backlog_asset_filename(raw_name or f"attachment{expected_ext or ''}")
        stem, ext = os.path.splitext(base_name)
        if expected_ext and ext.lower() != expected_ext:
            base_name = _sanitize_backlog_asset_filename(f"{stem}{expected_ext}")

        dest = (assets_dir / base_name).resolve()
        try:
            dest.relative_to(assets_dir)
        except Exception:
            skipped.append({"artifact_id": aid, "filename": base_name, "reason": "invalid dest"})
            continue

        if dest.exists():
            for i in range(2, 2000):
                candidate = (assets_dir / f"{stem}-{i}{expected_ext or ext}").resolve()
                try:
                    candidate.relative_to(assets_dir)
                except Exception:
                    continue
                if not candidate.exists():
                    dest = candidate
                    base_name = candidate.name
                    break
        if dest.exists():
            skipped.append({"artifact_id": aid, "filename": base_name, "reason": "filename collision"})
            continue

        try:
            with open(dest, "xb") as f:
                f.write(content)
        except Exception as e:
            skipped.append({"artifact_id": aid, "filename": base_name, "reason": f"write failed: {e}"})
            continue

        sha = hashlib.sha256(content).hexdigest()
        relpath = f"docs/backlog/assets/{pid}/{base_name}"
        stored.append({"relpath": relpath, "bytes": len(content), "sha256": sha, "content_type": detected})

    if not stored and not skipped:
        return

    try:
        md = backlog_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return

    section = _backlog_assets_section_md(item_id=item_id, stored=stored, skipped=skipped)
    if not section.strip():
        return

    if "## Attachments (repo)" in md:
        return

    insert_before = "\n\n## Related (best-effort)"
    idx = md.find(insert_before)
    if idx >= 0:
        new_md = md[:idx] + "\n\n" + section.strip() + md[idx:]
    else:
        insert_before2 = "\n\n## Scope"
        idx2 = md.find(insert_before2)
        if idx2 >= 0:
            new_md = md[:idx2] + "\n\n" + section.strip() + md[idx2:]
        else:
            new_md = md.rstrip() + "\n\n" + section.strip() + "\n"

    try:
        backlog_path.write_text(new_md if new_md.endswith("\n") else new_md + "\n", encoding="utf-8")
    except Exception:
        return


def _maybe_autobridge_report_to_proposed_backlog(
    *,
    report_path: Path,
    report_relpath: str,
    report_type: str,
    repo_root: Path,
) -> Optional[Dict[str, Any]]:
    """Best-effort: write a typed proposed backlog item for a report.

    This must never fail the report creation endpoint; callers should treat errors as non-fatal.
    """

    try:
        from ..maintenance.draft_generator import BacklogIdAllocator, write_backlog_draft  # type: ignore
        from ..maintenance.report_models import TriageDecision  # type: ignore
        from ..maintenance.report_parser import parse_report_file  # type: ignore
        from ..maintenance.triage import compute_missing_fields  # type: ignore
        from ..maintenance.triage_queue import decision_id_for_report  # type: ignore
    except Exception:
        return None

    rr = Path(repo_root).expanduser().resolve()
    backlog_root = (rr / "docs" / "backlog").resolve()
    if not (backlog_root / "template.md").exists():
        # Repo exists, but backlog system isn't present.
        return None

    try:
        report = parse_report_file(Path(report_path))
    except Exception:
        return None

    missing = []
    try:
        missing = compute_missing_fields(report)
    except Exception:
        missing = []

    did = decision_id_for_report(report_relpath=str(report_relpath))
    decision = TriageDecision(
        decision_id=str(did),
        report_type=str(report_type or "bug"),  # type: ignore[arg-type]
        report_relpath=str(report_relpath),
        missing_fields=list(missing) if isinstance(missing, list) else [],
        duplicates=[],
    )

    allocator = BacklogIdAllocator.from_backlog_root(backlog_root)
    path, item_id = write_backlog_draft(
        repo_root=rr,
        backlog_root=backlog_root,
        allocator=allocator,
        report=report,
        decision=decision,
        llm_suggestion=None,
    )
    try:
        session_memory_run_id = str(getattr(getattr(report, "header", None), "session_memory_run_id", "") or "").strip()
        if session_memory_run_id:
            _maybe_copy_report_attachments_to_backlog_assets(
                repo_root=rr,
                item_id=int(item_id),
                report=report,
                session_memory_run_id=session_memory_run_id,
                backlog_path=Path(path),
            )
    except Exception:
        # Best-effort only; never fail report filing.
        pass
    try:
        rel = str(path.relative_to(rr))
    except Exception:
        rel = str(path)
    return {"item_id": int(item_id), "relpath": rel}


@router.post("/bugs/report")
async def bugs_report(req: BugReportCreateRequest) -> BugReportCreateResponse:
    svc = get_gateway_service()

    sid = str(req.session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="session_id is required")

    desc = str(req.description or "")
    if not desc.strip():
        raise HTTPException(status_code=400, detail="description is required")
    if len(desc) > 20_000:
        raise HTTPException(status_code=413, detail="description is too large (max 20,000 chars)")

    bug_dir = _gateway_bug_reports_dir()
    template_path = _ensure_bug_report_template(bug_dir=bug_dir)

    try:
        template_md = template_path.read_text(encoding="utf-8")
    except Exception:
        template_md = _DEFAULT_BUG_REPORT_TEMPLATE_MD

    created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    today = created_at[:10]
    slug = _bug_report_slug(desc)
    title = _bug_report_title(desc)
    bug_id = str(uuid.uuid4())

    # Ensure the session memory owner run exists so attachments can be listed deterministically.
    try:
        session_memory_run_id = _ensure_session_memory_owner_run_exists(run_store=svc.host.run_store, session_id=sid)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to ensure session memory run: {e}")

    attachments_md = _format_session_attachments_md(session_memory_run_id=session_memory_run_id)

    active_run_id = str(req.active_run_id or "").strip() or ""
    replay_run_id = active_run_id or "<RUN_ID>"
    workflow_id = str(req.workflow_id or "").strip() or ""
    client = str(req.client or "").strip() or ""
    client_version = str(req.client_version or "").strip() or ""
    user_agent = str(req.user_agent or "").strip() or ""
    url = str(req.url or "").strip() or ""
    provider = str(req.provider or "").strip() or ""
    model = str(req.model or "").strip() or ""
    template = str(req.template or "").strip() or ""

    # Server context (safe subset; do not include secrets).
    try:
        server_info = f"{platform.system()} {platform.release()} ({platform.machine()})"
    except Exception:
        server_info = platform.platform()
    python_version = str(sys.version.split()[0] if sys.version else "").strip()
    try:
        from abstractgateway import __version__ as gateway_version
    except Exception:
        gateway_version = ""

    ctx_obj: Dict[str, Any] = {
        "active_run_id": active_run_id or None,
        "workflow_id": workflow_id or None,
        "provider": provider or None,
        "model": model or None,
        "template": template or None,
        "client": client or None,
        "client_version": client_version or None,
        "url": url or None,
        "user_agent": user_agent or None,
        "session_memory_run_id": session_memory_run_id,
        "created_at": created_at,
        "bug_id": bug_id,
        "client_context": req.context if isinstance(req.context, dict) else {},
    }
    # Keep the JSON payload bounded; include only what fits.
    try:
        ctx_json = json.dumps(ctx_obj, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception:
        ctx_json = "{}"
    if len(ctx_json) > 80_000:
        ctx_json = ctx_json[:80_000] + "\n…(truncated)…\n"
    context_block = f"```json\n{ctx_json}\n```"

    replacements: Dict[str, str] = {
        "TITLE": title,
        "BUG_ID": bug_id,
        "CREATED_AT": created_at,
        "SESSION_ID": sid,
        "SESSION_MEMORY_RUN_ID": session_memory_run_id,
        "ACTIVE_RUN_ID": active_run_id,
        "REPLAY_RUN_ID": replay_run_id,
        "WORKFLOW_ID": workflow_id,
        "USER_DESCRIPTION": _indent_markdown_literal(desc),
        "SESSION_ATTACHMENTS": attachments_md,
        "CLIENT": client,
        "CLIENT_VERSION": client_version,
        "USER_AGENT": user_agent,
        "URL": url,
        "PROVIDER": provider,
        "MODEL": model,
        "TEMPLATE": template,
        "GATEWAY_VERSION": gateway_version,
        "SERVER_INFO": server_info,
        "PYTHON_VERSION": python_version,
        "CONTEXT_JSON": context_block,
    }

    out_md = str(template_md)
    for k, v in replacements.items():
        out_md = out_md.replace(f"{{{{{k}}}}}", str(v))
    if not out_md.endswith("\n"):
        out_md += "\n"

    base = f"{today}_{slug}"
    filename = ""
    path = None
    for i in range(0, 1000):
        name = f"{base}.md" if i == 0 else f"{base}_{i + 1}.md"
        candidate = bug_dir / name
        try:
            with open(candidate, "x", encoding="utf-8") as f:
                f.write(out_md)
            filename = name
            path = candidate
            break
        except FileExistsError:
            continue
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to write bug report: {e}")

    if not filename or path is None:
        raise HTTPException(status_code=409, detail="Could not allocate a unique bug report filename")

    # Do not leak absolute paths; return a stable path relative to the gateway data dir.
    rel_path = f"bug_reports/{filename}"

    proposed_backlog_relpath = ""
    proposed_backlog_item_id: Optional[int] = None
    try:
        repo_root = _triage_repo_root_from_env()
        if repo_root is not None:
            out = _maybe_autobridge_report_to_proposed_backlog(
                report_path=path,
                report_relpath=rel_path,
                report_type="bug",
                repo_root=repo_root,
            )
            if isinstance(out, dict):
                proposed_backlog_relpath = str(out.get("relpath") or "").strip()
                bid = out.get("item_id")
                if isinstance(bid, int):
                    proposed_backlog_item_id = bid
    except Exception:
        # Best-effort only; do not fail report filing.
        pass
    return BugReportCreateResponse(
        ok=True,
        filename=filename,
        path=rel_path,
        template_path="bug_reports/template.md",
        created_at=created_at,
        session_id=sid,
        session_memory_run_id=session_memory_run_id,
        active_run_id=active_run_id or None,
        proposed_backlog_relpath=proposed_backlog_relpath,
        proposed_backlog_item_id=proposed_backlog_item_id,
    )


# ---------------------------------------------------------------------------
# Feature requests (structured Markdown, gateway-local)
# ---------------------------------------------------------------------------

_DEFAULT_FEATURE_REQUEST_TEMPLATE_MD = """# Feature: {{TITLE}}

> Created: {{CREATED_AT}}
> Feature ID: {{FEATURE_ID}}
> Session ID: {{SESSION_ID}}
> Session memory run ID: {{SESSION_MEMORY_RUN_ID}}
> Relevant run ID: {{ACTIVE_RUN_ID}}
> Workflow ID: {{WORKFLOW_ID}}

## User Request
{{USER_DESCRIPTION}}

## Problem / Motivation
- What is painful today?
- Who needs this and why now?

## Proposed Solution
- What should the system do?
- Any UX expectations?

## Acceptance Criteria
- [ ] (clear, testable outcomes)

## Scope
### Included
- 

### Excluded
- 

## Priority
- [ ] P0 (blocker)
- [ ] P1 (important)
- [ ] P2 (nice-to-have)

## Attachments (session-scoped)
{{SESSION_ATTACHMENTS}}

## Context / Replay (durable)
- List runs by session: `GET /api/gateway/runs?session_id={{SESSION_ID}}`
- History bundle for the most relevant run (recommended):
  - `GET /api/gateway/runs/{{REPLAY_RUN_ID}}/history_bundle?include_session=true&include_subruns=true`
  - If `REPLAY_RUN_ID` is a placeholder, pick a run id from the session run list.
- Session attachments are stored as artifacts under the session memory run:
  - `GET /api/gateway/runs/{{SESSION_MEMORY_RUN_ID}}/artifacts`

## Environment
- Client: {{CLIENT}}
- Client version: {{CLIENT_VERSION}}
- Browser UA: {{USER_AGENT}}
- Page URL: {{URL}}
- Provider/model: {{PROVIDER}} / {{MODEL}}
- Agent template: {{TEMPLATE}}
- Gateway version: {{GATEWAY_VERSION}}
- Server: {{SERVER_INFO}}
- Python: {{PYTHON_VERSION}}

## Extra Context (JSON)
{{CONTEXT_JSON}}

## Notes / Backlog Translation (optional)
- Proposed backlog title:
- Proposed package(s):
- Acceptance criteria:
"""


class FeatureReportCreateRequest(BaseModel):
    session_id: str = Field(..., description="Session id to correlate durable context (runs + attachments).")
    description: str = Field(..., description="Free-form user description of the requested feature.")

    active_run_id: Optional[str] = Field(default=None, description="Best-effort relevant run id at report time.")
    workflow_id: Optional[str] = Field(default=None, description="Best-effort workflow id.")

    client: Optional[str] = Field(default=None, description="Client name (e.g. 'abstractcode-web').")
    client_version: Optional[str] = Field(default=None, description="Client version/build label (best-effort).")
    user_agent: Optional[str] = Field(default=None, description="Browser user agent (best-effort).")
    url: Optional[str] = Field(default=None, description="Client page URL (best-effort).")

    provider: Optional[str] = Field(default=None, description="Provider used for the run (best-effort).")
    model: Optional[str] = Field(default=None, description="Model used for the run (best-effort).")
    template: Optional[str] = Field(default=None, description="Agent/workflow template id (best-effort).")

    context: Dict[str, Any] = Field(default_factory=dict, description="Optional additional client context (JSON).")


class FeatureReportCreateResponse(BaseModel):
    ok: bool = True
    filename: str
    path: str
    template_path: str
    created_at: str
    session_id: str
    session_memory_run_id: str
    active_run_id: Optional[str] = None
    proposed_backlog_relpath: str = ""
    proposed_backlog_item_id: Optional[int] = None


class ReportInboxItem(BaseModel):
    report_type: str  # "bug" | "feature"
    filename: str
    relpath: str
    title: str
    created_at: str = ""
    session_id: str = ""
    workflow_id: str = ""
    active_run_id: str = ""
    decision_id: str = ""
    triage_status: str = ""  # pending|approved|deferred|rejected


class ReportInboxListResponse(BaseModel):
    items: List[ReportInboxItem] = Field(default_factory=list)


class ReportContentResponse(BaseModel):
    report_type: str
    filename: str
    relpath: str
    content: str


class EmailAccountInfo(BaseModel):
    account: str
    email: str = ""
    from_email: Optional[str] = None
    can_read: bool = False
    can_send: bool = False
    imap_password_set: Optional[bool] = None
    smtp_password_set: Optional[bool] = None


class EmailAccountsResponse(BaseModel):
    ok: bool = True
    source: str = ""
    config_path: str = ""
    default_account: str = ""
    accounts: List[EmailAccountInfo] = Field(default_factory=list)


class EmailMessageSummary(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    uid: str
    message_id: str = ""
    subject: str = ""
    from_: str = Field(default="", alias="from")
    to: str = ""
    date: str = ""
    flags: List[str] = Field(default_factory=list)
    seen: bool = False
    size: Optional[int] = None


class EmailListFilter(BaseModel):
    since: Optional[str] = None
    status: str = "all"
    limit: int = 20


class EmailListCounts(BaseModel):
    returned: int = 0
    unread: int = 0
    read: int = 0


class EmailListResponse(BaseModel):
    ok: bool = True
    account: str
    mailbox: str
    filter: EmailListFilter
    counts: EmailListCounts
    messages: List[EmailMessageSummary] = Field(default_factory=list)


class EmailAttachmentInfo(BaseModel):
    filename: str = ""
    content_type: str = ""


class EmailReadResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    ok: bool = True
    account: str
    mailbox: str
    uid: str
    message_id: str = ""
    subject: str = ""
    from_: str = Field(default="", alias="from")
    to: str = ""
    cc: str = ""
    date: str = ""
    flags: List[str] = Field(default_factory=list)
    seen: bool = False
    body_text: str = ""
    body_html: str = ""
    attachments: List[EmailAttachmentInfo] = Field(default_factory=list)


class EmailSendRequest(BaseModel):
    account: Optional[str] = None
    to: Any = Field(..., description="Recipient email or list of emails.")
    subject: str
    body_text: Optional[str] = None
    body_html: Optional[str] = None
    cc: Any = None
    bcc: Any = None
    headers: Optional[Dict[str, str]] = None


class EmailSmtpInfo(BaseModel):
    host: str = ""
    port: int = 0
    username: str = ""
    starttls: bool = True


class EmailSendResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    ok: bool = True
    account: str
    message_id: str = ""
    from_: str = Field(default="", alias="from")
    to: List[str] = Field(default_factory=list)
    cc: List[str] = Field(default_factory=list)
    bcc: List[str] = Field(default_factory=list)
    smtp: EmailSmtpInfo


class TriageDecisionSummary(BaseModel):
    decision_id: str
    report_type: str
    report_relpath: str
    status: str
    created_at: str = ""
    updated_at: str = ""
    defer_until: str = ""
    missing_fields: List[str] = Field(default_factory=list)
    duplicates: List[Dict[str, Any]] = Field(default_factory=list)
    draft_relpath: str = ""


class TriageDecisionListResponse(BaseModel):
    decisions: List[TriageDecisionSummary] = Field(default_factory=list)


class TriageRunRequest(BaseModel):
    write_drafts: bool = False
    enable_llm: bool = False


class TriageRunResponse(BaseModel):
    ok: bool = True
    reports: int
    updated_decisions: int
    decisions_dir: str
    drafts_written: List[str] = Field(default_factory=list)


class TriageDecisionApplyRequest(BaseModel):
    action: str = Field(description="approve|reject|defer")
    defer_days: Optional[int] = Field(default=None, description="If action=defer, number of days to defer (optional).")


class BacklogItemSummary(BaseModel):
    kind: str  # planned|completed|proposed|recurrent
    filename: str
    item_id: int
    package: str
    title: str
    task_type: str = Field(default="task", description="bug|feature|task")
    summary: str = ""
    parsed: bool = True


class BacklogListResponse(BaseModel):
    items: List[BacklogItemSummary] = Field(default_factory=list)


class BacklogContentResponse(BaseModel):
    kind: str
    filename: str
    content: str


class BacklogTemplateResponse(BaseModel):
    ok: bool = True
    relpath: str
    sha256: str
    content: str


class BacklogMoveRequest(BaseModel):
    from_kind: str = Field(..., description="Source backlog kind (planned|proposed|recurrent|completed|deprecated|trash).")
    to_kind: str = Field(..., description="Destination backlog kind (planned|proposed|recurrent|completed|deprecated|trash).")
    filename: str = Field(..., description="Backlog filename (must be a safe .md filename).")


class BacklogMoveResponse(BaseModel):
    ok: bool = True
    from_kind: str
    to_kind: str
    filename: str
    from_relpath: str
    to_relpath: str


class BacklogUpdateRequest(BaseModel):
    content: str = Field(..., description="New Markdown content to write.")
    expected_sha256: Optional[str] = Field(
        default=None,
        description="Optional optimistic concurrency guard: if provided, must match the current file sha256.",
    )


class BacklogUpdateResponse(BaseModel):
    ok: bool = True
    kind: str
    filename: str
    sha256: str
    bytes_written: int


class BacklogCreateRequest(BaseModel):
    kind: str = Field(..., description="Backlog kind to create in (planned|proposed|recurrent).")
    package: str = Field(..., description="Package scope (e.g. framework, abstractruntime, abstractgateway).")
    title: str = Field(..., description="Backlog title.")
    task_type: Optional[str] = Field(default=None, description="Backlog item type: bug|feature|task.")
    summary: Optional[str] = Field(default=None, description="Optional 1-paragraph summary.")
    content: Optional[str] = Field(
        default=None,
        description="Optional full Markdown content override (placeholders {ID}/{Package}/{Title} will be filled if present).",
    )


class BacklogCreateResponse(BaseModel):
    ok: bool = True
    kind: str
    filename: str
    relpath: str
    item_id: int
    sha256: str


class BacklogExecuteResponse(BaseModel):
    ok: bool = True
    request_id: str
    request_relpath: str
    prompt: str


class BacklogRef(BaseModel):
    kind: str
    filename: str


class BacklogExecuteBatchRequest(BaseModel):
    execution_mode: Optional[str] = Field(default=None, description="Execution mode override: uat|inplace.")
    items: List[BacklogRef] = Field(default_factory=list, description="Ordered backlog items to execute sequentially (planned items).")


class BacklogMergeRequest(BaseModel):
    kind: str = Field(default="planned", description="Destination kind for the master backlog (planned|proposed|recurrent).")
    package: str = Field(..., description="Package scope for the master backlog (e.g. framework, abstractobserver).")
    title: str = Field(..., description="Title for the master backlog.")
    task_type: Optional[str] = Field(default=None, description="Master backlog type: bug|feature|task (default: task).")
    summary: Optional[str] = Field(default=None, description="Optional summary override for the master backlog.")
    items: List[BacklogRef] = Field(default_factory=list, description="Backlog items to reference (planned items).")


class BacklogMergeResponse(BaseModel):
    ok: bool = True
    kind: str
    filename: str
    relpath: str
    item_id: int
    sha256: str
    merged_relpaths: List[str] = Field(default_factory=list)


class BacklogAssistRequest(BaseModel):
    kind: str = Field(..., description="Target backlog kind (planned|proposed|recurrent).")
    package: str = Field(..., description="Target package (best-effort).")
    title: str = Field(..., description="Working title (best-effort).")
    summary: Optional[str] = Field(default=None, description="Working summary (best-effort).")
    draft_markdown: Optional[str] = Field(default=None, description="Current draft markdown (optional).")
    messages: List[Dict[str, Any]] = Field(default_factory=list, description="Chat messages: {role, content}.")
    provider: Optional[str] = Field(default=None, description="Optional provider override (default: gateway provider).")
    model: Optional[str] = Field(default=None, description="Optional model override (default: gateway model).")


class BacklogAssistResponse(BaseModel):
    ok: bool = True
    reply: str
    draft_markdown: str = ""


class BacklogMaintainRequest(BaseModel):
    kind: str = Field(..., description="Target backlog kind (planned|proposed|recurrent|deprecated).")
    filename: str = Field(..., description="Backlog filename to maintain (e.g. 123-framework-title.md).")
    package: str = Field(..., description="Target package (best-effort).")
    title: str = Field(..., description="Working title (best-effort).")
    summary: Optional[str] = Field(default=None, description="Working summary (best-effort).")
    draft_markdown: Optional[str] = Field(default=None, description="Current draft markdown (optional).")
    messages: List[Dict[str, Any]] = Field(default_factory=list, description="Chat messages: {role, content}.")
    provider: Optional[str] = Field(default=None, description="Optional provider override (default: gateway provider).")
    model: Optional[str] = Field(default=None, description="Optional model override (default: gateway model).")


class BacklogAdvisorRequest(BaseModel):
    messages: List[Dict[str, Any]] = Field(default_factory=list, description="Chat messages: {role, content}.")
    provider: Optional[str] = Field(default=None, description="Optional provider override (default: gateway provider).")
    model: Optional[str] = Field(default=None, description="Optional model override (default: gateway model).")
    agent: Optional[str] = Field(
        default=None,
        description="Optional agent bundle id override (default: basic-agent).",
    )
    include_trace: bool = Field(
        default=False,
        description="When true, include a best-effort tool execution trace (bounded, for UI display).",
    )
    focus_kind: Optional[str] = Field(
        default=None,
        description="Optional current backlog tab (processing|planned|proposed|recurrent|completed|failed|deprecated|trash).",
    )
    focus_type: Optional[str] = Field(default=None, description="Optional current type filter (bug|feature|task|all).")


class BacklogAdvisorResponse(BaseModel):
    ok: bool = True
    reply: str
    run_id: Optional[str] = None
    tool_trace: Optional[List[Dict[str, Any]]] = None


class BacklogExecConfigResponse(BaseModel):
    ok: bool = True
    runner_enabled: bool
    runner_alive: bool = False
    runner_error: Optional[str] = None
    can_execute: bool
    executor: str
    notify: bool = False
    codex_bin: Optional[str] = None
    codex_model: Optional[str] = None
    codex_reasoning_effort: Optional[str] = None
    codex_available: Optional[bool] = None


class BacklogExecRequestSummary(BaseModel):
    request_id: str
    status: str
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    backlog_relpath: Optional[str] = None
    backlog_kind: Optional[str] = None
    backlog_filename: Optional[str] = None
    target_agent: Optional[str] = None
    target_model: Optional[str] = None
    target_reasoning_effort: Optional[str] = None
    executor_type: Optional[str] = None
    ok: Optional[bool] = None
    exit_code: Optional[int] = None
    error: Optional[str] = None
    run_dir_relpath: Optional[str] = None
    last_message: Optional[str] = None


class BacklogExecRequestListResponse(BaseModel):
    ok: bool = True
    requests: List[BacklogExecRequestSummary] = Field(default_factory=list)


class BacklogExecRequestDetailResponse(BaseModel):
    ok: bool = True
    request_id: str
    payload: Dict[str, Any] = Field(default_factory=dict)


class BacklogExecLogTailResponse(BaseModel):
    ok: bool = True
    request_id: str
    name: str
    bytes: int = 0
    truncated: bool = False
    content: str = ""


class BacklogExecActiveItem(BaseModel):
    request_id: str
    status: str
    kind: str
    filename: str
    relpath: str


class BacklogExecActiveItemsResponse(BaseModel):
    ok: bool = True
    items: List[BacklogExecActiveItem] = Field(default_factory=list)


class BacklogExecFeedbackRequest(BaseModel):
    feedback: str = Field(..., description="Operator feedback for the next iteration (max 20k chars).")


class BacklogExecPromoteRequest(BaseModel):
    redeploy: bool = Field(
        default=True,
        description="When true, attempt a best-effort redeploy via the process manager (if enabled).",
    )


class AuditLogTailResponse(BaseModel):
    ok: bool = True
    bytes: int = 0
    truncated: bool = False
    content: str = ""


class ProcessListResponse(BaseModel):
    ok: bool = True
    enabled: bool = False
    processes: List[Dict[str, Any]] = Field(default_factory=list)


class ProcessActionResponse(BaseModel):
    ok: bool = True
    process_id: str
    state: Dict[str, Any] = Field(default_factory=dict)


class ProcessLogTailResponse(BaseModel):
    ok: bool = True
    process_id: str
    bytes: int = 0
    truncated: bool = False
    log_relpath: Optional[str] = None
    content: str = ""


class ProcessEnvVarItem(BaseModel):
    key: str
    label: str
    description: str
    category: str = "general"
    secret: bool = False
    is_set: bool = False
    source: str = ""
    updated_at: Optional[str] = None


class ProcessEnvListResponse(BaseModel):
    ok: bool = True
    enabled: bool = False
    error: Optional[str] = None
    vars: List[ProcessEnvVarItem] = Field(default_factory=list)


class ProcessEnvUpdateRequest(BaseModel):
    set_vars: Dict[str, str] = Field(default_factory=dict, alias="set")
    unset: List[str] = Field(default_factory=list)


class BacklogAttachmentStored(BaseModel):
    filename: str
    relpath: str
    bytes: int
    sha256: str


class BacklogAttachmentUploadResponse(BaseModel):
    ok: bool = True
    kind: str
    filename: str
    item_id: int
    stored: BacklogAttachmentStored


def _gateway_feature_requests_dir() -> Path:
    svc = get_gateway_service()
    base = Path(getattr(getattr(svc, "stores", None), "base_dir", Path("."))).expanduser().resolve()
    out = base / "feature_requests"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _ensure_feature_request_template(*, feature_dir: Path) -> Path:
    feature_dir.mkdir(parents=True, exist_ok=True)
    path = feature_dir / "template.md"
    if path.exists():
        return path
    try:
        with open(path, "x", encoding="utf-8") as f:
            f.write(_DEFAULT_FEATURE_REQUEST_TEMPLATE_MD)
    except FileExistsError:
        pass
    return path


def _feature_request_title(description: str) -> str:
    first = str(description or "").strip().splitlines()[0].strip()
    title = first or "Feature request"
    title = re.sub(r"\s+", " ", title).strip()
    if title.lower().startswith("/feature"):
        title = title[len("/feature") :].strip() or "Feature request"
    if len(title) > 120:
        title = title[:120].rstrip()
    return title


def _feature_request_slug(description: str) -> str:
    first = str(description or "").strip().splitlines()[0].strip().lower()
    if first.startswith("/feature"):
        first = first[len("/feature") :].strip()
    slug = re.sub(r"[^a-z0-9]+", "-", first).strip("-")
    slug = slug or "feature"
    slug = slug[:80].strip("-") or "feature"
    return slug


@router.post("/features/report")
async def features_report(req: FeatureReportCreateRequest) -> FeatureReportCreateResponse:
    svc = get_gateway_service()

    sid = str(req.session_id or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="session_id is required")

    desc = str(req.description or "")
    if not desc.strip():
        raise HTTPException(status_code=400, detail="description is required")
    if len(desc) > 20_000:
        raise HTTPException(status_code=413, detail="description is too large (max 20,000 chars)")

    feature_dir = _gateway_feature_requests_dir()
    template_path = _ensure_feature_request_template(feature_dir=feature_dir)

    try:
        template_md = template_path.read_text(encoding="utf-8")
    except Exception:
        template_md = _DEFAULT_FEATURE_REQUEST_TEMPLATE_MD

    created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    today = created_at[:10]
    slug = _feature_request_slug(desc)
    title = _feature_request_title(desc)
    feature_id = str(uuid.uuid4())

    # Ensure the session memory owner run exists so attachments can be listed deterministically.
    try:
        session_memory_run_id = _ensure_session_memory_owner_run_exists(run_store=svc.host.run_store, session_id=sid)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to ensure session memory run: {e}")

    attachments_md = _format_session_attachments_md(session_memory_run_id=session_memory_run_id)

    active_run_id = str(req.active_run_id or "").strip() or ""
    replay_run_id = active_run_id or "<RUN_ID>"
    workflow_id = str(req.workflow_id or "").strip() or ""
    client = str(req.client or "").strip() or ""
    client_version = str(req.client_version or "").strip() or ""
    user_agent = str(req.user_agent or "").strip() or ""
    url = str(req.url or "").strip() or ""
    provider = str(req.provider or "").strip() or ""
    model = str(req.model or "").strip() or ""
    template = str(req.template or "").strip() or ""

    # Server context (safe subset; do not include secrets).
    try:
        server_info = f"{platform.system()} {platform.release()} ({platform.machine()})"
    except Exception:
        server_info = platform.platform()
    python_version = str(sys.version.split()[0] if sys.version else "").strip()
    try:
        from abstractgateway import __version__ as gateway_version
    except Exception:
        gateway_version = ""

    ctx_obj: Dict[str, Any] = {
        "active_run_id": active_run_id or None,
        "workflow_id": workflow_id or None,
        "provider": provider or None,
        "model": model or None,
        "template": template or None,
        "client": client or None,
        "client_version": client_version or None,
        "url": url or None,
        "user_agent": user_agent or None,
        "session_memory_run_id": session_memory_run_id,
        "created_at": created_at,
        "feature_id": feature_id,
        "client_context": req.context if isinstance(req.context, dict) else {},
    }
    # Keep the JSON payload bounded; include only what fits.
    try:
        ctx_json = json.dumps(ctx_obj, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception:
        ctx_json = "{}"
    if len(ctx_json) > 80_000:
        ctx_json = ctx_json[:80_000] + "\n…(truncated)…\n"
    context_block = f"```json\n{ctx_json}\n```"

    replacements: Dict[str, str] = {
        "TITLE": title,
        "FEATURE_ID": feature_id,
        "CREATED_AT": created_at,
        "SESSION_ID": sid,
        "SESSION_MEMORY_RUN_ID": session_memory_run_id,
        "ACTIVE_RUN_ID": active_run_id,
        "REPLAY_RUN_ID": replay_run_id,
        "WORKFLOW_ID": workflow_id,
        "USER_DESCRIPTION": _indent_markdown_literal(desc),
        "SESSION_ATTACHMENTS": attachments_md,
        "CLIENT": client,
        "CLIENT_VERSION": client_version,
        "USER_AGENT": user_agent,
        "URL": url,
        "PROVIDER": provider,
        "MODEL": model,
        "TEMPLATE": template,
        "GATEWAY_VERSION": gateway_version,
        "SERVER_INFO": server_info,
        "PYTHON_VERSION": python_version,
        "CONTEXT_JSON": context_block,
    }

    out_md = str(template_md)
    for k, v in replacements.items():
        out_md = out_md.replace(f"{{{{{k}}}}}", str(v))
    if not out_md.endswith("\n"):
        out_md += "\n"

    base = f"{today}_{slug}"
    filename = ""
    path = None
    for i in range(0, 1000):
        name = f"{base}.md" if i == 0 else f"{base}_{i + 1}.md"
        candidate = feature_dir / name
        try:
            with open(candidate, "x", encoding="utf-8") as f:
                f.write(out_md)
            filename = name
            path = candidate
            break
        except FileExistsError:
            continue
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to write feature request: {e}")

    if not filename or path is None:
        raise HTTPException(status_code=409, detail="Could not allocate a unique feature request filename")

    # Do not leak absolute paths; return a stable path relative to the gateway data dir.
    rel_path = f"feature_requests/{filename}"

    proposed_backlog_relpath = ""
    proposed_backlog_item_id: Optional[int] = None
    try:
        repo_root = _triage_repo_root_from_env()
        if repo_root is not None:
            out = _maybe_autobridge_report_to_proposed_backlog(
                report_path=path,
                report_relpath=rel_path,
                report_type="feature",
                repo_root=repo_root,
            )
            if isinstance(out, dict):
                proposed_backlog_relpath = str(out.get("relpath") or "").strip()
                bid = out.get("item_id")
                if isinstance(bid, int):
                    proposed_backlog_item_id = bid
    except Exception:
        # Best-effort only; do not fail report filing.
        pass
    return FeatureReportCreateResponse(
        ok=True,
        filename=filename,
        path=rel_path,
        template_path="feature_requests/template.md",
        created_at=created_at,
        session_id=sid,
        session_memory_run_id=session_memory_run_id,
        active_run_id=active_run_id or None,
        proposed_backlog_relpath=proposed_backlog_relpath,
        proposed_backlog_item_id=proposed_backlog_item_id,
    )

#
# ---------------------------------------------------------------------------
# Report inbox + triage (gateway-local)
# ---------------------------------------------------------------------------

_SAFE_INBOX_FILENAME_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}_.+\.md$")
_SAFE_DECISION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{6,80}$")


def _safe_inbox_filename(name: str) -> Optional[str]:
    raw = str(name or "").strip()
    if not raw or "/" in raw or "\\" in raw:
        return None
    if raw == "template.md":
        return None
    if not raw.lower().endswith(".md"):
        return None
    if not _SAFE_INBOX_FILENAME_RE.match(raw):
        # Best-effort: still allow simple safe names for older files.
        if re.fullmatch(r"[a-zA-Z0-9._-]{1,180}\.md", raw):
            return raw
        return None
    return raw


def _safe_decision_id(value: str) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    if not _SAFE_DECISION_ID_RE.match(raw):
        return None
    return raw


def _read_text_bounded(path: Path, *, max_chars: int = 300_000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read file: {e}")
    if len(text) > int(max_chars):
        text = text[: int(max_chars)] + "\n…(truncated)…\n"
    return text


def _sha256_hex_text(text: str) -> str:
    try:
        h = hashlib.sha256()
        h.update(str(text or "").encode("utf-8", errors="ignore"))
        return h.hexdigest()
    except Exception:
        return ""


def _load_json_bounded(path: Path, *, max_chars: int = 1_200_000) -> Dict[str, Any]:
    raw = _read_text_bounded(path, max_chars=max_chars)
    try:
        obj = json.loads(raw)
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _gateway_base_dir() -> Path:
    svc = get_gateway_service()
    return Path(getattr(getattr(svc, "stores", None), "base_dir", Path("."))).expanduser().resolve()


def _triage_repo_root_from_env() -> Optional[Path]:
    raw = str(os.getenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT") or os.getenv("ABSTRACT_TRIAGE_REPO_ROOT") or "").strip()
    if not raw:
        return None
    try:
        return Path(raw).expanduser().resolve()
    except Exception:
        return None


def _decision_to_summary(decision: Any) -> TriageDecisionSummary:
    duplicates: list[Any] = []
    raw_dups = getattr(decision, "duplicates", None)
    if isinstance(raw_dups, list):
        for d in raw_dups:
            if hasattr(d, "__dict__"):
                duplicates.append(dict(d.__dict__))
            elif isinstance(d, dict):
                duplicates.append(dict(d))

    missing = getattr(decision, "missing_fields", None)
    missing2 = [str(m).strip() for m in missing if isinstance(m, str) and m.strip()] if isinstance(missing, list) else []

    return TriageDecisionSummary(
        decision_id=str(getattr(decision, "decision_id", "") or "").strip(),
        report_type=str(getattr(decision, "report_type", "") or "").strip(),
        report_relpath=str(getattr(decision, "report_relpath", "") or "").strip(),
        status=str(getattr(decision, "status", "") or "").strip(),
        created_at=str(getattr(decision, "created_at", "") or "").strip(),
        updated_at=str(getattr(decision, "updated_at", "") or "").strip(),
        defer_until=str(getattr(decision, "defer_until", "") or "").strip(),
        missing_fields=missing2,
        duplicates=[d for d in duplicates if isinstance(d, dict)],
        draft_relpath=str(getattr(decision, "draft_relpath", "") or "").strip(),
    )


def _list_report_inbox_items(*, report_type: str, report_dir: Path, gateway_base_dir: Path) -> List[ReportInboxItem]:
    try:
        from ..maintenance.report_parser import parse_report_file  # type: ignore
        from ..maintenance.triage_queue import decision_id_for_report, decisions_dir, load_decision  # type: ignore
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Maintenance modules unavailable: {e}")

    qdir = decisions_dir(gateway_data_dir=gateway_base_dir)

    out: List[ReportInboxItem] = []
    for p in sorted(report_dir.glob("*.md")):
        safe = _safe_inbox_filename(p.name)
        if safe is None:
            continue
        relpath = f"{'bug_reports' if report_type == 'bug' else 'feature_requests'}/{safe}"
        try:
            rec = parse_report_file(p)
        except Exception:
            continue

        did = decision_id_for_report(report_relpath=relpath)
        decision = load_decision(dir_path=qdir, decision_id=did)
        status = str(getattr(decision, "status", "") or "").strip() if decision is not None else ""

        out.append(
            ReportInboxItem(
                report_type=report_type,
                filename=safe,
                relpath=relpath,
                title=str(getattr(rec.header, "title", "") or "").strip() or safe,
                created_at=str(getattr(rec.header, "created_at", "") or "").strip(),
                session_id=str(getattr(rec.header, "session_id", "") or "").strip(),
                workflow_id=str(getattr(rec.header, "workflow_id", "") or "").strip(),
                active_run_id=str(getattr(rec.header, "active_run_id", "") or "").strip(),
                decision_id=str(did),
                triage_status=status,
            )
        )
    return out


@router.get("/reports/bugs", response_model=ReportInboxListResponse)
async def list_bug_reports() -> ReportInboxListResponse:
    base = _gateway_base_dir()
    bug_dir = _gateway_bug_reports_dir()
    items = _list_report_inbox_items(report_type="bug", report_dir=bug_dir, gateway_base_dir=base)
    items.sort(key=lambda i: (str(i.created_at or ""), i.filename), reverse=True)
    return ReportInboxListResponse(items=items)


@router.get("/reports/features", response_model=ReportInboxListResponse)
async def list_feature_requests() -> ReportInboxListResponse:
    base = _gateway_base_dir()
    feature_dir = _gateway_feature_requests_dir()
    items = _list_report_inbox_items(report_type="feature", report_dir=feature_dir, gateway_base_dir=base)
    items.sort(key=lambda i: (str(i.created_at or ""), i.filename), reverse=True)
    return ReportInboxListResponse(items=items)


@router.get("/reports/bugs/{filename}/content", response_model=ReportContentResponse)
async def get_bug_report_content(filename: str) -> ReportContentResponse:
    safe = _safe_inbox_filename(filename)
    if safe is None:
        raise HTTPException(status_code=400, detail="Invalid filename")
    bug_dir = _gateway_bug_reports_dir()
    path = bug_dir / safe
    if not path.exists():
        raise HTTPException(status_code=404, detail="Bug report not found")
    content = _read_text_bounded(path, max_chars=400_000)
    return ReportContentResponse(report_type="bug", filename=safe, relpath=f"bug_reports/{safe}", content=content)


@router.get("/reports/features/{filename}/content", response_model=ReportContentResponse)
async def get_feature_request_content(filename: str) -> ReportContentResponse:
    safe = _safe_inbox_filename(filename)
    if safe is None:
        raise HTTPException(status_code=400, detail="Invalid filename")
    feature_dir = _gateway_feature_requests_dir()
    path = feature_dir / safe
    if not path.exists():
        raise HTTPException(status_code=404, detail="Feature request not found")
    content = _read_text_bounded(path, max_chars=400_000)
    return ReportContentResponse(report_type="feature", filename=safe, relpath=f"feature_requests/{safe}", content=content)


def _require_runtime_email_helpers() -> tuple[Any, Any, Any, Any]:
    try:
        from abstractruntime.integrations.abstractcore import list_email_accounts, list_emails, read_email, send_email
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Email helpers are not available: {e}") from e
    return list_email_accounts, list_emails, read_email, send_email


def _email_tool_payload_or_error(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail="Email tool returned a non-dict payload")
    if not bool(payload.get("success")):
        err = str(payload.get("error") or "Email tool failed")
        err_norm = err.strip().lower()
        if any(
            needle in err_norm
            for needle in (
                "is required",
                "must be",
                "unknown account",
                "specify account",
                "status must be",
            )
        ):
            raise HTTPException(status_code=400, detail=err)
        raise HTTPException(status_code=500, detail=err)
    return payload


@router.get("/email/accounts", response_model=EmailAccountsResponse)
async def email_list_accounts() -> EmailAccountsResponse:
    list_email_accounts, _list_emails, _read_email, _send_email = _require_runtime_email_helpers()
    payload = _email_tool_payload_or_error(list_email_accounts())
    items: list[EmailAccountInfo] = []
    for raw in payload.get("accounts") or []:
        if not isinstance(raw, dict):
            continue
        items.append(
            EmailAccountInfo(
                account=str(raw.get("account") or ""),
                email=str(raw.get("email") or ""),
                from_email=str(raw.get("from_email") or "") or None,
                can_read=bool(raw.get("can_read")),
                can_send=bool(raw.get("can_send")),
                imap_password_set=raw.get("imap_password_set") if raw.get("imap_password_set") in {True, False, None} else None,
                smtp_password_set=raw.get("smtp_password_set") if raw.get("smtp_password_set") in {True, False, None} else None,
            )
        )

    return EmailAccountsResponse(
        source=str(payload.get("source") or ""),
        config_path=str(payload.get("config_path") or ""),
        default_account=str(payload.get("default_account") or ""),
        accounts=items,
    )


@router.get("/email/messages", response_model=EmailListResponse)
async def email_list_messages(
    account: str = Query(default="", description="Optional account name (required if multiple configured)."),
    mailbox: str = Query(default="", description="Optional mailbox override (default: account config or INBOX)."),
    since: str = Query(default="", description="Optional since filter (e.g. '7d' or ISO8601)."),
    status: str = Query(default="all", description="all|unread|read"),
    limit: int = Query(default=20, ge=1, le=200),
) -> EmailListResponse:
    _list_email_accounts, list_emails, _read_email, _send_email = _require_runtime_email_helpers()
    payload = _email_tool_payload_or_error(
        list_emails(
            account=str(account or "").strip() or None,
            mailbox=str(mailbox or "").strip() or None,
            since=str(since or "").strip() or None,
            status=str(status or "").strip() or "all",
            limit=int(limit),
        )
    )
    filt = payload.get("filter") if isinstance(payload.get("filter"), dict) else {}
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []

    out_messages: list[EmailMessageSummary] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        out_messages.append(
            EmailMessageSummary(
                uid=str(m.get("uid") or ""),
                message_id=str(m.get("message_id") or ""),
                subject=str(m.get("subject") or ""),
                from_=str(m.get("from") or ""),
                to=str(m.get("to") or ""),
                date=str(m.get("date") or ""),
                flags=[str(x) for x in (m.get("flags") or []) if isinstance(x, str)],
                seen=bool(m.get("seen")),
                size=int(m.get("size")) if isinstance(m.get("size"), int) else None,
            )
        )

    return EmailListResponse(
        account=str(payload.get("account") or ""),
        mailbox=str(payload.get("mailbox") or ""),
        filter=EmailListFilter(
            since=str(filt.get("since") or "") or None,
            status=str(filt.get("status") or "all"),
            limit=int(filt.get("limit") or 0) or int(limit),
        ),
        counts=EmailListCounts(
            returned=int(counts.get("returned") or 0),
            unread=int(counts.get("unread") or 0),
            read=int(counts.get("read") or 0),
        ),
        messages=out_messages,
    )


@router.get("/email/messages/{uid}", response_model=EmailReadResponse)
async def email_read_message(
    uid: str,
    account: str = Query(default="", description="Optional account name (required if multiple configured)."),
    mailbox: str = Query(default="", description="Optional mailbox override (default: account config or INBOX)."),
    max_body_chars: int = Query(default=20000, ge=1000, le=200000),
) -> EmailReadResponse:
    _list_email_accounts, _list_emails, read_email, _send_email = _require_runtime_email_helpers()
    payload = _email_tool_payload_or_error(
        read_email(
            uid=str(uid or "").strip(),
            account=str(account or "").strip() or None,
            mailbox=str(mailbox or "").strip() or None,
            max_body_chars=int(max_body_chars),
        )
    )
    attachments_raw = payload.get("attachments") if isinstance(payload.get("attachments"), list) else []
    attachments: list[EmailAttachmentInfo] = []
    for a in attachments_raw:
        if not isinstance(a, dict):
            continue
        attachments.append(
            EmailAttachmentInfo(
                filename=str(a.get("filename") or ""),
                content_type=str(a.get("content_type") or ""),
            )
        )

    return EmailReadResponse(
        account=str(payload.get("account") or ""),
        mailbox=str(payload.get("mailbox") or ""),
        uid=str(payload.get("uid") or ""),
        message_id=str(payload.get("message_id") or ""),
        subject=str(payload.get("subject") or ""),
        from_=str(payload.get("from") or ""),
        to=str(payload.get("to") or ""),
        cc=str(payload.get("cc") or ""),
        date=str(payload.get("date") or ""),
        flags=[str(x) for x in (payload.get("flags") or []) if isinstance(x, str)],
        seen=bool(payload.get("seen")),
        body_text=str(payload.get("body_text") or ""),
        body_html=str(payload.get("body_html") or ""),
        attachments=attachments,
    )


@router.post("/email/send", response_model=EmailSendResponse)
async def email_send_message(req: EmailSendRequest) -> EmailSendResponse:
    _list_email_accounts, _list_emails, _read_email, send_email = _require_runtime_email_helpers()
    payload = _email_tool_payload_or_error(
        send_email(
            to=req.to,
            subject=str(req.subject or ""),
            account=str(req.account or "").strip() or None,
            body_text=req.body_text,
            body_html=req.body_html,
            cc=req.cc,
            bcc=req.bcc,
            headers=req.headers,
        )
    )
    smtp_raw = payload.get("smtp") if isinstance(payload.get("smtp"), dict) else {}
    smtp = EmailSmtpInfo(
        host=str(smtp_raw.get("host") or ""),
        port=int(smtp_raw.get("port") or 0),
        username=str(smtp_raw.get("username") or ""),
        starttls=bool(smtp_raw.get("starttls")) if smtp_raw.get("starttls") in {True, False} else True,
    )
    return EmailSendResponse(
        account=str(payload.get("account") or ""),
        message_id=str(payload.get("message_id") or ""),
        from_=str(payload.get("from") or ""),
        to=[str(x) for x in (payload.get("to") or []) if isinstance(x, str)],
        cc=[str(x) for x in (payload.get("cc") or []) if isinstance(x, str)],
        bcc=[str(x) for x in (payload.get("bcc") or []) if isinstance(x, str)],
        smtp=smtp,
    )


@router.post("/triage/run", response_model=TriageRunResponse)
async def triage_run(req: TriageRunRequest) -> TriageRunResponse:
    try:
        from ..maintenance.triage import triage_reports  # type: ignore
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Triage unavailable: {e}")

    base = _gateway_base_dir()
    repo_root = _triage_repo_root_from_env()

    out = triage_reports(
        gateway_data_dir=base,
        repo_root=repo_root,
        write_drafts=bool(req.write_drafts) and repo_root is not None,
        enable_llm=bool(req.enable_llm),
    )
    return TriageRunResponse(
        ok=True,
        reports=int(out.get("reports") or 0),
        updated_decisions=int(out.get("updated_decisions") or 0),
        decisions_dir=str(out.get("decisions_dir") or ""),
        drafts_written=list(out.get("drafts_written") or []),
    )


@router.get("/triage/decisions", response_model=TriageDecisionListResponse)
async def triage_list_decisions(
    status: str = Query(default="", description="Optional filter: pending|approved|deferred|rejected"),
    limit: int = Query(default=200, ge=1, le=1000),
) -> TriageDecisionListResponse:
    try:
        from ..maintenance.triage_queue import decisions_dir, iter_decisions  # type: ignore
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Triage unavailable: {e}")

    base = _gateway_base_dir()
    qdir = decisions_dir(gateway_data_dir=base)
    decisions = iter_decisions(qdir)

    wanted = str(status or "").strip().lower()
    if wanted:
        decisions = [d for d in decisions if str(getattr(d, "status", "") or "").strip().lower() == wanted]

    # Sort newest first.
    def _key(d: Any) -> Tuple[str, str]:
        return (str(getattr(d, "updated_at", "") or ""), str(getattr(d, "created_at", "") or ""))

    decisions.sort(key=_key, reverse=True)
    decisions = decisions[: int(limit)]

    return TriageDecisionListResponse(decisions=[_decision_to_summary(d) for d in decisions])


@router.post("/triage/decisions/{decision_id}/apply", response_model=TriageDecisionSummary)
async def triage_apply_decision(decision_id: str, req: TriageDecisionApplyRequest) -> TriageDecisionSummary:
    safe = _safe_decision_id(decision_id)
    if safe is None:
        raise HTTPException(status_code=400, detail="Invalid decision_id")

    try:
        from ..maintenance.triage import apply_decision_action  # type: ignore
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Triage unavailable: {e}")

    base = _gateway_base_dir()
    repo_root = _triage_repo_root_from_env()
    decision, err = apply_decision_action(
        gateway_data_dir=base,
        decision_id=safe,
        action=str(req.action or ""),
        repo_root=repo_root,
        defer_days=req.defer_days,
    )
    if err:
        raise HTTPException(status_code=400, detail=err)
    if decision is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    return _decision_to_summary(decision)


@router.get("/backlog/template", response_model=BacklogTemplateResponse)
async def backlog_template() -> BacklogTemplateResponse:
    repo_root = _triage_repo_root_from_env()
    if repo_root is None:
        raise HTTPException(status_code=404, detail="Backlog browsing not configured on this gateway")

    template_md = _read_backlog_template(repo_root)
    sha = _sha256_hex_text(template_md)
    return BacklogTemplateResponse(relpath="docs/backlog/template.md", sha256=sha, content=template_md)


@router.get("/backlog/exec/config", response_model=BacklogExecConfigResponse)
async def backlog_exec_config() -> BacklogExecConfigResponse:
    repo_root = _triage_repo_root_from_env()
    if repo_root is None:
        raise HTTPException(status_code=404, detail="Backlog browsing not configured on this gateway")

    try:
        from ..maintenance.backlog_exec_runner import BacklogExecRunnerConfig, _resolve_executor  # type: ignore
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backlog exec runner unavailable: {e}")

    cfg = BacklogExecRunnerConfig.from_env()
    runner = backlog_exec_runner_status()
    runner_alive = bool(runner.get("alive") is True)
    runner_error = str(runner.get("error") or "").strip() or None
    can_execute = bool(cfg.enabled) and runner_alive and _resolve_executor(cfg) is not None

    codex_available: Optional[bool] = None
    codex_bin: Optional[str] = None
    codex_model: Optional[str] = None
    codex_reasoning_effort: Optional[str] = None
    if str(cfg.executor or "").strip().lower() in {"codex", "codex_cli", "codex-cli"}:
        codex_bin = str(cfg.codex_bin or "").strip() or "codex"
        try:
            from ..maintenance.backlog_exec_runner import normalize_codex_model_id  # type: ignore
        except Exception:
            normalize_codex_model_id = None  # type: ignore

        raw_model = str(cfg.codex_model or "").strip() or "gpt-5.2"
        codex_reasoning_effort = str(getattr(cfg, "codex_reasoning_effort", "") or "").strip() or None
        if callable(normalize_codex_model_id):
            try:
                codex_model = str(normalize_codex_model_id(raw_model))
            except Exception:
                codex_model = raw_model
        else:
            codex_model = raw_model
        try:
            codex_available = bool(shutil.which(codex_bin))
        except Exception:
            codex_available = None
        if codex_available is False:
            can_execute = False

    return BacklogExecConfigResponse(
        runner_enabled=bool(cfg.enabled),
        runner_alive=bool(runner_alive),
        runner_error=runner_error,
        can_execute=bool(can_execute),
        executor=str(cfg.executor or "none").strip().lower() or "none",
        notify=bool(cfg.notify),
        codex_bin=codex_bin,
        codex_model=codex_model,
        codex_reasoning_effort=codex_reasoning_effort,
        codex_available=codex_available,
    )


def _exec_request_summary(req: Dict[str, Any], *, request_id: str) -> BacklogExecRequestSummary:
    status = str(req.get("status") or "").strip() or "unknown"
    created_at = str(req.get("created_at") or "").strip() or None
    started_at = str(req.get("started_at") or "").strip() or None
    finished_at = str(req.get("finished_at") or "").strip() or None

    backlog = req.get("backlog") if isinstance(req.get("backlog"), dict) else {}
    backlog_rel = str(backlog.get("relpath") or "").strip() or None
    backlog_kind = str(backlog.get("kind") or "").strip() or None
    backlog_filename = str(backlog.get("filename") or "").strip() or None

    target_agent = str(req.get("target_agent") or "").strip() or None
    target_model = str(req.get("target_model") or "").strip() or None
    target_reasoning_effort = str(req.get("target_reasoning_effort") or "").strip() or None
    executor_info = req.get("executor") if isinstance(req.get("executor"), dict) else {}
    executor_type = str(executor_info.get("type") or "").strip() or None
    if not target_model:
        target_model = str(executor_info.get("model") or "").strip() or None
    if not target_reasoning_effort:
        target_reasoning_effort = str(executor_info.get("reasoning_effort") or "").strip() or None

    result = req.get("result") if isinstance(req.get("result"), dict) else {}
    ok_val = result.get("ok")
    ok: Optional[bool] = bool(ok_val) if isinstance(ok_val, bool) else None
    exit_code_raw = result.get("exit_code")
    try:
        exit_code = int(exit_code_raw) if exit_code_raw is not None else None
    except Exception:
        exit_code = None
    error = str(result.get("error") or "").strip() or None
    run_dir_relpath = str(req.get("run_dir_relpath") or "").strip() or None
    last_msg = str(result.get("last_message") or "").strip()
    last_msg = last_msg[:1200] if last_msg else ""

    return BacklogExecRequestSummary(
        request_id=request_id,
        status=status,
        created_at=created_at,
        started_at=started_at,
        finished_at=finished_at,
        backlog_relpath=backlog_rel,
        backlog_kind=backlog_kind,
        backlog_filename=backlog_filename,
        target_agent=target_agent,
        target_model=target_model,
        target_reasoning_effort=target_reasoning_effort,
        executor_type=executor_type,
        ok=ok,
        exit_code=exit_code,
        error=error,
        run_dir_relpath=run_dir_relpath,
        last_message=last_msg or None,
    )


def _backlog_exec_active_items_from_queue(
    *,
    qdir: Path,
    wanted_statuses: set[str],
    limit_requests: int = 2000,
    limit_items: int = 1200,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen_rel: set[str] = set()

    if not qdir.exists():
        return []

    paths = list(qdir.glob("*.json"))
    try:
        paths.sort(key=lambda p: float(p.stat().st_mtime), reverse=True)
    except Exception:
        paths.sort(key=lambda p: p.name, reverse=True)

    scanned = 0
    for p in paths:
        scanned += 1
        if scanned > int(limit_requests):
            break
        if len(out) >= int(limit_items):
            break
        try:
            p.relative_to(qdir)
        except Exception:
            continue
        req = _load_json_bounded(p)
        st = str(req.get("status") or "").strip().lower() or "unknown"
        if st not in wanted_statuses:
            continue
        rid = str(req.get("request_id") or p.stem).strip().lower()
        rid = _safe_backlog_exec_request_id(rid) or ""
        if not rid:
            continue

        def _add_item(kind: str, filename: str, relpath: str) -> None:
            nonlocal out
            k = str(kind or "").strip()
            fn = str(filename or "").strip()
            rel = str(relpath or "").strip().replace("\\", "/")
            if not k or not fn or not rel:
                return
            if rel in seen_rel:
                return
            if len(out) >= int(limit_items):
                return
            seen_rel.add(rel)
            out.append({"request_id": rid, "status": st, "kind": k, "filename": fn, "relpath": rel})

        backlog = req.get("backlog") if isinstance(req.get("backlog"), dict) else {}
        bk = str(backlog.get("kind") or "").strip()
        bf = str(backlog.get("filename") or "").strip()
        br = str(backlog.get("relpath") or "").strip()
        if br and bk and bf and not bf.lower().startswith("batch("):
            _add_item(bk, bf, br)

        bq = req.get("backlog_queue") if isinstance(req.get("backlog_queue"), dict) else {}
        items = bq.get("items") if isinstance(bq.get("items"), list) else []
        for it in items[:200]:
            if len(out) >= int(limit_items):
                break
            if not isinstance(it, dict):
                continue
            k2 = str(it.get("kind") or "").strip()
            fn2 = str(it.get("filename") or "").strip()
            if not k2 or not fn2:
                continue
            rel2 = f"docs/backlog/{k2}/{fn2}"
            _add_item(k2, fn2, rel2)

    return out


@router.get("/backlog/exec/requests", response_model=BacklogExecRequestListResponse)
async def backlog_exec_requests(
    status: str = Query(
        default="",
        description="Optional comma-separated statuses (queued|running|awaiting_qa|completed|failed|promoted).",
    ),
    limit: int = Query(default=200, ge=1, le=1000),
) -> BacklogExecRequestListResponse:
    repo_root = _triage_repo_root_from_env()
    if repo_root is None:
        raise HTTPException(status_code=404, detail="Backlog browsing not configured on this gateway")

    base = _gateway_base_dir()
    qdir = (base / "backlog_exec_queue").resolve()
    if not qdir.exists():
        return BacklogExecRequestListResponse(requests=[])

    wanted_raw = [s.strip().lower() for s in str(status or "").split(",") if s.strip()]
    wanted = set(wanted_raw)

    paths = list(qdir.glob("*.json"))
    try:
        paths.sort(key=lambda p: float(p.stat().st_mtime), reverse=True)
    except Exception:
        paths.sort(key=lambda p: p.name, reverse=True)

    items: list[Tuple[str, BacklogExecRequestSummary]] = []
    scanned = 0
    for p in paths:
        scanned += 1
        if scanned > 5000:
            break
        try:
            p.relative_to(qdir)
        except Exception:
            continue
        req = _load_json_bounded(p)
        rid = str(req.get("request_id") or p.stem).strip().lower()
        rid = _safe_backlog_exec_request_id(rid) or ""
        if not rid:
            continue
        summary = _exec_request_summary(req, request_id=rid)
        if wanted and summary.status.strip().lower() not in wanted:
            continue
        key = summary.created_at or summary.started_at or summary.finished_at or ""
        items.append((key, summary))
        if len(items) >= int(limit):
            break

    # Newest first (best-effort, ISO timestamps sort lexicographically).
    items.sort(key=lambda t: t[0], reverse=True)
    out = [s for _, s in items[: int(limit)]]
    return BacklogExecRequestListResponse(requests=out)


@router.get("/backlog/exec/active_items", response_model=BacklogExecActiveItemsResponse)
async def backlog_exec_active_items(
    status: str = Query(
        default="queued,running,awaiting_qa",
        description="Optional comma-separated statuses (queued|running|awaiting_qa).",
    ),
    limit: int = Query(default=600, ge=1, le=2000),
) -> BacklogExecActiveItemsResponse:
    """Return backlog item relpaths currently queued/running in the backlog exec queue.

    This is used by thin clients to hide items from `docs/backlog/planned/` while they are being processed,
    reducing accidental duplicate executions.
    """
    repo_root = _triage_repo_root_from_env()
    if repo_root is None:
        raise HTTPException(status_code=404, detail="Backlog browsing not configured on this gateway")

    base = _gateway_base_dir()
    qdir = (base / "backlog_exec_queue").resolve()
    if not qdir.exists():
        return BacklogExecActiveItemsResponse(items=[])

    wanted_raw = [s.strip().lower() for s in str(status or "").split(",") if s.strip()]
    wanted = set(wanted_raw) if wanted_raw else {"queued", "running"}

    raw = _backlog_exec_active_items_from_queue(qdir=qdir, wanted_statuses=wanted, limit_items=int(limit))
    items = [BacklogExecActiveItem(**it) for it in raw[: int(limit)]]
    return BacklogExecActiveItemsResponse(items=items)


@router.get("/backlog/exec/requests/{request_id}", response_model=BacklogExecRequestDetailResponse)
async def backlog_exec_request_detail(
    request_id: str,
    include_prompt: bool = Query(default=False, description="Include the queued prompt (may be large)."),
) -> BacklogExecRequestDetailResponse:
    repo_root = _triage_repo_root_from_env()
    if repo_root is None:
        raise HTTPException(status_code=404, detail="Backlog browsing not configured on this gateway")

    rid = _safe_backlog_exec_request_id(request_id)
    if rid is None:
        raise HTTPException(status_code=400, detail="Invalid request_id")

    base = _gateway_base_dir()
    qdir = (base / "backlog_exec_queue").resolve()
    path = (qdir / f"{rid}.json").resolve()
    try:
        path.relative_to(qdir)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Request not found")

    req = _load_json_bounded(path)
    if not include_prompt and "prompt" in req:
        req = dict(req)
        req.pop("prompt", None)

    return BacklogExecRequestDetailResponse(request_id=rid, payload=req)


def _write_json_atomic(path: Path, obj: Dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    data = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)


def _extract_patch_paths(patch_text: str) -> List[str]:
    """Extract file paths referenced by a git patch (best-effort)."""
    out: List[str] = []
    rx = re.compile(r"^diff --git a/(.+?) b/(.+?)\r?$")
    for line in str(patch_text or "").splitlines():
        m = rx.match(line)
        if not m:
            continue
        for raw in (m.group(1), m.group(2)):
            s = str(raw or "").strip()
            if not s or s == "/dev/null":
                continue
            # Git may quote paths when they contain special chars: a/"foo bar"
            if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
                s = s[1:-1]
            out.append(s)
    # De-dupe (preserve order).
    seen: set[str] = set()
    deduped: List[str] = []
    for p in out:
        ps = str(p).replace("\\", "/").strip()
        if not ps or ps in seen:
            continue
        seen.add(ps)
        deduped.append(ps)
    return deduped


def _is_safe_repo_relpath(relpath: str) -> bool:
    s = str(relpath or "").replace("\\", "/").strip()
    if not s:
        return False
    if s.startswith("/") or s.startswith("~"):
        return False
    # Avoid Windows drive paths.
    if re.match(r"^[a-zA-Z]:/", s):
        return False
    parts = [p for p in s.split("/") if p]
    if not parts:
        return False
    return all(p not in {"..", "."} for p in parts)


def _sha256_hex_bytes(data: bytes) -> str:
    try:
        h = hashlib.sha256()
        h.update(data)
        return h.hexdigest()
    except Exception:
        return ""


@router.post("/backlog/exec/requests/{request_id}/feedback", response_model=BacklogExecRequestDetailResponse)
async def backlog_exec_request_feedback(request_id: str, req: BacklogExecFeedbackRequest) -> BacklogExecRequestDetailResponse:
    repo_root = _triage_repo_root_from_env()
    if repo_root is None:
        raise HTTPException(status_code=404, detail="Backlog browsing not configured on this gateway")

    rid = _safe_backlog_exec_request_id(request_id)
    if rid is None:
        raise HTTPException(status_code=400, detail="Invalid request_id")

    feedback = str(req.feedback or "")
    if not feedback.strip():
        raise HTTPException(status_code=400, detail="feedback is required")
    if len(feedback) > 20_000:
        raise HTTPException(status_code=413, detail="feedback is too large (max 20,000 chars)")

    base = _gateway_base_dir()
    qdir = (base / "backlog_exec_queue").resolve()
    path = (qdir / f"{rid}.json").resolve()
    try:
        path.relative_to(qdir)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Request not found")

    payload = _load_json_bounded(path)
    status = str(payload.get("status") or "").strip().lower() or "unknown"
    if status not in {"awaiting_qa", "failed", "completed"}:
        raise HTTPException(status_code=409, detail=f"Cannot add feedback when status={status!r}")

    exec_mode = str(payload.get("execution_mode") or "").strip().lower() or "uat"
    if exec_mode == "candidate":
        exec_mode = "uat"

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    attempt_raw = payload.get("attempt")
    try:
        attempt = int(attempt_raw) if attempt_raw is not None else 1
    except Exception:
        attempt = 1
    next_attempt = max(1, attempt + 1)

    # Preserve previous result for auditability.
    rh = payload.get("result_history") if isinstance(payload.get("result_history"), list) else []
    prev_result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    if prev_result:
        rh.append(
            {
                "at": now,
                "attempt": attempt,
                "status": status,
                "result": prev_result,
                "candidate_patch_relpath": str(payload.get("candidate_patch_relpath") or ""),
                "candidate_manifest_relpath": str(payload.get("candidate_manifest_relpath") or ""),
            }
        )
        if len(rh) > 25:
            rh = rh[-25:]

    fb = payload.get("feedback") if isinstance(payload.get("feedback"), list) else []
    fb.append({"at": now, "attempt": next_attempt, "text": feedback})
    if len(fb) > 50:
        fb = fb[-50:]

    payload["attempt"] = next_attempt
    payload["feedback"] = fb
    payload["result_history"] = rh

    payload["status"] = "queued"
    payload["started_at"] = ""
    payload["finished_at"] = ""
    payload["result"] = {}
    payload["candidate_patch_relpath"] = ""
    payload["candidate_manifest_relpath"] = ""
    payload.pop("uat_pending", None)
    payload.pop("uat_lock_owner_request_id", None)
    payload.pop("uat_lock_acquired", None)
    payload.pop("inplace_warning", None)

    try:
        _write_json_atomic(path, payload)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write request: {e}")

    # Release UAT lock (if owned).
    # This keeps the single shared UAT stack moving forward without manual intervention.
    feedback_report: Dict[str, Any] = {"at": now}
    lock_released = False
    lock_owner = ""
    try:
        lock_path = (base / "uat_deploy_lock.json").resolve()
        lock_path.relative_to(base)
        if lock_path.exists():
            lock_obj = _load_json_bounded(lock_path)
            lock_owner = str(lock_obj.get("owner_request_id") or "").strip()
            if lock_owner == rid:
                lock_path.unlink()
                lock_released = True
    except Exception as e:
        feedback_report["uat_lock_release_error"] = str(e)
    feedback_report["uat_lock_owner_request_id"] = lock_owner
    feedback_report["uat_lock_released"] = bool(lock_released)

    # No UAT auto-deploy: operator manually deploys/restarts UAT for a specific request.

    # Stop UAT after operator decision so UAT is short-lived (ports/services do not linger).
    if exec_mode == "uat" and lock_owner == rid:
        stop_report: Dict[str, Any] = {"at": now}
        if _process_manager_enabled():
            try:
                mgr = _require_process_manager()
                stopped: Dict[str, Any] = {}
                for pid in (
                    "abstractcode_web_uat",
                    "abstractobserver_uat",
                    "abstractflow_frontend_uat",
                    "abstractflow_backend_uat",
                    "gateway_uat",
                ):
                    try:
                        stopped[pid] = mgr.stop(pid)
                    except Exception as e:
                        stopped[pid] = {"status": "error", "error": str(e)}
                stop_report["processes"] = stopped
            except Exception as e:
                stop_report["error"] = str(e)
        else:
            stop_report["processes"] = {"status": "skipped", "reason": "process_manager_disabled"}
        feedback_report["uat_stop"] = stop_report

    payload["feedback_report"] = feedback_report
    try:
        _write_json_atomic(path, payload)
    except Exception as e:
        payload.setdefault("feedback_report", {})["writeback_error"] = str(e)

    # Hide prompt by default (the normal detail endpoint does that).
    sanitized = dict(payload)
    sanitized.pop("prompt", None)
    return BacklogExecRequestDetailResponse(request_id=rid, payload=sanitized)


@router.post("/backlog/exec/requests/{request_id}/promote", response_model=BacklogExecRequestDetailResponse)
async def backlog_exec_request_promote(request_id: str, req: BacklogExecPromoteRequest) -> BacklogExecRequestDetailResponse:
    repo_root = _triage_repo_root_from_env()
    if repo_root is None:
        raise HTTPException(status_code=404, detail="Backlog browsing not configured on this gateway")

    rid = _safe_backlog_exec_request_id(request_id)
    if rid is None:
        raise HTTPException(status_code=400, detail="Invalid request_id")

    base = _gateway_base_dir()
    qdir = (base / "backlog_exec_queue").resolve()
    path = (qdir / f"{rid}.json").resolve()
    try:
        path.relative_to(qdir)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Request not found")

    payload = _load_json_bounded(path)
    status = str(payload.get("status") or "").strip().lower() or "unknown"
    if status != "awaiting_qa":
        raise HTTPException(status_code=409, detail=f"Cannot promote when status={status!r}")

    exec_mode = str(payload.get("execution_mode") or "").strip().lower() or "uat"
    if exec_mode == "candidate":
        exec_mode = "uat"
    if exec_mode not in {"uat", "inplace"}:
        raise HTTPException(status_code=400, detail=f"Unsupported execution_mode={exec_mode!r}")

    promotion_summary: Dict[str, Any] = {}

    if exec_mode == "uat":
        # Candidate workspace root (under repo_root).
        candidate_rel = str(payload.get("candidate_relpath") or "").strip()
        if not candidate_rel or not _is_safe_repo_relpath(candidate_rel):
            raise HTTPException(status_code=400, detail="No candidate workspace available for promotion")
        candidate_root = (Path(repo_root).resolve() / candidate_rel).resolve()
        try:
            candidate_root.relative_to(Path(repo_root).resolve())
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid candidate workspace path")
        if not candidate_root.exists():
            raise HTTPException(status_code=400, detail="Candidate workspace does not exist")

        # Candidate manifest (produced by the exec runner).
        manifest_rel = str(payload.get("candidate_manifest_relpath") or "").strip()
        run_dir_rel = str(payload.get("run_dir_relpath") or "").strip()
        if not manifest_rel and run_dir_rel:
            manifest_rel = str(Path(run_dir_rel) / "candidate_manifest.json").replace("\\", "/")
        if not manifest_rel:
            raise HTTPException(status_code=400, detail="No candidate manifest available for promotion")

        manifest_path = (base / manifest_rel).resolve()
        try:
            manifest_path.relative_to(base)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid manifest path")
        if not manifest_path.exists():
            raise HTTPException(status_code=400, detail="Candidate manifest not found")

        manifest_bytes = b""
        try:
            manifest_bytes = manifest_path.read_bytes()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to read candidate manifest: {e}")
        if len(manifest_bytes) > 8_000_000:
            raise HTTPException(status_code=413, detail="Candidate manifest is too large")

        try:
            manifest_obj = json.loads(manifest_bytes.decode("utf-8", errors="replace"))
        except Exception:
            manifest_obj = {}
        if not isinstance(manifest_obj, dict):
            raise HTTPException(status_code=400, detail="Invalid candidate manifest")

        repos = manifest_obj.get("repos")
        if not isinstance(repos, list):
            raise HTTPException(status_code=400, detail="Invalid candidate manifest (repos)")

        # Conflict detection (safe-by-default):
        # Promotion is manifest-based (copy/delete), not a git merge/rebase. To avoid clobbering
        # changes that landed in prod after the candidate workspace was created, we compare the
        # current prod file contents to the candidate repo HEAD version. If prod differs from
        # base for any file we plan to copy/delete, we block promotion with 409.
        def _git_run(*, cwd: Path, args: List[str], timeout_s: float = 8.0) -> Tuple[bool, str]:
            try:
                proc = subprocess.run(
                    ["git", *args],
                    cwd=str(cwd),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=float(timeout_s),
                    check=False,
                )
            except Exception:
                return False, ""
            return proc.returncode == 0, str(proc.stdout or "").strip()

        def _base_blob_id(*, repo: Path, rel: str) -> Optional[str]:
            ok, out = _git_run(cwd=repo, args=["rev-parse", f"HEAD:{rel}"])
            if not ok or not out:
                return None
            return out.strip()

        def _file_blob_id(*, repo: Path, rel: str) -> Optional[str]:
            ok, out = _git_run(cwd=repo, args=["hash-object", rel])
            if not ok or not out:
                return None
            return out.strip()

        conflicts: List[Dict[str, Any]] = []

        # First pass: detect conflicts before applying any copy/delete.
        for entry in repos:
            if not isinstance(entry, dict):
                continue
            repo_name = str(entry.get("repo_relpath") or entry.get("repo") or "").strip()
            if not repo_name or "/" in repo_name or "\\" in repo_name or repo_name in {".", ".."}:
                continue
            src_repo = (candidate_root / repo_name).resolve()
            dst_repo = (Path(repo_root).resolve() / repo_name).resolve()
            try:
                src_repo.relative_to(candidate_root)
                dst_repo.relative_to(Path(repo_root).resolve())
            except Exception:
                continue
            if not src_repo.exists() or not dst_repo.exists():
                continue
            # Conflict detection relies on candidate+prod repos being real git repos.
            if not (src_repo / ".git").exists() or not (dst_repo / ".git").exists():
                continue

            copy_list = entry.get("copy")
            del_list = entry.get("delete")
            if not isinstance(copy_list, list):
                copy_list = []
            if not isinstance(del_list, list):
                del_list = []

            for raw in copy_list:
                rel = str(raw or "").replace("\\", "/").strip()
                if not rel or not _is_safe_repo_relpath(rel) or rel.startswith(".git/") or rel == ".git":
                    continue
                # Only consider files that exist in candidate and are regular files (promotion later will validate again).
                src = (src_repo / rel).resolve()
                try:
                    src.relative_to(src_repo)
                except Exception:
                    continue
                if not src.exists() or not src.is_file() or src.is_symlink():
                    continue

                dst = (dst_repo / rel).resolve()
                try:
                    dst.relative_to(dst_repo)
                except Exception:
                    continue
                prod_exists = dst.exists() and dst.is_file() and not dst.is_symlink()

                base_blob = _base_blob_id(repo=src_repo, rel=rel)
                if prod_exists:
                    prod_blob = _file_blob_id(repo=dst_repo, rel=rel)
                    if base_blob is None:
                        conflicts.append({"repo": repo_name, "path": rel, "reason": "prod_created_since_candidate_base"})
                    elif not prod_blob:
                        conflicts.append({"repo": repo_name, "path": rel, "reason": "prod_hash_failed"})
                    elif prod_blob != base_blob:
                        conflicts.append({"repo": repo_name, "path": rel, "reason": "prod_changed_since_candidate_base"})
                else:
                    # If the file existed in the candidate base (tracked at HEAD) but is missing in prod now,
                    # promotion would resurrect/overwrite a deletion; treat as a conflict to force re-run.
                    if base_blob is not None:
                        conflicts.append({"repo": repo_name, "path": rel, "reason": "prod_missing_since_candidate_base"})

            for raw in del_list:
                rel = str(raw or "").replace("\\", "/").strip()
                if not rel or not _is_safe_repo_relpath(rel) or rel.startswith(".git/") or rel == ".git":
                    continue
                dst = (dst_repo / rel).resolve()
                try:
                    dst.relative_to(dst_repo)
                except Exception:
                    continue
                if not dst.exists() or not dst.is_file() or dst.is_symlink():
                    continue

                base_blob = _base_blob_id(repo=src_repo, rel=rel)
                prod_blob = _file_blob_id(repo=dst_repo, rel=rel)
                if base_blob is None:
                    conflicts.append({"repo": repo_name, "path": rel, "reason": "delete_missing_base_blob"})
                elif not prod_blob:
                    conflicts.append({"repo": repo_name, "path": rel, "reason": "prod_hash_failed"})
                elif prod_blob != base_blob:
                    conflicts.append({"repo": repo_name, "path": rel, "reason": "prod_changed_since_candidate_base"})

        if conflicts:
            payload.setdefault("promotion_report", {})["blocked"] = True
            payload["promotion_report"]["reason"] = "conflicts"
            payload["promotion_report"]["conflicts"] = conflicts[:200]
            payload["promotion_report"]["conflicts_total"] = len(conflicts)
            try:
                _write_json_atomic(path, payload)
            except Exception:
                pass
            raise HTTPException(status_code=409, detail="Promotion blocked: prod has diverged from candidate base (conflicts detected)")

        max_total_files = 8000
        max_file_bytes = 25_000_000
        copied = 0
        deleted = 0
        skipped: List[Dict[str, Any]] = []
        applied_copy: List[str] = []
        applied_delete: List[str] = []
        total_seen = 0

        for entry in repos:
            if not isinstance(entry, dict):
                continue
            repo_name = str(entry.get("repo_relpath") or entry.get("repo") or "").strip()
            if not repo_name or "/" in repo_name or "\\" in repo_name or repo_name in {".", ".."}:
                continue
            src_repo = (candidate_root / repo_name).resolve()
            dst_repo = (Path(repo_root).resolve() / repo_name).resolve()
            try:
                src_repo.relative_to(candidate_root)
                dst_repo.relative_to(Path(repo_root).resolve())
            except Exception:
                continue
            if not src_repo.exists() or not dst_repo.exists():
                continue

            copy_list = entry.get("copy")
            del_list = entry.get("delete")
            if not isinstance(copy_list, list):
                copy_list = []
            if not isinstance(del_list, list):
                del_list = []

            for raw in copy_list:
                if total_seen >= max_total_files:
                    break
                rel = str(raw or "").replace("\\", "/").strip()
                if not _is_safe_repo_relpath(rel) or rel.startswith(".git/") or rel == ".git":
                    skipped.append({"repo": repo_name, "path": rel, "reason": "unsafe_path"})
                    continue
                src = (src_repo / rel).resolve()
                dst = (dst_repo / rel).resolve()
                try:
                    src.relative_to(src_repo)
                    dst.relative_to(dst_repo)
                except Exception:
                    skipped.append({"repo": repo_name, "path": rel, "reason": "unsafe_resolution"})
                    continue
                if src.is_symlink():
                    skipped.append({"repo": repo_name, "path": rel, "reason": "symlink_not_allowed"})
                    continue
                if not src.exists() or not src.is_file():
                    skipped.append({"repo": repo_name, "path": rel, "reason": "missing_source"})
                    continue
                try:
                    size = int(src.stat().st_size or 0)
                except Exception:
                    size = 0
                if size > max_file_bytes:
                    skipped.append({"repo": repo_name, "path": rel, "reason": "file_too_large", "bytes": size})
                    continue
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst, follow_symlinks=False)
                    copied += 1
                    total_seen += 1
                    if len(applied_copy) < 200:
                        applied_copy.append(f"{repo_name}/{rel}")
                except Exception as e:
                    skipped.append({"repo": repo_name, "path": rel, "reason": f"copy_failed: {e}"})

            for raw in del_list:
                if total_seen >= max_total_files:
                    break
                rel = str(raw or "").replace("\\", "/").strip()
                if not _is_safe_repo_relpath(rel) or rel.startswith(".git/") or rel == ".git":
                    skipped.append({"repo": repo_name, "path": rel, "reason": "unsafe_path"})
                    continue
                dst = (dst_repo / rel).resolve()
                try:
                    dst.relative_to(dst_repo)
                except Exception:
                    skipped.append({"repo": repo_name, "path": rel, "reason": "unsafe_resolution"})
                    continue
                if not dst.exists():
                    continue
                if dst.is_dir():
                    skipped.append({"repo": repo_name, "path": rel, "reason": "refuse_delete_dir"})
                    continue
                try:
                    dst.unlink()
                    deleted += 1
                    total_seen += 1
                    if len(applied_delete) < 200:
                        applied_delete.append(f"{repo_name}/{rel}")
                except Exception as e:
                    skipped.append({"repo": repo_name, "path": rel, "reason": f"delete_failed: {e}"})

        promotion_summary = {
            "mode": "uat_manifest",
            "candidate_relpath": candidate_rel,
            "candidate_manifest_relpath": manifest_rel,
            "manifest_sha256": _sha256_hex_bytes(manifest_bytes),
            "copied": copied,
            "deleted": deleted,
            "skipped": skipped[:500],
            "applied_copy_sample": applied_copy,
            "applied_delete_sample": applied_delete,
        }
    else:
        promotion_summary = {
            "mode": "inplace",
            "note": "Execution mode was inplace; no promotion copy was performed (prod may already be mutated).",
        }

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    payload["status"] = "promoted"
    payload["promoted_at"] = now
    payload["promotion"] = promotion_summary
    payload["promotion_report"] = {"at": now, "note": "Promotion completed."}

    # Write early so that any stale-lock recovery sees the updated status (avoids a lock handoff race
    # when the exec runner is disabled and the lock file couldn't be deleted for some reason).
    try:
        _write_json_atomic(path, payload)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write request: {e}")

    # Release UAT lock if we own it, so another pending request can deploy.
    lock_owner = ""
    lock_released = False
    try:
        lock_path = (base / "uat_deploy_lock.json").resolve()
        lock_path.relative_to(base)
        if lock_path.exists():
            lock_obj = _load_json_bounded(lock_path)
            lock_owner = str(lock_obj.get("owner_request_id") or "").strip()
            if lock_owner == rid:
                lock_path.unlink()
                lock_released = True
    except Exception as e:
        payload.setdefault("promotion_report", {})["uat_lock_release_error"] = str(e)
    payload.setdefault("promotion_report", {})["uat_lock_owner_request_id"] = lock_owner
    payload.setdefault("promotion_report", {})["uat_lock_released"] = bool(lock_released)

    # Release UAT lock (if owned). No UAT auto-deploy: operator manually deploys/restarts UAT for a specific request.

    # Stop UAT after operator decision so UAT is short-lived (ports/services do not linger).
    if exec_mode == "uat" and lock_owner == rid:
        stop_report: Dict[str, Any] = {"at": now}
        if _process_manager_enabled():
            try:
                mgr = _require_process_manager()
                stopped: Dict[str, Any] = {}
                for pid in (
                    "abstractcode_web_uat",
                    "abstractobserver_uat",
                    "abstractflow_frontend_uat",
                    "abstractflow_backend_uat",
                    "gateway_uat",
                ):
                    try:
                        stopped[pid] = mgr.stop(pid)
                    except Exception as e:
                        stopped[pid] = {"status": "error", "error": str(e)}
                stop_report["processes"] = stopped
            except Exception as e:
                stop_report["error"] = str(e)
        else:
            stop_report["processes"] = {"status": "skipped", "reason": "process_manager_disabled"}
        payload.setdefault("promotion_report", {})["uat_stop"] = stop_report

    # Optional redeploy hook (best-effort).
    if bool(req.redeploy):
        try:
            if _process_manager_enabled():
                mgr = _require_process_manager()
                out = mgr.redeploy_gateway()
                payload.setdefault("promotion_report", {})["redeploy"] = out
            else:
                payload.setdefault("promotion_report", {})["redeploy"] = {"status": "skipped", "reason": "process_manager_disabled"}
        except Exception as e:
            payload.setdefault("promotion_report", {})["redeploy_error"] = str(e)

    try:
        _write_json_atomic(path, payload)
    except Exception as e:
        # Promotion already completed and we already wrote the promoted status once above; do not fail the request.
        payload.setdefault("promotion_report", {})["writeback_error"] = str(e)

    sanitized = dict(payload)
    sanitized.pop("prompt", None)
    return BacklogExecRequestDetailResponse(request_id=rid, payload=sanitized)


@router.post("/backlog/exec/requests/{request_id}/uat/deploy", response_model=BacklogExecRequestDetailResponse)
async def backlog_exec_request_deploy_uat(request_id: str) -> BacklogExecRequestDetailResponse:
    """Manually deploy a pending UAT request to the shared UAT stack (best-effort)."""
    repo_root = _triage_repo_root_from_env()
    if repo_root is None:
        raise HTTPException(status_code=404, detail="Backlog browsing not configured on this gateway")

    rid = _safe_backlog_exec_request_id(request_id)
    if rid is None:
        raise HTTPException(status_code=400, detail="Invalid request_id")

    base = _gateway_base_dir()
    qdir = (base / "backlog_exec_queue").resolve()
    path = (qdir / f"{rid}.json").resolve()
    try:
        path.relative_to(qdir)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Request not found")

    try:
        from ..maintenance.backlog_exec_runner import deploy_uat_for_request  # type: ignore

        out = deploy_uat_for_request(gateway_data_dir=base, repo_root=Path(repo_root).resolve(), request_id=rid)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to deploy to UAT: {e}")

    if not bool(out.get("ok") is True):
        err = str(out.get("error") or "deploy_failed")
        raise HTTPException(status_code=400, detail=f"Deploy failed: {err}")

    payload = _load_json_bounded(path)
    sanitized = dict(payload)
    sanitized.pop("prompt", None)
    return BacklogExecRequestDetailResponse(request_id=rid, payload=sanitized)


@router.get("/backlog/exec/requests/{request_id}/logs/tail", response_model=BacklogExecLogTailResponse)
async def backlog_exec_log_tail(
    request_id: str,
    name: str = Query(default="events", description="Log name: events|stderr|last_message"),
    max_bytes: int = Query(default=80_000, ge=1024, le=400_000, description="Tail size in bytes (bounded)."),
) -> BacklogExecLogTailResponse:
    repo_root = _triage_repo_root_from_env()
    if repo_root is None:
        raise HTTPException(status_code=404, detail="Backlog browsing not configured on this gateway")

    rid = _safe_backlog_exec_request_id(request_id)
    if rid is None:
        raise HTTPException(status_code=400, detail="Invalid request_id")

    nm = str(name or "").strip().lower()
    file_name = None
    if nm in {"events", "event", "jsonl"}:
        nm = "events"
        file_name = "codex_events.jsonl"
    elif nm in {"stderr", "err"}:
        nm = "stderr"
        file_name = "codex_stderr.log"
    elif nm in {"last_message", "last", "message"}:
        nm = "last_message"
        file_name = "codex_last_message.txt"
    else:
        raise HTTPException(status_code=400, detail="Invalid log name (events|stderr|last_message)")

    base = _gateway_base_dir()
    run_dir = (base / "backlog_exec_runs" / rid).resolve()
    try:
        run_dir.relative_to((base / "backlog_exec_runs").resolve())
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid run dir")

    path = (run_dir / file_name).resolve()
    try:
        path.relative_to(run_dir)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid log path")

    if not path.exists():
        return BacklogExecLogTailResponse(request_id=rid, name=nm, bytes=0, truncated=False, content="")

    data = b""
    truncated = False
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = int(f.tell() or 0)
            start = max(0, size - int(max_bytes))
            truncated = start > 0
            f.seek(start, os.SEEK_SET)
            data = f.read(int(max_bytes))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read log: {e}")

    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        text = ""
    return BacklogExecLogTailResponse(request_id=rid, name=nm, bytes=len(data), truncated=bool(truncated), content=text)


@router.get("/audit/tail", response_model=AuditLogTailResponse)
async def audit_log_tail(
    max_bytes: int = Query(default=80_000, ge=1024, le=400_000, description="Tail size in bytes (bounded)."),
) -> AuditLogTailResponse:
    base = _gateway_base_dir()
    path = (base / "audit_log.jsonl").resolve()
    try:
        path.relative_to(base)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid audit log path")

    if not path.exists():
        return AuditLogTailResponse(bytes=0, truncated=False, content="")

    data = b""
    truncated = False
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = int(f.tell() or 0)
            start = max(0, size - int(max_bytes))
            truncated = start > 0
            f.seek(start, os.SEEK_SET)
            data = f.read(int(max_bytes))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read audit log: {e}")

    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        text = ""
    return AuditLogTailResponse(bytes=len(data), truncated=bool(truncated), content=text)


def _process_manager_enabled() -> bool:
    return _env_bool("ABSTRACTGATEWAY_ENABLE_PROCESS_MANAGER", False)


def _require_process_manager():
    if not _process_manager_enabled():
        raise HTTPException(status_code=404, detail="Process manager is disabled (set ABSTRACTGATEWAY_ENABLE_PROCESS_MANAGER=1)")
    repo_root = _triage_repo_root_from_env()
    if repo_root is None:
        raise HTTPException(status_code=404, detail="Repo root is not configured (set ABSTRACTGATEWAY_TRIAGE_REPO_ROOT)")
    base_dir = _gateway_base_dir()
    try:
        from ..maintenance.process_manager import get_process_manager  # type: ignore
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Process manager unavailable: {e}")
    try:
        return get_process_manager(base_dir=base_dir, repo_root=repo_root)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to initialize process manager: {e}")


def _require_managed_env_var_manager():
    if not _process_manager_enabled():
        raise HTTPException(status_code=404, detail="Process manager is disabled (set ABSTRACTGATEWAY_ENABLE_PROCESS_MANAGER=1)")
    base_dir = _gateway_base_dir()
    try:
        from ..maintenance.process_manager import get_managed_env_var_manager  # type: ignore
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Managed env var manager unavailable: {e}")
    try:
        return get_managed_env_var_manager(base_dir=base_dir)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to initialize managed env var manager: {e}")


@router.get("/processes", response_model=ProcessListResponse)
async def processes_list() -> ProcessListResponse:
    if not _process_manager_enabled():
        return ProcessListResponse(enabled=False, processes=[])
    # Fresh installs (packaged gateway) may enable the process manager for env config
    # while not having a repo checkout. Process control is repo-root scoped and must
    # degrade gracefully (UI should show a hint, not a hard error).
    if _triage_repo_root_from_env() is None:
        return ProcessListResponse(enabled=False, processes=[])
    mgr = _require_process_manager()
    try:
        procs = mgr.list_processes()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list processes: {e}")
    return ProcessListResponse(enabled=True, processes=procs)


@router.get("/processes/env", response_model=ProcessEnvListResponse)
async def processes_env_list() -> ProcessEnvListResponse:
    if not _process_manager_enabled():
        return ProcessEnvListResponse(enabled=False, vars=[])

    mgr = _require_managed_env_var_manager()
    try:
        out = mgr.list_managed_env_vars()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list managed env vars: {e}")

    vars0 = out.get("vars") if isinstance(out, dict) else []
    if not isinstance(vars0, list):
        vars0 = []
    err = str(out.get("error") or "").strip() if isinstance(out, dict) else ""
    return ProcessEnvListResponse(enabled=True, error=(err or None), vars=vars0)


@router.post("/processes/env", response_model=ProcessEnvListResponse)
async def processes_env_update(req: ProcessEnvUpdateRequest) -> ProcessEnvListResponse:
    mgr = _require_managed_env_var_manager()
    try:
        out = mgr.update_managed_env_vars(set_vars=dict(req.set_vars or {}), unset=list(req.unset or []))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update managed env vars: {e}")

    vars0 = out.get("vars") if isinstance(out, dict) else []
    if not isinstance(vars0, list):
        vars0 = []
    err = str(out.get("error") or "").strip() if isinstance(out, dict) else ""
    return ProcessEnvListResponse(enabled=True, error=(err or None), vars=vars0)


@router.post("/processes/{process_id}/start", response_model=ProcessActionResponse)
async def processes_start(process_id: str) -> ProcessActionResponse:
    mgr = _require_process_manager()
    pid = str(process_id or "").strip()
    if not pid:
        raise HTTPException(status_code=400, detail="process_id is required")
    try:
        st = mgr.start(pid)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown process id")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to start process: {e}")
    return ProcessActionResponse(process_id=pid, state=st)


@router.post("/processes/{process_id}/stop", response_model=ProcessActionResponse)
async def processes_stop(process_id: str) -> ProcessActionResponse:
    mgr = _require_process_manager()
    pid = str(process_id or "").strip()
    if not pid:
        raise HTTPException(status_code=400, detail="process_id is required")
    try:
        st = mgr.stop(pid)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown process id")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to stop process: {e}")
    return ProcessActionResponse(process_id=pid, state=st)


@router.post("/processes/{process_id}/restart", response_model=ProcessActionResponse)
async def processes_restart(process_id: str) -> ProcessActionResponse:
    mgr = _require_process_manager()
    pid = str(process_id or "").strip()
    if not pid:
        raise HTTPException(status_code=400, detail="process_id is required")
    try:
        st = mgr.restart(pid)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown process id")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to restart process: {e}")
    return ProcessActionResponse(process_id=pid, state=st if isinstance(st, dict) else {"status": "ok"})


@router.post("/processes/{process_id}/redeploy", response_model=ProcessActionResponse)
async def processes_redeploy(process_id: str) -> ProcessActionResponse:
    mgr = _require_process_manager()
    pid = str(process_id or "").strip()
    if not pid:
        raise HTTPException(status_code=400, detail="process_id is required")
    if pid != "gateway":
        raise HTTPException(status_code=400, detail="redeploy is only supported for process_id=gateway in v0")
    try:
        st = mgr.redeploy_gateway()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to redeploy gateway: {e}")
    return ProcessActionResponse(process_id=pid, state=st if isinstance(st, dict) else {"status": "ok"})


@router.get("/processes/{process_id}/logs/tail", response_model=ProcessLogTailResponse)
async def processes_log_tail(
    process_id: str,
    max_bytes: int = Query(default=80_000, ge=1024, le=400_000, description="Tail size in bytes (bounded)."),
) -> ProcessLogTailResponse:
    mgr = _require_process_manager()
    pid = str(process_id or "").strip()
    if not pid:
        raise HTTPException(status_code=400, detail="process_id is required")
    try:
        out = mgr.log_tail(pid, max_bytes=int(max_bytes))
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown process id")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read process log: {e}")
    return ProcessLogTailResponse(
        process_id=pid,
        bytes=int(out.get("bytes") or 0),
        truncated=bool(out.get("truncated")),
        log_relpath=str(out.get("log_relpath") or "") or None,
        content=str(out.get("content") or ""),
    )


@router.get("/backlog/{kind}", response_model=BacklogListResponse)
async def backlog_list(kind: str) -> BacklogListResponse:
    repo_root = _triage_repo_root_from_env()
    if repo_root is None:
        raise HTTPException(status_code=404, detail="Backlog browsing not configured on this gateway")

    k = str(kind or "").strip().lower()
    if k not in {"planned", "completed", "proposed", "recurrent", "deprecated", "trash"}:
        raise HTTPException(status_code=400, detail="Invalid backlog kind (planned|completed|proposed|recurrent|deprecated|trash)")

    try:
        from ..maintenance.backlog_parser import iter_backlog_items  # type: ignore
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backlog parser unavailable: {e}")

    dir_path = repo_root / "docs" / "backlog" / k
    if not dir_path.exists():
        return BacklogListResponse(items=[])
    parsed_items = list(iter_backlog_items(dir_path, kind=k))
    parsed_items.sort(key=lambda i: int(getattr(i, "item_id", 0)), reverse=True)

    parsed_names = {item.path.name for item in parsed_items}
    raw_files = sorted(
        [p for p in dir_path.glob("*.md") if p.name.lower() not in {"readme.md"}],
        key=lambda p: p.name,
    )

    out: List[BacklogItemSummary] = []
    for item in parsed_items[:500]:
        out.append(
            BacklogItemSummary(
                kind=k,
                filename=item.path.name,
                item_id=int(item.item_id),
                package=str(item.package),
                title=str(item.title),
                task_type=str(getattr(item, "task_type", "task") or "task"),
                summary=str(item.summary or ""),
                parsed=True,
            )
        )

    # Best-effort: include unparsed items so the UI can still browse them.
    for p in raw_files:
        if p.name in parsed_names:
            continue
        out.append(
            BacklogItemSummary(
                kind=k,
                filename=p.name,
                item_id=0,
                package="",
                title=p.name,
                task_type="task",
                summary="",
                parsed=False,
            )
        )
        if len(out) >= 800:
            break
    return BacklogListResponse(items=out)


@router.get("/backlog/{kind}/{filename}/content", response_model=BacklogContentResponse)
async def backlog_content(kind: str, filename: str) -> BacklogContentResponse:
    repo_root = _triage_repo_root_from_env()
    if repo_root is None:
        raise HTTPException(status_code=404, detail="Backlog browsing not configured on this gateway")

    k = str(kind or "").strip().lower()
    if k not in {"planned", "completed", "proposed", "recurrent", "deprecated", "trash"}:
        raise HTTPException(status_code=400, detail="Invalid backlog kind (planned|completed|proposed|recurrent|deprecated|trash)")

    raw = str(filename or "").strip()
    if not raw or "/" in raw or "\\" in raw or not raw.lower().endswith(".md"):
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not re.fullmatch(r"[a-zA-Z0-9._-]{1,220}\.md", raw):
        raise HTTPException(status_code=400, detail="Invalid filename")

    dir_path = repo_root / "docs" / "backlog" / k
    path = (dir_path / raw).resolve()
    # Ensure path is inside the backlog dir.
    try:
        path.relative_to(dir_path.resolve())
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Backlog item not found")

    content = _read_text_bounded(path, max_chars=500_000)
    return BacklogContentResponse(kind=k, filename=raw, content=content)


_BACKLOG_KINDS = {"planned", "completed", "proposed", "recurrent", "deprecated", "trash"}
_BACKLOG_WRITE_KINDS = {"planned", "proposed", "recurrent", "completed", "deprecated", "trash"}
_BACKLOG_CREATE_KINDS = {"planned", "proposed", "recurrent"}
_BACKLOG_ASSIST_KINDS = {"planned", "proposed", "recurrent", "deprecated"}


def _safe_backlog_kind(value: str) -> Optional[str]:
    k = str(value or "").strip().lower()
    if not k or k not in _BACKLOG_KINDS:
        return None
    return k


def _safe_backlog_package(value: str) -> Optional[str]:
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    if not re.fullmatch(r"[a-z][a-z0-9_-]{1,60}", raw):
        return None
    return raw


_BACKLOG_TASK_TYPES = {"bug", "feature", "task"}
_BACKLOG_TITLE_TYPE_PREFIX_RE = re.compile(r"^\[(bug|feature|task)\]\s*", re.IGNORECASE)


def _safe_backlog_task_type(value: Optional[str]) -> Optional[str]:
    t = str(value or "").strip().lower()
    if not t:
        return None
    if t in _BACKLOG_TASK_TYPES:
        return t
    return None


def _strip_backlog_title_type_prefix(title: str) -> str:
    s = str(title or "").strip()
    s = _BACKLOG_TITLE_TYPE_PREFIX_RE.sub("", s).strip()
    return s or str(title or "").strip()


def _slug_kebab(value: str) -> str:
    s = str(value or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = s[:80].strip("-") or "item"
    return s


def _backlog_dir_for(repo_root: Path, kind: str) -> Path:
    return (repo_root / "docs" / "backlog" / kind).resolve()


def _safe_backlog_filename(value: str) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw or "/" in raw or "\\" in raw:
        return None
    if not raw.lower().endswith(".md"):
        return None
    if not re.fullmatch(r"[a-zA-Z0-9._-]{1,220}\.md", raw):
        return None
    return raw


def _safe_backlog_exec_request_id(value: str) -> Optional[str]:
    rid = str(value or "").strip().lower()
    if not rid:
        return None
    if not re.fullmatch(r"[a-f0-9]{8,64}", rid):
        return None
    return rid


def _sanitize_backlog_asset_filename(value: str) -> str:
    """Sanitize an attachment filename to a safe on-disk name (no path traversal)."""
    raw = str(value or "").strip()
    # Drop any path components (best-effort).
    name = Path(raw).name if raw else ""
    name = name.strip()
    if not name:
        return "attachment"
    # Replace unsafe characters with underscores.
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    name = re.sub(r"_+", "_", name)
    name = name.strip("._-") or "attachment"

    if len(name) > 200:
        stem, ext = os.path.splitext(name)
        ext = ext[:20]
        keep = max(1, 200 - len(ext))
        name = stem[:keep] + ext
        name = name.strip("._-") or "attachment"
    return name


def _read_backlog_template(repo_root: Path) -> str:
    path = (repo_root / "docs" / "backlog" / "template.md").resolve()
    if not path.exists():
        raise HTTPException(status_code=404, detail="Backlog template not found (docs/backlog/template.md)")
    return _read_text_bounded(path, max_chars=250_000)


def _render_backlog_markdown(
    *,
    template_md: str,
    item_id: int,
    package: str,
    title: str,
    task_type: str,
    summary: str,
    created_at: str,
    content_override: Optional[str] = None,
) -> str:
    pid = f"{int(item_id):03d}"
    tt = _safe_backlog_task_type(task_type) or "task"
    clean_title = _strip_backlog_title_type_prefix(title)
    header = f"# {pid}-{package}: [{tt.upper()}] {clean_title}".strip()

    base = str(content_override or "").strip() or str(template_md or "")
    out = base
    out = out.replace("{ID}", pid).replace("{Package}", package).replace("{Title}", clean_title).replace("{Type}", tt)

    # Force the first H1 to match our id/pkg/title (best-effort).
    lines = out.splitlines()
    replaced_h1 = False
    for i, raw in enumerate(lines):
        if raw.strip().startswith("# "):
            lines[i] = header
            replaced_h1 = True
            break
        if raw.strip():
            break
    if not replaced_h1:
        lines.insert(0, header)
        lines.insert(1, "")
    out = "\n".join(lines)

    # Ensure Created line is present and correct (best-effort).
    lines = out.splitlines()
    created_line = f"> Created: {created_at}".strip()
    found_created = False
    for i, raw in enumerate(lines[:40]):
        if raw.strip().lower().startswith("> created:"):
            lines[i] = created_line
            found_created = True
            break
    if not found_created:
        # Insert after header (and an optional blank line).
        insert_at = 1
        if len(lines) > 1 and not lines[1].strip():
            insert_at = 2
        lines.insert(insert_at, created_line)
        lines.insert(insert_at + 1, "")
    out = "\n".join(lines)

    # Ensure Type line is present and correct (best-effort).
    lines = out.splitlines()
    type_line = f"> Type: {tt}".strip()
    found_type = False
    for i, raw in enumerate(lines[:60]):
        if raw.strip().lower().startswith("> type:"):
            lines[i] = type_line
            found_type = True
            break
    if not found_type:
        created_idx = -1
        for i, raw in enumerate(lines[:60]):
            if raw.strip().lower().startswith("> created:"):
                created_idx = i
                break
        if created_idx >= 0:
            insert_at = created_idx + 1
            if insert_at < len(lines) and not lines[insert_at].strip():
                lines.insert(insert_at, type_line)
            else:
                lines.insert(insert_at, type_line)
                lines.insert(insert_at + 1, "")
        else:
            # Insert after header (and an optional blank line).
            insert_at = 1
            if len(lines) > 1 and not lines[1].strip():
                insert_at = 2
            lines.insert(insert_at, type_line)
            lines.insert(insert_at + 1, "")
    out = "\n".join(lines)

    if summary.strip():
        # Best-effort: replace placeholder summary line after "## Summary".
        lines = out.splitlines()
        for i, raw in enumerate(lines):
            if raw.strip().lower() == "## summary":
                # Find the next non-empty line.
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                placeholder = lines[j] if j < len(lines) else ""
                if placeholder.strip().lower().startswith("one paragraph describing"):
                    lines[j] = summary.strip()
                else:
                    lines.insert(i + 1, summary.strip())
                    lines.insert(i + 2, "")
                break
        out = "\n".join(lines)

    if not out.endswith("\n"):
        out += "\n"
    return out


def _resolve_backlog_refs(
    *,
    repo_root: Path,
    refs: List[BacklogRef],
    allowed_kinds: set[str],
    max_items: int = 25,
) -> List[Dict[str, Any]]:
    if len(refs) > max_items:
        raise HTTPException(status_code=413, detail=f"Too many backlog items ({len(refs)} > {max_items})")

    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for r in refs:
        k = _safe_backlog_kind(getattr(r, "kind", ""))
        if k is None or k not in allowed_kinds:
            raise HTTPException(status_code=400, detail="Invalid backlog kind for this operation")
        safe = _safe_backlog_filename(getattr(r, "filename", ""))
        if safe is None:
            raise HTTPException(status_code=400, detail="Invalid filename")

        dir_path = _backlog_dir_for(repo_root, k)
        path = (dir_path / safe).resolve()
        try:
            path.relative_to(dir_path)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid path")
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"Backlog item not found: {safe}")

        relpath = f"docs/backlog/{k}/{safe}"
        if relpath in seen:
            continue
        seen.add(relpath)
        out.append({"kind": k, "filename": safe, "relpath": relpath, "path": path})
    return out


_DEFAULT_BACKLOG_EXEC_PROMPT_PREFIX = (
    "please investigate docs/architecture.md and continue with the various package {package_name}/docs/architecture.md . "
    "also check our docs/adr/ to understand the main decisions taken for our abstract framework and you can check "
    "docs/backlog/completed/ to understand what was done. note however that the code itself in each package remains the ultimate "
    "source of truth. when you are given a task : either follow or create a docs/backlog/planned/ task item (use the same formalism "
    "for both the filename and content), implement it while thinking of the long term consequences of your choices to inform on the best "
    "possible trajectory to create clean, simple and efficient code. when the code is implemented, test the new features and if there are "
    "errors, engage in a loop of reasoning, fix, test until everything work following the A/B/C tests (docs/adr/0019-testing-strategy-and-levels.md). "
    "if the backlog item references attachments (typically under docs/backlog/assets/<id>/...), treat them as first-class inputs and open/read them. "
    "when all tests pass, then you can move the task to docs/backlog/completed/ and write your report at the end. Once you are familiar enough "
    "with our framework and its different package, i would like you to work on the following task."
)


def _generate_backlog_assist_json(
    *,
    provider: str,
    model: str,
    template_md: str,
    kind: str,
    package: str,
    title: str,
    summary: str,
    draft_md: str,
    messages: list[Dict[str, Any]],
) -> Dict[str, str]:
    """Generate a backlog-authoring assistant response (patchable in tests)."""
    from abstractruntime.integrations.abstractcore.llm_client import LocalAbstractCoreLLMClient

    system = (
        "You are AbstractFramework Backlog Assistant.\n"
        "You help a human author a single backlog item that is clear, testable, and aligned with the framework conventions.\n\n"
        "Rules:\n"
        "- Always return ONLY valid JSON (no markdown fences).\n"
        '- JSON schema: {"reply": string, "draft_markdown": string}.\n'
        "- reply: ask for missing info or confirm decisions.\n"
        "- draft_markdown: when you have enough info, output a full backlog item markdown following BACKLOG_TEMPLATE.\n"
        "- Keep dependencies minimal; prefer permissive licensed deps (MIT/Apache/BSD).\n"
        "- Include ADR-0019 testing commands in the draft.\n"
        "- Do not include secrets.\n"
    )

    intro = json.dumps(
        {
            "kind": kind,
            "package": package,
            "title": title,
            "summary": summary,
            "backlog_template": _clamp_text(template_md, max_len=120_000),
            "current_draft_markdown": _clamp_text(draft_md, max_len=120_000),
        },
        ensure_ascii=False,
        indent=2,
    )

    prompt_msgs: list[Dict[str, str]] = [{"role": "user", "content": "CONTEXT:\n" + _clamp_text(intro, max_len=180_000)}]
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
    res = llm.generate(prompt="", messages=prompt_msgs, system_prompt=system, params={"temperature": 0.2})
    raw = str(res.get("content") or "").strip()
    try:
        obj = json.loads(raw)
    except Exception:
        return {"reply": raw, "draft_markdown": ""}
    if not isinstance(obj, dict):
        return {"reply": raw, "draft_markdown": ""}
    reply = str(obj.get("reply") or "").strip() or raw
    draft = str(obj.get("draft_markdown") or "").strip()
    if len(draft) > 400_000:
        draft = draft[:400_000] + "\n…(truncated)…\n"
    return {"reply": reply, "draft_markdown": draft}


@router.post("/backlog/move", response_model=BacklogMoveResponse)
async def backlog_move(req: BacklogMoveRequest) -> BacklogMoveResponse:
    repo_root = _triage_repo_root_from_env()
    if repo_root is None:
        raise HTTPException(status_code=404, detail="Backlog browsing not configured on this gateway")

    from_kind = _safe_backlog_kind(req.from_kind)
    to_kind = _safe_backlog_kind(req.to_kind)
    if from_kind is None or to_kind is None:
        raise HTTPException(status_code=400, detail="Invalid backlog kind")
    if from_kind not in _BACKLOG_WRITE_KINDS or to_kind not in _BACKLOG_WRITE_KINDS:
        raise HTTPException(status_code=400, detail="Invalid backlog kind")

    filename = _safe_backlog_filename(req.filename)
    if filename is None:
        raise HTTPException(status_code=400, detail="Invalid filename")

    from_dir = _backlog_dir_for(repo_root, from_kind)
    to_dir = _backlog_dir_for(repo_root, to_kind)
    from_path = (from_dir / filename).resolve()
    to_path = (to_dir / filename).resolve()
    try:
        from_path.relative_to(from_dir)
        to_path.relative_to(to_dir)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not from_path.exists():
        raise HTTPException(status_code=404, detail="Backlog item not found")
    to_dir.mkdir(parents=True, exist_ok=True)
    if to_path.exists():
        raise HTTPException(status_code=409, detail="Destination file already exists")
    try:
        from_path.rename(to_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to move backlog item: {e}")

    return BacklogMoveResponse(
        from_kind=from_kind,
        to_kind=to_kind,
        filename=filename,
        from_relpath=f"docs/backlog/{from_kind}/{filename}",
        to_relpath=f"docs/backlog/{to_kind}/{filename}",
    )


@router.post("/backlog/{kind}/{filename}/update", response_model=BacklogUpdateResponse)
async def backlog_update(kind: str, filename: str, req: BacklogUpdateRequest) -> BacklogUpdateResponse:
    repo_root = _triage_repo_root_from_env()
    if repo_root is None:
        raise HTTPException(status_code=404, detail="Backlog browsing not configured on this gateway")

    k = _safe_backlog_kind(kind)
    if k is None or k not in _BACKLOG_WRITE_KINDS:
        raise HTTPException(status_code=400, detail="Invalid backlog kind")

    safe_name = _safe_backlog_filename(filename)
    if safe_name is None:
        raise HTTPException(status_code=400, detail="Invalid filename")

    dir_path = _backlog_dir_for(repo_root, k)
    path = (dir_path / safe_name).resolve()
    try:
        path.relative_to(dir_path)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Backlog item not found")

    current = _read_text_bounded(path, max_chars=1_200_000)
    if req.expected_sha256:
        want = str(req.expected_sha256 or "").strip().lower()
        got = _sha256_hex_text(current)
        if want and got and want != got:
            raise HTTPException(status_code=409, detail="Backlog item changed (sha mismatch)")

    new_content = str(req.content or "")
    if len(new_content) > 1_200_000:
        raise HTTPException(status_code=400, detail="Content too large")
    if not new_content.endswith("\n"):
        new_content += "\n"
    try:
        path.write_text(new_content, encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write backlog item: {e}")

    sha = _sha256_hex_text(new_content)
    return BacklogUpdateResponse(kind=k, filename=safe_name, sha256=sha, bytes_written=len(new_content.encode("utf-8", errors="ignore")))


@router.post("/backlog/{kind}/{filename}/attachments/upload", response_model=BacklogAttachmentUploadResponse)
async def backlog_upload_attachment(
    kind: str,
    filename: str,
    file: UploadFile = File(..., description="Attachment (e.g. screenshot, diagram)."),
    overwrite: bool = Form(False, description="If true, overwrite an existing attachment with the same name."),
) -> BacklogAttachmentUploadResponse:
    repo_root = _triage_repo_root_from_env()
    if repo_root is None:
        raise HTTPException(status_code=404, detail="Backlog browsing not configured on this gateway")

    k = _safe_backlog_kind(kind)
    if k is None or k not in _BACKLOG_WRITE_KINDS:
        raise HTTPException(status_code=400, detail="Invalid backlog kind")

    safe = _safe_backlog_filename(filename)
    if safe is None:
        raise HTTPException(status_code=400, detail="Invalid filename")

    dir_path = _backlog_dir_for(repo_root, k)
    backlog_path = (dir_path / safe).resolve()
    try:
        backlog_path.relative_to(dir_path)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not backlog_path.exists():
        raise HTTPException(status_code=404, detail="Backlog item not found")

    m = re.match(r"^(\d{3})-", safe)
    if not m:
        raise HTTPException(status_code=400, detail="Backlog filename must start with '<id>-' to attach files")
    pid = m.group(1)
    item_id = int(pid)

    try:
        max_bytes_raw = str(os.getenv("ABSTRACTGATEWAY_MAX_BACKLOG_ATTACHMENT_BYTES", "") or "").strip()
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

    base_name = _sanitize_backlog_asset_filename(getattr(file, "filename", "") or "attachment")

    assets_root = (repo_root / "docs" / "backlog" / "assets").resolve()
    assets_dir = (assets_root / pid).resolve()
    try:
        assets_dir.relative_to(assets_root)
    except Exception:
        raise HTTPException(status_code=500, detail="Invalid assets dir")
    assets_dir.mkdir(parents=True, exist_ok=True)

    dest = (assets_dir / base_name).resolve()
    try:
        dest.relative_to(assets_dir)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid attachment path")

    if dest.exists() and not overwrite:
        stem, ext = os.path.splitext(base_name)
        for i in range(2, 2000):
            candidate = (assets_dir / f"{stem}-{i}{ext}").resolve()
            try:
                candidate.relative_to(assets_dir)
            except Exception:
                continue
            if not candidate.exists():
                dest = candidate
                base_name = candidate.name
                break
        if dest.exists():
            raise HTTPException(status_code=409, detail="Attachment filename collision")

    try:
        mode = "wb" if overwrite else "xb"
        with open(dest, mode) as f:
            f.write(content or b"")
    except FileExistsError:
        raise HTTPException(status_code=409, detail="Attachment already exists")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write attachment: {e}")

    sha = hashlib.sha256(content or b"").hexdigest()
    relpath = f"docs/backlog/assets/{pid}/{base_name}"
    return BacklogAttachmentUploadResponse(
        kind=k,
        filename=safe,
        item_id=item_id,
        stored=BacklogAttachmentStored(filename=base_name, relpath=relpath, bytes=size, sha256=sha),
    )


@router.post("/backlog/create", response_model=BacklogCreateResponse)
async def backlog_create(req: BacklogCreateRequest) -> BacklogCreateResponse:
    repo_root = _triage_repo_root_from_env()
    if repo_root is None:
        raise HTTPException(status_code=404, detail="Backlog browsing not configured on this gateway")

    k = _safe_backlog_kind(req.kind)
    if k is None or k not in _BACKLOG_CREATE_KINDS:
        raise HTTPException(status_code=400, detail="Invalid backlog kind for create (planned|proposed|recurrent)")

    pkg = _safe_backlog_package(req.package)
    if pkg is None:
        raise HTTPException(status_code=400, detail="Invalid package")
    title = str(req.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")
    task_type = _safe_backlog_task_type(req.task_type)
    if task_type is None:
        m = _BACKLOG_TITLE_TYPE_PREFIX_RE.match(title)
        if m:
            task_type = _safe_backlog_task_type(m.group(1))
    task_type = task_type or "task"
    summary = str(req.summary or "").strip()

    template_md = _read_backlog_template(repo_root)

    backlog_root = (repo_root / "docs" / "backlog").resolve()
    dir_path = (backlog_root / k).resolve()
    dir_path.mkdir(parents=True, exist_ok=True)

    try:
        from ..maintenance.draft_generator import BacklogIdAllocator  # type: ignore
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backlog id allocator unavailable: {e}")

    allocator = BacklogIdAllocator.from_backlog_root(backlog_root)
    created_at = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")

    last_err: Optional[str] = None
    for _ in range(0, 200):
        item_id = allocator.allocate()
        slug = _slug_kebab(title)
        filename = f"{item_id:03d}-{pkg}-{slug}.md"
        safe = _safe_backlog_filename(filename)
        if safe is None:
            last_err = "Failed to generate a safe filename"
            continue
        path = (dir_path / safe).resolve()
        try:
            path.relative_to(dir_path)
        except Exception:
            last_err = "Invalid path"
            continue
        if path.exists():
            last_err = "Filename collision"
            continue

        md = _render_backlog_markdown(
            template_md=template_md,
            item_id=item_id,
            package=pkg,
            title=title,
            task_type=task_type,
            summary=summary,
            created_at=created_at,
            content_override=req.content,
        )
        try:
            with open(path, "x", encoding="utf-8") as f:
                f.write(md)
        except FileExistsError:
            last_err = "Filename collision"
            continue
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to create backlog item: {e}")

        sha = _sha256_hex_text(md)
        return BacklogCreateResponse(
            kind=k,
            filename=safe,
            relpath=f"docs/backlog/{k}/{safe}",
            item_id=int(item_id),
            sha256=sha,
        )

    raise HTTPException(status_code=409, detail=last_err or "Could not allocate a unique backlog filename")


@router.post("/backlog/merge", response_model=BacklogMergeResponse)
async def backlog_merge(req: BacklogMergeRequest) -> BacklogMergeResponse:
    repo_root = _triage_repo_root_from_env()
    if repo_root is None:
        raise HTTPException(status_code=404, detail="Backlog browsing not configured on this gateway")

    k = _safe_backlog_kind(req.kind)
    if k is None or k not in _BACKLOG_CREATE_KINDS:
        raise HTTPException(status_code=400, detail="Invalid backlog kind for merge (planned|proposed|recurrent)")

    pkg = _safe_backlog_package(req.package)
    if pkg is None:
        raise HTTPException(status_code=400, detail="Invalid package")
    title = str(req.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")

    task_type = _safe_backlog_task_type(req.task_type)
    if task_type is None:
        m = _BACKLOG_TITLE_TYPE_PREFIX_RE.match(title)
        if m:
            task_type = _safe_backlog_task_type(m.group(1))
    task_type = task_type or "task"

    resolved = _resolve_backlog_refs(repo_root=repo_root, refs=list(req.items or []), allowed_kinds={"planned"})
    if len(resolved) < 2:
        raise HTTPException(status_code=400, detail="Merge requires at least 2 planned backlog items")

    merged_relpaths = [str(r.get("relpath") or "") for r in resolved if str(r.get("relpath") or "").strip()]

    default_summary = f"Execute {len(merged_relpaths)} planned backlog items sequentially in one agent run to reuse context."
    final_summary = str(req.summary or "").strip() or default_summary

    template_md = _read_backlog_template(repo_root)
    created_at = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")

    md_lines: List[str] = []
    md_lines.append(f"# {{ID}}-{{Package}}: [{task_type.upper()}] {{Title}}")
    md_lines.append("")
    md_lines.append(f"> Created: {created_at}")
    md_lines.append(f"> Type: {task_type}")
    md_lines.append("")
    md_lines.append("## Summary")
    md_lines.append(final_summary)
    md_lines.append("")
    md_lines.append("## Diagram")
    md_lines.append("```")
    md_lines.append("planned items (A, B, …) -> master backlog -> single exec worker (one Codex run)")
    md_lines.append("```")
    md_lines.append("")
    md_lines.append("## Context")
    md_lines.append(
        "Creating the proper context (architecture + ADRs + code scanning) takes time. This master backlog groups several planned items so a single agent run can execute them sequentially while keeping a growing context."
    )
    md_lines.append("")
    md_lines.append("## Scope")
    md_lines.append("### Included")
    md_lines.append("- Execute the referenced backlog items sequentially (in order).")
    md_lines.append("- For each referenced item: implement, test per ADR-0019, and move it to `docs/backlog/completed/` with a report.")
    md_lines.append("")
    md_lines.append("### Excluded")
    md_lines.append("- Rewriting the referenced backlog items as part of the merge (beyond normal edits required during implementation).")
    md_lines.append("")
    md_lines.append("## Implementation Plan")
    md_lines.append(
        "1. For each referenced backlog item (in order): read it, implement it, run tests per ADR-0019, fix until green, then move it to completed with a report."
    )
    md_lines.append("2. After all items are complete, re-run the relevant test suites to ensure nothing regressed.")
    md_lines.append("3. Move this master backlog item to completed with a short summary + the commands used.")
    md_lines.append("")
    md_lines.append("## Dependencies")
    md_lines.append("- Referenced backlog items:")
    for rel in merged_relpaths:
        md_lines.append(f"  - `{rel}`")
    md_lines.append("")
    md_lines.append("## Acceptance Criteria")
    md_lines.append("- [ ] All referenced backlog items are completed (moved to `docs/backlog/completed/` with reports).")
    md_lines.append("- [ ] Tests pass per ADR-0019 and commands are recorded in reports.")
    md_lines.append("- [ ] This master backlog is moved to completed with a report.")
    md_lines.append("")
    md_lines.append("## Testing (ADR-0019)")
    md_lines.append("- Level A:")
    md_lines.append("  - `...`")
    md_lines.append("- Level B:")
    md_lines.append("  - `...`")
    md_lines.append("- Level C (optional / opt-in):")
    md_lines.append("  - `...`")
    md_lines.append("")
    md_lines.append("## Related")
    md_lines.append("- ADRs:")
    md_lines.append("  - `docs/adr/0019-testing-strategy-and-levels.md`")
    md_lines.append("- Generated from:")
    for rel in merged_relpaths:
        md_lines.append(f"  - `{rel}`")
    md_lines.append("")
    md_lines.append("---")
    md_lines.append("## Report (added when completed)")
    md_lines.append("")
    md_lines.append("> Completed: YYYY-MM-DD")
    md_lines.append("")
    md_lines.append("### What changed")
    md_lines.append("- ")
    md_lines.append("")
    md_lines.append("### Security / hardening")
    md_lines.append("- ")
    md_lines.append("")
    md_lines.append("### Testing (ADR-0019)")
    md_lines.append("Levels executed:")
    md_lines.append("- A:")
    md_lines.append("- B:")
    md_lines.append("- C (if any):")
    md_lines.append("")
    md_lines.append("Commands run:")
    md_lines.append("- `...`")
    md_lines.append("")
    md_lines.append("### Follow-ups")
    md_lines.append("- ")
    md_lines.append("")
    merged_md = "\n".join(md_lines)

    backlog_root = (repo_root / "docs" / "backlog").resolve()
    dir_path = (backlog_root / k).resolve()
    dir_path.mkdir(parents=True, exist_ok=True)

    try:
        from ..maintenance.draft_generator import BacklogIdAllocator  # type: ignore
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backlog id allocator unavailable: {e}")

    allocator = BacklogIdAllocator.from_backlog_root(backlog_root)
    last_err: Optional[str] = None
    for _ in range(0, 200):
        item_id = allocator.allocate()
        slug = _slug_kebab(title)
        filename = f"{item_id:03d}-{pkg}-{slug}.md"
        safe = _safe_backlog_filename(filename)
        if safe is None:
            last_err = "Failed to generate a safe filename"
            continue
        path = (dir_path / safe).resolve()
        try:
            path.relative_to(dir_path)
        except Exception:
            last_err = "Invalid path"
            continue
        if path.exists():
            last_err = "Filename collision"
            continue

        md = _render_backlog_markdown(
            template_md=template_md,
            item_id=item_id,
            package=pkg,
            title=title,
            task_type=task_type,
            summary=final_summary,
            created_at=created_at,
            content_override=merged_md,
        )
        try:
            with open(path, "x", encoding="utf-8") as f:
                f.write(md)
        except FileExistsError:
            last_err = "Filename collision"
            continue
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to create master backlog item: {e}")

        sha = _sha256_hex_text(md)
        return BacklogMergeResponse(
            kind=k,
            filename=safe,
            relpath=f"docs/backlog/{k}/{safe}",
            item_id=int(item_id),
            sha256=sha,
            merged_relpaths=merged_relpaths,
        )

    raise HTTPException(status_code=409, detail=last_err or "Could not allocate a unique backlog filename")


@router.post("/backlog/{kind}/{filename}/execute", response_model=BacklogExecuteResponse)
async def backlog_execute(
    kind: str,
    filename: str,
    execution_mode: Optional[str] = Query(default=None, description="Execution mode override: uat|inplace."),
) -> BacklogExecuteResponse:
    mode_override = str(execution_mode or "").strip().lower()
    repo_root = _triage_repo_root_from_env()
    if repo_root is None:
        raise HTTPException(status_code=404, detail="Backlog browsing not configured on this gateway")

    k = _safe_backlog_kind(kind)
    if k is None:
        raise HTTPException(status_code=400, detail="Invalid backlog kind")
    safe = _safe_backlog_filename(filename)
    if safe is None:
        raise HTTPException(status_code=400, detail="Invalid filename")

    dir_path = _backlog_dir_for(repo_root, k)
    path = (dir_path / safe).resolve()
    try:
        path.relative_to(dir_path)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Backlog item not found")

    relpath = f"docs/backlog/{k}/{safe}"
    base = _gateway_base_dir()
    qdir = (base / "backlog_exec_queue").resolve()
    qdir.mkdir(parents=True, exist_ok=True)

    active = _backlog_exec_active_items_from_queue(qdir=qdir, wanted_statuses={"queued", "running", "awaiting_qa"}, limit_items=3000)
    existing = next((it for it in active if str(it.get("relpath") or "").strip() == relpath), None)
    if isinstance(existing, dict):
        rid = str(existing.get("request_id") or "").strip()
        st = str(existing.get("status") or "").strip() or "queued"
        if rid:
            raise HTTPException(status_code=409, detail=f"Backlog item is already {st} (request_id={rid})")
        raise HTTPException(status_code=409, detail=f"Backlog item is already {st}")

    content = _read_text_bounded(path, max_chars=500_000)
    prompt = _DEFAULT_BACKLOG_EXEC_PROMPT_PREFIX + "\n\nBacklog item:\n\n" + content

    request_id = uuid.uuid4().hex[:16]
    qpath = (qdir / f"{request_id}.json").resolve()
    try:
        qpath.relative_to(qdir)
    except Exception:
        raise HTTPException(status_code=500, detail="Invalid queue path")

    target_agent = "codex:gpt-5.2"
    target_model: Optional[str] = None
    target_reasoning_effort: Optional[str] = None
    execution_mode = "uat"
    try:
        from ..maintenance.backlog_exec_runner import BacklogExecRunnerConfig, normalize_codex_model_id  # type: ignore

        cfg = BacklogExecRunnerConfig.from_env()
        ex = str(getattr(cfg, "executor", "") or "").strip().lower()
        if ex in {"codex", "codex_cli", "codex-cli"}:
            target_model = normalize_codex_model_id(getattr(cfg, "codex_model", "gpt-5.2"))
            target_reasoning_effort = str(getattr(cfg, "codex_reasoning_effort", "") or "").strip() or None
            target_agent = f"codex:{target_model}"
        execution_mode = str(getattr(cfg, "exec_mode_default", "") or "").strip().lower() or "uat"
    except Exception:
        pass

    if mode_override:
        if mode_override in {"uat", "candidate"}:
            execution_mode = "uat"
        elif mode_override == "inplace":
            execution_mode = "inplace"
        else:
            raise HTTPException(status_code=400, detail="Invalid execution_mode (expected uat|inplace)")

    payload = {
        "created_at": datetime.datetime.now().astimezone().isoformat(),
        "request_id": request_id,
        "status": "queued",
        "execution_mode": execution_mode,
        "backlog": {"kind": k, "filename": safe, "relpath": relpath},
        "target_agent": target_agent,
        "target_model": target_model,
        "target_reasoning_effort": target_reasoning_effort,
        "prompt": prompt,
    }
    try:
        with open(qpath, "x", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    except FileExistsError:
        # Extremely unlikely; retry once.
        request_id = uuid.uuid4().hex[:16]
        qpath = (qdir / f"{request_id}.json").resolve()
        with open(qpath, "x", encoding="utf-8") as f:
            payload["request_id"] = request_id
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to queue execution request: {e}")

    return BacklogExecuteResponse(ok=True, request_id=request_id, request_relpath=f"backlog_exec_queue/{request_id}.json", prompt=prompt)


@router.post("/backlog/execute_batch", response_model=BacklogExecuteResponse)
async def backlog_execute_batch(req: BacklogExecuteBatchRequest) -> BacklogExecuteResponse:
    repo_root = _triage_repo_root_from_env()
    if repo_root is None:
        raise HTTPException(status_code=404, detail="Backlog browsing not configured on this gateway")

    resolved = _resolve_backlog_refs(repo_root=repo_root, refs=list(req.items or []), allowed_kinds={"planned"})
    if len(resolved) < 2:
        raise HTTPException(status_code=400, detail="Batch execute requires at least 2 planned backlog items")

    items = [{"kind": r["kind"], "filename": r["filename"], "relpath": r["relpath"]} for r in resolved]
    relpaths = [str(r["relpath"]) for r in resolved]

    base = _gateway_base_dir()
    qdir = (base / "backlog_exec_queue").resolve()
    qdir.mkdir(parents=True, exist_ok=True)

    active = _backlog_exec_active_items_from_queue(qdir=qdir, wanted_statuses={"queued", "running", "awaiting_qa"}, limit_items=8000)
    active_rel = {str(it.get("relpath") or "").strip() for it in active if isinstance(it, dict) and str(it.get("relpath") or "").strip()}
    overlapping = [rel for rel in relpaths if rel in active_rel]
    if overlapping:
        joined = ", ".join(overlapping[:5])
        more = f" (+{len(overlapping) - 5} more)" if len(overlapping) > 5 else ""
        raise HTTPException(status_code=409, detail=f"One or more backlog items are already queued/running: {joined}{more}")

    prompt_parts: List[str] = []
    prompt_parts.append(_DEFAULT_BACKLOG_EXEC_PROMPT_PREFIX)
    prompt_parts.append("")
    prompt_parts.append("Batch execution request:")
    prompt_parts.append("- Execute the following backlog items sequentially (in order) **in a single agent run**.")
    prompt_parts.append("- Keep a single growing context (do not restart between items).")
    prompt_parts.append("- For each item: follow its instructions, run tests per ADR-0019, and move it to `docs/backlog/completed/` with a report.")
    prompt_parts.append("")
    prompt_parts.append("Backlog queue:")
    for i, rel in enumerate(relpaths, start=1):
        prompt_parts.append(f"{i}. {rel}")
    prompt_parts.append("")
    prompt_parts.append("---")

    max_total_chars = 1_800_000
    total = sum(len(p) + 1 for p in prompt_parts)
    for i, r in enumerate(resolved, start=1):
        if total >= max_total_chars:
            prompt_parts.append("")
            prompt_parts.append("…(truncated: too many backlog items/contents)…")
            break
        prompt_parts.append("")
        prompt_parts.append(f"## Backlog item {i}: {r['relpath']}")
        prompt_parts.append("")
        content = _read_text_bounded(Path(r["path"]), max_chars=500_000)
        remaining = max_total_chars - total
        if remaining <= 0:
            prompt_parts.append("…(truncated)…")
            break
        if len(content) > remaining:
            content = content[:remaining] + "\n…(truncated)…\n"
        prompt_parts.append(content)
        total += len(content) + 2

    prompt = "\n".join(prompt_parts).strip() + "\n"
    request_id = uuid.uuid4().hex[:16]
    qpath = (qdir / f"{request_id}.json").resolve()
    try:
        qpath.relative_to(qdir)
    except Exception:
        raise HTTPException(status_code=500, detail="Invalid queue path")

    target_agent = "codex:gpt-5.2"
    target_model: Optional[str] = None
    target_reasoning_effort: Optional[str] = None
    execution_mode = "uat"
    try:
        from ..maintenance.backlog_exec_runner import BacklogExecRunnerConfig, normalize_codex_model_id  # type: ignore

        cfg = BacklogExecRunnerConfig.from_env()
        ex = str(getattr(cfg, "executor", "") or "").strip().lower()
        if ex in {"codex", "codex_cli", "codex-cli"}:
            target_model = normalize_codex_model_id(getattr(cfg, "codex_model", "gpt-5.2"))
            target_reasoning_effort = str(getattr(cfg, "codex_reasoning_effort", "") or "").strip() or None
            target_agent = f"codex:{target_model}"
        execution_mode = str(getattr(cfg, "exec_mode_default", "") or "").strip().lower() or "uat"
    except Exception:
        pass

    override = str(getattr(req, "execution_mode", None) or "").strip().lower()
    if override:
        if override in {"uat", "candidate"}:
            execution_mode = "uat"
        elif override == "inplace":
            execution_mode = "inplace"
        else:
            raise HTTPException(status_code=400, detail="Invalid execution_mode (expected uat|inplace)")

    payload = {
        "created_at": datetime.datetime.now().astimezone().isoformat(),
        "request_id": request_id,
        "status": "queued",
        "execution_mode": execution_mode,
        # Synthetic label: avoid copying a fake backlog path; the real list is in backlog_queue.
        "backlog": {"kind": "planned", "filename": f"batch({len(items)})", "relpath": ""},
        "backlog_queue": {"mode": "sequential", "items": items},
        "target_agent": target_agent,
        "target_model": target_model,
        "target_reasoning_effort": target_reasoning_effort,
        "prompt": prompt,
    }
    try:
        with open(qpath, "x", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    except FileExistsError:
        # Extremely unlikely; retry once.
        request_id = uuid.uuid4().hex[:16]
        qpath = (qdir / f"{request_id}.json").resolve()
        with open(qpath, "x", encoding="utf-8") as f:
            payload["request_id"] = request_id
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to queue execution request: {e}")

    return BacklogExecuteResponse(ok=True, request_id=request_id, request_relpath=f"backlog_exec_queue/{request_id}.json", prompt=prompt)


@router.post("/backlog/assist", response_model=BacklogAssistResponse)
async def backlog_assist(req: BacklogAssistRequest) -> BacklogAssistResponse:
    repo_root = _triage_repo_root_from_env()
    if repo_root is None:
        raise HTTPException(status_code=404, detail="Backlog browsing not configured on this gateway")

    k = _safe_backlog_kind(req.kind)
    if k is None or k not in _BACKLOG_ASSIST_KINDS:
        raise HTTPException(status_code=400, detail="Invalid backlog kind for assist (planned|proposed|recurrent|deprecated)")

    pkg = _safe_backlog_package(req.package)
    if pkg is None:
        raise HTTPException(status_code=400, detail="Invalid package")
    title = str(req.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")

    summary = str(req.summary or "").strip()
    draft_md = str(req.draft_markdown or "")
    if len(draft_md) > 500_000:
        draft_md = draft_md[:500_000] + "\n…(truncated)…\n"

    template_md = _read_backlog_template(repo_root)

    provider, model = _resolve_gateway_provider_model_or_400(
        provider=req.provider,
        model=req.model,
        purpose="backlog assist",
    )

    try:
        out = await asyncio.to_thread(
            _generate_backlog_assist_json,
            provider=provider,
            model=model,
            template_md=template_md,
            kind=k,
            package=pkg,
            title=title,
            summary=summary,
            draft_md=draft_md,
            messages=req.messages or [],
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Backlog assist failed: {e}")

    reply = str(out.get("reply") or "").strip()
    draft = str(out.get("draft_markdown") or "").strip()
    return BacklogAssistResponse(ok=True, reply=reply, draft_markdown=draft)


def _build_backlog_maintain_agent_prompt(
    *,
    repo_root: Path,
    kind: str,
    filename: str,
    package: str,
    title: str,
    summary: str,
    template_md: str,
    draft_md: str,
    messages: list[Dict[str, Any]],
) -> str:
    ctx = {
        "repo_root": str(repo_root),
        "backlog": {
            "kind": kind,
            "filename": filename,
            "relpath": f"docs/backlog/{kind}/{filename}",
            "template_relpath": "docs/backlog/template.md",
        },
        "package": package,
        "title": title,
        "summary": summary,
        "backlog_template": _clamp_text(template_md, max_len=80_000),
        "current_draft_markdown": _clamp_text(draft_md, max_len=220_000),
        "messages": [
            {"role": str(m.get("role") or ""), "content": _clamp_text(str(m.get("content") or ""), max_len=8_000)}
            for m in (messages or [])
            if isinstance(m, dict)
            and str(m.get("role") or "").strip().lower() in {"user", "assistant"}
            and isinstance(m.get("content"), str)
            and str(m.get("content") or "").strip()
        ],
    }
    return (
        "You are maintaining a single backlog markdown item in AbstractFramework.\n\n"
        "You have tool access (read/search web + repository). Use tools sparingly and only when needed.\n"
        "Do NOT write files or run commands.\n\n"
        "Goals:\n"
        "- Keep the backlog item clear, testable, and aligned with repo conventions.\n"
        "- Preserve the item id/filename semantics.\n"
        "- Keep security best practices (no secrets in markdown).\n"
        "- If attachments are referenced, keep them under the '## Related' -> 'Attachments' list.\n\n"
        "CONTEXT (JSON):\n"
        + json.dumps(ctx, ensure_ascii=False, indent=2)
        + "\n\n"
        "Return ONLY a JSON object matching the response schema with keys: reply, draft_markdown.\n"
    )


def _parse_backlog_maintain_reply(raw: str) -> tuple[str, str]:
    text = str(raw or "").strip()
    if not text:
        return ("", "")
    try:
        obj = json.loads(text)
    except Exception:
        return (text, "")
    if not isinstance(obj, dict):
        return (text, "")
    reply = str(obj.get("reply") or "").strip()
    draft = str(obj.get("draft_markdown") or obj.get("draftMarkdown") or "").strip()
    return (reply or text, draft)


def _build_backlog_advisor_agent_prompt(
    *,
    repo_root: Path,
    gateway_data_dir: Optional[Path],
    workspace_allowed_paths: Optional[list[str]],
    focus_kind: Optional[str],
    focus_type: Optional[str],
    web_tools_enabled: bool,
    messages: list[Dict[str, Any]],
) -> str:
    ctx: Dict[str, Any] = {
        "repo_root": str(repo_root),
        "gateway_data_dir": str(gateway_data_dir) if isinstance(gateway_data_dir, Path) else None,
        "backlog_root": "docs/backlog",
        "focus_kind": str(focus_kind or "").strip() or None,
        "focus_type": str(focus_type or "").strip() or None,
        "web_tools_enabled": bool(web_tools_enabled),
        "workspace_allowed_paths": list(workspace_allowed_paths or [])[:32],
        "notes": (
            "You are a read-only advisor. Use tools to inspect docs/backlog, relevant code (packages/*), and logs (gateway_data_dir) "
            "to connect/prioritize items and validate feasibility. Cite backlog items as repo-relative paths like docs/backlog/<kind>/<filename>."
        ),
        "messages": [],
    }

    # Bound message history for safety.
    out_msgs: list[Dict[str, str]] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "").strip().lower()
        if role not in {"user", "assistant", "system"}:
            continue
        content = m.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        out_msgs.append({"role": role, "content": _clamp_text(content.strip(), max_len=12_000)})
        if len(out_msgs) >= 40:
            break
    ctx["messages"] = out_msgs

    return (
        "You are AbstractFramework Backlog Advisor (read-only).\n\n"
        "You help a human review the entire repo backlog (docs/backlog/): search it, connect related items, find duplicates, "
        "and propose a prioritized next set of actions.\n\n"
        "Rules:\n"
        "- Read-only: do NOT write files and do NOT run shell commands.\n"
        "- Use tools sparingly. Prefer skim/search over reading many full files.\n"
        "- Do not claim you listed/read/searched something unless you actually did it with tools.\n"
        "- You may inspect source code under the repo root to validate recommendations.\n"
        "- You may inspect gateway logs/runtime artifacts only within the allowed workspace roots.\n"
        "- When recommending actions, reference backlog items using repo-relative paths.\n"
        "- Ask clarifying questions if needed.\n"
        "- Never include secrets.\n\n"
        "CONTEXT (JSON):\n"
        + json.dumps(ctx, ensure_ascii=False, indent=2)
        + "\n\n"
        'Return ONLY a JSON object matching the response schema with key: "reply".\n'
    )


def _parse_backlog_advisor_reply(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        obj = json.loads(text)
    except Exception:
        return text
    if not isinstance(obj, dict):
        return text
    reply = str(obj.get("reply") or "").strip()
    return reply or text


def _tool_trace_from_node_traces(*, run_id: str, node_traces: Any, max_calls: int = 80) -> list[dict[str, Any]]:
    """Extract a bounded, UI-friendly tool execution trace from runtime-owned node traces.

    Best-effort: intentionally avoids returning large tool outputs (e.g. file contents).
    """

    def _summarize_output(output: Any) -> dict[str, Any]:
        try:
            if output is None:
                return {}
            if isinstance(output, str):
                return {"type": "text", "chars": len(output)}
            if isinstance(output, list):
                preview: list[str] = []
                for x in output[:8]:
                    preview.append(_clamp_text(x if isinstance(x, str) else str(x), max_len=240))
                return {"type": "list", "items": len(output), **({"preview": preview} if preview else {})}
            if isinstance(output, dict):
                keys = [str(k) for k in list(output.keys())[:24]]
                return {"type": "object", "key_count": len(output.keys()), "keys": keys}
            return {"type": type(output).__name__}
        except Exception:
            return {}

    out: list[dict[str, Any]] = []
    rid = str(run_id or "").strip()
    nt = node_traces if isinstance(node_traces, dict) else {}
    for node_id, trace in nt.items():
        if len(out) >= max_calls:
            break
        if not isinstance(trace, dict):
            continue
        steps = trace.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if len(out) >= max_calls:
                break
            if not isinstance(step, dict):
                continue
            eff = step.get("effect")
            if not isinstance(eff, dict):
                continue
            if str(eff.get("type") or "").strip().lower() != "tool_calls":
                continue
            payload = eff.get("payload") if isinstance(eff.get("payload"), dict) else {}
            calls = payload.get("tool_calls")
            calls = calls if isinstance(calls, list) else []
            result = step.get("result") if isinstance(step.get("result"), dict) else {}
            results = result.get("results")
            results = results if isinstance(results, list) else []
            res_by_call_id: dict[str, dict[str, Any]] = {}
            for r in results:
                if not isinstance(r, dict):
                    continue
                cid = str(r.get("call_id") or "").strip()
                if cid:
                    res_by_call_id[cid] = r

            ts = str(step.get("ts") or "").strip() or None
            for c in calls[:64]:
                if len(out) >= max_calls:
                    break
                if not isinstance(c, dict):
                    continue
                name = str(c.get("name") or "").strip()
                if not name:
                    continue
                call_id = str(c.get("call_id") or "").strip() or None
                res = res_by_call_id.get(str(call_id or "")) if call_id else None
                success = res.get("success") if isinstance(res, dict) else None
                success = bool(success) if isinstance(success, bool) else None
                error = str((res or {}).get("error") or "").strip() or None
                args = c.get("arguments")
                entry: dict[str, Any] = {
                    "ts": ts,
                    "run_id": rid,
                    "node_id": str(node_id),
                    "name": name,
                    **({"call_id": call_id} if call_id else {}),
                    **({"arguments": args} if args is not None else {}),
                    **({"success": success} if success is not None else {}),
                    **({"error": error} if error else {}),
                }
                if isinstance(res, dict) and "output" in res:
                    summary = _summarize_output(res.get("output"))
                    if summary:
                        entry["output_summary"] = summary
                out.append(entry)

    return out


def _sub_run_ids_from_node_traces(node_traces: Any) -> list[str]:
    out: list[str] = []
    nt = node_traces if isinstance(node_traces, dict) else {}
    for trace in nt.values():
        if not isinstance(trace, dict):
            continue
        steps = trace.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            wait = step.get("wait")
            if not isinstance(wait, dict):
                continue
            reason = str(wait.get("reason") or "").strip().lower()
            if reason != "subworkflow":
                continue
            details = wait.get("details") if isinstance(wait.get("details"), dict) else {}
            sub = str(details.get("sub_run_id") or "").strip()
            if sub:
                out.append(sub)

    seen: set[str] = set()
    cleaned: list[str] = []
    for rid in out:
        if rid in seen:
            continue
        seen.add(rid)
        cleaned.append(rid)
        if len(cleaned) >= 24:
            break
    return cleaned


def _collect_tool_trace_for_run_tree(*, svc: Any, root_run_id: str, max_runs: int = 16, max_calls: int = 80) -> list[dict[str, Any]]:
    """Collect tool calls across a run and its subworkflow run tree (bounded)."""
    run_store = getattr(getattr(svc, "host", None), "run_store", None)
    if run_store is None:
        return []

    def _load(rid: str) -> Any:
        try:
            return run_store.load(str(rid))
        except Exception:
            return None

    def _node_traces(run: Any) -> dict[str, Any]:
        try:
            vars_obj = getattr(run, "vars", None)
        except Exception:
            vars_obj = None
        if not isinstance(vars_obj, dict):
            return {}
        rt = vars_obj.get("_runtime")
        if not isinstance(rt, dict):
            return {}
        nt = rt.get("node_traces")
        return nt if isinstance(nt, dict) else {}

    queue: list[str] = [str(root_run_id)]
    seen: set[str] = set()
    trace: list[dict[str, Any]] = []

    while queue and len(seen) < max_runs and len(trace) < max_calls:
        rid = str(queue.pop(0) or "").strip()
        if not rid or rid in seen:
            continue
        seen.add(rid)
        run = _load(rid)
        if run is None:
            continue
        nt = _node_traces(run)
        if nt:
            trace.extend(_tool_trace_from_node_traces(run_id=rid, node_traces=nt, max_calls=max_calls - len(trace)))
            queue.extend(_sub_run_ids_from_node_traces(nt))

    try:
        trace.sort(key=lambda x: str((x or {}).get("ts") or ""))
    except Exception:
        pass
    return trace[:max_calls]


def _backlog_agent_allowed_paths(*, repo_root: Path, svc: Any) -> list[str]:
    """Return additional allowed workspace roots for backlog advisor/maintainer runs.

    Rationale:
    - The repo is always the workspace root (code/docs).
    - The gateway data dir may be outside the repo in deployments; allow read-only access so the
      agent can consult logs/ledgers/artifacts for debugging and grounding.
    - Operators may extend the allowlist via env, but should avoid pointing at secret-bearing dirs
      when using remote providers.
    """
    out: list[str] = []

    if _env_bool("ABSTRACTGATEWAY_BACKLOG_AGENT_ALLOW_GATEWAY_DATA_DIR", True):
        try:
            data_dir = getattr(getattr(svc, "config", None), "data_dir", None)
            if isinstance(data_dir, (str, Path)) and str(data_dir).strip():
                out.append(str(Path(str(data_dir)).expanduser().resolve()))
        except Exception:
            pass

    extra = _parse_lines_or_json_list(os.getenv("ABSTRACTGATEWAY_BACKLOG_AGENT_ALLOWED_PATHS"))
    out.extend([str(x) for x in extra if isinstance(x, str) and str(x).strip()])

    seen: set[str] = set()
    cleaned: list[str] = []
    for p in out:
        raw = str(p or "").strip()
        if not raw:
            continue
        try:
            resolved = str(Path(raw).expanduser().resolve())
        except Exception:
            resolved = raw
        if resolved in seen:
            continue
        seen.add(resolved)
        cleaned.append(resolved)

    # Keep list bounded.
    return cleaned[:32]


@router.post("/backlog/maintain", response_model=BacklogAssistResponse)
async def backlog_maintain(req: BacklogMaintainRequest) -> BacklogAssistResponse:
    """Agentic (tool-using) maintenance assistant for an existing backlog item.

    Runs the `basic-agent` bundle (default entrypoint) with a structured JSON response schema.
    """
    repo_root = _triage_repo_root_from_env()
    if repo_root is None:
        raise HTTPException(status_code=404, detail="Backlog browsing not configured on this gateway")

    k = _safe_backlog_kind(req.kind)
    if k is None or k not in _BACKLOG_ASSIST_KINDS:
        raise HTTPException(status_code=400, detail="Invalid backlog kind for maintain (planned|proposed|recurrent|deprecated)")

    safe_name = _safe_backlog_filename(req.filename)
    if safe_name is None:
        raise HTTPException(status_code=400, detail="Invalid filename")

    pkg = _safe_backlog_package(req.package)
    if pkg is None:
        raise HTTPException(status_code=400, detail="Invalid package")

    title = str(req.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")

    summary = str(req.summary or "").strip()
    draft_md = str(req.draft_markdown or "")
    if len(draft_md) > 600_000:
        draft_md = draft_md[:600_000] + "\n…(truncated)…\n"

    dir_path = _backlog_dir_for(repo_root, k)
    backlog_path = (dir_path / safe_name).resolve()
    try:
        backlog_path.relative_to(dir_path)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")
    if not backlog_path.exists():
        raise HTTPException(status_code=404, detail="Backlog item not found")

    # Template is used as a style anchor in the prompt.
    template_md = _read_backlog_template(repo_root)

    provider, model = _resolve_gateway_provider_model_or_400(
        provider=req.provider,
        model=req.model,
        purpose="backlog maintenance",
    )

    # Build an agentic prompt (tools can read/search the repo + web).
    prompt = _build_backlog_maintain_agent_prompt(
        repo_root=repo_root,
        kind=k,
        filename=safe_name,
        package=pkg,
        title=title,
        summary=summary,
        template_md=template_md,
        draft_md=draft_md,
        messages=req.messages or [],
    )

    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["reply", "draft_markdown"],
        "properties": {
            "reply": {"type": "string"},
            "draft_markdown": {"type": "string"},
        },
    }

    svc = get_gateway_service()
    if not bool(getattr(svc.config, "runner_enabled", True)):
        raise HTTPException(status_code=409, detail="Gateway runner is disabled (start the gateway without --no-runner)")
    svc.runner.start()

    # Ensure the basic-agent bundle exists (bundle workflow source required).
    host = _require_bundle_host(svc)
    allowed_paths = _backlog_agent_allowed_paths(repo_root=repo_root, svc=svc)
    access_mode = "workspace_or_allowed" if allowed_paths else "workspace_only"
    try:
        run_id = host.start_run(
            flow_id="",
            bundle_id="basic-agent",
            bundle_version=None,
            input_data={
                "workspace_root": str(repo_root),
                "workspace_access_mode": access_mode,
                **({"workspace_allowed_paths": allowed_paths} if allowed_paths else {}),
                "provider": provider,
                "model": model,
                "prompt": prompt,
                "resp_schema": schema,
                "max_iterations": 12,
                # Defense-in-depth: allow only read/search/network tools.
                "tools": [
                    "read_file",
                    "search_files",
                    "list_files",
                    "skim_folders",
                    "skim_files",
                    "analyze_code",
                    "web_search",
                    "fetch_url",
                ],
                "system": (
                    "You are AbstractFramework Maintenance AI.\n"
                    "Return ONLY valid JSON (no markdown fences) matching the provided schema.\n"
                    "Do not write files or run shell commands.\n"
                    "Never include secrets.\n"
                ),
            },
            actor_id="gateway",
            session_id=None,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to start maintenance agent: {e}")

    # Wait for the run to complete (best-effort).
    deadline = time.time() + 90.0
    last_status = ""
    while time.time() < deadline:
        try:
            run = svc.host.run_store.load(str(run_id))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to load maintenance run: {e}")
        if run is None:
            raise HTTPException(status_code=500, detail="Maintenance run disappeared")
        st = getattr(run, "status", None)
        st_val = getattr(st, "value", None) or str(st or "")
        last_status = str(st_val or "")
        if st == RunStatus.COMPLETED or str(st_val) == "completed":
            break
        if st == RunStatus.FAILED or str(st_val) == "failed":
            err = str(getattr(run, "error", "") or "").strip()
            raise HTTPException(status_code=400, detail=f"Maintenance agent failed: {err or 'unknown error'}")
        if st == RunStatus.CANCELLED or str(st_val) == "cancelled":
            raise HTTPException(status_code=409, detail="Maintenance agent cancelled")
        if st == RunStatus.WAITING or str(st_val) == "waiting":
            waiting = getattr(run, "waiting", None)
            reason = getattr(waiting, "reason", None)
            reason_val = getattr(reason, "value", None) or str(reason or "")
            reason_str = str(reason_val or "").strip().lower()
            last_status = f"waiting({reason_str or 'unknown'})"
            if reason == WaitReason.USER or reason_str == "user":
                prompt = str(getattr(waiting, "prompt", "") or "").strip()
                choices = getattr(waiting, "choices", None)
                if isinstance(choices, list) and choices:
                    c = ", ".join([str(x) for x in choices[:12]])
                    raise HTTPException(status_code=409, detail=f"Maintenance agent is waiting for user input: {prompt or '(no prompt)'} (choices: {c})")
                raise HTTPException(status_code=409, detail=f"Maintenance agent is waiting for user input: {prompt or '(no prompt)'}")
            await asyncio.sleep(0.25)
            continue
        await asyncio.sleep(0.25)
    else:
        raise HTTPException(status_code=408, detail=f"Maintenance agent timed out (status={last_status or 'unknown'})")

    try:
        run2 = svc.host.run_store.load(str(run_id))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load maintenance run output: {e}")
    if run2 is None or not isinstance(getattr(run2, "output", None), dict):
        raise HTTPException(status_code=500, detail="Maintenance agent produced no output")
    raw_resp = str((run2.output or {}).get("response") or "").strip()
    reply, draft = _parse_backlog_maintain_reply(raw_resp)
    return BacklogAssistResponse(ok=True, reply=reply, draft_markdown=draft)


@router.post("/backlog/advisor", response_model=BacklogAdvisorResponse)
async def backlog_advisor(req: BacklogAdvisorRequest) -> BacklogAdvisorResponse:
    """Read-only advisor chat for the repo backlog (docs/backlog).

    Runs a ReAct-style agent bundle (default: `basic-agent`) with read/search/list/skim + optional web tools.
    """
    repo_root = _triage_repo_root_from_env()
    if repo_root is None:
        raise HTTPException(status_code=404, detail="Backlog browsing not configured on this gateway")

    provider, model = _resolve_gateway_provider_model_or_400(
        provider=req.provider,
        model=req.model,
        purpose="backlog advisor",
    )

    focus_kind = str(req.focus_kind or "").strip() or None
    focus_type = str(req.focus_type or "").strip() or None

    allow_web_raw = str(os.getenv("ABSTRACTGATEWAY_BACKLOG_ADVISOR_ALLOW_WEB", "") or "").strip().lower()
    allow_web = allow_web_raw in {"1", "true", "yes", "y", "on"}
    tools = ["read_file", "search_files", "list_files", "skim_folders", "skim_files", "analyze_code"]
    if allow_web:
        tools.extend(["web_search", "fetch_url"])

    agent_bundle = str(
        req.agent
        or os.getenv("ABSTRACTGATEWAY_BACKLOG_ADVISOR_AGENT")
        or os.getenv("ABSTRACT_BACKLOG_ADVISOR_AGENT")
        or "basic-agent"
    ).strip() or "basic-agent"

    svc = get_gateway_service()
    allowed_paths = _backlog_agent_allowed_paths(repo_root=repo_root, svc=svc)
    access_mode = "workspace_or_allowed" if allowed_paths else "workspace_only"

    prompt = _build_backlog_advisor_agent_prompt(
        repo_root=repo_root,
        gateway_data_dir=Path(getattr(getattr(svc, "config", None), "data_dir", repo_root)).expanduser().resolve(),
        workspace_allowed_paths=allowed_paths,
        focus_kind=focus_kind,
        focus_type=focus_type,
        web_tools_enabled=allow_web,
        messages=req.messages or [],
    )

    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["reply"],
        "properties": {
            "reply": {"type": "string"},
        },
    }

    if not bool(getattr(svc.config, "runner_enabled", True)):
        raise HTTPException(status_code=409, detail="Gateway runner is disabled (start the gateway without --no-runner)")
    svc.runner.start()

    host = _require_bundle_host(svc)
    try:
        run_id = host.start_run(
            flow_id="",
            bundle_id=agent_bundle,
            bundle_version=None,
            input_data={
                "workspace_root": str(repo_root),
                "workspace_access_mode": access_mode,
                **({"workspace_allowed_paths": allowed_paths} if allowed_paths else {}),
                "provider": provider,
                "model": model,
                "prompt": prompt,
                "resp_schema": schema,
                "max_iterations": 10,
                # Read-only advisor: allow only read/search/list/skim (+ optional web tools).
                "tools": tools,
                "system": (
                    "You are AbstractFramework Backlog Advisor (read-only).\n"
                    "Return ONLY valid JSON (no markdown fences) matching the provided schema.\n"
                    "Do not write files or run shell commands.\n"
                    "Never include secrets.\n"
                ),
            },
            actor_id="gateway",
            session_id=None,
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to start backlog advisor agent: {e}")

    # Wait for the run to complete (best-effort).
    deadline = time.time() + 90.0
    last_status = ""
    while time.time() < deadline:
        try:
            run = svc.host.run_store.load(str(run_id))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to load advisor run: {e}")
        if run is None:
            raise HTTPException(status_code=500, detail="Advisor run disappeared")
        st = getattr(run, "status", None)
        st_val = getattr(st, "value", None) or str(st or "")
        last_status = str(st_val or "")
        if st == RunStatus.COMPLETED or str(st_val) == "completed":
            break
        if st == RunStatus.FAILED or str(st_val) == "failed":
            err = str(getattr(run, "error", "") or "").strip()
            raise HTTPException(status_code=400, detail=f"Advisor failed: {err or 'unknown error'}")
        if st == RunStatus.CANCELLED or str(st_val) == "cancelled":
            raise HTTPException(status_code=409, detail="Advisor cancelled")
        if st == RunStatus.WAITING or str(st_val) == "waiting":
            waiting = getattr(run, "waiting", None)
            reason = getattr(waiting, "reason", None)
            reason_val = getattr(reason, "value", None) or str(reason or "")
            reason_str = str(reason_val or "").strip().lower()
            last_status = f"waiting({reason_str or 'unknown'})"
            if reason == WaitReason.USER or reason_str == "user":
                prompt = str(getattr(waiting, "prompt", "") or "").strip()
                choices = getattr(waiting, "choices", None)
                if isinstance(choices, list) and choices:
                    c = ", ".join([str(x) for x in choices[:12]])
                    raise HTTPException(status_code=409, detail=f"Advisor is waiting for user input: {prompt or '(no prompt)'} (choices: {c})")
                raise HTTPException(status_code=409, detail=f"Advisor is waiting for user input: {prompt or '(no prompt)'}")
            await asyncio.sleep(0.25)
            continue
        await asyncio.sleep(0.25)
    else:
        raise HTTPException(status_code=408, detail=f"Advisor timed out (status={last_status or 'unknown'})")

    try:
        run2 = svc.host.run_store.load(str(run_id))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load advisor run output: {e}")
    if run2 is None or not isinstance(getattr(run2, "output", None), dict):
        raise HTTPException(status_code=500, detail="Advisor produced no output")
    raw_resp = str((run2.output or {}).get("response") or "").strip()
    reply = _parse_backlog_advisor_reply(raw_resp)
    tool_trace = None
    if bool(req.include_trace):
        try:
            tool_trace = _collect_tool_trace_for_run_tree(svc=svc, root_run_id=str(run_id))
        except Exception:
            tool_trace = None
    return BacklogAdvisorResponse(ok=True, reply=reply, run_id=str(run_id), tool_trace=tool_trace)


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


class _GatewayBlocTarget(BaseModel):
    sha256: Optional[str] = Field(default=None, description="Optional durable bloc sha256 selector.")
    bloc_id: Optional[int] = Field(default=None, description="Optional durable bloc numeric identifier.")
    runtime_id: Optional[str] = Field(default=None, description="Optional loaded runtime identifier used to disambiguate local live state.")
    provider: Optional[str] = Field(default=None, description="Optional provider filter or target for KV operations.")
    model: Optional[str] = Field(default=None, description="Optional model filter or target for KV operations.")
    base_url: Optional[str] = Field(default=None, description="Optional upstream provider base URL forwarded to AbstractCore.")
    timeout_s: Optional[float] = Field(default=None, description="Optional timeout forwarded to Runtime/Core.")


class _GatewayUpsertTextBlocRequest(BaseModel):
    path: str = Field(..., description="Logical path/name for the durable text bloc.")
    content: str = Field(..., description="Text content stored in the bloc.")
    sha256: Optional[str] = Field(default=None, description="Optional caller-provided stable digest.")
    content_sha256: Optional[str] = Field(default=None, description="Optional content-only digest.")
    media_type: str = Field(default="text", description="Logical media type stored in the bloc record.")
    size_bytes: Optional[int] = Field(default=None, description="Optional known content size in bytes.")
    mtime_ns: Optional[int] = Field(default=None, description="Optional source mtime in nanoseconds.")
    format: Optional[str] = Field(default=None, description="Optional format label such as text/plain or markdown.")
    estimated_tokens: Optional[int] = Field(default=None, description="Optional estimated token count for the text.")
    relpath_base: Optional[str] = Field(default=None, description="Optional logical base path used for normalization.")
    summary: Optional[str] = Field(default=None, description="Optional short human summary of the bloc.")
    keywords: Optional[list[str]] = Field(default=None, description="Optional search keywords for the bloc.")


class _GatewayBlocKVEnsureRequest(_GatewayBlocTarget):
    artifact_path: Optional[str] = Field(default=None, description="Optional exact artifact path returned by prior list/manifest calls.")
    force_rebuild: bool = Field(default=False, description="Force regeneration of the KV artifact.")
    debug: bool = Field(default=False, description="Request extra Runtime/Core diagnostics where supported.")


class _GatewayBlocKVLoadRequest(_GatewayBlocKVEnsureRequest):
    stable_cache_key: Optional[str] = Field(default=None, description="Optional stable key used when loading the artifact.")
    key: Optional[str] = Field(default=None, description="Optional explicit destination cache key.")
    make_default: bool = Field(default=False, description="Set the loaded key as the provider default when supported.")


class _GatewayBlocKVDeleteRequest(_GatewayBlocTarget):
    artifact_path: Optional[str] = Field(default=None, description="Exact artifact path to delete.")
    clear_loaded: bool = Field(default=False, description="Also clear matching live loaded cache state when Runtime can see it.")
    force: bool = Field(default=False, description="Explicitly confirm the deletion action when the lower layer requires it.")
    dry_run: bool = Field(default=False, description="Preview deletion without mutating anything.")
    debug: bool = Field(default=False, description="Request extra Runtime/Core diagnostics where supported.")


class _GatewayBlocKVPruneRequest(_GatewayBlocTarget):
    clear_loaded: bool = Field(default=False, description="Also clear matching live loaded cache state when Runtime can see it.")
    force: bool = Field(default=False, description="Explicitly confirm the prune action when the lower layer requires it.")
    dry_run: bool = Field(default=False, description="Preview prune results without mutating anything.")
    debug: bool = Field(default=False, description="Request extra Runtime/Core diagnostics where supported.")


class _GatewayBlocDeleteRequest(_GatewayBlocTarget):
    delete_kv: bool = Field(default=True, description="Delete derived KV artifacts along with the durable text bloc.")
    clear_loaded: bool = Field(default=False, description="Also clear matching live loaded cache state when Runtime can see it.")
    force: bool = Field(default=False, description="Explicitly confirm the delete action when the lower layer requires it.")
    dry_run: bool = Field(default=False, description="Preview delete results without mutating anything.")


class _GatewayModelResidencyLoadRequest(BaseModel):
    task: Optional[str] = Field(default=None, description="Residency task, for example text_generation or image_generation.")
    provider: Optional[str] = Field(default=None, description="Provider id for the runtime to load.")
    model: Optional[str] = Field(default=None, description="Model id for the runtime to load.")
    options: Optional[Dict[str, Any]] = Field(default=None, description="Task/provider-specific load options.")
    pin: bool = Field(default=True, description="Keep the runtime resident until it is explicitly unloaded.")
    base_url: Optional[str] = Field(default=None, description="Optional upstream provider base URL forwarded to AbstractCore.")
    timeout_s: Optional[float] = Field(default=None, description="Optional provider load timeout forwarded to AbstractCore.")


class _GatewayModelResidencyUnloadRequest(BaseModel):
    task: Optional[str] = Field(default=None, description="Optional residency task filter.")
    runtime_id: Optional[str] = Field(default=None, description="Runtime identifier returned by load/list.")
    provider: Optional[str] = Field(default=None, description="Provider id for the runtime to unload.")
    model: Optional[str] = Field(default=None, description="Model id for the runtime to unload.")
    options: Optional[Dict[str, Any]] = Field(default=None, description="Task/provider-specific unload options.")
    base_url: Optional[str] = Field(default=None, description="Optional upstream provider base URL used to disambiguate the runtime.")
    timeout_s: Optional[float] = Field(default=None, description="Optional provider unload timeout forwarded to AbstractCore.")


class _GatewayCapabilityDefaultRequest(BaseModel):
    provider: Optional[str] = Field(default=None, description="Default provider/backend id for this capability route.")
    model: Optional[str] = Field(default=None, description="Default model id for this capability route.")
    base_url: Optional[str] = Field(default=None, description="Optional upstream provider base URL for this capability route.")
    options: Optional[Dict[str, Any]] = Field(default=None, description="Optional plugin/provider parameters, such as voice/profile/language.")


class _GatewaySessionPromptCacheTarget(_GatewayPromptCacheTarget):
    bundle_id: Optional[str] = Field(default=None, max_length=160, description="Optional workflow bundle id.")
    bundle_version: Optional[str] = Field(default=None, max_length=80, description="Optional workflow bundle version.")
    flow_id: Optional[str] = Field(default=None, max_length=160, description="Optional workflow flow id.")
    template_id: Optional[str] = Field(default=None, max_length=160, description="Optional stable assistant/agent template id.")
    version: int = Field(default=1, ge=1, le=10, description="Session cache key/schema version.")


class _GatewaySessionPromptCacheRequest(_GatewaySessionPromptCacheTarget):
    modules: Optional[list[Dict[str, Any]]] = Field(
        default=None,
        description="Optional ordered PromptCacheModule dicts. If omitted, convenience fields are converted into stable modules.",
    )
    system_prompt: Optional[str] = Field(default=None, max_length=20000, description="Optional system prompt module.")
    workflow_instructions: Optional[str] = Field(default=None, max_length=20000, description="Optional workflow instructions module.")
    tools: Optional[list[Dict[str, Any]]] = Field(default=None, description="Optional tool schema module.")
    pinned_attachments: Optional[list[Dict[str, Any]]] = Field(default=None, description="Optional pinned artifact refs included in the cache identity.")
    make_default: bool = Field(default=False, description="Set the prepared session key as the provider default when supported.")
    ttl_s: Optional[float] = Field(default=None, description="Optional provider TTL for prepared/forked keys.")


def _gateway_model_residency_contract_descriptor() -> Dict[str, Any]:
    facade, err = _gateway_abstractcore_host_facade()
    endpoints = {
        "loaded": _api_gateway_path("/models/loaded"),
        "load": _api_gateway_path("/models/load"),
        "unload": _api_gateway_path("/models/unload"),
    }
    task_names = ["text_generation", "image_generation", "tts", "stt", "music_generation"]
    supports = {task: False for task in task_names}
    task_capabilities: Dict[str, Any] = {}
    runtime_mode: Optional[str] = None
    relay_only: Optional[bool] = None
    diagnostics: Optional[Dict[str, Any]] = None
    capabilities_source: Optional[str] = None
    available = bool(facade is not None and not err)

    if facade is not None and not err:
        fn = getattr(facade, "get_model_residency_capabilities", None)
        if callable(fn):
            try:
                payload = fn()
            except Exception as exc:
                err = str(exc)
            else:
                payload_dict = dict(payload) if isinstance(payload, dict) else {}
                runtime_mode = str(payload_dict.get("mode") or "").strip() or None
                if payload_dict.get("relay_only") is not None:
                    relay_only = bool(payload_dict.get("relay_only"))
                diagnostics = dict(payload_dict.get("diagnostics")) if isinstance(payload_dict.get("diagnostics"), dict) else None
                capabilities_source = str(payload_dict.get("source") or "").strip() or None
                raw_tasks = payload_dict.get("tasks")
                if isinstance(raw_tasks, dict):
                    task_capabilities = {str(name): dict(info) for name, info in raw_tasks.items() if isinstance(info, dict)}
                    if task_capabilities:
                        task_names = list(task_capabilities.keys())
                        supports = {task: bool(info.get("supported")) for task, info in task_capabilities.items()}
        else:
            available = False
            err = err or "Gateway runtime does not expose model residency capabilities."

    return {
        "route_available": True,
        "available": available,
        "source": "abstractruntime.host_facade",
        "endpoints": endpoints,
        "tasks": task_names,
        "supports": supports,
        "task_capabilities": task_capabilities,
        "supported_tasks": [task for task in task_names if supports.get(task) is True],
        "unsupported_tasks": [task for task in task_names if supports.get(task) is not True],
        **({"mode": runtime_mode} if runtime_mode else {}),
        **({"relay_only": relay_only} if relay_only is not None else {}),
        **({"capabilities_source": capabilities_source} if capabilities_source else {}),
        **({"diagnostics": diagnostics} if diagnostics else {}),
        "ledger": "workflow model_residency effects are ledgered by Runtime; operator model routes are not ledger commands",
        **({"config_hint": err} if err else {}),
    }


def _gateway_durable_bloc_contract_descriptor() -> Dict[str, Any]:
    facade, err = _gateway_abstractcore_host_facade()
    endpoints = {
        "upsert_text": _api_gateway_path("/blocs/upsert_text"),
        "record": _api_gateway_path("/blocs/record"),
        "list": _api_gateway_path("/blocs"),
        "delete": _api_gateway_path("/blocs/delete"),
        "kv_manifest": _api_gateway_path("/blocs/kv/manifest"),
        "kv_list": _api_gateway_path("/blocs/kv/list"),
        "kv_ensure": _api_gateway_path("/blocs/kv/ensure"),
        "kv_load": _api_gateway_path("/blocs/kv/load"),
        "kv_delete": _api_gateway_path("/blocs/kv/delete"),
        "kv_prune": _api_gateway_path("/blocs/kv/prune"),
    }
    required = (
        "upsert_text_bloc",
        "get_bloc_record",
        "list_blocs",
        "get_bloc_kv_manifest",
        "ensure_bloc_kv_artifact",
        "load_bloc_kv_artifact",
    )
    lifecycle = (
        "list_bloc_kv_artifacts",
        "delete_bloc_kv_artifact",
        "prune_bloc_kv_artifacts",
        "delete_bloc",
    )
    available = facade is not None and all(callable(getattr(facade, name, None)) for name in required)
    lifecycle_available = facade is not None and all(callable(getattr(facade, name, None)) for name in lifecycle)
    return {
        "route_available": True,
        "available": bool(available),
        "lifecycle_available": bool(lifecycle_available),
        "source": "abstractruntime.host_facade",
        "endpoints": endpoints,
        "stable_identifiers": ["bloc_id", "sha256"],
        "exact_reuse_binding_param": "prompt_cache_binding",
        "ledger": "durable bloc routes are host operator controls; use prompt_cache_binding in Runtime run execution for ledgered exact reuse",
        **({"config_hint": err} if err else {}),
    }


def _gateway_bloc_unavailable(*, operation: str, error: str) -> Dict[str, Any]:
    return {
        "ok": False,
        "supported": False,
        "operation": operation,
        "code": "bloc_unavailable",
        "error": error,
        "source": "abstractruntime.host_facade",
        "route_available": True,
    }


def _gateway_bloc_client_call(
    *,
    method_name: str,
    operation: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    control, err = _gateway_abstractcore_host_facade()
    if err or control is None:
        return _gateway_bloc_unavailable(
            operation=operation,
            error=err or "Gateway runtime does not expose AbstractCore durable bloc controls.",
        )

    fn = getattr(control, method_name, None)
    if not callable(fn):
        return _gateway_bloc_unavailable(
            operation=operation,
            error=f"Gateway runtime does not expose durable bloc operation `{method_name}`.",
        )

    try:
        result = fn(**dict(payload or {}))
    except Exception as e:
        return {
            "ok": False,
            "supported": False,
            "operation": operation,
            "code": "bloc_error",
            "error": str(e),
            "source": "abstractruntime.host_facade",
            "route_available": True,
        }

    out = dict(result) if isinstance(result, dict) else {"ok": True, "data": result}
    out.setdefault("operation", operation)
    out.setdefault("supported", True)
    out.setdefault("source", "abstractruntime.host_facade")
    out.setdefault("route_available", True)
    return out


def _gateway_model_residency_unavailable(*, operation: str, error: str) -> Dict[str, Any]:
    return {
        "ok": False,
        "success": False,
        "supported": False,
        "operation": operation,
        "code": "model_residency_unavailable",
        "error": error,
        "warnings": [error],
        "affected_models": [],
    }


def _gateway_model_residency_client_call(
    *,
    method_name: str,
    operation: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    control, err = _gateway_abstractcore_host_facade()
    if err or control is None:
        return _gateway_model_residency_unavailable(
            operation=operation,
            error=err or "Gateway runtime does not expose AbstractCore model residency controls.",
        )

    fn = getattr(control, method_name, None)
    if not callable(fn):
        return _gateway_model_residency_unavailable(
            operation=operation,
            error="Gateway runtime does not expose model residency controls.",
        )

    try:
        result = fn(**dict(payload or {}))
    except Exception as e:
        return {
            "ok": False,
            "success": False,
            "supported": False,
            "operation": operation,
            "code": "model_residency_error",
            "error": str(e),
            "warnings": [str(e)],
            "affected_models": [],
        }
    if isinstance(result, dict):
        result.setdefault("operation", operation)
        return result
    return {"ok": True, "supported": True, "operation": operation, "data": result}


def _normalize_gateway_model_residency_response(payload: Dict[str, Any], *, operation: str) -> Dict[str, Any]:
    out = dict(payload)
    out.setdefault("operation", operation)
    out.setdefault("source", "abstractruntime.host_facade")
    out.setdefault("route_available", True)
    out.setdefault("success", out.get("ok") is not False)
    if operation == "list_loaded" and "models" not in out:
        for key in ("data", "loaded", "runtimes"):
            if isinstance(out.get(key), list):
                out["models"] = out[key]
                break
    if "affected_models" not in out:
        if isinstance(out.get("models"), list):
            out["affected_models"] = out["models"]
        elif isinstance(out.get("runtime"), dict):
            out["affected_models"] = [out["runtime"]]
        else:
            out["affected_models"] = []
    return out


def _model_dump_excluding_none(model: BaseModel) -> Dict[str, Any]:
    try:
        return dict(model.model_dump(exclude_none=True))
    except AttributeError:  # pragma: no cover - pydantic v1 compatibility
        return dict(model.dict(exclude_none=True))


def _gateway_prompt_cache_empty_caps() -> Dict[str, Any]:
    return {"supported": False, "mode": "none"}


def _gateway_prompt_cache_client_capabilities(*, llm_client: Any, provider: str, model: str) -> Dict[str, Any]:
    getter = getattr(llm_client, "get_prompt_cache_capabilities", None)
    if not callable(getter):
        return {"supported": False, "capabilities": _gateway_prompt_cache_empty_caps()}
    try:
        info = getter(provider=provider, model=model)
    except Exception as e:
        return {"supported": False, "error": str(e), "capabilities": _gateway_prompt_cache_empty_caps()}
    if isinstance(info, dict):
        caps = info.get("capabilities")
        if isinstance(caps, dict):
            return {"supported": bool(info.get("supported")), "capabilities": dict(caps), **({"error": str(info.get("error"))} if info.get("error") else {})}
    return {"supported": False, "capabilities": _gateway_prompt_cache_empty_caps()}


def _gateway_prompt_cache_client_call(
    *,
    llm_client: Any,
    method_name: str,
    operation: str,
    provider: str,
    model: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    caps_info = _gateway_prompt_cache_client_capabilities(llm_client=llm_client, provider=provider, model=model)
    caps = caps_info.get("capabilities") if isinstance(caps_info, dict) else _gateway_prompt_cache_empty_caps()
    if not isinstance(caps, dict):
        caps = _gateway_prompt_cache_empty_caps()

    fn = getattr(llm_client, method_name, None)
    if not callable(fn):
        return {
            "supported": False,
            "operation": operation,
            "code": "prompt_cache_unsupported",
            "error": "Runtime LLM client does not expose this prompt cache operation",
            "capabilities": caps,
        }

    kwargs = dict(payload or {})
    kwargs["provider"] = provider
    kwargs["model"] = model
    try:
        result = fn(**kwargs)
    except Exception as e:
        return {
            "supported": False,
            "operation": operation,
            "code": "prompt_cache_error",
            "error": str(e),
            "capabilities": caps,
        }

    if isinstance(result, dict):
        result.setdefault("operation", operation)
        result.setdefault("capabilities", caps)
        return result
    if isinstance(result, bool):
        return {"supported": True, "operation": operation, "ok": bool(result), "capabilities": caps}
    return {"supported": True, "operation": operation, "result": result, "capabilities": caps}


def _normalize_gateway_prompt_cache_admin_response(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(payload) if isinstance(payload, dict) else {"ok": True, "data": payload}
    out.setdefault("source", "abstractruntime.host_facade")
    out.setdefault("route_available", True)
    return out


def _session_cache_label(value: Any, *, fallback: str, max_len: int = 36) -> str:
    raw = str(value or "").strip()
    if not raw:
        return fallback
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", raw).strip("._-").lower()
    if not safe:
        return fallback
    return safe[: max(1, int(max_len))].rstrip("._-") or fallback


def _session_prompt_cache_identity(
    *,
    session_id: str,
    provider: str,
    model: str,
    bundle_id: Optional[str] = None,
    bundle_version: Optional[str] = None,
    flow_id: Optional[str] = None,
    template_id: Optional[str] = None,
    version: int = 1,
) -> Dict[str, Any]:
    sid = str(session_id or "").strip()
    prov = str(provider or "").strip()
    mod = str(model or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="session_id is required")
    if not prov:
        raise HTTPException(status_code=400, detail="provider is required")
    if not mod:
        raise HTTPException(status_code=400, detail="model is required")

    identity = {
        "session_id": sid,
        "bundle_id": str(bundle_id or "").strip(),
        "bundle_version": str(bundle_version or "").strip(),
        "flow_id": str(flow_id or "").strip(),
        "provider": prov,
        "model": mod,
        "template_id": str(template_id or "").strip(),
        "version": int(version or 1),
    }
    raw = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    parts = [
        f"s-{_session_cache_label(sid, fallback='session')}",
        f"b-{_session_cache_label(bundle_id, fallback='bundle')}",
        f"v-{_session_cache_label(bundle_version, fallback='latest', max_len=24)}",
        f"f-{_session_cache_label(flow_id, fallback='flow')}",
        f"t-{_session_cache_label(template_id, fallback='template')}",
        f"p-{_session_cache_label(prov, fallback='provider', max_len=28)}",
        f"m-{_session_cache_label(mod, fallback='model', max_len=48)}",
    ]
    namespace = f"agw.pc.v{int(version or 1)}." + ".".join(parts) + f".h-{digest[:16]}"
    if len(namespace) > 240:
        namespace = (
            f"agw.pc.v{int(version or 1)}."
            f"s-{_session_cache_label(sid, fallback='session')}."
            f"p-{_session_cache_label(prov, fallback='provider', max_len=28)}."
            f"h-{digest[:24]}"
        )
    key = f"{namespace}:session"
    return {
        "identity": identity,
        "namespace": namespace,
        "prompt_cache_key": key,
        "digest": digest[:24],
    }


def _session_prompt_cache_mode(caps: Dict[str, Any]) -> str:
    if not isinstance(caps, dict) or not bool(caps.get("supported")):
        return "unsupported"
    mode = str(caps.get("mode") or "").strip().lower()
    if mode in {"keyed", "local_control_plane"}:
        return mode
    if bool(caps.get("supports_prepare_modules")) or bool(caps.get("supports_update")) or bool(caps.get("supports_fork")):
        return "local_control_plane"
    return "keyed"


def _session_prompt_cache_runtime_hint(*, key: str, namespace: str, mode: str) -> Dict[str, Any]:
    return {
        "prompt_cache_key": key,
        "_runtime": {
            "prompt_cache": {
                "key": key,
                "namespace": namespace,
                "mode": mode,
                "version": 1,
            }
        },
    }


def _bounded_provider_payload(value: Any, *, max_bytes: int = 40000) -> Any:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        return {"omitted": True, "reason": "provider response was not JSON-serializable"}
    size = len(raw.encode("utf-8"))
    if size <= int(max_bytes):
        try:
            return json.loads(raw)
        except Exception:
            return value
    return {
        "omitted": True,
        "reason": "provider response exceeded session prompt-cache response budget",
        "size_bytes": size,
        "max_bytes": int(max_bytes),
    }


def _session_prompt_cache_base_payload(
    *,
    operation: str,
    session_id: str,
    req: _GatewaySessionPromptCacheTarget,
    capabilities: Dict[str, Any],
    error: Optional[str] = None,
    code: Optional[str] = None,
) -> Dict[str, Any]:
    ident = _session_prompt_cache_identity(
        session_id=session_id,
        provider=req.provider,
        model=req.model,
        bundle_id=req.bundle_id,
        bundle_version=req.bundle_version,
        flow_id=req.flow_id,
        template_id=req.template_id,
        version=int(getattr(req, "version", 1) or 1),
    )
    caps = capabilities if isinstance(capabilities, dict) else _gateway_prompt_cache_empty_caps()
    mode = _session_prompt_cache_mode(caps)
    key = str(ident["prompt_cache_key"])
    namespace = str(ident["namespace"])
    return {
        "supported": bool(caps.get("supported")),
        "operation": operation,
        "mode": mode,
        "session_id": str(session_id),
        "namespace": namespace,
        "prompt_cache_key": key,
        "runtime_hint": _session_prompt_cache_runtime_hint(key=key, namespace=namespace, mode=mode),
        "identity": ident["identity"],
        "capabilities": caps,
        **({"code": code} if code else {}),
        **({"error": error} if error else {}),
    }


def _validate_session_prompt_cache_modules(modules: Any) -> list[Dict[str, Any]]:
    if modules is None:
        return []
    if not isinstance(modules, list):
        raise HTTPException(status_code=400, detail="modules must be a list")
    if len(modules) > 12:
        raise HTTPException(status_code=400, detail="modules must contain at most 12 items")
    try:
        raw = json.dumps(modules, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except TypeError as e:
        raise HTTPException(status_code=400, detail=f"modules must be JSON-serializable: {e}")
    size = len(raw.encode("utf-8"))
    if size > 256_000:
        raise HTTPException(status_code=400, detail=f"modules payload is too large ({size} bytes; max 256000)")
    parsed = json.loads(raw)
    return [dict(m) for m in parsed if isinstance(m, dict)]


def _session_prompt_cache_modules_from_request(req: _GatewaySessionPromptCacheRequest) -> list[Dict[str, Any]]:
    if req.modules is not None:
        return _validate_session_prompt_cache_modules(req.modules)

    modules: list[Dict[str, Any]] = []
    if isinstance(req.system_prompt, str) and req.system_prompt.strip():
        modules.append({"module_id": "system", "system_prompt": req.system_prompt.strip(), "scope": "shared"})
    if isinstance(req.workflow_instructions, str) and req.workflow_instructions.strip():
        modules.append({"module_id": "workflow", "prompt": req.workflow_instructions.strip(), "scope": "shared"})
    if isinstance(req.tools, list) and req.tools:
        tools = _validate_session_prompt_cache_modules([{"tools": req.tools}])
        tool_list = tools[0].get("tools") if tools else []
        if isinstance(tool_list, list) and tool_list:
            modules.append({"module_id": "tools", "tools": tool_list, "scope": "shared"})
    if isinstance(req.pinned_attachments, list) and req.pinned_attachments:
        pins = _validate_session_prompt_cache_modules([{"attachments": req.pinned_attachments}])
        attachments = pins[0].get("attachments") if pins else []
        if isinstance(attachments, list) and attachments:
            modules.append(
                {
                    "module_id": "pinned_attachments",
                    "prompt": json.dumps({"pinned_attachments": attachments}, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    "scope": "private",
                }
            )
    return modules


@router.get("/models/loaded")
async def model_residency_loaded(
    request: Request,
    task: Optional[str] = Query(None, description="Optional residency task filter."),
    provider: Optional[str] = Query(None, description="Optional provider filter."),
    model: Optional[str] = Query(None, description="Optional model filter."),
    base_url: Optional[str] = Query(None, description="Optional upstream provider base URL filter."),
) -> Dict[str, Any]:
    _ = request
    query = {
        "task": task,
        "provider": provider,
        "model": model,
        "base_url": base_url,
    }
    payload = {k: v for k, v in query.items() if v is not None and str(v).strip()}
    return _normalize_gateway_model_residency_response(
        _gateway_model_residency_client_call(
            method_name="list_model_residency",
            operation="list_loaded",
            payload=payload,
        ),
        operation="list_loaded",
    )


@router.get("/config/capability-defaults")
async def capability_defaults_get() -> Dict[str, Any]:
    """List execution-host Core/Runtime capability routing defaults for thin-client settings UIs."""
    return gateway_capability_defaults_payload()


@router.put("/config/capability-defaults/{kind}/{modality}")
async def capability_defaults_put(kind: str, modality: str, req: _GatewayCapabilityDefaultRequest) -> Dict[str, Any]:
    """Persist one execution-host Core/Runtime capability route default."""
    try:
        save_gateway_capability_default(
            kind,
            modality,
            provider=req.provider,
            model=req.model,
            base_url=req.base_url,
            options=req.options,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return gateway_capability_defaults_payload()


@router.delete("/config/capability-defaults/{kind}/{modality}")
async def capability_defaults_delete(kind: str, modality: str) -> Dict[str, Any]:
    """Clear one execution-host Core/Runtime capability route default."""
    try:
        clear_gateway_capability_default(kind, modality)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return gateway_capability_defaults_payload()


@router.post("/models/load")
async def model_residency_load(request: Request, req: _GatewayModelResidencyLoadRequest) -> Dict[str, Any]:
    _ = request
    body = _model_dump_excluding_none(req)
    body.setdefault("task", "text_generation")
    return _normalize_gateway_model_residency_response(
        _gateway_model_residency_client_call(
            method_name="load_model_residency",
            operation="load",
            payload=body,
        ),
        operation="load",
    )


@router.post("/models/unload")
async def model_residency_unload(request: Request, req: _GatewayModelResidencyUnloadRequest) -> Dict[str, Any]:
    _ = request
    body = _model_dump_excluding_none(req)
    return _normalize_gateway_model_residency_response(
        _gateway_model_residency_client_call(
            method_name="unload_model_residency",
            operation="unload",
            payload=body,
        ),
        operation="unload",
    )


@router.post("/blocs/upsert_text")
async def bloc_upsert_text(request: Request, req: _GatewayUpsertTextBlocRequest) -> Dict[str, Any]:
    body = _model_dump_excluding_none(req)
    provider_api_key = _request_provider_api_key(request)
    if provider_api_key:
        body["provider_api_key"] = provider_api_key
    return _gateway_bloc_client_call(
        method_name="upsert_text_bloc",
        operation="upsert_text",
        payload=body,
    )


@router.get("/blocs/record")
async def bloc_record(
    request: Request,
    sha256: Optional[str] = Query(None, description="Optional durable bloc sha256 selector."),
    bloc_id: Optional[int] = Query(None, description="Optional durable bloc numeric identifier."),
    runtime_id: Optional[str] = Query(None, description="Optional loaded runtime identifier used to disambiguate local live state."),
    provider: Optional[str] = Query(None, description="Optional provider selector."),
    model: Optional[str] = Query(None, description="Optional model selector."),
    base_url: Optional[str] = Query(None, description="Optional upstream provider base URL forwarded to Runtime/Core."),
    timeout_s: Optional[float] = Query(None, description="Optional timeout forwarded to Runtime/Core."),
) -> Dict[str, Any]:
    _ = request
    payload = {
        "sha256": sha256,
        "bloc_id": bloc_id,
        "runtime_id": runtime_id,
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "timeout_s": timeout_s,
    }
    provider_api_key = _request_provider_api_key(request)
    if provider_api_key:
        payload["provider_api_key"] = provider_api_key
    return _gateway_bloc_client_call(
        method_name="get_bloc_record",
        operation="record",
        payload={k: v for k, v in payload.items() if v is not None and (not isinstance(v, str) or v.strip())},
    )


@router.get("/blocs")
async def bloc_list(
    request: Request,
    sha256: Optional[str] = Query(None, description="Optional durable bloc sha256 selector."),
    bloc_id: Optional[int] = Query(None, description="Optional durable bloc numeric identifier."),
    runtime_id: Optional[str] = Query(None, description="Optional loaded runtime identifier used to disambiguate local live state."),
    provider: Optional[str] = Query(None, description="Optional provider selector."),
    model: Optional[str] = Query(None, description="Optional model selector."),
    base_url: Optional[str] = Query(None, description="Optional upstream provider base URL forwarded to Runtime/Core."),
    timeout_s: Optional[float] = Query(None, description="Optional timeout forwarded to Runtime/Core."),
) -> Dict[str, Any]:
    _ = request
    payload = {
        "sha256": sha256,
        "bloc_id": bloc_id,
        "runtime_id": runtime_id,
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "timeout_s": timeout_s,
    }
    provider_api_key = _request_provider_api_key(request)
    if provider_api_key:
        payload["provider_api_key"] = provider_api_key
    return _gateway_bloc_client_call(
        method_name="list_blocs",
        operation="list",
        payload={k: v for k, v in payload.items() if v is not None and (not isinstance(v, str) or v.strip())},
    )


@router.post("/blocs/delete")
async def bloc_delete(request: Request, req: _GatewayBlocDeleteRequest) -> Dict[str, Any]:
    body = _model_dump_excluding_none(req)
    provider_api_key = _request_provider_api_key(request)
    if provider_api_key:
        body["provider_api_key"] = provider_api_key
    return _gateway_bloc_client_call(
        method_name="delete_bloc",
        operation="delete",
        payload=body,
    )


@router.get("/blocs/kv/manifest")
async def bloc_kv_manifest(
    request: Request,
    sha256: Optional[str] = Query(None, description="Optional durable bloc sha256 selector."),
    bloc_id: Optional[int] = Query(None, description="Optional durable bloc numeric identifier."),
    artifact_path: Optional[str] = Query(None, description="Optional exact artifact path returned by prior list calls."),
    runtime_id: Optional[str] = Query(None, description="Optional loaded runtime identifier used to disambiguate local live state."),
    provider: Optional[str] = Query(None, description="Optional provider selector."),
    model: Optional[str] = Query(None, description="Optional model selector."),
    base_url: Optional[str] = Query(None, description="Optional upstream provider base URL forwarded to Runtime/Core."),
    timeout_s: Optional[float] = Query(None, description="Optional timeout forwarded to Runtime/Core."),
) -> Dict[str, Any]:
    _ = request
    payload = {
        "sha256": sha256,
        "bloc_id": bloc_id,
        "artifact_path": artifact_path,
        "runtime_id": runtime_id,
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "timeout_s": timeout_s,
    }
    provider_api_key = _request_provider_api_key(request)
    if provider_api_key:
        payload["provider_api_key"] = provider_api_key
    return _gateway_bloc_client_call(
        method_name="get_bloc_kv_manifest",
        operation="kv_manifest",
        payload={k: v for k, v in payload.items() if v is not None and (not isinstance(v, str) or v.strip())},
    )


@router.get("/blocs/kv/list")
async def bloc_kv_list(
    request: Request,
    sha256: Optional[str] = Query(None, description="Optional durable bloc sha256 selector."),
    bloc_id: Optional[int] = Query(None, description="Optional durable bloc numeric identifier."),
    runtime_id: Optional[str] = Query(None, description="Optional loaded runtime identifier used to disambiguate local live state."),
    provider: Optional[str] = Query(None, description="Optional provider filter."),
    model: Optional[str] = Query(None, description="Optional model filter."),
    base_url: Optional[str] = Query(None, description="Optional upstream provider base URL forwarded to Runtime/Core."),
    timeout_s: Optional[float] = Query(None, description="Optional timeout forwarded to Runtime/Core."),
) -> Dict[str, Any]:
    _ = request
    payload = {
        "sha256": sha256,
        "bloc_id": bloc_id,
        "runtime_id": runtime_id,
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "timeout_s": timeout_s,
    }
    provider_api_key = _request_provider_api_key(request)
    if provider_api_key:
        payload["provider_api_key"] = provider_api_key
    return _gateway_bloc_client_call(
        method_name="list_bloc_kv_artifacts",
        operation="kv_list",
        payload={k: v for k, v in payload.items() if v is not None and (not isinstance(v, str) or v.strip())},
    )


@router.post("/blocs/kv/ensure")
async def bloc_kv_ensure(request: Request, req: _GatewayBlocKVEnsureRequest) -> Dict[str, Any]:
    body = _model_dump_excluding_none(req)
    provider_api_key = _request_provider_api_key(request)
    if provider_api_key:
        body["provider_api_key"] = provider_api_key
    return _gateway_bloc_client_call(
        method_name="ensure_bloc_kv_artifact",
        operation="kv_ensure",
        payload=body,
    )


@router.post("/blocs/kv/load")
async def bloc_kv_load(request: Request, req: _GatewayBlocKVLoadRequest) -> Dict[str, Any]:
    body = _model_dump_excluding_none(req)
    provider_api_key = _request_provider_api_key(request)
    if provider_api_key:
        body["provider_api_key"] = provider_api_key
    return _gateway_bloc_client_call(
        method_name="load_bloc_kv_artifact",
        operation="kv_load",
        payload=body,
    )


@router.post("/blocs/kv/delete")
async def bloc_kv_delete(request: Request, req: _GatewayBlocKVDeleteRequest) -> Dict[str, Any]:
    body = _model_dump_excluding_none(req)
    provider_api_key = _request_provider_api_key(request)
    if provider_api_key:
        body["provider_api_key"] = provider_api_key
    return _gateway_bloc_client_call(
        method_name="delete_bloc_kv_artifact",
        operation="kv_delete",
        payload=body,
    )


@router.post("/blocs/kv/prune")
async def bloc_kv_prune(request: Request, req: _GatewayBlocKVPruneRequest) -> Dict[str, Any]:
    body = _model_dump_excluding_none(req)
    provider_api_key = _request_provider_api_key(request)
    if provider_api_key:
        body["provider_api_key"] = provider_api_key
    return _gateway_bloc_client_call(
        method_name="prune_bloc_kv_artifacts",
        operation="kv_prune",
        payload=body,
    )


@router.get("/prompt_cache/stats")
async def prompt_cache_stats(
    provider: str = Query(..., description="AbstractCore provider name"),
    model: str = Query(..., description="Model id/name"),
) -> Dict[str, Any]:
    control, err = _gateway_abstractcore_host_facade()
    if err or control is None:
        return {
            "supported": False,
            "operation": "stats",
            "code": "prompt_cache_unavailable",
            "error": err or "Gateway runtime does not expose AbstractCore prompt-cache controls.",
            "capabilities": _gateway_prompt_cache_empty_caps(),
        }
    return _gateway_prompt_cache_client_call(
        llm_client=control,
        method_name="get_prompt_cache_stats",
        operation="stats",
        provider=provider,
        model=model,
    )


@router.get("/prompt_cache/capabilities")
async def prompt_cache_capabilities(
    provider: str = Query(..., description="AbstractCore provider name"),
    model: str = Query(..., description="Model id/name"),
) -> Dict[str, Any]:
    control, err = _gateway_abstractcore_host_facade()
    if err or control is None:
        return {
            "supported": False,
            "operation": "capabilities",
            "error": err or "Gateway runtime does not expose AbstractCore prompt-cache controls.",
            "capabilities": _gateway_prompt_cache_empty_caps(),
        }
    info = _gateway_prompt_cache_client_capabilities(llm_client=control, provider=provider, model=model)
    caps = info.get("capabilities") if isinstance(info, dict) else _gateway_prompt_cache_empty_caps()
    return {
        "supported": bool(info.get("supported") if isinstance(info, dict) else False),
        "operation": "capabilities",
        "capabilities": caps if isinstance(caps, dict) else _gateway_prompt_cache_empty_caps(),
        **({"error": str(info.get("error"))} if isinstance(info, dict) and info.get("error") else {}),
    }


@router.post("/prompt_cache/set")
async def prompt_cache_set(req: _GatewayPromptCacheSetRequest) -> Dict[str, Any]:
    control, err = _gateway_abstractcore_host_facade()
    if err or control is None:
        return {
            "supported": False,
            "operation": "set",
            "code": "prompt_cache_unavailable",
            "error": err or "Gateway runtime does not expose AbstractCore prompt-cache controls.",
            "capabilities": _gateway_prompt_cache_empty_caps(),
        }
    return _gateway_prompt_cache_client_call(
        llm_client=control,
        method_name="prompt_cache_set",
        operation="set",
        provider=req.provider,
        model=req.model,
        payload={"key": req.key, "make_default": bool(req.make_default), "ttl_s": req.ttl_s},
    )


@router.post("/prompt_cache/update")
async def prompt_cache_update(req: _GatewayPromptCacheUpdateRequest) -> Dict[str, Any]:
    control, err = _gateway_abstractcore_host_facade()
    if err or control is None:
        return {
            "supported": False,
            "operation": "update",
            "code": "prompt_cache_unavailable",
            "error": err or "Gateway runtime does not expose AbstractCore prompt-cache controls.",
            "capabilities": _gateway_prompt_cache_empty_caps(),
        }
    return _gateway_prompt_cache_client_call(
        llm_client=control,
        method_name="prompt_cache_update",
        operation="update",
        provider=req.provider,
        model=req.model,
        payload={
            "key": req.key,
            "prompt": str(req.prompt or ""),
            "messages": req.messages,
            "system_prompt": req.system_prompt,
            "tools": req.tools,
            "add_generation_prompt": bool(req.add_generation_prompt),
            "ttl_s": req.ttl_s,
        },
    )


@router.post("/prompt_cache/fork")
async def prompt_cache_fork(req: _GatewayPromptCacheForkRequest) -> Dict[str, Any]:
    control, err = _gateway_abstractcore_host_facade()
    if err or control is None:
        return {
            "supported": False,
            "operation": "fork",
            "code": "prompt_cache_unavailable",
            "error": err or "Gateway runtime does not expose AbstractCore prompt-cache controls.",
            "capabilities": _gateway_prompt_cache_empty_caps(),
        }
    return _gateway_prompt_cache_client_call(
        llm_client=control,
        method_name="prompt_cache_fork",
        operation="fork",
        provider=req.provider,
        model=req.model,
        payload={
            "from_key": req.from_key,
            "to_key": req.to_key,
            "make_default": bool(req.make_default),
            "ttl_s": req.ttl_s,
        },
    )


@router.post("/prompt_cache/clear")
async def prompt_cache_clear(req: _GatewayPromptCacheClearRequest) -> Dict[str, Any]:
    control, err = _gateway_abstractcore_host_facade()
    if err or control is None:
        return {
            "supported": False,
            "operation": "clear",
            "code": "prompt_cache_unavailable",
            "error": err or "Gateway runtime does not expose AbstractCore prompt-cache controls.",
            "capabilities": _gateway_prompt_cache_empty_caps(),
        }
    return _gateway_prompt_cache_client_call(
        llm_client=control,
        method_name="prompt_cache_clear",
        operation="clear",
        provider=req.provider,
        model=req.model,
        payload={"key": req.key},
    )


@router.post("/prompt_cache/prepare_modules")
async def prompt_cache_prepare_modules(req: _GatewayPromptCachePrepareModulesRequest) -> Dict[str, Any]:
    control, err = _gateway_abstractcore_host_facade()
    if err or control is None:
        return {
            "supported": False,
            "operation": "prepare_modules",
            "code": "prompt_cache_unavailable",
            "error": err or "Gateway runtime does not expose AbstractCore prompt-cache controls.",
            "capabilities": _gateway_prompt_cache_empty_caps(),
        }
    return _gateway_prompt_cache_client_call(
        llm_client=control,
        method_name="prompt_cache_prepare_modules",
        operation="prepare_modules",
        provider=req.provider,
        model=req.model,
        payload={
            "namespace": req.namespace,
            "modules": req.modules,
            "make_default": bool(req.make_default),
            "ttl_s": req.ttl_s,
            "version": int(req.version),
        },
    )


@router.get("/sessions/{session_id}/prompt_cache/status")
async def session_prompt_cache_status(
    session_id: str,
    provider: str = Query(..., description="AbstractCore provider name"),
    model: str = Query(..., description="Model id/name"),
    bundle_id: Optional[str] = Query(None, description="Optional workflow bundle id"),
    bundle_version: Optional[str] = Query(None, description="Optional workflow bundle version"),
    flow_id: Optional[str] = Query(None, description="Optional workflow flow id"),
    template_id: Optional[str] = Query(None, description="Optional assistant/agent template id"),
    version: int = Query(1, ge=1, le=10, description="Session cache key/schema version"),
) -> Dict[str, Any]:
    req = _GatewaySessionPromptCacheTarget(
        provider=provider,
        model=model,
        bundle_id=bundle_id,
        bundle_version=bundle_version,
        flow_id=flow_id,
        template_id=template_id,
        version=version,
    )
    control, err = _gateway_abstractcore_host_facade()
    if err or control is None:
        payload = _session_prompt_cache_base_payload(
            operation="status",
            session_id=session_id,
            req=req,
            capabilities=_gateway_prompt_cache_empty_caps(),
            error=err or "Gateway runtime does not expose AbstractCore prompt-cache controls.",
            code="prompt_cache_unavailable",
        )
        payload["ok"] = False
        return payload

    info = _gateway_prompt_cache_client_capabilities(llm_client=control, provider=provider, model=model)
    caps = info.get("capabilities") if isinstance(info, dict) else _gateway_prompt_cache_empty_caps()
    if not isinstance(caps, dict):
        caps = _gateway_prompt_cache_empty_caps()
    payload = _session_prompt_cache_base_payload(
        operation="status",
        session_id=session_id,
        req=req,
        capabilities=caps,
        error=str(info.get("error")) if isinstance(info, dict) and info.get("error") else None,
        code=None if bool(caps.get("supported")) else "prompt_cache_unsupported",
    )
    payload["ok"] = bool(caps.get("supported"))
    if bool(caps.get("supports_stats")):
        stats = _gateway_prompt_cache_client_call(
            llm_client=control,
            method_name="get_prompt_cache_stats",
            operation="stats",
            provider=provider,
            model=model,
        )
        payload["stats"] = _bounded_provider_payload(stats.get("stats") if isinstance(stats, dict) and "stats" in stats else stats)
    return payload


@router.post("/sessions/{session_id}/prompt_cache/prepare")
async def session_prompt_cache_prepare(session_id: str, req: _GatewaySessionPromptCacheRequest) -> Dict[str, Any]:
    control, err = _gateway_abstractcore_host_facade()
    if err or control is None:
        payload = _session_prompt_cache_base_payload(
            operation="prepare",
            session_id=session_id,
            req=req,
            capabilities=_gateway_prompt_cache_empty_caps(),
            error=err or "Gateway runtime does not expose AbstractCore prompt-cache controls.",
            code="prompt_cache_unavailable",
        )
        payload["ok"] = False
        return payload

    info = _gateway_prompt_cache_client_capabilities(llm_client=control, provider=req.provider, model=req.model)
    caps = info.get("capabilities") if isinstance(info, dict) else _gateway_prompt_cache_empty_caps()
    if not isinstance(caps, dict):
        caps = _gateway_prompt_cache_empty_caps()
    payload = _session_prompt_cache_base_payload(
        operation="prepare",
        session_id=session_id,
        req=req,
        capabilities=caps,
        error=str(info.get("error")) if isinstance(info, dict) and info.get("error") else None,
        code=None if bool(caps.get("supported")) else "prompt_cache_unsupported",
    )
    if not bool(caps.get("supported")):
        payload["ok"] = False
        return payload

    modules = _session_prompt_cache_modules_from_request(req)
    mode = str(payload.get("mode") or "unsupported")
    provider_responses: Dict[str, Any] = {}
    active_key = str(payload["prompt_cache_key"])
    prefix_key: Optional[str] = None

    if mode == "local_control_plane" and modules and bool(caps.get("supports_prepare_modules")):
        prepared = _gateway_prompt_cache_client_call(
            llm_client=control,
            method_name="prompt_cache_prepare_modules",
            operation="prepare_modules",
            provider=req.provider,
            model=req.model,
            payload={
                "namespace": str(payload["namespace"]),
                "modules": modules,
                "make_default": False,
                "ttl_s": req.ttl_s,
                "version": int(req.version),
            },
        )
        provider_responses["prepare_modules"] = _bounded_provider_payload(prepared)
        if not bool(prepared.get("supported")):
            payload.update({"ok": False, "code": str(prepared.get("code") or "prompt_cache_prepare_failed"), "provider_responses": provider_responses})
            if prepared.get("error"):
                payload["error"] = str(prepared.get("error"))
            return payload

        prefix_key0 = prepared.get("final_cache_key")
        prefix_key = str(prefix_key0).strip() if isinstance(prefix_key0, str) and prefix_key0.strip() else None
        if prefix_key and bool(caps.get("supports_fork")):
            forked = _gateway_prompt_cache_client_call(
                llm_client=control,
                method_name="prompt_cache_fork",
                operation="fork",
                provider=req.provider,
                model=req.model,
                payload={
                    "from_key": prefix_key,
                    "to_key": active_key,
                    "make_default": bool(req.make_default),
                    "ttl_s": req.ttl_s,
                },
            )
            provider_responses["fork"] = _bounded_provider_payload(forked)
            ok = bool(forked.get("supported")) and bool(forked.get("ok", True))
            payload.update({"ok": ok, "prepared": ok, "prefix_cache_key": prefix_key, "provider_responses": provider_responses})
            if not ok:
                payload["code"] = str(forked.get("code") or "prompt_cache_fork_failed")
                if forked.get("error"):
                    payload["error"] = str(forked.get("error"))
            return payload

        if prefix_key:
            active_key = prefix_key
            payload["prompt_cache_key"] = active_key
            payload["runtime_hint"] = _session_prompt_cache_runtime_hint(
                key=active_key,
                namespace=str(payload["namespace"]),
                mode=mode,
            )
        payload.update({"ok": True, "prepared": True, "prefix_cache_key": prefix_key, "provider_responses": provider_responses})
        return payload

    if mode == "local_control_plane" and not modules and bool(caps.get("supports_set")):
        selected = _gateway_prompt_cache_client_call(
            llm_client=control,
            method_name="prompt_cache_set",
            operation="set",
            provider=req.provider,
            model=req.model,
            payload={"key": active_key, "make_default": bool(req.make_default), "ttl_s": req.ttl_s},
        )
        provider_responses["set"] = _bounded_provider_payload(selected)
        ok = bool(selected.get("supported")) and bool(selected.get("ok", True))
        payload.update({"ok": ok, "prepared": ok, "provider_responses": provider_responses})
        if not ok:
            payload["code"] = str(selected.get("code") or "prompt_cache_set_failed")
            if selected.get("error"):
                payload["error"] = str(selected.get("error"))
        return payload

    payload.update(
        {
            "ok": True,
            "prepared": False,
            "code": "prompt_cache_key_hint_only",
            "hint": "Provider does not expose module preparation here; pass runtime_hint.prompt_cache_key with run input.",
        }
    )
    return payload


@router.post("/sessions/{session_id}/prompt_cache/clear")
async def session_prompt_cache_clear(session_id: str, req: _GatewaySessionPromptCacheRequest) -> Dict[str, Any]:
    control, err = _gateway_abstractcore_host_facade()
    if err or control is None:
        payload = _session_prompt_cache_base_payload(
            operation="clear",
            session_id=session_id,
            req=req,
            capabilities=_gateway_prompt_cache_empty_caps(),
            error=err or "Gateway runtime does not expose AbstractCore prompt-cache controls.",
            code="prompt_cache_unavailable",
        )
        payload["ok"] = False
        return payload

    info = _gateway_prompt_cache_client_capabilities(llm_client=control, provider=req.provider, model=req.model)
    caps = info.get("capabilities") if isinstance(info, dict) else _gateway_prompt_cache_empty_caps()
    if not isinstance(caps, dict):
        caps = _gateway_prompt_cache_empty_caps()
    payload = _session_prompt_cache_base_payload(
        operation="clear",
        session_id=session_id,
        req=req,
        capabilities=caps,
        code=None if bool(caps.get("supported")) else "prompt_cache_unsupported",
    )
    if not bool(caps.get("supports_clear")):
        payload.update(
            {
                "ok": False,
                "code": "prompt_cache_clear_unsupported",
                "error": "Provider does not support clearing prompt-cache keys through this control plane.",
            }
        )
        return payload

    cleared = _gateway_prompt_cache_client_call(
        llm_client=control,
        method_name="prompt_cache_clear",
        operation="clear",
        provider=req.provider,
        model=req.model,
        payload={"key": str(payload["prompt_cache_key"])},
    )
    ok = bool(cleared.get("supported")) and bool(cleared.get("ok", True))
    payload.update({"ok": ok, "cleared": ok, "provider_responses": {"clear": _bounded_provider_payload(cleared)}})
    if not ok:
        payload["code"] = str(cleared.get("code") or "prompt_cache_clear_failed")
        if cleared.get("error"):
            payload["error"] = str(cleared.get("error"))
    return payload


@router.post("/sessions/{session_id}/prompt_cache/rebuild")
async def session_prompt_cache_rebuild(session_id: str, req: _GatewaySessionPromptCacheRequest) -> Dict[str, Any]:
    cleared = await session_prompt_cache_clear(session_id=session_id, req=req)
    prepared = await session_prompt_cache_prepare(session_id=session_id, req=req)
    out = dict(prepared)
    out["operation"] = "rebuild"
    out["clear_result"] = _bounded_provider_payload(cleared)
    out["prepare_result"] = _bounded_provider_payload(prepared)
    out["ok"] = bool(prepared.get("ok"))
    if not bool(cleared.get("ok")) and str(cleared.get("code") or "") not in {"prompt_cache_clear_failed"}:
        out.setdefault("warnings", []).append("clear was unsupported or unavailable; rebuild returned a fresh prepare/key hint")
    return out


@router.get("/prompt_cache/saved")
async def prompt_cache_saved(
    provider: str = Query(..., description="AbstractCore provider name"),
    model: str = Query(..., description="Model id/name"),
) -> Dict[str, Any]:
    control, err = _gateway_abstractcore_host_facade()
    if err or control is None:
        return _normalize_gateway_prompt_cache_admin_response(
            {
                "supported": False,
                "operation": "list_exports",
                "code": "prompt_cache_unavailable",
                "error": err or "Gateway runtime does not expose AbstractCore prompt-cache controls.",
                "capabilities": _gateway_prompt_cache_empty_caps(),
                "items": [],
            }
        )
    return _normalize_gateway_prompt_cache_admin_response(
        _gateway_prompt_cache_client_call(
            llm_client=control,
            method_name="list_prompt_cache_exports",
            operation="list_exports",
            provider=provider,
            model=model,
        )
    )


@router.post("/prompt_cache/save")
async def prompt_cache_save(req: _GatewayPromptCacheSaveRequest) -> Dict[str, Any]:
    control, err = _gateway_abstractcore_host_facade()
    if err or control is None:
        return _normalize_gateway_prompt_cache_admin_response(
            {
                "supported": False,
                "operation": "export",
                "code": "prompt_cache_unavailable",
                "error": err or "Gateway runtime does not expose AbstractCore prompt-cache controls.",
                "capabilities": _gateway_prompt_cache_empty_caps(),
            }
        )
    return _normalize_gateway_prompt_cache_admin_response(
        _gateway_prompt_cache_client_call(
            llm_client=control,
            method_name="prompt_cache_export",
            operation="export",
            provider=req.provider,
            model=req.model,
            payload={
                "name": req.name,
                "key": req.key,
                "q8": bool(req.q8),
            },
        )
    )


@router.post("/prompt_cache/load")
async def prompt_cache_load(req: _GatewayPromptCacheLoadRequest) -> Dict[str, Any]:
    control, err = _gateway_abstractcore_host_facade()
    if err or control is None:
        return _normalize_gateway_prompt_cache_admin_response(
            {
                "supported": False,
                "operation": "import",
                "code": "prompt_cache_unavailable",
                "error": err or "Gateway runtime does not expose AbstractCore prompt-cache controls.",
                "capabilities": _gateway_prompt_cache_empty_caps(),
            }
        )
    return _normalize_gateway_prompt_cache_admin_response(
        _gateway_prompt_cache_client_call(
            llm_client=control,
            method_name="prompt_cache_import",
            operation="import",
            provider=req.provider,
            model=req.model,
            payload={
                "name": req.name,
                "key": req.key,
                "make_default": bool(req.make_default),
                "clear_existing": bool(req.clear_existing),
            },
        )
    )


@router.get("/host/metrics/gpu")
async def host_gpu_metrics() -> Dict[str, Any]:
    return host_metrics.get_host_gpu_metrics()


@router.post("/embeddings", response_model=EmbeddingsResponse)
async def embeddings(req: EmbeddingsRequest) -> EmbeddingsResponse:
    """Generate embeddings for text inputs using the execution-host embedding.text route.

    Contract:
    - Single embedding space per execution host (provider+model are stable).
    - Request overrides are rejected unless they match the configured route.
    """
    svc = get_gateway_service()
    client = getattr(svc, "embeddings_client", None)
    if client is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Embeddings are not available (missing AbstractCore embedding integration). "
                "Install `abstractruntime[abstractcore]` and configure the execution-host embedding.text route."
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
    """Return the execution-host embedding configuration (singleton embedding space)."""
    svc = get_gateway_service()
    client = getattr(svc, "embeddings_client", None)

    configured_provider = str(getattr(svc, "embedding_provider", "") or getattr(client, "provider", "") or "").strip().lower()
    configured_model = str(getattr(svc, "embedding_model", "") or getattr(client, "model", "") or "").strip()
    configured_base_url = str(getattr(svc, "embedding_base_url", "") or getattr(client, "base_url", "") or "").strip()

    out: Dict[str, Any] = {
        "ok": client is not None,
        "route": "embedding.text",
        "provider": configured_provider,
        "model": configured_model,
        "base_url": configured_base_url or None,
        "dimension": 0,
    }

    if client is None:
        out["error"] = (
            "Embeddings are not available (missing AbstractCore embedding integration). "
            "Install `abstractruntime[abstractcore]` and configure the execution-host embedding.text capability default."
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

    Uses Gateway's workspace helper implementation (including `.abstractignore`).
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

    content = read_workspace_file(str(resolved), start_line=start_line, end_line=end_line)

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

    Uses Gateway's workspace helper implementation (including `.abstractignore`).
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

    content = skim_workspace_files(
        [str(resolved)],
        target_percent=target_percent,
        head_lines=head_lines,
        tail_lines=tail_lines,
    )

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

    ignore = AbstractIgnore.for_path(root)
    if ignore.is_ignored(resolved, is_dir=False):
        raise HTTPException(status_code=403, detail="File is ignored by .abstractignore policy")

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
