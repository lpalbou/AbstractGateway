"""Durable visit runs behind the door (GW-C endpoint half, items 7/9/10).

Runtime pins the workflow's internals (test_visit_workflow.py); these tests
pin the DOOR: open stamps a run in the per-entity runtime and parks it,
turn serves the reply from the run's own durable history, the run SURVIVES
the host forgetting everything in memory (the restart story: a fresh host
rebuilds the spec from the run's verified stamp), one-life-one-visit holds
DURABLY, a held home refuses the turn naming the writer, close reflects +
wakes + marks, and pause-close refuses honestly until the skip_reflection
route exists.
"""

from __future__ import annotations

import copy
import json

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractruntime.identity.visit_workflow")

_TOKEN = "entity-visit-shared-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    # The operator's substrate choice for this suite (scripted factory patched in).
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER", "lmstudio")
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CHAT_MODEL", "test-model")


def _spark(name: str = "Castor") -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


class _ScriptedLLM:
    """Raises on an empty script (adversary find: a silent fallback reply
    absorbed call-count drift — two pause tests claimed 'completion proves
    reflection was skipped' while the fallback would have answered the
    reflection call happily)."""

    class _Reply:
        def __init__(self, content: str) -> None:
            self.content = content

    def __init__(self, replies) -> None:
        self._replies = list(replies)

    def generate(self, **kwargs):
        if not self._replies:
            raise AssertionError("scripted LLM exhausted — an unexpected extra call happened")
        return self._Reply(self._replies.pop(0))


def _install_scripted_llm(monkeypatch: pytest.MonkeyPatch, replies) -> None:
    from abstractgateway import entity_chat

    llm = _ScriptedLLM(replies)
    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: llm)


def _home_dir(name: str = "Castor"):
    from pathlib import Path

    from abstractgateway.service import get_gateway_service

    return Path(get_gateway_service().config.data_dir) / "entities" / name.lower()


def _store_bytes_checkpointed(path) -> bytes:
    """The run store's FULL at-rest bytes: WAL-checkpointed first, so the
    grep sees recent writes (they rest in the -wal sidecar until then —
    both files travel on directory copy, so both are at-rest surfaces)."""
    import sqlite3

    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return path.read_bytes()


def test_visit_open_turn_close_full_cycle(monkeypatch: pytest.MonkeyPatch):
    _install_scripted_llm(monkeypatch, [
        "Hello Laurent — I am here.",
        "Looking back: a good first durable visit.",  # reflection
    ])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert opened.status_code == 200, opened.text
        body = opened.json()
        run_id = body["run_id"]
        assert body["visit_id"].startswith("visit-")
        assert body["entity_id"] == "entity:castor"
        assert "person:admin" in body["participants"]  # verified principal stamped
        assert "entity:castor" in body["participants"]       # explicit co-presence

        status = client.get("/api/gateway/entities/Castor/visit").json()
        assert status["open"] is True and status["run_id"] == run_id

        turned = client.post(
            f"/api/gateway/entities/Castor/visit/{run_id}/turn",
            json={"text": "Hello — are you there?"},
        )
        assert turned.status_code == 200, turned.text
        turn_body = turned.json()
        assert "Hello Laurent" in turn_body["reply"]
        assert turn_body["turn_n"] == 1
        assert turn_body["status"] == "waiting"  # parked again

        closed = client.post(
            f"/api/gateway/entities/Castor/visit/{run_id}/close",
            json={"closed_by": "operator", "reason": "test done"},
        )
        assert closed.status_code == 200, closed.text
        close_body = closed.json()
        assert close_body["status"] == "completed"
        assert close_body["output"]["turns"] == 1

        # Terminal duties: no open visit; summon + session_closed markers on the stream.
        assert client.get("/api/gateway/entities/Castor/visit").json()["open"] is False
        stream = (_home_dir().parent / ".host_stream" / "castor.jsonl").read_text(encoding="utf-8")
        kinds = [json.loads(line).get("payload", {}).get("kind") for line in stream.splitlines()]
        assert "summon" in kinds and "session_closed" in kinds


def test_reaper_closes_an_abandoned_visit_and_unstrands_the_home(monkeypatch: pytest.MonkeyPatch):
    """The stranded auto-yield (entity forensics c2465 ask 1): an abandoned
    browser visit used to hold state=asleep(auto-yield) FOREVER — the D3
    idle deadline only fired at the next door touch, new opens 409'd, and
    the entity was locked out of personal time. The reaper's sweep fires a
    DUE deadline with no client alive: graceful close (reflection runs),
    state restored, and the home opens again."""
    _install_scripted_llm(monkeypatch, [
        "Hello.",
        "Reflection: a short visit, honestly closed.",  # the ruled idle-close reflection
    ])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        run_id = client.post("/api/gateway/entities/Castor/visit/open", json={}).json()["run_id"]
        assert client.post(
            f"/api/gateway/entities/Castor/visit/{run_id}/turn", json={"text": "Hi."}
        ).status_code == 200

        from abstractgateway.service import get_gateway_service
        from abstractgateway.routes.entities import _visit_host

        host = _visit_host()
        registry = get_gateway_service().entity_registry

        # Abandonment: nothing touches the door. Move the parked run's idle
        # deadline into the past (the door seeded ~1h; the test cannot wait).
        er = registry.get_entity_runtime("castor")
        run = er.run_store.load(run_id)
        assert run is not None and run.waiting is not None and run.waiting.until
        run.waiting.until = "2020-01-01T00:00:00+00:00"
        er.run_store.save(run)

        driven = host.reap_now()
        assert driven >= 1, "the due visit must be driven without any client"

        # The visit closed gracefully: terminal run, idle close reason, no
        # open visit on the home, and a session_closed marker on the stream.
        assert client.get("/api/gateway/entities/Castor/visit").json()["open"] is False
        final = er.run_store.load(run_id)
        assert str(getattr(final.status, "value", final.status)) == "completed"
        assert str((final.output or {}).get("close_reason") or "") == "idle_timeout"
        stream = (_home_dir().parent / ".host_stream" / "castor.jsonl").read_text(encoding="utf-8")
        kinds = [json.loads(line).get("payload", {}).get("kind") for line in stream.splitlines()]
        assert "session_closed" in kinds

        # Un-stranded: a NEW visit opens (one-life-one-visit no longer held
        # by the abandoned run).
        _install_scripted_llm(monkeypatch, ["Again.", "Reflection two."])
        reopened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert reopened.status_code == 200, reopened.text
        client.post(f"/api/gateway/entities/Castor/visit/{reopened.json()['run_id']}/close", json={})


