"""The sleep-candidate review desk (W3 second half, wave-4 dispatch c3291):
sleep PROPOSES (inactive review-gated candidates), waking evidence DISPOSES
(promote with the independence test / reject with the mandatory reason). The
door lists via the PUBLIC TripleQuery surface and wraps the engine verbs
with the principal-stamped actor; graph acts are journal-recorded engine-side
(no host marker — markers are for door/config acts)."""

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


def _plant_candidate(home) -> str:
    """Form two source records + one review-gated candidate the way the
    sleep pass does (attributes.maintenance_candidate + review_required).
    Uses the memory SYSTEM facade (home.memory.remember_many — the same
    surface the diary-trail test forms through)."""
    from abstractmemory.records import MemoryRecordInput

    eid = home.entity_id
    sources = []
    for i in (1, 2):
        [sid] = home.memory.remember_many(
            [MemoryRecordInput(kind="episode", title=f"source {i}",
                               digest=f"source {i}: watched the tide change")],
            scope="life", owner_id=eid, idempotency_key=f"cand-src-{i}")
        sources.append(sid)
    # A summary must name its sources (engine guard) — the sleep pass forms
    # candidates with summarizes edges; mirror it.
    [rid] = home.memory.remember_many(
        [MemoryRecordInput(kind="summary", title="Consolidated: tide",
                           digest="Consolidated: the tide-change thread",
                           attributes={"maintenance_candidate": True, "review_required": True},
                           edges=[("summarizes", s) for s in sources])],
        scope="life", owner_id=eid, idempotency_key="cand-1")
    return rid


def test_candidates_list_promote_refusal_and_reject(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "cand-secret")
    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": "Bearer cand-secret"}) as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")}).status_code == 201

        from abstractgateway.service import get_gateway_service

        registry = get_gateway_service().entity_registry
        home = registry.get_home("castor")
        rid = _plant_candidate(home)

        # The desk lists the standing candidate with its review flag.
        r = client.get("/api/gateway/entities/Castor/candidates")
        assert r.status_code == 200, r.text
        cands = r.json()["candidates"]
        mine = next(c for c in cands if c["record_id"] == rid)
        assert mine["review_required"] is True and mine["kind"] == "summary"

        # Promote with THIN evidence refuses loudly (the engine's
        # independence test — the door passes the human-written error).
        thin = client.post(
            f"/api/gateway/entities/Castor/candidates/{rid}/promote",
            json={"corroborating_ids": [], "reason": "no evidence at all"},
        )
        assert thin.status_code == 400, thin.text

        # Reject with the mandatory reason lands (the honest no).
        rej = client.post(
            f"/api/gateway/entities/Castor/candidates/{rid}/reject",
            json={"reason": "duplicate of a settled thread"},
        )
        assert rej.status_code == 200, rej.text
        assert rej.json()["rejected"] is True

        # Unknown candidate 404s naming the miss.
        miss = client.post(
            "/api/gateway/entities/Castor/candidates/ex:nothere/reject",
            json={"reason": "x-marks-nothing"},
        )
        assert miss.status_code == 404


def test_candidate_verbs_are_admin_gated() -> None:
    import re

    from abstractgateway.security.authorization import GATEWAY_ROUTE_POLICIES

    pats = [p.pattern for p in GATEWAY_ROUTE_POLICIES if getattr(p, "pattern", None) and "candidates" in p.pattern]
    assert pats, "candidate verbs must be admin-gated"
    pat = re.compile(pats[0])
    assert pat.match("/api/gateway/entities/castor/candidates/ex:abc/promote")
    assert pat.match("/api/gateway/entities/castor/candidates/ex:abc/reject")


def test_console_carries_the_review_desk() -> None:
    from abstractgateway.console import gateway_console_html

    html = gateway_console_html()
    for el in ("entity-candidates-box", "entity-candidates-list", "entity-candidates-count"):
        assert f'id="{el}"' in html, f"missing {el}"
    assert "loadEntityCandidates" in html
