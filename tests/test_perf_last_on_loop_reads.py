"""The last blocking calls of `GET /runs`, `/host/*` and the exec promote run in the work pools
(gateway load 2026-10-26, backlog 1054, round 3).

`GET /runs` (the docs-chat migration, the children page, the shared listing computation) and the
`/host/tray/show`, `/host/update`, `/host/update/check`, `/host/update/start` routes still used
`asyncio.to_thread`, the loop's ONE default executor every other blocking call shared; and
`POST /backlog/exec/requests/{id}/promote` read the request, the manifest and every candidate file
(plus a `git` subprocess per file) ON the event loop.

Technique (tests/test_perf_routes_off_loop.py): a function each route reaches in its blocking
part sleeps 200 ms and records the thread it ran on. The loop is blocked by that work only when it
runs on the loop's own thread, so the time recorded on the loop thread must stay under 1 ms (it is
0 off the loop, 200 ms on it). The pool is checked too: scans in `gw-heavy`, status in `gw-light`,
the remote update check in `gw-watchers`. A ticker on the app's loop shows the same thing from
outside (no gap near 200 ms).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Tuple

import pytest

from timing import timing_budget  # noqa: E402 - tests/timing.py (wall-clock budgets)
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

H = {"Authorization": "Bearer t"}
WORK_S = 0.2


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data = tmp_path / "runtime"
    flows = tmp_path / "bundles"
    flows.mkdir()
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    import abstractgateway.routes.gateway as gw
    from abstractgateway.app import app

    return SimpleNamespace(app=app, gw=gw, data=data, tmp=tmp_path, mp=monkeypatch)


class _Calls:
    """Records (thread name, ran on the loop thread, seconds) per call of a wrapped function."""

    def __init__(self) -> None:
        self.rows: List[Tuple[str, bool, float]] = []
        self.loop_thread: Any = None

    def wrap(self, real: Callable[..., Any] | None = None, *, result: Any = None) -> Callable[..., Any]:
        def wrapper(*a: Any, **k: Any) -> Any:
            t0 = time.perf_counter()
            time.sleep(WORK_S)
            out = real(*a, **k) if real is not None else result
            self.rows.append((threading.current_thread().name, threading.current_thread() is self.loop_thread, time.perf_counter() - t0))
            return out

        return wrapper

    def blocked_s(self) -> float:
        return sum(dt for _name, on_loop, dt in self.rows if on_loop)


def _drive(env: Any, calls: _Calls, do: Callable[[TestClient], Any]) -> Tuple[Any, float]:
    """Run `do(client)` while a ticker runs on the app's loop; returns (result, worst gap)."""
    gaps: List[float] = []
    stop = {"v": False}

    async def ticker() -> None:
        calls.loop_thread = threading.current_thread()
        while not stop["v"]:
            t0 = time.perf_counter()
            await asyncio.sleep(0.001)
            gaps.append(time.perf_counter() - t0 - 0.001)

    with TestClient(env.app) as client:
        # Once untimed first: the route's lazy imports and first-use caches are not the site
        # under test (on a loaded machine an import alone can stall the loop for a second).
        do(client)
        calls.rows.clear()
        client.portal.start_task_soon(ticker)
        deadline = time.monotonic() + 5
        while calls.loop_thread is None and time.monotonic() < deadline:
            time.sleep(0.005)
        assert calls.loop_thread is not None
        time.sleep(0.02)
        gaps.clear()
        out = do(client)
        stop["v"] = True
    return out, (max(gaps) if gaps else 0.0)


def _assert_off_loop(calls: _Calls, worst_gap: float, pool: str, *, n: int = 1) -> None:
    assert len(calls.rows) >= n, calls.rows
    blocked = calls.blocked_s()
    timing_budget(blocked, 0.001, "blocking work run on the loop")
    names = {name for name, _l, _d in calls.rows}
    assert all(name.startswith(f"gw-{pool}") for name in names), names
    # From outside: no gap anywhere near the 200 ms of work (generous for a loaded machine).
    timing_budget(worst_gap, WORK_S / 2, "loop stall during the route's blocking work")