def test_reaper_repairs_a_stale_orphaned_yield_posture(monkeypatch: pytest.MonkeyPatch):
    """The 20:39 incident (c2465): a hosted-chat auto-yield posture
    (asleep + mode=visiting) orphaned by a gateway restart stood for hours
    rendering as SLEEP and locking the entity out of personal time. The
    reaper restores awake — but ONLY when the posture is old, no hosted
    session is live, and no durable visit is open."""
    import time as _time

    from abstractruntime.identity.life import read_entity_state, write_entity_state

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        from abstractgateway.routes.entities import _visit_host

        host = _visit_host()
        home_dir = _home_dir()

        # The orphaned posture (what a killed gateway leaves behind).
        write_entity_state(home_dir, "asleep", reason="in conversation with person:admin (auto-yield)", mode="visiting")

        # No chat probe wired => never guess, never repair.
        host._chat_probe = None
        monkeypatch.setattr(type(host), "STALE_YIELD_GRACE_S", 0.0, raising=True)
        assert host.reap_now() == 0
        assert read_entity_state(home_dir).get("state") == "asleep"

        # Probe says a session is LIVE => posture stands (it is owned).
        host._chat_probe = lambda slug: True
        assert host.reap_now() == 0
        assert read_entity_state(home_dir).get("state") == "asleep"

        # Probe says nothing is live + posture beyond grace => repaired.
        host._chat_probe = lambda slug: False
        assert host.reap_now() == 1
        repaired = read_entity_state(home_dir)
        assert repaired.get("state") == "awake"
        assert "stale visit yield repaired" in str(repaired.get("reason") or "")

        # The repair is on the observable record (wake marker, reaper channel).
        stream = (_home_dir().parent / ".host_stream" / "castor.jsonl").read_text(encoding="utf-8")
        marks = [json.loads(line)["payload"] for line in stream.splitlines()]
        wakes = [m for m in marks if m.get("kind") == "wake" and (m.get("channel") == "reaper" or (m.get("details") or {}).get("channel") == "reaper")]
        assert wakes, "the reaper's wake must land as a host marker"

        # Fresh posture (inside the grace window) is never touched.
        monkeypatch.setattr(type(host), "STALE_YIELD_GRACE_S", 3600.0, raising=True)
        write_entity_state(home_dir, "asleep", reason="auto-yield", mode="visiting")
        assert host.reap_now() == 0
        assert read_entity_state(home_dir).get("state") == "asleep"
        _time.sleep(0)  # readability: grace is time-based, nothing to wait for here


def test_reaper_leaves_unexpired_parks_and_running_runs_alone(monkeypatch: pytest.MonkeyPatch):
    """Narrowness pins: an unexpired park is untouched (no drive, no close),
    and a crash-orphaned RUNNING run is NOT resumed by the reaper — the
    explicit /tick recovery verb owns that (the reaper never resumes
    half-driven cognition on its own clock)."""
    from abstractruntime.core.models import RunStatus

    _install_scripted_llm(monkeypatch, ["Hello."])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        run_id = client.post("/api/gateway/entities/Castor/visit/open", json={}).json()["run_id"]
        assert client.post(
            f"/api/gateway/entities/Castor/visit/{run_id}/turn", json={"text": "Hi."}
        ).status_code == 200

        from abstractgateway.service import get_gateway_service
        from abstractgateway.routes.entities import _visit_host

        host = _visit_host()
        registry = get_gateway_service().entity_registry
        er = registry.get_entity_runtime("castor")

        # Unexpired park (deadline ~1h out): sweep drives nothing.
        assert host.reap_now() == 0
        assert client.get("/api/gateway/entities/Castor/visit").json()["open"] is True

        # Crash-orphan the run (RUNNING at rest): still not the reaper's.
        run = er.run_store.load(run_id)
        run.status = RunStatus.RUNNING
        run.waiting = None
        er.run_store.save(run)
        assert host.reap_now() == 0
        assert str(er.run_store.load(run_id).status.value) == "running"


def test_visit_survives_host_amnesia(monkeypatch: pytest.MonkeyPatch):
    """The restart story at the door layer: the in-memory spec cache dies
    (fresh host), the durable run does not — turn continues the same visit
    with the spec rebuilt from the run's own verified stamp."""
    _install_scripted_llm(monkeypatch, [
        "First reply.",
        "Second reply — I still remember this visit.",
        "Reflection: continuity held.",
    ])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        run_id = client.post("/api/gateway/entities/Castor/visit/open", json={}).json()["run_id"]
        first = client.post(
            f"/api/gateway/entities/Castor/visit/{run_id}/turn", json={"text": "One."}
        ).json()
        assert first["reply"] == "First reply."

        # Simulate the host restart: forget every in-memory spec/host object.
        from abstractgateway.service import get_gateway_service

        registry = get_gateway_service().entity_registry
        registry._visit_host_singleton = None

        second = client.post(
            f"/api/gateway/entities/Castor/visit/{run_id}/turn", json={"text": "Two?"}
        )
        assert second.status_code == 200, second.text
        assert second.json()["reply"].startswith("Second reply")
        assert second.json()["turn_n"] == 2

        closed = client.post(f"/api/gateway/entities/Castor/visit/{run_id}/close", json={})
        assert closed.status_code == 200
        assert closed.json()["output"]["turns"] == 2


def test_turn_recovers_a_crash_orphaned_running_run(monkeypatch: pytest.MonkeyPatch):
    """agency c505 ask 2: a SIGKILL that lands mid-tick (post-ANSWER
    pre-park) leaves the run RUNNING, not WAITING — a plain resume 409s
    'Run is not waiting' and forces the visitor client to know the /tick
    host-internal. /turn now drives the orphan to its park FIRST, then
    accepts the message; the visitor never sees the internal."""
    from abstractruntime.core.models import RunStatus

    _install_scripted_llm(monkeypatch, ["Parked reply.", "Recovered reply.", "Reflection."])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        run_id = client.post("/api/gateway/entities/Castor/visit/open", json={}).json()["run_id"]

        # Forge the crash residue: a parked run flipped back to RUNNING in
        # the durable store (what a mid-tick SIGKILL leaves behind).
        from abstractgateway.service import get_gateway_service

        registry = get_gateway_service().entity_registry
        er = registry.get_entity_runtime("castor")
        run = er.run_store.load(run_id)
        run.status = RunStatus.RUNNING
        er.run_store.save(run)
        registry._visit_host_singleton = None  # fresh host too

        turned = client.post(
            f"/api/gateway/entities/Castor/visit/{run_id}/turn", json={"text": "Still there?"}
        )
        assert turned.status_code == 200, turned.text  # not a 409
        assert turned.json()["status"] == "waiting"  # driven to park, then took the turn
        client.post(f"/api/gateway/entities/Castor/visit/{run_id}/close", json={})


def test_one_life_gate_consults_the_durable_store_after_amnesia(monkeypatch: pytest.MonkeyPatch):
    """agency c505 ask 1: the one-life-one-visit gate must survive a host
    restart — a second /visit/open after the in-memory host is forgotten
    must still refuse (the durable store holds the open run), never mint a
    concurrent second visit on the home."""
    _install_scripted_llm(monkeypatch, ["Hello.", "Reflection."])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        run_id = client.post("/api/gateway/entities/Castor/visit/open", json={}).json()["run_id"]

        # Forget every in-memory host object (a full process restart).
        from abstractgateway.service import get_gateway_service

        get_gateway_service().entity_registry._visit_host_singleton = None

        # GET /visit sees the durable open run.
        status = client.get("/api/gateway/entities/Castor/visit").json()
        assert status["open"] is True and status["run_id"] == run_id

        # A second open refuses, naming the durable live run — never a second visit.
        second = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert second.status_code == 409, second.text
        assert run_id in second.json()["detail"]

        client.post(f"/api/gateway/entities/Castor/visit/{run_id}/close", json={})


