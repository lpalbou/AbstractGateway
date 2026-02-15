from __future__ import annotations

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from abstractruntime import Runtime, WorkflowRegistry, WorkflowSpec, persist_workflow_snapshot
from abstractruntime.visualflow_compiler import compile_visualflow
from abstractruntime.workflow_bundle import WorkflowBundle, WorkflowBundleError, open_workflow_bundle

from ..workflow_deprecations import WorkflowDeprecatedError, WorkflowDeprecationStore


logger = logging.getLogger(__name__)


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


def _pick_latest_version(versions: Dict[str, WorkflowBundle]) -> str:
    items = [(str(k), v) for k, v in (versions or {}).items() if isinstance(k, str)]
    if not items:
        return "0.0.0"

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


def _node_type_from_raw(n: Any) -> str:
    if not isinstance(n, dict):
        return ""
    t = n.get("type")
    return t.value if hasattr(t, "value") else str(t or "")


def _scan_flows_for_llm_defaults(flows_by_id: Dict[str, Dict[str, Any]]) -> Optional[Tuple[str, str]]:
    """Return a best-effort (provider, model) pair from VisualFlow node configs."""
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
                cfg = cfg if isinstance(cfg, dict) else {}
                provider = cfg.get("provider")
                model = cfg.get("model")
                if isinstance(provider, str) and provider.strip() and isinstance(model, str) and model.strip():
                    return (provider.strip().lower(), model.strip())

            if t == "agent":
                cfg = data.get("agentConfig")
                cfg = cfg if isinstance(cfg, dict) else {}
                provider = cfg.get("provider")
                model = cfg.get("model")
                if isinstance(provider, str) and provider.strip() and isinstance(model, str) and model.strip():
                    return (provider.strip().lower(), model.strip())

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


