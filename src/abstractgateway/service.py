from __future__ import annotations

import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Optional

from abstractruntime.core.run_lifecycle import extract_run_lifecycle

from .config import GatewayHostConfig
from .embeddings_config import build_embedding_client, resolve_embedding_config
from .runner import GatewayRunner, GatewayRunnerConfig
from .security.principal import GatewayPrincipal, current_gateway_principal, safe_principal_component
from .security import GatewayAuthPolicy, load_gateway_auth_policy_from_env
from .stores import GatewayStores, build_file_stores, build_sqlite_stores, warm_json_run_store
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
    # Per-user email (framework backlog 0992 WP3): the plane's mail watcher +
    # notification dispatcher (mail/worker.py). Replaces the retired
    # env-configured `email_bridge`.
    email_worker: Optional[Any] = None
    # Apple push (framework backlog 1049): the plane's push dispatcher (push/dispatcher.py), built
    # only when this process has `--push-relay <URL>`; started beside the email worker.
    push_dispatcher: Optional[Any] = None
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
    # The plane's memory home for /memory/* (memory_store.PlaneMemoryHome): opened once, closed at shutdown.
    memory_home: Any = field(default_factory=lambda: _plane_memory_home())


def _plane_memory_home() -> Any:
    from .memory_store import PlaneMemoryHome

    return PlaneMemoryHome()


_service: Optional[GatewayService] = None
_services_by_principal: Dict[str, GatewayService] = {}
_service_lock = threading.RLock()
# Keys whose service is in `_services_by_principal` but still starting its runner/mail
# (mutated under `_service_lock`; read lock-free by the fast paths, which treat a member
# as a miss).
_building_keys: set[str] = set()
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


def _default_runtime_principal() -> Optional[GatewayPrincipal]:
    """The admin principal that owns the gateway's DEFAULT runtime, when one does."""
    principal = GatewayPrincipal(user_id="admin", tenant_id="default", roles=("admin",), runtime_id="default", source="gateway")
    return principal if _principal_uses_default_runtime(principal) else None


# R16.5 ("admins always"): an admin acting on an entity whose home lives in ANOTHER account's
# runtime plane is served by THAT plane's service for the request (set by
# `entity_access.entity_plane_resolver` / `entity_access.entity_plane_for`); the caller stays the
# admin (audit, markers, authorization). Never set for a non-admin.
import contextvars as _contextvars
from contextlib import contextmanager as _contextmanager

_ENTITY_PLANE_OWNER: "_contextvars.ContextVar[Optional[GatewayPrincipal]]" = _contextvars.ContextVar(
    "abstractgateway_entity_plane_owner", default=None
)


@_contextmanager
def using_entity_plane(owner: Optional[GatewayPrincipal]):
    """Serve this block from `owner`'s runtime plane (an admin acting on an entity there)."""
    token = _ENTITY_PLANE_OWNER.set(owner)
    try:
        yield
    finally:
        _ENTITY_PLANE_OWNER.reset(token)


def set_entity_plane_owner(owner: Optional[GatewayPrincipal]) -> None:
    """For the rest of this request's context (a router dependency)."""
    _ENTITY_PLANE_OWNER.set(owner)


def get_gateway_service() -> GatewayService:
    global _service
    principal = current_gateway_principal()
    if principal is not None and gateway_multi_user_enabled():
        owner = _ENTITY_PLANE_OWNER.get()
        if owner is not None and principal.is_admin():
            return get_gateway_service_for_principal(owner)
        return get_gateway_service_for_principal(principal)
    if gateway_multi_user_enabled():
        # ONE SERVICE PER DATA DIRECTORY (R16.2). Under user auth the admin's
        # requests already own the default runtime's service
        # (`default:__gateway_default_runtime__`); a principal-less caller
        # (background threads, boot steps) used to build a SECOND global
        # `_service` on the same directory — a second runtime, LLM client and
        # in-process model, a second runner fighting for the lock, and twice
        # the work on every promote. It now gets that same service.
        default_principal = _default_runtime_principal()
        if default_principal is not None:
            return get_gateway_service_for_principal(default_principal)
    # Lock-free fast path (gateway load 2026-10-26): an existing service answers
    # without `_service_lock`, which a build elsewhere can hold for minutes.
    svc = _service
    if svc is not None:
        return svc
    if _on_event_loop():
        # A miss ON the loop never builds there (round 3, backlog 1054): the build starts in
        # its own thread and this request answers 503 "starting" (or the build's own error).
        _miss_on_loop(_GLOBAL_SERVICE_KEY, _build_global_service)
    return _build_global_service()


def _build_global_service() -> GatewayService:
    """The single-user service: built once, under `_service_lock` (never on the event loop)."""
    global _service
    with _service_lock:
        if _service is None:
            _service = create_default_gateway_service()
        return _service


# ---------------------------------------------------------------------------
# Service builds OFF the event loop (gateway load 2026-10-26, backlog 1054, round 3).
#
# A cache miss used to build inline: `get_gateway_service[_for_principal]` is called by routes
# ON the event loop, and a build (stores, bundle compilation, entity routing, mail) takes
# seconds to minutes under `_service_lock`. The pre-warm middleware built off the loop first,
# but only for `/api/gateway` paths and only for the caller's own principal: any other miss
# (another path, an admin served by another account's plane, a retry after a failed pre-warm,
# a service still starting its runner) parked the whole loop, `/api/health` included.
#
# Now every build runs in its own thread, single-flight per service key, behind a
# `concurrent.futures.Future`: the loop AWAITS it (`ensure_gateway_service*`, the pre-warm
# middleware, the entity-plane dependency); a worker thread blocks on the build as before; a
# miss that still reaches the loop starts (or joins) the build and answers at once with 503
# `service_starting` (Retry-After: 1), or with the error of the build that just failed.
# ---------------------------------------------------------------------------
import concurrent.futures as _cf

