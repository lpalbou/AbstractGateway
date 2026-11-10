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


# --------------------------------------------------------------------------- the gateway's sleep runs the runtime's night


class _Summaries:
    """The summaries capability (and the mind): one structured title + summary per call, numbered so sibling topics
    never collide; any other call answers plain text."""

    model_info = {"provider": "lmstudio", "model": "test-summaries"}

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, *args: Any, response_model: Any = None, **kwargs: Any) -> Any:
        self.calls += 1
        if response_model is not None:
            return response_model(title=f"Authored title {self.calls}", summary=f"What record {self.calls} holds.")

        class _Reply:
            content = "A quiet night."
            usage = {"total_tokens": 1}

        return _Reply()


def _install_minds(monkeypatch: pytest.MonkeyPatch) -> _Summaries:
    from abstractgateway import entity_chat

    llm = _Summaries()
    monkeypatch.setattr("abstractgateway.entity_chat.gateway_text_mind",
                        lambda: {"provider": "lmstudio", "model": "test-mind", "base_url": None, "reasoning": None})
    monkeypatch.setattr(entity_chat, "_default_llm_factory", lambda provider, **kw: llm)
    return llm


def _record(home: Any, gid: str) -> Dict[str, Any]:
    from abstractruntime.integrations.abstractmemory.memory_facade import resolve_digest_assertion

    a = resolve_digest_assertion(home.store, gid)
    return dict(a.attributes or {})


