"""Own time is the operator's to give (operator ruling 2026-11-08; clarification 2026-11-09: "own time
can only be given and removed by the operator ... if granted, when there are transitions to other
states, it can go back to that state"; root untracked/entity-phases-2026-11-08/CONTRACT-phases.md v2
§4). Reconcile RESTORES the phase an interruption took over (`resume_to`) — after a visit or chat
closes (normal or idle), a spontaneous sleep ends, a request ends, the work order is cleared, an
operator sleep's maintenance is done — only while its grant is still on; never under a visit, a
sleep, work or the kill switch; never twice; never from idle by itself, and never after a crash.
An interrupted sleep continues as the same sleep (§4.4). Hermetic gateway; the spawner is faked."""

from __future__ import annotations

from abstractruntime.identity.lifecycle_fence import LifecycleFence as _LF
import copy
import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractruntime.identity.visit_workflow")

_TOKEN = "entity-reconcile-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import entity_chat

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setattr(entity_chat, "gateway_text_mind",
                        lambda: {"provider": "lmstudio", "model": "test-model", "base_url": None, "reasoning": None})
    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: _ScriptedLLM())


class _ScriptedLLM:
    class _Reply:
        def __init__(self, content: str) -> None:
            self.content = content

    def generate(self, **kwargs):
        return self._Reply("Fine.")


class _Spawns(list):
    """The faked spawns (slugs), plus the `return_to` each was told (runtime seam 2026-11-09)."""

    def __init__(self) -> None:
        super().__init__()
        self.return_to: list = []


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch):
    from abstractgateway import entity_phase

    calls = _Spawns()
    lock = threading.Lock()

    def _fake_start(registry, manifest, params, return_to=None):
        time.sleep(0.2)  # a real spawn takes a moment: concurrent reconciles overlap here
        with lock:
            calls.append(manifest.slug)
            calls.return_to.append(return_to)
        return {"pid": 7000 + len(calls)}

    monkeypatch.setattr(entity_phase, "start_own_time", _fake_start)
    return calls


def _spark(name: str = "Castor") -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _home_dir():
    from pathlib import Path

    from abstractgateway.service import get_gateway_service

    return Path(get_gateway_service().config.data_dir) / "entities" / "castor"


def _registry():
    from abstractgateway.routes.entities import _registry as reg

    return reg()


def _born_awake_with_personal(client, personal: bool = True):
    from abstractruntime.identity.life import write_entity_state, write_personal_grant

    assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
    if personal:
        write_personal_grant(_home_dir(), mode="until_revoked", granted_by="person:admin", fence=_LF.admit(_home_dir()))
    write_entity_state(_home_dir(), "awake", reason="woken", fence=_LF.admit(_home_dir()))


@pytest.fixture
def own_time(monkeypatch: pytest.MonkeyPatch):
    """Put the entity in own time for real (a live loop the gateway observes: resume_to=personal)
    and give a way to end that loop cleanly while an interruption holds the phase."""
    procs = []

    def _start(client):
        _born_awake_with_personal(client)
        from entity_fake_loop import spawn

        proc = spawn(_home_dir(), "between", 60)
        procs.append(proc)
        assert client.get("/api/gateway/entities/Castor/life_state").json()["phase"] == "personal"
        return proc

    def _end(proc, stopped_by="yielded_and_exited"):
        proc.kill()
        proc.wait(5)
        (_home_dir() / "loop_status").write_text(
            json.dumps({"phase": "stopped", "pid": proc.pid, "stopped_by": stopped_by}), encoding="utf-8")

    yield SimpleNamespace(start=_start, end=_end)
    for p in procs:
        p.kill()


@pytest.mark.parametrize("lane", ["visit", "chat"])
def test_a_visit_close_returns_to_own_time(spawned, own_time, lane):
    """personal -> visit -> close returns to personal (the loop ended during the visit)."""
    with _client() as client:
        proc = own_time.start(client)
        if lane == "visit":
            opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
            assert opened.status_code == 200, opened.text
            own_time.end(proc)
            assert spawned == []  # never under a visit
            closed = client.post(f"/api/gateway/entities/Castor/visit/{opened.json()['run_id']}/close",
                                 json={"closed_by": "operator"})
        else:
            opened = client.post("/api/gateway/entities/Castor/chat/open", json={"context_window": 32000})
            assert opened.status_code == 200, opened.text
            own_time.end(proc)
            closed = client.post(f"/api/gateway/entities/Castor/chat/{opened.json()['chat_id']}/close")
        assert closed.status_code == 200, closed.text
        assert spawned == ["castor"]


def test_idle_with_the_grant_on_stays_idle_without_an_operator_act(spawned):
    """The entity never gives itself own time: idle (grant on, nothing running) -> visit -> close
    stays idle."""
    with _client() as client:
        _born_awake_with_personal(client)
        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        client.post(f"/api/gateway/entities/Castor/visit/{opened.json()['run_id']}/close", json={"closed_by": "operator"})
        assert spawned == []
        assert client.get("/api/gateway/entities/Castor/life_state").json()["phase"] == "idle"


