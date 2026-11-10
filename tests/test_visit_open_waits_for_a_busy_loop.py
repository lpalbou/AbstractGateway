"""A visit never fails because the entity is busy (operator 2026-11-09, aletheia
incident; root ADR-0027): both visit doors wait for the own-time loop to yield
with no time limit — the old 55 s bound rolled the state back and answered
409 `loop_busy` while a personal tick was still running — and the reaper
never repairs a visiting posture while the loop has not yet yielded its day
(a door may still be waiting on it).
"""

from __future__ import annotations

from abstractruntime.identity.lifecycle_fence import LifecycleFence as _LF
import copy
import json
import os
import threading
import time as _real_time

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractruntime.identity.visit_workflow")

_TOKEN = "visit-busy-loop-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setattr(
        "abstractgateway.entity_chat.gateway_text_mind",
        lambda: {"provider": "lmstudio", "model": "test-model", "base_url": None, "reasoning": None},
    )


class _ScriptedLLM:
    class _Reply:
        def __init__(self, content: str) -> None:
            self.content = content

    def __init__(self, replies) -> None:
        self._replies = list(replies)

    def generate(self, **kwargs):
        if not self._replies:
            raise AssertionError("scripted LLM exhausted")
        return self._Reply(self._replies.pop(0))


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


def _loop_in_a_day(home_dir) -> None:
    """A live own-time loop mid-day: this process's pid, phase day."""
    (home_dir / "loop_status").write_text(json.dumps({"phase": "day", "pid": os.getpid()}), encoding="utf-8")


class _ClockPast55s:
    """life.time with a monotonic clock that jumps 100 s per read: any bounded
    wait expires on its first check (the old 55 s door refused here)."""

    def __init__(self) -> None:
        self._t = _real_time.monotonic()

    def monotonic(self) -> float:
        self._t += 100.0
        return self._t

    def __getattr__(self, name: str):
        return getattr(_real_time, name)


def _yield_soon(home_dir) -> threading.Thread:
    def _go() -> None:
        _real_time.sleep(0.3)
        (home_dir / "loop_status").write_text(json.dumps({"phase": "between", "pid": os.getpid()}), encoding="utf-8")

    t = threading.Thread(target=_go, daemon=True)
    t.start()
    return t


@pytest.mark.parametrize("door", ["visit", "chat"])
def test_open_waits_past_any_bound_until_the_loop_yields(monkeypatch: pytest.MonkeyPatch, door: str):
    from abstractgateway import entity_chat
    from abstractruntime.identity import life
    from abstractruntime.identity.life import read_entity_state

    llm = _ScriptedLLM(["(unused)"])
    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: llm)
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        home_dir = _home_dir()
        _loop_in_a_day(home_dir)
        monkeypatch.setattr(life, "time", _ClockPast55s())
        yielder = _yield_soon(home_dir)

        if door == "visit":
            opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        else:
            opened = client.post("/api/gateway/entities/Castor/chat/open", json={"context_window": 32000})
        yielder.join()

        assert opened.status_code == 200, opened.text
        assert opened.json()["yielded_loop"] is True
        state = read_entity_state(home_dir)
        assert state["state"] == "asleep" and state.get("mode") == "visiting"
        assert "aborted" not in str(state.get("reason") or "")


def test_reaper_never_repairs_a_posture_while_the_loop_is_still_in_its_day(monkeypatch: pytest.MonkeyPatch):
    from abstractruntime.identity.life import read_entity_state, write_entity_state

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        from abstractgateway.routes.entities import _visit_host

        host = _visit_host()
        home_dir = _home_dir()
        write_entity_state(
            home_dir, "asleep", reason="in conversation with person:admin [visit v-1] (auto-yield)",
            mode="visiting", written_by="visit-door", fence=_LF.admit(home_dir)
        )
        host._chat_probe = lambda slug: False
        monkeypatch.setattr(type(host), "STALE_YIELD_GRACE_S", 0.0, raising=True)

        # The door is still waiting on a loop mid-tick: the posture stands.
        _loop_in_a_day(home_dir)
        assert host.reap_now() == 0
        assert read_entity_state(home_dir).get("state") == "asleep"

        # The loop has yielded and nothing holds the posture: an orphan, repaired.
        (home_dir / "loop_status").write_text(json.dumps({"phase": "between", "pid": os.getpid()}), encoding="utf-8")
        assert host.reap_now() == 1
        assert read_entity_state(home_dir).get("state") == "awake"


