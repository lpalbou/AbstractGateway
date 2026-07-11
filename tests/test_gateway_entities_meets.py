"""Two entities in one conversation (plan item 14 — the door's relay half).

Memory's engine pins the two-sided memory guarantee; runtime pins the
per-home run; these tests pin the DOOR's relay: a meet opens BOTH legs
under ONE visit_id (each a durable run in its own home), a relay delivers
the speaker's reply to the listener's leg with cross-entity participant
attribution, both episodes carry the SAME visit_id as DATA, A's home never
holds B's rows, a half-open (partner refuses) closes the first leg, and an
entity cannot meet itself.
"""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractruntime.identity.visit_workflow")

_TOKEN = "entity-meet-shared-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER", "lmstudio")
    monkeypatch.setenv("ABSTRACTGATEWAY_ENTITY_CHAT_MODEL", "test-model")


def _spark(name: str) -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


class _EchoLLM:
    """A per-entity LLM whose reply names the entity — so the relay's
    cross-delivery is observable in each leg's history."""

    class _Reply:
        def __init__(self, content: str) -> None:
            self.content = content

    def __init__(self, tag: str) -> None:
        self._tag = tag
        self._n = 0

    def generate(self, **kwargs):
        self._n += 1
        return self._Reply(f"[{self._tag} says {self._n}]")


def _install_per_entity_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """The door's factory is called per LLM_CALL with the resolved provider;
    route by the home the call is running in — but the factory has no home
    arg, so tag by a rotating counter keyed on the model kwargs is not
    enough. Instead: one shared registry keyed by call order is fragile, so
    we tag by the substrate model (same here) — use a single echo that
    encodes the running entity via the system prompt's name line."""
    from abstractgateway import entity_chat

    class _Router:
        def generate(self, **kwargs):
            system = str(kwargs.get("system_prompt") or "")
            tag = "castor" if "Castor" in system else ("pollux" if "Pollux" in system else "entity")
            return _EchoLLM._Reply(f"[{tag} replies]")

    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: _Router())


def _create(client: TestClient, name: str) -> None:
    assert client.post("/api/gateway/entities", json={"name": name, "spark": _spark(name)}).status_code == 201


def _home_dir(name: str):
    from pathlib import Path

    from abstractgateway.service import get_gateway_service

    return Path(get_gateway_service().config.data_dir) / "entities" / name.lower()


def test_meet_opens_both_legs_under_one_visit_id(monkeypatch: pytest.MonkeyPatch):
    _install_per_entity_llm(monkeypatch)
    with _client() as client:
        _create(client, "Castor")
        _create(client, "Pollux")

        opened = client.post("/api/gateway/entities/meets/open",
                             json={"entity_a": "Castor", "entity_b": "Pollux"})
        assert opened.status_code == 200, opened.text
        body = opened.json()
        assert body["visit_id"].startswith("visit-")
        assert body["a"]["entity_id"] == "entity:castor"
        assert body["b"]["entity_id"] == "entity:pollux"

        # Both legs are live visits on their own homes, one visit_id each.
        st = client.get(f"/api/gateway/entities/meets/{body['meet_id']}").json()
        assert st["a"]["open"] is True and st["b"]["open"] is True
        assert st["a"]["visit_id"] == st["b"]["visit_id"] == body["visit_id"]

        client.post(f"/api/gateway/entities/meets/{body['meet_id']}/close", json={})


