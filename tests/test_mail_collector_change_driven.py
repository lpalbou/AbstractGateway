"""The mail notification check reads nothing when nothing changed, and only what changed
otherwise (2026-10-26: every 15 s it listed every automation, read every automation's ledger,
walked the occurrence trees and listed completed/failed runs twice: 7-25% of a core idle)."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from timing import timing_budget  # noqa: E402 - tests/timing.py (wall-clock budgets)

from email_fixtures import *  # noqa: F401,F403 - fixtures
from email_fixtures import ALICE, connect_body, plane_of

from abstractruntime.core.models import RunState, RunStatus, StepRecord, StepStatus, WaitReason, WaitState
from abstractruntime.storage.json_files import JsonFileRunStore, JsonlLedgerStore
from abstractruntime.storage.observable import ObservableLedgerStore

pytestmark = pytest.mark.integration

ALL_ON = {"automation_result": True, "automation_failed": True, "approval_needed": True, "job_finished": True, "job_failed": True}


def _connect(gateway, imap, smtp) -> None:
    r = gateway["client"].put("/api/gateway/me/email", headers=gateway["alice"], json=connect_body(ALICE, imap, smtp))
    assert r.status_code == 200, r.text
    r = gateway["client"].put("/api/gateway/me/notifications", headers=gateway["alice"], json={"email": ALL_ON})
    assert r.status_code == 200, r.text


class _Counting:
    """Counts every store method a pass calls (the proof that an untouched store is not read)."""

    def __init__(self, inner, names) -> None:
        self._inner = inner
        self.calls = {n: 0 for n in names}
        for n in names:
            real = getattr(inner, n)

            def wrapper(*a, _n=n, _real=real, **k):
                self.calls[_n] += 1
                return _real(*a, **k)

            setattr(self, n, wrapper)

    def change_token(self):
        return self._inner.change_token()

    def changed_run_ids(self, since):
        return self._inner.changed_run_ids(since)

    def total(self) -> int:
        return sum(self.calls.values())


def _run(rid, status, *, notify=None, waiting=None, vars0=None, parent=None, updated_at="2999-01-01T00:00:00+00:00"):
    v = dict(vars0 or {})
    if notify:
        v["_runtime"] = {"notify": notify}
    return RunState(run_id=rid, workflow_id="Nightly build", status=status, current_node="n", vars=v, waiting=waiting,
                    parent_run_id=parent, actor_id="gateway", updated_at=updated_at)


@pytest.fixture
def stores(tmp_path, monkeypatch):
    import abstractruntime.automation_queries as aq
    import abstractruntime.automations.attention as att

    from abstractgateway.mail import notifications

    monkeypatch.setattr(notifications, "_COLLECT_MEMO", {}, raising=False)
    run_store = JsonFileRunStore(tmp_path / "runs")
    ledger = ObservableLedgerStore(JsonlLedgerStore(tmp_path / "runs"))
    rs = _Counting(run_store, ["list_runs", "list_run_index", "list_children", "load"])
    ls = _Counting(ledger, ["list", "append"])
    records = {"auto-1": []}
    automations = [{"automation_id": "auto-1", "title": "Daily digest", "status": "active"}]

    def fake_list_attention(ledger_store, aid, *, after_seq=0, cursor=None, limit=50):
        ledger_store.list(aid)  # the real one reads the automation's ledger: counted
        items = [i for i in records.get(aid, []) if i["seq"] > int(after_seq or 0)]
        return {"items": items[:limit], "next_cursor": None}

    def fake_list_automations(run_store, status=None, cursor=None, limit=50):
        run_store.list_run_index(limit=1)  # the real one lists the controller rows: counted
        return SimpleNamespace(items=list(automations), next_cursor=None)

    def fake_summary(run):
        meta = (run.vars or {}).get("_meta") or {}
        if not isinstance(meta.get("automation"), dict):
            raise LookupError("not an automation")
        return dict(automations[0])

    monkeypatch.setattr(aq, "list_automations", fake_list_automations)
    monkeypatch.setattr(aq, "automation_summary", fake_summary)
    monkeypatch.setattr(att, "list_attention", fake_list_attention)
    monkeypatch.setattr(att, "pending_waits", lambda run_store, aid, limit=20: [])
    svc = SimpleNamespace(host=SimpleNamespace(run_store=rs, ledger_store=ls, runtime=None))
    return SimpleNamespace(run_store=run_store, ledger=ledger, rs=rs, ls=ls, svc=svc, records=records)


def _collect(plane, svc):
    from abstractgateway.mail.notifications import NotificationCollector

    return NotificationCollector(plane, svc).collect()


def test_an_untouched_store_is_not_read(gateway, imap, smtp, stores) -> None:
    _connect(gateway, imap, smtp)
    plane = plane_of("alice")
    for i in range(50):
        stores.run_store.save(_run(f"old-{i}", RunStatus.COMPLETED))
    _collect(plane, stores.svc)  # baseline (full)
    _collect(plane, stores.svc)  # first non-baseline pass: full, records the tokens
    rs0, ls0 = stores.rs.total(), stores.ls.total()
    for _ in range(5):
        out = _collect(plane, stores.svc)
        assert out == {"queued": 0, "skipped": 1}
    assert stores.rs.total() == rs0 and stores.ls.total() == ls0


def test_a_change_is_read_alone_and_queued(gateway, imap, smtp, stores) -> None:
    _connect(gateway, imap, smtp)
    plane = plane_of("alice")
    stores.run_store.save(_run("auto-1", RunStatus.WAITING, vars0={"_meta": {"automation": {"title": "Daily digest"}}}))
    for i in range(200):
        stores.run_store.save(_run(f"old-{i}", RunStatus.COMPLETED))
    _collect(plane, stores.svc)  # baseline
    _collect(plane, stores.svc)  # full, tokens recorded
    stores.rs.calls.update({k: 0 for k in stores.rs.calls})
    stores.ls.calls.update({k: 0 for k in stores.ls.calls})

    # A job that asked, a run waiting on a person, a person's wait inside the automation's tree,
    # and a new attention item in the automation's ledger.
    stores.run_store.save(_run("job-ok", RunStatus.COMPLETED, notify={"on": ["finished"], "channels": ["email"]}))
    stores.run_store.save(_run("ask", RunStatus.WAITING, waiting=WaitState(reason=WaitReason.USER, wait_key="ask-1", prompt="Which folder?")))
    stores.run_store.save(_run("occ-1", RunStatus.WAITING, parent="auto-1",
                               vars0={"_meta": {"occurrence": {"automation_id": "auto-1", "occurrence_index": 1}}},
                               waiting=WaitState(reason=WaitReason.USER, wait_key="occ-ask", prompt="Approve?")))
    stores.records["auto-1"].append({"kind": "notify", "seq": 1, "title": "Daily digest", "body": "3 invoices",
                                     "channels": ["console", "email"], "index": 1, "run_id": "occ-1"})
    stores.ls.append(StepRecord(run_id="auto-1", step_id="s1", node_id="n", status=StepStatus.COMPLETED))

    out = _collect(plane, stores.svc)
    assert out.get("incremental") == 1
    assert out["queued"] == 4
    assert stores.rs.calls["list_runs"] == 0 and stores.rs.calls["list_run_index"] == 0 and stores.rs.calls["list_children"] == 0
    assert stores.rs.calls["load"] <= 6  # the changed runs and the controller, never the 200 others
    assert stores.ls.calls["list"] == 1  # only the ledger that grew
    assert _collect(plane, stores.svc) == {"queued": 0, "skipped": 1}


def test_a_changed_switch_or_an_unknown_change_set_reads_everything(gateway, imap, smtp, stores) -> None:
    _connect(gateway, imap, smtp)
    plane = plane_of("alice")
    _collect(plane, stores.svc)
    _collect(plane, stores.svc)
    r = gateway["client"].put("/api/gateway/me/email/notifications", headers=gateway["alice"], json={"job_failed": False, "approval_needed": True})
    assert r.status_code == 200, r.text
    before = stores.rs.calls["list_run_index"]
    out = _collect(plane, stores.svc)
    assert "skipped" not in out and "incremental" not in out and stores.rs.calls["list_run_index"] > before
    # Another process creating a run in the folder: the change set is unknown, so a full pass.
    assert _collect(plane, stores.svc) == {"queued": 0, "skipped": 1}
    JsonFileRunStore(stores.run_store._base).save(_run("foreign", RunStatus.COMPLETED))
    before = stores.rs.calls["list_run_index"]
    out = _collect(plane, stores.svc)
    assert "incremental" not in out and stores.rs.calls["list_run_index"] > before


def _runtime_before(version: str) -> bool:
    from importlib.metadata import version as _installed

    have = tuple(int(x) for x in _installed("abstractruntime").split(".")[:3] if x.isdigit())
    return have < tuple(int(x) for x in version.split("."))


# 2026-10-29 (backlog 0965): red on main with AbstractRuntime 0.15.2, whose index-build replay moves
# the run store's change token (a 0.15.2 regression), so the full pass reloads every finished run.
# Fixed in AbstractRuntime 0.15.3 (lpalbou/AbstractRuntime#21, released by #23); the gateway's floor
# moves to it in lpalbou/AbstractGateway#57. The test runs again by itself once 0.15.3 is installed.
@pytest.mark.skipif(_runtime_before("0.15.3"), reason="needs AbstractRuntime >= 0.15.3 (index-build change token "
                    "fix, AbstractRuntime#21; floor in AbstractGateway#57); backlog 0965, 2026-10-29")
def test_400_finished_runs_a_change_to_one_loads_exactly_one(gateway, imap, smtp, stores, monkeypatch) -> None:
    """The full pass (every 5 min, a foreign change, a restart) reads index rows, never the 400
    finished RunStates: a run is loaded only when its (status, updated_at) row is new."""
    from abstractgateway.mail import notifications

    _connect(gateway, imap, smtp)
    plane = plane_of("alice")
    asked = {"on": ["finished", "failed"], "channels": ["email"]}
    for i in range(400):
        status = RunStatus.COMPLETED if i % 2 else RunStatus.FAILED
        stores.run_store.save(_run(f"done-{i:03d}", status, notify=asked, updated_at=f"2020-01-01T00:{i // 60:02d}:{i % 60:02d}+00:00"))
    _collect(plane, stores.svc)  # baseline
    stores.rs.calls.update({k: 0 for k in stores.rs.calls})
    _collect(plane, stores.svc)  # full: 400 rows, all decided before the baseline, none loaded
    assert stores.rs.calls["load"] == 0

    def now() -> str:
        import datetime

        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    # One run re-finishes (in-process save): the incremental pass loads it alone.
    stores.rs.calls["load"] = 0
    stores.run_store.save(_run("done-007", RunStatus.COMPLETED, notify=asked, updated_at=now()))
    out = _collect(plane, stores.svc)
    assert out.get("incremental") == 1 and out["queued"] == 1
    assert stores.rs.calls["load"] == 1

    # Another process finishes a new one: unknown change set, a FULL pass, still exactly one load.
    stores.rs.calls["load"] = 0
    JsonFileRunStore(stores.run_store._base).save(_run("done-400", RunStatus.COMPLETED, notify=asked, updated_at=now()))
    out = _collect(plane, stores.svc)
    assert "incremental" not in out and out["queued"] == 1
    assert stores.rs.calls["load"] == 1

    # The 5-minute full pass with nothing new: no load at all.
    monkeypatch.setattr(notifications, "FULL_PASS_S", 0.0)
    stores.rs.calls["load"] = 0
    out = _collect(plane, stores.svc)
    assert "incremental" not in out and out["queued"] == 0 and stores.rs.calls["load"] == 0

    # A restart (process memo gone): the persisted cursor keeps it to the overlap window.
    monkeypatch.setattr(notifications, "_COLLECT_MEMO", {})
    stores.rs.calls["load"] = 0
    out = _collect(plane, stores.svc)
    assert out["queued"] == 0 and stores.rs.calls["load"] <= 2  # done-007/400, idempotent
    # Never a bulk read of full runs (list_runs loads every RunState it returns).
    assert stores.rs.calls["list_runs"] == 0


def test_benchmark_11500_quiet_runs_skip_under_5ms(gateway, imap, smtp, stores) -> None:
    _connect(gateway, imap, smtp)
    plane = plane_of("alice")
    for i in range(11_500):
        stores.run_store.save(_run(f"r{i:05d}", RunStatus.COMPLETED))
    _collect(plane, stores.svc)
    _collect(plane, stores.svc)
    t = time.perf_counter()
    for _ in range(20):
        assert _collect(plane, stores.svc).get("skipped") == 1
    per_pass = (time.perf_counter() - t) / 20
    timing_budget(per_pass, 0.005, "mail collector quiet pass")
