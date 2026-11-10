"""The kill switch (operator ruling 2026-11-08; root untracked/entity-phases-2026-11-08/
CONTRACT-phases.md §3): `POST /entities/{name}/lifecycle {action: stop}` is instant and
unconditional — the generation fence first, then the paused write, cancellation signalled at once,
each teardown on its own, one report row per item; a repeated Stop retries; every writer that began
before the Stop (the unbounded visit wait, a maintenance pass, a mention's persist) gives up instead
of undoing it. Stopped is the one visit refusal (`stopped_use_resume`). Hermetic gateway; the
own-time loop is a real child process (never this test process)."""

from __future__ import annotations

from abstractruntime.identity.lifecycle_fence import LifecycleFence as _LF
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
    from entity_fake_loop import spawn

    return spawn(home_dir, phase)  # this home's loop invocation, stamped (what the hard stop verifies)


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
        # Moved (the gateway's Stop bumps it; the runtime's hard stop of the live loop bumps it too).
        assert read_record(_registry().entities_dir, "castor")["generation"] > gen0
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
        # The Stop's own ledger row is the kill switch's, never the visit it closed (Castor E2E 2026-11-10: it read
        # visit_closed, the close having published first).
        from abstractgateway.entity_replay import read_host_markers

        rows = [m["payload"] for m in read_host_markers(_registry().entities_dir, "castor", since_seq=-1.0)
                if (m.get("payload") or {}).get("kind") == "phase_changed"]
        (into_stop,) = [r for r in rows if r.get("to") == "stopped" and r.get("from") != "stopped"]
        assert into_stop["cause"] == "kill_switch", rows

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
            from abstractruntime.identity.life import bump_lifecycle_generation

            bump_lifecycle_generation(self._registry.entities_dir / "castor", written_by="test")  # what Stop's fence does first
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


def test_stop_revokes_own_time_and_resume_is_idle(monkeypatch: pytest.MonkeyPatch):
    """v3 D3 (operator 2026-11-09: "personal time is never given back during a resume, it is
    considered cancelled if it was given before"): Stop revokes the grant (revoked_by kill_switch),
    forgets where to return, and says so in its receipt; Resume writes awake (never a sleep) and he
    is idle — own time only when the operator gives it again."""
    from abstractruntime.identity.life import read_entity_state, read_personal_grant, write_entity_state

    from abstractgateway import entity_phase
    from abstractgateway.entity_replay import read_host_markers

    spawned = []

    def _fake_start(registry, manifest, params, return_to=None, *, fence):
        spawned.append(manifest.slug)
        return {"pid": 4242}

    monkeypatch.setattr(entity_phase, "start_own_time", _fake_start)
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        home_dir = _home_dir()
        # The operator gives own time (starts at once), later puts him to sleep, then stops him.
        assert client.put("/api/gateway/entities/Castor/life_state/toggles", json={"personal": True}).status_code == 200
        assert spawned == ["castor"]
        write_entity_state(home_dir, "asleep", reason="operator sleep", fence=_LF.admit(home_dir))
        stopped = client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "stop"})
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["own_time_revoked"] is True
        row = {i["item"]: i for i in stopped.json()["items"]}["own_time"]
        assert row == {"item": "own_time", "id": None, "code": "revoked", "outcome": "done", "sentence": "Own time was revoked."}
        assert read_personal_grant(home_dir)["mode"] == "disabled"
        markers = [m["payload"] for m in read_host_markers(_registry().entities_dir, "castor", since_seq=-1.0)
                   if (m.get("payload") or {}).get("kind") == "personal_grant_revoked"]
        assert markers and markers[-1]["revoked_by"] == "kill_switch"
        assert entity_phase.read_record(_registry().entities_dir, "castor").get("resume_to") is None

        r = client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "resume"})
        assert r.status_code == 200, r.text
        assert r.json()["already"] is False
        assert read_entity_state(home_dir)["state"] == "awake"  # the sleep before the stop is gone
        assert r.json()["phase"] == "idle" and r.json()["toggles"]["personal"] is False
        assert spawned == ["castor"] and r.json()["started"] == {"own_time": False, "pid": None}

        again = client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "resume"})
        assert again.status_code == 200 and again.json()["already"] is True
        # A second Stop has nothing left to revoke: no row claims it.
        second = client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "stop"}).json()
        assert second["own_time_revoked"] is False and "own_time" not in {i["item"] for i in second["items"]}
        assert spawned == ["castor"]


