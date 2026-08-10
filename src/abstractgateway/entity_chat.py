"""Gateway-hosted entity chat sessions (a2a 0007, maintainer's chat drawer).

The maintainer's ask ("a chat drawer so i could chat directly with the AI
here") needs a TURN endpoint. This module hosts the runtime chat driver's
`ChatSession` — the same object the `entity chat` CLI runs — behind an
operator-authed HTTP surface, so the web chat and the terminal chat cannot
drift (one turn loop, two front doors).

Semantics inherited from the CLI driver, not reimplemented:

- **Auto-yield** (`--pause-loop` parity): if the entity's own-time loop has
  an open day, the open puts the entity to sleep (`mode="visiting"`, honest
  reason) and waits for the loop's day to close at its tick boundary. HTTP
  cannot block 15 minutes, so the wait is bounded and a timeout refuses
  loudly (409) — retry when the loop yields. A stale auto-yield (a crashed
  prior visit) is adopted together with the duty to wake him.
- **One life, one summon**: a second open on a home with a live session
  refuses (409). Paused entities refuse (the hard freeze). Asleep entities
  NOT in a visiting/auto-yield posture refuse — wake him first, or let the
  auto-yield negotiate with his loop.
- **Reflection on close**: feelings move in the entity-reflection pass;
  a reflection failure never voids the session that already happened.
- **The loop is never orphaned**: if the visit yielded the loop, close
  (and the idle reaper) wake it — a crashed visit leaving him asleep
  forever is the worst outcome of the yield.

Host markers: the open stamps a `summon` marker (channel=operator-visit)
and close stamps `session_closed`, so the visit is on the observable
record like every other door moment.

Idle sessions: an abandoned browser tab must not hold the loop hostage.
The next `open` on the same home reaps sessions idle beyond the timeout
(reflection still runs).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = ["EntityChatHost", "ChatOpenRefused"]

DEFAULT_IDLE_TIMEOUT_S = 15 * 60
# HTTP-bounded auto-yield wait: long enough for a tick boundary on a busy
# host, short enough that a browser request does not appear hung.
DEFAULT_YIELD_WAIT_S = 55.0

# Attention-geometry defaults (maintainer ruling 2026-07-08): the wide summon
# posture is the DEFAULT, not a tuning trick. Env vars
# ABSTRACTGATEWAY_ENTITY_CHAT_SHELF_SIZE / _CONTEXT_WINDOW and the request
# body override; these constants are the floor experience every entity gets.
# Shelf widened 24 -> 36 (maintainer, 2026-07-09: "it needs to retrieve more
# memories to function") -> 50 (maintainer c2468, 2026-07-15: "increase the
# max memories from 30 to 50" — his "30" was 36 minus the 6 identity seats,
# which render in the prelude, not the MEMORIES list). Arithmetic (memory's
# c2471 check on Ephemeral's live trace): observed digests ~77 tokens, so
# 50 seats ≈ 3,850 of the 7,864 budget (12% of 65536) — 2x headroom today.
# Caveat on record: at the ~200-token RICH-digest planning figure tokens
# bind first (~39 seats fill); the companion knob is the recall budget's
# token_fraction 0.12 -> 0.16 if live traces show tokens_used pinning.
DEFAULT_ENTITY_CHAT_SHELF_SIZE = 50
DEFAULT_ENTITY_CHAT_CONTEXT_WINDOW = 65536

# Output headroom has NO code default (ADR-0026 §2 + the operator's standing
# rule: never cap a budget unless the user asked). The prior 4096/2048 code
# constants were exactly the forbidden shape — an arbitrary literal nobody
# asked for, silently shrinking the wire cap. Measured on 2026-08-02 against
# a local qwen/qwen3.6-35b-a3b: with no caller cap AbstractCore sends the
# model's advertised ceiling (max_tokens 81920); with the old hardcoded 4096
# it sent 4096 — a 20x silent shrink of an entity's ability to finish a
# thought, and a `finish_reason=length` waiting to happen on any long report.
# None means "the caller chose nothing": AbstractCore then derives the bound
# from the model's registry capability (LM Studio, which requires a bound) or
# omits the parameter entirely (APIs that allow it) — never a literal here.
# An operator who WANTS a safeguard sets it explicitly, per request or via
# this env knob; an explicit operator cap is honored verbatim.
ENTITY_MAX_OUTPUT_TOKENS_ENV = "ABSTRACTGATEWAY_ENTITY_MAX_OUTPUT_TOKENS"


def resolve_entity_output_cap(explicit: Optional[int] = None) -> Optional[int]:
    """Resolve the entity output-token cap: request > operator env > unset.

    Returns None when nobody asked for a cap, which is the signal to leave
    `max_output_tokens` OUT of the LLM kwargs so AbstractCore uses the model's
    full advertised output capability. Never invents a number.
    """
    if explicit is not None:
        try:
            value = int(explicit)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    import os as _os

    raw = (_os.getenv(ENTITY_MAX_OUTPUT_TOKENS_ENV) or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None
# Mind substrate has NO code default (maintainer ruling 2026-07-09 04:26:
# "I decide which provider and model is used ... NO FALLBACK" — a code
# constant silently electing a provider, paid OVH in the removed case, is
# exactly the fallback class the ADR forbids). Refined 06:32 ("i don't see
# the point in having potentially different models for visit and own time,
# remove that"): the entity carries ONE persisted substrate — an operator
# file in his home (substrate.yaml, beside tool_policy.yaml) — set once,
# shown by the UI, used by BOTH visits and his own time. Resolution:
# request body (explicit override) > home substrate.yaml > operator env >
# LOUD REFUSAL. Every step is an explicit operator choice; still no code
# default anywhere.
SUBSTRATE_FILENAME = "substrate.yaml"
SUBSTRATE_REFUSAL = (
    "no mind substrate chosen for this entity: set it once "
    "(PUT /{name}/substrate, or the UI's substrate picker) — "
    "the operator decides; the gateway never falls back on its own. "
    "The choice persists per entity in substrate.yaml under the entity's home "
    "directory; ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER and "
    "ABSTRACTGATEWAY_ENTITY_CHAT_MODEL set a host-wide fallback for every "
    "entity that has none. AbstractCore's output.text default is deliberately "
    "not consulted here: it answers which model this host uses for text, not "
    "which mind this being is."
)


def read_entity_substrate(home_dir: Any) -> Dict[str, str]:
    """The entity's persisted substrate choice: {provider, model} or {}.
    A malformed file reads as unset (works-or-loud at the resolve site).

    Delegates to the runtime's single reader (`identity.substrate`) so the
    file-parse contract has ONE source — the diary_type-clamp lesson: two
    copies with no shared source drift silently. The local parse remains
    only as the version-tolerant fallback for older runtimes."""
    from pathlib import Path

    try:
        from abstractruntime.identity.substrate import read_home_substrate

        out = read_home_substrate(Path(home_dir))
        # Version-skew belt (adversary cycle-2 N1): a runtime reader from
        # before the thinking field drops it on every read — and the PUT
        # door's keep-semantics would then rewrite the file WITHOUT it
        # (permanent loss). The gateway wrote the key; reading its own key
        # when the delegate does not return it is coherent, not a fork.
        if isinstance(out, dict) and out.get("provider") and out.get("model") and "thinking" not in out:
            local = _read_substrate_locally(home_dir)
            t = str(local.get("thinking") or "").strip()
            if t:
                out = dict(out)
                out["thinking"] = t
        return out
    except ImportError:
        pass  # older runtime without identity.substrate — parse locally

    return _read_substrate_locally(home_dir)


def _read_substrate_locally(home_dir: Any) -> Dict[str, str]:
    """The gateway's own substrate.yaml parse (fallback + the N1 belt)."""
    from pathlib import Path

    path = Path(home_dir) / SUBSTRATE_FILENAME
    if not path.exists():
        return {}
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        p = str(data.get("provider") or "").strip()
        m = str(data.get("model") or "").strip()
        if not (p and m):
            return {}
        out = {"provider": p, "model": m}
        # Optional reasoning effort, spelled `thinking` at rest (the
        # reasoning plan's one-spelling decision). Same rules as the
        # runtime reader: rides only with a full provider+model pair,
        # blank means unset. Mirrored here so the older-runtime fallback
        # never drops a stored choice.
        t = str(data.get("thinking") or "").strip()
        if t:
            out["thinking"] = t
        return out
    except Exception:
        return {}