def _flow_uses_memory_kg(raw: Dict[str, Any]) -> bool:
    nodes = raw.get("nodes")
    if not isinstance(nodes, list):
        return False
    for n in nodes:
        t = _node_type_from_raw(n)
        if t in {"memory_kg_assert", "memory_kg_query"}:
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
    deprecation_store: WorkflowDeprecationStore
    # bundle_id -> bundle_version -> WorkflowBundle
    bundles: Dict[str, Dict[str, WorkflowBundle]]
    # bundle_id -> latest bundle_version
    latest_bundle_versions: Dict[str, str]
    runtime: Runtime
    workflow_registry: WorkflowRegistry
    specs: Dict[str, WorkflowSpec]
    event_listener_specs_by_root: Dict[str, list[str]]
    _default_bundle_id: Optional[str]
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

        bundle_paths: list[Path] = []
        if base.is_file():
            bundle_paths = [base]
        else:
            bundle_paths = sorted([p for p in base.glob("*.flow") if p.is_file()])

        bundles_by_id: Dict[str, Dict[str, WorkflowBundle]] = {}
        for p in bundle_paths:
            try:
                b = open_workflow_bundle(p)
                bid = str(getattr(getattr(b, "manifest", None), "bundle_id", "") or "").strip()
                bver = str(getattr(getattr(b, "manifest", None), "bundle_version", "0.0.0") or "0.0.0").strip() or "0.0.0"
                if not bid:
                    raise WorkflowBundleError(f"Bundle '{p}' has empty bundle_id")
                versions = bundles_by_id.setdefault(bid, {})
                if bver in versions:
                    logger.warning("Duplicate bundle version '%s@%s' at %s; keeping first", bid, bver, p)
                    continue
                versions[bver] = b
            except Exception as e:
                logger.warning("Failed to load bundle %s: %s", p, e)

        if not bundles_by_id:
            logger.warning("No bundles found in %s (expected *.flow). Starting gateway with zero loaded bundles.", base)

        default_bundle_id = next(iter(bundles_by_id.keys())) if len(bundles_by_id) == 1 else None
        latest_versions: Dict[str, str] = {bid: _pick_latest_version(versions) for bid, versions in bundles_by_id.items()}

        data_root = Path(data_dir).expanduser().resolve()
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
        needs_memory_kg = any(_flow_uses_memory_kg(raw) for raw in flows_by_namespaced_id.values())

        extra_effect_handlers: Dict[Any, Any] = {}
        if needs_memory_kg:
            try:
                from abstractmemory import LanceDBTripleStore
                from abstractruntime.integrations.abstractmemory.effect_handlers import build_memory_kg_effect_handlers
                from abstractruntime.storage.artifacts import utc_now_iso
            except Exception as e:  # pragma: no cover
                raise WorkflowBundleError(
                    "Bundle uses memory_kg_* nodes but AbstractMemory integration is not available. "
                    "Install `abstractmemory` (and optionally `abstractmemory[lancedb]`)."
                ) from e

            embedder = None
            try:
                from abstractruntime.integrations.abstractcore.embeddings_client import AbstractCoreEmbeddingsClient
                from abstractgateway.embeddings_config import resolve_embedding_config

                emb_provider, emb_model = resolve_embedding_config(base_dir=Path(data_root))
                emb_client = AbstractCoreEmbeddingsClient(
                    provider=str(emb_provider).strip().lower(),
                    model=str(emb_model).strip(),
                    manager_kwargs={
                        "cache_dir": Path(data_root) / "abstractcore" / "embeddings",
                        # Embeddings must be trustworthy for semantic retrieval; do not return zero vectors on failure.
                        "strict": True,
                    },
                )

                class _Embedder:
                    def __init__(self, client: Any) -> None:
                        self._client = client

                    def embed_texts(self, texts):
                        return self._client.embed_texts(texts).embeddings

                embedder = _Embedder(emb_client)
            except Exception:
                embedder = None

            try:
                store_path = Path(data_root) / "abstractmemory" / "kg"
                store_path.parent.mkdir(parents=True, exist_ok=True)
                store_obj = LanceDBTripleStore(store_path, embedder=embedder)
            except Exception as e:
                raise WorkflowBundleError(
                    "Bundle uses memory_kg_* nodes, which require a LanceDB-backed store. "
                    "Install `lancedb` and ensure the gateway runs under the same environment."
                ) from e

            extra_effect_handlers = build_memory_kg_effect_handlers(store=store_obj, run_store=run_store, now_iso=utc_now_iso)

        # Optional AbstractCore integration for LLM_CALL + TOOL_CALLS.
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
                    "This bundle requires LLM/tool execution, but AbstractRuntime was installed "
                    "without AbstractCore integration. Install `abstractruntime[abstractcore]` "
                    "(and ensure `abstractcore` is importable)."
                ) from e

            tool_mode = str(_env("ABSTRACTGATEWAY_TOOL_MODE") or "passthrough").strip().lower()
            if tool_mode in {"local", "local_all"}:
                # Dev-only: execute the default tool map in-process without relying on the
                # AbstractCore global registry (which is typically empty in gateway mode).
                tool_executor: Any = MappingToolExecutor(build_default_tool_map())
            elif tool_mode in {"approval", "local_approval", "local-approval"}:
                # Local tool execution with explicit human approval for dangerous/unknown tools.
                base_executor: Any = MappingToolExecutor(build_default_tool_map())
                try:
                    from abstractruntime.integrations.abstractcore.tool_executor import ApprovalToolExecutor, ToolApprovalPolicy

                    tool_executor = ApprovalToolExecutor(delegate=base_executor, policy=ToolApprovalPolicy())
                except Exception:
                    tool_executor = base_executor
            else:
                # Default safe mode: do not execute tools in-process; enter a durable wait instead.
                tool_executor = PassthroughToolExecutor(mode="approval_required")

            if needs_llm:
                try:
                    from abstractruntime.integrations.abstractcore.factory import create_local_runtime
                except Exception as e:  # pragma: no cover
                    raise WorkflowBundleError(
                        "LLM nodes require AbstractRuntime AbstractCore integration. "
                        "Install `abstractruntime[abstractcore]`."
                    ) from e

                provider = _env("ABSTRACTGATEWAY_PROVIDER") or _env("ABSTRACTFLOW_PROVIDER") or ""
                model = _env("ABSTRACTGATEWAY_MODEL") or _env("ABSTRACTFLOW_MODEL") or ""
                provider = provider.strip().lower()
                model = model.strip()

                if not provider or not model:
                    detected = _scan_flows_for_llm_defaults(flows_by_namespaced_id)
                    if detected is not None:
                        provider, model = detected

                if (not provider or not model):
                    # Best-effort: fall back to AbstractCore config defaults (abstractcore --config).
                    try:
                        from abstractcore.config import get_config_manager  # type: ignore

                        cfg = get_config_manager().config
                        cfg_provider = str(getattr(cfg.default_models, "global_provider", "") or "").strip().lower()
                        cfg_model = str(getattr(cfg.default_models, "global_model", "") or "").strip()
                        if cfg_provider and cfg_model:
                            if not provider and not model:
                                provider, model = cfg_provider, cfg_model
                            elif provider and not model and cfg_provider == provider:
                                model = cfg_model
                    except Exception:
                        pass

                if not provider or not model:
                    raise WorkflowBundleError(
                        "Bundle contains LLM nodes but no default provider/model is configured. "
                        "Set ABSTRACTGATEWAY_PROVIDER and ABSTRACTGATEWAY_MODEL, configure defaults via "
                        "`abstractcore --config`, or ensure the flow JSON includes provider/model on at least "
                        "one llm_call/agent node."
                    )

                runtime = create_local_runtime(
                    provider=provider,
                    model=model,
                    run_store=run_store,
                    ledger_store=ledger_store,
                    artifact_store=artifact_store,
                    tool_executor=tool_executor,
                    extra_effect_handlers=extra_effect_handlers,
                )
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

            from abstractcore.tools import ToolDefinition

            try:
                from abstractruntime.integrations.abstractcore.default_tools import list_default_tool_specs
            except Exception as e:  # pragma: no cover
                raise WorkflowBundleError(
                    "Visual Agent nodes require AbstractCore tool schemas (abstractruntime[abstractcore])."
                ) from e

            def _tool_defs_from_specs(specs0: list[dict[str, Any]]) -> list[ToolDefinition]:
                out: list[ToolDefinition] = []
                for s in specs0:
                    if not isinstance(s, dict):
                        continue
                    name = s.get("name")
                    if not isinstance(name, str) or not name.strip():
                        continue
                    desc = s.get("description")
                    params = s.get("parameters")
                    out.append(
                        ToolDefinition(
                            name=name.strip(),
                            description=str(desc or ""),
                            parameters=dict(params) if isinstance(params, dict) else {},
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

        return WorkflowBundleGatewayHost(
            bundles_dir=base,
            data_dir=data_root,
            dynamic_flows_dir=dynamic_dir,
            deprecation_store=dep_store,
            bundles=bundles_by_id,
            latest_bundle_versions=latest_versions,
            runtime=runtime,
            workflow_registry=wf_reg,
            specs=specs,
            event_listener_specs_by_root=event_listener_specs_by_root,
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
            run_store=self.run_store,
            ledger_store=self.ledger_store,
            artifact_store=self.artifact_store,
        )
        with self._lock:
            self.bundles = new_host.bundles
            self.latest_bundle_versions = new_host.latest_bundle_versions
            self.runtime = new_host.runtime
            self.workflow_registry = new_host.workflow_registry
            self.specs = new_host.specs
            self.event_listener_specs_by_root = new_host.event_listener_specs_by_root
            self._default_bundle_id = new_host._default_bundle_id
            self.deprecation_store = new_host.deprecation_store
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
        # Best-effort: seed durable run vars with the gateway runtime defaults so VisualFlow
        # nodes (notably Agent nodes) can inherit provider/model without per-node wiring.
        try:
            cfg = getattr(self.runtime, "config", None)
            default_provider = getattr(cfg, "provider", None)
            default_model = getattr(cfg, "model", None)
        except Exception:  # pragma: no cover
            default_provider = None
            default_model = None
        if default_provider or default_model:
            rt_ns = vars0.get("_runtime")
            if not isinstance(rt_ns, dict):
                rt_ns = {}
                vars0["_runtime"] = rt_ns
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
