from __future__ import annotations

import logging
import os
import sys
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
    # Partial-boot honesty (resilience wave 2026-07-21, adversary P2-6): each
    # best-effort factory step that failed-and-continued records one labeled
    # line here; /api/health surfaces them so a boot that quietly runs
    # without embeddings/shipped-catalog/self-repair is VISIBLE, not silent.
    boot_warnings: tuple = ()
    # Boot env scan report (env-kill phase 1): the structured classification
    # of the process environment (names only, never values) — the console's
    # env-inventory read; the warning lines derived from it ride boot_warnings.
    env_scan: Optional[Dict[str, Any]] = None


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


def principal_service_cached(principal: GatewayPrincipal) -> bool:
    """True when this principal's GatewayService already exists (cheap peek —
    the pre-warm middleware's gate; never constructs anything)."""
    with _service_lock:
        return _principal_service_key(principal) in _services_by_principal


class ServicePrewarmMiddleware:
    """Pure-ASGI middleware: build a principal's GatewayService OFF the event
    loop before the route runs (resilience wave 2026-07-21, adversary P1-2).

    Under multi-user auth, per-principal services are created lazily on first
    touch — a heavy synchronous boot (stores, bundle compilation, entity
    routing, embeddings). Route handlers call get_gateway_service() inline
    from async contexts, so that first touch used to run the whole boot ON
    the ASGI event loop: every other user's requests AND /api/health queued
    behind it. This middleware sits INSIDE GatewaySecurityMiddleware (the
    principal contextvar is set), detects the cache miss, and runs the
    creation in a worker thread; the route's inline call then hits the cache.

    Failure honesty: a pre-warm failure logs and falls through — the route's
    own inline call retries and surfaces the real error to the caller.
    """

    def __init__(self, app: Any) -> None:
        self._app = app

    async def __call__(self, scope, receive, send):  # noqa: ANN001 - ASGI signature
        if scope.get("type") != "http":
            return await self._app(scope, receive, send)
        if not str(scope.get("path") or "").startswith("/api/gateway"):
            return await self._app(scope, receive, send)
        try:
            # Boot gate (c4063 follow-up): while the background boot runs,
            # API requests wait for it OFF the event loop — their inline
            # get_gateway_service() then hits the cache instead of racing a
            # second synchronous boot on the loop. /api/health is not under
            # /api/gateway, so probes answer instantly throughout.
            if _boot_state == "starting":
                import asyncio

                await asyncio.to_thread(wait_for_gateway_boot)
            if gateway_multi_user_enabled():
                principal = current_gateway_principal()
                if principal is not None and not principal_service_cached(principal):
                    import asyncio

                    await asyncio.to_thread(get_gateway_service_for_principal, principal)
        except Exception:
            import logging

            logging.getLogger("abstractgateway.service").exception(
                "Service pre-warm failed; the route path will retry inline"
            )
        return await self._app(scope, receive, send)


