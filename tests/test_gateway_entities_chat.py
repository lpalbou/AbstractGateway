"""The hosted chat endpoint (a2a 0007: the maintainer's chat drawer backend).

Covers: open -> turn -> close over a scripted LLM (the driver's ChatSession
hosted behind HTTP), driver-authored tools_ran as a data field, one-life-
one-summon refusal, paused refusal, the visit markers on the stream, the
loop-wake duty on close, and the `mode` passthrough pin on the state GET
(runtime's visiting-mode ask: the gateway must not strip unknown keys).
"""

from __future__ import annotations

import copy

import pytest

from abstractgateway import entity_chat
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")

_TOKEN = "entity-chat-shared-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    # The gateway text route stands in for the mind (round 3: no env vars).
    monkeypatch.setattr("abstractgateway.entity_chat.gateway_text_mind", lambda: {"provider": "lmstudio", "model": "test-model", "base_url": None, "reasoning": None})


def _spark(name: str = "Castor") -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _backdate_state(home_dir, *, seconds: float = 300.0) -> None:
    """Age a seeded visiting posture past the open lanes' freshness gate
    (mutual-exclusivity wave adversary P1-1): a posture younger than the
    grace window reads as a MID-OPEN visit and refuses adoption — tests
    simulating a genuinely STALE (crashed) posture must age it."""
    import json as _json
    from datetime import datetime, timedelta, timezone
    from pathlib import Path

    path = Path(home_dir) / "state"
    data = _json.loads(path.read_text(encoding="utf-8"))
    data["changed_at"] = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    path.write_text(_json.dumps(data), encoding="utf-8")


class _ScriptedLLM:
    """Duck-typed like the driver expects: .generate(...) -> .content."""

    class _Reply:
        def __init__(self, content: str) -> None:
            self.content = content

    def __init__(self, replies) -> None:
        self._replies = list(replies)
        self.calls = 0

    def generate(self, **kwargs):
        self.calls += 1
        text = self._replies.pop(0) if self._replies else "I have nothing more to say."
        return self._Reply(text)


def _install_scripted_llm(monkeypatch: pytest.MonkeyPatch, replies) -> _ScriptedLLM:
    from abstractgateway import entity_chat

    llm = _ScriptedLLM(replies)
    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: llm)
    return llm


