"""The replay view routes through the real app (framework backlog 0993, AbstractRuntime ADR-0009).

GET /api/gateway/runs/{id}/view (ETag = cursor token, 304) and /view/stream (SSE `view`, then
`delta`, then `done`): a delegated child approval is THE interaction wherever it waits; two
clients following the same run while records are appended converge on the same view as a fresh
GET; a reconnect with Last-Event-ID gets one delta from its cursor; every fold is shared.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from abstractruntime.core.models import RunState, RunStatus, StepRecord, StepStatus, WaitReason, WaitState
from abstractruntime.replay import apply_delta

TOKEN = "v" * 32
H = {"Authorization": f"Bearer {TOKEN}"}
ROOT = "11111111-root-0000-0000-000000000000"
CHILD = "22222222-chld-0000-0000-000000000000"
KEY = f"tool_approval:{CHILD}:act:idem-1"


def _sse(body: str) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    cur: Dict[str, Any] = {}
    for line in body.split("\n"):
        if not line.strip():
            if cur:
                events.append(cur)
                cur = {}
            continue
        if line.startswith(":"):
            continue
        if line.startswith("id: "):
            cur["id"] = line[4:]
        elif line.startswith("event: "):
            cur["event"] = line[7:]
        elif line.startswith("data: "):
            cur["data"] = json.loads(line[6:])
    if cur:
        events.append(cur)
    return events


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    backend = getattr(request, "param", "file")
    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("ABSTRACTGATEWAY_FLOWS_DIR", str(tmp_path / "flows"))
    (tmp_path / "flows").mkdir()
    monkeypatch.setenv("ABSTRACTGATEWAY_WORKFLOW_SOURCE", "bundle")
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_ALLOWED_ORIGINS", "*")
    monkeypatch.setenv("ABSTRACTGATEWAY_STORE_BACKEND", backend)
    monkeypatch.setenv("ABSTRACTGATEWAY_RUNNER", "0")
    from abstractgateway.app import app
    from abstractgateway.replay_view import get_view_hub

    get_view_hub().clear()
    with TestClient(app) as c:
        yield c
    get_view_hub().clear()


def _stores():
    from abstractgateway.service import get_gateway_service

    svc = get_gateway_service()
    return svc.host.run_store, svc.host.ledger_store


def _run(run_id: str, *, parent: str | None = None, status: RunStatus = RunStatus.RUNNING,
         waiting: WaitState | None = None, vars: Dict[str, Any] | None = None) -> RunState:
    return RunState(run_id=run_id, workflow_id="test:view" if parent is None else "test:view-child", status=status,
                    current_node="n", vars=dict(vars or {}), waiting=waiting, parent_run_id=parent,
                    created_at="2026-10-27T10:00:00+00:00")


def _rec(run_id: str, step: str, node: str, status: StepStatus, *, effect: Dict[str, Any] | None = None,
         result: Dict[str, Any] | None = None, idem: str | None = None, ts: str = "2026-10-27T10:00:01+00:00") -> StepRecord:
    r = StepRecord(run_id=run_id, step_id=step, node_id=node, status=status, effect=effect, result=result,
                   idempotency_key=idem)
    r.started_at = ts
    r.ended_at = ts if status != StepStatus.STARTED else None
    return r


CALL = {"name": "write_file", "arguments": {"file_path": "a.txt", "content": "x"}, "call_id": "c1", "runtime_call_id": "rt1"}


def _seed_delegated_approval() -> None:
    """Root waits on its child (delegation); the child waits for a tool approval."""
    rs, ls = _stores()
    sub_wait = WaitState(reason=WaitReason.SUBWORKFLOW, wait_key=f"subworkflow:{CHILD}", details={"sub_run_id": CHILD})
    rs.save(_run(ROOT, status=RunStatus.WAITING, waiting=sub_wait, vars={"prompt": "write a.txt"}))
    ap_wait = WaitState(reason=WaitReason.USER, wait_key=KEY, details={"mode": "approval_required", "tool_calls": [CALL]})
    rs.save(_run(CHILD, parent=ROOT, status=RunStatus.WAITING, waiting=ap_wait))
    eff = {"type": "start_subworkflow", "payload": {"workflow_id": "test:view-child"}, "result_key": "r"}
    ls.append(_rec(ROOT, "s1", "agent", StepStatus.STARTED, effect=eff, idem="i-root"))
    ls.append(_rec(ROOT, "s1", "agent", StepStatus.WAITING, effect=eff, idem="i-root",
                   result={"wait": {"reason": "subworkflow", "wait_key": f"subworkflow:{CHILD}", "details": {"sub_run_id": CHILD}}}))
    tc = {"type": "tool_calls", "payload": {"tool_calls": [CALL]}, "result_key": "_temp.tool_results"}
    ls.append(_rec(CHILD, "t1", "act", StepStatus.STARTED, effect=tc, idem="idem-1", ts="2026-10-27T10:00:02+00:00"))
    ls.append(_rec(CHILD, "t1", "act", StepStatus.WAITING, effect=tc, idem="idem-1", ts="2026-10-27T10:00:02+00:00",
                   result={"wait": {"reason": "user", "wait_key": KEY,
                                    "details": {"mode": "approval_required", "tool_calls": [CALL]}}}))


def _finish_turn(pause: float = 0.0) -> None:
    """The approval is granted: the tool runs, the child and the root complete."""
    rs, ls = _stores()
    tc = {"type": "tool_calls", "payload": {"tool_calls": [CALL]}, "result_key": "_temp.tool_results"}
    steps = [
        (CHILD, _rec(CHILD, "t2", "act", StepStatus.COMPLETED, effect=tc, idem="idem-1", ts="2026-10-27T10:00:03+00:00",
                     result={"mode": "executed", "results": [{"runtime_call_id": "rt1", "call_id": "c1", "success": True, "output": "ok"}]})),
        (CHILD, _rec(CHILD, "r1", "act", StepStatus.COMPLETED, ts="2026-10-27T10:00:03+00:00",
                     effect={"type": "resume", "payload": {"wait_key": KEY, "payload": {"approved": True}}}, result={"resumed": True})),
        (CHILD, _rec(CHILD, "e1", "done", StepStatus.COMPLETED, idem="system:completion:done", ts="2026-10-27T10:00:04+00:00",
                     result={"completed": True, "output": {"answer": "wrote a.txt"}})),
        (ROOT, _rec(ROOT, "r2", "agent", StepStatus.COMPLETED, ts="2026-10-27T10:00:05+00:00",
                    effect={"type": "resume", "payload": {"wait_key": f"subworkflow:{CHILD}", "payload": {"sub_run_id": CHILD}}},
                    result={"resumed": True})),
        (ROOT, _rec(ROOT, "e2", "end", StepStatus.COMPLETED, idem="system:completion:end", ts="2026-10-27T10:00:06+00:00",
                    result={"completed": True, "output": {"response": "Done: a.txt written."}})),
    ]
    for _rid, rec in steps:
        ls.append(rec)
        if pause:
            time.sleep(pause)
    rs.save(_run(CHILD, parent=ROOT, status=RunStatus.COMPLETED))
    rs.save(_run(ROOT, status=RunStatus.COMPLETED, vars={"prompt": "write a.txt"}))


@pytest.mark.parametrize("client", ["file", "sqlite"], indirect=True)
def test_view_shows_the_child_approval_and_serves_etag(client: TestClient) -> None:
    _seed_delegated_approval()
    r = client.get(f"/api/gateway/runs/{ROOT}/view", headers=H)
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["version"] == "replay_view_v1" and v["root_run_id"] == ROOT
    assert v["status"] == "waiting"
    it = v["interaction"]
    assert it["kind"] == "tool_approval" and it["run_id"] == CHILD and it["wait_key"] == KEY
    assert [c["name"] for c in it["tool_calls"]] == ["write_file"]
    assert it["command"] == {"type": "resume", "run_id": CHILD, "payload": {"wait_key": KEY}}
    assert [row["run_id"] for row in v["runs"]] == [ROOT, CHILD]
    assert v["messages"][0]["text"] == "write a.txt"
    assert "$slim" not in r.text
    etag = r.headers["etag"]
    assert etag == f'"{v["cursor"]["token"]}"' and v["cursor"]["runs"] == {ROOT: 2, CHILD: 2}

    again = client.get(f"/api/gateway/runs/{ROOT}/view", headers={**H, "If-None-Match": etag})
    assert again.status_code == 304 and again.content == b""

    _finish_turn()
    done = client.get(f"/api/gateway/runs/{ROOT}/view", headers={**H, "If-None-Match": etag})
    assert done.status_code == 200 and done.headers["etag"] != etag
    dv = done.json()
    assert dv["status"] == "completed" and dv["interaction"] is None
    assert dv["messages"][-1]["text"] == "Done: a.txt written."
    tool = [a for a in dv["activities"] if a["kind"] == "tool"][0]
    assert tool["status"] == "completed" and tool["tool_calls"][0]["status"] == "completed"


def test_view_of_an_unknown_run_is_404(client: TestClient) -> None:
    assert client.get("/api/gateway/runs/nope/view", headers=H).status_code == 404
    assert client.get("/api/gateway/runs/nope/view/stream", headers=H).status_code == 404


def _follow(client: TestClient, url: str, headers: Dict[str, str], out: List[Dict[str, Any]]) -> None:
    with client.stream("GET", url, headers=headers) as resp:
        assert resp.status_code == 200
        buf = ""
        for chunk in resp.iter_text():
            buf += chunk
            while "\n\n" in buf:
                frame, buf = buf.split("\n\n", 1)
                out.extend(_sse(frame + "\n\n"))
            if out and out[-1].get("event") == "done":
                return


def _fold(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    view: Dict[str, Any] = {}
    for e in events:
        if e.get("event") == "view":
            view = e["data"]
        elif e.get("event") == "delta":
            view = apply_delta(view, e["data"])
    return view


def test_two_clients_converge_live_and_share_one_fold(client: TestClient) -> None:
    from abstractgateway.replay_view import get_view_hub

    _seed_delegated_approval()
    a: List[Dict[str, Any]] = []
    b: List[Dict[str, Any]] = []
    url = f"/api/gateway/runs/{ROOT}/view/stream?heartbeat_s=0.5"
    ta = threading.Thread(target=_follow, args=(client, url, H, a))
    tb = threading.Thread(target=_follow, args=(client, url, H, b))
    ta.start()
    tb.start()
    # Starlette's TestClient hands a streamed body over only once the response ends, so the
    # followers are read after the fact: they subscribe now (the first frame must still show the
    # approval: proof they joined before the turn finished), the turn finishes while they stream.
    time.sleep(1.0)
    t0 = time.monotonic()
    _finish_turn(pause=0.05)
    ta.join(30)
    tb.join(30)
    # The appends themselves woke the streams (the entry's append subscription), well before
    # the 5 s safety check could have.
    assert time.monotonic() - t0 < 3.0
    assert not ta.is_alive() and not tb.is_alive(), "streams did not end with done"
    assert a[0]["event"] == "view" and b[0]["event"] == "view"
    assert a[0]["data"]["interaction"]["kind"] == "tool_approval"
    assert b[0]["data"]["interaction"]["kind"] == "tool_approval"
    assert any(e.get("event") == "delta" for e in a) and any(e.get("event") == "delta" for e in b)
    assert a[-1]["event"] == "done" and b[-1]["event"] == "done"
    final = client.get(f"/api/gateway/runs/{ROOT}/view", headers=H).json()
    assert _fold(a) == final
    assert _fold(b) == final
    # Every delta carries its cursor token as the SSE id, and ids only move forward.
    ids = [e["id"] for e in a if e.get("event") in ("view", "delta")]
    assert ids[-1] == final["cursor"]["token"]
    # Every record was folded exactly once for both clients (4 seeded + 5 appended), never once
    # per client; a fold pass covers whatever was appended since the last one.
    entry = get_view_hub().entry(ledger_store=_stores()[1], run_store=_stores()[0], root_run_id=ROOT)
    assert entry.records_folded == 9
    assert entry.folds <= 2 + 5  # the seed is folded in up to two passes (the child is found in the first)


def test_reconnect_with_last_event_id_gets_one_delta_to_the_same_view(client: TestClient) -> None:
    _seed_delegated_approval()
    first = client.get(f"/api/gateway/runs/{ROOT}/view", headers=H)
    held_view, held_token = first.json(), first.json()["cursor"]["token"]
    _finish_turn()
    events: List[Dict[str, Any]] = []
    _follow(client, f"/api/gateway/runs/{ROOT}/view/stream", {**H, "Last-Event-ID": held_token}, events)
    kinds = [e["event"] for e in events]
    assert kinds == ["delta", "done"], kinds
    final = client.get(f"/api/gateway/runs/{ROOT}/view", headers=H).json()
    assert apply_delta(held_view, events[0]["data"]) == final
    # The current token: nothing to send but `done`; a token that is not one: the whole view.
    events2: List[Dict[str, Any]] = []
    _follow(client, f"/api/gateway/runs/{ROOT}/view/stream", {**H, "Last-Event-ID": final["cursor"]["token"]}, events2)
    assert [e["event"] for e in events2] == ["done"]
    events3: List[Dict[str, Any]] = []
    _follow(client, f"/api/gateway/runs/{ROOT}/view/stream?after=garbage", H, events3)
    assert [e["event"] for e in events3] == ["view", "done"] and events3[0]["data"] == final


def test_view_at_a_cursor_is_the_ledger_as_it_stood(client: TestClient) -> None:
    """A push opened at its seq (root backlog 1058): `?at=<token>` rebuilds the view at that cursor."""
    _seed_delegated_approval()
    held = client.get(f"/api/gateway/runs/{ROOT}/view", headers=H).json()
    _finish_turn()
    now = client.get(f"/api/gateway/runs/{ROOT}/view", headers=H).json()
    assert now["status"] == "completed"
    at = client.get(f"/api/gateway/runs/{ROOT}/view", params={"at": held["cursor"]["token"]}, headers=H)
    assert at.status_code == 200, at.text
    v = at.json()
    assert v["cursor"] == held["cursor"]
    assert v["status"] == "waiting" and v["interaction"]["kind"] == "tool_approval"
    assert v["interaction"]["run_id"] == CHILD and v["activities"] == held["activities"]
    assert client.get(f"/api/gateway/runs/{ROOT}/view", params={"at": "zzzzzzzz:3"}, headers=H).status_code == 404
    assert client.get(f"/api/gateway/runs/{ROOT}/view", params={"at": "not a token"}, headers=H).status_code == 404


def test_a_read_that_changes_nothing_broadcasts_nothing(client: TestClient) -> None:
    """GETs and safety polls re-read open runs' snapshots; an unchanged snapshot is no delta."""
    from abstractgateway.replay_view import get_view_hub, poll, read_view

    _seed_delegated_approval()
    rs, ls = _stores()
    entry = get_view_hub().entry(ledger_store=ls, run_store=rs, root_run_id=ROOT)
    _token, _body, first = read_view(entry)
    assert first is not None
    _token2, _body2, again = read_view(entry)
    assert again is None
    assert poll(entry, reload_open_snapshots=True) == (False, False)


