"""A record's stored understanding on every surface the Entity app reads (understanding pass, gateway route part).

AbstractMemory rewrites a record's words in place and keeps every earlier reading as a version (memory 0059). The
gateway serves memory's fields under memory's names, null where the store holds none, on:
- the record read (`GET .../records/{graph_id}/verbatim`, the Detail tab), plus `versions`;
- recall snapshots in the replay stream (`display.recalled`), the reading that entered that context;
- the card (`GET .../card`): interests, lessons and key-moment briefs.
A summary without a verbatim answers 200 with its digest; diary records gain nothing (the diary door stays the only
way their words leave).

MUTATION LIST (each applied to the source, the named test went red, the source restored byte-exact):
- M1 drop "reader" from `UNDERSTANDING_KEYS` -> every surface test (the key set is spelled here, not imported);
- M2 `understanding_fields` keeps only keys memory returned (`if key in source`) -> the null-when-absent asserts;
- M3 remove the summary branch in the verbatim route -> test_summary_without_verbatim_answers_with_its_digest;
- M4 `_reading_at` returns the newest row -> test_recall_snapshots_carry_the_reading_that_was_recalled;
- M5 drop `not is_diary_record(assertion)` from `RecordUnderstandings.read` -> the snapshot and card tests;
- M6 `card_with_understanding` returns the card unchanged -> test_card_briefs_carry_the_current_reading;
- M7 the verbatim route reads the understanding of diary records too -> test_diary_record_read_is_unchanged;
- M8 the understanding gate counts the private flag as diary (`is_diary_shaped` in `read` and the route) ->
  test_private_non_diary_record_read_carries_the_understanding.
"""

from __future__ import annotations

import copy
import json
import warnings
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")

from abstractmemory import DEFAULT_SPARK_TEMPLATE, MemoryRecordInput, apply_redigestion  # noqa: E402

_TOKEN = "entity-understanding-shared-secret"

# The contract, spelled out (never imported from the module under test, so dropping a key there goes red here).
FIELDS = (
    "digest", "digest_method", "digest_version", "version_count", "authored_by", "reader", "understood_at", "cause",
    "target", "target_type", "card_revision", "source_count", "narrated", "narration_version", "replaced_by",
)
MIND = {"kind": "entity-mind", "id": "entity:mind"}
READER = {"provider": "lmstudio", "model": "qwen/qwen3.8-27b", "thinking": False}
STEP = [{"kind": "step", "run_id": "run-understand-1", "seq": 3}]


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _spark(name: str) -> dict:
    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _home(slug: str):
    from abstractgateway.service import get_gateway_service

    registry = get_gateway_service().entity_registry
    return registry.get_home(slug), registry.manifest_for(slug).entity_id


def _form(home, owner: str, key: str, kind: str, digest: str, *, title: str = "", scope: str = "life",
          attributes: Dict[str, Any] | None = None, **kw: Any) -> str:
    attrs = {"digest_method": "mechanical-v2", **(attributes or {})}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        [gid] = home.memory.remember_many(
            [MemoryRecordInput(kind=kind, title=title or f"{kind}: {key}", digest=digest, attributes=attrs, **kw)],
            scope=scope, owner_id=owner, idempotency_key=key)
    return gid


def _rewrite(home, gid: str, text: str, version: int = 0) -> None:
    out = home.memory.rewrite_digest(gid, text=text, author=MIND, reader=READER, sources=STEP, cause="formation",
                                     expected_version=version)
    assert out["applied"] is True, out


def _diary(slug: str, text: str, gist: str, kind: str) -> str:
    """A book entry written through the door (the way an entity writes one); returns its graph projection id."""
    from abstractgateway.entity_gate import CHANNEL_WORKPLACE, finalize_summon_stamp, mint_summon_stamp
    from abstractgateway.service import get_gateway_service
    from abstractruntime.core.models import Effect, EffectType

    svc = get_gateway_service()
    registry = svc.entity_registry
    stamp = finalize_summon_stamp(
        mint_summon_stamp(data_dir=registry.data_dir, entity_id=registry.manifest_for(slug).entity_id,
                          channel=CHANNEL_WORKPLACE, session_id=f"s-{slug}", participants=[]),
        data_dir=registry.data_dir, run_id=f"run-{slug}-diary",
    )

    class _Run:
        run_id = f"run-{slug}-diary"
        session_id = f"s-{slug}"
        parent_run_id = ""
        vars = {"_runtime": {"entity": stamp}}

    out = svc.host.runtime._handlers[EffectType.DIARY_WRITE](
        _Run(), Effect(type=EffectType.DIARY_WRITE, payload={"text": text, "gist": gist, "kind": kind,
                                                             "turn_id": f"t-{slug}-diary"}), None)
    assert out.status == "completed", out.error
    return out.result["projected_record_id"]


