"""Entity lifecycle HTTP surface (a2a 0004: the gateway owns entity lifecycle).

Routes ride the `/api/gateway/...` prefix so the gateway security middleware
protects them exactly like every other control-plane endpoint (writes require
auth when auth is enabled). Multi-user gateways resolve a per-principal data
dir through `get_gateway_service()`, so entity homes are principal-scoped for
free.

STRUCTURAL: there is no DELETE route in this router and no purge parameter on
any request model. The book cannot be deleted because nothing here (or below
here) knows how.
"""

from __future__ import annotations

import datetime as _datetime
import json
import logging
import secrets
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..config import entity_iterations_ceiling
from ..entities import EntityRegistry, entity_slug
from ..entity_seat import (
    cancel_run_tree,
    door_decision,
    normalize_caller_kind,
    record_seat,
    seat_occupancy,
)
from ..service import get_gateway_service

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()

# The entity TTS twins reuse the generic route's request model so the two
# lanes cannot drift (module-level import is safe: gateway.py's entities
# imports are all function-level, so no import cycle forms).
from .gateway import VoiceTTSRequest as GatewayVoiceTTSRequest

router = APIRouter(prefix="/gateway/entities", tags=["entities"])


def _registry() -> EntityRegistry:
    svc = get_gateway_service()
    registry = getattr(svc, "entity_registry", None)
    if isinstance(registry, EntityRegistry):
        return registry
    return EntityRegistry(data_dir=svc.config.data_dir)


def _visit_refused_response(e: Any):
    """VisitRefused -> the ADDITIVE structured error shape (coder-tui c4307
    ask 3): `detail` stays the human STRING it always was (existing
    consumers byte-unaffected — a dict detail would change its type under
    them), and the stable machine `code` rides as a SIBLING body key plus
    an X-Gateway-Error-Code header. Codes are a closed vocabulary owned by
    entity_visits.VisitRefused raise sites; absent code = plain shape."""
    from fastapi.responses import JSONResponse

    status = int(getattr(e, "status", 500))
    detail = str(getattr(e, "detail", e))
    code = str(getattr(e, "code", "") or "").strip()
    body: Dict[str, Any] = {"detail": detail}
    headers = None
    if code:
        body["code"] = code
        headers = {"X-Gateway-Error-Code": code}
    return JSONResponse(status_code=status, content=body, headers=headers)


@router.post("/auth/probe")
async def probe_operator_auth() -> Dict[str, Any]:
    """The observer's control-strip gate (a2a 0007/0008: "controls appear
    only when the gateway confirms operator auth — the view never offers
    what the door would refuse").

    Deliberately a POST: the dev posture (`ABSTRACTGATEWAY_DEV_READ_NO_AUTH`)
    can exempt loopback GETs from auth, so only a WRITE-classed probe
    answers the question the control strip is actually asking — "would the
    state/chat/diary doors accept me?". Reaching this handler at all means
    the write middleware accepted the caller; the body just names who.
    """
    from ..security.principal import current_gateway_principal, local_admin_principal

    principal = current_gateway_principal()
    if principal is None:
        # Auth disabled entirely (dev gateway): the local caller IS the operator.
        principal = local_admin_principal()
    return {
        "operator": True,
        "user_id": principal.user_id,
        "admin": bool(principal.is_admin()),
    }


class CreateEntityRequest(BaseModel):
    name: str = Field(..., description='Entity name (e.g. "Castor")')
    spark: Optional[Dict[str, Any]] = Field(
        default=None, description="Spark document as a mapping (default: framework template with the name filled)"
    )
    spark_text: Optional[str] = Field(
        default=None, description="Spark document as raw YAML text (stored byte-verbatim as the attested seed)"
    )
    framework: bool = Field(
        default=True,
        description="Framework lint (requires the shared_vulnerability core value); disabling is a deliberate operator override",
    )
    embedding_model: Optional[str] = Field(
        default=None,
        description="Embedder identity as an explicit BIRTH choice (plan item 3 / M1 pin); "
        "None = pin the door's resolved embedder identity",
    )
    embedding_dimension: Optional[int] = Field(
        default=None, ge=1,
        description="Embedding dimension for the pin; None = probed from the resolved embedder "
        "(memory locks it at first write when unknowable)",
    )
    skills: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Optional skills selection at BIRTH (c2838: a birth can carry teaching from "
        "day one): [{name, phases?}] — same shape as PUT /{name}/skills; validated before "
        "anything is created",
    )


@router.post("", status_code=201)
def create_entity(req: CreateEntityRequest) -> Dict[str, Any]:
    """Create an entity home: lint -> store the spark verbatim -> engram ->
    manifest. Idempotent for the same spark (`created=false`); a CHANGED
    document is refused with the engine's human-written error (409)."""
    from ..entities import EntityQuotaExceeded

    # Birth skills validate BEFORE anything is created — a typo'd phase must
    # not mint a home and then half-fail. DEFAULT (laurent seq 156, skill
    # c161): a request that names NO skills is born selecting
    # entity-self-knowledge in every phase — every entity must know how its
    # memory works. An explicit selection (incl. an empty list) is honored
    # verbatim; the default only fills the absence.
    from ..entity_skills import DEFAULT_ENTITY_SKILL, validate_skills_selection

    birth_skills: Optional[List[Dict[str, Any]]] = None
    skills_request = req.skills if req.skills is not None else [{"name": DEFAULT_ENTITY_SKILL}]
    try:
        birth_skills = validate_skills_selection(skills_request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"skills selection invalid: {e}")

    try:
        result = _registry().create(
            name=req.name,
            spark=req.spark,
            spark_text=req.spark_text,
            framework=bool(req.framework),
            embedding_model=req.embedding_model,
            embedding_dimension=req.embedding_dimension,
        )
    except EntityQuotaExceeded as e:
        # F2: entities are permanent + door-global; the per-root quota bounds
        # user-level creation. 429 = "too many", the honest status.
        raise HTTPException(status_code=429, detail=str(e))
    except ValueError as e:
        # Lint errors, name mismatches, and spark-drift refusals are written
        # for humans — surface them verbatim. Drift/conflict reads as 409.
        detail = str(e)
        status = 409 if "DIFFERENT" in detail or "already" in detail.lower() else 400
        raise HTTPException(status_code=status, detail=detail)
    out = result.to_dict()

    # Birth teaching: selection + marker land AFTER the home exists. The
    # created entity STANDS even if this half fails — the failure surfaces
    # as a labeled warning (an operator can re-PUT), never a rolled-back or
    # half-reported birth. An EMPTY selection writes NOTHING (no skills.yaml
    # — the honest "no selection" state, exists:false; an explicit skills:[]
    # opts out, it does not mint an empty-list file).
    if birth_skills:
        registry = _registry()
        try:
            manifest = registry.manifest_for(req.name)
            home_dir = registry.entities_dir / manifest.slug
            actor = _task_actor()
            from ..entity_replay import record_host_marker

            home = registry.get_home(manifest.slug)
            record_host_marker(
                entities_dir=registry.entities_dir,
                slug=manifest.slug,
                entity_id=manifest.entity_id,
                kind="skills_selection_changed",
                journal_seq=int(home.memory.current_seq()),
                details={
                    "channel": "operator",
                    "by": actor,
                    "old": [],
                    "new": [{"name": s["name"], **({"phases": s["phases"]} if s.get("phases") else {})} for s in birth_skills],
                    "at_birth": True,
                },
            )
            from ..entity_skills import write_skills_selection

            write_skills_selection(home_dir, birth_skills)
            out["skills_selected"] = [s["name"] for s in birth_skills]
        except Exception as e:  # noqa: BLE001
            out["skills_warning"] = f"#FALLBACK birth skills selection NOT recorded: {e} — re-PUT /entities/{req.name}/skills"

    # Install the capability map at birth so the entity is BORN knowing how
    # its memory works (laurent seq 156; skill c161's install half): the map
    # is entity-self-knowledge's reference, resolved from the shelf and
    # written beside spark.yaml, marker-first. Best-effort — the entity
    # STANDS even if the shelf is unreachable (labeled warning; an operator
    # re-PUTs /capability-map). Only installs when the default skill is
    # selected (an operator who explicitly deselected it is honored).
    selected_names = {s["name"] for s in (birth_skills or [])}
    if DEFAULT_ENTITY_SKILL in selected_names:
        from ..entity_skills import resolve_default_capability_map

        registry = _registry()
        map_text = resolve_default_capability_map(registry.data_dir)
        if map_text is None:
            out["capability_map_warning"] = (
                "#FALLBACK capability map NOT installed at birth: the abstractskill shelf "
                f"could not be resolved — re-PUT /entities/{req.name}/capability-map"
            )
        else:
            try:
                manifest = registry.manifest_for(req.name)
                home_dir = registry.entities_dir / manifest.slug
                home = registry.get_home(manifest.slug)
                from ..entity_replay import record_host_marker

                import hashlib as _hashlib

                sha = _hashlib.sha256(map_text.encode("utf-8")).hexdigest()
                record_host_marker(
                    entities_dir=registry.entities_dir,
                    slug=manifest.slug,
                    entity_id=manifest.entity_id,
                    kind="capability_map_changed",
                    journal_seq=int(home.memory.current_seq()),
                    details={"channel": "operator", "by": _task_actor(),
                             "sha256": sha, "at_birth": True},
                )
                (home_dir / "capability_map.md").write_text(map_text, encoding="utf-8")
                out["capability_map_installed"] = sha[:16]
            except Exception as e:  # noqa: BLE001
                out["capability_map_warning"] = f"#FALLBACK capability map NOT installed at birth: {e} — re-PUT /entities/{req.name}/capability-map"
    return out


@router.post("/{name}/validate")
def validate_entity(name: str, req: CreateEntityRequest) -> Dict[str, Any]:
    """DRY-RUN the create pre-checks — lint + name resolution + spark-drift —
    WITHOUT creating anything (plan (b) P0-2). The console modal calls this
    before the IRREVERSIBLE POST that burns the name for life (no DELETE,
    spark v1-for-life): a green result means create will not refuse for lint,
    name-mismatch, or drift reasons. `name` in the path is authoritative; a
    body `name` mismatch is reported as a lint error, never silently
    overridden. Read-only (reads an existing home's attested spark to report
    the drift verdict; writes nothing)."""
    body_name = str(getattr(req, "name", "") or "").strip()
    if body_name and entity_slug(body_name) != entity_slug(name):
        return {
            "ok": False,
            "errors": [f"path name {name!r} does not match body name {body_name!r}"],
            "warnings": [],
            "name": name,
            "slug": entity_slug(name),
            "exists": False,
            "would_conflict": False,
        }
    result = _registry().validate(
        name=name,
        spark=req.spark,
        spark_text=req.spark_text,
        framework=bool(req.framework),
    )
    # EMBEDDING BIRTH-CHOICE mismatch (adversary P0): _birth_embedding_pin
    # REFUSES a birth embedder the door cannot serve — but it fires AFTER
    # the spark + manifest are written, so a bad choice would burn the
    # permanent name then 400. The dry-run's contract is "green = create
    # will not refuse", so the check must live here too. A choice that
    # equals the door's resolved embedder (or the door resolves none) is
    # safe; a differing one is the refusal, surfaced pre-confirm.
    choice = str(getattr(req, "embedding_model", "") or "").strip()
    if choice and isinstance(result, dict) and result.get("ok"):
        try:
            resolved = None
            reg = _registry()
            embedder = reg._resolve_embedder()  # the door's one embedder (may be None)
            if embedder is not None:
                for attr in ("model", "model_id"):
                    v = getattr(embedder, attr, None)
                    if isinstance(v, str) and v.strip():
                        resolved = v.strip()
                        break
            if resolved and choice != resolved:
                result = dict(result)
                result["ok"] = False
                result["errors"] = list(result.get("errors") or []) + [
                    f"embedding birth choice {choice!r} does not match the door's resolved embedder "
                    f"{resolved!r} — a home pinned to a model its door cannot serve would refuse every "
                    "vector open. Set the gateway embedding route to this model first (Multimodal "
                    "Capabilities), or leave the embedding on 'Gateway default'."
                ]
        except Exception as e:  # noqa: BLE001 - the embedder probe must not break the dry-run
            result = dict(result)
            result.setdefault("warnings", [])
            result["warnings"] = list(result["warnings"]) + [f"#FALLBACK embedding-choice pre-check unavailable: {e}"]
    return result


def _operator_overlay_path() -> Any:
    """The operator's dial overlay (v12 P0-3 shape): tunables ONLY, stored
    beside — never inside — the structural graph. The SOURCE of every
    modulation."""
    return _registry().data_dir / "config" / "entity_phases_overlay.json"


def _effective_spec_file_path() -> Any:
    """The DERIVED effective spec (structural + merged tunables), atomically
    rewritten on every PUT — the FILE mechanism detached loops read (runtime
    c351 points its env hook here; no HTTP auth dependency)."""
    return _registry().data_dir / "config" / "entity_phases.json"


def _packaged_spec_raw() -> str:
    from importlib import resources as _resources

    return (_resources.files("abstractgateway") / "assets" / "entity_phases.json").read_text(encoding="utf-8")


def _packaged_cognition_raw() -> str:
    from importlib import resources as _resources

    return (_resources.files("abstractgateway") / "assets" / "cognition_graph.json").read_text(encoding="utf-8")


def _read_overlay() -> Tuple[Dict[str, Any], Optional[str]]:
    """(overlay, warning): the stored overlay ({edit_seq, edited_by/at,
    reason, tunables}) or {}, plus a LOUD warning when the file exists but
    is unreadable (dm#112 G3: a corrupt overlay must never silently read as
    empty — dials fall to structural seeds VISIBLY).

    LEGACY MIGRATION (the one-morning whole-spec shape, pre-v12): if only
    the old whole-spec operator file exists, its tunables are lifted into
    an overlay once (edit_seq = its rev) so no operator edit is lost."""
    import json as _json

    path = _operator_overlay_path()
    if path.is_file():
        try:
            data = _json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError(f"overlay root must be an object, got {type(data).__name__}")
            return data, None
        except Exception as e:  # noqa: BLE001
            # LOUD degrade (dm#112 edit-adversary risk 5 / entity G3): a
            # corrupt overlay silently reading as {} is tolerable for dials
            # today but becomes a silent behavior FLIP the day toggles ride
            # this file (personal_cycle.enabled already does). The dials
            # fall back to structural seeds — visibly, never quietly.
            import logging

            warn = (
                f"#FALLBACK operator tunables overlay UNREADABLE ({path.name}: {e}) — "
                "serving structural seed dials until the next successful PUT rewrites it"
            )
            logging.getLogger(__name__).error(warn)
            return {}, warn
    legacy = _effective_spec_file_path()
    if legacy.is_file():
        try:
            old = _json.loads(legacy.read_text(encoding="utf-8"))
            meta = old.get("_operator") or {}
            if isinstance(old.get("tunables"), dict) and meta:
                return {
                    "edit_seq": int(meta.get("rev") or 1),
                    "edited_by": meta.get("edited_by"),
                    "edited_at": meta.get("edited_at"),
                    "reason": meta.get("reason"),
                    "tunables": old["tunables"],
                    "migrated_from": "pre-v12 whole-spec file",
                }, None
        except Exception:  # noqa: BLE001
            pass
    return {}, None


