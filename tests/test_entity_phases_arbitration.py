"""One admission arbitration per home (Codex review 2026-11-09 #1, #7, #8, #9): every visit-door
and maintenance state write is checked and written in ONE step under the lifecycle lock, against the
generation the writer saw at its entry — a Stop (or a Stop then a Resume) landing between a check
and a write is never overwritten; a visit taking the phase invalidates an own-time spawn in
progress; a sleep never tears down a visit admitted during its maintenance pass; a failed pass
completes with a receipt and a stale completion never touches a newer pass. Hermetic gateway."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractruntime.identity.visit_workflow")

_TOKEN = "entity-arbitration-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import entity_chat

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setattr(entity_chat, "gateway_text_mind",
                        lambda: {"provider": "lmstudio", "model": "test-model", "base_url": None, "reasoning": None})

    class _LLM:
        def generate(self, **kw):
            return type("R", (), {"content": "Fine."})()

    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: _LLM())


def _spark(name: str = "Castor") -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _home():
    from pathlib import Path

    from abstractgateway.service import get_gateway_service

    return Path(get_gateway_service().config.data_dir) / "entities" / "castor"


def _registry():
    from abstractgateway.routes.entities import _registry as reg

    return reg()


def _stop_in_a_thread():
    from abstractgateway import entity_phase
    from abstractgateway.routes.entities import _chat_host, _visit_host

    reg, ch, vh = _registry(), _chat_host(), _visit_host()
    t = threading.Thread(target=lambda: entity_phase.stop(reg, "castor", actor="person:admin", chat_host=ch, visit_host=vh),
                         daemon=True)
    t.start()
    return t


def test_the_visit_wait_ends_on_a_stop_then_resume_and_never_writes_over_a_stop():
    from abstractruntime.identity.life import read_entity_state, write_entity_state

    from abstractgateway import entity_phase
    from abstractgateway.entity_visits import VisitRefused, hold_visiting_posture

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        home = _home()
        gen = entity_phase.generation_of(home.parent, "castor")
        poll = hold_visiting_posture(home, reason="in conversation [visit v-1]", token="[visit v-1]", generation=gen)
        # A Stop then a Resume while the door waited: awake again, but the wait must still end.
        assert client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "stop"}).status_code == 200
        assert client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "resume"}).status_code == 200
        write_entity_state(home, "awake", reason="resumed")
        with pytest.raises(VisitRefused) as e:
            poll(0)
        assert e.value.code == "stopped_use_resume"
        assert "visiting" != read_entity_state(home).get("mode")


def test_a_visit_restore_never_overwrites_a_stop_landing_mid_restore(monkeypatch: pytest.MonkeyPatch):
    import abstractruntime.identity.life as life

    from abstractgateway.entity_visits import _restore_prior_state

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        home = _home()
        life.write_entity_state(home, "asleep", reason="in conversation [visit v-2]", mode="visiting", written_by="visit-door")
        real_read = life.read_entity_state
        fired = []

        def _read_with_a_stop(home_dir):
            st = real_read(home_dir)
            if not fired:
                fired.append(_stop_in_a_thread())
                time.sleep(0.4)  # the Stop runs now, unless the lock makes it wait
            return st

        monkeypatch.setattr(life, "read_entity_state", _read_with_a_stop)
        _restore_prior_state(home, {"state": "awake"}, suffix="(aborted)", token="[visit v-2]")
        monkeypatch.setattr(life, "read_entity_state", real_read)
        fired[0].join(15)
        assert real_read(home)["state"] == "paused", "the restore wrote over the Stop"


def test_a_maintenance_write_never_overwrites_a_stop_landing_after_its_check(monkeypatch: pytest.MonkeyPatch):
    from abstractruntime.identity.life import read_entity_state, write_entity_state

    from abstractgateway import entity_phase

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        home = _home()
        write_entity_state(home, "asleep", reason="maintenance")
        gen = entity_phase.maintenance_started(_registry(), "castor")
        real_check = entity_phase.maintenance_may_write
        fired = []

        def _check_then_stop(registry, slug, generation):
            ok = real_check(registry, slug, generation)
            if not fired:
                fired.append(_stop_in_a_thread())
                time.sleep(0.4)
            return ok

        monkeypatch.setattr(entity_phase, "maintenance_may_write", _check_then_stop)
        entity_phase.maintenance_write_if_owner(_registry(), "castor", gen,
                                                lambda: write_entity_state(home, "asleep", reason="maintenance done"))
        fired[0].join(15)
        assert read_entity_state(home)["state"] == "paused"


def test_a_visit_taking_the_phase_during_a_spawn_wins(monkeypatch: pytest.MonkeyPatch):
    from abstractruntime.identity.life import write_entity_state, write_personal_grant

    from abstractgateway import entity_phase

    procs = []

    def _start_while_a_visit_opens(registry, manifest, params, return_to=None):
        from entity_fake_loop import spawn

        proc = spawn(_home(), "day", 60)
        procs.append(proc)
        gen = entity_phase.generation_of(registry.entities_dir, "castor")
        entity_phase.admit_visit_posture(registry.entities_dir, "castor", gen, reason="in conversation [visit v-3]")
        return {"pid": proc.pid}

    monkeypatch.setattr(entity_phase, "start_own_time", _start_while_a_visit_opens)
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        write_entity_state(_home(), "awake", reason="woken")
        write_personal_grant(_home(), mode="until_revoked", granted_by="person:admin")
        rows = []
        real_kill = entity_phase._kill_loop
        monkeypatch.setattr(entity_phase, "_kill_loop", lambda *a, **k: rows.append(real_kill(*a, **k)) or rows[-1])
        out = entity_phase.reconcile(_registry(), "castor", "test", operator=True)
        assert out["own_time"] is False and out["reason"] == "visit"
        deadline = time.monotonic() + 6
        while procs[0].poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert procs[0].poll() is not None


def test_a_visit_opening_during_the_maintenance_pass_is_never_torn_down(monkeypatch: pytest.MonkeyPatch):
    import abstractmemory
    from abstractruntime.identity.life import read_entity_state, write_entity_state

    from abstractgateway.routes.entities import _visit_host

    opened: dict = {}

    def _pass_while_a_visitor_comes(*args, **kwargs):
        host = _visit_host()
        t = threading.Thread(target=lambda: opened.update(host.open("castor", participants=["person:admin"])), daemon=True)
        t.start()
        opened["thread"] = t
        deadline = time.monotonic() + 10
        while read_entity_state(_home()).get("mode") != "visiting" and time.monotonic() < deadline:
            time.sleep(0.05)
        return {"ok": True}

    monkeypatch.setattr(abstractmemory, "sleep_pass", _pass_while_a_visitor_comes)
    from abstractgateway.entities import EntityRegistry

    real_set_state = EntityRegistry.set_state

    def _set_state_then_let_the_visit_in(self, **kw):
        out = real_set_state(self, **kw)
        # The pass returned and released the home: the visitor's open completes BEFORE the sleep
        # request finishes (the window the old post-pass teardown closed visits in).
        if "thread" in opened:
            opened["thread"].join(20)
        return out

    monkeypatch.setattr(EntityRegistry, "set_state", _set_state_then_let_the_visit_in)
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        write_entity_state(_home(), "awake", reason="woken")
        r = client.post("/api/gateway/entities/Castor/state", json={"state": "sleep", "dream": True})
        assert r.status_code == 200, r.text
        assert r.json()["maintenance"] == "after_visit"
        opened["thread"].join(20)
        assert opened.get("run_id"), opened
        assert client.get("/api/gateway/entities/Castor/visit").json().get("open") is True


def test_a_failed_pass_completes_with_a_receipt_and_a_stale_completion_touches_nothing(monkeypatch: pytest.MonkeyPatch):
    import abstractmemory
    from abstractruntime.identity.life import read_entity_state, write_entity_state

    from abstractgateway import entity_phase

    def _boom(*args, **kwargs):
        raise RuntimeError("the memory store is locked")

    monkeypatch.setattr(abstractmemory, "sleep_pass", _boom)
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        write_entity_state(_home(), "awake", reason="woken")
        r = client.post("/api/gateway/entities/Castor/state", json={"state": "sleep", "dream": True})
        assert r.status_code == 200, r.text
        assert r.json()["maintenance"] == "woke"
        assert read_entity_state(_home())["state"] == "awake"
        rec = entity_phase.read_record(_home().parent, "castor")
        assert rec["last_maintenance"]["status"] == "failed" and "locked" in rec["last_maintenance"]["error"]
        assert rec["maintenance"] is None

        # A stale completion (generation 1) while a newer pass (generation 2) runs: untouched.
        with entity_phase.phase_lock(_home().parent, "castor"):
            rec = entity_phase.read_record(_home().parent, "castor")
            rec["generation"] = 2
            rec["maintenance"] = {"status": "running", "generation": 2}
            entity_phase.write_record(_home().parent, "castor", rec)
        assert entity_phase.maintenance_done(_registry(), "castor", 1) == "stopped"
        assert entity_phase.read_record(_home().parent, "castor")["maintenance"] == {"status": "running", "generation": 2}


@pytest.mark.parametrize("door", ["visit", "chat"])
def test_a_visit_waits_for_a_live_summon_instead_of_refusing(door):
    """Codex review #6: a live summon answered 409 summon_seat_live; the door now waits (no deadline)
    for the summon's turn to end, then opens."""
    from abstractgateway.routes.entities import _chat_host, _visit_host

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        until = time.monotonic() + 0.8
        probe = lambda slug: {"run_id": "summon-1", "session_id": "s", "holder": "agent:x"} if time.monotonic() < until else None
        _visit_host()._summon_seat_probe = probe
        _chat_host()._summon_seat_probe = probe
        path, body = ("visit/open", {}) if door == "visit" else ("chat/open", {"context_window": 32000})
        t0 = time.monotonic()
        r = client.post(f"/api/gateway/entities/Castor/{path}", json=body)
        assert r.status_code == 200, r.text
        assert time.monotonic() - t0 >= 0.5  # it waited for the summon's turn


@pytest.mark.parametrize("door", ["visit", "chat"])
def test_a_visit_waits_for_another_opening_instead_of_retry_shortly(door):
    from abstractruntime.identity.life import write_entity_state

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        write_entity_state(_home(), "asleep", reason="in conversation with person:bob [visit v-other]",
                           mode="visiting", written_by="visit-door")

        def _the_other_open_gives_up():
            time.sleep(0.8)
            write_entity_state(_home(), "awake", reason="the other open was aborted")

        threading.Thread(target=_the_other_open_gives_up, daemon=True).start()
        path, body = ("visit/open", {}) if door == "visit" else ("chat/open", {"context_window": 32000})
        r = client.post(f"/api/gateway/entities/Castor/{path}", json=body)
        assert r.status_code == 200, r.text


def test_the_same_visitor_opening_again_joins_their_visit():
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        first = client.post("/api/gateway/entities/Castor/visit/open", json={})
        second = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert first.status_code == 200 and second.status_code == 200, second.text
        assert second.json()["run_id"] == first.json()["run_id"] and second.json()["joined"] is True
