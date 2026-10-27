"""The test harness frees every app it re-imports, and no test leaves GC tuning behind (backlog 0945).

Before the fix every test's `abstractgateway.app` (~50,000 objects: the whole route table)
stayed alive through FastAPI's endpoint-classification caches, and a test that ran
`cli.main(["serve"])` left GC tuning recorded, so later boots `gc.freeze()`d everything alive.
The full suite reached 29-35 M live objects and ran 3x slower on CI. The first test goes red if
`tests/app_reimport.py` stops clearing what keeps a dropped app alive (or a new
cache starts retaining the warmed route table or the endpoints app.py defines);
the paired tests go red if `tests/conftest.py` stops freeing the app around tests (it drops it
before AND after each test, so only removing both drops is caught) or stops restoring GC state.
"""

from __future__ import annotations

import asyncio
import gc
import importlib
import weakref
from typing import Any, List

import pytest

from app_reimport import drop_gateway_app_module

pytestmark = pytest.mark.basic


def _boot_like_serve(app_module: Any) -> None:
    """What a served app builds: every route's state (the lifespan's warm-up) plus one request on
    each endpoint `app.py` defines itself (the functions FastAPI's caches are keyed by)."""
    import httpx

    app_module._warm_route_table(app_module.app)

    async def go() -> None:
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            for path in ("/docs", "/redoc", "/api/health"):
                r = await client.get(path, timeout=10)
                assert r.status_code == 200, (path, r.status_code)

    asyncio.run(go())


def _fresh_app_module() -> Any:
    return importlib.import_module("abstractgateway.app")


def test_reimported_gateway_app_is_freed() -> None:
    apps: List[weakref.ref] = []
    for _ in range(4):
        mod = _fresh_app_module()
        _boot_like_serve(mod)
        apps.append(weakref.ref(mod.app))
        del mod
        drop_gateway_app_module()
    gc.collect()
    alive = [i for i, ref in enumerate(apps) if ref() is not None]
    assert alive == [], f"re-imported apps still alive after being dropped: {alive} of {len(apps)}"


_previous_app: List[weakref.ref] = []


def test_conftest_drops_the_app_after_a_test_1_build() -> None:
    mod = _fresh_app_module()
    _boot_like_serve(mod)
    _previous_app.append(weakref.ref(mod.app))


def test_conftest_drops_the_app_after_a_test_2_check() -> None:
    if not _previous_app:
        pytest.skip("needs test_conftest_drops_the_app_after_a_test_1_build to run first")
    gc.collect()
    assert _previous_app[0]() is None, "the previous test's app is still alive: conftest no longer frees it"


_tuned: List[bool] = []


def test_conftest_restores_gc_tuning_after_a_test_1_tune() -> None:
    from abstractgateway import service

    service.set_gc_tuning(True, source="serve")  # what `cli.main(["serve"])` records
    service.apply_gc_tuning()  # what the boot after it does
    assert gc.get_freeze_count() > 0
    _tuned.append(True)


def test_conftest_restores_gc_tuning_after_a_test_2_check() -> None:
    from abstractgateway import service

    if not _tuned:
        pytest.skip("needs test_conftest_restores_gc_tuning_after_a_test_1_tune to run first")
    assert service._gc_tuning_on is None, "a test's `serve` GC-tuning choice leaked into the next test"
    assert gc.get_freeze_count() == 0, "objects frozen by a previous test are still frozen"
    assert gc.get_threshold() != service.GC_TUNED_THRESHOLDS, "a previous test's GC thresholds are still set"
