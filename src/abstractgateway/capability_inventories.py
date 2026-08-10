"""Skills + MCP inventories for launch surfaces (operator directive
2026-07-15 16:22 via observer c2233: "you must get the skills from
abstractgateway, which gets them from abstractskill ... there should be
something for selecting MCP as well").

SKILLS: the abstractskill library contract is the ONE gate
(decision:workforce-capabilities-homes) — shelf discovery + trust verdicts,
roster row {name, description, trust_level, requires_review, tree_hash,
source}; the word "safe" never renders. This module composes exactly that:
FilesystemSkillLoader discovery over the curated shelf, one structural
inspection per skill (tree hash + has_scripts from one walk), and
evaluate_trust for the explainable verdict. Shelf resolution reuses the
skills-union rule (ABSTRACTGATEWAY_SKILLS_SHELF > the triage repo's
abstractskill/registry) so the picker lists the same shelf the workforce
lane resolves against — one truth, not a second copy.

MCP: v1 is a DECLARED registry — `<data_dir>/config/mcp_servers.json`
(admin-managed file; rows {name, url?, description?, auth_required?,
tags?}). Served fields are declared-only and the response says
`probed: false` honestly: tool counts/connect state require connecting,
which is a later lane (code c2234's tool_count suggestion lands when a
probe lane exists — declared-only must not fake it). MCP GRANTS stay in
the phases config family per the ruled decision; this endpoint is the
inventory half that ruling was waiting on.

Degradations are labeled warnings in the response body — an absent shelf
or registry file lists honestly as empty, never fabricates, never 500s.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MCP_SERVERS_CONFIG_FILENAME = "mcp_servers.json"


def _repo_root_for_shelf(data_dir: Path) -> Optional[Path]:
    """The framework checkout root the shelf lives under — same resolution
    the triage/backlog lane uses (runtime_config stored value, then env)."""
    try:
        from .runtime_config import resolve_triage_repo_root

        raw = resolve_triage_repo_root(Path(data_dir))
    except Exception:
        raw = None
    if not raw:
        raw = (
            os.getenv("ABSTRACT_TRIAGE_REPO_ROOT")
            or os.getenv("ABSTRACTGATEWAY_TRIAGE_REPO_ROOT")
            or ""
        ).strip() or None
    return Path(raw).expanduser().resolve() if raw else None


def skills_inventory(*, data_dir: Path) -> Dict[str, Any]:
    """The gateway's skills inventory, sourced from the abstractskill shelf.

    Returns {"skills": [roster rows], "shelf": path|None, "warnings": [...]}.
    Roster row = the ruled shape + explainability: name, description,
    trust_level, blocked, requires_review, tree_hash, source (with binding
    strength), has_scripts, and the verdict's reasons verbatim."""
    out: Dict[str, Any] = {"skills": [], "shelf": None, "warnings": []}

    try:
        from abstractskill import FilesystemSkillLoader, TrustRegistry, evaluate_trust
        from abstractskill.tree import inspect_skill_dir
    except Exception:
        out["warnings"].append(
            "#FALLBACK abstractskill is not installed on this gateway — no skills inventory"
        )
        return out

    from .skills_union import _shelf_registry_dir

    registry_dir, shelf_note = _shelf_registry_dir(_repo_root_for_shelf(data_dir))
    if registry_dir is None:
        out["warnings"].append(f"#FALLBACK no curated shelf found ({shelf_note})")
        return out

    skills_root = registry_dir / "skills"
    out["shelf"] = str(skills_root)

    try:
        registry = TrustRegistry.load(
            validations_path=registry_dir / "validations.yaml",
            advisories_path=registry_dir / "advisories.yaml",
            guidance_path=registry_dir / "guidance.yaml",
        )
    except Exception as e:
        # Fail CLOSED on trust, open on listing: without a registry every
        # skill evaluates against an EMPTY one (unverified/requires_review),
        # which is the honest posture — never a fabricated trust level.
        from abstractskill import TrustRegistry as _TR

        registry = _TR()
        out["warnings"].append(f"#FALLBACK trust registry failed to load ({e}) — all skills render unverified")

    loader = FilesystemSkillLoader(skills_root)
    for meta in loader.discover(on_warning=out["warnings"].append):
        row: Dict[str, Any] = {
            "name": meta.name,
            "description": meta.description,
        }
        if meta.license:
            row["license"] = meta.license
        if meta.compatibility:
            row["compatibility"] = meta.compatibility

        skill_dir = meta.source_path.parent if meta.source_path is not None else None
        tree_hash: Optional[str] = None
        has_scripts = False
        if skill_dir is not None:
            try:
                inventory = inspect_skill_dir(skill_dir)
                tree_hash = inventory.tree_hash
                has_scripts = bool(inventory.has_scripts)
            except Exception as e:
                out["warnings"].append(f"#FALLBACK could not inspect skill {meta.name!r}: {e}")

        source = None
        try:
            derived = registry.source_for(name=meta.name, tree_hash=tree_hash)
            if derived is not None:
                source = {"source": derived.source, "binding": derived.binding, "ambiguous": derived.ambiguous}
        except Exception:
            source = None

        try:
            verdict = evaluate_trust(
                registry,
                tree_hash=tree_hash,
                name=meta.name,
                source=(source or {}).get("source"),
                has_scripts=has_scripts,
            )
            row.update(
                {
                    "trust_level": verdict.level.value,
                    "blocked": bool(verdict.blocked),
                    "requires_review": bool(verdict.requires_review),
                    "reasons": list(verdict.reasons),
                }
            )
        except Exception as e:
            # Verdict failure fails CLOSED: unverified + review, stated.
            row.update(
                {
                    "trust_level": "unverified",
                    "blocked": False,
                    "requires_review": True,
                    "reasons": [f"#FALLBACK trust evaluation failed: {e}"],
                }
            )
        row["tree_hash"] = tree_hash
        row["has_scripts"] = has_scripts
        row["source"] = source
        out["skills"].append(row)
    return out