def gateway_runner_health_snapshot() -> Dict[str, Any]:
    """Peek-only runner liveness for the public health surface.

    Reports on ALREADY-INSTANTIATED services (never constructs one — a public,
    unauthenticated liveness probe must not trigger bundle compilation or store
    creation). The incident class this surfaces: a run-accepting gateway whose
    runner lost the singleton lock to another process and silently ticks
    nothing (runs hang forever on their entry node with zero ledger records).
    """
    # Bounded acquire (adversary P0-1): the eager sweep and first-touch builds
    # hold _service_lock across heavy service construction; a public liveness
    # probe must never BLOCK behind a build (that re-opens the supervisor
    # false-recycle window). On contention, report building=true and return —
    # a gateway mid-build is alive, not degraded.
    if not _service_lock.acquire(timeout=0.5):
        return {"initialized": False, "degraded": False, "building": True, "runners": []}
    try:
        services = []
        if _service is not None:
            services.append(_service)
        services.extend(list(_services_by_principal.values()))
    finally:
        _service_lock.release()

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
        # Partial-boot honesty (P2-6): labeled degradations ride the probe so
        # a boot that silently lost embeddings/catalog/sweeper is visible.
        warnings = list(getattr(svc, "boot_warnings", ()) or ())
        emb_err = getattr(svc, "embedding_error", None)
        if emb_err:
            warnings.append(f"#FALLBACK embeddings unavailable: {emb_err}")
        if warnings:
            st["boot_warnings"] = warnings
        # Boot env scan (env-kill phase 1): COUNTS ONLY on this surface —
        # /api/health is unauthenticated and must not disclose which
        # credential/identity names this process holds (adversary F4); the
        # full names live in the boot log banner and the authenticated
        # console read of service.env_scan. A failed scan is flagged, never
        # silent (F5).
        scan = getattr(svc, "env_scan", None)
        if isinstance(scan, dict) and (scan.get("scanned") or scan.get("error")):
            st["env_scan"] = {
                "scanned": scan.get("scanned"),
                "foreign_count": len(scan.get("foreign") or []),
                "undeclared_count": len(scan.get("undeclared") or []),
                "legacy_alias_count": len(scan.get("legacy_alias") or []),
                "behavior_env_count": scan.get("behavior_env_count"),
                **({"error": True} if scan.get("error") else {}),
            }
        # Degraded states: locked out with no live ticker, worker thread dead
        # without a deliberate stop (resilience wave 2026-07-21), the status
        # probe itself erroring, or every tick worker wedged past the wedge
        # threshold (adversary B P1-1: run progression frozen while the loop
        # polls happily). All mean "runs accepted here may be ticked by
        # nobody" — the exact class this surface exists for.
        if st.get("status") in {"degraded_no_ticker", "dead_worker", "error"} or st.get("all_tick_workers_wedged"):
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


