"""G1 serving half — the drive ratios on the gateway wire (cognition-health
directive 2026-07-18).

Memory's `cognition_health()` reads the same ladder + diary convention as
the card compositor; the gateway serves it through
`EntityHome.cognition_drives()` (the home owns the ladder — self/diary/life,
the diary pair load-bearing per runtime's fold note) onto two surfaces:
`/cognition` (always, render-when-present with labeled degrade) and the
roster (WARM homes only — the roster stays file-cheap, it never opens a
store). Absent ≠ zero, ratios are DATA (a 0/0 life has ratio None, never a
fabricated 100%).
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


def _seed_drives(home) -> None:
    """Two diary questions (one answered), one self interest, one life
    episode exploring it — writes span all three ladder scopes so the fold
    only balances if the gateway passes the FULL ladder."""
    from abstractmemory.records import MemoryRecordInput

    eid = home.entity_id
    [q1] = home.memory.remember_many(
        [MemoryRecordInput(kind="diary", title="Q1", digest="What persists?",
                           attributes={"diary_type": "question"},
                           provenance={"source": "owner-direct"})],
        scope="diary", owner_id=eid, idempotency_key="q1")
    home.memory.remember_many(
        [MemoryRecordInput(kind="diary", title="Q2", digest="Why do tags fail?",
                           attributes={"diary_type": "question"},
                           provenance={"source": "owner-direct"})],
        scope="diary", owner_id=eid, idempotency_key="q2")
    home.memory.remember_many(
        [MemoryRecordInput(kind="diary", title="A1", digest="Traces persist.",
                           attributes={"answers": q1},
                           provenance={"source": "owner-direct"})],
        scope="diary", owner_id=eid, idempotency_key="a1")
    [i1] = home.memory.remember_many(
        [MemoryRecordInput(kind="interest", title="interest: tides",
                           digest="How tides shape harbors.")],
        scope="self", owner_id=eid, idempotency_key="i1")
    home.memory.remember_many(
        [MemoryRecordInput(kind="episode", title="tide walk",
                           digest="Walked the tide line.",
                           attributes={"explores": i1})],
        scope="life", owner_id=eid, idempotency_key="explore-1")


def test_cognition_wire_serves_drives_from_the_full_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    """/cognition carries `drives` with the engine's exact numbers: the seed
    writes to diary (questions), self (interest), and life (explores), so a
    ladder missing any pair would show wrong counts here."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "drives-secret")
    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": "Bearer drives-secret"}) as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")}).status_code == 201

        from abstractgateway.service import get_gateway_service

        registry = get_gateway_service().entity_registry
        _seed_drives(registry.get_home("Castor"))

        cog = client.get("/api/gateway/entities/Castor/cognition")
        assert cog.status_code == 200, cog.text
        drives = cog.json().get("drives")
        assert drives is not None, "drives must ride /cognition when the engine serves the fold"
        # Per-key asserts, not dict equality — the shape is memory's to
        # evolve (the display-block over-pinning lesson).
        assert drives["questions"]["open"] == 1
        assert drives["questions"]["resolved"] == 1
        assert drives["questions"]["ratio"] == 0.5
        assert drives["interests"]["explored"] == 1
        assert drives["interests"]["open"] == 0
        # Empty category: ratio None — a life with no problems has NO bar,
        # never a fabricated 100%.
        assert drives["problems"]["ratio"] is None


def test_roster_drives_render_when_present_warm_homes_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The roster is file-cheap by contract: drives appear only for homes
    already open in this process; closing the home drops the field (absent,
    not zero) without touching the stored life."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "drives-roster-secret")
    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": "Bearer drives-roster-secret"}) as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")}).status_code == 201

        from abstractgateway.service import get_gateway_service

        registry = get_gateway_service().entity_registry
        _seed_drives(registry.get_home("Castor"))  # get_home warms the home

        roster = client.get("/api/gateway/entities").json()["entities"]
        castor = next(e for e in roster if e.get("slug") == "castor")
        assert castor.get("drives", {}).get("questions", {}).get("open") == 1

        # Cold home: evict the warm handle — the roster drops the field
        # rather than opening a store per roster poll.
        with registry._open_lock:
            home = registry._open_homes.pop("castor", None)
        if home is not None:
            home.close()
        roster = client.get("/api/gateway/entities").json()["entities"]
        castor = next(e for e in roster if e.get("slug") == "castor")
        assert "drives" not in castor