_GLOBAL_SERVICE_KEY = "__global_service__"
_build_futures: Dict[str, "_cf.Future[GatewayService]"] = {}
_build_errors: Dict[str, BaseException] = {}
_build_guard = threading.Lock()


def _on_event_loop() -> bool:
    """True when the calling thread is running an asyncio event loop."""
    import asyncio

    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


class ServiceStarting(Exception):
    """A route reached a service that is still being built, on the event loop. The app answers
    503 {"reason_code": "service_starting"} with Retry-After: 1 (app.py handler)."""

    def __init__(self, key: str) -> None:
        super().__init__(f"the gateway service for {key} is starting; retry in a moment")
        self.key = key


def _service_build_future(key: str, build: Any) -> "_cf.Future[GatewayService]":
    """Start `build()` in a thread named gw-service-build, or join the one in flight for `key`.
    The thread carries the caller's context variables (the request principal)."""
    import contextvars

    with _build_guard:
        fut = _build_futures.get(key)
        if fut is not None and not fut.done():
            return fut
        fut = _cf.Future()
        _build_futures[key] = fut
    ctx = contextvars.copy_context()

    def _run() -> None:
        try:
            result = ctx.run(build)
        except BaseException as exc:  # noqa: BLE001 - handed to every waiter
            with _build_guard:
                _build_errors[key] = exc
                if _build_futures.get(key) is fut:
                    _build_futures.pop(key, None)
            fut.set_exception(exc)
        else:
            with _build_guard:
                _build_errors.pop(key, None)
                if _build_futures.get(key) is fut:
                    _build_futures.pop(key, None)
            fut.set_result(result)

    threading.Thread(target=_run, name=f"gw-service-build:{key}", daemon=True).start()
    return fut


# Set by ServicePrewarmMiddleware for the request it serves: the app awaits service builds.
# An app assembled without it (a test's bare FastAPI + router, an embedder) keeps the old
# inline build on a miss, with one warning per service key.
import contextvars as _cv

_PREWARM_ACTIVE: "_cv.ContextVar[bool]" = _cv.ContextVar("abstractgateway_service_prewarm_active", default=False)
_inline_build_warned: set[str] = set()


def _miss_on_loop(key: str, build: Any) -> None:
    """A miss on the event loop: never build here. Re-raise the error of the build that just
    failed for `key` (once, so the next request retries), else start or join the build and
    raise ServiceStarting. Returns (the caller builds inline) only in an app without the
    pre-warm middleware."""
    if not _PREWARM_ACTIVE.get():
        if key not in _inline_build_warned:
            _inline_build_warned.add(key)
            logging.getLogger("abstractgateway.service").warning(
                "service %s built ON the event loop: this app has no ServicePrewarmMiddleware", key
            )
        return
    with _build_guard:
        err = _build_errors.pop(key, None)
        inflight = _build_futures.get(key)
    if err is not None and (inflight is None or inflight.done()):
        raise err
    _service_build_future(key, build)
    raise ServiceStarting(key)


def _resolved_service_target() -> tuple[str, Optional[GatewayService], Any]:
    """What `get_gateway_service()` would serve now: (key, the ready service or None, build)."""
    principal = current_gateway_principal()
    if gateway_multi_user_enabled():
        target = None
        if principal is not None:
            owner = _ENTITY_PLANE_OWNER.get()
            target = owner if (owner is not None and principal.is_admin()) else principal
        else:
            target = _default_runtime_principal()
        if target is not None:
            return _principal_service_key(target), _ready_principal_service(target), (lambda t=target: _build_principal_service(t))
    return _GLOBAL_SERVICE_KEY, _service, _build_global_service


async def ensure_gateway_service() -> GatewayService:
    """The service `get_gateway_service()` resolves to for this context, built off the loop
    (awaited, never blocking it). Returns at once when it exists."""
    import asyncio

    key, ready, build = _resolved_service_target()
    if ready is not None:
        return ready
    return await asyncio.wrap_future(_service_build_future(key, build))


async def ensure_gateway_service_for_principal(principal: GatewayPrincipal) -> GatewayService:
    """`get_gateway_service_for_principal`, awaited: the build runs in its own thread."""
    import asyncio

    if not gateway_multi_user_enabled():
        return await ensure_gateway_service()
    ready = _ready_principal_service(principal)
    if ready is not None:
        return ready
    return await asyncio.wrap_future(
        _service_build_future(_principal_service_key(principal), lambda: _build_principal_service(principal))
    )


