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
                results.append(start_loop(_home(), provider="lmstudio", model="test-model",
                                          fence=LifecycleFence.admit(_home())))
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
    from abstractruntime.identity.lifecycle_fence import LifecycleFence

    from abstractgateway.entity_loop import start_loop

    with _client() as client:
        _born(client, "Castor")
        with pytest.raises(RuntimeError, match="no personal time"):
            start_loop(_home(), provider="lmstudio", model="test-model", fence=LifecycleFence.admit(_home()))
        assert loop_spawns == []
        (_home() / "work_order.md").write_text("Sort the archive.\n", encoding="utf-8")
        assert start_loop(_home(), provider="lmstudio", model="test-model", fence=LifecycleFence.admit(_home()))["pid"]
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


# ------------------------------------------------------------------------------------------------- finding 8
def test_a_stop_while_the_chat_opens_leaves_no_session_lease_or_effect(monkeypatch):
    """Codex review 3 #8: the hosted chat registered its session after the last admission check, the session's
    construction and the pending look-back: a Stop landing there missed the session, which was then returned open.
    Registration is now fenced on the original admission; a refused session is disposed."""
    from abstractruntime.identity.chat import ChatSession
    from abstractruntime.identity.life import read_entity_state
    from abstractruntime.storage.lease import acquire_directory_lease

    from abstractgateway.routes.entities import _chat_host

    llm = _install_llm(monkeypatch, [])
    real_lookback = ChatSession.run_pending_lookback
    fired = []

    def _stop_then_lookback(self):
        if not fired:
            fired.append(_stop("castor"))  # after the session's construction, before its registration
        return real_lookback(self)

    with _client() as client:
        _born(client, "Castor")
        monkeypatch.setattr(ChatSession, "run_pending_lookback", _stop_then_lookback)
        r = client.post("/api/gateway/entities/Castor/chat/open", json={})
        assert fired
        assert r.status_code == 409, r.text
        assert not _chat_host().has_open("castor"), "a session opened across the Stop is live"
        acquire_directory_lease(_home(), holder="test").release()  # the visit's writer lease was released
        assert read_entity_state(_home())["state"] == "paused"
        assert llm.calls == []
        stream = _home().parent / ".host_stream" / "castor.jsonl"
        assert '"summon"' not in (stream.read_text(encoding="utf-8") if stream.exists() else "")


# ----------------------------------------------------------------------------------------------------- T10
@pytest.mark.parametrize("lane", ["visit", "chat"])
def test_a_visit_closed_after_a_stop_restores_nothing(monkeypatch, lane):
    """T10: a visit's close restores the state it took (by its ownership token), but never over a Stop since the visit
    was admitted. The runtime's own Stop (`hard_stop_loop`, a CLI) moves the generation without writing paused: the
    posture still reads as the visit's, and only the visit's fence keeps the close from writing over the Stop."""
    from abstractruntime.identity.life import hard_stop_loop, read_entity_state

    _install_llm(monkeypatch, [])
    with _client() as client:
        _born(client, "Castor")
        if lane == "visit":
            run_id = client.post("/api/gateway/entities/Castor/visit/open", json={}).json()["run_id"]
        else:
            chat_id = client.post("/api/gateway/entities/Castor/chat/open", json={}).json()["chat_id"]
        posture = read_entity_state(_home())
        assert posture["mode"] == "visiting"
        lines = _history_lines()
        hard_stop_loop(_home(), reason="stopped from the command line", requested_by="admin")
        if lane == "visit":
            client.post(f"/api/gateway/entities/Castor/visit/{run_id}/close", json={"closed_by": "operator"})
        else:
            client.post(f"/api/gateway/entities/Castor/chat/{chat_id}/close", json={})
        after = read_entity_state(_home())
        assert (after["state"], after.get("mode"), after["changed_at"]) == (
            posture["state"], posture.get("mode"), posture["changed_at"]), after
        assert _history_lines() == lines, "the close wrote a state over the Stop"


# ------------------------------------------------------------------------------------------------- finding 3
def test_a_busy_visited_home_never_holds_up_the_mentioned_entitys_stop(monkeypatch):
    """Codex review 3 #3: the mirror's admission held the mentioned entity's lifecycle lock while it waited for the
    visited home's writer lease, so a busy visit blocked that entity's Stop. The lease is now waited for outside every
    lifecycle lock (the wait ends on the Stop), and only the brief check-and-join runs under it."""
    import time

    from abstractruntime.storage.lease import acquire_directory_lease

    llm = _install_llm(monkeypatch, ["Nerin: hello.", "Castor: the bridge holds."])
    with _client() as client:
        _born(client, "Nerin", "Castor")
        opened = client.post("/api/gateway/entities/Nerin/visit/open", json={}).json()
        sid, nerin_run = opened["session_id"], opened["run_id"]
        assert client.post(f"/api/gateway/entities/Nerin/visit/{nerin_run}/turn",
                           json={"text": "@Castor does the bridge hold?"}).status_code == 200
        busy = acquire_directory_lease(_home("nerin"), holder="test: the visited home is busy")
        out = {}
        try:
            mention = threading.Thread(target=lambda: out.__setitem__("r", client.post(
                f"/api/gateway/sessions/{sid}/mentions",
                json={"entity": "Castor", "text": "@Castor does the bridge hold?", "mention_index": 0})), daemon=True)
            mention.start()
            deadline = time.monotonic() + 120
            while len(llm.calls) < 2 and time.monotonic() < deadline:
                time.sleep(0.05)  # Castor answered: the mirror is next, and Nerin's home is held
            assert len(llm.calls) == 2
            time.sleep(1.0)  # the mirror is waiting for Nerin's writer lease
            stopper = threading.Thread(target=lambda: out.__setitem__("stop", _stop("castor")), daemon=True)
            stopper.start()
            stopper.join(30)
            assert "stop" in out, "the Stop of the mentioned entity waited for the visited home's writer"
        finally:
            busy.release()
        mention.join(60)
        r = out["r"]
        assert r.status_code == 200, r.text
        (mirror,) = r.json()["mirrors"]
        assert mirror["reply"] is None and "was stopped; nothing was carried" in mirror["error"], mirror
        assert len(llm.calls) == 2


