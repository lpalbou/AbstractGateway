"""Diary -> verbatims trail on the operator door (diary---verbatims room,
lane C; laurent: "the diary entry MUST contain those references to enable
to trace back to the verbatims").

GET /entities/{name}/diary/{entry_id} serves an additive `trail` block:
the entry's graph projection + its written_amid episodes (what he attended
at write time) + reflected_in episodes (the conversation that led to the
entry), each with verbatim availability — the entity app renders
click-through with the EXISTING /records/{graph_id}/verbatim endpoint.
Pure reads over edges that already stand; render-when-present with a
labeled degrade (the book read never fails because the trail fold did).
"""

from __future__ import annotations

import copy

import pytest


def _spark(name: str) -> dict:
    pytest.importorskip("abstractmemory")
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def test_diary_door_serves_the_trail(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "trail-secret")
    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": "Bearer trail-secret"}) as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")}).status_code == 201

        from abstractgateway.service import get_gateway_service

        registry = get_gateway_service().entity_registry
        home = registry.get_home("Castor")
        eid = home.entity_id

        # The write shape the live drivers produce: a book entry, an episode
        # (with verbatim), and the diary PROJECTION carrying entry_id +
        # written_amid; formation later authors episode -> projection
        # reflected_in (the birth conversation).
        from abstractruntime.identity.diary import DiaryEntry

        home.diary.append_entry(DiaryEntry(
            entry_id="diary_trail01", author=eid,
            text="I wonder what persists.", gist="a question about persistence",
            kind="question", visibility="self",
            written_at="2026-07-19T12:00:00+00:00",
        ))

        from abstractmemory.records import MemoryRecordInput

        [attended] = home.memory.remember_many(
            [MemoryRecordInput(kind="episode", title="exchange: on persistence",
                               digest="We talked about what persists.",
                               attributes={"payload_ref": "aa" * 16})],
            scope="life", owner_id=eid, idempotency_key="ep-attended")
        [projection] = home.memory.remember_many(
            [MemoryRecordInput(kind="diary", title="diary act",
                               digest="Wrote a diary entry (question).",
                               attributes={"diary_type": "question", "entry_id": "diary_trail01"},
                               provenance={"source": "diary-projection"},
                               edges=[("written_amid", attended)])],
            scope="diary", owner_id=eid, idempotency_key="proj-1")
        # Formation authors the birth conversation AFTER the projection
        # exists (episode -> projection reflected_in edge).
        [birth] = home.memory.remember_many(
            [MemoryRecordInput(kind="episode", title="exchange: the prompting turn",
                               digest="The exchange that prompted the entry.",
                               edges=[("reflected_in", projection)])],
            scope="life", owner_id=eid, idempotency_key="ep-birth")

        body = client.get("/api/gateway/entities/Castor/diary/diary_trail01").json()
        assert body["entry"]["entry_id"] == "diary_trail01"
        trail = body.get("trail")
        assert trail is not None, "the trail must ride the diary read when the projection stands"
        assert trail["projection_id"] == projection
        amid = trail["written_amid"]
        assert [e["graph_id"] for e in amid] == [attended]
        assert amid[0]["verbatim_available"] is True
        assert amid[0]["title"].startswith("exchange: on persistence")
        # The birth conversation arrives through the incoming edge.
        assert [e["graph_id"] for e in trail["reflected_in"]] == [birth]
        assert "prompting" in trail["reflected_in"][0]["title"]


def test_projectionless_entry_serves_words_without_a_trail(monkeypatch: pytest.MonkeyPatch) -> None:
    """An old-vintage entry with no projection: the book read still serves
    the words; the trail is honestly absent, never invented."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "trail-none-secret")
    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": "Bearer trail-none-secret"}) as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")}).status_code == 201

        from abstractgateway.service import get_gateway_service

        registry = get_gateway_service().entity_registry
        home = registry.get_home("Castor")

        from abstractruntime.identity.diary import DiaryEntry

        home.diary.append_entry(DiaryEntry(
            entry_id="diary_orphan1", author=home.entity_id,
            text="an early entry", gist=None, kind="note", visibility="self",
            written_at="2026-07-19T12:00:00+00:00",
        ))
        body = client.get("/api/gateway/entities/Castor/diary/diary_orphan1").json()
        assert body["entry"]["entry_id"] == "diary_orphan1"
        assert "trail" not in body
        assert "warnings" not in body  # absence is a state, not a failure
