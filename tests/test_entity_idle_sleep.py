"""Idle -> sleep by the Mind's thresholds (contract-phases v3 D2). Hermetic gateway; the sleep's
maintenance pass is faked (its machinery is tested elsewhere)."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")

_TOKEN = "entity-idle-sleep-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import entity_chat

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setattr(entity_chat, "gateway_text_mind",
                        lambda: {"provider": "lmstudio", "model": "test-model", "base_url": None, "reasoning": None})


def _spark(name: str = "Castor") -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _registry():
    from abstractgateway.routes.entities import _registry as reg

    return reg()


def _home(slug: str = "castor"):
    return _registry().entities_dir / slug


def _slept_at(at: datetime, slug: str = "castor") -> None:
    """The gateway's own receipt of a completed maintenance pass at `at`."""
    from abstractgateway import entity_phase

    reg = _registry()
    with entity_phase.phase_lock(reg.entities_dir, slug):
        rec = entity_phase.read_record(reg.entities_dir, slug)
        rec["last_maintenance"] = {"generation": 0, "at": at.isoformat(), "status": "done", "error": None}
        entity_phase.write_record(reg.entities_dir, slug, rec)


def _born(client: TestClient, *names: str) -> None:
    for name in names:
        assert client.post("/api/gateway/entities", json={"name": name, "spark": _spark(name)}).status_code == 201


def _phase(client: TestClient, name: str = "Castor") -> str:
    return client.get(f"/api/gateway/entities/{name}/life_state").json()["phase"]


def _completed_night(slug: str = "castor"):
    """Memory's own completion marker of his last night (None = no night completed)."""
    from abstractruntime.integrations.abstractmemory.memory_facade import records_since_maintenance

    from abstractgateway.entities import DIARY_SCOPE, LIFE_SCOPE, SELF_SCOPE

    home = _registry().get_home(slug)
    eid = str(home.entity_id)
    return records_since_maintenance(home.memory, scopes=[(SELF_SCOPE, eid), (DIARY_SCOPE, eid), (LIFE_SCOPE, eid)],
                                     )["last_maintenance_at"]


def test_the_thresholds_decide(monkeypatch: pytest.MonkeyPatch):
    from abstractgateway.entity_idle_sleep import sleep_due

    t0 = datetime.now(timezone.utc) - timedelta(minutes=1)
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        _slept_at(t0)
        # Slept 2 h ago, a few memories since: no sleep.
        assert sleep_due(_registry(), "castor", now=t0 + timedelta(hours=2)) is None
        # 24 h past the last sleep: the hours threshold fires and says so.
        due = sleep_due(_registry(), "castor", now=t0 + timedelta(hours=25))
        assert due["threshold"] == "after_hours" and due["reason"] == "24 h since the last sleep"
        assert due["boundary_source"] == "after"
        # The events threshold (a Mind setting, set through the cognition profile).
        _slept_at(t0 - timedelta(days=2))  # the spark's memories formed after this sleep
        r = client.put("/api/gateway/entities/castor/cognition-profile", json={"sleep": {"after_events": 1}})
        assert r.status_code == 200, r.text
        assert client.get("/api/gateway/entities/castor/cognition-profile").json()["sleep"]["after_events"] == 1
        due = sleep_due(_registry(), "castor", now=t0 + timedelta(minutes=5))
        assert due["threshold"] == "after_events" and due["reason"] == "1 new memories since the last sleep"
        bad = client.put("/api/gateway/entities/castor/cognition-profile", json={"sleep": {"after_hours": 721}})
        assert bad.status_code == 400, bad.text


def test_a_never_slept_home_is_seeded_from_its_own_history():
    """Without the seed every existing home reads "never slept" and sleeps at the first sweep."""
    from abstractgateway.entity_idle_sleep import sleep_due

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        now = datetime.now(timezone.utc) + timedelta(hours=30)  # born 30 h before "now"
        with (_home() / "state_history.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"marker": "phase_changed", "from": "sleep", "to": "personal",
                                 "cause": "maintenance_done", "written_by": "loop",
                                 "at": (now - timedelta(hours=1)).isoformat()}) + "\n")
        assert sleep_due(_registry(), "castor", now=now) is None


