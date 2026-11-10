"""`PUT /entities/{name}/life_state/toggles` and Work off at the entity's admission points
(operator ruling 2026-11-08; root untracked/entity-phases-2026-11-08/CONTRACT-phases.md v2 §2, §6.2).
The toggles are permissions: personal off never puts him to sleep (root 1178); work off refuses new
work at the summon door (direct and queued), the task inbox and the work order, while a person's own
summon stays a visit. Hermetic gateway."""

from __future__ import annotations

from abstractruntime.identity.lifecycle_fence import LifecycleFence as _LF
import copy
import json
import subprocess
import sys
import threading

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")

_TOKEN = "entity-toggles-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch):
    from abstractgateway import entity_phase

    calls = []

    def _fake_start(registry, manifest, params, return_to=None, *, fence):
        calls.append({**dict(params), "_return_to": return_to})
        return {"pid": 5150 + len(calls)}

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


def _awake(client):
    from abstractruntime.identity.life import write_entity_state

    assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
    write_entity_state(_home_dir(), "awake", reason="woken", fence=_LF.admit(_home_dir()))


def _put(client, body, **kw):
    return client.put("/api/gateway/entities/Castor/life_state/toggles", json=body, **kw)


def test_personal_on_starts_own_time_and_off_never_puts_him_to_sleep(spawned):
    from abstractruntime.identity.life import read_entity_state, read_personal_grant, write_entity_state

    with _client() as client:
        _awake(client)
        r = _put(client, {"personal": True})
        assert r.status_code == 200, r.text
        assert r.json()["changed"] == {"personal": {"from": False, "to": True}}
        assert r.json()["toggles"] == {"personal": True}
        assert read_personal_grant(_home_dir())["mode"] == "until_revoked"
        assert len(spawned) == 1

        # Same values again: a no-op (no rev bump, no second start).
        rev = r.json()["rev"]
        again = _put(client, {"personal": True})
        assert again.status_code == 200 and again.json()["changed"] == {"personal": None}
        assert again.json()["rev"] == rev and len(spawned) == 1

        # The old loop gate's landing when own time ended: asleep + 6 h. Off = awake and reachable.
        write_entity_state(_home_dir(), "asleep", reason="personal ended (grant_revoked)", written_by="grant-gate",
                           wake_at="2099-01-01T00:00:00+00:00", fence=_LF.admit(_home_dir()))
        off = _put(client, {"personal": False})
        assert off.status_code == 200, off.text
        assert read_personal_grant(_home_dir())["mode"] == "disabled"
        assert read_entity_state(_home_dir())["state"] == "awake"
        assert off.json()["phase"] == "idle" and off.json()["can"]["visit"]["allowed"] is True


def test_personal_on_during_a_visit_waits_for_the_visit(spawned, monkeypatch: pytest.MonkeyPatch):
    from abstractruntime.identity.life import write_entity_state

    with _client() as client:
        _awake(client)
        write_entity_state(_home_dir(), "asleep", reason="in conversation with person:admin [visit v-1]",
                           mode="visiting", written_by="visit-door", fence=_LF.admit(_home_dir()))
        r = _put(client, {"personal": True})
        assert r.status_code == 200, r.text
        assert r.json()["phase"] == "visit" and r.json()["toggles"]["personal"] is True
        assert spawned == []  # own time starts when the visit closes, never under it


def test_toggle_body_validation_and_the_stopped_refusal():
    from abstractruntime.identity.life import write_entity_state

    with _client() as client:
        _awake(client)
        for body, code in (({}, "invalid_toggles"), ({"personal": "yes"}, "invalid_toggles"), ({"personal": 1}, "invalid_toggles"),
                           ({"personal": True, "sleep": True}, "invalid_toggles"), ([True], "invalid_body")):
            r = _put(client, body)
            assert r.status_code == 422, body
            assert r.json()["code"] == code and r.json()["detail"]["life_state"]["phase"] == "idle"
        bad = client.put("/api/gateway/entities/Castor/life_state/toggles", content=b"nope",
                         headers={"Content-Type": "application/json"})
        assert bad.status_code == 422 and bad.json()["code"] == "invalid_body"
        write_entity_state(_home_dir(), "paused", reason="stop", fence=_LF.admit(_home_dir()))
        r = _put(client, {"personal": False})
        assert r.status_code == 409
        assert r.json()["detail"]["message"] == "Stopped. Resume him first, then choose what he may do."
        assert r.json()["code"] == "stopped_use_resume" and r.json()["detail"]["life_state"]["phase"] == "stopped"
        assert client.put("/api/gateway/entities/Nobody/life_state/toggles", json={"personal": True}).status_code == 404


def test_there_is_no_work_switch():
    """Contract v3 D1 (operator 2026-11-09: "no work toggle"): `work` is gone from the toggles, from
    `can`, and from the PUT, which names why; nothing refuses work for a switch."""
    with _client() as client:
        _awake(client)
        for body in ({"work": False}, {"work": True, "personal": True}):
            r = _put(client, body)
            assert r.status_code == 422, r.text
            assert r.json()["code"] == "unknown_field"
            assert r.json()["detail"]["message"] == "There is no work switch: work starts when a job is queued."
        ls = client.get("/api/gateway/entities/Castor/life_state").json()
        assert ls["toggles"] == {"personal": False}
        assert set(ls["can"]) == {"visit", "sleep_now", "toggle_personal"}
        assert ls["work"] == {"jobs_queued": 0, "current": None}
        t = client.post("/api/gateway/entities/Castor/tasks", json={"title": "sort the archive"})
        assert t.status_code == 200 and "work_off" not in t.text


def test_a_queued_job_starts_work_without_the_grant(spawned):
    """Contract v3 D1: work is driven by the job queue, whatever the personal grant says: a work
    order queued on an idle entity starts the loop for work; a task too; a second job while the
    first is being picked up is counted in the queue."""
    with _client() as client:
        _awake(client)
        w = client.put("/api/gateway/entities/Castor/work-order", json={"order": "Sort the archive."})
        assert w.status_code == 200, w.text
        assert len(spawned) == 1 and spawned[-1]["_return_to"] is None  # the runtime's gate works the queue
        ls = client.get("/api/gateway/entities/Castor/life_state").json()
        assert ls["work"]["jobs_queued"] == 1 and ls["toggles"] == {"personal": False}
        t = client.post("/api/gateway/entities/Castor/tasks", json={"title": "and the letters"})
        assert t.status_code == 200, t.text
        # (The fake spawn leaves no live loop, so the task's reconcile starts it again — for work.)
        assert len(spawned) == 2 and all(c["_return_to"] is None for c in spawned)
        assert client.get("/api/gateway/entities/Castor/life_state").json()["work"]["jobs_queued"] == 2