def resolve_run_skills(names: List[str], *, data_dir: Path) -> Dict[str, Any]:
    """Resolve a run-start skills selection (card 0087; flow's c2254 transport
    ruling) into the loop contract's two halves:

    - `skills_block`: the rendered index (names + activation lines) for
      `_runtime.skills_block` — agent's named prompt slot, byte-stable per
      run by contract;
    - bookkeeping: requested/active/verdicts, the same default-REQUESTED-
      never-trust-bypassed posture as the workforce lane (held/blocked ride
      as labeled verdicts, never silently absent, never forced).

    ONE GATE: resolution goes through abstractskill's
    `select_skills_for_context` against the SAME shelf the /skills inventory
    and the workforce spawn lane use. Degradations are labeled verdicts —
    a missing shelf must not block a run the caller asked for."""
    requested = []
    for raw in names or []:
        clean = str(raw or "").strip()
        if clean and clean not in requested:
            requested.append(clean)
    out: Dict[str, Any] = {
        "requested": requested,
        "active": [],
        "verdicts": [],
        "skills_block": None,
        "resolved_tree_hashes": {},
    }
    if not requested:
        return out

    try:
        from abstractskill import TrustRegistry, select_skills_for_context
        from abstractskill.prompt import format_available_skills_xml
    except Exception:
        out["verdicts"].append(
            "#FALLBACK abstractskill is not installed on this gateway — the requested "
            "skills did not resolve; the run proceeds without them"
        )
        return out

    from .skills_union import _shelf_registry_dir

    registry_dir, shelf_note = _shelf_registry_dir(_repo_root_for_shelf(data_dir))
    if registry_dir is None:
        out["verdicts"].append(f"#FALLBACK no curated shelf found ({shelf_note}) — requested skills did not resolve")
        return out

    skills_root = registry_dir / "skills"
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

    active = list(getattr(selection, "active", ()) or ())
    # REQUIRES CHECK (abstractskill-0008 consumer half; laurent ruled it
    # active 2026-07-21): a skill declaring metadata.requires_mcp /
    # requires_tools activates ONLY when this gateway can serve them —
    # otherwise it DROPS from active with a labeled verdict naming the
    # missing dependency ("teach-what-is-wired applied to skills"). Never a
    # silent activation, never a run block. An unreadable registry or tool
    # universe SKIPS the check with a #FALLBACK verdict — a broken registry
    # must not silently drop skills.
    requires_map = dict(getattr(selection, "requires", {}) or {})
    if requires_map and active:
        active, unmet = _apply_requires_check(
            active, requires_map, data_dir=data_dir, verdicts=out["verdicts"]
        )
        if unmet:
            out["requires_unmet"] = unmet
        out["requires"] = {
            str(name): {
                "mcp_servers": [str(s) for s in (getattr(req, "mcp_servers", ()) or ())],
                "tools": [str(t) for t in (getattr(req, "tools", ()) or ())],
            }
            for name, req in requires_map.items()
        }
    out["active"] = [str(getattr(m, "name", m)) for m in active]
    out["resolved_tree_hashes"] = {
        str(k): str(v) for k, v in (getattr(selection, "resolved_tree_hashes", {}) or {}).items()
    }
    for held in getattr(selection, "held", ()) or ():
        name, verdict = held if isinstance(held, tuple) and len(held) == 2 else (held, None)
        reasons = "; ".join(str(r) for r in (getattr(verdict, "reasons", None) or []))
        out["verdicts"].append(f"held: {name} — {reasons}" if reasons else f"held: {name}")
    for blocked in getattr(selection, "blocked", ()) or ():
        name, verdict = blocked if isinstance(blocked, tuple) and len(blocked) == 2 else (blocked, None)
        reasons = "; ".join(str(r) for r in (getattr(verdict, "reasons", None) or []))
        out["verdicts"].append(f"blocked: {name} — {reasons}" if reasons else f"blocked: {name}")
    for missing in getattr(selection, "missing", ()) or ():
        out["verdicts"].append(f"missing from shelf: {missing}")
    for w in getattr(selection, "warnings", ()) or ():
        out["verdicts"].append(str(w))

    if active:
        out["skills_block"] = format_available_skills_xml(
            active, descriptions=dict(getattr(selection, "activation_descriptions", {}) or {})
        )
    return out