def test_open_uses_the_gateway_default_then_the_entity_mind_and_refuses_plainly_without_any(monkeypatch: pytest.MonkeyPatch):
    """Round 3 (operator 2026-10-01): an entity without its own mind thinks
    with the gateway's text route; its own mind (PUT /substrate) wins; the
    only refusal left is a gateway with no text model at all, in one plain
    sentence that names no environment variable."""
    from abstractgateway.entity_chat import NO_TEXT_MODEL_REFUSAL

    seen: list = []

    def _factory(provider, **kw):
        seen.append((provider, kw.get("model"), kw.get("base_url")))
        return _ScriptedLLM(["Reply."])

    monkeypatch.setattr(entity_chat, "_default_llm_factory", _factory)
    no_route = {"provider": None, "model": None, "base_url": None, "reasoning": None}
    monkeypatch.setattr("abstractgateway.entity_chat.gateway_text_mind", lambda: dict(no_route))
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        refused = client.post("/api/gateway/entities/Castor/chat/open", json={})
        assert refused.status_code == 400, refused.text
        assert refused.json()["detail"] == NO_TEXT_MODEL_REFUSAL
        assert "ABSTRACTGATEWAY" not in refused.json()["detail"]
        assert client.put("/api/gateway/entities/Castor/personal-grant", json={"mode": "until_revoked"}).status_code == 200
        assert client.post("/api/gateway/entities/Castor/state", json={"state": "awake"}).status_code == 200
        loop_refused = client.post("/api/gateway/entities/Castor/loop/start", json={})
        assert loop_refused.status_code == 400, loop_refused.text
        assert loop_refused.json()["detail"] == NO_TEXT_MODEL_REFUSAL
        assert client.post("/api/gateway/entities/Castor/state", json={"state": "asleep"}).status_code == 200

        # The gateway's text route answers for an entity with no mind of its own.
        monkeypatch.setattr(
            "abstractgateway.entity_chat.gateway_text_mind",
            lambda: {"provider": "lmstudio", "model": "route-model", "base_url": "http://10.0.0.9:1234/v1", "reasoning": None},
        )
        got = client.get("/api/gateway/entities/Castor/substrate").json()
        assert got["source"] == "gateway" and got["provider"] is None
        assert got["effective"]["model"] == "route-model"
        opened = client.post("/api/gateway/entities/Castor/chat/open", json={"context_window": 32000})
        assert opened.status_code == 200, opened.text
        assert opened.json()["mind"] == {"provider": "lmstudio", "model": "route-model", "thinking": None, "source": "gateway"}
        assert seen[-1] == ("lmstudio", "route-model", "http://10.0.0.9:1234/v1")  # the route's own endpoint
        assert client.post(
            f"/api/gateway/entities/Castor/chat/{opened.json()['chat_id']}/close", json={"reflect": False}
        ).status_code == 200

        # Its own mind wins over the gateway default.
        put = client.put("/api/gateway/entities/Castor/substrate", json={"provider": "ollama", "model": "own-model"})
        assert put.status_code == 200, put.text
        assert put.json()["source"] == "entity"
        opened2 = client.post("/api/gateway/entities/Castor/chat/open", json={"context_window": 32000})
        assert opened2.status_code == 200, opened2.text
        assert opened2.json()["mind"]["source"] == "entity"
        assert seen[-1][:2] == ("ollama", "own-model")
        assert seen[-1][2] is None  # the route's endpoint belongs to the route's provider only
        assert client.post(
            f"/api/gateway/entities/Castor/chat/{opened2.json()['chat_id']}/close", json={"reflect": False}
        ).status_code == 200

        # Back to the Gateway default (clear): recorded, and the route answers again.
        cleared = client.put("/api/gateway/entities/Castor/substrate", json={"clear": True})
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["source"] == "gateway" and cleared.json()["provider"] is None
        opened3 = client.post("/api/gateway/entities/Castor/chat/open", json={"context_window": 32000})
        assert opened3.json()["mind"]["source"] == "gateway"
        assert client.post(
            f"/api/gateway/entities/Castor/chat/{opened3.json()['chat_id']}/close", json={"reflect": False}
        ).status_code == 200
        replay = client.get("/api/gateway/entities/Castor/replay?families=host")
        changes = [
            __import__("json").loads(line)["payload"]
            for line in replay.text.splitlines() if line.strip()
        ]
        changes = [c for c in changes if c.get("kind") == "substrate_changed"]
        assert len(changes) == 2, changes


def test_chat_open_turn_close_over_http(monkeypatch: pytest.MonkeyPatch):
    _install_scripted_llm(monkeypatch, [
        "Hello Laurent. I checked my diary before answering.",
        "(reflection) Nothing moved me strongly today.",
    ])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        opened = client.post(
            "/api/gateway/entities/Castor/chat/open",
            json={"participants": ["person:laurent"], "context_window": 32000},
        )
        assert opened.status_code == 200, opened.text
        body = opened.json()
        chat_id = body["chat_id"]
        assert body["prelude_tokens"] > 0
        assert "person:laurent" in body["participants"]
        # The entity is a participant in its own life (driver rule).
        # Clean keys (item 6): new homes stamp the clean entity:<name>.
        assert "entity:castor" in body["participants"]

        status = client.get("/api/gateway/entities/Castor/chat").json()
        assert status["open"] is True and status["chat_id"] == chat_id

        turn = client.post(
            f"/api/gateway/entities/Castor/chat/{chat_id}/turn",
            json={"text": "Hello Castor — it's Laurent."},
        )
        assert turn.status_code == 200, turn.text
        t = turn.json()
        assert "Hello Laurent" in t["reply"]
        assert isinstance(t["tools_ran"], list)  # driver-authored, data not prose
        assert t["records_formed"], "the turn must form an episode"
        # The turn probe carries the EXACT system prompt sent to the model
        # (observability, maintainer 2026-07-09; operator transparency
        # ruling — never gated). It embeds the prelude + presence line.
        assert t["system_prompt"], "the turn must surface the system prompt verbatim"
        assert "person:laurent" in t["system_prompt"]

        # An empty turn refuses.
        assert client.post(
            f"/api/gateway/entities/Castor/chat/{chat_id}/turn", json={"text": "  "}
        ).status_code == 400

        closed = client.post(f"/api/gateway/entities/Castor/chat/{chat_id}/close", json={"reflect": True})
        assert closed.status_code == 200, closed.text
        out = closed.json()
        assert out["turns"] == 1
        assert "his memory persists" in out["summary"]

        # After close: status shows no visit; a second close refuses.
        assert client.get("/api/gateway/entities/Castor/chat").json()["open"] is False
        assert client.post(f"/api/gateway/entities/Castor/chat/{chat_id}/close").status_code in (404, 409)

        # The visit is on the observable record: summon + session_closed markers.
        replay = client.get("/api/gateway/entities/Castor/replay?families=host")
        kinds = [
            __import__("json").loads(line)["payload"]["kind"]
            for line in replay.text.splitlines() if line.strip()
        ]
        assert "summon" in kinds and "session_closed" in kinds