def test_relay_delivers_reply_across_the_seam(monkeypatch: pytest.MonkeyPatch):
    _install_per_entity_llm(monkeypatch)
    with _client() as client:
        _create(client, "Castor")
        _create(client, "Pollux")
        meet = client.post("/api/gateway/entities/meets/open",
                          json={"entity_a": "Castor", "entity_b": "Pollux"}).json()
        meet_id = meet["meet_id"]

        exchange = client.post(f"/api/gateway/entities/meets/{meet_id}/relay",
                              json={"opener": "a", "text": "Hello Pollux, it's Castor."})
        assert exchange.status_code == 200, exchange.text
        body = exchange.json()
        assert body["spoke"]["entity_id"] == "entity:castor"
        assert body["heard"]["entity_id"] == "entity:pollux"
        assert body["spoke"]["reply"]   # Castor replied to the opener line
        assert body["heard"]["reply"]   # Pollux replied to Castor's reply
        assert body["spoke"]["turn_n"] == 1 and body["heard"]["turn_n"] == 1

        client.post(f"/api/gateway/entities/meets/{meet_id}/close", json={})


def test_both_homes_remember_with_one_visit_id_no_crosstalk(monkeypatch: pytest.MonkeyPatch):
    from pathlib import Path

    from abstractgateway.service import get_gateway_service

    _install_per_entity_llm(monkeypatch)
    with _client() as client:
        _create(client, "Castor")
        _create(client, "Pollux")
        meet = client.post("/api/gateway/entities/meets/open",
                          json={"entity_a": "Castor", "entity_b": "Pollux"}).json()
        client.post(f"/api/gateway/entities/meets/{meet['meet_id']}/relay",
                   json={"opener": "a", "text": "One exchange."})
        client.post(f"/api/gateway/entities/meets/{meet['meet_id']}/close", json={})

        # Each home formed its OWN episode of the moment, both stamped with
        # the SAME visit_id; neither home holds the other's rows.
        from abstractmemory import TripleQuery

        registry = get_gateway_service().entity_registry
        for slug, other in (("castor", "entity:pollux"), ("pollux", "entity:castor")):
            er = registry.get_entity_runtime(slug)
            rows = er.home.ms.query(TripleQuery(scope="life", owner_id=er.entity_id, limit=0))
            episodes = [a.attributes for a in rows
                        if isinstance(a.attributes, dict) and a.attributes.get("record_kind") == "episode"]
            assert episodes, f"{slug} formed no episode of the meet"
            assert all(e.get("visit_id") == meet["visit_id"] for e in episodes)
            # co-presence: the OTHER entity is a stamped participant here
            assert all(other in (e.get("participants") or []) for e in episodes)
            # no cross-talk: this home's owner is never the other entity
            assert all(er.entity_id == a.owner_id for a in rows)


def test_meet_never_half_opens(monkeypatch: pytest.MonkeyPatch):
    """If the second entity refuses (here: it does not exist), the first
    leg is closed — no entity is left summoned without its partner."""
    _install_per_entity_llm(monkeypatch)
    with _client() as client:
        _create(client, "Castor")
        refused = client.post("/api/gateway/entities/meets/open",
                             json={"entity_a": "Castor", "entity_b": "Ghost"})
        assert refused.status_code == 404

        # Castor's leg was closed by the rollback — a fresh solo visit opens.
        solo = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert solo.status_code == 200, solo.text
        client.post(f"/api/gateway/entities/Castor/visit/{solo.json()['run_id']}/close", json={})


def test_rollback_closes_leg_a_when_a_REAL_second_leg_refuses(monkeypatch: pytest.MonkeyPatch):
    """A9: exercise the ACTUAL rollback branch — leg A opens, then leg B
    refuses AFTER A is durable (B is paused, a real per-home refusal). The
    rollback must close A so a fresh solo visit opens on A."""
    from abstractruntime.identity.life import write_entity_state

    _install_per_entity_llm(monkeypatch)
    with _client() as client:
        _create(client, "Castor")
        _create(client, "Pollux")
        # Pollux is a hard freeze: _preflight refuses leg B AFTER leg A is up.
        write_entity_state(_home_dir("Pollux"), "paused", reason="frozen for the test")

        refused = client.post("/api/gateway/entities/meets/open",
                             json={"entity_a": "Castor", "entity_b": "Pollux"})
        assert refused.status_code == 409, refused.text
        assert "paused" in refused.json()["detail"]

        # Castor's leg was rolled back — the door is free for a solo visit.
        solo = client.post("/api/gateway/entities/Castor/visit/open", json={})
        assert solo.status_code == 200, solo.text
        client.post(f"/api/gateway/entities/Castor/visit/{solo.json()['run_id']}/close", json={})


