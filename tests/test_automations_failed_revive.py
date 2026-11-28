"""Failed automations through the gateway (automations wave 2026-11-27, spec A + D).

A `failed` automation (its controller died, as the three that died on 2026-11-07 when the
workflow version they pinned was not in the registry) is served in the `failed` section, counted by
`failed_automations_count`, and its `capabilities` list exactly what the door lets through:
edit, resume, archive. Edit then resume brings it back, the run it died on closed as failed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from abstractgateway.routes.automations import _capabilities
from automations_fixtures import ECHO_FLOW_ID, HEADERS, gateway_env, wait_until, write_echo_bundle


def test_capabilities_of_stopped_automations() -> None:
    assert _capabilities("failed") == ["revise", "resume", "archive", "discuss"]
    assert _capabilities("failed", resumable=False) == ["revise", "archive", "discuss"]
    assert _capabilities("completed") == ["archive", "discuss"]
    assert _capabilities("archived") == ["unarchive", "discuss"]


@pytest.fixture()
def live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    gateway_env(monkeypatch, tmp_path, runner=True)
    ref = write_echo_bundle(tmp_path / "bundles")
    from abstractgateway.app import app

    with TestClient(app) as c:
        c.bundle_ref = ref  # type: ignore[attr-defined]
        yield c


def _summary(c: TestClient, aid: str) -> dict:
    r = c.get(f"/api/gateway/automations/{aid}", headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()["summary"]


def _post(c: TestClient, aid: str, typ: str, cid: str):
    return c.post(f"/api/gateway/automations/{aid}/commands", headers=HEADERS, json={"command_id": cid, "type": typ, "payload": {}})


def test_a_failed_automation_is_grouped_edited_and_resumed(live: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractruntime.automations import controller as controller_mod
    from abstractruntime.scheduler.registry import WorkflowRegistry

    dispatch = controller_mod.dispatch

    def dispatch_before_the_fix(turn):
        effect = dispatch(turn)
        effect.payload.pop("_absorb_failure")  # fails loudly if the fix's opt-in is gone
        return effect

    hidden = f"{live.bundle_ref}:slow"  # type: ignore[attr-defined]
    original_get = WorkflowRegistry.get
    # The workflow version the automation pins is uninstalled, and the dispatch fails the way it
    # did before the fix: the controller itself dies, as on 2026-11-07.
    monkeypatch.setattr(WorkflowRegistry, "get", lambda self, wid: None if wid == hidden else original_get(self, wid))
    with monkeypatch.context() as m:
        m.setattr(controller_mod, "dispatch", dispatch_before_the_fix)
        # A record that died before 2026-11-28 (no after-tick revival then), restarted by a person
        # before the host brings it back (that path: test_automation_runner_brings_back.py).
        from abstractruntime.automations import commands as commands_mod

        monkeypatch.setattr(commands_mod, "revive_dead_controller", lambda *a, **k: False)
        body = {
            "request_id": "dies",
            "title": "Dies at dispatch",
            "target": {"bundle_ref": live.bundle_ref, "flow_id": "slow", "input_data": {"prompt": "hello"}},  # type: ignore[attr-defined]
            "trigger": {"source_id": "schedule", "source_version": 1, "config": {"every": "1h"}},
            "context": {"mode": "independent"},
        }
        r = live.post("/api/gateway/automations", headers=HEADERS, json=body)
        assert r.status_code == 200, r.text
        aid = r.json()["automation_id"]
        dead = wait_until(lambda: (lambda s: s if s["status"] == "failed" else None)(_summary(live, aid)), timeout_s=20)
    keep = live.post("/api/gateway/automations", headers=HEADERS, json={**body, "request_id": "lives", "title": "Lives",
                     "target": {**body["target"], "flow_id": ECHO_FLOW_ID}, "trigger": {"source_id": "manual", "source_version": 1, "config": {}}})
    assert keep.status_code == 200, keep.text

    # Served in its own group, counted, with exactly what can be done with it.
    assert dead["section"] == "failed" and dead["capabilities"] == ["revise", "resume", "archive", "discuss"]
    assert dead["flow_missing"] is True
    # One plain sentence (what happened and when), the raw error aside.
    assert re.fullmatch(r"It stopped on \d{1,2} [A-Z][a-z]{2} while starting run #1 because its flow is not installed\.",
                        dead["failed_reason"]), dead["failed_reason"]
    assert dead["failed_detail"] == f"Workflow '{hidden}' not found in registry"
    assert dead["current_occurrence"] is None  # never the stale "Run #1 running" of a dead controller
    assert (dead["last_occurrence"]["status"], dead["last_occurrence"]["not_started"]) == ("failed", True)
    [stuck] = live.get(f"/api/gateway/automations/{aid}/occurrences", headers=HEADERS).json()["items"]
    assert (stuck["status"], stuck["not_started"], stuck["answer"], stuck["artifacts"]) == ("failed", True, "", [])
    assert stuck["failure"]["reason_code"] == "occurrence_not_started"
    assert stuck["failure"]["message"] == "This run did not start because its flow is not installed."
    assert stuck["failure"]["detail"] == dead["failed_detail"]
    assert stuck["finished_at"] and "ledger_url" not in stuck and "workspace_url" not in stuck
    # Its run does not exist: the run routes answer 404, which the row's `not_started` announces.
    assert live.get(f"/api/gateway/runs/{stuck['run_id']}", headers=HEADERS).status_code == 404
    listing = live.get("/api/gateway/automations", headers=HEADERS).json()
    assert [s["section"] for s in listing["items"]] == ["active", "failed"]
    assert listing["failed_automations_count"] == 1 and listing["paused_automations_count"] == 0
    for typ in ("automation.pause", "automation.run_now", "automation.stop_current"):
        r = _post(live, aid, typ, f"no-{typ}")
        assert r.status_code == 409 and r.json()["detail"]["reason_code"] == "invalid_state", (typ, r.text)

    # Edit first (an installed workflow), then turn it back on.
    r = live.patch(f"/api/gateway/automations/{aid}", headers=HEADERS, json={
        "command_id": "fix", "changes": {"target": {**body["target"], "flow_id": ECHO_FLOW_ID}}})
    assert r.status_code == 200, r.text
    wait_until(lambda: live.get(f"/api/gateway/automations/{aid}", headers=HEADERS).json()["definition"]["revision"] == 2)
    assert _post(live, aid, "automation.resume", "on").status_code == 200
    back = wait_until(lambda: (lambda s: s if s["status"] == "active" and s.get("next_fire_at") else None)(_summary(live, aid)), timeout_s=20)
    assert back["section"] == "active" and back["current_occurrence"] is None
    assert back["flow_missing"] is False and "failed_reason" not in back
    assert back["attention"]["unseen_count"] == 0  # the weeks-old run was closed quietly
    assert back["last_occurrence"]["index"] == 1 and back["last_occurrence"]["status"] == "failed"
    assert back["next_fire_at"] > back["updated_at"][:19]  # the schedule continues from now
    occ = live.get(f"/api/gateway/automations/{aid}/occurrences", headers=HEADERS).json()["items"]
    assert [(o["index"], o["status"]) for o in occ] == [(1, "failed")]
    assert occ[0]["task"] == "hello" and occ[0]["user_turn"].startswith("[Trigger schedule@1 · occurrence 1")
    # Closed at the resume: the same plain reason, the raw error aside.
    assert occ[0]["failure"]["message"] == "This run did not start because its flow is not installed."
    assert occ[0]["failure"]["detail"] == f"Workflow '{hidden}' not found in registry"
    listing = live.get("/api/gateway/automations", headers=HEADERS).json()
    assert listing["failed_automations_count"] == 0 and {s["section"] for s in listing["items"]} == {"active"}

    # The revision chosen while it was failed is what runs now.
    assert _post(live, aid, "automation.run_now", "now").status_code == 200
    wait_until(lambda: (lambda o: o if o and o[0]["index"] == 2 and o[0]["status"] == "completed" else None)(
        live.get(f"/api/gateway/automations/{aid}/occurrences", headers=HEADERS).json()["items"]), timeout_s=20)


def test_a_missing_workflow_fails_that_run_and_the_automation_stays_active(live: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Operator ruling 2026-11-28: a failure never deactivates an automation. The run fails once
    (no retry) with its reason; the automation stays active with every control; once the
    workflow is there again, the next run succeeds."""
    from abstractruntime.scheduler.registry import WorkflowRegistry

    hidden = f"{live.bundle_ref}:{ECHO_FLOW_ID}"  # type: ignore[attr-defined]
    original_get = WorkflowRegistry.get
    r = live.post("/api/gateway/automations", headers=HEADERS, json={
        "request_id": "survives",
        "title": "Survives",
        "target": {"bundle_ref": live.bundle_ref, "flow_id": ECHO_FLOW_ID, "input_data": {"prompt": "hello"}},  # type: ignore[attr-defined]
        "trigger": {"source_id": "manual", "source_version": 1, "config": {}},
        "context": {"mode": "independent"},
        "policy": {"retry": {"max_attempts": 3, "backoff": {"initial": "1s", "factor": 2, "max": "1s"}}},
    })
    assert r.status_code == 200, r.text
    aid = r.json()["automation_id"]
    with monkeypatch.context() as m:
        m.setattr(WorkflowRegistry, "get", lambda self, wid: None if wid == hidden else original_get(self, wid))
        alive = _summary(live, aid)  # alive but broken: the flow it pins is not loaded
        assert alive["status"] == "active" and alive["flow_missing"] is True and "failed_reason" not in alive
        assert _post(live, aid, "automation.run_now", "go").status_code == 200
        done = wait_until(lambda: (lambda s: s if (s.get("last_occurrence") or {}).get("status") == "failed" else None)(_summary(live, aid)), timeout_s=20)
    assert done["status"] == "active" and done["section"] == "active" and done["current_occurrence"] is None
    assert {"run_now", "pause", "revise", "archive"} <= set(done["capabilities"]) and "paused_reason" not in done
    assert done["last_occurrence"]["attempts"] == 1  # no retry inside the run
    assert done["attention"]["unseen_count"] == 1
    occ = live.get(f"/api/gateway/automations/{aid}/occurrences", headers=HEADERS).json()["items"]
    assert [(o["index"], o["status"]) for o in occ] == [(1, "failed")]
    assert occ[0]["failure"]["message"] == (f"This run could not start: its workflow ({hidden}) is no longer installed. "
                                            "Edit the automation to choose a workflow that is installed.")
    # The workflow is there again (a live fact per row): the next run succeeds.
    assert _summary(live, aid)["flow_missing"] is False
    assert _post(live, aid, "automation.run_now", "go-2").status_code == 200
    wait_until(lambda: (lambda o: o if o and o[0]["index"] == 2 and o[0]["status"] == "completed" else None)(
        live.get(f"/api/gateway/automations/{aid}/occurrences", headers=HEADERS).json()["items"]), timeout_s=20)
    assert _summary(live, aid)["status"] == "active"


