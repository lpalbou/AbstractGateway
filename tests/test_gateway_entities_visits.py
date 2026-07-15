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
        assert "person:local-admin" in body["participants"]  # verified principal stamped
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
    loop) is WOKEN by the visit rather than refused 'wake him first' — the
    operator always has a path in. The wake reason records the visit did it."""
    _install_scripted_llm(monkeypatch, ["I'm awake now.", "Reflection."])
    from abstractruntime.identity.life import read_entity_state, write_entity_state

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        # Operator puts him to sleep (not a visit-yield posture, no loop).
        write_entity_state(_home_dir(), "asleep", reason="operator rest")

        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert opened.status_code == 200, opened.text  # NOT a 409 "wake him first"
        run_id = opened.json()["run_id"]
        # He is awake, woken BY the visit (reason names it).
        st = read_entity_state(_home_dir())
        assert st["state"] == "awake"
        assert "visit" in str(st.get("reason", "")).lower()
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
        assert life["phase"] == "visiting"  # legacy chip keeps its historical spelling
        assert life["visit_run_id"] == run_id
        cog = client.get("/api/gateway/entities/Castor/cognition").json()
        assert cog["phase"] == "visit"  # strict ruled key on the composite

        client.post(f"/api/gateway/entities/Castor/visit/{run_id}/close", json={"closed_by": "operator"})
        assert client.get("/api/gateway/entities/Castor/life_state").json()["phase"] != "visiting"


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
        assert "person:local-admin" in parts and "entity:castor" in parts
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


def test_diary_read_via_tool_calls_rests_a_ref_never_words(monkeypatch: pytest.MonkeyPatch):
    """G1 for the visit tool path (adversary A's P0): an act-only tool's
    words must never enter the TOOL_CALLS effect result — the runtime rests
    effect results in the per-home run ledger + node traces, which travel on
    directory copy. The handler returns the canonical `$act_only` REFERENCE;
    the words stay in the book and resolve only at send time."""
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
        assert "revisited" in turned.json()["reply"]
        client.post(f"/api/gateway/entities/Castor/visit/{run_id}/close", json={})

        # The words REST nowhere outside the book: byte-grep the run store
        # (ledger + vars + node traces live in one sqlite file).
        store_bytes = _store_bytes_checkpointed(registry.entities_dir / "castor" / "runtime_castor.sqlite3")
        assert token.encode() not in store_bytes, "private diary words rested in the run store"
        # The REF did rest (the act happened, durably, content-free).
        assert b"$act_only" in store_bytes
        # A private entry's gist never rides the ref either.
        assert b"a private thought" not in store_bytes


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

    # A deliberately WIDENED grant: two walled names, one colliding name, one
    # registry-only name (execute_command lives in abstractcore's shell_tools,
    # never in the entity's walled declarations), one absent-driver name.
    widened = ["web_search", "diary_list", "execute_command", "read_memory", "write_file"]
    defs = entity_visits._entity_tool_definitions(widened, ToolDefinition)
    offered = {d.name for d in defs}

    # (1) registry-only + absent-driver names are NOT offered by the door.
    assert "execute_command" not in offered, "a registry-only name must never be offered by the door"
    assert "read_memory" not in offered, "an absent-driver name is not offered (declaring baits dead calls)"
    # Walled names it CAN execute are offered.
    assert {"web_search", "diary_list", "write_file"} <= offered

    # (2) the colliding name carries the ENTITY (walled) parameter shape.
    web = next(d for d in defs if d.name == "web_search")
    params = getattr(web, "parameters", {}) or {}
    props = params.get("properties", params)  # tolerate either schema nesting
    assert "query" in props, f"web_search must be the WALLED def (query param), got {params}"

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
    # The visit path forbids diary materialization with a raising stub — proof
    # the executor has no fallthrough that could reach an unwalled binding.
    assert "_no_materialized_read" in handler_src, (
        "the door's executor must pass a no-fallthrough diary read (walled dispatch only)"
    )
