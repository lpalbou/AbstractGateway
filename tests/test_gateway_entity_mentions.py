"""`@entity` from any chat window (2026-11-08): `POST /sessions/{sid}/mentions` runs through the
VISIT DOOR, the entity's HOME holds the episode, the reply is a turn of the session with `author`,
and every turn surface (history bloc, GET /runs, the replay view, the history replayed to the next
agent run) names who speaks. Hermetic gateway, scripted mind."""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractruntime.identity.visit_workflow")

_TOKEN = "entity-mention-shared-secret"
KEYS = {"kind", "name", "entity_id", "identity_ref", "avatar"}


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setattr("abstractgateway.entity_chat.gateway_text_mind", lambda: {"provider": "lmstudio", "model": "test-model", "base_url": None, "reasoning": None})


class _ScriptedLLM:
    class _Reply:
        def __init__(self, content: str) -> None:
            self.content = content

    def __init__(self, replies) -> None:
        self._replies = list(replies)

    def generate(self, **kwargs):
        if not self._replies:
            raise AssertionError("scripted LLM exhausted — an unexpected extra call happened")
        return self._Reply(self._replies.pop(0))


def _install_llm(monkeypatch, replies) -> None:
    from abstractgateway import entity_chat

    llm = _ScriptedLLM(replies)
    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: llm)


def _spark(name: str = "Castor") -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _seed_agent_turn(session_id: str) -> str:
    """An agent turn as /runs/start stores one (origin, prompt author, workflow selection)."""
    from abstractruntime.core.models import RunState, RunStatus
    from abstractruntime.core.turn_author import author_for

    from abstractgateway.service import get_gateway_service

    run = RunState(
        run_id="agent-turn-1", workflow_id="basic-agent@0.0.5:81795ea9", status=RunStatus.COMPLETED, current_node="done",
        vars={
            "prompt": "hello", "context": {"task": "hello", "messages": []},
            "workflow_selection": {"workflow_id": "basic-agent@0.0.5:81795ea9", "name": "Basic Agent"},
            "_meta": {"origin": {"kind": "person", "actor": "default:admin", "client": "assistant", "ref": {}},
                      "prompt_author": author_for("person", "admin")},
        },
        output={"answer": "Hi, I am the agent."}, created_at="2026-11-08T10:00:00+00:00",
        updated_at="2026-11-08T10:00:00+00:00", actor_id="gateway", session_id=session_id,
    )
    get_gateway_service().host.run_store.save(run)
    return run.run_id


def _mention(client, sid, text, *, entity="Castor", index=0):
    return client.post(f"/api/gateway/sessions/{sid}/mentions", json={"entity": entity, "text": text, "mention_index": index})


def test_mention_is_a_visit_turn_in_the_home_and_a_turn_of_the_session(monkeypatch):
    _install_llm(monkeypatch, ["Castor here: the bridge holds.", "Second answer from Castor."])
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        sid = "sess-mention-a"
        agent_rid = _seed_agent_turn(sid)

        r = _mention(client, sid, "@Castor does the bridge hold?")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["run_id"] and body["turn_id"] and body["session_id"] == sid
        author = body["author"]
        assert set(author) == KEYS and author["kind"] == "entity" and author["name"] == "Castor"
        assert author["entity_id"] == "entity:castor"
        from abstractgateway.entity_identity import entity_identity_ref
        from abstractgateway.routes.entities import _registry

        registry = _registry()
        ref = entity_identity_ref("entity:castor", registry.entities_dir / "castor")
        assert author["identity_ref"] == (str(ref) if ref is not None else None)
        visit_run = body["visit"]["run_id"]
        assert body["visit"]["reused"] is False

        # THE HOME PROBE: the entity's own runtime holds the visit turn (ledger + its history).
        er = registry.get_entity_runtime("castor")
        assert er.ledger_store.list(visit_run), "the home's ledger must hold the visit"
        history = (er.run_store.load(visit_run).vars or {}).get("_visit", {}).get("history") or []
        assert any("does the bridge hold" in str(m.get("content")) for m in history if m.get("role") == "user")
        assert er.run_store.load(visit_run).session_id == sid  # the visit is bound to this conversation

        # Session history: the agent turn, then the mention turn (person -> entity).
        bloc = client.get(f"/api/gateway/sessions/{sid}/history/bloc", params={"limit": 10}).json()
        rows = {t["run_id"]: t for t in bloc["turns"]}
        assert rows[body["run_id"]]["author"] == author
        assert rows[body["run_id"]]["prompt_author"]["kind"] == "person"
        assert rows[body["run_id"]]["bundle"]["input_data"]["prompt"] == "@Castor does the bridge hold?"
        assert rows[agent_rid]["author"]["kind"] == "agent" and rows[agent_rid]["author"]["name"] == "Basic Agent"
        assert rows[agent_rid]["prompt_author"] == {"kind": "person", "name": "admin", "entity_id": None,
                                                    "identity_ref": None, "avatar": None}

        # GET /runs/{id} and the replay view name the speaker.
        run = client.get(f"/api/gateway/runs/{body['run_id']}").json()
        assert run["author"] == author and run["status"] == "completed"
        view = client.get(f"/api/gateway/runs/{body['run_id']}/view").json()
        by_role = {m["role"]: m for m in view["messages"]}
        assert by_role["assistant"]["author"] == author and by_role["assistant"]["text"] == "Castor here: the bridge holds."
        assert by_role["user"]["author"]["kind"] == "person"
        agent_view = client.get(f"/api/gateway/runs/{agent_rid}/view").json()
        assert {m["role"]: m["author"]["kind"] for m in agent_view["messages"]} == {"user": "person", "assistant": "agent"}

        # The NEXT agent run's replayed history names the entity as the speaker.
        from abstractruntime import session_chat_messages

        from abstractgateway.service import get_gateway_service

        replayed = session_chat_messages(run_store=get_gateway_service().host.run_store, session_id=sid)
        assert [m["content"] for m in replayed][-1] == "Castor (entity): Castor here: the bridge holds."
        assert replayed[-1]["metadata"]["author"] == author
        assert replayed[1]["content"] == "Hi, I am the agent."

        # One life, one visit: a second mention in this session reuses the visit.
        r2 = _mention(client, sid, "@Castor and now?", index=1)
        assert r2.status_code == 200, r2.text
        assert r2.json()["visit"] == {**r2.json()["visit"], "run_id": visit_run, "reused": True}
        assert r2.json()["run_id"] != body["run_id"]

        # Another conversation while the visit is live: 409, naming the conversation holding the seat.
        r3 = _mention(client, "sess-mention-b", "@Castor hi from elsewhere")
        assert r3.status_code == 409, r3.text
        assert r3.json()["code"] == "in_another_conversation" and r3.json()["session_id"] == sid
        assert sid in r3.json()["detail"] and r3.headers["X-Gateway-Error-Code"] == "in_another_conversation"


