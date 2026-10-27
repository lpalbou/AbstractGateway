"""Regressions for the adversarial review of the replay view routes (2026-10-27).

Each test was written by the reviewer as a failing proof and passes since the fixes: a pause
(snapshot only) changes the ETag and reaches a reconnect; resume_run after a pause is not stuck;
`GET /view?at=` never swallows a delta live streams need; `?at=` shows only the runs of that
cursor; a client leaving while subscribing is unregistered.
"""
from __future__ import annotations
import asyncio
import json
import pytest
from test_gateway_replay_view import (  # noqa: E402
    client, _stores, _run, _rec, _seed_delegated_approval, _finish_turn, ROOT, CHILD, KEY, H, CALL,
)
from abstractruntime.core.models import RunStatus, StepStatus, WaitReason, WaitState
from abstractruntime.replay import apply_delta, replay

LLM = {"type": "llm_call", "payload": {"prompt": "hi", "provider": "p", "model": "m"}, "result_key": "r"}


def _seed_running() -> None:
    rs, ls = _stores()
    rs.save(_run(ROOT, status=RunStatus.RUNNING, vars={"prompt": "hello"}))
    ls.append(_rec(ROOT, "s1", "llm", StepStatus.STARTED, effect=LLM, idem="i1"))
    ls.append(_rec(ROOT, "s1", "llm", StepStatus.COMPLETED, effect=LLM, idem="i1", result={"content": "thinking"}))


def _pause() -> None:
    """Runtime.pause_run on a RUNNING run: snapshot only, no ledger record (core/runtime.py pause_run)."""
    rs, _ = _stores()
    w = WaitState(reason=WaitReason.USER, wait_key=f"pause:{ROOT}", prompt="Paused", details={"kind": "pause"})
    rs.save(_run(ROOT, status=RunStatus.WAITING, waiting=w, vars={"prompt": "hello", "_runtime": {"control": {"paused": True}}}))


@pytest.mark.parametrize("client", ["file", "sqlite"], indirect=True)
def test_etag_304_serves_stale_view_after_snapshot_only_change(client) -> None:
    r1 = client.get(f"/api/gateway/runs/{ROOT}/view", headers=H) if False else None
    _seed_running()
    r1 = client.get(f"/api/gateway/runs/{ROOT}/view", headers=H)
    assert r1.status_code == 200 and r1.json()["status"] == "running"
    etag = r1.headers["etag"]
    _pause()
    fresh = client.get(f"/api/gateway/runs/{ROOT}/view", headers=H)
    print("\nbody status after pause:", fresh.json()["status"], "etag same:", fresh.headers["etag"] == etag)
    cond = client.get(f"/api/gateway/runs/{ROOT}/view", headers={**H, "If-None-Match": etag})
    print("conditional GET with the old ETag ->", cond.status_code)
    assert fresh.json()["status"] == "waiting"
    assert cond.status_code == 200, "304 Not Modified while the view changed running -> waiting"


@pytest.mark.parametrize("client", ["file"], indirect=True)
def test_reconnect_with_current_token_misses_snapshot_only_change(client) -> None:
    from abstractgateway.replay_view import _Subscriber, get_view_hub, subscribe, unsubscribe
    _seed_running()
    held = client.get(f"/api/gateway/runs/{ROOT}/view", headers=H).json()
    _pause()  # client is disconnected while the operator pauses the run
    rs, ls = _stores()
    entry = get_view_hub().entry(ledger_store=ls, run_store=rs, root_run_id=ROOT)
    loop = asyncio.new_event_loop()
    sub = _Subscriber(loop=loop)
    frames, terminal = subscribe(entry, sub, held["cursor"]["token"])
    unsubscribe(entry, sub)
    current = entry.view.to_json()
    print("\nheld status:", held["status"], "| current:", current["status"], "| frames on reconnect:", frames)
    assert frames, "reconnect sent nothing; client keeps showing 'running' for a paused run"


@pytest.mark.parametrize("client", ["file"], indirect=True)
def test_resume_run_after_pause_view_stuck_waiting_and_differs_from_fresh_replay(client) -> None:
    _seed_running()
    client.get(f"/api/gateway/runs/{ROOT}/view", headers=H)
    _pause()
    assert client.get(f"/api/gateway/runs/{ROOT}/view", headers=H).json()["status"] == "waiting"
    rs, ls = _stores()
    rs.save(_run(ROOT, status=RunStatus.RUNNING, vars={"prompt": "hello"}))  # Runtime.resume_run: snapshot only
    served = client.get(f"/api/gateway/runs/{ROOT}/view", headers=H).json()
    fresh = replay({ROOT: ls.list(ROOT)}, {ROOT: rs.load(ROOT)}, root_run_id=ROOT).to_json()
    print("\nserved status:", served["status"], served["open_waits"], "| fresh replay:", fresh["status"])
    assert served["status"] == fresh["status"]


