"""The pre-warm middleware's peek never blocks the event loop.

`principal_service_cached` runs on the ASGI event loop for every /api/gateway
request. A cold first start builds a principal's service under `_service_lock`
for as long as its engines take to import (well over the 30 s watchdog limit
on a fresh install); the peek must answer at once while that build holds the
lock, so /api/health keeps answering and the watchdog never exits the gateway.
"""
from __future__ import annotations

from timing import timing_budget  # noqa: E402 - tests/timing.py (wall-clock budgets)

import threading
import time

from abstractgateway import service as svc
from abstractgateway.security.principal import GatewayPrincipal


def _hold_lock(held: threading.Event, release: threading.Event) -> None:
    with svc._service_lock:
        held.set()
        release.wait(timeout=10)


def test_the_peek_answers_at_once_while_a_build_holds_the_lock() -> None:
    held, release = threading.Event(), threading.Event()
    t = threading.Thread(target=_hold_lock, args=(held, release), daemon=True)
    t.start()
    assert held.wait(timeout=5)
    principal = GatewayPrincipal(user_id="someone", roles=("admin",))
    try:
        t0 = time.monotonic()
        assert svc.principal_service_cached(principal) is False
        timing_budget(time.monotonic() - t0, 0.5, "service peek behind the service lock")
    finally:
        release.set()
        t.join(timeout=5)


def test_the_peek_sees_a_cached_service_when_the_lock_is_free() -> None:
    principal = GatewayPrincipal(user_id="cached-one", roles=("admin",))
    key = svc._principal_service_key(principal)
    svc._services_by_principal[key] = object()  # type: ignore[assignment]
    try:
        assert svc.principal_service_cached(principal) is True
    finally:
        svc._services_by_principal.pop(key, None)
