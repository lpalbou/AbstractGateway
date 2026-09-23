"""Stop kill switch pins (stop_kill_switch.py; incident 2026-09-22; operator rule 2026-09-23).

A cancelled run's model call kept decoding for an hour because nothing passed
the cancel to it. The soft path (runtime `effect_cancellation` → core
`cancel_event`) now stops it within a token; the kill switch is the backstop
when it does not — and it kills THE INFERENCE ONLY, in process, never the
gateway. These pin:

* the switch fires AT the configured deadline — not before, exactly once;
* firing kills the stuck call's thread in process and is attributed: an ERROR
  log line naming run/step/provider/model/elapsed/killed_by, an
  `abstract.status` record with the UI text and `killed_by: kill_switch`,
  every run of the tree CANCELLED with that reason;
* END TO END with the real Runtime: a decode loop that ignores its cancel
  event (and swallows `Exception`) is unwound within the grace period, the
  step is recorded `cancelled` with `killed_by: kill_switch`, another thread
  is untouched, and the SAME process runs the next workflow;
* a thread blocked inside ONE native call is reported as not interruptible
  (never papered over) and still ends cancelled when the call returns;
* it NEVER fires when the soft stop worked, never for a tool, a disabled
  switch says so, and the module contains no process-exit path at all;
* the runner arms it on every applied cancel, and that cancel reaches the
  executing call; the deadline is operator config (runtime config > env >
  default, invalid values surfaced).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from abstractruntime import (
    Effect,
    EffectType,
    InMemoryLedgerStore,
    InMemoryRunStore,
    Runtime,
    RunStatus,
    StepPlan,
    WorkflowSpec,
)
from abstractruntime.core.effect_cancellation import InflightEffect, effect_inflight_scope
from abstractruntime.core.runtime import EffectOutcome

import abstractgateway.stop_kill_switch as ks
from abstractgateway.stop_kill_switch import StopKillSwitch, forced_stop_text


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def _stores_with_tree():
    run_store, ledger = InMemoryRunStore(), InMemoryLedgerStore()
    rt = Runtime(run_store=run_store, ledger_store=ledger)
    wf = WorkflowSpec(workflow_id="w", entry_node="n", nodes={"n": lambda run, ctx: StepPlan(node_id="n", complete_output={})})
    root = rt.start(workflow=wf, vars={})
    child = rt.start(workflow=wf, vars={}, parent_run_id=root)
    for rid in (root, child):
        rt.cancel_run(rid, reason="Cancelled", cancelled_by="command")
    return run_store, ledger, root, child


def _stuck_llm(run_id: str, **extra: Any) -> Dict[str, Any]:
    out = {
        "run_id": run_id, "node_id": "reason", "step_id": "step-123", "effect_type": "llm_call",
        "attempt": 1, "provider": "mlx", "model": "Jundot/Qwen3.8-Flash-Next-oQ4e-mtp", "elapsed_s": 3712.4,
        "since_cancel_s": 10.0, "cancel_requested": True, "cancelled_by": "command", "thread": "tick-7",
    }
    out.update(extra)
    return out


def _switch(run_store, ledger, *, inflight, clock, kill, deadline=10.0):
    return StopKillSwitch(
        run_store=run_store, ledger_store=ledger,
        settings=lambda: {"deadline_s": deadline, "source": "stored"},
        kill=kill, inflight=inflight, clock=clock, sleep=clock.sleep, watch=False,
    )


def _status_payloads(ledger, rid):
    out = []
    for r in ledger.list(rid):
        payload = ((r.get("effect") or {}).get("payload") or {})
        if payload.get("name") == "abstract.status" and (payload.get("payload") or {}).get("killed_by"):
            out.append(payload["payload"])
    return out


def test_fires_at_the_deadline_kills_the_inference_and_attributes_it(caplog):
    run_store, ledger, root, child = _stores_with_tree()
    clock = _Clock()
    live = {"stuck": [_stuck_llm(child)]}
    kills: List[Dict[str, Any]] = []

    def kill(step_id, *, killed_by, reason):
        kills.append({"step_id": step_id, "killed_by": killed_by, "reason": reason})
        live["stuck"] = []  # the injected EffectKilled unwound the call
        return {"injected": True, "step_id": step_id}

    switch = _switch(run_store, ledger, inflight=lambda ids: list(live["stuck"]), clock=clock, kill=kill)
    incident = switch.arm(root_run_id=root, run_ids=[root, child])

    clock.t += 9.9
    assert switch.check(incident) == "waiting"
    assert kills == []

    clock.t += 0.1001
    with caplog.at_level(logging.ERROR, logger="abstractgateway.stop_kill_switch"):
        assert switch.check(incident) == "fired"
    assert kills == [{"step_id": "step-123", "killed_by": "kill_switch", "reason": forced_stop_text(10.0)}]

    line = next(r.getMessage() for r in caplog.records if "KILL SWITCH FIRED" in r.getMessage())
    for needle in (f"run={child}", f"root={root}", "step=step-123", "provider=mlx",
                   "model=Jundot/Qwen3.8-Flash-Next-oQ4e-mtp", "killed_by=kill_switch", "deadline=10s",
                   "action=kill_inference"):
        assert needle in line, needle
    assert any("inference killed in process" in r.getMessage() for r in caplog.records)

    for rid in (root, child):
        payloads = _status_payloads(ledger, rid)
        assert payloads, f"no forced-stop status record on run {rid}"
        p = payloads[-1]
        assert p["text"] == "Stop forced at 10 s: inference killed"
        assert p["interrupted"] is True and p["killed_steps"] == ["step-123"] and p["stuck_steps"] == []
        assert p["provider"] == "mlx" and p["action"] == "kill_inference"
        run = run_store.load(rid)
        assert run.status == RunStatus.CANCELLED and "kill_switch" in run.error

    clock.t += 5
    switch.check(incident)
    assert len(kills) == 1 and len(switch.fired) == 1, "fired twice"
    assert switch.pending() == []


def test_a_call_blocked_in_a_native_op_is_reported_never_papered_over(caplog):
    run_store, ledger, root, child = _stores_with_tree()
    clock = _Clock()
    stuck = [_stuck_llm(child)]
    switch = _switch(run_store, ledger, inflight=lambda ids: list(stuck), clock=clock,
                     kill=lambda step_id, **kw: {"injected": True, "step_id": step_id})
    incident = switch.arm(root_run_id=root, run_ids=[root, child])
    clock.t += 10.5
    with caplog.at_level(logging.ERROR, logger="abstractgateway.stop_kill_switch"):
        assert switch.check(incident) == "fired"
    msg = next(r.getMessage() for r in caplog.records if "could NOT be interrupted" in r.getMessage())
    assert "thread=tick-7" in msg and "NOT restarted" in msg
    p = _status_payloads(ledger, root)[-1]
    assert p["interrupted"] is False and p["stuck_steps"] == ["step-123"] and p["stuck_threads"] == ["tick-7"]
    assert p["text"] == forced_stop_text(10.0, interrupted=False)


def test_never_fires_when_the_soft_stop_succeeded():
    run_store, ledger, root, child = _stores_with_tree()
    clock = _Clock()
    stuck = [_stuck_llm(child)]
    kills: List[Any] = []
    switch = _switch(run_store, ledger, inflight=lambda ids: list(stuck), clock=clock,
                     kill=lambda *a, **k: kills.append(a) or {"injected": True})
    incident = switch.arm(root_run_id=root, run_ids=[root, child])
    clock.t += 0.4
    assert switch.check(incident) == "waiting"
    stuck.clear()  # the provider honoured the cancel event
    clock.t += 0.1
    assert switch.check(incident) == "stopped"
    clock.t += 60
    assert switch.pending() == [] and kills == []
    assert _status_payloads(ledger, root) == []


def test_a_tool_still_running_is_named_but_never_escalated(caplog):
    run_store, ledger, root, child = _stores_with_tree()
    clock = _Clock()
    tool = [_stuck_llm(child, effect_type="tool_calls", step_id="tool-1")]
    kills: List[Any] = []
    switch = _switch(run_store, ledger, inflight=lambda ids: list(tool), clock=clock,
                     kill=lambda *a, **k: kills.append(a) or {})
    incident = switch.arm(root_run_id=root, run_ids=[root, child])
    clock.t += 30
    with caplog.at_level(logging.INFO, logger="abstractgateway.stop_kill_switch"):
        assert switch.check(incident) == "stopped"
    assert kills == []
    assert any("tool_calls@" in r.getMessage() and "never escalated" in r.getMessage() for r in caplog.records)


def test_a_disabled_switch_is_logged_at_error_not_silent(caplog):
    run_store, ledger, root, child = _stores_with_tree()
    switch = _switch(run_store, ledger, inflight=lambda ids: [_stuck_llm(child)], clock=_Clock(),
                     kill=lambda *a, **k: {}, deadline=0)
    with caplog.at_level(logging.ERROR, logger="abstractgateway.stop_kill_switch"):
        assert switch.arm(root_run_id=root, run_ids=[root]) is None
    assert any("DISABLED" in r.getMessage() for r in caplog.records)


def test_the_module_has_no_process_exit_path():
    """Operator rule 2026-09-23: kill the inference, never the gateway."""
    source = Path(ks.__file__).read_text()
    for forbidden in (r"os\._exit\(", r"\bexecv", r"sys\.exit\(", r"os\.kill\(", r"relaunch_process", r"SIGTERM"):
        assert not re.search(forbidden, source), forbidden


# --------------------------------------------------------------------------
# End to end: the real Runtime, a decode loop that ignores its cancel event
# --------------------------------------------------------------------------


def _decoder_workflow() -> WorkflowSpec:
    def reason(run, ctx):
        return StepPlan(node_id="reason", effect=Effect(type=EffectType.LLM_CALL, payload={"prompt": "p"},
                                                        result_key="llm"), next_node="done")

    def done(run, ctx):
        return StepPlan(node_id="done", complete_output={"llm": run.vars.get("llm")})

    return WorkflowSpec(workflow_id="decoder", entry_node="reason", nodes={"reason": reason, "done": done})


class _Deaf:
    """An LLM handler whose 'decode loop' ignores the cancel event and even
    swallows `Exception` — only a BaseException injection can stop it."""

    def __init__(self, native_block_s: float = 0.0) -> None:
        self.native_block_s = native_block_s
        self.started = threading.Event()
        self.tokens = 0

    def __call__(self, run, effect, default_next_node):
        if run.vars.get("quick"):
            return EffectOutcome.completed({"content": "hi"})
        self.started.set()
        if self.native_block_s:
            time.sleep(self.native_block_s)  # ONE blocking native call
        t0 = time.monotonic()
        while time.monotonic() - t0 < 30:
            try:
                self.tokens += 1
                sum(range(200))  # a "token" of pure-Python work
            except Exception:  # noqa: BLE001 - provider-style catch-all
                pass
        return EffectOutcome.completed({"content": "decoded to the end"})


def _cancel_and_kill(decoder, *, grace_s=2.0, deadline_s=0.3):
    run_store, ledger = InMemoryRunStore(), InMemoryLedgerStore()
    runtime = Runtime(run_store=run_store, ledger_store=ledger, effect_handlers={EffectType.LLM_CALL: decoder})
    wf = _decoder_workflow()
    run_id = runtime.start(workflow=wf, vars={})
    box: Dict[str, Any] = {}
    thread = threading.Thread(target=lambda: box.setdefault("state", runtime.tick(workflow=wf, run_id=run_id)),
                              daemon=True)
    thread.start()
    assert decoder.started.wait(5)
    Runtime(run_store=run_store, ledger_store=ledger).cancel_run(run_id, cancelled_by="command")
    switch = StopKillSwitch(run_store=run_store, ledger_store=ledger,
                            settings=lambda: {"deadline_s": deadline_s, "source": "test"},
                            grace_s=grace_s, interval_s=0.02)
    t0 = time.monotonic()
    switch.arm(root_run_id=run_id, run_ids=[run_id])
    return runtime, wf, run_store, ledger, run_id, thread, box, switch, t0


def test_end_to_end_the_deaf_decode_is_killed_in_process_and_the_process_keeps_working():
    # Another thread's work must be untouched by the kill.
    neighbour_stop = threading.Event()
    neighbour_ticks = [0]

    def neighbour():
        while not neighbour_stop.is_set():
            neighbour_ticks[0] += 1
            time.sleep(0.001)

    n_thread = threading.Thread(target=neighbour, daemon=True)
    n_thread.start()

    decoder = _Deaf()
    runtime, wf, run_store, ledger, run_id, thread, box, switch, t0 = _cancel_and_kill(decoder)
    thread.join(5)
    assert not thread.is_alive(), "the deaf decode was not killed"
    took = time.monotonic() - t0
    assert took < 2.0, f"kill took {took:.2f}s"
    assert box["state"].status == RunStatus.CANCELLED

    llm = [r for r in ledger.list(run_id) if (r.get("effect") or {}).get("type") == "llm_call"]
    assert [r["status"] for r in llm] == ["started", "cancelled"]
    res = llm[-1]["result"]
    assert res["killed_by"] == "kill_switch" and res["cancelled_by"] == "command"
    assert res["stopped_after_kill_s"] < 1.0
    deadline = time.monotonic() + 3
    while not switch.fired and time.monotonic() < deadline:
        time.sleep(0.02)
    assert switch.fired[-1]["interrupted"] is True and switch.fired[-1]["stuck_steps"] == []

    before = neighbour_ticks[0]
    time.sleep(0.05)
    assert neighbour_ticks[0] > before and n_thread.is_alive(), "the kill touched another thread"
    neighbour_stop.set()

    # The SAME process (and the same Runtime) runs the next workflow normally.
    nxt = runtime.start(workflow=wf, vars={"quick": True})
    state = runtime.tick(workflow=wf, run_id=nxt)
    assert state.status == RunStatus.COMPLETED and state.output["llm"]["content"] == "hi"


def test_end_to_end_a_thread_blocked_in_a_native_call_is_reported_then_ends_cancelled():
    decoder = _Deaf(native_block_s=1.5)
    runtime, wf, run_store, ledger, run_id, thread, box, switch, t0 = _cancel_and_kill(decoder, grace_s=0.3)
    deadline = time.monotonic() + 5
    while not switch.fired and time.monotonic() < deadline:
        time.sleep(0.02)
    assert switch.fired and switch.fired[-1]["interrupted"] is False, "a blocked native call was claimed killed"
    thread.join(5)
    assert not thread.is_alive(), "the pending kill never fired when the native call returned"
    assert decoder.tokens == 0, "the loop after the native call ran: the pending kill was lost"
    llm = [r for r in ledger.list(run_id) if (r.get("effect") or {}).get("type") == "llm_call"]
    assert llm[-1]["status"] == "cancelled" and llm[-1]["result"]["killed_by"] == "kill_switch"


# --------------------------------------------------------------------------
# Config: runtime config > env > default, invalid values surfaced
# --------------------------------------------------------------------------


def test_deadline_resolution_stored_over_env_over_default(tmp_path, monkeypatch):
    from abstractgateway.runtime_config import (
        RuntimeConfigError,
        read_runtime_config,
        resolve_stop_kill_switch,
        write_runtime_config,
    )

    monkeypatch.delenv("ABSTRACTGATEWAY_STOP_KILL_SWITCH_S", raising=False)
    got = resolve_stop_kill_switch(tmp_path)
    assert (got["deadline_s"], got["source"]) == (10.0, "default")

    monkeypatch.setenv("ABSTRACTGATEWAY_STOP_KILL_SWITCH_S", "4.5")
    assert resolve_stop_kill_switch(tmp_path) == {"deadline_s": 4.5, "source": "env"}

    write_runtime_config(tmp_path, {"stop_kill_switch_s": 12}, actor="t")
    assert resolve_stop_kill_switch(tmp_path) == {"deadline_s": 12.0, "source": "stored"}
    assert read_runtime_config(tmp_path)["stop_kill_switch_s"] == {"value": 12.0, "source": "stored"}

    with pytest.raises(RuntimeConfigError):
        write_runtime_config(tmp_path, {"stop_kill_switch_s": -1}, actor="t")

    write_runtime_config(tmp_path, {"stop_kill_switch_s": None}, actor="t")
    monkeypatch.setenv("ABSTRACTGATEWAY_STOP_KILL_SWITCH_S", "soon")
    got = resolve_stop_kill_switch(tmp_path)
    assert got["deadline_s"] == 10.0 and "soon" in got["invalid_env"], "an invalid env value was replaced silently"


# --------------------------------------------------------------------------
# Runner wiring
# --------------------------------------------------------------------------


class _Host:
    def __init__(self, run_store, ledger) -> None:
        self.run_store = run_store
        self.ledger_store = ledger
        self.artifact_store = None


def test_an_applied_cancel_command_stops_the_executing_effect_and_arms_the_switch(tmp_path, monkeypatch):
    from abstractruntime.storage.commands import CommandRecord

    from abstractgateway.runner import GatewayRunner

    monkeypatch.setenv("ABSTRACTGATEWAY_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ABSTRACTGATEWAY_STOP_KILL_SWITCH_S", "10")
    run_store, ledger = InMemoryRunStore(), InMemoryLedgerStore()
    rt = Runtime(run_store=run_store, ledger_store=ledger)
    wf = WorkflowSpec(workflow_id="w", entry_node="n", nodes={"n": lambda run, ctx: StepPlan(node_id="n", complete_output={})})
    root = rt.start(workflow=wf, vars={})
    child = rt.start(workflow=wf, vars={}, parent_run_id=root)

    runner = GatewayRunner(base_dir=tmp_path, host=_Host(run_store, ledger), enable=False)
    runner._kill_switch._kill = lambda *a, **k: pytest.fail("the kill switch fired in a soft-stop test")
    armed: List[Dict[str, Any]] = []
    real_arm = runner._kill_switch.arm
    runner._kill_switch.arm = lambda **kw: armed.append(kw) or real_arm(**kw)

    entry = InflightEffect(run_id=child, parent_run_id=root, node_id="reason", step_id="s1", effect_type="llm_call")
    started, seen = threading.Event(), threading.Event()

    def decoding():
        with effect_inflight_scope(entry):
            started.set()
            if entry.cancel_event.wait(5):
                seen.set()

    worker = threading.Thread(target=decoding, daemon=True)
    worker.start()
    assert started.wait(5)
    t0 = time.monotonic()
    runner._apply_command(CommandRecord(command_id="c1", run_id=root, type="cancel", payload={}, ts="now"))
    assert seen.wait(1.0), "the cancel command did not reach the executing model call"
    assert time.monotonic() - t0 < 1.0
    assert entry.cancelled_by == "command"
    assert armed and armed[0]["root_run_id"] == root and set(armed[0]["run_ids"]) == {root, child}
    worker.join(2)
    deadline = time.monotonic() + 3
    while runner._kill_switch.pending() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert runner._kill_switch.pending() == [] and runner._kill_switch.fired == []
    runner._kill_switch.stop()
    assert run_store.load(root).status == RunStatus.CANCELLED
    assert run_store.load(child).status == RunStatus.CANCELLED
