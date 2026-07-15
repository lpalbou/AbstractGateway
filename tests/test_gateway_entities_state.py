"""Sleep/wake/pause: the gateway verbs + the door (a2a 0008, ask 2).

- Writes go through the runtime's SINGLE state writer (`write_entity_state`);
  the gateway never parses or writes the state file itself.
- Every transition is a host marker (family="host", kinds sleep/wake/pause)
  in the replay stream.
- The summon door refuses non-awake entities (409 naming state + reason) —
  the no-summon window enforced by state, not etiquette.
- `sleep --dream` runs the dream pass inside the window it creates.
"""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractmemory.replay")

_TOKEN = "entity-state-shared-secret"


@pytest.fixture(autouse=True)
def _gateway_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)


def _spark(name: str = "Castor") -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def test_state_writes_carry_server_derived_principal_provenance():
    """The hypnos 10:20:42 lesson: a disputed wake could not be traced to a
    principal because state_history carried only written_by="operator" +
    client prose. The door now appends `[by person:<user_id> via POST
    .../state]` SERVER-side — the client cannot omit or forge it (its own
    prose is preserved but never the last word)."""
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        r = client.post(
            "/api/gateway/entities/Castor/state",
            json={"state": "asleep", "reason": "operator started his own time (web toggle)"},
        )
        assert r.status_code == 200, r.text
        reason = r.json()["state"]["reason"]
        assert "operator started his own time (web toggle)" in reason  # client prose preserved
        assert "[by person:local-admin via POST /entities/Castor/state]" in reason

        # A reason-less write still records the acting principal.
        r2 = client.post("/api/gateway/entities/Castor/state", json={"state": "awake"})
        assert r2.status_code == 200, r2.text
        assert r2.json()["state"]["reason"] == "[by person:local-admin via POST /entities/Castor/state]"


def test_state_verbs_door_and_markers():
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        # NEWBORN = SLEEP (laurent 13:46 totality (c); artifact
        # initial_phase; c1503 shape): create() writes the birth sleep.
        r = client.get("/api/gateway/entities/Castor/state")
        assert r.status_code == 200 and r.json()["state"] == "asleep"
        assert "newborn" in r.json()["reason"]

        # The operator's first wake begins the life's awake stretch.
        assert client.post("/api/gateway/entities/Castor/state", json={"state": "awake"}).status_code == 200
        assert client.get("/api/gateway/entities/Castor/state").json()["state"] == "awake"

        # Sleep with a dream: the pass runs inside the window it creates.
        r2 = client.post(
            "/api/gateway/entities/Castor/state",
            json={"state": "asleep", "reason": "operator maintenance", "dream": True},
        )
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert body["state"]["state"] == "asleep"
        assert body["prior"]["state"] == "awake"
        assert isinstance(body["dream"], dict)  # ran (quiet nights are valid)

        # THE LIVENESS GATE (laurent 16:12: reachability rides the liveness
        # axis — the a2a 0008 no-summon window is RETIRED): a sleeping-but-
        # ALIVE entity is reachable — the summon WAKES it. This deployment
        # has no bundles, so the summon proceeds past the state gate and
        # fails at workflow resolution — the pin is that the STATE never
        # refuses it and the wake landed.
        r3 = client.post("/api/gateway/entities/Castor/summon", json={"prompt": "hi"})
        if r3.status_code == 409:
            assert "asleep" not in str(r3.json().get("detail")), "the retired no-summon window refused a sleeping-alive entity"
        assert client.get("/api/gateway/entities/Castor/state").json()["state"] == "awake"
        assert "woken by summon" in client.get("/api/gateway/entities/Castor/state").json()["reason"]

        # PAUSED is the kill switch: every door refuses, this one included.
        assert client.post("/api/gateway/entities/Castor/state", json={"state": "paused"}).status_code == 200
        r_paused = client.post("/api/gateway/entities/Castor/summon", json={"prompt": "hi"})
        assert r_paused.status_code == 409
        assert "kill switch" in r_paused.json()["detail"]["reasons"][0]

        # A dream outside sleep is refused (dreams need the window).
        r4 = client.post("/api/gateway/entities/Castor/state", json={"state": "awake", "dream": True})
        assert r4.status_code == 400 and "no-summon window" in r4.json()["detail"]

        # Wake: state returns, list/inspect surface it.
        assert client.post("/api/gateway/entities/Castor/state", json={"state": "awake"}).status_code == 200
        entities = client.get("/api/gateway/entities").json()["entities"]
        assert entities[0]["state"]["state"] == "awake"
        assert client.get("/api/gateway/entities/Castor").json()["state"]["state"] == "awake"

        # Unknown state name: loud, naming the set.
        r5 = client.post("/api/gateway/entities/Castor/state", json={"state": "hibernating"})
        assert r5.status_code == 400 and "awake" in r5.json()["detail"]

        # Every transition is a host marker in the replay stream, in order.
        replay = client.get("/api/gateway/entities/Castor/replay?families=host")
        kinds = [
            __import__("json").loads(line)["payload"]["kind"]
            for line in replay.text.splitlines() if line.strip()
        ]
        # The leading wake is the test's own first-wake after the birth
        # sleep (create() writes the birth state directly — no marker; the
        # door-written transitions each land one).
        assert kinds == ["wake", "sleep", "pause", "wake"]
        # The sleep marker carries the dream result (observable story) —
        # second line now (the first is the first-wake marker).
        sleep_marker = __import__("json").loads(replay.text.splitlines()[1])
        assert sleep_marker["payload"]["state"] == "asleep"
        assert isinstance(sleep_marker["payload"]["dream"], dict)


