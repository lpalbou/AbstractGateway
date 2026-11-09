"""The old lifecycle routes meanwhile (operator ruling 2026-11-08; root untracked/entity-phases-2026-11-08/
CONTRACT-phases.md v2 §6.3): each keeps working, mapped onto the phase model, and the deprecated ones
name their successor (`Deprecation: true` + a successor `Link`). `state paused` and `loop/stop freeze`
ARE the kill switch; `restore` IS resume; a graceful `loop/stop` also turns personal off (else the
gateway would restart own time); `loop/start` and `PUT /personal-grant` are personal on; the operator
sleep refuses during a visit (`in_visit`) and after a sleep (`already_asleep`). Hermetic gateway."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import threading

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractruntime.identity.visit_workflow")

_TOKEN = "entity-old-routes-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import entity_chat

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setattr(entity_chat, "gateway_text_mind",
                        lambda: {"provider": "lmstudio", "model": "test-model", "base_url": None, "reasoning": None})


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch):
    from abstractgateway import entity_phase

    calls = []

    def _fake_start(registry, manifest, params):
        calls.append(dict(params))
        return {"pid": 9100 + len(calls)}

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


def _born(client):
    assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201


def _deprecated(r, successor: str) -> None:
    assert r.headers.get("Deprecation") == "true", r.headers
    assert successor in (r.headers.get("Link") or "") and 'rel="successor-version"' in r.headers["Link"]


def test_state_paused_is_the_kill_switch_and_restore_is_resume(spawned):
    from abstractruntime.identity.life import read_entity_state, write_personal_grant

    with _client() as client:
        _born(client)
        from abstractruntime.identity.life import write_entity_state

        write_entity_state(_home_dir(), "awake", reason="woken")  # own time runs (not the newborn's sleep)
        write_personal_grant(_home_dir(), mode="until_revoked", granted_by="person:admin")
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "-m", "abstractruntime.identity.life"])  # argv names the loop module
        threading.Thread(target=proc.wait, daemon=True).start()
        (_home_dir() / "loop_status").write_text(json.dumps({"phase": "between", "pid": proc.pid}), encoding="utf-8")
        r = client.post("/api/gateway/entities/Castor/state", json={"state": "paused"})
        assert r.status_code == 200, r.text
        _deprecated(r, "/lifecycle")
        assert r.json()["action"] == "stop" and r.json()["phase"] == "stopped"
        assert {i["item"]: i for i in r.json()["items"]}["loop"]["outcome"] in ("killed_sigterm", "killed_sigkill")
        proc.wait(5)

        back = client.post("/api/gateway/entities/Castor/state", json={"state": "restore"})
        assert back.status_code == 200, back.text
        _deprecated(back, "/lifecycle")
        assert back.json()["action"] == "resume"
        assert read_entity_state(_home_dir())["state"] == "awake"
        assert len(spawned) == 1


def test_the_operator_sleep_refuses_during_a_visit_and_after_a_sleep(monkeypatch: pytest.MonkeyPatch):
    from abstractruntime.identity.life import write_entity_state

    from abstractgateway import entity_chat

    class _LLM:
        def generate(self, **kw):
            return type("R", (), {"content": "Hi."})()

    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: _LLM())
    with _client() as client:
        _born(client)
        # A newborn's operator sleep takes the request as the SAME sleep (not a chained one).
        assert client.post("/api/gateway/entities/Castor/state", json={"state": "sleep"}).status_code == 200
        # A sleep whose maintenance is running cannot be followed by another sleep.
        write_entity_state(_home_dir(), "asleep", reason="maintenance", mode="dreaming")
        asleep = client.post("/api/gateway/entities/Castor/state", json={"state": "sleep"})
        assert asleep.status_code == 409
        assert asleep.json()["code"] == "already_asleep"
        assert asleep.json()["detail"]["message"] == "He is already resting; a sleep cannot follow a sleep."

        write_entity_state(_home_dir(), "awake", reason="woken")
        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert opened.status_code == 200, opened.text
        r = client.post("/api/gateway/entities/Castor/state", json={"state": "sleep"})
        assert r.status_code == 409 and r.json()["code"] == "in_visit"
        assert r.json()["detail"]["message"] == "He is in a visit; end it to let him sleep."
        # The visit is never ended implicitly.
        assert client.get("/api/gateway/entities/Castor/visit").json()["open"] is True


def test_loop_start_is_personal_on_and_never_starts_under_a_visit(spawned):
    from abstractruntime.identity.life import read_personal_grant, write_entity_state

    with _client() as client:
        _born(client)
        write_entity_state(_home_dir(), "asleep", reason="in conversation with person:admin [visit v-5]",
                           mode="visiting", written_by="visit-door")
        r = client.post("/api/gateway/entities/Castor/loop/start", json={"tick_seconds": 7.0})
        assert r.status_code == 200, r.text
        _deprecated(r, "/life_state/toggles")
        assert r.json()["started"] is False and r.json()["deferred"] is True
        assert r.json()["life_state"]["toggles"]["personal"] is True
        assert read_personal_grant(_home_dir())["mode"] == "until_revoked"
        assert spawned == []

        write_entity_state(_home_dir(), "awake", reason="visit over")
        from abstractgateway import entity_phase
        from abstractgateway.routes.entities import _registry

        entity_phase.reconcile(_registry(), "castor", "visit_closed")
        assert spawned and spawned[-1].get("tick_seconds") == 7.0  # the remembered parameters

        write_entity_state(_home_dir(), "paused", reason="stop")
        refused = client.post("/api/gateway/entities/Castor/loop/start", json={})
        assert refused.status_code == 409 and refused.json()["detail"]["reason_code"] == "stopped_use_resume"


def test_graceful_loop_stop_also_turns_personal_off_and_freeze_is_the_kill_switch(spawned):
    from abstractruntime.identity.life import read_personal_grant, write_entity_state, write_personal_grant

    with _client() as client:
        _born(client)
        write_entity_state(_home_dir(), "awake", reason="woken")
        write_personal_grant(_home_dir(), mode="until_revoked", granted_by="person:admin")
        r = client.post("/api/gateway/entities/Castor/loop/stop", json={})
        assert r.status_code == 200, r.text
        _deprecated(r, "/life_state/toggles")
        assert read_personal_grant(_home_dir())["mode"] == "disabled"
        assert r.json()["life_state"]["toggles"]["personal"] is False

        f = client.post("/api/gateway/entities/Castor/loop/stop", json={"mode": "freeze"})
        assert f.status_code == 200, f.text
        _deprecated(f, "/lifecycle")
        assert f.json()["frozen"] is True and f.json()["action"] == "stop" and f.json()["phase"] == "stopped"


def test_personal_grant_put_is_the_personal_toggle(spawned):
    from abstractruntime.identity.life import write_entity_state

    with _client() as client:
        _born(client)
        write_entity_state(_home_dir(), "awake", reason="woken")
        r = client.put("/api/gateway/entities/Castor/personal-grant", json={"mode": "until_revoked"})
        assert r.status_code == 200, r.text
        _deprecated(r, "/life_state/toggles")
        assert r.json()["life_state"]["toggles"]["personal"] is True
        assert len(spawned) == 1
