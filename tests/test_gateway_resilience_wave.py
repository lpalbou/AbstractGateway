"""Gateway-internal resilience wave (operator order dm#150, 2026-07-21).

Two adversarial reviews attacked the serving process; these tests pin the
fixes:

1. RUNNER SELF-HEAL (P0-1): an unhandled exception in the worker's
   acquire/loop scaffolding must never kill the thread permanently — it
   recovers with backoff, and the recovery is visible (loop_restarts +
   last_loop_error on runner_status).
2. HEALTH HONESTY (P0-1 belt): an enabled runner whose worker thread died
   without a deliberate stop() reports status="dead_worker" and the health
   snapshot flips degraded — /api/health must never say healthy over a dead
   ticker. A deliberate stop() stays "inactive" (no false alarms).
3. TELEGRAM PER-UPDATE ISOLATION (P1-3): one raising update is dropped;
   the bridge thread survives and handles the next update.
4. BOUNDED close_all (P1-4): a wedged in-flight turn (held turn_lock) must
   not hang shutdown — the session is skipped within the budget.
5. WORKER REGISTRY (P2-5): register/snapshot/unregister semantics; dead
   workers surface on /api/health as dead_workers + degraded.
6. BOOT WARNINGS (P2-6): best-effort factory failures surface as labeled
   boot_warnings on the health snapshot (embedding_error included).
7. CORS-SAFE MIDDLEWARE FAILURE (P2-7): an exception raised INSIDE the
   security middleware returns a JSON 500 through the middleware stack
   (CORS-header-capable), never a raw re-raise past CORSMiddleware.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

fcntl = pytest.importorskip("fcntl", reason="singleton lock uses fcntl.flock (Unix only)")


def _make_runner(base_dir: Path, *, poll_s: float = 0.02):
    from abstractgateway.runner import GatewayRunner, GatewayRunnerConfig
    from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore

    class _Host:
        run_store = InMemoryRunStore()
        ledger_store = InMemoryLedgerStore()
        artifact_store = None

        def runtime_and_workflow_for_run(self, run_id: str):  # pragma: no cover
            raise KeyError(run_id)

    return GatewayRunner(base_dir=base_dir, host=_Host(), config=GatewayRunnerConfig(poll_interval_s=poll_s))


def _wait_for(predicate, *, timeout_s: float = 5.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return False


# ---------------------------------------------------------------------------
# 1. Runner self-heal
# ---------------------------------------------------------------------------


@pytest.mark.basic
def test_runner_worker_recovers_from_loop_crash(tmp_path: Path) -> None:
    """A raising _loop must not kill the worker: it releases the lock, backs
    off, and re-enters — visible as loop_restarts + last_loop_error."""
    runner = _make_runner(tmp_path)
    original_loop = runner._loop
    crashes = {"n": 0}

    def _crashing_loop() -> bool:
        if crashes["n"] < 1:
            crashes["n"] += 1
            raise RuntimeError("injected loop crash")
        return original_loop()

    runner._loop = _crashing_loop  # type: ignore[method-assign]
    runner.start()
    try:
        # The crash fires, the guard recovers (1s backoff for restart #1),
        # and the loop re-enters and becomes active again.
        assert _wait_for(lambda: runner.runner_status().get("status") == "active", timeout_s=10.0), (
            f"runner never recovered: {runner.runner_status()}"
        )
        st = runner.runner_status()
        assert st.get("loop_restarts") == 1
        assert "injected loop crash" in str(st.get("last_loop_error"))
        assert st.get("thread_alive") is True
    finally:
        runner.stop(timeout_s=2.0, drain_timeout_s=2.0)


@pytest.mark.basic
def test_runner_crash_releases_lock_for_peers(tmp_path: Path) -> None:
    """The self-heal guard releases the flock during backoff — a healthy peer
    can acquire while the crashed runner waits (P0-1b: the leaked fd must not
    block even the SAME process's retry)."""
    runner = _make_runner(tmp_path)

    def _always_crash() -> bool:
        raise RuntimeError("permanent fault")

    runner._loop = _always_crash  # type: ignore[method-assign]
    runner.start()
    try:
        assert _wait_for(lambda: (runner.runner_status().get("loop_restarts") or 0) >= 1, timeout_s=5.0)
        # During backoff the lock is released: an independent flock succeeds.
        lock_path = tmp_path / "gateway_runner.lock"

        def _lock_is_free() -> bool:
            try:
                with lock_path.open("a") as fh:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                    return True
            except OSError:
                return False

        assert _wait_for(_lock_is_free, timeout_s=5.0), "crashed runner kept holding the singleton lock"
    finally:
        runner.stop(timeout_s=2.0, drain_timeout_s=2.0)


# ---------------------------------------------------------------------------
# 2. Health honesty: dead worker vs deliberate stop
# ---------------------------------------------------------------------------


@pytest.mark.basic
def test_dead_worker_reports_degraded_status(tmp_path: Path) -> None:
    """An enabled runner whose thread is GONE without stop() is dead_worker;
    the health snapshot flips degraded. (Pre-fix: fell through to 'inactive'
    and /api/health said healthy forever — adversary-executed proof.)"""
    runner = _make_runner(tmp_path)
    # Simulate a dead worker the way the field produces one: a thread object
    # exists (start() ran) but is no longer alive, and stop() was never
    # called. BaseException/guard-bug are the only remaining paths to this.
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    runner._thread = dead
    st = runner.runner_status()
    assert st["status"] == "dead_worker", st

    # Service-side mapping: dead_worker => degraded.
    import abstractgateway.service as service_mod
    from abstractgateway.service import gateway_runner_health_snapshot

    class _Svc:
        config = None

        def __init__(self, r):
            self.runner = r

    svc = _Svc(runner)
    old = service_mod._service
    service_mod._service = svc  # type: ignore[assignment]
    try:
        snap = gateway_runner_health_snapshot()
        assert snap["degraded"] is True
        assert any(r.get("status") == "dead_worker" for r in snap["runners"])
    finally:
        service_mod._service = old


@pytest.mark.basic
def test_deliberate_stop_reports_inactive_not_dead(tmp_path: Path) -> None:
    """stop() is operator intent: status must be 'inactive', never
    'dead_worker' (no restart storms from the supervisor)."""
    runner = _make_runner(tmp_path)
    runner.start()
    assert _wait_for(lambda: runner.runner_status().get("status") == "active", timeout_s=5.0)
    runner.stop(timeout_s=2.0, drain_timeout_s=2.0)
    st = runner.runner_status()
    assert st["status"] == "inactive", st


@pytest.mark.basic
def test_never_started_runner_reports_inactive(tmp_path: Path) -> None:
    """A constructed-but-never-started enabled runner is 'inactive' (split
    mode / pre-start), not a false dead_worker."""
    runner = _make_runner(tmp_path)
    assert runner.runner_status()["status"] == "inactive"


# ---------------------------------------------------------------------------
# 3. Telegram per-update isolation
# ---------------------------------------------------------------------------


@pytest.mark.basic
def test_telegram_bot_loop_survives_raising_update(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """One poison update is dropped (offset already advanced); the loop lives
    to handle the next update. Pre-fix: the raise propagated out of _bot_loop
    and killed the bridge thread permanently and invisibly."""
    from abstractgateway.integrations.telegram_bridge import TelegramBridge, TelegramBridgeConfig

    cfg = TelegramBridgeConfig(
        enabled=True,
        transport="bot_api",
        session_prefix="tg",
        flow_id="wf",
        bundle_id=None,
        state_path=tmp_path / "state.json",
        poll_sleep_s=0.0,
    )
    bridge = TelegramBridge.__new__(TelegramBridge)  # no full init: unit-test the loop only
    bridge._cfg = cfg
    bridge._stop = threading.Event()
    bridge._bot_offset = 0

    handled: List[int] = []
    batches = [
        {"ok": True, "result": [{"update_id": 1}, {"update_id": 2}]},
    ]

    def _fake_get_json(url, params=None, timeout_s=None):
        if batches:
            return batches.pop(0)
        bridge._stop.set()
        return {"ok": True, "result": []}

    def _fake_handle(upd):
        uid = upd.get("update_id")
        handled.append(uid)
        if uid == 1:
            raise RuntimeError("poison update")

    monkeypatch.setattr(bridge, "_bot_api", lambda method: "https://example.invalid/x")
    monkeypatch.setattr(bridge, "_http_get_json", _fake_get_json)
    monkeypatch.setattr(bridge, "_handle_bot_update", _fake_handle)

    bridge._bot_loop()  # returns (stop set) instead of raising

    assert handled == [1, 2], "update 2 must be handled after update 1 raised"
    assert bridge._bot_offset == 3


# ---------------------------------------------------------------------------
# 4. Bounded close_all
# ---------------------------------------------------------------------------


@pytest.mark.basic
def test_entity_chat_close_all_skips_wedged_session() -> None:
    """A held turn_lock (wedged in-flight turn) must not hang shutdown:
    close_all skips it within the budget and returns."""
    from abstractgateway.entity_chat import EntityChatHost, _HostedChat

    host = EntityChatHost.__new__(EntityChatHost)  # unit: no registry, no reaper
    host._lock = threading.Lock()
    host._by_slug = {}

    class _Session:
        reports: list = []

    wedged = _HostedChat(
        chat_id="chat-wedged",
        entity_slug="e",
        entity_id="entity:e",
        session=_Session(),
        home=None,
        yielded_loop=False,
        opened_at="2026-07-21T00:00:00+00:00",
        model_info={},
    )
    wedged.turn_lock.acquire()  # a turn is (forever) in flight
    host._sessions = {"chat-wedged": wedged}

    t0 = time.monotonic()
    host.close_all(budget_s=2.0)
    elapsed = time.monotonic() - t0
    assert elapsed < 15.0, f"close_all blocked {elapsed:.1f}s on a wedged session"
    assert wedged.closed is False  # skipped, not torn mid-turn


# ---------------------------------------------------------------------------
# 5. Worker registry
# ---------------------------------------------------------------------------


@pytest.mark.basic
def test_worker_registry_snapshot_and_unregister() -> None:
    from abstractgateway.worker_registry import register_worker, unregister_worker, workers_snapshot

    ev = threading.Event()
    t = threading.Thread(target=ev.wait, daemon=True)
    t.start()
    register_worker("test-live-worker", t)
    try:
        snap = workers_snapshot()
        assert snap["test-live-worker"]["alive"] is True

        ev.set()
        t.join(timeout=2.0)
        snap = workers_snapshot()
        assert snap["test-live-worker"]["alive"] is False

        # Deliberate stop semantics: unregister clears the entry.
        unregister_worker("test-live-worker")
        assert "test-live-worker" not in workers_snapshot()
    finally:
        ev.set()
        unregister_worker("test-live-worker")


@pytest.mark.basic
def test_health_endpoint_reports_dead_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """/api/health carries workers + dead_workers and degrades on a death."""
    import asyncio

    from abstractgateway.app import health_check
    from abstractgateway.worker_registry import register_worker, unregister_worker

    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    register_worker("test-dead-reaper", dead)
    try:
        body = asyncio.run(health_check())
        assert body["workers"]["test-dead-reaper"]["alive"] is False
        assert "test-dead-reaper" in body["dead_workers"]
        assert body["status"] == "degraded"
    finally:
        unregister_worker("test-dead-reaper")


# ---------------------------------------------------------------------------
# 6. Boot warnings surface
# ---------------------------------------------------------------------------


@pytest.mark.basic
def test_boot_warnings_ride_health_snapshot() -> None:
    import abstractgateway.service as service_mod
    from abstractgateway.service import gateway_runner_health_snapshot

    class _Runner:
        def runner_status(self) -> Dict[str, Any]:
            return {"status": "active"}

    class _Svc:
        config = None
        runner = _Runner()
        boot_warnings = ("#FALLBACK shipped-catalog boot publish failed: X",)
        embedding_error = "no embedding deps"

    old = service_mod._service
    service_mod._service = _Svc()  # type: ignore[assignment]
    try:
        snap = gateway_runner_health_snapshot()
        warnings = snap["runners"][0]["boot_warnings"]
        assert any("shipped-catalog" in w for w in warnings)
        assert any("embeddings unavailable" in w for w in warnings)
        assert snap["degraded"] is False  # labeled degradation, not a dead ticker
    finally:
        service_mod._service = old


# ---------------------------------------------------------------------------
# 8. Adversary B: entity boot guard + bridge non-fatal boot
# ---------------------------------------------------------------------------


@pytest.mark.basic
def test_entity_boot_failure_degrades_instead_of_aborting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """B P0-1: the entity block was the ONE unguarded factory subsystem — an
    entity import/collision failure aborted lifespan startup and NOTHING
    served. Now: service boots, entity hosts None, labeled boot warning."""
    import abstractgateway.entities as entities_mod
    from abstractgateway.config import GatewayHostConfig
    from abstractgateway.service import create_default_gateway_service

    def _boom(*a, **kw):
        raise RuntimeError("injected entity registry failure")

    monkeypatch.setattr(entities_mod, "EntityRegistry", _boom)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")

    svc = create_default_gateway_service(config=GatewayHostConfig.from_env())
    assert svc.entity_registry is None
    assert svc.entity_chat_host is None
    assert svc.entity_visit_host is None
    assert any("entity subsystem not available" in w for w in svc.boot_warnings), svc.boot_warnings
    # Plain-run machinery is intact.
    assert svc.host is not None
    assert svc.runner is not None


@pytest.mark.basic
def test_broken_telegram_bridge_degrades_instead_of_aborting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B P1-2: a bridge construction failure used to raise out of the
    composition root and kill ALL serving (plain runs, entities, health).
    Now: labeled degradation, bridge disabled, everything else boots."""
    import abstractgateway.integrations.telegram_bridge as tg_mod
    from abstractgateway.config import GatewayHostConfig
    from abstractgateway.service import create_default_gateway_service

    def _boom(*a, **kw):
        raise RuntimeError("injected bridge construction failure")

    monkeypatch.setattr(tg_mod, "TelegramBridge", _boom)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACT_TELEGRAM_BRIDGE", "1")

    svc = create_default_gateway_service(config=GatewayHostConfig.from_env())
    assert svc.telegram_bridge is None
    assert any("telegram bridge disabled" in w for w in svc.boot_warnings), svc.boot_warnings
    assert svc.host is not None  # serving machinery intact


# ---------------------------------------------------------------------------
# 9. Adversary B: wedged-tick visibility
# ---------------------------------------------------------------------------


@pytest.mark.basic
def test_wedged_ticks_surface_and_degrade_when_pool_exhausted(tmp_path: Path) -> None:
    """B P1-1: N wedged ticks (no-timeout provider calls) freeze all run
    progression; runner_status now counts them and all-workers-wedged flips
    the health snapshot degraded."""
    import abstractgateway.service as service_mod
    from abstractgateway.service import gateway_runner_health_snapshot

    runner = _make_runner(tmp_path)
    # Simulate: every tick worker held for far longer than the wedge
    # threshold (tick default config here is 4 workers).
    ancient = time.time() - 100_000
    with runner._inflight_lock:
        for i in range(int(runner._cfg.tick_workers)):
            runner._inflight[f"run-wedged-{i}"] = ancient

    st = runner.runner_status()
    assert st["inflight_ticks"] == int(runner._cfg.tick_workers)
    assert len(st["wedged_ticks"]) == int(runner._cfg.tick_workers)
    assert st["all_tick_workers_wedged"] is True

    class _Svc:
        config = None

        def __init__(self, r):
            self.runner = r

    old = service_mod._service
    service_mod._service = _Svc(runner)  # type: ignore[assignment]
    try:
        snap = gateway_runner_health_snapshot()
        assert snap["degraded"] is True
    finally:
        service_mod._service = old

    # One wedged tick among free workers: visible, NOT degraded (a single
    # slow provider call is normal operation).
    with runner._inflight_lock:
        runner._inflight.clear()
        runner._inflight["run-slow"] = ancient
    st = runner.runner_status()
    assert list(st["wedged_ticks"]) == ["run-slow"]
    assert "all_tick_workers_wedged" not in st


@pytest.mark.basic
def test_voice_synth_concurrency_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """B P0-2 knob: default 4, env-tunable, floor 1."""
    from abstractgateway.routes.gateway import _voice_synth_max_concurrency

    monkeypatch.delenv("ABSTRACTGATEWAY_VOICE_MAX_CONCURRENCY", raising=False)
    assert _voice_synth_max_concurrency() == 4
    monkeypatch.setenv("ABSTRACTGATEWAY_VOICE_MAX_CONCURRENCY", "9")
    assert _voice_synth_max_concurrency() == 9
    monkeypatch.setenv("ABSTRACTGATEWAY_VOICE_MAX_CONCURRENCY", "0")
    assert _voice_synth_max_concurrency() == 1


# ---------------------------------------------------------------------------
# 10. Boot honesty (framework live kill-proof follow-up, c4063)
# ---------------------------------------------------------------------------


@pytest.mark.basic
def test_health_answers_starting_during_boot_and_settles_ready() -> None:
    """The supervisor's probe must never see connection-refused (or a lie)
    during a long boot: lifespan yields immediately, health says "starting",
    then settles. A failed boot degrades loudly with the error."""
    import asyncio

    import abstractgateway.service as service_mod
    from abstractgateway.app import health_check

    old_state, old_err = service_mod._boot_state, service_mod._boot_error
    try:
        service_mod._boot_state = "starting"
        body = asyncio.run(health_check())
        assert body["status"] == "starting"
        assert body["boot"]["state"] == "starting"

        service_mod._boot_state = "failed"
        service_mod._boot_error = "RuntimeError: injected boot failure"
        body = asyncio.run(health_check())
        assert body["status"] == "degraded"
        assert "injected boot failure" in body["boot"]["error"]

        service_mod._boot_state = "ready"
        service_mod._boot_error = None
        body = asyncio.run(health_check())
        assert "boot" not in body
    finally:
        service_mod._boot_state = old_state
        service_mod._boot_error = old_err


# ---------------------------------------------------------------------------
# 7. CORS-safe security-middleware failure
# ---------------------------------------------------------------------------


@pytest.mark.basic
def test_security_middleware_internal_error_returns_json_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exception INSIDE the middleware must surface as a sendable JSON 500
    (exits through CORSMiddleware), never re-raise past the stack (raw 500
    without CORS headers = browser 'Failed to fetch')."""
    import asyncio

    from abstractgateway.security.gateway_security import GatewayAuthPolicy, GatewaySecurityMiddleware

    async def _inner_app(scope, receive, send):  # pragma: no cover - never reached
        raise AssertionError("inner app must not run")

    policy = GatewayAuthPolicy(enabled=True, protect_write_endpoints=False, tokens=("t" * 32,))
    mw = GatewaySecurityMiddleware(_inner_app, policy=policy)

    def _boom(*a, **kw):
        raise RuntimeError("injected middleware fault")

    monkeypatch.setattr(mw, "_route_authorization_requirement", _boom)

    sent: List[dict] = []

    async def _send(message):
        sent.append(message)

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/gateway/runs",
        "headers": [(b"authorization", b"Bearer " + b"t" * 32)],
        "query_string": b"",
        "client": ("127.0.0.1", 12345),
    }
    asyncio.run(mw(scope, _receive, _send))

    starts = [m for m in sent if m.get("type") == "http.response.start"]
    assert starts, "middleware swallowed the request without responding"
    assert starts[0]["status"] == 500
    body = b"".join(m.get("body", b"") for m in sent if m.get("type") == "http.response.body")
    assert b"injected middleware fault" in body