def _runner_needs_restart(runner: Any) -> bool:
    """True when an ENABLED runner is not actually ticking and no live peer
    is (backlog 0063): a start() that lost the singleton-lock race and
    returned dead, or a worker that never started. A refused lock with a
    fresh peer heartbeat (legit split/co-worker) is NOT a restart case, and
    a DELIBERATELY-stopped runner is NEVER restarted — the self-heal must
    not fight an operator stop (adversary P2-2). Never raises."""
    try:
        status_fn = getattr(runner, "runner_status", None)
        if not callable(status_fn):
            return False
        st = dict(status_fn() or {})
        if not st.get("enabled"):
            return False
        if bool(st.get("stopped_deliberately")):
            return False  # operator stop() — leave it stopped
        # active = holding the lock and looping; standby_peer_active = a live
        # peer ticks it; starting = mid-acquire. Everything else on an
        # enabled, not-deliberately-stopped runner (inactive-never-started /
        # dead_worker / degraded_no_ticker) means runs accepted here would
        # not be ticked — re-attempt start (idempotent: start() no-ops when
        # the thread is alive).
        # "paused" (host pause, 2026-09-05) is the operator's choice: the
        # loop is alive and holding the lock — never a restart case.
        return str(st.get("status")) not in {"active", "standby_peer_active", "starting", "paused"}
    except Exception:
        return False


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
        elif getattr(svc.config, "runner_enabled", False) and _runner_needs_restart(svc.runner):
            # Self-healing (backlog 0063): a cached service whose runner lost
            # the lock race and returned dead used to be returned as-is
            # forever — a permanent per-principal stall in split/multi-worker
            # topologies after a lock holder dies. Re-attempt start on access;
            # start() is idempotent (no-op when the worker thread is alive)
            # and the worker owns the whole retry/takeover lifecycle from
            # there. A failed restart never evicts a WORKING cache entry — the
            # service still serves reads; the next access retries.
            try:
                svc.runner.start()
            except Exception:
                import logging

                logging.getLogger("abstractgateway.service").warning(
                    "runner restart attempt failed for principal service %s (will retry next access)",
                    key,
                    exc_info=True,
                )
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

    # Partial-boot honesty ledger (adversary P2-6): every best-effort step
    # that fails-and-continues records one line; the service carries them to
    # /api/health. Append-only within this factory run.
    boot_warnings: list[str] = []

    # Boot env scan (env-kill phase 1; c4211 contamination class): classify
    # the process environment against the declared registry and WARN on
    # foreign/undeclared/legacy names — visibility only, never a gate (c4305).
    # Memoized process-wide (adversary F8: per-principal factories share one
    # scan; the banner logs once). Disclosure split (F4): full NAMES go to
    # the log banner + the in-process report; PUBLIC boot_warnings carry
    # counts only.
    env_scan_report: dict = {}
    try:
        from .env_scanner import env_scan_summary_warnings, log_env_scan_banner, scan_process_env_once

        first_scan = _service is None and not _services_by_principal
        env_scan_report = scan_process_env_once()
        if first_scan:
            log_env_scan_banner(env_scan_report)
        boot_warnings.extend(env_scan_summary_warnings(env_scan_report))
    except Exception as e:  # noqa: BLE001 - the scanner must never block boot
        boot_warnings.append(f"#FALLBACK boot env scan failed: {type(e).__name__}")

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
    except Exception as e:
        boot_warnings.append(f"#FALLBACK process-manager env overrides not applied: {type(e).__name__}: {e}")

    # Data & Caches writer wave (operator priority 2026-07-13 18:19, agency
    # c1580 ask 1a): register this data root's gateway-owned homes in the
    # machine-level registry at BOOT — artifacts (load-bearing), logs,
    # workspaces, every entity home (safe_to_purge=False by construction).
    # Best-effort: a broken registry never blocks a boot.
    try:
        from .data_homes import register_gateway_data_homes

        register_gateway_data_homes(stores.base_dir)
    except Exception as e:
        boot_warnings.append(f"#FALLBACK data-homes registry registration failed: {type(e).__name__}: {e}")

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

        # Fresh-install UX (card 013): ensure the shipped catalog bundles
        # (docs-qa — the console drawer's transport) are published into THIS
        # tenant's catalog before the host scans the catalog dir, so a fresh
        # data root answers docs questions without an admin curl. Publish-if-
        # absent semantics: restarts never churn records, admin default
        # pointers/tombstones/attribution are never touched, conflicts warn
        # loudly and never block boot. Per-tenant services created lazily
        # run the same hook for their own tenant.
        try:
            from .shipped_catalog import ensure_shipped_catalog_bundles

            ensure_shipped_catalog_bundles(
                root_data_dir=root_data_dir, tenant_id=tenant_id, flows_dir=Path(cfg.flows_dir)
            )
        except Exception as e:
            import logging

            logging.getLogger("abstractgateway.service").warning(
                "shipped-catalog boot publish failed (continuing)", exc_info=True
            )
            boot_warnings.append(f"#FALLBACK shipped-catalog boot publish failed: {type(e).__name__}: {e}")

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

    # Bridges are NON-FATAL boot dependencies (resilience wave 2026-07-21,
    # adversary B P1-2): an enabled-but-misconfigured chat bridge used to
    # raise out of the composition root and abort ALL serving (plain runs,
    # entities, health) — disproportionate to a bridge's blast radius. Each
    # bridge failure degrades to disabled with a labeled boot warning that
    # rides /api/health; the process serves.
    telegram_bridge = None
    enabled_raw = os.getenv("ABSTRACT_TELEGRAM_BRIDGE")
    if enabled_raw is not None and str(enabled_raw).strip().lower() in {"1", "true", "yes", "on"}:
        try:
            from .integrations.telegram_bridge import TelegramBridge, TelegramBridgeConfig

            tcfg = TelegramBridgeConfig.from_env(base_dir=cfg.data_dir)
            if not tcfg.flow_id:
                raise RuntimeError("ABSTRACT_TELEGRAM_FLOW_ID is required when ABSTRACT_TELEGRAM_BRIDGE=1")
            telegram_bridge = TelegramBridge(config=tcfg, host=host, runner=runner, artifact_store=stores.artifact_store)
        except Exception as e:
            import logging

            logging.getLogger("abstractgateway.service").exception("Telegram bridge failed to boot; disabled")
            boot_warnings.append(f"#FALLBACK telegram bridge disabled: {type(e).__name__}: {e}")
            telegram_bridge = None

    email_bridge = None
    email_enabled_raw = os.getenv("ABSTRACT_EMAIL_BRIDGE")
    if email_enabled_raw is not None and str(email_enabled_raw).strip().lower() in {"1", "true", "yes", "on"}:
        try:
            from .integrations.email_bridge import EmailBridge, EmailBridgeConfig

            ecfg = EmailBridgeConfig.from_env(base_dir=cfg.data_dir)
            email_bridge = EmailBridge(config=ecfg, host=host, runner=runner, artifact_store=stores.artifact_store)
        except Exception as e:
            import logging

            logging.getLogger("abstractgateway.service").exception("Email bridge failed to boot; disabled")
            boot_warnings.append(f"#FALLBACK email bridge disabled: {type(e).__name__}: {e}")
            email_bridge = None

    # Agora hub bridge (hooks plan P2): identity-carrying transport that wakes
    # gateway-hosted resident runs on hub traffic. Disabled = None (normal).
    agora_bridge = None
    try:
        from .integrations.agora_bridge import build_agora_bridge

        agora_bridge = build_agora_bridge(base_dir=cfg.data_dir, runner=runner, host=host)
    except Exception as e:
        import logging

        logging.getLogger("abstractgateway.service").exception("Agora bridge failed to boot; disabled")
        boot_warnings.append(f"#FALLBACK agora bridge disabled: {type(e).__name__}: {e}")
        agora_bridge = None

    # Entity lifecycle (a2a 0004): the registry hosts entity homes under this
    # service's data dir; the routing installer claims the MEMORY_* seam +
    # DIARY_* effect types on the host runtime and refuses to shadow existing
    # claimants (a collision means the wiring drifted — loud by design).
    #
    # GUARDED boot dependency (resilience wave 2026-07-21, adversary B P0-1):
    # this block was the ONE unguarded subsystem in the factory — an entity
    # import/collision failure aborted lifespan startup and NOTHING served
    # (plain workflow runs included). Only the store layer and the runner are
    # load-bearing for "serve runs"; the entity lane degrades to
    # labeled-disabled (entity routes 503 via their registry==None handling)
    # while everything else keeps working. The failure stays LOUD: log +
    # boot_warnings on /api/health.
    entity_registry = None
    entity_chat_host = None
    entity_visit_host = None
    entity_meet_host = None
    try:
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
        # A bundle reload swaps host.runtime for a BRAND-NEW instance
        # (reload_bundles_from_disk) — without this hook the entity handlers
        # die with the old runtime and every entity run after a catalog
        # publish fails "No effect handler registered for memory_recall"
        # (live defect, 2026-07-24). The hook re-arms each rebuilt runtime
        # BEFORE it is published; registry/stores are service-scoped objects
        # that survive reloads, so the closure stays valid for the service's
        # whole life.
        host.add_runtime_rebuild_hook(
            # Default-arg binding is deliberate: freeze the registry OBJECT at
            # registration time so a later `entity_registry = None` in the
            # degrade path can never turn this hook into a dead-registry arm.
            lambda rt, _reg=entity_registry, _rs=stores.run_store, _as=stores.artifact_store: install_entity_routing(
                rt,
                registry=_reg,
                run_store=_rs,
                artifact_store=_as,
            )
        )
        # The summon-seat probe (conversation-seat plan item 5: one seat,
        # three doors): visit/chat opens refuse while a summon turn is
        # mid-flight on the home. Live-run-only by the probe's contract —
        # a TTL-idle seat never blocks the drawer's own surfaces.
        from .entity_seat import build_summon_seat_probe

        summon_seat_probe = build_summon_seat_probe(stores.run_store, entity_registry.entities_dir)
        entity_chat_host = EntityChatHost(entity_registry, summon_seat_probe=summon_seat_probe)
        # Durable-visit + meet hosts constructed ONCE at the factory (frozen
        # dataclass lesson): a per-request host would drop the in-process
        # open-locks and the meet index. The meet host shares the visit host so
        # a meet leg and a solo open on one home take the same per-slug lock.
        # chat_probe wires the hosted lane into the visit reaper's stale-yield
        # repair (c2465: an orphaned auto-yield posture must never be repaired
        # out from under a LIVE drawer session — and never left forever either).
        entity_visit_host = EntityVisitHost(
            entity_registry,
            chat_probe=entity_chat_host.has_open,
            summon_seat_probe=summon_seat_probe,
        )
        # The visit-queue sweeper backstop (decision:summon-queue-v1 inv 11):
        # armed at boot so PARKED entries survive a bounce with zero client
        # traffic (poll-driven admission covers watched queues; this clock
        # covers the mailbox drops and dead pollers). Lazy no-op when no
        # queue files exist; the executor is wired by routes/entities at its
        # import (which the app's router include guarantees precedes any
        # traffic).
        from .entity_queue import ensure_queue_sweeper

        ensure_queue_sweeper(entity_registry.entities_dir)
        entity_meet_host = EntityMeetHost(entity_visit_host)
    except Exception as e:
        import logging

        logging.getLogger("abstractgateway.service").exception(
            "Entity subsystem failed to boot; entity routes degrade while runs keep serving"
        )
        boot_warnings.append(f"#FALLBACK entity subsystem not available: {type(e).__name__}: {e}")
        entity_registry = None
        entity_chat_host = None
        entity_visit_host = None
        entity_meet_host = None

    # Self-repair sweeper (laurent 2026-07-21: "you should self-repair the
    # entity"): respawns loops that died WITHOUT the operator's word
    # (failure cull / crash), guarded + circuit-broken. In-process daemon
    # thread — dies with the serve process, never machine persistence.
    if entity_registry is not None:
        try:
            from .entity_repair import start_repair_sweeper

            start_repair_sweeper(entity_registry)
        except Exception as e:  # noqa: BLE001 - a broken sweeper must not block serving
            import logging

            logging.getLogger(__name__).exception("entity self-repair sweeper failed to start")
            boot_warnings.append(f"#FALLBACK entity self-repair sweeper not running: {type(e).__name__}: {e}")

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
        boot_warnings=tuple(boot_warnings),
        env_scan=env_scan_report or None,
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