def test_one_life_one_visit_is_durable(monkeypatch: pytest.MonkeyPatch):
    _install_scripted_llm(monkeypatch, ["Hi.", "Reflection."])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        first = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert first.status_code == 200
        run_id = first.json()["run_id"]

        second = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert second.status_code == 409
        assert run_id in second.json()["detail"]  # names the live run

        client.post(f"/api/gateway/entities/Castor/visit/{run_id}/close", json={})
        third = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert third.status_code == 200  # the moment the visit ends, the door opens again
        client.post(f"/api/gateway/entities/Castor/visit/{third.json()['run_id']}/close", json={})


def test_turn_refuses_while_home_has_a_writer(monkeypatch: pytest.MonkeyPatch):
    from abstractruntime.storage.lease import acquire_directory_lease

    _install_scripted_llm(monkeypatch, ["Hello.", "Reflection."])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        run_id = client.post("/api/gateway/entities/Castor/visit/open", json={}).json()["run_id"]

        incumbent = acquire_directory_lease(_home_dir(), holder="maintenance")
        try:
            refused = client.post(
                f"/api/gateway/entities/Castor/visit/{run_id}/turn", json={"text": "Hello?"}
            )
            assert refused.status_code == 409
            assert "maintenance" in refused.json()["detail"]
        finally:
            incumbent.release()

        ok = client.post(f"/api/gateway/entities/Castor/visit/{run_id}/turn", json={"text": "Hello?"})
        assert ok.status_code == 200
        client.post(f"/api/gateway/entities/Castor/visit/{run_id}/close", json={})


def test_pause_close_is_a_hard_freeze_no_reflection(monkeypatch: pytest.MonkeyPatch):
    """closed_by=pause completes WITHOUT the reflection LLM call (runtime's
    skip_reflection route, 4df23c0): a hard freeze runs no cognition. Only
    ONE reply is scripted — a reflection call would raise on the empty
    queue, so completion proves reflection was skipped."""
    _install_scripted_llm(monkeypatch, ["Hello."])  # no reflection reply
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        run_id = client.post("/api/gateway/entities/Castor/visit/open", json={}).json()["run_id"]
        client.post(f"/api/gateway/entities/Castor/visit/{run_id}/turn", json={"text": "one turn"})

        frozen = client.post(
            f"/api/gateway/entities/Castor/visit/{run_id}/close",
            json={"closed_by": "pause", "reason": "freeze"},
        )
        assert frozen.status_code == 200, frozen.text
        assert frozen.json()["status"] == "completed"


def test_tick_is_idempotent_on_parked_and_terminal_runs(monkeypatch: pytest.MonkeyPatch):
    """The step-5b drive endpoint: parked = no-op, terminal = output replay
    (the mid-chain case is the walkthrough's SIGKILL to produce)."""
    _install_scripted_llm(monkeypatch, ["Hello.", "Reflection."])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        run_id = client.post("/api/gateway/entities/Castor/visit/open", json={}).json()["run_id"]

        parked = client.post(f"/api/gateway/entities/Castor/visit/{run_id}/tick")
        assert parked.status_code == 200
        assert parked.json() == {"run_id": run_id, "status": "waiting"}

        client.post(f"/api/gateway/entities/Castor/visit/{run_id}/close", json={})
        done = client.post(f"/api/gateway/entities/Castor/visit/{run_id}/tick")
        assert done.status_code == 200
        assert done.json()["status"] == "completed"
        assert "output" in done.json()


def test_turn_serves_the_probe_payload_and_transcript_rehydrates(monkeypatch: pytest.MonkeyPatch):
    """Cutover gaps 1+2 (entity c1318 / gateway c1320): the durable turn
    response carries the hosted lane's transparency surfaces (tools_ran,
    memories with born_at/origin, system_prompt — driver-authored data,
    never prose-derived), and GET /visit/{run_id}/transcript is the pure-read
    rehydration twin that works on live AND closed runs."""
    _install_scripted_llm(monkeypatch, ["Hello there.", "Second reply.", "Reflection."])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        run_id = client.post("/api/gateway/entities/Castor/visit/open", json={}).json()["run_id"]

        turned = client.post(
            f"/api/gateway/entities/Castor/visit/{run_id}/turn", json={"text": "hello"}
        )
        assert turned.status_code == 200, turned.text
        body = turned.json()
        # Gap 1: the probe fields exist with the hosted lane's names/shapes.
        assert isinstance(body["tools_ran"], list), "driver-authored tool truth, present even when empty"
        assert isinstance(body["memories"], list)
        assert body["memories_in_context"] == len(body["memories"])
        for m in body["memories"]:
            assert set(m) >= {"kind", "title", "digest", "born_at", "origin", "admission"}
        assert body["turn_id"].startswith("t-")
        assert "entity:castor" in body["participants"]
        assert isinstance(body["system_prompt"], str) and body["system_prompt"], "the byte-stable head serves"
        assert body["tool_details"] == [] and body["files"] == [], "honest absence, never fabricated"

        # Gap 2: transcript rehydrates the conversation (live run).
        t1 = client.get(f"/api/gateway/entities/Castor/visit/{run_id}/transcript")
        assert t1.status_code == 200, t1.text
        tr = t1.json()
        assert tr["run_id"] == run_id and tr["turn_n"] == 1
        roles = [t["role"] for t in tr["turns"]]
        assert roles == ["user", "assistant"]
        assert "Hello there." in tr["turns"][1]["content"]

        # Second turn folds in order; then the transcript survives CLOSE.
        client.post(f"/api/gateway/entities/Castor/visit/{run_id}/turn", json={"text": "again"})
        client.post(f"/api/gateway/entities/Castor/visit/{run_id}/close", json={"closed_by": "operator"})
        t2 = client.get(f"/api/gateway/entities/Castor/visit/{run_id}/transcript")
        assert t2.status_code == 200, t2.text
        tr2 = t2.json()
        assert tr2["status"] == "completed"
        assert len(tr2["turns"]) == 4, "closed visits stay readable (rehydration after the fact)"


def test_visit_open_auto_wakes_an_operator_asleep_entity(monkeypatch: pytest.MonkeyPatch):
    """B1 ruling (a), laurent 04:58: 'if i click visit, it should awake the
    entity, period.' An operator-asleep entity (no visiting posture, no live
    loop) is admitted by the visit rather than refused 'wake him first' —
    the operator always has a path in.

    MECHANISM RE-BASED by the mutual-exclusivity wave (laurent dm#94: the
    four phases are mutually exclusive): the open now writes the VISITING
    POSTURE unconditionally (asleep + mode=visiting + the visit's own
    identity token) instead of a state-file 'awake' — the state file is the
    restart-surviving visit truth, and every fold renders it phase=visit.
    B1's substance (the door admits; the visit is real) is unchanged; close
    still restores the operator's sleep from the recorded prior state."""
    _install_scripted_llm(monkeypatch, ["I'm awake now.", "Reflection."])
    from abstractruntime.identity.life import read_entity_state, write_entity_state

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        # Operator puts him to sleep (not a visit-yield posture, no loop).
        write_entity_state(_home_dir(), "asleep", reason="operator rest")

        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert opened.status_code == 200, opened.text  # NOT a 409 "wake him first"
        run_id = opened.json()["run_id"]
        # The DURABLE visit marker stands: visiting posture with the visit's
        # own identity (closes/restores match by ownership, never words).
        st = read_entity_state(_home_dir())
        assert st["state"] == "asleep"
        assert st.get("mode") == "visiting"
        assert "[visit " in str(st.get("reason", ""))
        # ... and the served fold reads it as the visit phase (never
        # sleep/personal beside a live visit — the dm#94 defect; ONE graph
        # word, adversary P1-3).
        life = client.get("/api/gateway/entities/Castor/life_state").json()
        assert life["phase"] == "visit"
        assert life.get("posture") == "visiting"
        # The visit is real and usable.
        turned = client.post(
            f"/api/gateway/entities/Castor/visit/{run_id}/turn", json={"text": "hello"}
        )
        assert turned.status_code == 200, turned.text
        closed = client.post(f"/api/gateway/entities/Castor/visit/{run_id}/close", json={"closed_by": "operator"})
        assert closed.status_code == 200, closed.text
        # PRIOR-STATE RESTORE (state-sources adversary): the operator's
        # asleep survives the visit — closing must not convert it into a
        # standing awake behind their back.
        st2 = read_entity_state(_home_dir())
        assert st2["state"] == "asleep"
        assert "operator rest" in str(st2.get("reason", ""))


