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

from ..entities import EntityRegistry
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


@router.post("", status_code=201)
async def create_entity(req: CreateEntityRequest) -> Dict[str, Any]:
    """Create an entity home: lint -> store the spark verbatim -> engram ->
    manifest. Idempotent for the same spark (`created=false`); a CHANGED
    document is refused with the engine's human-written error (409)."""
    try:
        result = _registry().create(
            name=req.name,
            spark=req.spark,
            spark_text=req.spark_text,
            framework=bool(req.framework),
        )
    except ValueError as e:
        # Lint errors, name mismatches, and spark-drift refusals are written
        # for humans — surface them verbatim. Drift/conflict reads as 409.
        detail = str(e)
        status = 409 if "DIFFERENT" in detail or "already" in detail.lower() else 400
        raise HTTPException(status_code=status, detail=detail)
    return result.to_dict()


@router.get("")
async def list_entities() -> Dict[str, Any]:
    return {"entities": _registry().list_entities()}


@router.get("/{name}")
async def inspect_entity(name: str, diary_limit: int = 5, standings_top_k: int = 10) -> Dict[str, Any]:
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
async def entity_card(
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
async def verify_entity(name: str) -> Dict[str, Any]:
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
async def set_entity_state(name: str, req: SetEntityStateRequest) -> Dict[str, Any]:
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
    close)."""
    target = str(req.state or "").strip().lower()
    closed: Optional[Dict[str, Any]] = None
    if target in ("asleep", "paused"):
        try:
            closed = _chat_host().close_open_visit(name, reflect=(target == "asleep"))
        except HTTPException:
            raise
        except Exception:
            closed = None  # no chat host on this service shape
    try:
        result = _registry().set_state(name=name, state=req.state, reason=req.reason, dream=bool(req.dream))
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if closed is not None:
        result["closed_visit"] = {"turns": closed.get("turns"), "summary": closed.get("summary")}
    return result


@router.get("/{name}/state")
async def get_entity_state(name: str) -> Dict[str, Any]:
    """The operator state badge source (pure read: awake/asleep/paused + mode)."""
    try:
        return _registry().state_of(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{name}/life_state")
async def get_entity_life_state(name: str) -> Dict[str, Any]:
    """The ONE composite life phase (observer ask, maintainer 2026-07-09):
    the gateway collapses chat + operator-state + loop into a single
    mutually-exclusive `phase` (visiting > paused > asleep > own_time >
    resting > awake) so clients render one chip and never re-derive
    contradictory badges. `own_time_running` rides alongside for a
    loop-alive indicator that does not fight the phase."""
    try:
        return _chat_host().life_state(name)
    except HTTPException:
        raise
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


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
        description="Recall shelf seats (None = env override or the wide default 36 — "
        "maintainer 2026-07-09: widened so the entity retrieves enough memories to function)",
    )
    max_output_tokens: int = Field(default=2048)
    enable_tools: bool = Field(default=True, description="Tier-1 tool blocks (web_search / diary_list / diary_read / read_memory)")
    enable_workspace: bool = Field(default=False, description="Workspace tools contained to <home>/workspace/")


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


@router.get("/{name}/chat/{chat_id}/transcript")
async def entity_chat_transcript(name: str, chat_id: str) -> Dict[str, Any]:
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
async def entity_chat_status(name: str) -> Dict[str, Any]:
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
async def summon_entity(name: str, req: SummonEntityRequest) -> Dict[str, Any]:
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

    # The state door (a2a 0008): asleep = the no-summon window (dreams may
    # run there); paused = hard freeze. Both refuse summons, naming the
    # state and its reason — the coordination rule enforced by state, not
    # etiquette.
    entity_state = registry.state_of(name)
    if str(entity_state.get("state") or "awake") != "awake":
        raise HTTPException(
            status_code=409,
            detail={
                "refused": True,
                "reasons": [
                    f"#REFUSED summon: {home.entity_id} is {entity_state.get('state')}"
                    + (f" ({entity_state.get('reason')})" if entity_state.get("reason") else "")
                    + " — wake it first (entity wake / POST .../state)"
                ],
                "entity_id": home.entity_id,
                "state": entity_state,
            },
        )

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
async def list_entity_workspace(name: str, path: str = ".") -> Dict[str, Any]:
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
async def read_entity_workspace_file(name: str, path: str) -> Dict[str, Any]:
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
async def write_entity_workspace_file(name: str, req: WorkspaceFileWriteRequest) -> Dict[str, Any]:
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
async def get_entity_workspace_mounts(name: str) -> Dict[str, Any]:
    from abstractruntime.identity.tools import read_workspace_mounts

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    return {"mounts": read_workspace_mounts(registry.entities_dir / manifest.slug)}


@router.put("/{name}/workspace/mounts")
async def put_entity_workspace_mounts(name: str, req: PutMountsRequest) -> Dict[str, Any]:
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
    policy: Dict[str, List[str]] = Field(
        ..., description='Explicit tools per phase, e.g. {"visit": ["diary_list"], "resident": [...], "sleep": []}'
    )


@router.get("/{name}/tool-policy")
async def get_entity_tool_policy(name: str) -> Dict[str, Any]:
    from abstractruntime.identity.tool_policy import ALL_TOOL_NAMES, PHASES, TIERS, resolve_tool_grant

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    home_dir = registry.entities_dir / manifest.slug
    phases: Dict[str, Any] = {}
    for phase in PHASES:
        # Display resolution: the bare default posture (visit shows tier-1;
        # a per-session --workspace flag is not the file's business).
        grant = resolve_tool_grant(home_dir, phase, enable_workspace=False)
        phases[phase] = {"tools": list(grant.tools), "source": grant.source, "notes": list(grant.notes)}
    return {
        "phases": phases,
        "all_tools": list(ALL_TOOL_NAMES),
        "tiers": {tier: list(names) for tier, names in TIERS.items()},
    }


@router.put("/{name}/tool-policy")
async def put_entity_tool_policy(name: str, req: PutToolPolicyRequest) -> Dict[str, Any]:
    from abstractruntime.identity.tool_policy import write_policy_file

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    try:
        write_policy_file(registry.entities_dir / manifest.slug, req.policy)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return await get_entity_tool_policy(name)


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
async def get_entity_substrate(name: str) -> Dict[str, Any]:
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
async def put_entity_substrate(name: str, req: PutSubstrateRequest) -> Dict[str, Any]:
    from ..entity_chat import write_entity_substrate

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    try:
        write_entity_substrate(registry.entities_dir / manifest.slug, provider=req.provider, model=req.model)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return await get_entity_substrate(name)


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
    # defaults (shelf 36, context 65536 — maintainer rulings 2026-07-08/09).
    shelf_size: Optional[int] = Field(default=None, ge=1, le=64)
    context_window: Optional[int] = Field(default=None, ge=20000)


@router.get("/{name}/loop")
async def get_entity_loop(name: str) -> Dict[str, Any]:
    """The loop's honest state (its own status file, pid-checked)."""
    from ..entity_loop import loop_status

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    return loop_status(registry.entities_dir / manifest.slug)


