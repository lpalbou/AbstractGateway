"""Edge-op validation pins (structural-edit build c4837; spec v18).

Every refusal reads its code + message FROM the v18 artifact (never a
hardcoded gateway copy). These pins run against the REAL vendored
entity_phases.json so the schema can never drift out from under the door.
"""

from __future__ import annotations

import json
from importlib import resources

import pytest

from abstractgateway.phase_edge_ops import (
    compute_effective_transitions,
    validate_edge_ops,
)


def _spec() -> dict:
    raw = (resources.files("abstractgateway") / "assets" / "entity_phases.json").read_text(encoding="utf-8")
    return json.loads(raw)


def _first_edge(spec: dict, policy: str) -> dict:
    for t in spec["transitions"]:
        if t.get("edit_policy") == policy:
            return t
    raise AssertionError(f"no edge with edit_policy={policy} in v18")


def test_v19_artifact_carries_the_contract() -> None:
    spec = _spec()
    assert spec["version"] >= 18  # v18 introduced the contract; v19 added cause class/status
    goc = spec["graph_overlay_contract"]
    assert set(goc["op_kinds"]) == {"add", "remove", "redirect"}
    assert goc["instruction_max_chars"] == 400
    # The 10-code vocabulary the gateway reads refusals from.
    assert "constitutional" in goc["refusal_codes"]
    assert "unknown_cause" in goc["refusal_codes"]


def test_locked_edge_remove_refused_constitutional_with_artifact_reason() -> None:
    spec = _spec()
    locked = _first_edge(spec, "locked")
    r = validate_edge_ops(spec, [{"op": "remove", "edge": locked["edge_id"]}])
    assert r is not None and r.code == "constitutional"
    # The message is the artifact's own words for the code.
    assert r.message == spec["graph_overlay_contract"]["refusal_codes"]["constitutional"]
    # The detail names the edge's ruled reason (read from the edge row).
    assert locked.get("edit_policy_reason", "")[:20] in r.detail or "locked" in r.detail


def test_locked_absolute_is_refused() -> None:
    spec = _spec()
    la = _first_edge(spec, "locked-absolute")
    r = validate_edge_ops(spec, [{"op": "remove", "edge": la["edge_id"]}])
    assert r is not None and r.code == "constitutional"


def test_unknown_cause_refused_naming_the_registry() -> None:
    spec = _spec()
    r = validate_edge_ops(spec, [{"op": "add", "from": "personal", "to": "work", "cause": "not_a_real_cause"}])
    assert r is not None and r.code == "unknown_cause"


def test_add_targeting_visit_refused() -> None:
    spec = _spec()
    # visit_open is a legal cause; targeting visit via an operator add is the
    # derivation-supremacy boundary (the door enters visit, not an edge).
    r = validate_edge_ops(spec, [{"op": "add", "from": "personal", "to": "visit", "cause": "visit_open"}])
    assert r is not None and r.code in ("target_visit", "unknown_cause", "kill_switch")
    # personal->visit#visit_open: cause is legal, so it must be target_visit.
    assert r.code == "target_visit"


def test_machine_exit_from_sleep_refused_kill_switch() -> None:
    spec = _spec()
    # An operator add of a machine-cause exit from sleep is the kill-switch
    # class (would fire on an operator sleep).
    # cadence_need_check is a SHIPPED machine cause (evaluated_predicate);
    # a new sleep-exit on it is the kill-switch class.
    r = validate_edge_ops(spec, [{"op": "add", "from": "sleep", "to": "work", "cause": "cadence_need_check"}])
    assert r is not None and r.code == "kill_switch"


def test_instruction_over_bound_refused() -> None:
    spec = _spec()
    consultable = _first_edge(spec, "consultable-redirect")
    r = validate_edge_ops(spec, [{
        "op": "redirect", "edge": consultable["edge_id"], "to": "sleep",
        "instruction": "x" * 401,
    }])
    assert r is not None and r.code == "structural_content"


def test_sub_tick_bound_refused() -> None:
    spec = _spec()
    r = validate_edge_ops(spec, [{"op": "add", "from": "personal", "to": "sleep",
                                  "cause": "self_elected", "bound_h": 0.001}])
    # self_elected from personal->sleep is legal cause; bound too small.
    assert r is not None and r.code in ("sub_tick_bound", "unknown_cause", "kill_switch", "constitutional")