def test_row_and_door_of_a_completed_and_of_an_exhausted_failed_automation(live: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import datetime, timezone

    from abstractruntime.automations import controller as controller_mod
    from abstractruntime.scheduler.registry import WorkflowRegistry

    now = datetime.now(timezone.utc).isoformat()
    once = {"source_id": "schedule", "source_version": 1, "config": {"start_at": now}}

    def create(rid: str, flow: str) -> str:
        r = live.post("/api/gateway/automations", headers=HEADERS, json={
            "request_id": rid, "title": rid, "trigger": once, "context": {"mode": "independent"},
            "target": {"bundle_ref": live.bundle_ref, "flow_id": flow, "input_data": {"prompt": "hello"}}})  # type: ignore[attr-defined]
        assert r.status_code == 200, r.text
        return r.json()["automation_id"]

    # Completed: archive only, through the row AND the door.
    done = create("once-ok", ECHO_FLOW_ID)
    row = wait_until(lambda: (lambda s: s if s["status"] == "completed" else None)(_summary(live, done)), timeout_s=20)
    assert row["capabilities"] == ["archive", "discuss"]
    for typ in ("automation.resume", "automation.run_now", "automation.pause"):
        r = _post(live, done, typ, f"c-{typ}")
        assert r.status_code == 409 and r.json()["detail"]["reason_code"] == "invalid_state", (typ, r.text)
    r = live.patch(f"/api/gateway/automations/{done}", headers=HEADERS, json={"command_id": "c-rv", "changes": {"title": "x"}})
    assert r.status_code == 409, r.text
    assert _post(live, done, "automation.archive", "c-arch").status_code == 200

    # A one-shot whose controller died on its only run: failed, nothing left to run.
    hidden = f"{live.bundle_ref}:slow"  # type: ignore[attr-defined]
    original_get = WorkflowRegistry.get
    dispatch = controller_mod.dispatch

    def dispatch_before_the_fix(turn):
        effect = dispatch(turn)
        effect.payload.pop("_absorb_failure")
        return effect

    with monkeypatch.context() as m:
        m.setattr(WorkflowRegistry, "get", lambda self, wid: None if wid == hidden else original_get(self, wid))
        m.setattr(controller_mod, "dispatch", dispatch_before_the_fix)
        # A record that died before 2026-11-28 (no after-tick revival then), restarted by a person
        # before the host brings it back (that path: test_automation_runner_brings_back.py).
        from abstractruntime.automations import commands as commands_mod

        monkeypatch.setattr(commands_mod, "revive_dead_controller", lambda *a, **k: False)
        dead = create("once-dead", "slow")
        row = wait_until(lambda: (lambda s: s if s["status"] == "failed" else None)(_summary(live, dead)), timeout_s=20)
    assert row["capabilities"] == ["revise", "archive", "discuss"]  # no resume: nothing left to run
    r = _post(live, dead, "automation.resume", "d-on")
    assert r.status_code == 409 and "nothing left to run" in r.json()["detail"]["message"], r.text


# --------------------------------------------------------------------- the live line (spec C)

from abstractgateway.automation_live import MAX_ARGUMENT_CHARS, bounded_arguments, fold_live  # noqa: E402


def _act(kind, status, order, **kw):
    return {"kind": kind, "status": status, "order": order, "ended_at": None if status == "running" else "t", **kw}


@pytest.mark.parametrize(
    "view,expected",
    [
        ({"activities": []}, {"state": "starting", "tool": None, "steps_done": 0}),
        # A container sub-workflow never counts; only llm/tool activities do.
        ({"activities": [_act("subworkflow", "running", ["2026-11-27T10:00:00", "r", 1])]},
         {"state": "starting", "tool": None, "steps_done": 0}),
        ({"activities": [_act("llm", "running", ["2026-11-27T10:00:01", "c", 2])]},
         {"state": "thinking", "tool": None, "steps_done": 0}),
        # Between two steps (one ended, none open yet): thinking, never starting.
        ({"activities": [_act("tool", "completed", ["2026-11-27T10:00:01", "c", 2], tool_calls=[
            {"name": "web_search", "arguments": {"query": "a"}, "status": "completed"}])]},
         {"state": "thinking", "tool": None, "steps_done": 1}),
        # The newest open activity wins; ended tool calls are the steps done.
        ({"activities": [
            _act("tool", "completed", ["2026-11-27T10:00:01", "c", 2], tool_calls=[
                {"name": "web_search", "arguments": {"query": "a"}, "status": "completed"},
                {"name": "fetch_url", "arguments": {"url": "u"}, "status": "failed"}]),
            _act("llm", "running", ["2026-11-27T10:00:02", "c", 3]),
            _act("tool", "running", ["2026-11-27T10:00:03", "c", 4], tool_calls=[
                {"name": "web_search", "arguments": {"query": "AI news", "opts": {"deep": True}}, "status": "running"}]),
        ]}, {"state": "tool", "tool": {"name": "web_search", "arguments": {"query": "AI news"}}, "steps_done": 2}),
        # A person-facing interaction (a question, a tool approval) is "waiting".
        ({"interaction": {"kind": "ask_user"}, "activities": [_act("llm", "running", ["t", "c", 1])]},
         {"state": "waiting", "tool": None, "steps_done": 0}),
    ],
)
def test_fold_live_cases(view, expected) -> None:
    assert fold_live(view) == expected


def test_tool_arguments_are_bounded_for_one_line() -> None:
    args = {f"k{i}": "x" * 500 for i in range(12)}
    args["k0"] = {"nested": 1}
    out = bounded_arguments(args)
    assert len(out) == 8 and "k0" not in out and all(len(v) == MAX_ARGUMENT_CHARS for v in out.values())


def test_the_detail_read_serves_the_live_line_and_the_list_does_not(live: TestClient) -> None:
    r = live.post("/api/gateway/automations", headers=HEADERS, json={
        "request_id": "asks", "title": "Asks",
        "target": {"bundle_ref": live.bundle_ref, "flow_id": "ask", "input_data": {"prompt": "hello"}},  # type: ignore[attr-defined]
        "trigger": {"source_id": "manual", "source_version": 1, "config": {}},
        "context": {"mode": "independent"},
    })
    assert r.status_code == 200, r.text
    aid = r.json()["automation_id"]
    assert _post(live, aid, "automation.run_now", "go").status_code == 200

    def waiting():
        occ = _summary(live, aid).get("current_occurrence") or {}
        return occ if (occ.get("live") or {}).get("state") == "waiting" else None

    occ = wait_until(waiting, timeout_s=20)
    child = live.get(f"/api/gateway/runs/{occ['run_id']}", headers=HEADERS).json()
    assert occ["live"] == {"started_at": child["created_at"], "state": "waiting", "tool": None, "attempt": 1, "steps_done": 0}
    row = next(s for s in live.get("/api/gateway/automations", headers=HEADERS).json()["items"] if s["automation_id"] == aid)
    assert row["current_occurrence"] is not None and "live" not in row["current_occurrence"]


# --------------------------------------------------------------------- the catalog guard (spec D)


def test_a_refused_catalog_workflow_is_a_typed_start_failure(tmp_path: Path) -> None:
    from abstractruntime import Effect, EffectType, Runtime
    from abstractruntime.core.models import RunState, RunStatus
    from abstractruntime.core.runtime import START_FAILURE_KEY
    from abstractruntime.storage.in_memory import InMemoryLedgerStore, InMemoryRunStore

    from abstractgateway.hosts.bundle_host import _install_catalog_subworkflow_guard
    from abstractgateway.workflow_catalog import catalog_internal_bundle_id

    runtime = Runtime(run_store=InMemoryRunStore(), ledger_store=InMemoryLedgerStore())
    _install_catalog_subworkflow_guard(runtime=runtime, catalog_root_data_dir=tmp_path, catalog_tenant_id="default",
                                       catalog_runtime_id="admin")
    wid = f"{catalog_internal_bundle_id(tenant_id='default', bundle_id='shared')}@1.0.0:main"
    parent = RunState(run_id="p", workflow_id="x", status=RunStatus.RUNNING, current_node="n", vars={})
    effect = Effect(type=EffectType.START_SUBWORKFLOW, payload={"workflow_id": wid, "_absorb_failure": True}, result_key="r")
    outcome = runtime._handlers[EffectType.START_SUBWORKFLOW](parent, effect, "next")
    assert outcome.status == "failed" and outcome.retryable is False
    assert outcome.error_details == {START_FAILURE_KEY: "workflow_not_allowed", "workflow_id": wid}
    assert outcome.error.startswith(f"Catalog subworkflow '{wid}' is not allowed: ")


def test_the_stopped_sentence_is_plain_and_dated() -> None:
    import datetime

    from abstractgateway.routes.automations import stopped_sentence

    now = datetime.datetime(2026, 11, 28, tzinfo=datetime.timezone.utc)
    base = {"at": "2026-11-07T19:53:30.942456+00:00", "run_index": 38, "run_started": False, "cause": None, "detail": "x"}
    assert stopped_sentence(base, "Europe/Paris", now=now) == "It stopped on 7 Nov while starting run #38."
    assert stopped_sentence({**base, "run_index": None}, "Europe/Paris", now=now) == "It stopped on 7 Nov."
    assert stopped_sentence({**base, "run_started": True}, "UTC", now=now) == "It stopped on 7 Nov during run #38."
    assert stopped_sentence({**base, "cause": "flow_not_installed"}, "UTC", now=now) == (
        "It stopped on 7 Nov while starting run #38 because its flow is not installed.")
    late = {**base, "at": "2025-12-31T23:30:00+00:00", "run_index": None}
    assert stopped_sentence(late, "Europe/Paris", now=now) == "It stopped on 1 Jan."  # the owner's day, this year
    assert stopped_sentence(late, "UTC", now=now) == "It stopped on 31 Dec 2025."


def test_an_archived_automation_whose_controller_died_shows_nothing_running(live: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A controller that ended (failed or cancelled), archived or not, supervises nothing: no run
    reads "running" for it (the copy of the operator's archived `bbc08b4b` showed #314 running)."""
    from abstractruntime.automations import commands as commands_mod
    from abstractruntime.automations import controller as controller_mod
    from abstractruntime.scheduler.registry import WorkflowRegistry

    hidden = f"{live.bundle_ref}:slow"  # type: ignore[attr-defined]
    original_get = WorkflowRegistry.get
    dispatch = controller_mod.dispatch

    def dispatch_before_the_fix(turn):
        effect = dispatch(turn)
        effect.payload.pop("_absorb_failure")
        return effect

    monkeypatch.setattr(WorkflowRegistry, "get", lambda self, wid: None if wid == hidden else original_get(self, wid))
    monkeypatch.setattr(controller_mod, "dispatch", dispatch_before_the_fix)
    monkeypatch.setattr(commands_mod, "revive_dead_controller", lambda *a, **k: False)  # a record from before the rule
    r = live.post("/api/gateway/automations", headers=HEADERS, json={
        "request_id": "archived-dead", "title": "Archived dead", "context": {"mode": "independent"},
        "target": {"bundle_ref": live.bundle_ref, "flow_id": "slow", "input_data": {"prompt": "hello"}},  # type: ignore[attr-defined]
        "trigger": {"source_id": "schedule", "source_version": 1, "config": {"every": "1h"}},
    })
    assert r.status_code == 200, r.text
    aid = r.json()["automation_id"]
    wait_until(lambda: _summary(live, aid)["status"] == "failed", timeout_s=20)
    assert _post(live, aid, "automation.archive", "arch").status_code == 200
    s = wait_until(lambda: (lambda x: x if x["status"] == "archived" else None)(_summary(live, aid)), timeout_s=20)
    assert s["current_occurrence"] is None
    assert s["last_occurrence"]["status"] == "failed" and s["last_occurrence"]["not_started"] is True