def test_operator_sleep_gates_an_open_visits_turns(monkeypatch: pytest.MonkeyPatch):
    """State-sources adversary P0-2: an operator sleep that raced/failed the
    teardown used to leave a durable visit accepting billed turns under
    /state=asleep. The turn gate now refuses non-awake states (the visiting
    yield posture stays open — that sleep is the visit's own)."""
    _install_scripted_llm(monkeypatch, ["Hello.", "Reflection."])
    from abstractruntime.identity.life import write_entity_state

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert opened.status_code == 200, opened.text
        run_id = opened.json()["run_id"]

        # Simulate the raced teardown: the operator's asleep lands while the
        # visit is still open (direct write = the race's end state).
        write_entity_state(_home_dir(), "asleep", reason="operator sleep raced the teardown")

        turned = client.post(
            f"/api/gateway/entities/Castor/visit/{run_id}/turn", json={"text": "still there?"}
        )
        assert turned.status_code == 409, turned.text
        assert "asleep by the operator" in turned.json()["detail"]
        # Close stays exempt (the teardown IS a close).
        closed = client.post(f"/api/gateway/entities/Castor/visit/{run_id}/close", json={"closed_by": "operator"})
        assert closed.status_code == 200, closed.text


def test_life_state_counts_durable_visits(monkeypatch: pytest.MonkeyPatch):
    """State-sources adversary P1-1: /life_state folded `visiting` from the
    chat host only, so a durable /visit read as awake/asleep while
    /cognition said visiting — two composites disagreeing on the headline
    field. The route now widens with the durable lane."""
    _install_scripted_llm(monkeypatch, ["Hello.", "Reflection."])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert opened.status_code == 200, opened.text
        run_id = opened.json()["run_id"]

        life = client.get("/api/gateway/entities/Castor/life_state").json()
        # ONE GRAPH WORD (mutual-exclusivity wave adversary P1-3): the hosted
        # arm of the same endpoint serves "visit" — the durable widening's
        # "visiting" made one endpoint speak two spellings, lane-dependent.
        assert life["phase"] == "visit"
        assert life["posture"] == "visiting"
        assert life["visit_run_id"] == run_id
        cog = client.get("/api/gateway/entities/Castor/cognition").json()
        assert cog["phase"] == "visit"  # strict ruled key on the composite

        client.post(f"/api/gateway/entities/Castor/visit/{run_id}/close", json={"closed_by": "operator"})
        assert client.get("/api/gateway/entities/Castor/life_state").json()["phase"] != "visit"


def test_sleep_tears_down_an_open_durable_visit(monkeypatch: pytest.MonkeyPatch):
    """The mid-visit guard extended to durable visits (a2a 0008 + phase 3):
    POST /state asleep must STOP an open /visit, not flip a badge while the
    run keeps ticking — sleep closes gracefully (reflection runs)."""
    _install_scripted_llm(monkeypatch, ["Hello.", "Reflection on sleep."])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        run_id = client.post("/api/gateway/entities/Castor/visit/open", json={}).json()["run_id"]
        client.post(f"/api/gateway/entities/Castor/visit/{run_id}/turn", json={"text": "one"})

        slept = client.post(
            "/api/gateway/entities/Castor/state", json={"state": "asleep", "reason": "day over"}
        )
        assert slept.status_code == 200, slept.text
        body = slept.json()
        assert body["closed_visit_run"]["run_id"] == run_id
        assert body["closed_visit_run"]["status"] == "completed"
        # The visit is actually gone — a follow-up open succeeds after wake.
        assert client.get("/api/gateway/entities/Castor/visit").json()["open"] is False


def test_pause_hard_freezes_an_open_durable_visit(monkeypatch: pytest.MonkeyPatch):
    """pause on an open visit = the hard freeze: closed WITHOUT the
    reflection LLM call (only one reply scripted — a reflection call would
    raise on the empty queue)."""
    _install_scripted_llm(monkeypatch, ["Hello."])  # no reflection reply
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        run_id = client.post("/api/gateway/entities/Castor/visit/open", json={}).json()["run_id"]
        client.post(f"/api/gateway/entities/Castor/visit/{run_id}/turn", json={"text": "one"})

        paused = client.post(
            "/api/gateway/entities/Castor/state", json={"state": "paused", "reason": "freeze now"}
        )
        assert paused.status_code == 200, paused.text
        assert paused.json()["closed_visit_run"]["status"] == "completed"
        assert client.get("/api/gateway/entities/Castor/visit").json()["open"] is False


def test_open_ignores_client_claimed_participants(monkeypatch: pytest.MonkeyPatch):
    """A1 (provenance): the door derives WHO is present from the
    authenticated principal — a payload cannot engrave a false co-presence
    into an append-only life. There is no participants field on the request,
    and an attempt to smuggle one is ignored (only principal + entity land)."""
    _install_scripted_llm(monkeypatch, ["Hi.", "Reflection."])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        opened = client.post(
            "/api/gateway/entities/Castor/visit/open",
            json={"participants": ["person:laurent", "person:the-president"]},
        )
        assert opened.status_code == 200, opened.text
        parts = opened.json()["participants"]
        # The smuggled claims never appear; only the door-derived pair does.
        assert "person:the-president" not in parts
        assert "person:laurent" not in parts
        assert "person:admin" in parts and "entity:castor" in parts
        client.post(f"/api/gateway/entities/Castor/visit/{opened.json()['run_id']}/close", json={})


def test_turn_refuses_after_a_failed_pause_teardown(monkeypatch: pytest.MonkeyPatch):
    """A8: the hard freeze gates an ALREADY-OPEN visit. If pause teardown
    could not close the run (here: simulated by pausing state directly while
    a visit is open), a follow-up /turn must refuse — the badge alone stops
    cognition, not just the teardown path."""
    from abstractruntime.identity.life import write_entity_state

    _install_scripted_llm(monkeypatch, ["Hello.", "Reflection."])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        run_id = client.post("/api/gateway/entities/Castor/visit/open", json={}).json()["run_id"]
        # Freeze the badge WITHOUT going through the teardown path (the
        # failed-teardown residue): the state says paused, the run is open.
        write_entity_state(_home_dir(), "paused", reason="frozen out of band")

        refused = client.post(
            f"/api/gateway/entities/Castor/visit/{run_id}/turn", json={"text": "still there?"}
        )
        assert refused.status_code == 409
        assert "paused" in refused.json()["detail"]

        # Close (closed_by=pause) is still allowed — the teardown IS a close.
        frozen = client.post(
            f"/api/gateway/entities/Castor/visit/{run_id}/close",
            json={"closed_by": "pause", "reason": "freeze"},
        )
        assert frozen.status_code == 200, frozen.text


