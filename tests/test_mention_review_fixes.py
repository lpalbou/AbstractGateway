"""Mentions, the 2026-11-09 review debt (REVIEW-1 items 5, 6, 11, 12; mentions-review probes B, C, D,
E, F, G, G2, L, M): what the mentioned entity reads (the visit lane's own exchange; only what a
reused visit does not already hold; an oversize turn skipped alone; attachments named), one answer per
`mention_id`, a mirror that reports its own new answer or its error, plain refusals that always carry
a code and the caller's session, `repeat_of` only for the caller's own turn with the same words, the
reuse racing the idle close, and per-session locks that do not outlive their use. Hermetic gateway,
scripted mind."""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractruntime.identity.visit_workflow")

_TOKEN = "mention-review-fixes-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setattr("abstractgateway.entity_chat.gateway_text_mind",
                        lambda: {"provider": "lmstudio", "model": "test-model", "base_url": None, "reasoning": None})


class _LLM:
    class _Reply:
        def __init__(self, content: str) -> None:
            self.content = content

    def __init__(self, replies) -> None:
        self._replies = list(replies)
        self.calls: list = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        if not self._replies:
            raise AssertionError("scripted LLM exhausted")
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return self._Reply(reply)


def _install(monkeypatch, replies) -> _LLM:
    from abstractgateway import entity_chat

    llm = _LLM(replies)
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


def _create(client, name="Castor"):
    assert client.post("/api/gateway/entities", json={"name": name, "spark": _spark(name)}).status_code == 201


def _mention(client, sid, text, *, entity="Castor", **extra):
    return client.post(f"/api/gateway/sessions/{sid}/mentions", json={"entity": entity, "text": text, "mention_index": 0, **extra})


def _last_user_text(llm) -> str:
    msgs = llm.calls[-1].get("messages") or []
    users = [m for m in msgs if m.get("role") == "user"]
    return str(users[-1].get("content") if users else llm.calls[-1].get("prompt") or "")


def test_session_locks_do_not_outlive_their_use(monkeypatch):
    from abstractgateway import entity_mentions

    _install(monkeypatch, ["Castor: one."])
    with _client() as client:
        _create(client)
        assert _mention(client, "sess-locks", "@Castor hi").status_code == 200
        assert "sess-locks" not in entity_mentions._SESSION_LOCKS


def test_a_reused_visit_is_only_sent_what_it_does_not_already_hold(monkeypatch):
    llm = _install(monkeypatch, ["Castor: UNIQUE-ANSWER-ONE.", "Castor: two."])
    with _client() as client:
        _create(client)
        assert _mention(client, "sess-requote", "@Castor UNIQUE-QUESTION-ONE").status_code == 200
        r = _mention(client, "sess-requote", "@Castor and next?")
        assert r.status_code == 200, r.text
        assert r.json()["visit"]["reused"] is True
        assert r.json()["context"]["turns"] == 0
        assert "[This conversation so far" not in _last_user_text(llm)


def test_in_a_visit_lane_the_mentioned_entity_reads_that_visits_exchange(monkeypatch):
    llm = _install(monkeypatch, ["Nerin: the tide is LOW-TIDE-FACT.", "Castor: noted.", "Nerin: ok."])
    with _client() as client:
        _create(client, "Nerin")
        _create(client, "Castor")
        opened = client.post("/api/gateway/entities/Nerin/visit/open", json={})
        assert opened.status_code == 200, opened.text
        sid, rid = opened.json()["session_id"], opened.json()["run_id"]
        assert client.post(f"/api/gateway/entities/Nerin/visit/{rid}/turn", json={"text": "how is the tide?"}).status_code == 200
        r = _mention(client, sid, "@Castor what do you think?")
        assert r.status_code == 200, r.text
        assert r.json()["context"]["turns"] >= 1
        castor_call = next(c for c in llm.calls if "what do you think" in str(c))
        assert "LOW-TIDE-FACT" in str(castor_call)


