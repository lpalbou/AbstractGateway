"""The gateway runner brings a dead automation back (operator ruling 2026-11-28, R3).

Two moments, both in `runner.py`, both calling `abstractruntime.automations.commands`:
- (b) after each tick: a controller the tick left `failed` (its tick raised and the handler
  marked it failed, or an effect failed without raising: the 2026-11-07 shape) is armed again
  for its next scheduled time by `revive_dead_controller` (no immediate attempt);
- (a) the runner's start (each time it begins ticking a plane): `recover_stopped_automations`
  brings back every controller that ENDED failed (here: one whose dispatch effect failed, the
  shape of the records that died on 2026-11-07).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from automations_fixtures import ECHO_FLOW_ID, HEADERS, gateway_env, wait_until, write_echo_bundle


@pytest.fixture()
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway_env(monkeypatch, tmp_path, runner=True)
    ref = write_echo_bundle(tmp_path / "bundles")
    from abstractgateway.app import app

    with TestClient(app) as c:
        c.bundle_ref = ref  # type: ignore[attr-defined]
        yield c


def _create(c: TestClient, rid: str, flow: str = ECHO_FLOW_ID) -> str:
    r = c.post("/api/gateway/automations", headers=HEADERS, json={
        "request_id": rid, "title": rid, "context": {"mode": "independent"},
        "target": {"bundle_ref": c.bundle_ref, "flow_id": flow, "input_data": {"prompt": "hello"}},  # type: ignore[attr-defined]
        "trigger": {"source_id": "schedule", "source_version": 1, "config": {"every": "1h"}},
    })
    assert r.status_code == 200, r.text
    return r.json()["automation_id"]


def _summary(c: TestClient, aid: str) -> dict:
    r = c.get(f"/api/gateway/automations/{aid}", headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()["summary"]


def _records(aid: str, name: str) -> list:
    from abstractruntime.automations.ledger import automation_records

    from abstractgateway.service import get_gateway_service

    return [r["payload"] for r in automation_records(get_gateway_service().host.ledger_store, aid, name)]


def test_a_controller_tick_that_raises_is_armed_again_for_its_next_time(live: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractruntime.automations import controller as controller_mod
    from abstractruntime.core.models import RunStatus

    from abstractgateway.service import get_gateway_service

    attempts = []

    def dying_admit(turn):  # a broken controller invariant (a kept raise site), every time
        attempts.append(turn.now)
        raise controller_mod.ControllerSeamError("trigger wait kind 'weird' is not supported by the v1 controller")

    monkeypatch.setattr(controller_mod, "admit", dying_admit)
    aid = _create(live, "dies-every-time")
    run = wait_until(lambda: (lambda r: r if r is not None and r.status == RunStatus.WAITING and _records(aid, "automation.recovered") else None)(
        get_gateway_service().host.run_store.load(aid)), timeout_s=20)
    assert len(attempts) == 1  # one attempt at its scheduled time, none after the revival
    assert run.waiting.until and run.waiting.until > attempts[0]  # armed for the NEXT scheduled time
    s = _summary(live, aid)
    assert (s["status"], s["section"]) == ("active", "active") and "run_now" in s["capabilities"]
    assert s["next_fire_at"] == run.waiting.until


def test_the_runner_start_brings_back_a_controller_that_ended_failed(live: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractruntime.automations import controller as controller_mod
    from abstractruntime.scheduler.registry import WorkflowRegistry

    from abstractgateway.service import get_gateway_service

    hidden = f"{live.bundle_ref}:{ECHO_FLOW_ID}"  # type: ignore[attr-defined]
    original_get = WorkflowRegistry.get
    dispatch = controller_mod.dispatch

    def dispatch_before_the_fix(turn):  # the 2026-11-07 shape: the dispatch effect was not absorbed
        effect = dispatch(turn)
        effect.payload.pop("_absorb_failure")
        return effect

    from abstractruntime.automations import commands as commands_mod

    with monkeypatch.context() as m:
        m.setattr(WorkflowRegistry, "get", lambda self, wid: None if wid == hidden else original_get(self, wid))
        m.setattr(controller_mod, "dispatch", dispatch_before_the_fix)
        # A record that died before this rule (the after-tick revival did not exist then).
        m.setattr(commands_mod, "revive_dead_controller", lambda *a, **k: False)
        aid = _create(live, "died-on-11-07")
        wait_until(lambda: _summary(live, aid)["status"] == "failed", timeout_s=20)
    runner = get_gateway_service().runner
    runner.stop()
    runner.start()  # the runner starts ticking this plane again: (a)
    back = wait_until(lambda: (lambda s: s if s["status"] == "active" else None)(_summary(live, aid)), timeout_s=20)
    assert back["section"] == "active" and back.get("next_fire_at") and "failed_reason" not in back
    assert back["last_occurrence"]["index"] == 1 and back["last_occurrence"]["status"] == "failed"
    occ = live.get(f"/api/gateway/automations/{aid}/occurrences", headers=HEADERS).json()["items"]
    assert [(o["index"], o["status"]) for o in occ] == [(1, "failed")]  # the dead run shown failed; none started
    assert len(_records(aid, "automation.recovered")) == 1


def test_a_controller_whose_dispatch_effect_fails_without_raising_is_armed_again(live: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The 2026-11-07 shape, live: the dispatch effect is not absorbed and fails (its workflow is
    not installed): the runtime ends the controller `failed`, no exception. The runner's after-tick
    check brings it back at once, parked for its next scheduled time: one attempt."""
    from abstractruntime.automations import controller as controller_mod
    from abstractruntime.core.models import RunStatus
    from abstractruntime.scheduler.registry import WorkflowRegistry

    from abstractgateway.service import get_gateway_service

    hidden = f"{live.bundle_ref}:{ECHO_FLOW_ID}"  # type: ignore[attr-defined]
    original_get = WorkflowRegistry.get
    dispatch = controller_mod.dispatch
    attempts = []

    def dispatch_before_the_fix(turn):
        effect = dispatch(turn)
        effect.payload.pop("_absorb_failure")
        attempts.append(turn.now)
        return effect

    monkeypatch.setattr(WorkflowRegistry, "get", lambda self, wid: None if wid == hidden else original_get(self, wid))
    monkeypatch.setattr(controller_mod, "dispatch", dispatch_before_the_fix)
    aid = _create(live, "effect-fails")
    run = wait_until(lambda: (lambda r: r if r is not None and r.status == RunStatus.WAITING and _records(aid, "automation.recovered") else None)(
        get_gateway_service().host.run_store.load(aid)), timeout_s=20)
    assert len(attempts) == 1 and run.waiting.until > attempts[0]
    s = _summary(live, aid)
    assert (s["status"], s["section"]) == ("active", "active") and s["next_fire_at"] == run.waiting.until
    occ = live.get(f"/api/gateway/automations/{aid}/occurrences", headers=HEADERS).json()["items"]
    assert [(o["index"], o["status"]) for o in occ] == [(1, "failed")]
    assert len(attempts) == 1  # nothing more since