def principal_service_cached(principal: GatewayPrincipal) -> bool:
    """True when this principal's GatewayService already exists and is fully set up
    (the pre-warm middleware's gate; never constructs anything).

    NON-blocking (gateway load 2026-10-26, two of three watchdog exits 02:05-02:07): this
    runs ON the event loop, and it used to take `_service_lock`, which the eager
    rehydration holds while it builds ANOTHER principal's service (store scans, bundle
    compilation, mail workers: tens of seconds). `GET /me` and the SSE stream parked the
    whole loop behind that build until the watchdog killed the process. A dict lookup is
    atomic under the GIL; a key still being set up (`_building_keys`) reads as a miss, so
    the caller builds (waits) off the loop."""
    key = _principal_service_key(principal)
    return key in _services_by_principal and key not in _building_keys


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

    # Paths that never touch a service (health peeks, never builds; static pages).
    _SKIP_PREFIXES = ("/api/health", "/console", "/docs", "/redoc", "/openapi.json", "/favicon")

    async def __call__(self, scope, receive, send):  # noqa: ANN001 - ASGI signature
        if scope.get("type") not in ("http", "websocket"):
            return await self._app(scope, receive, send)
        path = str(scope.get("path") or "")
        # Every path that can reach a service (round 3, backlog 1054): /api/gateway, the other
        # /api routers, /v1, /apps. The rule used to cover /api/gateway alone.
        if path in ("", "/") or path.startswith(self._SKIP_PREFIXES):
            return await self._app(scope, receive, send)
        try:
            # Boot gate (c4063 follow-up): while the background boot runs,
            # API requests wait for it OFF the event loop — their inline
            # get_gateway_service() then hits the cache instead of racing a
            # second synchronous boot on the loop. /api/health is not under
            # /api/gateway, so probes answer instantly throughout.
            # The wait holds NO worker thread (gateway load 2026-10-26): every request that
            # arrived during the boot used to park one pool thread for the whole boot.
            if _boot_state == "starting":
                import asyncio

                deadline = time.monotonic() + 300.0
                while _boot_state == "starting" and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
            # The service this request resolves to, built in its own thread and AWAITED (the
            # loop keeps serving). A cached service costs one dict lookup. From here on a miss
            # on the loop never builds inline (`_miss_on_loop`).
            _PREWARM_ACTIVE.set(True)
            await ensure_gateway_service()
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
    # NON-blocking (boot-time lane 2026-10-01): this runs ON the event loop
    # (`async def health_check`). The old `acquire(timeout=0.5)` parked the
    # whole loop for 0.5 s per probe while the eager rehydration built a
    # service (minutes on a cold checkout: the provider imports), so a
    # supervisor plus five apps polling /api/health queued far past their
    # 3 s timeouts and every app gave up ("gateway not reachable").
    if not _service_lock.acquire(blocking=False):
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


def caller_data_dir() -> Path:
    """The data folder (`config.data_dir`, = `stores.base_dir`) of the service THIS request is
    served by, computed with the same rule as `get_gateway_service()` but without building it:
    the configured gateway data folder when user accounts are off or no principal is bound,
    else the principal's runtime folder (`<data>/users/<tenant>/<runtime>/runtime`), or for an
    admin acting on an entity plane, that owner's. Cheap (no service build), so descriptors
    computed off a request can name the caller's plane instead of the root."""
    principal = current_gateway_principal()
    if principal is None or not gateway_multi_user_enabled():
        return Path(GatewayHostConfig.from_env().data_dir)
    owner = _ENTITY_PLANE_OWNER.get()
    if owner is not None and principal.is_admin():
        principal = owner
    return Path(_config_for_principal(principal).data_dir)


def plane_for_principal(principal: GatewayPrincipal, *, data_dir: Optional[Path] = None) -> Path:
    """THE folder a principal's service works in: the gateway data folder when user accounts are
    off or for the operator's default runtime (`_principal_uses_default_runtime`), else
    `<data>/users/<tenant>/<runtime_id>` — the same rule `get_gateway_service_for_principal`
    applies. `data_dir` defaults to the configured gateway data folder."""
    base = Path(data_dir) if data_dir is not None else Path(get_gateway_service().config.data_dir)
    if not gateway_multi_user_enabled() or _principal_uses_default_runtime(principal):
        return base
    tenant = safe_principal_component(principal.tenant_id, default="default")
    runtime_id = safe_principal_component(principal.runtime_id or principal.user_id, default=principal.user_id or "user")
    return base / "users" / tenant / runtime_id


def get_gateway_service_for_principal(principal: GatewayPrincipal) -> GatewayService:
    if not gateway_multi_user_enabled():
        return get_gateway_service()
    key = _principal_service_key(principal)
    # Lock-free fast path (gateway load 2026-10-26): routes call this inline ON the event
    # loop, and `_service_lock` is held across whole service builds (the eager rehydration
    # builds every account's service under it). A service that exists, is fully set up and
    # whose runner needs no restart answers without the lock; only a miss, a build in
    # progress or a self-heal takes it (and a miss reaches here off the loop: the pre-warm
    # middleware builds in a worker thread first).
    svc = _services_by_principal.get(key)
    if svc is not None and key not in _building_keys:
        if not (getattr(svc.config, "runner_enabled", False) and _runner_needs_restart(svc.runner)):
            return svc
        if _on_event_loop():
            # A working service whose runner needs a restart still serves reads: the restart
            # (under the lock) runs in the build thread, never on the loop.
            _service_build_future(key, lambda: _build_principal_service(principal))
            return svc
    if _on_event_loop():
        _miss_on_loop(key, lambda: _build_principal_service(principal))
    return _build_principal_service(principal)


def _ready_principal_service(principal: GatewayPrincipal) -> Optional[GatewayService]:
    """The principal's service when it exists and is fully set up (lock-free), else None."""
    key = _principal_service_key(principal)
    svc = _services_by_principal.get(key)
    if svc is None or key in _building_keys:
        return None
    return svc


