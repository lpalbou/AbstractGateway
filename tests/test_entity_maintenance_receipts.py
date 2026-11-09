"""Only a maintenance pass that ran to its end is the home's last maintenance (the idle worker's report,
2026-11-09). `maintenance_done` recorded "done" for a night that yielded to a visit or stopped on its hard
check (an unreadable lifecycle generation), and that receipt reset the idle sleep's 24 h / 500-memory clock.
A night memory ended early (`cancelled_after`) records its own status under `last_pass` (`yielded` /
`cancelled`); `last_maintenance` keeps the last pass that completed. Hermetic gateway; memory's real night,
ended at its start boundary."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")

_TOKEN = "maintenance-receipts-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import entity_chat

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setattr(entity_chat, "gateway_text_mind",
                        lambda: {"provider": "lmstudio", "model": "test-model", "base_url": None, "reasoning": None})


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _registry():
    from abstractgateway.routes.entities import _registry as reg

    return reg()


def _born(client) -> None:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = "Castor"
    spark["spark"] = 1
    assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": spark}).status_code == 201


def _slept_at(at: datetime) -> dict:
    from abstractgateway import entity_phase

    reg = _registry()
    with entity_phase.phase_lock(reg.entities_dir, "castor"):
        rec = entity_phase.read_record(reg.entities_dir, "castor")
        rec["last_maintenance"] = {"generation": 0, "at": at.isoformat(), "status": "done", "error": None}
        entity_phase.write_record(reg.entities_dir, "castor", rec)
        return dict(rec["last_maintenance"])


def _night_that(monkeypatch, before_end):
    """Memory's real night, told at its start boundary that it may not continue (after `before_end` ran)."""
    import abstractmemory

    real_night = abstractmemory.sleep_pass

    def _night(*args, **kwargs):
        def _no():
            before_end()
            return False

        return real_night(*args, **{**kwargs, "should_continue": _no})

    monkeypatch.setattr(abstractmemory, "sleep_pass", _night)


@pytest.mark.parametrize("ended_by", ["hard_check", "visit"])
def test_a_night_ended_early_never_becomes_the_last_maintenance(monkeypatch, ended_by) -> None:
    from abstractruntime.identity.life import write_entity_state

    from abstractgateway import entity_idle_sleep, entity_phase

    with _client() as client:
        _born(client)
        before = _slept_at(datetime.now(timezone.utc) - timedelta(hours=25))
        home = _registry().entities_dir / "castor"

        def _visitor_arrives():
            write_entity_state(home, "asleep", reason="in conversation with person:admin [visit v-1]", mode="visiting",
                               written_by="visit-door")

        _night_that(monkeypatch, _visitor_arrives if ended_by == "visit" else (lambda: None))
        out = entity_idle_sleep.idle_sweep_one(_registry(), "castor")
        assert out and out.get("action") == "idle_sleep", out
        rec = entity_phase.read_record(_registry().entities_dir, "castor")
        assert rec["last_maintenance"] == before, "a night that did not run to its end moved the idle clock"
        assert rec["last_pass"]["status"] == ("yielded" if ended_by == "visit" else "cancelled")
        assert rec["last_pass"]["cancelled_after"]
        assert entity_idle_sleep.last_maintenance_done(home, rec) == before["at"]


def test_a_night_that_ran_to_its_end_is_the_last_maintenance(monkeypatch) -> None:
    from abstractgateway import entity_idle_sleep, entity_phase

    with _client() as client:
        _born(client)
        before = _slept_at(datetime.now(timezone.utc) - timedelta(hours=25))
        out = entity_idle_sleep.idle_sweep_one(_registry(), "castor")
        assert out and out.get("maintenance") == "woke", out
        rec = entity_phase.read_record(_registry().entities_dir, "castor")
        assert rec["last_maintenance"]["status"] == "done" and rec["last_maintenance"]["at"] > before["at"]
