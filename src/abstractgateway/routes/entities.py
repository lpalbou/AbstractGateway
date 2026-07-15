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

import secrets
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..config import entity_iterations_ceiling
from ..entities import EntityRegistry, entity_slug
from ..service import get_gateway_service

router = APIRouter(prefix="/gateway/entities", tags=["entities"])


def _registry() -> EntityRegistry:
    svc = get_gateway_service()
    registry = getattr(svc, "entity_registry", None)
    if isinstance(registry, EntityRegistry):
        return registry
    return EntityRegistry(data_dir=svc.config.data_dir)


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


@router.post("", status_code=201)
def create_entity(req: CreateEntityRequest) -> Dict[str, Any]:
    """Create an entity home: lint -> store the spark verbatim -> engram ->
    manifest. Idempotent for the same spark (`created=false`); a CHANGED
    document is refused with the engine's human-written error (409)."""
    from ..entities import EntityQuotaExceeded

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
    return result.to_dict()


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
    from ..security.principal import current_gateway_principal

    principal = current_gateway_principal()
    actor = f"person:{principal.user_id}" if principal is not None else "person:operator"
    reason_in = str(req.reason or "").strip()
    stamp = f"[by {actor} via POST /entities/{name}/state]"
    stamped_reason = f"{reason_in} {stamp}".strip() if reason_in else stamp
    closed: Optional[Dict[str, Any]] = None
    closed_durable: Optional[Dict[str, Any]] = None
    # STATE WRITES FIRST (state-sources adversary P0-2, trigger b): the state
    # file is the coordination authority — writing it before the teardown
    # makes the visit gates (open/turn/tick check asleep+paused) refuse any
    # NEW work landing in the teardown window, so a raced open can no longer
    # survive the sleep. The teardown below then closes what was already
    # open; its terminal duty sees the operator's fresh state (not a
    # visit-authored one) and leaves it standing.
    try:
        result = _registry().set_state(name=name, state=req.state, reason=stamped_reason, dream=bool(req.dream))
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


class VisitCloseRequest(BaseModel):
    closed_by: str = Field(
        default="operator",
        description="operator | sleep (reflection runs) | pause (hard freeze: skip_reflection, no cognition)",
    )
    reason: str = Field(default="", description="Why — reaches the reflection look-back (sleep/operator)")


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
        raise HTTPException(status_code=e.status, detail=e.detail)


@router.post("/{name}/visit/{run_id}/turn")
def visit_turn(name: str, run_id: str, req: VisitTurnRequest) -> Dict[str, Any]:
    from ..entity_visits import VisitRefused

    try:
        return _visit_host().turn(name, run_id, text=req.text, speaker=req.speaker)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except VisitRefused as e:
        raise HTTPException(status_code=e.status, detail=e.detail)


@router.post("/{name}/visit/{run_id}/close")
def visit_close(name: str, run_id: str, req: VisitCloseRequest) -> Dict[str, Any]:
    from ..entity_visits import VisitRefused

    try:
        return _visit_host().close(name, run_id, closed_by=req.closed_by, reason=req.reason)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except VisitRefused as e:
        raise HTTPException(status_code=e.status, detail=e.detail)


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
        raise HTTPException(status_code=e.status, detail=e.detail)


