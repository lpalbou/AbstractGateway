"""Per-entity skills selection — the management half of laurent's c2857.

WHAT an entity is taught beyond the capability map is an operator choice
that lives IN THE HOME (`<home>/skills.yaml`, beside tool_policy.yaml /
substrate.yaml / capability_map.md — it travels on directory copy) and is
RESOLVED against the abstractskill shelf through the SAME trust gate every
other lane uses (`resolve_run_skills` → select_skills_for_context:
default-REQUESTED, never trust-bypassed — decision:skills-union-spawn-wiring
holds for entities exactly as for agents).

Selection file shape (operator-owned YAML):

    skills:
      - name: entity-self-knowledge
        phases: [personal, work]   # optional; ABSENT = selected everywhere

Rules:

- Selections pin by NAME and resolve to the CURRENT shelf state at read/
  summon time — a shelf re-pin reaches a home the next time the home is
  resolved, never by bulk push (skill's c2840 discipline, adopted c2841).
- Phases validate against the ruled four (imported from abstractruntime —
  one source, never a copy; the diary_type-clamp drift lesson).
- A selection naming a skill the shelf cannot serve resolves as a LABELED
  verdict (missing/blocked/held), never a silent drop — the write-time
  preview makes a typo visible the moment it is made.
- DELIVERY into entity prompts is deliberately NOT here: the delivery slot
  (progressive disclosure per skill c2840) is runtime's election — this
  module records and resolves the selection; nothing reads it into a
  prompt until runtime names the composition slot.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

SKILLS_FILENAME = "skills.yaml"

# THE DEFAULT MIND-SKILL (laurent seq 156 "all entities must know how their
# memory work and how to leverage it"; skill c161 named it): every entity is
# born selecting entity-self-knowledge in EVERY phase — its whole charter is
# the capability map (how memory forms, the keys that are his, the
# elections, the limits), and it asks NOTHING of the entity (orientation
# only, zero solicitations), so default-everywhere adds no cognitive load.
# A mind should know itself in every phase.
DEFAULT_ENTITY_SKILL = "entity-self-knowledge"


# The ruled-four phase vocabulary, imported from the owning package.
# (decision:phase-vocabulary v4; PHASES is runtime's canonical tuple.)


def _ruled_phases() -> tuple:
    from abstractruntime import PHASES

    return tuple(PHASES)


def resolve_default_capability_map(data_dir: Any) -> Optional[str]:
    """The canonical capability-map text from the abstractskill shelf, or
    None when the shelf cannot be resolved (env unset / no checkout). The
    map is entity-self-knowledge's reference; installing it at birth is what
    makes an entity "born knowing how to remember". Best-effort by design —
    a create must never fail because the shelf is unreachable; the caller
    degrades to a labeled warning and the operator can re-install later."""
    try:
        from .capability_inventories import _repo_root_for_shelf
        from .skills_union import _shelf_registry_dir

        registry_dir, _note = _shelf_registry_dir(_repo_root_for_shelf(Path(data_dir)))
        if registry_dir is None:
            return None
        map_path = registry_dir / "skills" / DEFAULT_ENTITY_SKILL / "references" / "capability_map.md"
        if not map_path.is_file():
            return None
        text = map_path.read_text(encoding="utf-8")
        return text or None
    except Exception:  # noqa: BLE001 - shelf resolution never breaks a birth
        return None


def skills_selection_path(home_dir: Any) -> Path:
    return Path(home_dir) / SKILLS_FILENAME


def read_skills_selection(home_dir: Any) -> Dict[str, Any]:
    """The stored selection. Absent file => exists:false with an empty list;
    a malformed file reads as empty WITH a labeled warning (substrate's
    malformed-reads-as-unset posture, plus the label)."""
    path = skills_selection_path(home_dir)
    if not path.is_file():
        return {"exists": False, "skills": []}
    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"exists": True, "skills": [], "warning": f"#FALLBACK skills.yaml unreadable: {e}"}
    if not isinstance(raw, dict) or not isinstance(raw.get("skills"), list):
        return {"exists": True, "skills": [], "warning": "#FALLBACK skills.yaml is not a mapping with a skills list"}
    out: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for item in raw["skills"]:
        if isinstance(item, str) and item.strip():
            out.append({"name": item.strip()})
            continue
        if not isinstance(item, dict):
            warnings.append(f"#FALLBACK skipped non-mapping selection entry: {item!r}")
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            warnings.append("#FALLBACK skipped selection entry without a name")
            continue
        entry: Dict[str, Any] = {"name": name}
        phases = item.get("phases")
        if isinstance(phases, list):
            clean = [str(p).strip().lower() for p in phases if str(p).strip()]
            entry["phases"] = clean
        out.append(entry)
    result: Dict[str, Any] = {"exists": True, "skills": out}
    if warnings:
        result["warnings"] = warnings
    return result


def validate_skills_selection(skills: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Loud validation for writes: unique non-empty names; phases (when
    present) each one of the ruled four. Returns the normalized list."""
    ruled = set(_ruled_phases())
    seen: set = set()
    normalized: List[Dict[str, Any]] = []
    for item in skills or []:
        if not isinstance(item, dict):
            raise ValueError(f"selection entries must be mappings, got {type(item).__name__}")
        name = str(item.get("name") or "").strip()
        if not name:
            raise ValueError("every selection entry needs a non-empty name")
        if name in seen:
            raise ValueError(f"duplicate skill selection: {name!r}")
        seen.add(name)
        entry: Dict[str, Any] = {"name": name}
        phases = item.get("phases")
        if phases is not None:
            if not isinstance(phases, list) or not phases:
                raise ValueError(f"{name!r}: phases must be a non-empty list when present (omit for all phases)")
            clean = []
            for p in phases:
                p2 = str(p or "").strip().lower()
                if p2 not in ruled:
                    raise ValueError(f"{name!r}: unknown phase {p!r} (one of {sorted(ruled)})")
                if p2 not in clean:
                    clean.append(p2)
            entry["phases"] = clean
        normalized.append(entry)
    return normalized


