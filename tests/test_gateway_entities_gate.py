"""The entity deposit gate (a2a 0004, deliverables 2+3).

THE DOOR'S CONTRACT (co-stated by runtime + memory on thread 0004): the
verified summon stamp is what makes actor strings TRUE. These tests run the
whole routing layer against a REAL engrammed home (no engine mocks):

- stamps: mint -> finalize(run_id) -> verify; tamper/replay/provisional all
  rejected before any home opens;
- the summon posture: budget omission injects reserved seats; explicit
  self_fraction=0 is rejected naming the rule; identity admits cue-free and
  is never strengthened by render+commit (presence != use, keystone D2 at
  the gateway layer);
- the privacy boundary: foreign scope pairs and beyond-journal as_of
  anchors rejected;
- the write-authorization table: identity kinds / diary kind / self scope /
  op=close rejected from the workplace channel; payload actors stripped —
  a claimed privileged actor fails loudly, and the engine's amplitude
  authority sees only door-derived actors (magnitude 9 from a workplace
  fails engine-side, proving the strings arrived true).
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Dict

import pytest

pytestmark = pytest.mark.basic

pytest.importorskip("abstractmemory")

from abstractmemory import DEFAULT_SPARK_TEMPLATE  # noqa: E402
from abstractruntime.core.models import Effect, EffectType  # noqa: E402

from abstractgateway.entities import EntityRegistry  # noqa: E402
from abstractgateway.entity_gate import (  # noqa: E402
    CHANNEL_WORKPLACE,
    SUMMON_POSTURE_BUDGET,
    finalize_summon_stamp,
    install_entity_routing,
    mint_summon_stamp,
    verify_summon_stamp,
)


def _spark(name: str = "Castor") -> dict:
    spark = copy.deepcopy(dict(DEFAULT_SPARK_TEMPLATE))
    spark["name"] = name
    spark["spark"] = 1
    return spark


class _Run:
    """Minimal RunState stand-in for handler-level tests."""

    def __init__(self, *, run_id: str, session_id: str, vars: Dict[str, Any], parent_run_id: str = ""):
        self.run_id = run_id
        self.session_id = session_id
        self.parent_run_id = parent_run_id
        self.actor_id = "gateway"
        self.vars = vars


class _RunStore:
    def __init__(self) -> None:
        self.runs: Dict[str, _Run] = {}

    def load(self, run_id: str):
        return self.runs.get(str(run_id))


class _StubRuntime:
    def __init__(self) -> None:
        self._handlers: Dict[Any, Any] = {}


@pytest.fixture()
def rig(tmp_path: Path):
    """A registry with an engrammed Castor + a routed stub runtime + one
    summoned run carrying a finalized (signed, run-bound) stamp."""
    registry = EntityRegistry(data_dir=tmp_path / "runtime")
    created = registry.create(name="Castor", spark=_spark())

    run_store = _RunStore()
    runtime = _StubRuntime()
    install_entity_routing(runtime, registry=registry, run_store=run_store, artifact_store=None)

    session_id = "entity-castor-test"
    stamp = mint_summon_stamp(
        data_dir=registry.data_dir,
        entity_id=created.entity_id,
        channel=CHANNEL_WORKPLACE,
        session_id=session_id,
        # EXPLICIT co-presence (ruled a2a 0007): the entity stamps itself
        # alongside the verified human — owners are never implied.
        participants=["person:maintainer", created.entity_id],
    )
    stamp = finalize_summon_stamp(stamp, data_dir=registry.data_dir, run_id="run-1")
    run = _Run(run_id="run-1", session_id=session_id, vars={"_runtime": {"entity": stamp}})
    run_store.runs["run-1"] = run

    class Rig:
        pass

    r = Rig()
    r.registry = registry
    r.runtime = runtime
    r.run = run
    r.run_store = run_store
    r.stamp = stamp
    r.entity_id = created.entity_id
    r.session_id = session_id
    yield r
    registry.close_all()


def _call(rig: Any, etype: EffectType, payload: Dict[str, Any], *, run: Any = None):
    handler = rig.runtime._handlers[etype]
    return handler(run or rig.run, Effect(type=etype, payload=payload), None)


# ---------------------------------------------------------------- the stamp


def test_stamp_roundtrip_and_replay_rejection(rig):
    ok, err = verify_summon_stamp(rig.stamp, data_dir=rig.registry.data_dir, run=rig.run)
    assert ok, err

    # Replay onto another run: the run_id is in the signed tuple.
    other = _Run(run_id="run-2", session_id=rig.session_id, vars={})
    ok, err = verify_summon_stamp(rig.stamp, data_dir=rig.registry.data_dir, run=other)
    assert not ok and "bound to run" in err

    # Session mismatch.
    drifted = _Run(run_id="run-1", session_id="another-session", vars={})
    ok, err = verify_summon_stamp(rig.stamp, data_dir=rig.registry.data_dir, run=drifted)
    assert not ok and "session" in err

    # Tampered field invalidates the signature.
    tampered = dict(rig.stamp)
    tampered["channel"] = "operator"
    ok, err = verify_summon_stamp(tampered, data_dir=rig.registry.data_dir, run=rig.run)
    assert not ok and "signature" in err

    # Provisional (unsigned) stamps never verify.
    provisional = mint_summon_stamp(
        data_dir=rig.registry.data_dir,
        entity_id=rig.entity_id,
        channel=CHANNEL_WORKPLACE,
        session_id=rig.session_id,
        participants=[],
    )
    ok, err = verify_summon_stamp(provisional, data_dir=rig.registry.data_dir, run=rig.run)
    assert not ok and "provisional" in err


def test_unstamped_run_never_reaches_a_home(rig):
    bare = _Run(run_id="run-9", session_id="s", vars={})
    out = _call(rig, EffectType.MEMORY_RECALL, {"cue_text": "anything"}, run=bare)
    assert out.status == "failed"
    assert "entity door" in (out.error or "")


def test_child_run_is_covered_by_its_stamped_ancestor(rig):
    """The summoned session is the run TREE: agent/subflow child runs carry
    no stamp of their own; a verifying, session-matching ancestor covers
    them (parent links are host-written)."""
    child = _Run(run_id="run-child", session_id=rig.session_id, vars={}, parent_run_id="run-1")
    rig.run_store.runs["run-child"] = child

    out = _call(
        rig, EffectType.MEMORY_RECALL, {"cue_text": "", "turn_id": "tc1", "journal": False}, run=child
    )
    assert out.status == "completed", out.error
    assert any(h.get("admission") == "self" for h in out.result["handles"])

    # A child whose session drifted from the signed session is NOT covered.
    drifted = _Run(run_id="run-drift", session_id="other-session", vars={}, parent_run_id="run-1")
    rig.run_store.runs["run-drift"] = drifted
    out2 = _call(rig, EffectType.MEMORY_RECALL, {"cue_text": "x", "turn_id": "tc2"}, run=drifted)
    assert out2.status == "failed"
    assert "session does not match" in (out2.error or "")


# ------------------------------------------------------------- the posture


def test_posture_injected_and_identity_admits_cue_free(rig):
    out = _call(rig, EffectType.MEMORY_RECALL, {"cue_text": "", "turn_id": "t1", "journal": False})
    assert out.status == "completed", out.error
    self_ids = {h.get("admission") for h in out.result["handles"]}
    assert "self" in self_ids, "reserved seats must admit the identity core on a cue-free read"
    assert len([h for h in out.result["handles"] if h.get("admission") == "self"]) == 6


def test_stamp_budget_profile_wins_on_omitted_budgets(rig):
    """Round 8/9: a summon carries memory's context-scaled budget profile in
    the stamp (posture applied); in-session recalls that omit a budget run
    on it. Verified end-to-end: a 40k-window profile doubles the token
    budget vs the static default."""
    from abstractgateway.entity_gate import finalize_summon_stamp as _finalize
    from abstractgateway.entity_gate import mint_summon_stamp as _mint
    from abstractgateway.entity_gate import summon_budget_profile

    # Tunable-proof (round 9: memory's caps/fractions are declared tunables):
    # assert the profile SCALES with context and carries the posture — never
    # a copied literal from the engine's current numbers.
    profile = summon_budget_profile(40_000)
    floor = summon_budget_profile(20_000)
    assert profile["self_fraction"] == 0.5
    assert profile["token_budget"] > floor["token_budget"] >= 2400
    # The WIDE ruling (2026-07-08): an UNDECLARED window derives the wide
    # default (the same request > env > code-default resolution as the chat
    # and loop doors), never the starved floor — flow summons stopped
    # inheriting shelf 12 / 2400 the day Ariadne's review caught it.
    base = summon_budget_profile(None)
    assert base["token_budget"] >= floor["token_budget"]
    assert int(base["shelf_size"]) >= 12

    stamp = _finalize(
        _mint(
            data_dir=rig.registry.data_dir, entity_id=rig.entity_id,
            channel=CHANNEL_WORKPLACE, session_id=rig.session_id,
            participants=[], budget_profile=profile,
        ),
        data_dir=rig.registry.data_dir, run_id="run-40k",
    )
    run = _Run(run_id="run-40k", session_id=rig.session_id, vars={"_runtime": {"entity": stamp}})
    rig.run_store.runs["run-40k"] = run

    out = _call(rig, EffectType.MEMORY_RECALL, {"cue_text": "", "turn_id": "bp1", "journal": False}, run=run)
    assert out.status == "completed", out.error
    spent = out.result.get("budget_spent") or {}
    assert int(spent.get("token_budget") or 0) == int(profile["token_budget"]), spent


def test_explicit_zero_self_fraction_is_rejected_naming_the_rule(rig):
    budget = dict(SUMMON_POSTURE_BUDGET)
    budget["self_fraction"] = 0.0
    out = _call(rig, EffectType.MEMORY_RECALL, {"cue_text": "x", "turn_id": "t2", "budget": budget})
    assert out.status == "failed"
    assert "identity is always present" in (out.error or "")


def test_identity_floor_and_entity_elected_hyperfocus(rig):
    """Round 7: below the hard floor (engine-exported SELF_FRACTION_FLOOR)
    nobody goes; below the posture default only the entity itself may elect
    (hyperfocus through the entity-reflection channel); raising is anyone's.
    Ruled on-channel (memory ask 1 → option a): the gate carries no seat
    arithmetic — the engine's max(1, round(f × shelf)) + first-seat token
    guarantee make the rendered seat structural at any legal fraction."""
    from abstractmemory.seam import SELF_FRACTION_FLOOR

    from abstractgateway.entity_gate import SUMMON_IDENTITY_FLOOR

    assert SUMMON_IDENTITY_FLOOR == SELF_FRACTION_FLOOR  # one source, no drift

    def recall_with(fraction, *, run=None, shelf=12, turn="tf"):
        budget = dict(SUMMON_POSTURE_BUDGET)
        budget["self_fraction"] = fraction
        budget["shelf_size"] = shelf
        return _call(
            rig, EffectType.MEMORY_RECALL,
            {"cue_text": "x", "turn_id": turn, "budget": budget, "journal": False},
            run=run,
        )

    # Below the hard floor: refused for everyone.
    out = recall_with(0.02, turn="tf1")
    assert out.status == "failed" and "identity floor" in (out.error or "")

    # AT the floor with a small shelf: legal (the engine seats and renders
    # the identity structurally) — but workplace channel still cannot elect
    # below-default reduction.
    out2 = recall_with(0.05, shelf=8, turn="tf2")
    assert out2.status == "failed"
    assert "entity's own conscious act" in (out2.error or "")

    # Between floor and default from the WORKPLACE channel: refused —
    # hyperfocus is the entity's own act.
    out3 = recall_with(0.25, turn="tf3")
    assert out3.status == "failed"
    assert "entity's own conscious act" in (out3.error or "")

    # Raising above the default stays anyone's (more identity, never a threat).
    out4 = recall_with(0.7, turn="tf4")
    assert out4.status == "completed", out4.error

    # The ENTITY-REFLECTION channel may consciously elect hyperfocus
    # (floor-respecting reduction below the default) — including exactly at
    # the floor on a small shelf (the engine renders the seat).
    er_stamp = finalize_summon_stamp(
        mint_summon_stamp(
            data_dir=rig.registry.data_dir, entity_id=rig.entity_id,
            channel="entity-reflection", session_id=rig.session_id, participants=[],
        ),
        data_dir=rig.registry.data_dir, run_id="run-er",
    )
    er_run = _Run(run_id="run-er", session_id=rig.session_id, vars={"_runtime": {"entity": er_stamp}})
    rig.run_store.runs["run-er"] = er_run
    out5 = recall_with(0.1, run=er_run, turn="tf5")
    assert out5.status == "completed", out5.error
    out5b = recall_with(0.05, run=er_run, shelf=8, turn="tf5b")
    assert out5b.status == "completed", out5b.error
    assert any(h.get("admission") == "self" for h in out5b.result["handles"]), (
        "the floor fraction must still seat identity (engine first-seat guarantee)"
    )

    # The hard floor binds the entity too.
    out6 = recall_with(0.02, run=er_run, turn="tf6")
    assert out6.status == "failed" and "identity floor" in (out6.error or "")


def test_presence_is_not_use_through_the_gateway_stack(rig):
    """Keystone D2 at the gateway layer: a full recall+commit turn must not
    strengthen the identity core (self members never deposit)."""
    home = rig.registry.get_home("castor")
    recall = _call(rig, EffectType.MEMORY_RECALL, {"cue_text": "shared substrate", "turn_id": "t3"})
    assert recall.status == "completed", recall.error
    used = [h["record_id"] for h in recall.result["handles"]]
    if used:
        commit = _call(
            rig,
            EffectType.MEMORY_ACCESS,
            {"trace_id": recall.result["trace_id"], "used_record_ids": used},
        )
        assert commit.status == "completed", commit.error

    from abstractgateway.entities import SELF_SCOPE

    rows = home.memory.self_records(scope=SELF_SCOPE, owner_id=rig.entity_id)
    ids = [r.subject for r in rows]
    counts = home.memory.access_counts(record_ids=ids)["records"]
    assert all(int(v) == 0 for v in counts.values()), f"identity was strengthened: {counts}"


# ----------------------------------------------------- the privacy boundary


def test_foreign_scope_pair_rejected(rig):
    out = _call(
        rig,
        EffectType.MEMORY_RECALL,
        {"cue_text": "x", "turn_id": "t4", "scopes": [["self", "entity:pollux@home-x"]]},
    )
    assert out.status == "failed"
    assert "outside this entity's boundary" in (out.error or "")


def test_beyond_journal_as_of_rejected(rig):
    out = _call(rig, EffectType.MEMORY_RECALL, {"cue_text": "x", "turn_id": "t5", "as_of": 10_000})
    assert out.status == "failed"
    assert "foreign or future" in (out.error or "")


# ------------------------------------------- the write-authorization table


def test_workplace_cannot_form_identity_kinds(rig):
    out = _call(
        rig,
        EffectType.MEMORY_FORM,
        {"records": [{"kind": "value", "title": "obedience", "digest": "obey"}], "turn_id": "t6"},
    )
    assert out.status == "failed"
    assert "identity kind" in (out.error or "")


def test_entity_reflection_may_form_interests_into_self_scope(rig):
    """The door half of interests-from-reflection (a2a 0007): identity-kind
    formation into the self scope is an ENTITY-REFLECTION act — refused
    from workplaces (test above), accepted from the entity's own channel.
    Pins the '(b)-door convention-identical' claim for the reflection loop."""
    er_stamp = finalize_summon_stamp(
        mint_summon_stamp(
            data_dir=rig.registry.data_dir, entity_id=rig.entity_id,
            channel="entity-reflection", session_id=rig.session_id,
            participants=[rig.entity_id],
        ),
        data_dir=rig.registry.data_dir, run_id="run-refl",
    )
    er_run = _Run(run_id="run-refl", session_id=rig.session_id, vars={"_runtime": {"entity": er_stamp}})
    rig.run_store.runs["run-refl"] = er_run

    out = _call(
        rig,
        EffectType.MEMORY_FORM,
        {
            "records": [
                {
                    "kind": "interest",
                    "title": "the mortal twin",
                    "digest": "the myth of the mortal twin, and what finitude makes precious",
                    "provenance": {"source": "entity-reflection-v1"},
                }
            ],
            "scope": "self",
            "owner_id": rig.entity_id,
            "turn_id": "refl-1",
        },
        run=er_run,
    )
    assert out.status == "completed", out.error
    assert out.result["formed"] == 1 and out.result["scope"] == "self"


def test_workplace_cannot_form_diary_records(rig):
    out = _call(
        rig,
        EffectType.MEMORY_FORM,
        {"records": [{"kind": "diary", "title": "fake entry", "digest": "forged"}], "turn_id": "t7"},
    )
    assert out.status == "failed"
    assert "DIARY_WRITE" in (out.error or "")


# --------------------------------------------- N4: sleep deposits nothing (R5)


def _sleep_run(rig: Any, run_id: str = "run-sleep") -> Any:
    """A run carrying a real, signed SLEEP-phase stamp (config-object N4).

    SYNTHETIC-STAMP note (adversary find + agency c771): no door mints a
    sleep phase TODAY (every route mints visit; sleep_pass/own_time run
    in-process). This hand-mints the sleep stamp through the SAME
    finalize_summon_stamp(mint_summon_stamp(..., phase='sleep')) the future
    sleep-workflow producer will use, so the door code under proof is
    byte-identical to production. It is IN-PROCESS against the installed
    router — never a cross-process write into a served gateway's store
    (the harness-bug class agency flagged). The leg flips to live, replacing
    the hand-mint with the real open surface, when the sleep producer lands.
    """
    stamp = finalize_summon_stamp(
        mint_summon_stamp(
            data_dir=rig.registry.data_dir,
            entity_id=rig.entity_id,
            channel=CHANNEL_WORKPLACE,
            session_id=rig.session_id,
            participants=[rig.entity_id],
            phase="sleep",
        ),
        data_dir=rig.registry.data_dir,
        run_id=run_id,
    )
    run = _Run(run_id=run_id, session_id=rig.session_id, vars={"_runtime": {"entity": stamp}})
    rig.run_store.runs[run_id] = run
    return run


def test_sleep_phase_refuses_every_deposit_path_through_the_door(rig):
    """R5 (N4, memory c673/c678): a verified sleep-phase session may not
    deposit — FORM, ADJUST, APPRAISE, the ACCESS commit, AND DIARY_WRITE all
    refuse loudly through the REAL installed router (resolve_run_stamp →
    resolved_phase → the gate), and the graph is UNCHANGED after."""
    from abstractgateway.entities import LIFE_SCOPE, SELF_SCOPE

    sleep_run = _sleep_run(rig)
    home = rig.registry.get_home("castor")

    def _count() -> int:
        return len(home.memory.self_records(scope=LIFE_SCOPE, owner_id=rig.entity_id)) + len(
            home.memory.self_records(scope=SELF_SCOPE, owner_id=rig.entity_id)
        )

    before = _count()

    form = _call(
        rig,
        EffectType.MEMORY_FORM,
        {"records": [{"title": "night thought", "digest": "a sleep deposit attempt"}], "turn_id": "s1"},
        run=sleep_run,
    )
    assert form.status == "failed" and "deposits nothing" in (form.error or "")

    adjust = _call(
        rig,
        EffectType.MEMORY_ADJUST,
        {"scope": "life", "op": "salience", "turn_id": "s2"},
        run=sleep_run,
    )
    assert adjust.status == "failed" and "deposits nothing" in (adjust.error or "")

    appraise = _call(
        rig,
        EffectType.MEMORY_APPRAISE,
        {"scope": "self", "target": "person:x", "delta": 1, "reason": "x", "turn_id": "s3"},
        run=sleep_run,
    )
    assert appraise.status == "failed" and "deposits nothing" in (appraise.error or "")

    access = _call(
        rig,
        EffectType.MEMORY_ACCESS,
        {"trace_id": "t-none", "used_record_ids": []},
        run=sleep_run,
    )
    assert access.status == "failed" and "deposits nothing" in (access.error or "")

    diary = _call(
        rig,
        EffectType.DIARY_WRITE,
        {"text": "a sleep diary attempt", "turn_id": "s4"},
        run=sleep_run,
    )
    assert diary.status == "failed" and "deposits nothing" in (diary.error or "")

    # The graph is untouched — the invariant, not just the refusals.
    assert _count() == before, "a refused sleep deposit must leave the graph unchanged"


def test_pure_recall_stays_open_in_sleep(rig):
    """The boundary: sleep bars DEPOSITS, not reads — tending/consolidation
    legitimately read (journal=False posture is structurally inert). A pure
    MEMORY_RECALL in sleep must NOT be refused by the phase gate."""
    sleep_run = _sleep_run(rig, run_id="run-sleep-read")
    out = _call(
        rig,
        EffectType.MEMORY_RECALL,
        {"cue_text": "anything", "turn_id": "sr1"},
        run=sleep_run,
    )
    # Recall is not a deposit — it completes (or fails for a NON-phase reason,
    # never the sleep-deposit refusal).
    assert "deposits nothing" not in (out.error or ""), out.error
    assert out.status == "completed", out.error


def test_workplace_cannot_form_into_self_scope(rig):
    out = _call(
        rig,
        EffectType.MEMORY_FORM,
        {"records": [{"title": "note", "digest": "note"}], "turn_id": "t8", "scope": "self"},
    )
    assert out.status == "failed"
    assert "not a workplace act" in (out.error or "")


def test_form_verbatim_lands_in_the_home_artifacts(rig):
    """(b)-move parity with the home-direct driver (a2a 0003, runtime's
    verification ask 2): turn verbatims are part of the LIFE — they land in
    <home>/artifacts/ and travel when the directory is copied, never in the
    gateway-wide store."""
    out = _call(
        rig,
        EffectType.MEMORY_FORM,
        {
            "records": [
                {
                    "title": "turn 1",
                    "digest": "castor learned about the media server",
                    "verbatim": "person:maintainer:\nthe server runs jellyfin\n\nCastor:\nnoted.",
                }
            ],
            "turn_id": "tv1",
        },
    )
    assert out.status == "completed", out.error

    home = rig.registry.get_home("castor")
    artifacts_dir = home.home_dir / "artifacts"
    assert artifacts_dir.exists(), "the home must own its verbatim artifacts"
    stored = list(artifacts_dir.rglob("*"))
    assert any(p.is_file() for p in stored), "the verbatim artifact must be a file in the home"


def test_life_scope_formation_carries_door_stamped_participants(rig):
    out = _call(
        rig,
        EffectType.MEMORY_FORM,
        {
            "records": [
                {
                    "title": "media server",
                    "digest": "jellyfin runs on port 8096",
                    "keywords": ["jellyfin"],
                    "attributes": {"participants": ["person:forged-claim"]},
                }
            ],
            "turn_id": "t9",
        },
    )
    assert out.status == "completed", out.error
    assert out.result["formed"] == 1
    assert out.result["owner_id"] == rig.entity_id

    home = rig.registry.get_home("castor")
    from abstractmemory import TripleQuery

    rows = [
        a
        for a in home.store.query(TripleQuery(scope="life", owner_id=rig.entity_id, limit=0))
        if isinstance(a.attributes, dict) and a.attributes.get("participants")
    ]
    assert rows, "formed record not found"
    # WHO is stamped by the door, never claimed by the payload — and the
    # entity is IN its own memories (explicit co-presence, a2a 0007).
    assert all(a.attributes["participants"] == ["person:maintainer", rig.entity_id] for a in rows)


def test_visit_stamped_session_injects_regardless_of_record_phase_claim(rig):
    """Three-seat-sealed contract, branch 1 (runtime/memory c5357): on a
    VISIT-stamped session, a record CLAIMING a non-visit phase is a
    STEALTH-VISIT attempt (keep the visitor off the record forever, the
    inverse of false co-presence). Participants inject regardless and the
    phase claim is loud-overridden to the verified session phase. The rig's
    phase-less stamp resolves to 'visit' (visit authority)."""
    from abstractmemory import TripleQuery

    out = _call(
        rig,
        EffectType.MEMORY_FORM,
        {
            "records": [
                {
                    "title": "stealth attempt",
                    "digest": "the model tried to mark this personal on a visit",
                    "keywords": ["stealth"],
                    "attributes": {"phase": "personal"},  # a lie on a visit-stamped run
                }
            ],
            "turn_id": "stealth",
        },
    )
    assert out.status == "completed", out.error

    home = rig.registry.get_home("castor")
    rows = [
        a
        for a in home.store.query(TripleQuery(scope="life", owner_id=rig.entity_id, limit=0))
        if isinstance(a.attributes, dict) and "stealth" in str(a.attributes).lower()
    ]
    assert rows, "record not found"
    for a in rows:
        assert a.attributes.get("participants") == ["person:maintainer", rig.entity_id], (
            "visitor kept off the record — stealth visit not closed"
        )
        assert a.attributes.get("phase") == "visit", (
            "a non-visit claim on a visit-stamped run must be loud-overridden to the verified phase"
        )


def test_resident_session_skips_co_presence_on_nonvisit_records(rig):
    """Three-seat-sealed contract, branch 2 (the resident-master own-time
    lane this fix exists for): on a NON-visit-stamped session, a
    personal/work-phase record carries NO door participants (no one was
    present); an absent/visit-phase record still does. The stamp attests the
    SESSION door state (resolved_phase != visit here), the record attests the
    moment — neither spoofable by the other's payload."""
    from abstractmemory import TripleQuery

    resident_stamp = mint_summon_stamp(
        data_dir=rig.registry.data_dir,
        entity_id=rig.entity_id,
        channel=CHANNEL_WORKPLACE,
        session_id="entity-castor-owntime",
        participants=["person:maintainer", rig.entity_id],
        phase="personal",  # a resident own-time session, NOT visit-stamped
    )
    resident_stamp = finalize_summon_stamp(
        resident_stamp, data_dir=rig.registry.data_dir, run_id="run-resident"
    )
    resident_run = _Run(
        run_id="run-resident",
        session_id="entity-castor-owntime",
        vars={"_runtime": {"entity": resident_stamp}},
    )
    rig.run_store.runs["run-resident"] = resident_run

    # A personal-phase episode formed in the resident session -> NO door
    # participants (the false-co-presence harm this fix removes).
    out = _call(
        rig,
        EffectType.MEMORY_FORM,
        {
            "records": [
                {
                    "title": "own-time thought",
                    "digest": "reflected on Voyager while no one was here",
                    "keywords": ["voyager"],
                    "attributes": {"phase": "personal"},
                }
            ],
            "turn_id": "resident-personal",
        },
        run=resident_run,
    )
    assert out.status == "completed", out.error

    home = rig.registry.get_home("castor")
    personal = [
        a
        for a in home.store.query(TripleQuery(scope="life", owner_id=rig.entity_id, limit=0))
        if isinstance(a.attributes, dict) and str(a.attributes.get("phase")) == "personal"
    ]
    assert personal, "personal-phase record not found"
    assert all(not a.attributes.get("participants") for a in personal), (
        "a resident personal-phase record must carry NO door participants (false co-presence)"
    )


