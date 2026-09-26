"""Skills-union resolution for backlog execution (operator confirmation
2026-07-13 23:09 via continuum c1731: "by default, any agent or entity
working on our framework will receive the coredoc and backlog skill" — the
default must be STRUCTURAL, not an accident of the local install).

Contract (named at c1749, consumed by continuum c1751):
- HALF 1 (here + backlog_execute): at QUEUE-PAYLOAD BUILD, resolve
  [item-class defaults: coredoc, backlog] UNION member skills via skill's
  `select_skills_for_context`, and record the outcome on the payload:
  `skills: {requested, active, resolved_tree_hashes, verdicts, source,
  shelf}`. Recording at payload build (not spawn) is deliberate — the
  payload is the durable request record continuum renders; requested-vs-
  resolved must survive a runner that dies before spawning.
- HALF 2 (backlog_exec_runner): at SPAWN, thread the resolved shelf roots
  into the executor env (ABSTRACTCODE_SKILLS_ROOTS for the abstractcode
  executor; recorded for codex whose skill discovery is its own lane).
  "default-REQUESTED, never trust-bypassed" (skill c1733, continuum c1737):
  a held/blocked verdict spawns WITHOUT that skill and the verdicts field
  says why — never silently absent, never forced past the trust gate.

Degradation: abstractskill absent or the shelf unlocatable records the
REQUEST with a labeled #FALLBACK verdict — the run proceeds (a missing
teaching must not block operator work), and the payload says exactly what
did not ride.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# The operator-confirmed item-class defaults for framework backlog items.
FRAMEWORK_ITEM_DEFAULT_SKILLS = ("coredoc", "backlog")

_CODE_ROOTS_ENV = "ABSTRACTCODE_SKILLS_ROOTS"


def shelf_resolution(repo_root: Optional[Path] = None) -> Dict[str, Any]:
    """The gateway's skills shelf (skills_shelf.resolve_skills_shelf):
    saved setting > legacy environment > the seeded copy in the gateway's
    data folder > a framework checkout next to the backlog folder. The
    setting and the seeded copy are gateway-wide (the root data folder)."""
    from .skills_shelf import resolve_skills_shelf
    from .users import gateway_data_dir_from_env

    return resolve_skills_shelf(gateway_data_dir_from_env(), checkout_root=repo_root)


def _shelf_registry_dir(repo_root: Optional[Path]) -> Tuple[Optional[Path], Optional[str]]:
    """(registry dir | None, why not in plain words)."""
    res = shelf_resolution(repo_root)
    return res["registry"], res["reason"]


def resolve_backlog_skills(
    *,
    member_skills: Optional[List[str]] = None,
    repo_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Resolve the skills union for one backlog execution request.

    Returns the payload `skills` field (always well-formed; degradations are
    labeled verdicts, never absent fields).
    """
    requested: List[str] = list(FRAMEWORK_ITEM_DEFAULT_SKILLS)
    for name in (member_skills or []):
        clean = str(name or "").strip()
        if clean and clean not in requested:
            requested.append(clean)

    out: Dict[str, Any] = {
        "requested": requested,
        "active": [],
        "resolved_tree_hashes": {},
        "verdicts": [],
        "source": "item-class-defaults\u222amember",
        "shelf": None,
    }

    try:
        from abstractskill import TrustRegistry, select_skills_for_context
    except Exception:
        out["verdicts"].append(
            "#FALLBACK abstractskill is not installed on this gateway — the requested "
            "teachings did not resolve; the executor runs without framework-guaranteed skills"
        )
        return out

    registry_dir, shelf_note = _shelf_registry_dir(repo_root)
    if registry_dir is None:
        out["verdicts"].append(
            f"No skill shelf is available ({shelf_note}); the requested skills were not added. "
            "Set the shelf in the console (Settings, Skills shelf) or with `abstractgateway config set skills.shelf <folder>`."
        )
        return out

    skills_root = registry_dir / "skills"
    out["shelf"] = str(skills_root)
    try:
        registry = TrustRegistry.load(
            validations_path=registry_dir / "validations.yaml",
            advisories_path=registry_dir / "advisories.yaml",
            guidance_path=registry_dir / "guidance.yaml",
        )
        selection = select_skills_for_context(registry, skills_root, requested)
    except Exception as e:  # noqa: BLE001 - resolution failure is a labeled verdict
        out["verdicts"].append(f"#FALLBACK skill resolution failed: {e}")
        return out

    def _name(x: Any) -> str:
        # Selection lists carry skill objects (SkillMetadata/LoadedSkill) or
        # plain strings depending on the lane — record NAMES either way (the
        # payload is JSON; objects don't serialize and shouldn't).
        for attr in ("name",):
            v = getattr(x, attr, None)
            if isinstance(v, str) and v.strip():
                return v.strip()
        meta = getattr(x, "metadata", None)
        v = getattr(meta, "name", None) if meta is not None else None
        if isinstance(v, str) and v.strip():
            return v.strip()
        return str(x)

    def _pair(x: Any) -> str:
        # held/blocked are (name, TrustVerdict) PAIRS (selection.py:74-75,
        # skill co-review c1783) — unpack to "name — reason; reason" so the
        # render gets clean words, never a Python repr.
        if isinstance(x, tuple) and len(x) == 2:
            name, verdict = x
            reasons = getattr(verdict, "reasons", None) or []
            tail = "; ".join(str(r) for r in reasons)
            return f"{name} — {tail}" if tail else str(name)
        return _name(x)

    out["active"] = [_name(s) for s in (getattr(selection, "active", []) or [])]
    out["resolved_tree_hashes"] = {
        str(k): str(v) for k, v in (getattr(selection, "resolved_tree_hashes", {}) or {}).items()
    }
    # Held/blocked/missing land as verbatim verdicts — "default-REQUESTED,
    # never trust-bypassed": the render shows why a teaching did not ride.
    for held in (getattr(selection, "held", []) or []):
        out["verdicts"].append(f"held: {_pair(held)}")
    for blocked in (getattr(selection, "blocked", []) or []):
        out["verdicts"].append(f"blocked: {_pair(blocked)}")
    for missing in (getattr(selection, "missing", []) or []):
        out["verdicts"].append(f"missing from shelf: {_name(missing)}")
    for w in (getattr(selection, "warnings", []) or []):
        out["verdicts"].append(str(w))
    return out