def _build_principal_service(principal: GatewayPrincipal) -> GatewayService:
    """Look up or build (and self-heal) a principal's service under `_service_lock`. Called in
    a worker or build thread only: it can hold the lock for minutes."""
    key = _principal_service_key(principal)
    with _service_lock:
        svc = _services_by_principal.get(key)
        if svc is None:
            svc = create_default_gateway_service(config=_config_for_principal(principal))
            # Published before its runner and mail start (a re-entrant lookup during that
            # setup finds it), but marked as building: the lock-free path above must not
            # hand out a half-started service.
            _building_keys.add(key)
            try:
                _services_by_principal[key] = svc
                if getattr(svc.config, "runner_enabled", False):
                    try:
                        svc.runner.start()
                    except Exception:
                        _services_by_principal.pop(key, None)
                        raise
                    # The mail watcher + notification dispatcher of this plane start
                    # with its runner (the multi-user path used to never start mail).
                    _start_email_worker(svc)
                    _start_push_dispatcher(svc)
            finally:
                _building_keys.discard(key)
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
        warm_json_run_store(stores)
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

        # The skills shelf (skills_shelf.py): seed <data dir>/skills/registry
        # from the curated registry abstractskill ships, once per process
        # (never at import). seed_registry never overwrites an operator's
        # edit and locks against concurrent gateway processes; a failure is
        # logged and shown by /skills, never fatal to a start.
        try:
            from .skills_shelf import seed_at_start

            seeded = seed_at_start(root_data_dir)
            if not seeded.get("ok"):
                boot_warnings.append(f"skills shelf not seeded at {seeded.get('dest')}: {seeded.get('error')}")
        except Exception as e:  # noqa: BLE001
            boot_warnings.append(f"skills shelf seed failed: {type(e).__name__}: {e}")

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
    # Per-user event stream (backlog 1046): the plane owner's stream receives this runner's
    # run pointers and attention events (events_stream.py).
    from .events_stream import plane_owner, stream_for

    runner.attach_user_stream(stream_for(cfg.data_dir, *plane_owner(cfg)))

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
        # One readable sentence per process, naming the model and the download route
        # (every account's service builds this: it used to be an ERROR per account).
        from .memory_store import say_embedder_unavailable_once

        say_embedder_unavailable_once(e)

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

    # The retired email bridge (ABSTRACT_EMAIL_*) is gone: mail is configured per
    # user (mail/accounts.py). The one-time import of that configuration into the
    # admin's account runs at boot (`begin_gateway_boot`); here only its notices
    # ride the boot warnings so a still-set variable is visible, never silent.
    try:
        from .mail.accounts import boot_notices

        for note in boot_notices():
            boot_warnings.append(f"email: {note}")
    except Exception:  # noqa: BLE001 - notices are visibility, never a boot blocker
        pass

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

    service = GatewayService(
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
        agora_bridge=agora_bridge,
        entity_registry=entity_registry,
        entity_chat_host=entity_chat_host,
        entity_visit_host=entity_visit_host,
        entity_meet_host=entity_meet_host,
        boot_warnings=tuple(boot_warnings),
        env_scan=env_scan_report or None,
    )
    try:
        from .mail.worker import build_email_worker

        object.__setattr__(service, "email_worker", build_email_worker(service))
    except Exception:  # noqa: BLE001 - email is optional (D5); the service still serves
        import logging

        logging.getLogger("abstractgateway.service").exception("email worker could not be built; email is off for this plane")
    return service


def gateway_service_label(svc: Any) -> str:
    """`<tenant>:<runtime>` — how a reload names a service (never a filesystem path)."""
    cfg = getattr(svc, "config", None)
    tenant = str(getattr(cfg, "tenant_id", "") or "default")
    runtime = str(getattr(cfg, "runtime_id", "") or getattr(cfg, "user_id", "") or "default")
    return f"{tenant}:{runtime}"


def reload_host_workflows(svc: Any, *, full: bool = False) -> Dict[str, Any]:
    """One service's reload result (the host's, labelled), never raising."""
    host = getattr(svc, "host", None)
    reload_fn = getattr(host, "reload_bundles_from_disk", None)
    label = gateway_service_label(svc)
    if not callable(reload_fn):
        return {"ok": False, "service": label, "error": "bundle reload is not supported on this gateway host"}
    try:
        result = dict((reload_fn(full=True) if full else reload_fn()) or {})
    except Exception as e:
        return {"ok": False, "service": label, "error": str(e)}
    result["service"] = label
    return result


def reload_gateway_workflow_bundles(*, full: bool = False) -> Dict[str, Any]:
    """Serve the bundles on disk on every instantiated Gateway service (R16.2).

    Each service swaps a recompiled workflow registry onto its EXISTING runtime
    (`registry_swap`); a service whose workflows did not move is left alone. A
    service's runtime is rebuilt only when its workflows newly need something it
    was started without (`service_reload`), every one only on request
    (`full=True`). Returns `workflow_reload.describe_workflow_reload`'s object:
    `{ok, kind, services, unchanged_services, duration_ms, sentence}`.
    """
    import time as _time

    from .workflow_reload import describe_workflow_reload

    started = _time.perf_counter()
    with _service_lock:
        services: list[GatewayService] = []
        if _service is not None:
            services.append(_service)
        for svc in _services_by_principal.values():
            if not any(svc is s for s in services):
                services.append(svc)
    results = [reload_host_workflows(svc, full=full) for svc in services]
    return describe_workflow_reload(results, duration_ms=int(round((_time.perf_counter() - started) * 1000.0)))


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
# A stop that lands while the boot is still running (lifespan shutdown on
# SIGTERM/Ctrl-C a few seconds after start, a TestClient that leaves at once)
# used to return while the boot thread kept going: it then built the service,
# cached it AFTER the stop's clear and started the runner, the email worker
# and the bridges of a gateway that had already shut down. Each boot now has
# its own cancel event; `stop_gateway_runner` sets it and waits for the boot
# thread, which stops at its next checkpoint (`_boot_checkpoint`) and cleans
# up whatever it had built. A boot still busy after BOOT_STOP_WAIT_S (one
# long build) is left to finish: it cancels and stops itself.
_boot_thread: Optional[threading.Thread] = None
_boot_cancel: Optional[threading.Event] = None
_boot_local = threading.local()
BOOT_STOP_WAIT_S = 60.0  # the runner drain (30 s) and visit reflections follow; a process exit ends a straggler anyway
_rehydrate_thread: Optional[threading.Thread] = None
REHYDRATE_STOP_WAIT_S = 60.0


