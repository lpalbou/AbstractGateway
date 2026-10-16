"""`GET /host/runs` for the tray's Sessions menu (2026-10-15): each row
carries the SESSION's title and, when it waits, what it waits for; a new
`GET /host/runs/{run_id}/wait` serves the pending wait with the tool calls
awaiting approval in full. Pinned against real run stores (file and SQLite)
and over HTTP.

- `title` / `title_source`: an occurrence takes its automation's title
  ("automation"); a chat takes the first line of what the person typed, cut
  at 60 chars ("prompt"); anything else the workflow name ("workflow").
- `wait`: typed from the wait record's STRUCTURE: `tool_approval`
  (`details.mode == "approval_required"`), `ask_user`, `paused`, `event`,
  else `other`; a root waiting on its sub-run carries the SUB-RUN's wait
  (`wait.run_id` names the holder).
- Cost: one run-document load per surviving row (the automation title once
  per controller); measured here, with the figure in the assertion message.
- The payload names the caller's plane (`caller_plane`) and the Observer's
  Automations page (`observer_automations_path`).
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

pytestmark = pytest.mark.basic

NOW = datetime.now(timezone.utc)


def _iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def _run(run_id: str, workflow_id: str, status: str, *, created_h: float = 1.0, updated_h: float = 0.5, parent: Optional[str] = None, vars_: Optional[Dict[str, Any]] = None, wait: Optional[str] = None, wait_key: Optional[str] = None, prompt: Optional[str] = None, details: Optional[Dict[str, Any]] = None, session_id: Optional[str] = None):
    from abstractruntime.core.models import RunState, RunStatus, WaitReason, WaitState

    waiting = WaitState(reason=WaitReason(wait), wait_key=wait_key, prompt=prompt, details=details) if wait else None
    return RunState(
        run_id=run_id,
        workflow_id=workflow_id,
        status=RunStatus(status),
        current_node="n",
        vars=dict(vars_ or {}),
        waiting=waiting,
        created_at=_iso(created_h),
        updated_at=_iso(updated_h),
        parent_run_id=parent,
        session_id=session_id,
    )


APPROVAL_DETAILS = {"mode": "approval_required", "tool_calls": [{"call_id": "c1", "name": "execute_command", "arguments": {"command": "rm -rf build", "cwd": "/tmp/x"}}, {"name": "write_file", "arguments": {"path": "a.txt", "content": "hello"}}]}
LONG_PROMPT = "Compare the three vector stores we evaluated last quarter and recommend one for the pilot\nSecond line is never shown"


def _seed(store: Any, base: Optional[Path]) -> List[Any]:
    occ_meta = {"occurrence": {"automation_id": "ctl-a", "occurrence_index": 7, "role": "occurrence"}}
    runs = [
        # An automation: its controller (never a row) titles its occurrence.
        _run("ctl-a", "daily-digest@1.0.0:main", "waiting", created_h=72, updated_h=0.5, vars_={"_meta": {"automation": {"title": "Daily digest", "revision": 1}}}, wait="event", wait_key="automation:ctl-a:wake"),
        _run("occ-a", "daily-digest@1.0.0:main", "running", created_h=0.2, updated_h=0.01, parent="ctl-a", vars_={"prompt": "[Trigger schedule] never shown", "_meta": occ_meta}),
        # A chat turn: the first line of the prompt, cut at 60.
        _run("chat-1", "basic-agent@0.0.5:x", "running", created_h=0.3, updated_h=0.02, vars_={"prompt": LONG_PROMPT}),
        # A plain run with no prompt: the workflow name.
        _run("plain-1", "coding-agent@0.2.7:coder", "running", created_h=0.4, updated_h=0.03),
        # Waiting on a tool approval, directly.
        _run("ap-1", "basic-agent@0.0.5:x", "waiting", created_h=0.5, updated_h=0.04, vars_={"prompt": "Clean the build folder"}, wait="user", wait_key="tool_approval:k1", details=APPROVAL_DETAILS),
        # Waiting on a sub-run that waits on a tool approval: the ROOT row carries the sub-run's wait.
        _run("root-2", "deep-research@0.2.0:main", "waiting", created_h=0.6, updated_h=0.3, vars_={"prompt": "Market study"}, wait="subworkflow", wait_key="subrun:x"),
        _run("root-2-old", "deep-research@0.2.0:worker", "completed", created_h=0.55, updated_h=0.4, parent="root-2"),
        _run("root-2-sub", "deep-research@0.2.0:worker", "waiting", created_h=0.5, updated_h=0.05, parent="root-2", wait="user", wait_key="tool_approval:k2", details={"mode": "approval_required", "tool_calls": [{"name": "web_search", "arguments": {"query": "vector db"}}]}),
        # A question, a paused run, an event wait, a timer.
        _run("ask-1", "basic-agent@0.0.5:x", "waiting", created_h=0.7, updated_h=0.06, vars_={"prompt": "Plan the trip"}, wait="user", wait_key="ask:1", prompt="Which dates?\nAnd where?"),
        _run("paused-1", "basic-agent@0.0.5:x", "waiting", created_h=0.8, updated_h=0.07, vars_={"prompt": "Paused one", "_runtime": {"control": {"paused": True}}}, wait="user", wait_key="pause:paused-1", details={"kind": "pause"}),
        _run("evt-1", "inbox-triage@1.0.0:main", "waiting", created_h=0.9, updated_h=0.08, vars_={"prompt": "Inbox"}, wait="event", wait_key="evt:session:s1:email.received", session_id="s1"),
        _run("until-1", "basic-agent@0.0.5:x", "waiting", created_h=1.0, updated_h=0.09, vars_={"task": "Timer one"}, wait="until", wait_key="until:x"),
        # Finished, with a `message` key.
        _run("done-1", "coding-agent@0.2.7:coder", "completed", created_h=2.5, updated_h=2.0, vars_={"message": "Write the changelog"}),
    ]
    for r in runs:
        store.save(r)
    if base is not None:
        for r in runs:
            ts = datetime.fromisoformat(r.updated_at).timestamp()
            os.utime(base / f"run_{r.run_id}.json", (ts, ts))
    return runs


def _store(kind: str, tmp_path: Path):
    from abstractgateway.stores import build_file_stores, build_sqlite_stores

    if kind == "file":
        stores = build_file_stores(base_dir=tmp_path)
        return stores.run_store, tmp_path.resolve()
    stores = build_sqlite_stores(base_dir=tmp_path, db_path=tmp_path / "gateway.sqlite3")
    return stores.run_store, None


@pytest.mark.parametrize("kind", ["file", "sqlite"])
def test_rows_carry_the_sessions_title_and_what_it_waits_for(kind: str, tmp_path: Path) -> None:
    from abstractgateway.admin_runtimes import recent_runs_host_wide

    store, base = _store(kind, tmp_path)
    _seed(store, base)
    out = recent_runs_host_wide(data_dir=tmp_path, limit=25, since_epoch=NOW.timestamp() - 24 * 3600, default_run_store=store)
    rows = {r["run_id"]: r for r in out["items"]}

    assert (rows["occ-a"]["title"], rows["occ-a"]["title_source"]) == ("Daily digest", "automation")
    assert rows["chat-1"]["title_source"] == "prompt"
    assert rows["chat-1"]["title"] == "Compare the three vector stores we evaluated last quarter a…" and len(rows["chat-1"]["title"]) == 60
    assert (rows["plain-1"]["title"], rows["plain-1"]["title_source"]) == ("coding-agent:coder", "workflow")
    assert (rows["done-1"]["title"], rows["done-1"]["title_source"]) == ("Write the changelog", "prompt")
    assert (rows["until-1"]["title"], rows["until-1"]["title_source"]) == ("Timer one", "prompt")
    assert "ctl-a" not in rows and "wait" not in rows["chat-1"]

    assert rows["ap-1"]["wait"] == {"kind": "tool_approval", "summary": 'execute_command {"command": "rm -rf build", "cwd": "/tmp/x"}', "tool": "execute_command +1", "wait_key": "tool_approval:k1", "run_id": "ap-1"}
    # The sub-run's approval belongs to the parent row, and names the sub-run as the holder.
    assert rows["root-2"]["activity"] == "waiting" and "root-2-sub" not in rows
    assert rows["root-2"]["wait"] == {"kind": "tool_approval", "summary": 'web_search {"query": "vector db"}', "tool": "web_search", "wait_key": "tool_approval:k2", "run_id": "root-2-sub"}
    assert rows["ask-1"]["wait"] == {"kind": "ask_user", "summary": "Which dates?", "tool": None, "wait_key": "ask:1", "run_id": "ask-1"}
    assert rows["paused-1"]["wait"]["kind"] == "paused" and rows["paused-1"]["wait"]["summary"] == "paused"
    assert rows["evt-1"]["wait"] == {"kind": "event", "summary": "waiting for email.received", "tool": None, "wait_key": "evt:session:s1:email.received", "run_id": "evt-1"}
    assert rows["until-1"]["wait"] == {"kind": "other", "summary": "waiting for a timer", "tool": None, "wait_key": "until:x", "run_id": "until-1"}

    assert out["observer_automations_path"] == "/apps/observer/#automations"


@pytest.mark.parametrize("kind", ["file", "sqlite"])
def test_the_title_and_wait_cost_one_document_load_per_row(kind: str, tmp_path: Path) -> None:
    """`/host/runs` is cached and polled: the enrichment is bounded to one
    `load` per surviving row, plus one per automation controller, plus one
    `list_children` per level for a root waiting on its sub-run."""
    from abstractgateway.admin_runtimes import recent_runs_host_wide

    store, base = _store(kind, tmp_path)
    _seed(store, base)
    loads: List[str] = []
    children: List[str] = []
    real_load, real_children = store.load, store.list_children

    def counting_load(run_id: str):
        loads.append(str(run_id))
        return real_load(run_id)

    def counting_children(*, parent_run_id: str, status=None):
        children.append(str(parent_run_id))
        return real_children(parent_run_id=parent_run_id, status=status)

    store.load = counting_load  # type: ignore[method-assign]
    store.list_children = counting_children  # type: ignore[method-assign]
    t0 = time.perf_counter()
    out = recent_runs_host_wide(data_dir=tmp_path, limit=25, since_epoch=NOW.timestamp() - 24 * 3600, default_run_store=store)
    took_ms = (time.perf_counter() - t0) * 1000.0
    n = len(out["items"])
    assert n == 10, [r["run_id"] for r in out["items"]]
    # Exactly one document load per row plus ONE for the automation
    # controller (the seed's running roots have no parents to walk).
    assert len(loads) == n + 1, f"{len(loads)} loads for {n} rows: {loads}"
    assert loads.count("ctl-a") == 1
    assert children == ["root-2"], children
    print(f"\n{kind}: recent_runs_host_wide with titles and waits: {took_ms:.1f} ms for {n} rows ({len(loads)} loads)")
    assert took_ms < 2000, f"{kind}: {took_ms:.0f} ms for {n} rows"


@pytest.mark.parametrize("kind", ["file", "sqlite"])
def test_the_wait_of_one_run_is_served_in_full_or_refused_with_its_status(kind: str, tmp_path: Path) -> None:
    from abstractgateway.admin_runtimes import host_run_wait

    store, base = _store(kind, tmp_path)
    _seed(store, base)
    out = host_run_wait(data_dir=tmp_path, run_id="ap-1", default_run_store=store)
    assert out is not None and out["waiting"] is True and out["plane"] == "default"
    assert out["kind"] == "tool_approval" and out["wait_key"] == "tool_approval:k1" and out["reason"] == "user"
    assert out["tool_calls"] == [
        {"name": "execute_command", "arguments": {"command": "rm -rf build", "cwd": "/tmp/x"}, "call_id": "c1"},
        {"name": "write_file", "arguments": {"path": "a.txt", "content": "hello"}},
    ]
    # The holder of a parent's wait is the sub-run: asked by its own id.
    sub = host_run_wait(data_dir=tmp_path, run_id="root-2-sub", default_run_store=store)
    assert sub is not None and sub["tool_calls"] == [{"name": "web_search", "arguments": {"query": "vector db"}}]
    ask = host_run_wait(data_dir=tmp_path, run_id="ask-1", default_run_store=store)
    assert ask is not None and ask["kind"] == "ask_user" and ask["prompt"] == "Which dates?\nAnd where?" and ask["tool_calls"] == []
    assert host_run_wait(data_dir=tmp_path, run_id="done-1", default_run_store=store) == {"run_id": "done-1", "plane": "default", "waiting": False, "status": "completed"}
    assert host_run_wait(data_dir=tmp_path, run_id="nope", default_run_store=store) is None
    assert host_run_wait(data_dir=tmp_path, run_id="", default_run_store=store) is None


def test_a_user_plane_is_read_too(tmp_path: Path) -> None:
    """The wait route finds a run on a materialized user plane, and the
    listing says which plane each row lives on."""
    from abstractgateway.admin_runtimes import host_run_wait, recent_runs_host_wide
    from abstractgateway.stores import build_sqlite_stores

    default_store, _ = _store("sqlite", tmp_path)
    plane_dir = tmp_path / "users" / "default" / "bob" / "runtime"
    plane_dir.mkdir(parents=True)
    bob = build_sqlite_stores(base_dir=plane_dir, db_path=plane_dir / "gateway.sqlite3").run_store
    bob.save(_run("bob-1", "basic-agent@0.0.5:x", "waiting", vars_={"prompt": "Bob's question"}, wait="user", wait_key="ask:b", prompt="Where?"))
    out = recent_runs_host_wide(data_dir=tmp_path, limit=25, since_epoch=0.0, default_run_store=default_store)
    row = next(r for r in out["items"] if r["run_id"] == "bob-1")
    assert row["plane"] == "default/bob" and row["title"] == "Bob's question" and row["wait"]["kind"] == "ask_user"
    wait = host_run_wait(data_dir=tmp_path, run_id="bob-1", default_run_store=default_store)
    assert wait is not None and wait["plane"] == "default/bob" and wait["prompt"] == "Where?"


# ----------------------------------------------------------------- over HTTP


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    token = "operator-token-for-tests"
    (tmp_path / "flows").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", token)
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    from abstractgateway.app import app

    return TestClient(app), {"Authorization": f"Bearer {token}"}


def test_the_routes_serve_titles_waits_the_caller_plane_and_refuse_what_they_should(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.routes import gateway as gateway_routes

    c, h = _client(tmp_path, monkeypatch)
    with c:
        from abstractgateway.service import get_gateway_service

        store = get_gateway_service().host.run_store
        store.save(_run("ap-1", "basic-agent@0.0.5:x", "waiting", vars_={"prompt": "Clean the build folder"}, wait="user", wait_key="tool_approval:k1", details=APPROVAL_DETAILS))
        store.save(_run("done-1", "coding-agent@0.2.7:coder", "completed", vars_={"message": "Write the changelog"}))
        gateway_routes._HOST_RUNS_CACHE.update({"at": 0.0, "key": None, "payload": None})

        assert c.get("/api/gateway/host/runs").status_code == 401
        r = c.get("/api/gateway/host/runs", headers=h)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["caller_plane"] == "default" and body["observer_automations_path"] == "/apps/observer/#automations"
        rows = {row["run_id"]: row for row in body["items"]}
        assert rows["ap-1"]["title"] == "Clean the build folder" and rows["ap-1"]["wait"]["kind"] == "tool_approval" and rows["ap-1"]["wait"]["tool"] == "execute_command +1"
        assert rows["done-1"]["title"] == "Write the changelog" and "wait" not in rows["done-1"]
        # Cached payloads still carry the caller's plane.
        assert c.get("/api/gateway/host/runs", headers=h).json()["caller_plane"] == "default"

        assert c.get("/api/gateway/host/runs/ap-1/wait").status_code == 401
        r = c.get("/api/gateway/host/runs/ap-1/wait", headers=h)
        assert r.status_code == 200, r.text
        wait = r.json()
        assert wait["kind"] == "tool_approval" and wait["wait_key"] == "tool_approval:k1" and wait["tool_calls"][0]["arguments"] == {"command": "rm -rf build", "cwd": "/tmp/x"}
        r = c.get("/api/gateway/host/runs/done-1/wait", headers=h)
        assert r.status_code == 409 and r.json()["detail"]["reason_code"] == "not_waiting" and r.json()["detail"]["status"] == "completed"
        r = c.get("/api/gateway/host/runs/nope/wait", headers=h)
        assert r.status_code == 404 and r.json()["detail"]["reason_code"] == "run_not_found"
