"""Night-voice sweep (wave-5, the seam runtime ruled at c3750): runtime owns
`<home>/night_narrations.jsonl` (append-only, in the home, travels on copy);
the gateway SWEEPS it into its host stream as `night_voice` markers, deduped
on dream_record_id. record-when-present: idempotent, safe on every serving
touch, best-effort (a bad line never breaks serving)."""

from __future__ import annotations

import copy
import json

import pytest


def _spark(name: str) -> dict:
    pytest.importorskip("abstractmemory")
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _write_narration(home_dir, **fields) -> None:
    with (home_dir / "night_narrations.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(fields, ensure_ascii=False) + "\n")


def _form_dream(home) -> tuple:
    """Form a review-gated dream the way the sleep pass does, return
    (graph_id, formation_seq) — the seq the sweep must anchor the marker at
    (memory c3755: formation rides the binding axis)."""
    from abstractmemory.records import MemoryRecordInput

    eid = home.entity_id
    [src] = home.memory.remember_many(
        [MemoryRecordInput(kind="episode", title="a source", digest="a lived moment")],
        scope="life", owner_id=eid, idempotency_key="nv-src")
    [rid] = home.memory.remember_many(
        [MemoryRecordInput(kind="dream", title="a dream",
                           digest="two threads leaned together",
                           attributes={"interpretation_required": True},
                           edges=[("mentions", src)])],
        scope="life", owner_id=eid, idempotency_key="nv-dream")
    seqs = [int(b.seq) for b in home.journal.bindings(record_id=rid, fold=False) if b.seq is not None]
    return rid, min(seqs)


def test_sweep_interleaves_narration_deduped_and_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "nv-secret")
    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": "Bearer nv-secret"}) as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")}).status_code == 201

        from abstractgateway.entity_replay import read_host_markers, sweep_night_narrations
        from abstractgateway.service import get_gateway_service

        registry = get_gateway_service().entity_registry
        home = registry.get_home("castor")
        home_dir = registry.entities_dir / "castor"

        dream_id, formation_seq = _form_dream(home)
        _write_narration(
            home_dir,
            dream_record_id=dream_id,
            narration="The night moved: two threads I had kept apart leaned toward each other.",
            self_label="dreamed, not lived",
            narrated_at="2026-07-20T02:00:00+00:00",
            trigger="scar_touched",
        )

        # First sweep records the marker.
        n1 = sweep_night_narrations(registry.entities_dir, "castor", home)
        assert n1 == 1
        markers = read_host_markers(registry.entities_dir, "castor")
        voices = [m for m in markers if (m.get("payload") or {}).get("kind") == "night_voice"]
        assert len(voices) == 1
        payload = voices[0]["payload"]
        assert payload["dream_record_id"] == dream_id
        assert "two threads" in payload["narration"]
        assert payload["self_label"] == "dreamed, not lived"
        # PRECISE ANCHOR (memory c3755): the marker sits at the dream's
        # formation seq (fractional, base = formation_seq), beside its dream.
        assert formation_seq < float(voices[0]["seq"]) < formation_seq + 1

        # Second sweep is idempotent — dedup on dream_record_id, no new marker.
        n2 = sweep_night_narrations(registry.entities_dir, "castor", home)
        assert n2 == 0
        voices2 = [m for m in read_host_markers(registry.entities_dir, "castor")
                   if (m.get("payload") or {}).get("kind") == "night_voice"]
        assert len(voices2) == 1

        # A second, distinct narration sweeps in; a malformed line is skipped.
        _write_narration(home_dir, dream_record_id="ex:dream-def456",
                         narration="A question I had carried resolved into a quiet knowing.")
        with (home_dir / "night_narrations.jsonl").open("a", encoding="utf-8") as f:
            f.write("{ this is not json\n")
        _write_narration(home_dir, dream_record_id="", narration="no dream link — skipped")
        n3 = sweep_night_narrations(registry.entities_dir, "castor", home)
        assert n3 == 1  # only the well-formed, linked narration
        voices3 = [m for m in read_host_markers(registry.entities_dir, "castor")
                   if (m.get("payload") or {}).get("kind") == "night_voice"]
        # An unknown dream id (no formation binding) still records — it falls
        # back to high-water anchoring (the honest fallback for a narration
        # whose dream the gateway can't resolve).
        assert {v["payload"]["dream_record_id"] for v in voices3} == {dream_id, "ex:dream-def456"}


def test_sweep_no_file_is_zero_and_serving_calls_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "nv2-secret")
    monkeypatch.setenv("ABSTRACTGATEWAY_DEV_READ_NO_AUTH", "1")
    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": "Bearer nv2-secret"}) as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")}).status_code == 201

        from abstractgateway.entity_replay import sweep_night_narrations
        from abstractgateway.service import get_gateway_service

        registry = get_gateway_service().entity_registry
        home = registry.get_home("castor")

        # No narrations file yet — a clean zero, never a raise.
        assert sweep_night_narrations(registry.entities_dir, "castor", home) == 0

        # The bounded replay endpoint runs the sweep before reading markers
        # (record-when-present) — a narration written now appears in /replay.
        home_dir = registry.entities_dir / "castor"
        _write_narration(home_dir, dream_record_id="ex:dream-live",
                         narration="The night's quiet rearranging.")
        r = client.get("/api/gateway/entities/Castor/replay?families=host")
        assert r.status_code == 200, r.text
        kinds = [json.loads(l)["payload"]["kind"] for l in r.text.splitlines() if l.strip()]
        assert "night_voice" in kinds, "the replay route must sweep narrations into the host stream"
