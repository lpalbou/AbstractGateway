"""A stop the gateway asks for interrupts a run; it never fails it (backlog 1103, ADR 0002 §5/§7).

Incident 2026-11-02: a polite gateway restart stopped the model host while an automation's model call
was in flight; the call failed with 424 ("the model host stopped while answering"), the run was marked
failed, the occurrence used a retry and an `automation_failed` email went out. Now:

- the gateway stopping the model host (its own stop, an operator Restart) or restarting it on purpose
  (a llama.cpp update: `kill(..., requested=True)`) raises AbstractRuntime's `EffectInterrupted` into
  the request in flight, and writes no incident;
- a child that dies ON ITS OWN (a crash, the memory ceiling, unresponsive) still fails the request with
  424 and an incident (the crash-loop guard);
- once a stop begins, no new workflow step starts (the step gate closes);
- the runner leaves an interrupted run RUNNING (no FAILED record) and it continues at the next start.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, List

import pytest

from abstractruntime import (
    Effect,
    EffectInterrupted,
    EffectType,
    InMemoryLedgerStore,
    InMemoryRunStore,
    Runtime,
    RunStatus,
    StepPlan,
    WorkflowSpec,
)
from abstractruntime.core.runtime import EffectOutcome

from abstractgateway import host_control, shutdown_drain
from abstractgateway import model_host as mh
from model_host_fakes import FakeOpenAIServer

posix_only = pytest.mark.skipif(os.name != "posix", reason="a real child process and signals")


@pytest.fixture()
def fake():
    with FakeOpenAIServer(chunks=4) as srv:
        yield srv


def _ctor(srv: FakeOpenAIServer) -> dict:
    return dict(provider="openai-compatible", model="fake-model",
                llm_kwargs={"base_url": srv.base_url, "api_key": "fake-key"})


def _sup(tmp_path: Path, **kw) -> mh.ModelHostSupervisor:
    kw.setdefault("nice", 0)
    kw.setdefault("backoff_s", (0.1,))
    return mh.ModelHostSupervisor(tmp_path / "data", **kw).start()


def _until(pred, timeout_s=20.0):
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _call_in_thread(client) -> dict:
    box: dict = {}

    def body() -> None:
        try:
            box["result"] = client.generate(prompt="victim")
        except BaseException as exc:  # noqa: BLE001 - EffectInterrupted is a BaseException
            box["error"] = exc

    t = threading.Thread(target=body, daemon=True)
    t.start()
    box["thread"] = t
    return box


@posix_only
def test_the_gateway_stopping_the_model_host_interrupts_the_call_in_flight(tmp_path, fake):
    sup = _sup(tmp_path)
    stopped = False
    try:
        client = mh.ModelHostClient(sup, kind="local", ctor=_ctor(fake))
        client.generate(prompt="warm")
        fake.delay_s = 1.0
        asked = len(fake.requests)
        box = _call_in_thread(client)
        assert _until(lambda: len(fake.requests) > asked)
        sup.stop()
        stopped = True
        box["thread"].join(10)
        assert isinstance(box.get("error"), EffectInterrupted), box
        assert mh.read_incidents(tmp_path / "data") == [], "a requested stop wrote an incident"
        # A new call while stopped is interrupted too (never a retryable 503 that burns attempts).
        with pytest.raises(EffectInterrupted):
            sup._acquire()
    finally:
        if not stopped:
            sup.stop()


@posix_only
def test_a_requested_restart_interrupts_writes_no_incident_and_the_next_call_waits_for_it(tmp_path, fake):
    sup = _sup(tmp_path)
    try:
        client = mh.ModelHostClient(sup, kind="local", ctor=_ctor(fake))
        client.generate(prompt="warm")
        pid = sup.pid
        fake.delay_s = 1.0
        asked = len(fake.requests)
        box = _call_in_thread(client)
        assert _until(lambda: len(fake.requests) > asked)
        sup.kill("llama.cpp updated: restarting the model host with the new llama.cpp", requested=True)
        box["thread"].join(10)
        assert isinstance(box.get("error"), EffectInterrupted), box
        # Sent again at once: it waits for the restarted child instead of a 503.
        fake.delay_s = 0.0
        assert "again" in client.generate(prompt="again")["content"]
        assert sup.pid not in (None, pid) and sup.generation == 2
        assert mh.read_incidents(tmp_path / "data") == [], "a requested restart wrote an incident"
        assert sup.snapshot()["state"] == "up"
    finally:
        sup.stop()


@posix_only
def test_a_child_killed_for_its_own_fault_still_fails_the_request_with_424_and_an_incident(tmp_path, fake):
    sup = _sup(tmp_path)
    try:
        client = mh.ModelHostClient(sup, kind="local", ctor=_ctor(fake))
        client.generate(prompt="warm")
        fake.delay_s = 1.0
        asked = len(fake.requests)
        box = _call_in_thread(client)
        assert _until(lambda: len(fake.requests) > asked)
        sup.kill("memory_ceiling")
        box["thread"].join(10)
        err = box.get("error")
        assert isinstance(err, mh.ModelHostCrashed) and err.status_code == 424, box
        kinds = [i["kind"] for i in mh.read_incidents(tmp_path / "data")]
        assert kinds and kinds[0] == "memory_ceiling", kinds
    finally:
        sup.stop()


def test_the_engines_update_restart_is_a_requested_one(monkeypatch):
    from abstractgateway import engines_install

    calls: List[Any] = []

    class _Sup:
        def kill(self, reason, **kw):
            calls.append((reason, kw))

    monkeypatch.setattr(mh, "supervisor", lambda: _Sup())
    engines_install._llama_takes_effect()
    assert calls and calls[0][1] == {"requested": True}, calls


@pytest.fixture()
def clean_host_state():
    shutdown_drain.reset()
    yield
    shutdown_drain.reset()


def _two_steps() -> WorkflowSpec:
    def first(run, ctx):
        return StepPlan(node_id="first", effect=Effect(type=EffectType.LLM_CALL, payload={"prompt": "1"}, result_key="a"),
                        next_node="second")

    def second(run, ctx):
        return StepPlan(node_id="second", effect=Effect(type=EffectType.TOOL_CALLS, payload={"tool_calls": []}, result_key="b"),
                        next_node="done")

    def done(run, ctx):
        return StepPlan(node_id="done", complete_output={"ok": True})

    return WorkflowSpec(workflow_id="two", entry_node="first", nodes={"first": first, "second": second, "done": done})


def test_once_a_stop_begins_no_new_step_starts(clean_host_state):
    steps: List[str] = []

    def llm(run, effect, default_next_node):
        steps.append("first")
        shutdown_drain.begin("SIGTERM")  # the stop begins while step 1 runs
        return EffectOutcome.completed({"content": "one"})

    def tools(run, effect, default_next_node):
        steps.append("second")
        return EffectOutcome.completed({"results": []})

    rt = Runtime(run_store=InMemoryRunStore(), ledger_store=InMemoryLedgerStore(),
                 effect_handlers={EffectType.LLM_CALL: llm, EffectType.TOOL_CALLS: tools})
    wf = _two_steps()
    run_id = rt.start(workflow=wf, vars={})
    state = rt.tick(workflow=wf, run_id=run_id, step_gate=host_control.step_gate)
    assert steps == ["first"], "a step started after the stop began"
    assert state.status == RunStatus.RUNNING and state.current_node == "second"


def test_the_step_gate_closes_on_a_requested_restart_or_shutdown(clean_host_state, monkeypatch):
    assert host_control.step_gate() is True
    monkeypatch.setattr(host_control, "_restart_requested", True)
    assert host_control.step_gate() is False
    monkeypatch.setattr(host_control, "_restart_requested", False)
    monkeypatch.setattr(host_control, "_shutdown_requested", True)
    assert host_control.step_gate() is False


def test_the_runner_leaves_an_interrupted_run_running_with_no_failed_record():
    """`GatewayRunner._tick_run` turns any other tick exception into FAILED; an interruption is not one."""
    from abstractgateway.runner import GatewayRunner

    run_store, ledger = InMemoryRunStore(), InMemoryLedgerStore()

    def llm(run, effect, default_next_node):
        raise EffectInterrupted()

    rt = Runtime(run_store=run_store, ledger_store=ledger, effect_handlers={EffectType.LLM_CALL: llm})
    wf = _two_steps()
    run_id = rt.start(workflow=wf, vars={})

    runner = GatewayRunner.__new__(GatewayRunner)  # only what `_tick_run` touches
    runner._stop = threading.Event()
    runner._cfg = type("Cfg", (), {"tick_max_steps": 10})()
    runner._host = type("Host", (), {"runtime_and_workflow_for_run": staticmethod(lambda rid: (rt, wf)),
                                     "ledger_store": ledger})()
    runner._clear_resolution_failure = lambda rid: None
    runner._stream_run = lambda state: (_ for _ in ()).throw(AssertionError("an interrupted tick went on"))
    runner._tick_run(run_id)

    state = run_store.load(run_id)
    assert state.status == RunStatus.RUNNING and not state.error
    assert [r["status"] for r in ledger.list(run_id)] == ["started"]