def test_open_geometry_ignores_the_removed_environment_variables(monkeypatch: pytest.MonkeyPatch):
    """Round 3: ABSTRACTGATEWAY_ENTITY_CHAT_* are gone. Setting them changes
    nothing: the wide code defaults apply and no warning mentions them."""
    _install_scripted_llm(monkeypatch, ["Reply."])
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CHAT_SHELF_SIZE", "24")
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CHAT_CONTEXT_WINDOW", "32768")
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER", "env-provider")
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CHAT_MODEL", "env-model")
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CHAT_BASE_URL", "http://env.invalid:9/v1")
    seen: list = []
    real_factory = entity_chat._default_llm_factory

    def _spy(provider, **kw):
        seen.append((provider, kw.get("model"), kw.get("base_url")))
        return real_factory(provider, **kw)

    monkeypatch.setattr(entity_chat, "_default_llm_factory", _spy)
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        opened = client.post("/api/gateway/entities/Castor/chat/open", json={})
        assert opened.status_code == 200, opened.text
        body = opened.json()
        assert body["budget_profile"]["shelf_size"] == entity_chat.DEFAULT_ENTITY_CHAT_SHELF_SIZE
        assert not any("ABSTRACTGATEWAY_ENTITY_CHAT" in w for w in body["warnings"])
        assert seen and seen[-1] == ("lmstudio", "test-model", None)  # the text route, not the env pair
        assert client.post(
            f"/api/gateway/entities/Castor/chat/{body['chat_id']}/close", json={"reflect": False}
        ).status_code == 200


def test_one_life_one_summon_and_paused_refusal(monkeypatch: pytest.MonkeyPatch):
    _install_scripted_llm(monkeypatch, ["First session reply."])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        first = client.post("/api/gateway/entities/Castor/chat/open", json={"context_window": 32000})
        assert first.status_code == 200, first.text
        chat_id = first.json()["chat_id"]

        # One life, one summon: the second open refuses while a session is live.
        second = client.post("/api/gateway/entities/Castor/chat/open", json={"context_window": 32000})
        assert second.status_code == 409
        assert "one life, one summon" in second.json()["detail"]

        assert client.post(f"/api/gateway/entities/Castor/chat/{chat_id}/close").status_code == 200

        # Paused = hard freeze: the open refuses with the reason.
        assert client.post(
            "/api/gateway/entities/Castor/state",
            json={"state": "paused", "reason": "maintenance window"},
        ).status_code == 200
        refused = client.post("/api/gateway/entities/Castor/chat/open", json={"context_window": 32000})
        assert refused.status_code == 409
        assert "paused" in refused.json()["detail"]
        assert "maintenance window" in refused.json()["detail"]


