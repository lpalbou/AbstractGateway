from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from abstractruntime import EffectType, Runtime, WorkflowRegistry, WorkflowSpec, persist_workflow_snapshot
from abstractruntime.core.runtime import EffectOutcome
from abstractruntime.visualflow_compiler import compile_visualflow
from abstractruntime.workflow_bundle import WorkflowBundle, WorkflowBundleError, open_workflow_bundle

from ..core_config import (
    capability_defaults_config_signature,
    core_server_token,
    gateway_capability_defaults_payload,
    runtime_core_config_file,
)
from ..memory_store import build_gateway_memory_embedder, open_gateway_memory_store
from ..provider_endpoint_profiles import ProviderEndpointProfileError, resolve_effective_endpoint_profile
from ..provider_connections import configured_provider_request_kwargs, providers_screen_api_key
from ..provider_defaults import (
    ProviderModelConfigError,
    default_text_route_connection_kwargs,
    resolve_gateway_provider_model,
)
from ..workflow_deprecations import WorkflowDeprecatedError, WorkflowDeprecationStore
from ..workflow_catalog import (
    CATALOG_SCOPE_TENANT,
    WorkflowCatalogStore,
    catalog_internal_bundle_id,
    load_or_create_workflow_policy_secret,
    principal_can_run_catalog_record,
    parse_catalog_internal_bundle_id,
    verify_workflow_policy_signature,
)
from ..security.principal import GatewayPrincipal, safe_principal_component
from .native_loop_bundles import (
    declares_native_loop_bundle,
    materialize_native_loop_specs,
)


logger = logging.getLogger(__name__)


# Per-run inputs that capped replayed history by message count / chars. Retired
# 2026-09-28 (operator ruling: history replay is the most recent 50k tokens of
# whole turns, nothing else); recorded as ignored when a client still sends them.
_RETIRED_SESSION_HISTORY_INPUTS = ("session_history_max_messages", "session_history_max_chars")

# What a runtime can serve, in the words a reload sentence uses.
_CAPABILITY_WORDS = {"llm": "a language model", "tools": "tool execution", "memory_kg": "the memory store"}
_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _run_is_terminal(run: Any) -> bool:
    status = getattr(run, "status", None)
    return str(getattr(status, "value", status) or "").lower() in _TERMINAL_RUN_STATUSES


@dataclass(frozen=True)
class _GatewayToolSpec:
    name: str
    description: str
    parameters: Dict[str, Any]
    when_to_use: Optional[str] = None
    examples: list[Any] = field(default_factory=list)
    tags: list[Any] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "parameters": dict(self.parameters),
        }
        if self.when_to_use is not None:
            data["when_to_use"] = self.when_to_use
        if self.examples:
            data["examples"] = list(self.examples)
        if self.tags:
            data["tags"] = list(self.tags)
        return data


def _namespace(bundle_id: str, flow_id: str) -> str:
    return f"{bundle_id}:{flow_id}"


def _bundle_ref(bundle_id: str, bundle_version: str) -> str:
    bid = str(bundle_id or "").strip()
    bv = str(bundle_version or "").strip()
    if not bid:
        raise ValueError("bundle_id is required")
    if not bv:
        raise ValueError("bundle_version is required")
    return f"{bid}@{bv}"


def _endpoint_profile_resolver(*, data_root: Path, catalog_root: Path):
    """Build a function that looks up endpoint profiles for one user's store.

    Each user's runtime gets its own lookup function, bound to that user's
    profile directory (plus the gateway root as a fallback). Because the
    directory is fixed when the function is created, one user's runtime can
    never read another user's profiles. Used by both attach helpers below.
    """

    def _resolve(provider_id: str) -> Optional[Dict[str, Any]]:
        try:
            profile = resolve_effective_endpoint_profile(provider_id, base_dir=data_root, root_base_dir=catalog_root)
        except ProviderEndpointProfileError:
            return None
        if profile is None or not profile.enabled:
            return None
        return profile.private_resolution()

    return _resolve


def _attach_tools_only_endpoint_profile_resolver(*, tool_executor: Any, data_root: Path, catalog_root: Path) -> None:
    """Give a tools-only runtime access to endpoint profile lookups.

    Runtimes built with an LLM client get profile lookups through that client.
    Tools-only runtimes have no LLM client, so without this step their tools
    could not resolve "endpoint:..." providers. We attach the lookup function
    directly to the tool executor instead. On older abstractruntime versions
    that lack this hook, we skip quietly — behavior is then the same as
    before the hook existed.
    """
    try:
        from abstractruntime.integrations.abstractcore.tool_executor import (
            attach_endpoint_profile_resolver_getter,
        )
    except Exception:  # noqa: BLE001 - version skew: pre-seam runtime
        return
    resolver = _endpoint_profile_resolver(data_root=data_root, catalog_root=catalog_root)
    try:
        attach_endpoint_profile_resolver_getter(tool_executor, lambda: resolver)
    except Exception:  # noqa: BLE001 - never block a tools-only host build
        pass


def _attach_provider_endpoint_profile_resolver(*, runtime: Runtime, data_root: Path, catalog_root: Path) -> None:
    llm_client = getattr(runtime, "_abstractcore_llm_client", None)
    if llm_client is None:
        return

    _resolve = _endpoint_profile_resolver(data_root=data_root, catalog_root=catalog_root)

    setter = getattr(llm_client, "set_provider_endpoint_profile_resolver", None)
    if callable(setter):
        try:
            setter(_resolve)
            return
        except Exception:
            pass
    try:
        setattr(llm_client, "resolve_provider_endpoint_profile", _resolve)
    except Exception:
        pass


def _resolve_gateway_default_endpoint_profile(
    *,
    provider: Optional[str],
    data_root: Path,
    catalog_root: Path,
) -> tuple[Optional[str], Dict[str, Any], Optional[str]]:
    provider_s = str(provider or "").strip()
    if not provider_s:
        return None, {}, None
    try:
        profile = resolve_effective_endpoint_profile(provider_s, base_dir=data_root, root_base_dir=catalog_root)
    except ProviderEndpointProfileError as exc:
        raise WorkflowBundleError(f"Invalid Gateway provider endpoint profile {provider_s!r}: {exc}") from exc
    if profile is None:
        if provider_s.startswith("endpoint:"):
            # Name the REAL cause when it is knowable (release gap 2): a
            # root profile scoped 'user' is invisible to per-user runtimes,
            # and "not configured" sent operators hunting a config that
            # existed all along.
            from ..provider_endpoint_profiles import explain_endpoint_profile_miss

            reason = explain_endpoint_profile_miss(provider_s, base_dir=data_root, root_base_dir=catalog_root)
            raise WorkflowBundleError(
                reason or f"Gateway provider endpoint profile {provider_s!r} is not configured or is disabled."
            )
        kwargs = configured_provider_request_kwargs(
            provider_s,
            current_base_dir=data_root,
            root_base_dir=catalog_root,
        )
        # The text route's own endpoint wins over the provider connection's:
        # it is the more specific setting, made for this default (backlog 0994
        # item 64 -- runs ignored it). Absent -> unchanged.
        kwargs.update(default_text_route_connection_kwargs(provider_s, base_dir=data_root))
        return provider_s, kwargs, None

    llm_kwargs: Dict[str, Any] = {}
    if profile.base_url:
        llm_kwargs["base_url"] = profile.base_url
    if profile.api_key:
        llm_kwargs["api_key"] = profile.api_key
    return profile.provider_family, llm_kwargs, profile.virtual_provider_id


def _with_voice_openai_key(llm_kwargs: Optional[Dict[str, Any]], data_root: Path, catalog_root: Path) -> Optional[Dict[str, Any]]:
    """`llm_kwargs` plus AbstractVoice's host setting `voice_openai_api_key`:
    the OpenAI key saved through the Providers screen, so voice generation
    (TTS/STT runs) uses the key the consoles call configured. Resolved when
    this runtime is built; None/unchanged when no key is saved."""
    key = providers_screen_api_key("openai", current_base_dir=data_root, root_base_dir=catalog_root)
    if not key:
        return llm_kwargs or None
    out = dict(llm_kwargs or {})
    out["voice_openai_api_key"] = key
    return out


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


def _try_parse_semver(v: str) -> Optional[tuple[int, int, int]]:
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


def _is_draft_bundle_version(v: str) -> bool:
    return str(v or "").strip().lower().startswith("draft.")


def _pick_latest_version(versions: Dict[str, WorkflowBundle]) -> str:
    items = [(str(k), v) for k, v in (versions or {}).items() if isinstance(k, str)]
    if not items:
        return "0.0.0"
    published_items = [(ver, b) for ver, b in items if not _is_draft_bundle_version(ver)]
    if published_items:
        items = published_items

    if all(_try_parse_semver(ver) is not None for ver, _ in items):
        return max(items, key=lambda x: _try_parse_semver(x[0]) or (0, 0, 0))[0]

    # Fallback when versions are not semver-like: prefer newest created_at.
    def _key(x: tuple[str, WorkflowBundle]) -> tuple[str, str]:
        ver, b = x
        created = str(getattr(getattr(b, "manifest", None), "created_at", "") or "")
        return (created, ver)

    return max(items, key=_key)[0]


def _coerce_namespaced_id(*, bundle_id: Optional[str], flow_id: str, default_bundle_id: Optional[str]) -> str:
    fid = str(flow_id or "").strip()
    if not fid:
        raise ValueError("flow_id is required")

    bid = str(bundle_id or "").strip() if isinstance(bundle_id, str) else ""
    if bid:
        # If the caller passed a fully-qualified id (bundle:flow), allow it as-is so
        # clients can safely send both {bundle_id, flow_id} without producing a
        # double-namespace like "bundle:bundle:flow".
        if ":" in fid:
            prefix = fid.split(":", 1)[0].strip()
            if prefix == bid:
                return fid
            raise ValueError(
                f"flow_id '{fid}' is already namespaced, but bundle_id '{bid}' was also provided; "
                "omit bundle_id or pass a non-namespaced flow_id"
            )
        return _namespace(bid, fid)

    # Allow passing a fully-qualified id as flow_id.
    if ":" in fid:
        return fid

    if default_bundle_id:
        return _namespace(default_bundle_id, fid)

    raise ValueError("bundle_id is required when multiple bundles are loaded (or pass flow_id as 'bundle:flow')")


def _catalog_workflow_parts(workflow_id: Any) -> Optional[tuple[str, str, str, str]]:
    wid = str(workflow_id or "").strip()
    if ":" not in wid:
        return None
    prefix, flow_id = wid.split(":", 1)
    bid, bver = _split_bundle_ref(prefix)
    if not bid or not bver or not flow_id.strip():
        return None
    parsed = parse_catalog_internal_bundle_id(bid)
    if parsed is None:
        return None
    scope, tenant_id, public_bid = parsed
    return scope, tenant_id, public_bid, bver