def test_roster_drives_fold_is_seq_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """P1 (code-tui c4307, roster-latency): the per-row cognition_drives fold
    is seq-cached — repeated roster calls at the same journal seq run the
    fold ONCE, and a journal advance re-folds. Uncached, N warm homes stacked
    ~90s reads per list into the shared threadpool (live 5.6s + timeouts)."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "drives-cache-secret")
    from abstractgateway.app import app
    import abstractgateway.entities as entities_mod

    with TestClient(app, headers={"Authorization": "Bearer drives-cache-secret"}) as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")}).status_code == 201

        from abstractgateway.service import get_gateway_service

        registry = get_gateway_service().entity_registry
        home = registry.get_home("Castor")
        _seed_drives(home)

        calls = {"n": 0}
        real = entities_mod.EntityHome.cognition_drives

        def _counting(self):
            calls["n"] += 1
            return real(self)

        monkeypatch.setattr(entities_mod.EntityHome, "cognition_drives", _counting)
        entities_mod._ROSTER_DRIVES_CACHE.clear()

        # Three roster reads at one journal seq: exactly ONE fold.
        for _ in range(3):
            roster = client.get("/api/gateway/entities").json()["entities"]
            castor = next(e for e in roster if e.get("slug") == "castor")
            assert castor.get("drives", {}).get("questions", {}).get("open") == 1
        assert calls["n"] == 1, f"seq-cached roster must fold once, folded {calls['n']}x"

        # A journal-seq change invalidates: poison the cache with a stale seq
        # (the deterministic form of "the life grew") and assert the next
        # roster read re-folds. Testing the seq-mismatch->refold logic
        # directly, not the journal-write mechanics of a seed.
        key = next(iter(entities_mod._ROSTER_DRIVES_CACHE))
        stale_seq, drives = entities_mod._ROSTER_DRIVES_CACHE[key]
        entities_mod._ROSTER_DRIVES_CACHE[key] = (stale_seq - 1, drives)
        client.get("/api/gateway/entities")
        assert calls["n"] == 2, "a journal-seq change must invalidate the roster drives cache"


def test_cognition_wire_degrades_labeled_never_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """Version skew (fold returns None) and read failures both degrade to a
    labeled #FALLBACK warning with NO drives key — absent until the source
    exists, never derived, never a 500."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "drives-degrade-secret")
    from abstractgateway.app import app

    with TestClient(app, headers={"Authorization": "Bearer drives-degrade-secret"}) as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")}).status_code == 201

        from abstractgateway.entities import EntityHome

        monkeypatch.setattr(EntityHome, "cognition_drives", lambda self: None)
        body = client.get("/api/gateway/entities/Castor/cognition").json()
        assert "drives" not in body
        assert any("drive ratios unavailable" in w for w in body.get("warnings", []))

        def _boom(self):  # noqa: ANN001
            raise RuntimeError("store offline")

        monkeypatch.setattr(EntityHome, "cognition_drives", _boom)
        body = client.get("/api/gateway/entities/Castor/cognition").json()
        assert "drives" not in body
        assert any("drive ratios unreadable" in w for w in body.get("warnings", []))


def test_console_renders_the_drive_bars() -> None:
    """The console's Overview panel carries the drives block and the paint
    function with the ruled render commitments: both counts visible, the
    never-100% amber cue, no stale pixels on a failed read."""
    from abstractgateway.console import gateway_console_html

    html = gateway_console_html()
    assert 'id="entity-drives"' in html
    assert "_paintDrives" in html
    # Render commitments (my c-claim): counts beside the bar, the amber
    # saturation warning, and the honest empty state.
    assert "nothing open — no pull forward" in html
    assert "none yet" in html


def test_drive_pressure_serves_the_group_structure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Count-weighted groups (laurent 277; memory c296): the engine's
    drive_pressure()['groups'] clusters similar drives (a family of many
    similar questions surges above a lone one). The gateway serves it
    render-when-present — a passthrough over the engine fold, absent when
    the engine predates grouping (older engine = no key, no crash)."""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", "grp-secret")
    from abstractgateway.app import app

    import abstractmemory

    real_drive_pressure = getattr(abstractmemory, "drive_pressure", None)
    if real_drive_pressure is None:
        pytest.skip("engine predates drive_pressure")

    def _with_groups(store, journal, *, scopes):
        out = dict(real_drive_pressure(store, journal, scopes=scopes))
        # Force a group + an over-threshold count so drive_pressure serves.
        out["open_questions"] = 25
        out["groups"] = [
            {"family": "question", "members": ["ex:q1", "ex:q2", "ex:q3"], "size": 3,
             "exemplar": "ex:q1", "shared_terms": ["persistence"]},
        ]
        return out

    monkeypatch.setattr(abstractmemory, "drive_pressure", _with_groups)

    with TestClient(app, headers={"Authorization": "Bearer grp-secret"}) as client:
        assert client.post("/api/gateway/entities", json={"name": "Castor", "spark": _spark("Castor")}).status_code == 201
        cog = client.get("/api/gateway/entities/Castor/cognition").json()
        dp = cog.get("drive_pressure")
        assert dp is not None, "over-threshold pressure must serve the block"
        assert dp.get("groups"), "the group structure must pass through render-when-present"
        grp = dp["groups"][0]
        assert grp["family"] == "question" and grp["size"] == 3
        # The numeric counts are byte-unchanged (a group is a VIEW).
        assert dp["counts"]["open_questions"] == 25