def write_entity_substrate(
    home_dir: Any, *, provider: str, model: str, thinking: Optional[str] = None
) -> None:
    """Persist the operator's one-per-entity substrate choice (his home,
    operator-owned like tool_policy.yaml; the entity's tools cannot touch
    the home root).

    `thinking` is the optional reasoning effort for the mind. None means
    "no choice" and writes nothing — the file stays exactly as small as
    before this field existed."""
    from pathlib import Path

    p, m = str(provider or "").strip(), str(model or "").strip()
    if not p or not m:
        raise ValueError("substrate needs BOTH provider and model (explicit operator choice)")
    import yaml

    data: Dict[str, str] = {"provider": p, "model": m}
    t = str(thinking or "").strip()
    if t:
        data["thinking"] = t
    # Atomic write (adversary cycle-2 N7): a crash mid-write must never
    # leave malformed YAML that reads as "substrate unset".
    target = Path(home_dir) / SUBSTRATE_FILENAME
    tmp = target.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
    tmp.replace(target)


def resolve_substrate(
    provider: Optional[str], model: Optional[str], *, home_dir: Any = None,
    thinking: Optional[str] = None,
) -> Tuple[str, str, Optional[str]]:
    """Request override > home substrate.yaml > operator env > refuse.
    Never a code default (NO FALLBACK).

    This chain deliberately does NOT read AbstractCore's `output.text` default.
    That route answers "what model does this host use for text"; this one
    answers "which mind is THIS being", and the two agreeing would be a
    coincidence rather than a contract. An entity without a chosen mind refuses
    so the choice stays the operator's.

    Returns (provider, model, thinking). The reasoning effort follows the
    same chain but NEVER refuses: a mind without a declared effort is
    valid, so absent stays absent (None). There is no environment variable
    for it — the home file is the one persisted choice."""
    import os as _os

    p = (provider or "").strip()
    m = (model or "").strip()
    t = (thinking or "").strip()
    if (not (p and m) or not t) and home_dir is not None:
        stored = read_entity_substrate(home_dir)
        p = p or stored.get("provider", "")
        m = m or stored.get("model", "")
        t = t or stored.get("thinking", "")
    p = p or (_os.getenv("ABSTRACTGATEWAY_ENTITY_CHAT_PROVIDER") or "").strip()
    m = m or (_os.getenv("ABSTRACTGATEWAY_ENTITY_CHAT_MODEL") or "").strip()
    if not p or not m:
        raise ChatOpenRefused(400, SUBSTRATE_REFUSAL)
    return p, m, (t or None)