# ---------------------------------------------------------------------------
# Boot state (resilience wave 2026-07-21, framework's live kill-proof
# follow-up c4063): uvicorn accepts NO connections until lifespan startup
# yields, and start_gateway_runner() used to run the whole heavy boot
# (entity-home load included) synchronously inside lifespan — so during a
# long boot /api/health was connection-refused and the supervisor counted
# probe misses against a healthy-but-loading gateway (>60s boot = false
# recycle). The boot now runs on a background thread; lifespan yields
# immediately; /api/health answers status="starting"; API requests wait
# for boot OFF the event loop (health never queues behind them).
# ---------------------------------------------------------------------------

_boot_state: str = "idle"
_boot_error: Optional[str] = None
_boot_done = threading.Event()


def gateway_boot_state() -> Dict[str, Any]:
    """{"state": idle|starting|ready|failed, "error": str|None} — pure read."""
    return {"state": _boot_state, "error": _boot_error}


def begin_gateway_boot() -> None:
    """Start the heavy service boot on a background thread (idempotent while
    starting; a failed/idle state boots fresh). Lifespan calls this and
    yields immediately so the listener opens and health probes answer."""
    global _boot_state, _boot_error
    if _boot_state == "starting":
        return
    _boot_state = "starting"
    _boot_error = None
    _boot_done.clear()

    def _boot() -> None:
        global _boot_state, _boot_error
        try:
            # Host pause (tray/console, 2026-09-05): a persisted pause must
            # be in force BEFORE the first runner schedules a tick.
            try:
                from . import host_control
                from .users import gateway_data_dir_from_env

                snap = host_control.configure(gateway_data_dir_from_env())
                if snap.get("paused"):
                    print(
                        "[WARN] gateway execution is PAUSED (persisted from a previous run"
                        f"{', by ' + str(snap.get('paused_by')) if snap.get('paused_by') else ''}); "
                        "runs queue until resumed from the tray or Console → Resources.",
                        file=sys.stderr,
                        flush=True,
                    )
            except Exception:  # noqa: BLE001 - the pause file is a courtesy, never a boot blocker
                logging.getLogger("abstractgateway.service").warning("host pause state could not be loaded", exc_info=True)
            start_gateway_runner()
            _boot_state = "ready"
        except Exception as e:
            _boot_state = "failed"
            _boot_error = f"{type(e).__name__}: {e}"
            import logging

            logging.getLogger("abstractgateway.service").exception("gateway boot failed")
        finally:
            _boot_done.set()

    threading.Thread(target=_boot, name="gateway-boot", daemon=True).start()