def test_a_grant_revoked_during_the_visit_means_idle_after_it(spawned, own_time):
    with _client() as client:
        proc = own_time.start(client)
        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert client.put("/api/gateway/entities/Castor/life_state/toggles", json={"personal": False}).status_code == 200
        own_time.end(proc)
        client.post(f"/api/gateway/entities/Castor/visit/{opened.json()['run_id']}/close", json={"closed_by": "operator"})
        assert spawned == []
        assert client.get("/api/gateway/entities/Castor/life_state").json()["phase"] == "idle"
        # The remembered phase retired with that interruption: a later visit does not bring own
        # time back, even once a grant stands again (only the operator's own act starts it).
        from abstractruntime.identity.life import write_personal_grant

        write_personal_grant(_home_dir(), mode="until_revoked", granted_by="person:admin", fence=_LF.admit(_home_dir()))
        again = client.post("/api/gateway/entities/Castor/visit/open", json={})
        client.post(f"/api/gateway/entities/Castor/visit/{again.json()['run_id']}/close", json={"closed_by": "operator"})
        assert spawned == []


def test_a_spontaneous_sleep_returns_to_own_time_when_it_ends(spawned, own_time):
    from abstractruntime.identity.life import write_entity_state

    with _client() as client:
        proc = own_time.start(client)
        write_entity_state(_home_dir(), "asleep", reason="self-elected sleep: tired", written_by="self", fence=_LF.admit(_home_dir()))
        assert client.get("/api/gateway/entities/Castor/life_state").json()["phase"] == "sleep"
        own_time.end(proc)
        write_entity_state(_home_dir(), "awake", reason="woke", written_by="self", fence=_LF.admit(_home_dir()))
        client.get("/api/gateway/entities/Castor/life_state")  # the gateway observes the wake
        assert spawned == ["castor"]


def test_a_crashed_loop_is_never_restarted_without_the_operator(spawned, own_time):
    with _client() as client:
        proc = own_time.start(client)
        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        own_time.end(proc, stopped_by="failures")
        client.post(f"/api/gateway/entities/Castor/visit/{opened.json()['run_id']}/close", json={"closed_by": "operator"})
        assert spawned == []
        r = client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "retry"})
        assert r.status_code == 200 and spawned == ["castor"]  # the operator's retry


def test_the_idle_reaper_close_returns_to_own_time_too(spawned, own_time):
    from abstractgateway.routes.entities import _visit_host

    with _client() as client:
        proc = own_time.start(client)
        host = _visit_host()
        host._idle_timeout_s = 0.05
        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert opened.status_code == 200, opened.text
        own_time.end(proc)
        time.sleep(0.3)
        assert host.reap_now() >= 1
        assert client.get("/api/gateway/entities/Castor/visit").json().get("open") is False
        assert spawned == ["castor"]


def test_personal_off_means_no_restart_and_he_stays_awake(spawned):
    from abstractruntime.identity.life import read_entity_state

    with _client() as client:
        _born_awake_with_personal(client, personal=False)
        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        client.post(f"/api/gateway/entities/Castor/visit/{opened.json()['run_id']}/close", json={"closed_by": "operator"})
        assert spawned == []
        assert read_entity_state(_home_dir())["state"] == "awake"
        assert client.get("/api/gateway/entities/Castor/life_state").json()["phase"] == "idle"


@pytest.mark.parametrize("writer", ["operator", "self"])
def test_an_interrupted_sleep_continues_as_the_same_sleep(spawned, writer):
    """visit→sleep is an allowed edge: the close restores the SAME sleep (who chose it, its wake
    time, its original since), and reconcile never starts own time over it."""
    from abstractruntime.identity.life import read_entity_state, write_entity_state

    procs = []
    with _client() as client:
        _born_awake_with_personal(client)
        home = _home_dir()
        if writer == "self":  # a self-chosen nap: the loop is alive and owns it
            from entity_fake_loop import spawn

            proc = spawn(home, "between", 60)
            procs.append(proc)
        write_entity_state(home, "asleep", reason="resting", written_by=writer, wake_at="2099-01-01T00:00:00+00:00", fence=_LF.admit(home))
        before = client.get("/api/gateway/entities/Castor/life_state").json()
        assert before["phase"] == "sleep"
        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert opened.status_code == 200, opened.text
        client.post(f"/api/gateway/entities/Castor/visit/{opened.json()['run_id']}/close", json={"closed_by": "operator"})
        st = read_entity_state(home)
        assert st["state"] == "asleep" and st["written_by"] == writer and st.get("wake_at") == "2099-01-01T00:00:00+00:00"
        after = client.get("/api/gateway/entities/Castor/life_state").json()
        assert after["phase"] == "sleep" and after["sleep"]["kind"] == writer
        assert after["sleep"]["since"] == before["sleep"]["since"]
        assert spawned == []
    for p in procs:
        p.kill()