def _save_runs(env: Any, n_children: int = 0) -> str:
    from abstractruntime.core.models import RunState, RunStatus

    rs = env.gw.get_gateway_service().host.run_store
    parent = RunState(run_id="parent-1", workflow_id="w@1:root", status=RunStatus.COMPLETED, current_node="end", vars={},
                      created_at="2026-10-26T09:00:00+00:00", updated_at="2026-10-26T09:00:00+00:00", session_id="s1")
    rs.save(parent)
    for i in range(n_children):
        rs.save(RunState(run_id=f"child-{i}", workflow_id="w@1:root", status=RunStatus.COMPLETED, current_node="end", vars={},
                         created_at="2026-10-26T09:01:00+00:00", updated_at="2026-10-26T09:01:00+00:00", session_id="s1",
                         parent_run_id="parent-1"))
    return "parent-1"


# ----------------------------------------------------------------------------- GET /runs


def test_runs_docs_chat_migration_runs_in_the_heavy_pool(env) -> None:
    from abstractgateway import session_kinds

    calls = _Calls()
    env.mp.setattr(session_kinds, "docs_qa_migration_done", lambda _d: False)
    env.mp.setattr(session_kinds, "migrate_docs_qa_sessions", calls.wrap(result=0))

    r, gap = _drive(env, calls, lambda c: c.get("/api/gateway/runs?limit=5", headers=H))
    assert r.status_code == 200, r.text
    _assert_off_loop(calls, gap, "heavy")


def test_runs_children_page_runs_in_the_heavy_pool(env) -> None:
    calls = _Calls()

    def do(c: TestClient) -> Any:
        _save_runs(env, n_children=3)
        rs = env.gw.get_gateway_service().host.run_store
        env.mp.setattr(rs, "list_children", calls.wrap(rs.list_children), raising=False)
        return c.get("/api/gateway/runs?parent_run_id=parent-1", headers=H)

    r, gap = _drive(env, calls, do)
    assert r.status_code == 200, r.text
    assert sorted(it["run_id"] for it in r.json()["items"]) == ["child-0", "child-1", "child-2"]
    _assert_off_loop(calls, gap, "heavy")


def test_runs_shared_listing_runs_in_the_heavy_pool(env) -> None:
    calls = _Calls()

    def do(c: TestClient) -> Any:
        _save_runs(env)
        rs = env.gw.get_gateway_service().host.run_store
        lister = getattr(rs, "list_run_index", None)
        name = "list_run_index" if callable(lister) else "list_runs"
        env.mp.setattr(rs, name, calls.wrap(getattr(rs, name)), raising=False)
        return c.get("/api/gateway/runs?limit=20", headers=H)

    r, gap = _drive(env, calls, do)
    assert r.status_code == 200, r.text
    assert "parent-1" in [it["run_id"] for it in r.json()["items"]]
    _assert_off_loop(calls, gap, "heavy")


# ----------------------------------------------------------------------------- /host/*


def test_host_tray_show_starts_the_helper_in_the_light_pool(env) -> None:
    from abstractgateway import tray_supervisor

    calls = _Calls()
    sup = SimpleNamespace(start=calls.wrap(result=None))
    env.mp.setattr(tray_supervisor, "serve_context", lambda: {"base_url": "http://127.0.0.1:1", "data_dir": str(env.data), "version": "x"})
    env.mp.setattr(tray_supervisor, "tray_decision", lambda **_k: SimpleNamespace(start=True, reason="", hint=""))
    env.mp.setattr(tray_supervisor, "get_tray_supervisor", lambda: sup)
    env.mp.setattr(tray_supervisor, "tray_overview", lambda: {"tray": "ok"})

    r, gap = _drive(env, calls, lambda c: c.post("/api/gateway/host/tray/show", headers=H))
    assert r.status_code == 200, r.text
    _assert_off_loop(calls, gap, "light")


