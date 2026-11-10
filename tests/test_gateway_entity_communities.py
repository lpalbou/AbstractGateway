"""His topics on the gateway (COMMUNITIES-DESIGN 2026-11-10; operator: memory owns the graph operations).

Memory forms the communities while he sleeps (the runtime's night stage) and keeps them as `community` records; the
gateway serves memory's own read through the runtime facade, payload version 2 on the same URL, and computes nothing
on GET. These tests drive memory's REAL pass (`communities_pass`, no author: every topic forms pending, which is
memory's honest shape for a title nobody wrote yet) on a home whose records carry real vectors (a hash embedder)."""

from __future__ import annotations

import copy
import hashlib
import math
import sys
import warnings
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("numpy")

_TOKEN = "entity-communities-shared-secret"


class _HashEmbedder:
    """Bag of hashed words: texts sharing words are close, texts sharing none are ~orthogonal."""

    dim = 256

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        out: List[List[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for tok in str(text).lower().split():
                tok = tok.strip(".,;:!?()[]\"'")
                if len(tok) > 3:
                    vec[int.from_bytes(hashlib.sha256(tok.encode()).digest()[:4], "big") % self.dim] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


RECORDS = [  # (key, kind, title, digest)
    ("dam-1", "episode", "Checking the dam spillway", "We checked the spillway gauges of the dam together."),
    ("dam-2", "episode", "The dam inspection report", "The inspection report of the dam spillway came back clean."),
    ("dam-3", "lesson", "Gauges drift in winter", "Spillway gauges drift when the winter cold sets in at the dam."),
    ("gar-1", "episode", "Planting the garden sensors", "Planted moisture sensors along the garden beds."),
    ("gar-2", "episode", "Watering schedule", "The garden moisture sensors now drive the watering schedule."),
    ("gar-3", "episode", "Garden beds in spring", "The garden beds and their moisture sensors woke in spring."),
]


@pytest.fixture(autouse=True)
def _gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    from abstractgateway.entities import EntityRegistry

    monkeypatch.setenv("ABSTRACTGATEWAY_AUTH_TOKEN", _TOKEN)
    embedder = _HashEmbedder()
    monkeypatch.setattr(EntityRegistry, "_resolve_embedder", lambda self: embedder)


def _client() -> TestClient:
    from abstractgateway.app import app

    return TestClient(app, headers={"Authorization": f"Bearer {_TOKEN}"})


def _spark(name: str) -> dict:
    from abstractmemory import DEFAULT_SPARK_TEMPLATE

    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


def _registry():
    from abstractgateway.service import get_gateway_service

    return get_gateway_service().entity_registry


def _ladder(eid: str) -> List[tuple]:
    return [("self", eid), ("diary", eid), ("life", eid)]


def _form(home: Any, key: str, kind: str, title: str, digest: str) -> str:
    from abstractmemory import MemoryRecordInput

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        [gid] = home.memory.remember_many([MemoryRecordInput(kind=kind, title=title, digest=digest)],
                                          scope="life", owner_id=home.entity_id, idempotency_key=key, turn_id=key)
    return gid


def _born(client: TestClient, name: str = "Castor") -> Any:
    assert client.post("/api/gateway/entities", json={"name": name, "spark": _spark(name)}).status_code == 201
    home = _registry().get_home(name.lower())
    ids = {key: _form(home, key, kind, title, digest) for key, kind, title, digest in RECORDS}
    home.test_ids = ids
    return home


def _sleep(home: Any) -> Dict[str, Any]:
    """Memory's real pass, as the night runs it (no author here: every topic forms pending with its tasks)."""
    from abstractruntime.integrations.abstractmemory.memory_facade import communities_pass

    receipt = communities_pass(home.memory, scopes=_ladder(home.entity_id), owner_id=home.entity_id, author=None,
                               cancelled=lambda: False)
    assert receipt["version_seq"] is not None and receipt["communities"] >= 1, receipt
    return receipt


# --------------------------------------------------------------------------- GET /entities/{name}/communities


def test_a_home_with_no_version_yet_says_his_first_sleep_is_pending() -> None:
    """No version: memory's own empty payload, unchanged (version 2, first_sleep_pending, no communities); no
    stand-in partition (the records are there, nothing groups them on GET)."""
    with _client() as client:
        _born(client)
        r = client.get("/api/gateway/entities/Castor/communities")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["version"] == 2 and body["version_seq"] is None
        assert body == {"version": 2, "version_seq": None, "first_sleep_pending": True, "algorithm": None,
                        "communities": [], "links": []}
        for gone in ("label", "keywords", "node_count", "unclustered", "params"):
            assert gone not in body, gone


def test_the_route_serves_memory_s_payload_v2_unchanged_after_a_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """After memory's pass the route serves memory's read through the facade, unchanged (members [{id, via}],
    layout.xy, key, facts), with no sentence - and never the runtime's retired Louvain: with
    abstractruntime.identity.communities unimportable the route still answers version 2."""
    from abstractruntime.integrations.abstractmemory.memory_facade import communities

    monkeypatch.setitem(sys.modules, "abstractruntime.identity.communities", None)  # import -> ImportError
    with _client() as client:
        home = _born(client)
        receipt = _sleep(home)
        r = client.get("/api/gateway/entities/Castor/communities")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body == communities(home.memory, scopes=_ladder(home.entity_id), owner_id=home.entity_id)
        assert body["version"] == 2 and body["version_seq"] == receipt["version_seq"]
        assert body["first_sleep_pending"] is False
        assert set(body) == {"version", "version_seq", "first_sleep_pending", "algorithm", "communities", "links"}
        assert body["communities"], body
        members = set()
        for c in body["communities"]:
            assert c["title"] or c["pending"], c  # authored, or honestly pending
            assert "label" not in c and "keywords" not in c and "member_ids" not in c, c
            assert c["key"] and c["id"] and c["members"] and all(set(m) == {"id", "via"} for m in c["members"]), c
            assert {"xy", "radius"} <= set(c["layout"]) and len(c["layout"]["xy"]) == 2, c
            assert isinstance(c["facts"], dict), c
            members.update(m["id"] for m in c["members"])
        assert set(home.test_ids.values()) <= members, (home.test_ids, members)


def test_the_payload_is_reread_when_the_journal_moves_and_folds_a_new_placement() -> None:
    """Cached per home + journal seq: a second GET with nothing new is the cached answer; a memory formed after the
    version (memory places it at formation, a `placed` event) moves the seq and the next GET lists it provisional."""
    with _client() as client:
        home = _born(client)
        _sleep(home)
        first = client.get("/api/gateway/entities/Castor/communities").json()
        assert client.get("/api/gateway/entities/Castor/communities").json() == first
        late = _form(home, "dam-4", "episode", "The dam spillway in the flood",
                     "The flood tested the dam spillway and its gauges held.")
        again = client.get("/api/gateway/entities/Castor/communities").json()
        provisional = {p for c in again["communities"] for p in c["provisional_ids"]}
        assert late in provisional, again
        assert all(late not in c["provisional_ids"] for c in first["communities"])


def test_an_unknown_entity_is_a_404() -> None:
    with _client() as client:
        assert client.get("/api/gateway/entities/Nobody/communities").status_code == 404