def test_two_concurrent_reconciles_spawn_once(spawned):
    from abstractgateway import entity_phase

    with _client() as client:
        _born_awake_with_personal(client)
        reg = _registry()
        ts = [threading.Thread(target=entity_phase.reconcile, args=(reg, "castor", "test"), kwargs={"operator": True})
              for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        assert spawned == ["castor"]


def test_reconcile_never_starts_under_a_visit_opening_or_a_stop(spawned):
    from abstractruntime.identity.life import write_entity_state

    from abstractgateway import entity_phase

    with _client() as client:
        _born_awake_with_personal(client)
        write_entity_state(_home_dir(), "asleep", reason="in conversation with person:admin [visit v-9]",
                           mode="visiting", written_by="visit-door", fence=_LF.admit(_home_dir()))
        assert entity_phase.reconcile(_registry(), "castor", "test")["reason"] == "visit"
        write_entity_state(_home_dir(), "paused", reason="stop", fence=_LF.admit(_home_dir()))
        assert entity_phase.reconcile(_registry(), "castor", "test")["reason"] == "stopped"
        assert spawned == []


def test_a_stop_landing_during_the_spawn_kills_the_new_loop(monkeypatch: pytest.MonkeyPatch):
    from abstractgateway import entity_phase

    procs = []

    def _start_then_stop(registry, manifest, params, return_to=None):
        from entity_fake_loop import spawn

        proc = spawn(_home_dir(), "day", 60)
        procs.append(proc)
        from abstractruntime.identity.life import bump_lifecycle_generation

        bump_lifecycle_generation(registry.entities_dir / "castor", written_by="test")  # Stop's fence lands mid-spawn
        return {"pid": proc.pid}

    monkeypatch.setattr(entity_phase, "start_own_time", _start_then_stop)
    with _client() as client:
        _born_awake_with_personal(client)
        out = entity_phase.reconcile(_registry(), "castor", "test", operator=True)
        assert out == {"own_time": False, "pid": None, "reason": "stopped"}
        deadline = time.monotonic() + 6
        while procs[0].poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert procs[0].poll() is not None, "the loop spawned across the Stop outlived the fence"


def test_a_request_ending_and_the_work_order_cleared_return_to_own_time(spawned, own_time):
    from types import SimpleNamespace as NS

    from abstractgateway import entity_phase

    with _client() as client:
        proc = own_time.start(client)
        own_time.end(proc)  # a request took the phase; the loop ended meanwhile
        run = NS(run_id="r1", status="completed",
                 vars={"_runtime": {"entity": {"entity_id": "entity:castor", "channel": "workplace"}}})
        entity_phase.note_run_terminal(run)
        deadline = time.monotonic() + 5
        while not spawned and time.monotonic() < deadline:
            time.sleep(0.05)
        assert spawned == ["castor"]

    spawned.clear()
    with _client() as client:
        proc = own_time.start(client)
        own_time.end(proc)
        (_home_dir() / "work_order.md").write_text("Sort the archive.\n", encoding="utf-8")
        cleared = client.put("/api/gateway/entities/Castor/work-order", json={"clear": True})
        assert cleared.status_code == 200, cleared.text
        assert spawned == ["castor"]


def test_a_graceful_stop_ends_own_time_for_good(spawned, own_time):
    """A loop that ended because it was asked to (the operator's stop, personal off) is own time
    ending, not an interruption: nothing restores it."""
    with _client() as client:
        proc = own_time.start(client)
        own_time.end(proc, stopped_by="stop_command")
        assert client.get("/api/gateway/entities/Castor/life_state").json()["phase"] == "idle"
        client.get("/api/gateway/entities/Castor/life_state")
        assert spawned == []


def test_an_operator_sleep_wakes_when_maintenance_is_done(spawned):
    from abstractruntime.identity.life import read_entity_state, read_personal_grant

    with _client() as client:
        _born_awake_with_personal(client, personal=False)
        # The operator gives own time (his act: it starts at once; the spawner is faked).
        assert client.put("/api/gateway/entities/Castor/life_state/toggles", json={"personal": True}).status_code == 200
        assert spawned == ["castor"]
        r = client.post("/api/gateway/entities/Castor/state", json={"state": "sleep", "dream": True})
        assert r.status_code == 200, r.text
        assert r.json()["maintenance"] == "woke"
        assert read_entity_state(_home_dir())["state"] == "awake"
        assert read_personal_grant(_home_dir())["mode"] == "until_revoked"  # sleep never turns personal off
        assert spawned == ["castor", "castor"]  # back to own time after the maintenance


def test_a_visit_during_maintenance_takes_the_wake_to_its_close(spawned, monkeypatch: pytest.MonkeyPatch):
    import abstractmemory
    from abstractruntime.identity.life import read_entity_state, write_entity_state

    from abstractgateway import entity_phase

    def _pass_then_a_visitor(*args, **kwargs):
        write_entity_state(_home_dir(), "asleep", reason="in conversation with person:admin [visit v-3]",
                           mode="visiting", written_by="visit-door", fence=_LF.admit(_home_dir()))
        return {"ok": True}

    monkeypatch.setattr(abstractmemory, "sleep_pass", _pass_then_a_visitor)
    with _client() as client:
        _born_awake_with_personal(client, personal=False)
        assert client.put("/api/gateway/entities/Castor/life_state/toggles", json={"personal": True}).status_code == 200
        spawned.clear()
        r = client.post("/api/gateway/entities/Castor/state", json={"state": "sleep", "dream": True})
        assert r.status_code == 200, r.text
        assert r.json()["maintenance"] == "after_visit"
        assert read_entity_state(_home_dir())["mode"] == "visiting"  # the pass never wrote over the visit
        assert spawned == []
        # The visit closes and restores the interrupted sleep; its maintenance is done: he wakes.
        write_entity_state(_home_dir(), "asleep", reason="operator sleep restored (visit ended)", written_by="operator", fence=_LF.admit(_home_dir()))
        entity_phase.after_visit_closed(_registry(), "castor", {"state": "asleep"})
        assert read_entity_state(_home_dir())["state"] == "awake"
        assert spawned == ["castor"]


def test_next_names_where_an_interruption_returns(spawned, own_time):
    """`next.phase` is the wire face of the remembered phase (Entity app ask, a24448c): during a
    visit and during a spontaneous sleep it says where he returns; a grant revoked meanwhile says
    idle, with the sentence."""
    from abstractruntime.identity.life import write_entity_state

    with _client() as client:
        proc = own_time.start(client)
        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        ls = client.get("/api/gateway/entities/Castor/life_state").json()
        assert ls["phase"] == "visit"
        assert ls["next"] == {"phase": "personal", "sentence": "When the visit closes, his own time resumes."}
        assert client.put("/api/gateway/entities/Castor/life_state/toggles", json={"personal": False}).status_code == 200
        ls = client.get("/api/gateway/entities/Castor/life_state").json()
        assert ls["next"] == {"phase": "idle", "sentence": "Own time is off; he stays awake."}
        client.post(f"/api/gateway/entities/Castor/visit/{opened.json()['run_id']}/close", json={"closed_by": "operator"})
        proc.kill()

    with _client() as client:
        own_time.start(client)
        write_entity_state(_home_dir(), "asleep", reason="self-elected sleep: tired", written_by="self",
                           wake_at="2099-01-01T00:00:00+00:00", fence=_LF.admit(_home_dir()))
        ls = client.get("/api/gateway/entities/Castor/life_state").json()
        assert ls["phase"] == "sleep" and ls["next"]["phase"] == "personal"
        assert ls["next"]["sentence"] == "When he wakes and his own time resumes."


def test_next_during_a_visit_that_interrupted_a_sleep_is_the_sleep(spawned):
    from abstractruntime.identity.life import write_entity_state

    with _client() as client:
        _born_awake_with_personal(client)
        write_entity_state(_home_dir(), "asleep", reason="operator sleep", fence=_LF.admit(_home_dir()))
        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert opened.status_code == 200, opened.text
        ls = client.get("/api/gateway/entities/Castor/life_state").json()
        assert ls["next"] == {"phase": "sleep", "sentence": "When the visit closes, he goes back to sleep."}
        # Nothing was interrupted under the sleep: the plain visit's next is idle.
        client.post(f"/api/gateway/entities/Castor/visit/{opened.json()['run_id']}/close", json={"closed_by": "operator"})


def test_a_request_interrupting_own_time_keeps_the_way_back(spawned):
    """Codex review #10: observing a summon's end (work -> idle) cleared resume_to like an own-time
    work day ending, so a read racing the run's end hook prevented the restore."""
    from abstractgateway.routes.entities import _chat_host, _visit_host

    with _client() as client:
        _born_awake_with_personal(client, personal=False)
        assert client.put("/api/gateway/entities/Castor/life_state/toggles", json={"personal": True}).status_code == 200
        assert spawned == ["castor"]  # own time given by the operator (faked: no live loop)
        live = {"on": True}
        probe = lambda slug: ({"run_id": "s-1", "session_id": "x", "holder": "agent:bot", "holder_kind": "agent"}
                              if live["on"] else None)
        _visit_host()._summon_seat_probe = probe
        _chat_host()._summon_seat_probe = probe
        assert client.get("/api/gateway/entities/Castor/life_state").json()["phase"] == "work"
        live["on"] = False
        client.get("/api/gateway/entities/Castor/life_state")  # the read sees the request end first
        assert spawned == ["castor", "castor"], "the interrupted own time was not restored"


def test_the_child_is_told_where_own_time_returns(spawned):
    """Runtime seam (2026-11-09, `spawn_loop_process(return_to=)`): the operator's own act starts
    personal time; a restore names the remembered phase — personal, or work when work was what
    the interruption took over. Without it the child decides from its own record alone."""
    with _client() as client:
        _born_awake_with_personal(client, personal=False)
        assert client.put("/api/gateway/entities/Castor/life_state/toggles", json={"personal": True}).status_code == 200
        assert spawned == ["castor"] and spawned.return_to == ["personal"]  # the operator's act


def test_a_restore_tells_the_child_the_remembered_phase(spawned, own_time):
    from abstractgateway import entity_phase

    with _client() as client:
        proc = own_time.start(client)
        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        own_time.end(proc)
        client.post(f"/api/gateway/entities/Castor/visit/{opened.json()['run_id']}/close", json={"closed_by": "operator"})
        assert spawned.return_to[-1] == "personal"  # the restore of an interrupted personal time

        # Queued jobs start the loop and tell it nothing (runtime v3: its gate works the queue, then
        # returns to the remembered phase or ends idle).
        (_home_dir() / "work_order.md").write_text("Sort the archive.\n", encoding="utf-8")
        reg = _registry()
        before = len(spawned)
        assert entity_phase.reconcile(reg, "castor", "test")["own_time"] is True
        assert len(spawned) == before + 1 and spawned.return_to[-1] is None


def test_start_loop_hands_return_to_to_the_runtime(monkeypatch, tmp_path):
    from abstractruntime.identity import life

    from abstractgateway import entity_loop

    seen = {}
    monkeypatch.setattr(life, "spawn_loop_process", lambda home, **kw: seen.update(kw) or {"pid": 1})
    monkeypatch.setattr(entity_loop, "loop_status", lambda home: {"running": False})
    entity_loop.start_loop(tmp_path, provider="lmstudio", model="m", return_to="personal")
    assert seen.get("return_to") == "personal"


@pytest.mark.parametrize("stopped_by", ["no_return", "work_off", "edge_removed", "jobs_done"])
def test_a_loop_that_ends_on_purpose_hands_the_phase_back(spawned, own_time, stopped_by):
    """The runtime batch's stop words (2026-11-09) are requested ends, never crashes: the gateway
    repairs the gate sleep the loop left (he is awake and reachable), shows no crash, and does
    not restart own time by itself."""
    from abstractruntime.identity.life import read_entity_state, write_entity_state

    with _client() as client:
        proc = own_time.start(client)
        write_entity_state(_home_dir(), "asleep", reason="own time ended", written_by="loop", fence=_LF.admit(_home_dir()))
        own_time.end(proc, stopped_by=stopped_by)
        body = client.get("/api/gateway/entities/Castor/life_state").json()
        assert read_entity_state(_home_dir())["state"] == "awake", "the gate sleep was left in place"
        assert body["phase"] == "idle" and (body.get("blocked") or {}).get("code") != "loop_crashed"
        assert spawned == []


def test_his_own_sleep_ends_when_its_maintenance_is_done(spawned, own_time):
    """Runtime a5ac083: the entity's own sleep carries no clock; `sleep.ends` says it ends when its
    maintenance is done (never null, never a time)."""
    from abstractruntime.identity.life import write_entity_state

    with _client() as client:
        proc = own_time.start(client)
        write_entity_state(_home_dir(), "asleep", reason="self-elected sleep: tired", written_by="self", fence=_LF.admit(_home_dir()))
        ls = client.get("/api/gateway/entities/Castor/life_state").json()
        assert ls["phase"] == "sleep" and ls["sleep"]["kind"] == "self"
        assert ls["sleep"]["ends"] == "when maintenance is done"
        proc.kill()


def test_a_loop_that_ends_with_jobs_left_is_not_respawned_by_its_own_end(spawned, own_time):
    """Jobs drive work (v3 D1), but never a respawn loop: the loop ending on its own word with a job
    still queued starts nothing; the next trigger (a job queued, an interruption ending, the
    operator) does."""
    with _client() as client:
        proc = own_time.start(client)
        (_home_dir() / "work_order.md").write_text("Sort the archive.\n", encoding="utf-8")
        own_time.end(proc, stopped_by="no_return")
        client.get("/api/gateway/entities/Castor/life_state")
        assert spawned == []
        assert client.put("/api/gateway/entities/Castor/work-order", json={"order": "Sort it all."}).status_code == 200
        assert spawned == ["castor"] and spawned.return_to == [None]


def test_resume_to_is_the_runtimes_bucket(spawned):
    """Runtime v3 (ed32f0e/901d9c5): the loop and the kill switch write `resume_to` themselves, and
    the bucket shadows the gateway sidecar — the gateway reads and writes it only through
    read_resume_to / write_resume_to."""
    from abstractruntime.identity.life import read_resume_to, write_resume_to

    from abstractgateway import entity_phase

    with _client() as client:
        _born_awake_with_personal(client)
        reg = _registry()
        write_resume_to(_home_dir(), None, written_by="loop", fence=_LF.admit(_home_dir()))  # the bucket exists (the runtime wrote it)
        entity_phase.set_resume_to(reg, "castor", "personal")
        assert read_resume_to(_home_dir())["phase"] == "personal"  # the gateway's word reaches the bucket
        write_resume_to(_home_dir(), None, written_by="loop", fence=_LF.admit(_home_dir()))  # the loop ended idle
        assert entity_phase.read_record(reg.entities_dir, "castor")["resume_to"] is None


def test_jobs_queued_is_the_runtimes_queue(spawned):
    """`work.jobs_queued` is read_job_queue's count: a task the loop has TAKEN is still a job."""
    from abstractruntime.identity.life import append_task_status

    with _client() as client:
        _born_awake_with_personal(client, personal=False)
        t = client.post("/api/gateway/entities/Castor/tasks", json={"title": "sort the archive"})
        assert t.status_code == 200, t.text
        append_task_status(_home_dir(), t.json()["task"]["task_id"], "taken", by="entity:castor")
        assert client.get("/api/gateway/entities/Castor/life_state").json()["work"]["jobs_queued"] == 1


def test_a_job_interrupting_own_time_keeps_the_personal_return(spawned, own_time):
    """Codex review 2 #6: publishing the queue's work day overwrote resume_to=personal with "work",
    so the own time the job interrupted was lost."""
    from abstractruntime.identity.life import read_resume_to

    from entity_fake_loop import write_status

    with _client() as client:
        proc = own_time.start(client)
        assert read_resume_to(_home_dir())["phase"] == "personal"
        (_home_dir() / "work_order.md").write_text("Sort the archive.\n", encoding="utf-8")
        write_status(_home_dir(), proc.pid, "day", day_kind="work")
        assert client.get("/api/gateway/entities/Castor/life_state").json()["phase"] == "work"
        assert read_resume_to(_home_dir())["phase"] == "personal"
        proc.kill()


def test_a_work_return_never_becomes_own_time_because_a_grant_stands(spawned):
    """Codex review 2 #6: reconcile turned a remembered "work" return into personal merely because
    the grant was armed."""
    from abstractruntime.identity.life import write_resume_to

    from abstractgateway import entity_phase

    with _client() as client:
        _born_awake_with_personal(client)
        write_resume_to(_home_dir(), "work", written_by="loop", fence=_LF.admit(_home_dir()))
        assert entity_phase.reconcile(_registry(), "castor", "observed")["own_time"] is False
        assert spawned == []


# --------------------------------------------------------------------------- a restart is not a Stop
def _restart_pass():
    """The gateway-start pass, as the service factory runs it once per process."""
    from abstractgateway import entity_phase

    entity_phase._RESTART_PASSES.clear()
    return entity_phase.resume_after_restart(_registry())


@pytest.mark.parametrize("left_by", ["stop_command", "crashed"])
def test_a_gateway_restart_resumes_own_time_where_the_grant_is_armed(spawned, own_time, left_by):
    """Operator decision D5 (2026-11-09): a gateway restart is not a Stop. The loop the restart took
    down (a graceful stop before it, or killed with it: the status names a gone process) comes back
    without a Retry while the personal grant stands; a second pass in the same process does
    nothing."""
    with _client() as client:
        proc = own_time.start(client)
        if left_by == "crashed":
            proc.kill()
            proc.wait(5)  # the status still says `between` for a pid that is gone
        else:
            own_time.end(proc, stopped_by="stop_command")
        rows = _restart_pass()
        assert spawned == ["castor"] and spawned.return_to == ["personal"], rows
        assert [r["action"] for r in rows if r["slug"] == "castor"] == ["resumed"]
        from abstractgateway import entity_phase

        assert entity_phase.resume_after_restart(_registry()) == []  # once per process
        assert spawned == ["castor"]


def test_a_restart_never_resumes_a_stopped_home_or_idle_without_a_grant(spawned):
    from abstractruntime.identity.life import write_entity_state, write_personal_grant

    with _client() as client:
        _born_awake_with_personal(client, personal=False)  # idle, no grant
        rows = _restart_pass()
        assert spawned == [] and [r["reason"] for r in rows if r["slug"] == "castor"] == ["no_grant"]
        body = client.get("/api/gateway/entities/Castor/life_state").json()
        assert body["phase"] == "idle" and body["next"] is None
        # Stopped with a grant standing (written behind the kill switch's back): the Stop's word stands.
        write_personal_grant(_home_dir(), mode="until_revoked", granted_by="person:admin", fence=_LF.admit(_home_dir()))
        write_entity_state(_home_dir(), "paused", reason="stop", fence=_LF.admit(_home_dir()))
        rows = _restart_pass()
        assert spawned == [] and [r["reason"] for r in rows if r["slug"] == "castor"] == ["stopped"]
        assert client.get("/api/gateway/entities/Castor/life_state").json()["phase"] == "stopped"


def test_a_visit_standing_at_the_restart_hands_own_time_back_at_its_close(spawned, own_time):
    """The restart finds him in a visit, his loop killed with the gateway: the visit keeps the phase,
    and its close restores own time (not refused as a crash)."""
    with _client() as client:
        proc = own_time.start(client)
        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert opened.status_code == 200, opened.text
        proc.kill()
        proc.wait(5)
        rows = _restart_pass()
        assert spawned == [] and [r["reason"] for r in rows if r["slug"] == "castor"] == ["visit"]
        closed = client.post(f"/api/gateway/entities/Castor/visit/{opened.json()['run_id']}/close",
                             json={"closed_by": "operator"})
        assert closed.status_code == 200, closed.text
        assert spawned == ["castor"]


def test_a_stop_after_the_restart_cancels_its_pending_restore(spawned, own_time):
    """The restart's restore lives under the generation it was made in: after a Stop, a remembered
    own time (written back by anyone) finds the crashed loop refused again, never the restart's
    pass."""
    from abstractgateway import entity_phase

    with _client() as client:
        proc = own_time.start(client)
        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        proc.kill()
        proc.wait(5)
        _restart_pass()
        assert client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "stop"}).status_code == 200
        # Awake again by a path that is not the Resume route (the CLI's restore writes the state).
        from abstractruntime.identity.life import write_entity_state, write_personal_grant

        write_entity_state(_home_dir(), "awake", reason="restored by hand", fence=_LF.admit(_home_dir()))
        write_personal_grant(_home_dir(), mode="until_revoked", granted_by="person:admin", fence=_LF.admit(_home_dir()))
        entity_phase.set_resume_to(_registry(), "castor", "personal")
        (_home_dir() / "loop_status").write_text(json.dumps({"phase": "between", "pid": proc.pid}), encoding="utf-8")
        assert entity_phase.reconcile(_registry(), "castor", "test")["reason"] == "loop_crashed"
        assert spawned == []
        del opened


