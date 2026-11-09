"""The entity TILE read and the card cache (backlog 1012).

Operator, 2026-11-07: "it is absurd for a tiny card about the entity to request so much data that
you will never show" — and of the Board's tiles: "there is zero information". These tests pin:

- GET /entities/{name}/tile: the shape (who / now / into lately / on its mind / latest lesson /
  last meaningful moment / counts), closed kinds only, a pure read (no journal deposit, no marker);
- the roster advertises the read (`reads.tile`) so clients fall back to the card on older gateways;
- the engine card is cached per (home directory, journal seq, parameters): a hit on an unchanged
  life, a miss after a new record, never shared between two homes of the same name (tenant rule);
- the merged moments are cached per (home, file size + mtime) and refreshed when a moment lands;
- the projection rules (newest interests first, newest open problem/question, meaningful moments
  only, since-when, the own-time-failed trouble).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")

_TOKEN = "entity-tile-shared-secret"

_TILE_KEYS = {
    "tile_version", "name", "slug", "look", "born", "age_days", "mind", "now", "into_lately",
    "on_its_mind", "latest_lesson", "last_moment", "counts", "as_of_seq", "read_at",
}


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)


def _spark(name: str) -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _form(kind: str, title: str, digest: str, *, turn: str, scope: str = "self", attributes: dict | None = None) -> None:
    """Form one record through the door's MEMORY_FORM handler (a stamped reflection run), the way
    a lived turn does."""
    from abstractgateway.entity_gate import finalize_summon_stamp, mint_summon_stamp
    from abstractgateway.service import get_gateway_service
    from abstractruntime.core.models import Effect, EffectType

    svc = get_gateway_service()
    registry = svc.entity_registry
    entity_id = registry.manifest_for("tessa").entity_id
    stamp = finalize_summon_stamp(
        mint_summon_stamp(data_dir=registry.data_dir, entity_id=entity_id, channel="entity-reflection", session_id="s-tile", participants=[]),
        data_dir=registry.data_dir,
        run_id=f"run-{turn}",
    )

    class _R:
        pass

    run = _R()
    run.run_id, run.session_id, run.parent_run_id = f"run-{turn}", "s-tile", ""
    run.vars = {"_runtime": {"entity": stamp}}
    record = {"kind": kind, "title": title, "digest": digest}
    if attributes:
        record["attributes"] = attributes
    out = svc.host.runtime._handlers[EffectType.MEMORY_FORM](
        run,
        Effect(type=EffectType.MEMORY_FORM, payload={"records": [record], "scope": scope, "owner_id": entity_id, "turn_id": turn}),
        None,
    )
    assert out.status == "completed", out.error


def test_tile_serves_what_a_card_shows_and_nothing_else():
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Tessa", "spark": _spark("Tessa")}).status_code == 201

        # The roster advertises the lean read (older gateways do not: clients fall back to /card).
        roster = client.get("/api/gateway/entities").json()
        assert roster["reads"]["tile"] == {"available": True, "version": 1, "endpoint": "/api/gateway/entities/{name}/tile"}

        born = client.get("/api/gateway/entities/Tessa/tile")
        assert born.status_code == 200, born.text
        tile = born.json()
        assert _TILE_KEYS <= set(tile), set(tile) ^ _TILE_KEYS
        assert tile["tile_version"] == 1 and tile["name"] == "Tessa" and tile["slug"] == "tessa"
        assert tile["age_days"] == 0
        # Newborns are born asleep (c1503): the phase is a ruled word, since its birth state.
        assert tile["now"]["phase"] == "sleep" and tile["now"]["busy"] is False and tile["now"]["trouble"] is None
        assert tile["now"]["since"]
        assert tile["into_lately"] == [] and tile["on_its_mind"] is None and tile["latest_lesson"] is None
        assert tile["counts"] == {"open_questions": 0, "open_problems": 0, "lessons": 0, "interests": 0, "sleeps": tile["counts"]["sleeps"]}
        # Its look comes from its identity record (framework ADR 0052: birth records the default).
        assert set(tile["look"]) == {"style", "style_requested", "color"} and tile["look"]["style"] and tile["look"]["color"].startswith("#")
        # the drawn design is an active catalog entry; the stored id rides beside it
        from abstractgateway.entity_identity import avatar_catalog

        assert avatar_catalog()[tile["look"]["style"]].get("status") != "retired"
        assert tile["look"]["style_requested"] == tile["look"]["style"] or avatar_catalog()[tile["look"]["style_requested"]].get("status") == "retired"
        assert tile["now"]["state"] == "asleep"

        _form("interest", "tides", "what the tide keeps and what it takes back", turn="t1")
        _form("interest", "lighthouses", "why a light that only warns is still a kind of welcome", turn="t2",
              attributes={"mind_substrate": {"provider": "lmstudio", "model": "test-9b"}})
        _form("lesson", "listen first", "Listening before answering saves a second answer.", turn="t3", scope="life")

        grown = client.get("/api/gateway/entities/Tessa/tile").json()
        assert grown["as_of_seq"] > tile["as_of_seq"]
        # Newest interest first, in its own words (the statement, never an id or a title prefix).
        assert [i["text"] for i in grown["into_lately"]] == [
            "why a light that only warns is still a kind of welcome",
            "what the tide keeps and what it takes back",
        ]
        assert grown["latest_lesson"]["text"] == "Listening before answering saves a second answer."
        assert grown["counts"]["interests"] == 2 and grown["counts"]["lessons"] == 1
        # No own model choice: the mind is the model its newest lived record was stamped with.
        assert grown["mind"] == {"model": "test-9b", "source": "lived"}

        # Waking and sleeping again: the last moment is that sleep, the phase says since when.
        from abstractgateway.entity_tile import LIFE_MOMENTS_CACHE

        hits = LIFE_MOMENTS_CACHE.hits
        client.get("/api/gateway/entities/Tessa/tile")
        assert LIFE_MOMENTS_CACHE.hits == hits + 1  # nothing new in the files: the merged moments are reused
        assert client.post("/api/gateway/entities/Tessa/state", json={"state": "awake"}).status_code == 200
        # A new moment lands in the marker stream: the cached moments are read again.
        woke = client.get("/api/gateway/entities/Tessa/tile").json()
        assert woke["last_moment"]["kind"] == "wake"
        # Awake with nothing running is idle (operator ruling 2026-11-08; IDLE-IS-SLEEP retired).
        assert woke["now"]["phase"] == "idle" and woke["now"]["state"] == "awake"
        assert client.post("/api/gateway/entities/Tessa/state", json={"state": "asleep", "reason": "rest"}).status_code == 200
        slept = client.get("/api/gateway/entities/Tessa/tile").json()
        assert slept["last_moment"]["kind"] == "sleep"
        state = client.get("/api/gateway/entities/Tessa/state").json()
        assert slept["now"]["since"] == state["changed_at"]

        # Pure read: no journal deposit, no host marker written by reading the tile.
        from abstractgateway.entity_replay import read_host_markers
        from abstractgateway.service import get_gateway_service

        registry = get_gateway_service().entity_registry
        markers = len(read_host_markers(registry.entities_dir, "tessa"))
        again = client.get("/api/gateway/entities/Tessa/tile").json()
        assert again["as_of_seq"] == slept["as_of_seq"]
        assert len(read_host_markers(registry.entities_dir, "tessa")) == markers

        # The tile is small: the whole point.
        assert len(json.dumps(again)) < 4096

        assert client.get("/api/gateway/entities/Nobody/tile").status_code == 404

        # One broken source costs its facts, never the tile (labelled, like /cognition).
        from abstractgateway.entities import EntityRegistry

        def _broken(self, slug, **_kw):
            raise RuntimeError("state history unreadable (test)")

        import pytest as _pytest

        with _pytest.MonkeyPatch.context() as mp:
            mp.setattr(EntityRegistry, "life_moments", _broken)
            degraded = client.get("/api/gateway/entities/Tessa/tile")
        assert degraded.status_code == 200, degraded.text
        body = degraded.json()
        assert body["last_moment"] is None and body["latest_lesson"]["text"]
        assert any("moments unavailable" in w for w in body["warnings"])

        # A held home answers 409 like every other door.
        assert client.post("/api/gateway/entities/Tessa/maintenance-window", json={"action": "open", "reason": "doctoring"}).status_code == 200
        assert client.get("/api/gateway/entities/Tessa/tile").status_code == 409


def test_engine_card_is_cached_until_the_life_grows():
    from abstractgateway.entity_tile import ENGINE_CARD_CACHE

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Tessa", "spark": _spark("Tessa")}).status_code == 201
        first = client.get("/api/gateway/entities/Tessa/card").json()
        misses = ENGINE_CARD_CACHE.misses
        hits = ENGINE_CARD_CACHE.hits
        second = client.get("/api/gateway/entities/Tessa/card").json()
        assert ENGINE_CARD_CACHE.misses == misses and ENGINE_CARD_CACHE.hits == hits + 1  # unchanged life: a hit
        assert second["age_and_context"] == first["age_and_context"]
        # The tile shares the default card's cached compositor result.
        client.get("/api/gateway/entities/Tessa/tile")
        assert ENGINE_CARD_CACHE.misses == misses

        _form("interest", "tides", "what the tide keeps and what it takes back", turn="t1")
        grown = client.get("/api/gateway/entities/Tessa/card").json()
        assert ENGINE_CARD_CACHE.misses == misses + 1  # a new record moved the seq: recomputed once
        assert grown["age_and_context"]["journal_seq"] > first["age_and_context"]["journal_seq"]
        assert any("tide" in i["statement"] for i in grown["discoveries"]["interests"])
        # Other parameters are other entries (never a card of another shape served from the cache).
        narrow = client.get("/api/gateway/entities/Tessa/card?top_n=1").json()
        assert ENGINE_CARD_CACHE.misses == misses + 2
        assert narrow["likes_dislikes"]["provenance"] == grown["likes_dislikes"]["provenance"].replace("top 5", "top 1")


def test_cache_key_is_the_home_directory_never_the_name(tmp_path: Path):
    """Two homes with the same name and the same journal seq (two principals' data dirs) must
    never share a cached card — the communities F3 rule."""
    from abstractgateway.entities import EntityRegistry

    cards = []
    for tenant, interest in (("a", "the sea"), ("b", "the mountains")):
        registry = EntityRegistry(data_dir=tmp_path / tenant)
        registry.create(name="Twin", spark=_spark("Twin"))
        home = registry.get_home("twin")
        from abstractmemory.records import MemoryRecordInput

        home.memory.remember_many(
            records=[MemoryRecordInput(kind="interest", title=interest, digest=interest)],
            scope="self",
            owner_id=home.entity_id,
            turn_id=f"t-{tenant}",
            idempotency_key=f"t-{tenant}-1",
        )
        cards.append((home.memory.current_seq(), registry.card("twin")))
        registry.close_all()
    (seq_a, card_a), (seq_b, card_b) = cards
    assert seq_a == seq_b  # the collision the name-keyed cache would have served across tenants
    assert [i["statement"] for i in card_a["discoveries"]["interests"]] == ["the sea"]
    assert [i["statement"] for i in card_b["discoveries"]["interests"]] == ["the mountains"]


# --------------------------------------------------------------------------- the projection rules


def test_memory_facts_pick_the_newest_in_the_entitys_words():
    from abstractgateway.entity_tile import INTO_LATELY_BOUND, memory_facts

    card = {
        "discoveries": {"interests": [
            {"title": "interest: old", "statement": "old interest", "observed_at": "2026-07-01T00:00:00+00:00", "explored": True},
            {"title": "interest: mid", "statement": "mid interest", "observed_at": "2026-07-02T00:00:00+00:00"},
            {"title": "interest: new", "statement": "new interest", "observed_at": "2026-07-04T00:00:00+00:00"},
            {"title": "interest: newer", "statement": "", "observed_at": "2026-07-03T00:00:00+00:00"},
        ]},
        "questions": {"open": [{"title": "q", "statement": "why the tide?", "observed_at": "2026-07-05T00:00:00+00:00"}]},
        "problems": {"open": [
            {"title": "p1", "statement": "the script never ran", "observed_at": "2026-07-03T00:00:00+00:00"},
            {"title": "p2", "statement": "the notes are lost", "observed_at": "2026-07-05T00:00:00+00:00"},
        ]},
        "lessons": {"lessons": [{"statement": "fix it now", "observed_at": "2026-07-06T00:00:00+00:00"}], "total": 33},
    }
    facts = memory_facts(card)
    assert INTO_LATELY_BOUND == 3
    # Newest first; a statement-less row falls back to its title (never dropped silently).
    assert [i["text"] for i in facts["into_lately"]] == ["new interest", "interest: newer", "mid interest"]
    # The newest open thing; a problem wins a tie with a question (something is wrong).
    assert facts["on_its_mind"] == {"kind": "problem", "text": "the notes are lost", "at": "2026-07-05T00:00:00+00:00"}
    assert facts["latest_lesson"] == {"text": "fix it now", "at": "2026-07-06T00:00:00+00:00"}
    assert facts["counts"] == {"open_questions": 1, "open_problems": 2, "lessons": 33, "interests": 4}
    assert memory_facts({})["on_its_mind"] is None


def test_last_moment_is_a_moment_a_person_recognises():
    from abstractgateway.entity_tile import last_meaningful_moment

    moments = [
        {"kind": "summon", "at": "2026-08-01T10:00:00+00:00", "details": {}},
        {"kind": "own_time_started", "at": "2026-08-01T11:00:00+00:00", "details": {}},
        {"kind": "diary_read", "at": "2026-09-17T13:37:44+00:00", "details": {"reason": "operator review"}},
        {"kind": "identity_changed", "at": "2026-11-05T04:30:40+00:00", "details": {"cause": "recorded_default"}},
        {"kind": "prompt_overlay_changed", "at": "2026-11-06T00:00:00+00:00", "details": {}},
    ]
    # Operator reads and the birth recording of an identity are not moments of its life; the engraved
    # own_time_* spelling is served as the ruled personal_* kind.
    assert last_meaningful_moment(moments) == {"kind": "personal_started", "at": "2026-08-01T11:00:00+00:00"}
    moments.append({"kind": "phase_changed", "at": "2026-11-07T00:00:00+00:00", "details": {"from": "sleep", "to": "personal"}})
    assert last_meaningful_moment(moments) == {"kind": "phase_changed", "at": "2026-11-07T00:00:00+00:00", "to": "personal"}
    moments.append({"kind": "identity_changed", "at": "2026-11-07T01:00:00+00:00", "details": {"rev": 2, "by": "person:admin"}})
    assert last_meaningful_moment(moments)["kind"] == "identity_changed"  # a real change of look is
    moments.append({"kind": "work_order_changed", "at": "2026-11-07T02:00:00+00:00", "details": {"change": "set"}})
    assert last_meaningful_moment(moments) == {"kind": "work_order_changed", "at": "2026-11-07T02:00:00+00:00", "change": "set"}
    assert last_meaningful_moment([{"kind": "diary_read", "at": "2026-01-01T00:00:00+00:00"}]) is None


def test_now_since_and_the_own_time_failed_trouble():
    from abstractgateway.entity_tile import compose_tile, now_since

    moments = [
        {"kind": "summon", "at": "2026-11-07T09:00:00+00:00", "details": {}},
        {"kind": "session_closed", "at": "2026-11-07T10:00:00+00:00", "details": {}},
        {"kind": "phase_changed", "at": "2026-11-07T10:00:05+00:00", "details": {"to": "personal"}},
    ]
    asleep = {"state": "asleep", "changed_at": "2026-08-01T23:25:22+00:00"}
    awake = {"state": "awake", "changed_at": "2026-11-07T10:00:00+00:00"}
    assert now_since(phase="sleep", state=asleep, loop={}, chat={}, moments=moments) == asleep["changed_at"]
    assert now_since(phase="personal", state=awake, loop={"running": True}, chat={}, moments=moments) == "2026-11-07T10:00:05+00:00"
    assert now_since(phase="visit", state=awake, loop={}, chat={"open": True, "opened_at": "2026-11-07T11:00:00+00:00"}, moments=moments) == "2026-11-07T11:00:00+00:00"
    assert now_since(phase="visit", state=awake, loop={}, chat={}, moments=moments) == "2026-11-07T09:00:00+00:00"
    # Resting by default (awake, nothing running): since the last thing that ended.
    assert now_since(phase="sleep", state=awake, loop={"running": False, "updated_at": "2026-11-07T10:30:00+00:00"}, chat={}, moments=moments) == "2026-11-07T10:30:00+00:00"
    assert now_since(phase=None, state={"state": "paused", "changed_at": "2026-11-01T00:00:00+00:00"}, loop={}, chat={}, moments=moments) == "2026-11-01T00:00:00+00:00"

    def tile(loop, phase="sleep"):
        return compose_tile(
            name="Ephemeral", slug="ephemeral", born="2026-07-11T01:03:25+00:00", look=None, mind=None,
            now={"phase": phase, "sleep_detail": "resting", "working": False, "st": asleep, "loop": loop, "chat": {}},
            moments=moments, memory={}, sleeps=261, as_of_seq=1,
        )

    failed = tile({"running": False, "stopped_by": "failures", "updated_at": "2026-08-01T11:39:22+00:00"})
    assert failed["now"]["trouble"] == {"kind": "own_time_failed", "at": "2026-08-01T11:39:22+00:00"}
    assert failed["now"]["phase"] == "sleep" and failed["now"]["detail"] == "resting"
    assert tile({"running": True, "stopped_by": "failures"})["now"]["trouble"] is None  # restarted
    assert tile({"running": False, "stopped_by": "operator"})["now"]["trouble"] is None
    stopped = tile({}, phase=None)
    assert stopped["now"]["phase"] == "stopped" and stopped["now"]["detail"] is None
    assert failed["counts"]["sleeps"] == 261


def test_discovery_contract_advertises_the_tile_read():
    import abstractgateway.routes.gateway as gateway_routes
    from abstractgateway.security.principal import GatewayPrincipal, reset_current_gateway_principal, set_current_gateway_principal

    token = set_current_gateway_principal(GatewayPrincipal(user_id="u", tenant_id="default", roles=("user",), runtime_id="u"))
    try:
        contracts = gateway_routes._build_client_capability_contracts({})
    finally:
        reset_current_gateway_principal(token)
    assert contracts["common"]["entities"]["tile"] == {"available": True, "version": 1, "endpoint": "/api/gateway/entities/{name}/tile"}


def test_a_recreated_home_never_meets_the_old_lifes_cache(tmp_path: Path):
    """A home restored from a backup (or deleted and re-created under the same name) is another
    memory file: same directory, maybe the same seq, never the old cached card."""
    import os
    import shutil

    from abstractgateway.entities import EntityRegistry
    from abstractgateway.entity_tile import ENGINE_CARD_CACHE

    registry = EntityRegistry(data_dir=tmp_path / "runtime")
    registry.create(name="Phoenix", spark=_spark("Phoenix"))
    registry.card("phoenix")
    misses = ENGINE_CARD_CACHE.misses
    registry.card("phoenix")
    assert ENGINE_CARD_CACHE.misses == misses  # same file, same seq: a hit
    db = registry.entities_dir / "phoenix" / "memory.sqlite3"
    copy = tmp_path / "restored.sqlite3"
    shutil.copy2(db, copy)
    os.replace(copy, db)  # the same bytes in a new file (a restore)
    registry.card("phoenix")
    assert ENGINE_CARD_CACHE.misses == misses + 1