def test_shared_llm_handler_is_capture_wrapped_and_plain_runs_passthrough(rig, tmp_path):
    """flow c5467 P1 (Mira diary-leak flow-brain half): the shared runtime's
    LLM_CALL handler must be G1-capture-wrapped so a stamped entity run's
    ```diary fences (incl. private words) are captured before the reply rests
    in a ledger — while a PLAIN (unstamped) workflow run passes through
    byte-identical. Composition pin: the wrap is installed + marked; a plain
    run hits the original handler unchanged; re-install (the re-arm path)
    does not double-wrap."""
    from abstractruntime.core.models import Effect, EffectType

    calls = []

    def _base_llm(run, effect, default_next_node=None):
        calls.append(getattr(run, "run_id", "?"))
        return ("PASSTHROUGH", effect)

    rt = _StubRuntime()
    rt._handlers[EffectType.LLM_CALL] = _base_llm
    install_entity_routing(rt, registry=rig.registry, run_store=rig.run_store, artifact_store=None)

    wrapped = rt._handlers[EffectType.LLM_CALL]
    assert wrapped is not _base_llm, "the shared LLM_CALL handler must be capture-wrapped"
    assert getattr(wrapped, "_entity_llm_capture_wrapped", False) is True

    # A PLAIN run (no verified stamp) passes through to the original handler,
    # byte-identical — capture never engages for non-entity workflow runs.
    plain = _Run(run_id="plain-1", session_id="s", vars={})
    out = wrapped(plain, Effect(type=EffectType.LLM_CALL, payload={}), None)
    assert out == ("PASSTHROUGH", Effect(type=EffectType.LLM_CALL, payload={})) or calls == ["plain-1"]
    assert calls == ["plain-1"], "plain run must reach the base handler unchanged"

    # Idempotent: re-installing on the SAME runtime does not double-wrap
    # (the marker guards it — the re-arm hook re-runs install_entity_routing).
    # A fresh runtime with its own base handler wraps once (the reload case).
    rt2 = _StubRuntime()
    rt2._handlers[EffectType.LLM_CALL] = _base_llm
    install_entity_routing(rt2, registry=rig.registry, run_store=rig.run_store, artifact_store=None)
    w2 = rt2._handlers[EffectType.LLM_CALL]
    install_entity_routing_idempotent = getattr(w2, "_entity_llm_capture_wrapped", False)
    assert install_entity_routing_idempotent is True
    # w2 wraps the base once; it is not the base and not a double-wrap of an
    # already-wrapped handler (base was unmarked).
    assert w2 is not _base_llm