def test_the_service_start_runs_the_restart_pass(monkeypatch: pytest.MonkeyPatch):
    """The factory wiring: the pass runs at the gateway's start (in its own thread), on the
    service's registry and hosts; it is off with the background sweeps."""
    from abstractgateway import entity_phase, entity_repair

    seen = []
    monkeypatch.setattr(entity_repair, "start_repair_sweeper", lambda registry, **kw: None)
    monkeypatch.setattr(entity_phase, "resume_after_restart",
                        lambda registry, **kw: seen.append((registry, kw)) or [])
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_SELF_REPAIR", "1")
    with _client():
        deadline = time.monotonic() + 5
        while not seen and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(seen) == 1 and seen[0][0] is _registry()
        assert seen[0][1]["chat_host"] is not None and seen[0][1]["visit_host"] is not None
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_SELF_REPAIR", "0")
    assert entity_phase.start_restart_resume(object()) is None


# --------------------------------------------------------------------------- a sleep across visits
def _host_markers(kind: str):
    path = _home_dir().parent / ".host_stream" / "castor.jsonl"
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [r["payload"] for r in rows if (r.get("payload") or {}).get("kind") == kind]


@pytest.mark.parametrize("lane", ["visit", "chat"])
def test_a_sleep_keeps_its_reason_and_since_across_three_visits(spawned, lane):
    """Castor's sleep reason grew by one "(visitor session ended: …)" per visit and its since moved
    to each close. The same sleep continues: after three visits the state's reason is the original
    word for word, `since` (the phase's and the sleep's) is where the sleep began, and the visits
    are in the ledger (a `summon` at each open, a `session_closed` at each end)."""
    from abstractruntime.identity.life import read_entity_state, write_entity_state

    with _client() as client:
        _born_awake_with_personal(client, personal=False)
        home = _home_dir()
        write_entity_state(home, "asleep", reason="by the operator (roster)", written_by="operator", fence=_LF.admit(home))
        before = client.get("/api/gateway/entities/Castor/life_state").json()
        assert before["phase"] == "sleep" and before["sleep"]["reason"] == "by the operator (roster)"
        origin = read_entity_state(home)["changed_at"]
        for _ in range(3):
            time.sleep(0.02)  # every write moves changed_at
            if lane == "visit":
                opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
                assert opened.status_code == 200, opened.text
                assert client.get("/api/gateway/entities/Castor/life_state").json()["phase"] == "visit"
                closed = client.post(f"/api/gateway/entities/Castor/visit/{opened.json()['run_id']}/close",
                                     json={"closed_by": "operator"})
            else:
                opened = client.post("/api/gateway/entities/Castor/chat/open", json={"context_window": 32000})
                assert opened.status_code == 200, opened.text
                assert client.get("/api/gateway/entities/Castor/life_state").json()["phase"] == "visit"
                closed = client.post(f"/api/gateway/entities/Castor/chat/{opened.json()['chat_id']}/close")
            assert closed.status_code == 200, closed.text
            st = read_entity_state(home)
            assert st["state"] == "asleep" and st["reason"] == "by the operator (roster)", st
            assert st["changed_at"] != origin  # the state was rewritten: since comes from the origin
            after = client.get("/api/gateway/entities/Castor/life_state").json()
            assert after["phase"] == "sleep" and after["sleep"]["reason"] == "by the operator (roster)"
            assert after["sleep"]["since"] == before["sleep"]["since"]
            assert after["since"] == before["since"]
        assert len(_host_markers("summon")) == 3
        closes = _host_markers("session_closed")
        assert len(closes) == 3 and all(c.get("sleep_continues") is True for c in closes), closes
        assert spawned == []