def test_reaper_never_repairs_a_posture_while_a_visit_is_opening(monkeypatch: pytest.MonkeyPatch):
    """The door holds the home's open lock from its posture write until the
    visit is registered: the loop may already be between days, and the
    reaper must still leave the posture to the door."""
    from abstractruntime.identity.life import read_entity_state, write_entity_state

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        from abstractgateway.routes.entities import _visit_host

        host = _visit_host()
        home_dir = _home_dir()
        write_entity_state(
            home_dir, "asleep", reason="in conversation with person:admin [visit v-1] (auto-yield)",
            mode="visiting", written_by="visit-door", fence=_LF.admit(home_dir)
        )
        (home_dir / "loop_status").write_text(json.dumps({"phase": "between", "pid": os.getpid()}), encoding="utf-8")
        host._chat_probe = lambda slug: False
        monkeypatch.setattr(type(host), "STALE_YIELD_GRACE_S", 0.0, raising=True)

        opening = host._slug_lock("castor")
        opening.acquire()
        try:
            assert host.reap_now() == 0
            assert read_entity_state(home_dir).get("state") == "asleep"
        finally:
            opening.release()
        assert host.reap_now() == 1


def test_the_door_keeps_its_posture_while_it_waits(monkeypatch: pytest.MonkeyPatch):
    """Something replaces the visiting posture while the door waits (an operator
    wake click during "Opening…"): the door writes it back, so the loop still
    yields to the visit and the visit opens on its own posture."""
    from abstractgateway import entity_chat
    from abstractruntime.identity.life import read_entity_state, write_entity_state

    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: _ScriptedLLM(["(unused)"]))
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        home_dir = _home_dir()
        _loop_in_a_day(home_dir)

        def _wake_then_yield() -> None:
            _real_time.sleep(0.5)
            write_entity_state(home_dir, "awake", reason="by the operator (web controls)", written_by="operator", fence=_LF.admit(home_dir))
            _real_time.sleep(2.5)  # past the door's first poll
            (home_dir / "loop_status").write_text(json.dumps({"phase": "between", "pid": os.getpid()}), encoding="utf-8")

        t = threading.Thread(target=_wake_then_yield, daemon=True)
        t.start()
        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        t.join()
        assert opened.status_code == 200, opened.text
        state = read_entity_state(home_dir)
        assert state["state"] == "asleep" and state.get("mode") == "visiting"
        assert f"[visit {opened.json()['visit_id']}]" in str(state.get("reason") or "")


def test_the_reaper_never_waits_on_a_held_home(monkeypatch: pytest.MonkeyPatch):
    """The one reaper thread sweeps every home: a due visit whose home is held
    by another writer is skipped this sweep (retried next), never waited on."""
    from abstractgateway import entity_chat
    from abstractruntime.storage.lease import acquire_directory_lease

    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: _ScriptedLLM(["Hello.", "Reflection."]))
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        run_id = client.post("/api/gateway/entities/Castor/visit/open", json={}).json()["run_id"]

        from abstractgateway.routes.entities import _visit_host
        from abstractgateway.service import get_gateway_service

        host = _visit_host()
        er = get_gateway_service().entity_registry.get_entity_runtime("castor")
        run = er.run_store.load(run_id)
        run.waiting.until = "2020-01-01T00:00:00+00:00"
        er.run_store.save(run)

        incumbent = acquire_directory_lease(_home_dir(), holder="maintenance")
        try:
            sweep = threading.Thread(target=host.reap_now, daemon=True)
            sweep.start()
            sweep.join(5.0)
            assert not sweep.is_alive(), "the reaper must not wait on a held home"
        finally:
            incumbent.release()
        assert host.reap_now() >= 1  # the next sweep drives the due visit