def test_resume_of_an_entity_that_had_no_own_time_stays_idle(monkeypatch: pytest.MonkeyPatch):
    from abstractruntime.identity.life import write_entity_state, write_personal_grant

    from abstractgateway import entity_phase

    spawned = []
    monkeypatch.setattr(entity_phase, "start_own_time", lambda r, m, p, return_to=None, *, fence: spawned.append(m.slug) or {"pid": 1})
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        write_entity_state(_home_dir(), "awake", reason="woken", fence=_LF.admit(_home_dir()))
        write_personal_grant(_home_dir(), mode="until_revoked", granted_by="person:admin", fence=_LF.admit(_home_dir()))  # granted, never run
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
        assert bad.json()["detail"]["life_state"]["phase"] == "idle"  # a newborn starts idle
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
        from entity_fake_loop import write_status

        write_status(home, proc.pid, "day", updated_at="2020-01-01T00:00:00+00:00")
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

        # (d) the runtime's own refusal (2026-11-09: it signals only a verified loop of this home)
        # is said in the Stop receipt, never swallowed.
        monkeypatch.setattr(entity_loop, "freeze_loop", lambda home_dir, **kw: {
            "killed": False, "pid": proc2.pid, "signal": None, "reason": "not a loop started for this home"})
        row = entity_phase._kill_loop(home, {}, "test")
        assert row["outcome"] == "failed" and row["code"] == "loop_still_alive"
        assert "not a loop started for this home" in row["sentence"]
        proc2.kill()


def test_stop_fences_before_it_observes_anything(monkeypatch: pytest.MonkeyPatch):
    """Review #4: Stop used to publish (and possibly restore own time) BEFORE its fence. A
    pending restore (an interruption ended, never observed) must not start own time under the
    Stop."""
    from abstractruntime.identity.life import write_entity_state

    from abstractgateway import entity_phase

    spawned = []
    monkeypatch.setattr(entity_phase, "start_own_time", lambda r, m, p, return_to=None, *, fence: spawned.append(m.slug) or {"pid": 1})
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        home = _home_dir()
        from abstractruntime.identity.life import write_personal_grant

        write_personal_grant(home, mode="until_revoked", granted_by="person:admin", fence=_LF.admit(home))
        proc = _fake_loop(home, "between")
        write_entity_state(home, "awake", reason="woken", fence=_LF.admit(home))
        assert client.get("/api/gateway/entities/Castor/life_state").json()["phase"] == "personal"
        write_entity_state(home, "asleep", reason="self-elected sleep", written_by="self", fence=_LF.admit(home))
        assert client.get("/api/gateway/entities/Castor/life_state").json()["phase"] == "sleep"
        proc.kill()
        proc.wait(5)
        (home / "loop_status").write_text(json.dumps({"phase": "stopped", "pid": proc.pid, "stopped_by": "rest"}), encoding="utf-8")
        write_entity_state(home, "awake", reason="woke", written_by="self", fence=_LF.admit(home))  # not observed yet
        r = client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "stop"})
        assert r.status_code == 200 and r.json()["phase"] == "stopped"
        assert spawned == [], "own time was restored under the Stop"