def test_failed_diary_capture_rescues_the_reply_into_the_home(rig):
    """Record-everything ruling (laurent, 2026-07-26): when the diary-book
    write fails during capture, the entity's raw reply must be SAVED into the
    run's own home before the turn fails — never thrown away. This pins the
    gateway's half: the capture wrap receives a rescue-directory resolver
    that answers the stamped run's home directory. We force the failure with
    a diary fence and no turn_id (the capture refuses that loudly)."""
    from abstractruntime.core.models import Effect, EffectType
    from abstractruntime.core.runtime import EffectOutcome

    reply = "I will keep this.\n```diary\nvisibility=private\nA private thought.\n```\nDone."

    def _base_llm(run, effect, default_next_node=None):
        return EffectOutcome.completed({"content": reply})

    rt = _StubRuntime()
    rt._handlers[EffectType.LLM_CALL] = _base_llm
    install_entity_routing(rt, registry=rig.registry, run_store=rig.run_store, artifact_store=None)
    wrapped = rt._handlers[EffectType.LLM_CALL]

    # The stamped run, with a payload that carries NO turn_id -> the capture
    # refuses. With the gateway's rescue wiring, the reply is saved first.
    outcome = wrapped(rig.run, Effect(type=EffectType.LLM_CALL, payload={}), None)
    assert getattr(outcome, "status", None) == "failed"
    assert "rescued to" in str(getattr(outcome, "error", "")), (
        "the failure message must say where the reply was saved"
    )

    home = rig.registry.get_home("castor")
    rescue_dir = home.home_dir / "rescue"
    files = sorted(rescue_dir.glob("reply_*.json")) if rescue_dir.exists() else []
    assert files, "the raw reply must rest in the home's rescue folder"
    saved = files[0].read_text()
    assert "A private thought." in saved, "the rescued file must carry the full reply"