class _BootCancelled(Exception):
    """The gateway is stopping: the boot thread stops starting things."""


def _boot_checkpoint() -> None:
    """Raise _BootCancelled when THIS thread is a boot whose gateway is
    stopping (no-op anywhere else, e.g. the split `runner` process)."""
    ev = getattr(_boot_local, "cancel", None)
    if ev is not None and ev.is_set():
        raise _BootCancelled()


def _cancel_and_wait_for_boot(timeout_s: Optional[float] = None) -> None:
    """Stop side of the boot race: cancel the running boot and wait for its
    thread (never the calling thread itself)."""
    t, ev = _boot_thread, _boot_cancel
    if ev is not None:
        ev.set()
    if t is None or t is threading.current_thread() or not t.is_alive():
        return
    wait_s = BOOT_STOP_WAIT_S if timeout_s is None else float(timeout_s)
    print("shutdown: waiting for the gateway startup still in progress ...", file=sys.stderr, flush=True)
    t.join(timeout=max(0.0, wait_s))
    if t.is_alive():
        logging.getLogger("abstractgateway.service").warning(
            "shutdown: the gateway startup is still running after %.0fs; it will stop itself when its current step ends",
            wait_s,
        )


def gateway_boot_state() -> Dict[str, Any]:
    """{"state": idle|starting|ready|failed|stopped, "error": str|None} — pure read."""
    return {"state": _boot_state, "error": _boot_error}


# ---------------------------------------------------------------------------
# Garbage-collection tuning (gateway performance, backlog 1054) -- BEGIN
#
# A warm gateway holds tens of millions of Python objects (bundles, stores,
# scan memos, RunState caches). CPython's full collection walks all of them
# while holding the GIL: one gen-2 pass was measured at >=10 s, a pause of the
# whole process (event loop, runners, health). Once the boot has built the
# long-lived objects, `gc.freeze()` moves them to the permanent generation,
# which no collection visits again, and higher thresholds make the young
# collections (and the full ones they trigger) rarer. `serve --gc-tuning off`
# leaves the interpreter's defaults alone. No environment variable. Only a
# `serve` process tunes (the flag is recorded by the CLI, default on): an app
# booted any other way (tests, embedding) keeps its garbage collector as is.
# ---------------------------------------------------------------------------
GC_TUNED_THRESHOLDS = (50_000, 50, 100)
_gc_tuning_on: Optional[bool] = None  # None = not started by `serve`: never tune
_gc_tuning_report: Dict[str, Any] = {}


def set_gc_tuning(on: bool, *, source: str = "serve") -> None:
    """Record `serve --gc-tuning on|off` for this process (applied once the boot is done)."""
    global _gc_tuning_on
    _gc_tuning_on = bool(on)
    _gc_tuning_report.clear()
    _gc_tuning_report.update({"requested": "on" if _gc_tuning_on else "off", "source": str(source)})


def gc_tuning_report() -> Dict[str, Any]:
    """What `apply_gc_tuning` did in this process ({} before it ran)."""
    return dict(_gc_tuning_report)


def apply_gc_tuning(on: Optional[bool] = None) -> Dict[str, Any]:
    """Freeze the objects alive now and raise the collection thresholds, or (off) touch nothing.

    Returns {applied, thresholds_before, thresholds, frozen, collect_s, line}; `line` is the log line.
    """
    import gc
    import time as _time

    enabled = bool(_gc_tuning_on) if on is None else bool(on)
    before = tuple(gc.get_threshold())
    if not enabled:
        line = "gc: tuning off (--gc-tuning off): thresholds %s left as they are, nothing frozen" % "/".join(map(str, before))
        out: Dict[str, Any] = {"applied": False, "thresholds_before": before, "thresholds": before,
                               "frozen": gc.get_freeze_count(), "collect_s": 0.0, "line": line}
    else:
        # Collect once first so the garbage of the boot is freed, not frozen forever.
        t0 = _time.perf_counter()
        gc.collect()
        collect_s = _time.perf_counter() - t0
        gc.freeze()
        gc.set_threshold(*GC_TUNED_THRESHOLDS)
        frozen = gc.get_freeze_count()
        line = "gc: froze %d objects alive after boot (collect %.2f s); thresholds %s -> %s" % (
            frozen, collect_s, "/".join(map(str, before)), "/".join(map(str, GC_TUNED_THRESHOLDS)))
        out = {"applied": True, "thresholds_before": before, "thresholds": tuple(gc.get_threshold()),
               "frozen": frozen, "collect_s": round(collect_s, 3), "line": line}
    logging.getLogger("abstractgateway.service").info(out["line"])
    print(f"[INFO] {out['line']}", file=sys.stderr, flush=True)
    _gc_tuning_report.update({k: (list(v) if isinstance(v, tuple) else v) for k, v in out.items()})
    return out