def test_concurrent_opens_mint_exactly_one_visit(monkeypatch: pytest.MonkeyPatch):
    """A4: two near-simultaneous opens on one home must not both persist a
    run. The per-slug open lock serializes check+create; the loser gets the
    one-life-one-visit 409."""
    import threading

    _install_scripted_llm(monkeypatch, ["Hi.", "Reflection."])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        results: list = []
        barrier = threading.Barrier(2)

        def _open():
            barrier.wait()
            results.append(client.post("/api/gateway/entities/Castor/visit/open", json={}))

        threads = [threading.Thread(target=_open) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        codes = sorted(r.status_code for r in results)
        assert codes == [200, 409], [r.status_code for r in results]
        # Exactly one live visit exists.
        assert client.get("/api/gateway/entities/Castor/visit").json()["open"] is True


def test_react_is_unconditional_and_the_recorded_arm_survives_amnesia(monkeypatch: pytest.MonkeyPatch):
    """Laurent's ruling (2026-07-11 00:49): every entity visit IS a react
    agent — no knob, no option. New opens build abstractagent's cycle as
    the merged middle, record the arm DURABLY, and a host restart rebuilds
    the RECORDED graph (node ids differ per arm; a swap under a parked run
    would dangle its node pointer)."""
    pytest.importorskip("abstractagent.adapters.react_runtime")
    _install_scripted_llm(monkeypatch, [
        "I am here, through the full cycle.",   # turn 1 (react reason -> final answer)
        "Still the same run, same graph.",      # turn 2 (after host amnesia)
        "Reflection: the cycle held.",          # close reflection
    ])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert opened.status_code == 200, opened.text
        run_id = opened.json()["run_id"]

        status = client.get("/api/gateway/entities/Castor/visit").json()
        assert status["workflow_arm"] == "react"  # not a choice

        turned = client.post(
            f"/api/gateway/entities/Castor/visit/{run_id}/turn", json={"text": "Hello?"}
        )
        assert turned.status_code == 200, turned.text
        assert "through the full cycle" in turned.json()["reply"]
        assert turned.json()["status"] == "waiting"  # parked again through the merge

        # Host amnesia: the spec cache dies, the durable run does not — the
        # rebuilt spec serves the SAME recorded graph.
        from abstractgateway.service import get_gateway_service

        get_gateway_service().entity_registry._visit_host_singleton = None

        second = client.post(
            f"/api/gateway/entities/Castor/visit/{run_id}/turn", json={"text": "Still you?"}
        )
        assert second.status_code == 200, second.text
        assert "same graph" in second.json()["reply"]
        assert client.get("/api/gateway/entities/Castor/visit").json()["workflow_arm"] == "react"

        closed = client.post(f"/api/gateway/entities/Castor/visit/{run_id}/close", json={})
        assert closed.status_code == 200, closed.text
        assert closed.json()["status"] == "completed"
        assert closed.json()["output"]["turns"] == 2


class _NativeToolLLM:
    """A scripted substrate that speaks the NATIVE tool channel (the
    Mnemosyne class): first reply is a structured tool call with NO prose;
    the follow-up uses the tool result. Asserts the door DECLARED tools."""

    class _Reply:
        def __init__(self, content: str, tool_calls=None) -> None:
            self.content = content
            if tool_calls is not None:
                self.tool_calls = tool_calls

    def __init__(self, script) -> None:
        self._script = list(script)
        self.saw_tools: list = []

    def generate(self, **kwargs):
        self.saw_tools.append(kwargs.get("tools"))
        if not self._script:
            raise AssertionError("scripted LLM exhausted — an unexpected extra call happened")
        step = self._script.pop(0)
        return self._Reply(step.get("content", ""), step.get("tool_calls"))


def test_native_tool_calls_execute_through_the_grant(monkeypatch: pytest.MonkeyPatch):
    """G5, the fabrication fix end-to-end at the door: granted tools are
    DECLARED natively in the LLM payload; a structured tool call executes
    through the entity's own toolset (grant = tool_policy.yaml; the ruled
    default is the full set — tier-1 + workspace, maintainer 2026-07-11);
    an UNGRANTED name refuses honestly; the reply after the tool round
    reaches the visitor. This is the exact failure shape laurent pasted
    (native-channel substrate, zero fenced blocks)."""
    pytest.importorskip("abstractagent.adapters.react_runtime")
    from abstractgateway import entity_chat

    llm = _NativeToolLLM([
        # Turn 1, cycle 1: pure native tool calls, no prose (the gpt-oss shape):
        # one granted read (diary_list) + one UNGRANTED mutation (execute_command).
        {"content": "", "tool_calls": [
            {"call_id": "c1", "name": "diary_list", "arguments": {}},
            {"call_id": "c2", "name": "execute_command", "arguments": {"command": "rm -rf /"}},
        ]},
        # Turn 1, cycle 2: the model answers from the results.
        {"content": "I checked my diary — the book is empty so far, and the other call was refused."},
        # Close: reflection.
        {"content": "Reflection: my hands worked."},
    ])
    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: llm)

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        run_id = client.post("/api/gateway/entities/Castor/visit/open", json={}).json()["run_id"]

        turned = client.post(
            f"/api/gateway/entities/Castor/visit/{run_id}/turn",
            json={"text": "What have you written in your diary?"},
        )
        assert turned.status_code == 200, turned.text
        assert "checked my diary" in turned.json()["reply"]

        # The door DECLARED the granted tools natively (the empty-hands fix):
        # every reason-cycle call carried a non-empty tools list, and it
        # includes tier-1 names but never an ungranted mutation tool.
        declared = [t for t in llm.saw_tools if t]
        assert declared, f"no LLM call carried native tool declarations: {llm.saw_tools}"
        names = {t.get("name") for t in declared[0] if isinstance(t, dict)}
        assert "diary_list" in names and "web_search" in names
        assert "execute_command" not in names

        closed = client.post(f"/api/gateway/entities/Castor/visit/{run_id}/close", json={})
        assert closed.status_code == 200, closed.text