def test_stop_never_waits_on_a_hosted_turn_in_flight(monkeypatch: pytest.MonkeyPatch):
    from abstractgateway import entity_chat

    release = threading.Event()
    entered = threading.Event()

    class _SlowLLM:
        class _R:
            def __init__(self, c):
                self.content = c

        def generate(self, **kwargs):
            entered.set()
            release.wait(20)
            return self._R("finally.")

    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: _SlowLLM())
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        opened = client.post("/api/gateway/entities/Castor/chat/open", json={"context_window": 32000})
        assert opened.status_code == 200, opened.text
        from abstractgateway.routes.entities import _chat_host

        host = _chat_host()
        chat_id = opened.json()["chat_id"]
        turn = threading.Thread(target=lambda: host.turn(chat_id, "think hard"), daemon=True)
        turn.start()
        assert entered.wait(10)
        t0 = time.monotonic()
        r = client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "stop"})
        took = time.monotonic() - t0
        assert r.status_code == 200, r.text
        assert took < 8, f"Stop waited on the turn ({took:.1f}s)"
        rows = {i["item"]: i for i in r.json()["items"]}
        assert rows["chat"]["outcome"] == "cancel_requested"
        release.set()
        turn.join(20)
        deadline = time.monotonic() + 10
        while host.status("castor").get("open") and time.monotonic() < deadline:
            time.sleep(0.1)
        assert host.status("castor").get("open") is False


def test_a_repeated_stop_retries_what_the_last_one_could_not_finish(monkeypatch: pytest.MonkeyPatch):
    """Review #5: the root of a run tree was cancelled, its child failed; the root then left live
    discovery and the next Stop forgot the child. It now retries the recorded child, in its store."""
    from abstractgateway import entity_phase, entity_seat

    calls = []

    def _fake_tree(runtime, store, run_id):
        calls.append(run_id)
        if run_id == "root":
            return [{"run_id": "root", "outcome": "cancelled", "code": None, "error": None},
                    {"run_id": "child", "outcome": "failed", "code": "run_cancel_failed", "error": "store locked"}]
        return [{"run_id": run_id, "outcome": "cancelled", "code": None, "error": None}]

    monkeypatch.setattr(entity_phase, "_cancel_tree", _fake_tree)
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        reg = _registry()
        monkeypatch.setattr(entity_seat, "read_seat", lambda d, s: {"run_id": "root"})
        first = client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "stop"}).json()
        assert first["complete"] is False
        monkeypatch.setattr(entity_seat, "read_seat", lambda d, s: None)  # the root left discovery
        second = client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "stop"}).json()
        assert calls == ["root", "child"]
        assert second["complete"] is True
        assert entity_phase.read_record(reg.entities_dir, "castor")["stop_outstanding"] == []


def test_nothing_gives_own_time_back_across_a_stop(monkeypatch: pytest.MonkeyPatch):
    """Codex review 2 #7: the deprecated grant route armed personal while stopped and Resume then
    reconciled into own time. The legacy route is stopped-aware like the toggles, and Resume lands
    in idle whatever was armed meanwhile."""
    from abstractruntime.identity.life import read_personal_grant, write_personal_grant

    from abstractgateway import entity_phase

    spawned = []
    monkeypatch.setattr(entity_phase, "start_own_time",
                        lambda r, m, p, return_to=None, *, fence: spawned.append(m.slug) or {"pid": 1})
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        assert client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "stop"}).status_code == 200
        r = client.put("/api/gateway/entities/Castor/personal-grant", json={"mode": "until_revoked"})
        assert r.status_code == 409 and r.json()["detail"]["reason_code"] == "stopped_use_resume", r.text
        assert read_personal_grant(_home_dir())["mode"] == "disabled"

        write_personal_grant(_home_dir(), mode="until_revoked", granted_by="someone", fence=_LF.admit(_home_dir()))  # armed by another path
        entity_phase.set_resume_to(_registry(), "castor", "personal")
        back = client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "resume"})
        assert back.status_code == 200, back.text
        assert back.json()["phase"] == "idle" and back.json()["toggles"]["personal"] is False
        assert read_personal_grant(_home_dir())["mode"] == "disabled" and spawned == []


