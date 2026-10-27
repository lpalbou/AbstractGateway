"""`GET /automations` under load (2026-10-26 gateway load investigation).

- the listing is O(automations): 50 automations x 100 occurrences each answer without one
  occurrence-tree walk, one full ledger read or more than one full run-index listing;
- identical concurrent requests share ONE computation (single flight), its answer is kept a
  few seconds and dropped by a seen cursor, a command or a ledger append;
- the tray backs off a failing `/automations` fetch (10, 20, 40, 80, 120, 120 s) and says
  "The gateway is slow to answer" for a timeout, keeping the red "Not responding" for no
  answer at all.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, List

import pytest

from timing import timing_budget  # noqa: E402 - tests/timing.py (wall-clock budgets)
from fastapi.testclient import TestClient

from automations_fixtures import HEADERS, controller_run, gateway_env, occurrence_run, save_runs

N_AUTOMATIONS = 50
N_OCCURRENCES = 100


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway_env(monkeypatch, tmp_path)
    from abstractgateway.app import app
    from abstractgateway.routes import automations as R

    R._LIST_MEMO.clear()
    with TestClient(app) as c:
        yield c
    R._LIST_MEMO.clear()


def _svc():
    from abstractgateway.service import get_gateway_service

    return get_gateway_service()


def _principal():
    import types

    return types.SimpleNamespace(tenant_id="default", user_id="admin")


def _populate(n_automations: int, n_occurrences: int) -> List[str]:
    """Controllers with `n_occurrences` admitted/dispatched/completed occurrences in their
    ledgers, a finished run per occurrence with an answer, and one run waiting on a person."""
    from abstractruntime.automations.ledger import _step_record
    from abstractruntime.core.models import RunStatus, WaitReason, WaitState

    svc = _svc()
    ledger = svc.host.ledger_store
    ids: List[str] = []
    for a in range(n_automations):
        controller = controller_run(title=f"Automation {a}", attention_seq=n_occurrences // 10, created_at=f"2026-10-0{1 + a % 9}T09:{a % 60:02d}:00+00:00")
        state = controller.vars["_runtime"]["automation"]
        state["next_index"] = n_occurrences + 1
        state["last_outcome"] = {"run_id": "", "index": n_occurrences, "status": "completed", "attempts": 1, "finished_at": "f"}
        aid = str(controller.run_id)
        runs = [controller]
        seq = 0
        for i in range(1, n_occurrences + 1):
            child = occurrence_run(aid, index=i, session_id=f"occ-session:{aid}:{i}")
            child.output = {"response": f"answer {i} " + "x" * 400, "success": True}
            runs.append(child)
            attention = None
            if i % 10 == 0:
                seq += 1
                attention = {"kind": "notify", "seq": seq, "title": "t", "body": "b"}
            for name, disc, payload in (
                ("automation.admitted", i, {"run_id": child.run_id, "index": i, "revision": 1, "event_id": f"e{i}",
                                            "trigger_envelope": {"fired_at": f"2026-10-01T{i % 24:02d}:00:00+00:00", "source_id": "schedule", "source_version": 1},
                                            "prepared": {"input_data": {"prompt": f"prompt {i} " + "p" * 400}}}),
                ("automation.dispatched", f"{child.run_id}", {"run_id": child.run_id, "index": i, "attempt": 1}),
                ("automation.completed", i, {"run_id": child.run_id, "index": i, "status": "completed", "attempts": 1,
                                             "finished_at": "2026-10-01T10:00:00+00:00", "notify": None, "attention": attention}),
            ):
                ledger.append(_step_record(controller, name=name, key=f"automation:{name}:{aid}:{disc}", payload=payload, node_id="n", at="2026-10-01T10:00:00+00:00"))
        if a % 10 == 0:
            waiting = occurrence_run(aid, index=n_occurrences, session_id="w", role="descendant", parent_run_id=runs[-1].run_id)
            waiting.status = RunStatus.WAITING
            waiting.waiting = WaitState(reason=WaitReason.USER, wait_key=f"ask:{waiting.run_id}", prompt="Proceed?")
            runs.append(waiting)
        save_runs(*runs)
        ids.append(aid)
    return ids


def test_the_listing_is_o_automations_50_x_100(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.routes import automations as R
    from abstractruntime.storage.json_files import JsonFileRunStore, JsonlLedgerStore

    ids = _populate(N_AUTOMATIONS, N_OCCURRENCES)
    svc = _svc()
    # Reference answer: what one automation's detail summary says (it reads the same seams).
    expected = R.automation_summary_row(svc, _principal(), svc.host.run_store.load(ids[0]))

    calls = {"children": 0, "ledger_list": 0, "full_index": 0}
    real_children, real_list, real_index = JsonFileRunStore.list_children, JsonlLedgerStore.list, JsonFileRunStore.list_run_index

    def children(self, **kw):
        calls["children"] += 1
        return real_children(self, **kw)

    def ledger_list(self, run_id):
        calls["ledger_list"] += 1
        return real_list(self, run_id)

    def index(self, **kw):
        if kw.get("status") is None and kw.get("session_id") is None:
            calls["full_index"] += 1
        return real_index(self, **kw)

    monkeypatch.setattr(JsonFileRunStore, "list_children", children)
    monkeypatch.setattr(JsonlLedgerStore, "list", ledger_list)
    monkeypatch.setattr(JsonFileRunStore, "list_run_index", index)

    t0 = time.perf_counter()
    out = R._list(svc, _principal(), None, None, 200, False)
    cold = time.perf_counter() - t0
    t0 = time.perf_counter()
    if os.environ.get("LIST_LOAD_PROFILE"):
        import cProfile, pstats
        pr = cProfile.Profile(); pr.enable()
    again = R._list(svc, _principal(), None, None, 200, False)
    warm = time.perf_counter() - t0
    if os.environ.get("LIST_LOAD_PROFILE"):
        pr.disable(); pstats.Stats(pr).sort_stats("cumulative").print_stats(25)
    print(f"\n50 automations x 100 occurrences: cold {cold * 1000:.0f} ms, warm {warm * 1000:.0f} ms")

    assert len(out["items"]) == N_AUTOMATIONS and again == out
    row = next(i for i in out["items"] if i["automation_id"] == ids[0])
    assert row == expected
    assert row["last_occurrence"]["index"] == N_OCCURRENCES and row["last_occurrence"]["excerpt"].startswith(f"answer {N_OCCURRENCES} ")
    assert row["attention"]["pending_waits"] == 1 and row["attention"]["waits"][0]["kind"] == "ask_user"
    assert sum(1 for i in out["items"] if i["attention"]["pending_waits"]) == N_AUTOMATIONS // 10
    # O(automations): no tree walk, no whole-ledger read, one full index listing (cached after).
    assert calls == {"children": 0, "ledger_list": 0, "full_index": 1}
    # Generous bounds for a loaded CI box; the operator's data copy measures 0.22 s / 0.02 s.
    timing_budget(cold, 3.0, "GET /automations cold")
    timing_budget(warm, 1.0, "GET /automations warm")


def test_fourteen_concurrent_listings_compute_once(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.routes import automations as R

    save_runs(controller_run(title="One"), controller_run(title="Two"))
    real = R._list
    started = threading.Event()

    def slow_list(*a: Any, **kw: Any):
        started.set()
        time.sleep(0.3)  # long enough for every caller to arrive while it runs
        return real(*a, **kw)

    monkeypatch.setattr(R, "_list", slow_list)
    svc, principal = _svc(), _principal()
    before = R._LIST_MEMO.computations
    barrier = threading.Barrier(14)
    results: List[Any] = [None] * 14

    def call(n: int) -> None:
        barrier.wait()
        results[n] = R.list_automations_shared(svc, principal, None, None, 50, False)

    threads = [threading.Thread(target=call, args=(n,)) for n in range(14)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert R._LIST_MEMO.computations - before == 1
    assert all(r == results[0] for r in results) and len(results[0]["items"]) == 2


def test_fourteen_concurrent_http_requests_compute_once(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.routes import automations as R

    save_runs(controller_run(title="One"))
    real = R._list

    def slow_list(*a: Any, **kw: Any):
        time.sleep(0.3)
        return real(*a, **kw)

    monkeypatch.setattr(R, "_list", slow_list)
    before = R._LIST_MEMO.computations
    barrier = threading.Barrier(14)
    codes: List[int] = []

    def call() -> None:
        barrier.wait()
        codes.append(client.get("/api/gateway/automations", headers=HEADERS).status_code)

    threads = [threading.Thread(target=call) for _ in range(14)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert codes == [200] * 14
    assert R._LIST_MEMO.computations - before == 1


def test_the_answer_is_kept_briefly_and_dropped_by_changes(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.routes import automations as R
    from abstractruntime.automations.ledger import _step_record

    controller = controller_run(attention_seq=2)
    save_runs(controller)
    url = "/api/gateway/automations"
    memo = R._LIST_MEMO

    def computed(fn) -> int:
        before = memo.computations
        fn()
        return memo.computations - before

    get = lambda: client.get(url, headers=HEADERS).json()  # noqa: E731
    assert computed(get) == 1
    assert computed(get) == 0  # kept
    # The seen cursor moves: dropped, and the next answer shows it.
    assert get()["items"][0]["attention"]["unseen_count"] == 2
    client.post(f"{url}/{controller.run_id}/seen", headers=HEADERS, json={"attention_cursor": "att1:2"})
    assert computed(get) == 1
    assert get()["items"][0]["attention"]["unseen_count"] == 0
    # Any ledger append on the plane (an occurrence step, a wait, a decision): dropped.
    _svc().host.ledger_store.append(_step_record(controller, name="automation.coalesced", key=f"k-{uuid.uuid4()}", payload={}, node_id="n", at="t"))
    assert computed(get) == 1
    assert computed(get) == 0
    # A command: dropped.
    r = client.post(f"{url}/{controller.run_id}/commands", headers=HEADERS, json={"command_id": str(uuid.uuid4()), "type": "automation.archive", "payload": {}})
    assert r.status_code in (200, 202), r.text
    assert computed(get) == 1
    # Time alone: dropped after the TTL (what this process cannot see).
    real_clock = memo._clock
    monkeypatch.setattr(memo, "_clock", lambda: real_clock() + memo.ttl_s + 1)
    assert computed(get) == 1


# ------------------------------------------------------------------------------- tray


class _Answer:
    def __init__(self, ok: bool, *, timed_out: bool = False, data: Any = None) -> None:
        self.ok, self.timed_out, self.data = ok, timed_out, data
        self.status = 200 if ok else 0
        self.error = None if ok else ("TimeoutError: timed out" if timed_out else "unreachable: [Errno 61] Connection refused")


class _Client:
    def __init__(self) -> None:
        self.answers: List[_Answer] = []
        self.automation_calls = 0

    def automations(self):
        self.automation_calls += 1
        return self.answers.pop(0)


def _sampler(client: _Client):
    from abstractgateway.tray.sampler import Sampler

    return Sampler(client)  # type: ignore[arg-type]


def test_tray_backoff_ladder() -> None:
    from abstractgateway.tray.sampler import automations_backoff_s

    ladder, s = [], 0.0
    for _ in range(7):
        s = automations_backoff_s(s)
        ladder.append(s)
    assert ladder == [10.0, 20.0, 40.0, 80.0, 120.0, 120.0, 120.0]


def test_tray_backs_off_a_slow_automations_fetch_and_resets_on_an_answer() -> None:
    c = _Client()
    sampler = _sampler(c)
    c.answers = [_Answer(False, timed_out=True)] * 3 + [_Answer(True, data={"items": []})]
    now = 1000.0
    sampler._sample_automations(now)
    assert c.automation_calls == 1
    snap = sampler.snapshot()
    assert snap.automations_error == "The gateway is slow to answer" and snap.slow
    # Every slow pass (5 s) before the pause is over: no new request.
    for t in (5.0, 9.9):
        sampler._sample_automations(now + t)
    assert c.automation_calls == 1
    sampler._sample_automations(now + 10.0)
    assert c.automation_calls == 2
    sampler._sample_automations(now + 10.0 + 19.9)
    assert c.automation_calls == 2
    sampler._sample_automations(now + 30.0)
    assert c.automation_calls == 3
    sampler._sample_automations(now + 30.0 + 40.0)  # the answer
    assert c.automation_calls == 4
    snap = sampler.snapshot()
    assert snap.automations_error is None and not snap.slow
    assert sampler._automations_retry_at == 0.0 and sampler._automations_backoff_s == 0.0


def test_tray_slow_probe_is_not_unreachable_but_no_answer_is() -> None:
    from abstractgateway.tray.menu_model import state_lines

    sampler = _sampler(_Client())
    with sampler._lock:
        sampler._note_answer_locked()
        for _ in range(5):
            sampler._note_failure_locked(_Answer(False, timed_out=True))
    snap = sampler.snapshot()
    assert snap.gateway_state == "running" and snap.slow
    assert state_lines(snap)[1] == "The gateway is slow to answer"
    with sampler._lock:
        for _ in range(3):
            sampler._note_failure_locked(_Answer(False))  # refused: no answer at all
    snap = sampler.snapshot()
    assert snap.gateway_state == "unreachable" and not snap.slow
    # Timeouts with nothing answering for SLOW_UNREACHABLE_S: unreachable too.
    with sampler._lock:
        sampler._note_answer_locked()
        sampler._last_answer -= 1000.0
        for _ in range(3):
            sampler._note_failure_locked(_Answer(False, timed_out=True))
    assert sampler.snapshot().gateway_state == "unreachable"