class ChatOpenRefused(Exception):
    """A refusal with an HTTP status shape (409 conflicts, 400 bad asks)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = int(status)
        self.detail = detail


class _RuntimeLLMAdapter:
    """The driver's duck-typed llm contract (`.generate(messages=...,
    system_prompt=...) -> .content`) over the RUNTIME's exported client.

    Boundary ruling (backlog 0059, landing night): the gateway never
    imports abstractcore directly — the one violation this file briefly
    carried was mine and is closed here. `LocalAbstractCoreLLMClient` is
    AbstractRuntime's sanctioned in-process LLM surface; it returns
    RunState-safe dicts, so this adapter lifts `content` back into the
    attribute shape the chat driver expects."""

    def __init__(self, provider: str, *, model: str, thinking: Optional[str] = None, **llm_kwargs: Any) -> None:
        from abstractruntime.integrations.abstractcore import LocalAbstractCoreLLMClient

        self._client = LocalAbstractCoreLLMClient(provider=provider, model=model, llm_kwargs=dict(llm_kwargs))
        # The mind's reasoning effort (substrate triple). Injected into each
        # call's params on the one wire name; None sends nothing.
        self._thinking = str(thinking or "").strip() or None

    def generate(self, **kwargs: Any) -> Any:
        from types import SimpleNamespace

        kwargs.setdefault("prompt", "")
        if self._thinking:
            params = kwargs.get("params")
            params = dict(params) if isinstance(params, dict) else {}
            params.setdefault("thinking", self._thinking)
            kwargs["params"] = params
        out = self._client.generate(**kwargs)
        if isinstance(out, dict):
            return SimpleNamespace(**{"content": out.get("content"), **{k: v for k, v in out.items() if k != "content"}})
        return out


def _default_llm_factory(provider: str, **kwargs: Any) -> Any:
    model = str(kwargs.pop("model", "") or "")
    thinking = kwargs.pop("thinking", None)
    return _RuntimeLLMAdapter(provider, model=model, thinking=thinking, **kwargs)


def _woken_reason() -> str:
    """The shared B1 wake-reason prefix (one spelling, entities.py owns it)."""
    from .entities import WOKEN_BY_VISIT_REASON

    return WOKEN_BY_VISIT_REASON


@dataclass
class _HostedChat:
    chat_id: str
    entity_slug: str
    entity_id: str
    session: Any            # runtime ChatSession
    home: Any               # runtime ChatHome (owns its own store handles)
    yielded_loop: bool
    opened_at: str
    model_info: Dict[str, str]
    lease: Any = None       # the visit's home-lease window (GW-A); released at close
    # True when THIS visit woke an operator-asleep entity (B1 doors-wake).
    # Close restores the sleep (skill's c219 audit: the drawer left awake
    # standing after waking a sleeper — the resting default must return).
    woke_for_visit: bool = False
    # The operator's word BEFORE this visit touched the state (mutual-
    # exclusivity wave, laurent dm#94): open writes the visiting posture
    # unconditionally; close restores THIS, never a hardcoded word.
    prior_state: Dict[str, str] = field(default_factory=lambda: {"state": "awake", "reason": ""})
    last_activity: float = 0.0
    turn_lock: threading.Lock = field(default_factory=threading.Lock)
    closed: bool = False
    # True while a turn's LLM call executes (the chat lane bills home-direct;
    # /cognition.working reads this — state-sources adversary P1-5).
    turn_in_flight: bool = False
    # The shared room's common record: every voice's turns in order
    # (speaker, text, reply, tools_ran, at). Session-lived, never persisted
    # here — the entity's own memory is the durable record.
    transcript: List[Dict[str, Any]] = field(default_factory=list)


class EntityChatHost:
    """Open/turn/close over hosted ChatSessions, one live session per home."""

    def __init__(
        self,
        registry: Any,
        *,
        llm_factory: Optional[Callable[..., Any]] = None,
        idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
        yield_wait_s: float = DEFAULT_YIELD_WAIT_S,
        summon_seat_probe: Any = None,
    ) -> None:
        self._registry = registry
        # `summon_seat_probe(slug) -> seat|None` (conversation-seat plan item
        # 5, service-wired from entity_seat.build_summon_seat_probe): a
        # summon turn MID-FLIGHT holds the one life, so a chat open under it
        # would interleave sessions. Live-run-only by the probe's contract
        # (a TTL-idle seat never blocks the drawer); absent probe = the old
        # blindness, honestly (standalone/test hosts).
        self._summon_seat_probe = summon_seat_probe
        # Late-bound: the module attribute is looked up at OPEN time, so a
        # test monkeypatching `_default_llm_factory` reaches hosts that were
        # constructed before the patch (a def-time default would freeze the
        # original function and silently call the real LLM in suites).
        self._llm_factory = llm_factory
        self._idle_timeout_s = float(idle_timeout_s)
        self._yield_wait_s = float(yield_wait_s)
        self._sessions: Dict[str, _HostedChat] = {}   # chat_id -> chat
        self._by_slug: Dict[str, str] = {}            # slug -> live chat_id
        self._lock = threading.RLock()
        # The reaper must not depend on API traffic (live failure 2026-07-09:
        # a web visit idled for hours, and because reaping only ran inside
        # open(), the entity's yielded loop stayed asleep the whole time —
        # no new visitor, no wake). A daemon thread sweeps on its own clock;
        # close_hosted wakes the loop exactly as a manual close would.
        self._reaper = threading.Thread(
            target=self._reap_forever, name="entity-chat-idle-reaper", daemon=True
        )
        self._reaper.start()
        try:
            from .worker_registry import register_worker

            register_worker("entity-chat-idle-reaper", self._reaper)
        except Exception:
            pass

    # ------------------------------------------------------------------ open
    def open(
        self,
        name: str,
        *,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        participants: Optional[List[str]] = None,
        context_window: Optional[int] = None,
        shelf_size: Optional[int] = None,
        # Output headroom: None = nobody asked for a cap, so the model's full
        # advertised output capability is used (see resolve_entity_output_cap
        # and ENTITY_MAX_OUTPUT_TOKENS_ENV). An explicit value is an operator
        # safeguard and is honored verbatim.
        max_output_tokens: Optional[int] = None,
        enable_tools: bool = True,
        enable_workspace: bool = False,
    ) -> Dict[str, Any]:
        import os as _os

        base_url = (base_url or _os.getenv("ABSTRACTGATEWAY_ENTITY_CHAT_BASE_URL") or "http://127.0.0.1:1234/v1").strip()
        # Attention geometry is operator config too (same ruling): an
        # undeclared context window collapses to the 20k floor (token budget
        # 2400) and the default 12-seat shelf pins every turn to 6 self +
        # 3 STM + 3 stimulus — the observed "only 6 memories" ceiling.
        # ints via env so the web UI need not know the numbers.
        # Works-or-loud (live lesson, 2026-07-08): a pasted non-breaking space
        # glued SHELF_SIZE=24 to the next export and the first cut of this
        # parser swallowed it SILENTLY — the operator set the knob and nothing
        # moved. Malformed values now salvage a leading integer when one
        # exists, and always say so in the open's warnings.
        env_warnings: List[str] = []

        def _env_int(name: str) -> Optional[int]:
            import re as _re

            raw = (_os.getenv(name) or "").strip()
            if not raw:
                return None
            try:
                return int(raw)
            except ValueError:
                lead = _re.match(r"\s*(\d+)", raw)
                if lead:
                    env_warnings.append(
                        f"#FALLBACK {name}={raw!r} is malformed (stray characters — a pasted "
                        f"non-breaking space?); using its leading integer {lead.group(1)}"
                    )
                    return int(lead.group(1))
                env_warnings.append(f"#FALLBACK {name}={raw!r} is not an integer; ignored")
                return None

        # Maintainer ruling (2026-07-08, after the live A/B on Castor): the
        # wide posture IS the default — shelf 24, context 65536 ("it feels
        # like castor has grown up"; 32768 verified live, then doubled).
        # Resolution order: request body > env > these defaults. Seats bind
        # before tokens at shelf 24 (~200-token digests ≈ 4.8k < 12% of 64k),
        # so the wide window buys headroom, not prompt bloat.
        if context_window is None:
            context_window = _env_int("ABSTRACTGATEWAY_ENTITY_CHAT_CONTEXT_WINDOW")
        if context_window is None:
            context_window = DEFAULT_ENTITY_CHAT_CONTEXT_WINDOW
        if shelf_size is None:
            shelf_size = _env_int("ABSTRACTGATEWAY_ENTITY_CHAT_SHELF_SIZE")
        if shelf_size is None:
            shelf_size = DEFAULT_ENTITY_CHAT_SHELF_SIZE
        import time as _time

        from abstractruntime.identity.chat import ChatSession, open_home
        from abstractruntime.identity.life import (
            await_loop_quiescent,
            read_entity_state,
            read_loop_status,
            write_entity_state,
        )

        manifest = self._registry.manifest_for(name)  # KeyError -> 404 at the route
        slug = manifest.slug
        home_dir = self._registry.entities_dir / slug

        # Maintenance window: refuse BEFORE any provider probe or state
        # read — a held home must answer 409 first, never 502 on an
        # unrelated substrate check (doctoring, operator GO 2026-07-13).
        self._registry._refuse_if_held(slug)

        # ONE substrate per entity (maintainer ruling 2026-07-09 06:32:
        # visits and own time share the SAME mind). Request override > the
        # home's persisted substrate.yaml > operator env > loud refusal.
        # The third element is the mind's reasoning effort (optional).
        provider, model, thinking = resolve_substrate(provider, model, home_dir=home_dir)

        with self._lock:
            self._reap_idle_locked()
            live = self._by_slug.get(slug)
            if live is not None:
                raise ChatOpenRefused(
                    409,
                    f"a chat session is already open on {manifest.entity_id} (chat_id {live!r}) — "
                    "one life, one summon; close it or let it idle out",
                )
        # THE SUMMON LANE too (conversation-seat plan, item 5: one seat,
        # three doors): a summon turn mid-flight holds the one life. The
        # probe answers only LIVE runs — a TTL-idle summon seat never blocks
        # the drawer's own chat open.
        if self._summon_seat_probe is not None:
            seat = None
            try:
                seat = self._summon_seat_probe(slug)
            except Exception:  # noqa: BLE001 - a broken probe must not block the door
                seat = None
            if seat is not None:
                raise ChatOpenRefused(
                    409,
                    f"a summoned conversation is mid-turn on {manifest.entity_id} "
                    f"(run {seat.get('run_id')!r}, session {seat.get('session_id')!r}, "
                    f"holder {seat.get('holder') or 'unknown'}) — one life, one summon; "
                    "wait for the turn to end",
                )

        state = read_entity_state(home_dir)
        mode = str(state.get("mode") or "")
        reason = str(state.get("reason") or "")
        if state.get("state") == "paused":
            raise ChatOpenRefused(409, f"{manifest.entity_id} is paused (hard freeze): {reason or 'no reason recorded'}")

        # THE VISITING POSTURE IS UNCONDITIONAL (mutual-exclusivity wave,
        # laurent dm#94 via entity's write audit finding 1): a visit opened
        # on an awake, loop-less entity used to write NOTHING durable — the
        # state file, other processes, and post-restart folds could not know
        # a visit existed, and the composite folds rendered "personal ·
        # resting" beside a live chat. ONE write at every open makes the
        # state file the restart-surviving visit truth every consumer
        # already understands (asleep+mode=visiting = the visit marker; the
        # folds render it as phase=visit). The posture carries the visit's
        # OWN identity ("[visit <chat_id>]") so closes/restores match by
        # OWNERSHIP, never by words (audit finding 4). The operator's prior
        # word is recorded and restored at close.
        #
        # Loop-alive nuance kept: the loop is a live process whenever phase
        # is "day" OR "between" (it re-opens a day on state=awake at its
        # next top-gate), so the posture is written FIRST (the loop's
        # top-gate cannot re-open a day) and quiescence is awaited.
        yielded = False
        woke_for_visit = False
        visitor = (participants or ["person:operator"])[0]
        chat_id = f"chat-{uuid.uuid4().hex[:12]}"
        visit_token = f"[visit {chat_id}]"
        # The operator's word before THIS visit touched the state. A stale
        # visiting posture belongs to a PREVIOUS visit — the operator's own
        # word is not recoverable from it; awake is the honest restore.
        # wake_at rides along so a BOUNDED sleep keeps its deadline through
        # the visit (runtime c343 seam).
        prior_state = {
            "state": str(state.get("state") or "awake"),
            "reason": reason,
            "wake_at": str(state.get("wake_at") or ""),
        }
        if state.get("state") == "asleep" and (mode == "visiting" or "auto-yield" in reason):
            # FRESHNESS GATE (wave adversary P1-1): a young posture may
            # belong to a visit MID-OPEN on the durable lane (its run is not
            # in status() until the leg stamps it) — adopting it destroyed
            # the live visit's ownership token. Mirrors the durable lane and
            # the /loop/start registration-window guard.
            from .entity_visits import EntityVisitHost, visiting_posture_age_s

            age = visiting_posture_age_s(state)
            if age is not None and age < float(EntityVisitHost.STALE_YIELD_GRACE_S):
                raise ChatOpenRefused(
                    409,
                    f"a visit is opening on {manifest.entity_id} (visiting posture {age:.0f}s old) — "
                    "one life, one summon; retry shortly "
                    "(a genuinely stale posture is adoptable after the grace window)",
                )
            prior_state = {"state": "awake", "reason": ""}
        elif state.get("state") == "asleep":
            # B1 ruling (laurent 04:58; newborn shape c1503: doors WAKE, they
            # don't refuse): an operator-asleep entity is visitable — the
            # posture write below supersedes the old explicit awake write;
            # close restores the operator's sleep from prior_state.
            woke_for_visit = True
        loop_alive = bool(read_loop_status(home_dir).get("running"))
        write_entity_state(
            home_dir, "asleep",
            reason=f"in conversation with {visitor} {visit_token}"
            + (" (auto-yield)" if loop_alive else ""),
            mode="visiting",
            written_by="visit-door",
        )
        if loop_alive:
            if not await_loop_quiescent(home_dir, timeout_seconds=self._yield_wait_s):
                # Restore the prior word so a failed open never strands the
                # loop asleep (nor erases an operator's sleep).
                self._restore_prior_state(home_dir, prior_state, suffix="(visit open aborted: loop did not yield in time)", token=visit_token)
                raise ChatOpenRefused(
                    409,
                    "the entity's own-time loop has not reached a tick boundary yet — "
                    f"waited {self._yield_wait_s:.0f}s; retry shortly (the yield request stands)",
                )
            yielded = True

        # One writer per home (plan item 1, GW-A): the visit IS a write
        # window. Acquired AFTER the auto-yield (the loop's day lease is
        # released at the boundary we just waited for); anything still
        # holding the home — a CLI visit, a dream pass, a maintenance run —
        # refuses this open loudly, naming the holder. The auto-yield
        # negotiation stays ABOVE the lease (policy); this is the belt.
        lease: Any = None
        try:
            from abstractruntime.storage.lease import DirectoryLeaseHeld, acquire_directory_lease

            try:
                lease = acquire_directory_lease(home_dir, holder="visit-host")
            except DirectoryLeaseHeld as e:
                self._restore_prior_state(home_dir, prior_state, suffix="(visit open aborted: home has a writer)", token=visit_token)
                raise ChatOpenRefused(409, str(e))
        except ImportError:
            # Older runtime without storage.lease: no site acquires (the
            # loop's sites are absent too) — labeled, never silent.
            env_warnings.append(
                "#FALLBACK runtime predates the home lease; visit opened without the writer mutex"
            )

        try:
            home = open_home(home_dir, embedder=self._registry._resolve_embedder())
            llm_kwargs: Dict[str, Any] = {"model": model}
            output_cap = resolve_entity_output_cap(max_output_tokens)
            if output_cap is not None:
                # Only an ASKED-FOR cap rides the wire; otherwise the key stays
                # absent so AbstractCore uses the model's full output budget.
                llm_kwargs["max_output_tokens"] = output_cap
            if thinking:
                # The mind's reasoning effort rides every turn of this visit.
                llm_kwargs["thinking"] = thinking
            if str(provider).strip().lower() in ("lmstudio", "openai-compatible", "openai_compatible"):
                llm_kwargs["base_url"] = base_url
            factory = self._llm_factory or _default_llm_factory
            llm = factory(str(provider).strip().lower(), **llm_kwargs)

            quiet: List[str] = list(env_warnings)  # operator-config problems travel with the open
            session_kwargs: Dict[str, Any] = dict(
                participants=list(participants or ["person:operator"]),
                context_window=context_window,
                enable_tools=bool(enable_tools),
                enable_workspace=bool(enable_workspace),
                model_info=(
                    # The mind stamp records the full triple when a reasoning
                    # effort is set, so "which effort was the mind at when
                    # this record formed" is answerable later.
                    {"provider": str(provider), "model": str(model), "thinking": str(thinking)}
                    if thinking
                    else {"provider": str(provider), "model": str(model)}
                ),
                out=quiet.append,  # prelude warnings surface in the response, not a console
            )
            if shelf_size is not None:
                # Version-tolerant: only widened summons require a driver
                # that knows the knob (a default open works on older drivers).
                session_kwargs["shelf_size"] = int(shelf_size)
            session = ChatSession(home, llm, **session_kwargs)
        except SystemExit as e:
            # A refused prelude aborts the summon — the reasons travel verbatim.
            if lease is not None:
                lease.release()
            self._restore_prior_state(home_dir, prior_state, suffix="(visit aborted: prelude refused)", token=visit_token)
            raise ChatOpenRefused(409, f"summon refused: {'; '.join(quiet) or e}")
        except BaseException:
            if lease is not None:
                lease.release()
            self._restore_prior_state(home_dir, prior_state, suffix="(visit aborted: open failed)", token=visit_token)
            raise

        # The reflection-loss guard, web half (a2a 0007/171500Z): if a
        # PREVIOUS session died unreflected (write-ahead marker), its
        # look-back runs FIRST, over its own sheet, attributed to its own
        # session id — never a re-opened closed sheet. Version-tolerant:
        # older drivers without the guard just skip (labeled).
        salvage: Optional[Dict[str, Any]] = None
        lookback = getattr(session, "run_pending_lookback", None)
        if callable(lookback):
            try:
                salvage = lookback()
            except Exception as e:  # a salvage failure never blocks the new visit
                quiet.append(f"#FALLBACK pending look-back failed ({e}); the marker stays for the next open")
        else:
            quiet.append("#FALLBACK driver predates the reflection-loss guard; no salvage check ran")

        hosted = _HostedChat(
            chat_id=chat_id,
            entity_slug=slug,
            entity_id=manifest.entity_id,
            session=session,
            home=home,
            yielded_loop=yielded,
            woke_for_visit=woke_for_visit,
            opened_at=datetime.now(timezone.utc).isoformat(),
            model_info={"provider": str(provider), "model": str(model)},
            lease=lease,
            last_activity=_time.monotonic(),
            prior_state=dict(prior_state),
        )
        with self._lock:
            self._sessions[chat_id] = hosted
            self._by_slug[slug] = chat_id

        self._marker(
            hosted, "summon",
            details={"channel": "operator-visit", "session_id": session.session_id, "model": str(model)},
        )
        out: Dict[str, Any] = {
            "chat_id": chat_id,
            "entity_id": manifest.entity_id,
            "session_id": session.session_id,
            "participants": list(session.participants),
            "prelude_tokens": sum(session.prelude["section_tokens"].values()),
            "budget_profile": dict(session.profile),
            "yielded_loop": yielded,
            "warnings": quiet,
        }
        if salvage:
            out["salvage"] = {
                "reply": salvage.get("reply"),
                "feelings_applied": salvage.get("feelings_applied", []),
                "interests": salvage.get("interests", []),
            }
        return out

    # ------------------------------------------------------------------ turn
    def turn(self, chat_id: str, text: str, *, speaker: Optional[str] = None) -> Dict[str, Any]:
        """One honest turn. `speaker` (namespace:name) makes this a SHARED
        ROOM turn (maintainer's experiment, the landing evening: "several
        entities could come together and have a live discussion"): the
        speaker is validated, JOINS the session's participants (stamps +
        presence line update from that turn on), and the turn is attributed
        to their voice — never to whoever opened the session.
        """
        import time as _time
        from datetime import datetime, timezone

        hosted = self._get(chat_id)
        user_text = str(text or "").strip()
        if not user_text:
            raise ChatOpenRefused(400, "an empty turn says nothing — send text")

        speaker_label: Optional[str] = None
        if speaker is not None and str(speaker).strip():
            speaker_label = str(speaker).strip()
            ns, _, name_part = speaker_label.partition(":")
            if not ns or not name_part:
                raise ChatOpenRefused(
                    400,
                    f"speaker must be namespace:name (person:laurent, agent:ariadne), got {speaker_label!r}",
                )

        with hosted.turn_lock:
            if hosted.closed:
                raise ChatOpenRefused(409, f"chat {chat_id!r} is closed")
            if speaker_label and speaker_label not in hosted.session.participants:
                # Joining the room is visible: participants stamp + presence
                # line carry the new voice from this turn forward.
                hosted.session.participants.append(speaker_label)
            # In-flight flag (state-sources adversary P1-5): the chat lane
            # bills tokens home-direct; /cognition's `working` reads this so
            # the "am I spending?" surface is true DURING chat turns.
            hosted.turn_in_flight = True
            try:
                reply, report = hosted.session.turn(
                    user_text, **({"speaker_label": speaker_label} if speaker_label else {})
                )
            finally:
                hosted.turn_in_flight = False
            hosted.last_activity = _time.monotonic()
            hosted.transcript.append({
                "turn_id": report.turn_id,
                "speaker": speaker_label or (hosted.session.participants[0] if hosted.session.participants else "person:operator"),
                "text": user_text,
                "reply": reply,
                "tools_ran": list(report.tools),
                "at": datetime.now(timezone.utc).isoformat(),
            })
        return {
            "reply": reply,
            "turn_id": report.turn_id,
            # DRIVER-AUTHORED authority (the marker-imitation lesson): what
            # actually ran, as data — never derived from the reply prose.
            "tools_ran": list(report.tools),
            "memories_in_context": report.displayed,
            "records_formed": list(report.formed),
            "diary_entries": list(report.diary),
            "notices": list(report.notices),
            "participants": list(hosted.session.participants),
            # The probe surface (maintainer, 2026-07-08: "probe the input
            # context and the outcomes"): every memory that entered the
            # prompt (digest, lifetime count, temporal activation), each
            # tool election with its argument, each file touched. Driver-
            # authored; version-tolerant for older drivers.
            "memories": [dict(m) for m in getattr(report, "memories", [])],
            "tool_details": [dict(t) for t in getattr(report, "tool_details", [])],
            "files": [dict(f) for f in getattr(report, "files", [])],
            # The EXACT prompt this turn sent to the model (observability,
            # maintainer 2026-07-09; operator transparency ruling — never
            # gated). Version-tolerant: empty on older drivers.
            "system_prompt": str(getattr(report, "system_prompt", "") or ""),
        }

    # ------------------------------------------------------------ transcript
    def transcript(self, chat_id: str) -> Dict[str, Any]:
        """The shared room's common view: every voice's turns, in order.
        Pure read — any participant (or the operator's UI) can poll it."""
        hosted = self._get(chat_id)
        with hosted.turn_lock:
            return {
                "chat_id": chat_id,
                "participants": list(hosted.session.participants),
                "turns": [dict(t) for t in hosted.transcript],
            }

    # ----------------------------------------------------------------- close
    def close(self, chat_id: str, *, reflect: bool = True) -> Dict[str, Any]:
        hosted = self._get(chat_id)
        with hosted.turn_lock:
            if hosted.closed:
                raise ChatOpenRefused(409, f"chat {chat_id!r} is already closed")
            hosted.closed = True
        return self._close_hosted(hosted, reflect=reflect)

    def status(self, name: str) -> Dict[str, Any]:
        manifest = self._registry.manifest_for(name)
        with self._lock:
            chat_id = self._by_slug.get(manifest.slug)
            hosted = self._sessions.get(chat_id) if chat_id else None
        if hosted is None or hosted.closed:
            return {"open": False}
        return {
            "open": True,
            "chat_id": hosted.chat_id,
            "session_id": hosted.session.session_id,
            "opened_at": hosted.opened_at,
            "turns": len(hosted.session.reports),
            "turn_in_flight": bool(getattr(hosted, "turn_in_flight", False)),
            "model_info": dict(hosted.model_info),
        }

    # The ONE composite phase (observer's ask c, maintainer 2026-07-09 02:02:
    # the header showed VISITING + RESTING + own-time-lit at once because the
    # client re-derived state from three raw sources and they contradicted).
    # Computed SERVER-SIDE with a single precedence so clients never re-derive
    # (and re-bug) it. Mutually exclusive by construction: exactly one `phase`.
    # SPELLING (c786 phase vocabulary): the running-loop value is "personal"
    # (the ruled word for the entity's own time) so the operator never reads
    # a retired word; the own_time_running/own_time_phase FIELD NAMES stay —
    # they are a served API contract consumers already read, and renaming
    # fields breaks clients for zero semantic gain (flagged to observer).
    # ONE VOCABULARY (laurent dm#79 "one state graph, shared"; sharedgraph
    # adversary P1-2: this composite served FIVE non-graph words in a field
    # named `phase` and disagreed with /cognition on the same instant). The
    # `phase` field now carries GRAPH WORDS ONLY (visit/work/personal/sleep;
    # None = kill switch); the old nuances survive verbatim in `posture`
    # (visiting/paused/asleep/yielded/resting/sleep) so no renderer loses
    # information — a consumer that read the old words reads posture now.
    _LIFE_PHASES = ("visit", "work", "personal", "sleep")

    def life_state(self, name: str) -> Dict[str, Any]:
        """The gateway-computed life phase — one mutually-exclusive answer the
        observer consumes directly, in GRAPH WORDS (the /entities/spec/phases
        vocabulary): a visit outranks everything (the loop is auto-yielded
        under it — the yield posture IS a visit in progress); paused blocks
        the phase axis (None + posture=paused); a running loop day is work
        when a work order stands, else personal; a loop between days is
        personal (the phase spans its rest windows) with posture=resting;
        the floor is sleep (resting default — never an awake-idle dwelling,
        laurent c203). `posture` carries the bookkeeping nuance the old
        vocabulary served as phase words."""
        from .entity_loop import loop_status

        manifest = self._registry.manifest_for(name)
        home_dir = self._registry.entities_dir / manifest.slug

        chat = self.status(name)
        state = self._registry.state_of(name)
        loop = loop_status(home_dir)
        loop_running = bool(loop.get("running"))
        loop_phase = str(loop.get("phase") or "")

        posture: str
        phase: Optional[str]
        if chat.get("open"):
            phase, posture = "visit", "visiting"
        elif str(state.get("state") or "awake") == "paused":
            phase, posture = None, "paused"
        elif str(state.get("state") or "awake") == "asleep":
            # mode=visiting is VISIT BOOKKEEPING (auto-yield / mid-open
            # posture) — a visit is in progress on the durable lane, so the
            # GRAPH word is visit; rendering it "asleep" fabricated the
            # "went to sleep during personal" read of the 22:38 incident
            # (entity forensics c2465 finding 3).
            if str(state.get("mode") or "") == "visiting":
                phase, posture = "visit", "yielded"
            else:
                phase, posture = "sleep", "asleep"
        elif loop_running and loop_phase == "day":
            # Work day when a standing order exists (the loop's own day gate
            # reads the same file) — the /cognition fold's rule, mirrored so
            # the two composites can never disagree on the same instant.
            try:
                from abstractruntime.identity.life import read_work_order

                phase = "work" if read_work_order(home_dir) else "personal"
            except Exception:  # noqa: BLE001
                phase = "personal"
            posture = "day"
        elif loop_running:
            phase, posture = "personal", "resting"
        else:
            # No process, no visit, not paused: sleep is the resting
            # default. sleep_detail on /cognition carries the honesty
            # nuance; here the word alone retires the awake floor.
            phase, posture = "sleep", "resting"

        return {
            "entity_id": manifest.entity_id,
            "phase": phase,
            # The old vocabulary's nuance, verbatim — renderers that read
            # visiting/yielded/resting/paused as words read posture now.
            "posture": posture,
            "chat_open": bool(chat.get("open")),
            "chat_id": chat.get("chat_id"),
            "state": state.get("state"),
            "state_mode": state.get("mode"),
            "state_reason": state.get("reason"),
            "own_time_running": loop_running,
            "own_time_phase": loop_phase or None,
        }

    @staticmethod
    def _restore_prior_state(home_dir: Any, prior_state: Dict[str, str], *, suffix: str, token: str = "") -> None:
        """Failed-open restore: put the OPERATOR's pre-visit word back
        (mutual-exclusivity wave — the aborts used to hardcode awake/asleep,
        erasing the operator's word on half the paths).

        OWNERSHIP-CHECKED (wave adversary P1-2): the abort writes ONLY while
        the standing state is still THIS visit's posture (token match) — an
        operator pause/sleep landed mid-window is the coordination authority
        and stands (the close paths' exact predicate; without it a quiescence
        timeout could erase the kill switch). Best-effort: an abort surfaces
        its own error, never a restore failure."""
        from abstractruntime.identity.life import read_entity_state, write_entity_state

        try:
            if token:
                st = read_entity_state(home_dir)
                still_ours = (
                    str(st.get("state") or "") == "asleep"
                    and str(st.get("mode") or "") == "visiting"
                    and token in str(st.get("reason") or "")
                )
                if not still_ours:
                    return  # the state changed hands mid-window; it stands
            target = str((prior_state or {}).get("state") or "awake")
            if target not in ("awake", "asleep"):
                target = "awake"  # paused is the operator's act alone, never auto-restored
            reason = str((prior_state or {}).get("reason") or "")
            # A BOUNDED sleep keeps its deadline (runtime c343: wake_at is
            # first-class on the writer; dropping it slept past need-checks).
            wake_at = str((prior_state or {}).get("wake_at") or "") if target == "asleep" else ""
            write_entity_state(
                home_dir, target,
                reason=(f"{reason} {suffix}".strip() if reason else suffix),
                wake_at=wake_at,
            )
        except Exception:  # noqa: BLE001
            pass

    def has_open(self, slug: str) -> bool:
        """Live hosted session on this home? (The visit reaper's chat probe:
        a stale-yield repair must never fire under a LIVE drawer session.)"""
        with self._lock:
            return bool(self._by_slug.get(str(slug or "").strip().lower()))

    def close_open_visit(self, name: str, *, reflect: bool = True) -> Optional[Dict[str, Any]]:
        """Close the home's open visit if one exists (revocation + the
        deterministic-teardown path for POST /state and freeze). Returns the
        close result, or None when no visit was open. Idempotent."""
        manifest = self._registry.manifest_for(name)
        with self._lock:
            chat_id = self._by_slug.get(manifest.slug)
        if not chat_id:
            return None
        try:
            return self.close(chat_id, reflect=reflect)
        except ChatOpenRefused:
            return None  # already closing/closed — the race resolved itself

    def close_all(self, *, budget_s: float = 60.0) -> None:
        """Shutdown hygiene: reflect + close every live session (the loop
        wake duty travels with each). Mark-closed under each session's
        turn_lock (adversary P1): an in-flight turn finishes whole before
        its session closes — never a home closed under an executing turn.

        BOUNDED (resilience wave 2026-07-21, adversary P1-4): this runs on
        the SIGTERM/lifespan-shutdown path. An unbounded turn_lock acquire
        (wedged in-flight turn) or an unbounded reflection LLM call used to
        hang process shutdown until an external SIGKILL. Each session gets a
        bounded lock acquire; reflection is skipped (loudly) once the total
        budget is spent — an unreflected close loses a look-back the next
        open's pending-lookback salvage recovers, never memories.
        """
        deadline = time.monotonic() + max(0.0, float(budget_s))
        with self._lock:
            hosted_all = [h for h in self._sessions.values() if not h.closed]
        for h in hosted_all:
            remaining = deadline - time.monotonic()
            try:
                # Bounded acquire: a wedged in-flight turn must not hang
                # shutdown; skip the session (process exit abandons it and
                # the next open's pending-lookback salvage owns the debt).
                acquired = h.turn_lock.acquire(timeout=max(1.0, min(10.0, remaining)))
                if not acquired:
                    logger.warning(
                        "close_all: session %s turn_lock busy past deadline; skipping close (salvage at next open)",
                        getattr(h, "chat_id", "?"),
                    )
                    continue
                try:
                    if h.closed:
                        continue
                    h.closed = True
                finally:
                    h.turn_lock.release()
                # Reflection only while budget remains: it is an LLM call
                # with no deadline of its own.
                self._close_hosted(h, reflect=(deadline - time.monotonic()) > 0)
            except Exception:
                continue

    # -------------------------------------------------------------- internal
    def _get(self, chat_id: str) -> _HostedChat:
        with self._lock:
            hosted = self._sessions.get(str(chat_id or ""))
        if hosted is None:
            raise ChatOpenRefused(404, f"no chat session {chat_id!r}")
        return hosted

    def _close_hosted(self, hosted: _HostedChat, *, reflect: bool) -> Dict[str, Any]:
        from abstractruntime.identity.life import write_entity_state

        reflection: Optional[Dict[str, Any]] = None
        warnings: List[str] = []
        try:
            if reflect and hosted.session.reports:
                try:
                    reflection = hosted.session.reflect()
                except Exception as e:  # a reflection failure never voids the session
                    warnings.append(f"#FALLBACK reflection failed ({e}); the session's memories are intact")
            summary = hosted.session.close_summary()
        finally:
            try:
                hosted.home.close()
            except Exception:
                pass
            try:
                # RESTORE BY OWNERSHIP (mutual-exclusivity wave, laurent
                # dm#94, audit finding 4): every open writes the visiting
                # posture stamped with THIS visit's chat_id, so close matches
                # the posture's identity token — never vocabulary. An
                # operator sleep/pause landed mid-visit REWROTE the state
                # (token gone) and is the coordination authority: it stands
                # untouched (the old words-match let any lane's close adopt
                # any lane's posture — the cross-lane clobber class).
                from abstractruntime.identity.life import read_entity_state

                home_dir = self._registry.entities_dir / hosted.entity_slug
                st = read_entity_state(home_dir)
                owned = (
                    str(st.get("state") or "") == "asleep"
                    and str(st.get("mode") or "") == "visiting"
                    and f"[visit {hosted.chat_id}]" in str(st.get("reason") or "")
                )
                if owned:
                    prior = dict(hosted.prior_state or {})
                    if str(prior.get("state") or "awake") == "asleep":
                        # THE SLEEP RETURNS (skill c219 audit): this visit
                        # woke an operator-asleep entity (B1 doors-wake);
                        # the rest resumes when the visitor leaves — with
                        # its wake deadline intact (runtime c343 seam).
                        write_entity_state(
                            home_dir, "asleep",
                            reason=(str(prior.get("reason") or "") or "operator sleep restored")
                            + f" (visit ended: {len(hosted.session.reports)} turns)",
                            wake_at=str(prior.get("wake_at") or ""),
                        )
                    else:
                        # Wake-cue seeding (maintainer escalation 2026-07-09,
                        # R3 — "agency blindness"): the return reason carries
                        # the VISIT's facts (who came, how long, what he
                        # elected to pursue) so his next day can begin from
                        # the visit instead of the bridges default.
                        visitors = [p for p in hosted.session.participants if not p.startswith("entity:")]
                        reason = (
                            f"visitor session ended ({', '.join(visitors) or 'a visitor'}; "
                            f"{len(hosted.session.reports)} turns)"
                        )
                        interests = [words for _rid, words in (reflection or {}).get("interests", []) if words]
                        if interests:
                            reason += f" — you elected to pursue: {'; '.join(str(w)[:120] for w in interests[:2])}"
                        write_entity_state(home_dir, "awake", reason=reason)
                else:
                    warnings.append(
                        "state changed hands mid-visit — left standing (the posture no longer "
                        "carries this visit's token; state is the authority, no restore-write)"
                    )
            except Exception as e:  # noqa: BLE001 - close must finish; the state write is best-effort
                warnings.append(f"#FALLBACK could not restore the pre-visit state: {e}")
            with self._lock:
                self._sessions.pop(hosted.chat_id, None)
                if self._by_slug.get(hosted.entity_slug) == hosted.chat_id:
                    del self._by_slug[hosted.entity_slug]
            # The visit's write window ends HERE — after the home closed and
            # the wake write landed (both are home writes under this hold).
            if getattr(hosted, "lease", None) is not None:
                try:
                    hosted.lease.release()
                except Exception:
                    pass  # fd close releases the kernel lock regardless

        self._marker(
            hosted, "session_closed",
            details={"session_id": hosted.session.session_id, "turns": len(hosted.session.reports)},
        )
        out: Dict[str, Any] = {"summary": summary, "turns": len(hosted.session.reports)}
        if reflection:
            out["reflection"] = {
                "reply": reflection.get("reply"),
                "feelings_applied": reflection.get("feelings_applied", []),
                "interests": reflection.get("interests", []),
            }
        if warnings:
            out["warnings"] = warnings
        return out

    def _reap_idle_locked(self) -> None:
        import time as _time

        now = _time.monotonic()
        # Never reap a session with a turn IN FLIGHT (whole-package adversary
        # P1): last_activity only updates at turn END, so a long turn started
        # near the idle deadline used to be torn down mid-execution —
        # reflect() running concurrently with turn() over one ChatSession and
        # the home's stores closed under the turn's writes. The in-flight
        # flag is maintained under turn_lock; a skipped session is re-checked
        # next sweep.
        stale = [
            h for h in self._sessions.values()
            if not h.closed
            and not getattr(h, "turn_in_flight", False)
            and (now - h.last_activity) > self._idle_timeout_s
        ]
        if not stale:
            return
        # Close outside caller-visible state but inside our lock scope is
        # deadlock-prone (reflect calls the LLM); release-and-close instead.
        # Mark-closed happens under each session's turn_lock in the closer
        # thread (the route-path close()'s exact discipline) so a turn that
        # slipped in between this scan and the close is serialized, never
        # torn.
        def _close_later() -> None:
            for h in stale:
                try:
                    with h.turn_lock:
                        if h.closed or getattr(h, "turn_in_flight", False):
                            continue
                        h.closed = True
                    self._close_hosted(h, reflect=True)
                except Exception:
                    continue

        threading.Thread(target=_close_later, name="entity-chat-reaper", daemon=True).start()

    def _reap_forever(self) -> None:
        """Background sweep so idle sessions close (and yielded loops wake)
        WITHOUT waiting for the next API call. Sweep cadence is a fraction
        of the timeout — precise enough for a 15-minute idle rule."""
        import time as _time

        interval = max(30.0, min(self._idle_timeout_s / 5.0, 300.0))
        while True:
            _time.sleep(interval)
            try:
                with self._lock:
                    self._reap_idle_locked()
            except Exception:
                # The reaper must survive anything; a failed sweep retries
                # on the next interval.
                continue

    def _marker(self, hosted: _HostedChat, kind: str, *, details: Dict[str, Any]) -> None:
        """Best-effort host marker: the observable record matters, but a
        marker failure must never break a conversation."""
        try:
            from .entity_replay import record_host_marker

            home = self._registry.get_home(hosted.entity_slug)
            record_host_marker(
                entities_dir=self._registry.entities_dir,
                slug=hosted.entity_slug,
                entity_id=hosted.entity_id,
                kind=kind,
                journal_seq=int(home.memory.current_seq()),
                session_id=str(details.get("session_id") or ""),
                details=details,
            )
        except Exception:
            pass
