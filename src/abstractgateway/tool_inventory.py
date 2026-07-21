"""Serve-time tool-inventory composition (descriptor contract v6, plan (a)
gateway lane; agency c909 P0-1).

This is the gateway's ONLY authorship in the derive-never-copy chain (contract
rule 1): take each owning enumeration's rows VERBATIM and attach the ONE fact
the gateway owns — `executes_via`, the CONTAINMENT the handlers were installed
under. Nothing here re-derives a field from a name.

- core rows (`abstractcore.tools.builtin_tool_inventory_as_dicts`): attach
  `executes_via="core_registry"`, `grant_lane=None`, `capability_class=None`
  (the grant-lane + boundary axes are walled concepts; a registry row owns
  neither and the gateway invents neither — contract rule 3, F1).
- runtime walled rows (`abstractruntime.identity.tools.walled_tool_rows`):
  attach `executes_via="entity_walled"`; every other field is verbatim.

Total order (contract rule 4): `(executes_via, owner, name)` ascending — two
independent implementations produce byte-identical order. Static-union
validation refuses LOUDLY if the served set diverges from the union of the
owning enumerations (a row from no enumeration, or an enumeration member
dropped from the served set).

The phase MATRIX (rule 2b) offers `entity_walled` rows ONLY — a registry-row
grant has no defined entity-phase semantics in v1.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# The gateway-owned containment values (contract v6, semantics c885 rename).
CONTAINMENT_ENTITY_WALLED = "entity_walled"
CONTAINMENT_CORE_REGISTRY = "core_registry"


def _core_rows() -> Optional[List[Dict[str, Any]]]:
    """Core's registry inventory, sourced THROUGH a runtime facade — the
    gateway never imports abstractcore directly (backlog 0059 boundary; the
    fix for a missing need is a runtime facade, not a direct import).

    Returns None (not []) when the facade is absent, so the caller can tell
    "no registry rows because the conduit isn't built yet" from "the registry
    genuinely has zero tools" and degrade HONESTLY (labeled), never silently
    serve a partial union as if it were complete.
    """
    try:
        from abstractruntime.integrations.abstractcore.tool_inventory_facade import (
            core_registry_tool_rows,
        )
    except Exception:  # noqa: BLE001 - facade not yet shipped in this runtime
        return None

    out: List[Dict[str, Any]] = []
    for row in core_registry_tool_rows():
        r = dict(row)
        # Gateway authorship: the containment these handlers execute under.
        r["executes_via"] = CONTAINMENT_CORE_REGISTRY
        # Grant lane + boundary are walled concepts — never invented for a
        # registry row (contract rule 3 / adversary F1). Absent = deny-safe.
        r.setdefault("grant_lane", None)
        r.setdefault("capability_class", None)
        out.append(r)
    return out


def _walled_rows() -> List[Dict[str, Any]]:
    from abstractruntime.identity.tools import walled_tool_rows

    out: List[Dict[str, Any]] = []
    for row in walled_tool_rows():
        r = dict(row)
        r["executes_via"] = CONTAINMENT_ENTITY_WALLED
        out.append(r)
    return out


def _order_key(row: Dict[str, Any]) -> tuple:
    return (
        str(row.get("executes_via") or ""),
        str(row.get("owner") or ""),
        str(row.get("name") or ""),
    )


def compose_tool_inventory() -> Dict[str, Any]:
    """The full served inventory: core ∪ walled, total-ordered, union-validated.

    Returns {tools, degraded, warnings}. `degraded` is True when the core
    registry facade is absent (runtime has not shipped it) — the walled rows
    still serve completely, and the absence is LABELED, never a silent partial
    union. Raises RuntimeError LOUDLY if the composed set diverges from the
    union of the enumerations that ARE present (contract rule 4).
    """
    core = _core_rows()
    walled = _walled_rows()
    warnings: List[str] = []
    degraded = core is None
    if degraded:
        warnings.append(
            "#FALLBACK core registry inventory unavailable (no runtime "
            "tool_inventory_facade); serving entity_walled rows only — the "
            "registry half lights up when the facade ships (contract rule 1, "
            "gateway never imports abstractcore directly)"
        )
        core = []
    composed = sorted(core + walled, key=_order_key)

    # Static-union validation over the enumerations that ARE present: identity
    # is (executes_via, name); the served set must equal that union exactly.
    served_ids = {(r["executes_via"], r["name"]) for r in composed}
    core_ids = {(CONTAINMENT_CORE_REGISTRY, r["name"]) for r in core}
    walled_ids = {(CONTAINMENT_ENTITY_WALLED, r["name"]) for r in walled}
    expected = core_ids | walled_ids
    if served_ids != expected:
        missing = expected - served_ids
        extra = served_ids - expected
        raise RuntimeError(
            "tool inventory composition diverged from the owning enumerations "
            f"(missing={sorted(missing)}, extra={sorted(extra)}) — derive-never-copy "
            "union validation refuses a served set that does not equal the union"
        )
    if len(composed) != len(served_ids):
        raise RuntimeError(
            "tool inventory has a duplicate (executes_via, name) identity — "
            "each containment+name pair is one row (contract rule 2)"
        )
    return {"tools": composed, "degraded": degraded, "warnings": warnings}


def entity_walled_inventory() -> List[Dict[str, Any]]:
    """The `entity_walled` rows only — the matrix-offerable set (rule 2b).

    Sourced from `_walled_rows()` directly (runtime is always present); the
    matrix never depends on the core-registry facade, so the capabilities tab
    is never degraded by that facade's absence.
    """
    return sorted(_walled_rows(), key=_order_key)


# Per-phase EXECUTION-LANE truth (c69 audit: `executable: True` was
# hardcoded for every cell including sleep, so the kit's executable:false
# cue could never fire — the dashboard showed grants no lane consumes).
# executable(tool, phase) = does a live execution lane run tools in this
# phase? Post the 2026-07-18 memory-tools fix, BOTH live lanes (visit door
# executor, personal ChatSession driver) execute the FULL walled set, so
# the per-cell truth is currently phase-uniform — kept as a function so a
# tool-level nuance lands here, never as a hand-set boolean again.
_PHASE_LANE_EXECUTION: Dict[str, tuple] = {
    "visit": (True, None),
    "personal": (True, None),
    "sleep": (False, "no execution lane runs tools in this phase — sleep is the consolidation window (grants here are stored, nothing consumes them yet)"),
    "work": (False, "no work lane exists yet — grants here are stored for the day the work phase ships"),
}


def phase_executability(tool_name: str, phase: str) -> tuple:
    """(ok, reason) for one (tool, phase) cell — the served truth behind
    the kit's executable cell axis and the /tool-policy executable map."""
    ok, reason = _PHASE_LANE_EXECUTION.get(str(phase), (False, f"unknown phase {phase!r}"))
    return bool(ok), reason


