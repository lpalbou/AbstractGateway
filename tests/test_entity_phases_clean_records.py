"""The kill switch keeps the ledger clean (operator 2026-11-09: "the ledger must be kept clean"):
every visit, turn and pass it ends leaves ONE clean terminal record — status cancelled, reason
kill_switch, by the operator — never a failure, never a half-written entry. (The summon run case
lives with the summon tests: tests/test_gateway_entities_summon.py.) Hermetic gateway."""

from __future__ import annotations

import copy
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractruntime.identity.visit_workflow")

_TOKEN = "entity-clean-records-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import entity_chat

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setattr(entity_chat, "gateway_text_mind",
                        lambda: {"provider": "lmstudio", "model": "test-model", "base_url": None, "reasoning": None})


class _BlockingLLM:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def generate(self, **kwargs):
        self.entered.set()
        self.release.wait(20)
        return type("R", (), {"content": "a reply that came too late"})()


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


def _markers(kind: str):
    path = _home().parent / ".host_stream" / "castor.jsonl"
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [r["payload"] for r in rows if (r.get("payload") or {}).get("kind") == kind]


def test_a_visit_turn_stopped_mid_generation_ends_cancelled_and_clean(monkeypatch: pytest.MonkeyPatch):
    from abstractgateway import entity_chat
    from abstractgateway.entity_phase import KILL_SWITCH_REASON
    from abstractgateway.routes.entities import _registry, _visit_host

    llm = _BlockingLLM()
    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: llm)
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert opened.status_code == 200, opened.text
        run_id = opened.json()["run_id"]
        host = _visit_host()
        turn = threading.Thread(target=lambda: _swallow(lambda: host.turn("castor", run_id, text="think hard")), daemon=True)
        turn.start()
        assert llm.entered.wait(15)
        stop = client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "stop"})
        assert stop.status_code == 200, stop.text
        llm.release.set()
        turn.join(20)
        er = _registry().get_entity_runtime("castor")
        run = er.run_store.load(run_id)
        assert str(getattr(run.status, "value", run.status)) == "cancelled"
        assert run.error == KILL_SWITCH_REASON
        records = er.ledger_store.list(run_id) or []
        assert not any(r.get("status") == "failed" for r in records), "a failure was written for a Stop"
        started = {r.get("step_id") for r in records if r.get("status") == "started"}
        ended = {r.get("step_id") for r in records if r.get("status") in ("completed", "cancelled", "waiting")}
        assert started <= ended, "a half-written ledger entry was left behind"
        closed = _markers("session_closed")[-1]
        assert closed.get("status") == "cancelled" and closed.get("reason") == KILL_SWITCH_REASON


def test_a_hosted_chat_turn_stopped_mid_generation_closes_as_cancelled(monkeypatch: pytest.MonkeyPatch):
    from abstractgateway import entity_chat
    from abstractgateway.routes.entities import _chat_host

    llm = _BlockingLLM()
    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: llm)
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        opened = client.post("/api/gateway/entities/Castor/chat/open", json={"context_window": 32000})
        host = _chat_host()
        turn = threading.Thread(target=lambda: _swallow(lambda: host.turn(opened.json()["chat_id"], "think")), daemon=True)
        turn.start()
        assert llm.entered.wait(15)
        assert client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "stop"}).status_code == 200
        llm.release.set()
        turn.join(20)
        deadline = time.monotonic() + 10
        while not _markers("session_closed") and time.monotonic() < deadline:
            time.sleep(0.1)
        closed = _markers("session_closed")[-1]
        assert closed["status"] == "cancelled" and closed["closed_by"] == "kill_switch" and closed["by"] == "operator"


def test_a_maintenance_pass_stopped_midway_records_a_clean_cancellation(monkeypatch: pytest.MonkeyPatch):
    import abstractmemory
    from abstractruntime.identity.life import write_entity_state

    from abstractgateway import entity_phase
    from abstractgateway.routes.entities import _chat_host, _registry, _visit_host

    def _pass_with_a_stop(*args, **kwargs):
        entity_phase.stop(_registry(), "castor", actor="person:admin", chat_host=_chat_host(), visit_host=_visit_host())
        return {"ok": True}

    monkeypatch.setattr(abstractmemory, "sleep_pass", _pass_with_a_stop)
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        write_entity_state(_home(), "awake", reason="woken")
        r = client.post("/api/gateway/entities/Castor/state", json={"state": "sleep", "dream": True})
        assert r.status_code == 200, r.text
        receipt = entity_phase.read_record(_home().parent, "castor")["last_maintenance"]
        assert receipt["status"] == "cancelled" and receipt["reason"] == "kill_switch" and receipt["by"] == "operator"
        assert receipt["error"] is None


def _swallow(fn):
    try:
        fn()
    except Exception:  # noqa: BLE001 - the stopped turn's own refusal is not under test here
        pass


def test_a_parked_visit_stopped_ends_cancelled_never_completed(monkeypatch: pytest.MonkeyPatch):
    from abstractgateway import entity_chat
    from abstractgateway.entity_phase import KILL_SWITCH_REASON
    from abstractgateway.routes.entities import _registry

    monkeypatch.setattr(entity_chat, "_default_llm_factory",
                        lambda provider, **kw: type("L", (), {"generate": lambda self, **k: type("R", (), {"content": "hi"})()})())
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        run_id = client.post("/api/gateway/entities/Castor/visit/open", json={}).json()["run_id"]
        assert client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "stop"}).status_code == 200
        run = _registry().get_entity_runtime("castor").run_store.load(run_id)
        assert str(getattr(run.status, "value", run.status)) == "cancelled" and run.error == KILL_SWITCH_REASON
