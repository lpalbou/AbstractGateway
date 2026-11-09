"""Own time is the operator's to give (operator ruling 2026-11-08; clarification 2026-11-09: "own time
can only be given and removed by the operator ... if granted, when there are transitions to other
states, it can go back to that state"; root untracked/entity-phases-2026-11-08/CONTRACT-phases.md v2
§4). Reconcile RESTORES the phase an interruption took over (`resume_to`) — after a visit or chat
closes (normal or idle), a spontaneous sleep ends, a request ends, the work order is cleared, an
operator sleep's maintenance is done — only while its grant is still on; never under a visit, a
sleep, work or the kill switch; never twice; never from idle by itself, and never after a crash.
An interrupted sleep continues as the same sleep (§4.4). Hermetic gateway; the spawner is faked."""

from __future__ import annotations

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
        write_personal_grant(_home_dir(), mode="until_revoked", granted_by="person:admin")
    write_entity_state(_home_dir(), "awake", reason="woken")


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

        write_personal_grant(_home_dir(), mode="until_revoked", granted_by="person:admin")
        again = client.post("/api/gateway/entities/Castor/visit/open", json={})
        client.post(f"/api/gateway/entities/Castor/visit/{again.json()['run_id']}/close", json={"closed_by": "operator"})
        assert spawned == []


def test_a_spontaneous_sleep_returns_to_own_time_when_it_ends(spawned, own_time):
    from abstractruntime.identity.life import write_entity_state

    with _client() as client:
        proc = own_time.start(client)
        write_entity_state(_home_dir(), "asleep", reason="self-elected sleep: tired", written_by="self")
        assert client.get("/api/gateway/entities/Castor/life_state").json()["phase"] == "sleep"
        own_time.end(proc)
        write_entity_state(_home_dir(), "awake", reason="woke", written_by="self")
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
        write_entity_state(home, "asleep", reason="resting", written_by=writer, wake_at="2099-01-01T00:00:00+00:00")
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
                           mode="visiting", written_by="visit-door")
        assert entity_phase.reconcile(_registry(), "castor", "test")["reason"] == "visit"
        write_entity_state(_home_dir(), "paused", reason="stop")
        assert entity_phase.reconcile(_registry(), "castor", "test")["reason"] == "stopped"
        assert spawned == []


def test_a_stop_landing_during_the_spawn_kills_the_new_loop(monkeypatch: pytest.MonkeyPatch):
    from abstractgateway import entity_phase

    procs = []

    def _start_then_stop(registry, manifest, params, return_to=None):
        from entity_fake_loop import spawn

        proc = spawn(_home_dir(), "day", 60)
        procs.append(proc)
        with entity_phase.phase_lock(registry.entities_dir, "castor"):
            rec = entity_phase.read_record(registry.entities_dir, "castor")
            rec["generation"] += 1  # Stop's fence lands mid-spawn
            entity_phase.write_record(registry.entities_dir, "castor", rec)
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
                           mode="visiting", written_by="visit-door")
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
        write_entity_state(_home_dir(), "asleep", reason="operator sleep restored (visit ended)", written_by="operator")
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
                           wake_at="2099-01-01T00:00:00+00:00")
        ls = client.get("/api/gateway/entities/Castor/life_state").json()
        assert ls["phase"] == "sleep" and ls["next"]["phase"] == "personal"
        assert ls["next"]["sentence"] == "When he wakes and his own time resumes."


def test_next_during_a_visit_that_interrupted_a_sleep_is_the_sleep(spawned):
    from abstractruntime.identity.life import write_entity_state

    with _client() as client:
        _born_awake_with_personal(client)
        write_entity_state(_home_dir(), "asleep", reason="operator sleep")
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
        with entity_phase.phase_lock(reg.entities_dir, "castor"):
            rec = entity_phase.read_record(reg.entities_dir, "castor")
            rec.pop("spawning", None)
            entity_phase.write_record(reg.entities_dir, "castor", rec)
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
        write_entity_state(_home_dir(), "asleep", reason="own time ended", written_by="loop")
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
        write_entity_state(_home_dir(), "asleep", reason="self-elected sleep: tired", written_by="self")
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
        write_resume_to(_home_dir(), None, written_by="loop")  # the bucket exists (the runtime wrote it)
        entity_phase.set_resume_to(reg, "castor", "personal")
        assert read_resume_to(_home_dir())["phase"] == "personal"  # the gateway's word reaches the bucket
        write_resume_to(_home_dir(), None, written_by="loop")  # the loop ended idle
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
