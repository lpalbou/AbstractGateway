from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from abstractruntime import Runtime, WorkflowRegistry, WorkflowSpec
from abstractruntime.visualflow_compiler import compile_visualflow
from abstractruntime.workflow_bundle import WorkflowBundle, WorkflowBundleError, open_workflow_bundle


logger = logging.getLogger(__name__)


def _namespace(bundle_id: str, flow_id: str) -> str:
    return f"{bundle_id}:{flow_id}"


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


@dataclass(frozen=True)
class WorkflowBundleGatewayHost:
    """Gateway host that starts/ticks runs from WorkflowBundles (no AbstractFlow import).

    Compiles `manifest.flows` (VisualFlow JSON) via AbstractRuntime's VisualFlow compiler
    (single semantics).
    """

    bundles_dir: Path
    bundles: Dict[str, WorkflowBundle]
    runtime: Runtime
    workflow_registry: WorkflowRegistry
    specs: Dict[str, WorkflowSpec]
    event_listener_specs_by_root: Dict[str, list[str]]
    _default_bundle_id: Optional[str]

    @staticmethod
    def load_from_dir(
        *,
        bundles_dir: Path,
        run_store: Any,
        ledger_store: Any,
        artifact_store: Any,
    ) -> "WorkflowBundleGatewayHost":
        base = Path(bundles_dir).expanduser().resolve()
        if not base.exists():
            raise FileNotFoundError(f"bundles_dir does not exist: {base}")

        bundle_paths: list[Path] = []
        if base.is_file():
            bundle_paths = [base]
        else:
            bundle_paths = sorted([p for p in base.glob("*.flow") if p.is_file()])

        bundles: Dict[str, WorkflowBundle] = {}
        for p in bundle_paths:
            try:
                b = open_workflow_bundle(p)
                bundles[str(b.manifest.bundle_id)] = b
            except Exception as e:
                logger.warning("Failed to load bundle %s: %s", p, e)

        if not bundles:
            raise FileNotFoundError(f"No bundles found in {base} (expected *.flow)")

        default_bundle_id = next(iter(bundles.keys())) if len(bundles) == 1 else None

        # Build runtime + registry and register all workflow specs.
        wf_reg: WorkflowRegistry = WorkflowRegistry()
        specs: Dict[str, WorkflowSpec] = {}
        flows_by_namespaced_id: Dict[str, Dict[str, Any]] = {}

        for bid, b in bundles.items():
            man = b.manifest
            if not man.flows:
                raise WorkflowBundleError(f"Bundle '{bid}' has no flows (manifest.flows is empty)")

            flow_ids = set(man.flows.keys())
            id_map = {flow_id: _namespace(bid, flow_id) for flow_id in flow_ids}

            for flow_id, rel in man.flows.items():
                raw = b.read_json(rel)
                if not isinstance(raw, dict):
                    raise WorkflowBundleError(f"VisualFlow JSON for '{bid}:{flow_id}' must be an object")
                namespaced_raw = _namespace_visualflow_raw(
                    raw=raw,
                    bundle_id=bid,
                    flow_id=flow_id,
                    id_map=id_map,
                )
                flows_by_namespaced_id[str(namespaced_raw.get("id") or _namespace(bid, flow_id))] = namespaced_raw
                try:
                    spec = compile_visualflow(namespaced_raw)
                except Exception as e:
                    raise WorkflowBundleError(f"Failed compiling VisualFlow '{bid}:{flow_id}': {e}") from e
                wf_reg.register(spec)
                specs[str(spec.workflow_id)] = spec

        needs_llm = any(_flow_uses_llm(raw) for raw in flows_by_namespaced_id.values())
        needs_tools = any(_flow_uses_tools(raw) for raw in flows_by_namespaced_id.values())

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
            if tool_mode == "local":
                # Dev-only: execute the default tool map in-process without relying on the
                # AbstractCore global registry (which is typically empty in gateway mode).
                tool_executor: Any = MappingToolExecutor(build_default_tool_map())
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

                if not provider or not model:
                    raise WorkflowBundleError(
                        "Bundle contains LLM nodes but no default provider/model is configured. "
                        "Set ABSTRACTGATEWAY_PROVIDER and ABSTRACTGATEWAY_MODEL (or ensure the flow JSON "
                        "includes provider/model on at least one llm_call/agent node)."
                    )

                runtime = create_local_runtime(
                    provider=provider,
                    model=model,
                    run_store=run_store,
                    ledger_store=ledger_store,
                    artifact_store=artifact_store,
                    tool_executor=tool_executor,
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
                        EffectType.TOOL_CALLS: make_tool_calls_handler(tools=tool_executor),
                    },
                )
        else:
            runtime = Runtime(
                run_store=run_store,
                ledger_store=ledger_store,
                workflow_registry=wf_reg,
                artifact_store=artifact_store,
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
                    INSPECT_VARS_TOOL,
                    RECALL_MEMORY_TOOL,
                    REMEMBER_TOOL,
                )

                builtin_defs = [ASK_USER_TOOL, RECALL_MEMORY_TOOL, INSPECT_VARS_TOOL, REMEMBER_TOOL, COMPACT_MEMORY_TOOL]
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
            bundles=bundles,
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

    def start_run(
        self,
        *,
        flow_id: str,
        input_data: Dict[str, Any],
        actor_id: str = "gateway",
        bundle_id: Optional[str] = None,
    ) -> str:
        fid = str(flow_id or "").strip()
        bid = str(bundle_id or "").strip() if isinstance(bundle_id, str) else ""
        bundle_id_clean = bid or None

        if not fid:
            # Default entrypoint selection for the common case:
            # start {bundle_id, input_data} without needing flow_id.
            selected_bundle_id = bundle_id_clean or self._default_bundle_id
            if not selected_bundle_id:
                raise ValueError(
                    "flow_id is required when multiple bundles are loaded; "
                    "provide bundle_id (or pass flow_id as 'bundle:flow')"
                )
            bundle = self.bundles.get(str(selected_bundle_id))
            if bundle is None:
                raise KeyError(f"Bundle '{selected_bundle_id}' not found")
            entrypoints = list(getattr(bundle.manifest, "entrypoints", None) or [])
            default_ep = str(getattr(bundle.manifest, "default_entrypoint", "") or "").strip()
            if len(entrypoints) == 1:
                ep_fid = str(getattr(entrypoints[0], "flow_id", "") or "").strip()
            elif default_ep:
                ep_fid = default_ep
            else:
                raise ValueError(
                    f"Bundle '{selected_bundle_id}' has {len(entrypoints)} entrypoints; "
                    "specify flow_id to select which entrypoint to start "
                    "(or set manifest.default_entrypoint)"
                )
            if not ep_fid:
                raise ValueError(f"Bundle '{selected_bundle_id}' entrypoint flow_id is empty")
            workflow_id = _namespace(str(selected_bundle_id), ep_fid)
        else:
            workflow_id = _coerce_namespaced_id(
                bundle_id=bundle_id_clean, flow_id=fid, default_bundle_id=self._default_bundle_id
            )

        spec = self.specs.get(workflow_id)
        if spec is None:
            raise KeyError(f"Workflow '{workflow_id}' not found")
        run_id = str(self.runtime.start(workflow=spec, vars=dict(input_data or {}), actor_id=actor_id))

        # Start session-scoped event listener workflows (best-effort).
        listener_ids = self.event_listener_specs_by_root.get(workflow_id) or []
        for wid in listener_ids:
            listener_spec = self.specs.get(wid)
            if listener_spec is None:
                continue
            try:
                child_run_id = self.runtime.start(
                    workflow=listener_spec,
                    vars={},
                    session_id=run_id,
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
        if spec is None:
            raise KeyError(f"Workflow '{workflow_id}' not registered")
        return (self.runtime, spec)
