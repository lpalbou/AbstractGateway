from __future__ import annotations

import os
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Optional

from abstractruntime.core.run_lifecycle import extract_run_lifecycle

from .config import GatewayHostConfig
from .embeddings_config import build_embedding_client, resolve_embedding_config
from .runner import GatewayRunner, GatewayRunnerConfig
from .security.principal import GatewayPrincipal, current_gateway_principal, safe_principal_component
from .security import GatewayAuthPolicy, load_gateway_auth_policy_from_env
from .stores import GatewayStores, build_file_stores, build_sqlite_stores
from .users import gateway_user_auth_enabled
from .workflow_catalog import workflow_catalog_bundles_root_from_env

_DRAFT_RUN_PURPOSE = "draft_test"


def is_draft_run_lifecycle(lifecycle: Any) -> bool:
    if not isinstance(lifecycle, dict):
        return False
    purpose = lifecycle.get("purpose")
    return isinstance(purpose, str) and purpose.strip() == _DRAFT_RUN_PURPOSE


@dataclass(frozen=True)
class GatewayService:
    """Composition root: host + runner + security policy."""

    config: GatewayHostConfig
    stores: GatewayStores
    host: Any
    runner: GatewayRunner
    auth_policy: GatewayAuthPolicy
    embedding_provider: Optional[str] = None
    embedding_model: Optional[str] = None
    embedding_base_url: Optional[str] = None
    embedding_error: Optional[str] = None
    embeddings_client: Optional[Any] = None
    telegram_bridge: Optional[Any] = None
    email_bridge: Optional[Any] = None


_service: Optional[GatewayService] = None
_services_by_principal: Dict[str, GatewayService] = {}
_service_lock = threading.RLock()
_backlog_exec_runner: Optional[Any] = None
_backlog_exec_runner_error: Optional[str] = None


