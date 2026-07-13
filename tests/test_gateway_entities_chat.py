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
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")

_TOKEN = "entity-chat-shared-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    # Substrate ruling (2026-07-09): NO code default — the operator chooses.
    # These env vars ARE the operator's choice in this suite (the scripted
    # LLM factory is patched anyway; the names just satisfy the contract).
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


def test_open_refuses_without_explicit_substrate(monkeypatch: pytest.MonkeyPatch):
    """Maintainer ruling 2026-07-09 04:26: the operator decides provider +
    model — NO code default, NO fallback. With neither request body nor
    operator env carrying a choice, both summon doors refuse loudly."""
    monkeypatch.delenv("ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER", raising=False)
    monkeypatch.delenv("ABSTRACTGATEWAY_ENTITY_CHAT_MODEL", raising=False)
    _install_scripted_llm(monkeypatch, ["never reached"])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        refused = client.post("/api/gateway/entities/Castor/chat/open", json={})
        assert refused.status_code == 400, refused.text
        assert "no mind substrate chosen" in refused.json()["detail"]
        loop_refused = client.post("/api/gateway/entities/Castor/loop/start", json={})
        assert loop_refused.status_code == 400, loop_refused.text
        assert "no mind substrate chosen" in loop_refused.json()["detail"]
        # An explicit request-body choice opens normally (request > env).
        opened = client.post(
            "/api/gateway/entities/Castor/chat/open",
            json={"provider": "lmstudio", "model": "test-model", "context_window": 32000},
        )
        assert opened.status_code == 200, opened.text


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


def test_open_env_geometry_survives_pasted_junk(monkeypatch: pytest.MonkeyPatch):
    """Live failure 2026-07-08: a pasted non-breaking space glued
    SHELF_SIZE=24 to the next export ('24\\xa0ABSTRACTGATEWAY_...=32768') and
    the parser swallowed it SILENTLY - the operator set the knob and every
    turn stayed at 6 memories. Malformed values salvage their leading
    integer LOUDLY; clean values just work."""
    _install_scripted_llm(monkeypatch, ["Reply."])
    monkeypatch.setenv(
        "ABSTRACTGATEWAY_ENTITY_CHAT_SHELF_SIZE",
        "24\xa0ABSTRACTGATEWAY_ENTITY_CHAT_CONTEXT_WINDOW=32768",
    )
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CHAT_CONTEXT_WINDOW", "32768")
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        opened = client.post("/api/gateway/entities/Castor/chat/open", json={})
        assert opened.status_code == 200, opened.text
        body = opened.json()
        profile = body["budget_profile"]
        assert profile["shelf_size"] == 24  # salvaged from the glued value
        assert profile["token_budget"] == round(0.12 * 32768)  # env window, not the 20k floor
        assert any("malformed" in w for w in body["warnings"])  # loud, never silent
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
    time back (state returns to awake)."""
    _install_scripted_llm(monkeypatch, ["Adopted-session reply."])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        from abstractgateway.service import get_gateway_service
        from abstractruntime.identity.life import write_entity_state

        registry = get_gateway_service().entity_registry
        write_entity_state(
            registry.entities_dir / "castor", "asleep",
            reason="in conversation with person:laurent (auto-yield)", mode="visiting",
        )

        opened = client.post("/api/gateway/entities/Castor/chat/open", json={"context_window": 32000})
        assert opened.status_code == 200, opened.text
        assert opened.json()["yielded_loop"] is True

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


def test_operator_set_sleep_refuses_the_visit(monkeypatch: pytest.MonkeyPatch):
    """Asleep WITHOUT the visiting posture is the operator's no-summon
    window — the web chat respects it exactly like the summon endpoint."""
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        assert client.post(
            "/api/gateway/entities/Castor/state",
            json={"state": "asleep", "reason": "night consolidation"},
        ).status_code == 200

        refused = client.post("/api/gateway/entities/Castor/chat/open", json={"context_window": 32000})
        assert refused.status_code == 409
        assert "asleep" in refused.json()["detail"]
        assert "night consolidation" in refused.json()["detail"]


def test_life_state_is_one_mutually_exclusive_phase(monkeypatch: pytest.MonkeyPatch):
    """The composite phase (observer ask, maintainer 2026-07-09 02:02): the
    gateway returns ONE mutually-exclusive phase so the client never renders
    VISITING + RESTING + own-time-lit at once. Visiting outranks all."""
    _install_scripted_llm(monkeypatch, ["hi", "(reflection) quiet"])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        # Awake, no loop, no visit -> awake.
        ls = client.get("/api/gateway/entities/Castor/life_state").json()
        assert ls["phase"] == "awake" and ls["own_time_running"] is False

        # A visit outranks everything and reads as exactly one phase.
        opened = client.post("/api/gateway/entities/Castor/chat/open", json={"context_window": 32000})
        assert opened.status_code == 200, opened.text
        ls = client.get("/api/gateway/entities/Castor/life_state").json()
        assert ls["phase"] == "visiting"
        assert ls["chat_open"] is True
        # Never two-of-three active: the phase is a single string, and the
        # only "running" flag is the loop indicator, which is off here.
        assert ls["own_time_running"] is False

        client.post(f"/api/gateway/entities/Castor/chat/{opened.json()['chat_id']}/close")
        ls = client.get("/api/gateway/entities/Castor/life_state").json()
        assert ls["phase"] in ("awake", "resting", "personal")


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
