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
import threading
import time
from pathlib import Path

import httpx
import pytest

from test_gateway_prompt_cache_endpoints import _make_client
from timing import patient, timing_budget  # noqa: E402 - tests/timing.py (wall-clock budgets)

REBUILD_S = 1.2
_REBUILD_STARTED_AT: list[float] = []
# The rebuild holds until /api/health has answered (or REBUILD_S, patiently, passes). Off the
# loop, health answers while the rebuild still holds; on the loop, health cannot be served until
# the rebuild gives up, so `_REBUILD_ENDED` is already set when it answers. An ordering check,
# not a stopwatch: a slow machine cannot turn it red.
_HEALTH_ANSWERED = threading.Event()
_REBUILD_ENDED: list[bool] = []


def _hold_rebuild() -> dict:
    _REBUILD_STARTED_AT.append(time.perf_counter())
    _REBUILD_ENDED.append(_HEALTH_ANSWERED.wait(timeout=patient(REBUILD_S)))
    return {"ok": True, "bundle_ids": [], "count": 0}


def _reset() -> None:
    _REBUILD_STARTED_AT.clear()
    _REBUILD_ENDED.clear()
    _HEALTH_ANSWERED.clear()


def _slow_rebuild(self):  # stands in for "recompile everything and reload a 15 GB model"
    return _hold_rebuild()


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
                # rebuild returns. Off the loop, health answers while the rebuild holds.
                rebuild_already_ended = bool(_REBUILD_ENDED)
                _HEALTH_ANSWERED.set()
                waited = time.perf_counter() - _REBUILD_STARTED_AT[0]
                response = await rebuild
                return rebuild_already_ended, waited, response.status_code, health.status_code

        _reset()
        rebuild_already_ended, waited, rebuild_status, health_status = asyncio.run(scenario())

    assert rebuild_status == 200 and health_status == 200
    assert not rebuild_already_ended, (
        "/api/health was answered only after the host rebuild had returned — "
        "the rebuild ran on the event loop and nothing else could be served"
    )
    timing_budget(waited, REBUILD_S / 2, "/api/health behind a running host rebuild")


# R16.2: every route that writes a workflow registry reloads OFF the event loop — the
# admin catalog upload used to call it inline. A reload is a cheap registry swap now,
# so a regression would no longer show up as a slow publish; this pin catches it
# structurally: with a deliberately slow reload, /api/health must still answer.
@pytest.mark.parametrize(
    "route",
    ["/api/gateway/bundles/upload", "/api/gateway/admin/workflow-catalog/upload", "/api/gateway/admin/workflow-catalog/promote"],
)
def test_every_publish_route_reloads_off_the_event_loop(route: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_gateway_publish_without_rebuild import _bundle_bytes, _llm_flow

    client, headers = _make_client(tmp_path=tmp_path, monkeypatch=monkeypatch)
    payload = _bundle_bytes(bundle_id="offloop", version="1.0.0", flow=_llm_flow("root", prompt="hi"))
    with client:
        from abstractgateway.app import app
        from abstractgateway.hosts.bundle_host import WorkflowBundleGatewayHost

        if route.endswith("/promote"):
            # promote copies an installed bundle into the catalog: install it first.
            res = client.post(
                "/api/gateway/bundles/upload", headers=headers,
                files={"file": ("offloop@1.0.0.flow", payload, "application/octet-stream")},
                data={"overwrite": "true", "reload": "true"},
            )
            assert res.status_code == 200, res.text

        def _slow(self, *, full: bool = False):  # noqa: ARG001
            return _hold_rebuild()

        monkeypatch.setattr(WorkflowBundleGatewayHost, "reload_bundles_from_disk", _slow)

        async def scenario() -> tuple[float, int, int]:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as http:
                if route.endswith("/promote"):
                    send = http.post(route, headers=headers, json={"bundle_id": "offloop", "bundle_version": "1.0.0"})
                else:
                    send = http.post(
                        route, headers=headers,
                        files={"file": ("offloop@1.0.1.flow", payload, "application/octet-stream")},
                        data={"overwrite": "true", "reload": "true"} if route.endswith("/bundles/upload") else {},
                    )
                task = asyncio.create_task(send)
                while not _REBUILD_STARTED_AT:
                    await asyncio.sleep(0.01)
                health = await http.get("/api/health")
                rebuild_already_ended = bool(_REBUILD_ENDED)
                _HEALTH_ANSWERED.set()
                waited = time.perf_counter() - _REBUILD_STARTED_AT[0]
                response = await task
                return rebuild_already_ended, waited, response.status_code, health.status_code

        _reset()
        rebuild_already_ended, waited, status, health_status = asyncio.run(scenario())

    assert status == 200 and health_status == 200
    assert not rebuild_already_ended, f"{route}: /api/health answered only after the reload returned — it ran on the event loop"
    timing_budget(waited, REBUILD_S / 2, f"{route}: /api/health behind a running reload")
