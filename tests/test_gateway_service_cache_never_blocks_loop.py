"""A cached service answers without `_service_lock` (gateway load 2026-10-26).

Two of the night's three watchdog exits (02:05-02:07, `runtime/incidents/watchdog-*.json`):
`GET /me` and `GET /me/events/stream` blocked the event loop in `principal_service_cached`
(`service.py:170`) for 30 s, waiting on `_service_lock` while the eager rehydration built
ANOTHER account's service under it. The cached path is now a lock-free peek; only a miss
builds, and the build (with the lock) runs off the loop.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from timing import timing_budget  # noqa: E402 - tests/timing.py (wall-clock budgets)

pytestmark = pytest.mark.basic


@pytest.fixture()
def multi_user(monkeypatch):
    import abstractgateway.service as svc_mod

    monkeypatch.setattr(svc_mod, "gateway_multi_user_enabled", lambda: True)
    with svc_mod._service_lock:
        saved = dict(svc_mod._services_by_principal)
        svc_mod._services_by_principal.clear()
        svc_mod._building_keys.clear()
    yield svc_mod
    with svc_mod._service_lock:
        svc_mod._services_by_principal.clear()
        svc_mod._services_by_principal.update(saved)
        svc_mod._building_keys.clear()


def _principal(user_id: str):
    from abstractgateway.security.principal import GatewayPrincipal

    return GatewayPrincipal(user_id=user_id, tenant_id="default", roles=("user",), runtime_id=user_id, source="test")


def _fake_service():
    return SimpleNamespace(config=SimpleNamespace(runner_enabled=False), runner=None)


def test_a_slow_build_never_blocks_a_cached_lookup_beyond_1ms(multi_user, monkeypatch) -> None:
    svc_mod = multi_user
    alice, bob = _principal("alice"), _principal("bob")
    alice_svc = _fake_service()
    svc_mod._services_by_principal[svc_mod._principal_service_key(alice)] = alice_svc

    building = threading.Event()
    release = threading.Event()

    def _slow_create(config=None):  # a cold build: provider imports, store scans, a hung endpoint
        building.set()
        release.wait(10)
        return _fake_service()

    monkeypatch.setattr(svc_mod, "create_default_gateway_service", _slow_create)
    monkeypatch.setattr(svc_mod, "_config_for_principal", lambda p: None)
    t = threading.Thread(target=svc_mod.get_gateway_service_for_principal, args=(bob,), daemon=True)
    t.start()
    try:
        assert building.wait(5)
        worst = 0.0
        for _ in range(500):
            t0 = time.perf_counter()
            assert svc_mod.principal_service_cached(alice) is True
            assert svc_mod.get_gateway_service_for_principal(alice) is alice_svc
            worst = max(worst, time.perf_counter() - t0)
        # Bob's service is still building: a miss (the caller builds off the loop), never a wait.
        t0 = time.perf_counter()
        assert svc_mod.principal_service_cached(bob) is False
        worst = max(worst, time.perf_counter() - t0)
        timing_budget(worst, 0.001, "cached service lookup behind a held build lock")
    finally:
        release.set()
        t.join(5)
    assert svc_mod.principal_service_cached(bob) is True


def test_a_service_still_starting_its_runner_is_not_handed_out_lock_free(multi_user, monkeypatch) -> None:
    svc_mod = multi_user
    carol = _principal("carol")
    key = svc_mod._principal_service_key(carol)
    in_start = threading.Event()
    release = threading.Event()

    class _Runner:
        def start(self):
            in_start.set()
            release.wait(10)

        def runner_status(self):
            return {"enabled": True, "status": "active"}

    svc = SimpleNamespace(config=SimpleNamespace(runner_enabled=True), runner=_Runner())
    monkeypatch.setattr(svc_mod, "create_default_gateway_service", lambda config=None: svc)
    monkeypatch.setattr(svc_mod, "_config_for_principal", lambda p: None)
    monkeypatch.setattr(svc_mod, "_start_email_worker", lambda s: None)
    monkeypatch.setattr(svc_mod, "_start_push_dispatcher", lambda s: None)
    t = threading.Thread(target=svc_mod.get_gateway_service_for_principal, args=(carol,), daemon=True)
    t.start()
    try:
        assert in_start.wait(5)
        assert key in svc_mod._services_by_principal  # published for re-entrant setup lookups
        assert svc_mod.principal_service_cached(carol) is False  # but not ready for the loop
    finally:
        release.set()
        t.join(5)
    assert svc_mod.principal_service_cached(carol) is True
    assert svc_mod.get_gateway_service_for_principal(carol) is svc


def test_prewarm_middleware_does_not_park_the_loop_behind_a_build(multi_user, monkeypatch) -> None:
    """The middleware path of the incident: a cached principal passes straight through
    while another principal's build holds the lock."""
    svc_mod = multi_user
    from abstractgateway.security.principal import reset_current_gateway_principal, set_current_gateway_principal

    alice = _principal("alice")
    svc_mod._services_by_principal[svc_mod._principal_service_key(alice)] = _fake_service()
    monkeypatch.setattr(svc_mod, "_boot_state", "ready")

    async def _inner(scope, receive, send):
        return None

    mw = svc_mod.ServicePrewarmMiddleware(_inner)
    held = threading.Event()
    release = threading.Event()

    def _hold():
        with svc_mod._service_lock:
            held.set()
            release.wait(10)

    t = threading.Thread(target=_hold, daemon=True)
    t.start()
    assert held.wait(5)

    async def _many() -> float:
        token = set_current_gateway_principal(alice)
        try:
            worst = 0.0
            for _ in range(200):
                t0 = time.perf_counter()
                await mw({"type": "http", "path": "/api/gateway/me"}, None, None)
                worst = max(worst, time.perf_counter() - t0)
            return worst
        finally:
            reset_current_gateway_principal(token)

    try:
        worst = asyncio.run(_many())
    finally:
        release.set()
        t.join(5)
    timing_budget(worst, 0.001, "pre-warm middleware behind a held build lock")