def test_the_sweep_puts_an_idle_entity_to_its_own_sleep_and_back_to_idle(monkeypatch: pytest.MonkeyPatch):
    import abstractmemory
    from abstractruntime.identity.life import read_entity_state

    from abstractgateway import entity_idle_sleep

    seen = {}

    def _pass(*args, **kwargs):
        seen["state"] = dict(read_entity_state(_home()))
        return {"ok": True}

    monkeypatch.setattr(abstractmemory, "sleep_pass", _pass)
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        _slept_at(datetime.now(timezone.utc) - timedelta(hours=25))
        out = entity_idle_sleep.idle_sweep_one(_registry(), "castor")
        assert out and out["action"] == "idle_sleep" and out["reason"] == "24 h since the last sleep", out
        assert out["maintenance"] == "woke", out
        assert seen["state"]["state"] == "asleep" and seen["state"]["written_by"] == "idle-sweep"
        ls = client.get("/api/gateway/entities/Castor/life_state").json()
        assert ls["phase"] == "idle", ls
        from abstractgateway.entity_replay import read_host_markers

        receipt = [m["payload"] for m in read_host_markers(_registry().entities_dir, "castor", since_seq=-1.0)
                   if (m.get("payload") or {}).get("kind") == "idle_sleep"]
        assert receipt and receipt[-1]["boundary_source"] == "after" and receipt[-1]["threshold"] == "after_hours"
        # Just slept: the next sweep does not put him to sleep again.
        again = entity_idle_sleep.idle_sweep_one(_registry(), "castor")
        assert again is None or again["action"] != "idle_sleep"


def test_only_an_idle_entity_is_put_to_sleep(monkeypatch: pytest.MonkeyPatch):
    """No threshold ever applies in visit, work or personal (spec v25 SLEEP-IS-A-DECISION)."""
    import abstractmemory
    from abstractruntime.identity.life import read_entity_state, write_entity_state

    from abstractgateway import entity_idle_sleep

    monkeypatch.setattr(abstractmemory, "sleep_pass", lambda *a, **k: {"ok": True})
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        _slept_at(datetime.now(timezone.utc) - timedelta(hours=25))
        write_entity_state(_home(), "asleep", reason="in conversation with person:admin [visit v-1]",
                           mode="visiting", written_by="visit-door")
        assert entity_idle_sleep.idle_sweep_one(_registry(), "castor") is None
        assert read_entity_state(_home())["mode"] == "visiting"


# --------------------------------------------------------------------------- Codex review 3 (#5)


def test_a_stop_during_the_final_dream_stage_records_no_completed_night(monkeypatch: pytest.MonkeyPatch):
    """Codex review 3 #5: memory was handed only `should_continue`, which a night checks at its stage
    boundaries, so a Stop landing while its final stage (the dream) ran still recorded a completed night
    and reset the idle sleep's boundary. The Stop check now also goes as memory's `cancelled`, consulted
    right before the completion marker. The real night runs; the Stop lands inside its dream."""
    import abstractmemory.maintenance as night
    from abstractruntime.identity.life import read_entity_state

    from abstractgateway import entity_idle_sleep, entity_phase

    real_dream = night.dream_pass
    stages = []
    with _client() as client:
        _born(client, "Castor")
        _slept_at(datetime.now(timezone.utc) - timedelta(hours=25))

        def _dream_then_stop(*args, **kwargs):
            out = real_dream(*args, **kwargs)
            stages.append("dream")
            assert client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "stop"}).status_code == 200
            return out

        monkeypatch.setattr(night, "dream_pass", _dream_then_stop)
        out = entity_idle_sleep.idle_sweep_one(_registry(), "castor")
        assert stages == ["dream"] and out["maintenance"] == "stopped", out
        assert _completed_night() is None
        assert entity_phase.read_record(_registry().entities_dir, "castor")["last_maintenance"]["status"] == "cancelled"
        assert read_entity_state(_home())["state"] == "paused"


def test_an_unreadable_lifecycle_generation_ends_the_night_like_a_stop(monkeypatch: pytest.MonkeyPatch):
    """The pass's Stop check fails CLOSED (Codex review 3 #5): it answered "not stopped" when the
    generation could not be read. A phases.yaml that turns unreadable during the final stage now ends
    the night like a Stop: no completion recorded."""
    import abstractmemory
    import abstractmemory.maintenance as night

    from abstractgateway import entity_idle_sleep

    real_dream, real_night = night.dream_pass, abstractmemory.sleep_pass
    saved = {}
    with _client() as client:
        _born(client, "Castor")
        _slept_at(datetime.now(timezone.utc) - timedelta(hours=25))
        phases = _home() / "phases.yaml"

        def _dream_then_unreadable(*args, **kwargs):
            out = real_dream(*args, **kwargs)
            saved["text"] = phases.read_text(encoding="utf-8") if phases.exists() else None
            phases.write_text("schema_version: [unterminated\n", encoding="utf-8")
            return out

        def _night(*args, **kwargs):
            try:
                return real_night(*args, **kwargs)
            finally:
                if saved.get("text") is None:
                    phases.unlink()
                else:
                    phases.write_text(saved["text"], encoding="utf-8")

        monkeypatch.setattr(night, "dream_pass", _dream_then_unreadable)
        monkeypatch.setattr(abstractmemory, "sleep_pass", _night)
        out = entity_idle_sleep.idle_sweep_one(_registry(), "castor")
        assert "text" in saved, out  # the night reached its final stage
        assert _completed_night() is None, out