def write_skills_selection(home_dir: Any, skills: List[Dict[str, Any]]) -> None:
    """Whole-document replace (the selection IS the operator's word), atomic."""
    import os
    import tempfile

    import yaml

    normalized = validate_skills_selection(skills)
    path = skills_selection_path(home_dir)
    doc = yaml.safe_dump({"skills": normalized}, sort_keys=False, allow_unicode=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".skills_", suffix=".tmp", dir=str(Path(home_dir)))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(doc)
        os.replace(tmp_name, str(path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except Exception:
            pass
        raise


def resolve_entity_skills(home_dir: Any, *, data_dir: Path) -> Dict[str, Any]:
    """Selection + shelf truth, resolved server-side (uic's matrix pin: the
    UI renders SERVER truth, never re-derives policy).

    Returns {selection, resolved, matrix}:
    - selection: the stored file (exists/skills/warnings)
    - resolved: per selected skill — roster row (name, description,
      trust_level, requires_review, blocked, tree_hash, source), the
      phases it is selected for (None = everywhere), and `active` from the
      trust gate; plus gate verdicts for anything that did not resolve
    - matrix: the PhaseCapabilityMatrix payload, in the KIT-VALIDATED shape
      (uic's executable corrections, c2894 — run against the real compiled
      validator, not guessed): numeric `schema_version: 1`; ruled-four
      `phases` declared in-payload; items nest INSIDE their section
      (`sections[].items[].cells` — no top-level items/cells); availability
      enum `granted|denied|structurally_unavailable|trust_gated` (a
      non-selected cell is `denied` + `assigned: false`, never a private
      word; advisory blocking is `trust_gated` + `trust_state: "blocked"`);
      trust_state enum `attachable|requires_review|blocked`; every cell
      carries the REQUIRED booleans `assigned` (the operator holds an
      explicit stored word) and `resolved_value` (the resolved on/off).
      The whole SHELF renders as rows — selected skills with their cells,
      unselected shelf skills as denied/unassigned rows — so the grid is a
      real management surface, not a read-only echo of the selection.
      A GLOBAL selection renders the ruled four with identical cells
      (uic c2844, adopted).
    """
    from .capability_inventories import resolve_run_skills, skills_inventory

    selection = read_skills_selection(home_dir)
    names = [s["name"] for s in selection["skills"]]
    phases_by_name: Dict[str, Optional[List[str]]] = {
        s["name"]: s.get("phases") for s in selection["skills"]
    }

    inventory = skills_inventory(data_dir=data_dir)
    rows_by_name: Dict[str, Dict[str, Any]] = {str(r.get("name")): r for r in inventory.get("skills") or []}

    resolved_block: Dict[str, Any] = {"skills": [], "verdicts": []}
    active: List[str] = []
    requires_by_name: Dict[str, Any] = {}
    unmet_by_name: Dict[str, Any] = {}
    if names:
        gate = resolve_run_skills(names, data_dir=data_dir)
        active = list(gate.get("active") or [])
        resolved_block["verdicts"] = list(gate.get("verdicts") or [])
        # 0008 consumer half: declared dependencies + this gateway's unmet
        # verdicts ride the resolve verbatim so the console/entity app can
        # render selectable-with-warning / blocked-with-reason, never silent.
        requires_by_name = dict(gate.get("requires") or {})
        unmet_by_name = dict(gate.get("requires_unmet") or {})
        for name in names:
            row = dict(rows_by_name.get(name) or {"name": name})
            row["selected_phases"] = phases_by_name.get(name)
            row["active"] = name in active
            if name in requires_by_name:
                row["requires"] = requires_by_name[name]
            if name in unmet_by_name:
                row["requires_unmet"] = unmet_by_name[name]
            if name not in rows_by_name:
                row.setdefault("trust_level", "unknown")
                row.setdefault("requires_review", True)
                row.setdefault("reasons", ["not on the shelf"])
            resolved_block["skills"].append(row)

    ruled = list(_ruled_phases())

    def _cell(*, name: str, phase: str, row: Dict[str, Any]) -> Dict[str, Any]:
        # Kit provenance enum is default|operator|structural (the validator
        # refuses free strings — live-verified against the compiled kit):
        # a stored selection is the OPERATOR's word; an unselected cell is
        # the framework default; trust blocking rides trust_state, never a
        # provenance word.
        selected_phases = phases_by_name.get(name)
        is_selected = name in phases_by_name and (selected_phases is None or phase in selected_phases)
        if not is_selected:
            return {
                "availability": "denied",
                "provenance": "default",
                "assigned": False,
                "resolved_value": False,
            }
        # 0008 consumer half: a selected skill whose declared dependency this
        # gateway cannot serve is STRUCTURALLY unavailable in every phase —
        # the kit enum's word for "the substrate is missing", with the
        # missing dependency named in reason (never a silent activation).
        unmet = unmet_by_name.get(name)
        if unmet:
            # Wording states what was CHECKED (semantics c3965 precision 2:
            # never overclaim — declaration/universe membership, not
            # reachability or grant state). Causes ride the requires_unmet
            # MAP (machine-readable); this reason text is display-only.
            missing_bits = []
            if unmet.get("mcp_servers"):
                missing_bits.append("MCP server(s) not declared: " + ", ".join(unmet["mcp_servers"]))
            if unmet.get("tools"):
                missing_bits.append("tool(s) not in the run tool universe: " + ", ".join(unmet["tools"]))
            return {
                "availability": "structurally_unavailable",
                "provenance": "operator",
                "assigned": True,
                "resolved_value": False,
                "reason": "requires unmet — " + "; ".join(missing_bits),
            }
        blocked = bool(row.get("blocked"))
        requires_review = bool(row.get("requires_review"))
        reasons = "; ".join(str(r) for r in (row.get("reasons") or []))
        if blocked:
            cell: Dict[str, Any] = {
                "availability": "trust_gated",
                "trust_state": "blocked",
                "provenance": "operator",
                "assigned": True,
                "resolved_value": False,
                "reason": reasons or "blocked by trust verdict",
            }
        elif requires_review or name not in active:
            cell = {
                "availability": "trust_gated",
                "trust_state": "requires_review",
                "provenance": "operator",
                "assigned": True,
                "resolved_value": False,
            }
            if reasons:
                cell["reason"] = reasons
        else:
            cell = {
                "availability": "granted",
                "provenance": "operator",
                "assigned": True,
                "resolved_value": True,
            }
        return cell

    # Rows = the whole shelf (management surface), selected-but-unshelved
    # names included so a typo stays VISIBLE in the grid, never hidden.
    row_names = list(rows_by_name.keys())
    for name in names:
        if name not in rows_by_name:
            row_names.append(name)
    items: List[Dict[str, Any]] = []
    for name in row_names:
        row = rows_by_name.get(name) or {"name": name, "requires_review": True, "reasons": ["not on the shelf"]}
        item: Dict[str, Any] = {
            "id": name,
            "label": name,
            "description": str(row.get("description") or ""),
            "cells": {phase: _cell(name=name, phase=phase, row=row) for phase in ruled},
        }
        for extra in ("trust_level", "requires_review", "blocked", "tree_hash", "source"):
            if extra in row:
                item[extra] = row[extra]
        items.append(item)

    matrix = {
        "schema_version": 1,
        "phases": [{"id": p} for p in ruled],
        "sections": [{"id": "skills", "label": "Skills", "items": items}],
    }
    return {"selection": selection, "resolved": resolved_block, "matrix": matrix}