def test_an_oversize_turn_is_skipped_alone_and_attachments_are_named(monkeypatch):
    from abstractgateway import entity_mentions

    llm = _install(monkeypatch, ["Castor: ok."])
    monkeypatch.setattr(entity_mentions, "CONTEXT_MAX_TOKENS", 200)
    rows = [
        {"run_id": "r1", "prompt": "SMALL-OLD question", "answer": "small old answer", "status": "completed",
         "prompt_author": None, "author": None, "attachments": [{"$artifact": "a1", "filename": "contract.pdf"}]},
        {"run_id": "r2", "prompt": "HUGE " + "word " * 2000, "answer": "x", "status": "completed",
         "prompt_author": None, "author": None},
        {"run_id": "r3", "prompt": "SMALL-NEW question", "answer": "small new answer", "status": "completed",
         "prompt_author": None, "author": None},
    ]
    monkeypatch.setattr("abstractruntime.history_bundle._best_effort_session_turns", lambda **kw: list(rows))
    with _client() as client:
        _create(client)
        r = _mention(client, "sess-big", "@Castor hi")
        assert r.status_code == 200, r.text
        ctx = r.json()["context"]
        assert ctx["turns"] == 2 and ctx["skipped_oversize"] == 1
        text = _last_user_text(llm)
        assert "SMALL-NEW" in text and "SMALL-OLD" in text and "HUGE" not in text
        assert "contract.pdf" in text
        assert "too long to quote" in text


def test_one_answer_per_mention_id(monkeypatch):
    llm = _install(monkeypatch, ["Castor: first.", "Castor: a duplicate that must never be asked for."])
    with _client() as client:
        _create(client)
        a = _mention(client, "sess-idem", "@Castor once", mention_id="m-1")
        b = _mention(client, "sess-idem", "@Castor once", mention_id="m-1")
        assert a.status_code == 200 and b.status_code == 200, (a.text, b.text)
        assert b.json() == a.json()
        assert len(llm.calls) == 1


def test_a_failed_answer_is_a_plain_sentence_with_a_code(monkeypatch):
    _install(monkeypatch, [RuntimeError("provider exploded: models a,b,c")])
    with _client() as client:
        _create(client)
        r = _mention(client, "sess-503", "@Castor hi")
        assert r.status_code == 503, r.text
        assert r.json()["code"] == "entity_cannot_answer"
        assert r.json()["detail"] == "Castor cannot answer right now. Try again in a moment."
        assert "exploded" not in r.text and r.json()["session_id"] == "sess-503"


def test_every_refusal_carries_a_code_and_the_callers_session(monkeypatch):
    from abstractgateway.entity_mentions import _from_visit_refusal
    from abstractgateway.entity_visits import VisitRefused

    class _M:
        name = "Castor"
        slug = "castor"

    for e in (VisitRefused(409, "a visit is opening on entity:castor (visiting posture 3s old) — retry shortly"),
              VisitRefused(409, "a hosted chat session is already open on entity:castor", code="chat_session_open")):
        out = _from_visit_refusal(e, _M(), "sess-x")
        assert out.code and out.session_id == "sess-x"
        assert "entity:castor" not in out.detail
    # The kill switch's refusal passes through intact (Codex review #6).
    out = _from_visit_refusal(VisitRefused(409, "Stopped. Resume to visit.", code="stopped_use_resume"), _M(), "sess-x")
    assert (out.code, out.detail, out.session_id) == ("stopped_use_resume", "Stopped. Resume to visit.", "sess-x")


