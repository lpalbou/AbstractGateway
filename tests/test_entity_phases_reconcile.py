"""The gateway owns own time (operator ruling 2026-11-08; root untracked/entity-phases-2026-11-08/
CONTRACT-phases.md v2 §4). Reconcile starts own time when `personal` is on and nothing else holds
the phase — after a visit or chat closes (normal or idle), a summon ends, the work order is cleared,
a graceful loop stop completes, an operator sleep's maintenance is done — never under a visit, a
sleep, work or the kill switch, and never twice. A visit's close returns to the previous phase
(§4.4): an interrupted sleep continues as the same sleep. Hermetic gateway; the spawner is faked."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
import time

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


@pytest.mark.parametrize("lane", ["visit", "chat"])
def test_a_visit_close_restarts_own_time_when_personal_is_on(spawned, lane):
    with _client() as client:
        _born_awake_with_personal(client)
        if lane == "visit":
            opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
            assert opened.status_code == 200, opened.text
            assert spawned == []  # never under a visit
            closed = client.post(f"/api/gateway/entities/Castor/visit/{opened.json()['run_id']}/close",
                                 json={"closed_by": "operator"})
        else:
            opened = client.post("/api/gateway/entities/Castor/chat/open", json={"context_window": 32000})
            assert opened.status_code == 200, opened.text
            closed = client.post(f"/api/gateway/entities/Castor/chat/{opened.json()['chat_id']}/close")
        assert closed.status_code == 200, closed.text
        assert spawned == ["castor"]


def test_the_idle_reaper_close_restarts_own_time_too(spawned):
    from abstractgateway.routes.entities import _visit_host

    with _client() as client:
        _born_awake_with_personal(client)
        host = _visit_host()
        host._idle_timeout_s = 0.05
        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert opened.status_code == 200, opened.text
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
        ts = [threading.Thread(target=entity_phase.reconcile, args=(reg, "castor", "test")) for _ in range(4)]
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
        out = entity_phase.reconcile(_registry(), "castor", "test")
        assert out == {"own_time": False, "pid": None, "reason": "stopped"}
        assert frozen == ["castor"]


def test_a_summon_ending_the_work_order_cleared_and_a_graceful_stop_hand_back_own_time(spawned):
    from types import SimpleNamespace

    from abstractgateway import entity_phase

    with _client() as client:
        _born_awake_with_personal(client)
        run = SimpleNamespace(run_id="r1", status="completed",
                              vars={"_runtime": {"entity": {"entity_id": "entity:castor", "channel": "workplace"}}})
        entity_phase.note_run_terminal(run)
        deadline = time.monotonic() + 5
        while not spawned and time.monotonic() < deadline:
            time.sleep(0.05)
        assert spawned == ["castor"]

    spawned.clear()
    with _client() as client:
        from abstractruntime.identity.life import write_entity_state

        write_entity_state(_home_dir(), "awake", reason="reset")
        (_home_dir() / "work_order.md").write_text("Sort the archive.\n", encoding="utf-8")
        cleared = client.put("/api/gateway/entities/Castor/work-order", json={"clear": True})
        assert cleared.status_code == 200, cleared.text
        assert spawned == ["castor"]

    spawned.clear()
    with _client() as client:
        home = _home_dir()
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        threading.Thread(target=proc.wait, daemon=True).start()
        (home / "loop_status").write_text(json.dumps({"phase": "between", "pid": proc.pid}), encoding="utf-8")
        assert client.get("/api/gateway/entities/Castor/life_state").json()["own_time_running"] is True
        proc.kill()
        proc.wait(5)
        (home / "loop_status").write_text(json.dumps({"phase": "stopped", "pid": proc.pid, "stopped_by": "stop_command"}),
                                          encoding="utf-8")
        client.get("/api/gateway/entities/Castor/life_state")
        assert spawned == ["castor"]


def test_an_operator_sleep_wakes_when_maintenance_is_done(spawned):
    from abstractruntime.identity.life import read_entity_state, read_personal_grant

    with _client() as client:
        _born_awake_with_personal(client)
        r = client.post("/api/gateway/entities/Castor/state", json={"state": "sleep", "dream": True})
        assert r.status_code == 200, r.text
        assert r.json()["maintenance"] == "woke"
        assert read_entity_state(_home_dir())["state"] == "awake"
        assert read_personal_grant(_home_dir())["mode"] == "until_revoked"  # sleep never turns personal off
        assert spawned == ["castor"]


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
        _born_awake_with_personal(client)
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
