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


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch):
    from abstractgateway import entity_phase

    calls = []
    lock = threading.Lock()

    def _fake_start(registry, manifest, params):
        time.sleep(0.2)  # a real spawn takes a moment: concurrent reconciles overlap here
        with lock:
            calls.append(manifest.slug)
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
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        threading.Thread(target=proc.wait, daemon=True).start()
        procs.append(proc)
        (_home_dir() / "loop_status").write_text(json.dumps({"phase": "between", "pid": proc.pid}), encoding="utf-8")
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
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            threading.Thread(target=proc.wait, daemon=True).start()
            procs.append(proc)
            (home / "loop_status").write_text(json.dumps({"phase": "between", "pid": proc.pid}), encoding="utf-8")
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
    from abstractgateway import entity_loop, entity_phase

    frozen = []

    def _start_then_stop(registry, manifest, params):
        with entity_phase.phase_lock(registry.entities_dir, "castor"):
            rec = entity_phase.read_record(registry.entities_dir, "castor")
            rec["generation"] += 1  # Stop's fence lands mid-spawn
            entity_phase.write_record(registry.entities_dir, "castor", rec)
        return {"pid": 4321}

    monkeypatch.setattr(entity_phase, "start_own_time", _start_then_stop)
    monkeypatch.setattr(entity_loop, "freeze_loop", lambda home, **kw: frozen.append(home.name) or {})
    with _client() as client:
        _born_awake_with_personal(client)
        out = entity_phase.reconcile(_registry(), "castor", "test", operator=True)
        assert out == {"own_time": False, "pid": None, "reason": "stopped"}
        assert frozen == ["castor"]


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