def _apply_gc_tuning_after_boot() -> None:
    """Called once by the boot thread when serving starts. Multi-user boots warm every plane's
    service on the eager-rehydrate thread: wait for it (bounded) so those objects are frozen too.
    Runs on its own daemon thread, so the boot is never delayed by it. Nothing at all when this
    process was not started by `serve` (no --gc-tuning choice recorded)."""
    if _gc_tuning_on is None:
        return

    def _run() -> None:
        t = _rehydrate_thread
        if t is not None:
            t.join(timeout=900.0)
        try:
            apply_gc_tuning()
        except Exception:  # noqa: BLE001 - tuning is an optimisation, never a reason to fail
            logging.getLogger("abstractgateway.service").warning("gc tuning failed", exc_info=True)

    threading.Thread(target=_run, name="gateway-gc-tuning", daemon=True).start()
# Garbage-collection tuning -- END


def begin_gateway_boot() -> None:
    """Start the heavy service boot on a background thread (idempotent while
    starting; a failed/idle state boots fresh). Lifespan calls this and
    yields immediately so the listener opens and health probes answer."""
    global _boot_state, _boot_error, _boot_thread, _boot_cancel
    if _boot_state == "starting":
        return
    _boot_state = "starting"
    _boot_error = None
    _boot_done.clear()
    cancel = threading.Event()

    def _boot() -> None:
        global _boot_state, _boot_error
        _boot_local.cancel = cancel
        current = lambda: _boot_thread is threading.current_thread()  # noqa: E731
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
            # Retired ABSTRACT_EMAIL_* configuration (framework backlog 0992, D10):
            # imported ONCE into the admin's email account, then ignored; each
            # variable still set is named with the setting that replaced it.
            try:
                from .mail.accounts import import_core_account_once, import_legacy_env_once

                for note in import_legacy_env_once() + import_core_account_once():
                    print(f"[WARN] email: {note}", file=sys.stderr, flush=True)
            except Exception:  # noqa: BLE001 - never a boot blocker
                logging.getLogger("abstractgateway.service").warning("email legacy import failed", exc_info=True)
            # Round 16: sealed secrets use ONE key file per data folder (secrets/sealing.key,
            # security/sealing.py), never the OS keychain. Stores sealed by gateway <= 0.13 with
            # the key in the macOS keychain are NEVER opened (no keychain call, no prompt):
            # mailboxes are marked needs_reconnect, the other stores are moved aside; one audit
            # line `…secret_key_migrated_to_file` per store.
            try:
                from . import mcp_registry, openai_keys
                from .mail.accounts import migrate_keychain_sealed_mailboxes
                from .users import gateway_data_dir_from_env as _seal_data_dir

                for note in migrate_keychain_sealed_mailboxes():
                    print(f"[WARN] email: {note}", file=sys.stderr, flush=True)
                if mcp_registry.migrate_legacy_store(_seal_data_dir()):
                    print(f"[WARN] mcp: {mcp_registry.MCP_HEADERS_KEY_MOVED}", file=sys.stderr, flush=True)
                if openai_keys.migrate_legacy_store(_seal_data_dir()):
                    print(
                        "[WARN] API keys: keys made before this version can no longer be revealed (their sealed copy "
                        "used the old macOS keychain key); they still work, and Revoke works.",
                        file=sys.stderr,
                        flush=True,
                    )
            except Exception:  # noqa: BLE001 - never a boot blocker
                logging.getLogger("abstractgateway.service").warning("sealed-secret key migration failed", exc_info=True)
            # Round 11: older workspace models (pre-round-9 access modes; the round-9 shared
            # workspace) are migrated ONCE at serve start (it would also run at the first read);
            # `_migrated.workspace_policy_v1/_v2` keep the old blocks.
            try:
                from .users import gateway_data_dir_from_env as _ws_data_dir
                from .workspace_policy import ensure_migrated

                if ensure_migrated(_ws_data_dir()):
                    print(
                        "[INFO] workspaces: the old workspace settings were migrated (posture \"Allow everything, "
                        "refuse listed workspaces\"; the old shared workspace is now a listed workspace); see Accounts.",
                        file=sys.stderr,
                        flush=True,
                    )
            except Exception:  # noqa: BLE001 - never a boot blocker; the first read retries
                logging.getLogger("abstractgateway.service").warning("workspace policy migration failed", exc_info=True)
            # Round 11 (R11.2): every entity has a gateway account — entity homes created before
            # entity accounts existed get theirs here, once, audited.
            try:
                from .entity_accounts import ensure_entity_accounts

                for note in ensure_entity_accounts():
                    print(f"[INFO] accounts: {note}", file=sys.stderr, flush=True)
            except Exception:  # noqa: BLE001 - never a boot blocker; the next boot retries
                logging.getLogger("abstractgateway.service").warning("entity account migration failed", exc_info=True)
            _boot_checkpoint()
            start_gateway_runner()
            _boot_checkpoint()
            if current():
                _apply_gc_tuning_after_boot()  # gc tuning (backlog 1054), see the block above
                _boot_state = "ready"
        except _BootCancelled:
            # Stopped during startup: stop what this boot already built or
            # started (the stopper may have given up waiting).
            try:
                stop_gateway_runner()
            except Exception:  # noqa: BLE001
                logging.getLogger("abstractgateway.service").warning("cleanup after a cancelled boot failed", exc_info=True)
            if current():
                _boot_state = "stopped"
        except Exception as e:
            if not current():
                return
            _boot_state = "failed"
            _boot_error = f"{type(e).__name__}: {e}"
            logging.getLogger("abstractgateway.service").exception("gateway boot failed")
        finally:
            if current():
                _boot_done.set()

    t = threading.Thread(target=_boot, name="gateway-boot", daemon=True)
    _boot_thread, _boot_cancel = t, cancel
    t.start()


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
    global _rehydrate_thread
    # Round 12 (R12.1): the command sandbox's HOST policy — the scrubbed environment every command
    # starts from and `--unsandboxed-commands` — set once for this process (serve's boot thread or
    # the split runner) BEFORE any runner can execute a tool, and audited. Never skipped: without it
    # a command would start from the gateway's own environment.
    from .command_sandbox import configure_at_boot

    rec = configure_at_boot()
    if rec.get("first"):
        print(f"[INFO] commands: {rec['line']}", file=sys.stderr, flush=True)
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
    _boot_checkpoint()
    if gateway_multi_user_enabled():
        # Per-principal services are created and started lazily on first
        # request; the BACKLOG EXEC RUNNER lives at the base data dir and is
        # NOT per-principal — under user-auth the old early return silently
        # never started it (the "no execution agent" incident 2026-07-14).
        sync_backlog_exec_runner()
        _boot_checkpoint()
        # Eager re-arm (backlog 0063): warm every registered runtime's runner
        # so parked/scheduled runs resume on boot. Runs on its OWN daemon
        # thread, NOT inline (adversary P0-1: the sweep does N heavy builds
        # under _service_lock — running it inline in the boot thread held the
        # boot gate "starting" for the whole sweep, parking every
        # /api/gateway/* request behind minutes of builds and re-opening the
        # false-recycle window the background-boot fix just closed). The
        # sweep is best-effort and every service insert is serialized by
        # _service_lock, so racing live prewarms is already safe.
        with _service_lock:
            _boot_checkpoint()
            _rehydrate_shutdown.clear()
            _rehydrate_thread = threading.Thread(
                target=_eager_rehydrate_principal_runners,
                name="gateway-eager-rehydrate",
                daemon=True,
            )
            _rehydrate_thread.start()
        return
    _boot_checkpoint()
    svc = get_gateway_service()
    _boot_checkpoint()
    svc.runner.start()
    # Optional: backlog execution runner (consumes backlog_exec_queue and executes requests).
    _boot_checkpoint()
    sync_backlog_exec_runner(data_dir=Path(svc.stores.base_dir))
    bridge = getattr(svc, "telegram_bridge", None)
    _boot_checkpoint()
    if bridge is not None:
        bridge.start()
    _boot_checkpoint()
    _start_email_worker(svc)
    _start_push_dispatcher(svc)
    agora_bridge = getattr(svc, "agora_bridge", None)
    _boot_checkpoint()
    if agora_bridge is not None:
        agora_bridge.start()


