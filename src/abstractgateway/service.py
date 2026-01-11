from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .config import GatewayHostConfig
from .hosts.visualflow_host import VisualFlowGatewayHost, VisualFlowRegistry
from .runner import GatewayRunner, GatewayRunnerConfig
from .security import GatewayAuthPolicy, load_gateway_auth_policy_from_env
from .stores import GatewayStores, build_file_stores


@dataclass(frozen=True)
class GatewayService:
    """Composition root: host + runner + security policy."""

    config: GatewayHostConfig
    stores: GatewayStores
    host: Any
    runner: GatewayRunner
    auth_policy: GatewayAuthPolicy
    telegram_bridge: Optional[Any] = None


_service: Optional[GatewayService] = None


def get_gateway_service() -> GatewayService:
    global _service
    if _service is None:
        _service = create_default_gateway_service()
    return _service


def create_default_gateway_service() -> GatewayService:
    cfg = GatewayHostConfig.from_env()
    stores = build_file_stores(base_dir=cfg.data_dir)

    # Workflow source:
    # - bundle (default): `.flow` bundles with VisualFlow JSON (compiled via AbstractRuntime; no AbstractFlow import)
    # - visualflow (optional): load VisualFlow JSON files directly from a directory (host wiring currently uses AbstractFlow extras)
    source = str(os.getenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle") or "bundle").strip().lower()
    if source == "bundle":
        from .hosts.bundle_host import WorkflowBundleGatewayHost

        host = WorkflowBundleGatewayHost.load_from_dir(
            bundles_dir=cfg.flows_dir,
            data_dir=cfg.data_dir,
            run_store=stores.run_store,
            ledger_store=stores.ledger_store,
            artifact_store=stores.artifact_store,
        )
    elif source == "visualflow":
        flows = VisualFlowRegistry(flows_dir=cfg.flows_dir).load()
        host = VisualFlowGatewayHost(
            flows_dir=cfg.flows_dir,
            flows=flows,
            run_store=stores.run_store,
            ledger_store=stores.ledger_store,
            artifact_store=stores.artifact_store,
        )
    else:
        raise RuntimeError(f"Unsupported workflow source: {source}. Supported: bundle|visualflow")

    runner_cfg = GatewayRunnerConfig(
        poll_interval_s=float(cfg.poll_interval_s),
        command_batch_limit=int(cfg.command_batch_limit),
        tick_max_steps=int(cfg.tick_max_steps),
        tick_workers=int(cfg.tick_workers),
        run_scan_limit=int(cfg.run_scan_limit),
    )
    runner = GatewayRunner(base_dir=stores.base_dir, host=host, config=runner_cfg, enable=bool(cfg.runner_enabled))

    policy = load_gateway_auth_policy_from_env()

    telegram_bridge = None
    enabled_raw = os.getenv("ABSTRACT_TELEGRAM_BRIDGE")
    if enabled_raw is not None and str(enabled_raw).strip().lower() in {"1", "true", "yes", "on"}:
        try:
            from .integrations.telegram_bridge import TelegramBridge, TelegramBridgeConfig
        except Exception as e:
            raise RuntimeError(
                "Telegram bridge is enabled (ABSTRACT_TELEGRAM_BRIDGE=1) but the optional Telegram dependencies are not installed. "
                "Install with: `pip install \"abstractgateway[telegram]\"`"
            ) from e

        tcfg = TelegramBridgeConfig.from_env(base_dir=cfg.data_dir)
        if not tcfg.flow_id:
            raise RuntimeError("ABSTRACT_TELEGRAM_FLOW_ID is required when ABSTRACT_TELEGRAM_BRIDGE=1")
        telegram_bridge = TelegramBridge(config=tcfg, host=host, runner=runner, artifact_store=stores.artifact_store)

    return GatewayService(
        config=cfg,
        stores=stores,
        host=host,
        runner=runner,
        auth_policy=policy,
        telegram_bridge=telegram_bridge,
    )


def start_gateway_runner() -> None:
    svc = get_gateway_service()
    svc.runner.start()
    bridge = getattr(svc, "telegram_bridge", None)
    if bridge is not None:
        bridge.start()


def stop_gateway_runner() -> None:
    global _service
    if _service is None:
        return
    try:
        try:
            bridge = getattr(_service, "telegram_bridge", None)
            if bridge is not None:
                bridge.stop()
        except Exception:
            pass
        _service.runner.stop()
    finally:
        _service = None


def run_summary(run: Any) -> Dict[str, Any]:
    """HTTP-safe run summary (do not return full run.vars)."""

    waiting = getattr(run, "waiting", None)
    status = getattr(getattr(run, "status", None), "value", None) or str(getattr(run, "status", "unknown"))
    out: Dict[str, Any] = {
        "run_id": getattr(run, "run_id", ""),
        "workflow_id": getattr(run, "workflow_id", None),
        "status": status,
        "current_node": getattr(run, "current_node", None),
        "created_at": getattr(run, "created_at", None),
        "updated_at": getattr(run, "updated_at", None),
        "actor_id": getattr(run, "actor_id", None),
        "session_id": getattr(run, "session_id", None),
        "parent_run_id": getattr(run, "parent_run_id", None),
        "error": getattr(run, "error", None),
        # Best-effort pause metadata. We intentionally do not return full run.vars over HTTP.
        "paused": False,
        "pause_reason": None,
        "paused_at": None,
        "resumed_at": None,
        "waiting": None,
    }
    try:
        vars_obj = getattr(run, "vars", None)
        runtime_ns = vars_obj.get("_runtime") if isinstance(vars_obj, dict) else None
        control = runtime_ns.get("control") if isinstance(runtime_ns, dict) else None
        if isinstance(control, dict):
            out["paused"] = bool(control.get("paused") is True)
            out["pause_reason"] = control.get("pause_reason")
            out["paused_at"] = control.get("paused_at")
            out["resumed_at"] = control.get("resumed_at")
    except Exception:
        pass
    if waiting is not None:
        out["waiting"] = {
            "reason": getattr(getattr(waiting, "reason", None), "value", None) or str(getattr(waiting, "reason", "")),
            "wait_key": getattr(waiting, "wait_key", None),
            "prompt": getattr(waiting, "prompt", None),
            "choices": getattr(waiting, "choices", None),
            "allow_free_text": getattr(waiting, "allow_free_text", None),
            "details": getattr(waiting, "details", None),
        }
    return out
