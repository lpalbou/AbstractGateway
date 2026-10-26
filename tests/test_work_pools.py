"""Bounded worker pools by kind of work (gateway load 2026-10-26, work_pools.py).

One 22-thread default executor served every off-loop call, so `/me`, the live streams and
health waited behind run and artifact scans. Light, heavy and watchers work now run in
separate bounded pools, sized by `serve --pool-light/--pool-heavy/--pool-watchers`.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time

import pytest

pytestmark = pytest.mark.basic


@pytest.fixture()
def small_pools():
    from abstractgateway import work_pools as wp

    before = wp.pool_sizes()
    wp.configure_pools(light=2, heavy=2, watchers=2)
    yield wp
    with wp._guard:
        pools = list(wp._pools.values())
        wp._pools.clear()
        wp._sizes.update(before)
    for pool in pools:
        pool.shutdown(wait=False, cancel_futures=True)


def test_ten_heavy_two_second_calls_never_delay_a_light_call_beyond_50ms(small_pools) -> None:
    wp = small_pools
    release = threading.Event()

    def _scan() -> str:  # a run/artifact scan that takes 2 s
        release.wait(2.0)
        return "scanned"

    def _me() -> str:
        return "me"

    async def scenario() -> tuple[float, dict]:
        heavy = [asyncio.ensure_future(wp.off_loop("heavy", _scan)) for _ in range(10)]
        await asyncio.sleep(0.05)  # both heavy threads busy, eight scans queued
        stats = wp.pool_stats()
        t0 = time.perf_counter()
        assert await wp.off_loop("light", _me) == "me"
        light_s = time.perf_counter() - t0
        release.set()
        assert await asyncio.gather(*heavy) == ["scanned"] * 10
        return light_s, stats

    light_s, stats = asyncio.run(scenario())
    # On one shared pool of 2 the light call waited for five rounds of scans (~10 s).
    assert light_s < 0.05, f"a light call waited {light_s * 1000:.1f} ms behind 10 heavy calls"
    assert stats["heavy"]["threads"] == 2 and stats["heavy"]["queued"] == 8, stats


def test_kinds_run_in_their_own_threads_and_carry_the_request_context(small_pools) -> None:
    wp = small_pools
    var: contextvars.ContextVar[str] = contextvars.ContextVar("who", default="nobody")

    def _where() -> tuple[str, str]:
        return threading.current_thread().name, var.get()

    async def scenario():
        var.set("alice")
        return {kind: await wp.off_loop(kind, _where) for kind in wp.KINDS}

    seen = asyncio.run(scenario())
    for kind, (thread_name, who) in seen.items():
        assert thread_name.startswith(f"gw-{kind}"), seen
        assert who == "alice", seen


def test_exceptions_propagate_and_unknown_kinds_are_refused(small_pools) -> None:
    wp = small_pools

    def _boom():
        raise KeyError("x")

    with pytest.raises(KeyError):
        asyncio.run(wp.off_loop("heavy", _boom))
    with pytest.raises(ValueError, match="unknown work pool kind"):
        asyncio.run(wp.off_loop("scan", _boom))


def test_sizes_are_bounded_and_validated(small_pools) -> None:
    wp = small_pools
    assert wp.configure_pools(heavy=3)["heavy"] == 3
    for bad in (0, -1, wp.MAX_SIZE + 1, "many"):
        with pytest.raises(ValueError, match="--pool-light"):
            wp.configure_pools(light=bad)


def test_serve_flags_size_the_pools(monkeypatch) -> None:
    """`serve --pool-light N --pool-heavy N --pool-watchers N` (no environment variable)."""
    import abstractgateway.cli as cli
    from abstractgateway import work_pools as wp

    seen: dict = {}

    def _configure(**kw):
        seen.update(kw)
        raise SystemExit(0)  # stop before the server starts

    monkeypatch.setattr(wp, "configure_pools", _configure)
    with pytest.raises(SystemExit):
        cli.main(["serve", "--pool-light", "5", "--pool-heavy", "3", "--pool-watchers", "7", "--no-tray"])
    assert seen == {"light": 5, "heavy": 3, "watchers": 7}
    seen.clear()
    with pytest.raises(SystemExit):
        cli.main(["serve", "--no-tray"])
    assert seen == dict(wp.DEFAULT_SIZES)


def test_health_reports_the_pools() -> None:
    from fastapi.testclient import TestClient

    from abstractgateway.app import app

    with TestClient(app) as client:
        body = client.get("/api/health").json()
    assert set(body["pools"]) == {"light", "heavy", "watchers"}
    assert all(row["size"] >= 1 for row in body["pools"].values())
