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

from ..capability_defaults import core_server_token
from ..memory_store import build_gateway_memory_embedder, open_gateway_memory_store
from ..provider_endpoint_profiles import ProviderEndpointProfileError, resolve_effective_endpoint_profile
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


def _attach_provider_endpoint_profile_resolver(*, runtime: Runtime, data_root: Path, catalog_root: Path) -> None:
    llm_client = getattr(runtime, "_abstractcore_llm_client", None)
    if llm_client is None:
        return

    def _resolve(provider_id: str) -> Optional[Dict[str, Any]]:
        try:
            profile = resolve_effective_endpoint_profile(provider_id, base_dir=data_root, root_base_dir=catalog_root)
        except ProviderEndpointProfileError:
            return None
        if profile is None or not profile.enabled:
            return None
        return profile.private_resolution()

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
            raise WorkflowBundleError(f"Gateway provider endpoint profile {provider_s!r} is not configured or is disabled.")
        return provider_s, {}, None

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


def _flow_uses_llm(raw: Dict[str, Any]) -> bool:
    nodes = raw.get("nodes")
    if not isinstance(nodes, list):
        return False
    for n in nodes:
        t = _node_type_from_raw(n)
        if t in {"llm_call", "agent"}:
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
    return False


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
    memory_store: Optional[Any] = None
    memory_store_info: Optional[Dict[str, Any]] = None
    _lock: Any = field(default_factory=threading.RLock, repr=False, compare=False)

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
    def load_from_dir(
        *,
        bundles_dir: Path,
        data_dir: Path,
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

        def _load_bundle_path(p: Path, *, source_scope: str, source_tenant_id: str = "default") -> None:
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
                }
            except Exception as e:
                logger.warning("Failed to load bundle %s: %s", p, e)

        private_bundle_ids: set[str] = set()
        private_paths: list[Path]
        if base.is_file():
            private_paths = [base]
        else:
            private_paths = sorted([p for p in base.glob("*.flow") if p.is_file()])
        for p in private_paths:
            before = set(bundles_by_id.keys())
            _load_bundle_path(p, source_scope="private")
            private_bundle_ids.update(set(bundles_by_id.keys()) - before)

        catalog_base = Path(catalog_bundles_dir).expanduser().resolve() if catalog_bundles_dir is not None else None
        if catalog_base is not None and catalog_base.exists() and catalog_base.is_dir():
            for p in sorted([p for p in catalog_base.glob("*.flow") if p.is_file()]):
                _load_bundle_path(
                    p,
                    source_scope=CATALOG_SCOPE_TENANT,
                    source_tenant_id=catalog_tenant,
                )

        if not bundles_by_id:
            logger.warning("No bundles found in %s (expected *.flow). Starting gateway with zero loaded bundles.", base)

        default_bundle_id = sorted(private_bundle_ids)[0] if len(private_bundle_ids) == 1 else None
        latest_versions: Dict[str, str] = {bid: _pick_latest_version(versions) for bid, versions in bundles_by_id.items()}

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

        for bid, versions in bundles_by_id.items():
            for bver, b in versions.items():
                bundle_ref = _bundle_ref(bid, bver)
                man = b.manifest
                if not man.flows:
                    raise WorkflowBundleError(f"Bundle '{bid}@{bver}' has no flows (manifest.flows is empty)")

                flow_ids = set(man.flows.keys())
                id_map = {flow_id: _namespace(bundle_ref, flow_id) for flow_id in flow_ids}

                for flow_id, rel in man.flows.items():
                    raw = b.read_json(rel)
                    if not isinstance(raw, dict):
                        raise WorkflowBundleError(f"VisualFlow JSON for '{bid}@{bver}:{flow_id}' must be an object")
                    namespaced_raw = _namespace_visualflow_raw(
                        raw=raw,
                        bundle_id=bundle_ref,
                        flow_id=flow_id,
                        id_map=id_map,
                    )
                    flows_by_namespaced_id[str(namespaced_raw.get("id") or _namespace(bundle_ref, flow_id))] = namespaced_raw
                    try:
                        spec = compile_visualflow(namespaced_raw)
                    except Exception as e:
                        raise WorkflowBundleError(f"Failed compiling VisualFlow '{bid}@{bver}:{flow_id}': {e}") from e
                    wf_reg.register(spec)
                    specs[str(spec.workflow_id)] = spec

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
            base_executor: Any = MappingToolExecutor(build_default_tool_map())
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
                try:
                    provider, model = resolve_gateway_provider_model(
                        flow_defaults=_scan_flows_for_llm_defaults(flows_by_namespaced_id),
                        purpose="bundle LLM execution",
                    ).require()
                except ProviderModelConfigError as e:
                    if needs_llm or not core_server_base_url:
                        raise WorkflowBundleError(
                            "Bundle contains LLM nodes or local model_residency nodes but no default provider/model is configured. "
                            "Configure the execution-host output.text capability default, provide "
                            "flow defaults, or ensure the flow JSON includes provider/model on at least "
                            "one llm_call/agent node."
                        ) from e

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
                    )
                    if extra_effect_handlers:
                        handlers = getattr(runtime, "_handlers", None)
                        if isinstance(handlers, dict):
                            handlers.update(dict(extra_effect_handlers))
                else:
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
                    )
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
                from abstractruntime.integrations.abstractcore.effect_handlers import make_tool_calls_handler

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
                        **extra_effect_handlers,
                    },
                )
                try:  # pragma: no cover
                    setter = getattr(runtime, "set_tool_executor_for_resume", None)
                    if callable(setter):
                        setter(tool_executor)
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
            runtime=runtime,
            workflow_registry=wf_reg,
            specs=specs,
            event_listener_specs_by_root=event_listener_specs_by_root,
            memory_store=memory_store_obj,
            memory_store_info=memory_store_info,
            _default_bundle_id=default_bundle_id,
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
            catalog_bundles_dir=self.catalog_bundles_dir,
            catalog_root_data_dir=self.catalog_root_data_dir,
            catalog_tenant_id=self.catalog_tenant_id,
            catalog_user_id=self.catalog_user_id,
            catalog_runtime_id=self.catalog_runtime_id,
            run_store=self.run_store,
            ledger_store=self.ledger_store,
            artifact_store=self.artifact_store,
        )
        with self._lock:
            old_memory_store = getattr(self, "memory_store", None)
            self.bundles = new_host.bundles
            self.bundle_sources = new_host.bundle_sources
            self.latest_bundle_versions = new_host.latest_bundle_versions
            self.runtime = new_host.runtime
            self.workflow_registry = new_host.workflow_registry
            self.specs = new_host.specs
            self.event_listener_specs_by_root = new_host.event_listener_specs_by_root
            self.memory_store = new_host.memory_store
            self.memory_store_info = new_host.memory_store_info
            self._default_bundle_id = new_host._default_bundle_id
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
        return {"ok": True, "bundle_ids": bundle_ids, "count": len(bundle_ids)}

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
        rt_ns = _ensure_runtime_namespace(vars0)

        # Gateway-owned deployment settings are handed to Runtime explicitly as
        # JSON-safe run state. Lower packages should not read ABSTRACTGATEWAY_*
        # environment names directly.
        prompt_cache_raw = _env("ABSTRACTGATEWAY_PROMPT_CACHE")
        prompt_cache_enabled = _bool_text(prompt_cache_raw)
        if prompt_cache_enabled is not None and not isinstance(rt_ns.get("prompt_cache"), dict):
            rt_ns["prompt_cache"] = {"enabled": bool(prompt_cache_enabled), "version": 1}

        max_attachment_bytes = _int_text(_env("ABSTRACTGATEWAY_MAX_ATTACHMENT_BYTES"))
        if max_attachment_bytes is not None and "max_attachment_bytes" not in rt_ns:
            rt_ns["max_attachment_bytes"] = int(max_attachment_bytes)

        if "workflow_bundles_dir" not in rt_ns:
            rt_ns["workflow_bundles_dir"] = str(self.bundles_dir)

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
