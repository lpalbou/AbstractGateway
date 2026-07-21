"""Entity lifecycle HTTP endpoints (a2a 0004, deliverable 1).

POST /api/gateway/entities            create (lint -> verbatim spark -> engram -> manifest)
GET  /api/gateway/entities            list
GET  /api/gateway/entities/{name}     inspect (pure reads)
GET  /api/gateway/entities/{name}/verify

And the structural absence: there is NO DELETE route for entities.
"""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")

_TOKEN = "entity-tests-shared-secret"


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


def test_entity_lifecycle_over_http():
    with _client() as client:
        # Create.
        r = client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()})
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["created"] is True
        entity_id = body["entity_id"]
        # Clean keys (plan item 6): new homes engrave entity:<name>.
        assert entity_id == "entity:castor"

        # Idempotent re-create (same spark): re-adoption, not re-creation.
        r2 = client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()})
        assert r2.status_code == 201, r2.text
        assert r2.json()["created"] is False
        assert r2.json()["entity_id"] == entity_id

        # List.
        r3 = client.get("/api/gateway/entities")
        assert r3.status_code == 200
        entities = r3.json()["entities"]
        assert [e["slug"] for e in entities] == ["castor"]

        # Inspect (pure read).
        r4 = client.get("/api/gateway/entities/Castor")
        assert r4.status_code == 200, r4.text
        payload = r4.json()
        assert payload["manifest"]["entity_id"] == entity_id
        assert [v["name"] for v in payload["identity"]["values"]][0] == "shared_vulnerability"

        # Verify: both attestation planes + spark hash.
        r5 = client.get("/api/gateway/entities/Castor/verify")
        assert r5.status_code == 200, r5.text
        report = r5.json()
        assert report["ok"] is True, report


def test_create_refuses_drifted_spark_with_409():
    with _client() as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark()}).status_code == 201
        drifted = _spark()
        drifted["origin"] = "a different origin story"
        r = client.post("/api/gateway/entities", json={"name": "Castor", "spark": drifted})
        assert r.status_code == 409, r.text
        assert "spark" in r.json()["detail"].lower()


def test_create_surfaces_lint_errors():
    with _client() as client:
        spark = _spark()
        spark["values"] = [v for v in spark["values"] if v.get("name") != "shared_vulnerability"]
        r = client.post("/api/gateway/entities", json={"name": "Castor", "spark": spark})
        assert r.status_code == 400, r.text
        assert "shared_vulnerability" in r.json()["detail"]


def test_inspect_unknown_entity_404():
    with _client() as client:
        r = client.get("/api/gateway/entities/nobody")
        assert r.status_code == 404


