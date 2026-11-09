"""Memory metadata authored with the memory (operator rulings 2026-11-09; AbstractRuntime 4eefddb +
bb6d17a): every episode a visit forms carries the title and summary the entity's summaries
capability model wrote, in the formation step. The gateway's seams:
- the durable visit runtime routes the capability-tagged LLM_CALL to the summaries model
  (`open_entity_runtime(capability_handlers=)`), with the JSON schema forwarded as structured output;
- the hosted chat session knows its capability models (`ChatSession.llm_for_capability`).
Without them every record forms pending and logs an authoring failure. Hermetic gateway."""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

pytestmark = [pytest.mark.basic, pytest.mark.capability_models]

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractruntime.identity.visit_workflow")

_TOKEN = "entity-authoring-seams-secret"
TITLE = "The tide at dusk"
SUMMARY = "We talked about the tide coming in at dusk."


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway import entity_chat

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setattr(entity_chat, "gateway_text_mind",
                        lambda: {"provider": "lmstudio", "model": "test-model", "base_url": None, "reasoning": None})


class _LLM:
    """One fake model for every client the gateway builds: a structured call (a response model) is
    the summaries capability authoring a record; anything else is the mind talking."""

    class _Reply:
        def __init__(self, content: str) -> None:
            self.content = content

    def __init__(self) -> None:
        self.structured: list = []

    def generate(self, **kwargs):
        model = kwargs.get("response_model")
        if model is not None:
            self.structured.append(kwargs)
            return model(title=TITLE, summary=SUMMARY)
        return self._Reply("The tide comes in at dusk.")


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> _LLM:
    from abstractgateway import entity_chat

    fake = _LLM()
    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: fake)
    return fake


def _spark(name: str = "Castor") -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _episodes(ms) -> list:
    from abstractmemory import TripleQuery

    return [a for a in ms.store.query(TripleQuery(predicate="dcterms:abstract", limit=0))
            if (a.attributes or {}).get("record_kind") == "episode"]


def _registry():
    from abstractgateway.routes.entities import _registry as reg

    return reg()


def test_a_durable_visit_turn_forms_its_episode_authored(llm):
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert opened.status_code == 200, opened.text
        rid = opened.json()["run_id"]
        turned = client.post(f"/api/gateway/entities/Castor/visit/{rid}/turn", json={"text": "when does the tide come in?"})
        assert turned.status_code == 200, turned.text
        episodes = _episodes(_registry().get_entity_runtime("castor").home.ms)
        assert episodes, "the visit turn formed no episode"
        ep = episodes[-1]
        assert ep.attributes.get("title") == TITLE and str(ep.object) == SUMMARY, ep.attributes
        assert "pending" not in ep.attributes
        assert llm.structured, "the authoring call never reached the summaries model as a structured call"
        client.post(f"/api/gateway/entities/Castor/visit/{rid}/close", json={"closed_by": "operator"})


def test_a_hosted_chat_turn_forms_its_records_authored(llm):
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        opened = client.post("/api/gateway/entities/Castor/chat/open", json={"context_window": 32000})
        assert opened.status_code == 200, opened.text
        cid = opened.json()["chat_id"]
        from abstractgateway.routes.entities import _chat_host

        host = _chat_host()
        hosted = host._sessions[host._by_slug["castor"]]
        assert callable(hosted.session.llm_for_capability), "the hosted session has no capability models"
        assert hosted.session.llm_for_capability("summaries") is llm
        client.post(f"/api/gateway/entities/Castor/chat/{cid}/close")


def test_a_schema_the_converter_refuses_is_a_failed_call_never_an_unstructured_answer():
    from types import SimpleNamespace

    from abstractgateway.entities import _llm_effect_generate

    class _Never:
        def generate(self, **kw):  # pragma: no cover - must not be reached
            raise AssertionError("an unstructured call was made")

    out = _llm_effect_generate(_Never(), SimpleNamespace(payload={"messages": [], "response_schema": []}), empty_message="x")
    assert str(getattr(out.status, "value", out.status)) == "failed"


def test_a_durable_visit_close_authors_what_formation_left_pending(monkeypatch: pytest.MonkeyPatch):
    """A failed authoring call at formation forms the episode PENDING (title.author / digest.author
    tasks); on a loop-less home nothing else retries it, so the durable visit's close does (the
    runtime's drainer, bounded by cap_per_turn, under the home's writer lease)."""
    from abstractgateway import entity_authoring, entity_chat

    class _FirstAuthoringFails(_LLM):
        def generate(self, **kwargs):
            if kwargs.get("response_model") is not None and not self.structured:
                self.structured.append(kwargs)
                raise RuntimeError("the summaries endpoint refused")
            return super().generate(**kwargs)

    fake = _FirstAuthoringFails()
    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: fake)
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        opened = client.post("/api/gateway/entities/Castor/visit/open", json={})
        rid = opened.json()["run_id"]
        assert client.post(f"/api/gateway/entities/Castor/visit/{rid}/turn", json={"text": "the tide?"}).status_code == 200
        ms = _registry().get_entity_runtime("castor").home.ms
        assert "pending" in _episodes(ms)[-1].attributes, "the failed authoring did not leave the episode pending"
        assert client.post(f"/api/gateway/entities/Castor/visit/{rid}/close", json={"closed_by": "operator"}).status_code == 200
        drain = entity_authoring._RUNNING.get("castor")
        assert drain is not None, "the close started no authoring drain"
        drain.join(30)
        ep = _episodes(_registry().get_entity_runtime("castor").home.ms)[-1]
        assert ep.attributes.get("title") == TITLE and "pending" not in ep.attributes, ep.attributes