def test_visiting_mode_passes_through_state_and_card(monkeypatch: pytest.MonkeyPatch):
    """Runtime's visiting-mode ask (a2a 0007/160200Z): `mode` is in the
    state file; the gateway's GET /state and the card's state overlay must
    serve it unmodified — the badge must be able to say VISITING, not
    ASLEEP, while the maintainer talks to him."""
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        from abstractgateway.service import get_gateway_service
        from abstractruntime.identity.life import write_entity_state

        registry = get_gateway_service().entity_registry
        write_entity_state(
            registry.entities_dir / "castor", "asleep",
            reason="in conversation with person:laurent (auto-yield)", mode="visiting",
        )

        state = client.get("/api/gateway/entities/Castor/state").json()
        assert state["state"] == "asleep"
        assert state["mode"] == "visiting"
        assert "person:laurent" in state["reason"]

        card_state = client.get("/api/gateway/entities/Castor/card").json()["state"]
        assert card_state["mode"] == "visiting"


def test_open_adopts_visiting_posture_and_wakes_on_close(monkeypatch: pytest.MonkeyPatch):
    """A stale auto-yield (crashed visit) is adopted WITH the duty to wake:
    the open succeeds against asleep+visiting, and the close hands his own
    time back (state returns to awake).

    RE-BASED by the mutual-exclusivity wave (laurent dm#94): the open now
    OVERWRITES the stale posture with its own identity token (ownership,
    never words), and `yielded_loop` means exactly what it says — a RUNNING
    loop was yielded (no loop here, so False; the old True overloaded the
    flag with 'adopted a stale posture'). The wake duty rides prior_state
    now: a stale posture's operator word is unrecoverable, so prior=awake
    and close wakes with the visit facts — the same end state."""
    _install_scripted_llm(monkeypatch, ["Adopted-session reply."])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        from abstractgateway.service import get_gateway_service
        from abstractruntime.identity.life import read_entity_state, write_entity_state

        registry = get_gateway_service().entity_registry
        write_entity_state(
            registry.entities_dir / "castor", "asleep",
            reason="in conversation with person:laurent (auto-yield)", mode="visiting",
        )
        # A CRASHED visit's posture is old; a fresh one refuses (P1-1 gate).
        _backdate_state(registry.entities_dir / "castor")

        opened = client.post("/api/gateway/entities/Castor/chat/open", json={"context_window": 32000})
        assert opened.status_code == 200, opened.text
        assert opened.json()["yielded_loop"] is False  # no live loop was yielded

        # The stale posture was re-stamped with THIS visit's identity.
        st = read_entity_state(registry.entities_dir / "castor")
        assert st["state"] == "asleep" and st.get("mode") == "visiting"
        assert f"[visit {opened.json()['chat_id']}]" in str(st.get("reason", ""))

        closed = client.post(f"/api/gateway/entities/Castor/chat/{opened.json()['chat_id']}/close")
        assert closed.status_code == 200, closed.text

        state = client.get("/api/gateway/entities/Castor/state").json()
        assert state["state"] == "awake"
        # Wake-cue seeding (R3, maintainer escalation 2026-07-09): the return
        # reason carries the visit's facts so his next day can begin from the
        # visit instead of a generic line.
        assert "visitor session ended" in state["reason"]
        assert "person:operator" in state["reason"]
        assert "1 turns" in state["reason"] or "turns" in state["reason"]