def test_identity_card_composes_a_life():
    """The identity card (a2a 0009): age, likes/dislikes with recent mood,
    pending questions, discoveries, host-marked moments — all pure reads."""
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
        handlers = svc.host.runtime._handlers

        def _run(channel: str, run_id: str):
            stamp = finalize_summon_stamp(
                mint_summon_stamp(
                    data_dir=registry.data_dir, entity_id=entity_id,
                    channel=channel, session_id="s-card", participants=[],
                ),
                data_dir=registry.data_dir, run_id=run_id,
            )

            class _R:
                pass

            r = _R()
            r.run_id, r.session_id, r.parent_run_id = run_id, "s-card", ""
            r.vars = {"_runtime": {"entity": stamp}}
            return r

        wp = _run(CHANNEL_WORKPLACE, "run-card")
        formed = handlers[EffectType.MEMORY_FORM](
            wp,
            Effect(type=EffectType.MEMORY_FORM, payload={
                "records": [{"kind": "episode", "title": "turn 1", "digest": "the first exchange",
                             "verbatim": "person:test:\nhello\n\nCastor:\nhello.",
                             "attributes": {"mind_substrate": {"provider": "lmstudio", "model": "test-9b"}}}],
                "turn_id": "t-card-1",
            }),
            None,
        )
        assert formed.status == "completed", formed.error

        refl = _run("entity-reflection", "run-card-refl")
        interest = handlers[EffectType.MEMORY_FORM](
            refl,
            Effect(type=EffectType.MEMORY_FORM, payload={
                "records": [{"kind": "interest", "title": "tides",
                             "digest": "what the tide keeps and what it takes back"}],
                "scope": "self", "owner_id": entity_id, "turn_id": "t-card-2",
            }),
            None,
        )
        assert interest.status == "completed", interest.error

        adjusted = handlers[EffectType.MEMORY_APPRAISE](
            refl,
            Effect(type=EffectType.MEMORY_APPRAISE, payload={
                "target_id": "person:laurent", "sign": 1,
                "magnitude": 2, "reason": "he answered my question honestly",
                "scope": "self", "owner_id": entity_id, "turn_id": "t-card-3",
            }),
            None,
        )
        assert adjusted.status == "completed", adjusted.error

        card = client.get("/api/gateway/entities/Castor/card")
        assert card.status_code == 200, card.text
        body = card.json()
        # Gateway overlays.
        assert body["entity_id"] == entity_id
        assert body["age_days"] == 0
        assert body["mind_substrate"] == {"provider": "lmstudio", "model": "test-9b"}
        # Engine compositor sections (one truth; provenance on every field).
        assert body["identity"]["provenance"]
        assert body["age_and_context"]["record_counts"]["life"]["episode"] == 1
        likes = body["likes_dislikes"]["likes"]
        assert likes and likes[0]["target"] == "person:laurent"
        assert "honestly" in " ".join(body["current_state"]["top_reasons"])
        assert any("tide" in d["statement"] for d in body["discoveries"]["interests"])
        assert body["questions"]["open"] == [] and body["questions"]["resolved"] == []

        # Pure read: the card deposits nothing (second read identical seq).
        again = client.get("/api/gateway/entities/Castor/card").json()
        assert again["age_and_context"]["journal_seq"] == body["age_and_context"]["journal_seq"]
        assert "warnings" not in body or not any("regressed" in w for w in body["warnings"])

        # The anchored card (observer's timeline ask): as_of=1 excludes the
        # later feelings/records; substrate is honestly omitted with a label.
        anchored = client.get("/api/gateway/entities/Castor/card?as_of=1")
        assert anchored.status_code == 200, anchored.text
        a_body = anchored.json()
        assert a_body["as_of_seq"] == 1
        assert a_body["likes_dislikes"]["likes"] == []
        assert a_body["mind_substrate"] is None
        assert any("anchored" in w for w in a_body["warnings"])
        # An out-of-range anchor refuses loudly (engine rule, HTTP 400).
        assert client.get("/api/gateway/entities/Castor/card?as_of=99999").status_code == 400

        # Moments merge BOTH ledgers (Ariadne's state_history close):
        # a door transition (marker + history, deduped to one) and a
        # runtime-side transition (history only) both render.
        assert client.post(
            "/api/gateway/entities/Castor/state",
            json={"state": "asleep", "reason": "host under load"},
        ).status_code == 200

        from abstractruntime.identity.life import write_entity_state

        write_entity_state(
            registry.entities_dir / "castor", "awake", reason="loop resumed runtime-side"
        )

        moments = client.get("/api/gateway/entities/Castor/card").json()["moments"]
        state_moments = [(m["kind"], m["details"].get("reason") or "") for m in moments if m["kind"] in ("sleep", "wake")]
        # The door-written sleep carries the client prose PLUS the server's
        # principal stamp (hypnos 10:20 lesson); the runtime-side write has
        # no door, so no stamp — both are honest.
        sleep_reasons = [r for k, r in state_moments if k == "sleep"]
        assert len(sleep_reasons) == 1  # door dedup held
        assert "host under load" in sleep_reasons[0]
        assert "[by person:admin via POST /entities/Castor/state]" in sleep_reasons[0]
        assert ("wake", "loop resumed runtime-side") in state_moments


def test_auth_probe_answers_the_control_strip():
    """The write-classed auth probe (a2a 0007 next-wave queue): an authed
    caller gets operator=true + who they are; an unauthenticated caller is
    refused by the write middleware — exactly the signal the observer's
    control strip gates on."""
    with _client() as client:
        r = client.post("/api/gateway/entities/auth/probe")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["operator"] is True
        assert body["user_id"]

    from abstractgateway.app import app

    with TestClient(app) as anon:  # no Authorization header
        assert anon.post("/api/gateway/entities/auth/probe").status_code in (401, 403)


def test_no_delete_route_exists():
    """Never-purge is structural: the app exposes no DELETE under
    /api/gateway/entities, and this test pins that absence."""
    from abstractgateway.app import app

    for route in app.routes:
        path = str(getattr(route, "path", ""))
        methods = {m.upper() for m in (getattr(route, "methods", None) or set())}
        if path.startswith("/api/gateway/entities"):
            assert "DELETE" not in methods, f"a DELETE route appeared on {path}"
