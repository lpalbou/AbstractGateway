"""flow c5260 P1-A: durable events must APPEND during a pause window.

Live repro (run e6cb3e66): a durable emit whose wait_key matched a PAUSED
run hit `Runtime.resume` unguarded — its `ValueError("Run is paused")`
aborted the WHOLE emit before `_deliver_durable_event` ever ran, silently
dropping the event for EVERY mailbox. A `stop` steered during an operator
pause vanished. `durable: true` exists precisely for windows when the run
cannot receive: the fix skips paused runs in the resume pass (wake stays
refused by the skip, never by an exception) and guards each resume so one
refusing listener cannot abort delivery to the runs behind it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pytest

from abstractruntime.core.event_keys import build_event_wait_key
from abstractruntime.core.models import RunStatus, WaitReason

from abstractgateway.runner import GatewayRunner, GatewayRunnerConfig

MAILBOX = "wh"
WAIT_KEY = build_event_wait_key(scope="global", name=MAILBOX, session_id=None, workflow_id=None, run_id=None)


@dataclass
class _Wait:
    reason: Any = WaitReason.EVENT
    wait_key: str = WAIT_KEY
    resume_to_node: str = "park"
    details: Optional[Dict[str, Any]] = None


@dataclass
class _Run:
    run_id: str
    actor_id: str = "gateway"
    status: Any = RunStatus.WAITING
    waiting: Any = field(default_factory=_Wait)
    created_at: str = "2020-01-01T00:00:00+00:00"
    vars: Dict[str, Any] = field(default_factory=dict)


class _Store:
    def __init__(self, runs: List[_Run]) -> None:
        self._by_id = {r.run_id: r for r in runs}
        self._runs = runs
        self.saved: List[str] = []

    def list_runs(self, *, status=None, wait_reason=None, workflow_id=None, limit=100):
        out = [r for r in self._runs if status is None or r.status == status]
        if wait_reason is not None:
            out = [r for r in out if getattr(r.waiting, "reason", None) == wait_reason]
        return out[: int(limit)]

    def list_due_wait_until(self, *, now_iso=None, limit=100):
        return []

    def load(self, run_id: str):
        return self._by_id.get(str(run_id))

    def save(self, run) -> None:
        self.saved.append(str(run.run_id))


class _RefusingRuntime:
    """Mirrors Runtime.resume's paused refusal — the pin fails if the emit
    path ever reaches resume for a paused run (or lets the raise escape)."""

    def __init__(self) -> None:
        self.resumed: List[str] = []

    def resume(self, *, workflow=None, run_id: str, wait_key=None, payload=None, max_steps: int = 0):
        run_id = str(run_id)
        if run_id.startswith("paused"):
            raise ValueError("Run is paused")
        self.resumed.append(run_id)


class _Host:
    def __init__(self, runtime: Any, store: Any) -> None:
        self.runtime = runtime
        self.run_store = store

    def runtime_and_workflow_for_run(self, run_id: str):
        return self.runtime, object()


def _paused_vars() -> Dict[str, Any]:
    return {
        "_runtime": {"control": {"paused": True, "paused_at": "2026-07-24T19:11:12+00:00"}},
        "events_mailbox": MAILBOX,
        "events_inbox": [],
    }


def _runner(store: _Store, runtime: Any, tmp_path) -> GatewayRunner:
    return GatewayRunner(
        base_dir=tmp_path,
        host=_Host(runtime, store),
        config=GatewayRunnerConfig(),
        enable=False,
    )


@pytest.mark.basic
def test_durable_emit_appends_to_paused_run_without_waking_it(tmp_path) -> None:
    paused = _Run(run_id="paused-1", vars=_paused_vars())
    store = _Store([paused])
    runtime = _RefusingRuntime()
    runner = _runner(store, runtime, tmp_path)

    out = runner._apply_emit_event(
        {"name": MAILBOX, "scope": "global", "durable": True, "event_id": "wh-h1-e5", "payload": {"kind": "grant"}},
        default_session_id="s",
        client_id="test",
    )

    # Wake stays refused; the append landed; nothing raised out of the emit.
    assert runtime.resumed == [], "a paused run must never be woken by an event"
    assert out["resumed"] == 0
    assert out["appended"] == 1, "the durable append must reach the paused run's inbox"
    inbox = paused.vars["events_inbox"]
    assert len(inbox) == 1 and inbox[0]["event_id"] == "wh-h1-e5"
    assert "paused-1" in store.saved


@pytest.mark.basic
def test_one_refusing_listener_does_not_abort_delivery_to_the_rest(tmp_path) -> None:
    """The raise-isolation half: a paused run racing in BEHIND the skip (or
    any refusing listener) must not abort resumes for other runs or the
    durable append after the loop."""
    paused = _Run(run_id="paused-2", vars=_paused_vars())
    healthy = _Run(run_id="healthy-1", vars={"events_mailbox": MAILBOX, "events_inbox": []})
    store = _Store([paused, healthy])
    runtime = _RefusingRuntime()
    runner = _runner(store, runtime, tmp_path)

    out = runner._apply_emit_event(
        {"name": MAILBOX, "scope": "global", "durable": True, "event_id": "e-2", "payload": {}},
        default_session_id="s",
        client_id="test",
    )

    assert runtime.resumed == ["healthy-1"], "the healthy listener behind the paused one must still wake"
    assert out["resumed"] == 1
    assert out["appended"] == 2, "both mailboxes get the durable append"
    assert len(paused.vars["events_inbox"]) == 1
    assert len(healthy.vars["events_inbox"]) == 1