def _carries_every_field(block: Dict[str, Any]) -> None:
    missing = [k for k in FIELDS if k not in block]
    assert not missing, f"fields omitted instead of null: {missing}"


def _lines(text: str) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# --------------------------------------------------------------------------- the record read (Detail tab)


def test_record_read_serves_the_stored_understanding_and_versions():
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Lumen", "spark": _spark("Lumen")}).status_code == 201
        home, owner = _home("lumen")
        episode = _form(home, owner, "ep-1", "episode", "User: hi. Lumen: hello.",
                        payload_ref=None, attributes={})
        lesson = _form(home, owner, "lesson-1", "lesson", "Repair before perfection.", title="repair")
        card = _form(home, owner, "card-1", "world_model", "Ada keeps the archive.", title="World model: ada",
                     attributes={"digest_method": "mechanical-card-v2", "target": "person:ada", "revision": 2,
                                 "source_count": 3})
        dream = _form(home, owner, "dream-1", "dream", "Two islands lit up.", title="Dream: islands")
        _rewrite(home, lesson, "Own the mistake, then fix the path.")

        # A rewritten lesson: the reading and its provenance, one to one with memory, plus every version.
        body = client.get(f"/api/gateway/entities/Lumen/records/{lesson}/verbatim").json()
        _carries_every_field(body)
        understood = home.memory.record_understanding(lesson)
        for key in FIELDS:
            assert body[key] == understood.get(key), key
        assert body["text"] == body["digest"] == "Own the mistake, then fix the path."
        assert body["digest_method"] == "authored-v1" and body["digest_version"] == 1 and body["version_count"] == 2
        assert body["authored_by"] == MIND and body["reader"] == READER and body["cause"] == "formation"
        assert body["understood_at"]
        assert body["versions"] == home.memory.versions(lesson)
        assert [v["version"] for v in body["versions"]] == [0, 1]
        assert body["versions"][0]["text"] == "Repair before perfection." and body["versions"][1]["current"] is True
        # Not a card, not a dream, not superseded: those fields are null, never absent.
        for key in ("target", "target_type", "card_revision", "source_count", "narrated", "narration_version",
                    "replaced_by"):
            assert body[key] is None, key

        # A never-rewritten card: target, type, revision and source count; no reader or cause recorded.
        card_body = client.get(f"/api/gateway/entities/Lumen/records/{card}/verbatim").json()
        _carries_every_field(card_body)
        assert card_body["born_digest"] is True and card_body["text"] == "Ada keeps the archive."
        assert card_body["target"] == "person:ada" and card_body["target_type"] == "person"
        assert card_body["card_revision"] == 2 and card_body["source_count"] == 3
        assert card_body["authored_by"] == {"kind": "mechanical", "id": "mechanical-card-v2"}
        assert card_body["digest_version"] == 0 and card_body["version_count"] == 0 and card_body["versions"] == []
        assert card_body["reader"] is None and card_body["cause"] is None and card_body["replaced_by"] is None

        # A dream: narrated or not.
        dream_body = client.get(f"/api/gateway/entities/Lumen/records/{dream}/verbatim").json()
        _carries_every_field(dream_body)
        assert dream_body["narrated"] is False and dream_body["narration_version"] is None
        out = home.memory.narrate_dream(dream, text="I dreamt the islands were one shore.", author=MIND,
                                        reader=READER, sources=STEP)
        assert out["applied"] is True, out
        seq_before = home.memory.current_seq()
        dream_body = client.get(f"/api/gateway/entities/Lumen/records/{dream}/verbatim").json()
        assert dream_body["narrated"] is True and dream_body["narration_version"] == 1

        # An episode without a verbatim stays an honest 404 (only summaries, cards and dreams answer with words).
        assert client.get(f"/api/gateway/entities/Lumen/records/{episode}/verbatim").status_code == 404
        client.get(f"/api/gateway/entities/Lumen/records/{lesson}/verbatim")

        # Reads deposit nothing.
        assert home.memory.current_seq() == seq_before