@router.get("/{name}/visit")
def visit_status(name: str) -> Dict[str, Any]:
    from ..entity_visits import VisitRefused

    try:
        return _visit_host().status(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except VisitRefused as e:
        raise HTTPException(status_code=e.status, detail=e.detail)


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
        raise HTTPException(status_code=e.status, detail=e.detail)


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
      runtime's loop-usage half lands)."""
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
    if stopped:
        # The kill switch is NOT a phase — everything active was torn down;
        # the state block carries paused and liveness says stopped plainly.
        phase = None
    elif bool(visit.get("open")) or chat_open:
        phase = "visit"
    elif state_word == "asleep":
        phase = "sleep"
    elif bool(loop.get("running")):
        # The personal phase spans its own rest windows (the loop lives);
        # `resting` carries the between-days nuance without a fifth phase.
        phase = "personal"
    else:
        phase = None  # awake, idle — present, no phase active
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
    out: Dict[str, Any] = {
        "working": working,
        "phase": phase,
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
            out["phase"] = "visiting"
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
    del name
    from ..entity_chat import ChatOpenRefused
    from fastapi.concurrency import run_in_threadpool

    try:
        return await run_in_threadpool(
            _chat_host().close, chat_id, reflect=bool(req.reflect) if req is not None else True
        )
    except ChatOpenRefused as e:
        raise HTTPException(status_code=e.status, detail=e.detail)


@router.get("/{name}/chat")
def entity_chat_status(name: str) -> Dict[str, Any]:
    """Is a visit open on this home right now? (one life, one summon)"""
    try:
        return _chat_host().status(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))


class SummonEntityRequest(BaseModel):
    prompt: str = Field(..., description="The work brief for this session")
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
def summon_entity(name: str, req: SummonEntityRequest) -> Dict[str, Any]:
    """Summon the entity into a work session.

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
    """
    from abstractruntime.identity import render_summon_prelude

    from ..entity_gate import (
        CHANNEL_WORKPLACE,
        finalize_summon_stamp,
        mint_summon_stamp,
        summon_budget_profile,
    )
    from ..security.principal import current_gateway_principal

    svc = get_gateway_service()
    registry = _registry()

    try:
        home = registry.get_home(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

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
        from abstractruntime.identity.life import write_entity_state

        principal_for_wake = current_gateway_principal()
        summoner = f"person:{principal_for_wake.user_id}" if principal_for_wake is not None else "person:operator"
        write_entity_state(
            registry.entities_dir / entity_slug(name), "awake",
            reason=f"woken by summon from {summoner}",
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
    # contract): the verified participant is the authenticated principal;
    # a local single-operator gateway (auth off) is the operator. The entity
    # stamps ITSELF into its own records (EXPLICIT co-presence, ruled a2a
    # 0007: the entity IS present at its own session by construction —
    # owners are never implied).
    principal = current_gateway_principal()
    participants: List[str] = [f"person:{principal.user_id}"] if principal is not None else ["person:operator"]
    participants.append(home.entity_id)

    input_data: Dict[str, Any] = dict(req.input_data or {})
    input_data["prompt"] = req.prompt
    caller_system = str(input_data.get("system") or "").strip()
    input_data["system"] = prelude["text"] + (("\n\n" + caller_system) if caller_system else "")

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
        "prelude": {
            "text": prelude["text"],
            "section_tokens": prelude.get("section_tokens"),
            "as_of_seq": prelude.get("as_of_seq"),
            "spark_version": prelude.get("spark_version"),
            "warnings": list(prelude.get("warnings") or []),
        },
    }


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
    phases: Dict[str, Any] = {}
    for phase in PHASES:
        # Display resolution: the bare default posture — legacy visit/tasked/own_time
        # show the full ruled default (tier-1 + workspace, maintainer
        # 2026-07-11; Q1 c684), sleep the read-only exploration set;
        # enable_workspace is inert.
        grant = resolve_tool_grant(home_dir, phase, enable_workspace=False)
        phases[phase] = {"tools": list(grant.tools), "source": grant.source, "notes": list(grant.notes)}
    return {
        "phases": phases,
        "all_tools": list(ALL_TOOL_NAMES),
        "tiers": {tier: list(names) for tier, names in TIERS.items()},
    }


@router.put("/{name}/tool-policy")
def put_entity_tool_policy(name: str, req: PutToolPolicyRequest) -> Dict[str, Any]:
    from abstractruntime import write_policy_file

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    try:
        write_policy_file(registry.entities_dir / manifest.slug, req.policy)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return get_entity_tool_policy(name)


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
        return {"provider": stored["provider"], "model": stored["model"], "source": "entity"}
    env_p = (_os.getenv("ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER") or "").strip()
    env_m = (_os.getenv("ABSTRACTGATEWAY_ENTITY_CHAT_MODEL") or "").strip()
    if env_p and env_m:
        return {"provider": env_p, "model": env_m, "source": "operator-env"}
    return {"provider": None, "model": None, "source": "unset"}


@router.put("/{name}/substrate")
def put_entity_substrate(name: str, req: PutSubstrateRequest) -> Dict[str, Any]:
    """The ONE sanctioned substrate write path (laurent 12:39, hypnos
    incident): a mind swap is a DURABLE EVENT on the life — the marker
    (old → new, principal, timestamp) lands BEFORE the file moves, so
    "which llm was behind during which time" is answerable from the
    stream even if the write itself crashes. Direct file edits bypass
    this record; the 12:24 emergency flip proved the gap from inside."""
    from ..entity_chat import read_entity_substrate, write_entity_substrate
    from ..security.principal import current_gateway_principal

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    home_dir = registry.entities_dir / manifest.slug
    # Strip-validate BEFORE the marker (adversary P2-2: min_length=1 accepts
    # " "; the writer strips and raises AFTER the substrate_changed marker
    # landed — a recorded mind-swap that never happened).
    provider_in = str(req.provider or "").strip()
    model_in = str(req.model or "").strip()
    if not provider_in or not model_in:
        raise HTTPException(status_code=400, detail="provider and model must both be non-empty")
    prior = read_entity_substrate(home_dir) or {}
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
                "old": {"provider": prior.get("provider"), "model": prior.get("model")},
                "new": {"provider": provider_in, "model": model_in},
            },
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=503,
            detail=f"substrate change refused: the durable marker could not be recorded ({e}) — "
            "an unrecorded mind swap is not allowed; retry when the home is reachable",
        )
    try:
        write_entity_substrate(home_dir, provider=provider_in, model=model_in)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return get_entity_substrate(name)


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
        provider, model = resolve_substrate(req.provider, req.model, home_dir=home_dir)
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
