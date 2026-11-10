"""The gateway half of the Stop fence (STOP-LANE-DESIGN 2026-11-09 §2.2, §3 T9/T10; Codex review 3 of the gateway).
Every test lands the operator's Stop at the precise point the review names and asserts what must not happen after it:
no model call, no write, no live session, no restore over the Stop. Hermetic gateway, scripted minds; only the loop's
process creation is stubbed (conftest)."""

from __future__ import annotations

import copy
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractruntime.identity.visit_workflow")

_TOKEN = "entity-stop-fence-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setattr("abstractgateway.entity_chat.gateway_text_mind",
                        lambda: {"provider": "lmstudio", "model": "test-model", "base_url": None, "reasoning": None})


class _ScriptedLLM:
    class _Reply:
        def __init__(self, content: str) -> None:
            self.content = content

    def __init__(self, replies) -> None:
        self._replies = list(replies)
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        if not self._replies:
            raise AssertionError("scripted LLM exhausted: a model call happened after the Stop")
        return self._Reply(self._replies.pop(0))


def _install_llm(monkeypatch, replies) -> _ScriptedLLM:
    from abstractgateway import entity_chat

    llm = _ScriptedLLM(replies)
    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: llm)
    return llm


def _spark(name: str) -> dict:
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


def _home(slug: str = "castor") -> Path:
    return _registry().entities_dir / slug


def _born(client: TestClient, *names: str) -> None:
    for name in names:
        assert client.post("/api/gateway/entities", json={"name": name, "spark": _spark(name)}).status_code == 201


def _stop(slug: str = "castor") -> dict:
    from abstractgateway import entity_phase
    from abstractgateway.routes.entities import _chat_host, _visit_host

    return entity_phase.stop(_registry(), slug, actor="person:admin", chat_host=_chat_host(), visit_host=_visit_host())


def _memory_dump(slug: str = "castor") -> str:
    """Every row of the home's memory as SQL (an UPDATE shows, not only a count)."""
    from abstractgateway.entities import MEMORY_FILENAME

    con = sqlite3.connect(str(_home(slug) / MEMORY_FILENAME))
    try:
        return "\n".join(con.iterdump())
    finally:
        con.close()


def _history_lines(slug: str = "castor") -> int:
    path = _home(slug) / "state_history.jsonl"
    return path.read_text(encoding="utf-8").count("\n") if path.exists() else 0