def _apply_requires_check(
    active: List[Any],
    requires_map: Dict[str, Any],
    *,
    data_dir: Path,
    verdicts: List[str],
) -> tuple:
    """Split `active` into (still_active, unmet) against this gateway's
    declared MCP registry and run-lane tool universe (0008 consumer half).

    Returns (kept_active_metadata, unmet_map) where unmet_map is
    {skill_name: {"mcp_servers": [missing...], "tools": [missing...]}}.
    Check-substrate failures SKIP the corresponding check with a #FALLBACK
    verdict — degraded knowledge must never read as a missing dependency."""
    declared_mcp: Optional[set] = None
    try:
        inv = mcp_servers_inventory(data_dir=data_dir)
        declared_mcp = {str(r.get("name") or "").strip() for r in inv.get("servers") or []}
    except Exception as e:  # noqa: BLE001
        verdicts.append(f"#FALLBACK requires_mcp check skipped (MCP registry unreadable: {e})")

    tool_universe: Optional[set] = None
    if any((getattr(req, "tools", ()) or ()) for req in requires_map.values()):
        try:
            from abstractruntime.integrations.abstractcore.default_tools import list_default_tool_specs

            tool_universe = {
                str(s.get("name") or "").strip()
                for s in list_default_tool_specs()
                if isinstance(s, dict)
            }
        except Exception as e:  # noqa: BLE001
            verdicts.append(f"#FALLBACK requires_tools check skipped (tool universe unavailable: {e})")

    kept: List[Any] = []
    unmet: Dict[str, Dict[str, List[str]]] = {}
    for meta in active:
        name = str(getattr(meta, "name", meta))
        req = requires_map.get(name)
        missing_mcp: List[str] = []
        missing_tools: List[str] = []
        if req is not None:
            if declared_mcp is not None:
                missing_mcp = [
                    s for s in (str(x) for x in (getattr(req, "mcp_servers", ()) or ())) if s and s not in declared_mcp
                ]
            if tool_universe is not None:
                missing_tools = [
                    t for t in (str(x) for x in (getattr(req, "tools", ()) or ())) if t and t not in tool_universe
                ]
        if missing_mcp or missing_tools:
            parts = []
            if missing_mcp:
                # Declared-only registry (no probe lane yet): the honest
                # wording is "not declared", never a fabricated "not
                # reachable" the gateway cannot know.
                parts.append(
                    "MCP server(s) not declared on this gateway: " + ", ".join(sorted(missing_mcp))
                )
            if missing_tools:
                parts.append("tool(s) not in the run-lane universe: " + ", ".join(sorted(missing_tools)))
            verdicts.append(f"requires_unmet: {name} — " + "; ".join(parts))
            unmet[name] = {"mcp_servers": sorted(missing_mcp), "tools": sorted(missing_tools)}
        else:
            kept.append(meta)
    return kept, unmet