def wait_for_gateway_boot(timeout_s: float = 300.0) -> str:
    """Block until boot settles (or timeout); returns the boot state. Callers
    on the event loop must wrap in asyncio.to_thread."""
    _boot_done.wait(timeout=max(0.0, float(timeout_s)))
    return _boot_state


def reset_gateway_boot_state() -> None:
    """Lifespan shutdown hygiene: the next startup boots fresh (TestClient
    reuses module state across contexts; a stale 'ready' would skip boot)."""
    global _boot_state, _boot_error
    _boot_state = "idle"
    _boot_error = None
    _boot_done.clear()


_rehydrate_shutdown = threading.Event()
_rehydrate_result: Dict[str, Any] = {}


def rehydration_status() -> Dict[str, Any]:
    """Last eager-rehydration outcome for /api/health (backlog 0063,
    adversary P1-3: a failed warm is invisible otherwise — the idle tenants
    this feature serves never send the request that would retry)."""
    return dict(_rehydrate_result)


def _principal_runtime_dir_exists(principal: GatewayPrincipal) -> bool:
    """Cheap peek: does this principal's per-runtime data dir already exist?
    (adversary P1-2: warming a never-run user/entity MKDIRs a phantom
    runtime tree + standing threads scaling with registrations, not with
    parked work). Never build to find out."""
    try:
        cfg = _config_for_principal(principal)
        base = Path(getattr(cfg, "data_dir", "") or "")
        return bool(str(base)) and base.exists()
    except Exception:
        return False