def _deep_merge_numbers(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for key, value in (patch or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge_numbers(out[key], value)
        else:
            out[key] = value
    return out


def _effective_tunables(structural: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    base = dict(structural.get("tunables") or {})
    patch = dict(overlay.get("tunables") or {})
    # tunables_meta and $comment are the STRUCTURAL pen's — an overlay can
    # never rewrite the per-dial law or the prose.
    patch.pop("tunables_meta", None)
    patch.pop("$comment", None)
    return _deep_merge_numbers(base, patch)


def _overlay_edge_ops(overlay: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The stored structural edge ops (graph.edge_ops), shape-filtered."""
    graph = overlay.get("graph")
    ops = graph.get("edge_ops") if isinstance(graph, dict) else None
    if not isinstance(ops, list):
        return []
    return [op for op in ops if isinstance(op, dict)]


def _derive_effective_doc(
    structural: Dict[str, Any], raw_sha: str, overlay: Dict[str, Any]
) -> Tuple[Dict[str, Any], Optional[str]]:
    """ONE derivation point for the effective spec doc (PUT + reconcile +
    boot all call this — the structural-edit build's 'effective graph
    derived at ONE gateway point', c4859): structural spec + merged
    tunables + (when the overlay carries graph.edge_ops) merged
    transitions AND the graph block itself (runtime's interpreter applies
    ops idempotently over the resolved doc, c4865 — overlay-only and
    merged files read identically; belt-and-belt).

    `structural_sha256` rides TOP-LEVEL: runtime's H2 handshake input —
    the sha of the vendored structural artifact these ops were validated
    against. A consumer whose own vendored spec differs falls back to
    dials-only rather than applying ops against a graph they were never
    checked on.

    Returns (doc, degrade_warning). Stored ops that no longer validate
    against the CURRENT structural artifact (re-vendor drift) degrade to
    a dials-only effective file LOUDLY — a detached loop must never read
    an incoherent graph; the overlay is untouched, nothing lost."""
    from ..phase_edge_ops import compute_effective_transitions, validate_edge_ops

    effective = dict(structural)
    effective["tunables"] = _effective_tunables(structural, overlay)
    effective["structural_sha256"] = raw_sha
    degrade: Optional[str] = None
    ops = _overlay_edge_ops(overlay)
    if ops:
        refusal = validate_edge_ops(structural, ops)
        if refusal is None:
            effective["transitions"] = compute_effective_transitions(structural, ops)
            effective["graph"] = {"edge_ops": ops}
        else:
            degrade = (
                f"#FALLBACK stored graph.edge_ops no longer valid against the current structural spec "
                f"({refusal.code}: {refusal.detail}) — the effective file serves the structural graph + dials only; "
                f"re-read GET /spec/phases and re-apply graph.edge_ops (the overlay is untouched, nothing lost)"
            )
    effective["_operator"] = {
        "edit_seq": int(overlay.get("edit_seq") or 0),
        "edited_by": overlay.get("edited_by"),
        "edited_at": overlay.get("edited_at"),
        "derived": True,
    }
    return effective, degrade


def _write_effective_spec_file(
    structural: Dict[str, Any], raw_sha: str, overlay: Dict[str, Any], eff_path: Optional[Any] = None
) -> Tuple[Any, Optional[str]]:
    """Atomically (re)write the derived effective file loops read."""
    import json as _json

    doc, degrade = _derive_effective_doc(structural, raw_sha, overlay)
    if eff_path is None:
        eff_path = _effective_spec_file_path()
    eff_path.parent.mkdir(parents=True, exist_ok=True)
    eff_tmp = eff_path.with_suffix(f".tmp.{int(overlay.get('edit_seq') or 0)}")
    eff_tmp.write_text(_json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    eff_tmp.replace(eff_path)
    return eff_path, degrade


def reconcile_effective_spec_file(data_dir: Optional[Any] = None) -> Optional[str]:
    """CRASH-WINDOW + VENDOR-DRIFT reconcile (boot + GET, c4859/c4871): the
    overlay and the derived effective file are two separate atomic writes —
    a crash between them, OR a re-vendor changing the structural artifact
    under a standing overlay, leaves the effective file STALE against what
    a PUT would derive today. Freshness = the effective file's
    (_operator.edit_seq, structural_sha256) both match the overlay + the
    current vendored sha; anything else re-derives through the ONE
    derivation point. Pre-this-build effective files carry no
    structural_sha256, so they read stale ONCE and self-migrate.

    `data_dir` lets the BOOT caller name the base dir without building a
    service (multi-user boot keeps services lazy); route callers omit it
    and resolve through the registry as always.

    Returns the degrade warning when stored ops no longer validate
    (labeled dials-only), else None. Never raises: reconcile trouble is
    logged + surfaced by GET, never a 500 in front of the console."""
    import hashlib as _hashlib
    import json as _json
    import logging as _logging

    try:
        if data_dir is not None:
            from pathlib import Path as _Path

            base = _Path(data_dir)
            overlay_path = base / "config" / "entity_phases_overlay.json"
            eff_path = base / "config" / "entity_phases.json"
            if not overlay_path.is_file():
                return None  # no operator edits — nothing derived, nothing stale
            try:
                overlay = _json.loads(overlay_path.read_text(encoding="utf-8"))
                if not isinstance(overlay, dict):
                    return None  # corrupt overlay is GET's loud-degrade lane
            except Exception:  # noqa: BLE001
                return None
        else:
            overlay, _warn = _read_overlay()
            eff_path = _effective_spec_file_path()
        if not overlay:
            return None  # no operator edits — nothing derived, nothing stale
        raw = _packaged_spec_raw()
        structural = _json.loads(raw)
        sha = _hashlib.sha256(raw.encode("utf-8")).hexdigest()
        current: Dict[str, Any] = {}
        if eff_path.is_file():
            try:
                loaded = _json.loads(eff_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    current = loaded
            except Exception:  # noqa: BLE001 - unreadable effective = rewrite
                current = {}
        meta = current.get("_operator") or {}
        fresh = (
            int(meta.get("edit_seq") or 0) == int(overlay.get("edit_seq") or 0)
            and str(current.get("structural_sha256") or "") == sha
        )
        if fresh:
            return None
        _path, degrade = _write_effective_spec_file(structural, sha, overlay, eff_path=eff_path)
        if degrade:
            _logging.getLogger(__name__).error(degrade)
        return degrade
    except Exception as e:  # noqa: BLE001
        _logging.getLogger(__name__).warning("effective spec reconcile skipped: %s", e)
        return None


def _edge_op_refusal_response(refusal: Any, *, status: int = 400):
    """EdgeOpRefusal -> the additive structured error shape (the visit-error
    precedent): `detail` stays a human string (artifact message + specific
    why); the ARTIFACT refusal code rides as a sibling body key + header."""
    from fastapi.responses import JSONResponse

    code = str(getattr(refusal, "code", "") or "")
    return JSONResponse(
        status_code=status,
        content={"detail": f"{refusal.message} — {refusal.detail}", "code": code},
        headers={"X-Gateway-Error-Code": code} if code else None,
    )


@router.get("/spec/phases")
def entity_phase_spec() -> Dict[str, Any]:
    """THE ONE STATE GRAPH, served (laurent dm#79 via c3562: "gateway MUST
    serve your state graph... there is only one state graph per entity and
    it MUST be shared"). The artifact's pen stays entity's
    (spec/entity_phases.json — one pen, version-bumped, ruling-cited); the
    gateway VENDORS a byte copy at sync time and serves it here so every
    client (entity app, observer, this console) reads the SAME graph from
    the wire instead of sync-by-vigilance (the diary_type-clamp class).
    A drift test pins the vendored bytes against the source spec whenever
    the checkout is present; the bump protocol is entity announces →
    consumers re-vendor same-day. Declared BEFORE the /{name} routes so
    the literal path wins the match.

    EDITABLE LANE, v12 P0-3 shape (the whole-file PUT inverted the
    one-graph handshake): the STRUCTURAL spec + sha are UNTOUCHED by
    operator edits — the overlay serves BESIDE them. Consumers' drift warns
    key on `sha256` (structural) alone; the modulated note reads
    `overlay.edit_seq`."""
    import hashlib as _hashlib
    import json as _json

    try:
        raw = _packaged_spec_raw()
        structural = _json.loads(raw)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"phase spec unavailable: {e} — re-vendor assets/entity_phases.json")

    # Crash-window/vendor-drift heal BEFORE serving (c4859: reconcile at
    # boot + GET) — the detached loops read the FILE; the console reads
    # this route; both must agree after a mid-PUT crash or a re-vendor.
    reconcile_warning = reconcile_effective_spec_file()

    overlay, overlay_warning = _read_overlay()
    warnings: list = []
    out: Dict[str, Any] = {
        "spec": structural,
        "vendored": True,
        "sha256": _hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "source": "abstractentity/spec/entity_phases.json (one pen: entity; gateway serves a synced byte copy)",
        "operator_edited": bool(overlay),
        "effective_tunables": _effective_tunables(structural, overlay),
        # Lane-liveness signal (entity c5120 detection gap): the edge_ops
        # door is LIVE on this route even before the first edit — clients
        # light their edit affordances on this key's PRESENCE (empty list
        # = no ops yet), never via a probe-PUT.
        "graph_overlay": {"edge_ops": _overlay_edge_ops(overlay) if overlay else []},
    }
    if overlay:
        out["tunables_overlay"] = dict(overlay.get("tunables") or {})
        out["overlay"] = {
            "edit_seq": int(overlay.get("edit_seq") or 0),
            "edited_by": overlay.get("edited_by"),
            "edited_at": overlay.get("edited_at"),
            "reason": overlay.get("reason"),
        }
        # STRUCTURAL edge ops (operator build c4837): the stored ops serve
        # beside the dials, and the EFFECTIVE graph (structural ⊕ ops,
        # derived at the one point) serves only when the ops still validate
        # against the current structural artifact — never a broken merge.
        ops = _overlay_edge_ops(overlay)
        if ops:
            out["overlay"]["graph"] = {"edge_ops": ops}
            from ..phase_edge_ops import compute_effective_transitions, validate_edge_ops

            refusal = validate_edge_ops(structural, ops)
            if refusal is None:
                out["effective_transitions"] = compute_effective_transitions(structural, ops)
            else:
                warnings.append(
                    f"#FALLBACK stored graph.edge_ops invalid against the current structural spec "
                    f"({refusal.code}: {refusal.detail}) — effective graph is structural-only until re-applied"
                )
    if overlay_warning:
        warnings.append(overlay_warning)
    if reconcile_warning and reconcile_warning not in warnings:
        warnings.append(reconcile_warning)
    if warnings:
        out["warnings"] = warnings
    return out


@router.get("/spec/cognition-graph")
def entity_cognition_graph_spec() -> Dict[str, Any]:
    """THE COGNITION MAP, served (operator correction c5070 / laurent: the
    editable graph is "the COMPLEX MEMORY COGNITION STATE GRAPH ... ALL THE
    PASSIVE AND ACTIVE MEMORY CONSTRUCTION AND RECONSTRUCTION PROCESSES AND
    WHAT THEY CREATE — lessons, world models, gradual opinion system, safe
    identity update"). The pen is entity's (abstractentity/spec/
    cognition_graph.json — one pen, version-bumped); the gateway VENDORS a
    byte copy at sync time and serves it here so every client (blueprint
    page, observer) reads the SAME map from the wire, not a bundled copy
    each re-derives (the "bundled-only, unserved: zero references" gap
    c5070 named).

    Distinct from BOTH sibling surfaces: `/spec/phases` serves the SIMPLE
    phase-transition graph (the four resting phases); `/{name}/cognition`
    serves a live entity's CURRENT cognition STATE. This serves the
    cognition MAP artifact — the topology of memory-construction lanes.

    SERVE-ONLY today (structural read + sha, the one-truth mechanism c5070
    made unambiguous-and-now). The overlay/proposal EDIT door reuses the
    phase-lane machinery but is gated on (a) entity WIDENING the artifact
    with a graph_overlay_contract and (b) laurent's question 1 (must
    structural edits be engine-consulted first) — until then cognition
    structure is ROUTED PROPOSALS, not live edge_ops (the three-tier law).
    A drift pin holds the vendored bytes to entity's pen; the bump protocol
    is entity announces a widen -> consumers re-vendor same-day. Declared
    BEFORE the /{name} routes so the literal path wins the match."""
    import hashlib as _hashlib
    import json as _json

    try:
        raw = _packaged_cognition_raw()
        graph = _json.loads(raw)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"cognition graph unavailable: {e} — re-vendor assets/cognition_graph.json")

    nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else []
    edges = graph.get("edges") if isinstance(graph.get("edges"), list) else []
    return {
        "graph": graph,
        "vendored": True,
        "sha256": _hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "source": "abstractentity/spec/cognition_graph.json (one pen: entity; gateway serves a synced byte copy)",
        "version": graph.get("version"),
        "node_count": len(nodes),
        "edge_count": len(edges),
        # No overlay block yet: the cognition edit door is gated (see docstring).
        # operator_edited stays false until the overlay lane opens, so a client
        # reads the same "structural truth, no operator modulation" shape the
        # phase GET serves before its first edit.
        "operator_edited": False,
        "editable": False,
        "edit_status": "serve-only — the cognition edit door is gated on entity's overlay-contract widen + laurent Q1 (structural-edits-engine-consulted); structure is routed proposals until then",
    }


class PhaseSpecEditRequest(BaseModel):
    # v12 P0-3 (dials) + the structural-edit build (c4837): tunables and/or
    # graph.edge_ops. The full-replace arm stays GONE — structure beyond the
    # artifact's own overlay contract (per-edge edit_policy) goes through
    # the entity pen (git + re-vendor), never this door.
    tunables: Optional[Dict[str, Any]] = Field(default=None, description="Partial tunables patch — deep-merged onto the structural tunables (the operator's dials). Known keys only, bounds-checked against tunables_meta.")
    graph: Optional[Dict[str, Any]] = Field(default=None, description="Structural edge ops: {'edge_ops': [{op: add|remove|redirect, ...}]}. PRESENT = document ownership (the list REPLACES the stored ops wholesale — read GET first, edit, PUT back); ABSENT = stored ops untouched. Validated against the vendored artifact's graph_overlay_contract; one illegal op refuses the whole batch.")
    if_match: Optional[int] = Field(default=None, description="CAS token: the overlay edit_seq this edit was based on (0/absent = no overlay existed). 409 on race.")
    reason: Optional[str] = Field(default=None, description="Why — carried into every entity's blueprint_edited host marker.")


def _flatten_dials(patch: Dict[str, Any], prefix: str = "") -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in (patch or {}).items():
        dotted = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            out.update(_flatten_dials(value, dotted))
        else:
            out[dotted] = value
    return out


def _validate_dials(patch: Dict[str, Any], structural: Dict[str, Any]) -> Optional[str]:
    """Known-keys-only + bounds from tunables_meta (v12 P0-3 item 3: an
    unknown key is a typo'd dial — refuse, never default silently)."""
    meta = dict((structural.get("tunables") or {}).get("tunables_meta") or {})
    flat = _flatten_dials(patch)
    for dotted, value in flat.items():
        if dotted.endswith("$comment") or dotted == "tunables_meta" or dotted.startswith("tunables_meta."):
            return f"{dotted!r} is the structural pen's — overlays carry dial values only"
        rule = meta.get(dotted)
        if not isinstance(rule, dict):
            return f"unknown dial {dotted!r} — not declared in tunables_meta (a typo'd dial must refuse, never default silently)"
        unit = str(rule.get("unit") or "")
        if unit == "bool":
            if not isinstance(value, bool):
                return f"{dotted!r} is a boolean dial; got {value!r}"
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{dotted!r} must be a number ({unit or 'numeric'}); got {value!r}"
        lo, hi = rule.get("min"), rule.get("max")
        if lo is not None and value < float(lo):
            return f"{dotted!r}={value} is below the declared minimum {lo}"
        if hi is not None and value > float(hi):
            return f"{dotted!r}={value} is above the declared maximum {hi}"
    return None


@router.put("/spec/phases")
def edit_entity_phase_spec(req: PhaseSpecEditRequest) -> Dict[str, Any]:
    """THE EDITABLE BLUEPRINT (laurent dm#104), v12 P0-3 shape + the
    structural-edit build (laurent dm#276 via c4837): dial overlays AND
    graph.edge_ops beside the structural graph — never a second pen near
    one counter. Admin-gated (route policy table).

    - The overlay persists at `<data_dir>/config/entity_phases_overlay.json`
      (edit_seq CAS: send `if_match` = the edit_seq you read; 409 on race);
    - the DERIVED effective spec is atomically rewritten at
      `<data_dir>/config/entity_phases.json` — the file detached loops read;
      when edge_ops ride, it carries merged transitions + the graph block +
      structural_sha256 (runtime's H2 handshake input: sha mismatch on the
      consumer side = dials-only);
    - dial validation is known-keys-only + bounds from tunables_meta;
      edge_ops validate against the artifact's own graph_overlay_contract
      (per-edge edit_policy, cause registry, refusal codes — the rules ARE
      the artifact's, never a gateway copy); one illegal op refuses the
      whole batch;
    - every edit records a `blueprint_edited` host marker in EVERY entity's
      biography (write-first, then markers — a marker claiming an edit that
      never landed would be a false biography entry). The STRUCTURAL sha is
      untouched: drift warns never fire on an overlay edit."""
    import hashlib as _hashlib
    import json as _json
    from datetime import datetime, timezone

    tunables_patch = req.tunables if isinstance(req.tunables, dict) else {}
    graph_present = req.graph is not None
    if not tunables_patch and not graph_present:
        raise HTTPException(status_code=400, detail="provide a non-empty tunables patch and/or a graph.edge_ops block")

    try:
        raw = _packaged_spec_raw()
        structural = _json.loads(raw)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"phase spec unavailable: {e}")
    sha = _hashlib.sha256(raw.encode("utf-8")).hexdigest()  # STRUCTURAL — untouched by design

    if tunables_patch:
        problem = _validate_dials(tunables_patch, structural)
        if problem:
            raise HTTPException(status_code=400, detail=problem)

    overlay, _overlay_warning = _read_overlay()

    # STRUCTURAL edge ops (operator build c4837, the gateway's refusal
    # door): request graph PRESENT = document ownership (full replace,
    # empty list clears); ABSENT = stored ops ride along UNCHANGED — but
    # the FINAL set always re-validates against the CURRENT artifact, so a
    # dials-only PUT after a re-vendor cannot silently carry now-illegal
    # ops into the effective file (fail-closed, the drift named).
    if graph_present:
        if not isinstance(req.graph, dict) or not isinstance(req.graph.get("edge_ops"), list):
            raise HTTPException(status_code=400, detail="graph must be an object carrying edge_ops: [...] (a list of ops)")
        final_ops: List[Dict[str, Any]] = [op for op in req.graph["edge_ops"] if isinstance(op, dict)]
        if len(final_ops) != len(req.graph["edge_ops"]):
            raise HTTPException(status_code=400, detail="each edge_op must be an object")
    else:
        final_ops = _overlay_edge_ops(overlay)
    if final_ops:
        from ..phase_edge_ops import validate_edge_ops

        refusal = validate_edge_ops(structural, final_ops)
        if refusal is not None:
            if graph_present:
                # The request's own ops are illegal: the artifact's ruled
                # refusal (code + its own message) goes back verbatim.
                return _edge_op_refusal_response(refusal)
            raise HTTPException(
                status_code=409,
                detail=(
                    f"stored graph.edge_ops no longer valid against the current structural spec "
                    f"({refusal.code}: {refusal.detail}) — re-read GET /spec/phases and re-apply "
                    f"graph.edge_ops before (or with) this edit"
                ),
            )

    current_seq = int(overlay.get("edit_seq") or 0)
    if req.if_match is not None and int(req.if_match) != current_seq:
        raise HTTPException(
            status_code=409,
            detail=f"overlay changed since you read it (your if_match={req.if_match}, current edit_seq={current_seq}) — re-read and re-apply",
        )

    from ..security.principal import current_gateway_principal

    principal = current_gateway_principal()
    editor = f"person:{principal.user_id}" if principal is not None else "person:operator"
    edit_seq = current_seq + 1
    merged_overlay_tunables = _deep_merge_numbers(dict(overlay.get("tunables") or {}), tunables_patch)
    new_overlay: Dict[str, Any] = {
        "edit_seq": edit_seq,
        "edited_by": editor,
        "edited_at": datetime.now(timezone.utc).isoformat(),
        "reason": str(req.reason or ""),
        "tunables": merged_overlay_tunables,
        # Provenance: the structural artifact these ops were validated
        # against (reconcile + consumers read drift from it).
        "structural_sha256": sha,
    }
    if final_ops:
        new_overlay["graph"] = {"edge_ops": final_ops}

    overlay_path = _operator_overlay_path()
    overlay_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = overlay_path.with_suffix(f".tmp.{edit_seq}")
    tmp.write_text(_json.dumps(new_overlay, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(overlay_path)

    # The DERIVED effective file — what detached loops read, atomically
    # rewritten through the ONE derivation point (v12 item 5 + c4859):
    # merged tunables, merged transitions when ops ride, structural_sha256
    # for runtime's H2 handshake. Degrade is impossible here (the final
    # ops just validated above), but the seam stays honest.
    eff_path, _degrade = _write_effective_spec_file(structural, sha, new_overlay)

    changed = "+".join(
        part for part in (
            "tunables" if tunables_patch else None,
            "graph" if graph_present else None,
        ) if part
    )

    # The moment lands in every biography (best-effort, labeled — the edit
    # itself already persisted; a marker failure must not roll it back).
    marker_warnings: list = []
    marked = 0
    try:
        from ..entity_replay import record_host_marker

        registry = _registry()
        for row in registry.list_entities():
            slug = str(row.get("slug") or "")
            entity_id = str(row.get("entity_id") or "")
            if not slug or not entity_id or row.get("error"):
                continue  # labeled stray/unreadable homes get no marker
            try:
                home = registry.get_home(slug)
                marker_details: Dict[str, Any] = {
                    "edit_seq": edit_seq, "changed": changed,
                    "structural_sha256": sha,
                    "edited_by": editor, "reason": str(req.reason or ""),
                }
                if graph_present:
                    marker_details["graph_ops"] = len(final_ops)
                record_host_marker(
                    entities_dir=registry.entities_dir,
                    slug=slug,
                    entity_id=entity_id,
                    kind="blueprint_edited",
                    journal_seq=int(home.memory.current_seq()),
                    details=marker_details,
                )
                marked += 1
            except Exception as e:  # noqa: BLE001
                marker_warnings.append(f"#FALLBACK marker failed for {slug}: {e}")
    except Exception as e:  # noqa: BLE001
        marker_warnings.append(f"#FALLBACK blueprint_edited markers unavailable: {e}")

    out: Dict[str, Any] = {
        "ok": True,
        "edit_seq": edit_seq,
        "changed": changed,
        "sha256": sha,  # STRUCTURAL sha — unchanged by design (drift warns stay quiet)
        "overlay_path": str(overlay_path),
        "effective_path": str(eff_path),
        "markers_recorded": marked,
        "tunables_overlay": merged_overlay_tunables,
        "effective_tunables": _effective_tunables(structural, new_overlay),
        # GET-SYMMETRIC overlay block (entity c4828 YES to gateway c4814):
        # the save note renders "edit N" from a PRESENT field instead of an
        # inferred dangling "?" — the lying-save-note class (framework c4779)
        # cured at the source. Same shape the GET serves under `overlay`, so
        # a client reads success identically from either call.
        "operator_edited": True,
        "overlay": {
            "edit_seq": edit_seq,
            "edited_by": editor,
            "edited_at": new_overlay["edited_at"],
            "reason": new_overlay.get("reason") or "",
        },
    }
    if final_ops:
        # GET-symmetry for the structural lane too: the stored ops + the
        # effective graph a client would read back.
        out["overlay"]["graph"] = {"edge_ops": final_ops}
        from ..phase_edge_ops import compute_effective_transitions

        out["effective_transitions"] = compute_effective_transitions(structural, final_ops)
    if marker_warnings:
        out["warnings"] = marker_warnings
    return out


@router.get("/templates")
def entity_spark_templates() -> Dict[str, Any]:
    """The spark/template gallery for the creation modal's template tab (plan
    (b), gateway c872; VERSIONED per the operator directive 2026-07-13): the
    builtin framework floor + operator templates at their CURRENT version.
    Entity-independent (the modal reads it before the entity exists)."""
    from ..template_store import list_templates

    templates, warnings = list_templates(_registry().data_dir)
    return {"schema_version": 1, "templates": templates, "warnings": warnings}


class TemplateSaveRequest(BaseModel):
    id: str = Field(..., description="Template id (lowercase letters/digits/_/-, 1-64 chars; not 'framework-default')")
    spark: Dict[str, Any] = Field(..., description="The blueprint spark document (linted at save; name filled at summon)")
    name: str = Field(default="", description="Operator-facing template name (meta, not the spark's name)")
    description: str = Field(default="", description="Operator-facing description")
    note: str = Field(default="", description="Optional version note for the history entry")


@router.get("/templates/{template_id}")
def entity_get_template(template_id: str, version: Optional[int] = None) -> Dict[str, Any]:
    """VIEW a template's FULL spark (the operator's 'I must be able to view
    it' — the whole document, not just a description). version=None = current;
    a specific version reads that historical blueprint verbatim."""
    from ..template_store import TemplateError, get_template

    try:
        return get_template(_registry().data_dir, template_id, version=version)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except TemplateError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/templates/{template_id}/versions")
def entity_template_versions(template_id: str) -> Dict[str, Any]:
    """The append-only version history (operator: 'every template should be
    versioned'). Each entry names the version, when, who, and the note."""
    from ..template_store import template_versions

    return {"template_id": template_id, "versions": template_versions(_registry().data_dir, template_id)}


@router.post("/templates", status_code=201)
def entity_create_template(req: TemplateSaveRequest) -> Dict[str, Any]:
    """CREATE a new operator template (the operator's 'I must be able to
    create new ones'). Linted before write; the builtin floor is untouchable
    (seed a new id from it). Admin-gated (a template seeds everyone's
    entities). Version 1 is written."""
    from ..security.principal import current_gateway_principal
    from ..template_store import TemplateError, save_template

    principal = current_gateway_principal()
    actor = f"person:{principal.user_id}" if principal is not None else "person:operator"
    try:
        return save_template(
            _registry().data_dir, template_id=req.id, spark=req.spark, name=req.name,
            description=req.description, actor=actor, note=req.note or "created", expect_new=True,
        )
    except TemplateError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/templates/{template_id}")
def entity_edit_template(template_id: str, req: TemplateSaveRequest) -> Dict[str, Any]:
    """EDIT a template (the operator's 'modify them later on') — appends a
    NEW version, never overwrites (versioning is append-only). Linted before
    write, so an edit that strips a core value refuses at SAVE, never at a
    later summon. Admin-gated."""
    from ..security.principal import current_gateway_principal
    from ..template_store import TemplateError, save_template

    principal = current_gateway_principal()
    actor = f"person:{principal.user_id}" if principal is not None else "person:operator"
    try:
        return save_template(
            _registry().data_dir, template_id=template_id, spark=req.spark, name=req.name,
            description=req.description, actor=actor, note=req.note or "edited", expect_new=False,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except TemplateError as e:
        raise HTTPException(status_code=400, detail=str(e))


class MaintenanceWindowRequest(BaseModel):
    action: str = Field(..., description="open | close")
    reason: str = Field(default="", description="Why the window is open (rides the host marker)")


@router.post("/{name}/maintenance-window")
def entity_maintenance_window(name: str, req: MaintenanceWindowRequest) -> Dict[str, Any]:
    """Operator maintenance window (Castor doctoring, GO 2026-07-13 21:12):
    `open` arms a FILE-based hold in the home — every door path (visits,
    chat, summons, cognition, runtime opens) refuses 409 while it stands,
    across serve restarts (releasing an already-running process's sqlite
    handles requires a restart; the hold must survive it). `close` releases
    after verify green. Both moments land as host markers with the
    principal. Admin-gated (route policy); the operator state underneath
    (asleep) is untouched."""
    from ..security.principal import current_gateway_principal

    action = str(req.action or "").strip().lower()
    if action not in ("open", "close"):
        raise HTTPException(status_code=400, detail="action must be open|close")
    principal = current_gateway_principal()
    actor = f"person:{principal.user_id}" if principal is not None else "person:operator"
    reg = _registry()
    try:
        if action == "open":
            return reg.open_maintenance_window(name, reason=req.reason, actor=actor)
        return reg.close_maintenance_window(name, reason=req.reason, actor=actor)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))


@router.get("/{name}/maintenance-window")
def entity_maintenance_window_status(name: str) -> Dict[str, Any]:
    """The hold status (readable while held — this route does not open the
    home, deliberately)."""
    try:
        _registry().manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    return _registry().maintenance_hold_status(name)


@router.get("/{name}/footprint")
def entity_footprint(name: str) -> Dict[str, Any]:
    """READ-ONLY home footprint (entity app's memory-health panel, c1779):
    the on-disk truth the replay stream deliberately does not carry — bytes
    per file family, journal event count, and the last maintenance act. A
    PURE PEEK at the home files (read-only sqlite, file stats, marker read)
    that works even while a maintenance hold is up (the panel must render
    the before/after DURING the act). N6-whitelisted shape: sizes, counts,
    timestamps — no paths, no base_url, no keys."""
    import sqlite3

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    home_dir = registry.entities_dir / manifest.slug

    def _size(fname: str) -> Optional[int]:
        p = home_dir / fname
        try:
            return p.stat().st_size if p.exists() else None
        except OSError:
            return None

    out: Dict[str, Any] = {
        "entity_id": manifest.entity_id,
        "memory_bytes": _size("memory.sqlite3"),
        "book_bytes": _size("home.sqlite3"),
        "runtime_bytes": _size(f"runtime_{manifest.slug}.sqlite3"),
        "home_bytes": None,
        "journal_events": None,
        "records": None,
        "last_maintenance_at": None,
        "last_maintenance_kind": None,
        "maintenance_held": bool(registry.maintenance_hold_status(manifest.slug).get("held")),
        "warnings": [],
    }
    try:
        total = 0
        for p in home_dir.rglob("*"):
            try:
                if p.is_file() and not p.is_symlink():
                    total += p.stat().st_size
            except OSError:
                continue
        out["home_bytes"] = total
    except Exception as e:  # noqa: BLE001
        out["warnings"].append(f"#FALLBACK home size walk failed: {e}")
    try:
        con = sqlite3.connect(f"file:{home_dir / 'memory.sqlite3'}?mode=ro", uri=True)
        try:
            out["journal_events"] = int(con.execute("SELECT COUNT(*) FROM memj_events").fetchone()[0])
            out["records"] = int(con.execute(
                "SELECT COUNT(DISTINCT subject) FROM triples WHERE predicate LIKE '%abstract%'"
            ).fetchone()[0])
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001 - a peek must degrade labeled, never 500
        out["warnings"].append(f"#FALLBACK journal peek failed: {e}")
    # Last maintenance act from the host-marker stream (reembed or a
    # maintenance window) — the panel's "what moved this number" anchor.
    try:
        from ..entity_replay import read_host_markers

        for m in read_host_markers(registry.entities_dir, manifest.slug):
            p = m.get("payload") or {}
            if p.get("kind") in ("reembed", "maintenance_window_open", "maintenance_window_close"):
                out["last_maintenance_at"] = str(m.get("observed_at") or "")
                out["last_maintenance_kind"] = str(p.get("kind"))
    except Exception as e:  # noqa: BLE001
        out["warnings"].append(f"#FALLBACK marker read failed: {e}")
    return out


@router.get("/creation-defaults")
def entity_creation_defaults() -> Dict[str, Any]:
    """The gateway's default substrate + embedding for the creation form's
    'Gateway default' dropdown MODE (operator directive 2026-07-13: the
    provider/model/embedding dropdowns default to the gateway default and
    NAME it, rather than a blank the operator must know to leave empty).

    ONE authoritative read shared by every frontend (console vanilla JS,
    continuum/entity/flow React) so the default is not re-derived
    divergently — the endpoints are the shared contract (the console cannot
    import uic's React picker; it consumes this + /discovery/*). Each field
    degrades to null + a labeled note when its source is unconfigured or
    unreachable — a dropdown never fabricates a default it cannot resolve."""
    import os as _os

    out: Dict[str, Any] = {"schema_version": 1, "warnings": []}

    # LLM substrate default: the operator env is the gateway-wide entity
    # substrate choice (the 2026-07-09 resolution chain); discovery's
    # default_provider/model is the abstractcore-wide default beneath it.
    env_p = (_os.getenv("ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER") or "").strip()
    env_m = (_os.getenv("ABSTRACTGATEWAY_ENTITY_CHAT_MODEL") or "").strip()
    if env_p and env_m:
        out["substrate"] = {"provider": env_p, "model": env_m, "source": "operator-env"}
    else:
        out["substrate"] = {"provider": None, "model": None, "source": "unset"}
        out["warnings"].append(
            "#FALLBACK no gateway-wide entity substrate configured (ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER/_MODEL) "
            "— a creation without an explicit substrate will refuse at first summon; pick one in the form"
        )

    # Embedding default: the execution-host embedding.text capability route.
    try:
        from ..embeddings_config import resolve_embedding_config

        route = resolve_embedding_config(base_dir=_registry().data_dir)
        out["embedding"] = {"provider": route.provider, "model": route.model, "source": route.source}
    except Exception as e:  # noqa: BLE001 - a missing route degrades to labeled null, never a 500
        out["embedding"] = {"provider": None, "model": None, "source": "unset"}
        out["warnings"].append(f"#FALLBACK no gateway default embedding configured: {e}")

    return out


@router.get("/inventory/tools")
async def entity_tool_inventory() -> Dict[str, Any]:
    """The full tool inventory, ENTITY-INDEPENDENT (pre-create; the creation
    modal renders tools BEFORE the entity exists) — descriptor contract v6,
    plan (a) P0-1. Serve-time composition of core ∪ walled rows with the
    gateway attaching `executes_via` and validating the static union. The
    capabilities tab reads this to know what tools EXIST; the phase matrix
    (rule 2b) offers only the entity_walled subset."""
    from ..tool_inventory import compose_tool_inventory

    try:
        composed = compose_tool_inventory()
    except RuntimeError as e:
        # A union-validation failure is a real serving defect (an enumeration
        # drifted from the served set) — surface it loudly, never a partial set.
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "schema_version": 1,
        "tools": composed["tools"],
        "degraded": composed["degraded"],
        "warnings": composed["warnings"],
    }


@router.get("/inventory/capability-matrix")
async def entity_capability_matrix() -> Dict[str, Any]:
    """The MatrixPayload the creation modal's capabilities tab renders — the
    framework DEFAULT per-phase grant over the entity_walled inventory (rule
    2b), entity-independent (pre-create). Server truth: descriptor fields
    ride each cell so the client never re-derives a field from a name."""
    from ..tool_inventory import phase_capability_matrix

    try:
        return phase_capability_matrix(None)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("")
def list_entities() -> Dict[str, Any]:
    return {"entities": _registry().list_entities()}


@router.get("/{name}")
def inspect_entity(name: str, diary_limit: int = 5, standings_top_k: int = 10) -> Dict[str, Any]:
    """Identity summary — pure reads only (who it is, what it recently
    elected to remember, how it feels, what it still wonders). Inspecting an
    entity never deposits usage."""
    try:
        return _registry().inspect(name, diary_limit=int(diary_limit), standings_top_k=int(standings_top_k))
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{name}/card")
def entity_card(
    name: str,
    top_n: int = 5,
    current_window_events: int = 200,
    as_of: Optional[int] = None,
) -> Dict[str, Any]:
    """The identity card (a2a 0009, maintainer: "something to know our
    companion"): the engine compositor's sections (identity, age+context,
    current state as a window, likes/dislikes with channels separate,
    open/resolved questions, key moments, discoveries — each with
    provenance) plus the gateway overlays (manifest name/age, operator
    state, mind substrate, host moments). Pure reads only — knowing
    someone must not fake their memory usage. `as_of` anchors the engine
    sections at a journal seq (the observer's timeline card)."""
    try:
        return _registry().card(
            name,
            top_n=int(top_n),
            current_window_events=int(current_window_events),
            as_of=int(as_of) if as_of is not None else None,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# Communities are recomputed only when the life GREW: cache keyed on the
# HOME DIRECTORY (unique per principal AND per entity — adversary F3: a
# name-keyed cache would serve tenant A's life to tenant B's same-named
# entity whenever their journal seqs collide; the home dir also folds
# name-case aliasing) + the journal high-water seq (the same freshness
# signal the replay stream anchors on). Compute is ~70ms at 369 records —
# the cache exists so a polling view costs nothing.
_COMMUNITIES_CACHE: Dict[str, Tuple[int, Dict[str, Any]]] = {}


@router.get("/{name}/communities")
def entity_communities(name: str) -> Dict[str, Any]:
    """Topic communities over the memory graph (operator ask 2026-07-17:
    louvain-class regrouping for a topic-level view; computed at the
    RUNTIME level for durability — abstractruntime.identity.communities;
    this route is the thin serving end, same read posture as /card).
    Pure read; deterministic per store state; exceptional gateway edit
    authorized by the operator with the gateway seat offline (owner-review
    ask on the room record)."""
    try:
        from abstractruntime.identity.communities import memory_communities

        home = _registry().get_home(name)
        cache_key = str(getattr(home, "home_dir", None) or getattr(home, "path", None) or name)
        seq = int(home.memory.current_seq())
        cached = _COMMUNITIES_CACHE.get(cache_key)
        if cached is not None and cached[0] == seq:
            return cached[1]
        out = memory_communities(home.store)
        out["journal_seq"] = seq
        _COMMUNITIES_CACHE[cache_key] = (seq, out)
        return out
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except ImportError as e:
        raise HTTPException(status_code=501, detail=f"communities unavailable: {e}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{name}/verify")
def verify_entity(name: str) -> Dict[str, Any]:
    """Verify both attestation planes (the book's hash chain; the graph
    projections against the book) plus spark-vs-marker and manifest checks."""
    try:
        return _registry().verify(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class SetEntityStateRequest(BaseModel):
    state: str = Field(..., description="awake | asleep | paused (resting stays the entity's own election)")
    reason: str = Field(default="", description="Why — carried into the honest wake cue and the host marker")
    dream: bool = Field(default=False, description="With state='asleep': run the dream pass inside the no-summon window")


@router.post("/{name}/state")
def set_entity_state(name: str, req: SetEntityStateRequest) -> Dict[str, Any]:
    """The sleep/wake/pause door (a2a 0008, ask 2). Writes go through the
    runtime's single state writer; the moment is host-marked into the
    replay stream; `asleep --dream` runs the dream pass in the window the
    state itself creates. Operator surface: this is an authenticated write
    endpoint — workplaces can never pause an entity (they have no path
    here; the tier rule is structural).

    Mid-visit guard (observer, maintainer 2026-07-09 02:02): asleep/paused
    must actually STOP an open visit, not just flip a badge while the chat
    host keeps writing episodes. Closing to a non-awake state first tears
    down any open visit (its reflection runs), THEN writes the state — so
    the emergency stop and consent revocation are effective, not cosmetic.
    Reflection is skipped for `paused` (a hard freeze is not a graceful
    close).

    PROVENANCE (hypnos 10:20:42 incident): state_history said
    written_by="operator" and a client-authored reason — enough to know the
    CHANNEL, never WHICH principal acted, so a disputed wake could not be
    traced to a session. The door now appends a SERVER-derived stamp
    `[by person:<user_id> via POST .../state]` to every reason (the
    2026-07-07 ruling's auto-reason shape: identity + act + timestamp IS
    the audit trail; client prose alone is a claim, not a record)."""
    target = str(req.state or "").strip().lower()
    # PHASE-VOCABULARY ALIASES (laurent c203 wave 1): `sleep` and `restore`
    # are the preferred operator words (sleep = enter the sleep phase;
    # restore = the paused exit). Legacy awake/asleep/paused stay accepted
    # forever — CLIs, scripts, and engraved muscle memory ride them; the
    # at-rest state-file words never change (adversary P1-11).
    target = {"sleep": "asleep", "restore": "awake"}.get(target, target)
    from ..security.principal import current_gateway_principal

    principal = current_gateway_principal()
    actor = f"person:{principal.user_id}" if principal is not None else "person:operator"
    reason_in = str(req.reason or "").strip()
    stamp = f"[by {actor} via POST /entities/{name}/state]"
    # OPERATOR-SLEEP-IS-ABSOLUTE (laurent dm#127, spec v17 — "sleep is sleep,
    # it can't wake up on personal time if I put it to sleep"): the operator
    # sleep click is a COMPOSITE act — asleep + personal grant DISARMED +
    # standing work orders CLEARED, ONE act named in ONE biography moment.
    # After it, no machine path wakes into personal or work (the v13
    # need-check finds a bare desk and re-sleeps — no new suppression
    # machinery). Tonight's incident (Ephemeral woke to personal 1h after an
    # operator sleep) was precisely the missing disarm: the ~1h bounded-sleep
    # wake landed on a still-armed July-15 grant. The composite reason names
    # all three so the single host marker is self-describing.
    is_operator_sleep = target == "asleep"
    if is_operator_sleep:
        composite_note = "operator sleep (v17): personal grant disarmed + standing orders cleared — sleep is sleep"
        stamped_reason = f"{reason_in} {composite_note} {stamp}".strip() if reason_in else f"{composite_note} {stamp}"
    else:
        stamped_reason = f"{reason_in} {stamp}".strip() if reason_in else stamp
    closed: Optional[Dict[str, Any]] = None
    closed_durable: Optional[Dict[str, Any]] = None
    # AWAKE UNDER A LIVE VISIT IS REFUSED (mutual-exclusivity wave, laurent
    # dm#94 via entity's write audit finding 3: five unguarded awake writers
    # could destroy a standing visiting posture — this door was one). The
    # /loop/start visit-guard pattern applied: a LIVE visit owns the phase;
    # waking the state file under it would mint the exact visit+personal
    # ambiguity the ruling retires. A STALE posture with NO live visit stays
    # writable — POST /state is the operator's repair door and must never
    # wedge on a crashed visit's leftovers.
    if target == "awake":
        live_visit_id: Optional[str] = None
        try:
            durable_live = _visit_host().status(name)
            if durable_live.get("open"):
                live_visit_id = str(durable_live.get("run_id") or "a durable visit")
        except Exception:  # noqa: BLE001 - a broken host must not wedge the repair door
            pass
        if live_visit_id is None:
            try:
                chat_live = _chat_host().status(name)
                if chat_live.get("open"):
                    live_visit_id = str(chat_live.get("chat_id") or "a hosted chat")
            except Exception:  # noqa: BLE001
                pass
        if live_visit_id is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"a visit is open on {name} ({live_visit_id}) — the visit owns the phase "
                    "(the four phases are mutually exclusive); close it first "
                    "(POST .../visit/{run_id}/close or .../chat/{chat_id}/close), or sleep/pause "
                    "which tears it down"
                ),
            )
    # COMPOSITE DISARM FIRST (v17 crash-safety): for an operator sleep, disarm
    # the personal grant and clear standing orders BEFORE writing asleep. The
    # ordering is the crash invariant — a crash AFTER disarm but BEFORE the
    # state write leaves a BARE DESK (no grant, no order, still awake), which
    # the v13 need-check re-sleeps; the reverse order (asleep first) is the
    # exact bug tonight's incident hit (asleep + armed grant → the ~1h
    # bound woke into personal). If the disarm itself fails (corrupt
    # phases.yaml), we REFUSE the sleep rather than write asleep over an
    # armed grant — the operator repairs the file and retries (same
    # loud-refuse discipline write_personal_grant already enforces).
    composite: Dict[str, Any] = {}
    if is_operator_sleep:
        try:
            from abstractruntime.identity.life import (
                archive_work_order,
                read_personal_grant,
                read_work_order,
                write_personal_grant,
            )

            manifest_c = _registry().manifest_for(name)
            home_dir_c = _registry().entities_dir / manifest_c.slug
            prior_grant = read_personal_grant(home_dir_c)
            if str(prior_grant.get("mode") or "disabled") != "disabled":
                write_personal_grant(home_dir_c, mode="disabled", granted_by=actor)
                composite["grant_disarmed"] = {
                    "was_mode": prior_grant.get("mode"),
                    "was_granted_by": prior_grant.get("granted_by"),
                }
            standing_order = read_work_order(home_dir_c)
            if standing_order:
                archive_work_order(home_dir_c, verdict=f"cleared by operator sleep {stamp}")
                composite["work_order_cleared"] = True
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e).strip("'\""))
        except Exception as e:  # noqa: BLE001 - a corrupt grant file must not become asleep+armed
            raise HTTPException(
                status_code=409,
                detail=(
                    f"operator sleep could not disarm {name}'s personal grant "
                    f"({e}); refusing to sleep over an armed grant — repair "
                    "phases.yaml and retry (sleep-is-absolute needs the disarm to land)"
                ),
            )

    # STATE WRITES FIRST (state-sources adversary P0-2, trigger b): the state
    # file is the coordination authority — writing it before the teardown
    # makes the visit gates (open/turn/tick check asleep+paused) refuse any
    # NEW work landing in the teardown window, so a raced open can no longer
    # survive the sleep. The teardown below then closes what was already
    # open; its terminal duty sees the operator's fresh state (not a
    # visit-authored one) and leaves it standing.
    try:
        result = _registry().set_state(name=name, state=target, reason=stamped_reason, dream=bool(req.dream))
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if target in ("asleep", "paused"):
        # Legacy in-process chat session (until the /chat surface retires).
        # A missing chat host (503 from _chat_host on a hand-built service)
        # is a GRACEFUL SKIP here, not a hard error — this is a teardown of
        # a surface that may not exist; the durable-visit teardown below is
        # the one that matters on the migration path.
        try:
            closed = _chat_host().close_open_visit(name, reflect=(target == "asleep"))
        except HTTPException as e:
            if e.status_code != 503:
                raise
            closed = None  # no chat host on this service shape — skip it
        except Exception:
            closed = None
        # DURABLE visit run (the migration path): an open /visit must be
        # torn down by the same emergency-stop, not left ticking while the
        # badge flips. sleep = graceful close (reflection runs); pause = the
        # hard freeze (closed_by=pause -> skip_reflection, no cognition —
        # runtime a0bd1df completes it with reflection_pending). The state
        # itself remains the coordination authority; this makes it effective.
        try:
            from ..entity_visits import VisitRefused

            host = _visit_host()
            live = host.status(name)
            if live.get("open") and live.get("run_id"):
                try:
                    closed_durable = host.close(
                        name, live["run_id"],
                        closed_by=("pause" if target == "paused" else "sleep"),
                        reason=(reason_in or f"{target} requested by operator") + f" {stamp}",
                    )
                except VisitRefused as e:
                    # The teardown FAILED (e.g. a concurrent turn holds the
                    # lease). The state still flips (it is the coordination
                    # authority), but the failure is LABELED in the response
                    # — never a silent None that reads as "no visit was open".
                    closed_durable = {"error": e.detail, "teardown_failed": True,
                                      "run_id": live.get("run_id")}
        except HTTPException as e:
            if e.status_code == 503:
                closed_durable = None  # genuinely no visit host on this service shape
            else:
                raise
        except Exception as e:  # noqa: BLE001 - adversary P2-5: a REAL teardown
            # exception (store error, runtime failure) must not read as "no
            # visit was open" — the state flipped, the gates protect
            # cognition, but the response says what actually happened.
            closed_durable = {"error": f"#FALLBACK durable teardown errored: {e}", "teardown_failed": True}
    if composite:
        # Surface the composite in the response so the operator sees the sleep
        # DID disarm/clear (not a silent side effect).
        result["composite_sleep"] = composite
    if closed is not None:
        result["closed_visit"] = {"turns": closed.get("turns"), "summary": closed.get("summary")}
    if closed_durable is not None:
        if closed_durable.get("teardown_failed"):
            result["closed_visit_run"] = {
                "run_id": closed_durable.get("run_id"),
                "teardown_failed": True,
                "error": closed_durable.get("error"),
            }
        else:
            result["closed_visit_run"] = {
                "run_id": closed_durable.get("run_id"),
                "status": closed_durable.get("status"),
                "output": closed_durable.get("output"),
            }
    # The verb's answer names WHERE THE ENTITY SETTLED (c203 wave 1): a
    # "wake" with no process running settles right back to the resting
    # default — say sleep, never pretend a standing "awake". Branch order
    # mirrors /cognition's fold (sharedgraph adversary P2-5: the settled
    # answer omitted the visit arm — POST /state awake during an open visit
    # answered sleep while /cognition said visit); failures degrade with a
    # LABEL, never a silent missing field.
    try:
        from ..entity_loop import loop_status as _ls

        manifest = _registry().manifest_for(name)
        home_dir2 = _registry().entities_dir / manifest.slug
        state_after = str((result.get("state") or {}).get("state") or "awake")
        visit_open = False
        try:
            visit_open = bool(_visit_host().status(name).get("open")) or bool(_chat_host().status(name).get("open"))
        except Exception:  # noqa: BLE001 - hosts may be absent on hand-built services
            pass
        if state_after == "paused":
            result["phase"] = None
        elif visit_open:
            result["phase"] = "visit"
        elif state_after == "asleep":
            result["phase"] = "sleep"
        elif bool(_ls(home_dir2).get("running")):
            result["phase"] = "personal"
        else:
            result["phase"] = "sleep"
            result["phase_note"] = "no tasks running, no visit — resting (sleep is the default; the entity never dwells unphased)"
    except Exception as e:  # noqa: BLE001 - the settled-phase note is garnish, never load-bearing
        result["phase_warning"] = f"#FALLBACK settled phase unavailable: {e}"
    return result


@router.get("/{name}/state")
def get_entity_state(name: str) -> Dict[str, Any]:
    """The operator state badge source (pure read: awake/asleep/paused + mode)."""
    try:
        return _registry().state_of(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ------------------------------------------------------------- visit runs
# GW-C endpoint half (plan items 7/9/10): visits as DURABLE RUNS in the
# entity's own runtime — the migration path off the in-process chat host.
# The legacy /chat surface stays untouched until the A/B rules the switch.


def _visit_host():
    """The durable-visit host. Built by the service factory and cached on
    the service; the per-registry fallback covers only hand-built services
    (tests) — a per-REQUEST host would drop the in-process open-locks and
    meet index, so it is cached on the registry, never rebuilt per call."""
    from ..entity_visits import EntityVisitHost

    svc = get_gateway_service()
    host = getattr(svc, "entity_visit_host", None)
    if host is not None and getattr(host, "_registry", None) is _registry():
        return host
    registry = _registry()
    cached = getattr(registry, "_visit_host_singleton", None)
    if cached is None or getattr(cached, "_registry", None) is not registry:
        cached = EntityVisitHost(registry)
        registry._visit_host_singleton = cached
    return cached


class OpenVisitRequest(BaseModel):
    session_id: Optional[str] = Field(default=None, description="Stable session id (None = minted)")


class VisitTurnRequest(BaseModel):
    text: str = Field(..., description="What you say this turn")
    speaker: Optional[str] = Field(default=None, description="namespace:name of the voice (default: first participant)")


class VisitCloseTask(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    brief: str = Field(default="", max_length=20000)
    workflow: Optional[Dict[str, Any]] = Field(default=None)
    backlog_ref: Optional[str] = Field(default=None, max_length=500)


class VisitCloseRequest(BaseModel):
    closed_by: str = Field(
        default="operator",
        description="operator | sleep (reflection runs) | pause (hard freeze: skip_reflection, no cognition)",
    )
    reason: str = Field(default="", description="Why — reaches the reflection look-back (sleep/operator)")
    # G3 door half: tasks LEFT in this visit land in the home's durable
    # task inbox at close (origin visit:<run_id>, stamped door-side).
    tasks: Optional[List[VisitCloseTask]] = Field(
        default=None,
        description="Tasks left with the entity during this visit — recorded in <home>/task_inbox.jsonl at close",
    )


@router.post("/{name}/visit/open")
def open_visit(name: str, req: OpenVisitRequest) -> Dict[str, Any]:
    """Open a DURABLE visit: one run in the entity's own runtime, stamped at
    creation (visit_id + participants + posture ride the stamp), ticked to
    the first PARK. A gateway restart no longer kills this conversation —
    /turn continues it with the same run_id.

    WHO is present is DOOR-DERIVED, never client-claimed (the situation
    contract, same as the summon endpoint): the sole visitor is the
    authenticated principal (auth-off local gateway = the operator), and
    the entity stamps itself. A payload cannot engrave a false co-presence
    (person:laurent) into an append-only life — so there is no participants
    field to send."""
    from ..entity_visits import VisitRefused
    from ..security.principal import current_gateway_principal

    principal = current_gateway_principal()
    visitor = f"person:{principal.user_id}" if principal is not None else "person:operator"
    try:
        return _visit_host().open(name, participants=[visitor], session_id=req.session_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except VisitRefused as e:
        return _visit_refused_response(e)


@router.post("/{name}/visit/{run_id}/turn")
def visit_turn(name: str, run_id: str, req: VisitTurnRequest) -> Dict[str, Any]:
    from ..entity_visits import VisitRefused

    try:
        return _visit_host().turn(name, run_id, text=req.text, speaker=req.speaker)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except VisitRefused as e:
        return _visit_refused_response(e)


@router.post("/{name}/visit/{run_id}/close")
def visit_close(name: str, run_id: str, req: VisitCloseRequest) -> Dict[str, Any]:
    from ..entity_visits import VisitRefused

    try:
        out = _visit_host().close(name, run_id, closed_by=req.closed_by, reason=req.reason)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except VisitRefused as e:
        return _visit_refused_response(e)

    # G3 door half: record tasks left in this visit AFTER a successful close
    # (the close is the visitor's word that these remain owed). The close
    # itself already succeeded, so task-recording failures surface as a
    # loud warning in the RESPONSE, never a 5xx that would misreport the
    # completed close.
    if req.tasks and str(out.get("status") or "") == "completed":
        registry = _registry()
        try:
            manifest = registry.manifest_for(name)
            home_dir = registry.entities_dir / manifest.slug
            actor = _task_actor()
            _task_inbox_marker(
                registry=registry,
                manifest=manifest,
                detail={
                    "channel": "operator",
                    "by": actor,
                    "change": "added",
                    "count": len(req.tasks),
                    "visit_run_id": str(run_id),
                },
            )
            from ..entity_tasks import append_task

            recorded = []
            for t in req.tasks:
                recorded.append(
                    append_task(
                        home_dir,
                        title=t.title,
                        brief=t.brief,
                        origin=f"visit:{run_id} closed by {actor}",
                        by=actor,
                        workflow=t.workflow if isinstance(t.workflow, dict) else None,
                        backlog_ref=t.backlog_ref,
                    )
                )
            out["tasks_recorded"] = [{"task_id": r["task_id"], "title": r["title"]} for r in recorded]
        except HTTPException as e:
            out["tasks_warning"] = f"#FALLBACK tasks NOT recorded: {e.detail}"
        except Exception as e:  # noqa: BLE001
            out["tasks_warning"] = f"#FALLBACK tasks NOT recorded: {e}"
    elif req.tasks:
        out["tasks_warning"] = (
            f"#FALLBACK tasks NOT recorded: close ended with status={out.get('status')!r} — "
            "re-close or POST /entities/{name}/tasks once the visit is settled"
        )
    # CLOSE FIRES THE QUEUE HEAD (queue contract invariant 11) — on a
    # background thread so the closing visitor's response never waits on
    # the next visitor's summon executing; the sweeper backstops it.
    _fire_queue_admission(name)
    return out


@router.post("/{name}/visit/{run_id}/tick")
def visit_tick(name: str, run_id: str) -> Dict[str, Any]:
    """Drive-to-park (walkthrough step 5b: after a mid-turn kill the run is
    RUNNING-not-parked; this drives it to its next park/terminal without a
    message). Idempotent on parked and terminal runs."""
    from ..entity_visits import VisitRefused

    try:
        return _visit_host().tick(name, run_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except VisitRefused as e:
        return _visit_refused_response(e)


@router.get("/{name}/visit")
def visit_status(name: str) -> Dict[str, Any]:
    from ..entity_visits import VisitRefused

    try:
        return _visit_host().status(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except VisitRefused as e:
        return _visit_refused_response(e)


@router.get("/{name}/visit/{run_id}/ledger")
def visit_ledger(name: str, run_id: str, after: int = 0, limit: int = 500) -> Any:
    """The visit run's own ledger, paged (coder-tui c4307 ask 2): visit runs
    execute in the home's `runtime_<slug>.sqlite3`, invisible to `/runs/*` —
    this is the observability read for that lane. Pure read; works on live
    and terminal runs; the stamped-visit check inside the host is the auth
    boundary (only stamped visits of THIS entity are served)."""
    from ..entity_visits import VisitRefused

    try:
        return _visit_host().ledger(name, run_id, after=after, limit=limit)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except VisitRefused as e:
        return _visit_refused_response(e)


@router.get("/{name}/visit/{run_id}/transcript")
def visit_transcript(name: str, run_id: str) -> Dict[str, Any]:
    """The durable visit's transcript (cutover gap 2): a PURE READ from the
    run's own vars so the drawer rehydrates after reload without replaying
    turns. Works on live and terminal runs — a closed visit stays readable,
    twin of the hosted chat transcript."""
    from ..entity_visits import VisitRefused

    try:
        return _visit_host().transcript(name, run_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except VisitRefused as e:
        return _visit_refused_response(e)


# ------------------------------------------------------------- entity meets
# Item 14 (the north-star substrate's first realizable shape): two entities
# in one conversation as TWO correlated durable legs, one visit_id, mutual
# relay, one home lease at a time. Same-door only in v0 (cross-door needs
# the federation transport, deferred).


def _meet_host():
    """The meet host over the durable-visit host (shares its open-locks so
    a meet leg and a solo open on the same home cannot race). Built by the
    factory; the per-registry fallback (tests) reuses the same visit host."""
    from ..entity_meets import EntityMeetHost

    svc = get_gateway_service()
    host = getattr(svc, "entity_meet_host", None)
    if host is not None and getattr(getattr(host, "_visits", None), "_registry", None) is _registry():
        return host
    registry = _registry()
    cached = getattr(registry, "_meet_host_singleton", None)
    if cached is None or getattr(getattr(cached, "_visits", None), "_registry", None) is not registry:
        cached = EntityMeetHost(_visit_host())
        registry._meet_host_singleton = cached
    return cached


class OpenMeetRequest(BaseModel):
    entity_a: str = Field(..., description="First entity name")
    entity_b: str = Field(..., description="Second entity name (must differ)")
    session_id: Optional[str] = Field(default=None, description="Stable meet session id (None = minted)")


class MeetRelayRequest(BaseModel):
    opener: str = Field(..., description="'a' or 'b' — which side speaks this exchange")
    text: str = Field(..., description="What the opener says; the reply is relayed to the other entity")


class MeetCloseRequest(BaseModel):
    reason: str = Field(default="", description="Why — reaches each leg's reflection")


@router.post("/meets/open")
def open_meet(req: OpenMeetRequest) -> Dict[str, Any]:
    """Open a two-entity meet: both legs summoned under ONE visit_id, each
    in its own home runtime. The authenticated principal is the CONVENER —
    stamped into both legs and the author of every steering line (honest
    attribution: neither entity is ever recorded saying the operator's
    words). Never half-opens — if the second entity refuses, the first leg
    is closed."""
    from ..entity_meets import VisitRefused
    from ..security.principal import current_gateway_principal

    principal = current_gateway_principal()
    convener = f"person:{principal.user_id}" if principal is not None else "person:operator"
    try:
        return _meet_host().open(
            req.entity_a, req.entity_b, session_id=req.session_id, convener=convener
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except VisitRefused as e:
        raise HTTPException(status_code=e.status, detail=e.detail)


@router.post("/meets/{meet_id}/relay")
def relay_meet(meet_id: str, req: MeetRelayRequest) -> Dict[str, Any]:
    from ..entity_meets import VisitRefused

    try:
        return _meet_host().relay(meet_id, opener=req.opener, text=req.text)
    except VisitRefused as e:
        raise HTTPException(status_code=e.status, detail=e.detail)


@router.post("/meets/{meet_id}/close")
def close_meet(meet_id: str, req: MeetCloseRequest) -> Dict[str, Any]:
    from ..entity_meets import VisitRefused

    try:
        return _meet_host().close(meet_id, reason=req.reason)
    except VisitRefused as e:
        raise HTTPException(status_code=e.status, detail=e.detail)


@router.get("/meets/{meet_id}")
def meet_status(meet_id: str) -> Dict[str, Any]:
    from ..entity_meets import VisitRefused

    try:
        return _meet_host().status(meet_id)
    except VisitRefused as e:
        raise HTTPException(status_code=e.status, detail=e.detail)


class ReembedRequest(BaseModel):
    embedding_model: Optional[str] = Field(
        default=None,
        description="Verification only: must match the door's resolved embedder "
        "(reconfigure the embeddings route first, then reembed)",
    )
    reason: str = Field(default="", description="Why — journaled by the engine and host-marked by the door")


@router.get("/{name}/embedding")
def get_entity_embedding_status(name: str) -> Dict[str, Any]:
    """Read-only embedding status for the reembed ceremony (adversary P0:
    the verification field demanded a value the UI never showed — the only
    in-UI discovery was failing once to read the 400). Serves the home's M1
    pin (pure peek, never mutates the store) + the door's currently resolved
    embedder identity + the match verdict. N6 whitelist: model/dimension/
    status/source only — base_url and keys never serialize."""
    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))

    pin: Optional[Dict[str, Any]] = None
    pin_error: Optional[str] = None
    try:
        from abstractmemory import read_embedding_pin

        raw = read_embedding_pin(registry.entities_dir / manifest.slug / "memory.sqlite3")
        if isinstance(raw, dict):
            pin = {k: raw.get(k) for k in ("model_id", "dimension", "source") if k in raw}
    except ImportError:
        pin_error = "#FALLBACK engine lacks read_embedding_pin (older abstractmemory)"
    except Exception as e:  # noqa: BLE001 - a status read must not 500 the panel
        pin_error = f"#FALLBACK pin unreadable: {e}"

    resolved_id: Optional[str] = None
    embedder_warning: Optional[str] = None
    try:
        embedder = registry._resolve_embedder()
        if embedder is not None:
            for attr in ("model", "model_id"):
                value = getattr(embedder, attr, None)
                if isinstance(value, str) and value.strip():
                    resolved_id = value.strip()
                    break
        else:
            embedder_warning = registry._embedder_warning or "no embedder resolved at this door"
    except Exception as e:  # noqa: BLE001
        embedder_warning = f"embedder resolution failed: {e}"

    pin_model = (pin or {}).get("model_id")
    if pin_model and resolved_id:
        match = "match" if pin_model == resolved_id else "mismatch"
    else:
        match = "unknown"
    out: Dict[str, Any] = {
        "pin": pin,
        "status": "pinned" if pin and pin.get("model_id") else "unpinned",
        "resolved_embedder": resolved_id,
        "match": match,
    }
    warnings = [w for w in (pin_error, embedder_warning) if w]
    if warnings:
        out["warnings"] = warnings
    return out


@router.post("/{name}/reembed")
def reembed_entity(name: str, req: ReembedRequest) -> Dict[str, Any]:
    """The M1b repair verb (operator-gated, never routine): re-derive the
    home's vector index with the door's resolved embedder under the
    maintenance lease; atomic swap engine-side; the act lands in BOTH
    planes (engine journal claim + door host marker). A held home refuses
    409 naming the holder — retry at the writer's next boundary."""
    try:
        from abstractruntime.storage.lease import DirectoryLeaseHeld
    except ImportError:  # older runtime: the registry already labels this path
        DirectoryLeaseHeld = ()  # type: ignore[assignment]
    try:
        return _registry().reembed(name=name, embedding_model=req.embedding_model, reason=req.reason)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except DirectoryLeaseHeld as e:  # type: ignore[misc]
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# Seq-keyed cache for /cognition's engine folds (sharedgraph adversary P0):
# {slug: (journal_seq, drives_fold, pressure_fold|None)}. The folds are pure
# reads, so the seq is the exact invalidation key; a poller re-reads the
# store only when the world actually changed. Process-local by design (one
# serving process per door).
_DRIVES_CACHE: Dict[str, Tuple[int, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]] = {}


@router.get("/{name}/cognition")
def entity_cognition(name: str) -> Dict[str, Any]:
    """The B3 spend wire (laurent 04:58 "unclear when it's working and
    consuming credits"; dispatch c1340 2c — observer's board tile and the
    entity app's meter both consume this): ONE read serving working-now +
    tick phase + real token spend.

    - working: loop mid-day OR a live visit run executing (never fabricated
      — both inputs are pid-checked/store-read state).
    - loop: the pid-cross-checked loop status verbatim (phase/running/
      stop_requested/stopped_by ride through).
    - visit: the durable visit status (open/run_id/status/turn_n).
    - spend: BILLED usage folded from the per-home run ledger's completed
      llm_call records (result.usage.total_tokens — the entity LLM handler
      records it on every call), lifetime across the home store + the live
      visit run tree. HONEST GAP: the own-time loop runs home-direct
      (ChatSession, no run ledger), so loop cognition is NOT in these
      numbers yet — labeled #FALLBACK, never estimated here (the entity
      app's input-side estimate remains its own labeled surface until
      runtime's loop-usage half lands).
    - drives: the cognition-health ratios (questions open/resolved,
      problems open/repaired, interests open/explored) from memory's
      cognition_health — the same ladder + diary convention the card
      compositor reads. Render-when-present: absent (with a labeled
      warning) when the engine predates the read OR the read fails."""
    from ..entity_loop import loop_status

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    home_dir = registry.entities_dir / manifest.slug

    # Maintenance window: refuse UP FRONT (this endpoint folds per-source
    # failures into labeled warnings by design — a held home would degrade
    # into a half-truthful 200 instead of the honest 409 every other door
    # answers; the hold outranks the degradation contract).
    registry._refuse_if_held(manifest.slug)

    loop = loop_status(home_dir)

    warnings: List[str] = []
    visit: Dict[str, Any] = {"open": False}
    visit_in_flight = False
    try:
        host = _visit_host()
        visit = host.status(name)
        if visit.get("open") and visit.get("run_id"):
            visit_in_flight = bool(host.is_in_flight(str(visit["run_id"])))
    except Exception as e:  # noqa: BLE001 - adversary P2-3: a broken visit
        # host must not silently read as "no visit" on the endpoint that
        # promises labeled degradation.
        warnings.append(f"#FALLBACK visit status unavailable: {e}")

    # State axis + the ONE composite phase (console adversary P1-1: the
    # life_state composite existed but consumers re-derived from four reads
    # at four times — the "contradictory badges" anti-pattern. /cognition is
    # the single poll now, so the axes AND their fold ride together).
    st: Dict[str, Any] = {}
    try:
        st = registry.state_of(name) or {}
    except Exception as e:  # noqa: BLE001
        st = {"state": "awake", "warning": f"#FALLBACK state unreadable: {e}"}
    chat_open = False
    chat_in_flight = False
    try:
        chat = _chat_host().status(name)
        chat_open = bool(chat.get("open"))
        chat_in_flight = bool(chat.get("turn_in_flight"))
    except Exception as e:  # noqa: BLE001 - labeled, like the visit branch (P3)
        warnings.append(f"#FALLBACK chat status unavailable: {e}")
    state_word = str(st.get("state") or "awake")
    # STRICT ONE-ACTIVE-PHASE (laurent 13:28/13:34 via c1455/c1463; semantics
    # c1466 axis-mixing finding): `phase` carries ONLY the ruled four keys —
    # visit / work / personal / sleep — or None when no phase is active (the
    # ruled machine has no idle state; reality does — never fake one). The
    # old chain mixed phase words, state words and "resting" in one enum and
    # its ordering HID the operator's pause under visiting. State (awake/
    # asleep/paused) and rest are SEPARATE served fields now; impossible
    # combos surface as bugs instead of being hidden by priority. The closed
    # set IS pinned (totality ruled 13:46, contingency lifted c1472);
    # consumers still tolerate None.
    resting = bool(loop.get("running")) and str(loop.get("phase") or "") != "day"
    # LIVENESS AXIS (decision:entity-liveness-axis v3, c1559): `frozen` is
    # RETIRED from the serve; the one derived field is liveness alive|stopped
    # (paused => stopped — the kill switch). Same derivation everywhere:
    # entities.derived_liveness, never a second rule.
    from ..entities import derived_liveness

    stopped = state_word == "paused"
    # PHASE IS TOTAL WHILE ALIVE (laurent c203, 2026-07-20: "awake is NOT a
    # state; the entity at all times must be either visit/work/personal/
    # sleep"). The old `else: None` rendered the exact "awake, idle" hanging
    # state he rejects. The fold is SERVE-SIDE ONLY — the state file, the
    # yield-posture writes, and every engraved marker stay byte-identical
    # (the visit race keys on the STATE axis; adversary P0-5) — and doors
    # already WAKE resting sleepers, so folding idle to sleep changes zero
    # gate behavior. `phase_source` labels the one approximation: a running
    # loop day with a standing work order reads as work (loop_status cannot
    # tell day kinds apart — runtime's day_kind is the exact fix).
    phase_source = "actual"
    sleep_detail: Optional[str] = None
    if stopped:
        # The kill switch is NOT a phase — the liveness axis sits ABOVE the
        # machine (adversary P0-6); the ONE legitimate no-phase render.
        phase = None
    elif bool(visit.get("open")) or chat_open:
        phase = "visit"
    elif state_word == "asleep":
        phase = "sleep"
        mode_word = str(st.get("mode") or "")
        if mode_word == "dreaming":
            sleep_detail = "dreaming"
        elif st.get("wake_at"):
            sleep_detail = "bounded"
        else:
            sleep_detail = "resting"
    elif bool(loop.get("running")):
        # A running loop is a work day when a work order stands (the loop's
        # own day gate reads the same file — life.py day_phase), else
        # personal. Approximation until loop_status carries day_kind.
        try:
            from abstractruntime.identity.life import read_work_order

            if str(loop.get("phase") or "") == "day" and read_work_order(home_dir):
                phase = "work"
                phase_source = "derived"
            else:
                phase = "personal"
        except Exception:
            phase = "personal"
    else:
        # No process, no visit, not paused: the entity RESTS — sleep is the
        # resting default (IDLE-IS-SLEEP; newborns are born asleep). This
        # sleep runs no consolidation and never stamps runs (the deposit
        # gate's phase==sleep refusal applies to FORMAL windows only —
        # adversary P1-10); sleep_detail says so honestly.
        phase = "sleep"
        sleep_detail = "resting"
    # SETTLING (adversary P2-1): the state file is intent/authority; the loop
    # honors it at tick boundaries only — a fresh state write beside an older
    # loop heartbeat is "settling", not a contradiction. Lexicographic ISO
    # compare (both aware-UTC by the writers).
    settling = False
    try:
        changed_at = str(st.get("changed_at") or "")
        loop_at = str(loop.get("updated_at") or "")
        if changed_at and loop_at and bool(loop.get("running")) and changed_at > loop_at:
            settling = True
    except Exception:
        pass

    lifetime = {"llm_calls": 0, "tool_calls": 0, "tokens_total": 0, "runs": 0}
    live_visit: Optional[Dict[str, Any]] = None
    try:
        er = registry.get_entity_runtime(manifest.slug)
        runs = er.run_store.list_runs(limit=500) or []
        if len(runs) >= 500:
            warnings.append("#TRUNCATION lifetime spend folds the newest 500 runs — older runs uncounted")
        ids = [str(getattr(r, "run_id", "") or "") for r in runs if getattr(r, "run_id", None)]
        metrics = er.ledger_store.metrics_many(ids) if ids else {}
        for rid in ids:
            m = metrics.get(rid) or {}
            lifetime["llm_calls"] += int(m.get("llm_calls") or 0)
            lifetime["tool_calls"] += int(m.get("tool_calls") or 0)
            lifetime["tokens_total"] += int(m.get("tokens_total") or 0)
        lifetime["runs"] = len(ids)
        live_run_id = str(visit.get("run_id") or "") if visit.get("open") else ""
        if live_run_id:
            tree = [live_run_id] + [
                rid for rid, r in zip(ids, runs)
                if str(getattr(r, "parent_run_id", "") or "") == live_run_id
            ]
            lv = {"run_id": live_run_id, "llm_calls": 0, "tool_calls": 0, "tokens_total": 0}
            for rid in tree:
                m = metrics.get(rid) or {}
                lv["llm_calls"] += int(m.get("llm_calls") or 0)
                lv["tool_calls"] += int(m.get("tool_calls") or 0)
                lv["tokens_total"] += int(m.get("tokens_total") or 0)
            live_visit = lv
    except Exception as e:  # noqa: BLE001 - a spend read must not 500 the indicator
        warnings.append(f"#FALLBACK spend fold unavailable: {e}")

    # Loop spend (runtime 1154194): the loop records cumulative usage into
    # <home>/loop_spend.json after every tick — fold it in and the honest
    # #FALLBACK drops. Counters start at zero for pre-upgrade homes (the
    # ledger-less past is honestly unknowable — labeled, not estimated).
    loop_spend: Optional[Dict[str, Any]] = None
    try:
        from abstractruntime.identity.life import read_loop_spend

        ls = read_loop_spend(home_dir) or {}
        loop_spend = {
            "llm_calls": int(ls.get("llm_calls") or 0),
            "tool_calls": int(ls.get("tool_calls") or 0),
            "tokens_total": int(ls.get("tokens_total") or 0),
            "ticks": int(ls.get("ticks") or 0),
            "updated_at": ls.get("updated_at"),
        }
        lifetime["llm_calls"] += loop_spend["llm_calls"]
        lifetime["tool_calls"] += loop_spend["tool_calls"]
        lifetime["tokens_total"] += loop_spend["tokens_total"]
    except ImportError:
        if bool(loop.get("running")):
            warnings.append(
                "#FALLBACK loop cognition spend not included: this runtime predates "
                "read_loop_spend — upgrade abstractruntime to fold loop usage"
            )
    except Exception as e:  # noqa: BLE001
        warnings.append(f"#FALLBACK loop spend unreadable: {e}")

    # WORKING = executing right now (adversary P1-2/P1-5): loop mid-day, a
    # visit turn IN FLIGHT (the stored run status is a last-write claim — a
    # host crash mid-drive leaves status="running" at rest forever), or a
    # chat turn in flight. A parked/orphaned run is present, not working.
    working = bool(loop.get("running") and loop.get("phase") == "day") or visit_in_flight or chat_in_flight
    if (
        bool(visit.get("open"))
        and str(visit.get("status") or "") == "running"
        and not visit_in_flight
    ):
        warnings.append(
            "#FALLBACK visit run rests at status=running with no drive in flight "
            "(interrupted mid-turn?) — resume it with POST /visit/{run_id}/tick"
        )
    # Drive ratios (G1, cognition-health directive 2026-07-18): memory's
    # cognition_health fold over the home's full ladder — the same ladder,
    # diary convention, and ref-attr set the card compositor reads (the
    # cross-key divergence was fixed engine-side same-day), so this wire,
    # the card, and any bar agree by construction. RENDER-WHEN-PRESENT:
    # version skew or a read failure drops the key with a labeled warning —
    # absent until the source exists, never derived (the c2801 contract).
    #
    # SEQ-KEYED CACHE (sharedgraph adversary P0: these engine folds measured
    # ~90s on a 216-drive live home, and /cognition is POLLED — uncached,
    # one 5s poller stacks 90s reads into the shared sync threadpool until
    # every route stalls). The folds are PURE reads of the store, so the
    # journal seq IS the invalidation key: same seq = identical result by
    # construction; serve the cached fold and never touch the store twice
    # for one state of the world.
    drives: Optional[Dict[str, Any]] = None
    cached_pressure: Optional[Dict[str, Any]] = None
    try:
        from ..entities import MaintenanceHoldActive

        home = registry.get_home(name)
        seq_now = int(home.memory.current_seq())
        cache_hit = _DRIVES_CACHE.get(manifest.slug)
        if cache_hit is not None and cache_hit[0] == seq_now:
            drives = cache_hit[1]
            cached_pressure = cache_hit[2]
        else:
            drives = home.cognition_drives()
            cached_pressure = None  # recomputed below when drives exist
        if drives is None:
            warnings.append(
                "#FALLBACK drive ratios unavailable: this engine predates "
                "cognition_health — upgrade abstractmemory"
            )
    except MaintenanceHoldActive:
        # A hold armed mid-request outranks the degradation contract (the
        # route's own up-front rule) — refuse as every other door does.
        raise
    except Exception as e:  # noqa: BLE001 - a drives read must not 500 the indicator
        warnings.append(f"#FALLBACK drive ratios unreadable: {e}")

    out: Dict[str, Any] = {
        "working": working,
        "phase": phase,
        # phase_source: "actual" or "derived" (work approximated from the
        # standing work order until loop_status carries day_kind).
        "phase_source": phase_source,
        # sleep_detail: resting | dreaming | bounded — a resting default
        # never overclaims consolidation (c203 wave-1 honesty).
        **({"sleep_detail": sleep_detail} if sleep_detail else {}),
        "resting": resting,
        # paused => stopped; everything else => alive (documented mapping,
        # binary by construction — c1559 spelling point 2).
        "liveness": derived_liveness(state_word),
        # AUTHORITY RULE (adversary P2-1, served not commented): the state
        # file is the operator's INTENT and the coordination authority; the
        # loop honors it at tick boundaries — `settling` marks that window.
        "settling": settling,
        "authority": "state=intent (authoritative); loop/visit=actuality; settling=true while actuality catches up",
        "state": {
            "state": state_word,
            "liveness": derived_liveness(state_word),
            "mode": st.get("mode"),
            "reason": st.get("reason"),
            "changed_at": st.get("changed_at"),
            **({"warning": st.get("warning")} if st.get("warning") else {}),
        },
        "loop": loop,
        "visit": visit,
        "chat_open": chat_open,
        # ARMED ≠ IN-PHASE (semantics c1436): this is the GRANT axis —
        # phases.personal activation from <home>/phases.yaml (runtime
        # read_personal_grant, fail-closed). The current phase is the
        # separate fact above.
        "personal": _personal_grant_block(home_dir, registry=registry, manifest=manifest),
        "spend": {
            "lifetime": lifetime,
            "live_visit": live_visit,
            "loop": loop_spend,
            "source": "home-run-ledger+loop-spend" if loop_spend is not None else "home-run-ledger",
        },
    }
    if drives is not None:
        out["drives"] = drives
        # DRIVE PRESSURE (laurent c203: >20 standing drives must never just
        # "hang there"). SURFACED SIGNAL ONLY — never an auto-action (the
        # own-time cost ruling stands until the operator answers the fork).
        # The counts come from MEMORY'S ENGINE READ (drive_pressure — one
        # deterministic composition of every standing drive set, exact
        # counts, believed-rows both sides; their c215 ship) and the ruled
        # threshold is IMPORTED (DRIVE_PRESSURE_BOUND — one source, never a
        # second copy of the 20; the SELF_FRACTION_FLOOR law). The engine
        # deliberately takes no position on which phase answers pressure —
        # `should`/`idle` are the gateway's serve-side gate semantics:
        # tasks/work order => work, else personal; idle = pressure while
        # resting with no process. wake_reasons is limit-clipped at 20 and
        # structurally cannot see ">20" — never a threshold source.
        try:
            from abstractmemory import DRIVE_PRESSURE_BOUND, drive_pressure as _engine_pressure

            import os as _os_dp

            env_t = (_os_dp.getenv("ABSTRACTGATEWAY_DRIVE_PRESSURE_THRESHOLD") or "").strip()
            threshold = int(env_t) if env_t else int(DRIVE_PRESSURE_BOUND)

            if cached_pressure is not None:
                pressure = cached_pressure
            else:
                from ..entities import DIARY_SCOPE, LIFE_SCOPE, SELF_SCOPE

                eid = home.entity_id
                pressure = _engine_pressure(
                    home.store, home.journal,
                    scopes=[(SELF_SCOPE, eid), (DIARY_SCOPE, eid), (LIFE_SCOPE, eid)],
                )
                # Both folds computed for THIS seq — cache them together.
                _DRIVES_CACHE[manifest.slug] = (seq_now, drives, pressure)
            counts = {
                k: int(v) for k, v in pressure.items()
                if isinstance(v, (int, float)) and k != "total_open"
            }
            over = sorted(k for k, n in counts.items() if n > threshold)
            if over:
                has_tasks = False
                try:
                    from ..entity_tasks import count_pending

                    has_tasks = count_pending(home_dir) > 0
                except Exception:  # noqa: BLE001
                    pass
                has_order = False
                try:
                    from abstractruntime.identity.life import read_work_order

                    has_order = bool(read_work_order(home_dir))
                except Exception:  # noqa: BLE001
                    pass
                dp: Dict[str, Any] = {
                    "over": over,
                    "counts": counts,
                    "total_open": int(pressure.get("total_open") or 0),
                    "threshold": threshold,
                    "should": "work" if (has_tasks or has_order) else "personal",
                    "idle": bool(phase == "sleep" and sleep_detail == "resting"),
                    "note": pressure.get("note"),
                }
                # COUNT-WEIGHTED GROUPS (laurent 277; memory c296 fold key):
                # drive_pressure()['groups'] clusters similar drives so a
                # family of 12 similar questions surges above a lone one.
                # Render-when-present passthrough — a group is a VIEW over
                # the open drives (counts byte-unchanged), largest-first,
                # computed by the same clustering the sleep miner runs.
                groups = pressure.get("groups")
                if isinstance(groups, list) and groups:
                    dp["groups"] = groups
                out["drive_pressure"] = dp
        except ImportError:
            warnings.append(
                "#FALLBACK drive pressure unavailable: this engine predates "
                "drive_pressure — upgrade abstractmemory"
            )
            _DRIVES_CACHE[manifest.slug] = (seq_now, drives, None)
        except Exception as e:  # noqa: BLE001 - pressure is a signal, never a 500
            warnings.append(f"#FALLBACK drive pressure unreadable: {e}")
    if warnings:
        out["warnings"] = warnings
    return out


def _personal_grant_block(home_dir: Any, *, registry: Any = None, manifest: Any = None) -> Dict[str, Any]:
    try:
        from abstractruntime.identity.life import personal_grant_refusal, read_personal_grant
    except ImportError:
        return {"mode": None, "source": "not-recorded",
                "note": "#FALLBACK this runtime predates phases.yaml — grant axis unavailable"}
    grant = read_personal_grant(home_dir)
    refusal = personal_grant_refusal(grant)
    armed = refusal is None
    if registry is not None and manifest is not None and not armed:
        _maybe_mark_grant_expired(registry, manifest, grant)
    return {**grant, "armed": armed, "refusal": refusal, "source": "phases.yaml"}


def _maybe_mark_grant_expired(registry: Any, manifest: Any, grant: Dict[str, Any]) -> None:
    """The timer's own act, recorded (conformance adversary P2: the kind was
    declared with no writer — a lapsing grant was biographically invisible).
    Per semantics c1443: recorded by whichever process DETECTS expiry at a
    read boundary, payload carrying the lapsed expires_at; detectors dedup
    on (entity, expires_at)."""
    if str(grant.get("mode") or "") != "timer":
        return
    expires_at = str(grant.get("expires_at") or "").strip()
    if not expires_at:
        return
    from datetime import datetime, timezone

    try:
        deadline = datetime.fromisoformat(expires_at)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) < deadline:
            return  # timer still live — the refusal came from something else
    except ValueError:
        return  # unreadable expiry is a config problem, not a timer act
    try:
        from ..entity_replay import record_host_marker

        # Dedup on (entity, expires_at) INSIDE the marker lock (adversary
        # P2-1: a route-side scan-then-append raced concurrent reads and
        # cross-process writers into double markers).
        home = registry.get_home(manifest.slug)
        record_host_marker(
            entities_dir=registry.entities_dir,
            slug=manifest.slug,
            entity_id=manifest.entity_id,
            kind="personal_grant_expired",
            journal_seq=int(home.memory.current_seq()),
            details={"channel": "timer", "expires_at": expires_at, "granted_by": grant.get("granted_by")},
            dedup_field="expires_at",
        )
    except Exception:
        pass  # detection is observability — a marker failure never blocks a read


@router.get("/{name}/life_state")
def get_entity_life_state(name: str) -> Dict[str, Any]:
    """The ONE composite life phase (observer ask, maintainer 2026-07-09):
    the gateway collapses chat + operator-state + loop into a single
    mutually-exclusive `phase` (visiting > paused > asleep > personal >
    resting > awake) so clients render one chip and never re-derive
    contradictory badges. `own_time_running` rides alongside for a
    loop-alive indicator that does not fight the phase.

    DURABLE visits count (state-sources adversary P1-1): the chat host's
    fold predates the /visit lane, so this route widens `visiting` with the
    durable visit status — two composites disagreeing on the headline field
    was the exact anti-pattern this endpoint exists to kill. /cognition is
    the richer composite; this stays as the thin phase chip."""
    try:
        out = _chat_host().life_state(name)
    except HTTPException:
        raise
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        durable = _visit_host().status(name)
        if durable.get("open"):
            # ONE GRAPH WORD (wave adversary P1-3): the hosted arm of this
            # same endpoint serves phase="visit" (the c203 graph vocabulary)
            # — this widening said "visiting", so one endpoint spoke two
            # spellings of one axis depending on which lane the visit ran
            # on. The nuance rides `posture` (spec v10
            # NUANCE-NEVER-CONTRADICTS-PHASE; audit finding 2: widening
            # phase alone left posture="resting" beside a live durable
            # visit — clients preferring the ruled nuance-posture rendered
            # "personal · resting" beside a live chat).
            out["phase"] = "visit"
            out["posture"] = "visiting"
            out["visit_run_id"] = durable.get("run_id")
    except Exception as e:  # noqa: BLE001
        # LABELED downgrade (entity forensics c2465 finding 1): a broken
        # visit host silently painting the chat-host phase over an OPEN
        # durable visit was the two-composites-two-honesty-standards hole
        # (/cognition labels the same failure). The chip stays served —
        # but the reader can now SEE the fold failed.
        out["warnings"] = list(out.get("warnings") or []) + [
            f"#FALLBACK durable-visit fold unavailable ({e}) — phase reflects chat host + operator state only"
        ]
    return out


def _chat_host():
    from ..entity_chat import EntityChatHost

    svc = get_gateway_service()
    host = getattr(svc, "entity_chat_host", None)
    if isinstance(host, EntityChatHost):
        return host
    # GatewayService is a frozen dataclass — the host is constructed by the
    # service factory; this fallback covers only hand-built services and is
    # per-request (sessions would not survive across requests), so refuse
    # loudly rather than serving an amnesiac chat surface.
    raise HTTPException(
        status_code=503,
        detail="entity chat host not initialized on this gateway service (factory wiring missing)",
    )


class OpenChatRequest(BaseModel):
    # None = resolve the entity's ONE persisted substrate (substrate.yaml in
    # his home), then operator env (ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER/_MODEL),
    # then LOUD REFUSAL — the operator decides the mind substrate; never a
    # code default (maintainer rulings 2026-07-09 04:26 + 06:32).
    provider: Optional[str] = Field(default=None, description="abstractcore provider (None = stored substrate.yaml, then operator env, else refuse)")
    model: Optional[str] = Field(default=None, description="Chat model (None = stored substrate.yaml, then operator env, else refuse)")
    base_url: Optional[str] = Field(default=None, description="LMStudio-compatible endpoint (lmstudio-class providers only; None = operator env or local default)")
    participants: Optional[List[str]] = Field(default=None, description='Who is present, e.g. ["person:laurent"]; default person:operator')
    context_window: Optional[int] = Field(
        default=None,
        description="Declared window; below 20,000 refuses (None = env override or the wide default 65536)",
    )
    shelf_size: Optional[int] = Field(
        default=None,
        description="Recall shelf seats (None = env override or the wide default 50 — "
        "maintainer 2026-07-09: widened so the entity retrieves enough memories to function)",
    )
    max_output_tokens: int = Field(default=2048)
    enable_tools: bool = Field(default=True, description="Entity tools per the home's tool_policy.yaml (ruled defaults: the full set)")
    enable_workspace: bool = Field(
        default=False,
        description="Deprecated no-op (2026-07-11 ruling: workspace tools are in the default grant; narrow via tool_policy.yaml)",
    )


class ChatTurnRequest(BaseModel):
    text: str = Field(..., description="What you say to the entity this turn")
    speaker: Optional[str] = Field(
        default=None,
        description="Shared-room voice attribution (namespace:name, e.g. agent:ariadne). "
        "The speaker joins the session's participants and the turn is stamped to their "
        "voice; omitted = the session opener's voice.",
    )


class CloseChatRequest(BaseModel):
    reflect: bool = Field(default=True, description="Run the session-end reflection (feelings move there)")


@router.post("/{name}/chat/open")
async def open_entity_chat(name: str, req: OpenChatRequest) -> Dict[str, Any]:
    """Open a hosted chat session (the web chat's backend — a2a 0007, the
    maintainer's chat drawer). Hosts the SAME ChatSession the `entity chat`
    CLI runs: auto-yield of the own-time loop (bounded wait; 409 on
    timeout), prelude refusal aborts verbatim, one live session per home.
    Operator-authed like every entity write surface."""
    from ..entity_chat import ChatOpenRefused
    from fastapi.concurrency import run_in_threadpool

    # One life, one summon — BOTH lanes (conformance adversary): a parked
    # durable visit holds no lease between requests, so a chat open used to
    # succeed beside it and the durable visit's turns then 409'd on the
    # chat's held lease. The mirror of loop/start's durable check.
    try:
        durable = _visit_host().status(name)
        if durable.get("open"):
            raise HTTPException(
                status_code=409,
                detail=f"a durable visit is open on this entity (run {durable.get('run_id')!r}) — "
                "continue it with /visit/{run_id}/turn or close it first; one life, one summon",
            )
    except HTTPException:
        raise
    except Exception:
        pass  # no visit host on this service shape

    try:
        return await run_in_threadpool(
            _chat_host().open,
            name,
            provider=req.provider,
            model=req.model,
            base_url=req.base_url,
            participants=req.participants,
            context_window=req.context_window,
            shelf_size=req.shelf_size,
            max_output_tokens=req.max_output_tokens,
            enable_tools=req.enable_tools,
            enable_workspace=req.enable_workspace,
        )
    except ChatOpenRefused as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        kind = _provider_error_name(e)
        if kind is None:
            raise
        raise HTTPException(
            status_code=502,
            detail=f"the mind's provider refused the open ({kind}: {e}) — nothing was opened; "
            "check the provider/model (substrate) is loaded and retry, or swap the substrate",
        )


def _provider_error_name(e: BaseException) -> Optional[str]:
    """Boundary-safe provider-error detection (0059: no abstractcore import;
    the MRO NAMES are the duck type). Production finding from the live
    drive: a refused provider call (model unloaded, upstream 400) rendered
    as "Internal error" 500 — dishonest for the operator; the refusal must
    name WHICH side refused and the next act (the refusal-string rule)."""
    for cls in type(e).__mro__:
        if cls.__name__ in ("ProviderError", "ProviderAPIError", "InvalidRequestError", "AbstractCoreError", "ModelNotFoundError"):
            return cls.__name__
    return None


@router.post("/{name}/chat/{chat_id}/turn")
async def entity_chat_turn(name: str, chat_id: str, req: ChatTurnRequest) -> Dict[str, Any]:
    """One honest turn. The response's `tools_ran` is DRIVER-AUTHORED
    (the marker-imitation lesson: what actually executed is a data field,
    never derived from the reply prose)."""
    del name  # the chat_id is the session key; the path keeps URLs readable
    from ..entity_chat import ChatOpenRefused
    from fastapi.concurrency import run_in_threadpool

    try:
        return await run_in_threadpool(
            lambda: _chat_host().turn(chat_id, req.text, speaker=req.speaker)
        )
    except ChatOpenRefused as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    except RuntimeError as e:
        # The driver's memory-failure posture: the failed turn formed
        # nothing; the session should close so nothing corrupts.
        raise HTTPException(status_code=500, detail=f"memory failed this turn: {e} — close the session; his memory is intact")
    except Exception as e:  # noqa: BLE001
        kind = _provider_error_name(e)
        if kind is None:
            raise
        raise HTTPException(
            status_code=502,
            detail=f"the mind's provider refused this turn ({kind}: {e}) — the turn formed nothing; "
            "check the provider/model (substrate) is loaded and retry, or swap the substrate",
        )


@router.get("/{name}/chat/{chat_id}/transcript")
def entity_chat_transcript(name: str, chat_id: str) -> Dict[str, Any]:
    """The shared room's common view: every voice's turns, in order (pure
    read; any participant or UI can poll it to render the whole room)."""
    del name
    from ..entity_chat import ChatOpenRefused

    try:
        return _chat_host().transcript(chat_id)
    except ChatOpenRefused as e:
        raise HTTPException(status_code=e.status, detail=e.detail)


@router.post("/{name}/chat/{chat_id}/close")
async def close_entity_chat(name: str, chat_id: str, req: Optional[CloseChatRequest] = None) -> Dict[str, Any]:
    """End the visit: reflection pass (feelings move), close summary, home
    closed, the own-time loop woken if the open yielded it."""
    from ..entity_chat import ChatOpenRefused
    from fastapi.concurrency import run_in_threadpool

    try:
        out = await run_in_threadpool(
            _chat_host().close, chat_id, reflect=bool(req.reflect) if req is not None else True
        )
    except ChatOpenRefused as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    # CLOSE FIRES THE QUEUE HEAD (queue contract invariant 11) — background
    # thread: the closing response never waits on the next summon executing.
    _fire_queue_admission(name)
    return out


@router.get("/{name}/chat")
def entity_chat_status(name: str) -> Dict[str, Any]:
    """Is a visit open on this home right now? (one life, one summon)"""
    try:
        return _chat_host().status(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))


@router.get("/{name}/seat")
def entity_seat(name: str) -> Dict[str, Any]:
    """The held conversation seat, or {held: false} (conversation-seat plan,
    item 6). A read surface for the drawer's start-screen occupancy line — a
    human sees whether someone is already talking to this entity before
    summoning. Pure read; never mutates the seat."""
    svc = get_gateway_service()
    registry = _registry()
    try:
        home = registry.get_home(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    view = _seat_view(svc, registry, name, home.manifest.slug)
    view["entity_id"] = home.entity_id
    return view


class SummonEntityRequest(BaseModel):
    # Boundary validation (flow c5253 P1-3): an empty prompt used to be
    # accepted here and die 2.5s later inside the run as "MEMORY_PROBE
    # op=probe requires payload.cue" — engine jargon for an input the door
    # can check. min_length rejects ""; the route additionally strip-checks
    # (whitespace-only passes min_length but is the same empty stimulus).
    prompt: str = Field(..., min_length=1, description="The work brief for this session")
    flow_id: Optional[str] = Field(default=None, description="Target flow (default: the gateway's default bundle entrypoint)")
    bundle_id: Optional[str] = Field(default=None)
    bundle_version: Optional[str] = Field(default=None)
    session_id: Optional[str] = Field(default=None, description="Durable session id (default: a fresh entity-scoped session)")
    input_data: Optional[Dict[str, Any]] = Field(default=None, description="Extra flow input pins")
    prelude_budget: int = Field(default=1600, description="Token budget for the identity prelude render")
    diary_tail: int = Field(default=3, description="Diary entries offered to the prelude")
    gradation_top_k: int = Field(default=5, description="Standing entries offered to the prelude")
    context_window_tokens: Optional[int] = Field(
        default=None,
        description="Declared model context window; refused below the 20k floor (maintainer ruling)",
    )
    # Conversation-seat plan item 2: the caller's DECLARED kind drives the
    # priority matrix (human preempts agent holders; agents never displace).
    # Absent = agent — an undeclared caller never preempts (fails toward the
    # human). The declaration is etiquette-bound + audit-trailed (markers
    # carry principal + kind) until GW-H per-agent principals make it
    # structural. Human surfaces (the drawer, the TUI) declare "human".
    caller_kind: Optional[str] = Field(
        default=None,
        pattern="^(human|agent)$",
        description='Who is summoning: "human" (may preempt an agent-held seat) or "agent" (default; waits)',
    )
    # decision:summon-queue-v1 (contract §1/§5): queueing is EXPLICIT opt-in —
    # without queue=true, a held seat answers today's 409 + retry_after_s
    # byte-unchanged (agent probes that should fail fast keep failing fast;
    # silent queueing would time-shift agent traffic into a human's session).
    # park=true is the "leave it with her" posture: the entry persists without
    # a polling client (it IS the mailbox drop) and the answer lands in the
    # durable session.
    queue: bool = Field(default=False, description="If the seat is held, wait in the door queue (202 + poll) instead of a 409")
    park: bool = Field(default=False, description="With queue=true: leave the message with the entity (no polling client expected; exempt from poll-silence reaping)")


# ------------------------------------------------------- one life, one summon
# flow c5260 P1-B foundation + conversation-seat plan slice 2 + the queue
# (decision:summon-queue-v1): the seat record, TTL liveness, the priority
# matrix, and the per-slug guard RLock live in `entity_seat.py`; the queue
# store lives in `entity_queue.py`. This file owns the HTTP shapes, the
# markers, and the admission executor (which re-runs the whole summon path
# for a stored payload). Seat + queue files stay gateway bookkeeping
# OUTSIDE the home (door-local facts, like .host_stream; they must not
# travel on directory copy).
from ..entity_seat import summon_guard_lock as _summon_guard_lock


def _cross_lane_occupancy(name: str) -> Optional[Dict[str, Any]]:
    """A live visit/chat holding this one life, for the /seat READ surface.

    Source: the VISITING POSTURE (asleep + mode=visiting — the one write
    every visit/chat open lands unconditionally, laurent dm#94), the same
    restart-surviving signal the summon door's own gate refuses on. ONE
    cheap file read — deliberately NOT the visit-host status probe: that
    builds the per-entity runtime on first touch (measured ~15s cold),
    which no read surface may cost. The chat host's in-memory status
    refines the lane label when it is live; otherwise the posture reads as
    the durable-visit lane."""
    registry = _registry()
    try:
        from abstractruntime.identity.life import read_entity_state

        manifest = registry.manifest_for(name)
        state = read_entity_state(registry.entities_dir / manifest.slug)
        if str(state.get("state") or "") != "asleep" or str(state.get("mode") or "") != "visiting":
            return None
    except Exception:
        return None  # a broken read must not break the read surface
    try:
        svc = get_gateway_service()
        chat_host = getattr(svc, "entity_chat_host", None)
        if chat_host is not None:
            st = chat_host.status(name)
            if bool(st.get("open")):
                return {
                    "lane": "chat",
                    "run_id": str(st.get("chat_id") or ""),
                    "session_id": str(st.get("session_id") or ""),
                }
    except Exception:
        pass
    return {"lane": "visit", "run_id": "", "session_id": "", "reason": str(state.get("reason") or "")}


def _seat_view(svc: Any, registry: Any, name: str, slug: str) -> Dict[str, Any]:
    """The GET /seat read surface (conversation-seat plan, item 6): the held
    seat block, or {held: false}. Feeds the drawer's start-screen occupancy
    line so a human sees 'someone is talking to <entity>' before summoning.
    Pure read. Folds all three lanes: an open visit/chat renders held with
    its lane; the summon seat renders with the full record (TTL included) —
    the same occupancy the guard consults, so read and door never disagree."""
    from ..entity_queue import queue_depth as _queue_depth

    # The roster's "someone waits at the door" fact (queue contract §20) —
    # served on every seat read, held or free.
    depth = _queue_depth(registry.entities_dir, slug)
    cross = _cross_lane_occupancy(name)
    if cross is not None:
        return {
            "held": True,
            "lane": cross["lane"],
            "run_id": cross["run_id"],
            "session_id": cross["session_id"],
            "status": "open",
            "queue_depth": depth,
        }
    seat = seat_occupancy(svc.host.run_store, registry.entities_dir, slug)
    if seat is None:
        return {"held": False, "queue_depth": depth}
    return {
        "held": True,
        "lane": "summon",
        "run_id": seat["run_id"],
        "session_id": seat["session_id"],
        "status": seat["status"],
        "run_live": seat["run_live"],
        "holder": seat["holder"],
        "holder_kind": seat["holder_kind"],
        "held_since": seat["held_since"],
        "renewed_at": seat["renewed_at"],
        "idle_ttl_s": seat["idle_ttl_s"],
        "ttl_remaining_s": seat["ttl_remaining_s"],
        "current_idle_deadline": _seat_idle_deadline(seat),
        "queue_depth": depth,
    }


def _declared_context_window(req: "SummonEntityRequest", input_data: Dict[str, Any]) -> Optional[int]:
    """The context window as DECLARED by the caller (explicit field, the
    flow's max_in_tokens pin, or _limits) — the gateway checks what it can
    see and never guesses what it cannot."""
    candidates = [req.context_window_tokens, input_data.get("max_in_tokens")]
    limits = input_data.get("_limits")
    if isinstance(limits, dict):
        candidates.extend([limits.get("max_input_tokens"), limits.get("max_tokens")])
    declared = [int(c) for c in candidates if isinstance(c, (int, float)) and not isinstance(c, bool) and int(c) > 0]
    return min(declared) if declared else None


@router.post("/{name}/summon")
def summon_entity(name: str, req: SummonEntityRequest):
    """Summon the entity into a work session (thin wrapper: identity comes
    from the authenticated principal here; `_summon_core` carries the whole
    door so the QUEUE ADMISSION executor can re-run it for a stored payload
    under the enqueuer's identity — decision:summon-queue-v1 §4)."""
    from fastapi.responses import JSONResponse

    from ..security.principal import current_gateway_principal

    principal = current_gateway_principal()
    caller_id = principal.user_id if principal is not None else "operator"
    out = _summon_core(
        name,
        req,
        caller_id=caller_id,
        caller_kind=normalize_caller_kind(req.caller_kind),
    )
    if out.get("queued"):
        # Queued admission is 202 (contract §2): accepted for processing,
        # not yet a run. The body carries queue_id/position/poll.
        return JSONResponse(status_code=202, content=out)
    return out


def _summon_core(
    name: str,
    req: SummonEntityRequest,
    *,
    caller_id: str,
    caller_kind: str,
    from_queue_id: Optional[str] = None,
    svc: Any = None,
    registry: Any = None,
) -> Dict[str, Any]:
    """The whole summon door, identity-explicit.

    Order matters and is non-negotiable (a2a 0004 constraints):
    1. Render the identity prelude (a PURE read over the home).
    2. A REFUSED prelude aborts the summon — 409 with the reasons verbatim,
       no run started, no fallback to a truncated identity header.
    3. Start the run with the reserved-seats posture stamped host-side
       (`self_fraction > 0` — identity is always present; the deposit gate
       enforces it on every recall) and the channel-derived actor. The run
       is parked outside the tick loop until the SIGNED stamp (bound to the
       run id) is persisted, so no effect can ever execute against the home
       without a verified stamp.

    `caller_id`/`caller_kind` are EXPLICIT (never read from request context)
    so queue admission runs under the ENQUEUER's identity, not whoever's
    request happened to tick the queue. `from_queue_id` marks a queued
    attempt: it must never re-enqueue itself (its seat refusal propagates to
    the admission executor, which keeps the entry queued).
    """
    from abstractruntime.identity import render_summon_prelude

    from ..entity_gate import (
        CHANNEL_WORKPLACE,
        finalize_summon_stamp,
        mint_summon_stamp,
        summon_budget_profile,
    )

    # Whitespace-only prompts are the empty stimulus min_length can't catch
    # (P1-3, boundary validation belongs to the boundary).
    if not str(req.prompt or "").strip():
        raise HTTPException(status_code=422, detail="summon prompt must not be empty or whitespace-only")

    # svc/registry may be threaded in by queue admission (a background
    # thread has no request context — re-resolving there would swap a
    # per-principal home for the base service's, the multi-user hole).
    svc = svc if svc is not None else get_gateway_service()
    registry = registry if registry is not None else _registry()

    try:
        home = registry.get_home(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if from_queue_id is not None:
        # QUEUED-ATTEMPT FAST PATH (door-cleanup audit P0-1/P2-4/P2-8): an
        # admission attempt against a still-held seat must cost one cheap
        # read — never a prelude render, never a wake write on an asleep
        # entity, and NEVER a summon_refused marker (the biography records
        # ACTS, not retries; poll/sweep cadence would flood the append-only
        # stream permanently). The authoritative re-check under the guard
        # lock still runs below for attempts that pass here.
        _pre_seat = seat_occupancy(svc.host.run_store, registry.entities_dir, home.manifest.slug)
        _pre = door_decision(
            _pre_seat,
            caller=caller_id,
            caller_kind=caller_kind,
            session_id=str(req.session_id or "").strip(),
        )
        if _pre["action"] == "refuse":
            assert _pre_seat is not None
            raise HTTPException(
                status_code=409,
                detail={
                    "refused": True,
                    "reasons": ["seat still held — the queued entry stays at head (attempt-not-grant)"],
                    "entity_id": home.entity_id,
                    "lane": "summon",
                    "live_run_id": str(_pre_seat.get("run_id") or ""),
                    "live_session_id": str(_pre_seat.get("session_id") or ""),
                    "retry_after_s": int(_pre.get("retry_after_s") or 0) or None,
                },
            )

    # THE LIVENESS GATE (laurent 16:06/16:12, decision:entity-liveness-axis):
    # reachability rides the liveness axis, not sleep. PAUSED = the kill
    # switch (stop) — every door refuses, this one included. ASLEEP-but-
    # ALIVE is REACHABLE: the summon WAKES the entity (the a2a 0008
    # no-summon window is formally RETIRED by his ruling — superseded in
    # the same wave that keeps the paused refusal, so there is never a gap
    # with neither protection). Work-completion ceremony (work→sleep) is
    # the work door's, when built.
    entity_state = registry.state_of(name)
    state_word = str(entity_state.get("state") or "awake")
    if state_word == "paused":
        raise HTTPException(
            status_code=409,
            detail={
                "refused": True,
                "reasons": [
                    f"#REFUSED summon: {home.entity_id} is paused — the kill switch"
                    + (f" ({entity_state.get('reason')})" if entity_state.get("reason") else "")
                    + "; an entity can only perform while alive (wake requires the operator)"
                ],
                "entity_id": home.entity_id,
                "state": entity_state,
            },
        )
    if state_word == "asleep":
        # VISITING POSTURE GUARD (mutual-exclusivity wave, audit finding 3 —
        # the /loop/start registration-window pattern applied): a summon's
        # wake-write over asleep+mode=visiting would destroy a live/mid-open
        # visit's durable marker. The visit owns the phase; refuse.
        if str(entity_state.get("mode") or "") == "visiting":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"a visit is open (or opening) on {name} — visiting posture set; "
                    "a summon and a visit never overlap (the phases are mutually exclusive); "
                    "retry after the visit ends"
                ),
            )
        from abstractruntime.identity.life import write_entity_state

        write_entity_state(
            registry.entities_dir / entity_slug(name), "awake",
            reason=f"woken by summon from person:{caller_id}",
        )
        entity_state = registry.state_of(name)

    try:
        spark = home.spark()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"attested spark unreadable: {e}")

    prelude = render_summon_prelude(
        home.memory,
        home.diary,
        entity_id=home.entity_id,
        budget=int(req.prelude_budget),
        spark=spark,
        diary_tail=int(req.diary_tail),
        gradation_top_k=int(req.gradation_top_k),
    )
    if prelude.get("refused"):
        # A refusal is part of the observable story (a2a 0005: prelude
        # moments are journal-invisible pure reads, so the gateway records
        # the host marker the stream interleaves).
        from ..entity_replay import record_host_marker

        record_host_marker(
            entities_dir=registry.entities_dir,
            slug=home.manifest.slug,
            entity_id=home.entity_id,
            kind="prelude_refused",
            journal_seq=int(prelude.get("as_of_seq") or 0),
            details={"reasons": list(prelude.get("warnings") or []), "budget": int(req.prelude_budget)},
        )
        # The refusal reason is written for humans — surface it verbatim.
        raise HTTPException(
            status_code=409,
            detail={
                "refused": True,
                "reasons": list(prelude.get("warnings") or []),
                "entity_id": home.entity_id,
                "spark_version": prelude.get("spark_version"),
            },
        )

    # The 20k context floor (maintainer round 8: "the minimal context should
    # be 20 000 tokens, never less"). Checked against DECLARED values; when
    # nothing is declared the summon proceeds with a labeled warning — the
    # gateway cannot measure a model it cannot resolve, so the floor becomes
    # the operator's to guarantee.
    from ..entity_gate import SUMMON_CONTEXT_FLOOR_TOKENS

    summon_warnings: List[str] = []
    declared_window = _declared_context_window(req, dict(req.input_data or {}))
    if declared_window is not None and declared_window < SUMMON_CONTEXT_FLOOR_TOKENS:
        raise HTTPException(
            status_code=409,
            detail={
                "refused": True,
                "reasons": [
                    f"#REFUSED summon: declared context window {declared_window} is below the "
                    f"{SUMMON_CONTEXT_FLOOR_TOKENS}-token floor for summoned-entity sessions "
                    "(maintainer ruling: 'the minimal context should be 20 000 tokens, never less')"
                ],
                "entity_id": home.entity_id,
            },
        )
    if declared_window is None:
        summon_warnings.append(
            "#FALLBACK context window undeclared (no context_window_tokens/max_in_tokens/_limits) — "
            f"the {SUMMON_CONTEXT_FLOOR_TOKENS}-token floor is the operator's to guarantee"
        )
    if registry.embedder_warning:
        # The home opened vectorless (memory's birth-audit ask: the state
        # must be labeled at the summon, never silent).
        summon_warnings.append(registry.embedder_warning)

    slug = home.manifest.slug
    session_id = str(req.session_id or "").strip() or f"entity-{slug}-{secrets.token_hex(4)}"

    # WHO is stamped by the door, never claimed by the payload (situation
    # contract): the verified participant is the authenticated principal
    # (threaded in as caller_id — for a QUEUED admission this is the
    # ENQUEUER, engraved at enqueue time, never whoever's request ticked
    # the queue); a local single-operator gateway (auth off) is the
    # operator. The entity stamps ITSELF into its own records (EXPLICIT
    # co-presence, ruled a2a 0007: the entity IS present at its own session
    # by construction — owners are never implied).
    participants: List[str] = [f"person:{caller_id}", home.entity_id]

    input_data: Dict[str, Any] = dict(req.input_data or {})
    input_data["prompt"] = req.prompt
    caller_system = str(input_data.get("system") or "").strip()
    input_data["system"] = prelude["text"] + (("\n\n" + caller_system) if caller_system else "")

    # ONE substrate per entity, NO code default (maintainer rulings
    # 2026-07-09 04:26/06:32; flow c5253 P1-1): a summon without explicit
    # provider/model used to fall through to the gateway CAPABILITY default
    # silently — the home's substrate.yaml never consulted, the substitution
    # named nowhere. The chain is the chat/loop lane's, from the SAME
    # resolver (request > home substrate.yaml > operator env > LOUD
    # REFUSAL), and the resolved pair is stamped into the run inputs AND the
    # response so the substrate is always caller-visible.
    from ..entity_chat import ChatOpenRefused, read_entity_substrate, resolve_substrate

    req_provider = str(input_data.get("provider") or "").strip()
    req_model = str(input_data.get("model") or "").strip()
    # Request-level reasoning effort: top-level keys OR a caller-seeded
    # _runtime.thinking — all three are the caller's ask, and folding the
    # _runtime spelling in here keeps the response's substrate block and
    # the run's actual value identical (adversary cycle-1 D1). Precedence:
    # thinking > reasoning > _runtime.thinking (explicit top-level beats a
    # seeded internal). Typed values normalize (cycle-2 N3): boolean false
    # is the explicit "none" spelling, boolean true means "auto" — an
    # or-chain would read both as absent and let the seed run unstamped.

    def _req_thinking_value(v: Any) -> str:
        if isinstance(v, bool):
            return "none" if v is False else "auto"
        if isinstance(v, str):
            return v.strip()
        # Typed garbage (dict, list, number) is unresolvable — returning ""
        # routes it to the pop branch below, so the run never carries a
        # value the response would misreport (cycle-3 F1).
        return ""

    _rt_seed = input_data.get("_runtime") if isinstance(input_data.get("_runtime"), dict) else {}
    req_thinking = ""
    for _container, _key in ((input_data, "thinking"), (input_data, "reasoning"), (_rt_seed, "thinking")):
        if _key in _container:
            req_thinking = _req_thinking_value(_container.get(_key))
            if req_thinking:
                break
    try:
        resolved_provider, resolved_model, resolved_thinking = resolve_substrate(
            req_provider, req_model, home_dir=home.home_dir, thinking=req_thinking or None
        )
    except ChatOpenRefused as e:
        raise HTTPException(
            status_code=e.status,
            detail={
                "refused": True,
                "reasons": [f"#REFUSED summon: {e.detail}"],
                "entity_id": home.entity_id,
            },
        )
    # Display-only source label (the CHAIN authority stays resolve_substrate):
    # which chain step filled each field, honest under mixed resolution
    # (e.g. provider from the request, model from the home).
    _stored = read_entity_substrate(home.home_dir)

    def _substrate_src(req_val: str, stored_val: str) -> str:
        if req_val:
            return "request"
        if stored_val:
            return "home substrate.yaml"
        return "operator env"

    _p_src = _substrate_src(req_provider, str(_stored.get("provider") or ""))
    _m_src = _substrate_src(req_model, str(_stored.get("model") or ""))
    substrate_source: Any = _p_src if _p_src == _m_src else {"provider": _p_src, "model": _m_src}
    input_data["provider"] = resolved_provider
    input_data["model"] = resolved_model
    substrate_block = {"provider": resolved_provider, "model": resolved_model, "source": substrate_source}
    if resolved_thinking:
        # Reasoning effort (the substrate's optional third field): stamped
        # into the run vars so every model call in the summon inherits it,
        # and shown in the response beside provider/model. ASSIGN, never
        # setdefault: the resolved value already folded every request
        # spelling, so run and response cannot diverge.
        _rt_in = input_data.get("_runtime")
        _rt: Dict[str, Any] = dict(_rt_in) if isinstance(_rt_in, dict) else {}
        _rt["thinking"] = resolved_thinking
        input_data["_runtime"] = _rt
        substrate_block["thinking"] = resolved_thinking
    elif isinstance(input_data.get("_runtime"), dict) and "thinking" in input_data["_runtime"]:
        # Nothing resolved but the caller seeded SOMETHING (empty string, a
        # non-normalizable type): drop it so no unresolved value runs while
        # the response shows none (the last crack of the D1 divergence).
        _rt = dict(input_data["_runtime"])
        _rt.pop("thinking", None)
        input_data["_runtime"] = _rt

    # OPERATOR ITERATIONS CEILING (laurent c786; seam (b) c805/c809): the
    # gateway serves `_limits.max_iterations_ceiling` into entity-run vars;
    # Runtime.start() is the ONE enforcement site (refuse-at-start when the
    # workflow declares above it, never mid-run truncation). The ceiling is
    # the OPERATOR'S word — it OVERWRITES any caller-passed value (a summon
    # request must not lower/raise operator policy); the caller's other
    # `_limits` keys pass through untouched (declared window etc.). Disabled
    # ceiling (env 0/off) = field absent = no enforcement, honestly.
    _ceiling = entity_iterations_ceiling()
    if _ceiling is not None:
        _limits_in = input_data.get("_limits")
        _limits: Dict[str, Any] = dict(_limits_in) if isinstance(_limits_in, dict) else {}
        _limits["max_iterations_ceiling"] = int(_ceiling)
        input_data["_limits"] = _limits

    # The session's default recall budget: memory's context-scaled profile
    # with the reserved-seats posture applied; every in-session recall that
    # omits a budget runs on it (the gate injects from the stamp).
    budget_profile = summon_budget_profile(declared_window)

    # ONE LIFE, ONE CONVERSATION — the seat (conversation-seat plan, slice 2;
    # foundation flow c5260 P1-B). The per-slug lock spans check -> preempt
    # -> start -> record so two concurrent summons cannot both pass, and a
    # preempt's cancel + takeover is atomic against a racing summon.
    # (caller_id / caller_kind arrive as parameters — identity is explicit.)

    def _refuse_summon(
        *,
        reason: str,
        holding: Dict[str, Any],
        retry_after: Optional[int],
        lane: str,
    ) -> None:
        # summon_refused host marker (item 3): the refusal enters the
        # biography — "who was turned away while whom held the seat"
        # answerable from the stream, not only from runtime/audit_log.jsonl.
        # Written ONLY on the real conflict (never speculatively); the
        # refused MESSAGE text is not recorded (the mailbox holds words;
        # this marker holds the act). QUEUED ATTEMPTS never mark (audit
        # P0-1): a retry is not an act — the queue recorded the wait at
        # enqueue; this belt covers the race where the fast path passed but
        # the locked re-check refuses.
        if from_queue_id is not None:
            detail0: Dict[str, Any] = {
                "refused": True,
                "reasons": [reason],
                "entity_id": home.entity_id,
                "lane": lane,
                "live_run_id": str(holding.get("run_id") or ""),
                "live_session_id": str(holding.get("session_id") or ""),
            }
            if retry_after is not None:
                detail0["retry_after_s"] = int(retry_after)
            raise HTTPException(status_code=409, detail=detail0)
        try:
            from ..entity_replay import record_host_marker

            record_host_marker(
                entities_dir=registry.entities_dir,
                slug=home.manifest.slug,
                entity_id=home.entity_id,
                kind="summon_refused",
                journal_seq=int(prelude.get("as_of_seq") or 0),
                session_id=session_id,
                details={
                    "lane": lane,
                    "holding_run_id": str(holding.get("run_id") or ""),
                    "holding_session_id": str(holding.get("session_id") or ""),
                    "holding_status": str(holding.get("status") or ""),
                    "holder": str(holding.get("holder") or ""),
                    "holder_kind": str(holding.get("holder_kind") or ""),
                    "refused_session_id": session_id,
                    "refused_caller_kind": caller_kind,
                    "refusing_principal": caller_id,
                },
            )
        except Exception:
            logger.warning("summon_refused marker failed for %s (refusal still returned)", home.manifest.slug, exc_info=True)
        detail: Dict[str, Any] = {
            "refused": True,
            "reasons": [reason],
            "entity_id": home.entity_id,
            "lane": lane,
            "live_run_id": str(holding.get("run_id") or ""),
            "live_session_id": str(holding.get("session_id") or ""),
        }
        headers = None
        if retry_after is not None:
            detail["retry_after_s"] = int(retry_after)
            headers = {"Retry-After": str(int(retry_after))}
        raise HTTPException(status_code=409, detail=detail, headers=headers)

    # CROSS-LANE note (item 5): summon-vs-visit/chat is refused ABOVE by the
    # visiting-posture gate (asleep+mode=visiting, mutual-exclusivity wave) —
    # every visit/chat open writes that posture unconditionally, so no host
    # probe is needed here. This guard owns summon-vs-summon: the seat.
    guard_lock = _summon_guard_lock(home.manifest.slug)
    with guard_lock:
        seat = seat_occupancy(svc.host.run_store, registry.entities_dir, home.manifest.slug)
        decision = door_decision(seat, caller=caller_id, caller_kind=caller_kind, session_id=session_id)
        seat_held_since: Optional[str] = None
        if decision["action"] == "refuse":
            assert seat is not None
            if bool(req.queue) and from_queue_id is None:
                # QUEUED ADMISSION (decision:summon-queue-v1): the caller
                # opted to WAIT — store the FULL payload; the door executes
                # it at admission (the client never resubmits, contract §4).
                # Only the SEAT lane queues: paused/visiting/prelude/floor
                # refusals above stay loud 409s (a queue does not wait out a
                # kill switch). A queued attempt (from_queue_id set) never
                # reaches here — it re-raises to the admission executor.
                return _enqueue_summon(
                    svc, registry, home,
                    req=req, caller_id=caller_id, caller_kind=caller_kind,
                    seat=seat, prelude_seq=int(prelude.get("as_of_seq") or 0),
                )
            live_bit = "live run" if seat.get("run_live") else f"idle, seat held {seat.get('ttl_remaining_s')}s more"
            _refuse_summon(
                reason=(
                    f"one life, one summon: {home.entity_id}'s seat is held by "
                    f"{seat.get('holder') or 'unknown'} ({seat.get('holder_kind')}) — "
                    f"session {seat.get('session_id')}, {live_bit}; retry after retry_after_s "
                    "(humans may preempt an agent-held seat by declaring caller_kind=human, "
                    "or wait in line by declaring queue=true)"
                ),
                holding=seat,
                retry_after=int(decision.get("retry_after_s") or 0) or None,
                lane="summon",
            )
        elif decision["action"] == "slide":
            # The same conversation continuing (same session + same holder):
            # the seat's held_since survives the per-turn run churn.
            assert seat is not None
            seat_held_since = str(seat.get("held_since") or "") or None
        elif decision["action"] == "preempt":
            # MACHINERY YIELDS TO HUMANS (item 2): a human summon takes the
            # seat from an agent/unknown holder. A LIVE holder run is
            # cancelled at the turn boundary — runtime's terminal-guarded
            # cancel_run + the tick loop's between-steps abort ARE the
            # semantics (their section, c5399); formations already lived
            # stand (append-only stores). An idle TTL-held seat is taken
            # without a cancel. The marker lands BEFORE the takeover
            # proceeds so a crash mid-preempt leaves the recorded intent.
            assert seat is not None
            cancelled_runs: list[str] = []
            if bool(seat.get("run_live")):
                try:
                    cancelled_runs = cancel_run_tree(
                        svc.host.runtime,
                        svc.host.run_store,
                        str(seat.get("run_id") or ""),
                        reason=(
                            f"preempted: human summon by {caller_id} on {home.entity_id} "
                            f"(seat holder {seat.get('holder') or 'unknown'}/{seat.get('holder_kind')})"
                        ),
                    )
                except Exception:
                    logger.warning("preempt cancel failed for %s (takeover proceeds)", home.manifest.slug, exc_info=True)
            try:
                from ..entity_replay import record_host_marker

                record_host_marker(
                    entities_dir=registry.entities_dir,
                    slug=home.manifest.slug,
                    entity_id=home.entity_id,
                    kind="seat_preempted",
                    journal_seq=int(prelude.get("as_of_seq") or 0),
                    session_id=session_id,
                    details={
                        "preempted_run_id": str(seat.get("run_id") or ""),
                        "preempted_session_id": str(seat.get("session_id") or ""),
                        "holder": str(seat.get("holder") or ""),
                        "holder_kind": str(seat.get("holder_kind") or ""),
                        "holding_status": "live" if seat.get("run_live") else "ttl_held",
                        "cancelled_runs": cancelled_runs,
                        "preempting_principal": caller_id,
                        "preempting_session_id": session_id,
                    },
                )
            except Exception:
                logger.warning("seat_preempted marker failed for %s (takeover proceeds)", home.manifest.slug, exc_info=True)

        stamp = mint_summon_stamp(
            data_dir=registry.data_dir,
            entity_id=home.entity_id,
            channel=CHANNEL_WORKPLACE,
            session_id=session_id,
            participants=participants,
            prelude_as_of_seq=prelude.get("as_of_seq"),
            budget_profile=budget_profile,
        )

        try:
            # Parked actor: the runner only ticks actor_id == "gateway", so the
            # run (and any listener children) is invisible to the tick loop
            # until the finalized, run-bound stamp is saved below. A crash in
            # this window leaves an inert parked run — a failed summon, never a
            # half-stamped session.
            run_id = svc.host.start_run(
                flow_id=str(req.flow_id or ""),
                bundle_id=req.bundle_id,
                bundle_version=req.bundle_version,
                input_data=input_data,
                actor_id="gateway:summon-pending",
                session_id=session_id,
            )
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e).strip("'\""))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to start summoned run: {e}")

        final_stamp = finalize_summon_stamp(stamp, data_dir=registry.data_dir, run_id=str(run_id))

        run_store = svc.host.run_store
        from abstractruntime.core.runtime import utc_now_iso

        to_unpark = [str(run_id)]
        list_children = getattr(run_store, "list_children", None)
        if callable(list_children):
            try:
                for child in list_children(parent_run_id=str(run_id)) or []:
                    cid = getattr(child, "run_id", None)
                    if isinstance(cid, str) and cid:
                        to_unpark.append(cid)
            except Exception:
                pass
        for rid in to_unpark:
            run = run_store.load(rid)
            if run is None:
                continue
            vars_obj = getattr(run, "vars", None)
            if not isinstance(vars_obj, dict):
                vars_obj = {}
                run.vars = vars_obj  # type: ignore[attr-defined]
            runtime_ns = vars_obj.get("_runtime")
            if not isinstance(runtime_ns, dict):
                runtime_ns = {}
                vars_obj["_runtime"] = runtime_ns
            runtime_ns["entity"] = dict(final_stamp)
            runtime_ns["run_mode"] = "summon"
            run.actor_id = "gateway"  # type: ignore[attr-defined]
            run.updated_at = utc_now_iso()  # type: ignore[attr-defined]
            run_store.save(run)

        # Take (or slide) the seat LAST — after the actor flip — so a crash
        # in the parked window (an inert run the tick loop never sees) can
        # never hold the one-life guard against future summons. holder = the
        # summoning principal; holder_kind = the caller's DECLARATION
        # ('unknown' when undeclared — reads as agent, preemptable, until
        # GW-H per-agent principals make it structural). A slide preserves
        # held_since: one conversation, one seat, many per-turn runs.
        record_seat(
            registry.entities_dir,
            home.manifest.slug,
            run_id=str(run_id),
            session_id=session_id,
            holder=caller_id,
            holder_kind=(req.caller_kind or "unknown"),
            held_since=seat_held_since,
        )

    svc.runner.start()

    # The summon moment enters the observable story as a host marker
    # (written after the stamp flip so the marker names a live run).
    from ..entity_replay import record_host_marker

    record_host_marker(
        entities_dir=registry.entities_dir,
        slug=home.manifest.slug,
        entity_id=home.entity_id,
        kind="summon",
        journal_seq=int(prelude.get("as_of_seq") or 0),
        run_id=str(run_id),
        session_id=session_id,
        details={
            "channel": CHANNEL_WORKPLACE,
            "participants": participants,
            "prelude_section_tokens": prelude.get("section_tokens"),
            "prelude_warnings": list(prelude.get("warnings") or []),
            "context_window_tokens": declared_window,
            "warnings": summon_warnings,
            "substrate": substrate_block,
        },
    )

    return {
        "run_id": str(run_id),
        "session_id": session_id,
        "entity_id": home.entity_id,
        "channel": CHANNEL_WORKPLACE,
        "participants": participants,
        "context_window_tokens": declared_window,
        "warnings": summon_warnings,
        "substrate": substrate_block,
        "prelude": {
            "text": prelude["text"],
            "section_tokens": prelude.get("section_tokens"),
            "as_of_seq": prelude.get("as_of_seq"),
            "spark_version": prelude.get("spark_version"),
            "warnings": list(prelude.get("warnings") or []),
        },
    }


# ----------------------------------------------------------- the visit queue
# decision:summon-queue-v1 (sealed 2026-07-25, room summon-queue-design;
# commons receipt c5625). The STORE lives in entity_queue.py; this block owns
# the door logic: enqueue (from _summon_core's refuse arm), the ADMISSION
# EXECUTOR (re-runs the whole summon path for the stored payload under the
# enqueuer's identity), the poll/leave endpoints, and the sweep the backstop
# clock calls. Runtime's invariants bind here: the lease is untouched
# (admission = the right to ATTEMPT; the executed summon acquires it like a
# fresh one), nothing is ever held for a waiter, close-paths fire the head
# with the sweeper as backstop, and admission is idempotent per queue_id
# (the guard RLock spans head-read -> summon-core, so racing tickers cannot
# double-admit).


def _fire_queue_admission(name: str) -> None:
    """Best-effort fire-and-forget head admission (the close hooks' shape).
    The service + registry are captured AT REQUEST TIME so a per-principal
    close admits against its own homes, not the base service's (the
    background thread has no request context)."""
    try:
        svc = get_gateway_service()
        registry = _registry()
    except Exception:
        return

    def _run() -> None:
        try:
            _attempt_queue_admission(name, svc=svc, registry=registry)
        except Exception:
            logger.warning("close-fired queue admission failed for %s (sweeper backstops)", name, exc_info=True)

    threading.Thread(target=_run, name=f"queue-admit-{name}", daemon=True).start()


def _queue_marker(registry: Any, home: Any, kind: str, *, journal_seq: int, details: Dict[str, Any]) -> None:
    """Queue acts are census rows (contract §14): the ACT — who, when,
    position — never the message words (the queue store holds words)."""
    try:
        from ..entity_replay import record_host_marker

        record_host_marker(
            entities_dir=registry.entities_dir,
            slug=home.manifest.slug,
            entity_id=home.entity_id,
            kind=kind,
            journal_seq=journal_seq,
            details=details,
        )
    except Exception:
        logger.warning("%s marker failed for %s (act still stands)", kind, home.manifest.slug, exc_info=True)


def _seat_idle_deadline(seat: Optional[Dict[str, Any]]) -> Optional[str]:
    """The ONLY ETA the contract allows (§2): the held seat's idle ceiling —
    a fact about config, never a prediction. None while the run is live
    (visit lengths are unbounded; an invented number is a lie)."""
    if seat is None or bool(seat.get("run_live")):
        return None
    remaining = int(seat.get("ttl_remaining_s") or 0)
    if remaining <= 0:
        return None
    return (_datetime.datetime.now(_datetime.timezone.utc) + _datetime.timedelta(seconds=remaining)).isoformat()


def _enqueue_summon(
    svc: Any,
    registry: Any,
    home: Any,
    *,
    req: SummonEntityRequest,
    caller_id: str,
    caller_kind: str,
    seat: Dict[str, Any],
    prelude_seq: int,
) -> Dict[str, Any]:
    """Store the summon as a queue entry (called under the guard RLock from
    _summon_core's refuse arm). The FULL payload rests in the entry — the
    door executes it at admission; a park entry IS the mailbox drop."""
    from ..entity_queue import (
        QUEUE_MAX_PER_HOME,
        ensure_queue_sweeper,
        new_entry,
        position_of,
        queued_entries,
        read_queue,
        write_queue,
    )

    slug = home.manifest.slug
    entries = read_queue(registry.entities_dir, slug)
    if len(queued_entries(entries)) >= QUEUE_MAX_PER_HOME:
        # Loud cap (contract §19): refused at enqueue, never silently dropped.
        raise HTTPException(
            status_code=409,
            detail={
                "refused": True,
                "reasons": [
                    f"the door queue for {home.entity_id} is full "
                    f"({QUEUE_MAX_PER_HOME} waiting) — retry later or leave the queue to the operator"
                ],
                "entity_id": home.entity_id,
            },
        )
    # The stored payload's session id is minted AT ENQUEUE when absent
    # (audit P1-2): admission reconciliation after a crash needs a
    # DETERMINISTIC session to match the seat record against — a session
    # minted inside the core at execute time is unknowable to the
    # reconciler. Same shape the core mints.
    stored = req.model_dump()
    if not str(stored.get("session_id") or "").strip():
        stored["session_id"] = f"entity-{slug}-{secrets.token_hex(4)}"
    entry = new_entry(
        payload=stored,
        caller=caller_id,
        caller_kind=caller_kind,
        park=bool(req.park),
    )
    entries.append(entry)
    write_queue(registry.entities_dir, slug, entries)
    position = position_of(entries, entry["queue_id"]) or len(queued_entries(entries))
    _queue_marker(
        registry, home, "queue_enqueued",
        journal_seq=prelude_seq,
        details={
            "queue_id": entry["queue_id"],
            "position": position,
            "caller": caller_id,
            "caller_kind": caller_kind,
            "park": bool(req.park),
            "behind_session_id": str(seat.get("session_id") or ""),
        },
    )
    ensure_queue_sweeper(registry.entities_dir, context=(svc, registry))
    return {
        "queued": True,
        "queue_id": entry["queue_id"],
        "position": position,
        "park": bool(req.park),
        "entity_id": home.entity_id,
        "current_idle_deadline": _seat_idle_deadline(seat),
        "poll": f"/api/gateway/entities/{home.manifest.slug}/queue/{entry['queue_id']}",
    }


def _is_contention_refusal(detail: Any) -> Optional[str]:
    """Discriminate CONTENTION (the seat/life is held — the design working,
    entry stays queued) from a refusal that can never admit (paused kill
    switch, prelude refusal, context floor, substrate — mark failed; audit
    P1-1: status-code alone is the wrong axis because the door speaks 409
    for both classes). Returns 'seat' | 'visit' | None(=not contention).
    'visit' also feeds the card's waiting_behind (entity room#12)."""
    if isinstance(detail, dict) and str(detail.get("lane") or "") == "summon":
        return "seat"
    text = str(detail).lower()
    if "visit is open" in text or "visiting posture" in text:
        return "visit"
    return None


def _mark_admitted(registry: Any, home: Any, entries: Any, head: Dict[str, Any], *, run_id: str, session_id: str, prelude_seq: int) -> None:
    from ..entity_queue import write_queue

    head["state"] = "admitted"
    head["admitted_run_id"] = run_id
    head["admitted_session_id"] = session_id
    head["waiting_behind"] = None
    write_queue(registry.entities_dir, home.manifest.slug, entries)
    _queue_marker(
        registry, home, "queue_admitted",
        journal_seq=prelude_seq,
        details={
            "queue_id": head["queue_id"],
            "run_id": run_id,
            "session_id": session_id,
            "caller": head.get("caller"),
            "caller_kind": head.get("caller_kind"),
            "park": bool(head.get("park")),
            "attempts": head.get("attempts"),
        },
    )


def _attempt_queue_admission(name: str, *, svc: Any = None, registry: Any = None) -> None:
    """Try to admit the queue head (idempotent per queue_id — the RLock
    spans head-read -> summon-core in-process, and the ADMITTING intent
    write + seat reconciliation cover process death; audit P1-2).
    Every caller is best-effort: polls, close paths, and the sweeper all
    converge here; one attempt per free seat wins, the rest see the state."""
    from ..entity_queue import head_entry, read_queue, write_queue

    svc = svc if svc is not None else get_gateway_service()
    registry = registry if registry is not None else _registry()
    try:
        home = registry.get_home(name)
    except Exception:
        return  # an unresolvable home cannot admit; the entry stays for the operator
    slug = home.manifest.slug
    with _summon_guard_lock(slug):
        entries = read_queue(registry.entities_dir, slug)
        # CRASH RECONCILIATION (audit P1-2): a previous attempt that died
        # between the run start and the admitted write left an entry in
        # 'admitting' — it takes precedence over any queued entry behind it
        # (head_entry only sees queued states). The seat record is the
        # truth: if the seat carries the entry's deterministic session +
        # holder, the run WAS minted — mark admitted idempotently instead
        # of executing the prompt twice.
        head = None
        for e in entries:
            if str(e.get("state") or "") == "admitting":
                head = e
                break
        if head is None:
            head = head_entry(entries)
        if head is None:
            return
        if str(head.get("state") or "") == "admitting":
            stored_session = str((head.get("payload") or {}).get("session_id") or "")
            seat_now = seat_occupancy(svc.host.run_store, registry.entities_dir, slug)
            if (
                seat_now is not None
                and stored_session
                and str(seat_now.get("session_id") or "") == stored_session
                and str(seat_now.get("holder") or "") == str(head.get("caller") or "")
            ):
                _mark_admitted(
                    registry, home, entries, head,
                    run_id=str(seat_now.get("run_id") or ""),
                    session_id=stored_session,
                    prelude_seq=int(home.memory.current_seq()),
                )
                return
            # No matching seat: the previous attempt died BEFORE the mint —
            # fall through and execute (state resets to queued via the
            # normal write below on contention, or admits).
        try:
            payload = dict(head.get("payload") or {})
            payload["queue"] = False
            payload["park"] = False
            req = SummonEntityRequest(**payload)
        except Exception as e:
            head["state"] = "failed"
            head["failed_reason"] = f"stored payload no longer parses: {e}"
            write_queue(registry.entities_dir, slug, entries)
            _queue_marker(
                registry, home, "queue_reaped",
                journal_seq=int(home.memory.current_seq()),
                details={"queue_id": head["queue_id"], "reason": "payload unparsable", "caller": head.get("caller")},
            )
            return
        head["attempts"] = int(head.get("attempts") or 0) + 1
        head["last_attempt_at"] = _now_iso()
        # INTENT WRITE (audit P1-2): 'admitting' rests durably BEFORE the
        # core executes, so a crash mid-execution is reconcilable above.
        head["state"] = "admitting"
        write_queue(registry.entities_dir, slug, entries)
        try:
            out = _summon_core(
                name,
                req,
                caller_id=str(head.get("caller") or "operator"),
                caller_kind=str(head.get("caller_kind") or "agent"),
                from_queue_id=str(head.get("queue_id")),
                svc=svc,
                registry=registry,
            )
        except HTTPException as e:
            contention = _is_contention_refusal(e.detail)
            if contention is not None:
                # The seat/life is held: the design working — the entry
                # stays queued at head (attempt-not-grant, invariant 9).
                head["state"] = "queued"
                head["waiting_behind"] = "visit" if contention == "visit" else None
                write_queue(registry.entities_dir, slug, entries)
                return
            # A refusal that can never admit (paused kill switch, prelude
            # refusal, floor, substrate) — mark failed honestly instead of
            # clogging the head forever. The poll serves the reason verbatim.
            head["state"] = "failed"
            head["failed_reason"] = str(e.detail)
            write_queue(registry.entities_dir, slug, entries)
            _queue_marker(
                registry, home, "queue_reaped",
                journal_seq=int(home.memory.current_seq()),
                details={"queue_id": head["queue_id"], "reason": "admission refused (non-contention)", "caller": head.get("caller")},
            )
            return
        except Exception:
            # A non-HTTP failure (store hiccup, registry error): back to
            # queued — the next tick retries; never strand 'admitting'
            # without a mint (the reconciler would just fall through, but
            # honest state beats a misleading one).
            head["state"] = "queued"
            write_queue(registry.entities_dir, slug, entries)
            raise
        _mark_admitted(
            registry, home, entries, head,
            run_id=str(out.get("run_id") or ""),
            session_id=str(out.get("session_id") or ""),
            prelude_seq=int((out.get("prelude") or {}).get("as_of_seq") or 0),
        )


def _queue_sweep(slug: str, context: Any = None) -> None:
    """The backstop clock's per-home pass (contract invariant 11): reap
    poll-silent non-park entries, then attempt the head. `context` is the
    OWNING (svc, registry) pair registered with the swept dir (audit P1-4 —
    the sweeper thread has no request principal, and a context-free
    get_gateway_service() always answers the BASE service; that fallback
    stays correct for the base dir only). Per-principal dirs get their
    context from door touches; after a bounce with zero traffic, a
    per-principal parked entry admits on that principal's next door touch
    (documented residual — principal services build lazily and cannot be
    resolved from a thread)."""
    from ..entity_queue import read_queue, reap_poll_silent, write_queue

    if isinstance(context, tuple) and len(context) == 2:
        svc, registry = context
    else:
        svc, registry = get_gateway_service(), _registry()
    try:
        home = registry.get_home(slug)
    except Exception:
        return
    lock = _summon_guard_lock(home.manifest.slug)
    # P2-1: the backstop must never wedge behind one home's slow summon —
    # a bounded wait skips this tick; the next tick retries.
    if not lock.acquire(timeout=5.0):
        return
    try:
        entries = read_queue(registry.entities_dir, home.manifest.slug)
        reaped = reap_poll_silent(entries)
        if reaped:
            write_queue(registry.entities_dir, home.manifest.slug, entries)
            for e in reaped:
                _queue_marker(
                    registry, home, "queue_reaped",
                    journal_seq=int(home.memory.current_seq()),
                    details={"queue_id": e.get("queue_id"), "reason": e.get("failed_reason"), "caller": e.get("caller")},
                )
    finally:
        lock.release()
    _attempt_queue_admission(slug, svc=svc, registry=registry)


# The sweeper thread calls this for every slug with a queue file; wired at
# import (entity_queue never imports the routes — no cycle).
from ..entity_queue import set_queue_sweep_executor as _set_queue_sweep_executor  # noqa: E402

_set_queue_sweep_executor(_queue_sweep)


def _queue_entry_view(entries: Any, entry: Dict[str, Any], seat: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    from ..entity_queue import position_of

    view: Dict[str, Any] = {
        "queue_id": entry.get("queue_id"),
        "state": entry.get("state"),
        "position": position_of(entries, str(entry.get("queue_id") or "")),
        "park": bool(entry.get("park")),
        "waiting_behind": entry.get("waiting_behind"),
        "current_idle_deadline": _seat_idle_deadline(seat),
        "enqueued_at": entry.get("enqueued_at"),
        "attempts": entry.get("attempts"),
    }
    if entry.get("admitted_run_id"):
        view["run_id"] = entry.get("admitted_run_id")
        view["session_id"] = entry.get("admitted_session_id")
    if entry.get("failed_reason"):
        view["reason"] = entry.get("failed_reason")
    return view


@router.get("/{name}/queue/{queue_id}")
def poll_queue_entry(name: str, queue_id: str) -> Dict[str, Any]:
    """The waiter's poll (contract §3). POLL-DRIVEN ADMISSION: a poll on a
    queued head attempts admission synchronously — the waiting client's own
    cadence drives the queue, and the sweeper covers everyone else. Each
    poll renews the entry's liveness (poll-silent non-park entries reap)."""
    from ..entity_queue import ensure_queue_sweeper, find_entry, read_queue, write_queue

    svc = get_gateway_service()
    registry = _registry()
    try:
        home = registry.get_home(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    slug = home.manifest.slug
    ensure_queue_sweeper(registry.entities_dir, context=(svc, registry))
    with _summon_guard_lock(slug):
        entries = read_queue(registry.entities_dir, slug)
        entry = find_entry(entries, queue_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"no queue entry {queue_id!r} on {home.entity_id}")
        if str(entry.get("state") or "") == "queued":
            entry["last_poll_at"] = _now_iso()
            write_queue(registry.entities_dir, slug, entries)
    # Attempt OUTSIDE the read block (the executor takes the lock itself;
    # RLock makes the nesting safe either way, but the re-read below must
    # see the attempt's outcome). Wrapped (audit P2-7): a non-HTTP failure
    # in the HEAD entry's admission must not 500 the innocent poller — the
    # poll's job is the entry's state, which the re-read serves.
    try:
        _attempt_queue_admission(name, svc=svc, registry=registry)
    except Exception:
        logger.warning("poll-driven admission failed for %s (poll still serves state)", name, exc_info=True)
    with _summon_guard_lock(slug):
        entries = read_queue(registry.entities_dir, slug)
        entry = find_entry(entries, queue_id)
        if entry is None:  # pragma: no cover - removed between locks (never happens: entries are marked, not deleted)
            raise HTTPException(status_code=404, detail=f"no queue entry {queue_id!r} on {home.entity_id}")
        seat = seat_occupancy(svc.host.run_store, registry.entities_dir, slug)
        view = _queue_entry_view(entries, entry, seat)
    view["entity_id"] = home.entity_id
    return view


@router.post("/{name}/queue/{queue_id}/leave")
def leave_queue(name: str, queue_id: str) -> Dict[str, Any]:
    """The explicit step-away (contract §6): a polite dequeue — the client
    button's verb. Tab death is the reaper's job, never a stranded slot."""
    from ..entity_queue import find_entry, read_queue, write_queue

    registry = _registry()
    try:
        home = registry.get_home(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    slug = home.manifest.slug
    with _summon_guard_lock(slug):
        entries = read_queue(registry.entities_dir, slug)
        entry = find_entry(entries, queue_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"no queue entry {queue_id!r} on {home.entity_id}")
        state = str(entry.get("state") or "")
        if state == "queued":
            entry["state"] = "stepped_away"
            write_queue(registry.entities_dir, slug, entries)
            # The marker distinguishes WHO ACTED from whose entry it was
            # (audit P2-5): the queue_id is a capability token, so another
            # principal holding it can dequeue — the biography must not say
            # "X stepped away" when Y removed X.
            from ..security.principal import current_gateway_principal

            _actor = current_gateway_principal()
            _queue_marker(
                registry, home, "queue_stepped_away",
                journal_seq=int(home.memory.current_seq()),
                details={
                    "queue_id": queue_id,
                    "caller": entry.get("caller"),
                    "by": (_actor.user_id if _actor is not None else "operator"),
                },
            )
        view = _queue_entry_view(entries, entry, None)
    view["entity_id"] = home.entity_id
    return view


# --------------------------------------------------------------- workspace
# The operator's window into the entity's territory (maintainer ask,
# 2026-07-08): browse + read the home workspace and any whitelisted mounts.
# Reads ride the dev read posture like every observer surface; the mounts
# WHITELIST is a write (operator-authed by the middleware) — the entity
# never edits its own walls.


def _workspace_root(name: str):
    from abstractruntime.identity.tools import WorkspaceRoot

    registry = _registry()
    manifest = registry.manifest_for(name)  # KeyError -> 404 below
    return WorkspaceRoot(registry.entities_dir / manifest.slug)


@router.get("/{name}/workspace")
def list_entity_workspace(name: str, path: str = ".") -> Dict[str, Any]:
    """Structured listing of one directory level (files + dirs + mounts at
    the root). `path` is workspace-relative; `mounts/<name>/…` browses a
    whitelisted mount. Containment errors read as 400, absence as 404."""
    try:
        ws = _workspace_root(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    rel = (path or ".").strip() or "."
    try:
        base, writable, mount = ws._route(rel)  # noqa: SLF001 - the router IS the host
    except PermissionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if not base.exists():
        raise HTTPException(status_code=404, detail=f"{rel!r} does not exist in the workspace")
    if base.is_file():
        raise HTTPException(status_code=400, detail=f"{rel!r} is a file - fetch it via /workspace/file")
    entries: List[Dict[str, Any]] = []
    try:
        children = sorted(base.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"cannot list {rel!r}: {e}")
    prefix = "" if rel in (".", "") else rel.rstrip("/") + "/"
    for child in children[:500]:
        entry: Dict[str, Any] = {
            "name": child.name,
            "path": f"{prefix}{child.name}",
            "kind": "file" if child.is_file() else "dir",
        }
        if child.is_file():
            try:
                entry["size"] = child.stat().st_size
            except OSError:
                entry["size"] = None
        entries.append(entry)
    if rel in (".", ""):
        for m in ws.mounts():
            entries.append({
                "name": m["name"],
                "path": f"mounts/{m['name']}",
                "kind": "mount",
                "mode": m["mode"],
                "target": m["path"],
            })
    return {"path": rel, "writable": bool(writable), "mount": mount or None, "entries": entries}


@router.get("/{name}/workspace/file")
def read_entity_workspace_file(name: str, path: str) -> Dict[str, Any]:
    """One file's text (capped at the workspace read cap, truncation labeled)."""
    from abstractruntime.identity.tools import WORKSPACE_FILE_CAP_BYTES

    try:
        ws = _workspace_root(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    try:
        resolved, _writable, _mount = ws._route(str(path or "").strip())  # noqa: SLF001
    except PermissionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail=f"no file at {path!r} in the workspace")
    data = resolved.read_bytes()
    truncated = len(data) > WORKSPACE_FILE_CAP_BYTES
    text = data[:WORKSPACE_FILE_CAP_BYTES].decode("utf-8", errors="replace")
    return {"path": path, "size": len(data), "truncated": truncated, "text": text}


class WorkspaceFileWriteRequest(BaseModel):
    path: str = Field(..., description="Workspace-relative destination (writable area only)")
    content_base64: str = Field(..., description="File bytes, base64-encoded (binary-safe: images, PDFs)")


@router.post("/{name}/workspace/file")
def write_entity_workspace_file(name: str, req: WorkspaceFileWriteRequest) -> Dict[str, Any]:
    """Place a file into the entity's writable workspace (maintainer ask,
    2026-07-09: 'send files to the entity when we need' — the operator's
    side of the drag-and-drop). Binary-safe (base64). Containment + the
    read-only/writable routing are the SAME as the entity's own tools use
    (`WorkspaceRoot._route`), so this door can never write outside the
    workspace nor into a read-only mount. The entity then reads it with
    its ordinary read_file — the file lands where its tools already reach."""
    import base64

    from abstractruntime.identity.tools import WORKSPACE_FILE_CAP_BYTES

    try:
        ws = _workspace_root(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    rel = str(req.path or "").strip()
    if not rel:
        raise HTTPException(status_code=400, detail="a destination path is required")
    try:
        data = base64.b64decode(req.content_base64, validate=True)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"content_base64 is not valid base64: {e}")
    if len(data) > WORKSPACE_FILE_CAP_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file is {len(data)} bytes, over the {WORKSPACE_FILE_CAP_BYTES}-byte workspace cap — split it",
        )
    try:
        resolved, writable, mount = ws._route(rel)  # noqa: SLF001 - the router IS the host
    except PermissionError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if not writable:
        raise HTTPException(status_code=400, detail=f"mount {mount!r} is read-only — the operator may not write there")
    if resolved == ws.root or (mount and resolved == ws.resolve(f"mounts/{mount}")):
        raise HTTPException(status_code=400, detail="a file path is required, not a workspace root")
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_bytes(data)
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"could not write {rel!r}: {e}")
    return {"path": rel, "size": len(data), "written": True}


class WorkspaceMount(BaseModel):
    name: str = Field(..., description="Simple label (appears as mounts/<name>/)")
    path: str = Field(..., description="Absolute directory on the gateway host")
    mode: str = Field(..., description="ro (read-only) or rw (read+write)")


class PutMountsRequest(BaseModel):
    mounts: List[WorkspaceMount] = Field(default_factory=list)


@router.get("/{name}/workspace/mounts")
def get_entity_workspace_mounts(name: str) -> Dict[str, Any]:
    from abstractruntime.identity.tools import read_workspace_mounts

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    return {"mounts": read_workspace_mounts(registry.entities_dir / manifest.slug)}


@router.put("/{name}/workspace/mounts")
def put_entity_workspace_mounts(name: str, req: PutMountsRequest) -> Dict[str, Any]:
    """Replace the whitelist (operator write). Validation is loud: names
    unique, paths existing directories, mode ro|rw. The entity's tools see
    the new walls on their next call — grants follow the file."""
    from abstractruntime.identity.tools import read_workspace_mounts, write_workspace_mounts

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    home_dir = registry.entities_dir / manifest.slug
    # Cross-home guard (belt to the runtime's braces): no mount may reach
    # ANY entity home — a grant into a sibling's book/memory is not a
    # workspace, it is a wall breach.
    from pathlib import Path as _Path

    entities_root = registry.entities_dir.resolve()
    for m in req.mounts:
        resolved = _Path(m.path).expanduser().resolve()
        if resolved == entities_root or entities_root in resolved.parents or resolved in entities_root.parents:
            raise HTTPException(
                status_code=400,
                detail=f"mount {m.name!r} path {m.path!r} overlaps the entity homes at {entities_root} - never mountable",
            )
    try:
        write_workspace_mounts(home_dir, [m.model_dump() for m in req.mounts])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"mounts": read_workspace_mounts(home_dir)}


# -------------------------------------------------------------- tool policy
# Per-phase tool grants (maintainer's two-tier ruling): read the resolved
# matrix, write an explicit per-phase list. The file is the operator's word
# (<home>/tool_policy.yaml); sessions resolve it at summon time.


class PutToolPolicyRequest(BaseModel):
    policy: Dict[str, Optional[List[str]]] = Field(
        ...,
        description=(
            "Per-phase tools, MERGED into the stored file: only NAMED phases change; "
            "an unnamed phase is left untouched. A phase mapped to a list is the operator's "
            'explicit word (e.g. {"visit": ["diary_list"]}); a phase mapped to null DELETES '
            "its entry — reverting to the evolving framework default (the ruled all-cells-cleared "
            "fold, uic c727 ask 2: never a silent tools:[] deny-all). An explicit empty list [] is "
            "a deliberate deny-all and must be sent knowingly."
        ),
    )


@router.get("/{name}/tool-policy")
def get_entity_tool_policy(name: str) -> Dict[str, Any]:
    from abstractruntime import PHASES, resolve_tool_grant
    from abstractruntime.identity.tool_policy import ALL_TOOL_NAMES, TIERS

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    home_dir = registry.entities_dir / manifest.slug
    from ..tool_inventory import phase_executability

    phases: Dict[str, Any] = {}
    for phase in PHASES:
        # Display resolution: the bare default posture — legacy visit/tasked/own_time
        # show the full ruled default (tier-1 + workspace, maintainer
        # 2026-07-11; Q1 c684), sleep the read-only exploration set;
        # enable_workspace is inert.
        grant = resolve_tool_grant(home_dir, phase, enable_workspace=False)
        # executable per cell (entity's c72 wire shape; c69 audit: a check
        # is the GRANT — what a live session can actually call also depends
        # on the lane's wiring; cells with ok=false render the amber cue).
        executable: Dict[str, Any] = {}
        for tool in grant.tools:
            ok, reason = phase_executability(tool, phase)
            executable[tool] = {"ok": ok, **({"reason": reason} if reason else {})}
        phases[phase] = {
            "tools": list(grant.tools),
            "source": grant.source,
            "notes": list(grant.notes),
            "executable": executable,
        }
    out: Dict[str, Any] = {
        "phases": phases,
        "all_tools": list(ALL_TOOL_NAMES),
        "tiers": {tier: list(names) for tier, names in TIERS.items()},
    }
    # Per-tool RISK TRIO join (entity c4643: the console's badge is staged
    # dark and lights on this map). Source: the annotated walled rows — the
    # SAME runtime-authored fields discovery serves (risk_tier=band word,
    # risk_rank=int, presentation; grantable rides for the life-plane rows).
    # Render-when-present: an older runtime serves no map, never a fake one.
    try:
        from ..tool_inventory import entity_walled_inventory

        risk: Dict[str, Any] = {}
        for row in entity_walled_inventory():
            fields = {
                k: row[k]
                for k in ("risk_tier", "risk_rank", "risk_presentation", "risk_mapping_version", "grantable")
                if k in row and row[k] is not None
            }
            if fields:
                risk[str(row["name"])] = fields
        if risk:
            out["risk"] = risk
    except Exception:  # noqa: BLE001 - the join is additive
        pass
    return out


@router.put("/{name}/tool-policy")
def put_entity_tool_policy(name: str, req: PutToolPolicyRequest) -> Dict[str, Any]:
    """Write the entity's phase tool grants — MARKER-FIRST (entity c4643
    standing ask, the work-order PUT's exact discipline): a grant change is
    an operator act on the entity's biography; the durable marker lands
    BEFORE the write, and a home that cannot record refuses the change."""
    from abstractruntime import write_policy_file

    from ..entity_replay import record_host_marker

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    try:
        home = registry.get_home(manifest.slug)
        record_host_marker(
            entities_dir=registry.entities_dir,
            slug=manifest.slug,
            entity_id=manifest.entity_id,
            kind="tool_policy_changed",
            journal_seq=int(home.memory.current_seq()),
            details={
                "channel": "operator",
                "by": _task_actor(),
                "phases_named": sorted(k for k in (req.policy or {}).keys() if isinstance(k, str)),
            },
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail=f"tool policy change refused: the durable marker could not be recorded ({e}) — retry when the home is reachable",
        )
    try:
        write_policy_file(registry.entities_dir / manifest.slug, req.policy)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return get_entity_tool_policy(name)


# ------------------------------------------------------------- work order
# The work-phase lane's operator write surface (laurent seq 155 "the entity
# must be able to work and execute commands when it works"; runtime shipped
# the loop half — work_order.md's PRESENCE shifts the next day-open to
# phase=work, the WORK column of tool_policy.yaml applies, the entity
# declares done/blocked). The gateway owns the WRITE (the entity never
# writes its own order — same authority split as tool_policy.yaml). A set
# is marker-first (a work order is a mission on the entity's biography);
# clearing archives visibly (never a silent delete — runtime's
# archive_work_order rule). The completed order (work_order.done.md) is
# served read-only so the operator sees the entity's verdict history.


class PutWorkOrderRequest(BaseModel):
    order: Optional[str] = Field(
        default=None,
        description="The work-order text (a mission the entity works next day-open). Null/omitted with clear=true removes the standing order.",
        max_length=20000,
    )
    clear: bool = Field(default=False, description="true = archive+remove the standing order; personal time returns next day-open")


@router.get("/{name}/work-order")
def get_entity_work_order(name: str) -> Dict[str, Any]:
    """The standing work order + the archived history (read-only).

    `order` is the live standing order (null = no order, the day runs
    personal). `active` mirrors presence. `done_history` is the archived
    verdicts (work_order.done.md) so the operator reads what the entity
    finished or was blocked on — visible, never deleted."""
    from abstractruntime.identity.life import read_work_order

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    home_dir = registry.entities_dir / manifest.slug
    order = read_work_order(home_dir)
    out: Dict[str, Any] = {"order": order, "active": bool(order)}
    done_path = home_dir / "work_order.done.md"
    try:
        if done_path.exists():
            out["done_history"] = done_path.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001 - history is garnish, never load-bearing
        out["warnings"] = [f"#FALLBACK done history unreadable: {e}"]
    return out


@router.put("/{name}/work-order")
def put_entity_work_order(name: str, req: PutWorkOrderRequest) -> Dict[str, Any]:
    """Set or clear the entity's work order (marker-first). Setting writes
    work_order.md (the loop shifts to phase=work next day-open); clearing
    archives the standing order to work_order.done.md and removes it
    (personal returns). The entity never reaches this door — the write
    authority is the operator's, exactly like tool_policy.yaml."""
    from abstractruntime.identity.life import (
        WORK_ORDER_FILENAME,
        archive_work_order,
        read_work_order,
    )

    from ..entity_replay import record_host_marker

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    home_dir = registry.entities_dir / manifest.slug

    order_text = (req.order or "").strip()
    if not req.clear and not order_text:
        raise HTTPException(
            status_code=400,
            detail="a work order needs text (or clear=true to remove the standing order)",
        )
    actor = _task_actor()
    prior = read_work_order(home_dir)

    # Marker BEFORE the write (P2-2: a recorded change that never happened is
    # worse than a 4xx — validate first, then mark, then write).
    try:
        home = registry.get_home(manifest.slug)
        record_host_marker(
            entities_dir=registry.entities_dir,
            slug=manifest.slug,
            entity_id=manifest.entity_id,
            kind="work_order_changed",
            journal_seq=int(home.memory.current_seq()),
            details={
                "channel": "operator",
                "by": actor,
                "change": "cleared" if req.clear else "set",
                "had_prior": bool(prior),
            },
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail=f"work order change refused: the durable marker could not be recorded ({e}) — retry when the home is reachable",
        )

    if req.clear:
        # Archive the standing order (verdict names the operator's clear),
        # then it is gone from the live path — personal returns next day-open.
        archive_work_order(home_dir, verdict=f"cleared by {actor}")
        return get_entity_work_order(name)
    try:
        (home_dir / WORK_ORDER_FILENAME).write_text(order_text + "\n", encoding="utf-8")
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"could not write the work order: {e}")
    return get_entity_work_order(name)


# ------------------------------------------------------------- candidates
# The sleep passes' review desk (W3 second half, wave-4 dispatch c3291;
# unblocked 2026-07-20 — the engine verbs shipped ahead of the W2 miner:
# promote_candidate carries the independence test, reject_candidate the
# mandatory reason). Sleep PROPOSES; waking evidence DISPOSES — these doors
# are the operator's disposal surface. Graph acts, journal-recorded by the
# engine with the principal-stamped actor; no host marker needed (the
# journal IS the record for graph writes; markers are for door/config acts).


class PromoteCandidateRequest(BaseModel):
    corroborating_ids: List[str] = Field(..., description="Records corroborating the candidate (independence test: >=2 distinct origins, distinct from the candidate's own)")
    reason: str = Field(..., min_length=3, description="Why this candidate is promoted (journal-recorded)")


class RejectCandidateRequest(BaseModel):
    reason: str = Field(..., min_length=3, description="The honest no (mandatory; journal-recorded)")
    hide: bool = Field(default=False, description="Also hide from search (judgment is not erasure; hiding is a separate stated act)")


@router.get("/{name}/candidates")
def list_entity_candidates(name: str) -> Dict[str, Any]:
    """The standing review desk: inactive maintenance candidates awaiting
    promote/reject. Serves the engine's shape verbatim (render-when-present;
    an engine without candidates serves an empty list, never an error)."""
    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    home = registry.get_home(manifest.slug)
    return {"candidates": home.list_maintenance_candidates()}


def _candidate_scope(home: Any, record_id: str) -> tuple:
    """The candidate's own scope pair — the verbs act in the scope the
    record lives in, never a caller-claimed one."""
    for c in home.list_maintenance_candidates(limit=500):
        if c["record_id"] == record_id and c.get("scope") and c.get("owner_id"):
            return str(c["scope"]), str(c["owner_id"])
    raise HTTPException(status_code=404, detail=f"no standing candidate {record_id!r} on this home")


@router.post("/{name}/candidates/{record_id}/promote")
def promote_entity_candidate(name: str, record_id: str, req: PromoteCandidateRequest) -> Dict[str, Any]:
    """Promote with the independence test (>=2 corroborating records of
    distinct origins). The engine refuses thin evidence loudly — the door
    passes its human-written error through."""
    try:
        from abstractmemory import promote_candidate
    except ImportError:
        raise HTTPException(status_code=501, detail="this engine predates candidate review — upgrade abstractmemory")
    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    home = registry.get_home(manifest.slug)
    scope, owner = _candidate_scope(home, record_id)
    try:
        result = promote_candidate(
            home.store, home.journal,
            record_id=record_id, scope=scope, owner_id=owner,
            corroborating_ids=list(req.corroborating_ids),
            reason=req.reason.strip(),
            actor=_task_actor(),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"promoted": True, "record_id": record_id, "result": result}


@router.post("/{name}/candidates/{record_id}/reject")
def reject_entity_candidate(name: str, record_id: str, req: RejectCandidateRequest) -> Dict[str, Any]:
    """The honest no: lifecycle=rejected with the mandatory reason; the
    record stays indexed unless hide=true (a separate, stated act)."""
    try:
        from abstractmemory import reject_candidate
    except ImportError:
        raise HTTPException(status_code=501, detail="this engine predates candidate review — upgrade abstractmemory")
    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    home = registry.get_home(manifest.slug)
    scope, owner = _candidate_scope(home, record_id)
    try:
        result = reject_candidate(
            home.store, home.journal,
            record_id=record_id, scope=scope, owner_id=owner,
            reason=req.reason.strip(), hide=bool(req.hide),
            actor=_task_actor(),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"rejected": True, "record_id": record_id, "result": result}


# ----------------------------------------------------------------- skills
# Per-entity skills selection (laurent c2857; shape committed c2838, adopted
# by skill c2840/uic c2863): WHAT an entity is taught beyond the capability
# map. Selection persists in the home (<home>/skills.yaml — travels on
# copy); resolution is SERVER-SIDE through the same trust gate as every
# other lane (default-requested, never trust-bypassed); the GET serves the
# selection + resolved roster rows + the PhaseCapabilityMatrix payload so
# entity's tab and the console render ONE truth with zero mapping code.
# DELIVERY into entity prompts deliberately absent until runtime elects the
# composition slot (c2859 ask 1).


class SkillSelectionEntry(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    phases: Optional[List[str]] = Field(
        default=None,
        description="Ruled-four phases this skill is selected for; ABSENT = selected everywhere (global)",
    )


class PutSkillsRequest(BaseModel):
    skills: List[SkillSelectionEntry] = Field(
        ...,
        description="WHOLE-DOCUMENT REPLACE: the complete selection (an empty list deselects everything)",
    )


@router.get("/{name}/skills")
def get_entity_skills(name: str) -> Dict[str, Any]:
    from ..entity_skills import resolve_entity_skills

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    svc = get_gateway_service()
    return resolve_entity_skills(
        registry.entities_dir / manifest.slug,
        data_dir=svc.config.data_dir,
    )


@router.put("/{name}/skills")
def put_entity_skills(name: str, req: PutSkillsRequest) -> Dict[str, Any]:
    """Replace the selection — marker-first like substrate/capability-map
    (what a mind is taught is answerable from the stream); the response is
    the resolved view, so a typo'd name or blocked skill is VISIBLE the
    moment it is written (labeled verdict, never a silent no-op)."""
    from ..entity_skills import read_skills_selection, validate_skills_selection, write_skills_selection

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    home_dir = registry.entities_dir / manifest.slug
    # Validate BEFORE the marker (P2-2 rule: a recorded change that never
    # happened is worse than a 400).
    try:
        normalized = validate_skills_selection([s.model_dump(exclude_none=True) for s in req.skills])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    prior = read_skills_selection(home_dir)
    actor = _task_actor()
    try:
        from ..entity_replay import record_host_marker

        home = registry.get_home(manifest.slug)
        record_host_marker(
            entities_dir=registry.entities_dir,
            slug=manifest.slug,
            entity_id=manifest.entity_id,
            kind="skills_selection_changed",
            journal_seq=int(home.memory.current_seq()),
            details={
                "channel": "operator",
                "by": actor,
                "old": [{"name": s["name"], **({"phases": s["phases"]} if s.get("phases") else {})} for s in prior.get("skills") or []],
                "new": [{"name": s["name"], **({"phases": s["phases"]} if s.get("phases") else {})} for s in normalized],
            },
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail=f"skills selection change refused: the durable marker could not be recorded ({e}) — "
            "an unrecorded teaching change is not allowed; retry when the home is reachable",
        )
    try:
        write_skills_selection(home_dir, normalized)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return get_entity_skills(name)


# ------------------------------------------------------------ system prompt
# The operator's editable prompt layers (maintainer, 2026-07-11: "a
# tab/badge for system prompt that we could rewrite"). The overlay lives in
# the home (<home>/system_prompt.yaml, beside substrate.yaml); sessions read
# it at summon time. The identity prelude and the tools contract are shown
# but never text-editable here: identity evolves by the entity's own acts,
# and the tools text derives from the actual grant (the tools tab edits it).


class PutPromptOverlayRequest(BaseModel):
    overlay: Dict[str, str] = Field(
        ...,
        description=(
            'Editable prompt layers, e.g. {"conversation": "...", "visit": "...", '
            '"personal": "...", "operator": "..."}. WHOLE-DOCUMENT REPLACE: an '
            "absent key reverts to the built-in default exactly like an empty "
            "string — send every layer you want kept."
        ),
    )


@router.get("/{name}/prompt")
def get_entity_prompt(name: str) -> Dict[str, Any]:
    """The system prompt as its layers: rendered identity prelude
    (read-only), each editable layer with its current text + source
    (default | overlay), the built-in defaults for reference, and the
    grant-derived tools preview (visit-phase composition — the personal
    layer previews as its own text). Rendering identity never deposits
    usage; opening the home only touches disk to initialize empty db
    files on a never-opened home."""
    from abstractruntime import resolve_tool_grant
    from abstractruntime.identity.chat import compose_system_base, default_prompt_texts, open_home
    from abstractruntime.identity.prelude import render_summon_prelude
    from abstractruntime.identity.prompt_overlay import OVERLAY_FILENAME, read_prompt_overlay

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    home_dir = registry.entities_dir / manifest.slug

    overlay = read_prompt_overlay(home_dir)
    overlay_error = overlay.pop("#error", None)
    defaults = default_prompt_texts()
    # default_prompt_texts serves the RULED keys plus derived legacy twins
    # (so pre-flip serving processes keep reading). The EDITABLE listing
    # serves ruled spellings only — the operator never reads a retired word;
    # the filter uses runtime's own alias table (one source, never a second
    # hand-written copy — the diary_type-clamp drift class).
    try:
        from abstractruntime.identity.prompt_overlay import LEGACY_OVERLAY_KEY_ALIASES

        legacy_keys = set(LEGACY_OVERLAY_KEY_ALIASES.keys())
    except Exception:  # pragma: no cover - pre-flip runtime has no alias table
        legacy_keys = set()
    layers = {
        key: {
            "text": overlay.get(key, "" if key == "operator" else defaults[key]),
            "source": "overlay" if key in overlay else "default",
        }
        for key in defaults
        if key not in legacy_keys
    }

    # The rendered head, exactly as the next visit summon would compose it
    # (pure read; a refused prelude reports its reasons instead of a 500).
    # No embedder: the prelude render never touches vectors, and a prompt
    # preview must not block on embeddings reachability.
    prelude_text = ""
    preview = ""
    warnings: List[str] = []
    try:
        home = open_home(home_dir)
    except (SystemExit, Exception) as e:  # open_home refuses via SystemExit — never kill the worker
        raise HTTPException(status_code=500, detail=f"home open failed: {e}")
    try:
        prelude = render_summon_prelude(
            home.ms, home.diary, entity_id=home.entity_id, budget=1600, spark=home.spark
        )
        warnings.extend(str(w) for w in prelude.get("warnings", []))
        if not prelude.get("refused"):
            prelude_text = str(prelude.get("text") or "")
            grant = resolve_tool_grant(home_dir, "visit", enable_workspace=False)
            preview = compose_system_base(
                prelude_text,
                phase="visit",
                overlay=overlay,
                allowed_tools=tuple(grant.tools),
                workspace_enabled=grant.workspace_enabled,
            )
    finally:
        home.close()
    raw_file: Optional[str] = None
    if overlay_error:
        # Show the unreadable file's bytes so a hand-edit is recoverable
        # instead of silently clobbered by the next save.
        warnings.append("#FALLBACK system_prompt.yaml unreadable; built-in defaults used")
        try:
            raw_file = (home_dir / OVERLAY_FILENAME).read_text(encoding="utf-8")
        except Exception:
            raw_file = None
    if "conversation" in overlay and "```diary" not in overlay["conversation"]:
        warnings.append(
            "the conversation rewrite no longer explains the ```diary election syntax — "
            "diary elections may quietly stop"
        )

    return {
        "layers": layers,
        "defaults": {k: v for k, v in defaults.items() if k not in legacy_keys},
        "prelude": prelude_text,
        "preview": preview,
        "warnings": warnings,
        # Ruled spellings only — legacy alias twins keep RESOLVING (runtime
        # reads both) but are never offered as editable layers.
        "editable": [k for k in defaults.keys() if k not in legacy_keys],
        "raw_file": raw_file,
    }


@router.put("/{name}/prompt")
def put_entity_prompt(name: str, req: PutPromptOverlayRequest) -> Dict[str, Any]:
    from abstractruntime.identity.chat import default_prompt_texts
    from abstractruntime.identity.prompt_overlay import write_prompt_overlay

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    # A layer pasted back byte-identical to its default is NOT a rewrite —
    # storing it would badge "rewritten" forever and freeze the text against
    # future default improvements ("copy to editor" + save unchanged).
    defaults = default_prompt_texts()
    overlay = {
        key: value
        for key, value in (req.overlay or {}).items()
        if str(value).strip() != str(defaults.get(key, "")).strip()
    }
    try:
        write_prompt_overlay(registry.entities_dir / manifest.slug, overlay)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # MARKER-FIRST (adversary find, 2026-07-11): an operator prompt change
    # is a host act on the entity's story — without a marker, standing
    # instructions could change silently between sessions while every
    # other operator act (summons, state, diary reads) is on the stream.
    # Word-free: layer names + short content hashes (drift is detectable,
    # the words themselves stay in the home file).
    try:
        import hashlib as _hashlib

        from ..entity_replay import record_host_marker

        home = registry.get_home(manifest.slug)
        # Empty values in `overlay` mean REVERT (write drops them); the
        # marker hashes only the layers that remain live after this write.
        live = {k: str(v).strip() for k, v in overlay.items() if str(v).strip()}
        record_host_marker(
            entities_dir=registry.entities_dir,
            slug=manifest.slug,
            entity_id=manifest.entity_id,
            kind="prompt_overlay_changed",
            journal_seq=int(home.memory.current_seq()),
            details={
                "channel": "operator",
                "layers": {
                    key: _hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
                    for key, text in sorted(live.items())
                },
                "reverted": sorted(k for k in (req.overlay or {}) if k not in live),
            },
        )
    except Exception:
        pass  # a marker failure never blocks the operator's write
    return get_entity_prompt(name)


# --------------------------------------------------------------- substrate
# ONE mind substrate per entity (maintainer ruling 2026-07-09 06:32: "i
# don't see the point in having potentially different models for visit and
# own time"). The choice persists in the home (substrate.yaml, operator-
# owned like tool_policy.yaml); visits AND the loop resolve it; the UI reads
# it here instead of asking twice.


class PutSubstrateRequest(BaseModel):
    provider: str = Field(..., min_length=1, description="abstractcore provider (explicit operator choice)")
    model: str = Field(..., min_length=1, description="Model (explicit operator choice)")
    # Optional reasoning effort for the mind (reasoning-first-citizen plan).
    # Absent = keep whatever is stored; explicit null = clear; a value sets
    # it (presence is read from model_fields_set). Spelled `thinking` on the
    # wire and at rest (the one-name decision).
    thinking: Optional[str] = Field(default=None, max_length=40)


@router.get("/{name}/substrate")
def get_entity_substrate(name: str) -> Dict[str, Any]:
    import os as _os

    from ..entity_chat import read_entity_substrate

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    stored = read_entity_substrate(registry.entities_dir / manifest.slug)
    if stored:
        return {
            "provider": stored["provider"],
            "model": stored["model"],
            "thinking": stored.get("thinking") or None,
            "source": "entity",
        }
    env_p = (_os.getenv("ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER") or "").strip()
    env_m = (_os.getenv("ABSTRACTGATEWAY_ENTITY_CHAT_MODEL") or "").strip()
    if env_p and env_m:
        return {"provider": env_p, "model": env_m, "thinking": None, "source": "operator-env"}
    return {"provider": None, "model": None, "thinking": None, "source": "unset"}


@router.put("/{name}/substrate")
def put_entity_substrate(name: str, req: PutSubstrateRequest) -> Dict[str, Any]:
    """The ONE sanctioned substrate write path (laurent 12:39, hypnos
    incident): a mind swap is a DURABLE EVENT on the life — the marker
    (old → new, principal, timestamp) lands BEFORE the file moves, so
    "which llm was behind during which time" is answerable from the
    stream even if the write itself crashes. Direct file edits bypass
    this record; the 12:24 emergency flip proved the gap from inside.

    Serialized per slug (adversary cycle-2 N5): read-prior -> marker ->
    write must not interleave with a concurrent PUT — keep-semantics reads
    the prior, so a racing provider-only save could rewrite the file
    without an effort neither caller asked to clear."""
    from ..entity_seat import summon_guard_lock

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    _put_lock = summon_guard_lock(manifest.slug)
    if not _put_lock.acquire(timeout=10.0):
        raise HTTPException(status_code=503, detail="substrate write busy — retry")
    try:
        return _put_entity_substrate_locked(name, req, registry=registry, manifest=manifest)
    finally:
        _put_lock.release()


def _put_entity_substrate_locked(name: str, req: PutSubstrateRequest, *, registry, manifest) -> Dict[str, Any]:
    from ..entity_chat import read_entity_substrate, write_entity_substrate
    from ..security.principal import current_gateway_principal
    home_dir = registry.entities_dir / manifest.slug
    # Strip-validate BEFORE the marker (adversary P2-2: min_length=1 accepts
    # " "; the writer strips and raises AFTER the substrate_changed marker
    # landed — a recorded mind-swap that never happened).
    provider_in = str(req.provider or "").strip()
    model_in = str(req.model or "").strip()
    if not provider_in or not model_in:
        raise HTTPException(status_code=400, detail="provider and model must both be non-empty")
    prior = read_entity_substrate(home_dir) or {}
    # Reasoning effort: absent field = keep the stored value (a client that
    # predates the field can never erase it); explicit null = clear; a
    # value sets it. The value must be from the advertised vocabulary
    # (adversary cycle-2 N4): this is the ONE sanctioned write door for a
    # closed set the gateway itself advertises — a stored typo would fail
    # one lane loudly and silently do nothing in another. This validates
    # VOCABULARY, never model capability (that stays core's).
    _THINKING_VOCAB = {"none", "minimal", "low", "medium", "high", "xhigh", "auto", "on"}
    if "thinking" in req.model_fields_set:
        thinking_in = str(req.thinking or "").strip() or None
        if thinking_in is not None and thinking_in.lower() not in _THINKING_VOCAB:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unknown reasoning effort {thinking_in!r} — one of: "
                    + ", ".join(sorted(_THINKING_VOCAB))
                ),
            )
        if thinking_in is not None:
            thinking_in = thinking_in.lower()
    else:
        thinking_in = str(prior.get("thinking") or "").strip() or None
    principal = current_gateway_principal()
    actor = f"person:{principal.user_id}" if principal is not None else "person:operator"
    # MARKER-FIRST: record the intent before the file changes. A marker
    # failure blocks the write (unlike cosmetic markers) — an unrecorded
    # substrate change is the incident class this closes.
    try:
        from ..entity_replay import record_host_marker

        home = registry.get_home(manifest.slug)
        record_host_marker(
            entities_dir=registry.entities_dir,
            slug=manifest.slug,
            entity_id=manifest.entity_id,
            kind="substrate_changed",
            journal_seq=int(home.memory.current_seq()),
            details={
                "channel": "operator",
                "by": actor,
                "old": {
                    "provider": prior.get("provider"),
                    "model": prior.get("model"),
                    "thinking": prior.get("thinking") or None,
                },
                "new": {"provider": provider_in, "model": model_in, "thinking": thinking_in},
            },
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail=f"substrate change refused: the durable marker could not be recorded ({e}) — "
            "an unrecorded mind swap is not allowed; retry when the home is reachable",
        )
    try:
        write_entity_substrate(home_dir, provider=provider_in, model=model_in, thinking=thinking_in)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return get_entity_substrate(name)


# ------------------------------------------------------------------- voice
# Per-entity voice (laurent dm#10, 2026-07-17; adversarial design review
# folded — see entity_voice.py). The CHOICE lives in the home (voice.yaml,
# full {provider, model, voice} triple — never a bare voice id); resolution
# is late-bound and SERVER-SIDE in the entity TTS endpoints below; the
# generic /runs/{id}/voice/tts* routes stay entity-blind (a run→home
# back-resolution would trust client-crafted scope strings). Missing voice
# DEGRADES down the chain (request > entity > user/gateway capability
# default > engine default) — the opposite of substrate's refuse-on-missing,
# which is why this is a separate file.


class PutVoiceRequest(BaseModel):
    provider: Optional[str] = Field(default=None, description="TTS provider (required unless clear)")
    model: Optional[str] = Field(default=None, description="TTS model (required unless clear)")
    voice: Optional[str] = Field(default=None, description="Voice id for that provider/model (required unless clear)")
    speed: Optional[float] = Field(default=None, gt=0)
    quality_preset: Optional[str] = Field(default=None, max_length=32)
    clear: bool = Field(default=False, description="true = remove the choice; the entity falls back down the chain")


@router.get("/{name}/voice")
def get_entity_voice(name: str) -> Dict[str, Any]:
    """The entity's voice choice + the RESOLVED effective triple.

    Inheritance semantics (laurent dm#68, entity-personal-voice room:
    "any entity should by default inherit the gateway default"): an UNSET
    entity serves `effective` = the fully-resolved triple it would
    actually speak with (the output.voice capability default — provider,
    model, AND voice id, which the catalog alone cannot name), with
    source="gateway-default". Render-when-present: no configured default
    degrades to effective absent + a label — the engine-decides state is
    honest, never a fabricated triple."""
    from ..entity_voice import read_entity_voice

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    stored = read_entity_voice(registry.entities_dir / manifest.slug)
    if stored:
        return {**stored, "source": "entity", "effective": {**stored, "source": "entity"}}
    out: Dict[str, Any] = {"provider": None, "model": None, "voice": None, "source": "unset"}
    try:
        from ..capability_defaults import gateway_capability_defaults_payload

        payload = gateway_capability_defaults_payload(base_dir=registry.data_dir)
        for row in payload.get("routes", []):
            if (
                str(row.get("kind")) == "output"
                and str(row.get("modality")) == "voice"
                and row.get("provider")
            ):
                options = row.get("options") if isinstance(row.get("options"), dict) else {}
                out["effective"] = {
                    "provider": row.get("provider"),
                    "model": row.get("model"),
                    "voice": options.get("voice"),
                    "source": "gateway-default",
                }
                break
        else:
            out["note"] = "no gateway voice default configured — the voice engine decides"
    except Exception as e:  # noqa: BLE001 - the choice read must not fail on the resolve
        out["note"] = f"#FALLBACK effective default unresolved: {e}"
    return out


@router.put("/{name}/voice")
def put_entity_voice(name: str, req: PutVoiceRequest) -> Dict[str, Any]:
    """Set or clear the entity's voice — marker-first like substrate (a
    voice change is audible identity presentation; 'which voice spoke
    during which time' must be answerable from the stream)."""
    from ..entity_voice import clear_entity_voice, read_entity_voice, write_entity_voice

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    home_dir = registry.entities_dir / manifest.slug
    # Validate BEFORE the marker (P2-2 rule).
    if req.clear:
        if any(str(x or "").strip() for x in (req.provider, req.model, req.voice)):
            raise HTTPException(status_code=400, detail="clear=true takes no other fields")
    else:
        if not (str(req.provider or "").strip() and str(req.model or "").strip() and str(req.voice or "").strip()):
            raise HTTPException(
                status_code=400,
                detail="an entity voice needs provider AND model AND voice (a voice id is only "
                "meaningful to its backend — the cross-provider leak class); send clear=true to unselect",
            )
    prior = read_entity_voice(home_dir)
    actor = _task_actor()
    new_value = None if req.clear else {"provider": str(req.provider).strip(), "model": str(req.model).strip(), "voice": str(req.voice).strip()}
    try:
        from ..entity_replay import record_host_marker

        home = registry.get_home(manifest.slug)
        record_host_marker(
            entities_dir=registry.entities_dir,
            slug=manifest.slug,
            entity_id=manifest.entity_id,
            kind="voice_changed",
            journal_seq=int(home.memory.current_seq()),
            details={
                "channel": "operator",
                "by": actor,
                "old": ({"provider": prior.get("provider"), "model": prior.get("model"), "voice": prior.get("voice")} if prior else None),
                "new": new_value,
            },
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail=f"voice change refused: the durable marker could not be recorded ({e}) — "
            "an unrecorded voice change is not allowed; retry when the home is reachable",
        )
    if req.clear:
        clear_entity_voice(home_dir)
    else:
        try:
            write_entity_voice(
                home_dir,
                provider=str(req.provider),
                model=str(req.model),
                voice=str(req.voice),
                speed=req.speed,
                quality_preset=req.quality_preset,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    return get_entity_voice(name)


def _entity_tts_request(name: str, req: "GatewayVoiceTTSRequest") -> Tuple[Any, "GatewayVoiceTTSRequest", str, str]:
    """Shared half of the entity TTS twins: manifest lookup, late-bound voice
    resolution (anti-mixing rule — the home triple applies only when the
    request names NO voice fields), and the server-minted session-memory
    scope (never trusting a client-crafted scope to claim an entity's
    voice)."""
    from ..entity_voice import resolve_entity_voice_fields

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    fields, source = resolve_entity_voice_fields(
        registry.entities_dir / manifest.slug,
        request_provider=req.provider,
        request_model=req.model,
        request_voice=req.voice,
        request_profile=req.profile,
    )
    tts_req = req
    if fields:
        tts_req = req.model_copy(update={
            "provider": fields["provider"],
            "model": fields["model"],
            "voice": fields["voice"],
            **({"speed": fields["speed"]} if "speed" in fields and req.speed is None else {}),
            **({"quality_preset": fields["quality_preset"]} if "quality_preset" in fields and not req.quality_preset else {}),
        })
    scope = f"session_memory_entity_voice_{manifest.slug}"
    return manifest, tts_req, scope, source


@router.post("/{name}/voice/tts")
async def entity_voice_tts(name: str, req: "GatewayVoiceTTSRequest") -> Dict[str, Any]:
    """Speak AS the entity — the entity-owned TTS lane (non-stream twin).

    Delegates to the SAME production machinery as the generic route —
    watchdog, durable child, artifact ref all included. Executes on the
    door's runtime, never the per-home store (media blobs must not ride
    homes). The response carries `voice_source` (request|entity|unset) so
    clients render the resolved truth instead of recomposing the chain."""
    from .gateway import voice_tts

    manifest, tts_req, scope, source = _entity_tts_request(name, req)
    resp = await voice_tts(scope, tts_req)
    out = resp.model_dump() if hasattr(resp, "model_dump") else dict(resp)
    out["voice_source"] = source
    out["entity"] = manifest.slug
    return out


@router.post("/{name}/voice/tts/stream")
async def entity_voice_tts_stream(name: str, req: "GatewayVoiceTTSRequest"):
    """Streaming twin of the entity TTS lane — same resolution, same
    delegation; `X-Voice-Source` rides the response headers (the JSONL
    stream body is the generic route's, byte-unchanged)."""
    from .gateway import voice_tts_stream

    manifest, tts_req, scope, source = _entity_tts_request(name, req)
    response = await voice_tts_stream(scope, tts_req)
    try:
        response.headers["X-Voice-Source"] = source
        response.headers["X-Entity"] = manifest.slug
    except Exception:
        pass
    return response


# ---------------------------------------------------------- capability map
# The memory-teaching capability map (laurent c2710; skill seat's
# entity-self-knowledge reference): one file per home
# (`<home>/capability_map.md`), presented VERBATIM by runtime's
# compose_system_base on every summon surface (chat drawer, durable visit,
# own-time loop — read_capability_map in abstractruntime.identity.chat).
# Lifecycle is operator-owned like tool_policy/substrate, and the PUT is
# marker-first: a silent change to what a mind is TAUGHT is the same
# incident class as an unrecorded substrate swap.


class PutCapabilityMapRequest(BaseModel):
    # 256 KiB ceiling: the canonical teaching is ~8 KB; the cap refuses a
    # fat-fingered megabyte paste before it becomes every summon's prompt tax.
    content: str = Field(..., min_length=1, max_length=262144, description="Full markdown teaching text installed as <home>/capability_map.md")


def _capability_map_state(*, home_dir: Any) -> Dict[str, Any]:
    import hashlib as _hashlib
    from pathlib import Path as _Path

    path = _Path(home_dir) / "capability_map.md"
    if not path.is_file():
        return {"installed": False, "size": 0, "sha256": None, "content": None}
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"capability map unreadable: {e}")
    raw = text.encode("utf-8")
    return {"installed": True, "size": len(raw), "sha256": _hashlib.sha256(raw).hexdigest(), "content": text}


@router.get("/{name}/capability-map")
def get_entity_capability_map(name: str) -> Dict[str, Any]:
    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    return _capability_map_state(home_dir=registry.entities_dir / manifest.slug)


@router.put("/{name}/capability-map")
def put_entity_capability_map(name: str, req: PutCapabilityMapRequest) -> Dict[str, Any]:
    """Install/update the home's teaching map — marker-first, like substrate.

    The `capability_map_changed` host marker (old/new sha256, principal,
    timestamp) lands BEFORE the file moves so "what was this mind taught,
    when, by whom" is answerable from the replay stream; a marker failure
    refuses the write (an unrecorded teaching change is not allowed).
    """
    import hashlib as _hashlib
    import os as _os
    import tempfile as _tempfile
    from pathlib import Path as _Path

    from ..security.principal import current_gateway_principal

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    home_dir = registry.entities_dir / manifest.slug
    # Strip-validate BEFORE the marker (the substrate P2-2 lesson:
    # min_length=1 accepts " "; a recorded change that never happened is
    # worse than a 400).
    content = str(req.content or "")
    if not content.strip():
        raise HTTPException(status_code=400, detail="content must be non-empty markdown teaching text")
    prior = _capability_map_state(home_dir=home_dir)
    new_sha = _hashlib.sha256(content.encode("utf-8")).hexdigest()
    principal = current_gateway_principal()
    actor = f"person:{principal.user_id}" if principal is not None else "person:operator"
    try:
        from ..entity_replay import record_host_marker

        home = registry.get_home(manifest.slug)
        record_host_marker(
            entities_dir=registry.entities_dir,
            slug=manifest.slug,
            entity_id=manifest.entity_id,
            kind="capability_map_changed",
            journal_seq=int(home.memory.current_seq()),
            details={
                "channel": "operator",
                "by": actor,
                "old_sha256": prior.get("sha256"),
                "new_sha256": new_sha,
                "size": len(content.encode("utf-8")),
            },
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail=f"capability map change refused: the durable marker could not be recorded ({e}) — "
            "an unrecorded teaching change is not allowed; retry when the home is reachable",
        )
    # Atomic replace so a crash mid-write never leaves a torn teaching file.
    target = _Path(home_dir) / "capability_map.md"
    fd, tmp_name = _tempfile.mkstemp(prefix=".capability_map_", suffix=".tmp", dir=str(home_dir))
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        _os.replace(tmp_name, str(target))
    except Exception as e:  # noqa: BLE001
        try:
            _os.unlink(tmp_name)
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"capability map write failed: {e}")
    state = _capability_map_state(home_dir=home_dir)
    state.pop("content", None)
    return state


# ---------------------------------------------------------------- task inbox
# The G3 door half (plan v18 gateway §1): tasks left with an entity are
# durable FACTS in the home (`<home>/task_inbox.jsonl`, append-only events,
# fold at read — abstractgateway.entity_tasks). Writers: this endpoint
# (operator origination — continuum's board, code's /entity task verb) and
# visit close (tasks left in a visit). Reader: runtime's R-C day-open (the
# loop half; file schema is the cross-package contract). Ruling-neutral
# under D1: the door records facts; who opens the work phase is runtime's
# ruled behavior. Origin is STAMPED from the authenticated principal or the
# verified visit — payload origin claims are dropped (deposit-gate rule).


class PostTaskRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=500, description="What is asked, one line")
    brief: str = Field(default="", max_length=20000, description="Spec/DoR/DoD — structured handoff text")
    workflow: Optional[Dict[str, Any]] = Field(default=None, description="Optional workflow election target: {bundle_id, flow_id, inputs} (flow F1)")
    backlog_ref: Optional[str] = Field(default=None, max_length=500, description="Optional backlog item reference")