@pytest.mark.capability_models  # the real capability path, on this file's scripted client
def test_an_operator_sleep_runs_the_runtime_s_night_repair_then_topics(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator's sleep (POST .../state asleep + dream) runs the runtime's night (build_consolidator), not
    memory's sleep_pass alone: the metadata repair authors a record formed without a title, the topics stage forms
    a communities version (titled by the same author), its version joins tonight's maintenance detail, and the
    night's topics line is in the home's own-time log."""
    from abstractmemory import MemoryRecordInput

    _install_minds(monkeypatch)
    with _client() as client:
        home = _born(client)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            [untitled] = home.memory.remember_many(
                [MemoryRecordInput(kind="episode", title="", digest="The dam keeper told me about the spillway gauges.",
                                   pending=("title",))],
                scope="life", owner_id=home.entity_id, idempotency_key="untitled", turn_id="untitled")
        assert not str(_record(home, untitled).get("title") or "").startswith("Authored title")
        assert client.get("/api/gateway/entities/Castor/communities").json()["first_sleep_pending"] is True

        r = client.post("/api/gateway/entities/Castor/state", json={"state": "asleep", "dream": True})
        assert r.status_code == 200, r.text
        night = r.json()["dream"]
        assert night.get("communities", {}).get("version_seq") is not None, night
        assert night["authoring"]["authored"] >= 1, night["authoring"]

        served = client.get("/api/gateway/entities/Castor/communities").json()
        assert served["first_sleep_pending"] is False and served["version_seq"] == night["communities"]["version_seq"]
        assert any(str(c["title"] or "").startswith("Authored title") for c in served["communities"]), served
        assert str(_record(home, untitled).get("title") or "").startswith("Authored title"), _record(home, untitled)

        last = home.memory.tasks.last_maintenance(_ladder(home.entity_id), kind="night")
        assert last and last["detail"].get("communities_version") == served["version_seq"], last
        log = (home.home_dir / "own_time.log").read_text(encoding="utf-8")
        assert f"(topics: version {served['version_seq']} - " in log, log[-2000:]


# --------------------------------------------------------------------------- GET /entities/{name}/graph (one read)


def _graph(client: TestClient) -> Dict[str, Any]:
    r = client.get("/api/gateway/entities/Castor/graph")
    assert r.status_code == 200, r.text
    return r.json()


def test_the_graph_is_one_read_nodes_with_their_topic_and_seat_and_edges() -> None:
    """GET /graph serves the graph the Entity app draws in one request: every open formed record but the community
    records (their latest display block as the replay serves it), its community key, `via` and seat from memory's
    communities read, the formation edges between nodes, and that read itself (unchanged)."""
    from abstractruntime.integrations.abstractmemory.memory_facade import communities

    with _client() as client:
        home = _born(client)
        before = _graph(client)
        assert before["communities"]["first_sleep_pending"] is True and before["communities_version"] is None
        assert set(home.test_ids.values()) <= {n["id"] for n in before["nodes"]}
        assert all(n["community"] is None and n["seat"] is None for n in before["nodes"])

        _sleep(home)
        snap = _graph(client)
        served = communities(home.memory, scopes=_ladder(home.entity_id), owner_id=home.entity_id)
        assert snap["communities"] == served and snap["communities_version"] == served["version_seq"]
        assert snap["journal_seq"] == int(home.memory.current_seq())
        nodes = {n["id"]: n for n in snap["nodes"]}
        assert not [n for n in snap["nodes"] if n["kind"] == "community"], "a community record is never a node"
        for c in served["communities"]:
            for m in c["members"]:
                if m["id"] in nodes:
                    assert nodes[m["id"]]["community"] == c["key"] and nodes[m["id"]]["via"] == m["via"]
                    seat = (c["layout"].get("seats") or {}).get(m["id"])
                    assert nodes[m["id"]]["seat"] == (list(seat) if seat is not None else None)
        assert all(nodes[i]["community"] for i in home.test_ids.values()), "every memory is in a topic"
        assert all(e["a"] in nodes and e["b"] in nodes and e["relation"] for e in snap["edges"])
        for n in snap["nodes"]:
            assert n["display"]["graph_id"] == n["id"] and n["title"] == n["display"].get("title")


def test_the_graph_folds_only_the_tail_after_a_formation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cached per home at the journal position it covers: a request after a new memory formed folds the replay
    from there (never the whole life again), and the new memory is there, provisional in its topic."""
    from abstractgateway import entity_replay

    with _client() as client:
        home = _born(client)
        _sleep(home)
        first = _graph(client)
        seen: List[float] = []
        real = entity_replay.merged_replay

        def _counting(*args: Any, **kwargs: Any):
            seen.append(float(kwargs.get("since_seq") or 0.0))
            return real(*args, **kwargs)

        monkeypatch.setattr(entity_replay, "merged_replay", _counting)
        late = _form(home, "dam-4", "episode", "The dam spillway in the flood",
                     "The flood tested the dam spillway and its gauges held.")
        snap = _graph(client)
        assert seen == [float(first["journal_seq"])], seen
        (node,) = [n for n in snap["nodes"] if n["id"] == late]
        assert node["community"] and node["via"] == "provisional" and node["seat"] is None
        assert _graph(client)["journal_seq"] == snap["journal_seq"] and seen == [float(first["journal_seq"])]


def test_one_communities_changed_marker_per_version_and_no_member_edges_on_the_stream() -> None:
    """The stream (COMMUNITIES-DESIGN §3): a new version is ONE host marker {kind: communities_changed,
    version_seq} however many readers pass it; a community record's binding draws its revision chain only, never
    an edge per member."""
    import json as _json

    with _client() as client:
        home = _born(client)
        receipt = _sleep(home)
        for _ in range(2):  # two readers of the same version
            lines = [_json.loads(x) for x in client.get("/api/gateway/entities/Castor/replay").text.splitlines() if x.strip()]
        markers = [e for e in lines if e.get("family") == "host" and (e.get("payload") or {}).get("kind") == "communities_changed"]
        assert [m["payload"]["version_seq"] for m in markers] == [receipt["version_seq"]], markers
        from abstractgateway.entity_replay import read_host_markers

        logged = [m for m in read_host_markers(_registry().entities_dir, "castor", since_seq=-1.0)
                  if (m.get("payload") or {}).get("kind") == "communities_changed"]
        assert len(logged) == 1, logged
        community_bindings = [e for e in lines if e.get("family") == "binding"
                              and (e.get("display") or {}).get("kind") == "community"]
        assert community_bindings, "the version's records pass through the stream"
        for e in community_bindings:
            assert all(edge.get("relation") == "refines" for edge in (e["display"].get("edges") or [])), e["display"]