def _seed_case(case: Dict[str, Any]) -> None:
    """Put a conformance case's ledgers and snapshots in the gateway's own stores, seq kept."""
    from abstractgateway.ledger_tail import _unwrap_chain
    from abstractruntime.storage.sqlite import SqliteLedgerStore, _runstate_from_dict

    rs, ls = _stores()
    layers = list(_unwrap_chain(ls))
    for rid, recs in case["records"].items():
        sq = next((x for x in layers if isinstance(x, SqliteLedgerStore)), None)
        if sq is not None:
            sq.import_records(rid, recs)
            continue
        jsonl = next(x for x in layers if callable(getattr(x, "_path", None)))
        with jsonl._path(rid).open("w", encoding="utf-8") as f:
            for rec in recs:
                f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
    for snap in case["runs"].values():
        rs.save(_runstate_from_dict(dict(snap)))


def _conformance_cases() -> List[str]:
    from abstractruntime.replay.conformance import case_names

    return case_names()


@pytest.mark.parametrize("case_name", _conformance_cases())
@pytest.mark.parametrize("client", ["file", "sqlite"], indirect=True)
def test_the_gateway_serves_every_conformance_case_as_the_runtime_replays_it(client: TestClient, case_name: str) -> None:
    """The view a client gets is the runtime's reducer over the stored ledgers (ADR 0009)."""
    from abstractruntime.replay import replay
    from abstractruntime.replay.conformance import load_case

    assert len(_conformance_cases()) >= 8
    rs, _ls = _stores()
    case = load_case(case_name)
    _seed_case(case)
    root = case["manifest"]["root_run_id"]
    got = client.get(f"/api/gateway/runs/{root}/view", headers=H)
    assert got.status_code == 200, got.text
    # The stored snapshots are RunStates (what a host holds); the reducer over them and the
    # stored records is the conformance answer, and it is the case's expected view.
    snaps = {rid: rs.load(rid) for rid in case["runs"]}
    want = replay(case["records"], {k: v for k, v in snaps.items() if v is not None}, root_run_id=root).to_json()
    assert got.json() == want
    assert got.json() == case["expected_view"]
    assert "$slim" not in got.text


def test_periodic_checks_are_shared_by_the_streams_of_one_view(client: TestClient) -> None:
    """50 idle streams of one run cost one check per period, not 50 (benchmark finding)."""
    from abstractgateway.replay_view import get_view_hub, poll, read_view

    _seed_delegated_approval()
    rs, ls = _stores()
    entry = get_view_hub().entry(ledger_store=ls, run_store=rs, root_run_id=ROOT)
    read_view(entry)
    reads: List[str] = []
    real_load = rs.load

    def counting_load(run_id: str):
        reads.append(run_id)
        return real_load(run_id)

    rs.load = counting_load  # type: ignore[method-assign]
    try:
        for _ in range(50):  # every stream's check falls in the same period
            poll(entry, reload_open_snapshots=True, check_every=4.0, reload_every=4.0)
    finally:
        rs.load = real_load  # type: ignore[method-assign]
    assert len(reads) == 2  # the two open runs, re-read once for all 50 streams