def _eager_rehydrate_principal_runners() -> Dict[str, Any]:
    """Boot-time re-arm of per-principal runners (backlog 0063).

    In multi-user mode per-principal services (and their runner threads)
    were created only on that principal's first authenticated request — so
    after a crash/redeploy every idle tenant's in-flight and scheduled runs
    stayed paused until that user happened to hit an endpoint, and
    WAIT_UNTIL/event deadlines could miss their windows indefinitely. This
    warms each registered runtime's service on boot so its runner ticks
    parked work immediately.

    Bounded + best-effort: runs on the background boot thread (never blocks
    the listener), sequential (naturally one-at-a-time — no thundering
    herd), capped by ABSTRACTGATEWAY_EAGER_REHYDRATE_MAX (0 disables), and a
    per-principal failure never blocks the others or the boot. Each
    principal has its OWN per-runtime data dir, so their singleton locks
    never contend — warming N runners is N independent locks, not a race.
    """
    import logging

    logger = logging.getLogger("abstractgateway.service")
    result: Dict[str, Any] = {"attempted": 0, "started": 0, "skipped_by_cap": 0, "errors": []}
    try:
        raw = os.getenv("ABSTRACTGATEWAY_EAGER_REHYDRATE_MAX")
        cap = int(str(raw).strip()) if raw and str(raw).strip() else 32
    except ValueError:
        cap = 32
    if cap <= 0:
        _rehydrate_result.clear()
        _rehydrate_result.update(result)
        return result
    # A no-runner API process (split mode) must NOT do N heavy builds for
    # zero ticking value (adversary angle C).
    try:
        if not bool(getattr(GatewayHostConfig.from_env(), "runner_enabled", True)):
            result["skipped_reason"] = "runner disabled in this process (split mode)"
            _rehydrate_result.clear()
            _rehydrate_result.update(result)
            return result
    except Exception:
        pass
    try:
        from .users import GatewayUserRegistry

        users = GatewayUserRegistry().list_users()
    except Exception as e:
        logger.warning("eager rehydration could not list users: %s", e, exc_info=True)
        result["errors"].append(f"list_users: {type(e).__name__}: {e}")
        _rehydrate_result.clear()
        _rehydrate_result.update(result)
        return result

    # Filter BEFORE the cap (adversary P2-1: cap-then-filter let disabled
    # records at the front of the sorted list starve the enabled ones).
    # Entities are enabled registry users by construction but their runtime
    # plane is the per-home store, NOT users/<tenant>/<slug>/runtime — warming
    # them mints phantom trees (adversary P1-2); skip role=entity. And warm
    # only principals whose runtime dir ALREADY exists — never mkdir a tree
    # for a user who has never run.
    candidates = []
    for rec in users:
        if not getattr(rec, "enabled", True):
            continue
        if "entity" in {str(r).strip().lower() for r in (getattr(rec, "roles", ()) or ())}:
            continue
        candidates.append(rec)

    for rec in candidates[cap:]:
        result["skipped_by_cap"] += 1
    if result["skipped_by_cap"]:
        logger.warning(
            "eager rehydration cap %s reached: %s principal(s) not warmed at boot "
            "(they warm lazily on first request)",
            cap,
            result["skipped_by_cap"],
        )

    for rec in candidates[:cap]:
        if _rehydrate_shutdown.is_set():
            result["stopped"] = "shutdown"
            break
        try:
            principal = rec.to_principal(token_fingerprint_value="")
            if not _principal_runtime_dir_exists(principal):
                continue  # never-run principal — no parked work, no mkdir on warm
            result["attempted"] += 1
            svc = get_gateway_service_for_principal(principal)
            # get_gateway_service_for_principal already starts the runner on
            # first build and self-heals a dead one; count a live ticker.
            if not _runner_needs_restart(getattr(svc, "runner", None)):
                result["started"] += 1
        except Exception as e:
            result["errors"].append(f"{rec.tenant_id}/{rec.user_id}: {type(e).__name__}: {e}")
    if result["errors"]:
        logger.warning(
            "eager rehydration: %s principal(s) failed to warm: %s",
            len(result["errors"]),
            "; ".join(result["errors"][:5]),
        )
    _rehydrate_result.clear()
    _rehydrate_result.update(result)
    return result