# ------------------------------------------------------------------------------------------------- finding 9
@pytest.mark.parametrize("kind", ["denied", "malformed"])
def test_an_unreadable_status_is_never_taken_for_a_stopped_loop(kind):
    """Codex review 3 #9: a loop status the gateway cannot read (a denied permission) or parse (a torn file) was taken
    for "not running" and the Stop reported the loop stopped. Its process may well be alive: the row is now `failed
    loop_unverified` (the Stop is incomplete; a repeated Stop retries) and nothing is signalled."""
    import os

    from abstractruntime.identity.life import write_personal_grant
    from abstractruntime.identity.lifecycle_fence import LifecycleFence

    from entity_fake_loop import spawn

    with _client() as client:
        _born(client, "Castor")
        write_personal_grant(_home(), mode="until_revoked", granted_by="person:admin", fence=LifecycleFence.admit(_home()))
        proc = spawn(_home(), "day")
        status = _home() / "loop_status"
        try:
            if kind == "denied":
                os.chmod(status, 0)
            else:
                status.write_text('{"phase": "day", "pid": ', encoding="utf-8")
            r = client.post("/api/gateway/entities/Castor/lifecycle", json={"action": "stop"})
            assert r.status_code == 200, r.text
            body = r.json()
            (loop_row,) = [i for i in body["items"] if i["item"] == "loop"]
            assert (loop_row["outcome"], loop_row["code"]) == ("failed", "loop_unverified"), loop_row
            assert body["complete"] is False
            assert proc.poll() is None, "a process the gateway could not verify was signalled"
        finally:
            os.chmod(status, 0o644)
            proc.kill()


# ------------------------------------------------------------------------------------------------- finding 6
class _EchoLLM:
    class _Reply:
        def __init__(self, content: str) -> None:
            self.content = content

    def generate(self, **kwargs):
        return self._Reply("ok.")


def test_a_stop_then_resume_during_the_last_wait_interval_ends_the_open(monkeypatch):
    """Codex review 3 #6: the wait checked the generation only while the blocker held; a Stop removing the blocker (it
    closes the other visit) then a Resume, both within the last polling interval, let the open go on - with a fresh
    generation read after the wait. One admission now runs through every wait and is checked on exit."""
    import time as time_mod

    from abstractgateway import entity_chat, entity_phase
    from abstractgateway.entity_visits import VisitRefused
    from abstractgateway.routes.entities import _chat_host, _visit_host

    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: _EchoLLM())
    real_sleep = time_mod.sleep
    waiter = {}

    def _sleep(seconds):
        if threading.current_thread() is waiter.get("thread") and not waiter.get("fired"):
            waiter["fired"] = True
            _stop("castor")  # ends Bob's visit: the blocker is gone ...
            entity_phase.resume(_registry(), "castor", actor="person:admin", chat_host=_chat_host(),
                                visit_host=_visit_host())  # ... and he is resumed, inside the same interval
        return real_sleep(seconds)

    with _client() as client:
        _born(client, "Castor")
        vh = _visit_host()
        vh.open("castor", participants=["person:bob"])
        monkeypatch.setattr(time_mod, "sleep", _sleep)

        def _open():
            try:
                waiter["opened"] = vh.open("castor", participants=["person:alice"])
            except VisitRefused as e:
                waiter["refused"] = e

        t = threading.Thread(target=_open, daemon=True)
        waiter["thread"] = t
        t.start()
        t.join(120)
        assert waiter.get("fired"), "the open never waited"
        assert "opened" not in waiter, "an open admitted before the Stop went on after it"
        assert waiter["refused"].code == "stopped_use_resume"