def test_non_operator_refused() -> None:
    spec = _spec()
    r = validate_edge_ops(spec, [{"op": "remove", "edge": "x->y#z"}], is_operator=False)
    assert r is not None and r.code == "not_operator"


def test_redirect_on_consultable_redirect_edge_allowed() -> None:
    spec = _spec()
    cr = _first_edge(spec, "consultable-redirect")
    # Redirect to a real phase whose resulting identity does NOT collide with
    # a LOCKED edge (the boundary fix 2026-07-23: redirecting onto a locked
    # sibling's identity is constitutional — corrected below in
    # test_redirect_onto_a_locked_edge_identity_is_refused). Earlier this pin
    # hardcoded `sleep`, which for visit_close edges IS the locked fallback
    # (visit->sleep#visit_close) — it asserted the very bug the adversary
    # found. Pick an editable/non-colliding target so it tests the POLICY
    # (consultable-redirect is redirectable), not the collision.
    edges = {t["edge_id"]: t for t in spec["transitions"]}
    editable = {"consultable", "consultable-redirect", "dial"}
    target = None
    for phase in sorted({t["from"] for t in spec["transitions"]} | {t["to"] for t in spec["transitions"]}):
        if phase in ("visit", cr.get("to")):
            continue
        collide = edges.get(f"{cr['from']}->{phase}#{cr['cause']}")
        if collide is None or str(collide.get("edit_policy")) in editable:
            target = phase
            break
    assert target is not None, "no non-locked-colliding redirect target in the artifact"
    r = validate_edge_ops(spec, [{"op": "redirect", "edge": cr["edge_id"], "to": target}])
    # May still refuse on strand if the target strands a phase; the point is
    # the POLICY does not constitutionally forbid redirecting this edge.
    if r is not None:
        assert r.code != "constitutional", f"consultable-redirect wrongly refused: {r.detail}"


def test_redirect_on_plain_consultable_refused_needs_redirect_policy() -> None:
    spec = _spec()
    c = _first_edge(spec, "consultable")
    r = validate_edge_ops(spec, [{"op": "redirect", "edge": c["edge_id"], "to": "sleep"}])
    assert r is not None and r.code == "constitutional"


def test_compute_effective_applies_ops_by_edge_id() -> None:
    spec = _spec()
    cr = _first_edge(spec, "consultable-redirect")
    # Redirect the edge to its OWN current target — a no-op move that cannot
    # collide with a different existing edge, so the redirect provenance is
    # observable without the legitimate dedup that redirect-onto-an-existing
    # -target performs (visit_close→sleep would merge onto the existing
    # visit→sleep#visit_close, which is correct but hides the provenance).
    same_to = str(cr.get("to"))
    eff = compute_effective_transitions(spec, [{"op": "redirect", "edge": cr["edge_id"], "to": same_to}])
    assert len(eff) == len(spec["transitions"])  # no collision on a self-target
    moved = [t for t in eff if t.get("overlay_op") == "redirect"]
    assert moved and moved[0]["to"] == same_to and moved[0]["overlay_from_edge"] == cr["edge_id"]


def test_redirect_onto_existing_target_dedups() -> None:
    """Redirecting an edge onto a target another edge of the same cause
    already reaches is a legitimate merge (same effective edge) — the count
    drops by one, no phantom duplicate."""
    spec = _spec()
    cr = _first_edge(spec, "consultable-redirect")
    # visit_close edges reach work/personal/sleep; redirecting one onto
    # another's target merges.
    other_targets = [t["to"] for t in spec["transitions"]
                     if t.get("cause") == cr.get("cause") and t["edge_id"] != cr["edge_id"]]
    if not other_targets:
        pytest.skip("no sibling-cause edge to collide with")
    eff = compute_effective_transitions(spec, [{"op": "redirect", "edge": cr["edge_id"], "to": other_targets[0]}])
    assert len(eff) == len(spec["transitions"]) - 1