@pytest.mark.parametrize("client", ["file"], indirect=True)
def test_get_at_swallows_a_delta_live_subscribers_never_receive(client) -> None:
    from abstractgateway.replay_view import _Subscriber, get_view_hub, subscribe, unsubscribe, poll, read_view_at
    _seed_delegated_approval()
    rs, ls = _stores()
    entry = get_view_hub().entry(ledger_store=ls, run_store=rs, root_run_id=ROOT)
    loop = asyncio.new_event_loop()
    sub = _Subscriber(loop=loop)
    frames, _ = subscribe(entry, sub, None)
    # make the entry not followed so no pump fires (as in the split API process / poll mode)
    if entry.unsubscribe:
        entry.unsubscribe(); entry.unsubscribe = None
    view0 = json.loads(frames[0].split(b"data: ", 1)[1])
    token0 = view0["cursor"]["token"]
    _finish_turn()  # 5 records appended
    assert read_view_at(entry, token0) is not None     # someone opens a push: GET /view?at=<token0>
    poll(entry, reload_open_snapshots=True)             # the stream's periodic check
    loop.run_until_complete(asyncio.sleep(0))           # deliver queued frames
    got = []
    while not sub.queue.empty():
        got.append(sub.queue.get_nowait())
    unsubscribe(entry, sub)
    client_view = view0
    for f in got:
        client_view = apply_delta(client_view, json.loads(f.split(b"data: ", 1)[1]))
    server = entry.view.to_json()
    print("\nframes after the ?at= GET:", len(got))
    print("client messages:", [m["text"] for m in client_view["messages"]])
    print("server messages:", [m["text"] for m in server["messages"]])
    assert client_view == server


@pytest.mark.parametrize("client", ["file"], indirect=True)
def test_view_at_shows_child_that_did_not_exist_yet(client) -> None:
    rs, ls = _stores()
    rs.save(_run(ROOT, status=RunStatus.RUNNING, vars={"prompt": "p"}))
    ls.append(_rec(ROOT, "s0", "llm", StepStatus.STARTED, effect=LLM, idem="i0"))
    ls.append(_rec(ROOT, "s0", "llm", StepStatus.COMPLETED, effect=LLM, idem="i0", result={"content": "x"}))
    early = client.get(f"/api/gateway/runs/{ROOT}/view", headers=H).json()
    assert [r["run_id"] for r in early["runs"]] == [ROOT]
    _seed_delegated_approval()  # later: the root delegates to CHILD
    _finish_turn()
    at = client.get(f"/api/gateway/runs/{ROOT}/view", params={"at": early["cursor"]["token"]}, headers=H).json()
    print("\nruns at the early cursor:", [(r["run_id"][:8], r["status"]) for r in at["runs"]], "root:", at["status"])
    assert [r["run_id"] for r in at["runs"]] == [ROOT], "a child started AFTER the cursor appears in the ?at= view"


import time
class _Req:
    headers: dict = {}
    async def is_disconnected(self) -> bool:
        return False


@pytest.mark.parametrize("client", ["file"], indirect=True)
def test_disconnect_during_subscribe_leaks_subscriber(client, monkeypatch) -> None:
    import abstractgateway.replay_view as rv
    import abstractgateway.routes.replay_view as routes
    from abstractgateway.replay_view import get_view_hub

    _seed_delegated_approval()
    real = rv.subscribe

    def slow_subscribe(entry, sub, last):  # a large tree: subscribe takes a while (2.6 s measured on 84k records)
        time.sleep(0.3)
        return real(entry, sub, last)

    monkeypatch.setattr(routes, "subscribe", slow_subscribe)
    rs, ls = _stores()

    async def scenario():
        resp = await routes.stream_run_view(_Req(), ROOT, None, 5.0)
        gen = resp.body_iterator
        task = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0.1)
        task.cancel()                      # Starlette cancels the body on client disconnect
        try:
            await task
        except asyncio.CancelledError:
            pass
        await gen.aclose()
        await asyncio.sleep(0.6)           # the worker thread finishes subscribe()
        entry = get_view_hub().entry(ledger_store=ls, run_store=rs, root_run_id=ROOT)
        await asyncio.to_thread(_finish_turn, 0.05)   # the run goes on: appends wake the entry's pump
        await asyncio.sleep(1.0)
        entry._queued = sum(s.queue.qsize() for s in entry.subscribers)
        return entry

    entry = asyncio.run(scenario())
    print("\nsubscribers after the client went away:", len(entry.subscribers), "| append subscription live:", entry.unsubscribe is not None)
    print("frames queued for nobody (live loop):", entry._queued)
    assert entry.subscribers == [], "subscriber leaked: entry pinned, frames queued forever"
