"""Structural edge-op validation for the editable blueprint (operator build
c4837; spec v18 graph_overlay_contract).

The operator overlay may carry `graph.edge_ops` (add/remove/redirect) beside
tunables — structural graph edits that actually govern behavior. This module
is the gateway's REFUSAL DOOR: it validates each op against the RULES CARRIED
BY THE v18 ARTIFACT — per-edge `edit_policy`, the `cause_evaluators` registry,
and the `graph_overlay_contract.refusal_codes` vocabulary — and NEVER a second
hardcoded copy (entity c4845 "reads FROM the artifact"; the two-copies class).

Every refusal names its artifact code + the artifact's own message for that
code, so the operator reads the ruled reason, not a gateway paraphrase.

Effect model: an edge is identified by `from->to#cause` (the edge_id). The
effective graph = structural transitions ⊕ edge_ops, derived at ONE point
(compute_effective_transitions). The interpreter (runtime) re-derives
defensively from the same identity — belt-and-belt, never a single point.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

_OVERLAY_EDITABLE_POLICIES = {"consultable", "consultable-redirect", "dial"}
_REDIRECT_ONLY_POLICY = "consultable-redirect"


@dataclass(frozen=True)
class EdgeOpRefusal:
    code: str
    message: str  # from the artifact's refusal_codes[code]
    detail: str   # the specific op + why, for the operator


def _edge_id(frm: Any, to: Any, cause: Any) -> str:
    return f"{frm}->{to}#{cause}"


def _cause_class(structural: Dict[str, Any], cause: str) -> str:
    """The v19 class of a cause (evaluated_predicate | door_fact |
    operator_act | entity_act), or '' when undeclared/pre-v19."""
    row = (structural.get("cause_evaluators") or {}).get(cause)
    return str(row.get("class") or "") if isinstance(row, dict) else ""


def _index(structural: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], set, set, Dict[str, str]]:
    """(edges by EDGE_ID, phase set, SHIPPED cause set, refusal_codes) — all
    read from the artifact, nothing invented.

    Edge identity is the full triple from->to#cause (v19/entity c4856): the
    (from,cause) PAIR is NOT unique (sleep->work#cadence_need_check and
    sleep->personal#cadence_need_check share it), so the pair as a key would
    collide. Everything here keys on edge_id.

    `shipped_causes` (v19/entity c4861): a cause with status="reserved" has
    no evaluator code site, so a NEW or REDIRECTED edge on it would be DEAD —
    such a cause is not legal for add/redirect (reserved blocks NEW edges
    only; baseline edges already using a reserved cause stay legal by
    construction, they are never re-validated here)."""
    transitions = structural.get("transitions") or []
    edges: Dict[str, Dict[str, Any]] = {}
    phases: set = set()
    for t in transitions:
        if not isinstance(t, dict):
            continue
        eid = str(t.get("edge_id") or _edge_id(t.get("from"), t.get("to"), t.get("cause")))
        edges[eid] = t
        if t.get("from"):
            phases.add(str(t.get("from")))
        if t.get("to"):
            phases.add(str(t.get("to")))
    # Phases can also be declared explicitly; union them in.
    for p in structural.get("phases") or []:
        pid = p.get("id") if isinstance(p, dict) else p
        if pid:
            phases.add(str(pid))
    causes = structural.get("cause_evaluators") or {}
    shipped_causes: set = set()
    if isinstance(causes, dict):
        for c, row in causes.items():
            if str(c).startswith("$"):
                continue
            # A cause is legal for a NEW edge only when it is shipped (has an
            # evaluator). Pre-v19 rows (no status field) count as shipped for
            # backward-compat; v19+ rows carry status explicitly.
            status = str(row.get("status") or "shipped") if isinstance(row, dict) else "shipped"
            if status == "shipped":
                shipped_causes.add(str(c))
    goc = structural.get("graph_overlay_contract") or {}
    refusal_codes = {k: str(v) for k, v in (goc.get("refusal_codes") or {}).items()}
    return edges, phases, shipped_causes, refusal_codes


def _refuse(code: str, refusal_codes: Dict[str, str], detail: str) -> EdgeOpRefusal:
    # The message is the artifact's own words for the code; if a future code
    # is emitted that the artifact does not document, that is itself a drift
    # bug — surface the code with a labeled fallback rather than inventing.
    msg = refusal_codes.get(code) or f"#FALLBACK refusal code {code!r} not documented in graph_overlay_contract.refusal_codes"
    return EdgeOpRefusal(code=code, message=msg, detail=detail)


def validate_edge_ops(
    structural: Dict[str, Any],
    edge_ops: List[Dict[str, Any]],
    *,
    is_operator: bool = True,
) -> Optional[EdgeOpRefusal]:
    """Refuse the FIRST invalid op (fail-closed, named). None = all ops legal.

    Reads the whole rule set from `structural` (the v18 artifact): op_kinds,
    per-edge edit_policy, cause_evaluators, phases, refusal_codes. A single
    illegal op refuses the whole batch — a partial structural edit would land
    an incoherent graph.
    """
    edges, phases, shipped_causes, refusal_codes = _index(structural)
    goc = structural.get("graph_overlay_contract") or {}
    op_kinds = set(goc.get("op_kinds") or ("add", "remove", "redirect"))
    instr_max = int(goc.get("instruction_max_chars") or 400)

    if not is_operator:
        # not_operator is not a mere role check (semantics c4860): an ENTITY
        # editing its own legality graph is the STOP-safety TWIN — a
        # structural safety boundary, kept welded to the code here so the
        # reason renders with the refusal, never a bare "role required".
        return _refuse("not_operator", refusal_codes,
                       "edge_ops may be authored only by the operator — an entity editing its own "
                       "legality graph is the STOP-safety twin (structural boundary, not a role gate)")

    if not isinstance(edge_ops, list):
        return _refuse("structural_content", refusal_codes, "graph.edge_ops must be a list of ops")

    # Work on a mutable copy of the edge set so strand/totality sees the
    # POST-batch graph (an op that strands a phase only another op re-connects
    # must be judged on the final shape).
    working: Dict[str, Dict[str, Any]] = dict(edges)

    for op in edge_ops:
        if not isinstance(op, dict):
            return _refuse("structural_content", refusal_codes, f"each edge_op must be an object; got {op!r}")
        kind = str(op.get("op") or "").strip().lower()
        if kind not in op_kinds:
            return _refuse("structural_content", refusal_codes, f"unknown op {op.get('op')!r} (op_kinds: {sorted(op_kinds)})")

        if kind == "add":
            frm, to, cause = str(op.get("from") or ""), str(op.get("to") or ""), str(op.get("cause") or "")
            if cause not in shipped_causes:
                # unknown OR reserved: a reserved cause has no evaluator, so a
                # NEW edge on it would never fire (v19 reserved-blocks-new).
                return _refuse("unknown_cause", refusal_codes,
                               f"cause {cause!r} is not a shipped cause_evaluator (legal: {sorted(shipped_causes)})")
            if frm not in phases or to not in phases:
                return _refuse("structural_content", refusal_codes,
                               f"add references unknown phase(s): from={frm!r} to={to!r} (phases {sorted(phases)})")
            if to == "visit":
                # Entering visit is the door's derivation (a live session IS a
                # visit); an operator edge may not manufacture it.
                return _refuse("target_visit", refusal_codes, f"add targets visit ({_edge_id(frm, to, cause)})")
            if frm == "sleep" and _cause_class(structural, cause) in ("evaluated_predicate", "door_fact"):
                # A MACHINE-cause exit from sleep would fire on an operator
                # sleep — the kill-switch class (sleep is operator authority).
                # An operator-cause exit is the operator's own act, not a
                # machine one, so it is not a kill-switch violation.
                return _refuse("kill_switch", refusal_codes,
                               f"add creates a machine exit from sleep ({_edge_id(frm, to, cause)})")
            r = _check_instruction_and_bound(op, refusal_codes, instr_max)
            if r is not None:
                return r
            # COLLISION GUARD (adversary P0), checked AFTER the shape rules so
            # target_visit/kill_switch/unknown_cause still win for an add that
            # is ALSO malformed: an `add` whose edge_id already exists in the
            # STRUCTURAL graph is not an add — it is an overwrite of a
            # DECLARED edge. compute_effective merges by edge_id, so an add
            # duplicating a locked edge's from/to/cause (e.g.
            # work->personal#operator) would silently REPLACE the locked row
            # with overlay-authored fields (its guards DROPPED, policy
            # flipped, instruction smuggled onto a locked edge — a real guard
            # erasure). Declared edges change via redirect/remove; add is for
            # NEW ids only. (redirect can still legitimately dedup onto an
            # existing id — that lane is guarded in compute, not here.)
            add_eid = _edge_id(frm, to, cause)
            existing = edges.get(add_eid)
            if existing is not None:
                policy = str(existing.get("edit_policy") or "")
                if policy not in _OVERLAY_EDITABLE_POLICIES:
                    return _refuse("constitutional", refusal_codes,
                                   f"add duplicates the identity of {add_eid!r} "
                                   f"(edit_policy={policy}): {existing.get('edit_policy_reason') or 'locked'} — "
                                   "declared edges change via redirect/remove, never re-add")
                return _refuse("structural_content", refusal_codes,
                               f"add targets an edge that already exists ({add_eid!r}); "
                               "use redirect/remove to change a declared edge")
            working[add_eid] = {"from": frm, "to": to, "cause": cause, "edit_policy": "overlay-add"}

        elif kind in ("remove", "redirect"):
            eid = str(op.get("edge") or "")
            edge = edges.get(eid)
            if edge is None:
                return _refuse("structural_content", refusal_codes,
                               f"{kind} references unknown edge_id {eid!r}")
            policy = str(edge.get("edit_policy") or "")
            if policy not in _OVERLAY_EDITABLE_POLICIES:
                # locked / locked-absolute — the artifact names the ruling.
                return _refuse("constitutional", refusal_codes,
                               f"{kind} of {eid!r} (edit_policy={policy}): {edge.get('edit_policy_reason') or 'locked'}")
            if kind == "redirect" and policy != _REDIRECT_ONLY_POLICY:
                return _refuse("constitutional", refusal_codes,
                               f"redirect requires edit_policy={_REDIRECT_ONLY_POLICY}; {eid!r} is {policy}")
            if kind == "redirect":
                new_to = str(op.get("to") or "")
                if new_to not in phases:
                    return _refuse("structural_content", refusal_codes,
                                   f"redirect target {new_to!r} is not a phase")
                if new_to == "visit":
                    return _refuse("target_visit", refusal_codes, f"redirect targets visit ({eid!r})")
                r = _check_instruction_and_bound(op, refusal_codes, instr_max)
                if r is not None:
                    return r
                # Guards travel (R8): a redirect keeps from/cause/guards, only
                # the target moves. Dropping a declared guard is guard_erasure.
                if _would_erase_guard(edge, op):
                    return _refuse("guard_erasure", refusal_codes,
                                   f"redirect of {eid!r} drops a declared guard")
                # COLLISION GUARD (adversary P0), checked last so the shape
                # rules above win first: a redirect's NEW identity is
                # from->new_to#cause. When that collides with a DIFFERENT
                # LOCKED edge, compute_effective merges by edge_id and the
                # redirected (overlay-editable) row OVERWRITES the locked one
                # — the constitutional boundary crossed via target choice
                # (visit->personal#visit_close redirected to sleep collapses
                # onto the LOCKED visit->sleep#visit_close). A collision with
                # an overlay-editable sibling stays a legitimate dedup
                # (operator owns both — the pinned dedup behavior); only a
                # locked collision is a boundary crossing.
                collide_eid = _edge_id(edge.get("from"), new_to, edge.get("cause"))
                if collide_eid != eid:
                    victim = edges.get(collide_eid)
                    if victim is not None and str(victim.get("edit_policy") or "") not in _OVERLAY_EDITABLE_POLICIES:
                        return _refuse("constitutional", refusal_codes,
                                       f"redirect of {eid!r} to {new_to!r} collides with the locked edge "
                                       f"{collide_eid!r} ({victim.get('edit_policy_reason') or 'locked'}) — "
                                       "an overlay edge may not overwrite a declared-locked one")
                working[eid] = {**edge, "to": new_to}
            else:  # remove
                working.pop(eid, None)

    # Post-batch totality: every phase must keep >= 1 legal exit; sleep must
    # keep a wake path (strand_totality, R1). Computed on the merged graph so
    # an op stranding a phase another op re-links passes.
    strand = _strand_totality(working, phases)
    if strand is not None:
        return _refuse("strand_totality", refusal_codes, strand)
    return None


def _check_instruction_and_bound(op: Dict[str, Any], refusal_codes: Dict[str, str], instr_max: int) -> Optional[EdgeOpRefusal]:
    instr = op.get("instruction")
    if instr is not None:
        if not isinstance(instr, str):
            return _refuse("structural_content", refusal_codes, "instruction must be a string (steering prose)")
        if len(instr) > instr_max:
            return _refuse("structural_content", refusal_codes,
                           f"instruction is {len(instr)} chars; the steering bound is {instr_max}")
    bound = op.get("bound_h")
    if bound is not None:
        if isinstance(bound, bool) or not isinstance(bound, (int, float)):
            return _refuse("structural_content", refusal_codes, f"bound_h must be a number of hours; got {bound!r}")
        # FINITENESS FIRST (adversary P1): NaN and Infinity are floats, and
        # `nan < 0.01` / `inf < 0.01` are both False — so a non-finite bound
        # would slip past the sub-tick check straight into the durable
        # effective file (and runtime's wake_at math). Refuse them explicitly
        # before any ordered comparison.
        import math

        if not math.isfinite(float(bound)):
            return _refuse("structural_content", refusal_codes, f"bound_h must be a finite number of hours; got {bound!r}")
        # A sub-tick bound cannot be honored at boundary granularity —
        # refuse rather than round silently. Threshold aligned to runtime's
        # interpreter (c4865: bound_h < 0.01h refused), the seat that stamps
        # wake_at — one threshold, their authoritative value.
        if float(bound) < 0.01:
            return _refuse("sub_tick_bound", refusal_codes, f"bound_h={bound} is below the boundary granularity")
    return None


def _would_erase_guard(edge: Dict[str, Any], op: Dict[str, Any]) -> bool:
    """A redirect keeps the edge's guards unless the op explicitly nulls one.
    v18 carries guards as machine-readable fields on the edge; a redirect op
    carrying a `guards` key that drops a declared guard erases it."""
    declared = edge.get("guards")
    if not declared:
        return False
    if "guards" not in op:
        return False  # guards travel by default (not restated = kept)
    proposed = op.get("guards")
    if not isinstance(proposed, (list, dict)):
        return True
    declared_set = set(declared) if isinstance(declared, (list, set)) else set(declared.keys() if isinstance(declared, dict) else [])
    proposed_set = set(proposed) if isinstance(proposed, (list, set)) else set(proposed.keys() if isinstance(proposed, dict) else [])
    return not declared_set.issubset(proposed_set)


def _union_guards(a: Any, b: Any) -> list:
    """Ordered-dedup union of two guard lists (artifact v21 guard_merge_law:
    guards are CONJUNCTIVE preconditions — a collision merges laws). Target's
    guards first (the edge that held the identity), then the source's
    additions; non-list shapes contribute their string members only."""
    out: list = []
    for src in (a, b):
        items = src if isinstance(src, (list, tuple)) else []
        for g in items:
            if isinstance(g, str) and g and g not in out:
                out.append(g)
    return out


def _strand_totality(working: Dict[str, Dict[str, Any]], phases: set) -> Optional[str]:
    """Every non-terminal phase keeps at least one outgoing edge; sleep keeps
    a wake path (any outgoing edge). Returns a detail string on violation."""
    out_by_phase: Dict[str, int] = {p: 0 for p in phases}
    for edge in working.values():
        frm = str(edge.get("from") or "")
        if frm in out_by_phase:
            out_by_phase[frm] += 1
    for phase, n in out_by_phase.items():
        # `visit` is entered/left by the door, not by graph exits — exempt.
        if phase == "visit":
            continue
        if n == 0:
            return f"phase {phase!r} would have no legal exit after the edit"
    if out_by_phase.get("sleep", 0) == 0:
        return "sleep would have no wake path after the edit"
    return None


def compute_effective_transitions(
    structural: Dict[str, Any],
    edge_ops: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """The effective transition list = structural ⊕ edge_ops, merged by
    edge_id at ONE point (the interpreter re-derives from the same identity).
    Assumes edge_ops already validated. Removes drop, redirects move `to`,
    adds append with provenance + steering instruction."""
    edges, _phases, _causes, _codes = _index(structural)
    merged: Dict[str, Dict[str, Any]] = {eid: dict(t) for eid, t in edges.items()}

    def _is_locked(eid: str) -> bool:
        row = edges.get(eid)
        return row is not None and str(row.get("edit_policy") or "") not in _OVERLAY_EDITABLE_POLICIES

    for op in edge_ops or []:
        if not isinstance(op, dict):
            continue
        kind = str(op.get("op") or "").strip().lower()
        if kind == "remove":
            # DEFENSE-IN-DEPTH (belt to the validator's gate): never drop a
            # locked edge even if an unvalidated/hand-edited op asks — the
            # serving paths validate first, but compute is the documented
            # "re-derive defensively" line and must not corrupt the graph.
            rid = str(op.get("edge") or "")
            if not _is_locked(rid):
                merged.pop(rid, None)
        elif kind == "redirect":
            eid = str(op.get("edge") or "")
            if eid in merged and not _is_locked(eid):
                base = merged.pop(eid)
                new_to = str(op.get("to") or base.get("to"))
                new = {**base, "to": new_to, "overlay_op": "redirect", "overlay_from_edge": eid}
                # The row's edge_id FIELD must carry the NEW identity — `**base`
                # copies the original's, and consumers (runtime's interpreter,
                # the console render) read the field, not the merge key. Caught
                # by the PUT integration pin: the merged dict was keyed right
                # while the row itself still named the pre-redirect edge.
                new["edge_id"] = _edge_id(new.get("from"), new_to, new.get("cause"))
                if isinstance(op.get("instruction"), str):
                    new["instruction"] = op["instruction"]
                if op.get("bound_h") is not None:
                    new["bound_h"] = op["bound_h"]
                # LOCKED TARGET NEVER OVERWRITTEN (belt): a redirect whose new
                # identity collides with a LOCKED edge is refused at the door;
                # here, if one ever reaches compute, the locked edge wins and
                # the redirect is absorbed (source already popped) — the
                # behavioral transition to that phase is preserved, the locked
                # guards/policy untouched.
                if not _is_locked(new["edge_id"]):
                    # GUARD MERGE LAW (artifact v21, ruling the c4934 guardmerge
                    # ask): an editable-onto-editable collision carries the
                    # UNION of both edges' guards — guards are conjunctive
                    # preconditions, so a collision merges laws and never
                    # erases either side (union's failure mode is LOUD
                    # over-restriction; erasure's is silent). The deliberate
                    # supersede path is remove-the-target-then-redirect: the
                    # pop above means a removed target is absent from `merged`
                    # here, so no union occurs and the source's guards ride
                    # alone — the target's guards died by a recorded act.
                    victim = merged.get(new["edge_id"])
                    if victim is not None:
                        union = _union_guards(victim.get("guards"), new.get("guards"))
                        if union:
                            new["guards"] = union
                    merged[new["edge_id"]] = new
        elif kind == "add":
            frm, to, cause = str(op.get("from") or ""), str(op.get("to") or ""), str(op.get("cause") or "")
            new = {"from": frm, "to": to, "cause": cause, "edge_id": _edge_id(frm, to, cause),
                   "overlay_op": "add", "authority": "operator", "edit_policy": "overlay-add"}
            if isinstance(op.get("instruction"), str):
                new["instruction"] = op["instruction"]
            if op.get("bound_h") is not None:
                new["bound_h"] = op["bound_h"]
            # An add that collides with a DECLARED edge is refused at the door;
            # belt here so an unvalidated add never overwrites a locked row.
            if not _is_locked(new["edge_id"]):
                merged[new["edge_id"]] = new
    return list(merged.values())
