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

# The maintainer's round-8 ruling for summoned-entity sessions: "the minimal
# context should be 20 000 tokens, never less." The summon endpoint refuses
# when a declared context window is below this; an unverifiable window
# proceeds with a labeled #FALLBACK warning (the gateway cannot measure a
# model it cannot resolve — the floor is then the operator's to guarantee).
# One source (memory's round-8 export; same discipline as the identity floor).
from abstractmemory import ENTITY_CONTEXT_FLOOR as SUMMON_CONTEXT_FLOOR_TOKENS  # noqa: E402


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
    while the doors served shelf 24 / 65536)."""
    import dataclasses

    from abstractmemory import entity_recall_budget

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
    budget = dataclasses.asdict(entity_recall_budget(window, shelf_size=shelf))
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


def _sign(secret: str, *, entity_id: str, channel: str, session_id: str, nonce: str, run_id: str) -> str:
    basis = "|".join(("entity-stamp-v1", entity_id, channel, session_id, nonce, run_id))
    return hmac.new(secret.encode("utf-8"), basis.encode("utf-8"), hashlib.sha256).hexdigest()


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
    return {
        "entity_id": str(entity_id),
        "channel": str(channel),
        "session_id": str(session_id),
        "participants": [str(p) for p in participants],
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
    final["sig"] = _sign(
        _stamp_secret(data_dir),
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
    expected = _sign(
        _stamp_secret(data_dir),
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
    for entry in raw if isinstance(raw, (list, tuple)) else ():
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            pair = (str(entry[0]).strip().lower(), str(entry[1]).strip())
            if pair[1] == entity_id:
                continue
            if session_pair is not None and pair == session_pair:
                continue
            return (
                f"scope ladder pair {list(pair)!r} is outside this entity's boundary — a summoned "
                f"session may only read/write scopes owned by {entity_id!r} or its own session scope"
            )
        elif isinstance(entry, str):
            scope_name = entry.strip().lower()
            if scope_name == "session":
                continue  # resolves to this run's own session owner
            return (
                f"scope {entry!r} is not allowed for a summoned session — name the entity's scopes "
                f"explicitly ([scope, {entity_id!r}]) or use the run's own 'session' scope"
            )
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


def _gate_form(payload: Dict[str, Any], *, stamp: Dict[str, Any]) -> Optional[str]:
    entity_id = str(stamp.get("entity_id") or "")
    channel = str(stamp.get("channel") or "")
    session_id = str(stamp.get("session_id") or "")

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
    stamped = [str(p) for p in (stamp.get("participants") or [])]
    stamp_visit_id = str(stamp.get("visit_id") or "") or None
    records = payload.get("records")
    if isinstance(records, list) and (stamped or stamp_visit_id is not None):
        for rec in records:
            if not isinstance(rec, dict):
                continue
            attrs = rec.get("attributes")
            attrs = dict(attrs) if isinstance(attrs, dict) else {}
            if stamped:
                attrs["participants"] = stamped
            attrs.pop("visit_id", None)  # payload claims never engrave
            if stamp_visit_id is not None:
                attrs["visit_id"] = stamp_visit_id
            rec["attributes"] = attrs
    return None


def _gate_adjust(payload: Dict[str, Any], *, stamp: Dict[str, Any]) -> Optional[str]:
    entity_id = str(stamp.get("entity_id") or "")
    channel = str(stamp.get("channel") or "")
    session_id = str(stamp.get("session_id") or "")

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


def _gate_appraise(payload: Dict[str, Any], *, stamp: Dict[str, Any]) -> Optional[str]:
    entity_id = str(stamp.get("entity_id") or "")
    channel = str(stamp.get("channel") or "")
    session_id = str(stamp.get("session_id") or "")

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
    claimed = str(payload.get("actor") or "").strip()
    derived = channel_actor(channel, session_id=session_id)
    if claimed and claimed != derived:
        return (
            f"payload claims actor {claimed!r} but this run's channel derives {derived!r} — "
            "actors are stamped by the door, never claimed by payloads"
        )
    payload["actor"] = derived
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
            handlers_by_slug[slug] = (home, handlers)
            return home, handlers

    _payload_gates = {
        EffectType.MEMORY_RECALL: _gate_recall,
        EffectType.MEMORY_FORM: _gate_form,
        EffectType.MEMORY_ADJUST: _gate_adjust,
        EffectType.MEMORY_APPRAISE: _gate_appraise,
        # MEMORY_ACCESS commits a trace produced by a gated recall; DIARY_*
        # bind the author at construction — no payload rewriting needed.
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
                if etype == EffectType.MEMORY_RECALL:
                    err2 = gate(payload, home=home, stamp=stamp)
                else:
                    err2 = gate(payload, stamp=stamp)
                if err2:
                    return EffectOutcome.failed(f"{etype.value} refused at the entity door: {err2}")

            handler = raw.get(etype)
            if handler is None:  # pragma: no cover - all routed types are built above
                return EffectOutcome.failed(f"{etype.value} has no home handler")
            return handler(run, Effect(type=effect.type, payload=payload, result_key=effect.result_key), default_next_node)

        return _route

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
                if etype == EffectType.MEMORY_RECALL:
                    err2 = gate(payload, home=home, stamp=stamp)
                else:
                    err2 = gate(payload, stamp=stamp)
                if err2:
                    return EffectOutcome.failed(f"{etype.value} refused at the entity door: {err2}")
            return raw(run, Effect(type=effect.type, payload=payload, result_key=effect.result_key), default_next_node)

        _gated._entity_routing_wrapped = True  # type: ignore[attr-defined]
        return _gated

    routed_types = [
        EffectType.MEMORY_RECALL,
        EffectType.MEMORY_ACCESS,
        EffectType.MEMORY_FORM,
        EffectType.MEMORY_ADJUST,
        EffectType.MEMORY_APPRAISE,
        EffectType.DIARY_WRITE,
        EffectType.DIARY_READ,
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