def spawn_env_for_skills(skills_field: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """HALF 2: the env vars that make the resolved skills reachable at spawn.

    abstractcode's shipped trust wiring is THREE envs (skill co-review
    c1783): ROOTS alone makes the executor DISCOVER the shelf but evaluate
    it against an EMPTY trust registry — every curated skill lands
    held-as-unverified, the payload says active while the seat cannot
    activate (the fleet empty-registry gap re-manufactured). So the
    registry halves (validations/advisories, derived from the shelf's
    parent) ride beside ROOTS. Codex discovers skills from its own dirs —
    the payload records what rode; forcing its discovery is the executor
    lane's work, not smuggled env."""
    if not isinstance(skills_field, dict):
        return {}
    shelf = str(skills_field.get("shelf") or "").strip()
    active = skills_field.get("active") or []
    if not shelf or not active:
        return {}
    existing = str(os.getenv(_CODE_ROOTS_ENV, "") or "").strip()
    out = {_CODE_ROOTS_ENV: f"{existing}:{shelf}" if existing else shelf}
    registry_dir = Path(shelf).parent
    validations = registry_dir / "validations.yaml"
    advisories = registry_dir / "advisories.yaml"
    if validations.is_file():
        out["ABSTRACTCODE_SKILLS_VALIDATIONS"] = str(validations)
    if advisories.is_file():
        out["ABSTRACTCODE_SKILLS_ADVISORIES"] = str(advisories)
    return out