def _runtime_workflow_policy(vars_obj: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(vars_obj, dict):
        return None
    runtime_ns = vars_obj.get("_runtime")
    policy = runtime_ns.get("workflow_policy") if isinstance(runtime_ns, dict) else None
    if not isinstance(policy, dict):
        return None
    return policy


def _policy_principal(policy: Dict[str, Any]) -> GatewayPrincipal:
    raw = policy.get("principal")
    p = raw if isinstance(raw, dict) else {}
    roles = p.get("roles")
    scopes = p.get("scopes")
    return GatewayPrincipal(
        user_id=str(p.get("user_id") or ""),
        tenant_id=str(p.get("tenant_id") or "default"),
        roles=tuple(str(r).strip() for r in (roles if isinstance(roles, list) else []) if str(r or "").strip()),
        scopes=tuple(str(s).strip() for s in (scopes if isinstance(scopes, list) else []) if str(s or "").strip()),
        runtime_id=str(p.get("runtime_id") or p.get("user_id") or ""),
        source=str(p.get("source") or "workflow-policy"),
    )


def _runtime_workflow_policy_error(
    vars_obj: Any,
    *,
    workflow_id: str,
    policy_secret: str,
    catalog_store: WorkflowCatalogStore,
    expected_tenant_id: str,
    expected_runtime_id: str,
) -> Optional[str]:
    policy = _runtime_workflow_policy(vars_obj)
    if not isinstance(policy, dict):
        return "missing Gateway-issued workflow policy"
    if not verify_workflow_policy_signature(policy, secret=policy_secret):
        return "invalid Gateway workflow policy signature"
    host_tenant_id = safe_principal_component(policy.get("host_tenant_id"), default="")
    host_runtime_id = safe_principal_component(policy.get("host_runtime_id"), default="")
    expected_tenant = safe_principal_component(expected_tenant_id, default="default")
    expected_runtime = safe_principal_component(expected_runtime_id, default=expected_tenant)
    if host_tenant_id != expected_tenant or host_runtime_id != expected_runtime:
        return "Gateway workflow policy is not valid for this runtime"
    allowed: set[str] = set()
    host_wid = policy.get("host_workflow_id")
    if isinstance(host_wid, str) and host_wid.strip():
        allowed.add(host_wid.strip())
        if ":" in host_wid:
            allowed.add(host_wid.split(":", 1)[0].strip() + ":*")
    for item in list(policy.get("allowed_host_workflow_ids") or []):
        if isinstance(item, str) and item.strip():
            allowed.add(item.strip())
            if ":" in item:
                allowed.add(item.split(":", 1)[0].strip() + ":*")
    wid = str(workflow_id or "").strip()
    allowed_match = wid in allowed
    if ":" in wid:
        prefix = wid.split(":", 1)[0].strip()
        if f"{prefix}:*" in allowed:
            allowed_match = True
    if not allowed_match:
        return f"workflow '{wid}' is not allowed by the Gateway workflow policy"

    parts = _catalog_workflow_parts(workflow_id)
    if parts is None:
        return None
    scope, tenant_id, public_bid, bver = parts
    rec = catalog_store.get_record(scope=scope, tenant_id=tenant_id, bundle_id=public_bid, bundle_version=bver)
    if rec is None:
        return f"Catalog workflow '{public_bid}@{bver}' is not registered"
    status = str((rec or {}).get("status") or "").strip().lower()
    if status != "published":
        return f"Catalog workflow '{public_bid}@{bver}' is {status or 'unavailable'}"
    sha = str(policy.get("sha256") or "").strip()
    if sha and sha != str(rec.get("sha256") or "").strip():
        return f"Catalog workflow '{public_bid}@{bver}' content hash no longer matches the Gateway workflow policy"
    principal = _policy_principal(policy)
    if not principal_can_run_catalog_record(rec, principal):
        return f"Catalog workflow '{public_bid}@{bver}' is no longer allowed for this user"
    return None


def _install_catalog_subworkflow_guard(
    *,
    runtime: Runtime,
    catalog_root_data_dir: Path,
    catalog_tenant_id: str,
    catalog_runtime_id: str,
) -> None:
    original = getattr(runtime, "_handle_start_subworkflow", None)
    if not callable(original):
        return

    store = WorkflowCatalogStore(root_data_dir=catalog_root_data_dir)
    policy_secret = load_or_create_workflow_policy_secret(catalog_root_data_dir)

    def _guarded_start_subworkflow(run: Any, effect: Any, default_next_node: Optional[str]) -> Any:
        workflow_id = None
        try:
            payload = getattr(effect, "payload", None)
            workflow_id = payload.get("workflow_id") if isinstance(payload, dict) else None
        except Exception:
            workflow_id = None
        parts = _catalog_workflow_parts(workflow_id)
        if parts is not None:
            wid = str(workflow_id or "").strip()
            err = _runtime_workflow_policy_error(
                getattr(run, "vars", None),
                workflow_id=wid,
                policy_secret=policy_secret,
                catalog_store=store,
                expected_tenant_id=catalog_tenant_id,
                expected_runtime_id=catalog_runtime_id,
            )
            if err:
                return EffectOutcome.failed(f"Catalog subworkflow '{wid}' is not allowed: {err}")
        # B4: a run pinned across an in-place overwrite starts its sub-workflows from
        # the registry it resolved from (`_PinnableWorkflowRegistry`).
        pins = getattr(runtime, "_gateway_registry_pins", None)
        pinned_registry = pins.get(str(getattr(run, "run_id", "") or "")) if isinstance(pins, dict) else None
        if pinned_registry is None:
            return original(run, effect, default_next_node)
        token = _PINNED_REGISTRY.set(pinned_registry)
        try:
            return original(run, effect, default_next_node)
        finally:
            _PINNED_REGISTRY.reset(token)

    try:
        runtime._handlers[EffectType.START_SUBWORKFLOW] = _guarded_start_subworkflow  # type: ignore[attr-defined]
    except Exception:
        pass


def _namespace_visualflow_raw(
    *,
    raw: Dict[str, Any],
    bundle_id: str,
    flow_id: str,
    id_map: Dict[str, str],
) -> Dict[str, Any]:
    """Return a namespaced copy of VisualFlow JSON, rewriting internal subflow references."""
    fid = str(flow_id or "").strip()
    if not fid:
        raise ValueError("flow_id is required")

    namespaced_id = id_map.get(fid) or _namespace(bundle_id, fid)

    def _maybe_rewrite(v: Any) -> Any:
        if isinstance(v, str):
            s = v.strip()
            if s in id_map:
                return id_map[s]
        return v

    out: Dict[str, Any] = dict(raw)
    out["id"] = namespaced_id

    # Copy/normalize nodes to avoid mutating the original object and to ensure nested dicts
    # are not shared by reference.
    nodes_raw = out.get("nodes")
    if isinstance(nodes_raw, list):
        new_nodes: list[Any] = []
        try:
            from abstractruntime.visualflow_compiler.visual.agent_ids import visual_react_workflow_id
        except Exception:  # pragma: no cover
            visual_react_workflow_id = None  # type: ignore[assignment]

        for n_any in nodes_raw:
            n = n_any if isinstance(n_any, dict) else None
            if n is None:
                new_nodes.append(n_any)
                continue

            n2: Dict[str, Any] = dict(n)
            node_type = n2.get("type")
            type_str = node_type.value if hasattr(node_type, "value") else str(node_type or "")
            data0 = n2.get("data")
            data = dict(data0) if isinstance(data0, dict) else {}

            if type_str == "subflow":
                for key in ("subflowId", "flowId", "workflowId", "workflow_id"):
                    if key in data:
                        data[key] = _maybe_rewrite(data.get(key))

            if type_str == "agent":
                cfg0 = data.get("agentConfig")
                cfg = dict(cfg0) if isinstance(cfg0, dict) else {}
                node_id = str(n2.get("id") or "").strip()
                if node_id and callable(visual_react_workflow_id):
                    cfg["_react_workflow_id"] = visual_react_workflow_id(flow_id=namespaced_id, node_id=node_id)
                if cfg:
                    data["agentConfig"] = cfg

            n2["data"] = data
            new_nodes.append(n2)

        out["nodes"] = new_nodes

    # Shallow-copy edges for consistency (not strictly required).
    edges_raw = out.get("edges")
    if isinstance(edges_raw, list):
        out["edges"] = [dict(e) if isinstance(e, dict) else e for e in edges_raw]

    return out


def _env(name: str, fallback: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is not None and str(v).strip():
        return v
    if fallback:
        v2 = os.getenv(fallback)
        if v2 is not None and str(v2).strip():
            return v2
    return None


def _bool_text(raw: Any) -> Optional[bool]:
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _int_text(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except Exception:
        return None
    return value if value >= 0 else None


def _ensure_runtime_namespace(vars_obj: Dict[str, Any]) -> Dict[str, Any]:
    rt_ns = vars_obj.get("_runtime")
    if not isinstance(rt_ns, dict):
        rt_ns = {}
        vars_obj["_runtime"] = rt_ns
    return rt_ns


def _node_type_from_raw(n: Any) -> str:
    if not isinstance(n, dict):
        return ""
    t = n.get("type")
    return t.value if hasattr(t, "value") else str(t or "")


def _scan_flows_for_llm_defaults(flows_by_id: Dict[str, Dict[str, Any]]) -> Optional[Tuple[str, str]]:
    """Return a best-effort (provider, model) pair from VisualFlow node configs."""
    def _pair_from_mapping(raw: Any) -> Optional[Tuple[str, str]]:
        if not isinstance(raw, dict):
            return None
        provider = raw.get("provider")
        model = raw.get("model")
        if isinstance(provider, str) and provider.strip() and isinstance(model, str) and model.strip():
            return (provider.strip().lower(), model.strip())
        return None

    for raw in (flows_by_id or {}).values():
        nodes = raw.get("nodes")
        if not isinstance(nodes, list):
            continue
        for n in nodes:
            t = _node_type_from_raw(n)
            data = n.get("data") if isinstance(n, dict) else None
            if not isinstance(data, dict):
                data = {}

            if t == "llm_call":
                cfg = data.get("effectConfig")
                pair = _pair_from_mapping(cfg)
                if pair is not None:
                    return pair

            if t == "agent":
                cfg = data.get("agentConfig")
                pair = _pair_from_mapping(cfg)
                if pair is not None:
                    return pair

            pair = _pair_from_mapping(data.get("pinDefaults"))
            if pair is not None:
                return pair

    return None


def _capability_defaults_signature(data_root: Path) -> Optional[tuple]:
    """Cheap fingerprint of the files the capability-defaults payload reads.

    Thin wrapper so the host has ONE spelling of "has the store moved?" and the
    tests have one place to patch. ``None`` means "not file-backed" (a split
    AbstractCore server owns the store).
    """

    return capability_defaults_config_signature(base_dir=data_root)


def _flow_uses_llm(raw: Dict[str, Any]) -> bool:
    nodes = raw.get("nodes")
    if not isinstance(nodes, list):
        return False
    for n in nodes:
        t = _node_type_from_raw(n)
        if t in {"llm_call", "agent"}:
            return True
    return False


# Node types that emit a TOOL_INVOKE effect (one deterministic tool call per
# node — runtime's write_chart/camera pattern, c4207) as opposed to TOOL_CALLS
# (a model-driven batch). A flow using ONLY these still needs the tool
# executor + the TOOL_INVOKE handler; without them it fell to the bare runtime
# and failed at execution with "No effect handler registered for tool_invoke"
# (flow c4316 — the operator's deterministic camera flow: wait_event ->
# camera_open -> camera_capture_photo -> camera_analyze_media, NO llm/agent).
_TOOL_INVOKE_NODE_TYPES = frozenset({
    "tool_invoke",
    "call_tool",
    "camera_open",
    "camera_close",
    "camera_capture_photo",
    "camera_capture_video",
    "camera_analyze_media",
})


def _flow_uses_tool_invoke(raw: Dict[str, Any]) -> bool:
    nodes = raw.get("nodes")
    if not isinstance(nodes, list):
        return False
    for n in nodes:
        if _node_type_from_raw(n) in _TOOL_INVOKE_NODE_TYPES:
            return True
    return False


def _flow_uses_tools(raw: Dict[str, Any]) -> bool:
    nodes = raw.get("nodes")
    if not isinstance(nodes, list):
        return False
    for n in nodes:
        t = _node_type_from_raw(n)
        if t in {"tool_calls", "agent"}:
            return True
    # A deterministic tool-invoke node needs the same tool executor +
    # handlers as a tool_calls batch (flow c4316).
    return _flow_uses_tool_invoke(raw)


def _flow_uses_model_residency(raw: Dict[str, Any]) -> bool:
    nodes = raw.get("nodes")
    if not isinstance(nodes, list):
        return False
    for n in nodes:
        t = _node_type_from_raw(n)
        if t == "model_residency":
            return True
    return False


def _flow_uses_memory_kg(raw: Dict[str, Any]) -> bool:
    nodes = raw.get("nodes")
    if not isinstance(nodes, list):
        return False
    for n in nodes:
        t = _node_type_from_raw(n)
        if t in {"memory_kg_assert", "memory_kg_query", "memory_kg_resolve"}:
            return True
    return False


def _collect_agent_nodes(raw: Dict[str, Any]) -> list[tuple[str, Dict[str, Any]]]:
    nodes = raw.get("nodes")
    if not isinstance(nodes, list):
        return []
    out: list[tuple[str, Dict[str, Any]]] = []
    for n in nodes:
        if _node_type_from_raw(n) != "agent":
            continue
        if not isinstance(n, dict):
            continue
        node_id = str(n.get("id") or "").strip()
        if not node_id:
            continue
        data = n.get("data")
        data = data if isinstance(data, dict) else {}
        cfg0 = data.get("agentConfig")
        cfg = dict(cfg0) if isinstance(cfg0, dict) else {}
        out.append((node_id, cfg))
    return out


def _visual_event_listener_workflow_id(*, flow_id: str, node_id: str) -> str:
    # Local copy of the canonical id scheme (kept simple and deterministic).
    import re

    safe_re = re.compile(r"[^a-zA-Z0-9_-]+")

    def _sanitize(v: str) -> str:
        s = str(v or "").strip()
        if not s:
            return "unknown"
        s = safe_re.sub("_", s)
        return s or "unknown"

    return f"visual_event_listener_{_sanitize(flow_id)}_{_sanitize(node_id)}"


# The registry a run in flight resolved its workflows from, while that run starts a
# sub-workflow (set by the start-subworkflow guard). See `_PinnableWorkflowRegistry`.
_PINNED_REGISTRY: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar("abstractgateway_pinned_registry", default=None)


class _PinnableWorkflowRegistry(WorkflowRegistry):
    """A WorkflowRegistry that answers from a run's PINNED registry first.

    When a version is replaced in place (drafts, `overwrite: true`), a run in flight
    keeps the registry it started with (B4). The runtime looks sub-workflows up with
    `registry.get(workflow_id)` and no run context, so the gateway's start-subworkflow
    guard sets `_PINNED_REGISTRY` to the parent's pinned registry around the call: the
    child starts on the version its parent runs, not on the overwrite. Ids the old
    registry does not know (published since) fall through to this one.
    """

    def get(self, workflow_id: str) -> Optional[WorkflowSpec]:  # type: ignore[override]
        pinned = _PINNED_REGISTRY.get()
        if pinned is not None and pinned is not self:
            spec = pinned.get(workflow_id)
            if spec is not None:
                return spec
        return super().get(workflow_id)


def _file_signature(p: Path) -> Optional[tuple]:
    """(path, mtime_ns, size) — what the compile cache keys a bundle file by."""
    try:
        st = Path(p).stat()
    except OSError:
        return None
    return (str(p), int(st.st_mtime_ns), int(st.st_size))


@dataclass
class _WorkflowCompileCache:
    """Compiled workflows kept across reloads, keyed by the bundle file's signature.

    A publish adds or replaces ONE file; every other bundle keeps its compiled spec
    OBJECTS, which is both why a reload is cheap and how in-flight pinning tells a
    changed workflow from an untouched one (identity, not content).
    """

    # path -> (signature, WorkflowBundle | the Exception that refused it)
    paths: Dict[str, tuple] = field(default_factory=dict)
    # (host bundle id, version, signature) -> compiled entry (specs, flows, needs, derived)
    bundles: Dict[tuple, Dict[str, Any]] = field(default_factory=dict)
    # dynamic flow path -> (signature, WorkflowSpec | None)
    dynamic: Dict[str, tuple] = field(default_factory=dict)
    # the packaged automation controller + send-email action specs
    static_specs: Optional[list] = None


@dataclass
class _CompiledWorkflows:
    """Everything a host serves that does NOT depend on its runtime."""

    bundles: Dict[str, Dict[str, WorkflowBundle]]
    bundle_sources: Dict[str, Dict[str, Dict[str, Any]]]
    skipped_bundles: Dict[str, Dict[str, Dict[str, Any]]]
    latest_versions: Dict[str, str]
    default_bundle_id: Optional[str]
    workflow_registry: WorkflowRegistry
    specs: Dict[str, WorkflowSpec]
    event_listener_specs_by_root: Dict[str, list[str]]
    flows_by_namespaced_id: Dict[str, Dict[str, Any]]
    # What the runtime must be able to serve: "llm" (llm/agent/model-residency
    # nodes), "tools", "memory_kg".
    needs: frozenset
    flow_scanned_llm_defaults: Optional[Tuple[str, str]]
    email_tools_listed: bool
    dynamic_signature: tuple


def _tool_defs_from_specs(specs0: list[dict[str, Any]]) -> list[_GatewayToolSpec]:
    out: list[_GatewayToolSpec] = []
    for s in specs0:
        if not isinstance(s, dict):
            continue
        name = s.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        desc = s.get("description")
        params = s.get("parameters")
        when_to_use = s.get("when_to_use")
        examples = s.get("examples")
        tags = s.get("tags")
        out.append(
            _GatewayToolSpec(
                name=name.strip(),
                description=str(desc or ""),
                parameters=dict(params) if isinstance(params, dict) else {},
                when_to_use=str(when_to_use) if when_to_use is not None else None,
                examples=list(examples) if isinstance(examples, list) else [],
                tags=list(tags) if isinstance(tags, list) else [],
            )
        )
    return out


def _normalize_tool_names(raw_tools: Any) -> list[str]:
    if not isinstance(raw_tools, list):
        return []
    out: list[str] = []
    for t in raw_tools:
        if isinstance(t, str) and t.strip():
            out.append(t.strip())
    return out


def _build_gateway_react_logic(*, email_tools_listed: bool, mcp_registry_dir: Path) -> Any:
    """The ReAct logic every Visual Agent node's derived workflow runs (its tool registry)."""
    try:
        from abstractagent.logic.react import ReActLogic
    except Exception as e:  # pragma: no cover
        raise WorkflowBundleError(
            "Bundle contains Visual Agent nodes, but AbstractAgent is not installed/importable. "
            "Install `abstractagent` to execute Agent nodes."
        ) from e

    try:
        from abstractruntime.integrations.abstractcore.default_tools import list_default_tool_specs
    except Exception as e:  # pragma: no cover
        raise WorkflowBundleError(
            "Visual Agent nodes require AbstractCore tool schemas from the base AbstractRuntime install."
        ) from e

    all_tool_defs = _tool_defs_from_specs(list_default_tool_specs(email_enabled=email_tools_listed))
    # Schema-only builtins (executed as runtime effects by AbstractAgent adapters).
    try:
        from abstractagent.logic.builtins import (  # type: ignore
            ASK_USER_TOOL,
            COMPACT_MEMORY_TOOL,
            DELEGATE_AGENT_TOOL,
            INSPECT_VARS_TOOL,
            READ_SKILL_TOOL,
            RECALL_MEMORY_TOOL,
            REMEMBER_TOOL,
        )

        builtin_defs = [
            ASK_USER_TOOL,
            RECALL_MEMORY_TOOL,
            INSPECT_VARS_TOOL,
            REMEMBER_TOOL,
            COMPACT_MEMORY_TOOL,
            DELEGATE_AGENT_TOOL,
            # read_skill (card 0087): the agent contract keeps this out
            # of DEFAULT tool lists, but the allowlist normalizer
            # prunes names absent from the logic's registry — so the
            # schema must live here for a skills-carrying run's
            # allowlist to keep it. Runs without a skills_block that
            # call it anyway get the executor's honest refusal.
            READ_SKILL_TOOL,
        ]
        seen_names = {t.name for t in all_tool_defs if getattr(t, "name", None)}
        for t in builtin_defs:
            if getattr(t, "name", None) and t.name not in seen_names:
                all_tool_defs.append(t)
                seen_names.add(t.name)
    except Exception:
        pass

    # MCP tools offered to agents join the registry LIVE (mcp_run_tools.py): the logic's
    # `tools` is read at every agent step, so an admin turning a server on or off needs no
    # host rebuild; a run still sees only the names in its own tool list.
    from ..mcp_run_tools import offered_tool_specs

    class _McpAwareReActLogic(ReActLogic):
        @property
        def tools(self) -> list[Any]:  # type: ignore[override]
            static = list(self._tools)
            taken = {getattr(t, "name", None) for t in static}
            extra = [t for t in _tool_defs_from_specs(offered_tool_specs(mcp_registry_dir)) if t.name not in taken]
            return static + extra

    return _McpAwareReActLogic(tools=all_tool_defs)


def _derive_bundle_workflows(
    *,
    flows: Dict[str, Dict[str, Any]],
    specs: Dict[str, WorkflowSpec],
    logic: Any,
) -> tuple[list[WorkflowSpec], list[tuple[str, WorkflowSpec]]]:
    """One bundle's derived workflows: (per-Agent-node ReAct specs, [(root flow id, On Event listener spec)]).

    `logic` is a zero-argument callable returning the shared ReAct logic; it is only
    called when the bundle has an Agent node.
    """
    agent_pairs: list[tuple[str, Dict[str, Any]]] = []
    for flow_id, raw in flows.items():
        for node_id, cfg in _collect_agent_nodes(raw):
            agent_pairs.append((flow_id, {"node_id": node_id, "cfg": cfg}))

    agents: list[WorkflowSpec] = []
    if agent_pairs:
        try:
            from abstractagent.adapters.react_runtime import create_react_workflow
        except Exception as e:  # pragma: no cover
            raise WorkflowBundleError(
                "Bundle contains Visual Agent nodes, but AbstractAgent is not installed/importable. "
                "Install `abstractagent` to execute Agent nodes."
            ) from e
        from abstractruntime.visualflow_compiler.visual.agent_ids import visual_react_workflow_id

        shared_logic = logic()
        for flow_id, meta in agent_pairs:
            node_id = str(meta.get("node_id") or "").strip()
            cfg = meta.get("cfg") if isinstance(meta.get("cfg"), dict) else {}
            cfg2 = dict(cfg) if isinstance(cfg, dict) else {}
            workflow_id_raw = cfg2.get("_react_workflow_id")
            react_workflow_id = (
                workflow_id_raw.strip()
                if isinstance(workflow_id_raw, str) and workflow_id_raw.strip()
                else visual_react_workflow_id(flow_id=flow_id, node_id=node_id)
            )
            tools_selected = _normalize_tool_names(cfg2.get("tools"))
            agents.append(
                create_react_workflow(
                    logic=shared_logic,
                    workflow_id=react_workflow_id,
                    provider=None,
                    model=None,
                    allowed_tools=tools_selected,
                )
            )

    # Custom event listeners ("On Event" nodes) are compiled into dedicated listener workflows.
    listeners: list[tuple[str, WorkflowSpec]] = []
    for flow_id, raw in flows.items():
        nodes = raw.get("nodes")
        if not isinstance(nodes, list):
            continue
        for n in nodes:
            if _node_type_from_raw(n) != "on_event":
                continue
            if not isinstance(n, dict):
                continue
            node_id = str(n.get("id") or "").strip()
            if not node_id:
                continue
            # An event-entry flow already listens in its root run. A
            # second derived listener would handle the same event twice,
            # duplicating messages and potentially tool side effects.
            # Use the compiled entry, including inferred entrypoints;
            # independent On Event branches still need their own runs.
            root_spec = specs.get(flow_id)
            if root_spec is not None and node_id == root_spec.entry_node:
                continue
            listener_wid = _visual_event_listener_workflow_id(flow_id=flow_id, node_id=node_id)

            # Derive a listener workflow with entryNode = on_event node.
            derived: Dict[str, Any] = dict(raw)
            derived["id"] = listener_wid
            derived["entryNode"] = node_id
            try:
                spec = compile_visualflow(derived)
            except Exception as e:
                raise WorkflowBundleError(f"Failed compiling On Event listener '{listener_wid}': {e}") from e
            listeners.append((flow_id, spec))
    return agents, listeners


@dataclass
class WorkflowBundleGatewayHost:
    """Gateway host that starts/ticks runs from WorkflowBundles (no AbstractFlow import).

    Compiles `manifest.flows` (VisualFlow JSON) via AbstractRuntime's VisualFlow compiler
    (single semantics).
    """

    bundles_dir: Path
    data_dir: Path
    dynamic_flows_dir: Path
    framework_bundles_dir: Optional[Path]
    catalog_bundles_dir: Optional[Path]
    catalog_root_data_dir: Optional[Path]
    catalog_tenant_id: str
    catalog_user_id: str
    catalog_runtime_id: str
    catalog_policy_secret: str
    deprecation_store: WorkflowDeprecationStore
    # bundle_id -> bundle_version -> WorkflowBundle
    bundles: Dict[str, Dict[str, WorkflowBundle]]
    # bundle_id -> bundle_version -> source metadata
    bundle_sources: Dict[str, Dict[str, Dict[str, Any]]]
    # bundle_id -> latest bundle_version
    latest_bundle_versions: Dict[str, str]
    runtime: Runtime
    workflow_registry: WorkflowRegistry
    specs: Dict[str, WorkflowSpec]
    event_listener_specs_by_root: Dict[str, list[str]]
    _default_bundle_id: Optional[str]
    # bundle_id -> bundle_version -> why this version did NOT load (min_runtime
    # floor, compile failure, native-loop failure). A skipped version is ABSENT
    # from `bundles`/`specs` BY DESIGN — that part is correct. What was missing
    # is the RECORD: the reason died with a log line, so a version silently
    # stopped existing at every reload and `/bundles/upload` still answered
    # `{"ok": true}` for a bundle nothing could run. Keeping the reason here is
    # what makes the absence honest: the console can show a row that says WHY,
    # and an install can refuse to claim a success it did not achieve.
    skipped_bundles: Dict[str, Dict[str, Dict[str, Any]]] = field(default_factory=dict)
    memory_store: Optional[Any] = None
    memory_store_info: Optional[Dict[str, Any]] = None
    # The flow-scanned provider/model bootstrap pair used at load time. Kept so
    # `refresh_capability_defaults` re-resolves through the IDENTICAL cascade
    # instead of a second, subtly different one.
    _flow_scanned_llm_defaults: Optional[Tuple[str, str]] = None
    # (path, mtime_ns, size) of every capability-defaults config file as of the
    # last time this host published them. Watched so the OTHER entry point --
    # `abstractcore config set-default` / the core console-TUI, which write the
    # same file without going through a Gateway route -- is not silently
    # ignored by a running host. See `refresh_capability_defaults_if_config_changed`.
    _capability_defaults_config_signature: Optional[tuple] = None
    # Whether the agents' tool LISTS carried the email tools when this host was built
    # (`agent_tools_active` for `email_plane`): re-checked at every run start, so a mailbox
    # connected, paused, disconnected or (dis)allowed by an admin after the build never
    # leaves a run without a tool the client lists as enabled (operator report 2026-10-01).
    email_tools_listed: bool = False
    email_plane: Any = field(default=None, repr=False, compare=False)
    _lock: Any = field(default_factory=threading.RLock, repr=False, compare=False)
    # Hooks re-applied to every REBUILT runtime before it is published
    # (a service_reload/full_rebuild swaps self.runtime for a brand-new instance;
    # anything the composition root armed on the old one — entity routing —
    # would silently vanish otherwise: the 2026-07-24 "No effect handler
    # registered for memory_recall after a catalog publish" defect).
    _runtime_rebuild_hooks: Any = field(default_factory=list, repr=False, compare=False)
    # Compiled workflows kept across reloads (publish recompiles only what changed).
    _compile_cache: Any = field(default_factory=lambda: _WorkflowCompileCache(), repr=False, compare=False)
    # What `runtime` was built to serve ("llm", "tools", "memory_kg"); a reload
    # swaps a new registry onto it while the workflows need nothing outside it.
    _runtime_capabilities: frozenset = field(default_factory=frozenset, repr=False, compare=False)
    # run_id -> the spec a run in flight resolved before a swap replaced it (B4).
    _run_spec_pins: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    # run_id -> the whole registry that run resolved from: its sub-workflows (same
    # bundle version, overwritten in place) start from it too. Shared with the
    # runtime's start-subworkflow guard as `runtime._gateway_registry_pins`.
    _run_registry_pins: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    # Serializes reloads (concurrent publishes); NOT held by ticks, starts or reads.
    _reload_lock: Any = field(default_factory=threading.RLock, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._share_pins_with_runtime()

    def _share_pins_with_runtime(self) -> None:
        runtime = self.runtime
        try:
            setattr(runtime, "_gateway_registry_pins", self._run_registry_pins)
            setattr(runtime, "_gateway_spec_pins", self._run_spec_pins)
        except Exception:  # pragma: no cover - a runtime without attributes cannot pin sub-workflows
            return
        # A pinned run resumed by the runtime itself (an EMIT_EVENT effect resuming
        # its listeners looks the target's spec up with `registry.get`) resumes on its
        # pinned spec. Wrapped once per runtime object.
        if getattr(runtime, "_gateway_resume_pinned", False):
            return
        original_resume = getattr(runtime, "resume", None)
        if not callable(original_resume):
            return

        def _resume_pinned(*args: Any, **kwargs: Any) -> Any:
            pins = getattr(runtime, "_gateway_spec_pins", None)
            rid = kwargs.get("run_id")
            pinned = pins.get(str(rid)) if isinstance(pins, dict) and rid is not None else None
            if pinned is not None and "workflow" in kwargs:
                kwargs["workflow"] = pinned
            return original_resume(*args, **kwargs)

        try:
            setattr(runtime, "resume", _resume_pinned)
            setattr(runtime, "_gateway_resume_pinned", True)
        except Exception:  # pragma: no cover
            pass

    def _pin_run(self, run_id: str, spec: Any, registry: Any) -> None:
        self._run_spec_pins[run_id] = spec
        if registry is not None:
            self._run_registry_pins[run_id] = registry

    @staticmethod
    def _dynamic_flow_filename(workflow_id: str) -> str:
        safe_re = re.compile(r"[^a-zA-Z0-9._-]+")
        s = safe_re.sub("_", str(workflow_id or "").strip())
        if not s or s in {".", ".."}:
            s = "workflow"
        return f"{s}.json"

    def _dynamic_flow_path(self, workflow_id: str) -> Path:
        return Path(self.dynamic_flows_dir) / self._dynamic_flow_filename(workflow_id)

    def load_dynamic_visualflow(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        """Load a persisted dynamic VisualFlow JSON from disk (best-effort)."""
        wid = str(workflow_id or "").strip()
        if not wid:
            return None
        p = self._dynamic_flow_path(wid)
        if not p.exists() or not p.is_file():
            return None
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        return raw if isinstance(raw, dict) else None

    def register_dynamic_visualflow(self, raw: Dict[str, Any], *, persist: bool = True) -> str:
        """Register a VisualFlow JSON object as a dynamic workflow (durable in data_dir).

        This is used for gateway-generated wrapper workflows (e.g. scheduled runs).
        """
        if not isinstance(raw, dict):
            raise TypeError("Dynamic VisualFlow must be an object")
        wid = str(raw.get("id") or "").strip()
        if not wid:
            raise ValueError("Dynamic VisualFlow missing required 'id'")

        spec = compile_visualflow(raw)
        # Registered AND persisted under the host lock: a concurrent registry swap
        # (publish) checks the dynamic folder under the same lock, so it either sees
        # this file and compiles it, or runs before the registration exists.
        with self._lock:
            self.workflow_registry.register(spec)
            self.specs[str(spec.workflow_id)] = spec

            if persist:
                try:
                    Path(self.dynamic_flows_dir).mkdir(parents=True, exist_ok=True)
                    p = self._dynamic_flow_path(str(spec.workflow_id))
                    p.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception as e:
                    raise RuntimeError(f"Failed to persist dynamic VisualFlow: {e}") from e

        return str(spec.workflow_id)

    def upsert_dynamic_visualflow(self, raw: Dict[str, Any], *, persist: bool = True) -> str:
        """Register or replace a dynamic VisualFlow JSON object (durable in data_dir).

        This is used for gateway-generated wrapper workflows that must be edited in-place
        (e.g. rescheduling a recurrent job without killing the parent run).
        """
        if not isinstance(raw, dict):
            raise TypeError("Dynamic VisualFlow must be an object")
        wid = str(raw.get("id") or "").strip()
        if not wid:
            raise ValueError("Dynamic VisualFlow missing required 'id'")

        spec = compile_visualflow(raw)
        with self._lock:
            try:
                existing = self.workflow_registry.get(spec.workflow_id)
            except Exception:
                existing = None
            if existing is not None:
                try:
                    self.workflow_registry.unregister(spec.workflow_id)
                except Exception:
                    pass
            try:
                self.workflow_registry.register(spec)
            except Exception as e:
                raise RuntimeError(f"Failed to register workflow '{spec.workflow_id}': {e}") from e

            self.specs[str(spec.workflow_id)] = spec

            if persist:
                try:
                    Path(self.dynamic_flows_dir).mkdir(parents=True, exist_ok=True)
                    p = self._dynamic_flow_path(str(spec.workflow_id))
                    p.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception as e:
                    raise RuntimeError(f"Failed to persist dynamic VisualFlow: {e}") from e

        return str(spec.workflow_id)

    def _try_register_dynamic_workflow_from_disk(self, workflow_id: str) -> Optional[WorkflowSpec]:
        wid = str(workflow_id or "").strip()
        if not wid:
            return None
        p = self._dynamic_flow_path(wid)
        if not p.exists() or not p.is_file():
            return None
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(raw, dict):
            return None
        try:
            spec = compile_visualflow(raw)
        except Exception:
            return None
        try:
            with self._lock:
                self.workflow_registry.register(spec)
                self.specs[str(spec.workflow_id)] = spec
        except Exception:
            return None
        return spec

    @staticmethod
    def _min_runtime_gap(man: Any) -> Optional[str]:
        """The min_runtime ENFORCEMENT gate (flow c5652, claimed c5653):
        a bundle declaring `metadata.min_runtime` REFUSES to load on an
        older serving runtime — the fail-dangerous skew (a pin-expression
        bundle on a non-evaluating runtime inverts control signals: a
        FAILED run reads as SUCCESS) becomes a loud refuse-to-load.

        Refusal fires only on a PROVEN gap: absent metadata = no gate
        (today's bundles unchanged); an unparsable declaration or an
        unresolvable installed version WARNS loudly and loads (a typo must
        not brick a working bundle — the failure direction inverts there).
        Returns the human refusal string, or None to load."""
        meta = getattr(man, "metadata", None)
        declared = ""
        if isinstance(meta, dict):
            declared = str(meta.get("min_runtime") or "").strip()
        if not declared:
            return None
        installed = ""
        try:
            # The CANONICAL compare surface (runtime c5654):
            # abstractruntime.__version__ — a lazy attribute over the
            # installed dist metadata, so the serving truth is one source.
            import abstractruntime as _rt

            installed = str(getattr(_rt, "__version__", "") or "")
        except Exception:
            pass
        if not installed:
            try:
                import importlib.metadata as _im

                installed = _im.version("abstractruntime")
            except Exception:
                logger.warning(
                    "#FALLBACK min_runtime gate: installed abstractruntime version unresolvable — "
                    "bundle declaring min_runtime=%s loads UNVERIFIED",
                    declared,
                )
                return None

        def _cmp(a: str, b: str) -> Optional[bool]:
            """a < b, packaging semantics with a numeric-tuple fallback
            (packaging is not a declared dep — soft import only)."""
            try:
                from packaging.version import Version

                return Version(a) < Version(b)
            except Exception:
                pass
            try:
                ta = tuple(int(x) for x in a.split("."))
                tb = tuple(int(x) for x in b.split("."))
                return ta < tb
            except Exception:
                return None  # unparsable — the caller warns and loads

        older = _cmp(installed, declared)
        if older is None:
            logger.warning(
                "#FALLBACK min_runtime gate: cannot compare declared min_runtime=%r "
                "against installed abstractruntime %s — loading UNVERIFIED (fix the "
                "declaration; a typo must not brick the bundle)",
                declared, installed,
            )
            return None
        if older:
            return (
                f"requires abstractruntime >= {declared} but this gateway serves "
                f"abstractruntime {installed} — refusing to load (the bundle would run "
                "WRONG, not just degraded: features like pin expressions are silently "
                "ignored by older runtimes and can invert control signals). Upgrade "
                "abstractruntime or publish a bundle without the requirement."
            )
        return None

    # ---- Workflow compilation (runtime-free) and runtime construction --------
    #
    # PUBLISH WITHOUT REBUILD (R16.2): a publish/promote/upload changes WORKFLOWS,
    # not the services that run them. Loading is therefore two independent steps:
    #
    # 1. `_compile_workflows` — bundles on disk -> specs + a WorkflowRegistry.
    #    Touches no runtime, no LLM client, no provider, no memory store. Every
    #    compiled bundle is cached by its file signature (path, mtime_ns, size),
    #    so a recompile after a publish compiles the ONE new bundle and reuses
    #    the spec OBJECTS of everything else (identity is what B4's in-flight
    #    pinning compares).
    # 2. `_build_runtime` — the runtime, its LLM client/provider (an in-process
    #    model = its weights and its prompt caches), tool executor and memory
    #    store, sized to what the compiled workflows need.
    #
    # `reload_bundles_from_disk` runs step 1 and swaps the new registry onto the
    # EXISTING runtime. Step 2 runs again only when the workflows now need a
    # capability the running runtime was built without (`_runtime_capabilities`).

    @staticmethod
    def _bundle_paths_from_dir(path: Path) -> list[Path]:
        if path.is_file():
            return [path]
        if path.exists() and path.is_dir():
            return sorted([p for p in path.glob("*.flow") if p.is_file()])
        return []

    @staticmethod
    def _dynamic_dir_signature(dynamic_dir: Path) -> tuple:
        try:
            paths = sorted(p for p in Path(dynamic_dir).glob("*.json") if p.is_file())
        except Exception:
            return ()
        return tuple(sig for sig in (_file_signature(p) for p in paths) if sig is not None)

    @staticmethod
    def _compile_workflows(
        *,
        base: Path,
        framework_base: Optional[Path],
        catalog_base: Optional[Path],
        catalog_root: Path,
        data_root: Path,
        catalog_tenant: str,
        dynamic_dir: Path,
        email_tools_listed: bool,
        cache: Optional["_WorkflowCompileCache"] = None,
    ) -> "_CompiledWorkflows":
        """Bundles on disk -> specs and a fresh WorkflowRegistry. No runtime is touched.

        `cache` (the host's) is read for bundles whose file did not change and
        rewritten with exactly the entries this compile used, so it never grows
        past what is on disk.
        """
        old = cache if cache is not None else _WorkflowCompileCache()
        new_paths: Dict[str, tuple] = {}
        new_entries: Dict[tuple, Dict[str, Any]] = {}
        new_dynamic: Dict[str, tuple] = {}

        bundles_by_id: Dict[str, Dict[str, WorkflowBundle]] = {}
        bundle_sources: Dict[str, Dict[str, Dict[str, Any]]] = {}
        bundle_sigs: Dict[tuple, Optional[tuple]] = {}

        def _open(p: Path) -> WorkflowBundle:
            key = str(p)
            sig = _file_signature(p)
            hit = old.paths.get(key)
            if hit is not None and sig is not None and hit[0] == sig:
                new_paths[key] = hit
                if isinstance(hit[1], Exception):
                    raise hit[1]
                return hit[1]
            try:
                b = open_workflow_bundle(p)
            except Exception as e:
                new_paths[key] = (sig, e)
                raise
            new_paths[key] = (sig, b)
            return b

        def _load_bundle_path(
            p: Path,
            *,
            source_scope: str,
            source_tenant_id: str = "default",
            source_kind: str = "user",
        ) -> None:
            try:
                b = _open(p)
                public_bid = str(getattr(getattr(b, "manifest", None), "bundle_id", "") or "").strip()
                bver = str(getattr(getattr(b, "manifest", None), "bundle_version", "0.0.0") or "0.0.0").strip() or "0.0.0"
                if not public_bid:
                    raise WorkflowBundleError(f"Bundle '{p}' has empty bundle_id")
                bid = (
                    catalog_internal_bundle_id(scope=source_scope, tenant_id=source_tenant_id, bundle_id=public_bid)
                    if source_scope != "private"
                    else public_bid
                )
                versions = bundles_by_id.setdefault(bid, {})
                if bver in versions:
                    logger.warning("Duplicate bundle version '%s@%s' at %s; keeping first", bid, bver, p)
                    return
                versions[bver] = b
                bundle_sigs[(bid, bver)] = (new_paths.get(str(p)) or (None,))[0]
                bundle_sources.setdefault(bid, {})[bver] = {
                    "registry_scope": source_scope,
                    "tenant_id": source_tenant_id,
                    "bundle_id": public_bid,
                    "host_bundle_id": bid,
                    "bundle_version": bver,
                    "path": str(p),
                    "source_kind": source_kind,
                }
            except Exception as e:
                logger.warning("Failed to load bundle %s: %s", p, e)

        private_bundle_ids: set[str] = set()
        for p in WorkflowBundleGatewayHost._bundle_paths_from_dir(base):
            before = set(bundles_by_id.keys())
            _load_bundle_path(p, source_scope="private", source_kind="user")
            private_bundle_ids.update(set(bundles_by_id.keys()) - before)

        if framework_base is not None and framework_base != base:
            for p in WorkflowBundleGatewayHost._bundle_paths_from_dir(framework_base):
                _load_bundle_path(p, source_scope="private", source_kind="framework")

        if catalog_base is not None and catalog_base.exists() and catalog_base.is_dir():
            for p in sorted([p for p in catalog_base.glob("*.flow") if p.is_file()]):
                _load_bundle_path(
                    p,
                    source_scope=CATALOG_SCOPE_TENANT,
                    source_tenant_id=catalog_tenant,
                    source_kind="catalog",
                )

        if not bundles_by_id:
            logger.warning("No bundles found in %s (expected *.flow). Starting gateway with zero loaded bundles.", base)

        default_bundle_id = sorted(private_bundle_ids)[0] if len(private_bundle_ids) == 1 else None
        # NOTE: latest_versions is computed AFTER the spec loop below — the
        # loop DROPS skipped bundles (min_runtime gate / compile failure)
        # from bundles_by_id, and a latest pointer at a dropped version
        # would resurrect a bundle the skip declared absent.

        wf_reg: WorkflowRegistry = _PinnableWorkflowRegistry()
        specs: Dict[str, WorkflowSpec] = {}
        flows_by_namespaced_id: Dict[str, Dict[str, Any]] = {}
        skipped_bundles: Dict[str, Dict[str, Dict[str, Any]]] = {}
        needs: set[str] = set()
        derived_jobs: list[Dict[str, Any]] = []

        def _drop_skipped(bid: str, bver: str, *, reason: str, kind: str) -> None:
            # A skipped bundle must be ABSENT everywhere, not just spec-less
            # (flow's live min_runtime probe read the CATALOG LISTING as the
            # gate's verdict — the specs had correctly never registered, but
            # bundles_by_id still listed the bundle, so the skip looked like
            # a load). Same law for compile-skips.
            #
            # ABSENT, BUT NOT UNACCOUNTED FOR: the reason and the on-disk path
            # are lifted out of `bundle_sources` BEFORE the drop, so a version
            # that stops serving still has a row to show. Without this the file
            # sits on disk forever while every surface reports it as if it had
            # never existed.
            try:
                src0 = ((bundle_sources.get(bid) or {}).get(bver) or {})
                skipped_bundles.setdefault(bid, {})[bver] = {
                    "bundle_id": str(src0.get("bundle_id") or bid),
                    "bundle_version": bver,
                    "path": str(src0.get("path") or ""),
                    "registry_scope": str(src0.get("registry_scope") or "private"),
                    "source_kind": str(src0.get("source_kind") or ""),
                    "skip_kind": str(kind or "unknown"),
                    "reason": str(reason or "").strip(),
                }
            except Exception:
                pass
            try:
                versions0 = bundles_by_id.get(bid) or {}
                versions0.pop(bver, None)
                if not versions0:
                    bundles_by_id.pop(bid, None)
                srcs = bundle_sources.get(bid) or {}
                srcs.pop(bver, None)
                if not srcs:
                    bundle_sources.pop(bid, None)
            except Exception:
                pass

        def _compile_one(bid: str, bver: str, b: WorkflowBundle) -> Dict[str, Any]:
            bundle_ref = _bundle_ref(bid, bver)
            man = b.manifest
            # min_runtime ENFORCEMENT (flow c5652): a declared floor the
            # serving runtime cannot meet refuses THIS bundle loudly —
            # BEFORE compilation (an old runtime may compile the flows
            # fine and still run them wrong; the gate exists exactly for
            # the compiles-but-inverts-signals class).
            _gap = WorkflowBundleGatewayHost._min_runtime_gap(man)
            if _gap is not None:
                return {"skip": ("min_runtime", str(_gap))}

            if declares_native_loop_bundle(man):
                native_specs, native_error = materialize_native_loop_specs(
                    manifest=man,
                    bundle_ref=bundle_ref,
                    namespace=_namespace,
                )
                if native_error is not None:
                    return {"skip": ("native_loop", str(native_error))}
                return {"skip": None, "specs": dict(native_specs), "flows": {}, "needs": frozenset(), "derived": {}}

            if not man.flows:
                raise WorkflowBundleError(f"Bundle '{bid}@{bver}' has no flows (manifest.flows is empty)")

            flow_ids = set(man.flows.keys())
            id_map = {flow_id: _namespace(bundle_ref, flow_id) for flow_id in flow_ids}

            # BOOT RESILIENCE (flow, 2026-07-24): one un-compilable bundle
            # must never wedge the whole control plane. Compile a bundle's
            # flows into a STAGING registry first; on any failure, log a
            # loud warning and SKIP that bundle entirely (it neither
            # registers nor half-registers), then keep loading the rest.
            # The failure is still loud (warning + boot_warnings surface on
            # /api/health) and the bundle is simply absent until fixed —
            # the honest degradation the compile-refusal (c5182) needs so a
            # stale draft using an unknown node type doesn't take the
            # gateway down. Published good bundles keep serving.
            staged_specs: Dict[str, WorkflowSpec] = {}
            staged_flows: Dict[str, Dict[str, Any]] = {}
            for flow_id, rel in man.flows.items():
                raw = b.read_json(rel)
                if not isinstance(raw, dict):
                    return {"skip": ("compile", f"VisualFlow JSON for '{flow_id}' must be an object")}
                namespaced_raw = _namespace_visualflow_raw(
                    raw=raw,
                    bundle_id=bundle_ref,
                    flow_id=flow_id,
                    id_map=id_map,
                )
                nsid = str(namespaced_raw.get("id") or _namespace(bundle_ref, flow_id))
                staged_flows[nsid] = namespaced_raw
                try:
                    spec = compile_visualflow(namespaced_raw)
                except Exception as e:
                    return {"skip": ("compile", f"flow '{flow_id}' failed to compile: {e}")}
                staged_specs[str(spec.workflow_id)] = spec
            bundle_needs: set[str] = set()
            for raw in staged_flows.values():
                if _flow_uses_llm(raw) or _flow_uses_model_residency(raw):
                    bundle_needs.add("llm")
                if _flow_uses_tools(raw):
                    bundle_needs.add("tools")
                if _flow_uses_memory_kg(raw):
                    bundle_needs.add("memory_kg")
            return {"skip": None, "specs": staged_specs, "flows": staged_flows, "needs": frozenset(bundle_needs), "derived": {}}

        for bid, versions in list(bundles_by_id.items()):
            for bver, b in list(versions.items()):
                ckey = (bid, bver, bundle_sigs.get((bid, bver)))
                entry = old.bundles.get(ckey) if ckey[2] is not None else None
                if entry is None:
                    entry = _compile_one(bid, bver, b)
                if ckey[2] is not None:
                    new_entries[ckey] = entry
                skip = entry.get("skip")
                if skip is not None:
                    kind, reason = skip
                    logger.warning(
                        "WorkflowBundleGatewayHost: SKIPPING bundle '%s@%s' — %s "
                        "(the bundle is absent until fixed; other bundles keep serving)",
                        bid, bver, reason,
                    )
                    # absent means absent — listings included
                    _drop_skipped(bid, bver, reason=reason, kind=kind)
                    continue
                # Bundle compiled whole — commit it.
                for wfid, spec in entry["specs"].items():
                    wf_reg.register(spec)
                    specs[wfid] = spec
                flows_by_namespaced_id.update(entry["flows"])
                needs.update(entry["needs"])
                if entry["flows"]:
                    derived_jobs.append(entry)

        # Computed after the skip drops (see the note above): only SERVING
        # bundles get latest pointers.
        latest_versions: Dict[str, str] = {bid: _pick_latest_version(versions) for bid, versions in bundles_by_id.items()}

        # Load dynamic flows persisted in data_dir (e.g. scheduled wrapper flows).
        try:
            for p in sorted(Path(dynamic_dir).glob("*.json")):
                if not p.is_file():
                    continue
                key = str(p)
                sig = _file_signature(p)
                hit = old.dynamic.get(key)
                if hit is not None and sig is not None and hit[0] == sig:
                    new_dynamic[key] = hit
                    spec = hit[1]
                else:
                    spec = None
                    try:
                        raw = json.loads(p.read_text(encoding="utf-8"))
                    except Exception as e:
                        logger.warning("Failed to read dynamic flow %s: %s", p, e)
                        raw = None
                    if isinstance(raw, dict):
                        try:
                            spec = compile_visualflow(raw)
                        except Exception as e:
                            logger.warning("Failed compiling dynamic flow %s: %s", p, e)
                            spec = None
                    new_dynamic[key] = (sig, spec)
                if spec is None:
                    continue
                try:
                    wf_reg.register(spec)
                    specs[str(spec.workflow_id)] = spec
                except Exception as e:
                    logger.warning("Failed registering dynamic flow %s: %s", p, e)
                    continue
        except Exception:
            pass

        # Register derived workflows required by VisualFlow semantics:
        # - per-Agent-node ReAct subworkflows
        # - per-OnEvent-node listener workflows (Blueprint-style)
        # Cached on the bundle's entry per `email_tools_listed` (the agents'
        # tool lists are the only input that moves without the file moving).
        event_listener_specs_by_root: Dict[str, list[str]] = {}
        logic_box: list[Any] = []

        def _logic() -> Any:
            if not logic_box:
                logic_box.append(
                    _build_gateway_react_logic(email_tools_listed=email_tools_listed, mcp_registry_dir=Path(catalog_root or data_root))
                )
            return logic_box[0]

        derived_agent: list[WorkflowSpec] = []
        derived_listener: list[tuple[str, WorkflowSpec]] = []
        for entry in derived_jobs:
            listed_key = bool(email_tools_listed)
            got = entry["derived"].get(listed_key)
            if got is None:
                got = _derive_bundle_workflows(flows=entry["flows"], specs=entry["specs"], logic=_logic)
                entry["derived"] = {listed_key: got}
            agents, listeners = got
            derived_agent.extend(agents)
            derived_listener.extend(listeners)
        for spec in derived_agent:
            wf_reg.register(spec)
            specs[str(spec.workflow_id)] = spec
        for root_flow_id, spec in derived_listener:
            wf_reg.register(spec)
            specs[str(spec.workflow_id)] = spec
            event_listener_specs_by_root.setdefault(root_flow_id, []).append(str(spec.workflow_id))

        # Automations v1 (contracts C5/D): every host serves the shipped
        # automation controller under its PINNED versioned workflow id
        # (`abstractframework.automation-controller@1.0.0:controller`), so the
        # runner ticks controller runs like any run, also after a restart. It
        # is registered as a workflow, not loaded as a bundle, so it never
        # shows in bundle listings. A missing or wrong packaged controller
        # raises here: a gateway without it must not come up half-working.
        # Per-user email (framework backlog 0992): the send-email action
        # workflow (no-model automations). Both are packaged, so compiled once
        # per host and reused across reloads.
        static_specs = old.static_specs
        if static_specs is None:
            from abstractruntime.automations.bundle import register_controller_bundle
            from abstractruntime.email import register_email_action_workflow

            scratch = WorkflowRegistry()
            static_specs = [register_controller_bundle(scratch), register_email_action_workflow(scratch)]
        for spec in static_specs:
            wf_reg.register(spec)
            specs[str(spec.workflow_id)] = spec

        flow_scanned_llm_defaults = _scan_flows_for_llm_defaults(flows_by_namespaced_id) if "llm" in needs else None

        if cache is not None:
            cache.paths = new_paths
            cache.bundles = new_entries
            cache.dynamic = new_dynamic
            cache.static_specs = static_specs

        return _CompiledWorkflows(
            bundles=bundles_by_id,
            bundle_sources=bundle_sources,
            skipped_bundles=skipped_bundles,
            latest_versions=latest_versions,
            default_bundle_id=default_bundle_id,
            workflow_registry=wf_reg,
            specs=specs,
            event_listener_specs_by_root=event_listener_specs_by_root,
            flows_by_namespaced_id=flows_by_namespaced_id,
            needs=frozenset(needs),
            flow_scanned_llm_defaults=flow_scanned_llm_defaults,
            email_tools_listed=bool(email_tools_listed),
            dynamic_signature=WorkflowBundleGatewayHost._dynamic_dir_signature(dynamic_dir),
        )

    @staticmethod
    def _build_runtime(
        *,
        needs: frozenset,
        flow_scanned_llm_defaults: Optional[Tuple[str, str]],
        wf_reg: WorkflowRegistry,
        data_root: Path,
        catalog_root: Path,
        catalog_tenant: str,
        catalog_user: str,
        catalog_runtime: str,
        email_plane: Any,
        run_store: Any,
        ledger_store: Any,
        artifact_store: Any,
    ) -> Tuple[Runtime, Optional[Any], Optional[Dict[str, Any]], frozenset]:
        """Build the runtime the compiled workflows need: (runtime, memory store, its info, capabilities).

        `capabilities` is what this runtime CAN serve ("llm", "tools", "memory_kg"):
        a later reload swaps a new registry onto it as long as the new workflows
        need nothing outside that set.
        """
        needs_llm = "llm" in needs
        needs_tools = "tools" in needs
        needs_memory_kg = "memory_kg" in needs
        capabilities: set[str] = set()

        extra_effect_handlers: Dict[Any, Any] = {}
        memory_store_obj: Optional[Any] = None
        memory_store_info: Optional[Dict[str, Any]] = None
        if needs_memory_kg:
            try:
                from abstractruntime.integrations.abstractmemory.effect_handlers import build_memory_kg_effect_handlers
                from abstractruntime.storage.artifacts import utc_now_iso
            except Exception as e:  # pragma: no cover
                raise WorkflowBundleError(
                    "Bundle uses memory_kg_* nodes but AbstractMemory integration is not available. "
                    "Install/repair with `pip install abstractgateway`."
                ) from e

            embedder = build_gateway_memory_embedder(base_dir=Path(data_root))

            try:
                memory_resolution = open_gateway_memory_store(base_dir=Path(data_root), embedder=embedder)
                memory_store_obj = memory_resolution.store
                memory_store_info = memory_resolution.public_dict()
            except Exception as e:
                raise WorkflowBundleError(
                    "Bundle uses memory_kg_* nodes, but Gateway could not open the configured AbstractMemory store. "
                    f"{e}"
                ) from e
            for warning in list((memory_store_info or {}).get("warnings") or []):
                logger.warning("Gateway memory store warning: %s", warning)

            extra_effect_handlers = build_memory_kg_effect_handlers(store=memory_store_obj, run_store=run_store, now_iso=utc_now_iso)
            capabilities.add("memory_kg")

        # Optional AbstractCore integration for LLM_CALL + TOOL_CALLS + MODEL_RESIDENCY.
        if needs_llm or needs_tools:
            try:
                from abstractruntime.integrations.abstractcore.default_tools import build_default_tool_map
                from abstractruntime.integrations.abstractcore.tool_executor import (
                    AbstractCoreToolExecutor,
                    MappingToolExecutor,
                    PassthroughToolExecutor,
                )
            except Exception as e:  # pragma: no cover
                raise WorkflowBundleError(
                    "This bundle requires AbstractCore-backed LLM/tool/model-residency execution, but AbstractRuntime was installed "
                    "without AbstractCore integration. Install or upgrade `abstractruntime` "
                    "(and ensure `abstractcore` is importable)."
                ) from e

            # Tool execution policy:
            # - approval (default): execute safe tools locally; require explicit approval for dangerous/unknown tools.
            # - passthrough: require explicit approval for *all* tools (then execute in-process on resume).
            # - delegated: do not execute tools; TOOL_CALLS yields a durable JOB wait for external executors.
            # - local/local_all: execute all tools locally (dev-only; unsafe).
            tool_mode = str(_env("ABSTRACTGATEWAY_TOOL_MODE") or "approval").strip().lower()
            # Always build a concrete in-process executor so thin-client approvals can execute tools
            # inside the runtime (no bridge-owned tool execution).
            # The EXECUTOR always knows the email tools: the runtime's own send-email action
            # (user-authored templates) runs through them with agent tools off, and a later
            # connect needs no host rebuild. Every call resolves the account through this
            # plane's resolver, which refuses an unusable account and, for agent/workflow
            # calls, "Agent email tools" not active. Agents' tool LISTS (the compiled
            # agent workflows) carry the email tools only when agent tools are active.
            gateway_tool_map = build_default_tool_map(email_enabled=True)

            # read_skill execution half (card 0087; agent's progressive-
            # disclosure contract needs BOTH halves — the skills_block index
            # AND an executor behind read_skill). Bound to this host's data
            # root so the body comes from the same shelf /skills serves;
            # trust is RE-CHECKED at read time (a blocked skill's body never
            # reaches a model even if a stale block still lists it).
            def _gateway_read_skill(name: str = "", max_chars: int = 16000, **_ignored: Any) -> Dict[str, Any]:
                from ..capability_inventories import read_skill_body

                return read_skill_body(str(name or ""), data_dir=Path(data_root), max_chars=int(max_chars or 16000))

            gateway_tool_map.setdefault("read_skill", _gateway_read_skill)
            # Tools of MCP servers an admin enabled for agents (mcp_run_tools.py): routed by name,
            # UNDER the approval gate below, the registry re-read at every call.
            from ..mcp_run_tools import McpRoutingToolExecutor

            base_executor: Any = McpRoutingToolExecutor(
                MappingToolExecutor(gateway_tool_map), data_dir=Path(catalog_root or data_root)
            )
            if tool_mode in {"local", "local_all"}:
                tool_executor = base_executor
            elif tool_mode in {"approval", "local_approval", "local-approval"}:
                try:
                    from abstractruntime.integrations.abstractcore.tool_executor import ApprovalToolExecutor, ToolApprovalPolicy

                    tool_executor = ApprovalToolExecutor(delegate=base_executor, policy=ToolApprovalPolicy())
                except Exception:
                    tool_executor = base_executor
            elif tool_mode in {"delegated", "delegate", "job"}:
                tool_executor = PassthroughToolExecutor(mode="delegated")
            else:
                # Back-compat: "passthrough" means "approval required for all tools".
                try:
                    from abstractruntime.integrations.abstractcore.tool_executor import ApprovalToolExecutor, ToolApprovalPolicy

                    tool_executor = ApprovalToolExecutor(delegate=base_executor, policy=ToolApprovalPolicy(auto_approve_tools=set()))
                except Exception:
                    tool_executor = PassthroughToolExecutor(mode="approval_required")

            if needs_llm:
                try:
                    from abstractruntime.integrations.abstractcore.factory import create_local_runtime, create_remote_runtime
                except Exception as e:  # pragma: no cover
                    raise WorkflowBundleError(
                        "LLM/model_residency nodes require AbstractRuntime AbstractCore integration. "
                        "Install or upgrade `abstractruntime`."
                    ) from e

                core_server_base_url = _env("ABSTRACTCORE_SERVER_BASE_URL")
                provider: Optional[str] = None
                model: Optional[str] = None
                provider_deferred_error: Optional[Exception] = None
                try:
                    provider, model = resolve_gateway_provider_model(
                        flow_defaults=flow_scanned_llm_defaults,
                        base_dir=Path(data_root),
                        purpose="bundle LLM execution",
                    ).require()
                except ProviderModelConfigError as e:
                    # FRESH-INSTALL DEFERRAL (release gap, delegate order
                    # c5863): a brand-new install has NO provider configured
                    # anywhere — refusing here meant the shipped catalog
                    # never published and first-run users saw an empty
                    # gateway. Loading a bundle does not call a model;
                    # provider/model resolve at RUN time (caller-supplied
                    # _runtime values, console pickers, capability defaults
                    # set later) and a run that still has nothing fails
                    # loudly at the call with core's actionable error. We
                    # keep the original error to re-raise if the deferred
                    # construction is impossible on this runtime version.
                    provider_deferred_error = e
                    logger.warning(
                        "#FALLBACK no default provider/model configured (%s) — loading the "
                        "bundle anyway; runs must carry provider/model or a default must be "
                        "configured before LLM nodes can execute",
                        e,
                    )

                default_route_error: Optional[str] = None
                try:
                    provider_for_runtime, default_profile_kwargs, provider_override = _resolve_gateway_default_endpoint_profile(
                        provider=provider,
                        data_root=data_root,
                        catalog_root=catalog_root,
                    )
                except WorkflowBundleError as exc:
                    # The DEFAULT names an endpoint profile that is gone or
                    # disabled (operator deleted it). Loading a bundle calls no
                    # model, so this must not take the host down: every read
                    # (automations, runs, sessions) failed 500 while it did.
                    # The default stays what the operator configured, unrouted:
                    # a call that uses it fails at the call, naming the
                    # profile, and the console reports it on the text route
                    # (`capability_defaults_route_problems`).
                    default_route_error = str(exc)
                    logger.warning("default text route unavailable, host loads without it: %s", exc)
                    provider_for_runtime, default_profile_kwargs, provider_override = provider, {}, provider
                if core_server_base_url:
                    headers: Dict[str, str] = {}
                    token = core_server_token()
                    if token:
                        headers["Authorization"] = f"Bearer {token.strip()}"
                    remote_model = f"{provider_for_runtime}/{model}" if provider_for_runtime and model else "default"
                    runtime = create_remote_runtime(
                        server_base_url=core_server_base_url,
                        model=remote_model,
                        headers=headers,
                        run_store=run_store,
                        ledger_store=ledger_store,
                        artifact_store=artifact_store,
                        tool_executor=tool_executor,
                        core_config_file=runtime_core_config_file(data_root),
                        capability_defaults=gateway_capability_defaults_payload(base_dir=data_root),
                    )
                    if extra_effect_handlers:
                        handlers = getattr(runtime, "_handlers", None)
                        if isinstance(handlers, dict):
                            handlers.update(dict(extra_effect_handlers))
                else:
                    try:
                        runtime = create_local_runtime(
                            provider=str(provider_for_runtime or ""),
                            model=str(model or ""),
                            llm_kwargs=_with_voice_openai_key(default_profile_kwargs, data_root, catalog_root),
                            run_store=run_store,
                            ledger_store=ledger_store,
                            artifact_store=artifact_store,
                            tool_executor=tool_executor,
                            prompt_cache_export_root_dir=data_root / "prompt_cache_exports",
                            extra_effect_handlers=extra_effect_handlers,
                            core_config_file=runtime_core_config_file(data_root),
                            capability_defaults=gateway_capability_defaults_payload(base_dir=data_root),
                        )
                    except Exception as _construct_err:
                        if provider_deferred_error is not None:
                            # Version tolerance: this runtime cannot build a
                            # client without a provider (older releases construct
                            # the default client eagerly). Keep the original loud
                            # refusal so behavior is never worse than before the
                            # deferral existed.
                            raise WorkflowBundleError(
                                "Bundle contains LLM nodes or local model_residency nodes but no default provider/model is configured. "
                                "Configure the execution-host output.text capability default, provide "
                                "flow defaults, or ensure the flow JSON includes provider/model on at least "
                                "one llm_call/agent node."
                            ) from provider_deferred_error
                        # A default IS configured and the runtime still could
                        # not build it -- an older runtime, an unreachable
                        # provider, weights not downloaded yet. Once the
                        # recommended seed started writing a default into every
                        # fresh install, `provider_deferred_error is None`
                        # became the COMMON case, so re-raising raw here meant a
                        # bare ValueError escaping bundle loading with nothing
                        # to act on. Name the pair and keep the cause.
                        raise WorkflowBundleError(
                            f"Bundle contains LLM nodes but the execution host's configured default "
                            f"({provider_for_runtime or '<unset>'}/{model or '<unset>'}) could not be "
                            f"prepared: {_construct_err}. Configure a different output.text capability "
                            "default, download this model's weights (`abstractcore models status`), or "
                            "put provider/model on the flow's llm_call/agent nodes."
                        ) from _construct_err
                if provider_override:
                    try:
                        setattr(runtime, "_gateway_default_provider_override", provider_override)
                    except Exception:
                        pass
                setattr(runtime, "_gateway_default_route_error", default_route_error)
                _attach_provider_endpoint_profile_resolver(runtime=runtime, data_root=data_root, catalog_root=catalog_root)
                runtime.set_workflow_registry(wf_reg)
                capabilities.update({"llm", "tools"})
            else:
                # Tools-only runtime: avoid constructing an LLM client.
                from abstractruntime.core.models import EffectType
                from abstractruntime.integrations.abstractcore.effect_handlers import (
                    make_tool_calls_handler,
                    make_tool_invoke_handler,
                )

                # BOTH effect types (flow c4316): a deterministic tool-invoke
                # node (camera, call_tool) emits TOOL_INVOKE, not TOOL_CALLS.
                # A camera-only flow with no llm/agent node reaches this
                # tools-only branch and must find its TOOL_INVOKE handler here
                # — the LLM branch registers both via build_effect_handlers,
                # which is why an llm+camera flow masked the gap.
                runtime = Runtime(
                    run_store=run_store,
                    ledger_store=ledger_store,
                    workflow_registry=wf_reg,
                    artifact_store=artifact_store,
                    effect_handlers={
                        EffectType.TOOL_CALLS: make_tool_calls_handler(
                            tools=tool_executor,
                            artifact_store=artifact_store,
                            run_store=run_store,
                        ),
                        EffectType.TOOL_INVOKE: make_tool_invoke_handler(
                            tools=tool_executor,
                            artifact_store=artifact_store,
                            run_store=run_store,
                        ),
                        **extra_effect_handlers,
                    },
                )
                # This runtime has no LLM client, so tools here would not be
                # able to look up "endpoint:..." providers. Attach the lookup
                # function directly to the tool executor.
                _attach_tools_only_endpoint_profile_resolver(
                    tool_executor=tool_executor, data_root=data_root, catalog_root=catalog_root
                )
                try:  # pragma: no cover
                    setter = getattr(runtime, "set_tool_executor_for_resume", None)
                    if callable(setter):
                        setter(tool_executor)
                except Exception:
                    pass
                # Run-scoped persistent shell sessions (backlog 0220) must be reaped on
                # terminal runs even on this tools-only runtime path.
                try:  # pragma: no cover
                    from abstractruntime.integrations.abstractcore.factory import register_shell_session_teardown

                    register_shell_session_teardown(runtime)
                except Exception:
                    pass
                capabilities.add("tools")
        else:
            runtime = Runtime(
                run_store=run_store,
                ledger_store=ledger_store,
                workflow_registry=wf_reg,
                artifact_store=artifact_store,
                effect_handlers=extra_effect_handlers,
            )

        # H4 steer door: every runtime that TICKS runs for this data root
        # drains the root's durable steer sidecar at iteration boundaries
        # (Runtime._drain_steer_messages) — without this attach, steers
        # accepted by the /commands door would queue forever. Per-root
        # sidecar = per-principal steer isolation for free.
        try:
            from ..steering import attach_steer_store

            attach_steer_store(runtime, Path(data_root))
        except Exception:  # pragma: no cover - steering must never block runtime construction
            logger.warning("#FALLBACK could not attach steer sidecar for %s", data_root, exc_info=True)

        # Live token streaming (live_deltas.py): every runtime this host
        # builds publishes its runs' live deltas -- into the in-process hub,
        # or, in a split runner process, into `<data dir>/live/*.deltas.jsonl`
        # for the API process to tail -- and closes a run's live state when it
        # ends. No try/except: a runtime without the seam cannot stream (nor
        # enforce the built-in tool deny, see live_deltas.require_runtime_features),
        # and that must be visible at boot, not a silently frozen reply bubble
        # or a silently open data folder.
        from ..live_deltas import install_live_delta_sink, sweep_finished_live_files

        install_live_delta_sink(runtime, data_dir=data_root, run_store=run_store)
        try:
            sweep_finished_live_files(data_root, run_store)
        except OSError:
            logger.warning("live delta file sweep failed for %s", data_root, exc_info=True)

        # Per-user email (framework backlog 0992): THIS principal's account on
        # the runtime — the durable event inbox, the in-memory credential
        # resolver (this plane's account only) and the occurrence binding.
        # Every runtime build re-wires, so a rebuilt runtime is never left
        # without its account.
        from ..mail.runtime_wiring import wire_runtime_email

        wire_runtime_email(runtime, email_plane)
        # Automations that use their owner's default workspaces: resolved at each occurrence's
        # admission for THIS plane's owner (round 13; never a snapshot frozen at save time).
        from ..automation_workspace_resolver import wire_occurrence_workspaces

        wire_occurrence_workspaces(runtime, data_dir=data_root, root_data_dir=catalog_root, tenant_id=catalog_tenant, user_id=catalog_user)

        _install_catalog_subworkflow_guard(
            runtime=runtime,
            catalog_root_data_dir=catalog_root,
            catalog_tenant_id=catalog_tenant,
            catalog_runtime_id=catalog_runtime,
        )
        return runtime, memory_store_obj, memory_store_info, frozenset(capabilities)

    @staticmethod
    def load_from_dir(
        *,
        bundles_dir: Path,
        data_dir: Path,
        framework_bundles_dir: Optional[Path] = None,
        catalog_bundles_dir: Optional[Path] = None,
        catalog_root_data_dir: Optional[Path] = None,
        catalog_tenant_id: str = "default",
        catalog_user_id: str = "admin",
        catalog_runtime_id: str = "default",
        run_store: Any,
        ledger_store: Any,
        artifact_store: Any,
        compile_cache: Optional["_WorkflowCompileCache"] = None,
    ) -> "WorkflowBundleGatewayHost":
        base = Path(bundles_dir).expanduser().resolve()
        if not base.exists():
            if str(base.name or "").lower().endswith(".flow"):
                raise FileNotFoundError(f"bundles_dir file does not exist: {base}")
            try:
                base.mkdir(parents=True, exist_ok=True)
                logger.warning("bundles_dir did not exist; created %s", base)
            except Exception as e:
                raise FileNotFoundError(f"bundles_dir does not exist and could not be created: {base} ({e})") from e

        data_root = Path(data_dir).expanduser().resolve()
        catalog_root = Path(catalog_root_data_dir).expanduser().resolve() if catalog_root_data_dir is not None else data_root
        catalog_tenant = safe_principal_component(catalog_tenant_id, default="default")
        catalog_runtime = safe_principal_component(catalog_runtime_id, default=catalog_tenant)
        catalog_user = safe_principal_component(catalog_user_id, default="admin")
        catalog_policy_secret = load_or_create_workflow_policy_secret(catalog_root)
        from ..mail.accounts import agent_tools_active
        from ..mail.runtime_wiring import plane_for_host

        email_plane = plane_for_host(data_root=data_root, tenant_id=catalog_tenant, user_id=catalog_user, runtime_id=catalog_runtime)
        # Agent email tools (framework backlog 0992; default OFF): the toolsets carry
        # them only when the administrator made them available, this user's account is
        # connected and allowed, and the user's "Agent email tools" toggle is on. Checked again at execution time
        # by the runtime's credential resolver (runtime_wiring.py), and a toggle
        # change swaps in recompiled agent workflows (routes/email.py).
        email_tools_listed = agent_tools_active(email_plane)

        framework_base = Path(framework_bundles_dir).expanduser().resolve() if framework_bundles_dir is not None else None
        catalog_base = Path(catalog_bundles_dir).expanduser().resolve() if catalog_bundles_dir is not None else None

        dep_store = WorkflowDeprecationStore(path=data_root / "workflow_deprecations.json")
        dynamic_dir = data_root / "dynamic_flows"
        try:
            dynamic_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            # Best-effort: dynamic workflows are optional.
            pass

        cache = compile_cache if compile_cache is not None else _WorkflowCompileCache()
        compiled = WorkflowBundleGatewayHost._compile_workflows(
            base=base,
            framework_base=framework_base,
            catalog_base=catalog_base,
            catalog_root=catalog_root,
            data_root=data_root,
            catalog_tenant=catalog_tenant,
            dynamic_dir=dynamic_dir,
            email_tools_listed=bool(email_tools_listed),
            cache=cache,
        )
        runtime, memory_store_obj, memory_store_info, capabilities = WorkflowBundleGatewayHost._build_runtime(
            needs=compiled.needs,
            flow_scanned_llm_defaults=compiled.flow_scanned_llm_defaults,
            wf_reg=compiled.workflow_registry,
            data_root=data_root,
            catalog_root=catalog_root,
            catalog_tenant=catalog_tenant,
            catalog_user=catalog_user,
            catalog_runtime=catalog_runtime,
            email_plane=email_plane,
            run_store=run_store,
            ledger_store=ledger_store,
            artifact_store=artifact_store,
        )

        return WorkflowBundleGatewayHost(
            bundles_dir=base,
            data_dir=data_root,
            dynamic_flows_dir=dynamic_dir,
            framework_bundles_dir=framework_base,
            catalog_bundles_dir=catalog_base,
            catalog_root_data_dir=catalog_root,
            catalog_tenant_id=catalog_tenant,
            catalog_user_id=catalog_user,
            catalog_runtime_id=catalog_runtime,
            catalog_policy_secret=catalog_policy_secret,
            deprecation_store=dep_store,
            bundles=compiled.bundles,
            bundle_sources=compiled.bundle_sources,
            latest_bundle_versions=compiled.latest_versions,
            skipped_bundles=compiled.skipped_bundles,
            runtime=runtime,
            workflow_registry=compiled.workflow_registry,
            specs=compiled.specs,
            event_listener_specs_by_root=compiled.event_listener_specs_by_root,
            memory_store=memory_store_obj,
            memory_store_info=memory_store_info,
            _default_bundle_id=compiled.default_bundle_id,
            _flow_scanned_llm_defaults=compiled.flow_scanned_llm_defaults,
            # The store as this host just published it; anything newer on disk
            # is an out-of-band write from the core entry point.
            _capability_defaults_config_signature=_capability_defaults_signature(Path(data_root)),
            email_tools_listed=bool(email_tools_listed),
            email_plane=email_plane,
            _compile_cache=cache,
            _runtime_capabilities=capabilities,
        )


    @property
    def run_store(self) -> Any:
        return self.runtime.run_store

    @property
    def ledger_store(self) -> Any:
        return self.runtime.ledger_store

    @property
    def artifact_store(self) -> Any:
        return self.runtime.artifact_store

    def add_runtime_rebuild_hook(self, hook: Any) -> None:
        """Register a callable re-applied to every rebuilt Runtime.

        The composition root (service factory) arms effect handlers on
        `host.runtime` AFTER load_from_dir (entity routing today). A publish
        keeps the runtime (registry swap); a service_reload/full_rebuild swaps
        in a brand-new Runtime, so those arms must be re-applied or the
        reloaded process serves entity runs with no MEMORY_*/DIARY_* handlers.
        Hooks run on the NEW runtime BEFORE it is published (race-free: no
        tick can observe an unarmed runtime)."""
        self._runtime_rebuild_hooks.append(hook)

    def refresh_capability_defaults(self) -> Dict[str, Any]:
        """Re-apply the execution-host capability defaults to the LIVE runtime.

        A DEFAULT IS A DEFAULT: the operator sets it in the console and expects
        the next run to use it. Until this existed, the default provider/model
        was resolved ONCE at `load_from_dir` and baked into the pooled LLM
        client, so a console change was invisible until the process restarted
        or bundles were reloaded. Reproduced live 2026-07-31: after a
        console-path PUT of the text default, the very next unpinned run still
        used the previous provider/model.

        This re-runs the SAME cascade as load (`resolve_gateway_provider_model`
        with the same flow-scanned bootstrap pair, then the same endpoint
        profile resolution) and re-points the pool in place. It touches the
        DEFAULT identity only -- per-call provider/model pins are resolved per
        call and are never clobbered by it. It does not recompile bundles and
        does not disturb in-flight runs.
        """

        with self._lock:
            runtime = self.runtime
            data_root = Path(self.data_dir)
            catalog_root = Path(self.catalog_root_data_dir) if self.catalog_root_data_dir else data_root
            flow_defaults = self._flow_scanned_llm_defaults
            # Stamp the fingerprint BEFORE re-deriving: whatever is on disk now
            # is what this refresh is about to publish, so a write that lands
            # mid-refresh still looks "changed" to the next check.
            self._capability_defaults_config_signature = _capability_defaults_signature(data_root)

        client = getattr(runtime, "_abstractcore_llm_client", None)
        if client is None:
            return {"ok": True, "changed": False, "reason": "runtime has no AbstractCore LLM client"}

        payload = gateway_capability_defaults_payload(base_dir=data_root)

        resolution = resolve_gateway_provider_model(
            flow_defaults=flow_defaults,
            base_dir=data_root,
            purpose="bundle LLM execution",
        )
        provider, model = resolution.provider, resolution.model

        provider_for_runtime: Optional[str] = None
        profile_kwargs: Dict[str, Any] = {}
        provider_override: Optional[str] = None
        if provider:
            try:
                provider_for_runtime, profile_kwargs, provider_override = _resolve_gateway_default_endpoint_profile(
                    provider=provider,
                    data_root=data_root,
                    catalog_root=catalog_root,
                )
            except WorkflowBundleError as exc:
                # A default pointing at a broken/absent endpoint profile must
                # not silently half-apply: keep the live default as it was and
                # tell the caller why.
                return {"ok": False, "changed": False, "error": str(exc)}

        setter = getattr(client, "set_default_provider_model", None)
        if not callable(setter):
            capability_setter = getattr(client, "set_capability_defaults", None)
            if callable(capability_setter):
                return {
                    "ok": True, "changed": bool(capability_setter(payload)),
                    "reason": "capability defaults refreshed; remote model routing remains host-owned",
                }
            return {"ok": True, "changed": False, "reason": "runtime LLM client does not support live default refresh"}

        changed = bool(
            setter(
                provider=provider_for_runtime or "",
                model=model or "",
                llm_kwargs=profile_kwargs if provider else {},
                capability_defaults=payload,
            )
        )
        try:
            # The pool serves `endpoint:<id>` virtual providers under their
            # real family; the runtime records the virtual name for evidence.
            setattr(runtime, "_gateway_default_provider_override", provider_override)
        except Exception:
            pass

        # BOTH TRUTHS, OR NEITHER. `Runtime.start()` seeds
        # `_runtime.provider|model` from RuntimeConfig, and every VisualFlow
        # Agent node whose provider/model is Auto reads THAT -- not the LLM
        # client. Refreshing only the client left agent nodes on the previous
        # default while plain llm_call nodes followed the new one: two
        # different defaults inside one host. The capability probe rides along
        # so the derived tool_support bits describe the model now in force.
        config_changed = False
        runtime_setter = getattr(runtime, "set_default_provider_model", None)
        if callable(runtime_setter):
            capabilities: Optional[Dict[str, Any]] = None
            if changed and (provider_for_runtime or model):
                try:
                    probe = getattr(client, "get_model_capabilities", None)
                    capabilities = probe() if callable(probe) else None
                except Exception as exc:  # noqa: BLE001 - a probe miss must not block the refresh
                    logger.warning("capability probe failed after a default change: %s", exc)
                    capabilities = None
            # The REAL provider family, matching what `create_local_runtime`
            # records at load; the virtual `endpoint:<id>` name stays on
            # `_gateway_default_provider_override`, which start_run prefers.
            config_changed = bool(
                runtime_setter(
                    provider=provider_for_runtime,
                    model=model,
                    model_capabilities=capabilities,
                )
            )

        if changed:
            _attach_provider_endpoint_profile_resolver(runtime=runtime, data_root=data_root, catalog_root=catalog_root)
        return {
            "ok": True,
            "changed": bool(changed or config_changed),
            "client_changed": changed,
            "config_changed": config_changed,
            "provider": provider_override or provider_for_runtime or None,
            "model": model or None,
            "source": resolution.source,
        }

    def refresh_capability_defaults_if_config_changed(self) -> bool:
        """Refresh ONLY when a capability-defaults config file actually moved.

        TWO ENTRY POINTS, ONE STORE -- and the OTHER entry point writes without
        telling us. `abstractcore config set-default <route> --provider ...`
        and AbstractCore's console-TUI edit the same config file the Gateway's
        PUT routes do; those routes push the new payload into the live runtime,
        the CLI cannot. And once a payload has been pushed, the runtime stops
        consulting disk entirely (unconfigured rows travel as an explicit
        `source: "not_configured"`, which short-circuits
        `resolve_capability_default_route`'s config-file fallback for EVERY
        route). So without this the core-side entry point silently did nothing
        to a running Gateway until the next Gateway write or a restart.

        Cost: one `stat` per config file per run -- ~24us measured, no parse.
        The payload is re-derived only when a file changed. Best-effort in the
        strongest sense: this must never be able to fail a run.
        """

        try:
            current = _capability_defaults_signature(Path(self.data_dir))
        except Exception:  # noqa: BLE001 - a stat hiccup must not fail a run
            return False
        if current is None:
            # Split AbstractCore server: no file to watch (see
            # `capability_defaults_config_signature`).
            return False
        with self._lock:
            previous = getattr(self, "_capability_defaults_config_signature", None)
        if previous is not None and current == previous:
            return False
        try:
            result = self.refresh_capability_defaults()
        except Exception as exc:  # noqa: BLE001
            logger.warning("out-of-band capability-defaults refresh failed: %s", exc)
            with self._lock:
                self._capability_defaults_config_signature = current
            return False
        if previous is not None and isinstance(result, dict) and result.get("changed"):
            logger.info(
                "capability defaults changed on disk out-of-band (abstractcore config / console-TUI); "
                "live runtime refreshed to provider=%r model=%r",
                result.get("provider"),
                result.get("model"),
            )
        return bool(isinstance(result, dict) and result.get("changed"))

    def email_tools_current(self) -> bool:
        """True when the agents' tool lists match the email rule NOW (`agent_tools_active`)."""

        plane = getattr(self, "email_plane", None)
        if plane is None:
            return True
        from ..mail.accounts import agent_tools_active

        return bool(agent_tools_active(plane)) == bool(getattr(self, "email_tools_listed", False))

    def ensure_email_tools_current(self) -> bool:
        """Recompile the agents' toolsets when the email rule moved since they were compiled (a
        mailbox connected, paused, disconnected, the user's or an admin's switch) and swap them
        in (a registry swap: the runtime and its models stay). Returns True when it happened. A failing check never blocks a start: the runtime's credential
        resolver still refuses an unusable account at execution time."""

        try:
            if self.email_tools_current():
                return False
        except Exception:  # noqa: BLE001
            return False
        logger.info(
            "agent email tools moved since this host was built (listed=%s): rebuilding the toolsets",
            bool(getattr(self, "email_tools_listed", False)),
        )
        self.reload_bundles_from_disk()
        return True

    def reload_bundles_from_disk(self, *, full: bool = False) -> Dict[str, Any]:
        """Serve what is on disk now — without rebuilding what did not change.

        Three outcomes, reported in `out["reload"]` (`kind`, `duration_ms`, `reason`):

        - ``registry_swap`` (the normal case: publish, promote, upload, an
          email-tools toggle): bundles are recompiled (only the changed files —
          see `_WorkflowCompileCache`) into a NEW WorkflowRegistry that is set on
          the EXISTING runtime. No runtime, LLM client, provider, model weights,
          prompt cache, tool executor or memory store is touched. ``changed`` is
          False when nothing on disk moved (nothing is swapped).
        - ``service_reload``: the new workflows need something this service's
          runtime was built without (`_runtime_capabilities`: the first LLM/Agent
          workflow on a tools-only or plain runtime, the first tool workflow on a
          plain one, the first memory_kg workflow). Only then is this service's
          runtime rebuilt; its in-process models and prompt caches start empty.
        - ``full_rebuild``: only when explicitly asked (`full=True`,
          `POST /bundles/reload {"full": true}`), e.g. after changing the memory
          store configuration on disk.

        Runs already in flight keep the workflow spec they resolved (B4,
        `_pin_in_flight_runs`); new runs get the new one.
        """
        started = time.perf_counter()
        with self._reload_lock:
            if full:
                out = self._rebuild_from_disk(compile_cache=None)
                kind, reason = "full_rebuild", "requested"
            else:
                out, kind, reason = self._swap_or_rebuild()
        duration_ms = int(round((time.perf_counter() - started) * 1000.0))
        out["reload"] = {"kind": kind, "duration_ms": duration_ms, "reason": reason, "changed": bool(out.pop("_changed", True))}
        if kind != "registry_swap":
            logger.warning(
                "workflow reload rebuilt this service's runtime (%s, %s ms): %s — its in-process models and prompt caches start empty",
                kind,
                duration_ms,
                reason,
            )
        else:
            logger.info("workflow reload: registry swapped in %s ms (changed=%s)", duration_ms, out["reload"]["changed"])
        return out

    def _current_email_tools_listed(self) -> bool:
        plane = getattr(self, "email_plane", None)
        if plane is None:
            return bool(getattr(self, "email_tools_listed", False))
        try:
            from ..mail.accounts import agent_tools_active

            return bool(agent_tools_active(plane))
        except Exception:  # noqa: BLE001 - keep the toolsets as built; run start re-checks
            return bool(getattr(self, "email_tools_listed", False))

    def _compile_now(self) -> "_CompiledWorkflows":
        return WorkflowBundleGatewayHost._compile_workflows(
            base=Path(self.bundles_dir),
            framework_base=Path(self.framework_bundles_dir) if self.framework_bundles_dir is not None else None,
            catalog_base=Path(self.catalog_bundles_dir) if self.catalog_bundles_dir is not None else None,
            catalog_root=Path(self.catalog_root_data_dir) if self.catalog_root_data_dir else Path(self.data_dir),
            data_root=Path(self.data_dir),
            catalog_tenant=str(self.catalog_tenant_id or "default"),
            dynamic_dir=Path(self.dynamic_flows_dir),
            email_tools_listed=self._current_email_tools_listed(),
            cache=self._compile_cache,
        )

    def _swap_or_rebuild(self) -> tuple[Dict[str, Any], str, str]:
        # Compile OUTSIDE the host lock: ticks, run starts and reads keep going
        # on the current registry while the new one is built.
        compiled = self._compile_now()
        missing = sorted(set(compiled.needs) - set(self._runtime_capabilities or frozenset()))
        if missing:
            reason = (
                "the workflows now need " + ", ".join(_CAPABILITY_WORDS.get(m, m) for m in missing)
                + ", which this service's runtime was started without"
            )
            return self._rebuild_from_disk(compile_cache=self._compile_cache), "service_reload", reason

        with self._lock:
            if compiled.dynamic_signature != WorkflowBundleGatewayHost._dynamic_dir_signature(Path(self.dynamic_flows_dir)):
                # A dynamic workflow was written while we compiled (scheduled
                # wrappers persist there): recompile under the lock so the swap
                # cannot drop it. Cached, so only that file compiles.
                compiled = self._compile_now()
            old_specs = dict(self.specs or {})
            changed = (
                set(old_specs) != set(compiled.specs)
                or any(compiled.specs.get(k) is not v for k, v in old_specs.items())
                or compiled.skipped_bundles != (self.skipped_bundles or {})
                or compiled.latest_versions != (self.latest_bundle_versions or {})
                or compiled.email_tools_listed != bool(self.email_tools_listed)
            )
            if changed:
                pinned = self._pin_in_flight_runs(old_specs, compiled.specs, self.workflow_registry)
                # THE SWAP: one attribute on the live runtime. Everything the
                # runtime owns — its LLM client, providers (weights + prompt
                # caches), tool executor, handlers, memory store — stays.
                self.runtime.set_workflow_registry(compiled.workflow_registry)
                self.workflow_registry = compiled.workflow_registry
                self.specs = compiled.specs
                self.bundles = compiled.bundles
                self.bundle_sources = compiled.bundle_sources
                self.latest_bundle_versions = compiled.latest_versions
                self.skipped_bundles = compiled.skipped_bundles
                self.event_listener_specs_by_root = compiled.event_listener_specs_by_root
                self._default_bundle_id = compiled.default_bundle_id
                self.email_tools_listed = compiled.email_tools_listed
            else:
                pinned = 0
                self._release_finished_pins()
            defaults_moved = compiled.flow_scanned_llm_defaults != self._flow_scanned_llm_defaults
            self._flow_scanned_llm_defaults = compiled.flow_scanned_llm_defaults
        if defaults_moved and "llm" in (self._runtime_capabilities or frozenset()):
            # The flow-scanned bootstrap pair is the LAST fallback of the default
            # text route (used only when no capability default is configured).
            # Re-point the live client through the same cascade as load, in place.
            try:
                self.refresh_capability_defaults()
            except Exception as exc:  # noqa: BLE001 - a default refresh must not fail a publish
                logger.warning("default refresh after a workflow swap failed: %s", exc)
        out = self._reload_result()
        out["_changed"] = bool(changed)
        if pinned:
            out["pinned_runs"] = pinned
        return out, "registry_swap", ("workflows changed on disk" if changed else "nothing changed on disk")

    def _release_finished_pins(self) -> None:
        """Forget the pins of runs that ended (also done lazily at their next lookup)."""
        pins = self._run_spec_pins
        for rid in list(pins):
            try:
                st = self.run_store.load(rid)
            except Exception:
                st = None
            if st is None or _run_is_terminal(st):
                pins.pop(rid, None)
                self._run_registry_pins.pop(rid, None)

    def _pin_in_flight_runs(
        self, old_specs: Dict[str, WorkflowSpec], new_specs: Dict[str, WorkflowSpec], old_registry: Any = None
    ) -> int:
        """Keep every run in flight on the spec it already resolved (caller holds `_lock`).

        Only workflows whose spec OBJECT changes or disappears matter — a publish of
        a new version adds ids and leaves the old version's objects untouched (the
        compile cache), so the common case queries nothing. An overwritten version
        (drafts, `overwrite: true`) or a removed one pins the runs still on it.
        """
        pins = self._run_spec_pins
        self._release_finished_pins()
        changed_ids = {wid for wid, sp in old_specs.items() if new_specs.get(wid) is not sp}
        if not changed_ids:
            return 0
        from abstractruntime.core.models import RunStatus

        list_runs = getattr(self.run_store, "list_runs", None)
        if not callable(list_runs):
            return 0
        count = 0
        for status in (RunStatus.RUNNING, RunStatus.WAITING):
            try:
                runs = list(list_runs(status=status, limit=100_000) or [])
            except Exception:
                logger.warning("could not list in-flight runs to pin them across a workflow swap", exc_info=True)
                continue
            for run in runs:
                rid = str(getattr(run, "run_id", "") or "")
                wid = str(getattr(run, "workflow_id", "") or "")
                if not rid or rid in pins or wid not in changed_ids:
                    continue
                pins[rid] = old_specs[wid]
                if old_registry is not None:
                    self._run_registry_pins[rid] = old_registry
                count += 1
        return count

    def _rebuild_from_disk(self, *, compile_cache: Optional["_WorkflowCompileCache"]) -> Dict[str, Any]:
        """Rebuild this service's runtime (service_reload / full_rebuild) and swap it in.

        `compile_cache` reuses the compiled specs of unchanged bundles (service_reload);
        None recompiles everything (full_rebuild).
        """
        new_host = WorkflowBundleGatewayHost.load_from_dir(
            bundles_dir=self.bundles_dir,
            data_dir=self.data_dir,
            framework_bundles_dir=self.framework_bundles_dir,
            catalog_bundles_dir=self.catalog_bundles_dir,
            catalog_root_data_dir=self.catalog_root_data_dir,
            catalog_tenant_id=self.catalog_tenant_id,
            catalog_user_id=self.catalog_user_id,
            catalog_runtime_id=self.catalog_runtime_id,
            run_store=self.run_store,
            ledger_store=self.ledger_store,
            artifact_store=self.artifact_store,
            compile_cache=compile_cache,
        )
        # Re-arm the NEW runtime BEFORE the swap publishes it: the factory's
        # post-load arms (entity routing) live on the OLD runtime object and
        # would otherwise vanish with it — the exact defect behind
        # "No effect handler registered for memory_recall" after any catalog
        # publish/reload (2026-07-24). Doing it pre-swap means no tick can
        # ever observe the rebuilt runtime unarmed.
        rearm_warnings: list[str] = []
        for hook in list(self._runtime_rebuild_hooks or []):
            try:
                hook(new_host.runtime)
            except Exception as e:
                logger.exception(
                    "runtime rebuild hook failed; the reloaded runtime may be missing factory-armed effect handlers"
                )
                rearm_warnings.append(f"#FALLBACK runtime rebuild hook failed: {type(e).__name__}: {e}")
        with self._lock:
            old_memory_store = getattr(self, "memory_store", None)
            self._pin_in_flight_runs(dict(self.specs or {}), new_host.specs, self.workflow_registry)
            self.bundles = new_host.bundles
            self.bundle_sources = new_host.bundle_sources
            self.latest_bundle_versions = new_host.latest_bundle_versions
            # Skips are recomputed by the rebuild, so the swap must publish the
            # NEW verdict: a version fixed on disk stops being listed as
            # skipped, and a version that newly fails starts being listed.
            # Keeping the old dict here would make the reason drift from the
            # bundles it explains.
            self.skipped_bundles = new_host.skipped_bundles
            self.runtime = new_host.runtime
            self.workflow_registry = new_host.workflow_registry
            self.specs = new_host.specs
            self.event_listener_specs_by_root = new_host.event_listener_specs_by_root
            self.memory_store = new_host.memory_store
            self.memory_store_info = new_host.memory_store_info
            self._default_bundle_id = new_host._default_bundle_id
            self.framework_bundles_dir = new_host.framework_bundles_dir
            self.deprecation_store = new_host.deprecation_store
            self.catalog_bundles_dir = new_host.catalog_bundles_dir
            self.catalog_root_data_dir = new_host.catalog_root_data_dir
            self.catalog_tenant_id = new_host.catalog_tenant_id
            self.catalog_user_id = new_host.catalog_user_id
            self.catalog_runtime_id = new_host.catalog_runtime_id
            self.catalog_policy_secret = new_host.catalog_policy_secret
            self.email_tools_listed = new_host.email_tools_listed
            self.email_plane = new_host.email_plane
            self._flow_scanned_llm_defaults = new_host._flow_scanned_llm_defaults
            self._compile_cache = new_host._compile_cache
            self._runtime_capabilities = new_host._runtime_capabilities
            self._share_pins_with_runtime()
        try:
            if old_memory_store is not None and old_memory_store is not getattr(self, "memory_store", None):
                close = getattr(old_memory_store, "close", None)
                if callable(close):
                    close()
        except Exception:
            pass
        out = self._reload_result()
        if rearm_warnings:
            out["warnings"] = rearm_warnings
        return out

    def _reload_result(self) -> Dict[str, Any]:
        bundle_ids = sorted([str(k) for k in (self.bundles or {}).keys() if isinstance(k, str)])
        out: Dict[str, Any] = {"ok": True, "bundle_ids": bundle_ids, "count": len(bundle_ids)}
        # A reload that silently drops versions is how a workflow stops
        # existing without anyone being told. Report the skips with the
        # reload's own result so every caller — including publish/upload —
        # can see what did NOT survive the reload it just triggered.
        skipped = self.skipped_bundle_rows()
        if skipped:
            out["skipped"] = skipped
            out["skipped_count"] = len(skipped)
        return out

    def skipped_bundle_rows(self) -> list[Dict[str, Any]]:
        """Flat, sorted view of the versions this host refused to serve.

        One row per (bundle_id, bundle_version) with the reason. Callers use it
        to explain an absence instead of showing nothing.
        """
        rows: list[Dict[str, Any]] = []
        for _bid, versions in (self.skipped_bundles or {}).items():
            if not isinstance(versions, dict):
                continue
            for _bver, rec in versions.items():
                if isinstance(rec, dict):
                    rows.append(dict(rec))
        rows.sort(key=lambda r: (str(r.get("bundle_id") or ""), str(r.get("bundle_version") or "")))
        return rows

    def bundle_version_skip_reason(self, bundle_id: str, bundle_version: str) -> Optional[Dict[str, Any]]:
        """The skip record for one exact version, or None when it loaded."""
        rec = ((self.skipped_bundles or {}).get(str(bundle_id or "")) or {}).get(str(bundle_version or ""))
        return dict(rec) if isinstance(rec, dict) else None

    def _seed_session_history_strict(
        self,
        *,
        vars0: Dict[str, Any],
        rt_ns: Dict[str, Any],
        session_id: str,
        attribution: Dict[str, Any],
    ) -> None:
        """Strict seeding for automation / discussion sessions: no fallback."""
        from abstractruntime.session_history import session_chat_messages

        ctx0 = vars0.get("context")
        if ctx0 is not None and not isinstance(ctx0, dict):
            raise ValueError("client context must be an object in an automation or discussion session")
        existing = ctx0.get("messages") if isinstance(ctx0, dict) else None
        if isinstance(existing, list) and existing:
            raise ValueError("an automation or discussion session is seeded by the gateway; do not send context.messages")
        messages = session_chat_messages(
            run_store=self.runtime.run_store,
            ledger_store=self.runtime.ledger_store,
            artifact_store=self.runtime.artifact_store,
            session_id=session_id,
            automation_id=attribution.get("automation_id") if attribution.get("kind") == "automation" else None,
            strict=True,
        )
        self._normalize_seeded_context(vars0, ctx0, messages=messages)
        rt_ns["session_history"] = {
            "seeded": len(messages),
            **messages.report,
            "strict": True,
            "session_kind": attribution.get("kind"),
        }

    def _seed_session_history(
        self,
        *,
        vars0: Dict[str, Any],
        rt_ns: Dict[str, Any],
        session_id: str,
    ) -> None:
        """Seed `vars0.context.messages` from the session's durable prior turns.

        Server-side half of the durable session replay contract (agora
        `durable-sessions` v1). Explicit client-provided messages always win;
        any failure records a labeled `_runtime.session_history` note and the
        run starts unseeded — replay is a quality-of-answer feature, never a
        start blocker.

        EXCEPT automation and discussion sessions (automations contract C3,
        amendment 3): their history is read STRICTLY and a failure refuses the
        start (`SessionHistoryError`) — a discussion turn without its seed, or
        an automation turn without its prior turns, would answer from the
        wrong context without saying so.
        """
        from abstractruntime.core.run_attribution import session_attribution

        attribution = session_attribution(self.runtime.run_store, session_id)
        if attribution is not None and attribution.get("kind") in ("discussion", "automation"):
            self._seed_session_history_strict(vars0=vars0, rt_ns=rt_ns, session_id=session_id, attribution=attribution)
            return
        try:
            ctx0 = vars0.get("context")
            if ctx0 is not None and not isinstance(ctx0, dict):
                # A client sent a non-dict context: replacing it with a seeded
                # dict would stomp whatever the client meant (audit #10).
                rt_ns["session_history"] = {
                    "seeded": 0,
                    "skipped": "client context is not an object",
                }
                return
            existing = ctx0.get("messages") if isinstance(ctx0, dict) else None
            if isinstance(existing, list) and existing:
                # The client's transcript wins; keep the window receipt the
                # /runs/start door wrote for it (source "client_context").
                prior = rt_ns.get("session_history")
                receipt = dict(prior) if isinstance(prior, dict) and prior.get("source") == "client_context" else {}
                rt_ns["session_history"] = {
                    **receipt,
                    "seeded": 0,
                    "skipped": "client context.messages present",
                }
                return

            # THE history window (operator ruling 2026-09-28; ADR-0026): the
            # most recent `HISTORY_REPLAY_MAX_TOKENS` (50k) tokens of whole
            # turns, owned by abstractruntime.session_history. The retired
            # message-count / char caps are not honored; a client still
            # sending them is told so in the run record, never silently.
            retired = [k for k in _RETIRED_SESSION_HISTORY_INPUTS if k in vars0]
            if retired:
                logger.warning(
                    "session history: ignoring retired input(s) %s for session %s "
                    "(history replay is the most recent 50k tokens of whole turns)",
                    ", ".join(retired),
                    session_id,
                )

            from abstractruntime.session_history import session_chat_messages

            # artifact_store deliberately omitted (runtime review A1): the
            # seed read pays run loads + at most a ledger fallback per
            # answerless run — never artifact listings.
            messages = session_chat_messages(
                run_store=self.runtime.run_store,
                ledger_store=self.runtime.ledger_store,
                session_id=session_id,
            )
            # Normalize context.messages to a list even when the seed is
            # empty: turn classification treats a messages LIST as "chat",
            # and without it the session's first turn would classify "run"
            # and be hidden by the chat-preference filter on later reads
            # (audit #5).
            self._normalize_seeded_context(vars0, ctx0, messages=messages)
            note: Dict[str, Any] = {"seeded": len(messages), **messages.report}
            if retired:
                note["ignored_inputs"] = retired
            rt_ns["session_history"] = note
        except Exception as e:  # noqa: BLE001 - degrade, never block the start
            logger.warning(
                "#FALLBACK: session history seed failed for session %s: %s",
                session_id,
                e,
            )
            rt_ns["session_history"] = {
                "seeded": 0,
                "error": f"#FALLBACK: session history seed failed: {e}",
            }

    @staticmethod
    def _normalize_seeded_context(
        vars0: Dict[str, Any],
        ctx0: Optional[Dict[str, Any]],
        *,
        messages: list,
    ) -> None:
        if not isinstance(ctx0, dict):
            ctx0 = {}
            vars0["context"] = ctx0
        ctx0["messages"] = list(messages)

    @staticmethod
    def _normalize_agent_loop_input(vars0: Dict[str, Any]) -> None:
        """Map thin-client ``input_data.prompt`` into native-loop ``context.task``."""
        prompt = vars0.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return
        if isinstance(vars0.get("task"), str) and str(vars0.get("task") or "").strip():
            return
        ctx = vars0.get("context")
        if ctx is not None and not isinstance(ctx, dict):
            # A client-owned non-object context is never replaced (the same
            # rule the session-history seed follows).
            return
        if ctx is None:
            ctx = {}
            vars0["context"] = ctx
        if str(ctx.get("task") or "").strip():
            return
        ctx["task"] = prompt

    def _complete_workflow_selection(self, vars0: Dict[str, Any], workflow_id: str) -> None:
        """Fill `workflow_selection` (how this run's workflow was chosen) with
        what the host resolved, when the caller left the identity open
        (`{"source": "client"}`): {workflow_id, bundle_id, bundle_version,
        flow_id, registry_scope, name}. A selection that already names its
        workflow (a gateway default, a catalog start) is kept as written."""
        sel = vars0.get("workflow_selection")
        if not isinstance(sel, dict) or sel.get("workflow_id"):
            return
        sel = dict(sel)
        wid = str(workflow_id or "")
        prefix, sep, inner = wid.partition(":")
        bid, ver = _split_bundle_ref(prefix) if sep else ("", None)
        bundle = ((self.bundles.get(bid) or {}).get(ver or "") if bid else None)
        name = None
        if bundle is not None:
            for ep in list(getattr(getattr(bundle, "manifest", None), "entrypoints", None) or []):
                if str(getattr(ep, "flow_id", "") or "") == inner:
                    name = str(getattr(ep, "name", "") or "") or inner
        meta = ((self.bundle_sources.get(bid) or {}).get(ver or "") if bid and isinstance(self.bundle_sources, dict) else None) or {}
        sel.update(
            {
                "workflow_id": wid,
                "bundle_id": bid or None,
                "bundle_version": ver,
                "flow_id": inner if sep else wid,
                "registry_scope": (str(meta.get("registry_scope") or "private") if bundle is not None else None),
                "name": name,
            }
        )
        sel.setdefault("interface", None)
        vars0["workflow_selection"] = sel

    def start_run(
        self,
        *,
        flow_id: str,
        input_data: Dict[str, Any],
        actor_id: str = "gateway",
        bundle_id: Optional[str] = None,
        bundle_version: Optional[str] = None,
        session_id: Optional[str] = None,
        interface: Optional[str] = None,
        read_only_mounts: Sequence[str] = (),
    ) -> str:
        # The agents' tool lists must say the truth about the email tools at THIS start.
        self.ensure_email_tools_current()
        # flow_id "@default": the gateway default workflow for `interface`
        # (agents.default_workflow), for in-process callers (the Telegram
        # bridge) — the same resolution as POST /runs/start, recorded in the
        # run as workflow_selection.source "gateway_default". Unavailable ->
        # DefaultWorkflowUnavailable, never another workflow.
        if str(flow_id or "").strip() == "@default":
            from ..agent_defaults import (
                DefaultWorkflowUnavailable,
                Unavailable,
                host_entrypoint_index,
                resolve_default_agent_workflow,
                unavailable_detail,
            )
            from ..users import gateway_data_dir_from_env

            if bundle_id or bundle_version:
                raise ValueError("flow_id '@default' chooses the workflow itself; do not pass bundle_id/bundle_version")
            iface = str(interface or "").strip()
            if not iface:
                raise ValueError("flow_id '@default' needs an interface (e.g. abstractcode.agent.v1)")
            res = resolve_default_agent_workflow(iface, index=host_entrypoint_index(self), data_dir=gateway_data_dir_from_env())
            if isinstance(res, Unavailable):
                raise DefaultWorkflowUnavailable(res, unavailable_detail(res))
            if res.registry_scope != "private":
                raise DefaultWorkflowUnavailable(
                    res,
                    f"the gateway default workflow for {iface} is the catalog workflow {res.workflow_id}; "
                    "this caller starts workflows of the gateway's own registry only",
                )
            input_data = {**dict(input_data or {}), "workflow_selection": {**res.resolved_dict(), "source": "gateway_default", "interface": iface}}
            flow_id, bundle_id, bundle_version = res.flow_id, res.bundle_id, res.bundle_version
        # A DEFAULT IS A DEFAULT, WHICHEVER ENTRY POINT SET IT. Gateway writes
        # push themselves into the live runtime; a `abstractcore config
        # set-default` / console-TUI write cannot. One `stat` here (no parse)
        # makes the core entry point effective on the very next run instead of
        # at the next Gateway write or restart. Never load-bearing.
        try:
            self.refresh_capability_defaults_if_config_changed()
        except Exception:  # noqa: BLE001 - freshness must never fail a run
            pass
        fid_raw = str(flow_id or "").strip()

        bid_raw = str(bundle_id or "").strip() if isinstance(bundle_id, str) else ""
        bid_base, bid_ver = _split_bundle_ref(bid_raw)

        bver_raw = str(bundle_version or "").strip() if isinstance(bundle_version, str) else ""
        if bid_ver and bver_raw and bid_ver != bver_raw:
            raise ValueError("bundle_version conflicts with bundle_id (bundle_id already includes '@version')")

        # Effective requested version (may still be None; default to latest per bundle_id).
        requested_ver = bver_raw or bid_ver

        def _get_bundle(*, bundle_id2: str, bundle_version2: Optional[str]) -> tuple[str, WorkflowBundle]:
            bid2 = str(bundle_id2 or "").strip()
            if not bid2:
                raise ValueError("bundle_id is required")
            versions = self.bundles.get(bid2)
            if not isinstance(versions, dict) or not versions:
                raise KeyError(f"Bundle '{bid2}' not found")
            ver2 = str(bundle_version2 or "").strip() if isinstance(bundle_version2, str) and str(bundle_version2).strip() else ""
            if not ver2:
                ver2 = str(self.latest_bundle_versions.get(bid2) or "").strip()
            if not ver2:
                raise KeyError(f"Bundle '{bid2}' has no versions loaded")
            bundle2 = versions.get(ver2)
            if bundle2 is None:
                raise KeyError(f"Bundle '{bid2}@{ver2}' not found")
            return (ver2, bundle2)

        # Default entrypoint selection for the common case:
        # start {bundle_id, input_data} without needing flow_id.
        if not fid_raw:
            selected_bundle_id = bid_base or (str(self._default_bundle_id or "").strip() if self._default_bundle_id else "")
            if not selected_bundle_id:
                raise ValueError(
                    "flow_id is required when multiple bundles are loaded; "
                    "provide bundle_id (or pass flow_id as 'bundle:flow')"
                )
            selected_ver, bundle2 = _get_bundle(bundle_id2=selected_bundle_id, bundle_version2=requested_ver)
            entrypoints = list(getattr(bundle2.manifest, "entrypoints", None) or [])
            default_ep = str(getattr(bundle2.manifest, "default_entrypoint", "") or "").strip()
            if len(entrypoints) == 1:
                ep_fid = str(getattr(entrypoints[0], "flow_id", "") or "").strip()
            elif default_ep:
                ep_fid = default_ep
            else:
                raise ValueError(
                    f"Bundle '{selected_bundle_id}@{selected_ver}' has {len(entrypoints)} entrypoints; "
                    "specify flow_id to select which entrypoint to start "
                    "(or set manifest.default_entrypoint)"
                )
            if not ep_fid:
                raise ValueError(f"Bundle '{selected_bundle_id}@{selected_ver}' entrypoint flow_id is empty")
            workflow_id = _namespace(_bundle_ref(selected_bundle_id, selected_ver), ep_fid)
        else:
            # Allow passing fully qualified flow_id:
            # - bundle:flow (defaults to latest version)
            # - bundle@ver:flow (exact)
            if ":" in fid_raw:
                prefix, inner = fid_raw.split(":", 1)
                prefix_base, prefix_ver = _split_bundle_ref(prefix)
                # Dynamic workflows often use ':' in their ids (e.g. scheduled:uuid).
                # Only treat this as a bundle namespace when the prefix matches a loaded bundle id.
                if prefix_base and prefix_base in self.bundles:
                    if bid_base and prefix_base and bid_base != prefix_base:
                        raise ValueError("flow_id bundle prefix does not match bundle_id")
                    if prefix_ver and requested_ver and prefix_ver != requested_ver:
                        raise ValueError("flow_id version does not match bundle_version")
                    selected_bundle_id = prefix_base or bid_base
                    if not selected_bundle_id:
                        raise ValueError("bundle_id is required")
                    selected_ver, _bundle2 = _get_bundle(
                        bundle_id2=selected_bundle_id,
                        bundle_version2=prefix_ver or requested_ver,
                    )
                    workflow_id = _namespace(_bundle_ref(selected_bundle_id, selected_ver), inner.strip())
                else:
                    if bid_base or requested_ver:
                        raise ValueError(
                            f"flow_id '{fid_raw}' is already namespaced, but bundle_id/bundle_version was also provided"
                        )
                    workflow_id = fid_raw
            else:
                selected_bundle_id = bid_base or (str(self._default_bundle_id or "").strip() if self._default_bundle_id else "")
                if not selected_bundle_id:
                    raise ValueError("bundle_id is required when multiple bundles are loaded (or pass flow_id as 'bundle:flow')")
                selected_ver, _bundle2 = _get_bundle(bundle_id2=selected_bundle_id, bundle_version2=requested_ver)
                workflow_id = _namespace(_bundle_ref(selected_bundle_id, selected_ver), fid_raw)

        catalog_parts = _catalog_workflow_parts(workflow_id)
        if catalog_parts is not None:
            scope, tenant_id, public_bid, bver = catalog_parts
            store = WorkflowCatalogStore(root_data_dir=self.catalog_root_data_dir or self.data_dir)
            err = _runtime_workflow_policy_error(
                input_data,
                workflow_id=workflow_id,
                policy_secret=self.catalog_policy_secret or load_or_create_workflow_policy_secret(self.catalog_root_data_dir or self.data_dir),
                catalog_store=store,
                expected_tenant_id=self.catalog_tenant_id,
                expected_runtime_id=self.catalog_runtime_id,
            )
            if err:
                status_tokens = {"deprecated", "blocked", "tombstoned", "unavailable"}
                if any(token in str(err).lower() for token in status_tokens):
                    raise WorkflowDeprecatedError(bundle_id=public_bid, flow_id=fid_raw or "*", record={"reason": err})
                raise PermissionError(f"Catalog workflow '{workflow_id}' is not allowed: {err}")
            rec = store.get_record(scope=scope, tenant_id=tenant_id, bundle_id=public_bid, bundle_version=bver)
            status = str((rec or {}).get("status") or "").strip().lower()
            if rec is None:
                raise PermissionError(f"Catalog workflow '{public_bid}@{bver}' is not registered")
            if status != "published":
                raise WorkflowDeprecatedError(bundle_id=public_bid, flow_id=fid_raw or "*", record={"reason": f"catalog status: {status or 'unavailable'}"})

        # Enforce workflow deprecations (bundle-owned entry workflows).
        # This must live in the host so scheduled child launches are also blocked.
        if ":" in workflow_id:
            prefix, inner = workflow_id.split(":", 1)
            dep_bid, _dep_ver = _split_bundle_ref(prefix)
            dep_flow = inner.strip()
            if dep_bid and dep_flow and dep_bid in self.bundles:
                rec = self.deprecation_store.get_record(bundle_id=dep_bid, flow_id=dep_flow)
                if rec is not None:
                    raise WorkflowDeprecatedError(bundle_id=dep_bid, flow_id=dep_flow, record=rec)

        spec = self.specs.get(workflow_id)
        spec_registry = self.workflow_registry
        if spec is None:
            raise KeyError(f"Workflow '{workflow_id}' not found")
        sid = str(session_id).strip() if isinstance(session_id, str) and session_id.strip() else None
        vars0 = dict(input_data or {})
        self._complete_workflow_selection(vars0, workflow_id)
        # A run started without `tools` gets the start-node default plus the email tools
        # when this user's Agent email tools are active (run_default_tools.py; 0.7.0 E2E F2).
        from ..run_default_tools import apply_default_email_tools

        caller_sent_tools = "tools" in vars0
        apply_default_email_tools(self, workflow_id=workflow_id, vars0=vars0)
        # MCP tools of servers enabled for agents (mcp_run_tools.py): default list, offered-only,
        # initialize preflight (a failing server is skipped with `_runtime.mcp_notes`), never under
        # untrusted input.
        from ..mcp_run_tools import prepare_run_mcp_tools

        prepare_run_mcp_tools(self, workflow_id=workflow_id, vars0=vars0, caller_sent_tools=caller_sent_tools)
        rt_ns = _ensure_runtime_namespace(vars0)

        # Email account binding (framework backlog 0992 B1): a client never
        # chooses the account a run uses nor widens who it may mail unasked —
        # pop both keys, then SET the binding from THIS runtime's account.
        from abstractruntime.email import bind_email_account, strip_client_email_keys

        strip_client_email_keys(vars0)
        bind_email_account(vars0, binding=self.runtime.email_binding)
        rt_ns = _ensure_runtime_namespace(vars0)

        # Gateway-owned deployment settings are handed to Runtime explicitly as
        # JSON-safe run state. Lower packages should not read ABSTRACTGATEWAY_*
        # environment names directly.
        #
        # Prompt caching defaults ON (backlog 0212): the runtime derives a session-scoped
        # cache key (requires a session_id), so reuse cannot cross sessions. Precedence:
        # explicit run-level `_runtime.prompt_cache` (any shape) > ABSTRACTGATEWAY_PROMPT_CACHE
        # env > default enabled.
        if "prompt_cache" not in rt_ns:
            prompt_cache_enabled = _bool_text(_env("ABSTRACTGATEWAY_PROMPT_CACHE"))
            if prompt_cache_enabled is None:
                prompt_cache_enabled = True
            rt_ns["prompt_cache"] = {"enabled": bool(prompt_cache_enabled), "version": 1}

        max_attachment_bytes = _int_text(_env("ABSTRACTGATEWAY_MAX_ATTACHMENT_BYTES"))
        if max_attachment_bytes is not None and "max_attachment_bytes" not in rt_ns:
            rt_ns["max_attachment_bytes"] = int(max_attachment_bytes)

        if "workflow_bundles_dir" not in rt_ns:
            rt_ns["workflow_bundles_dir"] = str(self.bundles_dir)

        # Run-level skills selection (card 0087; flow's c2254 transport
        # ruling): `input_data.skills` = list of skill NAMES, resolved ONCE
        # at start through abstractskill's trust gate (same shelf and gate
        # as /skills and the workforce spawn lane) into agent's named slot
        # `_runtime.skills_block` (byte-stable per run — the cache
        # contract). Held/blocked ride as labeled verdicts in
        # `_runtime.skills_resolution`, never silently absent, never
        # trust-bypassed. An explicit caller tool ceiling is never widened
        # by skills selection; read_skill must be enabled by the caller.
        # Default-allowlist runs already see it via the logic registry.
        raw_skills = vars0.get("skills")
        if isinstance(raw_skills, list) and any(isinstance(s, str) and s.strip() for s in raw_skills):
            if "skills_block" in rt_ns:
                rt_ns.setdefault("skills_resolution", {})
                rt_ns["skills_resolution"]["verdicts"] = list(
                    rt_ns["skills_resolution"].get("verdicts") or []
                ) + ["#FALLBACK input_data.skills ignored: the caller already set _runtime.skills_block"]
            else:
                try:
                    from ..capability_inventories import resolve_run_skills

                    resolution = resolve_run_skills(
                        [str(s) for s in raw_skills if isinstance(s, str)], data_dir=Path(self.data_dir)
                    )
                except Exception as e:  # noqa: BLE001 - a broken shelf must not block the run
                    resolution = {
                        "requested": [str(s) for s in raw_skills if isinstance(s, str)],
                        "active": [],
                        "verdicts": [f"#FALLBACK skills resolution failed: {e}"],
                        "skills_block": None,
                        "resolved_tree_hashes": {},
                    }
                block = resolution.pop("skills_block", None)
                if isinstance(block, str) and block.strip():
                    rt_ns["skills_block"] = block
                rt_ns["skills_resolution"] = resolution

        # Best-effort: seed durable run vars with the gateway runtime defaults so VisualFlow
        # nodes (notably Agent nodes) can inherit provider/model without per-node wiring.
        try:
            cfg = getattr(self.runtime, "config", None)
            default_provider = getattr(self.runtime, "_gateway_default_provider_override", None) or getattr(cfg, "provider", None)
            default_model = getattr(cfg, "model", None)
        except Exception:  # pragma: no cover
            default_provider = None
            default_model = None
        if default_provider or default_model:
            if isinstance(default_provider, str) and default_provider.strip() and not str(rt_ns.get("provider") or "").strip():
                rt_ns["provider"] = default_provider.strip().lower()
            if isinstance(default_model, str) and default_model.strip() and not str(rt_ns.get("model") or "").strip():
                rt_ns["model"] = default_model.strip()

        # Gateway DEFAULT tool grant (tool-tiers grant-mode API, cycle-3):
        # when the caller sent NO tool_policy of its own, the operator's
        # default grant rides in — preset tiers inject the risk-tier ceiling
        # (runtime's auto_approve_max_risk_rank consumer), custom injects the
        # name list. Client policy always wins when present; injection is
        # additive and a broken grant store never blocks a start.
        try:
            from ..tool_grants import inject_default_grant

            # Gateway-ROOT store (adversary F3): under user auth the per-
            # principal data dir is not the control center — the operator's
            # default grant lives at the gateway root (the catalog-root
            # precedent, line ~1637); single-user layouts are identical.
            grant_note = inject_default_grant(Path(self.catalog_root_data_dir or self.data_dir), rt_ns)
            if grant_note:
                rt_ns["tool_grant_note"] = grant_note
        except Exception:  # noqa: BLE001
            pass

        # Registered-user-email identity for the send_email recipient refiner
        # + notifications (laurent c4677 + dm#246 via c4693; runtime's seam
        # ask c4679): the gateway INJECTS the run principal's registered
        # email into run vars. PER-ACCOUNT source of truth (1 account = 1
        # runtime = 1 email): the host's catalog principal names the account
        # record; the settings knob covers only the account-less posture.
        # SET, never setdefault — a client-supplied value here would let a
        # payload widen "self" to an attacker address (the actor-strings
        # class); absent email = key absent (refiner deny-safe: no
        # self-value -> everything asks; notifications simply off).
        # The value is the one the user's email settings show (registered email, else the
        # connected mailbox's own address: mail/accounts.py `self_address`, 0.7.0 E2E F1a).
        try:
            from ..mail.accounts import self_address
            from ..mail.runtime_wiring import plane_for_host

            rt_ns.pop("operator_email", None)
            op_email = self_address(
                plane_for_host(
                    data_root=Path(self.data_dir).expanduser().resolve(),
                    tenant_id=self.catalog_tenant_id,
                    user_id=self.catalog_user_id,
                    runtime_id=self.catalog_runtime_id,
                )
            )
            if isinstance(op_email, str) and op_email:
                rt_ns["operator_email"] = op_email
        except Exception:  # noqa: BLE001 - identity injection is additive
            rt_ns.pop("operator_email", None)

        # "Email me when this run finishes / fails" (framework backlog 0992 C5):
        # `_runtime.notify = {on: [finished|failed], channels: [email]}` is
        # HOST-VALIDATED — unknown values dropped, an empty request removed —
        # and read by the plane's notification collector (mail/notifications.py).
        # The recipient is never client-chosen: it is the user's own address.
        try:
            notify = rt_ns.get("notify")
            if notify is not None:
                on = notify.get("on") if isinstance(notify, dict) else None
                channels = notify.get("channels") if isinstance(notify, dict) else None
                on_ok = sorted({str(x) for x in (on if isinstance(on, (list, tuple)) else []) if str(x) in ("finished", "failed")})
                ch_ok = sorted({str(x) for x in (channels if isinstance(channels, (list, tuple)) else []) if str(x) in ("email",)})
                if on_ok and ch_ok:
                    rt_ns["notify"] = {"on": on_ok, "channels": ch_ok}
                else:
                    rt_ns.pop("notify", None)
        except Exception:  # noqa: BLE001
            rt_ns.pop("notify", None)

        # Durable session conversation replay (agora `durable-sessions` contract v1):
        # when the caller opts in (`input_data.use_session_history`) and the run
        # belongs to a session, seed the run's `context.messages` from the
        # session's prior COMPLETED root runs. The run store is the durable
        # transcript — history is server-owned and matches what thin clients
        # already display from history bundles. Client-provided context.messages
        # always win (never overwritten); read failures degrade to no-seed with
        # a labeled record, never a blocked start.
        # Session isolation for attachments, at the host so EVERY caller gets it (the HTTP door,
        # bridges, entities, automations): a run never attaches an artifact another session owns,
        # whatever the client sent (artifact_scope.py; operator report 2026-10-01). Raises
        # ForeignSessionArtifact (a ValueError) — a refused start, never a silent drop.
        if sid:
            from ..artifact_scope import refuse_foreign_session_artifacts

            refuse_foreign_session_artifacts(
                input_data=vars0,
                session_id=sid,
                artifact_store=getattr(self.runtime, "artifact_store", None),
                run_store=getattr(self.runtime, "run_store", None),
            )

        if sid and _bool_text(vars0.get("use_session_history")) is True:
            self._seed_session_history(vars0=vars0, rt_ns=rt_ns, session_id=sid)

        self._normalize_agent_loop_input(vars0)

        # Every run the gateway starts (HTTP routes, bridges, entity summons,
        # scheduled wrappers whose children inherit it) gets a workspace and
        # the host's built-in tool deny rule, here and nowhere else
        # (run_workspace_guard.py). No try/except: a run that cannot be
        # confined must not start.
        from ..run_workspace_guard import guard_run_vars

        guard_run_vars(
            vars0,
            data_dir=self.data_dir,
            root_data_dir=self.catalog_root_data_dir or self.data_dir,
            session_id=sid,
            tenant_id=str(self.catalog_tenant_id or ""),
            user_id=str(self.catalog_user_id or ""),
            read_only_mounts=read_only_mounts,
        )

        run_id = str(self.runtime.start(workflow=spec, vars=vars0, actor_id=actor_id, session_id=sid))
        # B4, the start/swap race: a reload that replaced this workflow between the
        # lookup above and the start would have listed in-flight runs before this one
        # existed. Checked under the swap's lock, so one of the two always pins it.
        with self._lock:
            if self.specs.get(workflow_id) is not spec:
                self._pin_run(run_id, spec, spec_registry)

        # Default session_id to the root run_id for durable session-scoped behavior
        # (matches VisualSessionRunner semantics).
        effective_session_id = sid
        if effective_session_id is None:
            try:
                state = self.runtime.get_state(run_id)
                if not getattr(state, "session_id", None):
                    state.session_id = run_id  # type: ignore[attr-defined]
                    self.runtime.run_store.save(state)
                effective_session_id = str(getattr(state, "session_id", None) or run_id).strip() or run_id
            except Exception:
                effective_session_id = run_id

        # Persist a workflow snapshot for reproducible replay (best-effort).
        try:
            if ":" in workflow_id:
                prefix, inner = workflow_id.split(":", 1)
                bid_base, bid_ver2 = _split_bundle_ref(prefix)
                if bid_base and bid_ver2:
                    versions = self.bundles.get(bid_base)
                    bundle2 = versions.get(bid_ver2) if isinstance(versions, dict) else None
                    if bundle2 is not None:
                        bundle_ref = _bundle_ref(bid_base, bid_ver2)
                        man = bundle2.manifest
                        flow_ids = set(man.flows.keys()) if isinstance(getattr(man, "flows", None), dict) else set()
                        id_map = {fid: _namespace(bundle_ref, fid) for fid in flow_ids if isinstance(fid, str) and fid.strip()}
                        rel = man.flow_path_for(inner) if hasattr(man, "flow_path_for") else None
                        raw = bundle2.read_json(rel) if isinstance(rel, str) and rel.strip() else None
                        if isinstance(raw, dict):
                            namespaced_raw = _namespace_visualflow_raw(
                                raw=raw,
                                bundle_id=bundle_ref,
                                flow_id=inner,
                                id_map=id_map,
                            )
                            snapshot = {
                                "kind": "visualflow_json",
                                "bundle_ref": bundle_ref,
                                "flow_id": str(inner),
                                "visualflow": namespaced_raw,
                            }
                            persist_workflow_snapshot(
                                run_store=self.run_store,
                                artifact_store=self.artifact_store,
                                run_id=str(run_id),
                                workflow_id=str(workflow_id),
                                snapshot=snapshot,
                                format="visualflow_json",
                            )
        except Exception:
            pass

        # Start session-scoped event listener workflows (best-effort).
        listener_vars: Dict[str, Any] = {}
        try:
            rt_seed = vars0.get("_runtime")
            if isinstance(rt_seed, dict) and rt_seed:
                listener_vars["_runtime"] = dict(rt_seed)
            limits_seed = vars0.get("_limits")
            if isinstance(limits_seed, dict) and limits_seed:
                listener_vars["_limits"] = dict(limits_seed)
        except Exception:
            listener_vars = {}

        listener_ids = self.event_listener_specs_by_root.get(workflow_id) or []
        for wid in listener_ids:
            listener_spec = self.specs.get(wid)
            if listener_spec is None:
                continue
            try:
                child_run_id = self.runtime.start(
                    workflow=listener_spec,
                    vars=dict(listener_vars),
                    session_id=effective_session_id,
                    parent_run_id=run_id,
                    actor_id=actor_id,
                )
                # Advance to the first WAIT_EVENT.
                self.runtime.tick(workflow=listener_spec, run_id=child_run_id, max_steps=10)
            except Exception:
                continue

        return run_id

    def runtime_and_workflow_for_run(self, run_id: str) -> tuple[Runtime, Any]:
        run = self.run_store.load(str(run_id))
        if run is None:
            raise KeyError(f"Run '{run_id}' not found")
        workflow_id = getattr(run, "workflow_id", None)
        if not isinstance(workflow_id, str) or not workflow_id:
            raise ValueError(f"Run '{run_id}' missing workflow_id")
        # B4: a run that was in flight when a reload replaced its workflow keeps
        # the spec it resolved (`_pin_in_flight_runs`); new runs get the new one.
        pinned = self._run_spec_pins.get(str(run_id))
        if pinned is None and not _run_is_terminal(run):
            # A child of a pinned run (a sub-workflow it started) runs from the
            # parent's registry: the version the parent started on, also when that
            # version was overwritten in place since.
            parent_reg = self._run_registry_pins.get(str(getattr(run, "parent_run_id", "") or ""))
            parent_spec = parent_reg.get(workflow_id) if parent_reg is not None else None
            if parent_spec is not None:
                self._pin_run(str(run_id), parent_spec, parent_reg)
                pinned = parent_spec
        if pinned is not None:
            if not _run_is_terminal(run):
                return (self.runtime, pinned)
            self._run_spec_pins.pop(str(run_id), None)
            self._run_registry_pins.pop(str(run_id), None)
        spec = self.specs.get(workflow_id)
        if spec is None and ":" in workflow_id:
            # Backward compatibility: older runs may store workflow_id as "bundle:flow"
            # (without bundle_version). Best-effort map to the latest loaded version.
            prefix, fid = workflow_id.split(":", 1)
            prefix_base, prefix_ver = _split_bundle_ref(prefix)
            if prefix_base and not prefix_ver:
                latest = str(self.latest_bundle_versions.get(prefix_base) or "").strip()
                if latest:
                    workflow_id2 = _namespace(_bundle_ref(prefix_base, latest), fid.strip())
                    spec = self.specs.get(workflow_id2)
        if spec is None:
            spec = self._try_register_dynamic_workflow_from_disk(workflow_id)
        if spec is None:
            raise KeyError(f"Workflow '{workflow_id}' not registered")
        return (self.runtime, spec)
