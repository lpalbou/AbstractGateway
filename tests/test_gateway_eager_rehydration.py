"""Eager run rehydration + self-healing runner start (backlog 0063).

Gateway promises durable runs. In multi-user mode two paths broke that
across a restart:

1. SELF-HEALING START: a per-principal service whose runner.start() lost the
   singleton-lock race and returned dead was cached and returned as-is
   forever — a permanent per-principal stall after a lock holder dies. A
   later access must re-attempt start.
2. EAGER REHYDRATION: per-principal runners started only on that user's
   first request, so after a crash every idle tenant's parked/scheduled runs
   stayed paused until the user hit an endpoint. Boot now warms every
   registered runtime's runner (bounded, best-effort, off the listener).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

pytestmark = pytest.mark.basic


def _reset_service_globals() -> None:
    import abstractgateway.service as svc_mod

    with svc_mod._service_lock:
        svc_mod._service = None
        svc_mod._services_by_principal.clear()
    # The autouse conftest fixture calls stop_gateway_runner() before each
    # test, which SETS _rehydrate_shutdown. Production clears it in
    # start_gateway_runner before spawning the sweep; direct-call tests must
    # mirror that or the sweep stops on the first iteration.
    svc_mod._rehydrate_shutdown.clear()


# ---------------------------------------------------------------------------
# 1. _runner_needs_restart predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,enabled,expect",
    [
        ("active", True, False),
        ("standby_peer_active", True, False),
        ("starting", True, False),
        ("dead_worker", True, True),
        ("degraded_no_ticker", True, True),
        ("inactive", True, True),
        ("disabled", False, False),  # enabled flag false → never restart
    ],
)
def test_runner_needs_restart_predicate(status: str, enabled: bool, expect: bool) -> None:
    from abstractgateway.service import _runner_needs_restart

    class _Runner:
        def runner_status(self) -> Dict[str, Any]:
            return {"enabled": enabled, "status": status}

    assert _runner_needs_restart(_Runner()) is expect


def test_runner_needs_restart_never_raises() -> None:
    from abstractgateway.service import _runner_needs_restart

    class _Boom:
        def runner_status(self):
            raise RuntimeError("status probe failed")

    assert _runner_needs_restart(_Boom()) is False
    assert _runner_needs_restart(None) is False


# ---------------------------------------------------------------------------
# 2. Self-healing restart on cached-service access
# ---------------------------------------------------------------------------


def test_cached_dead_runner_is_restarted_on_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cached service whose runner is dead (lost-lock) must re-attempt
    start on the next access — not be returned stalled forever."""
    import abstractgateway.service as svc_mod

    _reset_service_globals()
    monkeypatch.setattr(svc_mod, "gateway_multi_user_enabled", lambda: True)

    class _Runner:
        def __init__(self) -> None:
            self.status = "dead_worker"
            self.start_calls = 0

        def runner_status(self) -> Dict[str, Any]:
            return {"enabled": True, "status": self.status}

        def start(self) -> None:
            self.start_calls += 1
            self.status = "active"  # start succeeds → ticking again

    class _Cfg:
        runner_enabled = True

    class _Svc:
        config = _Cfg()
        runner = _Runner()

    key = "default:alice"
    monkeypatch.setattr(svc_mod, "_principal_service_key", lambda p: key)
    with svc_mod._service_lock:
        svc_mod._services_by_principal[key] = _Svc()

    from abstractgateway.security.principal import GatewayPrincipal

    p = GatewayPrincipal(user_id="alice", tenant_id="default")
    svc = svc_mod.get_gateway_service_for_principal(p)
    assert svc.runner.start_calls == 1, "dead cached runner must be restarted on access"
    assert svc.runner.status == "active"

    # A healthy runner is NOT restarted again.
    svc2 = svc_mod.get_gateway_service_for_principal(p)
    assert svc2.runner.start_calls == 1, "healthy runner must not be needlessly restarted"
    _reset_service_globals()