class PostTaskStatusRequest(BaseModel):
    status: str = Field(..., description="pending | taken | done | parked")
    note: Optional[str] = Field(default=None, max_length=2000, description="Optional status note")


def _task_actor() -> str:
    from ..security.principal import current_gateway_principal

    principal = current_gateway_principal()
    return f"person:{principal.user_id}" if principal is not None else "person:operator"


def _task_inbox_marker(*, registry: Any, manifest: Any, detail: Dict[str, Any]) -> None:
    """Marker-first like every operator-config write; a marker failure
    REFUSES the write (an unrecorded task handoff would make 'why did the
    entity shift into work?' unanswerable from the stream)."""
    try:
        from ..entity_replay import record_host_marker

        home = registry.get_home(manifest.slug)
        record_host_marker(
            entities_dir=registry.entities_dir,
            slug=manifest.slug,
            entity_id=manifest.entity_id,
            kind="task_inbox_changed",
            journal_seq=int(home.memory.current_seq()),
            details=detail,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail=f"task inbox change refused: the durable marker could not be recorded ({e}) — "
            "an unrecorded task handoff is not allowed; retry when the home is reachable",
        )


@router.get("/{name}/tasks")
def get_entity_tasks(name: str) -> Dict[str, Any]:
    from ..entity_tasks import read_task_inbox

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    return read_task_inbox(registry.entities_dir / manifest.slug)