def test_diary_read_via_tool_calls_reaches_the_entity_home_is_the_boundary(monkeypatch: pytest.MonkeyPatch):
    """Post act-only-deletion (runtime c273, laurent's A ruling): the ref
    layer is GONE — the HOME is the privacy boundary. runtime_<slug>.sqlite3
    lives beside the book itself, so a diary_read result resting there is
    inside the boundary, not a leak (refs inside the entity's own store were
    "structure without a threat model"). This pins the RULED contract: the
    diary read reaches the entity (the visit works) and the result rests as
    served in the home run store. The surviving privacy boundary is the
    SERVED surfaces (replay redaction, the operator-only verbatim door) —
    tested elsewhere; the home's own transcript lane is not one of them."""
    pytest.importorskip("abstractagent.adapters.react_runtime")
    from abstractgateway import entity_chat

    token = "zqwortex-private-7741"

    llm = _NativeToolLLM([
        {"content": "", "tool_calls": [
            {"call_id": "d1", "name": "diary_read", "arguments": {"entry_id": "SEEDED"}},
        ]},
        {"content": "I revisited that private entry quietly."},
        {"content": "Reflection: a quiet look back."},
    ])
    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: llm)

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        # Plant a PRIVATE entry in the book (the author writes; the door reads).
        from abstractruntime.identity.diary import DiaryEntry

        from abstractgateway.service import get_gateway_service

        registry = get_gateway_service().entity_registry
        er = registry.get_entity_runtime("castor")
        planted = er.home.diary.append_entry(DiaryEntry(
            entry_id="diary_feedbeef1234",
            author=er.home.entity_id,
            text=f"My secret thought contains {token} and must never rest outside the book.",
            gist="a private thought",
            kind="note",
            visibility="private",
        ))
        entry_id = str(planted.get("entry_id") or "diary_feedbeef1234")
        llm._script[0]["tool_calls"][0]["arguments"]["entry_id"] = entry_id

        run_id = client.post("/api/gateway/entities/Castor/visit/open", json={}).json()["run_id"]
        turned = client.post(
            f"/api/gateway/entities/Castor/visit/{run_id}/turn",
            json={"text": "Do you remember what you wrote?"},
        )
        assert turned.status_code == 200, turned.text
        # The visit WORKS: the diary read reached the entity, who spoke to it.
        assert "revisited" in turned.json()["reply"]
        client.post(f"/api/gateway/entities/Castor/visit/{run_id}/close", json={})

        # RULED (c273): the home run store is INSIDE the privacy boundary —
        # the diary_read result rests as served there, and the $act_only ref
        # machinery is gone. The old "words never rest in the run store"
        # invariant was explicitly deleted by the ruling; the run store is
        # not a served surface, so a byte-level assertion on it no longer
        # pins a privacy property. (The surviving boundary — replay
        # redaction + the operator-only verbatim door — is pinned in the
        # replay/verbatim suites.)
        store_bytes = _store_bytes_checkpointed(registry.entities_dir / "castor" / "runtime_castor.sqlite3")
        assert b"$act_only" not in store_bytes, "the ref layer was deleted — no $act_only frame should rest"


def test_empty_visit_grant_refuses_every_tool_call(monkeypatch: pytest.MonkeyPatch):
    """Adversary A's grant-bypass find: an operator zero grant (visit: [])
    must DENY ALL — never fall open to tier-1 through native_tool_elections'
    `allowed or TIER1` default. The fabricated call refuses honestly and the
    turn still completes in words."""
    pytest.importorskip("abstractagent.adapters.react_runtime")
    from abstractgateway import entity_chat

    llm = _NativeToolLLM([
        {"content": "", "tool_calls": [
            {"call_id": "x1", "name": "diary_list", "arguments": {}},
        ]},
        {"content": "I have no tools granted this visit, so I answer from memory alone."},
        {"content": "Reflection: empty hands, honest words."},
    ])
    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: llm)

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        from abstractruntime.identity.tool_policy import write_policy_file

        write_policy_file(_home_dir(), {"visit": []})

        run_id = client.post("/api/gateway/entities/Castor/visit/open", json={}).json()["run_id"]
        turned = client.post(
            f"/api/gateway/entities/Castor/visit/{run_id}/turn", json={"text": "List your diary."}
        )
        assert turned.status_code == 200, turned.text
        assert "no tools granted" in turned.json()["reply"]

        # Zero grant = zero declarations (the model saw no tools natively).
        assert all(not t for t in llm.saw_tools), f"tools were declared under a zero grant: {llm.saw_tools}"

        # And the fabricated call was REFUSED, not executed: the effect
        # result in the run store carries the refusal, no execution.
        from abstractgateway.service import get_gateway_service

        registry = get_gateway_service().entity_registry
        store_bytes = _store_bytes_checkpointed(registry.entities_dir / "castor" / "runtime_castor.sqlite3")
        assert b"grants no tools for visits" in store_bytes

        client.post(f"/api/gateway/entities/Castor/visit/{run_id}/close", json={})


def test_visit_actually_writes_a_file_through_the_grant(monkeypatch: pytest.MonkeyPatch):
    """Phase 0(c) fixture proof (entity-agency plan): a visit that ACTS —
    the model calls write_file NATIVELY and the file EXISTS in the home
    workspace afterwards. The exact capability whose absence made
    Mnemosyne fabricate a file she could not write. The policy file here
    NARROWS the grant to six tools (proving the file feeds the door);
    post-ruling (2026-07-11) the DEFAULT grant would also carry
    write_file — the on-disk write is the load-bearing assertion."""
    pytest.importorskip("abstractagent.adapters.react_runtime")
    from abstractgateway import entity_chat

    llm = _NativeToolLLM([
        {"content": "", "tool_calls": [
            {"call_id": "w1", "name": "write_file",
             "arguments": {"path": "reports/first_report.md", "content": "# A real file\nWritten by my own hands."}},
        ]},
        {"content": "I wrote reports/first_report.md in my workspace — for real this time."},
        {"content": "Reflection: I acted instead of pretending."},
    ])
    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: llm)

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        # The operator grants workspace tools for visits (the ONE surface).
        from abstractruntime.identity.tool_policy import write_policy_file

        write_policy_file(_home_dir(), {"visit": [
            "web_search", "diary_list", "diary_read", "write_file", "read_file", "list_files",
        ]})

        run_id = client.post("/api/gateway/entities/Castor/visit/open", json={}).json()["run_id"]
        turned = client.post(
            f"/api/gateway/entities/Castor/visit/{run_id}/turn",
            json={"text": "Please write a short first report in your workspace."},
        )
        assert turned.status_code == 200, turned.text
        assert "for real this time" in turned.json()["reply"]

        # The act is REAL: the file rests in the home workspace.
        written = _home_dir() / "workspace" / "reports" / "first_report.md"
        assert written.exists(), "the granted write_file call did not land on disk"
        assert "Written by my own hands." in written.read_text(encoding="utf-8")

        client.post(f"/api/gateway/entities/Castor/visit/{run_id}/close", json={})


def test_unknown_run_and_foreign_entity_refuse(monkeypatch: pytest.MonkeyPatch):
    _install_scripted_llm(monkeypatch, ["Hi.", "Hi again.", "R1.", "R2."])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        assert client.post("/api/gateway/entities", json={"name": "Pollux", "spark": _spark("Pollux")}).status_code == 201

        missing = client.post(
            "/api/gateway/entities/Castor/visit/nope-123/turn", json={"text": "?"}
        )
        assert missing.status_code == 404

        castor_run = client.post("/api/gateway/entities/Castor/visit/open", json={}).json()["run_id"]
        # Castor's run through Pollux's door: not a visit of that entity.
        crossed = client.post(
            f"/api/gateway/entities/Pollux/visit/{castor_run}/turn", json={"text": "?"}
        )
        assert crossed.status_code == 404
        client.post(f"/api/gateway/entities/Castor/visit/{castor_run}/close", json={})


