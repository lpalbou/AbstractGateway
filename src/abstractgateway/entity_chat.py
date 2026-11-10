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

# Attention-geometry defaults (maintainer ruling 2026-07-08): the wide summon
# posture is the DEFAULT, not a tuning trick. The request body overrides;
# these constants are the floor experience every entity gets. There is no
# environment variable for them (round 3, 2026-10-01: settings are never env
# vars).
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


def entity_recall_shelf_size(home_dir: Any) -> int:
    """Memories retrieved per turn for this entity when a request names no width:
    its cognition profile's `recall.shelf_size` (`<home>/cognition.yaml`, runtime
    `identity.cognition_profile`), else DEFAULT_ENTITY_CHAT_SHELF_SIZE. Visits,
    summons and the own-time loop all read it; a request's explicit width wins."""
    from pathlib import Path

    from abstractruntime.identity.cognition_profile import recall_shelf_size

    return recall_shelf_size(Path(home_dir), DEFAULT_ENTITY_CHAT_SHELF_SIZE)

# Output headroom: NONE, ever (operator 2026-10-02: "we should never use that
# parameter; by default models work at their full capacity"). The gateway never
# sends an output-token cap for an entity -- no code default, no env knob, no
# per-request field -- so AbstractCore uses the model's full advertised output
# capability (or omits the bound where the API allows). The former
# entity output-cap env var and the chat-open cap field were removed in 0.11.1.


# The entity's mind (round 3, operator 2026-10-01: "no env-var fallbacks for
# entity minds"). ONE persisted choice per entity -- an operator file in its
# home (substrate.yaml, beside tool_policy.yaml) -- used by visits, summons
# and its own time alike. Resolution: request body (explicit override) >
# the entity's own mind (substrate.yaml) > the gateway's text route
# (AbstractCore `output.text`, the console's default -- the same route runs
# use) > refusal only when the gateway has no text model at all. There is no
# environment variable in this chain any more (ABSTRACTGATEWAY_ENTITY_CHAT_*
# were removed) and no code default: an entity without its own mind thinks
# with the gateway default.
SUBSTRATE_FILENAME = "substrate.yaml"
NO_TEXT_MODEL_REFUSAL = (
    "This gateway has no text model yet: open Setup in the console, choose "
    "Use recommended defaults, then try again."
)
# Kept as an alias: the door's callers import this name.
SUBSTRATE_REFUSAL = NO_TEXT_MODEL_REFUSAL
MIND_SOURCE_REQUEST = "request"
MIND_SOURCE_ENTITY = "entity"
MIND_SOURCE_GATEWAY = "gateway"


def gateway_text_mind() -> Dict[str, Any]:
    """The gateway's text route (`output.text`, then `input.text`) read through
    the AbstractCore config seam -- the gateway-wide store, never a per-user
    overlay: an entity is not a user's session. Returns `{provider, model,
    base_url, reasoning}` with None for what is not configured."""
    from .core_config import text_default

    row = text_default(base_dir=None)
    return {
        "provider": (str(row.get("provider") or "").strip() or None),
        "model": (str(row.get("model") or "").strip() or None),
        "base_url": (str(row.get("base_url") or "").strip() or None),
        "reasoning": (str(row.get("reasoning") or "").strip() or None),
    }


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
        # MTP intent (round 3) is a gateway-written key the runtime reader
        # does not return: read it from the file the gateway wrote.
        if isinstance(out, dict) and out.get("provider") and out.get("model") and "speculation" not in out:
            local = _read_substrate_locally(home_dir)
            if "speculation" in local:
                out = dict(out)
                out["speculation"] = local["speculation"]
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
        spec = normalize_speculation(data.get("speculation"))
        if spec is not None:
            out["speculation"] = spec
        return out
    except Exception:
        return {}


def normalize_speculation(value: Any) -> Any:
    """The MTP intent at rest and on the wire (the kit's SpeculationValue):
    False = explicitly off; {"mode": "native_mtp", "num_draft_tokens": N,
    "require_acceleration": True} = on at depth N; None = not chosen (the
    gateway/model default applies). Anything else reads as None."""
    if value is False:
        return False
    if isinstance(value, dict):
        if str(value.get("mode") or "") == "off":
            return False
        n = value.get("num_draft_tokens")
        if value.get("mode") == "native_mtp" and isinstance(n, int) and not isinstance(n, bool) and n >= 1:
            return {"mode": "native_mtp", "num_draft_tokens": int(n), "require_acceleration": True}
    return None


