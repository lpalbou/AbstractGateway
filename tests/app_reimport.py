"""Drop `abstractgateway.app` between tests without keeping the dropped app alive (backlog 0945).

The test harness re-imports `abstractgateway.app` for every test (conftest pops it from
`sys.modules`): each import builds a fresh FastAPI with the whole route table (~90 routes,
their Dependants, pydantic validators and serializers: ~50,000 objects).

Popping the module is not enough to free that app. FastAPI (0.142) memoises "is this endpoint
a coroutine / generator / async generator" in three module-level `functools.lru_cache`s of
4096 entries each, keyed by the endpoint function itself (`fastapi.dependencies.models.
_is_coroutine_callable_cached` and siblings). The endpoints `app.py` defines (`/docs`,
`/redoc`, `/`, `/console`, `/api/health`) are new function objects on every import, and a
function holds its module's globals, which hold `app`: every re-imported app stayed reachable
from those caches until ~800 imports had pushed it out. Measured: ~13,500 objects retained per
test, 1.3 M per 100 tests, 29-35 M objects alive late in a full CI run.

Production imports the app once per process, so it never reaches this: the defect is the
harness's, and so is the fix. When the harness drops the module it also drops what FastAPI
memoised about it: every `lru_cache` that FastAPI's own modules define is cleared (they are
pure memo tables; FastAPI recomputes an entry on its next use). The sweep looks the caches up
by kind, not by private name, so a FastAPI release that renames or adds one is still covered;
`test_reimported_gateway_app_is_freed` goes red the day something else starts holding a
dropped app.
"""

from __future__ import annotations

import sys
from typing import List


def clear_fastapi_memo_caches() -> List[str]:
    """Clear every `functools.lru_cache` defined by a loaded `fastapi` module; return their names."""
    cleared: List[str] = []
    for mod_name, mod in list(sys.modules.items()):
        if mod is None or not (mod_name == "fastapi" or mod_name.startswith("fastapi.")):
            continue
        for attr, obj in list(vars(mod).items()):
            if not (callable(getattr(obj, "cache_clear", None)) and callable(getattr(obj, "cache_info", None))):
                continue
            # Only FastAPI's own caches (a module may re-export another library's).
            owner = str(getattr(obj, "__module__", "") or "")
            if not (owner == "fastapi" or owner.startswith("fastapi.")):
                continue
            obj.cache_clear()
            cleared.append(f"{mod_name}.{attr}")
    return cleared


def drop_gateway_app_module() -> None:
    """Forget `abstractgateway.app` so the next import builds a fresh app, and free the old one."""
    dropped = sys.modules.pop("abstractgateway.app", None)
    # The import also bound the submodule as an attribute of its package.
    package = sys.modules.get("abstractgateway")
    if dropped is not None and package is not None and getattr(package, "app", None) is dropped:
        delattr(package, "app")
    clear_fastapi_memo_caches()
