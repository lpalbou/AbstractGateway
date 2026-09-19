"""Regression pin (2026-09-17): a host rebuild must not run ON the event loop.

Operator report: the supervisor logged "gateway: UNHEALTHY for 6 probes (~60s) — alive
but not answering /api/health … likely busy (in-process model inference)". No run was
executing. The audit log lined up four for four with the probe log: each window was one
`POST /visualflows/{id}/publish` (9-17 s) followed by one
`POST /admin/workflow-catalog/promote` (27-53 s), sent by a desktop client at launch.

Both routes are `async def` and called `reload_bundles_from_disk()` inline. That rebuilds
the whole host — bundles recompiled, memory store reopened, a new runtime and LLM client
built, which for an in-process model means reloading its weights — on the asyncio event
loop. Nothing else could be served until it returned: not `/api/health`, not any client.

Off the loop, the rebuild costs the same but the gateway keeps answering.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest

from test_gateway_prompt_cache_endpoints import _make_client

REBUILD_S = 1.2
_REBUILD_STARTED_AT: list[float] = []


def _slow_rebuild(self):  # stands in for "recompile everything and reload a 15 GB model"
    _REBUILD_STARTED_AT.append(time.perf_counter())
    time.sleep(REBUILD_S)
    return {"ok": True, "bundle_ids": [], "count": 0}


def test_health_is_answered_while_a_rebuild_is_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, headers = _make_client(tmp_path=tmp_path, monkeypatch=monkeypatch)
    with client:
        from abstractgateway.app import app
        from abstractgateway.hosts.bundle_host import WorkflowBundleGatewayHost

        monkeypatch.setattr(WorkflowBundleGatewayHost, "reload_bundles_from_disk", _slow_rebuild)

        async def scenario() -> tuple[float, int, int]:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as http:
                rebuild = asyncio.create_task(http.post("/api/gateway/bundles/reload", headers=headers))
                # Probe only once the rebuild has REALLY begun (the route does ~0.3 s of
                # awaitable setup first; a probe sent on a fixed delay lands before the
                # blocking part and passes on the bug — which is how the first two
                # versions of this test were green against the unfixed route).
                while not _REBUILD_STARTED_AT:
                    await asyncio.sleep(0.01)
                health = await http.get("/api/health")
                # If the loop is blocked, the wait above cannot even wake until the
                # rebuild returns, so this reads >= REBUILD_S. Off the loop it is ~0.
                waited = time.perf_counter() - _REBUILD_STARTED_AT[0]
                response = await rebuild
                return waited, response.status_code, health.status_code

        _REBUILD_STARTED_AT.clear()
        waited, rebuild_status, health_status = asyncio.run(scenario())

    assert rebuild_status == 200 and health_status == 200
    assert waited < REBUILD_S / 2, (
        f"/api/health was answered {waited:.2f}s after a {REBUILD_S}s host rebuild BEGAN — "
        "the rebuild ran on the event loop and nothing else could be served"
    )