def test_operator_sleep_mid_chat_visit_survives_the_close(monkeypatch: pytest.MonkeyPatch):
    """Conformance adversary P1: the chat lane's close used to write awake
    unconditionally whenever the visit had yielded the loop — an operator
    sleep (or pause: the EMERGENCY STOP) landed mid-visit was silently
    undone at teardown, and a parked granted loop could reopen a day. The
    close now mirrors the durable lane's visit-authored guard: only the
    visit's own yield posture is overwritten; the operator's word stands."""
    _install_scripted_llm(monkeypatch, ["Adopted reply.", "(reflection) quiet."])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        from abstractgateway.service import get_gateway_service
        from abstractruntime.identity.life import write_entity_state

        registry = get_gateway_service().entity_registry
        home_dir = registry.entities_dir / "castor"
        # A standing stale posture (crashed visit); the open re-stamps it
        # with its own identity (mutual-exclusivity wave: ownership tokens;
        # yielded_loop stays False — no RUNNING loop was yielded).
        write_entity_state(
            home_dir, "asleep",
            reason="in conversation with person:laurent (auto-yield)", mode="visiting",
        )
        _backdate_state(home_dir)  # stale, not mid-open (P1-1 gate)
        opened = client.post("/api/gateway/entities/Castor/chat/open", json={"context_window": 32000})
        assert opened.status_code == 200, opened.text

        # The operator's sleep lands mid-visit through the state route
        # (state writes FIRST, then this same route tears the chat down).
        r = client.post(
            "/api/gateway/entities/Castor/state",
            json={"state": "asleep", "reason": "host under load"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["closed_visit"] is not None  # the teardown ran

        # The operator's word STANDS — the close's wake-write did not fire.
        state = client.get("/api/gateway/entities/Castor/state").json()
        assert state["state"] == "asleep"
        assert "host under load" in state["reason"]


def test_open_salvages_a_died_unreflected_session(monkeypatch: pytest.MonkeyPatch):
    """The reflection-loss guard, web half (a2a 0007/171500Z): a previous
    session's write-ahead marker (died unreflected) is salvaged as the new
    open's FIRST act — look-back over the ENDED session's own sheet,
    surfaced in the response as `salvage`."""
    import json as _json

    _install_scripted_llm(monkeypatch, [
        "(salvage look-back) That first conversation mattered to me.",
        "Present-turn reply.",
        "(reflection) Quiet close.",
    ])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        from abstractgateway.service import get_gateway_service

        registry = get_gateway_service().entity_registry
        home_dir = registry.entities_dir / "castor"

        # A first turn's episode to appraise, formed the normal way, then a
        # write-ahead marker exactly as a dying driver leaves it.
        from abstractgateway.entity_gate import CHANNEL_WORKPLACE, finalize_summon_stamp, mint_summon_stamp
        from abstractruntime.core.models import Effect, EffectType

        entity_id = registry.manifest_for("castor").entity_id
        stamp = finalize_summon_stamp(
            mint_summon_stamp(
                data_dir=registry.data_dir, entity_id=entity_id,
                channel=CHANNEL_WORKPLACE, session_id="chat-died", participants=[],
            ),
            data_dir=registry.data_dir, run_id="run-died",
        )

        class _R:
            run_id, session_id, parent_run_id = "run-died", "chat-died", ""
            vars = {"_runtime": {"entity": stamp}}

        svc = get_gateway_service()
        formed = svc.host.runtime._handlers[EffectType.MEMORY_FORM](
            _R(),
            Effect(type=EffectType.MEMORY_FORM, payload={
                "records": [{"kind": "episode", "title": "the first conversation",
                             "digest": "Laurent introduced himself; I verified before believing."}],
                "turn_id": "t-died-1",
            }),
            None,
        )
        assert formed.status == "completed", formed.error
        (episode_id,) = formed.result["record_ids"]

        (home_dir / "pending_reflection.json").write_text(_json.dumps({
            "session_id": "chat-died",
            "sheet": [[episode_id, "exchange: Laurent introduced himself; I verified before believing."]],
        }), encoding="utf-8")

        opened = client.post("/api/gateway/entities/Castor/chat/open", json={"context_window": 32000})
        assert opened.status_code == 200, opened.text
        body = opened.json()
        assert "salvage" in body, body
        assert "mattered" in body["salvage"]["reply"]
        # The marker retired: the ended session is reflected, not re-openable.
        assert not (home_dir / "pending_reflection.json").exists() or _json.loads(
            (home_dir / "pending_reflection.json").read_text()
        ).get("session_id") != "chat-died"

        assert client.post(
            f"/api/gateway/entities/Castor/chat/{body['chat_id']}/close"
        ).status_code == 200


def test_operator_set_sleep_is_woken_by_the_visit(monkeypatch: pytest.MonkeyPatch):
    """B1 extended to the chat lane (newborn shape c1503: doors WAKE, they
    don't refuse): an operator-asleep entity is admitted BY the visit — the
    operator always has a path in, and a newborn (asleep at birth) is
    visitable from its first moment.

    MECHANISM RE-BASED (mutual-exclusivity wave, laurent dm#94): the open
    writes the visiting posture (the durable visit marker) instead of a
    state-file awake; the folds read it as phase=visit, and close restores
    the operator's sleep from the recorded prior state."""
    _install_scripted_llm(monkeypatch, ["I'm awake now.", "(reflection) quiet"])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        assert client.post(
            "/api/gateway/entities/Castor/state",
            json={"state": "asleep", "reason": "night consolidation"},
        ).status_code == 200

        opened = client.post("/api/gateway/entities/Castor/chat/open", json={"context_window": 32000})
        assert opened.status_code == 200, opened.text
        # The visit marker stands (ownership-stamped), and the fold serves
        # the visit phase — never sleep/personal beside a live drawer.
        state = client.get("/api/gateway/entities/Castor/state").json()
        assert state["state"] == "asleep" and state.get("mode") == "visiting"
        assert f"[visit {opened.json()['chat_id']}]" in state["reason"]
        life = client.get("/api/gateway/entities/Castor/life_state").json()
        assert life["phase"] == "visit"
        # Close restores the OPERATOR's sleep (prior-state, not hardcoded).
        client.post(f"/api/gateway/entities/Castor/chat/{opened.json()['chat_id']}/close")
        after = client.get("/api/gateway/entities/Castor/state").json()
        assert after["state"] == "asleep"
        assert "night consolidation" in after["reason"]


def test_life_state_is_one_mutually_exclusive_phase(monkeypatch: pytest.MonkeyPatch):
    """The composite phase (observer ask, maintainer 2026-07-09 02:02): the
    gateway returns ONE mutually-exclusive phase so the client never renders
    VISITING + RESTING + own-time-lit at once. Visiting outranks all."""
    _install_scripted_llm(monkeypatch, ["hi", "(reflection) quiet"])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        # Newborn = sleep (artifact initial_phase); wake for the idle leg.
        assert client.post("/api/gateway/entities/Castor/state", json={"state": "awake"}).status_code == 200

        # state=awake, no loop, no visit -> SLEEP, the resting default
        # (laurent c203: awake never renders as a dwelling phase).
        ls = client.get("/api/gateway/entities/Castor/life_state").json()
        assert ls["phase"] == "sleep" and ls["own_time_running"] is False

        # A visit outranks everything and reads as exactly one phase.
        opened = client.post("/api/gateway/entities/Castor/chat/open", json={"context_window": 32000})
        assert opened.status_code == 200, opened.text
        ls = client.get("/api/gateway/entities/Castor/life_state").json()
        # GRAPH WORDS ONLY since dm#79 (one vocabulary): the phase is
        # "visit"; the old word survives as posture.
        assert ls["phase"] == "visit" and ls["posture"] == "visiting"
        assert ls["chat_open"] is True
        # Never two-of-three active: the phase is a single string, and the
        # only "running" flag is the loop indicator, which is off here.
        assert ls["own_time_running"] is False

        client.post(f"/api/gateway/entities/Castor/chat/{opened.json()['chat_id']}/close")
        ls = client.get("/api/gateway/entities/Castor/life_state").json()
        assert ls["phase"] in ("sleep", "personal")  # graph words only


def test_set_sleep_closes_an_open_visit(monkeypatch: pytest.MonkeyPatch):
    """Mid-visit revocation (observer/maintainer 2026-07-09): POST /state
    asleep must actually CLOSE an open visit (reflection runs), not just
    flip a badge while the chat host keeps accepting turns."""
    _install_scripted_llm(monkeypatch, ["mid-visit reply", "(reflection) closed by operator"])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        opened = client.post("/api/gateway/entities/Castor/chat/open", json={"context_window": 32000})
        chat_id = opened.json()["chat_id"]
        assert client.post(
            f"/api/gateway/entities/Castor/chat/{chat_id}/turn", json={"text": "hello"}
        ).status_code == 200

        # Operator puts him to sleep mid-visit -> the visit is torn down.
        r = client.post("/api/gateway/entities/Castor/state", json={"state": "asleep", "reason": "enough for tonight"})
        assert r.status_code == 200, r.text
        assert r.json().get("closed_visit") is not None

        # The chat is gone; further turns 404/409, and status shows no visit.
        assert client.get("/api/gateway/entities/Castor/chat").json()["open"] is False
        assert client.post(
            f"/api/gateway/entities/Castor/chat/{chat_id}/turn", json={"text": "still there?"}
        ).status_code in (404, 409)