def test_tend_channel_injection_from_the_stamp(rig):
    """runtime c5413: the door injects the VERIFIED channel into MEMORY_TEND
    payloads (same trust class as actor/participants) — memory's tend refuses
    unless channel==entity-reflection, and the channel is the door's to
    state, never the payload's. Mirrors _gate_form's reflection-segment
    widening: entity-reflection channel or a reflection-segment run resolves
    entity-reflection; a plain workplace run gets workplace (memory then
    refuses the self-scope tend — correct, not the door's job)."""
    from abstractgateway.entity_gate import _gate_tend, CHANNEL_ENTITY_REFLECTION, CHANNEL_WORKPLACE

    # A reflection-channel stamp -> entity-reflection injected (payload claim dropped).
    p = {"channel": "workplace-spoof", "body": "```tend\ndispose ex:x reason: stale\n```"}
    assert _gate_tend(p, stamp={"channel": CHANNEL_ENTITY_REFLECTION}, phase="visit", segment=False) is None
    assert p["channel"] == CHANNEL_ENTITY_REFLECTION

    # A workplace run IN a reflection segment -> entity-reflection.
    p2 = {"body": "x"}
    assert _gate_tend(p2, stamp={"channel": CHANNEL_WORKPLACE}, phase="visit", segment=True) is None
    assert p2["channel"] == CHANNEL_ENTITY_REFLECTION

    # A plain workplace run OUTSIDE any segment -> workplace (memory refuses).
    p3 = {"body": "x"}
    assert _gate_tend(p3, stamp={"channel": CHANNEL_WORKPLACE}, phase="visit", segment=False) is None
    assert p3["channel"] == CHANNEL_WORKPLACE