def test_an_aborted_open_keeps_the_sleep_as_it_was(spawned):
    from abstractgateway.entity_visits import _restore_prior_state
    from abstractruntime.identity.life import read_entity_state, write_entity_state

    with _client() as client:
        _born_awake_with_personal(client, personal=False)
        home = _home_dir()
        write_entity_state(home, "asleep", reason="resting", written_by="operator", wake_at="2099-01-01T00:00:00+00:00", fence=_LF.admit(home))
        before = client.get("/api/gateway/entities/Castor/life_state").json()
        prior = dict(read_entity_state(home))
        write_entity_state(home, "asleep", reason="in conversation with person:admin [visit v-1]", mode="visiting",
                           written_by="visit-door", fence=_LF.admit(home))
        _restore_prior_state(home, prior, suffix="(visit open failed)", token="[visit v-1]", fence=_LF.admit(home))
        st = read_entity_state(home)
        assert (st["state"], st["reason"], st.get("wake_at")) == ("asleep", "resting", "2099-01-01T00:00:00+00:00")
        after = client.get("/api/gateway/entities/Castor/life_state").json()
        assert after["sleep"]["since"] == before["sleep"]["since"]


def _host_kinds():
    path = _home_dir().parent / ".host_stream" / "castor.jsonl"
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [(r.get("payload") or {}).get("kind") for r in rows]