def test_host_update_state_in_the_light_pool(env) -> None:
    from abstractgateway import self_update

    calls = _Calls()
    env.mp.setattr(self_update, "update_overview", calls.wrap(result={"install": "pip"}))

    r, gap = _drive(env, calls, lambda c: c.get("/api/gateway/host/update", headers=H))
    assert r.status_code == 200 and r.json() == {"install": "pip"}, r.text
    _assert_off_loop(calls, gap, "light")


def test_host_update_check_in_the_watchers_pool(env) -> None:
    from abstractgateway import self_update

    calls = _Calls()
    env.mp.setattr(self_update, "check_for_update", calls.wrap(result={}))
    env.mp.setattr(self_update, "update_overview", lambda check=False: {"checked": True})

    r, gap = _drive(env, calls, lambda c: c.post("/api/gateway/host/update/check", headers=H))
    assert r.status_code == 200 and r.json() == {"checked": True}, r.text
    _assert_off_loop(calls, gap, "watchers")


def test_host_update_start_in_the_light_pool(env) -> None:
    from abstractgateway import self_update

    calls = _Calls()
    env.mp.setattr(self_update, "start_update", calls.wrap(result=None))
    env.mp.setattr(self_update, "update_overview", lambda check=False: {"job": "running"})

    r, gap = _drive(env, calls, lambda c: c.post("/api/gateway/host/update/start", headers=H, json={"auto_restart": False}))
    assert r.status_code == 200 and r.json() == {"job": "running"}, r.text
    _assert_off_loop(calls, gap, "light")


# ----------------------------------------------------------------------------- exec promote


def test_exec_promote_reads_and_writes_in_the_heavy_pool(env) -> None:
    gw = env.gw
    calls = _Calls()
    base = env.tmp / "base"
    qdir = base / "backlog_exec_queue"
    qdir.mkdir(parents=True)
    rid = "abcdef0123456789"
    def queue() -> None:
        (qdir / f"{rid}.json").write_text(json.dumps({"status": "awaiting_qa", "execution_mode": "inplace", "prompt": "p"}), encoding="utf-8")

    repo = env.tmp / "repo"
    repo.mkdir()
    env.mp.setattr(gw, "_backlog_repo_root", calls.wrap(result=repo))
    env.mp.setattr(gw, "_gateway_base_dir", lambda: base.resolve())
    real_load = gw._load_json_bounded
    env.mp.setattr(gw, "_load_json_bounded", calls.wrap(real_load))

    def do(c: TestClient) -> Any:
        queue()
        return c.post(f"/api/gateway/backlog/exec/requests/{rid}/promote", headers=H, json={"redeploy": False})

    r, gap = _drive(env, calls, do)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["payload"]["status"] == "promoted" and "prompt" not in body["payload"]
    assert json.loads((qdir / f"{rid}.json").read_text(encoding="utf-8"))["status"] == "promoted"
    _assert_off_loop(calls, gap, "heavy", n=2)


def test_exec_promote_refusals_still_answer_from_the_worker(env) -> None:
    """An HTTPException raised in the pool reaches the client unchanged (404, 400, 409)."""
    gw = env.gw
    base = env.tmp / "base"
    qdir = base / "backlog_exec_queue"
    qdir.mkdir(parents=True)
    (qdir / "abcdef0123456789.json").write_text(json.dumps({"status": "queued"}), encoding="utf-8")
    env.mp.setattr(gw, "_backlog_repo_root", lambda: env.tmp)
    env.mp.setattr(gw, "_gateway_base_dir", lambda: base.resolve())
    with TestClient(env.app) as client:
        assert client.post("/api/gateway/backlog/exec/requests/nope!/promote", headers=H, json={}).status_code == 400
        assert client.post("/api/gateway/backlog/exec/requests/0123456789abcdef/promote", headers=H, json={}).status_code == 404
        r = client.post("/api/gateway/backlog/exec/requests/abcdef0123456789/promote", headers=H, json={})
        assert r.status_code == 409 and "queued" in r.text, r.text