def test_operator_diary_read_is_a_visible_event():
    """The operator diary door (maintainer ruling): private words serve to
    the operator WITH a required reason, and every disclosure is a
    diary_read marker in the stream — reads are visible events."""
    import json as _json

    from abstractgateway.entity_gate import (
        CHANNEL_WORKPLACE,
        finalize_summon_stamp,
        mint_summon_stamp,
    )
    from abstractgateway.service import get_gateway_service
    from abstractruntime.core.models import Effect, EffectType

    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201

        svc = get_gateway_service()
        registry = svc.entity_registry
        entity_id = registry.manifest_for("castor").entity_id
        stamp = finalize_summon_stamp(
            mint_summon_stamp(
                data_dir=registry.data_dir, entity_id=entity_id,
                channel=CHANNEL_WORKPLACE, session_id="s-dr", participants=[],
            ),
            data_dir=registry.data_dir, run_id="run-dr",
        )

        class _Run:
            run_id = "run-dr"
            session_id = "s-dr"
            parent_run_id = ""
            vars = {"_runtime": {"entity": stamp}}

        written = svc.host.runtime._handlers[EffectType.DIARY_WRITE](
            _Run(),
            Effect(type=EffectType.DIARY_WRITE, payload={
                "text": "Private thought: the silverwing project.", "visibility": "private", "turn_id": "t-dr",
            }),
            None,
        )
        assert written.status == "completed", written.error
        entry_id = written.result["entry_id"]

        # Maintainer ruling 2026-07-08: no reason required — the read
        # serves with the default reason, still visibly marked.
        r0 = client.get(f"/api/gateway/entities/Castor/diary/{entry_id}")
        assert r0.status_code == 200, r0.text
        assert "silverwing" in r0.json()["entry"]["text"]

        # An explicit reason still rides when given...
        r = client.get(f"/api/gateway/entities/Castor/diary/{entry_id}?reason=debugging+why+he+rested")
        assert r.status_code == 200, r.text
        assert "silverwing" in r.json()["entry"]["text"]

        # ...and every read is ON THE RECORD in his stream, with its reason.
        replay = client.get("/api/gateway/entities/Castor/replay?families=host")
        markers = [_json.loads(line) for line in replay.text.splitlines() if line.strip()]
        reads = [m for m in markers if m["payload"]["kind"] == "diary_read"]
        assert len(reads) == 2
        assert reads[0]["payload"]["entry_id"] == entry_id
        assert reads[0]["payload"]["visibility"] == "private"
        assert "operator review" in reads[0]["payload"]["reason"]
        assert "debugging" in reads[1]["payload"]["reason"]

        # Unknown entry: 404, no disclosure, no marker.
        assert client.get("/api/gateway/entities/Castor/diary/diary_nope?reason=test").status_code == 404
        replay2 = client.get("/api/gateway/entities/Castor/replay?families=host")
        reads2 = [
            _json.loads(line) for line in replay2.text.splitlines()
            if line.strip() and _json.loads(line)["payload"]["kind"] == "diary_read"
        ]
        assert len(reads2) == 2