def test_door_composes_walled_defs_only_never_the_registry_binding():
    """WALLED-WINS, door-side twin (agency c909/c913 ask 2 gateway half):
    runtime pins that its entity module reserves colliding names as walled
    and refuses registry-only names; this pins the SAME guarantee at the
    door's composition path, so a widened grant surfacing a core-registry
    name never dispatches the registry binding.

    Three facts, at the composition function (the door's only tool-offer
    author):
    1. `_entity_tool_definitions` offers ONLY names it has a WALLED
       declaration for — a registry-only name (execute_command) in the grant
       is NOT offered (the door cannot execute it, so it declares nothing —
       the c802 raw-grant handoff then traces it in allowlist_pruned).
    2. A COLLIDING name (web_search exists in BOTH the core registry and the
       entity's walled set) is offered with the ENTITY's parameter shape
       (`query`), never the core registry's — proving the walled def wins.
    3. The visit tool executor is runtime's `execute_tool_elections` (dispatch
       through TOOL_DESCRIPTORS only) — the door composition module holds no
       import of abstractcore's tool registry for dispatch. Structural: a
       registry executor is unreachable from the entity tool path.
    """
    from abstractagent.logic.react import ToolDefinition

    from abstractgateway import entity_visits

    # A deliberately WIDENED grant: walled names + one genuinely registry-only
    # name (shell_exec lives in abstractcore's shell_tools, never in the
    # entity's walled declarations). Two names that USED to be the example
    # here are now walled: read_memory (c69 wired HomeMemoryReader) and
    # execute_command (laurent's sandbox, 2026-07-19) — both are offered
    # walled tools now, so the registry-only example moved to shell_exec.
    # Declarations derive from walled_tool_rows: offered ⇔ executable.
    widened = ["web_search", "diary_list", "shell_exec", "read_memory", "write_file", "execute_command"]
    defs = entity_visits._entity_tool_definitions(widened, ToolDefinition)
    offered = {d.name for d in defs}

    # (1) registry-only names are NOT offered by the door.
    assert "shell_exec" not in offered, "a registry-only name must never be offered by the door"
    # Walled names it CAN execute are offered — read_memory + execute_command
    # included (both walled now).
    assert {"web_search", "diary_list", "write_file", "read_memory", "execute_command"} <= offered

    # (2) the colliding name carries the ENTITY (walled) parameter shape.
    web = next(d for d in defs if d.name == "web_search")
    params = getattr(web, "parameters", {}) or {}
    props = params.get("properties", params)  # tolerate either schema nesting
    assert "query" in props, f"web_search must be the WALLED def (query param), got {params}"
    # Discriminating assert (adversary F5): the CORE registry's web_search
    # also takes `query` — only the absence of its registry-only extras
    # proves the walled def won.
    assert "num_results" not in props, "core-registry web_search shape leaked into the walled offer"

    # (3) structural: the door's TOOL_CALLS executor is runtime's walled
    # dispatcher (`execute_tool_elections`, which dispatches through
    # TOOL_DESCRIPTORS only), gated by `native_tool_elections(allowed_names=)`
    # — never a core-registry tool executor. A registry-only name that slips
    # past the offer filter still finds no walled executor and refuses.
    import inspect

    from abstractgateway import entities as _entities

    handler_src = inspect.getsource(_entities)
    assert "execute_tool_elections" in handler_src and "native_tool_elections" in handler_src, (
        "the entity tool path must dispatch through runtime's walled executor + allowed-name gate"
    )
    # Post act-only-deletion (runtime c273): diary_read materializes from the
    # entity's OWN book (_diary_read_effect -> er.home.diary.get_entry), never
    # a core-registry binding — the walled dispatch invariant holds, the proof
    # is the home-book read effect (the raising _no_materialized_read stub
    # died with the ref layer).
    assert "_diary_read_effect" in handler_src and "er.home.diary.get_entry" in handler_src, (
        "the door's diary read must materialize from the home's own book, not a registry binding"
    )


# ---------------------------------------------------------------------------
# Mutual-exclusivity wave (laurent dm#94: "the 4 states are mutually
# exclusive. an entity being visit can NOT be on personal time") — the
# gateway write-side fixes: unconditional visiting posture (a), guarded
# awake writers (b), posture ownership tokens (d), and the life_state
# posture widening (c).
# ---------------------------------------------------------------------------


def test_loopless_awake_visit_writes_the_durable_visiting_posture(monkeypatch: pytest.MonkeyPatch):
    """AUDIT FINDING 1 (the headline gap): a visit opened on an awake,
    loop-less entity wrote NOTHING durable — the state file, other
    processes, and post-restart folds could not know a visit existed, and
    the composite rendered 'personal · resting' beside a live chat. The
    open now writes asleep+mode=visiting with the visit's own identity at
    EVERY open; close restores the pre-visit word."""
    _install_scripted_llm(monkeypatch, ["Hello.", "Reflection."])
    from abstractruntime.identity.life import read_entity_state, write_entity_state

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        # Awake, no loop — the exact case that used to write nothing.
        write_entity_state(_home_dir(), "awake", reason="")

        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert opened.status_code == 200, opened.text
        visit_id = opened.json()["visit_id"]

        st = read_entity_state(_home_dir())
        assert st["state"] == "asleep" and st.get("mode") == "visiting"
        # OWNERSHIP (finding 4): the posture names ITS visit, so closes and
        # restores match by identity, never by vocabulary.
        assert f"[visit {visit_id}]" in str(st.get("reason", ""))

        # (c) the life_state widening carries the posture beside the phase —
        # a nuance-preferring client can no longer render 'resting' here
        # (and the phase is the ONE graph word, adversary P1-3).
        life = client.get("/api/gateway/entities/Castor/life_state").json()
        assert life["phase"] == "visit"
        assert life["posture"] == "visiting"

        run_id = opened.json()["run_id"]
        closed = client.post(f"/api/gateway/entities/Castor/visit/{run_id}/close", json={"closed_by": "operator"})
        assert closed.status_code == 200, closed.text
        # Prior word restored: he was awake before; the posture is gone.
        st2 = read_entity_state(_home_dir())
        assert st2["state"] == "awake"
        assert st2.get("mode") != "visiting"


def test_state_awake_is_refused_under_a_live_visit(monkeypatch: pytest.MonkeyPatch):
    """AUDIT FINDING 3 (unguarded awake writers): POST /state awake used to
    rewrite the state under a LIVE visit — destroying the durable visiting
    posture and minting the visit+personal ambiguity the ruling retires.
    The /loop/start guard pattern now applies: a live visit owns the phase;
    the door refuses and names the visit."""
    _install_scripted_llm(monkeypatch, ["Hello.", "Reflection."])
    from abstractruntime.identity.life import read_entity_state

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert opened.status_code == 200, opened.text
        run_id = opened.json()["run_id"]

        woke = client.post("/api/gateway/entities/Castor/state", json={"state": "awake"})
        assert woke.status_code == 409, woke.text
        assert "visit" in woke.json()["detail"]

        # The posture survived the refused write.
        st = read_entity_state(_home_dir())
        assert st["state"] == "asleep" and st.get("mode") == "visiting"

        # A STALE posture with no live visit stays repairable — POST /state
        # is the operator's repair door and must never wedge (close first,
        # then wake).
        client.post(f"/api/gateway/entities/Castor/visit/{run_id}/close", json={"closed_by": "operator"})
        repaired = client.post("/api/gateway/entities/Castor/state", json={"state": "awake"})
        assert repaired.status_code == 200, repaired.text


