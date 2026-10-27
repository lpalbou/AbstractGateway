"""File-reading routes run their scans and reads off the event loop, and the polled listings
read a file again only when it changed (gateway load 2026-10-26: 14 async routes called a
scan or a file read directly on the loop, routes_scan.txt; GET /visualflows encoded 2.6 MB
on the loop per call, Flow's library polls it).

Technique (round 1, tests/test_session_artifacts_indexed_off_loop.py): a function each route
reaches in its file-touching part records whether it runs on the event loop; every record
must say "off". One timing proof on top: a 300 ms read inside GET /visualflows never stalls
a ticker on the app's loop by more than a few ms.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

H = {"Authorization": "Bearer t"}

# Wall-clock assertions are only trusted off CI: a shared runner can pause the whole process.
_UNDER_CI = bool(os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"))


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    flows = tmp_path / "bundles"
    flows.mkdir()
    data = tmp_path / "runtime"
    repo = tmp_path / "repo"
    for kind in ("planned", "completed"):
        (repo / "docs" / "backlog" / kind).mkdir(parents=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(data))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(flows))
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "t")
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    monkeypatch.setenv("ABSTRACTGATEWAY_BACKLOG_ROOTS", str(repo))
    monkeypatch.setenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT", str(repo))
    import abstractgateway.routes.gateway as gw
    from abstractgateway.app import app

    return SimpleNamespace(app=app, gw=gw, data=data, repo=repo, mp=monkeypatch)


def _record(mp: pytest.MonkeyPatch, owner: Any, name: str, seen: List[bool], *, then: Any = None) -> None:
    real = getattr(owner, name)

    def wrapper(*a: Any, **k: Any) -> Any:
        seen.append(_on_loop())
        if then is not None:
            return then(*a, **k)
        return real(*a, **k)

    mp.setattr(owner, name, wrapper)


def _flow(fid: str, name: str = "x") -> str:
    return json.dumps({"id": fid, "name": name, "nodes": [], "edges": []})


def test_visualflows_off_the_loop_and_unchanged_files_never_reread(env) -> None:
    gw = env.gw
    reads: List[bool] = []
    _record(env.mp, gw, "_read_visualflow_json", reads)
    with TestClient(env.app) as client:
        vf = env.data / "visualflows"
        vf.mkdir(parents=True, exist_ok=True)
        for i in range(20):
            (vf / f"f{i:02d}.json").write_text(_flow(f"f{i:02d}"), encoding="utf-8")
        (vf / "noid.json").write_text(json.dumps({"name": "no id"}), encoding="utf-8")
        (vf / "bad.json").write_text("{not json", encoding="utf-8")
        r = client.get("/api/gateway/visualflows", headers=H)
        assert r.status_code == 200, r.text
        body = r.json()
        assert [it["id"] for it in body] == [f"f{i:02d}" for i in range(20)] + ["noid"]  # bad.json skipped
        assert len(reads) == 22 and not any(reads)

        # Polled again with nothing changed: no file read, the same bytes.
        r2 = client.get("/api/gateway/visualflows", headers=H)
        assert r2.content == r.content and len(reads) == 22
        # One file changes: that file alone is read again.
        (vf / "f03.json").write_text(_flow("f03", name="renamed"), encoding="utf-8")
        r3 = client.get("/api/gateway/visualflows", headers=H)
        assert len(reads) == 23 and [it for it in r3.json() if it["id"] == "f03"][0]["name"] == "renamed"
        # A deleted file leaves the listing.
        os.unlink(vf / "f04.json")
        assert "f04" not in [it["id"] for it in client.get("/api/gateway/visualflows", headers=H).json()]
        assert len(reads) == 23


def test_a_slow_flow_read_never_stalls_the_loop(env) -> None:
    """The proof is structural, not a stopwatch: the slow read must run on a thread that is NOT
    the event loop's, and it must not see a running loop. The timing check (the loop keeps
    ticking while the read sleeps) is kept where the clock is trustworthy: a CI runner under
    load stalls the whole process for seconds (2026-10-27: 2.7 s on a 59-minute run), which says
    nothing about where the read ran."""
    gw = env.gw
    real = gw._read_visualflow_json
    read_on: Dict[str, Any] = {}

    def slow(p: Path) -> Dict[str, Any]:
        read_on["thread"] = threading.get_ident()
        read_on["on_loop"] = _on_loop()
        time.sleep(0.3)
        return real(p)

    env.mp.setattr(gw, "_read_visualflow_json", slow)
    gaps: List[float] = []
    stop = {"v": False}
    loop_thread: Dict[str, int] = {}

    async def ticker() -> None:
        loop_thread["id"] = threading.get_ident()
        while not stop["v"]:
            t0 = time.perf_counter()
            await asyncio.sleep(0.001)
            gaps.append(time.perf_counter() - t0 - 0.001)

    with TestClient(env.app) as client:
        vf = env.data / "visualflows"
        vf.mkdir(parents=True, exist_ok=True)
        (vf / "a.json").write_text(_flow("a"), encoding="utf-8")
        client.portal.start_task_soon(ticker)
        time.sleep(0.05)
        r = client.get("/api/gateway/visualflows", headers=H)
        stop["v"] = True
        assert r.status_code == 200
    # The read ran in a worker thread, not on the loop (the old code read on the loop thread).
    assert read_on, "the route never reached the flow read"
    assert read_on["on_loop"] is False, "the flow read ran inside the running event loop"
    assert read_on["thread"] != loop_thread["id"], "the flow read ran on the event loop's thread"
    # The 300 ms read did not stall the ticker (the old code stalled it 300 ms): a wall-clock
    # check, skipped under CI where the runner itself can pause the process for seconds.
    if not _UNDER_CI:
        assert gaps and max(gaps) < 0.05, f"loop stalled {max(gaps) * 1000:.1f} ms"


def test_backlog_listings_off_the_loop_and_parsed_once_per_version(env) -> None:
    from abstractgateway.maintenance import backlog_parser

    parses: List[bool] = []
    _record(env.mp, backlog_parser, "parse_backlog_item", parses)
    planned = env.repo / "docs" / "backlog" / "planned"
    for i in range(1, 31):
        (planned / f"{i:04d}-item-{i}.md").write_text(f"# {i:04d}-framework: Item {i}\n\n## Summary\nThing {i}.\n", encoding="utf-8")
    (planned / "notes.md").write_text("no header here\n", encoding="utf-8")
    with TestClient(env.app) as client:
        r = client.get("/api/gateway/backlog/planned", headers=H)
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        assert [i["item_id"] for i in items[:3]] == [30, 29, 28]
        assert any(i["filename"] == "notes.md" and i["parsed"] is False for i in items)
        first = len(parses)
        assert first == 31 and not any(parses)
        r2 = client.get("/api/gateway/backlog/planned", headers=H)
        assert r2.content == r.content and len(parses) == first  # nothing changed: nothing parsed
        (planned / "0007-item-7.md").write_text("# 0007-framework: Item seven\n\n## Summary\nEdited.\n", encoding="utf-8")
        r3 = client.get("/api/gateway/backlog/planned", headers=H)
        assert len(parses) == first + 1
        assert [i for i in r3.json()["items"] if i["item_id"] == 7][0]["title"] != [i for i in items if i["item_id"] == 7][0]["title"]


def test_backlog_exec_requests_off_the_loop(env) -> None:
    gw = env.gw
    seen: List[bool] = []
    _record(env.mp, gw, "_load_json_bounded", seen)
    with TestClient(env.app) as client:
        q = gw._gateway_base_dir() / "backlog_exec_queue"
        q.mkdir(parents=True, exist_ok=True)
        for i in range(3):
            (q / f"abcdef0{i}.json").write_text(json.dumps({"request_id": f"abcdef0{i}", "status": "queued", "created_at": f"2026-10-26T0{i}:00:00Z"}), encoding="utf-8")
        r = client.get("/api/gateway/backlog/exec/requests", headers=H)
        assert r.status_code == 200, r.text
        assert [x["request_id"] for x in r.json()["requests"]] == ["abcdef02", "abcdef01", "abcdef00"]
        assert len(seen) == 3 and not any(seen)
        client.get("/api/gateway/backlog/exec/requests", headers=H)
        assert len(seen) == 3  # unchanged request files are not read again


def test_file_routes_touch_files_off_the_loop(env) -> None:
    gw = env.gw
    seen: Dict[str, List[bool]] = {}

    def rec(name: str, owner: Any = None, then: Any = None) -> None:
        seen[name] = []
        _record(env.mp, owner or gw, name, seen[name], then=then)

    def refuse(*_a: Any, **_k: Any) -> Any:
        raise HTTPException(status_code=418, detail="stop here")

    rec("_load_or_create_session_memory_owner_run", then=lambda **_k: None)  # artifact export
    rec("_request_workspace_scope", then=refuse)  # import, attachments ingest
    rec("_gateway_bug_reports_dir")
    rec("_gateway_feature_requests_dir")
    from abstractruntime.workflow_bundle import WorkflowBundleRegistry, WorkflowBundleRegistryError

    def no_bundle(self: Any, ref: str) -> Any:
        raise WorkflowBundleRegistryError(f"no bundle {ref}")

    rec("resolve_bundle", owner=WorkflowBundleRegistry, then=no_bundle)  # catalog promote
    rec("bundles_by_id", owner=WorkflowBundleRegistry)  # bundle download
    flow_file = env.data / "dyn.json"
    dyn: List[bool] = []

    def dynamic_path(wid: str) -> Path:
        dyn.append(_on_loop())
        return flow_file

    with TestClient(env.app) as client:
        flow_file.parent.mkdir(parents=True, exist_ok=True)
        flow_file.write_text(_flow("dyn"), encoding="utf-8")
        assert client.post("/api/gateway/runs/no-such-run/artifacts/a1/export", headers=H, json={"path": "x.txt"}).status_code == 404
        assert client.post("/api/gateway/artifacts/import", headers=H, json={"session_id": "s1", "path": "a.txt"}).status_code == 418
        assert client.post("/api/gateway/attachments/ingest", headers=H, json={"session_id": "s1", "path": "a.txt"}).status_code == 418
        r = client.post("/api/gateway/bugs/report", headers=H, json={"session_id": "s1", "description": "It broke."})
        assert r.status_code == 200, r.text
        r = client.post("/api/gateway/features/report", headers=H, json={"session_id": "s1", "description": "Add it."})
        assert r.status_code == 200, r.text
        listed = client.get("/api/gateway/reports/bugs", headers=H)
        assert listed.status_code == 200 and listed.json()["items"]
        fname = listed.json()["items"][0]["filename"]
        assert client.get(f"/api/gateway/reports/bugs/{fname}/content", headers=H).status_code == 200
        assert client.get("/api/gateway/reports/features", headers=H).status_code == 200
        assert client.post("/api/gateway/admin/workflow-catalog/promote", headers=H, json={"bundle_id": "nope"}).status_code == 404
        assert client.get("/api/gateway/bundles/nope/download", headers=H).status_code == 404
        host = gw._require_bundle_host(gw.get_gateway_service())
        env.mp.setattr(host, "_dynamic_flow_path", dynamic_path, raising=False)
        r = client.get("/api/gateway/workflows/dyn-flow/flow", headers=H)
        assert r.status_code == 200 and r.json()["source"] == "dynamic", r.text

    for name, flags in seen.items():
        assert flags, f"{name} was never reached"
        assert not any(flags), f"{name} ran on the event loop"
    assert dyn and not any(dyn)


def test_runs_reads_archive_and_kinds_once_and_sees_its_own_writes(env) -> None:
    from abstractgateway import session_archive, session_kinds
    from abstractruntime.core.models import RunState, RunStatus
    from abstractruntime.storage.json_files import JsonFileRunStore

    loads = {"archive": 0, "kinds": 0}
    real_a, real_k = session_archive._load, session_kinds._load

    def la(d: Any) -> Any:
        loads["archive"] += 1
        return real_a(d)

    def lk(d: Any) -> Any:
        loads["kinds"] += 1
        return real_k(d)

    env.mp.setattr(session_archive, "_load", la)
    env.mp.setattr(session_kinds, "_load", lk)
    env.mp.setattr(session_archive, "REVALIDATE_S", 3600.0)
    env.mp.setattr(session_kinds, "REVALIDATE_S", 3600.0)
    with TestClient(env.app) as client:
        store = JsonFileRunStore(env.data)
        for sid in ("s1", "s2"):
            store.save(RunState(run_id=f"root-{sid}", workflow_id="chat", status=RunStatus.COMPLETED, current_node="n", vars={}, session_id=sid))
        url = "/api/gateway/runs?root_only=true&include_ledger_len=false"
        first = client.get(url, headers=H)
        assert first.status_code == 200, first.text
        before = dict(loads)
        for _ in range(5):
            assert client.get(url, headers=H).status_code == 200
        assert loads == before  # five listings: no archive or kinds file read
        r = client.post("/api/gateway/sessions/s1/archive", headers=H)
        assert r.status_code == 200, r.text
        sids = {it.get("session_id") for it in client.get(url, headers=H).json()["items"]}
        assert "s1" not in sids and "s2" in sids  # the write is seen at once, not after the TTL