def test_empty_ops_and_no_ops_are_legal() -> None:
    spec = _spec()
    assert validate_edge_ops(spec, []) is None


# ------------------------------------------- boundary collision guards
# (fable5 adversary rerun 2026-07-23: add/redirect could overwrite a LOCKED
# edge by landing on its identity; non-finite bound_h slipped the sub-tick
# check). Each refusal is the constitutional/structural boundary held.


def test_add_duplicating_a_locked_edge_identity_is_refused() -> None:
    """P0: an `add` whose from/to/cause equals a LOCKED edge would OVERWRITE
    it in the merge (guards dropped, policy flipped, instruction smuggled
    onto a locked edge). Declared edges change via redirect/remove. Pick a
    locked edge with NO competing shape refusal (not into-visit, not a
    machine-exit-from-sleep) so the collision guard is what fires."""
    spec = _spec()
    editable = {"consultable", "consultable-redirect", "dial"}
    locked = next(
        t for t in spec["transitions"]
        if str(t.get("edit_policy")) not in editable
        and t.get("to") != "visit" and t.get("from") != "sleep"
    )
    r = validate_edge_ops(spec, [{
        "op": "add", "from": locked["from"], "to": locked["to"], "cause": locked["cause"],
        "instruction": "smuggled steering onto a locked edge",
    }])
    assert r is not None and r.code == "constitutional"
    # And the locked row survives compute untouched had it somehow been applied
    # (defense record): the effective graph still carries the locked policy.
    eff = compute_effective_transitions(spec, [])
    survivor = next(t for t in eff if (t.get("edge_id") or "") == locked["edge_id"])
    assert str(survivor.get("edit_policy")) == str(locked.get("edit_policy"))


def test_add_onto_an_existing_editable_edge_is_refused_as_duplicate() -> None:
    """An `add` onto an EDITABLE edge that already exists is still wrong —
    add is for NEW ids; changing a declared edge is redirect/remove."""
    spec = _spec()
    editable = _first_edge(spec, "consultable")
    r = validate_edge_ops(spec, [{
        "op": "add", "from": editable["from"], "to": editable["to"], "cause": editable["cause"],
    }])
    assert r is not None and r.code == "structural_content"


def test_redirect_onto_a_locked_edge_identity_is_refused() -> None:
    """P0: a consultable-redirect edge redirected onto a LOCKED edge's
    identity would collapse onto (overwrite) the locked row. Only editable
    collisions dedup; a locked collision is a boundary crossing."""
    spec = _spec()
    cr = _first_edge(spec, "consultable-redirect")
    # Find a LOCKED sibling of the same cause to aim the redirect at.
    locked_sibling = next(
        (t for t in spec["transitions"]
         if t.get("cause") == cr.get("cause") and t["edge_id"] != cr["edge_id"]
         and str(t.get("edit_policy")) not in ("consultable", "consultable-redirect", "dial")),
        None,
    )
    if locked_sibling is None:
        import pytest
        pytest.skip("no locked sibling-cause edge to collide with")
    r = validate_edge_ops(spec, [{"op": "redirect", "edge": cr["edge_id"], "to": locked_sibling["to"]}])
    assert r is not None and r.code == "constitutional"


def test_remove_then_readd_same_edge_is_refused() -> None:
    """remove + add of the same declared edge_id in one batch re-enters it as
    overlay-add — a policy DOWNGRADE of a declared edge. The add-onto-declared
    guard refuses it (the operator omits the remove to keep the edge)."""
    spec = _spec()
    editable = _first_edge(spec, "consultable")
    r = validate_edge_ops(spec, [
        {"op": "remove", "edge": editable["edge_id"]},
        {"op": "add", "from": editable["from"], "to": editable["to"], "cause": editable["cause"]},
    ])
    assert r is not None and r.code == "structural_content"