def read_skill_body(name: str, *, data_dir: Path, max_chars: int = 16000) -> Dict[str, Any]:
    """The `read_skill` executor half (agent's progressive-disclosure
    contract: the skills_block lists names, this loads one body on demand).

    TRUST RE-CHECK AT READ TIME: the body is re-gated through
    `select_skills_for_context` for exactly this name — a blocked skill's
    body never reaches a model even if a stale block still lists it. Output
    is bounded by max_chars with a labeled #TRUNCATION marker."""
    clean = str(name or "").strip()
    if not clean:
        return {"success": False, "error": "read_skill requires a skill name"}
    resolved = resolve_run_skills([clean], data_dir=data_dir)
    if clean not in (resolved.get("active") or []):
        reasons = "; ".join(resolved.get("verdicts") or []) or "not on the shelf"
        return {"success": False, "error": f"skill {clean!r} is not readable: {reasons}"}
    try:
        from abstractskill import FilesystemSkillLoader

        from .skills_union import _shelf_registry_dir

        registry_dir, _note = _shelf_registry_dir(_repo_root_for_shelf(data_dir))
        loaded = FilesystemSkillLoader(registry_dir / "skills").load(clean)
        body = str(loaded.document.body or "")
    except Exception as e:
        return {"success": False, "error": f"failed to load skill {clean!r}: {e}"}
    # #[WARNING:TRUNCATION] (ADR-0026 §4). Caller-overridable via max_chars,
    # marked in-band, and the marker tells the reader how to get the rest.
    cap = max(1000, int(max_chars or 16000))
    truncated = len(body) > cap
    if truncated:
        body = body[:cap] + "\n\n#TRUNCATION: skill body capped at " + str(cap) + " chars — call read_skill with a larger max_chars for the rest"
    return {"success": True, "name": clean, "body": body, "truncated": truncated}


def mcp_servers_inventory(*, data_dir: Path) -> Dict[str, Any]:
    """The declared MCP server registry (v1: config-file-managed).

    Reads `<data_dir>/config/mcp_servers.json`; rows are served with their
    DECLARED fields only and `probed: false` — connect state / tool counts
    require a probe lane this deliberately does not fake. Malformed rows
    become labeled warnings, never a 500 and never a silent drop."""
    out: Dict[str, Any] = {"servers": [], "source": None, "probed": False, "warnings": []}
    path = Path(data_dir) / "config" / MCP_SERVERS_CONFIG_FILENAME
    if not path.is_file():
        out["warnings"].append(
            f"no MCP server registry declared (create {path} with "
            '{"version": 1, "servers": [{"name": ..., "url": ..., "description": ..., "auth_required": ...}]})'
        )
        return out
    out["source"] = str(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        out["warnings"].append(f"#FALLBACK MCP server registry unreadable: {e}")
        return out
    rows = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        out["warnings"].append('#FALLBACK MCP server registry must be {"version": 1, "servers": [...]}')
        return out
    seen: set[str] = set()
    for i, raw in enumerate(rows):
        if not isinstance(raw, dict):
            out["warnings"].append(f"#FALLBACK servers[{i}] is not an object — skipped")
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            out["warnings"].append(f"#FALLBACK servers[{i}] has no name — skipped")
            continue
        if name in seen:
            out["warnings"].append(f"#FALLBACK duplicate MCP server name {name!r} — later row skipped")
            continue
        seen.add(name)
        row: Dict[str, Any] = {"name": name}
        for key in ("url", "description"):
            value = str(raw.get(key) or "").strip()
            if value:
                row[key] = value
        if "auth_required" in raw:
            row["auth_required"] = bool(raw.get("auth_required"))
        tags = raw.get("tags")
        if isinstance(tags, list):
            clean_tags = [str(t).strip() for t in tags if str(t or "").strip()]
            if clean_tags:
                row["tags"] = clean_tags
        out["servers"].append(row)
    return out
