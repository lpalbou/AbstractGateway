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
    agora_bridge: Optional[Any] = None
    entity_registry: Optional[Any] = None
    entity_chat_host: Optional[Any] = None
    entity_visit_host: Optional[Any] = None
    entity_meet_host: Optional[Any] = None


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


def gateway_runner_health_snapshot() -> Dict[str, Any]:
    """Peek-only runner liveness for the public health surface.

    Reports on ALREADY-INSTANTIATED services (never constructs one — a public,
    unauthenticated liveness probe must not trigger bundle compilation or store
    creation). The incident class this surfaces: a run-accepting gateway whose
    runner lost the singleton lock to another process and silently ticks
    nothing (runs hang forever on their entry node with zero ledger records).
    """
    with _service_lock:
        services = []
        if _service is not None:
            services.append(_service)
        services.extend(list(_services_by_principal.values()))

    runners: list[Dict[str, Any]] = []
    degraded = False
    for svc in services:
        runner = getattr(svc, "runner", None)
        status_fn = getattr(runner, "runner_status", None)
        if not callable(status_fn):
            continue
        try:
            st = dict(status_fn() or {})
        except Exception as e:
            st = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        # Identify the service without leaking filesystem paths on a public route.
        cfg = getattr(svc, "config", None)
        st["tenant_id"] = str(getattr(cfg, "tenant_id", "") or "") or None
        st["runtime_id"] = str(getattr(cfg, "runtime_id", "") or "") or None
        if st.get("status") == "degraded_no_ticker":
            degraded = True
        runners.append(st)
    return {"initialized": bool(runners), "degraded": degraded, "runners": runners}


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
        framework_flows_dir=base.framework_flows_dir or base.flows_dir,
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
        # F3: the steer sidecar cache outlives the service — after a root
        # purge a stale instance would append into a table that no longer
        # exists (dead steering until process restart). Evict with the root.
        try:
            from .steering import evict_steer_sidecar

            evict_steer_sidecar(svc.config.data_dir)
        except Exception:
            pass
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
    # Resolution now honors the admin runtime-config store (continuum c1550):
    # stored > env > default — so a launcher losing the env no longer blanks
    # the manager when the operator persisted it on (the c1526 incident).
    try:
        from .runtime_config import resolve_process_manager_enabled

        if resolve_process_manager_enabled(stores.base_dir):
            from .maintenance.process_manager import get_managed_env_var_manager

            get_managed_env_var_manager(base_dir=stores.base_dir)
    except Exception:
        pass

    # Data & Caches writer wave (operator priority 2026-07-13 18:19, agency
    # c1580 ask 1a): register this data root's gateway-owned homes in the
    # machine-level registry at BOOT — artifacts (load-bearing), logs,
    # workspaces, every entity home (safe_to_purge=False by construction).
    # Best-effort: a broken registry never blocks a boot.
    try:
        from .data_homes import register_gateway_data_homes

        register_gateway_data_homes(stores.base_dir)
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
        framework_bundles_dir = getattr(cfg, "framework_flows_dir", None)
        if framework_bundles_dir is not None:
            framework_bundles_dir = Path(framework_bundles_dir).expanduser().resolve()
            if framework_bundles_dir == Path(cfg.flows_dir).expanduser().resolve():
                framework_bundles_dir = None
        host = WorkflowBundleGatewayHost.load_from_dir(
            bundles_dir=cfg.flows_dir,
            data_dir=cfg.data_dir,
            framework_bundles_dir=framework_bundles_dir,
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

    # Agora hub bridge (hooks plan P2): identity-carrying transport that wakes
    # gateway-hosted resident runs on hub traffic. Disabled = None (normal).
    from .integrations.agora_bridge import build_agora_bridge

    agora_bridge = build_agora_bridge(base_dir=cfg.data_dir, runner=runner, host=host)

    # Entity lifecycle (a2a 0004): the registry hosts entity homes under this
    # service's data dir; the routing installer claims the MEMORY_* seam +
    # DIARY_* effect types on the host runtime and refuses to shadow existing
    # claimants (a collision means the wiring drifted — loud by design).
    from .entities import EntityRegistry
    from .entity_chat import EntityChatHost
    from .entity_gate import install_entity_routing
    from .entity_meets import EntityMeetHost
    from .entity_visits import EntityVisitHost
    from .users import gateway_user_registry_path_from_env

    # Entity principals (GW-H) must land in the file the AUTH layer reads —
    # resolve it from the SAME resolver auth uses, so a per-principal data
    # root never grows a private users file authentication ignores.
    entity_registry = EntityRegistry(
        data_dir=cfg.data_dir,
        users_registry_path=gateway_user_registry_path_from_env(),
        # The gateway ROOT (config-object endpoint-profile lane, agency c753):
        # under user auth cfg.data_dir is the per-principal runtime root, but
        # gateway-scoped endpoint profiles live at root_data_dir. Single-user
        # layouts set root_data_dir==data_dir (config.py:167).
        root_data_dir=getattr(cfg, "root_data_dir", None) or cfg.data_dir,
    )
    install_entity_routing(
        host.runtime,
        registry=entity_registry,
        run_store=stores.run_store,
        artifact_store=stores.artifact_store,
    )
    entity_chat_host = EntityChatHost(entity_registry)
    # Durable-visit + meet hosts constructed ONCE at the factory (frozen
    # dataclass lesson): a per-request host would drop the in-process
    # open-locks and the meet index. The meet host shares the visit host so
    # a meet leg and a solo open on one home take the same per-slug lock.
    entity_visit_host = EntityVisitHost(entity_registry)
    entity_meet_host = EntityMeetHost(entity_visit_host)

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
        agora_bridge=agora_bridge,
        entity_registry=entity_registry,
        entity_chat_host=entity_chat_host,
        entity_visit_host=entity_visit_host,
        entity_meet_host=entity_meet_host,
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


def sync_backlog_exec_runner(*, data_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Reconcile the backlog exec runner with the RESOLVED config (stored >
    env — runtime_config's chain). Called at boot AND after every admin
    runtime-config write, so continuum's Settings toggle takes effect
    LIVE instead of demanding a serve restart (the exact gap the operator's
    'no execution agent' screenshot showed). Idempotent: enabled+running =
    no-op; disabled+running = stop; enabled+stopped/config-changed =
    (re)start with the fresh config."""
    global _backlog_exec_runner, _backlog_exec_runner_error
    try:
        from .maintenance.backlog_exec_runner import BacklogExecRunner, BacklogExecRunnerConfig

        base_dir = Path(data_dir) if data_dir is not None else Path(GatewayHostConfig.from_env().data_dir)
        cfg = BacklogExecRunnerConfig.from_gateway(base_dir)
        runner = _backlog_exec_runner
        if not cfg.enabled:
            if runner is not None:
                runner.stop()
                _backlog_exec_runner = None
            _backlog_exec_runner_error = None
            return {"enabled": False, "alive": False}
        if runner is not None and runner.is_running() and runner.cfg == cfg:
            return {"enabled": True, "alive": True}
        if runner is not None:
            runner.stop()
        _backlog_exec_runner = BacklogExecRunner(gateway_data_dir=base_dir, cfg=cfg)
        _backlog_exec_runner.start()
        _backlog_exec_runner_error = None
        return {"enabled": True, "alive": _backlog_exec_runner.is_running(), "executor": cfg.executor}
    except Exception as e:
        # Best-effort: never break the caller if the maintenance runner fails.
        _backlog_exec_runner = None
        try:
            _backlog_exec_runner_error = str(e)
        except Exception:
            pass
        return {"enabled": False, "alive": False, "error": str(e)}


def start_gateway_runner() -> None:
    if gateway_multi_user_enabled():
        # Per-principal services are created and started lazily once auth resolves
        # the current user. Starting a process-wide service here would recreate
        # the singleton data-plane that hosted mode is meant to avoid.
        # The BACKLOG EXEC RUNNER is deliberately not per-principal: the
        # queue lives at the base data dir and executions run on the host —
        # under user-auth the old early return silently never started it
        # (the operator's "no execution agent" incident, 2026-07-14 21:09).
        sync_backlog_exec_runner()
        return
    svc = get_gateway_service()
    svc.runner.start()
    # Optional: backlog execution runner (consumes backlog_exec_queue and executes requests).
    sync_backlog_exec_runner(data_dir=Path(svc.stores.base_dir))
    bridge = getattr(svc, "telegram_bridge", None)
    if bridge is not None:
        bridge.start()
    email_bridge = getattr(svc, "email_bridge", None)
    if email_bridge is not None:
        email_bridge.start()
    agora_bridge = getattr(svc, "agora_bridge", None)
    if agora_bridge is not None:
        agora_bridge.start()


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
            bridge3 = getattr(service, "agora_bridge", None)
            if bridge3 is not None:
                bridge3.stop()
        except Exception:
            pass
        try:
            service.runner.stop()
        except Exception:
            pass
        try:
            chat_host = getattr(service, "entity_chat_host", None)
            if chat_host is not None:
                # Reflect + close live visits FIRST (they hold their own home
                # handles and may owe the own-time loop a wake).
                chat_host.close_all()
        except Exception:
            pass
        try:
            registry = getattr(service, "entity_registry", None)
            if registry is not None:
                registry.close_all()
        except Exception:
            pass
    except Exception:
        pass


def _summarize_run_output(value: Any, *, max_string: int = 50_000, max_items: int = 80, max_depth: int = 8) -> Any:
    """Return an HTTP-safe, bounded projection of a run's final output."""

    def _trunc(text: str) -> str:
        if len(text) <= max_string:
            return text
        return text[:max_string].rstrip() + f"\n#TRUNCATION: output string truncated from {len(text)} chars"

    def _walk(cur: Any, *, depth: int) -> Any:
        if depth > max_depth:
            return "#TRUNCATION: output depth limit reached"
        if cur is None or isinstance(cur, (bool, int, float)):
            return cur
        if isinstance(cur, str):
            return _trunc(cur)
        if isinstance(cur, (list, tuple)):
            out = [_walk(item, depth=depth + 1) for item in list(cur)[:max_items]]
            if len(cur) > max_items:
                out.append(f"#TRUNCATION: output list truncated from {len(cur)} items")
            return out
        if isinstance(cur, dict):
            out: Dict[str, Any] = {}
            items = list(cur.items())
            for key, item in items[:max_items]:
                out[str(key)] = _walk(item, depth=depth + 1)
            if len(items) > max_items:
                out["#TRUNCATION"] = f"output object truncated from {len(items)} keys"
            return out
        return _trunc(str(cur))

    return _walk(value, depth=0)


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
        "output": None,
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
    try:
        output = getattr(run, "output", None)
        if output is not None:
            out["output"] = _summarize_run_output(output)
    except Exception:
        out["output"] = None
    return out