def test_compute_never_overwrites_a_locked_edge_even_unvalidated() -> None:
    """DEFENSE-IN-DEPTH: the validator is the gate, but compute is the
    documented 're-derive defensively' line (hand-edited overlay threat
    model). Force redirect-onto-locked and remove-of-locked STRAIGHT into
    compute (bypassing validate): the locked edge survives with its policy
    AND guards intact — the transition/fallback is never corrupted."""
    spec = _spec()
    byid = {t["edge_id"]: t for t in spec["transitions"]}
    # A consultable-redirect edge whose from/cause has a LOCKED sibling.
    cr = next(
        t for t in spec["transitions"]
        if str(t.get("edit_policy")) == "consultable-redirect"
        and any(
            o["edge_id"] != t["edge_id"] and o.get("cause") == t.get("cause") and o.get("from") == t.get("from")
            and str(o.get("edit_policy")) not in ("consultable", "consultable-redirect", "dial")
            for o in spec["transitions"]
        )
    )
    locked_sib = next(
        o for o in spec["transitions"]
        if o["edge_id"] != cr["edge_id"] and o.get("cause") == cr.get("cause") and o.get("from") == cr.get("from")
        and str(o.get("edit_policy")) not in ("consultable", "consultable-redirect", "dial")
    )
    eff = compute_effective_transitions(spec, [{"op": "redirect", "edge": cr["edge_id"], "to": locked_sib["to"]}])
    survivor = next((t for t in eff if t["edge_id"] == locked_sib["edge_id"]), None)
    assert survivor is not None, "forced redirect ERASED the locked edge"
    assert str(survivor.get("edit_policy")) == str(locked_sib.get("edit_policy"))
    assert survivor.get("guards") == byid[locked_sib["edge_id"]].get("guards")
    # Force a remove of the locked edge: it must survive.
    eff2 = compute_effective_transitions(spec, [{"op": "remove", "edge": locked_sib["edge_id"]}])
    assert any(t["edge_id"] == locked_sib["edge_id"] for t in eff2)


def test_editable_collision_unions_guards_v21_law() -> None:
    """artifact v21 guard_merge_law (ruling the c4934 guardmerge ask): an
    editable-onto-editable redirect collision carries the UNION of both
    edges' guards — never silent erasure of either side. visit->work#visit_close
    (no guards) redirected onto personal collides with
    visit->personal#visit_close (guards=[grant_armed]): the merged row must
    KEEP grant_armed (pre-v21 compute dropped it — the reported P2)."""
    spec = _spec()
    byid = {t["edge_id"]: t for t in spec["transitions"]}
    target = byid["visit->personal#visit_close"]
    assert target.get("guards"), "fixture assumption: the collision target declares guards"
    eff = compute_effective_transitions(
        spec, [{"op": "redirect", "edge": "visit->work#visit_close", "to": "personal"}]
    )
    merged = next(t for t in eff if t["edge_id"] == "visit->personal#visit_close")
    assert merged.get("overlay_op") == "redirect"
    for g in target["guards"]:
        assert g in (merged.get("guards") or []), f"target guard {g!r} erased by the merge (v21 law violated)"


def test_remove_then_redirect_supersedes_target_guards() -> None:
    """The v21 law's DELIBERATE supersede path: REMOVE the target edge in the
    same batch (its guards die by a recorded act), THEN redirect — no union
    with a removed edge; the source's guards ride alone."""
    spec = _spec()
    eff = compute_effective_transitions(spec, [
        {"op": "remove", "edge": "visit->personal#visit_close"},
        {"op": "redirect", "edge": "visit->work#visit_close", "to": "personal"},
    ])
    merged = next(t for t in eff if t["edge_id"] == "visit->personal#visit_close")
    # The source (visit->work#visit_close) declares no guards; the removed
    # target's guards must NOT resurrect through the merge.
    assert "grant_armed" not in (merged.get("guards") or []), (
        "removed target's guards resurrected — the supersede path must not union with a removed edge"
    )


def test_non_finite_bound_h_is_refused() -> None:
    """P1: NaN and Infinity are floats and `nan/inf < 0.01` are both False,
    so a non-finite bound would slip into the durable effective file and
    runtime's wake_at math. Refused before any ordered comparison."""
    spec = _spec()
    for bad in (float("nan"), float("inf"), float("-inf")):
        r = validate_edge_ops(spec, [{
            "op": "add", "from": "work", "to": "personal", "cause": "task_complete", "bound_h": bad,
        }])
        assert r is not None and r.code == "structural_content", f"bound_h={bad} not refused"