def test_failed_restart_keeps_serving_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """A restart that raises must NOT evict the working cache entry — the
    service still serves reads; the next access retries."""
    import abstractgateway.service as svc_mod

    _reset_service_globals()
    monkeypatch.setattr(svc_mod, "gateway_multi_user_enabled", lambda: True)

    class _Runner:
        def __init__(self) -> None:
            self.start_calls = 0

        def runner_status(self) -> Dict[str, Any]:
            return {"enabled": True, "status": "dead_worker"}

        def start(self) -> None:
            self.start_calls += 1
            raise RuntimeError("cannot start")

    class _Cfg:
        runner_enabled = True

    class _Svc:
        config = _Cfg()
        runner = _Runner()

    key = "default:bob"
    monkeypatch.setattr(svc_mod, "_principal_service_key", lambda p: key)
    cached = _Svc()
    with svc_mod._service_lock:
        svc_mod._services_by_principal[key] = cached

    from abstractgateway.security.principal import GatewayPrincipal

    p = GatewayPrincipal(user_id="bob", tenant_id="default")
    svc = svc_mod.get_gateway_service_for_principal(p)  # must not raise
    assert svc is cached, "a failed restart must keep the working cache entry"
    assert svc.runner.start_calls == 1
    svc_mod.get_gateway_service_for_principal(p)
    assert cached.runner.start_calls == 2, "next access retries the restart"
    _reset_service_globals()


# ---------------------------------------------------------------------------
# 3. Eager rehydration warms registered runtimes
# ---------------------------------------------------------------------------


class _Rec:
    def __init__(self, uid: str, enabled: bool = True, roles=("user",)) -> None:
        self.user_id = uid
        self.tenant_id = "default"
        self.enabled = enabled
        self.roles = tuple(roles)

    def to_principal(self, *, token_fingerprint_value: str = ""):
        from abstractgateway.security.principal import GatewayPrincipal

        return GatewayPrincipal(user_id=self.user_id, tenant_id=self.tenant_id)


def _stub_sweep_env(monkeypatch, svc_mod, recs, *, runner_enabled=True):
    """Common stubs: user list, runner-enabled, runtime-dir-exists, and a
    warm-counting get_gateway_service_for_principal. Returns the warmed list."""
    import abstractgateway.users as users_mod

    class _Registry:
        def list_users(self):
            return list(recs)

    monkeypatch.setattr(users_mod, "GatewayUserRegistry", lambda *a, **k: _Registry())

    class _Cfg:
        runner_enabled = True

    _Cfg.runner_enabled = runner_enabled
    monkeypatch.setattr(svc_mod.GatewayHostConfig, "from_env", staticmethod(lambda: _Cfg()))
    # Every candidate's runtime dir "exists" unless a test overrides.
    monkeypatch.setattr(svc_mod, "_principal_runtime_dir_exists", lambda p: True)
    monkeypatch.delenv("ABSTRACTGATEWAY_EAGER_REHYDRATE_MAX", raising=False)

    warmed: List[str] = []

    class _Runner:
        def runner_status(self):
            return {"enabled": True, "status": "active"}

    class _Svc:
        runner = _Runner()

    def _fake_get(principal):
        if principal.user_id == "bad":
            raise RuntimeError("boom")
        warmed.append(principal.user_id)
        return _Svc()

    monkeypatch.setattr(svc_mod, "get_gateway_service_for_principal", _fake_get)
    return warmed