def start_gateway_runner() -> None:
    # Effective-spec crash-window reconcile (structural-edit build c4859):
    # the blueprint overlay + derived effective file are two atomic writes;
    # a crash between them (or a re-vendor under a standing overlay) leaves
    # the FILE detached loops read stale. Heal at boot BEFORE any loop
    # process reads it — GET reconciles too, but loops don't call GET.
    # data_dir passed explicitly: multi-user boot keeps services lazy, so
    # the heal must not force a service build. Never raises by construction.
    try:
        from .routes.entities import reconcile_effective_spec_file

        reconcile_effective_spec_file(data_dir=Path(GatewayHostConfig.from_env().data_dir))
    except Exception:  # noqa: BLE001 - boot must never die on a heal pass
        logger.warning("effective phase-spec reconcile skipped at boot", exc_info=True)
    # Host pause state (tray/console, 2026-09-05): bind + load BEFORE any
    # runner schedules a tick. Here rather than only in the boot thread so
    # the split `abstractgateway runner` process (which never runs the
    # lifespan boot) honours a pause written by the API process.
    try:
        from . import host_control

        host_control.configure(Path(GatewayHostConfig.from_env().data_dir))
    except Exception:  # noqa: BLE001 - the pause file is a courtesy, never a boot blocker
        logger.warning("host pause state could not be loaded", exc_info=True)
    if gateway_multi_user_enabled():
        # Per-principal services are created and started lazily on first
        # request; the BACKLOG EXEC RUNNER lives at the base data dir and is
        # NOT per-principal — under user-auth the old early return silently
        # never started it (the "no execution agent" incident 2026-07-14).
        sync_backlog_exec_runner()
        # Eager re-arm (backlog 0063): warm every registered runtime's runner
        # so parked/scheduled runs resume on boot. Runs on its OWN daemon
        # thread, NOT inline (adversary P0-1: the sweep does N heavy builds
        # under _service_lock — running it inline in the boot thread held the
        # boot gate "starting" for the whole sweep, parking every
        # /api/gateway/* request behind minutes of builds and re-opening the
        # false-recycle window the background-boot fix just closed). The
        # sweep is best-effort and every service insert is serialized by
        # _service_lock, so racing live prewarms is already safe.
        _rehydrate_shutdown.clear()
        threading.Thread(
            target=_eager_rehydrate_principal_runners,
            name="gateway-eager-rehydrate",
            daemon=True,
        ).start()
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
    # Tell the eager-rehydration sweep to stop (adversary P1-1: the sweep runs
    # on its own thread and, unsignalled, kept building services AFTER
    # shutdown returned — orphaned runners holding per-principal flocks that
    # flock-refuse the next boot's own twins). Checked per sweep iteration.
    _rehydrate_shutdown.set()
    try:
        # Snapshot + clear the caches ATOMICALLY under the lock (adversary
        # P1-1): the old unlocked snapshot let an in-flight build land in the
        # cache AFTER the clear, surviving teardown. An in-flight build now
        # lands in the snapshot or not at all.
        with _service_lock:
            services = []
            if _service is not None:
                services.append(_service)
            services.extend(list(_services_by_principal.values()))
            _service = None
            _services_by_principal.clear()
        if not services and _backlog_exec_runner is None:
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
    # Every stage logs start/done with elapsed time (shutdown-forensics,
    # 2026-07-24): this path used to run in TOTAL SILENCE with every
    # exception swallowed — a 60s+ TERM (runner drain 30s + per-visit
    # close reflections that each run an LLM call) was indistinguishable
    # from a wedge, and the operator loop SIGKILLed three healthy bounces.
    # Failures stay non-fatal (stop must always keep going) but are now
    # WARNED, never silent.
    import sys as _sys
    import time as _time

    log = logging.getLogger("abstractgateway.service")

    def _say(line: str) -> None:
        # Plain stderr print, like the boot banner: the default console level
        # filters INFO logs, and these lines exist precisely so an operator
        # tailing the log during a bounce sees WORK, not silence.
        try:
            print(line, file=_sys.stderr, flush=True)
        except Exception:
            pass

    def _stage(name: str, fn) -> None:
        t0 = _time.monotonic()
        _say(f"shutdown: {name} ...")
        try:
            fn()
        except Exception:
            log.warning("shutdown: %s FAILED (continuing)", name, exc_info=True)
            _say(f"shutdown: {name} FAILED (continuing; see log)")
            return
        _say(f"shutdown: {name} done ({_time.monotonic() - t0:.1f}s)")

    try:
        bridge = getattr(service, "telegram_bridge", None)
        if bridge is not None:
            _stage("telegram bridge stop", bridge.stop)
        bridge2 = getattr(service, "email_bridge", None)
        if bridge2 is not None:
            _stage("email bridge stop", bridge2.stop)
        bridge3 = getattr(service, "agora_bridge", None)
        if bridge3 is not None:
            _stage("agora bridge stop", bridge3.stop)
        _stage("runner drain", service.runner.stop)
        chat_host = getattr(service, "entity_chat_host", None)
        if chat_host is not None:
            # Reflect + close live visits FIRST (they hold their own home
            # handles and may owe the own-time loop a wake). Each close may
            # run a reflection LLM call — the log line above is what tells
            # the operator this is WORK, not a hang.
            _stage("entity visits close_all (reflections may take a minute)", chat_host.close_all)
        registry = getattr(service, "entity_registry", None)
        if registry is not None:
            _stage("entity homes close_all", registry.close_all)
    except Exception:
        log.warning("shutdown: unexpected failure in stop sequence", exc_info=True)


def _summarize_run_output(value: Any, *, max_string: int = 50_000, max_items: int = 80, max_depth: int = 8) -> Any:
    """Return an HTTP-safe, bounded projection of a run's final output.

    #[WARNING:TRUNCATION] (ADR-0026 §4). Lossy, but never silent: every cut
    emits an in-band `#TRUNCATION:` marker naming what was dropped and the
    original size. This is a PROJECTION for the HTTP summary — the full
    output stays in the run store, so nothing downstream reads this copy."""

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