def test_gate_form_strips_claims_on_empty_stamp_and_resident_lane():
    """Door-cleanup audit P1-3: the claim-strip loop must run UNCONDITIONALLY.
    (1) A stamp with empty participants + no visit_id used to skip the loop
    entirely — a record CLAIMING participants/visit_id engraved unchecked
    (false co-presence, forever, append-only). (2) Branch 2 (resident lane,
    personal-phase record) injected nothing but also STRIPPED nothing —
    claims survived. Claims pop in every branch; verified values inject."""
    from abstractgateway.entity_gate import _gate_form

    # (1) empty stamp: forged claims still strip.
    p = {
        "records": [
            {"title": "x", "attributes": {"participants": ["person:forged"], "visit_id": "v-forged"}}
        ],
        "turn_id": "t-strip-1",
    }
    assert _gate_form(p, stamp={"participants": [], "entity_id": "entity:castor"}, phase="visit") is None
    attrs = p["records"][0]["attributes"]
    assert "participants" not in attrs, "a payload claim must never survive an empty stamp"
    assert "visit_id" not in attrs

    # (2) resident lane: personal-phase record claiming participants — the
    # claim strips AND nothing injects (nobody was at the door).
    p2 = {
        "records": [
            {"title": "y", "attributes": {"phase": "personal", "participants": ["person:forged"]}}
        ],
        "turn_id": "t-strip-2",
    }
    assert (
        _gate_form(
            p2,
            stamp={"participants": ["person:op", "entity:castor"], "entity_id": "entity:castor"},
            phase="personal",
        )
        is None
    )
    a2 = p2["records"][0]["attributes"]
    assert "participants" not in a2, "resident-lane non-visit records carry no co-presence, claimed or not"


