"""`PUT /entities/{name}/life_state/toggles` and Work off at the entity's admission points
(operator ruling 2026-11-08; root untracked/entity-phases-2026-11-08/CONTRACT-phases.md v2 §2, §6.2).
The toggles are permissions: personal off never puts him to sleep (root 1178); work off refuses new
work at the summon door (direct and queued), the task inbox and the work order, while a person's own
summon stays a visit. Hermetic gateway."""

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

_TOKEN = "entity-toggles-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch):
    from abstractgateway import entity_phase

    calls = []

    def _fake_start(registry, manifest, params, return_to=None):
        calls.append(dict(params))
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
    write_entity_state(_home_dir(), "awake", reason="woken")


def _put(client, body, **kw):
    return client.put("/api/gateway/entities/Castor/life_state/toggles", json=body, **kw)


def test_personal_on_starts_own_time_and_off_never_puts_him_to_sleep(spawned):
    from abstractruntime.identity.life import read_entity_state, read_personal_grant, write_entity_state

    with _client() as client:
        _awake(client)
        r = _put(client, {"personal": True})
        assert r.status_code == 200, r.text
        assert r.json()["changed"] == {"work": None, "personal": {"from": False, "to": True}}
        assert r.json()["toggles"] == {"work": True, "personal": True}
        assert read_personal_grant(_home_dir())["mode"] == "until_revoked"
        assert len(spawned) == 1

        # Same values again: a no-op (no rev bump, no second start).
        rev = r.json()["rev"]
        again = _put(client, {"personal": True})
        assert again.status_code == 200 and again.json()["changed"] == {"work": None, "personal": None}
        assert again.json()["rev"] == rev and len(spawned) == 1

        # The old loop gate's landing when own time ended: asleep + 6 h. Off = awake and reachable.
        write_entity_state(_home_dir(), "asleep", reason="personal ended (grant_revoked)", written_by="grant-gate",
                           wake_at="2099-01-01T00:00:00+00:00")
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
                           mode="visiting", written_by="visit-door")
        r = _put(client, {"personal": True})
        assert r.status_code == 200, r.text
        assert r.json()["phase"] == "visit" and r.json()["toggles"]["personal"] is True
        assert spawned == []  # own time starts when the visit closes, never under it


def test_toggle_body_validation_and_the_stopped_refusal():
    from abstractruntime.identity.life import write_entity_state

    with _client() as client:
        _awake(client)
        for body, code in (({}, "invalid_toggles"), ({"work": "yes"}, "invalid_toggles"), ({"work": 1}, "invalid_toggles"),
                           ({"work": True, "sleep": True}, "invalid_toggles"), ([True], "invalid_body")):
            r = _put(client, body)
            assert r.status_code == 422, body
            assert r.json()["code"] == code and r.json()["detail"]["life_state"]["phase"] == "idle"
        bad = client.put("/api/gateway/entities/Castor/life_state/toggles", content=b"nope",
                         headers={"Content-Type": "application/json"})
        assert bad.status_code == 422 and bad.json()["code"] == "invalid_body"
        write_entity_state(_home_dir(), "paused", reason="stop")
        r = _put(client, {"work": False})
        assert r.status_code == 409
        assert r.json()["detail"]["message"] == "Stopped. Resume him first, then choose what he may do."
        assert r.json()["code"] == "stopped_use_resume" and r.json()["detail"]["life_state"]["phase"] == "stopped"
        assert client.put("/api/gateway/entities/Nobody/life_state/toggles", json={"work": True}).status_code == 404


def test_work_off_refuses_new_work_at_every_admission_point():
    with _client() as client:
        _awake(client)
        assert _put(client, {"work": False}).status_code == 200

        for kind in ("agent", None):  # an agent, or a caller that declares nothing: work
            body = {"prompt": "do the thing", **({"caller_kind": kind} if kind else {})}
            s = client.post("/api/gateway/entities/Castor/summon", json=body)
            assert s.status_code == 409, s.text
            assert s.json()["detail"]["reason_code"] == "work_off"
            assert s.json()["detail"]["message"] == "Work is off for him; turn Work on to send him requests."
        # A person principal declaring itself human is a visit (lane summon): the work gate lets it
        # through (this hermetic gateway then has no flow to run it).
        h = client.post("/api/gateway/entities/Castor/summon", json={"prompt": "hello", "caller_kind": "human"})
        assert "work_off" not in h.text, h.text

        t = client.post("/api/gateway/entities/Castor/tasks", json={"title": "sort the archive"})
        assert t.status_code == 409 and t.json()["detail"]["reason_code"] == "work_off"
        assert t.json()["detail"]["message"] == "Work is off for him; turn Work on to hand him a task."

        w = client.put("/api/gateway/entities/Castor/work-order", json={"order": "sort the archive"})
        assert w.status_code == 409 and w.json()["detail"]["reason_code"] == "work_off"
        cleared = client.put("/api/gateway/entities/Castor/work-order", json={"clear": True})
        assert cleared.status_code == 200, cleared.text  # clearing is never refused

        on = _put(client, {"work": True})
        assert on.json()["toggles"]["work"] is True
        assert client.post("/api/gateway/entities/Castor/tasks", json={"title": "sort the archive"}).status_code == 200


def test_work_off_with_a_standing_order_blocks_own_time_until_the_runtime_skips_it(spawned):
    with _client() as client:
        _awake(client)
        (_home_dir() / "work_order.md").write_text("Sort the archive.\n", encoding="utf-8")
        assert _put(client, {"work": False}).status_code == 200
        r = _put(client, {"personal": True})
        assert spawned == []
        assert r.json()["blocked"]["code"] == "work_order_waits_for_runtime"