def test_summary_without_verbatim_answers_with_its_digest():
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Sumi", "spark": _spark("Sumi")}).status_code == 201
        home, owner = _home("sumi")
        ep_a = _form(home, owner, "ep-a", "episode", "User: the map. Sumi: drawn.")
        ep_b = _form(home, owner, "ep-b", "episode", "User: the key. Sumi: found.")
        summary = _form(home, owner, "sum-1", "summary", "[session reflection] the map and the key",
                        title="session reflection: s-1", attributes={"digest_method": "mechanical-floor-v1"},
                        edges=(("summarizes", ep_a), ("summarizes", ep_b)))

        r = client.get(f"/api/gateway/entities/Sumi/records/{summary}/verbatim")
        assert r.status_code == 200, r.text
        body = r.json()
        _carries_every_field(body)
        assert body["text"] == body["digest"] == "[session reflection] the map and the key"
        assert body["born_digest"] is False
        assert sorted(body["summarizes"]) == sorted([ep_a, ep_b])
        assert body["digest_method"] == "mechanical-floor-v1"
        assert body["authored_by"] == {"kind": "mechanical", "id": "mechanical-floor-v1"}
        assert body["versions"] == [] and body["replaced_by"] is None

        # Superseded: the old record names its replacement.
        out = apply_redigestion(home.memory, [{"record_id": summary, "digest": "The map was drawn, the key found."}],
                                actor="operator")
        new_id = out["outcomes"][0]["new_record_id"]
        old = client.get(f"/api/gateway/entities/Sumi/records/{summary}/verbatim").json()
        assert old["replaced_by"] == [new_id]


# --------------------------------------------------------------------------- recall snapshots (replay stream)


def test_recall_snapshots_carry_the_reading_that_was_recalled():
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Tidal", "spark": _spark("Tidal")}).status_code == 201
        home, owner = _home("tidal")
        episode = _form(home, owner, "ep-t", "episode", "User: tides? Tidal: twice a day.")
        lesson = _form(home, owner, "lesson-t", "lesson", "Check the tide table first.")
        diary_gid = _diary("tidal", "The tide words stay in these pages.", "Tide thoughts.", "note")
        home.memory.commit_selection("trace-before", [episode, lesson, diary_gid])
        _rewrite(home, episode, "Tides come twice a day, the moon pulls them.")
        home.memory.commit_selection("trace-after", [episode])

        envelopes = _lines(client.get("/api/gateway/entities/Tidal/replay?families=snapshot").text)
        before, after = [e for e in envelopes if e["family"] == "snapshot"]
        assert before["payload"]["trace_id"] == "trace-before" and after["payload"]["trace_id"] == "trace-after"

        rows = {r["record_id"]: r for r in before["payload"]["display"]}
        blocks = {b["graph_id"]: b for b in before["display"]["recalled"]}
        assert set(blocks) == {episode, lesson}, "diary records are never described here"
        for block in blocks.values():
            _carries_every_field(block)
            assert block["digest"] == rows[block["record_id"]]["digest"], "the words that entered the context"
        # Recalled before the rewrite: the mechanical original, though the record has two readings now.
        ep = blocks[episode]
        assert ep["digest"] == "User: tides? Tidal: twice a day."
        assert ep["digest_version"] == 0 and ep["version_count"] == 2
        assert ep["authored_by"] == {"kind": "mechanical", "id": "mechanical-v2"}
        assert ep["reader"] is None and ep["digest_method"] == "mechanical-v2"
        never = blocks[lesson]
        assert never["version_count"] == 0 and never["cause"] is None and never["replaced_by"] is None
        assert never["target"] is None and never["narrated"] is None

        (now,) = after["display"]["recalled"]
        assert now["digest"] == "Tides come twice a day, the moon pulls them." == after["payload"]["display"][0]["digest"]
        assert now["digest_version"] == 1 and now["authored_by"] == MIND and now["reader"] == READER
        assert now["cause"] == "formation" and now["digest_method"] == "authored-v1"

        # The diary words never ride a block; enrich=false is the pure ledger (no display at all).
        assert "stay in these pages" not in json.dumps(envelopes)
        raw = _lines(client.get("/api/gateway/entities/Tidal/replay?families=snapshot&enrich=false").text)
        assert all("display" not in e for e in raw)

        # The live tail (SSE chunks) carries the same blocks.
        from abstractgateway.routes.entity_replay import _collect_replay_chunk
        from abstractgateway.service import get_gateway_service

        registry = get_gateway_service().entity_registry
        envs, _, _ = _collect_replay_chunk(home, registry.entities_dir, "tidal", 0.0, ["snapshot"], True, 50)
        assert [e["display"]["recalled"] for e in envs] == [before["display"]["recalled"],
                                                            after["display"]["recalled"]]