def default_phase_grants() -> Dict[str, List[str]]:
    """The framework DEFAULT grant per phase (no home yet — the creation
    modal's starting matrix). Sourced from runtime's `_default_tools`, the
    same function `resolve_tool_grant` uses when a home carries no policy
    file — so the modal's default matches the entity's birth default exactly
    (Q1: visit/work/personal = full walled set; sleep = read-only-minus-diary).
    """
    from abstractruntime import PHASES
    from abstractruntime.identity.tool_policy import _default_tools

    grants: Dict[str, List[str]] = {}
    for phase in PHASES:
        grants[phase] = list(_default_tools(phase, enable_workspace=True))
    return grants


def phase_capability_matrix(policy: Optional[Dict[str, List[str]]] = None) -> Dict[str, Any]:
    """The MatrixPayload the creation modal's capabilities tab renders — the
    EXACT shape uic's shipped `validateMatrixPayload` accepts (c727/doc-v8;
    agency c927 + uic c929 conformance catch; rule 2b: entity_walled rows only).

    Envelope (item-major, phases-as-metadata-array — NOT phase-major):
      {schema_version, phases: [{id, label, hint}],
       sections: [{id:"tools", label, items: [
         {id:<tool>, label, description, cells: {<phase_id>: MatrixCell}}]}]}

    MatrixCell per (tool, phase): {assigned, resolved_value, provenance,
    availability, executable, reason?} + the descriptor riders (grant_lane,
    capability_class, mutating, remote_write_capable) as legal additive
    server-truth fields. (act_only died with the ref layer, runtime c273.) Semantics (uic c929, load-bearing):
    - assigned = the OPERATOR HAS A STORED WORD on this cell (the policy
      explicitly grants this tool in this phase). A birth default is
      assigned=false with resolved_value=granted-by-default — restating a
      default is a no-op, clear = remove-my-word (both shipped behaviors).
    - resolved_value = the effective grant (policy for a policied phase, else
      the framework default).
    - provenance is PER CELL: "operator" for a phase the policy names, else
      "default".

    `policy` maps phase -> granted tool names (existing/proposed grant); None
    = the framework birth defaults (the creation modal's starting matrix).
    """
    from abstractruntime import PHASES

    walled = entity_walled_inventory()
    defaults = default_phase_grants()

    phase_ids = list(PHASES)
    phases_meta = [{"id": p, "label": p.capitalize(), "hint": None} for p in phase_ids]

    def _effective(phase: str) -> set:
        if policy is not None and phase in policy:
            return set(policy.get(phase) or [])
        return set(defaults.get(phase, []))

    items: List[Dict[str, Any]] = []
    for row in walled:
        name = row["name"]
        cells: Dict[str, Any] = {}
        for phase in phase_ids:
            operator_worded = policy is not None and phase in policy
            resolved = name in _effective(phase)
            # assigned = operator explicitly named this tool in this phase's
            # stored grant (uic c929: assigned:true only for cells the policy
            # names). A birth default is assigned=false, resolved=true.
            assigned = bool(operator_worded and resolved)
            # Executability is SERVED TRUTH per (tool, phase) — the c69
            # audit found this hardcoded True for every cell (sleep/work
            # included), leaving the kit's executable:false cue dormant
            # while the dashboard showed grants nothing consumes.
            exec_ok, exec_reason = phase_executability(name, phase)
            cells[phase] = {
                "assigned": assigned,
                "resolved_value": resolved,
                "provenance": "operator" if operator_worded else "default",
                "availability": "granted" if resolved else "denied",
                "executable": exec_ok,
                "grant_lane": row.get("grant_lane"),
                "capability_class": row.get("capability_class"),
                "mutating": bool(row.get("mutating")),
                "remote_write_capable": bool(row.get("remote_write_capable")),
                # act_only died with the ref layer (runtime c273) — walled
                # rows no longer carry it; no tool is act-only anymore.
                "reason": exec_reason,
            }
        items.append({
            "id": name,
            "label": name,
            "description": row.get("description", ""),
            "cells": cells,
        })

    return {
        "schema_version": 1,
        "containment": CONTAINMENT_ENTITY_WALLED,
        "phases": phases_meta,
        "sections": [{"id": "tools", "label": "Tools", "items": items}],
    }