def test_an_older_waiter_opens_before_the_newcomers(monkeypatch):
    """Codex review 3 #6: polling plus an unordered mutex let a later arrival overtake a visitor already waiting.
    Arrivals are served oldest first: here the newcomer even polls fifty times as often, and still goes after."""
    import time as time_mod

    from abstractgateway import entity_chat
    from abstractgateway.entity_visits import ARRIVALS, VisitRefused
    from abstractgateway.routes.entities import _visit_host

    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: _EchoLLM())
    real_sleep = time_mod.sleep
    threads = {}

    def _sleep(seconds):
        me = threading.current_thread()
        if me is threads.get("alice"):
            return real_sleep(1.0)
        if me is threads.get("carol"):
            return real_sleep(0.02)
        return real_sleep(seconds)

    def _until(cond, what):
        deadline = time_mod.monotonic() + 120
        while not cond():
            assert time_mod.monotonic() < deadline, what
            real_sleep(0.05)

    with _client() as client:
        _born(client, "Castor")
        vh = _visit_host()
        bob = vh.open("castor", participants=["person:bob"])
        key = str(_home())
        order = []
        monkeypatch.setattr(time_mod, "sleep", _sleep)

        def _open(who):
            try:
                out = vh.open("castor", participants=[f"person:{who}"])
                order.append((who, out["run_id"]))
            except VisitRefused as e:
                order.append((who, e.code))

        for who, n in (("alice", 1), ("carol", 2)):
            threads[who] = threading.Thread(target=_open, args=(who,), daemon=True)
            threads[who].start()
            _until(lambda n=n: ARRIVALS.waiting(key) == n, f"{who} never arrived")
        vh.close("castor", bob["run_id"], closed_by="operator")
        _until(lambda: len(order) >= 1, "nobody opened after the visit closed")
        assert order[0][0] == "alice", order
        vh.close("castor", order[0][1], closed_by="operator")
        _until(lambda: len(order) >= 2, "the newcomer never opened")
        assert order[1][0] == "carol", order
        for t in threads.values():
            t.join(60)


# ------------------------------------------------------------------------------------------------- finding 7
def test_the_same_visitor_hands_over_a_visit_parked_on_an_approval(monkeypatch):
    """Codex review 3 #7: the same visitor opening ANOTHER conversation hands his visit over, but the handover always
    resumed the visit's visitor wait: a visit parked on a tool approval (or an ask_user question) answered
    `not_waiting` and the new conversation got no visit. The pending interaction is now settled explicitly (the run
    is cancelled with the handover's reason, the approved-or-not tool never runs), its history and records stay, and
    the new conversation's visit opens."""
    from types import SimpleNamespace

    from abstractgateway import entity_chat

    class _LLM:
        def __init__(self) -> None:
            self.calls = 0

        def generate(self, **kwargs):
            self.calls += 1
            tools = [{"name": "read_memory", "arguments": {"tag": "missing"}, "call_id": "memory-call"}] if self.calls == 1 else None
            return SimpleNamespace(content="" if tools else "A completed answer.", tool_calls=tools,
                                   finish_reason="tool_calls" if tools else "stop", model="test-model",
                                   usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})

    llm = _LLM()
    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: llm)
    with _client() as client:
        _born(client, "Castor")
        base = "/api/gateway/entities/castor"
        opened = client.post(base + "/visit/open",
                             json={"session_id": "conv-a", "tool_policy": {"modes": {"read_memory": "approve"}}})
        assert opened.status_code == 200, opened.text
        run_a = opened.json()["run_id"]
        assert client.post(f"{base}/visit/{run_a}/turn", json={"text": "Read that memory."}).status_code == 200
        assert client.get(f"{base}/visit/{run_a}/state").json()["pending_wait"] is not None  # parked on the approval

        second = client.post(base + "/visit/open", json={"session_id": "conv-b"})
        assert second.status_code == 200, second.text
        assert second.json()["run_id"] != run_a and second.json()["session_id"] == "conv-b"
        er = _registry().get_entity_runtime("castor")
        old = er.run_store.load(run_a)
        assert str(getattr(old.status, "value", old.status)) == "cancelled"
        assert "another conversation" in str(old.error or "")
        assert "Read that memory." in str(er.ledger_store.list(run_a))  # his words and what ran stay readable
        assert llm.calls == 1, "the tool waiting for its approval ran, or the old visit thought again"


# ------------------------------------------------------------------------------- an unreadable Core settings file
def test_an_unreadable_core_settings_file_is_a_503_naming_it_and_the_rest_keeps_serving(monkeypatch):
    """AbstractCore refuses an unparseable settings file (abstractcore 8b970550 `ConfigParseError`; it never runs on
    defaults). The routes that read Core settings answered 500 "Internal error"; they answer 503 naming the file and
    the parse error, and the rest of the gateway keeps serving."""
    import os

    cfg = Path(os.environ["ABSTRACTCORE_CONFIG_FILE"])  # the scratch HOME's Core store (conftest)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("{ not json", encoding="utf-8")
    with _client() as client:
        r = client.get("/api/gateway/config/capability-defaults")
        assert r.status_code == 503, r.text
        body = r.json()
        assert body["code"] == "core_config_unreadable"
        assert str(cfg) in body["detail"] and "not valid JSON" in body["detail"]
        assert body["path"] == str(cfg) and "Expecting property name" in body["error"]
        assert client.get("/api/gateway/entities").status_code == 200  # the rest keeps serving
    assert cfg.read_text(encoding="utf-8") == "{ not json"  # left exactly as it is