def test_repeat_of_must_be_the_callers_own_turn_with_the_same_words(monkeypatch):
    from abstractruntime.core.models import RunState, RunStatus
    from abstractruntime.core.run_origin import origin_for

    from abstractgateway.service import get_gateway_service

    _install(monkeypatch, ["Castor: ok."])
    with _client() as client:
        _create(client)

        def _seed(rid, prompt, actor):
            get_gateway_service().host.run_store.save(RunState(
                run_id=rid, workflow_id="w", status=RunStatus.COMPLETED, current_node="done",
                vars={"prompt": prompt, "_meta": {"origin": origin_for("person", actor=actor, client="assistant")}},
                output={"answer": "a"}, created_at="2026-11-09T10:00:00+00:00", updated_at="2026-11-09T10:00:00+00:00",
                actor_id="gateway", session_id="sess-rep"))

        _seed("mine-other-words", "something else", "default:admin")
        _seed("someone-elses", "@Castor hello", "default:bob")
        _seed("mine", "@Castor hello", "default:admin")
        for rid in ("mine-other-words", "someone-elses"):
            r = _mention(client, "sess-rep", "@Castor hello", repeat_of=rid)
            assert r.status_code == 422 and r.json()["code"] == "repeat_of_unknown", rid
        assert _mention(client, "sess-rep", "@Castor hello", repeat_of="mine").status_code == 200


def test_a_reuse_racing_the_idle_close_opens_a_fresh_visit(monkeypatch):
    from abstractgateway.entity_visits import EntityVisitHost

    _install(monkeypatch, ["Castor: one.", "(the closing look-back)", "Castor: two.", "spare", "spare"])
    real_turn = EntityVisitHost.turn
    state = {"raced": False}

    def _turn(self, name, run_id, **kw):
        if not state["raced"] and state.get("first_done"):
            state["raced"] = True
            self.close(name, run_id, closed_by="operator", reason="idle close raced the mention")
        out = real_turn(self, name, run_id, **kw)
        state["first_done"] = True
        return out

    monkeypatch.setattr(EntityVisitHost, "turn", _turn)
    with _client() as client:
        _create(client)
        first = _mention(client, "sess-race", "@Castor one")
        assert first.status_code == 200, first.text
        second = _mention(client, "sess-race", "@Castor two")
        assert second.status_code == 200, second.text
        assert second.json()["visit"]["reused"] is False
        assert second.json()["visit"]["run_id"] != first.json()["visit"]["run_id"]


def test_a_stop_landing_before_the_answer_is_recorded_records_nothing(monkeypatch):
    """Codex review #2: the only generation check preceded the author lookup, the persistence and the
    mirrors; a Stop landing in between was recorded and mirrored anyway."""
    from abstractgateway import entity_mentions, entity_phase
    from abstractgateway.service import get_gateway_service

    _install(monkeypatch, ["Castor: answer."])
    real_author = entity_mentions.entity_author

    import inspect

    def _author_then_stop(registry, manifest):
        # Only the mention door's own lookup (after the turn, before the answer is recorded); the
        # visit door's lookup at open runs before the turn.
        if inspect.stack()[1].function == "mention":
            entity_phase.stop(registry, manifest.slug, actor="person:admin")
        return real_author(registry, manifest)

    monkeypatch.setattr(entity_mentions, "entity_author", _author_then_stop)
    with _client() as client:
        _create(client)
        r = _mention(client, "sess-stop-2", "@Castor hi")
        assert r.status_code == 409 and r.json()["code"] == "stopped_use_resume", r.text
        runs = get_gateway_service().host.run_store.list_runs(limit=200) or []
        assert [x for x in runs if getattr(x, "session_id", None) == "sess-stop-2"] == []


def test_a_stop_after_the_answer_carries_nothing_into_another_visit(monkeypatch):
    from abstractgateway import entity_mentions, entity_phase

    llm = _install(monkeypatch, ["Nerin: the tide is low.", "Castor: noted.", "Nerin: unexpected."])
    real_record = entity_mentions._record_session_turn

    def _record_then_stop(svc, **kw):
        out = real_record(svc, **kw)
        from abstractgateway.routes.entities import _registry

        entity_phase.stop(_registry(), "castor", actor="person:admin")
        return out

    monkeypatch.setattr(entity_mentions, "_record_session_turn", _record_then_stop)
    with _client() as client:
        _create(client, "Nerin")
        _create(client, "Castor")
        opened = client.post("/api/gateway/entities/Nerin/visit/open", json={})
        sid, rid = opened.json()["session_id"], opened.json()["run_id"]
        assert client.post(f"/api/gateway/entities/Nerin/visit/{rid}/turn", json={"text": "how is the tide?"}).status_code == 200
        r = _mention(client, sid, "@Castor what do you think?")
        assert r.status_code == 200, r.text
        assert r.json()["mirrors"][0]["reply"] is None and "stopped" in r.json()["mirrors"][0]["error"]
        assert len(llm.calls) == 2, "Nerin was asked to answer a mirrored turn after Castor was stopped"