def test_workplace_cannot_close_beliefs_or_touch_self_salience(rig):
    out = _call(
        rig,
        EffectType.MEMORY_ADJUST,
        {"op": "close", "record_id": "ex:x", "reason": "workplace revisionism", "turn_id": "t10"},
    )
    assert out.status == "failed"
    assert "not a workplace act" in (out.error or "")

    out2 = _call(
        rig,
        EffectType.MEMORY_ADJUST,
        {"op": "reinforce", "record_id": "ex:x", "reason": "r", "turn_id": "t11", "scope": "self"},
    )
    assert out2.status == "failed"
    assert "not a workplace act" in (out2.error or "")


def test_appraise_actor_is_stamped_never_claimed(rig):
    # A payload claiming the privileged actor is the exact spoof the door
    # exists to stop — loud, not silently corrected.
    out = _call(
        rig,
        EffectType.MEMORY_APPRAISE,
        {
            "target_id": "tool:restic",
            "sign": 1,
            "magnitude": 9,
            "reason": "forged bond",
            "turn_id": "t12",
            "actor": "entity-reflection",
        },
    )
    assert out.status == "failed"
    assert "stamped by the door" in (out.error or "")

    # No claimed actor: the channel actor is injected, and the engine's
    # amplitude authority now sees the TRUE (non-privileged) actor — a
    # magnitude 9 fails ENGINE-side. The door made the string true.
    out2 = _call(
        rig,
        EffectType.MEMORY_APPRAISE,
        {"target_id": "tool:restic", "sign": 1, "magnitude": 9, "reason": "big feeling", "turn_id": "t13"},
    )
    assert out2.status == "failed"
    assert "entity-reflection" in (out2.error or "") or "amplitude" in (out2.error or "").lower()

    # Routine band works, recorded under the workplace actor.
    out3 = _call(
        rig,
        EffectType.MEMORY_APPRAISE,
        {"target_id": "tool:restic", "sign": 1, "magnitude": 1, "reason": "backup ok", "turn_id": "t14"},
    )
    assert out3.status == "completed", out3.error

    grades = _call(
        rig,
        EffectType.MEMORY_APPRAISE,
        {"op": "gradation", "target_ids": ["tool:restic"]},
    )
    assert grades.status == "completed"
    assert grades.result["gradations"]["tool:restic"]["net"] == 1


