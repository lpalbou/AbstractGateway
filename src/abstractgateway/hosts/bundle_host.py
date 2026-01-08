from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

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
                try:
                    spec = compile_visualflow(namespaced_raw)
                except Exception as e:
                    raise WorkflowBundleError(f"Failed compiling VisualFlow '{bid}:{flow_id}': {e}") from e
                wf_reg.register(spec)
                specs[str(spec.workflow_id)] = spec

        runtime = Runtime(
            run_store=run_store,
            ledger_store=ledger_store,
            workflow_registry=wf_reg,
            artifact_store=artifact_store,
        )
        return WorkflowBundleGatewayHost(
            bundles_dir=base,
            bundles=bundles,
            runtime=runtime,
            workflow_registry=wf_reg,
            specs=specs,
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
        workflow_id = _coerce_namespaced_id(
            bundle_id=bundle_id, flow_id=flow_id, default_bundle_id=self._default_bundle_id
        )
        spec = self.specs.get(workflow_id)
        if spec is None:
            raise KeyError(f"Workflow '{workflow_id}' not found")
        return str(self.runtime.start(workflow=spec, vars=dict(input_data or {}), actor_id=actor_id))

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