def test_a_failed_mirrored_turn_is_answered_and_the_visit_stays_open(monkeypatch):
    """Runtime 1306804 (gateway slice finding 2026-11-09: a failed mirrored turn ended the visited
    entity's visit): the visited entity's failed model call answers THAT turn with the error, the
    mirror row shows it as the turn's answer, and the visit goes on."""
    llm = _install(monkeypatch, ["Nerin: the tide is low.", "Castor: noted.", RuntimeError("provider exploded"),
                                 "Nerin: still here."])
    with _client() as client:
        _create(client, "Nerin")
        _create(client, "Castor")
        opened = client.post("/api/gateway/entities/Nerin/visit/open", json={})
        sid, rid = opened.json()["session_id"], opened.json()["run_id"]
        assert client.post(f"/api/gateway/entities/Nerin/visit/{rid}/turn", json={"text": "how is the tide?"}).status_code == 200
        r = _mention(client, sid, "@Castor what do you think?")
        assert r.status_code == 200, r.text
        (row,) = r.json()["mirrors"]
        assert row["reply"].startswith("This turn could not be answered:") and "exploded" in row["turn_error"]
        assert client.get("/api/gateway/entities/Nerin/visit").json().get("open") is True
        nxt = client.post(f"/api/gateway/entities/Nerin/visit/{rid}/turn", json={"text": "and now?"})
        assert nxt.status_code == 200 and nxt.json()["reply"] == "Nerin: still here.", nxt.text
        assert len(llm.calls) == 4


def test_a_mention_id_survives_a_crash_between_the_answer_and_the_session_turn(monkeypatch):
    from abstractgateway import entity_mentions

    llm = _install(monkeypatch, ["Castor: the one answer.", "Castor: a second answer that must never be asked for."])
    real_record = entity_mentions._record_session_turn
    crashed = []

    def _crash_once(svc, **kw):
        if not crashed:
            crashed.append(1)
            raise RuntimeError("the session store fell over")
        return real_record(svc, **kw)

    monkeypatch.setattr(entity_mentions, "_record_session_turn", _crash_once)
    with _client() as client:
        _create(client)
        first = client.post("/api/gateway/sessions/sess-idem2/mentions",
                            json={"entity": "Castor", "text": "@Castor once", "mention_index": 0, "mention_id": "m-9"})
        assert first.status_code >= 500
        again = _mention(client, "sess-idem2", "@Castor once", mention_id="m-9")
        assert again.status_code == 200, again.text
        assert again.json()["reply"] == "Castor: the one answer."
        assert len(llm.calls) == 1
        from abstractgateway.service import get_gateway_service

        assert get_gateway_service().host.run_store.load(again.json()["run_id"]) is not None