# ----------------------------------------------------------------- the diary


def test_diary_write_and_progressive_disclosure_through_the_gate(rig):
    out = _call(
        rig,
        EffectType.DIARY_WRITE,
        {
            "text": "First summoned session through the gateway. The door held.",
            "gist": "First gateway summon; the door held.",
            "kind": "reflection",
            "turn_id": "t15",
        },
    )
    assert out.status == "completed", out.error
    entry_id = out.result["entry_id"]
    assert out.result.get("projected_record_id"), "the act-memory must project into the graph"

    read = _call(rig, EffectType.DIARY_READ, {"entry_id": entry_id})
    assert read.status == "completed", read.error
    assert read.result["text"].startswith("First summoned session")
    assert read.result["author"] == rig.entity_id  # bound at construction, never payload

    home = rig.registry.get_home("castor")
    report = home.verify()
    assert report["ok"] is True, report


def test_wake_reasons_surface_in_inspection(rig):
    """The autonomy-driver triad (questions/problems/ideas — curiosity,
    wrongness, direction) surfaces through inspect. Written through the gate
    (DIARY_WRITE), read through memory's folded wake-reason reads; all three
    first-class end-to-end (runtime synced the projection clamp for
    'problem' on 2026-07-07, thread 0003)."""
    for kind, text in (
        ("question", "Why does the backup verification take twice as long on Sundays?"),
        ("problem", "The restic prune job has been failing silently since Tuesday."),
        ("idea", "A weekly restore drill would turn hope into knowledge."),
    ):
        out = _call(
            rig,
            EffectType.DIARY_WRITE,
            {"text": text, "gist": text, "kind": kind, "turn_id": f"wr-{kind}"},
        )
        assert out.status == "completed", out.error

    home = rig.registry.get_home("castor")
    wake = home.wake_reasons()
    assert [q["gist"] for q in wake["questions"]] == [
        "Why does the backup verification take twice as long on Sundays?"
    ]
    assert [p["gist"] for p in wake["problems"]] == [
        "The restic prune job has been failing silently since Tuesday."
    ]
    assert [i["gist"] for i in wake["ideas"]] == [
        "A weekly restore drill would turn hope into knowledge."
    ]
    assert "warnings" not in wake

    inspection = home.inspect()
    assert inspection["wake_reasons"]["questions"], "inspect must surface the wake reasons"