@router.post("/{name}/loop/start")
async def start_entity_loop(name: str, req: StartLoopRequest) -> Dict[str, Any]:
    import os as _os

    from ..entity_loop import loop_status, start_loop
    from ..entity_replay import record_host_marker

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    home_dir = registry.entities_dir / manifest.slug

    # One life, one summon: a live visit and his own time never overlap.
    try:
        chat = _chat_host().status(name)
        if chat.get("open"):
            raise HTTPException(
                status_code=409,
                detail=f"a visit is open on {manifest.entity_id} (chat {chat.get('chat_id')!r}) — "
                "close it first; his own time and a visit never overlap",
            )
    except HTTPException:
        raise
    except Exception:
        pass  # no chat host on this service shape: nothing to collide with

    from abstractruntime.identity.life import read_entity_state

    state = read_entity_state(home_dir)
    if state.get("state") == "paused":
        raise HTTPException(status_code=409, detail=f"{manifest.entity_id} is paused (hard freeze) — wake him first")
    # Registration-window guard (observer 2026-07-09): a visit `open()` writes
    # the visiting posture (asleep + mode=visiting) BEFORE it registers in the
    # chat host's _by_slug, so a loop/start racing an in-flight open would pass
    # the status() check above. The state marker closes that window — refuse to
    # start a day into a visit that is mid-open.
    if str(state.get("state") or "") == "asleep" and str(state.get("mode") or "") == "visiting":
        raise HTTPException(
            status_code=409,
            detail=f"a visit is opening on {manifest.entity_id} (visiting posture set) — "
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
        raise HTTPException(status_code=409, detail=str(e))

    try:
        home = registry.get_home(manifest.slug)
        record_host_marker(
            entities_dir=registry.entities_dir,
            slug=manifest.slug,
            entity_id=manifest.entity_id,
            kind="own_time_started",
            journal_seq=int(home.memory.current_seq()),
            details={"channel": "operator", **{k: started[k] for k in ("pid", "provider", "model", "tick_seconds", "ticks_per_day", "rest_minutes")}},
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
async def stop_entity_loop(name: str, req: Optional[StopLoopRequest] = None) -> Dict[str, Any]:
    from ..entity_loop import freeze_loop, stop_loop
    from ..entity_replay import record_host_marker

    registry = _registry()
    try:
        manifest = registry.manifest_for(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e).strip("'\""))
    home_dir = registry.entities_dir / manifest.slug

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
        write_entity_state(home_dir, "paused", reason=f"FROZEN: {reason}")
        try:
            home = registry.get_home(manifest.slug)
            record_host_marker(
                entities_dir=registry.entities_dir,
                slug=manifest.slug,
                entity_id=manifest.entity_id,
                kind="own_time_frozen",
                journal_seq=int(home.memory.current_seq()),
                details={"channel": "admin", "reason": reason, **{k: result[k] for k in ("pid", "was_running", "escalated_to_sigkill")}},
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
            kind="own_time_stop_requested",
            journal_seq=int(home.memory.current_seq()),
            details={"channel": "operator", "phase": status.get("phase")},
        )
    except Exception:
        pass
    return {"stop_requested": True, "status": status}