def test_an_unverifiable_loop_is_never_taken_for_dead(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Codex review 2 #4: EPERM on the liveness probe, or an unreadable incarnation token, made the
    Stop report the loop as not running. Unknown is incomplete: no signal, a failed row, retried."""
    import os

    import abstractruntime.identity.life as life

    from abstractgateway import entity_loop, entity_phase

    home = tmp_path / "castor"
    home.mkdir()
    proc = _fake_loop(home)
    signalled = []
    monkeypatch.setattr(entity_loop, "freeze_loop", lambda home_dir, **kw: signalled.append(1) or {"killed": True})
    try:
        real_kill = os.kill

        def _eperm(pid, sig):
            if pid == proc.pid:
                raise PermissionError(1, "Operation not permitted")
            return real_kill(pid, sig)

        monkeypatch.setattr(os, "kill", _eperm)
        row = entity_phase._kill_loop(home, {}, "test")
        assert (row["outcome"], row["code"]) == ("failed", "loop_unverified") and signalled == []
        monkeypatch.setattr(os, "kill", real_kill)

        monkeypatch.setattr(life, "_pid_start_time", lambda pid: None)
        row = entity_phase._kill_loop(home, {}, "test")
        assert (row["outcome"], row["code"]) == ("failed", "loop_unverified") and signalled == []
    finally:
        proc.kill()


def test_the_stop_receipt_lists_only_the_runs_it_found_live(monkeypatch: pytest.MonkeyPatch):
    """The summon seat outlives its run: a Stop after a finished summon listed that run (and a
    pruned one) in its receipt. Only the runs the Stop found live are listed: the open visit it
    cancelled, never a run that had already ended or is gone."""
    from abstractruntime.core.models import RunState, RunStatus

    from abstractgateway import entity_chat, entity_phase
    from abstractgateway.entity_seat import record_seat

    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: _ScriptedLLM(["(unused)"]))
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        _runtime, store = entity_phase._run_owner(_registry(), "castor", "gateway")
        store.save(RunState(run_id="summon-done", workflow_id="w", status=RunStatus.COMPLETED, current_node="end", vars={}))
        record_seat(_registry().entities_dir, "castor", run_id="summon-done", session_id="s-1",
                    holder="agent:coder", holder_kind="agent")
        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert opened.status_code == 200, opened.text
        visit_run = opened.json()["run_id"]
        stop = client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "stop"})
        assert stop.status_code == 200, stop.text
        runs = [i for i in stop.json()["items"] if i["item"] == "run"]
        assert runs and all(i["outcome"] in ("cancelled", "failed") for i in runs), runs
        assert "summon-done" not in {i["id"] for i in runs}
        assert visit_run in {i["id"] for i in runs} and stop.json()["cancelled"]["runs"] == [i["id"] for i in runs
                                                                                            if i["outcome"] == "cancelled"]
        # The seat names a run that is gone (pruned): nothing is listed for it either.
        assert client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "resume"}).status_code == 200
        record_seat(_registry().entities_dir, "castor", run_id="pruned-run", session_id="s-2",
                    holder="agent:coder", holder_kind="agent")
        again = client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "stop"})
        assert [i for i in again.json()["items"] if i["item"] == "run"] == []


def test_resume_keeps_its_reason_as_stop_keeps_its_own():
    """Resume dropped the operator's reason (the state said only "resumed [by ...]"). It is kept,
    in the state's reason and on the wake marker, on both doors (lifecycle and the legacy state)."""
    from abstractruntime.identity.life import read_entity_state

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        assert client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "stop", "reason": "a test"}).status_code == 200
        r = client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "resume", "reason": "back to work"})
        assert r.status_code == 200, r.text
        st = read_entity_state(_home_dir())
        assert st["state"] == "awake" and st["reason"].startswith("resumed: back to work [by person:")
        path = _home_dir().parent / ".host_stream" / "castor.jsonl"
        wakes = [json.loads(x)["payload"] for x in path.read_text(encoding="utf-8").splitlines()
                 if x.strip() and (json.loads(x).get("payload") or {}).get("kind") == "wake"]
        assert wakes[-1]["reason"] == "back to work" and wakes[-1]["prior_state"] == "paused"
        assert client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "stop"}).status_code == 200
        legacy = client.post("/api/gateway/entities/Castor/state", json={"state": "awake", "reason": "by the old door"})
        assert legacy.status_code == 200, legacy.text
        assert read_entity_state(_home_dir())["reason"].startswith("resumed: by the old door [by person:")
        # No reason: the plain word, as before.
        assert client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "stop"}).status_code == 200
        assert client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "resume"}).status_code == 200
        assert read_entity_state(_home_dir())["reason"].startswith("resumed [by person:")
