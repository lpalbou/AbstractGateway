"""The kill switch (operator ruling 2026-11-08; root untracked/entity-phases-2026-11-08/
CONTRACT-phases.md §3): `POST /entities/{name}/lifecycle {action: stop}` is instant and
unconditional — the generation fence first, then the paused write, cancellation signalled at once,
each teardown on its own, one report row per item; a repeated Stop retries; every writer that began
before the Stop (the unbounded visit wait, a maintenance pass, a mention's persist) gives up instead
of undoing it. Stopped is the one visit refusal (`stopped_use_resume`). Hermetic gateway; the
own-time loop is a real child process (never this test process)."""

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

_TOKEN = "entity-kill-switch-secret"


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


def _fake_loop(home_dir, phase: str = "day") -> subprocess.Popen:
    """A live own-time loop: a real child process the kill switch may signal."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    threading.Thread(target=proc.wait, daemon=True).start()  # reap it, so a killed pid reads dead
    (home_dir / "loop_status").write_text(json.dumps({"phase": phase, "pid": proc.pid}), encoding="utf-8")
    return proc


def _registry():
    from abstractgateway.routes.entities import _registry as reg

    return reg()


def test_stop_fences_kills_the_loop_and_reports_every_item():
    from abstractruntime.identity.life import read_entity_state

    from abstractgateway.entity_phase import read_record

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        home_dir = _home_dir()
        proc = _fake_loop(home_dir)
        gen0 = read_record(_registry().entities_dir, "castor")["generation"]

        r = client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "stop", "reason": "test"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["action"] == "stop" and body["already"] is False
        assert body["phase"] == "stopped" and body["stopped"] is True
        assert read_entity_state(home_dir)["state"] == "paused"
        assert read_record(_registry().entities_dir, "castor")["generation"] == gen0 + 1
        rows = {i["item"]: i for i in body["items"]}
        assert set(rows) >= {"loop", "visit", "chat", "maintenance", "queue"}
        assert rows["loop"]["outcome"] in ("killed_sigterm", "killed_sigkill")
        assert body["cancelled"]["loop_pid"] == proc.pid
        assert body["loop_signal"] in ("sigterm", "sigkill")
        assert body["complete"] is True
        deadline = time.monotonic() + 5
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert proc.poll() is not None, "the own-time process survived the kill switch"

        # A repeated Stop is answered, never refused, and changes nothing it should not.
        again = client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "stop"})
        assert again.status_code == 200 and again.json()["already"] is True
        assert {i["item"]: i for i in again.json()["items"]}["loop"]["outcome"] == "not_running"


def test_stop_closes_the_visit_and_every_door_then_answers_stopped(monkeypatch: pytest.MonkeyPatch):
    from abstractgateway import entity_chat

    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: _ScriptedLLM(["(unused)"]))
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert opened.status_code == 200, opened.text
        run_id = opened.json()["run_id"]

        stop = client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "stop"})
        assert stop.status_code == 200, stop.text
        rows = {i["item"]: i for i in stop.json()["items"]}
        assert rows["visit"] == {"item": "visit", "id": run_id, "outcome": "closed", "code": None, "sentence": None}
        assert stop.json()["cancelled"]["visits"] == [run_id]
        assert client.get("/api/gateway/entities/Castor/visit").json().get("open") is False

        for path, body in (("visit/open", {}), ("chat/open", {"context_window": 32000})):
            refused = client.post(f"/api/gateway/entities/Castor/{path}", json=body)
            assert refused.status_code == 409, (path, refused.text)
            assert refused.json() == {"detail": "Stopped. Resume to visit.", "code": "stopped_use_resume"}
            assert refused.headers.get("X-Gateway-Error-Code") == "stopped_use_resume"


def test_stop_during_the_unbounded_visit_wait_ends_the_open(monkeypatch: pytest.MonkeyPatch):
    """The door waits for a busy loop with no limit (ADR-0027); the wait is cancellation-aware: a
    Stop landing meanwhile ends the open with the stopped sentence, never a visit under the switch."""
    from abstractruntime.identity.life import read_entity_state

    from abstractgateway import entity_chat, entity_phase

    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: _ScriptedLLM(["(unused)"]))
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        home_dir = _home_dir()
        _fake_loop(home_dir)  # mid-day: the door waits
        registry = _registry()
        from abstractgateway.routes.entities import _chat_host, _visit_host

        hosts = {"chat_host": _chat_host(), "visit_host": _visit_host()}

        def _stop_soon() -> None:
            time.sleep(0.5)
            entity_phase.stop(registry, "castor", actor="person:admin", **hosts)

        stopper = threading.Thread(target=_stop_soon, daemon=True)
        stopper.start()
        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        stopper.join(10)
        assert opened.status_code == 409, opened.text
        assert opened.json()["code"] == "stopped_use_resume"
        assert read_entity_state(home_dir)["state"] == "paused"


def test_a_maintenance_pass_never_undoes_a_stop(monkeypatch: pytest.MonkeyPatch):
    """The operator sleep's pass restored `asleep` in its finally, unconditionally: a Stop landing
    during the pass was undone. The finally is generation-fenced now."""
    import abstractmemory
    from abstractruntime.identity.life import read_entity_state

    from abstractgateway import entity_phase

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        registry = _registry()
        from abstractgateway.routes.entities import _chat_host, _visit_host

        hosts = {"chat_host": _chat_host(), "visit_host": _visit_host()}

        def _pass_with_a_stop(*args, **kwargs):
            entity_phase.stop(registry, "castor", actor="person:admin", **hosts)
            return {"ok": True}

        # The runtime facade re-exports the engine's sleep_pass lazily (module __getattr__): patch
        # the engine only — setting the facade attribute would leave a cached binding behind.
        monkeypatch.setattr(abstractmemory, "sleep_pass", _pass_with_a_stop)
        assert client.post("/api/gateway/entities/Castor/state", json={"state": "awake"}).status_code == 200
        r = client.post("/api/gateway/entities/Castor/state", json={"state": "sleep", "dream": True})
        assert r.status_code == 200, r.text
        assert read_entity_state(_home_dir())["state"] == "paused"


def test_a_mention_answer_landing_after_a_stop_is_neither_recorded_nor_mirrored(monkeypatch: pytest.MonkeyPatch):
    from abstractgateway import entity_chat, entity_phase
    from abstractgateway.entity_visits import EntityVisitHost
    from abstractgateway.service import get_gateway_service

    llm = _ScriptedLLM(["Castor answers after all."])
    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: llm)
    real_turn = EntityVisitHost.turn

    def _turn_then_stop(self, *args, **kwargs):
        out = real_turn(self, *args, **kwargs)
        with entity_phase.phase_lock(self._registry.entities_dir, "castor"):
            rec = entity_phase.read_record(self._registry.entities_dir, "castor")
            rec["generation"] = int(rec["generation"]) + 1  # what Stop's fence does first
            entity_phase.write_record(self._registry.entities_dir, "castor", rec)
        return out

    monkeypatch.setattr(EntityVisitHost, "turn", _turn_then_stop)
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        r = client.post("/api/gateway/sessions/sess-stop/mentions",
                        json={"entity": "Castor", "text": "@Castor still there?", "mention_index": 0})
        assert r.status_code == 409, r.text
        assert r.json()["code"] == "stopped_use_resume"
        runs = get_gateway_service().host.run_store.list_runs(limit=200) or []
        assert [r for r in runs if getattr(r, "session_id", None) == "sess-stop"] == [], (
            "the answer was recorded as a turn of the session after the Stop"
        )


def test_resume_writes_awake_never_a_sleep_and_restores_own_time(monkeypatch: pytest.MonkeyPatch):
    """Resume restores the phase the Stop interrupted while its grant is on (operator 2026-11-09);
    it never restores a sleep."""
    from abstractruntime.identity.life import read_entity_state, write_entity_state

    from abstractgateway import entity_phase

    spawned = []

    def _fake_start(registry, manifest, params):
        spawned.append(manifest.slug)
        return {"pid": 4242}

    monkeypatch.setattr(entity_phase, "start_own_time", _fake_start)
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        home_dir = _home_dir()
        write_entity_state(home_dir, "awake", reason="woken")
        # The operator gives own time (starts at once), later puts him to sleep, then stops him.
        assert client.put("/api/gateway/entities/Castor/life_state/toggles", json={"personal": True}).status_code == 200
        assert spawned == ["castor"]
        write_entity_state(home_dir, "asleep", reason="operator sleep")
        assert client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "stop"}).status_code == 200

        r = client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "resume"})
        assert r.status_code == 200, r.text
        assert r.json()["already"] is False
        assert read_entity_state(home_dir)["state"] == "awake"  # the sleep before the stop is gone
        assert spawned == ["castor", "castor"] and r.json()["started"] == {"own_time": True, "pid": 4242}

        again = client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "resume"})
        assert again.status_code == 200 and again.json()["already"] is True
        assert spawned == ["castor", "castor"]


def test_resume_of_an_entity_that_had_no_own_time_stays_idle(monkeypatch: pytest.MonkeyPatch):
    from abstractruntime.identity.life import write_entity_state, write_personal_grant

    from abstractgateway import entity_phase

    spawned = []
    monkeypatch.setattr(entity_phase, "start_own_time", lambda r, m, p: spawned.append(m.slug) or {"pid": 1})
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        write_entity_state(_home_dir(), "awake", reason="woken")
        write_personal_grant(_home_dir(), mode="until_revoked", granted_by="person:admin")  # granted, never run
        assert client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "stop"}).status_code == 200
        r = client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "resume"})
        assert r.status_code == 200 and r.json()["phase"] == "idle"
        assert spawned == []


def test_lifecycle_body_errors_and_unknown_entity():
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        bad = client.post("/api/gateway/entities/Castor/lifecycle", content=b"{not json",
                          headers={"Content-Type": "application/json"})
        assert bad.status_code == 422 and bad.json()["code"] == "invalid_body"
        assert bad.json()["detail"]["reason_code"] == "invalid_body"
        assert bad.json()["detail"]["life_state"]["phase"] == "sleep"  # a newborn is born asleep
        assert bad.json()["detail"]["life_state"]["rev"] >= 1
        for body in ({"action": "pause"}, {"action": "stop", "extra": 1}, ["stop"]):
            r = client.post("/api/gateway/entities/Castor/lifecycle", json=body)
            assert r.status_code == 422, body
            assert r.json()["code"] == ("invalid_body" if isinstance(body, list) else "invalid_action")
        missing = client.post("/api/gateway/entities/Nobody/lifecycle", json={"action": "stop"})
        assert missing.status_code == 404
        assert missing.json() == {"code": "entity_not_found", "detail": {
            "reason_code": "entity_not_found", "message": "There is no entity named Nobody.", "life_state": None}}


def test_both_phase_routes_are_admin_only():
    from abstractgateway.security.authorization import gateway_route_authorization_requirement

    for path, method in (("/api/gateway/entities/castor/lifecycle", "POST"),
                         ("/api/gateway/entities/castor/life_state/toggles", "PUT")):
        req = gateway_route_authorization_requirement(path, method)
        assert req is not None and req.required_role == "admin" and req.reason_code == "admin_required", path
    assert gateway_route_authorization_requirement("/api/gateway/entities/castor/life_state", "GET") is None


def test_cancel_run_tree_outcomes_reports_every_run():
    from abstractgateway.entity_seat import cancel_run_tree, cancel_run_tree_outcomes

    class _Run:
        def __init__(self, rid, status):
            self.run_id = rid
            self.status = status

    runs = {"root": _Run("root", "running"), "done": _Run("done", "completed"), "boom": _Run("boom", "running")}

    class _Store:
        def load(self, rid):
            return runs.get(rid)

        def list_children(self, parent_run_id):
            if parent_run_id == "root":
                return [runs["done"], runs["boom"], _Run("ghost", "running")]
            if parent_run_id == "boom":
                raise RuntimeError("index offline")
            return []

    class _Runtime:
        def cancel_run(self, rid, reason):
            if rid == "boom":
                raise RuntimeError("store locked")
            if rid == "ghost":
                raise KeyError(rid)

    out = cancel_run_tree_outcomes(_Runtime(), _Store(), "root", reason="test")
    by = {(o["run_id"], o["outcome"]): o for o in out}
    assert ("root", "cancelled") in by
    assert ("done", "already_terminal") in by
    assert ("ghost", "missing") in by
    assert by[("boom", "failed")]["code"] in ("run_cancel_failed", "run_children_unknown")
    assert {o["code"] for o in out if o["run_id"] == "boom"} == {"run_cancel_failed", "run_children_unknown"}
    assert cancel_run_tree(_Runtime(), _Store(), "root", reason="test") == ["root"]


def test_the_hard_stop_never_signals_the_gateway_itself_and_stops_once(monkeypatch: pytest.MonkeyPatch):
    """Review 2026-11-09 #3: a status naming this process is never signalled; two concurrent Stops
    run ONE hard stop; a live process behind a stale heartbeat is still stopped; a hard stop whose
    death is not confirmed is reported as failed."""
    import os

    from abstractgateway import entity_loop, entity_phase

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        home = _home_dir()
        # (a) the status names THIS process: never a signal.
        (home / "loop_status").write_text(json.dumps({"phase": "day", "pid": os.getpid()}), encoding="utf-8")
        row = entity_phase._kill_loop(home, {}, "test")
        assert row["outcome"] == "not_running"

        # (b) two concurrent Stops on a live loop with a stale heartbeat: one hard stop, the loop dies.
        calls = []
        real_freeze = entity_loop.freeze_loop

        def _spy(home_dir, **kw):
            calls.append(1)
            time.sleep(0.2)
            return real_freeze(home_dir, **kw)

        monkeypatch.setattr(entity_loop, "freeze_loop", _spy)
        proc = _fake_loop(home)
        (home / "loop_status").write_text(json.dumps({"phase": "day", "pid": proc.pid,
                                                      "updated_at": "2020-01-01T00:00:00+00:00"}), encoding="utf-8")
        rows = []
        ts = [threading.Thread(target=lambda: rows.append(entity_phase._kill_loop(home, {}, "test"))) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(15)
        assert len(calls) == 1, "two hard stops ran"
        assert sorted(r["outcome"] for r in rows)[0] in ("killed_sigkill", "killed_sigterm")
        assert proc.poll() is not None or not entity_phase._pid_alive_strict(proc.pid)

        # (c) a hard stop that does not kill is never reported as a kill.
        proc2 = _fake_loop(home)
        monkeypatch.setattr(entity_loop, "freeze_loop", lambda home_dir, **kw: {"frozen": True})
        row = entity_phase._kill_loop(home, {}, "test")
        assert row["outcome"] == "failed" and row["code"] == "loop_still_alive"
        proc2.kill()