def _start_email_worker(svc: GatewayService) -> None:
    worker = getattr(svc, "email_worker", None)
    if worker is None:
        return
    try:
        worker.start()
    except Exception:  # noqa: BLE001 - email is optional; never blocks a runner start
        logging.getLogger("abstractgateway.service").warning("email worker failed to start", exc_info=True)
    try:
        # Entities are AI users with their own mailbox (round 3 §3.1): their workers run beside.
        from .mail.worker import sync_entity_workers

        sync_entity_workers()
    except Exception:  # noqa: BLE001 - email is optional; never blocks a runner start
        logging.getLogger("abstractgateway.service").warning("entity email workers failed to start", exc_info=True)


def _push_root(svc: Any) -> Optional[Path]:
    """The gateway's root data dir of a service (where the saved Push forwarder setting lives)."""
    cfg = getattr(svc, "config", None)
    if cfg is None:
        return None
    return Path(getattr(cfg, "root_data_dir", None) or cfg.data_dir)


def _start_push_dispatcher(svc: GatewayService, *, turned_on: bool = False) -> None:
    """The plane's push dispatcher, in the process that runs its runner (backlog 1049). The forwarder
    address is the `--push-relay` flag > the saved Push forwarder setting > the packaged default
    (`push.settings.effective_forwarder`), re-read by the dispatcher at every pass. Built only while
    that address is set, except in a split runner without the flag (the API process cannot start it
    there, so it parks and stats the settings file); with `--push-relay off` nothing exists, no
    thread, no socket. `turned_on` (the settings PUT): what happened while it was off is never
    pushed (the cursor moves to the stream head)."""
    from .push import settings as push_settings

    if getattr(svc, "push_dispatcher", None) is not None:
        return
    root = _push_root(svc)
    relay = push_settings.push_relay_url(root)
    flag_given, _flag_url = push_settings.flag_relay()
    try:
        from .live_deltas import ROLE_RUNNER, process_role

        split = process_role() == ROLE_RUNNER
    except Exception:  # noqa: BLE001
        split = False
    if relay is None and (flag_given or not split or root is None):
        return
    try:
        from .events_stream import plane_owner, stream_for
        from .push.dispatcher import PushDispatcher

        cfg = svc.config
        dispatcher = PushDispatcher(
            plane_dir=Path(cfg.data_dir),
            root_dir=Path(getattr(cfg, "root_data_dir", None) or cfg.data_dir),
            stream=stream_for(cfg.data_dir, *plane_owner(cfg)),
            relay_url=relay,
            resolve_relay=lambda: push_settings.push_relay_url(root),
            settings_token=lambda: push_settings.settings_change_token(root),
            # A split runner cannot be woken by the API process's device route: it stats devices.json.
            poll_when_idle=split,
            name=f"push-{gateway_service_label(svc)}",
        )
        if turned_on and relay is not None:
            from .push.store import write_cursor

            write_cursor(Path(cfg.data_dir), int(dispatcher.stream.head()))
        object.__setattr__(svc, "push_dispatcher", dispatcher)
        dispatcher.start()
    except Exception:  # noqa: BLE001 - push is optional; never blocks a runner start
        logging.getLogger("abstractgateway.service").warning("push dispatcher failed to start", exc_info=True)