def test_eager_rehydration_warms_all_enabled_users(monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.service as svc_mod

    _reset_service_globals()
    warmed = _stub_sweep_env(
        monkeypatch, svc_mod, [_Rec("alice"), _Rec("bob"), _Rec("carol", enabled=False)]
    )
    result = svc_mod._eager_rehydrate_principal_runners()
    assert warmed == ["alice", "bob"], "disabled users are skipped"
    assert result["attempted"] == 2
    assert result["started"] == 2
    _reset_service_globals()


def test_eager_rehydration_skips_entity_principals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Entities are enabled registry users but their runtime plane is the
    per-home store, not users/<tenant>/<slug>/runtime — warming them mints
    phantom trees (adversary P1-2). Role=entity is skipped."""
    import abstractgateway.service as svc_mod

    _reset_service_globals()
    warmed = _stub_sweep_env(
        monkeypatch,
        svc_mod,
        [_Rec("alice"), _Rec("castor", roles=("entity",)), _Rec("bob")],
    )
    result = svc_mod._eager_rehydrate_principal_runners()
    assert warmed == ["alice", "bob"], "entity principals must not be warmed"
    assert result["attempted"] == 2
    _reset_service_globals()


def test_eager_rehydration_skips_never_run_principals(monkeypatch: pytest.MonkeyPatch) -> None:
    """A principal whose runtime dir does not exist has no parked work —
    never mkdir a phantom tree for it (adversary P1-2)."""
    import abstractgateway.service as svc_mod

    _reset_service_globals()
    warmed = _stub_sweep_env(monkeypatch, svc_mod, [_Rec("hasruns"), _Rec("neverran")])
    # Only "hasruns" has an existing runtime dir.
    monkeypatch.setattr(svc_mod, "_principal_runtime_dir_exists", lambda p: p.user_id == "hasruns")
    result = svc_mod._eager_rehydrate_principal_runners()
    assert warmed == ["hasruns"]
    assert result["attempted"] == 1
    _reset_service_globals()


def test_eager_rehydration_filters_before_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cap applies AFTER the enabled+non-entity filter (adversary P2-1: a
    cap-then-filter let disabled/entity records at the front starve the
    enabled ones)."""
    import abstractgateway.service as svc_mod

    _reset_service_globals()
    recs = [_Rec("z_disabled", enabled=False), _Rec("y_entity", roles=("entity",)), _Rec("alice"), _Rec("bob")]
    warmed = _stub_sweep_env(monkeypatch, svc_mod, recs)
    monkeypatch.setenv("ABSTRACTGATEWAY_EAGER_REHYDRATE_MAX", "1")
    result = svc_mod._eager_rehydrate_principal_runners()
    assert warmed == ["alice"], "the one warmed slot goes to a real candidate, not a filtered record"
    assert result["skipped_by_cap"] == 1  # bob deferred to lazy warm
    _reset_service_globals()


def test_eager_rehydration_skips_when_runner_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A no-runner (split-mode) API process must not do N heavy builds for
    zero ticking value (adversary angle C)."""
    import abstractgateway.service as svc_mod

    _reset_service_globals()
    warmed = _stub_sweep_env(monkeypatch, svc_mod, [_Rec("alice")], runner_enabled=False)
    result = svc_mod._eager_rehydrate_principal_runners()
    assert warmed == []
    assert result["attempted"] == 0
    assert "skipped_reason" in result
    _reset_service_globals()


def test_eager_rehydration_cap_zero_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.service as svc_mod

    monkeypatch.setenv("ABSTRACTGATEWAY_EAGER_REHYDRATE_MAX", "0")
    called = {"n": 0}

    import abstractgateway.users as users_mod

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("must not enumerate users when disabled")

    monkeypatch.setattr(users_mod, "GatewayUserRegistry", _boom)
    result = svc_mod._eager_rehydrate_principal_runners()
    assert result["attempted"] == 0 and result["started"] == 0
    assert called["n"] == 0


def test_eager_rehydration_stops_on_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sweep checks the shutdown event per iteration (adversary P1-1:
    an unsignalled sweep kept building after stop() returned)."""
    import abstractgateway.service as svc_mod

    _reset_service_globals()
    warmed = _stub_sweep_env(monkeypatch, svc_mod, [_Rec("a"), _Rec("b"), _Rec("c")])
    svc_mod._rehydrate_shutdown.set()
    try:
        result = svc_mod._eager_rehydrate_principal_runners()
        assert warmed == [], "shutdown before the first iteration stops the sweep"
        assert result.get("stopped") == "shutdown"
    finally:
        svc_mod._rehydrate_shutdown.clear()
        _reset_service_globals()


def test_eager_rehydration_one_failure_never_blocks_others(monkeypatch: pytest.MonkeyPatch) -> None:
    import abstractgateway.service as svc_mod

    _reset_service_globals()
    warmed = _stub_sweep_env(monkeypatch, svc_mod, [_Rec("good1"), _Rec("bad"), _Rec("good2")])
    result = svc_mod._eager_rehydrate_principal_runners()
    assert warmed == ["good1", "good2"], "one bad principal must not block the others"
    assert result["attempted"] == 3
    assert result["started"] == 2
    assert len(result["errors"]) == 1 and "bad" in result["errors"][0]
    _reset_service_globals()
