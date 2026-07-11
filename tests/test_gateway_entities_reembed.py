"""The reembed repair verb (plan item 3 / M1b — gateway wrap).

Memory owns the pass (atomic swap, journal claim, count guard); these tests
pin the DOOR's wrap: the verb runs under the MAINTENANCE lease (a held home
refuses loudly, naming the holder), one embedder source (the door's resolved
embedder — a mismatching explicit target refuses before any engine call),
the host marker lands beside the engine's journal claim, and the HTTP
surface speaks 409/400 in the right places.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractmemory.reembed")
pytest.importorskip("abstractruntime.storage.lease")
pytest.importorskip("yaml")

from abstractmemory import DEFAULT_SPARK_TEMPLATE  # noqa: E402

from abstractgateway.entities import EntityRegistry  # noqa: E402


def _spark(name: str = "Castor") -> dict:
    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


class _NamedEmbedder:
    def __init__(self, model: str, dimension: int) -> None:
        self.model = model
        self._dimension = int(dimension)

    def embed_texts(self, texts):
        return [[0.25] * self._dimension for _ in texts]


def _registry(tmp_path: Path, embedder) -> EntityRegistry:
    return EntityRegistry(data_dir=tmp_path / "runtime", embedder_factory=lambda: embedder)


def test_reembed_swaps_pin_and_marks_both_planes(tmp_path: Path):
    registry = _registry(tmp_path, _NamedEmbedder("route-model-x", 6))
    registry.create(name="Castor", spark=_spark())

    result = registry.reembed(name="Castor", reason="test migration")
    assert result["rows"] > 0
    assert result["vectored"] > 0
    assert result["new_pin"]["model_id"] == "route-model-x"
    assert result["new_pin"]["source"] == "reembed"
    assert result["marker_record_id"]  # the engine's journal claim
    assert isinstance(result["marker_seq"], (int, float))  # the door's host marker

    # The host marker is on the replay record with the pin transition
    # (envelope shape: family="host", payload.kind + payload.details).
    stream_path = registry.entities_dir / ".host_stream" / "castor.jsonl"
    markers = [json.loads(line) for line in stream_path.read_text(encoding="utf-8").splitlines()]
    reembeds = [m for m in markers if (m.get("payload") or {}).get("kind") == "reembed"]
    assert len(reembeds) == 1
    assert reembeds[0].get("family") == "host"
    payload = reembeds[0]["payload"] or {}  # details flatten into the payload
    assert payload.get("new_pin", {}).get("model_id") == "route-model-x"
    assert payload.get("new_pin", {}).get("source") == "reembed"
    assert payload.get("journal_marker_record_id")  # both planes carry the act


def test_reembed_refuses_while_home_has_a_writer(tmp_path: Path):
    from abstractruntime.storage.lease import DirectoryLeaseHeld, acquire_directory_lease

    registry = _registry(tmp_path, _NamedEmbedder("route-model-x", 6))
    registry.create(name="Castor", spark=_spark())
    home_dir = registry.entities_dir / "castor"

    incumbent = acquire_directory_lease(home_dir, holder="visit-host")
    try:
        with pytest.raises(DirectoryLeaseHeld, match="already has a writer"):
            registry.reembed(name="Castor")
    finally:
        incumbent.release()

    # The moment the writer leaves, the same verb succeeds.
    assert registry.reembed(name="Castor")["vectored"] > 0


def test_reembed_target_must_match_resolved_embedder(tmp_path: Path):
    registry = _registry(tmp_path, _NamedEmbedder("route-model-x", 6))
    registry.create(name="Castor", spark=_spark())
    with pytest.raises(ValueError, match="does not match the door's resolved embedder"):
        registry.reembed(name="Castor", embedding_model="some-other-model")
    # A matching explicit target is a no-op verification, not a refusal.
    assert registry.reembed(name="Castor", embedding_model="route-model-x")["vectored"] > 0


def test_reembed_refuses_without_an_embedder(tmp_path: Path):
    registry = _registry(tmp_path, None)
    registry.create(name="Castor", spark=_spark())
    with pytest.raises(ValueError, match="no embedder resolved"):
        registry.reembed(name="Castor")


def test_reembed_without_explicit_target_uses_route_model(tmp_path: Path):
    """The `chosen or resolved_id` path (adversary B's coverage gap): a
    reembed with NO explicit embedding_model under a flipped route pins the
    ROUTE's model — the door's one embedder source doing its job."""
    registry = _registry(tmp_path, _NamedEmbedder("model-a", 4))
    registry.create(name="Castor", spark=_spark())

    registry2 = EntityRegistry(
        data_dir=tmp_path / "runtime", embedder_factory=lambda: _NamedEmbedder("model-b", 6)
    )
    result = registry2.reembed(name="Castor", reason="route migration, target implied")
    assert result["new_pin"]["model_id"] == "model-b"
    assert result["new_pin"]["dimension"] == 6
    assert result["vectored"] > 0
    assert not result.get("warnings")  # the route NAMED its model — no anonymous fallback


def test_reembed_rebinds_the_shared_router_handlers(tmp_path: Path):
    """Adversary B's P0: install_entity_routing caches per-slug handlers
    bound to the cached home's ENGINE; reembed's eviction closes that
    engine. Without identity-checked rebinding, every entity effect on the
    host runtime fails on a closed connection until process restart —
    the repair verb converting one route flip into an outage."""
    from typing import Any, Dict

    from abstractruntime.core.models import Effect, EffectType

    from abstractgateway.entity_gate import (
        CHANNEL_WORKPLACE,
        finalize_summon_stamp,
        install_entity_routing,
        mint_summon_stamp,
    )

    registry = _registry(tmp_path, _NamedEmbedder("model-a", 4))
    created = registry.create(name="Castor", spark=_spark())

    class _Run:
        def __init__(self, *, run_id: str, session_id: str, vars: Dict[str, Any]):
            self.run_id = run_id
            self.session_id = session_id
            self.parent_run_id = ""
            self.actor_id = "gateway"
            self.vars = vars

    class _RunStore:
        def __init__(self) -> None:
            self.runs: Dict[str, Any] = {}

        def load(self, run_id: str):
            return self.runs.get(str(run_id))

    class _StubRuntime:
        def __init__(self) -> None:
            self._handlers: Dict[Any, Any] = {}

    run_store = _RunStore()
    runtime = _StubRuntime()
    install_entity_routing(runtime, registry=registry, run_store=run_store, artifact_store=None)

    stamp = mint_summon_stamp(
        data_dir=registry.data_dir,
        entity_id=created.entity_id,
        channel=CHANNEL_WORKPLACE,
        session_id="s-rebind",
        participants=["person:maintainer", created.entity_id],
    )
    stamp = finalize_summon_stamp(stamp, data_dir=registry.data_dir, run_id="run-1")
    run = _Run(run_id="run-1", session_id="s-rebind", vars={"_runtime": {"entity": stamp}})
    run_store.runs["run-1"] = run

    def _recall(turn: str):
        handler = runtime._handlers[EffectType.MEMORY_RECALL]
        return handler(
            run,
            Effect(type=EffectType.MEMORY_RECALL,
                   payload={"cue_text": "", "turn_id": turn, "journal": False}),
            None,
        )

    # Before: the router serves the cached home's handlers.
    out1 = _recall("t1")
    assert out1.status == "completed", out1.error

    # Force the router to CACHE by touching it, then repair: eviction closes
    # the cached engine those handlers bind.
    assert registry.reembed(name="Castor", reason="rebind test")["vectored"] > 0

    # After: the router must serve REBUILT handlers over the fresh home —
    # not the closed engine (pre-fix: sqlite ProgrammingError here).
    out2 = _recall("t2")
    assert out2.status == "completed", out2.error

    registry.close_all()


def test_reembed_repairs_across_a_route_flip(tmp_path: Path):
    """Walkthrough catch #2 (agency c424): the M1b ceremony's exact
    precondition is a route flipped to the TARGET model — the verb used to
    open the home with the route's embedder and trip the pin!=route refusal
    its own text tells the operator to repair with. The repair-posture open
    (memory c454: open WITHOUT embedder — always legal under M1) must let
    the pass run, swap the pin, and leave normal opens green on the new
    route. Dimension changes too (1024-class -> other), the index-swap case."""
    birth = _NamedEmbedder("model-a", 4)
    registry = _registry(tmp_path, birth)
    registry.create(name="Castor", spark=_spark())

    # The door restarts with the route flipped to the TARGET model: a fresh
    # registry whose resolved embedder mismatches the home's birth pin.
    target = _NamedEmbedder("model-b", 6)
    registry2 = EntityRegistry(data_dir=tmp_path / "runtime", embedder_factory=lambda: target)

    # Sanity of the precondition: a normal embedder-bound open REFUSES now
    # (this is exactly why the verb cannot open the home the normal way).
    with pytest.raises(Exception, match="mismatch|refus"):
        registry2.get_home("castor").memory.reconstruct  # open fires in get_home

    result = registry2.reembed(name="Castor", embedding_model="model-b", reason="route migration")
    assert result["new_pin"]["model_id"] == "model-b"
    assert result["new_pin"]["dimension"] == 6
    assert result["vectored"] > 0

    # After the swap: the same door serves the home normally on the new route
    # (the stale refused handle was evicted; this open binds the new pin).
    home = registry2.get_home("castor")
    assert home.memory is not None
    inspected = registry2.inspect("castor")
    assert inspected["manifest"]["entity_id"] == "entity:castor"
    assert inspected["embedding_pin"]["model_id"] == "model-b"