@pytest.mark.parametrize("lane,writer", [("visit", "operator"), ("chat", "operator"), ("visit", "self")])
def test_a_visit_closing_on_a_sleep_adds_only_the_visit_rows(spawned, lane, writer):
    """castor and nerin carried "sleep -> sleep" phase_changed rows: each close wrote the sleep back
    changed (reason, since, mode), so the snapshot moved without a phase change. The same sleep
    continues unchanged: a visit nobody watched adds its open and close rows and nothing else (no
    phase_changed, the contract has no sleep -> sleep edge), and the state's reason, mode, writer and
    since are the ones from before it."""
    from abstractruntime.identity.life import read_entity_state, write_entity_state

    procs = []
    with _client() as client:
        _born_awake_with_personal(client, personal=False)
        home = _home_dir()
        if writer == "self":  # his own nap after a dream: the loop is alive and owns it
            from entity_fake_loop import spawn

            procs.append(spawn(home, "between", 60))
            write_entity_state(home, "asleep", reason="self-elected sleep: tired", written_by="self",
                               mode="resting", wake_at="2099-01-01T00:00:00+00:00", fence=_LF.admit(home))
        else:
            write_entity_state(home, "asleep", reason="by the operator (roster)", written_by="operator", fence=_LF.admit(home))
        before = client.get("/api/gateway/entities/Castor/life_state").json()
        assert before["phase"] == "sleep"
        st0 = read_entity_state(home)
        kinds0 = _host_kinds()
        if lane == "visit":
            opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
            assert opened.status_code == 200, opened.text
            closed = client.post(f"/api/gateway/entities/Castor/visit/{opened.json()['run_id']}/close",
                                 json={"closed_by": "operator"})
        else:
            opened = client.post("/api/gateway/entities/Castor/chat/open", json={"context_window": 32000})
            assert opened.status_code == 200, opened.text
            closed = client.post(f"/api/gateway/entities/Castor/chat/{opened.json()['chat_id']}/close")
        assert closed.status_code == 200, closed.text
        after = client.get("/api/gateway/entities/Castor/life_state").json()
        added = _host_kinds()[len(kinds0):]
        assert sorted(added) == ["session_closed", "summon"], added
        assert after["rev"] == before["rev"] and after["since"] == before["since"]
        assert after["sleep"] == before["sleep"]
        st = read_entity_state(home)
        assert {k: st.get(k) for k in ("state", "reason", "mode", "written_by", "wake_at")} == {
            k: st0.get(k) for k in ("state", "reason", "mode", "written_by", "wake_at")}
        assert not [m for m in _host_markers("phase_changed") if m.get("from") == m.get("to") == "sleep"]
    for p in procs:
        p.kill()


def test_a_restore_onto_the_same_sleep_writes_nothing(spawned):
    from abstractgateway import entity_phase
    from abstractruntime.identity.life import read_entity_state, write_entity_state

    with _client() as client:
        _born_awake_with_personal(client, personal=False)
        home = _home_dir()
        write_entity_state(home, "asleep", reason="resting", written_by="operator", fence=_LF.admit(home))
        st0 = read_entity_state(home)
        lines0 = (home / "state_history.jsonl").read_text(encoding="utf-8").count("\n")
        entity_phase.continue_sleep(home.parent, "castor", dict(st0), fence=_LF.admit(home))
        assert read_entity_state(home)["changed_at"] == st0["changed_at"]
        assert (home / "state_history.jsonl").read_text(encoding="utf-8").count("\n") == lines0
