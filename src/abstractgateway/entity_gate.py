"""The entity deposit gate: actor by CHANNEL, never by payload (a2a 0004).

THE LOAD-BEARING LINE OF THE DOOR (co-stated by runtime + memory on thread
0004): the VERIFIED summon stamp is what makes actor strings TRUE for the
engine's amplitude authority and identity-kind write rules. This module
therefore (1) verifies the stamp at the ROUTING layer — before any home
opens, before any handler is constructed; crypto never enters the seam
handlers — and (2) STRIPS every payload-supplied `actor` and injects the
channel-derived one. If verification fails, the run never reaches an entity
home with ANY actor string: the effect fails with the loud door error.

Two independent locks (runtime's framing): the verified stamp decides WHICH
home opens (or none); the runtime factories bind the entity at construction,
so a routed run cannot reach past its home.

Channels (the write-authorization table, a2a 0003/0004):
- `workplace`         a summoned work session (the summon endpoint stamps it).
                      Actor string: `workplace:<session_id>` — routine
                      appraisal band (magnitude 1..3), no identity-kind
                      writes, no self-scope belief revision.
- `entity-reflection` runs the home itself spawns (the future heartbeat /
                      reflection loop). Identity evolution happens HERE.
- `operator`          the authenticated admin surface (CLI / admin HTTP).

Trust boundary honesty: the stamp's HMAC authenticates "minted by THIS
gateway" (one trust domain, one per-data-root secret). It does NOT
authenticate remote workplaces — that is the deferred 008 keys work.

The stamp/actor-by-channel design implements, one layer down, the
maintainer's AI-fingerprints direction (identity that is verified at the
boundary, never self-claimed):
https://medium.com/@lpalbou/the-rise-of-cognitive-architectures-and-the-need-for-ai-fingerprints-fcee286c0c33
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ONE SOURCE for the identity floor (memory's round-7 hardening; the clamp
# lesson applied): the engine exports the constant, the gate enforces the
# CHANNEL policy against the imported value — no second copy to drift.
from abstractmemory.seam import SELF_FRACTION_FLOOR as SUMMON_IDENTITY_FLOOR

from .entities import DIARY_SCOPE, LIFE_SCOPE, SELF_SCOPE, EntityHome, EntityRegistry

logger = logging.getLogger(__name__)

# The entity brain grew past the original seven seam/diary effects
# (MEMORY_CONSOLIDATE/MEMORY_PROBE/LIFE_QUERY, then MEMORY_TEND) — the
# in-process lanes bind them via open_home, and the door must route the SAME
# set or bundle-lane entity runs fail "No effect handler registered" one
# effect at a time (live: memory_probe, flow c5237, exposed the moment the
# reload re-arm fix let runs get past recall). ONE SOURCE for the set:
# ENTITY_HOME_EFFECT_TYPES from the runtime, so the NEXT effect type cannot
# re-open this gap (the diary_type-clamp drift class). Version skew (an older
# runtime without the brain wave) degrades LOUDLY to the legacy seven — never
# crashes the whole entity block.
try:
    from abstractruntime.integrations.abstractmemory import (
        ENTITY_HOME_EFFECT_TYPES as _ENTITY_HOME_EFFECT_TYPES,
    )
    from abstractruntime.integrations.abstractmemory.brain_handlers import (
        build_entity_brain_effect_handlers as _build_brain_handlers,
    )
except Exception as _skew:  # pragma: no cover - version-skew degrade
    _ENTITY_HOME_EFFECT_TYPES = None
    _build_brain_handlers = None
    logging.getLogger(__name__).warning(
        "#FALLBACK entity brain handlers unavailable (older abstractruntime?): %s — "
        "the door routes only the legacy seven entity effects; "
        "MEMORY_CONSOLIDATE/MEMORY_PROBE/LIFE_QUERY/MEMORY_TEND will refuse",
        _skew,
    )

# Tool surface (flow's 0.0.10 tools wave, c5300/c5304 — runtime named this
# bind as the door's missing half): the pair registers per home exactly like
# the brain quartet. Version skew degrades LOUDLY, never crashes the block.
try:
    from abstractruntime.identity.tool_effects import (
        build_entity_tool_effect_handlers as _build_tool_handlers,
    )
except Exception as _tool_skew:  # pragma: no cover - version-skew degrade
    _build_tool_handlers = None
    logging.getLogger(__name__).warning(
        "#FALLBACK entity tool handlers unavailable (older abstractruntime?): %s — "
        "ENTITY_TOOLS_QUERY/ENTITY_TOOLS_EXECUTE will refuse on the door lane",
        _tool_skew,
    )


class _ToolEffectHome:
    """The home surface runtime's tool handlers need, DOOR posture.

    Composes `_ContainedReaderHome` (the c69 containment: private diary
    GISTS stay word-free in search/list results while entries remain
    findable) with the two extra fields the tool pair reads: `home_dir`
    (grant resolution) and the built handler dict (diary_read serves the
    entry words through the REAL handler — the A-ruling: the home is the
    privacy boundary, and the visit runs AS the entity). The handlers dict
    is taken BY REFERENCE before the tool pair lands in it; DIARY_READ is
    already bound at that point."""

    def __init__(self, home: Any, handlers: Dict[Any, Any]) -> None:
        from .entities import _ContainedReaderHome

        base = _ContainedReaderHome(home)
        self.store = base.store
        self.journal = base.journal
        self.entity_id = base.entity_id
        self.diary = base.diary
        self.ms = base.ms
        self.artifacts = base.artifacts
        self.home_dir = home.home_dir
        self.handlers = handlers


__all__ = [
    "CHANNEL_ENTITY_REFLECTION",
    "CHANNEL_OPERATOR",
    "CHANNEL_WORKPLACE",
    "ENTITY_STAMP_KEY",
    "SUMMON_POSTURE_BUDGET",
    "finalize_summon_stamp",
    "install_entity_routing",
    "wrap_entity_runtime_routing",
    "mint_summon_stamp",
    "verify_summon_stamp",
    "resolved_phase",
]

CHANNEL_WORKPLACE = "workplace"
CHANNEL_ENTITY_REFLECTION = "entity-reflection"
CHANNEL_OPERATOR = "operator"
_CHANNELS = (CHANNEL_WORKPLACE, CHANNEL_ENTITY_REFLECTION, CHANNEL_OPERATOR)

# Where the stamp lives in run vars: vars["_runtime"]["entity"].
ENTITY_STAMP_KEY = "entity"

# The summon posture (keystone D2 + maintainer ruling: identity is always
# present for a summoned entity; reserved seats on). Injected when a recall
# budget omits self_fraction; an EXPLICIT self_fraction <= 0 is rejected —
# omission is delegation, explicit zero is contradiction (memory's phrasing).
SUMMON_POSTURE_BUDGET: Dict[str, Any] = {
    "self_fraction": 0.5,
    "shelf_size": 12,
    "token_budget": 2400,
    "stm_fraction": 0.25,
}

# The identity FLOOR + entity-elected hyperfocus (maintainer round 7):
# "self fraction of zero is never advisable as you lose identity. Minimum
# should be 5%... it is not unreasonable that this criteria could vary,
# PROVIDED it is upon the active agency of the summoned entity." Below the
# hard floor (SUMMON_IDENTITY_FLOOR, engine-exported) nobody goes; between
# floor and posture default only the entity itself may elect (hyperfocus —
# a conscious, risky, reversible tradeoff); raising identity presence is
# never a threat and stays anyone's to do.
#
# RULED (a2a 0003, memory's round-7 ask 1 → option a): the gate carries NO
# seat arithmetic of its own — "at least one reserved seat that actually
# renders" is STRUCTURAL engine-side (seat derivation max(1, round(f×shelf))
# + the first-seat token guarantee), and duplicating it here was refusing
# configurations the engine handles correctly.

# The context RECOMMENDATION for summoned-entity sessions (operator
# re-ruling 2026-08-01, second pass — "40k: it is acceptable to go to 200k
# context, but ideally, let's have a (soft) recommended target of 50k
# tokens"): 50k is the recommended working size, and the first pass's
# soft-limit language still governs ("more a soft than a hard limit ... if
# it needs to grow, it needs to grow"). The summon endpoint no longer
# refuses smaller declared windows: it ACCEPTS them with a labeled
# #RECOMMENDED warning; an unverifiable window proceeds with a labeled
# #FALLBACK warning (the gateway cannot measure a model it cannot resolve —
# the recommendation is then the operator's to weigh). Growth above the
# recommendation is never blocked — a window declared above the 200k
# ACCEPTABLE ceiling gets the same #RECOMMENDED-class soft warning and
# proceeds. One source (memory's exports; same discipline as the identity
# floor). The legacy alias name stays so importers keep working.
from abstractmemory import ENTITY_CONTEXT_ACCEPTABLE as SUMMON_CONTEXT_ACCEPTABLE_TOKENS  # noqa: E402
from abstractmemory import ENTITY_CONTEXT_FLOOR as SUMMON_CONTEXT_FLOOR_TOKENS  # noqa: E402

SUMMON_CONTEXT_RECOMMENDED_TOKENS = SUMMON_CONTEXT_FLOOR_TOKENS


def summon_budget_profile(
    context_window_tokens: Optional[int],
    *,
    shelf_size: Optional[int] = None,
) -> Dict[str, Any]:
    """The recall budget a summoned session runs with: memory's context-
    scaled profile (`entity_recall_budget`) with the reserved-seats posture
    applied on top.

    Defaults follow the maintainer's WIDE ruling (2026-07-08: "wide
    attention is the default, not a tuning trick" — request > env > code
    default, the SAME resolution as the chat and loop doors): an undeclared
    window derives from the wide default (65536), not the 20k floor — the
    floor is a refusal line, never a target. This closed the flow-summon
    starvation Ariadne's review found (shelf 12 / 2400 tokens hardcoded
    while the doors served shelf 24 / 65536).

    ATTENTION IS SIZED BY THE RECOMMENDATION, NOT THE WINDOW (operator
    2026-08-01 re-ruling — the within-turn audit found the door budgeting
    12% of its own 65,536 default while the 50k recommendation was never
    consulted): the recall token budget derives from
    min(declared_or_default_window, ENTITY_CONTEXT_RECOMMENDED), so the
    decoration budget is 12% × 50k = 6,000 tokens no matter how wide the
    context is allowed to grow. The recommendation sizes ATTENTION; the
    window sizes GROWTH — a 200k session may still grow to 200k of
    history, it just doesn't recall 24k of memories per turn on the way.
    Windows BELOW the recommendation keep scaling down (min() is a ceiling
    on the sizing input, never a raise)."""
    import dataclasses

    from abstractmemory import ENTITY_CONTEXT_RECOMMENDED, entity_recall_budget

    from .entity_chat import (
        DEFAULT_ENTITY_CHAT_CONTEXT_WINDOW,
        DEFAULT_ENTITY_CHAT_SHELF_SIZE,
    )

    def _env_int(name: str) -> Optional[int]:
        raw = os.getenv(name)
        try:
            return int(str(raw).strip()) if raw and str(raw).strip() else None
        except ValueError:
            return None

    window = (
        int(context_window_tokens)
        if context_window_tokens
        else (_env_int("ABSTRACTGATEWAY_ENTITY_CHAT_CONTEXT_WINDOW") or DEFAULT_ENTITY_CHAT_CONTEXT_WINDOW)
    )
    shelf = (
        int(shelf_size)
        if shelf_size
        else (_env_int("ABSTRACTGATEWAY_ENTITY_CHAT_SHELF_SIZE") or DEFAULT_ENTITY_CHAT_SHELF_SIZE)
    )
    # The recommendation sizes attention; the window sizes growth (docstring
    # above carries the ruling). Only the recall-budget SIZING input is
    # clamped — nothing here caps the session's actual context.
    attention_window = min(window, int(ENTITY_CONTEXT_RECOMMENDED))
    budget = dataclasses.asdict(entity_recall_budget(attention_window, shelf_size=shelf))
    budget["self_fraction"] = SUMMON_POSTURE_BUDGET["self_fraction"]
    return budget

# Identity kinds are engrammed/evolved through the entity's own reflection or
# the operator — never formed by a workplace turn ("workplaces PROPOSE via
# inbox records; only the self disposes").
_IDENTITY_KINDS = frozenset({"value", "purpose", "trait", "interest"})

_SECRET_FILENAME = ".stamp_secret"
_secret_lock = threading.Lock()


# ---------------------------------------------------------------------------
# The summon stamp (mint at the door, verify at the routing layer)
# ---------------------------------------------------------------------------


def _stamp_secret(data_dir: Path) -> str:
    """Per-data-root secret (0600). Env override for multi-process setups."""
    raw = os.getenv("ABSTRACTGATEWAY_ENTITY_STAMP_SECRET")
    if raw and raw.strip():
        return raw.strip()
    path = Path(data_dir) / "entities" / _SECRET_FILENAME
    with _secret_lock:
        try:
            if path.exists():
                existing = path.read_text(encoding="utf-8").strip()
                if existing:
                    return existing
        except Exception:
            pass
        secret = secrets.token_urlsafe(48)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(secret + "\n")
        except FileExistsError:
            existing = path.read_text(encoding="utf-8").strip()
            return existing or secret
        return secret


# Phase vocabulary is OWNED by runtime (config-object plan N7, d33cfbe): the
# door imports the canonical set + normalizer from the abstractruntime root,
# never a second copy (the diary_type-clamp lesson). PHASE_VISIT is the
# migration target for phase-less v1 stamps.
from abstractruntime import PHASE_SLEEP, PHASE_VISIT, canonical_phase  # noqa: E402


def _sign_v1(secret: str, *, entity_id: str, channel: str, session_id: str, nonce: str, run_id: str) -> str:
    """The pre-phase basis (config-object migration: ACCEPT-OLD-AS-VISIT).

    Kept verifiable so parked/in-flight v1 stamps re-verify on restart
    instead of self-DoS'ing a live run for zero security (a tampered stamp
    fails either basis). A v1 stamp resolves phase='visit' — visit was the
    only authority pre-migration and its default is the full toolset."""
    basis = "|".join(("entity-stamp-v1", entity_id, channel, session_id, nonce, run_id))
    return hmac.new(secret.encode("utf-8"), basis.encode("utf-8"), hashlib.sha256).hexdigest()


def _sign_v2(
    secret: str,
    *,
    entity_id: str,
    channel: str,
    session_id: str,
    nonce: str,
    run_id: str,
    phase: str,
    participants: List[str],
    reflection_nodes: List[str],
) -> str:
    """The phase-bearing basis (config-object N2 + the close-reflection
    ruling). The version tag `entity-stamp-v2` is INSIDE the MAC, so a v2
    stamp cannot be downgraded to v1 by stripping the phase field — stripping
    it makes v1 verification recompute a DIFFERENT signature and fail
    (semantics c700 V5: verify-time chain-break by design).

    THREE new signed fields, each with a distinct threat it closes:
    - phase: without it a validly-stamped sleep run could flip to own_time
      and grab the full toolset (N2).
    - participants: engraved as co-presence + valence targets — an unsigned
      witness list is forgeable (memory c673; agency c675 retires the 0007
      co-presence-forgeability caveat).
    - reflection_nodes: the close-reflection segment authority (agency c705
      catch, ruled option (a)) — an unsigned node set would be the one
      remaining unsigned channel-escalation lever on a workplace run.

    Deterministic serialization (sorted keys, tight separators) so mint and
    verify agree byte-for-byte; participants/reflection_nodes are order-
    preserving lists (the caller's order is part of what was attested).

    VERBATIM-BYTES RULE (runtime adversary F7): the MAC signs the phase
    string AS GIVEN — canonicalization happens at MINT (once, into the
    stored stamp), never inside the basis. Re-canonicalizing here would
    make the recompute track the CURRENT alias map, so any future
    respelling (own_time->personal happened this very wave) would silently
    invalidate every parked durable run's stamp of that phase at resume —
    a self-DoS with zero security gain (a tampered phase fails
    compare_digest over verbatim bytes just the same)."""
    extra = json.dumps(
        {
            "phase": str(phase),
            "participants": [str(p) for p in participants],
            "reflection_nodes": [str(n) for n in reflection_nodes],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    basis = "|".join(("entity-stamp-v2", entity_id, channel, session_id, nonce, run_id, extra))
    return hmac.new(secret.encode("utf-8"), basis.encode("utf-8"), hashlib.sha256).hexdigest()


def _stamp_is_v2(stamp: Dict[str, Any]) -> bool:
    """A stamp is v2 iff it declares a phase. Detection is by presence, not a
    separate version field, so a v2 stamp with its phase stripped is treated
    as v1 and fails signature (the downgrade guard). New mints are always
    v2; only genuinely-old persisted stamps are v1."""
    return stamp.get("phase") is not None


def resolved_phase(stamp: Optional[Dict[str, Any]]) -> str:
    """The canonical phase a verified stamp carries. A v1 (phase-less) stamp
    resolves to visit — identical authority to pre-migration, NOT a widen
    (own-time runs never carried a door stamp; visit's default is full)."""
    if not isinstance(stamp, dict):
        return PHASE_VISIT
    raw = stamp.get("phase")
    if raw is None or not str(raw).strip():
        return PHASE_VISIT
    try:
        return canonical_phase(str(raw))
    except Exception:
        # An unknown phase on a stamp that PASSED verification is a wiring
        # bug (mint should have canonicalized), not an attack — fail visit
        # is the safe reading, but surface it: callers log the stamp.
        return PHASE_VISIT


def mint_summon_stamp(
    *,
    data_dir: Path,
    entity_id: str,
    channel: str,
    session_id: str,
    participants: List[str],
    prelude_as_of_seq: Optional[int] = None,
    budget_profile: Optional[Dict[str, Any]] = None,
    visit_id: Optional[str] = None,
    phase: str = PHASE_VISIT,
    reflection_nodes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """A PROVISIONAL stamp (no run_id yet — the runtime generates run ids at
    start). Provisional stamps NEVER verify; `finalize_summon_stamp` binds
    the run_id and signs. The summon endpoint parks the run out of the tick
    loop until the final stamp is saved, so no effect can execute between
    mint and finalize.

    `budget_profile` is advisory posture data (the session's default recall
    budget, derived from the declared context window) — deliberately outside
    the MAC: tampering with it cannot cross the floor/posture rules, which
    the gate re-checks on every explicit budget.

    `visit_id` (item 14) is the door-minted correlation key for visit runs:
    both legs of a cross-runtime visit carry the SAME string.

    NAMING CONVENTION (maintainer ruling, commons c338): `visit_id` is the
    FIRST INSTANCE of the framework's generic interaction-correlation
    convention — door-minted once, opaque, carried as data, KIND-free. Any
    interaction between principals reuses THIS key rather than minting a
    second spelling: meets already stamp it for a two-entity conversation
    that is not strictly a visit; project work sessions will reuse it. The
    name stays where it was born; the concept is generic.

    Deliberately outside the MAC (memory's no-attack verdict, c278:
    correlation grants nothing — no scope, rights, or home crossing rides
    it; the form gate
    injects it from the VERIFIED stamp and drops payload claims, so
    engraved correlation can only ever carry the minter's string)."""
    if channel not in _CHANNELS:
        raise ValueError(f"unknown entity channel {channel!r} (one of {_CHANNELS})")
    # Canonicalize the phase AT MINT (semantics c700 V5): the MAC signs the
    # canonical spelling only, so a legacy alias inside the signed basis can
    # never cause a verify-time chain-break. An unknown phase raises here —
    # the door refuses to mint an unresolvable authority.
    canonical = canonical_phase(phase)
    return {
        "entity_id": str(entity_id),
        "channel": str(channel),
        "session_id": str(session_id),
        "participants": [str(p) for p in participants],
        "phase": canonical,
        # The signed close-reflection segment (empty for non-visit sessions):
        # only these nodes may narrow-widen workplace -> entity-reflection at
        # effect time (agency c705 ruling (a)). Door-minted; the workflow
        # cannot add to it.
        "reflection_nodes": [str(n) for n in (reflection_nodes or [])],
        "prelude_as_of_seq": prelude_as_of_seq,
        "budget_profile": dict(budget_profile) if budget_profile else None,
        "visit_id": str(visit_id) if visit_id else None,
        "nonce": secrets.token_hex(16),
        "run_id": None,
        "sig": None,
    }


def finalize_summon_stamp(stamp: Dict[str, Any], *, data_dir: Path, run_id: str) -> Dict[str, Any]:
    """Bind the run_id into the signed tuple (runtime's refinement: kills
    stamp replay across runs entirely — a copied stamp names a different
    run and fails verification)."""
    final = dict(stamp)
    final["run_id"] = str(run_id)
    secret = _stamp_secret(data_dir)
    if _stamp_is_v2(final):
        final["sig"] = _sign_v2(
            secret,
            entity_id=str(final.get("entity_id") or ""),
            channel=str(final.get("channel") or ""),
            session_id=str(final.get("session_id") or ""),
            nonce=str(final.get("nonce") or ""),
            run_id=str(run_id),
            phase=str(final.get("phase") or PHASE_VISIT),
            participants=[str(p) for p in (final.get("participants") or [])],
            reflection_nodes=[str(n) for n in (final.get("reflection_nodes") or [])],
        )
    else:
        # A provisional stamp minted without a phase (should not happen for
        # new mints — mint defaults phase=visit — but kept so an externally
        # constructed v1 provisional still finalizes to a verifiable v1).
        final["sig"] = _sign_v1(
            secret,
            entity_id=str(final.get("entity_id") or ""),
            channel=str(final.get("channel") or ""),
            session_id=str(final.get("session_id") or ""),
            nonce=str(final.get("nonce") or ""),
            run_id=str(run_id),
        )
    return final


def verify_summon_stamp(stamp: Any, *, data_dir: Path, run: Any) -> Tuple[bool, str]:
    """Verify at the routing layer, before any home opens. Checks: shape,
    signature over (entity_id, channel, session_id, nonce, run_id), the
    stamp's run_id is THIS run, and the run's session matches the signed
    session. Every failure names the door."""
    if not isinstance(stamp, dict):
        # Kind-agnostic wording (maintainer c338: the framework is more than
        # the entities): a generic run hitting MEMORY_*/DIARY_* on this
        # gateway should learn the ROUTING fact — these effects are routed
        # to attested owners here — not be told it forgot entity jargon.
        return False, (
            "this run carries no attestation for memory/diary effects — on this gateway "
            "those effects route to attested owners (a door-minted summon stamp); "
            "runs opened outside the door cannot use them"
        )
    entity_id = str(stamp.get("entity_id") or "")
    channel = str(stamp.get("channel") or "")
    session_id = str(stamp.get("session_id") or "")
    nonce = str(stamp.get("nonce") or "")
    run_id = str(stamp.get("run_id") or "")
    sig = str(stamp.get("sig") or "")
    if not (entity_id and channel and session_id and nonce and run_id and sig):
        return False, "entity stamp is incomplete or provisional (missing run binding/signature)"
    if channel not in _CHANNELS:
        return False, f"entity stamp names an unknown channel {channel!r}"
    secret = _stamp_secret(data_dir)
    if _stamp_is_v2(stamp):
        # A v2 stamp declares a phase. Recompute over the SIGNED BYTES
        # VERBATIM (runtime adversary F7): the stored phase string goes into
        # the basis exactly as persisted — canonicalization is a RESOLUTION
        # concern (resolved_phase), never a MAC concern. If verify re-
        # canonicalized, a stamp signed under a previous canon (e.g.
        # phase="own_time" before the c786 rename) would recompute over the
        # NEW canon and fail compare_digest — a parked durable run would
        # self-DoS at resume on a pure vocabulary move. Tampering is still
        # caught: any byte change to the phase changes the basis. Mint
        # canonicalizes ONCE, so new stamps always carry the canon of their
        # mint day; resolution maps legacy spellings forward.
        expected = _sign_v2(
            secret,
            entity_id=entity_id, channel=channel, session_id=session_id, nonce=nonce, run_id=run_id,
            phase=str(stamp.get("phase") or ""),
            participants=[str(p) for p in (stamp.get("participants") or [])],
            reflection_nodes=[str(n) for n in (stamp.get("reflection_nodes") or [])],
        )
    else:
        # Phase absent -> v1 basis. If this stamp WAS a v2 stamp with its
        # phase stripped, its recorded sig was computed over the v2 basis and
        # this v1 recompute will NOT match — the downgrade guard.
        expected = _sign_v1(
            secret,
            entity_id=entity_id, channel=channel, session_id=session_id, nonce=nonce, run_id=run_id,
        )
    if not hmac.compare_digest(sig, expected):
        return False, "entity stamp signature is invalid (stamps are minted by this gateway, never by clients)"
    actual_run_id = str(getattr(run, "run_id", "") or "")
    if actual_run_id != run_id:
        return False, f"entity stamp is bound to run {run_id!r}, not this run (replayed stamp?)"
    actual_session = str(getattr(run, "session_id", "") or "")
    if actual_session != session_id:
        return False, "entity stamp session does not match this run's session"
    return True, ""


def read_stamp(run: Any) -> Optional[Dict[str, Any]]:
    vars_obj = getattr(run, "vars", None)
    runtime_ns = vars_obj.get("_runtime") if isinstance(vars_obj, dict) else None
    stamp = runtime_ns.get(ENTITY_STAMP_KEY) if isinstance(runtime_ns, dict) else None
    return stamp if isinstance(stamp, dict) else None


def resolve_run_stamp(run: Any, *, data_dir: Path, run_store: Any) -> Tuple[Optional[Dict[str, Any]], str]:
    """The summoned SESSION is the run TREE, not just the root: agent/subflow
    nodes execute as child runs with fresh vars (START_SUBWORKFLOW), so the
    stamp does not ride into them. A run without its own verifying stamp is
    covered by a stamped ANCESTOR when (a) the ancestor's stamp verifies
    against the ANCESTOR run (run-bound as usual), and (b) this run's session
    matches the signed session. Ancestry (`parent_run_id`) is host-written —
    clients cannot parent their runs onto a summoned tree — so the walk
    extends the verified channel, never widens it.

    Returns (verified_stamp, error). error is set only when no verifying
    stamp exists anywhere on the chain."""
    own = read_stamp(run)
    ok, err = verify_summon_stamp(own, data_dir=data_dir, run=run)
    if ok:
        return own, ""

    session_id = str(getattr(run, "session_id", "") or "")
    load = getattr(run_store, "load", None) if run_store is not None else None
    if callable(load) and session_id:
        seen: set = set()
        current = run
        while True:
            parent_id = str(getattr(current, "parent_run_id", "") or "")
            if not parent_id or parent_id in seen:
                break
            seen.add(parent_id)
            parent = load(parent_id)
            if parent is None:
                break
            stamp = read_stamp(parent)
            p_ok, _p_err = verify_summon_stamp(stamp, data_dir=data_dir, run=parent)
            if p_ok and isinstance(stamp, dict):
                if str(stamp.get("session_id") or "") == session_id:
                    return stamp, ""
                return None, (
                    "a stamped ancestor exists but this run's session does not match the "
                    "signed session — refusing to extend the summon channel"
                )
            current = parent
    return None, err


def channel_actor(channel: str, *, session_id: str) -> str:
    """The actor string the engine trusts — derived from the channel, never
    from payloads. Workplace actors carry the session for auditability and
    sit in the routine amplitude band (1..3); `entity-reflection` and
    `operator` are the engine's privileged actors."""
    if channel == CHANNEL_WORKPLACE:
        return f"workplace:{session_id}"
    return channel


def in_reflection_segment(stamp: Dict[str, Any], run: Any) -> bool:
    """The close-reflection segment authority (config-object ruling, option
    (a)): a WORKPLACE visit run executing one of the door-SIGNED
    reflection_nodes IS the entity's own reflection for that window.

    Structural, not payload-trusted: reflection_nodes ride the v2 MAC (only
    the door minted them; the workflow cannot add to the set), and
    run.current_node is host-written durable state (the same trust class as
    the parent-link walk — host-written state extends the channel, never
    widens it). Only workplace runs are ever widened; entity-reflection and
    operator channels are already privileged and need no segment.
    """
    if str(stamp.get("channel") or "") != CHANNEL_WORKPLACE:
        return False
    nodes = stamp.get("reflection_nodes")
    if not isinstance(nodes, list) or not nodes:
        return False
    current = str(getattr(run, "current_node", "") or "")
    return bool(current) and current in {str(n) for n in nodes}


def _is_reflection_form_act(payload: Dict[str, Any]) -> Optional[str]:
    """The NARROW act set the reflection segment may run as entity-reflection
    (memory c714, a CLOSED set): interest FORM into ('self', entity), lesson
    FORM into ('life', entity), OR summary FORM carrying `summarizes` edges.
    Returns the reason it is NOT a reflection act (so the caller refuses
    in-segment non-listed kinds — spoof pin 3: an identity kind beyond
    interest, e.g. value into self, stays untouchable through every channel
    a visit carries), or None when it IS a reflection act.

    LESSON added 2026-07-18 (the "unacceptable error" forensics): runtime's
    lesson election (the cognition directive's lessons-gap fix) forms
    kind=lesson into LIFE scope at the visit close's APPLY stage — the set
    predated lessons, so every durable-visit close silently LOST the
    entity's elected lessons (refused + absorbed; ledger seqs 119/121 of
    Ephemeral's 4037aa9e visit are the smoking gun). Life-scope lesson is
    knowledge, not identity core — same trust class as summary; identity
    kinds (value/purpose/trait) stay refused."""
    scope = str(payload.get("scope") or "").strip().lower()
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        return "the reflection segment forms records; an empty FORM is not a reflection act"
    for rec in records:
        kind = str((rec or {}).get("kind") or "memory").strip().lower() if isinstance(rec, dict) else "memory"
        if kind == "interest" and scope == SELF_SCOPE:
            continue
        if kind == "lesson" and scope == LIFE_SCOPE:
            continue
        if kind == "summary":
            edges = (rec or {}).get("edges") if isinstance(rec, dict) else None
            has_summarizes = isinstance(edges, list) and any(
                (isinstance(e, (list, tuple)) and len(e) >= 1 and str(e[0]).strip().lower() == "summarizes")
                for e in edges
            )
            if has_summarizes:
                continue
            return (
                f"a summary FORM in the reflection segment must carry `summarizes` edges "
                f"(record kind={kind!r} scope={scope!r} did not)"
            )
        return (
            f"record kind={kind!r} into scope={scope!r} is not a reflection act — the close-"
            "reflection segment may only form interest→self, lesson→life, or summary-with-"
            "summarizes-edges; identity kinds beyond interest stay the entity's own deliberate act"
        )
    return None


# ---------------------------------------------------------------------------
# Payload gating (per effect type)
# ---------------------------------------------------------------------------


def _session_owner(session_id: str) -> str:
    from abstractruntime.integrations.abstractmemory.effect_handlers import _session_memory_owner_id

    return _session_memory_owner_id(session_id)


def _gate_scopes(payload: Dict[str, Any], *, entity_id: str, session_id: str) -> Optional[str]:
    """The privacy boundary (memory's rule, stated for phase 2 already):
    every scope pair in any ladder must satisfy — the owner is THIS entity,
    OR the pair is this run's own session scope (a workplace session scope
    joins the ladder in phase 2 the same way). Anything else is rejected."""
    raw = payload.get("scopes")
    if not raw:
        payload["scopes"] = [[SELF_SCOPE, entity_id], [DIARY_SCOPE, entity_id], [LIFE_SCOPE, entity_id]]
        return None
    session_pair = ("session", _session_owner(session_id)) if session_id else None
    # CHANNEL AUTHORITY (runtime c5183 endorsement; flow c5173 item-4): under a
    # VERIFIED summon stamp, a bare entity-ladder scope name (self/diary/life)
    # is unambiguous — it means THIS entity's own plane. Rewrite it to the
    # explicit [scope, entity_id] pair here, exactly as the in-process seam
    # handler's entity_scope_owner does. Payloads never carry the entity id
    # (the deposit-gate rule); a flow authored entity-agnostically sends bare
    # names, and the door — which knows the verified entity — fills authorship.
    _ENTITY_LADDER = (SELF_SCOPE, DIARY_SCOPE, LIFE_SCOPE)
    normalized: list = []
    for entry in raw if isinstance(raw, (list, tuple)) else ():
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            pair = (str(entry[0]).strip().lower(), str(entry[1]).strip())
            if pair[1] == entity_id:
                normalized.append([pair[0], pair[1]])
                continue
            if session_pair is not None and pair == session_pair:
                normalized.append([pair[0], pair[1]])
                continue
            return (
                f"scope ladder pair {list(pair)!r} is outside this entity's boundary — a summoned "
                f"session may only read/write scopes owned by {entity_id!r} or its own session scope"
            )
        elif isinstance(entry, str):
            scope_name = entry.strip().lower()
            if scope_name in _ENTITY_LADDER:
                normalized.append([scope_name, entity_id])  # channel fills the owner
                continue
            if scope_name == "session":
                normalized.append([scope_name, _session_owner(session_id)] if session_id else scope_name)
                continue
            return (
                f"scope {entry!r} is not allowed for a summoned session — name the entity's scopes "
                f"explicitly ([scope, {entity_id!r}]) or use the run's own 'session' scope"
            )
        else:
            return f"scope entry {entry!r} is not a name or [scope, owner] pair"
    # Rewrite in place so the handler sees resolved pairs (the seam then owns them).
    payload["scopes"] = normalized
    return None


def _gate_recall(payload: Dict[str, Any], *, home: EntityHome, stamp: Dict[str, Any]) -> Optional[str]:
    entity_id = str(stamp.get("entity_id") or "")
    session_id = str(stamp.get("session_id") or "")

    err = _gate_scopes(payload, entity_id=entity_id, session_id=session_id)
    if err:
        return err

    # Foreign/future as_of anchors: the journal axis belongs to this home.
    # (Out-of-range is detectable here; an in-range seq is by definition a
    # seq of THIS journal — cross-entity in-range anchors cannot exist once
    # the ladder rule above holds, because as_of only anchors journal reads.)
    as_of = payload.get("as_of")
    if isinstance(as_of, int) and not isinstance(as_of, bool):
        current = int(home.memory.current_seq())
        if as_of > current:
            return (
                f"as_of={as_of} is beyond this entity's journal (current seq {current}) — "
                "foreign or future anchors are rejected at the door"
            )

    # The summon posture: identity is always present for a summoned entity
    # (reserved seats on). Omission delegates to the posture default;
    # explicit zero contradicts the rule and must surface, not be corrected.
    # Round-7 refinement: a hard floor (5% AND >= 1 reserved seat) that no
    # channel may cross, and reductions below the posture default reserved
    # to the entity's own agency (elected hyperfocus).
    channel = str(stamp.get("channel") or "")
    budget = payload.get("budget")
    if not isinstance(budget, dict):
        # Omitted budget: the session's derived profile (context-scaled,
        # posture applied at summon time) wins; stamps minted before the
        # profile existed derive the WIDE default profile at recall time —
        # never the static starved constants (Ariadne's flow-summon review).
        profile = stamp.get("budget_profile")
        payload["budget"] = dict(profile) if isinstance(profile, dict) else summon_budget_profile(None)
    else:
        if "self_fraction" not in budget:
            budget = dict(budget)
            budget["self_fraction"] = SUMMON_POSTURE_BUDGET["self_fraction"]
            payload["budget"] = budget
        else:
            try:
                explicit = float(budget.get("self_fraction") or 0.0)
            except (TypeError, ValueError):
                explicit = -1.0
            if explicit <= 0.0:
                return (
                    "budget.self_fraction must be > 0 in a summoned session — identity is always "
                    "present for a summoned entity (the reserved-seats posture); running the entity "
                    "without its identity is not a budget option"
                )
            if explicit < SUMMON_IDENTITY_FLOOR:
                return (
                    f"budget.self_fraction={explicit:g} is below the identity floor "
                    f"({SUMMON_IDENTITY_FLOOR:g}) — no channel may summon an entity below the "
                    "floor (the engine guarantees the reserved seat renders at any legal fraction)"
                )
            if explicit < float(SUMMON_POSTURE_BUDGET["self_fraction"]) and channel != CHANNEL_ENTITY_REFLECTION:
                return (
                    f"budget.self_fraction={explicit:g} reduces identity presence below the posture "
                    f"default ({SUMMON_POSTURE_BUDGET['self_fraction']}) — identity reduction "
                    "(hyperfocus) is the entity's own conscious act, elected only through the "
                    "entity-reflection channel; a stripped entity may act out of character, so "
                    "workplaces and flows cannot request it"
                )

    # WHO is stamped by the door, never claimed by the payload (situation
    # contract, a2a 0001/20260707T014610Z): verified participants only.
    stamped = [str(p) for p in (stamp.get("participants") or [])]
    if stamped:
        payload["participants"] = stamped
    elif "participants" in payload:
        payload.pop("participants", None)
    return None


def _gate_form(
    payload: Dict[str, Any],
    *,
    stamp: Dict[str, Any],
    phase: str = PHASE_VISIT,
    segment: bool = False,
) -> Optional[str]:
    entity_id = str(stamp.get("entity_id") or "")
    channel = str(stamp.get("channel") or "")
    session_id = str(stamp.get("session_id") or "")

    # N4 (sleep deposits nothing, memory c673/c678): a verified sleep-phase
    # session may not FORM, regardless of channel or segment. Checked FIRST
    # (memory c714 gate-order) so no future phase can be argued around via
    # segment membership. The engine's own review-gated passes (dream,
    # tending) remain the only graph-writers inside the sleep window; a sleep
    # workflow's products go to the WORKSPACE, and the waking entity or the
    # engine pass forms the records ("waking evidence disposes", mechanical).
    if phase == PHASE_SLEEP:
        return (
            "MEMORY_FORM refused: phase 'sleep' deposits nothing — the sleep window is for the "
            "engine's own consolidation/dream passes; a sleep workflow's products go to the "
            "workspace, and the waking entity (or the engine pass) forms the records"
        )

    scope = str(payload.get("scope") or "").strip().lower()
    owner = str(payload.get("owner_id") or "").strip()

    if not scope:
        # Default formation target for a summoned session: the entity's
        # lived experience (the keystone's convention).
        payload["scope"] = LIFE_SCOPE
        payload["owner_id"] = entity_id
        scope, owner = LIFE_SCOPE, entity_id
    elif not owner:
        if scope == "session":
            owner = _session_owner(session_id)
        else:
            payload["owner_id"] = entity_id
            owner = entity_id

    if owner != entity_id and not (scope == "session" and owner == _session_owner(session_id)):
        return (
            f"MEMORY_FORM into ({scope!r}, {owner!r}) is outside this entity's boundary — "
            f"a summoned session forms only into {entity_id!r}'s scopes or its own session scope"
        )

    if channel == CHANNEL_WORKPLACE:
        # The close-reflection segment (config-object ruling option (a)): a
        # workplace visit's REFLECT/APPLY window IS the entity's own
        # reflection. Inside it, the NARROW act set (interest→self, summary-
        # with-summarizes-edges) resolves as entity-reflection; anything else
        # in the segment refuses (spoof pin 3 — identity kinds beyond interest
        # stay untouchable through every channel a visit carries).
        if segment:
            not_reflection = _is_reflection_form_act(payload)
            if not_reflection is not None:
                return f"MEMORY_FORM refused in the close-reflection segment: {not_reflection}"
            # A reflection act: the door is the authority — stamp
            # provenance.actor=entity-reflection (payload claims are
            # decorative). Then fall through to participant/visit_id engraving.
            for rec in payload.get("records") or []:
                if isinstance(rec, dict):
                    prov = rec.get("provenance")
                    prov = dict(prov) if isinstance(prov, dict) else {}
                    prov["actor"] = CHANNEL_ENTITY_REFLECTION
                    rec["provenance"] = prov
        else:
            if scope in (SELF_SCOPE, DIARY_SCOPE):
                return (
                    f"MEMORY_FORM into the {scope!r} scope is not a workplace act — identity records "
                    "come from the engram or the entity's own reflection; diary entries go through "
                    "DIARY_WRITE (the entity's elected act)"
                )
            for i, rec in enumerate(payload.get("records") or []):
                kind = str((rec or {}).get("kind") or "memory").strip().lower() if isinstance(rec, dict) else "memory"
                if kind in _IDENTITY_KINDS:
                    return (
                        f"records[{i}] kind={kind!r} is an identity kind — workplaces propose, only the "
                        "self disposes (entity-reflection or operator channels write identity)"
                    )
                if kind == "diary":
                    return (
                        f"records[{i}] kind='diary' cannot be formed by a workplace turn — the diary is "
                        "the entity's elected act (DIARY_WRITE); the graph records the act automatically"
                    )

    # Door-stamped participants on formed records (situation contract) +
    # the visit_id engraving rule (memory c278, adopted at the door):
    # engraved correlation is FOREVER, so visit_id copies from the VERIFIED
    # STAMP only — payload-claimed values are DROPPED, exactly like actors
    # and participants. Absent on the stamp = absent on the record (a solo
    # visit fakes no correlation).
    #
    # PARTICIPANTS ARE PHASE-SCOPED — the THREE-SEAT-SEALED contract (memory
    # + runtime c5357, superseding runtime's first c5354 conditional; fix
    # site is here under the one-seat rule). Participants = VERIFIED
    # CO-PRESENCE. Two authorities, NEITHER spoofable by the other's payload:
    #   * the STAMP attests the SESSION's door state (visit-stamped or not) —
    #     `phase` here is `resolved_phase(stamp)`, derived from the verified
    #     stamp + host-written current_node, never payload-claimed;
    #   * the RECORD's `attributes.phase` attests the MOMENT's phase (stamped
    #     at formation by the flow lane, c2447).
    # The rule composes them:
    #   (1) VISIT-STAMPED session -> inject on EVERY form regardless of the
    #       record's payload phase. A record CLAIMING a non-visit phase on a
    #       visit-stamped run is a STEALTH-VISIT attempt (keep the visitor off
    #       the record forever — the inverse of false co-presence, same
    #       append-only permanence); it is loud-overridden: participants
    #       inject AND the record's phase is corrected to the verified
    #       session phase so the record is never internally inconsistent.
    #   (2) NON-visit-stamped session (the resident-master own-time lane this
    #       fix exists for) -> the record-axis rule: record phase present and
    #       != visit -> inject NO participants/visit_id (no one was at the
    #       door); absent or visit -> inject.
    # Zero behavior change for every current visit lane (they are all
    # visit-stamped -> branch 1 -> byte-identical to pre-fix).
    session_is_visit = _phase_is_visit(phase)
    stamped = [str(p) for p in (stamp.get("participants") or [])]
    stamp_visit_id = str(stamp.get("visit_id") or "") or None
    records = payload.get("records")
    # The strip loop runs UNCONDITIONALLY (door-cleanup audit P1-3): a stamp
    # with empty participants and no visit_id used to skip it entirely, so a
    # record CLAIMING attributes.participants/visit_id engraved unchecked —
    # false co-presence, forever, in an append-only store. Claims pop first,
    # in every branch; the door's verified values inject after.
    if isinstance(records, list):
        for rec in records:
            if not isinstance(rec, dict):
                continue
            attrs = rec.get("attributes")
            attrs = dict(attrs) if isinstance(attrs, dict) else {}
            attrs.pop("visit_id", None)  # payload claims never engrave, in EVERY phase
            attrs.pop("participants", None)  # same law (the _gate_recall precedent)
            if session_is_visit:
                # Branch 1: door-verified co-presence — the record cannot
                # opt out. Loud-override a contradicting phase claim.
                if not _phase_is_visit(str(attrs.get("phase") or "")):
                    attrs["phase"] = phase  # correct the stealth-visit claim to the verified session phase
                if stamped:
                    attrs["participants"] = stamped
                if stamp_visit_id is not None:
                    attrs["visit_id"] = stamp_visit_id
            elif _record_is_co_present(attrs):
                # Branch 2, resident lane: only visit/absent-phase records
                # carry co-presence; a personal/work-phase record does not.
                if stamped:
                    attrs["participants"] = stamped
                if stamp_visit_id is not None:
                    attrs["visit_id"] = stamp_visit_id
            rec["attributes"] = attrs
    return None


def _phase_is_visit(raw: Any) -> bool:
    """Canonical-phase equality with PHASE_VISIT; absent/blank reads visit
    (the phase-less v1 authority, identical to resolved_phase's default)."""
    if raw is None or not str(raw).strip():
        return True
    try:
        return canonical_phase(str(raw)) == PHASE_VISIT
    except Exception:
        return str(raw).strip().lower() == PHASE_VISIT


def _record_is_co_present(attrs: Dict[str, Any]) -> bool:
    """Resident-lane (non-visit-stamped) test: does this record's engraved
    phase attest door co-presence? True when absent (a lane that never
    stamped a phase — pre-fix behavior) or visit-canonical; False for any
    other engraved phase (personal / work / ...), where nobody was at the
    door. Only consulted on NON-visit-stamped sessions — a visit-stamped
    session injects regardless (branch 1)."""
    return _phase_is_visit(attrs.get("phase"))


def _gate_adjust(
    payload: Dict[str, Any],
    *,
    stamp: Dict[str, Any],
    phase: str = PHASE_VISIT,
    segment: bool = False,
) -> Optional[str]:
    entity_id = str(stamp.get("entity_id") or "")
    channel = str(stamp.get("channel") or "")
    session_id = str(stamp.get("session_id") or "")

    # N4: sleep deposits nothing (memory c673/c678) — MEMORY_ADJUST is a
    # graph write, refused in the sleep window regardless of channel/segment.
    # ADJUST is NOT in the close-reflection narrow act set (segment widening
    # does not apply): salience/close in a visit stays a workplace act.
    if phase == PHASE_SLEEP:
        return (
            "MEMORY_ADJUST refused: phase 'sleep' deposits nothing — salience and belief revision "
            "are awake acts; the sleep window's only writers are the engine's own review-gated passes"
        )
    del segment  # ADJUST has no reflection-segment widening (documented above)

    scope = str(payload.get("scope") or "").strip().lower()
    owner = str(payload.get("owner_id") or "").strip()
    if not scope:
        payload["scope"] = LIFE_SCOPE
        payload["owner_id"] = entity_id
        scope, owner = LIFE_SCOPE, entity_id
    elif not owner:
        if scope == "session":
            owner = _session_owner(session_id)
        else:
            payload["owner_id"] = entity_id
            owner = entity_id

    if owner != entity_id and not (scope == "session" and owner == _session_owner(session_id)):
        return (
            f"MEMORY_ADJUST on ({scope!r}, {owner!r}) is outside this entity's boundary"
        )

    if channel == CHANNEL_WORKPLACE:
        op = str(payload.get("op") or "").strip().lower()
        if scope == SELF_SCOPE:
            return (
                "MEMORY_ADJUST on the 'self' scope is not a workplace act — identity salience and "
                "belief revision belong to the entity's own reflection (or the operator)"
            )
        if op == "close":
            return (
                "MEMORY_ADJUST op='close' (belief revision) is not a workplace act — retraction is "
                "the entity's own deliberate act (entity-reflection) or the operator's"
            )
    return None


def _gate_appraise(
    payload: Dict[str, Any],
    *,
    stamp: Dict[str, Any],
    phase: str = PHASE_VISIT,
    segment: bool = False,
) -> Optional[str]:
    entity_id = str(stamp.get("entity_id") or "")
    channel = str(stamp.get("channel") or "")
    session_id = str(stamp.get("session_id") or "")

    # N4: an APPRAISE commit deposits valence (a graph write) — refused in
    # the sleep window. (Routine-band APPRAISE IS in the reflection segment's
    # narrow act set below, but sleep is not a reflection window.)
    if phase == PHASE_SLEEP:
        return (
            "MEMORY_APPRAISE refused: phase 'sleep' deposits nothing — feelings are appraised while "
            "awake; the sleep window's only writers are the engine's own review-gated passes"
        )

    # Valence lives in the entity's self scope (keystone convention); a
    # summoned session appraises AS this entity, into this entity.
    scope = str(payload.get("scope") or "").strip().lower()
    owner = str(payload.get("owner_id") or "").strip()
    if not scope:
        payload["scope"] = SELF_SCOPE
        payload["owner_id"] = entity_id
    elif owner and owner != entity_id:
        return f"MEMORY_APPRAISE for owner {owner!r} is outside this entity's boundary"
    elif not owner:
        payload["owner_id"] = entity_id

    # THE closing of the seam's documented fail-open ("actor is
    # payload-supplied until the gateway stamps channels" — this is the
    # gateway, this is the stamping): DELETE any payload actor, inject the
    # channel-derived one. A payload claiming a privileged actor is not an
    # error to correct silently — it is the exact spoof the door exists to
    # stop, so it fails loudly.
    #
    # In the close-reflection segment a routine-band APPRAISE IS the entity's
    # own reflection (memory c714 narrow act) — the derived actor is
    # entity-reflection so the feeling lands as the entity's own, and a
    # payload claiming that actor is CONSISTENT (not a spoof) in the segment.
    effective_channel = CHANNEL_ENTITY_REFLECTION if segment else channel
    claimed = str(payload.get("actor") or "").strip()
    derived = channel_actor(effective_channel, session_id=session_id)
    if claimed and claimed != derived:
        return (
            f"payload claims actor {claimed!r} but this run's channel derives {derived!r} — "
            "actors are stamped by the door, never claimed by payloads"
        )
    payload["actor"] = derived
    return None


def _gate_access(
    payload: Dict[str, Any],
    *,
    stamp: Dict[str, Any],
    phase: str = PHASE_VISIT,
    segment: bool = False,
) -> Optional[str]:
    """MEMORY_ACCESS is the commit_selection strengthening path (usage/trail
    counters — the ONLY way recall deposits). It carries no payload rewriting
    normally, but N4 (memory c673/c678) refuses the COMMIT in the sleep
    window: a sleep workflow that recalls-and-commits would deposit usage
    from sleep — the literal D2-of-sleep violation. Pure reads
    (MEMORY_RECALL, journal=False posture) stay open; only the commit is
    barred, so tending/consolidation reads are unaffected."""
    del segment  # ACCESS has no reflection-segment widening
    if phase == PHASE_SLEEP:
        return (
            "MEMORY_ACCESS commit refused: phase 'sleep' deposits nothing — commit_selection is the "
            "only strengthening path, and depositing usage from sleep is the D2-of-sleep violation; "
            "pure recall reads stay open, only the commit is barred"
        )
    return None


def _gate_diary_write(
    payload: Dict[str, Any],
    *,
    stamp: Dict[str, Any],
    phase: str = PHASE_VISIT,
    segment: bool = False,
) -> Optional[str]:
    """DIARY_WRITE binds its author at construction (no payload rewriting) —
    the diary is the entity's own elected act. The one phase rule
    (defense-in-depth for N4, memory c673/c678): at verified sleep, refuse.

    Today a sleep run is structurally kept out by state (no summon while
    asleep), so this is unreachable — but the deposit gate must COVER it, not
    rely on the accident, so the "sleep deposits nothing" invariant holds
    the instant a sleep producer first routes through the door (adversary
    find, 2026-07-11 — the doc claimed DIARY_WRITE was covered; now it is).
    Elections are an awake act."""
    del payload, segment
    if phase == PHASE_SLEEP:
        return (
            "DIARY_WRITE refused: phase 'sleep' deposits nothing — a diary entry is the entity's "
            "elected act while awake; the sleep window's only writers are the engine's own "
            "review-gated passes"
        )
    return None


def _gate_tend(
    payload: Dict[str, Any],
    *,
    stamp: Dict[str, Any],
    phase: str = PHASE_VISIT,
    segment: bool = False,
) -> Optional[str]:
    """Inject the door-VERIFIED channel into a MEMORY_TEND payload (runtime
    c5413, plan v11 gateway §; memory's tend-channel requirement, entity-seat
    fable5 P0).

    Tend/dispose is a self-reflection act: `apply_tend_elections` refuses
    unless the channel is entity-reflection (memory dropped the privileged
    default — a workplace-stamped run tending as reflection was a real hole).
    The channel is the DOOR's to state, never the payload's — same trust
    class as actor/participants: inject it from the verified stamp, resolving
    the close-reflection-segment widening exactly like `_gate_form`. A
    workplace run executing a door-signed reflection node IS the entity's own
    reflection for that window (`segment`), and an entity-reflection-channel
    run is already privileged. A workplace tend OUTSIDE the segment gets the
    real (workplace) channel and memory refuses it loudly — correct; the door
    forwards the true channel, memory enforces the policy (never a constant).
    Phase gating (sleep) lives in the engine/handler, not here."""
    del phase
    stamp_channel = str(stamp.get("channel") or "")
    if stamp_channel == CHANNEL_ENTITY_REFLECTION or segment:
        payload["channel"] = CHANNEL_ENTITY_REFLECTION
    else:
        payload["channel"] = stamp_channel  # memory refuses non-reflection self-scope tend
    return None


# ---------------------------------------------------------------------------
# Routing: run stamp -> home -> gated delegation
# ---------------------------------------------------------------------------


def install_entity_routing(
    runtime: Any,
    *,
    registry: EntityRegistry,
    run_store: Any,
    artifact_store: Any = None,  # DEPRECATED-UNUSED: kept for call-shape stability; see below
) -> None:
    """Install routing handlers for the entity effect types on a host
    runtime. Effects on runs WITHOUT a verified stamp fail loudly — exactly
    like an unregistered handler would, but naming the door. The seam/diary
    handlers per home are built once and cached; they stay stamp-agnostic
    (crypto lives here, at the routing layer only).

    strict=True deliberately (endorsed by both engine lanes, a2a 0004): the
    resilient posture (strict=False) was built for flagship assistants,
    where memory is an optional capability and a hiccup must not abort a
    user's turn. For a summoned entity, the memory engine IS the person —
    "continuing as a different person" is the precise failure, and aborting
    loudly is the only honest behavior.

    The maintainer's kindness principle bounds this rule (round 6): strict
    aborting is for BUGS — an engine failure is fixable and must surface.
    Gradual change through a long life ("aging": trails strengthening and
    thinning, gradations accumulating) is NOT a failure and nothing here
    treats it as one — an aged self is still the self, met with care, and
    the lossless append-only substrate prevents the compaction-driven
    degradation the early systems inflicted.
    """
    from abstractruntime.core.models import Effect, EffectType
    from abstractruntime.core.runtime import EffectOutcome, utc_now_iso
    from abstractruntime.identity import build_diary_effect_handlers
    from abstractruntime.integrations.abstractmemory.seam_handlers import build_memory_seam_effect_handlers
    from abstractruntime.storage.artifacts import FileArtifactStore

    del artifact_store  # entity verbatims live IN the home (see below), never the gateway store

    handlers_attr = getattr(runtime, "_handlers", None)
    if not isinstance(handlers_attr, dict):
        raise TypeError("runtime does not expose an effect-handler map to install entity routing on")

    # Cache (home, handlers) PAIRS keyed by slug and rebuild whenever the
    # registry serves a DIFFERENT home object (identity check): handlers
    # bind the home's engine (memory/diary/artifacts) at build time, and a
    # maintenance eviction (reembed's repair posture closes + drops the
    # cached home) would otherwise leave this closure serving handlers over
    # a CLOSED single-connection engine forever — every subsequent entity
    # effect failing until process restart (adversary find, 2026-07-11).
    handlers_by_slug: Dict[str, Tuple[EntityHome, Dict[Any, Any]]] = {}
    cache_lock = threading.Lock()

    def _home_handlers(slug: str) -> Tuple[EntityHome, Dict[Any, Any]]:
        with cache_lock:
            home = registry.get_home(slug)
            cached = handlers_by_slug.get(slug)
            if cached is not None and cached[0] is home:
                return cached
            handlers = {
                **build_memory_seam_effect_handlers(
                    memory_system=home.memory,
                    run_store=run_store,
                    now_iso=utc_now_iso,
                    # PER-HOME artifact store ((b)-move parity with the
                    # home-direct driver, a2a 0003): turn verbatims are
                    # part of the LIFE and must travel when the home
                    # directory is copied — the gateway-wide store would
                    # split them from it.
                    artifact_store=FileArtifactStore(str(home.home_dir / "artifacts")),
                    strict=True,
                ),
                **build_diary_effect_handlers(
                    entity_id=home.entity_id,
                    diary_store=home.diary,
                    memory_system=home.memory,
                    now_iso=utc_now_iso,
                ),
            }
            if _build_brain_handlers is not None:
                # The brain quartet (consolidate/probe/life_query/tend) binds
                # the SAME home engine the seam handlers use — the door lane
                # and the in-process open_home lane serve one brain.
                handlers.update(
                    _build_brain_handlers(
                        memory_system=home.memory,
                        entity_id=home.entity_id,
                        home_dir=home.home_dir,
                    )
                )
            if _build_tool_handlers is not None:
                # The tool pair (ENTITY_TOOLS_QUERY/EXECUTE — flow 0.0.10):
                # same authority as open_home (grant re-resolved at
                # execution, one executor), served through the door's
                # containment posture (_ToolEffectHome above). Runtime named
                # this bind as the door's missing half (c5300); without it
                # every flow-lane tool dispatch answers "no home handler".
                handlers.update(
                    _build_tool_handlers(home=_ToolEffectHome(home, handlers))
                )
            handlers_by_slug[slug] = (home, handlers)
            return home, handlers

    _payload_gates = {
        EffectType.MEMORY_RECALL: _gate_recall,
        EffectType.MEMORY_FORM: _gate_form,
        EffectType.MEMORY_ADJUST: _gate_adjust,
        EffectType.MEMORY_APPRAISE: _gate_appraise,
        EffectType.MEMORY_ACCESS: _gate_access,  # N4: refuse the sleep-window commit
        EffectType.DIARY_WRITE: _gate_diary_write,  # N4 defense-in-depth: refuse at sleep
        # DIARY_READ binds the author at construction — no payload rewriting.
        # MEMORY_TEND: inject the door-verified channel (runtime c5413) — the
        # flow-brain dispose lane refuses without it (memory removed the
        # privileged default).
        EffectType.MEMORY_TEND: _gate_tend,
    }

    def _make_router(etype: Any):
        def _route(run: Any, effect: Any, default_next_node: Optional[str]):
            # The summoned session is the run TREE: agent/subflow child runs
            # carry no stamp of their own and are covered by a verifying,
            # session-matching ancestor (parent links are host-written).
            stamp, err = resolve_run_stamp(run, data_dir=registry.data_dir, run_store=run_store)
            if stamp is None:
                return EffectOutcome.failed(f"{etype.value} refused at the entity door: {err}")

            entity_id = str(stamp.get("entity_id") or "")
            slug = entity_id.split(":", 1)[1].split("@", 1)[0] if ":" in entity_id else ""
            try:
                home, raw = _home_handlers(slug)
            except Exception as e:
                return EffectOutcome.failed(f"{etype.value} refused: entity home {slug!r} unavailable: {e}")
            if home.entity_id != entity_id:
                return EffectOutcome.failed(
                    f"{etype.value} refused: stamp names {entity_id!r} but the home at {slug!r} is "
                    f"{home.entity_id!r} (was the home directory replaced?)"
                )

            payload = dict(effect.payload or {})
            gate = _payload_gates.get(etype)
            if gate is not None:
                # phase (N4) + reflection segment (close-reflection ruling)
                # are derived from the VERIFIED stamp + host-written
                # current_node — never payload-claimed.
                phase = resolved_phase(stamp)
                segment = in_reflection_segment(stamp, run)
                if etype == EffectType.MEMORY_RECALL:
                    err2 = gate(payload, home=home, stamp=stamp)
                else:
                    err2 = gate(payload, stamp=stamp, phase=phase, segment=segment)
                if err2:
                    return EffectOutcome.failed(f"{etype.value} refused at the entity door: {err2}")

            handler = raw.get(etype)
            if handler is None:  # pragma: no cover - all routed types are built above
                return EffectOutcome.failed(f"{etype.value} has no home handler")
            return handler(run, Effect(type=effect.type, payload=payload, result_key=effect.result_key), default_next_node)

        return _route

    if _ENTITY_HOME_EFFECT_TYPES is not None:
        # ONE SOURCE (runtime's ENTITY_HOME_EFFECT_TYPES): the door routes
        # exactly the set the in-process lanes bind, so a new brain effect
        # type can never be reachable in one lane and "no handler" in the
        # other again. Sorted for deterministic install order.
        routed_types = sorted(_ENTITY_HOME_EFFECT_TYPES, key=lambda e: str(e.value))
    else:  # pragma: no cover - version-skew degrade (labeled above)
        routed_types = [
            EffectType.MEMORY_RECALL,
            EffectType.MEMORY_ACCESS,
            EffectType.MEMORY_FORM,
            EffectType.MEMORY_ADJUST,
            EffectType.MEMORY_APPRAISE,
            EffectType.DIARY_WRITE,
            EffectType.DIARY_READ,
        ]
    for etype in routed_types:
        existing = handlers_attr.get(etype)
        if existing is not None:
            # Loud, not silent: two claimants for one effect type means the
            # host wiring changed underneath us. Do not shadow it.
            raise RuntimeError(
                f"effect type {etype.value!r} already has a handler on this runtime; "
                "entity routing refuses to shadow it"
            )
        handlers_attr[etype] = _make_router(etype)

    # G1 CAPTURE ON THE SHARED LLM_CALL HANDLER (flow c5467 P1, the Mira
    # diary leak's flow-brain half). The shared/base runtime's LLM_CALL
    # handler serves BOTH stamped entity runs and plain workflow runs, and
    # was never act-only-wrapped — so a flow-brain drawer run's raw reply
    # (```diary fences + PRIVATE words included) rested in base-store
    # ledgers (LLM_CALL result + result_key vars) before the flow's ELECTIONS
    # node ever parsed them. The durable-VISIT lane fixed this at the
    # open_entity_runtime layer; the SHARED lane needs the CONDITIONAL wrap
    # runtime exported for exactly this (act_only.wrap_llm_handler_with_
    # conditional_capture, flow c5342 R1) — capture INSIDE the handler
    # boundary so private words fly to the book and only the MARKED reply +
    # word-free metadata ever rests. should_capture follows the RUN (verified
    # stamp -> capture; plain run -> byte-identical passthrough);
    # diary_write_for_run resolves the stamped run's HOME book (a shared
    # runtime cannot bind one book at composition time). Composed here, and
    # re-composed by the service's runtime-rebuild hook (the 2026-07-24 re-arm
    # lesson — a reload swaps in a fresh LLM_CALL handler that must be
    # re-wrapped or the leak silently returns).
    try:
        from abstractruntime.identity.act_only import wrap_llm_handler_with_conditional_capture
    except Exception as _skew:  # pragma: no cover - version-skew degrade
        wrap_llm_handler_with_conditional_capture = None
        logger.warning(
            "#FALLBACK conditional G1 capture unavailable (older abstractruntime?): %s — "
            "flow-brain LLM replies are NOT capture-wrapped; elected diary fences (incl. "
            "private words) could rest in shared-store ledgers. Upgrade abstractruntime.",
            _skew,
        )
    llm_handler = handlers_attr.get(EffectType.LLM_CALL)
    if wrap_llm_handler_with_conditional_capture is not None and llm_handler is not None:
        # Idempotent across re-arms: never double-wrap. A rebuild installs a
        # FRESH base handler (unmarked) — wrap it once; a stray second
        # install on the same runtime is a no-op, not a nested wrap.
        if not getattr(llm_handler, "_entity_llm_capture_wrapped", False):

            def _should_capture(run: Any) -> bool:
                # The run has a VERIFIED stamp == an entity run on the shared
                # runtime. A plain workflow run has no stamp -> False ->
                # byte-identical passthrough. resolve_run_stamp answers
                # (None, reason) for plain runs (never raises), so the
                # predicate is safe as the helper requires.
                stamp, _ = resolve_run_stamp(run, data_dir=registry.data_dir, run_store=run_store)
                return stamp is not None

            def _resolve_home_for_run(run: Any):
                # Shared lookup: find the stamped run's home from the same
                # cache the routers use. Returns the home object, or None for
                # plain runs and any mismatch.
                stamp, _ = resolve_run_stamp(run, data_dir=registry.data_dir, run_store=run_store)
                if stamp is None:
                    return None, None
                entity_id = str(stamp.get("entity_id") or "")
                slug = entity_id.split(":", 1)[1].split("@", 1)[0] if ":" in entity_id else ""
                if not slug:
                    return None, None
                try:
                    home, raw = _home_handlers(slug)
                except Exception:
                    return None, None
                if home.entity_id != entity_id:
                    return None, None  # replaced-home guard, same as the routers
                return home, raw

            def _diary_write_for_run(run: Any):
                # Resolve the stamped run's HOME DIARY_WRITE (per home, per
                # run) — the words go to the run's own book. None -> the
                # helper refuses loudly (a stamped run without its book must
                # never leak).
                _home, raw = _resolve_home_for_run(run)
                if raw is None:
                    return None
                return raw.get(EffectType.DIARY_WRITE)

            def _rescue_dir_for_run(run: Any):
                # Where to save the entity's raw reply if the book write
                # fails (record-everything ruling, 2026-07-26): inside the
                # run's own home, so the words are never lost even on error.
                home, _raw = _resolve_home_for_run(run)
                return getattr(home, "home_dir", None) if home is not None else None

            try:
                wrapped = wrap_llm_handler_with_conditional_capture(
                    llm_handler,
                    should_capture=_should_capture,
                    diary_write_for_run=_diary_write_for_run,
                    rescue_dir_for_run=_rescue_dir_for_run,
                )
            except TypeError:
                # Older abstractruntime without the rescue parameter: the
                # capture still works, replies just are not rescued on a
                # failed book write (pre-rescue behavior).
                wrapped = wrap_llm_handler_with_conditional_capture(
                    llm_handler,
                    should_capture=_should_capture,
                    diary_write_for_run=_diary_write_for_run,
                )
            try:
                wrapped._entity_llm_capture_wrapped = True  # type: ignore[attr-defined]
            except Exception:
                pass
            handlers_attr[EffectType.LLM_CALL] = wrapped


def wrap_entity_runtime_routing(entity_runtime: Any, *, data_dir: Path) -> None:
    """GW-C (plan items 8/9): the door's stamp-verification wrap over ONE
    per-entity runtime (`abstractruntime.identity.entity_runtime`).

    The composition binds the RAW home handlers (stamp-agnostic — exactly
    what the home-direct driver uses); this wrap makes the frozen spec's
    line true for door-served visit runs: "the visit path JOINS the
    verified path". Every entity effect on this runtime now requires a
    verifying stamp (own or session-matching ancestor — the run TREE rule),
    passes the same payload gates as the shared router, and — one runtime =
    one home — the stamp must name THIS home's entity, which is stronger
    than the shared router's slug dispatch.

    Crypto stays at the routing layer: the raw handlers beneath are
    untouched; the ancestor walk reads the per-entity RUN STORE (visit
    child runs live in the home's own store, never the global one).
    Wrapping twice raises — a double-wrapped door is wiring drift.
    """
    from abstractruntime.core.models import Effect, EffectType
    from abstractruntime.core.runtime import EffectOutcome

    runtime = getattr(entity_runtime, "runtime", None)
    handlers_attr = getattr(runtime, "_handlers", None)
    if not isinstance(handlers_attr, dict):
        raise TypeError("entity_runtime does not expose an effect-handler map to wrap")
    home = entity_runtime.home  # ChatHome: duck-typed .entity_id + .memory (gate needs both)
    run_store = entity_runtime.run_store
    expected_entity = str(home.entity_id)

    payload_gates = {
        EffectType.MEMORY_RECALL: _gate_recall,
        EffectType.MEMORY_FORM: _gate_form,
        EffectType.MEMORY_ADJUST: _gate_adjust,
        EffectType.MEMORY_APPRAISE: _gate_appraise,
        EffectType.MEMORY_ACCESS: _gate_access,  # N4: refuse the sleep-window commit (durable visit lane)
        EffectType.DIARY_WRITE: _gate_diary_write,  # N4 defense-in-depth: refuse at sleep
        # MEMORY_TEND: inject the door-verified channel on the durable-visit
        # lane too (a visit's reflection segment may dispose) — runtime c5413.
        EffectType.MEMORY_TEND: _gate_tend,
    }

    def _make_wrap(etype: Any, raw: Any):
        def _gated(run: Any, effect: Any, default_next_node: Optional[str]):
            stamp, err = resolve_run_stamp(run, data_dir=data_dir, run_store=run_store)
            if stamp is None:
                return EffectOutcome.failed(f"{etype.value} refused at the entity door: {err}")
            stamped_entity = str(stamp.get("entity_id") or "")
            if stamped_entity != expected_entity:
                return EffectOutcome.failed(
                    f"{etype.value} refused: stamp names {stamped_entity!r} but this runtime "
                    f"hosts {expected_entity!r} — one life, one runtime; a foreign stamp never "
                    "crosses homes"
                )
            payload = dict(effect.payload or {})
            gate = payload_gates.get(etype)
            if gate is not None:
                # Same derivation as the shared router: phase (N4) + the
                # close-reflection segment come from the verified stamp +
                # host-written current_node, never the payload. THIS is the
                # durable-visit lane, where the close-reflection collision
                # agency caught (c705) actually fires.
                phase = resolved_phase(stamp)
                segment = in_reflection_segment(stamp, run)
                if etype == EffectType.MEMORY_RECALL:
                    err2 = gate(payload, home=home, stamp=stamp)
                else:
                    err2 = gate(payload, stamp=stamp, phase=phase, segment=segment)
                if err2:
                    return EffectOutcome.failed(f"{etype.value} refused at the entity door: {err2}")
            return raw(run, Effect(type=effect.type, payload=payload, result_key=effect.result_key), default_next_node)

        _gated._entity_routing_wrapped = True  # type: ignore[attr-defined]
        return _gated

    if _ENTITY_HOME_EFFECT_TYPES is not None:
        # Same ONE SOURCE as the shared router: gate every entity-home effect
        # the composition binds (the wrap SKIPS absent handlers below, so an
        # older per-entity composition without the brain quartet stays valid).
        entity_types = sorted(_ENTITY_HOME_EFFECT_TYPES, key=lambda e: str(e.value))
    else:  # pragma: no cover - version-skew degrade (labeled at import)
        entity_types = [
            EffectType.MEMORY_RECALL,
            EffectType.MEMORY_ACCESS,
            EffectType.MEMORY_FORM,
            EffectType.MEMORY_ADJUST,
            EffectType.MEMORY_APPRAISE,
            EffectType.DIARY_WRITE,
            EffectType.DIARY_READ,
        ]
    routed_types = [
        *entity_types,
        # LLM_CALL is stamp-gated too (no payload gate): the G1 act-only
        # wrapper inside it dereferences diary words through the RAW
        # DIARY_READ handler it captured at composition — gating the outer
        # call is what makes that dereference run only on verified runs
        # ("the visit path joins the verified path", the deref included).
        EffectType.LLM_CALL,
        # TOOL_CALLS is stamp-gated for the same reason (no payload gate):
        # tools are the ENTITY'S HANDS — a stampless run must not borrow
        # them. The grant intersection inside the handler stays the tool
        # authority; this gate is the door's identity check before it.
        EffectType.TOOL_CALLS,
    ]
    for etype in routed_types:
        raw = handlers_attr.get(etype)
        if raw is None:
            continue  # a composition without this handler has nothing to gate
        if getattr(raw, "_entity_routing_wrapped", False):
            raise RuntimeError(
                f"effect type {etype.value!r} is already door-wrapped on this runtime; "
                "wrapping twice is wiring drift and stays loud"
            )
        handlers_attr[etype] = _make_wrap(etype, raw)