# --------------------------------------------------------------------------- the card


def test_card_briefs_carry_the_current_reading():
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Corvid", "spark": _spark("Corvid")}).status_code == 201
        home, owner = _home("corvid")
        interest = _form(home, owner, "int-c", "interest", "Why crows remember faces.", title="crow memory",
                         scope="self")
        lesson = _form(home, owner, "lesson-c", "lesson", "Feed them the same time each day.", title="routine")
        _rewrite(home, lesson, "A routine earns their trust.")
        question = _diary("corvid", "Do the crows know my name, privately?", "A crow question.", "question")
        book_lesson = _diary("corvid", "Never chase them, in my own private words.", "Patience.", "lesson")
        seq_before = home.memory.current_seq()

        card = client.get("/api/gateway/entities/Corvid/card").json()
        (lesson_brief,) = [b for b in card["lessons"]["lessons"] if b["record_id"] == lesson]
        _carries_every_field(lesson_brief)
        assert lesson_brief["digest"] == lesson_brief["statement"] == "A routine earns their trust."
        assert lesson_brief["digest_version"] == 1 and lesson_brief["version_count"] == 2
        assert lesson_brief["authored_by"] == MIND and lesson_brief["reader"] == READER

        (interest_brief,) = [b for b in card["discoveries"]["interests"] if b["record_id"] == interest]
        _carries_every_field(interest_brief)
        assert interest_brief["digest"] == "Why crows remember faces." and interest_brief["digest_version"] == 0
        assert interest_brief["reader"] is None and interest_brief["target"] is None
        firsts = [m for m in card["key_moments"]["moments"] if m.get("record_id") == interest]
        assert firsts and all(set(FIELDS) <= set(m) for m in firsts)

        # Diary briefs are untouched: no digest, no provenance (a book lesson sits in the walked lessons list).
        diary_briefs = [b for b in card["questions"]["open"] if b.get("record_id") == question]
        diary_briefs += [b for b in card["lessons"]["lessons"] if b.get("record_id") == book_lesson]
        assert len(diary_briefs) == 2, "the diary question and the book lesson must be on the card"
        assert not any(set(FIELDS) & set(b) for b in diary_briefs)
        assert "privately" not in json.dumps(card) and "private words" not in json.dumps(card)

        assert home.memory.current_seq() == seq_before


# --------------------------------------------------------------------------- the diary door stays the only door


def test_diary_record_read_is_unchanged():
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Quill", "spark": _spark("Quill")}).status_code == 201
        diary_gid = _diary("quill", "The sealed words of Quill.", "Sealed.", "note")
        body = client.get(f"/api/gateway/entities/Quill/records/{diary_gid}/verbatim").json()
        assert set(body) == {"record_id", "title", "text", "content_type", "turn_id", "run_id", "created_at"}
        assert "The sealed words of Quill." in body["text"]
        from abstractgateway.entity_replay import merged_replay
        from abstractgateway.service import get_gateway_service

        registry = get_gateway_service().entity_registry
        home = registry.get_home("quill")
        reads = [e for e in merged_replay(home, entities_dir=registry.entities_dir, slug="quill", families=["host"])
                 if e["payload"].get("kind") == "diary_read"]
        assert reads, "the diary read is still recorded in the entity's history"

        # A diary-scope record without a book entry: the former answer exactly, nothing added.
        sealed = _form(home, registry.manifest_for("quill").entity_id, "sealed-1", "lesson", "Kept in the book scope.",
                       scope="diary")
        body = client.get(f"/api/gateway/entities/Quill/records/{sealed}/verbatim").json()
        assert set(body) == {"record_id", "title", "text", "content_type", "born_digest", "turn_id", "run_id",
                             "created_at"}



def test_private_non_diary_record_read_carries_the_understanding():
    """Visibility is not the diary: a private lesson's read carries every field and its versions."""
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Vesper", "spark": _spark("Vesper")}).status_code == 201
        home, owner = _home("vesper")
        private = _form(home, owner, "private-1", "lesson", "Kept to myself.", attributes={"private": True})
        _rewrite(home, private, "Kept to myself, and why.")
        body = client.get(f"/api/gateway/entities/Vesper/records/{private}/verbatim").json()
        _carries_every_field(body)
        assert body["text"] == body["digest"] == "Kept to myself, and why."
        assert body["digest_version"] == 1 and body["authored_by"] == MIND and body["reader"] == READER
        assert [v["version"] for v in body["versions"]] == [0, 1]
        assert body["versions"] == home.memory.versions(private)