def test_unknown_entity_is_404_and_an_unresolvable_mind_is_503(monkeypatch):
    with _client() as client:
        assert _mention(client, "sess-x", "@Nobody hi", entity="Nobody").status_code == 404
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        monkeypatch.setattr("abstractgateway.entity_chat.gateway_text_mind", lambda: {"provider": "", "model": "", "base_url": None, "reasoning": None})
        r = _mention(client, "sess-x", "@Castor hi")
        assert r.status_code == 503, r.text
        assert r.json()["code"] == "entity_cannot_answer" and r.json()["detail"].startswith("Castor cannot answer")


def test_entity_author_keeps_null_keys_when_the_home_has_no_identity(monkeypatch):
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        from abstractgateway import entity_identity
        from abstractgateway.entity_mentions import entity_author
        from abstractgateway.routes.entities import _registry

        monkeypatch.setattr(entity_identity, "read_identity", lambda home: None)
        registry = _registry()
        a = entity_author(registry, registry.manifest_for("Castor"))
        assert a == {"kind": "entity", "name": "Castor", "entity_id": "entity:castor", "identity_ref": None, "avatar": None}


def test_runs_start_stamps_the_person_and_drops_a_client_author():
    from abstractgateway.routes.gateway import _stamp_run_origin
    from abstractgateway.security.principal import GatewayPrincipal

    data = {"_meta": {"author": {"kind": "entity", "name": "castor"}, "prompt_author": {"kind": "person", "name": "forged"}}}
    _stamp_run_origin(data, kind="person", principal=GatewayPrincipal(user_id="alice"), client="assistant")
    assert "author" not in data["_meta"]
    assert data["_meta"]["prompt_author"] == {"kind": "person", "name": "alice", "entity_id": None, "identity_ref": None, "avatar": None}


def test_a_member_cannot_mention_an_entity_they_did_not_create(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    from email_fixtures import gateway as _gateway_fixture  # noqa: F401 - fixture module

    from abstractgateway.entities import EntityRegistry
    from abstractgateway.routes import entities_router
    from abstractgateway.routes.session_mentions import router as mentions_router
    import abstractgateway.routes.entities as entities_routes

    gw = _gateway_fixture.__wrapped__(tmp_path, monkeypatch)
    app = gw["app"]
    app.include_router(entities_router, prefix="/api")
    app.include_router(mentions_router, prefix="/api")
    registry = EntityRegistry(data_dir=gw["data_dir"], embedder_factory=lambda: None)
    monkeypatch.setattr(entities_routes, "_registry", lambda: registry)
    c = TestClient(app)
    assert c.post("/api/gateway/entities", headers=gw["bob"], json={"name": "Borea", "spark": _spark("Borea"), "skills": []}).status_code == 201
    r = c.post("/api/gateway/sessions/sess-alice/mentions", headers=gw["alice"], json={"entity": "Borea", "text": "@Borea hi"})
    # A hidden entity answers exactly like a missing one (RBAC ruling 2026-10-01).
    missing = c.post("/api/gateway/sessions/sess-alice/mentions", headers=gw["alice"], json={"entity": "nobody", "text": "hi"})
    assert r.status_code == 404 and missing.status_code == 404, r.text
    assert r.json()["detail"] == missing.json()["detail"].replace("nobody", "borea")
