"""GW-C: the door binds per-entity runtimes (plan items 8/9, phase 3).

Runtime R2 ships the composition (`open_entity_runtime`: one Runtime per
entity over `runtime_<slug>.sqlite3` INSIDE the home, raw home handlers);
these tests pin the DOOR half: the registry caches ONE wrapped runtime per
slug, and the wrap makes every entity effect require a verifying summon
stamp — "the visit path joins the verified path" — with the same payload
gates as the shared router plus the one-runtime-one-home rule (a foreign
entity's stamp never crosses homes). Double-wrapping raises.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict

import pytest

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")
pytest.importorskip("abstractruntime.identity.entity_runtime")

from abstractmemory import DEFAULT_SPARK_TEMPLATE  # noqa: E402
from abstractruntime.core.models import Effect, EffectType  # noqa: E402

from abstractgateway.entities import EntityRegistry  # noqa: E402
from abstractgateway.entity_gate import (  # noqa: E402
    CHANNEL_WORKPLACE,
    finalize_summon_stamp,
    mint_summon_stamp,
    wrap_entity_runtime_routing,
)


def _spark(name: str) -> dict:
    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


class _Run:
    def __init__(self, *, run_id: str, session_id: str, vars: Dict[str, Any], parent_run_id: str = ""):
        self.run_id = run_id
        self.session_id = session_id
        self.parent_run_id = parent_run_id
        self.actor_id = "gateway"
        self.vars = vars


@pytest.fixture()
def rig(tmp_path: Path):
    registry = EntityRegistry(data_dir=tmp_path / "runtime", embedder_factory=lambda: None)
    created = registry.create(name="Castor", spark=_spark("Castor"))

    er = registry.get_entity_runtime("Castor")

    session_id = "visit-castor-1"
    stamp = mint_summon_stamp(
        data_dir=registry.data_dir,
        entity_id=created.entity_id,
        channel=CHANNEL_WORKPLACE,
        session_id=session_id,
        participants=["person:laurent", created.entity_id],
    )
    stamp = finalize_summon_stamp(stamp, data_dir=registry.data_dir, run_id="visit-run-1")
    run = _Run(run_id="visit-run-1", session_id=session_id, vars={"_runtime": {"entity": stamp}})

    class Rig:
        pass

    r = Rig()
    r.registry = registry
    r.er = er
    r.run = run
    r.stamp = stamp
    r.entity_id = created.entity_id
    r.session_id = session_id
    yield r
    registry.close_all()


def _call(rig: Any, etype: EffectType, payload: Dict[str, Any], *, run: Any = None):
    handler = rig.er.runtime._handlers[etype]
    return handler(run or rig.run, Effect(type=etype, payload=payload), None)


def test_registry_caches_one_wrapped_runtime_per_slug(rig):
    er2 = rig.registry.get_entity_runtime("Castor")
    assert er2 is rig.er  # one life, one runtime, one cache slot
    # The store lives INSIDE the home, named by the directory (R2 contract).
    assert rig.er.store_path == rig.registry.entities_dir / "castor" / "runtime_castor.sqlite3"
    assert rig.er.store_path.exists()


def test_stamped_visit_run_reaches_the_home(rig):
    out = _call(
        rig, EffectType.MEMORY_RECALL, {"cue_text": "", "turn_id": "t1", "journal": False}
    )
    assert out.status == "completed", out.error
    assert any(h.get("admission") == "self" for h in out.result["handles"])

    formed = _call(
        rig,
        EffectType.MEMORY_FORM,
        {
            "turn_id": "t1",
            "records": [{"kind": "episode", "title": "first visit turn", "digest": "We spoke."}],
        },
    )
    assert formed.status == "completed", formed.error


def test_unstamped_run_is_refused_at_the_door(rig):
    bare = _Run(run_id="stray", session_id="s", vars={})
    out = _call(rig, EffectType.MEMORY_RECALL, {"cue_text": "x"}, run=bare)
    assert out.status == "failed"
    assert "entity door" in (out.error or "")


def test_foreign_entity_stamp_never_crosses_homes(rig):
    """One runtime = one home: a VALID stamp for another entity is refused
    on this runtime (stronger than the shared router's slug dispatch)."""
    other = rig.registry.create(name="Pollux", spark=_spark("Pollux"))
    stamp = mint_summon_stamp(
        data_dir=rig.registry.data_dir,
        entity_id=other.entity_id,
        channel=CHANNEL_WORKPLACE,
        session_id="visit-pollux-1",
        participants=["person:laurent", other.entity_id],
    )
    stamp = finalize_summon_stamp(stamp, data_dir=rig.registry.data_dir, run_id="pollux-run")
    foreign = _Run(run_id="pollux-run", session_id="visit-pollux-1", vars={"_runtime": {"entity": stamp}})

    out = _call(rig, EffectType.MEMORY_RECALL, {"cue_text": "x", "turn_id": "t9"}, run=foreign)
    assert out.status == "failed"
    assert "never crosses homes" in (out.error or "")


def test_double_wrap_raises(rig):
    with pytest.raises(RuntimeError, match="already door-wrapped"):
        wrap_entity_runtime_routing(rig.er, data_dir=rig.registry.data_dir)


def test_two_entities_two_isolated_runtimes(rig):
    rig.registry.create(name="Pollux", spark=_spark("Pollux"))
    er2 = rig.registry.get_entity_runtime("Pollux")
    assert er2 is not rig.er
    assert er2.store_path != rig.er.store_path
    assert er2.store_path.parent.name == "pollux"  # each store inside its own home