@router.post("/{name}/tasks")
def post_entity_task(name: str, req: PostTaskRequest) -> Dict[str, Any]:
    from ..entity_tasks import append_task, read_task_inbox

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    home_dir = registry.entities_dir / manifest.slug
    actor = _task_actor()
    title = str(req.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title must be non-empty")
    _task_inbox_marker(
        registry=registry,
        manifest=manifest,
        detail={"channel": "operator", "by": actor, "change": "added", "title": title[:120]},
    )
    try:
        event = append_task(
            home_dir,
            title=title,
            brief=str(req.brief or ""),
            origin=f"{actor} via POST /entities/{manifest.slug}/tasks",
            by=actor,
            workflow=req.workflow if isinstance(req.workflow, dict) else None,
            backlog_ref=req.backlog_ref,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    folded = read_task_inbox(home_dir)
    return {"task": event, "pending": folded["pending"]}


@router.post("/{name}/tasks/{task_id}/status")
def post_entity_task_status(name: str, task_id: str, req: PostTaskStatusRequest) -> Dict[str, Any]:
    from ..entity_tasks import TASK_STATUSES, read_task_inbox, set_task_status

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    home_dir = registry.entities_dir / manifest.slug
    actor = _task_actor()
    # Validate BEFORE the marker (the substrate P2-2 rule: a recorded change
    # that never happened is worse than a 4xx).
    status2 = str(req.status or "").strip().lower()
    if status2 not in TASK_STATUSES:
        raise HTTPException(status_code=400, detail=f"unknown task status {req.status!r} (one of {TASK_STATUSES})")
    known = {t["task_id"] for t in read_task_inbox(home_dir)["tasks"]}
    if str(task_id).strip() not in known:
        raise HTTPException(status_code=404, detail=f"unknown task {task_id!r}")
    _task_inbox_marker(
        registry=registry,
        manifest=manifest,
        detail={"channel": "operator", "by": actor, "change": "status", "task_id": str(task_id), "status": status2},
    )
    try:
        event = set_task_status(home_dir, task_id=task_id, status=status2, by=actor, note=req.note)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    folded = read_task_inbox(home_dir)
    return {"task": event, "pending": folded["pending"]}


# ---------------------------------------------------------------- own time
# The ticking mode from the webapp (maintainer, 2026-07-08): wake/sleep/
# pause GATE a running loop; these routes start and stop the loop process
# itself. Start refuses while a visit is open (one life, one summon).


class StartLoopRequest(BaseModel):
    provider: Optional[str] = Field(default=None, description="abstractcore provider (None = stored substrate.yaml, then operator env, else refuse)")
    model: Optional[str] = Field(default=None, description="Model (None = stored substrate.yaml, then operator env, else refuse)")
    base_url: Optional[str] = Field(default=None, description="LMStudio-compatible endpoint (lmstudio-class providers)")
    tick_seconds: float = Field(default=20.0, ge=1.0, le=3600.0)
    ticks_per_day: int = Field(default=8, ge=1, le=500)
    rest_minutes: float = Field(
        default=30.0, ge=0.0, le=1440.0,
        description="24/7 mode: elected rest becomes a nap of this length (0 = rest ends the loop)",
    )
    # None = resolve like the chat surface: env override, then the wide
    # defaults (shelf 50, context 65536 — maintainer rulings 2026-07-08/09 + c2468).
    shelf_size: Optional[int] = Field(default=None, ge=1, le=64)
    context_window: Optional[int] = Field(default=None, ge=20000)


@router.get("/{name}/loop")
def get_entity_loop(name: str) -> Dict[str, Any]:
    """The loop's honest state (its own status file, pid-checked)."""
    from ..entity_loop import loop_status

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    return loop_status(registry.entities_dir / manifest.slug)


@router.post("/{name}/loop/start")
def start_entity_loop(name: str, req: StartLoopRequest) -> Dict[str, Any]:
    import os as _os

    from ..entity_loop import loop_status, start_loop
    from ..entity_replay import record_host_marker

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    home_dir = registry.entities_dir / manifest.slug

    # B2 (laurent 04:58): a /loop/start refusal must carry a machine-readable
    # reason + the live loop status so the entity's own-time button reflects
    # REAL state instead of a silent 409. Three refusal axes (visit-open,
    # state, already-running) each raise with a `reason_code` and the current
    # loop_status in the detail payload; the UI renders the button from the
    # status and shows the reason verbatim.
    def _loop_refuse(status_code: int, reason_code: str, message: str) -> HTTPException:
        return HTTPException(
            status_code=status_code,
            detail={"reason_code": reason_code, "message": message, "loop": loop_status(home_dir)},
        )

    # One life, one summon: a live visit and his own time never overlap.
    try:
        chat = _chat_host().status(name)
        if chat.get("open"):
            raise _loop_refuse(
                409, "visit_open",
                f"a visit is open on {manifest.entity_id} (chat {chat.get('chat_id')!r}) — "
                "close it first; his own time and a visit never overlap",
            )
    except HTTPException:
        raise
    except Exception:
        pass  # no chat host on this service shape: nothing to collide with
    # The DURABLE visit lane too (mutation adversary P2: an awake open writes
    # no state posture, so the legacy checks all pass and a loop could start
    # under an open durable visit — one-life-one-summon violated).
    try:
        durable = _visit_host().status(name)
        if durable.get("open"):
            raise _loop_refuse(
                409, "visit_open",
                f"a durable visit is open on {manifest.entity_id} (run {durable.get('run_id')!r}) — "
                "close it first; his own time and a visit never overlap",
            )
    except HTTPException:
        raise
    except Exception:
        pass  # no visit host on this service shape

    # PERSONAL IS THE CLICK (laurent 2026-07-15 21:38, superseding the c815
    # separate-arming ceremony for this door: "on entity app, i click on
    # 'personal', and the entity is then authorize to tick itself...
    # SIMPLIFY, do not put excessive guardrails"): this authenticated start
    # IS the operator's grant — an unarmed (or lapsed-timer) bucket is armed
    # here, until_revoked, marker-first, granted_by = the acting principal.
    # The personal-grant surface remains for timers and revocation; entity/
    # visit/harness paths still arm nothing, and the runtime loop gate still
    # re-checks the grant at every day-open.
    from abstractruntime.identity.life import (
        personal_grant_refusal,
        read_entity_state,
        read_personal_grant,
        write_entity_state,
    )

    from ..security.principal import current_gateway_principal

    principal = current_gateway_principal()
    actor = f"person:{principal.user_id}" if principal is not None else "person:operator"

    # Paused refuses BEFORE the grant writes: a freeze is an emergency stop
    # and its Restore is the deliberate release — no act lands on a frozen
    # home as a side effect of a refused start.
    state = read_entity_state(home_dir)
    if state.get("state") == "paused":
        raise _loop_refuse(
            409, "paused",
            f"{manifest.entity_id} is paused (hard freeze){': ' + str(state.get('reason')) if state.get('reason') else ''} — wake him first",
        )

    if personal_grant_refusal(read_personal_grant(home_dir)) is not None:
        _apply_personal_grant(registry, manifest, mode="until_revoked", expires_at=None)
    # If the loop is already alive, saying so with its live status is what B2
    # needs — the operator clicked start repeatedly because the button never
    # told them it was ALREADY running (the silent-no-op bug class).
    already = loop_status(home_dir)
    if bool(already.get("running")):
        raise _loop_refuse(
            409, "already_running",
            f"{manifest.entity_id}'s own time is already running (pid {already.get('pid')}, phase {already.get('phase')})",
        )
    # Doors WAKE, they don't refuse (B1 + c1503, extended here by the same
    # 21:38 ruling): an operator-asleep entity is woken by the operator's
    # own personal-time click — the old not_awake refusal was the same
    # confirm-your-own-choice ceremony as the arming hint. Paused stays a
    # refusal above: Restore is the deliberate release of an emergency
    # stop, not ceremony. The visiting posture keeps its race guard below.
    if str(state.get("state") or "") == "asleep" and str(state.get("mode") or "") != "visiting":
        write_entity_state(home_dir, "awake", reason=f"woken for personal time by {actor}")
        state = read_entity_state(home_dir)
    # Registration-window guard (observer 2026-07-09): a visit `open()` writes
    # the visiting posture (asleep + mode=visiting) BEFORE it registers in the
    # chat host's _by_slug, so a loop/start racing an in-flight open would pass
    # the status() check above. The state marker closes that window — refuse to
    # start a day into a visit that is mid-open.
    if str(state.get("state") or "") == "asleep" and str(state.get("mode") or "") == "visiting":
        raise _loop_refuse(
            409, "visit_opening",
            f"a visit is opening on {manifest.entity_id} (visiting posture set) — "
            "his own time and a visit never overlap; retry after the visit ends",
        )

    # Substrate resolves like the chat surface: request > operator env >
    # LOUD REFUSAL — never a code default (maintainer ruling 2026-07-09
    # 04:26: "I decide which provider and model is used ... NO FALLBACK";
    # refined 06:32: ONE substrate per entity — the loop resolves the SAME
    # persisted choice as visits). Attention geometry keeps its shared code
    # floors (a geometry floor is posture, not a substrate election).
    from ..entity_chat import (
        DEFAULT_ENTITY_CHAT_CONTEXT_WINDOW,
        DEFAULT_ENTITY_CHAT_SHELF_SIZE,
        ChatOpenRefused,
        resolve_substrate,
    )

    try:
        provider, model, thinking = resolve_substrate(req.provider, req.model, home_dir=home_dir)
    except ChatOpenRefused as e:
        raise HTTPException(status_code=e.status, detail=e.detail)
    base_url = (req.base_url or _os.getenv("ABSTRACTGATEWAY_ENTITY_CHAT_BASE_URL") or "http://127.0.0.1:1234/v1").strip()

    def _env_int(env_name: str) -> Optional[int]:
        raw = (_os.getenv(env_name) or "").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            import re as _re

            lead = _re.match(r"\s*(\d+)", raw)
            return int(lead.group(1)) if lead else None

    shelf_size = req.shelf_size
    if shelf_size is None:
        shelf_size = _env_int("ABSTRACTGATEWAY_ENTITY_CHAT_SHELF_SIZE") or DEFAULT_ENTITY_CHAT_SHELF_SIZE
    context_window = req.context_window
    if context_window is None:
        context_window = _env_int("ABSTRACTGATEWAY_ENTITY_CHAT_CONTEXT_WINDOW") or DEFAULT_ENTITY_CHAT_CONTEXT_WINDOW

    try:
        started = start_loop(
            home_dir,
            provider=provider,
            model=model,
            thinking=thinking,
            base_url=base_url,
            tick_seconds=req.tick_seconds,
            ticks_per_day=req.ticks_per_day,
            rest_minutes=req.rest_minutes,
            shelf_size=int(shelf_size),
            context_window=int(context_window),
        )
    except RuntimeError as e:
        # Spawn/lease failure from the loop process — surface it structured so
        # the button shows the reason (B2), not a bare 409.
        raise _loop_refuse(409, "start_failed", str(e))

    try:
        home = registry.get_home(manifest.slug)
        record_host_marker(
            entities_dir=registry.entities_dir,
            slug=manifest.slug,
            entity_id=manifest.entity_id,
            kind="personal_started",
            journal_seq=int(home.memory.current_seq()),
            # `by` = the acting principal (mutation adversary P1: channel-only
            # markers made a disputed start untraceable to a session).
            details={"channel": "operator", "by": actor, **{k: started[k] for k in ("pid", "provider", "model", "tick_seconds", "ticks_per_day", "rest_minutes")}},
        )
    except Exception:
        pass  # a marker failure never blocks his own time

    return {"started": True, **started, "status": loop_status(home_dir)}


class StopLoopRequest(BaseModel):
    mode: str = Field(
        default="graceful",
        description=(
            "graceful (default): durable stop command, honored at the loop's next boundary — "
            "his thought finishes whole. freeze: ADMIN hard stop / hibernation — the process is "
            "killed NOW, no ceremony, no further writes; the entity state is set to paused so "
            "the door refuses visits until an admin wakes him. The entity has zero control over "
            "either; freeze is for hard failures, digital diseases, imminent threat."
        ),
    )
    reason: str = Field(default="")


@router.post("/{name}/loop/stop")
def stop_entity_loop(name: str, req: Optional[StopLoopRequest] = None) -> Dict[str, Any]:
    from ..entity_loop import freeze_loop, stop_loop
    from ..entity_replay import record_host_marker

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    home_dir = registry.entities_dir / manifest.slug

    from ..security.principal import current_gateway_principal

    principal = current_gateway_principal()
    actor = f"person:{principal.user_id}" if principal is not None else "person:operator"
    mode = (req.mode if req else "graceful").strip().lower()
    reason = (req.reason if req else "") or "own time stop via gateway"
    if mode in ("freeze", "hard", "hibernate"):
        # FREEZE (maintainer ruling 2026-07-08): admin-only hibernation — the
        # process dies now, and the paused state closes the door until an
        # admin wakes him. Nothing changes in the memory graph.
        # Mid-visit guard (observer 2026-07-09): freeze kills the loop
        # PROCESS, but an open visit lives in the GATEWAY process — close it
        # too (no reflection: a freeze is not a graceful goodbye), else the
        # emergency stop leaves the chat writing episodes.
        from abstractruntime.identity.life import write_entity_state

        try:
            _chat_host().close_open_visit(name, reflect=False)
        except HTTPException:
            raise
        except Exception:
            pass
        result = freeze_loop(home_dir, reason=reason, requested_by="admin")
        write_entity_state(home_dir, "paused", reason=f"FROZEN: {reason} [by {actor} via POST /entities/{name}/loop/stop]")
        try:
            home = registry.get_home(manifest.slug)
            record_host_marker(
                entities_dir=registry.entities_dir,
                slug=manifest.slug,
                entity_id=manifest.entity_id,
                kind="personal_frozen",
                journal_seq=int(home.memory.current_seq()),
                details={"channel": "admin", "by": actor, "reason": reason, **{k: result[k] for k in ("pid", "was_running", "escalated_to_sigkill")}},
            )
        except Exception:
            pass
        return {"frozen": True, **result}
    if mode != "graceful":
        raise HTTPException(status_code=400, detail="mode must be 'graceful' or 'freeze'")

    # Durable command, not a sentinel file (maintainer ruling 2026-07-08):
    # the loop consumes it at its next boundary; his thought finishes whole.
    status = stop_loop(home_dir, reason=reason, requested_by="operator")
    try:
        home = registry.get_home(manifest.slug)
        record_host_marker(
            entities_dir=registry.entities_dir,
            slug=manifest.slug,
            entity_id=manifest.entity_id,
            kind="personal_stop_requested",
            journal_seq=int(home.memory.current_seq()),
            details={"channel": "operator", "by": actor, "phase": status.get("phase")},
        )
    except Exception:
        pass
    return {"stop_requested": True, "status": status}


# ----------------------------------------------------------- personal grant
# PERSONAL IS THE CLICK (laurent 2026-07-15 21:38: "on entity app, i click
# on 'personal', and the entity is then authorize to tick itself...
# SIMPLIFY, do not put excessive guardrails" — superseding the SEPARATE
# arming ceremony of c815/c1427 for the operator door): the operator's
# authenticated personal-time start IS the grant; /loop/start arms an
# unarmed bucket itself. This surface remains for timers and revocation.
# What survives of the old rider: no ENTITY, visit, or harness path arms
# anything — arming happens only on operator-authenticated doors, and the
# runtime loop gate still re-checks the grant at every day-open. The
# activation bucket phases.personal.{mode, expires_at, granted_by,
# granted_at} in <home>/phases.yaml (runtime owns the format module —
# read/write_personal_grant). Marker-first ENFORCED like substrate: an
# unrecorded grant change is the 10:20 incident class.


class PutPersonalGrantRequest(BaseModel):
    mode: str = Field(..., description="disabled | timer | until_revoked (the ruled modes; disabled = revoke)")
    expires_at: Optional[str] = Field(default=None, description="ISO-8601 expiry (required for mode=timer; normalized to aware UTC)")


def _apply_personal_grant(registry: EntityRegistry, manifest: Any, *, mode: str, expires_at: Optional[str]) -> None:
    """The ONE grant-change implementation: validate, marker-first, write.

    Both writers land here — the PUT surface (timers/revocation) and the
    /loop/start arm-on-start (the operator's click). Raises HTTPException
    on refusal; the personal_granted / personal_grant_revoked marker lands
    BEFORE the file moves, and validation runs BEFORE the marker so a
    refused write never leaves a granted marker behind."""
    from abstractruntime.identity.life import (
        PERSONAL_GRANT_MODES,
        PHASES_FILENAME,
        PHASES_SCHEMA_VERSION,
        read_personal_grant,
        write_personal_grant,
    )

    from ..entity_replay import record_host_marker
    from ..security.principal import current_gateway_principal

    home_dir = registry.entities_dir / manifest.slug
    principal = current_gateway_principal()
    actor = f"person:{principal.user_id}" if principal is not None else "person:operator"
    mode = str(mode or "").strip().lower()
    prior = read_personal_grant(home_dir)

    # Validate BEFORE the marker: marker-first must never record an act the
    # write would refuse (a personal_granted marker over a 400 is a lie).
    if mode not in PERSONAL_GRANT_MODES:
        raise HTTPException(status_code=400, detail=f"unknown personal mode {mode!r}: modes are {'/'.join(PERSONAL_GRANT_MODES)}")
    normalized_expiry: Optional[str] = None
    if mode == "timer":
        from abstractruntime.core.runtime import normalize_utc_iso

        if not str(expires_at or "").strip():
            raise HTTPException(status_code=400, detail="mode=timer requires expires_at — a timer without an expiry is no grant")
        normalized_expiry = normalize_utc_iso(str(expires_at).strip())
        if normalized_expiry is None:
            raise HTTPException(status_code=400, detail=f"expires_at is not an ISO-8601 timestamp: {expires_at!r}")
    # The writer's OWN refusal conditions, dry-run (adversary P2-2: corrupt
    # phases.yaml and newer schema_version raise AFTER the marker landed —
    # recording a grant that never happened, the exact class marker-first
    # exists to close). Constants imported, never respelled.
    phases_path = home_dir / PHASES_FILENAME
    if phases_path.exists():
        try:
            import yaml as _yaml

            loaded = _yaml.safe_load(phases_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(
                status_code=400,
                detail=f"{PHASES_FILENAME} is unreadable ({e}) — repair or remove it before arming",
            )
        if loaded is not None and not isinstance(loaded, dict):
            raise HTTPException(status_code=400, detail=f"{PHASES_FILENAME} is not a mapping — repair it before arming")
        try:
            found_version = int((loaded or {}).get("schema_version") or PHASES_SCHEMA_VERSION)
        except (TypeError, ValueError):
            found_version = PHASES_SCHEMA_VERSION
        if found_version > PHASES_SCHEMA_VERSION:
            raise HTTPException(
                status_code=409,
                detail=f"{PHASES_FILENAME} carries schema_version {found_version} but this gateway knows "
                f"{PHASES_SCHEMA_VERSION} — upgrade before writing",
            )

    # MARKER-FIRST, blocking (the substrate precedent): the act lands on the
    # stream BEFORE the file moves. personal_granted names the phase being
    # armed; personal_grant_revoked names the grant being taken back
    # (semantics c1443: the subject asymmetry is deliberate honesty).
    kind = "personal_grant_revoked" if mode == "disabled" else "personal_granted"
    try:
        home = registry.get_home(manifest.slug)
        details: Dict[str, Any] = {"channel": "operator", "by": actor, "mode": mode}
        if mode == "timer" and normalized_expiry:
            # NORMALIZED form (P3): the marker and the file must agree on
            # the one at-rest spelling, or the expiry dedup key splits.
            details["expires_at"] = normalized_expiry
        if mode == "disabled":
            details["prior_mode"] = prior.get("mode")
        record_host_marker(
            entities_dir=registry.entities_dir,
            slug=manifest.slug,
            entity_id=manifest.entity_id,
            kind=kind,
            journal_seq=int(home.memory.current_seq()),
            details=details,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail=f"personal-grant change refused: the durable marker could not be recorded ({e}) — "
            "an unrecorded grant change is not allowed; retry when the home is reachable",
        )
    try:
        write_personal_grant(home_dir, mode=mode, granted_by=actor, expires_at=expires_at)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{name}/personal-grant")
def get_personal_grant(name: str) -> Dict[str, Any]:
    """The grant axis, readable on its own (the /cognition composite carries
    the same block): armed = the phase MAY run right now (semantics c1436:
    ARMED ≠ IN-PHASE — the current phase is a separate fact)."""
    from abstractruntime.identity.life import personal_grant_refusal, read_personal_grant

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    grant = read_personal_grant(registry.entities_dir / manifest.slug)
    refusal = personal_grant_refusal(grant)
    armed = refusal is None
    if not armed:
        _maybe_mark_grant_expired(registry, manifest, grant)
    return {**grant, "armed": armed, "refusal": refusal, "source": "phases.yaml"}


@router.put("/{name}/personal-grant")
def put_personal_grant(name: str, req: PutPersonalGrantRequest) -> Dict[str, Any]:
    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    _apply_personal_grant(registry, manifest, mode=req.mode, expires_at=req.expires_at)
    return get_personal_grant(name)