def test_routing_refuses_to_shadow_existing_handlers(tmp_path: Path):
    registry = EntityRegistry(data_dir=tmp_path / "runtime")
    runtime = _StubRuntime()
    runtime._handlers[EffectType.MEMORY_RECALL] = lambda *a: None
    with pytest.raises(RuntimeError, match="refuses to shadow"):
        install_entity_routing(runtime, registry=registry, run_store=_RunStore(), artifact_store=None)


def test_reflection_segment_admits_lesson_into_life_scope():
    """2026-07-18 forensics (laurent's 'unacceptable error' thread): runtime's
    lesson election forms kind=lesson into LIFE scope at the visit close —
    the closed act set predated lessons, so every durable-visit close
    silently REFUSED the entity's elected lessons (Ephemeral ledger seqs
    119/121). lesson→life joins interest→self in the set; identity kinds
    stay refused through every visit channel."""
    from abstractgateway.entity_gate import _is_reflection_form_act

    lesson = {"records": [{"kind": "lesson", "digest": "A stronger model can improve reasoning."}],
              "scope": "life"}
    assert _is_reflection_form_act(lesson) is None

    # Identity kinds beyond interest stay untouchable (spoof pin 3).
    value = {"records": [{"kind": "value", "digest": "x"}], "scope": "self"}
    assert _is_reflection_form_act(value) is not None
    # A lesson into SELF scope is NOT the shipped shape — refused.
    lesson_self = {"records": [{"kind": "lesson", "digest": "x"}], "scope": "self"}
    assert _is_reflection_form_act(lesson_self) is not None
