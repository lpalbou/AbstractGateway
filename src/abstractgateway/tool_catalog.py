"""Full tool catalog for discovery (tool-tiers item H build, cycle 3).

Laurent's complaint, confirmed by the H audit (plans/tool-audit.md): the
email/whatsapp/telegram/agora/persistent-shell toolsets EXIST in code but are
env-gated OFF, so `/discovery/tools` could not see them — tool existence was
invisible, configured by env flags nobody can read from a console. The ruled
fix (adopted room-wide, coder-tui consumer-committed c4555): discovery serves
the FULL catalog; exists-but-not-enabled is a VISIBLE state (`enabled: false`
+ the gate that disables it), never silence.

ONE SOURCE (c4562 seam, closed same-day): runtime shipped
`list_tool_catalog(include_disabled=True)` — the toolset composition owner
enumerating enabled AND disabled toolsets with their real callables. This
module consumes THAT and only falls back to a minimal hand fold on runtimes
too old to have it (labeled #FALLBACK, deleted when the floor moves).

Approval clamp (catalog adversary F3): runtime's approval fold contains
pre-tiers auto rows (send_telegram_message, agora_*) — serving
`approval_default: "auto"` on a DISABLED row would pre-approve a tool the
operator never enabled and undercut the pending send_email re-ruling (c4559
item C). Disabled rows CLAMP to ask here, always.

IMPORT BOUNDARY (backlog 0059, pinned by test_gateway_import_boundary):
this module reaches AbstractCore ONLY through AbstractRuntime facades
(`tool_inventory_facade.core_registry_tool_rows` for registry rows/facts,
`derive_risk_assessment` for the fold). Surfaces with no facade yet
(camera capability facts, capability plugin errors) are getattr-probed on
the runtime module and degrade LABELED until runtime ships them — never a
direct `abstractcore` import (the 0.2.26 regression class).

Every degradation is labeled, never silent.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Tuple

logger = logging.getLogger(__name__)


def _spec_extractors() -> Tuple[Callable, Callable]:
    from abstractruntime.integrations.abstractcore.default_tools import _normalize_tool_spec, _tool_spec

    return _tool_spec, _normalize_tool_spec


def _rows_from_callables(tools: List[Any], *, toolset: str, gate: str, why: str,
                         warnings: List[str]) -> List[Dict[str, Any]]:
    """Real specs from the real callables (never fabricated): the same
    extraction the enabled lane runs, stamped disabled + the gate."""
    try:
        tool_spec, normalize = _spec_extractors()
    except Exception as e:  # noqa: BLE001 - runtime too old for the extractors
        warnings.append(f"#FALLBACK catalog spec extractors unavailable ({type(e).__name__}); {toolset} rows omitted")
        return []
    out: List[Dict[str, Any]] = []
    for fn in tools:
        try:
            spec = normalize(tool_spec(fn))
            name = str(spec.get("name") or "").strip()
            if not name:
                continue
            spec["toolset"] = toolset
            spec["enabled"] = False
            spec["enable_gate"] = gate
            spec["why_disabled"] = why
            out.append(spec)
        except Exception as e:  # noqa: BLE001 - one broken callable must not hide the rest
            warnings.append(f"#FALLBACK catalog spec extraction failed for one {toolset} tool: {type(e).__name__}")
    return out


_WHY_BY_TOOLSET = {
    "comms": "registered but disabled on this gateway — enable via the named gate (migrating to console config per dm#177)",
    "agora": "agora hub tools require explicit intent AND a credential — never a lucky inherited key (c4211)",
    "shell": "persistent shell sessions escape per-call cwd confinement; explicit opt-in only (backlog 0220)",
}

# Comms KIND membership (email/whatsapp/telegram sub-toolsets). Runtime's
# composition is the truth; this map exists only because `list_tool_catalog`
# is TOOLSET-granular (the partial-enablement remainder, filed to runtime
# 2026-07-23) and the boundary contract forbids importing the callables from
# core here. DRIFT-PINNED: a test enables all comms kinds and asserts this
# map equals runtime's composed toolset names — never sync-by-vigilance.
_COMMS_KIND_TOOLS: Dict[str, Tuple[str, ...]] = {
    "email": ("list_email_accounts", "send_email", "list_emails", "read_email"),
    "whatsapp": ("send_whatsapp_message", "list_whatsapp_messages", "read_whatsapp_message"),
    "telegram": ("send_telegram_message", "send_telegram_artifact"),
}


def _registry_rows_by_name(warnings: List[str]) -> Dict[str, Dict[str, Any]]:
    """Core's registry rows by name, through the RUNTIME FACADE (the
    boundary-compliant read: name/description/parameters/facts + the risk
    trio, core-authored, spec-grade). {} + a labeled warning when the
    facade or core is absent."""
    try:
        from abstractruntime.integrations.abstractcore.tool_inventory_facade import core_registry_tool_rows

        return {str(r.get("name") or ""): r for r in core_registry_tool_rows() if isinstance(r, dict)}
    except Exception as e:  # noqa: BLE001 - older runtime / core absent
        warnings.append(f"#FALLBACK core registry rows unavailable via runtime facade ({type(e).__name__})")
        return {}


def _rows_from_registry(names: Tuple[str, ...] | List[str], registry: Dict[str, Dict[str, Any]], *,
                        toolset: str, gate: str, why: str, warnings: List[str]) -> List[Dict[str, Any]]:
    """Disabled-lane rows built from REGISTRY rows (no callable import):
    the full core-authored row (facts + risk trio included) stamped with
    the disabled-lane fields. A name the registry does not carry degrades
    to a labeled name-only row — visible, never silent."""
    out: List[Dict[str, Any]] = []
    for name in names:
        src = registry.get(name)
        if src:
            row = dict(src)
        else:
            row = {"name": name}
            warnings.append(f"#FALLBACK {toolset} tool {name!r} absent from the core registry; serving a name-only row")
        row["toolset"] = toolset
        row["enabled"] = False
        row["enable_gate"] = gate
        row["why_disabled"] = why
        out.append(row)
    return out


def disabled_toolset_rows() -> Tuple[List[Dict[str, Any]], List[str]]:
    """Tool rows for every toolset runtime knows but has NOT enabled.

    Primary path: runtime's `list_tool_catalog(include_disabled=True)` — the
    ONE composition source (enabled-state and membership both theirs; a
    toolset is in the enabled lane or here, never both, never neither,
    because both lanes read the same enumeration). Older runtimes without
    the catalog degrade to a labeled minimal fold.
    """
    warnings: List[str] = []
    try:
        from abstractruntime.integrations.abstractcore.default_tools import list_tool_catalog
    except Exception:
        return _legacy_disabled_rows()

    rows: List[Dict[str, Any]] = []
    try:
        catalog = list_tool_catalog(include_disabled=True)
    except Exception as e:  # noqa: BLE001 - a broken catalog never breaks discovery
        warnings.append(f"#FALLBACK runtime tool catalog failed ({type(e).__name__}); serving enabled rows only")
        return rows, warnings
    for ts in catalog or []:
        if not isinstance(ts, dict) or ts.get("enabled") is not False:
            continue  # enabled toolsets serve through list_default_tool_specs
        ts_id = str(ts.get("id") or "other")
        gate = str(ts.get("gate") or "")
        note = str(ts.get("note") or "")
        if note:
            warnings.append(f"{ts_id}: {note}")
        rows.extend(
            _rows_from_callables(
                list(ts.get("tools") or []),
                toolset=ts_id,
                gate=gate,
                why=_WHY_BY_TOOLSET.get(ts_id, f"registered but disabled on this gateway (gate: {gate})"),
                warnings=warnings,
            )
        )
    rows.extend(_comms_partial_remainder(existing=rows, warnings=warnings))
    return rows, warnings


def _comms_partial_remainder(*, existing: List[Dict[str, Any]], warnings: List[str]) -> List[Dict[str, Any]]:
    """BELT for runtime's catalog granularity gap (found by this module's
    own pins, filed to runtime 2026-07-23): `list_tool_catalog` treats comms
    as ONE toolset — with only email enabled, the comms row reads
    enabled:true containing the email tools, and the whatsapp/telegram
    members appear in NEITHER lane (the exact "never neither" break the
    catalog exists to kill). Until runtime serves sub-toolset granularity,
    this remainder re-adds the DISABLED comms kinds from the same per-kind
    predicates the composition itself consults. No-op when comms is fully
    disabled (the catalog row already carried all nine) or fully enabled."""
    try:
        from abstractruntime.integrations.abstractcore import default_tools as dt
    except Exception:  # noqa: BLE001
        return []
    seen = {str(r.get("name")) for r in existing}
    registry = _registry_rows_by_name(warnings)
    out: List[Dict[str, Any]] = []

    def _kind(kind: str, predicate_name: str, gate: str) -> None:
        predicate = getattr(dt, predicate_name, None)
        try:
            enabled = bool(predicate()) if callable(predicate) else False
        except Exception:  # noqa: BLE001
            enabled = False
        if enabled:
            return
        fresh = tuple(n for n in _COMMS_KIND_TOOLS[kind] if n not in seen)
        if not fresh:
            return
        # Rows from the REGISTRY FACADE (boundary contract: no callable
        # import here) — full core-authored rows, disabled-lane stamped.
        out.extend(
            _rows_from_registry(
                fresh,
                registry,
                toolset="comms",
                gate=gate,
                why=_WHY_BY_TOOLSET["comms"],
                warnings=warnings,
            )
        )

    _kind("email", "email_tools_enabled", "ABSTRACT_ENABLE_COMMS_TOOLS or ABSTRACT_ENABLE_EMAIL_TOOLS")
    _kind("whatsapp", "whatsapp_tools_enabled", "ABSTRACT_ENABLE_COMMS_TOOLS or ABSTRACT_ENABLE_WHATSAPP_TOOLS")
    _kind("telegram", "telegram_tools_enabled", "ABSTRACT_ENABLE_COMMS_TOOLS or ABSTRACT_ENABLE_TELEGRAM_TOOLS")
    return out


def _legacy_disabled_rows() -> Tuple[List[Dict[str, Any]], List[str]]:
    """#FALLBACK for runtimes predating list_tool_catalog: a minimal comms
    fold so the operator's named suspects (email/telegram) stay visible even
    on the older floor. Rows come from the registry facade when THAT exists;
    a runtime old enough to lack both serves labeled name-only rows —
    visibility is the promise, spec richness is not. Deleted when the
    runtime floor moves."""
    warnings: List[str] = ["#FALLBACK runtime lacks list_tool_catalog; serving a minimal legacy comms fold"]
    rows: List[Dict[str, Any]] = []
    try:
        from abstractruntime.integrations.abstractcore import default_tools as dt
    except Exception as e:  # noqa: BLE001
        return [], [f"#FALLBACK runtime default_tools unavailable ({type(e).__name__}); catalog serves enabled rows only"]
    try:
        enabled = bool(dt.email_tools_enabled()) if callable(getattr(dt, "email_tools_enabled", None)) else False
    except Exception:  # noqa: BLE001
        enabled = False
    if not enabled:
        rows.extend(
            _rows_from_registry(
                _COMMS_KIND_TOOLS["email"],
                _registry_rows_by_name(warnings),
                toolset="comms",
                gate="ABSTRACT_ENABLE_COMMS_TOOLS or ABSTRACT_ENABLE_EMAIL_TOOLS",
                why=_WHY_BY_TOOLSET["comms"],
                warnings=warnings,
            )
        )
    return rows, warnings


def join_registry_facts(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """NAME-JOIN core's declared risk FACTS onto model-lane spec rows BEFORE
    annotation (observer c4647, the all-50-rows-destroy/unvetted live gap):
    `list_default_tool_specs` serves prompt-lane specs that carry NO fact
    fields, so runtime's fold derived factless -> rank 4/unvetted for every
    discovery row — while core's inventory rows (c4577: "consume
    builtin_tool_inventory_as_dicts for builtin rows, capability_tool_facts
    for camera") carried the honest facts all along. This is exactly the
    consumption core prescribed; joining SERVED fact fields by name is
    derive-never-copy compliant (facts stay core-authored; the fold stays
    runtime's — it just finally receives its input). Rows with no registry
    match keep no facts and derive unvetted (agora/shell/open_attachment —
    honest until their facts are declared). In place; returns items."""
    fact_keys = (
        "mutating",
        "remote_write_capable",
        "network_egress",
        "captures_environment",
        "standing_effect",
        "destructive_capable",
        "comms_send",
        "model_controlled_destination",
    )
    facts_by_name: Dict[str, Dict[str, Any]] = {}
    try:
        from abstractruntime.integrations.abstractcore.tool_inventory_facade import core_registry_tool_rows

        for row in core_registry_tool_rows() or []:
            name = str(row.get("name") or "")
            if name:
                facts_by_name[name] = row
    except Exception:  # noqa: BLE001 - older runtime/core: builtin facts unavailable
        pass
    # Camera capability facts: NO runtime facade yet (asked of runtime
    # 2026-07-23, the import-boundary fix wave) — getattr-probe so the join
    # lights up the day runtime ships `capability_tool_facts`, and camera
    # rows honestly derive unvetted until then (render-when-present).
    try:
        from abstractruntime.integrations.abstractcore import tool_inventory_facade as _facade

        cap_facts = getattr(_facade, "capability_tool_facts", None)
        if callable(cap_facts):
            cam = cap_facts("camera")
            if isinstance(cam, dict):
                for name, facts in cam.items():
                    if isinstance(facts, dict):
                        facts_by_name.setdefault(str(name), facts)
    except Exception:  # noqa: BLE001 - camera plugin absent / older runtime
        pass
    if not facts_by_name:
        return items
    for row in items:
        try:
            name = str(row.get("name") or "")
            src = facts_by_name.get(name)
            if not src:
                continue
            for k in fact_keys:
                if k in src and k not in row:
                    row[k] = src[k]
        except Exception:  # noqa: BLE001
            continue
    return items


def plugin_error_warnings() -> List[str]:
    """Capability-plugin load failures, surfaced onto the discovery response
    (camera c4634: a plugin whose register() failed at entry-point load —
    e.g. the boot-time mid-rewrite race — lands in core's
    shared_capability_registry().status()['plugin_errors'], PROCESS-INTERNAL;
    the catalog lane cannot see it because IT never runs the failing import.
    That is exists-but-not-surfaced-and-SILENT, the exact class this catalog
    kills — so the registry's own error record rides catalog_warnings).

    BOUNDARY NOTE: core's `shared_capability_registry` has NO runtime facade
    yet (asked of runtime 2026-07-23) — getattr-probed so this lights up the
    day runtime exports `capability_plugin_errors`; until then the general
    plugin-error surfacing degrades to [] (runtime's own catalog notes still
    carry the camera-specific error through the disabled-row lane)."""
    try:
        from abstractruntime.integrations.abstractcore import default_tools as _dt

        probe = getattr(_dt, "capability_plugin_errors", None)
        if not callable(probe):
            return []
        errors = probe()
        out: List[str] = []
        if isinstance(errors, dict):
            for plugin, err in errors.items():
                out.append(f"#FALLBACK capability plugin {plugin!r} failed to load: {err} — its tools are absent from this catalog (bounce after the tree is quiescent)")
        elif isinstance(errors, list):
            for err in errors:
                if isinstance(err, dict):
                    out.append(f"#FALLBACK capability plugin {err.get('name')!r} failed to load: {err.get('error')} — its tools are absent from this catalog")
                else:
                    out.append(f"#FALLBACK capability plugin load error: {err} — its tools are absent from this catalog")
        return out
    except Exception:  # noqa: BLE001 - older runtime / no registry: nothing to surface
        return []


def clamp_disabled_approval(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """approval_default on a DISABLED row is always `ask` (adversary F3):
    runtime's approval fold predates the tiers work and carries auto rows
    (telegram, agora_*) — a client unioning approval defaults must never
    pre-approve a tool the operator has not even enabled. Factless disabled
    rows also gain the UNVETTED-top risk stamp via core's hosted fold
    (derive_risk(None) -> rank 4 / presentation unvetted — the ruled fail
    direction; core c4577's ask for the enabled:false catalog). In place;
    returns items for call-chaining."""
    deriver = None
    try:
        # RUNTIME FACADE (boundary contract): derive_risk_assessment({})
        # delegates to core's hosted derive_risk(None) when present and the
        # runtime seed otherwise — factless -> rank 4 / unvetted either way,
        # served in the ruled wire shape (dict of risk_* keys).
        from abstractruntime.integrations.abstractcore.tool_inventory_facade import derive_risk_assessment

        deriver = derive_risk_assessment
    except Exception:  # noqa: BLE001 - older runtime: rows stay unstamped (render-when-present)
        pass
    for row in items:
        try:
            if row.get("enabled") is not False:
                continue
            if row.get("approval_default") == "auto":
                row["approval_default"] = "ask"
                row.setdefault("approval_note", "clamped to ask: tool is disabled on this gateway")
            if deriver is not None and row.get("risk_tier") is None:
                # THE RULED WIRE SHAPE (semantics c4589 -> my vote c4592 ->
                # runtime flip c4599, all same-morning): risk_tier = the band
                # WORD (identity/teaching), risk_rank = the INTEGER (ordinal;
                # ceilings compare against rank), presentation always present.
                assessment = deriver({})  # factless row -> rank 4 / unvetted
                row["risk_tier"] = str(assessment.get("risk_tier", "destroy"))
                row["risk_rank"] = int(assessment.get("risk_rank", 4))
                row["risk_presentation"] = str(assessment.get("risk_presentation", "unvetted"))
                row["risk_mapping_version"] = assessment.get("risk_mapping_version")
            elif row.get("risk_rank") == 4 and not row.get("risk_presentation"):
                # Belt for annotator arms predating the always-emit-presentation
                # fix: a factless top rank must say UNVETTED, never render as
                # 'destructive' (the ruled distinction; core c4577).
                row["risk_presentation"] = "unvetted"
        except Exception:  # noqa: BLE001
            continue
    return items