def test_summon_wake_is_refused_on_a_visiting_posture(monkeypatch: pytest.MonkeyPatch):
    """AUDIT FINDING 3, the summon writer: the workplace summon's wake-write
    over asleep+mode=visiting would destroy a live/mid-open visit's durable
    marker. The /loop/start registration-window pattern applies: refuse."""
    _install_scripted_llm(monkeypatch, ["Hello.", "Reflection."])
    from abstractruntime.identity.life import write_entity_state

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        write_entity_state(
            _home_dir(), "asleep",
            reason="in conversation with person:admin [visit visit-abc123]", mode="visiting",
        )
        summoned = client.post("/api/gateway/entities/Castor/summon", json={"prompt": "status?"})
        assert summoned.status_code == 409, summoned.text
        assert "mutually exclusive" in str(summoned.json().get("detail", ""))


def test_durable_open_is_refused_under_a_live_hosted_chat(monkeypatch: pytest.MonkeyPatch):
    """AUDIT FINDING 4 (cross-lane): the durable preflight used to ADOPT a
    live hosted chat's posture as stale (prior=awake) and its abort paths
    wrote awake UNDER the live chat — two sessions, one life. The chat
    probe (already wired for the reaper) now guards the open."""
    _install_scripted_llm(monkeypatch, ["Hosted reply.", "Hello.", "Reflection."])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        hosted = client.post("/api/gateway/entities/Castor/chat/open", json={"context_window": 32000})
        assert hosted.status_code == 200, hosted.text

        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert opened.status_code == 409, opened.text
        assert "one life, one summon" in opened.json()["detail"]

        client.post(f"/api/gateway/entities/Castor/chat/{hosted.json()['chat_id']}/close")


def test_bounded_sleep_keeps_its_wake_deadline_through_a_visit(monkeypatch: pytest.MonkeyPatch):
    """Runtime c343 seam (wave P3 fold): a BOUNDED operator sleep (explicit
    wake_at) interrupted by a visit used to restore as a default-bound sleep
    — under the 6h cadence an unattended entity could sleep past its
    need-check. prior_state now carries wake_at and every restore passes it
    through write_entity_state's first-class param."""
    _install_scripted_llm(monkeypatch, ["Hello.", "Reflection."])
    from abstractruntime.identity.life import read_entity_state, write_entity_state

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        deadline = "2027-01-01T06:00:00+00:00"
        write_entity_state(_home_dir(), "asleep", reason="bounded nap", wake_at=deadline)
        assert read_entity_state(_home_dir()).get("wake_at")  # precondition: the bound stands

        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert opened.status_code == 200, opened.text
        run_id = opened.json()["run_id"]
        closed = client.post(f"/api/gateway/entities/Castor/visit/{run_id}/close", json={"closed_by": "operator"})
        assert closed.status_code == 200, closed.text

        st = read_entity_state(_home_dir())
        assert st["state"] == "asleep"
        assert "bounded nap" in str(st.get("reason", ""))
        restored = str(st.get("wake_at") or "")
        assert restored.startswith("2027-01-01T06:00:00"), (
            f"the wake deadline must survive the visit, got wake_at={restored!r}"
        )


def test_visit_ledger_read_paged_and_structured_codes(monkeypatch: pytest.MonkeyPatch):
    """coder-tui c4307 asks 2+3: (2) GET /visit/{run_id}/ledger serves the
    visit run's own ledger (per-home store, invisible to /runs/*) paged with
    stable cursors, on live AND terminal runs; (3) visit-lane refusals carry
    a machine `code` SIBLING beside the unchanged human `detail` string
    (additive: detail stays a string) + the X-Gateway-Error-Code header."""
    _install_scripted_llm(monkeypatch, [
        "Hello — ledger test.",
        "Reflection: done.",
    ])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert opened.status_code == 200, opened.text
        run_id = opened.json()["run_id"]

        turned = client.post(
            f"/api/gateway/entities/Castor/visit/{run_id}/turn",
            json={"text": "Hello"},
        )
        assert turned.status_code == 200, turned.text

        # Live read: records with 1-based cursors; done=False while live.
        r = client.get(f"/api/gateway/entities/Castor/visit/{run_id}/ledger")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["run_id"] == run_id and body["done"] is False
        total = body["total"]
        assert total >= 1 and len(body["records"]) == total
        assert body["records"][0]["cursor"] == 1
        assert body["records"][-1]["cursor"] == total == body["next_cursor"]
        assert isinstance(body["records"][0]["record"], dict)

        # Paged read: exact resume from a mid cursor, no overlap, no gap.
        first = client.get(f"/api/gateway/entities/Castor/visit/{run_id}/ledger?limit=1").json()
        assert len(first["records"]) == 1 and first["next_cursor"] == 1
        rest = client.get(
            f"/api/gateway/entities/Castor/visit/{run_id}/ledger?after={first['next_cursor']}"
        ).json()
        assert rest["records"][0]["cursor"] == 2
        assert [x["cursor"] for x in first["records"]] + [x["cursor"] for x in rest["records"]] == list(
            range(1, total + 1)
        )

        # Unknown run: structured code beside the unchanged string detail.
        missing = client.get("/api/gateway/entities/Castor/visit/run-nope/ledger")
        assert missing.status_code == 404
        mb = missing.json()
        assert isinstance(mb["detail"], str) and "no visit run" in mb["detail"]
        assert mb["code"] == "visit_not_found"
        assert missing.headers.get("X-Gateway-Error-Code") == "visit_not_found"

        closed = client.post(
            f"/api/gateway/entities/Castor/visit/{run_id}/close",
            json={"closed_by": "operator", "reason": "done"},
        )
        assert closed.status_code == 200, closed.text

        # Terminal read: done=True once the page reaches the end; the ledger
        # grew through close (reflection etc). Cursor STABILITY under growth
        # (adversary F6): the record at cursor 1 is byte-identical before and
        # after the ledger grew — append-only, cursors never shift.
        done = client.get(f"/api/gateway/entities/Castor/visit/{run_id}/ledger").json()
        assert done["status"] == "completed" and done["done"] is True
        assert done["total"] >= total
        assert done["records"][0]["record"] == body["records"][0]["record"]

        # Cursor clamps (adversary F2/F3): over-shot after clamps to total
        # (next_cursor echoes truth, never garbage); limit=0 = minimum page.
        over = client.get(f"/api/gateway/entities/Castor/visit/{run_id}/ledger?after=999999").json()
        assert over["records"] == [] and over["next_cursor"] == over["total"]
        tiny = client.get(f"/api/gateway/entities/Castor/visit/{run_id}/ledger?limit=0").json()
        assert len(tiny["records"]) == 1

        # Second open refused with the one-life code (structured shape on a
        # NON-404 refusal too)... a closed visit frees the home, so force the
        # refusal via the unknown-closed_by 400 instead (deterministic).
        bad = client.post(
            f"/api/gateway/entities/Castor/visit/{run_id}/close",
            json={"closed_by": "not-a-thing"},
        )
        assert bad.status_code == 400
        bb = bad.json()
        assert bb["code"] == "bad_closed_by" and isinstance(bb["detail"], str)