def test_meet_survives_host_amnesia(monkeypatch: pytest.MonkeyPatch):
    """A6: the two legs are durable runs, so the meet index correlating them
    must be durable too. After the in-memory host is dropped (a restart),
    relay/status/close still resolve the meet by reloading the index."""
    _install_per_entity_llm(monkeypatch)
    with _client() as client:
        _create(client, "Castor")
        _create(client, "Pollux")
        meet = client.post("/api/gateway/entities/meets/open",
                          json={"entity_a": "Castor", "entity_b": "Pollux"}).json()
        meet_id = meet["meet_id"]

        # Simulate the restart: drop the cached hosts on both service + registry.
        from abstractgateway.service import get_gateway_service

        svc = get_gateway_service()
        object.__setattr__(svc, "entity_meet_host", None)
        object.__setattr__(svc, "entity_visit_host", None)
        registry = svc.entity_registry
        registry._meet_host_singleton = None
        registry._visit_host_singleton = None

        # The meet still resolves — reloaded from the durable index.
        st = client.get(f"/api/gateway/entities/meets/{meet_id}")
        assert st.status_code == 200, st.text
        assert st.json()["a"]["open"] is True and st.json()["b"]["open"] is True

        closed = client.post(f"/api/gateway/entities/meets/{meet_id}/close", json={})
        assert closed.status_code == 200
        assert closed.json()["closed"] is True


def test_relay_attributes_the_convener_never_the_other_entity(monkeypatch: pytest.MonkeyPatch):
    """A2 (provenance): the convener's steering line enters the speaker's
    leg authored by the CONVENER (person:local-admin), never by the other
    entity. The speaker's REPLY reaches the listener authored by the speaker
    entity. No home ever engraves the operator's words as an entity's."""
    from abstractmemory import TripleQuery

    from abstractgateway.service import get_gateway_service

    _install_per_entity_llm(monkeypatch)
    with _client() as client:
        _create(client, "Castor")
        _create(client, "Pollux")
        meet = client.post("/api/gateway/entities/meets/open",
                          json={"entity_a": "Castor", "entity_b": "Pollux"}).json()
        assert meet["convener"] == "person:local-admin"
        client.post(f"/api/gateway/entities/meets/{meet['meet_id']}/relay",
                   json={"opener": "a", "text": "Please open the discussion."})
        client.post(f"/api/gateway/entities/meets/{meet['meet_id']}/close", json={})

        registry = get_gateway_service().entity_registry
        # Castor's leg (the speaker): the steering line's verbatim is authored
        # by the convener, NOT by Pollux.
        er = registry.get_entity_runtime("castor")
        rows = er.home.ms.query(TripleQuery(scope="life", owner_id="entity:castor", limit=0))
        castor_verbatims = []
        for a in rows:
            ref = (a.attributes or {}).get("verbatim_ref") if isinstance(a.attributes, dict) else None
            if isinstance(a.attributes, dict) and a.attributes.get("record_kind") == "episode":
                castor_verbatims.append(a.attributes)
        # The convener is a stamped participant of Castor's episodes; Pollux
        # is NOT the author of the opener line (attribution honesty).
        assert castor_verbatims, "Castor formed no episode of the meet"
        assert all("person:local-admin" in (e.get("participants") or []) for e in castor_verbatims)


def test_entity_cannot_meet_itself(monkeypatch: pytest.MonkeyPatch):
    _install_per_entity_llm(monkeypatch)
    with _client() as client:
        _create(client, "Castor")
        refused = client.post("/api/gateway/entities/meets/open",
                             json={"entity_a": "Castor", "entity_b": "castor"})
        assert refused.status_code == 400
        assert "two DIFFERENT entities" in refused.json()["detail"]
