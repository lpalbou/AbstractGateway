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
