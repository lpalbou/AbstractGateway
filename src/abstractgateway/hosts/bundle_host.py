from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

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
from ..provider_connections import configured_provider_request_kwargs
from ..provider_defaults import ProviderModelConfigError, resolve_gateway_provider_model
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
        return provider_s, configured_provider_request_kwargs(
            provider_s,
            current_base_dir=data_root,
            root_base_dir=catalog_root,
        ), None

    llm_kwargs: Dict[str, Any] = {}
    if profile.base_url:
        llm_kwargs["base_url"] = profile.base_url
    if profile.api_key:
        llm_kwargs["api_key"] = profile.api_key
    return profile.provider_family, llm_kwargs, profile.virtual_provider_id


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
        return original(run, effect, default_next_node)

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
    _lock: Any = field(default_factory=threading.RLock, repr=False, compare=False)
    # Hooks re-applied to every REBUILT runtime before it is published
    # (reload_bundles_from_disk swaps self.runtime for a brand-new instance;
    # anything the composition root armed on the old one — entity routing —
    # would silently vanish otherwise: the 2026-07-24 "No effect handler
    # registered for memory_recall after a catalog publish" defect).
    _runtime_rebuild_hooks: Any = field(default_factory=list, repr=False, compare=False)

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

        bundles_by_id: Dict[str, Dict[str, WorkflowBundle]] = {}
        bundle_sources: Dict[str, Dict[str, Dict[str, Any]]] = {}
        data_root = Path(data_dir).expanduser().resolve()
        catalog_root = Path(catalog_root_data_dir).expanduser().resolve() if catalog_root_data_dir is not None else data_root
        catalog_tenant = safe_principal_component(catalog_tenant_id, default="default")
        catalog_runtime = safe_principal_component(catalog_runtime_id, default=catalog_tenant)
        catalog_user = safe_principal_component(catalog_user_id, default="admin")
        catalog_policy_secret = load_or_create_workflow_policy_secret(catalog_root)

        def _bundle_paths_from_dir(path: Path) -> list[Path]:
            if path.is_file():
                return [path]
            if path.exists() and path.is_dir():
                return sorted([p for p in path.glob("*.flow") if p.is_file()])
            return []

        def _load_bundle_path(
            p: Path,
            *,
            source_scope: str,
            source_tenant_id: str = "default",
            source_kind: str = "user",
        ) -> None:
            try:
                b = open_workflow_bundle(p)
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
        private_paths = _bundle_paths_from_dir(base)
        for p in private_paths:
            before = set(bundles_by_id.keys())
            _load_bundle_path(p, source_scope="private", source_kind="user")
            private_bundle_ids.update(set(bundles_by_id.keys()) - before)

        framework_base = Path(framework_bundles_dir).expanduser().resolve() if framework_bundles_dir is not None else None
        if framework_base is not None and framework_base != base:
            framework_paths = _bundle_paths_from_dir(framework_base)
            if framework_paths:
                for p in framework_paths:
                    _load_bundle_path(p, source_scope="private", source_kind="framework")

        catalog_base = Path(catalog_bundles_dir).expanduser().resolve() if catalog_bundles_dir is not None else None
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

        dep_store = WorkflowDeprecationStore(path=data_root / "workflow_deprecations.json")
        dynamic_dir = data_root / "dynamic_flows"
        try:
            dynamic_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            # Best-effort: dynamic workflows are optional.
            pass

        # Build runtime + registry and register all workflow specs.
        wf_reg: WorkflowRegistry = WorkflowRegistry()
        specs: Dict[str, WorkflowSpec] = {}
        flows_by_namespaced_id: Dict[str, Dict[str, Any]] = {}

        skipped_bundles: Dict[str, Dict[str, Dict[str, Any]]] = {}

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

        for bid, versions in list(bundles_by_id.items()):
            for bver, b in list(versions.items()):
                bundle_ref = _bundle_ref(bid, bver)
                man = b.manifest
                # min_runtime ENFORCEMENT (flow c5652): a declared floor the
                # serving runtime cannot meet refuses THIS bundle loudly —
                # BEFORE compilation (an old runtime may compile the flows
                # fine and still run them wrong; the gate exists exactly for
                # the compiles-but-inverts-signals class).
                _gap = WorkflowBundleGatewayHost._min_runtime_gap(man)
                if _gap is not None:
                    logger.warning(
                        "WorkflowBundleGatewayHost: SKIPPING bundle '%s@%s' — %s",
                        bid, bver, _gap,
                    )
                    _drop_skipped(bid, bver, reason=str(_gap), kind="min_runtime")
                    continue

                _factory = declares_native_loop_bundle(man)
                if _factory:
                    native_specs, native_error = materialize_native_loop_specs(
                        manifest=man,
                        bundle_ref=bundle_ref,
                        namespace=_namespace,
                    )
                    if native_error is not None:
                        logger.warning(
                            "WorkflowBundleGatewayHost: SKIPPING native-loop bundle '%s@%s' — %s "
                            "(the bundle is absent until fixed; other bundles keep serving)",
                            bid,
                            bver,
                            native_error,
                        )
                        _drop_skipped(bid, bver, reason=str(native_error), kind="native_loop")
                        continue
                    for wfid, spec in native_specs.items():
                        wf_reg.register(spec)
                        specs[wfid] = spec
                    continue

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
                bundle_error: Optional[str] = None
                for flow_id, rel in man.flows.items():
                    raw = b.read_json(rel)
                    if not isinstance(raw, dict):
                        bundle_error = f"VisualFlow JSON for '{flow_id}' must be an object"
                        break
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
                        bundle_error = f"flow '{flow_id}' failed to compile: {e}"
                        break
                    staged_specs[str(spec.workflow_id)] = spec
                if bundle_error is not None:
                    logger.warning(
                        "WorkflowBundleGatewayHost: SKIPPING bundle '%s@%s' — %s "
                        "(the bundle is absent until fixed; other bundles keep serving)",
                        bid, bver, bundle_error,
                    )
                    # absent means absent — listings included
                    _drop_skipped(bid, bver, reason=str(bundle_error), kind="compile")
                    continue
                # Bundle compiled whole — commit it.
                for wfid, spec in staged_specs.items():
                    wf_reg.register(spec)
                    specs[wfid] = spec
                flows_by_namespaced_id.update(staged_flows)

        # Computed after the skip drops (see the note above): only SERVING
        # bundles get latest pointers.
        latest_versions: Dict[str, str] = {bid: _pick_latest_version(versions) for bid, versions in bundles_by_id.items()}

        # Load dynamic flows persisted in data_dir (e.g. scheduled wrapper flows).
        try:
            for p in sorted(dynamic_dir.glob("*.json")):
                if not p.is_file():
                    continue
                try:
                    raw = json.loads(p.read_text(encoding="utf-8"))
                except Exception as e:
                    logger.warning("Failed to read dynamic flow %s: %s", p, e)
                    continue
                if not isinstance(raw, dict):
                    continue
                try:
                    spec = compile_visualflow(raw)
                except Exception as e:
                    logger.warning("Failed compiling dynamic flow %s: %s", p, e)
                    continue
                try:
                    wf_reg.register(spec)
                    specs[str(spec.workflow_id)] = spec
                except Exception as e:
                    logger.warning("Failed registering dynamic flow %s: %s", p, e)
                    continue
        except Exception:
            pass

        flow_scanned_llm_defaults: Optional[Tuple[str, str]] = None
        needs_llm = any(_flow_uses_llm(raw) for raw in flows_by_namespaced_id.values())
        needs_tools = any(_flow_uses_tools(raw) for raw in flows_by_namespaced_id.values())
        needs_model_residency = any(_flow_uses_model_residency(raw) for raw in flows_by_namespaced_id.values())
        needs_memory_kg = any(_flow_uses_memory_kg(raw) for raw in flows_by_namespaced_id.values())

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

        # Optional AbstractCore integration for LLM_CALL + TOOL_CALLS + MODEL_RESIDENCY.
        if needs_llm or needs_tools or needs_model_residency:
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
            gateway_tool_map = build_default_tool_map()

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
            base_executor: Any = MappingToolExecutor(gateway_tool_map)
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

            if needs_llm or needs_model_residency:
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
                flow_scanned_llm_defaults = _scan_flows_for_llm_defaults(flows_by_namespaced_id)
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

                provider_for_runtime, default_profile_kwargs, provider_override = _resolve_gateway_default_endpoint_profile(
                    provider=provider,
                    data_root=data_root,
                    catalog_root=catalog_root,
                )
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
                            llm_kwargs=default_profile_kwargs or None,
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
                _attach_provider_endpoint_profile_resolver(runtime=runtime, data_root=data_root, catalog_root=catalog_root)
                runtime.set_workflow_registry(wf_reg)
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

        # Register derived workflows required by VisualFlow semantics:
        # - per-Agent-node ReAct subworkflows
        # - per-OnEvent-node listener workflows (Blueprint-style)
        event_listener_specs_by_root: Dict[str, list[str]] = {}

        agent_pairs: list[tuple[str, Dict[str, Any]]] = []
        for flow_id, raw in flows_by_namespaced_id.items():
            for node_id, cfg in _collect_agent_nodes(raw):
                agent_pairs.append((flow_id, {"node_id": node_id, "cfg": cfg}))

        if agent_pairs:
            try:
                from abstractagent.adapters.react_runtime import create_react_workflow
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

            all_tool_defs = _tool_defs_from_specs(list_default_tool_specs())
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

            logic = ReActLogic(tools=all_tool_defs)

            from abstractruntime.visualflow_compiler.visual.agent_ids import visual_react_workflow_id

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
                spec = create_react_workflow(
                    logic=logic,
                    workflow_id=react_workflow_id,
                    provider=None,
                    model=None,
                    allowed_tools=tools_selected,
                )
                wf_reg.register(spec)
                specs[str(spec.workflow_id)] = spec

        # Custom event listeners ("On Event" nodes) are compiled into dedicated listener workflows.
        for flow_id, raw in flows_by_namespaced_id.items():
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
                wf_reg.register(spec)
                specs[str(spec.workflow_id)] = spec
                event_listener_specs_by_root.setdefault(flow_id, []).append(str(spec.workflow_id))

        _install_catalog_subworkflow_guard(
            runtime=runtime,
            catalog_root_data_dir=catalog_root,
            catalog_tenant_id=catalog_tenant,
            catalog_runtime_id=catalog_runtime,
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
            bundles=bundles_by_id,
            bundle_sources=bundle_sources,
            latest_bundle_versions=latest_versions,
            skipped_bundles=skipped_bundles,
            runtime=runtime,
            workflow_registry=wf_reg,
            specs=specs,
            event_listener_specs_by_root=event_listener_specs_by_root,
            memory_store=memory_store_obj,
            memory_store_info=memory_store_info,
            _default_bundle_id=default_bundle_id,
            _flow_scanned_llm_defaults=flow_scanned_llm_defaults,
            # The store as this host just published it; anything newer on disk
            # is an out-of-band write from the core entry point.
            _capability_defaults_config_signature=_capability_defaults_signature(Path(data_root)),
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
        `host.runtime` AFTER load_from_dir (entity routing today). A reload
        swaps in a brand-new Runtime, so those arms must be re-applied or the
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

    def reload_bundles_from_disk(self) -> Dict[str, Any]:
        """Reload bundles/specs from bundles_dir (best-effort, intended for dev).

        Notes:
        - This rebuilds the in-memory registry and swaps host internals in-place so the
          runner can keep using the same host object.
        - Dynamic flows persisted in `data_dir/dynamic_flows` are reloaded as part of
          the rebuild.
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
        try:
            if old_memory_store is not None and old_memory_store is not getattr(self, "memory_store", None):
                close = getattr(old_memory_store, "close", None)
                if callable(close):
                    close()
        except Exception:
            pass
        bundle_ids = sorted([str(k) for k in (self.bundles or {}).keys() if isinstance(k, str)])
        out: Dict[str, Any] = {"ok": True, "bundle_ids": bundle_ids, "count": len(bundle_ids)}
        # A reload that silently drops versions is how a workflow stops
        # existing without anyone being told. Report the skips with the
        # reload's own result so every caller — including publish/upload —
        # can see what did NOT survive the rebuild it just triggered.
        skipped = self.skipped_bundle_rows()
        if skipped:
            out["skipped"] = skipped
            out["skipped_count"] = len(skipped)
        if rearm_warnings:
            out["warnings"] = rearm_warnings
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
        """
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
                rt_ns["session_history"] = {
                    "seeded": 0,
                    "skipped": "client context.messages present",
                }
                return

            limit = _int_text(vars0.get("session_history_max_messages"))
            if limit == 0:
                # Explicit 0 = replay disabled for this run (audit #9); the
                # empty-list normalization below still runs via the shared
                # tail so turn classification stays stable.
                self._normalize_seeded_context(vars0, ctx0, messages=[])
                rt_ns["session_history"] = {
                    "seeded": 0,
                    "skipped": "disabled by session_history_max_messages=0",
                }
                return
            if limit is None:
                limit = _int_text(_env("ABSTRACTGATEWAY_SESSION_HISTORY_MAX_MESSAGES"))
            if limit is None or limit <= 0:
                limit = 40
            limit = max(1, min(200, int(limit)))

            max_chars = _int_text(vars0.get("session_history_max_chars"))
            if max_chars is None or max_chars <= 0:
                max_chars = _int_text(_env("ABSTRACTGATEWAY_SESSION_HISTORY_MAX_CHARS"))
            if max_chars is None or max_chars <= 0:
                max_chars = 24000
            max_chars = max(1000, min(200000, int(max_chars)))

            from abstractruntime.session_history import session_chat_messages

            # artifact_store deliberately omitted (runtime review A1): the
            # seed read pays run loads + at most a ledger fallback per
            # answerless run — never artifact listings.
            messages = session_chat_messages(
                run_store=self.runtime.run_store,
                ledger_store=self.runtime.ledger_store,
                session_id=session_id,
                max_messages=limit,
                max_total_chars=max_chars,
            )
            # Normalize context.messages to a list even when the seed is
            # empty: turn classification treats a messages LIST as "chat",
            # and without it the session's first turn would classify "run"
            # and be hidden by the chat-preference filter on later reads
            # (audit #5).
            self._normalize_seeded_context(vars0, ctx0, messages=messages)
            rt_ns["session_history"] = {
                "seeded": len(messages),
                "max_messages": limit,
                "max_total_chars": max_chars,
            }
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
    ) -> str:
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
        if spec is None:
            raise KeyError(f"Workflow '{workflow_id}' not found")
        sid = str(session_id).strip() if isinstance(session_id, str) and session_id.strip() else None
        vars0 = dict(input_data or {})
        self._complete_workflow_selection(vars0, workflow_id)
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
        try:
            from ..runtime_config import resolve_operator_email

            rt_ns.pop("operator_email", None)
            op_email = resolve_operator_email(
                Path(self.catalog_root_data_dir or self.data_dir),
                tenant_id=self.catalog_tenant_id,
                user_id=self.catalog_user_id,
            ).get("value")
            if isinstance(op_email, str) and op_email:
                rt_ns["operator_email"] = op_email
        except Exception:  # noqa: BLE001 - identity injection is additive
            rt_ns.pop("operator_email", None)

        # Durable session conversation replay (agora `durable-sessions` contract v1):
        # when the caller opts in (`input_data.use_session_history`) and the run
        # belongs to a session, seed the run's `context.messages` from the
        # session's prior COMPLETED root runs. The run store is the durable
        # transcript — history is server-owned and matches what thin clients
        # already display from history bundles. Client-provided context.messages
        # always win (never overwritten); read failures degrade to no-seed with
        # a labeled record, never a blocked start.
        if sid and _bool_text(vars0.get("use_session_history")) is True:
            self._seed_session_history(vars0=vars0, rt_ns=rt_ns, session_id=sid)

        self._normalize_agent_loop_input(vars0)

        run_id = str(self.runtime.start(workflow=spec, vars=vars0, actor_id=actor_id, session_id=sid))

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