def write_entity_substrate(
    home_dir: Any, *, provider: str, model: str, thinking: Optional[str] = None, speculation: Any = None
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

    data: Dict[str, Any] = {"provider": p, "model": m}
    t = str(thinking or "").strip()
    if t:
        data["thinking"] = t
    spec = normalize_speculation(speculation)
    if spec is not None:
        data["speculation"] = spec
    # Atomic write (adversary cycle-2 N7): a crash mid-write must never
    # leave malformed YAML that reads as "substrate unset".
    target = Path(home_dir) / SUBSTRATE_FILENAME
    tmp = target.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
    tmp.replace(target)


def clear_entity_substrate(home_dir: Any) -> bool:
    """Remove the entity's own mind: it thinks with the gateway default again.
    Returns True when a file was removed."""
    from pathlib import Path

    target = Path(home_dir) / SUBSTRATE_FILENAME
    if not target.exists():
        return False
    target.unlink()
    return True


def resolve_entity_mind(
    provider: Optional[str], model: Optional[str], *, home_dir: Any = None,
    thinking: Optional[str] = None,
) -> Dict[str, Any]:
    """Request override > the entity's own mind (substrate.yaml) > the
    gateway's text route > refusal (no text model configured at all).

    Returns `{provider, model, thinking, speculation, base_url, source}`:
    `source` is "request", "entity" or "gateway" (which step supplied the
    provider/model pair); `base_url` is the text route's own endpoint when the
    pair came from it (the round-3 base_url rule: runs on the default reach
    the route's address), else None. A request that names only one of
    provider/model completes it from the entity's own mind; a pair that is
    still half-filled refuses with a plain sentence. Reasoning effort and MTP
    follow the pair's source and never refuse."""
    p = (provider or "").strip()
    m = (model or "").strip()
    t = (thinking or "").strip()
    source = MIND_SOURCE_REQUEST if (p or m) else ""
    stored: Dict[str, Any] = read_entity_substrate(home_dir) if home_dir is not None else {}
    speculation = None
    base_url: Optional[str] = None
    if stored:
        # The entity's own mind fills what the request left out (explicit
        # pieces, never a silent mix with the gateway default).
        p = p or str(stored.get("provider") or "")
        m = m or str(stored.get("model") or "")
        t = t or str(stored.get("thinking") or "")
        source = source or MIND_SOURCE_ENTITY
        if p == stored.get("provider") and m == stored.get("model"):
            speculation = stored.get("speculation")
    elif not p and not m:
        route = gateway_text_mind()
        if not (route["provider"] and route["model"]):
            raise ChatOpenRefused(400, NO_TEXT_MODEL_REFUSAL)
        p, m = route["provider"], route["model"]
        t = t or (route["reasoning"] or "")
        base_url = route["base_url"]
        source = MIND_SOURCE_GATEWAY
    if not p or not m:
        missing = "model" if p else "provider"
        raise ChatOpenRefused(
            400,
            f"The mind needs a {missing} as well: choose both in the entity's Mind settings, "
            "or use the Gateway default.",
        )
    return {
        "provider": p, "model": m, "thinking": (t or None), "speculation": speculation,
        "base_url": base_url, "source": source,
    }


def resolve_substrate(
    provider: Optional[str], model: Optional[str], *, home_dir: Any = None,
    thinking: Optional[str] = None,
) -> Tuple[str, str, Optional[str]]:
    """(provider, model, thinking) from `resolve_entity_mind` -- the one chain."""
    mind = resolve_entity_mind(provider, model, home_dir=home_dir, thinking=thinking)
    return mind["provider"], mind["model"], mind["thinking"]


LOCAL_BASE_URL_PROVIDERS = ("lmstudio", "openai-compatible", "openai_compatible")


def entity_base_url(provider: str, explicit: Optional[str] = None, *, mind_base_url: Optional[str] = None) -> Optional[str]:
    """The endpoint for an entity's mind: the request's explicit base_url >
    the gateway text route's own base_url when the provider is the route's
    provider > None (the provider's own address applies)."""
    e = str(explicit or "").strip()
    if e:
        return e
    if mind_base_url:
        return str(mind_base_url).strip() or None
    from .provider_defaults import default_text_route_connection_kwargs

    return default_text_route_connection_kwargs(provider, base_dir=None).get("base_url") or None


class ChatOpenRefused(Exception):
    """A refusal with an HTTP status shape (409 conflicts, 400 bad asks)."""

    def __init__(self, status: int, detail: str, *, code: str = "") -> None:
        super().__init__(detail)
        self.status = int(status)
        self.detail = detail
        # A stable machine code beside the sentence (served as {detail, code}
        # by the routes when set; contract-phases §7).
        self.code = str(code or "").strip()


class _RuntimeLLMAdapter:
    """The driver's duck-typed llm contract (`.generate(messages=...,
    system_prompt=...) -> .content`) over the RUNTIME's exported client.

    Boundary ruling (backlog 0059, landing night): the gateway never
    imports abstractcore directly — the one violation this file briefly
    carried was mine and is closed here. `LocalAbstractCoreLLMClient` is
    AbstractRuntime's sanctioned in-process LLM surface; it returns
    RunState-safe dicts, so this adapter lifts `content` back into the
    attribute shape the chat driver expects."""

    def __init__(
        self, provider: str, *, model: str, thinking: Optional[str] = None, speculation: Any = None,
        patience: Optional[Dict[str, Any]] = None, **llm_kwargs: Any
    ) -> None:
        from .generation_pool import generation_client

        # R2 (2026-10-29): the principal's runtime pool (one resident model shared with the runs);
        # a client of its own only when there is no pool (ADR 0002: in the model host child then).
        # `patience` (the entity lanes' timeout window) shapes a client of its own; the pool keeps its own.
        self._client = generation_client(
            provider=provider, model=model, llm_kwargs=dict(llm_kwargs) or None, patience=patience,
        )
        self._provider = str(provider or "").strip()
        self._model = str(model or "").strip()
        # Who answers (the authoring failure lines and receipts name it; never None).
        self.model_info = {"provider": self._provider, "model": self._model, "thinking": str(thinking or "").strip() or None}
        # The mind's reasoning effort (substrate triple). Injected into each
        # call's params on the one wire name; None sends nothing.
        self._thinking = str(thinking or "").strip() or None
        # The mind's MTP intent (round 3): None sends nothing; False or a
        # native_mtp request rides params["speculation"] (the runtime's wire).
        self._speculation = normalize_speculation(speculation)

    def generate(self, **kwargs: Any) -> Any:
        from types import SimpleNamespace

        kwargs.setdefault("prompt", "")
        if self._thinking or self._speculation is not None:
            params = kwargs.get("params")
            params = dict(params) if isinstance(params, dict) else {}
            if self._thinking:
                params.setdefault("thinking", self._thinking)
            if self._speculation is not None:
                params.setdefault("speculation", self._speculation)
            kwargs["params"] = params
        # A saved MTP request the mind's model cannot honour is dropped, never a failed turn
        # (operator rule 2026-11-07; speculation_guard.py), and the reply's metadata says so.
        note = None
        params = kwargs.get("params")
        if isinstance(params, dict) and "speculation" in params:
            from .generation_pool import PoolGeneration
            from .speculation_guard import drop_unsupported_speculation

            source = self._client.pool if isinstance(self._client, PoolGeneration) else self._client
            params = dict(params)
            note = drop_unsupported_speculation(source, params, provider=self._provider, model=self._model)
            kwargs["params"] = params
        from .generation_pool import PoolGeneration, structured_generate

        if isinstance(self._client, PoolGeneration):
            out = self._client.generate(**kwargs)
        else:
            # A client of its own takes structured output on the same wire as the pool.
            out = structured_generate(self._client.generate, kwargs, provider=self._provider, model=self._model)
        if note is not None:
            from .speculation_guard import attach_note

            out = attach_note(out, note)
        if isinstance(out, dict):
            return SimpleNamespace(**{"content": out.get("content"), **{k: v for k, v in out.items() if k != "content"}})
        return out


def _default_llm_factory(provider: str, **kwargs: Any) -> Any:
    model = str(kwargs.pop("model", "") or "")
    thinking = kwargs.pop("thinking", None)
    speculation = kwargs.pop("speculation", None)
    patience = kwargs.pop("patience", None)
    return _RuntimeLLMAdapter(
        provider, model=model, thinking=thinking, speculation=speculation, patience=patience, **kwargs
    )


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
    # The LifecycleFence this visit was admitted at (STOP-LANE-DESIGN §2.2): the home was opened
    # with it (memory guard, book, artifacts, the session's every model call and effect) and the
    # close's restore is fenced on it.
    fence: Any = None


class EntityChatHost:
    """Open/turn/close over hosted ChatSessions, one live session per home."""

    def __init__(
        self,
        registry: Any,
        *,
        llm_factory: Optional[Callable[..., Any]] = None,
        idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
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
        enable_tools: bool = True,
        enable_workspace: bool = False,
    ) -> Dict[str, Any]:
        """Open the drawer's visit. ONE admission (Codex review 3 #6): the generation read at the door's
        arrival runs through every wait and retry of the open, and the open waits its turn behind the
        visitors who arrived before it (`entity_visits.ARRIVALS`, shared with the durable door)."""
        from .entity_phase import GenerationMoved, admission_fence, generation_of
        from .entity_visits import ARRIVALS, STOPPED_VISIT_SENTENCE, VisitRefused

        slug = self._registry.manifest_for(name).slug  # KeyError -> 404 at the route
        try:
            admission = generation_of(self._registry.entities_dir, slug)
        except GenerationMoved:
            raise ChatOpenRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume") from None
        try:
            with ARRIVALS.turn(str(self._registry.entities_dir / slug),
                               admission_fence(self._registry.entities_dir, slug, admission)):
                return self._open_admitted(
                    name, admission, provider=provider, model=model, base_url=base_url, participants=participants,
                    context_window=context_window, shelf_size=shelf_size, enable_tools=enable_tools,
                    enable_workspace=enable_workspace)
        except VisitRefused as e:
            if e.code == "stopped_use_resume":
                raise ChatOpenRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume") from None
            raise

    def _open_admitted(
        self,
        name: str,
        _admission: int,
        *,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        participants: Optional[List[str]] = None,
        context_window: Optional[int] = None,
        shelf_size: Optional[int] = None,
        enable_tools: bool = True,
        enable_workspace: bool = False,
    ) -> Dict[str, Any]:
        # Attention geometry (maintainer ruling 2026-07-08, after the live A/B
        # on Castor): the wide posture IS the default. Resolution order:
        # request body > the home's cognition profile (shelf only, read below
        # once the home is known) > these code defaults. No environment
        # variable (round 3).
        env_warnings: List[str] = []
        if context_window is None:
            context_window = DEFAULT_ENTITY_CHAT_CONTEXT_WINDOW
        import time as _time

        from abstractruntime.identity.chat import ChatSession, open_home
        from abstractruntime.identity.life import (
            await_loop_quiescent,
            read_entity_state,
            read_loop_status,
        )

        manifest = self._registry.manifest_for(name)  # KeyError -> 404 at the route
        # Wake entry point (round 3): an archived entity is never talked to (entity_access.py).
        from .entity_access import EntityArchivedError, refuse_if_entity_archived, users_path_of

        try:
            refuse_if_entity_archived(manifest.slug, users_path=users_path_of(self._registry))
        except EntityArchivedError as e:
            raise ChatOpenRefused(409, e.message) from None
        slug = manifest.slug
        home_dir = self._registry.entities_dir / slug
        if shelf_size is None:
            shelf_size = entity_recall_shelf_size(home_dir)

        # Maintenance window: refuse BEFORE any provider probe or state
        # read — a held home must answer 409 first, never 502 on an
        # unrelated substrate check (doctoring, operator GO 2026-07-13).
        self._registry._refuse_if_held(slug)

        # ONE mind per entity (maintainer ruling 2026-07-09 06:32: visits and
        # own time share the SAME mind). Request override > the entity's own
        # mind (substrate.yaml) > the gateway's text route; refusal only when
        # the gateway has no text model at all.
        mind = resolve_entity_mind(provider, model, home_dir=home_dir)
        provider, model, thinking = mind["provider"], mind["model"], mind["thinking"]
        speculation = mind["speculation"]
        base_url = entity_base_url(provider, base_url, mind_base_url=mind["base_url"])

        # Another drawer session holds the home: wait for it to close or idle out, without a
        # deadline (a visit never fails, Codex review 2 #9); only the kill switch ends the wait.
        from .entity_visits import STOPPED_VISIT_SENTENCE
        from .entity_visits import _wait_while as _wait_open

        _open_gen = int(_admission)  # the door's one admission (Codex review 3 #6)

        def _drawer_live() -> bool:
            with self._lock:
                self._reap_idle_locked()
                return self._by_slug.get(slug) is not None

        try:
            _wait_open(_drawer_live, self._registry.entities_dir, slug, _open_gen)
        except Exception as e:  # VisitRefused(stopped) -> the chat door's own refusal
            if getattr(e, "code", None) == "stopped_use_resume":
                raise ChatOpenRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume") from None
            raise
        # THE SUMMON LANE too (conversation-seat plan, item 5: one seat,
        # three doors): a summon turn mid-flight holds the one life. The
        # probe answers only LIVE runs — a TTL-idle summon seat never blocks
        # the drawer's own chat open.
        if self._summon_seat_probe is not None:
            # A VISIT NEVER FAILS on a live summon (Codex review 2026-11-09 #6): the door waits,
            # without a deadline, for the summon's turn to end; only the kill switch ends the wait.
            from .entity_visits import STOPPED_VISIT_SENTENCE, VisitRefused, _wait_while

            def _summon_live() -> bool:
                try:
                    return self._summon_seat_probe(slug) is not None
                except Exception:  # noqa: BLE001 - a broken probe must not block the door
                    return False

            try:
                _wait_while(_summon_live, self._registry.entities_dir, slug, _open_gen)
            except VisitRefused:
                raise ChatOpenRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume") from None

        state = read_entity_state(home_dir)
        mode = str(state.get("mode") or "")
        reason = str(state.get("reason") or "")
        if state.get("state") == "paused":
            from .entity_visits import STOPPED_VISIT_SENTENCE

            raise ChatOpenRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume")

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
            # The same sleep continues after the visit (contract-phases §4.4).
            "written_by": str(state.get("written_by") or ""),
            "changed_at": str(state.get("changed_at") or ""),
            "mode": str(state.get("mode") or ""),
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
                # Another visit is opening (Codex review #6: no "retry shortly"): wait, without a
                # deadline, until it registers or goes, then pass every gate again.
                from .entity_visits import STOPPED_VISIT_SENTENCE, VisitRefused, _wait_while

                def _opening() -> bool:
                    a2 = visiting_posture_age_s(read_entity_state(home_dir))
                    with self._lock:
                        registered = slug in self._by_slug
                    return a2 is not None and a2 < float(EntityVisitHost.STALE_YIELD_GRACE_S) and not registered

                try:
                    _wait_while(_opening, self._registry.entities_dir, slug, _open_gen)
                except VisitRefused:
                    raise ChatOpenRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume") from None
                return self._open_admitted(name, _open_gen, provider=provider, model=model, base_url=base_url,
                                           participants=participants, context_window=context_window,
                                           shelf_size=shelf_size, enable_tools=enable_tools,
                                           enable_workspace=enable_workspace)
            prior_state = {"state": "awake", "reason": ""}
        elif state.get("state") == "asleep":
            # B1 ruling (laurent 04:58; newborn shape c1503: doors WAKE, they
            # don't refuse): an operator-asleep entity is visitable — the
            # posture write below supersedes the old explicit awake write;
            # close restores the operator's sleep from prior_state.
            woke_for_visit = True
        loop_alive = bool(read_loop_status(home_dir).get("running"))
        posture_reason = f"in conversation with {visitor} {visit_token}" + (" (auto-yield)" if loop_alive else "")
        # The phase this visit interrupts is observed BEFORE the posture lands (resume_to).
        from .entity_phase import GenerationMoved, admit_visit_posture, default_hosts, generation_of, safe_publish

        # The door's admission is fenced on the lifecycle generation it saw at arrival (review #1; Codex review 3
        # #6: never a fresh one read after the waits).
        generation = _open_gen
        safe_publish(self._registry, name, "observed", chat_host=self, visit_host=default_hosts()[1])
        try:
            admit_visit_posture(self._registry.entities_dir, slug, generation, reason=posture_reason)
        except GenerationMoved:
            from .entity_visits import STOPPED_VISIT_SENTENCE

            raise ChatOpenRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume") from None
        if loop_alive:
            # A VISIT NEVER FAILS (operator 2026-11-09, root ADR-0027): no
            # limit on this wait — the durable lane's rule (entity_visits),
            # the posture held while it waits.
            from .entity_visits import STOPPED_VISIT_SENTENCE, VisitRefused, hold_visiting_posture

            try:
                await_loop_quiescent(
                    home_dir, timeout_seconds=None,
                    sleep_fn=hold_visiting_posture(home_dir, reason=posture_reason, token=visit_token,
                                                   generation=generation),
                )
            except VisitRefused:
                # The kill switch landed while the door waited (contract-phases §3.3).
                raise ChatOpenRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume") from None
            yielded = True
        from .entity_phase import admission_fence, moved_since

        if read_entity_state(home_dir).get("state") == "paused" or moved_since(self._registry.entities_dir, slug, generation):
            from .entity_visits import STOPPED_VISIT_SENTENCE

            raise ChatOpenRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume")
        # THE VISIT'S FENCE (STOP-LANE-DESIGN §2.2): the door's admission, the one generation this
        # whole visit runs at — the home opens with it, the session's every model call, tool and
        # effect checks it, and the close's restore is fenced on it.
        visit_fence = admission_fence(self._registry.entities_dir, slug, generation)

        # One writer per home (plan item 1, GW-A): the visit IS a write
        # window. Acquired AFTER the auto-yield (the loop's day lease is
        # released at the boundary we just waited for); anything still
        # holding the home — a CLI visit, a dream pass, a maintenance run —
        # makes this open wait (logged once, naming the holder). The auto-yield
        # negotiation stays ABOVE the lease (policy); this is the belt.
        lease: Any = None
        try:
            from abstractruntime.storage.lease import DirectoryLeaseHeld, acquire_directory_lease

            # A visit never fails (operator 2026-11-09): another writer (a
            # sleep's consolidation pass) holds the home only until its step
            # ends, and it stops for the visiting posture — wait, no limit.
            logged = False
            while lease is None:
                try:
                    lease = acquire_directory_lease(home_dir, holder="visit-host")
                except DirectoryLeaseHeld as e:
                    if read_entity_state(home_dir).get("state") == "paused" or visit_fence.moved():
                        from .entity_visits import STOPPED_VISIT_SENTENCE

                        raise ChatOpenRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume") from None
                    if not logged:
                        logger.info("visit waits for the home's writer to finish: %s", e)
                        logged = True
                    time.sleep(0.5)
        except ImportError:
            # Older runtime without storage.lease: no site acquires (the
            # loop's sites are absent too) — labeled, never silent.
            env_warnings.append(
                "#FALLBACK runtime predates the home lease; visit opened without the writer mutex"
            )

        try:
            home = open_home(home_dir, embedder=self._registry._resolve_embedder(), fence=visit_fence)
            llm_kwargs: Dict[str, Any] = {"model": model}
            # No output cap ever rides the wire: the model works at full capacity.
            if thinking:
                # The mind's reasoning effort rides every turn of this visit.
                llm_kwargs["thinking"] = thinking
            if speculation is not None:
                # The mind's MTP intent (round 3) rides every turn like thinking.
                llm_kwargs["speculation"] = speculation
            if base_url:
                llm_kwargs["base_url"] = base_url
                if not str(provider).strip().lower().startswith("endpoint:"):
                    # The configured key goes to this address only when it is the
                    # configured one (security, 0.21.0).
                    from .provider_connections import configured_provider_request_kwargs

                    llm_kwargs.update(
                        configured_provider_request_kwargs(
                            str(provider).strip().lower(),
                            current_base_dir=self._registry.data_dir,
                            root_base_dir=self._registry.root_data_dir,
                            base_url=base_url,
                        )
                    )
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
            session = ChatSession(home, llm, fence=visit_fence, **session_kwargs)
            # The models other than the mind (the summaries capability authors every record's title
            # and summary at formation, runtime 4eefddb): without it every turn forms pending records.
            session.llm_for_capability = self._registry.capability_llm_factory(slug)
            # CANCELLATION AT EVERY EFFECT BOUNDARY (Codex review 2 #2; runtime 2c9b673): the session
            # carries the home's fence (`ChatSession.fence`, from open_home), so once a Stop moves the
            # lifecycle generation its turn runs no further tool, model call, formation or authoring.
            if session.fence is not visit_fence:
                raise RuntimeError("the hosted chat session does not carry the visit's fence (runtime drift)")
        except SystemExit as e:
            # A refused prelude aborts the summon — the reasons travel verbatim.
            if lease is not None:
                lease.release()
            self._restore_prior_state(home_dir, prior_state, suffix="(visit aborted: prelude refused)", token=visit_token,
                                      fence=visit_fence)
            raise ChatOpenRefused(409, f"summon refused: {'; '.join(quiet) or e}")
        except BaseException:
            if lease is not None:
                lease.release()
            self._restore_prior_state(home_dir, prior_state, suffix="(visit aborted: open failed)", token=visit_token,
                                      fence=visit_fence)
            raise

        # The reflection-loss guard, web half (a2a 0007/171500Z): if a
        # PREVIOUS session died unreflected (write-ahead marker), its
        # look-back runs FIRST, over its own sheet, attributed to its own
        # session id — never a re-opened closed sheet. Version-tolerant:
        # older drivers without the guard just skip (labeled).
        from abstractruntime.identity.chat import TurnCancelled

        from .entity_phase import GenerationMoved, fenced
        from .entity_visits import STOPPED_VISIT_SENTENCE

        salvage: Optional[Dict[str, Any]] = None
        stopped_while_opening = False
        lookback = getattr(session, "run_pending_lookback", None)
        if callable(lookback):
            try:
                salvage = lookback()
            except TurnCancelled:
                stopped_while_opening = True  # the operator's Stop (the session's fence): never a failure line
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
            model_info={"provider": str(provider), "model": str(model), "source": str(mind["source"])},
            lease=lease,
            last_activity=_time.monotonic(),
            prior_state=dict(prior_state),
            fence=visit_fence,
        )
        # REGISTRATION IS FENCED ON THE ORIGINAL ADMISSION (Codex review 3 #8): the session becomes visible to
        # Stop (`stop_open_visit` reads `_by_slug`) in one locked step with the check of the generation this
        # visit was admitted at - a Stop that landed anywhere since (construction, the salvage) refuses the
        # open, and a Stop after it finds the session and closes it. A refused session is disposed: its home
        # closed, its lease released, nothing restored over the Stop.
        try:
            if stopped_while_opening:
                raise GenerationMoved("stopped while the visit opened")
            with fenced(self._registry.entities_dir, slug, visit_fence):
                with self._lock:
                    self._sessions[chat_id] = hosted
                    self._by_slug[slug] = chat_id
        except GenerationMoved:
            try:
                home.close()
            except Exception:  # noqa: BLE001
                pass
            if lease is not None:
                lease.release()
            raise ChatOpenRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume") from None

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
            # Which mind this visit thinks with and where it came from
            # ("entity" = its own choice, "gateway" = the gateway default).
            "mind": {"provider": str(provider), "model": str(model), "thinking": thinking, "source": mind["source"]},
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
            from abstractruntime.identity.chat import TurnCancelled

            try:
                reply, report = hosted.session.turn(
                    user_text, **({"speaker_label": speaker_label} if speaker_label else {})
                )
            except TurnCancelled as cancelled:
                # The kill switch stopped this turn: a clean record, stopped by the operator — never
                # a failure, never a reply (nothing of the turn was formed).
                from .entity_phase import KILL_SWITCH_REASON
                from .entity_visits import STOPPED_VISIT_SENTENCE

                hosted.transcript.append({
                    "turn_id": getattr(getattr(cancelled, "report", None), "turn_id", None),
                    "speaker": speaker_label or (hosted.session.participants[0] if hosted.session.participants else "person:operator"),
                    "text": user_text, "reply": None, "status": "cancelled", "reason": KILL_SWITCH_REASON,
                    "cancelled_by": "operator", "at": datetime.now(timezone.utc).isoformat(),
                })
                raise ChatOpenRefused(409, STOPPED_VISIT_SENTENCE, code="stopped_use_resume") from None
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
            # What this turn formed or staged, beside the served reply (turn.notices, AbstractRuntime 09cbad7:
            # TurnReport.reply_notices); `notices` above stays the operator's log lines.
            "reply_notices": [dict(n) for n in report.reply_notices],
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
            "prior_state": {"state": str((hosted.prior_state or {}).get("state") or "") or None},
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
        """The gateway-computed life phase: ONE derivation, `entity_phase` (contract-phases v2
        §1.2) — this host only delegates, so no second fold can drift from /life_state."""
        from .entity_phase import default_hosts, publish

        _chat, visit = default_hosts()
        return publish(self._registry, name, "observed", chat_host=self, visit_host=visit)

    @staticmethod
    def _restore_prior_state(home_dir: Any, prior_state: Dict[str, str], *, suffix: str, token: str = "",
                             fence: Any) -> None:
        """Failed-open restore: put the OPERATOR's pre-visit word back
        (mutual-exclusivity wave — the aborts used to hardcode awake/asleep,
        erasing the operator's word on half the paths).

        OWNERSHIP-CHECKED (wave adversary P1-2): the abort writes ONLY while
        the standing state is still THIS visit's posture (token match) — an
        operator pause/sleep landed mid-window is the coordination authority
        and stands (the close paths' exact predicate; without it a quiescence
        timeout could erase the kill switch). Best-effort: an abort surfaces
        its own error, never a restore failure. FENCED on the visit's admission (`fence`): nothing is
        written over a Stop since the visit was admitted."""
        from abstractruntime.identity.life import read_entity_state, write_entity_state

        from pathlib import Path as _P

        from .entity_phase import fenced

        home = _P(home_dir)
        try:
            with fenced(home.parent, home.name, fence) as rec:
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
                if target == "asleep":
                    # The same sleep continues: its original reason, writer, wake time (runtime
                    # c343) and since; the aborted open is not part of its reason.
                    from .entity_phase import continue_sleep

                    continue_sleep(home.parent, home.name, dict(prior_state or {}), fence=rec.fence)
                    return
                reason = str((prior_state or {}).get("reason") or "")
                write_entity_state(home_dir, target, reason=(f"{reason} {suffix}".strip() if reason else suffix),
                                   fence=rec.fence)
        except Exception:  # noqa: BLE001 - GenerationMoved included: a Stop's word stands
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

    def stop_open_visit(self, name: str) -> Dict[str, Any]:
        """The kill switch's chat row (contract-phases §3.1; review 2026-11-09 #4). Never waits on
        cognition: an in-flight turn is told to yield at its next model/tool boundary
        (`ChatSession.yield_check`), the session leaves the host's live index at once, and its close
        (no reflection) runs when the turn lets go of the session. Returns {outcome, chat_id}."""
        manifest = self._registry.manifest_for(name)
        with self._lock:
            chat_id = self._by_slug.get(manifest.slug)
            hosted = self._sessions.get(chat_id) if chat_id else None
        if hosted is None:
            return {"outcome": "not_open", "chat_id": None}
        # No further effect of the turn: the Stop already moved the session's fence (its bump ran before
        # this row); the yield ends the turn at its next boundary too.
        try:
            hosted.session.yield_check = lambda: True
        except Exception:  # noqa: BLE001 - a session without the seam still closes after its turn
            pass
        # Its close record says what ended it: cancelled by the operator's kill switch.
        hosted.stopped_by_switch = True
        if hosted.turn_lock.acquire(blocking=False):
            try:
                if hosted.closed:
                    return {"outcome": "not_open", "chat_id": chat_id}
                hosted.closed = True
            finally:
                hosted.turn_lock.release()
            self._close_hosted(hosted, reflect=False)
            return {"outcome": "closed", "chat_id": chat_id}
        with self._lock:
            if self._by_slug.get(manifest.slug) == chat_id:
                del self._by_slug[manifest.slug]

        def _close_after_the_turn() -> None:
            with hosted.turn_lock:
                if hosted.closed:
                    return
                hosted.closed = True
            try:
                self._close_hosted(hosted, reflect=False)
            except Exception:  # noqa: BLE001
                logger.exception("the stopped chat %s could not be closed after its turn", chat_id)

        threading.Thread(target=_close_after_the_turn, name=f"entity-chat-stop-{chat_id}", daemon=True).start()
        return {"outcome": "cancel_requested", "chat_id": chat_id}

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
        # Shutdown is not a visit's end: no own time is started while the gateway stops.
        self._shutting_down = True
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

        from .entity_phase import GenerationMoved, fenced

        reflection: Optional[Dict[str, Any]] = None
        warnings: List[str] = []
        sleep_continued = False
        try:
            if reflect and hosted.session.reports:
                from abstractruntime.identity.chat import TurnCancelled

                try:
                    reflection = hosted.session.reflect()
                except TurnCancelled:
                    pass  # the operator's Stop (the session's fence): no look-back, and never a failure line
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

                # One locked step with the kill switch's write (review 2026-11-09 #1), FENCED on the
                # visit's admission (STOP-LANE-DESIGN T10): nothing is restored over a Stop since.
                with fenced(self._registry.entities_dir, hosted.entity_slug, hosted.fence) as rec:
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
                            # the same sleep resumes when the visitor leaves —
                            # its original reason, writer, wake deadline (runtime
                            # c343 seam) and since; the visit is its
                            # `session_closed` marker, never part of the reason.
                            from .entity_phase import continue_sleep

                            continue_sleep(self._registry.entities_dir, hosted.entity_slug, prior, fence=rec.fence)
                            sleep_continued = True
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
                            write_entity_state(home_dir, "awake", reason=reason, fence=rec.fence)
                    else:
                        warnings.append(
                            "state changed hands mid-visit — left standing (the posture no longer "
                            "carries this visit's token; state is the authority, no restore-write)"
                        )
            except GenerationMoved:
                pass  # the operator's Stop since the visit was admitted: its word stands, nothing restored
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

        details_closed: Dict[str, Any] = {"session_id": hosted.session.session_id, "turns": len(hosted.session.reports)}
        if sleep_continued:
            details_closed["sleep_continues"] = True  # the sleep this visit interrupted goes on, unchanged
        if getattr(hosted, "stopped_by_switch", False):
            details_closed.update({"status": "cancelled", "closed_by": "kill_switch", "by": "operator",
                                   "reason": "kill_switch: stopped by the operator"})
        self._marker(hosted, "session_closed", details=details_closed)
        # Back to the previous phase where the toggles allow it; the gateway restarts own time
        # when personal is on (contract-phases v2 §4.2, §4.4).
        if not getattr(self, "_shutting_down", False):
            from .entity_phase import after_visit_closed, default_hosts

            after_visit_closed(self._registry, hosted.entity_slug, dict(hosted.prior_state or {}), cause="chat_closed",
                               chat_host=self, visit_host=default_hosts()[1])
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