def _env_bool(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def backlog_exec_runner_status() -> Dict[str, Any]:
    runner = _backlog_exec_runner
    alive = False
    last_error: Optional[str] = None
    try:
        if runner is not None and hasattr(runner, "is_running"):
            alive = bool(runner.is_running())
        elif runner is not None and hasattr(runner, "_thread"):
            t = getattr(runner, "_thread", None)
            alive = bool(t is not None and getattr(t, "is_alive", lambda: False)())
    except Exception:
        alive = False

    try:
        if runner is not None and hasattr(runner, "last_error"):
            last_error = runner.last_error()
    except Exception:
        last_error = None

    if _backlog_exec_runner_error:
        last_error = _backlog_exec_runner_error

    return {"alive": bool(alive), "error": (str(last_error) if last_error else "") or None}


def get_gateway_service() -> GatewayService:
    global _service
    principal = current_gateway_principal()
    if principal is not None and gateway_multi_user_enabled():
        return get_gateway_service_for_principal(principal)
    with _service_lock:
        if _service is None:
            _service = create_default_gateway_service()
        return _service


def gateway_multi_user_enabled() -> bool:
    return bool(gateway_user_auth_enabled())


def _principal_uses_default_runtime(principal: GatewayPrincipal) -> bool:
    if not _env_bool("ABSTRACTGATEWAY_ADMIN_USES_DEFAULT_RUNTIME", default=True):
        return False
    tenant = safe_principal_component(principal.tenant_id, default="default")
    user = safe_principal_component(principal.user_id, default="")
    runtime_id = safe_principal_component(principal.runtime_id or principal.user_id, default=user or "user")
    return (
        tenant == "default"
        and user == "admin"
        and runtime_id in {"admin", "default"}
        and principal.is_admin()
    )


def _principal_service_key(principal: GatewayPrincipal) -> str:
    if _principal_uses_default_runtime(principal):
        return "default:__gateway_default_runtime__"
    tenant = safe_principal_component(principal.tenant_id, default="default")
    user = safe_principal_component(principal.runtime_id or principal.user_id, default=principal.user_id or "user")
    return f"{tenant}:{user}"


def _config_for_principal(principal: GatewayPrincipal) -> GatewayHostConfig:
    base = GatewayHostConfig.from_env()
    if _principal_uses_default_runtime(principal):
        return replace(base, root_data_dir=base.root_data_dir or base.data_dir, tenant_id="default", user_id=principal.user_id, runtime_id="default")
    tenant = safe_principal_component(principal.tenant_id, default="default")
    runtime_id = safe_principal_component(principal.runtime_id or principal.user_id, default=principal.user_id or "user")
    root = base.data_dir / "users" / tenant / runtime_id
    data_dir = root / "runtime"
    flows_dir = root / "flows"
    db_path = data_dir / "gateway.sqlite3" if str(base.store_backend).strip().lower() == "sqlite" else None
    return replace(
        base,
        data_dir=data_dir,
        flows_dir=flows_dir,
        root_data_dir=base.root_data_dir or base.data_dir,
        tenant_id=tenant,
        user_id=safe_principal_component(principal.user_id, default="user"),
        runtime_id=runtime_id,
        db_path=db_path,
    )


def get_gateway_service_for_principal(principal: GatewayPrincipal) -> GatewayService:
    if not gateway_multi_user_enabled():
        return get_gateway_service()
    key = _principal_service_key(principal)
    with _service_lock:
        svc = _services_by_principal.get(key)
        if svc is None:
            svc = create_default_gateway_service(config=_config_for_principal(principal))
            _services_by_principal[key] = svc
            if getattr(svc.config, "runner_enabled", False):
                try:
                    svc.runner.start()
                except Exception:
                    _services_by_principal.pop(key, None)
                    raise
        return svc


def invalidate_gateway_service_for_runtime(*, tenant_id: str, runtime_id: str) -> bool:
    """Drop a cached per-principal service after admin runtime reassignment/purge."""
    tenant = safe_principal_component(tenant_id, default="default")
    runtime = safe_principal_component(runtime_id, default="")
    if not tenant or not runtime:
        return False
    keys = [f"{tenant}:{runtime}"]
    if tenant == "default" and runtime == "default":
        keys.append("default:__gateway_default_runtime__")
    removed: list[GatewayService] = []
    with _service_lock:
        for key in keys:
            svc = _services_by_principal.pop(key, None)
            if svc is not None:
                removed.append(svc)
    for svc in removed:
        _stop_gateway_service_instance(svc)
    return bool(removed)


def create_default_gateway_service(*, config: Optional[GatewayHostConfig] = None) -> GatewayService:
    cfg = config or GatewayHostConfig.from_env()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.flows_dir.mkdir(parents=True, exist_ok=True)
    backend = str(getattr(cfg, "store_backend", "file") or "file").strip().lower() or "file"
    if backend == "file":
        stores = build_file_stores(base_dir=cfg.data_dir)
    elif backend == "sqlite":
        stores = build_sqlite_stores(base_dir=cfg.data_dir, db_path=getattr(cfg, "db_path", None))
    else:
        raise RuntimeError(f"Unsupported store backend: {backend}. Supported: file|sqlite")

    # Best-effort: apply persisted process-manager env overrides early so runtime
    # integrations (email bridge, report triage, etc.) observe the configured values
    # immediately after a gateway restart.
    try:
        enabled_raw = os.getenv("ABSTRACTGATEWAY_ENABLE_PROCESS_MANAGER")
        if enabled_raw is not None and str(enabled_raw).strip().lower() in {"1", "true", "yes", "on"}:
            from .maintenance.process_manager import get_managed_env_var_manager

            get_managed_env_var_manager(base_dir=stores.base_dir)
    except Exception:
        pass

    # Workflow source:
    # - bundle (default): `.flow` bundles with VisualFlow JSON (compiled via AbstractRuntime; no AbstractFlow import)
    #
    # NOTE: VisualFlow directory mode was intentionally removed. Gateway is the
    # execution/control plane and should not depend on the AbstractFlow compiler
    # library. Use bundle mode and publish VisualFlows to `.flow` bundles via:
    # `POST /api/gateway/visualflows/{flow_id}/publish`.
    source = str(os.getenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle") or "bundle").strip().lower()
    if source == "bundle":
        from .hosts.bundle_host import WorkflowBundleGatewayHost

        root_data_dir = Path(getattr(cfg, "root_data_dir", None) or cfg.data_dir).expanduser().resolve()
        tenant_id = safe_principal_component(getattr(cfg, "tenant_id", "default"), default="default")
        catalog_bundles_dir = workflow_catalog_bundles_root_from_env(root_data_dir) / "tenant_catalog" / tenant_id
        host = WorkflowBundleGatewayHost.load_from_dir(
            bundles_dir=cfg.flows_dir,
            data_dir=cfg.data_dir,
            catalog_bundles_dir=catalog_bundles_dir,
            catalog_root_data_dir=root_data_dir,
            catalog_tenant_id=tenant_id,
            catalog_user_id=safe_principal_component(getattr(cfg, "user_id", "admin"), default="admin"),
            catalog_runtime_id=safe_principal_component(getattr(cfg, "runtime_id", tenant_id), default=tenant_id),
            run_store=stores.run_store,
            ledger_store=stores.ledger_store,
            artifact_store=stores.artifact_store,
        )
    else:
        raise RuntimeError(f"Unsupported workflow source: {source}. Supported: bundle")

    runner_cfg = GatewayRunnerConfig(
        poll_interval_s=float(cfg.poll_interval_s),
        command_batch_limit=int(cfg.command_batch_limit),
        tick_max_steps=int(cfg.tick_max_steps),
        tick_workers=int(cfg.tick_workers),
        run_scan_limit=int(cfg.run_scan_limit),
    )
    runner = GatewayRunner(
        base_dir=stores.base_dir,
        host=host,
        config=runner_cfg,
        enable=bool(cfg.runner_enabled),
        command_store=stores.command_store,
        cursor_store=stores.command_cursor_store,
    )

    policy = load_gateway_auth_policy_from_env()

    embedding_provider: Optional[str] = None
    embedding_model: Optional[str] = None
    embedding_base_url: Optional[str] = None
    embedding_error: Optional[str] = None
    embeddings_client: Optional[Any] = None
    try:
        embedding_route = resolve_embedding_config(base_dir=stores.base_dir)
        embedding_provider = embedding_route.provider
        embedding_model = embedding_route.model
        embedding_base_url = embedding_route.base_url
        embeddings_client = build_embedding_client(
            embedding_route,
            cache_dir=Path(stores.base_dir) / "abstractcore" / "embeddings",
            # Embeddings must be trustworthy for semantic retrieval; do not return zero vectors on failure.
            strict=True,
        )
    except Exception as e:
        # Embeddings are optional: the gateway may run without AbstractCore embedding deps.
        embedding_error = str(e)
        embeddings_client = None

    telegram_bridge = None
    enabled_raw = os.getenv("ABSTRACT_TELEGRAM_BRIDGE")
    if enabled_raw is not None and str(enabled_raw).strip().lower() in {"1", "true", "yes", "on"}:
        try:
            from .integrations.telegram_bridge import TelegramBridge, TelegramBridgeConfig
        except Exception as e:
            raise RuntimeError(
                "Telegram bridge is enabled (ABSTRACT_TELEGRAM_BRIDGE=1) but Gateway's Telegram integration could not be imported. "
                "Install/repair the base Gateway environment with: `pip install abstractgateway`"
            ) from e

        tcfg = TelegramBridgeConfig.from_env(base_dir=cfg.data_dir)
        if not tcfg.flow_id:
            raise RuntimeError("ABSTRACT_TELEGRAM_FLOW_ID is required when ABSTRACT_TELEGRAM_BRIDGE=1")
        telegram_bridge = TelegramBridge(config=tcfg, host=host, runner=runner, artifact_store=stores.artifact_store)

    email_bridge = None
    email_enabled_raw = os.getenv("ABSTRACT_EMAIL_BRIDGE")
    if email_enabled_raw is not None and str(email_enabled_raw).strip().lower() in {"1", "true", "yes", "on"}:
        from .integrations.email_bridge import EmailBridge, EmailBridgeConfig

        ecfg = EmailBridgeConfig.from_env(base_dir=cfg.data_dir)
        email_bridge = EmailBridge(config=ecfg, host=host, runner=runner, artifact_store=stores.artifact_store)

    return GatewayService(
        config=cfg,
        stores=stores,
        host=host,
        runner=runner,
        auth_policy=policy,
        embedding_provider=embedding_provider,
        embedding_model=embedding_model,
        embedding_base_url=embedding_base_url,
        embedding_error=embedding_error,
        embeddings_client=embeddings_client,
        telegram_bridge=telegram_bridge,
        email_bridge=email_bridge,
    )


def reload_gateway_workflow_bundles() -> Dict[str, Any]:
    """Reload private and catalog bundles for all instantiated Gateway services."""
    with _service_lock:
        services = []
        if _service is not None:
            services.append(_service)
        services.extend(list(_services_by_principal.values()))
    results: list[Dict[str, Any]] = []
    for svc in services:
        host = getattr(svc, "host", None)
        reload_fn = getattr(host, "reload_bundles_from_disk", None)
        if not callable(reload_fn):
            continue
        try:
            result = dict(reload_fn() or {})
            result["data_dir"] = str(getattr(svc.config, "data_dir", ""))
            results.append(result)
        except Exception as e:
            results.append({"ok": False, "data_dir": str(getattr(svc.config, "data_dir", "")), "error": str(e)})
    return {"ok": all(bool(r.get("ok", True)) for r in results), "services": results}


def start_gateway_runner() -> None:
    if gateway_multi_user_enabled():
        # Per-principal services are created and started lazily once auth resolves
        # the current user. Starting a process-wide service here would recreate
        # the singleton data-plane that hosted mode is meant to avoid.
        return
    svc = get_gateway_service()
    svc.runner.start()
    # Optional: backlog execution runner (consumes backlog_exec_queue and executes requests).
    global _backlog_exec_runner, _backlog_exec_runner_error
    try:
        if _backlog_exec_runner is None:
            from .maintenance.backlog_exec_runner import BacklogExecRunner, BacklogExecRunnerConfig

            cfg = BacklogExecRunnerConfig.from_env()
            if cfg.enabled:
                _backlog_exec_runner = BacklogExecRunner(gateway_data_dir=svc.stores.base_dir, cfg=cfg)
                _backlog_exec_runner.start()
                _backlog_exec_runner_error = None
    except Exception as e:
        # Best-effort: never break the gateway runner start if maintenance runner fails.
        _backlog_exec_runner = None
        try:
            _backlog_exec_runner_error = str(e)
        except Exception:
            pass
    bridge = getattr(svc, "telegram_bridge", None)
    if bridge is not None:
        bridge.start()
    email_bridge = getattr(svc, "email_bridge", None)
    if email_bridge is not None:
        email_bridge.start()


def stop_gateway_runner() -> None:
    global _service, _backlog_exec_runner, _backlog_exec_runner_error
    try:
        services = []
        if _service is not None:
            services.append(_service)
        services.extend(list(_services_by_principal.values()))
        if not services:
            return
        try:
            if _backlog_exec_runner is not None:
                _backlog_exec_runner.stop()
        except Exception:
            pass
        _backlog_exec_runner = None
        _backlog_exec_runner_error = None
        for svc in services:
            _stop_gateway_service_instance(svc)
    finally:
        with _service_lock:
            _service = None
            _services_by_principal.clear()


def _stop_gateway_service_instance(service: GatewayService) -> None:
    try:
        try:
            bridge = getattr(service, "telegram_bridge", None)
            if bridge is not None:
                bridge.stop()
        except Exception:
            pass
        try:
            bridge2 = getattr(service, "email_bridge", None)
            if bridge2 is not None:
                bridge2.stop()
        except Exception:
            pass
        try:
            service.runner.stop()
        except Exception:
            pass
    except Exception:
        pass


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
        "flow_warnings": None,
        # Best-effort pause metadata. We intentionally do not return full run.vars over HTTP.
        "paused": False,
        "pause_reason": None,
        "paused_at": None,
        "resumed_at": None,
        "waiting": None,
        # Best-effort schedule metadata (only for scheduled parent runs).
        "is_scheduled": False,
        "schedule": None,
        # Best-effort limits metadata (for UX, not for enforcing).
        "limits": None,
        # First-class authoring/execution lifecycle summary. This is a sanitized
        # projection of vars._run_lifecycle, not the full run input payload.
        "run_lifecycle": None,
        "is_draft": False,
    }
    try:
        lifecycle = extract_run_lifecycle(getattr(run, "vars", None))
        if lifecycle is not None:
            out["run_lifecycle"] = lifecycle
            out["is_draft"] = is_draft_run_lifecycle(lifecycle)
    except Exception:
        pass
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

    try:
        vars_obj = getattr(run, "vars", None)
        raw_warnings = vars_obj.get("_flow_warnings") if isinstance(vars_obj, dict) else None
        if isinstance(raw_warnings, list):
            cleaned: list[str] = []
            for w in raw_warnings:
                if not isinstance(w, str):
                    continue
                s = w.strip()
                if s:
                    cleaned.append(s)
            if cleaned:
                out["flow_warnings"] = cleaned
    except Exception:
        pass

    # Schedule + limits are safe, small subsets for UI. Never return full run.vars.
    try:
        vars_obj = getattr(run, "vars", None)
        if isinstance(vars_obj, dict):
            meta = vars_obj.get("_meta")
            schedule = meta.get("schedule") if isinstance(meta, dict) else None
            if isinstance(schedule, dict) and schedule.get("kind") == "scheduled_run":
                out["is_scheduled"] = True
                out["schedule"] = {
                    "kind": "scheduled_run",
                    "interval": schedule.get("interval"),
                    "repeat_count": schedule.get("repeat_count"),
                    "repeat_until": schedule.get("repeat_until"),
                    "start_at": schedule.get("start_at"),
                    "share_context": schedule.get("share_context"),
                    "target_workflow_id": schedule.get("target_workflow_id"),
                    "target_bundle_ref": schedule.get("target_bundle_ref"),
                    "target_flow_id": schedule.get("target_flow_id"),
                    "created_at": schedule.get("created_at"),
                    "updated_at": schedule.get("updated_at"),
                }
            else:
                wid = getattr(run, "workflow_id", None)
                if isinstance(wid, str) and wid.startswith("scheduled:"):
                    out["is_scheduled"] = True

            limits = vars_obj.get("_limits")
            if isinstance(limits, dict):
                used = limits.get("estimated_tokens_used")
                max_tokens = limits.get("max_tokens")
                max_input = limits.get("max_input_tokens")
                warn_pct = limits.get("warn_tokens_pct")
                budget = max_input if max_input is not None else max_tokens
                pct = None
                try:
                    used_i = int(used) if used is not None and not isinstance(used, bool) else None
                    budget_i = int(budget) if budget is not None and not isinstance(budget, bool) else None
                    if used_i is not None and budget_i is not None and budget_i > 0:
                        pct = float(used_i) / float(budget_i)
                except Exception:
                    pct = None
                out["limits"] = {
                    "tokens": {
                        "estimated_used": used,
                        "max_tokens": max_tokens,
                        "max_input_tokens": max_input,
                        "pct": pct,
                        "warn_tokens_pct": warn_pct,
                    }
                }
    except Exception:
        pass
    if waiting is not None:
        out["waiting"] = {
            "reason": getattr(getattr(waiting, "reason", None), "value", None) or str(getattr(waiting, "reason", "")),
            "wait_key": getattr(waiting, "wait_key", None),
            "until": getattr(waiting, "until", None),
            "prompt": getattr(waiting, "prompt", None),
            "choices": getattr(waiting, "choices", None),
            "allow_free_text": getattr(waiting, "allow_free_text", None),
            "details": getattr(waiting, "details", None),
        }
    return out
