"""The entity replay serving end (a2a 0005: gateway lane of the stream).

Covers: host-marker mechanics (fractional seqs, append-only log), merged
ordering (markers interleave with journal envelopes by seq), the bounded
NDJSON endpoint (cursor resume, family filters incl. the reserved "host",
diary display redaction surviving transport), summon/refusal markers, and
an SSE first-event smoke.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractmemory.replay")

from abstractmemory import DEFAULT_SPARK_TEMPLATE  # noqa: E402

from abstractgateway.entities import EntityRegistry  # noqa: E402
from abstractgateway.entity_replay import (  # noqa: E402
    merged_replay,
    read_host_markers,
    record_host_marker,
    validate_families,
)

_TOKEN = "entity-replay-shared-secret"


def _spark(name: str = "Castor") -> dict:
    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


# ------------------------------------------------------------ unit: markers


def test_marker_fractional_seqs_and_read_window(tmp_path: Path):
    registry = EntityRegistry(data_dir=tmp_path / "runtime")
    created = registry.create(name="Castor", spark=_spark())

    m1 = record_host_marker(
        entities_dir=registry.entities_dir, slug="castor", entity_id=created.entity_id,
        kind="summon", journal_seq=13, run_id="run-a", session_id="s-a",
    )
    m2 = record_host_marker(
        entities_dir=registry.entities_dir, slug="castor", entity_id=created.entity_id,
        kind="prelude_refused", journal_seq=13, details={"reasons": ["#REFUSED ..."]},
    )
    assert m1["seq"] == 13.001 and m2["seq"] == 13.002  # same base increments
    assert m1["family"] == "host" and m1["payload"]["kind"] == "summon"

    # since_seq is exclusive and fractional-aware.
    assert [m["seq"] for m in read_host_markers(registry.entities_dir, "castor", since_seq=13.001)] == [13.002]

    with pytest.raises(ValueError, match="unknown host marker kind"):
        record_host_marker(
            entities_dir=registry.entities_dir, slug="castor", entity_id=created.entity_id,
            kind="not-a-kind", journal_seq=13,
        )


def test_merged_stream_orders_markers_between_journal_items(tmp_path: Path):
    registry = EntityRegistry(data_dir=tmp_path / "runtime")
    created = registry.create(name="Castor", spark=_spark())
    home = registry.get_home("castor")
    try:
        hw = int(home.memory.current_seq())
        assert hw > 0, "the engram must have journaled identity bindings"
        # A marker mid-journal and one at the high-water mark.
        record_host_marker(
            entities_dir=registry.entities_dir, slug="castor", entity_id=created.entity_id,
            kind="summon", journal_seq=hw // 2, run_id="run-a",
        )
        record_host_marker(
            entities_dir=registry.entities_dir, slug="castor", entity_id=created.entity_id,
            kind="session_closed", journal_seq=hw, run_id="run-a",
        )

        envelopes = list(merged_replay(home, entities_dir=registry.entities_dir, slug="castor"))
        seqs = [float(e["seq"]) for e in envelopes]
        assert seqs == sorted(seqs), "merged stream must be strictly seq-ordered"
        assert len(seqs) == len(set(seqs)), "no seq collisions between markers and journal items"
        families = {e["family"] for e in envelopes}
        assert "host" in families and "binding" in families
        # The mid-journal marker sits between its base and base+1.
        marker_seq = next(float(e["seq"]) for e in envelopes if e["family"] == "host")
        assert (hw // 2) < marker_seq < (hw // 2) + 1

        # Family filter without "host" excludes markers; with only "host",
        # only markers.
        no_host = list(merged_replay(home, entities_dir=registry.entities_dir, slug="castor", families=["binding"]))
        assert {e["family"] for e in no_host} == {"binding"}
        only_host = list(merged_replay(home, entities_dir=registry.entities_dir, slug="castor", families=["host"]))
        assert {e["family"] for e in only_host} == {"host"}

        # The confirmed resume rule (0005): a fractional cursor AT a marker
        # excludes that marker AND its journal base, and continues with the
        # next item — no repeats, no gaps.
        resumed = list(
            merged_replay(home, entities_dir=registry.entities_dir, slug="castor", since_seq=marker_seq)
        )
        resumed_seqs = [float(e["seq"]) for e in resumed]
        assert resumed_seqs == [s for s in seqs if s > marker_seq]
        assert resumed_seqs and resumed_seqs[0] == (hw // 2) + 1
    finally:
        registry.close_all()


def test_validate_families_names_valid_and_reserved():
    assert validate_families(None) is None
    assert validate_families("event,host") == ["event", "host"]
    with pytest.raises(ValueError, match="reserved"):
        validate_families("event,bogus")


# ------------------------------------------------------------- HTTP surface


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _lines(text: str) -> list:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_replay_endpoint_bounded_read_and_cursor_resume():
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        # A refused summon writes a host marker (journal-invisible moment).
        r = client.post(
            "/api/gateway/entities/Castor/summon",
            json={"prompt": "hi", "prelude_budget": 16},
        )
        assert r.status_code == 409

        r2 = client.get("/api/gateway/entities/Castor/replay")
        assert r2.status_code == 200, r2.text
        envelopes = _lines(r2.text)
        assert envelopes, "the engram journal must replay"
        seqs = [float(e["seq"]) for e in envelopes]
        assert seqs == sorted(seqs)
        assert all(e["stream"] == "abstractmemory.replay" and e["stream_version"] == 1 for e in envelopes)
        refusals = [e for e in envelopes if e["family"] == "host" and e["payload"]["kind"] == "prelude_refused"]
        assert len(refusals) == 1
        assert any("#REFUSED" in reason for reason in refusals[0]["payload"]["reasons"])

        # Cursor resume: everything strictly after the midpoint, no repeats.
        mid = seqs[len(seqs) // 2]
        r3 = client.get(f"/api/gateway/entities/Castor/replay?since_seq={mid}")
        resumed = _lines(r3.text)
        assert [float(e["seq"]) for e in resumed] == [s for s in seqs if s > mid]

        # Unknown family -> 400 naming valid + reserved sets.
        r4 = client.get("/api/gateway/entities/Castor/replay?families=bogus")
        assert r4.status_code == 400
        assert "reserved" in r4.json()["detail"]

        r5 = client.get("/api/gateway/entities/nobody/replay")
        assert r5.status_code == 404


def test_diary_redaction_survives_transport():
    """Operator-audience serving (maintainer ruling 2026-07-08 21:39: "the
    ledger is the ledger — we must see the content and ideally a 1-sentence
    summary"): the engine still marks diary blocks redacted; THIS serving
    end — whose consumers are all operator surfaces — resolves the mark
    into the entry's GIST. The full text never rides the stream (one click
    away via the diary door); future federation surfaces are deny-by-default
    routes that never reach merged_replay."""
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

    from abstractgateway.entity_gate import (
        CHANNEL_WORKPLACE,
        finalize_summon_stamp,
        install_entity_routing,
        mint_summon_stamp,
    )
    from abstractgateway.service import get_gateway_service
    from abstractruntime.core.models import Effect, EffectType

    # Write a diary entry through the real gate (the service runtime already
    # has entity routing installed).
    svc = get_gateway_service()
    registry = svc.entity_registry
    created_id = registry.manifest_for("castor").entity_id
    stamp = finalize_summon_stamp(
        mint_summon_stamp(
            data_dir=registry.data_dir, entity_id=created_id,
            channel=CHANNEL_WORKPLACE, session_id="s-diary", participants=[],
        ),
        data_dir=registry.data_dir, run_id="run-d",
    )

    class _Run:
        run_id = "run-d"
        session_id = "s-diary"
        parent_run_id = ""
        vars = {"_runtime": {"entity": stamp}}

    handler = svc.host.runtime._handlers[EffectType.DIARY_WRITE]
    out = handler(
        _Run(),
        Effect(type=EffectType.DIARY_WRITE, payload={
            "text": "The silverfin project stays private to these pages.",
            "gist": "Silverfin thoughts.",
            "kind": "note",
            "turn_id": "t-d1",
        }),
        None,
    )
    assert out.status == "completed", out.error

    home = registry.get_home("castor")
    envelopes = list(merged_replay(home, entities_dir=registry.entities_dir, slug="castor"))
    diary_displays = [
        e["display"] for e in envelopes
        if isinstance(e.get("display"), dict) and (
            e["display"].get("redacted") == "diary" or e["display"].get("kind") == "diary"
        )
    ]
    assert diary_displays, "the diary projection must appear in the stream"
    # Operator audience: the gist (1-sentence summary) is VISIBLE; the mark
    # is resolved, not forwarded. The full entry TEXT still never rides the
    # stream — content is one click away through the diary door.
    for d in diary_displays:
        assert d.get("kind") == "diary", d
        assert d.get("redacted") is None, d
        assert "Silverfin thoughts." in str(d.get("title") or d.get("gist") or ""), d
        assert d.get("entry_id"), d
    text = json.dumps(envelopes)
    assert "stays private to these pages" not in text, "full diary text leaked into the stream (gist only)"

    # The engine's own export stays marked (the audience seam lives at the
    # SERVING end — memory's default contract is unchanged).
    raw = list(home.memory.export_replay(families=["binding"]))
    raw_diary = [e["display"] for e in raw if isinstance(e.get("display"), dict) and e["display"].get("redacted") == "diary"]
    assert raw_diary, "engine-side export must still carry the redaction mark"


def test_verbatim_on_click_endpoint():
    """Verbatim-on-click (a2a 0007, observer's ask; maintainer ruling
    2026-07-08: the operator reads EVERYTHING — no 403s by design): the
    lossless exchange behind a record serves from the HOME's artifact
    store; diary-shaped records serve the book entry with a visible
    diary_read host marker; absence is honest 404; reads deposit nothing
    into the journal."""
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

    from abstractgateway.entity_gate import (
        CHANNEL_WORKPLACE,
        finalize_summon_stamp,
        mint_summon_stamp,
    )
    from abstractgateway.service import get_gateway_service
    from abstractruntime.core.models import Effect, EffectType

    svc = get_gateway_service()
    registry = svc.entity_registry
    entity_id = registry.manifest_for("castor").entity_id
    stamp = finalize_summon_stamp(
        mint_summon_stamp(
            data_dir=registry.data_dir, entity_id=entity_id,
            channel=CHANNEL_WORKPLACE, session_id="s-verbatim", participants=[],
        ),
        data_dir=registry.data_dir, run_id="run-v",
    )

    class _Run:
        run_id = "run-v"
        session_id = "s-verbatim"
        parent_run_id = ""
        vars = {"_runtime": {"entity": stamp}}

    handlers = svc.host.runtime._handlers
    formed = handlers[EffectType.MEMORY_FORM](
        _Run(),
        Effect(type=EffectType.MEMORY_FORM, payload={
            "records": [
                {"title": "turn 1", "digest": "the first exchange",
                 "verbatim": "person:test:\nhello Castor\n\nCastor:\nhello."},
                {"title": "turn 2", "digest": "an exchange with no verbatim"},
            ],
            "turn_id": "t-v1",
        }),
        None,
    )
    assert formed.status == "completed", formed.error
    with_verbatim, without_verbatim = formed.result["record_ids"]

    diary = handlers[EffectType.DIARY_WRITE](
        _Run(),
        Effect(type=EffectType.DIARY_WRITE, payload={
            "text": "The sealed words.", "gist": "Sealed.", "kind": "note", "turn_id": "t-v2",
        }),
        None,
    )
    assert diary.status == "completed", diary.error
    diary_graph_id = diary.result["projected_record_id"]

    # The leak class (observer's adversarial find, a2a 0007): a life-scope
    # verbatim carrying a RAW diary fence — formed here, refused below.
    leaked = handlers[EffectType.MEMORY_FORM](
        _Run(),
        Effect(type=EffectType.MEMORY_FORM, payload={
            "records": [
                {"title": "turn with leak", "digest": "an exchange",
                 "verbatim": "person:test:\nhi\n\nCastor:\n```diary kind=reflection visibility=private\nsecret words\n```\nreply."}
            ],
            "turn_id": "t-v3",
        }),
        None,
    )
    assert leaked.status == "completed", leaked.error
    (leaked_id,) = leaked.result["record_ids"]

    # A born-digest record (interest, formed from the entity's own channel:
    # identity kinds are entity-reflection acts).
    refl_stamp = finalize_summon_stamp(
        mint_summon_stamp(
            data_dir=registry.data_dir, entity_id=entity_id,
            channel="entity-reflection", session_id="s-verbatim", participants=[entity_id],
        ),
        data_dir=registry.data_dir, run_id="run-v-refl",
    )

    class _ReflRun:
        run_id = "run-v-refl"
        session_id = "s-verbatim"
        parent_run_id = ""
        vars = {"_runtime": {"entity": refl_stamp}}

    interest = handlers[EffectType.MEMORY_FORM](
        _ReflRun(),
        Effect(type=EffectType.MEMORY_FORM, payload={
            "records": [{"kind": "interest", "title": "the mortal twin",
                         "digest": "the myth of the mortal twin, and what finitude makes precious"}],
            "scope": "self", "owner_id": entity_id, "turn_id": "refl-v1",
        }),
        None,
    )
    assert interest.status == "completed", interest.error
    (interest_id,) = interest.result["record_ids"]

    with _client() as client:
        # The app context owns the registry; measure purity inside it (the
        # cached home closes with the app on context exit).
        home = get_gateway_service().entity_registry.get_home("castor")
        seq_before = home.memory.current_seq()

        r = client.get(f"/api/gateway/entities/Castor/records/{with_verbatim}/verbatim")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["text"].startswith("person:test:")
        assert body["record_id"] == with_verbatim
        assert body["turn_id"] == "t-v1"

        # Honest absence: formed without verbatim -> 404 naming the reason.
        r2 = client.get(f"/api/gateway/entities/Castor/records/{without_verbatim}/verbatim")
        assert r2.status_code == 404 and "no payload_ref" in r2.json()["detail"]

        # Maintainer ruling 2026-07-08: a diary projection SERVES the book
        # entry (no 403), and the read lands as a visible diary_read host
        # marker in the merged stream — transparency, not refusal.
        r3 = client.get(f"/api/gateway/entities/Castor/records/{diary_graph_id}/verbatim")
        assert r3.status_code == 200, r3.text
        assert "The sealed words." in r3.json()["text"]
        marker_stream = list(merged_replay(home, entities_dir=registry.entities_dir, slug="castor", families=["host"]))
        diary_reads = [e for e in marker_stream if e.get("payload", {}).get("kind") == "diary_read"]
        assert diary_reads, "the diary read must land as a visible host marker"

        # Unknown record -> 404.
        r4 = client.get("/api/gateway/entities/Castor/records/ex:nothing-here/verbatim")
        assert r4.status_code == 404

        # Maintainer ruling 2026-07-08: the former leak-backstop 403 is
        # gone — the operator reads the artifact as it exists (the
        # formation-side strip fix remains runtime's item).
        r5 = client.get(f"/api/gateway/entities/Castor/records/{leaked_id}/verbatim")
        assert r5.status_code == 200, r5.text
        assert "secret words" in r5.json()["text"]

        # Identity records: their verbatim IS the attested seed (the
        # engram's payload_ref is the spark FILE, not an artifact id) —
        # serve the spark text, never a 500 (live-incident regression).
        inspection = client.get("/api/gateway/entities/Castor").json()
        value_id = inspection["identity"]["values"][0]["record_id"]
        r6 = client.get(f"/api/gateway/entities/Castor/records/{value_id}/verbatim")
        assert r6.status_code == 200, r6.text
        assert "shared_vulnerability" in r6.json()["text"]

        # Born-digest kinds (round 2): an interest has no payload_ref
        # because its digest IS its complete text — answer with the words
        # and say so, instead of a 404 that reads like an error.
        r7 = client.get(f"/api/gateway/entities/Castor/records/{interest_id}/verbatim")
        assert r7.status_code == 200, r7.text
        born = r7.json()
        assert born["born_digest"] is True
        assert "mortal twin" in born["text"]

        # Pure read: nothing deposited by any of the reads above.
        assert home.memory.current_seq() == seq_before


def test_sse_stream_first_event_and_cursor_resume():
    """The live tail never terminates by design (a life has no terminal
    state), so this exercises the route's async generator directly instead
    of holding an infinite HTTP stream open under the test client."""
    import asyncio

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        from abstractgateway.routes.entity_replay import stream_entity_replay

        async def _first_event(since_seq: float = 0.0) -> str:
            response = await stream_entity_replay(
                "Castor", since_seq=since_seq, families=None, enrich=True,
                heartbeat_s=5.0, poll_s=0.05, last_event_id=None,
            )
            assert response.media_type == "text/event-stream"
            gen = response.body_iterator
            buffer = ""
            try:
                while "\n\n" not in buffer:
                    buffer += (await gen.__anext__()).decode("utf-8")
            finally:
                await gen.aclose()
            return buffer.split("\n\n")[0]

        first = asyncio.run(_first_event())
        assert first.startswith("id: ")
        assert "event: replay" in first
        payload = json.loads(first.split("data: ", 1)[1])
        assert payload["stream"] == "abstractmemory.replay"
        assert float(payload["seq"]) >= 1.0

        # Last-Event-ID style resume: starting past the first seq yields a
        # strictly later item.
        later = asyncio.run(_first_event(since_seq=float(payload["seq"])))
        later_payload = json.loads(later.split("data: ", 1)[1])
        assert float(later_payload["seq"]) > float(payload["seq"])