# ------------------------------------------------------------------------------------------------ finding 12
def test_two_concurrent_starts_create_one_loop_process(loop_spawns):
    """Codex review 3 #12: the tests replaced `spawn_loop_process` whole, so its admission (the spawn lock, the start
    refusal, the reservation) never ran in them. Only process creation is stubbed now: two concurrent starts through
    the gateway's own entry create ONE process; the runtime's admission refuses the other."""
    from abstractruntime.identity.life import read_loop_status, write_personal_grant
    from abstractruntime.identity.lifecycle_fence import LifecycleFence

    from abstractgateway.entity_loop import start_loop

    with _client() as client:
        _born(client, "Castor")
        write_personal_grant(_home(), mode="until_revoked", granted_by="person:admin", fence=LifecycleFence.admit(_home()))
        gate = threading.Barrier(2)
        results = []

        def _start():
            gate.wait()
            try:
                results.append(start_loop(_home(), provider="lmstudio", model="test-model"))
            except Exception as e:  # noqa: BLE001
                results.append(e)

        threads = [threading.Thread(target=_start) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert len(loop_spawns) == 1, loop_spawns
        started = [r for r in results if isinstance(r, dict)]
        refused = [r for r in results if isinstance(r, Exception)]
        assert len(started) == 1 and len(refused) == 1, results
        assert "already" in str(refused[0]), refused[0]
        assert read_loop_status(_home())["phase"] == "starting"  # the reservation names the child, no clock


def test_the_spawn_admission_runs_a_job_starts_without_a_grant_nothing_without_either(loop_spawns):
    """Codex review 3 #12: a queued job starts the loop whatever the personal grant says (v3 D1); with neither a grant
    nor a job the runtime refuses synchronously and no process is created. Through the real spawn path."""
    from abstractgateway.entity_loop import start_loop

    with _client() as client:
        _born(client, "Castor")
        with pytest.raises(RuntimeError, match="no personal time"):
            start_loop(_home(), provider="lmstudio", model="test-model")
        assert loop_spawns == []
        (_home() / "work_order.md").write_text("Sort the archive.\n", encoding="utf-8")
        assert start_loop(_home(), provider="lmstudio", model="test-model")["pid"]
        assert len(loop_spawns) == 1


# ------------------------------------------------------------------------------------------- T9 / finding 2
def test_a_source_stopped_after_the_last_check_carries_nothing_into_the_visit(monkeypatch):
    """Codex review 3 #2 (T9): the mention mirror checked the SOURCE's generation, then ran the visited entity's
    turn: a Stop of the source landing after that last check and before the target's turn is admitted still carried
    his words in and made the visited entity answer them. The source's fence now travels with the act: the visit door
    re-checks it under the target's writer lease, right before it runs anything."""
    from abstractgateway.entity_visits import EntityVisitHost

    llm = _install_llm(monkeypatch, [
        "Nerin: hello.",               # nerin, the person's own turn
        "Castor: the bridge holds.",   # castor, the mention
        "Nerin: thank you, Castor.",   # nerin answering the mirrored words: must never be asked
    ])
    real_turn = EntityVisitHost.turn
    fired = []

    def _turn(self, name, run_id, **kw):
        if kw.get("speaker") == "entity:castor" and not fired:
            fired.append(_stop("castor"))  # the Stop lands after the mirror's last check, before the turn
        return real_turn(self, name, run_id, **kw)

    with _client() as client:
        _born(client, "Nerin", "Castor")
        opened = client.post("/api/gateway/entities/Nerin/visit/open", json={}).json()
        sid, nerin_run = opened["session_id"], opened["run_id"]
        assert client.post(f"/api/gateway/entities/Nerin/visit/{nerin_run}/turn",
                           json={"text": "@Castor does the bridge hold?"}).status_code == 200
        er = _registry().get_entity_runtime("nerin")
        history_before = list(er.run_store.load(nerin_run).vars["_visit"]["history"])

        monkeypatch.setattr(EntityVisitHost, "turn", _turn)
        r = client.post(f"/api/gateway/sessions/{sid}/mentions",
                        json={"entity": "Castor", "text": "@Castor does the bridge hold?", "mention_index": 0})
        assert fired, "the mirror never reached the visited entity's turn"
        assert r.status_code == 200, r.text
        (mirror,) = r.json()["mirrors"]
        assert mirror["reply"] is None and "was stopped; nothing was carried" in mirror["error"], mirror
        assert len(llm.calls) == 2, "the visited entity answered the stopped entity's words"
        run = er.run_store.load(nerin_run)
        assert run.vars["_visit"]["history"] == history_before
        assert str(getattr(run.status, "value", run.status)) == "waiting"  # Nerin's own visit goes on


# ------------------------------------------------------------------------------------------------- finding 4
def test_a_drain_admitted_before_a_stop_never_runs_after_it(monkeypatch):
    """Codex review 3 #4: the authoring drainer read its generation AFTER the paused check, the lease and the runtime
    lookup, so a Stop landing in between became the drain's own generation and it ran while paused (`should_stop()`
    False). The admission is now read first and revalidated under the lease."""
    import abstractruntime.identity.authoring as authoring
    import abstractruntime.identity.cognition_profile as cognition_profile

    from abstractgateway.entity_authoring import drain_after_close

    drained = []
    monkeypatch.setattr(authoring, "drain_authoring",
                        lambda *a, **kw: drained.append(kw["should_stop"]()) or {"considered": 0})
    real_policy = cognition_profile.rebuild_policy
    fired = []

    def _policy_then_stop(home_dir):
        if not fired:
            fired.append(_stop("castor"))  # between the initial state check and the runtime lookup
        return real_policy(home_dir)

    with _client() as client:
        _born(client, "Castor")
        monkeypatch.setattr(cognition_profile, "rebuild_policy", _policy_then_stop)
        out = drain_after_close(_registry(), "castor")
        assert fired
        assert out == {"skipped": "stopped"}, out
        assert drained == [], "the drainer ran after the operator's Stop"


# ------------------------------------------------------------------------------------------------- finding 5
def test_a_stop_during_the_night_refuses_every_later_write_of_the_pass(monkeypatch):
    """Codex review 3 #5: the night's persistence was not fenced: memory's `cancelled=` only guards its completion
    marker. The pass runs under its fence (the shared home's commit guard, step 1): after a Stop landing inside the
    final stage no completion is recorded AND any further write of the pass is refused inside its transaction."""
    import abstractmemory.maintenance as night
    from abstractmemory import MemoryRecordInput
    from abstractruntime.identity.lifecycle_fence import StoppedByOperator

    from abstractgateway import entity_idle_sleep, entity_phase
    from abstractgateway.entities import LIFE_SCOPE

    real_dream = night.dream_pass
    seen = {}
    with _client() as client:
        _born(client, "Castor")
        reg = _registry()
        with entity_phase.fenced_here(reg.entities_dir, "castor") as rec:
            rec["last_maintenance"] = {"generation": 0, "status": "done", "error": None,
                                       "at": (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()}
            entity_phase.write_record(reg.entities_dir, "castor", rec)

        def _dream_then_stop(system, *args, **kwargs):
            out = real_dream(system, *args, **kwargs)
            assert _stop("castor")["action"] == "stop"
            seen["after_stop"] = _memory_dump()
            try:
                system.remember(MemoryRecordInput(kind="episode", title="after the Stop", digest="written after the Stop."),
                                scope=LIFE_SCOPE, owner_id="entity:castor", idempotency_key="after-the-stop")
                seen["write"] = "landed"
            except StoppedByOperator:
                seen["write"] = "refused"
            return out

        monkeypatch.setattr(night, "dream_pass", _dream_then_stop)
        out = entity_idle_sleep.idle_sweep_one(reg, "castor")
        assert seen.get("write") == "refused", seen.get("write")
        assert _memory_dump() == seen["after_stop"], "the night wrote after the operator's Stop"
        assert out["maintenance"] == "stopped", out
