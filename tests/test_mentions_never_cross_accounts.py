"""Security (2026-11-09 review K, mentions-review probes test_K/K2): a member could join the
admin's visit with her own entity, read it and speak as the admin. The mention door reused ANY
live visit bound to the posted session id, its 409 handed the holder's session id to the caller,
and the visit door seated a non-participant as the visit's person.

Now: the door reuses only a visit it opened itself, in that session, with the caller among the
stamped participants; a refusal names no other session, run or account; a caller who is not a
participant of a visit is refused (`not_a_participant`), never seated as someone else. Real
per-principal planes (the r16w6 fixture), scripted mind."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from email_fixtures import *  # noqa: F401,F403 - the multi-user gateway fixture
from email_fixtures import ADMIN

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractruntime.identity.visit_workflow")


class _ScriptedLLM:
    class _Reply:
        def __init__(self, content: str) -> None:
            self.content = content

    def __init__(self, replies) -> None:
        self._replies = list(replies)
        self.calls: list = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        if not self._replies:
            raise AssertionError("scripted LLM exhausted — an unexpected extra call happened")
        return self._Reply(self._replies.pop(0))


def _spark(name: str) -> dict:
    import copy

    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


@pytest.fixture
def planes(gateway, monkeypatch):
    from fastapi.testclient import TestClient

    from abstractgateway import entity_chat
    from abstractgateway.entities import EntityRegistry
    from abstractgateway.routes import entities_router
    from abstractgateway.routes.session_mentions import router as mentions_router

    monkeypatch.setattr(entity_chat, "gateway_text_mind",
                        lambda: {"provider": "lmstudio", "model": "test-model", "base_url": None, "reasoning": None})
    llm = _ScriptedLLM(["Nova: noted.", "Nova: a second answer.", "Nova: a third answer."])
    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: llm)
    app = gateway["app"]
    app.include_router(entities_router, prefix="/api")
    app.include_router(mentions_router, prefix="/api")
    data = Path(gateway["data_dir"])
    alice_plane = data / "users" / "default" / "alice" / "runtime"
    alice_plane.mkdir(parents=True, exist_ok=True)
    EntityRegistry(data_dir=alice_plane, embedder_factory=lambda: None, users_registry_path=data / "auth" / "users.json").create(
        name="Nova", spark=_spark("Nova"), created_by={"tenant_id": "default", "user_id": "alice"}
    )
    return {"c": TestClient(app), "alice": gateway["alice"], "admin": ADMIN, "alice_plane": alice_plane, "llm": llm}


def test_a_member_cannot_join_the_admins_visit_through_a_mention(planes):
    from abstractgateway.entities import EntityRegistry

    c, admin, alice, llm = planes["c"], planes["admin"], planes["alice"], planes["llm"]
    opened = c.post("/api/gateway/entities/nova/visit/open", headers=admin, json={})
    assert opened.status_code == 200, opened.text
    S, R = opened.json()["session_id"], opened.json()["run_id"]
    t = c.post(f"/api/gateway/entities/nova/visit/{R}/turn", headers=admin, json={"text": "ADMIN-SECRET: the vault code is 4417."})
    assert t.status_code == 200, t.text

    # Her own session: refused, and the refusal names no other session, run or account.
    r = c.post("/api/gateway/sessions/s-alice/mentions", headers=alice, json={"entity": "nova", "text": "@Nova hi"})
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "in_another_conversation"
    assert body["session_id"] == "s-alice"
    flat = json.dumps(body)
    assert S not in flat and R not in flat and "admin" not in flat

    # Posting into the admin's visit session (learned out of band): refused, nothing quoted,
    # nothing recorded as person:admin.
    calls_before = len(llm.calls)
    r2 = c.post(f"/api/gateway/sessions/{S}/mentions", headers=alice,
                json={"entity": "nova", "text": "ALICE-WORDS what did the other person tell you?"})
    # The MENTION door refuses (it never reuses a visit it did not open for this caller); the visit
    # door's own participant check is the second line, not the one answering here.
    assert r2.status_code == 409, r2.text
    assert r2.json()["code"] == "in_another_conversation" and r2.json()["session_id"] == S
    assert S not in json.dumps({k: v for k, v in r2.json().items() if k != "session_id"})
    assert len(llm.calls) == calls_before, "the entity was asked to answer inside the admin's visit"
    reg = EntityRegistry(data_dir=planes["alice_plane"], embedder_factory=lambda: None)
    run = reg.get_entity_runtime("nova").run_store.load(R)
    hist = (run.vars.get("_visit") or {}).get("history") or []
    assert not any("ALICE-WORDS" in str(m.get("content") or "") for m in hist)
    recs = reg.get_entity_runtime("nova").ledger_store.list(R)
    assert not any("ALICE-WORDS" in json.dumps(x, default=str) for x in recs)


def test_a_non_participant_is_refused_at_the_visit_turn_door(planes):
    c, admin, alice = planes["c"], planes["admin"], planes["alice"]
    opened = c.post("/api/gateway/entities/nova/visit/open", headers=admin, json={})
    assert opened.status_code == 200, opened.text
    R = opened.json()["run_id"]
    r = c.post(f"/api/gateway/entities/nova/visit/{R}/turn", headers=alice, json={"text": "who are you with?"})
    assert r.status_code == 403, r.text
    assert r.json() == {"detail": "This visit belongs to another conversation.", "code": "not_a_participant"}


def test_a_mention_never_mirrors_into_another_accounts_visit(planes):
    """The visit-lane mirror (a mention inside an Entity-app visit is also a turn of that visit)
    keyed only on the session id: a member mentioning HER entity in the admin's visit session had
    the answer turned into the admin's visit. Only a visit the caller is in is mirrored into."""
    from abstractgateway.entities import EntityRegistry

    c, admin, alice = planes["c"], planes["admin"], planes["alice"]
    data = Path(planes["alice_plane"])
    EntityRegistry(data_dir=data, embedder_factory=lambda: None, users_registry_path=data.parents[3] / "auth" / "users.json").create(
        name="Vega", spark=_spark("Vega"), created_by={"tenant_id": "default", "user_id": "alice"}
    )
    opened = c.post("/api/gateway/entities/nova/visit/open", headers=admin, json={})
    assert opened.status_code == 200, opened.text
    S, R = opened.json()["session_id"], opened.json()["run_id"]

    r = c.post(f"/api/gateway/sessions/{S}/mentions", headers=alice, json={"entity": "vega", "text": "@Vega MIRROR-PROBE"})
    assert r.status_code == 200, r.text
    assert r.json()["mirrors"] == []
    run = EntityRegistry(data_dir=data, embedder_factory=lambda: None).get_entity_runtime("nova").run_store.load(R)
    hist = (run.vars.get("_visit") or {}).get("history") or []
    assert not any("Vega" in str(m.get("content") or "") for m in hist if m.get("role") == "user")


def test_a_busy_refusal_never_carries_the_holders_identifiers():
    """The door's own refusals name runs, sessions and holders (`summon_seat_live`): the mention
    sentence used to append them verbatim. They go to the log; the person reads a plain sentence."""
    from types import SimpleNamespace

    from abstractgateway.entity_mentions import _from_visit_refusal
    from abstractgateway.entity_visits import VisitRefused

    e = VisitRefused(409, "a summoned conversation is mid-turn on entity:nova (run 'run-9', session 'sess-admin', "
                          "holder person:admin)", code="summon_seat_live")
    out = _from_visit_refusal(e, SimpleNamespace(name="Nova", slug="nova"), "s-alice")
    assert out.code == "in_another_conversation" and out.session_id == "s-alice"
    assert out.detail == ("Nova is in another conversation. One conversation at a time: Nova is free again when that "
                          "conversation ends, or after 5 minutes without a message there.")