def test_a_reused_mention_in_a_visit_lane_never_requotes_the_lane(monkeypatch):
    """Codex review #13: with a visit lane, the reuse cursor was ignored and every mention resent
    the whole exchange into the mentioned entity's visit."""
    llm = _install(monkeypatch, ["Nerin: the tide is LOW-TIDE-FACT.", "Castor: noted.", "Nerin: thanks Castor.",
                                 "Castor: still here.", "Nerin: ok."])
    with _client() as client:
        _create(client, "Nerin")
        _create(client, "Castor")
        opened = client.post("/api/gateway/entities/Nerin/visit/open", json={})
        sid, rid = opened.json()["session_id"], opened.json()["run_id"]
        assert client.post(f"/api/gateway/entities/Nerin/visit/{rid}/turn", json={"text": "how is the tide?"}).status_code == 200
        first = _mention(client, sid, "@Castor what do you think?")
        assert first.status_code == 200 and first.json()["context"]["turns"] >= 1, first.text
        second = _mention(client, sid, "@Castor and now?")
        assert second.status_code == 200 and second.json()["visit"]["reused"] is True, second.text
        # Everything in the lane is already in Castor's visit: nothing is quoted again (Castor may
        # still REMEMBER it — recall is memory, not a re-sent quote).
        assert second.json()["context"]["turns"] == 0
        castor_second = next(c for c in reversed(llm.calls) if "and now?" in str(c))
        assert "Nerin: the tide is LOW-TIDE-FACT." not in _last_user_text_of(castor_second).split("MEMORIES")[0]


def _last_user_text_of(call) -> str:
    msgs = call.get("messages") or []
    users = [m for m in msgs if m.get("role") == "user"]
    return str(users[-1].get("content") if users else call.get("prompt") or "")


def test_a_mirror_is_admitted_in_one_step_against_a_stop(monkeypatch):
    """Codex review 2 #3: the mirror checked the mentioned entity's generation, then joined and
    turned; a Stop landing after the check still carried his words into the other visit. Now the
    check and the join are one locked step, and the generation is checked again before the turn."""
    from abstractruntime.identity.life import bump_lifecycle_generation

    from abstractgateway.entity_visits import EntityVisitHost

    llm = _install(monkeypatch, ["Nerin: the tide is low.", "Castor: noted.", "Nerin: unexpected."])
    real_join = EntityVisitHost.join_participant

    def _join_then_stop(self, name, run_id, participant, **kw):
        out = real_join(self, name, run_id, participant, **kw)
        bump_lifecycle_generation(self._registry.entities_dir / "castor", written_by="test")  # Castor stopped
        return out

    monkeypatch.setattr(EntityVisitHost, "join_participant", _join_then_stop)
    with _client() as client:
        _create(client, "Nerin")
        _create(client, "Castor")
        opened = client.post("/api/gateway/entities/Nerin/visit/open", json={})
        sid, rid = opened.json()["session_id"], opened.json()["run_id"]
        assert client.post(f"/api/gateway/entities/Nerin/visit/{rid}/turn", json={"text": "how is the tide?"}).status_code == 200
        r = _mention(client, sid, "@Castor what do you think?")
        assert r.status_code == 200, r.text
        assert r.json()["mirrors"][0]["reply"] is None and "stopped" in r.json()["mirrors"][0]["error"]
        assert len(llm.calls) == 2, "Nerin was asked to answer words carried across a Stop"


def test_the_runs_list_and_the_history_bloc_carry_the_turns_notices(monkeypatch):
    """Castor E2E 2026-11-10: a mention turn's answer says what the turn formed or staged (`notices`), but its
    GET /runs row and its history bloc turn carried none. Both now carry the same list as the answer."""
    _install(monkeypatch, ["Noted, I hold it as your report.\n\n```lesson\ntitle: Storm ledgers can be misfiled\n"
                           "Before citing the 1890 storm ledger, check where it is filed now.\n```", "Reflection."])
    with _client() as client:
        _create(client)
        r = _mention(client, "sess-notices", "@Castor the 1890 storm ledger was misfiled")
        assert r.status_code == 200, r.text
        answer = r.json()
        assert answer["notices"], answer
        rows = client.get("/api/gateway/runs", params={"session_id": "sess-notices"}).json()
        items = rows.get("items") or rows.get("runs") or []
        (row,) = [i for i in items if i["run_id"] == answer["run_id"]]
        assert row["notices"] == answer["notices"], row
        bloc = client.get("/api/gateway/sessions/sess-notices/history/bloc").json()
        (turn,) = [t for t in bloc["turns"] if t["run_id"] == answer["run_id"]]
        assert turn["notices"] == answer["notices"], turn
