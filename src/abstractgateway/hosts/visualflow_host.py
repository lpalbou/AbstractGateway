from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from abstractruntime import Runtime


logger = logging.getLogger(__name__)


def _parse_visual_listener_id(workflow_id: str) -> Optional[tuple[str, str]]:
    """Return (flow_id, node_id) for a visual_event_listener_* workflow_id."""
    prefix = "visual_event_listener_"
    if not isinstance(workflow_id, str) or not workflow_id.startswith(prefix):
        return None
    rest = workflow_id[len(prefix) :]
    parts = rest.split("_", 1)
    if len(parts) != 2:
        return None
    flow_id, node_id = parts[0], parts[1]
    if not flow_id or not node_id:
        return None
    return (flow_id, node_id)


def _parse_visual_agent_id(workflow_id: str) -> Optional[tuple[str, str]]:
    """Return (flow_id, node_id) for a visual_react_agent_* workflow_id."""
    prefix = "visual_react_agent_"
    if not isinstance(workflow_id, str) or not workflow_id.startswith(prefix):
        return None
    rest = workflow_id[len(prefix) :]
    parts = rest.split("_", 1)
    if len(parts) != 2:
        return None
    flow_id, node_id = parts[0], parts[1]
    if not flow_id or not node_id:
        return None
    return (flow_id, node_id)


def _require_visualflow_deps() -> None:
    try:
        import abstractflow  # noqa: F401
    except Exception as e:  # pragma: no cover
        raise RuntimeError(
            "VisualFlow execution requires the AbstractFlow compiler library (not the web UI). "
            "Install: `pip install abstractgateway[visualflow]`"
        ) from e


@dataclass(frozen=True)
class VisualFlowRegistry:
    """Load VisualFlow JSON files from a directory."""

    flows_dir: Path

    def load(self) -> Dict[str, Any]:
        _require_visualflow_deps()
        from abstractflow.visual.models import VisualFlow

        base = Path(self.flows_dir).expanduser().resolve()
        if not base.exists():
            raise FileNotFoundError(f"flows_dir does not exist: {base}")

        flows: Dict[str, Any] = {}
        for p in sorted(base.glob("*.json")):
            try:
                raw = p.read_text(encoding="utf-8")
                data = json.loads(raw)
                flow = VisualFlow.model_validate(data)
                flow_id = str(getattr(flow, "id", "") or "").strip() or p.stem
                flows[flow_id] = flow
            except Exception as e:
                logger.warning("Failed to load flow file %s: %s", p, e)
        return flows


class VisualFlowGatewayHost:
    """Gateway host that starts/ticks runs from VisualFlow JSON.

    This host is used until WorkflowBundles/WorkflowArtifact remove the need for AbstractFlow at runtime.
    """

    def __init__(
        self,
        *,
        flows_dir: Path,
        flows: Dict[str, Any],
        run_store: Any,
        ledger_store: Any,
        artifact_store: Any,
    ) -> None:
        _require_visualflow_deps()
        self._flows_dir = Path(flows_dir).expanduser().resolve()
        self._flows = dict(flows)
        self._run_store = run_store
        self._ledger_store = ledger_store
        self._artifact_store = artifact_store

    @property
    def flows_dir(self) -> Path:
        return self._flows_dir

    @property
    def flows(self) -> Dict[str, Any]:
        return dict(self._flows)

    @property
    def run_store(self) -> Any:
        return self._run_store

    @property
    def ledger_store(self) -> Any:
        return self._ledger_store

    @property
    def artifact_store(self) -> Any:
        return self._artifact_store

    def start_run(self, *, flow_id: str, input_data: Dict[str, Any], actor_id: str = "gateway") -> str:
        _require_visualflow_deps()
        from abstractflow.visual.executor import create_visual_runner
        from abstractflow.visual.workspace_scoped_tools import WorkspaceScope, build_scoped_tool_executor

        fid = str(flow_id or "").strip()
        if not fid:
            raise ValueError("flow_id is required")
        visual_flow = self._flows.get(fid)
        if visual_flow is None:
            raise KeyError(f"Flow '{fid}' not found in {self._flows_dir}")

        data = dict(input_data or {})
        scope = WorkspaceScope.from_input_data(data)
        tool_executor = build_scoped_tool_executor(scope=scope) if scope is not None else None

        vis_runner = create_visual_runner(
            visual_flow,
            flows=self._flows,
            run_store=self._run_store,
            ledger_store=self._ledger_store,
            artifact_store=self._artifact_store,
            tool_executor=tool_executor,
        )
        return str(vis_runner.start(data, actor_id=actor_id))

    def runtime_and_workflow_for_run(self, run_id: str) -> tuple[Runtime, Any]:
        """Return (runtime, workflow_spec) for the given run, ensuring derived VisualFlow workflows are registered."""

        run = self._run_store.load(run_id)
        if run is None:
            raise KeyError(f"Run '{run_id}' not found")
        workflow_id = getattr(run, "workflow_id", None)
        if not isinstance(workflow_id, str) or not workflow_id:
            raise ValueError(f"Run '{run_id}' missing workflow_id")

        # Determine which VisualFlow root we need to compile to ensure this workflow_id is registered.
        root_flow_id = workflow_id
        parsed = _parse_visual_listener_id(workflow_id)
        if parsed is not None:
            root_flow_id = parsed[0]
        parsed2 = _parse_visual_agent_id(workflow_id)
        if parsed2 is not None:
            root_flow_id = parsed2[0]

        visual_flow = self._flows.get(root_flow_id)
        if visual_flow is None:
            raise KeyError(f"Flow '{root_flow_id}' not found (needed for workflow '{workflow_id}')")

        # Rebuild tool scope from persisted vars (best-effort).
        _require_visualflow_deps()
        from abstractflow.visual.executor import create_visual_runner
        from abstractflow.visual.workspace_scoped_tools import WorkspaceScope, build_scoped_tool_executor

        vars0 = getattr(run, "vars", None)
        scope = WorkspaceScope.from_input_data(vars0) if isinstance(vars0, dict) else None
        tool_executor = build_scoped_tool_executor(scope=scope) if scope is not None else None

        runner = create_visual_runner(
            visual_flow,
            flows=self._flows,
            run_store=self._run_store,
            ledger_store=self._ledger_store,
            artifact_store=self._artifact_store,
            tool_executor=tool_executor,
        )
        runtime = runner.runtime

        # WorkflowSpec lookup: root uses runner.workflow; derived workflows use runtime.workflow_registry.
        if getattr(runner.workflow, "workflow_id", None) == workflow_id:
            return (runtime, runner.workflow)

        reg = getattr(runtime, "workflow_registry", None)
        if reg is None or not hasattr(reg, "get"):
            raise KeyError(f"Runtime has no workflow_registry for workflow '{workflow_id}'")
        wf = reg.get(workflow_id)
        if wf is None:
            raise KeyError(f"Workflow '{workflow_id}' not registered")
        return (runtime, wf)