def push_forwarder_changed() -> int:
    """The saved Push forwarder setting changed (the settings PUT, this process): start the missing
    dispatchers of the live planes that run their runner here, wake the others. Returns how many."""
    with _service_lock:
        services = [s for s in [_service, *_services_by_principal.values()] if s is not None]
    seen: set = set()
    n = 0
    for svc in services:
        if id(svc) in seen or not getattr(getattr(svc, "config", None), "runner_enabled", False):
            continue
        seen.add(id(svc))
        dispatcher = getattr(svc, "push_dispatcher", None)
        if dispatcher is None:
            _start_push_dispatcher(svc, turned_on=True)
            dispatcher = getattr(svc, "push_dispatcher", None)
        if dispatcher is not None:
            dispatcher.kick()
            n += 1
    return n


def stop_gateway_runner() -> None:
    global _service, _backlog_exec_runner, _backlog_exec_runner_error
    # A startup still in progress is cancelled and waited for FIRST, so no
    # boot step starts a runner/worker after this stop (see _boot_cancel).
    _cancel_and_wait_for_boot()
    # Tell the eager-rehydration sweep to stop (adversary P1-1: the sweep runs
    # on its own thread and, unsignalled, kept building services AFTER
    # shutdown returned — orphaned runners holding per-principal flocks that
    # flock-refuse the next boot's own twins). Checked per sweep iteration;
    # the set happens under _service_lock, where the boot clears and starts
    # the sweep, and the sweep is then waited for (its current build ends,
    # the next is skipped), so none of its services lands after the snapshot.
    with _service_lock:
        _rehydrate_shutdown.set()
        sweep = _rehydrate_thread
    if sweep is not None and sweep is not threading.current_thread() and sweep.is_alive():
        sweep.join(timeout=REHYDRATE_STOP_WAIT_S)
    try:
        # Entity mailbox workers (round 3 §3.1) are gateway-wide, not per service.
        from .mail.worker import stop_all_entity_workers

        stop_all_entity_workers()
    except Exception:  # noqa: BLE001 - stop must always keep going
        logging.getLogger("abstractgateway.service").warning("entity email workers stop failed", exc_info=True)
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
        email_worker = getattr(service, "email_worker", None)
        if email_worker is not None:
            _stage("email worker stop", email_worker.stop)
        push_dispatcher = getattr(service, "push_dispatcher", None)
        if push_dispatcher is not None:
            _stage("push dispatcher stop", push_dispatcher.stop)
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
        _stage("memory home close", service.memory_home.close)
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
        # The session history window's receipt, when the run was seeded.
        "session_history": None,
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
    # The session history window's receipt (`vars._runtime.session_history`:
    # replayed/dropped messages and tokens, the budget) — counts only, never
    # the replayed content. Clients show "earlier messages not replayed: N".
    vars_obj = getattr(run, "vars", None)
    runtime_ns = vars_obj.get("_runtime") if isinstance(vars_obj, dict) else None
    session_history = runtime_ns.get("session_history") if isinstance(runtime_ns, dict) else None
    out["session_history"] = dict(session_history) if isinstance(session_history, dict) else None

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
    # Who or what started the run (backlog 1038): `_meta.origin` as the host
    # stamped it ({kind, actor, client, ref}); None on runs started before it.
    _vars_for_origin = getattr(run, "vars", None)
    _meta_for_origin = _vars_for_origin.get("_meta") if isinstance(_vars_for_origin, dict) else None
    _origin = _meta_for_origin.get("origin") if isinstance(_meta_for_origin, dict) else None
    out["origin"] = dict(_origin) if isinstance(_origin, dict) else None
    # Automation attribution (automations contract C11): the same four fields
    # the run index stores, derived by the runtime from the run's inline _meta.
    from abstractruntime.core.run_attribution import automation_index_fields

    out.update(automation_index_fields(getattr(run, "vars", None), run_id=getattr(run, "run_id", None)))
    out["legacy"] = out.get("role") == "legacy_schedule"
    # The folder the run executes in (its top-level vars["workspace_root"],
    # the same value the run index carries), on TURN rows only: parent-less
    # non-controller runs and occurrences. Absent otherwise.
    vars_obj = getattr(run, "vars", None)
    ws = vars_obj.get("workspace_root") if isinstance(vars_obj, dict) else None
    is_turn = out.get("role") == "occurrence" or (
        not str(out.get("parent_run_id") or "").strip() and out.get("role") != "controller"
    )
    if isinstance(ws, str) and ws.strip() and is_turn:
        out["workspace_root"] = ws.strip()
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
