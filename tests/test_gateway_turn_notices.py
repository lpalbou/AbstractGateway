"""turn.notices pass through the gateway unchanged (AbstractRuntime 09cbad7, the Entity app owner's contract
2026-11-09): what a turn formed or staged is served BESIDE the reply as data, `[{kind, record_id, title, text?,
staged?, visibility?}]`, and the served reply carries no bracket markers (the stored words keep them, ADR 0054).

- the visit door: the turn answers `_turn.reply` (not the history's stored words) with `reply_notices` on the probe
  (`notices` stays the operator's log lines); its close names what it formed of the staged elections, with their
  records (`notices` on the close answer), so a "forming" badge resolves to its record;
- the hosted chat: `reply_notices` beside `reply`;
- a mention: the answer and the session turn row carry the entity's notices.
Hermetic gateway, a scripted model."""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractruntime.identity.visit_workflow")

_TOKEN = "turn-notices-secret"
LESSON_REPLY = "Good.\n```lesson\ntitle: Wait first\nWaiting before answering lets them finish.\n```\nDone."


class _Scripted:
    class _Reply:
        def __init__(self, content: str) -> None:
            self.content = content

    def __init__(self, replies) -> None:
        self._replies = list(replies)

    def generate(self, **kwargs):
        return self._Reply(self._replies.pop(0) if self._replies else "Fine.")


@pytest.fixture(autouse=True)
def _gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setattr("abstractgateway.entity_chat.gateway_text_mind",
                        lambda: {"provider": "lmstudio", "model": "test-model", "base_url": None, "reasoning": None})


def _llm(monkeypatch, replies) -> None:
    from abstractgateway import entity_chat

    llm = _Scripted(replies)
    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: llm)


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _castor(client) -> None:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = "Castor"
    spark["spark"] = 1
    assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": spark}).status_code == 201


def _kind_of(graph_id: str) -> str:
    from abstractgateway.service import get_gateway_service

    home = get_gateway_service().entity_registry.get_home("castor")
    return str((home.memory.record_understanding(graph_id) or {}).get("kind") or "")


def test_a_visit_turn_serves_its_reply_and_notices_and_its_close_names_what_it_formed(monkeypatch) -> None:
    _llm(monkeypatch, [LESSON_REPLY, "Looking back: we spoke of waiting."])
    with _client() as client:
        _castor(client)
        run_id = client.post("/api/gateway/entities/Castor/visit/open", json={}).json()["run_id"]
        turned = client.post(f"/api/gateway/entities/Castor/visit/{run_id}/turn", json={"text": "What did you learn?"})
        assert turned.status_code == 200, turned.text
        body = turned.json()
        assert body["reply"] == "Good.\nDone.", repr(body["reply"])  # the served words, not the history's
        assert body["reply_notices"] == [{"kind": "lesson", "record_id": None, "title": "Wait first",
                                          "text": "Waiting before answering lets them finish.", "staged": True}]
        assert all(isinstance(n, str) for n in body["notices"])  # the operator's log lines, unchanged
        closed = client.post(f"/api/gateway/entities/Castor/visit/{run_id}/close", json={"closed_by": "operator"})
        assert closed.status_code == 200, closed.text
        formed = closed.json()["notices"]
        assert [(n["kind"], n["title"], n["text"]) for n in formed] == [
            ("lesson", "Wait first", "Waiting before answering lets them finish.")], formed
        assert _kind_of(formed[0]["record_id"]) == "lesson", "the record the badge resolves to"


def test_a_hosted_chat_turn_serves_reply_notices(monkeypatch) -> None:
    _llm(monkeypatch, [LESSON_REPLY])
    with _client() as client:
        _castor(client)
        chat_id = client.post("/api/gateway/entities/Castor/chat/open", json={"context_window": 32000}).json()["chat_id"]
        turned = client.post(f"/api/gateway/entities/Castor/chat/{chat_id}/turn", json={"text": "What did you learn?"})
        assert turned.status_code == 200, turned.text
        body = turned.json()
        assert "[learned" not in body["reply"]
        assert [(n["kind"], n["title"]) for n in body["reply_notices"]] == [("lesson", "Wait first")], body["reply_notices"]


def test_a_mention_answer_and_its_session_turn_carry_the_notices(monkeypatch) -> None:
    _llm(monkeypatch, [LESSON_REPLY])
    with _client() as client:
        _castor(client)
        sid = "sess-notices"
        r = client.post(f"/api/gateway/sessions/{sid}/mentions", json={"entity": "castor", "text": "@Castor what did you learn?",
                                                                       "mention_index": 0})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["reply"] == "Good.\nDone." and [n["kind"] for n in body["notices"]] == ["lesson"]
        bundle = client.get(f"/api/gateway/runs/{body['run_id']}/history_bundle", params={"include_session": "true"}).json()
        (row,) = [t for t in bundle["session"]["turns"] if t["run_id"] == body["run_id"]]
        assert row["notices"] == body["notices"], row
